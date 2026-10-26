"""Cost check for the exit-plan change: the holdout replays of the live crypto streams (live plan vs P2, one
target at 1R) with the site's flat cost (0.12% round trip) and with realistic costs by coin group plus funding.

Realistic costs (research notes, costs_execution.md; estimates): round trip on perpetuals by coin rank on the
site's list (markets.CRYPTO_COINS order): BTC/ETH 0.13%, ranks 3-30 0.18%, ranks 31-62 0.25%, ranks 63-102 (the
4h-only coins) 0.45%. Funding: longs pay 0.03% a day while the trade is open (OKX baseline 0.01% per 8 hours;
shorts are assumed to receive nothing, which is conservative).

python exit_costs.py DATA_DIR          (needs the lab walk-forward files for the 1h stream, RESEARCH_DIR)
"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import exit_holdout_4h as H4  # noqa: E402
import exit_lab as X  # noqa: E402
import lab as L  # noqa: E402

COINS = list(H4.SE.signal_coins())
import markets  # noqa: E402

RANK = {c: k for k, c in enumerate(markets.CRYPTO_COINS)}
SITE = 0.0012
FUNDING_DAY = 0.0003


def tier_cost(sym):
    k = RANK.get(sym, 999)
    return 0.0013 if k < 2 else 0.0018 if k < 30 else 0.0025 if k < 62 else 0.0045


def hold_days(t, tf_hours):
    if t.get("bars_held") is not None:
        return float(t["bars_held"]) * tf_hours / 24
    if t.get("closed_at") is not None:
        return (pd.Timestamp(t["closed_at"]) - pd.Timestamp(t["signal_at"])).total_seconds() / 86400
    return 0.0


def realistic(trades, tf_hours):
    out = []
    for t in trades:
        risk = abs(t["entry"] - t["stop_loss"]) / t["entry"]
        extra = tier_cost(t["symbol"]) - SITE
        if t["side"] == "LONG":
            extra += FUNDING_DAY * hold_days(t, tf_hours)
        out.append(t["r"] - extra / risk if risk > 0 else t["r"])
    return np.array(out, float)


def summary(r, trades, t0, t1):
    order = np.argsort([pd.Timestamp(t.get("closed_at") or t["signal_at"]).value for t in trades], kind="stable")
    eq = np.cumsum(r[order])
    dd = float(np.min(eq - np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:])) if len(r) else 0.0
    gp, gl = r[r > 0].sum(), -r[r < 0].sum()
    by = {}
    for t, x in zip(trades, r):
        k = RANK.get(t["symbol"], 999)
        g = "ranks 1-30" if k < 30 else "ranks 31-62" if k < 62 else "ranks 63-102"
        by.setdefault(g, []).append(x)
    return {"signals": len(r), "avg_r": round(float(r.mean()), 3), "total_r": round(float(r.sum()), 1),
            "profit_factor": round(float(gp / gl), 2) if gl > 0 else None, "max_drawdown_r": round(dd, 1),
            "money_making_share": round(float((r > 0).mean() * 100), 1),
            "by_group": {g: {"signals": len(v), "avg_r": round(float(np.mean(v)), 3)} for g, v in sorted(by.items())}}


def main(data_dir):
    out = {}
    # 4h: the live engine replay
    frames = {}
    for c in COINS:
        d = H4.CE.load(data_dir, c.split("/")[0])
        if d is not None and len(d):
            frames[c] = d
    have = [c for c in COINS if c in frames]
    t1 = min(pd.Timestamp(frames["BTC/USDT"]["timestamp"].iloc[-1]).floor("4h"), pd.Timestamp("2026-10-17"))
    S = H4.CE.scores_4h(frames, have)
    for name, plan in (("live_P0", None), ("P2_1R", H4.PLAN)):
        tr = [t for t in H4.replay(S, have, plan) if pd.Timestamp(t["signal_at"]) < t1]
        site = np.array([t["r"] for t in tr], float)
        out[f"crypto_4h {name}"] = {"site_costs": summary(site, tr, H4.CE.T0, t1),
                                     "realistic_costs": summary(realistic(tr, 4), tr, H4.CE.T0, t1)}
        L.log("4h", name, json.dumps(out[f"crypto_4h {name}"]))
    # 1h: the lab holdout at the live threshold and gate
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
    for name in ("P0_now", "P2_1R"):
        tr = X.simulate_plan(stream, g, D, preds, thr, hs, he, cal, X.PLANS[name], gate)
        site = np.array([t["r"] for t in tr], float)
        out[f"crypto_1h {name}"] = {"site_costs": summary(site, tr, hs, he),
                                     "realistic_costs": summary(realistic(tr, 1), tr, hs, he)}
        L.log("1h", name, json.dumps(out[f"crypto_1h {name}"]))
    json.dump(out, open(f"{L.R}/results/exit_costs.json", "w"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1])
