"""
Signal Track Record - Signals FM
================================

Measures how the Signals FM signal (signal_engine.py, v2) actually performs:

  1. LIVE record. At every 4-hour candle close the engine reads the signal
     for a fixed list of coins - the same coins every time, so nothing is
     cherry-picked - and records every LONG / SHORT. Each record is then
     followed candle by candle until its target, stop or time limit.

  2. BACKTEST over the last 365 days, re-run every 7 days and whenever the
     engine version changes. Every candle only uses data that had closed
     by then (no look-ahead).

Rules (same for both, shown on the page):
  * Entry   = close of the signal candle.
  * Stop    = 3 x ATR(14) from entry, Target = 1.5 x ATR(14) (the levels
              the dashboard shows).
  * One open signal per coin; new signals are ignored until it closes.
  * If one candle touches both stop and target, it counts as a LOSS.
  * If a candle opens beyond the stop, the exit is that (worse) open.
  * After 48 candles (8 days) without stop/target the signal closes at that
    candle's close ("expired").
  * Results are in R (1R = distance from entry to stop) AFTER estimated
    fees and slippage of 0.12% of the position (round trip).

ENVIRONMENT
  TRACK_RECORD_PUBLIC   "on" -> /track-record is visible to everyone.
                        Default: only logged-in users (review it first).
  TRACK_RECORD_ENGINE   "off" disables recording/backtesting.
"""

import json
import math
import os
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, redirect, render_template, session, url_for

from db import get_db_connection
import signal_engine as se

track_bp = Blueprint("trackrecord", __name__)

TRACKED_COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT", "DOGE/USDT", "ADA/USDT", "LINK/USDT"]
TIMEFRAMES = {"4h": 14400}  # the v2 engine works on 4-hour candles
MAX_BARS = 48                # time limit per signal, in candles
REWARD_RISK = 0.5            # target 1.5 x ATR / stop 3 x ATR
BACKTEST_DAYS = {"4h": 365}
BACKTEST_REFRESH_DAYS = 7
MIN_RISK = 1e-9
CONF_BUCKETS = ((60, 70), (70, 75), (75, 80), (80, 101))
ENGINE_LOCK_NAME = "signalx_trackrecord_engine"
EVAL_EVERY_SEC = 300

_hooks = {"get_candles": None, "engine": None, "available_coins": None}


# ===========================================================================
# helpers
# ===========================================================================
def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def _to_dt(v):
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", ""))
    except ValueError:
        return None


def _iso(v):
    d = _to_dt(v)
    return d.isoformat() + "Z" if d else None


def _f(v, default=None):
    try:
        if v is None:
            return default
        x = float(v)
        return default if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return default


def is_public():
    return (os.environ.get("TRACK_RECORD_PUBLIC", "off") or "off").lower() in ("1", "on", "true", "yes")


class _Cursor:
    def __enter__(self):
        self.conn = get_db_connection()
        self.cur = self.conn.cursor()
        return self.cur

    def __exit__(self, *a):
        try:
            self.cur.close()
        finally:
            self.conn.close()
        return False


def tracked_coins():
    avail = _hooks.get("available_coins")
    return [c for c in TRACKED_COINS if not avail or c in avail]


# ===========================================================================
# DB
# ===========================================================================
def init_tables():
    with _Cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS track_signals (
                id INT PRIMARY KEY AUTO_INCREMENT,
                source VARCHAR(10) NOT NULL,
                symbol VARCHAR(30) NOT NULL,
                timeframe VARCHAR(5) NOT NULL,
                side VARCHAR(5) NOT NULL,
                confidence DOUBLE NOT NULL,
                bullish_pct DOUBLE NULL,
                entry_price DOUBLE NOT NULL,
                stop_loss DOUBLE NOT NULL,
                take_profit DOUBLE NOT NULL,
                risk_pct DOUBLE NOT NULL,
                bar_time DATETIME NOT NULL,
                signal_at DATETIME NOT NULL,
                status VARCHAR(10) NOT NULL,
                exit_price DOUBLE NULL,
                exit_at DATETIME NULL,
                r_multiple DOUBLE NULL,
                pnl_pct DOUBLE NULL,
                bars_held INT NULL,
                mfe_r DOUBLE NULL,
                mae_r DOUBLE NULL,
                created_at DATETIME NOT NULL,
                UNIQUE (source, symbol, timeframe, bar_time),
                INDEX idx_ts_src_status (source, status)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS track_meta (
                k VARCHAR(80) PRIMARY KEY,
                v TEXT NULL
            )
            """
        )
    print("[trackrecord] tables ready")


def meta_get(key, default=None, cur=None):
    def q(c):
        c.execute("SELECT v FROM track_meta WHERE k=%s", (key,))
        row = c.fetchone()
        if not row or row.get("v") is None:
            return default
        try:
            return json.loads(row["v"])
        except (TypeError, ValueError):
            return default
    if cur is not None:
        return q(cur)
    with _Cursor() as c:
        return q(c)


def meta_set(key, value, cur=None):
    def q(c):
        c.execute("SELECT k FROM track_meta WHERE k=%s", (key,))
        if c.fetchone():
            c.execute("UPDATE track_meta SET v=%s WHERE k=%s", (json.dumps(value), key))
        else:
            c.execute("INSERT INTO track_meta (k, v) VALUES (%s,%s)", (key, json.dumps(value)))
    if cur is not None:
        return q(cur)
    with _Cursor() as c:
        return q(c)


def _count_verdict(counts, source, timeframe, verdict):
    key = f"{source}:{timeframe}"
    d = counts.setdefault(key, {"LONG": 0, "SHORT": 0, "WAIT": 0})
    d[verdict] = d.get(verdict, 0) + 1


def _add_counts(source, delta):
    """delta: {"LIVE:1h": {"LONG": n, ...}} -> merged into meta 'verdicts'."""
    with _Cursor() as cur:
        total = meta_get("verdicts", {}, cur) or {}
        for key, d in delta.items():
            t = total.setdefault(key, {"LONG": 0, "SHORT": 0, "WAIT": 0})
            for k, v in d.items():
                t[k] = t.get(k, 0) + v
        meta_set("verdicts", total, cur)


# ===========================================================================
# Pure logic
# ===========================================================================
def net_r(r, entry, sl):
    """Gross R minus round-trip fees/slippage (in R)."""
    return float(r) - se.cost_r(float(entry), float(sl))


def evaluate(side, entry, sl, tp, bars, max_bars=MAX_BARS):
    """Follows a signal through the candles after it.

    bars: list of dicts {time, open, high, low, close}, oldest first, only
    candles AFTER the signal candle and already closed.
    Returns dict(resolved, status, exit_price, exit_time, bars_held, r, pnl_pct,
    mfe_r, mae_r)."""
    risk = abs(entry - sl)
    if risk < MIN_RISK * max(1.0, entry):
        risk = max(entry * 1e-6, MIN_RISK)
    long_ = side == "LONG"
    mfe = mae = 0.0

    def r_of(price):
        return ((price - entry) if long_ else (entry - price)) / risk

    def done(status, price, i, t):
        r = r_of(price)
        return {"resolved": True, "status": status, "exit_price": price, "exit_time": t, "bars_held": i,
                "r": r, "pnl_pct": r * risk / entry * 100, "mfe_r": max(mfe, r if r > 0 else 0),
                "mae_r": min(mae, r if r < 0 else 0)}

    for i, b in enumerate(bars[:max_bars], start=1):
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        if long_:
            if o <= sl:
                return done("SL", o, i, b["time"])
            hit_sl, hit_tp = l <= sl, h >= tp
        else:
            if o >= sl:
                return done("SL", o, i, b["time"])
            hit_sl, hit_tp = h >= sl, l <= tp
        if hit_sl:  # both touched in one candle -> counted as a loss
            return done("SL", sl, i, b["time"])
        if hit_tp:
            return done("TP", tp, i, b["time"])
        best = r_of(h if long_ else l)
        worst = r_of(l if long_ else h)
        mfe, mae = max(mfe, best), min(mae, worst)
        if i == max_bars:
            return done("EXPIRED", c, i, b["time"])
    return {"resolved": False, "bars_held": min(len(bars), max_bars), "mfe_r": mfe, "mae_r": mae}


def conf_bucket(conf):
    for lo, hi in CONF_BUCKETS:
        if lo <= conf < hi:
            return f"{lo}–{min(hi, 100)}%" if hi <= 100 else f"{lo}%+"
    return f"<{CONF_BUCKETS[0][0]}%"


def compute_stats(rows):
    """rows: closed signals (dicts with r_multiple, symbol, timeframe, side,
    confidence, exit_at, bars_held, status). Oldest exit first."""
    rows = sorted(rows, key=lambda r: (_to_dt(r["exit_at"]) or datetime.min, r.get("id") or 0))
    rs = [float(r["r_multiple"]) for r in rows]
    n = len(rs)
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    gp, gl = sum(wins), -sum(losses)

    def group(key_fn, order=None):
        g = {}
        for r in rows:
            k = key_fn(r)
            d = g.setdefault(k, {"key": k, "count": 0, "wins": 0, "total_r": 0.0})
            d["count"] += 1
            d["wins"] += 1 if float(r["r_multiple"]) > 0 else 0
            d["total_r"] += float(r["r_multiple"])
        out = list(g.values())
        for d in out:
            d["win_rate"] = d["wins"] / d["count"] * 100
            d["avg_r"] = d["total_r"] / d["count"]
        if order:
            out.sort(key=lambda d: order.index(d["key"]) if d["key"] in order else 99)
        else:
            out.sort(key=lambda d: -d["count"])
        return out

    equity, peak, max_dd, curve = 10000.0, 10000.0, 0.0, []
    for r in rows:
        equity *= 1 + 0.01 * float(r["r_multiple"])
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
        curve.append({"t": _iso(r["exit_at"]), "equity": round(equity, 2)})
    if len(curve) > 500:
        stride = math.ceil(len(curve) / 500)
        curve = curve[::stride] + ([curve[-1]] if (len(curve) - 1) % stride else [])

    best_w = best_l = cw = cl = 0
    for x in rs:
        if x > 0:
            cw, cl = cw + 1, 0
        else:
            cl, cw = cl + 1, 0
        best_w, best_l = max(best_w, cw), max(best_l, cl)

    statuses = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    avg_win = gp / len(wins) if wins else None
    avg_loss = -gl / len(losses) if losses else None
    bucket_order = [conf_bucket(lo) for lo, _hi in CONF_BUCKETS]
    return {
        "signals": n, "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / n * 100 if n else None,
        "total_r": sum(rs), "avg_r": sum(rs) / n if n else None,
        "profit_factor": gp / gl if gl > 0 else None,
        "avg_win_r": avg_win, "avg_loss_r": avg_loss,
        "breakeven_win_rate": (100 / (1 + avg_win / -avg_loss)) if avg_win and avg_loss else None,
        "equity_1pct": equity, "return_1pct": (equity / 10000 - 1) * 100, "max_drawdown_pct": max_dd,
        "avg_bars": sum(int(r.get("bars_held") or 0) for r in rows) / n if n else None,
        "best_win_streak": best_w, "worst_loss_streak": best_l, "by_status": statuses,
        "by_coin": group(lambda r: r["symbol"]),
        "by_timeframe": group(lambda r: r["timeframe"], ["4h"]),
        "by_side": group(lambda r: r["side"], ["LONG", "SHORT"]),
        "by_confidence": group(lambda r: conf_bucket(float(r["confidence"])), bucket_order),
        "curve": curve,
        "first_signal_at": _iso(min((_to_dt(r.get("signal_at")) for r in rows if r.get("signal_at")), default=None)),
        "last_exit_at": _iso(rows[-1]["exit_at"]) if rows else None,
    }


# ===========================================================================
# Candles + signals
# ===========================================================================
def _df_to_bars(df):
    out = []
    for row in df.itertuples():
        ts = row.timestamp
        t = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        out.append({"time": t.replace(tzinfo=None, microsecond=0), "open": float(row.open), "high": float(row.high),
                    "low": float(row.low), "close": float(row.close)})
    return out


def _closed_only(df, tf_sec, now=None):
    """Drops the still-forming last candle."""
    if df is None or not len(df):
        return df
    now = now or _utcnow()
    last = df["timestamp"].iloc[-1]
    last = last.to_pydatetime() if hasattr(last, "to_pydatetime") else last
    if last.replace(tzinfo=None) + timedelta(seconds=tf_sec) > now:
        return df.iloc[:-1]
    return df


def fetch_history(symbol, timeframe, start):
    """Paginated candles from `start` (naive UTC datetime) up to now, oldest first."""
    import pandas as pd  # local import keeps the module light for tests
    get_candles = _hooks["get_candles"]
    tf_ms = TIMEFRAMES[timeframe] * 1000
    epoch = datetime(1970, 1, 1)
    cursor_ms = int((start - epoch).total_seconds() * 1000)
    now_ms = int((_utcnow() - epoch).total_seconds() * 1000)
    frames, seen_last = [], None
    for _ in range(80):
        df = get_candles(symbol=symbol, timeframe=timeframe, limit=300, since=cursor_ms)
        if df is None or not len(df):
            break
        frames.append(df)
        last_ms = int(pd.Timestamp(df["timestamp"].iloc[-1]).value // 1_000_000)
        if seen_last is not None and last_ms <= seen_last:
            break
        seen_last = last_ms
        cursor_ms = last_ms + tf_ms
        if cursor_ms > now_ms:
            break
        time.sleep(0.15)
    if not frames:
        return None
    return pd.concat(frames).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def _insert_signal(cur, source, symbol, timeframe, sig, bar_time, tf_sec, outcome=None):
    """sig: {verdict, confidence, p_long, entry, stop_loss, take_profit}."""
    side = sig["verdict"]
    entry, sl, tp = float(sig["entry"]), float(sig["stop_loss"]), float(sig["take_profit"])
    risk_pct = abs(entry - sl) / entry * 100
    signal_at = bar_time + timedelta(seconds=tf_sec)
    vals = {"status": "OPEN", "exit_price": None, "exit_at": None, "r": None, "pnl": None, "bars": 0, "mfe": None, "mae": None}
    if outcome and outcome.get("resolved"):
        r = net_r(outcome["r"], entry, sl)
        vals.update({
            "status": outcome["status"], "exit_price": outcome["exit_price"],
            "exit_at": outcome["exit_time"] + timedelta(seconds=tf_sec), "r": r, "pnl": r * risk_pct,
            "bars": outcome["bars_held"], "mfe": outcome["mfe_r"], "mae": outcome["mae_r"],
        })
    try:
        cur.execute(
            """INSERT INTO track_signals (source, symbol, timeframe, side, confidence, bullish_pct, entry_price,
               stop_loss, take_profit, risk_pct, bar_time, signal_at, status, exit_price, exit_at, r_multiple, pnl_pct,
               bars_held, mfe_r, mae_r, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (source, symbol, timeframe, side, sig["confidence"], sig.get("p_long"), entry, sl, tp, risk_pct,
             bar_time, signal_at, vals["status"], vals["exit_price"], vals["exit_at"], vals["r"], vals["pnl"],
             vals["bars"], vals["mfe"], vals["mae"], _utcnow()),
        )
        return True
    except Exception:
        return False  # duplicate (already recorded)


# ===========================================================================
# Backtest (whole universe at once: the model needs the market context)
# ===========================================================================
def backtest_frames(frames, symbols, start_time, counts=None):
    """frames: {symbol: 4h candle DataFrame (closed only)} incl. the 16-coin
    universe. Returns {symbol: [(sig, bar_time, outcome), ...]} for trades
    whose signal candle is at/after start_time. Pure (no DB)."""
    feats = {sym: se.coin_features(df) for sym, df in frames.items() if df is not None and len(df) >= 60}
    if "BTC/USDT" not in feats:
        raise RuntimeError("no Bitcoin history")
    mkt = se.market_block({s: feats.get(s) for s in se.UNIVERSE})
    btc = se.btc_block(feats["BTC/USDT"])
    out = {}
    for sym in symbols:
        F = feats.get(sym)
        if F is None:
            continue
        X = se.assemble(F, btc, mkt, sym=sym)
        pl, ps, cl, cs = se.score(X)
        bars = _df_to_bars(frames[sym])
        times = [b["time"] for b in bars]
        i = next((k for k, t in enumerate(times) if t >= start_time), len(times))
        trades = []
        while i < len(bars) - 1:
            side, _best = se.decide(pl[i], ps[i])
            if counts is not None:
                _count_verdict(counts, "BACKTEST", se.TIMEFRAME, side)
            if side == "WAIT":
                i += 1
                continue
            entry, atr = float(F["close"].iloc[i]), float(F["atr"].iloc[i])
            sl, tp = se.levels(side, entry, atr)
            outcome = evaluate(side, entry, sl, tp, bars[i + 1:])
            if not outcome["resolved"]:
                break  # still running at the end of history - the live record takes over
            sig = {"verdict": side, "confidence": round(float(max(cl[i], cs[i])) * 100, 1),
                   "p_long": round(float(cl[i]) * 100, 1), "entry": entry, "stop_loss": sl, "take_profit": tp}
            trades.append((sig, bars[i]["time"], outcome))
            i += outcome["bars_held"]  # next signal can come at the close of the exit candle
        out[sym] = trades
    return out


def _backtest_due(status, now):
    if status.get("version") != se.ENGINE_VERSION:
        return True
    fin = _to_dt(status.get("finished_at"))
    if status.get("state") == "done":
        return fin is None or now - fin >= timedelta(days=BACKTEST_REFRESH_DAYS)
    if status.get("state") == "error":       # e.g. exchange down: retry hourly, not every loop
        return fin is None or now - fin >= timedelta(hours=1)
    return True


def run_backtests(now=None, force=False):
    now = now or _utcnow()
    status = meta_get("bt_status", {}) or {}
    if not force and not _backtest_due(status, now):
        return
    tf = se.TIMEFRAME
    tf_sec = TIMEFRAMES[tf]
    start_time = now - timedelta(days=BACKTEST_DAYS[tf])
    fetch_from = start_time - timedelta(seconds=tf_sec * (se.HISTORY_BARS + 5))
    status = {"state": "running", "version": se.ENGINE_VERSION, "started_at": _iso(now), "errors": {}}
    meta_set("bt_status", status)
    frames = {}
    for sym in sorted(set(se.UNIVERSE) | set(tracked_coins())):
        try:
            df = fetch_history(sym, tf, fetch_from)
            frames[sym] = _closed_only(df, tf_sec, now) if df is not None else None
        except Exception as e:
            status["errors"][sym] = str(e)[:160]
        time.sleep(0)
    counts = {}
    try:
        result = backtest_frames(frames, tracked_coins(), start_time, counts)
    except Exception as e:
        status.update({"state": "error", "error": str(e)[:200], "finished_at": _iso(now)})
        meta_set("bt_status", status)
        return
    recorded = 0
    with _Cursor() as cur:
        cur.execute("DELETE FROM track_signals WHERE source='BACKTEST'")
        for sym, trades in result.items():
            for sig, bar_time, outcome in trades:
                if _insert_signal(cur, "BACKTEST", sym, tf, sig, bar_time, tf_sec, outcome):
                    recorded += 1
        total = meta_get("verdicts", {}, cur) or {}
        total.pop(f"BACKTEST:{tf}", None)
        total.update(counts)
        meta_set("verdicts", total, cur)
    print(f"[trackrecord] backtest v{se.ENGINE_VERSION}: {recorded} signals")
    missing = [s for s in tracked_coins() if s not in result]
    status.update({"state": "done" if not missing else "partial", "recorded": recorded, "missing": missing,
                   "finished_at": _iso(now), "total": len(tracked_coins()), "done": [[s, tf] for s in result]})
    if missing:
        status["state"] = "done"  # retried with the weekly refresh; errors are listed
    meta_set("bt_status", status)
    _cache.clear()


def ensure_engine_version():
    """A new engine version starts a fresh record (old results measured a different signal)."""
    if meta_get("engine_version") == se.ENGINE_VERSION:
        return False
    with _Cursor() as cur:
        cur.execute("DELETE FROM track_signals")
        cur.execute("DELETE FROM track_meta")
    meta_set("engine_version", se.ENGINE_VERSION)
    _cache.clear()
    print(f"[trackrecord] engine v{se.ENGINE_VERSION}: track record reset")
    return True


# ===========================================================================
# Live record
# ===========================================================================
def live_scan(timeframe, now=None):
    tf_sec = TIMEFRAMES[timeframe]
    now = now or _utcnow()
    counts = {}
    engine = _hooks.get("engine")
    if engine is None:
        return
    with _Cursor() as cur:
        cur.execute("SELECT symbol FROM track_signals WHERE source='LIVE' AND status='OPEN' AND timeframe=%s", (timeframe,))
        open_syms = {r["symbol"] for r in cur.fetchall() or []}
    for symbol in tracked_coins():
        try:
            sig = engine.signal(symbol, now)
        except Exception as e:
            print(f"[trackrecord] live scan {symbol} failed: {e}")
            continue
        _count_verdict(counts, "LIVE", timeframe, sig["verdict"])
        if sig["verdict"] in ("LONG", "SHORT") and symbol not in open_syms:
            with _Cursor() as cur:
                _insert_signal(cur, "LIVE", symbol, timeframe, sig, sig["bar_time"], tf_sec)
    _add_counts("LIVE", counts)
    _cache.clear()


def live_evaluate(now=None):
    now = now or _utcnow()
    with _Cursor() as cur:
        cur.execute("SELECT * FROM track_signals WHERE source='LIVE' AND status='OPEN'")
        rows = cur.fetchall() or []
    for r in rows:
        tf = r["timeframe"]
        tf_sec = TIMEFRAMES.get(tf, 14400)
        try:
            bar_time = _to_dt(r["bar_time"])
            since_ms = int((bar_time - datetime(1970, 1, 1)).total_seconds() * 1000) + tf_sec * 1000
            df = _hooks["get_candles"](symbol=r["symbol"], timeframe=tf, limit=MAX_BARS + 3, since=since_ms)
            df = _closed_only(df, tf_sec, now)
            bars = [b for b in _df_to_bars(df) if b["time"] > bar_time] if df is not None else []
            out = evaluate(r["side"], float(r["entry_price"]), float(r["stop_loss"]), float(r["take_profit"]), bars)
            with _Cursor() as cur:
                if out["resolved"]:
                    cur.execute(
                        """UPDATE track_signals SET status=%s, exit_price=%s, exit_at=%s, r_multiple=%s, pnl_pct=%s,
                           bars_held=%s, mfe_r=%s, mae_r=%s WHERE id=%s AND status='OPEN'""",
                        (out["status"], out["exit_price"], out["exit_time"] + timedelta(seconds=tf_sec),
                         net_r(out["r"], r["entry_price"], r["stop_loss"]),
                         net_r(out["r"], r["entry_price"], r["stop_loss"]) * float(r["risk_pct"]),
                         out["bars_held"], out["mfe_r"], out["mae_r"], r["id"]),
                    )
                else:
                    cur.execute("UPDATE track_signals SET bars_held=%s, mfe_r=%s, mae_r=%s WHERE id=%s AND status='OPEN'",
                                (out["bars_held"], out["mfe_r"], out["mae_r"], r["id"]))
        except Exception as e:
            print(f"[trackrecord] evaluate {r['id']} failed: {e}")
        time.sleep(0.1)
    _cache.clear()


def engine_step(now=None):
    """One pass of the recorder. Returns list of actions done (for tests)."""
    now = now or _utcnow()
    actions = []
    epoch = (now - datetime(1970, 1, 1)).total_seconds()
    for tf, sec in TIMEFRAMES.items():
        slot = int(epoch // sec)
        key = f"live_slot:{tf}"
        last = meta_get(key)
        if last is None:
            meta_set(key, slot)          # start with the next candle close
            if not meta_get("live_since"):
                meta_set("live_since", _iso(now))
            continue
        if slot > int(last) and epoch - slot * sec >= 45:   # give the exchange a moment to publish the candle
            meta_set(key, slot)
            live_scan(tf, now)
            actions.append(f"scan:{tf}")
    if _backtest_due(meta_get("bt_status", {}) or {}, now):
        run_backtests(now)
        actions.append("backtest")
    last_eval = meta_get("last_eval_epoch", 0) or 0
    if epoch - float(last_eval) >= EVAL_EVERY_SEC:
        meta_set("last_eval_epoch", epoch)
        live_evaluate(now)
        actions.append("evaluate")
    meta_set("heartbeat", _iso(now))
    return actions


def _engine_loop(lock_conn):
    _safe(ensure_engine_version)
    _safe(run_backtests)
    while True:
        lock_conn.ping(reconnect=False)
        _safe(engine_step)
        time.sleep(30)


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:
        print(f"[trackrecord] {getattr(fn, '__name__', fn)} failed:\n{traceback.format_exc()}")
        return None


def _supervisor():
    time.sleep(20)  # let the app finish starting first
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
                time.sleep(60)
                continue
            print(f"[trackrecord] engine started on {socket.gethostname()}")
            _engine_loop(lock_conn)
        except Exception:
            print(f"[trackrecord] engine error, restarting in 30s:\n{traceback.format_exc()}")
        finally:
            if lock_conn is not None:
                try:
                    lock_conn.close()
                except Exception:
                    pass
        time.sleep(30)


_started = False
_start_lock = threading.Lock()


def init_trackrecord(app=None, *, get_candles=None, engine=None, available_coins=None, start=True, signal_core=None):
    global _started
    if get_candles:
        _hooks["get_candles"] = get_candles
    if engine is not None:
        _hooks["engine"] = engine
    elif get_candles and _hooks.get("engine") is None:
        _hooks["engine"] = se.LiveEngine(get_candles)
    if available_coins:
        _hooks["available_coins"] = list(available_coins)
    try:
        init_tables()
    except Exception as e:
        print(f"[trackrecord] WARNING: could not create tables: {e}")
    if not start or (os.environ.get("TRACK_RECORD_ENGINE", "on") or "on").lower() in ("0", "off", "false", "no"):
        return
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_supervisor, name="trackrecord-engine", daemon=True).start()


# ===========================================================================
# API + page
# ===========================================================================
_cache = {}


def _signal_json(r):
    return {
        "id": r["id"], "source": r["source"], "symbol": r["symbol"], "timeframe": r["timeframe"], "side": r["side"],
        "confidence": _f(r["confidence"]), "entry_price": _f(r["entry_price"]), "stop_loss": _f(r["stop_loss"]),
        "take_profit": _f(r["take_profit"]), "risk_pct": _f(r["risk_pct"]), "signal_at": _iso(r["signal_at"]),
        "status": r["status"], "exit_price": _f(r["exit_price"]), "exit_at": _iso(r["exit_at"]),
        "r_multiple": _f(r["r_multiple"]), "pnl_pct": _f(r["pnl_pct"]), "bars_held": r.get("bars_held"),
        "mfe_r": _f(r.get("mfe_r")), "mae_r": _f(r.get("mae_r")),
    }


def build_summary():
    hit = _cache.get("summary")
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    with _Cursor() as cur:
        out = {"sources": {}}
        for src in ("LIVE", "BACKTEST"):
            cur.execute("SELECT * FROM track_signals WHERE source=%s AND status<>'OPEN'", (src,))
            closed = cur.fetchall() or []
            stats = compute_stats(closed)
            cur.execute(f"""SELECT * FROM track_signals WHERE source=%s ORDER BY signal_at DESC, id DESC LIMIT 60""", (src,))
            recent = [_signal_json(r) for r in cur.fetchall() or []]
            cur.execute("SELECT * FROM track_signals WHERE source=%s AND status='OPEN' ORDER BY signal_at DESC", (src,))
            open_rows = [_signal_json(r) for r in cur.fetchall() or []]
            ordered = sorted(closed, key=lambda r: (_to_dt(r["exit_at"]) or datetime.min, r["id"]))[-800:]
            tape = [{"symbol": r["symbol"], "timeframe": r["timeframe"], "side": r["side"], "status": r["status"],
                     "r_multiple": round(float(r["r_multiple"]), 3), "signal_at": _iso(r["signal_at"])} for r in ordered]
            out["sources"][src] = {"stats": stats, "recent": recent, "open": open_rows, "tape": tape}
        verdicts = meta_get("verdicts", {}, cur) or {}
        out["verdicts"] = verdicts
        out["backtest_status"] = meta_get("bt_status", {}, cur) or {}
        out["live_since"] = meta_get("live_since", None, cur)
        out["heartbeat"] = meta_get("heartbeat", None, cur)
    out["rules"] = {
        "coins": tracked_coins(), "timeframes": list(TIMEFRAMES), "max_bars": MAX_BARS,
        "reward_risk": REWARD_RISK, "backtest_days": BACKTEST_DAYS, "engine_version": se.ENGINE_VERSION,
        "sl_atr": se.model().sl_atr, "tp_atr": se.model().tp_atr, "fees_pct": se.COST_ROUND_TRIP * 100,
        "model_test": se.test_stats(),
    }
    out["public"] = is_public()
    out["generated_at"] = _iso(_utcnow())
    _cache["summary"] = (time.time(), out)
    return out


def _allowed():
    return is_public() or bool(session.get("user_id"))


@track_bp.route("/track-record", methods=["GET"])
def track_record_page():
    if not _allowed():
        return redirect(url_for("login_page"))
    return render_template("track-record.html", public=is_public(), logged_in=bool(session.get("user_id")))


@track_bp.route("/api/track-record", methods=["GET"])
def track_record_api():
    if not _allowed():
        return jsonify({"ok": False, "error": "Login required."}), 401
    try:
        return jsonify({"ok": True, **build_summary()})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Track record unavailable: {str(e)[:120]}"}), 500
