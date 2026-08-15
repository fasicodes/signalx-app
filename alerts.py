"""
Price / Indicator / Signal / Liquidity Alerts + Notification Center.

Phase 1 upgrade feature. Fully additive - does not touch the signal engine,
liquidity scanner, or trade-tracking system; it only reads their outputs.

ALERT TYPES
    Price:      PRICE_ABOVE, PRICE_BELOW, PRICE_REACHES
    Indicator:  RSI_ABOVE, RSI_BELOW, MACD_BULLISH_CROSS, MACD_BEARISH_CROSS
    Signal:     NEW_LONG_SIGNAL, NEW_SHORT_SIGNAL, CONFIDENCE_ABOVE
    Liquidity:  LIQUIDITY_WALL, LIQUIDITY_IMBALANCE,
                SUPPORT_LIQUIDITY, RESISTANCE_LIQUIDITY

Debounce/cooldown: price alerts are one-shot (they disable themselves once
triggered, like a classic "notify me when X happens" alert). Recurring
alert types (RSI/MACD/signal/liquidity) use a cooldown so the same
condition can't spam a notification more than once per COOLDOWN_MINUTES.

Routes:
    POST   /api/alerts                    -> create
    GET    /api/alerts                    -> list (optional ?status=)
    PUT    /api/alerts/<id>                -> edit target_value / is_enabled
    POST   /api/alerts/<id>/toggle         -> enable/disable
    DELETE /api/alerts/<id>                -> delete
    GET    /api/alerts/check               -> evaluate now, create notifications

    GET    /api/notifications              -> list (optional ?unread_only=true&limit=)
    GET    /api/notifications/unread-count
    POST   /api/notifications/<id>/read
    POST   /api/notifications/read-all
"""

from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, session

from db import get_db_connection

alerts_bp = Blueprint("alerts", __name__)

COOLDOWN_MINUTES = 30

PRICE_TYPES = {"PRICE_ABOVE", "PRICE_BELOW", "PRICE_REACHES"}
INDICATOR_TYPES = {"RSI_ABOVE", "RSI_BELOW", "MACD_BULLISH_CROSS", "MACD_BEARISH_CROSS"}
SIGNAL_TYPES = {"NEW_LONG_SIGNAL", "NEW_SHORT_SIGNAL", "CONFIDENCE_ABOVE"}
LIQUIDITY_TYPES = {"LIQUIDITY_WALL", "LIQUIDITY_IMBALANCE", "SUPPORT_LIQUIDITY", "RESISTANCE_LIQUIDITY"}
ALL_TYPES = PRICE_TYPES | INDICATOR_TYPES | SIGNAL_TYPES | LIQUIDITY_TYPES

_NEEDS_TARGET = ALL_TYPES - {
    "MACD_BULLISH_CROSS", "MACD_BEARISH_CROSS", "NEW_LONG_SIGNAL", "NEW_SHORT_SIGNAL",
    "LIQUIDITY_WALL", "LIQUIDITY_IMBALANCE",
}


def _require_login():
    return session.get("user_id")


def _category_for(alert_type):
    if alert_type in PRICE_TYPES:
        return "PRICE"
    if alert_type in INDICATOR_TYPES or alert_type in SIGNAL_TYPES:
        return "SIGNAL"
    if alert_type in LIQUIDITY_TYPES:
        return "LIQUIDITY"
    return "SYSTEM"


# ---------------------------------------------------------------------
# Market-data helpers - all lazy imports from main.py (main.py imports
# this blueprint at module load time, before `exchange`/functions below
# exist yet, so we only reach into main at request time - same pattern
# trades.py already uses for get_live_price).
# ---------------------------------------------------------------------

def _get_live_price(symbol):
    try:
        from main import get_live_price
        return get_live_price(symbol)
    except Exception:
        return None


def _get_rsi_macd(symbol):
    """Returns (rsi, macd, macd_signal) off the 1h candles, or (None, None,
    None) if candle data isn't available - reuses main.py's own RSI/MACD
    helpers so the numbers match what the rest of the app shows."""
    try:
        from main import get_candles, _HAS_PANDAS_TA, ta, _ta_rsi, _ta_macd
        df = get_candles(symbol=symbol, timeframe="1h", limit=100)
        if _HAS_PANDAS_TA:
            rsi_series = ta.rsi(df["close"], length=14)
            macd_df = ta.macd(df["close"])
            macd_series = macd_df.iloc[:, 0] if macd_df is not None else None
            signal_series = macd_df.iloc[:, 2] if macd_df is not None else None
        else:
            rsi_series = _ta_rsi(df["close"], 14)
            macd_series, signal_series = _ta_macd(df["close"])
        rsi = float(rsi_series.dropna().iloc[-1]) if rsi_series is not None and not rsi_series.dropna().empty else None
        macd = float(macd_series.dropna().iloc[-1]) if macd_series is not None and not macd_series.dropna().empty else None
        macd_sig = float(signal_series.dropna().iloc[-1]) if signal_series is not None and not signal_series.dropna().empty else None
        return rsi, macd, macd_sig
    except Exception:
        return None, None, None


def _get_cached_signal(symbol):
    try:
        from main import get_cached_signal
        return get_cached_signal(symbol, max_age_sec=600)
    except Exception:
        return None


def _get_liquidity_snapshot(symbol):
    """Lightweight liquidity read (order book + wall zones only - NOT the
    full expensive /liquidity route) so alert polling stays cheap."""
    try:
        from main import exchange, _clean_order_book, liquidity_target_zones, order_book_depth_profile
        ob = _clean_order_book(exchange.fetch_order_book(symbol, limit=50))
        current_price = (ob["bids"][0][0] + ob["asks"][0][0]) / 2 if ob["bids"] and ob["asks"] else None
        zones = liquidity_target_zones(ob, current_price) if current_price else []
        depth = order_book_depth_profile(symbol, depth=20, order_book=ob)
        return {"zones": zones, "depth": depth, "price": current_price}
    except Exception:
        return None


def _create_notification(cursor, user_id, category, symbol, title, message, alert_id=None):
    cursor.execute(
        "INSERT INTO notifications (user_id, category, symbol, title, message, alert_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, category, symbol, title, message, alert_id),
    )


def _serialize_alert(a):
    return {
        "id": a["id"],
        "symbol": a["symbol"],
        "alert_type": a["alert_type"],
        "target_value": a["target_value"],
        "is_enabled": bool(a["is_enabled"]),
        "status": a["status"],
        "last_checked_value": a.get("last_checked_value"),
        "created_at": a["created_at"].isoformat() if a.get("created_at") else None,
        "triggered_at": a["triggered_at"].isoformat() if a.get("triggered_at") else None,
    }


# ---------------------------------------------------------------------
# Alert CRUD
# ---------------------------------------------------------------------

@alerts_bp.route("/api/alerts", methods=["POST"])
def create_alert():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    alert_type = (data.get("alert_type") or "").strip().upper()
    target_value = data.get("target_value")

    if not symbol or "/" not in symbol:
        return jsonify({"error": "A valid symbol (e.g. BTC/USDT) is required"}), 400
    if alert_type not in ALL_TYPES:
        return jsonify({"error": f"Unknown alert_type. Must be one of: {sorted(ALL_TYPES)}"}), 400
    if alert_type in _NEEDS_TARGET:
        if target_value is None:
            return jsonify({"error": "target_value is required for this alert type"}), 400
        try:
            target_value = float(target_value)
        except (TypeError, ValueError):
            return jsonify({"error": "target_value must be a number"}), 400
    else:
        target_value = None

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS c FROM alerts WHERE user_id = %s", (user_id,))
            if cursor.fetchone()["c"] >= 200:
                return jsonify({"error": "Alert limit reached (200)"}), 400

            cursor.execute(
                "INSERT INTO alerts (user_id, symbol, alert_type, target_value) VALUES (%s, %s, %s, %s)",
                (user_id, symbol, alert_type, target_value),
            )
            alert_id = cursor.lastrowid
            cursor.execute("SELECT * FROM alerts WHERE id = %s", (alert_id,))
            alert = cursor.fetchone()
        return jsonify(_serialize_alert(alert)), 201
    finally:
        conn.close()


@alerts_bp.route("/api/alerts", methods=["GET"])
def list_alerts():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    status = request.args.get("status")
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            if status:
                cursor.execute(
                    "SELECT * FROM alerts WHERE user_id = %s AND status = %s ORDER BY created_at DESC",
                    (user_id, status.upper()),
                )
            else:
                cursor.execute(
                    "SELECT * FROM alerts WHERE user_id = %s ORDER BY created_at DESC", (user_id,)
                )
            rows = cursor.fetchall()
        return jsonify({"alerts": [_serialize_alert(a) for a in rows]})
    finally:
        conn.close()


@alerts_bp.route("/api/alerts/<int:alert_id>", methods=["PUT"])
def update_alert(alert_id):
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM alerts WHERE id = %s AND user_id = %s", (alert_id, user_id)
            )
            alert = cursor.fetchone()
            if not alert:
                return jsonify({"error": "Alert not found"}), 404

            updates, params = [], []
            if "target_value" in data and data["target_value"] is not None:
                try:
                    updates.append("target_value = %s")
                    params.append(float(data["target_value"]))
                except (TypeError, ValueError):
                    return jsonify({"error": "target_value must be a number"}), 400
            if "is_enabled" in data:
                updates.append("is_enabled = %s")
                params.append(1 if data["is_enabled"] else 0)
                # Re-enabling a one-shot triggered alert resets it to ACTIVE
                if data["is_enabled"] and alert["status"] == "TRIGGERED":
                    updates.append("status = %s")
                    params.append("ACTIVE")

            if not updates:
                return jsonify({"error": "Nothing to update"}), 400

            params.extend([alert_id, user_id])
            cursor.execute(
                f"UPDATE alerts SET {', '.join(updates)} WHERE id = %s AND user_id = %s", params
            )
            cursor.execute("SELECT * FROM alerts WHERE id = %s", (alert_id,))
            alert = cursor.fetchone()
        return jsonify(_serialize_alert(alert))
    finally:
        conn.close()


@alerts_bp.route("/api/alerts/<int:alert_id>/toggle", methods=["POST"])
def toggle_alert(alert_id):
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM alerts WHERE id = %s AND user_id = %s", (alert_id, user_id)
            )
            alert = cursor.fetchone()
            if not alert:
                return jsonify({"error": "Alert not found"}), 404

            new_enabled = 0 if alert["is_enabled"] else 1
            new_status = "ACTIVE" if new_enabled else "DISABLED"
            if new_enabled and alert["status"] == "DISABLED":
                new_status = "ACTIVE"
            cursor.execute(
                "UPDATE alerts SET is_enabled = %s, status = %s WHERE id = %s",
                (new_enabled, new_status, alert_id),
            )
        return jsonify({"message": "Updated", "is_enabled": bool(new_enabled)})
    finally:
        conn.close()


@alerts_bp.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
def delete_alert(alert_id):
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM alerts WHERE id = %s AND user_id = %s", (alert_id, user_id))
            if cursor.rowcount == 0:
                return jsonify({"error": "Alert not found"}), 404
        return jsonify({"message": "Deleted"})
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------

@alerts_bp.route("/api/alerts/check", methods=["GET"])
def check_alerts():
    """Evaluates all of the current user's enabled alerts against live
    data and creates a notification for each newly-triggered one. Meant
    to be polled by the frontend every ~45-60s while the user is on the
    site (never server-side background polling, to avoid uncontrolled
    resource use per-user)."""
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    triggered = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM alerts WHERE user_id = %s AND is_enabled = 1 AND status != 'TRIGGERED'",
                (user_id,),
            )
            active_alerts = cursor.fetchall()
            if not active_alerts:
                return jsonify({"triggered": [], "checked": 0})

            now = datetime.utcnow()
            cooldown = timedelta(minutes=COOLDOWN_MINUTES)

            # Group by symbol so we only hit market data once per symbol
            # per check, not once per alert.
            by_symbol = {}
            for a in active_alerts:
                by_symbol.setdefault(a["symbol"], []).append(a)

            for symbol, alert_list in by_symbol.items():
                needs_price = any(a["alert_type"] in PRICE_TYPES for a in alert_list)
                needs_indicator = any(a["alert_type"] in INDICATOR_TYPES for a in alert_list)
                needs_signal = any(a["alert_type"] in SIGNAL_TYPES for a in alert_list)
                needs_liquidity = any(a["alert_type"] in LIQUIDITY_TYPES for a in alert_list)

                price = _get_live_price(symbol) if needs_price else None
                rsi = macd = macd_sig = None
                if needs_indicator:
                    rsi, macd, macd_sig = _get_rsi_macd(symbol)
                cached_signal = _get_cached_signal(symbol) if needs_signal else None
                liquidity = _get_liquidity_snapshot(symbol) if needs_liquidity else None

                for a in alert_list:
                    t = a["alert_type"]
                    on_cooldown = a["last_triggered_at"] and (now - a["last_triggered_at"] < cooldown)
                    fired, title, message, new_last_value, new_direction = False, None, None, a.get("last_checked_value"), a.get("last_direction")

                    if t in PRICE_TYPES and price is not None:
                        new_last_value = price
                        prev = a.get("last_checked_value")
                        target = a["target_value"]
                        if t == "PRICE_ABOVE" and price >= target:
                            fired = True
                        elif t == "PRICE_BELOW" and price <= target:
                            fired = True
                        elif t == "PRICE_REACHES" and prev is not None and (
                            (prev < target <= price) or (prev > target >= price)
                        ):
                            fired = True
                        if fired:
                            title = f"{symbol} price alert"
                            message = f"{symbol} price reached {price:.6g} (target {target:.6g})."

                    elif t == "RSI_ABOVE" and rsi is not None and not on_cooldown:
                        if rsi >= a["target_value"]:
                            fired = True
                            title = f"{symbol} RSI above {a['target_value']}"
                            message = f"{symbol} RSI is {rsi:.1f}, above your {a['target_value']} threshold."
                        new_last_value = rsi
                    elif t == "RSI_BELOW" and rsi is not None and not on_cooldown:
                        if rsi <= a["target_value"]:
                            fired = True
                            title = f"{symbol} RSI below {a['target_value']}"
                            message = f"{symbol} RSI is {rsi:.1f}, below your {a['target_value']} threshold."
                        new_last_value = rsi

                    elif t in ("MACD_BULLISH_CROSS", "MACD_BEARISH_CROSS") and macd is not None and macd_sig is not None:
                        cross_state = "BULL" if macd > macd_sig else "BEAR"
                        prev_state = a.get("last_direction")
                        if prev_state and prev_state != cross_state and not on_cooldown:
                            if t == "MACD_BULLISH_CROSS" and cross_state == "BULL":
                                fired = True
                                title = f"{symbol} MACD bullish crossover"
                                message = f"{symbol} MACD just crossed above its signal line."
                            elif t == "MACD_BEARISH_CROSS" and cross_state == "BEAR":
                                fired = True
                                title = f"{symbol} MACD bearish crossover"
                                message = f"{symbol} MACD just crossed below its signal line."
                        new_direction = cross_state
                        new_last_value = macd

                    elif t in ("NEW_LONG_SIGNAL", "NEW_SHORT_SIGNAL") and cached_signal:
                        verdict = cached_signal.get("final_verdict")
                        prev_direction = a.get("last_direction")
                        wanted = "LONG" if t == "NEW_LONG_SIGNAL" else "SHORT"
                        if verdict == wanted and prev_direction != verdict and not on_cooldown:
                            fired = True
                            title = f"{symbol} new {wanted} signal"
                            conf = cached_signal.get("confidence_pct")
                            conf_txt = f" ({conf}% confidence)" if conf is not None else ""
                            message = f"Signal FM issued a new {wanted} signal for {symbol}{conf_txt}."
                        if verdict:
                            new_direction = verdict
                    elif t == "CONFIDENCE_ABOVE" and cached_signal and not on_cooldown:
                        conf = cached_signal.get("confidence_pct")
                        if conf is not None and conf >= a["target_value"]:
                            fired = True
                            title = f"{symbol} confidence above {a['target_value']}%"
                            message = f"{symbol} signal confidence is {conf}%, above your {a['target_value']}% threshold."
                        new_last_value = conf

                    elif t == "LIQUIDITY_WALL" and liquidity and not on_cooldown:
                        zones = liquidity.get("zones") or []
                        big = next((z for z in zones if z["score"] >= 80), None)
                        if big:
                            fired = True
                            title = f"{symbol} liquidity wall detected"
                            message = (f"Large {big['side'].replace('_', ' ').lower()} of "
                                       f"~${big['usd_size']:,.0f} near {big['price']:.6g} "
                                       f"({big['distance_pct']:+.2f}% away).")
                    elif t == "LIQUIDITY_IMBALANCE" and liquidity and not on_cooldown:
                        wall_bias = (liquidity.get("depth") or {}).get("wall_bias")
                        if wall_bias in ("BUY", "SELL", "BULLISH", "BEARISH"):
                            fired = True
                            title = f"{symbol} liquidity imbalance"
                            message = f"Order-book depth is currently skewed {wall_bias} for {symbol}."
                    elif t == "SUPPORT_LIQUIDITY" and liquidity and not on_cooldown:
                        zones = liquidity.get("zones") or []
                        major = next((z for z in zones if z["side"] == "BUY_WALL" and z["score"] >= a["target_value"]), None)
                        if major:
                            fired = True
                            title = f"{symbol} major support liquidity"
                            message = f"Major buy-side liquidity (score {major['score']}) near {major['price']:.6g}."
                    elif t == "RESISTANCE_LIQUIDITY" and liquidity and not on_cooldown:
                        zones = liquidity.get("zones") or []
                        major = next((z for z in zones if z["side"] == "SELL_WALL" and z["score"] >= a["target_value"]), None)
                        if major:
                            fired = True
                            title = f"{symbol} major resistance liquidity"
                            message = f"Major sell-side liquidity (score {major['score']}) near {major['price']:.6g}."

                    # --- apply result ---
                    one_shot = t in PRICE_TYPES
                    if fired:
                        _create_notification(cursor, user_id, _category_for(t), symbol, title, message, a["id"])
                        triggered.append({"alert_id": a["id"], "symbol": symbol, "title": title, "message": message})
                        set_clause = "last_triggered_at = %s, triggered_at = %s, last_checked_value = %s, last_direction = %s"
                        params = [now, now, new_last_value, new_direction]
                        if one_shot:
                            set_clause += ", is_enabled = 0, status = 'TRIGGERED'"
                        cursor.execute(f"UPDATE alerts SET {set_clause} WHERE id = %s", params + [a["id"]])
                    else:
                        cursor.execute(
                            "UPDATE alerts SET last_checked_value = %s, last_direction = %s WHERE id = %s",
                            (new_last_value, new_direction, a["id"]),
                        )

        return jsonify({"triggered": triggered, "checked": len(active_alerts)})
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Notification Center
# ---------------------------------------------------------------------

@alerts_bp.route("/api/notifications", methods=["GET"])
def list_notifications():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    unread_only = request.args.get("unread_only", "").lower() == "true"
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except ValueError:
        limit = 50

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            if unread_only:
                cursor.execute(
                    "SELECT * FROM notifications WHERE user_id = %s AND is_read = 0 "
                    "ORDER BY created_at DESC LIMIT %s",
                    (user_id, limit),
                )
            else:
                cursor.execute(
                    "SELECT * FROM notifications WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                    (user_id, limit),
                )
            rows = cursor.fetchall()
        out = [{
            "id": n["id"],
            "category": n["category"],
            "symbol": n["symbol"],
            "title": n["title"],
            "message": n["message"],
            "is_read": bool(n["is_read"]),
            "created_at": n["created_at"].isoformat() if n.get("created_at") else None,
        } for n in rows]
        return jsonify({"notifications": out})
    finally:
        conn.close()


@alerts_bp.route("/api/notifications/unread-count", methods=["GET"])
def unread_count():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS c FROM notifications WHERE user_id = %s AND is_read = 0", (user_id,)
            )
            count = cursor.fetchone()["c"]
        return jsonify({"unread_count": count})
    finally:
        conn.close()


@alerts_bp.route("/api/notifications/<int:notif_id>/read", methods=["POST"])
def mark_read(notif_id):
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE notifications SET is_read = 1 WHERE id = %s AND user_id = %s",
                (notif_id, user_id),
            )
            if cursor.rowcount == 0:
                return jsonify({"error": "Notification not found"}), 404
        return jsonify({"message": "Marked as read"})
    finally:
        conn.close()


@alerts_bp.route("/api/notifications/read-all", methods=["POST"])
def mark_all_read():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE notifications SET is_read = 1 WHERE user_id = %s AND is_read = 0", (user_id,)
            )
        return jsonify({"message": "All marked as read"})
    finally:
        conn.close()
