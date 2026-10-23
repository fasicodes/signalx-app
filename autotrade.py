"""
Auto-Trade Bot (Binance) - SignalX / Signals FM
================================================

Turns the old HTML auto-trade mockup into a real, risk-controlled bot.

  1. Each user has THREE bot accounts: "paper" (the site's own Demo trading
     account, papertrade.py: no keys, no Binance, works right away), "demo"
     (Binance Demo Trading, no real money) and "live" (real funds). Each
     account has its own bot switch, settings, positions, history and log,
     so all bots can run at the same time, independently.
  2. API keys are stored ENCRYPTED on the server (never in the browser).
  3. Real balances, prices and market orders via ccxt.
  4. Trade decisions come from SignalX's own signal engine
     (generate_signal -> final_verdict + confidence_pct), never random.
  5. Every trade gets a Stop-Loss + Take-Profit, risk-based position size,
     max open positions, a daily loss limit and a leverage cap.
  6. A background engine monitors positions and closes them when SL/TP is
     hit. On futures a "backup stop" order is also placed on Binance so
     losses stay limited even if the server goes down.
  7. Emergency "Stop all": bot off + every open position closed.

ENVIRONMENT VARIABLES
  AUTOTRADE_ENCRYPTION_KEY  (required) Fernet key. Generate with:
      python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
      Never change it later, or saved API keys can no longer be decrypted.
  AUTOTRADE_ENGINE          (optional) "off" disables the background engine.
  AUTOTRADE_MONITOR_SEC     (optional) position check interval, default 10.

LIMITATIONS
  - Signals come from the site's signal engine (walk-forward tested, see
    signal_model.json). A positive test result is not a guarantee of profit.
  - Run on the Demo account for a few weeks before going live.
  - Spot = LONG only. SHORT requires USDT-M futures.
  - Spot stop-losses are watched by the bot (checked every few seconds), not
    placed on Binance. If the server is down, a spot position has no stop.
    Futures positions also get a backup stop on Binance.
  - An entry order is never re-sent blindly after a timeout: the bot first
    asks Binance whether the order exists (see _place_entry).
"""

import json
import math
import re
import os
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from db import get_db_connection
import fxbroker
import markets

try:  # already in requirements.txt
    import ccxt
except ImportError:  # pragma: no cover
    ccxt = None

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover
    Fernet = None
    InvalidToken = Exception


autotrade_bp = Blueprint("autotrade", __name__)

# ---------------------------------------------------------------------------
# Hard safety limits (enforced server-side)
# ---------------------------------------------------------------------------
MODES = ("paper", "demo", "live", "oanda_demo", "oanda_live")
LIVE_MODES = ("live", "oanda_live")            # real money: explicit confirmation needed
FX_MODES = ("oanda_demo", "oanda_live")        # OANDA accounts (forex only)
BINANCE_MODES = ("demo", "live")
MARKETS = ("crypto", "forex")
SIGNAL_TIMEFRAMES = ("15m", "1h", "4h")
GRADES = ("A", "B", "C")
ENTRY_WINDOW_BARS = 2        # the bot takes a signal up to 2 candles of its timeframe after it starts
MAX_ENTRY_DRIFT_R = 0.35     # ...while the price is still within 0.35 R of the signal's entry
RETRY_BACKOFF_SEC = (20, 40, 80, 160, 320)   # waits between attempts on a retryable problem
SL_MIN_PCT_SIGNAL = {"crypto": 0.15, "forex": 0.02}   # floor for a stop that comes from a site signal
ALLOWED_LEVERAGE = (1, 2, 3, 5, 10, 20)
TIMEFRAME_SCAN_SEC = {"15m": 180, "1h": 300, "4h": 600}
TIMEFRAME_MINUTES = {"15m": 15, "1h": 60, "4h": 240}
MAX_COINS = 20          # coins one bot account can watch (signals come for every coin in the list)
SL_MIN_PCT = 0.4           # stop-loss at least 0.4% away
SL_MAX_PCT = 15.0          # and at most 15% (engine v3 stops are 2 x ATR on 4h candles)
SL_FALLBACK_PCT = 1.5      # used when the signal has no volatility figure
LIQ_SAFETY_FRACTION = 0.6  # SL must sit within 60% of the liquidation distance
BACKUP_STOP_EXTRA = 0.25   # futures backup stop sits 25% beyond the bot SL
LOG_RETENTION_DAYS = 14

DEFAULT_COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
FALLBACK_COIN_CHOICES = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT", "DOGE/USDT",
    "ADA/USDT", "AVAX/USDT", "LINK/USDT", "SUI/USDT", "NEAR/USDT", "LTC/USDT",
    "DOT/USDT", "TRX/USDT", "BCH/USDT", "ATOM/USDT", "UNI/USDT", "FIL/USDT",
]

DEFAULT_SETTINGS = {
    "enabled": 0,
    "market_type": "spot",
    "leverage": 1,
    "risk_pct": 1.0,
    "max_position_usdt": 100.0,
    "max_open_positions": 2,
    "daily_loss_limit_pct": 5.0,
    "min_confidence": 65.0,
    "timeframe": "1h",
    "reward_risk": 1.5,
    "coins": list(DEFAULT_COINS),
    "markets": ["crypto", "forex"],
    "timeframes": ["15m", "1h", "4h"],
    "min_grade": "B",
    "symbols_mode": "all",
}

ENGINE_LOCK_NAME = "signalx_autotrade_engine"
MONITOR_INTERVAL_SEC = max(5, int(os.environ.get("AUTOTRADE_MONITOR_SEC", "10") or 10))

# Injected from main.py (avoids a circular import)
_hooks = {
    "signal_feed": None,      # signal_hub.bot_feed: active site signals still open for entry
    "signal_get": None,       # signal_hub.signal_by_id
    "forex_price": None,      # live forex price (main._live_price_cached)
    "get_candles": None,
    "generate_signal": None,
    "engine_signal": None,
    "data_exchange": None,
    "available_coins": None,
}


# ===========================================================================
# Small helpers
# ===========================================================================
def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def _iso(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt if dt.endswith("Z") else dt.replace(" ", "T") + "Z"
    return dt.isoformat() + "Z"


def _to_dt(value):
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        return None


def _f(value, default=None):
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except (TypeError, ValueError):
        return default


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _user_id():
    return session.get("user_id")


def _err(message, status=400, **extra):
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return jsonify(payload), status


def _label(mode):
    return {"live": "Live", "demo": "Binance Demo", "paper": "Site demo", "oanda_demo": "OANDA demo",
            "oanda_live": "OANDA live"}.get(mode, "Demo")


class _Cursor:
    """`with _Cursor() as cur:` -> fresh connection + cursor, closed on exit."""

    def __enter__(self):
        self.conn = get_db_connection()
        self.cur = self.conn.cursor()
        return self.cur

    def __exit__(self, exc_type, exc, tb):
        try:
            self.cur.close()
        finally:
            self.conn.close()
        return False


def coin_choices():
    coins = _hooks.get("available_coins") or FALLBACK_COIN_CHOICES
    return [c for c in coins if isinstance(c, str) and c.endswith("/USDT")]


# ===========================================================================
# Encryption (API keys)
# ===========================================================================
def _fernet():
    key = (os.environ.get("AUTOTRADE_ENCRYPTION_KEY") or "").strip()
    if not key or Fernet is None:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        return None


def encryption_ready():
    return _fernet() is not None


def _encrypt(text):
    f = _fernet()
    if f is None:
        raise RuntimeError("AUTOTRADE_ENCRYPTION_KEY is not set.")
    return f.encrypt(text.encode()).decode()


def _decrypt(token):
    f = _fernet()
    if f is None:
        raise RuntimeError("AUTOTRADE_ENCRYPTION_KEY is not set.")
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        raise RuntimeError("Saved API keys could not be decrypted (was the encryption key changed?). "
                           "Please reconnect Binance.")


# ===========================================================================
# Database tables
# ===========================================================================
def init_tables():
    with _Cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS autotrade_exchange_accounts (
                user_id INT NOT NULL,
                mode VARCHAR(10) NOT NULL,
                exchange VARCHAR(20) NOT NULL DEFAULT 'binance',
                api_key_enc TEXT NOT NULL,
                api_secret_enc TEXT NOT NULL,
                key_hint VARCHAR(16) NULL,
                balance_usdt DOUBLE NULL,
                balance_total_usdt DOUBLE NULL,
                balance_market VARCHAR(10) NULL,
                balance_at DATETIME NULL,
                connected_at DATETIME NULL,
                PRIMARY KEY (user_id, mode),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS autotrade_bot_settings (
                user_id INT NOT NULL,
                mode VARCHAR(10) NOT NULL,
                enabled TINYINT(1) NOT NULL DEFAULT 0,
                market_type VARCHAR(10) NOT NULL DEFAULT 'spot',
                leverage INT NOT NULL DEFAULT 1,
                risk_pct DOUBLE NOT NULL DEFAULT 1,
                max_position_usdt DOUBLE NOT NULL DEFAULT 100,
                max_open_positions INT NOT NULL DEFAULT 2,
                daily_loss_limit_pct DOUBLE NOT NULL DEFAULT 5,
                min_confidence DOUBLE NOT NULL DEFAULT 65,
                timeframe VARCHAR(5) NOT NULL DEFAULT '1h',
                reward_risk DOUBLE NOT NULL DEFAULT 1.5,
                coins TEXT NULL,
                paused_until DATETIME NULL,
                pause_reason VARCHAR(255) NULL,
                day_ref_date VARCHAR(10) NULL,
                day_ref_balance DOUBLE NULL,
                last_scan_at DATETIME NULL,
                updated_at DATETIME NULL,
                PRIMARY KEY (user_id, mode),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS autotrade_positions (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                mode VARCHAR(10) NOT NULL,
                market_type VARCHAR(10) NOT NULL,
                symbol VARCHAR(30) NOT NULL,
                side VARCHAR(5) NOT NULL,
                source VARCHAR(10) NOT NULL DEFAULT 'BOT',
                amount DOUBLE NOT NULL,
                entry_price DOUBLE NOT NULL,
                stop_loss DOUBLE NOT NULL,
                take_profit DOUBLE NOT NULL,
                leverage INT NOT NULL DEFAULT 1,
                notional_usdt DOUBLE NOT NULL,
                fee_rate DOUBLE NOT NULL DEFAULT 0.001,
                confidence DOUBLE NULL,
                status VARCHAR(10) NOT NULL DEFAULT 'OPEN',
                entry_order_id VARCHAR(64) NULL,
                backup_stop_id VARCHAR(64) NULL,
                last_price DOUBLE NULL,
                unrealized_pnl DOUBLE NULL,
                exit_price DOUBLE NULL,
                pnl_usdt DOUBLE NULL,
                close_reason VARCHAR(20) NULL,
                tp1 DOUBLE NULL,
                tp2 DOUBLE NULL,
                hits INT NOT NULL DEFAULT 0,
                initial_amount DOUBLE NULL,
                initial_stop DOUBLE NULL,
                realized_usdt DOUBLE NOT NULL DEFAULT 0,
                opened_at DATETIME NOT NULL,
                closed_at DATETIME NULL,
                status_changed_at DATETIME NULL,
                INDEX idx_at_user_status (user_id, status),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS autotrade_logs (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                mode VARCHAR(10) NULL,
                level VARCHAR(10) NOT NULL,
                message VARCHAR(500) NOT NULL,
                created_at DATETIME NOT NULL,
                INDEX idx_atl_user_created (user_id, created_at),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS autotrade_engine (
                id INT PRIMARY KEY,
                heartbeat_at DATETIME NULL,
                host VARCHAR(100) NULL
            )
            """
        )
    _migrate_v1()
    _migrate_targets()
    _migrate_bridge()
    print("[autotrade] tables ready")


TARGET_COLUMNS = (("tp1", "DOUBLE NULL"), ("tp2", "DOUBLE NULL"), ("hits", "INT NOT NULL DEFAULT 0"),
                  ("initial_amount", "DOUBLE NULL"), ("initial_stop", "DOUBLE NULL"),
                  ("realized_usdt", "DOUBLE NOT NULL DEFAULT 0"))


def _migrate_targets():
    """Signal engine v3: positions keep three targets (TP1, TP2 + take_profit = TP3), how many were
    reached, the size at the start and the profit already taken by partial closes."""
    for col, ddl in TARGET_COLUMNS:
        with _Cursor() as cur:
            try:
                cur.execute(f"SELECT {col} FROM autotrade_positions LIMIT 1")
                cur.fetchall()
            except Exception:
                cur.execute(f"ALTER TABLE autotrade_positions ADD COLUMN {col} {ddl}")
                print(f"[autotrade] migrated: autotrade_positions.{col} added")


BRIDGE_SETTING_COLUMNS = (("markets", "TEXT NULL"), ("timeframes", "TEXT NULL"), ("min_grade", "VARCHAR(1) NULL"),
                          ("symbols_mode", "VARCHAR(8) NULL"))
BRIDGE_POSITION_COLUMNS = (("signal_id", "INT NULL"), ("timeframe", "VARCHAR(5) NULL"), ("market", "VARCHAR(10) NULL"),
                           ("fx_rate", "DOUBLE NULL"))


def _add_columns(table, columns):
    for col, ddl in columns:
        with _Cursor() as cur:
            try:
                cur.execute(f"SELECT {col} FROM {table} LIMIT 1")
                cur.fetchall()
            except Exception:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                print(f"[autotrade] migrated: {table}.{col} added")


def _migrate_bridge():
    """Site signals -> bot: filters per account, the signal a position came from, and one row per
    (account, signal) so a signal is never ordered twice. Adds columns/tables only; no data changes."""
    _add_columns("autotrade_bot_settings", BRIDGE_SETTING_COLUMNS)
    _add_columns("autotrade_positions", BRIDGE_POSITION_COLUMNS)
    with _Cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS autotrade_signal_orders (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                mode VARCHAR(12) NOT NULL,
                signal_id INT NOT NULL,
                status VARCHAR(10) NOT NULL,
                reason VARCHAR(300) NULL,
                attempts INT NOT NULL DEFAULT 0,
                next_try_at DATETIME NULL,
                position_id INT NULL,
                source VARCHAR(10) NOT NULL DEFAULT 'BOT',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE (user_id, mode, signal_id)
            )
            """
        )


def _migrate_v1():
    """The first release kept one account + one settings row per user
    (tables autotrade_accounts / autotrade_settings, no logs.mode column).
    Copy that data into the per-account tables once, then rename the old
    tables so the copy never runs again."""
    with _Cursor() as cur:
        try:
            cur.execute("SELECT mode FROM autotrade_logs LIMIT 1")
            cur.fetchall()
        except Exception:
            cur.execute("ALTER TABLE autotrade_logs ADD COLUMN mode VARCHAR(10) NULL")
            print("[autotrade] migrated: autotrade_logs.mode added")

    with _Cursor() as cur:
        try:
            cur.execute("SELECT * FROM autotrade_accounts")
            old_accounts = cur.fetchall() or []
        except Exception:
            return  # no v1 tables -> nothing to migrate
        try:
            cur.execute("SELECT * FROM autotrade_settings")
            old_settings = {r["user_id"]: r for r in (cur.fetchall() or [])}
        except Exception:
            old_settings = {}

        account_mode = {}
        for a in old_accounts:
            mode = a.get("mode") if a.get("mode") in MODES else "demo"
            account_mode[a["user_id"]] = mode
            cur.execute("SELECT user_id FROM autotrade_exchange_accounts WHERE user_id=%s AND mode=%s",
                        (a["user_id"], mode))
            if cur.fetchone() is None:
                cur.execute(
                    """INSERT INTO autotrade_exchange_accounts (user_id, mode, exchange, api_key_enc, api_secret_enc,
                       key_hint, balance_usdt, balance_total_usdt, balance_market, balance_at, connected_at)
                       VALUES (%s,%s,'binance',%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (a["user_id"], mode, a["api_key_enc"], a["api_secret_enc"], a.get("key_hint"),
                     a.get("balance_usdt"), a.get("balance_total_usdt"), a.get("balance_market"),
                     a.get("balance_at"), a.get("connected_at")),
                )
        for user_id, s in old_settings.items():
            mode = account_mode.get(user_id, "demo")
            cur.execute("SELECT user_id FROM autotrade_bot_settings WHERE user_id=%s AND mode=%s", (user_id, mode))
            if cur.fetchone() is None:
                cur.execute(
                    """INSERT INTO autotrade_bot_settings (user_id, mode, enabled, market_type, leverage, risk_pct,
                       max_position_usdt, max_open_positions, daily_loss_limit_pct, min_confidence, timeframe,
                       reward_risk, coins, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (user_id, mode, s.get("enabled") or 0, s.get("market_type") or "spot", s.get("leverage") or 1,
                     s.get("risk_pct") or 1, s.get("max_position_usdt") or 100, s.get("max_open_positions") or 2,
                     s.get("daily_loss_limit_pct") or 5, s.get("min_confidence") or 65, s.get("timeframe") or "1h",
                     s.get("reward_risk") or 1.5, s.get("coins"), _utcnow()),
                )
        for old, new in (("autotrade_accounts", "autotrade_accounts_v1_migrated"),
                         ("autotrade_settings", "autotrade_settings_v1_migrated")):
            try:
                cur.execute(f"ALTER TABLE {old} RENAME TO {new}")
            except Exception as e:
                print(f"[autotrade] could not rename {old}: {e}")
        print(f"[autotrade] migrated {len(old_accounts)} account(s) from v1 tables")


def log_event(user_id, level, message, cur=None, mode=None):
    message = str(message)[:500]
    sql = "INSERT INTO autotrade_logs (user_id, mode, level, message, created_at) VALUES (%s,%s,%s,%s,%s)"
    params = (user_id, mode, level, message, _utcnow())
    try:
        if cur is not None:
            cur.execute(sql, params)
        else:
            with _Cursor() as c:
                c.execute(sql, params)
    except Exception as e:  # logging must never crash the engine
        print(f"[autotrade] log failed: {e}")


# ===========================================================================
# Settings
# ===========================================================================
def _row_to_settings(row):
    s = dict(DEFAULT_SETTINGS)
    if not row:
        return s
    for key in DEFAULT_SETTINGS:
        if key in row and row[key] is not None:
            s[key] = row[key]
    try:
        coins = json.loads(row.get("coins") or "[]")
        s["coins"] = coins if isinstance(coins, list) and coins else list(DEFAULT_COINS)
    except (TypeError, ValueError):
        s["coins"] = list(DEFAULT_COINS)
    legacy = row.get("markets") is None      # a row from before the signal hand-off: keep its old behaviour
    s["markets"] = _json_list(row.get("markets"), MARKETS) or (["crypto"] if legacy else list(DEFAULT_SETTINGS["markets"]))
    s["timeframes"] = _json_list(row.get("timeframes"), SIGNAL_TIMEFRAMES) or (
        ["4h"] if legacy else list(DEFAULT_SETTINGS["timeframes"]))
    s["min_grade"] = row.get("min_grade") if row.get("min_grade") in GRADES else ("C" if legacy else "B")
    s["symbols_mode"] = row.get("symbols_mode") if row.get("symbols_mode") in ("all", "list") else (
        "list" if legacy else "all")
    s["enabled"] = int(s["enabled"] or 0)
    s["leverage"] = int(s["leverage"] or 1)
    s["max_open_positions"] = int(s["max_open_positions"] or 1)
    for k in ("risk_pct", "max_position_usdt", "daily_loss_limit_pct", "min_confidence", "reward_risk"):
        s[k] = float(s[k])
    for k in ("paused_until", "pause_reason", "day_ref_date", "day_ref_balance", "last_scan_at"):
        s[k] = row.get(k)
    return s


def _json_list(raw, allowed):
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(v, list):
        return None
    out = [x for x in v if x in allowed]
    return out or None


def signal_symbols():
    """Every symbol that gets site signals (crypto + forex), for the coin picker."""
    try:
        return list(markets.CRYPTO_COINS) + list(markets.forex_signal_pairs())
    except Exception:
        return coin_choices()


def load_settings(user_id, mode, cur=None):
    def _q(c):
        c.execute("SELECT * FROM autotrade_bot_settings WHERE user_id=%s AND mode=%s", (user_id, mode))
        return c.fetchone()

    if cur is not None:
        return _row_to_settings(_q(cur))
    with _Cursor() as c:
        return _row_to_settings(_q(c))


def validate_settings(raw, current=None):
    """Checks + clamps settings coming from the browser.
    Returns (clean_dict, error_message_or_None)."""
    cur = dict(current or DEFAULT_SETTINGS)
    raw = raw or {}
    clean = {}

    market_type = str(raw.get("market_type", cur["market_type"])).lower()
    if market_type not in ("spot", "futures"):
        return None, "Market type must be 'spot' or 'futures'."
    clean["market_type"] = market_type

    try:
        leverage = int(raw.get("leverage", cur["leverage"]))
    except (TypeError, ValueError):
        return None, "Invalid leverage."
    if leverage not in ALLOWED_LEVERAGE:
        return None, f"Leverage must be one of: {', '.join(str(x) for x in ALLOWED_LEVERAGE)}x"
    clean["leverage"] = 1 if market_type == "spot" else leverage

    def num(key, lo, hi, label):
        val = _f(raw.get(key, cur[key]))
        if val is None:
            raise ValueError(f"{label} is invalid.")
        if val < lo or val > hi:
            raise ValueError(f"{label} must be between {lo:g} and {hi:g}.")
        return val

    try:
        clean["risk_pct"] = round(num("risk_pct", 0.1, 3.0, "Risk per trade %"), 2)
        clean["max_position_usdt"] = round(num("max_position_usdt", 10, 100000, "Max position (USDT)"), 2)
        clean["max_open_positions"] = int(num("max_open_positions", 1, 5, "Max open positions"))
        clean["daily_loss_limit_pct"] = round(num("daily_loss_limit_pct", 1, 20, "Daily loss limit %"), 2)
        clean["min_confidence"] = round(num("min_confidence", 55, 95, "Min confidence %"), 1)
        clean["reward_risk"] = round(num("reward_risk", 1.0, 4.0, "Reward:Risk"), 2)
    except ValueError as e:
        return None, str(e)

    timeframe = str(raw.get("timeframe", cur["timeframe"]))
    if timeframe not in TIMEFRAME_SCAN_SEC:
        return None, "Timeframe must be 15m, 1h or 4h."
    clean["timeframe"] = timeframe

    mk = raw.get("markets", cur.get("markets") or DEFAULT_SETTINGS["markets"])
    if not isinstance(mk, list) or not [m for m in mk if m in MARKETS]:
        return None, "Choose at least one market (crypto or forex)."
    clean["markets"] = [m for m in MARKETS if m in mk]
    tfs = raw.get("timeframes", cur.get("timeframes") or DEFAULT_SETTINGS["timeframes"])
    if not isinstance(tfs, list) or not [t for t in tfs if t in SIGNAL_TIMEFRAMES]:
        return None, "Choose at least one timeframe (15m, 1h or 4h)."
    clean["timeframes"] = [t for t in SIGNAL_TIMEFRAMES if t in tfs]
    grade = str(raw.get("min_grade", cur.get("min_grade") or "B")).upper()
    if grade not in GRADES:
        return None, "Choose a signal strength from the list."
    clean["min_grade"] = grade
    sm = str(raw.get("symbols_mode", cur.get("symbols_mode") or "all")).lower()
    if sm not in ("all", "list"):
        return None, "Symbols must be 'all' or 'list'."
    clean["symbols_mode"] = sm

    coins = raw.get("coins", cur["coins"])
    if not isinstance(coins, list):
        return None, "Invalid coin list."
    allowed = set(coin_choices()) | set(signal_symbols())
    picked = []
    for c in coins:
        c = str(c).upper().strip()
        if c in allowed and c not in picked:
            picked.append(c)
    if not picked and sm == "list":
        return None, "Select at least one coin."
    if not picked:
        picked = list(cur.get("coins") or DEFAULT_COINS)
    if len(picked) > MAX_COINS:
        return None, f"You can select up to {MAX_COINS} coins."
    clean["coins"] = picked
    return clean, None


def save_settings(user_id, mode, clean, cur):
    cur.execute("SELECT user_id FROM autotrade_bot_settings WHERE user_id=%s AND mode=%s", (user_id, mode))
    exists = cur.fetchone() is not None
    values = (
        clean["market_type"], clean["leverage"], clean["risk_pct"], clean["max_position_usdt"],
        clean["max_open_positions"], clean["daily_loss_limit_pct"], clean["min_confidence"],
        clean["timeframe"], clean["reward_risk"], json.dumps(clean["coins"]), _utcnow(),
        json.dumps(clean.get("markets") or DEFAULT_SETTINGS["markets"]),
        json.dumps(clean.get("timeframes") or DEFAULT_SETTINGS["timeframes"]),
        clean.get("min_grade") or "B", clean.get("symbols_mode") or "all",
    )
    if exists:
        cur.execute(
            """UPDATE autotrade_bot_settings SET market_type=%s, leverage=%s, risk_pct=%s,
               max_position_usdt=%s, max_open_positions=%s, daily_loss_limit_pct=%s,
               min_confidence=%s, timeframe=%s, reward_risk=%s, coins=%s, updated_at=%s,
               markets=%s, timeframes=%s, min_grade=%s, symbols_mode=%s
               WHERE user_id=%s AND mode=%s""",
            values + (user_id, mode),
        )
    else:
        cur.execute(
            """INSERT INTO autotrade_bot_settings (market_type, leverage, risk_pct, max_position_usdt,
               max_open_positions, daily_loss_limit_pct, min_confidence, timeframe, reward_risk,
               coins, updated_at, markets, timeframes, min_grade, symbols_mode, user_id, mode, enabled)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)""",
            values + (user_id, mode),
        )


def _ensure_settings_row(user_id, mode, cur):
    cur.execute("SELECT user_id FROM autotrade_bot_settings WHERE user_id=%s AND mode=%s", (user_id, mode))
    if cur.fetchone() is None:
        d = dict(DEFAULT_SETTINGS)
        if mode == "paper":
            d["market_type"] = "futures"      # the demo account can short, so every site signal can be copied
        if mode in FX_MODES:
            d["markets"] = ["forex"]
        save_settings(user_id, mode, d, cur)


# ===========================================================================
# Pure trading math (unit-testable, no network / DB)
# ===========================================================================
def plan_levels(side, entry_price, volatility_pct, reward_risk, leverage, min_pct=None):
    """Stop-Loss / Take-Profit from the entry price.

    volatility_pct = the signal's `extreme_volatility_95_pct` (95th
    percentile candle move, %), used as the SL distance - the same idea as
    main.py's quantile_volatility(), but kept at full precision (main.py
    rounds to 2 decimals, which breaks for cheap coins like DOGE/PEPE).
    Returns (plan_dict, error_or_None)."""
    if side not in ("LONG", "SHORT"):
        return None, "Side must be LONG or SHORT."
    entry_price = _f(entry_price)
    if not entry_price or entry_price <= 0:
        return None, "Price not available."
    vol = _f(volatility_pct)
    sl_pct = _clamp(vol if vol and vol > 0 else SL_FALLBACK_PCT, SL_MIN_PCT if min_pct is None else min_pct, SL_MAX_PCT)
    leverage = int(leverage or 1)
    if leverage > 1:
        liq_pct = 100.0 / leverage
        if sl_pct > liq_pct * LIQ_SAFETY_FRACTION:
            return None, (f"At {leverage}x leverage the stop-loss ({sl_pct:.2f}%) is too close to liquidation. "
                          f"Lower the leverage.")
    tp_pct = sl_pct * float(reward_risk or 1.5)
    if side == "LONG":
        sl = entry_price * (1 - sl_pct / 100)
        tp = entry_price * (1 + tp_pct / 100)
    else:
        sl = entry_price * (1 + sl_pct / 100)
        tp = entry_price * (1 - tp_pct / 100)
    return {"sl_pct": round(sl_pct, 3), "tp_pct": round(tp_pct, 3), "stop_loss": sl, "take_profit": tp}, None


def ladder_levels(side, entry, stop, targets_r):
    """Target prices from the entry, the stop (1R) and the engine's targets in R, e.g. [0.5, 1, 2]."""
    risk = abs(float(entry) - float(stop))
    sgn = 1 if side == "LONG" else -1
    return [float(entry) + sgn * float(k) * risk for k in targets_r]


PARTIAL_SHARE = 1.0 / 3.0   # a third of the starting size closes at TP1 and at TP2 (the rest at TP3 or the stop)


def size_position(free_usdt, risk_pct, sl_pct, max_position_usdt, leverage, market_type):
    """Risk-based sizing: if SL is hit, the loss is ~risk_pct % of balance.
    Returns (notional_usdt, risk_usdt)."""
    free_usdt = max(0.0, _f(free_usdt, 0.0))
    risk_usdt = free_usdt * float(risk_pct) / 100.0
    if sl_pct <= 0:
        return 0.0, risk_usdt
    notional = risk_usdt / (sl_pct / 100.0)
    notional = min(notional, float(max_position_usdt))
    if market_type == "futures":
        notional = min(notional, free_usdt * 0.9 * max(1, int(leverage)))
    else:
        notional = min(notional, free_usdt * 0.95)
    return max(0.0, notional), risk_usdt


def calc_pnl(side, entry, exit_price, amount, fee_rate):
    entry = float(entry)
    exit_price = float(exit_price)
    amount = float(amount)
    gross = (exit_price - entry) * amount if side == "LONG" else (entry - exit_price) * amount
    fees = (entry * amount + exit_price * amount) * float(fee_rate or 0)
    return round(gross - fees, 4)


def _pnl(pos, exit_price, amount=None):
    """P&L in the account currency (USDT / USD): forex results are converted from the pair's quote currency."""
    pnl = calc_pnl(pos["side"], pos["entry_price"], exit_price, pos["amount"] if amount is None else amount,
                   pos.get("fee_rate"))
    return round(pnl * float(pos.get("fx_rate") or 1.0), 4)


def level_hit(side, price, stop_loss, take_profit):
    if price is None:
        return None
    if side == "LONG":
        if price <= stop_loss:
            return "SL"
        if price >= take_profit:
            return "TP"
    else:
        if price >= stop_loss:
            return "SL"
        if price <= take_profit:
            return "TP"
    return None


# ===========================================================================
# Binance client (thin ccxt wrapper)
# ===========================================================================
class BinanceClient:
    """The only class that talks to the network. The engine uses only these
    methods, which makes it testable with a fake client."""

    def __init__(self, api_key, secret, mode, market_type):
        if ccxt is None:
            raise RuntimeError("The ccxt library is not installed (pip install ccxt).")
        self.mode = mode
        self.market_type = market_type
        options = {
            "defaultType": "future" if market_type == "futures" else "spot",
            "adjustForTimeDifference": True,
        }
        self.ex = ccxt.binance({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "timeout": 15000,
            "options": options,
        })
        if mode == "demo":
            # Binance replaced the futures testnet with "Demo Trading";
            # recent ccxt versions expose enable_demo_trading().
            if hasattr(self.ex, "enable_demo_trading"):
                self.ex.enable_demo_trading(True)
            else:
                self.ex.set_sandbox_mode(True)
        self._markets_loaded = False

    # --- symbols / markets -------------------------------------------------
    def sym(self, symbol):
        if self.market_type == "futures" and ":" not in symbol:
            quote = symbol.split("/")[1]
            return f"{symbol}:{quote}"
        return symbol

    def _load(self):
        if not self._markets_loaded:
            self.ex.load_markets()
            self._markets_loaded = True

    def has_symbol(self, symbol):
        self._load()
        return self.sym(symbol) in self.ex.markets

    def market_rules(self, symbol):
        self._load()
        m = self.ex.market(self.sym(symbol))
        limits = m.get("limits") or {}
        default_fee = 0.0005 if self.market_type == "futures" else 0.001
        return {
            "min_amount": _f((limits.get("amount") or {}).get("min"), 0.0),
            "min_cost": _f((limits.get("cost") or {}).get("min"), 0.0),
            "taker": _f(m.get("taker"), default_fee) or default_fee,
        }

    def amount_to_precision(self, symbol, amount):
        self._load()
        return float(self.ex.amount_to_precision(self.sym(symbol), amount))

    def price_to_precision(self, symbol, price):
        self._load()
        return float(self.ex.price_to_precision(self.sym(symbol), price))

    # --- account -------------------------------------------------------------
    def balance_usdt(self):
        bal = self.ex.fetch_balance()
        usdt = bal.get("USDT") or {}
        return _f(usdt.get("free"), 0.0), _f(usdt.get("total"), 0.0)

    def base_free(self, symbol):
        base = symbol.split("/")[0]
        bal = self.ex.fetch_balance()
        return _f((bal.get(base) or {}).get("free"), 0.0)

    def api_restrictions(self):
        """Live account API permissions (is withdrawal enabled?)."""
        try:
            return self.ex.sapi_get_account_apirestrictions()
        except Exception:
            return None

    # --- prices --------------------------------------------------------------
    def price(self, symbol):
        return _f(self.ex.fetch_ticker(self.sym(symbol)).get("last"))

    def prices(self, symbols):
        out = {}
        if not symbols:
            return out
        try:
            tickers = self.ex.fetch_tickers([self.sym(s) for s in symbols])
            for s in symbols:
                t = tickers.get(self.sym(s))
                if t and t.get("last") is not None:
                    out[s] = _f(t["last"])
        except Exception:
            for s in symbols:
                try:
                    out[s] = self.price(s)
                except Exception:
                    pass
        return out

    # --- futures setup -------------------------------------------------------
    def prepare_futures(self, symbol, leverage):
        s = self.sym(symbol)
        try:
            self.ex.set_position_mode(False)  # one-way mode (not hedge mode)
        except Exception:
            pass
        try:
            self.ex.set_margin_mode("isolated", s)
        except Exception:
            pass  # Binance errors if it is already isolated
        self.ex.set_leverage(int(leverage), s)

    def futures_positions(self, symbols):
        """{ 'BTC/USDT': contracts } for symbols with an open position."""
        out = {}
        if not symbols:
            return out
        ps = self.ex.fetch_positions([self.sym(s) for s in symbols])
        reverse = {self.sym(s): s for s in symbols}
        for p in ps or []:
            s = reverse.get(p.get("symbol"))
            if not s:
                continue
            contracts = abs(_f(p.get("contracts"), 0.0))
            if contracts > 0:
                out[s] = out.get(s, 0.0) + contracts
        return out

    # --- orders --------------------------------------------------------------
    def market_order(self, symbol, side, amount, reduce_only=False, client_order_id=None):
        """Sends ONE market order. client_order_id (optional) tags the order so that, after a timeout, the caller
        can ask Binance whether it was placed (see find_order) instead of blindly sending it a second time."""
        s = self.sym(symbol)
        params = {"reduceOnly": True} if (reduce_only and self.market_type == "futures") else {}
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        o = self.ex.create_order(s, "market", side, amount, None, params)
        return self._order_result(o, s)

    def _order_result(self, o, s):
        avg = o.get("average") or o.get("price")
        filled = o.get("filled")
        if not avg or not filled:
            try:
                o2 = self.ex.fetch_order(o["id"], s)
                avg = avg or o2.get("average") or o2.get("price")
                filled = filled or o2.get("filled")
            except Exception:
                pass
        return {"id": str(o.get("id")), "average": _f(avg), "filled": _f(filled)}

    def find_order(self, symbol, client_order_id):
        """The order that was sent with this client_order_id, or None when Binance confirms there is no such
        order (so it is safe to send it again). Any other error is raised: the outcome is then unknown."""
        s = self.sym(symbol)
        try:
            o = self.ex.fetch_order(None, s, {"origClientOrderId": client_order_id})
        except Exception as e:
            text = f"{type(e).__name__} {e}".lower()
            if (ccxt is not None and isinstance(e, getattr(ccxt, "OrderNotFound", ()))) \
                    or "order does not exist" in text or "-2013" in text:
                return None
            raise
        if (o.get("status") in ("canceled", "cancelled", "rejected", "expired")) and not _f(o.get("filled"), 0.0):
            return None
        return self._order_result(o, s)

    def backup_stop(self, symbol, close_side, amount, stop_price):
        o = self.ex.create_order(self.sym(symbol), "market", close_side, amount, None,
                                 {"stopLossPrice": stop_price, "reduceOnly": True})
        return str(o.get("id"))

    def cancel_stop(self, symbol, order_id):
        """Cancels only the bot's own safety stop. The account may hold the user's other orders on the same
        symbol, so nothing else is touched."""
        if not order_id:
            return
        s = self.sym(symbol)
        for params in ({}, {"trigger": True}):   # newer Binance futures keeps stop orders in a separate service
            try:
                self.ex.cancel_order(order_id, s, params)
                return
            except Exception:
                continue

    def order_average(self, order_id, symbol):
        try:
            o = self.ex.fetch_order(order_id, self.sym(symbol))
            if (o.get("status") == "closed") and o.get("average"):
                return _f(o["average"])
        except Exception:
            pass
        return None

    def cancel_all(self, symbol):
        """Cancels every open order on the symbol. The bot does not use this any more (see cancel_stop); it is
        kept for emergency tools only."""
        try:
            self.ex.cancel_all_orders(self.sym(symbol))
        except Exception:
            pass


class PaperClient:
    """The "Site demo" account: trades the user's own Signals FM Demo trading account (papertrade.py) with the
    same methods as BinanceClient, so the engine, sizing, SL/TP and logs work unchanged. No API keys, no
    network beyond the public prices the site already reads. The demo engine itself closes the position at the
    bot's stop-loss / take-profit (it checks every 2 seconds); the bot notices and records the result."""

    is_paper = True

    def __init__(self, user_id, market_type):
        import papertrade
        self.pt = papertrade
        self.user_id = int(user_id)
        self.market_type = market_type
        self.mode = "paper"

    def sym(self, symbol):
        return symbol

    def _meta(self, symbol):
        return self.pt.feed.meta(symbol)

    def has_symbol(self, symbol):
        return symbol in self.pt.feed.coins()

    def market_rules(self, symbol):
        return {"min_amount": _f(self._meta(symbol).get("min_qty"), 0.0), "min_cost": float(self.pt.MIN_NOTIONAL),
                "taker": float(self.pt.FEES[self.market_type]["taker"])}

    def amount_to_precision(self, symbol, amount):
        return float(self.pt._floor_step(float(amount), self._meta(symbol)["qty_step"]))

    def price_to_precision(self, symbol, price):
        return float(self.pt._round_tick(float(price), self._meta(symbol)["price_tick"]))

    def balance_usdt(self):
        snap = self.pt.bot_snapshot(self.user_id)
        return max(0.0, float(snap["available"])), float(snap["equity"])

    def base_free(self, symbol):
        pos = self.pt.bot_positions(self.user_id, "spot").get(symbol)
        return float(pos["qty"]) if pos else 0.0

    def api_restrictions(self):
        return None

    def price(self, symbol):
        p = self.pt.bot_price(symbol)
        if p is None:
            raise RuntimeError(f"Live price for {symbol} is not available right now.")
        return p

    def prices(self, symbols):
        return self.pt.bot_prices(symbols)

    def prepare_futures(self, symbol, leverage):
        self.pt.bot_prepare_futures(self.user_id, symbol, leverage)

    def futures_positions(self, symbols):
        """{symbol: qty} of open demo positions in this market (spot too: the demo engine can close either)."""
        rows = self.pt.bot_positions(self.user_id, self.market_type)
        return {s: float(rows[s]["qty"]) for s in symbols if s in rows and float(rows[s]["qty"]) > 0}

    def market_order(self, symbol, side, amount, reduce_only=False):
        opening = not reduce_only and (self.market_type == "futures" or side == "buy")
        try:
            return self.pt.bot_market_order(self.user_id, self.market_type, symbol, side.upper(), float(amount),
                                            reduce_only=reduce_only, opening=opening)
        except self.pt.BotError as e:
            raise RuntimeError(str(e))

    def attach_tpsl(self, symbol, stop_loss, take_profit):
        self.pt.bot_set_tpsl(self.user_id, self.market_type, symbol, tp=take_profit, sl=stop_loss)

    def backup_stop(self, symbol, close_side, amount, stop_price):
        return None  # the demo position already carries the exact stop-loss (attach_tpsl)

    def order_average(self, order_id, symbol):
        return None

    def last_close(self, symbol, since=None):
        return self.pt.bot_last_close(self.user_id, self.market_type, symbol, since=since)

    def cancel_all(self, symbol):
        pass


_client_cache = {}
_client_cache_lock = threading.Lock()
CLIENT_TTL_SEC = 1800


def _client_factory(api_key, secret, mode, market_type):
    return BinanceClient(api_key, secret, mode, market_type)


def _fx_price(symbol):
    fn = _hooks.get("forex_price")
    return fn(symbol) if fn else None


def _oanda_factory(token, account_id, env):
    return fxbroker.OandaClient(token, account_id, env)


def get_client(account, market_type):
    mode = account.get("mode")
    if market_type == "forex":
        if mode == "paper":
            return fxbroker.FxPaperClient(account["user_id"], _fx_price, _Cursor)
        if mode in FX_MODES:
            key = (account["user_id"], mode, "forex", account.get("key_hint"), hash(account["api_key_enc"]))
            now = time.time()
            with _client_cache_lock:
                hit = _client_cache.get(key)
                if hit and now - hit[0] < CLIENT_TTL_SEC:
                    return hit[1]
            client = _oanda_factory(_decrypt(account["api_key_enc"]), _decrypt(account["api_secret_enc"]),
                                    "live" if mode == "oanda_live" else "practice")
            with _client_cache_lock:
                _client_cache[key] = (now, client)
            return client
        raise RuntimeError("Binance has no forex. Use the Site demo account or connect an OANDA account.")
    if mode in FX_MODES:
        raise RuntimeError("An OANDA account trades forex only.")
    if mode == "paper":
        return PaperClient(account["user_id"], market_type)
    key = (account["user_id"], account["mode"], market_type, account.get("key_hint"),
           hash(account["api_key_enc"]))
    now = time.time()
    with _client_cache_lock:
        hit = _client_cache.get(key)
        if hit and now - hit[0] < CLIENT_TTL_SEC:
            return hit[1]
    client = _client_factory(_decrypt(account["api_key_enc"]), _decrypt(account["api_secret_enc"]),
                             account["mode"], market_type)
    with _client_cache_lock:
        _client_cache[key] = (now, client)
    return client


def _drop_clients(user_id, mode):
    with _client_cache_lock:
        for k in [k for k in _client_cache if k[0] == user_id and k[1] == mode]:
            _client_cache.pop(k, None)


PAPER_KEY_HINT = "Signals FM"


def _ensure_paper_account(user_id, cur):
    """The Site demo account needs no keys: one placeholder row, so the engine's queries see it like the others."""
    cur.execute("SELECT user_id FROM autotrade_exchange_accounts WHERE user_id=%s AND mode='paper'", (user_id,))
    if cur.fetchone() is None:
        cur.execute(
            """INSERT INTO autotrade_exchange_accounts (user_id, mode, exchange, api_key_enc, api_secret_enc, key_hint,
               connected_at) VALUES (%s,'paper','signalsfm','-','-',%s,%s)""",
            (user_id, PAPER_KEY_HINT, _utcnow()),
        )


def load_account(user_id, mode, cur=None):
    def _q(c):
        if mode == "paper":
            _ensure_paper_account(user_id, c)
        c.execute("SELECT * FROM autotrade_exchange_accounts WHERE user_id=%s AND mode=%s", (user_id, mode))
        return c.fetchone()

    if cur is not None:
        return _q(cur)
    with _Cursor() as c:
        return _q(c)


def _friendly_exchange_error(e):
    name = type(e).__name__
    text = str(e)
    if "451" in text or "restricted location" in text.lower():
        return ("Binance blocks API access from this server's country. "
                "Deploy the server in a non-US region.")
    if name in ("AuthenticationError",) or "API-key" in text or "Invalid Api-Key" in text or "-2015" in text:
        return "Invalid API key/secret, or this key has no permission for this market (Spot/Futures)."
    if name == "InsufficientFunds" or "insufficient" in text.lower():
        return "Not enough USDT balance on the Binance account."
    if name in ("NetworkError", "RequestTimeout", "ExchangeNotAvailable", "DDoSProtection"):
        return "Could not reach Binance (network/timeout). Please try again shortly."
    return f"Binance error: {text[:180]}"


def _error_text(e):
    return str(e) if isinstance(e, (RuntimeError,)) else _friendly_exchange_error(e)


# ===========================================================================
# Signals (SignalX engine)
# ===========================================================================
_signal_cache = {}
_signal_cache_lock = threading.Lock()


def get_signal(symbol, timeframe, max_age_sec=300):
    """Runs SignalX's own generate_signal(). The verdict/confidence come
    only from Hawkes + Bayesian (Conformal), which do not depend on the
    order book, so include_orderbook=False (lighter and faster)."""
    key = (symbol, timeframe)
    now = time.time()
    with _signal_cache_lock:
        hit = _signal_cache.get(key)
        if hit and now - hit[0] <= max_age_sec:
            return hit[1]
    engine_signal = _hooks.get("engine_signal")
    if engine_signal:
        # Signal engine v3 (4h candles): same signal as the dashboard and the track record.
        res = engine_signal(symbol)
        if res.get("error"):
            raise RuntimeError(res["error"])
        sl_pct, tp_pct = _f(res.get("sl_pct")), _f(res.get("tp_pct"))
        active = res.get("active") or {}
        summary = {
            "symbol": symbol,
            "timeframe": res.get("timeframe") or "4h",
            # the bot only enters on a signal from the candle that just closed, never late
            "verdict": res.get("verdict") if res.get("fresh") else "WAIT",
            "active_side": active.get("side"),
            "active_since": active.get("signal_at"),
            "confidence": _f(res.get("confidence"), 0.0),
            "volatility_pct": sl_pct,
            "reward_risk": round(tp_pct / sl_pct, 4) if sl_pct and tp_pct else None,
            # three targets in R (0.5 / 1 / 2): a third closes at each, the stop moves after TP1 and TP2
            "targets_r": res.get("targets_r") if res.get("targets") else None,
            "tier": res.get("tier"),
            "last_price": _f(res.get("entry")),
            "trend": "Bullish" if (_f(res.get("p_long"), 0) >= _f(res.get("p_short"), 0)) else "Bearish",
            "engine_version": res.get("engine_version"),
            "computed_at": _iso(_utcnow()),
        }
        with _signal_cache_lock:
            _signal_cache[key] = (now, summary)
        return summary
    get_candles = _hooks.get("get_candles")
    generate_signal = _hooks.get("generate_signal")
    if not get_candles or not generate_signal:
        raise RuntimeError("Signal engine is not available.")
    df = get_candles(symbol=symbol, timeframe=timeframe, limit=200)
    res = generate_signal(df, symbol=symbol, include_orderbook=False)
    summary = {
        "symbol": symbol,
        "timeframe": timeframe,
        "verdict": res.get("final_verdict"),
        "confidence": _f(res.get("confidence_pct"), 0.0),
        "volatility_pct": _f(res.get("extreme_volatility_95_pct")),
        "last_price": _f(res.get("last_price")),
        "trend": res.get("trend"),
        "computed_at": _iso(_utcnow()),
    }
    with _signal_cache_lock:
        _signal_cache[key] = (now, summary)
    return summary


# ===========================================================================
# Open / close positions (shared by the engine and the routes)
# ===========================================================================
class TradeError(Exception):
    pass


def _open_positions(user_id, mode, cur):
    cur.execute(
        """SELECT * FROM autotrade_positions WHERE user_id=%s AND mode=%s AND status IN ('OPEN','CLOSING')
           ORDER BY opened_at""",
        (user_id, mode),
    )
    return cur.fetchall() or []


def _store_balance(user_id, mode, free, total, market_type, cur):
    cur.execute(
        """UPDATE autotrade_exchange_accounts SET balance_usdt=%s, balance_total_usdt=%s, balance_market=%s,
           balance_at=%s WHERE user_id=%s AND mode=%s""",
        (free, total, market_type, _utcnow(), user_id, mode),
    )


def _retry(fn, *args, tries=3, delay=0.5, **kwargs):
    """Calls an exchange function, retrying network / rate-limit problems with a growing wait."""
    last = None
    for k in range(tries):
        try:
            return fn(*args, **kwargs)
        except TradeError:
            raise
        except Exception as e:
            last = e
            if not _retryable(e) or k == tries - 1:
                raise
            time.sleep(delay * (2 ** k))
    raise last


def _retryable(e):
    if ccxt is not None and isinstance(e, (getattr(ccxt, "NetworkError", ()), getattr(ccxt, "RateLimitExceeded", ()),
                                           getattr(ccxt, "DDoSProtection", ()), getattr(ccxt, "RequestTimeout", ()))):
        return True
    text = f"{type(e).__name__} {e}".lower()
    return any(w in text for w in ("timeout", "timed out", "temporar", "rate limit", "429", "502", "503", "504",
                                   "connection", "not reachable", "network"))


def _place_entry(client, symbol, order_side, amount):
    """Sends the entry market order so that it can never be placed twice.

    A timeout does not tell us whether the exchange received the order. Re-sending blindly could open the
    position twice with real money. So on Binance the order carries its own id; after a network problem we first
    ask Binance whether that order exists and only send it again when Binance confirms it does not. If even that
    question cannot be answered, we stop and tell the user to check the account instead of guessing.
    Other clients (the site demo account, OANDA) send the order exactly once."""
    if not hasattr(client, "find_order"):
        return client.market_order(symbol, order_side, amount)
    cid = "sfm" + os.urandom(14).hex()          # 31 characters: letters + digits only, as Binance requires
    for k in range(3):
        try:
            return client.market_order(symbol, order_side, amount, client_order_id=cid)
        except TradeError:
            raise
        except Exception as e:
            if not _retryable(e) or k == 2:
                raise
            time.sleep(1.0 + k)                 # give a slow request time to finish before asking
            try:
                found = client.find_order(symbol, cid)
            except Exception:
                raise TradeError(
                    f"The exchange did not confirm whether the {symbol} order was placed. Nothing was sent again. "
                    f"Check your Binance account for an open {symbol} position before trying again.")
            if found:
                return found


def _cancel_backup(client, pos):
    """Removes the bot's own backup stop for this position. Never cancels the user's other orders."""
    if hasattr(client, "cancel_stop"):
        client.cancel_stop(pos["symbol"], pos.get("backup_stop_id"))
    else:
        client.cancel_all(pos["symbol"])         # site demo / OANDA clients: no-op or the bot's own orders


def open_position(user_id, account, settings, symbol, side, signal=None, source="BOT", dry_run=False):
    """Opens a new position with a market order (with SL/TP).
    dry_run=True -> returns only the plan/size, places no order.
    A site signal (signal["sl_pct"] + signal["targets_r"]) keeps its own stop distance and targets in R,
    applied to the actual fill price."""
    mode = account["mode"]
    fx = markets.is_forex(symbol)
    market_type = "forex" if fx else settings["market_type"]
    leverage = int(settings["leverage"]) if market_type == "futures" else 1
    if side not in ("LONG", "SHORT"):
        raise TradeError("Side must be LONG or SHORT.")
    if mode in FX_MODES and not fx:
        raise TradeError("An OANDA account trades forex only.")
    if fx and mode in BINANCE_MODES:
        raise TradeError("Binance has no forex. Use the Site demo account or connect an OANDA account.")
    if market_type == "spot" and side == "SHORT":
        raise TradeError("SHORT is not possible on spot. Choose Futures in the bot settings.")

    with _Cursor() as cur:
        existing = _open_positions(user_id, mode, cur)
    if any(p["symbol"] == symbol for p in existing):
        raise TradeError(f"A position on {symbol} is already open.")
    if len(existing) >= int(settings["max_open_positions"]):
        raise TradeError(f"Max open positions ({settings['max_open_positions']}) reached.")

    try:
        client = get_client(account, market_type)
    except RuntimeError as e:
        raise TradeError(str(e))
    try:
        if not client.has_symbol(symbol):
            where = "Demo trading" if getattr(client, "is_paper", False) else (
                "this account" if fx else f"Binance {market_type}")
            raise TradeError(f"{symbol} is not available on {where}.")
        free, total = _retry(client.balance_usdt)
        price = _retry(client.price, symbol)
        rules = client.market_rules(symbol)
    except TradeError:
        raise
    except Exception as e:
        raise TradeError(_error_text(e))

    sig = signal or {}
    vol = sig.get("sl_pct") or sig.get("volatility_pct")
    rr = sig.get("reward_risk") or settings["reward_risk"]  # engine levels when the signal has them
    min_pct = SL_MIN_PCT_SIGNAL["forex" if fx else "crypto"] if sig.get("sl_pct") else SL_MIN_PCT
    plan, err = plan_levels(side, price, vol, rr, leverage, min_pct=min_pct)
    if err:
        raise TradeError(err)
    rate = 1.0
    if fx:
        rate = fxbroker.fx_rate(symbol, client.price)
        if not rate:
            raise TradeError(f"No conversion price for {symbol} right now.")
        risk_usdt = (total or free) * float(settings["risk_pct"]) / 100.0
        stop_dist = price * plan["sl_pct"] / 100.0
        units = risk_usdt / (stop_dist * rate) if stop_dist > 0 else 0.0
        units = min(units, free * fxbroker.FX_MAX_LEVERAGE / (price * rate))
        amount = client.amount_to_precision(symbol, units)
        notional = amount * price * rate
        if amount < 1:
            raise TradeError("Trade size is too small for this account balance.")
    else:
        notional, risk_usdt = size_position(free, settings["risk_pct"], plan["sl_pct"],
                                            settings["max_position_usdt"], leverage, market_type)
        amount = client.amount_to_precision(symbol, notional / price) if notional > 0 else 0.0
        min_cost = max(rules["min_cost"], 5.0)
        if amount <= 0 or amount < rules["min_amount"] or amount * price < min_cost:
            raise TradeError(
                f"Trade size is too small ({amount * price:.2f} USDT). The minimum is about {min_cost:.2f} USDT - "
                f"add balance or raise Risk % / Max position."
            )

    targets_r = sig.get("targets_r")
    ladder = bool(targets_r and len(targets_r) >= 3)

    def plan_targets(entry_px, stop_px):
        if not ladder:
            return None
        return [client.price_to_precision(symbol, v) for v in ladder_levels(side, entry_px, stop_px, targets_r)]

    preview = {
        "symbol": symbol, "side": side, "market_type": market_type, "leverage": leverage,
        "price": price, "amount": amount, "notional_usdt": round(amount * price * rate, 2),
        "margin_usdt": round(amount * price * rate / (fxbroker.FX_MAX_LEVERAGE if fx else leverage), 2),
        "risk_usdt": round(risk_usdt, 2),
        "stop_loss": client.price_to_precision(symbol, plan["stop_loss"]),
        "take_profit": client.price_to_precision(symbol, plan["take_profit"]),
        "targets": plan_targets(price, plan["stop_loss"]),
        "sl_pct": plan["sl_pct"], "tp_pct": plan["tp_pct"], "free_usdt": round(free, 2),
        "mode": mode, "account_currency": "USD" if fx else "USDT",
    }
    if dry_run:
        return preview

    try:
        if market_type == "futures":
            _retry(client.prepare_futures, symbol, leverage)
        order = _place_entry(client, symbol, "buy" if side == "LONG" else "sell", amount)
    except TradeError:
        raise
    except Exception as e:
        raise TradeError(_error_text(e))

    fill = order["average"] or price
    filled = order["filled"] or amount
    plan, _ = plan_levels(side, fill, vol, rr, 1, min_pct=min_pct)  # levels from the actual fill
    sl = client.price_to_precision(symbol, plan["stop_loss"])
    tp = client.price_to_precision(symbol, plan["take_profit"])
    tps = plan_targets(fill, plan["stop_loss"])
    if tps:
        tp = tps[-1]
    tp1, tp2 = (tps[0], tps[1]) if tps else (None, None)
    now = _utcnow()
    try:
        with _Cursor() as cur:
            cur.execute(
                """INSERT INTO autotrade_positions (user_id, mode, market_type, symbol, side, source, amount,
                   entry_price, stop_loss, take_profit, leverage, notional_usdt, fee_rate, confidence, status,
                   entry_order_id, last_price, unrealized_pnl, opened_at, status_changed_at,
                   tp1, tp2, hits, initial_amount, initial_stop, realized_usdt, signal_id, timeframe, market, fx_rate)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s,%s,0,%s,%s,%s,%s,0,%s,%s,0,%s,%s,%s,%s)""",
                (user_id, mode, market_type, symbol, side, source, filled, fill, sl, tp, leverage,
                 round(filled * fill * rate, 4), rules["taker"], sig.get("confidence"), order["id"],
                 fill, now, now, tp1, tp2, filled, sl, sig.get("signal_id"), sig.get("timeframe"),
                 "forex" if fx else "crypto", rate if fx else None),
            )
            position_id = cur.lastrowid
            try:
                free2, total2 = client.balance_usdt()
                _store_balance(user_id, mode, free2, total2, market_type, cur)
            except Exception:
                pass
    except Exception as db_err:
        # The order went through but could not be saved -> close it right away
        try:
            client.market_order(symbol, "sell" if side == "LONG" else "buy", filled, reduce_only=True)
        except Exception:
            pass
        raise TradeError(f"Position could not be saved, so the order was closed again: {db_err}")

    if getattr(client, "is_paper", False):
        try:
            client.attach_tpsl(symbol, sl, tp)
        except Exception as e:
            log_event(user_id, "WARN", f"{symbol}: SL/TP could not be set on the demo position ({type(e).__name__}). "
                                       f"The bot will monitor them itself.", mode=mode)
    elif market_type == "forex":
        try:
            sid = _retry(client.set_stop, order["id"], symbol, sl)
            with _Cursor() as cur:
                cur.execute("UPDATE autotrade_positions SET backup_stop_id=%s WHERE id=%s", (sid, position_id))
        except Exception as e:
            log_event(user_id, "WARN", f"{symbol}: the stop-loss could not be attached at the broker "
                                       f"({type(e).__name__}). The bot will monitor it itself.", mode=mode)
    elif market_type == "futures":
        dist = abs(fill - sl)
        backup_price = sl - dist * BACKUP_STOP_EXTRA if side == "LONG" else sl + dist * BACKUP_STOP_EXTRA
        try:
            stop_id = client.backup_stop(symbol, "sell" if side == "LONG" else "buy", filled,
                                         client.price_to_precision(symbol, backup_price))
            with _Cursor() as cur:
                cur.execute("UPDATE autotrade_positions SET backup_stop_id=%s WHERE id=%s", (stop_id, position_id))
        except Exception as e:
            log_event(user_id, "WARN", f"{symbol}: backup stop could not be placed on Binance "
                                       f"({type(e).__name__}). The bot will monitor the SL itself.", mode=mode)

    conf = sig.get("confidence")
    conf_txt = f", signal {conf:.0f}%" if conf else ""
    tf_txt = f" {sig['timeframe']}" if sig.get("timeframe") else ""
    tp_txt = f"TP1 {tp1:g} | TP2 {tp2:g} | TP3 {tp:g}" if tps else f"TP {tp:g}"
    log_event(user_id, "TRADE",
              f"OPEN {side} {symbol}{tf_txt} {filled:g} @ {fill:g} ({leverage}x{conf_txt}) | SL {sl:g} | {tp_txt} [{source}]",
              mode=mode)
    preview.update({"id": position_id, "entry_price": fill, "amount": filled, "stop_loss": sl, "take_profit": tp,
                    "targets": tps})
    return preview


def _claim(position_id, from_status=("OPEN",)):
    """Atomically marks a position 'CLOSING' so the engine and a user click
    never close it twice. True = we own the close."""
    placeholders = ",".join(["%s"] * len(from_status))
    with _Cursor() as cur:
        cur.execute(
            f"UPDATE autotrade_positions SET status='CLOSING', status_changed_at=%s WHERE id=%s AND status IN ({placeholders})",
            (_utcnow(), position_id) + tuple(from_status),
        )
        return cur.rowcount == 1


def _release(position_id):
    with _Cursor() as cur:
        cur.execute("UPDATE autotrade_positions SET status='OPEN', status_changed_at=%s WHERE id=%s AND status='CLOSING'",
                    (_utcnow(), position_id))


def _finalize(pos, exit_price, reason, client=None, pnl=None):
    """Closes the record. pnl = result of the part still open (computed when None); the profit already taken
    at TP1 / TP2 (realized_usdt) is added, so pnl_usdt is the whole trade."""
    if pnl is None:
        pnl = _pnl(pos, exit_price)
    pnl = round(float(pnl) + _f(pos.get("realized_usdt"), 0.0), 4)
    with _Cursor() as cur:
        cur.execute(
            """UPDATE autotrade_positions SET status='CLOSED', exit_price=%s, pnl_usdt=%s, close_reason=%s,
               closed_at=%s, last_price=%s, unrealized_pnl=0, status_changed_at=%s WHERE id=%s""",
            (exit_price, pnl, reason, _utcnow(), exit_price, _utcnow(), pos["id"]),
        )
        if client is not None:
            try:
                free, total = client.balance_usdt()
                _store_balance(pos["user_id"], pos["mode"], free, total, pos["market_type"], cur)
            except Exception:
                pass
    sign = "+" if pnl >= 0 else ""
    log_event(pos["user_id"], "TRADE",
              f"CLOSE {pos['side']} {pos['symbol']} @ {exit_price:g} | PnL {sign}{pnl:.2f} USDT [{reason}]",
              mode=pos["mode"])
    return pnl


PAPER_REASON = {"TP": "TP", "SL": "SL", "LIQUIDATION": "LIQUIDATION", "TRAILING": "SL"}


def _gone_exit(client, pos, price_hint=None):
    """The position is no longer on the exchange: (exit price, reason, pnl or None) from what closed it."""
    if getattr(client, "is_paper", False):
        info = client.last_close(pos["symbol"], since=_to_dt(pos.get("opened_at")))
        if info and info.get("exit_price"):
            return info["exit_price"], PAPER_REASON.get(info.get("close_reason"), "EXTERNAL"), info.get("net_pnl")
        return price_hint or pos.get("last_price") or client.price(pos["symbol"]), "EXTERNAL", None
    exit_price = client.order_average(pos["backup_stop_id"], pos["symbol"]) if pos.get("backup_stop_id") else None
    return exit_price or price_hint or pos.get("last_price") or client.price(pos["symbol"]), "EXCHANGE", None


def close_position(pos, account, reason, price_hint=None, already_claimed=False):
    """Closes a position with a market order. Returns pnl (float)."""
    if not already_claimed and not _claim(pos["id"]):
        raise TradeError("This position is already being closed.")
    try:
        client = get_client(account, pos["market_type"])
        symbol = pos["symbol"]
        close_side = "sell" if pos["side"] == "LONG" else "buy"
        if getattr(client, "is_paper", False):
            qty = client.futures_positions([symbol]).get(symbol, 0.0)
            if qty <= 0:
                exit_price, why, pnl = _gone_exit(client, pos, price_hint)
                return _finalize(pos, exit_price, why, client, pnl=pnl)
            amount = client.amount_to_precision(symbol, min(qty, float(pos["amount"])))
            if float(pos["amount"]) >= qty * 0.999:
                amount = qty  # all of it, never leave dust behind
            order = client.market_order(symbol, close_side, amount, reduce_only=True)
            closed = order.get("closed") or {}
            exit_price = closed.get("exit_price") or order["average"] or price_hint or client.price(symbol)
            return _finalize(pos, exit_price, reason, client, pnl=closed.get("net_pnl"))
        if pos["market_type"] == "forex":
            live = client.futures_positions([symbol])
            if live is not None and live.get(symbol, 0.0) <= 0:
                exit_price, why, pnl = _gone_exit(client, pos, price_hint)
                return _finalize(pos, exit_price, why, client, pnl=pnl)
            order = client.market_order(symbol, close_side, float(pos["amount"]), reduce_only=True)
            exit_price = order["average"] or price_hint or client.price(symbol)
            return _finalize(pos, exit_price, reason, client)
        if pos["market_type"] == "futures":
            _cancel_backup(client, pos)  # remove the bot's own backup stop
            qty = client.futures_positions([symbol]).get(symbol, 0.0)
            if qty <= 0:
                # Already closed on Binance (backup stop fired, or closed by hand)
                exit_price, why, pnl = _gone_exit(client, pos, price_hint)
                return _finalize(pos, exit_price, why, client, pnl=pnl)
            amount = client.amount_to_precision(symbol, min(qty, float(pos["amount"])))
            order = client.market_order(symbol, close_side, amount, reduce_only=True)
        else:
            free_base = client.base_free(symbol)
            amount = client.amount_to_precision(symbol, min(free_base, float(pos["amount"])))
            price_now = price_hint or client.price(symbol)
            rules = client.market_rules(symbol)
            if amount <= 0 or amount * price_now < max(rules["min_cost"], 1.0):
                # The coin is no longer in the wallet (sold manually on Binance)
                return _finalize(pos, price_now, "EXTERNAL", client)
            order = client.market_order(symbol, "sell", amount)
        exit_price = order["average"] or price_hint or client.price(symbol)
        return _finalize(pos, exit_price, reason, client)
    except Exception as e:
        _release(pos["id"])
        msg = str(e) if isinstance(e, TradeError) else _error_text(e)
        log_event(pos["user_id"], "ERROR", f"Could not close {pos['symbol']}: {msg}", mode=pos["mode"])
        raise TradeError(msg)


# ===========================================================================
# Background engine
# ===========================================================================
_engine_started = False
_engine_start_lock = threading.Lock()
_error_throttle = {}


def _throttled_log(user_id, mode, level, message, every_sec=600):
    key = (user_id, mode, message[:80])
    now = time.time()
    if now - _error_throttle.get(key, 0) >= every_sec:
        _error_throttle[key] = now
        log_event(user_id, level, message, mode=mode)


def _heartbeat():
    with _Cursor() as cur:
        cur.execute("UPDATE autotrade_engine SET heartbeat_at=%s, host=%s WHERE id=1", (_utcnow(), socket.gethostname()[:100]))
        if cur.rowcount == 0:
            cur.execute("SELECT id FROM autotrade_engine WHERE id=1")
            if cur.fetchone() is None:
                cur.execute("INSERT INTO autotrade_engine (id, heartbeat_at, host) VALUES (1,%s,%s)",
                            (_utcnow(), socket.gethostname()[:100]))


def recover_stuck_positions():
    """If the server died mid-close, put 'CLOSING' positions back to 'OPEN'
    so the monitor checks them again."""
    cutoff = _utcnow() - timedelta(minutes=2)
    with _Cursor() as cur:
        cur.execute("UPDATE autotrade_positions SET status='OPEN' WHERE status='CLOSING' AND status_changed_at < %s",
                    (cutoff,))


def monitor_positions():
    with _Cursor() as cur:
        cur.execute("SELECT * FROM autotrade_positions WHERE status='OPEN'")
        rows = cur.fetchall() or []
    groups = {}
    for r in rows:
        groups.setdefault((r["user_id"], r["mode"]), []).append(r)

    for (user_id, mode), positions in groups.items():
        try:
            account = load_account(user_id, mode)
            if not account:
                continue
            for market_type in ("spot", "futures", "forex"):
                group = [p for p in positions if p["market_type"] == market_type]
                if not group:
                    continue
                client = get_client(account, market_type)
                symbols = sorted({p["symbol"] for p in group})
                prices = client.prices(symbols)
                paper = getattr(client, "is_paper", False)
                live_futures = client.futures_positions(symbols) if (
                    market_type in ("futures", "forex") or paper) else None
                for pos in group:
                    _check_position(pos, account, client, prices.get(pos["symbol"]), live_futures)
        except Exception as e:
            msg = str(e) if isinstance(e, (TradeError, RuntimeError)) else _friendly_exchange_error(e)
            _throttled_log(user_id, mode, "ERROR", f"Position monitor error: {msg}")


def _stop_reason(pos):
    """Close reason when the stop is hit: before TP1 a normal stop, after TP1 the stop sat at the entry,
    after TP2 at TP1."""
    return {0: "SL", 1: "SL_ENTRY"}.get(int(pos.get("hits") or 0), "SL_TP1")


def _next_target(pos):
    """The next partial target (TP1, then TP2) not reached yet, or None (then only the stop / TP3 remain)."""
    hits = int(pos.get("hits") or 0)
    levels = [pos.get("tp1"), pos.get("tp2")]
    if hits < len(levels) and levels[hits] is not None:
        return float(levels[hits])
    return None


def _beyond(side, price, level):
    return price >= level if side == "LONG" else price <= level


def _replace_backup_stop(pos, client, stop, amount):
    """Binance futures: move the safety stop with the bot's stop (it sits 25% of the starting risk beyond it)."""
    side, symbol = pos["side"], pos["symbol"]
    dist = abs(float(pos["entry_price"]) - float(pos.get("initial_stop") or pos["stop_loss"]))
    level = stop - dist * BACKUP_STOP_EXTRA if side == "LONG" else stop + dist * BACKUP_STOP_EXTRA
    try:
        _cancel_backup(client, pos)
        sid = client.backup_stop(symbol, "sell" if side == "LONG" else "buy", amount, client.price_to_precision(symbol, level))
        with _Cursor() as cur:
            cur.execute("UPDATE autotrade_positions SET backup_stop_id=%s WHERE id=%s", (sid, pos["id"]))
    except Exception as e:
        log_event(pos["user_id"], "WARN", f"{symbol}: backup stop could not be moved on Binance ({type(e).__name__}). "
                                          f"The bot keeps watching the stop itself.", mode=pos["mode"])


def take_partial(pos, account, client, price):
    """Price reached the next target (TP1 or TP2): close a third of the starting size and move the stop
    (to the entry after TP1, to TP1 after TP2). A position too small to split keeps its size and only the
    stop moves. Returns True when the step was done."""
    if not _claim(pos["id"]):
        return False
    k = int(pos.get("hits") or 0)
    symbol, side = pos["symbol"], pos["side"]
    paper = getattr(client, "is_paper", False)
    entry = float(pos["entry_price"])
    remaining = float(pos["amount"])
    initial = _f(pos.get("initial_amount"), remaining) or remaining
    new_stop = client.price_to_precision(symbol, entry if k == 0 else float(pos["tp1"]))
    label = f"TP{k + 1}"
    try:
        rules = client.market_rules(symbol)
        min_cost = max(rules["min_cost"], 5.0)
        part = client.amount_to_precision(symbol, min(remaining, initial * PARTIAL_SHARE))
        left = remaining - part
        can_split = (part > 0 and part >= rules["min_amount"] and part * price >= min_cost
                     and left >= rules["min_amount"] and left * price >= min_cost)
        realized = _f(pos.get("realized_usdt"), 0.0)
        if can_split:
            close_side = "sell" if side == "LONG" else "buy"
            if pos["market_type"] == "spot" and not paper:
                order = client.market_order(symbol, "sell", part)
            else:
                order = client.market_order(symbol, close_side, part, reduce_only=True)
            fill = order.get("average") or price
            done = order.get("filled") or part
            pnl_part = _pnl(pos, fill, done)
            remaining = max(0.0, remaining - done)
            realized += pnl_part
            what = f"closed {done:g} @ {fill:g} ({'+' if pnl_part >= 0 else ''}{pnl_part:.2f} USDT)"
        else:
            what = "too small to split, so the whole size stays open"
        with _Cursor() as cur:
            cur.execute(
                """UPDATE autotrade_positions SET amount=%s, realized_usdt=%s, hits=%s, stop_loss=%s, status='OPEN',
                   status_changed_at=%s WHERE id=%s""",
                (remaining, round(realized, 4), k + 1, new_stop, _utcnow(), pos["id"]),
            )
        if paper:
            try:
                client.attach_tpsl(symbol, new_stop, float(pos["take_profit"]))
            except Exception:
                pass  # the bot watches the stop itself as well
        elif pos["market_type"] == "forex":
            try:
                client.set_stop(pos.get("backup_stop_id") or pos.get("entry_order_id"), symbol, new_stop)
            except Exception as e:
                log_event(pos["user_id"], "WARN", f"{symbol}: the stop could not be moved at the broker "
                                                  f"({type(e).__name__}). The bot keeps watching it itself.",
                          mode=pos["mode"])
        elif pos["market_type"] == "futures":
            _replace_backup_stop(pos, client, new_stop, remaining)
        log_event(pos["user_id"], "TRADE",
                  f"{label} {side} {symbol}: {what}; stop moved to {'the entry' if k == 0 else 'TP1'} ({new_stop:g})",
                  mode=pos["mode"])
        return True
    except Exception as e:
        _release(pos["id"])
        msg = str(e) if isinstance(e, TradeError) else _error_text(e)
        log_event(pos["user_id"], "ERROR", f"Could not take {label} on {symbol}: {msg}", mode=pos["mode"])
        return False


def _check_position(pos, account, client, price, live_futures):
    age = (_utcnow() - (_to_dt(pos["opened_at"]) or _utcnow())).total_seconds()
    settle = 5 if getattr(client, "is_paper", False) else 60   # the demo account updates at once
    if live_futures is not None and age > settle and live_futures.get(pos["symbol"], 0.0) <= 0:
        # Position no longer exists on the exchange -> its stop / target fired, or it was closed by hand
        if _claim(pos["id"]):
            try:
                exit_price, why, pnl = _gone_exit(client, pos, price)
                if why == "SL":
                    why = _stop_reason(pos)
                _cancel_backup(client, pos)
                _finalize(pos, exit_price, why, client, pnl=pnl)
            except Exception:
                _release(pos["id"])
                raise
        return
    if price is None:
        return
    unreal = _pnl(pos, price)
    with _Cursor() as cur:
        cur.execute("UPDATE autotrade_positions SET last_price=%s, unrealized_pnl=%s WHERE id=%s AND status='OPEN'",
                    (price, unreal, pos["id"]))
    stop, final = float(pos["stop_loss"]), float(pos["take_profit"])
    nxt = _next_target(pos)
    if nxt is not None and _beyond(pos["side"], price, nxt) and not _beyond(pos["side"], stop, price) \
            and not _beyond(pos["side"], price, final):
        take_partial(pos, account, client, price)
        return
    hit = level_hit(pos["side"], price, stop, final)
    if hit:
        try:
            close_position(pos, account, hit if hit == "TP" else _stop_reason(pos), price_hint=price)
        except TradeError:
            pass  # already logged


def _realized_today(user_id, mode, cur):
    start = _utcnow().replace(hour=0, minute=0, second=0)
    cur.execute(
        """SELECT COALESCE(SUM(pnl_usdt),0) AS s FROM autotrade_positions
           WHERE user_id=%s AND mode=%s AND status='CLOSED' AND closed_at >= %s""",
        (user_id, mode, start),
    )
    row = cur.fetchone() or {}
    return _f(row.get("s"), 0.0)


def _daily_guard(user_id, mode, settings, client):
    """Daily loss limit check. True = new trades allowed."""
    today = _utcnow().strftime("%Y-%m-%d")
    with _Cursor() as cur:
        if settings.get("day_ref_date") != today or not settings.get("day_ref_balance"):
            free, total = client.balance_usdt()
            ref = total or free
            cur.execute("UPDATE autotrade_bot_settings SET day_ref_date=%s, day_ref_balance=%s WHERE user_id=%s AND mode=%s",
                        (today, ref, user_id, mode))
            settings["day_ref_balance"] = ref
            settings["day_ref_date"] = today
        realized = _realized_today(user_id, mode, cur)
        limit_usdt = float(settings["day_ref_balance"] or 0) * float(settings["daily_loss_limit_pct"]) / 100.0
        if limit_usdt > 0 and realized <= -limit_usdt:
            tomorrow = (_utcnow() + timedelta(days=1)).replace(hour=0, minute=0, second=0)
            cur.execute("UPDATE autotrade_bot_settings SET paused_until=%s, pause_reason=%s WHERE user_id=%s AND mode=%s",
                        (tomorrow, "Daily loss limit", user_id, mode))
            log_event(user_id, "WARN",
                      f"Daily loss limit reached ({settings['daily_loss_limit_pct']}% = {limit_usdt:.2f} USDT). "
                      f"No new trades until 00:00 UTC.", cur, mode=mode)
            return False
    return True


def scan_user(user_id, mode, between_coins=None):
    """For one user's bot on one account: check signals and trade when all
    conditions are met."""
    settings = load_settings(user_id, mode)
    account = load_account(user_id, mode)
    if not settings["enabled"] or not account:
        return
    with _Cursor() as cur:
        cur.execute("UPDATE autotrade_bot_settings SET last_scan_at=%s WHERE user_id=%s AND mode=%s",
                    (_utcnow(), user_id, mode))
    paused_until = _to_dt(settings.get("paused_until"))
    if paused_until and paused_until > _utcnow():
        return

    try:
        client = get_client(account, settings["market_type"])
        if not _daily_guard(user_id, mode, settings, client):
            return
    except Exception as e:
        _throttled_log(user_id, mode, "ERROR", f"Scan stopped: {_error_text(e)}")
        return

    tf = settings["timeframe"]
    cooldown = timedelta(minutes=TIMEFRAME_MINUTES[tf])
    with _Cursor() as cur:
        open_now = _open_positions(user_id, mode, cur)
        cur.execute(
            """SELECT symbol, MAX(closed_at) AS last_closed FROM autotrade_positions
               WHERE user_id=%s AND mode=%s AND status='CLOSED' GROUP BY symbol""",
            (user_id, mode),
        )
        last_closed = {r["symbol"]: _to_dt(r["last_closed"]) for r in (cur.fetchall() or [])}
    open_symbols = {p["symbol"] for p in open_now}
    open_count = len(open_now)
    notes = []

    for symbol in settings["coins"]:
        if between_coins:
            between_coins()
        short = symbol.split("/")[0]
        if symbol in open_symbols:
            notes.append(f"{short}: position open")
            continue
        lc = last_closed.get(symbol)
        if lc and _utcnow() - lc < cooldown:
            notes.append(f"{short}: cooldown")
            continue
        if open_count >= settings["max_open_positions"]:
            notes.append(f"{short}: max positions")
            continue
        try:
            sig = get_signal(symbol, tf, max_age_sec=TIMEFRAME_SCAN_SEC[tf] - 10)
        except Exception as e:
            notes.append(f"{short}: signal error")
            _throttled_log(user_id, mode, "ERROR", f"No signal for {symbol}: {str(e)[:120]}")
            continue
        verdict, conf = sig.get("verdict"), sig.get("confidence") or 0.0
        if verdict not in ("LONG", "SHORT"):
            notes.append(f"{short}: WAIT {conf:.0f}%")
            continue
        if conf < settings["min_confidence"]:
            notes.append(f"{short}: {verdict} {conf:.0f}% (low confidence)")
            continue
        if settings["market_type"] == "spot" and verdict == "SHORT":
            notes.append(f"{short}: SHORT {conf:.0f}% (no shorts on spot)")
            continue
        try:
            open_position(user_id, account, settings, symbol, verdict, signal=sig, source="BOT")
            open_count += 1
            open_symbols.add(symbol)
            notes.append(f"{short}: {verdict} {conf:.0f}% -> TRADE")
        except TradeError as e:
            notes.append(f"{short}: {verdict} {conf:.0f}% (skipped)")
            _throttled_log(user_id, mode, "WARN", f"{symbol} trade skipped: {e}", every_sec=1800)
    log_event(user_id, "SCAN", f"Scan {tf}: " + " | ".join(notes), mode=mode)


# ===========================================================================
# Site signals -> bot (the hand-off): every new signal of the site goes to
# every bot whose filters it matches, once, with the signal's own levels.
# ===========================================================================
PERMANENT_SKIPS = ("SHORT is not possible on spot", "has no forex", "trades forex only", "is not available on")


def _sig_for_order(row):
    """A site signal (signal_hub.signal_json) -> what open_position needs."""
    entry, stop = float(row["entry"]), float(row["stop_loss"])
    tr = [float(x) for x in (row.get("targets_r") or [0.5, 1.0, 2.0])]
    return {"signal_id": int(row["id"]), "timeframe": row.get("timeframe"), "market": row.get("market"),
            "sl_pct": abs(entry - stop) / entry * 100 if entry else None, "targets_r": tr, "reward_risk": tr[-1],
            "confidence": _f(row.get("confidence")), "grade": row.get("grade"), "entry": entry, "stop_loss": stop}


def signal_wanted(settings, row, mode=None):
    """Does this bot account want this site signal? -> (True, None) or (False, reason).
    Real-money accounts copy only streams that passed their test on unseen data; demo accounts copy every
    stream that is switched on, so an untested stream can be watched without risking money."""
    if mode in LIVE_MODES and row.get("tested") is False:
        return False, "untested stream: live accounts copy only tested streams"
    if row.get("market") not in (settings.get("markets") or ["crypto"]):
        return False, "market not selected"
    if row.get("timeframe") not in (settings.get("timeframes") or ["4h"]):
        return False, "timeframe not selected"
    g = row.get("grade") or "C"
    if GRADES.index(g if g in GRADES else "C") > GRADES.index(settings.get("min_grade") or "C"):
        return False, f"grade {g} is below the minimum"
    if settings.get("symbols_mode") == "list" and row["symbol"] not in (settings.get("coins") or []):
        return False, "symbol not in the bot's list"
    conf = _f(row.get("confidence"))
    if conf is not None and str(row.get("engine") or "").startswith("v") and conf < float(settings["min_confidence"]):
        return False, f"confidence {conf:.0f}% is below the minimum"
    return True, None


def entry_check(sig, price):
    """None when the price is still a good entry for the signal, else the reason."""
    if price is None:
        return "no live price"
    entry, stop = float(sig["entry"]), float(sig["stop_loss"])
    risk = abs(entry - stop)
    if risk <= 0:
        return "the signal has no stop distance"
    sgn = 1 if sig["side"] == "LONG" else -1
    moved = sgn * (float(price) - entry) / risk
    first = float((sig.get("targets_r") or [0.5])[0])
    if moved <= -1:
        return "the price is already past the stop"
    if moved >= first:
        return "the price already reached TP1"
    if abs(moved) > MAX_ENTRY_DRIFT_R:
        return f"the price is {moved:+.2f} R away from the entry (limit {MAX_ENTRY_DRIFT_R} R)"
    return None


def _order_get(cur, uid, mode, sid):
    cur.execute("SELECT * FROM autotrade_signal_orders WHERE user_id=%s AND mode=%s AND signal_id=%s", (uid, mode, sid))
    return cur.fetchone()


def _order_set(uid, mode, sid, status, reason=None, position_id=None, source="BOT", next_try=None, bump=False):
    now = _utcnow()
    with _Cursor() as cur:
        row = _order_get(cur, uid, mode, sid)
        if row and row["status"] == "placed" and status != "placed":
            return                      # a placed signal keeps its record (a later refusal does not overwrite it)
        if row:
            cur.execute("""UPDATE autotrade_signal_orders SET status=%s, reason=%s, position_id=COALESCE(%s, position_id),
                           next_try_at=%s, attempts=attempts+%s, updated_at=%s WHERE id=%s""",
                        (status, (reason or "")[:300] or None, position_id, next_try, 1 if bump else 0, now, row["id"]))
        else:
            cur.execute("""INSERT INTO autotrade_signal_orders (user_id, mode, signal_id, status, reason, attempts, next_try_at,
                           position_id, source, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (uid, mode, sid, status, (reason or "")[:300] or None, 1 if bump else 0, next_try, position_id,
                         source, now, now))


def _window_end(row):
    sec = {"15m": 900, "1h": 3600, "4h": 14400}.get(row.get("timeframe"), 14400)
    start = _to_dt(row.get("signal_at"))
    return start + timedelta(seconds=sec * ENTRY_WINDOW_BARS) if start else None


def trade_signal(uid, mode, row, source="BOT", dry_run=False, settings=None, account=None):
    """Places one site signal on one account (also the 'Trade this signal' button).
    Returns open_position's result; raises TradeError with a clear reason."""
    if (row.get("status") or "ACTIVE") != "ACTIVE":
        raise TradeError("This signal is no longer active.")
    if int(row.get("hits") or 0) > 0:
        raise TradeError("This signal already reached TP1, so it is too late to enter.")
    with _Cursor() as cur:
        _ensure_settings_row(uid, mode, cur)
        settings = settings or load_settings(uid, mode, cur)
        account = account or load_account(uid, mode, cur)
        prev = _order_get(cur, uid, mode, int(row["id"]))
    if not account:
        raise TradeError(f"Connect your {_label(mode)} account first.")
    if prev and prev["status"] == "placed":
        raise TradeError(f"This signal was already traded on your {_label(mode)} account.")
    sig = _sig_for_order(row)
    sig["side"] = row["side"]
    fx = markets.is_forex(row["symbol"])
    try:
        client = get_client(account, "forex" if fx else settings["market_type"])
        price = _retry(client.price, row["symbol"])
    except TradeError:
        raise
    except Exception as e:
        raise TradeError(_error_text(e) if not isinstance(e, RuntimeError) else str(e))
    why = entry_check(sig, price)
    if why:
        raise TradeError(f"Not entered: {why}.")
    return open_position(uid, account, settings, row["symbol"], row["side"], signal=sig, source=source, dry_run=dry_run)


def _bot_accounts():
    with _Cursor() as cur:
        cur.execute("""SELECT s.user_id, s.mode FROM autotrade_bot_settings s
                       JOIN autotrade_exchange_accounts a ON a.user_id = s.user_id AND a.mode = s.mode
                       WHERE s.enabled = 1""")
        return [(r["user_id"], r["mode"]) for r in (cur.fetchall() or [])]


def bridge_step(now=None):
    """One pass: new site signals -> every bot account that wants them. Returns {(uid, mode): [results]}."""
    feed = _hooks.get("signal_feed")
    if not feed:
        return {}
    now = now or _utcnow()
    signals = sorted(feed(now) or [], key=lambda r: (str(r.get("signal_at")), r["id"]))
    if not signals:
        return {}
    out = {}
    for uid, mode in _bot_accounts():
        settings = load_settings(uid, mode)
        account = load_account(uid, mode)
        if not settings["enabled"] or not account:
            continue
        paused_until = _to_dt(settings.get("paused_until"))
        if paused_until and paused_until > now:
            continue
        with _Cursor() as cur:
            ids = [int(r["id"]) for r in signals]
            cur.execute("SELECT * FROM autotrade_signal_orders WHERE user_id=%s AND mode=%s AND signal_id IN ("
                        + ",".join(["%s"] * len(ids)) + ")", (uid, mode, *ids))
            seen = {int(r["signal_id"]): r for r in (cur.fetchall() or [])}
        guard_checked = False
        res = []
        for row in signals:
            ok, _ = signal_wanted(settings, row, mode)
            if not ok:
                continue
            sid = int(row["id"])
            prev = seen.get(sid)
            if prev and prev["status"] in ("placed", "skipped", "failed"):
                continue
            if prev and prev["status"] == "retry" and _to_dt(prev.get("next_try_at")) and _to_dt(prev["next_try_at"]) > now:
                continue
            end = _window_end(row)
            if not guard_checked:
                guard_checked = True
                try:
                    gclient = get_client(account, "forex" if mode in FX_MODES else settings["market_type"])
                    if not _daily_guard(uid, mode, settings, gclient):
                        break
                except Exception as e:
                    _throttled_log(uid, mode, "ERROR", f"Bot paused for this pass: {_error_text(e)}")
                    break
            label = f"{row['symbol']} {row.get('timeframe')} {row['side']} (signal #{sid})"
            try:
                r = trade_signal(uid, mode, row, source="BOT", settings=settings, account=account)
                _order_set(uid, mode, sid, "placed", position_id=r.get("id"), bump=True)
                res.append((sid, "placed"))
            except TradeError as e:
                msg = str(e)
                attempts = int((prev or {}).get("attempts") or 0) + 1
                permanent = any(w in msg for w in PERMANENT_SKIPS)
                expired = end is not None and now >= end
                if permanent or expired or attempts > len(RETRY_BACKOFF_SEC):
                    _order_set(uid, mode, sid, "skipped", msg, bump=True)
                    log_event(uid, "WARN", f"Skipped {label}: {msg}", mode=mode)
                    res.append((sid, "skipped"))
                else:
                    wait = RETRY_BACKOFF_SEC[min(attempts - 1, len(RETRY_BACKOFF_SEC) - 1)]
                    _order_set(uid, mode, sid, "retry", msg, next_try=now + timedelta(seconds=wait), bump=True)
                    if attempts == 1:
                        log_event(uid, "INFO", f"Waiting to enter {label}: {msg} Next try in {wait}s.", mode=mode)
                    res.append((sid, "retry"))
            except Exception as e:
                _order_set(uid, mode, sid, "retry", _error_text(e), next_try=now + timedelta(seconds=60), bump=True)
                _throttled_log(uid, mode, "ERROR", f"{label}: {_error_text(e)}")
                res.append((sid, "error"))
        with _Cursor() as cur:
            cur.execute("UPDATE autotrade_bot_settings SET last_scan_at=%s WHERE user_id=%s AND mode=%s", (now, uid, mode))
        out[(uid, mode)] = res
    return out


def _due_bots():
    with _Cursor() as cur:
        cur.execute(
            """SELECT s.user_id, s.mode, s.timeframe, s.last_scan_at FROM autotrade_bot_settings s
               JOIN autotrade_exchange_accounts a ON a.user_id = s.user_id AND a.mode = s.mode
               WHERE s.enabled = 1"""
        )
        rows = cur.fetchall() or []
    now = _utcnow()
    due = []
    for r in rows:
        last = _to_dt(r.get("last_scan_at"))
        interval = TIMEFRAME_SCAN_SEC.get(r.get("timeframe"), 300)
        if last is None or (now - last).total_seconds() >= interval:
            due.append((r["user_id"], r["mode"]))
    return due


def prune_logs():
    cutoff = _utcnow() - timedelta(days=LOG_RETENTION_DAYS)
    with _Cursor() as cur:
        cur.execute("DELETE FROM autotrade_logs WHERE created_at < %s", (cutoff,))


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        print(f"[autotrade] {getattr(fn, '__name__', fn)} failed:\n{traceback.format_exc()}")
        return None


def _engine_loop(lock_conn):
    _safe(recover_stuck_positions)
    state = {"last_monitor": 0.0, "last_scan_check": 0.0, "last_prune": 0.0}

    def maybe_monitor():
        if time.time() - state["last_monitor"] >= MONITOR_INTERVAL_SEC:
            state["last_monitor"] = time.time()
            _safe(_heartbeat)
            _safe(monitor_positions)

    while True:
        lock_conn.ping(reconnect=False)  # lost lock connection -> exception -> re-acquire the lock
        maybe_monitor()
        now = time.time()
        if _hooks.get("signal_feed"):
            if now - state["last_scan_check"] >= 15:     # site signals -> bots
                state["last_scan_check"] = now
                _safe(bridge_step)
        elif now - state["last_scan_check"] >= 30:       # no signal hub: the old per-coin polling
            state["last_scan_check"] = now
            for uid, mode in _safe(_due_bots) or []:
                _safe(scan_user, uid, mode, between_coins=maybe_monitor)
        if now - state["last_prune"] >= 6 * 3600:
            state["last_prune"] = now
            _safe(prune_logs)
        time.sleep(1)


def _engine_supervisor():
    time.sleep(5)
    while True:
        lock_conn = None
        try:
            lock_conn = get_db_connection()
            with lock_conn.cursor() as c:
                c.execute("SELECT GET_LOCK(%s, 0) AS got", (ENGINE_LOCK_NAME,))
                got = (c.fetchone() or {}).get("got")
            if got != 1:
                lock_conn.close()
                lock_conn = None
                time.sleep(30)  # another worker/instance runs the engine
                continue
            print(f"[autotrade] engine started on {socket.gethostname()} (monitor every {MONITOR_INTERVAL_SEC}s)")
            _engine_loop(lock_conn)
        except Exception:
            print(f"[autotrade] engine error, restarting in 10s:\n{traceback.format_exc()}")
        finally:
            if lock_conn is not None:
                try:
                    lock_conn.close()
                except Exception:
                    pass
        time.sleep(10)


def start_engine():
    global _engine_started
    if (os.environ.get("AUTOTRADE_ENGINE", "on") or "on").lower() in ("0", "off", "false", "no"):
        print("[autotrade] engine disabled via AUTOTRADE_ENGINE")
        return
    with _engine_start_lock:
        if _engine_started:
            return
        _engine_started = True
    threading.Thread(target=_engine_supervisor, name="autotrade-engine", daemon=True).start()


def init_autotrade(app=None, *, get_candles=None, generate_signal=None, data_exchange=None,
                   available_coins=None, start=True, engine_signal=None, signal_feed=None, signal_get=None,
                   forex_price=None):
    """Called from main.py. Injects the signal functions, creates tables and
    starts the background engine."""
    for k, v in (("signal_feed", signal_feed), ("signal_get", signal_get), ("forex_price", forex_price)):
        if v is not None:
            _hooks[k] = v
    if get_candles and not _hooks["get_candles"]:
        _hooks["get_candles"] = get_candles
    if generate_signal and not _hooks["generate_signal"]:
        _hooks["generate_signal"] = generate_signal
    if engine_signal and not _hooks["engine_signal"]:
        _hooks["engine_signal"] = engine_signal
    if data_exchange is not None and _hooks["data_exchange"] is None:
        _hooks["data_exchange"] = data_exchange
    if available_coins and not _hooks["available_coins"]:
        _hooks["available_coins"] = list(available_coins)
    try:
        init_tables()
    except Exception as e:
        print(f"[autotrade] WARNING: could not create tables: {e}")
    if not encryption_ready():
        print("[autotrade] WARNING: AUTOTRADE_ENCRYPTION_KEY is not set - Binance connect is disabled.")
    if start:
        start_engine()


# ===========================================================================
# Serializers
# ===========================================================================
def _pos_json(p):
    return {
        "id": p["id"], "symbol": p["symbol"], "side": p["side"], "source": p["source"],
        "market_type": p["market_type"], "mode": p["mode"], "amount": _f(p["amount"]),
        "entry_price": _f(p["entry_price"]), "stop_loss": _f(p["stop_loss"]),
        "take_profit": _f(p["take_profit"]), "leverage": int(p["leverage"] or 1),
        "notional_usdt": _f(p["notional_usdt"]), "confidence": _f(p.get("confidence")),
        "status": p["status"], "last_price": _f(p.get("last_price")),
        "unrealized_pnl": _f(p.get("unrealized_pnl"), 0.0), "exit_price": _f(p.get("exit_price")),
        "pnl_usdt": _f(p.get("pnl_usdt")), "close_reason": p.get("close_reason"),
        "opened_at": _iso(_to_dt(p.get("opened_at"))), "closed_at": _iso(_to_dt(p.get("closed_at"))),
        "targets": ([_f(p.get("tp1")), _f(p.get("tp2")), _f(p["take_profit"])] if p.get("tp1") is not None else None),
        "hits": int(p.get("hits") or 0), "realized_usdt": _f(p.get("realized_usdt"), 0.0),
        "initial_amount": _f(p.get("initial_amount")), "initial_stop": _f(p.get("initial_stop")),
        "signal_id": p.get("signal_id"), "timeframe": p.get("timeframe"), "market": p.get("market") or (
            "forex" if p.get("market_type") == "forex" else "crypto"),
    }


def _settings_json(s):
    return {k: s.get(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}


def _recent_signal_orders(uid, mode, limit=15):
    try:
        with _Cursor() as cur:
            cur.execute("""SELECT signal_id, status, reason, attempts, position_id, source, updated_at FROM autotrade_signal_orders
                           WHERE user_id=%s AND mode=%s ORDER BY updated_at DESC, id DESC LIMIT %s""", (uid, mode, limit))
            rows = cur.fetchall() or []
    except Exception:
        return []
    return [{"signal_id": r["signal_id"], "status": r["status"], "reason": r.get("reason"), "attempts": r["attempts"],
             "position_id": r.get("position_id"), "source": r.get("source"),
             "updated_at": _iso(_to_dt(r.get("updated_at")))} for r in rows]


def _confidence_levels():
    """Confidence (%) the signal engine shows at its 'standard' and 'strong' signal levels, and the highest it can
    show, so the settings form can say which minimum takes which signals. None if the engine model is missing."""
    try:
        import signal_engine as se
        m = se.model()

        def pct(raw):
            return round(float(m.calibrate(raw)) * 100, 1)
        return {"standard": pct(m.floor), "strong": pct(m.threshold), "max": round(float(m.cal_y[-1]) * 100, 1)}
    except Exception:
        return None


# ===========================================================================
# Routes
# ===========================================================================
def _require_json_post():
    if not request.is_json:
        return _err("A JSON request is required.", 415)
    return None


def _req_mode(data=None):
    """Account ('paper' = Site demo, 'demo' = Binance Demo, 'live') from the JSON body or the query string."""
    mode = None
    if data is not None:
        mode = data.get("account")
    if not mode:
        mode = request.args.get("account")
    mode = str(mode or "").lower()
    return mode if mode in MODES else None


@autotrade_bp.route("/auto-trading", methods=["GET"])
def auto_trading_page():
    if not _user_id():
        return redirect(url_for("login_page"))
    return render_template("auto-trading.html", user_email=session.get("email", ""))


_balance_failures = {}


@autotrade_bp.route("/api/autotrade/status", methods=["GET"])
def status():
    uid = _user_id()
    if not uid:
        return _err("Login required.", 401)
    mode = _req_mode()
    if not mode:
        return _err("Unknown account. Choose Site demo, Binance demo, Binance live, OANDA demo or OANDA live.")
    with _Cursor() as cur:
        _ensure_settings_row(uid, mode, cur)
        settings = load_settings(uid, mode, cur)
        account = load_account(uid, mode, cur)
        cur.execute("""SELECT * FROM autotrade_positions WHERE user_id=%s AND mode=%s AND status IN ('OPEN','CLOSING')
                       ORDER BY opened_at DESC""", (uid, mode))
        open_rows = cur.fetchall() or []
        cur.execute("""SELECT * FROM autotrade_positions WHERE user_id=%s AND mode=%s AND status='CLOSED'
                       ORDER BY closed_at DESC LIMIT 25""", (uid, mode))
        history = cur.fetchall() or []
        cur.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END),0) AS wins,
               COALESCE(SUM(pnl_usdt),0) AS total FROM autotrade_positions
               WHERE user_id=%s AND mode=%s AND status='CLOSED'""",
            (uid, mode),
        )
        agg = cur.fetchone() or {}
        realized_today = _realized_today(uid, mode, cur)
        cur.execute("""SELECT id, level, message, created_at FROM autotrade_logs
                       WHERE user_id=%s AND (mode=%s OR mode IS NULL) ORDER BY id DESC LIMIT 40""", (uid, mode))
        logs = cur.fetchall() or []
        cur.execute("SELECT heartbeat_at FROM autotrade_engine WHERE id=1")
        hb = (cur.fetchone() or {}).get("heartbeat_at")
        # Summary of both accounts for the account switcher
        cur.execute("SELECT mode, key_hint FROM autotrade_exchange_accounts WHERE user_id=%s", (uid,))
        connected = {r["mode"]: r for r in (cur.fetchall() or [])}
        cur.execute("SELECT mode, enabled FROM autotrade_bot_settings WHERE user_id=%s", (uid,))
        enabled = {r["mode"]: int(r["enabled"] or 0) for r in (cur.fetchall() or [])}
        cur.execute("""SELECT mode, COUNT(*) AS n FROM autotrade_positions WHERE user_id=%s AND status IN ('OPEN','CLOSING')
                       GROUP BY mode""", (uid,))
        open_counts = {r["mode"]: int(r["n"]) for r in (cur.fetchall() or [])}

    # Refresh the balance from Binance if older than 90s. After a failure,
    # wait 60s before retrying (do not hammer Binance on every 5s poll).
    if account and (mode == "paper" or encryption_ready()):
        bal_at = _to_dt(account.get("balance_at"))
        stale = mode == "paper" or bal_at is None or (_utcnow() - bal_at).total_seconds() > 90 or \
            account.get("balance_market") != settings["market_type"]
        fail_key = (uid, mode)
        recent_fail = _balance_failures.get(fail_key)
        if stale and recent_fail and time.time() - recent_fail[0] < 60:
            account["balance_error"] = recent_fail[1]
        elif stale:
            try:
                bal_market = "forex" if mode in FX_MODES else settings["market_type"]
                client = get_client(account, bal_market)
                free, total = client.balance_usdt()
                with _Cursor() as cur:
                    _store_balance(uid, mode, free, total, bal_market, cur)
                account.update({"balance_usdt": free, "balance_total_usdt": total, "balance_at": _utcnow(),
                                "balance_market": settings["market_type"]})
                _balance_failures.pop(fail_key, None)
            except Exception as e:
                account["balance_error"] = _error_text(e)
                _balance_failures[fail_key] = (time.time(), account["balance_error"])

    hb_dt = _to_dt(hb)
    n = int(_f(agg.get("n"), 0))
    wins = int(_f(agg.get("wins"), 0))
    paused_until = _to_dt(settings.get("paused_until"))
    is_paused = bool(paused_until and paused_until > _utcnow())
    return jsonify({
        "ok": True,
        "mode": mode,
        "ready": {"encryption": encryption_ready(), "ccxt": ccxt is not None},
        "accounts": {m: {"connected": m in connected or m == "paper",
                         "key_hint": (connected.get(m) or {}).get("key_hint"),
                         "bot_enabled": bool(enabled.get(m)),
                         "open_positions": open_counts.get(m, 0)} for m in MODES},
        "connected": bool(account),
        "account": None if not account else {
            "mode": account["mode"], "key_hint": account.get("key_hint"),
            "balance_usdt": _f(account.get("balance_usdt")),
            "balance_total_usdt": _f(account.get("balance_total_usdt")),
            "balance_market": account.get("balance_market"),
            "balance_at": _iso(_to_dt(account.get("balance_at"))),
            "balance_error": account.get("balance_error"),
            "connected_at": _iso(_to_dt(account.get("connected_at"))),
        },
        "settings": _settings_json(settings),
        "bot": {
            "enabled": bool(settings["enabled"]),
            "paused_until": _iso(paused_until) if is_paused else None,
            "pause_reason": settings.get("pause_reason") if is_paused else None,
            "last_scan_at": _iso(_to_dt(settings.get("last_scan_at"))),
        },
        "engine": {
            "online": bool(hb_dt and (_utcnow() - hb_dt).total_seconds() < 90),
            "heartbeat_at": _iso(hb_dt),
        },
        "positions": [_pos_json(p) for p in open_rows],
        "history": [_pos_json(p) for p in history],
        "stats": {
            "realized_today": round(realized_today, 2),
            "unrealized": round(sum(_f(p.get("unrealized_pnl"), 0.0) for p in open_rows), 2),
            "closed_count": n, "wins": wins, "losses": n - wins,
            "win_rate": round(wins / n * 100, 1) if n else None,
            "total_pnl": round(_f(agg.get("total"), 0.0), 2),
        },
        "logs": [{"id": l["id"], "level": l["level"], "message": l["message"],
                  "created_at": _iso(_to_dt(l["created_at"]))} for l in logs],
        "options": {"leverage": list(ALLOWED_LEVERAGE), "timeframes": list(TIMEFRAME_SCAN_SEC),
                    "coins": coin_choices(), "symbols": signal_symbols(), "forex_symbols": list(markets.forex_signal_pairs()),
                    "max_coins": MAX_COINS, "confidence": _confidence_levels(), "markets": list(MARKETS),
                    "signal_timeframes": list(SIGNAL_TIMEFRAMES), "grades": list(GRADES),
                    "live_modes": list(LIVE_MODES), "fx_modes": list(FX_MODES),
                    "signal_feed": bool(_hooks.get("signal_feed")), "streams_live": _streams_live(),
                    "streams_tested": _streams_tested()},
        "signal_orders": _recent_signal_orders(uid, mode),
        "server_time": _iso(_utcnow()),
    })


def _streams_live():
    """Signal streams that are running on the site."""
    try:
        import signal_hub
        return signal_hub.enabled_streams()
    except Exception:
        return []


def _streams_tested():
    """The running streams that passed their test on unseen data (the only ones live accounts copy)."""
    try:
        import signal_hub
        return [k for k in signal_hub.enabled_streams() if signal_hub.stream_tested(*k.split("_"))]
    except Exception:
        return []


@autotrade_bp.route("/api/autotrade/connect", methods=["POST"])
def connect():
    uid = _user_id()
    if not uid:
        return _err("Login required.", 401)
    bad = _require_json_post()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    mode = _req_mode(data)
    if not mode:
        return _err("Unknown account. Choose Site demo, Binance demo, Binance live, OANDA demo or OANDA live.")
    if mode == "paper":
        return _err("The Site demo account needs no API keys: it trades your Demo trading account on this site.")
    if mode in FX_MODES:
        return _connect_oanda(uid, mode, data)
    if not encryption_ready():
        return _err("AUTOTRADE_ENCRYPTION_KEY is not set on the server, so keys cannot be stored. "
                    "Add it in Railway Variables.", 503)
    api_key = str(data.get("api_key") or "").strip()
    secret = str(data.get("api_secret") or "").strip()
    if len(api_key) < 16 or len(secret) < 16:
        return _err("Paste both the full API key and the secret key.")

    with _Cursor() as cur:
        _ensure_settings_row(uid, mode, cur)
        settings = load_settings(uid, mode, cur)
        if _open_positions(uid, mode, cur):
            return _err(f"The {_label(mode)} account has open positions. Close them before changing keys.", 409)

    try:
        client = _client_factory(api_key, secret, mode, settings["market_type"])
        free, total = client.balance_usdt()
    except Exception as e:
        return _err(f"Verification failed: {_error_text(e)}")

    note = None
    if mode == "live":
        restrictions = client.api_restrictions()
        if not restrictions:
            # Fail closed: if the key's permissions cannot be read, we cannot prove withdrawals are off.
            return _err("Could not verify this API key's permissions with Binance, so it was not saved. "
                        "Please try again in a minute.", 503)
        if restrictions.get("enableWithdrawals"):
            return _err("This API key has WITHDRAWALS enabled. For safety, disable that permission on Binance "
                        "(keep only Reading + Spot/Futures Trading) and connect again.")
        if not restrictions.get("ipRestrict"):
            note = "Tip: restrict this API key to your server's IP address on Binance for extra safety."

    now = _utcnow()
    with _Cursor() as cur:
        cur.execute("DELETE FROM autotrade_exchange_accounts WHERE user_id=%s AND mode=%s", (uid, mode))
        cur.execute(
            """INSERT INTO autotrade_exchange_accounts (user_id, mode, exchange, api_key_enc, api_secret_enc, key_hint,
               balance_usdt, balance_total_usdt, balance_market, balance_at, connected_at)
               VALUES (%s,%s,'binance',%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uid, mode, _encrypt(api_key), _encrypt(secret), "..." + api_key[-4:], free, total,
             settings["market_type"], now, now),
        )
        cur.execute("UPDATE autotrade_bot_settings SET enabled=0 WHERE user_id=%s AND mode=%s", (uid, mode))
        log_event(uid, "INFO", f"{_label(mode)} connected. {settings['market_type']} balance: {free:.2f} USDT",
                  cur, mode=mode)
    _drop_clients(uid, mode)
    return jsonify({"ok": True, "mode": mode, "balance_usdt": free, "balance_total_usdt": total, "note": note})


def _connect_oanda(uid, mode, data):
    if not encryption_ready():
        return _err("AUTOTRADE_ENCRYPTION_KEY is not set on the server, so keys cannot be stored. "
                    "Add it in Railway Variables.", 503)
    token = str(data.get("api_key") or "").strip()
    account_id = str(data.get("account_id") or data.get("api_secret") or "").strip()
    if len(token) < 20 or not re.fullmatch(r"[0-9]{3}-[0-9]{3}-[0-9]+-[0-9]{3}", account_id):
        return _err("Paste the OANDA API token and the account ID (it looks like 101-004-1234567-001).")
    with _Cursor() as cur:
        _ensure_settings_row(uid, mode, cur)
        if _open_positions(uid, mode, cur):
            return _err(f"The {_label(mode)} account has open positions. Close them before changing keys.", 409)
    try:
        client = _oanda_factory(token, account_id, "live" if mode == "oanda_live" else "practice")
        free, total = client.balance_usdt()
    except Exception as e:
        return _err(f"Verification failed: {str(e)[:200]}")
    now = _utcnow()
    with _Cursor() as cur:
        cur.execute("DELETE FROM autotrade_exchange_accounts WHERE user_id=%s AND mode=%s", (uid, mode))
        cur.execute(
            """INSERT INTO autotrade_exchange_accounts (user_id, mode, exchange, api_key_enc, api_secret_enc, key_hint,
               balance_usdt, balance_total_usdt, balance_market, balance_at, connected_at)
               VALUES (%s,%s,'oanda',%s,%s,%s,%s,%s,'forex',%s,%s)""",
            (uid, mode, _encrypt(token), _encrypt(account_id), "..." + account_id[-4:], free, total, now, now),
        )
        cur.execute("UPDATE autotrade_bot_settings SET enabled=0 WHERE user_id=%s AND mode=%s", (uid, mode))
        log_event(uid, "INFO", f"{_label(mode)} connected. Balance: {total:.2f} USD", cur, mode=mode)
    _drop_clients(uid, mode)
    return jsonify({"ok": True, "mode": mode, "balance_usdt": free, "balance_total_usdt": total,
                    "note": "The OANDA API cannot withdraw money; the token only trades and reads this account."})


@autotrade_bp.route("/api/autotrade/disconnect", methods=["POST"])
def disconnect():
    uid = _user_id()
    if not uid:
        return _err("Login required.", 401)
    mode = _req_mode(request.get_json(silent=True) or {})
    if not mode:
        return _err("Unknown account. Choose Site demo, Binance demo, Binance live, OANDA demo or OANDA live.")
    if mode == "paper":
        return _err("The Site demo account has nothing to disconnect. Turn its bot off instead.")
    with _Cursor() as cur:
        if _open_positions(uid, mode, cur):
            return _err("Close all open positions on this account first, then disconnect.", 409)
        cur.execute("DELETE FROM autotrade_exchange_accounts WHERE user_id=%s AND mode=%s", (uid, mode))
        cur.execute("UPDATE autotrade_bot_settings SET enabled=0 WHERE user_id=%s AND mode=%s", (uid, mode))
        log_event(uid, "INFO", f"{_label(mode)} disconnected. API keys deleted.", cur, mode=mode)
    _drop_clients(uid, mode)
    return jsonify({"ok": True})


@autotrade_bp.route("/api/autotrade/settings", methods=["POST"])
def update_settings():
    uid = _user_id()
    if not uid:
        return _err("Login required.", 401)
    bad = _require_json_post()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    mode = _req_mode(data)
    if not mode:
        return _err("Unknown account. Choose Site demo, Binance demo, Binance live, OANDA demo or OANDA live.")
    with _Cursor() as cur:
        _ensure_settings_row(uid, mode, cur)
        current = load_settings(uid, mode, cur)
        clean, err = validate_settings(data, current)
        if err:
            return _err(err)
        save_settings(uid, mode, clean, cur)
        log_event(uid, "INFO",
                  f"Settings saved: {clean['market_type']} {clean['leverage']}x, risk {clean['risk_pct']}%, "
                  f"min confidence {clean['min_confidence']}%, {clean['timeframe']}, {len(clean['coins'])} coins",
                  cur, mode=mode)
    return jsonify({"ok": True, "settings": clean})


@autotrade_bp.route("/api/autotrade/bot", methods=["POST"])
def toggle_bot():
    uid = _user_id()
    if not uid:
        return _err("Login required.", 401)
    bad = _require_json_post()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    mode = _req_mode(data)
    if not mode:
        return _err("Unknown account. Choose Site demo, Binance demo, Binance live, OANDA demo or OANDA live.")
    enable = bool(data.get("enabled"))
    with _Cursor() as cur:
        _ensure_settings_row(uid, mode, cur)
        account = load_account(uid, mode, cur)
        if enable:
            if not account:
                return _err(f"Connect your {_label(mode)} account first.")
            if mode in LIVE_MODES and not data.get("ack_live"):
                return _err("Please confirm the risk warning to run the bot on your live account.", 428, need_ack=True)
            cur.execute("""UPDATE autotrade_bot_settings SET enabled=1, last_scan_at=NULL, paused_until=NULL,
                           pause_reason=NULL WHERE user_id=%s AND mode=%s""", (uid, mode))
            log_event(uid, "INFO", f"{_label(mode)} bot turned ON.", cur, mode=mode)
        else:
            cur.execute("UPDATE autotrade_bot_settings SET enabled=0 WHERE user_id=%s AND mode=%s", (uid, mode))
            log_event(uid, "INFO", f"{_label(mode)} bot turned OFF. Open positions are still monitored for SL/TP.",
                      cur, mode=mode)
    return jsonify({"ok": True, "enabled": enable, "mode": mode})


@autotrade_bp.route("/api/autotrade/positions/<int:position_id>/close", methods=["POST"])
def close_one(position_id):
    uid = _user_id()
    if not uid:
        return _err("Login required.", 401)
    with _Cursor() as cur:
        cur.execute("SELECT * FROM autotrade_positions WHERE id=%s AND user_id=%s", (position_id, uid))
        pos = cur.fetchone()
        account = load_account(uid, pos["mode"], cur) if pos else None
    if not pos or pos["status"] != "OPEN":
        return _err("Position not found or already closing.", 404)
    if not account:
        return _err(f"The {_label(pos['mode'])} account is not connected.")
    try:
        pnl = close_position(pos, account, "MANUAL")
    except TradeError as e:
        return _err(str(e))
    return jsonify({"ok": True, "pnl_usdt": pnl})


@autotrade_bp.route("/api/autotrade/panic", methods=["POST"])
def panic():
    """Emergency: turn the bot off and close every open position on the
    given account (or on both accounts with account='all')."""
    uid = _user_id()
    if not uid:
        return _err("Login required.", 401)
    data = request.get_json(silent=True) or {}
    requested = str(data.get("account") or request.args.get("account") or "").lower()
    modes = MODES if requested == "all" else ((requested,) if requested in MODES else None)
    if not modes:
        return _err("Unknown account (or 'all').")
    closed, failed = 0, []
    for mode in modes:
        with _Cursor() as cur:
            cur.execute("UPDATE autotrade_bot_settings SET enabled=0 WHERE user_id=%s AND mode=%s", (uid, mode))
            account = load_account(uid, mode, cur)
            cur.execute("SELECT * FROM autotrade_positions WHERE user_id=%s AND mode=%s AND status='OPEN'", (uid, mode))
            positions = cur.fetchall() or []
            log_event(uid, "WARN", f"STOP ALL: {_label(mode)} bot OFF, closing {len(positions)} position(s).",
                      cur, mode=mode)
        for pos in positions:
            if not account:
                failed.append({"symbol": pos["symbol"], "error": "Account not connected"})
                continue
            try:
                close_position(pos, account, "PANIC")
                closed += 1
            except TradeError as e:
                failed.append({"symbol": pos["symbol"], "error": str(e)})
    return jsonify({"ok": not failed, "closed": closed, "failed": failed})


@autotrade_bp.route("/api/autotrade/manual", methods=["POST"])
def manual_trade():
    """Manual trade. confirm=false -> preview only (size/SL/TP), confirm=true -> order."""
    uid = _user_id()
    if not uid:
        return _err("Login required.", 401)
    bad = _require_json_post()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    mode = _req_mode(data)
    if not mode:
        return _err("Unknown account. Choose Site demo, Binance demo, Binance live, OANDA demo or OANDA live.")
    symbol = str(data.get("symbol") or "").upper()
    side = str(data.get("side") or "").upper()
    if symbol not in coin_choices() and symbol not in signal_symbols():
        return _err("This coin is not supported.")
    with _Cursor() as cur:
        _ensure_settings_row(uid, mode, cur)
        settings = load_settings(uid, mode, cur)
        account = load_account(uid, mode, cur)
    if not account:
        return _err(f"Connect your {_label(mode)} account first.")
    if mode in LIVE_MODES and data.get("confirm") and not data.get("ack_live"):
        return _err("Live trades need an explicit confirmation.", 428, need_ack=True)
    try:
        sig = get_signal(symbol, settings["timeframe"], max_age_sec=600)
    except Exception:
        sig = None
    try:
        result = open_position(uid, account, settings, symbol, side, signal=sig, source="MANUAL",
                               dry_run=not data.get("confirm"))
    except TradeError as e:
        return _err(str(e))
    result["signal"] = sig
    result["executed"] = bool(data.get("confirm"))
    return jsonify({"ok": True, **result})


@autotrade_bp.route("/api/autotrade/signal", methods=["GET"])
def signal_summary():
    if not _user_id():
        return _err("Login required.", 401)
    symbol = str(request.args.get("symbol") or "BTC/USDT").upper()
    timeframe = request.args.get("timeframe") or "1h"
    if symbol not in coin_choices() or timeframe not in TIMEFRAME_SCAN_SEC:
        return _err("Invalid coin or timeframe.")
    try:
        return jsonify({"ok": True, **get_signal(symbol, timeframe, max_age_sec=300)})
    except Exception as e:
        return _err(f"Signal not available: {str(e)[:150]}")


# ---------------------------------------------------------------------------
# Assistant: free questions about the bot (assistant.py). The page runs its own
# commands first ("start bot", "buy btc", ...) and sends everything else here.
# ---------------------------------------------------------------------------
AI_PER_HOUR = int(os.environ.get("ASSISTANT_AI_PER_HOUR", "40") or 40)   # AI answers per user per hour (cost guard)
_ai_usage = {}
_ai_usage_lock = threading.Lock()


def _ai_allowed(uid):
    now = time.time()
    with _ai_usage_lock:
        stamps = [t for t in _ai_usage.get(uid, []) if now - t < 3600]
        ok = len(stamps) < AI_PER_HOUR
        if ok:
            stamps.append(now)
        _ai_usage[uid] = stamps
        return ok


def _mentioned_coin(text):
    words = set(re.findall(r"[a-z0-9]+", str(text or "").lower()))
    for c in coin_choices():
        if c.split("/")[0].lower() in words:
            return c
    return None


def _assistant_ctx(uid, mode, question):
    with _Cursor() as cur:
        _ensure_settings_row(uid, mode, cur)
        settings = load_settings(uid, mode, cur)
        account = load_account(uid, mode, cur)
        open_rows = _open_positions(uid, mode, cur)
        cur.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END),0) AS wins,
               COALESCE(SUM(pnl_usdt),0) AS total FROM autotrade_positions WHERE user_id=%s AND mode=%s AND status='CLOSED'""",
            (uid, mode))
        agg = cur.fetchone() or {}
        today = _realized_today(uid, mode, cur)
        cur.execute("""SELECT level, message, created_at FROM autotrade_logs WHERE user_id=%s AND (mode=%s OR mode IS NULL)
                       ORDER BY id DESC LIMIT 30""", (uid, mode))
        logs = cur.fetchall() or []
        cur.execute("SELECT heartbeat_at FROM autotrade_engine WHERE id=1")
        hb = _to_dt((cur.fetchone() or {}).get("heartbeat_at"))
    n, wins = int(_f(agg.get("n"), 0)), int(_f(agg.get("wins"), 0))
    balance = _f(account.get("balance_usdt")) if account else None
    if account and mode == "paper":
        try:
            balance = get_client(account, settings["market_type"]).balance_usdt()[0]
        except Exception:
            pass
    paused_until = _to_dt(settings.get("paused_until"))
    scan = next((l["message"] for l in logs if l["level"] == "SCAN"), None)
    ctx = {
        "account_label": _label(mode), "mode": mode, "connected": bool(account), "balance": balance,
        "engine_online": bool(hb and (_utcnow() - hb).total_seconds() < 90),
        "settings": _settings_json(settings),
        "bot": {"enabled": bool(settings["enabled"]), "last_scan_at": _iso(_to_dt(settings.get("last_scan_at"))),
                "paused_until": _iso(paused_until) if paused_until and paused_until > _utcnow() else None,
                "pause_reason": settings.get("pause_reason")},
        "positions": [_pos_json(p) for p in open_rows],
        "stats": {"closed_count": n, "wins": wins, "win_rate": round(wins / n * 100, 1) if n else None,
                  "total_pnl": round(_f(agg.get("total"), 0.0), 2), "realized_today": round(today, 2)},
        "last_scan": scan,
        "recent_logs": [f"{l['level']}: {l['message']}" for l in logs if l["level"] in ("WARN", "ERROR", "TRADE")][:6],
    }
    sym = _mentioned_coin(question)
    if sym:
        coin = {"symbol": sym, "price": None, "change_pct": None, "signal": None}
        ex = _hooks.get("data_exchange")
        try:
            if ex is not None:
                t = ex.fetch_ticker(sym)
                coin["price"], coin["change_pct"] = _f(t.get("last")), _f(t.get("percentage"))
        except Exception:
            pass
        try:
            coin["signal"] = get_signal(sym, settings["timeframe"], max_age_sec=600)
            if coin["price"] is None:
                coin["price"] = coin["signal"].get("last_price")
        except Exception:
            pass
        ctx["coin"] = coin
    return ctx


@autotrade_bp.route("/api/autotrade/assistant", methods=["POST"])
def assistant_route():
    uid = _user_id()
    if not uid:
        return _err("Login required.", 401)
    bad = _require_json_post()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    mode = _req_mode(data) or "paper"
    question = str(data.get("message") or "").strip()[:600]
    history = data.get("history") if isinstance(data.get("history"), list) else []
    import assistant
    try:
        ctx = _assistant_ctx(uid, mode, question)
    except Exception as e:
        print(f"[autotrade] assistant context failed: {e}")
        ctx = {"account_label": _label(mode), "mode": mode}
    use_ai = assistant.ai_enabled() and _ai_allowed(uid)   # over the hourly AI quota: built-in answers only
    out = assistant.answer(question, ctx, history, use_ai=use_ai)
    return jsonify({"ok": True, **out, "ai_available": assistant.ai_enabled()})


_price_cache = {"ts": 0.0, "key": None, "data": None}


def _signal_row(sid):
    fn = _hooks.get("signal_get")
    return fn(int(sid)) if fn else None


@autotrade_bp.route("/api/autotrade/trade-signal", methods=["POST"])
def trade_signal_route():
    """'Trade this signal': confirm=false -> preview (size, levels), confirm=true -> order on the chosen account."""
    uid = _user_id()
    if not uid:
        return _err("Login required.", 401)
    bad = _require_json_post()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    mode = _req_mode(data)
    if not mode:
        return _err("Unknown account. Choose Site demo, Binance demo, Binance live, OANDA demo or OANDA live.")
    try:
        sid = int(data.get("signal_id"))
    except (TypeError, ValueError):
        return _err("Invalid signal.")
    row = _signal_row(sid)
    if not row:
        return _err("Signal not found.", 404)
    confirm = bool(data.get("confirm"))
    if mode in LIVE_MODES and confirm and not data.get("ack_live"):
        return _err("Live trades need an explicit confirmation.", 428, need_ack=True)
    try:
        result = trade_signal(uid, mode, row, source="SIGNAL", dry_run=not confirm)
    except TradeError as e:
        if confirm:
            try:
                _order_set(uid, mode, sid, "failed", str(e), source="SIGNAL", bump=True)
            except Exception:
                pass
        return _err(str(e))
    if confirm:
        _order_set(uid, mode, sid, "placed", position_id=result.get("id"), source="SIGNAL", bump=True)
    result["executed"] = confirm
    result["account_label"] = _label(mode)
    return jsonify({"ok": True, **result})


@autotrade_bp.route("/api/autotrade/signal-status", methods=["GET"])
def signal_status_route():
    """For the signals page: what the user's bots did with each signal (placed / waiting / skipped) and the result."""
    uid = _user_id()
    if not uid:
        return _err("Login required.", 401)
    try:
        ids = [int(x) for x in (request.args.get("ids") or "").split(",") if x.strip()][:100]
    except ValueError:
        return _err("Invalid ids.")
    if not ids:
        return jsonify({"ok": True, "signals": {}})
    ph = ",".join(["%s"] * len(ids))
    with _Cursor() as cur:
        cur.execute(f"SELECT * FROM autotrade_signal_orders WHERE user_id=%s AND signal_id IN ({ph})", (uid, *ids))
        orders = cur.fetchall() or []
        pids = [o["position_id"] for o in orders if o.get("position_id")]
        pos = {}
        if pids:
            cur.execute(f"SELECT * FROM autotrade_positions WHERE user_id=%s AND id IN ({','.join(['%s'] * len(pids))})",
                        (uid, *pids))
            pos = {p["id"]: p for p in (cur.fetchall() or [])}
    out = {}
    for o in orders:
        p = pos.get(o.get("position_id"))
        out.setdefault(str(o["signal_id"]), []).append({
            "account": o["mode"], "label": _label(o["mode"]), "status": o["status"], "reason": o.get("reason"),
            "source": o.get("source"),
            "position": None if not p else {"status": p["status"], "pnl_usdt": _f(p.get("pnl_usdt")),
                                            "unrealized_pnl": _f(p.get("unrealized_pnl")), "hits": int(p.get("hits") or 0),
                                            "close_reason": p.get("close_reason")}})
    return jsonify({"ok": True, "signals": out})


@autotrade_bp.route("/api/autotrade/prices", methods=["GET"])
def prices():
    """Live price + 24h change for the coin list (public market data)."""
    if not _user_id():
        return _err("Login required.", 401)
    allowed = set(coin_choices())
    symbols = [s for s in (request.args.get("symbols") or "").upper().split(",") if s in allowed][:MAX_COINS + 4]
    if not symbols:
        return jsonify({"ok": True, "prices": {}})
    key = ",".join(sorted(symbols))
    if _price_cache["key"] == key and time.time() - _price_cache["ts"] < 10:
        return jsonify({"ok": True, "prices": _price_cache["data"]})
    ex = _hooks.get("data_exchange")
    if ex is None:
        return _err("Market data is not available.")
    try:
        tickers = ex.fetch_tickers(symbols)
        out = {s: {"last": _f(tickers[s].get("last")), "change_pct": _f(tickers[s].get("percentage"))}
               for s in symbols if s in tickers}
    except Exception as e:
        return _err(f"Prices not available: {str(e)[:120]}")
    _price_cache.update({"ts": time.time(), "key": key, "data": out})
    return jsonify({"ok": True, "prices": out})
