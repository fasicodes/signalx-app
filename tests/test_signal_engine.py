"""Tests for signal_engine.py (signal engine v2). No network: candles are synthetic.
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


DATA = {sym: synth(k) for k, sym in enumerate(se.UNIVERSE)}
DATA["TAO/USDT"] = synth(99, drift=0.001)   # a coin outside the 16-coin universe

print("\n[1] model file")
m = se.model()
check(len(m.features) == 82 and len(m.trees) == 150, "82 features, 150 trees")
check(m.sl_atr == 3.0 and m.tp_atr == 1.5 and m.max_bars == 48, "levels: 3x ATR stop, 1.5x ATR target, 48 candles")
check(0.5 < m.threshold < 0.95, f"signal threshold {m.threshold}")
t = se.test_stats()
check(t["signals"] > 100 and 0 < t["win_rate"] < 100 and "avg_net_r" in t, "out-of-sample test stats bundled")
p = m.raw_proba(np.full((3, 82), np.nan))
check(np.all((p > 0) & (p < 1)) and np.allclose(p, p[0]), "all-missing input is handled (NaN-aware trees)")
cal = m.calibrate(np.array([0.0, 0.5, 0.99]))
check(np.all(np.diff(cal) >= 0), "calibration is monotonic")

print("\n[2] features")
F = se.coin_features(DATA["BTC/USDT"])
check(len(F) == N and F["atr"].iloc[300:].notna().all(), "per-candle features")
day_cols = [c for c in F.columns if c.startswith("h_")]
check(len(day_cols) == 12, "daily-trend features present")
# the daily features of a 4h candle must only use days that had already closed
row = F.iloc[700]
t_close = row["timestamp"] + pd.Timedelta(hours=4)
day = se.resample(DATA["BTC/USDT"], "1D")
closed_days = day[day["timestamp"] + pd.Timedelta(days=1) <= t_close]
ref = se.base_features(closed_days)["rsi14"].iloc[-1]
check(abs(row["h_rsi14"] - ref) < 1e-9, "daily features use only closed days (no look-ahead)")
X = se.assemble(F, se.btc_block(F), se.market_block({s: se.coin_features(d) for s, d in DATA.items() if s in se.UNIVERSE}), "BTC/USDT")
check(list(X.columns) == m.features, "model input in model order")
G = se.flip_for_short(X)
check(np.allclose(G["rsi14"], 100 - X["rsi14"], equal_nan=True) and np.allclose(G["r6"], -X["r6"], equal_nan=True)
      and np.allclose(G["upw"], X["loww"], equal_nan=True) and np.allclose(G["atr_pct"], X["atr_pct"], equal_nan=True),
      "short side mirrors directional features only")
check(se.price_round(0.0000123456789) == 0.00001234568 and se.price_round(64321.123) == 64321.12, "price rounding keeps cheap coins precise")

print("\n[3] live engine")
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
check(res["bar_time"] == T4[899].to_pydatetime(), "uses the last CLOSED 4h candle")
check(res["verdict"] in ("LONG", "SHORT", "WAIT") and 0 <= res["confidence"] <= 100, f"verdict {res['verdict']} {res['confidence']}%")
check(set(calls) >= set(se.UNIVERSE), "reads the whole universe for market context")
n_calls = len(calls)
res2 = eng.signal("ETH/USDT", CLOCK["now"] + timedelta(minutes=50))
check(len(calls) == n_calls and res2 is res, "cached until the next candle closes")
res_btc = eng.signal("BTC/USDT", CLOCK["now"])
check(len(calls) == n_calls, "universe coins reuse the cached candles")
res_tao = eng.signal("TAO/USDT", CLOCK["now"])
check(len(calls) > n_calls and res_tao["verdict"] in ("LONG", "SHORT", "WAIT"), "coin outside the universe works")
# live result == backtest computation on full history at the same candle (no look-ahead, same numbers)
frames = {s: se.coin_features(DATA[s].iloc[:900]) for s in se.UNIVERSE}
mkt = se.market_block(frames)
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
    check(ok and abs(abs(e0 - sl) - 2 * abs(tp - e0)) < 1e-9, f"{side} stop is twice the target distance")
if res["active"]:
    a = res["active"]
    check(res["verdict"] == a["side"] and res["stop_loss"] == a["stop_loss"] and res["fresh"] == (a["bar_time"] == res["bar_time"]),
          "active trade drives verdict, levels and freshness")
else:
    check(res["verdict"] == "WAIT" and res["stop_loss"] is None and not res["fresh"], "no active trade -> WAIT, no levels")
check(res["bias"] in ("LONG", "SHORT") and res["strength"] is not None and res["strength"] > 0, "WAIT still shows bias + strength")

# replay with a forced decision rule: active / last trade follow the one-at-a-time rules
orig = se.decide
k = {"n": 0}


def every_9th(pl, ps):
    k["n"] += 1
    return (("LONG" if pl >= ps else "SHORT") if k["n"] % 9 == 0 else "WAIT"), max(pl, ps)


se.decide = every_9th
FX = se.coin_features(DATA["SOL/USDT"].iloc[:900])
Xs = se.assemble(FX, se.btc_block(frames["BTC/USDT"]), mkt, "SOL/USDT")
a1, b1, c1, d1 = se.score(Xs)
act, last = se.replay(FX, a1, b1, c1, d1)
check(last is not None and last["status"] in ("TP", "SL", "EXPIRED") and last["closed_at"] > last["signal_at"], "last closed trade reported")
if act:
    check(act["bars_held"] < 48 and act["expires_at"] > act["signal_at"], "active trade is still inside its 8-day window")
se.decide = orig
fake = {"side": "LONG", "entry": 100.0, "stop_loss": 94.0, "take_profit": 103.0}
pr = se.progress(fake, 101.5)
check(pr["pct"] == 50.0 and abs(pr["r_now"] - 0.25) < 1e-12 and pr["state"] == "running", "progress toward target")
check(se.progress(fake, 97.0)["pct"] == -50.0 and se.progress(fake, 93.0)["state"] == "stop_touched", "progress toward stop")
check(se.progress({**fake, "side": "SHORT", "stop_loss": 106.0, "take_profit": 97.0}, 97.0)["state"] == "target_touched", "short target")
CLOCK["now"] = (T4[901] + pd.Timedelta(minutes=1)).to_pydatetime()
res3 = eng.signal("ETH/USDT", CLOCK["now"])
check(res3["bar_time"] == T4[900].to_pydatetime() and len(calls) > n_calls, "new candle close -> fresh signal")
check(se.decide(0.5, 0.4)[0] == "WAIT" and se.decide(0.99, 0.2)[0] == "LONG" and se.decide(0.1, 0.99)[0] == "SHORT",
      "decision: only above the threshold, best side wins")
check(abs(se.cost_r(100, 97) - 0.0012 * 100 / 3) < 1e-12, "fees in R")

print(f"\nALL {passed} CHECKS PASSED")
