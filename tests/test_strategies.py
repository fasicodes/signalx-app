"""Tests for strategy_library.py (the 30 strategies, the backtest) and strategies.py (library results, daily run,
community posts, likes, reports, moderation, pages). SQLite stand-in, fake candles, no network.
Run from the project root:  python tests/test_strategies.py
"""
import io
import json
import os
import sys
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(1, HERE)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from dbshim import setup  # noqa: E402

Conn = setup("strategies")
import db  # noqa: E402

db.get_db_connection = Conn
import strategy_library as SL  # noqa: E402
import strategies as ST  # noqa: E402

ST.get_db_connection = Conn
FAILS = []


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def candles(n=1600, seed=1, start="2025-01-01", freq="1h", price=100.0):
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.006, n) + np.sin(np.arange(n) / 90) * 0.0015
    c = price * np.exp(np.cumsum(ret))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.002, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.002, n)))
    return pd.DataFrame({"timestamp": pd.date_range(start, periods=n, freq=freq), "open": o, "high": h, "low": l,
                         "close": c, "volume": rng.uniform(100, 1000, n)})


# ---------------------------------------------------------------------------
print("\n[1] the library")
slugs = [s["slug"] for s in SL.LIBRARY]
check(len(SL.LIBRARY) == 30 and len(set(slugs)) == 30, "30 strategies with unique slugs")
check(all(s["category"] in SL.CATEGORIES for s in SL.LIBRARY), "every strategy has a known category")
check(all(len(s["name"]) > 5 and len(s["summary"]) > 30 and len(s["description"]) > 60 and len(s["rules"]) >= 2
          for s in SL.LIBRARY), "every strategy has a name, summary, description and at least two rules")
check(all(s["stop_atr"] > 0 and s["max_bars"] > 0 for s in SL.LIBRARY), "every strategy has a stop and a time limit")
check(all(not any(ch in s["summary"] + s["description"] + "".join(s["rules"]) for ch in "؀ا") for s in SL.LIBRARY),
      "texts are English")

# ---------------------------------------------------------------------------
print("\n[2] no look-ahead: a signal never changes when later candles arrive")
d_full = SL.prepare(candles(1500, seed=7))
bad = []
for spec in SL.LIBRARY:
    full = spec["fn"](d_full)
    for k in (700, 1000, 1499):
        part = spec["fn"](d_full.iloc[:k].reset_index(drop=True))
        for key in ("le", "se", "lx", "sx"):
            if not np.array_equal(full[key][:k], part[key][:k]):
                bad.append(f"{spec['slug']}:{key}@{k}")
check(not bad, "signals use only candles that had closed" + (f" (differences: {bad[:6]})" if bad else ""))
counts = {s["slug"]: int(np.sum(s["fn"](d_full)["le"]) + np.sum(s["fn"](d_full)["se"])) for s in SL.LIBRARY}
quiet = [k for k, v in counts.items() if v == 0 and k not in ("golden-cross-50-200", "bollinger-squeeze-breakout")]
check(not quiet, f"every strategy finds entries on 1,500 candles (none: {quiet})")

# ---------------------------------------------------------------------------
print("\n[3] the backtest rules")
n = 30
ts = pd.date_range("2026-01-01", periods=n, freq="h")
base = pd.DataFrame({"timestamp": ts, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1.0})
base["atr"] = 1.0
spec = {"slug": "t", "stop_atr": 2.0, "max_bars": 5, "reverse": False}


def sig(le=(), se=(), lx=(), sx=()):
    out = {k: np.zeros(n, dtype=bool) for k in ("le", "se", "lx", "sx")}
    for k, idx in (("le", le), ("se", se), ("lx", lx), ("sx", sx)):
        out[k][list(idx)] = True
    return out


d = base.copy()
d.loc[3, "low"] = 97.5                                   # long from candle 1 (stop 98) is stopped at candle 3
tr = SL.simulate("BTC/USDT", d, sig(le=[1]), spec, 1, 3600)
cost = SL.CRYPTO_COST if hasattr(SL, "CRYPTO_COST") else 0.0012
check(len(tr) == 1 and tr[0]["why"] == "stop" and tr[0]["exit"] == 98.0 and abs(tr[0]["r"] - (-1 - 0.0012 * 100 / 2)) < 1e-9,
      "stop: exit at the stop, -1R minus costs (crypto 0.12% of the position)")
d = base.copy()
d.loc[2, "open"] = 97.0
d.loc[2, "low"] = 96.5                                   # opens beyond the stop -> exits at the worse open
tr = SL.simulate("BTC/USDT", d, sig(le=[1]), spec, 1, 3600)
check(tr and tr[0]["exit"] == 97.0 and tr[0]["why"] == "stop", "a candle that opens beyond the stop exits at its open")
d = base.copy()
d.loc[4, "close"] = 103.0
d.loc[4, "high"] = 103.2
tr = SL.simulate("BTC/USDT", d, sig(le=[1], lx=[4]), spec, 1, 3600)
check(tr and tr[0]["why"] == "exit" and tr[0]["j"] == 4 and abs(tr[0]["r"] - (1.5 - 0.0012 * 100 / 2)) < 1e-9,
      "exit rule: exit at that candle's close (+1.5R before costs)")
d = base.copy()
d.loc[3, "low"] = 97.0
d.loc[3, "close"] = 101.0
tr = SL.simulate("BTC/USDT", d, sig(le=[1], lx=[3]), spec, 1, 3600)
check(tr and tr[0]["why"] == "stop", "stop and exit in the same candle: the stop counts first")
tr = SL.simulate("BTC/USDT", base, sig(le=[1]), spec, 1, 3600)
check(tr and tr[0]["why"] == "time" and tr[0]["j"] == 6, "time limit: closes after max_bars candles")
tr = SL.simulate("BTC/USDT", base, sig(le=[1, 2, 3]), spec, 1, 3600)
check(len(tr) == 1 and tr[0]["i"] == 1, "one position at a time per symbol (entries while a trade is open are ignored)")
tr = SL.simulate("BTC/USDT", base, sig(le=[1, 9]), spec, 1, 3600)
check(len(tr) == 2 and tr[1]["i"] == 9, "a new entry after the trade has closed")
rspec = dict(spec, reverse=True, max_bars=50)
tr = SL.simulate("BTC/USDT", base, sig(le=[2], se=[6], lx=[]), rspec, 1, 3600)
check(len(tr) >= 1 and tr[0]["why"] == "reverse" and tr[0]["j"] == 6 and (len(tr) == 1 or tr[1]["side"] == "SHORT"),
      "reversal strategies: the opposite signal closes and turns the trade around")
tr = SL.simulate("BTC/USDT", base, sig(le=[27]), spec, 1, 3600)
check(tr == [], "a trade still open at the end of the data is not counted")
fx = base.copy()
fx[["open", "high", "low", "close"]] = 1.1
fx["high"] = 1.1005
fx["low"] = 1.0995
fx["atr"] = 0.001
tr = SL.simulate("EUR/USD", fx, sig(le=[1]), spec, 1, 3600)
check(tr and abs(tr[0]["r"] - (0 - 0.00012 / 0.002)) < 1e-9, "forex costs: the pair's spread in R")

r = [{"symbol": "A", "side": "LONG", "r": x, "bars": 3, "exit_time": np.datetime64("2026-01-01") + np.timedelta64(i, "h")}
     for i, x in enumerate([1.0, -1.0, -1.0, 2.0, -0.5])]
s = SL.stats(r, 10.0, 1)
check(s["trades"] == 5 and s["win_rate"] == 40.0 and s["avg_r"] == 0.1 and s["total_r"] == 0.5
      and s["profit_factor"] == 1.2 and s["max_drawdown_r"] == -2.0 and s["trades_per_day"] == 0.5,
      "stats: win rate, average, total, profit factor, drawdown, trades a day")
check(s["curve"][-1][1] == 0.5 and len(s["curve"]) == 5, "stats: equity curve of the cumulative result")

h1 = candles(30, seed=2, start="2026-01-01 00:00")
h4 = SL.resample(h1, "4h")
check(len(h4) == 7 and h4["timestamp"].iloc[-1] == pd.Timestamp("2026-01-02 00:00") and
      abs(h4["high"].iloc[0] - h1["high"].iloc[:4].max()) < 1e-12, "4h candles from 1h: UTC buckets, the unfinished one dropped")

# ---------------------------------------------------------------------------
print("\n[4] all strategies on several symbols")
frames = {"crypto": {"1h": {}, "4h": {}}, "forex": {"1h": {}, "4h": {}}}
for k, sym in enumerate(["BTC/USDT", "ETH/USDT", "SOL/USDT"]):
    raw = candles(3000, seed=10 + k, start="2025-06-01")
    frames["crypto"]["1h"][sym] = SL.prepare(raw)
    frames["crypto"]["4h"][sym] = SL.prepare(SL.resample(raw, "4h"))
start = datetime(2025, 7, 15)
lib = SL.compute_all(frames, start, datetime(2025, 10, 6))
check(set(lib) == set(slugs), "a result for every strategy")
one = lib["ema-crossover-9-21"]
check(one["results"]["crypto_1h"]["trades"] > 0 and one["results"]["forex_1h"].get("error"),
      "results per market and timeframe; a market without candles says so")
check(one["headline"] and one["headline"]["key"] == "crypto_4h", "the card shows the crypto 4h result")
pv = one["preview"]
check(pv and len(pv["c"]) == SL.PREVIEW_BARS and "EMA 9" in pv["overlays"] and all(0 <= m[0] < SL.PREVIEW_BARS for m in pv["entries"]),
      "chart data: last 140 candles, the strategy's lines, entries inside the window")
json.dumps(lib)
check(True, "results are JSON")

# ---------------------------------------------------------------------------
print("\n[5] pages and API (seed until the first run)")
from flask import Flask, session  # noqa: E402

app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"), static_folder=os.path.join(ROOT, "static"))
app.secret_key = "x" * 32
app.register_blueprint(ST.strategies_bp)
ADMINS = {"boss@example.com"}
app.context_processor(lambda: {"is_admin": session.get("email") in ADMINS, "track_record_public": False})
ST.init_strategies(app, is_admin=lambda: session.get("email") in ADMINS, start=False)


@app.route("/login")
def login_page():
    return "login"


def client(uid=None, email=None):
    c = app.test_client()
    if uid:
        with c.session_transaction() as s:
            s["user_id"] = uid
            s["email"] = email or f"u{uid}@example.com"
    return c


guest = client()
r = guest.get("/strategies")
check(r.status_code == 200 and b"Trading strategies, tested on real data" in r.data, "/strategies is public")
r = guest.get("/strategies/rsi-30-70")
check(r.status_code == 200 and b"RSI 30 / 70 reversal" in r.data and b"Backtested" in r.data and b"By Signals FM" in r.data,
      "library page: name and the By Signals FM / Backtested labels")
check(guest.get("/strategies/not-a-strategy").status_code == 404, "unknown strategy -> 404 page")
d = guest.get("/api/strategies?tab=library").get_json()
check(d["ok"] and d["total"] == 30 and d["meta"]["source"] in ("seed", None), "API: 30 library cards (seed until the first run)")
item = d["items"][0]
check(item["badge"] == "By Signals FM" and item["tested"] is True and item["url"].startswith("/strategies/"),
      "library cards carry By Signals FM and tested = true")
d2 = guest.get("/api/strategies?tab=library&category=Mean%20reversion").get_json()
check(d2["total"] == 7 and all(x["category"] == "Mean reversion" for x in d2["items"]), "category filter")
d3 = guest.get("/api/strategies?tab=library&q=bollinger").get_json()
check(d3["total"] == 2, "search")
d4 = guest.get("/api/strategies?tab=library&sort=best").get_json()
vals = [x["headline"]["avg_r"] for x in d4["items"] if x.get("headline")]
check(vals == sorted(vals, reverse=True), "sort by best backtest result")
d5 = guest.get("/api/strategies/library/macd-signal-crossover").get_json()
check(d5["ok"] and d5["strategy"]["results"]["crypto_4h"]["trades"] > 0 and d5["strategy"]["rules"], "library detail API")
check(guest.get("/strategies/new").status_code == 302, "sharing needs a login")

# ---------------------------------------------------------------------------
print("\n[6] community posts")
alice, bob = client(1), client(2)
good = {"title": "London open breakout with EMA filter", "summary": "Trade the first London hour range break in the "
        "direction of the 50 EMA.", "description": "The idea is simple. " * 6, "rules": "Long: break above the range.\n"
        "Short: break below.", "market": "forex", "timeframe": "15m", "category": "Breakout", "author": "Alice T."}
r = guest.post("/api/strategies/community", data=good)
check(r.status_code == 401, "posting needs a login")
r = alice.post("/api/strategies/community", data={**good, "description": "Join https://example.com for more. " * 4})
check(r.status_code == 400 and "Links" in r.get_json()["error"], "links are refused")
r = alice.post("/api/strategies/community", data={**good, "description": "Write me at me@mail.com please. " * 4})
check(r.status_code == 400, "e-mail addresses are refused")
r = alice.post("/api/strategies/community", data={**good, "title": "Hi"})
check(r.status_code == 400 and "title" in r.get_json()["error"], "too short a title is refused")
r = alice.post("/api/strategies/community", data={**good, "market": "stocks"})
check(r.status_code == 400, "unknown market is refused")
png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
r = alice.post("/api/strategies/community", data={**good, "image": (io.BytesIO(b"GIF89a" + b"\x00" * 50), "x.gif")},
               content_type="multipart/form-data")
check(r.status_code == 400 and "PNG" in r.get_json()["error"], "only PNG, JPEG or WebP images")
r = alice.post("/api/strategies/community", data={**good, "image": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1_600_000), "big.png")},
               content_type="multipart/form-data")
check(r.status_code == 400 and "1.5 MB" in r.get_json()["error"], "images over 1.5 MB are refused")
r = alice.post("/api/strategies/community", data={**good, "title": "<script>alert(1)</script> breakout",
                                                    "image": (io.BytesIO(png), "chart.png")}, content_type="multipart/form-data")
check(r.status_code == 200 and r.get_json()["ok"], "a valid post with an image is published")
pid = r.get_json()["id"]
page = guest.get(f"/strategies/community/{pid}")
check(page.status_code == 200 and b"By a user" in page.data and b"Not tested" in page.data, "community page: By a user / Not tested")
check(b"<script>alert(1)</script>" not in page.data and b"&lt;script&gt;" in page.data, "user text is escaped (no script runs)")
img = guest.get(f"/strategies/image/{pid}")
check(img.status_code == 200 and img.mimetype == "image/png" and img.data == png, "the chart image is served as image/png")
lst = guest.get("/api/strategies?tab=community").get_json()
check(lst["total"] == 1 and lst["items"][0]["badge"] == "By a user" and lst["items"][0]["tested"] is False
      and "description" not in lst["items"][0], "community cards: By a user, tested = false, no full text in the list")
all_items = guest.get("/api/strategies").get_json()
check(all_items["total"] == 31, "the All tab has both kinds")
check(guest.get("/api/strategies?tab=community&market=forex").get_json()["total"] == 1 and
      guest.get("/api/strategies?tab=community&market=crypto").get_json()["total"] == 0, "market filter on community posts")
for k in range(2):
    alice.post("/api/strategies/community", data={**good, "title": f"Second idea number {k}"})
r = alice.post("/api/strategies/community", data={**good, "title": "One too many today"})
check(r.status_code == 429, "at most 3 posts a day per user")

# likes
r = bob.post("/api/strategies/like", json={"target": "lib:rsi-30-70"})
check(r.get_json() == {"ok": True, "liked": True, "likes": 1}, "like a library strategy")
r = bob.post("/api/strategies/like", json={"target": "lib:rsi-30-70"})
check(r.get_json()["liked"] is False and r.get_json()["likes"] == 0, "like again = unlike")
check(bob.post("/api/strategies/like", json={"target": "lib:nope"}).status_code == 404, "unknown target -> 404")
check(guest.post("/api/strategies/like", json={"target": "lib:rsi-30-70"}).status_code == 401, "liking needs a login")
bob.post("/api/strategies/like", json={"target": f"com:{pid}"})
check(guest.get(f"/api/strategies?tab=community&sort=popular").get_json()["items"][0]["id"] == pid, "popular sort uses likes")

# reports and moderation
check(alice.post(f"/api/strategies/community/{pid}/report").status_code == 400, "authors cannot report their own post")
for u in (2, 3):
    client(u).post(f"/api/strategies/community/{pid}/report")
client(3).post(f"/api/strategies/community/{pid}/report")
check(ST._post_row(pid)["status"] == "visible", "one report per user (2 different users so far)")
client(4).post(f"/api/strategies/community/{pid}/report")
check(ST._post_row(pid)["status"] == "hidden", "3 reports hide the post")
check(guest.get(f"/strategies/community/{pid}").status_code == 404, "hidden: not shown to visitors")
check(alice.get(f"/strategies/community/{pid}").status_code == 200, "hidden: the author still sees it")
check(guest.get(f"/strategies/image/{pid}").status_code == 404, "hidden: image not served to visitors")
admin = client(5, "boss@example.com")
check(bob.post(f"/api/strategies/community/{pid}/moderate", json={"action": "restore"}).status_code == 403, "moderation is for admins")
r = admin.post(f"/api/strategies/community/{pid}/moderate", json={"action": "restore"})
check(r.get_json()["ok"] and ST._post_row(pid)["status"] == "visible" and ST._post_row(pid)["reports"] == 0,
      "an admin restores it (reports reset)")
check(bob.post(f"/api/strategies/community/{pid}/delete").status_code == 403, "others cannot delete a post")
check(alice.post(f"/api/strategies/community/{pid}/delete").get_json()["ok"], "the author deletes it")
check(guest.get(f"/strategies/community/{pid}").status_code == 404 and ST._post_row(pid)["status"] == "deleted",
      "deleted posts are gone")
check(alice.get("/strategies/new").status_code == 200, "the share form opens for members")

# ---------------------------------------------------------------------------
print("\n[7] the daily run")
NOW = datetime(2026, 3, 1, 10, 5)
DATA = {sym: candles(24 * 460, seed=30 + i, start=NOW - timedelta(days=460)) for i, sym in
        enumerate(["BTC/USDT", "ETH/USDT", "EUR/USD"])}
DATA["EUR/USD"][["open", "high", "low", "close"]] /= 90.0
calls = []


def fake_get(symbol, timeframe, limit, since=None):
    calls.append((symbol, since is not None))
    d = DATA.get(symbol)
    if d is None:
        raise ValueError("no such market")
    d = d[d["timestamp"] <= pd.Timestamp(NOW)]
    if since is None:
        return d.tail(limit).reset_index(drop=True)
    return d[d["timestamp"] >= pd.to_datetime(int(since), unit="ms")].head(min(limit, 300)).reset_index(drop=True)


os.environ["STRATEGIES_COINS"] = "BTC/USDT,ETH/USDT,XYZ/USDT"
os.environ["STRATEGIES_FOREX"] = "EUR/USD"
ST._hooks["get_candles"] = fake_get
ST.time.sleep = lambda s: None
check(ST._due(NOW), "a run is due while only the seed is shown")
meta = ST.refresh(NOW)
check(meta["source"] == "live" and meta["coins"] == ["BTC/USDT", "ETH/USDT"] and meta["forex"] == ["EUR/USD"]
      and any("XYZ" in m for m in meta["missing"]), "run: coins and forex loaded, a missing market is listed")
res = ST.library_results(max_age=0)
check(res["_meta"]["source"] == "live" and res["rsi-30-70"]["results"]["forex_4h"].get("trades", 0) >= 0
      and not res["rsi-30-70"]["results"]["crypto_1h"].get("error"), "results stored (crypto and forex)")
check(not ST._due(NOW + timedelta(hours=1)) and ST._due(NOW + timedelta(hours=25)), "next run after 24 hours")
n_before = len(calls)
ST.refresh(NOW + timedelta(hours=25))
inc = [c for c in calls[n_before:] if c[0] == "BTC/USDT"]
check(1 <= len(inc) <= 2, f"the next run fetches only the new candles ({len(inc)} call(s) for BTC)")
d = guest.get("/api/strategies?tab=library").get_json()
check(d["meta"]["source"] == "live" and d["meta"]["coins"] == 2, "the page shows the live run")
ST._hooks["get_candles"] = lambda **kw: (_ for _ in ()).throw(ValueError("exchange down"))
try:
    ST.refresh(NOW + timedelta(hours=50))
    failed = False
except Exception:
    failed = True
m = ST.library_results(max_age=0)["_meta"]
check(failed and m.get("last_error") and m["source"] == "live", "a failed run keeps the last good results")
check(not ST._due(NOW + timedelta(hours=50, minutes=20)), "after a failed run the next try waits an hour")

print()
print(f"{len(FAILS)} failed" if FAILS else "ALL PASSED")
sys.exit(1 if FAILS else 0)
