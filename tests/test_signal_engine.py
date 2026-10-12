"""Tests for signal_engine.py (signal engine v3). No network: candles are synthetic.
Run from the project root:  python tests/test_signal_engine.py
"""
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import signal_engine as se  # noqa: E402

se.time.sleep = lambda s: None
passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


def close_to(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1, abs(b))


N = 1100
T4 = pd.date_range("2025-06-01", periods=N, freq="4h")


def synth(seed, drift=0.0003):
    rng = np.random.default_rng(seed)
    close = 50 * (1 + seed) * np.cumprod(1 + rng.normal(drift, 0.012, N))
    df = pd.DataFrame({"timestamp": T4, "open": np.r_[close[0], close[:-1]], "close": close,
                       "volume": rng.uniform(500, 1500, N)})
    df["high"] = np.maximum(df["open"], df["close"]) * (1 + rng.uniform(0, 0.006, N))
    df["low"] = np.minimum(df["open"], df["close"]) * (1 - rng.uniform(0, 0.006, N))
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


COINS = sorted(set(se.UNIVERSE) | set(se.signal_coins()))
DATA = {sym: synth(k) for k, sym in enumerate(COINS)}
DATA["TAO/USDT"] = synth(99, drift=0.001)   # a coin the model gives no trades for

print("\n[1] model file")
m = se.model()
check(len(m.features) == 82 and len(m.trees) == 250, "82 features, 250 trees")
check(m.sl_atr == 2.0 and m.targets_r == [0.5, 1.0, 2.0] and m.max_bars == 54 and m.be_after == 1,
      "plan: 2x ATR stop, targets 0.5R / 1R / 2R, stop to entry after TP1, 54 candles")
check(abs(sum(m.fractions) - 1) < 1e-9 and len(set(round(x, 9) for x in m.fractions)) == 1, "a third closes at each target")
check(0.5 < m.floor < m.watch_threshold < m.threshold < 0.95 and m.gap_hours == 24,
      f"levels: standard {m.floor:.3f} < forming {m.watch_threshold:.3f} < strong {m.threshold:.3f}, 24h rule")
check(len(se.signal_coins()) == 21 and set(se.UNIVERSE) <= set(se.signal_coins()), "21 signal coins incl. the 16-coin universe")
t = se.test_stats()
check(t["signals"] > 300 and 60 < t["win_rate"] < 90 and t["avg_net_r"] > 0 and t["period"].startswith("2025-10"),
      "out-of-sample test stats bundled (Oct 2025 onward, data the model never saw)")
wf = se.walk_forward_stats()
check(wf["signals"] > 1000 and all(v["avg_net_r"] > 0 for v in wf["years"].values()), "walk-forward 2023-2026: every year positive")
p = m.raw_proba(np.full((3, 82), np.nan))
check(np.all((p > 0) & (p < 1)) and np.allclose(p, p[0]), "all-missing input is handled (NaN-aware trees)")
cal = m.calibrate(np.array([0.0, 0.5, 0.7, 0.99]))
check(np.all(np.diff(cal) >= 0) and 0 <= cal[0] <= cal[-1] <= 1, "calibration is monotonic")

print("\n[2] features")
F = se.coin_features(DATA["BTC/USDT"])
check(len(F) == N and F["atr"].iloc[300:].notna().all(), "per-candle features")
day_cols = [c for c in F.columns if c.startswith("h_")]
check(len(day_cols) == 12, "daily-trend features present")
row = F.iloc[700]
t_close = row["timestamp"] + pd.Timedelta(hours=4)
day = se.resample(DATA["BTC/USDT"], "1D")
closed_days = day[day["timestamp"] + pd.Timedelta(days=1) <= t_close]
ref = se.base_features(closed_days)["rsi14"].iloc[-1]
check(abs(row["h_rsi14"] - ref) < 1e-9, "daily features use only closed days (no look-ahead)")
FU = {s: se.coin_features(d) for s, d in DATA.items() if s in se.UNIVERSE}
X = se.assemble(F, se.btc_block(F), se.market_block(FU), "BTC/USDT")
check(list(X.columns) == m.features, "model input in model order")
G = se.flip_for_short(X)
check(np.allclose(G["rsi14"], 100 - X["rsi14"], equal_nan=True) and np.allclose(G["r6"], -X["r6"], equal_nan=True)
      and np.allclose(G["upw"], X["loww"], equal_nan=True) and np.allclose(G["atr_pct"], X["atr_pct"], equal_nan=True),
      "short side mirrors directional features only")
check(se.price_round(0.0000123456789) == 0.00001234568 and se.price_round(64321.123) == 64321.12, "price rounding keeps cheap coins precise")
sc = se.score_many({"BTC/USDT": X, "ETH/USDT": X.iloc[:50]})
one = se.score(X)
check(np.allclose(sc["BTC/USDT"][0], one[0]) and np.allclose(sc["BTC/USDT"][3], one[3]) and len(sc["ETH/USDT"][0]) == 50,
      "batched scoring == one coin at a time")

print("\n[3] trade plan (follow)")


def run(side, entry, sl, rows, start=0, max_bars=None):
    o, h, l, c = (np.array([r[k] for r in rows], float) for k in range(4))
    return se.follow(side, entry, sl, o, h, l, c, start, max_bars)


# LONG entry 100, stop 98 -> 1R = 2: TP1 101, TP2 102, TP3 104
check(se.targets("LONG", 100, 98) == [101, 102, 104] and se.targets("SHORT", 100, 102) == [99, 98, 96], "targets from entry and stop")
r = run("LONG", 100, 98, [(100, 100.5, 97.9, 98.5)])
check(r["status"] == "SL" and r["hits"] == 0 and close_to(r["r_gross"], -1.0), "stop before TP1 = -1R")
r = run("LONG", 100, 98, [(100, 101.2, 99.5, 100.8), (100.8, 100.9, 99.9, 100.1)])
check(r["status"] == "TP1" and r["hits"] == 1 and close_to(r["r_gross"], 0.5 / 3) and close_to(r["exit_price"], 100),
      "TP1 then back to entry: +1/6 R (a third at +0.5R, rest at entry)")
r = run("LONG", 100, 98, [(100, 102.1, 99.5, 101.8), (101.8, 101.9, 100.9, 101)])
check(r["status"] == "TP2" and r["hits"] == 2 and close_to(r["r_gross"], (0.5 + 1 + 0.5) / 3) and close_to(r["exit_price"], 101),
      "TP1 + TP2 then stop at TP1: +2/3 R")
r = run("LONG", 100, 98, [(100, 101.5, 99.6, 101), (101, 104.5, 100.5, 104.2)])
check(r["status"] == "TP3" and r["hits"] == 3 and close_to(r["r_gross"], 3.5 / 3) and close_to(r["exit_price"], 104),
      "all three targets: +7/6 R")
r = run("LONG", 100, 98, [(100, 102.5, 97.5, 101)])
check(r["status"] == "SL" and close_to(r["r_gross"], -1.0), "stop and targets in one candle: the stop counts first")
r = run("LONG", 100, 98, [(96.5, 97, 95, 96)])
check(r["status"] == "SL" and close_to(r["exit_price"], 96.5) and close_to(r["r_gross"], -1.75), "gap below the stop exits at the worse open")
r = run("LONG", 100, 98, [(100, 101.2, 99.8, 101), (99.0, 99.5, 98.5, 99)])
check(r["status"] == "TP1" and close_to(r["r_gross"], 0.5 / 3 + (2 / 3) * (-0.5)), "gap below the moved stop exits at the open")
r = run("SHORT", 100, 102, [(100, 100.4, 98.8, 99), (99, 99.3, 95.5, 96)])
check(r["status"] == "TP3" and close_to(r["r_gross"], 3.5 / 3), "SHORT mirrored")
flat = [(100, 100.4, 99.6, 100.2)] * 60
r = run("LONG", 100, 98, flat)
check(r["status"] == "EXPIRED" and r["bars"] == 54 and close_to(r["r_gross"], 0.1), "time limit after 54 candles at the close")
r = run("LONG", 100, 98, [(100, 101.2, 99.8, 101)] + [(100.6, 100.9, 100.3, 100.6)] * 5)
check(r["status"] is None and r["hits"] == 1 and close_to(r["stop_now"], 100) and r["bars"] == 6, "still running: TP1 reached, stop at entry")
check(abs(se.cost_r(100, 98) - 0.0012 * 100 / 2) < 1e-12, "fees in R")

print("\n[4] signal rules (replay_all)")
base = {s: se.coin_features(DATA[s]) for s in ("BTC/USDT", "ETH/USDT", "SOL/USDT")}
n = len(base["BTC/USDT"])


def scored(pl):
    pl = np.asarray(pl, float)
    ps = np.full(len(pl), 0.1)
    return {"pl": pl, "ps": ps, "cl": np.full(len(pl), 0.7), "cs": np.full(len(pl), 0.3)}


lo = np.full(n, 0.2)
S = {s: {"F": base[s], **scored(lo)} for s in base}
act, last = se.replay_all(S, lookback=300)
check(not act and not last, "nothing above the levels -> no signals")
strong = lo.copy()
strong[n - 250] = m.threshold + 0.01
S["ETH/USDT"] = {"F": base["ETH/USDT"], **scored(strong)}
closed = {}
act, last = se.replay_all(S, lookback=300, closed=closed)
tr = (act.get("ETH/USDT") or last.get("ETH/USDT"))
check(tr and tr["tier"] == "strong" and tr["side"] == "LONG" and tr["bar_time"] == base["ETH/USDT"]["timestamp"].iloc[n - 250],
      "strong score -> strong signal on that candle")
check(close_to(abs(tr["entry"] - tr["stop_loss"]), 2 * base["ETH/USDT"]["atr"].iloc[n - 250]) and len(tr["targets"]) == 3,
      "stop 2x ATR at the signal candle, three targets")
# standard rule: a mid score only becomes a signal when no coin had one for 24h
mid = lo.copy()
mid[n - 200:] = m.floor + 0.01
S = {"BTC/USDT": {"F": base["BTC/USDT"], **scored(mid)}, "SOL/USDT": {"F": base["SOL/USDT"], **scored(lo)}}
closed = {}
act, last = se.replay_all(S, lookback=300, closed=closed)
trades = closed.get("BTC/USDT", []) + ([act["BTC/USDT"]] if "BTC/USDT" in act else [])
check(trades and all(t["tier"] == "standard" for t in trades), "mid score -> standard signal")
gaps = [(b["bar_time"] - a["bar_time"]).total_seconds() / 3600 for a, b in zip(trades, trades[1:])]
check(all(g >= 24 for g in gaps), "standard signals at most one per 24 hours")
ends = [(t["bar_time"], t["bar_time"] + timedelta(hours=4 * t["bars_held"])) for t in trades]
check(all(ends[k + 1][0] >= ends[k][1] for k in range(len(ends) - 1)), "one trade at a time per coin")
below = lo.copy()
below[n - 200:] = m.floor - 0.01
act, last = se.replay_all({"BTC/USDT": {"F": base["BTC/USDT"], **scored(below)}}, lookback=300)
check(not act and not last, "below the standard level -> still WAIT")
S = {"BTC/USDT": {"F": base["BTC/USDT"], **scored(np.full(n, m.floor + 0.01))},
     "SOL/USDT": {"F": base["SOL/USDT"], **scored(np.full(n, m.floor + 0.02))}}
closed = {}
act, last = se.replay_all(S, lookback=300, closed=closed)
allt = [t for L in closed.values() for t in L] + list(act.values())
first_t = min(t["bar_time"] for t in allt)
at_first = [s for s, L in list(closed.items()) + [(k, [v]) for k, v in act.items()] for t in L if t["bar_time"] == first_t]
check(at_first == ["SOL/USDT"], "the 24-hour rule picks only the best free coin")
spike = np.full(n, m.floor + 0.01)
spike[n - 120] = m.threshold + 0.05
S = {"BTC/USDT": {"F": base["BTC/USDT"], **scored(np.full(n, 0.2))}, "SOL/USDT": {"F": base["SOL/USDT"], **scored(spike)},
     "ETH/USDT": {"F": base["ETH/USDT"], **scored(np.where(np.arange(n) == n - 120, m.threshold + 0.02, 0.2))}}
closed = {}
act, last = se.replay_all(S, lookback=300, closed=closed)
t_spike = base["SOL/USDT"]["timestamp"].iloc[n - 120]
at_spike = {s for s, L in list(closed.items()) + [(k, [v]) for k, v in act.items()] for t in L if t["bar_time"] == t_spike}
check({"ETH/USDT"} <= at_spike and all(t["tier"] == "strong" for L in closed.values() for t in L if t["bar_time"] == t_spike),
      "every coin above the strong level gets a signal on the same candle")
t0_ = pd.Timestamp(base["BTC/USDT"]["timestamp"].iloc[n - 150]).to_pydatetime()
closed = {}
se.replay_all({"BTC/USDT": {"F": base["BTC/USDT"], **scored(mid)}}, start_time=t0_, closed=closed)
check(closed.get("BTC/USDT") and all(t["bar_time"] >= t0_ for t in closed["BTC/USDT"]), "start_time replays from that candle")

print("\n[5] live engine")
calls = []


def fake_get_candles(symbol, timeframe, limit=200, since=None):
    calls.append(symbol)
    assert timeframe == "4h"
    df = DATA[symbol]
    df = df[df["timestamp"] <= CLOCK["now"]]          # what the exchange has (incl. the forming candle)
    if since is not None:
        df = df[df["timestamp"] >= pd.to_datetime(since, unit="ms")]
        return df.head(limit).reset_index(drop=True)
    return df.tail(limit).reset_index(drop=True)


CLOCK = {"now": (T4[900] + pd.Timedelta(hours=2)).to_pydatetime()}   # candle 900 still forming
eng = se.LiveEngine(fake_get_candles)
res = eng.signal("ETH/USDT", CLOCK["now"])
check(res["bar_time"] == T4[899].to_pydatetime() and res["covered"], "uses the last CLOSED 4h candle")
check(res["verdict"] in ("LONG", "SHORT", "WAIT") and 0 <= res["confidence"] <= 100, f"verdict {res['verdict']} {res['confidence']}%")
check(set(calls) >= set(se.signal_coins()), "reads every signal coin (the rules look at all of them)")
n_calls = len(calls)
res2 = eng.signal("ETH/USDT", CLOCK["now"] + timedelta(minutes=50))
check(len(calls) == n_calls and res2 is res, "cached until the next candle closes")
res_sol = eng.signal("SOL/USDT", CLOCK["now"])
check(len(calls) == n_calls and res_sol["covered"], "other signal coins come from the same pass")
res_tao = eng.signal("TAO/USDT", CLOCK["now"])
check(len(calls) > n_calls and res_tao["verdict"] == "WAIT" and not res_tao["covered"] and res_tao["brief"],
      "coin outside the tested list: brief and lean, never a trade")
frames = {s: se.coin_features(DATA[s].iloc[:900]) for s in COINS}
mkt = se.market_block({s: frames[s] for s in se.UNIVERSE})
Xf = se.assemble(frames["ETH/USDT"], se.btc_block(frames["BTC/USDT"]), mkt, "ETH/USDT")
pl, ps, cl, cs = se.score(Xf.iloc[[-1]])
check(abs(res["p_long"] - round(cl[0] * 100, 1)) < 1e-9 and abs(res["p_short"] - round(cs[0] * 100, 1)) < 1e-9,
      "live engine (600 candles) == full-history computation")
Xall = se.assemble(se.coin_features(DATA["ETH/USDT"]), se.btc_block(se.coin_features(DATA["BTC/USDT"])),
                   se.market_block({s: se.coin_features(DATA[s]) for s in se.UNIVERSE}), "ETH/USDT")
pl2, ps2, _, _ = se.score(Xall.iloc[[899]])
check(abs(pl2[0] - pl[0]) < 1e-9 and abs(ps2[0] - ps[0]) < 1e-9, "later candles do not change an earlier signal")
for side in ("LONG", "SHORT"):
    e0 = res["last_close"]
    sl, tp = se.levels(side, e0, res["atr"])
    ok = (sl < e0 < tp) if side == "LONG" else (tp < e0 < sl)
    check(ok and close_to(abs(tp - e0), 2 * abs(e0 - sl)), f"{side}: last target is twice the stop distance")
board = eng.board(CLOCK["now"])
act_rows = [r for r in board.values() if r["active"]]
for r_ in act_rows[:3]:
    a = r_["active"]
    check(r_["verdict"] == a["side"] and r_["targets"] == a["targets"] and r_["take_profit"] == a["targets"][-1]
          and r_["fresh"] == (a["bar_time"] == r_["bar_time"]) and r_["tier"] in ("strong", "standard"),
          f"{a['side']} active trade drives verdict, targets, tier and freshness")
for r_ in [r for r in board.values() if not r["active"]][:2]:
    check(r_["verdict"] == "WAIT" and r_["stop_loss"] is None and r_["targets"] is None and not r_["fresh"], "WAIT has no levels")
check(all(r_["bias"] in ("LONG", "SHORT") and r_["strength"] is not None for r_ in board.values()), "every coin shows bias + strength")
rec = [t for r_ in board.values() for t in r_["recent"]]
check(all(t["status"] in se.CLOSED_STATUSES and t["closed_at"] > t["signal_at"] for t in rec), "recent closed trades listed")
saved = (m.threshold, m.floor)
m.threshold = m.floor = 0.01              # every free coin trades -> active trades on the board
eng2 = se.LiveEngine(fake_get_candles)
board2 = eng2.board(CLOCK["now"])
act2 = [r_ for r_ in board2.values() if r_["active"]]
check(len(act2) >= 10, f"{len(act2)} coins in a trade when every score qualifies")
for r_ in act2[:4]:
    a = r_["active"]
    tps = r_["targets"]
    long_ = a["side"] == "LONG"
    ordered = (r_["stop_loss"] < r_["entry"] < tps[0] < tps[1] < tps[2]) if long_ else (r_["stop_loss"] > r_["entry"] > tps[0] > tps[1] > tps[2])
    check(r_["verdict"] == a["side"] and ordered and r_["take_profit"] == tps[-1] and r_["fresh"] == (a["bar_time"] == r_["bar_time"])
          and r_["tier"] == "strong" and r_["hits"] == a["hits"] and len(r_["tp_pcts"]) == 3,
          f"{a['side']} {r_['tp_pcts']}: verdict, stop, three targets, tier and freshness from the running trade")
    pr = se.progress(a, r_["last_close"])
    check(pr is not None and -100 <= pr["pct"] <= 100, "progress of a running trade at the last close")
m.threshold, m.floor = saved
CLOCK["now"] = (T4[901] + pd.Timedelta(minutes=1)).to_pydatetime()
res3 = eng.signal("ETH/USDT", CLOCK["now"])
check(res3["bar_time"] == T4[900].to_pydatetime() and len(calls) > n_calls, "new candle close -> fresh pass")
check(se.decide(0.5, 0.4)[0] == "WAIT" and se.decide(0.99, 0.2)[0] == "LONG" and se.decide(0.1, 0.99)[0] == "SHORT",
      "decision: strong level, best side wins")

print("\n[6] progress")
a = {"side": "LONG", "entry": 100.0, "stop_loss": 98.0, "take_profit": 104.0, "targets": [101.0, 102.0, 104.0],
     "hits": 0, "stop_now": 98.0}
pr = se.progress(a, 100.5)
check(pr["pct"] == 50.0 and pr["next"] == 1 and close_to(pr["r_now"], 0.25) and pr["state"] == "running", "halfway to TP1")
check(se.progress(a, 99.0)["pct"] == -50.0 and se.progress(a, 97.9)["state"] == "stop_touched", "toward the stop")
check(se.progress(a, 101.1)["state"] == "target_touched", "TP1 touched (confirms at the close)")
b = {**a, "hits": 1, "stop_now": 100.0}
pr = se.progress(b, 101.5)
check(pr["next"] == 2 and pr["pct"] == 50.0, "after TP1: halfway from TP1 to TP2")
pr = se.progress(b, 100.5)
check(pr["pct"] == -50.0 and se.progress(b, 99.9)["state"] == "stop_touched", "after TP1 the stop is the entry")
c_ = {**a, "side": "SHORT", "stop_loss": 102.0, "take_profit": 96.0, "targets": [99.0, 98.0, 96.0], "hits": 2, "stop_now": 99.0}
check(se.progress(c_, 97.0)["next"] == 3 and se.progress(c_, 95.9)["state"] == "target_touched", "short: after TP2, TP3 next")

print(f"\nALL {passed} CHECKS PASSED")
