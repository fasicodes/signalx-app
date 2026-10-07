"""Tests for trackrecord.py with synthetic candles + SQLite shim (no network,
no MySQL). Run from the project root:  python tests/test_trackrecord.py
"""
import os
import re
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pm = types.ModuleType("pymysql")
pm.cursors = types.ModuleType("pymysql.cursors")
pm.cursors.DictCursor = object
pm.connect = lambda **kw: None
sys.modules.setdefault("pymysql", pm)
sys.modules.setdefault("pymysql.cursors", pm.cursors)

sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))
DB_PATH = os.path.join(tempfile.gettempdir(), "trackrecord_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)


def _translate(sql):
    s = sql.strip()
    if "GET_LOCK" in s:
        return "SELECT 1 AS got"
    if s.upper().startswith("CREATE TABLE"):
        lines = [ln for ln in s.splitlines() if not ln.strip().startswith(("INDEX ", "FOREIGN KEY"))]
        s = "\n".join(lines).replace("INT PRIMARY KEY AUTO_INCREMENT", "INTEGER PRIMARY KEY AUTOINCREMENT")
        s = re.sub(r",\s*\)\s*$", "\n)", s)
    return s.replace("%s", "?")


class Cur:
    def __init__(self, conn):
        self.c = conn.cursor()
        self.rowcount = -1

    def execute(self, sql, params=()):
        t = _translate(sql)
        self.c.execute(t, tuple(params or ()) if "?" in t else ())
        self.rowcount = self.c.rowcount

    def fetchone(self):
        r = self.c.fetchone()
        return dict(r) if r else None

    def fetchall(self):
        return [dict(r) for r in self.c.fetchall()]

    def close(self):
        self.c.close()


class Conn:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, isolation_level=None)
        self.conn.row_factory = sqlite3.Row

    def cursor(self):
        return Cur(self.conn)

    def close(self):
        self.conn.close()

    def ping(self, reconnect=False):
        pass


import trackrecord as tr  # noqa: E402

tr.get_db_connection = Conn
tr.time.sleep = lambda s: None
passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


def close_to(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1, abs(b))


def bar(t, o, h, l, c):
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


T0 = datetime(2026, 1, 1)

print("\n[1] levels + outcome rules")
sl, tp = tr.plan_trade("LONG", 100.0, 0.02)
check(close_to(sl, 98.0) and close_to(tp, 103.0), "LONG: SL = 95% move, TP = 1.5x")
sl, tp = tr.plan_trade("SHORT", 100.0, 0.02)
check(close_to(sl, 102.0) and close_to(tp, 97.0), "SHORT levels mirrored")
o = tr.evaluate("LONG", 100, 98, 103, [bar(T0, 100, 101, 99, 100.5), bar(T0, 100.5, 103.2, 100, 103)])
check(o["resolved"] and o["status"] == "TP" and close_to(o["r"], 1.5) and o["bars_held"] == 2, "TP = +1.5R")
o = tr.evaluate("LONG", 100, 98, 103, [bar(T0, 100, 101, 97.5, 98.5)])
check(o["status"] == "SL" and close_to(o["r"], -1.0), "SL = -1R")
o = tr.evaluate("LONG", 100, 98, 103, [bar(T0, 100, 104, 97, 101)])
check(o["status"] == "SL" and close_to(o["r"], -1.0), "SL and TP in one candle counts as a loss")
o = tr.evaluate("LONG", 100, 98, 103, [bar(T0, 96, 97, 95, 96.5)])
check(o["status"] == "SL" and close_to(o["exit_price"], 96) and close_to(o["r"], -2.0), "gap below the stop exits at the worse open")
o = tr.evaluate("SHORT", 100, 102, 97, [bar(T0, 100, 101, 96.8, 97.5)])
check(o["status"] == "TP" and close_to(o["r"], 1.5), "SHORT take-profit")
flat = [bar(T0 + timedelta(hours=i), 100, 100.5, 99.5, 100.2) for i in range(60)]
o = tr.evaluate("LONG", 100, 98, 103, flat)
check(o["status"] == "EXPIRED" and o["bars_held"] == 48 and close_to(o["r"], 0.1), "expires after 48 candles at the close")
o = tr.evaluate("LONG", 100, 98, 103, flat[:10])
check(not o["resolved"] and o["bars_held"] == 10 and close_to(o["mfe_r"], 0.25), "still open: tracks best excursion")

print("\n[2] stats")
rows = [
    {"id": 1, "symbol": "BTC/USDT", "timeframe": "1h", "side": "LONG", "confidence": 72, "r_multiple": 1.5, "status": "TP",
     "exit_at": datetime(2026, 1, 1, 5), "signal_at": datetime(2026, 1, 1), "bars_held": 5},
    {"id": 2, "symbol": "ETH/USDT", "timeframe": "4h", "side": "SHORT", "confidence": 58, "r_multiple": -1.0, "status": "SL",
     "exit_at": datetime(2026, 1, 2), "signal_at": datetime(2026, 1, 1, 6), "bars_held": 3},
    {"id": 3, "symbol": "BTC/USDT", "timeframe": "1h", "side": "LONG", "confidence": 85, "r_multiple": -1.0, "status": "SL",
     "exit_at": datetime(2026, 1, 3), "signal_at": datetime(2026, 1, 2, 6), "bars_held": 2},
]
s = tr.compute_stats(rows)
check(s["signals"] == 3 and close_to(s["win_rate"], 100 / 3) and close_to(s["total_r"], -0.5), "win rate + total R")
check(close_to(s["profit_factor"], 0.75) and close_to(s["breakeven_win_rate"], 40.0), "profit factor + break-even win rate")
check(close_to(s["equity_1pct"], 10000 * 1.015 * 0.99 * 0.99), "1% risk equity compounds per trade")
check(close_to(s["max_drawdown_pct"], (1 - 0.99 * 0.99) * 100, 1e-6), "drawdown from peak")
b = {d["key"]: d for d in s["by_confidence"]}
check(b["55–60%"]["count"] == 1 and b["70–80%"]["win_rate"] == 100 and b["80%+"]["count"] == 1, "confidence buckets")
check(s["by_timeframe"][0]["key"] == "1h" and s["first_signal_at"].startswith("2026-01-01T00:00"), "timeframe grouping + first signal")
check(tr.compute_stats([])["signals"] == 0, "empty stats are safe")

print("\n[3] walk-forward backtest")
c = Conn(); cu = c.cursor()
tr.init_tables()
c.close()
rng = np.random.default_rng(7)
N = 800
close = 100 * np.cumprod(1 + rng.normal(0.0002, 0.006, N))
hours = pd.date_range("2026-01-01", periods=N, freq="h")
DF = pd.DataFrame({"timestamp": hours, "open": np.r_[close[0], close[:-1]], "high": close * 1.004,
                   "low": close * 0.996, "close": close, "volume": 1.0})
DF["high"] = DF[["open", "close", "high"]].max(axis=1)
DF["low"] = DF[["open", "close", "low"]].min(axis=1)
seen_windows = []


def fake_core(df):
    seen_windows.append((len(df), df["timestamp"].iloc[-1]))
    last = float(df["close"].iloc[-1])
    v = "LONG" if df["close"].iloc[-1] > df["close"].iloc[-20] else "SHORT"
    if len(seen_windows) % 5 == 0:
        v = "WAIT"
    return {"verdict": v, "confidence": 65.0, "bullish_pct": 60.0, "price": last, "extreme_move": 0.008}


tr._hooks["signal_core"] = fake_core
now = (hours[-1] + pd.Timedelta(hours=1, minutes=5)).to_pydatetime()
n, counts = tr.backtest_pair("BTC/USDT", "1h", now=now, df=DF.copy())
check(n > 20, f"backtest recorded {n} signals")
check(all(w[0] == 200 for w in seen_windows), "every decision saw exactly 200 candles")
recs = Conn().cursor()
recs.execute("SELECT * FROM track_signals WHERE source='BACKTEST' ORDER BY bar_time")
rows = recs.fetchall()
check(all(r["status"] in ("TP", "SL", "EXPIRED") for r in rows), "backtest only stores finished signals")
overlap = any(pd.Timestamp(rows[k + 1]["bar_time"]) < pd.Timestamp(rows[k]["exit_at"]) - pd.Timedelta(hours=1) for k in range(len(rows) - 1))
check(not overlap, "no overlapping signals on the same coin/timeframe")
by_time = {pd.Timestamp(t): i for i, t in enumerate(DF["timestamp"])}
r0 = rows[0]
i0 = by_time[pd.Timestamp(r0["bar_time"])]
exp = tr.evaluate(r0["side"], r0["entry_price"], r0["stop_loss"], r0["take_profit"], tr._df_to_bars(DF.iloc[i0 + 1:]))
check(close_to(r0["r_multiple"], exp["r"]) and r0["status"] == exp["status"], "outcome uses only candles after the signal")
check(close_to(r0["entry_price"], DF["close"].iloc[i0]), "entry = close of the signal candle")
check(sum(counts["BACKTEST:1h"].values()) == len(seen_windows) and counts["BACKTEST:1h"]["WAIT"] > 0, "verdict counts kept (incl. WAIT)")
seen_windows.clear()  # same inputs -> same decisions
n2, _ = tr.backtest_pair("BTC/USDT", "1h", now=now, df=DF.copy())
check(n2 == 0, "re-running the backtest does not duplicate records")

orig_bp, orig_coins = tr.backtest_pair, tr._hooks.get("available_coins")
tr._hooks["available_coins"] = ["BTC/USDT", "ETH/USDT"]
calls = []


def flaky(symbol, timeframe):
    calls.append((symbol, timeframe))
    if symbol == "ETH/USDT" and timeframe == "4h":
        raise RuntimeError("network down")
    return 0, {}


tr.backtest_pair = flaky
tr.meta_set("bt_status", {})
tr.run_backtests()
st = tr.meta_get("bt_status")
check(st["state"] == "partial" and ["ETH/USDT", "4h"] not in st["done"] and "ETH/USDT 4h" in st["errors"], "failed pair is not marked done")
calls.clear()
tr.run_backtests()
check(calls == [("ETH/USDT", "4h")], "only the failed pair is retried on the next start")
tr.backtest_pair, tr._hooks["available_coins"] = orig_bp, orig_coins

print("\n[4] live recorder")
LIVE = {}


def fake_get_candles(symbol, timeframe, limit=200, since=None):
    df = LIVE[(symbol, timeframe)]
    if since is not None:
        df = df[df["timestamp"] >= pd.to_datetime(since, unit="ms")]
        return df.head(limit).reset_index(drop=True)
    return df.tail(limit).reset_index(drop=True)


tr._hooks["get_candles"] = fake_get_candles
tr._hooks["available_coins"] = ["BTC/USDT", "ETH/USDT"]
check(tr.tracked_coins() == ["BTC/USDT", "ETH/USDT"], "tracks a fixed coin list (only available coins)")
base = DF.iloc[:400].copy()
for sym in ("BTC/USDT", "ETH/USDT"):
    for tf in ("1h", "4h"):
        LIVE[(sym, tf)] = base.copy()
tr._hooks["signal_core"] = lambda df: {"verdict": "LONG", "confidence": 70.0, "bullish_pct": 62.0,
                                       "price": float(df["close"].iloc[-1]), "extreme_move": 0.01}
t_now = (base["timestamp"].iloc[-1] + pd.Timedelta(minutes=30)).to_pydatetime()  # last candle still forming
acts = tr.engine_step(t_now)
check(acts == ["evaluate"] and tr.meta_get("live_slot:1h") is not None, "first run only remembers the current candle")
t_next = (base["timestamp"].iloc[-1] + pd.Timedelta(hours=1, minutes=1)).to_pydatetime()
acts = tr.engine_step(t_next)
q = Conn().cursor(); q.execute("SELECT * FROM track_signals WHERE source='LIVE'"); live = q.fetchall()
check("scan:1h" in acts and len([r for r in live if r["timeframe"] == "1h"]) == 2, "candle close -> one signal per coin")
r = [x for x in live if x["symbol"] == "BTC/USDT" and x["timeframe"] == "1h"][0]
check(pd.Timestamp(r["bar_time"]) == base["timestamp"].iloc[-1] and r["status"] == "OPEN", "uses the just-closed candle")
check(close_to(r["entry_price"], base["close"].iloc[-1]) and close_to(r["stop_loss"], r["entry_price"] * 0.99), "entry + stop recorded")
t_next2 = t_next + timedelta(hours=1)
tr.engine_step(t_next2)
q = Conn().cursor(); q.execute("SELECT COUNT(*) AS n FROM track_signals WHERE source='LIVE' AND timeframe='1h'")
check(q.fetchone()["n"] == 2, "no new signal while one is open on that coin")
entry = r["entry_price"]
ext = base.copy()
t_last = ext["timestamp"].iloc[-1]
ext = pd.concat([ext, pd.DataFrame({"timestamp": [t_last + pd.Timedelta(hours=1), t_last + pd.Timedelta(hours=2)],
                                     "open": [entry, entry], "high": [entry * 1.003, entry * 1.02],
                                     "low": [entry * 0.998, entry * 0.999], "close": [entry * 1.001, entry * 1.016],
                                     "volume": [1.0, 1.0]})], ignore_index=True)
LIVE[("BTC/USDT", "1h")] = ext
tr.meta_set("last_eval_epoch", 0)
tr.engine_step((t_last + pd.Timedelta(hours=3, minutes=1)).to_pydatetime())
q = Conn().cursor(); q.execute("SELECT * FROM track_signals WHERE id=?", (r["id"],)); r2 = q.fetchone()
check(r2["status"] == "TP" and close_to(r2["r_multiple"], 1.5) and r2["bars_held"] == 2, "live signal resolved at take-profit")

print("\n[5] page + API")
from flask import Flask  # noqa: E402

app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
app.secret_key = "t"
app.register_blueprint(tr.track_bp)


@app.route("/login")
def login_page():
    return "login"


cl = app.test_client()
os.environ.pop("TRACK_RECORD_PUBLIC", None)
check(cl.get("/api/track-record").status_code == 401 and cl.get("/track-record").status_code == 302,
      "private by default (login required)")
os.environ["TRACK_RECORD_PUBLIC"] = "on"
tr._cache.clear()
d = cl.get("/api/track-record").get_json()
check(d["ok"] and d["public"] and d["sources"]["BACKTEST"]["stats"]["signals"] == n and
      d["sources"]["LIVE"]["stats"]["signals"] == 1, "public API returns live + backtest stats")
check(len(d["sources"]["LIVE"]["open"]) >= 1 and d["rules"]["max_bars"] == 48, "open signals + rules exposed")
page = cl.get("/track-record")
check(page.status_code == 200 and b"Track record" in page.data, "public page renders")
os.environ.pop("TRACK_RECORD_PUBLIC", None)

print(f"\nALL {passed} CHECKS PASSED")
