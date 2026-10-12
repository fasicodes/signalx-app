"""
Signals FM - signal engine v3.

What it does
  * Works on CLOSED 4-hour candles (a signal never changes while a candle is
    still forming).
  * Builds 82 scale-free features per coin: price action, momentum, trend,
    volatility, volume, the daily trend, Bitcoin's trend, and the state of
    the whole market (16 large coins: breadth, average momentum, and how
    strong this coin is relative to the others).
  * A gradient-boosted model (trained on Jul 2020 - Sep 2025, 21 coins,
    exported to signal_model.json) estimates, for LONG and for SHORT, the
    probability that target 1 is reached before the stop. A calibration
    curve turns that into an honest win probability.
  * Which candles become signals (all 21 coins are read together):
      - "strong": the model's score is in its top 3%;
      - "standard": if no coin has had a signal for 24 hours, the best free
        coin gets one when its score is at least the model's median.
    One trade at a time per coin; everything else is WAIT.
  * Trade plan: stop = 2 x ATR(14) from entry (1R). Three targets:
    TP1 = 0.5R, TP2 = 1R (as far as the stop), TP3 = 2R. A third of the
    position closes at each target; after TP1 the stop moves to the entry,
    after TP2 to TP1. Time limit 54 candles (9 days). Fees are counted as
    0.12% round trip.

Measured on data the model never saw (Oct 2025 - Oct 2026, 21 coins) and in
a walk-forward test (Jan 2023 - Oct 2026): see MODEL["test"] and
MODEL["walk_forward"] / signal_model.json.

This module is pure numpy/pandas (no sklearn at runtime). main.py, the
auto-trade bot and the track record all call it, so every surface shows the
same signal.
"""
import json
import math
import os
import re
import threading
import time
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

ENGINE_VERSION = "3.0"
TIMEFRAME = "4h"
TF_SEC = 4 * 3600
HISTORY_BARS = 600            # 4h candles fetched per coin (100 days; features need ~230)
REPLAY_BARS = 300             # candles replayed to find each coin's running trade (~50 days)
WARMUP_BARS = 240             # first candles of a series are only used to warm up the features
UNIVERSE = ["ADA/USDT", "ATOM/USDT", "AVAX/USDT", "BCH/USDT", "BNB/USDT", "BTC/USDT", "DOGE/USDT", "DOT/USDT",
            "ETH/USDT", "LINK/USDT", "LTC/USDT", "NEAR/USDT", "SOL/USDT", "TRX/USDT", "XLM/USDT", "XRP/USDT"]
COST_ROUND_TRIP = 0.0012      # 0.05% taker in + 0.05% out + 0.02% slippage
EPS = 1e-12
RECENT_KEYS = ("side", "entry", "stop_loss", "take_profit", "targets", "confidence", "tier", "signal_at", "closed_at",
               "status", "exit_price", "r", "hits")
RECENT_MAX = 30               # closed signals kept in a result's "recent" list (chart markers)
CLOSED_STATUSES = ("SL", "TP1", "TP2", "TP3", "EXPIRED")
_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_model.json")


# ===========================================================================
# model (exported gradient-boosted trees + calibration + trade plan)
# ===========================================================================
class _Model:
    def __init__(self, path=_MODEL_PATH):
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        self.meta = m
        self.version = str(m.get("version", ENGINE_VERSION))
        self.features = m["features"]
        self.baseline = float(m["baseline"])
        self.trees = [{k: np.asarray(v) for k, v in t.items()} for t in m["trees"]]
        self.cal_x = np.asarray(m["calibration"]["x"], dtype=float)
        self.cal_y = np.asarray(m["calibration"]["y"], dtype=float)
        self.threshold = float(m["threshold"])                          # "strong" signal level
        self.watch_threshold = float(m.get("watch_threshold", m["threshold"]))
        self.floor = float(m.get("floor", m["threshold"]))              # "standard" signal level
        self.gap_hours = float(m.get("gap_hours", 0) or 0)               # 0 = no standard signals
        lv = m["levels"]
        self.sl_atr = float(lv["sl_atr"])
        self.targets_r = [float(x) for x in (lv.get("targets_r") or [float(lv["tp_atr"]) / float(lv["sl_atr"])])]
        fr = lv.get("fractions") or [1.0 / len(self.targets_r)] * len(self.targets_r)
        self.fractions = [float(x) for x in fr]
        self.be_after = int(lv.get("be_after", 0) or 0)                 # 0 = the stop never moves
        self.max_bars = int(lv["max_bars"])
        self.tp_atr = self.sl_atr * self.targets_r[-1]                  # final target distance, in ATR
        self.signal_coins = list(m.get("signal_coins") or UNIVERSE)

    def raw_proba(self, X):
        """X: 2-D float array (rows x features, model order). NaN = missing."""
        X = np.asarray(X, dtype=float)
        out = np.full(len(X), self.baseline)
        rows = np.arange(len(X))
        for t in self.trees:
            node = np.zeros(len(X), dtype=np.int64)
            active = ~t["leaf"][node].astype(bool)
            while active.any():
                n = node[active]
                x = X[rows[active], t["f"][n]]
                go_left = np.where(np.isnan(x), t["m"][n].astype(bool), x <= t["t"][n])
                node[active] = np.where(go_left, t["l"][n], t["r"][n])
                active = ~t["leaf"][node].astype(bool)
            out += t["v"][node]
        return 1.0 / (1.0 + np.exp(-out))

    def calibrate(self, p):
        return np.interp(p, self.cal_x, self.cal_y)


_model = None
_model_lock = threading.Lock()


def model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = _Model()
    return _model


def signal_coins():
    """Coins that get trade signals (the coins the model was trained and tested on)."""
    return model().signal_coins


# ===========================================================================
# indicators / features (identical to the research code)
# ===========================================================================
def _wilder(s, n):
    return s.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()


def _rsi(close, n):
    d = close.diff()
    up, dn = _wilder(d.clip(lower=0), n), _wilder(-d.clip(upper=0), n)
    out = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    return out.where(dn != 0, 100.0).where(up.notna())


def _atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return _wilder(tr, n)


def _adx(df, n=14):
    h, l = df["high"], df["low"]
    up, dn = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = _atr(df, n)
    pdi = 100 * _wilder(pdm, n) / (a + EPS)
    ndi = 100 * _wilder(ndm, n) / (a + EPS)
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi + EPS)
    return _wilder(dx, n), (pdi - ndi) / (pdi + ndi + EPS)


def base_features(df):
    o, h, l, c, v = (df[k].astype(float) for k in ("open", "high", "low", "close", "volume"))
    f = pd.DataFrame(index=df.index)
    a = _atr(df, 14)
    f["atr"] = a
    f["atr_pct"] = a / c
    f["atr_rank"] = f["atr_pct"].rolling(200, min_periods=200).rank(pct=True)
    for k in (1, 3, 6, 12, 24, 48):
        f[f"r{k}"] = (c - c.shift(k)) / a
    f["rsi14"] = _rsi(c, 14)
    f["rsi2"] = _rsi(c, 2)
    f["rsi14_d"] = f["rsi14"] - f["rsi14"].shift(3)
    s20, s50, s200 = c.rolling(20).mean(), c.rolling(50).mean(), c.rolling(200).mean()
    f["d_sma20"] = (c - s20) / a
    f["d_sma50"] = (c - s50) / a
    f["d_sma200"] = (c - s200) / a
    f["slope20"] = (s20 - s20.shift(5)) / a
    f["slope50"] = (s50 - s50.shift(10)) / a
    f["slope200"] = (s200 - s200.shift(20)) / a
    sd20 = c.rolling(20).std(ddof=0)
    f["bb_pctb"] = (c - (s20 - 2 * sd20)) / (4 * sd20 + EPS)
    f["bb_width"] = 4 * sd20 / c
    f["bbw_rank"] = f["bb_width"].rolling(200, min_periods=200).rank(pct=True)
    f["adx"], f["di_diff"] = _adx(df, 14)
    for n in (20, 55):
        hi, lo = h.rolling(n).max(), l.rolling(n).min()
        f[f"don{n}"] = (c - lo) / (hi - lo + EPS)
    hi48, lo48 = h.rolling(48).max(), l.rolling(48).min()
    f["dist_hi48"] = (hi48 - c) / a
    f["dist_lo48"] = (c - lo48) / a
    rng = (h - l).replace(0, np.nan)
    f["body"] = ((c - o) / rng).fillna(0)
    f["upw"] = ((h - np.maximum(o, c)) / rng).fillna(0)
    f["loww"] = ((np.minimum(o, c) - l) / rng).fillna(0)
    f["rng_atr"] = (h - l) / a
    lv = np.log(v.clip(lower=EPS))
    f["vol_z"] = (lv - lv.rolling(50).mean()) / (lv.rolling(50).std(ddof=0) + 1e-9)
    f["vol_trend"] = v.rolling(5).mean() / (v.rolling(50).mean() + EPS)
    dc = c.diff().abs()
    f["er20"] = (c - c.shift(20)).abs() / (dc.rolling(20).sum() + EPS)
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    f["macd_h"] = (macd - macd.ewm(span=9, adjust=False).mean()) / a
    up = (c > c.shift(1)).astype(int)
    dn = (c < c.shift(1)).astype(int)
    f["streak"] = (up.groupby((up == 0).cumsum()).cumsum() - dn.groupby((dn == 0).cumsum()).cumsum()).clip(-8, 8)
    lr = np.log(c).diff()
    f["rv_ratio"] = lr.rolling(12).std() / (lr.rolling(72).std() + EPS)
    return f


_HTF_KEEP = ["rsi14", "d_sma20", "d_sma50", "slope20", "slope50", "r3", "r6", "adx", "di_diff", "don20", "er20", "atr_pct"]
_FLIP = {
    "r1": "neg", "r3": "neg", "r6": "neg", "r12": "neg", "r24": "neg", "r48": "neg",
    "rsi14": "inv100", "rsi2": "inv100", "rsi14_d": "neg",
    "d_sma20": "neg", "d_sma50": "neg", "d_sma200": "neg", "slope20": "neg", "slope50": "neg", "slope200": "neg",
    "bb_pctb": "inv1", "di_diff": "neg", "don20": "inv1", "don55": "inv1",
    "dist_hi48": ("swap", "dist_lo48"), "dist_lo48": ("swap", "dist_hi48"),
    "body": "neg", "upw": ("swap", "loww"), "loww": ("swap", "upw"), "macd_h": "neg", "streak": "neg",
}


def _epoch_s(ts):
    return pd.to_datetime(ts).astype("datetime64[s]").astype("int64").to_numpy()


def resample(df, rule):
    """UTC-aligned OHLCV resample, incomplete buckets dropped."""
    g = df.set_index("timestamp").resample(rule, label="left", closed="left")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    cnt = g["close"].count()
    out = out[cnt == cnt.max()].dropna().reset_index()
    return out


def coin_features(df4h):
    """Per-coin features on 4h candles (oldest first, closed only), incl. the
    daily trend (built from the same candles, only days that had closed)."""
    df = df4h.reset_index(drop=True)
    F = base_features(df)
    day = resample(df[["timestamp", "open", "high", "low", "close", "volume"]], "1D")
    hf = base_features(day)[_HTF_KEEP]
    hf.columns = ["h_" + k for k in _HTF_KEEP]
    left = pd.DataFrame({"ct": _epoch_s(df["timestamp"]) + TF_SEC})
    right = hf.copy()
    right["ct"] = _epoch_s(day["timestamp"]) + 86400
    m = pd.merge_asof(left.reset_index(), right.sort_values("ct"), on="ct", direction="backward").set_index("index")
    F = pd.concat([F, m.drop(columns=["ct"])], axis=1)
    c = df["close"].astype(float)
    a = F["atr"]
    for k in (42, 180):
        F[f"r{k}"] = (c - c.shift(k)) / a
    lr = np.log(c)
    vol = lr.diff().rolling(42).std()
    for k in (6, 24, 42, 180):
        F[f"z{k}"] = (lr - lr.shift(k)) / (vol * np.sqrt(k))
    F["timestamp"] = df["timestamp"].values
    for k in ("open", "high", "low"):
        F[k] = df[k].astype(float).values
    F["close"] = c.values
    return F


_ZC = ["z6", "z24", "z42", "z180"]


def market_block(frames):
    """frames: {symbol: coin_features(...)} for the universe. Returns a frame
    indexed by timestamp with the market-wide columns + each coin's z values."""
    parts = []
    for sym, F in frames.items():
        if F is None or not len(F):
            continue
        p = F[["timestamp"] + _ZC + ["d_sma200", "h_d_sma50"]].copy()
        p["sym"] = sym
        parts.append(p)
    if not parts:
        return None
    A = pd.concat(parts, ignore_index=True)
    A["above200"] = (A["d_sma200"] > 0).astype(float)
    A["h_up"] = (A["h_d_sma50"] > 0).astype(float)
    g = A.groupby("timestamp")
    M = pd.DataFrame(index=g.size().index)
    for zc in _ZC:
        M["m_" + zc] = g[zc].median()
        M["disp_" + zc] = g[zc].std(ddof=0)
    M["breadth200"] = g["above200"].mean()
    M["breadth_h50"] = g["h_up"].mean()
    Z = {zc: A.pivot_table(index="timestamp", columns="sym", values=zc, aggfunc="last") for zc in _ZC}
    return {"M": M, "Z": Z}


def btc_block(btc_frame):
    b = btc_frame[["timestamp", "r6", "r24", "d_sma50", "rsi14", "slope50", "atr_rank",
                   "h_d_sma50", "h_slope50", "h_rsi14", "h_r6"]].copy()
    b.columns = ["timestamp", "b_r6", "b_r24", "b_d_sma50", "b_rsi14", "b_slope50", "b_atr_rank",
                 "bh_d_sma50", "bh_slope50", "bh_rsi14", "bh_r6"]
    return b.set_index("timestamp")


def assemble(F, btc, mkt, sym=None):
    """Model input (all rows of one coin) in model feature order."""
    X = F.set_index("timestamp")
    X = X.join(btc, how="left")
    if mkt is not None:
        X = X.join(mkt["M"], how="left")
        for zc in _ZC:
            Zt = mkt["Z"][zc].reindex(X.index)
            own = X[zc].to_numpy()
            if sym in Zt.columns:
                others = Zt.drop(columns=[sym]).to_numpy()
                allv = Zt.to_numpy()
            else:          # coin outside the universe: compare with all universe coins
                others = Zt.to_numpy()
                allv = np.column_stack([others, own])
            with np.errstate(all="ignore"), warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                med = X["m_" + zc].to_numpy() if sym in Zt.columns else np.nanmedian(allv, axis=1)
                X["rel_" + zc] = own - med
                cnt = np.isfinite(others).sum(axis=1)
                X["rk_" + zc] = np.where(cnt > 0, (others < own[:, None]).sum(axis=1) / np.maximum(cnt, 1), np.nan)
    feats = model().features
    for col in feats:
        if col not in X.columns:
            X[col] = np.nan
    return X[feats]


def flip_for_short(X):
    G = X.copy()
    for col in X.columns:
        base, pre = col, ""
        for p in ("h_", "D_", "b_", "bh_"):
            if col.startswith(p) and col[len(p):] in _FLIP:
                pre, base = p, col[len(p):]
                break
        rule = _FLIP.get(base)
        if rule is None:
            if re.fullmatch(r"(r|z|rel_z|m_z)\d+", base):
                rule = "neg"
            elif re.fullmatch(r"rk_z\d+", base) or base.startswith("breadth"):
                rule = "inv1"
            else:
                continue
        if rule == "neg":
            G[col] = -X[col]
        elif rule == "inv1":
            G[col] = 1 - X[col]
        elif rule == "inv100":
            G[col] = 100 - X[col]
        else:
            G[col] = X[pre + rule[1]]
    return G


def score(X):
    """Raw + calibrated probabilities for LONG and SHORT on every row."""
    m = model()
    pl = m.raw_proba(X.to_numpy(dtype=float))
    ps = m.raw_proba(flip_for_short(X).to_numpy(dtype=float))
    return pl, ps, m.calibrate(pl), m.calibrate(ps)


def score_many(Xs):
    """score() for several coins in one pass: {sym: X} -> {sym: (pl, ps, cl, cs)}."""
    m = model()
    keys = [k for k, X in Xs.items() if X is not None and len(X)]
    if not keys:
        return {}
    blocks = [Xs[k].to_numpy(dtype=float) for k in keys] + [flip_for_short(Xs[k]).to_numpy(dtype=float) for k in keys]
    p = m.raw_proba(np.vstack(blocks))
    out, pos, n = {}, 0, len(keys)
    sizes = [len(Xs[k]) for k in keys]
    longs = []
    for sz in sizes:
        longs.append(p[pos:pos + sz])
        pos += sz
    for k, sz, pl in zip(keys, sizes, longs):
        ps = p[pos:pos + sz]
        pos += sz
        out[k] = (pl, ps, m.calibrate(pl), m.calibrate(ps))
    return out


def decide(pl, ps):
    """-> ('LONG'|'SHORT'|'WAIT', raw prob of the better side). Strong level only
    (the 24-hour 'standard' rule needs every coin, see replay_all)."""
    thr = model().threshold
    best = max(pl, ps)
    if not np.isfinite(best) or best < thr:
        return "WAIT", best
    return ("LONG" if pl >= ps else "SHORT"), best


def levels(side, entry, atr):
    """(stop, final target) for a signal at `entry`."""
    m = model()
    d = m.sl_atr * atr
    if side == "LONG":
        return entry - d, entry + m.targets_r[-1] * d
    if side == "SHORT":
        return entry + d, entry - m.targets_r[-1] * d
    return None, None


def targets(side, entry, stop):
    """Target prices (TP1, TP2, TP3) from entry and stop."""
    risk = abs(entry - stop)
    sgn = 1 if side == "LONG" else -1
    return [entry + sgn * k * risk for k in model().targets_r]


def cost_r(entry, stop):
    """Round-trip fees/slippage expressed in R."""
    risk = abs(entry - stop)
    return COST_ROUND_TRIP * entry / risk if risk > 0 else 0.0


def price_round(x):
    """Keeps enough significant digits for cheap coins (DOGE, SHIB...)."""
    if x is None or not np.isfinite(x) or x == 0:
        return x
    digits = max(2, 6 - int(math.floor(math.log10(abs(x)))))
    return round(float(x), min(digits, 12))


# ===========================================================================
# following a trade (same rules as the track record) + replay
# ===========================================================================
def follow(side, entry, sl, o, h, l, c, start, max_bars=None):
    """Walks candles start.. (arrays, closed candles only) with the trade plan.

    Pessimistic like the track record: a candle that opens beyond the stop
    exits at that open; a stop touched in a candle is taken before any
    target in that candle; stop moves (to entry after TP1, to TP1 after TP2)
    apply from the next candle; after `max_bars` the rest closes at the close.

    Returns dict(status, exit_price, bars, hits, r_gross, stop_now) where
    status is None while the trade is still running, else one of
    SL (stopped before TP1), TP1 / TP2 (stopped after that target), TP3
    (every target reached) or EXPIRED (time limit)."""
    m = model()
    max_bars = max_bars or m.max_bars
    tr, fr = m.targets_r, m.fractions
    risk = abs(entry - sl)
    sgn = 1 if side == "LONG" else -1
    n = len(c)
    stop_r, done, realized, hits = -1.0, 0.0, 0.0, 0

    def out(status, px, k):
        return {"status": status, "exit_price": float(px) if px is not None else None, "bars": k, "hits": hits,
                "r_gross": realized, "stop_now": entry + sgn * stop_r * risk}

    if risk <= 0:
        return out("EXPIRED", entry, 0)
    for k in range(max_bars):
        j = start + k
        if j >= n:
            return out(None, None, k)
        ro = sgn * (o[j] - entry) / risk
        rh = sgn * ((h[j] if sgn > 0 else l[j]) - entry) / risk
        rl = sgn * ((l[j] if sgn > 0 else h[j]) - entry) / risk
        if ro <= stop_r:                                   # gapped through the stop
            realized += (1 - done) * ro
            return out("SL" if hits == 0 else f"TP{hits}", o[j], k + 1)
        if rl <= stop_r:                                   # stop touched (taken first)
            realized += (1 - done) * stop_r
            return out("SL" if hits == 0 else f"TP{hits}", entry + sgn * stop_r * risk, k + 1)
        new_stop = stop_r
        while hits < len(tr) and rh >= tr[hits]:
            realized += fr[hits] * tr[hits]
            done += fr[hits]
            hits += 1
            if m.be_after and hits == m.be_after:
                new_stop = max(new_stop, 0.0)
            elif m.be_after and hits > m.be_after:
                new_stop = max(new_stop, tr[hits - 2])
        if hits == len(tr) or done >= 1 - 1e-9:
            return out(f"TP{hits}", entry + sgn * tr[hits - 1] * risk, k + 1)
        stop_r = new_stop
        if k + 1 == max_bars:
            realized += (1 - done) * sgn * (c[j] - entry) / risk
            return out("EXPIRED", c[j], k + 1)
    return out(None, None, max_bars)


def _trade(side, i, c, atr, cl, cs, ts, tier):
    m = model()
    entry = float(c[i])
    sl = entry - m.sl_atr * atr[i] if side == "LONG" else entry + m.sl_atr * atr[i]
    tps = targets(side, entry, sl)
    bar_time = pd.Timestamp(ts[i]).to_pydatetime()
    return {
        "side": side, "entry": entry, "stop_loss": float(sl), "take_profit": float(tps[-1]),
        "targets": [float(x) for x in tps], "targets_r": list(m.targets_r), "fractions": list(m.fractions),
        "atr": float(atr[i]), "confidence": round(float(cl[i] if side == "LONG" else cs[i]) * 100, 1), "tier": tier,
        "bar_time": bar_time, "signal_at": bar_time + timedelta(seconds=TF_SEC),
    }


def replay_all(S, lookback=REPLAY_BARS, start_time=None, closed=None):
    """Replays every coin together with the signal rules (strong + 24-hour
    standard signal, one trade at a time per coin).

    S: {symbol: dict(F=coin_features, pl, ps, cl, cs)} - closed 4h candles.
    Starts `lookback` candles before each coin's last candle, or at
    `start_time` if given. Returns ({symbol: active trade}, {symbol: last
    closed trade}); `closed` (optional dict) receives every closed trade per
    symbol, oldest first."""
    m = model()
    gap = np.timedelta64(int(m.gap_hours * 3600), "s") if m.gap_hours > 0 else None
    data = {}
    for sym, d in S.items():
        F = d["F"]
        ts = pd.to_datetime(F["timestamp"]).to_numpy()
        n = len(ts)
        if start_time is not None:
            i0 = max(WARMUP_BARS, int(np.searchsorted(ts, np.datetime64(pd.Timestamp(start_time)))))
        else:
            i0 = max(WARMUP_BARS, n - lookback)
        if i0 >= n:
            continue
        data[sym] = {"ts": ts, "i0": i0, "pos": {t: i for i, t in enumerate(ts[i0:], start=i0)},
                     "o": F["open"].to_numpy(float), "h": F["high"].to_numpy(float), "l": F["low"].to_numpy(float),
                     "c": F["close"].to_numpy(float), "atr": F["atr"].to_numpy(float),
                     "pl": np.asarray(d["pl"], float), "ps": np.asarray(d["ps"], float),
                     "cl": np.asarray(d["cl"], float), "cs": np.asarray(d["cs"], float)}
    times = sorted({t for d in data.values() for t in d["ts"][d["i0"]:]})
    busy = {s: -1 for s in data}
    active, last = {}, {}
    last_signal = None
    for t in times:
        cands = []
        for sym, d in data.items():
            i = d["pos"].get(t)
            if i is None or i <= busy[sym]:
                continue
            a, b = d["pl"][i], d["ps"][i]
            if not (np.isfinite(a) and np.isfinite(b) and np.isfinite(d["atr"][i]) and d["atr"][i] > 0):
                continue
            cands.append((max(a, b), sym, i, "LONG" if a >= b else "SHORT"))
        if not cands:
            continue
        cands.sort(key=lambda x: (-x[0], x[1]))
        picks = [(x, "strong") for x in cands if x[0] >= m.threshold]
        if not picks and gap is not None and (last_signal is None or t - last_signal >= gap) and cands[0][0] >= m.floor:
            picks = [(cands[0], "standard")]
        for (p, sym, i, side), tier in picks:
            d = data[sym]
            tr = _trade(side, i, d["c"], d["atr"], d["cl"], d["cs"], d["ts"], tier)
            res = follow(side, tr["entry"], tr["stop_loss"], d["o"], d["h"], d["l"], d["c"], i + 1)
            tr["bars_held"] = int(res["bars"])
            tr["hits"] = int(res["hits"])
            last_signal = t
            if res["status"] is None:
                tr["stop_now"] = float(res["stop_now"])
                tr["realized_r"] = round(float(res["r_gross"]), 4)
                tr["expires_at"] = tr["signal_at"] + timedelta(seconds=TF_SEC * m.max_bars)
                active[sym] = tr
                busy[sym] = 10 ** 12
                continue
            r = float(res["r_gross"]) - cost_r(tr["entry"], tr["stop_loss"])
            j = min(i + res["bars"], len(d["ts"]) - 1)
            tr.update({"status": res["status"], "exit_price": res["exit_price"], "r": round(r, 3),
                       "closed_at": pd.Timestamp(d["ts"][j]).to_pydatetime() + timedelta(seconds=TF_SEC)})
            last[sym] = tr
            if closed is not None:
                closed.setdefault(sym, []).append(tr)
            busy[sym] = i + res["bars"]
    return active, last


# ===========================================================================
# live engine (fetches candles, caches per closed 4h candle)
# ===========================================================================
def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def closed_only(df, now=None):
    now = pd.Timestamp(now or _utcnow())
    ts = pd.to_datetime(df["timestamp"])
    return df[ts + pd.Timedelta(seconds=TF_SEC) <= now].reset_index(drop=True)


def last_closed_bar(now=None):
    now = now or _utcnow()
    epoch = int((now - datetime(1970, 1, 1)).total_seconds())
    return datetime(1970, 1, 1) + timedelta(seconds=(epoch // TF_SEC - 1) * TF_SEC)


class LiveEngine:
    def __init__(self, get_candles):
        self.get_candles = get_candles
        self._lock = threading.Lock()
        self._frames = {}     # symbol -> (bar_time, frame)
        self._market = None   # (bar_time, mkt, btc)
        self._board = None    # (bar_time, {symbol: result}) for the signal coins
        self._signals = {}    # symbol -> (bar_time, result) for coins without signals

    def fetch(self, symbol, bars=HISTORY_BARS, now=None):
        now = now or _utcnow()
        start = now - timedelta(seconds=TF_SEC * (bars + 2))
        cursor = int((start - datetime(1970, 1, 1)).total_seconds() * 1000)
        frames, last_seen = [], None
        for _ in range(10):
            df = self.get_candles(symbol=symbol, timeframe=TIMEFRAME, limit=300, since=cursor)
            if df is None or not len(df):
                break
            frames.append(df)
            last_ms = int(pd.Timestamp(df["timestamp"].iloc[-1]).value // 1_000_000)
            if last_seen is not None and last_ms <= last_seen:
                break
            last_seen = last_ms
            cursor = last_ms + TF_SEC * 1000
            if cursor > (now - datetime(1970, 1, 1)).total_seconds() * 1000:
                break
            time.sleep(0.12)
        if not frames:
            return None
        df = pd.concat(frames).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return closed_only(df, now)

    def frame(self, symbol, bar, now=None, strict=True):
        hit = self._frames.get(symbol)
        if hit and hit[0] == bar:
            return hit[1]
        df = self.fetch(symbol, now=now)
        if strict and df is not None and len(df) and pd.Timestamp(df["timestamp"].iloc[-1]) < pd.Timestamp(bar):
            time.sleep(2)                       # the exchange has not published the new candle yet
            df = self.fetch(symbol, now=now)
        if df is None or len(df) < 60:
            raise ValueError(f"Not enough 4h history for {symbol}.")
        if strict and pd.Timestamp(df["timestamp"].iloc[-1]) < pd.Timestamp(bar):
            raise ValueError(f"The latest 4h candle for {symbol} is not available yet.")
        F = coin_features(df)
        self._frames[symbol] = (bar, F)
        return F

    def market(self, bar, now=None):
        if self._market and self._market[0] == bar:
            return self._market[1], self._market[2]
        frames = {}
        for sym in UNIVERSE:
            try:
                frames[sym] = self.frame(sym, bar, now)
            except Exception:
                frames[sym] = None
        mkt = market_block(frames)
        if frames.get("BTC/USDT") is None:
            raise ValueError("Bitcoin data is not available right now.")
        btc = btc_block(frames["BTC/USDT"])
        self._market = (bar, mkt, btc)
        return mkt, btc

    def board(self, now=None):
        """{symbol: result} for every signal coin at the last closed candle
        (the signal rules look at all of them together)."""
        bar = last_closed_bar(now)
        hit = self._board
        if hit and hit[0] == bar:
            return hit[1]
        with self._lock:
            hit = self._board
            if hit and hit[0] == bar:
                return hit[1]
            mkt, btc = self.market(bar, now)
            feats, Xs = {}, {}
            for sym in signal_coins():
                try:
                    F = self.frame(sym, bar, now)
                except Exception:
                    continue
                if len(F) <= WARMUP_BARS:
                    continue
                feats[sym] = F
                Xs[sym] = assemble(F, btc, mkt, sym=sym)
            scores = score_many(Xs)
            S = {sym: {"F": feats[sym], "pl": scores[sym][0], "ps": scores[sym][1], "cl": scores[sym][2],
                       "cs": scores[sym][3]} for sym in scores}
            closed = {}
            active, last = replay_all(S, closed=closed)
            out = {sym: _result(S[sym], active.get(sym), last.get(sym), closed.get(sym, []), covered=True)
                   for sym in S}
            self._board = (bar, out)
            return out

    def signal(self, symbol, now=None):
        if symbol in signal_coins():
            res = self.board(now).get(symbol)
            if res is None:
                raise ValueError(f"Not enough 4h history for {symbol}.")
            return res
        bar = last_closed_bar(now)
        hit = self._signals.get(symbol)
        if hit and hit[0] == bar:
            return hit[1]
        with self._lock:
            hit = self._signals.get(symbol)
            if hit and hit[0] == bar:
                return hit[1]
            mkt, btc = self.market(bar, now)
            F = self.frame(symbol, bar, now)
            pl, ps, cl, cs = score(assemble(F, btc, mkt, sym=symbol))
            res = _result({"F": F, "pl": pl, "ps": ps, "cl": cl, "cs": cs}, None, None, [], covered=False)
            self._signals[symbol] = (bar, res)
            return res


def _result(d, active, last, closed, covered=True):
    """The JSON-ready view of one coin at its last closed candle."""
    m = model()
    F = d["F"]
    row = F.iloc[-1]
    bar_time = pd.Timestamp(row["timestamp"]).to_pydatetime()
    p_long, p_short = float(d["cl"][-1]), float(d["cs"][-1])
    best_raw = float(max(d["pl"][-1], d["ps"][-1]))
    a = active
    tps = a["targets"] if a else None
    return {
        "engine_version": ENGINE_VERSION,
        "timeframe": TIMEFRAME,
        # coins outside the trained/tested list get the market brief and the lean, never a trade
        "covered": bool(covered),
        # verdict = the trade the engine is in right now (one at a time per coin), else WAIT
        "verdict": a["side"] if a else "WAIT",
        "fresh": bool(a and a["bar_time"] == bar_time),
        "confidence": a["confidence"] if a else round(max(p_long, p_short) * 100, 1),
        "tier": a["tier"] if a else None,
        "p_long": round(p_long * 100, 1),
        "p_short": round(p_short * 100, 1),
        "bias": "LONG" if p_long >= p_short else "SHORT",
        # how close the current candle came to a strong signal (100 = strong level)
        "strength": round(min(best_raw / m.threshold, 1.5) * 100, 1) if np.isfinite(best_raw) and m.threshold > 0 else None,
        # close to a strong signal but not in a trade -> watch only
        "setup_forming": bool(covered and not a and np.isfinite(best_raw) and best_raw >= m.watch_threshold),
        "threshold": m.threshold,
        "entry": a["entry"] if a else None,
        "stop_loss": a["stop_loss"] if a else None,          # the stop when the signal started
        "stop_now": a["stop_now"] if a else None,            # after TP1 it sits at the entry, after TP2 at TP1
        "take_profit": a["take_profit"] if a else None,      # the last target (TP3)
        "targets": tps,
        "targets_r": list(m.targets_r),
        "fractions": list(m.fractions),
        "hits": a["hits"] if a else None,
        "atr": a["atr"] if a else float(row["atr"]),
        "sl_pct": round(m.sl_atr * a["atr"] / a["entry"] * 100, 3) if a else None,
        "tp_pct": round(abs(tps[-1] - a["entry"]) / a["entry"] * 100, 3) if a else None,
        "tp_pcts": [round(abs(x - a["entry"]) / a["entry"] * 100, 3) for x in tps] if a else None,
        "max_bars": m.max_bars,
        "active": a,
        "last_closed": last,
        # every signal the same rules gave on this coin in the replay window (~50 days), with its result
        "recent": [{k: t.get(k) for k in RECENT_KEYS} for t in closed[-RECENT_MAX:]],
        "last_close": float(row["close"]),
        "brief": market_brief(F),
        "history_bars": int(len(F)),
        "bar_time": bar_time,
        "next_update": bar_time + timedelta(seconds=2 * TF_SEC),
    }


def progress(active, price):
    """Where a running trade stands at the live price.

    r_now: result in R so far (unrealised, whole position); pct: share of the
    way from the last level reached to the next target (positive), or toward
    the current stop (negative); next: which target is next (1-3)."""
    if not active or price is None or not np.isfinite(price):
        return None
    sgn = 1 if active["side"] == "LONG" else -1
    entry = float(active["entry"])
    risk = abs(entry - float(active["stop_loss"]))
    if risk <= 0:
        return None
    tps = active.get("targets") or [float(active["take_profit"])]
    hits = int(active.get("hits") or 0)
    stop_now = float(active.get("stop_now") if active.get("stop_now") is not None else active["stop_loss"])
    move = sgn * (float(price) - entry)
    nxt = min(hits, len(tps) - 1)
    lo = 0.0 if hits == 0 else sgn * (tps[hits - 1] - entry)
    hi = sgn * (tps[nxt] - entry)
    stop_d = sgn * (entry - stop_now)          # >0 below entry, 0 at entry, <0 above (locked profit)
    if move >= lo:
        pct = (move - lo) / (hi - lo) * 100 if hi > lo else 100.0
    else:
        room = lo + stop_d                      # distance from the last level down to the stop
        pct = -(lo - move) / room * 100 if room > 0 else -100.0
    state = "running"
    if move >= hi and hits < len(tps):
        state = "target_touched"
    elif sgn * (float(price) - stop_now) <= 0:
        state = "stop_touched"
    return {"price": float(price), "r_now": round(move / risk, 3), "pct": round(max(-100.0, min(100.0, pct)), 1),
            "move_pct": round(sgn * (float(price) / entry - 1) * 100, 2), "state": state, "next": nxt + 1,
            "hits": hits}


def brief_for(engine, symbol, now=None):
    """Market brief for any symbol (forex too: markets close at weekends, so the
    latest candle may be older than the last 4h slot - that is fine here)."""
    bar = last_closed_bar(now)
    key = ("brief", symbol)
    hit = engine._frames.get(key)
    if hit and hit[0] == bar:
        return hit[1]
    df = engine.fetch(symbol, now=now)
    if df is None or len(df) < 60:
        raise ValueError(f"Not enough 4h history for {symbol}.")
    out = market_brief(coin_features(df))
    engine._frames[key] = (bar, out)
    return out


def market_brief(F):
    """Plain-language market context from a coin_features() frame (last closed 4h candle).
    Works for any symbol with 4h candles (crypto or forex). Not a signal."""
    if F is None or len(F) < 60:
        return None
    r = F.iloc[-1]

    def num(k):
        v = r.get(k)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if np.isfinite(v) else None

    c = num("close")
    d50, d200, hd50 = num("d_sma50"), num("d_sma200"), num("h_d_sma50")
    if d50 is not None and d200 is not None and d50 > 0 and d200 > 0:
        trend = "up"
    elif d50 is not None and d200 is not None and d50 < 0 and d200 < 0:
        trend = "down"
    else:
        trend = "sideways"
    rsi = num("rsi14")
    momentum = None if rsi is None else ("overbought" if rsi >= 70 else "oversold" if rsi <= 30 else
                                         "strong" if rsi >= 55 else "weak" if rsi <= 45 else "neutral")
    rank = num("atr_rank")
    vol = None if rank is None else ("high" if rank >= 0.8 else "calm" if rank <= 0.2 else "normal")
    tail = F.tail(20)
    hi, lo = float(tail["high"].max()), float(tail["low"].min())
    prev = F["close"].iloc[-7] if len(F) > 7 else None
    return {
        "price": c, "trend": trend, "daily_trend": None if hd50 is None else ("up" if hd50 > 0 else "down"),
        "rsi": None if rsi is None else round(rsi, 1), "momentum": momentum,
        "atr_pct": None if num("atr_pct") is None else round(num("atr_pct") * 100, 2), "volatility": vol,
        "range_high": hi, "range_low": lo,
        "to_high_pct": round((hi / c - 1) * 100, 2) if c else None,
        "to_low_pct": round((lo / c - 1) * 100, 2) if c else None,
        "change_24h_pct": round((c / float(prev) - 1) * 100, 2) if c and prev else None,
        "bar_time": pd.Timestamp(r["timestamp"]).to_pydatetime(),
    }


def test_stats():
    return model().meta.get("test", {})


def walk_forward_stats():
    return model().meta.get("walk_forward", {})
