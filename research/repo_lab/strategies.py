"""Entry rules ported from public GitHub strategy repositories. Each function takes a candle frame (open, high,
low, close, volume) and returns (long_mask, short_mask): True on the candle whose CLOSE triggers the entry.
Exits are NOT the repos' own: every rule is tested with the same stop / target plans (see lab.py), because the
site's tracker needs a stop and targets for each signal.

Sources
  ft_  github.com/freqtrade/freqtrade-strategies   (user_data/strategies, berlinguyinca/, futures/)
  js_  github.com/jesse-ai/example-strategies
  zz_  github.com/zeta-zetra/code                   (forex strategies: "20 pips challenge", "code trading")
  cs_  github.com/CryptoSignal/Crypto-Signal        (app/analyzers/indicators: default buy / sell thresholds)
Rules that need data we do not have (order book, fear & greed, other pairs) or that are pure hyperopt output
(GodStra, Heracles, mabStra, MultiMa, Diamond, UniversalMACD, NostalgiaForInfinity) are left out."""
import numpy as np
import pandas as pd

import ind as I

F = pd.Series(dtype=bool)


def _false(d):
    return pd.Series(False, index=d.index)


def _hammer(o, h, l, c):
    body = (c - o).abs()
    lower = np.minimum(o, c) - l
    upper = h - np.maximum(o, c)
    return (lower >= 2 * body) & (upper <= body) & (body > 0)


def _star(o, h, l, c):
    body = (c - o).abs()
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    return (upper >= 2 * body) & (lower <= body) & (body > 0)


def _all_above(l, line, n):
    return (l > line).astype(float).rolling(n).min() == 1


def _all_below(h, line, n):
    return (h < line).astype(float).rolling(n).min() == 1


# ----------------------------------------------------------------------------------------------- freqtrade
def ft_ADXMomentum(d):
    o, h, l, c = d.open, d.high, d.low, d.close
    adx, pdi, ndi = I.dmi(h, l, c, 14)
    mom = c - c.shift(14)
    lg = (adx > 25) & (mom > 0) & (pdi > 25) & (pdi > ndi)
    return lg & ~lg.shift(1, fill_value=False), _false(d)


def ft_AdxSmas(d):
    adx, _, _ = I.dmi(d.high, d.low, d.close, 14)
    return (adx > 25) & I.cross_up(I.sma(d.close, 3), I.sma(d.close, 6)), _false(d)


def ft_AverageStrategy(d):
    return I.cross_up(I.ema(d.close, 8), I.ema(d.close, 21)), _false(d)


def ft_AwesomeMacd(d):
    m, _, _ = I.macd(d.close)
    a = I.ao(d.high, d.low)
    return (m > 0) & (a > 0) & (a.shift(1) < 0), _false(d)


def ft_BbandRsi(d):
    _, _, lo = I.bbands(d.close, 20, 2)
    lg = (I.rsi(d.close, 14) < 30) & (d.close < lo)
    return lg & ~lg.shift(1, fill_value=False), _false(d)


def ft_CMCWinner(d):
    h, l, c, v = d.high, d.low, d.close, d.volume
    lg = (I.cci_fast(h, l, c, 14).shift(1) < -100) & (I.mfi(h, l, c, v, 14).shift(1) < 20) & (I.cmo(c, 14).shift(1) < -50)
    return lg & ~lg.shift(1, fill_value=False), _false(d)


def ft_ClucMay72018(d):
    c, v = d.close, d.volume
    _, _, lo = I.bbands(c, 20, 2)
    lg = (c < I.ema(c, 100)) & (c < 0.985 * lo) & (v < v.rolling(30).mean().shift(1) * 20)
    return lg & ~lg.shift(1, fill_value=False), _false(d)


def ft_CofiBit(d):
    fk, fd, _, _ = I.stoch(d.high, d.low, d.close, 5, 3, 3)
    adx, _, _ = I.dmi(d.high, d.low, d.close, 14)
    return ((d.open < I.ema(d.low, 5)) & I.cross_up(fk, fd) & (fk < 23) & (fd < 23) & (adx > 25)), _false(d)


def ft_MACDcross(d):
    m, s, _ = I.macd(d.close)
    return I.cross_up(m, s) & (I.cci_fast(d.high, d.low, d.close, 20) <= -50), _false(d)


def ft_Quickie(d):
    c = d.close
    t = I.tema(c, 9)
    _, mid, _ = I.bbands(c, 20, 2)
    adx, _, _ = I.dmi(d.high, d.low, c, 14)
    lg = (adx > 30) & (t < mid) & (t > t.shift(1)) & (I.sma(c, 200) > c)
    return lg & ~lg.shift(1, fill_value=False), _false(d)


def ft_Simple(d):
    c = d.close
    m, s, _ = I.macd(c)
    up, _, _ = I.bbands(c, 12, 2)
    lg = (m > 0) & (m > s) & (up > up.shift(1)) & (I.rsi(c, 14) > 70)
    return lg & ~lg.shift(1, fill_value=False), _false(d)


def ft_Strategy001(d):
    ho, hc = I.heikin(d.open, d.high, d.low, d.close)
    e20, e50 = I.ema(d.close, 20), I.ema(d.close, 50)
    return I.cross_up(e20, e50) & (hc > e20) & (ho < hc), _false(d)


def ft_Strategy002(d):
    o, h, l, c = d.open, d.high, d.low, d.close
    _, _, sk, _ = I.stoch(h, l, c, 14, 3, 3)
    _, _, lo = I.bbands(c, 20, 2)
    return (I.rsi(c, 14) < 30) & (sk < 20) & (lo > c) & _hammer(o, h, l, c), _false(d)


def ft_Strategy003(d):
    h, l, c, v = d.high, d.low, d.close, d.volume
    fk, fd, _, _ = I.stoch(h, l, c, 5, 3, 3)
    lg = ((I.rsi(c, 14) < 28) & (c < I.sma(c, 40)) & (I.fisher_rsi(c) < -0.94) & (I.mfi(h, l, c, v, 14) < 16)
          & ((I.ema(c, 50) > I.ema(c, 100)) | I.cross_up(I.ema(c, 5), I.ema(c, 10))) & (fd > fk) & (fd > 0))
    return lg & ~lg.shift(1, fill_value=False), _false(d)


def ft_Strategy004(d):
    h, l, c = d.high, d.low, d.close
    adx, _, _ = I.dmi(h, l, c, 14)
    sadx, _, _ = I.dmi(h, l, c, 35)
    fk, fd, _, _ = I.stoch(h, l, c, 5, 3, 3)
    lg = (((adx > 50) | (sadx > 26)) & (I.cci_fast(h, l, c, 20) < -100) & (fk.shift(1) < 20) & (fd.shift(1) < 20)
          & (fk.shift(1) < fd.shift(1)) & (fk > fd))
    return lg, _false(d)


def ft_hlhb(d):
    c = d.close
    r = I.rsi(c, 10)
    e5, e10 = I.ema(c, 5), I.ema(c, 10)
    adx, _, _ = I.dmi(d.high, d.low, c, 14)
    return (I.cross_up(r, 50) & I.cross_up(e5, e10) & (adx > 25),
            I.cross_dn(r, 50) & I.cross_dn(e5, e10) & (adx > 25))


def ft_Supertrend(d):
    h, l, c = d.high, d.low, d.close
    a, b, e = I.supertrend(h, l, c, 3, 10), I.supertrend(h, l, c, 2, 20), I.supertrend(h, l, c, 4, 30)
    up = (a == 1) & (b == 1) & (e == 1)
    dn = (a == -1) & (b == -1) & (e == -1)
    return up & ~up.shift(1, fill_value=False), dn & ~dn.shift(1, fill_value=False)


def ft_FAdxSma(d):
    adx, _, _ = I.dmi(d.high, d.low, d.close, 14)
    s, lg = I.sma(d.close, 10), I.sma(d.close, 50)
    return (adx > 25) & I.cross_up(s, lg), (adx > 25) & I.cross_dn(s, lg)


def ft_FSample(d):
    c = d.close
    r, t = I.rsi(c, 14), I.tema(c, 9)
    _, mid, _ = I.bbands(c, 20, 2)
    return (I.cross_up(r, 30) & (t <= mid) & (t > t.shift(1)),
            I.cross_up(r, 70) & (t > mid) & (t < t.shift(1)))


def ft_TrendFollowing(d):
    c = d.close
    tr = I.ema(c, 20)
    ob = I.obv(c, d.volume)
    return ((c > tr) & (c.shift(1) <= tr.shift(1)) & (ob > ob.shift(1)),
            (c < tr) & (c.shift(1) >= tr.shift(1)) & (ob < ob.shift(1)))


def ft_VolatilitySystem(d):
    c = d.close
    ch = c - c.shift(1)
    a = I.atr(d.high, d.low, c, 14).shift(1) * 2
    return ch > a, -ch > a


def ft_PSAR(d):
    s = I.psar(d.high, d.low)
    return I.cross_up(d.close, s), I.cross_dn(d.close, s)


def ft_TDSequential(d):
    c = d.close
    dn = (c < c.shift(4)).astype(int)
    up = (c > c.shift(4)).astype(int)
    cnt_d = dn.groupby((dn == 0).cumsum()).cumsum()
    cnt_u = up.groupby((up == 0).cumsum()).cumsum()
    return cnt_d == 9, cnt_u == 9


# ----------------------------------------------------------------------------------------------- jesse
def js_Donchian(d):
    c = d.close
    up = d.high.rolling(20).max().shift(1)
    lg = (c > up) & (c > I.sma(c, 200))
    return lg & ~lg.shift(1, fill_value=False), _false(d)


def js_IFR2(d):
    c = d.close
    _, _, a, b = I.ichimoku(d.high, d.low, c)
    return (I.rsi(c, 2) < 10) & (c > a) & (c > b), _false(d)


def js_MACD_EMA(d):
    c = d.close
    m, s, _ = I.macd(c)
    return I.cross_up(m, s) & (c > I.ema(c, 100)), _false(d)


def js_RSI2(d):
    c = d.close
    r, s = I.rsi(c, 2), I.sma(c, 200)
    return (c > s) & (r <= 10), (c < s) & (r >= 90)


def js_SMACrossover(d):
    f, s = I.sma(d.close, 50), I.sma(d.close, 200)
    return I.cross_up(f, s), I.cross_dn(f, s)


def js_SimpleBollinger(d):
    c = d.close
    up, _, _ = I.bbands((d.high + d.low) / 2, 20, 2)
    _, _, a, b = I.ichimoku(d.high, d.low, c)
    return I.cross_up(c, up) & (c > a) & (c > b), _false(d)


def js_TradingViewRSI(d):
    return I.cross_up(I.rsi(d.close, 14), 35), _false(d)


def js_Turtle(d):
    up = d.high.rolling(20).max().shift(1)
    lo = d.low.rolling(20).min().shift(1)
    lg, sh = d.close > up, d.close < lo
    return lg & ~lg.shift(1, fill_value=False), sh & ~sh.shift(1, fill_value=False)


def js_KDJ(d):
    k, dd, j = I.kdj(d.high, d.low, d.close)
    return I.cross_up(j, k) & (j > dd) & (k < 30), _false(d)


# ----------------------------------------------------------------------------------------------- zeta-zetra (forex)
def zz_ema_scalping(d):
    c, l, h = d.close, d.low, d.high
    e50, e100, e150 = I.ema(c, 50), I.ema(c, 100), I.ema(c, 150)
    up = (e50 > e100) & (e100 > e150) & (e50 > e50.shift(5))
    dn = (e50 < e100) & (e100 < e150) & (e50 < e50.shift(5))
    return up & (l <= e50) & (c > e50), dn & (h >= e50) & (c < e50)


def zz_rayner_teo_bb(d):
    c = d.close
    s = I.sma(c, 200)
    up, _, lo = I.bbands(c, 20, 2.5)
    return _all_above(d.high, s, 5) & (c <= lo), _all_below(d.low, s, 5) & (c >= up)


def zz_engulfing(d):
    o, c = d.open, d.close
    po, pc = o.shift(1), c.shift(1)
    return ((pc < po) & (c > o) & (c > po) & (o <= pc), (pc > po) & (c < o) & (c < po) & (o >= pc))


def zz_heiken_ashi(d):
    ho, hc = I.heikin(d.open, d.high, d.low, d.close)
    e20, e50 = I.ema(d.close, 20), I.ema(d.close, 50)
    return (e20 > e50) & (ho < e20) & (hc > e20), (e20 < e50) & (ho > e20) & (hc < e20)


def zz_bollinger_candle(d):
    o, c = d.open, d.close
    up, _, lo = I.bbands(c, 30, 2)
    lg = (c.shift(1) < lo.shift(1)) & (c > o) & (c.shift(1) < o.shift(1)) & (o < c.shift(1)) & (c > o.shift(1))
    sh = (c.shift(1) > up.shift(1)) & (c < o) & (c.shift(1) > o.shift(1)) & (o > c.shift(1)) & (c < o.shift(1))
    return lg, sh


def zz_star_rsi(d):
    o, h, l, c = d.open, d.high, d.low, d.close
    r = I.rsi(c, 14)
    return _hammer(o, h, l, c) & (r < 30), _star(o, h, l, c) & (r > 70)


def zz_scalping_rsi(d):
    c = d.close
    e = I.ema(c, 200)
    r = I.rsi(c, 3)
    return _all_above(d.low, e, 15) & (r < 10), _all_below(d.high, e, 15) & (r > 90)


def zz_adx_rsi_ema(d):
    c = d.close
    e = I.ema(c, 200)
    r = I.rsi(c, 3)
    adx, _, _ = I.dmi(d.high, d.low, c, 5)
    return (_all_above(d.low, e, 8) & (r < 20) & (adx > 30), _all_below(d.high, e, 8) & (r > 80) & (adx > 30))


def zz_ma_hilo(d):
    e = I.ema(d.close, 100)
    lo = d.low.rolling(7).min().shift(1)
    hi = d.high.rolling(7).max().shift(1)
    return _all_above(d.low, e, 30).shift(1, fill_value=False) & (d.low <= lo), \
        _all_below(d.high, e, 30).shift(1, fill_value=False) & (d.high >= hi)


# ----------------------------------------------------------------------------------------------- Crypto-Signal
def cs_rsi(d):
    r = I.rsi(d.close, 14)
    return I.cross_up(r, 30), I.cross_dn(r, 70)


def cs_macd(d):
    m, s, _ = I.macd(d.close)
    return I.cross_up(m, s), I.cross_dn(m, s)


def cs_ichimoku(d):
    c = d.close
    t, k, a, b = I.ichimoku(d.high, d.low, c)
    top, bot = np.maximum(a, b), np.minimum(a, b)
    return I.cross_up(t, k) & (c > top), I.cross_dn(t, k) & (c < bot)


def cs_stochrsi(d):
    k, _ = I.stochrsi(d.close)
    return I.cross_up(k, 20), I.cross_dn(k, 80)


def cs_mfi(d):
    m = I.mfi(d.high, d.low, d.close, d.volume, 14)
    return I.cross_up(m, 20), I.cross_dn(m, 80)


def cs_momentum(d):
    m = d.close - d.close.shift(10)
    return I.cross_up(m, 0), I.cross_dn(m, 0)


STRATEGIES = {k: v for k, v in globals().items() if k[:3] in ("ft_", "js_", "zz_", "cs_") and callable(v)}
# volume-based rules need real volume (HistData forex volume is 0)
NEEDS_VOLUME = {"ft_CMCWinner", "ft_ClucMay72018", "ft_Strategy003", "ft_TrendFollowing", "cs_mfi"}
