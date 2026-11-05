"""Loop round 6: the trend trader on forex and gold (crypto's trend trader failed its final test on 2025-2026).

Data (both public on GitHub):
* validation: github.com/ejtraderLabs/historical-data, MetaTrader 15-minute candles 2012-11 .. 2022-03 (prices
  stored x10^digits, broker time = New York + 7 h -> converted to UTC);
* final test (never looked at before the variant is chosen): github.com/mailztoganesh-civil/forex-data, HistData
  1-minute candles 2023-01 .. 2026-10 (EST without daylight saving = UTC-5 -> UTC).
Both are turned into UTC 4h and daily candles.

Trend trader for one pair: in its own uptrend (20 average above the 50) a close at a new N-candle high = buy;
mirror for sells; optional: the daily trend must agree (for 4h signals). Stop 3 ATR, target 2R or a trailing stop,
time limit 40 candles. Costs: SignalX markets.py (spread + commission + 0.3 pip slippage); swap is not included.

Validation rule (fixed before running): >= 80 trades, >= +0.05R, profit factor >= 1.1 and positive in at least 7 of
the 9 years 2013-2021. Choice among the promising variants: the best worst year. Final test (fixed now, run once):
2023-01 .. 2026-10 must pass streams.passed_test(), make >= +0.05R and be positive in 2025 AND in 2026.

Usage: python fx_lab.py prep | python fx_lab.py val | python fx_lab.py final <variant number>
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lab  # noqa: E402
import loop  # noqa: E402
from chart_agents.risk import simulate  # noqa: E402

FX = os.environ.get("FX_DIR", "/home/claude/fx")
OUT = f"{FX}/frames"
NAME = {"EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY", "USDCHF": "USD/CHF", "USDCAD": "USD/CAD",
        "AUDUSD": "AUD/USD", "NZDUSD": "NZD/USD", "XAUUSD": "XAU/USD", "AUDJPY": "AUD/JPY", "EURCHF": "EUR/CHF",
        "EURGBP": "EUR/GBP", "EURJPY": "EUR/JPY", "GBPJPY": "GBP/JPY"}
TF_SEC = {"4h": 14400, "1d": 86400}
RULE = {"4h": "4h", "1d": "1D"}


def _scale(pair):
    return 1000.0 if pair.endswith("JPY") else (100.0 if pair == "XAUUSD" else 100000.0)


def _bars(df, tf):
    g = df.set_index("timestamp").resample(RULE[tf], label="left", closed="left")
    a = g.agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna().reset_index()
    a["n"] = g["close"].count().reindex(a["timestamp"]).to_numpy()
    return a


def prep():
    os.makedirs(OUT, exist_ok=True)
    for f in sorted(glob.glob(f"{FX}/historical-data/*/*m15.csv")):
        pair = os.path.basename(f)[:6]
        d = pd.read_csv(f)
        s = _scale(pair)
        for c in ("open", "high", "low", "close"):
            d[c] = d[c] / s
        ny = pd.to_datetime(d["Date"]) - pd.Timedelta(hours=7)
        utc = ny.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")
        d["timestamp"] = utc.dt.tz_localize(None)
        d = d.dropna(subset=["timestamp"]).sort_values("timestamp").drop_duplicates("timestamp")
        for tf in ("4h", "1d"):
            b = _bars(d, tf)
            b = b[b["n"] >= (8 if tf == "4h" else 40)]
            b.to_pickle(f"{OUT}/val_{pair}_{tf}.pkl")
        print("validation", pair, len(d), d.timestamp.iloc[0], d.timestamp.iloc[-1], flush=True)
    for pair in NAME:
        files = sorted(glob.glob(f"{FX}/forex-data/data/{pair}/1m_histdata/*.csv.gz"))
        files = [x for x in files if os.path.basename(x)[:4] >= "2023"]
        if not files:
            continue
        parts = []
        for x in files:
            d = pd.read_csv(x)
            d.columns = [c.lower() for c in d.columns]
            d["timestamp"] = pd.to_datetime(d["datetime"]) + pd.Timedelta(hours=5)
            parts.append(d[["timestamp", "open", "high", "low", "close"]])
        d = pd.concat(parts).sort_values("timestamp").drop_duplicates("timestamp")
        for tf in ("4h", "1d"):
            b = _bars(d, tf)
            b = b[b["n"] >= (120 if tf == "4h" else 600)]
            b.to_pickle(f"{OUT}/test_{pair}_{tf}.pkl")
        print("final test", pair, len(d), d.timestamp.iloc[0], d.timestamp.iloc[-1], flush=True)


class _A:
    pass


def trades(kind, tf, n=20, daily_filter=False, k_atr=3.0, target_r=2.0, trail=False, max_bars=40):
    out = []
    for f in sorted(glob.glob(f"{OUT}/{kind}_*_{tf}.pkl")):
        pair = os.path.basename(f).split("_")[1]
        sym = NAME[pair]
        d = pd.read_pickle(f).sort_values("timestamp").reset_index(drop=True)
        c, h, lo, o = (d[k].to_numpy(float) for k in ("close", "high", "low", "open"))
        pc = np.r_[c[0], c[:-1]]
        tr = np.maximum(h - lo, np.maximum(np.abs(h - pc), np.abs(lo - pc)))
        atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy(float)
        e20 = pd.Series(c).ewm(span=20, adjust=False).mean().to_numpy(float)
        e50 = pd.Series(c).ewm(span=50, adjust=False).mean().to_numpy(float)
        hi_n = pd.Series(c).shift(1).rolling(n).max().to_numpy(float)
        lo_n = pd.Series(c).shift(1).rolling(n).min().to_numpy(float)
        t_close = (d["timestamp"] + pd.Timedelta(seconds=TF_SEC[tf])).to_numpy("datetime64[ns]")
        dtrend = np.zeros(len(d))
        if daily_filter and tf == "4h":
            dd = pd.read_pickle(f.replace(f"_{tf}.pkl", "_1d.pkl")).sort_values("timestamp")
            de = dd["close"].ewm(span=50, adjust=False).mean()
            dsig = np.sign(dd["close"].to_numpy() - de.to_numpy())
            d_close = (dd["timestamp"] + pd.Timedelta(days=1)).to_numpy("datetime64[ns]")
            j = np.searchsorted(d_close, t_close, side="right") - 1        # last daily candle already closed
            dtrend = np.where(j >= 0, dsig[np.clip(j, 0, None)], 0)
        a = _A()
        a.o, a.h, a.l, a.c, a.n, a.tf = o, h, lo, c, len(d), tf
        for i in range(60, len(d) - 1):
            side = 0
            if c[i] > e50[i] and e20[i] > e50[i] and c[i] > hi_n[i]:
                side = 1
            elif c[i] < e50[i] and e20[i] < e50[i] and c[i] < lo_n[i]:
                side = -1
            if not side or (daily_filter and tf == "4h" and dtrend[i] != side):
                continue
            entry, risk = c[i], k_atr * atr[i]
            stop = entry - side * risk
            if trail:
                x = _trail(a, i, side, entry, stop, atr, max_bars, sym)
            else:
                x = simulate(a, i, side, entry, stop, entry + side * target_r * risk, max_bars, "forex", sym)
            if x is None:
                continue
            out.append({"symbol": sym, "side": side, "t": t_close[i], "exit_t": t_close[min(i + x[2], len(d) - 1)],
                        "net_r": x[0], "outcome": x[1], "setups": ["trend breakout"], "scores": {}})
    return out


def _trail(a, i, side, entry, stop, atr, max_bars, sym):
    from chart_agents.risk import cost_in_r
    risk = (entry - stop) * side
    cur, best = stop, entry
    end = min(a.n - 1, i + max_bars)
    for j in range(i + 1, end + 1):
        if side == 1 and (a.o[j] <= cur or a.l[j] <= cur):
            px = a.o[j] if a.o[j] <= cur else cur
            break
        if side == -1 and (a.o[j] >= cur or a.h[j] >= cur):
            px = a.o[j] if a.o[j] >= cur else cur
            break
        best = max(best, a.c[j]) if side == 1 else min(best, a.c[j])
        if (best - entry) * side >= risk:
            cur = max(cur, entry) if side == 1 else min(cur, entry)
        t = best - side * 3.0 * atr[j]
        cur = max(cur, t) if side == 1 else min(cur, t)
    else:
        if end < i + max_bars:
            return None
        px, j = a.c[end], end
    net = (px - entry) * side / risk - cost_in_r("forex", sym, entry, risk, 0, side)
    return float(net), ("target" if net > 0 else "stop"), int(j - i), float(px)


def judge_fx(trades_):
    s = lab.stats(trades_)
    if not s.get("signals"):
        return {"signals": 0, "promising": False}
    yrs = {y: v["avg_net_r"] for y, v in s["years"].items()}
    pos = sum(1 for y in range(2013, 2022) if yrs.get(y, -1) > 0)
    ok = s["signals"] >= 80 and s["avg_net_r"] >= 0.05 and (s["profit_factor"] or 0) >= 1.1 and pos >= 7
    return {"signals": s["signals"], "win_rate": s["win_rate"], "avg_net_r": s["avg_net_r"],
            "profit_factor": s["profit_factor"], "total_net_r": s["total_net_r"], "max_drawdown_r": s["max_drawdown_r"],
            "years": yrs, "positive_years_2013_2021": pos, "worst_year": min(yrs.get(y, 0) for y in range(2013, 2022)),
            "promising": bool(ok)}


VARIANTS = [(tf, n, df, tr) for tf in ("4h", "1d") for n in (20, 55) for df in (False, True) for tr in (False, True)
            if not (tf == "1d" and df)]


def val():
    res = []
    for k, (tf, n, df, trl) in enumerate(VARIANTS):
        tr = trades("val", tf, n=n, daily_filter=df, trail=trl)
        tr = [t for t in tr if np.datetime64("2013-01-01") <= t["t"] < np.datetime64("2022-01-01")]
        r = judge_fx(lab.portfolio(tr, [0.0] * len(tr), "4h"))
        name = f"forex trend: {tf} {n}-candle breakout{' + daily trend' if df else ''}{' trailing' if trl else ' 2R'}"
        loop.log({"tf": tf, "name": name + f" (variant {k})", "exit": "trailing 3 ATR" if trl else "3 ATR / 2R",
                  "result": r})
        res.append((k, r))
    good = [(r["worst_year"], k) for k, r in res if r["promising"]]
    print("promising variants (worst year, number):", sorted(good, reverse=True))
    return res


def final(k):
    tf, n, df, trl = VARIANTS[k]
    tr = trades("test", tf, n=n, daily_filter=df, trail=trl)
    tr = [t for t in tr if t["t"] >= np.datetime64("2023-01-01")]
    taken = lab.portfolio(tr, [0.0] * len(tr), "4h")
    last = max(t["t"] for t in taken)
    s = lab.stats(taken, float((last - np.datetime64("2023-01-01")) / np.timedelta64(1, "D")))
    s["period"] = f"2023-01-01 to {str(last)[:10]}"
    ok = bool(lab.passed_test(s) and s["avg_net_r"] >= 0.05 and
              all(s["years"].get(y, {}).get("avg_net_r", -1) > 0 for y in (2025, 2026)))
    out = {"variant": {"tf": tf, "n": n, "daily_filter": df, "trailing": trl, "number": k}, "passed": ok, **s}
    with open(f"{HERE}/fx_final.json", "w") as fh:
        json.dump(out, fh, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    print(json.dumps({x: out.get(x) for x in ("variant", "passed", "signals", "win_rate", "avg_net_r", "profit_factor",
                                              "max_drawdown_r", "total_net_r", "signals_per_day")}, default=str))
    print({y: v["avg_net_r"] for y, v in s["years"].items()})
    return out


if __name__ == "__main__":
    {"prep": prep, "val": val}.get(sys.argv[1], lambda: final(int(sys.argv[2])))()
