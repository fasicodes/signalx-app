"""
Google aur X (Twitter) OAuth login SignalX ke liye.

Zaroori environment variables (Railway mein set karni hain):
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    X_CLIENT_ID
    X_CLIENT_SECRET

Callback URLs (inhe Google Cloud Console / X Developer Portal mein
"Authorized redirect URI" ke tor par register karna hai):
    https://<your-domain>/api/auth/google/callback
    https://<your-domain>/api/auth/x/callback

Is file ko main.py mein register karna hai:
    from oauth import oauth_bp, init_oauth
    init_oauth(app)
    app.register_blueprint(oauth_bp)
"""

import os

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, redirect, session, url_for, request

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

    if os.environ.get("X_CLIENT_ID") and os.environ.get("X_CLIENT_SECRET"):
        oauth.register(
            name="x",
            client_id=os.environ.get("X_CLIENT_ID"),
            client_secret=os.environ.get("X_CLIENT_SECRET"),
            access_token_url="https://api.twitter.com/2/oauth2/token",
            authorize_url="https://twitter.com/i/oauth2/authorize",
            api_base_url="https://api.twitter.com/2/",
            client_kwargs={
                "scope": "tweet.read users.read offline.access",
                "code_challenge_method": "S256",
            },
        )
    else:
        print("[oauth] WARNING: X_CLIENT_ID/SECRET not set - X login disabled")


def _find_or_create_oauth_user(email, provider):
    """Email se user dhoondta hai; agar nahi milta to naya OAuth user bana
    deta hai (password_hash NULL rehta hai)."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, email FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if user:
                return user
            cursor.execute(
                "INSERT INTO users (email, password_hash, auth_provider) VALUES (%s, NULL, %s)",
                (email, provider),
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
    if not email:
        return redirect("/login?error=google_no_email")

    user = _find_or_create_oauth_user(email, "google")
    session["user_id"] = user["id"]
    session["email"] = user["email"]
    return redirect("/")


# -------------------------------------------------------------------- X ---

@oauth_bp.route("/api/auth/x/login")
def x_login():
    if "x" not in oauth._clients:
        return redirect("/login?error=x_not_configured")
    redirect_uri = url_for("oauth.x_callback", _external=True)
    return oauth.x.authorize_redirect(redirect_uri)


@oauth_bp.route("/api/auth/x/callback")
def x_callback():
    token = oauth.x.authorize_access_token()
    # X ka /2/users/me endpoint email nahi deta by default (X email scope
    # allow-list par hai) - isliye X user-id ko hi hamara "email" jaisa
    # unique identifier bana dete hain agar asal email na mile.
    resp = oauth.x.get("users/me", token=token)
    profile = resp.json().get("data", {})
    x_user_id = profile.get("id")
    username = profile.get("username", x_user_id)

    if not x_user_id:
        return redirect("/login?error=x_failed")

    pseudo_email = f"{username}@x.local"
    user = _find_or_create_oauth_user(pseudo_email, "x")
    session["user_id"] = user["id"]
    session["email"] = user["email"]
    return redirect("/")
