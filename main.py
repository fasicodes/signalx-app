"""
Trading Signal Backend - v2 (5 Advanced Concepts)
---------------------------------------------------
Ye Flask app 5 concepts real formulas se calculate karta hai:
  1. Hawkes Process        -> Buying/Selling Pressure (0-10)
  2. Bayesian Classifier   -> Bullish% / Bearish%
  3. Quantile Volatility   -> Expected move, SL/TP
  4. Conformal Prediction  -> Confidence%, Trade/Skip (WAIT)
  5. Fractional Kelly      -> Suggested Risk%

Chalane ka tareeqa:
    pip install flask flask-cors pandas pandas-ta ccxt numpy --break-system-packages
    python app.py

URL: http://localhost:5000/signal?coin=BTC/USDT
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas as pd
import numpy as np
import pandas_ta as ta
import ccxt

app = Flask(__name__)
CORS(app)

exchange = ccxt.binance()


def get_candles(symbol="BTC/USDT", timeframe="1h", limit=200):
    """Exchange se OHLCV candles fetch karta hai."""
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


# ============================================================
# 1. HAWKES PROCESS APPROXIMATION
# Formula: lambda(t) = mu + sum( alpha * exp(-beta * (t - ti)) )
# mu = baseline rate, alpha = impact of past event, beta = decay,
# ti = times of past "events" (yahan: bade price moves)
# ============================================================
def hawkes_pressure(df, alpha=0.6, beta=0.4, lookback=40):
    """
    Buying aur Selling "intensity" nikalta hai based on recent big moves
    (jaise real Hawkes process mein "events" ka clustering measure karte hain).
    Return: buying_pressure (0-10), selling_pressure (0-10)
    """
    returns = df["close"].pct_change().dropna().tail(lookback).reset_index(drop=True)

    # "Event" = ek candle jiska move top 30% (bade moves) mein aata hai
    move_threshold = returns.abs().quantile(0.70)

    buy_event_times = []   # jab bhi bada UP move hua, uska time index
    sell_event_times = []  # jab bhi bada DOWN move hua

    for t, r in enumerate(returns):
        if r > move_threshold:
            buy_event_times.append(t)
        elif r < -move_threshold:
            sell_event_times.append(t)

    now = len(returns) - 1  # "current time" = sabse aakhri candle
    mu = 0.1  # baseline intensity (chhota constant)

    def intensity(event_times):
        # lambda(t) = mu + sum(alpha * exp(-beta*(t - ti)))
        total = mu
        for ti in event_times:
            total += alpha * np.exp(-beta * (now - ti))
        return total

    buy_intensity = intensity(buy_event_times)
    sell_intensity = intensity(sell_event_times)

    # 0-10 scale par normalize karna (simple cap-based scaling)
    max_possible = mu + alpha * len(returns)  # rough upper bound
    buying_pressure = min(10, round((buy_intensity / max_possible) * 10, 1))
    selling_pressure = min(10, round((sell_intensity / max_possible) * 10, 1))

    return buying_pressure, selling_pressure


# ============================================================
# 2. BAYESIAN CLASSIFIER (Naive Bayes)
# Formula: P(C|X) = P(X|C) * P(C) / P(X)
# Hum history dekh kar empirically P(up|current RSI+MACD state) nikalte hain
# ============================================================
def bayesian_bullish_bearish(df):
    """
    Pichle candles ka history dekh kar: jab bhi RSI+MACD ki jaisi state
    bani thi jaisi abhi hai, tab kitni baar agli candle upar gayi?
    Isi frequency ko posterior probability (P(up|X)) ki tarah use karte hain.
    """
    data = df.copy()
    data["rsi_bucket"] = pd.cut(data["rsi"], bins=[0, 35, 65, 100], labels=["low", "mid", "high"])
    data["macd_state"] = np.where(data["macd"] > data["macd_signal"], "bullish", "bearish")
    data["next_up"] = data["close"].shift(-1) > data["close"]

    current = data.iloc[-1]
    current_bucket = current["rsi_bucket"]
    current_macd_state = current["macd_state"]

    # Sirf wahi historical rows lo jinki state abhi jaisi hai
    matching_rows = data[
        (data["rsi_bucket"] == current_bucket) &
        (data["macd_state"] == current_macd_state)
    ].dropna(subset=["next_up"])

    if len(matching_rows) >= 5:  # kaafi data hai to empirical probability use karo
        bullish_prob = matching_rows["next_up"].mean()
    else:  # kaafi data nahi -> overall market ka average prior use karo (fallback)
        bullish_prob = data["next_up"].dropna().mean()

    bullish_pct = round(float(bullish_prob) * 100, 1)
    bearish_pct = round(100 - bullish_pct, 1)
    return bullish_pct, bearish_pct


# ============================================================
# 3. QUANTILE VOLATILITY
# Formula: Q(q) = inf{x : F(x) >= q}  -> pandas .quantile() isi ko implement karta hai
# ============================================================
def quantile_volatility(df, current_price, direction):
    """95th percentile move se SL/TP aur expected volatility nikalta hai."""
    returns = df["close"].pct_change().dropna()
    extreme_move = returns.abs().quantile(0.95)   # 95% quantile
    typical_move = returns.abs().quantile(0.50)   # median quantile

    if direction == "LONG":
        stop_loss = current_price * (1 - extreme_move)
        take_profit = current_price * (1 + extreme_move * 1.5)
    elif direction == "SHORT":
        stop_loss = current_price * (1 + extreme_move)
        take_profit = current_price * (1 - extreme_move * 1.5)
    else:
        stop_loss, take_profit = None, None

    return {
        "expected_volatility_pct": round(float(typical_move) * 100, 2),
        "extreme_volatility_95_pct": round(float(extreme_move) * 100, 2),
        "stop_loss": round(float(stop_loss), 2) if stop_loss else None,
        "take_profit": round(float(take_profit), 2) if take_profit else None,
    }


# ============================================================
# 4. CONFORMAL PREDICTION
# Confidence = 1 - epsilon. Agar confidence kam ho -> trade SKIP (WAIT)
# Hum "epsilon" ko models ke aapas mein disagreement se nikalte hain
# ============================================================
def conformal_confidence(bullish_pct, buying_pressure, selling_pressure):
    """
    3 independent signals (Bayesian, Hawkes-buy, Hawkes-sell) ka agreement
    dekh kar confidence banata hai. Jitna zyada agreement, utna kam "epsilon" (error).
    """
    bayesian_says_long = bullish_pct > 50
    hawkes_says_long = buying_pressure > selling_pressure

    votes_long = sum([bayesian_says_long, hawkes_says_long])
    votes_short = 2 - votes_long

    agreement = max(votes_long, votes_short) / 2  # 0.5 to 1.0

    # Bayesian probability ki "strength" (50% se kitna door hai) bhi confidence mein shamil
    bayesian_strength = abs(bullish_pct - 50) / 50  # 0 (unsure) to 1 (very sure)

    confidence = (agreement * 0.6) + (bayesian_strength * 0.4)
    confidence = min(max(confidence, 0), 1)

    epsilon = 1 - confidence
    decision = "SKIP" if confidence < 0.55 else "TRADE"  # threshold

    return round(confidence * 100, 1), decision


# ============================================================
# 5. FRACTIONAL KELLY
# Full Kelly: f* = (b*p - q) / b   where q = 1-p
# Fractional: f = f* * k  (k = 0.5 => Half Kelly)
# ============================================================
def fractional_kelly(win_prob, reward_risk_ratio=1.5, k=0.5):
    """
    b = reward:risk ratio, p = win probability, q = 1-p
    f* = (b*p - q) / b
    """
    b = reward_risk_ratio
    p = win_prob
    q = 1 - p

    f_star = (b * p - q) / b
    f_star = max(f_star, 0)  # negative ho to 0 (trade mat karo)

    fractional = f_star * k
    fractional = min(fractional, 0.05)  # safety cap: max 5% account risk

    return round(fractional * 100, 2)


# ============================================================
# MASTER FUNCTION - sab kuch combine karta hai
# ============================================================
def generate_signal(df):
    # Base indicators (in par baaki concepts depend karte hain)
    df["rsi"] = ta.rsi(df["close"], length=14)
    macd = ta.macd(df["close"])
    df["macd"] = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    df = df.dropna(subset=["rsi", "macd", "macd_signal"]).reset_index(drop=True)

    latest = df.iloc[-1]
    current_price = float(latest["close"])

    # ---- 1. Hawkes Process ----
    buying_pressure, selling_pressure = hawkes_pressure(df)

    # ---- 2. Bayesian Classifier ----
    bullish_pct, bearish_pct = bayesian_bullish_bearish(df)

    # ---- 4. Conformal Prediction (confidence + trade/skip) ----
    confidence_pct, trade_decision = conformal_confidence(bullish_pct, buying_pressure, selling_pressure)

    # ---- Final Verdict ----
    if trade_decision == "SKIP":
        final_verdict = "WAIT"
    elif bullish_pct > bearish_pct:
        final_verdict = "LONG"
    else:
        final_verdict = "SHORT"

    # ---- 3. Quantile Volatility (SL/TP) ----
    volatility_data = quantile_volatility(df, current_price, final_verdict)

    # ---- 5. Fractional Kelly (risk %) ----
    win_prob = max(bullish_pct, bearish_pct) / 100
    suggested_risk_pct = fractional_kelly(win_prob)

    trend = "Bullish" if bullish_pct > bearish_pct else "Bearish"

    result = {
        "trend": trend,
        "buying_pressure": buying_pressure,
        "selling_pressure": selling_pressure,
        "bullish_pct": bullish_pct,
        "bearish_pct": bearish_pct,
        "confidence_pct": confidence_pct,
        "suggested_risk_pct": suggested_risk_pct,
        "final_verdict": final_verdict,
        "last_price": round(current_price, 2),
        "rsi": round(float(latest["rsi"]), 2),
        "macd": round(float(latest["macd"]), 4),
        "disclaimer": "Probability estimates only - not financial advice.",
    }
    result.update(volatility_data)
    return result


@app.route("/signal", methods=["GET"])
def signal_endpoint():
    coin = request.args.get("coin", "BTC/USDT")
    timeframe = request.args.get("timeframe", "1h")

    try:
        df = get_candles(symbol=coin, timeframe=timeframe)
        result = generate_signal(df)
        result["coin"] = coin
        result["timeframe"] = timeframe
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/coins", methods=["GET"])
def available_coins():
    return jsonify(["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"])


if __name__ == "__main__":
    app.run(debug=True, port=5000)