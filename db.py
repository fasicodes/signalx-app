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

            # Migration: email verification aur password reset ke liye
            # zaroori columns add karte hain (agar pehle se nahi hain).
            cursor.execute("SHOW COLUMNS FROM users LIKE 'email_verified'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN email_verified TINYINT(1) NOT NULL DEFAULT 0"
                )
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN verify_token VARCHAR(64) NULL"
                )
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN verify_token_expires DATETIME NULL"
                )
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN reset_token VARCHAR(64) NULL"
                )
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN reset_token_expires DATETIME NULL"
                )
                # Purane users (jo already bane the) auto-verified maan lete
                # hain taake unhe achanak lock out na hona pare.
                cursor.execute("UPDATE users SET email_verified = 1 WHERE email_verified = 0")
                print("[db] migrated: added email_verified/verify_token/reset_token columns")

            # Migration: Google profile picture store karne ke liye
            cursor.execute("SHOW COLUMNS FROM users LIKE 'avatar_url'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN avatar_url VARCHAR(500) NULL"
                )
                print("[db] migrated: added avatar_url column")

            # Active Trade Tracking system: active_trades + trade_events
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS active_trades (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    user_id INT NOT NULL,
                    asset VARCHAR(30) NOT NULL,
                    direction VARCHAR(10) NOT NULL,
                    entry_price DOUBLE NOT NULL,
                    position_size DOUBLE NOT NULL,
                    stop_loss DOUBLE NULL,
                    take_profit DOUBLE NULL,
                    leverage DOUBLE NULL,
                    holding_period_label VARCHAR(50) NULL,
                    holding_period_minutes INT NULL,
                    signal_snapshot TEXT NULL,
                    status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE',
                    last_price DOUBLE NULL,
                    estimated_pnl DOUBLE NULL,
                    estimated_pnl_percent DOUBLE NULL,
                    holding_period_notified TINYINT(1) NOT NULL DEFAULT 0,
                    near_sl_notified TINYINT(1) NOT NULL DEFAULT 0,
                    near_tp_notified TINYINT(1) NOT NULL DEFAULT 0,
                    setup_eval_notified TINYINT(1) NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP NULL,
                    exit_price DOUBLE NULL,
                    exit_reason VARCHAR(50) NULL,
                    INDEX idx_user_status (user_id, status),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_events (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    trade_id INT NOT NULL,
                    event_type VARCHAR(40) NOT NULL,
                    message TEXT NOT NULL,
                    price DOUBLE NULL,
                    pnl DOUBLE NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_trade (trade_id),
                    FOREIGN KEY (trade_id) REFERENCES active_trades(id) ON DELETE CASCADE
                )
                """
            )
            # Signal FM: Unified Trade Condition Detection columns
            # (state machine fields - see condition_engine.py)
            cursor.execute("SHOW COLUMNS FROM active_trades LIKE 'trade_condition'")
            if cursor.fetchone() is None:
                cursor.execute("ALTER TABLE active_trades ADD COLUMN trade_condition VARCHAR(40) NULL")
                cursor.execute("ALTER TABLE active_trades ADD COLUMN condition_message TEXT NULL")
                cursor.execute("ALTER TABLE active_trades ADD COLUMN condition_started_at DATETIME NULL")
                cursor.execute("ALTER TABLE active_trades ADD COLUMN condition_version INT NOT NULL DEFAULT 0")
                cursor.execute("ALTER TABLE active_trades ADD COLUMN pending_condition VARCHAR(40) NULL")
                cursor.execute("ALTER TABLE active_trades ADD COLUMN pending_condition_count INT NOT NULL DEFAULT 0")
                cursor.execute("ALTER TABLE active_trades ADD COLUMN worst_pnl_percent DOUBLE NULL")
                cursor.execute("ALTER TABLE active_trades ADD COLUMN best_pnl_percent DOUBLE NULL")
                cursor.execute("ALTER TABLE active_trades ADD COLUMN last_momentum_check DATETIME NULL")
                cursor.execute("ALTER TABLE active_trades ADD COLUMN momentum_snapshot TEXT NULL")
                print("[db] migrated: added Signal FM condition-engine columns to active_trades")

            # Performance Summary: qualifying-outcome classification, so
            # Total Profit/Loss/Win-Rate can be computed without guessing
            # from P&L sign alone. Values: TAKE_PROFIT, STOP_LOSS,
            # MANUAL_PROFIT, MANUAL_LOSS (set when a trade closes; NULL
            # for trades still ACTIVE or closed before this migration).
            cursor.execute("SHOW COLUMNS FROM active_trades LIKE 'outcome_class'")
            if cursor.fetchone() is None:
                cursor.execute("ALTER TABLE active_trades ADD COLUMN outcome_class VARCHAR(20) NULL")
                # Best-effort backfill for trades closed before this column
                # existed, using the same rules the app now applies going
                # forward, so old history isn't excluded from the summary.
                cursor.execute(
                    "UPDATE active_trades SET outcome_class = "
                    "CASE "
                    "  WHEN status='TARGET_REACHED' THEN 'TAKE_PROFIT' "
                    "  WHEN status='STOP_LOSS_REACHED' THEN 'STOP_LOSS' "
                    "  WHEN status='MANUALLY_CLOSED' AND estimated_pnl >= 0 THEN 'MANUAL_PROFIT' "
                    "  WHEN status='MANUALLY_CLOSED' AND estimated_pnl < 0 THEN 'MANUAL_LOSS' "
                    "  ELSE outcome_class "
                    "END "
                    "WHERE status != 'ACTIVE' AND outcome_class IS NULL"
                )
                print("[db] migrated: added outcome_class column to active_trades (+ backfilled existing closed trades)")

            # "User Mistake" widget: flags a MANUAL_LOSS trade where no
            # risk-warning condition (see condition_engine.RISK_WARNING_CONDITIONS)
            # was active when the user closed it - i.e. closed at a loss
            # without the system having said anything about elevated risk.
            # Purely informational: never affects Win Rate / qualifying
            # trade stats (MANUAL_LOSS is already excluded from those).
            cursor.execute("SHOW COLUMNS FROM active_trades LIKE 'is_user_mistake'")
            if cursor.fetchone() is None:
                cursor.execute("ALTER TABLE active_trades ADD COLUMN is_user_mistake TINYINT(1) NOT NULL DEFAULT 0")
                # Best-effort backfill for trades closed before this column
                # existed: a MANUAL_LOSS trade counts as a user mistake if
                # its locked trade_condition at close time was NOT one of
                # the risk-warning conditions.
                cursor.execute(
                    "UPDATE active_trades SET is_user_mistake = 1 "
                    "WHERE outcome_class = 'MANUAL_LOSS' "
                    "AND (trade_condition IS NULL OR trade_condition NOT IN "
                    "('HIGH_RISK_OPPOSITE_MOVE', 'STOP_LOSS_APPROACHING', 'SETUP_INVALIDATED'))"
                )
                print("[db] migrated: added is_user_mistake column to active_trades (+ backfilled existing closed trades)")

            # ------------------------------------------------------------
            # Phase 2 upgrade: Trade Journal (notes/tags/setup_type on the
            # EXISTING active_trades table - koi naya trades table nahi
            # banaya, taake active/closed trades aur unki journal entry
            # hamesha ek hi row rahe, koi data duplication na ho).
            # ------------------------------------------------------------
            cursor.execute("SHOW COLUMNS FROM active_trades LIKE 'notes'")
            if cursor.fetchone() is None:
                cursor.execute("ALTER TABLE active_trades ADD COLUMN notes TEXT NULL")
                # tags JSON array (text) ke tor par store hote hain, e.g. ["Breakout","Trend"]
                cursor.execute("ALTER TABLE active_trades ADD COLUMN tags TEXT NULL")
                cursor.execute("ALTER TABLE active_trades ADD COLUMN setup_type VARCHAR(50) NULL")
                cursor.execute("ALTER TABLE active_trades ADD COLUMN journal_updated_at DATETIME NULL")
                cursor.execute("ALTER TABLE active_trades ADD INDEX idx_user_setup (user_id, setup_type)")
                print("[db] migrated: added notes/tags/setup_type journal columns to active_trades")

            # ------------------------------------------------------------
            # Phase 1 upgrade: Watchlist, Alerts, Notifications
            # (see watchlist.py / alerts.py). Additive only - no existing
            # table/column is touched or removed.
            # ------------------------------------------------------------
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    user_id INT NOT NULL,
                    symbol VARCHAR(30) NOT NULL,
                    is_favorite TINYINT(1) NOT NULL DEFAULT 0,
                    sort_order INT NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_user_symbol (user_id, symbol),
                    INDEX idx_user (user_id),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    user_id INT NOT NULL,
                    symbol VARCHAR(30) NOT NULL,
                    alert_type VARCHAR(30) NOT NULL,
                    target_value DOUBLE NULL,
                    is_enabled TINYINT(1) NOT NULL DEFAULT 1,
                    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
                    last_checked_value DOUBLE NULL,
                    last_direction VARCHAR(10) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    triggered_at DATETIME NULL,
                    last_triggered_at DATETIME NULL,
                    INDEX idx_user (user_id),
                    INDEX idx_user_enabled (user_id, is_enabled),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    user_id INT NOT NULL,
                    category VARCHAR(20) NOT NULL,
                    symbol VARCHAR(30) NULL,
                    title VARCHAR(200) NOT NULL,
                    message TEXT NOT NULL,
                    is_read TINYINT(1) NOT NULL DEFAULT 0,
                    alert_id INT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user_read (user_id, is_read),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            print("[db] migrated: watchlist/alerts/notifications tables ready")

        print("[db] users table ready")
    finally:
        conn.close()
