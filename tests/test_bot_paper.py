"""Auto-trade bot on the Site demo account (autotrade.py mode "paper" -> papertrade.py) and the bot page
assistant (assistant.py). Fake price feed + SQLite shim: no network, no MySQL. Run from the project root:

    python tests/test_bot_paper.py
"""
import os
import re
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pm = types.ModuleType("pymysql")
pm.cursors = types.ModuleType("pymysql.cursors")
pm.cursors.DictCursor = object
pm.connect = lambda **kw: None
sys.modules.setdefault("pymysql", pm)
sys.modules.setdefault("pymysql.cursors", pm.cursors)
os.environ.pop("ANTHROPIC_API_KEY", None)

sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))
DB_PATH = os.path.join(tempfile.gettempdir(), "bot_paper_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)


def _translate(sql):
    s = sql.strip()
    if "GET_LOCK" in s:
        return "SELECT 1 AS got"
    if "RELEASE_LOCK" in s:
        return "SELECT 1 AS rel"
    if s.upper().startswith("CREATE TABLE"):
        lines = [ln for ln in s.splitlines() if not ln.strip().startswith(("INDEX ", "FOREIGN KEY"))]
        s = "\n".join(lines)
        s = s.replace("INT PRIMARY KEY AUTO_INCREMENT", "INTEGER PRIMARY KEY AUTOINCREMENT")
        s = s.replace("TINYINT(1)", "INTEGER")
        s = re.sub(r",\s*\)\s*$", "\n)", s)
    return s.replace("%s", "?")


class Cur:
    def __init__(self, conn):
        self.c = conn.cursor()
        self.rowcount = -1
        self.lastrowid = None

    def execute(self, sql, params=()):
        tsql = _translate(sql)
        self.c.execute(tsql, tuple(params or ()) if "?" in tsql else ())
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


import papertrade as pt  # noqa: E402
import autotrade as at  # noqa: E402
import assistant  # noqa: E402

pt.get_db_connection = Conn
at.get_db_connection = Conn


class FakeExchange:
    precisionMode = 4

    def __init__(self):
        self.prices = {}
        self.markets = {}

    def load_markets(self):
        self.markets = {s: {"precision": {"price": 0.01, "amount": 0.0001}, "limits": {"amount": {"min": 0.0001}}}
                        for s in ("BTC/USDT", "ETH/USDT", "SOL/USDT")}
        return self.markets

    def fetch_tickers(self, symbols=None):
        return {s: {"last": p, "bid": p, "ask": p, "high": p, "low": p, "percentage": 1.0, "quoteVolume": 1e9}
                for s, p in self.prices.items()}

    def fetch_ticker(self, s):
        return self.fetch_tickers()[s]


EX = FakeExchange()
pt._hooks["exchange"] = EX
pt._hooks["available_coins"] = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


def set_prices(**kw):
    for k, v in kw.items():
        EX.prices[k.replace("_", "/")] = float(v)
    pt.feed._fetched_at = 0.0


SIGNALS = {}


def engine_signal(sym):
    side, conf = SIGNALS.get(sym, ("WAIT", 50.0))
    return {"verdict": side, "fresh": True, "confidence": conf, "sl_pct": 2.0, "tp_pct": 3.0, "entry": EX.prices.get(sym),
            "timeframe": "4h", "p_long": 0.7, "p_short": 0.3, "active": {}}


at._hooks.update({"engine_signal": engine_signal, "available_coins": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
                  "data_exchange": EX})

from flask import Flask  # noqa: E402

app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"), static_folder=os.path.join(ROOT, "static"))
app.secret_key = "t"
app.register_blueprint(at.autotrade_bp)
app.register_blueprint(pt.paper_bp)


@app.route("/login")
def login_page():
    return "login"


c = Conn()
c.cursor().execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
c.cursor().execute("INSERT INTO users (id, email) VALUES (1, 'a@b.c')")
c.close()
os.environ.pop("AUTOTRADE_ENCRYPTION_KEY", None)   # the Site demo account must work without it
pt.init_papertrade(start=False)
at.init_autotrade(start=False)

client = app.test_client()
with client.session_transaction() as s:
    s["user_id"] = 1
passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


def q(sql, params=()):
    cc = Conn()
    cu = cc.cursor()
    cu.execute(sql, params)
    rows = cu.fetchall()
    cc.close()
    return rows


def status(mode="paper"):
    r = client.get("/api/autotrade/status?account=" + mode)
    assert r.status_code == 200, r.data
    return r.get_json()


set_prices(BTC_USDT=60000, ETH_USDT=3000, SOL_USDT=150)

print("\n[1] Site demo account: no keys, always connected")
d = status("paper")
check(d["connected"] is True and d["accounts"]["paper"]["connected"] is True, "Site demo is connected without keys")
check(d["ready"]["encryption"] is False, "encryption key missing on purpose")
check(abs(d["account"]["balance_usdt"] - 10000) < 1e-6, "balance = demo account available (10,000 USDT)")
check(d["accounts"]["demo"]["connected"] is False, "Binance demo still not connected")
r = client.post("/api/autotrade/connect", json={"account": "paper", "api_key": "x" * 20, "api_secret": "y" * 20})
check(r.status_code == 400 and "no API keys" in r.get_json()["error"], "connect is refused for Site demo")
r = client.get("/auto-trading")
check(r.status_code == 200 and b'data-mode="paper"' in r.data and b"Site demo" in r.data, "page shows the Site demo switch")

print("\n[2] bot opens a futures trade on the demo account")
r = client.post("/api/autotrade/settings", json={"account": "paper", "market_type": "futures", "leverage": 3, "risk_pct": 1,
                                                  "max_position_usdt": 5000, "max_open_positions": 2, "daily_loss_limit_pct": 5,
                                                  "min_confidence": 60, "timeframe": "1h", "reward_risk": 1.5,
                                                  "coins": ["BTC/USDT", "ETH/USDT"]})
check(r.status_code == 200, "futures settings saved")
r = client.post("/api/autotrade/bot", json={"account": "paper", "enabled": True})
check(r.status_code == 200 and r.get_json()["enabled"] is True, "bot turned on without keys")
SIGNALS["BTC/USDT"] = ("LONG", 72.0)
SIGNALS["ETH/USDT"] = ("WAIT", 40.0)
at.scan_user(1, "paper")
bot_pos = q("SELECT * FROM autotrade_positions WHERE mode='paper' AND status='OPEN'")
check(len(bot_pos) == 1 and bot_pos[0]["symbol"] == "BTC/USDT" and bot_pos[0]["side"] == "LONG", "bot position recorded")
paper_pos = q("SELECT * FROM paper_positions WHERE user_id=1")
check(len(paper_pos) == 1 and paper_pos[0]["side"] == "LONG" and paper_pos[0]["market"] == "futures", "demo account holds the LONG")
pp = paper_pos[0]
check(pp["leverage"] == 3 and pp["margin_mode"] == "isolated", "leverage 3x isolated on the demo position")
check(pp["tag"] == pt.BOT_TAG, "demo position tagged as a bot trade")
check(abs(pp["sl_price"] - bot_pos[0]["stop_loss"]) < 0.02 and abs(pp["tp_price"] - bot_pos[0]["take_profit"]) < 0.02,
      "the bot's SL/TP sit on the demo position")
risk = (pp["entry_price"] - pp["sl_price"]) * pp["qty"]
check(80 <= risk <= 101, f"size risks about 1% of the balance ({risk:.2f} USDT)")
logs = [l["message"] for l in q("SELECT message FROM autotrade_logs WHERE mode='paper' ORDER BY id")]
check(any(m.startswith("OPEN LONG BTC/USDT") for m in logs), "trade logged")
check(any("ETH: WAIT" in m for m in logs if m.startswith("Scan")), "scan log explains ETH")
d = status("paper")
check(d["account"]["balance_usdt"] < 10000 and len(d["positions"]) == 1, "status shows the margin used and the position")

print("\n[3] the demo engine closes at the stop-loss, the bot records it")
sl = pp["sl_price"]
set_prices(BTC_USDT=sl - 50)
pt.engine_tick(pt.feed.refresh(max_age=0))
check(not q("SELECT * FROM paper_positions WHERE user_id=1"), "demo engine closed the position at the stop")
closed = q("SELECT * FROM paper_closed WHERE user_id=1")
check(len(closed) == 1 and closed[0]["close_reason"] == "SL", "closed demo trade has reason SL")
cc = Conn(); cc.cursor().execute("UPDATE autotrade_positions SET opened_at=?", (datetime.utcnow() - timedelta(minutes=5),)); cc.close()
at.monitor_positions()
row = q("SELECT * FROM autotrade_positions WHERE mode='paper'")[0]
check(row["status"] == "CLOSED" and row["close_reason"] == "SL", "bot position closed with reason SL")
check(abs(row["pnl_usdt"] - closed[0]["net_pnl"]) < 0.01 and row["pnl_usdt"] < 0, "bot PnL = demo net PnL (fees included)")

print("\n[4] manual close + coins traded by hand are left alone")
set_prices(BTC_USDT=60000, ETH_USDT=3000)
SIGNALS["BTC/USDT"] = ("WAIT", 40.0)
r = client.post("/api/paper/orders", json={"market": "futures", "symbol": "ETH/USDT", "side": "BUY", "type": "MARKET", "qty": 0.5})
check(r.status_code == 200, "user opens ETH by hand on Demo trading")
SIGNALS["ETH/USDT"] = ("SHORT", 80.0)
cc = Conn(); cc.cursor().execute("UPDATE autotrade_bot_settings SET last_scan_at=NULL"); cc.close()
at._signal_cache.clear()
at.scan_user(1, "paper")
check(not q("SELECT * FROM autotrade_positions WHERE mode='paper' AND status='OPEN'"), "bot skips a coin held by hand")
warn = [l["message"] for l in q("SELECT message FROM autotrade_logs WHERE mode='paper' AND level='WARN'")]
check(any("leaves this coin alone" in m for m in warn), "skip reason logged")
r = client.post("/api/autotrade/manual", json={"account": "paper", "symbol": "BTC/USDT", "side": "SHORT", "confirm": True})
check(r.status_code == 200 and r.get_json()["executed"], "manual SHORT on Site demo from the bot page")
pid = q("SELECT id FROM autotrade_positions WHERE mode='paper' AND status='OPEN'")[0]["id"]
r = client.post(f"/api/autotrade/positions/{pid}/close", json={})
check(r.status_code == 200, "closed from the bot page")
check(not q("SELECT * FROM paper_positions WHERE user_id=1 AND symbol='BTC/USDT'"), "demo position is gone too")
row = q("SELECT * FROM autotrade_positions WHERE id=?", (pid,))[0]
check(row["close_reason"] == "MANUAL" and row["pnl_usdt"] is not None, "recorded as MANUAL")

print("\n[5] spot on the demo account")
r = client.post("/api/autotrade/settings", json={"account": "paper", "market_type": "spot", "leverage": 1, "risk_pct": 1,
                                                  "max_position_usdt": 500, "max_open_positions": 2, "daily_loss_limit_pct": 5,
                                                  "min_confidence": 60, "timeframe": "1h", "reward_risk": 1.5,
                                                  "coins": ["SOL/USDT"]})
check(r.status_code == 200, "spot settings saved")
SIGNALS["SOL/USDT"] = ("LONG", 75.0)
cc = Conn(); cc.cursor().execute("UPDATE autotrade_bot_settings SET last_scan_at=NULL"); cc.close()
at._signal_cache.clear()
at.scan_user(1, "paper")
sp = q("SELECT * FROM paper_positions WHERE user_id=1 AND market='spot'")
check(len(sp) == 1 and sp[0]["symbol"] == "SOL/USDT", "spot buy on the demo account")
tp = sp[0]["tp_price"]
set_prices(SOL_USDT=tp + 1)
pt.engine_tick(pt.feed.refresh(max_age=0))
cc = Conn(); cc.cursor().execute("UPDATE autotrade_positions SET opened_at=?", (datetime.utcnow() - timedelta(minutes=5),)); cc.close()
at.monitor_positions()
row = q("SELECT * FROM autotrade_positions WHERE mode='paper' AND symbol='SOL/USDT'")[0]
check(row["status"] == "CLOSED" and row["close_reason"] == "TP" and row["pnl_usdt"] > 0, "spot target hit -> TP with profit")

print("\n[6] panic covers all accounts")
r = client.post("/api/autotrade/panic", json={"account": "all"})
check(r.status_code == 200 and r.get_json()["ok"], "stop all accounts")
check(status("paper")["bot"]["enabled"] is False, "Site demo bot is off")

print("\n[7] assistant: questions get answers, not 'I did not understand'")


def ask(text, mode="paper"):
    r = client.post("/api/autotrade/assistant", json={"account": mode, "message": text, "history": []})
    assert r.status_code == 200, r.data
    return r.get_json()


a = ask("why did the bot not trade?")
check(a["source"] == "built-in" and "OFF" in a["reply"], "why-no-trade explains the bot is off")
client.post("/api/autotrade/bot", json={"account": "paper", "enabled": True})
at.log_event(1, "SCAN", "Scan 1h: SOL: WAIT 41% | BTC: LONG 58% (low confidence)", mode="paper")
a = ask("bot trade kyun nahi kar raha")
check("low confidence" in a["reply"].lower() or "minimum confidence" in a["reply"], "Roman Urdu question reads the last scan")
a = ask("what is leverage?")
check(a["reply"].startswith("Leverage:"), "glossary term explained")
a = ask("what does funding rate mean")
check("Funding" in a["reply"], "another glossary term")
a = ask("How does the bot work?")
check("signal engine v2" in a["reply"], "how the bot works")
a = ask("which settings should I use for safe trading")
check("Risk per trade" in a["reply"] and "0.5%" in a["reply"], "settings advice")
a = ask("what is the price of BTC")
check("BTC/USDT" in a["reply"] and "60,000" in a["reply"], "coin price answer")
a = ask("difference between site demo and live")
check("Site demo" in a["reply"] and "Live" in a["reply"], "accounts explained")
a = ask("asdfgh qwerty")
check("not sure" in a["reply"] and a["suggest"], "unknown question gets help + suggestions")
check(a["ai_available"] is False, "AI off without ANTHROPIC_API_KEY")

# AI path: the request carries the account summary and the answer is returned
sent = {}


class FakeResp:
    status_code = 200

    def json(self):
        return {"content": [{"type": "text", "text": "Aap ka bot abhi ON hai."}]}


def fake_post(url, headers=None, json=None, timeout=None):
    sent.update(url=url, headers=headers, body=json)
    return FakeResp()


assistant.requests = types.SimpleNamespace(post=fake_post)
os.environ["ANTHROPIC_API_KEY"] = "sk-test"
a = ask("mera bot on hai?")
check(a["source"] == "ai" and "ON" in a["reply"], "AI answer used when the key is set")
check(sent["headers"]["x-api-key"] == "sk-test" and sent["body"]["model"] == assistant.DEFAULT_MODEL, "API key + model sent")
check("Bot: ON" in sent["body"]["system"] and "Site demo" in sent["body"]["system"], "account summary sent as context")
check(sent["body"]["messages"][-1]["role"] == "user" and "mera bot on hai?" in sent["body"]["messages"][-1]["content"], "question sent")


class BadResp:
    status_code = 500
    text = "overloaded"


assistant.requests = types.SimpleNamespace(post=lambda *a, **k: BadResp())
a = ask("what is leverage?")
check(a["source"] == "built-in" and a["reply"].startswith("Leverage:"), "falls back to built-in when the AI fails")
os.environ.pop("ANTHROPIC_API_KEY", None)

print(f"\nALL {passed} CHECKS PASSED")
