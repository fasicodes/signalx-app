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


at.init_tables()
c = Conn()
cu = c.cursor()
cu.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT)")
cu.execute("INSERT INTO users (id, email) VALUES (1, 'a@b.c')")
c.close()

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


reset_state()
print("\n[1] auth + status")
r = client.get("/api/autotrade/status")
check(r.status_code == 401, "status requires login")
r = client.get("/auto-trading")
check(r.status_code == 302 and "/login" in r.headers["Location"], "page redirects to login")
with client.session_transaction() as s:
    s["user_id"] = 1
r = client.get("/auto-trading")
check(r.status_code == 200 and b"Auto-Trade Bot" in r.data, "page renders for logged-in user")
check(b"localStorage.setItem('signal_fm_binance_secret'" not in r.data, "no secret stored in browser")
r = client.get("/api/autotrade/status")
d = r.get_json()
check(r.status_code == 200 and d["connected"] is False, "status ok, not connected")
check(d["settings"]["market_type"] == "spot" and d["settings"]["leverage"] == 1, "safe defaults (spot, 1x)")
check("EUR/USD" not in d["options"]["coins"], "forex excluded from coin choices")

print("\n[2] connect")
os.environ.pop("AUTOTRADE_ENCRYPTION_KEY", None)
r = post("/api/autotrade/connect", {"api_key": "k" * 20, "api_secret": "s" * 20, "mode": "demo"})
check(r.status_code == 503, "connect refused without encryption key")
from cryptography.fernet import Fernet  # noqa: E402
os.environ["AUTOTRADE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
r = client.post("/api/autotrade/connect", data="x")
check(r.status_code == 415, "non-JSON POST rejected")
r = post("/api/autotrade/connect", {"api_key": "bad" + "k" * 20, "api_secret": "s" * 20, "mode": "demo"})
check(r.status_code == 400 and "Verify fail" in r.get_json()["error"], "bad key -> verify fail")
r = post("/api/autotrade/connect", {"api_key": "AKEY" + "k" * 20 + "WXYZ", "api_secret": "SECRETVALUE" * 3, "mode": "demo"})
check(r.status_code == 200 and r.get_json()["balance_usdt"] == 1000.0, "demo connect ok + balance")
c = Conn(); cu = c.cursor()
cu.execute("SELECT * FROM autotrade_accounts WHERE user_id=1"); row = cu.fetchone(); c.close()
check("SECRETVALUE" not in row["api_secret_enc"] and "AKEY" not in row["api_key_enc"], "keys encrypted at rest")
check(at._decrypt(row["api_secret_enc"]) == "SECRETVALUE" * 3, "keys decrypt correctly")
check(row["key_hint"] == "...WXYZ", "only last 4 chars shown")

print("\n[3] settings validation")
r = post("/api/autotrade/settings", {"leverage": 50})
check(r.status_code == 400, "50x leverage rejected")
r = post("/api/autotrade/settings", {"risk_pct": 10})
check(r.status_code == 400, "10% risk rejected")
r = post("/api/autotrade/settings", {"coins": ["BTC/USDT"] * 3 + ["DOGE/FAKE"]})
check(r.status_code == 200 and r.get_json()["settings"]["coins"] == ["BTC/USDT"], "coins deduped + filtered")
r = post("/api/autotrade/settings", {"market_type": "spot", "leverage": 10})
check(r.get_json()["settings"]["leverage"] == 1, "spot forces 1x")
r = post("/api/autotrade/settings", {"coins": ["BTC/USDT", "ETH/USDT", "SOL/USDT"], "max_open_positions": 2,
                                     "min_confidence": 65, "max_position_usdt": 300})
check(r.status_code == 200, "valid settings saved")

print("\n[4] pure math")
plan, err = at.plan_levels("LONG", 100.0, 1.0, 1.5, 1)
check(err is None and abs(plan["stop_loss"] - 99.0) < 1e-9 and abs(plan["take_profit"] - 101.5) < 1e-9, "LONG SL/TP levels")
plan, err = at.plan_levels("SHORT", 100.0, 1.0, 2.0, 1)
check(abs(plan["stop_loss"] - 101.0) < 1e-9 and abs(plan["take_profit"] - 98.0) < 1e-9, "SHORT SL/TP levels")
plan, err = at.plan_levels("LONG", 0.12, 0.01, 1.5, 1)
check(plan["sl_pct"] == at.SL_MIN_PCT and plan["stop_loss"] < 0.12, "tiny vol clamped to min SL, full precision for cheap coins")
_, err = at.plan_levels("LONG", 100.0, 4.0, 1.5, 20)
check(err and "liquidation" in err, "20x with 4% SL blocked (too close to liquidation)")
n, risk = at.size_position(1000, 1.0, 1.0, 10000, 1, "spot")
check(abs(risk - 10) < 1e-9 and abs(n - 950) < 1e-9, "spot sizing capped by free balance")
n, risk = at.size_position(1000, 1.0, 2.0, 200, 5, "futures")
check(abs(n - 200) < 1e-9, "futures sizing capped by max position")
check(abs(at.calc_pnl("LONG", 100, 110, 2, 0) - 20) < 1e-9 and abs(at.calc_pnl("SHORT", 100, 110, 2, 0) + 20) < 1e-9, "pnl math")
check(at.level_hit("LONG", 98.9, 99, 101.5) == "SL" and at.level_hit("SHORT", 97.9, 101, 98) == "TP", "level_hit")

print("\n[5] manual spot trade: preview then execute")
r = post("/api/autotrade/manual", {"symbol": "ETH/USDT", "side": "SHORT"})
check(r.status_code == 400 and "Spot" in r.get_json()["error"], "spot SHORT rejected")
calls_before = len(FakeBinance.state["calls"])
r = post("/api/autotrade/manual", {"symbol": "ETH/USDT", "side": "LONG", "confirm": False})
d = r.get_json()
check(r.status_code == 200 and d["executed"] is False and len(FakeBinance.state["calls"]) == calls_before, "preview places no order")
check(d["stop_loss"] < 3000 < d["take_profit"], "preview SL/TP sane")
r = post("/api/autotrade/manual", {"symbol": "ETH/USDT", "side": "LONG", "confirm": True})
d = r.get_json()
check(r.status_code == 200 and d["executed"] and d["id"], "manual LONG executed")
r = post("/api/autotrade/manual", {"symbol": "ETH/USDT", "side": "LONG", "confirm": True})
check(r.status_code == 400 and "pehle se" in r.get_json()["error"], "duplicate position on same coin blocked")

print("\n[6] monitor -> take profit")
FakeBinance.state["prices"]["ETH/USDT"] = 3000 * 1.05
at.monitor_positions()
c = Conn(); cu = c.cursor()
cu.execute("SELECT * FROM autotrade_positions WHERE symbol='ETH/USDT'"); p = cu.fetchone(); c.close()
check(p["status"] == "CLOSED" and p["close_reason"] == "TP" and p["pnl_usdt"] > 0, "TP hit -> closed in profit")
sold = [x for x in FakeBinance.state["calls"] if x[0] == "order" and x[2] == "sell"]
check(sold and sold[-1][3] <= p["amount"], "spot sell uses held amount (fee-adjusted)")

print("\n[7] bot scan (spot)")
r = post("/api/autotrade/bot", {"enabled": True})
check(r.status_code == 200, "bot enabled (demo needs no ack)")
SIGNALS.update({"BTC/USDT": ("LONG", 72.0), "ETH/USDT": ("LONG", 60.0), "SOL/USDT": ("SHORT", 80.0)})
at.scan_user(1)
c = Conn(); cu = c.cursor()
cu.execute("SELECT message FROM autotrade_logs WHERE level='SCAN' ORDER BY id DESC LIMIT 1"); scan0 = cu.fetchone()["message"]; c.close()
check("ETH: cooldown" in scan0, "recently closed coin is on cooldown")
c = Conn(); cu = c.cursor()
cu.execute("UPDATE autotrade_positions SET closed_at=? WHERE symbol='ETH/USDT'", (datetime.utcnow() - timedelta(hours=2),))
cu.execute("UPDATE autotrade_settings SET last_scan_at=NULL WHERE user_id=1"); c.close()
at._signal_cache.clear()  # manual preview earlier cached ETH as WAIT
at.scan_user(1)
c = Conn(); cu = c.cursor()
cu.execute("SELECT symbol, side, source FROM autotrade_positions WHERE status='OPEN'"); rows = cu.fetchall()
cu.execute("SELECT message FROM autotrade_logs WHERE level='SCAN' ORDER BY id DESC LIMIT 1"); scan = cu.fetchone()["message"]; c.close()
check([r["symbol"] for r in rows] == ["BTC/USDT"] and rows[0]["source"] == "BOT", "only BTC (72% LONG) traded")
check("kam confidence" in scan and "position khuli" in scan and "spot mein short nahi" in scan, "scan log explains skips: " + scan)
at.scan_user(1)
c = Conn(); cu = c.cursor()
cu.execute("SELECT COUNT(*) AS n FROM autotrade_positions WHERE status='OPEN'"); n = cu.fetchone()["n"]; c.close()
check(n == 1, "re-scan does not duplicate open position")

print("\n[8] stop-loss + disconnect guard + panic")
r = post("/api/autotrade/disconnect")
check(r.status_code == 409, "disconnect blocked while positions open")
FakeBinance.state["prices"]["BTC/USDT"] = 60000 * 0.97
at.monitor_positions()
c = Conn(); cu = c.cursor()
cu.execute("SELECT * FROM autotrade_positions WHERE symbol='BTC/USDT'"); p = cu.fetchone(); c.close()
check(p["status"] == "CLOSED" and p["close_reason"] == "SL" and p["pnl_usdt"] < 0, "SL hit -> closed at loss")
check(at._claim(p["id"]) is False, "closed position cannot be claimed again")

print("\n[9] futures: leverage, backup stop, external close")
reset_state()
at._signal_cache.clear()
r = post("/api/autotrade/bot", {"enabled": False})
r = post("/api/autotrade/settings", {"market_type": "futures", "leverage": 5, "coins": ["SOL/USDT", "XRP/USDT"],
                                     "max_open_positions": 2, "max_position_usdt": 300, "min_confidence": 65})
check(r.get_json()["settings"]["leverage"] == 5, "futures 5x saved")
r = post("/api/autotrade/bot", {"enabled": True})
SIGNALS.update({"SOL/USDT": ("SHORT", 80.0), "XRP/USDT": ("LONG", 70.0)})
at.scan_user(1)
calls = FakeBinance.state["calls"]
check(("lev", "SOL/USDT", 5) in calls, "leverage set before futures order")
stops = [x for x in calls if x[0] == "stop"]
check(len(stops) == 2, "backup stop placed for each futures position")
c = Conn(); cu = c.cursor()
cu.execute("SELECT * FROM autotrade_positions WHERE status='OPEN' AND symbol='SOL/USDT'"); sol = cu.fetchone(); c.close()
check(sol["side"] == "SHORT" and sol["stop_loss"] > sol["entry_price"], "futures SHORT opened with SL above entry")
sol_stop = [x for x in stops if x[1] == "SOL/USDT"][0]
check(sol_stop[4] > sol["stop_loss"], "backup stop sits beyond bot SL")
# simulate Binance closing SOL (backup stop fired) while server was away
FakeBinance.state["fut"]["SOL/USDT"] = 0
FakeBinance.state["stop_fill"] = sol_stop[4]
c = Conn(); cu = c.cursor()
cu.execute("UPDATE autotrade_positions SET opened_at=? WHERE id=?", ((datetime.utcnow() - timedelta(minutes=5)), sol["id"]))
c.close()
at.monitor_positions()
c = Conn(); cu = c.cursor()
cu.execute("SELECT * FROM autotrade_positions WHERE id=?", (sol["id"],)); sol2 = cu.fetchone(); c.close()
check(sol2["status"] == "CLOSED" and sol2["close_reason"] == "EXCHANGE" and sol2["pnl_usdt"] < 0, "external close detected + recorded")

print("\n[10] panic")
r = post("/api/autotrade/panic")
d = r.get_json()
check(r.status_code == 200 and d["closed"] == 1, "panic closed remaining position")
r = client.get("/api/autotrade/status")
d = r.get_json()
check(d["bot"]["enabled"] is False and not d["positions"], "panic -> bot off, no open positions")
check(d["stats"]["closed_count"] == 4, "history counts all closed trades")

print("\n[11] daily loss limit")
c = Conn(); cu = c.cursor()
cu.execute("UPDATE autotrade_settings SET day_ref_date=?, day_ref_balance=? WHERE user_id=1",
           (datetime.utcnow().strftime("%Y-%m-%d"), 1000.0))
cu.execute("""INSERT INTO autotrade_positions (user_id, mode, market_type, symbol, side, source, amount, entry_price, stop_loss,
              take_profit, leverage, notional_usdt, fee_rate, status, pnl_usdt, opened_at, closed_at)
              VALUES (1,'demo','futures','BNB/USDT','LONG','BOT',1,600,590,615,1,600,0,'CLOSED',-80,?,?)""",
           (datetime.utcnow(), datetime.utcnow()))
c.close()
post("/api/autotrade/bot", {"enabled": True})
SIGNALS["XRP/USDT"] = ("LONG", 90.0)
at.scan_user(1)
r = client.get("/api/autotrade/status"); d = r.get_json()
check(d["bot"]["paused_until"] is not None and not d["positions"], "loss > 5% today -> paused, no new trades")

print("\n[12] live mode safety")
post("/api/autotrade/bot", {"enabled": False})
FakeBinance.state["restrictions"] = {"enableWithdrawals": True, "ipRestrict": False}
r = post("/api/autotrade/connect", {"api_key": "L" * 24, "api_secret": "S" * 24, "mode": "live"})
check(r.status_code == 400 and "WITHDRAWAL" in r.get_json()["error"], "live key with withdrawals refused")
FakeBinance.state["restrictions"] = {"enableWithdrawals": False, "ipRestrict": False}
r = post("/api/autotrade/connect", {"api_key": "L" * 24, "api_secret": "S" * 24, "mode": "live"})
check(r.status_code == 200 and "IP" in (r.get_json()["note"] or ""), "live connect ok + IP whitelist tip")
r = post("/api/autotrade/bot", {"enabled": True})
check(r.status_code == 428 and r.get_json()["need_ack"], "live bot needs explicit risk ack")
r = post("/api/autotrade/bot", {"enabled": True, "ack_live": True})
check(r.status_code == 200, "live bot enabled with ack")
r = post("/api/autotrade/manual", {"symbol": "XRP/USDT", "side": "LONG", "confirm": True})
check(r.status_code == 428, "live manual order needs ack")

print("\n[13] engine bookkeeping")
at._heartbeat(); at._heartbeat()
r = client.get("/api/autotrade/status"); d = r.get_json()
check(d["engine"]["online"] is True, "heartbeat -> engine online")
c = Conn(); cu = c.cursor()
cu.execute("""INSERT INTO autotrade_positions (user_id, mode, market_type, symbol, side, source, amount, entry_price, stop_loss,
              take_profit, leverage, notional_usdt, fee_rate, status, opened_at, status_changed_at)
              VALUES (1,'live','futures','ETH/USDT','LONG','BOT',0.1,3000,2950,3075,1,300,0,'CLOSING',?,?)""",
           (datetime.utcnow(), datetime.utcnow() - timedelta(minutes=10)))
stuck = cu.lastrowid
c.close()
at.recover_stuck_positions()
c = Conn(); cu = c.cursor()
cu.execute("SELECT status FROM autotrade_positions WHERE id=?", (stuck,)); st = cu.fetchone()["status"]; c.close()
check(st == "OPEN", "stuck CLOSING position recovered to OPEN")
at.prune_logs()
check(at.validate_settings({"timeframe": "1m"}, at.DEFAULT_SETTINGS)[1] is not None, "invalid timeframe rejected")

FakeBinance.state["bal_fail"] = True
c = Conn(); cu = c.cursor(); cu.execute("UPDATE autotrade_accounts SET balance_at=?", (datetime.utcnow() - timedelta(hours=1),)); c.close()
at._client_cache.clear()
d1 = client.get("/api/autotrade/status").get_json(); d2 = client.get("/api/autotrade/status").get_json()
check(FakeBinance.state["bal_fail_calls"] == 1 and d2["account"]["balance_error"], "failing balance refresh throttled, error still shown")

print(f"\nALL {passed} CHECKS PASSED")
