"""
Google OAuth login SignalX ke liye.

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

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, redirect, session, url_for

from db import get_db_connection

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


def _find_or_create_oauth_user(email, provider, avatar_url=None):
    """Email se user dhoondta hai; agar nahi milta to naya OAuth user bana
    deta hai (password_hash NULL rehta hai). Google apni taraf se email
    already verify kar chuka hota hai, isliye email_verified = 1 rakhte hain.
    Google profile picture (avatar_url) bhi save/update karte hain."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, email FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if user:
                if avatar_url:
                    cursor.execute(
                        "UPDATE users SET avatar_url = %s WHERE id = %s",
                        (avatar_url, user["id"]),
                    )
                return user
            cursor.execute(
                "INSERT INTO users (email, password_hash, auth_provider, email_verified, avatar_url) VALUES (%s, NULL, %s, 1, %s)",
                (email, provider, avatar_url),
            )
            new_id = cursor.lastrowid
            return {"id": new_id, "email": email}
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
    token = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.google.parse_id_token(token)
    email = (userinfo.get("email") or "").lower()
    avatar_url = userinfo.get("picture")
    if not email:
        return redirect("/login?error=google_no_email")

    user = _find_or_create_oauth_user(email, "google", avatar_url)
    session.permanent = True
    session["user_id"] = user["id"]
    session["email"] = user["email"]
    session["avatar_url"] = avatar_url
    return redirect("/")
