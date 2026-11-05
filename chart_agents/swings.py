"""Swing highs and lows, the way a trader marks them on a chart.

* A pivot high at candle p has a higher high than the PIVOT_K candles on each side. It can only be known PIVOT_K
  candles later, so it is "confirmed" at candle p + PIVOT_K and used from then on.
* The zigzag keeps only swings that matter: highs and lows alternate, and a new swing must be at least ZIGZAG_ATR
  ATR away from the previous opposite one. A higher high after a high (with no real low in between) moves the swing.
"""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from . import config as C


class Swing:
    __slots__ = ("kind", "idx", "price", "conf")

    def __init__(self, kind, idx, price, conf):
        self.kind, self.idx, self.price, self.conf = kind, idx, price, conf

    def __repr__(self):
        return f"{self.kind}@{self.idx}:{self.price:.6g}"


def pivots(high, low, k):
    """Boolean arrays: is candle p a pivot high / pivot low with k candles on each side."""
    n = len(high)
    ph = np.zeros(n, bool)
    pl = np.zeros(n, bool)
    if n < 2 * k + 1:
        return ph, pl
    wh = sliding_window_view(high, 2 * k + 1)
    wl = sliding_window_view(low, 2 * k + 1)
    mid_h, mid_l = wh[:, k], wl[:, k]
    left_h, right_h = wh[:, :k].max(1), wh[:, k + 1:].max(1)
    left_l, right_l = wl[:, :k].min(1), wl[:, k + 1:].min(1)
    ph[k:n - k] = (mid_h > left_h) & (mid_h >= right_h)
    pl[k:n - k] = (mid_l < left_l) & (mid_l <= right_l)
    return ph, pl


class SwingTracker:
    """Major swings (zigzag) and minor pivots, updated candle by candle."""

    def __init__(self, ch):
        self.ch = ch
        self.ph, self.pl = pivots(ch.h, ch.l, C.PIVOT_K)
        self.mph, self.mpl = pivots(ch.h, ch.l, C.MINOR_PIVOT_K)
        self.swings = []          # alternating H / L (major)
        self.minor = []           # every minor pivot, in confirmation order
        self.changed = False      # True on candles where the major swings changed

    def on_bar(self, i):
        """Take in the pivots confirmed at candle i. Returns True when the major swings changed."""
        ch = self.ch
        self.changed = False
        k = C.PIVOT_K
        p = i - k
        if p >= 0:
            evs = []
            if self.ph[p]:
                evs.append(Swing("H", p, ch.h[p], i))
            if self.pl[p]:
                evs.append(Swing("L", p, ch.l[p], i))
            if len(evs) == 2 and ch.c[p] < ch.o[p]:
                evs.reverse()                 # a red outside candle: the high came first
            for s in evs:
                self._add(s, i)
        mk = C.MINOR_PIVOT_K
        mp = i - mk
        if mp >= 0:
            if self.mph[mp]:
                self.minor.append(Swing("H", mp, ch.h[mp], i))
            if self.mpl[mp]:
                self.minor.append(Swing("L", mp, ch.l[mp], i))
            if len(self.minor) > 400:
                del self.minor[:100]
        return self.changed

    def _add(self, s, i):
        sw = self.swings
        if not sw:
            sw.append(s)
            self.changed = True
            return
        last = sw[-1]
        if s.kind == last.kind:
            if (s.kind == "H" and s.price > last.price) or (s.kind == "L" and s.price < last.price):
                sw[-1] = s
                self.changed = True
            return
        if abs(s.price - last.price) >= C.ZIGZAG_ATR * self.ch.a(i):
            sw.append(s)
            self.changed = True
            if len(sw) > 60:
                del sw[:20]

    # ------------------------------------------------------------------ helpers
    def last(self, kind, n=1):
        out = [s for s in reversed(self.swings) if s.kind == kind][:n]
        return out

    def recent(self, n):
        return self.swings[-n:]
