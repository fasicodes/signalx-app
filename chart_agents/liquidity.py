"""Liquidity agent. Two kinds of liquidity matter to a trader:

1. How liquid the market is. Crypto: the USD value traded in the last 24 hours. A thin market means slippage and
   fake moves; a market whose volume has dried up compared with its own normal is often a trap.
2. Where the stops are (smart-money liquidity).
   * Buy-side liquidity rests above swing highs, sell-side below swing lows. Equal highs / lows (two or more swings
     at the same price) and the previous day / week high and low are the biggest pools.
   * A sweep (stop hunt): price takes out a pool and closes back on the other side - the big players filled their
     orders against the trapped stops. A sell-side sweep is bullish, a buy-side sweep bearish.
   * Draw on liquidity: an untouched pool in the trade's direction is where price tends to go next (a target).
   * A stop placed right in front of a pool gets hunted: the stop should go beyond the pool.
   * Fair value gap (imbalance): a three-candle gap that price moved through too fast. Unfilled gaps act as
     support / resistance and get filled later.
"""
import numpy as np

from . import config as C
from .zones import fmt

THIN_USD = 2_000_000          # under $2M traded in 24 hours: too thin to trade
LOW_USD = 10_000_000          # under $10M: thin, expect slippage
POOL_LOOKBACK = 300
EQUAL_ATR = 0.25
FVG_MIN_ATR = 0.25
FVG_MAX_AGE = 200


class LiquidityAgent:
    name = "liquidity"

    def __init__(self, ch, swings):
        self.ch, self.sw = ch, swings
        self.fvgs = []               # dicts: side, lo, hi, bar
        self.levels = []             # extra pools from the engine: (price, kind 'H'|'L', name)
        self.intact = {"L": [], "H": []}     # untouched swing prices: [price, candle]
        self.swept = {}              # candle -> {1: pool, -1: pool} swept on that candle

    # ------------------------------------------------------------------ every candle
    def on_bar(self, i):
        ch = self.ch
        for s in reversed(self.sw.minor):
            if s.conf < i:
                break
            if s.conf == i:
                self.intact[s.kind].append([s.price, s.idx])
        a = ch.a(i)
        res = {}
        for side, kind in ((1, "L"), (-1, "H")):
            best = None
            for price, size, name in self._cluster(kind, a) + [(p, 2, nm) for p, k, nm in self.levels if k == kind]:
                if side == 1:
                    poke = price - ch.l[i]
                    ok = 0.05 * a < poke <= C.SWEEP_MAX_ATR * a and ch.c[i] > price
                else:
                    poke = ch.h[i] - price
                    ok = 0.05 * a < poke <= C.SWEEP_MAX_ATR * a and ch.c[i] < price
                if ok and (best is None or size > best[1]):
                    best = (price, size, name)
            if best:
                res[side] = best
        if res:
            self.swept[i] = res
            if len(self.swept) > 50:
                for k in sorted(self.swept)[:-20]:
                    del self.swept[k]
        start = i - POOL_LOOKBACK
        self.intact["L"] = [x for x in self.intact["L"] if ch.l[i] >= x[0] and x[1] >= start]
        self.intact["H"] = [x for x in self.intact["H"] if ch.h[i] <= x[0] and x[1] >= start]
        if i >= 2:
            a = ch.a(i)
            if ch.l[i] - ch.h[i - 2] >= FVG_MIN_ATR * a and ch.c[i - 1] > ch.o[i - 1]:
                self.fvgs.append({"side": 1, "lo": float(ch.h[i - 2]), "hi": float(ch.l[i]), "bar": i})
            elif ch.l[i - 2] - ch.h[i] >= FVG_MIN_ATR * a and ch.c[i - 1] < ch.o[i - 1]:
                self.fvgs.append({"side": -1, "lo": float(ch.h[i]), "hi": float(ch.l[i - 2]), "bar": i})
        keep = []
        for g in self.fvgs:
            if i - g["bar"] > FVG_MAX_AGE:
                continue
            if i > g["bar"] and ((g["side"] == 1 and ch.l[i] <= g["lo"]) or (g["side"] == -1 and ch.h[i] >= g["hi"])):
                continue                     # filled
            keep.append(g)
        self.fvgs = keep[-40:]

    # ------------------------------------------------------------------ pools
    def _cluster(self, kind, a):
        raw = sorted(x[0] for x in self.intact[kind])
        out = []
        for p in raw:
            if out and abs(p - out[-1][0]) <= EQUAL_ATR * a:
                q, n, _ = out[-1]
                out[-1] = ((q * n + p) / (n + 1), n + 1, "equal " + ("lows" if kind == "L" else "highs"))
            else:
                out.append((p, 1, "swing " + ("low" if kind == "L" else "high")))
        return out

    def pools(self, i, kind):
        """Untouched pools after candle i: kind 'L' = sell-side (below swing lows), 'H' = buy-side (above swing
        highs). Returns [(price, size, name)] - size = how many swings sit at that price."""
        out = self._cluster(kind, self.ch.a(i))
        for price, k, name in self.levels:
            if k == kind:
                out.append((price, 2, name))
        return out

    def sweep(self, i, side):
        """The pool candle i swept and closed back from (side 1: a sell-side sweep, bullish), or None."""
        return self.swept.get(i, {}).get(side)

    def draw(self, i, side, beyond):
        """Nearest untouched pool in the trade's direction (above `beyond` for longs): where price is drawn to."""
        pools = self.pools(i, "H" if side == 1 else "L")
        if side == 1:
            c = [p for p in pools if p[0] > beyond]
            return min(c, key=lambda p: p[0]) if c else None
        c = [p for p in pools if p[0] < beyond]
        return max(c, key=lambda p: p[0]) if c else None

    # ------------------------------------------------------------------ opinion
    def opinion(self, i, side, stop=None):
        ch = self.ch
        a = ch.a(i)
        notes, veto = [], None
        score = 0.0
        feats = {"usd_log": 0.0, "usd_rank": 0.5, "sweep_for": 0, "sweep_against": 0, "pool_at_stop": 0,
                 "fvg_for": 0, "fvg_against": 0}
        if ch.market == "crypto" and ch.has_volume:
            usd = float(ch.usd_24h[i])
            feats["usd_log"] = float(np.log10(max(usd, 1.0)))
            rk = ch.usd_rank[i]
            feats["usd_rank"] = float(rk) if rk == rk else 0.5
            if usd < THIN_USD:
                veto = f"too thin: only ${usd / 1e6:.1f}M traded in 24 hours"
            elif usd < LOW_USD:
                score -= 0.3
                notes.append(f"thin market (${usd / 1e6:.1f}M in 24 hours): expect slippage")
            if rk == rk and rk < 0.1:
                score -= 0.2
                notes.append("volume has dried up compared with normal")
        sw = self.sweep(i, side)
        if sw:
            score += 0.5
            feats["sweep_for"] = 1
            where = "under" if side == 1 else "above"
            notes.append(f"took the {'sell' if side == 1 else 'buy'}-side liquidity {where} the {sw[2]} at "
                         f"{fmt(sw[0])} and closed back: a stop hunt")
        if self.sweep(i, -side):
            score -= 0.4
            feats["sweep_against"] = 1
            notes.append("a stop hunt just happened against the trade")
        if stop is not None:
            kind = "L" if side == 1 else "H"
            for price, size, name in self.pools(i, kind):
                gap = (stop - price) * side           # > 0: the pool is beyond the stop
                if 0 <= gap <= 0.6 * a or (side == 1 and price < ch.c[i] and price > stop and size >= 2) or \
                   (side == -1 and price > ch.c[i] and price < stop and size >= 2):
                    feats["pool_at_stop"] = 1
                    score -= 0.15
                    notes.append(f"{name} at {fmt(price)} sit next to the stop: a stop hunt could reach it")
                    break
        for g in self.fvgs:
            if g["bar"] >= i:
                continue
            if g["side"] == side and g["lo"] - 0.2 * a <= (ch.l[i] if side == 1 else ch.h[i]) <= g["hi"] + 0.2 * a:
                score += 0.2
                feats["fvg_for"] = 1
                notes.append(f"price filled a {'bullish' if side == 1 else 'bearish'} fair value gap "
                             f"{fmt(g['lo'])}-{fmt(g['hi'])}")
                break
        for g in self.fvgs:
            if g["side"] == -side and g["bar"] < i:
                dist = ((g["lo"] - ch.c[i]) if side == 1 else (ch.c[i] - g["hi"])) / a
                if 0 <= dist <= 1.0:
                    score -= 0.15
                    feats["fvg_against"] = 1
                    notes.append("an unfilled fair value gap right in the way")
                    break
        return float(np.clip(score, -1, 1)), notes, feats, veto
