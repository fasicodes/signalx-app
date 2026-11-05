"""Indicators (pandas / numpy), TA-Lib conventions where it matters (Wilder smoothing for RSI / ATR / ADX)."""
import numpy as np
import pandas as pd

EPS = 1e-12


def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s, n):
    return s.rolling(n, min_periods=n).mean()


def wilder(s, n):
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(c, n=14):
    d = c.diff()
    up, dn = wilder(d.clip(lower=0), n), wilder((-d).clip(lower=0), n)
    return 100 - 100 / (1 + up / (dn + EPS))


def true_range(h, l, c):
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def atr(h, l, c, n=14):
    return wilder(true_range(h, l, c), n)


def dmi(h, l, c, n=14):
    upm, dnm = h.diff(), -l.diff()
    pdm = pd.Series(np.where((upm > dnm) & (upm > 0), upm, 0.0), index=h.index)
    ndm = pd.Series(np.where((dnm > upm) & (dnm > 0), dnm, 0.0), index=h.index)
    tr = wilder(true_range(h, l, c), n) + EPS
    pdi, ndi = 100 * wilder(pdm, n) / tr, 100 * wilder(ndm, n) / tr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi + EPS)
    return wilder(dx, n), pdi, ndi


def macd(c, f=12, s=26, sig=9):
    m = ema(c, f) - ema(c, s)
    sg = m.ewm(span=sig, adjust=False).mean()
    return m, sg, m - sg


def bbands(c, n=20, k=2.0):
    m = sma(c, n)
    sd = c.rolling(n, min_periods=n).std(ddof=0)
    return m + k * sd, m, m - k * sd


def stoch(h, l, c, k=14, d=3, smooth=3):
    lo, hi = l.rolling(k).min(), h.rolling(k).max()
    fastk = 100 * (c - lo) / (hi - lo + EPS)
    slowk = fastk.rolling(smooth).mean()
    return fastk, fastk.rolling(d).mean(), slowk, slowk.rolling(d).mean()


def stochrsi(c, n=14, k=5, d=3):
    r = rsi(c, n)
    lo, hi = r.rolling(n).min(), r.rolling(n).max()
    fk = 100 * (r - lo) / (hi - lo + EPS)
    fk = fk.rolling(k).mean()
    return fk, fk.rolling(d).mean()


def cci(h, l, c, n=20):
    tp = (h + l + c) / 3
    m = tp.rolling(n).mean()
    md = tp.rolling(n).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - m) / (0.015 * md + EPS)


def cci_fast(h, l, c, n=20):
    """CCI with the mean absolute deviation approximated by 0.8 x std (much faster on long series)."""
    tp = (h + l + c) / 3
    return (tp - tp.rolling(n).mean()) / (0.015 * 0.7979 * tp.rolling(n).std(ddof=0) + EPS)


def mfi(h, l, c, v, n=14):
    tp = (h + l + c) / 3
    mf = tp * v
    pos = mf.where(tp > tp.shift(1), 0.0).rolling(n).sum()
    neg = mf.where(tp < tp.shift(1), 0.0).rolling(n).sum()
    return 100 - 100 / (1 + pos / (neg + EPS))


def cmo(c, n=14):
    d = c.diff()
    up, dn = d.clip(lower=0).rolling(n).sum(), (-d).clip(lower=0).rolling(n).sum()
    return 100 * (up - dn) / (up + dn + EPS)


def heikin(o, h, l, c):
    hc = (o + h + l + c) / 4
    ho = np.empty(len(c))
    ov, cv = o.to_numpy(float), hc.to_numpy(float)
    ho[0] = (ov[0] + c.iloc[0]) / 2
    for i in range(1, len(c)):
        ho[i] = (ho[i - 1] + cv[i - 1]) / 2
    return pd.Series(ho, index=c.index), hc


def supertrend(h, l, c, mult=3.0, n=10):
    """+1 up / -1 down."""
    a = atr(h, l, c, n).to_numpy(float)
    hl2 = ((h + l) / 2).to_numpy(float)
    cv = c.to_numpy(float)
    ub, lb = hl2 + mult * a, hl2 - mult * a
    fu, fl = ub.copy(), lb.copy()
    dirn = np.ones(len(cv))
    for i in range(1, len(cv)):
        if np.isnan(a[i]):
            continue
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or cv[i - 1] > fu[i - 1] or np.isnan(fu[i - 1])) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or cv[i - 1] < fl[i - 1] or np.isnan(fl[i - 1])) else fl[i - 1]
        if dirn[i - 1] == 1:
            dirn[i] = -1 if cv[i] < fl[i] else 1
        else:
            dirn[i] = 1 if cv[i] > fu[i] else -1
    return pd.Series(dirn, index=c.index)


def psar(h, l, af0=0.02, step=0.02, mx=0.2):
    hv, lv = h.to_numpy(float), l.to_numpy(float)
    n = len(hv)
    sar = np.full(n, np.nan)
    if n < 3:
        return pd.Series(sar, index=h.index)
    up, af, ep, s = True, af0, hv[0], lv[0]
    for i in range(1, n):
        s = s + af * (ep - s)
        if up:
            s = min(s, lv[i - 1], lv[i - 2] if i > 1 else lv[i - 1])
            if lv[i] < s:
                up, s, ep, af = False, ep, lv[i], af0
            elif hv[i] > ep:
                ep, af = hv[i], min(af + step, mx)
        else:
            s = max(s, hv[i - 1], hv[i - 2] if i > 1 else hv[i - 1])
            if hv[i] > s:
                up, s, ep, af = True, ep, hv[i], af0
            elif lv[i] < ep:
                ep, af = lv[i], min(af + step, mx)
        sar[i] = s
    return pd.Series(sar, index=h.index)


def tema(c, n=9):
    e1 = ema(c, n)
    e2 = ema(e1, n)
    return 3 * e1 - 3 * e2 + ema(e2, n)


def fisher_rsi(c, n=14):
    r = 0.1 * (rsi(c, n) - 50)
    return (np.exp(2 * r) - 1) / (np.exp(2 * r) + 1)


def obv(c, v):
    return (np.sign(c.diff()).fillna(0) * v).cumsum()


def ao(h, l):
    m = (h + l) / 2
    return sma(m, 5) - sma(m, 34)


def kdj(h, l, c, n=9):
    lo, hi = l.rolling(n).min(), h.rolling(n).max()
    rsv = 100 * (c - lo) / (hi - lo + EPS)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    return k, d, 3 * k - 2 * d


def ichimoku(h, l, c):
    tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2
    kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(26)
    span_b = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26)
    return tenkan, kijun, span_a, span_b


def cross_up(a, b):
    a, b = pd.Series(a), pd.Series(b) if not np.isscalar(b) else b
    return (a > b) & (a.shift(1) <= (b.shift(1) if not np.isscalar(b) else b))


def cross_dn(a, b):
    a, b = pd.Series(a), pd.Series(b) if not np.isscalar(b) else b
    return (a < b) & (a.shift(1) >= (b.shift(1) if not np.isscalar(b) else b))
