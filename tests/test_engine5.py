"""Tests for engine 5: the crypto 1h stream (the 4-hour model read at every hourly close), the stream policy
additions (float32 inputs, cost cap, daily tier, replay length, one running signal per coin) and the shipped model
files. SQLite shim, synthetic candles, no network. Run from the project root:  python tests/test_engine5.py
"""
import json
import os
import sys
import zlib
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from dbshim import setup  # noqa: E402

Conn = setup("engine5")

import features as FE  # noqa: E402
import markets  # noqa: E402
import signal_engine as se  # noqa: E402
import streams  # noqa: E402
import signal_hub as hub  # noqa: E402

hub.get_db_connection = Conn
FAILS = []


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# [1] the shipped models: what is live, and why
# ---------------------------------------------------------------------------
print("\n[1] shipped models")
m = streams.load_model("crypto_1h")
check(m is not None and m.version == "5.0" and m.read_tf == "4h" and m.float32 and len(m.features) == 82 and len(m.trees) == 250,
      "crypto 1h: an engine 5 model that reads 4-hour candles (82 inputs, 250 trees, 32-bit inputs)")
check(m.sl_atr == 2.0 and m.targets_r == [0.5] and m.fractions == [1.0] and m.be_after == 0 and m.max_bars == 216
      and m.max_per_bar == 3 and m.cooldown == 4 and m.cost_cap is None and m.gap_hours == 0 and m.gate is None,
      "crypto 1h plan: stop 2 ATR of the 4-hour candles, one target at 0.5R, 216 hourly candles, 3 per candle, no daily tier")
t = m.meta["test"]
check(streams.passed_test(t, "crypto_1h") and t["win_rate"] >= 70 and t["signals"] >= 500 and t["avg_net_r"] >= 0.05
      and t["cost_round_trip"] == markets.CRYPTO_COST_ROUND_TRIP and t["period"].startswith("2025-10"),
      f"crypto 1h passed its test on unseen data with the real cost ({t['signals']} signals, {t['win_rate']}%, {t['avg_net_r']:+}R)")
check(all(v["avg_net_r"] > 0 and v["win_rate"] >= 70 for v in t["folds"].values())
      and all(v["avg_net_r"] > 0 for v in t["by_offset"].values()) and len(t["by_offset"]) == 4,
      "crypto 1h: both walk-forward folds and all four hour offsets of the 4-hour grid were positive")
check(m.meta["walk_forward"]["win_rate"] >= 73 and all(v["avg_net_r"] > 0 for v in m.meta["walk_forward"]["years"].values()),
      "crypto 1h: every validation year was positive")
LIVE = ["crypto_15m", "crypto_1h", "crypto_4h"]
check([k for k in hub.ALL_STREAMS if streams.validated(k)] == LIVE and hub.enabled_streams() == LIVE,
      "live streams: crypto 15m, 1h and 4h; forex has no model that passed")
os.environ["SIGNAL_STREAMS"] = "all"
check(hub.enabled_streams() == LIVE, "SIGNAL_STREAMS=all cannot switch on a stream that did not pass")
os.environ["SIGNAL_STREAMS"] = "crypto_4h,forex_1h"
check(hub.enabled_streams() == ["crypto_4h"], "SIGNAL_STREAMS can only narrow the tested streams down")
del os.environ["SIGNAL_STREAMS"]
check(markets.stream_coins("crypto", "1h") == markets.CRYPTO_COINS and m.signal_symbols == markets.CRYPTO_COINS
      and set(m.meta["trained_symbols"]) <= set(markets.CRYPTO_COINS) and len(m.meta["trained_symbols"]) >= 90,
      "crypto 1h covers every coin; the model was trained and tested on the coins with enough history")
check(streams.plan_for("crypto", "1h") == {"targets_r": [0.5], "fractions": [1.0], "be_after": 0, "max_bars": 216,
                                           "tf_sec": 3600, "engine": "v5.0", "sl_atr": 2.0}
      and streams.plan_for("crypto", "4h")["targets_r"] == [0.5, 1.0, 2.0] and streams.plan_for("crypto", "4h")["engine"] == "v5.0",
      "plans stored with every signal: 1h one target, 4h three targets, both engine 5.0")
check(streams.replay_bars(m) == 2 * 216 + 4 + 2 and streams.hold_text("crypto_1h").startswith("about"),
      "the 1h replay covers two full time limits; the model file gives the expected holding time")
R = json.load(open(os.path.join(ROOT, "models", "research.json")))
check(set(R["streams"]) == set(hub.ALL_STREAMS) and all(R["streams"][k]["live"] == (k in hub.enabled_streams()) for k in hub.ALL_STREAMS),
      "models/research.json agrees with what is live")
check(all(R["streams"][k]["reason"].startswith("Switched off") for k in hub.ALL_STREAMS if k not in hub.enabled_streams())
      and all(R["streams"][k]["reason"].startswith("Live") for k in hub.enabled_streams())
      and "not opened" in R["streams"]["forex_1h"]["reason"] and "69.4%" in R["streams"]["crypto_15m"]["reason"]
      and "82.9%" in R["streams"]["crypto_15m"]["reason"],
      "research.json says in plain words why each stream is on or off (forex never reached the test; the separate 15m "
      "model failed it, the 4-hour model read every 15 minutes passed)")
info = {x["key"]: x for x in hub.stream_info()}
check(info["crypto_1h"]["enabled"] and info["crypto_1h"]["validated"] and info["crypto_1h"]["targets_r"] == [0.5]
      and info["crypto_1h"]["symbols"] == len(markets.CRYPTO_COINS) and info["forex_4h"]["enabled"] is False
      and info["forex_4h"]["research"]["reason"].startswith("Switched off"),
      "stream info for the Signals page: 1h on with its plan, forex off with the reason")
ts_ = hub.tested_summary()
check([x["key"] for x in ts_["streams"]] == LIVE and ts_["signals"] == sum(x["signals"] for x in ts_["streams"])
      and 70 <= ts_["win_rate"] <= 90 and ts_["avg_r"] > 0 and ts_["markets"] == ["crypto"] and ts_["pairs"] == 0,
      "landing summary: the three tested streams and their signal-weighted total")
mt = hub.model_test("crypto", "1h")
check(mt["holdout"]["signals"] == t["signals"] and mt["walk_forward"]["signals"] > 0 and mt["engine"] == "v5.0",
      "the Signals page gets the 1h model's holdout and walk-forward tests")
m4 = se.model()
check(m4.version == "5.0" and m4.targets_r == [0.5, 1.0, 2.0] and streams.passed_test(se.test_stats(), "crypto_4h"),
      "crypto 4h: engine 5.0 with three targets passes the live bar")

print("\n[1b] the 15m stream: the same 4-hour model read at every 15-minute close")
m15 = streams.load_model("crypto_15m")
raw1 = json.load(open(os.path.join(ROOT, "models", "crypto_1h.json")))
raw15 = json.load(open(os.path.join(ROOT, "models", "crypto_15m.json")))
check(m15 is not None and m15.version == "5.0" and m15.tf == "15m" and m15.read_tf == "4h" and m15.float32
      and raw15["trees"] == raw1["trees"] and raw15["features"] == raw1["features"] and raw15["calibration"] == raw1["calibration"],
      "crypto 15m: the very model of the 1h stream (same trees, inputs and calibration), read on 4-hour candles")
check(m15.sl_atr == 2.0 and m15.targets_r == [0.5] and m15.fractions == [1.0] and m15.be_after == 0 and m15.max_bars == 864
      and m15.max_per_bar == 1 and m15.cooldown == 8 and m15.cost_cap is None and m15.gap_hours == 0,
      "crypto 15m plan: the 1h stream's plan in 15-minute candles (864 = 9 days); 1 new signal per candle, cooldown 8")
check(m15.threshold > m.threshold and m15.threshold == raw1["grade_a"] and m15.grade_a == m15.threshold,
      "crypto 15m takes only the model's top 0.5% (the level of the 1h stream's grade A), chosen on the validation years")
t15 = m15.meta["test"]
check(streams.passed_test(t15, "crypto_15m") and t15["win_rate"] >= 70 and t15["avg_net_r"] >= 0.05 and t15["signals"] >= 150
      and t15["cost_round_trip"] == markets.CRYPTO_COST_ROUND_TRIP and t15["period"].startswith("2025-10"),
      f"crypto 15m passed its test on unseen data with the real cost ({t15['signals']} signals, {t15['win_rate']}%, "
      f"{t15['avg_net_r']:+}R)")
check(all(v["avg_net_r"] > 0 and v["win_rate"] >= 70 for v in t15["folds"].values()) and len(t15["by_minute"]) == 4
      and all(v["avg_net_r"] > 0 for v in t15["by_minute"].values()),
      "crypto 15m: both folds and every minute of the hour (:00, :15, :30, :45) were positive")
check(m15.meta["walk_forward"]["win_rate"] >= 73 and all(v["avg_net_r"] > 0 for v in m15.meta["walk_forward"]["years"].values())
      and m15.meta["pre_registered_validation"]["signals_per_day"] > m15.meta["walk_forward"]["signals_per_day"],
      "crypto 15m: every validation year positive; the stricter selection gives fewer signals than the pre-registered one")
ov = t15["overlap_with_1h"]
check(ov["signals_15m"] == t15["signals"] and 0 < ov["also_in_1h_pct"] < 100,
      f"the share of 15m signals the 1h stream also gives is recorded ({ov['also_in_1h_pct']}%)")
check(streams.plan_for("crypto", "15m") == {"targets_r": [0.5], "fractions": [1.0], "be_after": 0, "max_bars": 864,
                                            "tf_sec": 900, "engine": "v5.0", "sl_atr": 2.0}
      and streams.policy_for("crypto", "15m")["max_per_bar"] == 1 and streams.policy_for("crypto", "15m")["cooldown"] == 8,
      "the plan and the selection stored with every 15m signal come from its model file")
check(markets.stream_coins("crypto", "15m") == markets.CRYPTO_COINS and m15.signal_symbols == markets.CRYPTO_COINS
      and streams.hold_text("crypto_15m").startswith("about") and streams.replay_bars(m15) == 2 * 864 + 8 + 2,
      "crypto 15m covers every coin, has its holding time and replays two full time limits")
info15 = {x["key"]: x for x in hub.stream_info()}["crypto_15m"]
check(info15["enabled"] and info15["validated"] and info15["targets_r"] == [0.5] and info15["symbols"] == len(markets.CRYPTO_COINS),
      "stream info for the Signals page: 15m on, one target, every coin")

# ---------------------------------------------------------------------------
# [2] 4-hour candles on a shifted grid
# ---------------------------------------------------------------------------
print("\n[2] shifted candles")
rng = np.random.default_rng(7)
N1 = 203
t0 = datetime(2026, 9, 1, 0, 0)
c_ = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, N1)))
o_ = np.r_[c_[0], c_[:-1]]
H1 = pd.DataFrame({"timestamp": [t0 + timedelta(hours=i) for i in range(N1)], "open": o_,
                   "high": np.maximum(o_, c_) * 1.002, "low": np.minimum(o_, c_) * 0.998, "close": c_,
                   "volume": rng.uniform(1, 3, N1)})
b0 = streams.shifted_candles(H1, "4h", 0)
ref = FE.resample_closed(H1, "4h")
check(len(b0) == len(ref) and np.allclose(b0["close"], ref["close"]) and list(b0["timestamp"]) == list(ref["timestamp"]),
      "no shift = the usual 4-hour candles")
ok = True
for off in (1, 2, 3):
    b = streams.shifted_candles(H1, "4h", off * 3600)
    ok &= all(ts.hour % 4 == off for ts in b["timestamp"])
    last_close = H1["timestamp"].iloc[-1] + pd.Timedelta(hours=1)
    ok &= bool((b["timestamp"] + pd.Timedelta(hours=4) <= last_close).all())
    row = b.iloc[-2]
    part = H1[(H1["timestamp"] >= row["timestamp"]) & (H1["timestamp"] < row["timestamp"] + pd.Timedelta(hours=4))]
    ok &= len(part) == 4 and abs(row["open"] - part["open"].iloc[0]) < 1e-12 and abs(row["close"] - part["close"].iloc[-1]) < 1e-12
    ok &= abs(row["high"] - part["high"].max()) < 1e-12 and abs(row["low"] - part["low"].min()) < 1e-12
    ok &= abs(row["volume"] - part["volume"].sum()) < 1e-9
check(ok, "shifted by 1, 2, 3 hours: candles start at that hour, hold exactly their four hourly candles, only complete ones")
ends = set()
for off in range(4):
    b = streams.shifted_candles(H1, "4h", off * 3600)
    ends |= {ts + pd.Timedelta(hours=4) for ts in b["timestamp"].iloc[2:]}
hours = {ts + pd.Timedelta(hours=1) for ts in H1["timestamp"].iloc[12:]}
check(hours <= ends, "together the four grids have a 4-hour candle ending at every hourly close")

# ---------------------------------------------------------------------------
# [3] the reading board (the site's 1h engine) on synthetic candles
# ---------------------------------------------------------------------------
print("\n[3] the 1h engine")
SEC = markets.TF_SEC
SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT"]
END = datetime(2026, 10, 14)


def candles(symbol, timeframe, limit, since=None, upto=None):
    """Deterministic trending candles per symbol (legs with pullbacks)."""
    rng = np.random.default_rng(zlib.crc32(symbol.encode()) + 11)
    sec = SEC[timeframe]
    base = datetime(2026, 3, 20)
    n = int((END - base).total_seconds() // sec)
    vol = 0.006 * np.sqrt(sec / 3600)
    ret = np.zeros(n)
    i, sign = 0, 1
    while i < n:
        leg = int(rng.integers(40, 160))
        ret[i:i + leg] = sign * vol * rng.uniform(0.05, 0.35) + rng.normal(0, vol, len(ret[i:i + leg]))
        i += leg
        sign = -sign if rng.random() < 0.55 else sign
    cl = 50 * np.exp(np.cumsum(ret))
    op = np.r_[cl[0], cl[:-1]]
    hi = np.maximum(op, cl) * (1 + abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(op, cl) * (1 - abs(rng.normal(0, vol / 2, n)))
    df = pd.DataFrame({"timestamp": [base + timedelta(seconds=sec * k) for k in range(n)], "open": op, "high": hi,
                       "low": lo, "close": cl, "volume": rng.uniform(1, 3, n)})
    if since is not None:
        return df[df.timestamp >= pd.Timestamp(since, unit="ms")].head(limit)
    return df.tail(limit)


m.meta["trained_symbols"] = list(SYMS)        # the market view of the test: these five (the real list has 94)
NOW = datetime(2026, 10, 10, 14, 7)
SEEN = {}
_orig_model_data = streams.model_data


def spy(mm, F, pl, ps, gate=None, sym=None):
    SEEN[sym] = (pd.to_datetime(F["timestamp"]).to_numpy(), np.array(pl, float), np.array(ps, float),
                 F["atr"].to_numpy(float).copy())
    return _orig_model_data(mm, F, pl, ps, gate=gate, sym=sym)


streams.model_data = spy
eng = streams.StreamEngine("crypto", "1h", streams.CandleStore(candles, sleep=0), SYMS)
board = eng.board(NOW)
check(set(board) == set(SYMS) and not eng.errors and all(r["timeframe"] == "1h" and r["engine_version"] == "v5.0"
                                                         and r["verdict"] in ("LONG", "SHORT", "WAIT") for r in board.values()),
      "a result for every coin at the last closed hourly candle")
check(all(0 <= r["p_long"] <= 100 and 0 <= r["p_short"] <= 100 and r["bias"] in ("LONG", "SHORT") and r["targets_r"] == [0.5]
          and r["max_bars"] == 216 and r["bar_time"] == datetime(2026, 10, 10, 13, 0) for r in board.values()),
      "every coin shows its lean, the plan and the candle it was read at")
first = {s: tuple(a.copy() for a in v) for s, v in SEEN.items()}
check(all(np.isfinite(v[1][-300:]).all() and np.isfinite(v[3][-300:]).all() and len(v[0]) == streams.replay_bars(m) + 2
          for v in first.values()),
      "every hourly candle of the replay has a score and an ATR (one of the four 4-hour grids ends at it)")
# the ATR of an hourly candle is the ATR(14) of the 4-hour candle that ends with it
sym = "ETH/USDT"
full = candles(sym, "1h", 10 ** 6)
full = full[full["timestamp"] + pd.Timedelta(hours=1) <= pd.Timestamp(NOW)].tail(4008)
ok = True
for back in (1, 2, 3, 4, 9):
    ts_c = first[sym][0][-back]
    off = (pd.Timestamp(ts_c).hour + 1) % 4
    b = streams.shifted_candles(full, "4h", off * 3600)
    ok &= pd.Timestamp(b["timestamp"].iloc[-1 if back <= 4 and b["timestamp"].iloc[-1] + pd.Timedelta(hours=3) == ts_c else -1]) is not None
    row = b[b["timestamp"] + pd.Timedelta(hours=3) == pd.Timestamp(ts_c)]
    ok &= len(row) == 1
    atr = se._atr(b, 14)
    ok &= abs(float(atr.loc[row.index[0]]) - first[sym][3][-back]) < 1e-9 * first[sym][3][-back]
check(ok, "the stop distance comes from the 4-hour candle that ends at the signal's hourly close (each of the four grids)")

# no look-ahead: a later reading gives the same scores for the candles both readings have
SEEN.clear()
eng2 = streams.StreamEngine("crypto", "1h", streams.CandleStore(candles, sleep=0), SYMS)
eng2.board(NOW + timedelta(hours=7))
ok, n_cmp = True, 0
for s in SYMS:
    ta, pla, psa, atra = first[s]
    tb, plb, psb, atrb = SEEN[s]
    common, ia, ib = np.intersect1d(ta, tb, return_indices=True)
    n_cmp += len(common)
    ok &= len(common) >= 400 and np.allclose(pla[ia], plb[ib], atol=1e-12) and np.allclose(psa[ia], psb[ib], atol=1e-12)
    ok &= np.allclose(atra[ia], atrb[ib], rtol=1e-12)
check(ok, f"no look-ahead: seven hours later every earlier candle has the same scores and stop distance ({n_cmp} candles)")
later = {s: tuple(a.copy() for a in v) for s, v in SEEN.items()}

# the grids kept between scans give the same result as a fresh engine
SEEN.clear()
for k in range(1, 8):
    eng.board(NOW + timedelta(hours=k))
ok = all(np.array_equal(SEEN[s][0], later[s][0]) and np.allclose(SEEN[s][1], later[s][1], atol=1e-12)
         and np.allclose(SEEN[s][2], later[s][2], atol=1e-12) and np.allclose(SEEN[s][3], later[s][3], rtol=1e-12) for s in SYMS)
check(ok and set(eng._grid_cache["grids"]) == {0, 1, 2, 3}, "scanning hour after hour (only the newest grid is computed again) == a fresh engine")
calls = {"n": 0}
_orig_read = eng._read_grid


def counting(*a, **k):
    calls["n"] += 1
    return _orig_read(*a, **k)


eng._read_grid = counting
eng.board(NOW + timedelta(hours=8))
check(calls["n"] == 1, "an hourly scan computes one grid, not four")
eng.board(NOW + timedelta(hours=8, minutes=20))
check(calls["n"] == 1, "nothing is computed again before the next hourly close")

# signals: with the score level at zero every free coin trades, so the plan can be checked on real trades
saved = (m.threshold, m.grade_a, m.grade_b, m.watch)
m.threshold = m.grade_b = m.watch = 0.0
eng3 = streams.StreamEngine("crypto", "1h", streams.CandleStore(candles, sleep=0), SYMS)
b3 = eng3.board(NOW)
trades = [(s, t_) for s, r in b3.items() for t_ in r["recent"] + ([r["active"]] if r["active"] else [])]
check(len(trades) >= 10, f"with the level at zero the coins trade ({len(trades)} signals in the replay)")
ok = True
for s, t_ in trades:
    risk = abs(t_["entry"] - t_["stop_loss"])
    sgn = 1 if t_["side"] == "LONG" else -1
    ok &= abs(risk - 2.0 * t_["atr"]) < 1e-9 * risk and len(t_["targets"]) == 1
    ok &= abs(t_["targets"][0] - (t_["entry"] + sgn * 0.5 * risk)) < 1e-9 * t_["entry"] and t_["take_profit"] == t_["targets"][0]
    ok &= t_["expires_at"] - t_["signal_at"] == timedelta(hours=216) and t_["signal_at"] - t_["bar_time"] == timedelta(hours=1)
    ok &= t_["grade"] in ("A", "B") and t_["tier"] is None and 0 <= t_["confidence"] <= 100 and t_["timeframe"] == "1h"
check(ok, "every 1h signal: stop 2 ATR(4h), one target at 0.5R, 9-day limit, a grade and a calibrated confidence")
ok, per_bar = True, {}
for s in SYMS:
    lst = sorted(b3[s]["recent"], key=lambda x: x["bar_time"])
    for a, b in zip(lst, lst[1:]):
        ok &= b["bar_time"] >= a["closed_at"] + timedelta(hours=m.cooldown - 1)
    for t_ in lst:
        per_bar[t_["bar_time"]] = per_bar.get(t_["bar_time"], 0) + 1
check(ok and max(per_bar.values()) <= 3, "one trade at a time per coin with a cooldown, at most 3 new signals per hourly candle")
closed = [t_ for _, t_ in trades if t_.get("status")]
check(all(t_["status"] in ("TP1", "SL", "EXPIRED") for t_ in closed)
      and all(t_["r"] < 0.5 for t_ in closed if t_["status"] == "TP1") and all(t_["r"] < -1 for t_ in closed if t_["status"] == "SL"),
      "results: the target pays +0.5R minus costs, a stop costs 1R plus costs")
act = [(s, r["active"]) for s, r in b3.items() if r["active"]]
check(all(b3[s]["verdict"] == a["side"] and b3[s]["stop_loss"] == a["stop_loss"] and b3[s]["fresh"] == (a["bar_time"] == b3[s]["bar_time"])
          for s, a in act), "a running trade drives the coin's verdict, levels and freshness")
m.threshold, m.grade_a, m.grade_b, m.watch = saved
streams.model_data = _orig_model_data

# ---------------------------------------------------------------------------
# [4] policy additions
# ---------------------------------------------------------------------------
print("\n[3b] the 15m engine: 16 grids of 4-hour candles, one ending at every 15-minute close")
m15.meta["trained_symbols"] = list(SYMS)
SEEN.clear()
streams.model_data = spy
eng15 = streams.StreamEngine("crypto", "15m", streams.CandleStore(candles, sleep=0), SYMS)
check(eng15.history_need() == ("15m", FE.HISTORY["4h"] * 16 + 32) and eng.history_need() == ("1h", FE.HISTORY["4h"] * 4 + 8)
      and eng15.history_symbols() == SYMS,
      "history the 15m stream reads at every scan: 16 times the 4-hour history (the 1h stream: 4 times)")
b15 = eng15.board(NOW)
check(set(b15) == set(SYMS) and not eng15.errors
      and all(r["timeframe"] == "15m" and r["max_bars"] == 864 and r["targets_r"] == [0.5]
              and r["bar_time"] == datetime(2026, 10, 10, 13, 45) for r in b15.values()),
      "a result for every coin at the last closed 15-minute candle, with the 15m plan")
ok = True
sym = "SOL/USDT"
full15 = candles(sym, "15m", 10 ** 6)
full15 = full15[full15["timestamp"] + pd.Timedelta(minutes=15) <= pd.Timestamp(NOW)].tail(eng15.history_need()[1])
ts15, _, _, atr15 = SEEN[sym]
ok &= np.isfinite(atr15[-200:]).all()
for back in (1, 2, 5, 9, 16, 23):
    ts_c = pd.Timestamp(ts15[-back])
    end = ts_c + pd.Timedelta(minutes=15)
    off = (end.hour * 60 + end.minute) % 240
    b = streams.shifted_candles(full15, "4h", off * 60)
    row = b[b["timestamp"] + pd.Timedelta(hours=3, minutes=45) == ts_c]
    ok &= len(row) == 1
    if len(row):
        atr = se._atr(b, 14)
        ok &= abs(float(atr.loc[row.index[0]]) - atr15[-back]) < 1e-9 * atr15[-back]
check(ok, "the stop distance of a 15m candle comes from the 4-hour candle that ends at its close (any of the 16 grids)")
streams.model_data = _orig_model_data

print("\n[4] stream policy")


class Stub:
    threshold, floor, gap_hours, cost_cap, sl_atr, gate = 0.6, 0.6, 0.0, None, 2.0, None
    float32 = True

    def calibrate(self, p):
        return np.asarray(p, float)

    def grade(self, raw):
        return "A" if raw >= 0.9 else "B"

    def raw_proba(self, X):
        return np.asarray(X, float)[:, 0]


st = Stub()
F = pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=6, freq="h"), "open": 100.0, "high": 101.0, "low": 99.0,
                  "close": 100.0, "atr": [0.5, 1.0, 2.0, 3.0, 4.0, 5.0]})
pl = np.array([0.7, 0.7, 0.7, 0.7, 0.5, 0.95])
d = streams.model_data(st, F, pl, np.full(6, 0.1), sym="SOL/USDT")
check(list(d["side"]) == ["LONG", "LONG", "LONG", "LONG", "", "LONG"] and "strong" not in d and list(d["grade"])[-1] == "A"
      and np.allclose(d["dist"], 2.0 * F["atr"]), "candidates: score at the level or above; stop = sl_atr x ATR")
st.cost_cap = 0.07
d = streams.model_data(st, F, pl, np.full(6, 0.1), sym="SOL/USDT")
cost = markets.CRYPTO_COST_ROUND_TRIP * 100.0 / (2.0 * F["atr"])
check(list(d["side"]) == [("LONG" if (c <= 0.07 and p >= 0.6) else "") for c, p in zip(cost, pl)] and list(d["side"]).count("LONG") == 2,
      "cost cap: a trade whose own cost is above the cap gets no signal (0.30% of the price against the stop distance)")
check(abs(streams.entry_cost_r("EUR/USD", 1.1, 0.0020) - markets.cost_price("EUR/USD") / 0.0020) < 1e-12
      and abs(streams.entry_cost_r("SOL/USDT", 100.0, 4.0) - 0.003 * 100 / 4) < 1e-12, "a trade's own cost in R: spread for forex, fees for crypto")
st.cost_cap, st.floor, st.gap_hours = None, 0.4, 24.0
d = streams.model_data(st, F, pl, np.full(6, 0.1), sym="SOL/USDT")
check(list(d["side"]).count("LONG") == 6 and list(d["strong"]) == [True, True, True, True, False, True],
      "daily tier: candidates from the lower level up, marked strong when they reach the full level")

# 32-bit inputs: the value the trees see is the float32 rounding of the live value, on both sides
X = pd.DataFrame({"bb_pctb": [0.1, 1 / 3], "r6": [0.25, -1 / 7]})
a, b_ = streams.score_sides(st, X)
check(a[0] == float(np.float32(0.1)) and a[1] == float(np.float32(1 / 3)) and a[0] != 0.1
      and b_[1] == float(np.float32(1) - np.float32(1 / 3)), "inputs are rounded to 32-bit floats before the trees (long and mirrored side)")
st.float32 = False
a, _ = streams.score_sides(st, X)
check(a[0] == 0.1, "a model without that flag still gets 64-bit inputs")

# daily tier in run_policy: strong candidates always; a standard one only after 24 hours without a signal
n = 80
ts = pd.date_range("2026-06-01", periods=n, freq="h").to_numpy()
flat = np.full(n, 100.0)


def sym_data(cand, strong):
    side = np.array(["LONG" if i in cand else "" for i in range(n)], dtype=object)
    return {"ts": ts, "o": flat, "h": flat + 0.1, "l": flat - 0.1, "c": flat, "atr": np.full(n, 1.0), "side": side,
            "score": np.where(side != "", 0.7, 0.0), "conf": np.full(n, 70.0), "grade": np.full(n, "C", dtype=object),
            "setup": None, "dist": np.full(n, 2.0), "strong": np.array([i in strong for i in range(n)])}


plan = {"targets_r": [0.5], "fractions": [1.0], "be_after": 0, "max_bars": 2, "tf_sec": 3600}
pol = {"max_per_bar": 3, "cooldown": 0, "gap_hours": 24}
data = {"AAA/USDT": sym_data(set(range(3, n)), {40}), "BBB/USDT": sym_data({40, 41}, {40})}
data["BBB/USDT"]["score"][40] = 0.9
_, _, closed = streams.run_policy(data, "crypto", "1h", plan, pol, start_time=ts[0], lookback=10 ** 9)
got = sorted((t_["bar_time"].hour + 24 * (t_["bar_time"].day - 1), s, t_["tier"]) for s, lst in closed.items() for t_ in lst)
check(got == [(3, "AAA/USDT", "standard"), (27, "AAA/USDT", "standard"), (40, "AAA/USDT", "strong"), (40, "BBB/USDT", "strong"),
              (64, "AAA/USDT", "standard")],
      "daily tier: one standard signal after 24 quiet hours, strong signals always, and a strong signal restarts the 24 hours")
pol0 = dict(pol, gap_hours=0)
for v in data.values():
    v.pop("strong")
data["AAA/USDT"]["side"][:] = ""
data["AAA/USDT"]["side"][5] = "LONG"
_, _, closed = streams.run_policy(data, "crypto", "1h", plan, pol0, start_time=ts[0], lookback=10 ** 9)
check(sorted((s, t_["bar_time"].hour + 24 * (t_["bar_time"].day - 1), t_["tier"]) for s, lst in closed.items() for t_ in lst)
      == [("AAA/USDT", 5, None), ("BBB/USDT", 40, None)],
      "without a daily tier every candidate is an ordinary signal (no tier); a coin in a trade gets no second one")

# costs in the replay: crypto longs pay funding for the hours held; the research can pass the real fill
up = np.r_[np.full(10, 100.0), np.full(n - 10, 101.5)]
one = {"ts": ts, "o": up, "h": up + 0.05, "l": up - 0.05, "c": up, "atr": np.full(n, 1.0),
       "side": np.array(["LONG" if i == 5 else "" for i in range(n)], dtype=object), "score": np.full(n, 0.9),
       "conf": np.full(n, 80.0), "grade": np.full(n, "B", dtype=object), "setup": None, "dist": np.full(n, 2.0)}
long_plan = dict(plan, max_bars=50)
_, _, closed = streams.run_policy({"SOL/USDT": one}, "crypto", "1h", long_plan, pol0, start_time=ts[0], lookback=10 ** 9)
tr = closed["SOL/USDT"][0]
exp = 0.5 - streams.cost_r("SOL/USDT", 100.0, 98.0, hours=tr["bars_held"], side="LONG")
check(tr["status"] == "TP1" and tr["bars_held"] == 5 and abs(tr["r"] - round(exp, 3)) < 1e-9
      and exp < 0.5 - markets.CRYPTO_COST_ROUND_TRIP * 100 / 2, "replay results: fees plus funding for the hours a long was held")
one["slip"] = np.full(n, 0.4)
_, _, closed = streams.run_policy({"SOL/USDT": one}, "crypto", "1h", long_plan, pol0, start_time=ts[0], lookback=10 ** 9)
check(abs(closed["SOL/USDT"][0]["r"] - round(exp - 0.4 / 2.0, 3)) < 1e-9,
      "research replays can enter at the real fill (later than the close) with the stop and target where the signal put them")

# ---------------------------------------------------------------------------
# [5] hub: one running signal per coin and stream; the live record of the engine that runs now
# ---------------------------------------------------------------------------
print("\n[5] hub")
hub.init_tables()
bt = datetime(2026, 10, 13, 8, 0)
sig = {"side": "LONG", "entry": 100.0, "stop_loss": 93.0, "take_profit": 103.5, "targets": [103.5], "confidence": 81.0,
       "grade": "B", "bar_time": bt, "signal_at": bt + timedelta(hours=1)}
ev = hub.record("SOL/USDT", "crypto", "1h", sig)
with hub._Cursor() as cur:
    cur.execute("SELECT * FROM signal_events WHERE symbol='SOL/USDT'")
    row = cur.fetchone()
check(ev is not None and row["engine"] == "v5.0" and json.loads(row["plan"])["targets_r"] == [0.5] and row["grade"] == "B"
      and row["expires_at"] is not None, "a 1h signal is stored with the engine 5 plan")
check(hub._has_active("SOL/USDT", "crypto", "1h") and not hub._has_active("SOL/USDT", "crypto", "4h")
      and not hub._has_active("ETH/USDT", "crypto", "1h"), "running signals are known per coin and stream")


class FakeEngine:
    def __init__(self, rows):
        self.rows = rows

    def board(self, now=None, force=False):
        return self.rows


later_bar = bt + timedelta(hours=3)
fresh = dict(sig, bar_time=later_bar, signal_at=later_bar + timedelta(hours=1))
hub._engines["crypto_1h"] = FakeEngine({"SOL/USDT": {"fresh": True, "active": dict(fresh)},
                                        "ETH/USDT": {"fresh": True, "active": dict(fresh, entry=50.0, stop_loss=46.5, take_profit=51.75, targets=[51.75])},
                                        "XRP/USDT": {"fresh": False, "active": dict(fresh)}, "ADA/USDT": {"error": "no candles"}})
evs = hub.scan_stream("crypto_1h", later_bar + timedelta(hours=1, minutes=1))
check([e["symbol"] for e in evs] == ["ETH/USDT"],
      "scanner: a coin that already has a running signal on the stream gets no second one; older and failed rows are skipped")
check(hub.scan_stream("crypto_1h", later_bar + timedelta(hours=1, minutes=2)) == [], "the same candle is never stored twice")
hub._engines.pop("crypto_1h", None)

# tracker: the target of a one-target signal closes the whole position; stats count the engine that runs now
df = pd.DataFrame({"timestamp": [bt + timedelta(hours=k) for k in range(5)], "open": [99.5, 100.0, 101.0, 102.0, 103.0],
                   "high": [100.2, 101.3, 102.4, 103.6, 103.2], "low": [99.3, 99.6, 100.5, 101.6, 102.5],
                   "close": [100.0, 101.0, 102.0, 103.0, 103.0], "volume": 1.0})
upd = hub.evaluate_row(row, df, now=bt + timedelta(hours=6))
exp = 0.5 - streams.cost_r("SOL/USDT", 100.0, 93.0, hours=3, side="LONG")
check(upd["status"] == "TP1" and upd["hits"] == 1 and abs(upd["exit_price"] - 103.5) < 1e-9 and abs(upd["r_multiple"] - round(exp, 3)) < 1e-9,
      "tracker: the 0.5R target closes a 1h signal at +0.5R minus fees and funding")
with hub._Cursor() as cur:
    sets = ", ".join(f"{k}=%s" for k in upd)
    cur.execute(f"UPDATE signal_events SET {sets} WHERE id=%s", (*upd.values(), row["id"]))
old = hub.record("BTC/USDT", "crypto", "1h", dict(sig, bar_time=bt - timedelta(days=30), signal_at=bt - timedelta(days=30) + timedelta(hours=1)))
with hub._Cursor() as cur:
    cur.execute("UPDATE signal_events SET engine='v4.0', status='SL', r_multiple=-1.05, closed_at=%s WHERE symbol='BTC/USDT'",
                (bt - timedelta(days=29),))
s = hub.stream_stats("crypto", "1h", now=bt + timedelta(days=1))
check(s["engine"] == "v5.0" and s["windows"]["all"]["closed"] == 1 and s["windows"]["all"]["win_rate"] == 100.0
      and s["older_engines"]["closed"] == 1 and s["older_engines"]["win_rate"] == 0.0 and s["total"] == 2,
      "live statistics: the record of the engine that runs now; an older engine's signals are counted apart")
check(hub.signal_json(row)["tested"] is True and hub.signal_json(row)["targets_r"] == [0.5] and hub.signal_json(row)["rr"] is None,
      "signal JSON: a 1h signal is from a tested stream and has one target")

print("\n[5b] a long first history load runs in slices and never holds up the other streams")
CALLS = []


def counting(symbol, timeframe, limit, since=None):
    CALLS.append((symbol, timeframe))
    if symbol == "BAD/USDT":
        raise ValueError("this coin is not on the exchange")
    return candles(symbol, timeframe, limit, since)


st = streams.CandleStore(counting, sleep=0)
saved = (hub._store, dict(hub._engines), hub._FORCE_STREAMS, hub.scan_stream, hub.WARM_SLICE_SEC)
hub._store = st
hub._engines.update({"crypto_1h": streams.StreamEngine("crypto", "1h", st, SYMS),
                     "crypto_15m": streams.StreamEngine("crypto", "15m", st, SYMS + ["BAD/USDT"])})
hub._FORCE_STREAMS = ["crypto_15m", "crypto_1h", "crypto_4h"]
hub._state["scanned"].clear()
hub._warm_failed.clear()
T = datetime(2026, 10, 10, 14, 0, 50)        # the 13:00 hourly and the 13:45 15-minute candle just closed (+45 s)
check(hub.warming("crypto_15m", T) == {"loaded": 0, "total": 6} and hub.warming("crypto_1h", T) == {"loaded": 0, "total": 5}
      and hub.warming("crypto_4h", T) is None, "after a restart nothing is loaded yet (the 4h board loads its own)")
hub.warm("crypto_1h", T, budget=10 ** 6)
check(hub.warming("crypto_1h", T) is None, "a short history (the 1h stream) loads in one slice")
SCANS = []
hub.scan_stream = lambda key, now=None: SCANS.append(key) or []
hub.WARM_SLICE_SEC = 0                        # one coin per slice
acts = hub.step(T)
check(SCANS == ["crypto_1h"] and "warming:crypto_15m:1/6" in acts and hub._state["scanned"]["crypto_15m"] == datetime(2026, 10, 10, 13, 45),
      "the ready 1h stream scans first; the 15m stream loads one coin and gives no signal for that candle")
info15 = {x["key"]: x for x in hub.stream_info()}["crypto_15m"]
check(info15["warming"] == {"loaded": 1, "total": 6}, "the Signals page shows how far the loading is")
for k in range(8):
    hub.step(T + timedelta(minutes=2 + k))     # between candle closes: one slice per pass
check(hub.warming("crypto_15m", T) is None and "crypto_15m" not in hub._state["warming"] and SCANS == ["crypto_1h"]
      and ("crypto_15m", "BAD/USDT") in hub._warm_failed,
      "the slices finish the history; a coin that cannot be loaded does not keep the stream waiting")
SCANS.clear()
hub.step(datetime(2026, 10, 10, 14, 15, 50))
check(SCANS == ["crypto_15m"], "from the next 15-minute close on, the 15m stream scans")
n_calls = len(CALLS)
hub.step(datetime(2026, 10, 10, 14, 17, 0))
check(len(CALLS) == n_calls, "a loaded stream costs no more history calls between candle closes")
hub._store, engines, hub._FORCE_STREAMS, hub.scan_stream, hub.WARM_SLICE_SEC = saved
hub._engines.clear()
hub._engines.update(engines)

print("\n[5c] a copy of the long history in the database: a restart loads it in seconds")
saved = (hub._store, dict(hub._engines), hub._FORCE_STREAMS)
st1 = streams.CandleStore(counting, sleep=0)
hub._store = st1
hub._engines.clear()
hub._engines["crypto_15m"] = streams.StreamEngine("crypto", "15m", st1, SYMS)
hub._FORCE_STREAMS = ["crypto_15m", "crypto_4h"]
hub._warm_failed.clear()
hub._cache_saved.clear()
with hub._Cursor() as cur:
    cur.execute("DELETE FROM candle_cache")
T = datetime(2026, 10, 10, 14, 0, 50)
need = hub.engine("crypto_15m").history_need()[1]
CALLS.clear()
hub.warm("crypto_15m", T, budget=10 ** 6)
first_calls = len(CALLS)
with hub._Cursor() as cur:
    cur.execute("SELECT symbol, depth, n FROM candle_cache WHERE tf='15m'")
    rows = cur.fetchall()
check(hub.warming("crypto_15m", T) is None and sorted(r["symbol"] for r in rows) == sorted(SYMS)
      and all(r["depth"] >= need and r["n"] >= need for r in rows) and first_calls >= 50 * len(SYMS),
      f"the first load asks the exchange ({first_calls} calls) and writes one copy per coin")


def restart(at):
    st = streams.CandleStore(counting, sleep=0)
    hub._store = st
    hub._engines["crypto_15m"] = streams.StreamEngine("crypto", "15m", st, SYMS)
    hub._cache_saved.clear()
    CALLS.clear()
    hub.warm("crypto_15m", at, budget=10 ** 6)
    return st


T2 = T + timedelta(days=1, hours=3)
st2 = restart(T2)
check(hub.warming("crypto_15m", T2) is None and len(CALLS) <= 2 * len(SYMS),
      f"a restart a day later reads the copy: {len(CALLS)} exchange calls instead of {first_calls}")
fresh = streams.CandleStore(candles, sleep=0)
check(all(st2.frame(s_, "15m", T2, bars=need).equals(fresh.frame(s_, "15m", T2, bars=need)) for s_ in SYMS),
      "the copy plus the newest candles is exactly the frame a full load gives")
with hub._Cursor() as cur:
    cur.execute("UPDATE candle_cache SET data=%s WHERE symbol=%s AND tf='15m'", (b"not a copy", "BTC/USDT"))
st3 = restart(T2)
check(sum(1 for c_ in CALLS if c_[0] == "BTC/USDT") >= 50 and sum(1 for c_ in CALLS if c_[0] == "ETH/USDT") <= 2,
      "a damaged copy is not used: that coin loads from the exchange again, the others from their copies")
with hub._Cursor() as cur:
    cur.execute("SELECT n FROM candle_cache WHERE symbol='BTC/USDT' AND tf='15m'")
    check((cur.fetchone() or {}).get("n", 0) >= need, "and its copy is written again")
T_OLD = T + timedelta(days=hub.CACHE_MAX_AGE_DAYS + 1)
st4 = restart(T_OLD)
with hub._Cursor() as cur:
    cur.execute("SELECT updated_at FROM candle_cache WHERE symbol='ETH/USDT' AND tf='15m'")
    upd = hub._parse((cur.fetchone() or {}).get("updated_at"))
check(sum(1 for c_ in CALLS if c_[0] == "ETH/USDT") >= 20 and upd == T_OLD,
      f"a copy older than {hub.CACHE_MAX_AGE_DAYS} days is not used: the full history is loaded again and copied anew")
st5 = restart(T2)
check(hub._refresh_cache(T2 + timedelta(hours=1)) == 0
      and hub._refresh_cache(T2 + timedelta(seconds=hub.CACHE_REFRESH_SEC + 60), limit=2) == 2
      and hub._refresh_cache(T2 + timedelta(seconds=hub.CACHE_REFRESH_SEC + 120), limit=99) == len(SYMS) - 2,
      "the copies are rewritten once a day, a few coins per scanner pass")
hub._store, engines, hub._FORCE_STREAMS = saved
hub._engines.clear()
hub._engines.update(engines)

print("\n[5d] a signal that repeats another stream's running trade says so")
import signalboard as sbd  # noqa: E402
import telegram_bot as tgb  # noqa: E402
bt2 = datetime(2026, 10, 20, 9, 0)
base_sig = {"side": "LONG", "entry": 20.0, "stop_loss": 18.6, "take_profit": 20.35, "targets": [20.35], "confidence": 76.0,
            "grade": "A", "bar_time": bt2, "signal_at": bt2 + timedelta(hours=1)}
e1 = hub.record("AVAX/USDT", "crypto", "1h", base_sig)
e15 = hub.record("AVAX/USDT", "crypto", "15m", dict(base_sig, bar_time=bt2 + timedelta(minutes=30),
                                                   signal_at=bt2 + timedelta(minutes=45)))
e15s = hub.record("AVAX/USDT", "crypto", "15m", dict(base_sig, side="SHORT", stop_loss=21.4, take_profit=19.65,
                                                    targets=[19.65], bar_time=bt2 + timedelta(minutes=45),
                                                    signal_at=bt2 + timedelta(hours=1)))
check(e1 is not None and "same_as" not in e1 and e15["same_as"] == {"timeframe": "1h", "signal_at": "2026-10-20T10:00:00Z"}
      and e15s is not None and "same_as" not in e15s,
      "a 15m long while the 1h long runs on the coin is marked; the first signal and an opposite one are not")
txt = sbd.event_text(e15)
mail = sbd._email_html([e15, e15s], user_id=None)
tg = tgb.signal_block(e15)
check("the same coin and side as the running 1h signal on AVAX/USDT: taking both doubles the risk" in txt
      and "The same coin and side as the running 1h signal on AVAX/USDT" in mail and mail.count("same coin and side") == 1
      and "same coin and side as the running 1h signal: taking both doubles the risk" in tg
      and "same coin and side" not in sbd.event_text(e15s) + tgb.signal_block(e15s),
      "the app notification, the email and Telegram say it; the other signals do not")
with hub._Cursor() as cur:
    cur.execute("SELECT * FROM signal_events WHERE symbol='AVAX/USDT' ORDER BY id")
    rows_ = cur.fetchall()
listed = hub.mark_same_trades([hub.signal_json(r) for r in rows_])
by_tf = {(x["timeframe"], x["side"]): x for x in listed}
check(by_tf[("15m", "LONG")]["same_as"]["timeframe"] == "1h" and by_tf[("1h", "LONG")]["same_as"]["timeframe"] == "15m"
      and "same_as" not in by_tf[("15m", "SHORT")],
      "the Signals page marks both running signals of the same trade (each points to the other stream)")
with hub._Cursor() as cur:
    cur.execute("UPDATE signal_events SET status='TP1' WHERE symbol='AVAX/USDT' AND timeframe='1h'")
listed = hub.mark_same_trades([hub.signal_json(r) for r in rows_])
check(all("same_as" not in x for x in listed if x["timeframe"] == "15m"), "once the 1h signal has closed the mark goes away")
tpl = open(os.path.join(ROOT, "templates", "signals.html"), encoding="utf-8").read()
check("s.same_as" in tpl and "Also running on" in tpl and "doubles the risk on this coin" in tpl,
      "the signal card shows the mark with its reason")

print()
print(f"{len(FAILS)} failed" if FAILS else "ALL PASSED")
sys.exit(1 if FAILS else 0)
