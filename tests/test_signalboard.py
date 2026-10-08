"""Tests for signalboard.py with a fake engine + SQLite shim (no network,
no MySQL). Run from the project root:  python tests/test_signalboard.py
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
DB_PATH = os.path.join(tempfile.gettempdir(), "signalboard_test.db")
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


import signalboard as sb  # noqa: E402
sb.get_db_connection = Conn
sb.time.sleep = lambda s: None
passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


c = Conn().cursor()
c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
c.execute("""CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, category TEXT, symbol TEXT,
             title TEXT, message TEXT, is_read INT DEFAULT 0, alert_id INT, created_at TEXT)""")
for i, e in ((1, "a@x.com"), (2, "b@x.com"), (3, "c@x.com")):
    c.execute("INSERT INTO users (id, email) VALUES (?, ?)", (i, e))

NOW = datetime(2026, 10, 8, 8, 2)           # 4h candle 04:00-08:00 closed at 08:00
BAR = datetime(2026, 10, 8, 4, 0)
STATE = {"BTC/USDT": "new", "ETH/USDT": "old", "SOL/USDT": "wait", "HYPE/USDT": "error"}
CALLS = []


def fake_engine(sym):
    CALLS.append(sym)
    kind = STATE[sym]
    base = {"engine_version": "2.0", "p_long": 70.0, "p_short": 40.0, "bias": "LONG", "strength": 88.0,
            "last_close": 100.0, "history_bars": 600, "next_update": "2026-10-08T12:00:00Z", "last_closed": None}
    if kind == "error":
        return {"error": "Signal engine unavailable: no market"}
    if kind == "wait":
        return {**base, "verdict": "WAIT", "fresh": False, "active": None, "strength": 91.0}
    bt = "2026-10-08T04:00:00Z" if kind == "new" else "2026-10-07T12:00:00Z"
    act = {"side": "LONG", "entry": 100.0, "stop_loss": 94.0, "take_profit": 103.0, "atr": 2.0, "confidence": 76.4,
           "bar_time": bt, "signal_at": bt.replace("04:00", "08:00").replace("12:00", "16:00"), "bars_held": 0,
           "expires_at": "2026-10-16T08:00:00Z"}
    return {**base, "verdict": "LONG", "fresh": kind == "new", "active": act, "entry": 100.0, "stop_loss": 94.0,
            "take_profit": 103.0, "confidence": 76.4}


SENT = []
sb.init_signalboard(engine_signal=fake_engine, coins=list(STATE), prices=lambda syms: {s: 101.5 for s in syms},
                    send_email=lambda to, subj, html: SENT.append((to, subj, html)), start=False)
sb.threading.Thread = lambda target=None, args=(), **k: type("T", (), {"start": lambda self: target(*args)})()

print("\n[1] subscriptions")
from flask import Flask  # noqa: E402
app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"), static_folder=os.path.join(ROOT, "static"))
app.secret_key = "t"
app.register_blueprint(sb.signalboard_bp)
app.jinja_env.globals.update(track_record_public=False, current_year=2026, ga_measurement_id=None)


@app.route("/login")
def login_page():
    return "login"


cl = app.test_client()
check(cl.get("/api/signals/board").status_code == 401 and cl.get("/signals").status_code == 302, "login required")


def as_user(uid):
    t = app.test_client()
    with t.session_transaction() as s:
        s["user_id"] = uid
    return t


u1, u2, u3 = as_user(1), as_user(2), as_user(3)
d = u1.get("/api/signals/alerts").get_json()
check(d["ok"] and not d["enabled"] and d["coins"] == "ALL" and "BTC/USDT" in d["choices"], "default: alerts off, all coins")
r = u1.post("/api/signals/alerts", json={"enabled": True, "email": True, "coins": "ALL"}).get_json()
check(r["ok"] and r["enabled"] and r["email"], "user 1: all coins + email")
r = u2.post("/api/signals/alerts", json={"enabled": True, "email": False, "coins": ["ETH/USDT", "FAKE/USDT"]}).get_json()
check(r["ok"] and r["coins"] == ["ETH/USDT"], "user 2: only ETH, unknown coins dropped")
check(u3.post("/api/signals/alerts", json={"enabled": True, "coins": ["NOPE/USDT"]}).status_code == 400, "empty coin pick rejected")
check(u3.post("/api/signals/alerts", data="x").status_code == 400, "non-JSON rejected")
u3.post("/api/signals/alerts", json={"enabled": False, "email": True})
check(u3.get("/api/signals/alerts").get_json()["email"] is False, "email can't be on while alerts are off")

print("\n[2] refresh + events + notifications")
act = sb.board_step(NOW)
check(act == "refresh" and CALLS == list(STATE), "first pass reads every coin")
q = Conn().cursor()
q.execute("SELECT * FROM signal_events")
ev = q.fetchall()
check(len(ev) == 1 and ev[0]["symbol"] == "BTC/USDT", "only the NEW signal is stored as an event (not the older active one)")
q.execute("SELECT user_id, title FROM notifications ORDER BY user_id")
nt = q.fetchall()
check([n["user_id"] for n in nt] == [1] and "New LONG signal: BTC/USDT" in nt[0]["title"], "bell notification for subscribers of that coin only")
check(len(SENT) == 1 and SENT[0][0] == "a@x.com" and "BTC/USDT" in SENT[0][1] and "94" in SENT[0][2], "email to the user who asked for it")
CALLS.clear()
check(sb.board_step(NOW + timedelta(minutes=5)) is None and not CALLS, "nothing re-read inside the same candle")
sb._board["attempt_at"] = 0
check(sb.board_step(NOW + timedelta(minutes=15)) == "retry" and CALLS == ["HYPE/USDT"], "coins that failed are retried every 10 min")
sb._board["bar"] = None
sb.board_step(NOW + timedelta(minutes=20))
q.execute("SELECT COUNT(*) AS n FROM signal_events"); n1 = q.fetchone()["n"]
q.execute("SELECT COUNT(*) AS n FROM notifications"); n2 = q.fetchone()["n"]
check(n1 == 1 and n2 == 1 and len(SENT) == 1, "restart in the same candle: no duplicate event, bell or email")
STATE["ETH/USDT"] = "new"
CALLS.clear()
nxt = NOW + timedelta(hours=4)
check(sb.board_step(nxt - timedelta(seconds=90)) is None, "waits a minute after the close for the exchange")
check(sb.board_step(nxt) == "refresh", "next candle close -> refresh")
q.execute("SELECT user_id, symbol FROM notifications ORDER BY id")
nt = q.fetchall()
check(sorted((n["user_id"], n["symbol"]) for n in nt[1:]) == [(1, "ETH/USDT"), (2, "ETH/USDT")],
      "new ETH signal reaches user 1 (all coins) and user 2 (ETH only); BTC not repeated")
check(len(SENT) == 2 and "ETH/USDT" in SENT[1][1], "email for the new ETH signal")
sb.MAX_EMAILS_PER_DAY = 2
STATE["SOL/USDT"] = "new"
sb.board_step(nxt + timedelta(hours=4))
q.execute("SELECT COUNT(*) AS n FROM notifications WHERE symbol='SOL/USDT'")
check(q.fetchone()["n"] == 1 and len(SENT) == 2, "daily email cap respected (bell still works)")
box = []
sb._send_all(lambda to, subj, html: box.append(subj), [("z@x.com", [{"symbol": "A/USDT", "side": "LONG", "entry": 1, "stop_loss": 0.9,
             "take_profit": 1.05, "confidence": 70}, {"symbol": "B/USDT", "side": "SHORT", "entry": 2, "stop_loss": 2.1,
             "take_profit": 1.9, "confidence": 72}])])
check(box == ["2 new signals on Signals FM"], "several signals at once -> one email")

print("\n[3] board API + page")
d = u1.get("/api/signals/board").get_json()
rows = d["rows"]
check(d["ok"] and not d["loading"] and d["counts"]["coins"] == 4, "board lists every coin")
check(rows[0]["state"] == "ACTIVE" and rows[0]["fresh"], "new signals first")
check(rows[0]["progress"]["pct"] == 50.0 and rows[0]["live_price"] == 101.5, "live progress toward the target")
check(rows[-1]["state"] == "UNAVAILABLE" and "error" in rows[-1], "a coin that failed is listed as unavailable")
e = u1.get("/api/signals/events").get_json()
check(e["ok"] and e["events"][0]["signal_at"].endswith("Z") and len(e["events"]) >= 2, "recent events feed")
pg = u1.get("/signals")
check(pg.status_code == 200 and b"Live signals board" in pg.data and b"Signal alerts" in pg.data, "page renders")
check("same_side_warning" in d and d["counts"]["forming"] >= 0, "board reports same-direction warning + forming count")
STATE.update({"BTC/USDT": "old", "ETH/USDT": "old", "SOL/USDT": "new"})
sb._board["bar"] = None
sb.board_step(nxt + timedelta(hours=8))
d = u1.get("/api/signals/board").get_json()
w = d["same_side_warning"]
check(w and w["side"] == "LONG" and w["count"] == 3 and w["total"] == 3, "3 active signals the same way -> warning")
STATE.update({"SOL/USDT": "wait"})

print("\n[4] one-click unsubscribe")
sb._hooks["secret"] = "unit-secret"
html = sb._email_html([{"symbol": "A/USDT", "side": "LONG", "entry": 1, "stop_loss": 0.9, "take_profit": 1.05, "confidence": 70}], user_id=1)
import re as _re
tok = _re.search(r"unsubscribe\?t=([^\"]+)", html).group(1)
check(sb._unsub_user(tok) == 1 and sb._unsub_user(tok + "x") is None, "signed token, tampering rejected")
r = cl.get("/signals/unsubscribe?t=" + tok)
q.execute("SELECT email, enabled FROM signal_alert_subs WHERE user_id=1")
row = q.fetchone()
check(r.status_code == 200 and b"Email alerts are off" in r.data and row["email"] == 0 and row["enabled"] == 1,
      "no login needed: email off, bell alerts kept")
check(cl.get("/signals/unsubscribe?t=bad").status_code == 400, "bad link -> clear message")

print(f"\nALL {passed} CHECKS PASSED")
