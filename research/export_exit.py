"""Switch the live crypto 4h and 1h streams to the exit plan chosen on the validation years (one target at 1R,
the whole position closes there, the stop never moves) and rewrite their published test results under that plan.

How the plan was chosen (fixed before any holdout run, see exit_holdout.py): on the walk-forward years
(2023-01-01 to 2025-10-01) the plan with the highest average R after costs among those with >= 50% wins, profit
factor >= 1.2 and every year positive; ties go to the simpler plan. For both streams that was P2. It was then
scored once on the holdout (2025-10-01 to the end of the data) and replaces the old plan only because it passed
there too (>= 50 signals, >= 50% wins, positive average after costs, profit factor >= 1.1).

What is written:
  * signal_model.json (crypto 4h, engine v3.1): levels -> P2; "test" = the engine + board policy replayed with P2 on
    the holdout (all coins with data); "walk_forward" = the lab model of the same kind (exit_lab.py crypto_4h s2,
    score level q 0.98, about the live signal rate) on the walk-forward years with P2; "exit_change" = the old plan's
    numbers on the same holdout, for reference.
  * models/crypto_1h.json: levels -> P2; "test" / "walk_forward" = exit_holdout.py's holdout / validation rows of P2
    at the live threshold and 4h gate; "exit_change" = the old plan's holdout numbers.

python export_exit.py DATA_DIR        # DATA_DIR holds <COIN>_1h.csv.gz; RESEARCH_DIR holds the lab results
"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coin_expansion as CE  # noqa: E402
import exit_holdout_4h as H4  # noqa: E402

APP = os.path.dirname(HERE)
R = os.environ.get("RESEARCH_DIR") or os.path.join(HERE, "work")
SE = CE.SE
P2 = {"targets_r": [1.0], "fractions": [1.0], "be_after": 0}
NOTE_PLAN = ("Exit: one target at 1R (as far as the stop); the whole position closes there, the stop never moves. "
             "Chosen on the walk-forward years 2023-01 to 2025-10 (highest average result after costs), then checked "
             "once on the holdout.")


def _wins(ts):
    return round(sum(1 for t in ts if str(t["status"]).startswith("TP")) / len(ts) * 100, 1) if ts else None


def _avg(ts):
    return round(float(np.mean([t["r"] for t in ts])), 3) if ts else None


def full_stats(trades, t0, t1, trained):
    s = CE.stats(trades, t0, t1)
    n = len(trades)
    s["tp1_rate"] = s["win_rate"]
    s["tp2_rate"] = 0.0
    s["tp3_rate"] = 0.0
    s["money_making_rate"] = round(sum(1 for t in trades if t["r"] > 0) / n * 100, 1) if n else None
    s["avg_hold_bars"] = round(float(np.mean([t.get("bars_held") or 0 for t in trades])), 1) if n else None
    t_sorted = sorted(pd.Timestamp(t["signal_at"]) for t in trades)
    gaps = np.diff([t0] + t_sorted + [t1]) / pd.Timedelta(days=1)
    s["max_gap_days"] = round(float(np.max(gaps)), 1) if n else None
    s["avg_stop_pct"] = round(float(np.mean([abs(t["entry"] - t["stop_loss"]) / t["entry"] * 100 for t in trades])), 2)
    for tier in ("strong", "standard"):
        sel = [t for t in trades if t.get("tier") == tier]
        s[tier] = {"signals": len(sel), "win_rate": _wins(sel), "avg_net_r": _avg(sel)}
    tc = [t for t in trades if t["symbol"] in trained]
    nc = [t for t in trades if t["symbol"] not in trained]
    s["tested_coins"] = {"signals": len(tc), "win_rate": _wins(tc), "avg_net_r": _avg(tc)}
    s["new_coins"] = {"signals": len(nc), "win_rate": _wins(nc), "avg_net_r": _avg(nc)}
    years = {}
    for t in trades:
        years.setdefault(str(pd.Timestamp(t["signal_at"]).year), []).append(t)
    s["years"] = {y: {"signals": len(v), "win_rate": _wins(v), "avg_net_r": _avg(v)} for y, v in sorted(years.items())}
    s["fees"] = "0.12% round trip"
    return s


def export_4h(data_dir):
    path = os.path.join(APP, "signal_model.json")
    meta = json.load(open(path))
    trained = set(meta.get("trained_coins") or [])
    coins = list(SE.signal_coins())
    frames = {}
    for c in coins:
        d = CE.load(data_dir, c.split("/")[0])
        if d is not None and len(d):
            frames[c] = d
    have = [c for c in coins if c in frames]
    t1 = min(pd.Timestamp(frames["BTC/USDT"]["timestamp"].iloc[-1]).floor("4h"), pd.Timestamp("2026-10-17"))
    CE.log("4h coins with data", len(have), "end", t1)
    S = CE.scores_4h(frames, have)
    res = {}
    for name, plan in (("old", None), ("new", P2)):
        tr = [t for t in H4.replay(S, have, plan) if pd.Timestamp(t["signal_at"]) < t1]
        res[name] = full_stats(tr, CE.T0, t1, trained)
        CE.log("4h", name, json.dumps({k: res[name][k] for k in ("signals", "win_rate", "avg_net_r", "profit_factor",
                                                                  "max_drawdown_r")}))
    test = res["new"]
    test["period"] = f"{CE.T0.date()} to {t1.date()}"
    test["coins"] = len(have)
    test["note"] = (f"{len(have)} coins with data ({len(trained & set(have))} used in training, the rest never used). "
                    "The model was trained on data up to Sep 2025, so this period is data it never saw. " + NOTE_PLAN)
    wf_lab = json.load(open(f"{R}/results/exit_crypto_4h_s2.json"))["validation"]["P2_1R"]
    row = next(r for r in wf_lab if abs(r["q"] - 0.98) < 1e-9)
    wf = {k: row.get(k) for k in ("period", "signals", "signals_per_day", "win_rate", "tp1_rate", "stop_rate",
                                  "expired_rate", "avg_net_r", "total_net_r", "profit_factor", "max_drawdown_r",
                                  "avg_hold_bars", "max_gap_days", "years")}
    wf["tp2_rate"] = wf["tp3_rate"] = 0.0
    wf["note"] = ("Walk-forward 2023-01 to 2025-10 (each half year scored by a model trained only on earlier data) of "
                  "the research model of the same kind (same inputs, 2 ATR stop), at about the live signal rate. "
                  + NOTE_PLAN)
    lv = dict(meta["levels"])
    lv.update({"targets_r": [1.0], "fractions": [1.0], "be_after": 0, "tp_atr": float(lv["sl_atr"]) * 1.0})
    meta["levels"] = lv
    meta["test"], meta["walk_forward"] = test, wf
    old = res["old"]
    meta["exit_change"] = {
        "changed": "2026-10", "from": "targets 0.5R / 1R / 2R, a third at each, stop to entry after TP1",
        "to": "one target at 1R, the whole position, the stop never moves",
        "holdout_old_plan": {k: old[k] for k in ("signals", "win_rate", "avg_net_r", "total_net_r", "profit_factor",
                                                  "max_drawdown_r")},
        "holdout_new_plan": {k: test[k] for k in ("signals", "win_rate", "avg_net_r", "total_net_r", "profit_factor",
                                                   "max_drawdown_r")}}
    json.dump(meta, open(path, "w"), indent=1)
    CE.log("wrote", path)


def rescore_1h(m):
    import exit_lab as X
    import lab as L
    stream, g = "crypto_1h", "s2"
    thr, lev = float(m["threshold"]), float(m["gate"]["level"])
    c = L.CFG[stream]
    cols, D = L.load_ds(stream)
    preds = dict(np.load(f"{L.R}/wf/{stream}_{g}.npz"))
    ds, de = c["dev"]
    hs, he = c["hold"]
    vals, labs = [], []
    for sym, d in D.items():
        key = sym.replace("/", "")
        mm = (d["ts"] >= np.datetime64(ds)) & (d["ts"] < np.datetime64(de)) & d["elig"]
        for side in ("L", "S"):
            vals.append(preds[f"{key}_{side}"][mm])
            labs.append(d[f"y{side}_{g}"][mm])
    cal = L.calibration(np.concatenate(vals), np.concatenate(labs))
    gate = L.make_gate(L.htf_scores(stream, "crypto_4h", "s2"), lev)
    ga, gb = float(m["grade_a"]), float(m["grade_b"])
    out = []
    for a, b in ((hs, he), (ds, de)):
        tr = X.simulate_plan(stream, g, D, preds, thr, a, b, cal, X.PLANS["P2_1R"], gate, ga, gb)
        s = X.summ(tr, a, b)
        s["symbols"] = len(D)
        out.append(s)
        CE.log("1h", a, json.dumps({k: s.get(k) for k in ("signals", "win_rate", "avg_net_r", "profit_factor")}),
               json.dumps(s.get("by_grade")))
    return out[0], out[1]


def export_1h():
    path = os.path.join(APP, "models", "crypto_1h.json")
    m = json.load(open(path))
    eh = json.load(open(f"{R}/results/exit_holdout_crypto_1h.json"))
    assert eh["choice"] == "P2_1R" and eh.get("passed"), "the 1h plan check did not pass"
    hold, val = rescore_1h(m)        # same runs as exit_holdout.py, with the model's grade levels (A / B)
    for d in (hold, val):
        d.pop("plan", None)
        d["tp2_rate"] = d["tp3_rate"] = 0.0
    old = eh["holdout"]["P0_now"]
    m["levels"] = dict(m["levels"], targets_r=[1.0], fractions=[1.0], be_after=0)
    m["test"] = dict(hold, note="Data the model never saw. " + NOTE_PLAN)
    m["walk_forward"] = dict(val, note="Walk-forward predictions at the live score level and 4-hour gate. " + NOTE_PLAN)
    m["exit_change"] = {
        "changed": "2026-10", "from": "targets 0.5R / 1R / 2R, a third at each, stop to entry after TP1",
        "to": "one target at 1R, the whole position, the stop never moves",
        "holdout_old_plan": {k: old.get(k) for k in ("signals", "win_rate", "avg_net_r", "total_net_r",
                                                      "profit_factor", "max_drawdown_r")},
        "holdout_new_plan": {k: hold.get(k) for k in ("signals", "win_rate", "avg_net_r", "total_net_r",
                                                       "profit_factor", "max_drawdown_r")}}
    json.dump(m, open(path, "w"), indent=1)
    CE.log("wrote", path, json.dumps({k: hold.get(k) for k in ("signals", "win_rate", "avg_net_r", "profit_factor")}))


if __name__ == "__main__":
    export_1h()
    export_4h(sys.argv[1])
