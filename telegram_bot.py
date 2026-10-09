"""
Telegram alerts - Signals FM
============================

Users connect Telegram on the Signals board (Signal alerts -> Connect Telegram):
  1. POST /api/telegram/link gives a one-time code (valid 30 minutes) inside a t.me link.
  2. The user presses Start in Telegram, so Telegram sends "/start <code>" to this app.
  3. The chat is linked to the account, and signal alerts are switched on.
After that, every new signal (signalboard.notify) is sent to the user's chat for the coins they chose.
Optional: TELEGRAM_CHANNEL_ID also posts every new signal to a channel (the bot must be an admin there).

Bot commands: /signals (active signals now), /stop (pause), /resume, /unlink, /help.

Environment variables:
  TELEGRAM_BOT_TOKEN   token from @BotFather. Without it, everything here stays off.
  TELEGRAM_CHANNEL_ID  optional: @your_channel or -100... id
  SITE_URL             public address (otherwise Railway's RAILWAY_PUBLIC_DOMAIN). Used for the webhook and links.
How updates arrive: a webhook at /telegram/webhook when the public address is known (checked with a secret header),
otherwise the app asks Telegram for updates itself (long polling).

API (login required): GET /api/telegram/status, POST /api/telegram/link, /unlink, /test, /toggle
"""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import traceback
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, session

from db import get_db_connection

telegram_bp = Blueprint("telegram", __name__)

CODE_TTL_MIN = 30
MAX_PER_DAY = 30            # alert messages per user per UTC day
TEST_EVERY_SEC = 30
API_TIMEOUT = 10

_state = {"username": None, "mode": None, "started": False, "sync": False, "board_rows": None}
_lock = threading.Lock()
_last_test = {}


# ===========================================================================
# config + Telegram API
# ===========================================================================
def token():
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


def enabled():
    return bool(re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,80}", token()))


def channel_id():
    v = (os.environ.get("TELEGRAM_CHANNEL_ID") or "").strip()
    return v if re.fullmatch(r"@[A-Za-z0-9_]{5,64}|-?\d{5,20}", v) else ""


def site_base():
    site = (os.environ.get("SITE_URL") or "").strip().rstrip("/")
    if re.fullmatch(r"https://[A-Za-z0-9.\-:]+", site):
        return site
    dom = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip().rstrip("/")
    if re.fullmatch(r"[A-Za-z0-9.\-]+", dom):
        return "https://" + dom
    return ""


def webhook_secret():
    """Derived from the bot token: stable across restarts, unknown to anyone without the token."""
    return hashlib.sha256(("sfm-telegram-webhook|" + token()).encode()).hexdigest()[:48]


class TelegramError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def _requests():
    import requests
    return requests


def api(method, payload=None, timeout=API_TIMEOUT):
    """Calls the Bot API. Retries once when Telegram asks us to slow down (429)."""
    if not enabled():
        raise TelegramError("Telegram is not set up.")
    url = f"https://api.telegram.org/bot{token()}/{method}"
    for attempt in (1, 2):
        r = _requests().post(url, json=payload or {}, timeout=timeout)
        try:
            data = r.json()
        except ValueError:
            data = {"ok": False, "description": f"HTTP {getattr(r, 'status_code', '?')}"}
        if data.get("ok"):
            return data.get("result")
        code = data.get("error_code") or getattr(r, "status_code", None)
        retry = (data.get("parameters") or {}).get("retry_after")
        if code == 429 and retry and attempt == 1:
            time.sleep(min(float(retry), 10.0))
            continue
        raise TelegramError(str(data.get("description") or "Telegram error")[:200], code)


def send(chat_id, text, **extra):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    payload.update(extra)
    return api("sendMessage", payload)


def bot_username():
    v = (os.environ.get("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
    if v:
        return v
    if _state["username"] is None and enabled():
        try:
            _state["username"] = (api("getMe") or {}).get("username") or ""
        except Exception as e:
            print(f"[telegram] getMe failed: {e}")
            return ""
    return _state["username"] or ""


# ===========================================================================
# database
# ===========================================================================
def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


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
            CREATE TABLE IF NOT EXISTS telegram_links (
                user_id INT PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                username VARCHAR(64) NULL,
                enabled TINYINT(1) NOT NULL DEFAULT 1,
                sent_day VARCHAR(10) NULL,
                sent_count INT NOT NULL DEFAULT 0,
                linked_at DATETIME NOT NULL,
                UNIQUE (chat_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_link_codes (
                code VARCHAR(40) PRIMARY KEY,
                user_id INT NOT NULL,
                expires_at DATETIME NOT NULL
            )
            """
        )


def _link_of_user(uid):
    with _Cursor() as cur:
        cur.execute("SELECT * FROM telegram_links WHERE user_id=%s", (uid,))
        return cur.fetchone()


def _link_of_chat(chat_id):
    with _Cursor() as cur:
        cur.execute("SELECT * FROM telegram_links WHERE chat_id=%s", (int(chat_id),))
        return cur.fetchone()


def create_code(uid):
    code = secrets.token_urlsafe(18)[:24]
    now = _utcnow()
    with _Cursor() as cur:
        cur.execute("DELETE FROM telegram_link_codes WHERE user_id=%s OR expires_at < %s", (uid, now))
        cur.execute("INSERT INTO telegram_link_codes (code, user_id, expires_at) VALUES (%s,%s,%s)",
                    (code, uid, now + timedelta(minutes=CODE_TTL_MIN)))
    return code


def _ensure_alerts_on(cur, uid):
    """Connecting Telegram means the user wants signal alerts: switch them on (keeps their coin choice)."""
    cur.execute("SELECT user_id, enabled FROM signal_alert_subs WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    if row is None:
        cur.execute("""INSERT INTO signal_alert_subs (user_id, enabled, email, coins, emails_sent, updated_at)
                       VALUES (%s,1,0,'ALL',0,%s)""", (uid, _utcnow()))
    elif not row.get("enabled"):
        cur.execute("UPDATE signal_alert_subs SET enabled=1, updated_at=%s WHERE user_id=%s", (_utcnow(), uid))


def link_chat(code, chat_id, username):
    """Returns the linked user id, or None when the code is unknown or expired."""
    now = _utcnow()
    with _Cursor() as cur:
        cur.execute("SELECT user_id, expires_at FROM telegram_link_codes WHERE code=%s", (code,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("DELETE FROM telegram_link_codes WHERE code=%s", (code,))
        exp = row["expires_at"]
        if isinstance(exp, str):
            exp = datetime.fromisoformat(exp)
        if exp < now:
            return None
        uid = int(row["user_id"])
        cur.execute("DELETE FROM telegram_links WHERE chat_id=%s OR user_id=%s", (int(chat_id), uid))
        cur.execute("""INSERT INTO telegram_links (user_id, chat_id, username, enabled, sent_count, linked_at)
                       VALUES (%s,%s,%s,1,0,%s)""", (uid, int(chat_id), (username or "")[:64] or None, now))
        try:
            _ensure_alerts_on(cur, uid)
        except Exception as e:   # the signal board tables may not exist yet
            print(f"[telegram] could not switch alerts on: {e}")
    return uid


def _set_enabled_chat(chat_id, on):
    with _Cursor() as cur:
        cur.execute("UPDATE telegram_links SET enabled=%s WHERE chat_id=%s", (1 if on else 0, int(chat_id)))


def _delete_chat(chat_id):
    with _Cursor() as cur:
        cur.execute("DELETE FROM telegram_links WHERE chat_id=%s", (int(chat_id),))


# ===========================================================================
# message text
# ===========================================================================
def _fmt(x):
    x = float(x)
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def _pct(level, entry):
    return f"{(float(level) / float(entry) - 1) * 100:+.2f}%"


def _link(path):
    base = site_base()
    return f"{base}{path}" if base else ""


def signal_block(e):
    side = e["side"]
    icon = "\U0001F7E2" if side == "LONG" else "\U0001F534"
    sym = html.escape(e["symbol"])
    lines = [f"{icon} <b>New {'Long' if side == 'LONG' else 'Short'} signal: {sym}</b>",
             f"Entry: <b>{_fmt(e['entry'])}</b>",
             f"Stop loss: {_fmt(e['stop_loss'])} ({_pct(e['stop_loss'], e['entry'])})",
             f"Target: {_fmt(e['take_profit'])} ({_pct(e['take_profit'], e['entry'])})"]
    if e.get("confidence") is not None:
        lines.append(f"Win chance: {float(e['confidence']):.0f}% · ends after 8 days if neither level is hit")
    url = _link("/?coin=" + e["symbol"].replace("/", "%2F"))
    if url:
        lines.append(f'<a href="{html.escape(url)}">Open on Signals FM</a>')
    return "\n".join(lines)


def alert_text(events):
    head = "" if len(events) == 1 else f"\U0001F514 <b>{len(events)} new signals</b>\n\n"
    foot = "\n\n<i>Not financial advice. Risk a small, fixed amount on each signal, for example 1% of your account.</i>"
    return head + "\n\n".join(signal_block(e) for e in events) + foot


HELP_TEXT = ("<b>Signals FM alerts</b>\n"
             "/signals – the active signals right now\n"
             "/stop – pause alerts\n"
             "/resume – turn alerts back on\n"
             "/unlink – disconnect this chat from your account\n"
             "/help – this list")


def _not_linked_text():
    where = _link("/signals#alerts") or "the Signals board"
    return ("This chat is not connected to a Signals FM account yet.\n\n"
            f"Open {html.escape(where)}, go to <b>Signal alerts</b> and press <b>Connect Telegram</b>.")


def _signals_text():
    rows_fn = _state.get("board_rows")
    try:
        rows = rows_fn()[0] if rows_fn else []
    except Exception:
        rows = []
    active = [r for r in rows if r.get("state") == "ACTIVE"]
    if not rows:
        return "The signals board is still loading. Please try again in a minute."
    if not active:
        return ("No active signals right now. The engine is waiting for a strong setup; "
                "new signals can start at every 4-hour close.")
    out = [f"<b>Active signals ({len(active)})</b>"]
    for r in active[:12]:
        a, p = r["active"], r.get("progress") or {}
        side = "Long" if a["side"] == "LONG" else "Short"
        icon = "\U0001F7E2" if a["side"] == "LONG" else "\U0001F534"
        coin = html.escape(r["symbol"].replace("/USDT", ""))
        line = f"{icon} <b>{coin}</b> {side}"
        line += f" · entry {_fmt(a['entry'])}"
        if p.get("pct") is not None:
            line += f" · {abs(p['pct']):g}% to {'target' if p['pct'] >= 0 else 'stop'}"
        if r.get("fresh"):
            line += " · <b>new</b>"
        out.append(line)
    url = _link("/signals")
    if url:
        out.append(f'\n<a href="{html.escape(url)}">Open the signals board</a>')
    return "\n".join(out)


# ===========================================================================
# incoming updates (webhook or polling)
# ===========================================================================
def handle_update(update):
    """Processes one Telegram update. Returns the reply text it sent (for tests), or None."""
    msg = (update or {}).get("message") or {}
    chat = msg.get("chat") or {}
    if chat.get("type") != "private" or "id" not in chat:
        return None
    chat_id = chat["id"]
    text = (msg.get("text") or "").strip()
    username = (msg.get("from") or {}).get("username") or ""
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    link = None
    reply = None
    try:
        if cmd == "/start" and arg:
            uid = link_chat(arg, chat_id, username)
            if uid is None:
                reply = ("This connect link has expired or was already used.\n\n"
                         "Open Signals FM, go to <b>Signal alerts</b> and press <b>Connect Telegram</b> again.")
            else:
                reply = ("✅ <b>Connected to Signals FM.</b>\n\n"
                         "New signals for the coins you chose will arrive here right after each 4-hour close.\n\n" + HELP_TEXT)
        else:
            link = _link_of_chat(chat_id)
            if cmd in ("/start", "/help") or not cmd.startswith("/"):
                reply = (HELP_TEXT if link else _not_linked_text())
            elif not link:
                reply = _not_linked_text()
            elif cmd == "/signals":
                reply = _signals_text()
            elif cmd == "/stop":
                _set_enabled_chat(chat_id, False)
                reply = "Alerts are paused. Send /resume to turn them back on."
            elif cmd == "/resume":
                _set_enabled_chat(chat_id, True)
                reply = "Alerts are on again. New signals will arrive here."
            elif cmd == "/unlink":
                _delete_chat(chat_id)
                reply = "This chat is disconnected from your Signals FM account. You can connect again from Signal alerts."
            else:
                reply = HELP_TEXT
    except Exception:
        print(f"[telegram] update failed:\n{traceback.format_exc()}")
        reply = "Something went wrong on our side. Please try again in a minute."
    try:
        send(chat_id, reply)
    except Exception as e:
        print(f"[telegram] reply failed: {e}")
    return reply


@telegram_bp.route("/telegram/webhook", methods=["POST"])
def webhook():
    if not enabled():
        return jsonify({"ok": False}), 404
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
    if not hmac.compare_digest(got, webhook_secret()):
        return jsonify({"ok": False}), 403
    update = request.get_json(silent=True)
    if isinstance(update, dict):
        handle_update(update)
    return jsonify({"ok": True})


def ensure_webhook():
    """Points Telegram at /telegram/webhook (only changes it when needed). Returns the mode used."""
    base = site_base()
    if not base:
        return "polling"
    want = f"{base}/telegram/webhook"
    info = api("getWebhookInfo") or {}
    if info.get("url") != want:
        api("setWebhook", {"url": want, "secret_token": webhook_secret(), "allowed_updates": ["message"],
                           "drop_pending_updates": False})
        print(f"[telegram] webhook set to {want}")
    return "webhook"


def _poll_loop():
    try:
        api("deleteWebhook", {"drop_pending_updates": False})
    except Exception as e:
        print(f"[telegram] deleteWebhook failed: {e}")
    offset = None
    while True:
        try:
            payload = {"timeout": 50, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            for upd in api("getUpdates", payload, timeout=60) or []:
                offset = int(upd["update_id"]) + 1
                handle_update(upd)
        except Exception as e:
            print(f"[telegram] polling error: {e}")
            time.sleep(15)


def _start_updates():
    for attempt in range(6):
        try:
            mode = ensure_webhook()
            _state["mode"] = mode
            if mode == "polling":
                print("[telegram] no public address (SITE_URL / RAILWAY_PUBLIC_DOMAIN): polling for updates")
                _poll_loop()
            return
        except Exception as e:
            print(f"[telegram] webhook setup failed (attempt {attempt + 1}): {e}")
            time.sleep(min(300, 20 * (attempt + 1)))


# ===========================================================================
# outgoing alerts (called by signalboard.notify)
# ===========================================================================
def notify_users(per_user, events):
    """per_user: {user_id: [events for that user's coins]} for users with alerts on. Also posts to the channel."""
    if not enabled():
        return 0
    jobs = []
    if per_user:
        today = _utcnow().strftime("%Y-%m-%d")
        ids = [int(u) for u in per_user]
        try:
            with _Cursor() as cur:
                cur.execute("SELECT * FROM telegram_links WHERE enabled=1 AND user_id IN (" + ",".join(["%s"] * len(ids)) + ")",
                            tuple(ids))
                links = cur.fetchall() or []
                for ln in links:
                    sent = int(ln.get("sent_count") or 0) if ln.get("sent_day") == today else 0
                    if sent >= MAX_PER_DAY:
                        continue
                    jobs.append((ln["chat_id"], per_user.get(ln["user_id"]) or per_user.get(int(ln["user_id"]))))
                    cur.execute("UPDATE telegram_links SET sent_day=%s, sent_count=%s WHERE user_id=%s",
                                (today, sent + 1, ln["user_id"]))
        except Exception as e:
            print(f"[telegram] could not read linked chats: {e}")
    ch = channel_id()
    if ch and events:
        jobs.append((ch, events))
    jobs = [(c, ev) for c, ev in jobs if ev]
    if not jobs:
        return 0
    if _state["sync"]:
        _send_jobs(jobs)
    else:
        threading.Thread(target=_send_jobs, args=(jobs,), name="telegram-alerts", daemon=True).start()
    return len(jobs)


def _send_jobs(jobs):
    for chat_id, events in jobs:
        try:
            send(chat_id, alert_text(events))
        except TelegramError as e:
            if e.code == 403 and not str(chat_id).startswith(("@", "-")):
                try:
                    _set_enabled_chat(chat_id, False)      # the user blocked the bot
                except Exception:
                    pass
            print(f"[telegram] alert to {chat_id} failed: {e}")
        except Exception as e:
            print(f"[telegram] alert to {chat_id} failed: {e}")
        time.sleep(0 if _state["sync"] else 0.05)


# ===========================================================================
# website API (login required)
# ===========================================================================
def _uid():
    return session.get("user_id")


def _status(uid):
    out = {"ok": True, "available": enabled(), "linked": False, "enabled": False, "username": None, "bot": None}
    if not out["available"]:
        return out
    out["bot"] = bot_username() or None
    try:
        ln = _link_of_user(uid)
    except Exception:
        ln = None
    if ln:
        out.update({"linked": True, "enabled": bool(ln.get("enabled")), "username": ln.get("username")})
    return out


@telegram_bp.route("/api/telegram/status", methods=["GET"])
def api_status():
    uid = _uid()
    if not uid:
        return jsonify({"ok": False, "error": "Login required."}), 401
    return jsonify(_status(uid))


@telegram_bp.route("/api/telegram/link", methods=["POST"])
def api_link():
    uid = _uid()
    if not uid:
        return jsonify({"ok": False, "error": "Login required."}), 401
    if not enabled():
        return jsonify({"ok": False, "error": "Telegram alerts are not set up on this server yet."}), 503
    bot = bot_username()
    if not bot:
        return jsonify({"ok": False, "error": "Telegram is not reachable right now. Please try again in a minute."}), 503
    code = create_code(uid)
    return jsonify({"ok": True, "url": f"https://t.me/{bot}?start={code}", "bot": bot, "expires_in_min": CODE_TTL_MIN})


@telegram_bp.route("/api/telegram/unlink", methods=["POST"])
def api_unlink():
    uid = _uid()
    if not uid:
        return jsonify({"ok": False, "error": "Login required."}), 401
    ln = _link_of_user(uid)
    if ln:
        with _Cursor() as cur:
            cur.execute("DELETE FROM telegram_links WHERE user_id=%s", (uid,))
        try:
            send(ln["chat_id"], "This chat was disconnected from your Signals FM account.")
        except Exception:
            pass
    return jsonify({"ok": True, "linked": False})


@telegram_bp.route("/api/telegram/toggle", methods=["POST"])
def api_toggle():
    uid = _uid()
    if not uid:
        return jsonify({"ok": False, "error": "Login required."}), 401
    data = request.get_json(silent=True) or {}
    on = bool(data.get("enabled"))
    with _Cursor() as cur:
        cur.execute("UPDATE telegram_links SET enabled=%s WHERE user_id=%s", (1 if on else 0, uid))
    return jsonify({"ok": True, "enabled": on})


@telegram_bp.route("/api/telegram/test", methods=["POST"])
def api_test():
    uid = _uid()
    if not uid:
        return jsonify({"ok": False, "error": "Login required."}), 401
    ln = _link_of_user(uid)
    if not ln:
        return jsonify({"ok": False, "error": "Connect Telegram first."}), 400
    if time.time() - _last_test.get(uid, 0) < TEST_EVERY_SEC:
        return jsonify({"ok": False, "error": "Please wait a few seconds before sending another test."}), 429
    _last_test[uid] = time.time()
    try:
        send(ln["chat_id"], "✅ Test message from Signals FM. Signal alerts will arrive in this chat.")
    except TelegramError as e:
        if e.code == 403:
            return jsonify({"ok": False, "error": "Telegram says the bot is blocked in this chat. Unblock it, or connect again."}), 400
        return jsonify({"ok": False, "error": "Telegram did not accept the message. Please try again."}), 502
    except Exception:
        return jsonify({"ok": False, "error": "Telegram is not reachable right now. Please try again."}), 502
    return jsonify({"ok": True})


# ===========================================================================
# start
# ===========================================================================
def init_telegram(app=None, *, board_rows=None, start=True):
    if board_rows:
        _state["board_rows"] = board_rows
    if not enabled():
        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            print("[telegram] WARNING: TELEGRAM_BOT_TOKEN looks wrong; Telegram alerts are off")
        return False
    try:
        init_tables()
    except Exception as e:
        print(f"[telegram] WARNING: could not create tables: {e}")
    if not start:
        return True
    with _lock:
        if _state["started"]:
            return True
        _state["started"] = True
    threading.Thread(target=_start_updates, name="telegram-updates", daemon=True).start()
    return True
