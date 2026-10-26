"""Exit-structure lab: the same model signals (walk-forward predictions of a stream) under different trade plans.

Only the validation years are scored, unless `--holdout PLAN` names the one plan chosen on validation (that plan
is then scored once on the holdout). Results are after costs, with the site's own trade walker (streams.follow).

python exit_lab.py crypto_1h s2 [--mtf crypto_4h s2] [--holdout P4]
"""
import json
import os
import sys

import numpy as np

import lab as L

PLANS = {
    "P0_now": dict(targets_r=[0.5, 1.0, 2.0], fractions=[1 / 3] * 3, be_after=1),
    "P1_123": dict(targets_r=[1.0, 2.0, 3.0], fractions=[1 / 3] * 3, be_after=1),
    "P2_1R": dict(targets_r=[1.0], fractions=[1.0], be_after=0),
    "P3_1.5R": dict(targets_r=[1.5], fractions=[1.0], be_after=0),
    "P4_half1R_trail1": dict(targets_r=[1.0, 50.0], fractions=[0.5, 0.5], be_after=1, trail_r=1.0, trail_after=1),
    "P5_now_noBE": dict(targets_r=[0.5, 1.0, 2.0], fractions=[1 / 3] * 3, be_after=0),
    "P6_now_BEafterTP2": dict(targets_r=[0.5, 1.0, 2.0], fractions=[1 / 3] * 3, be_after=2),
    "P7_half05_trail1": dict(targets_r=[0.5, 50.0], fractions=[0.5, 0.5], be_after=1, trail_r=1.0, trail_after=1),
    "P8_1_2_trail1.5": dict(targets_r=[1.0, 2.0, 50.0], fractions=[1 / 3] * 3, be_after=1, trail_r=1.5, trail_after=2),
}


MAXCOST = 0.0


def simulate_plan(stream, g, D, preds, thr, start, end, cal, extra, gate=None, grade_a=9, grade_b=9):
    c = L.CFG[stream]
    k, H = c["geoms"][g][:2]
    plan = {"max_bars": H, "tf_sec": L.FE.TF_SEC[c["tf"]], **extra}
    policy = {"max_per_bar": c["max_per_bar"], "cooldown": c["cooldown"], "sessions": c.get("sessions"),
              "no_new_before_close_h": c.get("close_h", 0)}
    data = L.policy_data(D, preds, thr, grade_a, grade_b, k, cal, gate=gate)
    if MAXCOST:                          # skip signals whose round-trip cost is a big part of the stop
        for sym, v in data.items():
            cr = np.array([L.ST.cost_r(sym, e, e - dd) if np.isfinite(dd) and dd > 0 else 9.0
                           for e, dd in zip(v["c"], v["dist"])]) if c["market"] == "forex" else \
                L.ST.markets.CRYPTO_COST_ROUND_TRIP * v["c"] / np.where(v["dist"] > 0, v["dist"], np.nan)
            v["side"] = np.where(np.nan_to_num(cr, nan=9.0) <= MAXCOST, v["side"], "").astype(object)
    _a, _l, closed = L.ST.run_policy(data, c["market"], c["tf"], plan, policy, start_time=start, end_time=end,
                                     lookback=10 ** 9)
    out = []
    for sym, lst in closed.items():
        for t in lst:
            t["symbol"] = sym
            out.append(t)
    return out


def summ(trades, start, end):
    s = L.stats(trades, start, end)
    if not trades:
        return s
    r = np.array([t["r"] for t in trades], float)
    s["pos_rate"] = round(float((r > 0).mean() * 100), 1)          # share of signals that made money
    weeks = {}
    for t in trades:
        w = str(np.datetime64(str(t["signal_at"])[:10], "W"))
        weeks[w] = weeks.get(w, 0.0) + t["r"]
    wv = np.array(list(weeks.values()))
    s["weeks_positive"] = round(float((wv > 0).mean() * 100), 1)
    return s


def main():
    a = sys.argv[1:]
    stream, g = a[0], a[1]
    mtf = a[a.index("--mtf") + 1: a.index("--mtf") + 3] if "--mtf" in a else None
    hold_plan = a[a.index("--holdout") + 1] if "--holdout" in a else None
    global MAXCOST
    MAXCOST = float(a[a.index("--maxcost") + 1]) if "--maxcost" in a else 0.0
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
    gate = None
    if mtf:
        H = L.htf_scores(stream, mtf[0], mtf[1])
        parts = []
        for s in H:
            mv = (D[s]["ts"] >= np.datetime64(ds)) & (D[s]["ts"] < np.datetime64(de)) & D[s]["elig"]
            parts += [H[s][0][mv], H[s][1][mv]]
        hv = np.concatenate(parts)
        lev = float(np.quantile(hv[np.isfinite(hv)], 0.5))
        gate = L.make_gate(H, lev)
    qs = [0.8, 0.9, 0.95, 0.97, 0.98, 0.99]
    out = {"stream": stream, "geom": g, "mtf": mtf, "validation": {}}
    for name, extra in PLANS.items():
        rows = []
        for q in qs:
            thr = float(np.quantile(pv[okv], q))
            s = summ(simulate_plan(stream, g, D, preds, thr, ds, de, cal, extra, gate), ds, de)
            s.update({"q": q, "thr": thr})
            rows.append(s)
            L.log(stream, name, f"q={q}", s["signals"], "win", s.get("win_rate"), "pos", s.get("pos_rate"),
                  "avg", s.get("avg_net_r"), "pf", s.get("profit_factor"), "wk+", s.get("weeks_positive"))
        out["validation"][name] = rows
    if hold_plan:
        q = float(a[a.index("--q") + 1]) if "--q" in a else 0.95
        thr = float(np.quantile(pv[okv], q))
        s = summ(simulate_plan(stream, g, D, preds, thr, hs, he, cal, PLANS[hold_plan], gate), hs, he)
        out["holdout"] = {"plan": hold_plan, "q": q, **s}
        L.log("HOLDOUT", hold_plan, json.dumps({k: s.get(k) for k in ("signals", "win_rate", "pos_rate", "avg_net_r",
                                                                       "profit_factor", "weeks_positive")}))
    os.makedirs(f"{L.R}/results", exist_ok=True)
    tag = f"{stream}_{g}{'_mtf' if mtf else ''}{f'_mc{MAXCOST}' if MAXCOST else ''}"
    json.dump(out, open(f"{L.R}/results/exit_{tag}.json", "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
