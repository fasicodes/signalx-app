"""
Research lab for the SignalX signal streams (crypto/forex x 15m/1h/4h).

  python lab.py build  <stream>            features + labels per symbol (ds/<stream>/<sym>.npz)
  python lab.py wf     <stream> <geom>     walk-forward over the validation years + holdout predictions
  python lab.py eval   <stream> <geom>     threshold chosen on validation only, then the holdout test
  python lab.py export <stream> <geom>     writes models/<stream>.json for the site
  python lab.py rules  <stream>            backtest of rules engine R1 (the 'before' baseline)

Protocol (no look-ahead, no tuning on the holdout):
  * features: features.py (the same code the site runs);
  * label: the trade's first target (TP1 = 0.5R) is reached before the stop
    (stop = sl_atr x ATR), pessimistic (a candle touching both = stop);
  * walk-forward on the validation years: for every 6-month fold the model
    is trained only on earlier data, minus a purge gap of one label horizon;
  * the threshold, grades and calibration are chosen on these validation
    predictions only (lowest threshold with win rate >= TARGET_WIN and
    average result >= MIN_AVG_R after costs);
  * holdout: the final model (trained on everything before the holdout,
    minus the purge gap) is scored once on the holdout with the chosen
    threshold - that is the published test, and it is the model the site runs.
"""
import glob
import json
import os
import pickle
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
import features as FE  # noqa: E402
import markets  # noqa: E402
import streams as ST  # noqa: E402

R = os.environ.get("RESEARCH_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
# Selection rule (fixed before the holdout of the 15m streams, the multi-timeframe variants and forex s3 was
# looked at; the first version - 72% / +0.02R, no yearly check - failed the crypto 1h holdout):
TARGET_WIN = 73.0      # validation win rate the threshold must reach (the holdout must stay >= 70)
MIN_AVG_R = 0.03       # and a positive average result after costs
YEAR_MIN_WIN = 70.0    # ... in every validation year too, each with a positive average
MIN_SIGNALS = 100
RULE = (f"v2: validation win rate >= {TARGET_WIN}%, average >= {MIN_AVG_R}R after costs, every validation year "
        f">= {YEAR_MIN_WIN}% with a positive average, >= {MIN_SIGNALS} signals")
TARGETS_R = [0.5, 1.0, 2.0]
FRACTIONS = [1 / 3, 1 / 3, 1 / 3]
BE_AFTER = 1
FOLD_MONTHS = 6

CFG = {
    "crypto_15m": dict(market="crypto", tf="15m", data_start="2021-06-01", dev=("2023-01-01", "2025-10-01"),
                       hold=("2025-10-01", "2026-10-11"), sub=12, geoms={"s2": (2.0, 64), "s3": (3.0, 64), "t3": (3.0, 64, 1.0)},
                       session=True, volume=True, anchor="BTC/USDT", max_per_bar=3, cooldown=4),
    "crypto_1h": dict(market="crypto", tf="1h", data_start="2020-03-01", dev=("2023-01-01", "2025-10-01"),
                      hold=("2025-10-01", "2026-10-11"), sub=2, geoms={"s2": (2.0, 48), "s3": (3.0, 48), "t2": (2.0, 48, 1.0)},
                      session=True, volume=True, anchor="BTC/USDT", max_per_bar=4, cooldown=2),
    # same as crypto_1h / crypto_4h plus order-flow inputs (taker buy volume, trade counts)
    "crypto_1hf": dict(market="crypto", tf="1h", data_start="2020-03-01", dev=("2023-01-01", "2025-10-01"),
                       hold=("2025-10-01", "2026-10-11"), sub=2, geoms={"s2": (2.0, 48)}, flow=True,
                       session=True, volume=True, anchor="BTC/USDT", max_per_bar=4, cooldown=2),
    "crypto_4hf": dict(market="crypto", tf="4h", data_start="2020-03-01", dev=("2023-01-01", "2025-10-01"),
                       hold=("2025-10-01", "2026-10-11"), sub=1, geoms={"s2": (2.0, 54)}, flow=True,
                       session=False, volume=True, anchor="BTC/USDT", max_per_bar=5, cooldown=1),
    "crypto_4h": dict(market="crypto", tf="4h", data_start="2020-03-01", dev=("2023-01-01", "2025-10-01"),
                      hold=("2025-10-01", "2026-10-11"), sub=1, geoms={"s2": (2.0, 54), "t2": (2.0, 54, 1.0)},
                      session=False, volume=True, anchor="BTC/USDT", max_per_bar=5, cooldown=1),
    "forex_15m": dict(market="forex", tf="15m", data_start="2013-01-01", dev=("2017-01-01", "2021-01-01"),
                      hold=("2021-01-01", "2022-03-05"), sub=4, geoms={"s2": (2.0, 64), "s3": (3.0, 64)},
                      session=True, volume=False, anchor=None, max_per_bar=3, cooldown=4,
                      sessions=("London", "New York"), close_h=2),
    "forex_1h": dict(market="forex", tf="1h", data_start="2013-01-01", dev=("2017-01-01", "2021-01-01"),
                     hold=("2021-01-01", "2022-03-05"), sub=1, geoms={"s2": (2.0, 48), "s3": (3.0, 48)},
                     session=True, volume=False, anchor=None, max_per_bar=3, cooldown=2,
                     sessions=("London", "New York"), close_h=4),
    "forex_4h": dict(market="forex", tf="4h", data_start="2013-01-01", dev=("2017-01-01", "2021-01-01"),
                     hold=("2021-01-01", "2022-03-05"), sub=1, geoms={"s2": (2.0, 30), "s15": (1.5, 30)},
                     session=False, volume=False, anchor=None, max_per_bar=3, cooldown=1,
                     sessions=None, close_h=12),
}
FX_SCALE = {"XAUUSD": 100.0}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------------------
# raw data
# ---------------------------------------------------------------------------
def crypto_symbols():
    return sorted(os.path.basename(p).split("_")[0] + "/USDT" for p in glob.glob(f"{R}/data/crypto/*_15m.csv.gz"))


def load_crypto(sym, start, flow=False):
    coin = sym.split("/")[0]
    cols = ["timestamp", "open", "high", "low", "close", "volume"] + (FE.FLOW_RAW if flow else [])
    d = pd.read_csv(f"{R}/data/crypto/{coin}_15m.csv.gz", usecols=cols, parse_dates=["timestamp"])
    return d[d["timestamp"] >= pd.Timestamp(start) - pd.Timedelta(days=200)].reset_index(drop=True)


def fx_symbols():
    out = []
    for p in sorted(glob.glob(f"{R}/data/fx/*m15.csv")):
        n = os.path.basename(p)[:6]
        out.append(n[:3] + "/" + n[3:])
    return out


def load_fx(sym, start):
    n = sym.replace("/", "")
    d = pd.read_csv(f"{R}/data/fx/{n}m15.csv")
    scale = FX_SCALE.get(n, 1000.0 if n.endswith("JPY") else 100000.0)
    broker = pd.to_datetime(d["Date"])
    ny_local = broker - pd.Timedelta(hours=7)                     # broker time = New York + 7h
    utc = (ny_local.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward")
           .dt.tz_convert("UTC").dt.tz_localize(None))
    out = pd.DataFrame({"timestamp": utc, "open": d["open"] / scale, "high": d["high"] / scale,
                        "low": d["low"] / scale, "close": d["close"] / scale, "volume": d["tick_volume"].astype(float)})
    out = out.dropna(subset=["timestamp"]).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    out = out[ST.forex_open_mask(out["timestamp"])].reset_index(drop=True)
    return out[out["timestamp"] >= pd.Timestamp(start) - pd.Timedelta(days=200)].reset_index(drop=True)


def base_bars(df15, tf):
    if tf == "15m":
        return df15
    return FE.resample_closed(df15, tf)


# ---------------------------------------------------------------------------
# features + labels
# ---------------------------------------------------------------------------
BASE_COLS = ["atr_pct", "atr_rank", "r1", "r3", "r6", "r12", "r24", "r48", "rsi14", "rsi2", "rsi14_d", "d_sma20",
             "d_sma50", "d_sma200", "slope20", "slope50", "slope200", "bb_pctb", "bb_width", "bbw_rank", "adx",
             "di_diff", "don20", "don55", "dist_hi48", "dist_lo48", "body", "upw", "loww", "rng_atr", "vol_z",
             "vol_trend", "er20", "macd_h", "streak", "rv_ratio"]


def feature_list(c):
    cols = [x for x in BASE_COLS if c["volume"] or x not in ("vol_z", "vol_trend")]
    for _, prefix in FE.HTFS[c["tf"]]:
        cols += [prefix + k for k in FE._HTF_KEEP]
    cols += ["r42", "r180"] + [f"z{k}" for k in FE.ZK]
    if c["session"]:
        cols += ["s_hsin", "s_hcos", "s_dow", "s_london", "s_ny"]
    if c.get("flow"):
        cols += FE.FLOW_COLS
    if c["market"] == "crypto":
        cols += ["b_r6", "b_r24", "b_d_sma50", "b_rsi14", "b_slope50", "b_atr_rank", "bh_d_sma50", "bh_slope50",
                 "bh_rsi14", "bh_r6"]
        for zc in FE._ZC:
            cols += ["m_" + zc, "disp_" + zc]
        cols += ["breadth200", "breadth_h50"]
        for zc in FE._ZC:
            cols += ["rel_" + zc, "rk_" + zc]
    else:
        for k in FE.CS_K:
            cols += [f"cs_b{k}", f"cs_q{k}", f"cs_d{k}"]
    return cols


def tp1_labels(o, h, l, c, dist, H, exit_mask=None, tpm=0.5):
    """1 = TP1 (0.5 x dist) reached before the stop (dist) within H candles
    (pessimistic: a candle that touches the stop counts as the stop, a candle
    that opens beyond the stop too). Returns (yL, yS) as int8, -1 = unknown (end of data)."""
    n = len(c)
    pad = H
    hp = np.concatenate([h, np.full(pad, np.nan)])
    lp = np.concatenate([l, np.full(pad, np.nan)])
    from numpy.lib.stride_tricks import sliding_window_view as swv
    HH = swv(hp[1:], H)[:n]
    LL = swv(lp[1:], H)[:n]
    if exit_mask is not None:
        # forex: the trade is closed at the last candle before the weekend -> ignore candles after it
        em = np.concatenate([exit_mask, np.zeros(pad, dtype=bool)])
        EM = swv(em[1:], H)[:n]
        cut = np.where(EM.any(axis=1), EM.argmax(axis=1), H - 1)
        valid = np.arange(H)[None, :] <= cut[:, None]
    else:
        valid = np.ones((n, H), dtype=bool)
    out = []
    for sgn in (1, -1):
        stop = c - sgn * dist
        tp = c + sgn * tpm * dist
        if sgn > 0:
            hit_sl = (LL <= stop[:, None]) & valid
            hit_tp = (HH >= tp[:, None]) & valid
        else:
            hit_sl = (HH >= stop[:, None]) & valid
            hit_tp = (LL <= tp[:, None]) & valid
        fs = np.where(hit_sl.any(axis=1), hit_sl.argmax(axis=1), H + 1)
        ft = np.where(hit_tp.any(axis=1), hit_tp.argmax(axis=1), H + 1)
        y = (ft < fs).astype(np.int8)
        known = np.isfinite(HH).all(axis=1) | (np.minimum(fs, ft) <= H)
        y[~known] = -1
        y[~np.isfinite(dist) | (dist <= 0)] = -1
        out.append(y)
    return out[0], out[1]


def eligible(ts, c, tf):
    """Forex: candles where a new signal may start (sessions, not close to the weekly close)."""
    t = pd.to_datetime(pd.Series(ts))
    close = t + pd.Timedelta(seconds=FE.TF_SEC[tf])
    ok = np.ones(len(t), dtype=bool)
    if c["market"] != "forex":
        return ok
    ok &= ST.forex_open_mask(t)
    ny = close.dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    local = ny.dt.tz_localize(None)
    days = (4 - local.dt.weekday) % 7
    cand = local.dt.normalize() + pd.to_timedelta(days, unit="D") + pd.Timedelta(hours=17)
    cand = cand.where(cand > local, cand + pd.Timedelta(days=7))
    wclose = cand.dt.tz_localize("America/New_York").dt.tz_convert("UTC").dt.tz_localize(None)
    hours = (wclose - close).dt.total_seconds() / 3600
    ok &= (hours >= c.get("close_h", 0)).to_numpy()
    if c.get("sessions"):
        ldn = close.dt.tz_localize("UTC").dt.tz_convert("Europe/London")
        s_l = (ldn.dt.hour >= 8) & (ldn.dt.hour < 17) & (ldn.dt.weekday < 5)
        s_n = (ny.dt.hour >= 8) & (ny.dt.hour < 17) & (ny.dt.weekday < 5)
        sel = np.zeros(len(t), dtype=bool)
        if "London" in c["sessions"]:
            sel |= s_l.to_numpy()
        if "New York" in c["sessions"]:
            sel |= s_n.to_numpy()
        ok &= sel
    return ok


def build(stream):
    c = CFG[stream]
    out = f"{R}/ds/{stream}"
    os.makedirs(out, exist_ok=True)
    syms = crypto_symbols() if c["market"] == "crypto" else fx_symbols()
    log(stream, "symbols", len(syms))
    small, done = {}, []
    anchor = None
    keep_cols = ["timestamp", "close", "d_sma200", "h_d_sma50"] + FE._ZC
    for sym in syms:                        # pass 1: per-symbol features (kept on disk, small part in memory)
        raw = load_crypto(sym, c["data_start"], flow=c.get("flow")) if c["market"] == "crypto" else load_fx(sym, c["data_start"])
        b = base_bars(raw, c["tf"])
        del raw
        if len(b) < 600:
            log("skip", sym, len(b))
            continue
        F = FE.symbol_features(b, c["tf"], session=c["session"], volume=c["volume"], flow=c.get("flow", False))
        F = F[F["timestamp"] >= pd.Timestamp(c["data_start"]) - pd.Timedelta(days=60)].reset_index(drop=True)
        for col in F.columns:
            if F[col].dtype == np.float64 and col not in ("open", "high", "low", "close", "atr"):
                F[col] = F[col].astype(np.float32)
        if c.get("anchor") == sym:
            anchor = FE.anchor_block(F)
        small[sym] = F[keep_cols].copy()
        F.to_pickle(f"{out}/F_{sym.replace('/', '')}.pkl")
        done.append(sym)
        log("features", sym, len(F))
        del F, b
    mkt = cs = None
    if c["market"] == "crypto":
        mkt = FE.market_block(small)
    else:
        cs = FE.currency_block(small)
    del small
    cols = feature_list(c)
    sec = FE.TF_SEC[c["tf"]]
    for sym in done:                        # pass 2: model inputs + labels
        fp = f"{out}/F_{sym.replace('/', '')}.pkl"
        F = pd.read_pickle(fp)
        X = FE.assemble(F, cols, sym=sym, anchor=anchor, mkt=mkt, cs=cs)
        ts = pd.to_datetime(F["timestamp"])
        keep = (ts >= pd.Timestamp(c["data_start"])).to_numpy()
        o, h, l, cl, atr = (F[k].to_numpy(float) for k in ("open", "high", "low", "close", "atr"))
        em = ST.weekend_exit_mask(ts, sec) if c["market"] == "forex" else None
        labs = {}
        for g, gv in c["geoms"].items():
            k, H = gv[:2]
            yL, yS = tp1_labels(o, h, l, cl, k * atr, H, em, gv[2] if len(gv) > 2 else 0.5)
            labs[f"yL_{g}"], labs[f"yS_{g}"] = yL[keep], yS[keep]
        el = eligible(ts, c, c["tf"])
        np.savez(f"{out}/{sym.replace('/', '')}.npz", X=X.to_numpy(np.float32)[keep],
                 ts=ts.to_numpy().astype("datetime64[ns]").astype("int64")[keep], o=o[keep], h=h[keep], l=l[keep], c=cl[keep], atr=atr[keep],
                 elig=el[keep], **labs)
        os.remove(fp)
        log("saved", sym, int(keep.sum()), "label rate", {g: round(float((labs[f"yL_{g}"] == 1).mean()), 3) for g in c["geoms"]})
        del F, X
    json.dump({"cols": cols, "symbols": done}, open(f"{out}/meta.json", "w"))
    log("build done", stream)


# ---------------------------------------------------------------------------
# walk-forward
# ---------------------------------------------------------------------------
def load_ds(stream):
    """The model inputs X are memory-mapped (X_<sym>.npy, written once from the .npz) so the 15-minute
    datasets (about 4 GB of inputs) fit in memory; everything else is loaded."""
    out = f"{R}/ds/{stream}"
    meta = json.load(open(f"{out}/meta.json"))
    D = {}
    for sym in meta["symbols"]:
        key = sym.replace('/', '')
        z = np.load(f"{out}/{key}.npz")
        xp = f"{out}/X_{key}.npy"
        if not os.path.exists(xp):
            np.save(xp + ".tmp.npy", z["X"])
            os.replace(xp + ".tmp.npy", xp)
        D[sym] = {k: z[k] for k in z.files if k != "X"}
        D[sym]["X"] = np.load(xp, mmap_mode="r")
        D[sym]["ts"] = D[sym]["ts"].astype("datetime64[ns]")
        c = CFG[stream]
        for g, gv in c["geoms"].items():
            if f"yL_{g}" in D[sym]:
                continue
            k, H = gv[:2]
            lp = f"{out}/lab_{g}_{key}.npz"
            if os.path.exists(lp):
                z2 = np.load(lp)
                D[sym][f"yL_{g}"], D[sym][f"yS_{g}"] = z2["yL"], z2["yS"]
                continue
            d = D[sym]
            em = ST.weekend_exit_mask(pd.Series(d["ts"]), FE.TF_SEC[c["tf"]]) if c["market"] == "forex" else None
            yL, yS = tp1_labels(d["o"], d["h"], d["l"], d["c"], k * d["atr"], H, em, gv[2] if len(gv) > 2 else 0.5)
            np.savez(lp, yL=yL, yS=yS)
            D[sym][f"yL_{g}"], D[sym][f"yS_{g}"] = yL, yS
    return meta["cols"], D


_FLIP_CACHE = {}


def flip_np(X, cols):
    key = tuple(cols)
    if key not in _FLIP_CACHE:
        # learn the column transform from FE.flip on a probe frame (exact same rule as the site)
        rng = np.random.default_rng(1)
        P = pd.DataFrame(rng.uniform(0.1, 0.9, size=(4, len(cols))), columns=cols)
        G = FE.flip(P)
        ops = []
        for j, col in enumerate(cols):
            g = G[col].to_numpy()
            p = P.to_numpy()
            if np.allclose(g, p[:, j]):
                ops.append(("id", j))
            elif np.allclose(g, -p[:, j]):
                ops.append(("neg", j))
            elif np.allclose(g, 1 - p[:, j]):
                ops.append(("inv1", j))
            elif np.allclose(g, 100 - p[:, j]):
                ops.append(("inv100", j))
            else:
                src = [i for i in range(len(cols)) if np.allclose(g, p[:, i])]
                assert src, f"unknown flip rule for {col}"
                ops.append(("swap", src[0]))
        _FLIP_CACHE[key] = ops
    ops = _FLIP_CACHE[key]
    G = np.empty_like(X)
    for j, (op, s) in enumerate(ops):
        if op == "id":
            G[:, j] = X[:, j]
        elif op == "neg":
            G[:, j] = -X[:, j]
        elif op == "inv1":
            G[:, j] = 1 - X[:, j]
        elif op == "inv100":
            G[:, j] = 100 - X[:, j]
        else:
            G[:, j] = X[:, s]
    return G


def train_rows(D, cols, g, end, sub, seed=0):
    Xs, ys = [], []
    rng = np.random.default_rng(seed)
    for sym, d in D.items():
        m = (d["ts"] < np.datetime64(end)) & d["elig"]
        idx = np.nonzero(m)[0]
        if not len(idx):
            continue
        if sub > 1:
            off = rng.integers(0, sub)
            idx = idx[off::sub]
        X = d["X"][idx]
        for y, XX in ((d[f"yL_{g}"][idx], X), (d[f"yS_{g}"][idx], flip_np(X, cols))):
            ok = y >= 0
            Xs.append(XX[ok])
            ys.append(y[ok])
    return np.vstack(Xs), np.concatenate(ys)


def fit(X, y):
    from sklearn.ensemble import HistGradientBoostingClassifier
    clf = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=400,
                                         l2_regularization=1.0, max_bins=255, early_stopping=False, random_state=0)
    clf.fit(X, y)
    return clf


def predict(clf, D, cols, start, end):
    P = {}
    for sym, d in D.items():
        m = (d["ts"] >= np.datetime64(start)) & (d["ts"] < np.datetime64(end))
        idx = np.nonzero(m)[0]
        if not len(idx):
            continue
        X = d["X"][idx]
        pl = clf.predict_proba(X)[:, 1].astype(np.float32)
        ps = clf.predict_proba(flip_np(X, cols))[:, 1].astype(np.float32)
        P[sym] = (idx, pl, ps)
    return P


def folds(c):
    a, b = pd.Timestamp(c["dev"][0]), pd.Timestamp(c["dev"][1])
    out = []
    while a < b:
        e = min(a + pd.DateOffset(months=FOLD_MONTHS), b)
        out.append((a, e))
        a = e
    return out


def wf(stream, g):
    c = CFG[stream]
    cols, D = load_ds(stream)
    k, H = c["geoms"][g][:2]
    purge = pd.Timedelta(seconds=FE.TF_SEC[c["tf"]] * (H + 2))
    preds = {sym: (np.full(len(d["ts"]), np.nan, np.float32), np.full(len(d["ts"]), np.nan, np.float32))
             for sym, d in D.items()}
    for a, e in folds(c):
        t0 = time.time()
        X, y = train_rows(D, cols, g, a - purge, c["sub"])
        clf = fit(X, y)
        P = predict(clf, D, cols, a, e)
        for sym, (idx, pl, ps) in P.items():
            preds[sym][0][idx] = pl
            preds[sym][1][idx] = ps
        log(stream, g, "fold", a.date(), e.date(), "rows", len(y), "base", round(float(y.mean()), 3),
            f"{time.time() - t0:.0f}s")
        del X, y
    t0 = time.time()
    hs, he = pd.Timestamp(c["hold"][0]), pd.Timestamp(c["hold"][1])
    X, y = train_rows(D, cols, g, hs - purge, c["sub"])
    clf = fit(X, y)
    P = predict(clf, D, cols, hs, he)
    for sym, (idx, pl, ps) in P.items():
        preds[sym][0][idx] = pl
        preds[sym][1][idx] = ps
    log(stream, g, "final model rows", len(y), f"{time.time() - t0:.0f}s")
    os.makedirs(f"{R}/wf", exist_ok=True)
    np.savez(f"{R}/wf/{stream}_{g}.npz", **{f"{s.replace('/', '')}_L": v[0] for s, v in preds.items()},
             **{f"{s.replace('/', '')}_S": v[1] for s, v in preds.items()})
    pickle.dump({"clf": clf, "cols": cols, "train_end": str(hs - purge), "rows": int(len(y))},
                open(f"{R}/wf/{stream}_{g}_final.pkl", "wb"))
    log("wf done", stream, g)


# ---------------------------------------------------------------------------
# evaluation with the site's own selection policy
# ---------------------------------------------------------------------------
class _Cal:
    def __init__(self, x, y):
        self.x, self.y = np.asarray(x, float), np.asarray(y, float)

    def __call__(self, p):
        return np.interp(p, self.x, self.y)


def calibration(pl, yl, bins=40):
    """Isotonic map raw score -> share of cases that reached TP1 (equal-count bins)."""
    from sklearn.isotonic import IsotonicRegression
    ok = np.isfinite(pl) & (yl >= 0)
    p, y = pl[ok], yl[ok].astype(float)
    qs = np.quantile(p, np.linspace(0, 1, bins + 1))
    xb, yb, wb = [], [], []
    for a, b in zip(qs[:-1], qs[1:]):
        m = (p >= a) & (p <= b)
        if m.sum():
            xb.append(p[m].mean())
            yb.append(y[m].mean())
            wb.append(m.sum())
    iso = IsotonicRegression(out_of_bounds="clip").fit(xb, yb, sample_weight=wb)
    xs = np.array(xb)
    return _Cal(xs, iso.predict(xs))


def policy_data(D, preds, thr, grade_a, grade_b, k, cal, gate=None):
    data = {}
    for sym, d in D.items():
        key = sym.replace("/", "")
        pl, ps = preds[f"{key}_L"], preds[f"{key}_S"]
        best = np.fmax(pl, ps)
        longs = pl >= ps
        ok = np.isfinite(best) & (best >= thr) & np.isfinite(d["atr"]) & (d["atr"] > 0)
        if gate is not None:
            ok &= gate(sym, longs)
        side = np.where(ok, np.where(longs, "LONG", "SHORT"), "").astype(object)
        conf = np.where(longs, cal(pl), cal(ps)) * 100
        grade = np.where(best >= grade_a, "A", np.where(best >= grade_b, "B", "C")).astype(object)
        data[sym] = {"ts": d["ts"], "o": d["o"], "h": d["h"], "l": d["l"], "c": d["c"], "atr": d["atr"],
                     "side": side, "score": np.nan_to_num(best, nan=0.0), "conf": conf, "grade": grade, "setup": None,
                     "dist": k * d["atr"]}
    return data


def simulate(stream, g, D, preds, thr, start, end, cal, grade_a=9, grade_b=9, max_per_bar=None, cooldown=None, gate=None):
    c = CFG[stream]
    k, H = c["geoms"][g][:2]
    plan = {"targets_r": TARGETS_R, "fractions": FRACTIONS, "be_after": BE_AFTER, "max_bars": H,
            "tf_sec": FE.TF_SEC[c["tf"]]}
    policy = {"max_per_bar": c["max_per_bar"] if max_per_bar is None else max_per_bar,
              "cooldown": c["cooldown"] if cooldown is None else cooldown,
              "sessions": c.get("sessions"), "no_new_before_close_h": c.get("close_h", 0)}
    data = policy_data(D, preds, thr, grade_a, grade_b, k, cal, gate=gate)
    active, last, closed = ST.run_policy(data, c["market"], c["tf"], plan, policy, start_time=start, end_time=end,
                                         lookback=10 ** 9)
    return [t for lst in closed.values() for t in lst]


def stats(trades, start, end, nsym=None):
    days = (pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 86400
    n = len(trades)
    if not n:
        return {"signals": 0, "signals_per_day": 0.0, "win_rate": None, "avg_r": None}
    st = [t["status"] for t in trades]
    r = np.array([t["r"] for t in trades], float)
    wins = sum(1 for s in st if s.startswith("TP"))
    # drawdown of the account that takes every signal: results in the order the trades closed (the list itself is
    # grouped by symbol; before Oct 2026 the drawdown was computed in that order, which understated it)
    order = np.argsort([pd.Timestamp(t.get("closed_at") or t["signal_at"]).value for t in trades], kind="stable")
    eq = np.cumsum(r[order])
    dd = float(np.min(eq - np.maximum.accumulate(np.concatenate([[0], eq]))[1:])) if n else 0.0
    gp, gl = r[r > 0].sum(), -r[r < 0].sum()
    t_sorted = sorted(pd.Timestamp(t["signal_at"]) for t in trades)
    gaps = np.diff([pd.Timestamp(start)] + t_sorted + [pd.Timestamp(end)]) / pd.Timedelta(days=1)
    years = {}
    for t in trades:
        y = str(pd.Timestamp(t["signal_at"]).year)
        years.setdefault(y, []).append(t)
    out = {"period": f"{pd.Timestamp(start).date()} to {pd.Timestamp(end).date()}", "signals": n,
           "signals_per_day": round(n / days, 2), "win_rate": round(wins / n * 100, 1),
           "tp1_rate": round(sum(1 for t in trades if t.get("hits", 0) >= 1) / n * 100, 1),
           "tp2_rate": round(sum(1 for t in trades if t.get("hits", 0) >= 2) / n * 100, 1),
           "tp3_rate": round(sum(1 for t in trades if t.get("hits", 0) >= 3) / n * 100, 1),
           "stop_rate": round(sum(1 for s in st if s == "SL") / n * 100, 1),
           "expired_rate": round(sum(1 for s in st if s == "EXPIRED") / n * 100, 1),
           "avg_net_r": round(float(r.mean()), 3), "total_net_r": round(float(r.sum()), 1),
           "profit_factor": round(float(gp / gl), 2) if gl > 0 else None, "max_drawdown_r": round(dd, 1),
           "avg_hold_bars": round(float(np.mean([t["bars_held"] for t in trades])), 1),
           "max_gap_days": round(float(np.max(gaps)), 1),
           "by_grade": {}, "years": {}}
    for gname in ("A", "B", "C"):
        sel = [t for t in trades if t["grade"] == gname]
        if sel:
            rr = np.array([t["r"] for t in sel], float)
            out["by_grade"][gname] = {"signals": len(sel), "win_rate": round(
                sum(1 for t in sel if t["status"].startswith("TP")) / len(sel) * 100, 1), "avg_net_r": round(float(rr.mean()), 3)}
    for y, sel in sorted(years.items()):
        rr = np.array([t["r"] for t in sel], float)
        out["years"][y] = {"signals": len(sel), "win_rate": round(
            sum(1 for t in sel if t["status"].startswith("TP")) / len(sel) * 100, 1), "avg_net_r": round(float(rr.mean()), 3)}
    if nsym:
        out["symbols"] = nsym
    return out


def passes(s):
    """The validation rule a threshold (and gate) must meet to be used."""
    if s.get("win_rate") is None or s["signals"] < MIN_SIGNALS:
        return False
    if s["win_rate"] < TARGET_WIN or s["avg_net_r"] < MIN_AVG_R:
        return False
    for y, v in (s.get("years") or {}).items():
        if v["signals"] >= 20 and (v["win_rate"] < YEAR_MIN_WIN or v["avg_net_r"] <= 0):
            return False
    return True


def evaluate(stream, g, write=True):
    c = CFG[stream]
    cols, D = load_ds(stream)
    preds = dict(np.load(f"{R}/wf/{stream}_{g}.npz"))
    k, H = c["geoms"][g][:2]
    ds, de = c["dev"]
    hs, he = c["hold"]
    # validation predictions (walk-forward) for calibration + threshold
    vals, labs = [], []
    for sym, d in D.items():
        key = sym.replace("/", "")
        m = (d["ts"] >= np.datetime64(ds)) & (d["ts"] < np.datetime64(de)) & d["elig"]
        for side in ("L", "S"):
            vals.append(preds[f"{key}_{side}"][m])
            labs.append(d[f"y{side}_{g}"][m])
    pv, yv = np.concatenate(vals), np.concatenate(labs)
    cal = calibration(pv, yv)
    okv = np.isfinite(pv)
    qs = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.88, 0.9, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.985, 0.99, 0.995]
    grid = []
    for q in qs:
        thr = float(np.quantile(pv[okv], q))
        tr = simulate(stream, g, D, preds, thr, ds, de, cal)
        s = stats(tr, ds, de)
        s["q"], s["thr"] = q, thr
        grid.append(s)
        log(stream, g, f"q={q} thr={thr:.4f}", s["signals"], s["signals_per_day"], s["win_rate"], s["avg_net_r"])
    ok = [s for s in grid if passes(s)]
    if not ok:
        log(stream, g, "NO threshold reaches the target on validation")
        choice = None
    else:
        choice = min(ok, key=lambda s: s["thr"])     # most signals that still meet the target
    res = {"stream": stream, "geom": g, "sl_atr": k, "max_bars": H, "grid": grid, "choice": choice, "rule": RULE}
    if choice:
        thr = choice["thr"]
        # grades: A = validation win >= 80%, B >= 75% (raw score levels, validation only)
        ga = next((s["thr"] for s in sorted(grid, key=lambda s: s["thr"]) if s["thr"] >= thr and s["win_rate"]
                   and s["win_rate"] >= 80 and s["signals"] >= 20), float(np.quantile(pv[okv], 0.995)))
        gb = next((s["thr"] for s in sorted(grid, key=lambda s: s["thr"]) if s["thr"] >= thr and s["win_rate"]
                   and s["win_rate"] >= 75 and s["signals"] >= 20), (thr + ga) / 2)
        dev_tr = simulate(stream, g, D, preds, thr, ds, de, cal, ga, gb)
        hold_tr = simulate(stream, g, D, preds, thr, hs, he, cal, ga, gb)
        res.update({"threshold": thr, "grade_a": ga, "grade_b": gb,
                    "walk_forward": stats(dev_tr, ds, de, len(D)), "test": stats(hold_tr, hs, he, len(D)),
                    "calibration": {"x": cal.x.tolist(), "y": cal.y.tolist()},
                    "hold_bars_median": float(np.median([t["bars_held"] for t in dev_tr])) if dev_tr else None,
                    "watch": float(np.quantile(pv[okv], max(0.5, choice["q"] - 0.05)))})
        log(stream, g, "VALIDATION", json.dumps({k2: res["walk_forward"][k2] for k2 in ("signals", "signals_per_day", "win_rate", "avg_net_r", "profit_factor")}))
        log(stream, g, "HOLDOUT", json.dumps({k2: res["test"][k2] for k2 in ("signals", "signals_per_day", "win_rate", "avg_net_r", "profit_factor")}))
    if write:
        os.makedirs(f"{R}/results", exist_ok=True)
        json.dump(res, open(f"{R}/results/{stream}_{g}.json", "w"), indent=1, default=str)
    return res


# ---------------------------------------------------------------------------
# multi-timeframe gate: a signal needs the higher-timeframe model to agree
# ---------------------------------------------------------------------------
def htf_scores(stream, htf_stream, htf_g):
    """{symbol: (hL, hS)} raw scores of the higher-timeframe model (walk-forward / holdout predictions)
    of the last higher-timeframe candle that had closed when each candle of `stream` closed."""
    c, ch = CFG[stream], CFG[htf_stream]
    _, D = load_ds(stream)
    _, Dh = load_ds(htf_stream)
    Ph = dict(np.load(f"{R}/wf/{htf_stream}_{htf_g}.npz"))
    tf_sec = np.timedelta64(FE.TF_SEC[c["tf"]], "s")
    h_sec = np.timedelta64(FE.TF_SEC[ch["tf"]], "s")
    out = {}
    for sym, d in D.items():
        if sym not in Dh:
            continue
        key = sym.replace("/", "")
        close = d["ts"] + tf_sec
        hclose = Dh[sym]["ts"] + h_sec
        idx = np.searchsorted(hclose, close, side="right") - 1
        ok = idx >= 0
        ii = idx.clip(0)
        hL = np.where(ok, Ph[f"{key}_L"][ii], np.nan)
        hS = np.where(ok, Ph[f"{key}_S"][ii], np.nan)
        out[sym] = (hL.astype(np.float32), hS.astype(np.float32))
    return out


def make_gate(H, level):
    """The higher-timeframe model must lean the same way, with its score for that side >= level."""
    def gate(sym, longs):
        if sym not in H:
            return np.zeros(len(longs), dtype=bool)
        hL, hS = H[sym]
        side_score = np.where(longs, hL, hS)
        other = np.where(longs, hS, hL)
        return np.isfinite(side_score) & (side_score >= other) & (side_score >= level)
    return gate


def evaluate_mtf(stream, g, htf_stream, htf_g, write=True):
    """Like evaluate(), with the higher-timeframe gate; the gate level and the threshold are chosen on the
    validation years only (the same rule), then the holdout is scored once."""
    c = CFG[stream]
    cols, D = load_ds(stream)
    preds = dict(np.load(f"{R}/wf/{stream}_{g}.npz"))
    H = htf_scores(stream, htf_stream, htf_g)
    k, H_bars = c["geoms"][g][:2]
    ds, de = c["dev"]
    hs, he = c["hold"]
    vals, labs = [], []
    for sym, d in D.items():
        key = sym.replace("/", "")
        m = (d["ts"] >= np.datetime64(ds)) & (d["ts"] < np.datetime64(de)) & d["elig"]
        for side in ("L", "S"):
            vals.append(preds[f"{key}_{side}"][m])
            labs.append(d[f"y{side}_{g}"][m])
    pv, yv = np.concatenate(vals), np.concatenate(labs)
    cal = calibration(pv, yv)
    okv = np.isfinite(pv)
    parts = []                  # gate levels come from the validation years only (no holdout scores)
    for s in H:
        mv = (D[s]["ts"] >= np.datetime64(ds)) & (D[s]["ts"] < np.datetime64(de)) & D[s]["elig"]
        parts += [H[s][0][mv], H[s][1][mv]]
    hv = np.concatenate(parts)
    hv = hv[np.isfinite(hv)]
    levels = {"lean": 0.0, "h50": float(np.quantile(hv, 0.5)), "h70": float(np.quantile(hv, 0.7)),
              "h85": float(np.quantile(hv, 0.85))}
    qs = [0.5, 0.7, 0.8, 0.85, 0.9, 0.93, 0.95, 0.97, 0.98, 0.99]
    grid = []
    for lname, lev in levels.items():
        gate = make_gate(H, lev)
        for q in qs:
            thr = float(np.quantile(pv[okv], q))
            tr = simulate(stream, g, D, preds, thr, ds, de, cal, gate=gate)
            s = stats(tr, ds, de)
            s.update({"q": q, "thr": thr, "gate": lname, "gate_level": lev})
            grid.append(s)
            log(stream, g, "MTF", lname, f"q={q}", s["signals"], s["signals_per_day"], s["win_rate"], s["avg_net_r"])
    ok = [s for s in grid if passes(s)]
    res = {"stream": stream, "geom": g, "htf": f"{htf_stream}_{htf_g}", "grid": grid, "choice": None, "rule": RULE}
    if ok:
        choice = max(ok, key=lambda s: (s["signals"], s["avg_net_r"]))
        res["choice"] = choice
        gate = make_gate(H, choice["gate_level"])
        thr = choice["thr"]
        same = sorted([s for s in grid if s["gate"] == choice["gate"]], key=lambda s: s["thr"])
        # grades, as in evaluate(): A / B = the lowest score levels (same gate) whose validation win rate
        # reached 80% / 75% with at least 20 signals
        ga = next((s["thr"] for s in same if s["thr"] >= thr and s["win_rate"] and s["win_rate"] >= 80
                   and s["signals"] >= 20), float(np.quantile(pv[okv], 0.995)))
        gb = next((s["thr"] for s in same if s["thr"] >= thr and s["win_rate"] and s["win_rate"] >= 75
                   and s["signals"] >= 20), (thr + ga) / 2)
        dev_tr = simulate(stream, g, D, preds, thr, ds, de, cal, ga, gb, gate=gate)
        hold_tr = simulate(stream, g, D, preds, thr, hs, he, cal, ga, gb, gate=gate)
        res.update({"grade_a": ga, "grade_b": gb, "watch": float(np.quantile(pv[okv], max(0.5, choice["q"] - 0.05)))})
        res.update({"threshold": choice["thr"], "gate_level": choice["gate_level"], "gate": choice["gate"],
                    "walk_forward": stats(dev_tr, ds, de, len(D)), "test": stats(hold_tr, hs, he, len(D)),
                    "calibration": {"x": cal.x.tolist(), "y": cal.y.tolist()},
                    "hold_bars_median": float(np.median([t["bars_held"] for t in dev_tr])) if dev_tr else None})
        log(stream, g, "MTF VALIDATION", json.dumps({k2: res["walk_forward"][k2] for k2 in ("signals", "signals_per_day", "win_rate", "avg_net_r", "profit_factor")}))
        log(stream, g, "MTF HOLDOUT", json.dumps({k2: res["test"][k2] for k2 in ("signals", "signals_per_day", "win_rate", "avg_net_r", "profit_factor")}))
    else:
        log(stream, g, "MTF: no gate/threshold reaches the target on validation")
    if write:
        json.dump(res, open(f"{R}/results/{stream}_{g}_mtf.json", "w"), indent=1, default=str)
    return res


# ---------------------------------------------------------------------------
# export for the site
# ---------------------------------------------------------------------------
def export_trees(clf):
    trees = []
    for it in clf._predictors:
        nodes = it[0].nodes
        trees.append({"f": nodes["feature_idx"].astype(int).tolist(), "t": [float(x) for x in nodes["num_threshold"]],
                      "l": nodes["left"].astype(int).tolist(), "r": nodes["right"].astype(int).tolist(),
                      "leaf": nodes["is_leaf"].astype(int).tolist(), "v": [round(float(x), 7) for x in nodes["value"]],
                      "m": nodes["missing_go_to_left"].astype(int).tolist()})
    return trees, float(np.ravel(clf._baseline_prediction)[0])


HOLD_TEXT = {"15m": "a few hours", "1h": "about a day", "4h": "a few days"}


def export(stream, g):
    c = CFG[stream]
    res = json.load(open(f"{R}/results/{stream}_{g}.json"))
    fin = pickle.load(open(f"{R}/wf/{stream}_{g}_final.pkl", "rb"))
    clf, cols = fin["clf"], fin["cols"]
    trees, base = export_trees(clf)
    k, H = c["geoms"][g][:2]
    hb = res.get("hold_bars_median") or H / 4
    mins = hb * FE.TF_SEC[c["tf"]] / 60
    hold_text = f"about {int(round(mins))} minutes" if mins < 90 else (
        f"about {mins / 60:.0f} hours" if mins < 48 * 60 else f"about {mins / 1440:.1f} days")
    signal_symbols = markets.forex_signal_pairs() if c["market"] == "forex" else list(markets.CRYPTO_COINS)
    trained = json.load(open(f"{R}/ds/{stream}/meta.json"))["symbols"]
    model = {
        "version": "4.0", "stream": stream, "market": c["market"], "timeframe": c["tf"],
        "trained_on": f"{c['data_start']} to {fin['train_end'][:10]}, {len(trained)} symbols",
        "spec": {"session": c["session"], "volume": c["volume"], "anchor": c.get("anchor")},
        "features": cols, "baseline": base, "trees": trees, "calibration": res["calibration"],
        "calibration_note": "Maps the model output to the share of past cases that reached target 1 before the stop "
                            f"(walk-forward predictions {c['dev'][0]} to {c['dev'][1]}).",
        "threshold": res["threshold"], "grade_a": res["grade_a"], "grade_b": res["grade_b"],
        "watch_threshold": res.get("watch", res["threshold"]),
        "policy_note": ("A symbol gets a signal when the model's score is at least the threshold chosen on the "
                        f"validation years (the lowest level whose validation win rate was at least {TARGET_WIN:.0f}% "
                        "with a positive average result after costs). One trade at a time per symbol, "
                        f"a cooldown of {c['cooldown']} candles, at most {c['max_per_bar']} new signals per candle."),
        "levels": {"sl_atr": k, "targets_r": TARGETS_R, "fractions": FRACTIONS, "max_bars": H, "be_after": BE_AFTER},
        "selection": {"max_per_bar": c["max_per_bar"], "cooldown_bars": c["cooldown"],
                      "sessions": list(c["sessions"]) if c.get("sessions") else None,
                      "no_new_before_close_h": c.get("close_h", 0)},
        "hold_bars_median": hb, "hold_text": hold_text,
        "signal_symbols": signal_symbols, "trained_symbols": trained,
        "test": res["test"], "walk_forward": res["walk_forward"],
        "fees": "0.12% round trip" if c["market"] == "crypto" else "typical spread per pair (markets.FOREX_COST_PIPS)",
    }
    rb = f"{R}/results/{stream}_rules.json"
    if os.path.exists(rb):
        model["baseline_rules"] = json.load(open(rb)).get("test")
    os.makedirs(f"{APP}/models", exist_ok=True)
    path = f"{APP}/models/{stream}.json"
    json.dump(model, open(path, "w"), separators=(",", ":"))
    log("exported", path, os.path.getsize(path) // 1024, "KB")


# ---------------------------------------------------------------------------
# rules engine R1 baseline (the 'before' numbers for the new streams)
# ---------------------------------------------------------------------------
def rules(stream):
    c = CFG[stream]
    syms = crypto_symbols() if c["market"] == "crypto" else fx_symbols()
    hs, he = c["hold"]
    ds, de = c["dev"]
    data = {}
    htf = ST.HTF_OF[c["tf"]]
    for sym in syms:
        raw = load_crypto(sym, c["data_start"]) if c["market"] == "crypto" else load_fx(sym, c["data_start"])
        b = base_bars(raw, c["tf"])
        hb = FE.resample_closed(raw, htf)
        d, cand = ST.prepare(sym, b, hb, c["tf"])
        data[sym] = ST.rules_data(sym, d, cand)
    plan = {"targets_r": ST.TARGETS_R, "fractions": ST.FRACTIONS, "be_after": ST.BE_AFTER,
            "max_bars": ST.STREAMS[stream]["max_bars"], "tf_sec": FE.TF_SEC[c["tf"]]}
    policy = ST.policy_for(c["market"], c["tf"]) if not os.path.exists(ST._model_path(stream)) else {
        "max_per_bar": ST.MAX_PER_BAR, "cooldown": ST.STREAMS[stream]["cooldown"],
        "sessions": ST.STREAMS[stream].get("sessions"), "no_new_before_close_h": ST.STREAMS[stream].get("no_new_before_close_h", 0)}
    out = {}
    for name, (a, e) in (("walk_forward", (ds, de)), ("test", (hs, he))):
        _, _, closed = ST.run_policy(data, c["market"], c["tf"], plan, policy, start_time=a, end_time=e,
                                     lookback=10 ** 9, warmup=ST.WARMUP)
        out[name] = stats([t for lst in closed.values() for t in lst], a, e, len(data))
        log(stream, "R1", name, json.dumps({k: out[name][k] for k in ("signals", "signals_per_day", "win_rate", "avg_net_r")}))
    os.makedirs(f"{R}/results", exist_ok=True)
    json.dump(out, open(f"{R}/results/{stream}_rules.json", "w"), indent=1, default=str)


def export_mtf(stream, g):
    """models/<stream>.json from the multi-timeframe result (the stream model + its higher-timeframe gate)."""
    c = CFG[stream]
    res = json.load(open(f"{R}/results/{stream}_{g}_mtf.json"))
    assert res.get("choice"), "no validated choice"
    htf_stream, htf_g = res["htf"].rsplit("_", 1)
    fin = pickle.load(open(f"{R}/wf/{stream}_{g}_final.pkl", "rb"))
    hfin = pickle.load(open(f"{R}/wf/{htf_stream}_{htf_g}_final.pkl", "rb"))
    trees, base = export_trees(fin["clf"])
    htrees, hbase = export_trees(hfin["clf"])
    ch = CFG[htf_stream]
    k, H = c["geoms"][g][:2]
    hb = res.get("hold_bars_median") or H / 4
    mins = hb * FE.TF_SEC[c["tf"]] / 60
    hold_text = f"about {int(round(mins))} minutes" if mins < 90 else (
        f"about {mins / 60:.0f} hours" if mins < 48 * 60 else f"about {mins / 1440:.1f} days")
    trained = json.load(open(f"{R}/ds/{stream}/meta.json"))["symbols"]
    # grades from validation: A/B = the score levels whose validation win rate reached 80% / 75% (same gate)
    ga, gb = res["grade_a"], res["grade_b"]
    model = {
        "version": "4.0", "stream": stream, "market": c["market"], "timeframe": c["tf"],
        "trained_on": f"{c['data_start']} to {fin['train_end'][:10]}, {len(trained)} symbols",
        "spec": {"session": c["session"], "volume": c["volume"], "anchor": c.get("anchor")},
        "features": fin["cols"], "baseline": base, "trees": trees, "calibration": res["calibration"],
        "calibration_note": "Maps the model output to the share of past cases that reached target 1 before the stop "
                            f"(walk-forward predictions {c['dev'][0]} to {c['dev'][1]}).",
        "threshold": res["threshold"], "grade_a": max(ga, res["threshold"]), "grade_b": max(gb, res["threshold"]),
        "watch_threshold": min(res.get("watch", res["threshold"]), res["threshold"]),
        "gate": {"timeframe": ch["tf"], "features": hfin["cols"], "baseline": hbase, "trees": htrees,
                 "spec": {"session": ch["session"], "volume": ch["volume"], "anchor": ch.get("anchor")},
                 "level": res["gate_level"], "label": f"the {ch['tf']} model leans the same way"},
        "policy_note": (f"A symbol gets a signal when the {c['tf']} model's score is at least the threshold chosen on the "
                        f"validation years AND the {ch['tf']} model leans the same way (multi-timeframe alignment). "
                        f"One trade at a time per symbol, a cooldown of {c['cooldown']} candles, at most "
                        f"{c['max_per_bar']} new signals per candle."),
        "levels": {"sl_atr": k, "targets_r": TARGETS_R, "fractions": FRACTIONS, "max_bars": H, "be_after": BE_AFTER},
        "selection": {"max_per_bar": c["max_per_bar"], "cooldown_bars": c["cooldown"],
                      "sessions": list(c["sessions"]) if c.get("sessions") else None,
                      "no_new_before_close_h": c.get("close_h", 0)},
        "hold_bars_median": hb, "hold_text": hold_text,
        "signal_symbols": markets.forex_signal_pairs() if c["market"] == "forex" else list(markets.CRYPTO_COINS),
        "trained_symbols": trained, "test": res["test"], "walk_forward": res["walk_forward"],
        "fees": "0.12% round trip" if c["market"] == "crypto" else "typical spread per pair (markets.FOREX_COST_PIPS)",
    }
    rb = f"{R}/results/{stream}_rules.json"
    if os.path.exists(rb):
        model["baseline_rules"] = json.load(open(rb)).get("test")
    path = f"{APP}/models/{stream}.json"
    os.makedirs(f"{APP}/models", exist_ok=True)
    json.dump(model, open(path, "w"), separators=(",", ":"))
    log("exported", path, os.path.getsize(path) // 1024, "KB")


# ---------------------------------------------------------------------------
# which variant goes live (rule fixed before the new holdout results were seen)
# ---------------------------------------------------------------------------
# 1. Validation picks: among the variants whose walk-forward result passes the selection rule (passes()), the
#    multi-timeframe variant is preferred (the spec asks for 15m to agree with 1h and 1h with 4h), else the
#    plain one. 2. The holdout confirms: the picked variant must reach >= 70% wins (TP1 or better), a positive
#    average after costs and >= 50 signals on the holdout, or the stream stays off. No other variant is promoted
#    because of its holdout result.
VARIANTS = {
    "crypto_15m": [("s3", "15-minute model alone, stop 3 ATR"),
                   ("s3_mtf", "15-minute model with the 1-hour model agreeing, stop 3 ATR"),
                   ("s2", "15-minute model alone, stop 2 ATR"),
                   ("s2_mtf", "15-minute model with the 1-hour model agreeing, stop 2 ATR")],
    "crypto_1h": [("s2", "1-hour model alone"), ("s2_mtf", "1-hour model with the 4-hour model agreeing")],
    "forex_15m": [("s3", "15-minute model alone"), ("s3_mtf", "15-minute model with the 1-hour model agreeing")],
    "forex_1h": [("s2", "1-hour model alone, stop 2 ATR"), ("s3", "1-hour model alone, stop 3 ATR"),
                 ("s3_mtf", "1-hour model with the 4-hour model agreeing, stop 3 ATR")],
    "forex_4h": [("s2", "4-hour model, stop 2 ATR"), ("s15", "4-hour model, stop 1.5 ATR")],
}
HOLD_PASS_WIN, HOLD_PASS_N = 70.0, 50
_KEEP = ("period", "signals", "signals_per_day", "win_rate", "avg_net_r", "profit_factor", "stop_rate", "tp2_rate",
         "tp3_rate", "max_drawdown_r", "years")


def _short(st):
    return {k: st[k] for k in _KEEP if st and k in st} if st else None


def closest(r):
    """For a version that never reached the target: the validation level with the best average result
    (at least 100 signals), so the page can show how close it came."""
    rows = [x for x in (r.get("grid") or []) if (x.get("signals") or 0) >= 100 and x.get("win_rate") is not None]
    if not rows:
        return None
    x = max(rows, key=lambda s: s["avg_net_r"])
    return _short(x)


def hold_ok(t):
    return bool(t) and (t.get("signals") or 0) >= HOLD_PASS_N and (t.get("win_rate") or 0) >= HOLD_PASS_WIN \
        and (t.get("avg_net_r") or 0) > 0


def pick(stream):
    rows = []
    for v, label in VARIANTS.get(stream, []):
        f = f"{R}/results/{stream}_{v}.json"
        if not os.path.exists(f):
            continue
        r = json.load(open(f))
        ch = r.get("choice")
        val = r.get("walk_forward") if ch else None
        rows.append({"variant": v, "label": label, "mtf": v.endswith("_mtf"),
                     "passed_validation": bool(ch) and passes(r["walk_forward"]),
                     "validation_period": f"{CFG[stream]['dev'][0]} to {CFG[stream]['dev'][1]}",
                     "validation": _short(val), "holdout": _short(r.get("test")) if ch else None,
                     "closest": None if ch else closest(r),
                     "rule": r.get("rule") or "v1: validation win rate >= 72%, average >= 0.02R"})
    cands = [x for x in rows if x["passed_validation"]]
    chosen = None
    if cands:
        chosen = sorted(cands, key=lambda x: (not x["mtf"], -(x["validation"]["signals"])))[0]
    for x in rows:
        x["chosen"] = x is chosen
        x["passed"] = bool(x is chosen and hold_ok(x["holdout"]))
    live = bool(chosen and chosen["passed"])
    if live:
        h = chosen["holdout"]
        reason = (f"Live: {chosen['label']} - {h['win_rate']}% wins and {h['avg_net_r']:+}R on average after costs "
                  f"on data it never saw ({h['period']}).")
    elif chosen:
        h = chosen["holdout"] or {}
        reason = (f"The model picked on the validation years ({chosen['label']}) reached {h.get('win_rate')}% wins and "
                  f"{(h.get('avg_net_r') or 0):+}R on average on data it never saw ({h.get('period')}), so it is not used.")
    elif rows:
        seen = [x for x in rows if x["holdout"]]
        if seen:
            h = seen[0]["holdout"]
            reason = (f"No model version passed the validation rule. An earlier, looser version ({seen[0]['label']}) was tested on "
                      f"data it never saw ({h.get('period')}) and reached {h.get('win_rate')}% wins and "
                      f"{(h.get('avg_net_r') or 0):+}R on average, so it is not used.")
        else:
            reason = "No model version reached the target on the validation years, so none was tried on the unseen data."
    else:
        reason = "Not tested yet."
    return {"live": live, "chosen": chosen["variant"] if chosen else None, "reason": reason, "variants": rows}


def summary():
    out = {"generated": datetime.utcnow().strftime("%Y-%m-%d"),
           "rule": ("A stream goes live only when the model version picked on the validation years also reaches at "
                    f"least {HOLD_PASS_WIN:.0f}% wins (TP1 or better) and a positive average result after costs on later "
                    "data it never saw."),
           "streams": {}}
    for stream in VARIANTS:
        p = pick(stream)
        rb = f"{R}/results/{stream}_rules.json"
        if os.path.exists(rb):
            rr = json.load(open(rb))
            p["baseline_rules"] = {"walk_forward": _short(rr.get("walk_forward")), "test": _short(rr.get("test"))}
        out["streams"][stream] = p
        log(stream, "LIVE" if p["live"] else "OFF", p["chosen"], p["reason"])
    path = f"{APP}/models/research.json"
    os.makedirs(f"{APP}/models", exist_ok=True)
    json.dump(out, open(path, "w"), indent=1)
    log("wrote", path)
    return out


if __name__ == "__main__":
    cmd, stream = sys.argv[1], sys.argv[2]
    g = sys.argv[3] if len(sys.argv) > 3 else None
    {"build": lambda: build(stream), "wf": lambda: wf(stream, g), "eval": lambda: evaluate(stream, g),
     "mtf": lambda: evaluate_mtf(stream, g, sys.argv[4], sys.argv[5]),
     "export_mtf": lambda: export_mtf(stream, g),
     "export": lambda: export(stream, g), "rules": lambda: rules(stream),
     "pick": lambda: print(json.dumps(pick(stream), indent=1, default=str)), "summary": summary}[cmd]()
