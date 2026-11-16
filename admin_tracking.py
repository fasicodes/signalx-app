"""
What the site records so the admin panel (admin_panel.py) shows real numbers, and the subscription data.

Tables (created at startup, additive only, nothing existing is changed or removed):
  activity_log         important events with time and email: registrations, logins, logouts, password resets,
                       subscription and payment changes, admin actions (actor = the admin's email)
  user_presence        one row per member: last heartbeat (static/presence.js pings every 45 s while a page is
                       open), whether the tab was visible, the page, when this session started. "Online" means a
                       heartbeat within ADMIN_ONLINE_WINDOW_SEC (default 120 s), not just "logged in".
  visitor_days         one row per visitor per day: a random first-party id in the cookie sfm_vid (no personal
                       data). Bots, VISITOR_TRACKING=off and browsers that send Do-Not-Track / GPC are not counted.
  user_daily           per member per day: was active, logins, analyses (Pro terminal runs)
  subscription_plans   the plans the admin sells (set up on /admin/settings): name, price, currency, days
  subscriptions        a member's subscription: a copy of the plan, price, status pending / active / expired /
                       cancelled, purchase / start / expiry dates, first and last use and the number of uses
  payments             payments for a subscription. Revenue counts ONLY payments with status 'verified'.
Columns added to users: full_name (from Google, or set by the admin) and account_status ('active' / 'blocked').

Everything here except the subscription / payment changes is called from the request path of the normal site,
so those functions never raise: a database problem only loses one record, never a page.
"""
import hashlib
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request, session

from db import get_db_connection

tracking_bp = Blueprint("tracking", __name__)

SUB_STATUSES = ("pending", "active", "expired", "cancelled")
PAY_STATUSES = ("pending", "verified", "refunded", "rejected")
SESSION_GAP = timedelta(minutes=30)          # a heartbeat after a longer pause starts a new session
VID_COOKIE = "sfm_vid"
VID_RE = re.compile(r"^[0-9a-f]{16}$")
BOT_RE = re.compile(r"bot|crawl|spider|slurp|preview|monitor|uptime|curl|wget|python|httpx|okhttp|go-http|java/|"
                    r"headless|lighthouse|pingdom|facebookexternalhit|embedly|whatsapp|telegram|discord|scanner|"
                    r"validator|feedfetcher|playwright|puppeteer", re.I)
_lock = threading.Lock()
_mem = {"ping": {}, "visit": set(), "active": set(), "usage": {}, "sub": {}, "warn": {}}


def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def _env_int(name, default, lo, hi):
    try:
        return max(lo, min(int(os.environ.get(name, default)), hi))
    except (TypeError, ValueError):
        return default


def online_window():
    """Seconds since the last heartbeat for a member to count as online."""
    return _env_int("ADMIN_ONLINE_WINDOW_SEC", 120, 30, 3600)


def heartbeat_seconds():
    return 45


def visitor_tracking_on():
    return (os.environ.get("VISITOR_TRACKING", "on") or "on").strip().lower() not in ("0", "off", "false", "no")


def _warn(where, e):
    """One line per problem and minute, so a missing table does not flood the log."""
    key = f"{where}:{type(e).__name__}"
    now = time.time()
    with _lock:
        if now - _mem["warn"].get(key, 0) < 60:
            return
        _mem["warn"][key] = now
    print(f"[admin_tracking] {where}: {type(e).__name__}: {str(e)[:200]}")


def ip_hash(ip):
    """A short salted hash of the IP address (to tell sessions apart), never the address itself."""
    if not ip:
        return None
    salt = os.environ.get("SECRET_KEY") or "signalsfm"
    return hashlib.sha256(f"{salt}|{ip}".encode()).hexdigest()[:16]


def _dt(v):
    if v is None or isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    try:
        return datetime.strptime(str(v)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.strptime(str(v)[:10], "%Y-%m-%d")
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


# ===========================================================================
# tables
# ===========================================================================
TABLES = [
    """CREATE TABLE IF NOT EXISTS activity_log (
        id INT PRIMARY KEY AUTO_INCREMENT,
        at DATETIME NOT NULL,
        kind VARCHAR(32) NOT NULL,
        user_id INT NULL,
        email VARCHAR(254) NULL,
        actor VARCHAR(254) NULL,
        detail VARCHAR(500) NULL,
        ip_hash CHAR(16) NULL,
        INDEX idx_al_at (at),
        INDEX idx_al_kind (kind, at),
        INDEX idx_al_user (user_id, at)
    )""",
    """CREATE TABLE IF NOT EXISTS user_presence (
        user_id INT PRIMARY KEY,
        email VARCHAR(254) NULL,
        last_seen DATETIME NOT NULL,
        session_started DATETIME NOT NULL,
        path VARCHAR(200) NULL,
        visible TINYINT(1) NOT NULL DEFAULT 1,
        user_agent VARCHAR(160) NULL,
        ip_hash CHAR(16) NULL,
        ended_at DATETIME NULL,
        INDEX idx_up_seen (last_seen)
    )""",
    """CREATE TABLE IF NOT EXISTS visitor_days (
        day DATE NOT NULL,
        vid CHAR(16) NOT NULL,
        user_id INT NULL,
        first_at DATETIME NOT NULL,
        PRIMARY KEY (day, vid),
        INDEX idx_vd_vid (vid)
    )""",
    """CREATE TABLE IF NOT EXISTS user_daily (
        day DATE NOT NULL,
        user_id INT NOT NULL,
        logins INT NOT NULL DEFAULT 0,
        analyses INT NOT NULL DEFAULT 0,
        PRIMARY KEY (day, user_id),
        INDEX idx_ud_user (user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS subscription_plans (
        id INT PRIMARY KEY AUTO_INCREMENT,
        name VARCHAR(80) NOT NULL,
        price DECIMAL(12,2) NOT NULL DEFAULT 0,
        currency CHAR(3) NOT NULL DEFAULT 'USD',
        duration_days INT NOT NULL DEFAULT 30,
        description VARCHAR(300) NULL,
        active TINYINT(1) NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS subscriptions (
        id INT PRIMARY KEY AUTO_INCREMENT,
        user_id INT NOT NULL,
        plan_id INT NULL,
        plan_name VARCHAR(80) NOT NULL,
        price DECIMAL(12,2) NOT NULL DEFAULT 0,
        currency CHAR(3) NOT NULL DEFAULT 'USD',
        days INT NOT NULL DEFAULT 30,
        status VARCHAR(12) NOT NULL DEFAULT 'pending',
        purchased_at DATETIME NOT NULL,
        starts_at DATETIME NULL,
        expires_at DATETIME NULL,
        cancelled_at DATETIME NULL,
        cancel_reason VARCHAR(200) NULL,
        first_used_at DATETIME NULL,
        last_used_at DATETIME NULL,
        use_count INT NOT NULL DEFAULT 0,
        source VARCHAR(20) NOT NULL DEFAULT 'admin',
        note VARCHAR(300) NULL,
        created_by VARCHAR(254) NULL,
        updated_at DATETIME NOT NULL,
        INDEX idx_sub_user (user_id),
        INDEX idx_sub_status (status, expires_at)
    )""",
    """CREATE TABLE IF NOT EXISTS payments (
        id INT PRIMARY KEY AUTO_INCREMENT,
        subscription_id INT NULL,
        user_id INT NOT NULL,
        amount DECIMAL(12,2) NOT NULL,
        currency CHAR(3) NOT NULL DEFAULT 'USD',
        method VARCHAR(40) NULL,
        reference VARCHAR(120) NULL,
        status VARCHAR(12) NOT NULL DEFAULT 'pending',
        paid_at DATETIME NOT NULL,
        verified_at DATETIME NULL,
        verified_by VARCHAR(254) NULL,
        note VARCHAR(300) NULL,
        created_at DATETIME NOT NULL,
        INDEX idx_pay_status (status, verified_at),
        INDEX idx_pay_user (user_id),
        INDEX idx_pay_sub (subscription_id)
    )""",
]
USER_COLUMNS = [("full_name", "VARCHAR(120) NULL"), ("account_status", "VARCHAR(12) NOT NULL DEFAULT 'active'")]


def init_tables():
    """Creates the tables and adds the two users columns. Never raises (the site must start anyway)."""
    try:
        with _Cursor() as cur:
            for sql in TABLES:
                cur.execute(sql)
            for name, ddl in USER_COLUMNS:
                cur.execute(f"SHOW COLUMNS FROM users LIKE '{name}'")
                if not cur.fetchall():
                    cur.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
                    print(f"[admin_tracking] migrated: users.{name} added")
        return True
    except Exception as e:
        print(f"[admin_tracking] WARNING: tables not ready: {type(e).__name__}: {e}")
        return False


# ===========================================================================
# events (activity log)
# ===========================================================================
def log_event(kind, user_id=None, email=None, detail=None, actor=None, ip=None, at=None):
    try:
        with _Cursor() as cur:
            cur.execute("""INSERT INTO activity_log (at, kind, user_id, email, actor, detail, ip_hash)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (at or _utcnow(), str(kind)[:32], user_id, (email or None) and str(email)[:254],
                         (actor or None) and str(actor)[:254], (detail or None) and str(detail)[:500], ip_hash(ip)))
        return True
    except Exception as e:
        _warn("log_event", e)
        return False


def _client_ip():
    try:
        return (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip() or None
    except Exception:
        return None


def note_register(user_id, email, method="password"):
    log_event("register", user_id, email, f"signed up with {method}", ip=_client_ip())


def note_login(user_id, email, method="password"):
    log_event("login", user_id, email, f"logged in with {method}", ip=_client_ip())
    _bump_daily(user_id, "logins")
    presence_ping(user_id, email, path="(login)", visible=True, force=True)


def note_logout(user_id, email):
    if not user_id:
        return
    log_event("logout", user_id, email, ip=_client_ip())
    try:
        with _Cursor() as cur:
            cur.execute("UPDATE user_presence SET ended_at=%s WHERE user_id=%s", (_utcnow(), user_id))
    except Exception as e:
        _warn("note_logout", e)
    with _lock:
        _mem["ping"].pop(user_id, None)


def note_password_reset(user_id, email):
    log_event("password_reset", user_id, email, "set a new password with the email link", ip=_client_ip())


# ===========================================================================
# per-day counters, analyses
# ===========================================================================
def _bump_daily(user_id, field=None):
    """user_daily: marks the member active today and, with field ('logins' / 'analyses'), adds one."""
    if not user_id or field not in (None, "logins", "analyses"):
        return
    day = _utcnow().date().isoformat()
    if field is None:
        with _lock:
            if (day, user_id) in _mem["active"]:
                return
            if len(_mem["active"]) > 50000:
                _mem["active"].clear()
            _mem["active"].add((day, user_id))
    try:
        with _Cursor() as cur:
            if field:
                cur.execute(f"UPDATE user_daily SET {field} = {field} + 1 WHERE day=%s AND user_id=%s", (day, user_id))
                if cur.rowcount and cur.rowcount > 0:
                    return
            try:
                cur.execute("INSERT INTO user_daily (day, user_id, logins, analyses) VALUES (%s,%s,%s,%s)",
                            (day, user_id, 1 if field == "logins" else 0, 1 if field == "analyses" else 0))
            except Exception:        # the row appeared in between (another request): count on it
                if field:
                    cur.execute(f"UPDATE user_daily SET {field} = {field} + 1 WHERE day=%s AND user_id=%s", (day, user_id))
    except Exception as e:
        _warn("user_daily", e)


def count_analysis(user_id):
    """One analysis run (the Pro terminal asks /signal with run=1: opening it or pressing Run analysis)."""
    _bump_daily(user_id, "analyses")


# ===========================================================================
# presence (heartbeat) and visitors
# ===========================================================================
def presence_ping(user_id, email=None, path=None, visible=True, user_agent=None, ip=None, force=False):
    if not user_id:
        return False
    visible = bool(visible)
    path = (str(path)[:200] if path else None)
    now_ts = time.time()
    with _lock:
        last = _mem["ping"].get(user_id)
        if not force and last and now_ts - last[0] < 15 and last[1] == visible and last[2] == path:
            return True                      # the same state written less than 15 s ago
        _mem["ping"][user_id] = (now_ts, visible, path)
        if len(_mem["ping"]) > 20000:
            _mem["ping"].clear()
    now = _utcnow()
    ua = (user_agent or "")[:160] or None
    try:
        with _Cursor() as cur:
            cur.execute("SELECT last_seen, session_started, ended_at FROM user_presence WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            if row:
                last_seen = _dt(row.get("last_seen"))
                new_session = (not last_seen or now - last_seen > SESSION_GAP or row.get("ended_at") is not None)
                started = now if new_session else (_dt(row.get("session_started")) or now)
                cur.execute("""UPDATE user_presence SET email=COALESCE(%s, email), last_seen=%s, session_started=%s,
                                      path=COALESCE(%s, path), visible=%s, user_agent=COALESCE(%s, user_agent),
                                      ip_hash=COALESCE(%s, ip_hash), ended_at=NULL WHERE user_id=%s""",
                            (email, now, started, path, 1 if visible else 0, ua, ip_hash(ip), user_id))
            else:
                try:
                    cur.execute("""INSERT INTO user_presence (user_id, email, last_seen, session_started, path, visible,
                                                              user_agent, ip_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                                (user_id, email, now, now, path, 1 if visible else 0, ua, ip_hash(ip)))
                except Exception:   # created by another request a moment ago
                    cur.execute("UPDATE user_presence SET last_seen=%s, visible=%s WHERE user_id=%s",
                                (now, 1 if visible else 0, user_id))
    except Exception as e:
        _warn("presence", e)
        return False
    _bump_daily(user_id)
    return True


def record_visit(vid, user_id=None):
    """Counts a visitor once per day (and links the member when they are logged in)."""
    if not vid or not VID_RE.match(vid):
        return
    day = _utcnow().date().isoformat()
    key, ukey = (day, vid), (day, vid, user_id)
    with _lock:
        seen = key in _mem["visit"]
        if seen and (not user_id or ukey in _mem["visit"]):
            return
        if len(_mem["visit"]) > 200000:
            _mem["visit"].clear()
        _mem["visit"].add(key)
        if user_id:
            _mem["visit"].add(ukey)
    try:
        with _Cursor() as cur:
            try:
                cur.execute("INSERT INTO visitor_days (day, vid, user_id, first_at) VALUES (%s,%s,%s,%s)",
                            (day, vid, user_id, _utcnow()))
            except Exception:        # already counted today: only link the member
                if user_id:
                    cur.execute("UPDATE visitor_days SET user_id=%s WHERE day=%s AND vid=%s AND user_id IS NULL",
                                (user_id, day, vid))
    except Exception as e:
        _warn("visitor", e)


_SKIP_PREFIXES = ("/static/", "/api/", "/admin", "/media/", "/telegram/", "/strategies/image/")
_SKIP_PATHS = {"/robots.txt", "/sitemap.xml", "/favicon.ico", "/glossary.json"}


def after_request(resp):
    """Called from main.py for every response: visitor counting on page views (sets the sfm_vid cookie), the
    member's presence on page views and subscription use on member requests. Never raises."""
    try:
        path = request.path or "/"
        uid = session.get("user_id")
        if uid and not path.startswith(("/static/", "/api/admin", "/admin", "/api/presence", "/api/logout", "/api/login",
                                        "/api/auth/")):
            subscription_usage(uid)
        if request.method != "GET" or resp.status_code != 200 or resp.mimetype != "text/html":
            return resp
        if path.startswith(_SKIP_PREFIXES) or path in _SKIP_PATHS:
            return resp
        if uid:
            presence_ping(uid, session.get("email"), path=path, visible=True,
                          user_agent=request.headers.get("User-Agent"), ip=_client_ip())
        ua = request.headers.get("User-Agent") or ""
        if (not visitor_tracking_on() or not ua or BOT_RE.search(ua)
                or request.headers.get("DNT") == "1" or request.headers.get("Sec-GPC") == "1"):
            return resp
        vid = request.cookies.get(VID_COOKIE) or ""
        if not VID_RE.match(vid):
            vid = secrets.token_hex(8)
            resp.set_cookie(VID_COOKIE, vid, max_age=400 * 86400, secure=request.is_secure, httponly=True, samesite="Lax")
        record_visit(vid, uid)
    except Exception as e:
        _warn("after_request", e)
    return resp


@tracking_bp.route("/api/presence", methods=["POST"])
def api_presence():
    """Heartbeat from static/presence.js while a member has a page open (visible=false: a background tab)."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    path = str(data.get("path") or "")[:200]
    if not path.startswith("/"):
        path = None
    ok = presence_ping(uid, session.get("email"), path=path, visible=data.get("visible") is not False,
                       user_agent=request.headers.get("User-Agent"), ip=_client_ip())
    return jsonify({"ok": bool(ok), "every": heartbeat_seconds()})


# ===========================================================================
# subscriptions
# ===========================================================================
class SubError(ValueError):
    """A subscription / payment change that is not allowed; the message is shown to the admin."""


def _money(v, name="Amount"):
    try:
        x = round(float(str(v).replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        raise SubError(f"{name} must be a number.")
    if x < 0 or x > 10_000_000:
        raise SubError(f"{name} must be between 0 and 10,000,000.")
    return x


def _currency(v, default="USD"):
    c = str(v or default).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", c):
        raise SubError("Currency must be a three-letter code such as USD or PKR.")
    return c


def _days(v, default=None):
    try:
        d = int(v if v not in (None, "") else default)
    except (TypeError, ValueError):
        raise SubError("Days must be a whole number.")
    if d < 1 or d > 3660:
        raise SubError("Days must be between 1 and 3660.")
    return d


def _text(v, limit):
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(v or "")).strip()
    return s[:limit] or None


def list_plans(active_only=False):
    with _Cursor() as cur:
        cur.execute("SELECT * FROM subscription_plans" + (" WHERE active=1" if active_only else "") + " ORDER BY active DESC, price ASC, id ASC")
        return cur.fetchall() or []


def get_plan(plan_id):
    with _Cursor() as cur:
        cur.execute("SELECT * FROM subscription_plans WHERE id=%s", (plan_id,))
        return cur.fetchone()


def save_plan(data, actor, plan_id=None):
    updating = bool(plan_id)
    name = _text(data.get("name"), 80)
    if not name or len(name) < 2:
        raise SubError("Give the plan a name.")
    price = _money(data.get("price"), "Price")
    cur_ = _currency(data.get("currency"))
    days = _days(data.get("duration_days"), 30)
    desc = _text(data.get("description"), 300)
    active = 0 if str(data.get("active", "1")).lower() in ("0", "false", "off", "no") else 1
    now = _utcnow()
    with _Cursor() as cur:
        if plan_id:
            cur.execute("SELECT id FROM subscription_plans WHERE id=%s", (plan_id,))
            if not cur.fetchone():
                raise SubError("That plan does not exist.")
            cur.execute("""UPDATE subscription_plans SET name=%s, price=%s, currency=%s, duration_days=%s, description=%s,
                                  active=%s, updated_at=%s WHERE id=%s""", (name, price, cur_, days, desc, active, now, plan_id))
        else:
            cur.execute("""INSERT INTO subscription_plans (name, price, currency, duration_days, description, active,
                                                          created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (name, price, cur_, days, desc, active, now, now))
            plan_id = cur.lastrowid
    log_event("plan_updated" if updating else "plan_created", detail=f"{name}: {price:.2f} {cur_} for {days} days"
              + ("" if active else " (hidden)"), actor=actor)
    return get_plan(plan_id)


def get_subscription(sid):
    with _Cursor() as cur:
        cur.execute("""SELECT s.*, u.email FROM subscriptions s LEFT JOIN users u ON u.id = s.user_id WHERE s.id=%s""", (sid,))
        return cur.fetchone()


def _payment_row(cur, sub_id, user_id, pay, actor):
    amount = _money(pay.get("amount"))
    status = str(pay.get("status") or "verified").lower()
    if status not in PAY_STATUSES:
        raise SubError("Unknown payment status.")
    now = _utcnow()
    cur.execute("""INSERT INTO payments (subscription_id, user_id, amount, currency, method, reference, status, paid_at,
                                         verified_at, verified_by, note, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (sub_id, user_id, amount, _currency(pay.get("currency")), _text(pay.get("method"), 40),
                 _text(pay.get("reference"), 120), status, now, now if status == "verified" else None,
                 actor if status == "verified" else None, _text(pay.get("note"), 300), now))
    return cur.lastrowid, amount, status


def create_subscription(user_id, plan_id, actor, status="active", days=None, price=None, currency=None, note=None,
                        payment=None, starts_at=None):
    """A subscription for a member, from one of the plans: 'active' starts now (or starts_at) for the plan's days,
    'pending' waits for payment / activation. payment: optional {amount, currency, method, reference, status}."""
    plan = get_plan(plan_id) if plan_id else None
    if not plan:
        raise SubError("Choose a plan (create plans on the Settings page).")
    if status not in ("active", "pending"):
        raise SubError("A new subscription is either active or pending.")
    days = _days(days, plan["duration_days"])
    price = _money(price if price not in (None, "") else plan["price"], "Price")
    cur_ = _currency(currency or plan["currency"])
    now = _utcnow()
    start = _dt(starts_at) if starts_at else now
    if not start:
        raise SubError("The start date is not a valid date.")
    with _Cursor() as cur:
        cur.execute("SELECT id, email FROM users WHERE id=%s", (user_id,))
        user = cur.fetchone()
        if not user:
            raise SubError("That member does not exist.")
        cur.execute("""INSERT INTO subscriptions (user_id, plan_id, plan_name, price, currency, days, status, purchased_at,
                                                  starts_at, expires_at, source, note, created_by, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'admin',%s,%s,%s)""",
                    (user_id, plan["id"], plan["name"], price, cur_, days, status, now,
                     start if status == "active" else None, start + timedelta(days=days) if status == "active" else None,
                     _text(note, 300), actor, now))
        sid = cur.lastrowid
        pay_txt = ""
        if payment and str(payment.get("amount") or "").strip() != "":
            payment = dict(payment, currency=payment.get("currency") or cur_)
            _pid, amount, pstatus = _payment_row(cur, sid, user_id, payment, actor)
            pay_txt = f", payment {amount:.2f} {payment['currency']} {pstatus}"
    with _lock:
        _mem["sub"].pop(user_id, None)
    log_event("subscription_new", user_id, user["email"],
              f"{plan['name']} ({days} days, {price:.2f} {cur_}) {status}{pay_txt}", actor=actor)
    return get_subscription(sid)


def set_subscription_status(sid, status, actor, reason=None):
    if status not in SUB_STATUSES:
        raise SubError("Unknown status.")
    sub = get_subscription(sid)
    if not sub:
        raise SubError("That subscription does not exist.")
    old = sub["status"]
    if old == status:
        return sub
    now = _utcnow()
    expires = _dt(sub.get("expires_at"))
    with _Cursor() as cur:
        if status == "active":
            if not expires or expires <= now:            # (re)start a full period from now
                cur.execute("UPDATE subscriptions SET status='active', starts_at=%s, expires_at=%s, cancelled_at=NULL, "
                            "cancel_reason=NULL, updated_at=%s WHERE id=%s",
                            (now, now + timedelta(days=int(sub.get("days") or 30)), now, sid))
            else:
                cur.execute("UPDATE subscriptions SET status='active', cancelled_at=NULL, cancel_reason=NULL, updated_at=%s "
                            "WHERE id=%s", (now, sid))
        elif status == "cancelled":
            cur.execute("UPDATE subscriptions SET status='cancelled', cancelled_at=%s, cancel_reason=%s, updated_at=%s WHERE id=%s",
                        (now, _text(reason, 200), now, sid))
        elif status == "expired":
            cur.execute("UPDATE subscriptions SET status='expired', expires_at=%s, updated_at=%s WHERE id=%s",
                        (min(expires, now) if expires else now, now, sid))
        else:                                             # back to pending
            cur.execute("UPDATE subscriptions SET status='pending', updated_at=%s WHERE id=%s", (now, sid))
    with _lock:
        _mem["sub"].pop(sub["user_id"], None)
    kind = {"active": "subscription_activated", "cancelled": "subscription_cancelled", "expired": "subscription_expired",
            "pending": "subscription_pending"}[status]
    log_event(kind, sub["user_id"], sub.get("email"), f"{sub['plan_name']}: {old} -> {status}"
              + (f" ({_text(reason, 200)})" if reason else ""), actor=actor)
    return get_subscription(sid)


def extend_subscription(sid, days, actor):
    days = _days(days)
    sub = get_subscription(sid)
    if not sub:
        raise SubError("That subscription does not exist.")
    if sub["status"] in ("pending", "cancelled"):
        raise SubError("Activate the subscription first, then extend it.")
    now = _utcnow()
    base = max(_dt(sub.get("expires_at")) or now, now)
    with _Cursor() as cur:
        cur.execute("UPDATE subscriptions SET status='active', expires_at=%s, starts_at=COALESCE(starts_at, %s), updated_at=%s "
                    "WHERE id=%s", (base + timedelta(days=days), now, now, sid))
    with _lock:
        _mem["sub"].pop(sub["user_id"], None)
    log_event("subscription_extended", sub["user_id"], sub.get("email"), f"{sub['plan_name']}: +{days} days", actor=actor)
    return get_subscription(sid)


def add_payment(sid, data, actor):
    sub = get_subscription(sid)
    if not sub:
        raise SubError("That subscription does not exist.")
    with _Cursor() as cur:
        pid, amount, status = _payment_row(cur, sid, sub["user_id"], dict(data, currency=data.get("currency") or sub["currency"]), actor)
    log_event("payment_" + ("verified" if status == "verified" else "recorded"), sub["user_id"], sub.get("email"),
              f"{amount:.2f} {_currency(data.get('currency') or sub['currency'])} for {sub['plan_name']} ({status})", actor=actor)
    return pid


def set_payment_status(pid, status, actor):
    if status not in PAY_STATUSES:
        raise SubError("Unknown payment status.")
    with _Cursor() as cur:
        cur.execute("SELECT p.*, u.email FROM payments p LEFT JOIN users u ON u.id = p.user_id WHERE p.id=%s", (pid,))
        p = cur.fetchone()
        if not p:
            raise SubError("That payment does not exist.")
        if p["status"] == status:
            return p
        now = _utcnow()
        if status == "verified":
            cur.execute("UPDATE payments SET status='verified', verified_at=%s, verified_by=%s WHERE id=%s", (now, actor, pid))
        else:
            cur.execute("UPDATE payments SET status=%s WHERE id=%s", (status, pid))
    log_event("payment_" + status, p["user_id"], p.get("email"),
              f"{float(p['amount']):.2f} {p['currency']}: {p['status']} -> {status}", actor=actor)
    return p


def expire_due(now=None):
    """Active subscriptions whose expiry has passed become 'expired'; the log entry carries the real expiry time."""
    now = now or _utcnow()
    try:
        with _Cursor() as cur:
            cur.execute("""SELECT s.id, s.user_id, s.plan_name, s.expires_at, u.email FROM subscriptions s
                           LEFT JOIN users u ON u.id = s.user_id
                           WHERE s.status='active' AND s.expires_at IS NOT NULL AND s.expires_at <= %s""", (now,))
            due = cur.fetchall() or []
            for s in due:
                cur.execute("UPDATE subscriptions SET status='expired', updated_at=%s WHERE id=%s AND status='active'",
                            (now, s["id"]))
        for s in due:
            with _lock:
                _mem["sub"].pop(s["user_id"], None)
            log_event("subscription_expired", s["user_id"], s.get("email"), f"{s['plan_name']} ended",
                      at=_dt(s.get("expires_at")) or now)
        return len(due)
    except Exception as e:
        _warn("expire_due", e)
        return 0


def active_subscription(user_id):
    """The member's running subscription (or None), cached for a minute."""
    now_ts = time.time()
    with _lock:
        hit = _mem["sub"].get(user_id)
    if hit and now_ts - hit[0] < 60:
        return hit[1]
    sub = None
    try:
        with _Cursor() as cur:
            cur.execute("""SELECT id, plan_name, expires_at, first_used_at FROM subscriptions
                           WHERE user_id=%s AND status='active' AND expires_at > %s ORDER BY expires_at DESC LIMIT 1""",
                        (user_id, _utcnow()))
            sub = cur.fetchone()
    except Exception as e:
        _warn("active_subscription", e)
    with _lock:
        if len(_mem["sub"]) > 20000:
            _mem["sub"].clear()
        _mem["sub"][user_id] = (now_ts, sub)
    return sub


def subscription_usage(user_id):
    """The member used the site while subscribed: first / last use and one more use (at most every 10 minutes)."""
    if not user_id:
        return
    now_ts = time.time()
    with _lock:
        if now_ts - _mem["usage"].get(user_id, 0) < 600:
            return
        _mem["usage"][user_id] = now_ts
        if len(_mem["usage"]) > 20000:
            _mem["usage"].clear()
    sub = active_subscription(user_id)
    if not sub:
        return
    now = _utcnow()
    try:
        with _Cursor() as cur:
            cur.execute("""UPDATE subscriptions SET first_used_at=COALESCE(first_used_at, %s), last_used_at=%s,
                                  use_count=use_count+1 WHERE id=%s""", (now, now, sub["id"]))
    except Exception as e:
        _warn("subscription_usage", e)
