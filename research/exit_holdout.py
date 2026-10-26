"""Exit plans on the LIVE crypto models: pick the plan on the validation years, score it once on the holdout.

Rule (fixed before the holdout run):
  * candidates: exit_lab.PLANS without the trailing ones the site cannot run yet (P0, P1, P2, P3, P5, P6);
  * validation (walk-forward predictions, 2023-01-01 to 2025-10-01), at the live threshold and gate: the plan with
    the highest average R after costs among those with >= 50% wins, profit factor >= 1.2 and every validation
    year positive; ties (within 0.005 R) go to the simpler plan (fewer targets);
  * holdout (2025-10-01 to 2026-10-11, never used for any choice): the chosen plan replaces the live plan only if
    it has >= 50 signals, >= 50% wins, a positive average after costs and a profit factor >= 1.1.
The live plan (P0) is scored next to it on the holdout only as the reference.

python exit_holdout.py 1h          # models/crypto_1h.json, its 4h gate, lab walk-forward predictions
"""
import json
import os
import sys

import numpy as np

import exit_lab as X
import lab as L

CANDIDATES = ["P0_now", "P1_123", "P2_1R", "P3_1.5R", "P5_now_noBE", "P6_now_BEafterTP2"]


def pick(rows):
    ok = [r for r in rows if r["signals"] >= 100 and (r["win_rate"] or 0) >= 50 and (r["profit_factor"] or 0) >= 1.2
          and all(v["avg_net_r"] > 0 for v in r["years"].values() if v["signals"] >= 20)]
    if not ok:
        return None
    best = max(r["avg_net_r"] for r in ok)
    near = [r for r in ok if r["avg_net_r"] >= best - 0.005]
    return min(near, key=lambda r: (len(X.PLANS[r["plan"]]["targets_r"]), -r["avg_net_r"]))


def run_1h():
    stream, g = "crypto_1h", "s2"
    m = json.load(open(os.path.join(L.APP, "models", "crypto_1h.json")))
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
    rows = []
    for name in CANDIDATES:
        s = X.summ(X.simulate_plan(stream, g, D, preds, thr, ds, de, cal, X.PLANS[name], gate), ds, de)
        s["plan"] = name
        rows.append(s)
        L.log("VALIDATION", name, s["signals"], "win", s["win_rate"], "avg", s["avg_net_r"], "pf", s["profit_factor"],
              "dd", s["max_drawdown_r"], "wk+", s["weeks_positive"], {y: v["avg_net_r"] for y, v in s["years"].items()})
    choice = pick(rows)
    out = {"stream": stream, "threshold": thr, "gate_level": lev, "validation": rows,
           "choice": choice["plan"] if choice else None}
    if choice:
        for name in dict.fromkeys([choice["plan"], "P0_now"]):
            s = X.summ(X.simulate_plan(stream, g, D, preds, thr, hs, he, cal, X.PLANS[name], gate), hs, he)
            out.setdefault("holdout", {})[name] = s
            L.log("HOLDOUT", name, json.dumps({k: s.get(k) for k in ("signals", "win_rate", "pos_rate", "avg_net_r",
                                                                      "total_net_r", "profit_factor", "max_drawdown_r",
                                                                      "weeks_positive")}))
        h = out["holdout"][choice["plan"]]
        out["passed"] = bool(h["signals"] >= 50 and (h["win_rate"] or 0) >= 50 and h["avg_net_r"] > 0
                             and (h["profit_factor"] or 0) >= 1.1)
    json.dump(out, open(f"{L.R}/results/exit_holdout_{stream}.json", "w"), indent=1, default=str)
    L.log("CHOICE", out["choice"], "PASSED" if out.get("passed") else "not passed")


if __name__ == "__main__":
    {"1h": run_1h}[sys.argv[1]]()
