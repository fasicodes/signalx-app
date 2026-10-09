"""
Email/password auth routes for Signals FM, MySQL ke sath.
Email hi primary identifier hai (username nahi).

Is file ko main.py mein register karna hai:

    from auth import auth_bp
    app.register_blueprint(auth_bp)

Routes:
    POST /api/register            -> { "email": "...", "password": "..." }
    POST /api/login                 -> { "email": "...", "password": "..." }
    POST /api/logout
    GET  /api/me                     -> current logged-in user batata hai (ya 401)
    GET  /api/verify-email?token=..  -> email confirm karta hai
    POST /api/forgot-password        -> { "email": "..." }
    POST /api/reset-password         -> { "token": "...", "password": "..." }
"""

import html
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash
import pymysql

from db import get_db_connection
from mailer import send_email

auth_bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

VERIFY_TOKEN_HOURS = 24
RESET_TOKEN_MINUTES = 30
SESSION_CHECK_SEC = 60          # how long a "this login is still valid" answer is cached per user


# ===========================================================================
# startup: session versions + a one-time clean-up of the old sign-up bug
# ===========================================================================
def init_auth():
    """Runs at startup. Never raises (the site must start even if the database is busy).

    1. users.session_version: every login stores it in the session; resetting the password (or Google
       removing an unproven password) increases it, which logs out every other device on that account.
    2. One time only: the old sign-up form could set a password on a Google account without proof of the
       inbox, and the old verify link could confirm a password someone else had chosen. Passwords on accounts
       that use Google sign-in cannot be trusted, so they are removed once; the owners keep logging in with
       Google, or set a new password with "Forgot password" (the link only reaches their inbox)."""
    try:
        conn = get_db_connection()
    except Exception as e:
        print(f"[auth] WARNING: startup checks skipped: {e}")
        return
    try:
        with conn.cursor() as cursor:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN session_version INT NOT NULL DEFAULT 0")
            except Exception:
                pass                    # already there
            try:
                cursor.execute("""CREATE TABLE IF NOT EXISTS app_migrations (
                                      name VARCHAR(80) PRIMARY KEY, ran_at DATETIME NOT NULL)""")
                name = "2026-10-09-untrusted-google-passwords"
                cursor.execute("SELECT name FROM app_migrations WHERE name = %s", (name,))
                if not cursor.fetchone():
                    # Google accounts with a password (only the old sign-up form could add one), and password accounts
                    # whose owner has since signed in with Google (avatar_url), whose password may have been chosen by
                    # someone who registered the email first. Their owners keep "Continue with Google".
                    cursor.execute(
                        """UPDATE users SET password_hash = NULL, reset_token = NULL, reset_token_expires = NULL,
                                  session_version = session_version + 1
                           WHERE password_hash IS NOT NULL AND (auth_provider = 'google' OR avatar_url IS NOT NULL)""")
                    cursor.execute("INSERT INTO app_migrations (name, ran_at) VALUES (%s, %s)", (name, datetime.utcnow()))
                    print("[auth] one-time clean-up done: passwords added to Google accounts by the old sign-up form were removed")
            except Exception as e:
                print(f"[auth] WARNING: one-time clean-up not run: {e}")
    finally:
        conn.close()


_sv_cache = {}
_sv_lock = threading.Lock()


def _session_version(cursor, user_id):
    try:
        cursor.execute("SELECT session_version FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return int((row or {}).get("session_version") or 0)
    except Exception:
        return 0


def _start_session(user, cursor):
    session.permanent = True
    session["user_id"] = user["id"]
    session["email"] = user["email"]
    session["avatar_url"] = user.get("avatar_url")
    sv = user.get("session_version")
    session["sv"] = int(sv) if sv is not None else _session_version(cursor, user["id"])


def _bump_session_version(cursor, user_id):
    """Logs out every device on this account (each request compares the session's copy with this number)."""
    try:
        cursor.execute("UPDATE users SET session_version = session_version + 1 WHERE id = %s", (user_id,))
    except Exception as e:
        print(f"[auth] WARNING: could not end other sessions: {e}")
    with _sv_lock:
        _sv_cache.pop(user_id, None)


def session_still_valid(user_id, sv):
    """False when the account's password was reset (or the account is gone) after this session started.
    Cached for a minute per user; any database problem counts as valid, so an outage never logs people out."""
    now = time.time()
    with _sv_lock:
        hit = _sv_cache.get(user_id)
    if hit and now - hit[1] < SESSION_CHECK_SEC:
        current = hit[0]
    else:
        try:
            conn = get_db_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT session_version FROM users WHERE id = %s", (user_id,))
                    row = cursor.fetchone()
            finally:
                conn.close()
        except Exception:
            return True
        if row is None:
            return False
        current = int(row.get("session_version") or 0)
        with _sv_lock:
            _sv_cache[user_id] = (current, now)
    return int(sv or 0) >= current


def _base_url():
    """Public address for the links in emails: SITE_URL (or Railway's public domain) when set,
    otherwise the address of the current request."""
    site = (os.environ.get("SITE_URL") or "").strip().rstrip("/")
    if re.fullmatch(r"https://[A-Za-z0-9.\-:]+", site):
        return site
    dom = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip().rstrip("/")
    if re.fullmatch(r"[A-Za-z0-9.\-]+", dom):
        return "https://" + dom
    if not _warned_base:
        _warned_base.append(1)
        print("[auth] WARNING: set SITE_URL (https://your-domain) in Railway Variables; "
              "email links are using the request's address for now")
    return request.host_url.rstrip("/")


_warned_base = []


def _email_html(title, body_html, button_text, button_url, note_html=""):
    """Simple branded email (works in Gmail/Outlook: inline styles, one button, plain link as a fallback)."""
    url = html.escape(button_url, quote=True)
    return f"""
<div style="background:#eef4f1;padding:28px 12px;font-family:Segoe UI,Arial,sans-serif">
  <div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:14px;padding:28px 26px;color:#0d1b15">
    <p style="margin:0 0 18px;font-weight:700;font-size:18px;color:#0f8a50">Signals FM</p>
    <h1 style="margin:0 0 12px;font-size:21px;line-height:1.3;color:#0d1b15">{title}</h1>
    <div style="font-size:15px;line-height:1.6;color:#33463d">{body_html}</div>
    <p style="margin:22px 0"><a href="{url}" style="display:inline-block;background:#16a35f;color:#ffffff;text-decoration:none;font-weight:600;padding:12px 22px;border-radius:10px">{button_text}</a></p>
    <p style="font-size:13px;line-height:1.55;color:#6f8279;margin:0">If the button does not work, copy this link into your browser:<br>
      <a href="{url}" style="color:#0f8a50;word-break:break-all">{url}</a></p>
    {note_html}
  </div>
  <p style="max-width:520px;margin:14px auto 0;text-align:center;font-size:12px;color:#6f8279">Signals FM &middot; crypto signals with tested, honest results. Not financial advice.</p>
</div>"""


@auth_bp.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    if not EMAIL_RE.match(email):
        return jsonify({"error": "Please enter a valid email address"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
        return jsonify({"error": "Password must contain both letters and numbers"}), 400

    password_hash = generate_password_hash(password)
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=VERIFY_TOKEN_HOURS)

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, password_hash, email_verified FROM users WHERE email = %s", (email,)
            )
            existing = cursor.fetchone()

            # The sign-up form never changes an existing account: anyone can type any email here.
            # (Adding a password to a Google account goes through "Forgot password", whose link
            # only reaches the real owner of the inbox.)
            if existing and not existing.get("password_hash"):
                return jsonify({"error": "This email is already registered with Google. Use \"Continue with Google\" "
                                         "to log in, or \"Forgot password\" to add a password."}), 409
            if existing and not existing.get("email_verified"):
                return jsonify({"error": "This email is registered but not verified yet. Use \"Forgot password\" to get "
                                         "a new link: setting a new password also verifies your email."}), 409
            if existing:
                return jsonify({"error": "An account with this email already exists. Log in, or use \"Forgot password\"."}), 409

            cursor.execute(
                """INSERT INTO users
                   (email, password_hash, auth_provider, email_verified, verify_token, verify_token_expires)
                   VALUES (%s, %s, 'password', 0, %s, %s)""",
                (email, password_hash, token, expires),
            )
        verify_link = f"{_base_url()}/api/verify-email?token={token}"
        send_email(
            email,
            "Verify your email for Signals FM",
            _email_html(
                "Welcome to Signals FM",
                "<p style=\"margin:0\">Please confirm that this is your email address. Then you can log in.</p>",
                "Verify my email",
                verify_link,
                f"<p style=\"font-size:13px;color:#6f8279;margin:14px 0 0\">This link works for {VERIFY_TOKEN_HOURS} hours. "
                "If you did not sign up for Signals FM, you can ignore this email.</p>",
            ),
        )
        return jsonify({
            "message": f"Account created. We sent a verification link to {email}. Open it to finish "
                       "(check your spam folder too), then log in."
        }), 201
    finally:
        conn.close()


@auth_bp.route("/api/verify-email", methods=["GET"])
def verify_email():
    """Opening the link does not verify by itself: email scanners open links automatically, and the
    password on the account may not be the inbox owner's. The login page asks for the password with the
    token, and /api/login confirms the email only when both match."""
    token = request.args.get("token", "")
    if not token or not re.fullmatch(r"[A-Za-z0-9_\-]{20,100}", token):
        return redirect("/login?error=invalid_verify_token")

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, verify_token_expires FROM users WHERE verify_token = %s",
                (token,),
            )
            user = cursor.fetchone()
            if not user:
                return redirect("/login?error=invalid_verify_token")
            if user["verify_token_expires"] and user["verify_token_expires"] < datetime.utcnow():
                return redirect("/login?error=verify_token_expired")
        return redirect("/login?verify=" + token)
    finally:
        conn.close()


@auth_bp.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    verify_token = str(data.get("verify_token") or "")

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cols = "id, email, password_hash, email_verified, avatar_url, verify_token, verify_token_expires"
            try:        # session_version is read with the password, so a reset during this login still ends it
                cursor.execute(f"SELECT {cols}, session_version FROM users WHERE email = %s", (email,))
            except Exception:   # column not added yet (database was busy at startup)
                cursor.execute(f"SELECT {cols} FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()

            if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
                return jsonify({"error": "Incorrect email or password"}), 401

            verified_now = False
            if not user.get("email_verified"):
                # The link from the email (token) + this account's password together prove the inbox and the
                # password belong to the same person: only then is the email confirmed.
                exp = user.get("verify_token_expires")
                if (re.fullmatch(r"[A-Za-z0-9_\-]{20,100}", verify_token) and user.get("verify_token")
                        and secrets.compare_digest(verify_token, str(user["verify_token"]))
                        and not (exp and exp < datetime.utcnow())):
                    cursor.execute(
                        "UPDATE users SET email_verified = 1, verify_token = NULL, verify_token_expires = NULL WHERE id = %s",
                        (user["id"],),
                    )
                    verified_now = True
                else:
                    return jsonify({"error": "Please confirm your email first: open the link we sent you (check spam too) "
                                             "and log in on the page it opens. Link missing or expired? Use \"Forgot password\": "
                                             "setting a new password also confirms your email."}), 403

            # `permanent=True` makes Flask issue a real Max-Age/Expires cookie
            # (per PERMANENT_SESSION_LIFETIME in main.py) instead of a
            # browser-session-only cookie, so the user stays logged in after
            # closing the browser/tab, not just across page refreshes.
            _start_session(user, cursor)
    finally:
        conn.close()
    return jsonify({"message": "Email confirmed. Welcome to Signals FM!" if verified_now else "Login successful",
                    "email": user["email"]}), 200


@auth_bp.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out successfully"}), 200


@auth_bp.route("/api/me", methods=["GET"])
def me():
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401
    return jsonify({"email": session["email"]}), 200


@auth_bp.route("/api/forgot-password", methods=["POST"])
def forgot_password():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    generic_response = jsonify({
        "message": "If an account with that email exists, we sent a reset link. Check your spam folder too."
    }), 200

    if not email:
        return generic_response

    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_MINUTES)

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, password_hash FROM users WHERE email = %s", (email,)
            )
            user = cursor.fetchone()
            # Google accounts get the link too: it is how they add a password (only the inbox owner gets it).
            if not user:
                return generic_response

            cursor.execute(
                "UPDATE users SET reset_token = %s, reset_token_expires = %s WHERE id = %s",
                (token, expires, user["id"]),
            )
    finally:
        conn.close()

    reset_link = f"{_base_url()}/reset-password?token={token}"
    send_email(
        email,
        "Set a new password for Signals FM",
        _email_html(
            "Set a new password",
            "<p style=\"margin:0\">We received a request to set a new password for your Signals FM account. "
            "Setting it also confirms your email address.</p>",
            "Choose a new password",
            reset_link,
            f"<p style=\"font-size:13px;color:#6f8279;margin:14px 0 0\">This link works for {RESET_TOKEN_MINUTES} minutes. "
            "If you did not ask for this, you can ignore this email: your password stays the same.</p>",
        ),
    )
    return generic_response


@auth_bp.route("/api/reset-password", methods=["POST"])
def reset_password():
    data = request.get_json(silent=True) or {}
    token = data.get("token") or ""
    password = data.get("password") or ""

    if not token:
        return jsonify({"error": "Invalid or missing reset token"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
        return jsonify({"error": "Password must contain both letters and numbers"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, reset_token_expires FROM users WHERE reset_token = %s",
                (token,),
            )
            user = cursor.fetchone()
            if not user:
                return jsonify({"error": "Invalid or expired reset link"}), 400
            if user["reset_token_expires"] and user["reset_token_expires"] < datetime.utcnow():
                return jsonify({"error": "This reset link has expired. Please request a new one."}), 400

            password_hash = generate_password_hash(password)
            # The reset link reached this inbox, so the email address is proven: mark it verified
            # (this is also how someone whose verification link expired gets in).
            cursor.execute(
                """UPDATE users SET password_hash = %s, reset_token = NULL, reset_token_expires = NULL,
                          email_verified = 1, verify_token = NULL, verify_token_expires = NULL WHERE id = %s""",
                (password_hash, user["id"]),
            )
            # A new password logs out every other device (someone else may have had the old one).
            _bump_session_version(cursor, user["id"])
        return jsonify({"message": "Password saved. You can now log in. For your safety, every device was logged out."}), 200
    finally:
        conn.close()
