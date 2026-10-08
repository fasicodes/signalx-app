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

se = tr.se
print("\n[1] levels + outcome rules")
sl, tp = se.levels("LONG", 100.0, 2.0)
check(close_to(sl, 94.0) and close_to(tp, 103.0), "LONG: stop 3x ATR, target 1.5x ATR")
sl, tp = se.levels("SHORT", 100.0, 2.0)
check(close_to(sl, 106.0) and close_to(tp, 97.0), "SHORT levels mirrored")
check(close_to(tr.net_r(1.5, 100, 98), 1.5 - 0.0012 * 100 / 2), "fees taken off in R (0.12% round trip)")
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
flat = [bar(T0 + timedelta(hours=4 * i), 100, 100.5, 99.5, 100.2) for i in range(60)]
o = tr.evaluate("LONG", 100, 98, 103, flat)
check(o["status"] == "EXPIRED" and o["bars_held"] == 48 and close_to(o["r"], 0.1), "expires after 48 candles at the close")
o = tr.evaluate("LONG", 100, 98, 103, flat[:10])
check(not o["resolved"] and o["bars_held"] == 10 and close_to(o["mfe_r"], 0.25), "still open: tracks best excursion")

rng2 = np.random.default_rng(3)
mism = 0
for _ in range(300):
    n_ = 60
    cc = 100 * np.cumprod(1 + rng2.normal(0, 0.01, n_))
    oo = np.r_[100, cc[:-1]] * (1 + rng2.normal(0, 0.004, n_))
    hh = np.maximum(oo, cc) * (1 + rng2.uniform(0, 0.01, n_))
    ll = np.minimum(oo, cc) * (1 - rng2.uniform(0, 0.01, n_))
    side = "LONG" if rng2.random() < 0.5 else "SHORT"
    e = cc[0]
    sl_, tp_ = se.levels(side, e, e * 0.008)
    bs = [bar(T0 + timedelta(hours=4 * i), oo[i], hh[i], ll[i], cc[i]) for i in range(1, n_)]
    ref = tr.evaluate(side, e, sl_, tp_, bs)
    st_, px, held = se.follow(side, e, sl_, tp_, oo, hh, ll, cc, 1)
    if ref["status"] != st_ or not close_to(ref["exit_price"], px) or ref["bars_held"] != held:
        mism += 1
check(mism == 0, "engine's trade follower == track record rules (300 random trades)")

print("\n[2] stats")
rows = [
    {"id": 1, "symbol": "BTC/USDT", "timeframe": "4h", "side": "LONG", "confidence": 72, "r_multiple": 1.5, "status": "TP",
     "exit_at": datetime(2026, 1, 1, 5), "signal_at": datetime(2026, 1, 1), "bars_held": 5},
    {"id": 2, "symbol": "ETH/USDT", "timeframe": "4h", "side": "SHORT", "confidence": 58, "r_multiple": -1.0, "status": "SL",
     "exit_at": datetime(2026, 1, 2), "signal_at": datetime(2026, 1, 1, 6), "bars_held": 3},
    {"id": 3, "symbol": "BTC/USDT", "timeframe": "4h", "side": "LONG", "confidence": 85, "r_multiple": -1.0, "status": "SL",
     "exit_at": datetime(2026, 1, 3), "signal_at": datetime(2026, 1, 2, 6), "bars_held": 2},
]
s = tr.compute_stats(rows)
check(s["signals"] == 3 and close_to(s["win_rate"], 100 / 3) and close_to(s["total_r"], -0.5), "win rate + total R")
check(close_to(s["profit_factor"], 0.75) and close_to(s["breakeven_win_rate"], 40.0), "profit factor + break-even win rate")
check(close_to(s["equity_1pct"], 10000 * 1.015 * 0.99 * 0.99), "1% risk equity compounds per trade")
check(close_to(s["max_drawdown_pct"], (1 - 0.99 * 0.99) * 100, 1e-6), "drawdown from peak")
b = {d["key"]: d for d in s["by_confidence"]}
check(b["<60%"]["count"] == 1 and b["70–75%"]["win_rate"] == 100 and b["80%+"]["count"] == 1, "confidence buckets")
check(s["by_timeframe"][0]["key"] == "4h" and s["first_signal_at"].startswith("2026-01-01T00:00"), "timeframe grouping + first signal")
check(tr.compute_stats([])["signals"] == 0, "empty stats are safe")

print("\n[3] backtest (signal engine v2)")
c = Conn(); cu = c.cursor()
tr.init_tables()
c.close()
N = 900
T4 = pd.date_range("2025-09-01", periods=N, freq="4h")


def synth(seed):
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.012, N))
    df = pd.DataFrame({"timestamp": T4, "open": np.r_[close[0], close[:-1]], "close": close,
                       "volume": rng.uniform(500, 1500, N)})
    df["high"] = np.maximum(df["open"], df["close"]) * (1 + rng.uniform(0, 0.006, N))
    df["low"] = np.minimum(df["open"], df["close"]) * (1 - rng.uniform(0, 0.006, N))
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


FRAMES = {sym: synth(k) for k, sym in enumerate(se.UNIVERSE)}
orig_decide = se.decide
calls_seen = {"n": 0}


def every_7th(pl, ps):
    calls_seen["n"] += 1
    if calls_seen["n"] % 7:
        return "WAIT", max(pl, ps)
    return ("LONG" if pl >= ps else "SHORT"), max(pl, ps)


se.decide = every_7th
start_time = T4[400].to_pydatetime()
counts = {}
res = tr.backtest_frames(FRAMES, ["BTC/USDT", "ETH/USDT"], start_time, counts)
trades = res["BTC/USDT"]
check(len(trades) > 10, f"backtest produced {len(trades)} BTC signals")
check(all(t[1] >= start_time for t in trades), "only signals inside the backtest window")
bars_all = tr._df_to_bars(FRAMES["BTC/USDT"])
idx = {b["time"]: i for i, b in enumerate(bars_all)}
F = se.coin_features(FRAMES["BTC/USDT"])
sig, bt, out = trades[0]
i0 = idx[bt]
exp = tr.evaluate(sig["verdict"], sig["entry"], sig["stop_loss"], sig["take_profit"], bars_all[i0 + 1:])
check(close_to(sig["entry"], FRAMES["BTC/USDT"]["close"].iloc[i0]), "entry = close of the signal candle")
check(close_to(abs(sig["entry"] - sig["stop_loss"]), 3 * F["atr"].iloc[i0]) and
      close_to(abs(sig["take_profit"] - sig["entry"]), 1.5 * F["atr"].iloc[i0]), "levels = 3 / 1.5 x ATR at that candle")
check(exp["status"] == out["status"] and close_to(exp["r"], out["r"]), "outcome uses only candles after the signal")
ok_seq = all(idx[trades[k + 1][1]] >= idx[trades[k][1]] + trades[k][2]["bars_held"] for k in range(len(trades) - 1))
check(ok_seq, "one signal at a time per coin")
check(counts["BACKTEST:4h"]["WAIT"] > 0 and sum(counts["BACKTEST:4h"].values()) > len(trades), "verdict counts kept (incl. WAIT)")
# the dashboard's replay (active / last trade) agrees with the backtest from the same start candle
FB = se.coin_features(FRAMES["BTC/USDT"])
featsU = {s_: se.coin_features(d_) for s_, d_ in FRAMES.items()}
XB = se.assemble(FB, se.btc_block(featsU["BTC/USDT"]), se.market_block(featsU), "BTC/USDT")
calls_seen["n"] = 0
res_bt = tr.backtest_frames(FRAMES, ["BTC/USDT"], T4[N - 300].to_pydatetime())["BTC/USDT"]
calls_seen["n"] = 0
pl_, ps_, cl_, cs_ = se.score(XB)
act_, last_ = se.replay(FB, pl_, ps_, cl_, cs_, lookback=300)
check(last_ is not None and res_bt and last_["bar_time"] == res_bt[-1][1] and last_["status"] == res_bt[-1][2]["status"],
      "replay's last closed trade == backtest's last trade")

orig_fetch, orig_coins = tr.fetch_history, tr._hooks.get("available_coins")
tr._hooks["available_coins"] = ["BTC/USDT", "ETH/USDT"]
fetched = []


def fake_fetch(symbol, timeframe, start):
    fetched.append(symbol)
    return FRAMES.get(symbol)


tr.fetch_history = fake_fetch
now_bt = (T4[-1] + pd.Timedelta(hours=4, minutes=5)).to_pydatetime()
calls_seen["n"] = 0
tr.run_backtests(now_bt)
st = tr.meta_get("bt_status")
q = Conn().cursor(); q.execute("SELECT COUNT(*) AS n FROM track_signals WHERE source='BACKTEST'"); n_bt = q.fetchone()["n"]
check(st["state"] == "done" and st["version"] == se.ENGINE_VERSION and n_bt == st["recorded"] > 0,
      f"backtest stored {n_bt} signals for the tracked coins")
check(set(fetched) >= set(se.UNIVERSE), "fetches the whole 16-coin universe (market context)")
q.execute("SELECT * FROM track_signals WHERE source='BACKTEST' ORDER BY id LIMIT 1"); row = q.fetchone()
check(row["status"] in ("TP", "SL", "EXPIRED") and row["r_multiple"] < (0.5 if row["status"] == "TP" else 99),
      "stored R is after fees")
fetched.clear()
tr.run_backtests(now_bt + timedelta(days=2))
check(not fetched, "not re-run within 7 days")
calls_seen["n"] = 0
tr.run_backtests(now_bt + timedelta(days=8))
q.execute("SELECT COUNT(*) AS n FROM track_signals WHERE source='BACKTEST'")
check(fetched and q.fetchone()["n"] == n_bt, "weekly refresh replaces (no duplicates)")
tr.fetch_history = orig_fetch
se.decide = orig_decide

tr.meta_set("engine_version", "1.0")
check(tr.ensure_engine_version() is True, "older engine version detected")
q.execute("SELECT COUNT(*) AS n FROM track_signals")
check(q.fetchone()["n"] == 0 and tr.meta_get("engine_version") == se.ENGINE_VERSION and tr.meta_get("bt_status") is None,
      "new engine version starts a fresh record")
check(tr.ensure_engine_version() is False, "same version: nothing reset")

print("\n[4] live recorder")
LIVE = {}


def fake_get_candles(symbol, timeframe, limit=200, since=None):
    df = LIVE[(symbol, timeframe)]
    if since is not None:
        df = df[df["timestamp"] >= pd.to_datetime(since, unit="ms")]
        return df.head(limit).reset_index(drop=True)
    return df.tail(limit).reset_index(drop=True)


class FakeEngine:
    def signal(self, symbol, now=None):
        df = closed = LIVE[(symbol, "4h")]
        closed = df[df["timestamp"] + pd.Timedelta(hours=4) <= pd.Timestamp(now)]
        last = closed.iloc[-1]
        e = float(last["close"])
        bt = last["timestamp"].to_pydatetime()
        prev = closed.iloc[-2]
        if symbol == "ETH/USDT" and STALE["on"]:   # an older trade still running -> not recorded again
            return {"verdict": "LONG", "fresh": False, "confidence": 74.0, "entry": float(prev["close"]),
                    "stop_loss": 1.0, "take_profit": 2.0, "active": {"bar_time": prev["timestamp"].to_pydatetime()}}
        return {"verdict": "LONG", "fresh": True, "confidence": 74.0, "p_long": 74.0, "entry": e, "stop_loss": e * 0.97,
                "take_profit": e * 1.015, "bar_time": bt, "active": {"bar_time": bt}}


STALE = {"on": False}


tr.run_backtests = lambda *a, **k: None
tr._hooks["get_candles"] = fake_get_candles
tr._hooks["engine"] = FakeEngine()
tr._hooks["available_coins"] = ["BTC/USDT", "ETH/USDT"]
check(tr.tracked_coins() == ["BTC/USDT", "ETH/USDT"], "tracks a fixed coin list (only available coins)")
base = FRAMES["BTC/USDT"].iloc[:400].copy()
for sym in ("BTC/USDT", "ETH/USDT"):
    LIVE[(sym, "4h")] = base.copy()
t_now = (base["timestamp"].iloc[-1] + pd.Timedelta(hours=2)).to_pydatetime()  # last candle still forming
tr.meta_set("bt_status", {"state": "done", "version": se.ENGINE_VERSION, "finished_at": tr._iso(t_now)})
check(not tr._backtest_due(tr.meta_get("bt_status"), t_now + timedelta(days=6)) and
      tr._backtest_due(tr.meta_get("bt_status"), t_now + timedelta(days=7)), "backtest refresh due weekly")
check(not tr._backtest_due({"state": "error", "version": se.ENGINE_VERSION, "finished_at": tr._iso(t_now)}, t_now + timedelta(minutes=30)),
      "failed backtest retried hourly, not every loop")
acts = tr.engine_step(t_now)
check(acts == ["evaluate"] and tr.meta_get("live_slot:4h") is not None, "first run only remembers the current candle")
t_next = (base["timestamp"].iloc[-1] + pd.Timedelta(hours=4, minutes=1)).to_pydatetime()
acts = tr.engine_step(t_next)
q = Conn().cursor(); q.execute("SELECT * FROM track_signals WHERE source='LIVE'"); live = q.fetchall()
check("scan:4h" in acts and len(live) == 2, "candle close -> one signal per coin")
STALE["on"] = True
r = [x for x in live if x["symbol"] == "BTC/USDT"][0]
check(pd.Timestamp(r["bar_time"]) == base["timestamp"].iloc[-1] and r["status"] == "OPEN", "uses the just-closed candle")
check(close_to(r["entry_price"], base["close"].iloc[-1]) and close_to(r["stop_loss"], r["entry_price"] * 0.97), "entry + stop recorded")
tr.engine_step(t_next + timedelta(hours=4))
q = Conn().cursor(); q.execute("SELECT COUNT(*) AS n FROM track_signals WHERE source='LIVE'")
check(q.fetchone()["n"] == 2, "no new signal while one is open on that coin")
q.execute("DELETE FROM track_signals WHERE source='LIVE' AND symbol='ETH/USDT'")
tr.engine_step(t_next + timedelta(hours=8))
q.execute("SELECT COUNT(*) AS n FROM track_signals WHERE source='LIVE' AND symbol='ETH/USDT'")
check(q.fetchone()["n"] == 0, "a trade that started on an older candle is not recorded late")
STALE["on"] = False
entry = r["entry_price"]
ext = base.copy()
t_last = ext["timestamp"].iloc[-1]
ext = pd.concat([ext, pd.DataFrame({"timestamp": [t_last + pd.Timedelta(hours=4), t_last + pd.Timedelta(hours=8)],
                                     "open": [entry, entry], "high": [entry * 1.003, entry * 1.02],
                                     "low": [entry * 0.998, entry * 0.999], "close": [entry * 1.001, entry * 1.016],
                                     "volume": [1.0, 1.0]})], ignore_index=True)
LIVE[("BTC/USDT", "4h")] = ext
tr.meta_set("last_eval_epoch", 0)
tr.engine_step((t_last + pd.Timedelta(hours=12, minutes=1)).to_pydatetime())
q = Conn().cursor(); q.execute("SELECT * FROM track_signals WHERE id=?", (r["id"],)); r2 = q.fetchone()
check(r2["status"] == "TP" and close_to(r2["r_multiple"], 0.5 - 0.0012 / 0.03) and r2["bars_held"] == 2,
      "live signal resolved at target, R after fees")
n = 0
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
check(isinstance(d["sources"]["LIVE"]["open"], list) and d["rules"]["max_bars"] == 48 and d["rules"]["model_test"]["signals"] > 0,
      "open signals, rules and model test stats exposed")
page = cl.get("/track-record")
check(page.status_code == 200 and b"Track record" in page.data, "public page renders")
os.environ.pop("TRACK_RECORD_PUBLIC", None)

print(f"\nALL {passed} CHECKS PASSED")
