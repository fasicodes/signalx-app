"""Checks for chart_agents (the human-style chart engine). Run: python tests/test_chart_agents.py"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chart_agents import config as C  # noqa: E402
from chart_agents.candles import CandleAgent  # noqa: E402
from chart_agents.data import Chart, resample  # noqa: E402
from chart_agents.engine import Book, Engine  # noqa: E402
from chart_agents.head import FEATURES, HeadTrader  # noqa: E402
from chart_agents.liquidity import LiquidityAgent  # noqa: E402
from chart_agents.risk import Portfolio, RiskManager, simulate  # noqa: E402
from chart_agents.structure import StructureAgent  # noqa: E402
from chart_agents.swings import SwingTracker, pivots  # noqa: E402
from chart_agents.zones import Zone, build_zones, merge, obstacles  # noqa: E402

FAILS = []
N = [0]


def check(name, cond):
    N[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def frame(closes, start="2024-01-01", tf="15min", wick=0.3, vol=100.0):
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + wick
    lo = np.minimum(o, c) - wick
    t = pd.date_range(start, periods=len(c), freq=tf)
    return pd.DataFrame({"timestamp": t, "open": o, "high": h, "low": lo, "close": c, "volume": vol,
                         "taker_buy_volume": vol / 2, "quote_volume": vol * c})


def walk(n, seed=0, drift=0.0, vol=0.006):
    """A crypto-like price path (about 0.6% per 15 minutes) with trends and ranges, so every agent has work."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift * vol, vol, n) + np.sin(np.arange(n) / 400.0) * 0.4 * vol
    return 1000 * np.exp(np.cumsum(steps))


# ------------------------------------------------------------------ swings
h = np.array([1, 2, 3, 9, 3, 2, 1, 2, 3, 2, 1], float)
lo = h - 0.5
ph, pl = pivots(h, lo, 3)
check("pivot high found at the peak", bool(ph[3]) and ph.sum() == 1)
ch = Chart(frame(np.r_[np.linspace(100, 120, 40), np.linspace(120, 100, 40), np.linspace(100, 130, 40)], wick=0.1),
           "15m")
sw = SwingTracker(ch)
for i in range(ch.n):
    sw.on_bar(i)
kinds = [s.kind for s in sw.swings]
check("zigzag swings alternate", all(a != b for a, b in zip(kinds, kinds[1:])))
check("zigzag finds the top at 120", any(s.kind == "H" and abs(s.price - 120.1) < 0.6 for s in sw.swings))

# ------------------------------------------------------------------ structure
up = np.r_[np.linspace(100, 110, 20), np.linspace(110, 105, 10), np.linspace(105, 116, 20),
           np.linspace(116, 111, 10), np.linspace(111, 122, 20), np.linspace(122, 100, 30)]
ch = Chart(frame(up, wick=0.05), "15m")
sw = SwingTracker(ch)
st = StructureAgent(ch, sw)
for i in range(ch.n):
    sw.on_bar(i)
    st.on_bar(i)
evs = [e for e in st.event if e]
check("break of structure up in the rally", any(e[0] == "BOS" and e[1] == 1 for e in evs) or
      any(e[0] == "CHOCH" and e[1] == 1 for e in evs))
check("change of character down when the last higher low breaks", any(e[0] == "CHOCH" and e[1] == -1 for e in evs))
check("trend ends down", st.trend[-1] == -1)
check("order block recorded on the change of character", st.last_choch is not None and "ob" in st.last_choch)

# ------------------------------------------------------------------ zones
seg = []
for k in range(4):
    seg += list(np.linspace(110, 100, 15)) + list(np.linspace(100, 110, 15))
ch = Chart(frame(np.r_[seg, np.linspace(110, 112, 20)], wick=0.2), "15m")
sw = SwingTracker(ch)
for i in range(ch.n):
    sw.on_bar(i)
zs = build_zones(ch, sw.minor, ch.n - 1)
check("support zone around 100 with several touches",
      any(z.lo <= 100 <= z.hi + 0.5 and z.touches >= 2 for z in zs))
check("resistance zone around 110", any(z.lo - 0.5 <= 110 <= z.hi + 0.5 for z in zs))
check("zones are not stacked on top of each other",
      all(abs(a.mid - b.mid) >= C.ZONE_SPACING_ATR * ch.a(ch.n - 1) - 1e-9 for a in zs for b in zs if a is not b))
a1 = Zone(100, 102, 2, strength=3, level=0)
a2 = Zone(101, 103, 3, strength=5, level=1)
a3 = Zone(102.5, 104.5, 2, strength=4, level=0)
m = merge([a1, a2, a3])
check("overlapping zones merge without growing into one huge zone", max(z.hi - z.lo for z in m) <= 2.0 + 1e-9)
check("obstacles: nearest significant resistance first",
      [z.lo for z in obstacles([Zone(120, 121, 3, strength=4), Zone(110, 111, 3, strength=4)], 105, 1)][0] == 110)

# ------------------------------------------------------------------ candles
df = pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=30, freq="15min"),
                   "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1.0})
df.loc[28, ["open", "high", "low", "close"]] = [100.5, 100.6, 98.9, 99.0]     # red
df.loc[29, ["open", "high", "low", "close"]] = [98.9, 101.2, 98.8, 101.1]     # green, engulfs
ch = Chart(df, "15m")
cd = CandleAgent(ch)
check("bullish engulfing detected", cd.bull_name[29] == "bullish engulfing")
df2 = df.copy()
df2.loc[29, ["open", "high", "low", "close"]] = [100.0, 100.15, 97.6, 100.1]  # long lower wick
cd2 = CandleAgent(Chart(df2, "15m"))
check("hammer detected", cd2.bull_name[29] == "hammer")

# ------------------------------------------------------------------ patterns: double bottom and its breakout
dbl = np.r_[np.linspace(130, 100, 30), np.linspace(100, 115, 20), np.linspace(115, 100.3, 20),
            np.linspace(100.3, 112, 15), np.linspace(112, 120, 10)]
ch = Chart(frame(dbl, wick=0.1), "15m")
bk = Book(ch, 0, entry=True)
fired = []
for i in range(ch.n):
    bk.step(i)
    fired += bk.pt.fired.get(i, [])
check("double bottom breaks out above the neckline", any(p.name == "double bottom" and p.side == 1 for p in fired))

# ------------------------------------------------------------------ liquidity: sweep of equal lows, fair value gap
lq_c = np.r_[np.linspace(110, 100, 10), np.linspace(100, 106, 8), np.linspace(106, 100.05, 8), np.linspace(100, 107, 8),
             np.full(4, 107.0)]
df = frame(lq_c, wick=0.1)
k = len(df) - 2
df.loc[k, ["open", "high", "low", "close"]] = [106.5, 106.8, 98.9, 106.6]       # wick under the equal lows
ch = Chart(df, "15m")
sw = SwingTracker(ch)
lq = LiquidityAgent(ch, sw)
for i in range(ch.n):
    sw.on_bar(i)
    lq.on_bar(i)
check("sell-side sweep under the equal lows", lq.sweep(k, 1) is not None)
fv = frame(np.r_[np.full(10, 100.0), [100, 104, 108], np.full(5, 108.0)], wick=0.2)
ch = Chart(fv, "15m")
sw = SwingTracker(ch)
lq = LiquidityAgent(ch, sw)
for i in range(ch.n):
    sw.on_bar(i)
    lq.on_bar(i)
check("bullish fair value gap recorded", any(g["side"] == 1 for g in lq.fvgs))

# ------------------------------------------------------------------ risk manager
ch = Chart(frame(walk(400, 3), wick=3.0), "15m")
rm = RiskManager("crypto", "BTC/USDT", "4h")
i = 350
a = ch.a(i)
plan, err = rm.plan(ch, i, 1, ch.c[i] - 2 * a, None, [Zone(ch.c[i] + 0.5 * a, ch.c[i] + 0.8 * a, 3, strength=4)])
check("no trade when strong resistance is too close", plan is None and "room" in (err or ""))
plan, err = rm.plan(ch, i, 1, ch.c[i] - 2 * a, None, [])
check("2R target when nothing is in the way", plan is not None and abs(plan["rr"] - C.NO_ROOM_TARGET_R) < 1e-9)
check("costs are counted in R", plan is not None and plan["cost_r"] > 0)
plan, err = rm.plan(ch, i, 1, ch.c[i] - 10 * a, None, [])
check("no trade when the stop would be too wide", plan is None and "wide" in (err or ""))
df = frame([100, 100, 100, 100], tf="4h")
df.loc[1, ["high", "low"]] = [103, 97]                    # touches the stop and the target on one candle
ch = Chart(df, "4h")
r = simulate(ch, 0, 1, 100.0, 98.0, 102.0, 2)
check("a candle touching stop and target counts as the stop (pessimistic)", r is not None and r[1] == "stop")
df.loc[1, ["open", "high", "low", "close"]] = [96, 97, 95, 96]
r = simulate(Chart(df, "4h"), 0, 1, 100.0, 98.0, 102.0, 2)
check("a gap through the stop exits at the open", r is not None and r[3] == 96 and r[0] < -1.5)
pf = Portfolio("4h")
now = np.datetime64("2025-01-01T00:00")
ok1, _ = pf.allow("BTC/USDT", 1, now, 0)
pf.take("BTC/USDT", 1, now + np.timedelta64(8, "h"), -1.0)
ok2, why2 = pf.allow("BTC/USDT", 1, now, 1)
ok3, why3 = pf.allow("BTC/USDT", 1, now + np.timedelta64(9, "h"), 0)
check("portfolio: one trade per coin", ok1 and not ok2)
check("portfolio: cool-down on a coin after a loss", not ok3 and "cool-down" in why3)

# ------------------------------------------------------------------ engine: no look-ahead, live = backtest
base = frame(walk(9000, 7, 0.01), start="2023-01-01", wick=2.0, vol=1e6)
base["volume"] = 1e6 * (1 + 0.5 * np.sin(np.arange(len(base)) / 50.0) ** 2)
base["quote_volume"] = base["volume"] * base["close"]


def frames_of(df):
    return {"15m": df, **{h: resample(df, h) for h in ("1h", "4h", "1d", "1w")}}


def key(c):
    return (c["time"], c["side"], tuple(c["setups"]), round(c["plan"]["stop"], 8), round(c["plan"]["target"], 8),
            tuple(sorted((k, round(float(v), 8)) for k, v in c["feats"].items())))


full = Engine(frames_of(base), "1h", "TEST/USDT")
fc = full.run()
check("the engine finds trade ideas on a random chart", len(fc) > 0)
cut = base.timestamp.iloc[6000]
part = Engine(frames_of(base[base.timestamp < cut]), "1h", "TEST/USDT")
pc = part.run()
cut_iso = str(np.datetime64(cut, "s"))
check("no look-ahead: cutting the future away changes nothing before the cut",
      [key(c) for c in fc if c["time"] <= cut_iso] == [key(c) for c in pc if c["time"] <= cut_iso])
late = [c for c in fc if c["bar"] > 1200]
same = 0
for c in late[:4]:
    t_close = pd.Timestamp(c["time"])
    win = {}
    for h, df in frames_of(base).items():
        done = df[df.timestamp + pd.Timedelta(seconds=C.TF_SEC[h]) <= t_close]
        win[h] = done.tail(C.LIVE_HISTORY.get(h, 3000))
    out = Engine(win, "1h", "TEST/USDT").run(evaluate_from=0, last_only=True)
    same += int(bool(out) and key(out[-1])[:5] == key(c)[:5])
check("live replay of the last candles gives the backtest's decision", late and same == min(4, len(late)))
rep = full.report()
check("chart report lists trend per timeframe", len(rep["trend"]) == 3)

# ------------------------------------------------------------------ head trader
c0 = fc[0]
ht = HeadTrader("learned", np.ones(len(FEATURES)) * 0.01, 0.0, threshold=0.0)
ht2 = HeadTrader.from_json(ht.to_json())
check("head trader survives a save / load", abs(ht.expected_r(c0) - ht2.expected_r(c0)) < 1e-9)
txt = ht.explain(c0)
check("explanation is plain English text", txt.isascii() and ("BUY" in txt or "SELL" in txt) and "stop" in txt)
check("a veto always wins", not HeadTrader("learned", threshold=-99).take(dict(c0, vetoes=["news"])))

print()
if FAILS:
    print(f"{len(FAILS)} OF {N[0]} CHECKS FAILED:", ", ".join(FAILS))
else:
    print(f"ALL {N[0]} CHECKS PASSED")
sys.exit(1 if FAILS else 0)
