"""
Forex / gold candles for Signals FM.

Sources, in order:
  1. Twelve Data (https://twelvedata.com) when TWELVEDATA_API_KEY is set.
     Reliable from cloud servers; the free plan allows 8 requests a minute
     and 800 a day, so results are cached and requests are paced.
  2. Yahoo Finance through yfinance (no key). Works, but Yahoo often slows
     down or blocks requests coming from cloud servers.

Every result is cached for a few seconds to minutes (depending on the
timeframe), so many users on the same chart cost one request.
Returns the same DataFrame shape as the crypto path:
timestamp (naive UTC), open, high, low, close, volume.
"""
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import pandas as pd

SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
       "6h": 21600, "12h": 43200, "1d": 86400, "1w": 604800, "1M": 2592000, "3M": 7776000}

# Twelve Data: native interval, or (base interval, pandas resample rule)
TD_NATIVE = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "2h": "2h", "4h": "4h",
             "1d": "1day", "1w": "1week", "1M": "1month"}
TD_RESAMPLE = {"3m": ("1min", "3min"), "6h": ("1h", "6h"), "12h": ("1h", "12h"), "3M": ("1month", "3MS")}

# Yahoo: (base interval, max days of history Yahoo serves for it, resample rule or None)
YF_PLAN = {
    "1m": ("1m", 7, None), "3m": ("1m", 7, "3min"), "5m": ("5m", 59, None), "15m": ("15m", 59, None),
    "30m": ("30m", 59, None), "1h": ("60m", 729, None), "2h": ("60m", 729, "2h"), "4h": ("60m", 729, "4h"),
    "6h": ("60m", 729, "6h"), "12h": ("60m", 729, "12h"), "1d": ("1d", 3650, None), "1w": ("1wk", 3650, None),
    "1M": ("1mo", 7300, None), "3M": ("3mo", 7300, None),
}
YF_TICKER = {"XAU/USD": "GC=F", "XAG/USD": "SI=F"}   # spot metals are not on Yahoo: front-month futures instead

TD_PER_MINUTE = 7
_td_calls = deque()
_cache = {}
_lock = threading.Lock()
_inflight = {}


def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def _ttl(timeframe):
    return max(20, min(300, SEC.get(timeframe, 3600) // 6))


def _resample(df, rule):
    g = df.set_index("timestamp").resample(rule, label="left", closed="left", origin="epoch" if rule[-1] in "hn" else "start_day")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index()
    return out


def _yahoo(symbol, timeframe, limit, since):
    import yfinance as yf

    base, max_days, rule = YF_PLAN.get(timeframe, ("60m", 729, None))
    tf_sec = SEC.get(timeframe, 3600)
    end = _utcnow() + timedelta(days=1)
    if since is not None:
        start = datetime(1970, 1, 1) + timedelta(milliseconds=int(since))
    else:
        span_days = limit * tf_sec * 1.6 / 86400 + 4          # x1.6: markets close at weekends
        start = end - timedelta(days=span_days)
    start = max(start, end - timedelta(days=max_days))
    base_pair = symbol.upper()
    ticker = YF_TICKER.get(base_pair) or base_pair.replace("/", "") + "=X"
    data = yf.Ticker(ticker).history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
                                     interval=base, auto_adjust=False)
    if data is None or data.empty:
        raise ValueError("Yahoo Finance returned no data")
    data = data.reset_index()
    tcol = "Datetime" if "Datetime" in data.columns else "Date"
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(data[tcol], utc=True).dt.tz_localize(None),
        "open": data["Open"].astype(float), "high": data["High"].astype(float),
        "low": data["Low"].astype(float), "close": data["Close"].astype(float),
        "volume": data["Volume"].astype(float) if "Volume" in data.columns else 0.0,
    })
    if rule:
        df = _resample(df, rule)
    return df


def _td_slot():
    now = time.time()
    with _lock:
        while _td_calls and now - _td_calls[0] > 60:
            _td_calls.popleft()
        if len(_td_calls) >= TD_PER_MINUTE:
            return False
        _td_calls.append(now)
        return True


def _twelvedata(symbol, timeframe, limit, since, key):
    import requests

    if timeframe in TD_NATIVE:
        interval, rule, mult = TD_NATIVE[timeframe], None, 1
    elif timeframe in TD_RESAMPLE:
        interval, rule = TD_RESAMPLE[timeframe]
        mult = {"3m": 3, "6h": 6, "12h": 12, "3M": 3}[timeframe]
    else:
        raise ValueError(f"timeframe {timeframe} not supported")
    if not _td_slot():
        raise ValueError("Twelve Data minute limit reached")
    params = {"symbol": symbol.upper(), "interval": interval, "outputsize": min(5000, limit * mult + 5),
              "timezone": "UTC", "order": "asc", "apikey": key}
    if since is not None:
        params["start_date"] = (datetime(1970, 1, 1) + timedelta(milliseconds=int(since))).strftime("%Y-%m-%d %H:%M:%S")
    r = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=12)
    data = r.json()
    if data.get("status") != "ok" or not data.get("values"):
        raise ValueError(f"Twelve Data: {data.get('message') or data.get('status')}")
    v = pd.DataFrame(data["values"])
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(v["datetime"]),
        "open": v["open"].astype(float), "high": v["high"].astype(float),
        "low": v["low"].astype(float), "close": v["close"].astype(float),
        "volume": v["volume"].astype(float) if "volume" in v.columns else 0.0,
    }).sort_values("timestamp")
    if rule:
        df = _resample(df, rule)
    return df


def get_candles(symbol, timeframe="1h", limit=200, since=None):
    tf_ttl = _ttl(timeframe)
    since_key = None if since is None else int(since) // (tf_ttl * 1000)
    key = (symbol.upper(), timeframe, int(limit), since_key)
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < tf_ttl:
        return hit[1].copy()
    # one request at a time per key: other users wait for it instead of hitting the source too
    with _lock:
        ev = _inflight.get(key)
        mine = ev is None
        if mine:
            ev = _inflight[key] = threading.Event()
    if not mine:
        ev.wait(20)
        hit = _cache.get(key)
        if hit:
            return hit[1].copy()
    try:
        errors = []
        df = None
        key_td = (os.environ.get("TWELVEDATA_API_KEY") or "").strip()
        if key_td:
            try:
                df = _twelvedata(symbol, timeframe, limit, since, key_td)
            except Exception as e:
                errors.append(str(e)[:120])
        if df is None:
            try:
                df = _yahoo(symbol, timeframe, limit, since)
            except Exception as e:
                errors.append(str(e)[:120])
        if df is None or df.empty:
            stale = hit[1].copy() if hit else None
            if stale is not None:
                return stale
            raise ValueError(f"Forex data for {symbol} is not available right now ({'; '.join(errors) or 'no data'}).")
        if since is not None:
            df = df[df["timestamp"] >= pd.to_datetime(int(since), unit="ms")]
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").tail(limit).reset_index(drop=True)
        _cache[key] = (time.time(), df)
        if len(_cache) > 400:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                _cache.pop(k, None)
        return df.copy()
    finally:
        if mine:
            with _lock:
                _inflight.pop(key, None)
            ev.set()
