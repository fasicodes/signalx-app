"""
Backtest of the 'Professional AI Trading Prompt' exactly as it was coded in the site's first engine (main.py:
hawkes_pressure, bayesian_bullish_bearish, conformal_confidence, quantile_volatility, fractional_kelly).

The original verdict (git commit aa3b9e8, generate_signal): conformal_confidence() says SKIP -> WAIT, otherwise
LONG if bullish% > bearish% else SHORT; stop / target from quantile_volatility (stop = 95th percentile of the absolute
candle return, target = 1.5 x that); risk from fractional_kelly.  Every function is run on the last 200 closed
candles, the same window the site uses.

  python legacy_prompt.py parity 4h         # vectorised version == the real main.py functions on random windows
  python legacy_prompt.py run 4h            # the backtest (nothing is fitted, so 2023-2026 are all out of sample)
"""
import ast
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
APP = os.path.dirname(HERE)
R = os.environ.get("RESEARCH_DIR") or os.path.join(HERE, "work")
WIN = 200
V0 = pd.Timestamp("2023-01-01")
V1 = pd.Timestamp("2025-10-01")
H1 = pd.Timestamp("2026-10-11")
COST = 0.0012                     # round trip, share of the position (same as the other crypto tests)
MAXBARS = {"1h": 48, "4h": 54}
TFH = {"1h": 1, "4h": 4}


def real_functions():
    """The functions of main.py, loaded from its source text (importing main.py would start the whole app)."""
    src = open(f"{APP}/main.py", encoding="utf-8").read()
    tree = ast.parse(src)
    want = {"_ta_rsi", "_ta_macd", "hawkes_pressure", "bayesian_bullish_bearish", "conformal_confidence",
            "quantile_volatility", "fractional_kelly"}
    ns = {"np": np, "pd": pd}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            exec(compile(ast.Module([node], []), "main.py", "exec"), ns)
    return ns


def py_round1(a):
    return np.array([round(v, 1) for v in np.asarray(a, float).tolist()])


def legacy_chunk(W):
    """W: (m, 200) closes of the last 200 closed candles for m bars -> dict of arrays (one value per bar)."""
    m, n = W.shape
    d = np.diff(W, axis=1)
    gain = np.clip(d, 0, None)
    loss = -np.clip(d, None, 0)
    a = 1.0 / 14
    ag = np.full((m, n), np.nan)
    al = np.full((m, n), np.nan)
    ag[:, 1], al[:, 1] = gain[:, 0], loss[:, 0]
    for k in range(2, n):
        ag[:, k] = (1 - a) * ag[:, k - 1] + a * gain[:, k - 1]
        al[:, k] = (1 - a) * al[:, k - 1] + a * loss[:, k - 1]
    ag[:, :14] = np.nan
    al[:, :14] = np.nan
    with np.errstate(all="ignore"):
        rs = ag / np.where(al == 0, np.nan, al)
        rsi = 100 - 100 / (1 + rs)
        rsi = np.where((al != 0) | (ag != 0), rsi, 100.0)
    ef = W[:, 0].copy()
    es = W[:, 0].copy()
    macd = np.zeros((m, n))
    for k in range(1, n):
        ef = ef + (2 / 13) * (W[:, k] - ef)
        es = es + (2 / 27) * (W[:, k] - es)
        macd[:, k] = ef - es
    sig = np.zeros((m, n))
    sig[:, 0] = macd[:, 0]
    for k in range(1, n):
        sig[:, k] = sig[:, k - 1] + 0.2 * (macd[:, k] - sig[:, k - 1])
    valid = np.isfinite(rsi)
    valid[:, :14] = False
    bucket = np.where(rsi <= 0, -1, np.where(rsi <= 35, 0, np.where(rsi <= 65, 1, 2)))
    key = np.where(valid & (bucket >= 0), bucket * 2 + (macd > sig), -1)
    up = np.zeros((m, n), bool)
    up[:, :-1] = W[:, 1:] > W[:, :-1]
    cur = key[:, -1]
    match = valid & (key == cur[:, None]) & (cur[:, None] >= 0)
    cnt = match.sum(axis=1)
    ups = (match & up).sum(axis=1)
    nvalid = valid.sum(axis=1)
    base = (valid & up).sum(axis=1) / np.maximum(nvalid, 1)
    prob = np.where(cnt >= 5, ups / np.maximum(cnt, 1), base)
    bull = py_round1(prob * 100)
    bear = py_round1(100 - bull)
    # Hawkes: last 40 returns of the valid rows
    ret = W[:, 1:] / W[:, :-1] - 1
    r40 = ret[:, -40:]
    thr = np.quantile(np.abs(r40), 0.70, axis=1)
    mu, alpha, beta = 0.1, 0.6, 0.4
    wts = alpha * np.exp(-beta * (39 - np.arange(40)))
    buy = mu + ((r40 > thr[:, None]) * wts).sum(axis=1)
    sell = mu + ((r40 < -thr[:, None]) * wts).sum(axis=1)
    maxp = mu + alpha * 40
    bp = np.array([min(10, round(v, 1)) for v in (buy / maxp * 10).tolist()])
    sp = np.array([min(10, round(v, 1)) for v in (sell / maxp * 10).tolist()])
    # conformal
    b_long = bull > 50
    h_long = bp > sp
    votes_long = b_long.astype(int) + h_long.astype(int)
    agree = np.maximum(votes_long, 2 - votes_long) / 2
    strength = np.abs(bull - 50) / 50
    conf = np.clip(agree * 0.6 + strength * 0.4, 0, 1)
    trade = conf >= 0.55
    verdict = np.where(~trade, 0, np.where(bull > bear, 1, -1))
    # quantile volatility over the valid rows (185 returns)
    vr = np.abs(ret[:, 14:])
    q95 = np.quantile(vr, 0.95, axis=1)
    q50 = np.quantile(vr, 0.50, axis=1)
    ok = valid[:, -1] & (key[:, -1] >= 0)
    verdict = np.where(ok, verdict, 0)
    return {"bull": bull, "bear": bear, "bp": bp, "sp": sp, "conf": np.round(conf * 100, 1), "verdict": verdict,
            "q95": q95, "q50": q50, "ok": ok}


def run_all(c, chunk=20000):
    """Verdicts for every bar t >= 199 of the close series c (window = c[t-199 .. t])."""
    from numpy.lib.stride_tricks import sliding_window_view as swv
    n = len(c)
    out = {k: [] for k in ("bull", "bear", "bp", "sp", "conf", "verdict", "q95", "q50", "ok")}
    if n < WIN:
        return None
    Wall = swv(c, WIN)
    for i in range(0, len(Wall), chunk):
        res = legacy_chunk(np.ascontiguousarray(Wall[i:i + chunk]))
        for k in out:
            out[k].append(res[k])
    full = {}
    for k, v in out.items():
        a = np.concatenate(v)
        pad = np.full(WIN - 1, 0 if k in ("verdict",) else (False if k == "ok" else np.nan))
        full[k] = np.concatenate([pad, a])
    return full


def parity(tf, samples=300):
    ns = real_functions()
    rng = np.random.default_rng(3)
    syms = ["BTC", "ETH", "SOL", "DOGE", "ADA", "LINK"]
    bad = {"verdict": 0, "bull": 0, "bp": 0, "sp": 0, "conf": 0, "q95": 0}
    total = 0
    for s in syms:
        z = np.load(f"{R}/ds/crypto_{tf}/{s}USDT.npz")
        c, ts = z["c"].astype(float), z["ts"]
        full = run_all(c)
        for t in rng.integers(300, len(c), samples // len(syms)):
            df = pd.DataFrame({"close": c[t - WIN + 1:t + 1]})
            df["rsi"] = ns["_ta_rsi"](df["close"], 14)
            mc, sg = ns["_ta_macd"](df["close"])
            df["macd"], df["macd_signal"] = mc, sg
            df = df.dropna(subset=["rsi", "macd", "macd_signal"]).reset_index(drop=True)
            bp, sp = ns["hawkes_pressure"](df)
            bull, bear = ns["bayesian_bullish_bearish"](df)
            conf, dec = ns["conformal_confidence"](bull, bp, sp)
            verdict = 0 if dec == "SKIP" else (1 if bull > bear else -1)
            q = ns["quantile_volatility"](df, float(df["close"].iloc[-1]), "LONG")
            total += 1
            bad["verdict"] += int(verdict != full["verdict"][t])
            bad["bull"] += int(abs(bull - full["bull"][t]) > 1e-9)
            bad["bp"] += int(abs(bp - full["bp"][t]) > 1e-9)
            bad["sp"] += int(abs(sp - full["sp"][t]) > 1e-9)
            bad["conf"] += int(abs(conf - full["conf"][t]) > 1e-9)
            ext = q["extreme_volatility_95_pct"] / 100
            bad["q95"] += int(abs(round(full["q95"][t] * 100, 2) / 100 - ext) > 1e-9)
    print("windows compared:", total, "mismatches:", bad)
    return total, bad


# ---------------------------------------------------------------------------
# trades
# ---------------------------------------------------------------------------
def own_plan_trades(ts, o, h, l, c, F, tf):
    """The prompt's own plan: stop = q95 of the absolute candle return, target = 1.5 x that, one trade per coin
    at a time. Returns a list of dicts (entry bar index, side, result in R after costs)."""
    H = MAXBARS[tf]
    n = len(c)
    idx = np.nonzero((F["verdict"] != 0) & F["ok"] & np.isfinite(F["q95"]) & (F["q95"] > 0))[0]
    idx = idx[idx < n - 1]
    side = F["verdict"][idx].astype(int)
    x = F["q95"][idx]
    entry = c[idx]
    stop = entry * (1 - side * x)
    tp = entry * (1 + side * 1.5 * x)
    m = len(idx)
    exit_j = np.full(m, -1)
    status = np.zeros(m, int)       # 1 = TP, -1 = SL, 0 = expired
    done = np.zeros(m, bool)
    for k in range(1, H + 1):
        j = idx + k
        valid = (j < n) & ~done
        jj = np.minimum(j, n - 1)
        hit_sl = np.where(side > 0, l[jj] <= stop, h[jj] >= stop) & valid
        hit_tp = np.where(side > 0, h[jj] >= tp, l[jj] <= tp) & valid
        sl_first = hit_sl                      # a candle touching both counts as the stop
        tp_only = hit_tp & ~hit_sl
        status[sl_first] = -1
        status[tp_only] = 1
        exit_j[sl_first | tp_only] = jj[sl_first | tp_only]
        done |= sl_first | tp_only
    exp = ~done
    jx = np.minimum(idx + H, n - 1)
    exit_j[exp] = jx[exp]
    exit_price = np.where(status == 1, tp, np.where(status == -1, stop, c[jx]))
    gross = side * (exit_price - entry) / (x * entry)           # in R
    net = gross - COST / x
    out = []
    busy = -1
    for i in range(m):
        if idx[i] <= busy:
            continue
        # a trade whose end is unknown (data ends) is skipped
        if status[i] == 0 and idx[i] + H >= n:
            continue
        busy = exit_j[i]
        out.append({"i": int(idx[i]), "ts": ts[idx[i]], "side": int(side[i]), "status": int(status[i]), "r": float(net[i]),
                    "risk_pct": float(x[i] * 100)})
    return out


def summarize(trs, start, end):
    t = [x for x in trs if start <= pd.Timestamp(x["ts"]) < end]
    days = (end - start).days
    if not t:
        return {"signals": 0}
    r = np.array([x["r"] for x in t])
    st = np.array([x["status"] for x in t])
    gp, gl = r[r > 0].sum(), -r[r < 0].sum()
    yrs = {}
    for x in t:
        yrs.setdefault(str(pd.Timestamp(x["ts"]).year), []).append(x)
    return {"signals": len(t), "signals_per_day": round(len(t) / days, 2), "win_rate": round(float((st == 1).mean() * 100), 1),
            "stop_rate": round(float((st == -1).mean() * 100), 1), "expired_rate": round(float((st == 0).mean() * 100), 1),
            "avg_net_r": round(float(r.mean()), 3), "profit_factor": round(float(gp / gl), 2) if gl > 0 else None,
            "avg_stop_pct": round(float(np.mean([x["risk_pct"] for x in t])), 2),
            "years": {y: {"signals": len(v), "win_rate": round(float(np.mean([q["status"] == 1 for q in v]) * 100), 1),
                          "avg_net_r": round(float(np.mean([q["r"] for q in v])), 3)} for y, v in sorted(yrs.items())}}


def run(tf):
    import lab
    from lab import FE
    syms = sorted(json.load(open(f"{R}/ds/crypto_{tf}/meta.json"))["symbols"])
    own, nxt_rows, cal_rows = [], [], []
    std_data = {}
    for sym in syms:
        key = sym.replace("/", "")
        z = np.load(f"{R}/ds/crypto_{tf}/{key}.npz")
        ts = z["ts"].astype("datetime64[ns]")
        o, h, l, c, atr = (z[k].astype(float) for k in ("o", "h", "l", "c", "atr"))
        F = run_all(c)
        if F is None:
            continue
        for x in own_plan_trades(ts, o, h, l, c, F, tf):
            x["sym"] = sym
            own.append(x)
        # next-candle direction accuracy and calibration of Bullish %
        nu = np.zeros(len(c), bool)
        nu[:-1] = c[1:] > c[:-1]
        sel = np.arange(len(c)) < len(c) - 1
        nxt_rows.append(pd.DataFrame({"ts": ts[sel], "verdict": F["verdict"][sel], "bull": F["bull"][sel], "up": nu[sel], "ok": F["ok"][sel]}))
        # standard plan data for the common policy (stop 2 ATR, targets 0.5 / 1 / 2 R)
        side = np.where(F["verdict"] > 0, "LONG", np.where(F["verdict"] < 0, "SHORT", "")).astype(object)
        side[~F["ok"]] = ""
        std_data[sym] = {"ts": ts, "o": o, "h": h, "l": l, "c": c, "atr": atr, "side": side,
                         "score": np.nan_to_num(F["conf"], nan=0.0), "conf": np.nan_to_num(F["conf"], nan=0.0),
                         "grade": np.full(len(c), "C", dtype=object), "setup": None, "dist": 2.0 * atr}
        print(tf, sym, "verdict bars", int((F["verdict"] != 0).sum()), "of", len(c), flush=True)
    res = {"tf": tf, "rule": "main.py prompt engine, last 200 closed candles, nothing fitted"}
    for name, (a, b) in {"validation_years": (V0, V1), "holdout": (V1, H1)}.items():
        res[f"own_plan_{name}"] = summarize(own, a, b)
    # next candle accuracy
    N = pd.concat(nxt_rows)
    N = N[N.ok]
    for name, (a, b) in {"validation_years": (V0, V1), "holdout": (V1, H1)}.items():
        S = N[(N.ts >= np.datetime64(a)) & (N.ts < np.datetime64(b))]
        tr = S[S.verdict != 0]
        hit = ((tr.verdict > 0) == tr.up).mean()
        res[f"next_candle_{name}"] = {"all_bars": int(len(S)), "trade_bars": int(len(tr)), "share_trade": round(len(tr) / max(len(S), 1) * 100, 1),
                                      "up_rate_all": round(float(S.up.mean() * 100), 2), "direction_accuracy_trade": round(float(hit * 100), 2),
                                      "long_accuracy": round(float(tr[tr.verdict > 0].up.mean() * 100), 2) if (tr.verdict > 0).any() else None,
                                      "short_accuracy": round(float((~tr[tr.verdict < 0].up).mean() * 100), 2) if (tr.verdict < 0).any() else None}
        bins = pd.cut(S.bull, [0, 30, 40, 45, 50, 55, 60, 70, 100])
        res[f"bullish_pct_reliability_{name}"] = {str(k): {"bars": int(v["n"]), "actual_up_pct": round(float(v["u"] * 100), 2)}
                                                  for k, v in S.groupby(bins, observed=True).agg(n=("up", "size"), u=("up", "mean")).iterrows()}
    # the same verdicts in the common plan (stop 2 ATR, 0.5 / 1 / 2 R, one trade per coin, same costs as the streams)
    plan = {"targets_r": lab.TARGETS_R, "fractions": lab.FRACTIONS, "be_after": lab.BE_AFTER, "max_bars": MAXBARS[tf],
            "tf_sec": FE.TF_SEC[tf]}
    cfgk = "crypto_" + tf
    policy = {"max_per_bar": lab.CFG[cfgk]["max_per_bar"], "cooldown": lab.CFG[cfgk]["cooldown"], "sessions": None,
              "no_new_before_close_h": 0}
    for name, (a, b) in {"validation_years": (V0, V1), "holdout": (V1, H1)}.items():
        active, last, closed = lab.ST.run_policy(std_data, "crypto", tf, plan, policy, start_time=a, end_time=b, lookback=10 ** 9)
        trades = [t for lst in closed.values() for t in lst]
        s = lab.stats(trades, a, b)
        res[f"common_plan_{name}"] = {k: s.get(k) for k in ("signals", "signals_per_day", "win_rate", "avg_net_r", "profit_factor", "years")}
    # fractional Kelly as coded: win_prob = max(bullish, bearish)/100, reward/risk 1.5, k = 0.5, cap 5 %
    json.dump(res, open(f"{R}/results/legacy_prompt_{tf}.json", "w"), indent=1, default=str)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    if sys.argv[1] == "parity":
        parity(sys.argv[2])
    elif sys.argv[1] == "run":
        run(sys.argv[2])
