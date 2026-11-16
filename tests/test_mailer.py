"""mailer.py: which email service is used (Brevo, Resend or Gmail SMTP), what each one is sent, and the errors the
admin page shows (a blocked SMTP port on Railway Hobby / Trial, a wrong Gmail password, a refused API key).
No network: requests.post and smtplib.SMTP are replaced by fakes. Run from the project root:  python tests/test_mailer.py
"""
import base64
import json
import os
import smtplib
import socket
import sys
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import requests  # noqa: E402
import mailer  # noqa: E402

FAILS = []
MAIL_VARS = ("BREVO_API_KEY", "RESEND_API_KEY", "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM", "MAIL_SENDER_NAME")
HTML = '<p>Hello</p><p><a href="https://example.com/reset?token=abc">Choose a new password</a></p>'


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def env(**kw):
    for k in MAIL_VARS:
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in kw.items() if v is not None})


class Resp:
    def __init__(self, code, data=None, text=""):
        self.status_code, self._data, self.text = code, data, text

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


POSTS = []
ANSWER = {"resp": Resp(201, {"messageId": "<1@brevo>"})}


def fake_post(url, json=None, headers=None, timeout=None, **kw):
    POSTS.append({"url": url, "json": json, "headers": headers or {}, "timeout": timeout})
    a = ANSWER["resp"]
    if isinstance(a, Exception):
        raise a
    return a


requests.post = fake_post

# the connected Gmail lives in MySQL (mail_settings); here a dict stands in for that table
STORE = {}


def fake_read():
    if STORE.get("__down__"):
        raise RuntimeError("database busy")
    return {k: v for k, v in STORE.items() if not k.startswith("__")}


def fake_write(values):
    for k, v in values.items():
        if v is None:
            STORE.pop(k, None)
        else:
            STORE[k] = v


mailer._db_read = fake_read
mailer._db_write = fake_write

print("\n[1] no email service set up")
env()
check(mailer.provider() is None, "provider() is None without any key or Gmail login")
ok = mailer.send_email("member@example.com", "Hi", HTML)
st = mailer.status()
check(ok is False and not POSTS, "send_email returns False and calls nothing")
check(st["provider"] is None and st["last"]["ok"] is False and st["notes"] and "BREVO_API_KEY" in st["notes"][0],
      "status: not set up, last send failed, a note says which variables to set")

print("\n[2] Brevo (HTTPS, no own domain needed)")
env(BREVO_API_KEY=" brevo-key-123 ", MAIL_FROM="Signals FM <owner@gmail.com>", MAIL_USERNAME="old@gmail.com",
    MAIL_PASSWORD="x")
check(mailer.provider() == "brevo", "BREVO_API_KEY wins over the Gmail login")
POSTS.clear()
ANSWER["resp"] = Resp(201, {"messageId": "<1@brevo>"})
ok = mailer.send_email("member@example.com", "Set a new password", HTML, reply_to="visitor@example.org")
p = POSTS[0] if POSTS else {}
j = p.get("json") or {}
check(ok is True and p.get("url") == mailer.BREVO_URL, "sent to Brevo's email API")
check(p.get("headers", {}).get("api-key") == "brevo-key-123", "API key (trimmed) in the api-key header")
check(j.get("sender") == {"name": "Signals FM", "email": "owner@gmail.com"} and j.get("to") == [{"email": "member@example.com"}],
      "sender from MAIL_FROM, one recipient")
check(j.get("subject") == "Set a new password" and j.get("htmlContent") == HTML
      and "Choose a new password (https://example.com/reset?token=abc)" in j.get("textContent", ""),
      "HTML body plus a plain-text copy with the link written out")
check(j.get("replyTo") == {"email": "visitor@example.org"}, "reply_to passed on (contact form)")
check(p.get("timeout") == mailer.TIMEOUT_SEC, "request has a timeout")
st = mailer.status()
check(st["provider_name"] == "Brevo" and st["sender"] == "Signals FM <owner@gmail.com>" and st["last"]["ok"] is True
      and not st["notes"], "status: Brevo, the sender, last send OK, no warnings")
check(mailer.last_failed() is False, "last_failed(): False after an email went out")
check("brevo-key-123" not in repr(st), "status never contains the key")

POSTS.clear()
mailer.send_email("member@example.com", "Hi", HTML, reply_to="a@b.com\r\nBcc: x@y.com")
check("replyTo" not in (POSTS[0]["json"] if POSTS else {"replyTo": 1}), "a reply_to with a line break is dropped")

ANSWER["resp"] = Resp(401, {"code": "unauthorized", "message": "We have detected you are using an unrecognised IP address"})
ok = mailer.send_email("member@example.com", "Hi", HTML)
st = mailer.status()
check(ok is False and st["last"]["ok"] is False and "HTTP 401" in st["last"]["error"]
      and "unrecognised IP address" in st["last"]["error"], "a refused request: False, Brevo's own message kept for the admin")

ANSWER["resp"] = requests.exceptions.ConnectTimeout("connect timeout")
ok = mailer.send_email("member@example.com", "Hi", HTML)
check(ok is False and "connect timeout" in mailer.status()["last"]["error"], "a network error never raises")
check(mailer.last_failed() is True, "last_failed(): the last email failed (forgot password then warns the link may not arrive)")

env(BREVO_API_KEY="k", MAIL_SENDER_NAME="Signals FM")
ok = mailer.send_email("member@example.com", "Hi", HTML)
st = mailer.status()
check(ok is False and "MAIL_FROM" in st["last"]["error"] and st["notes"] and "MAIL_FROM" in st["notes"][0],
      "Brevo without any sender address: not sent, the note asks for MAIL_FROM")
env(BREVO_API_KEY="k", MAIL_USERNAME="owner@gmail.com")
check(mailer.sender() == ("Signals FM", "owner@gmail.com"), "without MAIL_FROM the Gmail address is the sender")
env(BREVO_API_KEY="k", MAIL_FROM="owner@gmail.com", MAIL_SENDER_NAME="Signals Desk")
check(mailer.sender() == ("Signals Desk", "owner@gmail.com"), "MAIL_FROM without a name takes MAIL_SENDER_NAME")
for raw in ('"Signals FM <owner@gmail.com>"', "Signals FM owner@gmail.com", " 'Signals FM' <owner@gmail.com> "):
    env(BREVO_API_KEY='"k"', MAIL_FROM=raw)
    check(mailer.sender() == ("Signals FM", "owner@gmail.com"), f"MAIL_FROM written as {raw!r} still works")
check(mailer._env("BREVO_API_KEY") == "k", "a key pasted with quotes around it is used without them")
env(BREVO_API_KEY="k", MAIL_FROM="Signals FM <not an address>")
check(mailer.sender() == ("Signals FM", ""), "a MAIL_FROM without an address gives no sender (and a note)")

print("\n[3] Resend (HTTPS, own domain)")
env(RESEND_API_KEY="re_key", MAIL_FROM="Signals FM <no-reply@mydomain.com>")
check(mailer.provider() == "resend", "RESEND_API_KEY -> Resend")
POSTS.clear()
ANSWER["resp"] = Resp(200, {"id": "abc"})
ok = mailer.send_email("member@example.com", "Hi", HTML, reply_to="visitor@example.org")
p = POSTS[0] if POSTS else {}
j = p.get("json") or {}
check(ok is True and p.get("url") == mailer.RESEND_URL and p.get("headers", {}).get("Authorization") == "Bearer re_key",
      "sent to Resend with the key as a Bearer token")
check(j.get("from") == "Signals FM <no-reply@mydomain.com>" and j.get("to") == ["member@example.com"]
      and j.get("reply_to") == "visitor@example.org" and j.get("text"), "from, to, reply_to and a text copy")
ANSWER["resp"] = Resp(403, {"statusCode": 403, "message": "The mydomain.com domain is not verified."})
ok = mailer.send_email("member@example.com", "Hi", HTML)
check(ok is False and "domain is not verified" in mailer.status()["last"]["error"], "Resend's refusal is shown")
env(RESEND_API_KEY="re_key")
st = mailer.status()
check(st["sender"] == "Signals FM <onboarding@resend.dev>" and st["notes"] and "test sender" in st["notes"][0],
      "Resend without MAIL_FROM: its test sender, with a warning that it only reaches the account's own email")

print("\n[4] Gmail SMTP")


class FakeSMTP:
    mode = "ok"
    sent = []

    def __init__(self, host, port, timeout=None):
        FakeSMTP.args = (host, port, timeout)
        if FakeSMTP.mode == "blocked":
            raise socket.timeout("timed out")
        if FakeSMTP.mode == "refused":
            raise ConnectionRefusedError(111, "Connection refused")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        if FakeSMTP.mode == "badpass":
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def sendmail(self, frm, to, msg):
        if FakeSMTP.mode == "recipient":
            raise smtplib.SMTPRecipientsRefused({to[0]: (550, b"no such user")})
        FakeSMTP.sent.append((frm, to, msg))


smtplib.SMTP = FakeSMTP
env(MAIL_USERNAME="owner@gmail.com", MAIL_PASSWORD="abcd efgh ijkl mnop")
check(mailer.provider() == "smtp", "Gmail login only -> SMTP")
POSTS.clear()
ok = mailer.send_email("member@example.com", "Hi", HTML, reply_to="visitor@example.org")
msg = FakeSMTP.sent[-1][2] if FakeSMTP.sent else ""
check(ok is True and not POSTS and FakeSMTP.args == ("smtp.gmail.com", 587, mailer.TIMEOUT_SEC),
      "sent through smtp.gmail.com:587 with a timeout")
check("From: Signals FM <owner@gmail.com>" in msg and "Reply-To: visitor@example.org" in msg, "From and Reply-To headers")
st = mailer.status()
check(st["provider_name"] == "Gmail SMTP" and st["notes"] and "Railway Pro" in st["notes"][0],
      "status warns that SMTP needs Railway Pro")

for mode in ("blocked", "refused"):
    FakeSMTP.mode = mode
    ok = mailer.send_email("member@example.com", "Hi", HTML)
    err = mailer.status()["last"]["error"] or ""
    check(ok is False and "Railway blocks outbound SMTP" in err and "BREVO_API_KEY" in err,
          f"port {mode}: False, and the error says Railway blocks SMTP and what to set instead")
FakeSMTP.mode = "badpass"
ok = mailer.send_email("member@example.com", "Hi", HTML)
err = mailer.status()["last"]["error"] or ""
check(ok is False and "App Password" in err and "Railway blocks" not in err, "wrong Gmail password: App Password hint")
FakeSMTP.mode = "recipient"
ok = mailer.send_email("member@example.com", "Hi", HTML)
err = mailer.status()["last"]["error"] or ""
check(ok is False and "Railway blocks" not in err, "a refused recipient is not blamed on a blocked port")
FakeSMTP.mode = "ok"

print("\n[5] Connect Gmail: the site sends through the owner's own Gmail (Gmail API over HTTPS)")
GV = {"GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "gsecret", "SECRET_KEY": "k" * 32}
REDIR = "https://site.example/api/auth/google/callback"
TOKEN, SEND, REVOKE = mailer.GOOGLE_TOKEN_URL, mailer.GMAIL_SEND_URL, mailer.GOOGLE_REVOKE_URL
env()
for k in GV:
    os.environ.pop(k, None)
check(not mailer.gmail_available() and mailer.status()["gmail"]["available"] is False
      and "GOOGLE_CLIENT_ID" in mailer.status()["notes"][0], "without the Google login settings Connect Gmail is not offered")
os.environ.update(GV)
check(mailer.gmail_available() and "Connect Gmail" in mailer.status()["notes"][0], "with them it is, and the note points to it")
q = parse_qs(urlparse(mailer.gmail_auth_url(REDIR, "st4te")).query)
check(q.get("scope") == ["openid email https://www.googleapis.com/auth/gmail.send"] and q.get("access_type") == ["offline"]
      and "consent" in q.get("prompt", [""])[0] and q.get("state") == ["st4te"] and q.get("redirect_uri") == [REDIR]
      and q.get("client_id") == [GV["GOOGLE_CLIENT_ID"]], "Google's page: send-only permission, lasting, the login's return address")

ROUTES = {}


def route_post(url, json=None, data=None, headers=None, timeout=None, **kw):
    POSTS.append({"url": url, "json": json, "data": data, "headers": headers or {}, "timeout": timeout})
    a = ROUTES.get(url)
    if isinstance(a, list):
        a = a.pop(0)
    if isinstance(a, Exception):
        raise a
    return a


def jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return enc({"alg": "RS256"}) + "." + enc(claims) + ".signature"


requests.post = route_post
GRANT = {"access_token": "acc-1", "expires_in": 3599, "refresh_token": "1//refresh-xyz", "token_type": "Bearer",
         "scope": "openid https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/userinfo.email",
         "id_token": jwt({"email": "Owner@Gmail.com", "email_verified": True})}
ROUTES[TOKEN] = Resp(200, dict(GRANT, scope="openid https://www.googleapis.com/auth/userinfo.email"))
ok, msg = mailer.gmail_finish("code-0", REDIR)
check(not ok and "Send email on your behalf" in msg and not STORE and mailer.provider() is None,
      "permission box not ticked on Google's page: not connected, the admin is told to tick it")
ROUTES[TOKEN] = Resp(400, {"error": "invalid_grant", "error_description": "Bad Request"})
ok, msg = mailer.gmail_finish("used-code", REDIR)
check(not ok and "Google refused" in msg and not STORE, "a refused code: not connected, Google's reason shown")
ROUTES[TOKEN] = Resp(200, {k: v for k, v in GRANT.items() if k != "refresh_token"})
ok, msg = mailer.gmail_finish("code-1", REDIR)
check(not ok and "lasting permission" in msg and not STORE, "no lasting permission (refresh token): not connected")
POSTS.clear()
ROUTES[TOKEN] = Resp(200, GRANT)
ok, msg = mailer.gmail_finish("code-2", REDIR)
d = POSTS[-1]["data"] if POSTS else {}
check(ok and "owner@gmail.com" in msg, "connected: the message names the Gmail")
check(d.get("grant_type") == "authorization_code" and d.get("code") == "code-2" and d.get("redirect_uri") == REDIR
      and d.get("client_secret") == "gsecret", "the code is swapped at Google with the same return address")
check(STORE.get("gmail_email") == "owner@gmail.com" and STORE.get("gmail_token")
      and "refresh-xyz" not in STORE["gmail_token"], "the Gmail and its permission are stored, the permission encrypted")
check(mailer.provider() == "gmail", "a connected Gmail is used")
os.environ["BREVO_API_KEY"] = "k"
check(mailer.provider() == "gmail", "the connected Gmail comes before BREVO_API_KEY")
os.environ.pop("BREVO_API_KEY")
st = mailer.status()
check(st["provider_name"] == "Gmail" and st["sender"] == "Signals FM <owner@gmail.com>" and st["gmail"]["connected"]
      and st["gmail"]["email"] == "owner@gmail.com" and not st["notes"], "status: Gmail, the sender is that Gmail, no warnings")
check("refresh-xyz" not in repr(st) and "acc-1" not in repr(st), "status never shows a token")

POSTS.clear()
ROUTES[SEND] = Resp(200, {"id": "m1", "threadId": "t1", "labelIds": ["SENT"]})
ok = mailer.send_email("member@example.com", "Set a new password", HTML, reply_to="visitor@example.org")
p = POSTS[-1] if POSTS else {"json": {"raw": ""}, "headers": {}, "url": ""}
raw = base64.urlsafe_b64decode(p["json"]["raw"].encode()).decode()
check(ok and len(POSTS) == 1 and p["url"] == SEND and p["headers"].get("Authorization") == "Bearer acc-1",
      "sent with the Gmail API, with the fresh access token (no extra call to Google)")
check("From: Signals FM <owner@gmail.com>" in raw and "To: member@example.com" in raw and "Subject: Set a new password" in raw
      and "Reply-To: visitor@example.org" in raw and "text/html" in raw and "text/plain" in raw,
      "the email: from the Gmail, to, subject, reply-to, HTML and a text copy")

mailer._gmail["access_until"] = 0
POSTS.clear()
ROUTES[TOKEN] = Resp(200, {"access_token": "acc-2", "expires_in": 3599, "token_type": "Bearer"})
ok = mailer.send_email("member@example.com", "Hi", HTML)
check(ok and [x["url"] for x in POSTS] == [TOKEN, SEND] and POSTS[0]["data"].get("grant_type") == "refresh_token"
      and POSTS[0]["data"].get("refresh_token") == "1//refresh-xyz" and POSTS[1]["headers"].get("Authorization") == "Bearer acc-2",
      "an expired access token is renewed with the stored permission")
POSTS.clear()
ROUTES[SEND] = [Resp(401, {"error": {"code": 401, "message": "Invalid Credentials"}}), Resp(200, {"id": "m2"})]
ROUTES[TOKEN] = Resp(200, {"access_token": "acc-3", "expires_in": 3599})
ok = mailer.send_email("member@example.com", "Hi", HTML)
check(ok and [x["url"] for x in POSTS] == [SEND, TOKEN, SEND] and POSTS[2]["headers"].get("Authorization") == "Bearer acc-3",
      "a refused access token is renewed once and the email still goes out")
ROUTES[SEND] = Resp(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message":
                      "Gmail API has not been used in project 123 before or it is disabled. Enable it by visiting "
                      "https://console.developers.google.com/apis/api/gmail.googleapis.com/overview?project=123 then retry."}})
ok = mailer.send_email("member@example.com", "Hi", HTML)
err = mailer.status()["last"]["error"] or ""
check(not ok and "HTTP 403" in err and "Turn on the Gmail API" in err, "Gmail API switched off in Google Cloud: says how to turn it on")
ROUTES[SEND] = Resp(200, {"id": "m3"})

mailer._gmail["access_until"] = 0
ROUTES[TOKEN] = Resp(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
ok = mailer.send_email("member@example.com", "Hi", HTML)
err = mailer.status()["last"]["error"] or ""
check(not ok and "Connect Gmail" in err and "In production" in err,
      "Google ended the permission: says to connect again (and the 7-day Testing hint)")

mailer._gmail.update(loaded=0.0, email=None, token=None, enc=None, since=None, access=None, access_until=0.0, error=None)
check(mailer.gmail_account() == {"email": "owner@gmail.com", "since": STORE.get("gmail_since"), "ready": True},
      "after a restart the Gmail is read back from the database and its permission decrypted")
STORE["__down__"] = True
check(mailer.gmail_account(force=True) is not None, "database busy: the connected Gmail is kept, not dropped")
STORE.pop("__down__")
os.environ["SECRET_KEY"] = "another-secret-key-" * 2
mailer._gmail.update(loaded=0.0, enc=None, token=None, access=None, access_until=0.0)
acct = mailer.gmail_account()
ok = mailer.send_email("member@example.com", "Hi", HTML)
check(acct is not None and acct["ready"] is False and not ok and "SECRET_KEY" in (mailer.status()["last"]["error"] or "")
      and mailer.status()["notes"], "a permission saved with another SECRET_KEY is not used; the admin is told to connect again")
os.environ["SECRET_KEY"] = GV["SECRET_KEY"]
mailer._gmail.update(loaded=0.0, enc=None)
check(mailer.gmail_account()["ready"] is True and mailer.status()["gmail"]["error"] is None, "the right SECRET_KEY reads it again")

POSTS.clear()
ROUTES[REVOKE] = Resp(200, {})
mailer.gmail_disconnect()
check(not STORE and mailer.provider() is None and POSTS and POSTS[-1]["url"] == REVOKE
      and POSTS[-1]["data"] == {"token": "1//refresh-xyz"}, "disconnect: forgotten, the permission cancelled at Google, nothing left")
ROUTES[REVOKE] = requests.exceptions.ConnectionError("down")
mailer.gmail_disconnect()
check(mailer.provider() is None, "disconnect again (Google unreachable) does not fail")
for k in GV:
    os.environ.pop(k, None)
requests.post = fake_post

print("\n[6] code stays readable by older Python (production version unknown)")
import ast  # noqa: E402
for f in ("mailer.py", "auth.py", "signalboard.py", "oauth.py"):
    src = open(os.path.join(ROOT, f), encoding="utf-8").read()
    try:
        ast.parse(src, feature_version=(3, 10))
        ok = True
    except SyntaxError as e:
        ok = False
        print("   ", f, e)
    check(ok, f"{f} parses as Python 3.10")

env()
print()
print(f"{len(FAILS)} failed" if FAILS else "ALL PASSED")
sys.exit(1 if FAILS else 0)
