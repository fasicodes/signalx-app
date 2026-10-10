"""
Demo Trading (paper trading) - Signals FM
=========================================

A server-side practice exchange. Every user gets a virtual USDT account and
trades real, live market prices without risking money. It behaves like a
real exchange so new traders learn the actual mechanics:

  * Spot (buy / sell coins, no leverage) and USDT-M Futures (long / short,
    1x-125x leverage, cross or isolated margin, one-way position mode).
  * Order types: Market, Limit (GTC / IOC / FOK, post-only), Stop-Market,
    Stop-Limit and Trailing Stop. Reduce-only orders.
  * Take-profit / stop-loss attached at entry or edited on the position.
  * Realistic costs: maker / taker fees, bid/ask spread + size-based price
    impact on market orders, funding every 8 hours on futures.
  * Margin math: initial / maintenance margin with leverage brackets,
    liquidation price, cross-margin account liquidation, isolated margin
    add / remove.
  * Pre-trade preview + warnings (no stop-loss, risk % of equity, R:R,
    stop beyond liquidation, high leverage...).
  * Journal (notes + setup tag), trade history, closed-trade analytics
    (win rate, profit factor, drawdown, MFE/MAE, stop-loss discipline).

Limit / stop / trailing orders, TP/SL, liquidation and funding are handled
by a background engine, so they execute even when the page is closed.

Prices come from the same exchange the app already uses for charts (OKX
public market data). Only one engine runs across workers/instances (MySQL
GET_LOCK). Per-user changes are serialized with a per-user MySQL lock.

ENVIRONMENT
  PAPER_ENGINE           "off" disables the background engine.
  PAPER_ENGINE_INTERVAL  seconds between engine ticks (default 2).
"""

import json
import math
import os
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, session

from db import get_db_connection

try:
    import ccxt
except ImportError:  # pragma: no cover
    ccxt = None


paper_bp = Blueprint("paper", __name__)

# ---------------------------------------------------------------------------
# Market rules
# ---------------------------------------------------------------------------
MARKETS = ("spot", "futures")
FEES = {
    "spot": {"maker": 0.001, "taker": 0.001},        # 0.10% / 0.10%
    "futures": {"maker": 0.0002, "taker": 0.0005},   # 0.02% / 0.05%
}
FUNDING_RATE = 0.0001                 # 0.01% every 8h (exchange default rate)
FUNDING_INTERVAL_SEC = 8 * 3600
MIN_NOTIONAL = 5.0                    # USDT
START_BALANCES = (1000, 5000, 10000, 25000, 50000, 100000)
DEFAULT_START = 10000
DEFAULT_LEVERAGE = 5
DEFAULT_MARGIN_MODE = "isolated"
MARGIN_MODES = ("isolated", "cross")
ORDER_TYPES = ("MARKET", "LIMIT", "STOP_MARKET", "STOP_LIMIT", "TRAILING_STOP")
TIFS = ("GTC", "IOC", "FOK")
CALLBACK_MIN, CALLBACK_MAX = 0.1, 10.0
MAX_OPEN_ORDERS = 50
NOTES_MAX = 1000
TAG_MAX = 40
PRICE_STALE_SEC = 30
SPREAD_FALLBACK = 0.0002              # used when the feed has no bid/ask
IMPACT_PER_MILLION = 0.0005           # +0.05% price impact per $1M of size
IMPACT_CAP = 0.01
EPS = 1e-12

# Simplified, Binance-style leverage brackets:
# (max position notional USDT, max leverage, maintenance margin rate)
BRACKETS = (
    (50_000, 125, 0.004),
    (250_000, 50, 0.005),
    (1_000_000, 20, 0.01),
    (5_000_000, 10, 0.025),
    (20_000_000, 5, 0.05),
)
SYMBOL_MAX_LEVERAGE = {
    "BTC/USDT": 125, "ETH/USDT": 125,
    "SOL/USDT": 75, "XRP/USDT": 75, "BNB/USDT": 75, "DOGE/USDT": 75,
}
DEFAULT_MAX_LEVERAGE = 50

FALLBACK_COINS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT", "DOGE/USDT",
    "ADA/USDT", "LINK/USDT", "AVAX/USDT", "DOT/USDT", "LTC/USDT", "TRX/USDT",
]

ENGINE_LOCK_NAME = "signalx_paper_engine"
ENGINE_INTERVAL_SEC = max(1.0, float(os.environ.get("PAPER_ENGINE_INTERVAL", "2") or 2))

_hooks = {"available_coins": None, "exchange": None}


# ===========================================================================
# Small helpers
# ===========================================================================
def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def _to_dt(value):
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        return None


def _iso(value):
    dt = _to_dt(value)
    return dt.isoformat() + "Z" if dt else None


def _f(value, default=None):
    try:
        if value is None or value == "":
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except (TypeError, ValueError):
        return default


def _r(x, nd=8):
    return None if x is None else round(float(x), nd)


def _user_id():
    return session.get("user_id")


def _err(message, status=400, **extra):
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return jsonify(payload), status


def _base(symbol):
    return symbol.split("/")[0]


def _fmt(x):
    """Human number for messages: 61,234.5 / 0.0001234."""
    x = float(x)
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:.6g}"


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


class Busy(Exception):
    pass


class _UserLock:
    """Serializes every balance-changing operation of one user across
    threads, workers and instances (MySQL named lock)."""

    def __init__(self, uid, timeout=8):
        self.name = f"signalx_paper_u{int(uid)}"
        self.timeout = timeout

    def __enter__(self):
        self.conn = get_db_connection()
        self.cur = self.conn.cursor()
        self.cur.execute("SELECT GET_LOCK(%s, %s) AS got", (self.name, self.timeout))
        got = (self.cur.fetchone() or {}).get("got")
        if got != 1:
            self.cur.close()
            self.conn.close()
            raise Busy("Your account is busy processing another order. Please try again.")
        return self.cur

    def __exit__(self, exc_type, exc, tb):
        try:
            self.cur.execute("SELECT RELEASE_LOCK(%s) AS rel", (self.name,))
            self.cur.fetchall()
        except Exception:
            pass
        try:
            self.cur.close()
        finally:
            self.conn.close()
        return False


# ===========================================================================
# Leverage brackets / margin math (pure functions)
# ===========================================================================
def symbol_max_leverage(symbol):
    return SYMBOL_MAX_LEVERAGE.get(symbol, DEFAULT_MAX_LEVERAGE)


def mmr_for(notional):
    for cap, _lev, rate in BRACKETS:
        if notional <= cap:
            return rate
    return BRACKETS[-1][2]


def max_notional_for(leverage, symbol):
    """Largest position (USDT) allowed at this leverage."""
    sym_max = symbol_max_leverage(symbol)
    cap_ok = 0
    for cap, lev, _rate in BRACKETS:
        if min(lev, sym_max) >= leverage:
            cap_ok = cap
        else:
            break
    return cap_ok


def bracket_table(symbol):
    sym_max = symbol_max_leverage(symbol)
    rows, lo = [], 0
    for cap, lev, rate in BRACKETS:
        rows.append({"from": lo, "to": cap, "max_leverage": min(lev, sym_max), "mmr_pct": rate * 100})
        lo = cap
    return rows


def upnl(side, entry, price, qty):
    return (price - entry) * qty if side == "LONG" else (entry - price) * qty


def liq_price_isolated(side, entry, qty, margin, mmr):
    if qty <= 0:
        return None
    if side == "LONG":
        p = (entry * qty - margin) / (qty * (1 - mmr))
    else:
        p = (entry * qty + margin) / (qty * (1 + mmr))
    return p if p > 0 else None


def liq_price_cross(side, entry, qty, extra, mmr):
    """extra = collateral this position can draw on (cross wallet + other
    positions' PnL - their maintenance margin)."""
    if qty <= 0:
        return None
    if side == "LONG":
        p = (entry * qty - extra) / (qty * (1 - mmr))
    else:
        p = (entry * qty + extra) / (qty * (1 + mmr))
    return p if p > 0 else None


def market_fill_price(side, ticker, qty):
    """Market orders fill at the ask (buy) / bid (sell) plus a small,
    size-based price impact - never exactly at the last price."""
    last = float(ticker["last"])
    ask = _f(ticker.get("ask")) or last * (1 + SPREAD_FALLBACK)
    bid = _f(ticker.get("bid")) or last * (1 - SPREAD_FALLBACK)
    base = ask if side == "BUY" else bid
    impact = min(IMPACT_CAP, qty * base / 1_000_000 * IMPACT_PER_MILLION)
    return base * (1 + impact) if side == "BUY" else base * (1 - impact)


def _floor_step(x, step):
    if not step or step <= 0:
        return math.floor(x * 1e8 + 1e-6) / 1e8
    return round(math.floor(x / step + 1e-9) * step, 12)


def _round_tick(p, tick):
    if p is None:
        return None
    if not tick or tick <= 0:
        return float(f"{p:.8g}")
    return round(round(p / tick) * tick, 12)


# ===========================================================================
# Live prices
# ===========================================================================
class PriceFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}
        self._fetched_at = 0.0
        self._fail_until = 0.0
        self._ex = None
        self._meta = {}
        self._markets_loaded = False
        self._markets_retry_at = 0.0
        self.last_error = None

    def exchange(self):
        if self._ex is None:
            if _hooks.get("exchange") is not None:
                self._ex = _hooks["exchange"]
            elif ccxt is not None:
                self._ex = ccxt.okx({"enableRateLimit": True, "timeout": 10000})
        return self._ex

    def reset(self):
        with self._lock:
            self._data, self._fetched_at, self._fail_until = {}, 0.0, 0.0
            self._ex, self._meta, self._markets_loaded, self._markets_retry_at = None, {}, False, 0.0

    def _all_coins(self):
        coins = _hooks.get("available_coins") or FALLBACK_COINS
        out = []
        for c in coins:
            if isinstance(c, str) and c.endswith("/USDT") and c not in out:
                out.append(c)
        return out

    def markets(self):
        if self._markets_loaded or time.time() < self._markets_retry_at:
            return self._meta
        ex = self.exchange()
        if ex is None:
            return self._meta
        try:
            ex.load_markets()
            markets = getattr(ex, "markets", {}) or {}
        except Exception as e:
            self.last_error = str(e)[:200]
            self._markets_retry_at = time.time() + 60
            return self._meta
        decimal_mode = getattr(ccxt, "DECIMAL_PLACES", 2) if ccxt else 2
        mode = getattr(ex, "precisionMode", None)

        def step(v):
            v = _f(v)
            if v is None:
                return None
            if mode == decimal_mode:
                return 10 ** -int(v)
            return v

        meta = {}
        for c in self._all_coins():
            m = markets.get(c)
            if not m:
                continue
            prec = m.get("precision") or {}
            limits = (m.get("limits") or {}).get("amount") or {}
            meta[c] = {
                "price_tick": step(prec.get("price")),
                "qty_step": step(prec.get("amount")),
                "min_qty": _f(limits.get("min"), 0.0) or 0.0,
            }
        self._meta = meta
        self._markets_loaded = bool(markets)
        if not self._markets_loaded:
            self._markets_retry_at = time.time() + 60
        return self._meta

    def coins(self):
        meta = self.markets()
        coins = self._all_coins()
        return [c for c in coins if c in meta] if meta else coins

    def meta(self, symbol):
        m = self.markets().get(symbol) or {}
        return {
            "price_tick": m.get("price_tick"),
            "qty_step": m.get("qty_step") or 1e-8,
            "min_qty": m.get("min_qty") or 0.0,
            "max_leverage": symbol_max_leverage(symbol),
        }

    def refresh(self, max_age=2.0):
        now = time.time()
        with self._lock:
            if now - self._fetched_at < max_age or now < self._fail_until:
                return dict(self._data)
            ex = self.exchange()
            if ex is None:
                return dict(self._data)
            wanted = self.coins()
            try:
                raw = ex.fetch_tickers()
            except Exception:
                try:
                    raw = ex.fetch_tickers(wanted)
                except Exception as e:
                    self.last_error = str(e)[:200]
                    self._fail_until = now + 5
                    return dict(self._data)
            for s in wanted:
                t = (raw or {}).get(s)
                if not t or _f(t.get("last")) is None:
                    continue
                self._data[s] = {
                    "symbol": s,
                    "last": _f(t.get("last")),
                    "bid": _f(t.get("bid")),
                    "ask": _f(t.get("ask")),
                    "high": _f(t.get("high")),
                    "low": _f(t.get("low")),
                    "change_pct": _f(t.get("percentage")),
                    "vol_quote": _f(t.get("quoteVolume")),
                    "ts": now,
                }
            self._fetched_at = now
            self.last_error = None
            return dict(self._data)

    def fresh(self, prices, symbol):
        t = prices.get(symbol)
        if t and time.time() - t["ts"] <= PRICE_STALE_SEC:
            return t
        return None


feed = PriceFeed()


# ===========================================================================
# Database
# ===========================================================================
def init_tables():
    with _Cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_accounts (
                user_id INT PRIMARY KEY,
                sess INT NOT NULL DEFAULT 1,
                start_balance DOUBLE NOT NULL,
                wallet DOUBLE NOT NULL,
                created_at DATETIME NOT NULL,
                reset_at DATETIME NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_symbol_settings (
                user_id INT NOT NULL,
                symbol VARCHAR(30) NOT NULL,
                leverage INT NOT NULL DEFAULT 5,
                margin_mode VARCHAR(10) NOT NULL DEFAULT 'isolated',
                PRIMARY KEY (user_id, symbol),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_positions (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                sess INT NOT NULL,
                market VARCHAR(10) NOT NULL,
                symbol VARCHAR(30) NOT NULL,
                side VARCHAR(5) NOT NULL,
                qty DOUBLE NOT NULL,
                entry_price DOUBLE NOT NULL,
                leverage INT NOT NULL DEFAULT 1,
                margin_mode VARCHAR(10) NOT NULL,
                margin DOUBLE NOT NULL DEFAULT 0,
                tp_price DOUBLE NULL,
                sl_price DOUBLE NULL,
                sl_used TINYINT(1) NOT NULL DEFAULT 0,
                realized DOUBLE NOT NULL DEFAULT 0,
                fees DOUBLE NOT NULL DEFAULT 0,
                funding DOUBLE NOT NULL DEFAULT 0,
                max_qty DOUBLE NOT NULL DEFAULT 0,
                closed_qty DOUBLE NOT NULL DEFAULT 0,
                closed_value DOUBLE NOT NULL DEFAULT 0,
                high_price DOUBLE NULL,
                low_price DOUBLE NULL,
                notes TEXT NULL,
                tag VARCHAR(40) NULL,
                opened_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE (user_id, market, symbol),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_orders (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                sess INT NOT NULL,
                market VARCHAR(10) NOT NULL,
                symbol VARCHAR(30) NOT NULL,
                side VARCHAR(4) NOT NULL,
                type VARCHAR(16) NOT NULL,
                qty DOUBLE NOT NULL,
                price DOUBLE NULL,
                trigger_price DOUBLE NULL,
                trigger_dir VARCHAR(4) NULL,
                callback_pct DOUBLE NULL,
                activation_price DOUBLE NULL,
                trail_extreme DOUBLE NULL,
                trail_active TINYINT(1) NOT NULL DEFAULT 0,
                triggered TINYINT(1) NOT NULL DEFAULT 0,
                reduce_only TINYINT(1) NOT NULL DEFAULT 0,
                post_only TINYINT(1) NOT NULL DEFAULT 0,
                tif VARCHAR(4) NOT NULL DEFAULT 'GTC',
                tp_price DOUBLE NULL,
                sl_price DOUBLE NULL,
                leverage INT NOT NULL DEFAULT 1,
                margin_mode VARCHAR(10) NOT NULL,
                reserved DOUBLE NOT NULL DEFAULT 0,
                status VARCHAR(12) NOT NULL,
                avg_price DOUBLE NULL,
                fee DOUBLE NULL,
                realized DOUBLE NULL,
                liquidity VARCHAR(6) NULL,
                source VARCHAR(12) NOT NULL DEFAULT 'USER',
                reason VARCHAR(255) NULL,
                notes TEXT NULL,
                tag VARCHAR(40) NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                filled_at DATETIME NULL,
                INDEX idx_po_status (status),
                INDEX idx_po_user_status (user_id, status),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_fills (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                sess INT NOT NULL,
                order_id INT NOT NULL,
                market VARCHAR(10) NOT NULL,
                symbol VARCHAR(30) NOT NULL,
                side VARCHAR(4) NOT NULL,
                qty DOUBLE NOT NULL,
                price DOUBLE NOT NULL,
                fee DOUBLE NOT NULL,
                liquidity VARCHAR(6) NOT NULL,
                realized DOUBLE NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                INDEX idx_pf_user (user_id, sess),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_closed (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                sess INT NOT NULL,
                market VARCHAR(10) NOT NULL,
                symbol VARCHAR(30) NOT NULL,
                side VARCHAR(5) NOT NULL,
                leverage INT NOT NULL,
                margin_mode VARCHAR(10) NOT NULL,
                qty DOUBLE NOT NULL,
                entry_price DOUBLE NOT NULL,
                exit_price DOUBLE NOT NULL,
                realized DOUBLE NOT NULL,
                fees DOUBLE NOT NULL,
                funding DOUBLE NOT NULL,
                net_pnl DOUBLE NOT NULL,
                roe_pct DOUBLE NULL,
                close_reason VARCHAR(20) NOT NULL,
                sl_used TINYINT(1) NOT NULL DEFAULT 0,
                mfe_pct DOUBLE NULL,
                mae_pct DOUBLE NULL,
                notes TEXT NULL,
                tag VARCHAR(40) NULL,
                opened_at DATETIME NOT NULL,
                closed_at DATETIME NOT NULL,
                INDEX idx_pc_user (user_id, sess),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_ledger (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                sess INT NOT NULL,
                type VARCHAR(20) NOT NULL,
                symbol VARCHAR(30) NULL,
                amount DOUBLE NOT NULL,
                balance_after DOUBLE NOT NULL,
                created_at DATETIME NOT NULL,
                INDEX idx_pl_user (user_id, sess),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_engine (
                id INT PRIMARY KEY,
                heartbeat_at DATETIME NULL,
                host VARCHAR(100) NULL,
                funding_slot BIGINT NULL
            )
            """
        )
    print("[papertrade] tables ready")


def _ledger(cur, uid, sess, kind, symbol, amount, balance_after):
    if abs(amount) < 1e-12 and kind not in ("DEPOSIT", "RESET"):
        return
    cur.execute(
        """INSERT INTO paper_ledger (user_id, sess, type, symbol, amount, balance_after, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (uid, sess, kind, symbol, round(amount, 8), round(balance_after, 8), _utcnow()),
    )


def _ensure_account(cur, uid):
    cur.execute("SELECT * FROM paper_accounts WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    if row:
        return row
    now = _utcnow()
    try:
        cur.execute(
            """INSERT INTO paper_accounts (user_id, sess, start_balance, wallet, created_at, reset_at)
               VALUES (%s,1,%s,%s,%s,%s)""",
            (uid, DEFAULT_START, DEFAULT_START, now, now),
        )
        _ledger(cur, uid, 1, "DEPOSIT", None, DEFAULT_START, DEFAULT_START)
    except Exception:
        pass  # created concurrently
    cur.execute("SELECT * FROM paper_accounts WHERE user_id=%s", (uid,))
    return cur.fetchone()


def _load_user(cur, uid):
    acct = _ensure_account(cur, uid)
    cur.execute("SELECT * FROM paper_positions WHERE user_id=%s ORDER BY opened_at", (uid,))
    positions = cur.fetchall() or []
    cur.execute("SELECT * FROM paper_orders WHERE user_id=%s AND status='NEW' ORDER BY id", (uid,))
    orders = cur.fetchall() or []
    cur.execute("SELECT * FROM paper_symbol_settings WHERE user_id=%s", (uid,))
    settings = {r["symbol"]: r for r in (cur.fetchall() or [])}
    return acct, positions, orders, settings


def _symbol_settings(settings, symbol):
    s = settings.get(symbol) or {}
    lev = int(s.get("leverage") or DEFAULT_LEVERAGE)
    lev = max(1, min(lev, symbol_max_leverage(symbol)))
    mode = s.get("margin_mode") if s.get("margin_mode") in MARGIN_MODES else DEFAULT_MARGIN_MODE
    return lev, mode


# ===========================================================================
# Account snapshot
# ===========================================================================
def snapshot(acct, positions, orders, prices):
    """Everything the account is worth right now, at current prices."""
    wallet = float(acct["wallet"])
    rows = []
    iso_margin = spot_cost = cross_im = cross_upnl = cross_mm = total_upnl = 0.0
    for p in positions:
        qty, entry = float(p["qty"]), float(p["entry_price"])
        t = feed.fresh(prices, p["symbol"])
        mark = t["last"] if t else entry
        u = upnl(p["side"], entry, mark, qty)
        notional = qty * mark
        lev = int(p["leverage"] or 1)
        if p["market"] == "spot":
            im, mm = float(p["margin"]), 0.0
            spot_cost += im
            base_margin = float(p["margin"]) or qty * entry
        elif p["margin_mode"] == "isolated":
            im, mm = float(p["margin"]), notional * mmr_for(notional)
            iso_margin += im
            base_margin = qty * entry / lev
        else:
            im, mm = notional / lev, notional * mmr_for(notional)
            cross_im += im
            cross_upnl += u
            cross_mm += mm
            base_margin = qty * entry / lev
        total_upnl += u
        rows.append({
            "id": p["id"], "market": p["market"], "symbol": p["symbol"], "side": p["side"],
            "qty": qty, "entry_price": entry, "mark": mark, "price_live": bool(t),
            "leverage": lev, "margin_mode": p["margin_mode"], "margin": im, "maint_margin": mm,
            "notional": notional, "upnl": u, "roe_pct": (u / base_margin * 100) if base_margin else None,
            "tp_price": _f(p.get("tp_price")), "sl_price": _f(p.get("sl_price")),
            "realized": float(p["realized"] or 0), "fees": float(p["fees"] or 0),
            "funding": float(p["funding"] or 0), "notes": p.get("notes"), "tag": p.get("tag"),
            "opened_at": _iso(p["opened_at"]), "liq_price": None,
            "_iso_margin": float(p["margin"]) if p["market"] == "futures" and p["margin_mode"] == "isolated" else None,
        })
    collateral = wallet - iso_margin - spot_cost
    for r in rows:
        if r["market"] != "futures":
            continue
        mmr = mmr_for(r["entry_price"] * r["qty"])
        if r["margin_mode"] == "isolated":
            r["liq_price"] = liq_price_isolated(r["side"], r["entry_price"], r["qty"], r["_iso_margin"], mmr)
        else:
            extra = collateral + (cross_upnl - r["upnl"]) - (cross_mm - r["maint_margin"])
            r["liq_price"] = liq_price_cross(r["side"], r["entry_price"], r["qty"], extra, mmr)
    for r in rows:
        r.pop("_iso_margin", None)
    reserves = sum(float(o["reserved"] or 0) for o in orders if o["status"] == "NEW")
    cross_balance = collateral + cross_upnl
    has_cross = any(r["market"] == "futures" and r["margin_mode"] == "cross" for r in rows)
    available = wallet - iso_margin - spot_cost - cross_im - reserves + min(0.0, cross_upnl)
    equity = wallet + total_upnl
    margin_ratio = None
    if has_cross:
        margin_ratio = (cross_mm / cross_balance * 100) if cross_balance > 0 else 100.0
    start = float(acct["start_balance"])
    return {
        "wallet": wallet, "equity": equity, "available": available, "upnl": total_upnl,
        "position_margin": iso_margin + cross_im, "spot_holdings_cost": spot_cost,
        "order_margin": reserves, "cross_balance": cross_balance, "cross_mm": cross_mm,
        "cross_upnl": cross_upnl, "has_cross": has_cross, "margin_ratio": margin_ratio,
        "start_balance": start, "return_pct": (equity - start) / start * 100 if start else 0.0,
        "positions": rows,
    }


# ===========================================================================
# Order planning: validation + pre-trade preview
# ===========================================================================
class OrderError(Exception):
    def __init__(self, message, plan=None):
        super().__init__(message)
        self.plan = plan


def parse_order(data):
    """Raw JSON -> normalized order request. Raises ValueError."""
    d = data or {}
    market = str(d.get("market") or "futures").lower()
    if market not in MARKETS:
        raise ValueError("Market must be 'spot' or 'futures'.")
    symbol = str(d.get("symbol") or "").upper()
    if symbol not in feed.coins():
        raise ValueError("This coin is not available for demo trading.")
    side = str(d.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("Side must be BUY or SELL.")
    otype = str(d.get("type") or "MARKET").upper()
    if otype not in ORDER_TYPES:
        raise ValueError("Unknown order type.")
    qty = _f(d.get("qty"))
    if qty is None or qty <= 0:
        raise ValueError("Enter an order size greater than zero.")
    tif = str(d.get("tif") or "GTC").upper()
    if tif not in TIFS:
        raise ValueError("Time in force must be GTC, IOC or FOK.")
    req = {
        "market": market, "symbol": symbol, "side": side, "type": otype, "qty": qty,
        "price": _f(d.get("price")), "trigger_price": _f(d.get("trigger_price")),
        "callback_pct": _f(d.get("callback_pct")), "activation_price": _f(d.get("activation_price")),
        "reduce_only": bool(d.get("reduce_only")) and market == "futures",
        "post_only": bool(d.get("post_only")) and otype in ("LIMIT", "STOP_LIMIT"),
        "tif": tif if otype in ("LIMIT", "STOP_LIMIT") else "GTC",
        "tp_price": _f(d.get("tp_price")), "sl_price": _f(d.get("sl_price")),
        "notes": (str(d.get("notes") or "").strip()[:NOTES_MAX]) or None,
        "tag": (str(d.get("tag") or "").strip()[:TAG_MAX]) or None,
    }
    for k in ("price", "trigger_price", "activation_price", "tp_price", "sl_price"):
        if req[k] is not None and req[k] <= 0:
            raise ValueError("Prices must be greater than zero.")
    return req


def _warn(lst, level, code, text):
    lst.append({"level": level, "code": code, "text": text})


def plan_order(acct, positions, orders, settings, prices, req):
    """Validates an order and computes everything a trader should know
    before sending it. Returns a plan dict with `errors` and `warnings`."""
    errors, warnings = [], []
    market, symbol, side, otype = req["market"], req["symbol"], req["side"], req["type"]
    meta = feed.meta(symbol)
    tick, step, min_qty = meta["price_tick"], meta["qty_step"], meta["min_qty"]
    plan = {"market": market, "symbol": symbol, "side": side, "type": otype,
            "errors": errors, "warnings": warnings}

    t = feed.fresh(prices, symbol)
    if not t:
        errors.append(f"Live price for {symbol} is unavailable right now. Try again in a moment.")
        return plan
    last = t["last"]
    plan["last_price"] = last

    if market == "futures":
        lev, mode = _symbol_settings(settings, symbol)
    else:
        lev, mode = 1, "spot"
    pos = next((p for p in positions if p["market"] == market and p["symbol"] == symbol), None)
    if pos is not None and market == "futures":
        lev, mode = int(pos["leverage"]), pos["margin_mode"]
    plan.update({"leverage": lev, "margin_mode": mode})

    qty = _floor_step(req["qty"], step)
    if qty <= 0 or qty < min_qty - EPS:
        errors.append(f"Order size is below the minimum of {_fmt(max(min_qty, step))} {_base(symbol)}.")
        return plan
    plan["qty"] = qty

    price = _round_tick(req["price"], tick) if req["price"] else None
    trigger = _round_tick(req["trigger_price"], tick) if req["trigger_price"] else None
    activation = _round_tick(req["activation_price"], tick) if req["activation_price"] else None
    tp = _round_tick(req["tp_price"], tick) if req["tp_price"] else None
    sl = _round_tick(req["sl_price"], tick) if req["sl_price"] else None
    plan.update({"price": price, "trigger_price": trigger, "activation_price": activation,
                 "tp_price": tp, "sl_price": sl})

    # --- execution price + liquidity ------------------------------------
    immediate = False
    trigger_dir = None
    liquidity = "TAKER"
    if otype == "MARKET":
        est = market_fill_price(side, t, qty)
        immediate = True
    elif otype == "LIMIT":
        if not price:
            errors.append("Enter a limit price.")
            return plan
        ask = t.get("ask") or last
        bid = t.get("bid") or last
        marketable = price >= ask if side == "BUY" else price <= bid
        if marketable:
            if req["post_only"]:
                errors.append("Post-only order would execute immediately as a taker, so it is rejected. "
                              "Move the price away from the market.")
                return plan
            mkt = market_fill_price(side, t, qty)
            est = min(price, mkt) if side == "BUY" else max(price, mkt)
            immediate = True
            _warn(warnings, "info", "marketable_limit",
                  "This limit price crosses the market, so it fills immediately as a taker.")
        else:
            est = price
            liquidity = "MAKER"
            if req["tif"] in ("IOC", "FOK"):
                _warn(warnings, "warn", "ioc_unfilled",
                      f"{req['tif']} orders must fill immediately. At this price it would be canceled right away.")
        dist = abs(price - last) / last * 100
        if dist > 10:
            _warn(warnings, "info", "far_limit", f"Limit price is {dist:.1f}% away from the last price.")
    elif otype in ("STOP_MARKET", "STOP_LIMIT"):
        if not trigger:
            errors.append("Enter a trigger (stop) price.")
            return plan
        if abs(trigger - last) <= EPS:
            errors.append("Trigger price must be different from the current price.")
            return plan
        trigger_dir = "UP" if trigger > last else "DOWN"
        if otype == "STOP_LIMIT":
            if not price:
                errors.append("Enter a limit price for the stop-limit order.")
                return plan
            est = price
            liquidity = "MAKER"
        else:
            est = trigger
        _warn(warnings, "info", "trigger_dir",
              f"Triggers when the price {'rises to' if trigger_dir == 'UP' else 'falls to'} {_fmt(trigger)}.")
    else:  # TRAILING_STOP
        cb = req["callback_pct"]
        if cb is None or cb < CALLBACK_MIN or cb > CALLBACK_MAX:
            errors.append(f"Callback rate must be between {CALLBACK_MIN}% and {CALLBACK_MAX}%.")
            return plan
        ref = activation or last
        est = ref * (1 - cb / 100) if side == "SELL" else ref * (1 + cb / 100)
        plan["callback_pct"] = cb
        if activation:
            ok = activation > last if side == "SELL" else activation < last
            if not ok:
                _warn(warnings, "info", "activation_now",
                      "Activation price is already reached, so trailing starts immediately.")
    plan.update({"est_price": est, "liquidity": liquidity, "immediate": immediate,
                 "trigger_dir": trigger_dir})
    fee_rate = FEES[market]["maker" if liquidity == "MAKER" else "taker"]

    # --- effect on the position -----------------------------------------
    direction = "LONG" if side == "BUY" else "SHORT"
    pos_qty = float(pos["qty"]) if pos else 0.0
    if market == "spot":
        if side == "SELL":
            pending = sum(float(o["qty"]) for o in orders if o["market"] == "spot"
                          and o["symbol"] == symbol and o["side"] == "SELL")
            free = pos_qty - pending
            if pos is None or free <= EPS:
                errors.append(f"You don't hold any free {_base(symbol)} to sell. Spot trading can't short.")
                return plan
            if qty > free + EPS:
                errors.append(f"You can sell at most {_fmt(free)} {_base(symbol)} "
                              f"({_fmt(pending)} is already in open sell orders)." if pending else
                              f"You can sell at most {_fmt(free)} {_base(symbol)}.")
                return plan
            reduce_qty, open_qty = qty, 0.0
        else:
            reduce_qty, open_qty = 0.0, qty
    else:
        if pos is None or pos["side"] == direction:
            reduce_qty, open_qty = 0.0, qty
        else:
            reduce_qty = min(qty, pos_qty)
            open_qty = qty - reduce_qty
        if req["reduce_only"]:
            if reduce_qty <= EPS:
                errors.append("Reduce-only: there is no opposite position to reduce.")
                return plan
            if open_qty > EPS:
                errors.append(f"Reduce-only size can't be larger than the position ({_fmt(pos_qty)} {_base(symbol)}).")
                return plan
    if pos is None or (market == "futures" and pos["side"] == direction) or (market == "spot" and side == "BUY"):
        effect = "increase" if pos is not None else "open"
    elif open_qty > EPS:
        effect = "flip"
        _warn(warnings, "info", "flip",
              f"This closes your {pos['side']} and opens a {direction} of {_fmt(open_qty)} {_base(symbol)}.")
    elif abs(reduce_qty - pos_qty) <= EPS:
        effect = "close"
    else:
        effect = "reduce"
    plan["effect"] = effect

    notional = qty * est
    fee = notional * fee_rate
    realized = upnl(pos["side"], float(pos["entry_price"]), est, reduce_qty) if reduce_qty > EPS else 0.0
    open_notional = open_qty * est
    open_margin = open_notional / lev if market == "futures" else (open_notional if side == "BUY" else 0.0)
    plan.update({"notional": notional, "fee": fee, "fee_rate": fee_rate, "margin_required": open_margin,
                 "realized_est": realized, "open_qty": open_qty, "reduce_qty": reduce_qty})

    if notional < MIN_NOTIONAL:
        errors.append(f"Order value must be at least {MIN_NOTIONAL:.0f} USDT (this is {notional:.2f}).")

    snap = snapshot(acct, positions, orders, prices)
    plan["available_before"] = snap["available"]
    plan["equity"] = snap["equity"]
    need = open_margin + fee
    if open_qty > EPS and need > snap["available"] + 1e-9:
        errors.append(f"Insufficient available balance: this order needs {need:,.2f} USDT "
                      f"(margin + fee) but only {max(0.0, snap['available']):,.2f} USDT is available.")

    res_qty, res_side, res_entry, res_margin = 0.0, None, None, 0.0
    if market == "futures" or side == "BUY":
        if pos is not None and pos["side"] == direction:
            res_qty = pos_qty + open_qty
            res_entry = (pos_qty * float(pos["entry_price"]) + open_qty * est) / res_qty
            res_margin = float(pos["margin"]) + open_margin
            res_side = direction
        elif open_qty > EPS:
            res_qty, res_entry, res_side, res_margin = open_qty, est, direction, open_margin
        elif pos is not None:
            res_qty = pos_qty - reduce_qty
            res_entry, res_side = float(pos["entry_price"]), pos["side"]
            res_margin = float(pos["margin"]) * (res_qty / pos_qty if pos_qty else 0)
    else:
        res_qty = pos_qty - reduce_qty
        res_entry, res_side = float(pos["entry_price"]), "LONG"
        res_margin = float(pos["margin"]) * (res_qty / pos_qty if pos_qty else 0)

    if market == "futures" and res_qty > EPS and open_qty > EPS:
        cap = max_notional_for(lev, symbol)
        if res_qty * est > cap:
            errors.append(f"At {lev}x the maximum position size is {cap:,.0f} USDT "
                          f"(this would be {res_qty * est:,.0f}). Lower the leverage or the size.")

    # --- resulting position + liquidation estimate ------------------------
    liq = None
    if res_qty > EPS:
        hypo_positions = [p for p in positions if not (p["market"] == market and p["symbol"] == symbol)]
        hypo_positions.append({
            "id": 0, "market": market, "symbol": symbol, "side": res_side, "qty": res_qty,
            "entry_price": res_entry, "leverage": lev, "margin_mode": mode, "margin": res_margin,
            "realized": 0, "fees": 0, "funding": 0, "opened_at": _utcnow(),
        })
        hypo_acct = dict(acct)
        hypo_acct["wallet"] = float(acct["wallet"]) + realized - fee
        hypo_prices = dict(prices)
        hypo_prices[symbol] = dict(t, last=est, ts=time.time())
        snap2 = snapshot(hypo_acct, hypo_positions, orders, hypo_prices)
        liq = next((r["liq_price"] for r in snap2["positions"] if r["id"] == 0), None)
        plan["available_after"] = snap2["available"] if immediate else snap["available"] - need
    else:
        plan["available_after"] = snap["available"] + (realized - fee if immediate else 0)
    plan.update({"result_qty": res_qty, "result_side": res_side, "result_entry": res_entry,
                 "liq_price": liq})
    if liq and res_entry:
        liq_dist = abs(res_entry - liq) / res_entry * 100
        plan["liq_distance_pct"] = liq_dist
        if open_qty > EPS and liq_dist < 2:
            _warn(warnings, "danger", "liq_close",
                  f"Liquidation is only {liq_dist:.2f}% away from entry. A normal price wiggle can wipe this position.")

    # --- TP / SL ----------------------------------------------------------
    opens = open_qty > EPS
    if (tp or sl) and not opens:
        errors.append("Take-profit / stop-loss can only be attached to orders that open or add to a position. "
                      "Set TP/SL on the position instead.")
    plan["tp"] = plan["sl"] = None
    if opens and res_qty > EPS:
        im = res_qty * res_entry / lev if market == "futures" else res_qty * res_entry
        exit_rate = FEES[market]["taker"]

        def outcome(level):
            pnl = upnl(res_side, res_entry, level, res_qty) - level * res_qty * exit_rate
            return {"price": level, "pnl": pnl, "roe_pct": pnl / im * 100 if im else None,
                    "move_pct": (level - res_entry) / res_entry * 100}

        if tp:
            if (res_side == "LONG" and tp <= res_entry) or (res_side == "SHORT" and tp >= res_entry):
                errors.append(f"Take-profit must be {'above' if res_side == 'LONG' else 'below'} the entry price "
                              f"({_fmt(res_entry)}) for a {res_side.lower()}.")
            else:
                plan["tp"] = outcome(tp)
        if sl:
            if (res_side == "LONG" and sl >= res_entry) or (res_side == "SHORT" and sl <= res_entry):
                errors.append(f"Stop-loss must be {'below' if res_side == 'LONG' else 'above'} the entry price "
                              f"({_fmt(res_entry)}) for a {res_side.lower()}.")
            else:
                plan["sl"] = outcome(sl)
                if liq and ((res_side == "LONG" and sl <= liq) or (res_side == "SHORT" and sl >= liq)):
                    _warn(warnings, "danger", "sl_beyond_liq",
                          f"Your stop-loss ({_fmt(sl)}) is beyond the liquidation price ({_fmt(liq)}). "
                          f"You would be liquidated before it triggers.")
        same_pos = pos is not None and pos["side"] == res_side
        if same_pos and (tp or sl) and (_f(pos.get("tp_price")) or _f(pos.get("sl_price"))):
            _warn(warnings, "info", "tpsl_replace",
                  "The TP/SL on this order will replace the TP/SL currently set on your position.")
        existing_sl = same_pos and _f(pos.get("sl_price"))
        if not sl and not existing_sl:
            if market == "futures":
                _warn(warnings, "warn", "no_sl", "No stop-loss. One sharp move against you can take most of this margin.")
            else:
                _warn(warnings, "info", "no_sl", "No stop-loss set. Decide in advance where you would exit if you are wrong.")
        if plan["sl"]:
            loss = -plan["sl"]["pnl"]
            risk_pct = loss / snap["equity"] * 100 if snap["equity"] > 0 else None
            plan["risk_pct_equity"] = risk_pct
            if risk_pct is not None and risk_pct > 5:
                _warn(warnings, "danger", "risk_high",
                      f"If the stop-loss hits you lose {risk_pct:.1f}% of your account. Most pros risk 0.5–2% per trade.")
            elif risk_pct is not None and risk_pct > 2:
                _warn(warnings, "warn", "risk_mid",
                      f"This trade risks {risk_pct:.1f}% of your account. Many traders cap risk at 1–2% per trade.")
        if plan["tp"] and plan["sl"] and plan["sl"]["pnl"] < 0:
            rr = plan["tp"]["pnl"] / -plan["sl"]["pnl"]
            plan["rr"] = rr
            if rr < 1:
                _warn(warnings, "warn", "rr_low",
                      f"Reward:risk is only 1:{rr:.2f}. You'd need a win rate above {100 / (1 + rr):.0f}% to break even.")
    if market == "futures" and opens:
        if lev >= 50:
            _warn(warnings, "danger", "lev_extreme", f"{lev}x leverage: a {100 / lev:.1f}% move against you liquidates the position.")
        elif lev >= 20:
            _warn(warnings, "warn", "lev_high", f"{lev}x is high leverage. Beginners usually stay at 2–10x.")
        if snap["available"] > 0 and open_margin > snap["available"] * 0.5:
            _warn(warnings, "warn", "big_margin", "This order uses more than half of your available balance.")
    plan["reserve"] = 0.0 if immediate else (need if opens else 0.0)
    plan["fee_rate_pct"] = fee_rate * 100
    return plan


# ===========================================================================
# Fills
# ===========================================================================
class _Reject(Exception):
    pass


def _close_reason(order):
    src = order.get("source") or "USER"
    if src in ("TP", "SL", "LIQUIDATION"):
        return src
    return {"MARKET": "MANUAL", "LIMIT": "LIMIT", "STOP_MARKET": "STOP", "STOP_LIMIT": "STOP",
            "TRAILING_STOP": "TRAILING"}.get(order.get("type"), "MANUAL")


def _archive(cur, pos, final, reason):
    """Moves a fully closed position into paper_closed."""
    qty_max = float(final["max_qty"]) or float(final["closed_qty"])
    entry = float(pos["entry_price"])
    exit_price = final["closed_value"] / final["closed_qty"] if final["closed_qty"] else entry
    net = final["realized"] - final["fees"] - final["funding"]
    lev = int(pos["leverage"] or 1)
    base_margin = qty_max * entry / lev if pos["market"] == "futures" else qty_max * entry
    high, low = _f(pos.get("high_price")), _f(pos.get("low_price"))
    mfe = mae = None
    if high and low and entry:
        if pos["side"] == "LONG":
            mfe, mae = (high - entry) / entry * 100, (low - entry) / entry * 100
        else:
            mfe, mae = (entry - low) / entry * 100, (entry - high) / entry * 100
    cur.execute(
        """INSERT INTO paper_closed (user_id, sess, market, symbol, side, leverage, margin_mode, qty, entry_price,
           exit_price, realized, fees, funding, net_pnl, roe_pct, close_reason, sl_used, mfe_pct, mae_pct, notes, tag,
           opened_at, closed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (pos["user_id"], pos["sess"], pos["market"], pos["symbol"], pos["side"], lev, pos["margin_mode"], qty_max,
         entry, exit_price, final["realized"], final["fees"], final["funding"], net,
         net / base_margin * 100 if base_margin else None, reason, int(pos.get("sl_used") or 0), mfe, mae,
         pos.get("notes"), pos.get("tag"), pos["opened_at"], _utcnow()),
    )
    return cur.lastrowid


def _cancel_orphans(cur, uid, market, symbol, reason):
    """When a position is gone, reduce-only (futures) / sell (spot) orders
    on it can no longer do anything."""
    if market == "futures":
        cur.execute("""UPDATE paper_orders SET status='CANCELED', reason=%s, reserved=0, updated_at=%s
                       WHERE user_id=%s AND status='NEW' AND market='futures' AND symbol=%s AND reduce_only=1""",
                    (reason, _utcnow(), uid, symbol))
    else:
        cur.execute("""UPDATE paper_orders SET status='CANCELED', reason=%s, reserved=0, updated_at=%s
                       WHERE user_id=%s AND status='NEW' AND market='spot' AND symbol=%s AND side='SELL'""",
                    (reason, _utcnow(), uid, symbol))


def _apply_fill(cur, uid, order, price, liquidity, prices, liquidation=False):
    """Executes `order` at `price` against the user's account (inside the
    user lock). Returns a result dict; raises _Reject if it can't fill."""
    acct, positions, orders, _settings = _load_user(cur, uid)
    sess = int(acct["sess"])
    market, symbol, side = order["market"], order["symbol"], order["side"]
    direction = "LONG" if side == "BUY" else "SHORT"
    qty = float(order["qty"])
    pos = next((p for p in positions if p["market"] == market and p["symbol"] == symbol), None)
    lev = int(order["leverage"] or 1)
    mode = order["margin_mode"]
    if pos is not None and market == "futures":
        lev, mode = int(pos["leverage"]), pos["margin_mode"]
    pos_qty = float(pos["qty"]) if pos else 0.0

    if market == "spot":
        if side == "SELL":
            if pos is None or qty > pos_qty * (1 + 1e-9) + EPS:
                raise _Reject(f"Not enough {_base(symbol)} to sell (you hold {_fmt(pos_qty)}).")
            reduce_qty, open_qty = min(qty, pos_qty), 0.0
        else:
            reduce_qty, open_qty = 0.0, qty
    else:
        if pos is None or pos["side"] == direction:
            reduce_qty, open_qty = 0.0, qty
        else:
            reduce_qty = min(qty, pos_qty)
            open_qty = qty - reduce_qty
        if order.get("reduce_only"):
            if reduce_qty <= EPS:
                raise _Reject("Reduce-only order would open or increase a position.")
            if open_qty > EPS:  # reduce-only is trimmed to the position size
                qty, open_qty = reduce_qty, 0.0

    fee_rate = FEES[market]["maker" if liquidity == "MAKER" else "taker"]
    fee = qty * price * fee_rate
    if open_qty > EPS:
        open_margin = open_qty * price / lev if market == "futures" else open_qty * price
        snap = snapshot(acct, positions, orders, prices)
        own_reserve = float(order.get("reserved") or 0) if any(o["id"] == order["id"] for o in orders) else 0.0
        if open_margin + fee > snap["available"] + own_reserve + 1e-9:
            raise _Reject(f"Insufficient available balance when the order executed "
                          f"(needed {open_margin + fee:,.2f} USDT).")
        if market == "futures":
            same = pos_qty if (pos is not None and pos["side"] == direction) else 0.0
            cap = max_notional_for(lev, symbol)
            if (same + open_qty) * price > cap:
                raise _Reject(f"Position would exceed the {cap:,.0f} USDT limit for {lev}x leverage.")
    else:
        open_margin = 0.0

    now = _utcnow()
    wallet = float(acct["wallet"])
    realized = 0.0
    extra_fee = 0.0
    closed_id = None
    reason_note = None
    reason = _close_reason(order)

    if reduce_qty > EPS:
        realized = upnl(pos["side"], float(pos["entry_price"]), price, reduce_qty)
        frac = reduce_qty / pos_qty
        fee_part = fee * reduce_qty / qty
        if liquidation and market == "futures" and pos["margin_mode"] == "isolated":
            # Isolated liquidation: the whole position margin is lost. What is
            # left after the loss + fee goes to the insurance fund; if the
            # price gapped past bankruptcy, the fund covers the shortfall.
            extra_fee = float(pos["margin"]) + realized - fee_part
        new_qty = pos_qty - reduce_qty
        final = {
            "realized": float(pos["realized"]) + realized,
            "fees": float(pos["fees"]) + fee_part + extra_fee,
            "funding": float(pos["funding"]),
            "closed_qty": float(pos["closed_qty"]) + reduce_qty,
            "closed_value": float(pos["closed_value"]) + reduce_qty * price,
            "max_qty": float(pos["max_qty"]),
        }
        step = feed.meta(symbol)["qty_step"]
        if new_qty <= max(EPS, step / 2):
            closed_id = _archive(cur, pos, final, reason)
            cur.execute("DELETE FROM paper_positions WHERE id=%s", (pos["id"],))
            _cancel_orphans(cur, uid, market, symbol, "Position closed")
            pos = None
        else:
            cur.execute(
                """UPDATE paper_positions SET qty=%s, margin=%s, realized=%s, fees=%s, closed_qty=%s,
                   closed_value=%s, updated_at=%s WHERE id=%s""",
                (new_qty, float(pos["margin"]) * (1 - frac), final["realized"], final["fees"],
                 final["closed_qty"], final["closed_value"], now, pos["id"]),
            )
            pos = dict(pos, qty=new_qty)

    new_position_id = None
    if open_qty > EPS:
        fee_part = fee * open_qty / qty
        tp, sl = _f(order.get("tp_price")), _f(order.get("sl_price"))
        if pos is not None and pos["side"] == direction:
            total = pos_qty + open_qty
            entry = (pos_qty * float(pos["entry_price"]) + open_qty * price) / total
            cur.execute(
                """UPDATE paper_positions SET qty=%s, entry_price=%s, margin=%s, fees=%s, max_qty=%s,
                   updated_at=%s WHERE id=%s""",
                (total, entry, float(pos["margin"]) + open_margin, float(pos["fees"]) + fee_part,
                 max(float(pos["max_qty"]), total), now, pos["id"]),
            )
            new_position_id = pos["id"]
        else:
            entry = price
            cur.execute(
                """INSERT INTO paper_positions (user_id, sess, market, symbol, side, qty, entry_price, leverage,
                   margin_mode, margin, fees, max_qty, high_price, low_price, notes, tag, opened_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (uid, sess, market, symbol, direction, open_qty, price, lev if market == "futures" else 1,
                 mode if market == "futures" else "spot", open_margin, fee_part, open_qty, price, price,
                 order.get("notes"), order.get("tag"), now, now),
            )
            new_position_id = cur.lastrowid
        # attach TP/SL - drop a level the fill price already moved past
        sets, vals = [], []
        if tp:
            if (direction == "LONG" and tp > entry) or (direction == "SHORT" and tp < entry):
                sets.append("tp_price=%s")
                vals.append(tp)
            else:
                reason_note = "TP not set: the fill price moved past it."
        if sl:
            if (direction == "LONG" and sl < entry) or (direction == "SHORT" and sl > entry):
                sets += ["sl_price=%s", "sl_used=1"]
                vals.append(sl)
            else:
                reason_note = "SL not set: the fill price moved past it."
        if sets:
            cur.execute(f"UPDATE paper_positions SET {', '.join(sets)} WHERE id=%s", tuple(vals) + (new_position_id,))

    wallet += realized - fee - extra_fee
    cur.execute("UPDATE paper_accounts SET wallet=%s WHERE user_id=%s", (wallet, uid))
    running = float(acct["wallet"])
    if realized:
        running += realized
        _ledger(cur, uid, sess, "REALIZED_PNL", symbol, realized, running)
    running -= fee
    _ledger(cur, uid, sess, "FEE", symbol, -fee, running)
    if extra_fee > 0:
        running -= extra_fee
        _ledger(cur, uid, sess, "LIQUIDATION_FEE", symbol, -extra_fee, running)
    elif extra_fee < 0:
        running -= extra_fee
        _ledger(cur, uid, sess, "INSURANCE", symbol, -extra_fee, running)

    cur.execute(
        """INSERT INTO paper_fills (user_id, sess, order_id, market, symbol, side, qty, price, fee, liquidity,
           realized, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (uid, sess, order["id"], market, symbol, side, qty, price, fee, liquidity, realized, now),
    )
    cur.execute(
        """UPDATE paper_orders SET status='FILLED', qty=%s, avg_price=%s, fee=%s, realized=%s, liquidity=%s,
           reserved=0, filled_at=%s, updated_at=%s, reason=%s WHERE id=%s""",
        (qty, price, fee, realized, liquidity, now, now, reason_note, order["id"]),
    )
    return {"order_id": order["id"], "qty": qty, "price": price, "fee": fee, "realized": realized,
            "liquidity": liquidity, "closed_id": closed_id, "position_id": new_position_id}


def _insert_order(cur, uid, sess, req, plan, status="NEW", source="USER"):
    now = _utcnow()
    cur.execute(
        """INSERT INTO paper_orders (user_id, sess, market, symbol, side, type, qty, price, trigger_price,
           trigger_dir, callback_pct, activation_price, reduce_only, post_only, tif, tp_price, sl_price, leverage,
           margin_mode, reserved, status, source, notes, tag, created_at, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (uid, sess, req["market"], req["symbol"], req["side"], req["type"], plan["qty"], plan.get("price"),
         plan.get("trigger_price"), plan.get("trigger_dir"), plan.get("callback_pct"), plan.get("activation_price"),
         int(req.get("reduce_only") or 0), int(req.get("post_only") or 0), req.get("tif") or "GTC",
         plan.get("tp_price"), plan.get("sl_price"), plan.get("leverage") or 1, plan.get("margin_mode") or "spot",
         plan.get("reserve") or 0.0, status, source, req.get("notes"), req.get("tag"), now, now),
    )
    cur.execute("SELECT * FROM paper_orders WHERE id=%s", (cur.lastrowid,))
    return cur.fetchone()


def place_order(uid, req):
    prices = feed.refresh(max_age=1.0)
    with _UserLock(uid) as cur:
        acct, positions, orders, settings = _load_user(cur, uid)
        plan = plan_order(acct, positions, orders, settings, prices, req)
        if plan["errors"]:
            raise OrderError(plan["errors"][0], plan)
        if len(orders) >= MAX_OPEN_ORDERS and not plan["immediate"]:
            raise OrderError(f"You can have at most {MAX_OPEN_ORDERS} open orders.", plan)
        order = _insert_order(cur, uid, int(acct["sess"]), req, plan)
        result = None
        if plan["immediate"]:
            try:
                result = _apply_fill(cur, uid, order, plan["est_price"], "TAKER", prices)
            except _Reject as e:
                cur.execute("UPDATE paper_orders SET status='REJECTED', reason=%s, reserved=0, updated_at=%s WHERE id=%s",
                            (str(e)[:255], _utcnow(), order["id"]))
                raise OrderError(str(e), plan)
        elif req["type"] == "LIMIT" and req["tif"] in ("IOC", "FOK"):
            cur.execute("""UPDATE paper_orders SET status='EXPIRED', reserved=0, updated_at=%s,
                           reason='IOC/FOK order could not fill immediately' WHERE id=%s""", (_utcnow(), order["id"]))
        elif req["type"] == "TRAILING_STOP":
            t = feed.fresh(prices, req["symbol"])
            act = plan.get("activation_price")
            active = act is None or (req["side"] == "SELL" and t["last"] >= act) or \
                (req["side"] == "BUY" and t["last"] <= act)
            if active:
                cur.execute("UPDATE paper_orders SET trail_active=1, trail_extreme=%s WHERE id=%s",
                            (t["last"], order["id"]))
        cur.execute("SELECT * FROM paper_orders WHERE id=%s", (order["id"],))
        order = cur.fetchone()
    return order, plan, result


def cancel_order(uid, order_id, reason="Canceled by you"):
    with _UserLock(uid) as cur:
        cur.execute("""UPDATE paper_orders SET status='CANCELED', reserved=0, reason=%s, updated_at=%s
                       WHERE id=%s AND user_id=%s AND status='NEW'""", (reason, _utcnow(), order_id, uid))
        return cur.rowcount == 1


def _market_close(cur, uid, pos, prices, fraction=1.0, source="USER", liquidation=False):
    """Closes (part of) a position at market inside the user lock."""
    t = feed.fresh(prices, pos["symbol"])
    if not t:
        raise _Reject("Live price unavailable, can't close at market right now.")
    meta = feed.meta(pos["symbol"])
    qty = float(pos["qty"]) if fraction >= 0.999 else _floor_step(float(pos["qty"]) * fraction, meta["qty_step"])
    if qty <= 0:
        raise _Reject("Close size is too small.")
    side = "SELL" if pos["side"] == "LONG" else "BUY"
    acct = _ensure_account(cur, uid)
    req = {"market": pos["market"], "symbol": pos["symbol"], "side": side, "type": "MARKET",
           "reduce_only": pos["market"] == "futures", "post_only": False, "tif": "GTC",
           "notes": None, "tag": None}
    plan = {"qty": qty, "leverage": pos["leverage"], "margin_mode": pos["margin_mode"], "reserve": 0.0}
    order = _insert_order(cur, uid, int(acct["sess"]), req, plan, source=source)
    price = t["last"] if liquidation else market_fill_price(side, t, qty)
    try:
        return _apply_fill(cur, uid, order, price, "TAKER", prices, liquidation=liquidation)
    except _Reject as e:
        cur.execute("UPDATE paper_orders SET status='REJECTED', reason=%s, updated_at=%s WHERE id=%s",
                    (str(e)[:255], _utcnow(), order["id"]))
        raise


# ===========================================================================
# Background engine
# ===========================================================================
def _order_action(o, t):
    """What should happen to a resting order at ticker t?
    Returns (action, price, liquidity) or None. Also returns state updates
    for trailing orders as ('trail', extreme, active)."""
    last = t["last"]
    side = o["side"]
    ask = t.get("ask") or last
    bid = t.get("bid") or last
    otype = o["type"]
    is_limit = otype == "LIMIT" or (otype == "STOP_LIMIT" and o["triggered"])
    if is_limit:
        lim = float(o["price"])
        if side == "BUY" and (ask <= lim or last <= lim):
            return ("fill", lim, "MAKER")
        if side == "SELL" and (bid >= lim or last >= lim):
            return ("fill", lim, "MAKER")
        return None
    if otype in ("STOP_MARKET", "STOP_LIMIT"):
        trig = float(o["trigger_price"])
        hit = last >= trig if o["trigger_dir"] == "UP" else last <= trig
        if not hit:
            return None
        if otype == "STOP_MARKET":
            return ("fill_market", None, "TAKER")
        lim = float(o["price"])
        marketable = (side == "BUY" and ask <= lim) or (side == "SELL" and bid >= lim)
        return ("fill_market_capped", lim, "TAKER") if marketable else ("trigger", None, None)
    if otype == "TRAILING_STOP":
        cb = float(o["callback_pct"]) / 100
        active = bool(o["trail_active"])
        extreme = _f(o["trail_extreme"])
        if not active:
            act = _f(o["activation_price"])
            if act is None or (side == "SELL" and last >= act) or (side == "BUY" and last <= act):
                return ("trail", last, 1)
            return None
        if side == "SELL":
            if extreme is None or last > extreme:
                return ("trail", last, 1)
            if last <= extreme * (1 - cb):
                return ("fill_market", None, "TAKER")
        else:
            if extreme is None or last < extreme:
                return ("trail", last, 1)
            if last >= extreme * (1 + cb):
                return ("fill_market", None, "TAKER")
    return None


def _process_order(uid, order_id, prices):
    with _UserLock(uid) as cur:
        cur.execute("SELECT * FROM paper_orders WHERE id=%s AND status='NEW'", (order_id,))
        o = cur.fetchone()
        if not o:
            return
        t = feed.fresh(prices, o["symbol"])
        if not t:
            return
        act = _order_action(o, t)
        if not act:
            return
        kind, px, liq = act
        if kind == "trail":
            cur.execute("UPDATE paper_orders SET trail_extreme=%s, trail_active=%s, updated_at=%s WHERE id=%s",
                        (px, liq, _utcnow(), o["id"]))
            return
        if kind == "trigger":
            cur.execute("UPDATE paper_orders SET triggered=1, updated_at=%s WHERE id=%s", (_utcnow(), o["id"]))
            return
        qty = float(o["qty"])
        if kind == "fill_market":
            px = market_fill_price(o["side"], t, qty)
        elif kind == "fill_market_capped":
            mkt = market_fill_price(o["side"], t, qty)
            px = min(px, mkt) if o["side"] == "BUY" else max(px, mkt)
        try:
            _apply_fill(cur, uid, o, px, liq, prices)
        except _Reject as e:
            cur.execute("UPDATE paper_orders SET status='REJECTED', reason=%s, reserved=0, updated_at=%s WHERE id=%s",
                        (str(e)[:255], _utcnow(), o["id"]))


def _position_trigger(uid, position_id, kind, prices):
    """TP / SL hit on a position -> market close."""
    with _UserLock(uid) as cur:
        cur.execute("SELECT * FROM paper_positions WHERE id=%s", (position_id,))
        pos = cur.fetchone()
        if not pos:
            return
        t = feed.fresh(prices, pos["symbol"])
        if not t:
            return
        level = _f(pos["tp_price"] if kind == "TP" else pos["sl_price"])
        if level is None:
            return
        last = t["last"]
        long_ = pos["side"] == "LONG"
        hit = (last >= level if long_ else last <= level) if kind == "TP" else (last <= level if long_ else last >= level)
        if not hit:
            return
        liquidation = False
        if kind == "SL" and pos["market"] == "futures":
            # If the price already gapped past the liquidation price, the
            # position is liquidated rather than stopped out (an isolated
            # position can never lose more than its margin).
            acct, positions, orders, _s = _load_user(cur, uid)
            row = next((r for r in snapshot(acct, positions, orders, prices)["positions"] if r["id"] == pos["id"]), None)
            liq = row and row["liq_price"]
            if liq and ((long_ and last <= liq) or (not long_ and last >= liq)):
                if pos["margin_mode"] != "isolated":
                    return  # the cross-margin liquidation check handles it
                liquidation = True
        try:
            _market_close(cur, uid, pos, prices, 1.0, source="LIQUIDATION" if liquidation else kind,
                          liquidation=liquidation)
        except _Reject:
            pass


def _liquidate(uid, prices):
    """Checks one user's futures positions and liquidates what must go."""
    with _UserLock(uid) as cur:
        acct, positions, orders, _s = _load_user(cur, uid)
        snap = snapshot(acct, positions, orders, prices)
        by_id = {p["id"]: p for p in positions}
        # isolated
        for r in snap["positions"]:
            if r["market"] != "futures" or r["margin_mode"] != "isolated" or not r["liq_price"] or not r["price_live"]:
                continue
            if (r["side"] == "LONG" and r["mark"] <= r["liq_price"]) or (r["side"] == "SHORT" and r["mark"] >= r["liq_price"]):
                try:
                    _market_close(cur, uid, by_id[r["id"]], prices, 1.0, source="LIQUIDATION", liquidation=True)
                except _Reject:
                    pass
        # cross: whole cross account
        acct, positions, orders, _s = _load_user(cur, uid)
        snap = snapshot(acct, positions, orders, prices)
        if not snap["has_cross"] or snap["cross_balance"] > snap["cross_mm"]:
            return
        if not all(r["price_live"] for r in snap["positions"] if r["market"] == "futures" and r["margin_mode"] == "cross"):
            return
        mm_total = snap["cross_mm"]
        closed_ids = []
        for p in positions:
            if p["market"] == "futures" and p["margin_mode"] == "cross":
                try:
                    res = _market_close(cur, uid, p, prices, 1.0, source="LIQUIDATION", liquidation=True)
                    if res.get("closed_id"):
                        closed_ids.append(res["closed_id"])
                except _Reject:
                    pass
        acct, positions, orders, _s = _load_user(cur, uid)
        snap = snapshot(acct, positions, orders, prices)
        collateral = snap["cross_balance"]
        wallet = float(acct["wallet"])
        sess = int(acct["sess"])
        adjust = -min(mm_total, max(0.0, collateral)) if collateral > 0 else -collateral
        if abs(adjust) > EPS:
            wallet += adjust
            cur.execute("UPDATE paper_accounts SET wallet=%s WHERE user_id=%s", (wallet, uid))
            _ledger(cur, uid, sess, "LIQUIDATION_FEE" if adjust < 0 else "INSURANCE", None, adjust, wallet)
            if closed_ids:
                share = adjust / len(closed_ids)
                for cid in closed_ids:
                    cur.execute("UPDATE paper_closed SET fees=fees-%s, net_pnl=net_pnl+%s WHERE id=%s",
                                (share, share, cid))


def apply_funding(slot, prices):
    """Charges / pays funding on every futures position opened before the
    funding time. Longs pay shorts when the rate is positive."""
    slot_time = datetime.utcfromtimestamp(slot * FUNDING_INTERVAL_SEC)
    with _Cursor() as cur:
        cur.execute("SELECT DISTINCT user_id FROM paper_positions WHERE market='futures' AND opened_at <= %s",
                    (slot_time,))
        users = [r["user_id"] for r in (cur.fetchall() or [])]
    for uid in users:
        try:
            with _UserLock(uid) as cur:
                acct = _ensure_account(cur, uid)
                cur.execute("""SELECT * FROM paper_positions WHERE user_id=%s AND market='futures'
                               AND opened_at <= %s""", (uid, slot_time))
                wallet = float(acct["wallet"])
                for p in cur.fetchall() or []:
                    t = feed.fresh(prices, p["symbol"])
                    mark = t["last"] if t else float(p["entry_price"])
                    pay = float(p["qty"]) * mark * FUNDING_RATE * (1 if p["side"] == "LONG" else -1)
                    wallet -= pay
                    cur.execute("UPDATE paper_positions SET funding=funding+%s WHERE id=%s", (pay, p["id"]))
                    _ledger(cur, uid, int(acct["sess"]), "FUNDING", p["symbol"], -pay, wallet)
                cur.execute("UPDATE paper_accounts SET wallet=%s WHERE user_id=%s", (wallet, uid))
        except Exception:
            print(f"[papertrade] funding failed for user {uid}:\n{traceback.format_exc()}")


def engine_tick(prices=None):
    with _Cursor() as cur:
        cur.execute("SELECT id, user_id, symbol FROM paper_orders WHERE status='NEW' ORDER BY id")
        orders = cur.fetchall() or []
        cur.execute("SELECT * FROM paper_positions")
        positions = cur.fetchall() or []
    if not orders and not positions:
        return
    if prices is None:
        prices = feed.refresh(max_age=ENGINE_INTERVAL_SEC * 0.75)

    for o in orders:
        if feed.fresh(prices, o["symbol"]):
            try:
                _process_order(o["user_id"], o["id"], prices)
            except Busy:
                pass
            except Exception:
                print(f"[papertrade] order {o['id']} failed:\n{traceback.format_exc()}")

    liq_users = set()
    for p in positions:
        t = feed.fresh(prices, p["symbol"])
        if not t:
            continue
        last = t["last"]
        hi, lo = _f(p.get("high_price")), _f(p.get("low_price"))
        if hi is None or last > hi or lo is None or last < lo:
            with _Cursor() as cur:
                cur.execute("UPDATE paper_positions SET high_price=%s, low_price=%s WHERE id=%s",
                            (max(hi or last, last), min(lo or last, last), p["id"]))
        long_ = p["side"] == "LONG"
        tp, sl = _f(p.get("tp_price")), _f(p.get("sl_price"))
        try:
            if sl and (last <= sl if long_ else last >= sl):
                _position_trigger(p["user_id"], p["id"], "SL", prices)
            elif tp and (last >= tp if long_ else last <= tp):
                _position_trigger(p["user_id"], p["id"], "TP", prices)
        except Busy:
            pass
        except Exception:
            print(f"[papertrade] TP/SL {p['id']} failed:\n{traceback.format_exc()}")
        if p["market"] == "futures":
            liq_users.add(p["user_id"])

    for uid in liq_users:
        try:
            _liquidate(uid, prices)
        except Busy:
            pass
        except Exception:
            print(f"[papertrade] liquidation check for {uid} failed:\n{traceback.format_exc()}")


def _engine_row(cur):
    cur.execute("SELECT * FROM paper_engine WHERE id=1")
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO paper_engine (id, heartbeat_at, host, funding_slot) VALUES (1,%s,%s,NULL)",
                    (_utcnow(), socket.gethostname()[:100]))
        cur.execute("SELECT * FROM paper_engine WHERE id=1")
        row = cur.fetchone()
    return row


def funding_check(now_ts=None, prices=None):
    slot = int((now_ts or time.time()) // FUNDING_INTERVAL_SEC)
    with _Cursor() as cur:
        row = _engine_row(cur)
        last_slot = row.get("funding_slot")
        if last_slot is None:
            cur.execute("UPDATE paper_engine SET funding_slot=%s WHERE id=1", (slot,))
            return False
        if int(last_slot) >= slot:
            return False
        cur.execute("UPDATE paper_engine SET funding_slot=%s WHERE id=1", (slot,))
    apply_funding(slot, prices if prices is not None else feed.refresh(max_age=5))
    return True


def _heartbeat():
    with _Cursor() as cur:
        _engine_row(cur)
        cur.execute("UPDATE paper_engine SET heartbeat_at=%s, host=%s WHERE id=1",
                    (_utcnow(), socket.gethostname()[:100]))


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        print(f"[papertrade] {getattr(fn, '__name__', fn)} failed:\n{traceback.format_exc()}")
        return None


def _engine_loop(lock_conn):
    last_hb = 0.0
    while True:
        lock_conn.ping(reconnect=False)
        started = time.time()
        if started - last_hb >= 10:
            last_hb = started
            _safe(_heartbeat)
            _safe(funding_check)
        _safe(engine_tick)
        time.sleep(max(0.2, ENGINE_INTERVAL_SEC - (time.time() - started)))


def _engine_supervisor():
    time.sleep(4)
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
                time.sleep(30)
                continue
            print(f"[papertrade] engine started on {socket.gethostname()} (tick {ENGINE_INTERVAL_SEC}s)")
            _engine_loop(lock_conn)
        except Exception:
            print(f"[papertrade] engine error, restarting in 10s:\n{traceback.format_exc()}")
        finally:
            if lock_conn is not None:
                try:
                    lock_conn.close()
                except Exception:
                    pass
        time.sleep(10)


_engine_started = False
_engine_start_lock = threading.Lock()


def start_engine():
    global _engine_started
    if (os.environ.get("PAPER_ENGINE", "on") or "on").lower() in ("0", "off", "false", "no"):
        print("[papertrade] engine disabled via PAPER_ENGINE")
        return
    with _engine_start_lock:
        if _engine_started:
            return
        _engine_started = True
    threading.Thread(target=_engine_supervisor, name="paper-engine", daemon=True).start()


def init_papertrade(app=None, *, available_coins=None, exchange=None, start=True):
    if available_coins and not _hooks["available_coins"]:
        _hooks["available_coins"] = list(available_coins)
    if exchange is not None:
        _hooks["exchange"] = exchange
    try:
        init_tables()
    except Exception as e:
        print(f"[papertrade] WARNING: could not create tables: {e}")
    if start:
        start_engine()


# ===========================================================================
# Auto-trade bot bridge
# autotrade.py's "Site demo" account (mode "paper") trades THIS demo account
# through the functions below, with the same rules, fees and engine as a
# person clicking on the Demo trading page. Bot orders carry BOT_TAG.
# ===========================================================================
BOT_TAG = "Auto-trade bot"


class BotError(Exception):
    pass


def bot_snapshot(uid):
    prices = feed.refresh(max_age=2.0)
    with _Cursor() as cur:
        acct, positions, orders, _s = _load_user(cur, uid)
    return snapshot(acct, positions, orders, prices)


def bot_price(symbol, max_age=2.0):
    t = feed.fresh(feed.refresh(max_age=max_age), symbol)
    return t["last"] if t else None


def bot_prices(symbols):
    prices = feed.refresh(max_age=2.0)
    out = {}
    for s in symbols:
        t = feed.fresh(prices, s)
        if t:
            out[s] = t["last"]
    return out


def bot_positions(uid, market):
    """{symbol: row} of this user's open demo positions in one market."""
    with _Cursor() as cur:
        cur.execute("SELECT * FROM paper_positions WHERE user_id=%s AND market=%s", (uid, market))
        return {r["symbol"]: r for r in (cur.fetchall() or [])}


def bot_prepare_futures(uid, symbol, leverage):
    """Isolated margin + the bot's leverage for this coin, unless a position or open order already fixes them."""
    lev = max(1, min(int(leverage or 1), symbol_max_leverage(symbol)))
    with _UserLock(uid) as cur:
        cur.execute("SELECT id FROM paper_positions WHERE user_id=%s AND market='futures' AND symbol=%s", (uid, symbol))
        if cur.fetchone():
            return
        cur.execute("SELECT id FROM paper_orders WHERE user_id=%s AND market='futures' AND symbol=%s AND status='NEW'",
                    (uid, symbol))
        if cur.fetchone():
            return
        cur.execute("SELECT * FROM paper_symbol_settings WHERE user_id=%s AND symbol=%s", (uid, symbol))
        if cur.fetchone():
            cur.execute("UPDATE paper_symbol_settings SET leverage=%s, margin_mode='isolated' WHERE user_id=%s AND symbol=%s",
                        (lev, uid, symbol))
        else:
            cur.execute("INSERT INTO paper_symbol_settings (user_id, symbol, leverage, margin_mode) VALUES (%s,%s,%s,'isolated')",
                        (uid, symbol, lev))


def bot_market_order(uid, market, symbol, side, qty, reduce_only=False, opening=False):
    """A market order for the bot. opening=True refuses a coin the user already holds on Demo trading, so a
    hand trade and a bot trade never merge into one position. Returns the fill and, when the position closed,
    the closed-trade row (exit price, reason, net PnL)."""
    if opening:
        pos = bot_positions(uid, market).get(symbol)
        if pos is not None:
            raise BotError(f"You already have a {symbol} {market} position on Demo trading, so the bot leaves this coin alone.")
    req = parse_order({"market": market, "symbol": symbol, "side": side, "type": "MARKET", "qty": qty,
                       "reduce_only": bool(reduce_only) and market == "futures", "tag": BOT_TAG,
                       "notes": "Placed by the auto-trade bot (Site demo account)."})
    try:
        order, plan, result = place_order(uid, req)
    except OrderError as e:
        raise BotError(str(e))
    except Busy as e:
        raise BotError(str(e))
    out = {"id": str(order["id"]), "average": _f(order.get("avg_price")), "filled": _f(order.get("qty")),
           "position_id": (result or {}).get("position_id"), "closed": None}
    if result and result.get("closed_id"):
        with _Cursor() as cur:
            cur.execute("SELECT * FROM paper_closed WHERE id=%s", (result["closed_id"],))
            row = cur.fetchone()
        if row:
            out["closed"] = {"exit_price": _f(row["exit_price"]), "close_reason": row["close_reason"],
                             "net_pnl": _f(row["net_pnl"])}
    return out


def bot_set_tpsl(uid, market, symbol, tp=None, sl=None):
    """Puts the bot's take-profit / stop-loss on its demo position, so the demo engine closes it right at the
    level (every 2 s, even with the page closed) and the Demo trading chart shows the lines."""
    with _UserLock(uid) as cur:
        cur.execute("SELECT * FROM paper_positions WHERE user_id=%s AND market=%s AND symbol=%s", (uid, market, symbol))
        pos = cur.fetchone()
        if not pos:
            return False
        tick = feed.meta(symbol)["price_tick"]
        tp = _round_tick(tp, tick) if tp else None
        sl = _round_tick(sl, tick) if sl else None
        cur.execute("UPDATE paper_positions SET tp_price=%s, sl_price=%s, sl_used=%s, tag=%s, updated_at=%s WHERE id=%s",
                    (tp, sl, 1 if sl else int(pos.get("sl_used") or 0), BOT_TAG, _utcnow(), pos["id"]))
    return True


def bot_last_close(uid, market, symbol, since=None):
    """The newest closed demo trade on this coin (after `since`): exit price, reason and net PnL."""
    with _Cursor() as cur:
        if since is not None:
            cur.execute("""SELECT * FROM paper_closed WHERE user_id=%s AND market=%s AND symbol=%s AND closed_at >= %s
                           ORDER BY id DESC LIMIT 1""", (uid, market, symbol, since))
        else:
            cur.execute("SELECT * FROM paper_closed WHERE user_id=%s AND market=%s AND symbol=%s ORDER BY id DESC LIMIT 1",
                        (uid, market, symbol))
        row = cur.fetchone()
    if not row:
        return None
    return {"exit_price": _f(row["exit_price"]), "close_reason": row["close_reason"], "net_pnl": _f(row["net_pnl"])}


# ===========================================================================
# Serializers
# ===========================================================================
def _order_json(o):
    return {
        "id": o["id"], "market": o["market"], "symbol": o["symbol"], "side": o["side"], "type": o["type"],
        "qty": _f(o["qty"]), "price": _f(o.get("price")), "trigger_price": _f(o.get("trigger_price")),
        "trigger_dir": o.get("trigger_dir"), "callback_pct": _f(o.get("callback_pct")),
        "activation_price": _f(o.get("activation_price")), "trail_extreme": _f(o.get("trail_extreme")),
        "trail_active": bool(o.get("trail_active")), "triggered": bool(o.get("triggered")),
        "reduce_only": bool(o.get("reduce_only")), "post_only": bool(o.get("post_only")), "tif": o.get("tif"),
        "tp_price": _f(o.get("tp_price")), "sl_price": _f(o.get("sl_price")), "leverage": int(o.get("leverage") or 1),
        "margin_mode": o.get("margin_mode"), "reserved": _f(o.get("reserved"), 0.0), "status": o["status"],
        "avg_price": _f(o.get("avg_price")), "fee": _f(o.get("fee")), "realized": _f(o.get("realized")),
        "liquidity": o.get("liquidity"), "source": o.get("source"), "reason": o.get("reason"),
        "notes": o.get("notes"), "tag": o.get("tag"),
        "created_at": _iso(o.get("created_at")), "filled_at": _iso(o.get("filled_at")),
        "updated_at": _iso(o.get("updated_at")),
    }


def _plan_json(plan):
    out = {}
    for k, v in (plan or {}).items():
        out[k] = v
    return out


def _account_json(acct, snap):
    out = {k: v for k, v in snap.items() if k != "positions"}
    out.update({"session": int(acct["sess"]), "reset_at": _iso(acct.get("reset_at"))})
    return out


# ===========================================================================
# Routes
# ===========================================================================
def _require_login():
    uid = _user_id()
    if not uid:
        return None, _err("Login required.", 401)
    return uid, None


def _json_body():
    if not request.is_json:
        return None, _err("A JSON request is required.", 415)
    return request.get_json(silent=True) or {}, None


@paper_bp.route("/api/paper/bootstrap", methods=["GET"])
def bootstrap():
    uid, bad = _require_login()
    if bad:
        return bad
    coins = feed.coins()
    return jsonify({
        "ok": True,
        "coins": [dict(symbol=c, **feed.meta(c)) for c in coins],
        "options": {
            "start_balances": list(START_BALANCES), "order_types": list(ORDER_TYPES), "tifs": list(TIFS),
            "fees": {m: {k: v * 100 for k, v in r.items()} for m, r in FEES.items()},
            "funding_rate_pct": FUNDING_RATE * 100, "funding_interval_h": FUNDING_INTERVAL_SEC // 3600,
            "min_notional": MIN_NOTIONAL, "callback_range": [CALLBACK_MIN, CALLBACK_MAX],
            "default_leverage": DEFAULT_LEVERAGE, "default_margin_mode": DEFAULT_MARGIN_MODE,
            "brackets": {c: bracket_table(c) for c in coins},
        },
    })


@paper_bp.route("/api/paper/tickers", methods=["GET"])
def tickers():
    uid, bad = _require_login()
    if bad:
        return bad
    prices = feed.refresh(max_age=2.0)
    out = []
    for c in feed.coins():
        t = feed.fresh(prices, c)
        if t:
            out.append({k: t[k] for k in ("symbol", "last", "bid", "ask", "high", "low", "change_pct", "vol_quote")})
    return jsonify({"ok": True, "tickers": out, "feed_error": None if out else feed.last_error})


@paper_bp.route("/api/paper/state", methods=["GET"])
def state():
    uid, bad = _require_login()
    if bad:
        return bad
    prices = feed.refresh(max_age=2.0)
    with _Cursor() as cur:
        acct, positions, orders, settings = _load_user(cur, uid)
        cur.execute("SELECT heartbeat_at FROM paper_engine WHERE id=1")
        hb = _to_dt((cur.fetchone() or {}).get("heartbeat_at"))
    snap = snapshot(acct, positions, orders, prices)
    next_funding = (int(time.time() // FUNDING_INTERVAL_SEC) + 1) * FUNDING_INTERVAL_SEC
    return jsonify({
        "ok": True,
        "account": _account_json(acct, snap),
        "positions": snap["positions"],
        "orders": [_order_json(o) for o in orders],
        "symbol_settings": {s: {"leverage": _symbol_settings(settings, s)[0],
                                "margin_mode": _symbol_settings(settings, s)[1]} for s in settings},
        "engine": {"online": bool(hb and (_utcnow() - hb).total_seconds() < 60), "heartbeat_at": _iso(hb)},
        "next_funding_at": datetime.utcfromtimestamp(next_funding).isoformat() + "Z",
        "server_time": _iso(_utcnow()),
    })


@paper_bp.route("/api/paper/preview", methods=["POST"])
def preview():
    uid, bad = _require_login()
    if bad:
        return bad
    data, bad = _json_body()
    if bad:
        return bad
    try:
        req = parse_order(data)
    except ValueError as e:
        return jsonify({"ok": True, "plan": {"errors": [str(e)], "warnings": []}})
    prices = feed.refresh(max_age=2.0)
    with _Cursor() as cur:
        acct, positions, orders, settings = _load_user(cur, uid)
    plan = plan_order(acct, positions, orders, settings, prices, req)
    return jsonify({"ok": True, "plan": _plan_json(plan)})


@paper_bp.route("/api/paper/orders", methods=["POST"])
def create_order():
    uid, bad = _require_login()
    if bad:
        return bad
    data, bad = _json_body()
    if bad:
        return bad
    try:
        req = parse_order(data)
        order, plan, result = place_order(uid, req)
    except ValueError as e:
        return _err(str(e))
    except OrderError as e:
        return _err(str(e), plan=_plan_json(e.plan))
    except Busy as e:
        return _err(str(e), 409)
    return jsonify({"ok": True, "order": _order_json(order), "plan": _plan_json(plan), "fill": result})


@paper_bp.route("/api/paper/orders/<int:order_id>/cancel", methods=["POST"])
def cancel_one(order_id):
    uid, bad = _require_login()
    if bad:
        return bad
    try:
        ok = cancel_order(uid, order_id)
    except Busy as e:
        return _err(str(e), 409)
    if not ok:
        return _err("Order not found or no longer open.", 404)
    return jsonify({"ok": True})


@paper_bp.route("/api/paper/orders/cancel-all", methods=["POST"])
def cancel_all():
    uid, bad = _require_login()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol") or "").upper() or None
    try:
        with _UserLock(uid) as cur:
            sql = "UPDATE paper_orders SET status='CANCELED', reserved=0, reason='Canceled by you', updated_at=%s " \
                  "WHERE user_id=%s AND status='NEW'"
            params = [_utcnow(), uid]
            if symbol:
                sql += " AND symbol=%s"
                params.append(symbol)
            cur.execute(sql, tuple(params))
            n = cur.rowcount
    except Busy as e:
        return _err(str(e), 409)
    return jsonify({"ok": True, "canceled": n})


@paper_bp.route("/api/paper/positions/<int:position_id>/close", methods=["POST"])
def close_position_route(position_id):
    uid, bad = _require_login()
    if bad:
        return bad
    data, bad = _json_body()
    if bad:
        return bad
    fraction = _f(data.get("fraction"), 1.0)
    if fraction is None or fraction <= 0 or fraction > 1:
        return _err("Close size must be between 1% and 100%.")
    ctype = str(data.get("type") or "MARKET").upper()
    prices = feed.refresh(max_age=1.0)
    try:
        with _Cursor() as cur:
            cur.execute("SELECT * FROM paper_positions WHERE id=%s AND user_id=%s", (position_id, uid))
            pos = cur.fetchone()
        if not pos:
            return _err("Position not found.", 404)
        if ctype == "MARKET":
            with _UserLock(uid) as cur:
                cur.execute("SELECT * FROM paper_positions WHERE id=%s AND user_id=%s", (position_id, uid))
                pos = cur.fetchone()
                if not pos:
                    return _err("Position not found.", 404)
                res = _market_close(cur, uid, pos, prices, fraction, source="USER")
            return jsonify({"ok": True, "fill": res})
        if ctype != "LIMIT":
            return _err("Close type must be MARKET or LIMIT.")
        meta = feed.meta(pos["symbol"])
        qty = float(pos["qty"]) if fraction >= 0.999 else _floor_step(float(pos["qty"]) * fraction, meta["qty_step"])
        req = parse_order({"market": pos["market"], "symbol": pos["symbol"],
                           "side": "SELL" if pos["side"] == "LONG" else "BUY", "type": "LIMIT", "qty": qty,
                           "price": data.get("price"), "reduce_only": pos["market"] == "futures", "tif": "GTC"})
        order, plan, result = place_order(uid, req)
        return jsonify({"ok": True, "order": _order_json(order), "fill": result})
    except _Reject as e:
        return _err(str(e))
    except ValueError as e:
        return _err(str(e))
    except OrderError as e:
        return _err(str(e), plan=_plan_json(e.plan))
    except Busy as e:
        return _err(str(e), 409)


@paper_bp.route("/api/paper/positions/<int:position_id>/tpsl", methods=["POST"])
def set_tpsl(position_id):
    uid, bad = _require_login()
    if bad:
        return bad
    data, bad = _json_body()
    if bad:
        return bad
    prices = feed.refresh(max_age=2.0)
    try:
        with _UserLock(uid) as cur:
            cur.execute("SELECT * FROM paper_positions WHERE id=%s AND user_id=%s", (position_id, uid))
            pos = cur.fetchone()
            if not pos:
                return _err("Position not found.", 404)
            tick = feed.meta(pos["symbol"])["price_tick"]
            tp = _round_tick(_f(data.get("tp_price")), tick) if _f(data.get("tp_price")) else None
            sl = _round_tick(_f(data.get("sl_price")), tick) if _f(data.get("sl_price")) else None
            t = feed.fresh(prices, pos["symbol"])
            ref = t["last"] if t else float(pos["entry_price"])
            long_ = pos["side"] == "LONG"
            if tp and ((long_ and tp <= ref) or (not long_ and tp >= ref)):
                return _err(f"Take-profit must be {'above' if long_ else 'below'} the current price ({_fmt(ref)}).")
            if sl and ((long_ and sl >= ref) or (not long_ and sl <= ref)):
                return _err(f"Stop-loss must be {'below' if long_ else 'above'} the current price ({_fmt(ref)}).")
            cur.execute("UPDATE paper_positions SET tp_price=%s, sl_price=%s, sl_used=%s, updated_at=%s WHERE id=%s",
                        (tp, sl, 1 if (sl or pos.get("sl_used")) else 0, _utcnow(), position_id))
    except Busy as e:
        return _err(str(e), 409)
    return jsonify({"ok": True, "tp_price": tp, "sl_price": sl})


@paper_bp.route("/api/paper/positions/<int:position_id>/margin", methods=["POST"])
def adjust_margin(position_id):
    uid, bad = _require_login()
    if bad:
        return bad
    data, bad = _json_body()
    if bad:
        return bad
    amount = _f(data.get("amount"))
    if not amount:
        return _err("Enter an amount.")
    prices = feed.refresh(max_age=2.0)
    try:
        with _UserLock(uid) as cur:
            acct, positions, orders, _s = _load_user(cur, uid)
            pos = next((p for p in positions if p["id"] == position_id), None)
            if not pos:
                return _err("Position not found.", 404)
            if pos["market"] != "futures" or pos["margin_mode"] != "isolated":
                return _err("Margin can only be adjusted on isolated futures positions.")
            snap = snapshot(acct, positions, orders, prices)
            margin = float(pos["margin"])
            if amount > 0 and amount > snap["available"] + 1e-9:
                return _err(f"Only {max(0, snap['available']):,.2f} USDT is available to add.")
            if amount < 0:
                row = next(r for r in snap["positions"] if r["id"] == position_id)
                floor_margin = float(pos["qty"]) * float(pos["entry_price"]) / int(pos["leverage"]) + max(0.0, -row["upnl"])
                max_remove = max(0.0, margin - floor_margin)
                if -amount > max_remove + 1e-9:
                    return _err(f"You can remove at most {max_remove:,.2f} USDT from this position.")
            cur.execute("UPDATE paper_positions SET margin=%s, updated_at=%s WHERE id=%s",
                        (margin + amount, _utcnow(), position_id))
    except Busy as e:
        return _err(str(e), 409)
    return jsonify({"ok": True, "margin": margin + amount})


@paper_bp.route("/api/paper/leverage", methods=["POST"])
def set_leverage():
    uid, bad = _require_login()
    if bad:
        return bad
    data, bad = _json_body()
    if bad:
        return bad
    symbol = str(data.get("symbol") or "").upper()
    if symbol not in feed.coins():
        return _err("This coin is not available for demo trading.")
    try:
        lev = int(data.get("leverage"))
    except (TypeError, ValueError):
        return _err("Invalid leverage.")
    mode = str(data.get("margin_mode") or DEFAULT_MARGIN_MODE).lower()
    if mode not in MARGIN_MODES:
        return _err("Margin mode must be isolated or cross.")
    mx = symbol_max_leverage(symbol)
    if lev < 1 or lev > mx:
        return _err(f"Leverage for {symbol} must be between 1x and {mx}x.")
    try:
        with _UserLock(uid) as cur:
            cur.execute("SELECT id FROM paper_positions WHERE user_id=%s AND market='futures' AND symbol=%s", (uid, symbol))
            has_pos = cur.fetchone() is not None
            cur.execute("""SELECT id FROM paper_orders WHERE user_id=%s AND market='futures' AND symbol=%s
                           AND status='NEW'""", (uid, symbol))
            has_orders = cur.fetchone() is not None
            cur.execute("SELECT * FROM paper_symbol_settings WHERE user_id=%s AND symbol=%s", (uid, symbol))
            cur_row = cur.fetchone()
            cur_lev, cur_mode = _symbol_settings({symbol: cur_row} if cur_row else {}, symbol)
            if (has_pos or has_orders) and (lev != cur_lev or mode != cur_mode):
                return _err(f"Close your {symbol} futures position and cancel its open orders before changing "
                            f"leverage or margin mode.")
            if cur_row:
                cur.execute("UPDATE paper_symbol_settings SET leverage=%s, margin_mode=%s WHERE user_id=%s AND symbol=%s",
                            (lev, mode, uid, symbol))
            else:
                cur.execute("INSERT INTO paper_symbol_settings (user_id, symbol, leverage, margin_mode) VALUES (%s,%s,%s,%s)",
                            (uid, symbol, lev, mode))
    except Busy as e:
        return _err(str(e), 409)
    return jsonify({"ok": True, "symbol": symbol, "leverage": lev, "margin_mode": mode,
                    "max_notional": max_notional_for(lev, symbol)})


@paper_bp.route("/api/paper/reset", methods=["POST"])
def reset_account():
    uid, bad = _require_login()
    if bad:
        return bad
    data, bad = _json_body()
    if bad:
        return bad
    try:
        start = int(data.get("start_balance") or DEFAULT_START)
    except (TypeError, ValueError):
        return _err("Invalid starting balance.")
    if start not in START_BALANCES:
        return _err("Choose one of the listed starting balances.")
    try:
        with _UserLock(uid) as cur:
            acct = _ensure_account(cur, uid)
            new_sess = int(acct["sess"]) + 1
            now = _utcnow()
            cur.execute("""UPDATE paper_orders SET status='CANCELED', reserved=0, reason='Account reset', updated_at=%s
                           WHERE user_id=%s AND status='NEW'""", (now, uid))
            cur.execute("DELETE FROM paper_positions WHERE user_id=%s", (uid,))
            cur.execute("UPDATE paper_accounts SET sess=%s, start_balance=%s, wallet=%s, reset_at=%s WHERE user_id=%s",
                        (new_sess, start, start, now, uid))
            _ledger(cur, uid, new_sess, "DEPOSIT", None, start, start)
    except Busy as e:
        return _err(str(e), 409)
    return jsonify({"ok": True, "session": new_sess, "start_balance": start})


@paper_bp.route("/api/paper/history", methods=["GET"])
def history():
    uid, bad = _require_login()
    if bad:
        return bad
    kind = request.args.get("kind", "orders")
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except ValueError:
        limit = 100
    with _Cursor() as cur:
        acct = _ensure_account(cur, uid)
        sess = int(acct["sess"])
        if kind == "orders":
            cur.execute(f"""SELECT * FROM paper_orders WHERE user_id=%s AND sess=%s AND status<>'NEW'
                            ORDER BY id DESC LIMIT {limit}""", (uid, sess))
            rows = [_order_json(o) for o in cur.fetchall() or []]
        elif kind == "fills":
            cur.execute(f"SELECT * FROM paper_fills WHERE user_id=%s AND sess=%s ORDER BY id DESC LIMIT {limit}", (uid, sess))
            rows = [{"id": r["id"], "order_id": r["order_id"], "market": r["market"], "symbol": r["symbol"],
                     "side": r["side"], "qty": _f(r["qty"]), "price": _f(r["price"]), "fee": _f(r["fee"]),
                     "liquidity": r["liquidity"], "realized": _f(r["realized"]), "created_at": _iso(r["created_at"])}
                    for r in cur.fetchall() or []]
        elif kind == "closed":
            cur.execute(f"SELECT * FROM paper_closed WHERE user_id=%s AND sess=%s ORDER BY id DESC LIMIT {limit}", (uid, sess))
            rows = [_closed_json(r) for r in cur.fetchall() or []]
        elif kind == "ledger":
            cur.execute(f"SELECT * FROM paper_ledger WHERE user_id=%s AND sess=%s ORDER BY id DESC LIMIT {limit}", (uid, sess))
            rows = [{"id": r["id"], "type": r["type"], "symbol": r["symbol"], "amount": _f(r["amount"]),
                     "balance_after": _f(r["balance_after"]), "created_at": _iso(r["created_at"])}
                    for r in cur.fetchall() or []]
        else:
            return _err("Unknown history type.")
    return jsonify({"ok": True, "kind": kind, "rows": rows})


def _closed_json(r):
    opened, closed = _to_dt(r["opened_at"]), _to_dt(r["closed_at"])
    return {
        "id": r["id"], "market": r["market"], "symbol": r["symbol"], "side": r["side"],
        "leverage": int(r["leverage"]), "margin_mode": r["margin_mode"], "qty": _f(r["qty"]),
        "entry_price": _f(r["entry_price"]), "exit_price": _f(r["exit_price"]), "realized": _f(r["realized"]),
        "fees": _f(r["fees"]), "funding": _f(r["funding"]), "net_pnl": _f(r["net_pnl"]), "roe_pct": _f(r["roe_pct"]),
        "close_reason": r["close_reason"], "sl_used": bool(r["sl_used"]), "mfe_pct": _f(r["mfe_pct"]),
        "mae_pct": _f(r["mae_pct"]), "notes": r.get("notes"), "tag": r.get("tag"),
        "opened_at": _iso(opened), "closed_at": _iso(closed),
        "duration_sec": int((closed - opened).total_seconds()) if opened and closed else None,
    }


@paper_bp.route("/api/paper/closed/<int:closed_id>/journal", methods=["POST"])
def journal(closed_id):
    uid, bad = _require_login()
    if bad:
        return bad
    data, bad = _json_body()
    if bad:
        return bad
    notes = (str(data.get("notes") or "").strip()[:NOTES_MAX]) or None
    tag = (str(data.get("tag") or "").strip()[:TAG_MAX]) or None
    with _Cursor() as cur:
        cur.execute("UPDATE paper_closed SET notes=%s, tag=%s WHERE id=%s AND user_id=%s", (notes, tag, closed_id, uid))
        if cur.rowcount != 1:
            cur.execute("SELECT id FROM paper_closed WHERE id=%s AND user_id=%s", (closed_id, uid))
            if cur.fetchone() is None:
                return _err("Trade not found.", 404)
    return jsonify({"ok": True})


@paper_bp.route("/api/paper/positions/<int:position_id>/journal", methods=["POST"])
def position_journal(position_id):
    uid, bad = _require_login()
    if bad:
        return bad
    data, bad = _json_body()
    if bad:
        return bad
    notes = (str(data.get("notes") or "").strip()[:NOTES_MAX]) or None
    tag = (str(data.get("tag") or "").strip()[:TAG_MAX]) or None
    with _Cursor() as cur:
        cur.execute("SELECT id FROM paper_positions WHERE id=%s AND user_id=%s", (position_id, uid))
        if cur.fetchone() is None:
            return _err("Position not found.", 404)
        cur.execute("UPDATE paper_positions SET notes=%s, tag=%s WHERE id=%s", (notes, tag, position_id))
    return jsonify({"ok": True})


def compute_stats(closed, ledger, start_balance):
    """Pure: closed trades (oldest first) + ledger rows -> analytics."""
    n = len(closed)
    nets = [float(c["net_pnl"]) for c in closed]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gp, gl = sum(wins), -sum(losses)

    def side_stats(side):
        rows = [c for c in closed if c["side"] == side]
        w = [c for c in rows if float(c["net_pnl"]) > 0]
        return {"count": len(rows), "win_rate": len(w) / len(rows) * 100 if rows else None,
                "net": sum(float(c["net_pnl"]) for c in rows)}

    by_symbol = {}
    for c in closed:
        s = by_symbol.setdefault(c["symbol"], {"symbol": c["symbol"], "count": 0, "wins": 0, "net": 0.0})
        s["count"] += 1
        s["wins"] += 1 if float(c["net_pnl"]) > 0 else 0
        s["net"] += float(c["net_pnl"])
    symbols = sorted(by_symbol.values(), key=lambda s: (-s["count"], -s["net"]))[:10]
    for s in symbols:
        s["win_rate"] = s["wins"] / s["count"] * 100

    best_w = best_l = cur_w = cur_l = 0
    for x in nets:
        if x > 0:
            cur_w, cur_l = cur_w + 1, 0
        else:
            cur_l, cur_w = cur_l + 1, 0
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)

    durations = []
    for c in closed:
        o, cl = _to_dt(c["opened_at"]), _to_dt(c["closed_at"])
        if o and cl:
            durations.append((cl - o).total_seconds())

    curve = [{"t": _iso(r["created_at"]), "v": float(r["balance_after"])} for r in ledger]
    peak, max_dd = None, 0.0
    for p in curve:
        v = p["v"]
        peak = v if peak is None or v > peak else peak
        if peak and peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100)
    if len(curve) > 400:  # thin the curve for the chart, keep the last point
        stride = math.ceil(len(curve) / 400)
        curve = curve[::stride] + ([curve[-1]] if (len(curve) - 1) % stride else [])

    fees = -sum(float(r["amount"]) for r in ledger if r["type"] in ("FEE", "LIQUIDATION_FEE"))
    funding = -sum(float(r["amount"]) for r in ledger if r["type"] == "FUNDING")
    avg_win = gp / len(wins) if wins else None
    avg_loss = -gl / len(losses) if losses else None
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / n * 100 if n else None,
        "net_pnl": sum(nets), "gross_profit": gp, "gross_loss": gl,
        "profit_factor": gp / gl if gl > 0 else None,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "payoff_ratio": avg_win / -avg_loss if avg_win and avg_loss else None,
        "expectancy": sum(nets) / n if n else None,
        "largest_win": max(nets) if nets else None, "largest_loss": min(nets) if nets else None,
        "avg_duration_sec": sum(durations) / len(durations) if durations else None,
        "max_drawdown_pct": max_dd, "fees_paid": fees, "funding_paid": funding,
        "long": side_stats("LONG"), "short": side_stats("SHORT"), "by_symbol": symbols,
        "best_win_streak": best_w, "worst_loss_streak": best_l,
        "sl_usage_pct": sum(1 for c in closed if int(c.get("sl_used") or 0)) / n * 100 if n else None,
        "liquidations": sum(1 for c in closed if c["close_reason"] == "LIQUIDATION"),
        "avg_mfe_pct": (sum(_f(c.get("mfe_pct"), 0) for c in closed) / n) if n else None,
        "avg_mae_pct": (sum(_f(c.get("mae_pct"), 0) for c in closed) / n) if n else None,
        "start_balance": start_balance, "curve": curve,
    }


@paper_bp.route("/api/paper/stats", methods=["GET"])
def stats():
    uid, bad = _require_login()
    if bad:
        return bad
    prices = feed.refresh(max_age=2.0)
    with _Cursor() as cur:
        acct, positions, orders, _s = _load_user(cur, uid)
        sess = int(acct["sess"])
        cur.execute("SELECT * FROM paper_closed WHERE user_id=%s AND sess=%s ORDER BY id", (uid, sess))
        closed = cur.fetchall() or []
        cur.execute("SELECT type, amount, balance_after, created_at FROM paper_ledger WHERE user_id=%s AND sess=%s ORDER BY id",
                    (uid, sess))
        ledger = cur.fetchall() or []
    out = compute_stats(closed, ledger, float(acct["start_balance"]))
    snap = snapshot(acct, positions, orders, prices)
    out["equity"] = snap["equity"]
    out["return_pct"] = snap["return_pct"]
    return jsonify({"ok": True, "stats": out})
