"""Renders the public pages and checks robots.txt / sitemap.xml / SEO tags.
The new main.py block is loaded on its own (main.py needs the exchange and
DB to import). Run from the project root:  python tests/test_public_pages.py
"""
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["TRACK_RECORD_ENGINE"] = "off"
pm = types.ModuleType("pymysql")
pm.cursors = types.ModuleType("pymysql.cursors")
pm.cursors.DictCursor = object


def _no_db(**kw):
    raise RuntimeError("no database in this test")


pm.connect = _no_db
sys.modules.setdefault("pymysql", pm)
sys.modules.setdefault("pymysql.cursors", pm.cursors)

from flask import Flask, render_template, request, session  # noqa: E402

app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"), static_folder=os.path.join(ROOT, "static"))
app.secret_key = "t"


class _Limiter:
    def limit(self, *_a, **_k):
        return lambda bp: bp


src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read().replace("\r\n", "\n")
block = src[src.index("# TRACK RECORD + PUBLIC PAGES + SEO"):src.index('if __name__ == "__main__":')]
ns = {"app": app, "limiter": _Limiter(), "os": os, "request": request, "render_template": render_template,
      "get_candles": lambda **k: None, "get_signal_engine": lambda: None, "AVAILABLE_COINS": ["BTC/USDT"],
      # the Live chart + Liquidity scanner registration (market_tools.py) sits in the same block
      "exchange": None, "build_liquidity_payload": lambda *a, **k: {}, "FOREX_PAIRS": ["EUR/USD"], "_is_forex_pair": lambda s: s == "EUR/USD"}
exec(compile(block, "main.py (track record block)", "exec"), ns)


LANDING = {}


@app.route("/")
def home():
    return render_template("landing.html", **LANDING)


@app.route("/login")
def login_page():
    return "login"


@app.route("/terms")
def terms_page():
    return render_template("terms.html")


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")


@app.route("/boom")
def _boom():
    raise RuntimeError("test crash")


@app.route("/api/boom")
def _api_boom():
    raise RuntimeError("test crash")


app.config["PROPAGATE_EXCEPTIONS"] = False


c = app.test_client()
passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


def get(path):
    r = c.get(path, base_url="https://signals.example")
    return r.status_code, r.get_data(as_text=True)


print("\n[1] robots + sitemap")
for k in ("TRACK_RECORD_PUBLIC", "GA_MEASUREMENT_ID", "SUPPORT_EMAIL", "SITE_URL"):
    os.environ.pop(k, None)
st, body = get("/robots.txt")
check(st == 200 and "Disallow: /api/" in body and "Sitemap: https://signals.example/sitemap.xml" in body, "robots.txt")
st, body = get("/sitemap.xml")
check(st == 200 and "<loc>https://signals.example/tools</loc>" in body and "track-record" not in body, "sitemap hides a private track record")
os.environ["TRACK_RECORD_PUBLIC"] = "on"
os.environ["SITE_URL"] = "https://signalsfm.com/"
check("<loc>https://signalsfm.com/track-record</loc>" in get("/sitemap.xml")[1], "sitemap lists public track record + uses SITE_URL")

print("\n[2] landing")
st, html = get("/")
check(st == 200 and 'href="/track-record"' in html and 'href="/tools"' in html, "landing links to track record + tools")
check("Start Free Trial" not in html and "Cancel anytime" not in html and "Free while we" in html, "no misleading trial copy")
check('property="og:image" content="https://signalsfm.com/static/og-image.png"' in html, "Open Graph image uses the site URL")
check('<link rel="canonical" href="https://signalsfm.com/">' in html, "canonical URL")
os.environ.pop("TRACK_RECORD_PUBLIC")
check('href="/track-record"' not in get("/")[1], "track record link hidden while private")

print("\n[3] analytics + contact are validated")
os.environ["GA_MEASUREMENT_ID"] = 'G-ABC123"><script>alert(1)</script>'
check("googletagmanager" not in get("/")[1], "malformed analytics id ignored")
os.environ["GA_MEASUREMENT_ID"] = "G-ABC123XYZ"
check("gtag/js?id=G-ABC123XYZ" in get("/")[1], "valid analytics id rendered")
check("Google Analytics" in get("/privacy")[1], "privacy policy mentions analytics only when enabled")
os.environ.pop("GA_MEASUREMENT_ID")
check("Google Analytics" not in get("/privacy")[1], "no analytics mention when disabled")
os.environ["SUPPORT_EMAIL"] = "help@signalsfm.com"
check('mailto:help@signalsfm.com' in get("/terms")[1], "support email shown")
os.environ["SUPPORT_EMAIL"] = "<b>x"
check("mailto:" not in get("/terms")[1], "invalid support email ignored")
os.environ.pop("SUPPORT_EMAIL")

print("\n[4] pages")
st, html = get("/tools")
check(st == 200 and "Position size calculator" in html and "Liquidation price calculator" in html and 'id="ladder"' in html, "tools page")
st, html = get("/risk-disclosure")
check(st == 200 and "Leverage and liquidation" in html and "Auto-Trade places real orders" in html, "risk disclosure page")
st, html = get("/privacy")
check(st == 200 and "Supabase" not in html and "encrypted with a server-side key" in html, "privacy policy is accurate")
st, html = get("/terms")
check(st == 200 and "Auto-Trade and exchange accounts" in html and "Demo Trading" in html, "terms cover new features")
check(get("/track-record")[0] == 302, "track record needs login while private")
with c.session_transaction() as s:
    s["user_id"] = 1
    s["email"] = "user@example.com"
r = c.get("/track-record")  # same host as the session cookie
st, html = r.status_code, r.get_data(as_text=True)
check(st == 200 and "Every signal, recorded" in html and "TRACK_RECORD_PUBLIC" not in html,
      "logged-in user sees the page, without the owner's server note")
os.environ["ADMIN_EMAILS"] = "Owner@Example.com, other@example.com"
with c.session_transaction() as s:
    s["email"] = "owner@example.com"
html = c.get("/track-record").get_data(as_text=True)
check("TRACK_RECORD_PUBLIC=on" in html, "the owner (ADMIN_EMAILS) sees the publish hint")
os.environ.pop("ADMIN_EMAILS")
os.environ.pop("SITE_URL", None)

print("\n[landing card]")
with c.session_transaction() as s_:
    s_.clear()
LANDING.update(live={"ok": True, "active": 4, "coins": 30, "recent": [{"coin": "ETH", "side": "LONG", "ago": "2 h ago"},
                                                                      {"coin": "BTC", "side": "SHORT", "ago": "9 h ago"}]},
               stats={"win_rate": 69.8, "signals": 437})
html = c.get("/").get_data(as_text=True)
card = html[html.index('class="dash-mock"'):html.index('id="features"')]
check("ETH/USDT" in card and "Long" in card and "2 h ago" in card and ">4<" in card and "69.8%" in card and "Live" in card,
      "landing card: real active signals, coins and tested win rate")
check("EUR/USD" not in card and "BUY" not in card and "19" not in card and "24" not in card, "landing card: no invented signals or numbers")
check("19-channel" not in html.lower() and "Four independent" not in html and "Day Traders" not in html, "landing copy matches the current engine")
LANDING.update(live={"ok": False}, stats={})
card = c.get("/").get_data(as_text=True)
card = card[card.index('class="dash-mock"'):card.index('id="features"')]
check("Live" not in card and "checked at every 4-hour close" in card, "landing card: no 'Live' badge when there is no data")
LANDING.clear()

print("\n[error pages]")
with c.session_transaction() as s_:
    s_["user_id"] = 1
    s_["email"] = "user@example.com"
r = c.get("/no-such-page")
html = r.get_data(as_text=True)
check(r.status_code == 404 and "Page not found" in html and 'href="/contact"' in html and "an-side" in html,
      "404: designed page with the app menu for a logged-in user, home and contact links")
r = c.get("/api/no-such-thing")
check(r.status_code == 404 and r.is_json and r.get_json()["ok"] is False, "404 on an API path: JSON, not HTML")
with c.session_transaction() as s:
    s.clear()
r = c.get("/no-such-page")
html = r.get_data(as_text=True)
check(r.status_code == 404 and "Page not found" in html and "an-side" not in html and "pnav" in html, "404 for a visitor: public navigation")

r = c.get("/boom")
html = r.get_data(as_text=True)
check(r.status_code == 500 and "Something went wrong" in html and "test crash" not in html, "500: friendly page, no error details shown")
r = c.get("/api/boom")
check(r.status_code == 500 and r.is_json and "went wrong" in r.get_json()["error"], "500 on an API path: JSON error")

print(f"\nALL {passed} CHECKS PASSED")
