"""One honest backtest for every ported strategy (strategies.py).

Protocol (fixed before any holdout result is looked at):
* Entry at the close of the signal candle. One open trade per symbol per strategy.
* Exit plans (the site's tracker needs a stop and a target): stop = SL x ATR(14), one target at TP x the stop
  distance, time limit MAX_BARS candles. Pessimistic: a candle touching stop and target counts as the stop; a
  candle opening beyond the stop exits at its open.
* Costs (SignalX markets.py, Nov 2026): crypto 0.30% per round trip + funding 0.00125%/hour for longs;
  forex the pair's spread + commission + 0.3 pip slippage.
* Validation: crypto 2021-2024, forex 2023-2024. Holdout (never used for any choice): 2025-01-01 to Oct 2026.
* A strategy passes validation when, for its best plan, it has >= MIN_VAL_TRADES trades, avg >= +0.05R, profit
  factor >= 1.1 and every validation year a positive average.
* Per stream (market x timeframe) the passing strategies are combined (one trade per symbol at a time; when two
  fire together the one with the better validation average wins) and the combination is scored ONCE on the
  holdout. It goes live only if the holdout passes SignalX's streams.passed_test().
Usage: python lab.py masks crypto 15m   |   python lab.py run crypto 15m   |   python lab.py report"""
import json
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.dirname(os.path.dirname(HERE)))
import markets  # noqa: E402  (SignalX cost model)
import ind as I  # noqa: E402
import strategies as S  # noqa: E402

D = os.environ.get("REPO_LAB_DIR") or os.path.join(HERE, "work")
FR = f"{D}/frames"
MK = f"{D}/masks"
RES = f"{D}/results"
TF_H = {"15m": 0.25, "1h": 1.0, "4h": 4.0}
MAX_BARS = {"15m": 32, "1h": 36, "4h": 30}
PLANS = [(1.5, 1.0), (1.5, 2.0), (2.5, 1.0), (2.5, 2.0)]
SPLIT = {"crypto": ("2021-03-01", "2025-01-01"), "forex": ("2023-02-01", "2025-01-01")}
MIN_VAL_TRADES = 80
FX_NAME = {"EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY", "USDCHF": "USD/CHF", "USDCAD": "USD/CAD",
           "AUDUSD": "AUD/USD", "NZDUSD": "NZD/USD", "XAUUSD": "XAU/USD"}


def symbols(market):
    return sorted({f.split("_")[0] for f in os.listdir(f"{FR}/{market}")})


def frame(market, sym, tf):
    return pd.read_pickle(f"{FR}/{market}/{sym}_{tf}.pkl")


# ------------------------------------------------------------------------------------------------ masks
def _masks_one(args):
    market, sym, tf = args
    out = f"{MK}/{market}_{tf}_{sym}.npz"
    if os.path.exists(out):
        return sym
    d = frame(market, sym, tf)
    arr = {}
    for name, fn in S.STRATEGIES.items():
        if market == "forex" and name in S.NEEDS_VOLUME:
            continue
        if market == "crypto" and name in getattr(S, "FOREX_ONLY", ()):
            continue
        try:
            lg, sh = fn(d)
            arr[name + "|L"] = np.asarray(pd.Series(lg).fillna(False), bool)
            arr[name + "|S"] = np.asarray(pd.Series(sh).fillna(False), bool)
        except Exception as e:  # a broken port must not stop the lab
            print("mask fail", name, sym, tf, e, flush=True)
    arr["_atr"] = I.atr(d.high, d.low, d.close, 14).to_numpy(float)
    np.savez_compressed(out, **arr)
    return sym


def masks(market, tf):
    os.makedirs(MK, exist_ok=True)
    t0 = time.time()
    with Pool(2) as p:
        for s in p.imap_unordered(_masks_one, [(market, s, tf) for s in symbols(market)]):
            print("masks", market, tf, s, f"{time.time() - t0:.0f}s", flush=True)


# ------------------------------------------------------------------------------------------------ simulation
def cost_r(market, sym, entry, risk, side, hours):
    if market == "forex":
        return markets.cost_price(FX_NAME[sym]) / risk
    c = markets.CRYPTO_COST_ROUND_TRIP + (markets.CRYPTO_FUNDING_PER_HOUR * hours if side > 0 else 0.0)
    return c * entry / risk


def simulate(market, sym, tf, o, h, l, c, atr, sig, plan):
    """sig: int8 array (+1 long, -1 short, 0). Returns list of (entry_index, side, r_net, bars)."""
    sl_k, tp_k = plan
    mb = MAX_BARS[tf]
    n = len(c)
    idx = np.flatnonzero(sig)
    out = []
    busy = -1
    for i in idx:
        if i <= busy or i + 1 >= n:
            continue
        a = atr[i]
        if not a > 0:
            continue
        side = int(sig[i])
        entry = c[i]
        risk = sl_k * a
        stop = entry - side * risk
        tp = entry + side * tp_k * risk
        j0, j1 = i + 1, min(n, i + 1 + mb)
        oo, hh, ll = o[j0:j1], h[j0:j1], l[j0:j1]
        if side > 0:
            sh_ = ll <= stop
            th_ = hh >= tp
        else:
            sh_ = hh >= stop
            th_ = ll <= tp
        fs = int(np.argmax(sh_)) if sh_.any() else 10 ** 9
        ft = int(np.argmax(th_)) if th_.any() else 10 ** 9
        if fs == 10 ** 9 and ft == 10 ** 9:
            if j1 - j0 < mb:          # ran out of data: still open, not counted
                break
            k = j1 - j0 - 1
            exit_px = c[j0 + k]
        elif fs <= ft:
            k = fs
            gap = (oo[k] <= stop) if side > 0 else (oo[k] >= stop)
            exit_px = oo[k] if gap else stop
        else:
            k = ft
            exit_px = tp
        bars = k + 1
        r = side * (exit_px - entry) / risk - cost_r(market, sym, entry, risk, side, bars * TF_H[tf])
        out.append((int(i), side, float(r), bars))
        busy = i + bars
    return out


def stats(r):
    r = np.asarray(r, float)
    n = len(r)
    if not n:
        return {"trades": 0}
    gp, gl = r[r > 0].sum(), -r[r < 0].sum()
    cum = np.cumsum(r)
    dd = float((cum - np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]).min())
    return {"trades": n, "win_rate": round(float((r > 0).mean() * 100), 1), "avg_r": round(float(r.mean()), 4),
            "profit_factor": round(float(gp / gl), 3) if gl > 0 else None, "total_r": round(float(r.sum()), 1),
            "max_drawdown_r": round(dd, 1)}


def _run_symbol(args):
    market, sym, tf = args
    d = frame(market, sym, tf)
    z = np.load(f"{MK}/{market}_{tf}_{sym}.npz")
    o, h, l, c = (d[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    ts = d["timestamp"].to_numpy()
    atr = z["_atr"]
    names = sorted({k.split("|")[0] for k in z.files if "|" in k})
    rows = []
    for name in names:
        sig = z[name + "|L"].astype(np.int8) - z[name + "|S"].astype(np.int8)
        for plan in PLANS:
            for (i, side, r, bars) in simulate(market, sym, tf, o, h, l, c, atr, sig, plan):
                rows.append((name, plan[0], plan[1], sym, ts[i], side, r, bars))
    return rows


def run(market, tf):
    os.makedirs(RES, exist_ok=True)
    t0 = time.time()
    allrows = []
    with Pool(2) as p:
        for rows in p.imap_unordered(_run_symbol, [(market, s, tf) for s in symbols(market)]):
            allrows.extend(rows)
            print("run", market, tf, len(allrows), f"{time.time() - t0:.0f}s", flush=True)
    df = pd.DataFrame(allrows, columns=["strategy", "sl", "tp", "symbol", "time", "side", "r", "bars"])
    df.to_pickle(f"{RES}/trades_{market}_{tf}.pkl")
    print("saved", len(df), flush=True)


# ------------------------------------------------------------------------------------------------ evaluation
def evaluate(market, tf):
    df = pd.read_pickle(f"{RES}/trades_{market}_{tf}.pkl").sort_values("time")
    start, cut = (pd.Timestamp(x) for x in SPLIT[market])
    df = df[df.time >= start]
    val, hold = df[df.time < cut], df[df.time >= cut]
    table, chosen = [], {}
    for (name, sl, tp), g in val.groupby(["strategy", "sl", "tp"]):
        s = stats(g.r)
        years = g.groupby(g.time.dt.year).r.mean()
        s.update(strategy=name, sl=sl, tp=tp, years={int(k): round(float(v), 3) for k, v in years.items()})
        s["passed"] = bool(s["trades"] >= MIN_VAL_TRADES and s["avg_r"] >= 0.05 and (s["profit_factor"] or 0) >= 1.1
                           and (years > 0).all())
        hg = hold[(hold.strategy == name) & (hold.sl == sl) & (hold.tp == tp)]
        s["holdout"] = stats(hg.r)
        table.append(s)
        if s["passed"] and (name not in chosen or s["avg_r"] > chosen[name]["avg_r"]):
            chosen[name] = s
    combo = combine(hold, chosen, tf) if chosen else {"trades": 0}
    combo_val = combine(val, chosen, tf) if chosen else {"trades": 0}
    import streams  # SignalX rule
    test = dict(combo)
    test.update(signals=combo.get("trades", 0), avg_net_r=combo.get("avg_r"), total_net_r=combo.get("total_r"),
                period=f"{cut.date()} to {df.time.max().date()}")
    live = bool(chosen) and streams.passed_test(test)
    return {"market": market, "tf": tf, "strategies_tested": len({t["strategy"] for t in table}),
            "plans_tested": len(table), "passed_validation": sorted(chosen),
            "chosen": {k: {"sl": v["sl"], "tp": v["tp"], "val_avg_r": v["avg_r"], "val_trades": v["trades"],
                           "holdout": v["holdout"]} for k, v in chosen.items()},
            "combined_validation": combo_val, "combined_holdout": combo, "live": live, "table": table}


def combine(trades, chosen, tf):
    """Merge the chosen strategies: per symbol one trade at a time, ties go to the better validation average."""
    rank = {k: v["avg_r"] for k, v in chosen.items()}
    parts = [trades[(trades.strategy == k) & (trades.sl == v["sl"]) & (trades.tp == v["tp"])] for k, v in chosen.items()]
    t = pd.concat(parts)
    if not len(t):
        return {"trades": 0}
    t = t.assign(rank=t.strategy.map(rank)).sort_values(["symbol", "time", "rank"], ascending=[True, True, False])
    keep, r_all, times = [], [], []
    for sym, g in t.groupby("symbol"):
        busy_until = pd.Timestamp.min
        for row in g.itertuples():
            if row.time <= busy_until:
                continue
            keep.append(row)
            busy_until = row.time + pd.Timedelta(hours=TF_H[tf] * row.bars)
    k = pd.DataFrame(keep).sort_values("time")
    s = stats(k.r)
    s["by_strategy"] = k.groupby("strategy").r.agg(["count", "mean"]).round(3).to_dict("index")
    s["years"] = {int(y): round(float(v), 3) for y, v in k.groupby(k.time.dt.year).r.mean().items()}
    return s



def report_all():
    out = {}
    for market in ("crypto", "forex"):
        for tf in ("15m", "1h", "4h"):
            p = f"{RES}/trades_{market}_{tf}.pkl"
            if os.path.exists(p):
                out[f"{market}_{tf}"] = evaluate(market, tf)
    with open(f"{RES}/summary.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    for k, v in out.items():
        print(f"== {k}: {v['strategies_tested']} strategies, {len(v['passed_validation'])} passed validation "
              f"{v['passed_validation']}")
        print("   combined validation:", {x: v["combined_validation"].get(x) for x in ("trades", "avg_r", "profit_factor")})
        print("   combined HOLDOUT:  ", {x: v["combined_holdout"].get(x) for x in ("trades", "win_rate", "avg_r",
                                                                                   "profit_factor", "max_drawdown_r", "total_r")},
              "LIVE" if v["live"] else "not live")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "masks":
        masks(sys.argv[2], sys.argv[3])
    elif cmd == "run":
        run(sys.argv[2], sys.argv[3])
    elif cmd == "report":
        report_all()
