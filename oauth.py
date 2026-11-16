"""
Google OAuth login Signals FM ke liye.

Zaroori environment variables (Railway mein set karni hain):
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET

Callback URL (isay Google Cloud Console mein "Authorized redirect URI"
ke tor par register karna hai):
    https://<your-domain>/api/auth/google/callback

Is file ko main.py mein register karna hai:
    from oauth import oauth_bp, init_oauth
    init_oauth(app)
    app.register_blueprint(oauth_bp)
"""

import os
import time

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, redirect, request, session, url_for

from db import get_db_connection
from auth import _bump_session_version, _session_version

oauth_bp = Blueprint("oauth", __name__)
oauth = OAuth()


def init_oauth(app):
    oauth.init_app(app)

    if os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
        oauth.register(
            name="google",
            client_id=os.environ.get("GOOGLE_CLIENT_ID"),
            client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    else:
        print("[oauth] WARNING: GOOGLE_CLIENT_ID/SECRET not set - Google login disabled")


def _track(fn, *args):
    """Records an event for the admin panel (admin_tracking.py). Never breaks a Google login."""
    try:
        import admin_tracking
        getattr(admin_tracking, fn)(*args)
    except Exception as e:
        print(f"[oauth] activity record skipped ({fn}): {type(e).__name__}")


def _find_or_create_oauth_user(email, provider, avatar_url=None, full_name=None):
    """Email se user dhoondta hai; agar nahi milta to naya OAuth user bana
    deta hai (password_hash NULL rehta hai). Google apni taraf se email
    already verify kar chuka hota hai, isliye email_verified = 1 rakhte hain.
    Google profile picture (avatar_url) aur naam (full_name, admin panel ke liye) bhi save/update karte hain."""
    full_name = (str(full_name or "").strip()[:120]) or None
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            try:            # account_status: a suspended account may not log in (admin panel)
                cursor.execute("SELECT id, email, email_verified, account_status FROM users WHERE email = %s", (email,))
            except Exception:
                cursor.execute("SELECT id, email, email_verified FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if user:
                if not user.get("email_verified"):
                    # Someone signed up with this email and a password but never proved the inbox is theirs.
                    # Google has now proved who owns it: mark it verified and drop that unproven password,
                    # so nobody else can log in to this account with it (and log out any session that used it).
                    cursor.execute(
                        """UPDATE users SET email_verified = 1, password_hash = NULL,
                                  verify_token = NULL, verify_token_expires = NULL WHERE id = %s""",
                        (user["id"],),
                    )
                    _bump_session_version(cursor, user["id"])
                user["sv"] = _session_version(cursor, user["id"])
                if avatar_url:
                    cursor.execute(
                        "UPDATE users SET avatar_url = %s WHERE id = %s",
                        (avatar_url, user["id"]),
                    )
                if full_name:
                    try:
                        cursor.execute("UPDATE users SET full_name = %s WHERE id = %s", (full_name, user["id"]))
                    except Exception:
                        pass        # column not added yet
                return user
            cursor.execute(
                "INSERT INTO users (email, password_hash, auth_provider, email_verified, avatar_url) VALUES (%s, NULL, %s, 1, %s)",
                (email, provider, avatar_url),
            )
            new_id = cursor.lastrowid
            if full_name:
                try:
                    cursor.execute("UPDATE users SET full_name = %s WHERE id = %s", (full_name, new_id))
                except Exception:
                    pass
            _track("note_register", new_id, email, provider)
            return {"id": new_id, "email": email, "sv": 0}
    finally:
        conn.close()


# ---------------------------------------------------------------- Google ---

@oauth_bp.route("/api/auth/google/login")
def google_login():
    if "google" not in oauth._clients:
        return redirect("/login?error=google_not_configured")
    redirect_uri = url_for("oauth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@oauth_bp.route("/api/auth/google/callback")
def google_callback():
    pending = session.get("gmail_connect")
    if isinstance(pending, dict) and request.args.get("state") and request.args.get("state") == pending.get("state"):
        return _finish_gmail_connect(pending)       # "Connect Gmail" on the admin page comes back here too
    try:
        google = oauth.google
    except AttributeError:          # Google login is not configured on this server
        return redirect("/login?error=google_not_configured")
    try:
        token = google.authorize_access_token()
        userinfo = token.get("userinfo") or google.parse_id_token(token)
    except Exception as e:
        # The person pressed "Cancel" at Google, the link expired, or the state did not match.
        print(f"[oauth] Google sign-in did not complete: {type(e).__name__}")
        return redirect("/login?error=google_failed")
    email = (userinfo.get("email") or "").lower()
    avatar_url = userinfo.get("picture")
    if not email:
        return redirect("/login?error=google_no_email")
    if userinfo.get("email_verified") is not True:
        # Only an address Google has verified may open (or create) an account with that email.
        return redirect("/login?error=google_email_unverified")

    user = _find_or_create_oauth_user(email, "google", avatar_url, userinfo.get("name"))
    if (user.get("account_status") or "active") == "blocked":       # suspended by the admin
        return redirect("/login?error=account_suspended")
    session.permanent = True
    session["user_id"] = user["id"]
    session["email"] = user["email"]
    session["avatar_url"] = avatar_url
    session["sv"] = user.get("sv", 0)
    session["login_method"] = "google"
    _track("note_login", user["id"], user["email"], "google")
    return redirect("/")


def _finish_gmail_connect(pending):
    """The end of "Connect Gmail" (started on /admin/news, see main.py and mailer.py). It uses the Google login's
    return address, so nothing new has to be registered with Google. Nobody is logged in or out here."""
    import mailer
    session.pop("gmail_connect", None)
    if ((session.get("email") or "").strip().lower() != pending.get("email")
            or time.time() - float(pending.get("at") or 0) > 1800):
        ok, text = False, "The Gmail connection was started in another login or too long ago. Press Connect Gmail again."
    elif request.args.get("error"):
        ok, text = False, "Gmail was not connected: the Google page was closed or the permission was not given."
    else:
        ok, text = mailer.gmail_finish(request.args.get("code") or "",
                                       pending.get("redirect_uri") or url_for("oauth.google_callback", _external=True))
    session["gmail_connect_msg"] = {"ok": ok, "text": text}
    return redirect("/admin/news?gmail=" + ("connected" if ok else "failed"))
