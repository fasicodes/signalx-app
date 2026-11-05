"""Second round of stream models (Nov 2026), on top of lab.py.

What changes against lab.py
* Forex uses NEW data: HistData 1-minute candles 2023-01 to 2026-10 (github.com/mailztoganesh-civil/forex-data,
  EST without DST -> UTC), built into 15-minute frames by repo_lab/prep.py. The old forex data ended in March 2022.
* Costs are the real ones in markets.py (crypto 0.30% per round trip + funding, forex spread + slippage).
* The threshold is chosen for the best AVERAGE RESULT after costs, not for a win rate (fixed before any holdout):
  among the validation grid points with >= 100 signals, profit factor >= 1.1 and a positive average in every
  validation year with >= 20 signals, the one with the highest average R. No such point -> the attempt fails.
* A stream may go live only when its one holdout test passes streams.passed_test(test, key).
Every attempt is logged in work/push/attempts.json, including the failed ones.

Usage:  python push_lab.py build forex_4h | wf forex_4h s2 | eval forex_4h s2 | summary
Env:    RESEARCH_DIR (lab data), FX_FRAMES_DIR (folder with <PAIR>_15m.pkl from repo_lab/prep.py)"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.dirname(HERE))
import lab as L  # noqa: E402
import streams as ST  # noqa: E402

FX_DIR = os.environ.get("FX_FRAMES_DIR") or os.path.join(L.R, "fx_frames")
OUT = os.path.join(L.R, "push")
FX_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "XAUUSD"]

# forex: 2023-01 data start -> features from March 2023, walk-forward 2024-01..2025-07, holdout 2025-07..2026-10
FX_DATES = dict(data_start="2023-03-01", dev=("2024-01-01", "2025-07-01"), hold=("2025-07-01", "2026-10-10"))
for key in ("forex_15m", "forex_1h", "forex_4h"):
    L.CFG[key].update(FX_DATES)
# crypto 1h / 15m: a one-target plan at 1R (geometry t2 / t3) is the cost-robust choice; dates as in lab.py
L.CFG["crypto_1h"]["geoms"]["t2"] = (2.0, 48, 1.0)
L.CFG["crypto_15m"]["geoms"]["t3"] = (3.0, 64, 1.0)
GRID = [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.93, 0.95, 0.97, 0.98, 0.99, 0.995]


def fx_symbols():
    return [p[:3] + "/" + p[3:] for p in FX_PAIRS if os.path.exists(f"{FX_DIR}/{p}_15m.pkl")]


def load_fx(sym, start):
    d = pd.read_pickle(f"{FX_DIR}/{sym.replace('/', '')}_15m.pkl")
    d = d[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    d["volume"] = d["volume"].astype(float)
    d = d[ST.forex_open_mask(d["timestamp"])].reset_index(drop=True)
    return d[d["timestamp"] >= pd.Timestamp(start) - pd.Timedelta(days=200)].reset_index(drop=True)


L.fx_symbols = fx_symbols
L.load_fx = load_fx


def year_ok(s):
    for y, v in (s.get("years") or {}).items():
        if v["signals"] >= 20 and v["avg_net_r"] <= 0:
            return False
    return True


def evaluate(stream, g):
    c = L.CFG[stream]
    cols, D = L.load_ds(stream)
    preds = dict(np.load(f"{L.R}/wf/{stream}_{g}.npz"))
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
    cal = L.calibration(pv, yv)
    okv = np.isfinite(pv)
    grid = []
    for q in GRID:
        thr = float(np.quantile(pv[okv], q))
        s = L.stats(L.simulate(stream, g, D, preds, thr, ds, de, cal), ds, de)
        s["q"], s["thr"] = q, thr
        grid.append(s)
        L.log(stream, g, f"q={q}", s["signals"], s.get("win_rate"), s.get("avg_net_r"), s.get("profit_factor"))
    ok = [s for s in grid if s["signals"] >= 100 and (s.get("profit_factor") or 0) >= 1.1 and year_ok(s)
          and (s.get("avg_net_r") or 0) > 0]
    res = {"stream": stream, "geom": g, "grid": grid, "choice": None, "test": None, "live": False}
    if ok:
        ch = max(ok, key=lambda s: s["avg_net_r"])
        res["choice"] = ch
        thr = ch["thr"]
        res["walk_forward"] = L.stats(L.simulate(stream, g, D, preds, thr, ds, de, cal), ds, de, len(D))
        test = L.stats(L.simulate(stream, g, D, preds, thr, hs, he, cal), hs, he, len(D))
        res["test"] = test
        res["live"] = bool(ST.passed_test(test, stream))
        L.log(stream, g, "VALIDATION", {k: res["walk_forward"].get(k) for k in ("signals", "win_rate", "avg_net_r", "profit_factor")})
        L.log(stream, g, "HOLDOUT", {k: test.get(k) for k in ("signals", "win_rate", "avg_net_r", "profit_factor", "max_drawdown_r")},
              "LIVE" if res["live"] else "not live")
    else:
        L.log(stream, g, "no threshold passes the validation rule")
    os.makedirs(OUT, exist_ok=True)
    json.dump(res, open(f"{OUT}/{stream}_{g}.json", "w"), indent=1, default=str)
    att = f"{OUT}/attempts.json"
    hist = json.load(open(att)) if os.path.exists(att) else []
    hist.append({"at": time.strftime("%Y-%m-%d %H:%M"), "stream": stream, "geom": g,
                 "validation": {k: (res.get("walk_forward") or {}).get(k) for k in ("signals", "avg_net_r", "profit_factor")},
                 "holdout": {k: (res.get("test") or {}).get(k) for k in ("signals", "win_rate", "avg_net_r", "profit_factor", "max_drawdown_r")},
                 "live": res["live"]})
    json.dump(hist, open(att, "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "build":
        L.build(sys.argv[2])
    elif cmd == "wf":
        L.wf(sys.argv[2], sys.argv[3])
    elif cmd == "eval":
        evaluate(sys.argv[2], sys.argv[3])
    elif cmd == "summary":
        print(json.dumps(json.load(open(f"{OUT}/attempts.json")), indent=1))
