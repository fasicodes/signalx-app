"""
Email bhejne ka helper - Gmail SMTP use karta hai.

Zaroori environment variables:
    MAIL_USERNAME       - Gmail address (e.g. you@gmail.com)
    MAIL_PASSWORD       - Gmail App Password (16-digit, spaces ke bina)
    MAIL_SENDER_NAME    - Kis naam se email aaye (default: "SignalX")
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def send_email(to_email, subject, html_body):
    """Email bhejta hai. Agar MAIL_USERNAME/PASSWORD set nahi hain to
    False return karta hai aur warning print karta hai (crash nahi karta)."""
    username = os.environ.get("MAIL_USERNAME")
    password = os.environ.get("MAIL_PASSWORD")
    sender_name = os.environ.get("MAIL_SENDER_NAME", "SignalX")

    if not username or not password:
        print(f"[mailer] WARNING: MAIL_USERNAME/PASSWORD not set - email to {to_email} not sent")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{username}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[mailer] ERROR sending email to {to_email}: {e}")
        return False
