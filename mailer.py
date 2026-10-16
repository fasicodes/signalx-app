"""
Email bhejne ka helper - Gmail SMTP use karta hai.

Zaroori environment variables:
    MAIL_USERNAME       - Gmail address (e.g. you@gmail.com)
    MAIL_PASSWORD       - Gmail App Password (16-digit, spaces ke bina)
    MAIL_SENDER_NAME    - Kis naam se email aaye (default: "Signals FM")
"""

import os
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


SMTP_TIMEOUT_SEC = 15   # without a timeout one unreachable mail server could freeze every worker thread


def _plain_text(html_body):
    """Plain-text copy of the HTML mail (mail programs and spam filters like having both)."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html_body)
    text = re.sub(r"(?i)<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2 (\1)", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h1>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&middot;", "-").replace("&amp;", "&")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def send_email(to_email, subject, html_body, reply_to=None):
    """Email bhejta hai. Agar MAIL_USERNAME/PASSWORD set nahi hain to
    False return karta hai aur warning print karta hai (crash nahi karta).
    reply_to: optional address that "Reply" goes to (contact form)."""
    username = os.environ.get("MAIL_USERNAME")
    password = os.environ.get("MAIL_PASSWORD")
    sender_name = os.environ.get("MAIL_SENDER_NAME", "Signals FM")

    if not username or not password:
        print(f"[mailer] WARNING: MAIL_USERNAME/PASSWORD not set - email to {to_email} not sent")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{username}>"
    msg["To"] = to_email
    if reply_to and "\n" not in reply_to and "\r" not in reply_to:
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(_plain_text(html_body), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=SMTP_TIMEOUT_SEC) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[mailer] ERROR sending email to {to_email}: {e}")
        return False
