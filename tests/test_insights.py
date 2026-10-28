"""Tests for insights.py (pure maths, database part with the SQLite shim, routes). No network.
Run from the project root:  python tests/test_insights.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from dbshim import setup  # noqa: E402

os.environ["MODELS_DIR"] = tempfile.mkdtemp(prefix="models_")
Conn = setup("insights")

import signal_hub as hub  # noqa: E402
import insights as I  # noqa: E402

hub.get_db_connection = Conn

FAILS = []


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def near(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


# --------------------------------------------------------------------------- 5. confidence ranges
lo, hi = I.wilson(60, 100)
check(near(lo, 50.2, 0.2) and near(hi, 69.1, 0.2), f"wilson 60/100 -> about 50.2..69.1 ({lo}..{hi})")
check(I.wilson(0, 0) == (None, None), "wilson with no signals -> None")
lo2, hi2 = I.wilson(600, 1000)
check((hi2 - lo2) < (hi - lo) / 2, "more signals -> narrower range")
m, a, b, sd = I.mean_ci([1, -1, 1, 1, -1, 1, 1, -1, 1, 1])
check(near(m, 0.4) and a < m < b and sd > 0, "mean_ci: mean inside its range")
check(I.mean_ci([]) == (None, None, None, None), "mean_ci empty")
check(I.signals_needed(-0.1, 1.0) is None and I.signals_needed(0.2, 1.0) == 97, "signals needed: (1.96*1/0.2)^2 = 97")

few = I.sure_summary([1.0] * 5 + [-1.0] * 3, 5)
check(few["verdict"] == "too_early" and few["n"] == 8, "under 30 signals -> too early")
rng = np.random.default_rng(1)
good = list(rng.choice([1.0, -1.0], size=800, p=[0.6, 0.4]))
sg = I.sure_summary(good, sum(1 for x in good if x > 0))
check(sg["verdict"] == "positive" and sg["avg_low"] > 0, "800 signals at 60% -> proven positive")
bad = list(rng.choice([1.0, -1.0], size=800, p=[0.4, 0.6]))
check(I.sure_summary(bad, sum(1 for x in bad if x > 0))["verdict"] == "negative", "800 signals at 40% -> negative")
thin = list(rng.choice([1.0, -1.0], size=60, p=[0.55, 0.45]))
sv = I.sure_summary(thin, sum(1 for x in thin if x > 0))
check(sv["verdict"] in ("likely_positive", "unclear", "positive") and (sv["signals_needed"] is None or sv["signals_needed"] > 60),
      "thin edge on 60 signals is not called proven")

# --------------------------------------------------------------------------- 7. live against the test
d = I.drift(sg, {"avg_net_r": 0.2, "win_rate": 60, "signals": 800})
check(d["status"] == "in_line", f"drift: in line when expected is inside the range ({d['status']})")
check(I.drift(sg, {"avg_net_r": 0.9, "win_rate": 90, "signals": 100})["status"] == "below", "drift: live clearly below the test")
check(I.drift(sg, {"avg_net_r": -0.5})["status"] == "above", "drift: live above the test")
check(I.drift(few, {"avg_net_r": 0.2})["status"] == "too_early", "drift: too early")
check(I.drift(sg, None)["status"] == "no_test", "drift: no test published")

# --------------------------------------------------------------------------- 9. sessions
check(I.session_of(3).startswith("Asia") and I.session_of(8).startswith("London") and I.session_of(12).startswith("New York")
      and I.session_of(22).startswith("Late"), "sessions by hour")
tr = [{"t": datetime(2026, 1, 1, 8), "r": 1.0, "win": True}] * 3 + [{"t": datetime(2026, 1, 1, 9), "r": -1.0, "win": False}]
sess = {s["label"][:6]: s for s in I.by_session(tr)}
check(sess["London"]["n"] == 4 and sess["London"]["win_rate"] == 75.0 and sess["Asia ("]["n"] == 0, "session table counts")

# --------------------------------------------------------------------------- 6. market condition
n = 400
close = 100 * np.exp(np.cumsum(np.concatenate([np.full(200, 0.004), np.full(200, -0.004)]) + rng.normal(0, 0.001, n)))
ts = pd.date_range("2026-01-01", periods=n, freq="4h")
df = pd.DataFrame({"timestamp": ts, "open": close, "high": close * 1.002, "low": close * 0.998, "close": close, "volume": 1.0})
lab = I.regime_labels(df)
check(set(lab["trend"]) <= {"Uptrend", "Downtrend", "Sideways"} and (lab["trend"] == "Uptrend").sum() > 50
      and (lab["trend"] == "Downtrend").sum() > 50, "regime labels find the up and the down leg")
t_up = {"t": ts[150].to_pydatetime() + timedelta(hours=4), "r": 1.0, "win": True}
t_dn = {"t": ts[350].to_pydatetime() + timedelta(hours=4), "r": -1.0, "win": False}
t_old = {"t": datetime(2020, 1, 1), "r": 1.0, "win": True}
I.attach_regimes([t_up, t_dn, t_old], lab)
check(t_up.get("trend") == "Uptrend" and t_dn.get("trend") == "Downtrend" and "trend" not in t_old,
      "trades get the label of the last closed bar; trades outside the candles stay unlabelled")
br = I.by_regime([t_up, t_dn, t_old])
check(br["labelled"] == 2 and {x["label"]: x["n"] for x in br["trend"]}["Uptrend"] == 1, "regime table")

# --------------------------------------------------------------------------- 10. streaks and drawdown
s = I.streaks([1, -1, -1, -1, 1, 1, -1, -1])
check(s["longest_losing"] == 3 and s["current_losing"] == 2 and s["max_drawdown_r"] == -3.0, f"streaks {s}")
check(I.streaks([1, 1, 1])["current_losing"] == 0, "no current losing run after a win")
check(near(I.prob_run(0.5, 2, 2), 0.25) and near(I.prob_run(0.5, 3, 2), 0.375), "prob_run matches hand calculation")
check(I.prob_run(0.4, 100, 3) > I.prob_run(0.4, 100, 7) > I.prob_run(0.4, 100, 10), "longer runs are rarer")
check(I.prob_run(0.4, 2, 3) == 0.0 and I.prob_run(0.0, 50, 3) == 0.0, "impossible runs")
bd = I.drawdown_bootstrap(good)
check(bd and bd["p95"] >= bd["median"] > 0, f"drawdown bootstrap {bd}")
check(I.drawdown_bootstrap([1, -1]) is None, "bootstrap needs 10+ results")
sb = I.streak_block(good)
check(sb["available"] and sb["chance"][0]["pct"] > sb["chance"][-1]["pct"], "streak block")
check(not I.streak_block([1, 2, 3])["available"], "streak block with few results")

# --------------------------------------------------------------------------- 3. clusters
ne, avg = I.effective_bets(np.eye(3), [1, 1, 1])
check(near(ne, 3.0) and near(avg, 0.0), "three unrelated longs = 3 bets")
ne, avg = I.effective_bets(np.ones((3, 3)), [1, 1, 1])
check(near(ne, 1.0) and near(avg, 1.0), "three identical longs = 1 bet")
ne, _ = I.effective_bets(np.ones((2, 2)), [1, -1])
check(ne == 2.0, "a long and a short on the same coin offset (clamped to n)")
check(I.effective_bets(np.eye(1), [1]) == (1.0, 0.0) and I.effective_bets([], []) == (0.0, 0.0), "edge cases")
x = rng.normal(0, 0.01, 200)
ret = pd.DataFrame({"AAA/USDT": x, "BBB/USDT": x * 0.9 + rng.normal(0, 0.002, 200), "CCC/USDT": rng.normal(0, 0.01, 200)})
cb = I.cluster_block([("AAA/USDT", "LONG"), ("BBB/USDT", "LONG"), ("CCC/USDT", "LONG")], ret)
check(cb["available"] and 1.6 < cb["n_eff"] < 2.4 and cb["risk_multiplier"] > 1.2, f"cluster: two linked + one free ~2 bets ({cb.get('n_eff')})")
check(I.cluster_block([], None)["open"] == 0, "cluster: nothing open")
cb1 = I.cluster_block([("AAA/USDT", "LONG"), ("BBB/USDT", "SHORT")], None)
check(cb1["n_eff"] is None and "same way" in cb1["text"] or cb1["open"] == 2, "cluster: no price history -> counts only")

# --------------------------------------------------------------------------- 2. liquidity
bids = [[99.9, 10], [99.8, 100], [99.0, 1000]]
asks = [[100.1, 10], [100.2, 100], [101.0, 1000]]
check(near(I.walk_book(asks, 500)[0], 100.1) and near(I.walk_book(asks, 500)[1], 500), "walk_book small order at best ask")
avg_p, filled = I.walk_book(asks, 3000)
check(100.1 < avg_p < 100.5 and near(filled, 3000), "walk_book bigger order moves up the book")
c_small, c_big = I.book_cost("LONG", bids, asks, 500), I.book_cost("LONG", bids, asks, 5000)
check(near(c_small["bps"], 10.0, 0.2) and c_big["bps"] > c_small["bps"], f"book cost grows with size ({c_small['bps']} -> {c_big['bps']} bps)")
check(near(I.book_cost("SHORT", bids, asks, 500)["bps"], 10.0, 0.2), "short side uses the bids")
thin_fill = I.book_cost("LONG", bids, asks, 5_000_000)
check(thin_fill["filled_pct"] < 100, "order bigger than the book is reported as partly filled")
check(I.book_cost("LONG", [], asks, 100) is None, "empty book")
cr = I.cost_in_r(100.0, 98.0, 5.0)     # stop 2% away; fees 0.1% + slippage 0.1% = 0.2% -> 0.1 R
check(near(cr, 0.1, 1e-3), f"cost in R = 0.1 ({cr})")
check(I.cost_in_r(100, 100, 5) is None, "cost in R without a stop distance")

# --------------------------------------------------------------------------- 4. position size
p = I.position_size(10000, 1, 100.0, 98.0, fee_pct=0.05)
check(p["ok"] and p["side"] == "LONG" and near(p["risk_amount"], 100.0), "position size: 1% of 10,000 is 100")
check(near(p["units"] * (2.0 + 100 * 0.001), 100.0, 1e-6), "units x (stop distance + fees) = risk amount")
check(near(p["notional"], p["units"] * 100.0, 0.01) and near(p["leverage_needed"], p["notional"] / 10000, 0.01), "notional and leverage needed")
ps = I.position_size(10000, 1, 100.0, 103.0, leverage=5)
check(ps["side"] == "SHORT" and ps["margin"] == round(ps["notional"] / 5, 2), "short side and margin")
check(not I.position_size(0, 1, 100, 98)["ok"] and not I.position_size(1000, 20, 100, 98)["ok"]
      and not I.position_size(1000, 1, 100, 100)["ok"] and not I.position_size(1000, "x", 100, 98)["ok"], "bad inputs are refused")
check(any("leverage" in w for w in I.position_size(1000, 5, 100, 99.5)["warnings"]), "size above the balance warns about leverage")
check(any("0.5%" in w for w in I.position_size(1000, 5, 100, 90)["warnings"]), "risk above 2% warns")
sr = I.suggest_risk(1.0, 6, cap_pct=3.0)
check(sr["per_signal"] == 0.5 and near(sr["worst_case"], 3.0), "suggested risk keeps the total under the cap")
check(I.suggest_risk(1.0, 2)["per_signal"] == 1.0 and I.suggest_risk(1.0, 0)["per_signal"] == 1.0, "suggested risk never raises the risk")

# --------------------------------------------------------------------------- 1. funding / open interest
fs = I.funding_state(0.08, "LONG")
check(fs["crowd"] == "longs_crowded" and "crowded" in fs["text"] and fs["you_pay"] is True, "high funding + long = crowded, you pay")
check("squeeze" in I.funding_state(-0.05, "LONG")["text"], "negative funding helps a long (squeeze)")
check("flush" in I.funding_state(0.06, "SHORT")["text"], "positive funding helps a short")
check(I.funding_state(0.0, "LONG")["crowd"] == "neutral" and I.funding_state(None, "LONG")["crowd"] is None, "neutral and missing funding")
check(near(I.funding_state(0.01, "LONG")["year_pct"], 10.95, 0.1), "funding per year")
I._oi_hist.clear()
t0 = 1_000_000.0
I.remember_oi("BTC/USDT", 100.0, now=t0)
check(I.oi_change("BTC/USDT", now=t0 + 60) is None, "no open-interest change before there is history")
I.remember_oi("BTC/USDT", 110.0, now=t0 + 3600)
oc = I.oi_change("BTC/USDT", now=t0 + 3600)
check(oc and near(oc["pct"], 10.0) and oc["hours"] == 1.0, f"open-interest change from history ({oc})")
I.remember_oi("BTC/USDT", 111.0, now=t0 + 3700)
check(len(I._oi_hist["BTC/USDT"]) == 2, "snapshots are not stored more often than every 10 minutes")

# --------------------------------------------------------------------------- database part
hub.init_tables()
base = datetime(2026, 9, 1, 0, 0)


def add(i, status, r, sym="BTC/USDT", tf="4h", mk="crypto", side="LONG", fresh=False):
    bt = (datetime.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)) if fresh else base + timedelta(hours=4 * i)
    with hub._Cursor() as cur:
        cur.execute("""INSERT INTO signal_events (symbol, side, bar_time, entry, stop_loss, take_profit, confidence, created_at,
                       market, timeframe, status, r_multiple, closed_at, grade, expires_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (sym, side, bt, 100.0, 98.0, 102.0, 0.7, bt, mk, tf, status, r, bt + timedelta(hours=8) if r is not None else None,
                     "B", bt + timedelta(hours=48)))


for i in range(40):
    add(i, "TP1" if i % 5 < 3 else "SL", 1.0 if i % 5 < 3 else -1.0, sym="ETH/USDT")
add(100, "ACTIVE", None, sym="BTC/USDT", side="LONG", fresh=True)
add(101, "ACTIVE", None, sym="ETH/USDT", side="LONG", fresh=True)
add(102, "ACTIVE", None, sym="EUR/USD", tf="1h", mk="forex", side="SHORT", fresh=True)
tr_live = I.live_trades("crypto", "4h")
check(len(tr_live) == 40 and all(t["r"] is not None for t in tr_live), "live trades: only closed signals with a result")
check(tr_live[0]["t"] == base + timedelta(hours=4), "trade time = candle open + timeframe (when the signal came)")
labels = I.regime_labels(df.assign(timestamp=pd.date_range("2026-07-01", periods=n, freq="4h")))
I._hooks["get_candles"] = lambda sym, tf, limit: df.assign(timestamp=pd.date_range("2026-07-01", periods=n, freq="4h"))
I._cache.clear()
perf = I.performance("crypto", "4h")
check(perf["live"]["sure"]["n"] == 40 and perf["live"]["sure"]["win_rate"] == 60.0, "performance: 40 closed signals at 60%")
check(perf["live"]["sure"]["verdict"] in ("positive", "likely_positive", "unclear"), "performance: verdict present")
check(sum(s["n"] for s in perf["live"]["sessions"]) == 40, "performance: every signal in one session")
check(perf["regime_available"] and perf["live"]["regimes"]["labelled"] > 0, "performance: market condition table is filled")
check(perf["live"]["streaks"]["available"] and perf["live"]["streaks"]["longest_losing"] == 2, "performance: streaks")
check(perf["drift"]["status"] in ("in_line", "below", "above", "too_early", "no_test"), "performance: drift status")
perf_fx = I.performance("forex", "15m")
check(perf_fx["live"]["sure"]["n"] == 0 and perf_fx["live"]["sure"]["verdict"] == "too_early", "performance: stream without signals is 'too early', no crash")
check(perf_fx["live"]["streaks"]["available"] is False, "performance: no streak numbers without results")

# live view with fake sources
I._cache.clear()
I._hooks.update(
    funding=lambda s: {"available": True, "funding_rate_pct": 0.07, "open_interest": 1234.0, "next_funding_ts": 1},
    order_book=lambda s: {"bids": [[99.95, 500], [99.9, 5000]], "asks": [[100.05, 500], [100.1, 5000]]},
    calendar=lambda a, b, c, d: [{"time": (datetime.utcnow() + timedelta(hours=5)).isoformat() + "Z", "currency": "USD",
                                  "title": "US CPI", "impact": "high"},
                                 {"time": (datetime.utcnow() + timedelta(hours=6)).isoformat() + "Z", "currency": "JPY",
                                  "title": "BoJ", "impact": "high"}],
    calendar_status=lambda: {"configured": True, "ok": True})
lv = I.live_view()
by = {s["symbol"]: s for s in lv["signals"]}
check(len(lv["signals"]) >= 2 and "BTC/USDT" in by, "live view: open signals listed")
check(by["BTC/USDT"]["funding"]["crowd"] == "longs_crowded", "live view: funding crowding for a long")
check(by["BTC/USDT"]["liquidity"]["available"] and by["BTC/USDT"]["liquidity"]["sizes"][0]["bps"] > 0, "live view: liquidity cost")
check(any(h["title"] == "US CPI" for h in by["BTC/USDT"]["event_hits"]), "live view: CPI falls inside the holding time")
check(lv["events"]["available"] and len(lv["events"]["events"]) == 1, "live view: only events of the currencies in play (USD, EUR), not JPY")
fx = by.get("EUR/USD")
if fx:
    check(fx["funding"]["available"] is False and fx["liquidity"]["kind"] == "forex" and fx["liquidity"]["cost_r"] is not None,
          "live view: forex has swap note and a pip cost, no crypto funding")
check(lv["cluster"]["crypto"]["open"] == 2 and lv["cluster"]["crypto"]["long"] == 2, "live view: cluster counts")

# a failing source never breaks the page
I._cache.clear()
I._hooks.update(funding=lambda s: 1 / 0, order_book=lambda s: 1 / 0, calendar=lambda *a: 1 / 0)
lv2 = I.live_view()
check(all(s["funding"]["available"] is False for s in lv2["signals"] if s["market"] == "crypto"), "failing funding source -> not available")
check(all(s["liquidity"]["available"] is False for s in lv2["signals"] if s["market"] == "crypto"), "failing order book -> not available")
check(lv2["events"]["available"] is False, "failing calendar -> not available")
I._cache.clear()
I._hooks.update(calendar=lambda *a: [], calendar_status=lambda: {"configured": False})
check("FMP_API_KEY" in I.live_view()["events"]["reason"], "no calendar key -> tells what to set")

# --------------------------------------------------------------------------- routes
from flask import Flask  # noqa: E402

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(HERE), "templates"),
            static_folder=os.path.join(os.path.dirname(HERE), "static"))
app.secret_key = "t"
app.register_blueprint(I.insights_bp)
c = app.test_client()
check(c.get("/api/insights/performance").status_code == 401 and c.get("/api/insights/live").status_code == 401
      and c.get("/api/insights/size?balance=1").status_code == 401, "routes need a login")
check(c.get("/pro-insights").status_code == 302, "page redirects visitors to the login")
with c.session_transaction() as s:
    s["user_id"] = 1
I._cache.clear()
r = c.get("/api/insights/performance?market=crypto&timeframe=4h").get_json()
check(r["ok"] and r["live"]["sure"]["n"] == 40, "route: performance")
check(c.get("/api/insights/performance?market=stocks").status_code == 400, "route: unknown market")
check(c.get("/api/insights/live").get_json()["ok"], "route: live")
rs = c.get("/api/insights/size?balance=5000&risk=1&entry=100&stop=98&open=4").get_json()
check(rs["ok"] and rs["suggest"]["per_signal"] == 0.75 and rs["risk_amount"] == 50.0, f"route: size + suggested risk ({rs.get('suggest')})")
check(c.get("/api/insights/size?balance=abc&risk=1&entry=100&stop=98").status_code == 400, "route: bad size input")
try:
    html = c.get("/pro-insights").data.decode()
    check("Pro insights" in html and "insights.js" in html, "page renders")
except Exception as e:
    check(False, f"page renders ({e})")

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASSED")
sys.exit(1 if FAILS else 0)
