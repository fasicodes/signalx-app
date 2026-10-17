"""
Builds models/strategies_seed.json: the strategy library's results that the Strategies page shows until the site's
own daily run (strategies.py) has finished for the first time.

It runs exactly the same code (strategy_library.compute_all) on Binance spot candles from the research data
(RESEARCH_DIR/data/crypto/<COIN>_15m.csv.gz, see README.md in this folder): the library coins, 1h candles and 4h
candles built from them, the last 365 days before END. Forex is left out (the research forex data ends in 2022).

  python strategies_seed.py                   # END = the last candle in the data
  python strategies_seed.py 2026-10-10        # END given
"""
import json
import os
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import strategy_library as SL  # noqa: E402

R = os.environ.get("RESEARCH_DIR") or os.path.join(HERE, "work")
DAYS = 365


def load(coin, start, end):
    path = f"{R}/data/crypto/{coin}_15m.csv.gz"
    if not os.path.exists(path):
        return None
    d = pd.read_csv(path, usecols=["timestamp", "open", "high", "low", "close", "volume"], parse_dates=["timestamp"])
    d = d[(d["timestamp"] >= start) & (d["timestamp"] < end)]
    g = d.set_index("timestamp").resample("1h", label="left", closed="left", origin="epoch")
    h1 = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    h1 = h1[g["close"].count() >= 4].dropna().reset_index()
    return h1


def main():
    t0 = time.time()
    if len(sys.argv) > 1:
        end = pd.Timestamp(sys.argv[1]) + pd.Timedelta(days=1)
    else:
        btc = pd.read_csv(f"{R}/data/crypto/BTC_15m.csv.gz", usecols=["timestamp"], parse_dates=["timestamp"])
        end = btc["timestamp"].iloc[-1].floor("h")
    start = end - pd.Timedelta(days=DAYS)
    frames = {"crypto": {"1h": {}, "4h": {}}, "forex": {"1h": {}, "4h": {}}}
    for sym in SL.LIB_COINS:
        h1 = load(sym.split("/")[0], start - pd.Timedelta(days=45), end)
        if h1 is None or len(h1) < SL.WARMUP + 100:
            print("skip", sym)
            continue
        frames["crypto"]["1h"][sym] = SL.prepare(h1)
        frames["crypto"]["4h"][sym] = SL.prepare(SL.resample(h1, "4h"))
    now = frames["crypto"]["1h"]["BTC/USDT"]["timestamp"].iloc[-1] + pd.Timedelta(hours=1)
    lib = SL.compute_all(frames, start.to_pydatetime(), now.to_pydatetime(), source="seed",
                         note="Binance spot candles, made before launch")
    for r in lib.values():                      # the seed has no forex: say when it comes instead of "no candles"
        for k, v in r["results"].items():
            if k.startswith("forex") and v.get("error"):
                v["error"] = "Forex results appear after the site's first daily update."
    meta = {"source": "seed", "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "days": DAYS,
            "period": f"{start:%Y-%m-%d} to {now:%Y-%m-%d}", "coins": sorted(frames["crypto"]["1h"]), "forex": [],
            "data": "Binance spot candles (research data)"}
    out = os.path.join(APP, "models", "strategies_seed.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "strategies": lib}, f, separators=(",", ":"))
    print(f"wrote {out}: {len(lib)} strategies, {os.path.getsize(out) / 1e6:.2f} MB, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
