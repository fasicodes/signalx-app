"""Fetch 1m Binance spot klines (public CC repos on GitHub) and aggregate to 15m.
Usage: python fetch_crypto.py COIN [first_year]"""
import glob, os, shutil, subprocess, sys, time
import pandas as pd
coin = sys.argv[1]
first = int(sys.argv[2]) if len(sys.argv) > 2 else 2021
R = os.environ.get("RESEARCH_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
os.makedirs(f"{R}/data/crypto", exist_ok=True)
out = f"{R}/data/crypto/{coin}_15m.csv.gz"
if os.path.exists(out):
    print(coin, "exists"); sys.exit(0)
tmp = f"{R}/tmp_{coin}"
shutil.rmtree(tmp, ignore_errors=True)
t0 = time.time()
url = f"https://github.com/parshant231234-eng/{coin}USDT-Historical-Data"
r = subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", url, tmp], capture_output=True, text=True)
if r.returncode:
    print(coin, "clone failed", r.stderr[-300:]); sys.exit(1)
years = [f"1m/{y}" for y in range(first, 2027)]
r = subprocess.run(["git", "-C", tmp, "sparse-checkout", "set", *years], capture_output=True, text=True)
if r.returncode:
    print(coin, "checkout failed", r.stderr[-300:]); sys.exit(1)
frames = []
for f in sorted(glob.glob(f"{tmp}/1m/*/*/*.csv")):
    d = pd.read_csv(f, usecols=["timestamp", "open", "high", "low", "close", "volume", "quote_volume", "trade_count", "taker_buy_volume"])
    d["timestamp"] = pd.to_datetime(d["timestamp"], unit="ms")
    g = d.set_index("timestamp").resample("15min", label="left", closed="left")
    a = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "quote_volume": "sum",
               "trade_count": "sum", "taker_buy_volume": "sum"})
    a["n1m"] = g["close"].count()
    frames.append(a[a["n1m"] > 0].reset_index())
df = pd.concat(frames).drop_duplicates("timestamp").sort_values("timestamp")
df.to_csv(out, index=False, compression="gzip", float_format="%.10g")
shutil.rmtree(tmp, ignore_errors=True)
print(coin, len(df), df["timestamp"].iloc[0], df["timestamp"].iloc[-1], f"{time.time() - t0:.0f}s", flush=True)
