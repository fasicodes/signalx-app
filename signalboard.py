"""
Signals board + signal alerts - Signals FM
==========================================

* Background refresher: right after every 4-hour candle close it reads
  signal engine v2 for every crypto coin and keeps the result in memory
  (the board page then loads instantly).
* Every NEW signal (a trade that started on the candle that just closed) is
  stored once in `signal_events` (UNIQUE symbol + candle, so several app
  instances never notify twice) and sent to subscribed users as an in-app
  notification (the bell in the dashboard) and, if they chose it, by email.
* Pages / API (login required):
    GET  /signals                 board page
    GET  /api/signals/board       every coin: active trade with live progress, or WAIT with bias
    GET  /api/signals/events      recent new signals
    GET  /api/signals/alerts      the user's alert settings
    POST /api/signals/alerts      save them
"""
import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, redirect, render_template, request, session

from db import get_db_connection
import signal_engine as se

signalboard_bp = Blueprint("signalboard", __name__)

REFRESH_DELAY_SEC = 60          # wait after a candle close so the exchange has published it
LOOP_SEC = 30
RETRY_ERRORS_SEC = 600
MAX_EMAILS_PER_DAY = 12
PRICE_TTL_SEC = 20

_hooks = {"engine_signal": None, "coins": [], "prices": None, "send_email": None}
_board = {"bar": None, "rows": {}, "updated_at": None, "attempt_at": 0.0, "running": False}
_board_lock = threading.Lock()
_price_cache = {"ts": 0.0, "data": {}}


def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def _iso(d):
    return d.replace(microsecond=0).isoformat() + "Z" if isinstance(d, datetime) else d


def _parse(v):
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", ""))
    except ValueError:
        return None


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
                UNIQUE (symbol, bar_time)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_alert_subs (
                user_id INT PRIMARY KEY,
                enabled TINYINT(1) NOT NULL DEFAULT 1,
                email TINYINT(1) NOT NULL DEFAULT 0,
                coins TEXT NULL,
                emails_day VARCHAR(10) NULL,
                emails_sent INT NOT NULL DEFAULT 0,
                updated_at DATETIME NOT NULL
            )
            """
        )


# ===========================================================================
# board refresh + new-signal events
# ===========================================================================
def crypto_coins():
    return list(_hooks.get("coins") or [])


def refresh_board(now=None, only=None):
    """Reads the engine for every coin (or `only`). Returns the new events."""
    fn = _hooks.get("engine_signal")
    if fn is None:
        return []
    now = now or _utcnow()
    bar = se.last_closed_bar(now)
    rows = {}
    for sym in (only or crypto_coins()):
        try:
            rows[sym] = fn(sym)
        except Exception as e:  # engine_signal never raises, but stay safe
            rows[sym] = {"error": str(e)[:160]}
        time.sleep(0)
    with _board_lock:
        if only and _board["bar"] == bar:
            _board["rows"].update(rows)
        else:
            _board["rows"] = rows
        _board["bar"] = bar
        _board["updated_at"] = now
    events = []
    for sym, res in rows.items():
        if res.get("fresh") and res.get("active"):
            ev = record_event(sym, res)
            if ev:
                events.append(ev)
    if events:
        notify(events)
    return events


def record_event(sym, res):
    a = res["active"]
    bar_time = _parse(a.get("bar_time"))
    try:
        with _Cursor() as cur:
            cur.execute(
                """INSERT INTO signal_events (symbol, side, bar_time, entry, stop_loss, take_profit, confidence, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (sym, a["side"], bar_time, float(a["entry"]), float(a["stop_loss"]), float(a["take_profit"]),
                 a.get("confidence"), _utcnow()),
            )
    except Exception:
        return None  # already recorded (another instance, or a restart)
    return {"symbol": sym, "side": a["side"], "bar_time": bar_time, "entry": float(a["entry"]),
            "stop_loss": float(a["stop_loss"]), "take_profit": float(a["take_profit"]), "confidence": a.get("confidence"),
            "expires_at": a.get("expires_at")}


def _fmt(x):
    x = float(x)
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def _sub_coins(raw):
    if not raw or raw == "ALL":
        return None
    try:
        v = json.loads(raw)
        return set(v) if isinstance(v, list) and v else None
    except (TypeError, ValueError):
        return None


def notify(events):
    """In-app notification for every subscriber; email for those who asked for it."""
    with _Cursor() as cur:
        cur.execute("""SELECT s.*, u.email AS user_email FROM signal_alert_subs s JOIN users u ON u.id = s.user_id
                       WHERE s.enabled = 1""")
        subs = cur.fetchall() or []
    if not subs:
        return 0
    today = _utcnow().strftime("%Y-%m-%d")
    outbox = []
    with _Cursor() as cur:
        for sub in subs:
            wanted = _sub_coins(sub.get("coins"))
            mine = [e for e in events if wanted is None or e["symbol"] in wanted]
            if not mine:
                continue
            for e in mine:
                title = f"New {e['side']} signal: {e['symbol']}"
                msg = f"Entry {_fmt(e['entry'])} · stop {_fmt(e['stop_loss'])} · target {_fmt(e['take_profit'])}"
                if e.get("confidence") is not None:
                    msg += f" · win probability {float(e['confidence']):.0f}%"
                cur.execute(
                    "INSERT INTO notifications (user_id, category, symbol, title, message) VALUES (%s,%s,%s,%s,%s)",
                    (sub["user_id"], "SIGNAL", e["symbol"], title, msg),
                )
            if sub.get("email") and sub.get("user_email"):
                sent = int(sub.get("emails_sent") or 0) if sub.get("emails_day") == today else 0
                if sent < MAX_EMAILS_PER_DAY:
                    outbox.append((sub["user_email"], mine))
                    cur.execute("UPDATE signal_alert_subs SET emails_day=%s, emails_sent=%s WHERE user_id=%s",
                                (today, sent + 1, sub["user_id"]))
    send = _hooks.get("send_email")
    if send and outbox:
        threading.Thread(target=_send_all, args=(send, outbox), name="signal-alert-mail", daemon=True).start()
    return len(subs)


def _email_html(events):
    base = (os.environ.get("SITE_URL") or "").rstrip("/")
    rows = "".join(
        f"<tr><td style='padding:6px 10px'><b>{e['symbol']}</b></td>"
        f"<td style='padding:6px 10px;color:{'#0f8a50' if e['side'] == 'LONG' else '#d92d47'}'><b>{e['side']}</b></td>"
        f"<td style='padding:6px 10px'>{_fmt(e['entry'])}</td><td style='padding:6px 10px'>{_fmt(e['stop_loss'])}</td>"
        f"<td style='padding:6px 10px'>{_fmt(e['take_profit'])}</td>"
        f"<td style='padding:6px 10px'>{(e.get('confidence') or 0):.0f}%</td></tr>"
        for e in events)
    link = f"{base}/signals" if base else "/signals"
    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;color:#0d1c15;max-width:620px">
      <h2 style="margin:0 0 6px">New signal{'s' if len(events) > 1 else ''} from Signals FM</h2>
      <p style="color:#4c5f56;margin:0 0 14px">Signal engine v2, 4-hour candles. Time limit 8 days.</p>
      <table style="border-collapse:collapse;font-size:14px;border:1px solid #dbe6e0">
        <tr style="background:#eef4f1;text-align:left"><th style="padding:6px 10px">Coin</th><th style="padding:6px 10px">Side</th>
        <th style="padding:6px 10px">Entry</th><th style="padding:6px 10px">Stop</th><th style="padding:6px 10px">Target</th>
        <th style="padding:6px 10px">Win prob.</th></tr>{rows}
      </table>
      <p style="margin:16px 0"><a href="{link}" style="background:#16a34a;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none">Open the signals board</a></p>
      <p style="color:#6f8279;font-size:12px;line-height:1.6">Not financial advice. Signals can and do lose; past results do not guarantee future results.
      You get this email because you turned on signal alerts. Turn them off any time on the signals page ({link}).</p>
    </div>"""


def _send_all(send, outbox):
    for to, events in outbox:
        subject = (f"New {events[0]['side']} signal: {events[0]['symbol']}" if len(events) == 1
                   else f"{len(events)} new signals on Signals FM")
        try:
            send(to, subject, _email_html(events))
        except Exception as e:
            print(f"[signalboard] email failed: {e}")
        time.sleep(1.0)


def board_step(now=None):
    """One loop pass. Returns 'refresh', 'retry' or None (for tests)."""
    now = now or _utcnow()
    bar = se.last_closed_bar(now)
    due_at = bar + timedelta(seconds=se.TF_SEC + REFRESH_DELAY_SEC)
    if _board["bar"] != bar and now >= due_at:
        _board["attempt_at"] = time.time()
        refresh_board(now)
        return "refresh"
    if _board["bar"] is None:   # just started: build the board right away
        _board["attempt_at"] = time.time()
        refresh_board(now)
        return "refresh"
    errored = [s for s, r in _board["rows"].items() if r.get("error")]
    if errored and time.time() - _board["attempt_at"] >= RETRY_ERRORS_SEC:
        _board["attempt_at"] = time.time()
        refresh_board(now, only=errored)
        return "retry"
    return None


def _loop():
    time.sleep(25)
    while True:
        try:
            board_step()
        except Exception:
            print(f"[signalboard] refresh failed:\n{traceback.format_exc()}")
        time.sleep(LOOP_SEC)


_started = False
_start_lock = threading.Lock()


def init_signalboard(app=None, *, engine_signal=None, coins=None, prices=None, send_email=None, start=True):
    global _started
    if engine_signal:
        _hooks["engine_signal"] = engine_signal
    if coins is not None:
        _hooks["coins"] = list(coins)
    if prices:
        _hooks["prices"] = prices
    if send_email:
        _hooks["send_email"] = send_email
    try:
        init_tables()
    except Exception as e:
        print(f"[signalboard] WARNING: could not create tables: {e}")
    if not start or (os.environ.get("SIGNAL_BOARD", "on") or "on").lower() in ("0", "off", "false", "no"):
        return
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="signal-board", daemon=True).start()


# ===========================================================================
# API + page
# ===========================================================================
def _uid():
    return session.get("user_id")


def live_prices(symbols):
    if time.time() - _price_cache["ts"] < PRICE_TTL_SEC and all(s in _price_cache["data"] for s in symbols):
        return _price_cache["data"]
    fn = _hooks.get("prices")
    if not fn:
        return {}
    try:
        data = fn(symbols) or {}
        _price_cache.update({"ts": time.time(), "data": data})
        return data
    except Exception:
        return _price_cache["data"]


def board_rows():
    with _board_lock:
        rows = dict(_board["rows"])
        bar, updated = _board["bar"], _board["updated_at"]
    active_syms = [s for s, r in rows.items() if r.get("active")]
    prices = live_prices(active_syms) if active_syms else {}
    out = []
    for sym in crypto_coins():
        r = rows.get(sym)
        if r is None:
            out.append({"symbol": sym, "state": "LOADING"})
            continue
        if r.get("error"):
            out.append({"symbol": sym, "state": "UNAVAILABLE", "error": r["error"]})
            continue
        a = r.get("active")
        item = {
            "symbol": sym, "state": "ACTIVE" if a else "WAIT", "verdict": r.get("verdict"), "fresh": bool(r.get("fresh")),
            "confidence": r.get("confidence"), "bias": r.get("bias"), "p_long": r.get("p_long"), "p_short": r.get("p_short"),
            "strength": r.get("strength"), "last_closed": r.get("last_closed"), "next_update": r.get("next_update"),
            "limited_history": (r.get("history_bars") or 0) < 300,
        }
        if a:
            live = prices.get(sym)
            prog = se.progress({k: (float(a[k]) if k in ("entry", "stop_loss", "take_profit") else a[k]) for k in a},
                               live if live is not None else r.get("last_close"))
            item.update({"active": a, "progress": prog, "live_price": live})
        out.append(item)

    def key(it):
        if it["state"] == "ACTIVE":
            return (0 if it["fresh"] else 1, -(_parse(it["active"].get("signal_at")) or datetime.min).timestamp())
        if it["state"] == "WAIT":
            return (2, -(it.get("strength") or 0))
        return (3, 0)

    out.sort(key=key)
    return out, bar, updated


@signalboard_bp.route("/signals", methods=["GET"])
def signals_page():
    if not _uid():
        return redirect("/login")
    return render_template("signals.html", active="signals")


@signalboard_bp.route("/api/signals/board", methods=["GET"])
def api_board():
    if not _uid():
        return jsonify({"ok": False, "error": "Login required."}), 401
    if _board["bar"] is None and not _board["running"] and _hooks.get("engine_signal"):
        def _build():
            _board["running"] = True
            try:
                refresh_board()
            finally:
                _board["running"] = False
        threading.Thread(target=_build, daemon=True).start()
    rows, bar, updated = board_rows()
    nxt = (bar + timedelta(seconds=2 * se.TF_SEC)) if bar else None
    return jsonify({
        "ok": True, "rows": rows, "bar_time": _iso(bar), "next_update": _iso(nxt), "updated_at": _iso(updated),
        "loading": bar is None, "test": se.test_stats(), "timeframe": se.TIMEFRAME,
        "counts": {"active": sum(1 for r in rows if r["state"] == "ACTIVE"),
                   "new": sum(1 for r in rows if r.get("fresh")), "coins": len(rows)},
    })


@signalboard_bp.route("/api/signals/events", methods=["GET"])
def api_events():
    if not _uid():
        return jsonify({"ok": False, "error": "Login required."}), 401
    try:
        limit = max(1, min(int(request.args.get("limit", 20)), 100))
    except ValueError:
        limit = 20
    with _Cursor() as cur:
        cur.execute("SELECT * FROM signal_events ORDER BY bar_time DESC, id DESC LIMIT %s", (limit,))
        rows = cur.fetchall() or []
    out = [{"symbol": r["symbol"], "side": r["side"], "entry": r["entry"], "stop_loss": r["stop_loss"],
            "take_profit": r["take_profit"], "confidence": r["confidence"],
            "signal_at": _iso(_parse(r["bar_time"]) + timedelta(seconds=se.TF_SEC))} for r in rows]
    return jsonify({"ok": True, "events": out})


@signalboard_bp.route("/api/signals/alerts", methods=["GET", "POST"])
def api_alerts():
    uid = _uid()
    if not uid:
        return jsonify({"ok": False, "error": "Login required."}), 401
    if request.method == "GET":
        with _Cursor() as cur:
            cur.execute("SELECT * FROM signal_alert_subs WHERE user_id=%s", (uid,))
            r = cur.fetchone()
        coins = _sub_coins(r.get("coins")) if r else None
        return jsonify({"ok": True, "enabled": bool(r and r["enabled"]), "email": bool(r and r["email"]),
                        "coins": sorted(coins) if coins else "ALL", "choices": crypto_coins(),
                        "email_ready": bool(os.environ.get("MAIL_USERNAME") and os.environ.get("MAIL_PASSWORD"))})
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Invalid request."}), 400
    enabled = bool(data.get("enabled"))
    email = bool(data.get("email")) and enabled
    coins = data.get("coins")
    if coins in (None, "ALL", []):
        coins_val = "ALL"
    elif isinstance(coins, list) and all(isinstance(c, str) for c in coins):
        valid = [c for c in coins if c in crypto_coins()]
        if not valid:
            return jsonify({"ok": False, "error": "Pick at least one coin, or all coins."}), 400
        coins_val = json.dumps(sorted(set(valid)))
    else:
        return jsonify({"ok": False, "error": "Invalid coin list."}), 400
    with _Cursor() as cur:
        cur.execute("SELECT user_id FROM signal_alert_subs WHERE user_id=%s", (uid,))
        if cur.fetchone():
            cur.execute("UPDATE signal_alert_subs SET enabled=%s, email=%s, coins=%s, updated_at=%s WHERE user_id=%s",
                        (int(enabled), int(email), coins_val, _utcnow(), uid))
        else:
            cur.execute("""INSERT INTO signal_alert_subs (user_id, enabled, email, coins, emails_sent, updated_at)
                           VALUES (%s,%s,%s,%s,0,%s)""", (uid, int(enabled), int(email), coins_val, _utcnow()))
    return jsonify({"ok": True, "enabled": enabled, "email": email, "coins": coins_val if coins_val == "ALL" else json.loads(coins_val)})
