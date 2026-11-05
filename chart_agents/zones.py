"""Support / resistance agent: draws zones where price turned before, the way a trader does with rectangles.

* Swing highs and lows of the last ZONE_LOOKBACK candles are grouped by price: swings closer than ZONE_MERGE_ATR ATR
  make one zone. A zone needs ZONE_MIN_TOUCHES touches, or one swing with a strong reaction (price ran away from it).
* Strength grows with touches, with the size of the reaction, when the zone worked both as support and resistance
  ("flip"), when it is recent, and when it comes from a higher timeframe (higher-timeframe zones matter more).
* Extra levels every trader watches: previous day and previous week high / low, and round numbers.
* Whether a zone is support or resistance is simply where it is relative to price now.
* The agent also records breakouts: a close beyond a strong zone, for the breakout-and-retest setup.
"""
import math

from . import config as C


class Zone:
    __slots__ = ("lo", "hi", "touches", "flip", "reaction", "last", "strength", "level", "source", "prices")

    def __init__(self, lo, hi, touches=1, flip=False, reaction=0.0, last=0, strength=1.0, level=0, source="swing",
                 prices=None):
        self.lo, self.hi, self.touches, self.flip = lo, hi, touches, flip
        self.reaction, self.last, self.strength, self.level, self.source = reaction, last, strength, level, source
        self.prices = prices or [((lo + hi) / 2, reaction)]

    @property
    def mid(self):
        return (self.lo + self.hi) / 2

    def __repr__(self):
        return f"Zone[{self.lo:.6g}-{self.hi:.6g} x{self.touches} s{self.strength:.1f} L{self.level} {self.source}]"


def _reaction(ch, s, i, cache):
    """How far price ran away from a swing in the 20 candles after it (in ATR), using candles up to i only."""
    key = (s.kind, s.idx)
    r = cache.get(key) if cache is not None else None
    if r is not None:
        return r
    end = min(i, s.idx + 20) + 1
    aa = ch.a(s.idx)
    if s.kind == "L":
        r = float((ch.h[s.idx:end].max() - s.price) / aa)
    else:
        r = float((s.price - ch.l[s.idx:end].min()) / aa)
    if cache is not None and i >= s.idx + 20:      # final: no later candle can change it
        cache[key] = r
    return r


def build_zones(ch, minor, i, level=0, cache=None):
    """Zones from the swings known at candle i that price really turned from."""
    a = ch.a(i)
    start = i - C.ZONE_LOOKBACK
    pts = []
    for s in minor:
        if s.idx >= start and s.conf <= i:
            r = _reaction(ch, s, i, cache)
            if r >= C.ZONE_POINT_REACTION_ATR:
                pts.append((s, r))
    if not pts:
        return []
    pts.sort(key=lambda x: x[0].price)
    tol = C.ZONE_MERGE_ATR * a
    clusters, cur = [], [pts[0]]
    for x in pts[1:]:
        if x[0].price - cur[-1][0].price <= tol and x[0].price - cur[0][0].price <= 1.6 * tol:
            cur.append(x)
        else:
            clusters.append(cur)
            cur = [x]
    clusters.append(cur)
    out = []
    w = C.HTF_ZONE_WEIGHT.get(level, 1.0)
    for cl in clusters:
        reaction = max(r for _, r in cl)
        touches = len(cl)
        if touches < C.ZONE_MIN_TOUCHES and reaction < C.ZONE_MIN_REACTION_ATR:
            continue
        kinds = {s.kind for s, _ in cl}
        last = max(s.idx for s, _ in cl)
        age = (i - last) / C.ZONE_LOOKBACK
        strength = (0.6 * min(touches, 5) + (1.0 if len(kinds) == 2 else 0.0) + 0.3 * min(reaction, 5.0)
                    + 0.5 * max(0.0, 1 - age)) * w
        pad = C.ZONE_PAD_ATR * a
        out.append(Zone(min(s.price for s, _ in cl) - pad, max(s.price for s, _ in cl) + pad, touches,
                        len(kinds) == 2, reaction, last, strength, level, "swing", [(s.price, r) for s, r in cl]))
    out.sort(key=lambda z: -z.strength)
    keep, limit, gap = [], (C.MAX_ZONES if level == 0 else C.HTF_MAX_ZONES), C.ZONE_SPACING_ATR * a
    for z in out:
        if all(abs(z.mid - k.mid) >= gap for k in keep):
            keep.append(z)
            if len(keep) >= limit:
                break
    return keep


def on_entry_chart(z, a):
    """A higher-timeframe zone seen on the entry chart. A wide one (its timeframe's ATR is much bigger) is split into
    the swing prices it was made of, each a thin level of 0.3 entry-chart ATR - where price really turned."""
    if z.level == 0 or z.hi - z.lo <= 2.0 * a:
        return [z]
    ps = sorted(p for p, r in z.prices if r >= C.HTF_LEVEL_REACTION_ATR) or [max(z.prices, key=lambda x: x[1])[0]]
    groups = [[ps[0]]]
    for p in ps[1:]:
        if p - groups[-1][-1] <= 0.5 * a:
            groups[-1].append(p)
        else:
            groups.append([p])
    out = []
    for g in groups:
        lo, hi = min(g) - 0.3 * a, max(g) + 0.3 * a
        out.append(Zone(lo, hi, z.touches, z.flip, z.reaction, z.last, z.strength, z.level, "swing",
                        [(p, z.reaction) for p in g]))
    return out


def merge(zones):
    """One zone where several timeframes drew overlapping zones (a trader draws one rectangle, and marks it as
    stronger). Day / week levels and round numbers stay separate: they are confluence labels."""
    sw = sorted([z for z in zones if z.source == "swing"], key=lambda z: z.lo)
    rest = [z for z in zones if z.source != "swing"]
    out = []
    for z in sw:
        if out:
            m = out[-1]
            ov = min(m.hi, z.hi) - max(m.lo, z.lo)
            if ov > 0 and ov >= 0.5 * min(m.hi - m.lo, z.hi - z.lo):
                strong, weak = (m, z) if m.strength >= z.strength else (z, m)
                # the stronger zone keeps its edges (no chain of merges growing one huge zone)
                out[-1] = Zone(strong.lo, strong.hi, max(m.touches, z.touches), m.flip or z.flip,
                               max(m.reaction, z.reaction), max(m.last, z.last),
                               min(strong.strength + 0.3 * weak.strength, 14.0), max(m.level, z.level), "swing",
                               strong.prices)
                continue
        out.append(z)
    return out + rest


def significant(z):
    """A zone a trader treats as a wall for the target: tested twice and strong, a big reaction, a higher-timeframe
    zone, or the previous week's high / low. Minor zones and the previous day's levels are passed through."""
    if z.source == "swing":
        return z.level >= 1 or (z.touches >= 2 and z.strength >= 2.5) or z.reaction >= 3.0
    return z.source in ("pwh", "pwl")


def major(z):
    """A zone the target should never go past."""
    return (z.source == "swing" and (z.level >= 1 or z.strength >= 3.5)) or z.source in ("pwh", "pwl")


def round_levels(price, a):
    """Round numbers near price (e.g. 60,000 / 65,000 for BTC, 0.50 / 0.55 for XRP)."""
    if price <= 0:
        return []
    step = 10 ** math.floor(math.log10(price)) / 2
    base = math.floor(price / step) * step
    out = []
    for k in (-1, 0, 1, 2):
        lv = base + k * step
        if lv > 0:
            out.append(Zone(lv - 0.15 * a, lv + 0.15 * a, 1, False, 0.0, 0, 0.8, 0, "round"))
    return out


class ZoneAgent:
    name = "zones"

    def __init__(self, ch, swings, level=0):
        self.ch, self.sw, self.level = ch, swings, level
        self.zones = []
        self._n_minor = 0
        self.snap = [None] * ch.n          # zones known at each candle (same list object until it changes)
        self.breakouts = []                # dicts: side, lo, hi, bar, strength
        self._cache = {}

    def on_bar(self, i):
        if len(self.sw.minor) != self._n_minor or not self.zones:
            self._n_minor = len(self.sw.minor)
            self.zones = build_zones(self.ch, self.sw.minor, i, self.level, self._cache)
            if len(self._cache) > 3000:
                lim = i - C.ZONE_LOOKBACK - 50
                self._cache = {k: v for k, v in self._cache.items() if k[1] >= lim}
        self.snap[i] = self.zones
        if self.level == 0:
            self._breakouts(i)

    def _breakouts(self, i):
        ch = self.ch
        if i == 0:
            return
        a = ch.a(i)
        for z in self.zones:
            if z.strength < 2.0:
                continue
            if ch.c[i - 1] <= z.hi and ch.c[i] > z.hi + C.ZONE_BREAK_ATR * a:
                self.breakouts.append({"side": 1, "lo": z.lo, "hi": z.hi, "bar": i, "strength": z.strength,
                                       "rvol": float(ch.rvol[i])})
            elif ch.c[i - 1] >= z.lo and ch.c[i] < z.lo - C.ZONE_BREAK_ATR * a:
                self.breakouts.append({"side": -1, "lo": z.lo, "hi": z.hi, "bar": i, "strength": z.strength,
                                       "rvol": float(ch.rvol[i])})
        if self.breakouts and self.breakouts[0]["bar"] < i - C.RETEST_MAX_BARS:
            self.breakouts = [b for b in self.breakouts if b["bar"] >= i - C.RETEST_MAX_BARS]


def touching(zones, ch, i, side):
    """Zones the candle at i tested from the right side and closed back out of (support for longs)."""
    a = ch.a(i)
    out = []
    for z in zones:
        if side == 1:
            if ch.l[i] <= z.hi + 0.1 * a and ch.c[i] >= z.lo and z.mid <= ch.c[i] + 0.3 * a:
                out.append(z)
        else:
            if ch.h[i] >= z.lo - 0.1 * a and ch.c[i] <= z.hi and z.mid >= ch.c[i] - 0.3 * a:
                out.append(z)
    return out


def obstacles(zones, price, side):
    """Significant opposing zones in the way of the trade, nearest first (resistance above a long)."""
    if side == 1:
        zs = [z for z in zones if z.lo > price and significant(z)]
        zs.sort(key=lambda z: z.lo)
    else:
        zs = [z for z in zones if z.hi < price and significant(z)]
        zs.sort(key=lambda z: -z.hi)
    return zs


def opinion(zones_all, ch, i, side, tfname):
    """What the support / resistance agent says about a trade at candle i (zones_all: entry + higher-timeframe +
    day/week + round-number zones)."""
    t = touching(zones_all, ch, i, side)
    notes = []
    a = ch.a(i)
    feats = {"zone_strength": 0.0, "zone_touches": 0, "zone_htf": 0, "zone_flip": 0, "zone_round": 0, "zone_pd": 0,
             "room_atr": 10.0}
    if t:
        best = max(t, key=lambda z: z.strength)
        feats.update(zone_strength=min(best.strength, 8.0), zone_touches=min(best.touches, 6),
                     zone_htf=max(z.level for z in t), zone_flip=int(any(z.flip for z in t)),
                     zone_round=int(any(z.source == "round" for z in t)),
                     zone_pd=int(any(z.source in ("pdh", "pdl", "pwh", "pwl") for z in t)))
        score = min(1.0, 0.25 + best.strength / 6.0)
        role = "support" if side == 1 else "resistance"
        where = {0: tfname, 1: "higher-timeframe", 2: "higher-timeframe"}[best.level] if best.source == "swing" else \
            {"round": "round-number", "pdh": "previous day high", "pdl": "previous day low",
             "pwh": "previous week high", "pwl": "previous week low"}.get(best.source, "")
        txt = f"price tested a {where} {role} zone {fmt(best.lo)}-{fmt(best.hi)}"
        if best.touches >= 2:
            txt += f" that held {best.touches} times"
        if best.flip:
            txt += " (old " + ("resistance turned support" if side == 1 else "support turned resistance") + ")"
        notes.append(txt)
        extra = [z for z in t if z is not best and z.source != "swing"]
        if extra:
            notes.append("lines up with the " + ", ".join(sorted({_src(z) for z in extra})))
            score = min(1.0, score + 0.1)
    else:
        score = -0.2
        notes.append("price is not at a support / resistance zone" if side == 1 else
                     "price is not at a resistance / support zone")
    obs = obstacles(zones_all, ch.c[i], side)
    if obs:
        z = obs[0]
        room = ((z.lo - ch.c[i]) if side == 1 else (ch.c[i] - z.hi)) / a
        feats["room_atr"] = float(min(room, 10.0))
    return float(score), notes, feats


def _src(z):
    return {"round": "round number", "pdh": "previous day high", "pdl": "previous day low",
            "pwh": "previous week high", "pwl": "previous week low"}.get(z.source, "zone")


def fmt(x):
    """Price to a readable number of digits."""
    x = float(x)
    if x == 0:
        return "0"
    d = max(0, 5 - int(math.floor(math.log10(abs(x)))) - 1)
    d = min(d, 10)
    return f"{x:,.{d}f}"
