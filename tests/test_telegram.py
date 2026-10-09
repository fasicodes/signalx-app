"""Tests for telegram_bot.py with a fake Telegram API and a SQLite shim (no network, no MySQL).
Run from the project root:  python tests/test_telegram.py
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta
import warnings
warnings.simplefilter("ignore", DeprecationWarning)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pm = types.ModuleType("pymysql")
pm.cursors = types.ModuleType("pymysql.cursors")
pm.cursors.DictCursor = object
pm.connect = lambda **kw: None
sys.modules.setdefault("pymysql", pm)
sys.modules.setdefault("pymysql.cursors", pm.cursors)

sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))
DB_PATH = os.path.join(tempfile.gettempdir(), "telegram_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)


def _translate(sql):
    s = sql.strip()
    if s.upper().startswith("CREATE TABLE"):
        s = s.replace("INT PRIMARY KEY AUTO_INCREMENT", "INTEGER PRIMARY KEY AUTOINCREMENT")
    return s.replace("%s", "?")


class Cur:
    def __init__(self, conn):
        self.c = conn.cursor()

    def execute(self, sql, params=()):
        t = _translate(sql)
        self.c.execute(t, tuple(params or ()) if "?" in t else ())

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


# ---- fake Telegram Bot API (via a fake `requests` module)
CALLS = []
TG = {"blocked": set(), "webhook": "", "fail": False}


class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


def fake_post(url, json=None, timeout=None):
    method = url.rsplit("/", 1)[1]
    CALLS.append((method, json or {}))
    if TG["fail"]:
        return FakeResp({"ok": False, "error_code": 500, "description": "Internal"}, 500)
    if method == "getMe":
        return FakeResp({"ok": True, "result": {"username": "SignalsFMBot"}})
    if method == "getWebhookInfo":
        return FakeResp({"ok": True, "result": {"url": TG["webhook"]}})
    if method == "setWebhook":
        TG["webhook"] = json["url"]
        return FakeResp({"ok": True, "result": True})
    if method == "sendMessage":
        if json["chat_id"] in TG["blocked"]:
            return FakeResp({"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}, 403)
        return FakeResp({"ok": True, "result": {"message_id": len(CALLS)}})
    return FakeResp({"ok": True, "result": True})


req = types.ModuleType("requests")
req.post = fake_post
sys.modules["requests"] = req

os.environ["TELEGRAM_BOT_TOKEN"] = "123456789:AAH-fake_token_for_tests_1234567890"
os.environ["SITE_URL"] = "https://signals.example"
os.environ.pop("TELEGRAM_CHANNEL_ID", None)

from flask import Flask  # noqa: E402
import telegram_bot as tg  # noqa: E402
import signalboard as sb  # noqa: E402

tg.get_db_connection = Conn
sb.get_db_connection = Conn
tg.time.sleep = lambda s: None
tg._state["sync"] = True
passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


c0 = Conn().cursor()
c0.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
c0.execute("""CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, category TEXT, symbol TEXT,
              title TEXT, message TEXT, is_read INT DEFAULT 0, alert_id INT, created_at TEXT)""")
for i, e in ((1, "a@x.com"), (2, "b@x.com"), (3, "c@x.com")):
    c0.execute("INSERT INTO users (id, email) VALUES (?, ?)", (i, e))
sb.init_tables()

BOARD = {"rows": []}
app = Flask(__name__)
app.secret_key = "t"
app.register_blueprint(tg.telegram_bp)
check(tg.init_telegram(app, board_rows=lambda: (BOARD["rows"], None, None), start=False) is True, "turns on with a valid token")
client = app.test_client()


def sent_to(chat_id):
    return [p["text"] for m, p in CALLS if m == "sendMessage" and p.get("chat_id") == chat_id]


def update(chat_id, text, username="ali", chat_type="private"):
    return {"update_id": 1, "message": {"message_id": 5, "chat": {"id": chat_id, "type": chat_type},
                                        "from": {"id": chat_id, "username": username}, "text": text}}


def hook(upd, secret=None):
    h = {"X-Telegram-Bot-Api-Secret-Token": tg.webhook_secret() if secret is None else secret}
    return client.post("/telegram/webhook", data=json.dumps(upd), content_type="application/json", headers=h)


def login(uid):
    with client.session_transaction() as s:
        if uid:
            s["user_id"] = uid
        else:
            s.clear()


print("\n[1] config")
check(tg.enabled() and tg.site_base() == "https://signals.example", "token + site address read from the environment")
os.environ["SITE_URL"] = "http://not-https.example"
os.environ["RAILWAY_PUBLIC_DOMAIN"] = "app.up.railway.app"
check(tg.site_base() == "https://app.up.railway.app", "falls back to Railway's public domain (https only)")
os.environ["SITE_URL"] = "https://signals.example"
saved = os.environ["TELEGRAM_BOT_TOKEN"]
os.environ["TELEGRAM_BOT_TOKEN"] = "not a token"
check(not tg.enabled(), "a malformed token keeps Telegram off")
os.environ["TELEGRAM_BOT_TOKEN"] = saved
check(len(tg.webhook_secret()) == 48 and tg.webhook_secret() == tg.webhook_secret(), "webhook secret is stable")
check(tg.ensure_webhook() == "webhook" and TG["webhook"] == "https://signals.example/telegram/webhook", "webhook registered")
n = len(CALLS)
tg.ensure_webhook()
check(not any(m == "setWebhook" for m, _ in CALLS[n:]), "webhook not set again when it is already right")
os.environ["TELEGRAM_CHANNEL_ID"] = "bad channel!"
check(tg.channel_id() == "", "bad channel id ignored")
os.environ["TELEGRAM_CHANNEL_ID"] = "@signalsfm"
check(tg.channel_id() == "@signalsfm", "channel id accepted")
os.environ.pop("TELEGRAM_CHANNEL_ID")

print("\n[2] website API needs login")
login(None)
check(client.get("/api/telegram/status").status_code == 401 and client.post("/api/telegram/link").status_code == 401, "401 without login")
login(1)
st = client.get("/api/telegram/status").get_json()
check(st["available"] and not st["linked"] and st["bot"] == "SignalsFMBot", "status: available, not linked, bot name from getMe")
r = client.post("/api/telegram/link").get_json()
m = re.fullmatch(r"https://t\.me/SignalsFMBot\?start=([A-Za-z0-9_-]{16,40})", r["url"])
check(r["ok"] and m is not None, "connect link is a t.me deep link with a one-time code")
code1 = m.group(1)

print("\n[3] webhook security")
check(hook(update(555, f"/start {code1}"), secret="wrong").status_code == 403, "wrong secret header -> 403")
check(hook(update(555, f"/start {code1}"), secret="").status_code == 403, "missing secret header -> 403")
cur = Conn().cursor(); cur.execute("SELECT COUNT(*) AS n FROM telegram_links"); n_links = cur.fetchone()["n"]
check(n_links == 0, "nothing linked by a forged request")

print("\n[4] linking")
check(hook(update(-100, f"/start {code1}", chat_type="group")).status_code == 200 and not sent_to(-100), "group chats are ignored")
check(hook(update(555, f"/start {code1}")).status_code == 200, "Start with the code -> 200")
st = client.get("/api/telegram/status").get_json()
check(st["linked"] and st["enabled"] and st["username"] == "ali", "account linked, alerts enabled, username saved")
check("Connected to Signals FM" in sent_to(555)[-1], "bot confirms in the chat")
cur = Conn().cursor(); cur.execute("SELECT * FROM signal_alert_subs WHERE user_id=1"); sub = cur.fetchone()
check(sub and sub["enabled"] == 1 and sub["coins"] == "ALL", "connecting switches signal alerts on (all coins by default)")
hook(update(555, f"/start {code1}"))
check("expired or was already used" in sent_to(555)[-1], "a code works only once")
login(2)
code2 = client.post("/api/telegram/link").get_json()["url"].split("start=")[1]
cur = Conn().cursor()
cur.execute("UPDATE telegram_link_codes SET expires_at=? WHERE code=?", (datetime.utcnow() - timedelta(minutes=1), code2))
hook(update(777, f"/start {code2}", username="bilal"))
check("expired" in sent_to(777)[-1] and not client.get("/api/telegram/status").get_json()["linked"], "an expired code does not link")
code2 = client.post("/api/telegram/link").get_json()["url"].split("start=")[1]
cur = Conn().cursor(); cur.execute("SELECT COUNT(*) AS n FROM telegram_link_codes WHERE user_id=2"); n_codes = cur.fetchone()["n"]
check(n_codes == 1, "a new code replaces the user's old codes")
hook(update(777, f"/start {code2}", username="bilal"))
check(client.get("/api/telegram/status").get_json()["linked"], "second user linked")
cur = Conn().cursor()
cur.execute("INSERT INTO signal_alert_subs (user_id, enabled, email, coins, emails_sent, updated_at) VALUES (3,1,0,'ALL',0,?)", (datetime.utcnow(),))
login(3)
code3 = client.post("/api/telegram/link").get_json()["url"].split("start=")[1]
hook(update(777, f"/start {code3}", username="bilal"))
cur = Conn().cursor(); cur.execute("SELECT user_id FROM telegram_links WHERE chat_id=777"); owners = cur.fetchall()
check([o["user_id"] for o in owners] == [3], "one chat belongs to one account (re-linking moves it)")
cur.execute("DELETE FROM telegram_links WHERE user_id=3")
hook(update(777, f"/start {code2}"))
login(2)
code2 = client.post("/api/telegram/link").get_json()["url"].split("start=")[1]
hook(update(777, f"/start {code2}", username="bilal"))

print("\n[5] commands")
hook(update(999, "/signals"))
check("not connected" in sent_to(999)[-1], "unknown chat: explains how to connect")
hook(update(555, "/help"))
check("/signals" in sent_to(555)[-1] and "/stop" in sent_to(555)[-1], "/help lists the commands")
BOARD["rows"] = []
hook(update(555, "/signals"))
check("still loading" in sent_to(555)[-1], "/signals while the board loads")
BOARD["rows"] = [{"symbol": "SOL/USDT", "state": "WAIT"}]
hook(update(555, "/signals"))
check("No active signals" in sent_to(555)[-1], "/signals with nothing active")
BOARD["rows"] = [{"symbol": "BTC/USDT", "state": "ACTIVE", "fresh": True,
                  "active": {"side": "LONG", "entry": 63120.5, "stop_loss": 60000.0, "take_profit": 64700.0},
                  "progress": {"pct": 42.5}},
                 {"symbol": "ETH/USDT", "state": "ACTIVE", "fresh": False,
                  "active": {"side": "SHORT", "entry": 2500.0, "stop_loss": 2600.0, "take_profit": 2450.0},
                  "progress": {"pct": -12.0}}]
hook(update(555, "/signals"))
txt = sent_to(555)[-1]
check("Active signals (2)" in txt and "BTC</b> Long" in txt and "42.5% to target" in txt and "12% to stop" in txt and "<b>new</b>" in txt,
      "/signals lists active signals with progress")
check("https://signals.example/signals" in txt, "with a link to the board")
hook(update(555, "/stop"))
check("Alerts are paused" in sent_to(555)[-1], "/stop answered in the chat")
login(1)
check(client.get("/api/telegram/status").get_json()["enabled"] is False, "/stop pauses alerts")
hook(update(555, "/resume"))
check(client.get("/api/telegram/status").get_json()["enabled"] is True, "/resume turns them back on")
hook(update(555, "hello there"))
check("/signals" in sent_to(555)[-1], "plain text gets the help list")

print("\n[6] alerts on new signals")
ev_btc = {"symbol": "BTC/USDT", "side": "LONG", "entry": 63120.5, "stop_loss": 60000.0, "take_profit": 64700.0, "confidence": 73.4}
ev_eth = {"symbol": "ETH/USDT", "side": "SHORT", "entry": 2500.0, "stop_loss": 2600.0, "take_profit": 2450.0, "confidence": 71.0}
cur = Conn().cursor()
cur.execute("UPDATE signal_alert_subs SET coins=? WHERE user_id=2", (json.dumps(["ETH/USDT"]),))
sb.init_signalboard(engine_signal=None, telegram=tg.notify_users, start=False)
n = len(CALLS)
sb.notify([ev_btc, ev_eth])
msgs = {p["chat_id"]: p["text"] for m, p in CALLS[n:] if m == "sendMessage"}
check(set(msgs) == {555, 777}, "both linked users get a message")
check("2 new signals" in msgs[555] and "BTC/USDT" in msgs[555] and "ETH/USDT" in msgs[555], "user 1 (all coins): one message with both")
check("BTC/USDT" not in msgs[777] and "New Short signal: ETH/USDT" in msgs[777], "user 2 gets only the coin they chose")
check("Entry: <b>63,120.50</b>" in msgs[555] and "(-4.94%)" in msgs[555] and "(+2.50%)" in msgs[555] and "Win chance: 73%" in msgs[555],
      "message shows entry, stop/target distance and win chance")
check('href="https://signals.example/?coin=BTC%2FUSDT"' in msgs[555] and "Not financial advice" in msgs[555], "link to the coin + risk note")
cur = Conn().cursor(); cur.execute("SELECT user_id FROM notifications WHERE category='SIGNAL'"); bell = cur.fetchall()
check(len(bell) == 5, "bell notifications still created for everyone with alerts on (2 + 1 + 2)")
login(2)
client.post("/api/telegram/toggle", data=json.dumps({"enabled": False}), content_type="application/json")
n = len(CALLS)
sb.notify([ev_eth])
check([p["chat_id"] for m, p in CALLS[n:] if m == "sendMessage"] == [555], "a user who switched Telegram off gets nothing")
client.post("/api/telegram/toggle", data=json.dumps({"enabled": True}), content_type="application/json")
cur = Conn().cursor(); cur.execute("UPDATE signal_alert_subs SET enabled=0 WHERE user_id=2")
n = len(CALLS)
sb.notify([ev_eth])
check([p["chat_id"] for m, p in CALLS[n:] if m == "sendMessage"] == [555], "alerts switched off on the website -> no Telegram either")
cur.execute("UPDATE signal_alert_subs SET enabled=1 WHERE user_id=2")
today = datetime.utcnow().strftime("%Y-%m-%d")
cur.execute("UPDATE telegram_links SET sent_day=?, sent_count=? WHERE user_id=1", (today, tg.MAX_PER_DAY))
n = len(CALLS)
sb.notify([ev_btc, ev_eth])
check([p["chat_id"] for m, p in CALLS[n:] if m == "sendMessage"] == [777], "daily cap per user")
cur.execute("UPDATE telegram_links SET sent_day=NULL, sent_count=0 WHERE user_id=1")
TG["blocked"].add(777)
sb.notify([ev_eth])
cur.execute("SELECT enabled FROM telegram_links WHERE user_id=2")
check(cur.fetchone()["enabled"] == 0, "user blocked the bot -> their Telegram alerts are switched off")
TG["blocked"].clear()
os.environ["TELEGRAM_CHANNEL_ID"] = "@signalsfm"
n = len(CALLS)
sb.notify([ev_btc])
check(any(p["chat_id"] == "@signalsfm" and "BTC/USDT" in p["text"] for m, p in CALLS[n:] if m == "sendMessage"), "new signals also posted to the channel")
cur.execute("UPDATE signal_alert_subs SET enabled=0")
n = len(CALLS)
sb.notify([ev_eth])
check([p["chat_id"] for m, p in CALLS[n:] if m == "sendMessage"] == ["@signalsfm"], "channel posts even when no user has alerts on")
cur.execute("UPDATE signal_alert_subs SET enabled=1")
os.environ.pop("TELEGRAM_CHANNEL_ID")
TG["fail"] = True
sb.notify([ev_btc])
TG["fail"] = False
check(True, "Telegram down: alerts fail quietly, the board keeps working")

print("\n[7] test message, unlink")
login(1)
r = client.post("/api/telegram/test")
check(r.status_code == 200 and "Test message" in sent_to(555)[-1], "test message")
check(client.post("/api/telegram/test").status_code == 429, "test messages are rate-limited")
r = client.post("/api/telegram/unlink").get_json()
check(r["ok"] and not client.get("/api/telegram/status").get_json()["linked"] and "disconnected" in sent_to(555)[-1], "disconnect from the website")
hook(update(777, "/unlink"))
login(2)
check(not client.get("/api/telegram/status").get_json()["linked"], "/unlink from the chat")
check(client.post("/api/telegram/test").status_code == 400, "test needs a connected chat")

print("\n[8] without a token")
os.environ.pop("TELEGRAM_BOT_TOKEN")
check(client.get("/api/telegram/status").get_json()["available"] is False, "status says not available")
check(client.post("/api/telegram/link").status_code == 503, "link refused with a clear message")
check(hook(update(555, "/help")).status_code == 404, "webhook off")
check(tg.notify_users({1: [ev_btc]}, [ev_btc]) == 0, "no alerts sent")

print(f"\nALL {passed} CHECKS PASSED")
