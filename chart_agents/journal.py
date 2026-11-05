"""Journal agent: keeps the record a disciplined trader keeps - every signal, what each agent said, how it ended -
and answers "which agents actually help?". It never changes a live model by itself: re-training happens only in the
research lab, with the same out-of-sample rules.
"""
import numpy as np

from .head import AGENTS


def attribution(rows):
    """rows: candidates with a 'net_r' result. For every agent: average result when it was for the trade
    (score >= 0.3) and when it was not, and how many trades each side had."""
    out = {}
    for a in AGENTS + ("critic",):
        sc = np.array([r["scores"].get(a, 0.0) for r in rows], float)
        rr = np.array([r["net_r"] for r in rows], float)
        if not len(rr):
            continue
        thr = 0.3 if a != "critic" else -0.15
        on = sc >= thr
        out[a] = {"for_trades": int(on.sum()), "for_avg_r": round(float(rr[on].mean()), 3) if on.any() else None,
                  "other_trades": int((~on).sum()),
                  "other_avg_r": round(float(rr[~on].mean()), 3) if (~on).any() else None}
    return out


def by_setup(rows):
    out = {}
    for r in rows:
        for s in set(r["setups"]):
            d = out.setdefault(s, [])
            d.append(r["net_r"])
    return {s: {"trades": len(v), "avg_r": round(float(np.mean(v)), 3),
                "win_rate": round(100 * float(np.mean(np.array(v) > 0)), 1)} for s, v in sorted(out.items())}
