"""
Trading Signal Backend + Frontend - v7
---------------------------------------------------
IMPORTANT: FINAL VERDICT + CONFIDENCE ka logic v4 jaisa hi hai
(sirf Hawkes Process + Bayesian Classifier, Conformal Prediction ke
zariye combine hote hain). Neeche wale 9 NAYE concepts verdict ko
BILKUL touch nahi karte - ye sirf extra "display panels" hain, jaisa
v4 mein OFI/VPIN/HMM/Jump/Meta add hue thay.

v7 mein sirf ye add hua hai:
  - /candles endpoint -> naye "LIVE CHART" tab (frontend) ke liye
    OHLCV candle series return karta hai, taake ek live candlestick
    chart bana ja sake. Verdict/confidence logic ko bilkul touch
    nahi kiya gaya.

PURANE 5 concepts (WAISAY HI, koi change nahi):
  1. Hawkes Process        -> Buying/Selling Pressure (0-10)
  2. Bayesian Classifier   -> Bullish% / Bearish%
  3. Quantile Volatility   -> Expected move, SL/TP
  4. Conformal Prediction  -> Confidence%, Trade/Skip (WAIT)   <-- VERDICT YAHAN SE BANTA HAI
  5. Fractional Kelly      -> Suggested Risk%

v4 ke 4 concepts (display-only, verdict ko touch nahi karte):
  6. Order Flow Imbalance (OFI)
  7. VPIN (Toxic Flow)
  8. HMM Regime Detection
  9. Jump Diffusion Detector
  10. Meta-Labeling (ML filter)

v6 ke 9 concepts (display-only, verdict ko touch nahi karte):
  11. Cross-Asset Flow & Intermarket Divergence
  12. Multi-Timeframe Permutation Entropy
  13. Order Book Depth Profiling (L2 Slope)
  14. Volume-Synchronized VWAP Deviation & Toxicity
  15. RL-style Dynamic Risk Agent (simplified heuristic)
  16. Adaptive Hurst Exponent
  17. Wavelet Transform Noise Filtering
  18. Structural Break Detection (CUSUM)
  19. Liquidity Sweep / Stop-Cluster Detection

Folder structure honi chahiye:
  main.py
  templates/design.html
  static/style.css
  static/script.js
  Procfile
  requirements.txt

Chalane ka tareeqa (local):
    pip install flask flask-cors pandas pandas-ta ccxt numpy hmmlearn scikit-learn PyWavelets --break-system-packages
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
from collections import deque

from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import pandas as pd
import numpy as np
import pandas_ta as ta
import ccxt
import requests

# v4 ke naye concepts ke liye extra libraries
from hmmlearn.hmm import GaussianHMM
from sklearn.ensemble import RandomForestClassifier

app = Flask(__name__)
CORS(app)

exchange = ccxt.okx()

# NOTE: exchange OKX hai. Neeche wali AVAILABLE_COINS list frontend (design.html)
# ke coin-select dropdown se match karti hai. Agar koi coin OKX par USDT pair
# ke sath list nahi hai to /signal us coin ke liye error return karega
# (fetch_ohlcv exception -> already try/except mein handled hai neeche).
# LEO (UNUS SED LEO) is list mein NAHI hai kyunke wo Bitfinex ka apna token
# hai aur OKX par generally available nahi hota - iski jagah DOT (Polkadot)
# rakha gaya hai.
AVAILABLE_COINS = [
    # TOP 1-10
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
    "TRX/USDT", "HYPE/USDT", "DOGE/USDT", "ZEC/USDT", "DOT/USDT",
    # TOP 11-20
    "XLM/USDT", "XMR/USDT", "LINK/USDT", "ADA/USDT", "BCH/USDT",
    "GRAM/USDT", "LTC/USDT", "SUI/USDT", "HBAR/USDT", "FIL/USDT",
    # TOP 21-30
    "AVAX/USDT", "CRO/USDT", "NEAR/USDT", "SHIB/USDT", "UNI/USDT",
    "TAO/USDT", "ONDO/USDT", "OKB/USDT", "ASTER/USDT", "ATOM/USDT",
]


def get_candles(symbol="BTC/USDT", timeframe="1h", limit=200):
    """Exchange se OHLCV candles fetch karta hai."""
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


# ============================================================
# 1. HAWKES PROCESS APPROXIMATION  (PURANA - UNCHANGED)
# ============================================================
def hawkes_pressure(df, alpha=0.6, beta=0.4, lookback=40):
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
# *** FINAL VERDICT + CONFIDENCE ka asal source yahi hai ***
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
# 6. ORDER FLOW IMBALANCE (OFI)  (v4 - UNCHANGED)
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

    return {"ofi_score": ofi_score, "ofi_raw": round(float(ofi_raw), 4)}


# ============================================================
# 7. VPIN - TOXIC FLOW DETECTION  (v4 - UNCHANGED)
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
# 8. HMM REGIME DETECTION  (v4 - UNCHANGED)
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
    model = GaussianHMM(n_components=n_states, covariance_type="diag", n_iter=100, random_state=42)
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
# 9. JUMP DIFFUSION / HAWKES-JUMP DETECTOR  (v4 - UNCHANGED)
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
# 10. META-LABELING (Secondary ML Classifier)  (v4 - UNCHANGED)
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
    current_features = np.array([[current_row["rsi"], current_row["macd"] - current_row["macd_signal"]]])

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
# 11. CROSS-ASSET FLOW & INTERMARKET DIVERGENCE  <-- (v6)
# Formula: Divergence_t = Z-Score(P_asset,t) - Z-Score(P_benchmark,t)
#
# LIMITATION (honestly): Doc mein "Total Crypto Market Cap" ya "DXY"
# jaisa proper benchmark maanga gaya tha - ye ccxt/exchange REST se
# directly available nahi hota (ye ek index hai, tradable pair nahi).
# Simple proxy use kiya: agar primary asset BTC nahi to BTC benchmark,
# agar primary khud BTC hai to ETH benchmark. Asal market-wide index
# jitna comprehensive nahi hai.
# ============================================================
def cross_asset_divergence(df, symbol, lookback=50):
    benchmark_symbol = "ETH/USDT" if symbol.upper().startswith("BTC") else "BTC/USDT"
    try:
        bench_df = get_candles(symbol=benchmark_symbol, timeframe="1h", limit=max(lookback + 10, 60))
    except Exception as e:
        return {"divergence_score": None, "benchmark": benchmark_symbol, "error": str(e)}

    asset_returns = df["close"].pct_change().dropna().tail(lookback)
    bench_returns = bench_df["close"].pct_change().dropna().tail(lookback)

    n = min(len(asset_returns), len(bench_returns))
    if n < 10:
        return {"divergence_score": None, "benchmark": benchmark_symbol}

    asset_returns = asset_returns.tail(n).reset_index(drop=True)
    bench_returns = bench_returns.tail(n).reset_index(drop=True)

    asset_z = (asset_returns.iloc[-1] - asset_returns.mean()) / (asset_returns.std() + 1e-9)
    bench_z = (bench_returns.iloc[-1] - bench_returns.mean()) / (bench_returns.std() + 1e-9)
    divergence = round(float(asset_z - bench_z), 3)

    if divergence > 1.0:
        interpretation = "ASSET_OUTPERFORMING_BENCHMARK"
    elif divergence < -1.0:
        interpretation = "ASSET_UNDERPERFORMING_BENCHMARK"
    else:
        interpretation = "IN_SYNC_WITH_BENCHMARK"

    return {"divergence_score": divergence, "benchmark": benchmark_symbol, "interpretation": interpretation}


# ============================================================
# 12. MULTI-TIMEFRAME PERMUTATION ENTROPY  <-- (v6)
# Formula: H(d) = - sum( P(pi) * log2(P(pi)) )
#
# LIMITATION (honestly): "Multi-Timeframe" naam hai lekin proper version
# alag timeframes (1h+4h+1d) alag se fetch kar ke combine karta - extra
# API calls + latency add karta. Yahan sirf ek hi timeframe par mukhtalif
# "order" (pattern length 3,4,5) try kar ke average liya - single-
# timeframe multi-order proxy hai.
# ============================================================
def _permutation_entropy(series, order=3, delay=1):
    n = len(series)
    counts = {}
    for i in range(n - (order - 1) * delay):
        window = series[i:i + order * delay:delay]
        pattern = tuple(np.argsort(window))
        counts[pattern] = counts.get(pattern, 0) + 1
    total = sum(counts.values())
    if total == 0:
        return None
    probs = np.array([c / total for c in counts.values()])
    pe = -np.sum(probs * np.log2(probs))
    max_entropy = np.log2(np.math.factorial(order))
    return pe / max_entropy if max_entropy > 0 else None


def multi_timeframe_entropy(df, orders=(3, 4, 5), lookback=100):
    returns = df["close"].pct_change().dropna().tail(lookback).values
    if len(returns) < 20:
        return {"entropy_avg": None, "regime": "INSUFFICIENT_DATA"}

    entropies = []
    for order in orders:
        try:
            pe = _permutation_entropy(returns, order=order)
            if pe is not None:
                entropies.append(pe)
        except Exception:
            continue

    if not entropies:
        return {"entropy_avg": None, "regime": "INSUFFICIENT_DATA"}

    avg_entropy = round(float(np.mean(entropies)), 3)
    if avg_entropy < 0.60:
        regime = "LOW_ENTROPY_TRENDING"
    elif avg_entropy > 0.85:
        regime = "HIGH_ENTROPY_CHOPPY"
    else:
        regime = "MODERATE_ENTROPY"

    return {"entropy_avg": avg_entropy, "regime": regime}


# ============================================================
# 13. ORDER BOOK DEPTH PROFILING (L2 SLOPE)  <-- (v6)
# Formula: DepthSlope_t = sum( w_i * (BidVol_i - AskVol_i) / Distance_i )
#
# LIMITATION (honestly): Doc mein "L2/L3" likha tha - individual order-
# level (L3) data retail-facing REST APIs se generally milta hi nahi.
# Yahan sirf L2 (aggregated price-level) depth use ho raha hai, "L3"
# sirf naam mein hai.
# ============================================================
def order_book_depth_profile(symbol="BTC/USDT", depth=10):
    try:
        ob = exchange.fetch_order_book(symbol, limit=depth)
    except Exception as e:
        return {"depth_slope": None, "error": str(e)}

    bids = ob.get("bids", [])[:depth]
    asks = ob.get("asks", [])[:depth]
    if not bids or not asks:
        return {"depth_slope": None}

    mid_price = (bids[0][0] + asks[0][0]) / 2
    weighted_sum = 0.0

    for i, (price, vol) in enumerate(bids):
        distance = max(abs(mid_price - price), 1e-9)
        weighted_sum += (1.0 / (i + 1)) * (vol / distance)

    for i, (price, vol) in enumerate(asks):
        distance = max(abs(price - mid_price), 1e-9)
        weighted_sum -= (1.0 / (i + 1)) * (vol / distance)

    depth_slope = round(float(weighted_sum), 4)
    if depth_slope > 0:
        wall_bias = "BID_WALL_HEAVIER"
    elif depth_slope < 0:
        wall_bias = "ASK_WALL_HEAVIER"
    else:
        wall_bias = "BALANCED"

    return {"depth_slope": depth_slope, "wall_bias": wall_bias, "depth_levels_used": len(bids)}


# ============================================================
# 14. VOLUME-SYNCHRONIZED VWAP DEVIATION & TOXICITY  <-- (v6)
# Formula: VWAP_Dev_t = (P_t - VWAP_t) / (sigma_VWAP * sqrt(t))
#
# LIMITATION (honestly): "Volume-synchronized" ka matlab hota hai VWAP
# fixed VOLUME-bars par based ho, time-bars par nahi. Simplicity ke
# liye already-fetched time-based OHLCV candles hi use ho rahe hain -
# asal volume-bar resampling nahi ho rahi.
# ============================================================
def vwap_deviation(df, vpin_score=None):
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    cum_vol_price = (typical_price * df["volume"]).cumsum()

    if cum_vol.iloc[-1] == 0:
        return {"vwap_deviation_z": None, "signal": "NO_DATA"}

    vwap = cum_vol_price / cum_vol
    deviation_series = df["close"] - vwap
    std_dev = deviation_series.std()

    if std_dev == 0 or np.isnan(std_dev):
        return {"vwap_deviation_z": None, "signal": "NO_DATA"}

    z = float(deviation_series.iloc[-1] / std_dev)
    toxic_reversion_flag = bool(vpin_score is not None and vpin_score > 0.6 and abs(z) > 2)
    signal = "MEAN_REVERSION_LIKELY" if abs(z) > 2 else "NO_EXTREME_DEVIATION"

    return {"vwap_deviation_z": round(z, 2), "signal": signal, "toxic_reversion_flag": toxic_reversion_flag}


# ============================================================
# 15. RL-STYLE DYNAMIC RISK & ALLOCATION AGENT  <-- (v6)
# Formula (doc): Q(s,a) <- Q(s,a) + alpha[R + gamma*max_a' Q(s',a') - Q(s,a)]
#
# LIMITATION (honestly, IMPORTANT): Ye ASAL Q-Learning training loop
# NAHI hai. Real RL agent ke liye actual trade outcomes (reward) ka
# feedback chahiye hota hai over many episodes, jo is stateless
# request/response API mein maujood nahi. Yahan sirf ek SIMPLIFIED
# HEURISTIC hai jo current volatility "state" ke mutabiq risk scale
# karta hai - "Q-Learning" sirf naam ke tor par hai, koi training
# nahi ho rahi.
# ============================================================
def rl_risk_agent(volatility_pct, base_risk_pct):
    if volatility_pct < 1.0:
        state = "LOW_VOL"
        multiplier = 1.2
    elif volatility_pct < 3.0:
        state = "MED_VOL"
        multiplier = 1.0
    else:
        state = "HIGH_VOL"
        multiplier = 0.6

    adjusted_risk = round(min(base_risk_pct * multiplier, 5.0), 2)
    return {"rl_state": state, "rl_risk_multiplier": multiplier, "rl_adjusted_risk_pct": adjusted_risk}


# ============================================================
# 16. ADAPTIVE HURST EXPONENT  <-- (v6)
# Formula: E[|R(t+tau) - R(t)|] proportional to tau^H
#
# LIMITATION (honestly): Chote lags (2-19) aur ek hi estimator (simple
# variance-scaling method) use ho raha hai - proper Hurst estimation
# ke liye zyada data + multiple estimators (DFA, GHE) cross-check
# karna chahiye. Chota sample noisy H de sakta hai.
# ============================================================
def hurst_exponent(df, lookback=100):
    prices = df["close"].tail(lookback).values
    if len(prices) < 30:
        return {"hurst": None, "memory": "INSUFFICIENT_DATA"}

    log_returns = np.diff(np.log(prices))
    lags = list(range(2, 20))
    tau = []
    for lag in lags:
        diffs = log_returns[lag:] - log_returns[:-lag]
        tau.append(np.sqrt(np.std(diffs)))

    tau = np.array(tau)
    valid = tau > 0
    if valid.sum() < 5:
        return {"hurst": None, "memory": "INSUFFICIENT_DATA"}

    log_lags = np.log(np.array(lags)[valid])
    log_tau = np.log(tau[valid])
    poly = np.polyfit(log_lags, log_tau, 1)
    hurst = round(float(poly[0] * 2), 3)

    if hurst > 0.55:
        memory = "TRENDING_PERSISTENT"
    elif hurst < 0.45:
        memory = "MEAN_REVERTING"
    else:
        memory = "RANDOM_WALK"

    return {"hurst": hurst, "memory": memory}


# ============================================================
# 17. WAVELET TRANSFORM NOISE FILTERING  <-- (v6)
# Formula: W_f(a,b) = (1/sqrt(|a|)) * integral( f(t) * psi*((t-b)/a) dt )
#
# LIMITATION (honestly): Wavelet denoising boundary/edge-effects ka
# shikar hoti hai - series ka bilkul AAKHRI hissa (jahan hume trend
# chahiye) sabse zyada is distortion ka shikar hota hai. Requires
# PyWavelets (pywt) library.
# ============================================================
def wavelet_denoise_trend(df, wavelet="db4", level=2):
    prices = df["close"].tail(128).values
    if len(prices) < 32:
        return {"wavelet_trend_direction": None, "signal": "INSUFFICIENT_DATA"}

    try:
        import pywt
        coeffs = pywt.wavedec(prices, wavelet, level=level)
        threshold = np.std(coeffs[-1]) * 0.6745
        denoised_coeffs = [coeffs[0]] + [pywt.threshold(c, threshold, mode="soft") for c in coeffs[1:]]
        denoised = pywt.waverec(denoised_coeffs, wavelet)[:len(prices)]

        trend_slope = float(denoised[-1] - denoised[-5]) if len(denoised) >= 5 else 0.0
        direction = "UP" if trend_slope > 0 else ("DOWN" if trend_slope < 0 else "FLAT")

        return {
            "wavelet_denoised_last": round(float(denoised[-1]), 2),
            "wavelet_trend_direction": direction,
            "wavelet_trend_slope": round(trend_slope, 4),
        }
    except ImportError:
        return {"wavelet_denoised_last": None, "signal": "PYWT_NOT_INSTALLED"}
    except Exception as e:
        return {"wavelet_denoised_last": None, "error": str(e)}


# ============================================================
# 18. STRUCTURAL BREAK DETECTION (CUSUM TEST)  <-- (v6)
# Formula: S_t = max(0, S_{t-1} + (dy_t - mu0) - threshold)
#
# LIMITATION (honestly): threshold_k manually chuna gaya hai (data se
# calibrate nahi hua), isliye false-positive break-detection rate
# unknown hai bina proper backtesting ke.
# ============================================================
def cusum_structural_break(df, lookback=100, threshold_k=0.5):
    returns = df["close"].pct_change().dropna().tail(lookback)
    if len(returns) < 20:
        return {"structural_break": False, "cusum_pos": None, "cusum_neg": None}

    mu0 = returns.mean()
    std = returns.std()
    threshold = threshold_k * std

    s_pos, s_neg = 0.0, 0.0
    recent_breaks = 0
    for r in returns:
        s_pos = max(0.0, s_pos + (r - mu0) - threshold)
        s_neg = min(0.0, s_neg + (r - mu0) + threshold)
        if s_pos > 4 * std or abs(s_neg) > 4 * std:
            recent_breaks += 1
            s_pos, s_neg = 0.0, 0.0

    structural_break_detected = (s_pos > 4 * std) or (abs(s_neg) > 4 * std)

    return {
        "structural_break": bool(structural_break_detected),
        "cusum_pos": round(float(s_pos), 6),
        "cusum_neg": round(float(s_neg), 6),
        "recent_break_count": recent_breaks,
    }


# ============================================================
# 19. LIQUIDITY SWEEP / STOP-CLUSTER DETECTION  <-- (v6)
# Formula: LiquidityPoolScore = sum( Volume_orders / |P_current - P_level| )
#
# LIMITATION (honestly): "Historical highs/lows" sirf isi fetch kiye
# gaye OHLCV window (last ~50-200 candles) se liye ja rahe hain - asal
# institutional stop-hunt levels aksar bohot purane (weekly/monthly)
# highs-lows par hote hain jo yahan capture nahi ho rahe. Order-book
# cluster wala hissa is function mein duplicate nahi kiya - wo pehle
# se hi concept #13 (Depth Profiling) mein cover ho raha hai.
# ============================================================
def liquidity_sweep_detector(df, lookback=50):
    recent = df.tail(lookback)
    swing_high = float(recent["high"].max())
    swing_low = float(recent["low"].min())
    current_price = float(df["close"].iloc[-1])

    dist_to_high_pct = round(abs(current_price - swing_high) / current_price * 100, 2)
    dist_to_low_pct = round(abs(current_price - swing_low) / current_price * 100, 2)

    sweep_detected = False
    sweep_direction = None

    last_candle = df.iloc[-1]
    prev_candles = df.iloc[-lookback:-1]

    if not prev_candles.empty:
        prev_high = prev_candles["high"].max()
        prev_low = prev_candles["low"].min()

        if last_candle["high"] > prev_high and last_candle["close"] < prev_high:
            sweep_detected = True
            sweep_direction = "SWEPT_HIGH_REVERSED_DOWN"
        elif last_candle["low"] < prev_low and last_candle["close"] > prev_low:
            sweep_detected = True
            sweep_direction = "SWEPT_LOW_REVERSED_UP"

    return {
        "swing_high": round(swing_high, 2),
        "swing_low": round(swing_low, 2),
        "distance_to_high_pct": dist_to_high_pct,
        "distance_to_low_pct": dist_to_low_pct,
        "liquidity_sweep_detected": sweep_detected,
        "sweep_direction": sweep_direction,
    }


# ============================================================
# MASTER FUNCTION - sab 19 concepts combine karta hai
# *** FINAL VERDICT + CONFIDENCE ab bhi SIRF Hawkes + Bayesian se
#     bante hain (Conformal Prediction), v4 jaisa hi - ISE CHANGE
#     NAHI KIYA GAYA. Baaqi concepts sirf extra info hain. ***
# ============================================================
def generate_signal(df, symbol="BTC/USDT", include_orderbook=True):
    df["rsi"] = ta.rsi(df["close"], length=14)
    macd = ta.macd(df["close"])
    df["macd"] = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    df = df.dropna(subset=["rsi", "macd", "macd_signal"]).reset_index(drop=True)

    latest = df.iloc[-1]
    current_price = float(latest["close"])

    # --- Purane 5 concepts (VERDICT YAHAN SE BANTA HAI - UNCHANGED) ---
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

    # --- v4 ke 4 concepts (display-only, UNCHANGED) ---
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

    fake_breakout_warning = False
    if ofi_data.get("ofi_score") is not None:
        if final_verdict == "LONG" and ofi_data["ofi_score"] < 0:
            fake_breakout_warning = True
        elif final_verdict == "SHORT" and ofi_data["ofi_score"] > 0:
            fake_breakout_warning = True

    # --- v6 ke 9 concepts (display-only, verdict ko touch nahi karte) ---
    try:
        divergence_data = cross_asset_divergence(df, symbol)
    except Exception as e:
        divergence_data = {"divergence_score": None, "error": str(e)}

    try:
        entropy_data = multi_timeframe_entropy(df)
    except Exception as e:
        entropy_data = {"entropy_avg": None, "error": str(e)}

    if include_orderbook:
        try:
            depth_data = order_book_depth_profile(symbol)
        except Exception as e:
            depth_data = {"depth_slope": None, "error": str(e)}
    else:
        depth_data = {"depth_slope": None, "wall_bias": "SKIPPED"}

    try:
        vwap_data = vwap_deviation(df, vpin_score=vpin_data.get("vpin_score"))
    except Exception as e:
        vwap_data = {"vwap_deviation_z": None, "error": str(e)}

    try:
        rl_data = rl_risk_agent(
            volatility_pct=volatility_data.get("expected_volatility_pct", 1.0),
            base_risk_pct=suggested_risk_pct,
        )
    except Exception as e:
        rl_data = {"rl_state": None, "error": str(e)}

    try:
        hurst_data = hurst_exponent(df)
    except Exception as e:
        hurst_data = {"hurst": None, "error": str(e)}

    try:
        wavelet_data = wavelet_denoise_trend(df)
    except Exception as e:
        wavelet_data = {"wavelet_trend_direction": None, "error": str(e)}

    try:
        cusum_data = cusum_structural_break(df)
    except Exception as e:
        cusum_data = {"structural_break": None, "error": str(e)}

    try:
        sweep_data = liquidity_sweep_detector(df)
    except Exception as e:
        sweep_data = {"liquidity_sweep_detected": None, "error": str(e)}

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

        # v4 concepts (display-only)
        "order_flow": ofi_data,
        "toxic_flow": vpin_data,
        "market_regime": regime_data,
        "jump_shock": jump_data,
        "meta_label": meta_data,
        "fake_breakout_warning": fake_breakout_warning,

        # v6 concepts (display-only)
        "intermarket_divergence": divergence_data,
        "entropy": entropy_data,
        "depth_profile": depth_data,
        "vwap_deviation": vwap_data,
        "rl_risk_agent": rl_data,
        "hurst": hurst_data,
        "wavelet_trend": wavelet_data,
        "structural_break": cusum_data,
        "liquidity_sweep": sweep_data,

        "disclaimer": ("Probability estimates only - not financial advice. "
                        "In-sample calculations, no walk-forward backtest run yet. "
                        "Final verdict/confidence come ONLY from Hawkes+Bayesian "
                        "(Conformal Prediction); all other panels are display-only."),
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
    return jsonify(AVAILABLE_COINS)


# ============================================================
# NEW (v7): /candles  -->  "LIVE CHART" tab ke liye OHLCV series
#
# Frontend har chand second baad (poll) chhote limit ke sath is
# endpoint ko dobara call karta hai taake chart ka AAKHRI candle aur
# current price update hote rahein - is tarah "live" feel milti hai.
# Note: ye ek naya display-only endpoint hai, /signal ke verdict
# logic ko bilkul touch nahi karta.
# ============================================================
@app.route("/candles", methods=["GET"])
def candles_endpoint():
    coin = request.args.get("coin", "BTC/USDT")
    timeframe = request.args.get("timeframe", "1h")
    try:
        limit = int(request.args.get("limit", 200))
    except ValueError:
        limit = 200
    limit = max(2, min(limit, 1000))

    try:
        df = get_candles(symbol=coin, timeframe=timeframe, limit=limit)
        candles = [
            {
                "time": int(row.timestamp.timestamp()),
                "open": round(float(row.open), 8),
                "high": round(float(row.high), 8),
                "low": round(float(row.low), 8),
                "close": round(float(row.close), 8),
                "volume": round(float(row.volume), 8),
            }
            for row in df.itertuples()
        ]
        last_price = float(df["close"].iloc[-1])
        prev_price = float(df["close"].iloc[-2]) if len(df) > 1 else last_price
        change_pct = round(((last_price - prev_price) / prev_price) * 100, 3) if prev_price else 0.0

        return jsonify({
            "coin": coin,
            "timeframe": timeframe,
            "candles": candles,
            "last_price": round(last_price, 8),
            "change_pct": change_pct,
            "server_time": int(time.time()),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)


# ============================================================
# LIQUIDITY SCANNER (Binance USD-M Futures) — NEW ADDITION
# ============================================================
BINANCE_FAPI = "https://fapi.binance.com"


def fetch_json(path, params=None, timeout=8):
    try:
        r = requests.get(BINANCE_FAPI + path, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"[warn] request failed for {path}: {e}")
        return None


def fetch_ticker(symbol):
    return fetch_json("/fapi/v1/ticker/24hr", {"symbol": symbol})


def fetch_mark_price(symbol):
    return fetch_json("/fapi/v1/premiumIndex", {"symbol": symbol})


def fetch_open_interest(symbol):
    return fetch_json("/fapi/v1/openInterest", {"symbol": symbol})


def fetch_depth(symbol, limit=100):
    return fetch_json("/fapi/v1/depth", {"symbol": symbol, "limit": limit})


def compute_bias(bids, asks, levels=50):
    """Buy vs sell pressure from order book depth (qty-weighted)."""
    buy_qty = sum(float(q) for _, q in bids[:levels])
    sell_qty = sum(float(q) for _, q in asks[:levels])
    total = buy_qty + sell_qty
    if total == 0:
        return 50.0, 50.0
    buy_pct = (buy_qty / total) * 100
    sell_pct = 100 - buy_pct
    return buy_pct, sell_pct


def largest_wall(levels, side_label):
    """Find the single price level with the largest resting quantity."""
    if not levels:
        return None
    best = max(levels, key=lambda lv: float(lv[1]))
    price, qty = float(best[0]), float(best[1])
    return {"side": side_label, "price": price, "qty": qty, "notional": price * qty}


# In-memory spoof detection history (per-process, max 5 snapshots)
_wall_history = deque(maxlen=5)


def detect_spoof(history, current_walls, threshold_notional=1_000_000):
    """
    Very simple spoofing heuristic: a big wall (>threshold) that was
    present in a recent snapshot but is now gone or drastically reduced.
    Not a real spoofing detector — illustrative only.
    """
    flags = []
    for prev_wall in history:
        if prev_wall is None:
            continue
        still_there = False
        for cur in current_walls:
            if cur and abs(cur["price"] - prev_wall["price"]) < prev_wall["price"] * 0.0005:
                if cur["qty"] >= prev_wall["qty"] * 0.5:
                    still_there = True
        if not still_there and prev_wall["notional"] >= threshold_notional:
            flags.append(prev_wall)
    return flags


@app.route("/liquidity", methods=["GET"])
def liquidity_endpoint():
    """
    Returns live liquidity scanner data for the given symbol.
    Query params:
      - symbol: e.g., BTCUSDT, ETHUSDT (Binance futures format)
    """
    symbol = request.args.get("symbol", "BTCUSDT").upper()

    ticker = fetch_ticker(symbol)
    mark = fetch_mark_price(symbol)
    oi = fetch_open_interest(symbol)
    depth = fetch_depth(symbol, limit=100)

    if not depth:
        return jsonify({"error": "failed to fetch order book"}), 400

    bids = depth.get("bids", [])
    asks = depth.get("asks", [])

    buy_pct, sell_pct = compute_bias(bids, asks)
    bid_wall = largest_wall(bids, "BID")
    ask_wall = largest_wall(asks, "ASK")

    spoof_flags = detect_spoof(list(_wall_history), [bid_wall, ask_wall])
    _wall_history.append(bid_wall)
    _wall_history.append(ask_wall)

    price = float(ticker["lastPrice"]) if ticker else None
    high = float(ticker["highPrice"]) if ticker else None
    low = float(ticker["lowPrice"]) if ticker else None
    vol_usd = float(ticker["quoteVolume"]) if ticker else None
    pct_change = float(ticker["priceChangePercent"]) if ticker else None
    mark_price = float(mark["markPrice"]) if mark else None
    funding_rate = float(mark["lastFundingRate"]) * 100 if mark else None
    open_interest = float(oi["openInterest"]) * price if (oi and price) else None

    bias_tag = "BULLISH" if buy_pct > 55 else "BEARISH" if sell_pct > 55 else "NEUTRAL"

    return jsonify({
        "symbol": symbol,
        "price": price,
        "change_24h_pct": pct_change,
        "high_24h": high,
        "low_24h": low,
        "volume_24h_usd": vol_usd,
        "mark_price": mark_price,
        "funding_rate_pct": funding_rate,
        "open_interest_usd": open_interest,
        "buy_pressure_pct": round(buy_pct, 1),
        "sell_pressure_pct": round(sell_pct, 1),
        "bias": bias_tag,
        "bid_wall": bid_wall,
        "ask_wall": ask_wall,
        "spoof_flags": spoof_flags,
        "disclaimer": "Probability estimates only — not financial advice. Heuristics are illustrative only."
    })
