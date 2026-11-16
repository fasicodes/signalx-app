"""The admin panel (admin_panel.py) and what it records (admin_tracking.py): who may open it, CSRF on changes, and that
every number comes from the database. No network, no MySQL (SQLite stand-in), exchange calls replaced by fakes.
Run from the project root:  python tests/test_admin_panel.py
"""
import csv
import io
import json
import os
import sys
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(1, HERE)
os.environ.update({"SECRET_KEY": "t" * 32, "AUTOTRADE_ENGINE": "off", "SIGNALX_ENGINE": "off", "SIGNAL_HUB": "off",
                   "SIGNAL_BOARD": "off", "TRACK_RECORD_ENGINE": "off", "STRATEGIES_ENGINE": "off", "NEWS_JOB": "off",
                   "PAPER_ENGINE": "off", "ADMIN_EMAILS": "Owner@Example.com",
                   "AUTOTRADE_ENCRYPTION_KEY": "6HQGyRqbL3x4j5Z4rX3j2pQ7o1m8Zl0b9m1xg7g4n0U="})
for k in ("ADMIN_REQUIRE_GOOGLE", "VISITOR_TRACKING", "ADMIN_CURRENCY", "ADMIN_ONLINE_WINDOW_SEC"):
    os.environ.pop(k, None)

from dbshim import setup  # noqa: E402

Conn = setup("admin_panel")
import db  # noqa: E402

db.get_db_connection = Conn

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402
import main  # noqa: E402
import admin_tracking as T  # noqa: E402
import admin_panel as A  # noqa: E402

FAILS = []


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def sql(q, args=()):
    c = Conn()
    cur = c.cursor()
    cur.execute(q, args)
    try:
        rows = cur.fetchall()
    except Exception:
        rows = []
    last = getattr(cur, "lastrowid", None)
    c.close()
    return rows, last


def add_user(email, verified=1, provider="password", password="Passw0rd!x"):
    _, uid = sql("INSERT INTO users (email, password_hash, auth_provider, email_verified) VALUES (%s,%s,%s,%s)",
                 (email, generate_password_hash(password), provider, verified))
    return uid


OWNER = add_user("owner@example.com")
MEMBER = add_user("member@example.com")
OTHER = add_user("other@example.com")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def client(uid=None, email=None, method=None):
    c = main.app.test_client()
    if uid:
        with c.session_transaction() as s:
            s["user_id"], s["email"], s["sv"] = uid, email, 0
            if method:
                s["login_method"] = method
    return c


def csrf_of(c):
    c.get("/admin", headers=UA)
    with c.session_transaction() as s:
        return s.get("admin_csrf")


guest = client()
member = client(MEMBER, "member@example.com")
admin = client(OWNER, "owner@example.com")
TOKEN = csrf_of(admin)

PAGES = ["/admin", "/admin/users", f"/admin/users/{MEMBER}", "/admin/subscriptions", "/admin/analytics", "/admin/trades",
         "/admin/activity", "/admin/settings"]
APIS = ["/api/admin/overview", "/api/admin/analytics?range=7", "/api/admin/users", f"/api/admin/users/{MEMBER}",
        "/api/admin/users/lookup?email=member@example.com", "/api/admin/subscriptions", "/api/admin/online", "/api/admin/activity",
        "/api/admin/trades", "/api/admin/trades?source=bot", "/api/admin/trades?source=demo", "/api/admin/plans",
        "/api/admin/settings", "/api/admin/export/users.csv", "/api/admin/export/subscriptions.csv", "/api/admin/export/activity.csv"]
POSTS = [("/api/admin/plans", {"name": "X", "price": 1}), ("/api/admin/subscriptions", {"email": "member@example.com", "plan_id": 1}),
         (f"/api/admin/users/{MEMBER}/status", {"status": "blocked"}), (f"/api/admin/users/{MEMBER}/logout", {}),
         (f"/api/admin/users/{MEMBER}/name", {"full_name": "x"}), ("/api/admin/subscriptions/1/status", {"status": "active"}),
         ("/api/admin/subscriptions/1/extend", {"days": 3}), ("/api/admin/subscriptions/1/payments", {"amount": 1}),
         ("/api/admin/payments/1/status", {"status": "verified"}), ("/api/admin/plans/1", {"name": "Y", "price": 1})]

# ---------------------------------------------------------------------------
print("\n[1] only the admin: pages and API, checked on the server")
for p in PAGES:
    r = guest.get(p)
    check(r.status_code == 302 and "/login" in r.headers.get("Location", ""), f"visitor: {p} -> login")
    check(member.get(p).status_code == 404, f"member: {p} -> 404 (the panel is not revealed)")
    r = admin.get(p, headers=UA)
    check(r.status_code == 200 and 'name="csrf-token"' in r.get_data(as_text=True), f"admin: {p} opens")
for p in APIS:
    check(guest.get(p).status_code == 404 and member.get(p).status_code == 404, f"visitor and member: {p.split('?')[0]} -> 404")
    check(admin.get(p).status_code == 200, f"admin: {p.split('?')[0]} -> 200")
for p, body in POSTS:
    check(member.post(p, json=body, headers={"X-CSRF-Token": TOKEN}).status_code == 404, f"member cannot change: {p}")
    check(admin.post(p, json=body).status_code == 403, f"admin without the CSRF token: {p} -> 403")
    check(admin.post(p, json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403, f"admin with a wrong token: {p} -> 403")
check(sql("SELECT account_status FROM users WHERE id=%s", (MEMBER,))[0][0]["account_status"] == "active",
      "nothing was changed by the refused requests")
html = admin.get("/admin", headers=UA).get_data(as_text=True)
check("t" * 32 not in html and "6HQGyRqb" not in html, "no secret appears in an admin page")
# the email must belong to a real, confirmed, not suspended account with the same address
unverified = add_user("boss@example.com", verified=0)
os.environ["ADMIN_EMAILS"] = "owner@example.com, boss@example.com"
check(client(unverified, "boss@example.com").get("/api/admin/overview").status_code == 404,
      "an ADMIN_EMAILS address whose account never confirmed its email is refused")
check(client(MEMBER, "owner@example.com").get("/api/admin/overview").status_code == 404,
      "a session whose email does not match its account is refused")
os.environ["ADMIN_REQUIRE_GOOGLE"] = "on"
A._cache.clear()
check(admin.get("/api/admin/overview").status_code == 404, "ADMIN_REQUIRE_GOOGLE=on: a password session is refused")
check(client(OWNER, "owner@example.com", "google").get("/api/admin/overview").status_code == 200,
      "ADMIN_REQUIRE_GOOGLE=on: a Google session is let in")
os.environ.pop("ADMIN_REQUIRE_GOOGLE")
os.environ["ADMIN_EMAILS"] = "Owner@Example.com"
A._cache.clear()
r = client(OWNER, "owner@example.com").get("/robots.txt")
check("Disallow: /admin" in r.get_data(as_text=True), "robots.txt keeps search engines out of /admin")

# ---------------------------------------------------------------------------
print("\n[2] what the site records: visitors, presence, logins, sign-ups, analyses")
v = client()
r = v.get("/", headers=UA)
cookie = (r.headers.get("Set-Cookie") or "")
check("sfm_vid=" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie, "a page view sets the anonymous visitor cookie")
n1 = sql("SELECT COUNT(*) AS n FROM visitor_days")[0][0]["n"]
v.get("/", headers=UA)
v.get("/pricing", headers=UA)
check(n1 == 1 and sql("SELECT COUNT(*) AS n FROM visitor_days")[0][0]["n"] == 1, "the same visitor is counted once a day")
client().get("/", headers={"User-Agent": "Googlebot/2.1 (+http://www.google.com/bot.html)"})
client().get("/", headers=dict(UA, DNT="1"))
client().get("/api/news", headers=UA)
check(sql("SELECT COUNT(*) AS n FROM visitor_days")[0][0]["n"] == 1, "bots, Do-Not-Track and API calls are not counted")
os.environ["VISITOR_TRACKING"] = "off"
r = client().get("/", headers=UA)
check("sfm_vid" not in (r.headers.get("Set-Cookie") or ""), "VISITOR_TRACKING=off: no cookie, no count")
os.environ.pop("VISITOR_TRACKING")

r = member.post("/api/presence", json={"visible": True, "path": "/signals"})
check(r.status_code == 200 and r.get_json()["ok"], "heartbeat from a member page")
check(guest.post("/api/presence", json={}).status_code == 401, "no heartbeat without a login")
d = admin.get("/api/admin/online").get_json()
check(any(m["email"] == "member@example.com" and m["status"] == "online" and m["page"] == "/signals" for m in d["online"]),
      "the member is online, with the page")
T._mem["ping"].clear()
member.post("/api/presence", json={"visible": False, "path": "/signals"})
d = admin.get("/api/admin/online").get_json()
check(any(m["email"] == "member@example.com" and m["status"] == "idle" for m in d["online"]), "a background tab shows as idle")
sql("UPDATE user_presence SET last_seen=%s WHERE user_id=%s", (datetime.utcnow() - timedelta(minutes=5), MEMBER))
d = admin.get("/api/admin/online").get_json()
check(not any(m["email"] == "member@example.com" for m in d["online"]) and any(m["email"] == "member@example.com" for m in d["recent"]),
      "no heartbeat for 5 minutes: not online any more (logged in is not the same as online)")
check(admin.get("/api/admin/overview").get_json()["online"] == sum(1 for m in d["online"]), "the dashboard count matches the list")

lg = client()
r = lg.post("/api/login", json={"email": "other@example.com", "password": "Passw0rd!x"})
ev = sql("SELECT kind, email, detail FROM activity_log WHERE user_id=%s ORDER BY id DESC", (OTHER,))[0]
check(r.status_code == 200 and ev and ev[0]["kind"] == "login" and "password" in ev[0]["detail"], "a login is recorded with its method")
check(sql("SELECT logins FROM user_daily WHERE user_id=%s", (OTHER,))[0][0]["logins"] == 1, "logins per day")
with lg.session_transaction() as s:
    check(s.get("login_method") == "password", "the session remembers how it logged in")
lg.post("/api/logout")
check(sql("SELECT kind FROM activity_log WHERE user_id=%s ORDER BY id DESC LIMIT 1", (OTHER,))[0][0]["kind"] == "logout"
      and sql("SELECT ended_at FROM user_presence WHERE user_id=%s", (OTHER,))[0][0]["ended_at"] is not None,
      "a logout is recorded and ends the presence session")
rg = client().post("/api/register", json={"email": "new.member@gmail.com", "password": "abc12345xyz"})
check(rg.status_code == 201 and sql("SELECT COUNT(*) AS n FROM activity_log WHERE kind='register' AND email='new.member@gmail.com'")[0][0]["n"] == 1,
      "a sign-up is recorded")


def fake_candles(symbol="BTC/USDT", timeframe="1h", limit=200, since=None):
    rng = np.random.default_rng(1)
    n = 150
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=n, freq="h"), "open": close, "high": close * 1.01,
                         "low": close * 0.99, "close": close, "volume": rng.uniform(1, 2, n)})


main.get_candles = fake_candles
main.generate_signal = lambda df, symbol=None, include_orderbook=True: {"final_verdict": "WAIT", "confidence_pct": 50}
member.get("/signal?coin=BTC/USDT&timeframe=1h&run=1")
member.get("/signal?coin=ETH/USDT&timeframe=1h&run=1")
member.get("/signal?coin=ETH/USDT&timeframe=1h")          # the Pro terminal's 30-second refresh: not an analysis
check(sql("SELECT SUM(analyses) AS a FROM user_daily WHERE user_id=%s", (MEMBER,))[0][0]["a"] == 2,
      "analyses: only the Pro terminal's runs (run=1) are counted")
check(A.user_detail(MEMBER)["analyses"] == 2, "the member profile shows the number of analyses")

# ---------------------------------------------------------------------------
print("\n[3] plans, subscriptions, payments and revenue (only verified payments count)")
H = {"X-CSRF-Token": TOKEN}
r = admin.post("/api/admin/plans", json={"name": "Monthly", "price": "19.50", "currency": "usd", "duration_days": 30}, headers=H)
plan = r.get_json()["plan"]
check(r.status_code == 200 and plan["currency"] == "USD" and plan["price"] == 19.5, "a plan is created")
check(admin.post("/api/admin/plans", json={"name": "", "price": "x"}, headers=H).status_code == 400, "a bad plan is refused with a message")
ov0 = admin.get("/api/admin/overview").get_json()
check(ov0["revenue_main"] == 0 and ov0["subscriptions_active"] == 0, "no subscriptions yet: no revenue (nothing invented)")
r = admin.post("/api/admin/subscriptions", headers=H, json={"email": "member@example.com", "plan_id": plan["id"], "status": "active",
                                                            "payment": {"amount": "19.50", "method": "JazzCash", "reference": "TX1", "status": "verified"}})
sub = r.get_json()["subscription"]
check(r.status_code == 200 and sub["status"] == "active" and sub["days"] == 30 and sub["expires_at"] and len(sub["payments"]) == 1
      and sub["payments"][0]["verified_by"] == "owner@example.com", "an active subscription with a verified payment")
r = admin.post("/api/admin/subscriptions", headers=H, json={"email": "other@example.com", "plan_id": plan["id"], "status": "pending",
                                                            "payment": {"amount": "19.50", "method": "Bank transfer", "status": "pending"}})
pend = r.get_json()["subscription"]
check(pend["status"] == "pending" and pend["expires_at"] is None, "a pending subscription waits for payment (no dates yet)")
check(admin.post("/api/admin/subscriptions", headers=H, json={"email": "nobody@x.com", "plan_id": plan["id"]}).status_code == 400,
      "an unknown member is refused")
ov = admin.get("/api/admin/overview").get_json()
check(ov["revenue_main"] == 19.5 and ov["subscriptions_active"] == 1 and ov["subscriptions_pending"] == 1,
      "revenue counts only the verified payment; pending money is not revenue")
check(ov["subscriptions_never_used"] == 1, "a bought subscription that was not used yet counts as never used")
T._mem["usage"].clear()
T._mem["sub"].clear()
member.get("/signals")
s1 = T.get_subscription(sub["id"])
check(s1["first_used_at"] is not None and s1["use_count"] == 1, "a member page while subscribed marks the subscription as used")
member.get("/signals")
check(T.get_subscription(sub["id"])["use_count"] == 1, "uses are recorded at most every 10 minutes")
ud = A.user_detail(MEMBER)
check(ud["ever_used_subscription"] is True and ud["subscription_status"] == "active" and ud["paid_verified"] == 19.5,
      "the member profile: subscription, ever used, verified payments")
check(admin.get("/api/admin/overview").get_json()["subscriptions_never_used"] == 0, "after use: no longer 'never used'")
pid = admin.get(f"/api/admin/subscriptions/{pend['id']}").get_json()["subscription"]["payments"][0]["id"]
admin.post(f"/api/admin/payments/{pid}/status", json={"status": "verified"}, headers=H)
admin.post(f"/api/admin/subscriptions/{pend['id']}/status", json={"status": "active"}, headers=H)
sp = T.get_subscription(pend["id"])
check(sp["status"] == "active" and sp["starts_at"] is not None and sp["expires_at"] is not None, "activating a pending one starts its days")
check(admin.get("/api/admin/overview").get_json()["revenue_main"] == 39.0, "a verified payment adds to the revenue")
admin.post(f"/api/admin/payments/{pid}/status", json={"status": "refunded"}, headers=H)
check(admin.get("/api/admin/overview").get_json()["revenue_main"] == 19.5, "a refunded payment leaves the revenue")
admin.post(f"/api/admin/subscriptions/{pend['id']}/status", json={"status": "cancelled", "reason": "asked"}, headers=H)
check(T.get_subscription(pend["id"])["status"] == "cancelled", "cancel")
# expiry: the log carries the real expiry time
past = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)
sql("UPDATE subscriptions SET expires_at=%s WHERE id=%s", (past, sub["id"]))
ov = admin.get("/api/admin/overview").get_json()
ev = sql("SELECT at FROM activity_log WHERE kind='subscription_expired' AND user_id=%s", (MEMBER,))[0]
check(ov["subscriptions_expired"] == 1 and ov["subscriptions_active"] == 0 and ev and str(ev[0]["at"])[:16] == past.strftime("%Y-%m-%d %H:%M"),
      "an ended subscription becomes expired, logged at its expiry time")
admin.post(f"/api/admin/subscriptions/{sub['id']}/extend", json={"days": 10}, headers=H)
se = T.get_subscription(sub["id"])
check(se["status"] == "active" and T._dt(se["expires_at"]) > datetime.utcnow() + timedelta(days=9), "extend restarts an expired one")
lst = admin.get("/api/admin/subscriptions?status=cancelled").get_json()
check(lst["total"] == 1 and lst["subscriptions"][0]["id"] == pend["id"] and lst["summary"]["cancelled"] == 1, "filter: cancelled")
check(admin.get("/api/admin/subscriptions?q=member@").get_json()["total"] == 1, "search by email")
check(admin.post("/api/admin/subscriptions/999/status", json={"status": "active"}, headers=H).status_code == 400, "unknown subscription")

# ---------------------------------------------------------------------------
print("\n[4] members: search, profile, suspend, sign out everywhere, name")
d = admin.get("/api/admin/users?q=member").get_json()
check(d["total"] == 2 and {u["email"] for u in d["users"]} == {"member@example.com", "new.member@gmail.com"}, "search by part of the email")
d = admin.get("/api/admin/users/lookup?email=Member@Example.com").get_json()
u = d["user"]
need = ["full_name", "email", "created_at", "account_status", "subscription_status", "current_subscription", "ever_used_subscription",
        "analyses", "last_active", "trades", "payments", "subscriptions"]
check(all(k in u for k in need), "the lookup gives every field the admin asked for")
r = admin.get("/api/admin/users/lookup?email=nobody@example.com")
check(r.status_code == 404 and r.get_json()["ok"] is False and "No account" in r.get_json()["error"], "lookup of an unknown email")
victim = client(MEMBER, "member@example.com")
check(victim.get("/api/me").status_code == 200, "the member is logged in")
r = admin.post(f"/api/admin/users/{MEMBER}/status", json={"status": "blocked"}, headers=H)
check(r.status_code == 200 and r.get_json()["user"]["account_status"] == "blocked", "suspend a member")
check(victim.get("/api/me").status_code == 401, "a suspended member's session ends at once")
r = client().post("/api/login", json={"email": "member@example.com", "password": "Passw0rd!x"})
check(r.status_code == 403 and "suspended" in r.get_json()["error"], "a suspended member cannot log in")
check(admin.post(f"/api/admin/users/{OWNER}/status", json={"status": "blocked"}, headers=H).status_code == 400,
      "an admin account cannot be suspended")
admin.post(f"/api/admin/users/{MEMBER}/status", json={"status": "active"}, headers=H)
check(client().post("/api/login", json={"email": "member@example.com", "password": "Passw0rd!x"}).status_code == 200,
      "re-activated: the member can log in again")
victim = client(MEMBER, "member@example.com")
admin.post(f"/api/admin/users/{MEMBER}/logout", json={}, headers=H)
check(victim.get("/api/me").status_code == 401, "sign out everywhere ends the member's sessions")
admin.post(f"/api/admin/users/{MEMBER}/name", json={"full_name": "  Mem Ber  "}, headers=H)
check(A.user_detail(MEMBER)["full_name"] == "Mem Ber", "the admin sets a name")

# ---------------------------------------------------------------------------
print("\n[5] analytics: every range, real counts")
for rng_, n in (("7", 7), ("30", 30), ("90", 90), ("365", 365)):
    d = admin.get(f"/api/admin/analytics?range={rng_}").get_json()
    check(len(d["days"]) == n and all(len(d[k]) == n for k in ("visitors", "registrations", "subscriptions", "revenue",
                                                                  "active_users", "logins", "analyses")), f"{n} days of every series")
d = admin.get("/api/admin/analytics?range=7").get_json()
total_users = sql("SELECT COUNT(*) AS n FROM users")[0][0]["n"]
visitors_today = sql("SELECT COUNT(DISTINCT vid) AS n FROM visitor_days")[0][0]["n"]
print("   totals:", d["totals"], "visitors today:", d["visitors"][-1], "db:", visitors_today, "users:", total_users)
check(d["totals"]["registrations"] == total_users and d["visitors"][-1] == visitors_today >= 1 and d["totals"]["revenue"] == 19.5
      and d["totals"]["analyses"] == 2 and d["weekly_visitors"] and d["monthly_visitors"],
      "the totals match the database (registrations, visitors, verified revenue, analyses)")

# ---------------------------------------------------------------------------
print("\n[6] activity log and admin actions")
d = admin.get("/api/admin/activity?group=admin").get_json()
kinds = {e["kind"] for e in d["events"]}
check({"user_blocked", "user_unblocked", "user_logged_out", "user_renamed", "plan_created", "subscription_new"} <= kinds
      and all(e["actor"] == "owner@example.com" for e in d["events"]), "every admin action is logged with the admin's email")
d = admin.get("/api/admin/activity?group=registrations").get_json()
check(d["total"] >= 1 and all(e["kind"] == "register" for e in d["events"]), "filter: registrations")
d = admin.get("/api/admin/activity?group=subscriptions&q=member@example.com").get_json()
check(d["total"] >= 3 and all(e["kind"].startswith("subscription") for e in d["events"]), "filter: subscriptions of one member")
d = admin.get("/api/admin/activity?group=payments").get_json()
check({"payment_verified", "payment_refunded"} <= {e["kind"] for e in d["events"]}, "payments are logged")

# ---------------------------------------------------------------------------
print("\n[7] trade history: read from the trading tables, nothing changed")
snap = json.dumps({"final_verdict": "LONG", "confidence_pct": 71.5, "timeframe": "1h", "big": "x" * 1000})
sql("""INSERT INTO active_trades (user_id, asset, direction, entry_price, position_size, stop_loss, take_profit, signal_snapshot, status)
       VALUES (%s,'BTC/USDT','LONG',62000,100,61000,63000,%s,'ACTIVE')""", (MEMBER, snap))
before = sql("SELECT * FROM active_trades")[0]
d = admin.get("/api/admin/trades?q=member@").get_json()
t = d["trades"][0]
check(d["total"] == 1 and t["email"] == "member@example.com" and t["symbol"] == "BTC/USDT" and t["signal"] == "LONG · 71.5% · 1h"
      and t["status"] == "ACTIVE" and d["counts"]["tracked"]["open"] == 1, "tracked trades: member, coin, generated signal, status")
check(admin.get("/api/admin/trades?status=closed").get_json()["total"] == 0, "filter: closed")
check(sql("SELECT * FROM active_trades")[0] == before, "the trade itself is untouched")
d = admin.get("/api/admin/trades?source=bot").get_json()
check(d["ok"] and d["trades"] == [], "bot trades: an empty list when there are none")

# ---------------------------------------------------------------------------
print("\n[8] exports")
sql("UPDATE users SET full_name=%s WHERE id=%s", ("=HYPERLINK(\"http://x\")", OTHER))
r = admin.get("/api/admin/export/users.csv")
rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("﻿"))))
check(r.status_code == 200 and r.mimetype == "text/csv" and "attachment" in r.headers["Content-Disposition"]
      and rows[0][:3] == ["id", "email", "full_name"] and len(rows) == total_users + 1, "users CSV: one row per member")
check(any(x[2].startswith("'=HYPERLINK") for x in rows[1:]), "a cell that starts with = cannot run as a spreadsheet formula")
r = admin.get("/api/admin/export/subscriptions.csv")
rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("﻿"))))
check(rows[0][0] == "id" and len(rows) == 3 and "verified_paid" in rows[0], "subscriptions CSV")
check(admin.get("/api/admin/export/secrets.csv").status_code == 404, "unknown export")
check(sql("SELECT COUNT(*) AS n FROM activity_log WHERE kind LIKE 'export_%%'")[0][0]["n"] >= 2, "exports are logged")

# ---------------------------------------------------------------------------
print("\n[9] the normal site still works")
check(client().get("/", headers=UA).status_code == 200 and client().get("/login").status_code == 200, "landing and login")
m2 = client(MEMBER, "member@example.com")
with m2.session_transaction() as s:      # "sign out everywhere" above raised the account's session version
    s["sv"] = sql("SELECT session_version FROM users WHERE id=%s", (MEMBER,))[0][0]["session_version"]
check(m2.get("/").status_code == 200 and m2.get("/signals").status_code == 200, "member pages")
page = client(OWNER, "owner@example.com").get("/signals").get_data(as_text=True)
check('href="/admin">Admin panel</a>' in page and "presence.js" in page, "the admin sees the Admin panel link; pages send the heartbeat")
check('href="/admin">' not in m2.get("/signals").get_data(as_text=True), "members do not see the link")
check(client(OWNER, "owner@example.com").get("/admin/news").status_code == 200, "the older site admin page still opens")

print()
print(f"{len(FAILS)} failed" if FAILS else "ALL PASSED")
sys.exit(1 if FAILS else 0)
