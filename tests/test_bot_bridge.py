"""Signal -> bot -> order flow with a mocked broker (no network, SQLite shim).

Covers the fix for "the bot does not place the site's signals": every stream's
signal reaches every bot account that wants it, once, with the signal's own
levels; filters, risk rules, retries with backoff, entry checks, the kill
switch, 'Trade this signal', forex on the Site demo account and the OANDA
connector. Run from the project root:  python tests/test_bot_bridge.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from dbshim import setup  # noqa: E402

Conn = setup("bot_bridge")
from cryptography.fernet import Fernet  # noqa: E402

os.environ["AUTOTRADE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ.pop("FOREX_PAPER_BALANCE", None)
os.environ["SIGNAL_STREAMS"] = "all"           # the bridge test uses 1h and forex signals

import autotrade as at  # noqa: E402
import fxbroker  # noqa: E402
import signal_hub as hub  # noqa: E402

at.get_db_connection = Conn
hub.get_db_connection = Conn
at.time.sleep = lambda s: None
fxbroker.time.sleep = lambda s: None
FAILS = []


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# mocked broker (same methods as autotrade.BinanceClient)
# ---------------------------------------------------------------------------
class Broker:
    state = {}

    def __init__(self, key, secret, mode, market_type):
        self.mode, self.market_type = mode, market_type
        self.st = Broker.state

    def has_symbol(self, s):
        return s in self.st["prices"]

    def market_rules(self, s):
        return {"min_amount": 0.00001, "min_cost": 5.0, "taker": 0.001}

    def amount_to_precision(self, s, a):
        return float(int(a * 1e5) / 1e5)

    def price_to_precision(self, s, p):
        return round(p, 6)

    def balance_usdt(self):
        return self.st["usdt"], self.st["usdt"]

    def base_free(self, s):
        return self.st["base"].get(s, 0.0)

    def api_restrictions(self):
        return None

    def price(self, s):
        return self.st["prices"][s]

    def prices(self, syms):
        return {s: self.st["prices"][s] for s in syms if s in self.st["prices"]}

    def prepare_futures(self, s, lev):
        pass

    def futures_positions(self, syms):
        return {s: q for s, q in self.st["fut"].items() if s in syms and q > 0}

    def find_order(self, s, cid):
        """Like BinanceClient.find_order: the order sent with this id, or None when the exchange has no such order."""
        for k, o in enumerate(self.st["orders"]):
            if self.st.setdefault("cids", {}).get(k) == cid:
                return {"id": f"o{k + 1}", "average": self.st["prices"][s], "filled": o[2]}
        return None

    def market_order(self, s, side, amount, reduce_only=False, client_order_id=None):
        if self.st.get("fail_next", 0) > 0:
            self.st["fail_next"] -= 1
            raise RuntimeError("Connection reset by peer (network)")
        lose_reply = self.st.get("lose_reply", 0) > 0
        px = self.st["prices"][s]
        self.st.setdefault("cids", {})[len(self.st["orders"])] = client_order_id
        self.st["orders"].append((s, side, amount, reduce_only))
        if lose_reply:      # the order reached the exchange, but the answer never came back
            self.st["lose_reply"] -= 1
            self.st["base"][s] = self.st["base"].get(s, 0) + (amount if side == "buy" else -amount) if self.market_type == "spot" else 0
            if self.market_type != "spot":
                self.st["fut"][s] = self.st["fut"].get(s, 0) + amount
            raise RuntimeError("Read timed out (network)")
        if self.market_type == "spot":
            self.st["base"][s] = self.st["base"].get(s, 0) + (amount if side == "buy" else -amount)
        elif reduce_only:
            self.st["fut"][s] = max(0.0, self.st["fut"].get(s, 0) - amount)
        else:
            self.st["fut"][s] = self.st["fut"].get(s, 0) + amount
        return {"id": f"o{len(self.st['orders'])}", "average": px, "filled": amount}

    def backup_stop(self, s, side, amount, stop_price):
        return "stop1"

    def order_average(self, oid, s):
        return None

    def cancel_all(self, s):
        pass


def reset_broker():
    Broker.state = {"usdt": 10000.0, "base": {}, "fut": {}, "orders": [], "fail_next": 0,
                    "prices": {"BTC/USDT": 60000.0, "LINK/USDT": 20.0, "SOL/USDT": 150.0, "ETH/USDT": 3000.0}}
    at._client_cache.clear()


reset_broker()
at._client_factory = lambda k, s, m, mt: Broker(k, s, m, mt)

# ---------------------------------------------------------------------------
# database: users, bot tables, signals
# ---------------------------------------------------------------------------
cn = Conn()
cn.cursor().execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
cn.cursor().execute("INSERT INTO users (id, email) VALUES (1, 'a@b.c')")
cn.close()
at.init_tables()
at.init_tables()
hub.init_tables()

NOW = datetime(2026, 10, 14, 12, 0, 30)
at._utcnow = lambda: NOW
hub._utcnow = lambda: NOW
at._hooks.update({"signal_feed": hub.bot_feed, "signal_get": hub.signal_by_id, "available_coins": ["BTC/USDT", "ETH/USDT"]})


def add_signal(symbol, side, tf, entry, stop, grade="B", market="crypto", conf=75.0, bar=None, targets_r=(0.5, 1.0, 2.0)):
    sec = {"15m": 900, "1h": 3600, "4h": 14400}[tf]
    bar = bar or (NOW.replace(second=0, minute=0) - timedelta(seconds=sec))
    risk = abs(entry - stop)
    sgn = 1 if side == "LONG" else -1
    plan = {"targets_r": list(targets_r), "fractions": [1 / 3] * 3, "be_after": 1, "max_bars": 48, "tf_sec": sec,
            "engine": "v4.0"}
    a = {"side": side, "entry": entry, "stop_loss": stop, "take_profit": entry + sgn * targets_r[-1] * risk,
         "targets": [entry + sgn * k * risk for k in targets_r], "confidence": conf, "grade": grade, "bar_time": bar}
    with hub._Cursor() as cur:
        cur.execute("""INSERT INTO signal_events (symbol, side, bar_time, entry, stop_loss, take_profit, confidence, created_at,
                       market, timeframe, grade, targets, plan, status, hits, engine) VALUES
                       (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',0,'v4.0')""",
                    (symbol, side, bar, entry, stop, a["take_profit"], conf, NOW, market, tf, grade,
                     json.dumps(a["targets"]), json.dumps(plan)))
        cur.execute("SELECT id FROM signal_events WHERE symbol=%s AND timeframe=%s AND bar_time=%s", (symbol, tf, bar))
        return cur.fetchone()["id"]


def account(mode, market_type="futures", **settings):
    with at._Cursor() as cur:
        cur.execute("DELETE FROM autotrade_exchange_accounts WHERE user_id=1 AND mode=%s", (mode,))
        if mode != "paper":
            cur.execute("""INSERT INTO autotrade_exchange_accounts (user_id, mode, exchange, api_key_enc, api_secret_enc,
                           key_hint, connected_at) VALUES (1,%s,'binance',%s,%s,'...TEST',%s)""",
                        (mode, at._encrypt("k" * 24), at._encrypt("s" * 24), NOW))
        at._ensure_settings_row(1, mode, cur)
        if mode == "paper":
            at.load_account(1, "paper", cur)          # the Site demo account exists once the bot page was opened
        cur_s = at.load_settings(1, mode, cur)
        base = dict(cur_s, market_type=market_type, leverage=1, risk_pct=1.0, max_position_usdt=5000,
                    max_open_positions=3, daily_loss_limit_pct=5, min_confidence=60)
        base.update(settings)
        clean, err = at.validate_settings(base, cur_s)
        assert not err, err
        at.save_settings(1, mode, clean, cur)
        cur.execute("UPDATE autotrade_bot_settings SET enabled=1, paused_until=NULL WHERE user_id=1 AND mode=%s", (mode,))


def q(sql, params=()):
    with at._Cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# 1. the root cause: a signal on a coin outside the old 5-coin list, 1h stream
# ---------------------------------------------------------------------------
account("demo")
s_link = add_signal("LINK/USDT", "LONG", "1h", 20.0, 19.6)
st = at.load_settings(1, "demo")
check(st["symbols_mode"] == "all" and "1h" in st["timeframes"] and "forex" in st["markets"],
      "new bot accounts take every signal symbol, every timeframe and both markets")
res = at.bridge_step(NOW)
pos = q("SELECT * FROM autotrade_positions WHERE user_id=1 AND mode='demo'")
check(len(pos) == 1 and pos[0]["symbol"] == "LINK/USDT" and pos[0]["signal_id"] == s_link and pos[0]["timeframe"] == "1h",
      "the site's 1h LINK signal is placed automatically (it was never seen by the old 5-coin, 4h-only bot)")
p = pos[0]
risk = abs(p["entry_price"] - p["stop_loss"])
check(abs(risk - 0.4) < 1e-6 and abs(p["tp1"] - 20.2) < 1e-6 and abs(p["tp2"] - 20.4) < 1e-6
      and abs(p["take_profit"] - 20.8) < 1e-6, "the order uses the signal's own stop and targets (0.5R / 1R / 2R)")
equity_risk = 10000 * 0.01
check(abs(p["amount"] * risk - equity_risk) < 1.0, "position size risks 1% of the balance at the stop")
at.bridge_step(NOW)
at.bridge_step(NOW + timedelta(seconds=20))
check(len(q("SELECT * FROM autotrade_positions WHERE mode='demo'")) == 1 and len(Broker.state["orders"]) == 1,
      "no duplicate order for the same signal")
o = q("SELECT * FROM autotrade_signal_orders WHERE signal_id=%s", (s_link,))
check(len(o) == 1 and o[0]["status"] == "placed" and o[0]["position_id"] == p["id"], "signal order recorded as placed")

# ---------------------------------------------------------------------------
# 2. filters
# ---------------------------------------------------------------------------
account("demo", timeframes=["4h"], min_grade="A")
s15 = add_signal("SOL/USDT", "LONG", "15m", 150.0, 148.0, grade="A")
s4c = add_signal("ETH/USDT", "LONG", "4h", 3000.0, 2940.0, grade="C")
at.bridge_step(NOW)
check(len(q("SELECT * FROM autotrade_positions WHERE mode='demo'")) == 1, "timeframe and grade filters respected")
check(not q("SELECT * FROM autotrade_signal_orders WHERE signal_id IN (%s,%s)", (s15, s4c)),
      "filtered signals are not marked, so changing the settings later can still take them")
account("demo", timeframes=["15m", "1h", "4h"], min_grade="C", symbols_mode="list", coins=["ETH/USDT"])
at.bridge_step(NOW)
syms = sorted(r["symbol"] for r in q("SELECT symbol FROM autotrade_positions WHERE mode='demo'"))
check(syms == ["ETH/USDT", "LINK/USDT"], "'only these coins' mode takes only the listed coins")

# ---------------------------------------------------------------------------
# 3. spot cannot short; futures can
# ---------------------------------------------------------------------------
reset_broker()
q("DELETE FROM autotrade_positions")
q("DELETE FROM autotrade_signal_orders")
account("demo", market_type="spot", symbols_mode="all")
s_short = add_signal("BTC/USDT", "SHORT", "1h", 60000.0, 61200.0)
at.bridge_step(NOW)
o = q("SELECT * FROM autotrade_signal_orders WHERE signal_id=%s", (s_short,))
check(o and o[0]["status"] == "skipped" and "spot" in o[0]["reason"], "SHORT on a spot account: skipped with a clear reason")
logs = [r["message"] for r in q("SELECT message FROM autotrade_logs WHERE mode='demo' ORDER BY id")]
check(any("Skipped BTC/USDT 1h SHORT" in m for m in logs), "the skip is written to the activity log")
check(not any(x[0] == "BTC/USDT" for x in Broker.state["orders"]), "no order for the skipped short")

# ---------------------------------------------------------------------------
# 4. max open positions -> wait (retry with backoff), entry while still valid
# ---------------------------------------------------------------------------
reset_broker()
q("DELETE FROM autotrade_positions")
q("DELETE FROM autotrade_signal_orders")
q("DELETE FROM signal_events")
account("demo", market_type="futures", max_open_positions=1)
a1 = add_signal("ETH/USDT", "LONG", "1h", 3000.0, 2970.0)
a2 = add_signal("SOL/USDT", "LONG", "1h", 150.0, 148.5)
at.bridge_step(NOW)
o2 = q("SELECT * FROM autotrade_signal_orders WHERE signal_id=%s", (a2,))
check(len(q("SELECT * FROM autotrade_positions")) == 1 and o2[0]["status"] == "retry" and "Max open positions" in o2[0]["reason"],
      "second signal waits while the account is full")
nt = at._to_dt(o2[0]["next_try_at"])
check(nt == NOW + timedelta(seconds=at.RETRY_BACKOFF_SEC[0]), "retry is scheduled with backoff")
at.bridge_step(NOW + timedelta(seconds=5))
check(q("SELECT attempts FROM autotrade_signal_orders WHERE signal_id=%s", (a2,))[0]["attempts"] == 1,
      "no new attempt before the backoff time")
pos1 = q("SELECT * FROM autotrade_positions")[0]
at.close_position(pos1, at.load_account(1, "demo"), "MANUAL")
at.bridge_step(NOW + timedelta(seconds=25))
check(q("SELECT status FROM autotrade_signal_orders WHERE signal_id=%s", (a2,))[0]["status"] == "placed",
      "after a slot frees up, the waiting signal is placed")

# ---------------------------------------------------------------------------
# 5. entry check: price ran away -> wait, then give up when the window ends
# ---------------------------------------------------------------------------
reset_broker()
q("DELETE FROM autotrade_positions")
q("DELETE FROM autotrade_signal_orders")
q("DELETE FROM signal_events")
account("demo", max_open_positions=3)
Broker.state["prices"]["ETH/USDT"] = 3012.0          # +0.4R from 3000 (stop 2970), TP1 is at +0.5R
d1 = add_signal("ETH/USDT", "LONG", "1h", 3000.0, 2970.0)
at.bridge_step(NOW)
o = q("SELECT * FROM autotrade_signal_orders WHERE signal_id=%s", (d1,))
check(o[0]["status"] == "retry" and "R away from the entry" in o[0]["reason"], "price too far from the entry: not chased")
Broker.state["prices"]["ETH/USDT"] = 3004.0
at.bridge_step(NOW + timedelta(seconds=30))
check(q("SELECT status FROM autotrade_signal_orders WHERE signal_id=%s", (d1,))[0]["status"] == "placed",
      "price came back near the entry within the window: placed")
Broker.state["prices"]["SOL/USDT"] = 152.0
d2 = add_signal("SOL/USDT", "LONG", "1h", 150.0, 148.0)
at.bridge_step(NOW)
later = NOW + timedelta(hours=2, minutes=1)
at._utcnow = lambda: later
hub._utcnow = lambda: later
q("UPDATE autotrade_signal_orders SET next_try_at=%s WHERE signal_id=%s", (NOW, d2))
at.bridge_step(later)
check(not hub.bot_feed(later) or all(r["id"] != d2 for r in hub.bot_feed(later)),
      "signals older than 2 candles are no longer offered to the bot")
at._utcnow = lambda: NOW
hub._utcnow = lambda: NOW
w = at._window_end(hub.signal_by_id(d2))
check(w == datetime(2026, 10, 14, 14, 0), "entry window = 2 candles after the signal")

# ---------------------------------------------------------------------------
# 6. network errors are retried with backoff inside the order call
# ---------------------------------------------------------------------------
reset_broker()
q("DELETE FROM autotrade_positions")
q("DELETE FROM autotrade_signal_orders")
q("DELETE FROM signal_events")
Broker.state["fail_next"] = 2
n1 = add_signal("ETH/USDT", "LONG", "15m", 3000.0, 2985.0)
at.bridge_step(NOW)
check(q("SELECT status FROM autotrade_signal_orders WHERE signal_id=%s", (n1,))[0]["status"] == "placed"
      and len(Broker.state["orders"]) == 1, "two network errors, third try fills (one order, no duplicate)")

# a reply lost AFTER the exchange accepted the order must not produce a second order
reset_broker()
q("DELETE FROM autotrade_positions")
q("DELETE FROM autotrade_signal_orders")
q("DELETE FROM signal_events")
Broker.state["lose_reply"] = 1
at.time.sleep = lambda s: None
n2 = add_signal("ETH/USDT", "LONG", "15m", 3000.0, 2985.0)
at.bridge_step(NOW)
check(q("SELECT status FROM autotrade_signal_orders WHERE signal_id=%s", (n2,))[0]["status"] == "placed"
      and len(Broker.state["orders"]) == 1, "reply lost after the order was accepted: found by its id, still ONE order")
Broker.state["fail_next"] = 5
n2 = add_signal("SOL/USDT", "LONG", "15m", 150.0, 149.0)
at.bridge_step(NOW)
o = q("SELECT * FROM autotrade_signal_orders WHERE signal_id=%s", (n2,))
check(o[0]["status"] == "retry", "a longer outage: the signal is retried on a later pass")

# ---------------------------------------------------------------------------
# 7. daily loss limit, kill switch, live confirmation, Trade this signal
# ---------------------------------------------------------------------------
from flask import Flask  # noqa: E402

app = Flask(__name__)
app.secret_key = "t"
app.register_blueprint(at.autotrade_bp)
cl = app.test_client()
with cl.session_transaction() as ss:
    ss["user_id"] = 1

reset_broker()
q("DELETE FROM autotrade_positions")
q("DELETE FROM autotrade_signal_orders")
q("DELETE FROM signal_events")
t1 = add_signal("ETH/USDT", "LONG", "4h", 3000.0, 2940.0)
r = cl.post("/api/autotrade/trade-signal", json={"account": "demo", "signal_id": t1})
d = r.get_json()
check(r.status_code == 200 and d["executed"] is False and d["stop_loss"] == 2940.0 and not Broker.state["orders"],
      "'Trade this signal' preview: size and levels, no order")
r = cl.post("/api/autotrade/trade-signal", json={"account": "demo", "signal_id": t1, "confirm": True})
check(r.status_code == 200 and r.get_json()["executed"] and len(Broker.state["orders"]) == 1, "'Trade this signal' places the order")
r = cl.post("/api/autotrade/trade-signal", json={"account": "demo", "signal_id": t1, "confirm": True})
check(r.status_code == 400 and "already traded" in r.get_json()["error"], "the same signal cannot be traded twice")
d = cl.get(f"/api/autotrade/signal-status?ids={t1}").get_json()
check(d["signals"][str(t1)][0]["status"] == "placed" and d["signals"][str(t1)][0]["position"]["status"] == "OPEN",
      "signals page sees what the bot did with the signal")
account("live")
t2 = add_signal("SOL/USDT", "LONG", "4h", 150.0, 147.0)
r = cl.post("/api/autotrade/trade-signal", json={"account": "live", "signal_id": t2, "confirm": True})
check(r.status_code == 428 and r.get_json().get("need_ack"), "live trading needs an explicit confirmation")
r = cl.post("/api/autotrade/bot", json={"account": "live", "enabled": True})
check(r.status_code == 428, "turning the live bot on needs the risk confirmation")
q("UPDATE autotrade_bot_settings SET enabled=0 WHERE mode='live'")

r = cl.post("/api/autotrade/panic", json={"account": "demo"})
d = r.get_json()
check(d["ok"] and d["closed"] == 1 and not q("SELECT * FROM autotrade_positions WHERE status='OPEN'")
      and q("SELECT enabled FROM autotrade_bot_settings WHERE mode='demo'")[0]["enabled"] == 0,
      "kill switch: bot off and every position closed")
t3 = add_signal("BTC/USDT", "LONG", "15m", 60000.0, 59700.0)
at.bridge_step(NOW)
check(not q("SELECT * FROM autotrade_signal_orders WHERE signal_id=%s", (t3,)), "a stopped bot takes no new signals")
account("demo")
q("UPDATE autotrade_bot_settings SET day_ref_date=%s, day_ref_balance=10000 WHERE mode='demo'", (NOW.strftime("%Y-%m-%d"),))
q("""INSERT INTO autotrade_positions (user_id, mode, market_type, symbol, side, source, amount, entry_price, stop_loss,
     take_profit, leverage, notional_usdt, status, pnl_usdt, opened_at, closed_at) VALUES
     (1,'demo','futures','XRP/USDT','LONG','BOT',1,1,0.9,1.2,1,1,'CLOSED',-600,%s,%s)""", (NOW, NOW))
at.bridge_step(NOW)
check(not q("SELECT * FROM autotrade_signal_orders WHERE signal_id=%s", (t3,)), "daily loss limit reached: no new trades")
check(q("SELECT pause_reason FROM autotrade_bot_settings WHERE mode='demo'")[0]["pause_reason"] == "Daily loss limit",
      "the bot pauses until the next day")

# ---------------------------------------------------------------------------
# 8. targets: TP1 closes a third and moves the stop to the entry
# ---------------------------------------------------------------------------
reset_broker()
q("DELETE FROM autotrade_positions")
q("DELETE FROM autotrade_signal_orders")
q("DELETE FROM signal_events")
q("UPDATE autotrade_bot_settings SET paused_until=NULL, pause_reason=NULL, day_ref_date=NULL WHERE mode='demo'")
account("demo")
m1 = add_signal("ETH/USDT", "LONG", "1h", 3000.0, 2970.0)
at.bridge_step(NOW)
Broker.state["prices"]["ETH/USDT"] = 3016.0
at.monitor_positions()
p = q("SELECT * FROM autotrade_positions WHERE signal_id=%s", (m1,))[0]
check(p["hits"] == 1 and abs(p["stop_loss"] - 3000.0) < 1e-6 and abs(p["amount"] - p["initial_amount"] * 2 / 3) < 1e-4,
      "TP1: a third closed, the stop moved to the entry")

# ---------------------------------------------------------------------------
# 9. forex on the Site demo account (virtual USD account) + P&L in USD
# ---------------------------------------------------------------------------
FX = {"EUR/USD": 1.10000, "USD/JPY": 150.000}
at._hooks["forex_price"] = lambda s: FX.get(s)
account("paper", market_type="futures")
# the crypto side of the Site demo account is papertrade.py (tested in test_bot_paper.py); set today's reference
# balance so the daily-loss guard does not need it here
q("UPDATE autotrade_bot_settings SET day_ref_date=%s, day_ref_balance=10000 WHERE mode='paper'", (NOW.strftime("%Y-%m-%d"),))
q("UPDATE autotrade_bot_settings SET enabled=0 WHERE mode='demo'")
q("DELETE FROM signal_events")
f1 = add_signal("USD/JPY", "LONG", "1h", 150.000, 149.700, market="forex")
at.bridge_step(NOW)
fp = q("SELECT * FROM autotrade_positions WHERE signal_id=%s", (f1,))
check(len(fp) == 1 and fp[0]["market_type"] == "forex" and fp[0]["mode"] == "paper", "forex signal placed on the Site demo account")
fp = fp[0]
stop_dist = fp["entry_price"] - fp["stop_loss"]
loss_usd = fp["amount"] * stop_dist / fp["entry_price"]
check(abs(loss_usd - 100.0) < 1.5, f"forex size risks 1% of the 10,000 USD demo balance ({loss_usd:.2f} USD at the stop)")
check(abs(fp["fx_rate"] - 1 / 150.0) < 1e-9, "JPY results are converted to USD")
FX["USD/JPY"] = fp["stop_loss"] - 0.01
at.monitor_positions()
fp2 = q("SELECT * FROM autotrade_positions WHERE id=%s", (fp["id"],))[0]
check(fp2["status"] == "CLOSED" and -110 < fp2["pnl_usdt"] < -95, f"stop hit: loss about 1% in USD ({fp2['pnl_usdt']:.2f})")
free, eq = fxbroker.FxPaperClient(1, lambda s: FX.get(s), at._Cursor).balance_usdt()
check(abs(eq - (10000 + fp2["pnl_usdt"])) < 0.01, "the forex demo balance includes the realized result")
check(fxbroker.fx_rate("EUR/JPY", lambda s: {"USD/JPY": 150.0}.get(s)) == 1 / 150.0 and
      fxbroker.fx_rate("EUR/GBP", lambda s: {"GBP/USD": 1.25}.get(s)) == 1.25 and fxbroker.fx_rate("EUR/USD", lambda s: 1.1) == 1.0,
      "quote-currency conversion for crosses")
r = cl.post("/api/autotrade/trade-signal", json={"account": "demo", "signal_id": f1})
check(r.status_code == 400 and ("no longer active" in r.get_json()["error"] or "Binance has no forex" in r.get_json()["error"]
                                or "already" in r.get_json()["error"]), "forex cannot go to a Binance account")


# ---------------------------------------------------------------------------
# 10. OANDA connector (mocked HTTP)
# ---------------------------------------------------------------------------
class Resp:
    def __init__(self, code, data):
        self.status_code, self._d = code, data

    def json(self):
        return self._d


class FakeOanda:
    def __init__(self):
        self.calls = []
        self.fail = 0
        self.trades = []

    def request(self, method, url, json=None, params=None, headers=None, timeout=None):
        self.calls.append((method, url.split("/v3")[1], json, params))
        assert headers["Authorization"] == "Bearer tok-" + "x" * 30
        if self.fail:
            self.fail -= 1
            return Resp(503, {"errorMessage": "busy"})
        path = url.split("/v3")[1]
        if path.endswith("/summary"):
            return Resp(200, {"account": {"NAV": "5000.0", "marginAvailable": "4800.0", "balance": "5000"}})
        if path.endswith("/pricing"):
            return Resp(200, {"prices": [{"instrument": "EUR_USD", "bids": [{"price": "1.09990"}], "asks": [{"price": "1.10010"}]}]})
        if path.endswith("/orders") and method == "POST":
            self.trades.append({"id": "77", "instrument": json["order"]["instrument"], "currentUnits": json["order"]["units"]})
            return Resp(201, {"orderFillTransaction": {"id": "78", "price": "1.10010", "units": json["order"]["units"],
                                                       "tradeOpened": {"tradeID": "77"}}})
        if path.endswith("/openTrades"):
            return Resp(200, {"trades": self.trades})
        if "/trades/77/orders" in path:
            return Resp(200, {"stopLossOrderTransaction": {"id": "79"}})
        if "/trades/77/close" in path:
            self.trades = []
            return Resp(200, {"orderFillTransaction": {"id": "80", "price": "1.10500"}})
        return Resp(404, {"errorMessage": "not found"})


fo = FakeOanda()
oc = fxbroker.OandaClient("tok-" + "x" * 30, "101-004-1234567-001", "practice", session=fo)
fo.fail = 2
check(oc.balance_usdt() == (4800.0, 5000.0), "OANDA balance (retried twice on 503)")
check(abs(oc.price("EUR/USD") - 1.1) < 1e-9, "OANDA mid price")
o = oc.market_order("EUR/USD", "sell", 1000)
body = [c for c in fo.calls if c[0] == "POST"][-1][2]
check(body["order"]["units"] == "-1000" and body["order"]["instrument"] == "EUR_USD" and o["id"] == "77",
      "OANDA sell order: negative units on the right instrument")
oc.set_stop("77", "EUR/USD", 1.10512)
put = [c for c in fo.calls if c[0] == "PUT"][-1]
check(put[1].endswith("/trades/77/orders") and put[2]["stopLoss"]["price"] == "1.10512", "stop-loss attached at OANDA")
check(oc.futures_positions(["EUR/USD"]) == {"EUR/USD": 1000.0}, "open trades synced from OANDA")
c = oc.market_order("EUR/USD", "buy", 1000, reduce_only=True)
check(c["average"] == 1.105 and oc.futures_positions(["EUR/USD"]) == {"EUR/USD": 0.0}, "close through the trade endpoint")
check(oc.api_restrictions()["enableWithdrawals"] is False, "the OANDA API cannot withdraw")
fo.fail = 5
try:
    oc.balance_usdt()
    check(False, "a lasting OANDA outage raises a clear error")
except RuntimeError as e:
    check("OANDA" in str(e), "a lasting OANDA outage raises a clear error")
fo.fail = 0
at._oanda_factory = lambda token, acc, env: fxbroker.OandaClient(token, acc, env, session=FakeOanda())
r = cl.post("/api/autotrade/connect", json={"account": "oanda_demo", "api_key": "tok-" + "x" * 30, "account_id": "bad"})
check(r.status_code == 400, "OANDA connect: account id format checked")
r = cl.post("/api/autotrade/connect", json={"account": "oanda_demo", "api_key": "tok-" + "x" * 30,
                                             "account_id": "101-004-1234567-001"})
check(r.status_code == 200 and r.get_json()["balance_total_usdt"] == 5000.0, "OANDA demo connected and verified")
row = q("SELECT * FROM autotrade_exchange_accounts WHERE mode='oanda_demo'")[0]
check("tok-" not in row["api_key_enc"] and at._decrypt(row["api_key_enc"]).startswith("tok-"), "OANDA token stored encrypted")
st = cl.get("/api/autotrade/status?account=oanda_demo").get_json()
check(st["ok"] and st["settings"]["markets"] == ["forex"] and st["accounts"]["oanda_demo"]["connected"],
      "OANDA account defaults to forex signals")
check("tok-" not in json.dumps(st), "the token never reaches the browser")

print("\n[untested streams] live accounts copy only tested streams")
base_set = dict(at.DEFAULT_SETTINGS)
base_set.update(markets=["crypto", "forex"], timeframes=["15m", "1h", "4h"], min_grade="C", symbols_mode="all", min_confidence=65.0)
untested_row = {"id": 1, "symbol": "EUR/USD", "market": "forex", "timeframe": "1h", "grade": "B", "tested": False}
tested_row = dict(untested_row, symbol="BTC/USDT", market="crypto", timeframe="4h", tested=True)
check(at.signal_wanted(base_set, untested_row, "live") == (False, "untested stream: live accounts copy only tested streams"),
      "Binance live: an untested stream is not copied")
check(not at.signal_wanted(base_set, untested_row, "oanda_live")[0], "OANDA live: an untested stream is not copied")
check(at.signal_wanted(base_set, untested_row, "demo")[0] and at.signal_wanted(base_set, untested_row, "oanda_demo")[0]
      and at.signal_wanted(base_set, untested_row, "paper")[0], "demo accounts copy the untested stream (to watch it risk-free)")
check(at.signal_wanted(base_set, tested_row, "live")[0], "Binance live: a tested stream is copied as before")
check(at.signal_wanted(base_set, {k: v for k, v in untested_row.items() if k != "tested"}, "live")[0],
      "a signal row without the flag (older data) behaves as before")
check(at.signal_wanted(base_set, untested_row)[0], "no account mode given: unchanged behaviour")

print()
print(f"{len(FAILS)} failed" if FAILS else "ALL PASSED")
sys.exit(1 if FAILS else 0)
