"""
Engine 5 research lab: one pipeline for the six signal streams (crypto / forex x 15m / 1h / 4h).

  python lab5.py build <stream>     features, labels and entry slip per symbol            -> work/ds/<stream>/
  python lab5.py wf    <stream>     walk-forward models; out-of-sample scores for the validation years
                                    AND the holdout period (the holdout scores are only stored here)
  python lab5.py tune  <stream>     plan, score level and cost cap chosen on the VALIDATION years only
  python lab5.py final <stream>     the one holdout test of the tuned choice
  python lab5.py export <stream>    models/<stream>.json for the site (the model of the last walk-forward fold)

What is different from the earlier research (research/lab.py, engine 4):
  * the stop is wide enough for the costs: the trade plan of a stream is fixed BEFORE any model is trained so that
    a typical trade pays at most about 0.08R in fees, spread and slippage (15m: 6 ATR, 1h: 3 ATR, 4h: 2 ATR for
    crypto; 4 / 2.5 / 2 ATR for forex);
  * a candidate also needs its OWN cost to be small (cost cap in R, chosen on validation);
  * entries are not at the candle's close: the research enters at the price one minute (crypto) or two minutes
    (forex) into the next candle, with the stop and targets where the signal put them - what a follower gets;
  * crypto longs pay futures funding for the hours held;
  * the holdout is walk-forward too (the model is refitted every fold with data from before the fold only), so
    the model that goes live is the newest one and what is tested is the whole procedure.

Protocol: label = target 1 (0.5R) before the stop within H candles, pessimistic (a candle touching both = stop).
Folds: crypto validation 2023-01 .. 2025-09 in 6-month folds, holdout 2025-10 .. 2026-11 in two folds;
forex validation 2017-01 .. 2022-02 in yearly folds (MetaTrader candles), holdout 2023-04 .. 2026-10 in yearly
folds (HistData candles; a different data vendor than the training years). Every fold is trained only on data that
ended one label horizon before the fold starts.
"""
import glob
import json
import os
import pickle
import sys
import time
import warnings

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, APP)
import features as FE  # noqa: E402
import markets  # noqa: E402
import streams as ST  # noqa: E402

W = os.environ.get("V5_DIR") or os.path.join(HERE, "work")
PLANS = {
    "p3": {"targets_r": [0.5, 1.0, 2.0], "fractions": [1 / 3, 1 / 3, 1 / 3], "be_after": 1},
    "p2": {"targets_r": [0.5, 1.0], "fractions": [0.5, 0.5], "be_after": 1},
    "p1": {"targets_r": [0.5], "fractions": [1.0], "be_after": 0},
}
CRYPTO_VAL = ("2023-01-01", "2025-10-01")
CRYPTO_HOLD = [("2025-10-01", "2026-04-01"), ("2026-04-01", "2026-11-08")]
FX_VAL = ("2017-01-01", "2022-03-01")
FX_HOLD = [("2023-04-01", "2024-01-01"), ("2024-01-01", "2025-01-01"), ("2025-01-01", "2026-01-01"),
           ("2026-01-01", "2026-10-10")]
CFG = {
    "crypto_15m": dict(market="crypto", tf="15m", k=6.0, H=192, data_start="2020-09-01", val=CRYPTO_VAL, val_months=6,
                       hold=CRYPTO_HOLD, sub=10, session=True, volume=True, anchor="BTC/USDT", coins="main",
                       max_per_bar=3, cooldown=8),
    "crypto_1h": dict(market="crypto", tf="1h", k=3.0, H=96, data_start="2020-07-01", val=CRYPTO_VAL, val_months=6,
                      hold=CRYPTO_HOLD, sub=2, session=True, volume=True, anchor="BTC/USDT", coins="main",
                      max_per_bar=3, cooldown=4),
    "crypto_4h": dict(market="crypto", tf="4h", k=2.0, H=54, data_start="2020-07-01", val=CRYPTO_VAL, val_months=6,
                      hold=CRYPTO_HOLD, sub=1, session=False, volume=True, anchor="BTC/USDT", coins="all",
                      max_per_bar=5, cooldown=1),
    "forex_15m": dict(market="forex", tf="15m", k=4.0, H=128, data_start="2013-03-01", val=FX_VAL, val_months=12,
                      hold=FX_HOLD, sub=3, session=True, volume=False, anchor=None, sessions=("London", "New York"),
                      close_h=4, max_per_bar=2, cooldown=8),
    "forex_1h": dict(market="forex", tf="1h", k=2.5, H=72, data_start="2013-03-01", val=FX_VAL, val_months=12,
                     hold=FX_HOLD, sub=1, session=True, volume=False, anchor=None, sessions=("London", "New York"),
                     close_h=8, max_per_bar=2, cooldown=4),
    "forex_4h": dict(market="forex", tf="4h", k=2.0, H=36, data_start="2013-03-01", val=FX_VAL, val_months=12,
                     hold=FX_HOLD, sub=1, session=False, volume=False, anchor=None, sessions=None,
                     close_h=16, max_per_bar=2, cooldown=2),
}
# validation rule a setting must meet (fixed before any holdout score was looked at)
RULE = {"win": 73.0, "avg_r": 0.05, "pf": 1.15, "year_win": 68.0, "year_avg": 0.0, "min_signals": 150,
        "year_min_n": 30}
RULE_TEXT = ("validation: win rate >= 73%, average >= +0.05R after costs, profit factor >= 1.15, at least 150 signals, "
             "every validation year (30+ signals) >= 68% wins with a positive average, and the next stricter score "
             "level passes too; among the settings that pass, the one with the most signals")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------------------
# candles
# ---------------------------------------------------------------------------
def fx_name(pair):
    return pair[:3] + "/" + pair[3:]


def symbols(c):
    if c["market"] == "forex":
        return sorted({fx_name(os.path.basename(p)[:6]) for p in glob.glob(f"{W}/data/fx/*_[ab].parquet")})
    have = {os.path.basename(p)[:-8] + "/USDT" for p in glob.glob(f"{W}/data/crypto/*.parquet")}
    pool = markets.CRYPTO_COINS if c["coins"] == "all" else [x for x in markets.CRYPTO_COINS
                                                           if x not in markets.CRYPTO_4H_ONLY]
    return [s for s in pool if s in have]


def load15(c, sym):
    """15-minute candles of one symbol (UTC) with `fill`: the price a follower gets shortly after the candle opened."""
    if c["market"] == "crypto":
        d = pd.read_parquet(f"{W}/data/crypto/{sym.split('/')[0]}.parquet")
        d["fill"] = d["p1"]
    else:
        parts = []
        n = sym.replace("/", "")
        pa, pb = f"{W}/data/fx/{n}_a.parquet", f"{W}/data/fx/{n}_b.parquet"
        if os.path.exists(pa):
            a = pd.read_parquet(pa)
            a["fill"] = np.nan
            parts.append(a)
        if os.path.exists(pb):
            b = pd.read_parquet(pb)
            b["fill"] = b["p2"]
            parts.append(b)
        d = pd.concat(parts, ignore_index=True)
        d = d[ST.forex_open_mask(d["timestamp"])]
    d = d[["timestamp", "open", "high", "low", "close", "volume", "fill"]]
    d = d.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    return d[d["timestamp"] >= pd.Timestamp(c["data_start"]) - pd.Timedelta(days=260)].reset_index(drop=True)


def tf_bars(d15, tf):
    b = d15 if tf == "15m" else FE.resample_closed(d15, tf)
    b = b[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    # the fill of a signal on candle i = the first 15m candle after its close
    fill = d15.set_index("timestamp")["fill"]
    nxt = b["timestamp"] + pd.Timedelta(seconds=FE.TF_SEC[tf])
    b["slip"] = (fill.reindex(nxt).to_numpy() - b["close"].to_numpy())
    return b


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


def tp1_labels(h, l, c, dist, H, exit_mask=None, tpm=0.5, chunk=40000):
    """1 = target 1 (tpm x dist) reached before the stop (dist) within H candles; pessimistic: a candle that
    touches the stop counts as the stop. -1 = unknown (end of the data). Returns (long, short) int8."""
    from numpy.lib.stride_tricks import sliding_window_view as swv
    n = len(c)
    hp = np.concatenate([h, np.full(H, np.nan)])
    lp = np.concatenate([l, np.full(H, np.nan)])
    em = np.concatenate([exit_mask, np.zeros(H, dtype=bool)]) if exit_mask is not None else None
    out = [np.full(n, -1, np.int8), np.full(n, -1, np.int8)]
    for a in range(0, n, chunk):
        b = min(n, a + chunk)
        HH = swv(hp[a + 1:b + H], H)
        LL = swv(lp[a + 1:b + H], H)
        if em is not None:
            EM = swv(em[a + 1:b + H], H)
            cut = np.where(EM.any(axis=1), EM.argmax(axis=1), H - 1)
            valid = np.arange(H)[None, :] <= cut[:, None]
        else:
            valid = None
        cc, dd = c[a:b], dist[a:b]
        for j, sgn in enumerate((1, -1)):
            stop, tp = cc - sgn * dd, cc + sgn * tpm * dd
            if sgn > 0:
                hit_sl, hit_tp = LL <= stop[:, None], HH >= tp[:, None]
            else:
                hit_sl, hit_tp = HH >= stop[:, None], LL <= tp[:, None]
            if valid is not None:
                hit_sl, hit_tp = hit_sl & valid, hit_tp & valid
            fs = np.where(hit_sl.any(axis=1), hit_sl.argmax(axis=1), H + 1)
            ft = np.where(hit_tp.any(axis=1), hit_tp.argmax(axis=1), H + 1)
            y = (ft < fs).astype(np.int8)
            known = np.isfinite(HH).all(axis=1) | (np.minimum(fs, ft) <= H)
            y[~known] = -1
            y[~np.isfinite(dd) | (dd <= 0)] = -1
            out[j][a:b] = y
    return out[0], out[1]


def eligible(ts, c):
    """Candles where a new signal may start. Forex: market open, far enough from the weekly close, in the
    stream's sessions (spreads outside London / New York are much wider than the cost table)."""
    t = pd.to_datetime(pd.Series(ts)).reset_index(drop=True)
    ok = np.ones(len(t), dtype=bool)
    if c["market"] != "forex":
        return ok
    close = t + pd.Timedelta(seconds=FE.TF_SEC[c["tf"]])
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
    out = f"{W}/ds/{stream}"
    os.makedirs(out, exist_ok=True)
    syms = symbols(c)
    log(stream, "symbols", len(syms))
    small, done, anchor = {}, [], None
    keep_cols = ["timestamp", "close", "d_sma200", "h_d_sma50"] + FE._ZC
    for sym in syms:
        d15 = load15(c, sym)
        b = tf_bars(d15, c["tf"])
        del d15
        if len(b) < 1500:
            log("skip", sym, len(b))
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            F = FE.symbol_features(b, c["tf"], session=c["session"], volume=c["volume"])
        F["slip"] = b["slip"].to_numpy()
        F = F[F["timestamp"] >= pd.Timestamp(c["data_start"]) - pd.Timedelta(days=90)].reset_index(drop=True)
        for col in F.columns:
            if F[col].dtype == np.float64 and col not in ("open", "high", "low", "close", "atr", "slip"):
                F[col] = F[col].astype(np.float32)
        if c.get("anchor") == sym:
            anchor = FE.anchor_block(F)
        small[sym] = F[keep_cols].copy()
        F.to_pickle(f"{out}/F_{sym.replace('/', '')}.pkl")
        done.append(sym)
        log("features", sym, len(F), str(F["timestamp"].iloc[0])[:10], str(F["timestamp"].iloc[-1])[:10])
        del F, b
    mkt = cs = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if c["market"] == "crypto":
            mkt = FE.market_block(small)
        else:
            cs = FE.currency_block(small)
    del small
    cols = feature_list(c)
    sec = FE.TF_SEC[c["tf"]]
    for sym in done:
        fp = f"{out}/F_{sym.replace('/', '')}.pkl"
        F = pd.read_pickle(fp)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            X = FE.assemble(F, cols, sym=sym, anchor=anchor, mkt=mkt, cs=cs)
        ts = pd.to_datetime(F["timestamp"])
        keep = (ts >= pd.Timestamp(c["data_start"])).to_numpy()
        o, h, l, cl, atr = (F[k].to_numpy(float) for k in ("open", "high", "low", "close", "atr"))
        em = ST.weekend_exit_mask(ts, sec) if c["market"] == "forex" else None
        yL, yS = tp1_labels(h, l, cl, c["k"] * atr, c["H"], em)
        el = eligible(ts, c)
        key = sym.replace("/", "")
        np.save(f"{out}/X_{key}.npy", X.to_numpy(np.float32)[keep])
        np.savez(f"{out}/{key}.npz", ts=ts.to_numpy().astype("datetime64[ns]").astype("int64")[keep], o=o[keep],
                 h=h[keep], l=l[keep], c=cl[keep], atr=atr[keep], slip=F["slip"].to_numpy(float)[keep], elig=el[keep],
                 yL=yL[keep], yS=yS[keep])
        os.remove(fp)
        m = keep & el
        log("saved", sym, int(keep.sum()), "base long/short",
            round(float((yL[m] == 1).sum() / max((yL[m] >= 0).sum(), 1)), 3),
            round(float((yS[m] == 1).sum() / max((yS[m] >= 0).sum(), 1)), 3))
        del F, X
    json.dump({"cols": cols, "symbols": done}, open(f"{out}/meta.json", "w"))
    log("build done", stream)


def load_ds(stream):
    out = f"{W}/ds/{stream}"
    meta = json.load(open(f"{out}/meta.json"))
    D = {}
    for sym in meta["symbols"]:
        key = sym.replace("/", "")
        z = np.load(f"{out}/{key}.npz")
        D[sym] = {k: z[k] for k in z.files}
        D[sym]["X"] = np.load(f"{out}/X_{key}.npy", mmap_mode="r")
        D[sym]["ts"] = D[sym]["ts"].astype("datetime64[ns]")
    return meta["cols"], D


# ---------------------------------------------------------------------------
# walk-forward
# ---------------------------------------------------------------------------
_FLIP_CACHE = {}


def flip_np(X, cols):
    """FE.flip on a plain array (the column rule is learned once from FE.flip itself)."""
    key = tuple(cols)
    if key not in _FLIP_CACHE:
        rng = np.random.default_rng(1)
        P = pd.DataFrame(rng.uniform(0.1, 0.9, size=(4, len(cols))), columns=cols)
        G = FE.flip(P)
        ops = []
        p = P.to_numpy()
        for j, col in enumerate(cols):
            g = G[col].to_numpy()
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
    G = np.empty_like(X)
    for j, (op, s) in enumerate(_FLIP_CACHE[key]):
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


def train_rows(D, cols, end, sub, seed=0):
    Xs, ys = [], []
    rng = np.random.default_rng(seed)
    for sym, d in D.items():
        idx = np.nonzero((d["ts"] < np.datetime64(end)) & d["elig"])[0]
        if not len(idx):
            continue
        if sub > 1:
            idx = idx[rng.integers(0, sub)::sub]
        X = np.asarray(d["X"][idx])
        for y, XX in ((d["yL"][idx], X), (d["yS"][idx], flip_np(X, cols))):
            ok = y >= 0
            Xs.append(XX[ok])
            ys.append(y[ok])
    return np.vstack(Xs), np.concatenate(ys)


def fit(X, y):
    from sklearn.ensemble import HistGradientBoostingClassifier
    clf = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.05, max_leaf_nodes=31,
                                         min_samples_leaf=max(100, min(400, len(y) // 2000)), l2_regularization=1.0,
                                         max_bins=255, early_stopping=False, random_state=0)
    clf.fit(X, y)
    return clf


def folds(c):
    out = []
    a, b = pd.Timestamp(c["val"][0]), pd.Timestamp(c["val"][1])
    while a < b:
        e = min(a + pd.DateOffset(months=c["val_months"]), b)
        if (b - e) < pd.Timedelta(days=45):       # no tiny last fold
            e = b
        out.append(("val", a, e))
        a = e
    for a, e in c["hold"]:
        out.append(("hold", pd.Timestamp(a), pd.Timestamp(e)))
    return out


def wf(stream):
    c = CFG[stream]
    cols, D = load_ds(stream)
    purge = pd.Timedelta(seconds=FE.TF_SEC[c["tf"]] * (c["H"] + 2))
    preds = {s: (np.full(len(d["ts"]), np.nan, np.float32), np.full(len(d["ts"]), np.nan, np.float32))
             for s, d in D.items()}
    os.makedirs(f"{W}/wf", exist_ok=True)
    info = []
    for kind, a, e in folds(c):
        t0 = time.time()
        X, y = train_rows(D, cols, a - purge, c["sub"])
        clf = fit(X, y)
        for sym, d in D.items():
            idx = np.nonzero((d["ts"] >= np.datetime64(a)) & (d["ts"] < np.datetime64(e)))[0]
            if not len(idx):
                continue
            Xp = np.asarray(d["X"][idx])
            preds[sym][0][idx] = clf.predict_proba(Xp)[:, 1]
            preds[sym][1][idx] = clf.predict_proba(flip_np(Xp, cols))[:, 1]
        info.append({"kind": kind, "start": str(a.date()), "end": str(e.date()), "train_rows": int(len(y)),
                     "train_end": str((a - purge).date()), "base": round(float(y.mean()), 4)})
        log(stream, kind, a.date(), e.date(), "rows", len(y), "base", round(float(y.mean()), 3),
            f"{time.time() - t0:.0f}s")
        pickle.dump({"clf": clf, "cols": cols, "train_end": str(a - purge), "rows": int(len(y)), "fold": str(a.date())},
                    open(f"{W}/wf/{stream}_last.pkl", "wb"))
        del X, y
    np.savez(f"{W}/wf/{stream}.npz", **{f"{s.replace('/', '')}_L": v[0] for s, v in preds.items()},
             **{f"{s.replace('/', '')}_S": v[1] for s, v in preds.items()})
    json.dump(info, open(f"{W}/wf/{stream}_folds.json", "w"), indent=1)
    log("wf done", stream)


# ---------------------------------------------------------------------------
# simulation with the site's own policy and trade follower
# ---------------------------------------------------------------------------
def calibration(p, y, bins=40):
    from sklearn.isotonic import IsotonicRegression
    ok = np.isfinite(p) & (y >= 0)
    p, y = p[ok], y[ok].astype(float)
    qs = np.unique(np.quantile(p, np.linspace(0, 1, bins + 1)))
    xb, yb, wb = [], [], []
    for a, b in zip(qs[:-1], qs[1:]):
        m = (p >= a) & (p <= b)
        if m.sum():
            xb.append(p[m].mean())
            yb.append(y[m].mean())
            wb.append(m.sum())
    iso = IsotonicRegression(out_of_bounds="clip").fit(xb, yb, sample_weight=wb)
    xs = np.array(xb)
    return xs, iso.predict(xs)


def cost_array(sym, c, close, dist):
    """Round-trip cost in R of a trade entered at each candle (funding not included: it depends on the hold)."""
    if c["market"] == "forex":
        return markets.cost_price(sym) / dist
    return markets.CRYPTO_COST_ROUND_TRIP * close / dist


def policy_data(c, D, preds, thr, cal, cost_cap=None, grade_a=9.0, grade_b=9.0, gate=None):
    data = {}
    for sym, d in D.items():
        key = sym.replace("/", "")
        pl, ps = preds[f"{key}_L"], preds[f"{key}_S"]
        best = np.fmax(pl, ps)
        longs = pl >= ps
        dist = c["k"] * d["atr"]
        ok = np.isfinite(best) & (best >= thr) & np.isfinite(d["atr"]) & (d["atr"] > 0) & d["elig"]
        if cost_cap is not None:
            with np.errstate(all="ignore"):
                ok &= cost_array(sym, c, d["c"], dist) <= cost_cap
        if gate is not None:
            ok &= gate(sym, longs)
        side = np.where(ok, np.where(longs, "LONG", "SHORT"), "").astype(object)
        conf = np.interp(np.where(longs, pl, ps), cal[0], cal[1]) * 100
        grade = np.where(best >= grade_a, "A", np.where(best >= grade_b, "B", "C")).astype(object)
        data[sym] = {"ts": d["ts"], "o": d["o"], "h": d["h"], "l": d["l"], "c": d["c"], "atr": d["atr"],
                     "side": side, "score": np.nan_to_num(best, nan=0.0), "conf": conf, "grade": grade, "setup": None,
                     "dist": dist, "slip": d["slip"]}
    return data


def simulate(c, D, preds, thr, start, end, cal, plan="p3", cost_cap=None, grade_a=9.0, grade_b=9.0, gate=None,
             max_per_bar=None, cooldown=None):
    p = dict(PLANS[plan], max_bars=c["H"], tf_sec=FE.TF_SEC[c["tf"]])
    policy = {"max_per_bar": c["max_per_bar"] if max_per_bar is None else max_per_bar,
              "cooldown": c["cooldown"] if cooldown is None else cooldown,
              "sessions": None, "no_new_before_close_h": 0}        # forex time rules are in d["elig"] already
    data = policy_data(c, D, preds, thr, cal, cost_cap, grade_a, grade_b, gate)
    _, _, closed = ST.run_policy(data, c["market"], c["tf"], p, policy, start_time=start, end_time=end,
                                 lookback=10 ** 9)
    return [t for lst in closed.values() for t in lst]


def stats(trades, start, end):
    days = (pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 86400
    n = len(trades)
    if not n:
        return {"signals": 0, "signals_per_day": 0.0, "win_rate": None, "avg_net_r": None}
    st = [t["status"] for t in trades]
    r = np.array([t["r"] for t in trades], float)
    wins = sum(1 for s in st if s.startswith("TP"))
    order = np.argsort([pd.Timestamp(t.get("closed_at") or t["signal_at"]).value for t in trades], kind="stable")
    eq = np.cumsum(r[order])
    dd = float(np.min(eq - np.maximum.accumulate(np.concatenate([[0], eq]))[1:]))
    gp, gl = r[r > 0].sum(), -r[r < 0].sum()
    t_sorted = sorted(pd.Timestamp(t["signal_at"]) for t in trades)
    gaps = np.diff([pd.Timestamp(start)] + t_sorted + [pd.Timestamp(end)]) / pd.Timedelta(days=1)
    day_set = {t.normalize() for t in t_sorted}
    stop_pct = np.array([abs(t["entry"] - t["stop_loss"]) / t["entry"] * 100 for t in trades])
    out = {"period": f"{pd.Timestamp(start).date()} to {pd.Timestamp(end).date()}", "signals": n,
           "signals_per_day": round(n / days, 2), "days_with_signal_pct": round(len(day_set) / days * 100, 1),
           "win_rate": round(wins / n * 100, 1),
           "tp1_rate": round(sum(1 for t in trades if t.get("hits", 0) >= 1) / n * 100, 1),
           "tp2_rate": round(sum(1 for t in trades if t.get("hits", 0) >= 2) / n * 100, 1),
           "tp3_rate": round(sum(1 for t in trades if t.get("hits", 0) >= 3) / n * 100, 1),
           "stop_rate": round(sum(1 for s in st if s == "SL") / n * 100, 1),
           "expired_rate": round(sum(1 for s in st if s == "EXPIRED") / n * 100, 1),
           "money_making_rate": round(float((r > 0).mean()) * 100, 1),
           "avg_net_r": round(float(r.mean()), 3), "total_net_r": round(float(r.sum()), 1),
           "profit_factor": round(float(gp / gl), 2) if gl > 0 else None, "max_drawdown_r": round(dd, 1),
           "avg_hold_bars": round(float(np.mean([t["bars_held"] for t in trades])), 1),
           "median_hold_bars": float(np.median([t["bars_held"] for t in trades])),
           "max_gap_days": round(float(np.max(gaps)), 1), "avg_stop_pct": round(float(stop_pct.mean()), 2),
           "long_share": round(sum(1 for t in trades if t["side"] == "LONG") / n * 100, 1),
           "by_grade": {}, "years": {}}
    for g in ("A", "B", "C"):
        sel = [t for t in trades if t["grade"] == g]
        if sel:
            rr = np.array([t["r"] for t in sel], float)
            out["by_grade"][g] = {"signals": len(sel), "avg_net_r": round(float(rr.mean()), 3), "win_rate": round(
                sum(1 for t in sel if t["status"].startswith("TP")) / len(sel) * 100, 1)}
    years = {}
    for t in trades:
        years.setdefault(str(pd.Timestamp(t["signal_at"]).year), []).append(t)
    for y, sel in sorted(years.items()):
        rr = np.array([t["r"] for t in sel], float)
        out["years"][y] = {"signals": len(sel), "avg_net_r": round(float(rr.mean()), 3), "win_rate": round(
            sum(1 for t in sel if t["status"].startswith("TP")) / len(sel) * 100, 1)}
    return out


def passes(s):
    if s.get("win_rate") is None or s["signals"] < RULE["min_signals"]:
        return False
    if s["win_rate"] < RULE["win"] or s["avg_net_r"] < RULE["avg_r"] or (s.get("profit_factor") or 0) < RULE["pf"]:
        return False
    for v in (s.get("years") or {}).values():
        if v["signals"] >= RULE["year_min_n"] and (v["win_rate"] < RULE["year_win"] or v["avg_net_r"] <= RULE["year_avg"]):
            return False
    return True


QS = [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.93, 0.95, 0.97, 0.98, 0.99, 0.995]


def val_scores(c, D, preds):
    vals, labs = [], []
    a, e = np.datetime64(c["val"][0]), np.datetime64(c["val"][1])
    for sym, d in D.items():
        key = sym.replace("/", "")
        m = (d["ts"] >= a) & (d["ts"] < e) & d["elig"]
        for side in ("L", "S"):
            vals.append(preds[f"{key}_{side}"][m])
            labs.append(d[f"y{side}"][m])
    return np.concatenate(vals), np.concatenate(labs)


def short(s):
    keys = ("signals", "signals_per_day", "win_rate", "avg_net_r", "profit_factor", "max_drawdown_r")
    return {k: s.get(k) for k in keys}


def tune(stream, gate=None, tag=""):
    c = CFG[stream]
    cols, D = load_ds(stream)
    preds = dict(np.load(f"{W}/wf/{stream}.npz"))
    pv, yv = val_scores(c, D, preds)
    cal = calibration(pv, yv)
    okv = np.isfinite(pv)
    ds, de = c["val"]
    caps = [None, 0.12, 0.09, 0.07] if c["market"] == "crypto" else [None, 0.10, 0.07]
    grid = []
    for plan in PLANS:
        for cap in caps:
            for q in QS:
                thr = float(np.quantile(pv[okv], q))
                s = stats(simulate(c, D, preds, thr, ds, de, cal, plan, cap, gate=gate), ds, de)
                s.update({"plan": plan, "cap": cap, "q": q, "thr": thr})
                grid.append(s)
                log(stream, tag, plan, "cap", cap, f"q={q}", s["signals"], s["signals_per_day"], s["win_rate"],
                    s["avg_net_r"], s.get("profit_factor"), {y: (v["win_rate"], v["avg_net_r"]) for y, v in (s.get("years") or {}).items()})
    ok = []
    for s in grid:
        if not passes(s):
            continue
        stricter = [x for x in grid if x["plan"] == s["plan"] and x["cap"] == s["cap"] and x["q"] > s["q"]]
        nxt = min(stricter, key=lambda x: x["q"]) if stricter else None
        if nxt is not None and not (nxt["signals"] >= 50 and (nxt["win_rate"] or 0) >= RULE["win"] - 1
                                    and (nxt["avg_net_r"] or 0) >= RULE["avg_r"] / 2):
            continue
        ok.append(s)
    res = {"stream": stream, "tag": tag, "k": c["k"], "H": c["H"], "grid": grid, "rule": RULE_TEXT, "choice": None,
           "calibration": {"x": [float(x) for x in cal[0]], "y": [float(x) for x in cal[1]]}}
    if ok:
        ch = max(ok, key=lambda s: (s["signals"], s["avg_net_r"]))
        res["choice"] = ch
        same = sorted([x for x in grid if x["plan"] == ch["plan"] and x["cap"] == ch["cap"]], key=lambda x: x["thr"])
        ga = next((x["thr"] for x in same if x["thr"] > ch["thr"] and (x["win_rate"] or 0) >= 80 and x["signals"] >= 30),
                  float(np.quantile(pv[okv], 0.999)))
        gb = next((x["thr"] for x in same if x["thr"] > ch["thr"] and (x["win_rate"] or 0) >= 76 and x["signals"] >= 30),
                  (ch["thr"] + ga) / 2)
        gb = min(gb, ga)
        tr = simulate(c, D, preds, ch["thr"], ds, de, cal, ch["plan"], ch["cap"], ga, gb, gate=gate)
        res.update({"threshold": ch["thr"], "grade_a": ga, "grade_b": gb, "plan": ch["plan"], "cap": ch["cap"],
                    "watch": float(np.quantile(pv[okv], max(0.5, ch["q"] - 0.05))),
                    "walk_forward": stats(tr, ds, de),
                    "hold_bars_median": float(np.median([t["bars_held"] for t in tr]))})
        log(stream, tag, "CHOICE", ch["plan"], "cap", ch["cap"], "q", ch["q"], json.dumps(short(res["walk_forward"])))
    else:
        best = [s for s in grid if s["signals"] >= RULE["min_signals"] and s["win_rate"] is not None]
        if best:
            b = max(best, key=lambda s: s["avg_net_r"])
            res["closest"] = b
            log(stream, tag, "NO setting passes validation; closest:", b["plan"], b["cap"], b["q"], json.dumps(short(b)))
    os.makedirs(f"{W}/results", exist_ok=True)
    json.dump(res, open(f"{W}/results/{stream}{tag}.json", "w"), indent=1, default=str)
    return res


def final(stream, gate=None, tag=""):
    """The holdout test of the tuned choice - run once per stream."""
    c = CFG[stream]
    path = f"{W}/results/{stream}{tag}.json"
    res = json.load(open(path))
    if not res.get("choice"):
        log(stream, "no validated choice: the holdout is not opened")
        return res
    if res.get("test"):
        log(stream, "holdout already scored:", json.dumps(short(res["test"])))
        return res
    cols, D = load_ds(stream)
    preds = dict(np.load(f"{W}/wf/{stream}.npz"))
    cal = (np.array(res["calibration"]["x"]), np.array(res["calibration"]["y"]))
    hs, he = c["hold"][0][0], c["hold"][-1][1]
    tr = simulate(c, D, preds, res["threshold"], hs, he, cal, res["plan"], res["cap"], res["grade_a"], res["grade_b"],
                  gate=gate)
    res["test"] = stats(tr, hs, he)
    res["test"]["folds"] = {}
    for a, e in c["hold"]:
        sel = [t for t in tr if pd.Timestamp(a) <= pd.Timestamp(t["signal_at"]) < pd.Timestamp(e)]
        res["test"]["folds"][f"{a} to {e}"] = short(stats(sel, a, e))
    res["test"]["symbols"] = len(D)
    log(stream, tag, "HOLDOUT", json.dumps(short(res["test"])), json.dumps(res["test"]["years"]))
    json.dump(res, open(path, "w"), indent=1, default=str)
    pickle.dump(tr, open(f"{W}/results/{stream}{tag}_holdout_trades.pkl", "wb"))
    return res


if __name__ == "__main__":
    cmd, stream = sys.argv[1], sys.argv[2]
    {"build": build, "wf": wf, "tune": tune, "final": final}[cmd](stream)
