"""
Simple username/password auth routes for SignalX, MySQL ke sath.

Is file ko main.py mein register karna hai:

    from auth import auth_bp
    app.register_blueprint(auth_bp)

Routes:
    POST /register  -> { "username": "...", "password": "..." }
    POST /login      -> { "username": "...", "password": "..." }
    POST /logout
    GET  /me          -> current logged-in user batata hai (ya 401)
"""

from flask import Blueprint, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
import pymysql

from db import get_db_connection

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "username aur password dono zaroori hain"}), 400
    if len(password) < 6:
        return jsonify({"error": "password kam se kam 6 characters ka ho"}), 400

    password_hash = generate_password_hash(password)

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            try:
                cursor.execute(
                    "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                    (username, password_hash),
                )
            except pymysql.err.IntegrityError:
                return jsonify({"error": "Ye username pehle se maujood hai"}), 409
        return jsonify({"message": "Account ban gaya", "username": username}), 201
    finally:
        conn.close()


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, password_hash FROM users WHERE username = %s",
                (username,),
            )
            user = cursor.fetchone()
    finally:
        conn.close()

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Ghalat username ya password"}), 401

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    return jsonify({"message": "Login successful", "username": user["username"]}), 200


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logout ho gaya"}), 200


@auth_bp.route("/me", methods=["GET"])
def me():
    if "user_id" not in session:
        return jsonify({"error": "Login nahi hain"}), 401
    return jsonify({"username": session["username"]}), 200
