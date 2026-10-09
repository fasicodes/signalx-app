"""Tests for pages.py (about, FAQ, pricing, contact, glossary) and the shared navigation, with a SQLite shim
(no MySQL, no email). Run from the project root:  python tests/test_pages.py
"""
import glob
import json
import os
import re
import sqlite3
import sys
import tempfile
import types
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pm = types.ModuleType("pymysql")
pm.cursors = types.ModuleType("pymysql.cursors")
pm.cursors.DictCursor = object
pm.connect = lambda **kw: None
sys.modules.setdefault("pymysql", pm)
sys.modules.setdefault("pymysql.cursors", pm.cursors)

sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))
DB_PATH = os.path.join(tempfile.gettempdir(), "pages_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
DB = {"down": False}


def _translate(sql):
    s = sql.strip()
    if s.upper().startswith("CREATE TABLE"):
        s = s.replace("INT PRIMARY KEY AUTO_INCREMENT", "INTEGER PRIMARY KEY AUTOINCREMENT")
    return s.replace("%s", "?")


class Cur:
    def __init__(self, conn):
        self.c = conn.cursor()
        self.lastrowid = None

    def execute(self, sql, params=()):
        t = _translate(sql)
        self.c.execute(t, tuple(params or ()) if "?" in t else ())
        self.lastrowid = self.c.lastrowid

    def fetchone(self):
        r = self.c.fetchone()
        return dict(r) if r else None

    def fetchall(self):
        return [dict(r) for r in self.c.fetchall()]

    def close(self):
        self.c.close()


class Conn:
    def __init__(self):
        if DB["down"]:
            raise RuntimeError("database is down")
        self.conn = sqlite3.connect(DB_PATH, isolation_level=None)
        self.conn.row_factory = sqlite3.Row

    def cursor(self):
        return Cur(self.conn)

    def close(self):
        self.conn.close()


from flask import Flask, render_template  # noqa: E402
import pages  # noqa: E402
import glossary  # noqa: E402

pages.get_db_connection = Conn
SENT = []
MAIL = {"ok": True}


def fake_send(to, subject, html, reply_to=None):
    SENT.append({"to": to, "subject": subject, "html": html, "reply_to": reply_to})
    return MAIL["ok"]


app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"), static_folder=os.path.join(ROOT, "static"))
app.secret_key = "t"
app.context_processor(lambda: {"track_record_public": False, "ga_measurement_id": "", "support_email": "", "site_url": "",
                               "current_year": 2026})
app.register_blueprint(pages.pages_bp)
pages.init_pages(app, send_email=fake_send)
for path, tpl in (("/terms", "terms.html"), ("/privacy", "privacy.html"), ("/risk-disclosure", "risk-disclosure.html")):
    app.add_url_rule(path, tpl, (lambda tpl=tpl: render_template(tpl)))
app.add_url_rule("/dash", "dash", lambda: render_template("dashboard.html", user_email="me@x.com", user_avatar=None, active="home"))
app.add_url_rule("/sig", "sig", lambda: render_template("signals.html", active="signals"))
app.add_url_rule("/demo", "demo", lambda: render_template("demo-trading.html"))
app.add_url_rule("/bot", "bot", lambda: render_template("auto-trading.html", user_email="me@x.com"))
app.add_url_rule("/tr", "tr", lambda: render_template("track-record.html", public=True, logged_in=False))
app.add_url_rule("/tools", "tools", lambda: render_template("tools.html"))
app.add_url_rule("/land", "land", lambda: render_template("landing.html"))

c = app.test_client()
passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


def get(path):
    r = c.get(path)
    return r.status_code, r.get_data(as_text=True)


def login(on=True):
    with c.session_transaction() as s:
        if on:
            s["user_id"] = 7
            s["email"] = "me@x.com"
        else:
            s.clear()


INFO = ["/about", "/faq", "/pricing", "/contact", "/terms", "/privacy", "/risk-disclosure"]

print("\n[1] info pages for visitors")
for k in ("SUPPORT_EMAIL", "MAIL_USERNAME"):
    os.environ.pop(k, None)
for path in INFO:
    st, html = get(path)
    check(st == 200 and 'class="pnav"' in html and 'id="an-side"' not in html and "/static/shell.js" in html,
          f"{path}: public navigation + shell.js for the ? tips")
    check(all(f'href="{p}"' in html for p in ("/about", "/faq", "/pricing", "/contact")), f"{path}: footer links to the new pages")
st, html = get("/about")
check('id="pmenu"' in html and 'class="pmenu-panel"' in html, "phone menu in the public navigation")
check("Fasiullah Ayaz" in html and "437" in html and "+0.052R" in html, "about: founder + tested figures from the model")
st, html = get("/pricing")
check("$0" in html and "Free while we" in html and "No card" in html, "pricing: free during the beta")
st, html = get("/contact")
check('mailto:signalfm01@gmail.com' in html and "tiktok.com/@signalfm1" in html and 'id="c-company"' in html,
      "contact: email, social links and the hidden spam field")
os.environ["SUPPORT_EMAIL"] = "help@signalsfm.com"
check('mailto:help@signalsfm.com' in get("/contact")[1], "contact shows SUPPORT_EMAIL when it is set")
os.environ.pop("SUPPORT_EMAIL")

print("\n[2] info pages inside the app")
login()
for path, key in (("/about", "about"), ("/faq", "faq"), ("/pricing", "pricing"), ("/contact", "contact"),
                  ("/terms", "terms"), ("/privacy", "privacy"), ("/risk-disclosure", "riskd")):
    st, html = get(path)
    check(st == 200 and 'id="an-side"' in html and 'class="info an-shell in-app"' in html and 'class="pnav"' not in html,
          f"{path}: app menu for logged-in users")
    check(re.search(r'href="%s" class="an-item" aria-current="page"' % re.escape(path), html) is not None, f"{path}: menu marks the page")
check('value="me@x.com"' in get("/contact")[1], "contact form fills in the account email")
login(False)

print("\n[3] FAQ + glossary")
st, html = get("/faq")
check(html.count('<details class="qa"') == sum(len(g[2]) for g in pages.FAQ), "every question is on the page")
check(all(f'id="g-{k}"' in html for k in glossary.GLOSSARY), "every glossary term has its own anchor (/faq#g-...)")
check('id="faq-search"' in html and 'id="glossary"' in html, "search box + glossary section")
r = c.get("/glossary.json")
d = r.get_json()
check(r.status_code == 200 and set(d["terms"]) == set(glossary.GLOSSARY) and "max-age" in r.headers.get("Cache-Control", ""),
      "glossary.json serves every term, cacheable")
check(all(v["term"] and len(v["text"]) > 20 for v in d["terms"].values()), "every term has a real explanation")

print("\n[4] every ? button points at a real glossary term")
used = set()
for f in glob.glob(os.path.join(ROOT, "templates", "*.html")) + glob.glob(os.path.join(ROOT, "static", "*.js")):
    src = open(f, encoding="utf-8").read()
    used |= set(re.findall(r'data-tip="([a-z0-9_]+)"', src))
    used |= set(re.findall(r"""\btip\(\s*["']([a-z0-9_]+)["']""", src))
    used |= set(re.findall(r"""fig\([^)]*,\s*"([a-z0-9_]+)"\)""", src))
    for m in re.finditer(r"const KEYS = \{([^}]*)\}", src):
        used |= set(re.findall(r':\s*"([a-z0-9_]+)"', m.group(1)))
used.discard("key")
missing = sorted(k for k in used if k not in glossary.GLOSSARY)
check(len(used) >= 25 and not missing, f"{len(used)} keys used, none missing {missing}")

print("\n[5] contact form")
def post(body, raw=None):
    r = c.post("/api/contact", data=raw if raw is not None else json.dumps(body), content_type="application/json")
    return r.status_code, r.get_json()
good = {"name": "Ali Khan", "email": "ali@example.com", "topic": "Signals", "message": "How often do signals come?"}
check(post(None, raw="nope")[0] == 400, "bad JSON rejected")
check(post({**good, "name": "A"})[0] == 400, "name required")
check(post({**good, "email": "ali@"})[0] == 400, "valid email required")
check(post({**good, "message": "hi"})[0] == 400, "message too short")
check(post({**good, "message": "x" * 3200})[0] == 400, "message too long")
n0 = len(SENT)
st, d = post({**good, "company": "bot inc"})
cur = Conn().cursor(); cur.execute("SELECT COUNT(*) AS n FROM contact_messages"); stored0 = cur.fetchone()["n"]
check(st == 200 and d["ok"] and len(SENT) == n0 and stored0 == 0, "spam bots (hidden field) get a fake OK, nothing stored or sent")
os.environ["MAIL_USERNAME"] = "signalfm01@gmail.com"
st, d = post({**good, "name": "Ali\r\nBcc: evil@x.com", "topic": "Hacker topic"})
cur = Conn().cursor(); cur.execute("SELECT * FROM contact_messages ORDER BY id DESC"); row = cur.fetchone()
check(st == 200 and d["ok"] and row["email"] == "ali@example.com" and row["emailed"] == 1, "message stored and marked emailed")
m = SENT[-1]
check(m["to"] == "signalfm01@gmail.com" and m["reply_to"] == "ali@example.com", "emailed to the support address, Reply goes to the sender")
check("\n" not in m["subject"] and "\r" not in m["subject"] and row["topic"] == "General question",
      "no line breaks in the subject (header injection), unknown topic -> General question")
check("&lt;" not in row["message"] and "How often" in m["html"], "message text kept; email body is escaped HTML")
st, d = post({**good, "message": "<script>alert(1)</script> hello there"})
check("<script>" not in SENT[-1]["html"] and "&lt;script&gt;" in SENT[-1]["html"], "HTML in a message is escaped in the email")
os.environ["SUPPORT_EMAIL"] = "help@signalsfm.com"
post(good)
check(SENT[-1]["to"] == "help@signalsfm.com", "SUPPORT_EMAIL wins over MAIL_USERNAME")
os.environ.pop("SUPPORT_EMAIL")
MAIL["ok"] = False
st, d = post(good)
check(st == 200 and d["ok"], "email down but message stored -> still OK (nothing is lost)")
DB["down"] = True
st, d = post(good)
check(st == 503 and "signalfm01@gmail.com" in d["error"], "email and database both down -> clear error with the address")
DB["down"] = False
MAIL["ok"] = True
os.environ.pop("MAIL_USERNAME")

print("\n[6] app pages: shell, ? tips, guide, Telegram block")
login()
st, html = get("/dash")
check(st == 200 and 'class="app an-shell"' in html and 'id="an-guide"' in html and "data-auto" in html, "dashboard: shell + welcome guide (auto on home)")
check('data-tip="unseen_data"' in html and "/static/shell.css" in html and "/static/shell.js" in html, "dashboard: ? tip + shell files")
check('href="/about" class="an-item"' in html and 'href="/faq" class="an-item"' in html and "/advanced#about" not in html,
      "menu links go to the new About/FAQ pages")
check(html.count("data-guide-open") >= 2, "guide can be reopened from the menu and the account popover")
st, html = get("/sig")
check(st == 200 and 'id="tg"' in html and "/api/telegram/link" in html and 'data-tip="leaning"' in html and "data-auto" not in html,
      "signals: Telegram connect block + tips; guide does not pop up there")
st, html = get("/demo")
check(st == 200 and 'class="an-shell"' in html and 'href="/demo-trading" class="an-item" aria-current="page"' in html
      and "back-link" not in html.split("</style>", 1)[1] and "/static/legacy.css" in html, "demo trading: inside the app menu")
st, html = get("/bot")
check(st == 200 and 'href="/auto-trading" class="an-item" aria-current="page"' in html and "Auto-trade bot</h1>" in html,
      "auto-trade bot: inside the app menu")
st, html = get("/tr")
check(st == 200 and 'id="an-side"' in html and 'data-tip="r"' in html, "track record: app menu when logged in")
st, html = get("/tools")
check(st == 200 and 'href="/tools" class="an-item" aria-current="page"' in html, "trading tools: app menu when logged in")
login(False)
st, html = get("/tr")
check(st == 200 and 'class="pnav"' in html and 'id="an-side"' not in html, "track record: public navigation for visitors")
st, html = get("/land")
check(st == 200 and 'href="/pricing"' in html and 'href="/faq"' in html and 'id="lmenu"' in html and "Free while we" in html,
      "landing: links to the new pages + phone menu")

print(f"\nALL {passed} CHECKS PASSED")
