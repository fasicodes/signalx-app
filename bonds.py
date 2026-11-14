"""
Government bond yields for the bond screener (Premium chart workspace, /premium-chart).

Official, free sources only (no key, no scraping of commercial sites), all daily:
  * United States - U.S. Department of the Treasury, daily par yield curve (1 month to 30 years)
  * Euro area     - European Central Bank, AAA government yield curve (spot rates, 3 months to 30 years)
  * Canada        - Bank of Canada, benchmark bond yields (2 years to long-term)
  * Japan         - Ministry of Finance, JGB interest rates (1 to 40 years)
  * Australia     - Reserve Bank of Australia, table F2 (2, 3, 5 and 10 years)

screener() -> one row per bond (yield, change over 1 day / 1 week / 1 month / 1 year in basis points, 52-week
range, a small history for the sparkline); history(bond_id) -> its daily yields. Everything is cached for hours
(these are end-of-day numbers); one source failing never hides the others.
"""
import csv
import io
import json
import threading
import time
from datetime import date, datetime, timedelta

UA = {"User-Agent": "SignalsFM/1.0 (bond screener; daily official data)"}
TTL = 6 * 3600              # fresh copy every 6 hours
STALE_OK = 3 * 86400        # a failing source keeps its last good copy for up to 3 days
KEEP_DAYS = 3 * 366         # history kept per bond
SPARK = 60                  # points in the sparkline

US_COLS = [("1 Mo", "1M"), ("2 Mo", "2M"), ("3 Mo", "3M"), ("4 Mo", "4M"), ("6 Mo", "6M"), ("1 Yr", "1Y"),
           ("2 Yr", "2Y"), ("3 Yr", "3Y"), ("5 Yr", "5Y"), ("7 Yr", "7Y"), ("10 Yr", "10Y"), ("20 Yr", "20Y"),
           ("30 Yr", "30Y")]
ECB_TENORS = ["3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"]
CA_SERIES = [("BD.CDN.2YR.DQ.YLD", "2Y"), ("BD.CDN.3YR.DQ.YLD", "3Y"), ("BD.CDN.5YR.DQ.YLD", "5Y"),
             ("BD.CDN.7YR.DQ.YLD", "7Y"), ("BD.CDN.10YR.DQ.YLD", "10Y"), ("BD.CDN.LONG.DQ.YLD", "30Y")]
JP_TENORS = ["1Y", "2Y", "3Y", "4Y", "5Y", "6Y", "7Y", "8Y", "9Y", "10Y", "15Y", "20Y", "25Y", "30Y", "40Y"]
AU_SERIES = [("FCMYGBAG2D", "2Y"), ("FCMYGBAG3D", "3Y"), ("FCMYGBAG5D", "5Y"), ("FCMYGBAG10D", "10Y")]

SOURCES = {
    "US": {"country": "United States", "issuer": "U.S. Treasury", "source": "U.S. Department of the Treasury",
           "link": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates"},
    "EU": {"country": "Euro area", "issuer": "Euro area AAA curve", "source": "European Central Bank",
           "link": "https://www.ecb.europa.eu/stats/financial_markets_and_interest_rates/euro_area_yield_curves/html/index.en.html"},
    "CA": {"country": "Canada", "issuer": "Government of Canada", "source": "Bank of Canada",
           "link": "https://www.bankofcanada.ca/rates/interest-rates/canadian-bonds/"},
    "JP": {"country": "Japan", "issuer": "Japanese Government Bond", "source": "Ministry of Finance Japan",
           "link": "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"},
    "AU": {"country": "Australia", "issuer": "Australian Government", "source": "Reserve Bank of Australia",
           "link": "https://www.rba.gov.au/statistics/tables/"},
}

_cache = {}                 # cc -> (fetched_at, {tenor: [(date, yield), ...]}, error)
_locks = {}                 # one lock per country: one download at a time per source, countries in parallel
_locks_guard = threading.Lock()
_get = None                 # tests replace this: _get(url) -> text


def _http_get(url, timeout=25):
    if _get is not None:
        return _get(url)
    import requests
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.content.decode("utf-8", "replace")      # the numbers are ASCII; titles may be in other encodings


def tenor_years(t):
    """'3M' -> 0.25, '10Y' -> 10.0."""
    n = float(t[:-1])
    return n / 12.0 if t.endswith("M") else n


def _num(v):
    try:
        x = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) < 100 else None


def _add(out, tenor, d, v):
    v = _num(v)
    if d is not None and v is not None:
        out.setdefault(tenor, {})[d] = v


def _finish(raw):
    """{tenor: {date: yield}} -> {tenor: [(date, yield), ...]} oldest first, the last KEEP_DAYS days only."""
    out = {}
    for tenor, by_day in raw.items():
        if not by_day:
            continue
        last = max(by_day)
        cut = last - timedelta(days=KEEP_DAYS)
        out[tenor] = sorted((d, v) for d, v in by_day.items() if d >= cut)
    return out


# ------------------------------------------------------------------------------------------------ parsers
def parse_us(texts):
    """U.S. Treasury daily par yield curve CSV (one file per year, newest row first)."""
    raw = {}
    for text in texts:
        for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
            try:
                d = datetime.strptime((row.get("Date") or "").strip(), "%m/%d/%Y").date()
            except ValueError:
                continue
            for col, tenor in US_COLS:
                _add(raw, tenor, d, row.get(col))
    return _finish(raw)


def parse_ecb(text):
    """ECB data API, csvdata: one row per (series, day); the series key ends with SR_<tenor>."""
    raw = {}
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        key = row.get("KEY") or ""
        tenor = key.rsplit("SR_", 1)[-1] if "SR_" in key else None
        if tenor not in ECB_TENORS:
            continue
        try:
            d = datetime.strptime((row.get("TIME_PERIOD") or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        _add(raw, tenor, d, row.get("OBS_VALUE"))
    return _finish(raw)


def parse_ca(text):
    """Bank of Canada Valet JSON: observations [{"d": "2026-10-30", "<series>": {"v": "3.28"}, ...}]."""
    raw = {}
    data = json.loads(text)
    for obs in data.get("observations") or []:
        try:
            d = datetime.strptime(obs.get("d", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        for series, tenor in CA_SERIES:
            cell = obs.get(series)
            if isinstance(cell, dict):
                _add(raw, tenor, d, cell.get("v"))
    return _finish(raw)


def parse_jp(texts):
    """MOF JGB interest rates: 'Date,1Y,...,40Y' rows with dates like 2026/11/5; '-' = no bond of that tenor."""
    raw = {}
    for text in texts:
        header = None
        for row in csv.reader(io.StringIO(text.lstrip("﻿"))):
            if not row:
                continue
            if row[0].strip() == "Date":
                header = [c.strip() for c in row]
                continue
            if header is None:
                continue
            try:
                d = datetime.strptime(row[0].strip(), "%Y/%m/%d").date()
            except ValueError:
                continue
            for i, col in enumerate(header[1:], start=1):
                if col in JP_TENORS and i < len(row):
                    _add(raw, col, d, row[i])
    return _finish(raw)


def parse_au(text):
    """RBA table F2 CSV: metadata rows, a 'Series ID' row naming the columns, then rows like 05-Nov-2026,..."""
    raw = {}
    ids = None
    for row in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if not row:
            continue
        if row[0].strip() == "Series ID":
            ids = [c.strip() for c in row]
            continue
        if ids is None:
            continue
        try:
            d = datetime.strptime(row[0].strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        for series, tenor in AU_SERIES:
            if series in ids:
                i = ids.index(series)
                if i < len(row):
                    _add(raw, tenor, d, row[i])
    return _finish(raw)


# ------------------------------------------------------------------------------------------------ fetchers
def fetch_us(today=None):
    today = today or date.today()
    base = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
            "{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
    texts = []
    for y in (today.year - 1, today.year):
        try:
            texts.append(_http_get(base.format(y=y)))
        except Exception:
            if y == today.year:
                raise
    return parse_us(texts)


def fetch_eu(today=None):
    keys = "+".join("SR_" + t for t in ECB_TENORS)
    url = ("https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.G_N_A.SV_C_YM." + keys +
           "?lastNObservations=300&format=csvdata")
    return parse_ecb(_http_get(url))


def fetch_ca(today=None):
    return parse_ca(_http_get("https://www.bankofcanada.ca/valet/observations/group/bond_yields_benchmark/json?recent=300"))


def fetch_jp(today=None):
    base = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
    texts = [_http_get(base + "historical/jgbcme_all.csv")]
    try:
        texts.append(_http_get(base + "jgbcme.csv"))       # the current month (the big file ends last month)
    except Exception:
        pass
    return parse_jp(texts)


def fetch_au(today=None):
    return parse_au(_http_get("https://www.rba.gov.au/statistics/tables/csv/f2-data.csv"))


FETCHERS = {"US": fetch_us, "EU": fetch_eu, "CA": fetch_ca, "JP": fetch_jp, "AU": fetch_au}


def _series(cc, now=None):
    """(curves {tenor: [(date, y)]}, error or None) for one country, from the cache when fresh enough."""
    now = now or time.time()
    hit = _cache.get(cc)
    if hit and now - hit[0] < TTL:
        return hit[1], hit[2]
    with _locks_guard:
        lock = _locks.setdefault(cc, threading.Lock())
    with lock:
        hit = _cache.get(cc)
        if hit and now - hit[0] < TTL:
            return hit[1], hit[2]
        try:
            data = FETCHERS[cc]()
            if not data:
                raise ValueError("no rows in the file")
            _cache[cc] = (now, data, None)
            return data, None
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:120]}"
            if hit and now - hit[0] < TTL + STALE_OK and hit[1]:
                _cache[cc] = (now - TTL + 900, hit[1], err)          # retry in 15 minutes, keep the old numbers
                return hit[1], err
            _cache[cc] = (now - TTL + 900, {}, err)
            return {}, err


def _value_on_or_before(points, day):
    """Yield on the last day <= `day` (points sorted oldest first)."""
    lo, hi, ans = 0, len(points) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if points[mid][0] <= day:
            ans = points[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _bp(now, then):
    return None if now is None or then is None else round((now - then) * 100, 1)


def bond_row(cc, tenor, points):
    meta = SOURCES[cc]
    last_day, last = points[-1]
    prev = points[-2][1] if len(points) > 1 else None
    year = [v for d, v in points if d > last_day - timedelta(days=365)]
    years = tenor_years(tenor)
    label = f"{tenor[:-1]}-month" if tenor.endswith("M") else f"{tenor[:-1]}-year"
    return {
        "id": f"{cc}{tenor}", "cc": cc, "country": meta["country"], "tenor": tenor, "years": years,
        "name": f"{meta['issuer']} {label}", "yield": round(last, 3), "date": last_day.isoformat(),
        "chg_1d": _bp(last, prev),
        "chg_1w": _bp(last, _value_on_or_before(points, last_day - timedelta(days=7))),
        "chg_1m": _bp(last, _value_on_or_before(points, last_day - timedelta(days=30))),
        "chg_1y": _bp(last, _value_on_or_before(points, last_day - timedelta(days=365))),
        "hi_52w": round(max(year), 3) if year else None, "lo_52w": round(min(year), 3) if year else None,
        "spark": [round(v, 3) for _d, v in points[-SPARK:]],
        "bucket": "short" if years <= 2 else "medium" if years <= 10 else "long",
        "source": meta["source"],
    }


def screener(now=None):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(len(FETCHERS)) as pool:                 # the five sources download at the same time
        got = dict(zip(FETCHERS, pool.map(lambda cc: _series(cc, now), FETCHERS)))
    rows, sources = [], []
    for cc in FETCHERS:
        data, err = got[cc]
        meta = SOURCES[cc]
        as_of = None
        for tenor in sorted(data, key=tenor_years):
            pts = data[tenor]
            if not pts:
                continue
            row = bond_row(cc, tenor, pts)
            rows.append(row)
            as_of = max(as_of or row["date"], row["date"])
        sources.append({"cc": cc, "country": meta["country"], "name": meta["source"], "link": meta["link"],
                        "ok": bool(data), "as_of": as_of, "error": err, "bonds": len(data)})
    curves = {}
    for r in rows:
        curves.setdefault(r["cc"], []).append([r["years"], r["yield"]])
    return {"ok": bool(rows), "bonds": rows, "curves": curves, "sources": sources,
            "note": "End-of-day government bond yields from each country's official source. Changes are in basis "
                    "points (1 bp = 0.01%). The euro area row is the ECB's AAA yield curve, not a single bond."}


def history(bond_id, now=None):
    bond_id = (bond_id or "").strip().upper()
    cc, tenor = bond_id[:2], bond_id[2:]
    if cc not in FETCHERS or not tenor:
        return None
    data, _err = _series(cc, now)
    pts = data.get(tenor)
    if not pts:
        return None
    row = bond_row(cc, tenor, pts)
    return {"ok": True, "bond": row, "points": [[d.isoformat(), round(v, 3)] for d, v in pts]}
