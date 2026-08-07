"""
MySQL connection helper for SignalX.

Connection settings aate hain environment variables se (.env ya server
config), taake password code mein hardcode na ho. Agar env var set nahi hai
to neeche wali default values use hoti hain (local development ke liye).
"""

import os
import pymysql
import pymysql.cursors


def get_db_connection():
    """Naya MySQL connection return karta hai. Har request ke baad connection
    close karna zaroori hai (routes mein `with` block use karein)."""
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "signalx_db"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def init_db():
    """App start hote hi 'users' table khud ba khud bana deta hai agar
    pehle se maujood nahi hai. Isse manually SQL chalane ki zaroorat
    nahi rehti (Railway wagera par jahan query console na ho)."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    username VARCHAR(50) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        print("[db] users table ready")
    finally:
        conn.close()
