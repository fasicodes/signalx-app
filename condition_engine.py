"""
Signal FM — Unified Trade Condition Detection & Stable Guidance Engine
=======================================================================

Core rule: "One Trade -> One Current Condition -> One Primary Guidance
Message". The guidance message shown to the user must stay STABLE while
the trade remains in the same underlying condition, and must change
ONLY when the condition has materially changed (not on every small
price tick).

How it works
------------
1. classify_condition() looks at the trade's current state (P/L,
   distance to SL/TP, momentum) and returns a single CANDIDATE
   condition key, chosen by priority (see CONDITION_PRIORITY).
2. apply_condition() is the state machine gate: it does NOT switch the
   trade's LOCKED condition just because the candidate changed once.
   A candidate must be observed for `confirmations_required`
   consecutive evaluations in a row before it is allowed to replace the
   locked condition (hysteresis / confirmation, see spec item 19).
   High-severity conditions (SL reached, setup invalidated, high risk)
   require fewer confirmations than routine ones, since those matter
   more and flip-flop less.
3. Once locked, the condition + message are persisted on the trade row
   (trade_condition, condition_message, condition_started_at,
   condition_version) so a page refresh / new session sees the exact
   same stable message (spec item 22).
4. A trade_events row is written ONLY when the locked condition
   actually changes -- not on every poll (spec item 23).

Nothing here talks to the exchange directly except compute_momentum(),
which reuses the existing candle-fetching + RSI/MACD helpers in
main.py so we are not building a second market-data pipeline.
"""

import json
from datetime import datetime

# ------------------------------------------------------------------
# 1. CONFIGURABLE THRESHOLDS
#    (spec item 19: "exact thresholds should be configurable rather
#    than hard-coded" -- tune these here, nowhere else)
# ------------------------------------------------------------------
THRESHOLDS = {
    # % move against/for the position, measured as pnl_percent
    "small_drawdown_pct": 0.5,       # Condition 2: temporary small drawdown
    "extended_drawdown_pct": 2.0,    # Condition 4: extended drawdown
    "high_risk_drawdown_pct": 3.5,   # Condition 10: strong opposite movement
    "recovery_improvement_pct": 0.6, # Condition 5: must recover this much off the worst point
    "recovery_ceiling_pct": 1.5,     # above this pnl%, it's solid profit again, not "recovering"
    "healthy_profit_pct": 0.3,       # Condition 1: minimum profit to call it "healthy"
    "reversal_risk_drop_pct": 0.8,   # Condition 7: profit given back from the peak

    # Distance-to-level thresholds (as a fraction of the level's price)
    "tp_approach_pct": 0.01,         # Condition 8: within 1% of TP
    "sl_approach_pct": 0.01,         # Condition 9: within 1% of SL

    # Momentum (RSI / MACD histogram) thresholds
    "rsi_neutral_band": (45, 55),    # inside this band == "flat" momentum
    "macd_hist_flat": 0.0,           # |macd_hist| below this == flat
    "momentum_weak_rsi_drift": 5,    # RSI must move this many points against
                                       # the trade direction to call momentum "weakening"

    # Confirmations required before a candidate condition can replace the
    # locked one (hysteresis). Lower = flips faster, higher = more stable.
    "confirmations": {
        "STOP_LOSS_APPROACHING": 1,
        "SETUP_INVALIDATED": 1,
        "HIGH_RISK_OPPOSITE_MOVE": 2,
        "TAKE_PROFIT_APPROACHING": 1,
        "PROFIT_AT_RISK": 2,
        "EXTENDED_DRAWDOWN": 2,
        "RECOVERY": 2,
        "TEMPORARY_DRAWDOWN_PROLONGED": 2,
        "HEALTHY_PROFIT": 2,
        "PROFIT_MOMENTUM_WEAKENING": 2,
        "TEMPORARY_SMALL_DRAWDOWN": 2,
        "CONSOLIDATION": 3,
        "WAIT_EVALUATING": 3,
    },
    "default_confirmations": 2,

    # How often (minutes) we're allowed to re-fetch candles for a
    # momentum snapshot. Keeps this cheap and avoids hammering the
    # exchange on every single poll.
    "momentum_refresh_minutes": 3,
}

# ------------------------------------------------------------------
# 2. CONDITION CATALOG (message text = the "one primary guidance
#    message" per condition, per the spec)
# ------------------------------------------------------------------
CONDITION_MESSAGES = {
    "HEALTHY_PROFIT": "Your trade is moving in the expected direction. Stay focused and continue following your original trade plan.",
    "TEMPORARY_SMALL_DRAWDOWN": "The market is temporarily moving against your position, but the original setup remains valid. Stay patient and continue following your trade plan.",
    "TEMPORARY_DRAWDOWN_PROLONGED": "The trade is currently under temporary pressure. The system is monitoring the market for the expected recovery while the original setup remains valid.",
    "EXTENDED_DRAWDOWN": "The trade remains in a temporary drawdown, but the original setup is still being monitored. Avoid reacting to short-term market noise and continue following your defined risk plan.",
    "RECOVERY": "The market is recovering from the previous drawdown. Price movement is improving relative to your entry.",
    "PROFIT_MOMENTUM_WEAKENING": "Your trade is currently in profit, but momentum is weakening. Stay focused and monitor the current market conditions.",
    "PROFIT_AT_RISK": "Your trade is currently in profit, but market conditions are becoming less favorable. Consider protecting your current profit according to your risk plan.",
    "TAKE_PROFIT_APPROACHING": "Price is approaching your Take Profit level. Stay focused and follow your trade plan.",
    "STOP_LOSS_APPROACHING": "Price is approaching your Stop Loss level. Risk is increasing, so monitor the trade carefully.",
    "HIGH_RISK_OPPOSITE_MOVE": "Market risk has increased. The current movement is going against the original setup, and the Stop Loss may be reached. Monitor your risk carefully.",
    "SETUP_INVALIDATED": "The original trade setup is no longer valid according to the current market conditions. Review your risk plan before taking further action.",
    "WAIT_EVALUATING": "Please wait. The system is currently evaluating market conditions before providing further guidance.",
    "CONSOLIDATION": "The market is currently consolidating. The system is monitoring the movement for clearer confirmation.",
}

# Priority order, highest first. Terminal states (TP/SL reached, manual
# close, market data unavailable, setup invalidated-hard-override) are
# handled by the caller (trades.py) BEFORE this module is even called,
# so they aren't repeated here.
CONDITION_PRIORITY = [
    "HIGH_RISK_OPPOSITE_MOVE",       # 10
    "STOP_LOSS_APPROACHING",         # 9
    "TAKE_PROFIT_APPROACHING",       # 8
    "PROFIT_AT_RISK",                # 7
    "EXTENDED_DRAWDOWN",             # 4
    "RECOVERY",                      # 5
    "TEMPORARY_DRAWDOWN_PROLONGED",  # 3
    "HEALTHY_PROFIT",                # 1
    "PROFIT_MOMENTUM_WEAKENING",     # 6
    "TEMPORARY_SMALL_DRAWDOWN",      # 2
    "CONSOLIDATION",                 # 13
    "WAIT_EVALUATING",               # 12 (fallback, always matches)
]


# ------------------------------------------------------------------
# 3. LIGHTWEIGHT MOMENTUM SNAPSHOT
#    Reuses main.py's existing candle fetch + RSI/MACD helpers - no
#    new market-data pipeline. Order book / heavy v6 concepts are
#    intentionally skipped here to keep this cheap enough to run on
#    every evaluation cycle.
# ------------------------------------------------------------------
def compute_momentum(symbol):
    """Returns {rsi, macd_hist, bullish_pct, bearish_pct} or None if
    market data is temporarily unavailable. Never raises."""
    try:
        from main import get_candles, _ta_rsi, _ta_macd, bayesian_bullish_bearish
    except Exception:
        return None
    try:
        df = get_candles(symbol=symbol, timeframe="15m", limit=100)
        df["rsi"] = _ta_rsi(df["close"], 14)
        macd_line, macd_signal_line = _ta_macd(df["close"])
        df["macd"] = macd_line
        df["macd_signal"] = macd_signal_line
        df = df.dropna(subset=["rsi", "macd", "macd_signal"]).reset_index(drop=True)
        if df.empty:
            return None
        latest = df.iloc[-1]
        bullish_pct, bearish_pct = bayesian_bullish_bearish(df)
        return {
            "rsi": round(float(latest["rsi"]), 2),
            "macd_hist": round(float(latest["macd"] - latest["macd_signal"]), 4),
            "bullish_pct": bullish_pct,
            "bearish_pct": bearish_pct,
        }
    except Exception:
        return None


def _momentum_favors(direction, momentum):
    """True if current momentum still favors the trade's direction."""
    if not momentum:
        return None  # unknown
    if direction == "LONG":
        return momentum["bullish_pct"] >= momentum["bearish_pct"]
    return momentum["bearish_pct"] >= momentum["bullish_pct"]


def _momentum_is_flat(momentum):
    if not momentum:
        return False
    lo, hi = THRESHOLDS["rsi_neutral_band"]
    rsi_flat = lo <= momentum["rsi"] <= hi
    macd_flat = abs(momentum["macd_hist"]) <= THRESHOLDS["macd_hist_flat"] + 0.0005
    return rsi_flat and macd_flat


# ------------------------------------------------------------------
# 4. CONDITION CLASSIFICATION (pure function - no DB writes here)
# ------------------------------------------------------------------
def classify_condition(trade, current_price, pnl_pct, momentum):
    """Looks at the trade's current numbers and returns the single best
    CANDIDATE condition key for right now, using the priority order.
    This does NOT lock/persist anything - see apply_condition()."""
    direction = trade["direction"]
    sl, tp = trade.get("stop_loss"), trade.get("take_profit")
    worst_pnl = trade.get("worst_pnl_percent")
    best_pnl = trade.get("best_pnl_percent")
    momentum_favors = _momentum_favors(direction, momentum)

    candidates = set()

    # --- Distance to SL / TP (evaluated on raw price, independent of P/L sign) ---
    if sl:
        sl_dist_pct = abs(current_price - sl) / sl
        if sl_dist_pct <= THRESHOLDS["sl_approach_pct"]:
            candidates.add("STOP_LOSS_APPROACHING")
    if tp:
        tp_dist_pct = abs(current_price - tp) / tp
        if tp_dist_pct <= THRESHOLDS["tp_approach_pct"]:
            candidates.add("TAKE_PROFIT_APPROACHING")

    # --- Strong opposite movement (Condition 10) ---
    if pnl_pct <= -THRESHOLDS["high_risk_drawdown_pct"]:
        candidates.add("HIGH_RISK_OPPOSITE_MOVE")
    elif pnl_pct <= -THRESHOLDS["high_risk_drawdown_pct"] * 0.7 and momentum_favors is False:
        # Deep-ish drawdown AND momentum still confirming against us =
        # meaningful deterioration, not just noise.
        candidates.add("HIGH_RISK_OPPOSITE_MOVE")

    # --- Recovery (Condition 5): was meaningfully underwater, has
    # improved off the worst point by a meaningful margin, but hasn't
    # yet become a solidly healthy profit in its own right (once it
    # does, HEALTHY_PROFIT / PROFIT_MOMENTUM_WEAKENING take over instead
    # of getting stuck showing "recovering" forever). ---
    if worst_pnl is not None and worst_pnl < -THRESHOLDS["small_drawdown_pct"]:
        improvement = pnl_pct - worst_pnl
        if improvement >= THRESHOLDS["recovery_improvement_pct"] and pnl_pct < THRESHOLDS["recovery_ceiling_pct"]:
            candidates.add("RECOVERY")

    # --- Drawdown tiers (Conditions 2, 3, 4) ---
    if pnl_pct < 0:
        depth = abs(pnl_pct)
        if depth >= THRESHOLDS["extended_drawdown_pct"]:
            candidates.add("EXTENDED_DRAWDOWN")
        elif depth >= THRESHOLDS["small_drawdown_pct"]:
            if momentum_favors is False:
                candidates.add("TEMPORARY_DRAWDOWN_PROLONGED")
            else:
                candidates.add("TEMPORARY_SMALL_DRAWDOWN")
        else:
            candidates.add("TEMPORARY_SMALL_DRAWDOWN")

    # --- Profit-side conditions (Conditions 1, 6, 7) ---
    if pnl_pct >= 0:
        gave_back = (best_pnl - pnl_pct) if best_pnl is not None else 0
        if best_pnl is not None and best_pnl > 0 and gave_back >= THRESHOLDS["reversal_risk_drop_pct"] and momentum_favors is False:
            candidates.add("PROFIT_AT_RISK")
        elif pnl_pct >= THRESHOLDS["healthy_profit_pct"]:
            if momentum_favors is False:
                candidates.add("PROFIT_MOMENTUM_WEAKENING")
            else:
                candidates.add("HEALTHY_PROFIT")

    # --- Consolidation / WAIT fallback ---
    if abs(pnl_pct) < THRESHOLDS["small_drawdown_pct"] and _momentum_is_flat(momentum):
        candidates.add("CONSOLIDATION")

    candidates.add("WAIT_EVALUATING")  # always-valid fallback, lowest priority

    for key in CONDITION_PRIORITY:
        if key in candidates:
            return key
    return "WAIT_EVALUATING"


# ------------------------------------------------------------------
# 5. STATE MACHINE GATE (hysteresis + persistence)
# ------------------------------------------------------------------
def apply_condition(cursor, trade, candidate_key, extra_updates):
    """Given the freshly-classified `candidate_key`, decides whether the
    trade's LOCKED condition should change, applies hysteresis, and
    writes a trade_events row + updates the trade record ONLY on an
    actual, confirmed change. Mutates + returns `extra_updates` (the
    dict trades.py will use to build its SQL UPDATE)."""
    locked_key = trade.get("trade_condition")
    pending_key = trade.get("pending_condition")
    pending_count = trade.get("pending_condition_count") or 0

    if candidate_key == locked_key:
        # Nothing changed - reset any half-formed pending switch and stop.
        if pending_key is not None:
            extra_updates["pending_condition"] = None
            extra_updates["pending_condition_count"] = 0
        return extra_updates

    # First-ever classification for this trade: lock immediately, there
    # is no prior message to flip-flop away from.
    required = 1 if locked_key is None else THRESHOLDS["confirmations"].get(
        candidate_key, THRESHOLDS["default_confirmations"]
    )

    if candidate_key == pending_key:
        pending_count += 1
    else:
        pending_key = candidate_key
        pending_count = 1

    if pending_count < required:
        # Not enough confirmations yet - keep showing the locked message,
        # just remember how far along the candidate is.
        extra_updates["pending_condition"] = pending_key
        extra_updates["pending_condition_count"] = pending_count
        return extra_updates

    # Confirmed - lock the new condition in.
    new_message = CONDITION_MESSAGES[candidate_key]
    extra_updates["trade_condition"] = candidate_key
    extra_updates["condition_message"] = new_message
    extra_updates["condition_started_at"] = datetime.utcnow()
    extra_updates["condition_version"] = (trade.get("condition_version") or 0) + 1
    extra_updates["pending_condition"] = None
    extra_updates["pending_condition_count"] = 0

    cursor.execute(
        "INSERT INTO trade_events (trade_id, event_type, message, price, pnl) VALUES (%s,%s,%s,%s,%s)",
        (trade["id"], f"CONDITION_{candidate_key}", new_message,
         extra_updates.get("last_price", trade.get("last_price")),
         extra_updates.get("estimated_pnl", trade.get("estimated_pnl"))),
    )
    return extra_updates


# ------------------------------------------------------------------
# 6. ENTRY POINT used by trades.py
# ------------------------------------------------------------------
def evaluate(cursor, trade, current_price, pnl, pnl_pct, updates):
    """Called from trades._evaluate_trade() once P/L has been
    recalculated and the trade is confirmed still ACTIVE (i.e. TP/SL
    not hit). Adds condition-engine fields into `updates` in place and
    returns it."""
    # Track best/worst P/L seen so RECOVERY / PROFIT_AT_RISK can detect
    # meaningful improvement/deterioration rather than reclassifying on
    # every tick.
    worst = trade.get("worst_pnl_percent")
    best = trade.get("best_pnl_percent")
    worst = pnl_pct if worst is None else min(worst, pnl_pct)
    best = pnl_pct if best is None else max(best, pnl_pct)
    if worst != trade.get("worst_pnl_percent"):
        updates["worst_pnl_percent"] = worst
    if best != trade.get("best_pnl_percent"):
        updates["best_pnl_percent"] = best
    trade_for_classification = {**trade, "worst_pnl_percent": worst, "best_pnl_percent": best}

    # Momentum snapshot: throttled so we don't re-fetch candles on every
    # single poll (spec: don't react to single-tick noise; also keeps
    # this cheap).
    momentum = None
    last_check = trade.get("last_momentum_check")
    refresh_due = (
        last_check is None
        or (datetime.utcnow() - last_check).total_seconds() >= THRESHOLDS["momentum_refresh_minutes"] * 60
    )
    if refresh_due:
        momentum = compute_momentum(trade["asset"])
        updates["last_momentum_check"] = datetime.utcnow()
        updates["momentum_snapshot"] = json.dumps(momentum) if momentum else None
    else:
        cached = trade.get("momentum_snapshot")
        if cached:
            try:
                momentum = json.loads(cached)
            except Exception:
                momentum = None

    candidate = classify_condition(trade_for_classification, current_price, pnl_pct, momentum)
    apply_condition(cursor, trade_for_classification, candidate, updates)
    return updates
