"""
Auto-Trade Bot (Binance) - SignalX / Signals FM
================================================

Ye module "Auto-Trade" feature ko REAL banata hai. Purana HTML mockup sirf
random numbers dikhata tha; ye module:

  1. User ki Binance API keys SERVER par ENCRYPTED save karta hai
     (browser/localStorage mein kabhi nahi).
  2. Binance se asal balance, asal price aur asal market orders (ccxt) use
     karta hai - Demo Trading (risk-free) ya Live.
  3. Trade ka faisla SignalX ke apne signal engine (generate_signal ->
     final_verdict + confidence_pct) se leta hai, random se nahi.
  4. Har trade par Stop-Loss + Take-Profit, risk-based position size,
     max open positions, rozana loss limit aur leverage cap lagata hai.
  5. Ek background engine (thread) positions monitor karta hai aur
     SL/TP hit hone par position band karta hai. Futures par exchange ke
     upar ek "backup stop" bhi lagta hai, taake server band ho jaye tab
     bhi nuqsan mehdood rahe.
  6. Emergency "Stop All" (panic) button: bot band + saari positions close.

ZAROORI ENVIRONMENT VARIABLES
  AUTOTRADE_ENCRYPTION_KEY  (zaroori) - Fernet key. Banane ka tareeqa:
      python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
      Ye key kabhi change na karein, warna saved API keys decrypt nahi hongi.
  AUTOTRADE_ENGINE          (optional) - "off" kar dein to background bot
      nahi chalega (sirf manual trades + monitoring band).
  AUTOTRADE_MONITOR_SEC     (optional) - positions check karne ka waqfa
      (default 10 seconds).

IMPORTANT LIMITATIONS (sach):
  - Signal engine ki koi walk-forward backtesting nahi hui (main.py ka
    disclaimer dekhein). Bot profit ki guarantee nahi deta.
  - Pehle kam az kam 2-4 hafte Binance DEMO mode mein chalayein.
  - Spot market mein sirf LONG (buy) trades hoti hain; SHORT sirf futures.
"""

import json
import math
import os
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from db import get_db_connection

try:  # ccxt requirements.txt mein pehle se hai
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
# Hard safety limits (server-side - frontend inhein bypass nahi kar sakta)
# ---------------------------------------------------------------------------
ALLOWED_LEVERAGE = (1, 2, 3, 5, 10, 20)
TIMEFRAME_SCAN_SEC = {"15m": 180, "1h": 300, "4h": 600}
TIMEFRAME_MINUTES = {"15m": 15, "1h": 60, "4h": 240}
MAX_COINS = 8
SL_MIN_PCT = 0.4           # stop-loss kam az kam 0.4% door
SL_MAX_PCT = 6.0           # aur zyada se zyada 6%
SL_FALLBACK_PCT = 1.5      # agar signal se volatility na mile
LIQ_SAFETY_FRACTION = 0.6  # SL, liquidation distance ke 60% ke andar hona chahiye
BACKUP_STOP_EXTRA = 0.25   # futures backup stop, SL se 25% aur aage
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
}

ENGINE_LOCK_NAME = "signalx_autotrade_engine"
MONITOR_INTERVAL_SEC = max(5, int(os.environ.get("AUTOTRADE_MONITOR_SEC", "10") or 10))

# main.py se inject hone wale functions (circular import se bachne ke liye)
_hooks = {
    "get_candles": None,
    "generate_signal": None,
    "data_exchange": None,
    "available_coins": None,
}


# ===========================================================================
# Chhote helpers
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


class _Cursor:
    """`with _Cursor() as cur:` -> naya connection + cursor, end par close."""

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
        raise RuntimeError("AUTOTRADE_ENCRYPTION_KEY set nahi hai")
    return f.encrypt(text.encode()).decode()


def _decrypt(token):
    f = _fernet()
    if f is None:
        raise RuntimeError("AUTOTRADE_ENCRYPTION_KEY set nahi hai")
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        raise RuntimeError("Saved API keys decrypt nahi ho sakin (encryption key badal gayi?). "
                           "Binance dobara connect karein.")


# ===========================================================================
# Database tables
# ===========================================================================
def init_tables():
    with _Cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS autotrade_accounts (
                user_id INT PRIMARY KEY,
                exchange VARCHAR(20) NOT NULL DEFAULT 'binance',
                mode VARCHAR(10) NOT NULL DEFAULT 'demo',
                api_key_enc TEXT NOT NULL,
                api_secret_enc TEXT NOT NULL,
                key_hint VARCHAR(16) NULL,
                balance_usdt DOUBLE NULL,
                balance_total_usdt DOUBLE NULL,
                balance_market VARCHAR(10) NULL,
                balance_at DATETIME NULL,
                connected_at DATETIME NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS autotrade_settings (
                user_id INT PRIMARY KEY,
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
    print("[autotrade] tables ready")


def log_event(user_id, level, message, cur=None):
    message = str(message)[:500]
    sql = "INSERT INTO autotrade_logs (user_id, level, message, created_at) VALUES (%s,%s,%s,%s)"
    try:
        if cur is not None:
            cur.execute(sql, (user_id, level, message, _utcnow()))
        else:
            with _Cursor() as c:
                c.execute(sql, (user_id, level, message, _utcnow()))
    except Exception as e:  # logging kabhi engine ko crash na kare
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
    s["enabled"] = int(s["enabled"] or 0)
    s["leverage"] = int(s["leverage"] or 1)
    s["max_open_positions"] = int(s["max_open_positions"] or 1)
    for k in ("risk_pct", "max_position_usdt", "daily_loss_limit_pct", "min_confidence", "reward_risk"):
        s[k] = float(s[k])
    s["paused_until"] = row.get("paused_until")
    s["pause_reason"] = row.get("pause_reason")
    s["day_ref_date"] = row.get("day_ref_date")
    s["day_ref_balance"] = row.get("day_ref_balance")
    s["last_scan_at"] = row.get("last_scan_at")
    return s


def load_settings(user_id, cur=None):
    def _q(c):
        c.execute("SELECT * FROM autotrade_settings WHERE user_id=%s", (user_id,))
        return c.fetchone()

    row = _q(cur) if cur is not None else None
    if cur is None:
        with _Cursor() as c:
            row = _q(c)
    return _row_to_settings(row)


def validate_settings(raw, current=None):
    """Frontend se aaye settings ko check + clamp karta hai.
    Returns (clean_dict, error_message_or_None)."""
    cur = dict(current or DEFAULT_SETTINGS)
    raw = raw or {}
    clean = {}

    market_type = str(raw.get("market_type", cur["market_type"])).lower()
    if market_type not in ("spot", "futures"):
        return None, "Market type sirf 'spot' ya 'futures' ho sakta hai."
    clean["market_type"] = market_type

    try:
        leverage = int(raw.get("leverage", cur["leverage"]))
    except (TypeError, ValueError):
        return None, "Leverage ghalat hai."
    if leverage not in ALLOWED_LEVERAGE:
        return None, f"Leverage in mein se hona chahiye: {', '.join(str(x) for x in ALLOWED_LEVERAGE)}x"
    clean["leverage"] = 1 if market_type == "spot" else leverage

    def num(key, lo, hi, label):
        val = _f(raw.get(key, cur[key]))
        if val is None:
            raise ValueError(f"{label} ghalat hai.")
        if val < lo or val > hi:
            raise ValueError(f"{label} {lo} aur {hi} ke darmiyan hona chahiye.")
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
        return None, "Timeframe sirf 15m, 1h ya 4h ho sakta hai."
    clean["timeframe"] = timeframe

    coins = raw.get("coins", cur["coins"])
    if not isinstance(coins, list):
        return None, "Coins list ghalat hai."
    allowed = set(coin_choices())
    picked = []
    for c in coins:
        c = str(c).upper().strip()
        if c in allowed and c not in picked:
            picked.append(c)
    if not picked:
        return None, "Kam az kam ek coin chunein."
    if len(picked) > MAX_COINS:
        return None, f"Zyada se zyada {MAX_COINS} coins chun sakti hain."
    clean["coins"] = picked
    return clean, None


def save_settings(user_id, clean, cur):
    cur.execute("SELECT user_id FROM autotrade_settings WHERE user_id=%s", (user_id,))
    exists = cur.fetchone() is not None
    values = (
        clean["market_type"], clean["leverage"], clean["risk_pct"], clean["max_position_usdt"],
        clean["max_open_positions"], clean["daily_loss_limit_pct"], clean["min_confidence"],
        clean["timeframe"], clean["reward_risk"], json.dumps(clean["coins"]), _utcnow(),
    )
    if exists:
        cur.execute(
            """UPDATE autotrade_settings SET market_type=%s, leverage=%s, risk_pct=%s,
               max_position_usdt=%s, max_open_positions=%s, daily_loss_limit_pct=%s,
               min_confidence=%s, timeframe=%s, reward_risk=%s, coins=%s, updated_at=%s
               WHERE user_id=%s""",
            values + (user_id,),
        )
    else:
        cur.execute(
            """INSERT INTO autotrade_settings (market_type, leverage, risk_pct, max_position_usdt,
               max_open_positions, daily_loss_limit_pct, min_confidence, timeframe, reward_risk,
               coins, updated_at, user_id, enabled)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)""",
            values + (user_id,),
        )


def _ensure_settings_row(user_id, cur):
    cur.execute("SELECT user_id FROM autotrade_settings WHERE user_id=%s", (user_id,))
    if cur.fetchone() is None:
        save_settings(user_id, dict(DEFAULT_SETTINGS), cur)


# ===========================================================================
# Pure trading math (unit-testable, koi network/DB nahi)
# ===========================================================================
def plan_levels(side, entry_price, volatility_pct, reward_risk, leverage):
    """Entry price se Stop-Loss / Take-Profit nikalta hai.

    volatility_pct = signal ka `extreme_volatility_95_pct` (95th percentile
    candle move, %). Isi ko SL distance banaya jata hai (wahi logic jo
    main.py ka quantile_volatility() use karta hai), lekin full precision
    ke sath (main.py 2 decimals par round karta hai jo DOGE/PEPE jaise
    sastay coins ke liye ghalat ho jata hai).
    Returns (plan_dict, error_or_None)."""
    if side not in ("LONG", "SHORT"):
        return None, "Side LONG ya SHORT honi chahiye."
    entry_price = _f(entry_price)
    if not entry_price or entry_price <= 0:
        return None, "Price nahi mila."
    vol = _f(volatility_pct)
    sl_pct = _clamp(vol if vol and vol > 0 else SL_FALLBACK_PCT, SL_MIN_PCT, SL_MAX_PCT)
    leverage = int(leverage or 1)
    if leverage > 1:
        liq_pct = 100.0 / leverage
        if sl_pct > liq_pct * LIQ_SAFETY_FRACTION:
            return None, (f"{leverage}x leverage par stop-loss ({sl_pct:.2f}%) liquidation ke bohat qareeb hai. "
                          f"Leverage kam karein.")
    tp_pct = sl_pct * float(reward_risk or 1.5)
    if side == "LONG":
        sl = entry_price * (1 - sl_pct / 100)
        tp = entry_price * (1 + tp_pct / 100)
    else:
        sl = entry_price * (1 + sl_pct / 100)
        tp = entry_price * (1 - tp_pct / 100)
    return {"sl_pct": round(sl_pct, 3), "tp_pct": round(tp_pct, 3), "stop_loss": sl, "take_profit": tp}, None


def size_position(free_usdt, risk_pct, sl_pct, max_position_usdt, leverage, market_type):
    """Risk-based sizing: agar SL hit ho to nuqsan ~ (risk_pct % of balance).
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
# Binance client (ccxt wrapper)
# ===========================================================================
class BinanceClient:
    """ccxt.binance ka patla wrapper. Sirf yahi class network par jati hai,
    baaqi engine isi ke methods use karta hai (tests mein fake class)."""

    def __init__(self, api_key, secret, mode, market_type):
        if ccxt is None:
            raise RuntimeError("ccxt library install nahi hai (pip install ccxt).")
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
            # Binance ne Futures "testnet" ko "Demo Trading" se replace kar diya
            # hai; naye ccxt versions mein enable_demo_trading() hai.
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
        """Live account ki API permissions (withdrawal enabled hai ya nahi)."""
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
            self.ex.set_position_mode(False)  # One-way mode (hedge mode nahi)
        except Exception:
            pass
        try:
            self.ex.set_margin_mode("isolated", s)
        except Exception:
            pass  # pehle se isolated ho to Binance error deta hai
        self.ex.set_leverage(int(leverage), s)

    def futures_positions(self, symbols):
        """{ 'BTC/USDT': contracts } - sirf jin ki position khuli hai."""
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
    def market_order(self, symbol, side, amount, reduce_only=False):
        s = self.sym(symbol)
        params = {"reduceOnly": True} if (reduce_only and self.market_type == "futures") else {}
        o = self.ex.create_order(s, "market", side, amount, None, params)
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

    def backup_stop(self, symbol, close_side, amount, stop_price):
        o = self.ex.create_order(self.sym(symbol), "market", close_side, amount, None,
                                 {"stopLossPrice": stop_price, "reduceOnly": True})
        return str(o.get("id"))

    def order_average(self, order_id, symbol):
        try:
            o = self.ex.fetch_order(order_id, self.sym(symbol))
            if (o.get("status") == "closed") and o.get("average"):
                return _f(o["average"])
        except Exception:
            pass
        return None

    def cancel_all(self, symbol):
        try:
            self.ex.cancel_all_orders(self.sym(symbol))
        except Exception:
            pass


_client_cache = {}
_client_cache_lock = threading.Lock()
CLIENT_TTL_SEC = 1800


def _client_factory(api_key, secret, mode, market_type):
    return BinanceClient(api_key, secret, mode, market_type)


def get_client(account, market_type):
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


def _drop_clients(user_id):
    with _client_cache_lock:
        for k in [k for k in _client_cache if k[0] == user_id]:
            _client_cache.pop(k, None)


def load_account(user_id, cur=None):
    def _q(c):
        c.execute("SELECT * FROM autotrade_accounts WHERE user_id=%s", (user_id,))
        return c.fetchone()

    if cur is not None:
        return _q(cur)
    with _Cursor() as c:
        return _q(c)


def _friendly_exchange_error(e):
    name = type(e).__name__
    text = str(e)
    if "451" in text or "restricted location" in text.lower():
        return ("Binance is server ki location (country) se API access block karta hai. "
                "Server ko kisi aur region (non-US) mein deploy karein.")
    if name in ("AuthenticationError",) or "API-key" in text or "Invalid Api-Key" in text or "-2015" in text:
        return "API key/secret ghalat hai, ya is key ko is account type (Spot/Futures) ki permission nahi hai."
    if name == "InsufficientFunds" or "insufficient" in text.lower():
        return "Binance account mein kaafi USDT balance nahi hai."
    if name in ("NetworkError", "RequestTimeout", "ExchangeNotAvailable", "DDoSProtection"):
        return "Binance se connection nahi ho saka (network/timeout). Thori der baad dobara try karein."
    return f"Binance error: {text[:180]}"


# ===========================================================================
# Signals (SignalX engine)
# ===========================================================================
_signal_cache = {}
_signal_cache_lock = threading.Lock()


def get_signal(symbol, timeframe, max_age_sec=300):
    """SignalX ka apna generate_signal() chalata hai. Verdict/confidence
    sirf Hawkes+Bayesian (Conformal) se bante hain jo order-book par depend
    nahi karte, is liye include_orderbook=False (halka aur tez)."""
    key = (symbol, timeframe)
    now = time.time()
    with _signal_cache_lock:
        hit = _signal_cache.get(key)
        if hit and now - hit[0] <= max_age_sec:
            return hit[1]
    get_candles = _hooks.get("get_candles")
    generate_signal = _hooks.get("generate_signal")
    if not get_candles or not generate_signal:
        raise RuntimeError("Signal engine available nahi hai.")
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
# Open / close positions (engine + routes dono yahi use karte hain)
# ===========================================================================
class TradeError(Exception):
    pass


def _open_positions(user_id, cur):
    cur.execute(
        "SELECT * FROM autotrade_positions WHERE user_id=%s AND status IN ('OPEN','CLOSING') ORDER BY opened_at",
        (user_id,),
    )
    return cur.fetchall() or []


def _store_balance(user_id, free, total, market_type, cur):
    cur.execute(
        "UPDATE autotrade_accounts SET balance_usdt=%s, balance_total_usdt=%s, balance_market=%s, balance_at=%s WHERE user_id=%s",
        (free, total, market_type, _utcnow(), user_id),
    )


def open_position(user_id, account, settings, symbol, side, signal=None, source="BOT", dry_run=False):
    """Market order se nayi position kholta hai (SL/TP ke sath).
    dry_run=True -> sirf plan/size return karta hai, order nahi lagata."""
    market_type = settings["market_type"]
    leverage = int(settings["leverage"]) if market_type == "futures" else 1
    if side not in ("LONG", "SHORT"):
        raise TradeError("Side LONG ya SHORT honi chahiye.")
    if market_type == "spot" and side == "SHORT":
        raise TradeError("Spot market mein SHORT nahi ho sakta. Short ke liye Futures chunein.")

    with _Cursor() as cur:
        existing = _open_positions(user_id, cur)
    if any(p["symbol"] == symbol for p in existing):
        raise TradeError(f"{symbol} par pehle se ek position khuli hai.")
    if len(existing) >= int(settings["max_open_positions"]):
        raise TradeError(f"Max open positions ({settings['max_open_positions']}) poori ho chuki hain.")

    client = get_client(account, market_type)
    try:
        if not client.has_symbol(symbol):
            raise TradeError(f"Binance {market_type} par {symbol} market nahi mila.")
        free, total = client.balance_usdt()
        price = client.price(symbol)
        rules = client.market_rules(symbol)
    except TradeError:
        raise
    except Exception as e:
        raise TradeError(_friendly_exchange_error(e))

    vol = (signal or {}).get("volatility_pct")
    plan, err = plan_levels(side, price, vol, settings["reward_risk"], leverage)
    if err:
        raise TradeError(err)
    notional, risk_usdt = size_position(free, settings["risk_pct"], plan["sl_pct"],
                                        settings["max_position_usdt"], leverage, market_type)
    amount = client.amount_to_precision(symbol, notional / price) if notional > 0 else 0.0
    min_cost = max(rules["min_cost"], 5.0)
    if amount <= 0 or amount < rules["min_amount"] or amount * price < min_cost:
        raise TradeError(
            f"Trade size bohat chhota hai ({amount * price:.2f} USDT). Binance minimum ~{min_cost:.2f} USDT hai - "
            f"balance barhayein ya Risk %/Max position barhayein."
        )

    preview = {
        "symbol": symbol, "side": side, "market_type": market_type, "leverage": leverage,
        "price": price, "amount": amount, "notional_usdt": round(amount * price, 2),
        "margin_usdt": round(amount * price / leverage, 2), "risk_usdt": round(risk_usdt, 2),
        "stop_loss": client.price_to_precision(symbol, plan["stop_loss"]),
        "take_profit": client.price_to_precision(symbol, plan["take_profit"]),
        "sl_pct": plan["sl_pct"], "tp_pct": plan["tp_pct"], "free_usdt": round(free, 2),
        "mode": account["mode"],
    }
    if dry_run:
        return preview

    try:
        if market_type == "futures":
            client.prepare_futures(symbol, leverage)
        order = client.market_order(symbol, "buy" if side == "LONG" else "sell", amount)
    except Exception as e:
        raise TradeError(_friendly_exchange_error(e))

    fill = order["average"] or price
    filled = order["filled"] or amount
    plan, _ = plan_levels(side, fill, vol, settings["reward_risk"], 1)  # actual fill se levels
    sl = client.price_to_precision(symbol, plan["stop_loss"])
    tp = client.price_to_precision(symbol, plan["take_profit"])
    now = _utcnow()
    try:
        with _Cursor() as cur:
            cur.execute(
                """INSERT INTO autotrade_positions (user_id, mode, market_type, symbol, side, source, amount,
                   entry_price, stop_loss, take_profit, leverage, notional_usdt, fee_rate, confidence, status,
                   entry_order_id, last_price, unrealized_pnl, opened_at, status_changed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s,%s,0,%s,%s)""",
                (user_id, account["mode"], market_type, symbol, side, source, filled, fill, sl, tp, leverage,
                 round(filled * fill, 4), rules["taker"], (signal or {}).get("confidence"), order["id"],
                 fill, now, now),
            )
            position_id = cur.lastrowid
            try:
                free2, total2 = client.balance_usdt()
                _store_balance(user_id, free2, total2, market_type, cur)
            except Exception:
                pass
    except Exception as db_err:
        # Order lag gaya lekin DB mein save nahi hua -> foran wapas band karo
        try:
            client.market_order(symbol, "sell" if side == "LONG" else "buy", filled, reduce_only=True)
        except Exception:
            pass
        raise TradeError(f"Position save nahi ho saki, order wapas band kar diya gaya: {db_err}")

    if market_type == "futures":
        dist = abs(fill - sl)
        backup_price = sl - dist * BACKUP_STOP_EXTRA if side == "LONG" else sl + dist * BACKUP_STOP_EXTRA
        try:
            stop_id = client.backup_stop(symbol, "sell" if side == "LONG" else "buy", filled,
                                         client.price_to_precision(symbol, backup_price))
            with _Cursor() as cur:
                cur.execute("UPDATE autotrade_positions SET backup_stop_id=%s WHERE id=%s", (stop_id, position_id))
        except Exception as e:
            log_event(user_id, "WARN", f"{symbol}: Binance par backup stop nahi lag saka ({type(e).__name__}). "
                                       f"Bot khud SL monitor karega.")

    conf = (signal or {}).get("confidence")
    conf_txt = f", signal {conf:.0f}%" if conf else ""
    log_event(user_id, "TRADE",
              f"OPEN {side} {symbol} {filled:g} @ {fill:g} ({leverage}x{conf_txt}) | SL {sl:g} | TP {tp:g} [{source}]")
    preview.update({"id": position_id, "entry_price": fill, "amount": filled, "stop_loss": sl, "take_profit": tp})
    return preview


def _claim(position_id, from_status=("OPEN",)):
    """Position ko atomically 'CLOSING' mark karta hai taake engine aur
    user ka click dono ek sath close na karein. True = humne claim kiya."""
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


def _finalize(pos, exit_price, reason, client=None):
    pnl = calc_pnl(pos["side"], pos["entry_price"], exit_price, pos["amount"], pos["fee_rate"])
    with _Cursor() as cur:
        cur.execute(
            """UPDATE autotrade_positions SET status='CLOSED', exit_price=%s, pnl_usdt=%s, close_reason=%s,
               closed_at=%s, last_price=%s, unrealized_pnl=0, status_changed_at=%s WHERE id=%s""",
            (exit_price, pnl, reason, _utcnow(), exit_price, _utcnow(), pos["id"]),
        )
        if client is not None:
            try:
                free, total = client.balance_usdt()
                _store_balance(pos["user_id"], free, total, pos["market_type"], cur)
            except Exception:
                pass
    sign = "+" if pnl >= 0 else ""
    log_event(pos["user_id"], "TRADE",
              f"CLOSE {pos['side']} {pos['symbol']} @ {exit_price:g} | PnL {sign}{pnl:.2f} USDT [{reason}]")
    return pnl


def close_position(pos, account, reason, price_hint=None, already_claimed=False):
    """Position market order se band karta hai. Returns pnl (float)."""
    if not already_claimed and not _claim(pos["id"]):
        raise TradeError("Ye position pehle hi band ho rahi hai.")
    try:
        client = get_client(account, pos["market_type"])
        symbol = pos["symbol"]
        close_side = "sell" if pos["side"] == "LONG" else "buy"
        exit_price = None
        if pos["market_type"] == "futures":
            client.cancel_all(symbol)  # backup stop hata do
            qty = client.futures_positions([symbol]).get(symbol, 0.0)
            if qty <= 0:
                # Binance par pehle hi band (backup stop trigger / user ne khud band ki)
                exit_price = (client.order_average(pos["backup_stop_id"], symbol) if pos.get("backup_stop_id") else None)
                exit_price = exit_price or price_hint or client.price(symbol)
                return _finalize(pos, exit_price, "EXCHANGE", client)
            amount = client.amount_to_precision(symbol, min(qty, float(pos["amount"])))
            order = client.market_order(symbol, close_side, amount, reduce_only=True)
        else:
            free_base = client.base_free(symbol)
            amount = client.amount_to_precision(symbol, min(free_base, float(pos["amount"])))
            price_now = price_hint or client.price(symbol)
            rules = client.market_rules(symbol)
            if amount <= 0 or amount * price_now < max(rules["min_cost"], 1.0):
                # coin wallet mein hai hi nahi (user ne Binance par khud bech diya)
                return _finalize(pos, price_now, "EXTERNAL", client)
            order = client.market_order(symbol, "sell", amount)
        exit_price = order["average"] or price_hint or client.price(symbol)
        return _finalize(pos, exit_price, reason, client)
    except Exception as e:
        _release(pos["id"])
        msg = str(e) if isinstance(e, TradeError) else _friendly_exchange_error(e)
        log_event(pos["user_id"], "ERROR", f"{pos['symbol']} close nahi ho saki: {msg}")
        raise TradeError(msg)


# ===========================================================================
# Background engine
# ===========================================================================
_engine_started = False
_engine_start_lock = threading.Lock()
_error_throttle = {}


def _throttled_log(user_id, level, message, every_sec=600):
    key = (user_id, message[:80])
    now = time.time()
    if now - _error_throttle.get(key, 0) >= every_sec:
        _error_throttle[key] = now
        log_event(user_id, level, message)


def _heartbeat():
    with _Cursor() as cur:
        cur.execute("UPDATE autotrade_engine SET heartbeat_at=%s, host=%s WHERE id=1", (_utcnow(), socket.gethostname()[:100]))
        if cur.rowcount == 0:
            cur.execute("SELECT id FROM autotrade_engine WHERE id=1")
            if cur.fetchone() is None:
                cur.execute("INSERT INTO autotrade_engine (id, heartbeat_at, host) VALUES (1,%s,%s)",
                            (_utcnow(), socket.gethostname()[:100]))


def recover_stuck_positions():
    """Agar server close karte waqt band ho gaya ho to 'CLOSING' wali
    positions wapas 'OPEN' kar do - monitor unhein dobara check karega."""
    cutoff = _utcnow() - timedelta(minutes=2)
    with _Cursor() as cur:
        cur.execute("UPDATE autotrade_positions SET status='OPEN' WHERE status='CLOSING' AND status_changed_at < %s",
                    (cutoff,))


def monitor_positions():
    with _Cursor() as cur:
        cur.execute("SELECT * FROM autotrade_positions WHERE status='OPEN'")
        rows = cur.fetchall() or []
    by_user = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append(r)

    for user_id, positions in by_user.items():
        try:
            account = load_account(user_id)
            if not account:
                continue
            for market_type in ("spot", "futures"):
                group = [p for p in positions if p["market_type"] == market_type]
                if not group:
                    continue
                client = get_client(account, market_type)
                symbols = sorted({p["symbol"] for p in group})
                prices = client.prices(symbols)
                live_futures = client.futures_positions(symbols) if market_type == "futures" else None
                for pos in group:
                    _check_position(pos, account, client, prices.get(pos["symbol"]), live_futures)
        except Exception as e:
            msg = str(e) if isinstance(e, (TradeError, RuntimeError)) else _friendly_exchange_error(e)
            _throttled_log(user_id, "ERROR", f"Position monitor error: {msg}")


def _check_position(pos, account, client, price, live_futures):
    age = (_utcnow() - (_to_dt(pos["opened_at"]) or _utcnow())).total_seconds()
    if live_futures is not None and age > 60 and live_futures.get(pos["symbol"], 0.0) <= 0:
        # Binance par position ab mojood nahi -> backup stop chal gaya ya user ne khud band ki
        if _claim(pos["id"]):
            try:
                exit_price = (client.order_average(pos["backup_stop_id"], pos["symbol"])
                              if pos.get("backup_stop_id") else None) or price or pos["last_price"]
                client.cancel_all(pos["symbol"])
                _finalize(pos, exit_price, "EXCHANGE", client)
            except Exception:
                _release(pos["id"])
                raise
        return
    if price is None:
        return
    unreal = calc_pnl(pos["side"], pos["entry_price"], price, pos["amount"], pos["fee_rate"])
    with _Cursor() as cur:
        cur.execute("UPDATE autotrade_positions SET last_price=%s, unrealized_pnl=%s WHERE id=%s AND status='OPEN'",
                    (price, unreal, pos["id"]))
    hit = level_hit(pos["side"], price, float(pos["stop_loss"]), float(pos["take_profit"]))
    if hit:
        try:
            close_position(pos, account, hit, price_hint=price)
        except TradeError:
            pass  # already logged


def _realized_today(user_id, cur):
    start = _utcnow().replace(hour=0, minute=0, second=0)
    cur.execute(
        "SELECT COALESCE(SUM(pnl_usdt),0) AS s FROM autotrade_positions WHERE user_id=%s AND status='CLOSED' AND closed_at >= %s",
        (user_id, start),
    )
    row = cur.fetchone() or {}
    return _f(row.get("s"), 0.0)


def _daily_guard(user_id, settings, client):
    """Rozana loss limit check. True = naye trades allowed."""
    today = _utcnow().strftime("%Y-%m-%d")
    with _Cursor() as cur:
        if settings.get("day_ref_date") != today or not settings.get("day_ref_balance"):
            free, total = client.balance_usdt()
            ref = total or free
            cur.execute("UPDATE autotrade_settings SET day_ref_date=%s, day_ref_balance=%s WHERE user_id=%s",
                        (today, ref, user_id))
            settings["day_ref_balance"] = ref
            settings["day_ref_date"] = today
        realized = _realized_today(user_id, cur)
        limit_usdt = float(settings["day_ref_balance"] or 0) * float(settings["daily_loss_limit_pct"]) / 100.0
        if limit_usdt > 0 and realized <= -limit_usdt:
            tomorrow = (_utcnow() + timedelta(days=1)).replace(hour=0, minute=0, second=0)
            cur.execute("UPDATE autotrade_settings SET paused_until=%s, pause_reason=%s WHERE user_id=%s",
                        (tomorrow, "Daily loss limit", user_id))
            log_event(user_id, "WARN",
                      f"Aaj ka loss limit ({settings['daily_loss_limit_pct']}% = {limit_usdt:.2f} USDT) poora ho gaya. "
                      f"Bot kal (UTC 00:00) tak naye trades nahi lega.", cur)
            return False
    return True


def scan_user(user_id, between_coins=None):
    """Ek user ke liye: signals check karo aur conditions poori hon to trade lo."""
    settings = load_settings(user_id)
    account = load_account(user_id)
    if not settings["enabled"] or not account:
        return
    with _Cursor() as cur:
        cur.execute("UPDATE autotrade_settings SET last_scan_at=%s WHERE user_id=%s", (_utcnow(), user_id))
    paused_until = _to_dt(settings.get("paused_until"))
    if paused_until and paused_until > _utcnow():
        return

    try:
        client = get_client(account, settings["market_type"])
        if not _daily_guard(user_id, settings, client):
            return
    except Exception as e:
        msg = str(e) if isinstance(e, RuntimeError) else _friendly_exchange_error(e)
        _throttled_log(user_id, "ERROR", f"Scan ruk gaya: {msg}")
        return

    tf = settings["timeframe"]
    cooldown = timedelta(minutes=TIMEFRAME_MINUTES[tf])
    with _Cursor() as cur:
        open_now = _open_positions(user_id, cur)
        cur.execute(
            "SELECT symbol, MAX(closed_at) AS last_closed FROM autotrade_positions WHERE user_id=%s AND status='CLOSED' GROUP BY symbol",
            (user_id,),
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
            notes.append(f"{short}: position khuli")
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
            _throttled_log(user_id, "ERROR", f"{symbol} signal nahi mila: {str(e)[:120]}")
            continue
        verdict, conf = sig.get("verdict"), sig.get("confidence") or 0.0
        if verdict not in ("LONG", "SHORT"):
            notes.append(f"{short}: WAIT {conf:.0f}%")
            continue
        if conf < settings["min_confidence"]:
            notes.append(f"{short}: {verdict} {conf:.0f}% (kam confidence)")
            continue
        if settings["market_type"] == "spot" and verdict == "SHORT":
            notes.append(f"{short}: SHORT {conf:.0f}% (spot mein short nahi)")
            continue
        try:
            open_position(user_id, account, settings, symbol, verdict, signal=sig, source="BOT")
            open_count += 1
            open_symbols.add(symbol)
            notes.append(f"{short}: {verdict} {conf:.0f}% -> TRADE")
        except TradeError as e:
            notes.append(f"{short}: {verdict} {conf:.0f}% (skip)")
            _throttled_log(user_id, "WARN", f"{symbol} trade nahi li: {e}", every_sec=1800)
    log_event(user_id, "SCAN", f"Scan {tf}: " + " | ".join(notes))


def _due_users():
    with _Cursor() as cur:
        cur.execute(
            """SELECT s.user_id, s.timeframe, s.last_scan_at FROM autotrade_settings s
               JOIN autotrade_accounts a ON a.user_id = s.user_id WHERE s.enabled = 1"""
        )
        rows = cur.fetchall() or []
    now = _utcnow()
    due = []
    for r in rows:
        last = _to_dt(r.get("last_scan_at"))
        interval = TIMEFRAME_SCAN_SEC.get(r.get("timeframe"), 300)
        if last is None or (now - last).total_seconds() >= interval:
            due.append(r["user_id"])
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
        lock_conn.ping(reconnect=False)  # lock wala connection toot jaye -> exception -> lock dobara lo
        maybe_monitor()
        now = time.time()
        if now - state["last_scan_check"] >= 30:
            state["last_scan_check"] = now
            for uid in _safe(_due_users) or []:
                _safe(scan_user, uid, between_coins=maybe_monitor)
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
                time.sleep(30)  # koi aur worker/instance engine chala raha hai
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
                   available_coins=None, start=True):
    """main.py se call hota hai. Signal functions inject karta hai, tables
    banata hai aur background engine start karta hai."""
    if get_candles and not _hooks["get_candles"]:
        _hooks["get_candles"] = get_candles
    if generate_signal and not _hooks["generate_signal"]:
        _hooks["generate_signal"] = generate_signal
    if data_exchange is not None and _hooks["data_exchange"] is None:
        _hooks["data_exchange"] = data_exchange
    if available_coins and not _hooks["available_coins"]:
        _hooks["available_coins"] = list(available_coins)
    try:
        init_tables()
    except Exception as e:
        print(f"[autotrade] WARNING: tables nahi ban sakin: {e}")
    if not encryption_ready():
        print("[autotrade] WARNING: AUTOTRADE_ENCRYPTION_KEY set nahi hai - Binance connect disabled rahega.")
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
    }


def _settings_json(s):
    return {k: s[k] for k in DEFAULT_SETTINGS}


# ===========================================================================
# Routes
# ===========================================================================
def _require_json_post():
    if not request.is_json:
        return _err("JSON request zaroori hai.", 415)
    return None


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
        return _err("Login zaroori hai.", 401)
    with _Cursor() as cur:
        _ensure_settings_row(uid, cur)
        settings = load_settings(uid, cur)
        account = load_account(uid, cur)
        cur.execute("SELECT * FROM autotrade_positions WHERE user_id=%s AND status IN ('OPEN','CLOSING') ORDER BY opened_at DESC", (uid,))
        open_rows = cur.fetchall() or []
        cur.execute("SELECT * FROM autotrade_positions WHERE user_id=%s AND status='CLOSED' ORDER BY closed_at DESC LIMIT 25", (uid,))
        history = cur.fetchall() or []
        cur.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END),0) AS wins,
               COALESCE(SUM(pnl_usdt),0) AS total FROM autotrade_positions WHERE user_id=%s AND status='CLOSED'""",
            (uid,),
        )
        agg = cur.fetchone() or {}
        realized_today = _realized_today(uid, cur)
        cur.execute("SELECT id, level, message, created_at FROM autotrade_logs WHERE user_id=%s ORDER BY id DESC LIMIT 40", (uid,))
        logs = cur.fetchall() or []
        cur.execute("SELECT heartbeat_at FROM autotrade_engine WHERE id=1")
        hb = (cur.fetchone() or {}).get("heartbeat_at")

    # Balance purana ho (90s+) to Binance se taaza le lo. Fail hone par 60s
    # tak dobara try nahi karte (har 5s poll par Binance ko spam na karein).
    if account and encryption_ready():
        bal_at = _to_dt(account.get("balance_at"))
        stale = bal_at is None or (_utcnow() - bal_at).total_seconds() > 90 or \
            account.get("balance_market") != settings["market_type"]
        recent_fail = _balance_failures.get(uid)
        if stale and recent_fail and time.time() - recent_fail[0] < 60:
            account["balance_error"] = recent_fail[1]
        elif stale:
            try:
                client = get_client(account, settings["market_type"])
                free, total = client.balance_usdt()
                with _Cursor() as cur:
                    _store_balance(uid, free, total, settings["market_type"], cur)
                account.update({"balance_usdt": free, "balance_total_usdt": total, "balance_at": _utcnow(),
                                "balance_market": settings["market_type"]})
                _balance_failures.pop(uid, None)
            except Exception as e:
                account["balance_error"] = _friendly_exchange_error(e) if not isinstance(e, RuntimeError) else str(e)
                _balance_failures[uid] = (time.time(), account["balance_error"])

    hb_dt = _to_dt(hb)
    n = int(_f(agg.get("n"), 0))
    wins = int(_f(agg.get("wins"), 0))
    paused_until = _to_dt(settings.get("paused_until"))
    return jsonify({
        "ok": True,
        "ready": {"encryption": encryption_ready(), "ccxt": ccxt is not None},
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
            "paused_until": _iso(paused_until) if paused_until and paused_until > _utcnow() else None,
            "pause_reason": settings.get("pause_reason") if paused_until and paused_until > _utcnow() else None,
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
                    "coins": coin_choices(), "max_coins": MAX_COINS},
        "server_time": _iso(_utcnow()),
    })


@autotrade_bp.route("/api/autotrade/connect", methods=["POST"])
def connect():
    uid = _user_id()
    if not uid:
        return _err("Login zaroori hai.", 401)
    bad = _require_json_post()
    if bad:
        return bad
    if not encryption_ready():
        return _err("Server par AUTOTRADE_ENCRYPTION_KEY set nahi hai, is liye keys save nahi ho saktin. "
                    "Admin isay Railway Variables mein add kare.", 503)
    data = request.get_json(silent=True) or {}
    api_key = str(data.get("api_key") or "").strip()
    secret = str(data.get("api_secret") or "").strip()
    mode = str(data.get("mode") or "demo").lower()
    if mode not in ("demo", "live"):
        return _err("Mode 'demo' ya 'live' hona chahiye.")
    if len(api_key) < 16 or len(secret) < 16:
        return _err("API key aur secret dono poore paste karein.")

    with _Cursor() as cur:
        _ensure_settings_row(uid, cur)
        settings = load_settings(uid, cur)
        if _open_positions(uid, cur):
            return _err("Khuli positions ke dauran account change nahi ho sakta. Pehle positions band karein.", 409)

    try:
        client = _client_factory(api_key, secret, mode, settings["market_type"])
        free, total = client.balance_usdt()
    except Exception as e:
        msg = str(e) if isinstance(e, RuntimeError) else _friendly_exchange_error(e)
        return _err(f"Verify fail: {msg}")

    note = None
    if mode == "live":
        restrictions = client.api_restrictions()
        if restrictions and restrictions.get("enableWithdrawals"):
            return _err("Is API key par WITHDRAWAL permission ON hai. Security ke liye Binance mein ye permission "
                        "band karein (sirf Reading + Spot/Futures Trading rakhein), phir dobara connect karein.")
        if restrictions and not restrictions.get("ipRestrict"):
            note = "Tip: Binance API key par apne server ka IP whitelist kar dein (zyada mehfooz)."

    now = _utcnow()
    with _Cursor() as cur:
        cur.execute("DELETE FROM autotrade_accounts WHERE user_id=%s", (uid,))
        cur.execute(
            """INSERT INTO autotrade_accounts (user_id, exchange, mode, api_key_enc, api_secret_enc, key_hint,
               balance_usdt, balance_total_usdt, balance_market, balance_at, connected_at)
               VALUES (%s,'binance',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uid, mode, _encrypt(api_key), _encrypt(secret), "..." + api_key[-4:], free, total,
             settings["market_type"], now, now),
        )
        cur.execute("UPDATE autotrade_settings SET enabled=0 WHERE user_id=%s", (uid,))
        log_event(uid, "INFO", f"Binance {mode.upper()} connect hua. {settings['market_type']} balance: {free:.2f} USDT", cur)
    _drop_clients(uid)
    return jsonify({"ok": True, "mode": mode, "balance_usdt": free, "balance_total_usdt": total, "note": note})


@autotrade_bp.route("/api/autotrade/disconnect", methods=["POST"])
def disconnect():
    uid = _user_id()
    if not uid:
        return _err("Login zaroori hai.", 401)
    with _Cursor() as cur:
        if _open_positions(uid, cur):
            return _err("Pehle saari khuli positions band karein, phir disconnect karein.", 409)
        cur.execute("DELETE FROM autotrade_accounts WHERE user_id=%s", (uid,))
        cur.execute("UPDATE autotrade_settings SET enabled=0 WHERE user_id=%s", (uid,))
        log_event(uid, "INFO", "Binance disconnect hua, API keys delete kar di gayin.", cur)
    _drop_clients(uid)
    return jsonify({"ok": True})


@autotrade_bp.route("/api/autotrade/settings", methods=["POST"])
def update_settings():
    uid = _user_id()
    if not uid:
        return _err("Login zaroori hai.", 401)
    bad = _require_json_post()
    if bad:
        return bad
    with _Cursor() as cur:
        _ensure_settings_row(uid, cur)
        current = load_settings(uid, cur)
        clean, err = validate_settings(request.get_json(silent=True) or {}, current)
        if err:
            return _err(err)
        save_settings(uid, clean, cur)
        log_event(uid, "INFO",
                  f"Settings saved: {clean['market_type']} {clean['leverage']}x, risk {clean['risk_pct']}%, "
                  f"min conf {clean['min_confidence']}%, {clean['timeframe']}, coins {len(clean['coins'])}", cur)
    return jsonify({"ok": True, "settings": clean})


@autotrade_bp.route("/api/autotrade/bot", methods=["POST"])
def toggle_bot():
    uid = _user_id()
    if not uid:
        return _err("Login zaroori hai.", 401)
    bad = _require_json_post()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    enable = bool(data.get("enabled"))
    with _Cursor() as cur:
        _ensure_settings_row(uid, cur)
        account = load_account(uid, cur)
        if enable:
            if not account:
                return _err("Pehle Binance connect karein.")
            if account["mode"] == "live" and not data.get("ack_live"):
                return _err("Live mode mein bot chalane ke liye risk warning confirm karna zaroori hai.",
                            428, need_ack=True)
        cur.execute("UPDATE autotrade_settings SET enabled=%s, last_scan_at=NULL, paused_until=NULL, pause_reason=NULL WHERE user_id=%s"
                    if enable else "UPDATE autotrade_settings SET enabled=%s WHERE user_id=%s",
                    (1 if enable else 0, uid))
        log_event(uid, "INFO", "Auto-Trade bot ON kiya gaya." if enable else "Auto-Trade bot OFF kiya gaya. "
                  "Khuli positions SL/TP par monitor hoti rahengi.", cur)
    return jsonify({"ok": True, "enabled": enable})


@autotrade_bp.route("/api/autotrade/positions/<int:position_id>/close", methods=["POST"])
def close_one(position_id):
    uid = _user_id()
    if not uid:
        return _err("Login zaroori hai.", 401)
    with _Cursor() as cur:
        cur.execute("SELECT * FROM autotrade_positions WHERE id=%s AND user_id=%s", (position_id, uid))
        pos = cur.fetchone()
        account = load_account(uid, cur)
    if not pos or pos["status"] != "OPEN":
        return _err("Position nahi mili ya pehle se band ho rahi hai.", 404)
    if not account:
        return _err("Binance connected nahi hai.")
    try:
        pnl = close_position(pos, account, "MANUAL")
    except TradeError as e:
        return _err(str(e))
    return jsonify({"ok": True, "pnl_usdt": pnl})


@autotrade_bp.route("/api/autotrade/panic", methods=["POST"])
def panic():
    """Emergency: bot band + saari positions market par close."""
    uid = _user_id()
    if not uid:
        return _err("Login zaroori hai.", 401)
    with _Cursor() as cur:
        cur.execute("UPDATE autotrade_settings SET enabled=0 WHERE user_id=%s", (uid,))
        account = load_account(uid, cur)
        cur.execute("SELECT * FROM autotrade_positions WHERE user_id=%s AND status='OPEN'", (uid,))
        positions = cur.fetchall() or []
        log_event(uid, "WARN", f"STOP ALL dabaya gaya: bot OFF, {len(positions)} position(s) band ki ja rahi hain.", cur)
    closed, failed = 0, []
    for pos in positions:
        try:
            close_position(pos, account, "PANIC")
            closed += 1
        except TradeError as e:
            failed.append({"symbol": pos["symbol"], "error": str(e)})
    return jsonify({"ok": not failed, "closed": closed, "failed": failed})


@autotrade_bp.route("/api/autotrade/manual", methods=["POST"])
def manual_trade():
    """Manual trade. confirm=false -> sirf preview (size/SL/TP), confirm=true -> order."""
    uid = _user_id()
    if not uid:
        return _err("Login zaroori hai.", 401)
    bad = _require_json_post()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol") or "").upper()
    side = str(data.get("side") or "").upper()
    if symbol not in coin_choices():
        return _err("Ye coin supported nahi hai.")
    with _Cursor() as cur:
        _ensure_settings_row(uid, cur)
        settings = load_settings(uid, cur)
        account = load_account(uid, cur)
    if not account:
        return _err("Pehle Binance connect karein.")
    if account["mode"] == "live" and data.get("confirm") and not data.get("ack_live"):
        return _err("Live trade ke liye confirmation zaroori hai.", 428, need_ack=True)
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
        return _err("Login zaroori hai.", 401)
    symbol = str(request.args.get("symbol") or "BTC/USDT").upper()
    timeframe = request.args.get("timeframe") or "1h"
    if symbol not in coin_choices() or timeframe not in TIMEFRAME_SCAN_SEC:
        return _err("Coin ya timeframe ghalat hai.")
    try:
        return jsonify({"ok": True, **get_signal(symbol, timeframe, max_age_sec=300)})
    except Exception as e:
        return _err(f"Signal nahi mila: {str(e)[:150]}")


_price_cache = {"ts": 0.0, "key": None, "data": None}


@autotrade_bp.route("/api/autotrade/prices", methods=["GET"])
def prices():
    """Coin list ke liye live price + 24h change (public market data)."""
    if not _user_id():
        return _err("Login zaroori hai.", 401)
    allowed = set(coin_choices())
    symbols = [s for s in (request.args.get("symbols") or "").upper().split(",") if s in allowed][:MAX_COINS + 4]
    if not symbols:
        return jsonify({"ok": True, "prices": {}})
    key = ",".join(sorted(symbols))
    if _price_cache["key"] == key and time.time() - _price_cache["ts"] < 10:
        return jsonify({"ok": True, "prices": _price_cache["data"]})
    ex = _hooks.get("data_exchange")
    if ex is None:
        return _err("Market data available nahi.")
    try:
        tickers = ex.fetch_tickers(symbols)
        out = {s: {"last": _f(tickers[s].get("last")), "change_pct": _f(tickers[s].get("percentage"))}
               for s in symbols if s in tickers}
    except Exception as e:
        return _err(f"Prices nahi mile: {str(e)[:120]}")
    _price_cache.update({"ts": time.time(), "key": key, "data": out})
    return jsonify({"ok": True, "prices": out})
