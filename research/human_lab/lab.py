"""Honest test of the human-style chart engine (chart_agents) on crypto 15m / 1h / 4h.

Not part of the web app. It writes research/human_lab/results.json and, only for a stream that passes,
models/human_crypto_<tf>.json (the head trader the site would run).

Protocol (fixed BEFORE the holdout was looked at; the same spirit as every SignalX stream):
* Data: Binance spot 15-minute candles of 28 coins, Jan 2021 - Oct 2026 (research/fetch_crypto.py), with taker buy
  volume and quote volume. 1h / 4h / 1d / 1w are built from them; a higher-timeframe candle is only used after it
  closed.
* Every candle of every coin is replayed by the engine; every trade idea the agents produce is played out with
  the risk manager's stop and target: entry at the signal candle's close, pessimistic fills (stop first when a
  candle touches both, a gap through the stop exits at the open), time limit 24 h / 3 days / 7 days, costs from
  SignalX markets.py (0.30% per round trip + futures funding for longs).
* Validation: candles from 2021-03-01 to 2024-12-31. Holdout: 2025-01-01 to the end of the data, used ONCE.
* Head trader, learned version: walk-forward inside the validation years - for each half-year from 2022 on, the
  logistic regression is trained only on trades that had already ENDED before that half-year started, then
  scores it. Checklist version: nothing is fitted (N agents for the trade).
* Selection on the walk-forward results (2022-2024), after the portfolio rules: the lowest threshold (learned) /
  lowest N (checklist) with at least 80 trades, an average of at least +0.05R, a profit factor of at least 1.1 and
  a positive average in every year 2022, 2023 and 2024. If both versions pass, the one with the higher total R on
  2022-2024 goes to the holdout. Strong / standard: strong = the top 40% of the chosen version's validation scores.
* The chosen version is scored once on the holdout (learned: re-trained on all validation trades that ended
  before 2025-01-01). The stream may send signals only if the holdout passes SignalX's streams.passed_test():
  at least 50 signals, at least +0.05R per signal after costs, profit factor at least 1.1 and a drawdown smaller
  than half of the total. Otherwise it sends none.

Usage:
  python lab.py prep                 # work/frames/<COIN>_<tf>.pkl from work/data/crypto/<COIN>_15m.csv.gz
  python lab.py cands 4h             # every trade idea of every coin, played out (work/cands/4h_<COIN>.pkl)
  python lab.py test 4h              # walk-forward, selection, holdout -> results.json (+ model if it passes)
"""
import glob
import json
import os
import pickle
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
from chart_agents import config as C  # noqa: E402
from chart_agents import journal  # noqa: E402
from chart_agents.data import resample  # noqa: E402
from chart_agents.engine import Engine  # noqa: E402
from chart_agents.head import HeadTrader, confluence, vector  # noqa: E402
from chart_agents.risk import Portfolio, simulate  # noqa: E402

W = os.environ.get("HUMAN_LAB_DIR") or os.path.join(HERE, "work")
VAL_START, HOLD_START = np.datetime64("2021-03-01"), np.datetime64("2025-01-01")
OOF_START = np.datetime64("2022-01-01")
OOF_YEARS = (2022, 2023, 2024)
MIN_VAL_TRADES = 80
THRESHOLDS = [round(x, 2) for x in np.arange(-0.10, 1.01, 0.05)]
CONFLUENCE = (3, 4, 5, 6, 7, 8)
TF_ORDER = {"15m": ("1h", "4h", "1d", "1w"), "1h": ("4h", "1d", "1w"), "4h": ("1d", "1w")}


# ------------------------------------------------------------------------------------------------ data
def prep():
    os.makedirs(f"{W}/frames", exist_ok=True)
    for f in sorted(glob.glob(f"{W}/data/crypto/*_15m.csv.gz")):
        sym = os.path.basename(f).split("_")[0]
        d = pd.read_csv(f, parse_dates=["timestamp"])
        d = d[["timestamp", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume"]]
        d = d.sort_values("timestamp").reset_index(drop=True)
        d.to_pickle(f"{W}/frames/{sym}_15m.pkl")
        for tf in ("1h", "4h", "1d", "1w"):
            resample(d, tf).to_pickle(f"{W}/frames/{sym}_{tf}.pkl")
        print(sym, len(d), flush=True)


def symbols():
    return sorted({os.path.basename(f).split("_")[0] for f in glob.glob(f"{W}/frames/*_15m.pkl")})


def _frames(sym, tf):
    need = (tf,) + TF_ORDER[tf]
    return {h: pd.read_pickle(f"{W}/frames/{sym}_{h}.pkl") for h in need}


# ------------------------------------------------------------------------------------------------ candidates
def _cands_one(args):
    sym, tf = args
    out = f"{W}/cands/{tf}_{sym}.pkl"
    if os.path.exists(out):
        return sym, None
    t0 = time.time()
    eng = Engine(_frames(sym, tf), tf, f"{sym}/USDT", "crypto")
    cands = eng.run()
    ch = eng.ch
    rows = []
    for c in cands:
        pl = c["plan"]
        r = simulate(ch, c["bar"], c["side"], pl["entry"], pl["stop"], pl["target"], C.TIME_LIMIT_BARS[tf],
                     "crypto", c["symbol"])
        if r is None:
            continue
        c["net_r"], c["outcome"], c["bars_held"], c["exit_price"] = r
        c["t"] = ch.t_close[c["bar"]]
        c["exit_t"] = ch.t_close[c["bar"] + r[2]]
        rows.append(c)
    with open(out, "wb") as fh:
        pickle.dump({"rows": rows, "vetoed": eng.vetoed, "veto_reasons": eng.veto_reasons, "bars": ch.n}, fh)
    return sym, (len(rows), round(time.time() - t0, 1))


def cands(tf, procs=2):
    os.makedirs(f"{W}/cands", exist_ok=True)
    with Pool(procs) as p:
        for sym, info in p.imap_unordered(_cands_one, [(s, tf) for s in symbols()]):
            print(tf, sym, info, flush=True)


def load(tf):
    rows, vet = [], {}
    for f in sorted(glob.glob(f"{W}/cands/{tf}_*.pkl")):
        with open(f, "rb") as fh:
            d = pickle.load(fh)
        rows += d["rows"]
        for k, v in d["veto_reasons"].items():
            vet[k] = vet.get(k, 0) + v
    rows.sort(key=lambda r: (r["t"], r["symbol"]))
    return rows, vet


# ------------------------------------------------------------------------------------------------ evaluation
def portfolio(rows, scores, tf):
    """The SignalX portfolio rules over the chosen trades (time order; on one candle the best scores first)."""
    pf = Portfolio(tf)
    taken = []
    by_t = {}
    for r, s in zip(rows, scores):
        by_t.setdefault(r["t"], []).append((s, r))
    for t in sorted(by_t):
        new = 0
        for s, r in sorted(by_t[t], key=lambda x: -x[0]):
            ok, _ = pf.allow(r["symbol"], r["side"], t, new)
            if not ok:
                continue
            pf.take(r["symbol"], r["side"], r["exit_t"], r["net_r"])
            taken.append(r)
            new += 1
    return taken


def stats(trades, days=None):
    if not trades:
        return {"signals": 0}
    r = np.array([t["net_r"] for t in trades], float)
    eq = np.cumsum(r)
    dd = float((eq - np.maximum.accumulate(np.r_[0.0, eq])[1:]).min())
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    years = {}
    for t in trades:
        y = int(str(t["t"])[:4])
        years.setdefault(y, []).append(t["net_r"])
    quarters = {}
    for t in trades:
        ts = pd.Timestamp(t["t"])
        quarters.setdefault(f"{ts.year}-Q{(ts.month - 1) // 3 + 1}", []).append(t["net_r"])
    out = {"signals": int(len(r)), "win_rate": round(100 * float((r > 0).mean()), 1),
           "target_rate": round(100 * float(np.mean([t["outcome"] == "target" for t in trades])), 1),
           "stop_rate": round(100 * float(np.mean([t["outcome"] == "stop" for t in trades])), 1),
           "avg_net_r": round(float(r.mean()), 3), "total_net_r": round(float(r.sum()), 1),
           "profit_factor": round(float(wins / losses), 2) if losses > 0 else None,
           "max_drawdown_r": round(dd, 1),
           "avg_win_r": round(float(r[r > 0].mean()), 2) if (r > 0).any() else None,
           "avg_loss_r": round(float(r[r < 0].mean()), 2) if (r < 0).any() else None,
           "years": {y: {"signals": len(v), "avg_net_r": round(float(np.mean(v)), 3),
                         "win_rate": round(100 * float(np.mean(np.array(v) > 0)), 1)} for y, v in sorted(years.items())},
           "quarters": {q: {"signals": len(v), "avg_net_r": round(float(np.mean(v)), 3)}
                        for q, v in sorted(quarters.items())}}
    if days:
        out["signals_per_day"] = round(len(r) / days, 2)
    return out


def passes_validation(s):
    if s.get("signals", 0) < MIN_VAL_TRADES or s["avg_net_r"] < 0.05:
        return False
    if s["profit_factor"] is None or s["profit_factor"] < 1.1:
        return False
    return all(s["years"].get(y, {}).get("avg_net_r", -1) > 0 for y in OOF_YEARS)


def passed_test(s):
    """SignalX streams.passed_test() (crypto costs are already the real ones here)."""
    try:
        import streams
        return bool(streams.passed_test(s))
    except Exception:
        if s.get("signals", 0) < 50 or s["avg_net_r"] < 0.05 or (s["profit_factor"] or 0) < 1.1:
            return False
        return abs(s["max_drawdown_r"]) <= 0.5 * max(s["total_net_r"], 0.0)


def _fit(rows):
    from sklearn.linear_model import LogisticRegression
    X = np.array([vector(r) for r in rows])
    y = np.array([r["net_r"] > 0 for r in rows], int)
    mu, sd = X.mean(0), X.std(0)
    sd[sd < 1e-9] = 1.0
    m = LogisticRegression(C=0.3, max_iter=2000)
    m.fit((X - mu) / sd, y)
    return HeadTrader("learned", m.coef_[0], float(m.intercept_[0]), mu, sd)


def _clean(rows):
    return [r for r in rows if not r["vetoes"]]


def test(tf):
    rows, vet = load(tf)
    rows = _clean(rows)
    val = [r for r in rows if VAL_START <= r["t"] < HOLD_START]
    hold = [r for r in rows if r["t"] >= HOLD_START]
    oof_rows = [r for r in val if r["t"] >= OOF_START]
    print(tf, "candidates: validation", len(val), "holdout", len(hold), flush=True)

    # ---- learned head trader: walk-forward, half a year at a time
    folds = pd.date_range("2022-01-01", "2025-01-01", freq="6MS").to_numpy("datetime64[ns]")
    oof_score = {}
    for a, b in zip(folds[:-1], folds[1:]):
        train = [r for r in val if r["exit_t"] < a]
        part = [r for r in val if a <= r["t"] < b]
        if len(train) < 200 or not part:
            continue
        ht = _fit(train)
        for r in part:
            oof_score[id(r)] = ht.expected_r(r)
    oof_rows = [r for r in oof_rows if id(r) in oof_score]
    days_oof = float((np.datetime64("2025-01-01") - np.datetime64("2022-01-01")) / np.timedelta64(1, "D"))
    grid_learned = []
    for th in THRESHOLDS:
        sel = [r for r in oof_rows if oof_score[id(r)] >= th]
        tr = portfolio(sel, [oof_score[id(r)] for r in sel], tf)
        s = stats(tr, days_oof)
        grid_learned.append({"threshold": th, "pass": passes_validation(s), **{k: s.get(k) for k in
                             ("signals", "win_rate", "avg_net_r", "total_net_r", "profit_factor", "max_drawdown_r")},
                             "years": s.get("years")})
    grid_check = []
    for n in CONFLUENCE:
        sel = [r for r in oof_rows if confluence(r) >= n and r["scores"].get("critic", 0) > -0.5]
        sc = [confluence(r) + 0.1 * r["scores"].get("critic", 0) for r in sel]
        tr = portfolio(sel, sc, tf)
        s = stats(tr, days_oof)
        grid_check.append({"min_confluence": n, "pass": passes_validation(s), **{k: s.get(k) for k in
                           ("signals", "win_rate", "avg_net_r", "total_net_r", "profit_factor", "max_drawdown_r")},
                           "years": s.get("years")})
    # also: every idea, no head trader (what the agents give without judgement)
    tr_all = portfolio(oof_rows, [0.0] * len(oof_rows), tf)
    s_all = stats(tr_all, days_oof)

    pick_l = next((g for g in grid_learned if g["pass"]), None)
    pick_c = next((g for g in grid_check if g["pass"]), None)
    choice = None
    if pick_l and pick_c:
        choice = "learned" if pick_l["total_net_r"] >= pick_c["total_net_r"] else "checklist"
    elif pick_l:
        choice = "learned"
    elif pick_c:
        choice = "checklist"

    result = {"tf": tf, "generated": str(pd.Timestamp.now("UTC"))[:10], "engine": C.VERSION,
              "coins": len({r["symbol"] for r in rows}), "candidates_validation": len(val),
              "candidates_holdout": len(hold), "veto_reasons": dict(sorted(vet.items(), key=lambda x: -x[1])[:12]),
              "validation_all_ideas": s_all, "grid_learned": grid_learned, "grid_checklist": grid_check,
              "choice": choice, "live": False}
    if choice is None:
        result["reason"] = "No version of the head trader passed the validation rule (2022-2024 walk-forward)."
        _save(tf, result)
        return result

    # ---- fixed choice: score the holdout ONCE
    days_h = float((max(r["t"] for r in hold) - HOLD_START) / np.timedelta64(1, "D")) if hold else 1.0
    if choice == "learned":
        th = pick_l["threshold"]
        ht = _fit([r for r in val if r["exit_t"] < HOLD_START])
        ht.threshold = th
        val_scores = sorted(oof_score[id(r)] for r in oof_rows if oof_score[id(r)] >= th)
        ht.strong = float(np.quantile(val_scores, 0.6)) if val_scores else None
        sc = [ht.expected_r(r) for r in hold]
        sel = [(r, s) for r, s in zip(hold, sc) if s >= th]
    else:
        n = pick_c["min_confluence"]
        ht = HeadTrader("checklist", min_confluence=n)
        val_scores = sorted(confluence(r) for r in oof_rows if confluence(r) >= n)
        ht.strong = float(np.quantile(val_scores, 0.6)) if val_scores else None
        sel = [(r, ht.score(r)) for r in hold if ht.take(r)]
    taken = portfolio([r for r, _ in sel], [s for _, s in sel], tf)
    hs = stats(taken, days_h)
    for t in taken:
        t["grade"] = ht.grade(t)
    strong = [t for t in taken if t["grade"] == "strong"]
    std = [t for t in taken if t["grade"] != "strong"]
    hs["strong"] = {k: stats(strong).get(k) for k in ("signals", "win_rate", "avg_net_r")}
    hs["standard"] = {k: stats(std).get(k) for k in ("signals", "win_rate", "avg_net_r")}
    hs["period"] = f"2025-01-01 to {str(max(r['t'] for r in hold))[:10]}" if hold else ""
    hs["coins"] = result["coins"]
    hs["cost_round_trip"] = 0.003
    result["holdout"] = hs
    result["holdout_by_setup"] = journal.by_setup(taken)
    result["holdout_agents"] = journal.attribution(taken)
    result["validation_agents"] = journal.attribution(portfolio(oof_rows, [0.0] * len(oof_rows), tf))
    result["live"] = passed_test(hs)
    result["reason"] = ("Passed: the one holdout test on unseen data met every rule." if result["live"] else
                        "Failed the one holdout test on unseen data, so this stream sends no signals.")
    ht.meta = {"tf": tf, "market": "crypto", "engine": C.VERSION, "test": hs, "choice": choice,
               "selected_on": "walk-forward 2022-2024", "live": result["live"]}
    result["head_trader"] = ht.to_json()
    result["examples"] = [{"time": str(t["t"])[:16], "symbol": t["symbol"], "side": t["side"],
                           "net_r": round(t["net_r"], 2), "outcome": t["outcome"], "grade": t["grade"],
                           "explanation": ht.explain(t)} for t in taken[-8:]]
    if result["live"]:
        os.makedirs(f"{ROOT}/models", exist_ok=True)
        with open(f"{ROOT}/models/human_crypto_{tf}.json", "w") as fh:
            json.dump(ht.to_json(), fh, indent=1, default=float)
    _save(tf, result)
    return result


def _save(tf, result):
    path = f"{HERE}/results.json"
    allr = {}
    if os.path.exists(path):
        with open(path) as fh:
            allr = json.load(fh)
    allr[f"crypto_{tf}"] = result
    with open(path, "w") as fh:
        json.dump(allr, fh, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "prep":
        prep()
    elif cmd == "cands":
        cands(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 2)
    elif cmd == "test":
        r = test(sys.argv[2])
        print(json.dumps({k: r.get(k) for k in ("tf", "choice", "live", "reason")}, indent=1))
        if "holdout" in r:
            print(json.dumps({k: r["holdout"].get(k) for k in ("signals", "win_rate", "avg_net_r", "profit_factor",
                                                              "max_drawdown_r", "total_net_r", "signals_per_day")}))


def validation_report(tf):
    """Validation years only (no holdout): what the agents and both head traders do on 2021-2024. Used while
    building the engine; the holdout is never printed here."""
    rows, vet = load(tf)
    rows = [r for r in _clean(rows) if VAL_START <= r["t"] < HOLD_START]
    print(tf, "validation candidates", len(rows))
    allp = portfolio(rows, [0.0] * len(rows), tf)
    s = stats(allp)
    print(" every idea (portfolio rules only):", {k: s.get(k) for k in ("signals", "win_rate", "avg_net_r", "profit_factor")})
    print(" by setup:", json.dumps(journal.by_setup(allp)))
    print(" agents:", json.dumps(journal.attribution(allp)))
    folds = pd.date_range("2022-01-01", "2025-01-01", freq="6MS").to_numpy("datetime64[ns]")
    sc = {}
    for a, b in zip(folds[:-1], folds[1:]):
        train = [r for r in rows if r["exit_t"] < a]
        part = [r for r in rows if a <= r["t"] < b]
        if len(train) >= 200 and part:
            ht = _fit(train)
            for r in part:
                sc[id(r)] = ht.expected_r(r)
    oof = [r for r in rows if id(r) in sc]
    for th in THRESHOLDS[::2]:
        sel = [r for r in oof if sc[id(r)] >= th]
        s = stats(portfolio(sel, [sc[id(r)] for r in sel], tf))
        if s["signals"]:
            print(f" learned >= {th:+.2f}: n={s['signals']} win={s['win_rate']} avg={s['avg_net_r']} pf={s['profit_factor']}"
                  f" dd={s['max_drawdown_r']} years={ {y: v['avg_net_r'] for y, v in s['years'].items()} } pass={passes_validation(s)}")
    for n in CONFLUENCE:
        sel = [r for r in oof if confluence(r) >= n and r["scores"].get("critic", 0) > -0.5]
        s = stats(portfolio(sel, [confluence(r) for r in sel], tf))
        if s["signals"]:
            print(f" checklist >= {n}: n={s['signals']} win={s['win_rate']} avg={s['avg_net_r']} pf={s['profit_factor']}"
                  f" years={ {y: v['avg_net_r'] for y, v in s['years'].items()} } pass={passes_validation(s)}")


if __name__ == "__main__" and sys.argv[1] == "val":
    validation_report(sys.argv[2])
