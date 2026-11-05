"""Head trader: listens to every agent and decides BUY / SELL / WAIT, then explains the decision in plain English.

Two ways to decide, both fixed before the holdout test and chosen on the validation years only:
* "learned": a logistic regression learns from the validation years how much each agent's opinion is really worth
  (how often trades like this ended in profit after costs). Expected result per trade = chance of profit x reward
  - chance of loss x risk - costs. A trade is taken when the expected result is above a threshold chosen on the
  validation years.
* "checklist": a human checklist - at least N agents clearly for the trade, the critic not strongly against.
A veto (no room to the target, costs too high, a news blackout, a thin market, a panic candle ...) always wins.
"""
import json
import math

import numpy as np

AGENTS = ("structure", "topdown", "zones", "patterns", "candles", "momentum", "volume", "liquidity", "session")
FEATURES = (
    "trend", "legs", "range_pos", "pullback",
    "zone_strength", "zone_touches", "zone_htf", "zone_flip", "zone_round", "zone_pd", "room_atr",
    "pattern_q", "pattern_against", "candle", "candle_reject",
    "div_regular", "div_hidden", "div_against", "rsi", "adx", "di", "macd_turn",
    "rvol", "pullback_vol", "obv", "taker", "vwap",
    "htf1", "htf2", "htf1_ema", "htf2_ema",
    "usd_log", "usd_rank", "sweep_for", "sweep_against", "pool_at_stop", "fvg_for", "fvg_against",
    "atr_rank", "weekend", "hour_asia", "hour_us", "critic_pen", "stretch",
    "rr", "rr_net", "cost_r", "stop_atr",
    "s_trend_pullback", "s_breakout_retest", "s_pattern", "s_liquidity_sweep", "s_range_edge", "s_order_block",
    "s_failed_breakout",
    "n_setups", "long",
    "sc_structure", "sc_topdown", "sc_zones", "sc_patterns", "sc_candles", "sc_momentum", "sc_volume",
    "sc_liquidity", "sc_session", "sc_critic",
)


def vector(c):
    f = c["feats"]
    return np.array([float(f.get(k, 0.0) or 0.0) for k in FEATURES], float)


def confluence(c):
    """How many agents are clearly for the trade (score >= 0.3), plus extra setups on the same candle."""
    s = c["scores"]
    return sum(1 for a in AGENTS if s.get(a, 0) >= 0.3) + max(0, c["feats"].get("n_setups", 1) - 1)


class HeadTrader:
    def __init__(self, mode="learned", coef=None, intercept=0.0, mean=None, std=None, threshold=0.0,
                 min_confluence=4, strong=None, meta=None):
        self.mode = mode
        self.coef = np.asarray(coef if coef is not None else np.zeros(len(FEATURES)), float)
        self.intercept = float(intercept)
        self.mean = np.asarray(mean if mean is not None else np.zeros(len(FEATURES)), float)
        self.std = np.asarray(std if std is not None else np.ones(len(FEATURES)), float)
        self.threshold = float(threshold)
        self.min_confluence = int(min_confluence)
        self.strong = strong
        self.meta = meta or {}

    # ------------------------------------------------------------------ scoring
    def prob(self, c):
        x = (vector(c) - self.mean) / self.std
        z = float(x @ self.coef + self.intercept)
        return 1.0 / (1.0 + math.exp(-max(min(z, 30), -30)))

    def expected_r(self, c):
        p = self.prob(c)
        pl = c["plan"]
        return p * (pl["rr"] - pl["cost_r"]) - (1 - p) * (1 + pl["cost_r"])

    def score(self, c):
        return self.expected_r(c) if self.mode == "learned" else confluence(c) + 0.1 * c["scores"].get("critic", 0)

    def take(self, c):
        if c["vetoes"]:
            return False
        if self.mode == "learned":
            return self.expected_r(c) >= self.threshold
        return confluence(c) >= self.min_confluence and c["scores"].get("critic", 0) > -0.5

    def grade(self, c):
        if self.strong is None:
            return "standard"
        return "strong" if self.score(c) >= self.strong else "standard"

    # ------------------------------------------------------------------ explanation
    def explain(self, c, fmt=None):
        from .zones import fmt as _fmt
        fmt = fmt or _fmt
        pl = c["plan"]
        side = "BUY" if c["side"] == 1 else "SELL"
        n = c["notes"]
        parts = []
        for k in ("topdown", "structure", "zones", "patterns", "candles", "momentum", "volume", "liquidity",
                  "session", "news"):
            for t in n.get(k, [])[:3]:
                parts.append(t)
        why = "; ".join(_cap(p) for p in parts[:9])
        risk = f"Entry {fmt(pl['entry'])}, stop {fmt(pl['stop'])}, target {fmt(pl['target'])} ({pl['target_kind']}), " \
               f"reward {pl['rr']:.1f}x the risk ({pl['rr_net']:.1f}x after costs)."
        extra = n.get("risk", [])
        watch = n.get("critic", [])
        out = f"{c['symbol']} {c['tf']} - {side} ({c['desc']}). {why}. {risk}"
        if extra:
            out += " " + _cap("; ".join(extra)) + "."
        if watch:
            out += " Watch out: " + "; ".join(watch) + "."
        return out

    # ------------------------------------------------------------------ storage
    def to_json(self):
        return {"mode": self.mode, "features": list(FEATURES), "coef": [round(float(x), 6) for x in self.coef],
                "intercept": round(self.intercept, 6), "mean": [round(float(x), 6) for x in self.mean],
                "std": [round(float(x), 6) for x in self.std], "threshold": self.threshold,
                "min_confluence": self.min_confluence, "strong": self.strong, "meta": self.meta}

    @classmethod
    def from_json(cls, d):
        if isinstance(d, str):
            d = json.loads(d)
        feats = d.get("features") or list(FEATURES)
        if list(feats) != list(FEATURES):          # a model saved with other features: map by name
            idx = {k: j for j, k in enumerate(feats)}

            def pick(arr, default):
                return [arr[idx[k]] if k in idx else default for k in FEATURES]
            d = dict(d, coef=pick(d["coef"], 0.0), mean=pick(d["mean"], 0.0), std=pick(d["std"], 1.0))
        return cls(d.get("mode", "learned"), d.get("coef"), d.get("intercept", 0.0), d.get("mean"), d.get("std"),
                   d.get("threshold", 0.0), d.get("min_confluence", 4), d.get("strong"), d.get("meta"))


def _cap(s):
    return s[:1].upper() + s[1:] if s else s
