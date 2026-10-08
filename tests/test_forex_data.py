"""Tests for forex_data.py with fake Yahoo / Twelve Data responses (no network).
Run from the project root:  python tests/test_forex_data.py
"""
import os
import sys
import types
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


CALLS = {"yahoo": [], "td": []}
NOW = datetime.utcnow().replace(minute=0, second=0, microsecond=0)


class FakeTicker:
    def __init__(self, t):
        self.t = t

    def history(self, start=None, end=None, interval="60m", auto_adjust=False):
        CALLS["yahoo"].append((self.t, interval, start, end))
        if YAHOO["fail"]:
            return pd.DataFrame()
        step = {"60m": "h", "1d": "D", "15m": "15min", "1m": "min"}[interval]
        idx = pd.date_range(end=NOW - timedelta(hours=1), periods=2000, freq=step, tz="UTC")
        idx = idx[idx.dayofweek < 5]                      # forex is closed at weekends
        base = 1.08 + 0.0001 * np.arange(len(idx))
        df = pd.DataFrame({"Open": base, "High": base + 0.0005, "Low": base - 0.0005, "Close": base + 0.0001, "Volume": 0.0},
                          index=pd.DatetimeIndex(idx, name="Datetime"))
        return df


YAHOO = {"fail": False}
yf = types.ModuleType("yfinance")
yf.Ticker = FakeTicker
sys.modules["yfinance"] = yf

TD = {"mode": "ok"}


class FakeResp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def fake_get(url, params=None, timeout=None):
    CALLS["td"].append(params)
    if TD["mode"] == "error":
        return FakeResp({"status": "error", "code": 429, "message": "You have run out of API credits"})
    n = params["outputsize"]
    times = pd.date_range(end=NOW, periods=n, freq="4h")
    return FakeResp({"status": "ok", "values": [
        {"datetime": t.strftime("%Y-%m-%d %H:%M:%S"), "open": "2390.1", "high": "2395.0", "low": "2385.2", "close": "2391.4"}
        for t in times]})


req = types.ModuleType("requests")
req.get = fake_get
sys.modules["requests"] = req

import forex_data as fx  # noqa: E402

print("\n[1] Yahoo path (no key)")
os.environ.pop("TWELVEDATA_API_KEY", None)
df = fx.get_candles("EUR/USD", "4h", limit=100)
check(len(df) == 100 and list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"], "100 x 4h candles, same columns as crypto")
check(all(t.hour % 4 == 0 and t.minute == 0 for t in df["timestamp"]), "4h candles built from 1h and aligned to UTC 00/04/08...")
check(CALLS["yahoo"][-1][0] == "EURUSD=X" and CALLS["yahoo"][-1][1] == "60m", "Yahoo ticker + 60m base interval")
start = datetime.strptime(CALLS["yahoo"][-1][2], "%Y-%m-%d")
check((NOW - start).days < 300, "asks only for the history it needs (not 2 years every time)")
n = len(CALLS["yahoo"])
fx.get_candles("EUR/USD", "4h", limit=100)
check(len(CALLS["yahoo"]) == n, "cached: a second request does not call Yahoo again")
fx.get_candles("XAU/USD", "1h", limit=50)
check(CALLS["yahoo"][-1][0] == "GC=F", "gold maps to Yahoo's gold future")
since = int((NOW - timedelta(days=5) - datetime(1970, 1, 1)).total_seconds() * 1000)
d2 = fx.get_candles("GBP/USD", "1h", limit=500, since=since)
check(d2["timestamp"].min() >= pd.to_datetime(since, unit="ms"), "since filter respected")

print("\n[2] Twelve Data path (key set)")
os.environ["TWELVEDATA_API_KEY"] = "test"
fx._cache.clear()
y0 = len(CALLS["yahoo"])
d3 = fx.get_candles("XAU/USD", "4h", limit=60)
check(len(d3) == 60 and CALLS["td"][-1]["symbol"] == "XAU/USD" and CALLS["td"][-1]["interval"] == "4h", "Twelve Data used when a key is set")
check(len(CALLS["yahoo"]) == y0 and abs(float(d3["close"].iloc[-1]) - 2391.4) < 1e-9, "no Yahoo call needed")
TD["mode"] = "error"
fx._cache.clear()
d4 = fx.get_candles("EUR/USD", "1h", limit=30)
check(len(d4) == 30 and len(CALLS["yahoo"]) == y0 + 1, "Twelve Data error -> falls back to Yahoo")
fx._td_calls.clear()
for _ in range(fx.TD_PER_MINUTE):
    fx._td_slot()
check(fx._td_slot() is False, "Twelve Data free-plan pace limit (requests per minute) respected")

print("\n[3] failures")
os.environ.pop("TWELVEDATA_API_KEY", None)
fx._cache.clear()
YAHOO["fail"] = True
try:
    fx.get_candles("USD/JPY", "4h", limit=10)
    ok = False
except ValueError as e:
    ok = "not available right now" in str(e)
check(ok, "clear error when no source has data")
YAHOO["fail"] = False
d5 = fx.get_candles("USD/JPY", "4h", limit=10)
key = [k for k in fx._cache if k[0] == "USD/JPY"][0]
fx._cache[key] = (0, fx._cache[key][1])            # expired
YAHOO["fail"] = True
d6 = fx.get_candles("USD/JPY", "4h", limit=10)
check(len(d6) == len(d5), "source down -> last good candles are served instead of an error")
YAHOO["fail"] = False

print(f"\nALL {passed} CHECKS PASSED")
