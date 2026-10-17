"""
Strategy library - Signals FM
=============================

Thirty well-known, public trading strategies (moving-average crossovers, RSI, Bollinger Bands, breakouts,
candlestick patterns ...), written as plain rules and tested the same way on real candles. This file has no
web or database code: strategies.py runs it every day and the Strategies page shows the results.

How every strategy is tested (the same for all of them, shown on the page):
  * Entry at the close of the candle where the rule fires. Only candles that had closed are used.
  * Stop = a multiple of ATR(14) from the entry; that distance is 1R. Inside every later candle the stop is
    checked first; a candle that opens beyond the stop exits at that (worse) open.
  * Exit at the close of the candle where the strategy's exit rule fires, or at its time limit.
    Reversal strategies (crossovers, flips) close and turn around on the opposite signal.
  * One position per symbol at a time.
  * Results in R (1R = entry-to-stop distance) after costs: crypto 0.12% of the position round trip,
    forex the typical spread of the pair.

None of these strategies is a recommendation, and none of them is the site's own signal model.
"""

import math

import numpy as np
import pandas as pd

import markets

# the coins and forex pairs the site runs the library on (strategies.py; STRATEGIES_COINS / STRATEGIES_FOREX override)
LIB_COINS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT", "TRX/USDT", "DOGE/USDT", "DOT/USDT",
             "XLM/USDT", "LINK/USDT", "ADA/USDT", "BCH/USDT", "LTC/USDT", "SUI/USDT", "FIL/USDT", "AVAX/USDT",
             "NEAR/USDT", "UNI/USDT", "ATOM/USDT", "AAVE/USDT"]
LIB_FOREX = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "XAU/USD"]
PREVIEW_SYMBOL = "BTC/USDT"
HEADLINE_KEY = "crypto_4h"      # the result shown on the cards: fixed, so no result is picked afterwards
WARMUP = 210            # candles before the first possible entry (the slowest average is 200)
PREVIEW_BARS = 140      # candles in the small chart of each strategy
CURVE_POINTS = 120      # points kept from each equity curve


# ===========================================================================
# indicators (numpy in, numpy out; NaN until enough candles)
# ===========================================================================
def _s(x):
    return pd.Series(np.asarray(x, dtype=float))


def ema(x, n):
    return _s(x).ewm(span=n, adjust=False, min_periods=n).mean().to_numpy()


def sma(x, n):
    return _s(x).rolling(n, min_periods=n).mean().to_numpy()


def rma(x, n):
    """Wilder's moving average (RSI, ATR, ADX)."""
    return _s(x).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()


def stdev(x, n):
    return _s(x).rolling(n, min_periods=n).std(ddof=0).to_numpy()


def highest(x, n):
    return _s(x).rolling(n, min_periods=n).max().to_numpy()


def lowest(x, n):
    return _s(x).rolling(n, min_periods=n).min().to_numpy()


def shift(x, k=1):
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[:len(x) - k]
    return out


def true_range(h, l, c):
    pc = shift(c)
    tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))
    tr[0] = h[0] - l[0]
    return tr


def atr(h, l, c, n=14):
    return rma(true_range(h, l, c), n)


def rsi(c, n=14):
    d = np.diff(np.asarray(c, dtype=float), prepend=np.nan)
    gain = rma(np.where(d > 0, d, 0.0), n)
    loss = rma(np.where(d < 0, -d, 0.0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss
        out = 100 - 100 / (1 + rs)
    out = np.where((loss == 0) & (gain > 0), 100.0, out)
    out[:n] = np.nan
    return out


def macd(c, fast=12, slow=26, sig=9):
    m = ema(c, fast) - ema(c, slow)
    s = _s(m).ewm(span=sig, adjust=False, min_periods=sig).mean().to_numpy()
    return m, s, m - s


def bollinger(c, n=20, k=2.0):
    mid = sma(c, n)
    sd = stdev(c, n)
    up, lo = mid + k * sd, mid - k * sd
    with np.errstate(divide="ignore", invalid="ignore"):
        width = (up - lo) / mid
    return mid, up, lo, width


def stochastic(h, l, c, n=14, k=3, d=3):
    hh, ll = highest(h, n), lowest(l, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = 100 * (c - ll) / (hh - ll)
    kk = sma(raw, k)
    return kk, sma(kk, d)


def cci(h, l, c, n=20):
    tp = (h + l + c) / 3
    ma = sma(tp, n)
    md = np.full(len(tp), np.nan)
    if len(tp) >= n:
        w = np.lib.stride_tricks.sliding_window_view(tp, n)
        md[n - 1:] = np.mean(np.abs(w - w.mean(axis=1, keepdims=True)), axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (tp - ma) / (0.015 * md)


def williams_r(h, l, c, n=14):
    hh, ll = highest(h, n), lowest(l, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return -100 * (hh - c) / (hh - ll)


def adx(h, l, c, n=14):
    up = h - shift(h)
    dn = shift(l) - l
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = rma(true_range(h, l, c), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * rma(pdm, n) / a
        mdi = 100 * rma(mdm, n) / a
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    return rma(np.nan_to_num(dx, nan=0.0), n), pdi, mdi


def supertrend(h, l, c, n=10, m=3.0):
    """Returns (line, direction): direction +1 = up trend (line below price), -1 = down trend."""
    a = atr(h, l, c, n)
    hl2 = (h + l) / 2
    ub, lb = hl2 + m * a, hl2 - m * a
    k = len(c)
    fub, flb = ub.copy(), lb.copy()
    line = np.full(k, np.nan)
    dr = np.zeros(k)
    for i in range(1, k):
        if np.isnan(a[i]):
            continue
        if not np.isnan(fub[i - 1]) and not (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]):
            fub[i] = fub[i - 1]
        if not np.isnan(flb[i - 1]) and not (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]):
            flb[i] = flb[i - 1]
        prev = dr[i - 1] if dr[i - 1] != 0 else 1
        if prev > 0:
            dr[i] = -1 if c[i] < flb[i] else 1
        else:
            dr[i] = 1 if c[i] > fub[i] else -1
        line[i] = flb[i] if dr[i] > 0 else fub[i]
    return line, dr


def psar(h, l, step=0.02, mx=0.2):
    """Parabolic SAR. Returns (sar, direction)."""
    k = len(h)
    sar = np.full(k, np.nan)
    dr = np.zeros(k)
    if k < 3:
        return sar, dr
    up = True
    af = step
    ep = h[0]
    s = l[0]
    for i in range(1, k):
        s = s + af * (ep - s)
        if up:
            s = min(s, l[i - 1], l[i - 2] if i >= 2 else l[i - 1])
            if l[i] < s:
                up, s, ep, af = False, ep, l[i], step
            elif h[i] > ep:
                ep, af = h[i], min(af + step, mx)
        else:
            s = max(s, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > s:
                up, s, ep, af = True, ep, h[i], step
            elif l[i] < ep:
                ep, af = l[i], min(af + step, mx)
        sar[i] = s
        dr[i] = 1 if up else -1
    return sar, dr


def keltner(h, l, c, n=20, m=2.0, atr_n=10):
    mid = ema(c, n)
    a = atr(h, l, c, atr_n)
    return mid, mid + m * a, mid - m * a


def ichimoku(h, l):
    tenkan = (highest(h, 9) + lowest(l, 9)) / 2
    kijun = (highest(h, 26) + lowest(l, 26)) / 2
    span_a = shift((tenkan + kijun) / 2, 26)          # drawn 26 candles ahead = known 26 candles ago
    span_b = shift((highest(h, 52) + lowest(l, 52)) / 2, 26)
    return tenkan, kijun, span_a, span_b


def heikin_ashi(o, h, l, c):
    hc = (o + h + l + c) / 4
    ho = np.empty(len(c))
    ho[0] = (o[0] + c[0]) / 2
    for i in range(1, len(c)):
        ho[i] = (ho[i - 1] + hc[i - 1]) / 2
    hh = np.fmax(h, np.fmax(ho, hc))
    hl = np.fmin(l, np.fmin(ho, hc))
    return ho, hh, hl, hc


def wma(x, n):
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        w = np.arange(1, n + 1, dtype=float)
        out[n - 1:] = np.lib.stride_tricks.sliding_window_view(x, n) @ w / w.sum()
    return out


def hma(x, n):
    return wma(2 * wma(x, n // 2) - wma(x, n), int(math.sqrt(n)))


def vwap_daily(ts, h, l, c, v):
    """VWAP anchored at 00:00 UTC every day, and its volume-weighted standard deviation."""
    day = pd.to_datetime(pd.Series(ts)).dt.floor("D").to_numpy()
    tp = (h + l + c) / 3
    v = np.where(np.isfinite(v) & (v > 0), v, 0.0)
    df = pd.DataFrame({"d": day, "v": v, "pv": tp * v, "p2v": tp * tp * v})
    g = df.groupby("d", sort=False)
    cv = g["v"].cumsum().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        vw = g["pv"].cumsum().to_numpy() / cv
        var = g["p2v"].cumsum().to_numpy() / cv - vw * vw
    return vw, np.sqrt(np.clip(var, 0, None))


def roc(c, n=12):
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100 * (c / shift(c, n) - 1)


def zscore(c, n=20):
    with np.errstate(divide="ignore", invalid="ignore"):
        return (c - sma(c, n)) / stdev(c, n)


def cross_up(a, b):
    b = np.broadcast_to(np.asarray(b, dtype=float), np.shape(a))
    return (a > b) & (shift(a) <= shift(b))


def cross_dn(a, b):
    b = np.broadcast_to(np.asarray(b, dtype=float), np.shape(a))
    return (a < b) & (shift(a) >= shift(b))


def _b(x):
    return np.asarray(np.nan_to_num(np.asarray(x, dtype=float), nan=0.0) > 0.5) if np.asarray(x).dtype != bool \
        else np.asarray(x, dtype=bool)


def _cols(d):
    return (d["open"].to_numpy(float), d["high"].to_numpy(float), d["low"].to_numpy(float), d["close"].to_numpy(float),
            d["volume"].to_numpy(float) if "volume" in d else np.zeros(len(d)))


def _sig(le, se, lx, sx, overlays=None, osc=None):
    return {"le": _b(le), "se": _b(se), "lx": _b(lx), "sx": _b(sx), "overlays": overlays or {}, "osc": osc}


# ===========================================================================
# the strategies
# ===========================================================================
def s_ema_cross(d):
    o, h, l, c, v = _cols(d)
    f, s = ema(c, 9), ema(c, 21)
    up, dn = cross_up(f, s), cross_dn(f, s)
    return _sig(up, dn, dn, up, {"EMA 9": f, "EMA 21": s})


def s_golden_cross(d):
    o, h, l, c, v = _cols(d)
    f, s = sma(c, 50), sma(c, 200)
    up, dn = cross_up(f, s), cross_dn(f, s)
    return _sig(up, dn, dn, up, {"SMA 50": f, "SMA 200": s})


def s_macd(d):
    o, h, l, c, v = _cols(d)
    m, s, _ = macd(c)
    up, dn = cross_up(m, s), cross_dn(m, s)
    return _sig(up, dn, dn, up, osc={"lines": {"MACD": m, "Signal": s}, "levels": [0]})


def s_supertrend(d):
    o, h, l, c, v = _cols(d)
    line, dr = supertrend(h, l, c, 10, 3.0)
    up = (dr > 0) & (shift(dr) < 0)
    dn = (dr < 0) & (shift(dr) > 0)
    return _sig(up, dn, dn, up, {"Supertrend": line})


def s_turtle(d):
    o, h, l, c, v = _cols(d)
    hi20, lo20 = shift(highest(h, 20)), shift(lowest(l, 20))
    hi10, lo10 = shift(highest(h, 10)), shift(lowest(l, 10))
    return _sig(c > hi20, c < lo20, c < lo10, c > hi10, {"20-candle high": hi20, "20-candle low": lo20})


def s_ichimoku(d):
    o, h, l, c, v = _cols(d)
    tk, kj, sa, sb = ichimoku(h, l)
    top, bot = np.fmax(sa, sb), np.fmin(sa, sb)
    le = cross_up(tk, kj) & (c > top)
    se = cross_dn(tk, kj) & (c < bot)
    return _sig(le, se, c < kj, c > kj, {"Tenkan": tk, "Kijun": kj, "Cloud A": sa, "Cloud B": sb})


def s_psar(d):
    o, h, l, c, v = _cols(d)
    sar, dr = psar(h, l)
    up = (dr > 0) & (shift(dr) < 0)
    dn = (dr < 0) & (shift(dr) > 0)
    return _sig(up, dn, dn, up, {"SAR": sar})


def s_adx_di(d):
    o, h, l, c, v = _cols(d)
    a, pdi, mdi = adx(h, l, c)
    up, dn = cross_up(pdi, mdi), cross_dn(pdi, mdi)
    return _sig(up & (a > 25), dn & (a > 25), dn, up, osc={"lines": {"+DI": pdi, "-DI": mdi, "ADX": a}, "levels": [25]})


def s_ema_ribbon(d):
    o, h, l, c, v = _cols(d)
    e8, e21, e55 = ema(c, 8), ema(c, 21), ema(c, 55)
    up_tr = (e8 > e21) & (e21 > e55)
    dn_tr = (e8 < e21) & (e21 < e55)
    le = up_tr & (l <= e21) & (c > e21) & (c > o)
    se = dn_tr & (h >= e21) & (c < e21) & (c < o)
    return _sig(le, se, c < e55, c > e55, {"EMA 8": e8, "EMA 21": e21, "EMA 55": e55})


def s_heikin_ashi(d):
    o, h, l, c, v = _cols(d)
    ho, hh, hl, hc = heikin_ashi(o, h, l, c)
    green, red = hc > ho, hc < ho
    body_lo, body_hi = np.fmin(ho, hc), np.fmax(ho, hc)
    strong_up = green & (np.abs(hl - body_lo) <= 1e-12 * np.abs(body_lo) + 1e-12)
    strong_dn = red & (np.abs(hh - body_hi) <= 1e-12 * np.abs(body_hi) + 1e-12)
    le = strong_up & (shift(red.astype(float)) > 0.5)
    se = strong_dn & (shift(green.astype(float)) > 0.5)
    return _sig(le, se, red, green, {"Heikin-Ashi close": hc})


def s_hull(d):
    o, h, l, c, v = _cols(d)
    hm = hma(c, 55)
    up = (hm > shift(hm)) & (shift(hm) <= shift(hm, 2))
    dn = (hm < shift(hm)) & (shift(hm) >= shift(hm, 2))
    return _sig(up, dn, dn, up, {"Hull MA 55": hm})


def s_rsi(d):
    o, h, l, c, v = _cols(d)
    r = rsi(c, 14)
    return _sig(cross_up(r, 30), cross_dn(r, 70), r > 50, r < 50, osc={"lines": {"RSI 14": r}, "levels": [30, 50, 70]})


def s_rsi2(d):
    o, h, l, c, v = _cols(d)
    r2, s200, s5 = rsi(c, 2), sma(c, 200), sma(c, 5)
    return _sig((c > s200) & (r2 < 10), (c < s200) & (r2 > 90), c > s5, c < s5, {"SMA 200": s200, "SMA 5": s5},
                {"lines": {"RSI 2": r2}, "levels": [10, 90]})


def s_bollinger_rev(d):
    o, h, l, c, v = _cols(d)
    mid, up, lo, _ = bollinger(c, 20, 2.0)
    le = (shift(c) < shift(lo)) & (c > lo)
    se = (shift(c) > shift(up)) & (c < up)
    return _sig(le, se, c >= mid, c <= mid, {"Upper band": up, "Middle": mid, "Lower band": lo})


def s_stoch(d):
    o, h, l, c, v = _cols(d)
    k, dd = stochastic(h, l, c)
    return _sig(cross_up(k, dd) & (k < 20), cross_dn(k, dd) & (k > 80), k > 80, k < 20,
                osc={"lines": {"%K": k, "%D": dd}, "levels": [20, 80]})


def s_williams(d):
    o, h, l, c, v = _cols(d)
    w = williams_r(h, l, c)
    return _sig(cross_up(w, -80), cross_dn(w, -20), w > -20, w < -80, osc={"lines": {"%R": w}, "levels": [-80, -20]})


def s_cci(d):
    o, h, l, c, v = _cols(d)
    x = cci(h, l, c)
    return _sig(cross_up(x, -100), cross_dn(x, 100), x > 100, x < -100, osc={"lines": {"CCI 20": x}, "levels": [-100, 100]})


def s_vwap(d):
    o, h, l, c, v = _cols(d)
    vw, sd = vwap_daily(d["timestamp"].to_numpy(), h, l, c, v)
    lo, up = vw - 2 * sd, vw + 2 * sd
    le = (shift(c) < shift(lo)) & (c > shift(c)) & (c < vw)
    se = (shift(c) > shift(up)) & (c < shift(c)) & (c > vw)
    return _sig(le, se, c >= vw, c <= vw, {"VWAP": vw, "VWAP +2 sd": up, "VWAP -2 sd": lo})


def s_awesome(d):
    o, h, l, c, v = _cols(d)
    mp = (h + l) / 2
    ao = sma(mp, 5) - sma(mp, 34)
    up, dn = cross_up(ao, 0), cross_dn(ao, 0)
    return _sig(up, dn, dn, up, osc={"lines": {"Awesome Oscillator": ao}, "levels": [0]})


def s_squeeze(d):
    o, h, l, c, v = _cols(d)
    mid, up, lo, w = bollinger(c, 20, 2.0)
    tight = w <= lowest(w, 120) * 1.05
    recent = _s(tight.astype(float)).rolling(6, min_periods=1).max().to_numpy() > 0
    return _sig(recent & cross_up(c, up), recent & cross_dn(c, lo), c < mid, c > mid,
                {"Upper band": up, "Middle": mid, "Lower band": lo})


def s_keltner(d):
    o, h, l, c, v = _cols(d)
    mid, up, lo = keltner(h, l, c)
    return _sig(cross_up(c, up), cross_dn(c, lo), c < mid, c > mid, {"Upper": up, "EMA 20": mid, "Lower": lo})


def s_day_range(d):
    """Range of the first 4 hours of each UTC day; trade the first close outside it; flat at the end of the day."""
    o, h, l, c, v = _cols(d)
    ts = pd.to_datetime(d["timestamp"])
    tf_h = max(1, int(round((ts.diff().dt.total_seconds().median() or 3600) / 3600)))
    day = ts.dt.floor("D")
    hour = ts.dt.hour.to_numpy()
    first = hour < 4
    df = pd.DataFrame({"d": day, "h": np.where(first, h, np.nan), "l": np.where(first, l, np.nan)})
    rh = df.groupby("d")["h"].transform(lambda x: x.ffill().cummax()).to_numpy()
    rl = df.groupby("d")["l"].transform(lambda x: x.ffill().cummin()).to_numpy()
    after = hour >= 4                                # candles that start after the range is complete
    up = after & (c > rh)
    dn = after & (c < rl)
    seen_up = pd.Series(up).groupby(day.to_numpy()).cumsum().to_numpy()
    seen_dn = pd.Series(dn).groupby(day.to_numpy()).cumsum().to_numpy()
    le = up & (seen_up == 1)
    se = dn & (seen_dn == 1)
    end_of_day = (hour + tf_h) >= 24
    return _sig(le, se, end_of_day, end_of_day, {"Range high": rh, "Range low": rl})


def s_nr7(d):
    o, h, l, c, v = _cols(d)
    rng = h - l
    nr7 = rng <= lowest(rng, 7)
    sh = _s(np.where(nr7, h, np.nan)).ffill(limit=2).to_numpy()
    sl = _s(np.where(nr7, l, np.nan)).ffill(limit=2).to_numpy()
    ph, pl = shift(sh), shift(sl)
    return _sig(c > ph, c < pl, c < pl, c > ph, {"Setup high": ph, "Setup low": pl})


def s_chandelier(d):
    o, h, l, c, v = _cols(d)
    a = atr(h, l, c, 22)
    long_line = highest(h, 22) - 3 * a
    short_line = lowest(l, 22) + 3 * a
    k = len(c)
    dr = np.zeros(k)
    for i in range(1, k):
        prev = dr[i - 1] if dr[i - 1] != 0 else 0
        if np.isnan(long_line[i]) or np.isnan(short_line[i]):
            continue
        if prev >= 0 and c[i] < long_line[i]:
            dr[i] = -1
        elif prev <= 0 and c[i] > short_line[i]:
            dr[i] = 1
        else:
            dr[i] = prev if prev != 0 else (1 if c[i] > long_line[i] else -1)
    up = (dr > 0) & (shift(dr) <= 0)
    dn = (dr < 0) & (shift(dr) >= 0)
    line = np.where(dr > 0, long_line, short_line)
    return _sig(up, dn, dn, up, {"Chandelier stop": line})


def s_inside_bar(d):
    o, h, l, c, v = _cols(d)
    inside = (h < shift(h)) & (l > shift(l))
    ib = shift(inside.astype(float)) > 0.5
    e50, e20 = ema(c, 50), ema(c, 20)
    mh, ml = shift(h, 2), shift(l, 2)                 # the mother candle
    return _sig(ib & (c > mh) & (c > e50), ib & (c < ml) & (c < e50), c < e20, c > e20,
                {"EMA 20": e20, "EMA 50": e50})


def s_roc(d):
    o, h, l, c, v = _cols(d)
    r, s100 = roc(c, 12), sma(c, 100)
    up, dn = cross_up(r, 0), cross_dn(r, 0)
    return _sig(up & (c > s100), dn & (c < s100), dn, up, {"SMA 100": s100}, {"lines": {"ROC 12": r}, "levels": [0]})


def s_rsi50(d):
    o, h, l, c, v = _cols(d)
    r, e50, e200 = rsi(c, 14), ema(c, 50), ema(c, 200)
    return _sig(cross_up(r, 50) & (e50 > e200), cross_dn(r, 50) & (e50 < e200), r < 40, r > 60,
                {"EMA 50": e50, "EMA 200": e200}, {"lines": {"RSI 14": r}, "levels": [40, 50, 60]})


def s_engulfing(d):
    o, h, l, c, v = _cols(d)
    po, pc = shift(o), shift(c)
    bull = (pc < po) & (c > o) & (c >= po) & (o <= pc)
    bear = (pc > po) & (c < o) & (c <= po) & (o >= pc)
    s20 = sma(c, 20)
    at_low = np.fmin(l, shift(l)) <= lowest(l, 20)
    at_high = np.fmax(h, shift(h)) >= highest(h, 20)
    return _sig(bull & at_low, bear & at_high, c >= s20, c <= s20, {"SMA 20": s20})


def s_pin_bar(d):
    o, h, l, c, v = _cols(d)
    body = np.abs(c - o)
    rng = h - l
    lower = np.fmin(o, c) - l
    upper = h - np.fmax(o, c)
    hammer = (lower >= 2 * body) & (lower >= 0.6 * rng) & (rng > 0)
    star = (upper >= 2 * body) & (upper >= 0.6 * rng) & (rng > 0)
    s20 = sma(c, 20)
    return _sig(hammer & (l <= lowest(l, 20)), star & (h >= highest(h, 20)), c >= s20, c <= s20, {"SMA 20": s20})


def s_volume_breakout(d):
    o, h, l, c, v = _cols(d)
    hi20, lo20 = shift(highest(h, 20)), shift(lowest(l, 20))
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = v / shift(sma(v, 20))
    e20 = ema(c, 20)
    return _sig((c > hi20) & (vr > 2), (c < lo20) & (vr > 2), c < e20, c > e20,
                {"20-candle high": hi20, "20-candle low": lo20}, {"lines": {"Volume / average": vr}, "levels": [2]})


TREND, MEANREV, BREAKOUT, MOMENTUM, PATTERN = "Trend following", "Mean reversion", "Breakout", "Momentum", "Price action"
CATEGORIES = [TREND, MEANREV, BREAKOUT, MOMENTUM, PATTERN]

# slug, name, category, function, stop (x ATR), time limit (candles), reverse on the opposite signal,
# one-line summary, description, rules (long, short, exit)
LIBRARY = [
    dict(slug="ema-crossover-9-21", name="EMA crossover 9 / 21", category=TREND, fn=s_ema_cross, stop_atr=2.0, max_bars=200,
         reverse=True,
         summary="Buy when the 9-period EMA crosses above the 21-period EMA, sell short on the opposite cross.",
         description="One of the most used trend-following setups. Two exponential moving averages of the close: when the "
                     "fast one crosses above the slow one the trend is taken to have turned up, and the other way round. "
                     "The position is always turned around on the next opposite cross.",
         rules=["Long: the 9 EMA crosses above the 21 EMA.", "Short: the 9 EMA crosses below the 21 EMA.",
                "Exit: the opposite cross (the position turns around)."]),
    dict(slug="golden-cross-50-200", name="Golden cross and death cross (50 / 200)", category=TREND, fn=s_golden_cross,
         stop_atr=3.0, max_bars=600, reverse=True,
         summary="Long on the golden cross of the 50 and 200 SMA, short on the death cross.",
         description="The classic long-term trend signal watched by many investors: the 50-period simple moving average "
                     "crossing the 200-period one. It reacts slowly, so it gives few trades and stays in a trend for long.",
         rules=["Long: the 50 SMA crosses above the 200 SMA (golden cross).",
                "Short: the 50 SMA crosses below the 200 SMA (death cross).", "Exit: the opposite cross."]),
    dict(slug="macd-signal-crossover", name="MACD signal-line crossover", category=MOMENTUM, fn=s_macd, stop_atr=2.0,
         max_bars=150, reverse=True,
         summary="Long when the MACD line crosses above its signal line, short when it crosses below.",
         description="MACD (12, 26, 9) is the difference between a fast and a slow EMA; its signal line is a 9-period EMA "
                     "of it. Crossings of the two lines are read as changes in momentum.",
         rules=["Long: MACD crosses above the signal line.", "Short: MACD crosses below the signal line.",
                "Exit: the opposite cross."]),
    dict(slug="supertrend-10-3", name="Supertrend (10, 3)", category=TREND, fn=s_supertrend, stop_atr=2.5, max_bars=300,
         reverse=True,
         summary="Follow the Supertrend line: long when price closes above it, short when it closes below.",
         description="Supertrend draws a line 3 ATR(10) away from the middle of the candle and flips sides when the close "
                     "breaks through it. It is a popular trailing trend filter.",
         rules=["Long: Supertrend flips to an up trend.", "Short: Supertrend flips to a down trend.",
                "Exit: the next flip."]),
    dict(slug="donchian-turtle-20-10", name="Turtle breakout (Donchian 20 / 10)", category=BREAKOUT, fn=s_turtle,
         stop_atr=2.0, max_bars=300, reverse=False,
         summary="Buy a close above the 20-candle high, exit on a close below the 10-candle low (and the mirror for shorts).",
         description="The breakout rule made famous by the Turtle traders: enter when price makes a new 20-candle extreme, "
                     "leave when it makes a 10-candle extreme the other way, with a 2 ATR (2N) stop.",
         rules=["Long: the close is above the highest high of the previous 20 candles.",
                "Short: the close is below the lowest low of the previous 20 candles.",
                "Exit long: close below the 10-candle low. Exit short: close above the 10-candle high."]),
    dict(slug="ichimoku-tk-cross", name="Ichimoku Tenkan / Kijun cross", category=TREND, fn=s_ichimoku, stop_atr=2.5,
         max_bars=300, reverse=False,
         summary="Tenkan crossing Kijun on the right side of the cloud; exit when price closes back through the Kijun.",
         description="Ichimoku Kinko Hyo (9, 26, 52). A cross of the conversion line (Tenkan) and the base line (Kijun) "
                     "counts only when price is already outside the cloud in the same direction.",
         rules=["Long: Tenkan crosses above Kijun and the close is above the cloud.",
                "Short: Tenkan crosses below Kijun and the close is below the cloud.",
                "Exit long: close below the Kijun. Exit short: close above the Kijun."]),
    dict(slug="parabolic-sar", name="Parabolic SAR flip", category=TREND, fn=s_psar, stop_atr=2.5, max_bars=300,
         reverse=True,
         summary="Stay in the direction of the Parabolic SAR and turn around when the dots flip.",
         description="Welles Wilder's stop-and-reverse indicator (step 0.02, maximum 0.2) trails price and flips to the "
                     "other side when price touches it.",
         rules=["Long: the SAR flips below price.", "Short: the SAR flips above price.", "Exit: the next flip."]),
    dict(slug="adx-di-crossover", name="ADX trend with DI crossover", category=TREND, fn=s_adx_di, stop_atr=2.0,
         max_bars=200, reverse=False,
         summary="+DI crossing -DI while ADX is above 25 (a trending market); exit on the opposite DI cross.",
         description="Wilder's Directional Movement System: the DI lines show which side is stronger and ADX shows "
                     "whether there is a trend at all. Crosses are taken only when ADX(14) is above 25.",
         rules=["Long: +DI crosses above -DI with ADX above 25.", "Short: -DI crosses above +DI with ADX above 25.",
                "Exit: the opposite DI cross."]),
    dict(slug="ema-ribbon-pullback", name="EMA ribbon pullback (8 / 21 / 55)", category=TREND, fn=s_ema_ribbon,
         stop_atr=2.0, max_bars=150, reverse=False,
         summary="In a stacked EMA trend, buy the pullback that touches the 21 EMA and closes back above it.",
         description="When the 8, 21 and 55 EMAs are stacked in order the trend is clear; the entry waits for a dip to the "
                     "21 EMA that is rejected by a strong close.",
         rules=["Long: 8 EMA > 21 EMA > 55 EMA, the candle touches the 21 EMA and closes up, above it.",
                "Short: the mirror image in a down trend.",
                "Exit long: close below the 55 EMA. Exit short: close above the 55 EMA."]),
    dict(slug="heikin-ashi-trend", name="Heikin-Ashi trend change", category=TREND, fn=s_heikin_ashi, stop_atr=2.0,
         max_bars=150, reverse=False,
         summary="Enter on the first strong Heikin-Ashi candle after a colour change; exit on the first opposite candle.",
         description="Heikin-Ashi candles average price to show the trend more smoothly. A green Heikin-Ashi candle with no "
                     "lower shadow right after a red one is read as a new up move.",
         rules=["Long: a green Heikin-Ashi candle without a lower shadow follows a red one.",
                "Short: a red Heikin-Ashi candle without an upper shadow follows a green one.",
                "Exit long: the first red Heikin-Ashi candle. Exit short: the first green one."]),
    dict(slug="hull-ma-turn", name="Hull moving average turn (55)", category=TREND, fn=s_hull, stop_atr=2.0, max_bars=200,
         reverse=True,
         summary="Long when the Hull MA turns up, short when it turns down.",
         description="Alan Hull's moving average is built to react fast with little lag; its change of slope is used as "
                     "the signal.",
         rules=["Long: the Hull MA(55) rises after falling.", "Short: the Hull MA(55) falls after rising.",
                "Exit: the next turn."]),
    dict(slug="rsi-30-70", name="RSI 30 / 70 reversal", category=MEANREV, fn=s_rsi, stop_atr=2.0, max_bars=48,
         reverse=False,
         summary="Buy when RSI(14) climbs back above 30, sell short when it falls back below 70; exit at 50.",
         description="The textbook use of the Relative Strength Index: oversold below 30, overbought above 70. The trade "
                     "is taken when RSI leaves the extreme zone and closed when it is back at the middle.",
         rules=["Long: RSI(14) crosses up through 30.", "Short: RSI(14) crosses down through 70.",
                "Exit long: RSI above 50. Exit short: RSI below 50."]),
    dict(slug="rsi-2-connors", name="RSI(2) pullback (Connors)", category=MEANREV, fn=s_rsi2, stop_atr=3.0, max_bars=20,
         reverse=False,
         summary="In an up trend (above the 200 SMA) buy when RSI(2) is below 10; sell when the close is above the 5 SMA.",
         description="Larry Connors' short-term pullback rule: a very short RSI finds deep, quick dips inside a long-term "
                     "trend. The mirror image is used for shorts below the 200 SMA.",
         rules=["Long: close above the 200 SMA and RSI(2) below 10.", "Short: close below the 200 SMA and RSI(2) above 90.",
                "Exit long: close above the 5 SMA. Exit short: close below the 5 SMA."]),
    dict(slug="bollinger-band-reversion", name="Bollinger Band reversion", category=MEANREV, fn=s_bollinger_rev,
         stop_atr=2.0, max_bars=48, reverse=False,
         summary="Buy when price closes back inside the lower Bollinger Band; take profit at the middle band.",
         description="Bollinger Bands (20, 2) sit two standard deviations around a 20-period average. A close back inside "
                     "after a close outside is read as an overstretched move snapping back.",
         rules=["Long: the previous close was below the lower band and this close is back above it.",
                "Short: the previous close was above the upper band and this close is back below it.",
                "Exit: the close reaches the middle band."]),
    dict(slug="stochastic-20-80", name="Stochastic 20 / 80 crossover", category=MEANREV, fn=s_stoch, stop_atr=2.0,
         max_bars=48, reverse=False,
         summary="%K crossing above %D below 20 (oversold) to buy, crossing below %D above 80 to sell short.",
         description="The Stochastic oscillator (14, 3, 3) compares the close with the recent range. Crosses of its two "
                     "lines inside the extreme zones are the classic signal.",
         rules=["Long: %K crosses above %D while %K is below 20.", "Short: %K crosses below %D while %K is above 80.",
                "Exit long: %K above 80. Exit short: %K below 20."]),
    dict(slug="williams-r-reversal", name="Williams %R reversal", category=MEANREV, fn=s_williams, stop_atr=2.0,
         max_bars=48, reverse=False,
         summary="Buy when Williams %R(14) leaves the oversold zone (-80), sell short when it leaves overbought (-20).",
         description="Larry Williams' %R shows where the close sits inside the last 14 candles' range, from 0 (top) to "
                     "-100 (bottom).",
         rules=["Long: %R crosses up through -80.", "Short: %R crosses down through -20.",
                "Exit long: %R above -20. Exit short: %R below -80."]),
    dict(slug="cci-100-reversal", name="CCI ±100 reversal", category=MEANREV, fn=s_cci, stop_atr=2.0, max_bars=48,
         reverse=False,
         summary="Buy when CCI(20) climbs back above -100, sell short when it falls back below +100.",
         description="Donald Lambert's Commodity Channel Index measures how far the typical price is from its average, in "
                     "units of its mean deviation. Readings beyond ±100 are treated as stretched.",
         rules=["Long: CCI crosses up through -100.", "Short: CCI crosses down through +100.",
                "Exit long: CCI above +100. Exit short: CCI below -100."]),
    dict(slug="vwap-reversion", name="VWAP band reversion", category=MEANREV, fn=s_vwap, stop_atr=2.0, max_bars=24,
         reverse=False,
         summary="Fade a move two standard deviations away from the daily VWAP once price turns back; exit at the VWAP.",
         description="The volume-weighted average price, restarted every day at 00:00 UTC, is the average price paid that "
                     "day. Intraday traders fade stretches far from it.",
         rules=["Long: the previous close was below VWAP - 2 sd and this candle closes higher, still below the VWAP.",
                "Short: the mirror image above VWAP + 2 sd.", "Exit: the close reaches the VWAP."]),
    dict(slug="awesome-oscillator-zero-cross", name="Awesome Oscillator zero-line cross", category=MOMENTUM,
         fn=s_awesome, stop_atr=2.0, max_bars=150, reverse=True,
         summary="Long when Bill Williams' Awesome Oscillator crosses above zero, short when it crosses below.",
         description="The Awesome Oscillator is the 5-period average of the candle midpoint minus the 34-period one. A move "
                     "through zero shows short-term momentum overtaking the longer-term trend.",
         rules=["Long: the Awesome Oscillator crosses above 0.", "Short: it crosses below 0.",
                "Exit: the opposite cross (the position turns around)."]),
    dict(slug="bollinger-squeeze-breakout", name="Bollinger squeeze breakout", category=BREAKOUT, fn=s_squeeze,
         stop_atr=2.0, max_bars=100, reverse=False,
         summary="After the bands are at their tightest in 120 candles, trade the first close outside them.",
         description="John Bollinger's squeeze: very narrow bands mean low volatility, which is often followed by a strong "
                     "move. The breakout direction decides the side.",
         rules=["Setup: Bollinger Band width within 5% of its 120-candle low in the last 6 candles.",
                "Long: the close crosses above the upper band. Short: the close crosses below the lower band.",
                "Exit long: close below the middle band. Exit short: close above it."]),
    dict(slug="keltner-channel-breakout", name="Keltner channel breakout", category=BREAKOUT, fn=s_keltner, stop_atr=2.0,
         max_bars=120, reverse=False,
         summary="Buy a close above the upper Keltner channel, exit on a close below its middle (20 EMA).",
         description="Keltner channels sit 2 ATR(10) around a 20 EMA. A close outside the channel is a strong, "
                     "volatility-adjusted breakout.",
         rules=["Long: the close crosses above the upper channel.", "Short: the close crosses below the lower channel.",
                "Exit long: close below the 20 EMA. Exit short: close above it."]),
    dict(slug="daily-range-breakout", name="Opening range breakout (first 4 hours)", category=BREAKOUT, fn=s_day_range,
         stop_atr=1.5, max_bars=24, reverse=False,
         summary="Trade the first close outside the high / low of the first 4 hours of the UTC day; flat at the day's end.",
         description="The opening-range breakout, adapted to markets that trade around the clock: the first four hours of "
                     "each UTC day set the range, and the first break of it later that day is traded.",
         rules=["Range: high and low of 00:00-04:00 UTC.", "Long: the first close above the range high that day.",
                "Short: the first close below the range low that day.", "Exit: the last candle of the UTC day."]),
    dict(slug="nr7-breakout", name="NR7 volatility breakout", category=BREAKOUT, fn=s_nr7, stop_atr=1.5, max_bars=10,
         reverse=False,
         summary="After the narrowest candle of the last 7 (NR7), trade a close beyond its high or low within 3 candles.",
         description="Toby Crabel's narrow-range idea: the smallest range in seven candles marks a pause, and the break of "
                     "that candle often starts the next move.",
         rules=["Setup: the candle's range is the smallest of the last 7.",
                "Long: within 3 candles the close is above the setup candle's high.",
                "Short: within 3 candles the close is below its low.",
                "Exit: after 10 candles, or earlier on a close back beyond the other side of the setup candle."]),
    dict(slug="chandelier-exit-trend", name="Chandelier exit trend (22, 3)", category=TREND, fn=s_chandelier,
         stop_atr=2.5, max_bars=300, reverse=True,
         summary="Ride the trend until price closes through the Chandelier stop (22-candle extreme minus 3 ATR).",
         description="Chuck LeBeau's Chandelier exit hangs a stop 3 ATR(22) from the highest high (long) or lowest low "
                     "(short) of the last 22 candles; a close through it flips the trend.",
         rules=["Long: the close crosses above the short-side Chandelier line.",
                "Short: the close crosses below the long-side Chandelier line.", "Exit: the next flip."]),
    dict(slug="inside-bar-breakout", name="Inside bar breakout", category=PATTERN, fn=s_inside_bar, stop_atr=1.5,
         max_bars=40, reverse=False,
         summary="A close beyond an inside bar's range, in the direction of the 50 EMA trend.",
         description="An inside bar has its high and low inside the previous candle: a pause. The break of the mother "
                     "candle is traded only with the trend.",
         rules=["Setup: the previous candle was an inside bar.",
                "Long: close above the mother candle's high and above the 50 EMA.",
                "Short: close below the mother candle's low and below the 50 EMA.",
                "Exit long: close below the 20 EMA. Exit short: close above it."]),
    dict(slug="roc-momentum", name="Rate of change momentum (12)", category=MOMENTUM, fn=s_roc, stop_atr=2.0,
         max_bars=150, reverse=False,
         summary="Buy when the 12-candle rate of change turns positive above the 100 SMA; exit when it turns negative.",
         description="Rate of change is the plain percentage move over 12 candles. A move from negative to positive in a "
                     "rising market is a momentum entry.",
         rules=["Long: ROC(12) crosses above 0 and the close is above the 100 SMA.",
                "Short: ROC(12) crosses below 0 and the close is below the 100 SMA.", "Exit: ROC crosses 0 the other way."]),
    dict(slug="rsi-50-trend-filter", name="RSI 50 cross with trend filter", category=MOMENTUM, fn=s_rsi50, stop_atr=2.0,
         max_bars=150, reverse=False,
         summary="RSI(14) crossing 50 in the direction of the 50 / 200 EMA trend; exit when RSI gives back 10 points.",
         description="Momentum traders use RSI's midline: above 50 buyers lead. The cross is taken only in the direction "
                     "of the long-term trend.",
         rules=["Long: RSI crosses above 50 while the 50 EMA is above the 200 EMA.",
                "Short: RSI crosses below 50 while the 50 EMA is below the 200 EMA.",
                "Exit long: RSI below 40. Exit short: RSI above 60."]),
    dict(slug="engulfing-reversal", name="Engulfing candle at a 20-candle extreme", category=PATTERN, fn=s_engulfing,
         stop_atr=2.0, max_bars=40, reverse=False,
         summary="A bullish engulfing candle at a 20-candle low (or bearish at a high); exit at the 20 SMA.",
         description="One of the best known candlestick patterns: a candle whose body swallows the previous one in the "
                     "opposite direction, taken only where a reversal makes sense, at a fresh extreme.",
         rules=["Long: bullish engulfing with the low of the last two candles at a 20-candle low.",
                "Short: bearish engulfing with the high of the last two candles at a 20-candle high.",
                "Exit: the close reaches the 20 SMA."]),
    dict(slug="pin-bar-reversal", name="Pin bar (hammer / shooting star)", category=PATTERN, fn=s_pin_bar, stop_atr=2.0,
         max_bars=40, reverse=False,
         summary="A hammer at a 20-candle low or a shooting star at a 20-candle high; exit at the 20 SMA.",
         description="A pin bar has a long shadow (at least twice the body and 60% of the candle) that shows price was "
                     "pushed away from an extreme.",
         rules=["Long: a hammer that makes a 20-candle low.", "Short: a shooting star that makes a 20-candle high.",
                "Exit: the close reaches the 20 SMA."]),
    dict(slug="volume-breakout", name="Volume breakout", category=BREAKOUT, fn=s_volume_breakout, stop_atr=2.0,
         max_bars=100, reverse=False,
         summary="A close beyond the 20-candle range on at least twice the average volume; exit below the 20 EMA.",
         description="Breakouts with heavy volume are often seen as more reliable than quiet ones. This version needs the "
                     "candle's volume to be at least twice its 20-candle average.",
         rules=["Long: close above the previous 20-candle high with volume above 2x its 20-candle average.",
                "Short: close below the previous 20-candle low with the same volume rule.",
                "Exit long: close below the 20 EMA. Exit short: close above it."]),
]
BY_SLUG = {s["slug"]: s for s in LIBRARY}


# ===========================================================================
# backtest
# ===========================================================================
def cost_r(symbol, entry, risk):
    if risk <= 0:
        return 0.0
    if markets.is_forex(symbol):
        return markets.cost_price(symbol) / risk
    return markets.CRYPTO_COST_ROUND_TRIP * entry / risk


def prepare(df):
    """Closed candles (timestamp, open, high, low, close, volume) -> frame with ATR(14)."""
    d = df[["timestamp", "open", "high", "low", "close"] + (["volume"] if "volume" in df else [])].copy()
    if "volume" not in d:
        d["volume"] = 0.0
    d["timestamp"] = pd.to_datetime(d["timestamp"])
    d = d.dropna(subset=["open", "high", "low", "close"]).sort_values("timestamp").drop_duplicates("timestamp")
    d = d.reset_index(drop=True)
    d["atr"] = atr(d["high"].to_numpy(float), d["low"].to_numpy(float), d["close"].to_numpy(float), 14)
    return d


def simulate(symbol, d, sig, spec, start_idx, tf_sec):
    """Trades of one strategy on one symbol (see the rules at the top of this file)."""
    o, h, l, c = (d[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    a = d["atr"].to_numpy(float)
    ts = pd.to_datetime(d["timestamp"]).to_numpy()
    le, se, lx, sx = sig["le"], sig["se"], sig["lx"], sig["sx"]
    k_stop, mb, rev = float(spec["stop_atr"]), int(spec["max_bars"]), bool(spec.get("reverse"))
    n = len(c)
    out = []
    i = max(int(start_idx), 1)
    step = np.timedelta64(int(tf_sec), "s")
    while i < n - 1:
        side = 1 if (le[i] and not se[i]) else (-1 if (se[i] and not le[i]) else 0)
        if side == 0 or not (a[i] > 0) or not np.isfinite(c[i]):
            i += 1
            continue
        entry = c[i]
        risk = k_stop * a[i]
        stop = entry - side * risk
        last = min(n - 1, i + mb)
        px = why = None
        j = i + 1
        while j <= last:
            if side > 0:
                if o[j] <= stop:
                    px, why = o[j], "stop"
                elif l[j] <= stop:
                    px, why = stop, "stop"
                elif rev and se[j]:
                    px, why = c[j], "reverse"
                elif lx[j]:
                    px, why = c[j], "exit"
            else:
                if o[j] >= stop:
                    px, why = o[j], "stop"
                elif h[j] >= stop:
                    px, why = stop, "stop"
                elif rev and le[j]:
                    px, why = c[j], "reverse"
                elif sx[j]:
                    px, why = c[j], "exit"
            if px is None and j == i + mb:
                px, why = c[j], "time"
            if px is not None:
                break
            j += 1
        if px is None:                      # still open at the end of the data: not counted
            break
        r = side * (px - entry) / risk - cost_r(symbol, entry, risk)
        out.append({"symbol": symbol, "side": "LONG" if side > 0 else "SHORT", "i": i, "j": j,
                    "entry_time": ts[i] + step, "exit_time": ts[j] + step, "entry": float(entry), "exit": float(px),
                    "r": float(r), "bars": int(j - i), "why": why})
        i = j                               # a new entry may start at the exit candle's close
    return out


def _r2(x, k=3):
    return None if x is None or not np.isfinite(x) else round(float(x), k)


def _iso(t):
    return pd.Timestamp(t).strftime("%Y-%m-%dT%H:%M:%SZ")


def stats(trades, days, nsym):
    """Summary of a list of trades (all symbols of one market and timeframe)."""
    n = len(trades)
    base = {"trades": n, "symbols": nsym, "days": round(days, 1),
            "trades_per_day": _r2(n / days if days > 0 else 0.0, 2)}
    if not n:
        return {**base, "win_rate": None, "avg_r": None, "total_r": 0.0, "profit_factor": None, "max_drawdown_r": 0.0,
                "avg_bars": None, "long": {"trades": 0, "avg_r": None}, "short": {"trades": 0, "avg_r": None},
                "curve": [], "positive_symbols": 0}
    r = np.array([t["r"] for t in trades])
    order = np.argsort(np.array([t["exit_time"] for t in trades]))
    eq = np.cumsum(r[order])
    dd = float(np.min(eq - np.maximum.accumulate(np.r_[0.0, eq])[1:])) if n else 0.0
    gain, loss = float(r[r > 0].sum()), float(-r[r < 0].sum())
    idx = np.unique(np.linspace(0, n - 1, min(n, CURVE_POINTS)).round().astype(int))
    times = [trades[k]["exit_time"] for k in order]
    longs = [t["r"] for t in trades if t["side"] == "LONG"]
    shorts = [t["r"] for t in trades if t["side"] == "SHORT"]
    by = {}
    for t in trades:
        by.setdefault(t["symbol"], []).append(t["r"])
    return {**base,
            "win_rate": _r2(100.0 * float(np.mean(r > 0)), 1),
            "avg_r": _r2(float(np.mean(r))),
            "total_r": _r2(float(r.sum()), 1),
            "profit_factor": _r2(gain / loss, 2) if loss > 0 else None,
            "max_drawdown_r": _r2(dd, 1),
            "avg_bars": _r2(float(np.mean([t["bars"] for t in trades])), 1),
            "long": {"trades": len(longs), "avg_r": _r2(float(np.mean(longs))) if longs else None},
            "short": {"trades": len(shorts), "avg_r": _r2(float(np.mean(shorts))) if shorts else None},
            "curve": [[_iso(times[k]), _r2(float(eq[k]), 2)] for k in idx],
            "positive_symbols": sum(1 for v in by.values() if sum(v) > 0)}


def by_symbol(trades):
    by = {}
    for t in trades:
        by.setdefault(t["symbol"], []).append(t["r"])
    rows = [{"symbol": s, "trades": len(v), "win_rate": _r2(100.0 * float(np.mean(np.array(v) > 0)), 1),
             "avg_r": _r2(float(np.mean(v))), "total_r": _r2(float(np.sum(v)), 1)} for s, v in by.items()]
    return sorted(rows, key=lambda x: -(x["total_r"] or 0))


def run(spec, frames, tf_sec, start, end=None):
    """One strategy on one market / timeframe. frames: {symbol: prepared frame}. Returns (summary, trades)."""
    start = pd.Timestamp(start)
    end = pd.Timestamp(end) if end is not None else None
    trades = []
    used = 0
    first = last = None
    for sym, d in frames.items():
        if len(d) <= WARMUP + 5:
            continue
        ts = pd.to_datetime(d["timestamp"])
        i0 = max(WARMUP, int(np.searchsorted(ts.to_numpy(), np.datetime64(start))))
        if i0 >= len(d) - 1:
            continue
        dd = d if end is None else d[ts < end].reset_index(drop=True)
        sig = spec["fn"](dd)
        tr = simulate(sym, dd, sig, spec, i0, tf_sec)
        trades += tr
        used += 1
        t0, t1 = pd.Timestamp(dd["timestamp"].iloc[i0]), pd.Timestamp(dd["timestamp"].iloc[-1])
        first = t0 if first is None or t0 < first else first
        last = t1 if last is None or t1 > last else last
    days = max((last - first).total_seconds() / 86400, 1.0) if first is not None else 0.0
    s = stats(trades, days, used)
    s["by_symbol"] = by_symbol(trades)
    s["period"] = f"{first:%Y-%m-%d} to {last:%Y-%m-%d}" if first is not None else None
    return s, trades


def _round_series(x, sig=6):
    out = []
    for v in np.asarray(x, dtype=float):
        if not np.isfinite(v):
            out.append(None)
        elif v == 0:
            out.append(0.0)
        else:
            out.append(float(f"{v:.{sig}g}"))
    return out


def preview(spec, d, symbol, timeframe, tf_sec, bars=PREVIEW_BARS):
    """Data for the small chart: the last candles of one symbol, the strategy's lines and its entries."""
    if len(d) <= WARMUP + 5:
        return None
    sig = spec["fn"](d)
    n = len(d)
    k0 = max(0, n - bars)
    tr = simulate(symbol, d, sig, spec, max(WARMUP, k0 - spec["max_bars"]), tf_sec)
    marks = [[t["i"] - k0, "L" if t["side"] == "LONG" else "S"] for t in tr if t["i"] >= k0]
    exits = [[t["j"] - k0, t["why"]] for t in tr if k0 <= t["j"] < n]
    osc = sig.get("osc")
    return {"symbol": symbol, "timeframe": timeframe,
            "start": _iso(d["timestamp"].iloc[k0]), "end": _iso(d["timestamp"].iloc[-1]),
            "o": _round_series(d["open"].to_numpy()[k0:]), "h": _round_series(d["high"].to_numpy()[k0:]),
            "l": _round_series(d["low"].to_numpy()[k0:]), "c": _round_series(d["close"].to_numpy()[k0:]),
            "overlays": {k: _round_series(v[k0:]) for k, v in (sig.get("overlays") or {}).items()},
            "osc": {"lines": {k: _round_series(v[k0:], 4) for k, v in osc["lines"].items()}, "levels": osc["levels"]}
            if osc else None,
            "entries": marks, "exits": exits}


def resample(df1h, rule):
    """1h candles -> 4h candles (UTC buckets), keeping only complete buckets."""
    d = df1h.set_index(pd.to_datetime(df1h["timestamp"]))
    g = d.resample(rule, label="left", closed="left", origin="epoch")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    cnt = g["close"].count()
    need = int(pd.Timedelta(rule) / pd.Timedelta("1h"))
    last_close = pd.Timestamp(d.index[-1]) + pd.Timedelta("1h") if len(d) else None
    keep = np.array(cnt.to_numpy() >= max(1, need - 1), dtype=bool)
    if last_close is not None:
        keep = keep & np.asarray(out.index + pd.Timedelta(rule) <= last_close)      # the bucket has fully closed
    out = out[keep].dropna(subset=["close"])
    return out.reset_index(names="timestamp")


def describe(spec):
    """The public part of a strategy definition."""
    return {k: spec[k] for k in ("slug", "name", "category", "summary", "description", "rules", "stop_atr", "max_bars",
                                 "reverse")}


def headline(results):
    r = results.get(HEADLINE_KEY) or {}
    if not r or r.get("error") or not r.get("trades"):
        return None
    return {"key": HEADLINE_KEY, "market": "crypto", "timeframe": "4h", "avg_r": r.get("avg_r"),
            "win_rate": r.get("win_rate"), "trades": r.get("trades"), "trades_per_day": r.get("trades_per_day"),
            "total_r": r.get("total_r"), "profit_factor": r.get("profit_factor")}


def compute_all(frames, start, now, source="live", note=None):
    """frames: {"crypto": {"1h": {symbol: prepared}, "4h": {...}}, "forex": {...}} -> {slug: stored result}."""
    out = {}
    stamp = pd.Timestamp(now).strftime("%Y-%m-%dT%H:%M:%SZ")
    for spec in LIBRARY:
        results = {}
        for market in ("crypto", "forex"):
            for tf, sec in (("1h", 3600), ("4h", 14400)):
                key = f"{market}_{tf}"
                F = (frames.get(market) or {}).get(tf) or {}
                if not F:
                    results[key] = {"market": market, "timeframe": tf, "error": "No candles for this market right now."}
                    continue
                try:
                    s, _ = run(spec, F, sec, start)
                    results[key] = {"market": market, "timeframe": tf, **s}
                except Exception as e:  # one broken symbol or rule must not stop the others
                    results[key] = {"market": market, "timeframe": tf, "error": f"Could not run: {str(e)[:80]}"}
        pv = None
        f4 = (frames.get("crypto") or {}).get("4h") or {}
        sym = PREVIEW_SYMBOL if PREVIEW_SYMBOL in f4 else (next(iter(f4)) if f4 else None)
        if sym:
            try:
                pv = preview(spec, f4[sym], sym, "4h", 14400)
            except Exception:
                pv = None
        out[spec["slug"]] = {"slug": spec["slug"], "updated_at": stamp, "source": source, "note": note,
                             "results": results, "preview": pv, "headline": headline(results)}
    return out
