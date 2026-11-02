"""Test Tauric (TradingAgents) decisions on past dates, then score them with the site's own trade plan.

    python tauric_backtest.py decide --from 2026-07-01 --to 2026-10-31 --step 2 --max-runs 10   # asks the LLM (costs money)
    python tauric_backtest.py report                                                              # scores the saved decisions

Run it on your own computer or in a Railway shell, with the LLM key and `pip install tradingagents yfinance`.

How a decision is scored (same as the live site): the decision for day X is acted on at the close of the 4h candle
that starts 20:00 UTC on day X (= the open of the next day). Stop = 1.5 x ATR(14), targets 1R / 2R / 3R with a third
closed at each, stop to entry after TP1, time stop after the stream's max_bars candles, cost taken off in R.
Every run is also compared with "always BUY" and "always SELL" on the same days, and with random directions.

WARNING (look-ahead): an LLM may remember what happened to prices before its training cutoff, so a test on old dates
can look better than the live result will be. Start the dates AFTER the model's training cutoff (e.g. July 2026 for
Claude), or treat the result as optimistic. The only fully honest test is the live record.
"""
import argparse
import json
import math
import os
import random
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

import markets
import streams
import tauric_engine as te

DECISIONS = os.environ.get("TAURIC_BT_FILE") or "tauric_decisions.jsonl"
SL_ATR = 1.5


# ---------------------------------------------------------------------------
# phase 1: ask TradingAgents (resumable, capped)
# ---------------------------------------------------------------------------
def _done():
    seen = set()
    if os.path.exists(DECISIONS):
        for line in open(DECISIONS, encoding="utf-8"):
            try:
                r = json.loads(line)
                seen.add((r["symbol"], r["date"]))
            except Exception:
                pass
    return seen


def cmd_decide(a):
    d0, d1 = date.fromisoformat(a.start), date.fromisoformat(a.end)
    syms = [s.strip().upper() for s in (a.symbols or ",".join(te.symbols_for("crypto") + te.symbols_for("forex"))).split(",") if s.strip()]
    todo, day = [], d0
    seen = _done()
    while day <= d1:
        for s in syms:
            if (s, day.isoformat()) not in seen and not (markets.is_forex(s) and day.weekday() >= 5):
                todo.append((s, day.isoformat()))
        day += timedelta(days=a.step)
    print(f"{len(todo)} decisions still to run, doing at most {a.max_runs}. Cost = runs x (several LLM calls).")
    with open(DECISIONS, "a", encoding="utf-8") as f:
        for s, day in todo[: a.max_runs]:
            try:
                text = te._decide(te.yahoo_ticker(s), day)
            except Exception as e:
                print(f"  {s} {day}: FAILED {str(e)[:120]}")
                continue
            side = te.parse_decision(text)
            f.write(json.dumps({"symbol": s, "date": day, "side": side, "raw": str(text)[:200]}) + "\n")
            f.flush()
            print(f"  {s} {day}: {side or 'HOLD'}")


# ---------------------------------------------------------------------------
# phase 2: score
# ---------------------------------------------------------------------------
def fetch_4h(symbol, start, end):
    """4h UTC candles (closed) from Yahoo 1h data."""
    import yfinance as yf
    t = yf.Ticker(te.yahoo_ticker(symbol))
    df = t.history(start=str(start - timedelta(days=60)), end=str(end + timedelta(days=15)), interval="1h", auto_adjust=False)
    if df is None or not len(df):
        raise RuntimeError(f"no Yahoo data for {symbol}")
    df.index = pd.to_datetime(df.index).tz_convert("UTC").tz_localize(None)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close"]]
    r = df.resample("4h")
    out = pd.DataFrame({"open": r["open"].first(), "high": r["high"].max(), "low": r["low"].min(),
                        "close": r["close"].last()}).dropna().reset_index()
    out = out.rename(columns={out.columns[0]: "timestamp"})
    out["volume"] = 1.0
    return out


def plan_for_stream(symbol):
    cfg = streams.STREAMS["forex_4h" if markets.is_forex(symbol) else "crypto_4h"]
    return {"targets_r": list(streams.TARGETS_R), "fractions": list(streams.FRACTIONS), "be_after": streams.BE_AFTER,
            "max_bars": cfg["max_bars"]}


def score_side(d, i, side, symbol, plan, mask):
    """Net result in R of one trade entered at the close of candle i, or None if it is not finished / has no ATR."""
    atr = float(d["atr"].iat[i])
    entry = float(d["close"].iat[i])
    dist = SL_ATR * atr
    if not dist > 0 or i + 1 >= len(d):
        return None
    sgn = 1 if side == "LONG" else -1
    sl = entry - sgn * dist
    res = streams.follow(side, entry, sl, d["open"].to_numpy(), d["high"].to_numpy(), d["low"].to_numpy(),
                         d["close"].to_numpy(), i + 1, plan, exit_mask=mask)
    if res["status"] is None:
        return None
    return {"r": res["r_gross"] - streams.cost_r(symbol, entry, sl), "bars": res["bars"], "status": res["status"],
            "i": i, "end": i + res["bars"]}


def _stats(rs):
    n = len(rs)
    if not n:
        return {"n": 0}
    w = sum(1 for r in rs if r > 0)
    z = 1.96
    p = w / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    return {"n": n, "win": p, "lo": centre - half, "hi": centre + half, "avg_r": sum(rs) / n,
            "pf": (gp / gl) if gl > 0 else float("inf") if gp > 0 else 0.0, "total_r": sum(rs)}


def fmt(s):
    if not s["n"]:
        return "no trades"
    return (f"n={s['n']}  win={s['win']*100:.1f}% (95% range {s['lo']*100:.0f}-{s['hi']*100:.0f}%)  "
            f"avg={s['avg_r']:+.2f}R  PF={s['pf']:.2f}  total={s['total_r']:+.1f}R")


def evaluate(decisions, candles=None, seed=7):
    """decisions: list of {symbol,date,side}. candles: optional {symbol: 4h DataFrame} (tests); else Yahoo."""
    rng = random.Random(seed)
    by_sym = {}
    for r in decisions:
        by_sym.setdefault(r["symbol"], []).append(r)
    out = {"tauric": [], "always_long": [], "always_short": [], "random": [], "rows": [], "unfinished": 0}
    for sym, rows in by_sym.items():
        rows.sort(key=lambda r: r["date"])
        dates = [date.fromisoformat(r["date"]) for r in rows]
        d = (candles or {}).get(sym)
        if d is None:
            d = fetch_4h(sym, min(dates), max(dates))
        d = streams.indicators(d)
        ts = pd.to_datetime(d["timestamp"])
        plan = plan_for_stream(sym)
        mask = streams.weekend_exit_mask(d["timestamp"], 4 * 3600) if markets.is_forex(sym) else None
        busy_until = -1
        for r, day in zip(rows, dates):
            start = datetime(day.year, day.month, day.day, 20)
            hit = np.where(ts.to_numpy() == np.datetime64(start))[0]
            if not len(hit):
                continue                              # no 20:00 candle that day (weekend / missing data)
            i = int(hit[0])
            for name, side in (("always_long", "LONG"), ("always_short", "SHORT"), ("random", rng.choice(["LONG", "SHORT"]))):
                x = score_side(d, i, side, sym, plan, mask)
                if x:
                    out[name].append(x["r"])
            if not r["side"]:
                continue
            if i <= busy_until:
                continue                              # the site takes one trade per symbol at a time
            x = score_side(d, i, r["side"], sym, plan, mask)
            if x is None:
                out["unfinished"] += 1
                continue
            busy_until = x["end"]
            out["tauric"].append(x["r"])
            out["rows"].append({"symbol": sym, "date": r["date"], "side": r["side"], "r": round(x["r"], 3), "status": x["status"]})
    return out


def cmd_report(a):
    if not os.path.exists(DECISIONS):
        sys.exit(f"{DECISIONS} not found: run the 'decide' step first")
    dec = [json.loads(x) for x in open(DECISIONS, encoding="utf-8") if x.strip()]
    ev = evaluate(dec)
    holds = sum(1 for r in dec if not r["side"])
    print(f"decisions: {len(dec)}  (HOLD: {holds})")
    print("TAURIC        ", fmt(_stats(ev["tauric"])))
    print("always BUY    ", fmt(_stats(ev["always_long"])))
    print("always SELL   ", fmt(_stats(ev["always_short"])))
    print("random side   ", fmt(_stats(ev["random"])))
    if ev["unfinished"]:
        print(f"({ev['unfinished']} trades still running, not counted)")
    by = {}
    for r in ev["rows"]:
        by.setdefault(r["symbol"], []).append(r["r"])
    for s, rs in sorted(by.items()):
        print(f"  {s:10s}", fmt(_stats(rs)))
    pd.DataFrame(ev["rows"]).to_csv("tauric_backtest_trades.csv", index=False)
    print("trade list: tauric_backtest_trades.csv")
    t, base = _stats(ev["tauric"]), _stats(ev["random"])
    if t["n"] < 100:
        print("\nToo few trades (<100): the win rate is not reliable yet. Run more dates before trusting it.")
    if t["n"] and base["n"] and t["avg_r"] <= base["avg_r"]:
        print("Tauric is NOT better than random sides on these days.")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--from", dest="start", required=True)
    d.add_argument("--to", dest="end", required=True)
    d.add_argument("--step", type=int, default=1, help="every N days")
    d.add_argument("--symbols", default="", help="comma list, default = TAURIC_SYMBOLS")
    d.add_argument("--max-runs", type=int, default=10)
    sub.add_parser("report")
    a = ap.parse_args()
    {"decide": cmd_decide, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
