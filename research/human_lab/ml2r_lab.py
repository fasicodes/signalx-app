"""Loop round 7: a 2R engine built the way SignalX's tested crypto 4h engine (signal_engine.py v3.2) is built.

Why: after 83 tries the human chart rules (and simple trend rules) never survived 2025-2026, while engine 3.2 - a
statistical model on 82 measurements of every coin and of the whole market - did. So this round keeps 3.2's
measurements (signal_engine.coin_features / market_block / btc_block / assemble / flip_for_short) and changes the
plan to what the site wants: stop 2 ATR, ONE target at 2R (4 ATR), time limit 54 candles; pessimistic fills (the stop
first when a candle touches both, a gap through the stop exits at the open), 0.30% per round trip + funding for
longs.

Head trader: gradient boosting, trained walk-forward (half-years 2022-2024, only on trades that had ended), long
rows and mirrored short rows. A trade is taken when its expected result after costs, p*(2 - cost) - (1-p)*(1 + cost),
clears the threshold; SignalX portfolio rules. Validation rule as before (>= 80 trades, >= +0.05R, profit factor
>= 1.1, 2022, 2023 and 2024 each positive) + plateau (both neighbouring thresholds make money); the lowest passing
threshold is used.

Final test, fixed now and run once (python ml2r_lab.py final <fresh dir>): model re-trained on every trade that ended
before 2025-01-01; A) the 28 research coins 2025-01 .. 2026-10 must pass streams.passed_test() and be positive in
2025 AND 2026; B) 38 coins never used anywhere, same period, with the same market measurements (from the 28 coins),
must pass streams.passed_test().
"""
import json
import os
import sys
import warnings

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
warnings.filterwarnings("ignore")
import lab  # noqa: E402
import loop  # noqa: E402
import signal_engine as se  # noqa: E402
from chart_agents.risk import cost_in_r  # noqa: E402

SL_ATR, TGT_R, MAX_BARS, WARM = 2.0, 2.0, 54, 240
THRESHOLDS = [round(x, 2) for x in np.arange(-0.10, 0.81, 0.05)]


def load4h(d):
    out = {}
    for f in sorted(os.listdir(f"{d}/frames")):
        if f.endswith("_4h.pkl"):
            s = f.split("_")[0]
            df = pd.read_pickle(f"{d}/frames/{f}").sort_values("timestamp").reset_index(drop=True)
            out[s] = df[["timestamp", "open", "high", "low", "close", "volume"]]
    return out


def features(frames, market_from=None):
    F = {s: se.coin_features(df) for s, df in frames.items()}
    base = market_from or F
    mkt = se.market_block({f"{s}/USDT": f for s, f in base.items()})
    btc = se.btc_block(base["BTC"])
    X = {s: se.assemble(F[s], btc, mkt, f"{s}/USDT") for s in F}
    return F, X


def outcomes(F, side):
    """Every candle played out as a trade on `side`: (net R, target hit, candles held)."""
    c, h, lo, o = (F[k].to_numpy(float) for k in ("close", "high", "low", "open"))
    atr = F["atr"].to_numpy(float)
    n = len(c)
    net = np.full(n, np.nan)
    hit = np.zeros(n, bool)
    held = np.zeros(n, int)
    for i in range(WARM, n - 1):
        a = atr[i]
        if not a == a or a <= 0:
            continue
        e = c[i]
        risk = SL_ATR * a
        stop, tgt = e - side * risk, e + side * TGT_R * risk
        end = min(n - 1, i + MAX_BARS)
        if end < i + MAX_BARS:
            continue
        px = None
        for j in range(i + 1, end + 1):
            if side == 1:
                if o[j] <= stop:
                    px, k = o[j], "s"
                elif lo[j] <= stop:
                    px, k = stop, "s"
                elif h[j] >= tgt or o[j] >= tgt:
                    px, k = tgt, "t"
            else:
                if o[j] >= stop:
                    px, k = o[j], "s"
                elif h[j] >= stop:
                    px, k = stop, "s"
                elif lo[j] <= tgt or o[j] <= tgt:
                    px, k = tgt, "t"
            if px is not None:
                break
        if px is None:
            px, k, j = c[end], "x", end
        hours = (j - i) * 4
        net[i] = (px - e) * side / risk - cost_in_r("crypto", "X/USDT", e, risk, hours, side)
        hit[i] = k == "t"
        held[i] = j - i
    return net, hit, held


def table(F, X):
    """One row per (coin, candle, side) with features, label and result."""
    parts = []
    for s in F:
        f, x = F[s], X[s]
        ts = pd.to_datetime(f["timestamp"]).to_numpy("datetime64[ns]") + np.timedelta64(4, "h")
        for side in (1, -1):
            net, hit, held = outcomes(f, side)
            xx = x if side == 1 else se.flip_for_short(x)
            ok = np.isfinite(net)
            cost = np.array([cost_in_r("crypto", "X/USDT", 1.0, SL_ATR * f["atr_pct"].iloc[i], MAX_BARS * 2, side)
                             if ok[i] else np.nan for i in range(len(f))])
            d = pd.DataFrame(xx.to_numpy(float)[ok], columns=list(xx.columns))
            d["sym"], d["side"], d["t"] = s, side, ts[ok]
            d["exit_t"] = ts[ok] + held[ok].astype("timedelta64[h]") * 4
            d["net_r"], d["hit"], d["cost"] = net[ok], hit[ok], cost[ok]
            parts.append(d)
    return pd.concat(parts, ignore_index=True)


def fit(df, cols):
    from sklearn.ensemble import HistGradientBoostingClassifier
    m = HistGradientBoostingClassifier(max_depth=4, learning_rate=0.05, max_iter=300, min_samples_leaf=200,
                                       l2_regularization=1.0, random_state=0)
    m.fit(df[cols].to_numpy(float), df["hit"].to_numpy(int))
    return m


def ev(m, df, cols):
    p = m.predict_proba(df[cols].to_numpy(float))[:, 1]
    cst = df["cost"].to_numpy(float)
    return p * (TGT_R - cst) - (1 - p) * (1 + cst)


def as_trades(df, score):
    return [{"symbol": f"{r.sym}/USDT", "side": int(r.side), "t": np.datetime64(r.t, "ns"),
             "exit_t": np.datetime64(r.exit_t, "ns"), "net_r": float(r.net_r),
             "outcome": "target" if r.hit else "stop", "setups": ["ml2r"], "scores": {}, "_s": float(sc)}
            for r, sc in zip(df.itertuples(index=False), score)]


def select(df, score, th):
    m = score >= th
    tr = as_trades(df[m], score[m])
    return lab.portfolio(tr, [t["_s"] for t in tr], "4h")


def val():
    F, X = features(load4h(lab.W))
    T = table(F, X)
    cols = [c for c in T.columns if c not in ("sym", "side", "t", "exit_t", "net_r", "hit", "cost")]
    T = T[(T["t"] >= lab.VAL_START)].reset_index(drop=True)
    V = T[T["t"] < lab.HOLD_START].reset_index(drop=True)
    folds = pd.date_range("2022-01-01", "2025-01-01", freq="6MS").to_numpy("datetime64[ns]")
    sc = np.full(len(V), np.nan)
    for a, b in zip(folds[:-1], folds[1:]):
        tr = V[V["exit_t"] < a]
        te = (V["t"] >= a) & (V["t"] < b)
        m = fit(tr, cols)
        sc[te.to_numpy()] = ev(m, V[te], cols)
    ok = np.isfinite(sc)
    oof, s = V[ok].reset_index(drop=True), sc[ok]
    from sklearn.metrics import roc_auc_score
    print("walk-forward ranking skill (AUC):", round(roc_auc_score(oof["hit"], s), 3), "rows", len(oof))
    grid = []
    for th in THRESHOLDS:
        r = loop.judge(select(oof, s, th))
        grid.append((th, r))
    for k, (th, r) in enumerate(grid):
        nb = [grid[j][1] for j in (k - 1, k + 1) if 0 <= j < len(grid)]
        plateau = len(nb) == 2 and all((x.get("signals") or 0) >= 40 and (x.get("avg_net_r") or -1) > 0 and
                                       (x.get("profit_factor") or 0) >= 1.0 for x in nb)
        r["plateau"] = plateau
        r["pass"] = bool(r.get("promising") and plateau)
        loop.log({"tf": "4h", "name": f"ML 2R engine (3.2 measurements), expected R >= {th:+.2f}",
                  "exit": "stop 2 ATR, target 2R, 54 candles", "result": r})
    passing = [th for th, r in grid if r["pass"]]
    print("passing thresholds:", passing)
    return passing


def final(fresh_dir, th):
    research = load4h(lab.W)
    F, X = features(research)
    T = table(F, X)
    cols = [c for c in T.columns if c not in ("sym", "side", "t", "exit_t", "net_r", "hit", "cost")]
    train = T[(T["t"] >= lab.VAL_START) & (T["exit_t"] < lab.HOLD_START)]
    m = fit(train, cols)
    H = T[T["t"] >= lab.HOLD_START].reset_index(drop=True)
    ta = select(H, ev(m, H, cols), th)
    last = max(t["t"] for t in ta)
    sa = lab.stats(ta, float((last - lab.HOLD_START) / np.timedelta64(1, "D")))
    sa.update(period=f"2025-01-01 to {str(last)[:10]}", cost_round_trip=0.003)
    a_ok = bool(lab.passed_test(sa) and all(sa["years"].get(y, {}).get("avg_net_r", -1) > 0 for y in (2025, 2026)))
    fresh = {s: f for s, f in load4h(fresh_dir).items() if s not in research}
    Ff, Xf = features(fresh, market_from=F)
    Tf = table(Ff, Xf)
    Hf = Tf[Tf["t"] >= lab.HOLD_START].reset_index(drop=True)
    tb = select(Hf, ev(m, Hf, cols), th)
    sb = lab.stats(tb)
    sb.update(period=sa["period"], cost_round_trip=0.003)
    b_ok = bool(sb.get("signals") and lab.passed_test(sb))
    out = {"threshold": th, "A_research_coins_2025_2026": {"pass": a_ok, **sa},
           "B_fresh_coins_2025_2026": {"pass": b_ok, **sb}, "passed": a_ok and b_ok}
    with open(f"{HERE}/ml2r_final.json", "w") as fh:
        json.dump(out, fh, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    for k in ("A_research_coins_2025_2026", "B_fresh_coins_2025_2026"):
        s = out[k]
        print(k, {x: s.get(x) for x in ("pass", "signals", "win_rate", "avg_net_r", "profit_factor",
                                        "max_drawdown_r", "total_net_r", "signals_per_day")},
              {y: v["avg_net_r"] for y, v in (s.get("years") or {}).items()})
    print("PASSED" if out["passed"] else "FAILED")
    return out


if __name__ == "__main__" and sys.argv[1] in ("val", "final"):
    if sys.argv[1] == "val":
        val()
    else:
        final(sys.argv[2], float(sys.argv[3]))


def outcomes_plan(F, side, plan):
    """plan '1R': one target at 1R. plan 'half': half closed at 1R, the stop moves to the entry, the other half
    aims for 2R ("up to 2R"). Stop 2 ATR, 54 candles, pessimistic fills. Returns (net R, TP1 reached, held)."""
    c, h, lo, o = (F[k].to_numpy(float) for k in ("close", "high", "low", "open"))
    atr = F["atr"].to_numpy(float)
    n = len(c)
    net = np.full(n, np.nan)
    hit1 = np.zeros(n, bool)
    held = np.zeros(n, int)
    for i in range(WARM, n - 1):
        a = atr[i]
        if not a == a or a <= 0:
            continue
        end = min(n - 1, i + MAX_BARS)
        if end < i + MAX_BARS:
            continue
        e, risk = c[i], SL_ATR * a
        stop, t1, t2 = e - side * risk, e + side * risk, e + side * 2 * risk
        got1, res, j = False, None, end
        for j in range(i + 1, end + 1):
            hi_, lo_, op = (h[j], lo[j], o[j])
            adverse = (op <= stop or lo_ <= stop) if side == 1 else (op >= stop or hi_ >= stop)
            if adverse:
                px = (min(op, stop) if side == 1 else max(op, stop))
                r_stop = (px - e) * side / risk
                res = r_stop if not got1 else 0.5 * 1.0 + 0.5 * r_stop
                break
            fav1 = (hi_ >= t1) if side == 1 else (lo_ <= t1)
            fav2 = (hi_ >= t2) if side == 1 else (lo_ <= t2)
            if not got1 and fav1:
                got1 = True
                if plan == "1R":
                    res = 1.0
                    break
                stop = e                                   # the rest is protected at the entry
                if fav2:                                   # both on one candle: still pessimistic -> only TP1 now
                    pass
                continue
            if got1 and plan == "half" and fav2:
                res = 0.5 * 1.0 + 0.5 * 2.0
                break
        if res is None:
            r_end = (c[end] - e) * side / risk
            res = r_end if not got1 else (1.0 if plan == "1R" else 0.5 + 0.5 * r_end)
            j = end
        net[i] = res - cost_in_r("crypto", "X/USDT", e, risk, (j - i) * 4, side)
        hit1[i] = got1
        held[i] = j - i
    return net, hit1, held


def val_plans():
    """Loop round 8: same measurements, the model learns 'TP1 (1R) before the stop' like engine 3.2; the same
    selections are then played with three plans: 1R, half at 1R + half at 2R, and 2R."""
    F, X = features(load4h(lab.W))
    parts = []
    for s in F:
        f, x = F[s], X[s]
        ts = pd.to_datetime(f["timestamp"]).to_numpy("datetime64[ns]") + np.timedelta64(4, "h")
        for side in (1, -1):
            xx = x if side == 1 else se.flip_for_short(x)
            r1, h1, d1 = outcomes_plan(f, side, "1R")
            rh, hh, dh = outcomes_plan(f, side, "half")
            r2, h2, d2 = outcomes(f, side)
            ok = np.isfinite(r1) & np.isfinite(rh) & np.isfinite(r2)
            d = pd.DataFrame(xx.to_numpy(float)[ok], columns=list(xx.columns))
            d["sym"], d["side"], d["t"] = s, side, ts[ok]
            d["hit"] = h1[ok]
            for k, (r, dd) in {"1R": (r1, d1), "half": (rh, dh), "2R": (r2, d2)}.items():
                d[f"net_{k}"] = r[ok]
                d[f"exit_{k}"] = ts[ok] + dd[ok].astype("timedelta64[h]") * 4
            d["cost"] = [cost_in_r("crypto", "X/USDT", 1.0, SL_ATR * f["atr_pct"].iloc[i], MAX_BARS * 2, side)
                         for i in np.flatnonzero(ok)]
            parts.append(d)
    T = pd.concat(parts, ignore_index=True)
    T.to_pickle(f"{lab.W}/ml_plans_table.pkl")
    skip = {"sym", "side", "t", "hit", "cost"} | {c for c in T.columns if c.startswith(("net_", "exit_"))}
    cols = [c for c in T.columns if c not in skip]
    V = T[(T["t"] >= lab.VAL_START) & (T["t"] < lab.HOLD_START)].reset_index(drop=True)
    folds = pd.date_range("2022-01-01", "2025-01-01", freq="6MS").to_numpy("datetime64[ns]")
    p = np.full(len(V), np.nan)
    for a, b in zip(folds[:-1], folds[1:]):
        tr = V[V["exit_1R"] < a]
        te = ((V["t"] >= a) & (V["t"] < b)).to_numpy()
        p[te] = fit(tr, cols).predict_proba(V[te][cols].to_numpy(float))[:, 1]
    ok = np.isfinite(p)
    oof, pp = V[ok].reset_index(drop=True), p[ok]
    from sklearn.metrics import roc_auc_score
    print("walk-forward skill for TP1 (AUC):", round(roc_auc_score(oof["hit"], pp), 3))
    for q in (0.90, 0.95, 0.97, 0.98, 0.99):
        th = float(np.quantile(pp, q))
        for plan in ("1R", "half", "2R"):
            m = pp >= th
            sel = oof[m]
            tr = [{"symbol": f"{r.sym}/USDT", "side": int(r.side), "t": np.datetime64(r.t, "ns"),
                   "exit_t": np.datetime64(getattr(r, f"exit_{plan}"), "ns"), "net_r": float(getattr(r, f"net_{plan}")),
                   "outcome": "target" if getattr(r, f"net_{plan}") > 0 else "stop", "setups": ["ml"], "scores": {}}
                  for r in sel.itertuples(index=False)]
            res = loop.judge(lab.portfolio(tr, list(pp[m]), "4h"))
            loop.log({"tf": "4h", "name": f"ML (3.2 measurements, learns TP1) top {100 - q * 100:.0f}% | plan {plan}",
                      "exit": plan, "result": res})


if __name__ == "__main__" and sys.argv[1] == "plans":
    val_plans()
