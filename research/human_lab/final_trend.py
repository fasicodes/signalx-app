"""The ONE final test of the trend trader, exactly as pre-registered in loop_log.json (try 71) before it ran.

Variant: 4h, 55-candle breakout, no leaders filter, stop 3 ATR, target 2R, time limit 40 candles.
Test A: the 28 research coins, 2025-01-01 .. 2026-10-24 -> streams.passed_test() AND positive in 2025 AND 2026.
Test B: coins never used anywhere, 2021-03 .. 2026-10, market regime from the 28 research coins ->
        overall >= +0.05R, positive in >= 4 of the 6 years, and the 2025-2026 part passes streams.passed_test().
Writes trend_final.json.

Usage: python final_trend.py <fresh data dir>
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lab  # noqa: E402
import trend_lab  # noqa: E402

VARIANT = {"tf": "4h", "n": 55, "leaders": False, "k_atr": 3.0, "target_r": 2.0, "trail": False, "max_bars": 40}


def run(fresh_dir):
    tf = VARIANT["tf"]
    kw = {k: VARIANT[k] for k in ("n", "leaders", "k_atr", "target_r", "trail", "max_bars")}
    research = trend_lab.load(tf)
    # ---- test A
    tr = trend_lab.trades(research, tf, **kw)
    hold = [t for t in tr if t["t"] >= lab.HOLD_START]
    ta = lab.portfolio(hold, [0.0] * len(hold), "4h")
    last = max(t["t"] for t in hold)
    sa = lab.stats(ta, float((last - lab.HOLD_START) / np.timedelta64(1, "D")))
    sa["period"] = f"2025-01-01 to {str(last)[:10]}"
    sa["cost_round_trip"] = 0.003
    a_pass = bool(lab.passed_test(sa) and all(sa["years"].get(y, {}).get("avg_net_r", -1) > 0 for y in (2025, 2026)))
    # ---- test B
    fresh = trend_lab.load(tf, fresh_dir)
    fresh = {s: f for s, f in fresh.items() if s not in research}
    tb_all = trend_lab.trades(fresh, tf, btc_frames=research, **kw)
    tb_all = [t for t in tb_all if t["t"] >= lab.VAL_START]
    tb = lab.portfolio(tb_all, [0.0] * len(tb_all), "4h")
    sb = lab.stats(tb)
    yrs = sb.get("years", {})
    pos_years = sum(1 for y in range(2021, 2027) if yrs.get(y, {}).get("avg_net_r", -1) > 0)
    tb_recent = [t for t in tb if t["t"] >= lab.HOLD_START]
    sbr = lab.stats(tb_recent)
    sbr["cost_round_trip"] = 0.003
    sbr["period"] = sa["period"]
    b_pass = bool(sb.get("signals") and sb["avg_net_r"] >= 0.05 and pos_years >= 4 and lab.passed_test(sbr))
    out = {"variant": VARIANT, "pre_registered": "loop_log.json try 71",
           "test_A_research_coins_2025_2026": {"pass": a_pass, **sa},
           "test_B_fresh_coins": {"pass": b_pass, "coins": sorted(fresh), "positive_years": pos_years, **sb},
           "test_B_fresh_coins_2025_2026": sbr,
           "passed": a_pass and b_pass}
    with open(f"{HERE}/trend_final.json", "w") as fh:
        json.dump(out, fh, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    for k in ("test_A_research_coins_2025_2026", "test_B_fresh_coins", "test_B_fresh_coins_2025_2026"):
        s = out[k]
        print(k, {x: s.get(x) for x in ("pass", "signals", "win_rate", "avg_net_r", "profit_factor", "max_drawdown_r",
                                        "total_net_r", "signals_per_day")},
              {y: v["avg_net_r"] for y, v in (s.get("years") or {}).items()})
    print("PASSED" if out["passed"] else "FAILED")
    return out


if __name__ == "__main__":
    run(sys.argv[1])
