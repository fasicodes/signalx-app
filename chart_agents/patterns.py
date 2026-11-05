"""Chart pattern agent: finds the classic patterns on the swing highs and lows and watches for their breakout.

Reversal: double bottom / top, triple bottom / top, head and shoulders, inverse head and shoulders.
Continuation / either way: ascending, descending and symmetric triangles, rising and falling wedges, flags,
rectangles (ranges) and channels.

A pattern is only "complete" once its last swing is confirmed, and it only becomes a trade when a candle CLOSES
beyond its breakout line (neckline, triangle side, range edge) within PATTERN_BREAKOUT_WAIT candles. The target is the
usual measured move (the pattern's height from the breakout); the idea is wrong (the stop) beyond the pattern's last
swing. Ranges and channels are also used for trades off their edges (see setups.py).
"""
import numpy as np

from . import config as C
from .zones import fmt


def _line(points):
    """Least-squares line through (candle index, price) points: (slope, intercept, worst residual)."""
    x = np.array([p[0] for p in points], float)
    y = np.array([p[1] for p in points], float)
    if len(x) < 2 or x.max() == x.min():
        return 0.0, float(y.mean()), 0.0
    m, b = np.polyfit(x, y, 1)
    res = float(np.abs(y - (m * x + b)).max())
    return float(m), float(b), res


class Pattern:
    __slots__ = ("name", "side", "m", "b", "invalid", "height", "formed", "expire", "quality", "desc", "done",
                 "start")

    def __init__(self, name, side, m, b, invalid, height, formed, quality, desc, start):
        self.name, self.side, self.m, self.b, self.invalid = name, side, m, b, invalid
        self.height, self.formed, self.quality, self.desc = height, formed, quality, desc
        self.expire = formed + C.PATTERN_BREAKOUT_WAIT
        self.done = False
        self.start = start

    def level(self, i):
        return self.m * i + self.b

    def __repr__(self):
        return f"{self.name}({'+' if self.side > 0 else '-'}) lvl={self.level(self.formed):.6g}"


class PatternAgent:
    name = "patterns"

    def __init__(self, ch, swings):
        self.ch, self.sw = ch, swings
        self.armed = []            # patterns waiting for their breakout
        self.fired = {}            # candle -> list of patterns that broke out on it
        self.range = None          # current rectangle: dict(top, bottom, since)
        self.channel = None        # current channel: dict(m, b_hi, b_lo, side, since)
        self.poles = []            # flag poles waiting for a consolidation breakout
        self._seen = set()

    # ------------------------------------------------------------------ detection on new swings
    def on_swings(self, i):
        sw = self.sw.swings
        if len(sw) < 3:
            return
        a = self.ch.a(i)
        self._double_triple(sw, i, a)
        self._head_shoulders(sw, i, a)
        self._lines(sw, i, a)
        self._pole(sw, i, a)

    def _arm(self, p):
        key = (p.name, p.side, round(p.b, 10), p.start)
        if key in self._seen:
            return
        self._seen.add(key)
        if len(self._seen) > 5000:
            self._seen = set(list(self._seen)[-2000:])
        # price already beyond the line when the pattern became visible: the move was missed
        c = self.ch.c[p.formed]
        if (p.side == 1 and c > p.level(p.formed)) or (p.side == -1 and c < p.level(p.formed)):
            return
        self.armed.append(p)

    def _double_triple(self, sw, i, a):
        tol = C.PATTERN_TOL_ATR * a
        s1 = sw[-1]
        for side, kind in ((1, "L"), (-1, "H")):
            if s1.kind != kind or len(sw) < 3:
                continue
            x1, mid, x2 = sw[-3], sw[-2], sw[-1]
            if x2.idx - x1.idx > C.PATTERN_MAX_BARS or x2.idx - x1.idx < 6:
                continue
            if abs(x2.price - x1.price) <= tol:
                extreme = min(x1.price, x2.price) if side == 1 else max(x1.price, x2.price)
                height = (mid.price - extreme) * side
                if height >= C.PATTERN_MIN_HEIGHT_ATR * a:
                    name = "double bottom" if side == 1 else "double top"
                    q = 1.0 - abs(x2.price - x1.price) / tol * 0.4
                    prior = sw[-4] if len(sw) >= 4 else None
                    if prior is not None and (prior.price - mid.price) * side > 0:
                        q += 0.15       # it came after a move the other way: a real reversal spot
                    self._arm(Pattern(name, side, 0.0, mid.price, extreme, height, i, q,
                                      f"{name} at {fmt(extreme)}, neckline {fmt(mid.price)}", x1.idx))
            if len(sw) >= 5:
                y1, m1, y2, m2, y3 = sw[-5:]
                if y3.idx - y1.idx <= C.PATTERN_MAX_BARS:
                    ps = [y1.price, y2.price, y3.price]
                    if max(ps) - min(ps) <= tol:
                        neck = max(m1.price, m2.price) if side == 1 else min(m1.price, m2.price)
                        extreme = min(ps) if side == 1 else max(ps)
                        height = (neck - extreme) * side
                        if height >= C.PATTERN_MIN_HEIGHT_ATR * a:
                            name = "triple bottom" if side == 1 else "triple top"
                            self._arm(Pattern(name, side, 0.0, neck, extreme, height, i, 1.1,
                                              f"{name} at {fmt(extreme)}, neckline {fmt(neck)}", y1.idx))

    def _head_shoulders(self, sw, i, a):
        if len(sw) < 5:
            return
        s1, n1, hd, n2, s2 = sw[-5:]
        if s2.idx - s1.idx > C.PATTERN_MAX_BARS:
            return
        tol = C.PATTERN_TOL_ATR * a * 1.5
        if s2.kind == "L":       # inverse head and shoulders (bullish)
            side, name = 1, "inverse head and shoulders"
            ok = hd.price < s1.price - 0.5 * a and hd.price < s2.price - 0.5 * a
        else:
            side, name = -1, "head and shoulders"
            ok = hd.price > s1.price + 0.5 * a and hd.price > s2.price + 0.5 * a
        if not ok or abs(s1.price - s2.price) > tol:
            return
        m = (n2.price - n1.price) / max(n2.idx - n1.idx, 1)
        b = n1.price - m * n1.idx
        neck_at_head = m * hd.idx + b
        height = (neck_at_head - hd.price) * side
        if height < C.PATTERN_MIN_HEIGHT_ATR * a:
            return
        if abs(m) * (s2.idx - s1.idx) > 2.5 * height:       # neckline too steep to be a real H&S
            return
        q = 1.15 - abs(s1.price - s2.price) / tol * 0.3
        self._arm(Pattern(name, side, m, b, s2.price, height, i, q,
                          f"{name}, head {fmt(hd.price)}, neckline {fmt(neck_at_head)}", s1.idx))

    def _lines(self, sw, i, a):
        """Triangles, wedges, rectangles and channels from the last 5-6 swings."""
        pts = sw[-6:] if len(sw) >= 6 else sw[-5:]
        if len(pts) < 5 or pts[-1].idx - pts[0].idx > C.PATTERN_MAX_BARS:
            self.range = self.range if self.range and self.range["since"] > i - C.PATTERN_MAX_BARS else None
            return
        hs = [(s.idx, s.price) for s in pts if s.kind == "H"]
        ls = [(s.idx, s.price) for s in pts if s.kind == "L"]
        if len(hs) < 2 or len(ls) < 2:
            return
        mh, bh, rh = _line(hs)
        ml, bl, rl = _line(ls)
        if rh > 0.6 * a or rl > 0.6 * a:
            return
        x0, x1 = pts[0].idx, pts[-1].idx
        span = max(x1 - x0, 1)
        flat_h = abs(mh) * span <= 0.6 * a
        flat_l = abs(ml) * span <= 0.6 * a
        top0, bot0 = mh * x0 + bh, ml * x0 + bl
        top1, bot1 = mh * x1 + bh, ml * x1 + bl
        h0, h1 = top0 - bot0, top1 - bot1
        if h0 <= 0 or h1 <= 0:
            return
        height = max(h0, h1)
        if height < C.PATTERN_MIN_HEIGHT_ATR * a:
            return
        last_low = min(p[1] for p in ls[-2:])
        last_high = max(p[1] for p in hs[-2:])
        conv = h1 < 0.75 * h0
        q = 1.0 - (rh + rl) / (1.2 * a) * 0.3
        if flat_h and flat_l:
            self.range = {"top": float(np.mean([p[1] for p in hs])), "bottom": float(np.mean([p[1] for p in ls])),
                          "since": x0, "seen": i}
            self._arm(Pattern("range breakout", 1, 0.0, self.range["top"], last_low, height, i, q,
                              f"range {fmt(self.range['bottom'])}-{fmt(self.range['top'])}", x0))
            self._arm(Pattern("range breakdown", -1, 0.0, self.range["bottom"], last_high, height, i, q,
                              f"range {fmt(self.range['bottom'])}-{fmt(self.range['top'])}", x0))
            return
        if flat_h and ml > 0 and conv:
            self._arm(Pattern("ascending triangle", 1, mh, bh, last_low, h0, i, q + 0.1,
                              f"ascending triangle, flat top {fmt(top1)}", x0))
        elif flat_l and mh < 0 and conv:
            self._arm(Pattern("descending triangle", -1, ml, bl, last_high, h0, i, q + 0.1,
                              f"descending triangle, flat bottom {fmt(bot1)}", x0))
        elif mh < 0 < ml and conv:
            self._arm(Pattern("symmetric triangle", 1, mh, bh, last_low, h0, i, q,
                              f"symmetric triangle, upper side near {fmt(top1)}", x0))
            self._arm(Pattern("symmetric triangle", -1, ml, bl, last_high, h0, i, q,
                              f"symmetric triangle, lower side near {fmt(bot1)}", x0))
        elif mh > 0 and ml > 0 and conv:
            self._arm(Pattern("rising wedge", -1, ml, bl, last_high, h0, i, q,
                              f"rising wedge, lower side near {fmt(bot1)}", x0))
        elif mh < 0 and ml < 0 and conv:
            self._arm(Pattern("falling wedge", 1, mh, bh, last_low, h0, i, q,
                              f"falling wedge, upper side near {fmt(top1)}", x0))
        elif (mh > 0 and ml > 0) or (mh < 0 and ml < 0):
            if abs(h1 - h0) <= 0.25 * h0:
                side = 1 if mh > 0 else -1
                self.channel = {"m": (mh + ml) / 2, "b_hi": bh, "b_lo": bl, "mh": mh, "ml": ml, "side": side,
                                "since": x0, "seen": i, "height": height}

    def _pole(self, sw, i, a):
        """A flag pole: a strong, fast leg between the last two swings."""
        x, y = sw[-2], sw[-1]
        move = (y.price - x.price)
        bars = y.idx - x.idx
        if bars <= C.FLAG_POLE_MAX_BARS and abs(move) >= C.FLAG_POLE_ATR * a:
            side = 1 if move > 0 else -1
            self.poles.append({"side": side, "start": x.idx, "top": y.idx, "tip": y.price, "base": x.price,
                               "height": abs(move), "seen": i})
            self.poles = self.poles[-4:]

    # ------------------------------------------------------------------ every candle
    def on_bar(self, i):
        ch = self.ch
        a = ch.a(i)
        fired = []
        keep = []
        for p in self.armed:
            if i <= p.formed:
                keep.append(p)
                continue
            if i > p.expire:
                continue
            lvl = p.level(i)
            if (p.side == 1 and ch.c[i] < p.invalid) or (p.side == -1 and ch.c[i] > p.invalid):
                continue                                   # the pattern failed before breaking out
            if (p.side == 1 and ch.c[i] > lvl + 0.1 * a and ch.c[i] > ch.o[i]) or \
               (p.side == -1 and ch.c[i] < lvl - 0.1 * a and ch.c[i] < ch.o[i]):
                fired.append(p)
                continue
            keep.append(p)
        self.armed = keep[-30:]
        fired += self._flags(i, a)
        if fired:
            self.fired[i] = fired
        if self.range and i - self.range["seen"] > C.PATTERN_MAX_BARS:
            self.range = None
        if self.channel and i - self.channel["seen"] > C.PATTERN_MAX_BARS // 2:
            self.channel = None
        if self.range and (ch.c[i] > self.range["top"] + 1.5 * a or ch.c[i] < self.range["bottom"] - 1.5 * a):
            self.range = None
        return fired

    def _flags(self, i, a):
        ch = self.ch
        out = []
        keep = []
        for pole in self.poles:
            top, side = pole["top"], pole["side"]
            n_cons = i - top - 1                       # consolidation candles before this one
            if n_cons > C.FLAG_MAX_BARS or i <= pole["seen"]:
                if n_cons <= C.FLAG_MAX_BARS:
                    keep.append(pole)
                continue
            seg = slice(top + 1, i)
            if n_cons < 3:
                keep.append(pole)
                continue
            if side == 1:
                cons_hi, cons_lo = ch.h[seg].max(), ch.l[seg].min()
                retrace = (pole["tip"] - cons_lo) / pole["height"]
                if retrace > C.FLAG_MAX_RETRACE:
                    continue
                if ch.c[i] > cons_hi + 0.1 * a and ch.c[i] > ch.o[i]:
                    out.append(Pattern("bull flag", 1, 0.0, cons_hi, cons_lo, pole["height"], i - 1, 1.0,
                                       f"bull flag after a {pole['height'] / a:.1f} ATR rally", pole["start"]))
                    continue
            else:
                cons_hi, cons_lo = ch.h[seg].max(), ch.l[seg].min()
                retrace = (cons_hi - pole["tip"]) / pole["height"]
                if retrace > C.FLAG_MAX_RETRACE:
                    continue
                if ch.c[i] < cons_lo - 0.1 * a and ch.c[i] < ch.o[i]:
                    out.append(Pattern("bear flag", -1, 0.0, cons_lo, cons_hi, pole["height"], i - 1, 1.0,
                                       f"bear flag after a {pole['height'] / a:.1f} ATR drop", pole["start"]))
                    continue
            keep.append(pole)
        self.poles = keep
        return out

    # ------------------------------------------------------------------ opinion
    def opinion(self, i, side):
        notes = []
        score = 0.0
        q = 0.0
        name = None
        for p in self.fired.get(i, []):
            if p.side == side:
                q = max(q, p.quality)
                name = p.name
                notes.append(f"breakout from a {p.desc}")
        if q:
            score = min(1.0, 0.5 + 0.4 * q)
        against = [p for p in self.armed if p.side == -side and p.formed < i]
        if against:
            score -= 0.25
            notes.append(f"a {against[-1].name} is forming against the trade")
        feats = {"pattern_q": q, "pattern_against": int(bool(against))}
        return float(np.clip(score, -1, 1)), notes, feats, name
