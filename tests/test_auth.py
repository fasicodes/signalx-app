"""Tests for auth.py and oauth.py account safety, with a SQLite shim and a fake mailer (no MySQL, no network).
Run from the project root:  python tests/test_auth.py
"""
import os
import re
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta
import warnings
warnings.simplefilter("ignore", DeprecationWarning)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("FAIL: " + label)
    passed += 1
    print("  ok -", label)


# ---- fake pymysql + SQLite database
pm = types.ModuleType("pymysql")
pm.cursors = types.ModuleType("pymysql.cursors")
pm.cursors.DictCursor = object
pm.connect = lambda **kw: None
sys.modules.setdefault("pymysql", pm)
sys.modules.setdefault("pymysql.cursors", pm.cursors)

sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))
sqlite3.register_converter("DATETIME", lambda b: datetime.strptime(b.decode(), "%Y-%m-%d %H:%M:%S"))
DB_PATH = os.path.join(tempfile.gettempdir(), "auth_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)


class Cur:
    def __init__(self, conn):
        self.c = conn.cursor()
        self.lastrowid = None

    def execute(self, sql, params=()):
        t = sql.replace("%s", "?")
        self.c.execute(t, tuple(params or ()))
        self.lastrowid = self.c.lastrowid

    def fetchone(self):
        r = self.c.fetchone()
        return dict(r) if r else None

    def fetchall(self):
        return [dict(r) for r in self.c.fetchall()]

    def close(self):
        self.c.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class Conn:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, isolation_level=None, detect_types=sqlite3.PARSE_DECLTYPES)
        self.conn.row_factory = sqlite3.Row

    def cursor(self):
        return Cur(self.conn)

    def close(self):
        self.conn.close()


_c = Conn()
_c.conn.execute("""CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, password_hash TEXT NULL, auth_provider TEXT,
    email_verified INTEGER DEFAULT 0, verify_token TEXT NULL, verify_token_expires DATETIME NULL,
    reset_token TEXT NULL, reset_token_expires DATETIME NULL, avatar_url TEXT NULL)""")
_c.close()

db = types.ModuleType("db")
db.get_db_connection = lambda: Conn()
sys.modules["db"] = db

MAIL = []
mailer = types.ModuleType("mailer")
mailer.send_email = lambda to, subject, body, reply_to=None: MAIL.append({"to": to, "subject": subject, "body": body}) or True
sys.modules["mailer"] = mailer

# ---- fake authlib (only what oauth.py touches)
class FakeOAuth:
    def __init__(self):
        self._clients = {}

    def init_app(self, app):
        pass

    def register(self, name, **kw):
        self._clients[name] = kw


al = types.ModuleType("authlib")
al_int = types.ModuleType("authlib.integrations")
al_fc = types.ModuleType("authlib.integrations.flask_client")
al_fc.OAuth = FakeOAuth
sys.modules.update({"authlib": al, "authlib.integrations": al_int, "authlib.integrations.flask_client": al_fc})

from flask import Flask  # noqa: E402
import auth  # noqa: E402
import oauth  # noqa: E402

from flask import session as _session  # noqa: E402

app = Flask(__name__)
app.secret_key = "test"
app.register_blueprint(auth.auth_bp)
app.register_blueprint(oauth.oauth_bp)


@app.before_request
def _end_stale_sessions():          # same hook as main.py
    uid = _session.get("user_id")
    if uid and not auth.session_still_valid(uid, _session.get("sv", 0)):
        _session.clear()


client = app.test_client()
auth.SESSION_CHECK_SEC = 0          # no caching in tests


def row(email):
    c = Conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        return cur.fetchone()
    finally:
        c.close()


def sql(q, params=()):
    c = Conn()
    try:
        c.cursor().execute(q, params)
    finally:
        c.close()


def post(path, data):
    r = client.post(path, json=data)
    return r.status_code, (r.get_json() or {})


def link_in(mail, path):
    m = re.search(r'href="([^"]*' + re.escape(path) + r'[^"]*)"', mail["body"])
    return m.group(1).replace("&amp;", "&") if m else None


os.environ.pop("SITE_URL", None)
os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)

print("\n[0] startup")
auth.init_auth()
auth.init_auth()
c0 = Conn()
cols = [r[1] for r in c0.conn.execute("PRAGMA table_info(users)")]
c0.close()
check("session_version" in cols, "users.session_version added (and a second start does not fail)")

print("\n[1] a Google account cannot be taken over from the sign-up form")
oauth._find_or_create_oauth_user("victim@gmail.com", "google", None)
v = row("victim@gmail.com")
check(v and v["email_verified"] == 1 and v["password_hash"] is None, "Google sign-up: verified, no password")
code, d = post("/api/register", {"email": "victim@gmail.com", "password": "attacker123"})
check(code == 409 and "Google" in d.get("error", ""), "sign-up with that email is refused and points to Google")
check(row("victim@gmail.com")["password_hash"] is None, "no password was added to the Google account")
code, d = post("/api/login", {"email": "victim@gmail.com", "password": "attacker123"})
check(code == 401, "the attacker's password does not log in")
with client.session_transaction() as s:
    check("user_id" not in s, "no session was created")

print("\n[2] normal sign-up, branded email, verification")
os.environ["SITE_URL"] = "https://signals.example.com"
MAIL.clear()
code, d = post("/api/register", {"email": "ali@example.com", "password": "secret123"})
check(code == 201 and "spam" in d.get("message", "") and "ali@example.com" in d.get("message", ""), "201 with a clear message (mentions spam)")
check(len(MAIL) == 1 and "Signals FM" in MAIL[0]["subject"] and "SignalX" not in MAIL[0]["body"], "email says Signals FM, never SignalX")
vlink = link_in(MAIL[0], "/api/verify-email?token=")
check(vlink and vlink.startswith("https://signals.example.com/api/verify-email?token="), "link uses SITE_URL, not the request host")
code, d = post("/api/login", {"email": "ali@example.com", "password": "secret123"})
check(code == 403 and "Forgot password" in d.get("error", ""), "login before verifying: 403 that explains the way out")
code, d = post("/api/register", {"email": "ali@example.com", "password": "other456x"})
check(code == 409 and "Forgot password" in d.get("error", ""), "signing up again (unverified): 409 pointing to Forgot password")
code, d = post("/api/login", {"email": "ali@example.com", "password": "other456x"})
check(code == 401, "a second sign-up did not replace the password")
r = client.get(vlink.replace("https://signals.example.com", ""))
vtok = vlink.split("token=")[1]
check(r.status_code == 302 and r.headers["Location"].endswith("/login?verify=" + vtok), "verify link opens the login page with the token")
check(row("ali@example.com")["email_verified"] == 0, "just opening the link (or a mail scanner opening it) does not verify")
code, d = post("/api/login", {"email": "ali@example.com", "password": "wrong999x", "verify_token": vtok})
check(code == 401 and row("ali@example.com")["email_verified"] == 0, "token with the wrong password: refused, still unverified")
code, d = post("/api/login", {"email": "ali@example.com", "password": "secret123", "verify_token": "x" * 43})
check(code == 403, "right password with a wrong token: still needs the real link")
code, d = post("/api/login", {"email": "ali@example.com", "password": "secret123", "verify_token": "é" * 30})
check(code == 403, "odd characters in the token: a clear 403, not a server error")
code, d = post("/api/login", {"email": "ali@example.com", "password": "secret123", "verify_token": vtok})
check(code == 200 and "confirmed" in d.get("message", "") and row("ali@example.com")["email_verified"] == 1,
      "link + password together confirm the email and log in")
check(row("ali@example.com")["verify_token"] is None, "the verification token is used up")
code, d = post("/api/login", {"email": "ali@example.com", "password": "secret123"})
check(code == 200, "login works after verifying")
code, d = post("/api/register", {"email": "ali@example.com", "password": "secret123"})
check(code == 409 and "already exists" in d.get("error", ""), "signing up again (verified): 409 already exists")

print("\n[3] expired verification link -> Forgot password gets the user in")
MAIL.clear()
post("/api/register", {"email": "sara@example.com", "password": "secret123"})
tok = row("sara@example.com")["verify_token"]
sql("UPDATE users SET verify_token_expires = %s WHERE email = %s", (datetime.utcnow() - timedelta(hours=1), "sara@example.com"))
r = client.get("/api/verify-email?token=" + tok)
check(r.headers["Location"].endswith("/login?error=verify_token_expired"), "expired link -> clear error on the login page")
code, d = post("/api/login", {"email": "sara@example.com", "password": "secret123", "verify_token": tok})
check(code == 403, "an expired token cannot confirm the email at login either")
MAIL.clear()
code, d = post("/api/forgot-password", {"email": "sara@example.com"})
check(code == 200 and "spam" in d.get("message", "") and len(MAIL) == 1, "forgot password sends a reset link (message mentions spam)")
rlink = link_in(MAIL[0], "/reset-password?token=")
check(rlink and rlink.startswith("https://signals.example.com/reset-password?token=") and "Signals FM" in MAIL[0]["subject"], "reset email: Signals FM, SITE_URL link")
rtok = rlink.split("token=")[1]
code, d = post("/api/reset-password", {"token": rtok, "password": "newpass99"})
check(code == 200, "new password saved")
check(row("sara@example.com")["email_verified"] == 1 and row("sara@example.com")["verify_token"] is None, "reset also verified the email")
code, d = post("/api/login", {"email": "sara@example.com", "password": "newpass99"})
check(code == 200, "user can log in now (no more dead end)")
code, d = post("/api/login", {"email": "sara@example.com", "password": "secret123"})
check(code == 401, "old password no longer works")
code, d = post("/api/reset-password", {"token": rtok, "password": "again123x"})
check(code == 400, "a reset link works only once")

print("\n[4] someone pre-registers another person's email")
MAIL.clear()
post("/api/register", {"email": "owner@example.com", "password": "attacker1"})        # attacker, never verifies
scan = link_in(MAIL[-1], "/api/verify-email?token=")
client.get(scan.replace("https://signals.example.com", ""))                          # the owner's mail scanner opens it
check(row("owner@example.com")["email_verified"] == 0 and post("/api/login", {"email": "owner@example.com", "password": "attacker1"})[0] == 403,
      "a mail scanner opening the link does not let the attacker in")
MAIL.clear()
code, d = post("/api/register", {"email": "owner@example.com", "password": "realowner1"})
check(code == 409 and not MAIL, "real owner is told to use Forgot password; nothing is changed")
post("/api/forgot-password", {"email": "owner@example.com"})
rtok = link_in(MAIL[-1], "/reset-password?token=").split("token=")[1]
post("/api/reset-password", {"token": rtok, "password": "realowner1"})
check(post("/api/login", {"email": "owner@example.com", "password": "realowner1"})[0] == 200, "owner gets in with their own password")
check(post("/api/login", {"email": "owner@example.com", "password": "attacker1"})[0] == 401, "the attacker's password is gone")

print("\n[5] Google login on an unverified password account")
post("/api/register", {"email": "pre@gmail.com", "password": "attacker2"})        # attacker, never verifies
u = oauth._find_or_create_oauth_user("pre@gmail.com", "google", "https://pic")
r5 = row("pre@gmail.com")
check(u["id"] == r5["id"] and r5["email_verified"] == 1 and r5["password_hash"] is None, "Google proves the inbox: verified, unproven password dropped")
check(post("/api/login", {"email": "pre@gmail.com", "password": "attacker2"})[0] == 401, "the pre-set password cannot log in")
oauth._find_or_create_oauth_user("ali@example.com", "google", None)
check(post("/api/login", {"email": "ali@example.com", "password": "secret123"})[0] == 200, "a verified account keeps its password after Google login")

print("\n[6] Google account adds a password safely (through the emailed link)")
MAIL.clear()
code, d = post("/api/forgot-password", {"email": "victim@gmail.com"})
check(code == 200 and len(MAIL) == 1 and MAIL[0]["to"] == "victim@gmail.com", "reset link goes only to the Google account's inbox")
rtok = link_in(MAIL[0], "/reset-password?token=").split("token=")[1]
post("/api/reset-password", {"token": rtok, "password": "mine12345"})
check(post("/api/login", {"email": "victim@gmail.com", "password": "mine12345"})[0] == 200, "owner can now log in with a password too")
MAIL.clear()
code, d = post("/api/forgot-password", {"email": "nobody@example.com"})
check(code == 200 and not MAIL, "unknown email: same answer, no email sent")

print("\n[7] Google callback refuses unverified Google emails")
class G:
    def __init__(self, info):
        self.info = info
    def authorize_access_token(self):
        return {"userinfo": self.info}
oauth.oauth.google = G({"email": "x@corp.com", "email_verified": False})
r = client.get("/api/auth/google/callback")
check(r.status_code == 302 and r.headers["Location"].endswith("error=google_email_unverified") and row("x@corp.com") is None,
      "unverified Google email: no account, clear error")
oauth.oauth.google = G({"email": "new@gmail.com", "email_verified": True, "picture": "https://p"})
r = client.get("/api/auth/google/callback")
with client.session_transaction() as s:
    check(r.headers["Location"].endswith("/") and s.get("email") == "new@gmail.com", "verified Google email: logged in")

print("\n[7b] Google callback without an email_verified claim is refused")
oauth.oauth.google = G({"email": "nofield@gmail.com"})
r = client.get("/api/auth/google/callback")
check(r.headers["Location"].endswith("error=google_email_unverified") and row("nofield@gmail.com") is None, "missing claim: refused")

print("\n[7c] a password reset logs out other devices")
phone = app.test_client()
code, _ = phone.post("/api/login", json={"email": "sara@example.com", "password": "newpass99"}).status_code, None
check(code == 200 and phone.get("/api/me").status_code == 200, "phone is logged in")
laptop = app.test_client()
MAIL.clear()
laptop.post("/api/forgot-password", json={"email": "sara@example.com"})
rtok = link_in(MAIL[-1], "/reset-password?token=").split("token=")[1]
r = laptop.post("/api/reset-password", json={"token": rtok, "password": "fresh777x"})
check(r.status_code == 200 and "logged out" in r.get_json()["message"], "reset says every device was logged out")
check(phone.get("/api/me").status_code == 401, "the phone's old session no longer works")
check(laptop.post("/api/login", json={"email": "sara@example.com", "password": "fresh777x"}).status_code == 200
      and laptop.get("/api/me").status_code == 200, "a fresh login works normally")

print("\n[7d] one-time clean-up of passwords set on Google accounts by the old bug")
sql("INSERT INTO users (email, password_hash, auth_provider, email_verified) VALUES (%s, %s, 'google', 1)",
    ("hijacked@gmail.com", auth.generate_password_hash("attacker9")))
attacker = app.test_client()
check(attacker.post("/api/login", json={"email": "hijacked@gmail.com", "password": "attacker9"}).status_code == 200, "(before) the old bug's password works")
c0 = Conn()
c0.conn.execute("DELETE FROM app_migrations")
c0.close()
auth.init_auth()
check(row("hijacked@gmail.com")["password_hash"] is None, "clean-up removed the untrusted password")
check(attacker.get("/api/me").status_code == 401, "and logged out the session that used it")
check(post("/api/login", {"email": "hijacked@gmail.com", "password": "attacker9"})[0] == 401, "the old password no longer logs in")
MAIL.clear()
post("/api/forgot-password", {"email": "hijacked@gmail.com"})
rtok = link_in(MAIL[-1], "/reset-password?token=").split("token=")[1]
post("/api/reset-password", {"token": rtok, "password": "owner1234"})
auth.init_auth()
check(post("/api/login", {"email": "hijacked@gmail.com", "password": "owner1234"})[0] == 200,
      "clean-up runs only once: a password the owner sets later is kept")

print("\n[8] email links: safe base address")
with app.test_request_context("/", base_url="https://evil.example.org"):
    os.environ["SITE_URL"] = "https://signals.example.com"
    check(auth._base_url() == "https://signals.example.com", "SITE_URL wins over the request host")
    os.environ["SITE_URL"] = "javascript:alert(1)"
    os.environ["RAILWAY_PUBLIC_DOMAIN"] = "signalx-app-production.up.railway.app"
    check(auth._base_url() == "https://signalx-app-production.up.railway.app", "bad SITE_URL ignored, Railway domain used")
    os.environ.pop("SITE_URL"); os.environ.pop("RAILWAY_PUBLIC_DOMAIN")
    check(auth._base_url() == "https://evil.example.org", "no settings: falls back to the request address")
body = auth._email_html("T", "<p>x</p>", "Go", 'https://a.example/x?token=a"b<c')
check('href="https://a.example/x?token=a&quot;b&lt;c"' in body, "link in emails is escaped")

print(f"\nALL {passed} CHECKS PASSED")
