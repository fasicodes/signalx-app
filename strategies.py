"""
Strategies page - Signals FM
============================

/strategies shows two kinds of strategies:

  1. The library (strategy_library.py): 30 well-known public strategies, marked "By Signals FM" and "Backtested".
     Every STRATEGIES_REFRESH_HOURS one instance (MySQL named lock) runs all of them on the last STRATEGIES_DAYS days
     of 1h candles (4h candles are built from them) for the library coins and forex pairs, and stores the results in
     the strategy_results table. Until the first run has finished, models/strategies_seed.json is shown (made the same
     way from Binance candles; its period is shown on the page).

  2. Community strategies: logged-in users publish a title, summary, description, rules, market, timeframe and an
     optional chart image. They are shown at once, marked "By a user" and "Not tested". Links, e-mail addresses and
     @handles are refused. Three reports hide a post until an admin (ADMIN_EMAILS) restores it; the author or an
     admin can delete it.

ENVIRONMENT
  STRATEGIES_ENGINE         on (default) / off
  STRATEGIES_REFRESH_HOURS  hours between library runs (default 24)
  STRATEGIES_DAYS           backtest length in days (default 365)
  STRATEGIES_COINS          optional list of crypto symbols, e.g. BTC/USDT,ETH/USDT
  STRATEGIES_FOREX          optional list of forex pairs; "off" = no forex results
"""

import json
import os
import re
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, redirect, render_template, request, session

from db import get_db_connection
import strategy_library as SL

strategies_bp = Blueprint("strategies", __name__)

ENGINE_LOCK_NAME = "signalx_strategies_engine"
DEFAULT_COINS = SL.LIB_COINS
DEFAULT_FOREX = SL.LIB_FOREX
SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "strategies_seed.json")

MARKETS = ("crypto", "forex", "both")
TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "any")
COMMUNITY_CATEGORIES = SL.CATEGORIES + ["Scalping", "Other"]
REPORTS_TO_HIDE = 3
POSTS_PER_DAY = 3
MAX_IMAGE_BYTES = 1_500_000
PAGE_SIZE = 24
_LINKS = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|io|me|gg|ly|xyz|app|co|info|biz|ru|cn|pk|in|to|site|"
                    r"link|club|top|vip|live|online|shop|store|pro)\b|t\.me/|telegram\.(me|dog)|wa\.me|discord\.gg|"
                    r"\S+@\S+\.\S+|@[a-z0-9_]{4,})", re.I)
_AUTHOR = re.compile(r"^[A-Za-z0-9 ._\-]{2,40}$")

_hooks = {"get_candles": None, "is_admin": None}
_hist = {}                 # symbol -> 1h candles kept between runs (only new candles are fetched next time)
_hist_lock = threading.Lock()
_cache = {"at": 0.0, "data": None}
_state = {"running": False, "last_error": None}


def _env_on(name, default="on"):
    return (os.environ.get(name, default) or default).strip().lower() not in ("0", "off", "false", "no")


def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def _to_dt(v):
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "").replace("T", " "))
    except ValueError:
        return None


def _iso(v):
    d = _to_dt(v)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def refresh_hours():
    try:
        return max(1.0, float(os.environ.get("STRATEGIES_REFRESH_HOURS") or 24))
    except ValueError:
        return 24.0


def backtest_days():
    try:
        return max(60, min(730, int(os.environ.get("STRATEGIES_DAYS") or 365)))
    except ValueError:
        return 365


def lib_coins():
    raw = (os.environ.get("STRATEGIES_COINS") or "").strip()
    if raw:
        out = [x.strip().upper() for x in raw.split(",") if x.strip()]
        return [x if "/" in x else x + "/USDT" for x in out][:40]
    return list(DEFAULT_COINS)


def lib_forex():
    raw = (os.environ.get("STRATEGIES_FOREX") or "").strip()
    if raw.lower() in ("off", "none", "0", "no"):
        return []
    if raw:
        return [x.strip().upper() for x in raw.split(",") if x.strip()][:20]
    return list(DEFAULT_FOREX)


def _is_admin():
    fn = _hooks.get("is_admin")
    try:
        return bool(fn and fn())
    except Exception:
        return False


def _uid():
    return session.get("user_id")


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
# DB
# ===========================================================================
def init_tables():
    with _Cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_results (
                slug VARCHAR(80) PRIMARY KEY,
                data MEDIUMTEXT NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS community_strategies (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                author VARCHAR(40) NOT NULL,
                title VARCHAR(100) NOT NULL,
                summary VARCHAR(220) NOT NULL,
                description TEXT NOT NULL,
                rules TEXT NULL,
                market VARCHAR(10) NOT NULL DEFAULT 'crypto',
                timeframe VARCHAR(10) NOT NULL DEFAULT 'any',
                category VARCHAR(30) NOT NULL DEFAULT 'Other',
                image MEDIUMBLOB NULL,
                image_type VARCHAR(20) NULL,
                status VARCHAR(10) NOT NULL DEFAULT 'visible',
                reports INT NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                INDEX idx_community_status (status, created_at)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_votes (
                target VARCHAR(100) NOT NULL,
                user_id INT NOT NULL,
                kind VARCHAR(10) NOT NULL,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (target, user_id, kind)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_counters (
                target VARCHAR(100) PRIMARY KEY,
                likes INT NOT NULL DEFAULT 0,
                views INT NOT NULL DEFAULT 0
            )
            """
        )


def _put_result(slug, data, at, cur):
    blob = json.dumps(data, separators=(",", ":"), default=str)
    cur.execute("SELECT slug FROM strategy_results WHERE slug=%s", (slug,))
    if cur.fetchone():
        cur.execute("UPDATE strategy_results SET data=%s, updated_at=%s WHERE slug=%s", (blob, at, slug))
    else:
        cur.execute("INSERT INTO strategy_results (slug, data, updated_at) VALUES (%s,%s,%s)", (slug, blob, at))


def _load_seed():
    try:
        with open(SEED_FILE, encoding="utf-8") as f:
            seed = json.load(f)
        return seed if isinstance(seed, dict) and isinstance(seed.get("strategies"), dict) else None
    except (OSError, ValueError):
        return None


def library_results(max_age=60):
    """{slug: stored result} plus "_meta"; falls back to the seed file until the first run is stored."""
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < max_age:
        return _cache["data"]
    out = {}
    try:
        with _Cursor() as cur:
            cur.execute("SELECT slug, data FROM strategy_results")
            for row in cur.fetchall() or []:
                try:
                    out[row["slug"]] = json.loads(row["data"])
                except (TypeError, ValueError):
                    continue
    except Exception:
        out = {}
    if not any(k in SL.BY_SLUG for k in out):
        db_meta = out.get("_meta") or {}
        seed = _load_seed()
        out = dict(seed["strategies"]) if seed else {}
        out["_meta"] = {**((seed or {}).get("meta") or {}), "source": "seed" if seed else None,
                        **{k: db_meta[k] for k in ("last_error", "last_attempt") if k in db_meta}}
    _cache.update(at=now, data=out)
    return out


def _counters(targets):
    if not targets:
        return {}
    out = {}
    try:
        with _Cursor() as cur:
            marks = ",".join(["%s"] * len(targets))
            cur.execute(f"SELECT target, likes, views FROM strategy_counters WHERE target IN ({marks})", tuple(targets))
            for r in cur.fetchall() or []:
                out[r["target"]] = {"likes": int(r["likes"] or 0), "views": int(r["views"] or 0)}
    except Exception:
        pass
    return out


def _liked_by(uid, targets):
    if not uid or not targets:
        return set()
    try:
        with _Cursor() as cur:
            marks = ",".join(["%s"] * len(targets))
            cur.execute(f"SELECT target FROM strategy_votes WHERE user_id=%s AND kind='like' AND target IN ({marks})",
                        (uid, *targets))
            return {r["target"] for r in cur.fetchall() or []}
    except Exception:
        return set()


def _bump(target, field, delta, cur):
    cur.execute("SELECT target FROM strategy_counters WHERE target=%s", (target,))
    if cur.fetchone():
        cur.execute(f"UPDATE strategy_counters SET {field} = CASE WHEN {field} + %s < 0 THEN 0 ELSE {field} + %s END "
                    f"WHERE target=%s", (delta, delta, target))
    else:
        cur.execute(f"INSERT INTO strategy_counters (target, {field}) VALUES (%s,%s)", (target, max(0, delta)))


def count_view(target):
    try:
        with _Cursor() as cur:
            _bump(target, "views", 1, cur)
    except Exception:
        pass


# ===========================================================================
# library: data + daily run
# ===========================================================================
def _closed(df, tf_sec, now):
    import pandas as pd
    if df is None or not len(df):
        return df
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df[df["timestamp"] + pd.Timedelta(seconds=tf_sec) <= pd.Timestamp(now)]
    return df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def _fetch_crypto_1h(symbol, start, now):
    """1h candles from `start` to now (paged, 300 a call); later calls fetch only the new candles."""
    import pandas as pd
    get = _hooks["get_candles"]
    epoch = datetime(1970, 1, 1)
    with _hist_lock:
        old = _hist.get(symbol)
    if old is not None and len(old) and old["timestamp"].iloc[0] <= pd.Timestamp(start) + pd.Timedelta(days=2):
        cursor = int((pd.Timestamp(old["timestamp"].iloc[-1]) - pd.Timestamp(epoch)).total_seconds() * 1000) + 3_600_000
        frames = [old]
    else:
        cursor = int((start - epoch).total_seconds() * 1000)
        frames = []
    now_ms = int((now - epoch).total_seconds() * 1000)
    seen = None
    calls = int((now_ms - cursor) / 3_600_000 / 100) + 4
    for _ in range(max(1, calls)):
        if cursor > now_ms - 3_600_000:
            break
        df = get(symbol=symbol, timeframe="1h", limit=300, since=cursor)
        if df is None or not len(df):
            break
        frames.append(df[["timestamp", "open", "high", "low", "close", "volume"]])
        last_ms = int(pd.Timestamp(df["timestamp"].iloc[-1]).value // 1_000_000)
        if seen is not None and last_ms <= seen:
            break
        seen = last_ms
        cursor = last_ms + 3_600_000
        time.sleep(0.15)
    if not frames:
        return None
    df = _closed(pd.concat(frames), 3600, now)
    df = df[df["timestamp"] >= pd.Timestamp(start)].reset_index(drop=True)
    with _hist_lock:
        _hist[symbol] = df
    return df


def _fetch_forex_1h(symbol, start, now):
    import pandas as pd
    get = _hooks["get_candles"]
    hours = int((now - start).total_seconds() // 3600)
    df = get(symbol=symbol, timeframe="1h", limit=min(6000, int(hours * 5 / 7) + 200), since=None)
    df = _closed(df, 3600, now)
    if df is None or not len(df):
        return None
    return df[df["timestamp"] >= pd.Timestamp(start)].reset_index(drop=True)


def refresh(now=None):
    """Fetch candles, run every library strategy, store the results."""
    now = now or _utcnow()
    if _hooks.get("get_candles") is None:
        raise RuntimeError("candles are not configured")
    _state["running"] = True
    t0 = time.time()
    try:
        days = backtest_days()
        start = now - timedelta(days=days)
        fetch_from = start - timedelta(days=45)            # room for the slowest indicator before the first trade
        frames = {"crypto": {"1h": {}, "4h": {}}, "forex": {"1h": {}, "4h": {}}}
        missing = []
        for market, syms, fetch in (("crypto", lib_coins(), _fetch_crypto_1h), ("forex", lib_forex(), _fetch_forex_1h)):
            for sym in syms:
                try:
                    df = fetch(sym, fetch_from, now)
                except Exception as e:
                    missing.append(f"{sym}: {str(e)[:60]}")
                    continue
                if df is None or len(df) < SL.WARMUP + 100:
                    missing.append(f"{sym}: not enough candles")
                    continue
                frames[market]["1h"][sym] = SL.prepare(df)
                h4 = SL.resample(df, "4h")
                if len(h4) > SL.WARMUP + 20:
                    frames[market]["4h"][sym] = SL.prepare(h4)
        if not frames["crypto"]["1h"]:
            raise RuntimeError("no crypto candles could be loaded")
        lib = SL.compute_all(frames, start, now)
        meta = {"source": "live", "updated_at": _iso(now), "days": days, "seconds": round(time.time() - t0, 1),
                "period": f"{start:%Y-%m-%d} to {now:%Y-%m-%d}",
                "coins": sorted(frames["crypto"]["1h"]), "forex": sorted(frames["forex"]["1h"]), "missing": missing[:40],
                "host": socket.gethostname()}
        with _Cursor() as cur:
            for slug, data in lib.items():
                _put_result(slug, data, now, cur)
            _put_result("_meta", meta, now, cur)
        _cache.update(at=0.0, data=None)
        _state["last_error"] = None
        print(f"[strategies] library refreshed in {meta['seconds']}s: {len(meta['coins'])} coins, {len(meta['forex'])} forex")
        return meta
    except Exception as e:
        _state["last_error"] = str(e)[:200]
        try:
            with _Cursor() as cur:
                cur.execute("SELECT data FROM strategy_results WHERE slug='_meta'")
                row = cur.fetchone()
                meta = json.loads(row["data"]) if row else {}
                meta.update(last_error=str(e)[:200], last_attempt=_iso(now))
                _put_result("_meta", meta, _to_dt(meta.get("updated_at")) or now, cur)
        except Exception:
            pass
        raise
    finally:
        _state["running"] = False


def _due(now=None):
    """A run is due when there is no live result yet or the last one is older than STRATEGIES_REFRESH_HOURS;
    after a failed run the next try waits an hour."""
    now = now or _utcnow()
    meta = library_results(max_age=0).get("_meta") or {}
    last = _to_dt(meta.get("updated_at")) if meta.get("source") == "live" else None
    failed = _to_dt(meta.get("last_attempt"))
    if failed and (last is None or failed > last) and now - failed < timedelta(hours=1):
        return False
    return last is None or now - last >= timedelta(hours=refresh_hours())


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:
        print(f"[strategies] {getattr(fn, '__name__', fn)} failed:\n{traceback.format_exc()}")
        return None


def _supervisor():
    time.sleep(90)            # let the app and the signal scanners start first
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
                time.sleep(600)
                continue
            while True:
                lock_conn.ping(reconnect=False)
                if _due():
                    _safe(refresh)
                time.sleep(600)
        except Exception:
            print(f"[strategies] engine error, restarting:\n{traceback.format_exc()}")
        finally:
            if lock_conn is not None:
                try:
                    lock_conn.close()
                except Exception:
                    pass
        time.sleep(60)


_started = False
_start_lock = threading.Lock()


def init_strategies(app=None, *, get_candles=None, is_admin=None, start=True):
    global _started
    if get_candles is not None:
        _hooks["get_candles"] = get_candles
    if is_admin is not None:
        _hooks["is_admin"] = is_admin
    try:
        init_tables()
    except Exception as e:
        print(f"[strategies] WARNING: could not create tables: {e}")
    if not start or not _env_on("STRATEGIES_ENGINE"):
        return
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_supervisor, name="strategies-engine", daemon=True).start()


# ===========================================================================
# community posts
# ===========================================================================
def _clean_text(v, limit):
    s = str(v or "").replace("\r\n", "\n").replace("\r", "\n")
    s = "".join(ch for ch in s if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s[:limit + 1]


def _image_type(blob):
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_post(form, uid):
    """-> (clean dict, None) or (None, error message)."""
    title = _clean_text(form.get("title"), 100)
    summary = _clean_text(form.get("summary"), 220)
    description = _clean_text(form.get("description"), 6000)
    rules = _clean_text(form.get("rules"), 4000)
    author = _clean_text(form.get("author"), 40).replace("\n", " ") or f"Trader {uid}"
    market = (form.get("market") or "crypto").strip().lower()
    timeframe = (form.get("timeframe") or "any").strip().lower()
    category = (form.get("category") or "Other").strip()
    if not 6 <= len(title) <= 100:
        return None, "The title needs 6 to 100 characters."
    if not 20 <= len(summary) <= 220:
        return None, "The short summary needs 20 to 220 characters."
    if not 80 <= len(description) <= 6000:
        return None, "The description needs 80 to 6,000 characters. Explain the idea, the entry and the exit."
    if len(rules) > 4000:
        return None, "The rules can have up to 4,000 characters."
    if not _AUTHOR.match(author):
        return None, "The display name can have 2 to 40 letters, numbers, spaces, dots, dashes or underscores."
    if market not in MARKETS:
        return None, "Choose crypto, forex or both."
    if timeframe not in TIMEFRAMES:
        return None, "Choose a timeframe from the list."
    if category not in COMMUNITY_CATEGORIES:
        return None, "Choose a category from the list."
    for text in (title, summary, description, rules, author):
        if _LINKS.search(text):
            return None, "Links, e-mail addresses and @handles are not allowed in community strategies."
    return {"title": title, "summary": summary, "description": description, "rules": rules or None, "author": author,
            "market": market, "timeframe": timeframe, "category": category}, None


def _post_row(pid):
    with _Cursor() as cur:
        cur.execute("SELECT id, user_id, author, title, summary, description, rules, market, timeframe, category, "
                    "image_type, status, reports, created_at, updated_at FROM community_strategies WHERE id=%s", (pid,))
        return cur.fetchone()


def _post_json(r, counters=None, liked=False, full=False):
    t = f"com:{r['id']}"
    cn = (counters or {}).get(t) or {}
    out = {"kind": "community", "id": int(r["id"]), "title": r["title"], "summary": r["summary"],
           "category": r["category"], "market": r["market"], "timeframe": r["timeframe"], "author": r["author"],
           "tested": False, "badge": "By a user", "status": r["status"],
           "image_url": f"/strategies/image/{int(r['id'])}" if r.get("image_type") else None,
           "created_at": _iso(r.get("created_at")), "updated_at": _iso(r.get("updated_at") or r.get("created_at")),
           "likes": cn.get("likes", 0), "views": cn.get("views", 0), "liked": liked, "target": t,
           "url": f"/strategies/community/{int(r['id'])}"}
    if full:
        out.update(description=r["description"], rules=r.get("rules"))
    return out


# ===========================================================================
# listing
# ===========================================================================
def _card_preview(pv):
    """A light copy of the chart data for the cards: closes, up to two lines and the entries."""
    if not pv:
        return None
    lines = dict(list((pv.get("overlays") or {}).items())[:2])
    osc = None
    if pv.get("osc") and not lines:
        first = next(iter(pv["osc"]["lines"].items()), None)
        osc = {"lines": {first[0]: first[1]}, "levels": pv["osc"]["levels"]} if first else None
    return {"c": pv.get("c"), "overlays": lines, "osc": osc, "entries": pv.get("entries"), "symbol": pv.get("symbol"),
            "timeframe": pv.get("timeframe")}


def library_cards(counters=None, liked=()):
    res = library_results()
    meta = res.get("_meta") or {}
    out = []
    for spec in SL.LIBRARY:
        r = res.get(spec["slug"]) or {}
        t = f"lib:{spec['slug']}"
        cn = (counters or {}).get(t) or {}
        out.append({"kind": "library", "slug": spec["slug"], "title": spec["name"], "summary": spec["summary"],
                    "category": spec["category"], "market": "both", "timeframe": "1h, 4h", "author": "Signals FM",
                    "tested": True, "badge": "By Signals FM", "headline": r.get("headline"),
                    "preview": _card_preview(r.get("preview")), "updated_at": r.get("updated_at") or meta.get("updated_at"),
                    "source": r.get("source") or meta.get("source"), "likes": cn.get("likes", 0),
                    "views": cn.get("views", 0), "liked": t in liked, "target": t, "url": f"/strategies/{spec['slug']}"})
    return out


def community_cards(include_hidden=False, uid=None):
    with _Cursor() as cur:
        if include_hidden:
            cur.execute("SELECT id, user_id, author, title, summary, description, rules, market, timeframe, category, "
                        "image_type, status, reports, created_at, updated_at FROM community_strategies "
                        "WHERE status IN ('visible','hidden') ORDER BY created_at DESC LIMIT 500")
        elif uid:
            cur.execute("SELECT id, user_id, author, title, summary, description, rules, market, timeframe, category, "
                        "image_type, status, reports, created_at, updated_at FROM community_strategies "
                        "WHERE status='visible' OR (status='hidden' AND user_id=%s) ORDER BY created_at DESC LIMIT 500",
                        (uid,))
        else:
            cur.execute("SELECT id, user_id, author, title, summary, description, rules, market, timeframe, category, "
                        "image_type, status, reports, created_at, updated_at FROM community_strategies "
                        "WHERE status='visible' ORDER BY created_at DESC LIMIT 500")
        return cur.fetchall() or []


def list_items(args, uid=None, admin=False):
    tab = (args.get("tab") or "all").lower()
    category = (args.get("category") or "").strip()
    market = (args.get("market") or "").strip().lower()
    tf = (args.get("timeframe") or "").strip().lower()
    sort = (args.get("sort") or "popular").lower()
    q = (args.get("q") or "").strip().lower()[:80]
    try:
        page = max(1, int(args.get("page") or 1))
    except ValueError:
        page = 1
    items = []
    lib = tab in ("all", "library")
    com = tab in ("all", "community")
    rows = community_cards(include_hidden=admin, uid=uid) if com else []
    targets = ([f"lib:{s['slug']}" for s in SL.LIBRARY] if lib else []) + [f"com:{r['id']}" for r in rows]
    counters = _counters(targets)
    liked = _liked_by(uid, targets)
    if lib:
        items += library_cards(counters, liked)
    for r in rows:
        items.append(_post_json(r, counters, f"com:{r['id']}" in liked))
    if category:
        items = [x for x in items if x["category"] == category]
    if market in ("crypto", "forex"):
        items = [x for x in items if x["market"] in (market, "both")]
    if tf:
        items = [x for x in items if x["kind"] == "library" and tf in ("1h", "4h") or x.get("timeframe") in (tf, "any")]
    if q:
        items = [x for x in items if q in (x["title"] + " " + x["summary"] + " " + x["author"]).lower()]
    if sort == "best":
        items.sort(key=lambda x: (x.get("headline") or {}).get("avg_r") if (x.get("headline") or {}).get("avg_r")
                   is not None else -99, reverse=True)
    elif sort == "newest":
        items.sort(key=lambda x: (x["kind"] == "community", x.get("created_at") or x.get("updated_at") or ""), reverse=True)
    else:
        items.sort(key=lambda x: (x.get("likes", 0) * 5 + x.get("views", 0), x["kind"] == "library"), reverse=True)
    total = len(items)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    return {"items": items[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], "total": total, "page": page, "pages": pages,
            "meta": _public_meta()}


def _public_meta():
    m = library_results().get("_meta") or {}
    return {"source": m.get("source"), "updated_at": m.get("updated_at"), "days": m.get("days"),
            "coins": len(m.get("coins") or []), "forex": len(m.get("forex") or []), "period": m.get("period"),
            "refresh_hours": refresh_hours()}


def library_detail(slug, uid=None):
    spec = SL.BY_SLUG.get(slug)
    if not spec:
        return None
    r = library_results().get(slug) or {}
    t = f"lib:{slug}"
    cn = _counters([t]).get(t) or {}
    return {"kind": "library", **SL.describe(spec), "tested": True, "badge": "By Signals FM",
            "results": r.get("results") or {}, "preview": r.get("preview"), "headline": r.get("headline"),
            "updated_at": r.get("updated_at"), "source": r.get("source"), "meta": _public_meta(),
            "likes": cn.get("likes", 0), "views": cn.get("views", 0), "liked": t in _liked_by(uid, [t]), "target": t}


# ===========================================================================
# routes
# ===========================================================================
def _need_login():
    return jsonify({"ok": False, "error": "Please log in first."}), 401


def _json_error(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


@strategies_bp.route("/strategies", methods=["GET"])
def strategies_page():
    return render_template("strategies.html", active="strategies", categories=SL.CATEGORIES,
                           community_categories=COMMUNITY_CATEGORIES, logged_in=bool(_uid()), admin=_is_admin())


@strategies_bp.route("/strategies/new", methods=["GET"])
def strategy_new_page():
    if not _uid():
        return redirect("/login?next=/strategies/new")
    return render_template("strategy-new.html", active="strategies", categories=COMMUNITY_CATEGORIES,
                           markets=MARKETS, timeframes=TIMEFRAMES, logged_in=True)


@strategies_bp.route("/strategies/<slug>", methods=["GET"])
def strategy_page(slug):
    spec = SL.BY_SLUG.get(slug)
    if not spec:
        return render_template("message.html", title="Strategy not found",
                               text="This strategy does not exist. See all strategies on the Strategies page.",
                               action_href="/strategies", action_text="All strategies"), 404
    count_view(f"lib:{slug}")
    return render_template("strategy.html", active="strategies", s=SL.describe(spec), logged_in=bool(_uid()),
                           admin=_is_admin())


@strategies_bp.route("/strategies/community/<int:pid>", methods=["GET"])
def community_page(pid):
    r = _post_row(pid)
    uid = _uid()
    admin = _is_admin()
    visible = r and (r["status"] == "visible" or (r["status"] == "hidden" and (admin or (uid and r["user_id"] == uid))))
    if not visible:
        return render_template("message.html", title="Strategy not found",
                               text="This community strategy does not exist or was removed.",
                               action_href="/strategies", action_text="All strategies"), 404
    count_view(f"com:{pid}")
    t = f"com:{pid}"
    post = _post_json(r, _counters([t]), t in _liked_by(uid, [t]), full=True)
    return render_template("strategy-community.html", active="strategies", p=post, logged_in=bool(uid), admin=admin,
                           mine=bool(uid and r["user_id"] == uid))


@strategies_bp.route("/strategies/image/<int:pid>", methods=["GET"])
def community_image(pid):
    with _Cursor() as cur:
        cur.execute("SELECT image, image_type, status, user_id FROM community_strategies WHERE id=%s", (pid,))
        r = cur.fetchone()
    if not r or not r.get("image") or r.get("image_type") not in ("image/png", "image/jpeg", "image/webp"):
        return Response("Not found", status=404, mimetype="text/plain")
    if r["status"] != "visible" and not (_is_admin() or (_uid() and r["user_id"] == _uid())):
        return Response("Not found", status=404, mimetype="text/plain")
    data = r["image"]
    if isinstance(data, memoryview):
        data = data.tobytes()
    resp = Response(bytes(data), mimetype=r["image_type"])
    resp.headers["Cache-Control"] = "public, max-age=86400"
    resp.headers["Content-Disposition"] = "inline"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return resp


@strategies_bp.route("/api/strategies", methods=["GET"])
def api_list():
    try:
        return jsonify({"ok": True, **list_items(request.args, _uid(), _is_admin())})
    except Exception as e:
        return _json_error(f"Strategies are not available right now: {str(e)[:100]}", 500)


@strategies_bp.route("/api/strategies/library/<slug>", methods=["GET"])
def api_library(slug):
    d = library_detail(slug, _uid())
    if not d:
        return _json_error("Unknown strategy.", 404)
    return jsonify({"ok": True, "strategy": d})


@strategies_bp.route("/api/strategies/community", methods=["POST"])
def api_create():
    uid = _uid()
    if not uid:
        return _need_login()
    form = request.form if request.form else (request.get_json(silent=True) or {})
    clean, err = validate_post(form, uid)
    if err:
        return _json_error(err)
    blob = itype = None
    f = request.files.get("image") if request.files else None
    if f and f.filename:
        blob = f.read(MAX_IMAGE_BYTES + 1)
        if len(blob) > MAX_IMAGE_BYTES:
            return _json_error("The image is larger than 1.5 MB.")
        itype = _image_type(blob)
        if not itype:
            return _json_error("The image must be a PNG, JPEG or WebP file.")
    now = _utcnow()
    with _Cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM community_strategies WHERE user_id=%s AND created_at >= %s",
                    (uid, now - timedelta(days=1)))
        if int((cur.fetchone() or {}).get("n") or 0) >= POSTS_PER_DAY:
            return _json_error(f"You can share up to {POSTS_PER_DAY} strategies a day. Please try again tomorrow.", 429)
        cur.execute("INSERT INTO community_strategies (user_id, author, title, summary, description, rules, market, "
                    "timeframe, category, image, image_type, status, reports, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'visible',0,%s,%s)",
                    (uid, clean["author"], clean["title"], clean["summary"], clean["description"], clean["rules"],
                     clean["market"], clean["timeframe"], clean["category"], blob, itype, now, now))
        pid = cur.lastrowid
    return jsonify({"ok": True, "id": pid, "url": f"/strategies/community/{pid}"})


@strategies_bp.route("/api/strategies/like", methods=["POST"])
def api_like():
    uid = _uid()
    if not uid:
        return _need_login()
    data = request.get_json(silent=True) or {}
    target = str(data.get("target") or "")
    m = re.fullmatch(r"lib:([a-z0-9-]{3,80})|com:(\d{1,10})", target)
    if not m or (m.group(1) and m.group(1) not in SL.BY_SLUG):
        return _json_error("Unknown strategy.", 404)
    if m.group(2):
        r = _post_row(int(m.group(2)))
        if not r or r["status"] != "visible":
            return _json_error("Unknown strategy.", 404)
    with _Cursor() as cur:
        cur.execute("SELECT target FROM strategy_votes WHERE target=%s AND user_id=%s AND kind='like'", (target, uid))
        if cur.fetchone():
            cur.execute("DELETE FROM strategy_votes WHERE target=%s AND user_id=%s AND kind='like'", (target, uid))
            _bump(target, "likes", -1, cur)
            liked = False
        else:
            cur.execute("INSERT INTO strategy_votes (target, user_id, kind, created_at) VALUES (%s,%s,'like',%s)",
                        (target, uid, _utcnow()))
            _bump(target, "likes", 1, cur)
            liked = True
        cur.execute("SELECT likes FROM strategy_counters WHERE target=%s", (target,))
        likes = int((cur.fetchone() or {}).get("likes") or 0)
    return jsonify({"ok": True, "liked": liked, "likes": likes})


@strategies_bp.route("/api/strategies/community/<int:pid>/report", methods=["POST"])
def api_report(pid):
    uid = _uid()
    if not uid:
        return _need_login()
    r = _post_row(pid)
    if not r or r["status"] != "visible":
        return _json_error("Unknown strategy.", 404)
    if r["user_id"] == uid:
        return _json_error("You cannot report your own strategy.")
    t = f"com:{pid}"
    with _Cursor() as cur:
        cur.execute("SELECT target FROM strategy_votes WHERE target=%s AND user_id=%s AND kind='report'", (t, uid))
        if cur.fetchone():
            return jsonify({"ok": True, "message": "You have already reported this strategy. Thank you."})
        cur.execute("INSERT INTO strategy_votes (target, user_id, kind, created_at) VALUES (%s,%s,'report',%s)",
                    (t, uid, _utcnow()))
        cur.execute("UPDATE community_strategies SET reports = reports + 1 WHERE id=%s", (pid,))
        cur.execute("SELECT reports FROM community_strategies WHERE id=%s", (pid,))
        n = int((cur.fetchone() or {}).get("reports") or 0)
        if n >= REPORTS_TO_HIDE:
            cur.execute("UPDATE community_strategies SET status='hidden' WHERE id=%s AND status='visible'", (pid,))
    return jsonify({"ok": True, "message": "Thank you. An admin will look at this strategy."})


@strategies_bp.route("/api/strategies/community/<int:pid>/delete", methods=["POST"])
def api_delete(pid):
    uid = _uid()
    if not uid:
        return _need_login()
    r = _post_row(pid)
    if not r or r["status"] == "deleted":
        return _json_error("Unknown strategy.", 404)
    if r["user_id"] != uid and not _is_admin():
        return _json_error("Only the author or an admin can delete this strategy.", 403)
    with _Cursor() as cur:
        cur.execute("UPDATE community_strategies SET status='deleted', image=NULL, updated_at=%s WHERE id=%s",
                    (_utcnow(), pid))
    return jsonify({"ok": True})


@strategies_bp.route("/api/strategies/community/<int:pid>/moderate", methods=["POST"])
def api_moderate(pid):
    if not _uid():
        return _need_login()
    if not _is_admin():
        return _json_error("Admins only.", 403)
    action = str((request.get_json(silent=True) or {}).get("action") or "")
    r = _post_row(pid)
    if not r or r["status"] == "deleted":
        return _json_error("Unknown strategy.", 404)
    with _Cursor() as cur:
        if action == "hide":
            cur.execute("UPDATE community_strategies SET status='hidden' WHERE id=%s", (pid,))
        elif action == "restore":
            cur.execute("UPDATE community_strategies SET status='visible', reports=0 WHERE id=%s", (pid,))
            cur.execute("DELETE FROM strategy_votes WHERE target=%s AND kind='report'", (f"com:{pid}",))
        elif action == "delete":
            cur.execute("UPDATE community_strategies SET status='deleted', image=NULL WHERE id=%s", (pid,))
        else:
            return _json_error("Unknown action.")
    return jsonify({"ok": True})


def sitemap_paths():
    return ["/strategies"] + [f"/strategies/{s['slug']}" for s in SL.LIBRARY]
