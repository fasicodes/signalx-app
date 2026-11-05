"""Signal logic of popular open-source trading BOTS (GitHub, ranked by stars), ported with their default settings.
Same interface as strategies.py: fn(candles) -> (long_mask, short_mask), True on the candle whose close fires.
Exits are the lab's common stop / target plans (lab.py), not each bot's own sell logic.

  gk_  askmike/gekko            (strategies/*.js + config/strategies/*.toml defaults)
  zb_  DeviaVir/zenbot          (extensions/strategies/*/strategy.js defaults)
  ob_  Drakkar-Software/OctoBot (OctoBot-Tentacles Evaluator/TA: momentum + trend evaluators)
  hc_  Haehnchen/crypto-trading-bot (src/strategy/strategies)
  qt_  je-suis-tm/quant-trading (backtest scripts)
  bc_  bennycode/trading-signals (packages/trading-strategies)
  wb_  Ekliptor/WolfBot         (src/Strategies)
  itb_ asavinov/intelligent-trading-bot (configs/config-sample-1h: same features + labels, walk-forward model)
Gekko's MACD / PPO / DEMA thresholds are absolute price units in the original; here they are % of price so the
same default works on every symbol."""
import numpy as np
import pandas as pd

import ind as I


def _false(d):
    return pd.Series(False, index=d.index)


def _first(cond, persistence=1):
    """Gekko 'persisted and not yet advised': the candle where cond has held for max(persistence, 1) candles."""
    c = cond.fillna(False).astype(int)
    run = c.groupby((c == 0).cumsum()).cumsum()
    return run == max(int(persistence), 1)


def _turn(state):
    """True where a boolean state switches on."""
    s = state.fillna(False)
    return s & ~s.shift(1, fill_value=False)


def tsi(c, r=25, s=13):
    m = c.diff()
    num = m.ewm(span=r, adjust=False).mean().ewm(span=s, adjust=False).mean()
    den = m.abs().ewm(span=r, adjust=False).mean().ewm(span=s, adjust=False).mean()
    return 100 * num / (den + I.EPS)


def uo(h, l, c, p1=7, p2=14, p3=28):
    pc = c.shift(1)
    bp = c - np.minimum(l, pc)
    tr = np.maximum(h, pc) - np.minimum(l, pc)
    a = lambda n: bp.rolling(n).sum() / (tr.rolling(n).sum() + I.EPS)  # noqa: E731
    return 100 * (4 * a(p1) + 2 * a(p2) + a(p3)) / 7


def _wma(s, k):
    w = np.arange(1, k + 1, dtype=float)
    v = np.convolve(s.to_numpy(float), w[::-1] / w.sum(), mode="full")[:len(s)]
    v[:k - 1] = np.nan
    return pd.Series(v, index=s.index)


def hma(c, n=9):
    return _wma(2 * _wma(c, n // 2) - _wma(c, n), int(np.sqrt(n)))


def kama(c, n=10, fast=2, slow=30):
    er = (c - c.shift(n)).abs() / (c.diff().abs().rolling(n).sum() + I.EPS)
    sc = (er * (2 / (fast + 1) - 2 / (slow + 1)) + 2 / (slow + 1)) ** 2
    out = np.full(len(c), np.nan)
    cv, scv = c.to_numpy(float), sc.to_numpy(float)
    start = n
    if len(cv) > start:
        out[start] = cv[start]
        for i in range(start + 1, len(cv)):
            k = scv[i] if np.isfinite(scv[i]) else 0.0
            out[i] = out[i - 1] + k * (cv[i] - out[i - 1])
    return pd.Series(out, index=c.index)


def wavetrend(h, l, c, n1=10, n2=21):
    ap = (h + l + c) / 3
    esa = ap.ewm(span=n1, adjust=False).mean()
    dd = (ap - esa).abs().ewm(span=n1, adjust=False).mean()
    ci = (ap - esa) / (0.015 * dd + I.EPS)
    wt1 = ci.ewm(span=n2, adjust=False).mean()
    return wt1, wt1.rolling(4).mean()


# ------------------------------------------------------------------------------------------------ gekko
def gk_CCI(d):
    x = I.cci_fast(d.high, d.low, d.close, 20)
    return _first(x <= -100, 0), _first(x >= 100, 0)


def gk_DEMA(d):
    c = d.close
    dema = lambda n: 2 * I.ema(c, n) - I.ema(I.ema(c, n), n)  # noqa: E731
    s, lg = dema(10), dema(21)
    diff = 100 * (s - lg) / ((s + lg) / 2)
    return _turn(diff > 0.025), _turn(diff < -0.025)


def gk_MACD(d):
    c = d.close
    m = I.ema(c, 10) - I.ema(c, 21)
    diff = 100 * (m - m.ewm(span=9, adjust=False).mean()) / c
    return _first(diff > 0.025, 1), _first(diff < -0.025, 1)


def gk_PPO(d):
    c = d.close
    ppo = 100 * (I.ema(c, 12) - I.ema(c, 26)) / I.ema(c, 26)
    hist = ppo - ppo.ewm(span=9, adjust=False).mean()
    return _first(hist > 0.025, 2), _first(hist < -0.025, 2)


def gk_RSI(d):
    r = I.rsi(d.close, 14)
    return _first(r < 30, 1), _first(r > 70, 1)


def gk_StochRSI(d):
    r = I.rsi(d.close, 3)
    lo, hi = r.rolling(3).min(), r.rolling(3).max()
    s = 100 * (r - lo) / (hi - lo + I.EPS)
    return _first(s < 20, 3), _first(s > 80, 3)


def gk_TMA(d):
    c = d.close
    s, m, lg = I.sma(c, 7), I.sma(c, 25), I.sma(c, 99)
    up = (s > m) & (m > lg)
    return _turn(up), _turn(~up & lg.notna())


def gk_TSI(d):
    t = tsi(d.close)
    return _first(t < -25, 1), _first(t > 25, 1)


def gk_UO(d):
    u = uo(d.high, d.low, d.close)
    return _first(u < 30, 1), _first(u > 70, 1)


# ------------------------------------------------------------------------------------------------ zenbot
def zb_trend_ema(d):
    """zenbot's default strategy: rate of change of EMA26 vs its own stddev, plus 'oversold RSI 10' buys."""
    c = d.close
    e = I.ema(c, 26)
    rate = 100 * (e - e.shift(1)) / e.shift(1)
    sd = rate.rolling(52).std()
    up, dn = rate > sd, rate < -sd
    return _turn(up) | _first(I.rsi(c, 14) <= 10, 1), _turn(dn)


def zb_macd(d):
    _, _, hist = I.macd(d.close)
    return I.cross_up(hist, 0), I.cross_dn(hist, 0) | _first(I.rsi(d.close, 25) >= 70, 1)


def zb_rsi(d):
    r = I.rsi(d.close, 14)
    low = r.where(r <= 30).ffill(limit=30)
    high = r.where(r >= 82).ffill(limit=30)
    lg = (r.shift(1) <= 30 + 3).rolling(10).max().astype(bool) & (r >= r.rolling(10).min() + 3) & low.notna()
    sh = high.notna() & (r <= r.rolling(10).max() - 0) & (r.shift(1) >= 82)
    return _turn(lg), _turn(sh)


def zb_sar(d):
    s = I.psar(d.high, d.low, 0.015, 0.015, 0.3)
    c = d.close
    return I.cross_up(c, s) & (c > c.shift(1)), I.cross_dn(c, s) & (c < c.shift(1))


def zb_bollinger(d):
    up, _, lo = I.bbands(d.close, 20, 2)
    return _turn(d.close < lo), _turn(d.close > up)


def zb_cci_srsi(d):
    c = d.close
    x = I.cci_fast(d.high, d.low, c, 14)
    k, _ = I.stochrsi(c, 9, 5, 3)
    return _turn((x <= -90) & (k <= 18)), _turn((x >= 140) & (k >= 85))


def zb_srsi_macd(d):
    c = d.close
    k, dd = I.stochrsi(c, 9, 5, 3)
    m = I.ema(c, 24) - I.ema(c, 200)
    hist = m - m.ewm(span=9, adjust=False).mean()
    return ((hist >= 0) & (k > dd) & (k > k.shift(1)) & (k < 20),
            (hist < 0) & (k < dd) & (k < k.shift(1)) & (k > 80))


def zb_wavetrend(d):
    w1, w2 = wavetrend(d.high, d.low, d.close)
    return I.cross_up(w1, w2) & (w1 < -53), I.cross_dn(w1, w2) & (w1 > 53)


def zb_momentum(d):
    m = d.close - d.close.shift(5)
    return _turn((m > 0) & (m.shift(1) > 0)), _turn((m < 0) & (m.shift(1) < 0))


def zb_kc(d):
    mid = I.ema(d.close, 20)
    a = I.atr(d.high, d.low, d.close, 20)
    return _turn(d.close < mid - a), _turn(d.close > mid + a)


def zb_ichimoku(d):
    _, _, a, b = I.ichimoku(d.high, d.low, d.close)
    top = np.maximum(a, b)
    return _turn(d.close > top), _turn(d.close < top)


def zb_ta_ema(d):
    c = d.close
    e = I.ema(c, 20)
    rate = 100 * (e - e.shift(1)) / e.shift(1)
    return _turn(rate > 0.06) | _first(I.rsi(c, 20) <= 30, 1), _turn(rate < -0.06)


# ------------------------------------------------------------------------------------------------ OctoBot
def ob_RSIMomentum(d):
    r = I.rsi(d.close, 14)
    return _turn(r < 30), _turn(r > 70)


def ob_DoubleMovingAverage(d):
    f, s = I.sma(d.close, 5), I.sma(d.close, 10)
    return I.cross_up(f, s), I.cross_dn(f, s)


def ob_EMADivergence(d):
    e = I.ema(d.close, 50)
    div = 100 * (d.close - e) / e
    return _turn(div < -2), _turn(div > 2)


def ob_SuperTrend(d):
    st = I.supertrend(d.high, d.low, d.close, 3, 10)
    return _turn(st == 1), _turn(st == -1)


def ob_BBMomentum(d):
    up, _, lo = I.bbands(d.close, 20, 2)
    return _turn(d.close <= lo), _turn(d.close >= up)


# ------------------------------------------------------------------------------------------------ Haehnchen
def hc_cci(d):
    c = d.close
    x = I.cci_fast(d.high, d.low, c, 40)
    s200, e200 = I.sma(c, 200), I.ema(c, 200)
    deep_lo = (x <= -200).rolling(20).max().astype(bool)
    deep_hi = (x >= 200).rolling(20).max().astype(bool)
    return (I.cross_up(x, -100) & deep_lo & ((c > s200) | (c > e200)),
            I.cross_dn(x, 100) & deep_hi & (c < s200) & (c < e200))


def hc_macd(d):
    c = d.close
    _, _, hist = I.macd(c)
    h, s = hma(c, 9), I.sma(c, 200)
    return I.cross_up(hist, 0) & (h >= s), I.cross_dn(hist, 0) & (h < s)


def hc_ao_cross_zero(d):
    a = I.ao(d.high, d.low)
    s = I.sma(d.close, 200)
    return I.cross_up(a, 0) & (d.close >= s), I.cross_dn(a, 0) & (d.close < s)


# ------------------------------------------------------------------------------------------------ quant-trading
def qt_macd_oscillator(d):
    f, s = I.sma(d.close, 10), I.sma(d.close, 21)
    return I.cross_up(f, s), I.cross_dn(f, s)


def qt_dual_thrust(d):
    """Range of the 4 previous days; long when price closes above today's open + 0.5 x range (short mirrored)."""
    ts = pd.to_datetime(d.timestamp)
    day = ts.dt.floor("D")
    g = d.groupby(day)
    daily = pd.DataFrame({"o": g.open.first(), "h": g.high.max(), "l": g.low.min(), "c": g.close.last()})
    hh, ll = daily.h.rolling(4).max().shift(1), daily.l.rolling(4).min().shift(1)
    hc, lc = daily.c.rolling(4).max().shift(1), daily.c.rolling(4).min().shift(1)
    rng = np.maximum(hh - lc, hc - ll)
    up = (daily.o + 0.5 * rng).reindex(day).to_numpy()
    dn = (daily.o - 0.5 * rng).reindex(day).to_numpy()
    c = d.close.to_numpy()
    lg = pd.Series(c > up, index=d.index)
    sh = pd.Series(c < dn, index=d.index)
    first_l = lg & ~lg.groupby(day.values).cummax().shift(1, fill_value=False).astype(bool)
    first_s = sh & ~sh.groupby(day.values).cummax().shift(1, fill_value=False).astype(bool)
    return first_l, first_s


def qt_london_breakout(d):
    """Forex: high / low of the hour before the London open (07:00-08:00 UTC); first close outside it 08:00-09:00."""
    ts = pd.to_datetime(d.timestamp)
    hr, day = ts.dt.hour, ts.dt.floor("D")
    pre = d[hr == 7].groupby(day[hr == 7])
    hi = pre.high.max().reindex(day).to_numpy()
    lo = pre.low.min().reindex(day).to_numpy()
    win = (hr == 8).to_numpy()
    c = d.close.to_numpy()
    lg = pd.Series(win & (c > hi), index=d.index)
    sh = pd.Series(win & (c < lo), index=d.index)
    return lg & ~lg.shift(1, fill_value=False), sh & ~sh.shift(1, fill_value=False)


# ------------------------------------------------------------------------------------------------ bennycode
def bc_mean_reversion(d):
    up, _, lo = I.bbands(d.close, 20, 2.5)
    return _turn(d.close <= lo), _turn(d.close > up)


def bc_multi_confluence(d):
    c = d.close
    es, el = I.ema(c, 12), I.ema(c, 26)
    _, _, hist = I.macd(c)
    up, _, lo = I.bbands(c, 20, 2)
    r = I.rsi(c, 14)
    return (_turn((es > el) & (hist > 0) & (c <= lo) & (r < 70)),
            _turn((es < el) & (hist < 0) & (c >= up) & (r > 30)))


def bc_sma_crossover(d):
    f, s = I.sma(d.close, 10), I.sma(d.close, 30)
    return I.cross_up(f, s), I.cross_dn(f, s)


# ------------------------------------------------------------------------------------------------ WolfBot
def wb_BollingerBreakouts(d):
    up, _, lo = I.bbands(d.close, 20, 2)
    return _turn(d.close > up), _turn(d.close < lo)


def wb_KAMA(d):
    k = kama(d.close)
    return I.cross_up(d.close, k), I.cross_dn(d.close, k)


STRATEGIES = {k: v for k, v in globals().items() if k[:3] in ("gk_", "zb_", "ob_", "hc_", "qt_", "bc_", "wb_") and callable(v)}
NEEDS_VOLUME = set()
FOREX_ONLY = {"qt_london_breakout"}
