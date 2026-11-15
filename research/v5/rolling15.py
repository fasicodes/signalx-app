"""
A 15-minute stream from the engine-5 4-hour model: the 4h model read at every 15-minute close.

The 1-hour stream (rolling.py) reads the 4h model of lab5.py (crypto_4h) on 4-hour candles that end at every full
hour and passed its one holdout test. Here the SAME walk-forward fold models read 4-hour candles that end at EVERY
15-minute close: sixteen series per coin (the usual grid and fifteen shifted by 15, 30, ... 225 minutes), so the
stream can give a signal at any 15-minute close.

Fixed before any number of this stream was looked at (15 Nov 2026), nothing is tuned here:
  * score level, grade level, plan (one target at 0.5R, stop 2 ATR of the 4-hour candles, 9 days = 864 candles of
    15 minutes) and calibration: the 4h model's validated choice (work/results/crypto_4h_choice.json), as in rolling.py;
  * selection: lab5's own policy for a crypto 15-minute stream (CFG["crypto_15m"]: at most 3 new signals per
    15-minute candle, a cooldown of 8 candles per coin), fixed in lab5 before any model was trained;
  * entry one minute after the signal candle closed (the next candle's p1), 0.30% round trip, funding for longs.
Pass rules: validation (2023-01 .. 2025-09) must meet lab5's validation rule (L.passes); only then the holdout
(2025-10-01 .. 2026-11-08) is scored, once, and the stream may go live only if streams.passed_test() accepts it and
it meets the owner's bar too (win rate of at least 70% and a positive average after costs).

The holdout period was already used once for a 15-minute stream (the separate 15-minute model, which failed), so
this is the second 15-minute variant scored on it; models/research.json must say so. The 4h model it reads, and the
1-hour reading of it, were tested on the same period and passed.

  python rolling15.py build    shifted 4-hour features and the fold models' scores  -> work/rolling15/
  python rolling15.py val      validation years only: must pass lab5's validation rule
  python rolling15.py final    the one holdout test; refuses when validation did not pass; also how many of its
                               signals the 1-hour stream gives too (same coin and side within 4 hours)
"""
import json
import os
import pickle
import shutil
import sys
import warnings

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lab5 as L  # noqa: E402
from lab5 import FE, ST, W, log  # noqa: E402

SRC = "crypto_4h"                          # the model whose folds are read every 15 minutes
OUT = f"{W}/rolling15"
OFFSETS = tuple(range(0, 240, 15))         # minutes the 4-hour grid is shifted by
MAX_PER_BAR = L.CFG["crypto_15m"]["max_per_bar"]
COOLDOWN = L.CFG["crypto_15m"]["cooldown"]
C = L.CFG[SRC]
H15 = C["H"] * 16                          # the 4h model's time limit, in 15-minute candles
RESULTS = f"{W}/results/crypto_15m_rolling.json"


def build():
    os.makedirs(OUT, exist_ok=True)
    syms = L.symbols(C)
    cols = L.feature_list(C)
    folds = sorted(f for f in os.listdir(f"{W}/wf") if f.startswith(f"{SRC}_fold_"))
    models = [pickle.load(open(f"{W}/wf/{f}", "rb")) for f in folds]
    assert models and models[0]["cols"] == cols
    keep_cols = ["timestamp", "close", "d_sma200", "h_d_sma50"] + FE._ZC
    d15s = {}
    for sym in syms:
        try:
            d15s[sym] = L.load15(C, sym)
        except FileNotFoundError:
            log("no data", sym)
    for off in OFFSETS:
        frames, small, anchor = {}, {}, None
        for sym, d15 in d15s.items():
            b = ST.shifted_candles(d15, "4h", off * 60)
            if len(b) < 1500:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                F = FE.symbol_features(b, "4h", session=C["session"], volume=C["volume"])
            F = F[F["timestamp"] >= pd.Timestamp(C["val"][0]) - pd.Timedelta(days=5)].reset_index(drop=True)
            frames[sym] = F
            small[sym] = F[keep_cols]
            if sym == C["anchor"]:
                anchor = FE.anchor_block(F)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            mkt = FE.market_block(small)
            for sym, F in frames.items():
                X = FE.assemble(F, cols, sym=sym, anchor=anchor, mkt=mkt).to_numpy(np.float32)
                ts = pd.to_datetime(F["timestamp"]).to_numpy().astype("datetime64[ns]")
                pl, ps = np.full(len(ts), np.nan, np.float32), np.full(len(ts), np.nan, np.float32)
                for m in models:
                    idx = np.nonzero((ts >= np.datetime64(m["fold"])) & (ts < np.datetime64(m["fold_end"])))[0]
                    if len(idx):
                        pl[idx] = m["clf"].predict_proba(X[idx])[:, 1]
                        ps[idx] = m["clf"].predict_proba(L.flip_np(X[idx], cols))[:, 1]
                np.savez(f"{OUT}/{sym.replace('/', '')}_{off}.npz", ts=ts.astype("int64"), pl=pl, ps=ps,
                         atr=F["atr"].to_numpy(float), close=F["close"].to_numpy(float))
        log("offset (min)", off, "symbols", len(frames))
    json.dump({"symbols": syms, "cols": cols, "folds": folds, "offsets": OFFSETS}, open(f"{OUT}/meta.json", "w"))
    # the scores on the usual grid must be the 4h model's own walk-forward scores
    ref = dict(np.load(f"{W}/wf/{SRC}.npz"))
    _, D = L.load_ds(SRC)
    z = np.load(f"{OUT}/BTCUSDT_0.npz")
    d = D["BTC/USDT"]
    common, ia, ib = np.intersect1d(z["ts"], d["ts"].astype("int64"), return_indices=True)
    a, b = z["pl"][ia], ref["BTCUSDT_L"][ib]
    ok = np.isfinite(a) & np.isfinite(b)
    log("parity with the 4h walk-forward scores (BTC, usual grid): rows", int(ok.sum()), "max diff",
        float(np.abs(a[ok] - b[ok]).max()))


def quarter_data(thr, grade_a, cal):
    """Per coin, on 15-minute candles: the candidate (side, score, stop distance) of the 4-hour candle that ends with
    each 15-minute candle."""
    meta = json.load(open(f"{OUT}/meta.json"))
    data = {}
    q = np.timedelta64(15, "m")
    for sym in meta["symbols"]:
        key = sym.replace("/", "")
        if not os.path.exists(f"{OUT}/{key}_0.npz"):
            continue
        d15 = L.load15(C, sym)
        d15 = d15[d15["timestamp"] >= pd.Timestamp(C["val"][0]) - pd.Timedelta(days=2)].reset_index(drop=True)
        ts = pd.to_datetime(d15["timestamp"]).to_numpy().astype("datetime64[ns]")
        n = len(ts)
        close = d15["close"].to_numpy(float)
        pl, ps, atr, off = (np.full(n, np.nan) for _ in range(4))
        for o in OFFSETS:
            z = np.load(f"{OUT}/{key}_{o}.npz")
            last = z["ts"].astype("datetime64[ns]") + np.timedelta64(4, "h") - q     # the 15m candle that ends the bar
            pos = np.searchsorted(ts, last)
            ok = (pos < n) & (ts[np.minimum(pos, n - 1)] == last)
            # the 4-hour candle and its last 15-minute candle close at the same price
            ok &= np.isclose(z["close"], close[np.minimum(pos, n - 1)], rtol=1e-9, atol=0)
            i = pos[ok]
            pl[i], ps[i], atr[i], off[i] = z["pl"][ok], z["ps"][ok], z["atr"][ok], o
        best = np.fmax(pl, ps)
        longs = pl >= ps
        okc = np.isfinite(best) & (best >= thr) & np.isfinite(atr) & (atr > 0)
        fill = d15.set_index("timestamp")["fill"]
        slip = fill.reindex(d15["timestamp"] + pd.Timedelta(minutes=15)).to_numpy() - close
        data[sym] = {"ts": ts, "o": d15["open"].to_numpy(float), "h": d15["high"].to_numpy(float),
                     "l": d15["low"].to_numpy(float), "c": close, "atr": atr,
                     "side": np.where(okc, np.where(longs, "LONG", "SHORT"), "").astype(object),
                     "score": np.nan_to_num(best, nan=0.0),
                     "conf": np.interp(np.where(longs, pl, ps), cal[0], cal[1]) * 100,
                     "grade": np.where(best >= grade_a, "A", "B").astype(object), "setup": None,
                     "dist": C["k"] * atr, "slip": slip, "off": off}
    return data


def run(start, end):
    ch = json.load(open(f"{W}/results/{SRC}_choice.json"))
    cal = (np.array(ch["calibration"]["x"]), np.array(ch["calibration"]["y"]))
    data = quarter_data(ch["threshold"], ch["grade_a"], cal)
    plan = dict(L.PLANS[ch["plan"]], max_bars=H15, tf_sec=900)
    policy = {"max_per_bar": MAX_PER_BAR, "cooldown": COOLDOWN, "sessions": None, "no_new_before_close_h": 0}
    _, _, closed = ST.run_policy(data, "crypto", "15m", plan, policy, start_time=start, end_time=end, lookback=10 ** 9)
    trades = []
    for sym, lst in closed.items():
        ts, off = data[sym]["ts"], data[sym]["off"]
        for t in lst:
            t["offset"] = int(off[int(np.searchsorted(ts, np.datetime64(t["bar_time"])))])
            t["symbol"] = sym
            trades.append(t)
    return trades, len(data), ch


def by_minute(trades, start, end):
    """Results by the minute of the hour the signal came at (:00 = the hourly readings the 1-hour stream also has)."""
    out = {}
    for mm in (0, 15, 30, 45):
        sel = [t for t in trades if (pd.Timestamp(t["signal_at"]).minute == mm)]
        if sel:
            s = L.stats(sel, start, end)
            out[f":{mm:02d}"] = {k: s[k] for k in ("signals", "win_rate", "avg_net_r", "profit_factor")}
    return out


def owner_bar(s):
    """The owner's bar for a live 15-minute stream: at least 70% wins and a positive average after costs."""
    return bool(s.get("signals") and s["win_rate"] >= 70.0 and s["avg_net_r"] > 0)


def val():
    start, end = C["val"]
    trades, nsym, ch = run(start, end)
    s = L.stats(trades, start, end)
    res = {"source": SRC, "validation": s, "validation_by_minute": by_minute(trades, start, end),
           "passes_validation_rule": L.passes(s), "owner_bar_validation": owner_bar(s), "symbols": nsym,
           "policy": {"max_per_bar": MAX_PER_BAR, "cooldown": COOLDOWN, "max_bars": H15},
           "from_4h_choice": {k: ch[k] for k in ("threshold", "grade_a", "plan", "cap")}}
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    json.dump(res, open(RESULTS, "w"), indent=1, default=str)
    log("VALIDATION", json.dumps(L.short(s)), json.dumps(s["years"]), "days with a signal %", s["days_with_signal_pct"],
        "passes:", res["passes_validation_rule"], "owner bar:", res["owner_bar_validation"])
    log("by minute of the hour:", json.dumps(res["validation_by_minute"]))
    return res


def _one_hour_trades(start, end):
    """The 1-hour stream (rolling.py) on the same period, from this build's on-the-hour offsets."""
    import rolling as R1
    os.makedirs(R1.OUT, exist_ok=True)
    meta = json.load(open(f"{OUT}/meta.json"))
    for sym in meta["symbols"]:
        key = sym.replace("/", "")
        for h in R1.OFFSETS:
            src = f"{OUT}/{key}_{h * 60}.npz"
            if os.path.exists(src):
                shutil.copyfile(src, f"{R1.OUT}/{key}_{h}.npz")
    json.dump({"symbols": meta["symbols"], "cols": meta["cols"], "folds": meta["folds"]}, open(f"{R1.OUT}/meta.json", "w"))
    trades, _, _ = R1.run(start, end)
    return trades


def overlap(t15, t1h, window_h=4):
    """Share of the 15-minute signals that the 1-hour stream also gives (same coin and side, signal within
    `window_h` hours), and how much earlier the 15-minute one comes then."""
    by = {}
    for t in t1h:
        by.setdefault((t["symbol"], t["side"]), []).append(pd.Timestamp(t["signal_at"]))
    for v in by.values():
        v.sort()
    same, lead = 0, []
    w = pd.Timedelta(hours=window_h)
    for t in t15:
        at = pd.Timestamp(t["signal_at"])
        near = [x for x in by.get((t["symbol"], t["side"]), []) if abs(x - at) <= w]
        if near:
            same += 1
            lead.append(min(near, key=lambda x: abs(x - at)) - at)
    n = len(t15)
    return {"signals_15m": n, "signals_1h": len(t1h), "also_in_1h": same,
            "also_in_1h_pct": round(100.0 * same / n, 1) if n else None,
            "median_minutes_earlier": round(float(np.median([x.total_seconds() for x in lead])) / 60, 1) if lead else None}


def final():
    res = json.load(open(RESULTS))
    if not res.get("passes_validation_rule"):
        log("validation did not pass: the holdout is not opened")
        return res
    if res.get("test"):
        log("holdout already scored:", json.dumps(L.short(res["test"])))
        return res
    start, end = C["hold"][0][0], C["hold"][-1][1]
    trades, nsym, _ = run(start, end)
    s = L.stats(trades, start, end)
    s["symbols"] = nsym
    s["folds"] = {}
    for a, e in C["hold"]:
        sel = [t for t in trades if pd.Timestamp(a) <= pd.Timestamp(t["signal_at"]) < pd.Timestamp(e)]
        s["folds"][f"{a} to {e}"] = L.short(L.stats(sel, a, e))
    res["test"] = s
    res["test_by_minute"] = by_minute(trades, start, end)
    s["cost_round_trip"] = L.markets.CRYPTO_COST_ROUND_TRIP        # the simulation already paid the real cost
    res["passed_test"] = bool(ST.passed_test(s, "crypto_15m"))
    res["owner_bar_test"] = owner_bar(s)
    json.dump(res, open(RESULTS, "w"), indent=1, default=str)
    pickle.dump(trades, open(f"{W}/results/crypto_15m_rolling_holdout_trades.pkl", "wb"))
    log("HOLDOUT", json.dumps(L.short(s)), json.dumps(s["folds"]), "passed_test:", res["passed_test"],
        "owner bar:", res["owner_bar_test"])
    log("by minute of the hour:", json.dumps(res["test_by_minute"]))
    res["overlap_with_1h"] = overlap(trades, _one_hour_trades(start, end))
    json.dump(res, open(RESULTS, "w"), indent=1, default=str)
    log("overlap with the 1-hour stream:", json.dumps(res["overlap_with_1h"]))
    return res


if __name__ == "__main__":
    {"build": build, "val": val, "final": final}[sys.argv[1]]()
