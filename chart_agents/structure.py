"""Market structure agent: is the market making higher highs and higher lows (uptrend), lower highs and lower lows
(downtrend), or neither?

Like a price-action trader it watches the last swing high and the last swing low:
* a close above the last swing high is a break of structure (BOS) when the trend was already up, and a change of
  character (CHoCH) when it was down - the first sign of a reversal. Mirror for closes below the last swing low.
* the trend is the direction of the last break. The number of breaks in a row is its strength.
* the dealing range (last swing low to last swing high) tells whether price is cheap (discount, lower half) or
  expensive (premium, upper half) inside the current leg.
* after a CHoCH the last opposite candle before the move is the order block: where the move started.
"""
import numpy as np


class StructureAgent:
    name = "structure"

    def __init__(self, ch, swings):
        self.ch, self.sw = ch, swings
        n = ch.n
        self.trend = np.zeros(n, np.int8)
        self.legs = np.zeros(n, np.int16)
        self.event = [None] * n          # ("BOS"|"CHOCH", +1|-1, level) on the candle of the break
        self.cur_trend, self.cur_legs = 0, 0
        self._broken = set()             # swings (by candle index) already broken
        self.last_choch = None           # dict: side, bar, level, ob=(lo, hi), origin
        self.last_bos = None

    def on_bar(self, i):
        ch = self.ch
        hs = self.sw.last("H")
        ls = self.sw.last("L")
        ev = None
        if hs:
            s = hs[0]
            key = ("H", s.idx)
            if key not in self._broken and s.conf <= i and ch.c[i] > s.price:
                self._broken.add(key)
                if self.cur_trend == 1:
                    ev = ("BOS", 1, s.price)
                    self.cur_legs += 1
                else:
                    ev = ("CHOCH", 1, s.price)
                    self.cur_trend, self.cur_legs = 1, 1
        if ls and ev is None:
            s = ls[0]
            key = ("L", s.idx)
            if key not in self._broken and s.conf <= i and ch.c[i] < s.price:
                self._broken.add(key)
                if self.cur_trend == -1:
                    ev = ("BOS", -1, s.price)
                    self.cur_legs += 1
                else:
                    ev = ("CHOCH", -1, s.price)
                    self.cur_trend, self.cur_legs = -1, 1
        if ev is not None:
            self.event[i] = ev
            info = {"side": ev[1], "bar": i, "level": ev[2]}
            info.update(self._order_block(i, ev[1], hs[0] if ev[1] == 1 and hs else (ls[0] if ls else None)))
            if ev[0] == "CHOCH":
                self.last_choch = info
            else:
                self.last_bos = info
        if self.cur_trend == 0 and len(self.sw.swings) >= 4:
            self.cur_trend = self._swing_trend()
            self.cur_legs = 1 if self.cur_trend else 0
        self.trend[i] = self.cur_trend
        self.legs[i] = self.cur_legs

    def _swing_trend(self):
        h = self.sw.last("H", 2)
        lo = self.sw.last("L", 2)
        if len(h) < 2 or len(lo) < 2:
            return 0
        if h[0].price > h[1].price and lo[0].price > lo[1].price:
            return 1
        if h[0].price < h[1].price and lo[0].price < lo[1].price:
            return -1
        return 0

    def _order_block(self, i, side, broken):
        """The last opposite candle before the move that broke structure (where big orders sat)."""
        ch = self.ch
        start = broken.idx if broken is not None else max(0, i - 30)
        seg = slice(start, i + 1)
        if side == 1:
            origin = start + int(np.argmin(ch.l[seg]))
            for j in range(origin, max(origin - 6, -1), -1):
                if ch.c[j] < ch.o[j]:
                    return {"origin": origin, "ob": (float(ch.l[j]), float(max(ch.o[j], ch.c[j]))),
                            "ob_low": float(ch.l[origin])}
            return {"origin": origin, "ob": (float(ch.l[origin]), float(max(ch.o[origin], ch.c[origin]))),
                    "ob_low": float(ch.l[origin])}
        origin = start + int(np.argmax(ch.h[seg]))
        for j in range(origin, max(origin - 6, -1), -1):
            if ch.c[j] > ch.o[j]:
                return {"origin": origin, "ob": (float(min(ch.o[j], ch.c[j])), float(ch.h[j])),
                        "ob_high": float(ch.h[origin])}
        return {"origin": origin, "ob": (float(min(ch.o[origin], ch.c[origin])), float(ch.h[origin])),
                "ob_high": float(ch.h[origin])}

    # ------------------------------------------------------------------ what the agent says about a trade idea
    def dealing_position(self, i):
        h = self.sw.last("H")
        lo = self.sw.last("L")
        if not h or not lo or h[0].price <= lo[0].price:
            return None
        return float((self.ch.c[i] - lo[0].price) / (h[0].price - lo[0].price))

    def pullback_depth(self, i, side):
        """How much of the last impulse leg the pullback has given back (0.38-0.62 is a healthy pullback)."""
        h = self.sw.last("H")
        lo = self.sw.last("L")
        if not h or not lo:
            return None
        hi, low = h[0].price, lo[0].price
        if hi <= low:
            return None
        if side == 1:
            return float((hi - self.ch.l[i]) / (hi - low))
        return float((self.ch.h[i] - low) / (hi - low))

    def opinion(self, i, side, tfname):
        t, legs = int(self.trend[i]), int(self.legs[i])
        notes = []
        if t == side:
            score = 0.55 + 0.1 * min(legs, 4)
            word = "uptrend (higher highs and higher lows)" if side == 1 else "downtrend (lower highs and lower lows)"
            notes.append(f"{tfname} structure: {word}, {legs} break{'s' if legs != 1 else ''} of structure in a row")
        elif t == -side:
            score = -0.9
            notes.append(f"{tfname} structure is against the trade ({'down' if side == 1 else 'up'}trend)")
        else:
            score = 0.0
            notes.append(f"{tfname} structure: no clear trend")
        ev = self.event[i]
        if ev and ev[1] == side:
            score += 0.2
            notes.append(("change of character" if ev[0] == "CHOCH" else "break of structure") + " on this candle")
        pos = self.dealing_position(i)
        if pos is not None:
            if (side == 1 and pos < 0.5) or (side == -1 and pos > 0.5):
                score += 0.15
                notes.append("price is in the " + ("discount" if side == 1 else "premium") + " half of the range")
            elif (side == 1 and pos > 0.85) or (side == -1 and pos < 0.15):
                score -= 0.2
                notes.append("price is already near the " + ("top" if side == 1 else "bottom") + " of the range")
        depth = self.pullback_depth(i, side)
        feats = {"trend": t * side, "legs": min(legs, 6) * (1 if t == side else 0),
                 "range_pos": (pos if pos is not None else 0.5) if side == 1 else (1 - pos if pos is not None else 0.5),
                 "pullback": depth if depth is not None else 0.5}
        return float(np.clip(score, -1, 1)), notes, feats
