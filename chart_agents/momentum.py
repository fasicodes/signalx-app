"""Momentum agent: RSI, MACD, stochastic and ADX, used the way traders use them - as confirmation, not as the reason.

* Divergence. Regular bullish: price makes a lower low but RSI a higher low (selling is running out). Hidden
  bullish: price makes a higher low but RSI a lower low (a healthy pullback in an uptrend). Bearish: the mirrors.
  Checked between the last two confirmed swings and between the last swing and the current candle.
* RSI level: oversold / overbought, and the 40-60 "reset" area that pullbacks in a trend usually reach.
* MACD histogram turning in the trade's direction, a fresh MACD cross, a stochastic cross from the extreme.
* ADX: is there a trend at all, and do +DI / -DI agree with the trade.
"""
import numpy as np


class MomentumAgent:
    name = "momentum"

    def __init__(self, ch, swings):
        self.ch, self.sw = ch, swings

    def divergence(self, i, side):
        """('regular' | 'hidden' | None) for a bullish (side 1) / bearish (-1) divergence at candle i."""
        ch, rsi = self.ch, self.ch.rsi
        kind = "L" if side == 1 else "H"
        last = self.sw.last(kind, 2)
        price = ch.l if side == 1 else ch.h
        found = None
        if last:
            p = last[0]
            if i - p.idx >= 3:
                if side == 1:
                    if price[i] < p.price and rsi[i] > rsi[p.idx] + 2:
                        found = "regular"
                    elif price[i] > p.price and rsi[i] < rsi[p.idx] - 2 and ch.c[i] > ch.o[i]:
                        found = "hidden"
                else:
                    if price[i] > p.price and rsi[i] < rsi[p.idx] - 2:
                        found = "regular"
                    elif price[i] < p.price and rsi[i] > rsi[p.idx] + 2 and ch.c[i] < ch.o[i]:
                        found = "hidden"
        if found is None and len(last) == 2 and i - last[0].conf <= 6:
            b, a = last[0], last[1]
            if side == 1:
                if b.price < a.price and rsi[b.idx] > rsi[a.idx] + 2:
                    found = "regular"
                elif b.price > a.price and rsi[b.idx] < rsi[a.idx] - 2:
                    found = "hidden"
            else:
                if b.price > a.price and rsi[b.idx] < rsi[a.idx] - 2:
                    found = "regular"
                elif b.price < a.price and rsi[b.idx] > rsi[a.idx] + 2:
                    found = "hidden"
        return found

    def opinion(self, i, side, trend):
        ch = self.ch
        notes = []
        score = 0.0
        div = self.divergence(i, side)
        opp = self.divergence(i, -side)
        if div == "regular":
            score += 0.45
            notes.append(("bullish" if side == 1 else "bearish") + " RSI divergence (momentum is turning)")
        elif div == "hidden":
            score += 0.35 if trend == side else 0.1
            notes.append("hidden " + ("bullish" if side == 1 else "bearish") + " RSI divergence (trend continuation)")
        if opp == "regular":
            score -= 0.35
            notes.append("RSI divergence against the trade")
        r = float(ch.rsi[i])
        if side == 1:
            if r < 30:
                score += 0.15
                notes.append(f"RSI {r:.0f}: oversold")
            elif r > 72:
                score -= 0.3
                notes.append(f"RSI {r:.0f}: overbought, late to buy")
            elif trend == 1 and 38 <= r <= 58:
                score += 0.1
                notes.append(f"RSI {r:.0f}: reset to the middle in an uptrend")
        else:
            if r > 70:
                score += 0.15
                notes.append(f"RSI {r:.0f}: overbought")
            elif r < 28:
                score -= 0.3
                notes.append(f"RSI {r:.0f}: oversold, late to sell")
            elif trend == -1 and 42 <= r <= 62:
                score += 0.1
                notes.append(f"RSI {r:.0f}: reset to the middle in a downtrend")
        hist = ch.macd_hist
        if i >= 3:
            if (hist[i] - hist[i - 1]) * side > 0:
                score += 0.1
                if (hist[i] * side > 0) and (hist[i - 1] * side <= 0 or hist[i - 2] * side <= 0
                                            or hist[i - 3] * side <= 0):
                    score += 0.1
                    notes.append("fresh MACD cross " + ("up" if side == 1 else "down"))
            else:
                score -= 0.05
            k, d = ch.stoch_k, ch.stoch_d
            if side == 1 and k[i] > d[i] and k[i - 1] <= d[i - 1] and k[i - 1] < 25:
                score += 0.15
                notes.append("stochastic turned up from oversold")
            if side == -1 and k[i] < d[i] and k[i - 1] >= d[i - 1] and k[i - 1] > 75:
                score += 0.15
                notes.append("stochastic turned down from overbought")
        adx = float(ch.adx[i])
        di = (ch.pdi[i] - ch.mdi[i]) * side
        if adx >= 25 and di > 0:
            score += 0.15
            notes.append(f"ADX {adx:.0f}: a strong trend in the trade's direction")
        elif adx >= 25 and di < 0:
            score -= 0.2
            notes.append(f"ADX {adx:.0f}: a strong trend against the trade")
        feats = {"div_regular": int(div == "regular"), "div_hidden": int(div == "hidden"),
                 "div_against": int(opp == "regular"), "rsi": (r - 50) / 50 * side, "adx": adx / 50,
                 "di": float(np.clip(di / 30, -1, 1)), "macd_turn": float(np.sign(hist[i] - hist[i - 1]) * side)
                 if i else 0.0}
        return float(np.clip(score, -1, 1)), notes, feats
