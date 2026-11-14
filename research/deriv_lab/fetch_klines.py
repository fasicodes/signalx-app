"""Binance USD-M perpetual 15m klines (public monthly files on data.binance.vision) -> work/klines/<COIN>_15m.csv.gz.
Usage: python fetch_klines.py COIN [COIN ...]"""
import csv, gzip, io, os, sys, zipfile, datetime as dt, urllib.request, urllib.error, time
from concurrent.futures import ThreadPoolExecutor
BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work", "klines")
FUT = {"SHIB": "1000SHIBUSDT", "PEPE": "1000PEPEUSDT"}

def get(url):
    for k in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "signalx-research/1.0"}), timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
        time.sleep(2 * (k + 1))
    return None

def rows(blob):
    if not blob:
        return []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode()
    return [r for r in csv.reader(io.StringIO(text)) if r and r[0][:1].isdigit()]

def main(coins):
    os.makedirs(OUT, exist_ok=True)
    today = dt.date.today()
    for coin in coins:
        out = f"{OUT}/{coin}_15m.csv.gz"
        if os.path.exists(out):
            continue
        sym = FUT.get(coin, coin + "USDT")
        ms = [(y, m) for y in range(2021, today.year + 1) for m in range(1, 13) if (y, m) < (today.year, today.month)]
        urls = [f"{BASE}/{sym}/15m/{sym}-15m-{y}-{m:02d}.zip" for y, m in ms]
        with ThreadPoolExecutor(16) as ex:
            data = [r for b in ex.map(get, urls) for r in rows(b)]
        seen, outrows = set(), []
        for r in sorted(data, key=lambda r: int(r[0])):
            t = int(r[0])
            if t in seen:
                continue
            seen.add(t)
            outrows.append([t, r[1], r[2], r[3], r[4], r[5], r[7], r[8], r[9], r[10]])
        with gzip.open(out + ".part", "wt", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["open_ms", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote"])
            w.writerows(outrows)
        os.replace(out + ".part", out)
        print(coin, len(outrows), flush=True)

if __name__ == "__main__":
    main([c.upper() for c in sys.argv[1:]])
