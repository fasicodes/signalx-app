"""Candlestick agent: reads the last 1-3 candles like a trader does at a level.

Bullish: engulfing, hammer / pin bar, morning star, piercing line, tweezer bottom, inside-bar breakout,
three white soldiers, bullish marubozu. Bearish: the mirror images (engulfing, shooting star, evening star,
dark cloud cover, tweezer top, inside-bar breakdown, three black crows, bearish marubozu). A doji is indecision.
A candle pattern matters most at a support / resistance zone; the head trader learns how much.
"""
import numpy as np

STRENGTH = {"morning star": 0.9, "evening star": 0.9, "bullish engulfing": 0.8, "bearish engulfing": 0.8,
            "hammer": 0.7, "shooting star": 0.7, "piercing line": 0.5, "dark cloud cover": 0.5,
            "tweezer bottom": 0.5, "tweezer top": 0.5, "inside-bar breakout": 0.5, "inside-bar breakdown": 0.5,
            "three white soldiers": 0.6, "three black crows": 0.6, "bullish marubozu": 0.5,
            "bearish marubozu": 0.5}


def _sh(x, k, fill=np.nan):
    out = np.empty_like(x)
    out[:k] = fill
    out[k:] = x[:-k]
    return out


class CandleAgent:
    name = "candles"

    def __init__(self, ch):
        o, h, lo, c, a = ch.o, ch.h, ch.l, ch.c, np.nan_to_num(ch.atr, nan=np.nanmean(ch.rng))
        body, rng, upw, loww = c - o, ch.rng, ch.upw, ch.loww
        ab = np.abs(body)
        o1, c1, h1, l1, b1 = _sh(o, 1), _sh(c, 1), _sh(h, 1), _sh(lo, 1), _sh(body, 1)
        o2, c2, h2, l2, b2 = _sh(o, 2), _sh(c, 2), _sh(h, 2), _sh(lo, 2), _sh(body, 2)
        rng1 = _sh(rng, 1)
        green, red = c > o, c < o
        with np.errstate(invalid="ignore"):
            pats = {
                "bullish engulfing": (b1 < 0) & green & (c >= o1) & (o <= c1) & (ab >= 0.3 * a) & (ab >= np.abs(b1)),
                "bearish engulfing": (b1 > 0) & red & (c <= o1) & (o >= c1) & (ab >= 0.3 * a) & (ab >= np.abs(b1)),
                "hammer": (loww >= 2 * ab) & (loww >= 0.55 * rng) & (upw <= 0.25 * rng) & (rng >= 0.6 * a),
                "shooting star": (upw >= 2 * ab) & (upw >= 0.55 * rng) & (loww <= 0.25 * rng) & (rng >= 0.6 * a),
                "morning star": (b2 < 0) & (np.abs(b2) >= 0.6 * a) & (np.abs(b1) <= 0.35 * np.maximum(rng1, 1e-12))
                                & green & (c >= (o2 + c2) / 2),
                "evening star": (b2 > 0) & (np.abs(b2) >= 0.6 * a) & (np.abs(b1) <= 0.35 * np.maximum(rng1, 1e-12))
                                & red & (c <= (o2 + c2) / 2),
                "piercing line": (b1 < 0) & (np.abs(b1) >= 0.6 * a) & green & (c > (o1 + c1) / 2) & (c < o1),
                "dark cloud cover": (b1 > 0) & (np.abs(b1) >= 0.6 * a) & red & (c < (o1 + c1) / 2) & (c > o1),
                "tweezer bottom": (b1 < 0) & green & (np.abs(lo - l1) <= 0.1 * a) & (ab >= 0.2 * a),
                "tweezer top": (b1 > 0) & red & (np.abs(h - h1) <= 0.1 * a) & (ab >= 0.2 * a),
                "inside-bar breakout": (h1 < h2) & (l1 > l2) & (c > h1) & green,
                "inside-bar breakdown": (h1 < h2) & (l1 > l2) & (c < l1) & red,
                "three white soldiers": green & (b1 > 0) & (b2 > 0) & (c > c1) & (c1 > c2)
                                        & (ab >= 0.5 * a) & (np.abs(b1) >= 0.5 * a) & (np.abs(b2) >= 0.5 * a)
                                        & (upw <= 0.3 * rng),
                "three black crows": red & (b1 < 0) & (b2 < 0) & (c < c1) & (c1 < c2)
                                     & (ab >= 0.5 * a) & (np.abs(b1) >= 0.5 * a) & (np.abs(b2) >= 0.5 * a)
                                     & (loww <= 0.3 * rng),
                "bullish marubozu": green & (ab >= 0.85 * rng) & (rng >= 1.2 * a),
                "bearish marubozu": red & (ab >= 0.85 * rng) & (rng >= 1.2 * a),
            }
        bull = ["bullish engulfing", "hammer", "morning star", "piercing line", "tweezer bottom",
                "inside-bar breakout", "three white soldiers", "bullish marubozu"]
        bear = ["bearish engulfing", "shooting star", "evening star", "dark cloud cover", "tweezer top",
                "inside-bar breakdown", "three black crows", "bearish marubozu"]
        n = ch.n
        self.bull_score, self.bear_score = np.zeros(n), np.zeros(n)
        self.bull_name, self.bear_name = [None] * n, [None] * n
        for nm in bull:
            m = np.nan_to_num(pats[nm]).astype(bool)
            better = m & (STRENGTH[nm] > self.bull_score)
            self.bull_score[better] = STRENGTH[nm]
            for j in np.flatnonzero(better):
                self.bull_name[j] = nm
        for nm in bear:
            m = np.nan_to_num(pats[nm]).astype(bool)
            better = m & (STRENGTH[nm] > self.bear_score)
            self.bear_score[better] = STRENGTH[nm]
            for j in np.flatnonzero(better):
                self.bear_name[j] = nm
        self.doji = ab <= 0.1 * rng
        # a candle that rejected a level: long wick in the trade's direction, close in the far third
        self.bull_reject = (loww >= 0.45 * rng) & (c >= lo + 0.6 * rng)
        self.bear_reject = (upw >= 0.45 * rng) & (c <= h - 0.6 * rng)

    def trigger(self, i, side):
        """True when candle i is a bullish (side 1) / bearish (-1) trigger candle."""
        if side == 1:
            return self.bull_score[i] > 0 or bool(self.bull_reject[i])
        return self.bear_score[i] > 0 or bool(self.bear_reject[i])

    def opinion(self, i, side):
        s = self.bull_score[i] if side == 1 else self.bear_score[i]
        nm = self.bull_name[i] if side == 1 else self.bear_name[i]
        opp = self.bear_score[i] if side == 1 else self.bull_score[i]
        rej = bool(self.bull_reject[i] if side == 1 else self.bear_reject[i])
        notes = []
        score = float(s)
        if nm:
            notes.append(f"{nm} candle")
        elif rej:
            score = 0.4
            notes.append("rejection candle (long " + ("lower" if side == 1 else "upper") + " wick)")
        if opp >= 0.5:
            score -= 0.4
            notes.append("the same candle also looks " + ("bearish" if side == 1 else "bullish"))
        if self.doji[i]:
            score -= 0.2
            notes.append("doji: the market is undecided")
        feats = {"candle": score, "candle_reject": int(rej)}
        return float(np.clip(score, -1, 1)), notes, feats
