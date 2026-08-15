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

# Discipline tracker: agar last trade loss par band hua ho aur isi window
# ke andar naya trade track kiya jaye, to revenge-trading warning dikhti
# hai (non-blocking - final decision hamesha user ki hai).
REVENGE_WINDOW_MINUTES = 30


def _require_login():
    return session.get("user_id")


def _get_live_price(asset):
    """Asset ka current price nikalta hai - existing get_live_price()
    reuse karte hain (main.py), jo crypto (OKX) aur forex (Yahoo
    Finance) dono symbols handle karta hai - isse pehle ye seedha ccxt
    exchange.fetch_ticker() call karta tha jo sirf crypto ke liye kaam
    karta tha aur forex pairs par fail ho jata tha."""
    try:
        from main import get_live_price
        return get_live_price(asset)
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
        # Performance Summary qualifying-outcome classification (see
        # db.py migration + /api/trades/summary): TP is always a
        # qualifying profit, SL is always a qualifying loss.
        updates["outcome_class"] = "TAKE_PROFIT" if new_status == "TARGET_REACHED" else "STOP_LOSS"
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


def _journal_result(t):
    """Trade Journal 'Result' column: WIN / LOSS / BREAKEVEN / OPEN /
    INVALIDATED / EXPIRED. Reuses existing status/outcome_class fields -
    no new state is invented."""
    status = t.get("status")
    if status == "ACTIVE":
        return "OPEN"
    if status == "SETUP_INVALIDATED":
        return "INVALIDATED"
    if status == "HOLDING_PERIOD_EXPIRED":
        return "EXPIRED"
    oc = t.get("outcome_class")
    pnl = t.get("estimated_pnl")
    if oc in ("TAKE_PROFIT", "MANUAL_PROFIT"):
        return "WIN"
    if oc == "STOP_LOSS":
        return "LOSS"
    if oc == "MANUAL_LOSS":
        return "LOSS"
    if pnl is not None:
        if pnl > 0:
            return "WIN"
        if pnl < 0:
            return "LOSS"
        return "BREAKEVEN"
    return "OPEN"


def _risk_reward(t):
    """R:R = potential reward / potential risk, based on entry/SL/TP.
    Returns None if SL or TP missing (can't be computed - never fabricated)."""
    entry = t.get("entry_price")
    sl = t.get("stop_loss")
    tp = t.get("take_profit")
    if not entry or not sl or not tp:
        return None
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return None
    return round(reward / risk, 2)


def _duration_seconds(t):
    start = t.get("created_at")
    if not start:
        return None
    end = t.get("closed_at") or datetime.utcnow()
    try:
        return int((end - start).total_seconds())
    except Exception:
        return None


def _extract_confidence(t):
    try:
        snap = json.loads(t.get("signal_snapshot") or "{}")
        return snap.get("confidence_pct")
    except Exception:
        return None


def _parse_tags(raw):
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _serialize_trade(t, include_journal=False):
    result = {
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
        "outcome_class": t.get("outcome_class"),
        "is_user_mistake": bool(t.get("is_user_mistake")),
        # Signal FM - stable primary guidance (see condition_engine.py)
        "trade_condition": t.get("trade_condition"),
        "condition_message": t.get("condition_message"),
        "condition_started_at": t["condition_started_at"].isoformat() if t.get("condition_started_at") else None,
    }
    if include_journal:
        result.update({
            "notes": t.get("notes"),
            "tags": _parse_tags(t.get("tags")),
            "setup_type": t.get("setup_type"),
            "journal_updated_at": t["journal_updated_at"].isoformat() if t.get("journal_updated_at") else None,
            "result": _journal_result(t),
            "risk_reward": _risk_reward(t),
            "duration_seconds": _duration_seconds(t),
            "confidence": _extract_confidence(t),
        })
    return result


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
    # NOTE: pehle sirf AVAILABLE_COINS (crypto list) check hota tha, jis
    # wajah se koi bhi forex pair (EUR/USD, USD/JPY, etc.) "Unsupported
    # asset" error de kar track hi nahi ho pata tha - FOREX_PAIRS ko bhi
    # whitelist mein shamil kar diya taake forex trades bhi track ho sakein.
    from main import AVAILABLE_COINS, FOREX_PAIRS
    if asset not in AVAILABLE_COINS and asset not in FOREX_PAIRS:
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

            # --- Revenge-trading check (discipline tracker) ---
            # Agar user ka sabse recent CLOSED trade (koi bhi asset) ek loss
            # tha aur bohat kam waqt pehle (REVENGE_WINDOW_MINUTES) band hua
            # hai, to ye trade block nahi karte (final decision hamesha user
            # ki hai) - bas response mein ek non-blocking warning add karte
            # hain taake discipline ka reminder mil jaye.
            cursor.execute(
                "SELECT closed_at, outcome_class, estimated_pnl FROM active_trades "
                "WHERE user_id=%s AND status != 'ACTIVE' AND closed_at IS NOT NULL "
                "ORDER BY closed_at DESC LIMIT 1",
                (user_id,),
            )
            last_closed = cursor.fetchone()
            revenge_warning = None
            if last_closed and last_closed.get("closed_at"):
                was_loss = last_closed.get("outcome_class") in ("STOP_LOSS", "MANUAL_LOSS") or \
                    (last_closed.get("estimated_pnl") is not None and last_closed["estimated_pnl"] < 0)
                minutes_since = (datetime.utcnow() - last_closed["closed_at"]).total_seconds() / 60
                if was_loss and minutes_since <= REVENGE_WINDOW_MINUTES:
                    revenge_warning = (
                        f"Your last trade closed at a loss only {int(minutes_since)} min ago. "
                        "Taking a new trade this quickly after a loss is a common revenge-trading "
                        "pattern - consider reviewing your setup before proceeding."
                    )

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
        response = {"message": "Trade is now being tracked", "trade_id": trade_id}
        if revenge_warning:
            response["revenge_warning"] = revenge_warning
        return jsonify(response), 201
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


@trades_bp.route("/api/trades/summary", methods=["GET"])
def trade_performance_summary():
    """Performance Summary — qualifying-outcome statistics.

    Only trades classified via `outcome_class` (see db.py migration +
    _evaluate_trade / close_trade above) count toward Total Profit/Loss
    and Win Rate:
        TAKE_PROFIT, MANUAL_PROFIT  -> qualifying profit
        STOP_LOSS                   -> qualifying loss
        MANUAL_LOSS                 -> excluded from stats (still shown
                                        normally in /api/trades/history)
    Trades closed before this feature existed and never backfilled
    (outcome_class IS NULL) are excluded rather than guessed at here.

    "User Mistake" widget: separately counts MANUAL_LOSS trades where
    `is_user_mistake` was set (no risk-warning condition was active when
    the user closed it - see condition_engine.RISK_WARNING_CONDITIONS and
    close_trade() above). This is informational only and does NOT affect
    win_rate / total_qualifying_trades, exactly like MANUAL_LOSS already
    doesn't.
    """
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT estimated_pnl, outcome_class FROM active_trades "
                "WHERE user_id=%s AND status != 'ACTIVE' AND outcome_class IS NOT NULL",
                (user_id,),
            )
            rows = cursor.fetchall()

            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM active_trades "
                "WHERE user_id=%s AND status != 'ACTIVE' AND outcome_class='MANUAL_LOSS' AND is_user_mistake=1",
                (user_id,),
            )
            user_mistake_trades = cursor.fetchone()["cnt"]

        total_profit = 0.0
        total_loss = 0.0
        winning_trades = 0
        losing_trades = 0

        for r in rows:
            pnl = r.get("estimated_pnl") or 0.0
            oc = r.get("outcome_class")
            if oc in ("TAKE_PROFIT", "MANUAL_PROFIT"):
                total_profit += max(pnl, 0.0)
                winning_trades += 1
            elif oc == "STOP_LOSS":
                total_loss += abs(min(pnl, 0.0))
                losing_trades += 1
            # MANUAL_LOSS intentionally excluded from the Performance Summary

        total_qualifying = winning_trades + losing_trades
        net_pnl = total_profit - total_loss
        win_rate = (winning_trades / total_qualifying * 100) if total_qualifying else 0.0

        return jsonify({
            "total_profit": round(total_profit, 2),
            "total_loss": round(total_loss, 2),
            "net_pnl": round(net_pnl, 2),
            "win_rate": round(win_rate, 2),
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "total_qualifying_trades": total_qualifying,
            "user_mistake_trades": user_mistake_trades,
        }), 200
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

            # Performance Summary qualifying-outcome classification: a
            # manual close only "qualifies" toward Total Profit when it
            # was actually profitable. A manual close while losing stays
            # fully visible in Trade History but is excluded from the
            # website's Performance Summary (see /api/trades/summary).
            outcome_class = "MANUAL_PROFIT" if pnl >= 0 else "MANUAL_LOSS"

            # "User Mistake" widget: only relevant for a losing manual
            # close. It's a mistake ONLY when the system had NOT already
            # warned about elevated risk on this trade (no HIGH_RISK /
            # STOP_LOSS_APPROACHING / SETUP_INVALIDATED condition locked
            # at the moment of closing) - see condition_engine.RISK_WARNING_CONDITIONS.
            # Purely informational: never affects Win Rate / qualifying
            # trade counts, same as MANUAL_LOSS already is.
            is_user_mistake = (
                outcome_class == "MANUAL_LOSS"
                and trade.get("trade_condition") not in condition_engine.RISK_WARNING_CONDITIONS
            )

            cursor.execute(
                """UPDATE active_trades SET status='MANUALLY_CLOSED', exit_price=%s, exit_reason='MANUALLY_CLOSED',
                   estimated_pnl=%s, estimated_pnl_percent=%s, closed_at=%s, last_updated=%s, outcome_class=%s,
                   is_user_mistake=%s WHERE id=%s""",
                (exit_price, pnl, pnl_pct, datetime.utcnow(), datetime.utcnow(), outcome_class,
                 1 if is_user_mistake else 0, trade_id),
            )
            _add_event(cursor, trade_id, "MANUALLY_CLOSED", "Trade was manually closed by the user.", exit_price, pnl)
        return jsonify({"message": "Trade closed"}), 200
    finally:
        conn.close()


# ======================================================================
# PHASE 2 - Trade Journal
# Extends the existing active_trades table (notes/tags/setup_type
# columns, see db.py migration). No new trade table, no data duplication.
# ======================================================================

_ALLOWED_SORTS = {
    "newest": "created_at DESC",
    "oldest": "created_at ASC",
    "highest_pnl": "estimated_pnl DESC",
    "lowest_pnl": "estimated_pnl ASC",
    "highest_confidence": "created_at DESC",  # confidence lives in JSON, sorted in Python below
}


@trades_bp.route("/api/trades/<int:trade_id>/journal", methods=["PATCH"])
def update_trade_journal(trade_id):
    """Add/edit notes, tags, setup_type for a trade. Scoped to the
    authenticated user - a user can never edit another user's trade."""
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid request body"}), 400

    fields = []
    values = []

    if "notes" in data:
        notes = data.get("notes")
        if notes is not None and not isinstance(notes, str):
            return jsonify({"error": "notes must be a string"}), 400
        if notes is not None and len(notes) > 5000:
            return jsonify({"error": "notes must be under 5000 characters"}), 400
        fields.append("notes=%s")
        values.append(notes)

    if "tags" in data:
        tags = data.get("tags")
        if tags is None:
            tags = []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return jsonify({"error": "tags must be a list of strings"}), 400
        tags = [t.strip() for t in tags if t.strip()][:15]  # sane cap
        fields.append("tags=%s")
        values.append(json.dumps(tags))

    if "setup_type" in data:
        setup_type = data.get("setup_type")
        if setup_type is not None and (not isinstance(setup_type, str) or len(setup_type) > 50):
            return jsonify({"error": "setup_type must be a string under 50 characters"}), 400
        fields.append("setup_type=%s")
        values.append(setup_type)

    if not fields:
        return jsonify({"error": "Nothing to update - provide notes, tags, and/or setup_type"}), 400

    fields.append("journal_updated_at=%s")
    values.append(datetime.utcnow())
    values.extend([trade_id, user_id])

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Ownership check baked into the WHERE clause itself.
            cursor.execute(
                f"UPDATE active_trades SET {', '.join(fields)} WHERE id=%s AND user_id=%s",
                tuple(values),
            )
            if cursor.rowcount == 0:
                # Either trade doesn't exist, or doesn't belong to this user.
                cursor.execute("SELECT id FROM active_trades WHERE id=%s", (trade_id,))
                if cursor.fetchone() is None:
                    return jsonify({"error": "Trade not found"}), 404
                return jsonify({"error": "Not authorized to edit this trade"}), 403

            cursor.execute("SELECT * FROM active_trades WHERE id=%s AND user_id=%s", (trade_id, user_id))
            trade = cursor.fetchone()
        return jsonify(_serialize_trade(trade, include_journal=True)), 200
    finally:
        conn.close()


@trades_bp.route("/api/trades/journal", methods=["GET"])
def list_trade_journal():
    """Trade Journal table: every trade (active + closed) belonging to the
    user, with filtering/sorting/search - all scoped to user_id.

    Query params (all optional):
      symbol, direction (LONG/SHORT), result (WIN/LOSS/BREAKEVEN/OPEN/
      INVALIDATED/EXPIRED), setup_type, tag, date_from, date_to (ISO dates),
      pnl_min, pnl_max, confidence_min, search (matches asset/notes/tags/setup),
      sort (newest|oldest|highest_pnl|lowest_pnl|highest_confidence)
    """
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    args = request.args
    symbol = args.get("symbol")
    direction = args.get("direction")
    setup_type = args.get("setup_type")
    tag = args.get("tag")
    date_from = args.get("date_from")
    date_to = args.get("date_to")
    search = (args.get("search") or "").strip().lower()
    sort = args.get("sort", "newest")
    if sort not in _ALLOWED_SORTS:
        sort = "newest"

    where = ["user_id=%s"]
    params = [user_id]

    if symbol:
        where.append("asset=%s")
        params.append(symbol)
    if direction in ("LONG", "SHORT"):
        where.append("direction=%s")
        params.append(direction)
    if setup_type:
        where.append("setup_type=%s")
        params.append(setup_type)
    if tag:
        where.append("tags LIKE %s")
        params.append(f'%"{tag}"%')
    if date_from:
        where.append("created_at >= %s")
        params.append(date_from)
    if date_to:
        where.append("created_at <= %s")
        params.append(date_to)

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            query = f"SELECT * FROM active_trades WHERE {' AND '.join(where)} ORDER BY {_ALLOWED_SORTS[sort]}"
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()

            # Re-evaluate ACTIVE rows so P&L/status reflect live price,
            # same as /api/trades/active does.
            trades = [_evaluate_trade(cursor, t) if t["status"] == "ACTIVE" else t for t in rows]

        serialized = [_serialize_trade(t, include_journal=True) for t in trades]

        # Filters that need computed/derived fields are applied in Python
        # (result, pnl range, confidence, free-text search) since they
        # aren't plain columns.
        result_filter = args.get("result")
        if result_filter:
            serialized = [t for t in serialized if t["result"] == result_filter]

        pnl_min = args.get("pnl_min")
        pnl_max = args.get("pnl_max")
        if pnl_min is not None:
            try:
                pnl_min = float(pnl_min)
                serialized = [t for t in serialized if (t["estimated_pnl"] or 0) >= pnl_min]
            except ValueError:
                pass
        if pnl_max is not None:
            try:
                pnl_max = float(pnl_max)
                serialized = [t for t in serialized if (t["estimated_pnl"] or 0) <= pnl_max]
            except ValueError:
                pass

        confidence_min = args.get("confidence_min")
        if confidence_min is not None:
            try:
                confidence_min = float(confidence_min)
                serialized = [t for t in serialized if (t["confidence"] or 0) >= confidence_min]
            except ValueError:
                pass

        if search:
            def _matches(t):
                haystack = " ".join([
                    t["asset"] or "", t.get("notes") or "", t.get("setup_type") or "",
                    " ".join(t.get("tags") or []),
                ]).lower()
                return search in haystack
            serialized = [t for t in serialized if _matches(t)]

        if sort == "highest_confidence":
            serialized.sort(key=lambda t: (t["confidence"] or 0), reverse=True)

        return jsonify(serialized), 200
    finally:
        conn.close()


@trades_bp.route("/api/trades/journal/stats", methods=["GET"])
def trade_journal_stats():
    """Trade Journal Dashboard metrics - counts, win rate, P&L stats.
    Computed only from the user's own recorded trades; nothing fabricated."""
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM active_trades WHERE user_id=%s", (user_id,))
            rows = cursor.fetchall()
            trades = [_evaluate_trade(cursor, t) if t["status"] == "ACTIVE" else t for t in rows]

        total_trades = len(trades)
        open_trades = sum(1 for t in trades if t["status"] == "ACTIVE")
        closed = [t for t in trades if t["status"] != "ACTIVE"]
        closed_trades = len(closed)

        results = [_journal_result(t) for t in closed]
        winning_trades = results.count("WIN")
        losing_trades = results.count("LOSS")
        win_rate = round((winning_trades / closed_trades * 100), 2) if closed_trades else 0.0

        pnls = [t.get("estimated_pnl") for t in closed if t.get("estimated_pnl") is not None]
        net_pnl = round(sum(pnls), 2) if pnls else 0.0
        avg_pnl = round(sum(pnls) / len(pnls), 2) if pnls else 0.0

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
        avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
        best_trade = round(max(pnls), 2) if pnls else None
        worst_trade = round(min(pnls), 2) if pnls else None

        return jsonify({
            "total_trades": total_trades,
            "open_trades": open_trades,
            "closed_trades": closed_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": win_rate,
            "net_pnl": net_pnl,
            "avg_pnl": avg_pnl,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
        }), 200
    finally:
        conn.close()
