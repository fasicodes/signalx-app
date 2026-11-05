"""Critic agent ("devil's advocate"): before a trade is taken it looks only for reasons NOT to take it - the
questions a careful trader asks at the last second.

* Is it against both higher timeframes?
* Is it against the trend on this chart without a reversal signal (stop hunt, order block after a change of
  character, reversal pattern)?
* Is price stretched far from its 20-candle average (chasing)? Did a big move already happen without a pullback?
* Six candles in a row in the trade's direction (exhaustion)?
* No trend and no range (chop)?
* Is the stop very wide, or the reward barely above the minimum?
"""
import numpy as np

from .setups import REVERSAL

REVERSAL_PATTERNS = {"double bottom", "double top", "triple bottom", "triple top", "head and shoulders",
                     "inverse head and shoulders", "falling wedge", "rising wedge"}


def opinion(ch, i, side, setups, patterns, struct_trend, topdown_feats, plan):
    notes, vetoes = [], []
    pen = 0.0
    a = ch.a(i)
    reversal = bool(set(setups) & REVERSAL) or bool(set(patterns or []) & REVERSAL_PATTERNS)
    if topdown_feats.get("htf1") == -1 and topdown_feats.get("htf2") == -1:
        pen += 0.5
        notes.append("both higher timeframes are against the trade")
    if struct_trend == -side and not reversal and "range_edge" not in setups:
        pen += 0.4
        notes.append("against the trend on this chart without a reversal signal")
    stretch = (ch.c[i] - ch.ema20[i]) / a * side
    if stretch > 5:
        vetoes.append(f"price is {stretch:.1f} ATR away from its 20 average: far too late")
    elif stretch > 3:
        pen += 0.4
        notes.append(f"price is stretched ({stretch:.1f} ATR from its 20 average): chasing")
    if i >= 10:
        move = (ch.c[i] - ch.c[i - 10]) / a * side
        if move > 6:
            pen += 0.3
            notes.append(f"a {move:.0f} ATR move already happened in the last 10 candles")
        same = np.sign(ch.c[i - 5:i + 1] - ch.o[i - 5:i + 1]) * side
        if np.all(same > 0):
            pen += 0.2
            notes.append("six candles in a row the same way: the move may be tired")
    if ch.adx[i] < 15 and "range_edge" not in setups and not reversal:
        pen += 0.2
        notes.append("no trend and no clear range: choppy")
    if plan:
        if plan["stop_atr"] > 3:
            pen += 0.15
            notes.append(f"wide stop ({plan['stop_atr']:.1f} ATR)")
        if plan["rr_net"] < 1.8:
            pen += 0.1
            notes.append(f"the reward is only {plan['rr_net']:.1f}x the risk after costs")
    feats = {"critic_pen": float(min(pen, 2.0)), "stretch": float(np.clip(stretch, -6, 6))}
    return float(-min(pen, 1.0)), notes, vetoes, feats
