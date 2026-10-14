"""
Signal streams for SignalX: 15m, 1h and 4h, for crypto and forex.

Six independent streams (market x timeframe). Each has its own model or
rules, stop/target distances, expected holding time and expiry, and a signal
on one stream never touches another (a 15m and a 4h signal on the same symbol
can run at the same time).

Engines
-------
* crypto_4h: signal engine v3.1 (signal_engine.py, unchanged).
* every other stream: a trained model in models/<market>_<timeframe>.json
  (features.py builds its inputs; research/lab.py trains it and writes the
  walk-forward and holdout tests into the same file). The model scores the
  chance that a trade reaches its first target before its stop; the stream
  gives a signal when that score is above the stream's threshold (chosen on
  the validation years only, then checked on later data the model never saw).
* rules engine R1 (below) is only used for a stream that has no model file.

Trade plan (model streams): stop = sl_atr x ATR from the entry (the close of
the signal candle), targets at the model file's targets_r (in R = stop
distance), a part closed at each; after TP1 the stop moves to the entry,
after TP2 to TP1; the rest closes after max_bars candles. Forex trades are
closed before the weekend and no new forex signal starts close to the weekly
close or outside the stream's sessions. One trade at a time per symbol and
stream, a cooldown after each trade, at most max_per_bar new signals per
candle (best score first), and the news blackout (news.py) when it is on.

Rules engine R1 (long side; short is the mirror image)
------------------------------------------------------
One qualified setup on the closed candle (trend continuation, pullback to
EMA20, breakout of the 20-candle range, reversal at the 50-candle support)
AND the higher-timeframe trend agrees, a confluence score >= 60 (EMA trend,
higher-timeframe trend, MACD, RSI, ADX/DI, volume, room to the next level),
reward:risk >= 1.5 to the next level, an ATR stop behind the 5-candle swing.
"""
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import markets

RULES_VERSION = "R1"
MODEL_VERSION = "4.0"
EPS = 1e-12
MIN_SCORE = 60
GRADE_A, GRADE_B = 80, 70
WARMUP = 210              # candles needed before the first signal (EMA200)
REPLAY_BARS = 300         # candles replayed to find each symbol's running trade
RECENT_MAX = 30
MODELS_DIR = os.environ.get("MODELS_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

HTF_OF = {"15m": "1h", "1h": "4h", "4h": "1d"}

# per stream: market, timeframe, holding text, forex time rules, and the R1 fallback plan
STREAMS = {
    "crypto_15m": {"market": "crypto", "tf": "15m", "max_bars": 32, "cooldown": 4, "hold": "1-8 hours"},
    "crypto_1h": {"market": "crypto", "tf": "1h", "max_bars": 36, "cooldown": 3, "hold": "4 hours to 1.5 days"},
    "crypto_4h": {"market": "crypto", "tf": "4h", "max_bars": 54, "cooldown": 0, "hold": "1-9 days"},
    "forex_15m": {"market": "forex", "tf": "15m", "max_bars": 32, "cooldown": 4, "hold": "1-8 hours",
                  "sessions": ("London", "New York"), "no_new_before_close_h": 2},
    "forex_1h": {"market": "forex", "tf": "1h", "max_bars": 36, "cooldown": 3, "hold": "4 hours to 1.5 days",
                 "sessions": ("London", "New York"), "no_new_before_close_h": 4},
    "forex_4h": {"market": "forex", "tf": "4h", "max_bars": 30, "cooldown": 2, "hold": "1-5 days",
                 "sessions": None, "no_new_before_close_h": 12},
}
TARGETS_R = [1.0, 2.0, 3.0]
FRACTIONS = [1 / 3, 1 / 3, 1 / 3]
BE_AFTER = 1
MAX_PER_BAR = 5
SL_ATR_MIN, SL_ATR_MAX = 1.0, 2.5
MIN_RR = 1.5


def stream_key(market, tf):
    return f"{market}_{tf}"


def stream_cfg(market, tf):
    return STREAMS.get(stream_key(market, tf))


# ===========================================================================
# stream models (models/<key>.json)
# ===========================================================================
_models = {}
_models_lock = threading.Lock()


def _model_path(key):
    return os.path.join(MODELS_DIR, f"{key}.json")


def _tree_proba(trees, baseline, X):
    """Gradient-boosted trees (sklearn HistGradientBoosting export) -> probability. NaN = missing."""
    X = np.asarray(X, dtype=float)
    out = np.full(len(X), baseline)
    rows = np.arange(len(X))
    for t in trees:
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


class GateModel:
    """Higher-timeframe model that must agree with a signal (multi-timeframe alignment):
    its score for the signal's side must be the higher of the two and at least `level`."""

    def __init__(self, g):
        self.tf = g["timeframe"]
        self.features = g["features"]
        self.baseline = float(g["baseline"])
        self.trees = [{k: np.asarray(v) for k, v in t.items()} for t in g["trees"]]
        spec = g.get("spec") or {}
        self.session = bool(spec.get("session", False))
        self.volume = bool(spec.get("volume", True))
        self.anchor = spec.get("anchor")
        self.level = float(g.get("level", 0.0))
        self.label = g.get("label") or f"{self.tf} model agrees"

    def raw_proba(self, X):
        return _tree_proba(self.trees, self.baseline, X)


class StreamModel:
    """A trained stream model: gradient-boosted trees (same format as
    signal_model.json), calibration, threshold, trade plan and its tests."""

    def __init__(self, path):
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        self.meta = m
        self.key = m["stream"]
        self.market, self.tf = m["market"], m["timeframe"]
        self.version = str(m.get("version", MODEL_VERSION))
        self.features = m["features"]
        self.baseline = float(m["baseline"])
        self.trees = [{k: np.asarray(v) for k, v in t.items()} for t in m["trees"]]
        self.cal_x = np.asarray(m["calibration"]["x"], dtype=float)
        self.cal_y = np.asarray(m["calibration"]["y"], dtype=float)
        self.threshold = float(m["threshold"])
        self.grade_a = float(m.get("grade_a", self.threshold))
        self.grade_b = float(m.get("grade_b", self.threshold))
        self.watch = float(m.get("watch_threshold", self.threshold))
        spec = m.get("spec") or {}
        self.session = bool(spec.get("session", True))
        self.volume = bool(spec.get("volume", self.market == "crypto"))
        self.anchor = spec.get("anchor")
        lv = m["levels"]
        self.sl_atr = float(lv["sl_atr"])
        self.targets_r = [float(x) for x in lv["targets_r"]]
        self.fractions = [float(x) for x in lv["fractions"]]
        self.be_after = int(lv.get("be_after", 1) or 0)
        self.max_bars = int(lv["max_bars"])
        sel = m.get("selection") or {}
        self.max_per_bar = int(sel.get("max_per_bar", MAX_PER_BAR) or 0)
        self.cooldown = int(sel.get("cooldown_bars", 0) or 0)
        self.sessions = tuple(sel["sessions"]) if sel.get("sessions") else None
        self.no_new_before_close_h = float(sel.get("no_new_before_close_h", 0) or 0)
        self.hold_bars = float(m.get("hold_bars_median") or max(1, self.max_bars // 4))
        self.signal_symbols = list(m.get("signal_symbols") or [])
        self.gate = GateModel(m["gate"]) if m.get("gate") else None

    def raw_proba(self, X):
        return _tree_proba(self.trees, self.baseline, X)

    def calibrate(self, p):
        return np.interp(p, self.cal_x, self.cal_y)

    def grade(self, raw):
        if raw >= self.grade_a:
            return "A"
        if raw >= self.grade_b:
            return "B"
        return "C"


def load_model(key):
    """The stream's model, or None when the stream has no model file."""
    with _models_lock:
        if key in _models:
            return _models[key]
        path = _model_path(key)
        m = None
        if os.path.exists(path):
            try:
                m = StreamModel(path)
            except Exception as e:  # a broken file must not stop the site
                print(f"[streams] could not load {path}: {e}")
                m = None
        _models[key] = m
        return m


def reset_models():
    with _models_lock:
        _models.clear()


# A stream goes live by default only when its model passed the test on data it never saw (the holdout):
# win rate (TP1 or better) of at least PASS_WIN_RATE and a positive average result after costs.
PASS_WIN_RATE = 70.0
PASS_MIN_SIGNALS = 50
_research = {"t": 0.0, "data": None}


def passed_test(test):
    if not test:
        return False
    return ((test.get("signals") or 0) >= PASS_MIN_SIGNALS and (test.get("win_rate") or 0) >= PASS_WIN_RATE
            and (test.get("avg_net_r") or 0) > 0)


def validated(key):
    """crypto 4h (signal engine v3.1, tested separately) or a stream model whose holdout test passed."""
    if key == "crypto_4h":
        return True
    m = load_model(key)
    return m is not None and passed_test(m.meta.get("test"))


def research_summary():
    """models/research.json: every model variant that was tested per stream (validation + holdout), including
    the ones that failed - shown on the Signals page so a switched-off stream says why."""
    now = time.time()
    if _research["data"] is not None and now - _research["t"] < 600:
        return _research["data"]
    data = {}
    try:
        with open(os.path.join(MODELS_DIR, "research.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    _research.update(t=now, data=data)
    return data


def engine_name(key):
    if key == "crypto_4h":
        return "ml_v3"
    return "model" if load_model(key) is not None else "rules"


def plan_for(market, tf):
    """The trade plan stored with every signal (the tracker follows it)."""
    key = stream_key(market, tf)
    if key == "crypto_4h":
        import signal_engine as se
        m = se.model()
        return {"targets_r": list(m.targets_r), "fractions": list(m.fractions), "be_after": m.be_after,
                "max_bars": m.max_bars, "tf_sec": se.TF_SEC, "engine": "v" + se.ENGINE_VERSION}
    m = load_model(key)
    if m is not None:
        return {"targets_r": list(m.targets_r), "fractions": list(m.fractions), "be_after": m.be_after,
                "max_bars": m.max_bars, "tf_sec": markets.TF_SEC[tf], "engine": "v" + m.version,
                "sl_atr": m.sl_atr}
    cfg = stream_cfg(market, tf)
    return {"targets_r": list(TARGETS_R), "fractions": list(FRACTIONS), "be_after": BE_AFTER,
            "max_bars": cfg["max_bars"], "tf_sec": markets.TF_SEC[tf], "engine": RULES_VERSION}


def policy_for(market, tf):
    """Selection rules of a stream (who may get a new signal, and how many)."""
    key = stream_key(market, tf)
    cfg = STREAMS[key]
    m = load_model(key) if key != "crypto_4h" else None
    if m is not None:
        return {"max_per_bar": m.max_per_bar, "cooldown": m.cooldown, "sessions": m.sessions,
                "no_new_before_close_h": m.no_new_before_close_h}
    return {"max_per_bar": MAX_PER_BAR, "cooldown": int(cfg.get("cooldown") or 0), "sessions": cfg.get("sessions"),
            "no_new_before_close_h": float(cfg.get("no_new_before_close_h") or 0)}


def hold_text(key):
    m = load_model(key) if key != "crypto_4h" else None
    if m is not None and m.meta.get("hold_text"):
        return m.meta["hold_text"]
    return STREAMS[key]["hold"]


def grade_of(score):
    if score is None:
        return None
    if score >= GRADE_A:
        return "A"
    if score >= GRADE_B:
        return "B"
    return "C"


def grade_from_confidence(conf, tier=None):
    """Grade for crypto 4h (engine v3.1) signals: from the calibrated chance of TP1."""
    if conf is None:
        return "C"
    if tier == "strong" and conf >= 80:
        return "A"
    if conf >= 75 or tier == "strong":
        return "B"
    return "C"


# ===========================================================================
# rules engine R1 (fallback for a stream without a model file)
# ===========================================================================
def _ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _wilder(s, n):
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def indicators(df):
    d = df.reset_index(drop=True).copy()
    c, h, l, o = d["close"], d["high"], d["low"], d["open"]
    d["ema20"], d["ema50"], d["ema200"] = _ema(c, 20), _ema(c, 50), _ema(c, 200)
    delta = c.diff()
    up, dn = _wilder(delta.clip(lower=0), 14), _wilder((-delta).clip(lower=0), 14)
    d["rsi"] = 100 - 100 / (1 + up / (dn + EPS))
    macd = _ema(c, 12) - _ema(c, 26)
    d["hist"] = macd - macd.ewm(span=9, adjust=False).mean()
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = _wilder(tr, 14)
    upm, dnm = h.diff(), -l.diff()
    pdm = np.where((upm > dnm) & (upm > 0), upm, 0.0)
    ndm = np.where((dnm > upm) & (dnm > 0), dnm, 0.0)
    atr_s = _wilder(tr, 14) + EPS
    d["pdi"] = 100 * _wilder(pd.Series(pdm, index=d.index), 14) / atr_s
    d["ndi"] = 100 * _wilder(pd.Series(ndm, index=d.index), 14) / atr_s
    dx = 100 * (d["pdi"] - d["ndi"]).abs() / (d["pdi"] + d["ndi"] + EPS)
    d["adx"] = _wilder(dx, 14)
    vol = d["volume"].astype(float) if "volume" in d else pd.Series(0.0, index=d.index)
    vavg = vol.rolling(20).mean().shift(1)
    d["vol_ratio"] = np.where(vavg > 0, vol / (vavg + EPS), np.nan)
    d["hi20"] = h.rolling(20).max().shift(1)
    d["lo20"] = l.rolling(20).min().shift(1)
    d["res50"] = h.rolling(50).max().shift(1)
    d["sup50"] = l.rolling(50).min().shift(1)
    d["lo5"] = l.rolling(5).min()
    d["hi5"] = h.rolling(5).max()
    d["low3"] = l.rolling(3).min()
    d["high3"] = h.rolling(3).max()
    return d


def htf_trend(df):
    """+1 / -1 / 0 per higher-timeframe candle: close and EMA50 on the same side
    of EMA200 (EMA50 slope when there is no EMA200 yet)."""
    d = df.reset_index(drop=True)
    c = d["close"]
    e50, e200 = _ema(c, 50), _ema(c, 200)
    slope = e50 - e50.shift(3)
    up = (c > e50) & ((e50 > e200) | (e200.isna() & (slope > 0)))
    dn = (c < e50) & ((e50 < e200) | (e200.isna() & (slope < 0)))
    return pd.Series(np.where(up, 1, np.where(dn, -1, 0)), index=d.index)


def attach_htf(d, htf_df, tf, htf):
    """Adds column 'htf' = trend of the last higher-timeframe candle that had
    CLOSED when this candle closed (no look-ahead)."""
    if htf_df is None or not len(htf_df):
        d["htf"] = 0
        return d
    hs = markets.TF_SEC[htf]
    ls = markets.TF_SEC[tf]
    h = pd.DataFrame({"avail": pd.to_datetime(htf_df["timestamp"]).reset_index(drop=True) + pd.Timedelta(seconds=hs),
                      "htf": htf_trend(htf_df).values})
    h = h.sort_values("avail")
    left = pd.DataFrame({"at": pd.to_datetime(d["timestamp"]) + pd.Timedelta(seconds=ls), "_i": np.arange(len(d))})
    m = pd.merge_asof(left.sort_values("at"), h, left_on="at", right_on="avail", direction="backward")
    m = m.sort_values("_i")
    d["htf"] = m["htf"].fillna(0).astype(int).values
    return d


# ===========================================================================
# setups + score (vectorised per symbol)
# ===========================================================================
def _side_arrays(d, sgn, crypto):
    """Setup trigger name per candle (or ''), score 0-100 and room in ATR to the
    next opposing level, for one side (sgn=+1 long, -1 short)."""
    c, o, h, l = (d[k].to_numpy(float) for k in ("close", "open", "high", "low"))
    e20, e50, e200 = (d[k].to_numpy(float) for k in ("ema20", "ema50", "ema200"))
    rsi, hist, atr, adx = (d[k].to_numpy(float) for k in ("rsi", "hist", "atr", "adx"))
    pdi, ndi, vr = d["pdi"].to_numpy(float), d["ndi"].to_numpy(float), d["vol_ratio"].to_numpy(float)
    htf = d["htf"].to_numpy(int)
    rsi_p = np.roll(rsi, 1)
    rsi_p[0] = np.nan
    hist_p = np.roll(hist, 1)
    hist_p[0] = np.nan
    c_p = np.roll(c, 1)
    c_p[0] = np.nan
    rng = np.maximum(h - l, EPS)
    if sgn > 0:
        stack = (e20 > e50) & (e50 > e200)
        half = e20 > e50
        trend = e50 > e200
        mom_rsi = (rsi > 50) & (rsi < 75)
        di_ok = pdi > ndi
        brk = (c > d["hi20"].to_numpy(float)) & ((c - l) / rng >= 2 / 3)
        dip = d["low3"].to_numpy(float) <= e20 * 1.001
        bull = c > o
        cont = stack & (c > e20) & (((hist > 0) & (hist_p <= 0)) | ((rsi > 50) & (rsi_p <= 50)))
        pull = trend & dip & bull & (c > e20) & (c > e50) & (rsi >= 40) & (rsi <= 65)
        level = d["sup50"].to_numpy(float)
        rev = (l <= level + 0.5 * atr) & (rsi_p < 35) & (rsi > rsi_p) & bull & (c > c_p)
        opp = d["res50"].to_numpy(float)
        room = (opp - c) / np.maximum(atr, EPS)
    else:
        stack = (e20 < e50) & (e50 < e200)
        half = e20 < e50
        trend = e50 < e200
        mom_rsi = (rsi < 50) & (rsi > 25)
        di_ok = ndi > pdi
        brk = (c < d["lo20"].to_numpy(float)) & ((h - c) / rng >= 2 / 3)
        dip = d["high3"].to_numpy(float) >= e20 * 0.999
        bear = c < o
        cont = stack & (c < e20) & (((hist < 0) & (hist_p >= 0)) | ((rsi < 50) & (rsi_p >= 50)))
        pull = trend & dip & bear & (c < e20) & (c < e50) & (rsi <= 60) & (rsi >= 35)
        level = d["res50"].to_numpy(float)
        rev = (h >= level - 0.5 * atr) & (rsi_p > 65) & (rsi < rsi_p) & bear & (c < c_p)
        opp = d["sup50"].to_numpy(float)
        room = (c - opp) / np.maximum(atr, EPS)
    if crypto:
        brk = brk & (vr >= 1.3)
    # score
    sc = np.where(stack, 20.0, np.where(half, 10.0, 0.0))
    sc += np.where(htf == sgn, 20.0, np.where(htf == 0, 5.0, 0.0))
    sc += np.where(sgn * hist > 0, 8.0, 0.0) + np.where(sgn * (hist - hist_p) > 0, 7.0, 0.0)
    sc += np.where(mom_rsi, 10.0, 0.0)
    sc += np.where((adx >= 20) & di_ok, 15.0, np.where(adx >= 20, 5.0, 0.0))
    room_r_tp2 = np.where(np.isfinite(room), room, np.inf)
    sc += np.where(room_r_tp2 >= 4.0, 10.0, np.where(room_r_tp2 >= 2.5, 5.0, 0.0))
    if crypto:
        sc += np.where(vr >= 1.2, 10.0, 0.0)
        top = 100.0
    else:
        top = 90.0
    sc = sc / top * 100.0
    setup = np.full(len(c), "", dtype=object)
    # priority: breakout, trend continuation, pullback, reversal
    for name, mask in (("reversal", rev), ("pullback", pull), ("trend_continuation", cont), ("breakout", brk)):
        setup[np.asarray(mask, bool)] = name
    return setup, sc, room


def candidates(d, crypto):
    """Per candle: best qualified side. Returns dict of arrays."""
    n = len(d)
    out = {"side": np.full(n, "", dtype=object), "score": np.full(n, np.nan), "setup": np.full(n, "", dtype=object),
           "best_long": np.full(n, np.nan), "best_short": np.full(n, np.nan)}
    if n == 0:
        return out
    htf = d["htf"].to_numpy(int)
    adx = d["adx"].to_numpy(float)
    atr = d["atr"].to_numpy(float)
    ok_base = np.isfinite(atr) & (atr > 0) & np.isfinite(d["ema200"].to_numpy(float))
    res = {}
    for sgn, name in ((1, "LONG"), (-1, "SHORT")):
        setup, sc, room = _side_arrays(d, sgn, crypto)
        mtf = np.where(setup == "reversal", htf != -sgn, htf == sgn)
        adx_ok = np.where(setup == "reversal", True, adx >= 15)
        # minimum reward:risk 1.5 to the next level; the stop is at least 1 ATR, so 1.5R >= 1.5 ATR
        rr_ok = ~np.isfinite(room) | (room >= MIN_RR * SL_ATR_MIN)
        good = ok_base & (setup != "") & mtf & adx_ok & rr_ok & (sc >= MIN_SCORE)
        res[name] = (good, sc, setup)
        out["best_long" if sgn > 0 else "best_short"] = sc
    gl, sl_, setl = res["LONG"]
    gs, ss, sets = res["SHORT"]
    pick_long = gl & (~gs | (sl_ >= ss))
    pick_short = gs & ~pick_long
    out["side"][pick_long] = "LONG"
    out["side"][pick_short] = "SHORT"
    out["score"] = np.where(pick_long, sl_, np.where(pick_short, ss, np.nan))
    out["setup"] = np.where(pick_long, setl, np.where(pick_short, sets, ""))
    return out


def stop_distance(side, entry, d, i):
    """ATR stop behind the 5-candle swing, between 1.0 and 2.5 ATR."""
    atr = float(d["atr"].iat[i])
    if side == "LONG":
        swing = entry - (float(d["lo5"].iat[i]) - 0.2 * atr)
    else:
        swing = (float(d["hi5"].iat[i]) + 0.2 * atr) - entry
    return float(min(max(swing, SL_ATR_MIN * atr), SL_ATR_MAX * atr))


# ===========================================================================
# following a trade (shared with the live tracker)
# ===========================================================================
def follow(side, entry, sl, o, h, l, c, start, plan, exit_mask=None):
    """Walks closed candles from `start` with the trade plan (pessimistic: a
    stop touched in a candle is taken before any target in it; a candle that
    opens beyond the stop exits at the open; stop moves apply from the next
    candle). exit_mask[j] True = close the rest at that candle's close (forex,
    last candle before the weekend). Returns dict(status, exit_price, bars,
    hits, r_gross, stop_now); status None while still running."""
    tr, fr = plan["targets_r"], plan["fractions"]
    be_after, max_bars = int(plan.get("be_after") or 0), int(plan["max_bars"])
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
        if ro <= stop_r:
            realized += (1 - done) * ro
            return out("SL" if hits == 0 else f"TP{hits}", o[j], k + 1)
        if rl <= stop_r:
            realized += (1 - done) * stop_r
            return out("SL" if hits == 0 else f"TP{hits}", entry + sgn * stop_r * risk, k + 1)
        new_stop = stop_r
        while hits < len(tr) and rh >= tr[hits]:
            realized += fr[hits] * tr[hits]
            done += fr[hits]
            hits += 1
            if be_after and hits == be_after:
                new_stop = max(new_stop, 0.0)
            elif be_after and hits > be_after:
                new_stop = max(new_stop, tr[hits - 2])
        if hits == len(tr) or done >= 1 - 1e-9:
            return out(f"TP{hits}", entry + sgn * tr[hits - 1] * risk, k + 1)
        stop_r = new_stop
        if k + 1 == max_bars or (exit_mask is not None and exit_mask[j]):
            realized += (1 - done) * sgn * (c[j] - entry) / risk
            status = "EXPIRED" if hits == 0 else f"TP{hits}"
            return out(status, c[j], k + 1)
    return out(None, None, max_bars)


def weekend_exit_mask(ts, tf_sec, now=None):
    """True for the last forex candle before each weekly close (Friday 17:00
    New York): the trade is closed there, so weekend gaps never fake a stop or
    a target. Also true for a candle followed by a candle after the close."""
    t = pd.to_datetime(pd.Series(ts)).reset_index(drop=True)
    n = len(t)
    if not n:
        return np.zeros(0, dtype=bool)
    ny = t.dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    local = ny.dt.tz_localize(None)
    days = (4 - local.dt.weekday) % 7
    cand = local.dt.normalize() + pd.to_timedelta(days, unit="D") + pd.Timedelta(hours=17)
    cand = cand.where(cand > local, cand + pd.Timedelta(days=7))
    close = cand.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward")
    close = close.dt.tz_convert("UTC").dt.tz_localize(None)
    end = t + pd.Timedelta(seconds=int(tf_sec))
    mask = np.array((end >= close - pd.Timedelta(seconds=1)).to_numpy(), dtype=bool)
    nxt = t.shift(-1)
    mask = mask | np.array((nxt >= close).fillna(False).to_numpy(), dtype=bool)
    return mask


def cost_r(symbol, entry, stop):
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    if markets.is_forex(symbol):
        return markets.cost_price(symbol) / risk
    return markets.CRYPTO_COST_ROUND_TRIP * entry / risk


def price_round(x, symbol=None):
    if x is None or not np.isfinite(x) or x == 0:
        return x
    if symbol and markets.is_forex(symbol):
        return round(float(x), markets.price_decimals(symbol))
    digits = max(2, 6 - int(math.floor(math.log10(abs(x)))))
    return round(float(x), min(digits, 12))


def result_units(symbol, entry, stop, r):
    """Whole-position result (all partial closes, after costs) as % of the entry
    price and, for forex, pips: R x the stop distance."""
    if r is None or not entry or stop is None:
        return {"r": r}
    risk = abs(float(entry) - float(stop))
    out = {"r": r, "pct": round(float(r) * risk / float(entry) * 100, 3)}
    if markets.is_forex(symbol):
        out["pips"] = round(float(r) * markets.to_pips(symbol, risk), 1)
    return out


def hold_minutes(tf, bars):
    return int(markets.TF_SEC[tf] // 60 * bars)


# ===========================================================================
# the selection policy (shared by the live engine and the research backtests)
# ===========================================================================
def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def run_policy(data, market, tf, plan, policy, start_time=None, end_time=None, lookback=REPLAY_BARS, blocked=None,
               warmup=0):
    """Replays a stream over candles and returns (active, last, closed).

    data: {symbol: dict of numpy arrays, one value per candle (oldest first):
        ts (datetime64), o, h, l, c, dist (stop distance for a trade entered at
        that close), side ('LONG' / 'SHORT' / '' = no candidate), score (ranks
        candidates), conf (shown to users), grade, setup (optional), atr}
    Rules: one trade at a time per symbol, a cooldown after each trade, at most
    max_per_bar new signals per candle (best score first), forex time rules,
    and blocked(symbol, signal_time) -> reason (news blackout)."""
    tf_sec = markets.TF_SEC[tf]
    forex = market == "forex"
    max_per_bar = int(policy.get("max_per_bar") or 0)
    cooldown = int(policy.get("cooldown") or 0)
    sessions = set(policy.get("sessions") or ()) or None
    close_h = float(policy.get("no_new_before_close_h") or 0)
    events, info = [], {}
    for sym, v in data.items():
        ts = v["ts"]
        n = len(ts)
        if not n:
            continue
        if start_time is not None:
            i0 = int(np.searchsorted(ts, np.datetime64(pd.Timestamp(start_time))))
        else:
            i0 = n - lookback
        i0 = max(i0, warmup, 0)
        i1 = n if end_time is None else int(np.searchsorted(ts, np.datetime64(pd.Timestamp(end_time))))
        if i0 >= i1:
            continue
        side = v["side"]
        idx = np.nonzero(side[i0:i1] != "")[0] + i0
        for i in idx:
            events.append((ts[i], -float(v["score"][i]), sym, int(i)))
        info[sym] = {"exit": weekend_exit_mask(ts, tf_sec) if forex else None}
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    busy = {s: -1 for s in data}
    active, last, closed = {}, {}, {}
    k = 0
    ne = len(events)
    allowed_cache = {}
    while k < ne:
        t = events[k][0]
        j = k
        while j < ne and events[j][0] == t:
            j += 1
        group = events[k:j]
        k = j
        bar_time = pd.Timestamp(t).to_pydatetime()
        close_time = bar_time + timedelta(seconds=tf_sec)
        if forex:
            ok = allowed_cache.get(t)
            if ok is None:
                ok = markets.forex_open(bar_time) and markets.hours_to_close(close_time) >= close_h
                if ok and sessions:
                    ok = bool(set(markets.sessions_at(close_time)) & sessions)
                allowed_cache[t] = ok
            if not ok:
                continue
        picked = 0
        for _, neg, sym, i in group:
            if max_per_bar and picked >= max_per_bar:
                break
            if i <= busy[sym]:
                continue
            if blocked is not None and blocked(sym, close_time):
                continue
            v = data[sym]
            tr = make_trade(sym, market, tf, v, i, plan)
            res = follow(tr["side"], tr["entry"], tr["stop_loss"], v["o"], v["h"], v["l"], v["c"], i + 1, plan,
                         exit_mask=info[sym]["exit"])
            picked += 1
            tr["bars_held"] = int(res["bars"])
            tr["hits"] = int(res["hits"])
            if res["status"] is None:
                tr["stop_now"] = float(res["stop_now"])
                tr["realized_r"] = round(float(res["r_gross"]), 4)
                active[sym] = tr
                busy[sym] = 10 ** 12
                continue
            r = float(res["r_gross"]) - cost_r(sym, tr["entry"], tr["stop_loss"])
            jx = min(i + res["bars"], len(v["ts"]) - 1)
            tr.update({"status": res["status"], "exit_price": res["exit_price"], "r": round(r, 3),
                       "closed_at": pd.Timestamp(v["ts"][jx]).to_pydatetime() + timedelta(seconds=tf_sec)})
            tr.update({kk: x for kk, x in result_units(sym, tr["entry"], tr["stop_loss"], tr["r"]).items()
                       if kk != "r"})
            last[sym] = tr
            closed.setdefault(sym, []).append(tr)
            busy[sym] = i + res["bars"] + cooldown
    return active, last, closed


def make_trade(sym, market, tf, v, i, plan):
    tf_sec = markets.TF_SEC[tf]
    side = str(v["side"][i])
    entry = float(v["c"][i])
    dist = float(v["dist"][i])
    sgn = 1 if side == "LONG" else -1
    sl = entry - sgn * dist
    tps = [entry + sgn * k * dist for k in plan["targets_r"]]
    bar_time = pd.Timestamp(v["ts"][i]).to_pydatetime()
    signal_at = bar_time + timedelta(seconds=tf_sec)
    expires = signal_at + timedelta(seconds=tf_sec * plan["max_bars"])
    if market == "forex":
        expires = min(expires, markets.forex_week_close(signal_at))
    conf = float(v["conf"][i])
    setup = v.get("setup")
    hold = plan.get("hold_bars") or max(1, plan["max_bars"] // 3)
    tr = {"side": side, "entry": entry, "stop_loss": float(sl), "take_profit": float(tps[-1]),
          "targets": [float(x) for x in tps], "targets_r": list(plan["targets_r"]), "fractions": list(plan["fractions"]),
          "atr": float(v["atr"][i]), "confidence": round(conf, 1), "score": round(float(v["score"][i]), 4),
          "grade": str(v["grade"][i]), "setup": (str(setup[i]) or None) if setup is not None else None,
          "tier": None, "market": market, "timeframe": tf, "bar_time": bar_time,
          "signal_at": signal_at, "expires_at": expires, "rr": round(plan["targets_r"][-1], 2),
          "hold_minutes": hold_minutes(tf, hold)}
    if market == "forex":
        tr["sl_pips"] = round(markets.to_pips(sym, dist), 1)
        tr["tp_pips"] = [round(markets.to_pips(sym, k * dist), 1) for k in plan["targets_r"]]
    return tr


def summary(closed_lists, days=None):
    """Stats over closed trades (any iterable of trade dicts with status and r)."""
    rows = [t for lst in closed_lists for t in lst]
    n = len(rows)
    wins = sum(1 for t in rows if str(t.get("status", "")).startswith("TP"))
    losses = sum(1 for t in rows if t.get("status") == "SL")
    rs = [float(t["r"]) for t in rows if t.get("r") is not None]
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    out = {"signals": n, "wins": wins, "losses": losses, "expired": n - wins - losses,
           "win_rate": round(wins / n * 100, 1) if n else None,
           "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
           "total_r": round(sum(rs), 2) if rs else 0.0,
           "profit_factor": round(gp / gl, 2) if gl > 0 else None}
    if days:
        out["signals_per_day"] = round(n / days, 2)
    return out


# ===========================================================================
# rules engine R1 replay
# ===========================================================================
def prepare(symbol, df, htf_df, tf):
    d = indicators(df)
    d = attach_htf(d, htf_df, tf, HTF_OF[tf])
    cand = candidates(d, crypto=not markets.is_forex(symbol))
    return d, cand


def rules_data(symbol, d, cand):
    n = len(d)
    side = np.asarray(cand["side"], dtype=object)
    dist = np.full(n, np.nan)
    for i in np.nonzero(side != "")[0]:
        dist[i] = stop_distance(side[i], float(d["close"].iat[i]), d, i)
    score = np.nan_to_num(np.asarray(cand["score"], dtype=float), nan=0.0)
    return {"ts": pd.to_datetime(d["timestamp"]).to_numpy(), "o": d["open"].to_numpy(float),
            "h": d["high"].to_numpy(float), "l": d["low"].to_numpy(float), "c": d["close"].to_numpy(float),
            "atr": d["atr"].to_numpy(float), "side": side, "score": score, "conf": score,
            "grade": np.array([grade_of(x) if x else "C" for x in score], dtype=object),
            "setup": np.asarray(cand["setup"], dtype=object), "dist": dist}


def replay(prepared, market, tf, lookback=REPLAY_BARS, start_time=None, blocked=None, now=None):
    """R1: prepared = {symbol: (indicators, candidates)} -> (active, last, closed)."""
    plan = plan_for(market, tf)
    data = {sym: rules_data(sym, d, cand) for sym, (d, cand) in prepared.items()}
    return run_policy(data, market, tf, plan, policy_for(market, tf), start_time=start_time, lookback=lookback,
                      blocked=blocked, warmup=WARMUP)


# ===========================================================================
# candles
# ===========================================================================
def last_closed_bar(tf, now=None):
    now = now or _utcnow()
    sec = markets.TF_SEC[tf]
    epoch = int((now - datetime(1970, 1, 1)).total_seconds())
    return datetime(1970, 1, 1) + timedelta(seconds=(epoch // sec - 1) * sec)


def _ms(dt):
    return int((pd.Timestamp(dt) - pd.Timestamp(0)).total_seconds() * 1000)


class CandleStore:
    """Closed candles per (symbol, timeframe), topped up incrementally.
    Forex 4h candles are built from 1h candles (UTC 4-hour buckets), exactly
    as in the research, so the provider's own 4h alignment does not matter."""

    def __init__(self, get_candles, history=650, sleep=0.12):
        self.get_candles = get_candles
        self.history = history
        self.sleep = sleep
        self._data = {}
        self._lock = threading.Lock()

    def _call(self, symbol, tf, limit, since):
        df = self.get_candles(symbol=symbol, timeframe=tf, limit=limit, since=since)
        if df is None or not len(df):
            return None
        df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df

    def frame(self, symbol, tf, now=None, bars=None):
        now = now or _utcnow()
        bars = int(bars or self.history)
        if markets.is_forex(symbol) and tf == "4h":
            import features
            h1 = self.frame(symbol, "1h", now, bars=bars * 4 + 8)
            return features.resample_closed(h1, "4h").tail(bars).reset_index(drop=True)
        sec = markets.TF_SEC[tf]
        key = (symbol, tf)
        old = self._data.get(key)
        frames = [old] if old is not None and len(old) >= min(bars, 50) else []
        if frames and len(old) < bars and len(old) < self.history:
            frames = []      # the cache is too short for this request: load the full history again
        if frames:
            cursor = _ms(old["timestamp"].iloc[-1])
        else:
            cursor = _ms(now - timedelta(seconds=sec * (bars + 2)))
        if markets.is_forex(symbol):
            limit = min(5000, bars + 5) if not frames else 300
            df = self._call(symbol, tf, limit, None if not frames else cursor)
            if df is not None:
                frames.append(df)
        else:
            last_seen = None
            for _ in range(bars // 300 + 4):
                df = self._call(symbol, tf, 300, cursor)
                if df is None:
                    break
                frames.append(df)
                last_ms = _ms(df["timestamp"].iloc[-1])
                if last_seen is not None and last_ms <= last_seen:
                    break
                last_seen = last_ms
                cursor = last_ms + sec * 1000
                if cursor > _ms(now) - sec * 1000:
                    break
                time.sleep(self.sleep)
        if not frames:
            raise ValueError(f"No {tf} candles for {symbol}.")
        df = (pd.concat(frames).drop_duplicates(subset=["timestamp"], keep="last")
              .sort_values("timestamp").reset_index(drop=True))
        df = df[df["timestamp"] + pd.Timedelta(seconds=sec) <= pd.Timestamp(now)].reset_index(drop=True)
        if markets.is_forex(symbol) and tf != "1d":
            ok = forex_open_mask(df["timestamp"])
            df = df[ok].reset_index(drop=True)
        df = df.tail(max(bars, self.history)).reset_index(drop=True)
        with self._lock:
            self._data[key] = df
        return df


def forex_open_mask(ts):
    """Vectorised markets.forex_open for candle start times."""
    t = pd.to_datetime(pd.Series(ts)).reset_index(drop=True)
    if not len(t):
        return np.zeros(0, dtype=bool)
    ny = t.dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    wd, h = ny.dt.weekday.to_numpy(), ny.dt.hour.to_numpy()
    closed = (wd == 5) | ((wd == 4) & (h >= 17)) | ((wd == 6) & (h < 17))
    return ~closed


# ===========================================================================
# live engines
# ===========================================================================
class StreamEngine:
    """One stream. board() -> {symbol: result} at the last closed candle.
    Uses the stream's model when models/<key>.json exists, else rules R1."""

    def __init__(self, market, tf, store, symbols, blocked=None):
        self.market, self.tf = market, tf
        self.key = stream_key(market, tf)
        self.cfg = STREAMS[self.key]
        self.store = store
        self.symbols = list(symbols)
        self.blocked = blocked
        self._board = None
        self._lock = threading.Lock()
        self.errors = {}

    @property
    def model(self):
        return load_model(self.key)

    def board(self, now=None, force=False):
        now = now or _utcnow()
        bar = last_closed_bar(self.tf, now)
        if not force and self._board and self._board[0] == bar:
            return self._board[1]
        with self._lock:
            if not force and self._board and self._board[0] == bar:
                return self._board[1]
            m = self.model
            out = self._model_board(m, now) if m is not None else self._rules_board(now)
            self._board = (bar, out)
            return out

    # ---- rules R1
    def _rules_board(self, now):
        prepared, errors = {}, {}
        htf = HTF_OF[self.tf]
        for sym in self.symbols:
            try:
                df = self.store.frame(sym, self.tf, now)
                hdf = self.store.frame(sym, htf, now, bars=320)
                if len(df) <= WARMUP + 5:
                    raise ValueError(f"Not enough {self.tf} history for {sym}.")
                prepared[sym] = prepare(sym, df, hdf, self.tf)
            except Exception as e:
                errors[sym] = str(e)[:160]
        active, last, closed = replay(prepared, self.market, self.tf, blocked=self.blocked, now=now)
        out = {}
        for sym, (d, cand) in prepared.items():
            bl, bs = cand["best_long"][-1], cand["best_short"][-1]
            out[sym] = self._result(d, active.get(sym), last.get(sym), closed.get(sym, []),
                                    float(bl) if np.isfinite(bl) else 0.0, float(bs) if np.isfinite(bs) else 0.0,
                                    MIN_SCORE, MIN_SCORE - 10, RULES_VERSION, plan_for(self.market, self.tf))
        for sym, err in errors.items():
            out[sym] = {"error": err, "market": self.market, "timeframe": self.tf}
        self.errors = errors
        return out

    # ---- trained model
    def _model_board(self, m, now):
        import features as FE
        frames, errors = {}, {}
        hist = FE.HISTORY[self.tf]
        for sym in self.symbols:
            try:
                df = self.store.frame(sym, self.tf, now, bars=hist)
                if len(df) < 260:
                    raise ValueError(f"Not enough {self.tf} history for {sym} yet.")
                frames[sym] = FE.symbol_features(df, self.tf, session=m.session, volume=m.volume)
            except Exception as e:
                errors[sym] = str(e)[:160]
        anchor = mkt = cs = None
        # the cross-symbol blocks use the symbols the model was trained with (same inputs as in its tests)
        trained = set(m.meta.get("trained_symbols") or [])
        ref = {k: v for k, v in frames.items() if k in trained} or frames
        if self.market == "crypto":
            mkt = FE.market_block(ref)
            if m.anchor and frames.get(m.anchor) is not None:
                anchor = FE.anchor_block(frames[m.anchor])
        else:
            cs = FE.currency_block(ref)
        gates = self._gate_scores(m, frames) if m.gate is not None else {}
        data, lean = {}, {}
        tail = REPLAY_BARS + 2
        for sym, F in frames.items():
            try:
                X = FE.assemble(F, m.features, sym=sym, anchor=anchor, mkt=mkt, cs=cs).iloc[-tail:]
                Fx = F.iloc[-tail:]
                pl = m.raw_proba(X.to_numpy(dtype=float))
                ps = m.raw_proba(FE.flip(X).to_numpy(dtype=float))
                g = gates.get(sym)
                if m.gate is not None:
                    g = align_gate(Fx, self.tf, g, m.gate.tf) if g is not None else (
                        np.full(len(Fx), np.nan), np.full(len(Fx), np.nan))
                data[sym] = model_data(m, Fx, pl, ps, gate=g)
                lean[sym] = (float(m.calibrate(pl[-1:])[0]), float(m.calibrate(ps[-1:])[0]), float(max(pl[-1], ps[-1])))
            except Exception as e:
                errors[sym] = str(e)[:160]
        plan = plan_for(self.market, self.tf)
        plan["hold_bars"] = m.hold_bars
        active, last, closed = run_policy(data, self.market, self.tf, plan, policy_for(self.market, self.tf),
                                          lookback=REPLAY_BARS, blocked=self.blocked)
        out = {}
        for sym, v in data.items():
            cl, cs_, best = lean[sym]
            d = frames[sym]
            res = self._result(d, active.get(sym), last.get(sym), closed.get(sym, []), cl * 100, cs_ * 100,
                               m.threshold, m.watch, "v" + m.version, plan, best_raw=best)
            out[sym] = res
        for sym, err in errors.items():
            out[sym] = {"error": err, "market": self.market, "timeframe": self.tf}
        self.errors = errors
        return out

    def _gate_scores(self, m, frames):
        """{symbol: (gate timestamps, raw long, raw short)} of the higher-timeframe gate model."""
        import features as FE
        gm = m.gate
        G = {}
        for sym, F in frames.items():
            try:
                base = F[["timestamp", "open", "high", "low", "close"]].copy()
                base["volume"] = 1.0
                if "volume" in self.store._data.get((sym, self.tf), pd.DataFrame()).columns:
                    raw = self.store._data[(sym, self.tf)]
                    base = raw[["timestamp", "open", "high", "low", "close", "volume"]].copy()
                gdf = FE.resample_closed(base, gm.tf)
                if len(gdf) < 220:
                    continue
                G[sym] = FE.symbol_features(gdf, gm.tf, session=gm.session, volume=gm.volume)
            except Exception:
                continue
        mkt = anchor = cs = None
        trained = set(m.meta.get("trained_symbols") or [])
        ref = {k: v for k, v in G.items() if k in trained} or G
        if self.market == "crypto":
            mkt = FE.market_block(ref)
            if gm.anchor and G.get(gm.anchor) is not None:
                anchor = FE.anchor_block(G[gm.anchor])
        else:
            cs = FE.currency_block(ref)
        out = {}
        for sym, Gf in G.items():
            try:
                Xg = FE.assemble(Gf, gm.features, sym=sym, anchor=anchor, mkt=mkt, cs=cs)
                out[sym] = (pd.to_datetime(Gf["timestamp"]).to_numpy(), gm.raw_proba(Xg.to_numpy(dtype=float)),
                            gm.raw_proba(FE.flip(Xg).to_numpy(dtype=float)))
            except Exception:
                continue
        return out

    def _result(self, d, a, last, closed, p_long, p_short, threshold, watch, version, plan, best_raw=None):
        row = d.iloc[-1]
        bar_time = pd.Timestamp(row["timestamp"]).to_pydatetime()
        tf_sec = markets.TF_SEC[self.tf]
        best = best_raw if best_raw is not None else max(p_long, p_short)
        tps = a["targets"] if a else None
        return {
            "engine_version": version, "market": self.market, "timeframe": self.tf, "covered": True,
            "verdict": a["side"] if a else "WAIT",
            "fresh": bool(a and a["bar_time"] == bar_time),
            "confidence": a["confidence"] if a else round(max(p_long, p_short), 1),
            "grade": a["grade"] if a else None, "setup": a["setup"] if a else None, "tier": None,
            "p_long": round(p_long, 1), "p_short": round(p_short, 1), "bias": "LONG" if p_long >= p_short else "SHORT",
            "strength": round(min(best / threshold, 1.5) * 100, 1) if threshold else None,
            "setup_forming": bool(not a and best >= watch),
            "threshold": threshold,
            "entry": a["entry"] if a else None, "stop_loss": a["stop_loss"] if a else None,
            "stop_now": a["stop_now"] if a else None, "take_profit": a["take_profit"] if a else None,
            "targets": tps, "targets_r": list(plan["targets_r"]), "fractions": list(plan["fractions"]),
            "hits": a["hits"] if a else None, "atr": a["atr"] if a else float(row["atr"]),
            "sl_pct": round(abs(a["entry"] - a["stop_loss"]) / a["entry"] * 100, 3) if a else None,
            "tp_pct": round(abs(tps[-1] - a["entry"]) / a["entry"] * 100, 3) if a else None,
            "tp_pcts": [round(abs(x - a["entry"]) / a["entry"] * 100, 3) for x in tps] if a else None,
            "max_bars": plan["max_bars"], "active": a, "last_closed": last,
            "recent": closed[-RECENT_MAX:], "last_close": float(row["close"]), "brief": None,
            "history_bars": int(len(d)), "bar_time": bar_time,
            "next_update": bar_time + timedelta(seconds=2 * tf_sec),
        }

    def signal(self, symbol, now=None):
        res = self.board(now).get(symbol)
        if res is None:
            raise ValueError(f"{symbol} is not covered by the {self.market} {self.tf} stream.")
        return res


def align_gate(F, tf, g, gate_tf):
    """Gate scores of the last higher-timeframe candle that had closed when each candle of F closed."""
    gts, gl, gs = g
    close = pd.to_datetime(F["timestamp"]).to_numpy() + np.timedelta64(markets.TF_SEC[tf], "s")
    gclose = gts + np.timedelta64(markets.TF_SEC.get(gate_tf, 14400) if gate_tf != "1w" else 604800, "s")
    idx = np.searchsorted(gclose, close, side="right") - 1
    ok = idx >= 0
    ii = idx.clip(0)
    return np.where(ok, gl[ii], np.nan), np.where(ok, gs[ii], np.nan)


def model_data(m, F, pl, ps, gate=None):
    """Candidates of a model stream for run_policy: the better side when its
    raw score reaches the threshold (and the higher-timeframe gate agrees);
    stop = sl_atr x ATR."""
    pl = np.asarray(pl, dtype=float)
    ps = np.asarray(ps, dtype=float)
    best = np.fmax(pl, ps)
    longs = pl >= ps
    atr = F["atr"].to_numpy(float)
    ok = np.isfinite(best) & (best >= m.threshold) & np.isfinite(atr) & (atr > 0)
    if gate is not None and getattr(m, "gate", None) is not None:
        gl, gs = gate
        side_score = np.where(longs, gl, gs)
        other = np.where(longs, gs, gl)
        ok &= np.isfinite(side_score) & (side_score >= other) & (side_score >= m.gate.level)
    side = np.where(ok, np.where(longs, "LONG", "SHORT"), "").astype(object)
    conf = np.where(longs, m.calibrate(pl), m.calibrate(ps)) * 100
    grade = np.array([m.grade(x) for x in best], dtype=object)
    return {"ts": pd.to_datetime(F["timestamp"]).to_numpy(), "o": F["open"].to_numpy(float),
            "h": F["high"].to_numpy(float), "l": F["low"].to_numpy(float), "c": F["close"].to_numpy(float),
            "atr": atr, "side": side, "score": np.nan_to_num(best, nan=0.0), "conf": conf, "grade": grade,
            "setup": None, "dist": m.sl_atr * atr}
