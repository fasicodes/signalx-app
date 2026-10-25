"""Tests for markets.py, streams.py and signal_hub.py (SQLite shim, fake candles,
no network). Run from the project root:  python tests/test_signal_hub.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from dbshim import setup  # noqa: E402

import shutil  # noqa: E402
import tempfile  # noqa: E402

# the tests must not depend on the model files that ship with the site: they run against an empty models folder
# (rules engine fallback); the shipped crypto 1h model is checked separately below
_MD = tempfile.mkdtemp(prefix="models_")
os.environ["MODELS_DIR"] = _MD

Conn = setup("signal_hub")

import markets  # noqa: E402
import streams  # noqa: E402
import signal_hub as hub  # noqa: E402
import signalboard as sb  # noqa: E402

hub.get_db_connection = Conn
sb.get_db_connection = Conn

FAILS = []


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# markets: pips, weekend, daylight saving, sessions
# ---------------------------------------------------------------------------
check(markets.pip_size("EUR/USD") == 0.0001 and markets.pip_size("USD/JPY") == 0.01 and markets.pip_size("XAU/USD") == 0.1,
      "pip sizes: majors 0.0001, JPY 0.01, gold 0.1")
check(abs(markets.to_pips("EUR/USD", 0.0025) - 25) < 1e-9 and abs(markets.to_pips("XAU/USD", 3.5) - 35) < 1e-9,
      "price difference -> pips")
check(len(markets.FOREX_MAJORS) == 7 and all(p in markets.forex_signal_pairs() for p in markets.FOREX_MAJORS)
      and "XAU/USD" in markets.forex_signal_pairs() and "EUR/JPY" in markets.forex_signal_pairs(),
      "forex signal list: 7 majors + crosses + gold")
os.environ["FOREX_SIGNAL_PAIRS"] = "EUR/USD, gbp/usd, NOPE/XXX"
check(markets.forex_signal_pairs() == ["EUR/USD", "GBP/USD"], "FOREX_SIGNAL_PAIRS env override keeps only known pairs")
del os.environ["FOREX_SIGNAL_PAIRS"]
check(markets.forex_week_close(datetime(2026, 1, 14, 10)) == datetime(2026, 1, 16, 22, 0),
      "weekly close in winter: Friday 22:00 UTC (17:00 New York)")
check(markets.forex_week_close(datetime(2026, 7, 15, 10)) == datetime(2026, 7, 17, 21, 0),
      "weekly close in summer: Friday 21:00 UTC (daylight saving)")
check(not markets.forex_open(datetime(2026, 7, 18, 12)) and not markets.forex_open(datetime(2026, 7, 19, 20))
      and markets.forex_open(datetime(2026, 7, 19, 21, 30)) and markets.forex_open(datetime(2026, 7, 15, 12)),
      "forex closed Saturday and Sunday before 17:00 NY, open after")
check("London" in markets.sessions_at(datetime(2026, 7, 15, 9)) and "New York" in markets.sessions_at(datetime(2026, 7, 15, 14))
      and markets.sessions_at(datetime(2026, 7, 18, 12)) == [], "sessions: London morning, New York afternoon, none on Saturday")

# ---------------------------------------------------------------------------
# trade following (plan, stop moves, weekend exit)
# ---------------------------------------------------------------------------
plan = {"targets_r": [1.0, 2.0, 3.0], "fractions": [1 / 3] * 3, "be_after": 1, "max_bars": 10}
o = np.array([100, 100, 101, 101.5, 100.5])
h = np.array([100.5, 101.2, 101.8, 101.6, 100.6])
l = np.array([99.5, 99.8, 100.6, 100.4, 99.0])
c = np.array([100, 101, 101.5, 100.6, 99.5])
r = streams.follow("LONG", 100.0, 99.0, o, h, l, c, 1, plan)
check(r["status"] == "TP1" and abs(r["r_gross"] - 1 / 3) < 1e-9,
      "follow: TP1 hit, then the stop at entry closes the rest at 0R -> +0.33R")
r = streams.follow("SHORT", 100.0, 101.0, o, h, l, c, 1, plan)
check(r["status"] == "SL" and abs(r["r_gross"] + 1) < 1e-9, "follow: short stopped out -> -1R")
r = streams.follow("LONG", 100.0, 99.0, o[:2], h[:2], l[:2], c[:2], 1, plan)
check(r["status"] is None and r["hits"] == 1, "follow: still running when candles run out")
ts = pd.Series(pd.date_range("2026-07-17 18:00", periods=3, freq="h"))   # Friday 18:00-20:00 UTC, close 21:00
mask = streams.weekend_exit_mask(ts, 3600)
check(list(mask) == [False, False, True], "weekend exit: the last candle before the Friday close")
r = streams.follow("LONG", 100.0, 99.0, np.array([100, 100.2, 100.3]), np.array([100.4, 100.5, 100.6]),
                   np.array([99.9, 99.9, 99.9]), np.array([100.2, 100.3, 100.5]), 0, plan, exit_mask=mask)
check(r["status"] == "EXPIRED" and abs(r["exit_price"] - 100.5) < 1e-9, "forex trade closed at the weekly close, no weekend gap")
check(abs(streams.cost_r("EUR/USD", 1.1000, 1.0980) - 1.2 * 0.0001 / 0.002) < 1e-9, "forex costs in R from the pair's spread")
check(streams.grade_of(85) == "A" and streams.grade_of(72) == "B" and streams.grade_of(61) == "C", "grades A/B/C from the score")

# ---------------------------------------------------------------------------
# rules engine on fake candles
# ---------------------------------------------------------------------------
SEC = markets.TF_SEC


def fake_candles(symbol, timeframe, limit, since=None):
    rng = np.random.default_rng(abs(hash(symbol)) % 2 ** 32)
    sec = SEC[timeframe]
    base = datetime(2026, 5, 1)
    n = int((datetime(2026, 10, 14) - base).total_seconds() // sec)
    ret = rng.normal(0, 0.004 * np.sqrt(sec / 900), n) + np.sin(np.arange(n) / 250) * 0.0006
    cl = 100 * np.exp(np.cumsum(ret))
    op = np.r_[cl[0], cl[:-1]]
    hi = np.maximum(op, cl) * (1 + abs(rng.normal(0, 0.002, n)))
    lo = np.minimum(op, cl) * (1 - abs(rng.normal(0, 0.002, n)))
    df = pd.DataFrame({"timestamp": [base + timedelta(seconds=sec * i) for i in range(n)], "open": op, "high": hi,
                       "low": lo, "close": cl, "volume": rng.uniform(1, 3, n)})
    if since is not None:
        df = df[df.timestamp >= pd.Timestamp(since, unit="ms")]
        return df.head(limit)
    return df.tail(limit)


# ---------------------------------------------------------------------------
# shared candle cache: every stream and the tracker use one cache, with different lengths
# ---------------------------------------------------------------------------
def capped(symbol, timeframe, limit, since=None, cap=300):
    """Like the exchange: at most `cap` candles per call."""
    return fake_candles(symbol, timeframe, min(int(limit), cap), since)


CNOW = datetime(2026, 10, 13, 12, 7)
cs = streams.CandleStore(capped, sleep=0)
check(len(cs.frame("BTC/USDT", "1h", CNOW, bars=3000)) == 3000, "candle cache: a long 1h history loads in pages of 300")
cs.frame("BTC/USDT", "1h", CNOW + timedelta(minutes=16), bars=320)      # the 15m rules: their 1h trend
cs.frame("BTC/USDT", "1h", CNOW + timedelta(minutes=20))                # the result tracker: default length
again = cs.frame("BTC/USDT", "1h", CNOW + timedelta(hours=1), bars=3000)
check(len(again) == 3000 and len(cs._data[("BTC/USDT", "1h")]) >= 3000,
      "candle cache: shorter requests never cut back the long history (the 1h model and its 4h check need it)")
check(again["timestamp"].iloc[-1] == pd.Timestamp(CNOW + timedelta(hours=1)).floor("h") - pd.Timedelta(hours=1),
      "candle cache: the long history is topped up to the last closed candle")
small = cs.frame("BTC/USDT", "1h", CNOW + timedelta(hours=1, minutes=5), bars=320)
check(len(small) == max(320, cs.history), "candle cache: short requests still get their usual length")
cs100 = streams.CandleStore(lambda symbol, timeframe, limit, since=None: capped(symbol, timeframe, limit, since, cap=100),
                            sleep=0)
check(len(cs100.frame("ETH/USDT", "1h", CNOW, bars=3000)) == 3000,
      "candle cache: still loads the full history when the exchange sends only 100 candles per call")
FXNOW = [CNOW]


def fx_candles(symbol, timeframe, limit, since=None):
    """Like a forex data provider: candles up to now, none while the market is closed."""
    df = fake_candles(symbol, timeframe, 10 ** 6, since)
    df = df[(df["timestamp"] <= pd.Timestamp(FXNOW[0])) & streams.forex_open_mask(df["timestamp"])]
    return df.head(limit) if since is not None else df.tail(limit)


fxs = streams.CandleStore(fx_candles, sleep=0)
fxs.frame("EUR/USD", "1h", CNOW)                                         # the forex 1h stream ran first
FXNOW[0] = CNOW + timedelta(hours=1)
h4 = fxs.frame("EUR/USD", "4h", FXNOW[0])
check(len(h4) > streams.WARMUP + 5, f"candle cache: forex 4h (built from 1h) has enough history after the forex 1h stream ({len(h4)})")

NOW = datetime(2026, 10, 13, 12, 7)
store = streams.CandleStore(fake_candles, sleep=0)
eng = streams.StreamEngine("crypto", "1h", store, ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"])
board = eng.board(NOW)
check(set(board) == {"BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"} and all("verdict" in v for v in board.values()),
      "rules engine: a result for every symbol")
recent = [t for v in board.values() for t in v["recent"]]
check(len(recent) > 0, f"rules engine: replay finds closed signals ({len(recent)})")
ok_levels = all((t["side"] == "LONG" and t["stop_loss"] < t["entry"] < t["targets"][0] < t["targets"][1] < t["targets"][2]) or
                (t["side"] == "SHORT" and t["stop_loss"] > t["entry"] > t["targets"][0] > t["targets"][1] > t["targets"][2])
                for t in recent)
check(ok_levels, "every signal: stop and targets on the right sides of the entry")
check(all(t["grade"] in ("A", "B", "C") and t["score"] >= streams.MIN_SCORE and t["setup"] in
          ("breakout", "pullback", "trend_continuation", "reversal") and t["rr"] >= 1.5 for t in recent),
      "every signal: a grade, a qualified setup, score >= minimum and R:R >= 1.5")
d0 = recent[0]
risk = abs(d0["entry"] - d0["stop_loss"])
check(d0["atr"] * streams.SL_ATR_MIN - 1e-9 <= risk <= d0["atr"] * streams.SL_ATR_MAX + 1e-9, "ATR stop between 1.0 and 2.5 ATR")
by_sym = {}
for t in sorted(recent, key=lambda x: x["bar_time"]):
    by_sym.setdefault(t["symbol"] if "symbol" in t else id(t), []).append(t)
overlap = False
for sym, v in board.items():
    lst = sorted(v["recent"], key=lambda x: x["bar_time"])
    for a, b in zip(lst, lst[1:]):
        if b["bar_time"] < a["closed_at"]:
            overlap = True
check(not overlap, "one trade at a time per symbol (no overlap)")
per_bar = {}
for t in recent:
    per_bar[t["bar_time"]] = per_bar.get(t["bar_time"], 0) + 1
check(max(per_bar.values()) <= streams.MAX_PER_BAR, "at most 5 new signals per candle")

fx = streams.StreamEngine("forex", "1h", store, ["EUR/USD", "USD/JPY", "XAU/USD"])
fb = fx.board(NOW)
fxr = [t for v in fb.values() for t in v["recent"]]
check(all(markets.forex_open(t["bar_time"]) for t in fxr), "forex: no signal on a weekend candle")
check(all(set(markets.sessions_at(t["signal_at"])) & {"London", "New York"} for t in fxr),
      "forex 1h: signals only in the London / New York sessions")
check(all("pips" in t for t in fxr) and all(t.get("sl_pips") for t in fxr), "forex results and stops in pips")
check(all(t["closed_at"] <= markets.forex_week_close(t["bar_time"]) + timedelta(hours=1) for t in fxr),
      "forex trades never run over a weekend")
blocked = streams.StreamEngine("forex", "1h", store, ["EUR/USD", "USD/JPY", "XAU/USD"],
                               blocked=lambda s, at: "news" if s == "EUR/USD" else None)
bb = blocked.board(NOW)
check(not bb["EUR/USD"]["recent"] and not bb["EUR/USD"]["active"], "news blackout hook stops new signals for that pair")

# ---------------------------------------------------------------------------
# migration keeps old rows and gives them crypto / 4h
# ---------------------------------------------------------------------------
cn = Conn()
cu = cn.cursor()
cu.execute("""CREATE TABLE signal_events (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol VARCHAR(30) NOT NULL,
              side VARCHAR(5) NOT NULL, bar_time DATETIME NOT NULL, entry DOUBLE NOT NULL, stop_loss DOUBLE NOT NULL,
              take_profit DOUBLE NOT NULL, confidence DOUBLE NULL, created_at DATETIME NOT NULL)""")
cu.execute("""INSERT INTO signal_events (symbol, side, bar_time, entry, stop_loss, take_profit, confidence, created_at)
              VALUES ('BTC/USDT','LONG','2026-10-01 08:00:00',60000,59000,62000,78,'2026-10-01 12:01:00')""")
cu.execute("""CREATE TABLE signal_alert_subs (user_id INT PRIMARY KEY, enabled TINYINT(1) NOT NULL DEFAULT 1,
              email TINYINT(1) NOT NULL DEFAULT 0, coins TEXT NULL, emails_day VARCHAR(10) NULL,
              emails_sent INT NOT NULL DEFAULT 0, updated_at DATETIME NOT NULL)""")
cu.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
cu.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, category TEXT, symbol TEXT, title TEXT, message TEXT)")
cn.close()
sb.init_tables()
sb.init_tables()   # twice: the migration must be safe to repeat
with hub._Cursor() as cur:
    cur.execute("SELECT * FROM signal_events")
    rows = cur.fetchall()
check(len(rows) == 1 and rows[0]["market"] == "crypto" and rows[0]["timeframe"] == "4h" and rows[0]["status"] == "ACTIVE"
      and rows[0]["entry"] == 60000, "migration keeps the old signal and gives it crypto / 4h / active")
with hub._Cursor() as cur:
    cols = hub._columns(cur, "signal_alert_subs")
check({"markets", "timeframes"} <= cols, "alert settings get market and timeframe columns")

# ---------------------------------------------------------------------------
# recording: streams never overwrite each other
# ---------------------------------------------------------------------------
with hub._Cursor() as cur:
    cur.execute("DELETE FROM signal_events")
bt = datetime(2026, 10, 13, 8, 0)
a = {"side": "LONG", "entry": 100.0, "stop_loss": 99.0, "take_profit": 103.0, "targets": [101.0, 102.0, 103.0],
     "confidence": 74.0, "grade": "B", "setup": "pullback", "bar_time": bt, "signal_at": bt + timedelta(hours=1)}
e1 = hub.record("SOL/USDT", "crypto", "1h", a)
e2 = hub.record("SOL/USDT", "crypto", "15m", dict(a, signal_at=bt + timedelta(minutes=15)))
e3 = hub.record("SOL/USDT", "crypto", "1h", a)
check(e1 is not None and e2 is not None and e3 is None, "15m and 1h signal on the same symbol + candle both stored; duplicates ignored")
with hub._Cursor() as cur:
    cur.execute("SELECT * FROM signal_events WHERE timeframe='1h'")
    row = cur.fetchone()
check(json.loads(row["plan"])["max_bars"] == streams.STREAMS["crypto_1h"]["max_bars"] and row["grade"] == "B"
      and row["expires_at"] is not None, "stored with its plan, grade and expiry")

# tracker: TP1 then back to the entry -> TP1, +1/3 R minus costs
df = pd.DataFrame({"timestamp": [bt + timedelta(hours=k) for k in range(4)],
                   "open": [99.5, 100.0, 101.0, 100.6], "high": [100.2, 101.3, 101.4, 100.7],
                   "low": [99.3, 99.6, 100.5, 99.9], "close": [100.0, 101.0, 100.6, 100.1], "volume": 1.0})
upd = hub.evaluate_row(row, df, now=bt + timedelta(hours=5))
exp_r = 1 / 3 - streams.cost_r("SOL/USDT", 100.0, 99.0)
check(upd["status"] == "TP1" and abs(upd["r_multiple"] - round(exp_r, 3)) < 1e-6 and upd["closed_at"] == bt + timedelta(hours=4),
      "tracker: TP1 reached, then stopped at entry; result after costs and close time")
running = hub.evaluate_row(row, df.iloc[:2], now=bt + timedelta(hours=3))
check("status" not in running and running["hits"] == 1 and abs(running["stop_now"] - 100.0) < 1e-9,
      "tracker: running signal gets its hits and the moved stop")

hub._hooks["get_candles"] = lambda symbol, timeframe, limit, since=None: df if timeframe == "1h" else None
hub._store = None
store_fail = hub.store()
store_fail.frame = lambda *a, **k: (_ for _ in ()).throw(ValueError("no cache"))
n = hub.track_open(now=bt + timedelta(hours=5))
with hub._Cursor() as cur:
    cur.execute("SELECT status FROM signal_events WHERE timeframe='1h'")
    st1 = cur.fetchone()["status"]
check(n >= 1 and st1 == "TP1", "track_open writes the result to the database")

# stats only from closed real signals, with windows
with hub._Cursor() as cur:
    cur.execute("UPDATE signal_events SET closed_at=%s WHERE timeframe='1h'", (datetime(2026, 9, 1),))
s = hub.stream_stats("crypto", "1h", now=datetime(2026, 10, 14))
check(s["windows"]["all"]["closed"] == 1 and s["windows"]["all"]["win_rate"] == 100.0 and s["windows"]["30d"]["closed"] == 0
      and s["windows"]["7d"]["win_rate"] is None, "stats: all-time counts the closed signal; 30d/7d windows exclude it")
s15 = hub.stream_stats("crypto", "15m")
check(s15["active"] == 1 and s15["windows"]["all"]["closed"] == 0 and s15["windows"]["all"]["win_rate"] is None,
      "stats per stream: the 15m record is separate and has no invented win rate")

# scanner: fresh signals of a rules stream are stored once
hub._hooks["get_candles"] = fake_candles
hub._store = streams.CandleStore(fake_candles, sleep=0)
hub._engines.clear()
os.environ["SIGNAL_STREAMS"] = "crypto_1h"
hub.symbols_for = lambda market: ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"]
due = hub.due_streams(datetime(2026, 10, 13, 12, 0, 30))
check(due == [], "scanner waits for the publish delay after the close")
due = hub.due_streams(datetime(2026, 10, 13, 12, 1, 0))
check(due == [("crypto_1h", datetime(2026, 10, 13, 11, 0))], "scanner: crypto 1h due after the 12:00 close")
evs = hub.scan_stream("crypto_1h", NOW)
again = hub.scan_stream("crypto_1h", NOW)
fresh = [k for k, v in hub.engine("crypto_1h").board(NOW).items() if v.get("fresh")]
check(len(evs) == len(fresh) and again == [], f"scanner stores each fresh signal once ({len(evs)} new)")
os.environ.pop("SIGNAL_STREAMS")
check(len(hub.enabled_streams()) == 6 and hub.enabled_streams() == hub.ALL_STREAMS,
      "default: all six streams run (the ones that have not passed the test are marked untested)")
os.environ["SIGNAL_STREAMS"] = "validated"
check(hub.enabled_streams() == hub.validated_streams() and "crypto_4h" in hub.enabled_streams(),
      "SIGNAL_STREAMS=validated: only the streams whose model passed its test on unseen data (crypto 4h always)")
check(hub.validated_streams() == ["crypto_4h"], "no model file -> only crypto 4h counts as tested")
os.environ["SIGNAL_STREAMS"] = "bogus"
check(len(hub.enabled_streams()) == 6, "SIGNAL_STREAMS with only unknown names falls back to all six")
os.environ.pop("SIGNAL_STREAMS")
check(hub.stream_tested("crypto", "4h") and not hub.stream_tested("crypto", "15m") and not hub.stream_tested("forex", "1h")
      and not hub.stream_tested("forex", "15m") and not hub.stream_tested("forex", "4h"),
      "stream_tested: only crypto 4h (and a stream with a passed test) is tested")

# every stream that is on by default really scans and stores signals (15m, forex and gold included):
# scan 60 consecutive candle closes of each stream on the fake candles
import zlib  # noqa: E402


def trending_candles(symbol, timeframe, limit, since=None):
    """Deterministic candles with trend legs and pullbacks (so the rules find setups on every timeframe)."""
    rng = np.random.default_rng(zlib.crc32(symbol.encode()) + 1)
    sec = SEC[timeframe]
    base = datetime(2026, 5, 1)
    n = int((datetime(2026, 10, 14) - base).total_seconds() // sec)
    vol = 0.0025 * np.sqrt(sec / 900)
    ret = np.zeros(n)
    i, sign = 0, 1
    while i < n:
        leg = int(rng.integers(30, 90))
        part = ret[i:i + leg]
        part[:] = sign * vol * rng.uniform(0.25, 0.6) + rng.normal(0, vol, len(part))
        i += leg
        sign = -sign if rng.random() < 0.6 else sign
    cl = 100 * np.exp(np.cumsum(ret))
    op = np.r_[cl[0], cl[:-1]]
    hi = np.maximum(op, cl) * (1 + abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(op, cl) * (1 - abs(rng.normal(0, vol / 2, n)))
    df = pd.DataFrame({"timestamp": [base + timedelta(seconds=sec * k) for k in range(n)], "open": op, "high": hi,
                       "low": lo, "close": cl, "volume": rng.uniform(1, 3, n)})
    if since is not None:
        return df[df.timestamp >= pd.Timestamp(since, unit="ms")].head(limit)
    return df.tail(limit)


hub.symbols_for = lambda market: (["EUR/USD", "USD/JPY", "XAU/USD"] if market == "forex" else ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"])
hub._hooks["get_candles"] = trending_candles
hub._store = streams.CandleStore(trending_candles, sleep=0)
hub._engines.clear()
with hub._Cursor() as cur:
    cur.execute("SELECT COALESCE(MAX(id), 0) AS m FROM signal_events")
    keep_id = cur.fetchone()["m"]
made, scan_errors = {}, []
for key in hub.enabled_streams():
    if key == "crypto_4h":
        continue                      # produced by signalboard.py (signal engine v3.1), tested elsewhere
    sec = markets.TF_SEC[streams.STREAMS[key]["tf"]]
    made[key] = 0
    for k in range(60):
        made[key] += len(hub.scan_stream(key, NOW - timedelta(seconds=sec * k)))
    if hub.engine(key).errors:
        scan_errors.append(key)
check(not scan_errors, "every default stream scans every symbol without errors")
check(len(made) == 5 and all(v >= 1 for v in made.values()),
      "crypto 15m + 1h and forex 15m + 1h + 4h (gold included) each produce signals: " + ", ".join(f"{k} {v}" for k, v in made.items()))
with hub._Cursor() as cur:
    cur.execute("SELECT * FROM signal_events WHERE id > %s", (keep_id,))
    new_rows = cur.fetchall()
check(new_rows and all(hub.signal_json(r)["tested"] is False for r in new_rows if (r["market"], r["timeframe"]) != ("crypto", "4h")),
      "signals of the untested streams carry tested = false")
with hub._Cursor() as cur:
    cur.execute("DELETE FROM signal_events WHERE id > %s", (keep_id,))
hub._engines.clear()
hub._hooks["get_candles"] = fake_candles
hub._store = streams.CandleStore(fake_candles, sleep=0)
os.environ["SIGNAL_STREAMS"] = "all"
check(len(hub.enabled_streams()) == 6, "SIGNAL_STREAMS=all turns on all six streams")
os.environ["SIGNAL_STREAMS"] = "crypto_1h,bogus"
check(hub.enabled_streams() == ["crypto_1h"], "SIGNAL_STREAMS list: unknown names ignored")
os.environ.pop("SIGNAL_STREAMS")
import streams as _st  # noqa: E402
check(not _st.passed_test({"signals": 400, "win_rate": 71.0, "avg_net_r": -0.01})
      and not _st.passed_test({"signals": 400, "win_rate": 69.9, "avg_net_r": 0.05})
      and not _st.passed_test({"signals": 20, "win_rate": 80.0, "avg_net_r": 0.2})
      and _st.passed_test({"signals": 400, "win_rate": 70.0, "avg_net_r": 0.01}),
      "the live bar: >= 70% wins, positive average after costs, enough signals")

# alert filters
sub = {"coins": "ALL", "markets": json.dumps(["forex"]), "timeframes": json.dumps(["1h", "4h"])}
check(sb.wants(sub, {"symbol": "EUR/USD", "market": "forex", "timeframe": "1h"})
      and not sb.wants(sub, {"symbol": "EUR/USD", "market": "forex", "timeframe": "15m"})
      and not sb.wants(sub, {"symbol": "BTC/USDT", "market": "crypto", "timeframe": "4h"}),
      "alerts: users get only the markets and timeframes they chose")
check(sb.wants({"coins": None}, {"symbol": "BTC/USDT"}), "alerts: old subscriptions (no filters) still get everything")
title = sb.event_title({"symbol": "EUR/USD", "side": "LONG", "timeframe": "15m"})
check("15m" in title and "EUR/USD" in title, "alert title names the timeframe")
txt = sb.event_text({"symbol": "EUR/USD", "side": "LONG", "entry": 1.08123, "stop_loss": 1.0795, "targets": [1.083, 1.085, 1.087],
                     "timeframe": "1h", "market": "forex", "grade": "A"})
check("1.08123" in txt and "grade" not in txt.lower(), "alert text: forex prices with 5 decimals, no grade jargon")
check("experimental" in txt, "alert text: a signal from an untested stream is marked experimental")
txt4 = sb.event_text({"symbol": "BTC/USDT", "side": "LONG", "entry": 100.0, "stop_loss": 99.0, "targets": [101.0, 102.0, 103.0],
                      "timeframe": "4h", "market": "crypto", "grade": "A"})
check("untested" not in txt4, "alert text: crypto 4h (tested) carries no warning")

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
from flask import Flask  # noqa: E402

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(HERE), "templates"))
app.secret_key = "t"
app.register_blueprint(hub.hub_bp)
app.register_blueprint(sb.signalboard_bp)
cl = app.test_client()
check(cl.get("/api/signals").status_code == 401, "API needs a login")
with cl.session_transaction() as ss:
    ss["user_id"] = 1
d = cl.get("/api/signals?market=crypto&timeframe=1h").get_json()
check(d["ok"] and d["total"] >= 1 and all(x["timeframe"] == "1h" and x["market"] == "crypto" for x in d["signals"]),
      "GET /api/signals filters by market and timeframe")
sig = [x for x in d["signals"] if x["symbol"] == "SOL/USDT"][0]
check(all(k in sig for k in ("entry", "stop_loss", "targets", "rr", "grade", "status", "hold_minutes", "signal_at",
                             "closed_at", "expires_at", "market", "timeframe", "side", "tested")), "signal JSON has every field")
check(sig["tested"] is False, "signal JSON: a stream without a passed test is marked tested = false")
d = cl.get("/api/signals?status=active&timeframe=15m").get_json()
check(all(x["status"] == "ACTIVE" for x in d["signals"]) and d["total"] >= 1, "status filter")
check(cl.get("/api/signals?symbol=FAKE/USDT").status_code == 400, "unknown symbol rejected")
d = cl.get("/api/signals?grade=A&timeframe=1h&market=crypto").get_json()
check(all(x["grade"] == "A" for x in d["signals"]), "grade filter")
d = cl.get("/api/signals/stats?market=crypto&timeframe=1h").get_json()
check(d["ok"] and "windows" in d and set(d["windows"]) == {"all", "30d", "7d"}, "GET /api/signals/stats")
os.environ["SIGNAL_STREAMS"] = "all"
d = cl.get("/api/signals/latest").get_json()
check(d["ok"] and "crypto_1h" in d["streams"], "GET /api/signals/latest per stream")
os.environ.pop("SIGNAL_STREAMS")
d = cl.get("/api/signals/streams").get_json()
check(d["ok"] and len(d["streams"]) == 6, "GET /api/signals/streams lists the six streams")
hub._hooks["is_admin"] = lambda: False
r = cl.post(f"/api/signals/{sig['id']}/cancel", json={"reason": "test"})
check(r.status_code == 403, "cancel: admin only")
hub._hooks["is_admin"] = lambda: True
with hub._Cursor() as cur:
    cur.execute("SELECT id FROM signal_events WHERE status='ACTIVE' LIMIT 1")
    aid = cur.fetchone()["id"]
check(cl.post(f"/api/signals/{aid}/cancel", json={}).status_code == 400, "cancel needs a reason")
r = cl.post(f"/api/signals/{aid}/cancel", json={"reason": "Exchange outage"})
check(r.status_code == 200 and hub.get_signal(aid)["status"] == "CANCELLED", "admin can cancel an active signal with a reason")
r = cl.post("/api/signals/alerts", json={"enabled": True, "email": False, "coins": "ALL", "markets": ["forex"],
                                          "timeframes": ["15m", "1h"]})
d = r.get_json()
check(d["ok"] and d["markets"] == ["forex"] and d["timeframes"] == ["15m", "1h"], "alerts save markets and timeframes")
d = cl.get("/api/signals/alerts").get_json()
check(d["markets"] == ["forex"] and "EUR/USD" in d["choices"], "alerts read back; forex pairs are choices")
r = cl.post("/api/signals/alerts", json={"enabled": True, "markets": ["stocks"]})
check(r.status_code == 400, "alerts: invalid market rejected")

# ---------------------------------------------------------------------------
# Coins page: every coin, every moment (tested crypto streams only)
# ---------------------------------------------------------------------------
os.environ["SIGNAL_STREAMS"] = "all"
hub.symbols_for = lambda market: (["EUR/USD"] if market == "forex" else ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"])
with hub._Cursor() as cur:
    cur.execute("UPDATE signal_events SET status='EXPIRED' WHERE status='ACTIVE'")
_cb = datetime(2026, 10, 13, 8, 0)
hub.record("BTC/USDT", "crypto", "4h", {"side": "LONG", "entry": 63120.5, "stop_loss": 60000.0, "take_profit": 69361.5,
                                       "targets": [64680.75, 66241.0, 69361.5], "confidence": 78, "bar_time": _cb})
hub.record("SOL/USDT", "crypto", "15m", {"side": "SHORT", "entry": 150.0, "stop_loss": 152.0, "take_profit": 144.0,
                                        "targets": [148.0, 146.0, 144.0], "bar_time": _cb})


class _FakeEngine:
    _board = (_cb, {"ETH/USDT": {"setup_forming": True, "strength": 91.4, "bias": "SHORT"},
                    "XRP/USDT": {"setup_forming": False, "strength": 40.0, "bias": "LONG"}})


hub._engines["crypto_4h"] = _FakeEngine()
cs = hub.coins_status(datetime(2026, 10, 13, 9, 30))
st = {x["symbol"]: x for x in cs["coins"]}
check(st["BTC/USDT"]["status"] == "buy" and st["BTC/USDT"]["signal"]["entry"] == 63120.5 and st["BTC/USDT"]["timeframe"] == "4h",
      "coins: an active tested signal shows as buy with its plan")
check(st["SOL/USDT"]["status"] == "wait", "coins: signals of untested streams are not shown to beginners")
check(st["ETH/USDT"]["status"] == "forming" and st["ETH/USDT"]["lean"] == "down" and st["ETH/USDT"]["strength"] == 91,
      "coins: a setup the model is close to shows as getting close")
check(st["XRP/USDT"]["status"] == "wait", "coins: nothing to do = wait")
check([x["status"] for x in cs["coins"]] == ["buy", "forming", "wait", "wait"] and cs["counts"]["wait"] == 2,
      "coins: signals first, then getting close, then wait")
hub._engines.pop("crypto_4h", None)
check(client_guest := app.test_client().get("/api/coins/status").status_code == 401, "coins API needs a login")
d = cl.get("/api/coins/status").get_json()
check(d["ok"] and len(d["coins"]) == 4 and d["next_check"], "GET /api/coins/status")

# landing page: only the tested streams are counted and described
ps = hub.public_summary(now=datetime(2026, 10, 13, 9, 30))
check(ps["ok"] and ps["active"] == 1 and [(r["coin"], r["side"], r["timeframe"]) for r in ps["recent"]] == [("BTC", "LONG", "4h")]
      and all("entry" not in r for r in ps["recent"]), "landing: active signals of tested streams only, no levels")
check(ps["coins"] == 4 and "4h" in ps["timeframes"] and "15m" not in ps["timeframes"], "landing: coins and timeframes of the tested streams")
ts = hub.tested_summary()
check([x["timeframe"] for x in ts["streams"]] == ["4h"] and ts["signals"] == ts["streams"][0]["signals"] > 0
      and ts["win_rate"] == ts["streams"][0]["win_rate"], "landing: test numbers of the switched-on tested models")
check(hub._period_label("2025-10-01 to 2026-10-11") == "Oct 2025 to Oct 2026", "landing: readable test period")
os.environ["SIGNAL_STREAMS"] = "forex_1h"
check(hub.public_summary() == {"ok": False} and hub.tested_summary()["signals"] == 0, "landing: no tested stream switched on -> no numbers")
os.environ.pop("SIGNAL_STREAMS", None)

# ---------------------------------------------------------------------------
# more coins for the 4h stream (research/coin_expansion.py) and the 1h per-candle limit (research/cap_test.py)
# ---------------------------------------------------------------------------
c4, c1 = markets.stream_coins("crypto", "4h"), markets.stream_coins("crypto", "1h")
check(set(markets.CRYPTO_4H_ONLY) <= set(c4) and not (set(markets.CRYPTO_4H_ONLY) & set(c1)) and c4 == markets.CRYPTO_COINS
      and markets.stream_coins("crypto", "15m") == c1 and markets.stream_coins("forex", "1h") == markets.forex_signal_pairs(),
      "coins per stream: 4h every coin, 1h and 15m without the 4h-only coins, forex the signal pairs")
check(len(markets.CRYPTO_COINS) == len(set(markets.CRYPTO_COINS)) == 62 + len(markets.CRYPTO_4H_ONLY),
      "coin list: the 62 coins plus the added 4h coins, none twice")
_had = hub._hooks.get("get_candles")
hub._hooks["get_candles"] = _had or (lambda **kw: None)
hub._engines.pop("crypto_1h", None)
check(hub.engine("crypto_1h").symbols == c1, "the 1h engine scans only the coins its model passed on")
hub._engines.pop("crypto_1h", None)
if _had is None:
    hub._hooks.pop("get_candles", None)
shutil.copy(os.path.join(os.path.dirname(HERE), "models", "coin_test.json"), _MD)
hub._coin_test.update(t=0.0, data=None)
x4 = hub.coin_test("crypto_4h")
check(x4 and x4["coins"] == len(markets.CRYPTO_4H_ONLY) and x4["signals"] >= 50 and x4["win_rate"] >= 70 and x4["avg_r"] > 0,
      "coin test: the added 4h coins passed on the year the model never saw (shown with the 4h numbers)")
check(hub.coin_test("crypto_1h") is None, "coin test: the 1h stream got no new coins")
ship = json.load(open(os.path.join(os.path.dirname(HERE), "models", "crypto_1h.json")))
check(ship["selection"]["max_per_bar"] == 4 and ship["test"]["win_rate"] >= 70 and ship["test"]["avg_net_r"] > 0,
      "1h: at most 4 new signals per candle (8 passed the test but made the results swing more, see research/cap_test.py)")

# coins page: 4h "getting close" from the signal board, coins without data left out, joinable signals first
os.environ["SIGNAL_STREAMS"] = "all"
hub.symbols_for = lambda market: (["EUR/USD"] if market == "forex" else ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT"])
with hub._Cursor() as cur:
    cur.execute("UPDATE signal_events SET status='EXPIRED' WHERE status='ACTIVE'")
_b2 = datetime(2026, 10, 14, 8, 0)
hub.record("SOL/USDT", "crypto", "4h", {"side": "LONG", "entry": 150.0, "stop_loss": 144.0, "take_profit": 162.0,
                                       "targets": [153.0, 156.0, 162.0], "confidence": 75, "bar_time": _b2 - timedelta(hours=8)})
hub.record("BTC/USDT", "crypto", "4h", {"side": "SHORT", "entry": 63000.0, "stop_loss": 64000.0, "take_profit": 61000.0,
                                       "targets": [62500.0, 62000.0, 61000.0], "confidence": 74, "bar_time": _b2})
with hub._Cursor() as cur:
    cur.execute("UPDATE signal_events SET hits=1, stop_now=entry WHERE symbol='BTC/USDT' AND status='ACTIVE'")
hub._engines.pop("crypto_4h", None)
with sb._board_lock:
    _rows_before = sb._board.get("rows")
    sb._board["rows"] = {"ETH/USDT": {"setup_forming": True, "strength": 93.0, "bias": "LONG", "covered": True},
                         "XRP/USDT": {"error": "No 4h candles for XRP/USDT."},
                         "ADA/USDT": {"setup_forming": False, "strength": 50.0, "bias": "LONG", "covered": True}}
cs = hub.coins_status(datetime(2026, 10, 14, 13, 0))
order = [(x["symbol"], x["status"]) for x in cs["coins"]]
check(order[0] == ("SOL/USDT", "buy") and order[1] == ("BTC/USDT", "sell"),
      "coins: a signal past target 1 comes after the ones a beginner can still join")
check(("ETH/USDT", "forming") in order and next(x for x in cs["coins"] if x["symbol"] == "ETH/USDT")["timeframe"] == "4h",
      "coins: the 4h engine's 'getting close' coins come from the signal board")
check("XRP/USDT" not in [s for s, _ in order] and ("ADA/USDT", "wait") in order,
      "coins: a coin no stream could get candles for is left out, the others wait")
check(all(x["signal"]["strong"] is False for x in cs["coins"] if x["status"] in ("buy", "sell")) and cs["counts"]["strong"] == 0,
      "coins: 4h signals without the strong tier are not marked strong")
check(hub.is_strong({"market": "crypto", "timeframe": "4h", "tier": "strong"}) and not hub.is_strong({"market": "crypto", "timeframe": "4h", "tier": "standard"})
      and hub.is_strong({"market": "crypto", "timeframe": "1h", "grade": "A"}) and not hub.is_strong({"market": "crypto", "timeframe": "1h", "grade": "B"})
      and not hub.is_strong({"market": "forex", "timeframe": "1h", "grade": "A"}) and not hub.is_strong({"market": "crypto", "timeframe": "15m", "grade": "A"}),
      "strongest signals: 4h strong tier, 1h top grade, nothing else")
_md_before = streams.MODELS_DIR
streams.MODELS_DIR = os.path.join(os.path.dirname(HERE), "models")
streams.reset_models()
ss = hub.strong_summary(["1h", "4h"])
check(ss and ss["strong"]["win_rate"] > ss["all"]["win_rate"] and ss["strong"]["avg_r"] > ss["all"]["avg_r"] and ss["strong"]["per_day"] < ss["all"]["per_day"],
      "strongest signals: better and rarer than all signals in the shipped tests")
streams.MODELS_DIR = _md_before
streams.reset_models()
with sb._board_lock:
    sb._board["rows"] = _rows_before or {}
os.environ.pop("SIGNAL_STREAMS", None)

print()
print(f"{len(FAILS)} failed" if FAILS else "ALL PASSED")
sys.exit(1 if FAILS else 0)
