"""Builds 15m / 1h / 4h candles (UTC) for the strategy lab.
crypto: /home/claude/rdata/data/crypto/<COIN>_15m.csv.gz (Binance spot, from research/fetch_crypto.py)
forex:  HistData 1-minute files (EST = UTC-5, no DST) from github.com/mailztoganesh-civil/forex-data
Usage: python prep.py crypto|forex"""
import glob
import os
import sys

import pandas as pd

D = os.environ.get("REPO_LAB_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
OUT = f"{D}/frames"
RULES = {"15m": "15min", "1h": "1h", "4h": "4h"}


def agg(df, rule):
    g = df.set_index("timestamp").resample(rule, label="left", closed="left")
    a = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return a.dropna(subset=["open"]).reset_index()


def save(market, sym, base):
    os.makedirs(f"{OUT}/{market}", exist_ok=True)
    for tf, rule in RULES.items():
        a = base if tf == "15m" else agg(base, rule)
        a.to_pickle(f"{OUT}/{market}/{sym}_{tf}.pkl")
    print(market, sym, len(base), base.timestamp.iloc[0], base.timestamp.iloc[-1], flush=True)


def crypto():
    for f in sorted(glob.glob(f"{D}/data/crypto/*_15m.csv.gz")):
        sym = os.path.basename(f).split("_")[0]
        d = pd.read_csv(f, usecols=["timestamp", "open", "high", "low", "close", "volume"], parse_dates=["timestamp"])
        save("crypto", sym, d.sort_values("timestamp").reset_index(drop=True))


def forex():
    root = os.environ.get("FOREX_DATA_DIR") or f"{D}/forex-data/data"
    for sym in ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "XAUUSD"]:
        files = sorted(glob.glob(f"{root}/{sym}/1m_histdata/*.csv.gz"))
        files = [f for f in files if os.path.basename(f)[:4] >= "2023"]
        parts = []
        for f in files:
            d = pd.read_csv(f)
            d.columns = [c.lower() for c in d.columns]
            d = d.rename(columns={"datetime": "timestamp"})
            d["timestamp"] = pd.to_datetime(d["timestamp"]) + pd.Timedelta(hours=5)   # EST (fixed) -> UTC
            parts.append(agg(d[["timestamp", "open", "high", "low", "close", "volume"]], "15min"))
        base = pd.concat(parts).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        save("forex", sym, base)


if __name__ == "__main__":
    {"crypto": crypto, "forex": forex}[sys.argv[1]]()
