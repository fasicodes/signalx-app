"""models/research.json: what the engine 5 research tested for every stream, what went live and why.

Reads the result files of lab5.py, rolling.py and engine4h.py (work/results/) and the two live model files.
The Signals page shows a stream's "reason" when the stream is switched off.

  python summary.py
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lab5 as L  # noqa: E402
from lab5 import ST, W  # noqa: E402

KEEP = ("period", "signals", "signals_per_day", "days_with_signal_pct", "win_rate", "stop_rate", "avg_net_r",
        "total_net_r", "profit_factor", "max_drawdown_r", "sharpe_daily", "avg_stop_pct", "years", "folds")
GO_LIVE = ("A stream goes live only when its one test on data it never saw has at least 50 signals, earns at least "
           "+0.05R per signal after real costs, has a profit factor of at least 1.1 and a drawdown smaller than half "
           "of its total result, and at least 70% of its signals reach their first target.")
PROTOCOL = ("Engine 5 research (research/v5): gradient-boosted models on the site's own features; label = the first "
            "target (0.5R) before the stop; stops wide enough for the costs (crypto 15m 6 ATR, 1h 3 ATR, 4h 2 ATR; "
            "forex 4 / 2.5 / 2 ATR). Walk-forward: every fold is scored by a model trained only on candles from before "
            "it. Plan, score level, cost cap and higher-timeframe check are chosen on the validation years only "
            "(crypto 2023-01 to 2025-09, forex 2017-01 to 2022-02), then the choice is scored once on the unseen "
            "period (crypto 2025-10 to 2026-11; forex 2023-04 to 2026-10). Costs: crypto 0.30% per round trip plus "
            "funding for longs, forex the pair's spread plus slippage; entries at the price one or two minutes after "
            "the signal candle closed. Data: Binance spot candles of 94 coins; forex candles of 12 pairs and gold.")


def short(s):
    return {k: s[k] for k in KEEP if s and k in s} if s else None


def pct(x):
    return f"{x:.1f}%"


def r(x):
    return f"{x:+.2f}R"


def load(name):
    p = f"{W}/results/{name}.json"
    return json.load(open(p)) if os.path.exists(p) else None


def setting(ch):
    c = ch["choice"]
    plan = {"p1": "one target at 0.5R", "p2": "two targets (0.5R / 1R)", "p3": "three targets (0.5R / 1R / 2R)"}[c["plan"]]
    out = f"{plan}, score in the model's top {100 - c['q'] * 100:g}%"
    if c.get("cap"):
        out += f", own cost at most {c['cap']}R"
    if ch.get("gate"):
        out += f", the 4-hour model in its top {100 - int(ch['gate']['name'][1:])}% for the same side"
    return out


def main():
    m4 = json.load(open(f"{L.APP}/signal_model.json"))
    m1 = json.load(open(f"{L.APP}/models/crypto_1h.json"))
    c4, c1, c15 = load("crypto_4h_choice"), load("crypto_1h_choice"), load("crypto_15m_choice")
    roll = load("crypto_1h_rolling")
    t4, t1 = m4["test"], m1["test"]
    ex = m4["exit_change"]
    streams = {}

    streams["crypto_4h"] = {
        "live": True, "chosen": "the 4-hour engine (the trees of engine 3.1) with three targets",
        "reason": (f"Live: the 4-hour engine with targets at 0.5R / 1R / 2R. On data it never saw ({t4['period']}): "
                   f"{t4['signals']} signals on {t4['coins']} coins (about {t4['signals_per_day']:.1f} a day), "
                   f"{pct(t4['win_rate'])} reached target 1 or better, {r(t4['avg_net_r'])} per signal after costs, "
                   f"profit factor {t4['profit_factor']}. With the single 1R target used from Oct to Nov 2026 the same "
                   f"signals won {pct(ex['holdout_one_target_1r']['win_rate'])} of the time and earned "
                   f"{r(ex['holdout_one_target_1r']['avg_net_r'])} per signal: more per signal, but 4 in 10 hit the stop."),
        "test": short(t4),
        "tiers": {"strong": t4.get("strong"), "standard": t4.get("standard")},
        "also_tested": [
            {"label": "the same signals with one target at 1R (engine 3.2)", "holdout": ex["holdout_one_target_1r"]},
            {"label": "the same signals with one target at 0.5R", "holdout": ex["holdout_one_target_half_r"]},
            {"label": f"a new 4-hour model of the engine 5 research ({setting(c4)}); it is the model the 1-hour stream reads",
             "validation": short(c4["walk_forward"]), "holdout": short(c4["test"]),
             "passed": ST.passed_test(dict(c4["test"], cost_round_trip=0.003), "crypto_4h")},
        ],
    }

    streams["crypto_1h"] = {
        "live": True, "chosen": "the 4-hour model of the engine 5 research, read at every hourly close",
        "reason": (f"Live: the 4-hour model read at every hourly close (one target at 0.5R). On data it never saw "
                   f"({t1['period']}): {t1['signals']} signals on {t1['symbols']} coins (about "
                   f"{t1['signals_per_day']:.1f} a day on average, in bursts: a signal on about "
                   f"{t1['days_with_signal_pct']:.0f}% of the days), {pct(t1['win_rate'])} reached the target, "
                   f"{r(t1['avg_net_r'])} per signal after costs, profit factor {t1['profit_factor']}. A separate 1-hour "
                   f"model was tested first and failed ({pct(c1['test']['win_rate'])} wins, {r(c1['test']['avg_net_r'])} "
                   "per signal on the same unseen period), so this was the second 1-hour variant scored on that period."),
        "validation": short(roll["validation"]), "test": short(t1),
        "by_hour_offset": {"validation": roll.get("validation_by_offset"), "holdout": roll.get("test_by_offset")},
        "also_tested": [
            {"label": f"a separate 1-hour model, stop 3 ATR ({setting(c1)})", "validation": short(c1["walk_forward"]),
             "holdout": short(c1["test"]), "passed": False},
            {"label": "a daily signal for quiet days (the best candidate after 24 hours without a signal)",
             "validation_standard_signals": [{"score_quantile": x["q"], **{k: x["standard"].get(k) for k in
                                              ("signals", "win_rate", "avg_net_r")}} for x in c1.get("daily_grid", [])],
             "passed": False},
        ],
    }

    d15 = c15.get("daily_grid", [])
    streams["crypto_15m"] = {
        "live": False, "chosen": None,
        "reason": (f"Switched off. A 15-minute model with a stop wide enough for the costs (6 ATR; {setting(c15)}) passed "
                   f"the validation years 2023-2025 ({pct(c15['walk_forward']['win_rate'])} wins, "
                   f"{r(c15['walk_forward']['avg_net_r'])} per signal) but not its one test on data it never saw "
                   f"({c15['test']['period']}): {c15['test']['signals']} signals, {pct(c15['test']['win_rate'])} wins, "
                   f"{r(c15['test']['avg_net_r'])} per signal after costs, with heavy losses in the crash of October 2025. "
                   "A daily signal for quiet days lost money on the validation years too. A 15-minute stream comes back "
                   "only when a model for it passes the same test."),
        "variants": [
            {"label": f"15-minute model, stop 6 ATR ({setting(c15)})", "validation_period": "2023-01 to 2025-09",
             "validation": short(c15["walk_forward"]), "holdout": short(c15["test"]), "passed": False},
            {"label": "a daily signal for quiet days", "validation_standard_signals": [
                {"score_quantile": x["q"], **{k: x["standard"].get(k) for k in ("signals", "win_rate", "avg_net_r")}}
                for x in d15], "passed": False},
        ],
    }

    for tf, name in (("15m", "15-minute"), ("1h", "1-hour"), ("4h", "4-hour")):
        ch = load(f"forex_{tf}_choice")
        cl = ch.get("closest") or {}
        gate = " with the 4-hour model agreeing" if cl.get("gate") else ""
        streams[f"forex_{tf}"] = {
            "live": False, "chosen": None,
            "reason": (f"Switched off. A new {name} model was trained on 2013-2021 candles of 12 pairs and gold and checked "
                       "year by year on 2017 to early 2022 with each pair's spread. No setting reached the validation "
                       f"target (73% wins and +0.05R per signal); the closest{gate} had {pct(cl['win_rate'])} wins and "
                       f"{r(cl['avg_net_r'])} per signal, so the unseen years 2023-2026 were not opened. Rule "
                       "strategies and public trading bots tested on 2023-2026 lost money as well (research/repo_lab). "
                       "Forex and gold signals come back only when a model passes the same test."),
            "variants": [{"label": f"{name} model, stop {ch['k']:g} ATR (plain and with the 4-hour model agreeing)",
                          "validation_period": "2017-01 to 2022-02", "validation": None,
                          "closest": {k: cl.get(k) for k in ("signals", "signals_per_day", "win_rate", "avg_net_r",
                                                             "profit_factor")}, "holdout": None, "passed": False}],
        }

    out = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "engine": "5.0", "rule": GO_LIVE, "protocol": PROTOCOL,
           "validation_rule": L.RULE_TEXT, "daily_signal_rule": L.DAILY_RULE_TEXT,
           "fixed_before_the_test": load("prereg"), "streams": streams,
           "live_check": ("The site's engine was run on the research candles at 14 moments of the unseen period: all "
                          "1,190 signals of the compared candles were the research's signals, with the same side, entry "
                          "and stop (research/v5/live_check.py).")}
    path = f"{L.APP}/models/research.json"
    json.dump(out, open(path, "w"), indent=1)
    for k, v in streams.items():
        print(k, "LIVE" if v["live"] else "OFF", "-", v["reason"][:400])
    print("wrote", path, os.path.getsize(path) // 1024, "KB")


if __name__ == "__main__":
    main()
