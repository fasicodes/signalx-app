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
    pehle se maujood nahi hai. Email primary identifier hai. password_hash
    optional hai (Google/X se login karne walon ka koi password nahi hota).
    Isse manually SQL chalane ki zaroorat nahi rehti."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NULL,
                    auth_provider VARCHAR(20) NOT NULL DEFAULT 'password',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Migration: agar purani table 'username' column ke sath ban
            # chuki thi (naye email-based design se pehle), usay email-based
            # structure mein badal dete hain.
            cursor.execute("SHOW COLUMNS FROM users LIKE 'username'")
            has_username = cursor.fetchone() is not None
            cursor.execute("SHOW COLUMNS FROM users LIKE 'email'")
            has_email = cursor.fetchone() is not None

            if has_username and not has_email:
                cursor.execute(
                    "ALTER TABLE users CHANGE username email VARCHAR(255) UNIQUE NOT NULL"
                )
                print("[db] migrated: username column -> email column")

            # Migration: agar password_hash NOT NULL tha (purana schema),
            # usay nullable banate hain taake OAuth users bhi save ho sakein.
            cursor.execute("SHOW COLUMNS FROM users LIKE 'auth_provider'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE users MODIFY password_hash VARCHAR(255) NULL"
                )
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN auth_provider VARCHAR(20) NOT NULL DEFAULT 'password'"
                )
                print("[db] migrated: added auth_provider column, password_hash now nullable")
        print("[db] users table ready")
    finally:
        conn.close()
