"""Exit plan for the LIVE crypto 4h stream (signal_engine v3.1 + the board policy), scored once on the holdout.

The plan was picked before this run on the walk-forward years (exit_lab.py crypto_4h s2, 2023-01-01 to
2025-10-01, at the score level that matches the live signal rate, about 2 signals a day = q 0.98): by the rule in
exit_holdout.py the best plan there is P2 (one target at 1R, no partial exits, the stop never moves).
It replaces the live plan only if, on the holdout (2025-10-01 to the end of the data, never used for any choice),
it has >= 50 signals, >= 50% wins, a positive average after costs and a profit factor >= 1.1. The live plan is
replayed next to it only as the reference.

python exit_holdout_4h.py DATA_DIR      # DATA_DIR holds <COIN>_1h.csv.gz (see coin_expansion.py)
"""
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coin_expansion as CE  # noqa: E402

SE = CE.SE

PLAN = {"targets_r": [1.0], "fractions": [1.0], "be_after": 0}


def replay(S, coins, plan=None):
    m = SE.model()
    keep = (m.targets_r, m.fractions, m.be_after, m.tp_atr)
    if plan:
        m.targets_r, m.fractions, m.be_after = list(plan["targets_r"]), list(plan["fractions"]), plan["be_after"]
        m.tp_atr = m.sl_atr * m.targets_r[-1]
    try:
        return CE.run_4h(S, coins)
    finally:
        m.targets_r, m.fractions, m.be_after, m.tp_atr = keep


def main(data_dir):
    coins = [c for c in SE.signal_coins()]
    frames = {}
    for c in coins:
        d = CE.load(data_dir, c.split("/")[0])
        if d is not None and len(d):
            frames[c] = d
    have = [c for c in coins if c in frames]
    t1 = min(pd.Timestamp(frames["BTC/USDT"]["timestamp"].iloc[-1]).floor("4h"), pd.Timestamp("2026-10-17"))
    CE.log("coins with data", len(have), "of", len(coins), "end", t1)
    S = CE.scores_4h(frames, have)
    out = {"period": f"{CE.T0.date()} to {t1.date()}", "coins": len(have), "plan": PLAN}
    for name, plan in (("live_P0", None), ("P2_1R", PLAN)):
        tr = [t for t in replay(S, have, plan) if pd.Timestamp(t["signal_at"]) < t1]
        s = CE.stats(tr, CE.T0, t1)
        r = [t["r"] for t in tr]
        s["pos_rate"] = round(sum(1 for x in r if x > 0) / len(r) * 100, 1) if r else None
        out[name] = s
        CE.log(name, json.dumps({k: s.get(k) for k in ("signals", "win_rate", "pos_rate", "avg_net_r", "total_net_r",
                                                        "profit_factor", "max_drawdown_r")}))
    h = out["P2_1R"]
    out["passed"] = bool(h["signals"] >= 50 and (h["win_rate"] or 0) >= 50 and (h["avg_net_r"] or 0) > 0
                         and (h["profit_factor"] or 0) >= 1.1)
    CE.log("P2 PASSED" if out["passed"] else "P2 not passed")
    p = os.path.join(os.environ.get("RESEARCH_DIR", HERE), "results", "exit_holdout_crypto_4h.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(out, open(p, "w"), indent=1, default=str)


if __name__ == "__main__":
    main(sys.argv[1])
