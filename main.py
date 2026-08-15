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
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
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

from datetime import timedelta
app = Flask(__name__)

# Railway (aur zyada tar hosting platforms) apps ko HTTP proxy ke peeche
# chalate hain, isliye Flask ko batana zaroori hai ke asal request HTTPS
# thi - warna OAuth redirect URIs galti se http:// ban jate hain aur
# Google/X "redirect_uri_mismatch" error deta hai.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
import secrets as _secrets

# SECRET_KEY hamesha environment variable se aani chahiye (Railway Variables
# mein set karein). Agar set nahi hai, to hum ek random key generate kar dete
# hain (predictable hardcoded string rakhna security risk hai) - lekin is
# soorat mein har restart par purane sessions/cookies invalid ho jayenge,
# isliye SECRET_KEY zaroor set karein.
_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    _secret_key = _secrets.token_hex(32)
    print("[security] WARNING: SECRET_KEY env var set nahi hai - random key "
          "generate ki gayi hai. Isay Railway Variables mein set karein "
          "warna restart hone par sab log out ho jayenge.")
app.secret_key = _secret_key

# Session cookie settings explicitly set karna zaroori hai taake Google/X
# OAuth redirect flow ke dauran cookie sahi se preserve ho (Railway ke
# proxy environment mein implicit defaults kabhi kabhi sahi kaam nahi
# karte, jisse "state not equal" jaisi CSRF mismatch error aati hai).
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    # Session persistence fix: without this, Flask issues a browser-session
    # cookie that dies the moment the browser/tab is closed, forcing a
    # fresh login every time. Combined with `session.permanent = True` (set
    # at login, in auth.py/oauth.py), the cookie now carries a real
    # Max-Age/Expires so the user stays logged in across browser restarts
    # for up to 30 days, or until they explicitly log out.
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
# CORS ko sirf apni asal domain tak mehdood karte hain - warna koi bhi
# doosri website aapke API/cookies tak access kar sakti thi (security risk).
# Agar aap koi aur domain (misal custom domain) use karein, to
# ALLOWED_ORIGIN environment variable set kar dein.
_allowed_origin = os.environ.get("ALLOWED_ORIGIN", "https://signalx-app-production.up.railway.app")
CORS(app, supports_credentials=True, origins=[_allowed_origin, "http://localhost:5000", "http://127.0.0.1:5000"])

from auth import auth_bp
app.register_blueprint(auth_bp)

# Brute-force/spam se bachne ke liye rate limiting: login/register par
# thori si limit lagate hain (per IP address).
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")
limiter.limit("10 per minute")(auth_bp)

from oauth import oauth_bp, init_oauth
init_oauth(app)
app.register_blueprint(oauth_bp)

from trades import trades_bp
app.register_blueprint(trades_bp)

# Phase 1 upgrade: Watchlist + Alerts/Notification Center. Both reuse the
# existing session-based login (session["user_id"]) and MySQL connection
# helper - no new auth system, no new DB engine.
from watchlist import watchlist_bp
app.register_blueprint(watchlist_bp)

from alerts import alerts_bp
app.register_blueprint(alerts_bp)

from chart_drawings import chart_drawings_bp
app.register_blueprint(chart_drawings_bp)

# App start hote hi 'users' table khud ba khud ban jaye (agar pehle se
# maujood nahi hai). Agar DB abhi connect nahi ho pa raha (misal, MySQL
# service abhi tak deploy nahi hui) to sirf warning print hoti hai -
# app crash nahi hota, aur agli request par phir try hoga.
try:
    from db import init_db
    init_db()
except Exception as _db_init_err:
    print(f"[db] WARNING: users table startup par nahi ban saki: {_db_init_err}")

exchange = ccxt.okx()

# In-memory cache for the "Possible Spoofing" heuristic (Ch.21). Keyed by
# symbol, holds the last order-book snapshot so the NEXT /liquidity request
# can compare against it and see which large resting orders vanished.
# NOTE: this is per-process memory - fine for a single Railway dyno, but
# resets on restart and won't be shared across multiple workers/instances.
_OB_SNAPSHOT_CACHE = {}

# Phase 1 upgrade (watchlist.py / alerts.py): lightweight in-memory cache of
# the LAST /signal result per symbol, so the watchlist row + "new signal"
# alerts can show a direction/confidence without re-running the full
# (expensive) 27-concept engine on every poll. Populated only when a user
# actually requests /signal for that symbol (RUN ANALYSIS, or an alert
# check that needs it) - never force-computed in the background, per the
# performance rules (no excessive polling / no duplicate heavy work).
# NOTE: same per-process-memory caveat as _OB_SNAPSHOT_CACHE above.
_SIGNAL_CACHE = {}
_SIGNAL_CACHE_TTL_SEC = 90


def _cache_signal_result(symbol, result):
    _SIGNAL_CACHE[symbol] = {"result": result, "ts": time.time()}


def get_cached_signal(symbol, max_age_sec=None):
    """Last cached /signal result for `symbol`, or None if missing/stale.
    Used by watchlist.py + alerts.py so they never trigger a fresh heavy
    computation themselves."""
    entry = _SIGNAL_CACHE.get(symbol)
    if not entry:
        return None
    max_age = max_age_sec if max_age_sec is not None else _SIGNAL_CACHE_TTL_SEC
    if time.time() - entry["ts"] > max_age:
        return None
    return entry["result"]

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

# Top 30 forex pairs (majors + minors/crosses + a couple of common
# exotics), used by the new Forex tab in the coin/pair picker
# (templates/design.html + static/script.js). NOTE: this list only
# powers *selection* right now - the actual signal engine below
# (get_candles / generate_signal / order-book stuff) talks to OKX via
# ccxt, which is a crypto exchange and does not carry forex pairs. See
# _is_forex_pair() and its use in /signal, /candles and /liquidity for
# the friendly "coming soon" guard that keeps a Forex selection from
# hitting the crypto-only exchange and blowing up with a raw ccxt error.
FOREX_PAIRS = [
    # Majors
    "EUR/USD", "USD/JPY", "GBP/USD", "USD/CHF", "AUD/USD",
    "USD/CAD", "NZD/USD",
    # Minors / crosses
    "EUR/JPY", "GBP/JPY", "EUR/GBP", "EUR/CHF", "AUD/JPY",
    "EUR/AUD", "GBP/CHF", "AUD/NZD", "NZD/JPY", "CAD/JPY",
    "CHF/JPY", "EUR/CAD", "GBP/CAD", "EUR/NZD", "AUD/CAD",
    "GBP/AUD", "GBP/NZD", "AUD/CHF", "NZD/CAD", "NZD/CHF",
    "CAD/CHF",
    # Exotics
    "USD/TRY", "USD/ZAR",
]


def _is_forex_pair(symbol):
    """True agar symbol FOREX_PAIRS mein hai (case-insensitive)."""
    return (symbol or "").upper() in FOREX_PAIRS


def _forex_yf_symbol(pair):
    """'EUR/USD' -> 'EURUSD=X' (Yahoo Finance ka forex ticker format)."""
    base, quote = pair.upper().split("/")
    return f"{base}{quote}=X"


# timeframe -> (yfinance interval, yfinance lookback period). yfinance
# supports these intervals directly; anything else (3m, 2h, 6h, 12h) is
# built below by resampling a smaller supported interval with pandas.
FOREX_YF_DIRECT = {
    "1m": ("1m", "7d"), "5m": ("5m", "60d"), "15m": ("15m", "60d"),
    "30m": ("30m", "60d"), "1h": ("60m", "730d"), "4h": ("60m", "730d"),
    "1d": ("1d", "10y"), "1w": ("1wk", "10y"), "1M": ("1mo", "max"),
    "3M": ("3mo", "max"),
}
# timeframe not directly supported by yfinance -> (base interval to
# fetch, pandas resample rule to build the target timeframe from it).
FOREX_RESAMPLE = {
    "3m": ("1m", "3min"), "2h": ("60m", "2h"),
    "6h": ("60m", "6h"), "12h": ("60m", "12h"),
}


def get_forex_candles(symbol, timeframe="1h", limit=200, since=None):
    """Forex OHLC candles via Yahoo Finance (yfinance) - free, no API key.
    NOTE: unlike the crypto/ccxt path, Yahoo has no order book, funding
    rate, or open-interest data, so this only powers price/candle-based
    features (chart + the price-action channels in generate_signal), not
    the order-book channels (OFI, VPIN, depth profile, spoofing) or the
    Liquidity Scanner, which stay crypto-only - see the /liquidity route.
    """
    import yfinance as yf

    ticker = _forex_yf_symbol(symbol)
    resample_rule = None
    if timeframe in FOREX_RESAMPLE:
        interval, period = FOREX_RESAMPLE[timeframe][0], "60d"
        resample_rule = FOREX_RESAMPLE[timeframe][1]
    else:
        interval, period = FOREX_YF_DIRECT.get(timeframe, ("60m", "730d"))

    data = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    if data is None or data.empty:
        raise ValueError(f"No forex data available for {symbol} ({timeframe})")

    data = data.reset_index()
    time_col = "Datetime" if "Datetime" in data.columns else "Date"
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(data[time_col], utc=True).dt.tz_localize(None),
        "open": data["Open"].astype(float),
        "high": data["High"].astype(float),
        "low": data["Low"].astype(float),
        "close": data["Close"].astype(float),
        "volume": data["Volume"].astype(float) if "Volume" in data.columns else 0.0,
    })

    if resample_rule:
        df = (
            df.set_index("timestamp")
            .resample(resample_rule)
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
            .dropna()
            .reset_index()
        )

    if since is not None:
        since_dt = pd.to_datetime(since, unit="ms")
        df = df[df["timestamp"] >= since_dt].reset_index(drop=True)

    df = df.tail(limit).reset_index(drop=True)
    if df.empty:
        raise ValueError(f"No forex data available for {symbol} ({timeframe}) in the requested range")
    return df


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
    pagination isi se ban'ti hai.
    Forex pairs (FOREX_PAIRS) OKX par nahi milte, wo Yahoo Finance
    (get_forex_candles) se aate hain - dono same-shaped df return
    karte hain isliye har caller (get_candles ke baad) ko farq nahi
    padta symbol crypto tha ya forex."""
    if _is_forex_pair(symbol):
        return get_forex_candles(symbol, timeframe=timeframe, limit=limit, since=since)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, since=since)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def get_live_price(asset):
    """Current price - crypto ke liye OKX ticker, forex ke liye Yahoo
    Finance (yfinance) ka last close. trades.py (Active Trade Tracking)
    isay reuse karta hai taake forex trades bhi track ho sakein - pehle
    ye seedha ccxt exchange.fetch_ticker() use karta tha jo forex symbols
    (jaise 'EUR/USD') par fail ho jata tha kyunke OKX crypto-only hai."""
    if _is_forex_pair(asset):
        df = get_forex_candles(asset, timeframe="1m", limit=1)
        return float(df["close"].iloc[-1])
    ticker = exchange.fetch_ticker(asset)
    return float(ticker["last"])


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
# "ACCURACY SCORE" (Ch.01-19 agreement with the verdict)
# ------------------------------------------------------------
# NOTE: verdict/confidence khud AB BHI sirf 5 concepts (Hawkes +
# Bayesian, Conformal Prediction) se bante hain - ye function usay
# CHANGE nahi karta. Ye sirf ek ALAG display metric hai: baaqi
# concepts (Ch.06-19) ko unke apne output se ek directional "vote"
# (LONG / SHORT / NEUTRAL) diya jata hai, aur phir count kiya jata
# hai ke un 19 mein se kitne is verdict ki taraf ishara kar rahe
# hain. Kai concepts (volatility, kelly sizing, VPIN toxicity,
# entropy, RL agent, hurst memory) apni fitrat mein directional
# nahi hain - unhein hamesha NEUTRAL rakha gaya hai (denominator
# mein shamil hain, lekin kabhi "agree" nahi karte) taake number
# ghalat tareeqe se inflate na ho.
# ============================================================
def _concept_votes(*, buying_pressure, selling_pressure, bullish_pct, bearish_pct,
                    ofi_data, regime_data, jump_data, meta_data, divergence_data,
                    depth_data, vwap_data, wavelet_data, cusum_data, sweep_data,
                    magnet_target_data=None, spoofing_data=None, strength_data=None,
                    trap_squeeze_data=None, target_zones_data=None, funding_data=None,
                    cvd_data=None, crash_risk_data=None):
    votes = []

    def add(ch, name, direction):
        votes.append({"ch": ch, "name": name, "direction": direction})

    # 1. Hawkes Process
    if buying_pressure > selling_pressure:
        add(1, "Hawkes Process", "LONG")
    elif selling_pressure > buying_pressure:
        add(1, "Hawkes Process", "SHORT")
    else:
        add(1, "Hawkes Process", "NEUTRAL")

    # 2. Bayesian Classifier
    if bullish_pct > bearish_pct:
        add(2, "Bayesian Classifier", "LONG")
    elif bearish_pct > bullish_pct:
        add(2, "Bayesian Classifier", "SHORT")
    else:
        add(2, "Bayesian Classifier", "NEUTRAL")

    # 3-5. Not directional signals of their own (volatility sizing, the
    # verdict engine itself, risk sizing) - always neutral.
    add(3, "Quantile Volatility", "NEUTRAL")
    add(4, "Conformal Prediction", "NEUTRAL")
    add(5, "Fractional Kelly", "NEUTRAL")

    # 6. Order Flow Imbalance
    ofi_score = ofi_data.get("ofi_score")
    if ofi_score is None:
        add(6, "Order Flow Imbalance", "NEUTRAL")
    else:
        add(6, "Order Flow Imbalance", "LONG" if ofi_score > 0 else ("SHORT" if ofi_score < 0 else "NEUTRAL"))

    # 7. VPIN - measures toxicity/magnitude only, no direction of its own.
    add(7, "VPIN Toxic Flow", "NEUTRAL")

    # 8. HMM Regime - only has a directional read while actually Trending.
    if regime_data.get("regime") == "Trending" and regime_data.get("state_mean_return_pct") is not None:
        add(8, "HMM Regime", "LONG" if regime_data["state_mean_return_pct"] > 0 else "SHORT")
    else:
        add(8, "HMM Regime", "NEUTRAL")

    # 9. Jump Diffusion - only votes when an actual jump was detected.
    if jump_data.get("jump_detected") and jump_data.get("jump_direction"):
        add(9, "Jump Diffusion", "LONG" if jump_data["jump_direction"] == "UP" else "SHORT")
    else:
        add(9, "Jump Diffusion", "NEUTRAL")

    # 10. Meta-Labeling (win probability vs coin-flip)
    win_p = meta_data.get("meta_win_probability")
    if win_p is None:
        add(10, "Meta-Labeling", "NEUTRAL")
    else:
        add(10, "Meta-Labeling", "LONG" if win_p > 50 else ("SHORT" if win_p < 50 else "NEUTRAL"))

    # 11. Cross-Asset Divergence
    interp = divergence_data.get("interpretation")
    if interp == "ASSET_OUTPERFORMING_BENCHMARK":
        add(11, "Cross-Asset Divergence", "LONG")
    elif interp == "ASSET_UNDERPERFORMING_BENCHMARK":
        add(11, "Cross-Asset Divergence", "SHORT")
    else:
        add(11, "Cross-Asset Divergence", "NEUTRAL")

    # 12. Multi-Timeframe Entropy - measures randomness, not direction.
    add(12, "Multi-Timeframe Entropy", "NEUTRAL")

    # 13. Order Book Depth Profile (wall bias)
    wall = depth_data.get("wall_bias")
    if wall == "BID_WALL_HEAVIER":
        add(13, "Order Book Depth", "LONG")
    elif wall == "ASK_WALL_HEAVIER":
        add(13, "Order Book Depth", "SHORT")
    else:
        add(13, "Order Book Depth", "NEUTRAL")

    # 14. VWAP Deviation - extreme deviation implies mean-reversion the
    # OPPOSITE way (price far above VWAP -> reversion down, and vice versa).
    z = vwap_data.get("vwap_deviation_z")
    if z is not None and z > 2:
        add(14, "VWAP Deviation", "SHORT")
    elif z is not None and z < -2:
        add(14, "VWAP Deviation", "LONG")
    else:
        add(14, "VWAP Deviation", "NEUTRAL")

    # 15. RL Risk Agent - sizes risk, doesn't call direction.
    add(15, "RL Risk Agent", "NEUTRAL")

    # 16. Hurst Exponent - describes memory/regime, not direction.
    add(16, "Hurst Exponent", "NEUTRAL")

    # 17. Wavelet Trend
    wdir = wavelet_data.get("wavelet_trend_direction")
    if wdir == "UP":
        add(17, "Wavelet Trend", "LONG")
    elif wdir == "DOWN":
        add(17, "Wavelet Trend", "SHORT")
    else:
        add(17, "Wavelet Trend", "NEUTRAL")

    # 18. CUSUM Structural Break - direction of whichever side broke.
    if cusum_data.get("structural_break"):
        cusum_pos = cusum_data.get("cusum_pos") or 0
        cusum_neg = cusum_data.get("cusum_neg") or 0
        add(18, "Structural Break", "LONG" if cusum_pos > abs(cusum_neg) else "SHORT")
    else:
        add(18, "Structural Break", "NEUTRAL")

    # 19. Liquidity Sweep
    sdir = sweep_data.get("sweep_direction")
    if sdir == "SWEPT_LOW_REVERSED_UP":
        add(19, "Liquidity Sweep", "LONG")
    elif sdir == "SWEPT_HIGH_REVERSED_DOWN":
        add(19, "Liquidity Sweep", "SHORT")
    else:
        add(19, "Liquidity Sweep", "NEUTRAL")

    # --- Liquidity Scanner concepts (v8, Ch.20-27) ---

    # 20. Liquidity Magnet & Likely Target - "Resistance Sweep" means the
    # highest-scored pull is above price (bullish continuation target),
    # "Support Sweep" means it's below (bearish continuation target).
    magnet_target_data = magnet_target_data or {}
    target_type = (magnet_target_data.get("likely_target") or {}).get("type")
    if target_type == "Resistance Sweep":
        add(20, "Liquidity Magnet & Target", "LONG")
    elif target_type == "Support Sweep":
        add(20, "Liquidity Magnet & Target", "SHORT")
    else:
        add(20, "Liquidity Magnet & Target", "NEUTRAL")

    # 21. Possible Spoofing - anomaly flag only, has no directional
    # opinion of its own (a vanished bid and a vanished ask look the same).
    add(21, "Possible Spoofing", "NEUTRAL")

    # 22. Market Strength Score - already outputs an explicit bias.
    strength_data = strength_data or {}
    strength_bias = strength_data.get("bias")
    if strength_bias == "BUY":
        add(22, "Market Strength", "LONG")
    elif strength_bias == "SELL":
        add(22, "Market Strength", "SHORT")
    else:
        add(22, "Market Strength", "NEUTRAL")

    # 23. Trap & Squeeze Risk - bear-trap/short-squeeze readings lean
    # bullish (trapped shorts / squeezed shorts push price up), bull-trap/
    # long-squeeze readings lean bearish.
    trap_squeeze_data = trap_squeeze_data or {}
    bullish_pressure_ts = (trap_squeeze_data.get("bear_trap", 0) or 0) + (trap_squeeze_data.get("short_squeeze", 0) or 0)
    bearish_pressure_ts = (trap_squeeze_data.get("bull_trap", 0) or 0) + (trap_squeeze_data.get("long_squeeze", 0) or 0)
    if bullish_pressure_ts - bearish_pressure_ts >= 20:
        add(23, "Trap & Squeeze Risk", "LONG")
    elif bearish_pressure_ts - bullish_pressure_ts >= 20:
        add(23, "Trap & Squeeze Risk", "SHORT")
    else:
        add(23, "Trap & Squeeze Risk", "NEUTRAL")

    # 24. Liquidity Target Zones - the single largest resting wall: a
    # BUY_WALL (support) below price leans bullish, a SELL_WALL
    # (resistance) above price leans bearish.
    target_zones_data = target_zones_data or []
    top_zone_side = target_zones_data[0]["side"] if target_zones_data else None
    if top_zone_side == "BUY_WALL":
        add(24, "Liquidity Target Zones", "LONG")
    elif top_zone_side == "SELL_WALL":
        add(24, "Liquidity Target Zones", "SHORT")
    else:
        add(24, "Liquidity Target Zones", "NEUTRAL")

    # 25. Funding Rate + Open Interest - negative funding (shorts paying
    # longs) is a classic contrarian-bullish tell; richly positive
    # funding (crowded longs) is a contrarian-bearish tell.
    funding_data = funding_data or {}
    fr = funding_data.get("funding_rate_pct")
    if funding_data.get("available") and fr is not None:
        if fr < 0:
            add(25, "Funding Rate + OI", "LONG")
        elif fr > 0.05:
            add(25, "Funding Rate + OI", "SHORT")
        else:
            add(25, "Funding Rate + OI", "NEUTRAL")
    else:
        add(25, "Funding Rate + OI", "NEUTRAL")

    # 26. CVD (Cumulative Volume Delta)
    cvd_data = cvd_data or {}
    cvd_trend = cvd_data.get("trend")
    if cvd_trend == "RISING":
        add(26, "CVD Volume Delta", "LONG")
    elif cvd_trend == "FALLING":
        add(26, "CVD Volume Delta", "SHORT")
    else:
        add(26, "CVD Volume Delta", "NEUTRAL")

    # 27. Market Crash Risk - a one-sided (downside-only) stress
    # checklist: an elevated reading is a real bearish tell, but a low
    # reading only means "no elevated downside risk right now", not a
    # bullish vote, so it stays NEUTRAL rather than counting as LONG.
    crash_risk_data = crash_risk_data or {}
    if crash_risk_data.get("label") == "ELEVATED":
        add(27, "Market Crash Risk", "SHORT")
    else:
        add(27, "Market Crash Risk", "NEUTRAL")

    return votes


def concept_accuracy_score(final_verdict, **concept_kwargs):
    votes = _concept_votes(**concept_kwargs)

    # Only concepts that actually cast a directional (LONG/SHORT) vote this
    # time count toward the score. NEUTRAL / non-directional concepts
    # (Quantile Volatility, VPIN, Hurst, Possible Spoofing, etc.) never
    # have an opinion one way or the other, so they shouldn't water down
    # the score as either agreeing or disagreeing - they're just excluded.
    directional_votes = [v for v in votes if v["direction"] in ("LONG", "SHORT")]
    total = len(directional_votes)  # varies signal to signal, out of up to 27

    if final_verdict not in ("LONG", "SHORT") or total == 0:
        # WAIT has no direction to measure agreement against, or nobody
        # is currently signaling a direction at all.
        for v in votes:
            v["agrees"] = False
        return {
            "concept_accuracy_pct": None,
            "concept_agree_count": 0,
            "concept_total": total,
            "concept_votes": votes,
        }

    agree_count = 0
    for v in votes:
        v["agrees"] = v["direction"] == final_verdict
        if v["agrees"]:
            agree_count += 1

    return {
        "concept_accuracy_pct": round((agree_count / total) * 100, 1),
        "concept_agree_count": agree_count,
        "concept_total": total,
        "concept_votes": votes,
    }


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
    if _is_forex_pair(symbol):
        # Forex benchmark: EUR/USD is the most liquid pair and a common
        # dollar-strength proxy; if the symbol IS EUR/USD, fall back to
        # GBP/USD instead so we're never comparing a pair to itself.
        benchmark_symbol = "GBP/USD" if symbol.upper() == "EUR/USD" else "EUR/USD"
    else:
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
# 21. CANDLESTICK PATTERN RECOGNITION  <-- DISPLAY ONLY
# Classic, human-style price-action reading (Doji, Hammer, Engulfing,
# Stars, Soldiers/Crows) applied to the actual OHLC candles - the way a
# discretionary chart-reading trader would eyeball the last few bars.
#
# IMPORTANT: this is a separate, purely informational panel. It is NOT
# wired into final_verdict, confidence_pct, or concept_accuracy_score -
# those still come ONLY from Hawkes + Bayesian (Conformal Prediction),
# exactly as before. This just shows the user what a classic chart
# reader would also notice on the same candles.
# ============================================================
def detect_candlestick_patterns(df, lookback=5):
    """Looks at the last `lookback` candles and returns a list of
    detected classic candlestick patterns, most-recent-first. Each entry:
    {name, type: BULLISH/BEARISH/NEUTRAL, candles_ago, description}."""
    recent = df.tail(max(lookback, 3)).reset_index(drop=True)
    n = len(recent)
    if n < 1:
        return []

    avg_range = float((recent["high"] - recent["low"]).mean()) or 1e-9
    patterns = []

    def body(c):
        return abs(c["close"] - c["open"])

    def upper_wick(c):
        return c["high"] - max(c["close"], c["open"])

    def lower_wick(c):
        return min(c["close"], c["open"]) - c["low"]

    def rng(c):
        return c["high"] - c["low"] or 1e-9

    def is_bullish(c):
        return c["close"] > c["open"]

    # --- single-candle patterns (checked on each of the last `lookback` bars) ---
    for i in range(n - 1, -1, -1):
        c = recent.iloc[i]
        candles_ago = (n - 1) - i
        b = body(c)
        uw = upper_wick(c)
        lw = lower_wick(c)
        r = rng(c)

        # Doji: body is tiny relative to the bar's own range.
        if b <= r * 0.1:
            patterns.append({
                "name": "Doji",
                "type": "NEUTRAL",
                "candles_ago": candles_ago,
                "description": "Open and close are nearly equal — indecision between buyers and sellers.",
            })
            continue

        # Hammer: small body near the top, long lower wick, little/no upper wick.
        if lw >= b * 2 and uw <= b * 0.5 and b <= r * 0.4:
            patterns.append({
                "name": "Hammer",
                "type": "BULLISH",
                "candles_ago": candles_ago,
                "description": "Long lower wick with a small body near the high — rejection of lower prices.",
            })
            continue

        # Shooting Star / Inverted Hammer: small body near the bottom, long upper wick.
        if uw >= b * 2 and lw <= b * 0.5 and b <= r * 0.4:
            patterns.append({
                "name": "Shooting Star",
                "type": "BEARISH",
                "candles_ago": candles_ago,
                "description": "Long upper wick with a small body near the low — rejection of higher prices.",
            })
            continue

        # Marubozu: body fills almost the entire range (little to no wicks) — strong conviction.
        if b >= r * 0.9:
            patterns.append({
                "name": "Bullish Marubozu" if is_bullish(c) else "Bearish Marubozu",
                "type": "BULLISH" if is_bullish(c) else "BEARISH",
                "candles_ago": candles_ago,
                "description": "Body fills almost the whole candle, barely any wicks — strong one-sided conviction.",
            })

    # --- two-candle patterns ---
    if n >= 2:
        prev, cur = recent.iloc[-2], recent.iloc[-1]
        prev_body_top = max(prev["open"], prev["close"])
        prev_body_bot = min(prev["open"], prev["close"])
        cur_body_top = max(cur["open"], cur["close"])
        cur_body_bot = min(cur["open"], cur["close"])

        # Bullish Engulfing: prior red candle's body fully engulfed by a green candle.
        if (not is_bullish(prev)) and is_bullish(cur) and cur_body_bot <= prev_body_bot and cur_body_top >= prev_body_top:
            patterns.append({
                "name": "Bullish Engulfing",
                "type": "BULLISH",
                "candles_ago": 0,
                "description": "Current green candle's body fully engulfs the prior red candle's body.",
            })

        # Bearish Engulfing: prior green candle's body fully engulfed by a red candle.
        if is_bullish(prev) and (not is_bullish(cur)) and cur_body_bot <= prev_body_bot and cur_body_top >= prev_body_top:
            patterns.append({
                "name": "Bearish Engulfing",
                "type": "BEARISH",
                "candles_ago": 0,
                "description": "Current red candle's body fully engulfs the prior green candle's body.",
            })

    # --- three-candle patterns ---
    if n >= 3:
        c1, c2, c3 = recent.iloc[-3], recent.iloc[-2], recent.iloc[-1]

        # Three White Soldiers: three consecutive strong green candles, each closing higher.
        if is_bullish(c1) and is_bullish(c2) and is_bullish(c3) \
                and c2["close"] > c1["close"] and c3["close"] > c2["close"] \
                and body(c1) >= rng(c1) * 0.5 and body(c2) >= rng(c2) * 0.5 and body(c3) >= rng(c3) * 0.5:
            patterns.append({
                "name": "Three White Soldiers",
                "type": "BULLISH",
                "candles_ago": 0,
                "description": "Three consecutive strong green candles, each closing higher than the last.",
            })

        # Three Black Crows: three consecutive strong red candles, each closing lower.
        if (not is_bullish(c1)) and (not is_bullish(c2)) and (not is_bullish(c3)) \
                and c2["close"] < c1["close"] and c3["close"] < c2["close"] \
                and body(c1) >= rng(c1) * 0.5 and body(c2) >= rng(c2) * 0.5 and body(c3) >= rng(c3) * 0.5:
            patterns.append({
                "name": "Three Black Crows",
                "type": "BEARISH",
                "candles_ago": 0,
                "description": "Three consecutive strong red candles, each closing lower than the last.",
            })

        # Morning Star: red candle, small-bodied middle candle (gap down), then a strong green candle closing well into the first candle's body.
        if (not is_bullish(c1)) and body(c2) <= rng(c2) * 0.35 \
                and is_bullish(c3) and c3["close"] >= (c1["open"] + c1["close"]) / 2:
            patterns.append({
                "name": "Morning Star",
                "type": "BULLISH",
                "candles_ago": 0,
                "description": "Red candle, a small-bodied pause, then a strong green candle reclaiming the range — bottoming reversal.",
            })

        # Evening Star: green candle, small-bodied middle candle, then a strong red candle closing well into the first candle's body.
        if is_bullish(c1) and body(c2) <= rng(c2) * 0.35 \
                and (not is_bullish(c3)) and c3["close"] <= (c1["open"] + c1["close"]) / 2:
            patterns.append({
                "name": "Evening Star",
                "type": "BEARISH",
                "candles_ago": 0,
                "description": "Green candle, a small-bodied pause, then a strong red candle giving back the range — topping reversal.",
            })

    # Most recent first, cap the list so the panel stays readable.
    patterns.sort(key=lambda p: p["candles_ago"])
    return patterns[:8]


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
# MASTER FUNCTION - sab 27 concepts combine karta hai
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

    try:
        candlestick_patterns = detect_candlestick_patterns(df)
    except Exception as e:
        candlestick_patterns = []

    # --- v8 Liquidity Scanner concepts (Ch.20-27) - now ALSO computed here
    # (not just in the separate /liquidity endpoint) so they can feed the
    # combined Accuracy Score below. Each is best-effort / try-except: if
    # a fetch fails (e.g. no perp market for this coin, no order book),
    # it degrades to a NEUTRAL/empty vote instead of breaking the signal.
    if include_orderbook:
        try:
            raw_ob_v8 = _clean_order_book(exchange.fetch_order_book(symbol, limit=25))
        except Exception:
            raw_ob_v8 = {"bids": [], "asks": []}
    else:
        raw_ob_v8 = {"bids": [], "asks": []}

    try:
        magnet_target_data = liquidity_magnet_and_target(current_price, raw_ob_v8, sweep_data)
    except Exception as e:
        magnet_target_data = {"magnet": None, "likely_target": None, "error": str(e)}

    try:
        spoofing_data = possible_spoofing_detector(symbol, raw_ob_v8)
    except Exception as e:
        spoofing_data = {"available": False, "spoof_detected": False, "error": str(e)}

    try:
        strength_data = market_strength_score(
            buying_pressure, selling_pressure, ofi_data.get("ofi_score"),
            depth_data.get("depth_slope"), vpin_data.get("vpin_score"))
    except Exception as e:
        strength_data = {"score": None, "label": None, "bias": None, "error": str(e)}

    try:
        funding_data = funding_open_interest(symbol) if include_orderbook else {"available": False}
    except Exception as e:
        funding_data = {"available": False, "error": str(e)}

    try:
        trap_squeeze_data = trap_and_squeeze_risk(sweep_data, ofi_data, depth_data, funding_data)
    except Exception as e:
        trap_squeeze_data = {"bull_trap": 0, "bear_trap": 0, "short_squeeze": 0, "long_squeeze": 0, "error": str(e)}

    try:
        target_zones_data = liquidity_target_zones(raw_ob_v8, current_price)
    except Exception as e:
        target_zones_data = []

    try:
        cvd_data = cvd_volume_delta(df)
    except Exception as e:
        cvd_data = {"cvd": None, "trend": None, "error": str(e)}

    try:
        crash_risk_data = market_crash_risk(jump_data, cusum_data, vpin_data, ofi_data, sweep_data, cvd_data)
    except Exception as e:
        crash_risk_data = {"score": None, "label": None, "factors": [], "error": str(e)}

    # "Accuracy Score" - kitne DIRECTIONAL concepts (jo actually LONG/SHORT
    # bol rahe hain, 27 mein se jitne bhi ho) final_verdict ki taraf ishara
    # kar rahe hain (display metric, verdict khud change nahi hota - dekho
    # concept_accuracy_score() ke comments).
    accuracy_data = concept_accuracy_score(
        final_verdict,
        buying_pressure=buying_pressure, selling_pressure=selling_pressure,
        bullish_pct=bullish_pct, bearish_pct=bearish_pct,
        ofi_data=ofi_data, regime_data=regime_data, jump_data=jump_data,
        meta_data=meta_data, divergence_data=divergence_data,
        depth_data=depth_data, vwap_data=vwap_data, wavelet_data=wavelet_data,
        cusum_data=cusum_data, sweep_data=sweep_data,
        magnet_target_data=magnet_target_data, spoofing_data=spoofing_data,
        strength_data=strength_data, trap_squeeze_data=trap_squeeze_data,
        target_zones_data=target_zones_data, funding_data=funding_data,
        cvd_data=cvd_data, crash_risk_data=crash_risk_data,
    )

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

        # display-only, human-style chart-reading (does NOT affect final_verdict)
        "candlestick_patterns": candlestick_patterns,

        # v8 Liquidity Scanner concepts (Ch.20-27, display-only, now also
        # returned here so /signal alone reflects everything the accuracy
        # score below is based on)
        "liquidity_magnet_target": magnet_target_data,
        "possible_spoofing": spoofing_data,
        "market_strength": strength_data,
        "trap_squeeze_risk": trap_squeeze_data,
        "liquidity_target_zones": target_zones_data,
        "funding_open_interest": funding_data,
        "cvd_volume_delta": cvd_data,
        "market_crash_risk": crash_risk_data,

        # Accuracy Score widget (agreement among whichever of Ch.01-27 are
        # currently directional, out of up to 27, with the verdict above)
        "concept_accuracy_pct": accuracy_data["concept_accuracy_pct"],
        "concept_agree_count": accuracy_data["concept_agree_count"],
        "concept_total": accuracy_data["concept_total"],
        "concept_votes": accuracy_data["concept_votes"],

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
    # Logged-in users ko trading interface, baaqi sabko public landing page
    if "user_id" not in session:
        return render_template("landing.html")
    return render_template(
        "design.html",
        user_email=session.get("email", ""),
        user_avatar=session.get("avatar_url"),
    )


@app.route("/demo-trading", methods=["GET"])
def demo_trading_page():
    if "user_id" not in session:
        return redirect(url_for("login_page"))
    return render_template("demo-trading.html")


@app.route("/reset-password", methods=["GET"])
def reset_password_page():
    return render_template("reset-password.html")


@app.route("/terms", methods=["GET"])
def terms_page():
    return render_template("terms.html")


@app.route("/privacy", methods=["GET"])
def privacy_page():
    return render_template("privacy.html")


@app.route("/signal", methods=["GET"])
def signal_endpoint():
    coin = request.args.get("coin", "BTC/USDT")
    timeframe = request.args.get("timeframe", "1h")
    orderbook = request.args.get("orderbook", "true").lower() != "false"

    # Forex pairs have no free order-book/funding/OI source (that's
    # crypto-exchange-only data) - so those channels are force-skipped
    # for forex and generate_signal() falls back to its price-action-only
    # channels (RSI/MACD, HMM regime, Hawkes+Bayesian verdict, etc.),
    # same as when a user manually turns "orderbook" off for a crypto pair.
    if _is_forex_pair(coin):
        orderbook = False

    try:
        df = get_candles(symbol=coin, timeframe=timeframe)
        result = generate_signal(df, symbol=coin, include_orderbook=orderbook)
        result["coin"] = coin
        result["timeframe"] = timeframe
        _cache_signal_result(coin, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/coins", methods=["GET"])
def available_coins():
    # Grouped by asset type for the picker's Forex/Crypto tabs. Old shape
    # (a flat list) is still available at ?flat=true for any caller that
    # relied on the previous response format.
    if request.args.get("flat", "").lower() == "true":
        return jsonify(AVAILABLE_COINS)
    return jsonify({"crypto": AVAILABLE_COINS, "forex": FOREX_PAIRS})


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
    is_forex = _is_forex_pair(coin)

    try:
        df = get_candles(symbol=coin, timeframe=timeframe, limit=120)
        current_price = float(df["close"].iloc[-1])
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"candle fetch failed: {e}"}), 400

    # Forex: no free order book source, so `ob` stays empty and every
    # order-book-dependent block below (each already in its own
    # try/except) degrades to its error/None shape instead of crashing.
    # The price-action parts (sweep detector, CVD, jump/CUSUM) still run
    # normally off `df`, so the main Liquidity Sweep Scanner visual and
    # a few of the extra cards keep working for forex too.
    if is_forex:
        ob = {"bids": [], "asks": [], "error": "order book not available for forex"}
    else:
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
    # Production mein debug hamesha OFF rehna chahiye - warna crash hone par
    # poora Python code/file paths users ko dikh jate hain (security risk).
    # Local testing ke liye FLASK_DEBUG=1 environment variable set kar sakte hain.
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
