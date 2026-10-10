"""End-to-end tests for autotrade.py using a fake Binance client + SQLite shim
(no network, no MySQL needed). Run from project root:  python tests/test_autotrade.py"""
import os
import re
import sqlite3
import sys
import types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --- stub pymysql so db.py imports -------------------------------------------
pm = types.ModuleType("pymysql")
pm.cursors = types.ModuleType("pymysql.cursors")
pm.cursors.DictCursor = object
pm.connect = lambda **kw: None
sys.modules["pymysql"] = pm
sys.modules["pymysql.cursors"] = pm.cursors

sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))
import tempfile
DB_PATH = os.path.join(tempfile.gettempdir(), "autotrade_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)


def _translate(sql):
    s = sql.strip()
    if s.upper().startswith("CREATE TABLE"):
        lines = []
        for line in s.splitlines():
            t = line.strip()
            if t.startswith("INDEX ") or t.startswith("FOREIGN KEY"):
                continue
            lines.append(line)
        s = "\n".join(lines)
        s = s.replace("INT PRIMARY KEY AUTO_INCREMENT", "INTEGER PRIMARY KEY AUTOINCREMENT")
        s = s.replace("TINYINT(1)", "INTEGER")
        s = re.sub(r",\s*\)\s*$", "\n)", s)
    if "GET_LOCK" in s:
        return "SELECT 1 AS got"
    return s.replace("%s", "?")


class Cur:
    def __init__(self, conn):
        self.c = conn.cursor()
        self.rowcount = -1
        self.lastrowid = None

    def execute(self, sql, params=()):
        self.c.execute(_translate(sql), tuple(params or ()))
        self.rowcount = self.c.rowcount
        self.lastrowid = self.c.lastrowid

    def fetchone(self):
        r = self.c.fetchone()
        return dict(r) if r else None

    def fetchall(self):
        return [dict(r) for r in self.c.fetchall()]

    def close(self):
        self.c.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


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


import autotrade as at  # noqa: E402

at.get_db_connection = Conn


# --- fake exchange ------------------------------------------------------------
class FakeBinance:
    state = None

    def __init__(self, api_key, secret, mode, market_type):
        if api_key.startswith("bad"):
            raise RuntimeError("Invalid Api-Key ID")
        self.mode, self.market_type = mode, market_type
        self.st = FakeBinance.state

    def sym(self, s):
        return s

    def has_symbol(self, s):
        return s in self.st["prices"]

    def market_rules(self, s):
        return {"min_amount": 0.00001, "min_cost": 5.0, "taker": 0.001}

    def amount_to_precision(self, s, a):
        return float(int(a * 1e5) / 1e5)

    def price_to_precision(self, s, p):
        return round(p, 4)

    def balance_usdt(self):
        if self.st.get("bal_fail"):
            self.st["bal_fail_calls"] = self.st.get("bal_fail_calls", 0) + 1
            raise RuntimeError("451 restricted location")
        return self.st["usdt"], self.st["usdt"]

    def base_free(self, s):
        return self.st["base"].get(s, 0.0)

    def api_restrictions(self):
        return self.st.get("restrictions")

    def price(self, s):
        return self.st["prices"][s]

    def prices(self, syms):
        return {s: self.st["prices"][s] for s in syms if s in self.st["prices"]}

    def prepare_futures(self, s, lev):
        self.st["calls"].append(("lev", s, lev))

    def futures_positions(self, syms):
        return {s: q for s, q in self.st["fut"].items() if s in syms and q > 0}

    def market_order(self, s, side, amount, reduce_only=False):
        px = self.st["prices"][s]
        self.st["calls"].append(("order", s, side, amount, reduce_only))
        if self.market_type == "spot":
            if side == "buy":
                self.st["usdt"] -= amount * px
                self.st["base"][s] = self.st["base"].get(s, 0) + amount * 0.999  # fee in base
            else:
                self.st["usdt"] += amount * px
                self.st["base"][s] = self.st["base"].get(s, 0) - amount
        else:
            if reduce_only:
                self.st["fut"][s] = max(0.0, self.st["fut"].get(s, 0) - amount)
            else:
                self.st["fut"][s] = self.st["fut"].get(s, 0) + amount
        return {"id": f"o{len(self.st['calls'])}", "average": px, "filled": amount}

    def backup_stop(self, s, side, amount, stop_price):
        self.st["calls"].append(("stop", s, side, amount, stop_price))
        return "stop1"

    def order_average(self, oid, s):
        return self.st.get("stop_fill")

    def cancel_all(self, s):
        self.st["calls"].append(("cancel_all", s))


def reset_state():
    FakeBinance.state = {
        "usdt": 1000.0, "base": {}, "fut": {}, "calls": [],
        "prices": {"BTC/USDT": 60000.0, "ETH/USDT": 3000.0, "SOL/USDT": 150.0, "XRP/USDT": 0.6, "BNB/USDT": 600.0},
        "restrictions": None, "stop_fill": None,
    }
    at._client_cache.clear()
    at._signal_cache.clear()


at._client_factory = lambda k, s, m, mt: FakeBinance(k, s, m, mt)

SIGNALS = {}


def fake_generate_signal(df, symbol="BTC/USDT", include_orderbook=True):
    assert include_orderbook is False
    v, c = SIGNALS.get(symbol, ("WAIT", 50.0))
    return {"final_verdict": v, "confidence_pct": c, "extreme_volatility_95_pct": 1.2,
            "last_price": 1.0, "trend": "Bullish"}


at._hooks.update({
    "get_candles": lambda symbol, timeframe, limit: None,
    "generate_signal": fake_generate_signal,
    "available_coins": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT", "EUR/USD"],
    "data_exchange": None,
})

# --- Flask app ------------------------------------------------------------------
from flask import Flask  # noqa: E402

app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"),
            static_folder=os.path.join(ROOT, "static"))
app.secret_key = "test"
app.register_blueprint(at.autotrade_bp)


@app.route("/login")
def login_page():
    return "login"


from cryptography.fernet import Fernet  # noqa: E402
os.environ["AUTOTRADE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
_enc = Fernet(os.environ["AUTOTRADE_ENCRYPTION_KEY"].encode())

# --- simulate a database from the first release (v1 tables) -------------------
c = Conn()
cu = c.cursor()
cu.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
cu.execute("INSERT INTO users (id, email) VALUES (1, 'a@b.c'), (2, 'old@user.c')")
cu.execute("""CREATE TABLE autotrade_accounts (user_id INTEGER PRIMARY KEY, exchange TEXT, mode TEXT, api_key_enc TEXT,
              api_secret_enc TEXT, key_hint TEXT, balance_usdt REAL, balance_total_usdt REAL, balance_market TEXT,
              balance_at TEXT, connected_at TEXT)""")
cu.execute("""CREATE TABLE autotrade_settings (user_id INTEGER PRIMARY KEY, enabled INTEGER, market_type TEXT, leverage INTEGER,
              risk_pct REAL, max_position_usdt REAL, max_open_positions INTEGER, daily_loss_limit_pct REAL,
              min_confidence REAL, timeframe TEXT, reward_risk REAL, coins TEXT, paused_until TEXT, pause_reason TEXT,
              day_ref_date TEXT, day_ref_balance REAL, last_scan_at TEXT, updated_at TEXT)""")
cu.execute("""CREATE TABLE autotrade_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, level TEXT, message TEXT,
              created_at TEXT)""")
cu.execute("INSERT INTO autotrade_accounts VALUES (2,'binance','demo',?,?,'...OLD1',50,50,'spot',NULL,NULL)",
           (_enc.encrypt(b"OLDKEY" * 4).decode(), _enc.encrypt(b"OLDSECRET" * 3).decode()))
cu.execute("""INSERT INTO autotrade_settings (user_id, enabled, market_type, leverage, risk_pct, max_position_usdt,
              max_open_positions, daily_loss_limit_pct, min_confidence, timeframe, reward_risk, coins)
              VALUES (2,1,'spot',1,0.5,50,1,3,70,'4h',2.0,'["ETH/USDT"]')""")
cu.execute("INSERT INTO autotrade_logs (user_id, level, message, created_at) VALUES (2,'INFO','old log','2026-10-01 00:00:00')")
c.close()

at.init_tables()
at.init_tables()  # second start must be a no-op

client = app.test_client()
passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


def post(path, body=None):
    return client.post(path, json=body if body is not None else {})


def q(sql, params=()):
    c = Conn(); cu = c.cursor(); cu.execute(sql, params)
    try:
        rows = cu.fetchall()
    except Exception:
        rows = []
    c.close()
    return rows


def status(mode):
    return client.get("/api/autotrade/status?account=" + mode).get_json()


reset_state()
print("\n[0] migration from the first release")
acc = q("SELECT * FROM autotrade_exchange_accounts WHERE user_id=2")
check(len(acc) == 1 and acc[0]["mode"] == "demo" and acc[0]["key_hint"] == "...OLD1", "v1 account copied into Demo slot")
st2 = q("SELECT * FROM autotrade_bot_settings WHERE user_id=2")
check(len(st2) == 1 and st2[0]["timeframe"] == "4h" and st2[0]["enabled"] == 1, "v1 settings copied (incl. bot on)")
tables = {r["name"] for r in q("SELECT name FROM sqlite_master WHERE type='table'")}
check("autotrade_accounts_v1_migrated" in tables and "autotrade_accounts" not in tables, "old tables renamed (migration runs once)")
check(q("SELECT mode FROM autotrade_logs WHERE user_id=2")[0]["mode"] is None, "logs.mode column added to old log table")

print("\n[1] auth + status")
r = client.get("/api/autotrade/status?account=demo")
check(r.status_code == 401, "status requires login")
r = client.get("/auto-trading")
check(r.status_code == 302 and "/login" in r.headers["Location"], "page redirects to login")
with client.session_transaction() as s:
    s["user_id"] = 1
r = client.get("/auto-trading")
check(r.status_code == 200 and b"Auto-Trade Bot" in r.data and b'data-mode="live"' in r.data, "page renders with Demo/Live switcher")
r = client.get("/api/autotrade/status?account=xyz")
check(r.status_code == 400, "unknown account rejected")
d = status("demo")
check(d["connected"] is False and d["accounts"]["live"]["connected"] is False, "not connected on either account")
check(d["settings"]["market_type"] == "spot" and d["settings"]["leverage"] == 1, "safe defaults (spot, 1x)")
check("EUR/USD" not in d["options"]["coins"], "forex excluded from coin choices")

print("\n[2] connect both accounts")
saved_key = os.environ.pop("AUTOTRADE_ENCRYPTION_KEY")
r = post("/api/autotrade/connect", {"account": "demo", "api_key": "k" * 20, "api_secret": "s" * 20})
check(r.status_code == 503, "connect refused without encryption key")
os.environ["AUTOTRADE_ENCRYPTION_KEY"] = saved_key
r = client.post("/api/autotrade/connect", data="x")
check(r.status_code == 415, "non-JSON POST rejected")
r = post("/api/autotrade/connect", {"account": "demo", "api_key": "bad" + "k" * 20, "api_secret": "s" * 20})
check(r.status_code == 400 and "Verification failed" in r.get_json()["error"], "bad key -> verification failed (English)")
r = post("/api/autotrade/connect", {"account": "demo", "api_key": "DKEY" + "k" * 20 + "DEMO", "api_secret": "DEMOSECRET" * 3})
check(r.status_code == 200 and r.get_json()["balance_usdt"] == 1000.0, "demo connect ok + balance")
FakeBinance.state["restrictions"] = {"enableWithdrawals": True, "ipRestrict": False}
r = post("/api/autotrade/connect", {"account": "live", "api_key": "L" * 24, "api_secret": "S" * 24})
check(r.status_code == 400 and "WITHDRAWALS" in r.get_json()["error"], "live key with withdrawals refused")
FakeBinance.state["restrictions"] = {"enableWithdrawals": False, "ipRestrict": False}
r = post("/api/autotrade/connect", {"account": "live", "api_key": "LKEY" + "k" * 20 + "LIVE", "api_secret": "LIVESECRET" * 3})
check(r.status_code == 200 and "IP" in (r.get_json()["note"] or ""), "live connect ok + IP tip")
rows = q("SELECT * FROM autotrade_exchange_accounts WHERE user_id=1 ORDER BY mode")
check([r["mode"] for r in rows] == ["demo", "live"], "both accounts stored side by side")
check(all("SECRET" not in r["api_secret_enc"] for r in rows), "keys encrypted at rest")
check(at._decrypt(rows[1]["api_secret_enc"]) == "LIVESECRET" * 3, "live keys decrypt correctly")
d = status("live")
check(d["account"]["key_hint"] == "...LIVE" and d["accounts"]["demo"]["key_hint"] == "...DEMO", "status shows both key hints")

print("\n[3] settings are per account")
r = post("/api/autotrade/settings", {"account": "demo", "leverage": 50})
check(r.status_code == 400 and "Leverage must be" in r.get_json()["error"], "50x leverage rejected")
r = post("/api/autotrade/settings", {"account": "demo", "risk_pct": 10})
check(r.status_code == 400, "10% risk rejected")
r = post("/api/autotrade/settings", {"account": "demo", "coins": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
                                     "max_open_positions": 2, "min_confidence": 65, "max_position_usdt": 300})
check(r.status_code == 200, "demo settings saved")
r = post("/api/autotrade/settings", {"account": "live", "market_type": "futures", "leverage": 3,
                                     "coins": ["SOL/USDT", "XRP/USDT"], "max_position_usdt": 200, "min_confidence": 70})
check(r.status_code == 200 and r.get_json()["settings"]["leverage"] == 3, "live settings saved (futures 3x)")
check(status("demo")["settings"]["market_type"] == "spot" and status("live")["settings"]["market_type"] == "futures",
      "demo and live settings are independent")
r = post("/api/autotrade/settings", {"account": "demo", "market_type": "spot", "leverage": 10})
check(r.get_json()["settings"]["leverage"] == 1, "spot forces 1x")

print("\n[4] pure math")
plan, err = at.plan_levels("LONG", 100.0, 1.0, 1.5, 1)
check(err is None and abs(plan["stop_loss"] - 99.0) < 1e-9 and abs(plan["take_profit"] - 101.5) < 1e-9, "LONG SL/TP levels")
plan, err = at.plan_levels("SHORT", 100.0, 1.0, 2.0, 1)
check(abs(plan["stop_loss"] - 101.0) < 1e-9 and abs(plan["take_profit"] - 98.0) < 1e-9, "SHORT SL/TP levels")
plan, err = at.plan_levels("LONG", 0.12, 0.01, 1.5, 1)
check(plan["sl_pct"] == at.SL_MIN_PCT and plan["stop_loss"] < 0.12, "tiny vol clamped, full precision for cheap coins")
_, err = at.plan_levels("LONG", 100.0, 4.0, 1.5, 20)
check(err and "liquidation" in err, "20x with 4% SL blocked")
n, risk = at.size_position(1000, 1.0, 1.0, 10000, 1, "spot")
check(abs(risk - 10) < 1e-9 and abs(n - 950) < 1e-9, "spot sizing capped by free balance")
n, risk = at.size_position(1000, 1.0, 2.0, 200, 5, "futures")
check(abs(n - 200) < 1e-9, "futures sizing capped by max position")
check(at.level_hit("LONG", 98.9, 99, 101.5) == "SL" and at.level_hit("SHORT", 97.9, 101, 98) == "TP", "level_hit")

print("\n[5] manual demo trade: preview then execute")
r = post("/api/autotrade/manual", {"account": "demo", "symbol": "ETH/USDT", "side": "SHORT"})
check(r.status_code == 400 and "spot" in r.get_json()["error"], "spot SHORT rejected")
n_calls = len(FakeBinance.state["calls"])
r = post("/api/autotrade/manual", {"account": "demo", "symbol": "ETH/USDT", "side": "LONG", "confirm": False})
d = r.get_json()
check(r.status_code == 200 and d["executed"] is False and len(FakeBinance.state["calls"]) == n_calls, "preview places no order")
r = post("/api/autotrade/manual", {"account": "demo", "symbol": "ETH/USDT", "side": "LONG", "confirm": True})
check(r.status_code == 200 and r.get_json()["mode"] == "demo", "manual LONG executed on demo")
r = post("/api/autotrade/manual", {"account": "demo", "symbol": "ETH/USDT", "side": "LONG", "confirm": True})
check(r.status_code == 400 and "already open" in r.get_json()["error"], "duplicate position on same coin blocked")
r = post("/api/autotrade/manual", {"account": "live", "symbol": "XRP/USDT", "side": "LONG", "confirm": True})
check(r.status_code == 428, "live manual order needs explicit ack")

print("\n[6] monitor -> take profit")
FakeBinance.state["prices"]["ETH/USDT"] = 3000 * 1.05
at.monitor_positions()
p = q("SELECT * FROM autotrade_positions WHERE symbol='ETH/USDT'")[0]
check(p["status"] == "CLOSED" and p["close_reason"] == "TP" and p["pnl_usdt"] > 0 and p["mode"] == "demo", "TP hit -> closed in profit")

print("\n[7] both bots run at the same time")
r = post("/api/autotrade/bot", {"account": "demo", "enabled": True})
check(r.status_code == 200, "demo bot on (no ack needed)")
r = post("/api/autotrade/bot", {"account": "live", "enabled": True})
check(r.status_code == 428 and r.get_json()["need_ack"], "live bot needs explicit risk ack")
r = post("/api/autotrade/bot", {"account": "live", "enabled": True, "ack_live": True})
check(r.status_code == 200, "live bot on with ack")
due = set(at._due_bots())
check((1, "demo") in due and (1, "live") in due and (2, "demo") in due, "engine schedules both bots (+ migrated user)")
SIGNALS.update({"BTC/USDT": ("LONG", 72.0), "ETH/USDT": ("LONG", 60.0), "SOL/USDT": ("SHORT", 80.0), "XRP/USDT": ("LONG", 66.0)})
at._signal_cache.clear()
at.scan_user(1, "demo")
at.scan_user(1, "live")
open_rows = q("SELECT mode, symbol, side, market_type FROM autotrade_positions WHERE status='OPEN' ORDER BY mode, symbol")
got = [(r["mode"], r["symbol"], r["side"]) for r in open_rows]
check(got == [("demo", "BTC/USDT", "LONG"), ("live", "SOL/USDT", "SHORT")], f"each bot traded with its own settings: {got}")
demo_scan = q("SELECT message FROM autotrade_logs WHERE level='SCAN' AND mode='demo' ORDER BY id DESC LIMIT 1")[0]["message"]
live_scan = q("SELECT message FROM autotrade_logs WHERE level='SCAN' AND mode='live' ORDER BY id DESC LIMIT 1")[0]["message"]
check("ETH: cooldown" in demo_scan and "SOL: SHORT 80% (no shorts on spot)" in demo_scan, "demo scan log: " + demo_scan)
check("XRP: LONG 66% (low confidence)" in live_scan, "live scan log: " + live_scan)
d_demo, d_live = status("demo"), status("live")
check([p["symbol"] for p in d_demo["positions"]] == ["BTC/USDT"] and [p["symbol"] for p in d_live["positions"]] == ["SOL/USDT"],
      "status shows only the selected account's positions")
check(d_demo["accounts"]["live"]["bot_enabled"] and d_demo["accounts"]["live"]["open_positions"] == 1, "switcher summary sees the other account")
check(not any("OPEN SHORT SOL/USDT" in l["message"] for l in d_demo["logs"]) and
      any("OPEN SHORT SOL/USDT" in l["message"] for l in d_live["logs"]), "each account shows only its own trade log")

print("\n[8] stop-loss + disconnect guard")
r = post("/api/autotrade/disconnect", {"account": "demo"})
check(r.status_code == 409, "disconnect blocked while demo positions open")
FakeBinance.state["prices"]["BTC/USDT"] = 60000 * 0.97
at.monitor_positions()
p = q("SELECT * FROM autotrade_positions WHERE symbol='BTC/USDT'")[0]
check(p["status"] == "CLOSED" and p["close_reason"] == "SL" and p["pnl_usdt"] < 0, "SL hit -> closed at loss")
check(at._claim(p["id"]) is False, "closed position cannot be claimed again")

print("\n[9] live futures: backup stop + external close")
calls = FakeBinance.state["calls"]
check(("lev", "SOL/USDT", 3) in calls, "leverage set before futures order")
stops = [x for x in calls if x[0] == "stop"]
sol = q("SELECT * FROM autotrade_positions WHERE status='OPEN' AND symbol='SOL/USDT'")[0]
check(len(stops) == 1 and stops[0][4] > sol["stop_loss"] > sol["entry_price"], "backup stop placed beyond the bot SL")
FakeBinance.state["fut"]["SOL/USDT"] = 0
FakeBinance.state["stop_fill"] = stops[0][4]
c = Conn(); cu = c.cursor()
cu.execute("UPDATE autotrade_positions SET opened_at=? WHERE id=?", (datetime.utcnow() - timedelta(minutes=5), sol["id"])); c.close()
at.monitor_positions()
sol2 = q("SELECT * FROM autotrade_positions WHERE id=?", (sol["id"],))[0]
check(sol2["status"] == "CLOSED" and sol2["close_reason"] == "EXCHANGE" and sol2["pnl_usdt"] < 0, "external close detected + recorded")

print("\n[10] stop all")
at._signal_cache.clear()
SIGNALS.update({"XRP/USDT": ("LONG", 90.0)})
c = Conn(); cu = c.cursor()
cu.execute("UPDATE autotrade_positions SET closed_at=? WHERE status='CLOSED'", (datetime.utcnow() - timedelta(hours=3),)); c.close()
FakeBinance.state["prices"]["BTC/USDT"] = 60000.0
at.scan_user(1, "demo"); at.scan_user(1, "live")
n_open = {r["mode"]: r["n"] for r in q("SELECT mode, COUNT(*) AS n FROM autotrade_positions WHERE status='OPEN' GROUP BY mode")}
check(n_open.get("demo", 0) >= 1 and n_open.get("live", 0) >= 1, f"positions open on both accounts: {n_open}")
r = post("/api/autotrade/panic", {"account": "demo"})
check(r.status_code == 200, "stop all on demo")
d = status("demo")
check(not d["positions"] and not d["bot"]["enabled"] and d["accounts"]["live"]["bot_enabled"], "demo stopped, live untouched")
r = post("/api/autotrade/panic", {"account": "all"})
check(r.status_code == 200 and r.get_json()["closed"] >= 1, "stop both accounts")
check(not status("live")["positions"] and not status("live")["bot"]["enabled"], "live stopped too")
r = post("/api/autotrade/panic", {"account": "nope"})
check(r.status_code == 400, "invalid panic scope rejected")

print("\n[11] daily loss limit (per account)")
c = Conn(); cu = c.cursor()
cu.execute("UPDATE autotrade_bot_settings SET day_ref_date=?, day_ref_balance=? WHERE user_id=1 AND mode='demo'",
           (datetime.utcnow().strftime("%Y-%m-%d"), 1000.0))
cu.execute("""INSERT INTO autotrade_positions (user_id, mode, market_type, symbol, side, source, amount, entry_price, stop_loss,
              take_profit, leverage, notional_usdt, fee_rate, status, pnl_usdt, opened_at, closed_at)
              VALUES (1,'demo','spot','BNB/USDT','LONG','BOT',1,600,590,615,1,600,0,'CLOSED',-80,?,?)""",
           (datetime.utcnow(), datetime.utcnow()))
c.close()
post("/api/autotrade/bot", {"account": "demo", "enabled": True})
post("/api/autotrade/bot", {"account": "live", "enabled": True, "ack_live": True})
at.scan_user(1, "demo")
check(status("demo")["bot"]["paused_until"] is not None, "demo paused after a >5% loss today")
check(status("live")["bot"]["paused_until"] is None, "live is not paused by demo losses")

print("\n[12] engine bookkeeping")
at._heartbeat(); at._heartbeat()
check(status("demo")["engine"]["online"] is True, "heartbeat -> engine online")
c = Conn(); cu = c.cursor()
cu.execute("""INSERT INTO autotrade_positions (user_id, mode, market_type, symbol, side, source, amount, entry_price, stop_loss,
              take_profit, leverage, notional_usdt, fee_rate, status, opened_at, status_changed_at)
              VALUES (1,'live','futures','ETH/USDT','LONG','BOT',0.1,3000,2950,3075,1,300,0,'CLOSING',?,?)""",
           (datetime.utcnow(), datetime.utcnow() - timedelta(minutes=10)))
stuck = cu.lastrowid; c.close()
at.recover_stuck_positions()
check(q("SELECT status FROM autotrade_positions WHERE id=?", (stuck,))[0]["status"] == "OPEN", "stuck CLOSING position recovered")
at.prune_logs()
check(at.validate_settings({"timeframe": "1m"}, at.DEFAULT_SETTINGS)[1] is not None, "invalid timeframe rejected")
FakeBinance.state["bal_fail"] = True
c = Conn(); cu = c.cursor(); cu.execute("UPDATE autotrade_exchange_accounts SET balance_at=?", (datetime.utcnow() - timedelta(hours=1),)); c.close()
at._client_cache.clear()
status("demo"); d2 = status("demo")
check(FakeBinance.state["bal_fail_calls"] == 1 and d2["account"]["balance_error"], "failing balance refresh throttled")

print(f"\nALL {passed} CHECKS PASSED")
