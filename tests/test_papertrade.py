"""Tests for papertrade.py (Demo Trading) with a fake price feed and a SQLite
shim - no network, no MySQL needed. Run from the project root:

    python tests/test_papertrade.py
"""
import math
import os
import re
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --- stub pymysql so db.py imports -------------------------------------------
pm = types.ModuleType("pymysql")
pm.cursors = types.ModuleType("pymysql.cursors")
pm.cursors.DictCursor = object
pm.connect = lambda **kw: None
sys.modules.setdefault("pymysql", pm)
sys.modules.setdefault("pymysql.cursors", pm.cursors)

sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))
DB_PATH = os.path.join(tempfile.gettempdir(), "papertrade_test.db")
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

pt.get_db_connection = Conn


# --- fake exchange --------------------------------------------------------------
class FakeExchange:
    precisionMode = 4  # TICK_SIZE

    def __init__(self):
        self.prices = {}
        self.spread = 0.0
        self.markets = {}

    def load_markets(self):
        self.markets = {
            s: {"precision": {"price": 0.01 if s != "XRP/USDT" else 0.0001, "amount": 0.0001},
                "limits": {"amount": {"min": 0.0001}}}
            for s in ("BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT")
        }
        return self.markets

    def fetch_tickers(self, symbols=None):
        out = {}
        for s, p in self.prices.items():
            h = p * self.spread / 2
            out[s] = {"last": p, "bid": p - h, "ask": p + h, "high": p * 1.02, "low": p * 0.98,
                      "percentage": 1.5, "quoteVolume": 1e9}
        return out


EX = FakeExchange()
pt._hooks["exchange"] = EX
pt._hooks["available_coins"] = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT", "EUR/USD", "GRAM/USDT"]


def set_prices(**kw):
    for k, v in kw.items():
        EX.prices[k.replace("_", "/")] = float(v)
    pt.feed._fetched_at = 0.0  # force refresh


def prices():
    return pt.feed.refresh(max_age=0)


from flask import Flask  # noqa: E402

app = Flask(__name__)
app.secret_key = "test"
app.register_blueprint(pt.paper_bp)

c = Conn()
cu = c.cursor()
cu.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
cu.execute("INSERT INTO users (id, email) VALUES (1,'a@b.c'), (2,'b@b.c'), (3,'c@c.c')")
c.close()
pt.init_tables()
pt.init_tables()

client = app.test_client()
passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


def close(a, b, tol=1e-6):
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))


def q(sql, params=()):
    c = Conn(); cu = c.cursor(); cu.execute(sql, params)
    try:
        rows = cu.fetchall()
    except Exception:
        rows = []
    c.close()
    return rows


def login(uid):
    with client.session_transaction() as s:
        s["user_id"] = uid


def post(path, body=None):
    return client.post(path, json=body if body is not None else {})


def state():
    return client.get("/api/paper/state").get_json()


def order(**kw):
    body = {"market": "futures", "type": "MARKET"}
    body.update(kw)
    return post("/api/paper/orders", body)


def preview(**kw):
    body = {"market": "futures", "type": "MARKET"}
    body.update(kw)
    return post("/api/paper/preview", body).get_json()["plan"]


def tick():
    pt.feed._fetched_at = 0.0
    pt.engine_tick(prices())


def wallet_from_ledger(uid):
    acct = q("SELECT * FROM paper_accounts WHERE user_id=?", (uid,))[0]
    rows = q("SELECT amount FROM paper_ledger WHERE user_id=? AND sess=?", (uid, acct["sess"]))
    return sum(r["amount"] for r in rows), acct["wallet"]


set_prices(BTC_USDT=60000, ETH_USDT=3000, SOL_USDT=150, XRP_USDT=0.6, DOGE_USDT=0.2)

print("\n[1] pure math")
check(pt.mmr_for(10_000) == 0.004 and pt.mmr_for(100_000) == 0.005 and pt.mmr_for(30_000_000) == 0.05, "maintenance brackets")
check(pt.max_notional_for(125, "BTC/USDT") == 50_000 and pt.max_notional_for(50, "BTC/USDT") == 250_000
      and pt.max_notional_for(10, "SOL/USDT") == 5_000_000 and pt.max_notional_for(3, "BTC/USDT") == 20_000_000,
      "max position per leverage")
check(pt.max_notional_for(75, "SOL/USDT") == 50_000 and pt.max_notional_for(80, "SOL/USDT") == 0, "symbol leverage cap")
L = pt.liq_price_isolated("LONG", 100.0, 1.0, 20.0, 0.004)
check(close(L, 80 / 0.996), "isolated long liquidation price")
check(close(pt.upnl("LONG", 100, 80 / 0.996, 1) + 20, 0.004 * 80 / 0.996), "at liq price, margin + pnl == maintenance")
S = pt.liq_price_isolated("SHORT", 100.0, 1.0, 10.0, 0.004)
check(close(S, 110 / 1.004), "isolated short liquidation price")
check(pt.liq_price_isolated("LONG", 100, 1, 100, 0.004) is None, "1x long has no liquidation price")
t = {"last": 100.0, "bid": 99.9, "ask": 100.1}
check(close(pt.market_fill_price("BUY", t, 1), 100.1 * (1 + 100.1 / 1e6 * 0.0005)), "market buy fills at ask + impact")
check(pt.market_fill_price("SELL", {"last": 100.0}, 1) < 100, "no bid/ask -> synthetic spread")
check(pt._floor_step(0.123456, 0.0001) == 0.1234 and pt._round_tick(123.456, 0.01) == 123.46, "rounding to lot/tick")

print("\n[2] account + bootstrap")
r = client.get("/api/paper/state")
check(r.status_code == 401, "login required")
login(1)
d = client.get("/api/paper/bootstrap").get_json()
syms = [c["symbol"] for c in d["coins"]]
check("EUR/USD" not in syms and "GRAM/USDT" not in syms and "BTC/USDT" in syms, "only tradable USDT coins listed")
check(d["options"]["fees"]["futures"]["taker"] == 0.05, "fees exposed in %")
st = state()
check(st["account"]["wallet"] == 10000 and st["account"]["available"] == 10000 and st["account"]["session"] == 1,
      "new account starts with 10,000 USDT")
tk = client.get("/api/paper/tickers").get_json()
check(len(tk["tickers"]) == 5, "tickers for tradable coins")

print("\n[3] validation + preview")
p = preview(symbol="BTC/USDT", side="BUY", qty=0.00001)
check(any("below the minimum" in e for e in p["errors"]), "below min qty rejected")
p = preview(symbol="DOGE/USDT", side="BUY", qty=10)
check(any("at least 5 USDT" in e for e in p["errors"]), "below 5 USDT notional rejected")
p = preview(symbol="BTC/USDT", side="BUY", qty=10)
check(any("Insufficient available balance" in e for e in p["errors"]), "insufficient balance")
p = preview(symbol="BTC/USDT", side="BUY", qty=0.1, sl_price=59000, tp_price=63000)
check(not p["errors"] and p["effect"] == "open" and p["leverage"] == 5 and p["margin_mode"] == "isolated",
      "default 5x isolated, opens a position")
check(close(p["margin_required"], 0.1 * p["est_price"] / 5), "margin = notional / leverage")
check(close(p["fee"], 0.1 * p["est_price"] * 0.0005), "taker fee 0.05%")
check(p["liq_price"] and 48000 < p["liq_price"] < 49000, f"liquidation estimate ~48.2k ({p['liq_price']:.0f})")
check(p["rr"] and 2.8 < p["rr"] < 3.1 and p["sl"]["pnl"] < 0 < p["tp"]["pnl"], "R:R + PnL at TP/SL")
check(close(p["risk_pct_equity"], -p["sl"]["pnl"] / 10000 * 100), "risk as % of equity")
p = preview(symbol="BTC/USDT", side="BUY", qty=0.1, tp_price=59000)
check(any("Take-profit must be above" in e for e in p["errors"]), "TP on wrong side rejected")
p = preview(symbol="BTC/USDT", side="BUY", qty=0.1)
check(any(w["code"] == "no_sl" for w in p["warnings"]), "warns when no stop-loss")
p = preview(symbol="BTC/USDT", side="BUY", qty=0.5, sl_price=55000)
check(any(w["code"] == "risk_high" for w in p["warnings"]), "warns when risk > 5% of account")
p = preview(symbol="BTC/USDT", side="BUY", qty=0.1, sl_price=59000, tp_price=60300)
check(any(w["code"] == "rr_low" for w in p["warnings"]), "warns on reward:risk below 1")
p = preview(symbol="BTC/USDT", side="SELL", qty=0.1, reduce_only=True)
check(any("Reduce-only" in e for e in p["errors"]), "reduce-only with no position rejected")
p = preview(symbol="BTC/USDT", side="BUY", type="LIMIT", qty=0.1, price=60500, post_only=True)
check(any("Post-only" in e for e in p["errors"]), "marketable post-only rejected")
p = preview(symbol="BTC/USDT", side="BUY", type="STOP_MARKET", qty=0.1, trigger_price=61000)
check(not p["errors"] and p["trigger_dir"] == "UP", "stop buy above price triggers upward")
p = preview(symbol="BTC/USDT", side="SELL", type="TRAILING_STOP", qty=0.1, callback_pct=20)
check(any("Callback rate" in e for e in p["errors"]), "callback rate range enforced")
p = preview(market="spot", symbol="ETH/USDT", side="SELL", qty=1)
check(any("can't short" in e for e in p["errors"]), "spot cannot short")
r = order(symbol="EUR/USD", side="BUY", qty=1)
check(r.status_code == 400, "non-tradable symbol rejected")

print("\n[4] leverage settings + brackets")
r = post("/api/paper/leverage", {"symbol": "BTC/USDT", "leverage": 150, "margin_mode": "isolated"})
check(r.status_code == 400, "leverage above 125x rejected")
r = post("/api/paper/leverage", {"symbol": "BTC/USDT", "leverage": 125, "margin_mode": "isolated"})
check(r.status_code == 200 and r.get_json()["max_notional"] == 50000, "125x allowed with 50k cap")
p = preview(symbol="BTC/USDT", side="BUY", qty=0.9)
check(any("maximum position size is 50,000" in e for e in p["errors"]), "bracket cap enforced at 125x")
check(any(w["code"] == "lev_extreme" for w in preview(symbol="BTC/USDT", side="BUY", qty=0.01)["warnings"]),
      "extreme leverage warning")
post("/api/paper/leverage", {"symbol": "BTC/USDT", "leverage": 5, "margin_mode": "isolated"})

print("\n[5] market long -> TP")
EX.spread = 0.0002
set_prices(BTC_USDT=60000)
r = order(symbol="BTC/USDT", side="BUY", qty=0.1, tp_price=63000, sl_price=58000, notes="Breakout above range", tag="Breakout")
d = r.get_json()
check(r.status_code == 200 and d["order"]["status"] == "FILLED" and d["fill"]["liquidity"] == "TAKER", "market order filled")
fill = d["fill"]["price"]
check(fill > 60000, "filled at the ask, not the last price")
st = state()
pos = st["positions"][0]
check(pos["side"] == "LONG" and close(pos["qty"], 0.1) and pos["tp_price"] == 63000 and pos["sl_price"] == 58000,
      "position has TP/SL attached")
check(close(st["account"]["wallet"], 10000 - 0.1 * fill * 0.0005), "wallet charged the taker fee")
check(close(st["account"]["available"], st["account"]["wallet"] - 0.1 * fill / 5), "available minus isolated margin")
r = post("/api/paper/leverage", {"symbol": "BTC/USDT", "leverage": 10, "margin_mode": "isolated"})
check(r.status_code == 400, "leverage change blocked while position open")
set_prices(BTC_USDT=61000)
tick()
st = state()
check(st["positions"] and st["positions"][0]["upnl"] > 0, "unrealized PnL follows price")
set_prices(BTC_USDT=63100)
tick()
st = state()
check(not st["positions"], "TP closed the position")
cl = client.get("/api/paper/history?kind=closed").get_json()["rows"][0]
check(cl["close_reason"] == "TP" and cl["net_pnl"] > 0 and cl["tag"] == "Breakout" and cl["sl_used"], "closed trade journaled")
check(close(cl["net_pnl"], cl["realized"] - cl["fees"] - cl["funding"]), "net = realized - fees - funding")
check(cl["mfe_pct"] and cl["mfe_pct"] > 4, "MFE tracked")
lsum, w = wallet_from_ledger(1)
check(close(lsum, w), "ledger reconciles with wallet")

print("\n[6] stop-loss + partial close + flip")
EX.spread = 0.0
set_prices(ETH_USDT=3000)
order(symbol="ETH/USDT", side="BUY", qty=2, sl_price=2900)
r = post(f"/api/paper/positions/{state()['positions'][0]['id']}/close", {"fraction": 0.5})
check(r.status_code == 200 and close(state()["positions"][0]["qty"], 1.0), "50% partial close")
r = order(symbol="ETH/USDT", side="SELL", qty=3)
st = state()
pos = st["positions"][0]
check(r.status_code == 200 and pos["side"] == "SHORT" and close(pos["qty"], 2.0), "selling more than the long flips to short")
check(pos["sl_price"] is None, "old SL does not carry over to the flipped position")
cls = client.get("/api/paper/history?kind=closed").get_json()["rows"]
check(cls[0]["symbol"] == "ETH/USDT" and cls[0]["side"] == "LONG" and close(cls[0]["qty"], 2.0), "long archived with max size")
r = post(f"/api/paper/positions/{pos['id']}/tpsl", {"sl_price": 2950})
check(r.status_code == 400, "SL below price rejected for a short")
r = post(f"/api/paper/positions/{pos['id']}/tpsl", {"sl_price": 3100, "tp_price": 2800})
check(r.status_code == 200, "TP/SL edited on position")
set_prices(ETH_USDT=3105)
tick()
check(not state()["positions"], "SL closed the short")
check(client.get("/api/paper/history?kind=closed").get_json()["rows"][0]["close_reason"] == "SL", "close reason SL")

print("\n[7] limit / IOC / stop / stop-limit / trailing")
set_prices(BTC_USDT=60000)
r = order(symbol="BTC/USDT", side="BUY", type="LIMIT", qty=0.1, price=59000)
o = r.get_json()["order"]
st = state()
check(o["status"] == "NEW" and close(st["account"]["order_margin"], 0.1 * 59000 / 5 + 0.1 * 59000 * 0.0002),
      "resting limit reserves margin + maker fee")
set_prices(BTC_USDT=59500)
tick()
check(state()["orders"], "limit waits above its price")
set_prices(BTC_USDT=58990)
tick()
st = state()
f = client.get("/api/paper/history?kind=fills").get_json()["rows"][0]
check(not st["orders"] and st["positions"] and f["liquidity"] == "MAKER" and f["price"] == 59000,
      "limit filled at its price as maker")
check(close(f["fee"], 0.1 * 59000 * 0.0002), "maker fee 0.02%")
r = order(symbol="BTC/USDT", side="SELL", type="LIMIT", qty=0.1, price=65000, tif="IOC", reduce_only=True)
check(r.get_json()["order"]["status"] == "EXPIRED", "unfillable IOC expires")
r = order(symbol="BTC/USDT", side="SELL", type="TRAILING_STOP", qty=0.1, callback_pct=1, reduce_only=True)
o = r.get_json()["order"]
check(o["trail_active"] and close(o["trail_extreme"], 58990), "trailing stop active immediately")
set_prices(BTC_USDT=60000)
tick()
set_prices(BTC_USDT=59500)
tick()
check(state()["positions"], "pullback < callback does not trigger")
set_prices(BTC_USDT=59390)
tick()
st = state()
check(not st["positions"] and not st["orders"], "trailing stop closed after 1% pullback from the high")
check(client.get("/api/paper/history?kind=closed").get_json()["rows"][0]["close_reason"] == "TRAILING", "reason TRAILING")
r = order(symbol="SOL/USDT", side="BUY", type="STOP_MARKET", qty=10, trigger_price=155, sl_price=150)
check(r.get_json()["order"]["status"] == "NEW", "stop-market waiting")
set_prices(SOL_USDT=154)
tick()
check(not state()["positions"], "not triggered below the stop")
set_prices(SOL_USDT=155.5)
tick()
st = state()
check(st["positions"] and st["positions"][0]["sl_price"] == 150, "breakout stop filled, SL attached")
r = order(symbol="SOL/USDT", side="SELL", type="STOP_LIMIT", qty=10, trigger_price=152, price=151.5, reduce_only=True)
set_prices(SOL_USDT=151.0)
tick()
o = q("SELECT * FROM paper_orders WHERE id=?", (r.get_json()["order"]["id"],))[0]
check(o["triggered"] == 1 and o["status"] == "NEW" and state()["positions"],
      "price gapped below the limit: stop-limit triggered but rests (no fill below 151.5)")
set_prices(SOL_USDT=151.6)
tick()
f = client.get("/api/paper/history?kind=fills").get_json()["rows"][0]
check(not state()["positions"] and f["price"] == 151.5 and f["liquidity"] == "MAKER", "stop-limit filled at its limit on the bounce")
check(not any(o["symbol"] == "SOL/USDT" for o in state()["orders"]), "orphan orders canceled when position closes")

print("\n[8] cancel orders")
order(symbol="BTC/USDT", side="BUY", type="LIMIT", qty=0.01, price=50000)
order(symbol="ETH/USDT", side="BUY", type="LIMIT", qty=0.1, price=2500)
oid = state()["orders"][0]["id"]
check(post(f"/api/paper/orders/{oid}/cancel").status_code == 200, "cancel one")
check(post(f"/api/paper/orders/{oid}/cancel").status_code == 404, "cannot cancel twice")
check(post("/api/paper/orders/cancel-all", {}).get_json()["canceled"] == 1 and not state()["orders"], "cancel all")
check(state()["account"]["order_margin"] == 0, "reserves released")

print("\n[9] isolated liquidation")
post("/api/paper/leverage", {"symbol": "ETH/USDT", "leverage": 20, "margin_mode": "isolated"})
set_prices(ETH_USDT=3000)
w0 = state()["account"]["wallet"]
order(symbol="ETH/USDT", side="BUY", qty=1)
pos = state()["positions"][0]
margin = pos["margin"]
check(close(margin, 3000 / 20, 1e-4) and 2850 < pos["liq_price"] < 2870, f"20x liquidation ~{pos['liq_price']:.1f}")
set_prices(ETH_USDT=pos["liq_price"] - 1)
tick()
st = state()
check(not st["positions"], "liquidated below the liquidation price")
cl = client.get("/api/paper/history?kind=closed").get_json()["rows"][0]
check(cl["close_reason"] == "LIQUIDATION" and close(cl["net_pnl"], -(margin + 3000 * 0.0005)), "loss == margin + entry fee")
check(close(st["account"]["wallet"], w0 - margin - 3000 * 0.0005), "wallet lost exactly the margin + entry fee")
lsum, w = wallet_from_ledger(1)
check(close(lsum, w), "ledger reconciles after liquidation")

print("\n[10] SL that gaps past liquidation -> liquidation, capped at margin")
set_prices(ETH_USDT=3000)
w0 = state()["account"]["wallet"]
order(symbol="ETH/USDT", side="BUY", qty=1, sl_price=2900)
margin = state()["positions"][0]["margin"]
set_prices(ETH_USDT=2500)
tick()
cl = client.get("/api/paper/history?kind=closed").get_json()["rows"][0]
check(cl["close_reason"] == "LIQUIDATION" and close(state()["account"]["wallet"], w0 - margin - 3000 * 0.0005),
      "gap through SL + liquidation never loses more than the margin")

print("\n[11] isolated margin add / remove")
set_prices(ETH_USDT=3000)
order(symbol="ETH/USDT", side="BUY", qty=1, sl_price=2900)
pos = state()["positions"][0]
r = post(f"/api/paper/positions/{pos['id']}/margin", {"amount": 150})
pos2 = state()["positions"][0]
check(r.status_code == 200 and close(pos2["margin"], 300) and pos2["liq_price"] < pos["liq_price"], "add margin moves liq away")
check(post(f"/api/paper/positions/{pos['id']}/margin", {"amount": -200}).status_code == 400, "can't remove below initial margin")
check(post(f"/api/paper/positions/{pos['id']}/margin", {"amount": -140}).status_code == 200, "remove extra margin")
post(f"/api/paper/positions/{pos['id']}/close", {"fraction": 1})

print("\n[12] limit close of a position")
order(symbol="ETH/USDT", side="BUY", qty=1)
pid = state()["positions"][0]["id"]
r = post(f"/api/paper/positions/{pid}/close", {"type": "LIMIT", "price": 3100, "fraction": 1})
o = r.get_json()["order"]
check(o["status"] == "NEW" and o["reduce_only"] and o["type"] == "LIMIT", "limit close placed as reduce-only")
set_prices(ETH_USDT=3101)
tick()
check(not state()["positions"], "limit close filled")

print("\n[13] cross margin + account liquidation")
login(2)
post("/api/paper/leverage", {"symbol": "BTC/USDT", "leverage": 10, "margin_mode": "cross"})
set_prices(BTC_USDT=60000)
r = order(symbol="BTC/USDT", side="BUY", qty=1)
st = state()
pos = st["positions"][0]
check(r.status_code == 200 and pos["margin_mode"] == "cross" and st["account"]["has_cross"], "cross position open")
check(pos["liq_price"] and 50000 < pos["liq_price"] < 51000, f"cross liq uses the whole wallet (~{pos['liq_price']:.0f})")
check(st["account"]["margin_ratio"] and st["account"]["margin_ratio"] < 5, "margin ratio reported")
set_prices(BTC_USDT=pos["liq_price"] * 0.999)
tick()
st = state()
check(not st["positions"] and st["account"]["wallet"] >= -1e-6 and st["account"]["wallet"] < 100,
      "cross account liquidated, wallet not negative")
cl = client.get("/api/paper/history?kind=closed").get_json()["rows"][0]
check(cl["close_reason"] == "LIQUIDATION", "cross liquidation recorded")
lsum, w = wallet_from_ledger(2)
check(close(lsum, w), "ledger reconciles after cross liquidation")

print("\n[14] spot trading")
login(3)
EX.spread = 0.0
set_prices(XRP_USDT=0.6)
r = order(market="spot", symbol="XRP/USDT", side="BUY", qty=1000, sl_price=0.55)
st = state()
pos = st["positions"][0]
check(r.status_code == 200 and pos["market"] == "spot" and pos["leverage"] == 1 and pos["liq_price"] is None,
      "spot buy holds coins, no leverage, no liquidation")
check(close(st["account"]["available"], 10000 - 600 - 0.6), "spot cost + 0.1% fee leave the available balance")
r = order(market="spot", symbol="XRP/USDT", side="SELL", type="LIMIT", qty=600, price=0.7)
p = preview(market="spot", symbol="XRP/USDT", side="SELL", qty=500)
check(any("at most 400" in e for e in p["errors"]), "can't sell coins already in open sell orders")
set_prices(XRP_USDT=0.71)
tick()
st = state()
check(close(st["positions"][0]["qty"], 400), "spot limit sell filled partially out of the holding")
set_prices(XRP_USDT=0.54)
tick()
st = state()
check(not st["positions"], "spot stop-loss sold the rest")
lsum, w = wallet_from_ledger(3)
check(close(lsum, w), "spot ledger reconciles")

print("\n[15] funding")
login(1)
set_prices(BTC_USDT=60000, ETH_USDT=3000)
post("/api/paper/leverage", {"symbol": "BTC/USDT", "leverage": 5, "margin_mode": "isolated"})
order(symbol="BTC/USDT", side="BUY", qty=0.1)
order(symbol="ETH/USDT", side="SELL", qty=1)
c = Conn(); cu = c.cursor()
cu.execute("UPDATE paper_positions SET opened_at=?", (datetime.utcnow() - timedelta(hours=9),))
cu.execute("DELETE FROM paper_engine"); c.close()
now = 1_800_000_000
check(pt.funding_check(now, prices()) is False, "first engine run only records the slot")
w0 = state()["account"]["wallet"]
check(pt.funding_check(now + 8 * 3600, prices()) is True, "next slot applies funding")
w1 = state()["account"]["wallet"]
check(close(w1 - w0, -0.1 * 60000 * 0.0001 + 1 * 3000 * 0.0001), "longs pay, shorts receive 0.01%")
check(pt.funding_check(now + 8 * 3600 + 60, prices()) is False, "funding applied once per slot")
for p_ in state()["positions"]:
    post(f"/api/paper/positions/{p_['id']}/close", {"fraction": 1})
lsum, w = wallet_from_ledger(1)
check(close(lsum, w), "ledger reconciles after funding")

print("\n[16] stats + journal")
s = client.get("/api/paper/stats").get_json()["stats"]
check(s["trades"] >= 9 and s["win_rate"] is not None and s["liquidations"] == 2, "trade counts")
check(close(s["net_pnl"], sum(r["net_pnl"] for r in client.get("/api/paper/history?kind=closed&limit=500").get_json()["rows"])),
      "net PnL = sum of closed trades")
check(s["max_drawdown_pct"] > 0 and s["curve"] and s["fees_paid"] > 0 and s["funding_paid"] is not None, "drawdown + curve + costs")
check(0 <= s["sl_usage_pct"] <= 100 and s["long"]["count"] + s["short"]["count"] == s["trades"], "discipline + side split")
cid = client.get("/api/paper/history?kind=closed").get_json()["rows"][0]["id"]
check(post(f"/api/paper/closed/{cid}/journal", {"notes": "Exited early, nervous", "tag": "Pullback"}).status_code == 200,
      "journal saved")
check(client.get("/api/paper/history?kind=closed").get_json()["rows"][0]["notes"] == "Exited early, nervous", "journal persisted")
synthetic = pt.compute_stats(
    [{"symbol": "A", "side": "LONG", "net_pnl": 100, "close_reason": "TP", "sl_used": 1, "opened_at": "2026-01-01 00:00:00",
      "closed_at": "2026-01-01 01:00:00", "mfe_pct": 2, "mae_pct": -1},
     {"symbol": "A", "side": "SHORT", "net_pnl": -50, "close_reason": "SL", "sl_used": 1, "opened_at": "2026-01-01 00:00:00",
      "closed_at": "2026-01-01 00:30:00", "mfe_pct": 1, "mae_pct": -2}],
    [{"type": "DEPOSIT", "amount": 1000, "balance_after": 1000, "created_at": "2026-01-01 00:00:00"},
     {"type": "REALIZED_PNL", "amount": 100, "balance_after": 1100, "created_at": "2026-01-01 01:00:00"},
     {"type": "REALIZED_PNL", "amount": -50, "balance_after": 1050, "created_at": "2026-01-01 02:00:00"}], 1000)
check(synthetic["win_rate"] == 50 and synthetic["profit_factor"] == 2 and close(synthetic["max_drawdown_pct"], 50 / 1100 * 100),
      "win rate, profit factor, drawdown math")

print("\n[17] reset")
order(symbol="BTC/USDT", side="BUY", type="LIMIT", qty=0.01, price=50000)
order(symbol="ETH/USDT", side="BUY", qty=0.5)
check(post("/api/paper/reset", {"start_balance": 7777}).status_code == 400, "invalid starting balance rejected")
r = post("/api/paper/reset", {"start_balance": 50000})
st = state()
check(r.status_code == 200 and st["account"]["session"] == 2 and st["account"]["wallet"] == 50000
      and not st["positions"] and not st["orders"], "reset: fresh 50k account, positions + orders gone")
check(client.get("/api/paper/history?kind=closed").get_json()["rows"] == [] and
      client.get("/api/paper/stats").get_json()["stats"]["trades"] == 0, "history + stats start fresh after reset")

print("\n[18] engine bookkeeping")
pt._heartbeat()
check(state()["engine"]["online"], "heartbeat -> engine online")
check(post("/api/paper/orders", None).status_code in (200, 400) and
      client.post("/api/paper/orders", data="x").status_code == 415, "non-JSON order rejected")

print(f"\nALL {passed} CHECKS PASSED")
