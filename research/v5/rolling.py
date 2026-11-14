"""
Engine 5, a 1-hour stream from the 4-hour model: the 4h model read every hour.

The 4h model of lab5.py (crypto_4h) is the one new model that passed its holdout test; the separate 1-hour and
15-minute models did not. The 4h model reads 4-hour candles on the usual grid (00:00, 04:00, ... UTC). Here the SAME
walk-forward fold models read 4-hour candles that end at EVERY full hour - four series per coin: the usual one and
three shifted by 1, 2 and 3 hours - so the stream can give a signal at any hourly close.

Nothing is tuned here. The score level, the grade level, the plan (one target at 0.5R, stop 2 ATR of the 4-hour
candles, 9 days) and the calibration are the 4h model's validated choice (work/results/crypto_4h_choice.json). Fixed
before the test: at most 3 new signals per hourly candle, a cooldown of 4 hourly candles per coin.

  python rolling.py build      shifted 4-hour features and the fold models' scores  -> work/rolling/
  python rolling.py val        validation years only (2023-01 .. 2025-09): must pass lab5's validation rule
  python rolling.py final      the one holdout test (2025-10 .. 2026-11); refuses when validation did not pass

The holdout period had already been used once for a 1-hour stream (the separate 1-hour model with the 4h check,
which failed), so this is the second 1-hour variant scored on it; models/research.json says so.
"""
import json
import os
import pickle
import sys
import warnings

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lab5 as L  # noqa: E402
from lab5 import FE, ST, W, log  # noqa: E402

SRC = "crypto_4h"                    # the model whose folds are read every hour
OUT = f"{W}/rolling"
OFFSETS = (0, 1, 2, 3)
MAX_PER_BAR, COOLDOWN = 3, 4
C = L.CFG[SRC]
H1 = C["H"] * 4                      # the 4h model's time limit, in hourly candles
H4 = np.timedelta64(4, "h")


def shifted_bars(d15, off):
    """4-hour candles whose grid is shifted by `off` hours (they end at 0+off, 4+off, ... UTC)."""
    d = d15.copy()
    d["timestamp"] = d["timestamp"] - pd.Timedelta(hours=off)
    b = FE.resample_closed(d[["timestamp", "open", "high", "low", "close", "volume"]], "4h")
    b["timestamp"] = b["timestamp"] + pd.Timedelta(hours=off)
    return b.reset_index(drop=True)


def build():
    os.makedirs(OUT, exist_ok=True)
    syms = L.symbols(C)
    cols = L.feature_list(C)
    folds = sorted(f for f in os.listdir(f"{W}/wf") if f.startswith(f"{SRC}_fold_"))
    models = [pickle.load(open(f"{W}/wf/{f}", "rb")) for f in folds]
    assert models and models[0]["cols"] == cols
    keep_cols = ["timestamp", "close", "d_sma200", "h_d_sma50"] + FE._ZC
    for off in OFFSETS:
        frames, small, anchor = {}, {}, None
        for sym in syms:
            b = shifted_bars(L.load15(C, sym), off)
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
        log("offset", off, "symbols", len(frames))
    json.dump({"symbols": syms, "cols": cols, "folds": folds}, open(f"{OUT}/meta.json", "w"))
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


def hourly_data(thr, grade_a, cal):
    """Per coin, on hourly candles: the candidate (side, score, stop distance) of the 4-hour candle that ends with
    each hourly candle."""
    meta = json.load(open(f"{OUT}/meta.json"))
    data = {}
    for sym in meta["symbols"]:
        key = sym.replace("/", "")
        if not os.path.exists(f"{OUT}/{key}_0.npz"):
            continue
        d15 = L.load15(C, sym)
        h = FE.resample_closed(d15[["timestamp", "open", "high", "low", "close", "volume"]], "1h").reset_index(drop=True)
        h = h[h["timestamp"] >= pd.Timestamp(C["val"][0]) - pd.Timedelta(days=2)].reset_index(drop=True)
        ts = pd.to_datetime(h["timestamp"]).to_numpy().astype("datetime64[ns]")
        n = len(ts)
        pl, ps, atr, off = (np.full(n, np.nan) for _ in range(4))
        for o in OFFSETS:
            z = np.load(f"{OUT}/{key}_{o}.npz")
            last_hour = z["ts"].astype("datetime64[ns]") + np.timedelta64(3, "h")     # the hourly candle that ends the bar
            pos = np.searchsorted(ts, last_hour)
            ok = (pos < n) & (ts[np.minimum(pos, n - 1)] == last_hour)
            # the 4-hour candle and its last hourly candle close at the same price (same last 15-minute candle)
            ok &= np.isclose(z["close"], h["close"].to_numpy(float)[np.minimum(pos, n - 1)], rtol=1e-9, atol=0)
            i = pos[ok]
            pl[i], ps[i], atr[i], off[i] = z["pl"][ok], z["ps"][ok], z["atr"][ok], o
        best = np.fmax(pl, ps)
        longs = pl >= ps
        okc = np.isfinite(best) & (best >= thr) & np.isfinite(atr) & (atr > 0)
        fill = d15.set_index("timestamp")["fill"]
        slip = fill.reindex(h["timestamp"] + pd.Timedelta(hours=1)).to_numpy() - h["close"].to_numpy(float)
        data[sym] = {"ts": ts, "o": h["open"].to_numpy(float), "h": h["high"].to_numpy(float),
                     "l": h["low"].to_numpy(float), "c": h["close"].to_numpy(float), "atr": atr,
                     "side": np.where(okc, np.where(longs, "LONG", "SHORT"), "").astype(object),
                     "score": np.nan_to_num(best, nan=0.0),
                     "conf": np.interp(np.where(longs, pl, ps), cal[0], cal[1]) * 100,
                     "grade": np.where(best >= grade_a, "A", "B").astype(object), "setup": None,
                     "dist": C["k"] * atr, "slip": slip, "off": off}
    return data


def run(start, end):
    ch = json.load(open(f"{W}/results/{SRC}_choice.json"))
    cal = (np.array(ch["calibration"]["x"]), np.array(ch["calibration"]["y"]))
    data = hourly_data(ch["threshold"], ch["grade_a"], cal)
    plan = dict(L.PLANS[ch["plan"]], max_bars=H1, tf_sec=3600)
    policy = {"max_per_bar": MAX_PER_BAR, "cooldown": COOLDOWN, "sessions": None, "no_new_before_close_h": 0}
    _, _, closed = ST.run_policy(data, "crypto", "1h", plan, policy, start_time=start, end_time=end, lookback=10 ** 9)
    trades = []
    for sym, lst in closed.items():
        ts, off = data[sym]["ts"], data[sym]["off"]
        for t in lst:
            t["offset"] = int(off[int(np.searchsorted(ts, np.datetime64(t["bar_time"])))])
            t["symbol"] = sym
            trades.append(t)
    return trades, len(data), ch


def by_offset(trades, start, end):
    out = {}
    for o in OFFSETS:
        sel = [t for t in trades if t["offset"] == o]
        if sel:
            s = L.stats(sel, start, end)
            out[str(o)] = {k: s[k] for k in ("signals", "win_rate", "avg_net_r", "profit_factor")}
    return out


def val():
    start, end = C["val"]
    trades, nsym, ch = run(start, end)
    s = L.stats(trades, start, end)
    res = {"source": SRC, "validation": s, "validation_by_offset": by_offset(trades, start, end),
           "passes_validation_rule": L.passes(s), "symbols": nsym,
           "policy": {"max_per_bar": MAX_PER_BAR, "cooldown": COOLDOWN, "max_bars": H1},
           "from_4h_choice": {k: ch[k] for k in ("threshold", "grade_a", "plan", "cap")}}
    json.dump(res, open(f"{W}/results/crypto_1h_rolling.json", "w"), indent=1, default=str)
    log("VALIDATION", json.dumps(L.short(s)), json.dumps(s["years"]), "days with a signal %", s["days_with_signal_pct"],
        "passes:", res["passes_validation_rule"])
    log("by offset (0 = the usual 4-hour grid):", json.dumps(res["validation_by_offset"]))
    return res


def final():
    path = f"{W}/results/crypto_1h_rolling.json"
    res = json.load(open(path))
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
    res["test_by_offset"] = by_offset(trades, start, end)
    json.dump(res, open(path, "w"), indent=1, default=str)
    pickle.dump(trades, open(f"{W}/results/crypto_1h_rolling_holdout_trades.pkl", "wb"))
    log("HOLDOUT", json.dumps(L.short(s)), json.dumps(s["years"]), "passes the site's live bar:",
        ST.passed_test(dict(s, cost_round_trip=L.markets.CRYPTO_COST_ROUND_TRIP), "crypto_1h"))
    log("by offset:", json.dumps(res["test_by_offset"]))
    return res


if __name__ == "__main__":
    {"build": build, "val": val, "final": final}[sys.argv[1]]()
