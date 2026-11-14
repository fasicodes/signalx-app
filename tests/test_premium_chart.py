"""Tests for the Premium chart workspace (premium_chart.py) and the bond screener (bonds.py).
No network, no MySQL: the exchange and the bond sources are fakes (bond files are trimmed real downloads in
tests/fixtures/bonds). Run from the project root:
    python tests/test_premium_chart.py
"""
import calendar
import os
import sys
import time
import types
from datetime import date

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
pm = types.ModuleType("pymysql")
pm.cursors = types.ModuleType("pymysql.cursors")
pm.cursors.DictCursor = object
pm.connect = lambda **kw: None
sys.modules.setdefault("pymysql", pm)
sys.modules.setdefault("pymysql.cursors", pm.cursors)

from flask import Flask  # noqa: E402
import bonds  # noqa: E402
import premium_chart as pc  # noqa: E402

passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


def utc(*a):
    return calendar.timegm(a + (0,) * (6 - len(a)))


print("\n[1] intervals")
ok = {"7m": ("7m", "time", None, "1m", 7), "60m": ("60m", "time", "1h", "1h", 1), "3h": ("3h", "time", None, "1h", 3),
      "45 min": ("45m", "time", None, "15m", 3), "25h": ("25h", "time", None, "1h", 25), "1d": ("1D", "time", "1d", "1d", 1),
      "2D": ("2D", "time", None, "1d", 2), "7D": ("7D", "time", None, "1d", 7), "2 weeks": ("2W", "time", None, "1w", 2),
      "1M": ("1M", "month", "1M", "1M", 1), "6M": ("6M", "month", None, "1M", 6)}
for raw, want in ok.items():
    iv = pc.parse_interval(raw)
    check(iv is not None and (iv.name, iv.kind, iv.native, iv.base, iv.ratio) == want, f"{raw!r} -> {want}")
check(pc.parse_interval("10s").kind == "second" and pc.parse_interval("100T").kind == "tick", "seconds and ticks")
check(pc.parse_interval("1m").name == "1m" and pc.parse_interval("1M").name == "1M", "m is a minute, M a month")
for bad in ("", "x", "0m", "1441m", "5M", "13M", "301s", "10001T", "2y", "-3m", "3.5h"):
    check(pc.parse_interval(bad) is None, f"{bad!r} rejected")

print("\n[2] buckets")
iv = pc.parse_interval("7m")
b = pc.bucket_start(utc(2026, 11, 6, 23, 58), iv)
check(b == utc(2026, 11, 6, 23, 55) and pc.bucket_end(b, iv) == utc(2026, 11, 7), "7m: the last bar of the day ends at 00:00 UTC")
check(pc.bucket_start(utc(2026, 11, 7, 0, 3), iv) == utc(2026, 11, 7), "7m restarts at midnight")
check(pc.expected_parts(b, iv) == 5 and pc.expected_parts(utc(2026, 11, 7), iv) == 7, "base candles per bar (short last bar)")
check(pc.bucket_start(utc(2026, 11, 6, 14, 30), pc.parse_interval("3h")) == utc(2026, 11, 6, 12), "3h")
two_w = pc.parse_interval("2W")
wb = pc.bucket_start(utc(2026, 11, 6, 12), two_w)
check(time.gmtime(wb).tm_wday == 0 and pc.bucket_end(wb, two_w) - wb == 14 * 86400, "2W starts on a Monday, 14 days long")
six = pc.parse_interval("6M")
check(pc.bucket_start(utc(2026, 11, 6), six) == utc(2026, 7, 1) and pc.bucket_end(utc(2026, 7, 1), six) == utc(2027, 1, 1), "6M: Jul-Dec")
check(pc.bucket_start(utc(2026, 3, 9), pc.parse_interval("3M")) == utc(2026, 1, 1), "3M: quarters from January")
d2 = pc.parse_interval("2D")
check((pc.bucket_start(utc(2026, 11, 6, 9), d2) // 86400) % 2 == 0, "2D counts days from 1970-01-01")

print("\n[3] building bars from base candles")
NOW = utc(2026, 11, 6, 12, 3, 30)
T0 = utc(2026, 10, 1)
CALLS = []


def candle(t):
    i = (t - T0) // 60
    o = 100 + (i % 50) * 0.1
    return (o, o + 0.5 + (i % 7) * 0.01, o - 0.4, o + 0.05, 1.0 + i % 3)


def get_candles(symbol, timeframe="1h", limit=200, since=None):
    """Like the exchange: at most 300 candles a call, the newest ones without `since`."""
    CALLS.append((symbol, timeframe, limit, since))
    sec = pc.NATIVE_SEC[timeframe] if timeframe in pc.NATIVE_SEC else 2592000
    if timeframe == "1M":
        months = []
        y, m = 2024, 1
        while utc(y, m) <= NOW:
            months.append(utc(y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        times = months
    else:
        last = NOW - NOW % sec
        start = T0 if sec < 86400 else utc(2024, 1, 1) + (-utc(2024, 1, 1) % sec if timeframe == "1d" else 0)
        if timeframe == "1w":
            start = pc.MONDAY0 + ((utc(2024, 1, 1) - pc.MONDAY0) // 604800 + 1) * 604800
        times = list(range(start, last + 1, sec))
    n = min(limit, 300)
    if since is None:
        pick = times[-n:]
    else:
        pick = [t for t in times if t >= since // 1000][:n]
    rows = [candle(t) for t in pick]
    return pd.DataFrame({"timestamp": pd.to_datetime(pick, unit="s"), "open": [r[0] for r in rows],
                         "high": [r[1] for r in rows], "low": [r[2] for r in rows], "close": [r[3] for r in rows],
                         "volume": [r[4] for r in rows]})


FOREX = {"EUR/USD", "XAU/USD"}
COINS = {"BTC/USDT", "ETH/USDT", "SOL/USDT"} | FOREX
TICK_CALLS = []


def tickers():
    TICK_CALLS.append(1)
    return {"BTC/USDT": {"last": 101.5, "high": 102, "low": 99, "change_pct": 1.2},
            "ETH/USDT": {"last": 3001.25, "high": 3100, "low": 2900, "change_pct": -0.5}}


class FakeEx:
    def __init__(self):
        self.calls = []
        self.fail_history = False

    def fetch_trades(self, symbol, limit=None, params=None):
        self.calls.append((symbol, limit, dict(params or {})))
        base_id = 10_000
        if not params:                                   # latest 500: ids 10000..10499
            ids = range(base_id, base_id + 500)
        else:
            if self.fail_history:
                raise ValueError("history down")
            after = int(params["after"])
            ids = range(max(9_500, after - 100), after)  # 100 older ones, nothing before id 9500
        out = [{"id": str(i), "timestamp": 1_700_000_000_000 + i * 250, "price": 100 + (i % 10) * 0.01,
                "amount": 0.5, "side": "buy" if i % 2 else "sell"} for i in ids]
        return out + out[:3]                             # the exchange sometimes repeats trades


EX = FakeEx()
pc.init_premium(exchange=EX, get_candles=get_candles,
                clean_coin=lambda raw: (str(raw or "").strip().upper() if str(raw or "").strip().upper() in COINS else None),
                is_forex=lambda s: s in FOREX, tickers=tickers, forex_price=lambda s: 1.08345 if s == "EUR/USD" else None)

iv = pc.parse_interval("7m")
day = utc(2026, 11, 6, 9)
df = get_candles("BTC/USDT", "1m", limit=300, since=day * 1000)
bars = pc.aggregate(df, iv)
first = bars[0]
check(first["time"] == pc.bucket_start(day, iv) == utc(2026, 11, 6, 8, 59), "bars start on a 7m bucket (08:59 = 77 x 7 minutes)")
check(all(b["time"] == pc.bucket_start(b["time"], iv) for b in bars), "every bar time is a bucket start")
b2 = bars[1]
secs = (pd.to_datetime(df["timestamp"]) - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)
rows = df[((secs >= b2["time"]) & (secs < b2["time"] + 420)).values]
check(b2["open"] == round(rows["open"].iloc[0], 10) and b2["close"] == round(rows["close"].iloc[-1], 10)
      and b2["high"] == round(rows["high"].max(), 10) and b2["low"] == round(rows["low"].min(), 10)
      and abs(b2["volume"] - rows["volume"].sum()) < 1e-9 and b2["n"] == 7, "open / high / low / close / volume of a 7m bar")

CALLS.clear()
out = pc.build_bars("BTC/USDT", iv, 300, now=NOW)
check(len(out["bars"]) == 300, "300 bars of 7m")
check(len(CALLS) > 1 and all(c[3] is not None for c in CALLS), "more than 300 base candles: paged with `since`")
check(out["bars"][-1]["time"] == pc.bucket_start(NOW, iv) and out["next_start"] == pc.bucket_end(out["bars"][-1]["time"], iv),
      "the newest bar is the forming one, next_start is when it closes")
gaps = {out["bars"][i + 1]["time"] - out["bars"][i]["time"] for i in range(299)}
check(gaps == {420, 300}, "bars 7 minutes apart, the last bar of a day is 5 minutes")
check("n" not in out["bars"][0], "the base-candle count is not sent")

CALLS.clear()
nat = pc.build_bars("BTC/USDT", pc.parse_interval("1h"), 200, now=NOW)
check(len(nat["bars"]) == 200 and CALLS == [("BTC/USDT", "1h", 200, None)], "native interval: one exchange call, as is")

older = pc.build_bars("BTC/USDT", iv, 50, before_ts=out["bars"][0]["time"], now=NOW)
check(older["bars"] and older["bars"][-1]["time"] < out["bars"][0]["time"]
      and pc.bucket_end(older["bars"][-1]["time"], iv) == out["bars"][0]["time"], "older page ends right before the oldest bar")

m6 = pc.build_bars("BTC/USDT", pc.parse_interval("3M"), 10, now=NOW)
check([time.gmtime(b["time"]).tm_mon for b in m6["bars"]][-3:] == [4, 7, 10], "3M bars are quarters")
check(pc.build_bars("BTC/USDT", pc.parse_interval("12M"), 5000, now=NOW)["limit"] == 200, "limit capped by the base-candle budget")

print("\n[4] trades for second and tick charts")
rows = pc.fetch_trades("BTC/USDT", depth=3)
ids = [int(r[4]) for r in rows]
check(len(rows) == 800 and ids == sorted(ids) and len(set(ids)) == 800, "latest 500 + 3 older pages of 100, no repeats, oldest first")
check(EX.calls[0] == ("BTC/USDT", 500, {}) and EX.calls[1][2] == {"method": "publicGetMarketHistoryTrades", "after": "10000"},
      "older pages ask for trades before the oldest id")
check(rows[0][3] in (1, -1) and isinstance(rows[0][0], int), "rows are [time_ms, price, amount, side, id]")
EX.calls.clear()
rows = pc.fetch_trades("BTC/USDT", depth=10)
check(len(rows) == 1000 and len(EX.calls) == 7, "stops when the exchange has no older trades")
EX.fail_history = True
check(len(pc.fetch_trades("BTC/USDT", depth=2)) == 500, "history failing still gives the latest trades")
EX.fail_history = False

print("\n[5] prices for many charts")
TICK_CALLS.clear()
p = pc.all_prices(["BTC/USDT", "ETH/USDT", "SOL/USDT", "EUR/USD", "XAU/USD"])
check(p["BTC/USDT"]["last"] == 101.5 and p["ETH/USDT"]["change_pct"] == -0.5, "crypto from one tickers call")
check("SOL/USDT" not in p and "XAU/USD" not in p and p["EUR/USD"] == {"last": 1.08345}, "missing prices left out, forex from its own source")
pc.all_prices(["BTC/USDT"])
check(len(TICK_CALLS) == 1, "the tickers call is shared for 2 seconds")

print("\n[6] routes")
app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"), static_folder=os.path.join(ROOT, "static"))
app.secret_key = "t"
app.context_processor(lambda: {"track_record_public": False, "ga_measurement_id": "", "support_email": "", "site_url": "",
                               "current_year": 2026, "is_admin": False})
app.register_blueprint(pc.premium_bp)
c = app.test_client()


def login(on=True):
    with c.session_transaction() as s:
        if on:
            s["user_id"] = 7
            s["email"] = "me@x.com"
        else:
            s.clear()


r = c.get("/premium-chart?coin=ETH/USDT&interval=7m&feature=replay")
check(r.status_code == 302 and r.headers["Location"] == "/chart?coin=ETH%2FUSDT&tf=7m&feature=replay",
      "the old workspace link opens the Live chart with the same coin, interval and feature")
for path in ("/api/premium/bars?coin=BTC/USDT&interval=7m", "/api/premium/trades?coin=BTC/USDT",
             "/api/premium/prices?coins=BTC/USDT", "/api/premium/bonds", "/api/premium/bonds/history?id=US10Y"):
    check(c.get(path).status_code == 401, f"{path.split('?')[0]} -> 401 for a visitor")
login()
r = c.get("/api/premium/bars?coin=BTC/USDT&interval=2h&limit=50")
d = r.get_json()
check(r.status_code == 200 and d["ok"] and len(d["bars"]) == 50 and d["interval"]["native"] == "2h", "bars for a native interval")
d = c.get("/api/premium/bars?coin=btc/usdt&interval=45m&limit=20").get_json()
check(d["ok"] and d["interval"]["base"] == "15m" and len(d["bars"]) == 20 and d["coin"] == "BTC/USDT", "custom 45m from 15m candles")
check(c.get("/api/premium/bars?coin=DOGE/USDT&interval=7m").status_code == 400, "unknown coin rejected")
check(c.get("/api/premium/bars?coin=BTC/USDT&interval=7x").status_code == 400, "unknown interval rejected")
r = c.get("/api/premium/bars?coin=BTC/USDT&interval=10s")
check(r.status_code == 400 and "trades" in r.get_json()["error"], "seconds are built from trades, not candles")
d = c.get("/api/premium/trades?coin=BTC/USDT&depth=2").get_json()
check(d["ok"] and len(d["trades"]) == 700, "trades endpoint")
r = c.get("/api/premium/trades?coin=EUR/USD")
check(r.status_code == 400 and "crypto" in r.get_json()["error"], "no trades for forex")
d = c.get("/api/premium/prices?coins=BTC/USDT,eth/usdt,DOGE/USDT,BTC/USDT,EUR/USD").get_json()
check(d["ok"] and set(d["prices"]) == {"BTC/USDT", "ETH/USDT", "EUR/USD"}, "prices: unknown coins and repeats dropped")
check(c.get("/api/premium/prices?coins=").status_code == 400, "prices need at least one coin")


def broken(**kw):
    raise ValueError("exchange down")


pc._hooks["get_candles"] = broken
key = ("bars", "BTC/USDT", "45m", 20, None)
pc._cache[key] = (pc._cache[key][0] - 60, pc._cache[key][1])          # the copy is old: a new fetch is due
r = c.get("/api/premium/bars?coin=BTC/USDT&interval=45m&limit=20")
check(r.status_code == 200 and r.get_json()["stale"] is True and len(r.get_json()["bars"]) == 20, "a failing exchange serves the last copy")
r = c.get("/api/premium/bars?coin=SOL/USDT&interval=13m")
check(r.status_code == 502 and "exchange down" not in r.get_data(as_text=True), "no copy: a safe error, no internals")
pc._hooks["get_candles"] = get_candles

print("\n[6b] the Premium chart Access button and its menu")
import re  # noqa: E402

with app.test_request_context("/"):
    from flask import render_template_string  # noqa: E402
    menu = render_template_string("{% set pa_id = 'pa-x' %}{% set pa_coin_from = '#coin-sym' %}{% include '_premium_access.html' %}")
items = re.findall(r'href="/chart\?feature=(\w+)" data-feature="\w+"', menu)
check(len(items) == 12 and len(set(items)) == 12, f"12 premium features in the menu ({', '.join(items)})")
for key in ("multi", "live", "custom", "range", "replay", "seconds", "pricebased", "spread", "export", "ticks", "customsec", "bonds"):
    check(key in items, f"menu item: {key}")
check(menu.count("Free now") == 2 and "Premium chart Access" in menu and 'data-coin-from="#coin-sym"' in menu,
      "button and menu carry the FREE NOW label and read the chart's coin")
for name in ("16 charts per tab", "200 parallel chart connections", "Custom intervals", "Custom range bars", "Bar replay",
             "Predefined second intervals", "Intraday price-based charts", "Spread charts", "Chart data export",
             "Tick-based intervals", "Custom second intervals", "Bond screener"):
    check(name in menu, f"menu names {name}")
tpl = lambda name: open(os.path.join(ROOT, "templates", name), encoding="utf-8").read()
check("include '_premium_access.html'" in tpl("chart.html") and "premium-access.js" in tpl("chart.html")
      and "premium-access.css" in tpl("chart.html") and 'pa_tf_from = "#tf-seg [aria-pressed=\'true\']"' in tpl("chart.html"),
      "Live chart: the button, its script and style, the chart's coin and timeframe")
check("include '_premium_access.html'" in tpl("dashboard.html") and "premium-access.js" in tpl("dashboard.html")
      and 'pa_coin_from = "#chart-title"' in tpl("dashboard.html"), "Home chart card: the button")
check("dataset.coin = sym" in open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read(), "Home chart title carries the coin")
check("/premium-chart" not in tpl("_app_nav.html") and not os.path.exists(os.path.join(ROOT, "templates", "premium-chart.html")),
      "no separate premium page: everything is on the Live chart")
ch = tpl("chart.html")
for id_ in ("iv-btn", "iv-pop", "type-body", "type-set", "replay-btn", "ch-replay", "layout-btn", "layout-pop", "ch-grid", "export-btn",
            "spread-btn", "bonds-btn", "export-all-btn"):
    check(f'id="{id_}"' in ch, f"Live chart has #{id_}")
check(ch.index("premium-chart-core.js") < ch.index("chart-premium.js") < ch.index("chart-page.js"), "Live chart loads the premium scripts first")
cp = open(os.path.join(ROOT, "static", "chart-page.js"), encoding="utf-8").read()
for name in ("function buildView", "function replayStart", "function exportMain", "function setSpread", "window.PremiumWorkspace = { feature }",
             "/api/premium/bars?coin=", "PR.Trades.sub(", "PR.Grid.init("):
    check(name in cp, f"chart-page.js: {name}")

print("\n[7] bond screener")
FIX = os.path.join(ROOT, "tests", "fixtures", "bonds")
read = lambda name: open(os.path.join(FIX, name), encoding="utf-8", errors="replace").read()
FILES = {"daily-treasury-rates.csv/2025": "us_2025.csv", "daily-treasury-rates.csv/2026": "us_2026.csv",
         "data-api.ecb.europa.eu": "ecb.csv", "bankofcanada.ca": "boc.json", "jgbcme_all.csv": "jgb_all.csv",
         "interest_rate/jgbcme.csv": "jgb_month.csv", "f2-data.csv": "rba_f2.csv"}
SEEN = []
DOWN = set()


def fake_get(url):
    SEEN.append(url)
    for key, name in FILES.items():
        if key in url:
            if any(d in url for d in DOWN):
                raise ConnectionError("source down")
            return read(name)
    raise ValueError("unexpected url " + url)


bonds._get = fake_get
bonds._cache.clear()
us = bonds.parse_us([read("us_2025.csv"), read("us_2026.csv")])
check(len(us) == 13 and us["10Y"][-1] == (date(2026, 11, 5), 4.84) and us["1M"][-1][1] == 4.03, "US Treasury: 13 maturities, newest yields")
jp = bonds.parse_jp([read("jgb_all.csv"), read("jgb_month.csv")])
check(jp["10Y"][-1] == (date(2026, 11, 5), 2.806) and "40Y" in jp, "Japan: the big file + the current month")
au = bonds.parse_au(read("rba_f2.csv"))
check(set(au) == {"2Y", "3Y", "5Y", "10Y"} and au["10Y"][-1] == (date(2026, 11, 4), 5.082), "Australia: table F2")
ca = bonds.parse_ca(read("boc.json"))
check(set(ca) == {"2Y", "3Y", "5Y", "7Y", "10Y", "30Y"} and ca["10Y"][-1][1] == 3.67, "Canada: benchmark bonds")
eu = bonds.parse_ecb(read("ecb.csv"))
check(set(eu) == {"2Y", "10Y"} and abs(eu["10Y"][-1][1] - 3.2938441917) < 1e-9, "Euro area: ECB AAA curve")
check(bonds.parse_jp(["Date,1Y\n2026/1/2,-\n"]) == {}, "a '-' (no bond of that maturity) is skipped")

s = bonds.screener()
check(s["ok"] and len(s["bonds"]) == 13 + 2 + 6 + 15 + 4 and all(x["ok"] for x in s["sources"]), "screener: every source, one row per bond")
row = [b for b in s["bonds"] if b["id"] == "US10Y"][0]
check(row["yield"] == 4.84 and row["chg_1d"] == -6.0 and row["bucket"] == "medium" and row["years"] == 10.0, "US 10Y row: yield, 1-day change in bp")
check(row["chg_1y"] is not None and row["hi_52w"] >= row["yield"] >= row["lo_52w"] and len(row["spark"]) <= 60, "1-year change, 52-week range, sparkline")
check(s["curves"]["US"][0][0] < s["curves"]["US"][-1][0], "yield curve sorted by maturity")
n = len(SEEN)
bonds.screener()
check(len(SEEN) == n, "cached: no new downloads")
d = c.get("/api/premium/bonds").get_json()
check(d["ok"] and len(d["bonds"]) == 40, "bonds endpoint")
h = c.get("/api/premium/bonds/history?id=jp10y").get_json()
check(h["ok"] and h["bond"]["id"] == "JP10Y" and h["points"][-1] == ["2026-11-05", 2.806], "bond history")
check(c.get("/api/premium/bonds/history?id=XX1Y").status_code == 404, "unknown bond -> 404")

bonds._cache.clear()
DOWN.add("bankofcanada.ca")
s = bonds.screener()
src = {x["cc"]: x for x in s["sources"]}
check(s["ok"] and not src["CA"]["ok"] and src["CA"]["error"] and src["US"]["ok"] and not any(b["cc"] == "CA" for b in s["bonds"]),
      "one source down: the others still show, the failure is reported")
DOWN.clear()
old = bonds._cache["US"]
bonds._cache["US"] = (old[0] - bonds.TTL - 10, old[1], None)
DOWN.add("treasury")
s = bonds.screener()
src = {x["cc"]: x for x in s["sources"]}
check(any(b["cc"] == "US" for b in s["bonds"]) and src["US"]["error"], "a failing source keeps its last good numbers")
DOWN.clear()
bonds._get = None

print("\n[8] the browser builds the same buckets as the server")
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

node = shutil.which("node")
if not node:
    print("  (node is not installed: skipped)")
else:
    names = ["7m", "45m", "3h", "5h", "25h", "1D", "2D", "7D", "1W", "2W", "1M", "3M", "6M", "12M", "7s", "15s"]
    stamps = [utc(2026, 11, 6, 23, 58, 31), utc(2026, 1, 1), utc(2025, 12, 31, 23, 59, 59), utc(2024, 2, 29, 13, 7, 3),
              utc(2026, 3, 9, 4, 44), 1_700_000_123]
    cases = [[ts, n] for ts in stamps for n in names]
    js = json.loads(subprocess.run([node, os.path.join(ROOT, "tests", "test_premium_chart_core.js"), "--buckets", json.dumps(cases)],
                                   capture_output=True, text=True, check=True).stdout)
    py = []
    for ts, n in cases:
        iv = pc.parse_interval(n)
        if iv.unit == "s":                       # seconds are only built in the browser; same rule as intraday
            b = ts - ts % 86400 + ((ts - (ts - ts % 86400)) // iv.n) * iv.n
            py.append([b, min(b + iv.n, b - b % 86400 + 86400)])
        else:
            b = pc.bucket_start(ts, iv)
            py.append([b, pc.bucket_end(b, iv)])
    bad = [(c, p, j) for c, p, j in zip(cases, py, js) if p != j]
    check(not bad, f"{len(cases)} bucket starts and ends match ({bad[:2]})")
    out = subprocess.run([node, os.path.join(ROOT, "tests", "test_premium_chart_core.js")], capture_output=True, text=True)
    check(out.returncode == 0 and "CHECKS PASSED" in out.stdout, "the browser-side tests pass (tests/test_premium_chart_core.js)")

print(f"\nALL {passed} CHECKS PASSED")
