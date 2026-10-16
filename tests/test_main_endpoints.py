"""main.py itself: the public data endpoints (login, whitelist, safe errors), security headers and the
analysis functions that had bugs. No network, no MySQL (SQLite stand-in); exchange calls are replaced by fakes.
Run from the project root:  python tests/test_main_endpoints.py
"""
import os
import sys
import warnings

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(1, HERE)
os.environ.update({"SECRET_KEY": "t" * 32, "AUTOTRADE_ENGINE": "off", "SIGNALX_ENGINE": "off",
                   "AUTOTRADE_ENCRYPTION_KEY": "6HQGyRqbL3x4j5Z4rX3j2pQ7o1m8Zl0b9m1xg7g4n0U="})

from dbshim import setup  # noqa: E402

Conn = setup("main_endpoints")
import db  # noqa: E402

db.get_db_connection = Conn

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import main  # noqa: E402

FAILS = []


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def synthetic_candles(n=120, price=100.0, seed=3):
    rng = np.random.default_rng(seed)
    close = price * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=n, freq="h"), "open": open_,
                         "high": np.maximum(open_, close) * 1.002, "low": np.minimum(open_, close) * 0.998,
                         "close": close, "volume": rng.uniform(100, 1000, n)})


# ---------------------------------------------------------------- endpoints
print("\n[1] visitors: data endpoints are for members")
guest = main.app.test_client()
for path in ("/signal?coin=BTC/USDT", "/candles?coin=BTC/USDT", "/liquidity?coin=BTC/USDT", "/ticker",
             "/api/engine?coin=BTC/USDT"):
    r = guest.get(path)
    check(r.status_code == 401, f"{path.split('?')[0]} -> 401 for a visitor")
check(guest.get("/coins").status_code == 200, "/coins (names only) stays public")
check(guest.get("/").status_code == 200 and guest.get("/pricing").status_code == 200, "public pages still open")

print("\n[2] members: coin / timeframe whitelist, safe errors")
c = Conn()
cur = c.cursor()
cur.execute("INSERT INTO users (email, password_hash, auth_provider, email_verified) VALUES (%s, %s, 'password', 1)",
            ("member@example.com", "x"))
uid = cur.lastrowid
c.close()
member = main.app.test_client()
with member.session_transaction() as s:
    s["user_id"] = uid
    s["email"] = "member@example.com"
    s["sv"] = 0

SECRET = "SECRET-INTERNAL-DETAIL-9f3a"
calls = []


def fake_candles(symbol="BTC/USDT", timeframe="1h", limit=200, since=None):
    calls.append((symbol, timeframe))
    return synthetic_candles(max(int(limit), 120))


main.get_candles = fake_candles
r = member.get("/candles?coin=BTC/USDT&timeframe=1h&limit=50")
check(r.status_code == 200 and r.get_json()["coin"] == "BTC/USDT" and r.get_json()["candles"], "valid /candles works")
n = len(calls)
for bad in ("coin=../../etc/passwd", "coin=NOPE/USDT", "coin=BTC/USDT&timeframe=1x", "coin=BTC/USDT&timeframe=%27%3B--"):
    r = member.get("/candles?" + bad)
    check(r.status_code == 400, f"/candles?{bad[:28]} -> 400")
check(len(calls) == n, "bad coin / timeframe never reach the exchange")
r = member.get("/candles?coin=btc/usdt&timeframe=4h&limit=10")
check(r.status_code == 200 and calls[-1] == ("BTC/USDT", "4h"), "coin is upper-cased before use")


def failing_candles(*a, **k):
    raise RuntimeError(SECRET)


main.get_candles = failing_candles
for path in ("/candles?coin=BTC/USDT", "/signal?coin=BTC/USDT", "/liquidity?coin=BTC/USDT"):
    r = member.get(path)
    body = r.get_data(as_text=True)
    check(r.status_code == 502 and SECRET not in body and "error" in r.get_json(), f"{path.split('?')[0]}: failure -> 502 without internals")
main.get_candles = fake_candles
main.generate_signal = lambda df, symbol=None, include_orderbook=True: {"final_verdict": "WAIT", "confidence_pct": 50}
r = member.get("/signal?coin=ETH/USDT&timeframe=1h")
check(r.status_code == 200 and r.get_json()["coin"] == "ETH/USDT", "valid /signal works")
r = member.get("/signal?coin=FAKE/USDT")
check(r.status_code == 400, "/signal with an unknown coin -> 400")

main.exchange.fetch_tickers = lambda syms: (_ for _ in ()).throw(RuntimeError(SECRET))
r = member.get("/ticker")
check(r.status_code == 502 and SECRET not in r.get_data(as_text=True), "/ticker failure -> 502 without internals")


def boom(*a, **k):
    raise RuntimeError(SECRET)


class BoomEngine:
    def signal(self, *a, **k):
        boom()


main.get_signal_engine = lambda: BoomEngine()
r = member.get("/api/engine?coin=BTC/USDT")
check(r.status_code == 200 and SECRET not in r.get_data(as_text=True), "/api/engine: engine failure does not leak internals")
r = member.get("/api/engine?coin=%3Cscript%3E")
check(r.status_code == 400, "/api/engine with a bad coin -> 400")

print("\n[3] security headers and body size")
r = guest.get("/login")
h = r.headers
check(h.get("X-Content-Type-Options") == "nosniff" and h.get("X-Frame-Options") == "SAMEORIGIN"
      and "frame-ancestors 'self'" in h.get("Content-Security-Policy", "") and h.get("Referrer-Policy"),
      "browser protection headers are sent")
r = guest.get("/login", base_url="https://example.com")
check("max-age" in r.headers.get("Strict-Transport-Security", ""), "HSTS on https")
r = guest.post("/api/login", data=b"x" * (6 * 1024 * 1024), content_type="application/json")
check(r.status_code == 413, "a 6 MB request body is refused")

# ----------------------------------------------------- analysis functions
print("\n[4] analysis functions")
rng = np.random.default_rng(11)
h_rw = [main.hurst_exponent(pd.DataFrame({"close": 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 100)))}))["hurst"]
        for _ in range(200)]
check(0.42 < float(np.mean(h_rw)) < 0.58, f"Hurst of a random walk is near 0.5 (got {np.mean(h_rw):.3f}; it was ~0.01)")
pe = main._permutation_entropy(rng.normal(size=100), order=3)
check(pe is not None and 0.8 < pe <= 1.0, "permutation entropy runs on numpy 2.x (np.math is gone)")
ent = main.multi_timeframe_entropy(synthetic_candles(150))
check(ent.get("entropy_avg") is not None, "multi-timeframe entropy returns a value instead of INSUFFICIENT_DATA")

close = np.arange(1.0, 61.0)
d = pd.DataFrame({"close": close, "rsi": 50.0, "macd": 1.0, "macd_signal": 0.5})
up, down = main.bayesian_bullish_bearish(d)
check(up == 100.0 and down == 0.0, "Bayesian: the newest candle (no next candle yet) is not counted as a down move")

seen = {}


class Spy:
    def __init__(self, **kw):
        pass

    def fit(self, X, y):
        seen["y"] = np.asarray(y)

    def predict_proba(self, X):
        return np.array([[0.4, 0.6]])


real_rf = main.RandomForestClassifier
main.RandomForestClassifier = Spy
dd = synthetic_candles(80)
dd["rsi"] = 50.0
dd["macd"] = np.linspace(-1, 1, 80)
dd["macd_signal"] = 0.0
main.meta_label_filter(dd)
main.RandomForestClassifier = real_rf
check(len(seen["y"]) == 80 - 3, "meta-label: the last 3 candles (result unknown) are left out of the training labels")

cheap = synthetic_candles(120, price=1.2e-5)
qv = main.quantile_volatility(cheap, float(cheap["close"].iloc[-1]), "LONG")
check(qv["stop_loss"] and qv["take_profit"] and qv["stop_loss"] < float(cheap["close"].iloc[-1]),
      "stop / target of a 0.00001-priced coin are not rounded to 0.0")
check(main.signal_engine.price_round(7.68e-06) > 0, "price rounding keeps cheap-coin precision")

print("\n[5] Performance Summary counts every closed trade (manual losses too)")
c = Conn()
cur = c.cursor()
for oc, pnl in (("TAKE_PROFIT", 100.0), ("MANUAL_PROFIT", 50.0), ("STOP_LOSS", -60.0), ("MANUAL_LOSS", -90.0), ("MANUAL_LOSS", -30.0)):
    cur.execute("INSERT INTO active_trades (user_id, asset, direction, entry_price, position_size, status, estimated_pnl, outcome_class) "
                "VALUES (%s, 'BTC/USDT', 'LONG', 100, 1000, 'CLOSED', %s, %s)", (uid, pnl, oc))
c.close()
r = member.get("/api/trades/summary")
d = r.get_json()
check(r.status_code == 200 and d["total_qualifying_trades"] == 5 and d["winning_trades"] == 2 and d["losing_trades"] == 3,
      "2 wins + 3 losses (1 stop-loss, 2 manual losses) are all counted")
check(d["total_profit"] == 150.0 and d["total_loss"] == 180.0 and d["net_pnl"] == -30.0 and d["win_rate"] == 40.0,
      "net result is -30 and the win rate 40%, not the flattering 66.7% / +90 that hiding manual losses gave")

print()
print(f"{len(FAILS)} failed" if FAILS else "ALL PASSED")
sys.exit(1 if FAILS else 0)
