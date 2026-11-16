"""
Sends the site's emails (sign-up confirmation, password reset, contact form, signal alerts).

Railway's Free, Trial and Hobby plans block outbound SMTP (ports 25, 465, 587, 2525), so a site on Railway cannot use
Gmail's SMTP server. The ways below send over HTTPS (port 443, never blocked). The first one set up is used:

  Gmail (connected)   The site sends through the owner's own Gmail with the Gmail API. Set up once on /admin/news
                      with "Connect Gmail": it uses the Google login client (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and
                      its redirect URI .../api/auth/google/callback), and the Gmail API must be enabled in the same Google
                      Cloud project. The lasting permission Google gives back (a refresh token) is stored encrypted
                      with SECRET_KEY in the MySQL table mail_settings.
  BREVO_API_KEY       Brevo (brevo.com). Best with a domain of your own: Brevo rewrites a gmail.com sender address.
  RESEND_API_KEY      Resend (resend.com). Sends only from a domain you own and verified in Resend.
  MAIL_USERNAME       Gmail SMTP: the Gmail address ...
  MAIL_PASSWORD       ... and its 16-character Gmail App Password. Works only where SMTP is allowed (Railway Pro, locally).

  MAIL_FROM           The sender: "Signals FM <address>" or just the address (default: MAIL_USERNAME). With a connected
                      Gmail the address is always that Gmail; only the name is taken from here.
  MAIL_SENDER_NAME    The name the emails come from when MAIL_FROM has none (default "Signals FM").

send_email() never raises: it returns True or False and remembers the last result. The admin page (/admin/news) shows
it (status(), /api/admin/mail-status) with "Connect Gmail" and "Send a test email to me", so a delivery problem is
visible without reading the server logs.
"""

import base64
import hashlib
import json
import os
import re
import smtplib
import threading
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, parseaddr
from urllib.parse import urlencode

TIMEOUT_SEC = 15        # without a timeout one unreachable mail server could freeze every worker thread
SMTP_TIMEOUT_SEC = TIMEOUT_SEC
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
RESEND_URL = "https://api.resend.com/emails"
RESEND_TEST_SENDER = "onboarding@resend.dev"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SETTINGS_CACHE_SEC = 60  # the connected Gmail is read from the database at most once a minute
USER_AGENT = "SignalsFM-mailer/1.0"
SMTP_BLOCKED_HINT = ("The mail server could not be reached. Railway blocks outbound SMTP on its Free, Trial and Hobby "
                     "plans: press \"Connect Gmail\" on /admin/news (sends from your Gmail over HTTPS), or set "
                     "BREVO_API_KEY / RESEND_API_KEY in the Railway variables.")
RECONNECT = "press \"Connect Gmail\" on /admin/news again"
NAMES = {"gmail": "Gmail", "brevo": "Brevo", "resend": "Resend", "smtp": "Gmail SMTP"}

_last = {"at": None, "ok": None, "provider": None, "error": None}
_lock = threading.Lock()
# the connected Gmail: address, refresh token (decrypted), its encrypted copy, since when, a short-lived access token
_gmail = {"loaded": 0.0, "email": None, "token": None, "enc": None, "since": None, "error": None,
          "access": None, "access_until": 0.0}
_gmail_lock = threading.Lock()
EMAIL_RE = re.compile(r"[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[A-Za-z]{2,}")


def _plain_text(html_body):
    """Plain-text copy of the HTML mail (mail programs and spam filters like having both)."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html_body)
    text = re.sub(r"(?i)<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2 (\1)", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h1>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&middot;", "-").replace("&amp;", "&")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _env(name):
    """A setting without spaces or quotes around it (a value pasted as "..." into Railway keeps its quotes)."""
    return (os.environ.get(name) or "").strip().strip("\"'").strip()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- the connected Gmail (stored in MySQL)
def _db_read():
    """{name: val} from the mail_settings table ({} while the table does not exist)."""
    import db
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT name, val FROM mail_settings")
            except Exception:
                return {}
            return {r["name"]: r["val"] for r in cur.fetchall()}
    finally:
        conn.close()


def _db_write(values):
    """Saves (or, for None, deletes) settings in the mail_settings table."""
    import db
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS mail_settings (
                name VARCHAR(64) PRIMARY KEY,
                val TEXT,
                updated_at DATETIME
            )""")
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            for k, v in values.items():
                if v is None:
                    cur.execute("DELETE FROM mail_settings WHERE name = %s", (k,))
                else:
                    cur.execute("REPLACE INTO mail_settings (name, val, updated_at) VALUES (%s, %s, %s)", (k, v, stamp))
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


def _fernet():
    from cryptography.fernet import Fernet
    secret = os.environ.get("SECRET_KEY") or ""
    if not secret:
        raise RuntimeError("SECRET_KEY is not set in the Railway variables, so the Gmail permission cannot be kept safely.")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"signals-fm mail token|" + secret.encode()).digest()))


def google_client():
    """(client id, client secret) of the Google login, which also connects Gmail."""
    return _env("GOOGLE_CLIENT_ID"), _env("GOOGLE_CLIENT_SECRET")


def gmail_available():
    """True when "Connect Gmail" can work: the Google login client is set up."""
    cid, secret = google_client()
    return bool(cid and secret)


def gmail_account(force=False):
    """The connected Gmail as {"email", "since", "ready"} (ready: its permission could be read), or None."""
    now = time.time()
    with _gmail_lock:
        fresh = now - _gmail["loaded"] < SETTINGS_CACHE_SEC
    if force or not fresh:
        try:
            rows = _db_read()
        except Exception:
            rows = None          # the database is busy: keep what we knew, try again in a minute
        with _gmail_lock:
            _gmail["loaded"] = now
            if rows is not None:
                email, enc = rows.get("gmail_email"), rows.get("gmail_token")
                if not (email and enc):
                    _gmail.update(email=None, token=None, enc=None, since=None, access=None, access_until=0.0)
                elif enc != _gmail["enc"]:
                    try:
                        token, err = _fernet().decrypt(enc.encode()).decode(), None
                    except Exception:
                        token, err = None, ("The saved Gmail permission cannot be read (was SECRET_KEY changed?): "
                                            + RECONNECT + ".")
                    _gmail.update(email=email, token=token, enc=enc, since=rows.get("gmail_since"), error=err,
                                  access=None, access_until=0.0)
    with _gmail_lock:
        if not _gmail["email"]:
            return None
        return {"email": _gmail["email"], "since": _gmail["since"], "ready": bool(_gmail["token"])}


def gmail_auth_url(redirect_uri, state):
    """Google's page where the admin picks the Gmail and allows "Send email on your behalf"."""
    cid, _secret = google_client()
    return GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": "openid email " + GMAIL_SCOPE, "access_type": "offline", "prompt": "select_account consent",
        "state": state})


def _json(r):
    try:
        d = r.json()
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _id_token_claims(id_token):
    """The claims of the ID token that came straight from Google's token endpoint (over HTTPS, so no signature check)."""
    try:
        part = str(id_token).split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode())
    except Exception:
        return {}


def _gmail_fail(msg):
    with _gmail_lock:
        _gmail["error"] = msg
    print(f"[mailer] Gmail connect failed: {msg}")
    return False, msg


def gmail_finish(code, redirect_uri):
    """After Google's page: swaps the code for the lasting permission and stores it. Returns (ok, message)."""
    import requests
    cid, secret = google_client()
    if not (cid and secret):
        return _gmail_fail("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set in the Railway variables.")
    if not code:
        return _gmail_fail("Google sent nothing back: " + RECONNECT + ".")
    try:
        r = requests.post(GOOGLE_TOKEN_URL, timeout=TIMEOUT_SEC, headers={"User-Agent": USER_AGENT},
                          data={"code": code, "client_id": cid, "client_secret": secret, "redirect_uri": redirect_uri,
                                "grant_type": "authorization_code"})
    except Exception as e:
        return _gmail_fail(f"Google could not be reached ({type(e).__name__}): " + RECONNECT + ".")
    d = _json(r)
    if r.status_code != 200 or not d.get("access_token"):
        why = d.get("error_description") or d.get("error") or f"HTTP {r.status_code}"
        return _gmail_fail(f"Google refused the connection: {str(why)[:200]}.")
    if GMAIL_SCOPE not in str(d.get("scope") or "").split():
        return _gmail_fail("Google did not give permission to send email: " + RECONNECT +
                           " and tick \"Send email on your behalf\".")
    if not d.get("refresh_token"):
        return _gmail_fail("Google gave no lasting permission: " + RECONNECT + ".")
    claims = _id_token_claims(d.get("id_token"))
    email = str(claims.get("email") or "").strip().lower()
    if not EMAIL_RE.fullmatch(email) or claims.get("email_verified") is False:
        return _gmail_fail("Google did not say which Gmail was connected: " + RECONNECT + ".")
    since = _now()
    try:
        enc = _fernet().encrypt(d["refresh_token"].encode()).decode()
        _db_write({"gmail_email": email, "gmail_token": enc, "gmail_since": since})
    except Exception as e:
        return _gmail_fail(f"The Gmail permission could not be saved: {e}")
    with _gmail_lock:
        _gmail.update(loaded=time.time(), email=email, token=d["refresh_token"], enc=enc, since=since, error=None,
                      access=d["access_token"], access_until=time.time() + float(d.get("expires_in") or 3600))
    return True, f"Gmail connected: the site now sends its emails from {email}."


def gmail_disconnect():
    """Forgets the connected Gmail and asks Google to cancel the permission."""
    with _gmail_lock:
        token = _gmail["token"]
    _db_write({"gmail_email": None, "gmail_token": None, "gmail_since": None})
    with _gmail_lock:
        _gmail.update(loaded=time.time(), email=None, token=None, enc=None, since=None, error=None, access=None,
                      access_until=0.0)
    if token:
        try:
            import requests
            requests.post(GOOGLE_REVOKE_URL, data={"token": token}, timeout=TIMEOUT_SEC, headers={"User-Agent": USER_AGENT})
        except Exception:
            pass


def _gmail_access_token():
    """A short-lived access token, renewed with the stored permission when it is about to run out."""
    import requests
    with _gmail_lock:
        if _gmail["access"] and time.time() < _gmail["access_until"] - 60:
            return _gmail["access"]
        token, err = _gmail["token"], _gmail["error"]
    if not token:
        raise RuntimeError(err or ("Gmail is not connected: " + RECONNECT + "."))
    cid, secret = google_client()
    if not (cid and secret):
        raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set, so the Gmail permission cannot be used.")
    r = requests.post(GOOGLE_TOKEN_URL, timeout=TIMEOUT_SEC, headers={"User-Agent": USER_AGENT},
                      data={"client_id": cid, "client_secret": secret, "refresh_token": token,
                            "grant_type": "refresh_token"})
    d = _json(r)
    if r.status_code != 200 or not d.get("access_token"):
        if d.get("error") == "invalid_grant":
            raise RuntimeError("Google ended the Gmail permission (it was removed, the Google password changed, or it "
                               "expired): " + RECONNECT + ". If it ends every 7 days, set the Google app's publishing "
                               "status to \"In production\" (Google Cloud, OAuth consent screen).")
        why = d.get("error_description") or d.get("error") or f"HTTP {r.status_code}"
        raise RuntimeError(f"Google refused the Gmail permission: {str(why)[:200]}")
    with _gmail_lock:
        _gmail["access"] = d["access_token"]
        _gmail["access_until"] = time.time() + float(d.get("expires_in") or 3600)
    return d["access_token"]


def _gmail_error(r):
    d = _json(r)
    e = d.get("error")
    msg = str((e.get("message") if isinstance(e, dict) else (d.get("error_description") or e)) or r.text)[:400]
    low = (msg + " " + str(r.text)).lower()
    if "has not been used" in low or "is disabled" in low or "service_disabled" in low:
        msg += (" - Turn on the Gmail API: Google Cloud console (the project of the Google login), APIs & Services, "
                "Library, Gmail API, Enable. Wait a few minutes, then try again.")
    elif r.status_code == 403 and ("insufficient" in low or "scope" in low):
        msg += " - " + RECONNECT[0].upper() + RECONNECT[1:] + " and tick \"Send email on your behalf\"."
    return RuntimeError(f"Gmail answered HTTP {r.status_code}: {msg}")


# ---------------------------------------------------------------- which service, which sender
def provider():
    """'gmail' (connected on the admin page), 'brevo', 'resend', 'smtp' (Gmail SMTP) or None when nothing is set up."""
    if gmail_account():
        return "gmail"
    if _env("BREVO_API_KEY"):
        return "brevo"
    if _env("RESEND_API_KEY"):
        return "resend"
    if _env("MAIL_USERNAME") and os.environ.get("MAIL_PASSWORD"):
        return "smtp"
    return None


def sender(prov=None):
    """(name, address) the emails come from: the connected Gmail, else MAIL_FROM, else MAIL_USERNAME, else (Resend)
    its test sender. MAIL_FROM may be "Name <address>", "Name address" or just the address."""
    prov = prov or provider()
    raw = _env("MAIL_FROM")
    name, addr = parseaddr(raw)
    if not EMAIL_RE.fullmatch(addr):
        m = EMAIL_RE.search(raw)
        name, addr = (raw[:m.start()].strip(" <\"'"), m.group(0)) if m else ("", "")
    name = name.strip(" \"'") or _env("MAIL_SENDER_NAME") or "Signals FM"
    if prov == "gmail":
        acct = gmail_account()
        return name, (acct["email"] if acct else "")
    if not addr and EMAIL_RE.fullmatch(_env("MAIL_USERNAME")):
        addr = _env("MAIL_USERNAME")
    if not addr and prov == "resend":
        addr = RESEND_TEST_SENDER
    return name, addr


def _note(ok, prov, error=None):
    with _lock:
        _last.update(at=_now(), ok=ok, provider=prov, error=error)


def last_failed():
    """True when the last email the site tried to send failed (blocked SMTP, a Gmail permission Google ended ...):
    a password-reset link would probably not arrive either. The next email that goes out clears it."""
    with _lock:
        return _last.get("ok") is False


def status():
    """What the admin page shows: the service in use, the sender, the connected Gmail and the last send (never a key,
    password or token)."""
    prov = provider()
    name, addr = sender(prov)
    acct = gmail_account()
    with _gmail_lock:
        gmail_error = _gmail["error"]
    notes = []
    if prov is None:
        notes.append("No email service is set up, so no email can be sent (sign-up links, password resets, alerts). "
                     + ("Press \"Connect Gmail\" below to send from your Gmail."
                        if gmail_available() else
                        "\"Connect Gmail\" needs the Google login settings GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in "
                        "the Railway variables (or set BREVO_API_KEY / RESEND_API_KEY)."))
    elif prov == "gmail" and gmail_error:
        notes.append(gmail_error)
    elif not addr:
        notes.append("MAIL_FROM is not set: set it to the sender address you verified with the email service.")
    elif prov == "resend" and addr == RESEND_TEST_SENDER:
        notes.append("MAIL_FROM is not set: Resend's test sender only delivers to the Resend account's own email. "
                     "Verify your domain in Resend and set MAIL_FROM to an address on it.")
    elif prov == "smtp":
        notes.append("Gmail SMTP works only where outbound SMTP is allowed (Railway Pro, or locally). On Railway "
                     "Hobby or Trial press \"Connect Gmail\" below instead.")
    with _lock:
        last = dict(_last)
    return {"provider": prov, "provider_name": NAMES.get(prov), "sender": formataddr((name, addr)) if addr else None,
            "last": last, "notes": notes,
            "gmail": {"available": gmail_available(), "connected": bool(acct), "email": acct and acct["email"],
                      "since": acct and acct["since"], "error": gmail_error}}


# ---------------------------------------------------------------- sending
def _mime(from_header, to_email, subject, html_body, reply_to):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_header
    msg["To"] = to_email
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(_plain_text(html_body), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def _http_error(service, r):
    d = _json(r)
    msg = d.get("message") or d.get("error") or r.text
    return RuntimeError(f"{service} answered HTTP {r.status_code}: {str(msg)[:400]}")


def _send_gmail(to_email, subject, html_body, reply_to):
    import requests
    name, addr = sender("gmail")
    if not addr:
        raise RuntimeError("Gmail is not connected: " + RECONNECT + ".")
    raw = base64.urlsafe_b64encode(_mime(formataddr((name, addr)), to_email, subject, html_body, reply_to)
                                   .as_bytes()).decode()
    for attempt in (1, 2):
        r = requests.post(GMAIL_SEND_URL, json={"raw": raw}, timeout=TIMEOUT_SEC,
                          headers={"Authorization": f"Bearer {_gmail_access_token()}", "User-Agent": USER_AGENT})
        if r.status_code == 401 and attempt == 1:   # the access token ran out early: get a new one, once
            with _gmail_lock:
                _gmail["access"] = None
            continue
        break
    if r.status_code // 100 != 2:
        raise _gmail_error(r)


def _send_brevo(to_email, subject, html_body, reply_to):
    import requests
    name, addr = sender("brevo")
    if not addr:
        raise RuntimeError("MAIL_FROM is not set: set it to the sender address you verified in Brevo.")
    body = {"sender": {"name": name, "email": addr}, "to": [{"email": to_email}], "subject": subject,
            "htmlContent": html_body, "textContent": _plain_text(html_body)}
    if reply_to:
        body["replyTo"] = {"email": reply_to}
    r = requests.post(BREVO_URL, json=body, timeout=TIMEOUT_SEC,
                      headers={"api-key": _env("BREVO_API_KEY"), "accept": "application/json", "User-Agent": USER_AGENT})
    if r.status_code // 100 != 2:
        raise _http_error("Brevo", r)


def _send_resend(to_email, subject, html_body, reply_to):
    import requests
    name, addr = sender("resend")
    body = {"from": formataddr((name, addr)), "to": [to_email], "subject": subject, "html": html_body,
            "text": _plain_text(html_body)}
    if reply_to:
        body["reply_to"] = reply_to
    r = requests.post(RESEND_URL, json=body, timeout=TIMEOUT_SEC,
                      headers={"Authorization": f"Bearer {_env('RESEND_API_KEY')}", "User-Agent": USER_AGENT})
    if r.status_code // 100 != 2:
        raise _http_error("Resend", r)


def _send_smtp(to_email, subject, html_body, reply_to):
    username, password = _env("MAIL_USERNAME"), os.environ.get("MAIL_PASSWORD")
    msg = _mime(formataddr(sender("smtp")), to_email, subject, html_body, reply_to)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=TIMEOUT_SEC) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, [to_email], msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(f"Gmail refused the login (code {e.smtp_code}): MAIL_PASSWORD must be a 16-character Gmail "
                           "App Password (Google account, Security, 2-Step Verification, App passwords), not the "
                           "normal password.") from e
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected) as e:
        raise RuntimeError(f"{e} - {SMTP_BLOCKED_HINT}") from e
    except smtplib.SMTPException:
        raise
    except OSError as e:    # timed out, connection refused, network unreachable: the port is blocked
        raise RuntimeError(f"{str(e) or type(e).__name__} - {SMTP_BLOCKED_HINT}") from e


_SENDERS = {"gmail": _send_gmail, "brevo": _send_brevo, "resend": _send_resend, "smtp": _send_smtp}


def send_email(to_email, subject, html_body, reply_to=None):
    """Sends one email. Never raises: True when the email service accepted it, else False (the reason is printed
    to the server log and kept for the admin page). reply_to: optional address that "Reply" goes to (contact form)."""
    prov = provider()
    if prov is None:
        print(f"[mailer] WARNING: no email service is set up (Connect Gmail on /admin/news, BREVO_API_KEY, "
              f"RESEND_API_KEY or MAIL_USERNAME/MAIL_PASSWORD) - email to {to_email} not sent")
        _note(False, None, "No email service is set up.")
        return False
    if reply_to and ("\n" in reply_to or "\r" in reply_to or "@" not in reply_to):
        reply_to = None
    try:
        _SENDERS[prov](to_email, subject, html_body, reply_to)
    except Exception as e:
        print(f"[mailer] ERROR sending email to {to_email} via {NAMES[prov]}: {e}")
        _note(False, prov, str(e)[:600])
        return False
    _note(True, prov)
    return True
