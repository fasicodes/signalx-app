"""Download candle history for the engine 5 research (public data sets, read through GitHub's raw CDN).

crypto  Binance spot 1-minute klines, github.com/parshant231234-eng/<COIN>USDT-Historical-Data
        -> data/crypto/<COIN>.parquet (15-minute candles, UTC)
forex   2012-11 .. 2022-03: MetaTrader 15-minute candles, github.com/ejtraderLabs/historical-data
        (broker time = New York + 7 h)                                   -> data/fx/<PAIR>_a.parquet
        2023-01 .. today:   HistData 1-minute candles, github.com/mailztoganesh-civil/forex-data
        (file time = New York + 5 h; checked against UTC hourly candles) -> data/fx/<PAIR>_b.parquet

Every 15-minute candle built from 1-minute data also keeps p1 / p2: the price one and two minutes after the
candle opened. A signal is published a little after its candle closed, so the research enters at the NEXT candle's
p1 (crypto) or p2 (forex) instead of the close - what a follower could really get.

Usage: python fetch.py crypto [COIN ...] | python fetch.py fx_a | python fetch.py fx_b
"""
import io
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
W = os.environ.get("V5_DIR") or os.path.join(HERE, "work")
RAW = "https://raw.githubusercontent.com"
FIRST_YEAR = int(os.environ.get("V5_FIRST_YEAR", "2020"))
FX_A = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "XAUUSD", "AUDJPY", "EURCHF",
        "EURGBP", "EURJPY", "GBPJPY", "EURAUD", "CADJPY", "GBPCHF", "XAGUSD", "AUDNZD", "NZDJPY", "CHFJPY",
        "EURCAD", "GBPCAD", "GBPAUD", "EURNZD", "AUDCAD"]
FX_B = list(FX_A)
_session = requests.Session()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def get(url, tries=4):
    for k in range(tries):
        try:
            r = _session.get(url, timeout=90)
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r.content
        except requests.RequestException:
            pass
        time.sleep(2 * (k + 1))
    raise RuntimeError(f"download failed: {url}")


def months(first_year):
    now = pd.Timestamp.utcnow()
    out = []
    for y in range(first_year, now.year + 1):
        for m in range(1, 13):
            if (y, m) <= (now.year, now.month):
                out.append((y, m))
    return out


def to_15m(d):
    """1-minute rows (timestamp + ohlcv [+ flow columns]) -> 15-minute candles with n1m, p1, p2."""
    d = d.sort_values("timestamp").drop_duplicates("timestamp")
    d = d.set_index("timestamp")
    g = d.resample("15min", label="left", closed="left")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    for c in ("quote_volume", "trade_count", "taker_buy_volume"):
        if c in d.columns:
            agg[c] = "sum"
    a = g.agg(agg)
    a["n1m"] = g["close"].count()
    minute = (d.index.minute % 15)
    for k, name in ((0, "p1"), (1, "p2")):
        s = d["close"][minute == k]
        s.index = s.index.floor("15min")
        a[name] = s[~s.index.duplicated()].reindex(a.index)
    a = a[a["n1m"] > 0].reset_index()
    return a


def crypto_coin(coin):
    out = f"{W}/data/crypto/{coin}.parquet"
    if os.path.exists(out):
        return coin, "exists"
    frames = []
    for y, m in months(FIRST_YEAR):
        url = f"{RAW}/parshant231234-eng/{coin}USDT-Historical-Data/main/1m/{y}/{m:02d}/{coin}USDT_1m_{y}-{m:02d}.csv"
        raw = get(url)
        if raw is None:
            continue
        d = pd.read_csv(io.BytesIO(raw), usecols=["timestamp", "open", "high", "low", "close", "volume", "quote_volume",
                                                  "trade_count", "taker_buy_volume"])
        d["timestamp"] = pd.to_datetime(d["timestamp"], unit="ms")
        frames.append(to_15m(d))
    if not frames:
        return coin, "no data"
    df = pd.concat(frames).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    for c in df.columns:
        if c not in ("timestamp", "n1m", "trade_count"):
            df[c] = df[c].astype(np.float64)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_parquet(out, index=False)
    return coin, f"{len(df)} candles {df['timestamp'].iloc[0]} .. {df['timestamp'].iloc[-1]}"


def _ny_to_utc(local):
    return (local.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward")
            .dt.tz_convert("UTC").dt.tz_localize(None))


def fx_a(pair):
    out = f"{W}/data/fx/{pair}_a.parquet"
    if os.path.exists(out):
        return pair, "exists"
    raw = get(f"{RAW}/ejtraderLabs/historical-data/main/{pair}/{pair}m15.csv")
    if raw is None:
        return pair, "no data"
    d = pd.read_csv(io.BytesIO(raw))
    scale = 100.0 if pair == "XAUUSD" else (1000.0 if pair.endswith("JPY") or pair == "XAGUSD" else 100000.0)
    utc = _ny_to_utc(pd.to_datetime(d["Date"]) - pd.Timedelta(hours=7))
    df = pd.DataFrame({"timestamp": utc, "open": d["open"] / scale, "high": d["high"] / scale, "low": d["low"] / scale,
                       "close": d["close"] / scale, "volume": d["tick_volume"].astype(float)})
    df = df.dropna(subset=["timestamp"]).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_parquet(out, index=False)
    return pair, f"{len(df)} candles {df['timestamp'].iloc[0]} .. {df['timestamp'].iloc[-1]} median close {df['close'].median():.5f}"


def fx_b(pair):
    out = f"{W}/data/fx/{pair}_b.parquet"
    if os.path.exists(out):
        return pair, "exists"
    frames = []
    for y, m in months(2023):
        raw = get(f"{RAW}/mailztoganesh-civil/forex-data/main/data/{pair}/1m_histdata/{y}-{m:02d}.csv.gz")
        if raw is None:
            continue
        d = pd.read_csv(io.BytesIO(raw), compression="gzip")
        d.columns = [c.lower() for c in d.columns]
        d["timestamp"] = _ny_to_utc(pd.to_datetime(d["datetime"]) - pd.Timedelta(hours=5))
        d = d.dropna(subset=["timestamp"])
        frames.append(to_15m(d[["timestamp", "open", "high", "low", "close", "volume"]]))
    if not frames:
        return pair, "no data"
    df = pd.concat(frames).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_parquet(out, index=False)
    return pair, f"{len(df)} candles {df['timestamp'].iloc[0]} .. {df['timestamp'].iloc[-1]}"


def run(fn, names, workers=6):
    with ThreadPoolExecutor(workers) as ex:
        for name, msg in ex.map(lambda n: _safe(fn, n), names):
            log(name, msg)


def _safe(fn, n):
    try:
        return fn(n)
    except Exception as e:  # one missing symbol must not stop the rest
        return n, f"FAILED {str(e)[:120]}"


if __name__ == "__main__":
    what = sys.argv[1]
    if what == "crypto":
        import markets
        coins = sys.argv[2:] or [c.split("/")[0] for c in markets.CRYPTO_COINS]
        run(crypto_coin, coins)
    elif what == "fx_a":
        run(fx_a, sys.argv[2:] or FX_A, workers=4)
    elif what == "fx_b":
        run(fx_b, sys.argv[2:] or FX_B, workers=4)
