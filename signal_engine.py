"""
Signals FM - signal engine v2.

What it does
  * Works on CLOSED 4-hour candles (a signal never changes while a candle is
    still forming).
  * Builds 82 scale-free features per coin: price action, momentum, trend,
    volatility, volume, the daily trend, Bitcoin's trend, and the state of
    the whole market (16 large coins: breadth, average momentum, and how
    strong this coin is relative to the others).
  * A gradient-boosted model (trained on Jan 2022 - Jun 2025, exported to
    signal_model.json) estimates, for LONG and for SHORT, the probability
    that the target is reached before the stop. A calibration curve turns
    that into an honest win probability.
  * A signal is shown only when the model is in its top ~3% of conviction;
    everything else is WAIT.
  * Levels: stop = 3 x ATR(14), target = 1.5 x ATR(14), time limit 48
    candles (8 days). Fees are counted as 0.12% round trip.

Measured on data the model never saw (Jul 2025 - Oct 2026, 16 coins, one
trade at a time per coin): see MODEL["test"] / signal_model.json.

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

ENGINE_VERSION = "2.0"
TIMEFRAME = "4h"
TF_SEC = 4 * 3600
HISTORY_BARS = 600            # 4h candles fetched per coin (100 days; features need ~230)
UNIVERSE = ["ADA/USDT", "ATOM/USDT", "AVAX/USDT", "BCH/USDT", "BNB/USDT", "BTC/USDT", "DOGE/USDT", "DOT/USDT",
            "ETH/USDT", "LINK/USDT", "LTC/USDT", "NEAR/USDT", "SOL/USDT", "TRX/USDT", "XLM/USDT", "XRP/USDT"]
COST_ROUND_TRIP = 0.0012      # 0.05% taker in + 0.05% out + 0.02% slippage
EPS = 1e-12
_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_model.json")


# ===========================================================================
# model (exported gradient-boosted trees + calibration)
# ===========================================================================
class _Model:
    def __init__(self, path=_MODEL_PATH):
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        self.meta = m
        self.features = m["features"]
        self.baseline = float(m["baseline"])
        self.trees = [{k: np.asarray(v) for k, v in t.items()} for t in m["trees"]]
        self.cal_x = np.asarray(m["calibration"]["x"], dtype=float)
        self.cal_y = np.asarray(m["calibration"]["y"], dtype=float)
        self.threshold = float(m["threshold"])
        self.watch_threshold = float(m.get("watch_threshold", m["threshold"]))
        self.sl_atr = float(m["levels"]["sl_atr"])
        self.tp_atr = float(m["levels"]["tp_atr"])
        self.max_bars = int(m["levels"]["max_bars"])

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


def decide(pl, ps):
    """-> ('LONG'|'SHORT'|'WAIT', raw prob of the better side)."""
    thr = model().threshold
    best = max(pl, ps)
    if not np.isfinite(best) or best < thr:
        return "WAIT", best
    return ("LONG" if pl >= ps else "SHORT"), best


def levels(side, entry, atr):
    m = model()
    d = m.sl_atr * atr
    if side == "LONG":
        return entry - d, entry + m.tp_atr * atr
    if side == "SHORT":
        return entry + d, entry - m.tp_atr * atr
    return None, None


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
def follow(side, entry, sl, tp, o, h, l, c, start, max_bars=None):
    """Walks candles start.. (arrays, closed candles only). Same pessimistic
    rules as trackrecord.evaluate(): gap past the stop -> exit at the open,
    stop and target in one candle -> stop, time limit -> close.
    Returns (status or None if still open, exit_price, bars_held)."""
    max_bars = max_bars or model().max_bars
    n = len(c)
    for k in range(max_bars):
        j = start + k
        if j >= n:
            return None, None, k
        if side == "LONG":
            if o[j] <= sl:
                return "SL", float(o[j]), k + 1
            if l[j] <= sl:
                return "SL", float(sl), k + 1
            if h[j] >= tp:
                return "TP", float(tp), k + 1
        else:
            if o[j] >= sl:
                return "SL", float(o[j]), k + 1
            if h[j] >= sl:
                return "SL", float(sl), k + 1
            if l[j] <= tp:
                return "TP", float(tp), k + 1
        if k + 1 == max_bars:
            return "EXPIRED", float(c[j]), k + 1
    return None, None, max_bars


def replay(F, pl, ps, cl, cs, lookback=300):
    """One trade at a time per coin, replayed over the last `lookback`
    candles. Returns (active trade or None, last closed trade or None)."""
    o, h, l, c = (F[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    atr = F["atr"].to_numpy(float)
    ts = pd.to_datetime(F["timestamp"]).to_list()
    n = len(F)
    i = max(240, n - lookback)
    active = last = None
    while i < n:
        side, _ = decide(pl[i], ps[i])
        if side == "WAIT" or not np.isfinite(atr[i]):
            i += 1
            continue
        sl, tp = levels(side, c[i], atr[i])
        status, exit_px, held = follow(side, c[i], sl, tp, o, h, l, c, i + 1)
        trade = {
            "side": side, "entry": float(c[i]), "stop_loss": float(sl), "take_profit": float(tp),
            "atr": float(atr[i]), "confidence": round(float(cl[i] if side == "LONG" else cs[i]) * 100, 1),
            "bar_time": ts[i].to_pydatetime(), "signal_at": ts[i].to_pydatetime() + timedelta(seconds=TF_SEC),
            "bars_held": int(held),
        }
        if status is None:
            trade["expires_at"] = trade["signal_at"] + timedelta(seconds=TF_SEC * model().max_bars)
            active = trade
            break
        risk = abs(trade["entry"] - sl)
        r = ((exit_px - trade["entry"]) if side == "LONG" else (trade["entry"] - exit_px)) / risk
        trade.update({"status": status, "exit_price": exit_px, "r": round(r - cost_r(trade["entry"], sl), 3),
                      "closed_at": ts[min(i + held, n - 1)].to_pydatetime() + timedelta(seconds=TF_SEC)})
        last = trade
        i += held
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
        self._signals = {}    # symbol -> (bar_time, result)

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

    def signal(self, symbol, now=None):
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
            X = assemble(F, btc, mkt, sym=symbol)
            pl, ps, cl, cs = score(X)
            active, last = replay(F, pl, ps, cl, cs)
            m = model()
            row = F.iloc[-1]
            bar_time = pd.Timestamp(row["timestamp"]).to_pydatetime()
            p_long, p_short = float(cl[-1]), float(cs[-1])
            best_raw = float(max(pl[-1], ps[-1]))
            res = {
                "engine_version": ENGINE_VERSION,
                "timeframe": TIMEFRAME,
                # verdict = the trade the engine is in right now (one at a time per coin), else WAIT
                "verdict": active["side"] if active else "WAIT",
                "fresh": bool(active and active["bar_time"] == bar_time),
                "confidence": active["confidence"] if active else round(max(p_long, p_short) * 100, 1),
                "p_long": round(p_long * 100, 1),
                "p_short": round(p_short * 100, 1),
                "bias": "LONG" if p_long >= p_short else "SHORT",
                # how close the current candle came to a signal (100 = signal threshold)
                "strength": round(min(best_raw / m.threshold, 1.5) * 100, 1) if np.isfinite(best_raw) else None,
                # close to a signal, but in a band that lost money when tested as signals -> watch only, never traded
                "setup_forming": bool(not active and np.isfinite(best_raw) and best_raw >= m.watch_threshold),
                "threshold": m.threshold,
                "entry": active["entry"] if active else None,
                "stop_loss": active["stop_loss"] if active else None,
                "take_profit": active["take_profit"] if active else None,
                "atr": active["atr"] if active else float(row["atr"]),
                "sl_pct": round(m.sl_atr * active["atr"] / active["entry"] * 100, 3) if active else None,
                "tp_pct": round(m.tp_atr * active["atr"] / active["entry"] * 100, 3) if active else None,
                "max_bars": m.max_bars,
                "active": active,
                "last_closed": last,
                "last_close": float(row["close"]),
                "brief": market_brief(F),
                "history_bars": int(len(F)),
                "bar_time": bar_time,
                "next_update": bar_time + timedelta(seconds=2 * TF_SEC),
            }
            self._signals[symbol] = (bar, res)
            return res


def progress(active, price):
    """Where a running trade stands at the live price: R so far and how far
    along the way to the target (positive) or the stop (negative), in %."""
    if not active or price is None or not np.isfinite(price):
        return None
    sgn = 1 if active["side"] == "LONG" else -1
    risk = abs(active["entry"] - active["stop_loss"])
    reward = abs(active["take_profit"] - active["entry"])
    move = sgn * (float(price) - active["entry"])
    pct = move / reward * 100 if move >= 0 else move / risk * 100
    state = "running"
    if move >= reward:
        state = "target_touched"
    elif -move >= risk:
        state = "stop_touched"
    return {"price": float(price), "r_now": round(move / risk, 3), "pct": round(max(-100.0, min(100.0, pct)), 1),
            "move_pct": round(sgn * (float(price) / active["entry"] - 1) * 100, 2), "state": state}


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
