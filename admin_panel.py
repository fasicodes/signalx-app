"""
Admin panel for the site owner: /admin (dashboard), /admin/users (+ /admin/users/<id>), /admin/subscriptions,
/admin/analytics, /admin/trades, /admin/activity and /admin/settings, with their JSON API under /api/admin/.
The numbers come from the site's own database (admin_tracking.py records what the old tables did not have).

Who may open it - checked on the server for every page and every API call, never only in the browser:
  * the session's email is in ADMIN_EMAILS (Railway variable, comma separated), and
  * that account exists in the database with the same email, a confirmed email address and is not suspended, and
  * with ADMIN_REQUIRE_GOOGLE=on, the session was started with "Continue with Google".
Anyone else gets the normal "Page not found" (pages) or a 404 JSON answer (API), so the panel is not revealed.
Every change (POST) also needs the page's CSRF token in the X-CSRF-Token header, and is written to activity_log
with the admin's email. The trading code is only read here, never changed.
"""
import csv
import io
import json
import os
import re
import secrets
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import wraps

from flask import Blueprint, Response, abort, g, jsonify, redirect, render_template, request, session

import admin_tracking as T
from db import get_db_connection

admin_bp = Blueprint("admin_panel", __name__)

PER_PAGE = 25
ACTIVITY_GROUPS = {
    "registrations": ("kind = 'register'", ()),
    "logins": ("kind IN ('login', 'logout')", ()),
    "subscriptions": ("kind LIKE 'subscription%%'", ()),     # %% : the queries always go through pymysql's % formatting
    "payments": ("kind LIKE 'payment%%'", ()),
    "account": ("kind IN ('password_reset', 'user_blocked', 'user_unblocked', 'user_logged_out', 'user_renamed')", ()),
    "admin": ("actor IS NOT NULL", ()),
}
_cache = {}
_lock = threading.Lock()


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


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    with _lock:
        if len(_cache) > 200:
            _cache.clear()
        _cache[key] = (now, val)
    return val


def _forget(prefix):
    with _lock:
        for k in [k for k in _cache if isinstance(k, tuple) and k and k[0] == prefix]:
            _cache.pop(k, None)


# ===========================================================================
# who is the admin
# ===========================================================================
def admin_emails():
    return sorted({e.strip().lower() for e in (os.environ.get("ADMIN_EMAILS") or "").split(",") if e.strip()})


def require_google():
    return (os.environ.get("ADMIN_REQUIRE_GOOGLE", "off") or "off").strip().lower() in ("1", "on", "true", "yes")


def current_admin():
    """The admin for this request ({id, email, name}) or None. A database problem means "no" (fail closed)."""
    uid = session.get("user_id")
    email = (session.get("email") or "").strip().lower()
    if not uid or not email or email not in admin_emails():
        return None
    if require_google() and session.get("login_method") != "google":
        return None
    key = ("admin", uid, email)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < 30:
        return hit[1]
    try:
        with _Cursor() as cur:
            cur.execute("SELECT id, email, email_verified, account_status, full_name FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
    except Exception as e:
        print(f"[admin] admin check failed: {type(e).__name__}: {e}")
        return None
    ok = bool(row and (row.get("email") or "").strip().lower() == email and row.get("email_verified")
              and (row.get("account_status") or "active") != "blocked")
    admin = {"id": uid, "email": email, "name": row.get("full_name") if row else None} if ok else None
    with _lock:
        _cache[key] = (now, admin)
    return admin


def _csrf():
    tok = session.get("admin_csrf")
    if not tok:
        tok = session["admin_csrf"] = secrets.token_urlsafe(32)
    return tok


def admin_page(fn):
    @wraps(fn)
    def wrap(*a, **k):
        if not session.get("user_id"):
            return redirect("/login")
        admin = current_admin()
        if not admin:
            abort(404)
        g.admin = admin
        return fn(*a, **k)
    return wrap


def admin_api(fn):
    @wraps(fn)
    def wrap(*a, **k):
        admin = current_admin()
        if not admin:
            return jsonify({"ok": False, "error": "Not found."}), 404
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            tok, sent = session.get("admin_csrf"), request.headers.get("X-CSRF-Token") or ""
            if not tok or not secrets.compare_digest(str(tok), str(sent)):
                return jsonify({"ok": False, "error": "This page is out of date. Reload it and try again."}), 403
        g.admin = admin
        try:
            return fn(*a, **k)
        except T.SubError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:
            print(f"[admin] {request.path}: {type(e).__name__}: {e}")
            traceback.print_exc()
            return jsonify({"ok": False, "error": "Something went wrong on the server. Please try again."}), 500
    return wrap


# ===========================================================================
# formatting
# ===========================================================================
def _iso(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.replace(microsecond=0).isoformat() + "Z"
    if isinstance(v, date):
        return v.isoformat()
    d = T._dt(v)
    if d and len(str(v)) > 10:
        return d.isoformat() + "Z"
    return str(v)[:10]


def _num(v, nd=2):
    if v is None:
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


_DT_STR = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


def _row(r, dates=(), nums=()):
    """A database row for JSON: times as ISO UTC ("...Z"), DECIMAL as numbers."""
    out = {}
    for k, v in (r or {}).items():
        if k in dates:
            out[k] = _iso(v)
        elif k in nums or isinstance(v, Decimal):
            out[k] = _num(v, 8)
        elif isinstance(v, (datetime, date)) or (isinstance(v, str) and _DT_STR.match(v) and not v.endswith("Z")):
            out[k] = _iso(v)
        else:
            out[k] = v
    return out


def _int_arg(name, default, lo, hi):
    try:
        return max(lo, min(int(request.args.get(name, default)), hi))
    except (TypeError, ValueError):
        return default


def _q():
    return re.sub(r"[\x00-\x1f%]", "", (request.args.get("q") or "").strip().lower())[:120]


def _scalar(cur, sql, args=()):
    cur.execute(sql, args)
    r = cur.fetchone() or {}
    v = next(iter(r.values()), 0) if r else 0
    return v or 0


def _safe(fn, default=None):
    """Optional data from another feature's tables (trades, bot, demo): None when that table is not there."""
    try:
        return fn()
    except Exception as e:
        print(f"[admin] optional data skipped: {type(e).__name__}: {str(e)[:120]}")
        return default


def main_currency(totals=None):
    c = (os.environ.get("ADMIN_CURRENCY") or "").strip().upper()
    if re.fullmatch(r"[A-Z]{3}", c):
        return c
    if totals:
        return max(totals, key=lambda k: totals[k])
    return "USD"


def _days(n, end=None):
    end = end or _utcnow().date()
    return [(end - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _series(rows, days, key="d", val="n"):
    m = {str(r[key])[:10]: (r.get(val) or 0) for r in rows}
    return [float(m.get(d, 0) or 0) for d in days]


def _pct(cur, prev):
    if not prev:
        return None if not cur else 100.0
    return round((cur - prev) / prev * 100, 1)


# ===========================================================================
# pages
# ===========================================================================
PAGES = {"dashboard": "Dashboard", "users": "Users", "subscriptions": "Subscriptions", "analytics": "Analytics",
         "trades": "Trade history", "activity": "Activity logs", "settings": "Settings"}


def _render(page, **extra):
    last_login = _safe(lambda: _last_login(g.admin["id"]))
    return render_template(f"admin/{page}.html", page=page, page_title=PAGES.get(page, "Admin"), admin=g.admin,
                           csrf_token=_csrf(), last_login=last_login, currency=main_currency(), **extra)


def _last_login(uid):
    with _Cursor() as cur:
        cur.execute("SELECT at FROM activity_log WHERE user_id=%s AND kind='login' ORDER BY id DESC LIMIT 1 OFFSET 1", (uid,))
        r = cur.fetchone()
    return _iso(r["at"]) if r else None


@admin_bp.route("/admin", methods=["GET"])
@admin_page
def page_dashboard():
    return _render("dashboard")


@admin_bp.route("/admin/users", methods=["GET"])
@admin_page
def page_users():
    return _render("users")


@admin_bp.route("/admin/users/<int:uid>", methods=["GET"])
@admin_page
def page_user(uid):
    return _render("user", user_id=uid)


@admin_bp.route("/admin/subscriptions", methods=["GET"])
@admin_page
def page_subscriptions():
    return _render("subscriptions")


@admin_bp.route("/admin/analytics", methods=["GET"])
@admin_page
def page_analytics():
    return _render("analytics")


@admin_bp.route("/admin/trades", methods=["GET"])
@admin_page
def page_trades():
    return _render("trades")


@admin_bp.route("/admin/activity", methods=["GET"])
@admin_page
def page_activity():
    return _render("activity")


@admin_bp.route("/admin/settings", methods=["GET"])
@admin_page
def page_settings():
    return _render("settings")


# ===========================================================================
# dashboard
# ===========================================================================
def overview():
    T.expire_due()
    now = _utcnow()
    days14 = _days(14)
    since14 = days14[0]
    win = now - timedelta(seconds=T.online_window())
    out = {}
    with _Cursor() as cur:
        out["users_total"] = int(_scalar(cur, "SELECT COUNT(*) AS n FROM users"))
        reg7 = int(_scalar(cur, "SELECT COUNT(*) AS n FROM users WHERE created_at >= %s", (now - timedelta(days=7),)))
        reg_prev = int(_scalar(cur, "SELECT COUNT(*) AS n FROM users WHERE created_at >= %s AND created_at < %s",
                               (now - timedelta(days=14), now - timedelta(days=7))))
        cur.execute("SELECT status, COUNT(*) AS n FROM subscriptions GROUP BY status")
        by_status = {r["status"]: int(r["n"]) for r in cur.fetchall() or []}
        active = int(_scalar(cur, "SELECT COUNT(*) AS n FROM subscriptions WHERE status='active' AND expires_at > %s", (now,)))
        never = int(_scalar(cur, "SELECT COUNT(*) AS n FROM subscriptions WHERE status IN ('active','expired') "
                                 "AND first_used_at IS NULL"))
        online = int(_scalar(cur, "SELECT COUNT(*) AS n FROM user_presence WHERE last_seen >= %s AND ended_at IS NULL", (win,)))
        visitors_total = int(_scalar(cur, "SELECT COUNT(DISTINCT vid) AS n FROM visitor_days"))
        v7 = int(_scalar(cur, "SELECT COUNT(DISTINCT vid) AS n FROM visitor_days WHERE day >= %s",
                         ((now.date() - timedelta(days=6)).isoformat(),)))
        v_prev = int(_scalar(cur, "SELECT COUNT(DISTINCT vid) AS n FROM visitor_days WHERE day >= %s AND day < %s",
                             ((now.date() - timedelta(days=13)).isoformat(), (now.date() - timedelta(days=6)).isoformat())))
        cur.execute("SELECT currency, SUM(amount) AS total, COUNT(*) AS n FROM payments WHERE status='verified' GROUP BY currency")
        rev = {r["currency"]: float(r["total"] or 0) for r in cur.fetchall() or []}
        cur_main = main_currency(rev)
        rev7 = float(_scalar(cur, "SELECT SUM(amount) AS t FROM payments WHERE status='verified' AND currency=%s "
                                  "AND verified_at >= %s", (cur_main, now - timedelta(days=7))) or 0)
        rev_prev = float(_scalar(cur, "SELECT SUM(amount) AS t FROM payments WHERE status='verified' AND currency=%s "
                                      "AND verified_at >= %s AND verified_at < %s",
                                 (cur_main, now - timedelta(days=14), now - timedelta(days=7))) or 0)
        cur.execute("SELECT DATE(created_at) AS d, COUNT(*) AS n FROM users WHERE created_at >= %s GROUP BY DATE(created_at)",
                    (since14,))
        s_reg = _series(cur.fetchall() or [], days14)
        cur.execute("SELECT day AS d, COUNT(DISTINCT vid) AS n FROM visitor_days WHERE day >= %s GROUP BY day", (since14,))
        s_vis = _series(cur.fetchall() or [], days14)
        cur.execute("SELECT DATE(verified_at) AS d, SUM(amount) AS n FROM payments WHERE status='verified' AND currency=%s "
                    "AND verified_at >= %s GROUP BY DATE(verified_at)", (cur_main, since14))
        s_rev = _series(cur.fetchall() or [], days14)
        cur.execute("SELECT DATE(purchased_at) AS d, COUNT(*) AS n FROM subscriptions WHERE purchased_at >= %s "
                    "AND status <> 'pending' GROUP BY DATE(purchased_at)", (since14,))
        s_sub = _series(cur.fetchall() or [], days14)
        cur.execute("SELECT day AS d, COUNT(*) AS n FROM user_daily WHERE day >= %s GROUP BY day", (since14,))
        s_dau = _series(cur.fetchall() or [], days14)
        cur.execute("SELECT * FROM activity_log ORDER BY at DESC, id DESC LIMIT 8")
        recent = [_row(r) for r in cur.fetchall() or []]
    for r in recent:
        r.pop("ip_hash", None)
    out.update({
        "registered_7d": reg7, "registered_change": _pct(reg7, reg_prev),
        "subscriptions_active": active, "subscriptions_expired": by_status.get("expired", 0),
        "subscriptions_pending": by_status.get("pending", 0), "subscriptions_cancelled": by_status.get("cancelled", 0),
        "subscriptions_total": sum(by_status.values()), "subscriptions_never_used": never,
        "online": online, "online_window_sec": T.online_window(),
        "visitors_total": visitors_total, "visitors_7d": v7, "visitors_change": _pct(v7, v_prev),
        "revenue": rev, "currency": cur_main, "revenue_main": rev.get(cur_main, 0.0), "revenue_7d": rev7,
        "revenue_change": _pct(rev7, rev_prev),
        "series": {"days": days14, "registrations": s_reg, "visitors": s_vis, "revenue": s_rev, "subscriptions": s_sub,
                   "active_users": s_dau},
        "recent": recent, "visitor_tracking": T.visitor_tracking_on(), "generated_at": _iso(now),
    })
    return out


@admin_bp.route("/api/admin/overview", methods=["GET"])
@admin_api
def api_overview():
    return jsonify({"ok": True, **overview()})


# ===========================================================================
# analytics
# ===========================================================================
def _week_start(d):
    return (d - timedelta(days=d.weekday())).isoformat()


def analytics(days):
    now = _utcnow()
    dlist = _days(days)
    since = dlist[0]
    with _Cursor() as cur:
        cur.execute("SELECT day AS d, COUNT(DISTINCT vid) AS n FROM visitor_days WHERE day >= %s GROUP BY day", (since,))
        s_vis = _series(cur.fetchall() or [], dlist)
        cur.execute("SELECT day, vid FROM visitor_days WHERE day >= %s LIMIT 400000", (since,))
        pairs = cur.fetchall() or []
        cur.execute("SELECT DATE(created_at) AS d, COUNT(*) AS n FROM users WHERE created_at >= %s GROUP BY DATE(created_at)",
                    (since,))
        s_reg = _series(cur.fetchall() or [], dlist)
        cur.execute("SELECT DATE(purchased_at) AS d, COUNT(*) AS n FROM subscriptions WHERE purchased_at >= %s "
                    "AND status <> 'pending' GROUP BY DATE(purchased_at)", (since,))
        s_sub = _series(cur.fetchall() or [], dlist)
        cur.execute("SELECT DATE(verified_at) AS d, currency, SUM(amount) AS n FROM payments WHERE status='verified' "
                    "AND verified_at >= %s GROUP BY DATE(verified_at), currency", (since,))
        rev_rows = cur.fetchall() or []
        cur.execute("SELECT day AS d, COUNT(*) AS dau, SUM(logins) AS logins, SUM(analyses) AS analyses FROM user_daily "
                    "WHERE day >= %s GROUP BY day", (since,))
        act = cur.fetchall() or []
        visitors_range = int(_scalar(cur, "SELECT COUNT(DISTINCT vid) AS n FROM visitor_days WHERE day >= %s", (since,)))
    totals = {}
    for r in rev_rows:
        totals[r["currency"]] = totals.get(r["currency"], 0.0) + float(r["n"] or 0)
    cur_main = main_currency(totals or None)
    s_rev = _series([r for r in rev_rows if r["currency"] == cur_main], dlist)
    weeks, months = {}, {}
    for r in pairs:
        d = T._dt(r["day"])
        if not d:
            continue
        weeks.setdefault(_week_start(d.date()), set()).add(r["vid"])
        months.setdefault(d.strftime("%Y-%m"), set()).add(r["vid"])
    return {
        "range": days, "days": dlist, "generated_at": _iso(now),
        "visitors": s_vis, "registrations": s_reg, "subscriptions": s_sub, "revenue": s_rev, "currency": cur_main,
        "revenue_by_currency": {k: round(v, 2) for k, v in totals.items()},
        "active_users": _series(act, dlist, val="dau"), "logins": _series(act, dlist, val="logins"),
        "analyses": _series(act, dlist, val="analyses"),
        "weekly_visitors": [{"week": k, "n": len(v)} for k, v in sorted(weeks.items())],
        "monthly_visitors": [{"month": k, "n": len(v)} for k, v in sorted(months.items())],
        "totals": {"visitors": visitors_range, "registrations": int(sum(s_reg)), "subscriptions": int(sum(s_sub)),
                   "revenue": round(sum(s_rev), 2), "logins": int(sum(_series(act, dlist, val="logins"))),
                   "analyses": int(sum(_series(act, dlist, val="analyses")))},
    }


@admin_bp.route("/api/admin/analytics", methods=["GET"])
@admin_api
def api_analytics():
    days = {"7": 7, "30": 30, "90": 90, "365": 365, "1y": 365}.get((request.args.get("range") or "30").lower(), 30)
    return jsonify({"ok": True, **_cached(("analytics", days), 60, lambda: analytics(days))})


# ===========================================================================
# users
# ===========================================================================
USER_COLS = ("u.id, u.email, u.full_name, u.created_at, u.account_status, u.email_verified, u.auth_provider, "
             "p.last_seen, p.ended_at")


def _sub_summary(cur, ids):
    if not ids:
        return {}
    marks = ",".join(["%s"] * len(ids))
    cur.execute(f"""SELECT user_id, id, status, plan_name, expires_at, first_used_at FROM subscriptions
                    WHERE user_id IN ({marks}) ORDER BY id DESC""", tuple(ids))
    out = {}
    for r in cur.fetchall() or []:
        s = out.setdefault(r["user_id"], {"current": None, "count": 0, "ever_used": False})
        s["count"] += 1
        s["ever_used"] = s["ever_used"] or bool(r.get("first_used_at"))
        if s["current"] is None or (s["current"]["status"] != "active" and r["status"] == "active"):
            s["current"] = {"id": r["id"], "status": r["status"], "plan": r["plan_name"], "expires_at": _iso(r["expires_at"])}
    return out


def _analyses(cur, ids):
    if not ids:
        return {}
    marks = ",".join(["%s"] * len(ids))
    cur.execute(f"SELECT user_id, SUM(analyses) AS a FROM user_daily WHERE user_id IN ({marks}) GROUP BY user_id", tuple(ids))
    return {r["user_id"]: int(r["a"] or 0) for r in cur.fetchall() or []}


def _online_status(last_seen, ended_at, visible=True):
    ls = T._dt(last_seen)
    if not ls:
        return "never"
    if ended_at:
        return "logged out"
    age = (_utcnow() - ls).total_seconds()
    if age <= T.online_window():
        return "online" if visible else "idle"
    return "offline"


def list_users(q="", status="", page=1, per=PER_PAGE):
    where, args = ["1=1"], []
    if q:
        where.append("(u.email LIKE %s OR u.full_name LIKE %s)")
        args += [f"%{q}%", f"%{q}%"]
    if status in ("active", "blocked"):
        where.append("COALESCE(u.account_status, 'active') = %s")
        args.append(status)
    w = " AND ".join(where)
    with _Cursor() as cur:
        total = int(_scalar(cur, f"SELECT COUNT(*) AS n FROM users u WHERE {w}", tuple(args)))
        cur.execute(f"SELECT {USER_COLS} FROM users u LEFT JOIN user_presence p ON p.user_id = u.id WHERE {w} "
                    "ORDER BY u.id DESC LIMIT %s OFFSET %s", tuple(args) + (per, (page - 1) * per))
        rows = cur.fetchall() or []
        ids = [r["id"] for r in rows]
        subs = _sub_summary(cur, ids)
        an = _analyses(cur, ids)
    users = []
    for r in rows:
        u = _row(r)
        u["account_status"] = u.get("account_status") or "active"
        u["presence"] = _online_status(r.get("last_seen"), r.get("ended_at"))
        u["subscription"] = (subs.get(r["id"]) or {}).get("current")
        u["ever_used"] = (subs.get(r["id"]) or {}).get("ever_used", False)
        u["analyses"] = an.get(r["id"], 0)
        users.append(u)
    return {"users": users, "total": total, "page": page, "pages": max(1, -(-total // per))}


@admin_bp.route("/api/admin/users", methods=["GET"])
@admin_api
def api_users():
    return jsonify({"ok": True, **list_users(_q(), request.args.get("status") or "", _int_arg("page", 1, 1, 100000))})


def user_detail(uid):
    T.expire_due()
    with _Cursor() as cur:
        cur.execute(f"SELECT {USER_COLS}, p.session_started, p.path, p.visible, p.user_agent, u.avatar_url "
                    "FROM users u LEFT JOIN user_presence p ON p.user_id = u.id WHERE u.id=%s", (uid,))
        u = cur.fetchone()
        if not u:
            return None
        cur.execute("SELECT * FROM subscriptions WHERE user_id=%s ORDER BY id DESC", (uid,))
        subs = [_row(r) for r in cur.fetchall() or []]
        cur.execute("SELECT * FROM payments WHERE user_id=%s ORDER BY id DESC LIMIT 100", (uid,))
        pays = [_row(r) for r in cur.fetchall() or []]
        cur.execute("SELECT SUM(analyses) AS a, SUM(logins) AS l, COUNT(*) AS d FROM user_daily WHERE user_id=%s", (uid,))
        st = cur.fetchone() or {}
        cur.execute("SELECT at FROM activity_log WHERE user_id=%s AND kind='login' ORDER BY id DESC LIMIT 1", (uid,))
        ll = cur.fetchone()
        cur.execute("SELECT id, at, kind, detail, actor FROM activity_log WHERE user_id=%s ORDER BY at DESC, id DESC LIMIT 25", (uid,))
        events = [_row(r) for r in cur.fetchall() or []]

    def count(sql):
        def run():
            with _Cursor() as c2:
                c2.execute(sql, (uid,))
                r = c2.fetchone() or {}
                return {"total": int(r.get("n") or 0), "open": int(r.get("o") or 0)}
        return _safe(run)

    trades = {
        "tracked": count("SELECT COUNT(*) AS n, SUM(CASE WHEN status='ACTIVE' THEN 1 ELSE 0 END) AS o FROM active_trades WHERE user_id=%s"),
        "bot": count("SELECT COUNT(*) AS n, SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS o FROM autotrade_positions WHERE user_id=%s"),
        "demo_closed": count("SELECT COUNT(*) AS n, 0 AS o FROM paper_closed WHERE user_id=%s"),
        "demo_open": count("SELECT COUNT(*) AS n, COUNT(*) AS o FROM paper_positions WHERE user_id=%s"),
    }
    now = _utcnow()
    current = next((s for s in subs if s["status"] == "active" and (T._dt(s.get("expires_at")) or now) > now), None) \
        or (subs[0] if subs else None)
    last_seen = T._dt(u.get("last_seen"))
    last_event = T._dt(events[0]["at"]) if events else None
    last_active = max([d for d in (last_seen, last_event) if d], default=None)
    out = _row(u)
    out.update({
        "account_status": u.get("account_status") or "active",
        "presence": _online_status(u.get("last_seen"), u.get("ended_at"), bool(u.get("visible", 1))),
        "is_admin": (u.get("email") or "").lower() in admin_emails(),
        "subscriptions": subs, "payments": pays, "current_subscription": current,
        "subscription_status": (current or {}).get("status") or "none",
        "ever_used_subscription": any(s.get("first_used_at") for s in subs) if subs else None,
        "analyses": int(st.get("a") or 0), "logins": int(st.get("l") or 0), "active_days": int(st.get("d") or 0),
        "last_login": _iso(ll["at"]) if ll else None, "last_active": _iso(last_active),
        "paid_verified": round(sum(float(p["amount"] or 0) for p in pays if p["status"] == "verified"), 2),
        "trades": trades, "events": events,
    })
    return out


@admin_bp.route("/api/admin/users/<int:uid>", methods=["GET"])
@admin_api
def api_user(uid):
    u = user_detail(uid)
    if not u:
        return jsonify({"ok": False, "error": "No member with that id."}), 404
    return jsonify({"ok": True, "user": u})


@admin_bp.route("/api/admin/users/lookup", methods=["GET"])
@admin_api
def api_user_lookup():
    email = (request.args.get("email") or "").strip().lower()[:254]
    if not email:
        return jsonify({"ok": False, "error": "Enter an email address."}), 400
    with _Cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email=%s", (email,))
        r = cur.fetchone()
    if not r:
        found = list_users(email, per=5)["users"]
        return jsonify({"ok": False, "error": "No account with exactly that email.", "similar": found}), 404
    return jsonify({"ok": True, "user": user_detail(r["id"])})


def _target_user(uid):
    with _Cursor() as cur:
        cur.execute("SELECT id, email, account_status FROM users WHERE id=%s", (uid,))
        u = cur.fetchone()
    if not u:
        raise T.SubError("No member with that id.")
    return u


def _end_sessions(uid):
    import auth
    with _Cursor() as cur:
        auth._bump_session_version(cur, uid)


@admin_bp.route("/api/admin/users/<int:uid>/status", methods=["POST"])
@admin_api
def api_user_status(uid):
    status = str((request.get_json(silent=True) or {}).get("status") or "")
    if status not in ("active", "blocked"):
        raise T.SubError("Status must be active or blocked.")
    u = _target_user(uid)
    if status == "blocked" and (u["email"] or "").lower() in admin_emails():
        raise T.SubError("An admin account (ADMIN_EMAILS) cannot be suspended here.")
    with _Cursor() as cur:
        cur.execute("UPDATE users SET account_status=%s WHERE id=%s", (status, uid))
    if status == "blocked":
        _end_sessions(uid)          # signs the member out everywhere at once
    else:
        import auth
        with auth._sv_lock:
            auth._sv_cache.pop(uid, None)
    T.log_event("user_blocked" if status == "blocked" else "user_unblocked", uid, u["email"],
                "account suspended" if status == "blocked" else "account active again", actor=g.admin["email"])
    return jsonify({"ok": True, "user": user_detail(uid)})


@admin_bp.route("/api/admin/users/<int:uid>/logout", methods=["POST"])
@admin_api
def api_user_logout(uid):
    u = _target_user(uid)
    _end_sessions(uid)
    T.log_event("user_logged_out", uid, u["email"], "signed out on every device by the admin", actor=g.admin["email"])
    return jsonify({"ok": True})


@admin_bp.route("/api/admin/users/<int:uid>/name", methods=["POST"])
@admin_api
def api_user_name(uid):
    name = T._text((request.get_json(silent=True) or {}).get("full_name"), 120)
    u = _target_user(uid)
    with _Cursor() as cur:
        cur.execute("UPDATE users SET full_name=%s WHERE id=%s", (name, uid))
    T.log_event("user_renamed", uid, u["email"], f"name set to {name or '(empty)'}", actor=g.admin["email"])
    return jsonify({"ok": True, "user": user_detail(uid)})


# ===========================================================================
# subscriptions and payments
# ===========================================================================
SUB_FILTERS = {
    "active": ("s.status='active' AND s.expires_at > %s", "now"),
    "expired": ("s.status='expired'", None),
    "pending": ("s.status='pending'", None),
    "cancelled": ("s.status='cancelled'", None),
    "unused": ("s.status IN ('active','expired') AND s.first_used_at IS NULL", None),
}


def list_subscriptions(status="", q="", page=1, per=PER_PAGE):
    T.expire_due()
    now = _utcnow()
    where, args = ["1=1"], []
    if status in SUB_FILTERS:
        cond, arg = SUB_FILTERS[status]
        where.append(cond)
        if arg == "now":
            args.append(now)
    if q:
        where.append("(u.email LIKE %s OR s.plan_name LIKE %s)")
        args += [f"%{q}%", f"%{q}%"]
    w = " AND ".join(where)
    with _Cursor() as cur:
        total = int(_scalar(cur, f"SELECT COUNT(*) AS n FROM subscriptions s LEFT JOIN users u ON u.id = s.user_id WHERE {w}",
                            tuple(args)))
        cur.execute(f"""SELECT s.*, u.email FROM subscriptions s LEFT JOIN users u ON u.id = s.user_id WHERE {w}
                        ORDER BY s.id DESC LIMIT %s OFFSET %s""", tuple(args) + (per, (page - 1) * per))
        rows = [_row(r) for r in cur.fetchall() or []]
        ids = [r["id"] for r in rows]
        paid = {}
        if ids:
            marks = ",".join(["%s"] * len(ids))
            cur.execute(f"""SELECT subscription_id, SUM(CASE WHEN status='verified' THEN amount ELSE 0 END) AS v,
                                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS p
                            FROM payments WHERE subscription_id IN ({marks}) GROUP BY subscription_id""", tuple(ids))
            paid = {r["subscription_id"]: r for r in cur.fetchall() or []}
        cur.execute("SELECT status, COUNT(*) AS n, SUM(CASE WHEN first_used_at IS NULL THEN 1 ELSE 0 END) AS unused, "
                    "SUM(use_count) AS uses FROM subscriptions GROUP BY status")
        stats = {r["status"]: {"n": int(r["n"]), "unused": int(r["unused"] or 0), "uses": int(r["uses"] or 0)}
                 for r in cur.fetchall() or []}
        active_now = int(_scalar(cur, "SELECT COUNT(*) AS n FROM subscriptions WHERE status='active' AND expires_at > %s", (now,)))
    for r in rows:
        p = paid.get(r["id"]) or {}
        r["paid_verified"] = _num(p.get("v") or 0)
        r["payments_pending"] = int(p.get("p") or 0)
        r["ever_used"] = bool(r.get("first_used_at"))
    used = sum(v["n"] - v["unused"] for k, v in stats.items() if k in ("active", "expired"))
    bought = sum(v["n"] for k, v in stats.items() if k in ("active", "expired"))
    summary = {"active": active_now, "expired": stats.get("expired", {}).get("n", 0),
               "pending": stats.get("pending", {}).get("n", 0), "cancelled": stats.get("cancelled", {}).get("n", 0),
               "unused": bought - used, "used": used, "total": sum(v["n"] for v in stats.values()),
               "uses": sum(v["uses"] for v in stats.values()),
               "usage_rate": round(used / bought * 100, 1) if bought else None}
    return {"subscriptions": rows, "total": total, "page": page, "pages": max(1, -(-total // per)), "summary": summary}


@admin_bp.route("/api/admin/subscriptions", methods=["GET"])
@admin_api
def api_subscriptions():
    return jsonify({"ok": True, **list_subscriptions(request.args.get("status") or "", _q(), _int_arg("page", 1, 1, 100000))})


def _sub_out(sid):
    s = T.get_subscription(sid)
    if not s:
        return None
    out = _row(s)
    with _Cursor() as cur:
        cur.execute("SELECT * FROM payments WHERE subscription_id=%s ORDER BY id DESC", (sid,))
        out["payments"] = [_row(r) for r in cur.fetchall() or []]
        cur.execute("SELECT id, at, kind, detail, actor FROM activity_log WHERE user_id=%s AND (kind LIKE 'subscription%%' "
                    "OR kind LIKE 'payment%%') ORDER BY at DESC, id DESC LIMIT 30", (s["user_id"],))
        out["events"] = [_row(r) for r in cur.fetchall() or []]
    out["ever_used"] = bool(s.get("first_used_at"))
    return out


@admin_bp.route("/api/admin/subscriptions/<int:sid>", methods=["GET"])
@admin_api
def api_subscription(sid):
    s = _sub_out(sid)
    if not s:
        return jsonify({"ok": False, "error": "No subscription with that id."}), 404
    return jsonify({"ok": True, "subscription": s})


@admin_bp.route("/api/admin/subscriptions", methods=["POST"])
@admin_api
def api_subscription_create():
    d = request.get_json(silent=True) or {}
    uid = d.get("user_id")
    if not uid and d.get("email"):
        with _Cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email=%s", (str(d["email"]).strip().lower(),))
            r = cur.fetchone()
        uid = r and r["id"]
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        raise T.SubError("No member with that email.")
    try:
        plan_id = int(d.get("plan_id"))
    except (TypeError, ValueError):
        raise T.SubError("Choose a plan (create plans on the Settings page).")
    pay = d.get("payment") if isinstance(d.get("payment"), dict) else None
    s = T.create_subscription(uid, plan_id, g.admin["email"], status=str(d.get("status") or "active"),
                              days=d.get("days"), price=d.get("price"), currency=d.get("currency"), note=d.get("note"),
                              payment=pay, starts_at=d.get("starts_at") or None)
    _forget("analytics")
    return jsonify({"ok": True, "subscription": _sub_out(s["id"])})


@admin_bp.route("/api/admin/subscriptions/<int:sid>/status", methods=["POST"])
@admin_api
def api_subscription_status(sid):
    d = request.get_json(silent=True) or {}
    T.set_subscription_status(sid, str(d.get("status") or ""), g.admin["email"], d.get("reason"))
    _forget("analytics")
    return jsonify({"ok": True, "subscription": _sub_out(sid)})


@admin_bp.route("/api/admin/subscriptions/<int:sid>/extend", methods=["POST"])
@admin_api
def api_subscription_extend(sid):
    T.extend_subscription(sid, (request.get_json(silent=True) or {}).get("days"), g.admin["email"])
    return jsonify({"ok": True, "subscription": _sub_out(sid)})


@admin_bp.route("/api/admin/subscriptions/<int:sid>/payments", methods=["POST"])
@admin_api
def api_payment_add(sid):
    T.add_payment(sid, request.get_json(silent=True) or {}, g.admin["email"])
    _forget("analytics")
    return jsonify({"ok": True, "subscription": _sub_out(sid)})


@admin_bp.route("/api/admin/payments/<int:pid>/status", methods=["POST"])
@admin_api
def api_payment_status(pid):
    p = T.set_payment_status(pid, str((request.get_json(silent=True) or {}).get("status") or ""), g.admin["email"])
    _forget("analytics")
    return jsonify({"ok": True, "subscription": _sub_out(p["subscription_id"]) if p.get("subscription_id") else None})


# ===========================================================================
# plans and settings
# ===========================================================================
@admin_bp.route("/api/admin/plans", methods=["GET"])
@admin_api
def api_plans():
    return jsonify({"ok": True, "plans": [_row(p) for p in T.list_plans()]})


@admin_bp.route("/api/admin/plans", methods=["POST"])
@admin_api
def api_plan_create():
    p = T.save_plan(request.get_json(silent=True) or {}, g.admin["email"])
    return jsonify({"ok": True, "plan": _row(p)})


@admin_bp.route("/api/admin/plans/<int:pid>", methods=["POST"])
@admin_api
def api_plan_update(pid):
    p = T.save_plan(request.get_json(silent=True) or {}, g.admin["email"], plan_id=pid)
    return jsonify({"ok": True, "plan": _row(p)})


def _mask(email):
    name, _, dom = email.partition("@")
    return (name[:2] + "•••" + "@" + dom) if dom else email


@admin_bp.route("/api/admin/settings", methods=["GET"])
@admin_api
def api_settings():
    with _Cursor() as cur:
        counts = {t: int(_scalar(cur, f"SELECT COUNT(*) AS n FROM {t}")) for t in
                  ("activity_log", "user_presence", "visitor_days", "user_daily", "subscriptions", "payments")}
    return jsonify({"ok": True, "admin_emails": [e if e == g.admin["email"] else _mask(e) for e in admin_emails()],
                    "you": g.admin["email"], "require_google": require_google(),
                    "login_method": session.get("login_method") or "unknown",
                    "online_window_sec": T.online_window(), "heartbeat_sec": T.heartbeat_seconds(),
                    "visitor_tracking": T.visitor_tracking_on(), "currency": main_currency(),
                    "plans": [_row(p) for p in T.list_plans()], "records": counts})


# ===========================================================================
# online members, activity log
# ===========================================================================
def _device(ua):
    ua = ua or ""
    kind = "Phone" if re.search(r"Mobile|Android|iPhone", ua) else ("Tablet" if re.search(r"iPad|Tablet", ua) else "Desktop")
    for name, pat in (("Edge", r"Edg/"), ("Opera", r"OPR/"), ("Chrome", r"Chrome/"), ("Firefox", r"Firefox/"),
                      ("Safari", r"Safari/")):
        if re.search(pat, ua):
            return f"{kind} · {name}"
    return kind if ua else ""


def online_members():
    now = _utcnow()
    win = T.online_window()
    with _Cursor() as cur:
        cur.execute("""SELECT p.user_id, p.email, p.last_seen, p.session_started, p.path, p.visible, p.user_agent, p.ended_at,
                              u.full_name FROM user_presence p LEFT JOIN users u ON u.id = p.user_id
                       WHERE p.last_seen >= %s ORDER BY p.last_seen DESC LIMIT 300""", (now - timedelta(minutes=30),))
        rows = cur.fetchall() or []
    out = []
    for r in rows:
        ls, started = T._dt(r["last_seen"]), T._dt(r["session_started"])
        status = _online_status(ls, r.get("ended_at"), bool(r.get("visible")))
        out.append({"user_id": r["user_id"], "email": r["email"], "name": r.get("full_name"), "last_seen": _iso(ls),
                    "seconds_ago": int((now - ls).total_seconds()) if ls else None, "session_started": _iso(started),
                    "session_minutes": int(((ls or now) - (started or now)).total_seconds() // 60),
                    "page": r.get("path"), "device": _device(r.get("user_agent")),
                    "status": "away" if status == "offline" else status})
    online = [m for m in out if m["status"] in ("online", "idle")]
    return {"online": online, "recent": [m for m in out if m["status"] not in ("online", "idle")],
            "count": len(online), "window_sec": win, "heartbeat_sec": T.heartbeat_seconds(), "generated_at": _iso(now)}


@admin_bp.route("/api/admin/online", methods=["GET"])
@admin_api
def api_online():
    return jsonify({"ok": True, **online_members()})


def list_activity(group="", q="", page=1, per=50):
    where, args = ["1=1"], []
    if group in ACTIVITY_GROUPS:
        where.append(ACTIVITY_GROUPS[group][0])
    if q:
        where.append("(email LIKE %s OR actor LIKE %s OR detail LIKE %s)")
        args += [f"%{q}%"] * 3
    w = " AND ".join(where)
    with _Cursor() as cur:
        total = int(_scalar(cur, f"SELECT COUNT(*) AS n FROM activity_log WHERE {w}", tuple(args)))
        cur.execute(f"SELECT id, at, kind, user_id, email, actor, detail FROM activity_log WHERE {w} "
                    "ORDER BY at DESC, id DESC LIMIT %s OFFSET %s", tuple(args) + (per, (page - 1) * per))
        rows = [_row(r) for r in cur.fetchall() or []]
    return {"events": rows, "total": total, "page": page, "pages": max(1, -(-total // per))}


@admin_bp.route("/api/admin/activity", methods=["GET"])
@admin_api
def api_activity():
    return jsonify({"ok": True, **list_activity(request.args.get("group") or "", _q(), _int_arg("page", 1, 1, 100000))})


# ===========================================================================
# trade history (read only: the trading code and its tables are not changed)
# ===========================================================================
def _snapshot(text):
    """The signal a tracked trade came from (the Pro terminal stores the whole analysis when tracking starts)."""
    try:
        d = json.loads(text[:300000]) if text else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {"verdict": d.get("final_verdict"), "confidence": _num(d.get("confidence_pct"), 1),
            "timeframe": d.get("timeframe")}


def list_trades(source="tracked", q="", status="", page=1, per=PER_PAGE):
    like = f"%{q}%"
    off = (page - 1) * per
    with _Cursor() as cur:
        if source == "bot":
            st = {"open": " AND p.status='OPEN'", "closed": " AND p.status<>'OPEN'"}.get(status, "")
            total = int(_scalar(cur, f"SELECT COUNT(*) AS n FROM autotrade_positions p JOIN users u ON u.id=p.user_id "
                                     f"WHERE (u.email LIKE %s OR p.symbol LIKE %s){st}", (like, like)))
            cur.execute(f"""SELECT p.id, p.user_id, u.email, p.mode, p.symbol, p.side, p.source, p.entry_price, p.stop_loss,
                                   p.take_profit, p.confidence, p.status, p.opened_at, p.closed_at, p.exit_price, p.pnl_usdt,
                                   p.close_reason, p.notional_usdt
                            FROM autotrade_positions p JOIN users u ON u.id=p.user_id
                            WHERE (u.email LIKE %s OR p.symbol LIKE %s){st} ORDER BY p.id DESC LIMIT %s OFFSET %s""",
                        (like, like, per, off))
            rows = [{"source": "bot", "id": r["id"], "user_id": r["user_id"], "email": r["email"], "symbol": r["symbol"],
                     "side": r["side"], "signal": f"{r['side']} · {r['source']}" + (f" · {_num(r['confidence'], 1)}%" if r.get("confidence") else ""),
                     "mode": r["mode"], "entry": _num(r["entry_price"], 8), "stop": _num(r["stop_loss"], 8),
                     "target": _num(r["take_profit"], 8), "status": r["status"], "opened_at": _iso(r["opened_at"]),
                     "closed_at": _iso(r["closed_at"]), "exit": _num(r["exit_price"], 8), "pnl": _num(r["pnl_usdt"]),
                     "pnl_unit": "USDT", "result": r.get("close_reason")} for r in cur.fetchall() or []]
        elif source == "demo":
            rows = []
            if status != "open":
                total_c = int(_scalar(cur, "SELECT COUNT(*) AS n FROM paper_closed c JOIN users u ON u.id=c.user_id "
                                           "WHERE (u.email LIKE %s OR c.symbol LIKE %s)", (like, like)))
                cur.execute("""SELECT c.id, c.user_id, u.email, c.market, c.symbol, c.side, c.leverage, c.entry_price,
                                      c.exit_price, c.net_pnl, c.roe_pct, c.close_reason, c.opened_at, c.closed_at
                               FROM paper_closed c JOIN users u ON u.id=c.user_id WHERE (u.email LIKE %s OR c.symbol LIKE %s)
                               ORDER BY c.closed_at DESC LIMIT %s OFFSET %s""", (like, like, per, off))
                rows += [{"source": "demo", "id": r["id"], "user_id": r["user_id"], "email": r["email"], "symbol": r["symbol"],
                          "side": r["side"], "signal": f"{r['side']} · {r['leverage']}x", "mode": r["market"],
                          "entry": _num(r["entry_price"], 8), "stop": None, "target": None, "status": "CLOSED",
                          "opened_at": _iso(r["opened_at"]), "closed_at": _iso(r["closed_at"]),
                          "exit": _num(r["exit_price"], 8), "pnl": _num(r["net_pnl"]), "pnl_unit": "USDT (demo)",
                          "result": r.get("close_reason")} for r in cur.fetchall() or []]
            else:
                total_c = 0
            if status != "closed":
                total_o = int(_scalar(cur, "SELECT COUNT(*) AS n FROM paper_positions p JOIN users u ON u.id=p.user_id "
                                           "WHERE (u.email LIKE %s OR p.symbol LIKE %s)", (like, like)))
                cur.execute("""SELECT p.id, p.user_id, u.email, p.market, p.symbol, p.side, p.leverage, p.entry_price,
                                      p.tp_price, p.sl_price, p.opened_at FROM paper_positions p JOIN users u ON u.id=p.user_id
                               WHERE (u.email LIKE %s OR p.symbol LIKE %s) ORDER BY p.opened_at DESC LIMIT %s OFFSET %s""",
                            (like, like, per, off))
                rows += [{"source": "demo", "id": r["id"], "user_id": r["user_id"], "email": r["email"], "symbol": r["symbol"],
                          "side": r["side"], "signal": f"{r['side']} · {r['leverage']}x", "mode": r["market"],
                          "entry": _num(r["entry_price"], 8), "stop": _num(r["sl_price"], 8), "target": _num(r["tp_price"], 8),
                          "status": "OPEN", "opened_at": _iso(r["opened_at"]), "closed_at": None, "exit": None, "pnl": None,
                          "pnl_unit": "", "result": None} for r in cur.fetchall() or []]
            else:
                total_o = 0
            rows.sort(key=lambda r: r["closed_at"] or r["opened_at"] or "", reverse=True)
            rows = rows[:per]
            total = max(total_c, total_o) if status not in ("open", "closed") else (total_o or total_c)
        else:
            source = "tracked"
            st = {"open": " AND t.status='ACTIVE'", "closed": " AND t.status<>'ACTIVE'"}.get(status, "")
            total = int(_scalar(cur, f"SELECT COUNT(*) AS n FROM active_trades t JOIN users u ON u.id=t.user_id "
                                     f"WHERE (u.email LIKE %s OR t.asset LIKE %s){st}", (like, like)))
            cur.execute(f"""SELECT t.id, t.user_id, u.email, t.asset, t.direction, t.entry_price, t.stop_loss, t.take_profit,
                                   t.leverage, t.status, t.created_at, t.closed_at, t.exit_price, t.exit_reason,
                                   t.estimated_pnl_percent, t.holding_period_label, t.signal_snapshot
                            FROM active_trades t JOIN users u ON u.id=t.user_id
                            WHERE (u.email LIKE %s OR t.asset LIKE %s){st} ORDER BY t.id DESC LIMIT %s OFFSET %s""",
                        (like, like, per, off))
            rows = []
            for r in cur.fetchall() or []:
                snap = _snapshot(r.get("signal_snapshot"))
                sig = (snap.get("verdict") or r["direction"] or "")
                if snap.get("confidence") is not None:
                    sig += f" · {snap['confidence']}%"
                if snap.get("timeframe"):
                    sig += f" · {snap['timeframe']}"
                rows.append({"source": "tracked", "id": r["id"], "user_id": r["user_id"], "email": r["email"],
                             "symbol": r["asset"], "side": r["direction"], "signal": sig,
                             "mode": r.get("holding_period_label"), "entry": _num(r["entry_price"], 8),
                             "stop": _num(r["stop_loss"], 8), "target": _num(r["take_profit"], 8), "status": r["status"],
                             "opened_at": _iso(r["created_at"]), "closed_at": _iso(r["closed_at"]),
                             "exit": _num(r["exit_price"], 8), "pnl": _num(r["estimated_pnl_percent"]), "pnl_unit": "%",
                             "result": r.get("exit_reason")})
    return {"source": source, "trades": rows, "total": total, "page": page, "pages": max(1, -(-total // per))}


def trade_counts():
    def one(sql):
        def run():
            with _Cursor() as cur:
                cur.execute(sql)
                r = cur.fetchone() or {}
                return {"total": int(r.get("n") or 0), "open": int(r.get("o") or 0)}
        return _safe(run)
    return {"tracked": one("SELECT COUNT(*) AS n, SUM(CASE WHEN status='ACTIVE' THEN 1 ELSE 0 END) AS o FROM active_trades"),
            "bot": one("SELECT COUNT(*) AS n, SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS o FROM autotrade_positions"),
            "demo": one("SELECT (SELECT COUNT(*) FROM paper_closed) + (SELECT COUNT(*) FROM paper_positions) AS n, "
                        "(SELECT COUNT(*) FROM paper_positions) AS o")}


@admin_bp.route("/api/admin/trades", methods=["GET"])
@admin_api
def api_trades():
    source = request.args.get("source") or "tracked"
    data = _safe(lambda: list_trades(source if source in ("tracked", "bot", "demo") else "tracked", _q(),
                                     request.args.get("status") or "", _int_arg("page", 1, 1, 100000)))
    if data is None:
        data = {"source": source, "trades": [], "total": 0, "page": 1, "pages": 1,
                "note": "This feature has no records in the database yet."}
    return jsonify({"ok": True, **data, "counts": trade_counts()})


# ===========================================================================
# exports (CSV)
# ===========================================================================
def _csv_cell(v):
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return _iso(v)
    if isinstance(v, Decimal):
        v = float(v)
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") and not re.fullmatch(r"-?\d+(\.\d+)?", s) else s


def _csv_response(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([_csv_cell(x) for x in r])
    fname = f"signalsfm-{name}-{_utcnow():%Y%m%d-%H%M}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"', "Cache-Control": "no-store"})


@admin_bp.route("/api/admin/export/<kind>.csv", methods=["GET"])
@admin_api
def api_export(kind):
    if kind not in ("users", "subscriptions", "activity"):
        return jsonify({"ok": False, "error": "Unknown export."}), 404
    T.expire_due()
    with _Cursor() as cur:
        if kind == "users":
            cur.execute(f"SELECT {USER_COLS} FROM users u LEFT JOIN user_presence p ON p.user_id=u.id ORDER BY u.id")
            users = cur.fetchall() or []
            ids = [u["id"] for u in users]
            subs, an = {}, {}
            for i in range(0, len(ids), 500):
                subs.update(_sub_summary(cur, ids[i:i + 500]))
                an.update(_analyses(cur, ids[i:i + 500]))
            header = ["id", "email", "full_name", "registered_at", "account_status", "email_verified", "sign_in",
                      "subscription_status", "plan", "expires_at", "ever_used_subscription", "analyses", "last_seen"]
            rows = []
            for u in users:
                s = subs.get(u["id"]) or {}
                c = s.get("current") or {}
                rows.append([u["id"], u["email"], u.get("full_name"), u.get("created_at"), u.get("account_status") or "active",
                             "yes" if u.get("email_verified") else "no", u.get("auth_provider"), c.get("status") or "none",
                             c.get("plan"), c.get("expires_at"), "yes" if s.get("ever_used") else "no", an.get(u["id"], 0),
                             u.get("last_seen")])
        elif kind == "subscriptions":
            cur.execute("""SELECT s.id, u.email, s.plan_name, s.price, s.currency, s.days, s.status, s.purchased_at, s.starts_at,
                                  s.expires_at, s.cancelled_at, s.first_used_at, s.last_used_at, s.use_count, s.created_by, s.note,
                                  (SELECT SUM(amount) FROM payments p WHERE p.subscription_id = s.id AND p.status='verified') AS paid
                           FROM subscriptions s LEFT JOIN users u ON u.id = s.user_id ORDER BY s.id""")
            data = cur.fetchall() or []
            header = ["id", "email", "plan", "price", "currency", "days", "status", "purchased_at", "starts_at", "expires_at",
                      "cancelled_at", "first_used_at", "last_used_at", "uses", "verified_paid", "created_by", "note"]
            rows = [[r["id"], r["email"], r["plan_name"], r["price"], r["currency"], r["days"], r["status"], r["purchased_at"],
                     r["starts_at"], r["expires_at"], r["cancelled_at"], r["first_used_at"], r["last_used_at"], r["use_count"],
                     r.get("paid") or 0, r["created_by"], r["note"]] for r in data]
        else:
            cur.execute("SELECT id, at, kind, email, actor, detail FROM activity_log ORDER BY at DESC, id DESC LIMIT 50000")
            header = ["id", "at", "kind", "email", "actor", "detail"]
            rows = [[r["id"], r["at"], r["kind"], r["email"], r["actor"], r["detail"]] for r in cur.fetchall() or []]
    T.log_event("export_" + kind, detail=f"{len(rows)} rows", actor=g.admin["email"])
    return _csv_response(kind, header, rows)
