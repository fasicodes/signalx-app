"""Tests for the Live chart + Liquidity scanner pages (market_tools.py) and the pieces they use in
main.py, signal_engine.py and alerts.py. No network, no MySQL. Run from the project root:
    python tests/test_market_tools.py
"""
import ast
import os
import re
import sys
import time
import types

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

from flask import Flask, render_template  # noqa: E402
import market_tools as mt  # noqa: E402

passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


def frame(rows, start=1_700_000_000, step=3600):
    """rows: list of (open, high, low, close, volume)."""
    return pd.DataFrame({"timestamp": pd.to_datetime([start + i * step for i in range(len(rows))], unit="s"),
                         "open": [r[0] for r in rows], "high": [r[1] for r in rows], "low": [r[2] for r in rows],
                         "close": [r[3] for r in rows], "volume": [r[4] for r in rows]})


print("\n[1] order book statistics")
bids = [[100 - 0.1 * i, 1.0] for i in range(30)]
asks = [[100.1 + 0.1 * i, 1.0] for i in range(30)]
bids[5][1] = 20.0                        # a wall at 99.5
b = mt.book_stats(bids, asks)
check(b["available"] and abs(b["mid"] - 100.05) < 1e-9 and abs(b["spread"] - 0.1) < 1e-9, "best bid/ask, mid and spread")
check(abs(b["spread_bps"] - 0.1 / 100.05 * 1e4) < 1e-3, "spread in basis points")
check(b["levels"] == {"bids": 30, "asks": 30} and 2.9 < b["reach_pct"]["bids"] < 3.0, "levels and reach")
band1 = [x for x in b["bands"] if x["pct"] == 1.0][0]
check(band1["covered"] and band1["imbalance"] > 0.3, "bids heavier within 1% because of the wall")
check([x["covered"] for x in b["bands"] if x["pct"] == 5.0] == [False], "5% band marked as beyond the visible book")
top = b["walls"][0]
check(top["side"] == "BID" and top["price"] == 99.5 and top["is_wall"] and top["x_median"] >= 19, "biggest wall found with its size vs a normal level")
check(not any(w["is_wall"] for w in b["walls"][1:]), "ordinary levels are not called walls")
cum = [p[1] for p in b["depth"]["bids"]]
check(cum == sorted(cum) and abs(cum[-1] - sum(p * q for p, q in bids)) < 0.01, "depth curve adds up to the whole book")
rows = b["ladder"]["rows"]
check(len(rows) == 40 and abs(sum(r["usd"] for r in rows) - (b["total_usd"]["bids"] + b["total_usd"]["asks"])) < 0.05, "ladder covers every order")
check(rows[0]["side"] == "ask" and rows[-1]["side"] == "bid" and rows[19]["lo"] < rows[0]["lo"], "ladder runs from far asks down to far bids")
check(mt.book_stats([], asks)["available"] is False, "empty side -> unavailable")
check(mt.book_stats([[100, 0], ["x", 1], [99, 2]], [[101, 1]])["levels"]["bids"] == 1, "zero and bad levels dropped")
many = mt.book_stats([[100 - 0.01 * i, 1] for i in range(400)], [[100.01 + 0.01 * i, 1] for i in range(400)])
check(len(many["depth"]["bids"]) <= 90 and many["depth"]["bids"][-1][1] > many["depth"]["bids"][0][1], "depth thinned to 90 points, last point kept")

print("\n[2] sweeps, stop pools, precision")
rows = [(1.0, 1.01, 0.99, 1.0, 10)] * 60
rows[30] = (1.0, 1.05, 0.99, 1.0, 10)            # range high 1.05
rows[-1] = (1.0, 1.06, 0.99, 1.02, 10)           # wick above 1.05, close back below -> swept high
sw = mt.swing_levels(frame(rows))
check(sw["available"] and sw["swing_high"] == 1.06 and sw["sweeps"] and sw["sweeps"][0]["side"] == "HIGH", "sweep of the high detected")
check(sw["sweeps"][0]["level"] == 1.05 and sw["sweeps"][0]["forming"] is True and sw["sweeps"][0]["bars_ago"] == 0, "sweep level and live-candle flag")
tiny = mt.swing_levels(frame([(0.000012345, 0.000012399, 0.000012301, 0.000012345, 5)] * 20))
check(tiny["swing_high"] == 0.000012399 and tiny["swing_low"] == 0.000012301, "cheap coins keep full precision")
base = [(100, 101, 99, 100, 5)] * 80
eq = list(base)
for i in (20, 40):
    eq[i] = (100, 105, 99, 100, 5)               # two equal highs at 105
for i in (25, 45):
    eq[i] = (100, 101, 95, 100, 5)               # two equal lows at 95
pools = mt.stop_pools(frame(eq), 100.0)
kinds = {(p["type"], p["price"], p["touches"]) for p in pools}
check(("EQH", 105.0, 2) in kinds and ("EQL", 95.0, 2) in kinds, "equal highs and equal lows found")
check(all((p["type"] == "EQH") == (p["distance_pct"] > 0) for p in pools), "highs above the price, lows below")
taken = list(eq)
taken[60] = (100, 106, 99, 100, 5)               # later candle trades through 105 -> stops gone
check(not any(p["type"] == "EQH" and p["price"] == 105.0 for p in mt.stop_pools(frame(taken), 100.0)), "a pool price already traded through is dropped")

print("\n[3] estimated liquidation zones")
flat = [(100, 100.5, 99.5, 100, 1000)] * 200
liq = mt.liquidation_estimates(frame(flat), 100.0)
check(liq["available"] and liq["model"] == "estimate", "estimates available with volume")
check(all(c["price"] < 100 for c in liq["below"]) and all(c["price"] > 100 for c in liq["above"]), "long liquidations below, short liquidations above")
lev = {x["leverage"]: x for x in liq["by_leverage"]}
check(abs(lev[10]["long_liq_now"] - 100 * (1 - 0.1 + 0.005)) < 1e-9 and abs(lev[100]["short_liq_now"] - 100 * (1 + 0.01 - 0.005)) < 1e-9, "liquidation price formula per leverage")
crash = list(flat)
crash[150] = (100, 100.5, 96.0, 100, 1000)       # a wick to 96 wipes 25x+ longs opened before it
liq2 = mt.liquidation_estimates(frame(crash), 100.0)
near_before = [h for h in liq["heat"] if 96.0 < h["hi"] and h["lo"] < 99.6 and h["long"] > 0]
near_after = [h for h in liq2["heat"] if 96.0 < h["hi"] and h["lo"] < 99.6 and h["long"] > 0]
check(sum(h["long"] for h in near_after) < sum(h["long"] for h in near_before), "levels price already crossed are removed")
check(mt.liquidation_estimates(frame([(1.08, 1.09, 1.07, 1.08, 0)] * 50), 1.08)["available"] is False, "no volume (forex) -> not available")

print("\n[4] trades: taker flow and big prints")
now = int(time.time() * 1000)
trs = [{"timestamp": now - 1000 * (100 - i), "side": "buy" if i % 3 else "sell", "price": 100.0, "amount": 1.0, "cost": None} for i in range(100)]
trs[50] = {"timestamp": now - 50000, "side": "sell", "price": 100.0, "amount": 50.0, "cost": 5000.0}
tf = mt.trade_flow(trs)
check(tf["available"] and tf["count"] == 100 and abs(tf["span_sec"] - 99) < 0.01, "count and time span")
check(abs(tf["buy_usd"] - 6500) < 1e-6 and abs(tf["sell_usd"] - 8400) < 1e-6, "buy and sell volume (cost or price x amount)")
check(tf["large"] and tf["large"][0]["usd"] == 5000.0 and tf["large"][0]["side"] == "sell", "the big print is listed")
check(tf["delta"][-1][1] == round(tf["buy_usd"] - tf["sell_usd"], 2), "delta line ends at buys minus sells")
check(mt.trade_flow([{"side": "buy"}, "x", None])["available"] is False, "bad trades ignored")

print("\n[5] API with a fake exchange")


class FakeEx:
    def __init__(self):
        self.calls = {"book": 0, "trades": 0, "tickers": 0}
        self.markets = {"BTC/USDT": {}, "ETH/USDT": {}}
        self.fail_book = False

    def load_markets(self):
        return self.markets

    def fetch_order_book(self, sym, limit=400):
        self.calls["book"] += 1
        if self.fail_book:
            raise RuntimeError("timeout")
        return {"bids": [[100 - 0.05 * i, 1, 0] for i in range(limit)], "asks": [[100.05 + 0.05 * i, 1, 0] for i in range(limit)]}

    def fetch_trades(self, sym, limit=500):
        self.calls["trades"] += 1
        return trs

    def fetch_tickers(self, syms):
        self.calls["tickers"] += 1
        assert "GRAM/USDT" not in syms, "symbols the exchange does not list must be skipped"
        return {s: {"last": 100.0, "percentage": 1.234567, "high": 101, "low": 99, "quoteVolume": 12345.678} for s in syms}


EX = FakeEx()
PAYLOADS = []


def payload(coin, tf, df=None, order_book=None, trades=None):
    PAYLOADS.append({"coin": coin, "rows": len(df), "book": order_book is not None and len(order_book["bids"]), "trades": trades is not None})
    return {"coin": coin, "magnet": None}


CANDLES = {"fail": False}


def get_candles(symbol, timeframe="1h", limit=200, since=None):
    if CANDLES["fail"]:
        raise ValueError("exchange down")
    rows = [(100, 101, 99, 100 + (i % 5) * 0.1, 0 if symbol == "EUR/USD" else 50) for i in range(limit)]
    return frame(rows)


app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"), static_folder=os.path.join(ROOT, "static"))
app.secret_key = "t"
app.context_processor(lambda: {"track_record_public": False, "ga_measurement_id": "", "support_email": "", "site_url": "", "current_year": 2026, "is_admin": False})
mt.init_market_tools(app, exchange=EX, get_candles=get_candles, liquidity_payload=payload,
                     coins=["BTC/USDT", "ETH/USDT", "GRAM/USDT"], forex_pairs=["EUR/USD", "XAU/USD"], is_forex=lambda s: s in ("EUR/USD", "XAU/USD"))
app.register_blueprint(mt.market_bp)
c = app.test_client()


def login(on=True):
    with c.session_transaction() as s:
        if on:
            s["user_id"] = 7
            s["email"] = "me@x.com"
        else:
            s.clear()


r = c.get("/chart")
check(r.status_code == 302 and r.headers["Location"].endswith("/login"), "chart page needs login")
check(c.get("/liquidity-scanner").status_code == 302, "scanner page needs login")
check(c.get("/api/liquidity/map?coin=BTC/USDT").status_code == 401, "scanner data needs login")
login()
r = c.get("/api/liquidity/map?coin=DOGE/USDT")
check(r.status_code == 400 and "Unknown" in r.get_json()["error"], "unknown coin rejected")
r = c.get("/api/liquidity/map?coin=BTC/USDT&tf=2m")
check(r.status_code == 400 and "Timeframe" in r.get_json()["error"], "unknown timeframe rejected")
r = c.get("/api/liquidity/map?coin=btc/usdt&tf=1h")
d = r.get_json()
check(r.status_code == 200 and d["ok"] and d["coin"] == "BTC/USDT" and d["asset"] == "crypto", "map for a crypto coin (lower-case input accepted)")
check(d["book"]["available"] and d["book"]["levels"]["bids"] == 400 and d["trades"]["available"], "deep book (400 levels) and trades")
check(d["liquidations"]["available"] and "pools" in d["levels"] and d["levels"]["swing"]["available"], "levels and liquidation estimates")
check(PAYLOADS[-1] == {"coin": "BTC/USDT", "rows": 120, "book": 400, "trades": True}, "scanner cards get the same candles, book and trades (fetched once)")
check(abs(d["price"] - d["book"]["mid"]) < 1e-9, "price = order-book mid")
n_book = EX.calls["book"]
c.get("/api/liquidity/map?coin=BTC/USDT&tf=1h")
check(EX.calls["book"] == n_book, "second request within 5 s served from the cache")
mt._cache.clear()
d = c.get("/api/liquidity/map?coin=EUR/USD&tf=4h").get_json()
check(d["ok"] and d["asset"] == "forex" and not d["book"]["available"] and not d["trades"]["available"], "forex: no book or trades, still ok")
check(not d["liquidations"]["available"] and d["levels"]["swing"]["available"], "forex: price-based levels only")
check(PAYLOADS[-1]["book"] is False and PAYLOADS[-1]["trades"] is False, "forex scanner cards get no book")
mt._cache.clear()
EX.fail_book = True
d = c.get("/api/liquidity/map?coin=ETH/USDT&tf=1h").get_json()
check(d["ok"] and not d["book"]["available"] and "could not be read" in d["book"]["reason"], "order-book failure shown, rest still works")
EX.fail_book = False
mt._cache.clear()
CANDLES["fail"] = True
r = c.get("/api/liquidity/map?coin=ETH/USDT&tf=4h")
check(r.status_code == 400 and "not available right now" in r.get_json()["error"], "no candles -> clear message")
CANDLES["fail"] = False
mt._cache.clear()
r = c.get("/api/market/tickers")
t = r.get_json()
check(r.status_code == 200 and set(t["tickers"]) == {"BTC/USDT", "ETH/USDT"}, "tickers skip coins the exchange does not list")
check(t["tickers"]["BTC/USDT"]["change_pct"] == 1.235 and t["tickers"]["BTC/USDT"]["quote_volume"] == 12345.68, "ticker fields")
n = EX.calls["tickers"]
c.get("/api/market/tickers")
check(EX.calls["tickers"] == n, "tickers cached")
mt._cache[("tickers",)] = (0, t["tickers"])          # expired, and the exchange now fails
EX.fetch_tickers = lambda syms: (_ for _ in ()).throw(RuntimeError("down"))
t2 = c.get("/api/market/tickers").get_json()
check(t2["ok"] and t2.get("stale") and "BTC/USDT" in t2["tickers"], "old prices served (marked stale) when the exchange fails")

print("\n[6] pages")
html = c.get("/chart?coin=ETH/USDT").get_data(as_text=True)
check('id="coin-btn"' in html and "chart-page.js" in html and "market-common.js" in html and "market.css" in html, "chart page: coin switcher and its scripts")
check('id="tf-seg"' in html and 'id="ind-btn"' in html and 'id="alert-btn"' in html and 'id="ch-tools"' in html, "chart page: timeframes, indicators, alerts, drawing tools")
check("runAnalysis" not in html and "/static/script.js" not in html, "chart page never loads the Pro terminal analysis")
check('href="/chart" class="an-item" aria-current="page"' in html, "menu marks Live chart")
check("lightweight-charts@5.2.0" in html and "attributionLogo" not in html and "TradingView" in html, "chart library + TradingView credit")
html = c.get("/liquidity-scanner").get_data(as_text=True)
check('id="coin-btn"' in html and "liquidity-page.js" in html and 'id="depth"' in html and 'id="liq"' in html, "scanner page: coin switcher, depth, liquidations")
check('href="/liquidity-scanner" class="an-item" aria-current="page"' in html, "menu marks Liquidity scanner")
check("runAnalysis" not in html and "/advanced#liquidity" not in html, "scanner page is independent of the Pro terminal")
nav = open(os.path.join(ROOT, "templates", "_app_nav.html"), encoding="utf-8").read()
check('item("/chart", "Live chart"' in nav and 'item("/liquidity-scanner", "Liquidity scanner"' in nav, "side menu opens the new pages")
check("/advanced#livechart" not in nav and "/advanced#liquidity" not in nav, "side menu no longer sends these to the Pro terminal")
dash = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
appjs = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
check('data-page="/chart"' in dash and 'data-page="/liquidity-scanner"' in dash and 'href="/chart" id="adv-chart-link"' in dash, "dashboard tool links go to the new pages")
check("a.dataset.page" in appjs and "`/chart${q}" in appjs, "dashboard links carry the coin")
for f in ("chart-page.js", "liquidity-page.js", "market-common.js"):
    src = open(os.path.join(ROOT, "static", f), encoding="utf-8").read()
    check("runAnalysis" not in src and "/signal?" not in src, f"{f} does not call the heavy /signal analysis")
cp = open(os.path.join(ROOT, "static", "chart-page.js"), encoding="utf-8").read()
tools = set(re.findall(r'\["([a-z_]+)", "', cp.split("const TOOL_GROUPS")[1].split("];")[0]))
import chart_drawings  # noqa: E402
check(tools == chart_drawings.ALLOWED_DRAWING_TYPES | {"cursor"}, "all 36 drawing tools (same as the Pro terminal) and the server accepts each")
inds = re.findall(r'\{ key: "([a-z]+)", name:', cp)
check(len(inds) == 25 and len(set(inds)) == 25, "25 indicators")

print("\n[7] main.py, signal engine and alerts changes")
src = open(os.path.join(ROOT, "main.py"), encoding="utf-8", newline="").read()
check(src.count("\r\n") == src.count("\n"), "main.py keeps Windows line endings")
tree = ast.parse(src)
fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
src = src.replace("\r\n", "\n")
check("build_liquidity_payload" in fns and [a.arg for a in fns["build_liquidity_payload"].args.args] == ["coin", "timeframe", "df", "order_book", "trades"], "build_liquidity_payload(coin, timeframe, df, order_book, trades)")
check("return jsonify(build_liquidity_payload(coin, timeframe, df=df))" in src, "/liquidity (Pro terminal) uses the same function")
check("trades" in [a.arg for a in fns["vpin_toxicity"].args.args] and "vpin_toxicity(coin, trades=trades)" in src, "VPIN reuses fetched trades")
check("init_market_tools(app, exchange=exchange, get_candles=get_candles, liquidity_payload=build_liquidity_payload" in src, "market_tools registered in main.py")
ns = {"np": np, "pd": pd}
exec(compile(ast.Module(body=[fns["liquidity_sweep_detector"]], type_ignores=[]), "main", "exec"), ns)
r = ns["liquidity_sweep_detector"](frame([(0.12345, 0.12399, 0.12301, 0.12345, 1)] * 60))
check(r["swing_high"] == 0.12399 and r["swing_low"] == 0.12301, "Pro terminal sweep levels no longer rounded to 2 decimals")
import signal_engine as se  # noqa: E402
check(se.RECENT_KEYS[:4] == ("side", "entry", "stop_loss", "take_profit") and se.RECENT_MAX == 30, "engine result lists recent signals")
eng_src = open(os.path.join(ROOT, "signal_engine.py"), encoding="utf-8").read()
check("active, last = replay(F, pl, ps, cl, cs, trades=closed)" in eng_src and '"recent": [{k: t[k] for k in RECENT_KEYS}' in eng_src, "live engine fills recent from the same replay")
import alerts  # noqa: E402
ob = {"bids": [[99.5, 7.0], [90.0, 100.0]], "asks": [[100.5, 3.0]]}
check(abs(alerts.book_imbalance(ob, 100.0) - (7 * 99.5 - 3 * 100.5) / (7 * 99.5 + 3 * 100.5)) < 1e-9, "imbalance uses orders within 1% only")
check(alerts.book_imbalance({"bids": [], "asks": []}, 100.0) is None and alerts.book_imbalance(ob, None) is None, "empty book -> no imbalance")
asrc = open(os.path.join(ROOT, "alerts.py"), encoding="utf-8").read()
check('wall_bias in ("BUY", "SELL", "BULLISH", "BEARISH")' not in asrc and "abs(imb) >= IMBALANCE_ALERT" in asrc, "imbalance alert can fire now (old check never matched)")
shell = open(os.path.join(ROOT, "static", "shell.js"), encoding="utf-8").read()
check('"/api/alerts/check"' in shell and "setInterval(checkAlerts, 60000)" in shell, "alerts are checked on every app page")
import glossary  # noqa: E402
for k in ("order_book", "spread", "imbalance", "wall", "depth_chart", "liquidity_sweep", "stop_pool", "liquidation_zone", "taker_flow",
          "large_trades", "open_interest", "cvd", "spoofing", "trap", "likely_target", "signal_history", "heikin_ashi", "log_scale", "volume"):
    check(k in glossary.GLOSSARY, f"glossary explains {k}")

# --- endless chart history: /candles?before= fills the whole page right up to `before` (update 12)
H = 3600


def hist_fetch(cap=100, listed=1_600_000_000, gap=None):
    calls = []

    def fetch(symbol, timeframe, limit, since=None):
        calls.append(since)
        start = max(listed, (since // 1000 + H - 1) // H * H)
        rows = []
        for i in range(min(limit, cap)):       # like OKX history: at most 100 candles per call
            t = start + i * H
            if t > 1_700_000_000:
                break
            if gap and gap[0] <= t < gap[1]:
                continue
            rows.append([t * 1000, 1.0, 2.0, 0.5, 1.5, 10.0])
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    return fetch, calls


before = 1_699_000_000 // H * H
f, calls = hist_fetch()
df = mt.candles_before(f, "BTC/USDT", "1h", 300, before, H)
ts = [int(x.timestamp()) for x in df["timestamp"]]
check(len(ts) == 300 and ts[-1] == before - H and all(b - a == H for a, b in zip(ts, ts[1:])),
      "older page is complete and ends right before the oldest loaded candle (no hole)")
check(len(calls) == 3, "capped exchange answers are stitched together (3 calls of 100)")
f, calls = hist_fetch(listed=before - 50 * H)
check(len(mt.candles_before(f, "BTC/USDT", "1h", 300, before, H)) == 50, "start of history: returns what exists")
f, calls = hist_fetch(listed=before + 10)
check(mt.candles_before(f, "BTC/USDT", "1h", 300, before, H).empty and len(calls) <= mt.HISTORY_MAX_CALLS, "before listing: empty = no more history")
f, calls = hist_fetch(gap=(before - 700 * H, before))
df = mt.candles_before(f, "BTC/USDT", "1h", 300, before, H)
check(len(df) > 0 and int(df["timestamp"].iloc[-1].timestamp()) < before - 700 * H, "steps back over an empty stretch (weekend / outage)")
main_src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
check("candles_before(get_candles, coin, timeframe, limit, before_ts" in main_src, "/candles uses candles_before for older pages")
for name in ("app.js",):
    check("SFMHistory.attach" in open(os.path.join(ROOT, "static", name), encoding="utf-8").read(), f"{name} chart loads older candles")
for name in ("demo-trading.html", "auto-trading.html"):
    t = open(os.path.join(ROOT, "templates", name), encoding="utf-8").read()
    check("chart-history.js" in t and "SFMHistory.attach" in t, f"{name} chart loads older candles")
check("chart-history.js" in open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read(), "dashboard loads chart-history.js")
cp = open(os.path.join(ROOT, "static", "chart-page.js"), encoding="utf-8").read()
check("MAX_BARS = 20000" in cp and "function openFlyout" in cp, "Live chart: 20,000-candle history + flyout placed on screen")
css = open(os.path.join(ROOT, "static", "market.css"), encoding="utf-8").read()
check(".ch-fly { position: fixed;" in css, "drawing flyout is not clipped by the tool column on PC")
import chart_drawings  # noqa: E402
for tool in ("highlighter", "avwap", "arrowup", "arrowdown", "pricelabel", "parallel", "regression", "infoline",
             "datepricerange", "circle", "cyclic", "abcd", "xabcd", "elliott"):
    check(tool in chart_drawings.ALLOWED_DRAWING_TYPES and f"{tool}:" in cp, f"new drawing tool {tool} (saved + drawn)")

print(f"\nALL {passed} CHECKS PASSED")
