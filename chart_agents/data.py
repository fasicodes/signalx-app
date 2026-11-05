"""Candles plus the indicators a trader keeps on the chart.

Every value at candle i is computed from candles 0..i only (no look-ahead): recursive averages, rolling windows that
end at i. tests/test_chart_agents.py checks this by cutting the history and comparing.
"""
import numpy as np
import pandas as pd

from . import config as C


def _ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy(float)


def _rma(x, n):
    return pd.Series(x).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy(float)


def _sma(x, n):
    return pd.Series(x).rolling(n, min_periods=1).mean().to_numpy(float)


def resample(df, tf):
    """15-minute (or any finer) candles -> candles of timeframe tf, labelled by their open time (UTC)."""
    rule = C.PANDAS_RULE[tf]
    cols = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    for extra in ("taker_buy_volume", "quote_volume"):
        if extra in df:
            cols[extra] = "sum"
    g = df.set_index("timestamp").resample(rule, label="left", closed="left")
    out = g.agg(cols).dropna(subset=["open"]).reset_index()
    return out


class Chart:
    """One timeframe of one market: arrays of candles and indicators."""

    def __init__(self, df, tf, symbol="", market="crypto"):
        df = df.sort_values("timestamp").reset_index(drop=True)
        self.tf, self.symbol, self.market = tf, symbol, market
        self.n = len(df)
        self.t = pd.to_datetime(df["timestamp"]).to_numpy("datetime64[ns]")
        self.t_close = self.t + np.timedelta64(C.TF_SEC[tf], "s")
        o = self.o = df["open"].to_numpy(float)
        h = self.h = df["high"].to_numpy(float)
        lo = self.l = df["low"].to_numpy(float)
        c = self.c = df["close"].to_numpy(float)
        v = self.v = df["volume"].to_numpy(float) if "volume" in df else np.zeros(self.n)
        self.has_volume = bool(market == "crypto" and np.nansum(v) > 0)
        tb = df["taker_buy_volume"].to_numpy(float) if "taker_buy_volume" in df else None
        # traded value in USD (quote volume); without it volume x close is close enough
        qv = df["quote_volume"].to_numpy(float) if "quote_volume" in df else v * c
        per_day = max(1, int(round(86400 / C.TF_SEC[tf]))) if C.TF_SEC[tf] <= 86400 else 1
        self.usd_24h = pd.Series(qv).rolling(per_day, min_periods=1).sum().to_numpy(float) * \
            (1.0 if C.TF_SEC[tf] <= 86400 else 1 / 7)
        self.usd_rank = pd.Series(self.usd_24h).rolling(C.ATR_RANK_WINDOW, min_periods=100).rank(pct=True) \
            .to_numpy(float)

        pc = np.r_[c[0], c[:-1]]
        tr = np.maximum(h - lo, np.maximum(np.abs(h - pc), np.abs(lo - pc)))
        self.atr = _rma(tr, 14)
        self.atr = np.where(self.atr > 0, self.atr, np.nan)
        self.ema20, self.ema50, self.ema200 = _ema(c, 20), _ema(c, 50), _ema(c, 200)

        d = np.diff(c, prepend=c[0])
        up, dn = _rma(np.maximum(d, 0), 14), _rma(np.maximum(-d, 0), 14)
        with np.errstate(divide="ignore", invalid="ignore"):
            self.rsi = np.where(dn > 0, 100 - 100 / (1 + up / dn), 100.0)
        m12, m26 = _ema(c, 12), _ema(c, 26)
        self.macd = m12 - m26
        self.macd_sig = _ema(self.macd, 9)
        self.macd_hist = self.macd - self.macd_sig

        ll = pd.Series(lo).rolling(14, min_periods=1).min().to_numpy(float)
        hh = pd.Series(h).rolling(14, min_periods=1).max().to_numpy(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            k_fast = np.where(hh > ll, (c - ll) / (hh - ll) * 100, 50.0)
        self.stoch_k = _sma(k_fast, 3)
        self.stoch_d = _sma(self.stoch_k, 3)

        ph, pl = np.r_[h[0], h[:-1]], np.r_[lo[0], lo[:-1]]
        up_m, dn_m = h - ph, pl - lo
        pdm = np.where((up_m > dn_m) & (up_m > 0), up_m, 0.0)
        mdm = np.where((dn_m > up_m) & (dn_m > 0), dn_m, 0.0)
        atr_s = _rma(tr, 14)
        with np.errstate(divide="ignore", invalid="ignore"):
            self.pdi = np.where(atr_s > 0, 100 * _rma(pdm, 14) / atr_s, 0.0)
            self.mdi = np.where(atr_s > 0, 100 * _rma(mdm, 14) / atr_s, 0.0)
            dx = np.where(self.pdi + self.mdi > 0, 100 * np.abs(self.pdi - self.mdi) / (self.pdi + self.mdi), 0.0)
        self.adx = _rma(dx, 14)

        self.vol_sma = _sma(v, 20)
        with np.errstate(divide="ignore", invalid="ignore"):
            self.rvol = np.where(self.vol_sma > 0, v / self.vol_sma, 1.0)
        self.obv = np.cumsum(np.sign(d) * v)
        if tb is not None and self.has_volume:
            tb3 = pd.Series(tb).rolling(3, min_periods=1).sum().to_numpy(float)
            v3 = pd.Series(v).rolling(3, min_periods=1).sum().to_numpy(float)
            with np.errstate(divide="ignore", invalid="ignore"):
                self.taker = np.where(v3 > 0, tb3 / v3, 0.5)
        else:
            self.taker = None

        self.atr_rank = pd.Series(self.atr).rolling(C.ATR_RANK_WINDOW, min_periods=100).rank(pct=True).to_numpy(float)
        self.body = c - o
        self.rng = np.maximum(h - lo, 1e-12)
        self.upw = h - np.maximum(o, c)
        self.loww = np.minimum(o, c) - lo

        if C.TF_SEC[tf] < 86400:
            day = self.t.astype("datetime64[D]")
            tp = (h + lo + c) / 3
            pv = pd.Series(tp * np.maximum(v, 1e-12)).groupby(day).cumsum().to_numpy(float)
            vv = pd.Series(np.maximum(v, 1e-12)).groupby(day).cumsum().to_numpy(float)
            self.vwap = pv / vv
        else:
            self.vwap = None

    def a(self, i):
        """ATR at candle i (never zero)."""
        x = self.atr[i]
        return float(x) if x == x and x > 0 else float(max(self.rng[i], abs(self.c[i]) * 1e-4))


def align(htf, ltf):
    """For every candle of the entry chart (ltf): index of the last higher-timeframe candle that had CLOSED when the
    entry candle closed (-1 = none yet). This is what a trader sees: the running higher-timeframe candle is not
    finished, so it is not used."""
    return np.searchsorted(htf.t_close, ltf.t_close, side="right") - 1
