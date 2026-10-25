"""
Signal hub - the 15m / 1h / 4h signal streams for crypto and forex.

* Storage: every signal of every stream is one row in `signal_events`
  (market + timeframe + symbol + candle are unique, so a 15m and a 4h signal
  on the same symbol never overwrite each other). The migration only ADDS
  columns; rows from before it are crypto 4h signals (the only stream that
  existed) and get that market and timeframe.
* Scanner: after every candle close of each timeframe it runs the stream's
  engine on every symbol (streams.py; crypto 4h is still produced by
  signalboard.py with signal engine v3.1), stores the new signals and sends the
  alerts (signalboard.notify: bell, email, Telegram; users choose markets and
  timeframes).
* Tracker: follows every ACTIVE signal on closed candles with the plan stored
  with it and writes its result (TP1/TP2/TP3 hit, SL hit, expired) - the live
  statistics are computed only from these real, closed signals.
* API (login required): /api/signals, /api/signals/stats, /api/signals/latest,
  /api/signals/streams, /api/signals/<id>, POST /api/signals/<id>/cancel (admin).

Env: SIGNAL_STREAMS (default / 'all': all six streams; 'validated': only the streams whose model passed its test on
unseen data; or a comma-separated list). Streams that have not passed the test are marked 'untested' everywhere.
SIGNAL_HUB=off to stop the background job.
"""
import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, redirect, render_template, request, session

import markets
import streams
from db import get_db_connection

hub_bp = Blueprint("signal_hub", __name__)

ALL_STREAMS = ["crypto_15m", "crypto_1h", "crypto_4h", "forex_15m", "forex_1h", "forex_4h"]
DELAY_SEC = {"crypto": 45, "forex": 90}
LOOP_SEC = 20
CLOSED = ("TP1", "TP2", "TP3", "SL", "EXPIRED")
STATUS_FILTERS = {"active": ("ACTIVE",), "closed": CLOSED, "tp": ("TP1", "TP2", "TP3"), "sl": ("SL",),
                  "expired": ("EXPIRED",), "cancelled": ("CANCELLED",)}
SORTS = {"newest": "bar_time DESC, id DESC", "oldest": "bar_time ASC, id ASC",
         "grade": "grade ASC, bar_time DESC", "result": "r_multiple DESC, bar_time DESC",
         "symbol": "symbol ASC, bar_time DESC", "confidence": "confidence DESC, bar_time DESC"}

_hooks = {"get_candles": None, "notify": None, "crypto_prices": None, "forex_price": None, "blocked": None,
          "news_for": None, "is_admin": None}
_state = {"scanned": {}, "tracked": {}, "errors": {}, "last_scan": {}}
_engines = {}
_store = None
_lock = threading.Lock()


def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def _iso(d):
    return d.replace(microsecond=0).isoformat() + "Z" if isinstance(d, datetime) else d


def _parse(v):
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "").replace("T", " ")[:19])
    except ValueError:
        return None


class _Cursor:
    def __enter__(self):
        self.conn = get_db_connection()
        self.cur = self.conn.cursor()
        return self.cur

    def __exit__(self, *a):
        try:
            if a[0] is None:
                try:
                    self.conn.commit()
                except Exception:
                    pass
            self.cur.close()
        finally:
            self.conn.close()
        return False


def validated_streams():
    return [k for k in ALL_STREAMS if streams.validated(k)]


def stream_tested(market, tf):
    """True when the stream passed its test on data it never saw (crypto 4h: signal engine v3.1)."""
    try:
        return bool(streams.validated(f"{market}_{tf}"))
    except Exception:
        return False


def enabled_streams():
    """SIGNAL_STREAMS: empty or 'all' (default) = all six streams (the ones that have not passed the test on unseen
    data run simple rules and are marked 'untested' on the site, in alerts and for live auto-trading);
    'validated' = only the streams whose model passed that test; or a comma-separated list."""
    raw = (os.environ.get("SIGNAL_STREAMS") or "").strip().lower()
    if not raw or raw == "all":
        return list(ALL_STREAMS)
    if raw == "validated":
        return validated_streams()
    out = [s.strip() for s in raw.split(",") if s.strip() in ALL_STREAMS]
    return out or list(ALL_STREAMS)


def symbols_for(market):
    if market == "forex":
        return markets.forex_signal_pairs()
    return list(markets.CRYPTO_COINS)


# ===========================================================================
# DB + safe migration
# ===========================================================================
NEW_COLUMNS = [
    ("market", "VARCHAR(10) NOT NULL DEFAULT 'crypto'"),
    ("timeframe", "VARCHAR(5) NOT NULL DEFAULT '4h'"),
    ("grade", "CHAR(1) NULL"),
    ("setup", "VARCHAR(24) NULL"),
    ("tier", "VARCHAR(10) NULL"),
    ("targets", "TEXT NULL"),
    ("plan", "TEXT NULL"),
    ("status", "VARCHAR(12) NOT NULL DEFAULT 'ACTIVE'"),
    ("hits", "INT NOT NULL DEFAULT 0"),
    ("stop_now", "DOUBLE NULL"),
    ("exit_price", "DOUBLE NULL"),
    ("r_multiple", "DOUBLE NULL"),
    ("pips", "DOUBLE NULL"),
    ("result_pct", "DOUBLE NULL"),
    ("closed_at", "DATETIME NULL"),
    ("expires_at", "DATETIME NULL"),
    ("hold_minutes", "INT NULL"),
    ("engine", "VARCHAR(10) NULL"),
    ("news", "TEXT NULL"),
    ("cancel_reason", "VARCHAR(200) NULL"),
    ("updated_at", "DATETIME NULL"),
]


def _is_sqlite(cur):
    """True for the SQLite stand-in the tests use (MySQL has no sqlite_version())."""
    if getattr(cur, "is_sqlite", False):
        return True
    try:
        cur.execute("SELECT sqlite_version() AS v")
        cur.fetchall()
        return True
    except Exception:
        return False


def _columns(cur, table):
    if not _is_sqlite(cur):
        try:
            cur.execute(f"SHOW COLUMNS FROM {table}")
            return {r["Field"] for r in cur.fetchall()}
        except Exception:
            pass
    cur.execute(f"PRAGMA table_info({table})")
    return {r["name"] if isinstance(r, dict) else r[1] for r in cur.fetchall()}


def init_tables():
    with _Cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_events (
                id INT PRIMARY KEY AUTO_INCREMENT,
                symbol VARCHAR(30) NOT NULL,
                side VARCHAR(5) NOT NULL,
                bar_time DATETIME NOT NULL,
                entry DOUBLE NOT NULL,
                stop_loss DOUBLE NOT NULL,
                take_profit DOUBLE NOT NULL,
                confidence DOUBLE NULL,
                created_at DATETIME NOT NULL,
                market VARCHAR(10) NOT NULL DEFAULT 'crypto',
                timeframe VARCHAR(5) NOT NULL DEFAULT '4h',
                UNIQUE (symbol, timeframe, bar_time)
            )
            """
        )
        have = _columns(cur, "signal_events")
        for name, ddl in NEW_COLUMNS:
            if name not in have:
                cur.execute(f"ALTER TABLE signal_events ADD COLUMN {name} {ddl}")
        if _is_sqlite(cur):
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_stream_bar ON signal_events (symbol, timeframe, bar_time)")
        else:
            # the old unique key (symbol, bar_time) would block a 1h and a 4h signal on the same candle start
            cur.execute("SHOW INDEX FROM signal_events")
            idx = {}
            for r in cur.fetchall():
                if int(r["Non_unique"]) == 0 and r["Key_name"] != "PRIMARY":
                    idx.setdefault(r["Key_name"], []).append((int(r["Seq_in_index"]), r["Column_name"]))
            cols = {k: [c for _, c in sorted(v)] for k, v in idx.items()}
            if not any(v == ["symbol", "timeframe", "bar_time"] for v in cols.values()):
                cur.execute("ALTER TABLE signal_events ADD UNIQUE KEY uq_stream_bar (symbol, timeframe, bar_time)")
            for k, v in cols.items():
                if v == ["symbol", "bar_time"]:
                    cur.execute(f"ALTER TABLE signal_events DROP INDEX `{k}`")
            cur.execute("SHOW INDEX FROM signal_events WHERE Key_name = 'idx_stream_status'")
            if not cur.fetchall():
                cur.execute("ALTER TABLE signal_events ADD INDEX idx_stream_status (market, timeframe, status, bar_time)")
        have = _columns(cur, "signal_alert_subs") if _table_exists(cur, "signal_alert_subs") else None
        if have is not None:
            for name in ("markets", "timeframes"):
                if name not in have:
                    cur.execute(f"ALTER TABLE signal_alert_subs ADD COLUMN {name} TEXT NULL")


def _table_exists(cur, table):
    try:
        cur.execute(f"SELECT 1 FROM {table} LIMIT 1")
        cur.fetchall()
        return True
    except Exception:
        return False


# ===========================================================================
# recording new signals
# ===========================================================================
def record(symbol, market, tf, a, news=None):
    """Stores one new signal; returns the event dict, or None when it already exists."""
    plan = streams.plan_for(market, tf)
    bar_time = _parse(a.get("bar_time"))
    entry, stop = float(a["entry"]), float(a["stop_loss"])
    tps = [float(x) for x in (a.get("targets") or [])]
    grade = a.get("grade") or (streams.grade_from_confidence(a.get("confidence"), a.get("tier"))
                               if market == "crypto" and tf == "4h" else None)
    expires = _parse(a.get("expires_at")) or (bar_time + timedelta(seconds=plan["tf_sec"] * (plan["max_bars"] + 1)))
    hold = a.get("hold_minutes") or streams.hold_minutes(tf, max(1, plan["max_bars"] // 3))
    now = _utcnow()
    try:
        with _Cursor() as cur:
            cur.execute(
                """INSERT INTO signal_events (symbol, side, bar_time, entry, stop_loss, take_profit, confidence,
                       created_at, market, timeframe, grade, setup, tier, targets, plan, status, hits, stop_now,
                       expires_at, hold_minutes, engine, news, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',0,%s,%s,%s,%s,%s,%s)""",
                (symbol, a["side"], bar_time, entry, stop, float(a["take_profit"]), a.get("confidence"), now,
                 market, tf, grade, a.get("setup"), a.get("tier"), json.dumps(tps), json.dumps(plan), stop,
                 expires, hold, plan["engine"], json.dumps(news) if news else None, now),
            )
    except Exception as e:
        if "uniq" in str(e).lower() or "duplicate" in str(e).lower() or "constraint" in str(e).lower():
            return None
        raise
    return {"symbol": symbol, "market": market, "timeframe": tf, "side": a["side"], "bar_time": bar_time,
            "entry": entry, "stop_loss": stop, "take_profit": float(a["take_profit"]), "targets": tps,
            "confidence": a.get("confidence"), "grade": grade, "setup": a.get("setup"), "tier": a.get("tier"),
            "expires_at": expires, "hold_minutes": hold, "news": news}


# ===========================================================================
# engines + scanner
# ===========================================================================
def store():
    global _store
    if _store is None:
        if _hooks.get("get_candles") is None:
            raise RuntimeError("candles are not configured")
        _store = streams.CandleStore(_hooks["get_candles"])
    return _store


def engine(key):
    with _lock:
        e = _engines.get(key)
        if e is None:
            cfg = streams.STREAMS[key]
            e = streams.StreamEngine(cfg["market"], cfg["tf"], store(), markets.stream_coins(cfg["market"], cfg["tf"]),
                                     blocked=_blocked)
            _engines[key] = e
        return e


def _blocked(symbol, at):
    fn = _hooks.get("blocked")
    if not fn:
        return None
    try:
        return fn(symbol, at)
    except Exception:
        return None


def _news_for(symbol, at):
    fn = _hooks.get("news_for")
    if not fn:
        return None
    try:
        return fn(symbol, at)
    except Exception:
        return None


def scan_stream(key, now=None):
    """Runs one stream at its last closed candle; stores + returns the new signals."""
    now = now or _utcnow()
    cfg = streams.STREAMS[key]
    board = engine(key).board(now, force=True)
    events = []
    for sym, res in board.items():
        if res.get("error") or not res.get("fresh") or not res.get("active"):
            continue
        a = res["active"]
        ev = record(sym, cfg["market"], cfg["tf"], a, news=_news_for(sym, _parse(a.get("signal_at"))))
        if ev:
            events.append(ev)
    _state["last_scan"][key] = {"at": now, "symbols": len(board),
                                "errors": sum(1 for r in board.values() if r.get("error")), "new": len(events)}
    return events


def due_streams(now=None):
    """Streams whose newest candle has closed (plus the publish delay) and was not scanned yet."""
    now = now or _utcnow()
    out = []
    for key in enabled_streams():
        cfg = streams.STREAMS[key]
        if key == "crypto_4h":
            continue                      # crypto 4h: signalboard.py (engine v3.1)
        bar = streams.last_closed_bar(cfg["tf"], now)
        if _state["scanned"].get(key) == bar:
            continue
        if now < bar + timedelta(seconds=markets.TF_SEC[cfg["tf"]] + DELAY_SEC[cfg["market"]]):
            continue
        if cfg["market"] == "forex" and not markets.forex_open(bar):
            _state["scanned"][key] = bar   # weekend: nothing to scan
            continue
        out.append((key, bar))
    return out


# ===========================================================================
# tracker (results of the stored signals)
# ===========================================================================
def _plan_of(row):
    try:
        p = json.loads(row.get("plan") or "")
        if isinstance(p, dict) and p.get("targets_r"):
            return p
    except (TypeError, ValueError):
        pass
    return streams.plan_for(row.get("market") or "crypto", row.get("timeframe") or "4h")


def _targets_of(row, plan):
    try:
        t = json.loads(row.get("targets") or "")
        if isinstance(t, list) and t:
            return [float(x) for x in t]
    except (TypeError, ValueError):
        pass
    entry, stop = float(row["entry"]), float(row["stop_loss"])
    sgn = 1 if row["side"] == "LONG" else -1
    return [entry + sgn * k * abs(entry - stop) for k in plan["targets_r"]]


def _candles_after(symbol, tf, bar_time, need, now):
    """Closed candles from bar_time on (uses the stream cache when it reaches back far enough)."""
    sec = markets.TF_SEC[tf]
    try:
        df = store().frame(symbol, tf, now)
        if len(df) and pd.Timestamp(df["timestamp"].iloc[0]) <= pd.Timestamp(bar_time):
            return df
    except Exception:
        pass
    fn = _hooks.get("get_candles")
    since = int((bar_time - datetime(1970, 1, 1)).total_seconds() * 1000)
    df = fn(symbol=symbol, timeframe=tf, limit=min(1000, need + 5), since=since)
    if df is None or not len(df):
        return None
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df[df["timestamp"] + pd.Timedelta(seconds=sec) <= pd.Timestamp(now)]
    if markets.is_forex(symbol):
        df = df[[markets.forex_open(t.to_pydatetime()) for t in df["timestamp"]]]
    return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def evaluate_row(row, df, now=None):
    """-> dict of column updates for one ACTIVE signal, given closed candles."""
    plan = _plan_of(row)
    tf = row.get("timeframe") or "4h"
    sec = markets.TF_SEC[tf]
    bar_time = _parse(row["bar_time"])
    ts = pd.to_datetime(df["timestamp"])
    after = np.where(ts > pd.Timestamp(bar_time))[0]
    if not len(after):
        return None
    start = int(after[0])
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    mask = streams.weekend_exit_mask(ts, sec, now) if markets.is_forex(row["symbol"]) else None
    entry, sl = float(row["entry"]), float(row["stop_loss"])
    res = streams.follow(row["side"], entry, sl, o, h, l, c, start, plan, exit_mask=mask)
    upd = {"hits": int(res["hits"]), "stop_now": float(res["stop_now"]), "updated_at": now or _utcnow()}
    if res["status"] is None:
        return upd
    r = float(res["r_gross"]) - streams.cost_r(row["symbol"], entry, sl)
    j = min(start + res["bars"] - 1, len(ts) - 1)
    closed_at = pd.Timestamp(ts.iloc[j]).to_pydatetime() + timedelta(seconds=sec)
    units = streams.result_units(row["symbol"], entry, sl, round(r, 3))
    upd.update({"status": res["status"], "exit_price": res["exit_price"], "r_multiple": round(r, 3),
                "pips": units.get("pips"), "result_pct": units.get("pct"), "closed_at": closed_at})
    return upd


def track_open(now=None, limit=300):
    now = now or _utcnow()
    with _Cursor() as cur:
        cur.execute("SELECT * FROM signal_events WHERE status = 'ACTIVE' ORDER BY bar_time ASC LIMIT %s", (limit,))
        rows = cur.fetchall() or []
    done = 0
    for row in rows:
        tf = row.get("timeframe") or "4h"
        if tf not in markets.TF_SEC:
            continue
        plan = _plan_of(row)
        try:
            df = _candles_after(row["symbol"], tf, _parse(row["bar_time"]), int(plan["max_bars"]) + 2, now)
        except Exception as e:
            _state["errors"]["track"] = str(e)[:160]
            continue
        if df is None:
            continue
        upd = evaluate_row(row, df, now)
        if not upd:
            continue
        sets = ", ".join(f"{k}=%s" for k in upd)
        with _Cursor() as cur:
            cur.execute(f"UPDATE signal_events SET {sets} WHERE id=%s AND status='ACTIVE'", (*upd.values(), row["id"]))
        if upd.get("status"):
            done += 1
    return done


def step(now=None):
    """One background pass: scan due streams, notify, track open signals."""
    now = now or _utcnow()
    actions = []
    new = []
    for key, bar in due_streams(now):
        try:
            evs = scan_stream(key, now)
            new.extend(evs)
            actions.append(f"scan:{key}:{len(evs)}")
        except Exception as e:
            _state["errors"][key] = str(e)[:200]
            print(f"[signal_hub] {key} scan failed:\n{traceback.format_exc()}")
        _state["scanned"][key] = bar
    if new and _hooks.get("notify"):
        try:
            _hooks["notify"](new)
        except Exception:
            print(f"[signal_hub] notify failed:\n{traceback.format_exc()}")
    # results: after every 15-minute close (all timeframes share the pass)
    tick = streams.last_closed_bar("15m", now)
    if _state["tracked"].get("bar") != tick and now >= tick + timedelta(seconds=900 + 60):
        try:
            n = track_open(now)
            actions.append(f"track:{n}")
        except Exception:
            print(f"[signal_hub] tracker failed:\n{traceback.format_exc()}")
        _state["tracked"]["bar"] = tick
    return actions


def _loop():
    """One scanner across all workers / instances (MySQL named lock); the others wait and take over if it stops."""
    time.sleep(30)
    while True:
        lock = None
        try:
            lock = get_db_connection()
            with lock.cursor() as c:
                c.execute("SELECT GET_LOCK(%s, 0) AS got", ("signalx_signal_hub",))
                got = (c.fetchone() or {}).get("got")
            if got != 1:
                lock.close()
                lock = None
                time.sleep(60)
                continue
            while True:
                lock.ping(reconnect=False)
                try:
                    step()
                except Exception:
                    print(f"[signal_hub] step failed:\n{traceback.format_exc()}")
                time.sleep(LOOP_SEC)
        except Exception:
            print(f"[signal_hub] scanner error, restarting:\n{traceback.format_exc()}")
        finally:
            if lock is not None:
                try:
                    lock.close()
                except Exception:
                    pass
        time.sleep(30)


_started = False


def init_hub(app=None, *, get_candles=None, notify=None, crypto_prices=None, forex_price=None, blocked=None,
             news_for=None, is_admin=None, start=True):
    global _started
    for k, v in (("get_candles", get_candles), ("notify", notify), ("crypto_prices", crypto_prices),
                 ("forex_price", forex_price), ("blocked", blocked), ("news_for", news_for), ("is_admin", is_admin)):
        if v is not None:
            _hooks[k] = v
    try:
        init_tables()
    except Exception as e:
        print(f"[signal_hub] WARNING: could not migrate tables: {e}")
    if not start or (os.environ.get("SIGNAL_HUB", "on") or "on").lower() in ("0", "off", "false", "no"):
        return
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, name="signal-hub", daemon=True).start()


# ===========================================================================
# queries
# ===========================================================================
def _dec(symbol):
    return markets.price_decimals(symbol) if markets.is_forex(symbol) else None


def signal_json(r, price=None):
    plan = _plan_of(r)
    tps = _targets_of(r, plan)
    entry, stop = float(r["entry"]), float(r["stop_loss"])
    tf = r.get("timeframe") or "4h"
    sec = markets.TF_SEC.get(tf, 14400)
    bar_time = _parse(r["bar_time"])
    sym = r["symbol"]
    risk = abs(entry - stop)
    rr = round(abs(tps[1] - entry) / risk, 2) if risk > 0 and len(tps) > 1 else None
    out = {
        "id": r["id"], "market": r.get("market") or "crypto", "symbol": sym, "timeframe": tf, "side": r["side"],
        "entry": streams.price_round(entry, sym), "stop_loss": streams.price_round(stop, sym),
        "stop_now": streams.price_round(float(r["stop_now"]), sym) if r.get("stop_now") is not None else None,
        "targets": [streams.price_round(x, sym) for x in tps], "targets_r": plan["targets_r"],
        "rr": rr, "rr_final": round(plan["targets_r"][-1], 2), "grade": r.get("grade") or "C",
        "setup": r.get("setup"), "confidence": r.get("confidence"), "tier": r.get("tier"),
        "status": r.get("status") or "ACTIVE", "hits": int(r.get("hits") or 0),
        "signal_at": _iso(bar_time + timedelta(seconds=sec)) if bar_time else None,
        "created_at": _iso(_parse(r.get("created_at"))), "closed_at": _iso(_parse(r.get("closed_at"))),
        "expires_at": _iso(_parse(r.get("expires_at"))), "hold_minutes": r.get("hold_minutes"),
        "exit_price": streams.price_round(float(r["exit_price"]), sym) if r.get("exit_price") is not None else None,
        "r": r.get("r_multiple"), "pips": r.get("pips"), "pct": r.get("result_pct"),
        "engine": r.get("engine") or "v3", "cancel_reason": r.get("cancel_reason"), "decimals": _dec(sym),
        "tested": stream_tested(r.get("market") or "crypto", tf),
    }
    if markets.is_forex(sym):
        out["sl_pips"] = round(markets.to_pips(sym, risk), 1)
        out["tp_pips"] = [round(markets.to_pips(sym, abs(x - entry)), 1) for x in tps]
    try:
        out["news"] = json.loads(r["news"]) if r.get("news") else None
    except (TypeError, ValueError):
        out["news"] = None
    if price is not None and out["status"] == "ACTIVE":
        out["price"] = streams.price_round(float(price), sym)
        sgn = 1 if r["side"] == "LONG" else -1
        out["r_now"] = round(sgn * (float(price) - entry) / risk, 2) if risk > 0 else None
        if markets.is_forex(sym):
            out["pips_now"] = round(markets.to_pips(sym, sgn * (float(price) - entry)), 1)
        else:
            out["pct_now"] = round(sgn * (float(price) / entry - 1) * 100, 2)
    return out


def _where(market=None, tf=None, symbol=None, side=None, status=None, grade=None):
    w, args = [], []
    if market:
        w.append("market=%s")
        args.append(market)
    if tf:
        w.append("timeframe=%s")
        args.append(tf)
    if symbol:
        w.append("symbol=%s")
        args.append(symbol)
    if side:
        w.append("side=%s")
        args.append(side)
    if status:
        sts = STATUS_FILTERS.get(status, (status,))
        w.append("status IN (" + ",".join(["%s"] * len(sts)) + ")")
        args.extend(sts)
    if grade:
        gs = [g for g in grade if g in "ABC"]
        if gs:
            w.append("grade IN (" + ",".join(["%s"] * len(gs)) + ")")
            args.extend(gs)
    return (" WHERE " + " AND ".join(w)) if w else "", args


def list_signals(market=None, tf=None, symbol=None, side=None, status=None, grade=None, sort="newest",
                 page=1, per_page=25):
    where, args = _where(market, tf, symbol, side, status, grade)
    order = SORTS.get(sort, SORTS["newest"])
    with _Cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM signal_events{where}", tuple(args))
        total = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute(f"SELECT * FROM signal_events{where} ORDER BY {order} LIMIT %s OFFSET %s",
                    (*args, per_page, (page - 1) * per_page))
        rows = cur.fetchall() or []
    return rows, total


def live_prices(rows):
    syms = sorted({r["symbol"] for r in rows if (r.get("status") or "ACTIVE") == "ACTIVE"})
    out = {}
    crypto = [s for s in syms if not markets.is_forex(s)]
    if crypto and _hooks.get("crypto_prices"):
        try:
            out.update(_hooks["crypto_prices"](crypto) or {})
        except Exception:
            pass
    fx = _hooks.get("forex_price")
    for s in syms:
        if markets.is_forex(s) and fx:
            try:
                p = fx(s)
                if p is not None:
                    out[s] = float(p)
            except Exception:
                pass
    return out


def _stats_rows(rows):
    n = len(rows)
    wins = sum(1 for r in rows if str(r.get("status")).startswith("TP"))
    losses = sum(1 for r in rows if r.get("status") == "SL")
    rs = [float(r["r_multiple"]) for r in rows if r.get("r_multiple") is not None]
    pips = [float(r["pips"]) for r in rows if r.get("pips") is not None]
    pcts = [float(r["result_pct"]) for r in rows if r.get("result_pct") is not None]
    gp, gl = sum(x for x in rs if x > 0), -sum(x for x in rs if x < 0)
    rr = []
    for r in rows:
        p = _plan_of(r)
        if len(p["targets_r"]) > 1:
            rr.append(p["targets_r"][1])
    return {"closed": n, "wins": wins, "losses": losses, "expired": n - wins - losses,
            "win_rate": round(wins / n * 100, 1) if n else None,
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 2),
            "total_pips": round(sum(pips), 1) if pips else None, "total_pct": round(sum(pcts), 2) if pcts else None,
            "profit_factor": round(gp / gl, 2) if gl > 0 else None,
            "avg_rr": round(sum(rr) / len(rr), 2) if rr else None}


def stream_stats(market, tf, now=None):
    """Live statistics from real closed signals only, for all-time / 30 days / 7 days."""
    now = now or _utcnow()
    with _Cursor() as cur:
        cur.execute("SELECT * FROM signal_events WHERE market=%s AND timeframe=%s", (market, tf))
        rows = cur.fetchall() or []
    closed = [r for r in rows if r.get("status") in CLOSED]
    out = {"market": market, "timeframe": tf, "total": len([r for r in rows if r.get("status") != "CANCELLED"]),
           "active": sum(1 for r in rows if r.get("status") == "ACTIVE"),
           "cancelled": sum(1 for r in rows if r.get("status") == "CANCELLED"), "windows": {}, "by_grade": {}}
    for name, days in (("all", None), ("30d", 30), ("7d", 7)):
        sel = closed if days is None else [r for r in closed if _parse(r.get("closed_at")) and
                                          _parse(r["closed_at"]) >= now - timedelta(days=days)]
        out["windows"][name] = _stats_rows(sel)
    for g in "ABC":
        out["by_grade"][g] = _stats_rows([r for r in closed if (r.get("grade") or "C") == g])
    starts = [_parse(r["bar_time"]) for r in rows if r.get("bar_time")]
    if starts:
        days = max(1.0, (now - min(starts)).total_seconds() / 86400)
        out["signals_per_day"] = round(out["total"] / days, 2)
        out["since"] = _iso(min(starts))
    return out


def simulated(market, tf):
    """Replay of the same rules on the candles in memory (recent days), when the
    stream has run at least once. Clearly a simulation, never mixed with the live record."""
    key = streams.stream_key(market, tf)
    if key == "crypto_4h":
        return None
    e = _engines.get(key)
    if not e or not e._board:
        return None
    board = e._board[1]
    lists = [r.get("recent") or [] for r in board.values() if isinstance(r, dict) and not r.get("error")]
    s = streams.summary(lists)
    bars = [r.get("history_bars") or 0 for r in board.values() if isinstance(r, dict) and not r.get("error")]
    s["window_bars"] = streams.REPLAY_BARS
    s["window_days"] = round(streams.REPLAY_BARS * markets.TF_SEC[tf] / 86400, 1)
    s["symbols"] = len(bars)
    return s


def model_test(market, tf):
    """The stream model's published tests (walk-forward + holdout on data it never saw), or None."""
    key = streams.stream_key(market, tf)
    if key == "crypto_4h":
        try:
            import signal_engine as se
            return {"holdout": se.test_stats(), "walk_forward": se.walk_forward_stats(), "engine": "v" + se.ENGINE_VERSION,
                    "extra_coins": coin_test(key)}
        except Exception:
            return None
    m = streams.load_model(key)
    if m is None:
        return None
    lt = m.meta.get("limit_test") or None
    if lt:
        old = (lt.get("holdout") or {}).get(str((lt.get("before") or {}).get("max_per_bar", 4))) or {}
        lt = {"limit": lt.get("choice"), "before_limit": (lt.get("before") or {}).get("max_per_bar", 4),
              "before": {k: old.get(k) for k in ("signals", "signals_per_day", "win_rate", "avg_net_r")} if old else None}
    return {"holdout": m.meta.get("test"), "walk_forward": m.meta.get("walk_forward"), "engine": "v" + m.version,
            "baseline": m.meta.get("baseline_rules"), "limit_test": lt}


def latest_by_stream(per=3):
    out = {}
    with _Cursor() as cur:
        for key in enabled_streams():
            cfg = streams.STREAMS[key]
            cur.execute("""SELECT * FROM signal_events WHERE market=%s AND timeframe=%s AND status <> 'CANCELLED'
                           ORDER BY bar_time DESC, id DESC LIMIT %s""", (cfg["market"], cfg["tf"], per))
            out[key] = cur.fetchall() or []
    return out


def bot_feed(now=None, window_bars=2):
    """Active signals that are still open for entry (started at most `window_bars` candles of their
    timeframe ago, no target reached yet) - what the auto-trade bot picks up."""
    now = now or _utcnow()
    with _Cursor() as cur:
        cur.execute("""SELECT * FROM signal_events WHERE status='ACTIVE' AND hits=0 AND bar_time >= %s
                       ORDER BY bar_time ASC, id ASC LIMIT 300""", (now - timedelta(hours=4 * (window_bars + 1)),))
        rows = cur.fetchall() or []
    out = []
    for r in rows:
        sec = markets.TF_SEC.get(r.get("timeframe") or "4h", 14400)
        start = _parse(r["bar_time"]) + timedelta(seconds=sec)
        if start <= now <= start + timedelta(seconds=sec * window_bars):
            out.append(signal_json(r))
    return out


def signal_by_id(sid):
    r = get_signal(sid)
    return signal_json(r) if r else None


def get_signal(sid):
    with _Cursor() as cur:
        cur.execute("SELECT * FROM signal_events WHERE id=%s", (sid,))
        return cur.fetchone()


def active_streams(limit=40, skip_board=True):
    """Active signals of the enabled streams, newest first. skip_board leaves out crypto 4h (the dashboard
    gets those from the 4-hour board with live progress)."""
    keys = [k for k in enabled_streams() if not (skip_board and k == "crypto_4h")]
    if not keys:
        return []
    pairs = [(streams.STREAMS[k]["market"], streams.STREAMS[k]["tf"]) for k in keys]
    cond = " OR ".join(["(market=%s AND timeframe=%s)"] * len(pairs))
    args = [x for p in pairs for x in p]
    with _Cursor() as cur:
        cur.execute(f"""SELECT * FROM signal_events WHERE status='ACTIVE' AND ({cond})
                        ORDER BY bar_time DESC, id DESC LIMIT %s""", (*args, int(limit)))
        rows = cur.fetchall() or []
    return [signal_json(r) for r in rows]


def active_for(symbol):
    with _Cursor() as cur:
        cur.execute("SELECT * FROM signal_events WHERE symbol=%s AND status='ACTIVE' ORDER BY bar_time DESC",
                    (symbol,))
        return cur.fetchall() or []


def _tested_keys():
    """Switched-on streams that passed the test on data the model never saw (what the landing page talks about)."""
    return [k for k in enabled_streams() if stream_tested(*k.split("_", 1))]


def public_summary(limit=3, now=None):
    """Landing page: active signals of the tested streams (count + newest few: symbol, side, timeframe) - no levels."""
    now = now or _utcnow()
    keys = _tested_keys()
    if not keys:
        return {"ok": False}
    cond = " OR ".join(["(market=%s AND timeframe=%s)"] * len(keys))
    args = [x for k in keys for x in k.split("_", 1)]
    try:
        with _Cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM signal_events WHERE status='ACTIVE' AND ({cond})", tuple(args))
            n = int((cur.fetchone() or {}).get("n") or 0)
            cur.execute(f"""SELECT symbol, side, timeframe, bar_time FROM signal_events WHERE status='ACTIVE' AND ({cond})
                           ORDER BY bar_time DESC LIMIT %s""", tuple(args) + (int(limit),))
            rows = cur.fetchall() or []
    except Exception:
        return {"ok": False}
    recent = []
    for r in rows:
        t = _parse(r["bar_time"]) + timedelta(seconds=markets.TF_SEC.get(r.get("timeframe") or "4h", 14400))
        mins = max(0, int((now - t).total_seconds() // 60))
        ago = f"{mins} min ago" if mins < 60 else (f"{mins // 60} h ago" if mins < 2880 else f"{mins // 1440} days ago")
        recent.append({"coin": r["symbol"].split("/")[0] if r["symbol"].endswith("/USDT") else r["symbol"],
                       "side": r["side"], "timeframe": r.get("timeframe"), "ago": ago})
    mkts = sorted({k.split("_", 1)[0] for k in keys})
    return {"ok": True, "active": n, "coins": sum(len(symbols_for(m)) for m in mkts), "recent": recent,
            "timeframes": [k.split("_", 1)[1] for k in keys]}


_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _period_label(period):
    """'2025-10-01 to 2026-10-11' -> 'Oct 2025 to Oct 2026'."""
    try:
        a, b = [x.strip() for x in str(period).split(" to ")]
        f = lambda d: f"{_MONTHS[int(d[5:7]) - 1]} {d[:4]}"
        return f"{f(a)} to {f(b)}"
    except Exception:
        return str(period or "")


_coin_test = {"t": 0.0, "data": None}


def coin_test(key):
    """The coin test of a stream (models/coin_test.json, research/coin_expansion.py) when coins were added to it from
    it: the added coins' result on the year the model never saw. None for a stream that got no coins."""
    now = time.time()
    if _coin_test["data"] is None or now - _coin_test["t"] > 600:
        try:
            with open(os.path.join(streams.MODELS_DIR, "coin_test.json"), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        _coin_test.update(t=now, data=data)
    d = _coin_test["data"] or {}
    s = d.get(key) or {}
    added = [c for c in markets.CRYPTO_4H_ONLY] if key == "crypto_4h" else []
    if not s.get("passes") or not added:
        return None
    r = ((d.get("okx") or {}).get(key) or {}).get("new_coins") or s.get("new_coins") or {}
    if not r.get("signals"):
        return None
    return {"coins": len(added), "period": _period_label(d.get("period")), "signals": int(r["signals"]),
            "per_day": r.get("signals_per_day"), "win_rate": r.get("win_rate"), "avg_r": r.get("avg_net_r")}


def tested_summary():
    """Landing page: the test on data the model never saw, for every switched-on tested crypto stream, plus the
    signal-weighted total. Numbers come straight from the model files; nothing is estimated here."""
    out = []
    for key in ("crypto_1h", "crypto_4h"):
        if key not in _tested_keys():
            continue
        try:
            if key == "crypto_4h":
                import signal_engine as se
                t = se.test_stats() or {}
            else:
                m = streams.load_model(key)
                t = (m.meta.get("test") if m is not None else None) or {}
        except Exception:
            t = {}
        if not t.get("signals") or t.get("win_rate") is None:
            continue
        tf = key.split("_", 1)[1]
        row = {"key": key, "timeframe": tf, "period": _period_label(t.get("period")),
               "signals": int(t["signals"]), "per_day": float(t.get("signals_per_day") or 0),
               "win_rate": float(t["win_rate"]), "avg_r": float(t.get("avg_net_r") or 0),
               "profit_factor": t.get("profit_factor"), "max_drawdown_r": t.get("max_drawdown_r"),
               "coins": len(markets.stream_coins("crypto", tf))}
        extra = coin_test(key)
        if extra:
            row["extra_coins"] = extra
        out.append(row)
    if not out:
        return {"streams": [], "signals": 0}
    n = sum(x["signals"] for x in out)
    return {"streams": out, "signals": n, "coins": len(markets.CRYPTO_COINS),
            "per_day": round(sum(x["per_day"] for x in out), 1),
            "win_rate": round(sum(x["signals"] * x["win_rate"] for x in out) / n, 1),
            "avg_r": round(sum(x["signals"] * x["avg_r"] for x in out) / n, 3)}


def stream_info():
    out = []
    for key in ALL_STREAMS:
        cfg = streams.STREAMS[key]
        plan = streams.plan_for(cfg["market"], cfg["tf"])
        last = _state["last_scan"].get(key)
        kind = streams.engine_name(key)
        label = {"ml_v3": "Model v3.1", "model": f"Model {plan['engine']}",
                 "rules": f"Rules {streams.RULES_VERSION} (did not pass the test)"}[kind]
        res = (streams.research_summary().get("streams") or {}).get(key)
        out.append({"key": key, "market": cfg["market"], "timeframe": cfg["tf"], "enabled": key in enabled_streams(),
                    "validated": streams.validated(key), "research": res,
                    "engine": label, "engine_kind": kind, "hold": streams.hold_text(key), "max_bars": plan["max_bars"],
                    "targets_r": plan["targets_r"], "symbols": len(markets.stream_coins(cfg["market"], cfg["tf"])),
                    "last_scan": {k: _iso(v) for k, v in last.items()} if last else None,
                    "error": _state["errors"].get(key)})
    return out


# ===========================================================================
# API
# ===========================================================================
def _uid():
    return session.get("user_id")


def _need_login():
    return jsonify({"ok": False, "error": "Please log in to see signals."}), 401


def _arg(name, allowed=None, default=None):
    v = (request.args.get(name) or "").strip()
    if not v:
        return default
    if allowed is not None and v not in allowed:
        return None
    return v


@hub_bp.route("/api/signals", methods=["GET"])
def api_list():
    if not _uid():
        return _need_login()
    market = _arg("market", markets.MARKETS)
    tf = _arg("timeframe", markets.TIMEFRAMES)
    side = _arg("side", ("LONG", "SHORT"))
    status = _arg("status", tuple(STATUS_FILTERS) + ("all",), "all")
    sort = _arg("sort", tuple(SORTS), "newest")
    symbol = (request.args.get("symbol") or "").strip().upper() or None
    if symbol and symbol not in markets.CRYPTO_COINS and symbol not in markets.FOREX_PAIRS:
        return jsonify({"ok": False, "error": "Unknown symbol."}), 400
    grade = [g for g in (request.args.get("grade") or "").upper().split(",") if g in ("A", "B", "C")]
    try:
        page = max(1, min(int(request.args.get("page", 1)), 1000))
        per = max(1, min(int(request.args.get("per_page", 25)), 100))
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid page."}), 400
    rows, total = list_signals(market, tf, symbol, side, None if status == "all" else status, grade, sort, page, per)
    prices = live_prices(rows)
    return jsonify({"ok": True, "total": total, "page": page, "per_page": per,
                    "pages": max(1, -(-total // per)), "signals": [signal_json(r, prices.get(r["symbol"])) for r in rows]})


@hub_bp.route("/api/signals/stats", methods=["GET"])
def api_stats():
    if not _uid():
        return _need_login()
    market = _arg("market", markets.MARKETS, "crypto")
    tf = _arg("timeframe", markets.TIMEFRAMES, "4h")
    if not market or not tf:
        return jsonify({"ok": False, "error": "Unknown market or timeframe."}), 400
    out = stream_stats(market, tf)
    out["simulated"] = simulated(market, tf)
    out["model_test"] = model_test(market, tf)
    return jsonify({"ok": True, **out})


@hub_bp.route("/api/signals/latest", methods=["GET"])
def api_latest():
    if not _uid():
        return _need_login()
    data = latest_by_stream(3)
    flat = [r for rows in data.values() for r in rows]
    prices = live_prices(flat)
    return jsonify({"ok": True, "streams": {k: [signal_json(r, prices.get(r["symbol"])) for r in v]
                                            for k, v in data.items()}})


@hub_bp.route("/api/signals/streams", methods=["GET"])
def api_streams():
    if not _uid():
        return _need_login()
    return jsonify({"ok": True, "streams": stream_info(), "forex_open": markets.forex_open(_utcnow()),
                    "session": markets.session_label(_utcnow())})


@hub_bp.route("/api/signals/<int:sid>", methods=["GET"])
def api_one(sid):
    if not _uid():
        return _need_login()
    r = get_signal(sid)
    if not r:
        return jsonify({"ok": False, "error": "Signal not found."}), 404
    prices = live_prices([r])
    return jsonify({"ok": True, "signal": signal_json(r, prices.get(r["symbol"]))})


@hub_bp.route("/api/signals/<int:sid>/cancel", methods=["POST"])
def api_cancel(sid):
    if not _uid():
        return _need_login()
    fn = _hooks.get("is_admin")
    if not fn or not fn():
        return jsonify({"ok": False, "error": "Only the site admin can cancel a signal."}), 403
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or "").strip()[:200]
    if len(reason) < 3:
        return jsonify({"ok": False, "error": "Please give a short reason."}), 400
    with _Cursor() as cur:
        cur.execute("""UPDATE signal_events SET status='CANCELLED', cancel_reason=%s, closed_at=%s, updated_at=%s
                       WHERE id=%s AND status='ACTIVE'""", (reason, _utcnow(), _utcnow(), sid))
        n = cur.rowcount
    if not n:
        return jsonify({"ok": False, "error": "Only an active signal can be cancelled."}), 400
    return jsonify({"ok": True})



# ===========================================================================
# Coins page: every coin, every moment - a signal when there is a good trade, otherwise "getting close" or "wait"
# ===========================================================================
_coins_cache = {"at": 0.0, "data": None}
COINS_CACHE_SEC = 20


def coins_status(now=None):
    """One line per signal coin, from the tested crypto streams only (beginners see the signals that passed the test):
    an active signal (buy / sell), a setup a tested model is close to (getting close), or nothing to do (wait).
    Nothing is computed here: it reads the stored signals and the stream boards made at the last candle close
    (the 1h board of this hub, the 4h board of signalboard.py). A coin no stream could get candles for is left out."""
    now = now or _utcnow()
    tested = [k for k in enabled_streams() if streams.STREAMS[k]["market"] == "crypto" and streams.validated(k)]
    tfs = [streams.STREAMS[k]["tf"] for k in tested]
    coins = list(symbols_for("crypto"))
    rows = []
    if tfs:
        with _Cursor() as cur:
            marks = ",".join(["%s"] * len(tfs))
            cur.execute(f"SELECT * FROM signal_events WHERE status='ACTIVE' AND market='crypto' AND timeframe IN ({marks}) "
                        f"ORDER BY bar_time DESC", tuple(tfs))
            rows = cur.fetchall() or []
    prices = live_prices(rows) if rows else {}
    by = {}
    for r in rows:
        by.setdefault(r["symbol"], []).append(r)
    forming, seen_ok, seen_err = {}, set(), set()
    boards = []
    for key in tested:
        e = _engines.get(key)
        board = e._board[1] if e is not None and e._board else None
        if board:
            boards.append((streams.STREAMS[key]["tf"], board))
    if "crypto_4h" in tested and _engines.get("crypto_4h") is None:
        # the 4h stream is engine v3.1, run by signalboard.py: its board has the same fields
        try:
            import signalboard
            with signalboard._board_lock:
                rows4 = dict(signalboard._board.get("rows") or {})
            if rows4:
                boards.append(("4h", rows4))
        except Exception:
            pass
    for tf, board in boards:
        for sym, res in board.items():
            if not isinstance(res, dict):
                continue
            (seen_err if res.get("error") else seen_ok).add(sym)
            if res.get("error") or res.get("active") or not res.get("setup_forming") or res.get("covered") is False:
                continue
            st = float(res.get("strength") or 0)
            if sym not in forming or st > forming[sym]["strength"]:
                forming[sym] = {"timeframe": tf, "strength": round(min(st, 99.0)),
                                "lean": "up" if res.get("bias") == "LONG" else "down"}
    no_data = seen_err - seen_ok          # every stream failed to get candles for it (e.g. the exchange dropped it)
    out = []
    for sym in coins:
        if sym in no_data and sym not in by:
            continue
        sigs = by.get(sym) or []
        if sigs:
            r = sigs[0]
            j = signal_json(r, prices.get(sym))
            out.append({"symbol": sym, "status": "buy" if r["side"] == "LONG" else "sell", "timeframe": r["timeframe"],
                        "signal": dict({k: j.get(k) for k in ("id", "entry", "stop_loss", "stop_now", "targets", "signal_at",
                                                              "expires_at", "hits", "r_now", "pct_now", "price", "decimals")},
                                       strong=is_strong(r)),
                        "more": [x["timeframe"] for x in sigs[1:]]})
        elif sym in forming:
            out.append({"symbol": sym, "status": "forming", **forming[sym]})
        else:
            out.append({"symbol": sym, "status": "wait"})
    # signals a beginner can still join first (newest first), then the ones past target 1, then getting close, then wait
    sigs = sorted([x for x in out if x["status"] in ("buy", "sell")], key=lambda x: x["signal"].get("signal_at") or "", reverse=True)
    sigs.sort(key=lambda x: 1 if (x["signal"].get("hits") or 0) >= 1 else 0)
    rest = sorted([x for x in out if x["status"] not in ("buy", "sell")],
                  key=lambda x: (0 if x["status"] == "forming" else 1, -(x.get("strength") or 0)))
    out = sigs + rest
    sec = min((markets.TF_SEC[t] for t in tfs), default=3600)
    nxt = streams.last_closed_bar("1h" if sec <= 3600 else "4h", now) + timedelta(seconds=2 * sec)
    counts = {k: sum(1 for x in out if x["status"] == k) for k in ("buy", "sell", "forming", "wait")}
    counts["strong"] = sum(1 for x in out if x["status"] in ("buy", "sell") and x["signal"].get("strong"))
    return {"coins": out, "counts": counts, "timeframes": tfs, "updated_at": _iso(now), "next_check": _iso(nxt),
            "strong_test": strong_summary(tfs)}


def is_strong(row):
    """The strongest signals, by levels fixed before the unseen-data test and better on BOTH the walk-forward years and
    the unseen year: crypto 4h = the engine's 'strong' tier (its top 3% of scores), crypto 1h = the model's top grade."""
    if (row.get("market") or "crypto") != "crypto":
        return False
    tf = row.get("timeframe")
    if tf == "4h":
        return row.get("tier") == "strong"
    if tf == "1h":
        return row.get("grade") == "A"
    return False


def strong_summary(tfs=("1h", "4h")):
    """Test results (unseen data, after costs) of the strongest signals and of all signals of the tested crypto streams."""
    parts, alls = [], []
    try:
        if "4h" in tfs:
            import signal_engine as se
            t = se.test_stats() or {}
            days = max(1.0, (t.get("signals") or 0) / max(float(t.get("signals_per_day") or 1), 1e-9))
            if (t.get("strong") or {}).get("signals"):
                parts.append((t["strong"], days))
            if t.get("signals"):
                alls.append((t, days))
        if "1h" in tfs:
            m = streams.load_model("crypto_1h")
            t = (m.meta.get("test") if m is not None else None) or {}
            days = max(1.0, (t.get("signals") or 0) / max(float(t.get("signals_per_day") or 1), 1e-9))
            a = (t.get("by_grade") or {}).get("A") or {}
            if a.get("signals"):
                parts.append((a, days))
            if t.get("signals"):
                alls.append((t, days))
    except Exception:
        return None

    def mix(xs):
        n = sum(x["signals"] for x, _ in xs)
        if not n:
            return None
        return {"signals": n, "per_day": round(sum(x["signals"] / d for x, d in xs), 1),
                "win_rate": round(sum(x["signals"] * x["win_rate"] for x, _ in xs) / n, 1),
                "avg_r": round(sum(x["signals"] * x.get("avg_net_r", 0) for x, _ in xs) / n, 3)}
    st, al = mix(parts), mix(alls)
    return {"strong": st, "all": al} if st and al else None


@hub_bp.route("/coins-now", methods=["GET"])
def coins_page():
    if not _uid():
        return redirect("/login")
    return render_template("coins.html", active="coins")


@hub_bp.route("/api/coins/status", methods=["GET"])
def api_coins_status():
    if not _uid():
        return _need_login()
    t = time.time()
    if _coins_cache["data"] is None or t - _coins_cache["at"] > COINS_CACHE_SEC:
        try:
            _coins_cache.update(at=t, data=coins_status())
        except Exception as e:
            return jsonify({"ok": False, "error": f"Coin status is not available right now: {str(e)[:120]}"}), 500
    return jsonify({"ok": True, **_coins_cache["data"]})
