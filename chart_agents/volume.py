"""Volume / order-flow agent.

* Breakouts should come on high volume (relative volume = volume / its 20-candle average).
* Pullbacks in a trend should come on falling volume (the other side is not really pushing).
* A volume climax with a long wick at a level is capitulation / absorption.
* On-balance volume (OBV) rising with price confirms buyers; OBV making a higher low while price makes a lower low
  is accumulation.
* Taker buy share (crypto): who is hitting the market - buyers (> 55%) or sellers (< 45%).
* VWAP of the day: buyers are in control above it, sellers below it.
Forex has no real volume, so this agent stays neutral there.
"""
import numpy as np


class VolumeAgent:
    name = "volume"

    def __init__(self, ch, swings):
        self.ch, self.sw = ch, swings

    def opinion(self, i, side, setup_kind):
        ch = self.ch
        feats = {"rvol": 1.0, "pullback_vol": 1.0, "obv": 0.0, "taker": 0.0, "vwap": 0.0}
        if not ch.has_volume:
            return 0.0, [], feats
        notes = []
        score = 0.0
        rv = float(ch.rvol[i])
        feats["rvol"] = min(rv, 5.0)
        breakout = setup_kind in ("breakout_retest", "pattern")
        if setup_kind == "pattern":
            if rv >= 1.5:
                score += 0.35
                notes.append(f"breakout candle on {rv:.1f}x normal volume")
            elif rv < 0.8:
                score -= 0.3
                notes.append(f"breakout on low volume ({rv:.1f}x normal): could be a fake-out")
        if i >= 25 and not breakout:
            recent = ch.v[i - 5:i].mean()
            before = ch.v[i - 25:i - 5].mean()
            ratio = float(recent / before) if before > 0 else 1.0
            feats["pullback_vol"] = min(ratio, 3.0)
            if ratio < 0.8:
                score += 0.2
                notes.append("the pullback came on falling volume")
            elif ratio > 1.6:
                score -= 0.15
                notes.append("heavy volume into the level")
            rej = (ch.loww[i] if side == 1 else ch.upw[i]) >= 0.45 * ch.rng[i]
            if rv >= 3 and rej:
                score += 0.2
                notes.append(f"volume climax ({rv:.1f}x) with a rejection wick: " +
                             ("sellers were absorbed" if side == 1 else "buyers were absorbed"))
        if i >= 20:
            slope = (ch.obv[i] - ch.obv[i - 20]) / max(ch.vol_sma[i] * 20, 1e-12)
            feats["obv"] = float(np.clip(slope * side, -1, 1))
            if slope * side > 0.15:
                score += 0.1
                notes.append("on-balance volume agrees")
            elif slope * side < -0.15:
                score -= 0.1
        kind = "L" if side == 1 else "H"
        last = self.sw.last(kind)
        if last and i - last[0].idx >= 3:
            p = last[0]
            px = ch.l if side == 1 else ch.h
            if (px[i] - p.price) * side < 0 and (ch.obv[i] - ch.obv[p.idx]) * side > 0:
                score += 0.15
                notes.append("OBV divergence: " + ("accumulation" if side == 1 else "distribution"))
        if ch.taker is not None:
            tk = float(ch.taker[i])
            feats["taker"] = (tk - 0.5) * 2 * side
            if (tk - 0.5) * side > 0.05:
                score += 0.15
                share = tk if side == 1 else 1 - tk
                notes.append(f"{share * 100:.0f}% of the last 3 candles' volume was taker "
                             + ("buying" if side == 1 else "selling"))
            elif (tk - 0.5) * side < -0.05:
                score -= 0.15
        if ch.vwap is not None:
            above = ch.c[i] > ch.vwap[i]
            feats["vwap"] = 1.0 if above == (side == 1) else -1.0
            if above == (side == 1):
                score += 0.05
        return float(np.clip(score, -1, 1)), notes, feats
