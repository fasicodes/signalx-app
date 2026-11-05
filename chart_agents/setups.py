"""The trade setups a price-action trader actually takes. Each one says where the idea is wrong (stop reference)
and, if it has one, where it should go (target reference). The other agents then judge the idea.

trend_pullback   trend on the entry chart, price pulls back into a support zone (resistance for shorts) and prints
                 a trigger candle (engulfing, hammer, rejection ...).
breakout_retest  a strong zone is broken with a close beyond it; price comes back to test it from the other side and
                 holds (old resistance becomes support).
pattern          a chart pattern (double bottom, head and shoulders, triangle, wedge, flag, range ...) breaks out.
liquidity_sweep  price runs the stops under a swing low / equal lows (above highs for shorts) and closes back:
                 a stop hunt.
range_edge       inside a clear range or channel, price tests its edge in the right direction with a trigger candle.
order_block      after a change of character, the first return to the candle where the move started.
failed_breakout  a trap: price broke a strong zone that everybody watches, then within 1-3 candles closed back
                 inside it. The breakout traders are trapped; the trade goes the other way, stop beyond the
                 breakout's extreme.
"""
from . import config as C
from .zones import touching

REVERSAL = {"liquidity_sweep", "order_block", "failed_breakout"}


def find(bk, i, zones_all):
    """Trade ideas on candle i: list of dicts (side, setup, stop_ref, target_ref, desc, strength)."""
    ch = bk.ch
    a = ch.a(i)
    ideas = []
    trig = {1: bk.cd.trigger(i, 1), -1: bk.cd.trigger(i, -1)}
    trend = int(bk.st.trend[i])

    # trend pullback into a zone
    for side in (1, -1):
        if trend == side and trig[side]:
            t = [z for z in touching(zones_all, ch, i, side) if z.strength >= 1.5]
            dyn = None
            if not t:
                e = ch.ema50[i]
                if (side == 1 and ch.l[i] <= e + 0.1 * a and ch.c[i] > e) or \
                   (side == -1 and ch.h[i] >= e - 0.1 * a and ch.c[i] < e):
                    dyn = e
            if t:
                z = max(t, key=lambda z: z.strength)
                ref = min(ch.l[i], z.lo) if side == 1 else max(ch.h[i], z.hi)
                ideas.append({"side": side, "setup": "trend_pullback", "stop_ref": float(ref), "target_ref": None,
                              "desc": "pullback in the trend into a zone", "strength": z.strength})
            elif dyn is not None:
                ref = ch.l[i] if side == 1 else ch.h[i]
                ideas.append({"side": side, "setup": "trend_pullback", "stop_ref": float(ref), "target_ref": None,
                              "desc": "pullback in the trend to the 50 average", "strength": 1.0})

    # failed breakout (trap): back inside the zone within 1-3 candles
    for b in bk.zn.breakouts:
        age = i - b["bar"]
        if b.get("trap_used") or age < 1 or age > 3:
            continue
        mid = (b["lo"] + b["hi"]) / 2
        if b["side"] == 1 and ch.c[i] < mid and ch.c[i] < ch.o[i]:
            ref = float(ch.h[b["bar"]:i + 1].max())
            ideas.append({"side": -1, "setup": "failed_breakout", "stop_ref": ref, "target_ref": None,
                          "desc": "failed breakout above a strong zone (trapped buyers)", "strength": b["strength"]})
            b["trap_used"] = True
        elif b["side"] == -1 and ch.c[i] > mid and ch.c[i] > ch.o[i]:
            ref = float(ch.l[b["bar"]:i + 1].min())
            ideas.append({"side": 1, "setup": "failed_breakout", "stop_ref": ref, "target_ref": None,
                          "desc": "failed breakdown below a strong zone (trapped sellers)", "strength": b["strength"]})
            b["trap_used"] = True

    # breakout and retest
    used = []
    for b in bk.zn.breakouts:
        age = i - b["bar"]
        if age < C.RETEST_MIN_BARS or age > C.RETEST_MAX_BARS:
            continue
        side = b["side"]
        if side == 1:
            ok = ch.l[i] <= b["hi"] + 0.2 * a and ch.c[i] > b["hi"] and (trig[1] or ch.c[i] > ch.o[i])
            ref = min(ch.l[i], b["lo"])
        else:
            ok = ch.h[i] >= b["lo"] - 0.2 * a and ch.c[i] < b["lo"] and (trig[-1] or ch.c[i] < ch.o[i])
            ref = max(ch.h[i], b["hi"])
        if ok:
            ideas.append({"side": side, "setup": "breakout_retest", "stop_ref": float(ref), "target_ref": None,
                          "desc": "retest of a broken zone", "strength": b["strength"]})
            used.append(b)
    if used:
        bk.zn.breakouts = [b for b in bk.zn.breakouts if b not in used]

    # chart pattern breakout
    for p in bk.pt.fired.get(i, []):
        ideas.append({"side": p.side, "setup": "pattern", "stop_ref": float(p.invalid),
                      "target_ref": float(ch.c[i] + p.side * p.height), "desc": p.desc, "strength": p.quality,
                      "pattern": p.name})

    # liquidity sweep / stop hunt
    for side in (1, -1):
        sw = bk.lq.sweep(i, side)
        if sw and trig[side]:
            ref = ch.l[i] if side == 1 else ch.h[i]
            ideas.append({"side": side, "setup": "liquidity_sweep", "stop_ref": float(ref), "target_ref": None,
                          "desc": f"stop hunt of the {sw[2]}", "strength": float(sw[1])})

    # range / channel edge
    rg = bk.pt.range
    if rg and rg["top"] - rg["bottom"] >= 3 * a:
        width = rg["top"] - rg["bottom"]
        if trig[1] and ch.l[i] <= rg["bottom"] + 0.25 * a and ch.c[i] > rg["bottom"]:
            ideas.append({"side": 1, "setup": "range_edge", "stop_ref": float(min(ch.l[i], rg["bottom"])),
                          "target_ref": float(rg["top"] - 0.1 * width), "desc": "bottom of the range",
                          "strength": 1.5})
        if trig[-1] and ch.h[i] >= rg["top"] - 0.25 * a and ch.c[i] < rg["top"]:
            ideas.append({"side": -1, "setup": "range_edge", "stop_ref": float(max(ch.h[i], rg["top"])),
                          "target_ref": float(rg["bottom"] + 0.1 * width), "desc": "top of the range",
                          "strength": 1.5})
    chn = bk.pt.channel
    if chn and chn["height"] >= 3 * a:
        lo_line = chn["ml"] * i + chn["b_lo"]
        hi_line = chn["mh"] * i + chn["b_hi"]
        if chn["side"] == 1 and trig[1] and ch.l[i] <= lo_line + 0.25 * a and ch.c[i] > lo_line:
            ideas.append({"side": 1, "setup": "range_edge", "stop_ref": float(min(ch.l[i], lo_line)),
                          "target_ref": float(hi_line - 0.1 * (hi_line - lo_line)),
                          "desc": "lower line of a rising channel", "strength": 1.5})
        if chn["side"] == -1 and trig[-1] and ch.h[i] >= hi_line - 0.25 * a and ch.c[i] < hi_line:
            ideas.append({"side": -1, "setup": "range_edge", "stop_ref": float(max(ch.h[i], hi_line)),
                          "target_ref": float(lo_line + 0.1 * (hi_line - lo_line)),
                          "desc": "upper line of a falling channel", "strength": 1.5})

    # order block after a change of character
    ch_ev = bk.st.last_choch
    if ch_ev and not ch_ev.get("used") and 2 < i - ch_ev["bar"] <= C.OB_MAX_BARS:
        side = ch_ev["side"]
        lo, hi = ch_ev["ob"]
        if side == 1 and trig[1] and ch.l[i] <= hi and ch.c[i] > lo:
            ideas.append({"side": 1, "setup": "order_block", "stop_ref": float(min(ch.l[i], lo)), "target_ref": None,
                          "desc": "first return to the order block after a change of character", "strength": 1.5})
            ch_ev["used"] = True
        elif side == -1 and trig[-1] and ch.h[i] >= lo and ch.c[i] < hi:
            ideas.append({"side": -1, "setup": "order_block", "stop_ref": float(max(ch.h[i], hi)), "target_ref": None,
                          "desc": "first return to the order block after a change of character", "strength": 1.5})
            ch_ev["used"] = True
    return ideas
