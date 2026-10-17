"""Frequency vs accuracy of the rules engine R1 (the rules the untested streams run) on the VALIDATION years only;
the holdout period is never loaded. Several loosened variants are replayed with the site's own trade plan and costs.
python rules_replay.py <stream>   e.g. crypto_15m, forex_15m, forex_1h, forex_4h
Needs the research data (see README.md in this folder). Writes results/sweep_<stream>.json under RESEARCH_DIR."""
import json, os, sys, time, warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
sys.path.insert(0, HERE)
os.environ.setdefault("RESEARCH_DIR", os.path.join(HERE, "work"))
import lab
import streams as ST
import features as FE
import markets

stream = sys.argv[1]
c = lab.CFG[stream]
ds, de = c["dev"]                         # validation years only
t0 = time.time()
log = lab.log

syms = lab.crypto_symbols() if c["market"] == "crypto" else lab.fx_symbols()
htf = ST.HTF_OF[c["tf"]]
prep = {}
for sym in syms:
    raw = lab.load_crypto(sym, c["data_start"]) if c["market"] == "crypto" else lab.load_fx(sym, c["data_start"])
    raw = raw[raw["timestamp"] < pd.Timestamp(de)].reset_index(drop=True)      # never look past the validation period
    if len(raw) < 5000:
        continue
    b = lab.base_bars(raw, c["tf"])
    hb = FE.resample_closed(raw, htf)
    d = ST.indicators(b)
    d = ST.attach_htf(d, hb, c["tf"], htf)
    crypto = not markets.is_forex(sym)
    sides = {sgn: ST._side_arrays(d, sgn, crypto) for sgn in (1, -1)}
    prep[sym] = (d, sides)
log(stream, "prepared", len(prep), "symbols in", round(time.time() - t0), "s")


def cand_v(d, sides, min_score, mtf_mode, adx_min, rr_min):
    n = len(d)
    out = {"side": np.full(n, "", dtype=object), "score": np.full(n, np.nan), "setup": np.full(n, "", dtype=object)}
    htfv = d["htf"].to_numpy(int)
    adx = d["adx"].to_numpy(float)
    atr = d["atr"].to_numpy(float)
    ok_base = np.isfinite(atr) & (atr > 0) & np.isfinite(d["ema200"].to_numpy(float))
    res = {}
    for sgn, name in ((1, "LONG"), (-1, "SHORT")):
        setup, sc, room = sides[sgn]
        if mtf_mode == "strict":
            mtf = np.where(setup == "reversal", htfv != -sgn, htfv == sgn)
        else:                                   # only not against the higher timeframe
            mtf = htfv != -sgn
        adx_ok = np.where(setup == "reversal", True, adx >= adx_min)
        rr_ok = ~np.isfinite(room) | (room >= rr_min * ST.SL_ATR_MIN)
        good = ok_base & (setup != "") & mtf & adx_ok & rr_ok & (sc >= min_score)
        res[name] = (good, sc, setup)
    gl, sl_, setl = res["LONG"]
    gs, ss, sets = res["SHORT"]
    pick_long = gl & (~gs | (sl_ >= ss))
    pick_short = gs & ~pick_long
    out["side"][pick_long] = "LONG"
    out["side"][pick_short] = "SHORT"
    out["score"] = np.where(pick_long, sl_, np.where(pick_short, ss, np.nan))
    out["setup"] = np.where(pick_long, setl, np.where(pick_short, sets, ""))
    return out


VARIANTS = [
    ("R1 now (score>=60, htf agrees, adx>=15, rr>=1.5)", dict(min_score=60, mtf_mode="strict", adx_min=15, rr_min=1.5)),
    ("score>=50", dict(min_score=50, mtf_mode="strict", adx_min=15, rr_min=1.5)),
    ("score>=40", dict(min_score=40, mtf_mode="strict", adx_min=15, rr_min=1.5)),
    ("score>=50, htf not against", dict(min_score=50, mtf_mode="loose", adx_min=15, rr_min=1.5)),
    ("score>=40, htf not against", dict(min_score=40, mtf_mode="loose", adx_min=15, rr_min=1.5)),
    ("score>=40, htf not against, adx>=10, rr>=1.0", dict(min_score=40, mtf_mode="loose", adx_min=10, rr_min=1.0)),
]
plan = ST.plan_for(c["market"], c["tf"])
policy = ST.policy_for(c["market"], c["tf"])
log("plan", json.dumps({k: plan[k] for k in ("targets_r", "max_bars")}), "policy", json.dumps({k: policy[k] for k in ("max_per_bar", "cooldown")}))
results = []
for name, p in VARIANTS:
    data = {}
    for sym, (d, sides) in prep.items():
        data[sym] = ST.rules_data(sym, d, cand_v(d, sides, **p))
    _, _, closed = ST.run_policy(data, c["market"], c["tf"], plan, policy, start_time=ds, end_time=de,
                                 lookback=10 ** 9, warmup=ST.WARMUP)
    s = lab.stats([t for lst in closed.values() for t in lst], ds, de, len(data))
    results.append({"variant": name, **p, **{k: s.get(k) for k in ("signals", "signals_per_day", "win_rate", "avg_net_r", "stop_rate", "profit_factor", "max_drawdown_r", "years", "by_grade")}})
    log(stream, f"{name:50}", "signals/day", s.get("signals_per_day"), "win%", s.get("win_rate"), "avgR", s.get("avg_net_r"), "stop%", s.get("stop_rate"))
os.makedirs(os.environ["RESEARCH_DIR"] + "/results", exist_ok=True)
json.dump(results, open(os.environ["RESEARCH_DIR"] + f"/results/sweep_{stream}.json", "w"), indent=1)
log("done", stream, round(time.time() - t0), "s")
