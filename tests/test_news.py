"""Tests for news.py + safe_html.py (SQLite shim, providers mocked, no network).
Run from the project root:  python tests/test_news.py
"""
import io
import json
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from dbshim import setup  # noqa: E402

Conn = setup("news")
import news  # noqa: E402
import safe_html  # noqa: E402

news.get_db_connection = Conn
FAILS = []


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


for k in ("FMP_API_KEY", "FINNHUB_API_KEY", "TRADING_ECONOMICS_KEY", "NEWS_RSS_FEEDS", "NEWS_BLACKOUT_CRYPTO"):
    os.environ.pop(k, None)

# ---------------------------------------------------------------------------
# markdown + sanitising
# ---------------------------------------------------------------------------
h = safe_html.markdown_to_html('# Rates\n\nThe **ECB** <script>alert(1)</script> [x](javascript:alert(1)) '
                               '![a" onerror="x](https://img.example/a.png) [ok](https://ecb.europa.eu)')
check("<script>" not in h and "&lt;script&gt;" in h, "raw HTML in an article is escaped")
check("javascript:" not in h.split("&lt;")[0] and 'href="javascript' not in h, "javascript: links are dropped")
check('onerror="x"' not in h and "alt=\"a&quot; onerror=&quot;x\"" in h, "image alt text cannot break out of the attribute")
check('<h2>Rates</h2>' in h and '<strong>ECB</strong>' in h and 'rel="nofollow noopener"' in h, "headings, bold, outside links")
check(safe_html.slugify("ECB holds rates: what's next?") == "ecb-holds-rates-what-s-next", "slugs")
h2 = safe_html.markdown_to_html("[a](//evil.example/x) [b](/\\evil.example) [c](/pricing)")
check('href="//evil' not in h2 and "evil.example" not in h2 and 'href="/pricing"' in h2, "protocol-relative links (//host) are not accepted")
h3 = safe_html.markdown_to_html("[doc](https://example.com/_notes_/page) and _slanted_ text")
check('href="https://example.com/_notes_/page"' in h3 and "<em>slanted</em>" in h3 and "<em>notes</em>" not in h3, "italic rules never change a link's address")
check("<p>x 0 y</p>" in safe_html.markdown_to_html("x \x000\x00 y"), "stray internal marker characters cannot crash the renderer")
check(safe_html.strip_tags("<p>Hi <b>there</b></p>") == "Hi there", "strip tags")

# ---------------------------------------------------------------------------
# feeds, tags, categories
# ---------------------------------------------------------------------------
rss = b"""<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<item><title>Fed keeps rates on hold; dollar steady</title><link>https://www.federalreserve.gov/a1</link>
<description>&lt;p&gt;The FOMC kept the target range unchanged.&lt;/p&gt;</description>
<pubDate>Wed, 14 Oct 2026 18:00:00 GMT</pubDate><guid>a1</guid>
<media:content url="https://www.federalreserve.gov/img.png"/></item>
<item><title>No link item</title></item>
</channel></rss>"""
items = news.parse_feed(rss, "Federal Reserve", "economy", ["USD"])
check(len(items) == 1 and items[0]["title"].startswith("Fed keeps") and items[0]["summary"].startswith("The FOMC")
      and "<p>" not in items[0]["summary"], "RSS: headline + plain summary, items without a link skipped")
check(items[0]["published_at"] == datetime(2026, 10, 14, 18, 0) and items[0]["image"] == "https://www.federalreserve.gov/img.png",
      "RSS: UTC time and image")
check("USD" in items[0]["tags"], "RSS: currency tags")
atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>ECB raises rates</title>
<link href="https://www.ecb.europa.eu/p1"/><id>p1</id><updated>2026-10-14T12:15:00+02:00</updated>
<summary>Euro area inflation.</summary></entry></feed>"""
a = news.parse_feed(atom, "ECB")
check(len(a) == 1 and a[0]["published_at"] == datetime(2026, 10, 14, 10, 15) and "EUR" in a[0]["tags"], "Atom feed + time zone")
bomb = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><rss><channel></channel></rss>'
try:
    news.parse_feed(bomb, "x")
    check(False, "feeds with a DTD are refused")
except ValueError:
    check(True, "feeds with a DTD are refused")
check(news.category_for("Bitcoin ETF inflows") == "crypto" and news.category_for("Gold climbs") == "commodities"
      and news.category_for("EUR/USD slips as dollar firms") == "forex", "categories")
check(set(news.tags_for("EUR/USD falls after ECB")) >= {"EUR", "USD"}, "pair tags")

# ---------------------------------------------------------------------------
# storage + dedupe
# ---------------------------------------------------------------------------
news.init_tables()
news.init_tables()
NOW = datetime(2026, 10, 14, 12, 0)
news._utcnow = lambda: NOW
base = {"provider": "finnhub", "ext_id": "1", "url": "https://reuters.com/x?utm=1", "title": "Dollar rises", "summary": "s",
        "source": "Reuters", "image": None, "category": "forex", "tags": ["USD"], "published_at": NOW - timedelta(hours=1)}
n1 = news.store_news([base, dict(base, ext_id="2", url="https://reuters.com/x"), dict(base, provider="fmp", ext_id="1")])
check(n1 == 1, "the same article from two providers / two ids is stored once")
news.store_news([dict(base, ext_id=str(i), url=f"https://site.com/{i}", title=f"Crypto story {i}", category="crypto",
                      tags=["BTC"], published_at=NOW - timedelta(minutes=i)) for i in range(3, 30)])

# ---------------------------------------------------------------------------
# calendar providers (mocked) + upsert
# ---------------------------------------------------------------------------
calls = []


def fake_get(url, params, timeout=15):
    calls.append(url)
    if "financialmodelingprep" in url:
        return [{"date": "2026-10-14 12:30:00", "country": "US", "event": "CPI m/m", "currency": "USD", "previous": 0.2,
                 "estimate": 0.3, "actual": None, "impact": "High", "unit": "%"},
                {"date": "2026-10-14 09:00:00", "country": "EU", "event": "ZEW Sentiment", "currency": "EUR",
                 "previous": 10, "estimate": 12, "actual": 14, "impact": "Medium"},
                {"date": "2026-10-15 23:50:00", "country": "JP", "event": "Machinery Orders", "currency": "JPY",
                 "impact": "Low"}]
    if "finnhub" in url:
        return {"economicCalendar": [{"time": "2026-10-16 08:00:00", "country": "GB", "event": "GDP", "impact": "high",
                                      "prev": 0.1, "estimate": 0.2, "actual": None, "unit": "%"}]}
    if "tradingeconomics" in url:
        return [{"CalendarId": "77", "Date": "2026-10-17T14:00:00", "Country": "Canada", "Event": "CPI", "Importance": 3,
                 "Actual": "", "Previous": "2.1", "Forecast": "2.0"}]
    raise AssertionError(url)


news._get_json = fake_get
check(news.calendar_provider() is None and news.refresh_calendar() == 0, "calendar: 'not configured' without a key")
os.environ["FMP_API_KEY"] = "k"
check(news.calendar_provider() == "fmp" and news.refresh_calendar(NOW) == 3, "calendar: FMP events stored")
os.environ["FINNHUB_API_KEY"] = "f"
os.environ["TRADING_ECONOMICS_KEY"] = "t"
ev = news.fetch_calendar(NOW, NOW + timedelta(days=7), "finnhub")
check(ev[0]["currency"] == "GBP" and ev[0]["impact"] == "high", "calendar: Finnhub mapping (country -> currency)")
ev = news.fetch_calendar(NOW, NOW + timedelta(days=7), "tradingeconomics")
check(ev[0]["currency"] == "CAD" and ev[0]["impact"] == "high" and ev[0]["previous"] == "2.1", "calendar: Trading Economics mapping")
orig = fake_get
os.environ.pop("FINNHUB_API_KEY")
os.environ.pop("TRADING_ECONOMICS_KEY")


def fake_get2(url, params, timeout=15):
    data = orig(url, params, timeout)
    data[0]["actual"] = 0.4
    return data


news._get_json = fake_get2
news.refresh_calendar(NOW)
with news._Cursor() as cur:
    cur.execute("SELECT COUNT(*) AS n FROM calendar_events")
    cnt = cur.fetchone()["n"]
    cur.execute("SELECT actual FROM calendar_events WHERE title='CPI m/m'")
    act = cur.fetchone()["actual"]
check(cnt == 3 and act == "0.4", "calendar refresh updates the actual value instead of duplicating")

# ---------------------------------------------------------------------------
# news blackout for signals
# ---------------------------------------------------------------------------
cpi = datetime(2026, 10, 14, 12, 30)
check(news.blackout_reason("EUR/USD", cpi - timedelta(minutes=20)) is not None, "blackout: 20 min before USD CPI")
check(news.blackout_reason("EUR/USD", cpi + timedelta(minutes=10)) is not None, "blackout: 10 min after")
check(news.blackout_reason("EUR/USD", cpi - timedelta(minutes=45)) is None, "no blackout 45 min before")
check(news.blackout_reason("EUR/USD", cpi + timedelta(minutes=20)) is None, "no blackout 20 min after")
check(news.blackout_reason("XAU/USD", cpi) is not None, "gold is blocked by USD events")
check(news.blackout_reason("EUR/GBP", cpi) is None, "EUR/GBP is not blocked by a USD event")
check(news.blackout_reason("EUR/USD", datetime(2026, 10, 14, 9, 0)) is None, "medium-impact events do not block")
check(news.blackout_reason("BTC/USDT", cpi) is None, "crypto is not blocked by default")
os.environ["NEWS_BLACKOUT_CRYPTO"] = "on"
check(news.blackout_reason("BTC/USDT", cpi) is not None, "NEWS_BLACKOUT_CRYPTO=on blocks crypto around USD events")
os.environ.pop("NEWS_BLACKOUT_CRYPTO")
os.environ["NEWS_BLACKOUT_BEFORE_MIN"] = "60"
news._cache.clear()
check(news.blackout_reason("EUR/USD", cpi - timedelta(minutes=50)) is not None, "blackout window is configurable")
os.environ.pop("NEWS_BLACKOUT_BEFORE_MIN")
w = news.upcoming_for_symbol("USD/JPY", cpi - timedelta(hours=3))
check(w and w["title"] == "CPI m/m" and w["currency"] == "USD", "signal warning: next high-impact event within 12 h")

# ---------------------------------------------------------------------------
# API + pages
# ---------------------------------------------------------------------------
from flask import Flask, session  # noqa: E402

TPL = os.path.join(os.path.dirname(HERE), "templates")
app = Flask(__name__, template_folder=TPL, static_folder=os.path.join(os.path.dirname(HERE), "static"))
app.secret_key = "t"
ADMIN = {"on": False}
app.register_blueprint(news.news_bp)
news.init_news(app, is_admin=lambda: ADMIN["on"], start=False)
app.jinja_env.globals.update(track_record_public=False, current_year=2026, ga_measurement_id=None, site_url="https://signals.example",
                             support_email="", is_admin=False)


@app.route("/login")
def login():
    return "login"


cl = app.test_client()
d = cl.get("/api/news?per_page=10").get_json()
check(d["ok"] and d["total"] == 28 and len(d["items"]) == 10 and d["pages"] == 3, "news feed with pagination")
d = cl.get("/api/news?category=forex").get_json()
check(d["total"] == 1 and d["items"][0]["title"] == "Dollar rises" and d["items"][0]["url"].startswith("/news/item/"),
      "category filter; third-party items link to a summary page")
d = cl.get("/api/news?q=story%201").get_json()
check(d["total"] >= 1 and all("story 1" in x["title"].lower() for x in d["items"]), "search")
d = cl.get("/api/news?currency=USD").get_json()
check(d["total"] == 1, "currency filter")
iid = cl.get("/api/news?category=forex").get_json()["items"][0]["url"].split("/")[-1].split("-")[0]
r = cl.get(f"/news/item/{iid}")
check(r.status_code == 301 and r.headers["Location"].endswith(f"/news/item/{iid}-dollar-rises"), "item URL redirects to its SEO slug")
r = cl.get(f"/news/item/{iid}-dollar-rises")
html = r.get_data(as_text=True)
check(r.status_code == 200 and "Read the full story at Reuters" in html and 'rel="nofollow noopener noreferrer"' in html,
      "third-party item page: summary + link to the source")
check(cl.get("/news/item/99999-x").status_code == 404, "unknown item 404")
d = cl.get("/api/calendar?from=2026-10-13T00:00:00&to=2026-10-20T00:00:00").get_json()
check(d["ok"] and len(d["events"]) == 3 and d["configured"] and d["next_high"]["title"] == "CPI m/m", "calendar API + next high-impact")
d = cl.get("/api/calendar?from=2026-10-13T00:00:00&to=2026-10-20T00:00:00&impact=high").get_json()
check([e["title"] for e in d["events"]] == ["CPI m/m"], "calendar impact filter")
d = cl.get("/api/calendar?from=2026-10-13T00:00:00&to=2026-10-20T00:00:00&currency=EUR").get_json()
check([e["currency"] for e in d["events"]] == ["EUR"], "calendar currency filter")
check(cl.get("/api/calendar?from=2026-01-01&to=2026-06-01").status_code == 400, "calendar range limited to 31 days")
r = cl.get("/news")
check(r.status_code == 200 and "Economic calendar" in r.get_data(as_text=True), "news page renders for visitors")

# ---- admin
check(cl.post("/api/admin/articles", json={}).status_code == 401, "admin API needs a login")
with cl.session_transaction() as s:
    s["user_id"] = 1
check(cl.post("/api/admin/articles", json={}).status_code == 403, "admin API: admins only")
check(cl.get("/admin/news").status_code == 404, "admin page hidden from non-admins")
ADMIN["on"] = True
check(cl.get("/admin/news").status_code == 200, "admin page for the admin")
r = cl.post("/api/admin/articles", json={"title": "Hi", "body_md": "short"})
check(r.status_code == 400, "validation: title and body length")
body = "## What happened\n\nThe dollar **rose** after CPI. <script>x</script>\n\n- point one\n- point two"
r = cl.post("/api/admin/articles", json={"title": "Dollar after the CPI report", "body_md": body, "category": "forex",
                                         "tags": "USD, eur", "status": "draft"})
art = r.get_json()["article"]
check(r.status_code == 200 and art["status"] == "draft" and art["slug"] == "dollar-after-the-cpi-report" and art["tags"] == ["EUR", "USD"],
      "admin creates a draft")
with cl.session_transaction() as s:
    s.clear()
ADMIN["on"] = False
check(cl.get("/news/dollar-after-the-cpi-report").status_code == 404, "drafts are not public")
with cl.session_transaction() as s:
    s["user_id"] = 1
ADMIN["on"] = True
r = cl.get("/news/dollar-after-the-cpi-report")
check(r.status_code == 200 and "Preview" in r.get_data(as_text=True), "admin can preview a draft")
r = cl.put(f"/api/admin/articles/{art['id']}", json={"status": "published"})
check(r.status_code == 200 and r.get_json()["article"]["status"] == "published", "publish")
with cl.session_transaction() as s:
    s.clear()
ADMIN["on"] = False
r = cl.get("/news/dollar-after-the-cpi-report")
html = r.get_data(as_text=True)
check(r.status_code == 200 and "<strong>rose</strong>" in html and "<script>x</script>" not in html
      and '"@type": "NewsArticle"' in html and 'rel="canonical" href="https://signals.example/news/dollar-after-the-cpi-report"' in html,
      "published article: safe HTML, JSON-LD and canonical URL")
d = cl.get("/api/news?category=forex").get_json()
check(d["items"][0]["kind"] == "article" and d["items"][0]["source"] == "SignalX" and d["total"] == 2,
      "own articles appear in the news feed")
with cl.session_transaction() as s:
    s["user_id"] = 1
ADMIN["on"] = True
r = cl.post("/api/admin/articles", json={"title": "Scheduled weekly outlook", "body_md": body, "status": "scheduled",
                                         "publish_at": "2026-10-14T15:00:00Z"})
sch = r.get_json()["article"]
check(r.status_code == 200 and sch["status"] == "scheduled", "schedule an article")
r = cl.post("/api/admin/articles", json={"title": "Bad schedule time", "body_md": body, "status": "scheduled",
                                         "publish_at": "2026-10-01T15:00:00Z"})
check(r.status_code == 400, "a scheduled time must be in the future")
with cl.session_transaction() as s:
    s.clear()
ADMIN["on"] = False
check(cl.get("/news/scheduled-weekly-outlook").status_code == 404, "scheduled article hidden before its time")
NOW = datetime(2026, 10, 14, 15, 1)
check(cl.get("/news/scheduled-weekly-outlook").status_code == 200, "scheduled article appears at its time")
with cl.session_transaction() as s:
    s["user_id"] = 1
ADMIN["on"] = True
r = cl.post("/api/admin/articles", json={"title": "Dollar after the CPI report", "body_md": body})
check(r.get_json()["article"]["slug"] == "dollar-after-the-cpi-report-2", "duplicate titles get a unique slug")
check(cl.delete(f"/api/admin/articles/{art['id']}").status_code == 200, "delete (soft)")
with cl.session_transaction() as s:
    s.clear()
ADMIN["on"] = False
check(cl.get("/news/dollar-after-the-cpi-report").status_code == 404, "deleted article is gone from the site")
with cl.session_transaction() as s:
    s["user_id"] = 1
ADMIN["on"] = True
d = cl.get("/api/admin/articles?deleted=1").get_json()
check(len(d["articles"]) == 1 and d["articles"][0]["deleted"], "deleted articles can be listed")
check(cl.post(f"/api/admin/articles/{art['id']}/restore").status_code == 200, "restore")
d = cl.get("/api/admin/articles").get_json()
check(any(a["id"] == art["id"] and a["status"] == "draft" for a in d["articles"]), "restored as a draft (data kept)")
png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
r = cl.post("/api/admin/media", data={"file": (io.BytesIO(png), "c.png")}, content_type="multipart/form-data")
mid = r.get_json().get("id")
check(r.status_code == 200 and mid, "cover upload (PNG)")
r = cl.post("/api/admin/media", data={"file": (io.BytesIO(b"<svg onload=x>"), "x.png")}, content_type="multipart/form-data")
check(r.status_code == 400, "non-images rejected by content, not by name")
r = cl.post("/api/admin/media", data={"file": (io.BytesIO(png + b"0" * (3 * 1024 * 1024)), "big.png")}, content_type="multipart/form-data")
check(r.status_code == 400, "images over 3 MB rejected")
r = cl.get(f"/media/{mid}")
check(r.status_code == 200 and r.mimetype == "image/png" and r.headers.get("X-Content-Type-Options") == "nosniff", "media served safely")
r = cl.post("/api/admin/preview", json={"body_md": "**b** <img src=x onerror=alert(1)>"})
check("<strong>b</strong>" in r.get_json()["html"] and "<img src=x" not in r.get_json()["html"], "preview is sanitised")
r = cl.put(f"/api/admin/articles/{sch['id']}", json={"cover_id": mid})
check(r.get_json()["article"]["cover"] == f"/media/{mid}", "cover attached to an article")
os.environ["NEWS_RSS_FEEDS"] = "off"
r = cl.post("/api/admin/news/refresh")
check(r.status_code == 200 and r.get_json()["ok"], "admin refresh runs the providers")
entries = news.sitemap_entries()
check(any(p == "/news/scheduled-weekly-outlook" for p, _ in entries), "published articles listed for the sitemap")

print()
print(f"{len(FAILS)} failed" if FAILS else "ALL PASSED")
sys.exit(1 if FAILS else 0)
