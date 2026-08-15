"""
Watchlist — Phase 1 upgrade feature.

Lets a logged-in user keep a personal list of symbols with a live price/
24h-change/volume snapshot and (when available) the last cached signal
direction + confidence for that symbol. Does not touch /signal, /candles,
/liquidity or the trade-tracking system - it only reads from them.

Routes:
    GET    /api/watchlist                 -> user's watchlist + live snapshot
    POST   /api/watchlist                 -> { "symbol": "BTC/USDT" } add
    DELETE /api/watchlist/<symbol>        -> remove
    POST   /api/watchlist/<symbol>/favorite -> { "favorite": true/false }
    POST   /api/watchlist/reorder         -> { "symbols": ["BTC/USDT", ...] }
"""

from flask import Blueprint, request, jsonify, session

from db import get_db_connection

watchlist_bp = Blueprint("watchlist", __name__)


def _require_login():
    return session.get("user_id")


def _get_exchange():
    # Lazy import: main.py imports trades_bp/alerts_bp/watchlist_bp at
    # module load time, before `exchange` (ccxt.okx()) is created, so we
    # only reach into main for it at request time (same pattern trades.py
    # already uses for get_live_price).
    from main import exchange
    return exchange


def _get_cached_signal(symbol):
    try:
        from main import get_cached_signal
        return get_cached_signal(symbol, max_age_sec=600)
    except Exception:
        return None


@watchlist_bp.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT symbol, is_favorite, sort_order FROM watchlist "
                "WHERE user_id = %s ORDER BY sort_order ASC, id ASC",
                (user_id,),
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    symbols = [r["symbol"] for r in rows]
    tickers = {}
    tickers_error = None
    if symbols:
        try:
            exchange = _get_exchange()
            tickers = exchange.fetch_tickers(symbols)
        except Exception as e:
            # Never fail the whole watchlist because live price fetch
            # failed - show the list with an explicit "data unavailable"
            # per row instead of a fake 0.
            tickers_error = str(e)

    out = []
    for r in rows:
        sym = r["symbol"]
        t = tickers.get(sym) if tickers else None
        cached = _get_cached_signal(sym)
        out.append({
            "symbol": sym,
            "is_favorite": bool(r["is_favorite"]),
            "last_price": t.get("last") if t else None,
            "change_percent": t.get("percentage") if t else None,
            "volume": t.get("quoteVolume") if t else None,
            "data_unavailable": t is None,
            "signal_direction": cached.get("final_verdict") if cached else None,
            "signal_confidence": cached.get("confidence_pct") if cached else None,
            "funding_rate": (cached.get("funding_open_interest") or {}).get("funding_rate_pct")
                if cached and isinstance(cached.get("funding_open_interest"), dict) else None,
        })

    resp = {"watchlist": out}
    if tickers_error and symbols:
        resp["warning"] = "Some live prices could not be fetched."
    return jsonify(resp)


@watchlist_bp.route("/api/watchlist", methods=["POST"])
def add_to_watchlist():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    if not symbol or "/" not in symbol:
        return jsonify({"error": "A valid symbol (e.g. BTC/USDT) is required"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS c FROM watchlist WHERE user_id = %s", (user_id,)
            )
            count = cursor.fetchone()["c"]
            if count >= 100:
                return jsonify({"error": "Watchlist limit reached (100 symbols)"}), 400

            cursor.execute(
                "SELECT id FROM watchlist WHERE user_id = %s AND symbol = %s",
                (user_id, symbol),
            )
            if cursor.fetchone():
                return jsonify({"error": "Symbol already in your watchlist"}), 409

            cursor.execute(
                "INSERT INTO watchlist (user_id, symbol, sort_order) VALUES (%s, %s, %s)",
                (user_id, symbol, count),
            )
        return jsonify({"message": "Added to watchlist", "symbol": symbol}), 201
    finally:
        conn.close()


@watchlist_bp.route("/api/watchlist/<path:symbol>", methods=["DELETE"])
def remove_from_watchlist(symbol):
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    symbol = symbol.strip().upper()
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM watchlist WHERE user_id = %s AND symbol = %s",
                (user_id, symbol),
            )
            if cursor.rowcount == 0:
                return jsonify({"error": "Symbol not found in your watchlist"}), 404
        return jsonify({"message": "Removed from watchlist"}), 200
    finally:
        conn.close()


@watchlist_bp.route("/api/watchlist/<path:symbol>/favorite", methods=["POST"])
def toggle_favorite(symbol):
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    favorite = bool(data.get("favorite"))
    symbol = symbol.strip().upper()

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE watchlist SET is_favorite = %s WHERE user_id = %s AND symbol = %s",
                (1 if favorite else 0, user_id, symbol),
            )
            if cursor.rowcount == 0:
                return jsonify({"error": "Symbol not found in your watchlist"}), 404
        return jsonify({"message": "Updated"}), 200
    finally:
        conn.close()


@watchlist_bp.route("/api/watchlist/reorder", methods=["POST"])
def reorder_watchlist():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        return jsonify({"error": "symbols must be a non-empty list"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            for idx, sym in enumerate(symbols):
                cursor.execute(
                    "UPDATE watchlist SET sort_order = %s WHERE user_id = %s AND symbol = %s",
                    (idx, user_id, (sym or "").strip().upper()),
                )
        return jsonify({"message": "Reordered"}), 200
    finally:
        conn.close()
