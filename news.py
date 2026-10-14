"""
News + economic calendar for SignalX (Trade section).

Sources (licensed APIs and public RSS only - nothing is scraped):
  * Economic calendar: Financial Modeling Prep (FMP_API_KEY), Finnhub
    (FINNHUB_API_KEY, calendar needs a paid Finnhub plan) or Trading
    Economics (TRADING_ECONOMICS_KEY). The first configured one is used.
  * News: Finnhub market news (FINNHUB_API_KEY, free plan), FMP news
    (FMP_API_KEY) and central-bank RSS feeds (no key; NEWS_RSS_FEEDS can
    replace the list). Third-party items keep only the headline, a short
    summary, the source name and a link to the original.
  * Own articles: written in the admin panel (/admin/news, ADMIN_EMAILS only):
    draft, scheduled or published, Markdown body, cover image, category, tags.

Background job (one instance via MySQL GET_LOCK): news every NEWS_REFRESH_MIN
(10) minutes, the calendar (this week + next week) every
CALENDAR_REFRESH_MIN (30) minutes, with de-duplication on (provider, id)
and on the original URL.

Signals: blackout_reason(symbol, at) tells the forex scanner not to start a
signal for a currency from NEWS_BLACKOUT_BEFORE_MIN (30) minutes before to
NEWS_BLACKOUT_AFTER_MIN (15) minutes after a high-impact event;
upcoming_for_symbol(symbol, at) gives the warning shown on a signal.

Pages: /news (calendar + news), /news/<slug> (own article), /news/item/<id>-<slug>
(third-party summary), /admin/news. API: /api/news, /api/calendar, admin API.
"""
import hashlib
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

from flask import Blueprint, Response, abort, jsonify, redirect, render_template, request, session

import markets
from db import get_db_connection
from safe_html import markdown_to_html, slugify, strip_tags

news_bp = Blueprint("news", __name__)

CATEGORIES = ("forex", "crypto", "commodities", "economy")
IMPACTS = ("high", "medium", "low")
CURRENCIES = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "CNY", "XAU")
COUNTRY_CCY = {"US": "USD", "EU": "EUR", "EMU": "EUR", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
               "GB": "GBP", "UK": "GBP", "JP": "JPY", "CH": "CHF", "AU": "AUD", "CA": "CAD", "NZ": "NZD",
               "CN": "CNY", "UNITED STATES": "USD", "EURO AREA": "EUR", "EUROZONE": "EUR", "GERMANY": "EUR",
               "FRANCE": "EUR", "ITALY": "EUR", "SPAIN": "EUR", "UNITED KINGDOM": "GBP", "JAPAN": "JPY",
               "SWITZERLAND": "CHF", "AUSTRALIA": "AUD", "CANADA": "CAD", "NEW ZEALAND": "NZD", "CHINA": "CNY"}
DEFAULT_FEEDS = [
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml", "economy", ["USD"]),
    ("European Central Bank", "https://www.ecb.europa.eu/rss/press.html", "economy", ["EUR"]),
    ("Bank of England", "https://www.bankofengland.co.uk/rss/news", "economy", ["GBP"]),
    ("Bank of Japan", "https://www.boj.or.jp/en/rss/whatsnew.xml", "economy", ["JPY"]),
    ("Reserve Bank of Australia", "https://www.rba.gov.au/rss/rss-cb-media-releases.xml", "economy", ["AUD"]),
    ("Bank of Canada", "https://www.bankofcanada.ca/content_type/press-releases/feed/", "economy", ["CAD"]),
]
MAX_SUMMARY = 420
MAX_UPLOAD = 3 * 1024 * 1024
IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg", b"GIF87a": "image/gif",
               b"GIF89a": "image/gif", b"RIFF": "image/webp"}
ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"

_hooks = {"is_admin": None}
_status = {"news": {}, "calendar": {}, "last_news": None, "last_calendar": None}
_cache = {}
_cache_lock = threading.Lock()


def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def _iso(d):
    return d.replace(microsecond=0).isoformat() + "Z" if isinstance(d, datetime) else d


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def blackout_before():
    return _env_int("NEWS_BLACKOUT_BEFORE_MIN", 30)


def blackout_after():
    return _env_int("NEWS_BLACKOUT_AFTER_MIN", 15)


def _to_dt(v):
    """Many date formats -> naive UTC datetime (or None)."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo is None else (v - v.utcoffset()).replace(tzinfo=None)
    if isinstance(v, (int, float)):
        return datetime(1970, 1, 1) + timedelta(seconds=float(v))
    s = str(v).strip()
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(s)
        if d is not None:
            return (d - d.utcoffset()).replace(tzinfo=None) if d.utcoffset() is not None else d.replace(tzinfo=None)
    except (TypeError, ValueError, IndexError):
        pass
    s2 = s.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s2)
        return (d - d.utcoffset()).replace(tzinfo=None) if d.utcoffset() is not None else d
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


class _Cursor:
    def __enter__(self):
        self.conn = get_db_connection()
        self.cur = self.conn.cursor()
        return self.cur

    def __exit__(self, *a):
        try:
            self.cur.close()
        finally:
            self.conn.close()
        return False


# ===========================================================================
# tables
# ===========================================================================
def init_tables():
    with _Cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS news_items (
                id INT PRIMARY KEY AUTO_INCREMENT,
                provider VARCHAR(30) NOT NULL,
                ext_id VARCHAR(120) NOT NULL,
                url VARCHAR(1000) NOT NULL,
                url_hash CHAR(40) NOT NULL,
                title VARCHAR(400) NOT NULL,
                summary TEXT NULL,
                source VARCHAR(120) NULL,
                image VARCHAR(1000) NULL,
                category VARCHAR(20) NOT NULL DEFAULT 'economy',
                tags VARCHAR(400) NULL,
                published_at DATETIME NOT NULL,
                created_at DATETIME NOT NULL,
                UNIQUE (provider, ext_id),
                UNIQUE (url_hash)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS calendar_events (
                id INT PRIMARY KEY AUTO_INCREMENT,
                provider VARCHAR(30) NOT NULL,
                ext_id VARCHAR(160) NOT NULL,
                event_time DATETIME NOT NULL,
                country VARCHAR(40) NULL,
                currency VARCHAR(8) NULL,
                title VARCHAR(300) NOT NULL,
                impact VARCHAR(10) NOT NULL DEFAULT 'low',
                actual VARCHAR(40) NULL,
                forecast VARCHAR(40) NULL,
                previous VARCHAR(40) NULL,
                unit VARCHAR(20) NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE (provider, ext_id)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id INT PRIMARY KEY AUTO_INCREMENT,
                slug VARCHAR(120) NOT NULL,
                title VARCHAR(200) NOT NULL,
                summary VARCHAR(500) NULL,
                body_md MEDIUMTEXT NOT NULL,
                body_html MEDIUMTEXT NOT NULL,
                cover_id INT NULL,
                category VARCHAR(20) NOT NULL DEFAULT 'economy',
                tags VARCHAR(400) NULL,
                status VARCHAR(12) NOT NULL DEFAULT 'draft',
                publish_at DATETIME NULL,
                author_id INT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                deleted_at DATETIME NULL,
                UNIQUE (slug)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS media (
                id INT PRIMARY KEY AUTO_INCREMENT,
                mime VARCHAR(40) NOT NULL,
                data LONGBLOB NOT NULL,
                size INT NOT NULL,
                uploader_id INT NULL,
                created_at DATETIME NOT NULL
            )""")


# ===========================================================================
# helpers: tagging
# ===========================================================================
_COIN_WORDS = {"bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "ether": "ETH", "solana": "SOL", "xrp": "XRP",
               "ripple": "XRP", "dogecoin": "DOGE", "cardano": "ADA", "bnb": "BNB", "tron": "TRX", "litecoin": "LTC"}
_CCY_WORDS = {"dollar": "USD", "fed": "USD", "federal reserve": "USD", "fomc": "USD", "euro": "EUR", "ecb": "EUR",
              "pound": "GBP", "sterling": "GBP", "bank of england": "GBP", "boe": "GBP", "yen": "JPY",
              "bank of japan": "JPY", "boj": "JPY", "franc": "CHF", "snb": "CHF", "aussie": "AUD", "rba": "AUD",
              "loonie": "CAD", "bank of canada": "CAD", "kiwi": "NZD", "rbnz": "NZD", "gold": "XAU", "yuan": "CNY"}


def tags_for(text, extra=()):
    t = " " + (text or "").lower() + " "
    out = set(x.upper() for x in extra if x)
    for w, c in list(_CCY_WORDS.items()) + list(_COIN_WORDS.items()):
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t):
            out.add(c)
    for c in CURRENCIES:
        if re.search(r"(?<![a-z])" + c.lower() + r"(?![a-z])", t):
            out.add(c)
    for pair in re.findall(r"\b([a-z]{3})/?([a-z]{3})\b", t):
        a, b = pair[0].upper(), pair[1].upper()
        if a in CURRENCIES and b in CURRENCIES:
            out.update((a, b))
    return sorted(out)[:12]


def category_for(text, default="economy"):
    t = (text or "").lower()
    if re.search(r"bitcoin|crypto|ethereum|blockchain|stablecoin|altcoin|token", t):
        return "crypto"
    if re.search(r"\bgold\b|silver|\boil\b|crude|brent|copper|commodit", t):
        return "commodities"
    if re.search(r"forex|currenc|\bfx\b|eur/usd|usd/jpy|gbp/usd|dollar|yen|euro\b|sterling", t):
        return "forex"
    return default


def _short(text, n=MAX_SUMMARY):
    t = strip_tags(text or "")
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0]
    return cut + "…"


# ===========================================================================
# providers
# ===========================================================================
def _get_json(url, params, timeout=15):
    import requests
    r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "SignalX/1.0"})
    r.raise_for_status()
    return r.json()


def _impact(v):
    s = str(v or "").strip().lower()
    if s in ("3", "high", "h"):
        return "high"
    if s in ("2", "medium", "med", "m", "moderate"):
        return "medium"
    return "low"


def _num(v):
    if v is None or v == "":
        return None
    return str(v)[:40]


def calendar_provider():
    if (os.environ.get("FMP_API_KEY") or "").strip():
        return "fmp"
    if (os.environ.get("FINNHUB_API_KEY") or "").strip() and (os.environ.get("FINNHUB_CALENDAR", "on").lower() != "off"):
        return "finnhub"
    if (os.environ.get("TRADING_ECONOMICS_KEY") or "").strip():
        return "tradingeconomics"
    return None


def fetch_calendar(start, end, provider=None):
    """-> list of event dicts (naive UTC times). Raises on provider errors."""
    p = provider or calendar_provider()
    if p is None:
        return []
    out = []
    if p == "fmp":
        data = _get_json("https://financialmodelingprep.com/stable/economic-calendar",
                         {"from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d"),
                          "apikey": os.environ["FMP_API_KEY"].strip()})
        if isinstance(data, dict):
            raise ValueError(str(data.get("Error Message") or data.get("message") or data)[:200])
        for e in data or []:
            t = _to_dt(e.get("date"))
            if not t or not e.get("event"):
                continue
            ccy = (e.get("currency") or COUNTRY_CCY.get(str(e.get("country") or "").upper()) or "").upper()[:8]
            out.append({"provider": "fmp", "ext_id": f"{e.get('country')}|{e.get('event')}|{e.get('date')}"[:160],
                        "event_time": t, "country": (e.get("country") or "")[:40], "currency": ccy,
                        "title": str(e["event"])[:300], "impact": _impact(e.get("impact")),
                        "actual": _num(e.get("actual")), "forecast": _num(e.get("estimate")),
                        "previous": _num(e.get("previous")), "unit": _num(e.get("unit"))})
    elif p == "finnhub":
        data = _get_json("https://finnhub.io/api/v1/calendar/economic",
                         {"from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d"),
                          "token": os.environ["FINNHUB_API_KEY"].strip()})
        rows = (data or {}).get("economicCalendar") or []
        if isinstance(data, dict) and data.get("error"):
            raise ValueError(str(data["error"])[:200])
        for e in rows:
            t = _to_dt(e.get("time"))
            if not t or not e.get("event"):
                continue
            ccy = COUNTRY_CCY.get(str(e.get("country") or "").upper(), "")
            out.append({"provider": "finnhub", "ext_id": f"{e.get('country')}|{e.get('event')}|{e.get('time')}"[:160],
                        "event_time": t, "country": (e.get("country") or "")[:40], "currency": ccy,
                        "title": str(e["event"])[:300], "impact": _impact(e.get("impact")),
                        "actual": _num(e.get("actual")), "forecast": _num(e.get("estimate")),
                        "previous": _num(e.get("prev")), "unit": _num(e.get("unit"))})
    elif p == "tradingeconomics":
        key = os.environ["TRADING_ECONOMICS_KEY"].strip()
        data = _get_json(f"https://api.tradingeconomics.com/calendar/country/All/{start:%Y-%m-%d}/{end:%Y-%m-%d}",
                         {"c": key, "f": "json"})
        for e in data or []:
            t = _to_dt(e.get("Date"))
            if not t or not e.get("Event"):
                continue
            ccy = (e.get("Currency") or COUNTRY_CCY.get(str(e.get("Country") or "").upper()) or "").upper()[:8]
            out.append({"provider": "tradingeconomics", "ext_id": str(e.get("CalendarId") or f"{e.get('Country')}|{e.get('Event')}|{e.get('Date')}")[:160],
                        "event_time": t, "country": (e.get("Country") or "")[:40], "currency": ccy,
                        "title": str(e["Event"])[:300], "impact": _impact(e.get("Importance")),
                        "actual": _num(e.get("Actual")), "forecast": _num(e.get("Forecast") or e.get("TEForecast")),
                        "previous": _num(e.get("Previous")), "unit": _num(e.get("Unit"))})
    return out


def feeds():
    raw = (os.environ.get("NEWS_RSS_FEEDS") or "").strip()
    if raw.lower() == "off":
        return []
    if not raw:
        return list(DEFAULT_FEEDS)
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, url = part.partition("|") if "|" in part else ("", "", part)
        url = url.strip()
        if url.startswith("https://") or url.startswith("http://"):
            out.append((name.strip() or url.split("/")[2], url, "economy", []))
    return out


def parse_feed(xml_bytes, source, category="economy", tags=()):
    """RSS 2.0 / Atom -> items. Refuses DTDs and entities (XML bombs)."""
    data = xml_bytes if isinstance(xml_bytes, bytes) else str(xml_bytes).encode("utf-8")
    low = data.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise ValueError("feed has a DTD - refused")
    root = ET.fromstring(data)
    items = []
    nodes = root.findall(".//item") or root.findall(f".//{ATOM}entry")
    for it in nodes[:40]:
        def txt(*names):
            for n in names:
                el = it.find(n)
                if el is not None and (el.text or "").strip():
                    return el.text.strip()
            return ""
        link = txt("link")
        if not link:
            le = it.find(f"{ATOM}link")
            link = le.get("href") if le is not None else ""
        title = strip_tags(txt("title", f"{ATOM}title"))
        if not title or not link.startswith(("http://", "https://")):
            continue
        summary = _short(txt("description", f"{ATOM}summary", f"{ATOM}content"))
        published = _to_dt(txt("pubDate", f"{ATOM}updated", f"{ATOM}published", "{http://purl.org/dc/elements/1.1/}date"))
        img = ""
        m = it.find(f"{MEDIA}content")
        if m is None:
            m = it.find(f"{MEDIA}thumbnail")
        if m is not None and (m.get("url") or "").startswith("https://"):
            img = m.get("url")
        enc = it.find("enclosure")
        if not img and enc is not None and (enc.get("type") or "").startswith("image/") and (enc.get("url") or "").startswith("https://"):
            img = enc.get("url")
        guid = txt("guid", f"{ATOM}id") or link
        items.append({"provider": "rss", "ext_id": hashlib.sha1(guid.encode()).hexdigest(), "url": link[:1000],
                      "title": title[:400], "summary": summary, "source": source[:120], "image": img[:1000] or None,
                      "category": category_for(title + " " + summary, category),
                      "tags": tags_for(title + " " + summary, tags), "published_at": published or _utcnow()})
    return items


def fetch_news():
    """-> (items, {provider: status})."""
    import requests
    items, status = [], {}
    fk = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if fk:
        try:
            n = 0
            for cat in ("forex", "crypto", "general"):
                data = _get_json("https://finnhub.io/api/v1/news", {"category": cat, "token": fk})
                for e in (data or [])[:60]:
                    url = str(e.get("url") or "")
                    if not e.get("headline") or not url.startswith(("http://", "https://")):
                        continue
                    text = f"{e.get('headline')} {e.get('summary') or ''}"
                    items.append({"provider": "finnhub", "ext_id": str(e.get("id") or hashlib.sha1(url.encode()).hexdigest()),
                                  "url": url[:1000], "title": strip_tags(e["headline"])[:400],
                                  "summary": _short(e.get("summary")), "source": (e.get("source") or "Finnhub")[:120],
                                  "image": (e.get("image") or "")[:1000] if str(e.get("image") or "").startswith("https://") else None,
                                  "category": "crypto" if cat == "crypto" else ("forex" if cat == "forex" else category_for(text)),
                                  "tags": tags_for(text), "published_at": _to_dt(e.get("datetime")) or _utcnow()})
                    n += 1
            status["finnhub"] = {"ok": True, "items": n}
        except Exception as e:
            status["finnhub"] = {"ok": False, "error": str(e)[:200]}
    mk = (os.environ.get("FMP_API_KEY") or "").strip()
    if mk and (os.environ.get("FMP_NEWS", "on").lower() != "off"):
        try:
            n = 0
            for path, cat in (("forex-latest", "forex"), ("crypto-latest", "crypto"), ("general-latest", "economy")):
                data = _get_json(f"https://financialmodelingprep.com/stable/news/{path}", {"page": 0, "limit": 40, "apikey": mk})
                if isinstance(data, dict):
                    raise ValueError(str(data.get("Error Message") or data)[:200])
                for e in data or []:
                    url = str(e.get("url") or "")
                    if not e.get("title") or not url.startswith(("http://", "https://")):
                        continue
                    text = f"{e.get('title')} {e.get('text') or ''} {e.get('symbol') or ''}"
                    items.append({"provider": "fmp", "ext_id": hashlib.sha1(url.encode()).hexdigest(), "url": url[:1000],
                                  "title": strip_tags(e["title"])[:400], "summary": _short(e.get("text")),
                                  "source": (e.get("publisher") or e.get("site") or "FMP")[:120],
                                  "image": (e.get("image") or "")[:1000] if str(e.get("image") or "").startswith("https://") else None,
                                  "category": cat if cat != "economy" else category_for(text), "tags": tags_for(text),
                                  "published_at": _to_dt(e.get("publishedDate")) or _utcnow()})
                    n += 1
            status["fmp"] = {"ok": True, "items": n}
        except Exception as e:
            status["fmp"] = {"ok": False, "error": str(e)[:200]}
    for name, url, cat, tags in feeds():
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "SignalX/1.0 (+news reader)"})
            r.raise_for_status()
            got = parse_feed(r.content[:3_000_000], name, cat, tags)
            items.extend(got)
            status[f"rss:{name}"] = {"ok": True, "items": len(got)}
        except Exception as e:
            status[f"rss:{name}"] = {"ok": False, "error": str(e)[:200]}
    return items, status


# ===========================================================================
# storing
# ===========================================================================
def _url_hash(url):
    u = re.sub(r"[?#].*$", "", (url or "").strip().lower()).rstrip("/")
    return hashlib.sha1(u.encode()).hexdigest()


def store_news(items):
    added = 0
    now = _utcnow()
    with _Cursor() as cur:
        for it in items:
            try:
                cur.execute("""INSERT INTO news_items (provider, ext_id, url, url_hash, title, summary, source, image, category,
                               tags, published_at, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (it["provider"], it["ext_id"][:120], it["url"], _url_hash(it["url"]), it["title"],
                             it.get("summary"), it.get("source"), it.get("image"), it.get("category") or "economy",
                             json.dumps(it.get("tags") or []), min(it.get("published_at") or now, now), now))
                added += 1
            except Exception:
                continue     # duplicate (same provider id or same article URL)
    return added


def store_events(events):
    n = 0
    now = _utcnow()
    with _Cursor() as cur:
        for e in events:
            cur.execute("SELECT id FROM calendar_events WHERE provider=%s AND ext_id=%s", (e["provider"], e["ext_id"]))
            row = cur.fetchone()
            vals = (e["event_time"], e.get("country"), e.get("currency"), e["title"], e["impact"], e.get("actual"),
                    e.get("forecast"), e.get("previous"), e.get("unit"), now)
            if row:
                cur.execute("""UPDATE calendar_events SET event_time=%s, country=%s, currency=%s, title=%s, impact=%s,
                               actual=%s, forecast=%s, previous=%s, unit=%s, updated_at=%s WHERE id=%s""", (*vals, row["id"]))
            else:
                cur.execute("""INSERT INTO calendar_events (event_time, country, currency, title, impact, actual, forecast,
                               previous, unit, updated_at, provider, ext_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (*vals, e["provider"], e["ext_id"]))
            n += 1
    with _cache_lock:
        _cache.clear()
    return n


def refresh_news():
    items, status = fetch_news()
    added = store_news(items) if items else 0
    _status["news"] = status
    _status["last_news"] = _utcnow()
    return added


def refresh_calendar(now=None):
    now = now or _utcnow()
    p = calendar_provider()
    if not p:
        _status["calendar"] = {"configured": False}
        return 0
    start = now - timedelta(days=now.weekday() + 1)
    end = start + timedelta(days=15)
    try:
        ev = fetch_calendar(start, end, p)
        n = store_events(ev)
        _status["calendar"] = {"configured": True, "provider": p, "ok": True, "events": n}
    except Exception as e:
        _status["calendar"] = {"configured": True, "provider": p, "ok": False, "error": str(e)[:200]}
        n = 0
    _status["last_calendar"] = _utcnow()
    return n


def step(now=None):
    now = now or _utcnow()
    done = []
    ln, lc = _status.get("last_news"), _status.get("last_calendar")
    if ln is None or now - ln >= timedelta(minutes=_env_int("NEWS_REFRESH_MIN", 10)):
        done.append(("news", refresh_news()))
    if lc is None or now - lc >= timedelta(minutes=_env_int("CALENDAR_REFRESH_MIN", 30)):
        done.append(("calendar", refresh_calendar(now)))
    return done


def _supervisor():
    time.sleep(40)
    while True:
        lock = None
        try:
            lock = get_db_connection()
            with lock.cursor() as c:
                c.execute("SELECT GET_LOCK(%s, 0) AS got", ("signalx_news",))
                got = (c.fetchone() or {}).get("got")
            if got != 1:
                lock.close()
                lock = None
                time.sleep(120)
                continue
            while True:
                lock.ping(reconnect=False)
                try:
                    step()
                except Exception:
                    print(f"[news] refresh failed:\n{traceback.format_exc()}")
                time.sleep(60)
        except Exception:
            print(f"[news] job error, restarting:\n{traceback.format_exc()}")
        finally:
            if lock is not None:
                try:
                    lock.close()
                except Exception:
                    pass
        time.sleep(60)


_started = False


def init_news(app=None, *, is_admin=None, start=True):
    global _started
    if is_admin:
        _hooks["is_admin"] = is_admin
    try:
        init_tables()
    except Exception as e:
        print(f"[news] WARNING: could not create tables: {e}")
    if not start or (os.environ.get("NEWS_JOB", "on") or "on").lower() in ("0", "off", "false", "no"):
        return
    if _started:
        return
    _started = True
    threading.Thread(target=_supervisor, name="news-job", daemon=True).start()


# ===========================================================================
# links to signals
# ===========================================================================
def _events_between(a, b, impact="high"):
    key = ("ev", a.strftime("%Y%m%d%H%M"), b.strftime("%Y%m%d%H%M"), impact)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
    with _Cursor() as cur:
        cur.execute("""SELECT * FROM calendar_events WHERE event_time >= %s AND event_time <= %s AND impact=%s
                       ORDER BY event_time ASC""", (a, b, impact))
        rows = cur.fetchall() or []
    with _cache_lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (time.time(), rows)
    return rows


def _symbol_ccys(symbol):
    cur = markets.currencies(symbol)
    out = set()
    for c in cur:
        if c in ("XAU", "XAG"):
            out.add("USD")
        elif c == "USDT":
            continue
        else:
            out.add(c)
    return out


def blackout_reason(symbol, at):
    """A high-impact event for one of the pair's currencies from `before` minutes
    after `at` back to `after` minutes before it -> reason dict, else None."""
    if not markets.is_forex(symbol):
        if (os.environ.get("NEWS_BLACKOUT_CRYPTO", "off") or "off").lower() not in ("on", "1", "true", "yes"):
            return None
        ccys = {"USD"}
    else:
        ccys = _symbol_ccys(symbol)
    if not ccys or at is None:
        return None
    rows = _events_between(at - timedelta(minutes=blackout_after()), at + timedelta(minutes=blackout_before()))
    for r in rows:
        if (r.get("currency") or "").upper() in ccys:
            return {"title": r["title"], "currency": r["currency"], "at": _iso(r["event_time"])}
    return None


def upcoming_for_symbol(symbol, at, hours=None):
    """The next high-impact event for the symbol's currencies within `hours` (warning on a signal)."""
    hours = hours if hours is not None else _env_int("NEWS_WARNING_HOURS", 12)
    ccys = _symbol_ccys(symbol) if markets.is_forex(symbol) else {"USD"}
    if at is None:
        return None
    for r in _events_between(at, at + timedelta(hours=hours)):
        if (r.get("currency") or "").upper() in ccys:
            return {"title": r["title"], "currency": r["currency"], "at": _iso(r["event_time"]), "impact": "high"}
    return None


# ===========================================================================
# queries
# ===========================================================================
PUBLISHED_SQL = "deleted_at IS NULL AND status IN ('published','scheduled') AND publish_at IS NOT NULL AND publish_at <= %s"


def article_url(a):
    return f"/news/{a['slug']}"


def item_url(r):
    return f"/news/item/{r['id']}-{slugify(r['title'], 70)}"


def _tags(v):
    try:
        t = json.loads(v or "[]")
        return [str(x) for x in t if isinstance(x, (str, int))][:12]
    except (TypeError, ValueError):
        return []


def list_news(q=None, category=None, currency=None, page=1, per_page=20, now=None):
    now = now or _utcnow()
    w_items, a_items, w_art, a_art = [], [], [PUBLISHED_SQL], [now]
    if category in CATEGORIES:
        w_items.append("category=%s")
        a_items.append(category)
        w_art.append("category=%s")
        a_art.append(category)
    if currency:
        like = f'%"{currency}"%'
        w_items.append("tags LIKE %s")
        a_items.append(like)
        w_art.append("tags LIKE %s")
        a_art.append(like)
    if q:
        like = f"%{q}%"
        w_items.append("(title LIKE %s OR summary LIKE %s)")
        a_items += [like, like]
        w_art.append("(title LIKE %s OR summary LIKE %s)")
        a_art += [like, like]
    wi = (" WHERE " + " AND ".join(w_items)) if w_items else ""
    wa = " WHERE " + " AND ".join(w_art)
    sql = (f"SELECT 'item' AS kind, id, title, summary, source, image, category, tags, published_at AS at, url, NULL AS slug, NULL AS cover_id "
           f"FROM news_items{wi} UNION ALL "
           f"SELECT 'article' AS kind, id, title, summary, 'SignalX' AS source, NULL AS image, category, tags, publish_at AS at, "
           f"NULL AS url, slug, cover_id FROM articles{wa}")
    with _Cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM ({sql}) AS u", (*a_items, *a_art))
        total = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute(f"SELECT * FROM ({sql}) AS u ORDER BY at DESC, id DESC LIMIT %s OFFSET %s",
                    (*a_items, *a_art, per_page, (page - 1) * per_page))
        rows = cur.fetchall() or []
    out = []
    for r in rows:
        at = _to_dt(r["at"])
        item = {"kind": r["kind"], "title": r["title"], "summary": r.get("summary"), "source": r.get("source"),
                "category": r.get("category"), "tags": _tags(r.get("tags")), "published_at": _iso(at)}
        if r["kind"] == "article":
            item["url"] = f"/news/{r['slug']}"
            item["image"] = f"/media/{r['cover_id']}" if r.get("cover_id") else None
        else:
            item["url"] = item_url(r)
            item["original_url"] = r.get("url")
            item["image"] = r.get("image")
        out.append(item)
    return out, total


def calendar(start, end, currencies=None, impacts=None):
    with _Cursor() as cur:
        cur.execute("SELECT * FROM calendar_events WHERE event_time >= %s AND event_time < %s ORDER BY event_time ASC, id ASC",
                    (start, end))
        rows = cur.fetchall() or []
    out = []
    for r in rows:
        if currencies and (r.get("currency") or "") not in currencies:
            continue
        if impacts and r.get("impact") not in impacts:
            continue
        out.append({"id": r["id"], "time": _iso(_to_dt(r["event_time"])), "currency": r.get("currency"),
                    "country": r.get("country"), "title": r["title"], "impact": r["impact"], "actual": r.get("actual"),
                    "forecast": r.get("forecast"), "previous": r.get("previous"), "unit": r.get("unit")})
    return out


def next_high_impact(now=None, currencies=None):
    now = now or _utcnow()
    with _Cursor() as cur:
        cur.execute("""SELECT * FROM calendar_events WHERE event_time >= %s AND impact='high'
                       ORDER BY event_time ASC LIMIT 30""", (now,))
        rows = cur.fetchall() or []
    for r in rows:
        if currencies and (r.get("currency") or "") not in currencies:
            continue
        return {"time": _iso(_to_dt(r["event_time"])), "currency": r.get("currency"), "title": r["title"]}
    return None


def sitemap_entries(limit=500):
    with _Cursor() as cur:
        cur.execute(f"SELECT slug, updated_at FROM articles WHERE {PUBLISHED_SQL} ORDER BY publish_at DESC LIMIT %s",
                    (_utcnow(), limit))
        rows = cur.fetchall() or []
    return [(f"/news/{r['slug']}", (_to_dt(r["updated_at"]) or _utcnow()).strftime("%Y-%m-%d")) for r in rows]


def status():
    return {"calendar_provider": calendar_provider(), "news_sources": {
        "finnhub": bool((os.environ.get("FINNHUB_API_KEY") or "").strip()),
        "fmp": bool((os.environ.get("FMP_API_KEY") or "").strip()),
        "rss_feeds": len(feeds())},
        "last_news": _iso(_status.get("last_news")), "last_calendar": _iso(_status.get("last_calendar")),
        "news": _status.get("news"), "calendar": _status.get("calendar"),
        "blackout": {"before_min": blackout_before(), "after_min": blackout_after()}}


# ===========================================================================
# pages + API
# ===========================================================================
def _is_admin():
    fn = _hooks.get("is_admin")
    try:
        return bool(fn and fn())
    except Exception:
        return False


def _int_arg(name, default, lo, hi):
    try:
        return max(lo, min(int(request.args.get(name, default)), hi))
    except (TypeError, ValueError):
        return default


@news_bp.route("/news", methods=["GET"])
def news_page():
    return render_template("news.html", active="news", currencies=CURRENCIES, categories=CATEGORIES,
                           logged_in=bool(session.get("user_id")))


@news_bp.route("/api/news", methods=["GET"])
def api_news():
    q = (request.args.get("q") or "").strip()[:80] or None
    cat = request.args.get("category") if request.args.get("category") in CATEGORIES else None
    ccy = (request.args.get("currency") or "").upper()
    ccy = ccy if ccy in CURRENCIES or ccy in ("BTC", "ETH", "SOL", "XRP") else None
    page = _int_arg("page", 1, 1, 500)
    per = _int_arg("per_page", 20, 1, 50)
    try:
        items, total = list_news(q, cat, ccy, page, per)
    except Exception as e:
        return jsonify({"ok": False, "error": "News could not be loaded right now."}), 500
    return jsonify({"ok": True, "items": items, "total": total, "page": page, "pages": max(1, -(-total // per)),
                    "configured": bool(status()["news_sources"]["finnhub"] or status()["news_sources"]["fmp"]
                                       or status()["news_sources"]["rss_feeds"])})


@news_bp.route("/api/calendar", methods=["GET"])
def api_calendar():
    now = _utcnow()
    start = _to_dt(request.args.get("from")) or (now - timedelta(days=1))
    end = _to_dt(request.args.get("to")) or (now + timedelta(days=8))
    if end <= start or end - start > timedelta(days=31):
        return jsonify({"ok": False, "error": "Choose a range of at most 31 days."}), 400
    ccys = [c for c in (request.args.get("currency") or "").upper().split(",") if c in CURRENCIES]
    imps = [i for i in (request.args.get("impact") or "").lower().split(",") if i in IMPACTS]
    try:
        events = calendar(start, end, ccys or None, imps or None)
        nxt = next_high_impact(now, ccys or None)
    except Exception:
        return jsonify({"ok": False, "error": "The calendar could not be loaded right now."}), 500
    st = status()
    return jsonify({"ok": True, "events": events, "next_high": nxt, "now": _iso(now),
                    "configured": st["calendar_provider"] is not None, "provider": st["calendar_provider"],
                    "blackout": st["blackout"]})


def _published_article(slug):
    with _Cursor() as cur:
        cur.execute(f"SELECT * FROM articles WHERE slug=%s AND {PUBLISHED_SQL}", (slug, _utcnow()))
        return cur.fetchone()


@news_bp.route("/news/<slug>", methods=["GET"])
def article_page(slug):
    if not re.fullmatch(r"[a-z0-9-]{1,120}", slug or ""):
        abort(404)
    a = _published_article(slug)
    if not a and _is_admin():
        with _Cursor() as cur:
            cur.execute("SELECT * FROM articles WHERE slug=%s AND deleted_at IS NULL", (slug,))
            a = cur.fetchone()
            if a:
                a = dict(a, preview=True)
    if not a:
        abort(404)
    site = (os.environ.get("SITE_URL") or "").rstrip("/")
    return render_template("article.html", a=a, tags=_tags(a.get("tags")), published=_to_dt(a.get("publish_at")),
                           updated=_to_dt(a.get("updated_at")), canonical=f"{site}/news/{a['slug']}" if site else None,
                           cover=f"/media/{a['cover_id']}" if a.get("cover_id") else None, active="news",
                           logged_in=bool(session.get("user_id")))


@news_bp.route("/news/item/<int:iid>-<slug>", methods=["GET"])
@news_bp.route("/news/item/<int:iid>", methods=["GET"])
def item_page(iid, slug=None):
    with _Cursor() as cur:
        cur.execute("SELECT * FROM news_items WHERE id=%s", (iid,))
        r = cur.fetchone()
    if not r:
        abort(404)
    want = slugify(r["title"], 70)
    if slug != want:
        return redirect(f"/news/item/{iid}-{want}", code=301)
    return render_template("news-item.html", it=r, tags=_tags(r.get("tags")), published=_to_dt(r.get("published_at")),
                           active="news", logged_in=bool(session.get("user_id")))


@news_bp.route("/media/<int:mid>", methods=["GET"])
def media(mid):
    with _Cursor() as cur:
        cur.execute("SELECT mime, data FROM media WHERE id=%s", (mid,))
        r = cur.fetchone()
    if not r:
        abort(404)
    resp = Response(bytes(r["data"]), mimetype=r["mime"])
    resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---- admin -------------------------------------------------------------------
def _need_admin():
    if not session.get("user_id"):
        return jsonify({"ok": False, "error": "Please log in."}), 401
    if not _is_admin():
        return jsonify({"ok": False, "error": "Only the site admin can do this."}), 403
    return None


@news_bp.route("/admin/news", methods=["GET"])
def admin_page():
    if not session.get("user_id"):
        return redirect("/login")
    if not _is_admin():
        abort(404)
    return render_template("admin-news.html", active="admin-news", categories=CATEGORIES, status=status())


def _article_json(a, full=False):
    out = {"id": a["id"], "slug": a["slug"], "title": a["title"], "summary": a.get("summary"),
           "category": a.get("category"), "tags": _tags(a.get("tags")), "status": a.get("status"),
           "publish_at": _iso(_to_dt(a.get("publish_at"))), "created_at": _iso(_to_dt(a.get("created_at"))),
           "updated_at": _iso(_to_dt(a.get("updated_at"))), "deleted": a.get("deleted_at") is not None,
           "cover": f"/media/{a['cover_id']}" if a.get("cover_id") else None, "cover_id": a.get("cover_id"),
           "url": f"/news/{a['slug']}"}
    if full:
        out["body_md"] = a.get("body_md")
    return out


def _clean_article(data, existing=None):
    if not isinstance(data, dict):
        raise ValueError("Invalid request.")
    title = strip_tags(data.get("title") if data.get("title") is not None else (existing or {}).get("title") or "")[:200]
    if len(title) < 5:
        raise ValueError("The title needs at least 5 characters.")
    body = str(data.get("body_md") if data.get("body_md") is not None else (existing or {}).get("body_md") or "")
    if len(body.strip()) < 20:
        raise ValueError("The article body needs at least 20 characters.")
    if len(body) > 200_000:
        raise ValueError("The article body is too long (200,000 characters at most).")
    summary = strip_tags(data.get("summary") if data.get("summary") is not None else (existing or {}).get("summary") or "")[:500]
    category = data.get("category") or (existing or {}).get("category") or "economy"
    if category not in CATEGORIES:
        raise ValueError("Unknown category.")
    tags = data.get("tags")
    if tags is None:
        tags = _tags((existing or {}).get("tags"))
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    if not isinstance(tags, list):
        raise ValueError("Tags must be a list.")
    tags = sorted({re.sub(r"[^A-Za-z0-9/ .-]", "", str(t)).strip().upper()[:20] for t in tags if str(t).strip()})[:12]
    status_ = data.get("status") or (existing or {}).get("status") or "draft"
    if status_ not in ("draft", "published", "scheduled"):
        raise ValueError("Status must be draft, published or scheduled.")
    publish_at = _to_dt(data.get("publish_at")) if data.get("publish_at") else _to_dt((existing or {}).get("publish_at"))
    now = _utcnow()
    if status_ == "scheduled":
        same_time = existing is not None and publish_at is not None and _to_dt(existing.get("publish_at")) == publish_at
        if publish_at and publish_at <= now and same_time:
            status_ = "published"            # its time has come: editing keeps it live
        elif not publish_at or publish_at <= now:
            raise ValueError("Pick a publish time in the future for a scheduled article.")
    elif status_ == "published":
        publish_at = publish_at if (existing and existing.get("status") == "published" and publish_at) else now
    else:
        publish_at = None
    slug = slugify(data.get("slug") or (existing or {}).get("slug") or title, 100)
    cover_id = data.get("cover_id", (existing or {}).get("cover_id"))
    if cover_id not in (None, ""):
        try:
            cover_id = int(cover_id)
        except (TypeError, ValueError):
            raise ValueError("Invalid cover image.")
    else:
        cover_id = None
    return {"title": title, "summary": summary or _short(markdown_to_html(body), 300), "body_md": body,
            "body_html": markdown_to_html(body), "category": category, "tags": json.dumps(tags), "status": status_,
            "publish_at": publish_at, "slug": slug, "cover_id": cover_id}


def _unique_slug(cur, slug, aid=None):
    base, k = slug, 2
    while True:
        cur.execute("SELECT id FROM articles WHERE slug=%s", (slug,))
        r = cur.fetchone()
        if not r or (aid is not None and r["id"] == aid):
            return slug
        slug = f"{base[:110]}-{k}"
        k += 1


@news_bp.route("/api/admin/articles", methods=["GET", "POST"])
def admin_articles():
    bad = _need_admin()
    if bad:
        return bad
    if request.method == "GET":
        deleted = request.args.get("deleted") == "1"
        with _Cursor() as cur:
            cur.execute("SELECT * FROM articles WHERE deleted_at IS " + ("NOT NULL" if deleted else "NULL")
                        + " ORDER BY updated_at DESC LIMIT 200")
            rows = cur.fetchall() or []
        return jsonify({"ok": True, "articles": [_article_json(a) for a in rows]})
    try:
        v = _clean_article(request.get_json(silent=True))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    now = _utcnow()
    with _Cursor() as cur:
        v["slug"] = _unique_slug(cur, v["slug"])
        cur.execute("""INSERT INTO articles (slug, title, summary, body_md, body_html, cover_id, category, tags, status,
                       publish_at, author_id, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (v["slug"], v["title"], v["summary"], v["body_md"], v["body_html"], v["cover_id"], v["category"],
                     v["tags"], v["status"], v["publish_at"], session.get("user_id"), now, now))
        cur.execute("SELECT * FROM articles WHERE slug=%s", (v["slug"],))
        a = cur.fetchone()
    return jsonify({"ok": True, "article": _article_json(a, full=True)})


@news_bp.route("/api/admin/articles/<int:aid>", methods=["GET", "PUT", "DELETE"])
def admin_article(aid):
    bad = _need_admin()
    if bad:
        return bad
    with _Cursor() as cur:
        cur.execute("SELECT * FROM articles WHERE id=%s", (aid,))
        a = cur.fetchone()
    if not a:
        return jsonify({"ok": False, "error": "Article not found."}), 404
    if request.method == "GET":
        return jsonify({"ok": True, "article": _article_json(a, full=True)})
    if request.method == "DELETE":
        with _Cursor() as cur:
            cur.execute("UPDATE articles SET deleted_at=%s, updated_at=%s WHERE id=%s", (_utcnow(), _utcnow(), aid))
        return jsonify({"ok": True, "deleted": True})
    try:
        v = _clean_article(request.get_json(silent=True), existing=a)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    with _Cursor() as cur:
        v["slug"] = _unique_slug(cur, v["slug"], aid)
        cur.execute("""UPDATE articles SET slug=%s, title=%s, summary=%s, body_md=%s, body_html=%s, cover_id=%s, category=%s,
                       tags=%s, status=%s, publish_at=%s, updated_at=%s WHERE id=%s""",
                    (v["slug"], v["title"], v["summary"], v["body_md"], v["body_html"], v["cover_id"], v["category"],
                     v["tags"], v["status"], v["publish_at"], _utcnow(), aid))
        cur.execute("SELECT * FROM articles WHERE id=%s", (aid,))
        a = cur.fetchone()
    return jsonify({"ok": True, "article": _article_json(a, full=True)})


@news_bp.route("/api/admin/articles/<int:aid>/restore", methods=["POST"])
def admin_restore(aid):
    bad = _need_admin()
    if bad:
        return bad
    with _Cursor() as cur:
        cur.execute("UPDATE articles SET deleted_at=NULL, status='draft', updated_at=%s WHERE id=%s", (_utcnow(), aid))
        n = cur.rowcount
    if not n:
        return jsonify({"ok": False, "error": "Article not found."}), 404
    return jsonify({"ok": True})


@news_bp.route("/api/admin/preview", methods=["POST"])
def admin_preview():
    bad = _need_admin()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    body = str(data.get("body_md") or "")[:200_000]
    return jsonify({"ok": True, "html": markdown_to_html(body)})


def _image_type(data):
    for sig, mime in IMAGE_TYPES.items():
        if data.startswith(sig):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return None


@news_bp.route("/api/admin/media", methods=["POST"])
def admin_media():
    bad = _need_admin()
    if bad:
        return bad
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "Choose an image file."}), 400
    data = f.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return jsonify({"ok": False, "error": "The image is larger than 3 MB."}), 400
    mime = _image_type(data)
    if not mime:
        return jsonify({"ok": False, "error": "Only PNG, JPEG, GIF or WebP images are allowed."}), 400
    with _Cursor() as cur:
        cur.execute("INSERT INTO media (mime, data, size, uploader_id, created_at) VALUES (%s,%s,%s,%s,%s)",
                    (mime, data, len(data), session.get("user_id"), _utcnow()))
        mid = cur.lastrowid
    return jsonify({"ok": True, "id": mid, "url": f"/media/{mid}"})


@news_bp.route("/api/admin/news/refresh", methods=["POST"])
def admin_refresh():
    bad = _need_admin()
    if bad:
        return bad
    added = refresh_news()
    n = refresh_calendar()
    return jsonify({"ok": True, "news_added": added, "calendar_events": n, "status": status()})


@news_bp.route("/api/admin/news/status", methods=["GET"])
def admin_status():
    bad = _need_admin()
    if bad:
        return bad
    return jsonify({"ok": True, "status": status()})
