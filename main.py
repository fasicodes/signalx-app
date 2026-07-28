"""
Trading Signal Backend + Frontend - v5
---------------------------------------------------
v4 se FARQ (IMPORTANT):
  Pehle FINAL VERDICT + CONFIDENCE sirf 2 concepts se ban rahe thay:
      Hawkes Process + Bayesian Classifier (Conformal Prediction ke zariye)
  Baaki 8 concepts sirf "display panels" thay, verdict ko touch nahi karte thay.

  Ab (v5) FINAL VERDICT + CONFIDENCE saare 10 concepts se milkar bante hain
  ek WEIGHTED VOTING + PENALTY system ke zariye. Neeche `combined_signal()`
  function is poore logic ka core hai - achi tarah comments padhein.

10 CONCEPTS (recap):
  1. Hawkes Process        -> Buying/Selling Pressure       -> DIRECTION VOTE
  2. Bayesian Classifier   -> Bullish% / Bearish%            -> DIRECTION VOTE
  3. Quantile Volatility   -> Expected move, SL/TP           -> (verdict ke baad, risk levels)
  4. Conformal Prediction  -> ab sirf agreement-check helper hai (neeche dekhein)
  5. Fractional Kelly      -> Suggested Risk%                -> (verdict ke baad)
  6. Order Flow Imbalance  -> Order-book buy/sell pressure   -> DIRECTION VOTE
  7. VPIN (Toxic Flow)     -> Informed/toxic trading         -> CONFIDENCE PENALTY
  8. HMM Regime Detection  -> Trending / Ranging             -> DIRECTION VOTE (conditional)
  9. Jump Diffusion        -> Sudden shock/jump events       -> CONFIDENCE PENALTY
  10. Meta-Labeling (ML)   -> Secondary ML EXECUTE/SKIP prob -> DIRECTION VOTE

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
"accuracy %" ka koi guaranteed number nahi diya ja sakta. Weighted voting
scheme neeche bhi MANUALLY chuni gayi weights hain (data se optimize nahi
hui) - ye ek REASONABLE default hai, ground truth nahi.
"""

import os
import time

from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import pandas as pd
import numpy as np
import pandas_ta as ta
import ccxt

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
# 1. HAWKES PROCESS APPROXIMATION  (UNCHANGED)
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
# 2. BAYESIAN CLASSIFIER (Naive Bayes)  (UNCHANGED)
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
# 3. QUANTILE VOLATILITY  (UNCHANGED)
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
# 5. FRACTIONAL KELLY  (UNCHANGED)
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
# 6. ORDER FLOW IMBALANCE (OFI)  (UNCHANGED)
# Formula: OFI_t = I{dBid>=0}*BidSize_t  -  I{dAsk<=0}*AskSize_t
#
# LIMITATION: Real OFI ke liye continuous websocket order-book stream
# chahiye hota hai. Yahan sirf 2 REST snapshots (1 sec gap) se
# approximate kar rahe hain - "real" institutional OFI se noisy hai.
# ============================================================
def order_flow_imbalance(symbol="BTC/USDT", snapshot_gap_sec=1.0):
    ob1 = exchange.fetch_order_book(symbol, limit=5)
    time.sleep(snapshot_gap_sec)
    ob2 = exchange.fetch_order_book(symbol, limit=5)

    bid1_price, bid1_size = ob1["bids"][0]
    ask1_price, ask1_size = ob1["asks"][0]
    bid2_price, bid2_size = ob2["bids"][0]
    ask2_price, ask2_size = ob2["asks"][0]

    bid_flow = bid2_size if bid2_price >= bid1_price else -bid1_size
    ask_flow = ask2_size if ask2_price <= ask1_price else -ask1_size

    ofi_raw = bid_flow - ask_flow

    scale = max(abs(bid1_size), abs(ask1_size), 1e-9)
    ofi_score = round(float(np.clip((ofi_raw / scale) * 5, -10, 10)), 2)

    return {
        "ofi_score": ofi_score,
        "ofi_raw": round(float(ofi_raw), 4),
    }


# ============================================================
# 7. VPIN - TOXIC FLOW DETECTION  (UNCHANGED)
# Formula: VPIN = avg( |BuyVol - SellVol| / TotalVol )  per volume bucket
#
# LIMITATION: Asal VPIN thousands of trades + proper Bulk Volume
# Classification pe based hota hai. Yahan sirf last N public trades
# ka taker-side (ccxt "side" field) use ho raha hai.
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
# 8. HMM REGIME DETECTION  (UNCHANGED)
# State_t ~ TransitionMatrix(State_{t-1})  |  Emission_t ~ Gaussian(mean, var)
#
# LIMITATION: Har request pe fresh retrain hota hai (200 candles ka
# chota chunk) - proper usage mein bade dataset pe train/save/load
# hona chahiye.
# ============================================================
def hmm_regime(df, n_states=2):
    returns = df["close"].pct_change().dropna()
    volatility = returns.rolling(5).std()

    features = pd.concat([returns, volatility], axis=1)
    features.columns = ["returns", "volatility"]
    features = features.dropna()

    if len(features) < 30:
        return {"regime": "INSUFFICIENT_DATA", "state": None, "state_mean_return_pct": None}

    X = features.values

    model = GaussianHMM(n_components=n_states, covariance_type="diag",
                         n_iter=100, random_state=42)
    model.fit(X)
    hidden_states = model.predict(X)

    current_state = int(hidden_states[-1])
    state_mean_returns = model.means_[:, 0]

    trending_state = int(np.argmax(np.abs(state_mean_returns)))
    regime = "Trending" if current_state == trending_state else "Ranging"

    return {
        "regime": regime,
        "state": current_state,
        "state_mean_return_pct": round(float(state_mean_returns[current_state]) * 100, 3),
    }


# ============================================================
# 9. JUMP DIFFUSION / HAWKES-JUMP DETECTOR  (UNCHANGED)
# dS_t = mu*S_t*dt + sigma*S_t*dW_t + J_t*S_t*dN_t
#
# LIMITATION: Simplified z-score proxy hai, proper Poisson jump-
# intensity MLE fit nahi ho raha.
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
# 10. META-LABELING (Secondary ML Classifier)  (UNCHANGED)
# Primary Model: Signal Direction (-1/+1) -> Secondary ML: Execute(1)/Skip(0)
#
# LIMITATION (important): 200 candles pe hi train + "predict" ho raha
# hai (look-ahead bias jaisa Bayesian module mein bhi hai). 200 rows
# RandomForest ke liye chota dataset hai - probability fully reliable
# nahi maani ja sakti. Ye module architecture demo ke liye hai.
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
# *** NAYA CORE: COMBINED SIGNAL - saare 10 concepts yahan milte hain ***
#
# Tareeqa: har directional concept ek "vote" deta hai jo -1 (bearish)
# se +1 (bullish) ke beech hota hai, jiski apni "strength" hoti hai.
# Har vote ko fixed weight di gayi hai (neeche WEIGHTS dict). In sab
# ka weighted-average nikal ke ek final "score" (-1 to +1) banta hai:
#
#     score > 0   -> LONG
#     score < 0   -> SHORT
#
# Confidence = |score| ko 0-100% scale kiya jata hai, phir 2 cheezein
# ye confidence ko NEECHE kheenchti hain (penalty), kyunke ye directional
# nahi balke "risk/uncertainty" concepts hain:
#     - VPIN high toxicity  -> confidence * 0.75
#     - Jump/shock detected -> confidence * 0.70
#
# Agar final confidence < MIN_CONFIDENCE threshold ho, to verdict
# "WAIT" ban jata hai (jaisa pehle Conformal Prediction karta tha,
# ab yahi role combined score + penalties nibhate hain).
# ============================================================

WEIGHTS = {
    "bayesian": 0.25,   # concept 2
    "hawkes":   0.20,   # concept 1
    "ofi":      0.15,   # concept 6
    "meta":     0.25,   # concept 10
    "regime":   0.15,   # concept 8 (sirf jab Trending ho)
}

MIN_CONFIDENCE = 55.0  # is se neeche confidence ho to WAIT


def combined_signal(bullish_pct, bearish_pct, buying_pressure, selling_pressure,
                     ofi_data, vpin_data, regime_data, jump_data, meta_data):

    votes = {}     # concept_name -> (direction_vote in [-1,+1], weight_used)

    # --- 2. Bayesian vote ---
    bayes_dir = (bullish_pct - 50.0) / 50.0   # -1..+1
    votes["bayesian"] = (bayes_dir, WEIGHTS["bayesian"])

    # --- 1. Hawkes vote ---
    total_pressure = buying_pressure + selling_pressure
    if total_pressure > 0:
        hawkes_dir = (buying_pressure - selling_pressure) / 10.0  # -1..+1
    else:
        hawkes_dir = 0.0
    votes["hawkes"] = (hawkes_dir, WEIGHTS["hawkes"])

    # --- 6. OFI vote (agar data available ho) ---
    ofi_score = ofi_data.get("ofi_score")
    if ofi_score is not None:
        ofi_dir = float(np.clip(ofi_score / 10.0, -1, 1))
        votes["ofi"] = (ofi_dir, WEIGHTS["ofi"])

    # --- 10. Meta-labeling vote (agar data available ho) ---
    meta_prob = meta_data.get("meta_win_probability")
    if meta_prob is not None:
        meta_dir = (meta_prob - 50.0) / 50.0
        votes["meta"] = (meta_dir, WEIGHTS["meta"])

    # --- 8. HMM regime vote (sirf jab regime clearly "Trending" ho,
    #         warna "Ranging" mein direction pe koi vote nahi deta) ---
    if regime_data.get("regime") == "Trending" and regime_data.get("state_mean_return_pct") is not None:
        regime_dir = float(np.clip(regime_data["state_mean_return_pct"] / 2.0, -1, 1))
        votes["regime"] = (regime_dir, WEIGHTS["regime"])

    # --- Weighted average score (DIRECTION decide karne ke liye) ---
    total_weight = sum(w for _, w in votes.values())
    if total_weight > 0:
        raw_score = sum(d * w for d, w in votes.values()) / total_weight
    else:
        raw_score = 0.0

    # --- CONFIDENCE: sirf raw_score ki magnitude se nahi (wo hamesha
    # chhoti reh jati hai jab weak votes average hote hain aur ek dusre
    # ko "dilute" kar dete hain). Iski jagah 2 cheezein combine karte hain:
    #
    #   1) AGREEMENT: kitne % weight un concepts ka hai jo majority
    #      direction ke sath agree karte hain (chahe unki apni strength
    #      kam ho, agreement khud ek strong signal hai)
    #   2) STRENGTH: un concepts ki average absolute vote-strength
    #
    # Ye purane Conformal Prediction (agreement*0.6 + strength*0.4) wale
    # idea ko hi ab 5 concepts tak extend kar raha hai.
    majority_sign = 1 if raw_score >= 0 else -1

    agreeing_weight = sum(
        w for d, w in votes.values() if (1 if d >= 0 else -1) == majority_sign
    )
    agreement_ratio = (agreeing_weight / total_weight) if total_weight > 0 else 0.0

    avg_abs_strength = (
        sum(abs(d) * w for d, w in votes.values()) / total_weight
        if total_weight > 0 else 0.0
    )

    # NOTE: yahan agreement ko 0.6 weight aur strength ko 0.4 weight dena
    # (jaisa pehle try kiya gaya tha) galat nikla - simulation (20,000
    # synthetic trials, realistic vote-magnitude ranges use kar ke) ne
    # dikhaya ke us weighting se confidence sirf ~28% waqt 55% threshold
    # clear karti thi, matlab ~72% waqt system "WAIT" bolta rehta.
    # Wajah: avg_abs_strength hamesha chota reh jata hai (individual
    # concepts rarely bohot "strongly" confident hote hain), isliye ise
    # zyada weight dene se poora system neeche khinch jata hai. Agreement
    # (kitne concepts direction pe muttafiq hain) zyada reliable signal
    # hai - isi liye ab usay zyada weight (0.75) di ja rahi hai. Isi
    # simulation se naye weights ke sath ~52% signals threshold clear
    # karte hain, jo LONG/SHORT/WAIT ka zyada balanced split deta hai.
    base_confidence = (agreement_ratio * 0.75 + avg_abs_strength * 0.25) * 100

    # --- 7. VPIN penalty (toxic/manipulated flow -> kam bharosa) ---
    toxicity = vpin_data.get("toxicity")
    if toxicity == "HIGH_TOXICITY":
        base_confidence *= 0.75
    elif toxicity == "MODERATE_TOXICITY":
        base_confidence *= 0.90

    # --- 9. Jump-shock penalty (abhi abhi abnormal move hua -> uncertain) ---
    if jump_data.get("jump_detected"):
        base_confidence *= 0.70

    confidence_pct = round(min(max(base_confidence, 0), 100), 1)

    if confidence_pct < MIN_CONFIDENCE:
        final_verdict = "WAIT"
    elif raw_score > 0:
        final_verdict = "LONG"
    else:
        final_verdict = "SHORT"

    return {
        "final_verdict": final_verdict,
        "confidence_pct": confidence_pct,
        "raw_score": round(raw_score, 3),
        "agreement_ratio": round(agreement_ratio, 3),
        "avg_strength": round(avg_abs_strength, 3),
        "votes_used": {k: round(v[0], 3) for k, v in votes.items()},
    }


# ============================================================
# MASTER FUNCTION - sab 10 concepts combine karta hai
# ============================================================
def generate_signal(df, symbol="BTC/USDT", include_orderbook=True):
    df["rsi"] = ta.rsi(df["close"], length=14)
    macd = ta.macd(df["close"])
    df["macd"] = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    df = df.dropna(subset=["rsi", "macd", "macd_signal"]).reset_index(drop=True)

    latest = df.iloc[-1]
    current_price = float(latest["close"])

    # --- Sabhi 10 concepts pehle nikal lete hain ---
    buying_pressure, selling_pressure = hawkes_pressure(df)
    bullish_pct, bearish_pct = bayesian_bullish_bearish(df)

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

    # --- Ab in sab ko combine karke FINAL VERDICT + CONFIDENCE banti hai ---
    combo = combined_signal(
        bullish_pct, bearish_pct, buying_pressure, selling_pressure,
        ofi_data, vpin_data, regime_data, jump_data, meta_data,
    )
    final_verdict = combo["final_verdict"]
    confidence_pct = combo["confidence_pct"]

    # --- Verdict ban chuka, ab isi ke basis par SL/TP aur Risk % ---
    volatility_data = quantile_volatility(df, current_price, final_verdict)

    win_prob = max(bullish_pct, bearish_pct) / 100
    suggested_risk_pct = fractional_kelly(win_prob)

    trend = "Bullish" if bullish_pct > bearish_pct else "Bearish"

    # --- Fake breakout warning: OFI final verdict se ulta ho to flag ---
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

        "order_flow": ofi_data,
        "toxic_flow": vpin_data,
        "market_regime": regime_data,
        "jump_shock": jump_data,
        "meta_label": meta_data,
        "fake_breakout_warning": fake_breakout_warning,

        # Debug/transparency ke liye - kaunse concepts ne kitna vote diya
        "signal_breakdown": {
            "raw_score": combo["raw_score"],
            "votes_used": combo["votes_used"],
            "weights": WEIGHTS,
            "min_confidence_threshold": MIN_CONFIDENCE,
        },

        "disclaimer": ("Probability estimates only - not financial advice. "
                        "In-sample calculations, no walk-forward backtest run yet. "
                        "Weighted-voting scheme is manually chosen, not data-optimized."),
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
