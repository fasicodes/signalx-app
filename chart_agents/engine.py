"""The human-style chart engine: runs every agent over the chart candle by candle, exactly as a trader would scroll
through it, and turns the trade ideas into fully judged candidates.

The backtest (research/human_lab/lab.py) and the live site use this same code: the site replays the last
LIVE_HISTORY candles and judges the newest closed candle; the backtest replays all of history and judges every
candle. Nothing at candle i ever uses a candle after i.
"""
import re

import numpy as np

from . import config as C
from . import critic as critic_agent
from . import setups as setup_finder
from .candles import CandleAgent
from .data import Chart, align
from .liquidity import LiquidityAgent
from .momentum import MomentumAgent
from .patterns import PatternAgent
from .risk import RiskManager
from .session import NewsAgent, SessionAgent
from .structure import StructureAgent
from .swings import SwingTracker
from .topdown import TopDownAgent
from .volume import VolumeAgent
from .zones import Zone, ZoneAgent, merge, obstacles, on_entry_chart, round_levels
from . import zones as zone_agent

TF_NAME = {"15m": "15-minute", "1h": "1-hour", "4h": "4-hour", "1d": "daily", "1w": "weekly"}
SETUPS = ("trend_pullback", "breakout_retest", "pattern", "liquidity_sweep", "range_edge", "order_block",
          "failed_breakout")
WARMUP = 300


class Book:
    """All the agents that work on one timeframe."""

    def __init__(self, ch, level=0, entry=False):
        self.ch, self.level, self.entry = ch, level, entry
        self.sw = SwingTracker(ch)
        self.st = StructureAgent(ch, self.sw)
        self.zn = ZoneAgent(ch, self.sw, level)
        if entry:
            self.pt = PatternAgent(ch, self.sw)
            self.cd = CandleAgent(ch)
            self.mo = MomentumAgent(ch, self.sw)
            self.vo = VolumeAgent(ch, self.sw)
            self.lq = LiquidityAgent(ch, self.sw)
            self.se = SessionAgent(ch)

    def step(self, i):
        self.sw.on_bar(i)
        if self.entry and self.sw.changed:
            self.pt.on_swings(i)
        self.st.on_bar(i)
        self.zn.on_bar(i)
        if self.entry:
            self.pt.on_bar(i)
            self.lq.on_bar(i)

    def run(self):
        for i in range(self.ch.n):
            self.step(i)
        return self


class Engine:
    """frames: {timeframe: DataFrame(timestamp, open, high, low, close, volume[, taker_buy_volume, quote_volume])}
    with the entry timeframe and its two higher timeframes; '1d' and '1w' (previous day / week levels) if available."""

    def __init__(self, frames, tf, symbol, market="crypto", live=False):
        self.tf, self.symbol, self.market, self.live = tf, symbol, market, live
        self.ch = Chart(frames[tf], tf, symbol, market)
        self.htf_names = C.HIGHER_TF[tf]
        self.htf = [Book(Chart(frames[h], h, symbol, market), level=k + 1).run() for k, h in enumerate(self.htf_names)]
        self.aligns = [align(b.ch, self.ch) for b in self.htf]
        self.level_charts = {}
        for h in ("1d", "1w"):
            if h in self.htf_names:
                self.level_charts[h] = self.htf[self.htf_names.index(h)].ch
            elif h in frames and frames[h] is not None and len(frames[h]):
                self.level_charts[h] = Chart(frames[h], h, symbol, market)
        self.level_align = {h: align(c, self.ch) for h, c in self.level_charts.items()}
        self.bk = Book(self.ch, 0, entry=True)
        self.td = TopDownAgent(self.htf, self.aligns, [TF_NAME[h] for h in self.htf_names])
        self.rm = RiskManager(market, symbol, tf)
        self.news = NewsAgent(symbol, live=live)
        self.candidates = []
        self.vetoed = 0
        self.veto_reasons = {}

    # ------------------------------------------------------------------ levels every trader watches
    def _levels(self, i):
        zs, liq = [], []
        a = self.ch.a(i)
        for h, (hi_name, lo_name, hk, lk, s) in {"1d": ("previous day high", "previous day low", "pdh", "pdl", 1.6),
                                                  "1w": ("previous week high", "previous week low", "pwh", "pwl",
                                                         2.0)}.items():
            if h not in self.level_charts:
                continue
            j = int(self.level_align[h][i])
            if j < 0:
                continue
            c = self.level_charts[h]
            hi, lo = float(c.h[j]), float(c.l[j])
            zs.append(Zone(hi - 0.15 * a, hi + 0.15 * a, 1, False, 0.0, i, s, 0, hk))
            zs.append(Zone(lo - 0.15 * a, lo + 0.15 * a, 1, False, 0.0, i, s, 0, lk))
            liq.append((hi, "H", hi_name))
            liq.append((lo, "L", lo_name))
        return zs, liq

    def zones_all(self, i, level_zones=None):
        z = list(self.bk.zn.snap[i] or [])
        a = self.ch.a(i)
        for b, al in zip(self.htf, self.aligns):
            j = int(al[i])
            if j >= 0 and b.zn.snap[j]:
                for hz in b.zn.snap[j]:
                    z.extend(on_entry_chart(hz, a))
        if level_zones is None:
            level_zones, _ = self._levels(i)
        z = merge(z)
        z.extend(level_zones)
        z.extend(round_levels(float(self.ch.c[i]), self.ch.a(i)))
        return z

    # ------------------------------------------------------------------ replay
    def run(self, evaluate_from=WARMUP, last_only=False):
        """Replays every candle. Setups are looked for on every candle after the warm-up (a retest or an order
        block is used up once it happened, whether or not it became a trade), so a live replay of the last
        LIVE_HISTORY candles reaches the same state as the full backtest. Trade ideas are fully judged from
        evaluate_from on (last_only: only the newest candle)."""
        ch, bk = self.ch, self.bk
        start = max(evaluate_from, WARMUP)
        for i in range(ch.n):
            lz, liq = self._levels(i)
            bk.lq.levels = liq
            bk.step(i)
            if i < WARMUP:
                continue
            if any(int(al[i]) < 30 for al in self.aligns):
                continue
            if not (bk.cd.trigger(i, 1) or bk.cd.trigger(i, -1) or bk.zn.breakouts or bk.pt.fired.get(i)
                    or bk.lq.sweep(i, 1) or bk.lq.sweep(i, -1)):
                continue
            za = self.zones_all(i, lz)
            ideas = setup_finder.find(bk, i, za)
            if not ideas or i < start or (last_only and i != ch.n - 1):
                continue
            sides = {x["side"] for x in ideas}
            if len(sides) == 2:
                self._veto("mixed signals on the same candle")   # a trader waits
                continue
            cand = self.evaluate(i, ideas[0]["side"], ideas, za)
            if cand is not None:
                self.candidates.append(cand)
        return self.candidates

    def _veto(self, why):
        self.vetoed += 1
        self.veto_reasons[why] = self.veto_reasons.get(why, 0) + 1

    # ------------------------------------------------------------------ judging one trade idea
    def evaluate(self, i, side, ideas, za):
        ch, bk = self.ch, self.bk
        obs = obstacles(za, ch.c[i], side)
        draw = bk.lq.draw(i, side, ch.c[i])
        best, why_not = None, None
        for idea in ideas:
            pools = bk.lq.pools(i, "L" if side == 1 else "H")
            near = None
            a = ch.a(i)
            for price, size, name in pools:
                gap = (idea["stop_ref"] - price) * side
                if 0 <= gap <= 0.6 * a:
                    near = price if near is None or (price - near) * side < 0 else near
            with_trend = int(bk.st.trend[i]) == side and idea["setup"] != "range_edge"
            plan, err = self.rm.plan(ch, i, side, idea["stop_ref"], idea["target_ref"], obs, draw, near, with_trend)
            if plan is None and idea["setup"] == "pattern" and err and "too wide" in err:
                # the pattern's own invalidation is far away: stop under the base of the breakout instead
                base = ch.l[max(0, i - 4):i + 1].min() if side == 1 else ch.h[max(0, i - 4):i + 1].max()
                plan, err = self.rm.plan(ch, i, side, float(base), idea["target_ref"], obs, draw, near, with_trend)
                if plan is not None:
                    plan["notes"].append("stop under the base of the breakout (the pattern's low is too far)"
                                         if side == 1 else
                                         "stop above the base of the breakdown (the pattern's high is too far)")
            if plan is None:
                why_not = why_not or err
                continue
            if best is None or plan["rr_net"] > best[1]["rr_net"]:
                best = (idea, plan)
        if best is None:
            self._veto(re.sub(r"[0-9.]+", "#", why_not or "no plan"))
            return None
        idea, plan = best
        names = [x["setup"] for x in ideas]
        pats = [x.get("pattern") for x in ideas if x.get("pattern")]
        tfn = TF_NAME[self.tf]
        scores, notes, feats, vetoes = {}, {}, {}, []

        s, n, f = bk.st.opinion(i, side, tfn)
        scores["structure"], notes["structure"] = s, n
        feats.update(f)
        s, n, f = self.td.opinion(i, side)
        scores["topdown"], notes["topdown"] = s, n
        feats.update(f)
        s, n, f = zone_agent.opinion(za, ch, i, side, tfn)
        scores["zones"], notes["zones"] = s, n
        feats.update(f)
        s, n, f, _ = bk.pt.opinion(i, side)
        scores["patterns"], notes["patterns"] = s, n
        feats.update(f)
        s, n, f = bk.cd.opinion(i, side)
        scores["candles"], notes["candles"] = s, n
        feats.update(f)
        s, n, f = bk.mo.opinion(i, side, int(bk.st.trend[i]))
        scores["momentum"], notes["momentum"] = s, n
        feats.update(f)
        kind = "pattern" if "pattern" in names else ("breakout_retest" if "breakout_retest" in names else "other")
        s, n, f = bk.vo.opinion(i, side, kind)
        scores["volume"], notes["volume"] = s, n
        feats.update(f)
        s, n, f, v = bk.lq.opinion(i, side, plan["stop"])
        scores["liquidity"], notes["liquidity"] = s, n
        feats.update(f)
        if v:
            vetoes.append(v)
        s, n, f, v = bk.se.opinion(i, side)
        scores["session"], notes["session"] = s, n
        feats.update(f)
        if v:
            vetoes.append(v)
        s, n, v = self.news.opinion()
        scores["news"], notes["news"] = s, n
        if v:
            vetoes.append(v)
        s, n, v, f = critic_agent.opinion(ch, i, side, names, pats, int(bk.st.trend[i]), feats, plan)
        scores["critic"], notes["critic"] = s, n
        feats.update(f)
        vetoes += v
        notes["risk"] = plan.pop("notes", [])
        for k in ("rr", "rr_net", "cost_r", "stop_atr"):
            feats[k] = plan[k]
        for st in SETUPS:
            feats["s_" + st] = int(st in names)
        feats["n_setups"] = len(set(names))
        feats["long"] = int(side == 1)
        for k, v in scores.items():
            feats["sc_" + k] = v
        return {"symbol": self.symbol, "tf": self.tf, "market": self.market, "bar": i,
                "time": str(np.datetime_as_string(ch.t_close[i], unit="s")), "side": side, "setups": names,
                "setup": idea["setup"], "desc": idea["desc"], "patterns": pats, "plan": plan, "scores": scores,
                "notes": notes, "feats": feats, "vetoes": vetoes, "atr": ch.a(i)}

    # ------------------------------------------------------------------ the chart as the agents see it now
    def report(self, i=None):
        ch, bk = self.ch, self.bk
        i = ch.n - 1 if i is None else i
        za = self.zones_all(i)
        price = float(ch.c[i])
        sup = sorted([z for z in za if z.hi < price and z.source == "swing"], key=lambda z: -z.hi)[:3]
        res = sorted([z for z in za if z.lo > price and z.source == "swing"], key=lambda z: z.lo)[:3]
        trend_word = {1: "uptrend", -1: "downtrend", 0: "no clear trend"}
        tfs = [(TF_NAME[self.tf], int(bk.st.trend[i]))]
        for (nm, t, _e) in self.td.state(i):
            tfs.append((nm, t))
        return {
            "symbol": self.symbol, "tf": self.tf, "time": str(np.datetime_as_string(ch.t_close[i], unit="s")),
            "price": price,
            "trend": [{"timeframe": nm, "trend": trend_word[t]} for nm, t in tfs],
            "support": [{"lo": z.lo, "hi": z.hi, "touches": z.touches, "strength": round(z.strength, 1)} for z in sup],
            "resistance": [{"lo": z.lo, "hi": z.hi, "touches": z.touches, "strength": round(z.strength, 1)}
                           for z in res],
            "patterns": [{"name": p.name, "side": p.side, "breakout_level": p.level(i)} for p in bk.pt.armed][-5:],
            "liquidity_above": [{"price": p, "size": s, "name": nm} for p, s, nm in
                                sorted([x for x in bk.lq.pools(i, "H") if x[0] > price], key=lambda x: x[0])[:3]],
            "liquidity_below": [{"price": p, "size": s, "name": nm} for p, s, nm in
                                sorted([x for x in bk.lq.pools(i, "L") if x[0] < price], key=lambda x: -x[0])[:3]],
            "fair_value_gaps": [{"side": g["side"], "lo": g["lo"], "hi": g["hi"]} for g in bk.lq.fvgs][-4:],
            "rsi": float(ch.rsi[i]), "adx": float(ch.adx[i]), "atr": ch.a(i),
            "swings": [{"kind": s.kind, "price": s.price, "time": str(np.datetime_as_string(ch.t[s.idx], unit="s"))}
                       for s in bk.sw.swings[-8:]],
        }
