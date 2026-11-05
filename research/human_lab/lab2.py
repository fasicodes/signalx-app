"""Round 2 of the human lab: one fix for every reason round 1 failed, tested with the same honest rules.

Round 1 (lab.py): the agents' trade ideas were a coin flip (48% reached a 1R target first), costs took 0.11-0.16R a
trade, more agents agreeing made it worse (late entries), and the one setting that passed validation on 1h was luck.

The fixes (all fixed BEFORE any round 2 holdout number was seen):
1. Coin-flip entries  -> a gradient-boosting head trader (and the logistic one) learns from the agents' readings PLUS
   market context the chart of one coin does not show: Bitcoin's trend and momentum, market breadth (how many of
   the 28 coins are above their 50 average / up over 24 candles), the market's median move and dispersion, and the
   coin's strength against Bitcoin and against the other coins. All of it from candles that had closed.
2. Costs              -> every trade's cost is part of its expected result (a trade is taken only if the expected
   result AFTER costs clears the threshold); results with a 0.12% futures-like cost are reported for information,
   but pass / fail uses SignalX's 0.30%.
3. Obvious levels get hunted -> a new setup, failed_breakout (trap), is in the engine (setups.py).
4. Late entries       -> two other trade plans on the same ideas: "patient" (a limit order 0.25 ATR better than the
   signal price, valid 3 candles, same stop and target; unfilled = no trade) and "1R" (same entry and stop, one
   target at 1R, like SignalX's tested crypto 4h engine).
5. A lucky setting    -> a threshold only passes if it passes the validation rule AND both neighbouring thresholds
   (+-0.05) also made money (>= 40 trades, average > 0, profit factor >= 1.0): a plateau, not a spike.

Protocol: variants = 3 plans x 2 head traders x (all trades | only trades whose costs are at most 0.08R - the
cost fix: the stop must be wide enough for the fees) per timeframe. The cost filter was added after round 2's first
validation look (validation only, never the holdout) showed costs are the biggest lever; the protocol below was then
locked for every timeframe before any round 2 holdout number was computed. Walk-forward inside 2021-03 .. 2024-12 (half-years
from 2022, trained only on trades that had ended). Validation rule as round 1 (>= 80 trades, >= +0.05R, profit
factor >= 1.1, every year 2022-2024 positive) + the plateau rule; the lowest passing threshold; among passing
variants the highest total R on 2022-2024 goes to the holdout. Holdout 2025-01-01 .. 2026-10 is scored once per
timeframe. Because the 1h holdout was already looked at once in round 1, the bar is higher: SignalX's
streams.passed_test() AND a positive average in 2025 AND in 2026.

Usage: python lab2.py val 4h      (validation only)   |   python lab2.py test 4h   (protocol -> results2.json)
"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lab  # noqa: E402
from lab import (HOLD_START, OOF_START, VAL_START, W, passed_test, passes_validation, portfolio,  # noqa: E402
                 stats)
from chart_agents import config as C  # noqa: E402
from chart_agents import journal  # noqa: E402
from chart_agents.head import vector  # noqa: E402
from chart_agents.risk import cost_in_r, simulate  # noqa: E402

PLANS = ("agents", "patient", "1R")
MODELS = ("logit", "gbm")
THRESHOLDS = [round(x, 2) for x in np.arange(-0.10, 1.01, 0.05)]
CTX = ("ctx_btc_r6", "ctx_btc_r24", "ctx_btc_r72", "ctx_btc_above", "ctx_btc_slope", "ctx_breadth50",
       "ctx_breadth24", "ctx_mkt24", "ctx_disp24", "ctx_rs24", "ctx_rs72", "ctx_rank24", "ctx_coin_r24")
PATIENT_ATR, PATIENT_BARS = 0.25, 3
ALT_COST = 0.0012


# ------------------------------------------------------------------------------------------------ market context
def context(tf):
    syms = lab.symbols()
    closes = {}
    for s in syms:
        f = pd.read_pickle(f"{W}/frames/{s}_{tf}.pkl")
        closes[s] = f.set_index("timestamp")["close"]
    cl = pd.DataFrame(closes).sort_index()
    ema = cl.ewm(span=50, adjust=False, ignore_na=True).mean().where(cl.notna())
    r6, r24, r72 = (cl.pct_change(k, fill_method=None) for k in (6, 24, 72))
    above = (cl > ema).astype(float).where(cl.notna())
    out = pd.DataFrame(index=cl.index)
    out["breadth50"] = above.mean(1)
    out["breadth24"] = (r24 > 0).astype(float).where(r24.notna()).mean(1)
    out["mkt24"] = r24.median(1)
    out["disp24"] = r24.std(1)
    out["btc_r6"], out["btc_r24"], out["btc_r72"] = r6["BTC"], r24["BTC"], r72["BTC"]
    out["btc_above"] = above["BTC"] * 2 - 1
    out["btc_slope"] = ema["BTC"] / ema["BTC"].shift(10) - 1
    rank = r24.rank(axis=1, pct=True)
    coin = {"r24": r24, "rs24": r24.sub(r24["BTC"], axis=0), "rs72": r72.sub(r72["BTC"], axis=0), "rank24": rank}
    return out, coin


def add_context(rows, tf):
    mk, coin = context(tf)
    sec = np.timedelta64(C.TF_SEC[tf], "s")
    for r in rows:
        t0 = pd.Timestamp(r["t"] - sec)                # open time of the signal candle (it closed at r["t"])
        sym = r["symbol"].split("/")[0]
        s = r["side"]
        try:
            m = mk.loc[t0]
            g = {k: v.at[t0, sym] for k, v in coin.items()}
        except KeyError:
            r["ctx"] = {k: 0.0 for k in CTX}
            continue

        def f(x):
            return float(x) if x == x else 0.0
        r["ctx"] = {"ctx_btc_r6": f(m["btc_r6"]) * s, "ctx_btc_r24": f(m["btc_r24"]) * s,
                    "ctx_btc_r72": f(m["btc_r72"]) * s, "ctx_btc_above": f(m["btc_above"]) * s,
                    "ctx_btc_slope": f(m["btc_slope"]) * s, "ctx_breadth50": (f(m["breadth50"]) - 0.5) * 2 * s,
                    "ctx_breadth24": (f(m["breadth24"]) - 0.5) * 2 * s, "ctx_mkt24": f(m["mkt24"]) * s,
                    "ctx_disp24": f(m["disp24"]), "ctx_rs24": f(g["rs24"]) * s, "ctx_rs72": f(g["rs72"]) * s,
                    "ctx_rank24": (f(g["rank24"]) - 0.5) * 2 * s, "ctx_coin_r24": f(g["r24"]) * s}


def X(rows):
    return np.array([np.r_[vector(r), [r["ctx"][k] for k in CTX]] for r in rows], float)


# ------------------------------------------------------------------------------------------------ trade plans
class _Arr:
    pass


_cache = {}


def arrays(sym, tf):
    k = (sym, tf)
    if k not in _cache:
        d = pd.read_pickle(f"{W}/frames/{sym}_{tf}.pkl").sort_values("timestamp").reset_index(drop=True)
        a = _Arr()
        a.o, a.h, a.l, a.c = (d[x].to_numpy(float) for x in ("open", "high", "low", "close"))
        a.n, a.tf = len(d), tf
        a.t_close = (d["timestamp"] + pd.Timedelta(seconds=C.TF_SEC[tf])).to_numpy("datetime64[ns]")
        _cache[k] = a
    return _cache[k]


def plan_rows(rows, tf, plan):
    """The same trade ideas played out with another plan. Each row gets net_r / outcome / exit_t / entry / risk
    for that plan (rows that do not trade - an unfilled limit order - are left out)."""
    out = []
    mb = C.TIME_LIMIT_BARS[tf]
    for r in rows:
        pl, side = r["plan"], r["side"]
        sym = r["symbol"].split("/")[0]
        if plan == "agents":
            q = dict(r)
            q["p_entry"], q["p_risk"], q["p_rr"] = pl["entry"], pl["risk"], pl["rr"]
            q["p_cost"] = pl["cost_r"]
            out.append(q)
            continue
        a = arrays(sym, tf)
        i = r["bar"]
        if plan == "1R":
            tgt = pl["entry"] + side * pl["risk"]
            x = simulate(a, i, side, pl["entry"], pl["stop"], tgt, mb, "crypto", r["symbol"])
            if x is None:
                continue
            q = dict(r)
            q["net_r"], q["outcome"], q["bars_held"], q["exit_price"] = x
            q["exit_t"] = a.t_close[i + x[2]]
            q["p_entry"], q["p_risk"], q["p_rr"], q["p_cost"] = pl["entry"], pl["risk"], 1.0, pl["cost_r"]
            out.append(q)
            continue
        # patient: limit order PATIENT_ATR better, valid PATIENT_BARS candles
        lim = pl["entry"] - side * PATIENT_ATR * r["atr"]
        stop, tgt = pl["stop"], pl["target"]
        if (lim - stop) * side <= 0:
            continue
        filled = None
        for j in range(i + 1, min(i + 1 + PATIENT_BARS, a.n)):
            o, h, lo = a.o[j], a.h[j], a.l[j]
            if (side == 1 and o <= stop) or (side == -1 and o >= stop):
                break                                  # gapped through the stop: the order is cancelled
            if (side == 1 and lo <= lim) or (side == -1 and h >= lim):
                filled = (j, min(lim, o) if side == 1 else max(lim, o))
                break
        if filled is None:
            continue
        j, fill = filled
        risk = (fill - stop) * side
        hours_left = mb - (j - i)
        if (side == 1 and a.l[j] <= stop) or (side == -1 and a.h[j] >= stop):
            net = -1.0 - cost_in_r("crypto", r["symbol"], fill, risk, C.TF_SEC[tf] / 3600, side)
            x = (net, "stop", j - i, stop)
            exit_t = a.t_close[j]
        else:
            y = simulate(a, j, side, fill, stop, tgt, hours_left, "crypto", r["symbol"])
            if y is None:
                continue
            x = (y[0], y[1], y[2] + (j - i), y[3])
            exit_t = a.t_close[j + y[2]]
        q = dict(r)
        q["net_r"], q["outcome"], q["bars_held"], q["exit_price"] = x
        q["exit_t"] = exit_t
        q["p_entry"], q["p_risk"] = fill, risk
        q["p_rr"] = (tgt - fill) * side / risk
        q["p_cost"] = cost_in_r("crypto", r["symbol"], fill, risk, mb * C.TF_SEC[tf] / 3600 / 2, side)
        out.append(q)
    return out


def alt_cost(t):
    """The same trade with a 0.12% round trip instead of 0.30% (information only)."""
    return t["net_r"] + (0.0030 - ALT_COST) * t["p_entry"] / t["p_risk"]


# ------------------------------------------------------------------------------------------------ head traders
def fit(model, Xtr, ytr):
    if model == "logit":
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        m = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=3000))
    else:
        from sklearn.ensemble import HistGradientBoostingClassifier
        m = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, min_samples_leaf=80,
                                           l2_regularization=1.0, random_state=0)
    m.fit(Xtr, ytr)
    return m


def exp_r(m, rows, Xs):
    p = m.predict_proba(Xs)[:, 1]
    rr = np.array([r["p_rr"] for r in rows])
    cost = np.array([r["p_cost"] for r in rows])
    return p * (rr - cost) - (1 - p) * (1 + cost)


def walk_forward(rows, model):
    val = [r for r in rows if VAL_START <= r["t"] < HOLD_START]
    Xv = X(val)
    yv = np.array([r["net_r"] > 0 for r in val], int)
    t = np.array([r["t"] for r in val])
    ex = np.array([r["exit_t"] for r in val])
    folds = pd.date_range("2022-01-01", "2025-01-01", freq="6MS").to_numpy("datetime64[ns]")
    score = np.full(len(val), np.nan)
    for a, b in zip(folds[:-1], folds[1:]):
        tr = ex < a
        te = (t >= a) & (t < b)
        if tr.sum() < 300 or not te.any() or len(set(yv[tr])) < 2:
            continue
        m = fit(model, Xv[tr], yv[tr])
        score[te] = exp_r(m, [val[k] for k in np.flatnonzero(te)], Xv[te])
    keep = ~np.isnan(score)
    return [val[k] for k in np.flatnonzero(keep)], score[keep]


def grid(oof, sc, tf):
    days = float((np.datetime64("2025-01-01") - OOF_START) / np.timedelta64(1, "D"))
    res = []
    for th in THRESHOLDS:
        sel = sc >= th
        rows = [oof[k] for k in np.flatnonzero(sel)]
        tr = portfolio(rows, list(sc[sel]), tf)
        s = stats(tr, days)
        res.append({"threshold": th, "rule": passes_validation(s), **{k: s.get(k) for k in
                    ("signals", "win_rate", "avg_net_r", "total_net_r", "profit_factor", "max_drawdown_r",
                     "signals_per_day")}, "years": s.get("years")})
    for k, g in enumerate(res):
        nb = [res[j] for j in (k - 1, k + 1) if 0 <= j < len(res)]
        g["plateau"] = all((n["signals"] or 0) >= 40 and (n["avg_net_r"] or -1) > 0 and (n["profit_factor"] or 0) >= 1.0
                           for n in nb) and len(nb) == 2
        g["pass"] = bool(g["rule"] and g["plateau"])
    return res


# ------------------------------------------------------------------------------------------------ run
def prepare(tf):
    rows, vet = lab.load(tf)
    rows = lab._clean(rows)
    add_context(rows, tf)
    return rows, vet


CHEAP_COST_R = 0.08        # fix for costs: only trades whose stop is wide enough that costs are at most 0.08R


def variants():
    return [(plan, model, cheap) for plan in PLANS for model in MODELS for cheap in (False, True)]


def vkey(plan, model, cheap):
    return f"{plan}|{model}|{'cheap' if cheap else 'all'}"


def validation(tf, rows=None):
    rows = rows or prepare(tf)[0]
    out = {"plans": {}, "variants": {}}
    for plan in PLANS:
        pr = plan_rows(rows, tf, plan)
        out["plans"][plan] = pr
        val = [r for r in pr if OOF_START <= r["t"] < HOLD_START]
        cheap_val = [r for r in val if r["p_cost"] <= CHEAP_COST_R]
        out[f"baseline|{plan}|all"] = stats(portfolio(val, [0.0] * len(val), tf))
        out[f"baseline|{plan}|cheap"] = stats(portfolio(cheap_val, [0.0] * len(cheap_val), tf))
        for model in MODELS:
            for cheap in (False, True):
                use = [r for r in pr if r["p_cost"] <= CHEAP_COST_R] if cheap else pr
                oof, sc = walk_forward(use, model)
                out["variants"][vkey(plan, model, cheap)] = {"grid": grid(oof, sc, tf)}
    return out


def _brief(g):
    return {k: g[k] for k in ("threshold", "signals", "avg_net_r", "profit_factor", "total_net_r", "rule", "plateau")}


def show(tf, v):
    for plan in PLANS:
        b, bw = v[f"baseline|{plan}|all"], v[f"baseline|{plan}|cheap"]
        print(f"{tf} {plan:8s} every idea 2022-24: n={b['signals']} win={b.get('win_rate')} avg={b.get('avg_net_r')}"
              f" | cheap trades only: n={bw.get('signals')} avg={bw.get('avg_net_r')}")
        for model in MODELS:
            for cheap in (False, True):
                gs = v["variants"][vkey(plan, model, cheap)]["grid"]
                best = max((g for g in gs if (g["signals"] or 0) >= 80), key=lambda g: g["avg_net_r"], default=None)
                passing = [g["threshold"] for g in gs if g["pass"]]
                print(f"    {model:5s} {'cheap' if cheap else 'all  '} passing={passing} best={best and _brief(best)}")


def test(tf):
    rows, vet = prepare(tf)
    v = validation(tf, rows)
    show(tf, v)
    cands = []
    for plan, model, cheap in variants():
        p = next((g for g in v["variants"][vkey(plan, model, cheap)]["grid"] if g["pass"]), None)
        if p:
            cands.append((p["total_net_r"], plan, model, cheap, p))
    result = {"tf": tf, "round": 2, "generated": str(pd.Timestamp.now("UTC"))[:10], "engine": C.VERSION,
              "coins": len({r["symbol"] for r in rows}),
              "baselines": {k: {x: s.get(x) for x in ("signals", "win_rate", "avg_net_r", "profit_factor")}
                            for k, s in v.items() if k.startswith("baseline|")},
              "variants": {k: x["grid"] for k, x in v["variants"].items()},
              "passing_variants": [{"variant": vkey(pl, mo, ch), "threshold": p["threshold"],
                                    "signals": p["signals"], "avg_net_r": p["avg_net_r"],
                                    "total_net_r": p["total_net_r"]} for _, pl, mo, ch, p in cands],
              "live": False}
    if not cands:
        result["choice"] = None
        result["reason"] = "No variant passed the validation rule with a plateau (2022-2024 walk-forward)."
        _save(tf, result)
        return result
    cands.sort(key=lambda x: -x[0])
    _, plan, model, cheap, pick = cands[0]
    result["choice"] = {"variant": vkey(plan, model, cheap), "plan": plan, "model": model, "cheap_only": cheap,
                        "threshold": pick["threshold"]}
    pr = v["plans"][plan]
    if cheap:
        pr = [r for r in pr if r["p_cost"] <= CHEAP_COST_R]
    train = [r for r in pr if VAL_START <= r["t"] < HOLD_START and r["exit_t"] < HOLD_START]
    m = fit(model, X(train), np.array([r["net_r"] > 0 for r in train], int))
    hold = [r for r in pr if r["t"] >= HOLD_START]
    sc = exp_r(m, hold, X(hold)) if hold else np.array([])
    sel = sc >= pick["threshold"]
    chosen = [hold[k] for k in np.flatnonzero(sel)]
    taken = portfolio(chosen, list(sc[sel]), tf)
    last = max(r["t"] for r in hold) if hold else HOLD_START
    days = float((last - HOLD_START) / np.timedelta64(1, "D")) or 1.0
    hs = stats(taken, days)
    hs["period"] = f"2025-01-01 to {str(last)[:10]}"
    hs["cost_round_trip"] = 0.003
    alt = [dict(t, net_r=alt_cost(t)) for t in taken]
    hs["with_012pct_cost"] = {k: stats(alt).get(k) for k in ("signals", "win_rate", "avg_net_r", "profit_factor")}
    y = hs.get("years", {})
    both_years = all(y.get(k, {}).get("avg_net_r", -1) > 0 for k in (2025, 2026))
    result["holdout"] = hs
    result["holdout_by_setup"] = journal.by_setup(taken) if taken else {}
    result["live"] = bool(taken and passed_test(hs) and both_years)
    result["reason"] = ("Passed: the holdout met SignalX's test and made money in 2025 and in 2026." if result["live"]
                        else "Failed the holdout (SignalX's test, or not positive in both 2025 and 2026): no signals.")
    if taken:
        from chart_agents.head import HeadTrader
        ht = HeadTrader("learned")
        result["examples"] = [{"time": str(t["t"])[:16], "symbol": t["symbol"], "side": t["side"],
                               "net_r": round(t["net_r"], 2), "outcome": t["outcome"],
                               "explanation": ht.explain(t)} for t in taken[-6:]]
    if result["live"]:
        import pickle
        os.makedirs(f"{W}/models", exist_ok=True)
        with open(f"{W}/models/human_crypto_{tf}.pkl", "wb") as fh:
            pickle.dump({"model": m, "variant": result["choice"], "features": "head.FEATURES + lab2.CTX",
                         "test": hs}, fh)
    _save(tf, result)
    return result


def _save(tf, result):
    path = f"{HERE}/results2.json"
    allr = {}
    if os.path.exists(path):
        with open(path) as fh:
            allr = json.load(fh)
    allr[f"crypto_{tf}"] = result
    with open(path, "w") as fh:
        json.dump(allr, fh, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))


if __name__ == "__main__":
    if sys.argv[1] == "val":
        show(sys.argv[2], validation(sys.argv[2]))
    elif sys.argv[1] == "test":
        r = test(sys.argv[2])
        print(json.dumps({k: r.get(k) for k in ("tf", "choice", "live", "reason")}, indent=1, default=str))
        if "holdout" in r:
            print(json.dumps({k: r["holdout"].get(k) for k in ("signals", "win_rate", "avg_net_r", "profit_factor",
                                                              "max_drawdown_r", "total_net_r", "signals_per_day",
                                                              "years", "with_012pct_cost")}, default=str))
