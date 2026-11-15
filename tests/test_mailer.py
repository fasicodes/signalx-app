"""mailer.py: which email service is used (Brevo, Resend or Gmail SMTP), what each one is sent, and the errors the
admin page shows (a blocked SMTP port on Railway Hobby / Trial, a wrong Gmail password, a refused API key).
No network: requests.post and smtplib.SMTP are replaced by fakes. Run from the project root:  python tests/test_mailer.py
"""
import os
import smtplib
import socket
import sys

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

print("\n[5] code stays readable by older Python (production version unknown)")
import ast  # noqa: E402
for f in ("mailer.py", "auth.py", "signalboard.py"):
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
