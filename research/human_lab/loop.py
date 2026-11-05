"""The research loop: find a reason the agents lose -> try a fix -> test it -> next reason.

Every test here uses the validation years only (trades from 2021-03-01 to 2024-12-31). 2025-2026 is never looked at
in this file. An idea counts as "promising" only if it holds in EVERY validation year (2022, 2023, 2024 positive)
with >= 80 trades, >= +0.05R per trade after 0.30% costs and a profit factor >= 1.1. Every try is written to
loop_log.json with its number, so the final judgement can account for how many things were tried. Promising ideas
then get ONE final test on coins that were never used anywhere (fresh coins, 2025-2026) - see final_fresh() - and
after that a forward test on the site's demo account.

Usage: python loop.py run 4h        # all loop iterations on 4h (validation only)
"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lab  # noqa: E402
import lab2  # noqa: E402
from chart_agents import config as C  # noqa: E402
from chart_agents.risk import cost_in_r  # noqa: E402

LOG = f"{HERE}/loop_log.json"
YEARS = (2022, 2023, 2024)


# ------------------------------------------------------------------------------------------------ scoring
def judge(trades):
    s = lab.stats(trades)
    if not s.get("signals"):
        return {"signals": 0, "promising": False}
    ok = (s["signals"] >= 80 and s["avg_net_r"] >= 0.05 and (s["profit_factor"] or 0) >= 1.1 and
          all(s["years"].get(y, {}).get("avg_net_r", -1) > 0 for y in YEARS))
    return {"signals": s["signals"], "win_rate": s["win_rate"], "avg_net_r": s["avg_net_r"],
            "profit_factor": s["profit_factor"], "total_net_r": s["total_net_r"], "max_drawdown_r": s["max_drawdown_r"],
            "years": {y: v["avg_net_r"] for y, v in s["years"].items()}, "promising": bool(ok)}


def run_portfolio(rows, tf, score=None):
    rows = [r for r in rows if lab.VAL_START <= r["t"] < lab.HOLD_START]
    sc = [score(r) for r in rows] if score else [0.0] * len(rows)
    return lab.portfolio(rows, sc, tf)


def log(entry):
    allr = []
    if os.path.exists(LOG):
        with open(LOG) as fh:
            allr = json.load(fh)
    entry["try"] = len(allr) + 1
    allr.append(entry)
    with open(LOG, "w") as fh:
        json.dump(allr, fh, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    y = entry["result"].get("years", {})
    print(f"#{entry['try']:3d} {entry['tf']:3s} {entry['name']:46s} n={entry['result'].get('signals', 0):5d} "
          f"avg={entry['result'].get('avg_net_r')} pf={entry['result'].get('profit_factor')} "
          f"years={ {k: y[k] for k in sorted(y)} } {'PROMISING' if entry['result'].get('promising') else ''}",
          flush=True)


# ------------------------------------------------------------------------------------------------ exits
def atr_array(sym, tf):
    a = lab2.arrays(sym, tf)
    if not hasattr(a, "atr"):
        pc = np.r_[a.c[0], a.c[:-1]]
        tr = np.maximum(a.h - a.l, np.maximum(np.abs(a.h - pc), np.abs(a.l - pc)))
        a.atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy(float)
    return a


def trail(rows, tf, be_at=1.0, trail_atr=2.5, hold_mult=2.0):
    """Same entry and stop; no fixed target. The stop goes to the entry once the trade is be_at R in profit, then
    trails trail_atr ATR behind the best price (updated at each candle's close, used from the next candle).
    Pessimistic: the stop is checked first on every candle; a gap through it exits at the open."""
    out = []
    mb = int(C.TIME_LIMIT_BARS[tf] * hold_mult)
    for r in rows:
        sym = r["symbol"].split("/")[0]
        a = atr_array(sym, tf)
        i, side, pl = r["bar"], r["side"], r["plan"]
        entry, stop = pl["entry"], pl["stop"]
        risk = (entry - stop) * side
        cur, best = stop, entry
        end = min(a.n - 1, i + mb)
        px, j, how = None, i, None
        for j in range(i + 1, end + 1):
            o, h, lo, c = a.o[j], a.h[j], a.l[j], a.c[j]
            if side == 1:
                if o <= cur:
                    px, how = o, "stop"
                elif lo <= cur:
                    px, how = cur, "stop"
            else:
                if o >= cur:
                    px, how = o, "stop"
                elif h >= cur:
                    px, how = cur, "stop"
            if px is not None:
                break
            best = max(best, h) if side == 1 else min(best, lo)
            if (best - entry) * side >= be_at * risk:
                cur = max(cur, entry) if side == 1 else min(cur, entry)
            t = best - side * trail_atr * a.atr[j]
            cur = max(cur, t) if side == 1 else min(cur, t)
        if px is None:
            if end < i + mb and end == a.n - 1:
                continue
            px, how, j = a.c[end], "time", end
        gross = (px - entry) * side / risk
        net = gross - cost_in_r("crypto", r["symbol"], entry, risk, (j - i) * C.TF_SEC[tf] / 3600, side)
        q = dict(r)
        q["net_r"], q["outcome"], q["exit_t"] = float(net), ("target" if net > 0 else how), a.t_close[j]
        out.append(q)
    return out


# ------------------------------------------------------------------------------------------------ the loop
def run(tf):
    rows, _ = lab2.prepare(tf)
    val = [r for r in rows if lab.VAL_START <= r["t"] < lab.HOLD_START]
    f = {
        "every idea (baseline)": lambda r: True,
        # problem 1: the market's mood changes -> trade only with Bitcoin and with most coins
        "regime: with Bitcoin trend": lambda r: r["ctx"]["ctx_btc_above"] > 0 and r["ctx"]["ctx_btc_slope"] > 0,
        "regime: with breadth (>60% coins)": lambda r: r["ctx"]["ctx_breadth50"] > 0.2,
        "regime: Bitcoin + breadth": lambda r: (r["ctx"]["ctx_btc_above"] > 0 and r["ctx"]["ctx_btc_slope"] > 0
                                                and r["ctx"]["ctx_breadth50"] > 0.2),
        # problem 2: coins move together -> long the strongest, short the weakest
        "strength: coin in top 30% (bottom for shorts)": lambda r: r["ctx"]["ctx_rank24"] > 0.4,
        "strength: beats Bitcoin over 72 candles": lambda r: r["ctx"]["ctx_rs72"] > 0,
        "strength: top 30% + beats Bitcoin": lambda r: r["ctx"]["ctx_rank24"] > 0.4 and r["ctx"]["ctx_rs72"] > 0,
        "regime + strength": lambda r: (r["ctx"]["ctx_btc_above"] > 0 and r["ctx"]["ctx_breadth50"] > 0.2 and
                                        r["ctx"]["ctx_rank24"] > 0.4),
        # problem 3 (costs) combined with the above
        "cheap trades + regime + strength": lambda r: (r["plan"]["cost_r"] <= 0.08 and r["ctx"]["ctx_btc_above"] > 0
                                                       and r["ctx"]["ctx_breadth50"] > 0.2
                                                       and r["ctx"]["ctx_rank24"] > 0.4),
    }
    results = {}
    for name, cond in f.items():
        sel = [r for r in val if cond(r)]
        res = judge(run_portfolio(sel, tf))
        results[name] = res
        log({"tf": tf, "name": name, "exit": "agents plan", "result": res})
    # problem 4: the fixed target cuts winners short -> trailing stop, on the same filters
    for name in ("every idea (baseline)", "regime: Bitcoin + breadth", "strength: top 30% + beats Bitcoin",
                 "regime + strength", "cheap trades + regime + strength"):
        sel = [r for r in val if f[name](r)]
        tr = trail(sel, tf)
        res = judge(run_portfolio(tr, tf))
        log({"tf": tf, "name": name + " | trailing stop", "exit": "trail 2.5 ATR, breakeven at 1R",
             "result": res})
    return results


if __name__ == "__main__":
    if sys.argv[1] == "run":
        run(sys.argv[2])


def run_momentum(tf):
    """Loop round 2 - problem found: the human rules (buy the dip, wait for a deep pullback, do not chase) work
    AGAINST crypto, where moves keep going (validation: high RSI / far above the 20 average / strong ADX did better
    in every year 2022-2024). Fix: momentum agents - only ideas in the direction of a strong move. Fixed levels
    (not fitted): RSI >= 60 for longs (<= 40 shorts), price at least 1 ATR beyond its 20 average in the trade's
    direction, ADX >= 25 with +DI/-DI agreeing, the coin up (down) over 24 candles."""
    rows, _ = lab2.prepare(tf)
    val = [r for r in rows if lab.VAL_START <= r["t"] < lab.HOLD_START]
    F = lambda r: r["feats"]  # noqa: E731
    mom = {
        "momentum: RSI with the trade (>=60 / <=40)": lambda r: F(r)["rsi"] >= 0.2,
        "momentum: >= 1 ATR beyond the 20 average": lambda r: F(r)["stretch"] >= 1.0,
        "momentum: ADX >= 25 and DI agrees": lambda r: F(r)["adx"] >= 0.5 and F(r)["di"] > 0,
        "momentum: RSI + stretch + ADX": lambda r: F(r)["rsi"] >= 0.2 and F(r)["stretch"] >= 1.0
                                                    and F(r)["adx"] >= 0.5 and F(r)["di"] > 0,
        "momentum: all + coin moving with the trade": lambda r: F(r)["rsi"] >= 0.2 and F(r)["stretch"] >= 1.0
                                                    and F(r)["adx"] >= 0.5 and F(r)["di"] > 0
                                                    and r["ctx"]["ctx_coin_r24"] > 0,
        "momentum all + regime": lambda r: F(r)["rsi"] >= 0.2 and F(r)["stretch"] >= 1.0 and F(r)["adx"] >= 0.5
                                        and F(r)["di"] > 0 and r["ctx"]["ctx_btc_above"] > 0
                                        and r["ctx"]["ctx_breadth50"] > 0.2,
        "momentum all + regime + cheap": lambda r: F(r)["rsi"] >= 0.2 and F(r)["stretch"] >= 1.0
                                                and F(r)["adx"] >= 0.5 and F(r)["di"] > 0
                                                and r["ctx"]["ctx_btc_above"] > 0 and r["ctx"]["ctx_breadth50"] > 0.2
                                                and r["plan"]["cost_r"] <= 0.08,
        "momentum all + one setup only (no crowd)": lambda r: F(r)["rsi"] >= 0.2 and F(r)["stretch"] >= 1.0
                                                and F(r)["adx"] >= 0.5 and F(r)["di"] > 0 and F(r)["n_setups"] == 1,
    }
    for name, cond in mom.items():
        sel = [r for r in val if cond(r)]
        log({"tf": tf, "name": name, "exit": "agents plan", "result": judge(run_portfolio(sel, tf))})
    for name in ("momentum: RSI + stretch + ADX", "momentum all + regime"):
        sel = [r for r in val if mom[name](r)]
        log({"tf": tf, "name": name + " | 1R target", "exit": "1R",
             "result": judge(run_portfolio(lab2.plan_rows(sel, tf, "1R"), tf))})
        log({"tf": tf, "name": name + " | trailing stop", "exit": "trail",
             "result": judge(run_portfolio(trail(sel, tf), tf))})


if __name__ == "__main__" and sys.argv[1] == "momentum":
    run_momentum(sys.argv[2])


def wide_rows(rows, tf, k_atr=2.5, tgt_r=1.0):
    """Same entry; a wide volatility stop (k_atr ATR, like SignalX's tested crypto 4h engine) and one target at
    tgt_r R. Costs become a small share of the risk."""
    from chart_agents.risk import simulate
    out = []
    for r in rows:
        a = lab2.arrays(r["symbol"].split("/")[0], tf)
        side, e, atr = r["side"], r["plan"]["entry"], r["atr"]
        stop = e - side * k_atr * atr
        tgt = e + side * tgt_r * k_atr * atr
        x = simulate(a, r["bar"], side, e, stop, tgt, C.TIME_LIMIT_BARS[tf], "crypto", r["symbol"])
        if x is None:
            continue
        q = dict(r)
        q["net_r"], q["outcome"] = x[0], x[1]
        q["exit_t"] = a.t_close[r["bar"] + x[2]]
        out.append(q)
    return out


def run_wide(tf):
    """Loop round 3 - problem found: the agents' structural stops are tight (median 3.5% on 4h vs ~7% for the
    tested 4h engine), so normal noise and costs hit them. Fix: wide 2.5 ATR stop + 1R target on the same ideas."""
    rows, _ = lab2.prepare(tf)
    val = [r for r in rows if lab.VAL_START <= r["t"] < lab.HOLD_START]
    F = lambda r: r["feats"]  # noqa: E731
    mom = lambda r: F(r)["rsi"] >= 0.2 and F(r)["stretch"] >= 1.0 and F(r)["adx"] >= 0.5 and F(r)["di"] > 0  # noqa
    reg = lambda r: r["ctx"]["ctx_btc_above"] > 0 and r["ctx"]["ctx_breadth50"] > 0.2  # noqa: E731
    tests = {"every idea": lambda r: True, "regime": reg, "momentum": mom,
             "momentum + regime": lambda r: mom(r) and reg(r),
             "momentum + regime + strength": lambda r: mom(r) and reg(r) and r["ctx"]["ctx_rank24"] > 0.4}
    for k_atr, tgt in ((2.5, 1.0), (2.5, 1.5)):
        for name, cond in tests.items():
            sel = [r for r in val if cond(r)]
            log({"tf": tf, "name": f"wide {k_atr} ATR stop, {tgt}R | {name}", "exit": f"wide {k_atr}/{tgt}",
                 "result": judge(run_portfolio(wide_rows(sel, tf, k_atr, tgt), tf))})


if __name__ == "__main__" and sys.argv[1] == "wide":
    run_wide(sys.argv[2])


def random_rows(tf, every=6, seed=0):
    """Control group: trades on candles picked without any agent (every `every`-th candle of every coin, a random
    direction), with the same wide plan. If the agents' ideas do no better than these, the agents add nothing."""
    rng = np.random.default_rng(seed)
    mk, coin = lab2.context(tf)
    out = []
    sec = np.timedelta64(C.TF_SEC[tf], "s")
    for sym in lab.symbols():
        a = atr_array(sym, tf)
        t_open = a.t_close - sec
        for i in range(300, a.n - 1, every):
            t = a.t_close[i]
            if not (lab.VAL_START <= t < lab.HOLD_START):
                continue
            side = int(rng.choice((1, -1)))
            t0 = pd.Timestamp(t_open[i])
            try:
                m = mk.loc[t0]
                rk = coin["rank24"].at[t0, sym]
            except KeyError:
                continue
            ctx = {"ctx_btc_above": float(m["btc_above"]) * side if m["btc_above"] == m["btc_above"] else 0.0,
                   "ctx_breadth50": (float(m["breadth50"]) - 0.5) * 2 * side if m["breadth50"] == m["breadth50"] else 0.0,
                   "ctx_rank24": (float(rk) - 0.5) * 2 * side if rk == rk else 0.0}
            out.append({"symbol": f"{sym}/USDT", "bar": i, "side": side, "t": t, "atr": float(a.atr[i]),
                        "plan": {"entry": float(a.c[i])}, "ctx": ctx, "setups": ["random"], "scores": {}})
    return out


def run_control(tf):
    """Loop round 4: is the edge the agents' or just the market regime's?"""
    rows, _ = lab2.prepare(tf)
    val = [r for r in rows if lab.VAL_START <= r["t"] < lab.HOLD_START]
    reg = lambda r: r["ctx"]["ctx_btc_above"] > 0 and r["ctx"]["ctx_breadth50"] > 0.2  # noqa: E731
    strong = lambda r: r["ctx"]["ctx_btc_above"] > 0 and r["ctx"]["ctx_breadth50"] > 0.4  # noqa: E731
    rnd = random_rows(tf)
    for label, src in (("agents' ideas", val), ("random candles (no agents)", rnd)):
        for name, cond in (("regime", reg), ("strong regime (>70% coins)", strong)):
            sel = [r for r in src if cond(r)]
            log({"tf": tf, "name": f"wide 2.5/1.5R | {name} | {label}", "exit": "wide 2.5/1.5",
                 "result": judge(run_portfolio(wide_rows(sel, tf, 2.5, 1.5), tf))})


if __name__ == "__main__" and sys.argv[1] == "control":
    run_control(sys.argv[2])
