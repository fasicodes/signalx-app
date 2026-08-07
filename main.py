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

---------------------------------------------------------------------------
PATCH NOTE (is file mein): /liquidity route ko resilient bana diya gaya hai.
Pehle is route ke andar kuch calculations (sweep detector, hawkes pressure,
zones, spoofing, crash risk) bina try/except ke seedha call ho rahe thay -
agar in mein se kisi ek mein bhi koi chhota runtime error aata (jaise
order-book empty, ya edge-case data), to POORA route 400 (Bad Request)
return kar deta tha, chahe baaqi sab data theek se mil raha hota.
Ab har sub-calculation apne alag try/except mein hai (waisa hi jaisa
generate_signal() mein already ho raha hai) - agar ek panel fail ho to
sirf wo panel "N/A"/None dikhata hai, baaqi route normally kaam karta hai.
Sirf candle-fetch fail hone par (invalid coin/timeframe) hi 400 aata hai,
aur us waqt error message clear batata hai ke asal wajah kya thi.
---------------------------------------------------------------------------
"""

import os
import time

from flask import Flask, jsonify, request, render_template, redirect, url_for, session
from flask_cors import CORS
import pandas as pd
import numpy as np
import ccxt

# pandas-ta is optional: it is not published for every Python version, so we
# ship a numpy/pandas fallback for the two indicators generate_signal() needs
# (RSI + MACD) that produces equivalent columns. The verdict/confidence logic
# itself is untouched either way.
try:
    import pandas_ta as ta
    _HAS_PANDAS_TA = True
except ImportError:  # pragma: no cover - exercised only when pandas-ta is absent
    ta = None
    _HAS_PANDAS_TA = False


def _ta_rsi(series, length=14):
    """Wilder-smoothed RSI (same formula as pandas_ta.rsi)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.where((avg_loss != 0) | (avg_gain != 0), 100.0)


def _ta_macd(series, fast=12, slow=26, signal=9):
    """EMA-based MACD (same formula as pandas_ta.macd)."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    return macd, signal_line

# v4 ke naye concepts ke liye extra libraries
from hmmlearn.hmm import GaussianHMM
from sklearn.ensemble import RandomForestClassifier

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
CORS(app, supports_credentials=True)

from auth import auth_bp
app.register_blueprint(auth_bp)

exchange = ccxt.okx()

# In-memory cache for the "Possible Spoofing" heuristic (Ch.21). Keyed by
# symbol, holds the last order-book snapshot so the NEXT /liquidity request
# can compare against it and see which large resting orders vanished.
# NOTE: this is per-process memory - fine for a single Railway dyno, but
# resets on restart and won't be shared across multiple workers/instances.
_OB_SNAPSHOT_CACHE = {}

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


def _clean_order_book(ob):
    """OKX (via ccxt) kabhi kabhi har bid/ask level mein 2 se zyada values
    bhejta hai (e.g. [price, amount, extra_field]). Neeche code mein
    `for price, vol in bids` jaisi unpacking sirf exactly 2 values
    expect karti hai, isliye poore book ko yahan strictly [price, amount]
    tak trim kar dete hain taake koi bhi downstream function crash na ho."""
    def clean_side(levels):
        cleaned = []
        for lvl in levels or []:
            if len(lvl) >= 2:
                cleaned.append([float(lvl[0]), float(lvl[1])])
        return cleaned

    return {
        "bids": clean_side(ob.get("bids")),
        "asks": clean_side(ob.get("asks")),
    }


# seconds per candle, used to work out a `since` timestamp when the
# frontend asks for older history ("before" a given time) so infinite
# scroll-back can page further into the past instead of hitting a wall.
TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "1w": 604800, "1M": 2592000, "3M": 7776000,
}


def get_candles(symbol="BTC/USDT", timeframe="1h", limit=200, since=None):
    """Exchange se OHLCV candles fetch karta hai. `since` (ms epoch) diya
    jaye to us waqt se aage ki candles milti hain -- older-history
    pagination isi se ban'ti hai."""
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, since=since)
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
    ob1 = _clean_order_book(exchange.fetch_order_book(symbol, limit=5))
    time.sleep(snapshot_gap_sec)
    ob2 = _clean_order_book(exchange.fetch_order_book(symbol, limit=5))

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
def order_book_depth_profile(symbol="BTC/USDT", depth=10, order_book=None):
    try:
        ob = order_book if order_book is not None else exchange.fetch_order_book(symbol, limit=depth)
        ob = _clean_order_book(ob)
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
# 20. LIQUIDITY MAGNET + LIKELY TARGET  <-- (v8, LIQUIDITY SCANNER)
# Formula: Magnet = argmax(Price_i * Volume_i) across order-book levels
#          LikelyTarget = argmax( (Price_i * Volume_i) / Distance_i ),
#          nudged toward whichever side liquidity_sweep_detector already
#          flagged as the "continuation" direction.
#
# LIMITATION (honestly): ye sirf currently-fetched top-N order-book levels
# se ban raha hai (retail REST depth) - asal "liquidity magnet" institutional
# desks bohot zyada depth + historical resting-order data se banate hain.
# ============================================================
def liquidity_magnet_and_target(current_price, order_book, sweep_data=None, depth=25):
    bids = order_book.get("bids", [])[:depth]
    asks = order_book.get("asks", [])[:depth]
    if not bids or not asks:
        return {"magnet": None, "likely_target": None}

    clusters = [{"price": p, "usd": p * v, "side": "SUPPORT"} for p, v in bids]
    clusters += [{"price": p, "usd": p * v, "side": "RESISTANCE"} for p, v in asks]

    magnet = max(clusters, key=lambda c: c["usd"])
    magnet_out = {
        "price": round(magnet["price"], 6),
        "usd_size": round(magnet["usd"], 2),
        "side": magnet["side"],
        "distance_pct": round(abs(current_price - magnet["price"]) / current_price * 100, 3),
    }

    def score(c):
        dist = max(abs(current_price - c["price"]), 1e-9)
        s = c["usd"] / dist
        sweep_dir = (sweep_data or {}).get("sweep_direction")
        if sweep_dir == "SWEPT_LOW_REVERSED_UP" and c["side"] == "RESISTANCE":
            s *= 1.25
        if sweep_dir == "SWEPT_HIGH_REVERSED_DOWN" and c["side"] == "SUPPORT":
            s *= 1.25
        return s

    ranked = sorted(clusters, key=score, reverse=True)
    best = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else ranked[0]
    s1, s2 = score(best), score(runner_up)
    dominance = (s1 - s2) / s1 if s1 > 0 else 0.0
    target_score = round(60 + dominance * 39, 1)

    target_out = {
        "price": round(best["price"], 6),
        "score": target_score,
        "type": "Resistance Sweep" if best["side"] == "RESISTANCE" else "Support Sweep",
        "distance_pct": round(abs(current_price - best["price"]) / current_price * 100, 3),
    }
    return {"magnet": magnet_out, "likely_target": target_out}


# ============================================================
# 21. POSSIBLE SPOOFING DETECTOR  <-- (v8, LIQUIDITY SCANNER)
# Formula: flags a resting order-book level whose size drops by
#          >= cancel_ratio between two polls without price trading
#          through it.
#
# LIMITATION (honestly): asal spoofing detection order-by-order (L3)
# add/cancel event stream aur trade-tape matching maangta hai. Yahan
# sirf do polled L2 snapshots compare ho rahe hain - ye "order vanish
# hua" dikha sakta hai, lekin ye fill tha ya genuine cancel, ye REST
# API se pakka nahi bataya ja sakta. Isliye heuristic/exploratory hai.
# ============================================================
def possible_spoofing_detector(symbol, order_book, top_n=5, min_elapsed_sec=3, cancel_ratio=0.6):
    now = time.time()
    bids = order_book.get("bids", [])[:top_n]
    asks = order_book.get("asks", [])[:top_n]
    if not bids or not asks:
        return {"available": False, "spoof_detected": False}

    current_levels = {round(p, 6): v for p, v in (bids + asks)}
    mid = (bids[0][0] + asks[0][0]) / 2

    prev = _OB_SNAPSHOT_CACHE.get(symbol)
    _OB_SNAPSHOT_CACHE[symbol] = {"levels": current_levels, "ts": now, "mid": mid}

    if not prev or (now - prev["ts"]) < min_elapsed_sec:
        return {"available": True, "spoof_detected": False, "note": "Collecting baseline snapshot..."}

    vanished = []
    for price, vol in prev["levels"].items():
        if vol <= 0:
            continue
        current_vol = current_levels.get(price, 0.0)
        drop_ratio = (vol - current_vol) / vol
        if drop_ratio >= cancel_ratio:
            vanished.append({
                "price": round(price, 6),
                "usd_size_before": round(price * vol, 2),
                "cancelled_pct": round(min(drop_ratio, 1.0) * 100, 1),
                "seconds_ago": round(now - prev["ts"], 1),
            })

    vanished.sort(key=lambda v: v["usd_size_before"], reverse=True)
    top = vanished[0] if vanished else None
    spoof_score = round(min(100, len(vanished) * 25 + (top["cancelled_pct"] * 0.3 if top else 0)), 1)

    return {
        "available": True,
        "spoof_detected": bool(vanished),
        "spoof_score": spoof_score,
        "top_vanished_level": top,
        "vanished_count": len(vanished),
    }


# ============================================================
# 22. MARKET STRENGTH SCORE  <-- (v8, LIQUIDITY SCANNER)
# Blends Hawkes pressure, OFI and depth-slope into a single 0-100 dial.
# Purely a display convenience - does NOT feed into the Tier 01 verdict.
# ============================================================
def market_strength_score(buying_pressure, selling_pressure, ofi_score, depth_slope, vpin_score):
    bp = ((buying_pressure or 0) - (selling_pressure or 0)) / 10.0
    ofi_n = (ofi_score or 0) / 10.0
    depth_n = float(np.tanh((depth_slope or 0) / 5.0))
    toxicity_penalty = min(0.4, (vpin_score or 0) * 0.3)

    raw = (bp * 0.4 + ofi_n * 0.35 + depth_n * 0.25) * (1 - toxicity_penalty)
    score = round(float(np.clip(50 + raw * 50, 0, 100)), 1)

    if score >= 70:
        label = "STRONG"
    elif score >= 55:
        label = "MODERATE BULLISH"
    elif score > 45:
        label = "NEUTRAL"
    elif score >= 30:
        label = "MODERATE BEARISH"
    else:
        label = "WEAK"

    bias = "BUY" if score >= 55 else ("SELL" if score <= 45 else "NEUTRAL")
    return {"score": score, "label": label, "bias": bias}


# ============================================================
# 23. TRAP & SQUEEZE RISK  <-- (v8, LIQUIDITY SCANNER)
# Heuristic 0-100 bars for Bull Trap / Bear Trap / Short Squeeze / Long
# Squeeze, combining the sweep direction, OFI and order-book wall bias.
# ============================================================
def trap_and_squeeze_risk(sweep_data, ofi_data, depth_data, funding_data=None):
    ofi = ofi_data.get("ofi_score") or 0
    wall = depth_data.get("wall_bias")
    sweep_dir = sweep_data.get("sweep_direction")

    bull_trap = bear_trap = short_squeeze = long_squeeze = 0

    if sweep_dir == "SWEPT_HIGH_REVERSED_DOWN":
        bull_trap += 55
        if ofi < 0:
            bull_trap += 25
        if wall == "ASK_WALL_HEAVIER":
            bull_trap += 10

    if sweep_dir == "SWEPT_LOW_REVERSED_UP":
        bear_trap += 55
        if ofi > 0:
            bear_trap += 25
        if wall == "BID_WALL_HEAVIER":
            bear_trap += 10

    if ofi > 3 and wall == "ASK_WALL_HEAVIER":
        short_squeeze += 50
    if ofi < -3 and wall == "BID_WALL_HEAVIER":
        long_squeeze += 50

    if funding_data and funding_data.get("available") and funding_data.get("funding_rate_pct") is not None:
        fr = funding_data["funding_rate_pct"]
        if fr < 0:
            short_squeeze += 25
        elif fr > 0.05:
            long_squeeze += 20

    clip = lambda v: int(min(100, max(0, v)))
    return {
        "bull_trap": clip(bull_trap),
        "bear_trap": clip(bear_trap),
        "short_squeeze": clip(short_squeeze),
        "long_squeeze": clip(long_squeeze),
    }


# ============================================================
# 24. LIQUIDITY TARGET ZONES  <-- (v8, LIQUIDITY SCANNER)
# Top buy/sell walls straight from the order book, scored relative to
# the largest resting order currently visible.
# ============================================================
def liquidity_target_zones(order_book, current_price, n_levels=4, scan_depth=25):
    bids_all = order_book.get("bids", [])[:scan_depth]
    asks_all = order_book.get("asks", [])[:scan_depth]
    if not bids_all or not asks_all:
        return []

    max_usd = max([p * v for p, v in (bids_all + asks_all)] or [1])

    bids = sorted(bids_all, key=lambda x: x[0] * x[1], reverse=True)[:n_levels]
    asks = sorted(asks_all, key=lambda x: x[0] * x[1], reverse=True)[:n_levels]

    zones = []
    for price, vol in bids:
        usd = price * vol
        zones.append({
            "price": round(price, 6), "side": "BUY_WALL", "usd_size": round(usd, 2),
            "score": round(min(99, (usd / max_usd) * 99), 1),
            "distance_pct": round((current_price - price) / current_price * 100, 3),
        })
    for price, vol in asks:
        usd = price * vol
        zones.append({
            "price": round(price, 6), "side": "SELL_WALL", "usd_size": round(usd, 2),
            "score": round(min(99, (usd / max_usd) * 99), 1),
            "distance_pct": round((price - current_price) / current_price * 100, 3),
        })
    zones.sort(key=lambda z: z["score"], reverse=True)
    return zones


# ============================================================
# 25. FUNDING RATE + OPEN INTEREST  <-- (v8, LIQUIDITY SCANNER)
#
# LIMITATION (honestly): OKX ye sirf PERPETUAL SWAP instruments ke liye
# deta hai, spot pairs ke liye nahi. Har coin ka perp OKX par available
# nahi hota (e.g. kuch chhoti-cap coins) - un ke liye ye gracefully
# "available: false" return karta hai, page crash nahi hoti.
# ============================================================
def funding_open_interest(symbol):
    perp_symbol = symbol if ":" in symbol else f"{symbol}:USDT"

    funding_rate_pct, next_funding_ts = None, None
    try:
        funding = exchange.fetch_funding_rate(perp_symbol)
        rate = funding.get("fundingRate")
        if rate is not None:
            funding_rate_pct = round(float(rate) * 100, 4)
        next_funding_ts = funding.get("fundingTimestamp")
    except Exception:
        pass

    open_interest = None
    try:
        oi = exchange.fetch_open_interest(perp_symbol)
        oi_value = oi.get("openInterestAmount") or oi.get("openInterestValue") or oi.get("openInterest")
        if oi_value is not None:
            open_interest = round(float(oi_value), 2)
    except Exception:
        pass

    return {
        "available": funding_rate_pct is not None or open_interest is not None,
        "funding_rate_pct": funding_rate_pct,
        "open_interest": open_interest,
        "next_funding_ts": next_funding_ts,
        "perp_symbol": perp_symbol,
    }


# ============================================================
# 26. CVD (CUMULATIVE VOLUME DELTA)  <-- (v8, LIQUIDITY SCANNER)
# Formula: delta_i = +Volume_i agar close_i >= open_i warna -Volume_i;
#          CVD_t = cumsum(delta)
#
# LIMITATION (honestly): asal CVD taker buy-volume minus taker
# sell-volume se banta hai (trade-by-trade tape se). Yahan candle
# direction (green/red) ko proxy ke taur par use kiya gaya hai kyunke
# taker-side per-trade data har candle ke liye fetch karna bohot
# zyada API calls maangta - ye ek approximation hai, exact CVD nahi.
# ============================================================
def cvd_volume_delta(df, lookback=50):
    recent = df.tail(lookback).copy()
    recent["delta"] = np.where(recent["close"] >= recent["open"], recent["volume"], -recent["volume"])
    recent["cvd"] = recent["delta"].cumsum()
    series = [round(float(v), 4) for v in recent["cvd"].tolist()][-30:]
    cvd_now = series[-1] if series else 0.0
    cvd_prev = series[-6] if len(series) >= 6 else (series[0] if series else 0.0)
    trend = "RISING" if cvd_now > cvd_prev else ("FALLING" if cvd_now < cvd_prev else "FLAT")
    return {"cvd": round(cvd_now, 2), "trend": trend, "series": series}


# ============================================================
# 27. MARKET CRASH RISK  <-- (v8, LIQUIDITY SCANNER)
# Heuristic 0-100 composite of existing down-side stress signals
# (jump diffusion, structural break, VPIN toxicity, OFI, sweep, CVD).
#
# LIMITATION (honestly): ye koi calibrated/backtested crash-prediction
# model NAHI hai - sirf maujooda display-only signals ko ek weighted
# checklist mein combine kiya gaya hai. Sirf awareness ke liye hai,
# trading decision ke liye nahi.
# ============================================================
def market_crash_risk(jump_data, cusum_data, vpin_data, ofi_data, sweep_data, cvd_data):
    score = 0
    factors = []

    if jump_data.get("jump_detected") and jump_data.get("jump_direction") == "DOWN":
        score += 30
        factors.append("Downside price jump detected (Ch.09)")
    if cusum_data.get("structural_break"):
        score += 20
        factors.append("Structural break in returns (Ch.18)")

    vpin = vpin_data.get("vpin_score")
    if vpin is not None and vpin > 0.6:
        score += 20
        factors.append("High toxic order flow (VPIN)")

    ofi = ofi_data.get("ofi_score")
    if ofi is not None and ofi < -4:
        score += 15
        factors.append("Heavy sell-side order flow")

    if sweep_data.get("sweep_direction") == "SWEPT_HIGH_REVERSED_DOWN":
        score += 10
        factors.append("Liquidity sweep reversal at highs")

    if cvd_data.get("trend") == "FALLING":
        score += 5
        factors.append("Falling cumulative volume delta")

    score = min(100, score)
    if score >= 65:
        label = "ELEVATED"
    elif score >= 35:
        label = "WATCH"
    else:
        label = "LOW"

    return {"score": score, "label": label, "factors": factors}


# ============================================================
# MASTER FUNCTION - sab 19 concepts combine karta hai
# *** FINAL VERDICT + CONFIDENCE ab bhi SIRF Hawkes + Bayesian se
#     bante hain (Conformal Prediction), v4 jaisa hi - ISE CHANGE
#     NAHI KIYA GAYA. Baaqi concepts sirf extra info hain. ***
# ============================================================
def generate_signal(df, symbol="BTC/USDT", include_orderbook=True):
    if _HAS_PANDAS_TA:
        df["rsi"] = ta.rsi(df["close"], length=14)
        macd = ta.macd(df["close"])
        df["macd"] = macd["MACD_12_26_9"]
        df["macd_signal"] = macd["MACDs_12_26_9"]
    else:
        df["rsi"] = _ta_rsi(df["close"], 14)
        macd_line, macd_signal_line = _ta_macd(df["close"])
        df["macd"] = macd_line
        df["macd_signal"] = macd_signal_line
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
@app.route("/login", methods=["GET"])
def login_page():
    # Agar user pehle se login hai to seedha trading interface pe bhej dein
    if "user_id" in session:
        return redirect(url_for("home"))
    return render_template("login.html")


@app.route("/", methods=["GET"])
def home():
    # Trading interface sirf logged-in users ko dikhega
    if "user_id" not in session:
        return redirect(url_for("login_page"))
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
# NEW: /ticker  -->  homepage "Awaiting Analysis" strip ke liye
# chand top coins ka live last price + 24h % change deta hai.
# Display-only hai, /signal verdict logic ko bilkul touch nahi karta.
# ============================================================
TICKER_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "DOT/USDT", "XLM/USDT", "DOGE/USDT",
]


@app.route("/ticker", methods=["GET"])
def ticker_strip():
    try:
        tickers = exchange.fetch_tickers(TICKER_SYMBOLS)
        out = []
        for sym in TICKER_SYMBOLS:
            t = tickers.get(sym)
            if not t:
                continue
            out.append({
                "symbol": sym.split("/")[0],
                "last": t.get("last"),
                "changePercent": t.get("percentage"),
            })
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


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

    # infinite scroll-back: frontend sends the oldest candle time it
    # already has as `before` (unix seconds) and we page further into
    # the past from there instead of always returning the latest window.
    before = request.args.get("before")
    since_ms = None
    if before:
        try:
            before_ts = int(before)
            tf_secs = TIMEFRAME_SECONDS.get(timeframe, 3600)
            since_ms = max(0, (before_ts - limit * tf_secs) * 1000)
        except ValueError:
            since_ms = None

    try:
        df = get_candles(symbol=coin, timeframe=timeframe, limit=limit, since=since_ms)
        if before and since_ms is not None:
            before_cutoff = pd.to_datetime(int(before), unit="s")
            df = df[df["timestamp"] < before_cutoff]

        if df.empty:
            # no more history available further back — let the frontend
            # know cleanly instead of erroring out.
            return jsonify({
                "coin": coin, "timeframe": timeframe, "candles": [],
                "last_price": None, "change_pct": 0.0,
                "server_time": int(time.time()),
            })

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


# ============================================================
# NEW (v8): /liquidity  -->  "LIQUIDITY SCANNER" tab (Ch.19-27) ke liye
#
# Ye endpoint /signal se ALAG rakha gaya hai jaan-boojh kar: /signal
# heavy hai (HMM fit + RandomForest fit har baar), jabke ye endpoint
# har ~12-15 sec par frontend se auto-poll ho sakta hai taake scanner
# "dynamic"/live mehsoos ho, bina baar-baar poore 19-channel analysis
# ko dobara chalaye. Verdict/confidence logic ko bilkul touch nahi karta.
#
# PATCH: har sub-calculation ab apne alag try/except mein hai (jaisa
# generate_signal() mein pehle se ho raha tha) - taake ek panel ka
# fail hona poore route ko 400 na de de. Sirf candle-fetch fail hone
# par (invalid coin/timeframe) 400 aata hai, aur us waqt exact wajah
# error message mein saaf batayi jati hai.
# ============================================================
@app.route("/liquidity", methods=["GET"])
def liquidity_endpoint():
    coin = request.args.get("coin", "BTC/USDT")
    timeframe = request.args.get("timeframe", "1h")

    try:
        df = get_candles(symbol=coin, timeframe=timeframe, limit=120)
        current_price = float(df["close"].iloc[-1])
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"candle fetch failed: {e}"}), 400

    try:
        ob = _clean_order_book(exchange.fetch_order_book(coin, limit=50))
    except Exception as e:
        import traceback
        traceback.print_exc()
        ob = {"bids": [], "asks": [], "error": str(e)}

    try:
        sweep_data = liquidity_sweep_detector(df)
    except Exception as e:
        sweep_data = {"liquidity_sweep_detected": None, "error": str(e)}

    try:
        buying_pressure, selling_pressure = hawkes_pressure(df)
    except Exception as e:
        buying_pressure, selling_pressure = None, None

    try:
        ofi_data = order_flow_imbalance(coin, snapshot_gap_sec=0.6)
    except Exception as e:
        ofi_data = {"ofi_score": None, "error": str(e)}

    try:
        depth_data = order_book_depth_profile(coin, depth=20, order_book=ob)
    except Exception as e:
        depth_data = {"depth_slope": None, "wall_bias": None, "error": str(e)}

    try:
        vpin_data = vpin_toxicity(coin)
    except Exception as e:
        vpin_data = {"vpin_score": None, "error": str(e)}

    try:
        jump_data = jump_diffusion_detector(df)
    except Exception as e:
        jump_data = {"jump_detected": None, "error": str(e)}

    try:
        cusum_data = cusum_structural_break(df)
    except Exception as e:
        cusum_data = {"structural_break": None, "error": str(e)}

    try:
        cvd_data = cvd_volume_delta(df)
    except Exception as e:
        cvd_data = {"cvd": None, "trend": None, "series": [], "error": str(e)}

    try:
        funding_data = funding_open_interest(coin)
    except Exception as e:
        funding_data = {"available": False, "error": str(e)}

    try:
        magnet_target = liquidity_magnet_and_target(current_price, ob, sweep_data=sweep_data)
    except Exception as e:
        magnet_target = {"magnet": None, "likely_target": None, "error": str(e)}

    try:
        strength_data = market_strength_score(
            buying_pressure, selling_pressure,
            ofi_data.get("ofi_score"), depth_data.get("depth_slope"), vpin_data.get("vpin_score"),
        )
    except Exception as e:
        strength_data = {"score": None, "label": None, "error": str(e)}

    try:
        trap_squeeze_data = trap_and_squeeze_risk(sweep_data, ofi_data, depth_data, funding_data)
    except Exception as e:
        trap_squeeze_data = {"bull_trap": 0, "bear_trap": 0, "short_squeeze": 0, "long_squeeze": 0, "error": str(e)}

    try:
        zones = liquidity_target_zones(ob, current_price)
    except Exception as e:
        zones = []

    try:
        spoof_data = possible_spoofing_detector(coin, ob)
    except Exception as e:
        spoof_data = {"available": False, "spoof_detected": False, "error": str(e)}

    try:
        crash_data = market_crash_risk(jump_data, cusum_data, vpin_data, ofi_data, sweep_data, cvd_data)
    except Exception as e:
        crash_data = {"score": None, "label": None, "factors": [], "error": str(e)}

    return jsonify({
        "coin": coin,
        "last_price": round(current_price, 6),
        "liquidity_sweep": sweep_data,
        "magnet": magnet_target.get("magnet"),
        "likely_target": magnet_target.get("likely_target"),
        "market_strength": strength_data,
        "possible_spoofing": spoof_data,
        "trap_squeeze": trap_squeeze_data,
        "liquidity_zones": zones,
        "funding_open_interest": funding_data,
        "cvd": cvd_data,
        "crash_risk": crash_data,
        "server_time": int(time.time()),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
