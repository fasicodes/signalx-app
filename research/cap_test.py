"""
More 1h signals: let more than 4 new signals start in the same hour?

The 1h stream allows at most 4 new signals per candle (the best scores). Its model often finds many more good setups
at once, because coins move together. This test checks whether a higher limit keeps the quality.

Decided before any result was seen:
  * the model, its threshold, its 4h gate and the trade plan stay exactly as they are (models/crypto_1h.json);
  * limits tested: 4 (now), 6, 8, 12 and no limit;
  * the limit is chosen on the walk-forward predictions of the validation years only (2023-01-01 to 2025-10-01,
    every 6-month fold trained only on earlier data, as in lab.py): the highest limit whose result passes the same
    selection rule v2 as the threshold did (>= 73% wins, >= +0.03R after costs, every validation year >= 70% with a
    positive average, >= 100 signals);
  * that limit is then scored once on the year the model never saw (2025-10-01 to the end of the data) with the live
    model; it goes live only with >= 70% wins, a positive average after costs and >= 50 signals. Otherwise the
    limit stays 4.

Usage (after `lab.py build crypto_4h`, `lab.py build crypto_1h`, `lab.py wf crypto_4h s2`, `lab.py wf crypto_1h s2`):
  RESEARCH_DIR=... python cap_test.py validate            # walk-forward: every limit + the choice
  RESEARCH_DIR=... python cap_test.py holdout DATA_1H N   # the chosen limit N on the unseen year (1h candles)
"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import lab  # noqa: E402
import streams as ST  # noqa: E402

LIMITS = [4, 6, 8, 12, 0]          # 0 = no limit


def live_model():
    return json.load(open(os.path.join(os.path.dirname(HERE), "models", "crypto_1h.json")))


def validate():
    m = live_model()
    stream, g = "crypto_1h", "s2"
    cols, D = lab.load_ds(stream)
    preds = dict(np.load(f"{lab.R}/wf/{stream}_{g}.npz"))
    H = lab.htf_scores(stream, "crypto_4h", "s2")
    gate = lab.make_gate(H, float(m["gate"]["level"]))
    cal = lab._Cal(m["calibration"]["x"], m["calibration"]["y"])
    thr = float(m["threshold"])
    ds, de = lab.CFG[stream]["dev"]
    out = {"threshold": thr, "gate_level": float(m["gate"]["level"]), "period": f"{ds} to {de}",
           "published_walk_forward": {k: m["walk_forward"].get(k) for k in ("signals", "signals_per_day", "win_rate", "avg_net_r")},
           "limits": {}}
    for cap in LIMITS:
        tr = lab.simulate(stream, g, D, preds, thr, ds, de, cal, max_per_bar=cap, gate=gate)
        s = lab.stats(tr, ds, de)
        s.pop("by_grade", None)
        s["passes_v2"] = lab.passes(s)
        out["limits"][str(cap or "none")] = s
        lab.log("limit", cap or "none", json.dumps({k: s[k] for k in ("signals", "signals_per_day", "win_rate", "avg_net_r", "passes_v2")}),
                json.dumps({y: (v["win_rate"], v["avg_net_r"]) for y, v in s["years"].items()}))
    ok = [c for c in LIMITS if out["limits"][str(c or "none")]["passes_v2"]]
    # highest limit = most signals ("none" counts as the highest)
    choice = max(ok, key=lambda c: 10 ** 6 if c == 0 else c) if ok else None
    out["choice"] = choice
    lab.log("CHOICE", choice if choice is None or choice else "none")
    json.dump(out, open(f"{lab.R}/results/cap_test_validation.json", "w"), indent=1, default=str)
    return out


def holdout(data_dir, cap):
    import coin_expansion as CE
    import markets
    cap = int(cap)
    frames = {}
    coins = [s for s in markets.stream_coins("crypto", "1h")]
    for sym in coins:
        df = CE.load(data_dir, sym.split("/")[0])
        if df is not None:
            frames[sym] = df
    t1 = frames["BTC/USDT"]["timestamp"].iloc[-1] + pd.Timedelta(hours=1)
    ok = [s for s in coins if s in frames and CE.eligible(frames[s], t1)[0]]
    data, plan, policy = CE.stream_1h(frames, ok, t1)
    res = {}
    for c in sorted({4, cap}):
        pol = dict(policy, max_per_bar=c)
        _, _, closed = ST.run_policy({s: data[s] for s in ok}, "crypto", "1h", plan, pol, start_time=CE.T0, end_time=t1,
                                     lookback=10 ** 9)
        trades = [dict(t, symbol=s) for s, lst in closed.items() for t in lst]
        res[str(c)] = CE.stats(trades, CE.T0, t1)
        res[str(c)]["passes"] = CE.passes(res[str(c)])
        lab.log("holdout limit", c, json.dumps({k: res[str(c)][k] for k in ("signals", "signals_per_day", "win_rate", "avg_net_r", "profit_factor", "max_drawdown_r")}))
    out = {"period": f"{CE.T0.date()} to {t1.date()}", "coins": len(ok), "limit": cap, "results": res}
    json.dump(out, open(f"{lab.R}/results/cap_test_holdout.json", "w"), indent=1, default=str)
    return out


def export(data_dir):
    """Writes the chosen limit into models/crypto_1h.json with its tests (only when the holdout passed)."""
    import coin_expansion as CE
    import markets
    val = json.load(open(f"{lab.R}/results/cap_test_validation.json"))
    hold = json.load(open(f"{lab.R}/results/cap_test_holdout.json"))
    cap = val["choice"]
    res = hold["results"].get(str(cap))
    assert cap and res and res["passes"], "the chosen limit did not pass the holdout: nothing to export"
    path = os.path.join(os.path.dirname(HERE), "models", "crypto_1h.json")
    m = json.load(open(path))
    # published tests of the stream with the new limit, in the usual format (lab.stats)
    stream, g = "crypto_1h", "s2"
    cols, D = lab.load_ds(stream)
    preds = dict(np.load(f"{lab.R}/wf/{stream}_{g}.npz"))
    H = lab.htf_scores(stream, "crypto_4h", "s2")
    gate = lab.make_gate(H, float(m["gate"]["level"]))
    cal = lab._Cal(m["calibration"]["x"], m["calibration"]["y"])
    ds, de = lab.CFG[stream]["dev"]
    dev_tr = lab.simulate(stream, g, D, preds, float(m["threshold"]), ds, de, cal, float(m["grade_a"]), float(m["grade_b"]),
                          max_per_bar=cap, gate=gate)
    frames = {}
    coins = markets.stream_coins("crypto", "1h")
    for sym in coins:
        df = CE.load(data_dir, sym.split("/")[0])
        if df is not None:
            frames[sym] = df
    t1 = frames["BTC/USDT"]["timestamp"].iloc[-1] + pd.Timedelta(hours=1)
    ok = [s for s in coins if s in frames and CE.eligible(frames[s], t1)[0]]
    data, plan, policy = CE.stream_1h(frames, ok, t1)
    _, _, closed = ST.run_policy({s: data[s] for s in ok}, "crypto", "1h", plan, dict(policy, max_per_bar=cap),
                                 start_time=CE.T0, end_time=t1, lookback=10 ** 9)
    hold_tr = [t for lst in closed.values() for t in lst]
    before = {k: m["test"].get(k) for k in ("period", "signals", "signals_per_day", "win_rate", "avg_net_r", "profit_factor")}
    m["selection"]["max_per_bar"] = cap
    m["policy_note"] = m["policy_note"].replace("at most 4 new signals per candle", f"at most {cap} new signals per candle")
    m["walk_forward"] = lab.stats(dev_tr, ds, de, len(D))
    m["test"] = lab.stats(hold_tr, CE.T0, t1, len(ok))
    m["limit_test"] = {
        "script": "research/cap_test.py",
        "question": "more than 4 new signals in the same hour?",
        "rule": "the highest limit whose walk-forward result passes rule v2 is chosen on the validation years; it goes live only "
                "if the year the model never saw reaches >= 70% wins, a positive average after costs and >= 50 signals",
        "validation": {k: {kk: v.get(kk) for kk in ("signals", "signals_per_day", "win_rate", "avg_net_r", "passes_v2", "years")}
                       for k, v in val["limits"].items()},
        "choice": cap, "holdout": {k: {kk: v.get(kk) for kk in ("signals", "signals_per_day", "win_rate", "avg_net_r",
                                                                  "profit_factor", "max_drawdown_r", "passes")}
                                   for k, v in hold["results"].items()},
        "before": dict(before, max_per_bar=4),
    }
    json.dump(m, open(path, "w"), separators=(",", ":"))
    lab.log("exported limit", cap, "test", json.dumps({k: m["test"][k] for k in ("signals", "signals_per_day", "win_rate", "avg_net_r",
                                                                              "max_drawdown_r")}))


if __name__ == "__main__":
    if sys.argv[1] == "validate":
        validate()
    elif sys.argv[1] == "export":
        export(sys.argv[2])
    else:
        holdout(sys.argv[2], sys.argv[3])
