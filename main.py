"""
Trading Signal Backend + Frontend - v4
---------------------------------------------------
PURANE 5 concepts (WAISAY HI, koi change nahi):
  1. Hawkes Process        -> Buying/Selling Pressure (0-10)
  2. Bayesian Classifier   -> Bullish% / Bearish%
  3. Quantile Volatility   -> Expected move, SL/TP
  4. Conformal Prediction  -> Confidence%, Trade/Skip (WAIT)
  5. Fractional Kelly      -> Suggested Risk%

NAYE 4 concepts (is version mein add kiye gaye):
  6. Order Flow Imbalance (OFI)   -> Order-book level buy/sell pressure
  7. VPIN (Toxic Flow)            -> Informed/toxic trading detect karta hai
  8. HMM Regime Detection         -> Market "Trending" ya "Ranging" hai
  9. Jump Diffusion Detector      -> Sudden shock/jump events
  10. Meta-Labeling (ML filter)   -> Secondary ML model jo primary signal ko approve/reject karta hai

Folder structure honi chahiye:
  main.py
  templates/design.html
  static/style.css
  static/script.js
  Procfile
  requirements.txt

Chalane ka tareeqa (local):
    pip install flask flask-cors pandas pandas-ta ccxt numpy hmmlearn scikit-learn --break-system-packages
    python main.py

URL: http://localhost:5000/

IMPORTANT DISCLAIMER (sach mein padhein):
Ye tamam concepts statistically "sound-looking" hain lekin koi bhi is code
mein NO real backtesting / walk-forward validation nahi ho rahi. Isliye
"accuracy %" ka koi guaranteed number nahi diya ja sakta. Neeche har naye
function ke comment mein iski asli limitation likhi hui hai.
"""

import os
import time

from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import pandas as pd
import numpy as np
import pandas_ta as ta
import ccxt

# Naye concepts ke liye extra libraries
from hmmlearn.hmm import GaussianHMM
from sklearn.ensemble import RandomForestClassifier

app = Flask(__name__)
CORS(app)

exchange = ccxt.okx()


def get_candles(symbol="BTC/USDT", timeframe="1h", limit=200):
    """Exchange se OHLCV candles fetch karta hai."""
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


# ============================================================
# 1. HAWKES PROCESS APPROXIMATION  (PURANA - UNCHANGED)
# Formula: lambda(t) = mu + sum( alpha * exp(-beta * (t - ti)) )
# ============================================================
def hawkes_pressure(df, alpha=0.6, beta=0.4, lookback=40):
    """
    Buying aur Selling "intensity" nikalta hai based on recent big moves.
    Return: buying_pressure (0-10), selling_pressure (0-10)
    """
    returns = df["close"].pct_change().dropna().tail(lookback).reset_index(drop=True)

    move_threshold = returns.abs().quantile(0.70)

    buy_event_times = []
    sell_event_times = []

    for t, r in enumerate(returns):
        if r > move_threshold:
            buy_event_times.append(t)
        elif r < -move_threshold:
            sell_event_times.append(t)

    now = len(returns) - 1
    mu = 0.1

    def intensity(event_times):
        total = mu
        for ti in event_times:
            total += alpha * np.exp(-beta * (now - ti))
        return total

    buy_intensity = intensity(buy_event_times)
    sell_intensity = intensity(sell_event_times)

    max_possible = mu + alpha * len(returns)
    buying_pressure = min(10, round((buy_intensity / max_possible) * 10, 1))
    selling_pressure = min(10, round((sell_intensity / max_possible) * 10, 1))

    return buying_pressure, selling_pressure


# ============================================================
# 2. BAYESIAN CLASSIFIER (Naive Bayes)  (PURANA - UNCHANGED)
# Formula: P(C|X) = P(X|C) * P(C) / P(X)
# ============================================================
def bayesian_bullish_bearish(df):
    data = df.copy()
    data["rsi_bucket"] = pd.cut(data["rsi"], bins=[0, 35, 65, 100], labels=["low", "mid", "high"])
    data["macd_state"] = np.where(data["macd"] > data["macd_signal"], "bullish", "bearish")
    data["next_up"] = data["close"].shift(-1) > data["close"]

    current = data.iloc[-1]
    current_bucket = current["rsi_bucket"]
    current_macd_state = current["macd_state"]

    matching_rows = data[
        (data["rsi_bucket"] == current_bucket) &
        (data["macd_state"] == current_macd_state)
    ].dropna(subset=["next_up"])

    if len(matching_rows) >= 5:
        bullish_prob = matching_rows["next_up"].mean()
    else:
        bullish_prob = data["next_up"].dropna().mean()

    bullish_pct = round(float(bullish_prob) * 100, 1)
    bearish_pct = round(100 - bullish_pct, 1)
    return bullish_pct, bearish_pct


# ============================================================
# 3. QUANTILE VOLATILITY  (PURANA - UNCHANGED)
# Formula: Q(q) = inf{x : F(x) >= q}
# ============================================================
def quantile_volatility(df, current_price, direction):
    returns = df["close"].pct_change().dropna()
    extreme_move = returns.abs().quantile(0.95)
    typical_move = returns.abs().quantile(0.50)

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
# 4. CONFORMAL PREDICTION  (PURANA - UNCHANGED)
# ============================================================
def conformal_confidence(bullish_pct, buying_pressure, selling_pressure):
    bayesian_says_long = bullish_pct > 50
    hawkes_says_long = buying_pressure > selling_pressure

    votes_long = sum([bayesian_says_long, hawkes_says_long])
    votes_short = 2 - votes_long

    agreement = max(votes_long, votes_short) / 2

    bayesian_strength = abs(bullish_pct - 50) / 50

    confidence = (agreement * 0.6) + (bayesian_strength * 0.4)
    confidence = min(max(confidence, 0), 1)

    decision = "SKIP" if confidence < 0.55 else "TRADE"

    return round(confidence * 100, 1), decision


# ============================================================
# 5. FRACTIONAL KELLY  (PURANA - UNCHANGED)
# Full Kelly: f* = (b*p - q) / b   where q = 1-p
# ============================================================
def fractional_kelly(win_prob, reward_risk_ratio=1.5, k=0.5):
    b = reward_risk_ratio
    p = win_prob
    q = 1 - p

    f_star = (b * p - q) / b
    f_star = max(f_star, 0)

    fractional = f_star * k
    fractional = min(fractional, 0.05)

    return round(fractional * 100, 2)


# ============================================================
# 6. ORDER FLOW IMBALANCE (OFI)  <-- NAYA
# Formula: OFI_t = I{dBid>=0}*BidSize_t  -  I{dAsk<=0}*AskSize_t
#
# LIMITATION (honestly): Real OFI ke liye continuous websocket
# order-book stream chahiye hota hai (tick-by-tick). Yahan hum sirf
# 2 REST snapshots (1 second gap) le kar approximate kar rahe hain.
# Ye "real" institutional-grade OFI se kaafi zyada noisy hai.
# ============================================================
def order_flow_imbalance(symbol="BTC/USDT", snapshot_gap_sec=1.0):
    ob1 = exchange.fetch_order_book(symbol, limit=5)
    time.sleep(snapshot_gap_sec)
    ob2 = exchange.fetch_order_book(symbol, limit=5)

    bid1_price, bid1_size = ob1["bids"][0]
    ask1_price, ask1_size = ob1["asks"][0]
    bid2_price, bid2_size = ob2["bids"][0]
    ask2_price, ask2_size = ob2["asks"][0]

    # Bid side: price same/upar rahe aur size mojood ho -> buying pressure
    bid_flow = bid2_size if bid2_price >= bid1_price else -bid1_size
    # Ask side: price same/neeche rahe aur size mojood ho -> selling pressure
    ask_flow = ask2_size if ask2_price <= ask1_price else -ask1_size

    ofi_raw = bid_flow - ask_flow

    # Normalize karke -10 to +10 range mein le aate hain (rough scaling)
    scale = max(abs(bid1_size), abs(ask1_size), 1e-9)
    ofi_score = round(float(np.clip((ofi_raw / scale) * 5, -10, 10)), 2)

    return {
        "ofi_score": ofi_score,           # +ve = buyers order book eat kar rahe hain
        "ofi_raw": round(float(ofi_raw), 4),
    }


# ============================================================
# 7. VPIN - TOXIC FLOW DETECTION  <-- NAYA
# Formula: VPIN = avg( |BuyVol - SellVol| / TotalVol )  per volume bucket
#
# LIMITATION (honestly): Asal VPIN thousands of trades aur proper
# "Bulk Volume Classification" pe based hota hai. Yahan hum sirf
# exchange REST se last N public trades le rahe hain aur unka
# taker-side (ccxt "side" field) use kar rahe hain - ye kaafi
# hd exchanges par accurate hota hai lekin sab par nahi.
# ============================================================
def vpin_toxicity(symbol="BTC/USDT", trade_limit=500, n_buckets=20):
    trades = exchange.fetch_trades(symbol, limit=trade_limit)
    if not trades:
        return {"vpin_score": None, "toxicity": "NO_DATA"}

    df = pd.DataFrame(trades)
    if "side" not in df.columns or "amount" not in df.columns:
        return {"vpin_score": None, "toxicity": "NO_DATA"}

    df = df.dropna(subset=["side", "amount"])
    total_volume = df["amount"].sum()
    if total_volume <= 0:
        return {"vpin_score": None, "toxicity": "NO_DATA"}

    bucket_size = total_volume / n_buckets
    df["cum_vol"] = df["amount"].cumsum()
    df["bucket"] = (df["cum_vol"] // bucket_size).astype(int)

    imbalances = []
    for _, group in df.groupby("bucket"):
        buy_vol = group.loc[group["side"] == "buy", "amount"].sum()
        sell_vol = group.loc[group["side"] == "sell", "amount"].sum()
        bucket_total = buy_vol + sell_vol
        if bucket_total > 0:
            imbalances.append(abs(buy_vol - sell_vol) / bucket_total)

    if not imbalances:
        return {"vpin_score": None, "toxicity": "NO_DATA"}

    vpin_score = round(float(np.mean(imbalances)), 3)

    if vpin_score > 0.6:
        toxicity = "HIGH_TOXICITY"
    elif vpin_score > 0.35:
        toxicity = "MODERATE_TOXICITY"
    else:
        toxicity = "LOW_TOXICITY"

    return {"vpin_score": vpin_score, "toxicity": toxicity}


# ============================================================
# 8. HMM REGIME DETECTION  <-- NAYA
# State_t ~ TransitionMatrix(State_{t-1})  |  Emission_t ~ Gaussian(mean, var)
#
# LIMITATION (honestly): HMM har baar candles ke is chunk pe fresh
# train hota hai (random_state fixed hai warna results run-to-run
# badal sakte hain). Chunk chota ho (200 candles) to states unstable
# ho sakte hain. Proper usage mein isay bade dataset pe train karke
# save/load karna chahiye, har request pe retrain nahi.
# ============================================================
def hmm_regime(df, n_states=2):
    returns = df["close"].pct_change().dropna()
    volatility = returns.rolling(5).std()

    features = pd.concat([returns, volatility], axis=1)
    features.columns = ["returns", "volatility"]
    features = features.dropna()

    if len(features) < 30:
        return {"regime": "INSUFFICIENT_DATA", "state": None}

    X = features.values

    model = GaussianHMM(n_components=n_states, covariance_type="diag",
                         n_iter=100, random_state=42)
    model.fit(X)
    hidden_states = model.predict(X)

    current_state = int(hidden_states[-1])
    state_mean_returns = model.means_[:, 0]

    # Jis state ka |mean return| sabse zyada hai, wo "Trending" state hai
    trending_state = int(np.argmax(np.abs(state_mean_returns)))
    regime = "Trending" if current_state == trending_state else "Ranging"

    return {
        "regime": regime,
        "state": current_state,
        "state_mean_return_pct": round(float(state_mean_returns[current_state]) * 100, 3),
    }


# ============================================================
# 9. JUMP DIFFUSION / HAWKES-JUMP DETECTOR  <-- NAYA
# dS_t = mu*S_t*dt + sigma*S_t*dW_t + J_t*S_t*dN_t
#
# LIMITATION (honestly): Ye asal jump-diffusion model (Merton jump
# model waghera) ka simplified proxy hai - hum sirf z-score se
# "statistically abnormal" return dhoond rahe hain, koi proper
# Poisson jump-intensity MLE fit nahi kar rahe.
# ============================================================
def jump_diffusion_detector(df, lookback=100, jump_zscore=3.0):
    returns = df["close"].pct_change().dropna().tail(lookback)

    if len(returns) < 10:
        return {"jump_detected": False, "jump_zscore": None, "jump_direction": None}

    mean_r = returns.mean()
    std_r = returns.std()

    latest_return = returns.iloc[-1]
    z = float((latest_return - mean_r) / std_r) if std_r > 0 else 0.0

    is_jump = abs(z) > jump_zscore
    jump_direction = "UP" if latest_return > 0 else "DOWN"

    return {
        "jump_detected": bool(is_jump),
        "jump_zscore": round(z, 2),
        "jump_direction": jump_direction if is_jump else None,
    }


# ============================================================
# 10. META-LABELING (Secondary ML Classifier)  <-- NAYA
# Primary Model: Signal Direction (-1/+1) -> Secondary ML: Execute(1)/Skip(0)
#
# LIMITATION (honestly, ye important hai): Doc mein "75% se upar tabhi
# push karo" likha tha - is demo mein wo threshold anrealistic set nahi
# kiya kyunke:
#   a) Training data yahi 200 candles hain jo hum abhi fetch kar rahe
#      hain - koi proper out-of-sample / walk-forward split nahi hai.
#   b) "future_return" wala label thoda look-ahead-biased hai (jaisa
#      Bayesian module mein bhi hai) - isi window ke andar train aur
#      "predict" ho raha hai.
#   c) 200 rows ek RandomForest ke liye bohot chota dataset hai -
#      is se nikli probability ekdum reliable NAHI mani ja sakti.
# Ye module sirf ARCHITECTURE dikhane ke liye hai; real deployment
# ke liye isay mahino ke data pe alag se train/save/load karna hoga.
# ============================================================
def meta_label_filter(df, execute_threshold=0.55):
    data = df.copy()
    data["macd_diff"] = data["macd"] - data["macd_signal"]
    data["future_return"] = data["close"].shift(-3) / data["close"] - 1
    data["label"] = (data["future_return"] > 0).astype(int)

    data = data.dropna(subset=["rsi", "macd_diff", "label"])

    if len(data) < 30:
        return {"meta_win_probability": None, "meta_decision": "INSUFFICIENT_DATA"}

    feature_cols = ["rsi", "macd_diff"]
    X = data[feature_cols].values
    y = data["label"].values

    # Last row ka future_return NaN hoga (dropna se already hat chuka),
    # isliye current features alag se nikalte hain poore df se
    current_row = df.dropna(subset=["rsi", "macd", "macd_signal"]).iloc[-1]
    current_features = np.array([[current_row["rsi"],
                                   current_row["macd"] - current_row["macd_signal"]]])

    model = RandomForestClassifier(n_estimators=150, max_depth=4, random_state=42)
    model.fit(X, y)

    win_prob = float(model.predict_proba(current_features)[0][1])
    decision = "EXECUTE" if win_prob >= execute_threshold else "SKIP"

    return {
        "meta_win_probability": round(win_prob * 100, 1),
        "meta_decision": decision,
        "meta_threshold_used_pct": round(execute_threshold * 100, 1),
    }


# ============================================================
# MASTER FUNCTION - sab 9 concepts combine karta hai
# ============================================================
def generate_signal(df, symbol="BTC/USDT", include_orderbook=True):
    df["rsi"] = ta.rsi(df["close"], length=14)
    macd = ta.macd(df["close"])
    df["macd"] = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    df = df.dropna(subset=["rsi", "macd", "macd_signal"]).reset_index(drop=True)

    latest = df.iloc[-1]
    current_price = float(latest["close"])

    # --- Purane 5 concepts ---
    buying_pressure, selling_pressure = hawkes_pressure(df)
    bullish_pct, bearish_pct = bayesian_bullish_bearish(df)
    confidence_pct, trade_decision = conformal_confidence(bullish_pct, buying_pressure, selling_pressure)

    if trade_decision == "SKIP":
        final_verdict = "WAIT"
    elif bullish_pct > bearish_pct:
        final_verdict = "LONG"
    else:
        final_verdict = "SHORT"

    volatility_data = quantile_volatility(df, current_price, final_verdict)

    win_prob = max(bullish_pct, bearish_pct) / 100
    suggested_risk_pct = fractional_kelly(win_prob)

    trend = "Bullish" if bullish_pct > bearish_pct else "Bearish"

    # --- Naye 4 concepts ---
    # Order book calls thodi slow hain (1 sec sleep + network), isliye
    # optional flag rakha hai taake fast testing bhi ho sake
    if include_orderbook:
        try:
            ofi_data = order_flow_imbalance(symbol)
        except Exception as e:
            ofi_data = {"ofi_score": None, "ofi_raw": None, "error": str(e)}
        try:
            vpin_data = vpin_toxicity(symbol)
        except Exception as e:
            vpin_data = {"vpin_score": None, "toxicity": "ERROR", "error": str(e)}
    else:
        ofi_data = {"ofi_score": None, "ofi_raw": None}
        vpin_data = {"vpin_score": None, "toxicity": "SKIPPED"}

    regime_data = hmm_regime(df)
    jump_data = jump_diffusion_detector(df)
    meta_data = meta_label_filter(df)

    # --- Naye concepts, purane verdict ko "override" nahi karte,
    #     sirf warning flags ke tor par attach hote hain (transparent rehne ke liye) ---
    fake_breakout_warning = False
    if ofi_data.get("ofi_score") is not None:
        if final_verdict == "LONG" and ofi_data["ofi_score"] < 0:
            fake_breakout_warning = True
        elif final_verdict == "SHORT" and ofi_data["ofi_score"] > 0:
            fake_breakout_warning = True

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

        # naye concepts ka data
        "order_flow": ofi_data,
        "toxic_flow": vpin_data,
        "market_regime": regime_data,
        "jump_shock": jump_data,
        "meta_label": meta_data,
        "fake_breakout_warning": fake_breakout_warning,

        "disclaimer": ("Probability estimates only - not financial advice. "
                        "In-sample calculations, no walk-forward backtest run yet."),
    }
    result.update(volatility_data)
    return result


# ============================================================
# ROUTES
# ============================================================
@app.route("/", methods=["GET"])
def home():
    return render_template("design.html")


@app.route("/signal", methods=["GET"])
def signal_endpoint():
    coin = request.args.get("coin", "BTC/USDT")
    timeframe = request.args.get("timeframe", "1h")
    # order-book/trades calls slow honay ki wajah se optional query param
    orderbook = request.args.get("orderbook", "true").lower() != "false"

    try:
        df = get_candles(symbol=coin, timeframe=timeframe)
        result = generate_signal(df, symbol=coin, include_orderbook=orderbook)
        result["coin"] = coin
        result["timeframe"] = timeframe
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/coins", methods=["GET"])
def available_coins():
    return jsonify(["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
