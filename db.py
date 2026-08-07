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
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "signalx_db"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )
