"""Re-test of the crypto 4h engine (signal_engine.py; the gradient-boosted trees first shipped as engine 3.1)
on data it never saw, with the site's own replay code, for the exit plans that were ever used:

  p3  targets 0.5R / 1R / 2R, a third at each, stop to the entry after target 1 (the plan until Oct 2026;
      again the live plan from engine 5.0)
  p1  one target at 0.5R
  t1  one target at 1R (the plan of engine 3.2, Oct - Nov 2026)

The trees were trained on Jul 2020 - Sep 2025, so everything from 2025-10-01 on is unseen. Costs are the strict
ones of the engine 5 research: 0.30% per round trip, futures funding for longs for the hours held, and the entry at
the price one minute into the next candle (not the close) with the stop and targets where the signal put them.

  python engine4h.py test            prints the three plans
  python engine4h.py write           also writes the live plan's result into signal_model.json ("test")
"""
import json
import os
import sys
import warnings

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, APP)
sys.path.insert(0, HERE)
import features as FE  # noqa: E402
import markets  # noqa: E402
import signal_engine as se  # noqa: E402
import streams as ST  # noqa: E402
import lab5 as L  # noqa: E402

START, END = "2025-10-01", "2026-11-08"
PLANS = {"p3": ([0.5, 1.0, 2.0], [1 / 3, 1 / 3, 1 / 3], 1), "p1": ([0.5], [1.0], 0), "t1": ([1.0], [1.0], 0)}
LIVE_PLAN = "p3"
MODEL = os.path.join(APP, "signal_model.json")


def scored():
    m = se.model()
    coins = [c for c in se.signal_coins() if os.path.exists(f"{L.W}/data/crypto/{c.split('/')[0]}.parquet")]
    need = pd.Timestamp(START) - pd.Timedelta(days=260)
    frames = {}
    for sym in sorted(set(coins) | set(se.UNIVERSE)):
        d = pd.read_parquet(f"{L.W}/data/crypto/{sym.split('/')[0]}.parquet")
        d = d[(d.timestamp >= need) & (d.timestamp < pd.Timestamp(END))].reset_index(drop=True)
        if len(d) < 16 * 300:
            continue
        b = FE.resample_closed(d, "4h")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            F = se.coin_features(b[["timestamp", "open", "high", "low", "close", "volume"]])
        fill = d.set_index("timestamp")["p1"]
        F["slip"] = fill.reindex(b["timestamp"] + pd.Timedelta(hours=4)).to_numpy() - b["close"].to_numpy()
        frames[sym] = F
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mkt = se.market_block({s: frames.get(s) for s in se.UNIVERSE})
        btc = se.btc_block(frames["BTC/USDT"])
        S = {}
        for sym in coins:
            F = frames.get(sym)
            if F is None or len(F) <= se.WARMUP_BARS:
                continue
            pl, ps, cl, cs = se.score(se.assemble(F, btc, mkt, sym=sym))
            S[sym] = {"F": F, "pl": pl, "ps": ps, "cl": cl, "cs": cs}
    return m, S


def replay(m, S, plan):
    """Every closed signal of the engine's own replay under `plan`, with the strict result (r)."""
    old = (m.targets_r, m.fractions, m.be_after)
    m.targets_r, m.fractions, m.be_after = PLANS[plan]
    try:
        closed = {}
        se.replay_all(S, start_time=START, closed=closed)
    finally:
        m.targets_r, m.fractions, m.be_after = old
    rows = []
    for sym, lst in closed.items():
        F = S[sym]["F"]
        ts = pd.to_datetime(F["timestamp"]).to_numpy()
        slip = F["slip"].to_numpy()
        for t in lst:
            i = int(np.searchsorted(ts, np.datetime64(t["bar_time"])))
            risk = abs(t["entry"] - t["stop_loss"])
            sgn = 1 if t["side"] == "LONG" else -1
            gross = t["r"] + se.cost_r(t["entry"], t["stop_loss"])
            r = gross - ST.cost_r(sym, t["entry"], t["stop_loss"], hours=t["bars_held"] * 4.0, side=t["side"])
            if np.isfinite(slip[i]):
                r -= sgn * slip[i] / risk
            rows.append(dict(t, symbol=sym, r=round(float(r), 3), r_site=t["r"], grade="C"))
    return rows


def test_block(rows, n_coins):
    s = L.stats(rows, START, END)
    out = {k: s[k] for k in ("period", "signals", "signals_per_day", "win_rate", "tp1_rate", "tp2_rate", "tp3_rate",
                             "stop_rate", "money_making_rate", "avg_net_r", "total_net_r", "profit_factor",
                             "max_drawdown_r", "avg_hold_bars", "max_gap_days", "avg_stop_pct", "years")}
    out = {"period": out.pop("period"), "coins": n_coins, "coins_with_signals": len({r["symbol"] for r in rows}), **out}
    for tier in ("strong", "standard"):
        sel = [r for r in rows if r["tier"] == tier]
        if sel:
            out[tier] = {"signals": len(sel), "avg_net_r": round(float(np.mean([r["r"] for r in sel])), 3),
                         "win_rate": round(sum(1 for r in sel if r["status"].startswith("TP")) / len(sel) * 100, 1)}
    q = {}
    for r in rows:
        t = pd.Timestamp(r["signal_at"])
        q.setdefault(f"{t.year}-Q{(t.month - 1) // 3 + 1}", []).append(r)
    out["quarters"] = {k: {"signals": len(v), "avg_net_r": round(float(np.mean([r["r"] for r in v])), 3),
                           "win_rate": round(sum(1 for r in v if r["status"].startswith("TP")) / len(v) * 100, 1)}
                       for k, v in sorted(q.items())}
    out["fees"] = "0.30% per round trip (Binance VIP 0 fees + slippage) plus funding for longs"
    out["cost_round_trip"] = markets.CRYPTO_COST_ROUND_TRIP
    out["note"] = ("Tested Nov 2026 on data after the model's training period, replayed exactly as the site does (4h "
                   "candles, the engine's own stop / targets / time limit and signal rules). A win = target 1 or better. "
                   "Entries at the price one minute after the signal candle closed.")
    return out


def main(write=False):
    m, S = scored()
    res = {}
    for plan in PLANS:
        rows = replay(m, S, plan)
        res[plan] = test_block(rows, len(S))
        t = res[plan]
        print(f"{plan}: signals {t['signals']} ({t['signals_per_day']}/day) win {t['win_rate']}% avg {t['avg_net_r']:+}R "
              f"PF {t['profit_factor']} DD {t['max_drawdown_r']}R  strong {t.get('strong')}  standard {t.get('standard')}")
    if not write:
        return res
    raw = json.load(open(MODEL))
    tr, fr, be = PLANS[LIVE_PLAN]
    raw["version"] = "5.0"
    raw["levels"].update({"targets_r": tr, "fractions": [round(x, 6) for x in fr], "be_after": be,
                          "tp_atr": raw["levels"]["sl_atr"] * tr[-1]})
    raw["test"] = res[LIVE_PLAN]
    keep = ("signals", "signals_per_day", "win_rate", "avg_net_r", "total_net_r", "profit_factor", "max_drawdown_r")
    raw["exit_change"] = {
        "changed": "2026-11",
        "from": "one target at 1R, the whole position, the stop never moves (engine 3.2, Oct - Nov 2026)",
        "to": "targets 0.5R / 1R / 2R, a third at each, stop to the entry after target 1, to target 1 after target 2",
        "why": ("More signals end in profit: about 3 in 4 reach target 1, against about 6 in 10 that reached the single "
                "1R target. The price is a smaller average result per signal."),
        "holdout_one_target_1r": {k: res["t1"][k] for k in keep},
        "holdout_three_targets": {k: res["p3"][k] for k in keep},
        "holdout_one_target_half_r": {k: res["p1"][k] for k in keep},
    }
    raw["retest"] = ("Engine 5.0 (Nov 2026) uses the same trees as 3.1 / 3.2 with the three-target plan. The test above "
                     "uses 0.30% per round trip, funding for longs and the entry one minute after the candle close "
                     "(research/v5/engine4h.py).")
    with open(MODEL, "w", encoding="utf-8") as f:
        f.write(json.dumps(raw, indent=1) + "\n")
    print("wrote", MODEL)
    return res


if __name__ == "__main__":
    main(write=len(sys.argv) > 1 and sys.argv[1] == "write")
