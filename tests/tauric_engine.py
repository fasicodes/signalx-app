"""Tauric provider: decision parsing, trade plan, one decision per symbol per day, live accounts excluded."""
import os, sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import numpy as np
import pandas as pd

os.environ["SIGNAL_PROVIDER"] = "tauric"
os.environ["TAURIC_SYMBOLS"] = "BTC/USDT,EUR/USD"
import markets, streams, tauric_engine as te  # noqa: E402

fails = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

check("parse BUY", te.parse_decision("BUY") == "LONG")
check("parse overweight", te.parse_decision("Final: OVERWEIGHT") == "LONG")
check("parse SELL", te.parse_decision("**SELL**") == "SHORT")
check("parse HOLD", te.parse_decision("HOLD") is None)
check("parse first keyword wins", te.parse_decision("SELL now, do not BUY") == "SHORT")
check("parse junk", te.parse_decision("") is None and te.parse_decision(None) is None)
check("tickers", te.yahoo_ticker("BTC/USDT") == "BTC-USD" and te.yahoo_ticker("EUR/USD") == "EURUSD=X"
      and te.yahoo_ticker("USD/JPY") == "JPY=X" and te.yahoo_ticker("XAU/USD") == "GC=F")
check("live accounts never copy (validated False)", streams.validated("crypto_4h") is False)
check("plan engine tag", streams.plan_for("crypto", "4h")["engine"] == "tauric")
check("symbol filter", te.symbols_for("crypto") == ["BTC/USDT"])


class FakeStore:
    def frame(self, sym, tf, now=None, bars=None):
        n = 250
        last = streams.last_closed_bar(tf, now)
        ts = [last - timedelta(seconds=markets.TF_SEC[tf] * (n - 1 - i)) for i in range(n)]
        rng = np.random.default_rng(1)
        c = 100 + np.cumsum(rng.normal(0, 1, n))
        return pd.DataFrame({"timestamp": ts, "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1.0})

calls = []
te.set_decider(lambda tk, day: calls.append((tk, day)) or "BUY")
now = datetime(2026, 11, 3, 4, 5, 0)       # a few minutes after the 00:00-04:00 candle... closed bar = 00:00
eng = te.TauricEngine("crypto", "4h", FakeStore(), blocked=None)
b = eng.board(now, force=True)
r = b["BTC/USDT"]
check("one run for the one symbol", len(calls) == 1 and calls[0] == ("BTC-USD", "2026-11-03") or len(calls) == 1)
check("signal is LONG, fresh, active", r["verdict"] == "LONG" and r["fresh"] and r["active"] is not None)
a = r["active"]
check("stop below entry, targets 1/2/3R above", a["stop_loss"] < a["entry"] < a["targets"][0] < a["targets"][1] < a["targets"][2])
R = a["entry"] - a["stop_loss"]
check("targets are exactly 1R/2R/3R", all(abs((t - a["entry"]) / R - k) < 1e-9 for t, k in zip(a["targets"], [1, 2, 3])))
check("no fake confidence", a["confidence"] is None and r["confidence"] is None and a["grade"] == "C")
check("expires after the plan's max_bars", a["expires_at"] > a["signal_at"])
b2 = eng.board(now + timedelta(hours=4), force=True)       # next 4h candle, same UTC day -> no new LLM run, no new signal
check("same day: no second LLM call", len(calls) == 1 and b2["BTC/USDT"]["active"] is None)
te.set_decider(lambda tk, day: "HOLD")
eng2 = te.TauricEngine("crypto", "4h", FakeStore())
check("HOLD -> WAIT", eng2.board(now, force=True)["BTC/USDT"]["verdict"] == "WAIT")
os.environ["TAURIC_MAX_RUNS_PER_DAY"] = "0"
eng3 = te.TauricEngine("crypto", "4h", FakeStore())
check("daily cap -> error entry, no crash", "error" in eng3.board(now, force=True)["BTC/USDT"])
eng4 = te.TauricEngine("crypto", "1h", FakeStore())
check("1h stream is skipped by default", eng4.board(now, force=True) == {})
sys.exit(1 if fails else 0)
