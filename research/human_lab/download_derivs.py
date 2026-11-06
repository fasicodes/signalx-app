"""Download Binance futures positioning data for the SignalX research (run this on YOUR computer).

What it downloads (free, public: https://data.binance.vision, USD-M perpetual futures):
* funding rate         - every funding payment since 2021
* metrics (5-minute)   - open interest, top-trader and all-account long/short ratios, taker buy/sell ratio
* premium index (1h)   - how far the perpetual trades above / below the spot index (funding pressure)
and turns them into small hourly files: derivs/<COIN>_funding.csv.gz, derivs/<COIN>_metrics_1h.csv.gz,
derivs/<COIN>_premium_1h.csv.gz. Nothing else on your computer is touched.

Needs only Python 3.8+ (no extra packages). Usage:
    python download_derivs.py                 # all coins (about 1-2 GB of downloads, 30-90 minutes)
    python download_derivs.py BTC ETH SOL     # only these coins
Stopped half way? Run it again: finished coins are skipped.
When it is done it makes derivs.zip - send that (or the derivs folder) back.
"""
import csv
import datetime as dt
import gzip
import io
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("BINANCE_DATA_URL", "https://data.binance.vision").rstrip("/")
OUT = os.environ.get("DERIVS_DIR", "derivs")
START = dt.date(2021, 1, 1)
THREADS = int(os.environ.get("THREADS", 8))
COINS = ("BTC ETH SOL BNB XRP DOGE ADA AVAX LINK LTC DOT TRX BCH ATOM NEAR UNI ETC FIL APT ARB OP INJ SUI AAVE XLM "
         "ICP SHIB PEPE FET STX IMX GRT ALGO LDO CRV THETA SAND MANA AXS GALA CHZ APE EGLD XTZ SNX DYDX FLOW NEO "
         "IOTA MINA COMP YFI DASH ZEN SUSHI 1INCH BAT KSM ZIL ENJ ONE GMT RVN WOO SEI TIA").split()
# coins whose futures contract is quoted per 1000 units
FUTURES_NAME = {"SHIB": "1000SHIBUSDT", "PEPE": "1000PEPEUSDT", "BONK": "1000BONKUSDT", "FLOKI": "1000FLOKIUSDT"}


def symbol(coin):
    return FUTURES_NAME.get(coin, coin + "USDT")


def get(url, tries=4):
    """The file's bytes, or None if it does not exist (the coin was not listed yet)."""
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "signalx-research/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 * (k + 1))
        except Exception:
            time.sleep(2 * (k + 1))
    print("   could not download", url, flush=True)
    return None


def rows_of(blob):
    """CSV rows inside a Binance zip (header row skipped when there is one)."""
    if not blob:
        return []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = z.namelist()[0]
        text = z.read(name).decode("utf-8", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    if rows and rows[0] and not _num(rows[0][0]) and not _is_time(rows[0][0]):
        header = [h.strip() for h in rows[0]]
        return [dict(zip(header, r)) for r in rows[1:] if r]
    return [r for r in rows if r]


def _num(x):
    try:
        float(x)
        return True
    except ValueError:
        return False


def _is_time(x):
    return len(x) >= 10 and x[4] == "-" and x[7] == "-"


def _ms(x):
    """Milliseconds since 1970 from a Binance time field (ms, us or 'YYYY-MM-DD HH:MM:SS')."""
    x = str(x).strip()
    if _num(x):
        v = int(float(x))
        while v > 10 ** 14:          # microseconds -> milliseconds
            v //= 1000
        return v
    t = dt.datetime.strptime(x[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    return int(t.timestamp() * 1000)


def months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def days(start, end):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def write_gz(path, header, rows):
    tmp = path + ".part"
    with gzip.open(tmp, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    os.replace(tmp, path)


def funding(coin, today):
    sym = symbol(coin)
    urls = [f"{BASE}/data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{y}-{m:02d}.zip"
            for y, m in months(START, today)]
    out = []
    with ThreadPoolExecutor(THREADS) as ex:
        for blob in ex.map(get, urls):
            for r in rows_of(blob):
                if isinstance(r, dict):
                    t = r.get("calc_time") or next(iter(r.values()))
                    rate = r.get("last_funding_rate") or r.get("funding_rate") or r.get("fundingRate")
                else:
                    t, rate = r[0], r[-1]
                if rate is None or not _num(rate):
                    continue
                out.append((_ms(t), float(rate)))
    out = sorted(set(out))
    return [(dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), f"{r:.8f}")
            for t, r in out]


METRIC_COLS = ("sum_open_interest", "sum_open_interest_value", "count_toptrader_long_short_ratio",
               "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio")


def metrics(coin, today):
    """5-minute metrics -> one row per hour: the last value known in that hour (open interest, ratios) and the
    hour's average taker buy/sell ratio."""
    sym = symbol(coin)
    urls = [f"{BASE}/data/futures/um/daily/metrics/{sym}/{sym}-metrics-{d.isoformat()}.zip"
            for d in days(START, today)]
    hours = {}
    with ThreadPoolExecutor(THREADS) as ex:
        for blob in ex.map(get, urls):
            for r in rows_of(blob):
                if not isinstance(r, dict) or "create_time" not in r:
                    continue
                t = _ms(r["create_time"])
                h = t - t % 3_600_000
                vals = []
                for c in METRIC_COLS:
                    v = r.get(c, "")
                    vals.append(float(v) if _num(v) else None)
                slot = hours.setdefault(h, {"t": -1, "last": None, "taker": []})
                if t > slot["t"]:
                    slot["t"], slot["last"] = t, vals
                if vals[5] is not None:
                    slot["taker"].append(vals[5])
    out = []
    for h in sorted(hours):
        s = hours[h]
        last = s["last"]
        taker = sum(s["taker"]) / len(s["taker"]) if s["taker"] else None
        out.append([dt.datetime.fromtimestamp(h / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")] +
                   ["" if v is None else f"{v:.8g}" for v in last[:5]] + ["" if taker is None else f"{taker:.6g}"])
    return out


def premium(coin, today):
    sym = symbol(coin)
    urls = [f"{BASE}/data/futures/um/monthly/premiumIndexKlines/{sym}/1h/{sym}-1h-{y}-{m:02d}.zip"
            for y, m in months(START, today)]
    out = {}
    with ThreadPoolExecutor(THREADS) as ex:
        for blob in ex.map(get, urls):
            for r in rows_of(blob):
                if isinstance(r, dict):
                    t, c = r.get("open_time"), r.get("close")
                else:
                    t, c = r[0], r[4] if len(r) > 4 else None
                if t is None or c is None or not _num(c):
                    continue
                out[_ms(t)] = float(c)
    return [(dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), f"{v:.8g}")
            for t, v in sorted(out.items())]


def main(coins):
    os.makedirs(OUT, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=2)
    t0 = time.time()
    for n, coin in enumerate(coins, 1):
        done = [f"{OUT}/{coin}_{k}.csv.gz" for k in ("funding", "metrics_1h", "premium_1h")]
        if all(os.path.exists(p) for p in done):
            print(f"[{n}/{len(coins)}] {coin}: already done", flush=True)
            continue
        print(f"[{n}/{len(coins)}] {coin} ({symbol(coin)}) ...", flush=True)
        f = funding(coin, today)
        write_gz(done[0], ("time_utc", "funding_rate"), f)
        m = metrics(coin, today)
        write_gz(done[1], ("hour_utc",) + METRIC_COLS[:5] + ("taker_long_short_vol_ratio_avg",), m)
        p = premium(coin, today)
        write_gz(done[2], ("hour_utc", "premium_index_close"), p)
        print(f"   funding {len(f)} rows, metrics {len(m)} hours, premium {len(p)} hours "
              f"({(time.time() - t0) / 60:.1f} min so far)", flush=True)
    with zipfile.ZipFile("derivs.zip", "w", zipfile.ZIP_STORED) as z:
        for name in sorted(os.listdir(OUT)):
            if name.endswith(".csv.gz"):
                z.write(os.path.join(OUT, name), f"derivs/{name}")
    size = os.path.getsize("derivs.zip") / 1e6
    print(f"\nDone. derivs.zip is {size:.1f} MB - send it back (or upload the derivs folder to GitHub).")


if __name__ == "__main__":
    picked = [c.upper() for c in sys.argv[1:]] or list(COINS)
    main(picked)
