"""
Signals board + signal alerts - Signals FM
==========================================

* Background refresher: right after every 4-hour candle close it reads
  signal engine v3 for every crypto coin and keeps the result in memory
  (the board page then loads instantly). Trades are given for every crypto coin
  in the engine's signal list (signal_model.json); other symbols show the lean only.
* Every NEW signal (a trade that started on the candle that just closed) is
  stored once in `signal_events` (UNIQUE symbol + candle, so several app
  instances never notify twice) and sent to subscribed users as an in-app
  notification (the bell in the dashboard) and, if they chose it, by email
  and Telegram (telegram_bot.py, linked chats only).
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
import markets

signalboard_bp = Blueprint("signalboard", __name__)

REFRESH_DELAY_SEC = 60          # wait after a candle close so the exchange has published it
LOOP_SEC = 30
RETRY_ERRORS_SEC = 600
MAX_EMAILS_PER_DAY = 12
PRICE_TTL_SEC = 20

_hooks = {"engine_signal": None, "coins": [], "prices": None, "send_email": None, "secret": None, "telegram": None}
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
    import signal_hub
    signal_hub.init_tables()   # signal_events + the stream columns (safe, additive migration)


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
    """Stores a new crypto 4h signal (engine v3.1) as a crypto/4h stream event."""
    import signal_hub
    try:
        return signal_hub.record(sym, "crypto", "4h", res["active"])
    except Exception as e:
        print(f"[signalboard] could not store the signal for {sym}: {e}")
        return None


def _targets_txt(e):
    tps = e.get("targets") or se.targets(e["side"], float(e["entry"]), float(e["stop_loss"]))
    return " · ".join(f"TP{k + 1} {_fmt(v, e['symbol'])}" for k, v in enumerate(tps))


def _fmt(x, symbol=None):
    x = float(x)
    if symbol and markets.is_forex(symbol):
        return f"{x:.{markets.price_decimals(symbol)}f}"
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def _sub_list(raw, allowed):
    if not raw or raw == "ALL":
        return None
    try:
        v = json.loads(raw)
        v = [x for x in v if x in allowed] if isinstance(v, list) else []
        return set(v) if v else None
    except (TypeError, ValueError):
        return None


def wants(sub, e):
    """Does this subscriber want this event? (coins/pairs, markets, timeframes; empty = all)"""
    coins = _sub_coins(sub.get("coins"))
    mk = _sub_list(sub.get("markets"), markets.MARKETS)
    tfs = _sub_list(sub.get("timeframes"), markets.TIMEFRAMES)
    if mk is not None and (e.get("market") or "crypto") not in mk:
        return False
    if tfs is not None and (e.get("timeframe") or "4h") not in tfs:
        return False
    return coins is None or e["symbol"] in coins


def event_title(e):
    return f"New {e['side']} signal: {e['symbol']} ({e.get('timeframe') or '4h'})"


def _untested(e):
    """True when the signal comes from a stream that has not passed its test on unseen data."""
    try:
        import streams
        return not streams.validated(f"{e.get('market') or 'crypto'}_{e.get('timeframe') or '4h'}")
    except Exception:
        return False


def event_text(e):
    msg = f"Entry {_fmt(e['entry'], e['symbol'])} · stop {_fmt(e['stop_loss'], e['symbol'])} · {_targets_txt(e)}"
    if (e.get("timeframe") or "4h") == "4h" and (e.get("market") or "crypto") == "crypto" and e.get("confidence") is not None:
        msg += f" · model confidence {float(e['confidence']):.0f}%"
    news = e.get("news")
    if isinstance(news, dict) and news.get("title"):
        msg += f" · high-impact news soon: {news['title']}"
    if _untested(e):
        msg += " · experimental: simple rules that have not passed our test on unseen data"
    return msg


def _sub_coins(raw):
    if not raw or raw == "ALL":
        return None
    try:
        v = json.loads(raw)
        return set(v) if isinstance(v, list) and v else None
    except (TypeError, ValueError):
        return None


def notify(events):
    """In-app notification for every subscriber; email and Telegram for those who asked for them."""
    with _Cursor() as cur:
        cur.execute("""SELECT s.*, u.email AS user_email FROM signal_alert_subs s JOIN users u ON u.id = s.user_id
                       WHERE s.enabled = 1""")
        subs = cur.fetchall() or []
    today = _utcnow().strftime("%Y-%m-%d")
    outbox = []
    per_user = {}
    with _Cursor() as cur:
        for sub in subs:
            mine = [e for e in events if wants(sub, e)]
            if not mine:
                continue
            per_user[sub["user_id"]] = mine
            for e in mine:
                cur.execute(
                    "INSERT INTO notifications (user_id, category, symbol, title, message) VALUES (%s,%s,%s,%s,%s)",
                    (sub["user_id"], "SIGNAL", e["symbol"], event_title(e)[:255], event_text(e)[:1000]),
                )
            if sub.get("email") and sub.get("user_email"):
                sent = int(sub.get("emails_sent") or 0) if sub.get("emails_day") == today else 0
                if sent < MAX_EMAILS_PER_DAY:
                    outbox.append((sub["user_email"], sub["user_id"], mine))
                    cur.execute("UPDATE signal_alert_subs SET emails_day=%s, emails_sent=%s WHERE user_id=%s",
                                (today, sent + 1, sub["user_id"]))
    send = _hooks.get("send_email")
    if send and outbox:
        threading.Thread(target=_send_all, args=(send, outbox), name="signal-alert-mail", daemon=True).start()
    tg = _hooks.get("telegram")
    if tg:
        try:
            tg(per_user, events)          # linked chats with alerts on (+ the optional channel)
        except Exception as e:
            print(f"[signalboard] telegram alerts failed: {e}")
    return len(subs)


def _unsub_token(user_id):
    from itsdangerous import URLSafeSerializer
    return URLSafeSerializer(_hooks.get("secret") or "signalsfm", salt="signal-alerts-unsub").dumps({"u": int(user_id)})


def _unsub_user(token):
    from itsdangerous import BadSignature, URLSafeSerializer
    try:
        return int(URLSafeSerializer(_hooks.get("secret") or "signalsfm", salt="signal-alerts-unsub").loads(token)["u"])
    except (BadSignature, KeyError, TypeError, ValueError):
        return None


def _email_html(events, user_id=None):
    base = (os.environ.get("SITE_URL") or "").rstrip("/")
    def tps(e):
        return e.get("targets") or se.targets(e["side"], float(e["entry"]), float(e["stop_loss"]))
    rows = "".join(
        f"<tr><td style='padding:6px 10px'><b>{e['symbol']}</b></td>"
        f"<td style='padding:6px 10px'>{e.get('timeframe') or '4h'}</td>"
        f"<td style='padding:6px 10px;color:{'#0f8a50' if e['side'] == 'LONG' else '#d92d47'}'><b>{e['side']}</b></td>"
        f"<td style='padding:6px 10px'>{_fmt(e['entry'], e['symbol'])}</td>"
        f"<td style='padding:6px 10px'>{_fmt(e['stop_loss'], e['symbol'])}</td>"
        + "".join(f"<td style='padding:6px 10px'>{_fmt(v, e['symbol'])}</td>" for v in tps(e))
        + "</tr>"
        for e in events)
    link = f"{base}/signals" if base else "/signals"
    if user_id is not None:
        u = f"{base}/signals/unsubscribe?t={_unsub_token(user_id)}"
        unsub = f'<a href="{u}" style="color:#6f8279">Stop these emails</a> (one click, no login).'
    else:
        unsub = f"Turn them off any time on the signals page ({link})."
    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;color:#0d1c15;max-width:620px">
      <h2 style="margin:0 0 6px">New signal{'s' if len(events) > 1 else ''} from Signals FM</h2>
      <p style="color:#4c5f56;margin:0 0 14px">One-target signals: close the whole position at the target. Three-target
      signals: close a third at each; after TP1 move the stop to the entry, after TP2 to TP1. Every signal has a time limit
      (see the signals page).</p>
      <table style="border-collapse:collapse;font-size:14px;border:1px solid #dbe6e0">
        <tr style="background:#eef4f1;text-align:left"><th style="padding:6px 10px">Symbol</th><th style="padding:6px 10px">Timeframe</th><th style="padding:6px 10px">Side</th>
        <th style="padding:6px 10px">Entry</th><th style="padding:6px 10px">Stop</th><th style="padding:6px 10px">TP1</th>
        <th style="padding:6px 10px">TP2</th><th style="padding:6px 10px">TP3</th></tr>{rows}
      </table>
      <p style="margin:16px 0"><a href="{link}" style="background:#16a34a;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none">Open the signals board</a></p>
      <p style="color:#6f8279;font-size:12px;line-height:1.6">Not financial advice. Signals can and do lose; past results do not guarantee future results.
      You get this email because you turned on signal alerts. {unsub}</p>
    </div>"""


def _send_all(send, outbox):
    for item in outbox:
        to, uid, events = item if len(item) == 3 else (item[0], None, item[1])
        subject = (event_title(events[0]) if len(events) == 1
                   else f"{len(events)} new signals on Signals FM")
        try:
            send(to, subject, _email_html(events, uid))
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


def init_signalboard(app=None, *, engine_signal=None, coins=None, prices=None, send_email=None, telegram=None, start=True):
    global _started
    if app is not None and getattr(app, "secret_key", None):
        _hooks["secret"] = app.secret_key
    if engine_signal:
        _hooks["engine_signal"] = engine_signal
    if coins is not None:
        _hooks["coins"] = list(coins)
    if prices:
        _hooks["prices"] = prices
    if send_email:
        _hooks["send_email"] = send_email
    if telegram:
        _hooks["telegram"] = telegram
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
            "setup_forming": bool(r.get("setup_forming")),
            "covered": r.get("covered", True) is not False, "tier": r.get("tier"),
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
            return (2 if it.get("covered", True) else 3, -(it.get("strength") or 0))
        return (4, 0)

    out.sort(key=key)
    return out, bar, updated


def _ago(dt, now):
    mins = max(0, int((now - dt).total_seconds() // 60))
    if mins < 60:
        return f"{mins} min ago"
    hours = mins // 60
    return f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"


def public_summary(limit=3, now=None):
    """For the landing page (visitors): how many signals are active and the newest few (coin, side, start).
    Read from the board in memory: no prices, no network. Entry, stop and target stay for members."""
    now = now or _utcnow()
    with _board_lock:
        rows = dict(_board["rows"])
    if not rows:
        return {"ok": False}
    act = []
    for sym, r in rows.items():
        a = r.get("active") if isinstance(r, dict) and not r.get("error") else None
        if a and a.get("side") in ("LONG", "SHORT"):
            act.append((_parse(a.get("signal_at")) or _parse(a.get("bar_time")) or datetime.min, sym, a["side"]))
    act.sort(reverse=True)
    recent = [{"coin": sym.split("/")[0], "side": side, "ago": _ago(t, now) if t != datetime.min else ""}
              for t, sym, side in act[:limit]]
    return {"ok": True, "active": len(act), "coins": len(crypto_coins()) or len(rows), "recent": recent}


@signalboard_bp.route("/signals", methods=["GET"])
def signals_page():
    if not _uid():
        return redirect("/login")
    import signal_hub
    return render_template("signals.html", active="signals", streams=signal_hub.stream_info(),
                           crypto_coins=markets.CRYPTO_COINS, forex_pairs=markets.forex_signal_pairs())


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
    sides = [r["active"]["side"] for r in rows if r.get("state") == "ACTIVE"]
    warning = None
    if len(sides) >= 3:
        top = max(set(sides), key=sides.count)
        if sides.count(top) / len(sides) >= 0.75:
            warning = {"side": top, "count": sides.count(top), "total": len(sides)}
    return jsonify({
        "ok": True, "rows": rows, "bar_time": _iso(bar), "next_update": _iso(nxt), "updated_at": _iso(updated),
        "loading": bar is None, "test": se.test_stats(), "timeframe": se.TIMEFRAME,
        "counts": {"active": sum(1 for r in rows if r["state"] == "ACTIVE"),
                   "new": sum(1 for r in rows if r.get("fresh")), "coins": len(rows),
                   "forming": sum(1 for r in rows if r.get("setup_forming"))},
        "same_side_warning": warning, "streams_active": _streams_active(), "streams_enabled": _streams_enabled(),
    })


def _streams_enabled():
    try:
        import signal_hub
        return signal_hub.enabled_streams()
    except Exception:
        return []


def _streams_active():
    """Active signals of the 15m / 1h crypto streams and the forex streams (the board itself is crypto 4h)."""
    try:
        import signal_hub
        return signal_hub.active_streams(limit=40)
    except Exception:
        return []


@signalboard_bp.route("/api/signals/events", methods=["GET"])
def api_events():
    if not _uid():
        return jsonify({"ok": False, "error": "Login required."}), 401
    import signal_hub
    try:
        limit = max(1, min(int(request.args.get("limit", 20)), 100))
    except ValueError:
        limit = 20
    market = request.args.get("market") if request.args.get("market") in markets.MARKETS else None
    tf = request.args.get("timeframe") if request.args.get("timeframe") in markets.TIMEFRAMES else None
    rows, _ = signal_hub.list_signals(market=market, tf=tf, per_page=limit)
    return jsonify({"ok": True, "events": [signal_hub.signal_json(r) for r in rows]})


def symbol_choices():
    return list(crypto_coins() or markets.CRYPTO_COINS) + markets.forex_signal_pairs()


def _clean_list(v, allowed, what):
    if v in (None, "ALL", []):
        return "ALL"
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        valid = sorted({x for x in v if x in allowed})
        if not valid:
            raise ValueError(f"Pick at least one {what}, or all.")
        return "ALL" if set(valid) == set(allowed) else json.dumps(valid)
    raise ValueError(f"Invalid {what} list.")


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
        mk = _sub_list(r.get("markets"), markets.MARKETS) if r else None
        tfs = _sub_list(r.get("timeframes"), markets.TIMEFRAMES) if r else None
        return jsonify({"ok": True, "enabled": bool(r and r["enabled"]), "email": bool(r and r["email"]),
                        "coins": sorted(coins) if coins else "ALL", "choices": symbol_choices(),
                        "markets": sorted(mk) if mk else "ALL", "timeframes": sorted(tfs) if tfs else "ALL",
                        "email_ready": bool(os.environ.get("MAIL_USERNAME") and os.environ.get("MAIL_PASSWORD"))})
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Invalid request."}), 400
    enabled = bool(data.get("enabled"))
    email = bool(data.get("email")) and enabled
    try:
        coins_val = _clean_list(data.get("coins"), symbol_choices(), "symbol")
        markets_val = _clean_list(data.get("markets"), list(markets.MARKETS), "market")
        tfs_val = _clean_list(data.get("timeframes"), list(markets.TIMEFRAMES), "timeframe")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    with _Cursor() as cur:
        cur.execute("SELECT user_id FROM signal_alert_subs WHERE user_id=%s", (uid,))
        if cur.fetchone():
            cur.execute("""UPDATE signal_alert_subs SET enabled=%s, email=%s, coins=%s, markets=%s, timeframes=%s,
                           updated_at=%s WHERE user_id=%s""",
                        (int(enabled), int(email), coins_val, markets_val, tfs_val, _utcnow(), uid))
        else:
            cur.execute("""INSERT INTO signal_alert_subs (user_id, enabled, email, coins, markets, timeframes,
                           emails_sent, updated_at) VALUES (%s,%s,%s,%s,%s,%s,0,%s)""",
                        (uid, int(enabled), int(email), coins_val, markets_val, tfs_val, _utcnow()))
    dec = lambda v: v if v == "ALL" else json.loads(v)
    return jsonify({"ok": True, "enabled": enabled, "email": email, "coins": dec(coins_val),
                    "markets": dec(markets_val), "timeframes": dec(tfs_val)})


@signalboard_bp.route("/signals/unsubscribe", methods=["GET"])
def unsubscribe():
    uid = _unsub_user(request.args.get("t") or "")
    if uid is None:
        return render_template("message.html", title="Link not valid",
                               text="This unsubscribe link is not valid. You can turn email alerts off on the signals page."), 400
    with _Cursor() as cur:
        cur.execute("UPDATE signal_alert_subs SET email=0, updated_at=%s WHERE user_id=%s", (_utcnow(), uid))
    return render_template("message.html", title="Email alerts are off",
                           text="You will not get signal emails any more. In-app alerts (the bell) stay as you set them on the signals page.")
