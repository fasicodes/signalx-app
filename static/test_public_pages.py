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
      "get_candles": lambda **k: None, "signal_core": lambda df: None, "AVAILABLE_COINS": ["BTC/USDT"]}
exec(compile(block, "main.py (track record block)", "exec"), ns)


@app.route("/")
def home():
    return render_template("landing.html")


@app.route("/login")
def login_page():
    return "login"


@app.route("/terms")
def terms_page():
    return render_template("terms.html")


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")


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
r = c.get("/track-record")  # same host as the session cookie
st, html = r.status_code, r.get_data(as_text=True)
check(st == 200 and "TRACK_RECORD_PUBLIC=on" in html and "Every signal, recorded" in html, "logged-in user sees it with the publish hint")
os.environ.pop("SITE_URL", None)

print(f"\nALL {passed} CHECKS PASSED")
