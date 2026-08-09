"""
Active Trade Tracking & Guidance System.

Jab user ek signal lekar trade "track" karta hai, hum ek Active Trade
record banate hain aur usay track karte rehte hain jab tak wo close na ho
(TP hit, SL hit, manually close, ya setup invalidated).

"GET SIGNAL" dobara dabane se agar active trade maujood hai to naya signal
us active trade ko replace NAHI karta - dono alag reh kar dikhaye jate hain.

Routes:
    POST /api/trades/track          -> naya active trade banata hai
    GET  /api/trades/active         -> user ke saare ACTIVE trades (live re-evaluate ho kar)
    GET  /api/trades/history        -> user ke saare closed trades
    GET  /api/trades/<id>           -> ek trade ka poora detail + timeline
    POST /api/trades/<id>/close     -> trade manually close karta hai
"""

import json
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, session

from db import get_db_connection
import condition_engine

trades_bp = Blueprint("trades", __name__)

# "1 Hour" jaisi label ko minutes mein convert karne ke liye
_HOLDING_PERIOD_MINUTES = {
    "15 Minutes": 15, "30 Minutes": 30, "1 Hour": 60,
    "4 Hours": 240, "1 Day": 1440,
}


def _require_login():
    return session.get("user_id")


def _get_live_price(asset):
    """Asset ka current price nikalta hai - existing exchange connection
    reuse karte hain (koi naya market-data system nahi banate)."""
    try:
        from main import exchange
        ticker = exchange.fetch_ticker(asset)
        return float(ticker["last"])
    except Exception:
        return None


def _calc_pnl(direction, entry_price, current_price, position_size):
    """LONG/SHORT ke liye estimated P/L (dollars + percent)."""
    if direction == "LONG":
        pnl_pct = (current_price - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - current_price) / entry_price
    pnl_dollars = pnl_pct * position_size
    return round(pnl_dollars, 2), round(pnl_pct * 100, 3)


def _add_event(cursor, trade_id, event_type, message, price=None, pnl=None):
    cursor.execute(
        "INSERT INTO trade_events (trade_id, event_type, message, price, pnl) VALUES (%s,%s,%s,%s,%s)",
        (trade_id, event_type, message, price, pnl),
    )


def _evaluate_trade(cursor, trade):
    """Ek ACTIVE trade ko current price ke against evaluate karta hai:
    - P/L recalculate
    - TP/SL hit check
    - Holding period reached check (auto-close NAHI karta)
    - Near-SL / near-TP warnings (ek hi baar)
    Trade dict ko updated values ke sath return karta hai."""
    if trade["status"] != "ACTIVE":
        return trade

    current_price = _get_live_price(trade["asset"])
    if current_price is None:
        # Market data unavailable - purani values hi wapas bhej dete hain,
        # fake price kabhi nahi dikhate.
        return trade

    direction = trade["direction"]
    pnl, pnl_pct = _calc_pnl(direction, trade["entry_price"], current_price, trade["position_size"])

    updates = {"last_price": current_price, "estimated_pnl": pnl, "estimated_pnl_percent": pnl_pct}
    new_status = "ACTIVE"
    exit_reason = None

    sl, tp = trade.get("stop_loss"), trade.get("take_profit")

    # --- TP / SL hit check ---
    if direction == "LONG":
        if tp and current_price >= tp:
            new_status, exit_reason = "TARGET_REACHED", "TARGET_REACHED"
        elif sl and current_price <= sl:
            new_status, exit_reason = "STOP_LOSS_REACHED", "STOP_LOSS_REACHED"
    else:  # SHORT
        if tp and current_price <= tp:
            new_status, exit_reason = "TARGET_REACHED", "TARGET_REACHED"
        elif sl and current_price >= sl:
            new_status, exit_reason = "STOP_LOSS_REACHED", "STOP_LOSS_REACHED"

    if new_status != "ACTIVE":
        updates["status"] = new_status
        updates["exit_reason"] = exit_reason
        updates["exit_price"] = current_price
        updates["closed_at"] = datetime.utcnow()
        msg = ("The defined take-profit level has been reached. The tracked trade is now marked as completed."
               if new_status == "TARGET_REACHED" else
               "The defined stop-loss level has been reached. The tracked trade is now marked as completed.")
        _add_event(cursor, trade["id"], new_status, msg, current_price, pnl)
    else:
        # --- Signal FM: Unified Trade Condition Detection ---
        # Classifies the trade into ONE primary condition (drawdown,
        # recovery, TP/SL approaching, momentum weakening, etc.) and
        # only changes the locked guidance message when the condition
        # has *materially* and *repeatedly* changed - never on a single
        # small price tick. See condition_engine.py for the full state
        # machine. This replaces the old one-off near-TP/near-SL flags.
        condition_engine.evaluate(cursor, trade, current_price, pnl, pnl_pct, updates)

        # --- Holding period reached (evaluate setup, do NOT auto-close) ---
        holding_minutes = trade.get("holding_period_minutes")
        if holding_minutes and not trade["holding_period_notified"]:
            elapsed = (datetime.utcnow() - trade["created_at"]).total_seconds() / 60
            if elapsed >= holding_minutes:
                _add_event(
                    cursor, trade["id"], "HOLDING_PERIOD_REACHED",
                    "The original recommended holding period has been reached. The trade has not reached TP or SL yet. The current setup is being evaluated.",
                    current_price, pnl,
                )
                updates["holding_period_notified"] = 1

                # Setup ko re-evaluate karte hain - existing signal engine reuse
                # karke (naya market-data system nahi banate).
                try:
                    from main import get_candles, generate_signal
                    df = get_candles(symbol=trade["asset"], timeframe="1h")
                    fresh = generate_signal(df, symbol=trade["asset"], include_orderbook=False)
                    still_valid = (fresh.get("final_verdict") == direction)
                except Exception:
                    still_valid = True  # Uncertain hone par cautious rehte hain, invalidate nahi karte

                if still_valid:
                    _add_event(
                        cursor, trade["id"], "SETUP_VALID",
                        "The original trade setup remains valid according to the current analysis. No new signal is being created for this active trade.",
                        current_price, pnl,
                    )
                else:
                    # IMPORTANT: keep the single-guidance-message invariant.
                    # Only setting `status` here (without touching
                    # trade_condition/condition_message) used to leave a
                    # stale guidance message (e.g. "stay focused, follow
                    # your plan") displayed on a trade whose status badge
                    # now says SETUP_INVALIDATED - two contradictory
                    # signals shown together. Overwrite the locked
                    # condition too so both stay in sync.
                    updates["status"] = "SETUP_INVALIDATED"
                    updates["trade_condition"] = "SETUP_INVALIDATED"
                    updates["condition_message"] = condition_engine.CONDITION_MESSAGES["SETUP_INVALIDATED"]
                    updates["condition_started_at"] = datetime.utcnow()
                    updates["condition_version"] = (trade.get("condition_version") or 0) + 1
                    updates["pending_condition"] = None
                    updates["pending_condition_count"] = 0
                    _add_event(
                        cursor, trade["id"], "SETUP_INVALIDATED",
                        "The original trade setup is no longer valid according to the current model. Review your risk plan before taking further action.",
                        current_price, pnl,
                    )

    updates["last_updated"] = datetime.utcnow()
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    cursor.execute(f"UPDATE active_trades SET {set_clause} WHERE id = %s", (*updates.values(), trade["id"]))

    trade.update(updates)
    return trade


def _serialize_trade(t):
    return {
        "id": t["id"],
        "asset": t["asset"],
        "direction": t["direction"],
        "entry_price": t["entry_price"],
        "position_size": t["position_size"],
        "stop_loss": t["stop_loss"],
        "take_profit": t["take_profit"],
        "leverage": t.get("leverage"),
        "holding_period_label": t.get("holding_period_label"),
        "status": t["status"],
        "last_price": t.get("last_price"),
        "estimated_pnl": t.get("estimated_pnl"),
        "estimated_pnl_percent": t.get("estimated_pnl_percent"),
        "created_at": t["created_at"].isoformat() if t.get("created_at") else None,
        "last_updated": t["last_updated"].isoformat() if t.get("last_updated") else None,
        "closed_at": t["closed_at"].isoformat() if t.get("closed_at") else None,
        "exit_price": t.get("exit_price"),
        "exit_reason": t.get("exit_reason"),
        # Signal FM - stable primary guidance (see condition_engine.py)
        "trade_condition": t.get("trade_condition"),
        "condition_message": t.get("condition_message"),
        "condition_started_at": t["condition_started_at"].isoformat() if t.get("condition_started_at") else None,
    }


@trades_bp.route("/api/trades/track", methods=["POST"])
def track_trade():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    asset = data.get("asset")
    direction = data.get("direction")
    entry_price = data.get("entry_price")
    position_size = data.get("position_size")

    if not asset or direction not in ("LONG", "SHORT") or not entry_price or not position_size:
        return jsonify({"error": "asset, direction, entry_price, and position_size are required"}), 400

    # Asset ko whitelist ke against validate karte hain - koi bhi arbitrary
    # string yahan se aage (aur baad mein UI mein) nahi jaani chahiye.
    from main import AVAILABLE_COINS
    if asset not in AVAILABLE_COINS:
        return jsonify({"error": "Unsupported asset"}), 400
    try:
        entry_price = float(entry_price)
        position_size = float(position_size)
        if position_size <= 0 or entry_price <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "entry_price and position_size must be positive numbers"}), 400

    stop_loss = data.get("stop_loss")
    take_profit = data.get("take_profit")
    leverage = data.get("leverage")
    holding_period_label = data.get("holding_period_label", "1 Hour")
    holding_minutes = _HOLDING_PERIOD_MINUTES.get(holding_period_label, 60)
    signal_snapshot = json.dumps(data.get("signal_snapshot") or {})

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # User ka is asset par pehle se koi ACTIVE trade to nahi (ek asset
            # par ek waqt mein ek hi tracked trade - confusion se bachne ke liye)
            cursor.execute(
                "SELECT id FROM active_trades WHERE user_id=%s AND asset=%s AND status='ACTIVE'",
                (user_id, asset),
            )
            if cursor.fetchone():
                return jsonify({"error": "You already have an active trade for this asset"}), 409

            cursor.execute(
                """INSERT INTO active_trades
                   (user_id, asset, direction, entry_price, position_size, stop_loss, take_profit,
                    leverage, holding_period_label, holding_period_minutes, signal_snapshot, status, last_price)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',%s)""",
                (user_id, asset, direction, entry_price, position_size, stop_loss, take_profit,
                 leverage, holding_period_label, holding_minutes, signal_snapshot, entry_price),
            )
            trade_id = cursor.lastrowid
            _add_event(cursor, trade_id, "TRADE_STARTED", "Trade tracking started.", entry_price, 0)
        return jsonify({"message": "Trade is now being tracked", "trade_id": trade_id}), 201
    finally:
        conn.close()


@trades_bp.route("/api/trades/active", methods=["GET"])
def list_active_trades():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM active_trades WHERE user_id=%s AND status='ACTIVE' ORDER BY created_at DESC",
                (user_id,),
            )
            trades = cursor.fetchall()
            evaluated = [_evaluate_trade(cursor, t) for t in trades]
        return jsonify([_serialize_trade(t) for t in evaluated]), 200
    finally:
        conn.close()


@trades_bp.route("/api/trades/history", methods=["GET"])
def list_trade_history():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM active_trades WHERE user_id=%s AND status != 'ACTIVE' ORDER BY closed_at DESC",
                (user_id,),
            )
            trades = cursor.fetchall()
        return jsonify([_serialize_trade(t) for t in trades]), 200
    finally:
        conn.close()


@trades_bp.route("/api/trades/<int:trade_id>", methods=["GET"])
def get_trade_detail(trade_id):
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM active_trades WHERE id=%s AND user_id=%s", (trade_id, user_id))
            trade = cursor.fetchone()
            if not trade:
                return jsonify({"error": "Trade not found"}), 404

            if trade["status"] == "ACTIVE":
                trade = _evaluate_trade(cursor, trade)

            cursor.execute(
                "SELECT event_type, message, price, pnl, timestamp FROM trade_events WHERE trade_id=%s ORDER BY timestamp ASC",
                (trade_id,),
            )
            events = cursor.fetchall()
            for e in events:
                e["timestamp"] = e["timestamp"].isoformat()

        result = _serialize_trade(trade)
        result["timeline"] = events
        try:
            result["original_signal"] = json.loads(trade.get("signal_snapshot") or "{}")
        except Exception:
            result["original_signal"] = {}
        return jsonify(result), 200
    finally:
        conn.close()


@trades_bp.route("/api/trades/<int:trade_id>/close", methods=["POST"])
def close_trade(trade_id):
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    exit_price = data.get("exit_price")

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM active_trades WHERE id=%s AND user_id=%s", (trade_id, user_id))
            trade = cursor.fetchone()
            if not trade:
                return jsonify({"error": "Trade not found"}), 404
            if trade["status"] != "ACTIVE":
                return jsonify({"error": "Trade is already closed"}), 400

            if exit_price is None:
                exit_price = _get_live_price(trade["asset"]) or trade["entry_price"]
            exit_price = float(exit_price)
            pnl, pnl_pct = _calc_pnl(trade["direction"], trade["entry_price"], exit_price, trade["position_size"])

            cursor.execute(
                """UPDATE active_trades SET status='MANUALLY_CLOSED', exit_price=%s, exit_reason='MANUALLY_CLOSED',
                   estimated_pnl=%s, estimated_pnl_percent=%s, closed_at=%s, last_updated=%s WHERE id=%s""",
                (exit_price, pnl, pnl_pct, datetime.utcnow(), datetime.utcnow(), trade_id),
            )
            _add_event(cursor, trade_id, "MANUALLY_CLOSED", "Trade was manually closed by the user.", exit_price, pnl)
        return jsonify({"message": "Trade closed"}), 200
    finally:
        conn.close()
