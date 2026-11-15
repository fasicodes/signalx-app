"""
Sends the site's emails (sign-up confirmation, password reset, contact form, signal alerts).

Railway's Free, Trial and Hobby plans block outbound SMTP (ports 25, 465, 587, 2525), so Gmail SMTP cannot send from
there: use one of the email services below, which are called over HTTPS (port 443, never blocked). The first one set
up is used:

  BREVO_API_KEY       Brevo (brevo.com, free: 300 emails a day). Needs no domain of your own: MAIL_FROM can be an
                      address you verified as a sender in Brevo, e.g. your Gmail address.
  RESEND_API_KEY      Resend (resend.com, free: 100 emails a day). Sends only from a domain you own and verified in
                      Resend (DNS records); until then only to the Resend account's own email.
  MAIL_USERNAME       Gmail SMTP: the Gmail address ...
  MAIL_PASSWORD       ... and its 16-character Gmail App Password. Works only where SMTP is allowed (Railway Pro, locally).

  MAIL_FROM           The sender: "Signals FM <address>" or just the address (default: MAIL_USERNAME).
  MAIL_SENDER_NAME    The name the emails come from when MAIL_FROM has none (default "Signals FM").

send_email() never raises: it returns True or False and remembers the last result. The admin page (/admin/news) shows
it (status(), /api/admin/mail-status) with a "Send a test email" button, so a delivery problem is visible without
reading the server logs.
"""

import os
import re
import smtplib
import threading
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, parseaddr

TIMEOUT_SEC = 15        # without a timeout one unreachable mail server could freeze every worker thread
SMTP_TIMEOUT_SEC = TIMEOUT_SEC
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
RESEND_URL = "https://api.resend.com/emails"
RESEND_TEST_SENDER = "onboarding@resend.dev"
USER_AGENT = "SignalsFM-mailer/1.0"
SMTP_BLOCKED_HINT = ("The mail server could not be reached. Railway blocks outbound SMTP on its Free, Trial and Hobby "
                     "plans: set BREVO_API_KEY (or RESEND_API_KEY) and MAIL_FROM in the Railway variables instead.")
NAMES = {"brevo": "Brevo", "resend": "Resend", "smtp": "Gmail SMTP"}

_last = {"at": None, "ok": None, "provider": None, "error": None}
_lock = threading.Lock()


def _plain_text(html_body):
    """Plain-text copy of the HTML mail (mail programs and spam filters like having both)."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html_body)
    text = re.sub(r"(?i)<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2 (\1)", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h1>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&middot;", "-").replace("&amp;", "&")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


EMAIL_RE = re.compile(r"[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[A-Za-z]{2,}")


def _env(name):
    """A setting without spaces or quotes around it (a value pasted as "..." into Railway keeps its quotes)."""
    return (os.environ.get(name) or "").strip().strip("\"'").strip()


def provider():
    """'brevo', 'resend', 'smtp' (Gmail) or None when no email service is set up."""
    if _env("BREVO_API_KEY"):
        return "brevo"
    if _env("RESEND_API_KEY"):
        return "resend"
    if _env("MAIL_USERNAME") and os.environ.get("MAIL_PASSWORD"):
        return "smtp"
    return None


def sender(prov=None):
    """(name, address) the emails come from: MAIL_FROM, else MAIL_USERNAME, else (Resend) its test sender.
    MAIL_FROM may be "Name <address>", "Name address" or just the address."""
    prov = prov or provider()
    raw = _env("MAIL_FROM")
    name, addr = parseaddr(raw)
    if not EMAIL_RE.fullmatch(addr):
        m = EMAIL_RE.search(raw)
        name, addr = (raw[:m.start()].strip(" <\"'"), m.group(0)) if m else ("", "")
    name = name.strip(" \"'") or _env("MAIL_SENDER_NAME") or "Signals FM"
    if not addr and EMAIL_RE.fullmatch(_env("MAIL_USERNAME")):
        addr = _env("MAIL_USERNAME")
    if not addr and prov == "resend":
        addr = RESEND_TEST_SENDER
    return name, addr


def _note(ok, prov, error=None):
    with _lock:
        _last.update(at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), ok=ok, provider=prov, error=error)


def status():
    """What the admin page shows: the service in use, the sender and the last send (never a key or password)."""
    prov = provider()
    name, addr = sender(prov)
    notes = []
    if prov is None:
        notes.append("No email service is set up, so no email can be sent (sign-up links, password resets, alerts). "
                     "Set BREVO_API_KEY (or RESEND_API_KEY) and MAIL_FROM in the Railway variables.")
    elif not addr:
        notes.append("MAIL_FROM is not set: set it to the sender address you verified with the email service.")
    elif prov == "resend" and addr == RESEND_TEST_SENDER:
        notes.append("MAIL_FROM is not set: Resend's test sender only delivers to the Resend account's own email. "
                     "Verify your domain in Resend and set MAIL_FROM to an address on it.")
    elif prov == "smtp":
        notes.append("Gmail SMTP works only where outbound SMTP is allowed (Railway Pro, or locally). On Railway "
                     "Hobby or Trial use Brevo or Resend instead.")
    with _lock:
        last = dict(_last)
    return {"provider": prov, "provider_name": NAMES.get(prov), "sender": formataddr((name, addr)) if addr else None,
            "last": last, "notes": notes}


def _http_error(service, r):
    try:
        d = r.json() or {}
        msg = d.get("message") or d.get("error") or r.text
    except ValueError:
        msg = r.text
    return RuntimeError(f"{service} answered HTTP {r.status_code}: {str(msg)[:400]}")


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
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(sender("smtp"))
    msg["To"] = to_email
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(_plain_text(html_body), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
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


_SENDERS = {"brevo": _send_brevo, "resend": _send_resend, "smtp": _send_smtp}


def send_email(to_email, subject, html_body, reply_to=None):
    """Sends one email. Never raises: True when the email service accepted it, else False (the reason is printed
    to the server log and kept for the admin page). reply_to: optional address that "Reply" goes to (contact form)."""
    prov = provider()
    if prov is None:
        print(f"[mailer] WARNING: no email service is set up (BREVO_API_KEY, RESEND_API_KEY or MAIL_USERNAME/"
              f"MAIL_PASSWORD) - email to {to_email} not sent")
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
