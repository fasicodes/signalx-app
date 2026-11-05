"""Risk manager agent: where the stop goes, where the target goes, whether the trade is worth it after costs, and the
portfolio rules (how many trades at once).

* Stop: beyond the structure that proves the idea wrong (the zone, the sweep wick, the pattern's last swing) plus
  STOP_BUFFER_ATR. If a liquidity pool sits right next to it, the stop goes beyond the pool. Too tight (under
  MIN_STOP_ATR) is widened; too wide (over MAX_STOP_ATR) means no trade.
* Target: the pattern's measured move, else the nearest untouched liquidity pool (draw on liquidity), else the next
  zone; never beyond the next strong opposing zone (a ceiling); at most MAX_TARGET_R; NO_ROOM_TARGET_R when nothing
  is in the way (price discovery). With the trend, the trend's own swing highs / lows are targets, not walls.
* Costs: SignalX markets.py (crypto 0.30% per round trip + futures funding for longs; forex spread + commission +
  slippage). The trade needs MIN_NET_RR reward-to-risk AFTER costs, and costs may not eat more than MAX_COST_R.
* Portfolio (same in the backtest and on the site): one trade per coin, at most MAX_NEW_PER_BAR new trades per candle,
  MAX_OPEN open (MAX_OPEN_SAME_SIDE in one direction - coins move together), no new trades for the day after
  DAILY_LOSS_LIMIT_R, and a cool-down on a coin after a loss.
"""
import numpy as np

from . import config as C
from .zones import fmt, major

try:
    import markets as _markets       # SignalX cost model
except Exception:                     # the package also works on its own (tests, research)
    _markets = None


def costs(market, symbol):
    """(round-trip cost as a share of price, funding per hour for longs, fixed cost in price units)."""
    if market == "crypto":
        rt = getattr(_markets, "CRYPTO_COST_ROUND_TRIP", 0.0030) if _markets else 0.0030
        fund = getattr(_markets, "CRYPTO_FUNDING_PER_HOUR", 0.0000125) if _markets else 0.0000125
        return float(rt), float(fund), 0.0
    fixed = 0.0
    if _markets is not None:
        try:
            fixed = float(_markets.cost_price(symbol))
        except Exception:
            fixed = 0.0
    return 0.0, 0.0, fixed


def cost_in_r(market, symbol, entry, risk, hours, side):
    rt, fund, fixed = costs(market, symbol)
    c = (rt * entry + fixed) / risk
    if side == 1:
        c += fund * hours * entry / risk
    return float(c)


class RiskManager:
    name = "risk"

    def __init__(self, market, symbol, tf):
        self.market, self.symbol, self.tf = market, symbol, tf
        self.hours = C.TIME_LIMIT_BARS[tf] * C.TF_SEC[tf] / 3600 / 2      # expected holding time for the cost

    def plan(self, ch, i, side, stop_ref, target_ref, obstacles, draw=None, pool_beyond_stop=None, with_trend=False):
        """with_trend: a trade with the trend on the entry chart expects the trend's own swing highs (lows for
        shorts) to break - they are liquidity to aim for, not walls. Only higher-timeframe zones and the previous
        week's levels stop it. Against the trend or in a range every significant zone is a wall."""
        a = ch.a(i)
        if with_trend:
            obstacles = [z for z in obstacles if z.level >= 1 or z.source in ("pwh", "pwl")]
        entry = float(ch.c[i])
        notes = []
        stop = stop_ref - side * C.STOP_BUFFER_ATR * a
        if pool_beyond_stop is not None:
            alt = pool_beyond_stop - side * C.STOP_BUFFER_ATR * a
            if (entry - alt) * side <= C.MAX_STOP_ATR * a and (alt - stop) * side < 0:
                stop = alt
                notes.append(f"stop placed beyond the liquidity at {fmt(pool_beyond_stop)}")
        risk = (entry - stop) * side
        if risk < C.MIN_STOP_ATR * a:
            stop = entry - side * C.MIN_STOP_ATR * a
            risk = C.MIN_STOP_ATR * a
        if risk > C.MAX_STOP_ATR * a:
            return None, f"the stop would be too wide ({risk / a:.1f} ATR)"
        cr = cost_in_r(self.market, self.symbol, entry, risk, self.hours, side)
        if cr > C.MAX_COST_R:
            return None, f"costs would eat {cr * 100:.0f}% of the risk (the stop is too close for the fees)"
        ceiling = None
        for z in obstacles:
            if major(z):
                ceiling = (z.lo - 0.1 * a) if side == 1 else (z.hi + 0.1 * a)
                break
        kind = None
        if target_ref is not None and (target_ref - entry) * side > 0:
            target, kind = float(target_ref), "measured move"
        elif draw is not None and (draw[0] - entry) * side > C.MIN_NET_RR * risk:
            target, kind = float(draw[0] - side * 0.05 * a), f"liquidity at the {draw[2]}"
        elif obstacles:
            z = obstacles[0]
            target, kind = float((z.lo - 0.1 * a) if side == 1 else (z.hi + 0.1 * a)), "next zone"
        else:
            target, kind = entry + side * C.NO_ROOM_TARGET_R * risk, f"{C.NO_ROOM_TARGET_R:.0f}R (nothing in the way)"
        if ceiling is not None and (target - ceiling) * side > 0:
            target, kind = ceiling, "below the next strong zone" if side == 1 else "above the next strong zone"
        cap = entry + side * C.MAX_TARGET_R * risk
        if (target - cap) * side > 0:
            target, kind = cap, f"{C.MAX_TARGET_R:.0f}R cap"
        rr = (target - entry) * side / risk
        rr_net = (rr - cr) / (1 + cr)
        if rr_net < C.MIN_NET_RR:
            return None, f"not enough room: the target ({kind}) is only {rr:.1f}R away"
        plan = {"entry": entry, "stop": float(stop), "target": float(target), "risk": float(risk),
                "rr": float(rr), "rr_net": float(rr_net), "cost_r": float(cr), "stop_atr": float(risk / a),
                "target_kind": kind, "notes": notes}
        return plan, None


def simulate(ch, i, side, entry, stop, target, max_bars, market="crypto", symbol=""):
    """Plays the trade out on the candles after i. Pessimistic: a candle that touches the stop and the target counts
    as the stop; a candle that opens beyond the stop exits at its open. Returns (net R, outcome, candles held, exit
    price), or None when the data ends before the trade does. Costs: fees + slippage and, for crypto longs, the
    funding of the real holding time."""
    risk = (entry - stop) * side
    n = ch.n
    end = min(n - 1, i + max_bars)
    outcome, px, j = None, None, i
    for j in range(i + 1, end + 1):
        o, h, lo = ch.o[j], ch.h[j], ch.l[j]
        if side == 1:
            if o <= stop:
                outcome, px = "stop", o
            elif lo <= stop:
                outcome, px = "stop", stop
            elif o >= target or h >= target:
                outcome, px = "target", target
        else:
            if o >= stop:
                outcome, px = "stop", o
            elif h >= stop:
                outcome, px = "stop", stop
            elif o <= target or lo <= target:
                outcome, px = "target", target
        if outcome:
            break
    if outcome is None:
        if end <= i:
            return None
        if end < i + max_bars and end == n - 1:
            return None                      # the data ends before the trade did: not counted
        outcome, px, j = "time", ch.c[end], end
    gross = (px - entry) * side / risk
    hours = (j - i) * C.TF_SEC[ch.tf] / 3600
    cr = cost_in_r(market, symbol, entry, risk, hours, side)       # fees + slippage, funding for the real hold
    return float(gross - cr), outcome, int(j - i), float(px)


class Portfolio:
    """The portfolio rules, applied to signals in time order (backtest) or to the open signals (site)."""

    def __init__(self, tf):
        self.tf = tf
        self.open = {}            # symbol -> (exit time, side)
        self.closed = []          # (exit time, symbol, net R)
        self.last_loss = {}       # symbol -> exit time of the last losing trade

    def _day_r(self, now):
        day = now.astype("datetime64[D]")
        return sum(r for t, _, r in self.closed if t.astype("datetime64[D]") == day and t <= now)

    def allow(self, symbol, side, now, new_this_bar):
        self.open = {s: v for s, v in self.open.items() if v[0] > now}
        if symbol in self.open:
            return False, "already in a trade on this coin"
        if new_this_bar >= C.MAX_NEW_PER_BAR:
            return False, "enough new trades on this candle"
        if len(self.open) >= C.MAX_OPEN:
            return False, "too many open trades"
        if sum(1 for v in self.open.values() if v[1] == side) >= C.MAX_OPEN_SAME_SIDE:
            return False, "too many open trades in the same direction (coins move together)"
        if self._day_r(now) <= C.DAILY_LOSS_LIMIT_R:
            return False, "daily loss limit reached"
        ll = self.last_loss.get(symbol)
        if ll is not None and now < ll + np.timedelta64(C.COOLDOWN_BARS_AFTER_LOSS * C.TF_SEC[self.tf], "s"):
            return False, "cool-down after a loss on this coin"
        return True, None

    def take(self, symbol, side, exit_time, net_r):
        self.open[symbol] = (exit_time, side)
        self.closed.append((exit_time, symbol, net_r))
        if len(self.closed) > 500:
            self.closed = self.closed[-300:]
        if net_r < 0:
            prev = self.last_loss.get(symbol)
            self.last_loss[symbol] = exit_time if prev is None or exit_time > prev else prev
