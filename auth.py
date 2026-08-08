"""
Email/password auth routes for SignalX, MySQL ke sath.
Email hi primary identifier hai (username nahi).

Is file ko main.py mein register karna hai:

    from auth import auth_bp
    app.register_blueprint(auth_bp)

Routes:
    POST /api/register  -> { "email": "...", "password": "..." }
    POST /api/login      -> { "email": "...", "password": "..." }
    POST /api/logout
    GET  /api/me          -> current logged-in user batata hai (ya 401)
"""

import re

from flask import Blueprint, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
import pymysql

from db import get_db_connection

auth_bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@auth_bp.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    if not EMAIL_RE.match(email):
        return jsonify({"error": "Please enter a valid email address"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    password_hash = generate_password_hash(password)

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Pehle check karte hain ke is email se koi account pehle se
            # hai ya nahi (misal, Google se bana ho, password na ho).
            cursor.execute("SELECT id, password_hash FROM users WHERE email = %s", (email,))
            existing = cursor.fetchone()

            if existing and existing.get("password_hash"):
                # Account hai aur password bhi already set hai
                return jsonify({"error": "An account with this email already exists"}), 409

            if existing and not existing.get("password_hash"):
                # Account Google/X se bana tha, ab isi email par password
                # bhi link kar dete hain (jaise Instagram/Facebook karte hain)
                cursor.execute(
                    "UPDATE users SET password_hash = %s WHERE id = %s",
                    (password_hash, existing["id"]),
                )
                return jsonify({"message": "Password added to your account", "email": email}), 200

            # Bilkul naya account
            cursor.execute(
                "INSERT INTO users (email, password_hash, auth_provider) VALUES (%s, %s, 'password')",
                (email, password_hash),
            )
        return jsonify({"message": "Account created successfully", "email": email}), 201
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
                "SELECT id, email, password_hash FROM users WHERE email = %s",
                (email,),
            )
            user = cursor.fetchone()
    finally:
        conn.close()

    if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Incorrect email or password"}), 401

    session["user_id"] = user["id"]
    session["email"] = user["email"]
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
