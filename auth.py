"""
Email/password auth routes for SignalX, MySQL ke sath.
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

import re
import secrets
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


def _base_url():
    """Request se app ka base URL nikalta hai (email links banane ke liye)."""
    return request.host_url.rstrip("/")


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
                "SELECT id, password_hash FROM users WHERE email = %s", (email,)
            )
            existing = cursor.fetchone()

            if existing and existing.get("password_hash"):
                return jsonify({"error": "An account with this email already exists"}), 409

            if existing and not existing.get("password_hash"):
                # Account Google se bana tha, ab password bhi link kar dete hain
                cursor.execute(
                    "UPDATE users SET password_hash = %s WHERE id = %s",
                    (password_hash, existing["id"]),
                )
                return jsonify({"message": "Password added to your account", "email": email}), 200

            cursor.execute(
                """INSERT INTO users
                   (email, password_hash, auth_provider, email_verified, verify_token, verify_token_expires)
                   VALUES (%s, %s, 'password', 0, %s, %s)""",
                (email, password_hash, token, expires),
            )
        verify_link = f"{_base_url()}/api/verify-email?token={token}"
        send_email(
            email,
            "Verify your SignalX account",
            f"""
            <p>Welcome to SignalX!</p>
            <p>Please confirm your email address by clicking the link below:</p>
            <p><a href="{verify_link}">Verify my email</a></p>
            <p>This link expires in {VERIFY_TOKEN_HOURS} hours.</p>
            """,
        )
        return jsonify({
            "message": "Account created. Please check your email to verify your account before logging in."
        }), 201
    finally:
        conn.close()


@auth_bp.route("/api/verify-email", methods=["GET"])
def verify_email():
    token = request.args.get("token", "")
    if not token:
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

            cursor.execute(
                "UPDATE users SET email_verified = 1, verify_token = NULL, verify_token_expires = NULL WHERE id = %s",
                (user["id"],),
            )
        return redirect("/login?verified=1")
    finally:
        conn.close()


@auth_bp.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, email, password_hash, email_verified, avatar_url FROM users WHERE email = %s",
                (email,),
            )
            user = cursor.fetchone()
    finally:
        conn.close()

    if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Incorrect email or password"}), 401

    if not user.get("email_verified"):
        return jsonify({"error": "Please verify your email before logging in. Check your inbox for the verification link."}), 403

    session["user_id"] = user["id"]
    session["email"] = user["email"]
    session["avatar_url"] = user.get("avatar_url")
    return jsonify({"message": "Login successful", "email": user["email"]}), 200


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
        "message": "If an account with that email exists, a reset link has been sent."
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
            # Google-only accounts (no password_hash) don't get a reset link
            if not user or not user.get("password_hash"):
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
        "Reset your SignalX password",
        f"""
        <p>We received a request to reset your password.</p>
        <p><a href="{reset_link}">Click here to reset your password</a></p>
        <p>This link expires in {RESET_TOKEN_MINUTES} minutes. If you didn't request this, you can ignore this email.</p>
        """,
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
            cursor.execute(
                "UPDATE users SET password_hash = %s, reset_token = NULL, reset_token_expires = NULL WHERE id = %s",
                (password_hash, user["id"]),
            )
        return jsonify({"message": "Password reset successfully. You can now log in."}), 200
    finally:
        conn.close()
