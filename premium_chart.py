"""
Premium chart workspace - the page behind the "Premium chart Access" button (free for every member right now).

Page (login required):
  GET /premium-chart      templates/premium-chart.html + static/premium-chart-core.js + static/premium-chart.js
                          ?coin=ETH/USDT&interval=7m&layout=4&feature=replay (all optional)

What it offers (each one is a button in the "Premium chart Access" menu):
  up to 16 charts per tab, many live charts at once (one shared price feed for every chart in every tab), custom
  intervals (seconds, minutes, hours, days, weeks, months), range bars, bar replay, second intervals, price-based
  charts (Renko, Kagi, Line break, Point & figure) on intraday data, spread charts (A / B, A - B ...), chart data
  export (CSV), tick charts and a government bond screener.

API (login required; main.py rate-limits the blueprint per member):
  GET /api/premium/bars?coin=&interval=&limit=&before=
        Candles for any time interval from 1 minute to 12 months. Native intervals come straight from the
        exchange (the same candles as the Live chart); custom ones (7m, 45m, 3h, 5h, 2D, 2W, 6M ...) are built
        from the largest native interval that divides them. Buckets: intraday ones start again at 00:00 UTC
        every day, days count from 1970-01-01, weeks start on Monday, months from January.
  GET /api/premium/trades?coin=&depth=0..10
        The exchange's public trades for a crypto coin, oldest first: the latest 500 plus `depth` older pages of
        100. Second and tick charts are built from them in the browser and kept live with depth=0.
  GET /api/premium/prices?coins=A,B,...
        Last price of up to 200 markets in one call. Every crypto coin comes from ONE exchange call that all
        members share for 2 seconds, so a workspace with many charts in many tabs stays light.
  GET /api/premium/bonds                      bond screener rows (bonds.py)
  GET /api/premium/bonds/history?id=US10Y     one bond's daily yields
"""
import math
import re
import threading
import time
from datetime import datetime, timezone

import pandas as pd
from flask import Blueprint, jsonify, redirect, render_template, request, session

import bonds

premium_bp = Blueprint("premium_chart", __name__)

# the exchange intervals the site can fetch for every market (main.TIMEFRAME_SECONDS, forex_data.SEC)
NATIVE_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
              "6h": 21600, "12h": 43200, "1d": 86400, "1w": 604800}
UNIT_SEC = {"s": 1, "m": 60, "h": 3600, "D": 86400, "W": 604800}
UNIT_LIMITS = {"s": (1, 300), "m": (1, 1440), "h": (1, 168), "D": (1, 365), "W": (1, 52), "M": (1, 12),
               "T": (1, 10000)}
MONTH_STEPS = (1, 2, 3, 4, 6, 12)          # month buckets restart every January
UNIT_ALIASES = {"s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
                "m": "m", "min": "m", "mins": "m", "minute": "m", "minutes": "m",
                "h": "h", "H": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
                "d": "D", "D": "D", "day": "D", "days": "D",
                "w": "W", "W": "W", "wk": "W", "week": "W", "weeks": "W",
                "M": "M", "mo": "M", "mon": "M", "month": "M", "months": "M",
                "t": "T", "T": "T", "tick": "T", "ticks": "T"}
MONDAY0 = 4 * 86400                         # 1970-01-05 00:00 UTC, the first Monday
DEFAULT_BARS, MAX_BARS = 300, 1000
MAX_BASE = 2400                             # exchange candles one request may combine (about 8-24 exchange calls)
CRYPTO_PAGE = 300                           # the exchange returns at most 300 candles per call
MAX_PRICE_SYMBOLS = 200
MAX_FOREX_PRICES = 12                       # forex prices come from a paid-per-request source
MAX_TRADE_PAGES = 10

_hooks = {}
_cache = {}
_cache_lock = threading.Lock()
_key_locks = {}


# ================================================================================================ intervals
class Interval:
    """A parsed interval: n + unit (s, m, h, D, W, M or T)."""

    def __init__(self, n, unit):
        self.n, self.unit = n, unit

    @property
    def name(self):
        return f"{self.n}{self.unit}"

    @property
    def kind(self):
        return {"s": "second", "T": "tick", "M": "month"}.get(self.unit, "time")

    @property
    def seconds(self):
        return self.n * UNIT_SEC[self.unit] if self.unit in UNIT_SEC else None

    @property
    def native(self):
        """The exchange's own interval name when this interval is one (60m -> 1h), else None."""
        if self.unit in ("M", "W", "D"):      # 7D is not 1W: days count from 1970-01-01, weeks start on Monday
            return {"M": "1M", "W": "1w", "D": "1d"}[self.unit] if self.n == 1 else None
        if self.kind != "time":
            return None
        for name, sec in NATIVE_SEC.items():
            if sec == self.seconds and sec < 86400:
                return name
        return None

    @property
    def base(self):
        """The native interval the bars are built from."""
        if self.unit == "M":
            return "1M"
        sec = self.seconds
        if sec % 86400 == 0 and self.unit != "W":
            return "1d"
        if self.unit == "W":
            return "1w"
        best = "1m"
        for name, s in NATIVE_SEC.items():
            if s < 86400 and s <= sec and sec % s == 0 and s > NATIVE_SEC[best]:
                best = name
        return best

    @property
    def ratio(self):
        """At most how many base candles make one bar."""
        if self.unit in ("M", "W"):
            return self.n
        if self.base == "1d":
            return self.seconds // 86400
        return max(1, self.seconds // NATIVE_SEC[self.base])

    def to_json(self):
        return {"name": self.name, "n": self.n, "unit": self.unit, "kind": self.kind, "seconds": self.seconds,
                "native": self.native, "base": self.base if self.kind in ("time", "month") else None}


def parse_interval(raw):
    """'7m', '2h', '3D', '2W', '6M', '10s', '100T' (also '1d', '15 min', '2 weeks') -> Interval, or None."""
    m = re.match(r"^\s*(\d{1,5})\s*([A-Za-z]+)\s*$", str(raw or ""))
    if not m:
        return None
    n, word = int(m.group(1)), m.group(2)
    # "M" is a month and "m" a minute; every longer word is read without caring about upper / lower case
    unit = "M" if word == "M" else (UNIT_ALIASES.get(word) or UNIT_ALIASES.get(word.lower()))
    if unit is None:
        return None
    lo, hi = UNIT_LIMITS[unit]
    if not lo <= n <= hi:
        return None
    if unit == "M" and n not in MONTH_STEPS:
        return None
    return Interval(n, unit)


def _month_index(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.year * 12 + d.month - 1


def _month_start(idx):
    return int((datetime(idx // 12, idx % 12 + 1, 1) - datetime(1970, 1, 1)).total_seconds())


def bucket_start(ts, iv):
    """Start (unix seconds, UTC) of the bar that contains `ts`."""
    ts = int(ts)
    if iv.unit == "M":
        idx = _month_index(ts)
        return _month_start(idx - idx % iv.n)
    if iv.unit == "W":
        w = (ts - MONDAY0) // 604800
        return MONDAY0 + (w - w % iv.n) * 604800
    sec = iv.seconds
    if sec < 86400:                               # intraday: restart at 00:00 UTC every day
        day = ts - ts % 86400
        return day + ((ts - day) // sec) * sec
    if sec % 86400 == 0:                          # whole days, counted from 1970-01-01
        days = sec // 86400
        d = ts // 86400
        return (d - d % days) * 86400
    return ts - ts % sec


def bucket_end(start, iv):
    """Start of the next bar."""
    if iv.unit == "M":
        return _month_start(_month_index(start) + iv.n)
    if iv.unit == "W":
        return start + iv.n * 604800
    sec = iv.seconds
    if sec < 86400:
        return min(start + sec, start - start % 86400 + 86400)
    return start + sec


def aggregate(df, iv):
    """Base candles (timestamp, open, high, low, close, volume) -> bars of interval `iv`, oldest first. Each bar
    carries `n`, the number of base candles in it."""
    if df is None or not len(df):
        return []
    ts = pd.to_datetime(df["timestamp"])
    secs = (ts - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)
    frame = pd.DataFrame({"t": secs.values, "open": df["open"].astype(float).values,
                          "high": df["high"].astype(float).values, "low": df["low"].astype(float).values,
                          "close": df["close"].astype(float).values,
                          "volume": pd.to_numeric(df["volume"], errors="coerce").fillna(0.0).astype(float).values})
    frame = frame.drop_duplicates("t").sort_values("t")
    frame["b"] = [bucket_start(t, iv) for t in frame["t"].values]
    g = frame.groupby("b", sort=True)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum(), "n": g["t"].count()})
    return [{"time": int(b), "open": _sig(r.open), "high": _sig(r.high), "low": _sig(r.low), "close": _sig(r.close),
             "volume": round(float(r.volume), 8), "n": int(r.n)} for b, r in out.iterrows()]


def expected_parts(start, iv):
    """How many base candles a complete bar starting at `start` has."""
    if iv.unit in ("M", "W"):
        return iv.n
    base = NATIVE_SEC[iv.base]
    return max(1, (bucket_end(start, iv) - start) // base)


# ================================================================================================ helpers
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _sig(v, digits=10):
    v = _num(v)
    return None if v is None else float(f"{v:.{digits}g}")


def _cached(key, ttl, fn):
    """fn()'s result shared by every member for `ttl` seconds; one computation per key at a time."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _cache_lock:
        lock = _key_locks.setdefault(key, threading.Lock())
    with lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        with _cache_lock:
            _cache[key] = (time.time(), val)
            if len(_cache) > 400:
                for k, _v in sorted(_cache.items(), key=lambda kv: kv[1][0])[:120]:
                    _cache.pop(k, None)
                    _key_locks.pop(k, None)
        return val


def _stale(key):
    hit = _cache.get(key)
    return hit[1] if hit else None


def _login():
    if not session.get("user_id"):
        return jsonify({"ok": False, "error": "Please log in."}), 401
    return None


def _coin(raw):
    fn = _hooks.get("clean_coin")
    return fn(raw) if fn else None


def _is_forex(sym):
    fn = _hooks.get("is_forex")
    return bool(fn and fn(sym))


def _fail(where, e):
    print(f"[premium:{where}] {type(e).__name__}: {e}")
    return jsonify({"ok": False, "error": "Data is not available right now. Please try again in a moment."}), 502


# ================================================================================================ data
def fetch_base(coin, base_tf, count, before_ts=None, now=None):
    """`count` candles of the native interval `base_tf` ending at the newest one (or right before `before_ts`)."""
    get = _hooks["get_candles"]
    sec = NATIVE_SEC.get(base_tf, 2592000)
    if before_ts is None and (count <= CRYPTO_PAGE or _is_forex(coin)):
        return get(symbol=coin, timeframe=base_tf, limit=count)       # newest candles, one call
    from market_tools import candles_before
    end = int(before_ts) if before_ts is not None else int(now or time.time()) + sec
    calls = min(40, count // 100 + 4)
    return candles_before(get, coin, base_tf, count, end, sec, max_calls=calls)


def build_bars(coin, iv, limit, before_ts=None, now=None):
    """Bars of interval `iv` (time or month kind) for `coin`: the newest `limit`, or the `limit` before
    `before_ts`. The oldest bar is dropped when the fetched window starts inside it (it would be cut short)."""
    ratio = iv.ratio
    limit = max(1, min(int(limit), MAX_BARS, max(1, MAX_BASE // ratio)))
    if iv.native and iv.unit != "M":
        count = limit
    else:
        count = min(MAX_BASE, (limit + 1) * ratio)
    df = fetch_base(coin, iv.base, count, before_ts=before_ts, now=now)
    bars = aggregate(df, iv)
    if bars and not iv.native and bars[0]["n"] < expected_parts(bars[0]["time"], iv) and len(bars) > 1:
        bars = bars[1:]
    if before_ts is not None:
        bars = [b for b in bars if b["time"] < int(before_ts)]
    bars = bars[-limit:]
    for b in bars:
        b.pop("n", None)
    got = 0 if df is None else len(df)
    return {"bars": bars, "limit": limit, "more": bool(bars) and got >= max(1, int(count * 0.9)),
            "next_start": bucket_end(bars[-1]["time"], iv) if bars else None}


def fetch_trades(coin, depth=0):
    """The exchange's public trades, oldest first: [[time_ms, price, amount, side (1 buy / -1 sell), id], ...]."""
    ex = _hooks["exchange"]
    seen, rows = set(), []

    def take(trades):
        added = 0
        for t in trades or []:
            tid = str(t.get("id") or "")
            ts, px, qty = t.get("timestamp"), _num(t.get("price")), _num(t.get("amount"))
            if not tid or tid in seen or ts is None or px is None:
                continue
            seen.add(tid)
            rows.append([int(ts), _sig(px), _sig(qty or 0.0), 1 if t.get("side") == "buy" else -1, tid])
            added += 1
        return added

    take(ex.fetch_trades(coin, limit=500))
    for _ in range(max(0, min(int(depth), MAX_TRADE_PAGES))):
        if not rows:
            break
        oldest = min(rows, key=lambda r: (r[0], _tid_key(r[4])))[4]
        try:
            page = ex.fetch_trades(coin, limit=100, params={"method": "publicGetMarketHistoryTrades", "after": oldest})
        except Exception:
            break
        if not take(page):
            break
    rows.sort(key=lambda r: (r[0], _tid_key(r[4])))
    return rows


def _tid_key(tid):
    return int(tid) if str(tid).isdigit() else 0


def all_prices(symbols):
    """{symbol: {last, high, low, change_pct}} for every symbol that has a price right now."""
    crypto = [s for s in symbols if not _is_forex(s)]
    forex = [s for s in symbols if _is_forex(s)][:MAX_FOREX_PRICES]
    out = {}
    if crypto:
        try:
            tick = _cached(("tickers",), 2.0, _hooks["tickers"])
        except Exception:
            tick = _stale(("tickers",)) or {}
        for s in crypto:
            t = tick.get(s)
            if t and t.get("last") is not None:
                out[s] = {"last": t["last"], "high": t.get("high"), "low": t.get("low"),
                          "change_pct": t.get("change_pct")}
    fx = _hooks.get("forex_price")
    for s in forex:
        try:
            p = fx(s) if fx else None
        except Exception:
            p = None
        if p is not None:
            out[s] = {"last": _sig(p)}
    return out


# ================================================================================================ routes
@premium_bp.route("/premium-chart", methods=["GET"])
def premium_page():
    if not session.get("user_id"):
        return redirect("/login")
    return render_template("premium-chart.html", active="premium", user_email=session.get("email", ""),
                           user_avatar=session.get("avatar_url"))


@premium_bp.route("/api/premium/bars", methods=["GET"])
def api_bars():
    bad = _login()
    if bad:
        return bad
    coin = _coin(request.args.get("coin"))
    iv = parse_interval(request.args.get("interval"))
    if not coin:
        return jsonify({"ok": False, "error": "Unknown coin or pair."}), 400
    if not iv:
        return jsonify({"ok": False, "error": "Unknown interval. Examples: 7m, 45m, 3h, 2D, 2W, 6M."}), 400
    if iv.kind in ("second", "tick"):
        return jsonify({"ok": False, "error": "Second and tick charts are built from trades: use /api/premium/trades."}), 400
    try:
        limit = int(request.args.get("limit", DEFAULT_BARS))
    except ValueError:
        limit = DEFAULT_BARS
    before = request.args.get("before")
    try:
        before_ts = int(before) if before else None
    except ValueError:
        before_ts = None
    small = before_ts is None and limit <= 5
    key = ("bars", coin, iv.name, max(1, min(limit, MAX_BARS)), before_ts)
    ttl = 2.0 if small else (120.0 if before_ts else 10.0)
    try:
        data = _cached(key, ttl, lambda: build_bars(coin, iv, limit, before_ts))
    except Exception as e:
        stale = _stale(key)
        if stale is None:
            return _fail("bars", e)
        data = dict(stale, stale=True)
    return jsonify(dict(data, ok=True, coin=coin, interval=iv.to_json(), server_time=int(time.time())))


@premium_bp.route("/api/premium/trades", methods=["GET"])
def api_trades():
    bad = _login()
    if bad:
        return bad
    coin = _coin(request.args.get("coin"))
    if not coin:
        return jsonify({"ok": False, "error": "Unknown coin or pair."}), 400
    if _is_forex(coin):
        return jsonify({"ok": False, "error": "Second and tick charts need the exchange's trades, so they are for crypto "
                                              "coins only. Forex pairs have no public trades."}), 400
    try:
        depth = max(0, min(int(request.args.get("depth", 0)), MAX_TRADE_PAGES))
    except ValueError:
        depth = 0
    key = ("trades", coin, depth)
    try:
        rows = _cached(key, 1.0 if depth == 0 else 15.0, lambda: fetch_trades(coin, depth))
    except Exception as e:
        rows = _stale(key)
        if rows is None:
            return _fail("trades", e)
    return jsonify({"ok": True, "coin": coin, "trades": rows, "server_time": time.time()})


@premium_bp.route("/api/premium/prices", methods=["GET"])
def api_prices():
    bad = _login()
    if bad:
        return bad
    syms = []
    for raw in (request.args.get("coins") or "").split(","):
        c = _coin(raw)
        if c and c not in syms:
            syms.append(c)
        if len(syms) >= MAX_PRICE_SYMBOLS:
            break
    if not syms:
        return jsonify({"ok": False, "error": "No known coins in the request."}), 400
    return jsonify({"ok": True, "prices": all_prices(syms), "server_time": time.time()})


@premium_bp.route("/api/premium/bonds", methods=["GET"])
def api_bonds():
    bad = _login()
    if bad:
        return bad
    try:
        data = bonds.screener()
    except Exception as e:
        return _fail("bonds", e)
    status = 200 if data.get("ok") else 502
    return jsonify(data), status


@premium_bp.route("/api/premium/bonds/history", methods=["GET"])
def api_bond_history():
    bad = _login()
    if bad:
        return bad
    data = bonds.history(request.args.get("id"))
    if not data:
        return jsonify({"ok": False, "error": "Unknown bond."}), 404
    return jsonify(data)


def init_premium(app=None, *, exchange, get_candles, clean_coin, is_forex, tickers, forex_price=None):
    _hooks.update(exchange=exchange, get_candles=get_candles, clean_coin=clean_coin, is_forex=is_forex,
                  tickers=tickers, forex_price=forex_price)
