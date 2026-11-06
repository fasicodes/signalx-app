"""Derivatives lab - see PROTOCOL.md (pre-registered). Usage:
    python lab.py build 1h        # bars + features + labels for every coin -> work/feat_1h.pkl
    python lab.py val 1h          # walk-forward on 2022-2024, choose ONE candidate (no holdout numbers printed)
    python lab.py holdout 1h      # the one holdout test of the chosen candidate -> results.json
"""
import gzip, json, os, sys
import numpy as np
import pandas as pd
from numba import njit

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.path.join(HERE, "work")
COST = 0.003
TF = {"15m": ("15min", 32), "1h": ("1h", 36), "4h": ("4h", 30)}
SL_ATR, TARGET_R, MAX_PER_BAR = 2.0, 2.0, 3
VAL_START, HOLD_START = pd.Timestamp("2022-01-01"), pd.Timestamp("2025-01-01")
FOLDS = pd.date_range("2022-01-01", "2025-01-01", freq="6MS")
PARAMS = dict(n_estimators=300, learning_rate=0.03, num_leaves=31, min_child_samples=200, subsample=0.8,
              subsample_freq=1, colsample_bytree=0.8, verbose=-1, n_jobs=4)


def coins():
    return open(os.path.join(W, "coins.txt")).read().split()


def bars(coin, tf):
    k = pd.read_csv(os.path.join(W, "klines", f"{coin}_15m.csv.gz"))
    k["t"] = pd.to_datetime(k["open_ms"], unit="ms")
    k = k.set_index("t").sort_index()
    if tf != "15m":
        k = k.resample(TF[tf][0], label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
             "quote_volume": "sum", "taker_buy_volume": "sum"}).dropna()
    return k[["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume"]]


def derivs(coin):
    d = os.path.join(W, "derivs")
    m = pd.read_csv(os.path.join(d, f"{coin}_metrics_1h.csv.gz"), parse_dates=["hour_utc"]).set_index("hour_utc")
    p = pd.read_csv(os.path.join(d, f"{coin}_premium_1h.csv.gz"), parse_dates=["hour_utc"]).set_index("hour_utc")
    f = pd.read_csv(os.path.join(d, f"{coin}_funding.csv.gz"), parse_dates=["time_utc"]).set_index("time_utc")
    f = f[~f.index.duplicated()]
    h = m.join(p, how="outer").sort_index()
    h = h[~h.index.duplicated()]
    # last known funding rate at each hour
    h["funding"] = f["funding_rate"].reindex(h.index.union(f.index)).ffill().reindex(h.index)
    h.index = h.index + pd.Timedelta(hours=1)        # an hourly value is known only after its hour
    return h, f["funding_rate"]


def _z(s, n):
    r = s.rolling(n, min_periods=n // 3)
    return (s - r.mean()) / (r.std() + 1e-12)


@njit(cache=True)
def label(o, h, l, c, atr, max_bars, side, cost, fund_cum):
    """Net R of a trade signalled at bar i (entry next open) for every i; exit index; stop %."""
    n = len(o)
    out = np.full(n, np.nan)
    ex = np.full(n, -1)
    for i in range(n - 2):
        a = atr[i]
        if not (a > 0):
            continue
        e = o[i + 1]
        d = SL_ATR * a
        stop = e - side * d
        tgt = e + side * TARGET_R * d
        j_end = min(n - 1, i + max_bars)
        px = c[j_end]
        jx = j_end
        for j in range(i + 1, j_end + 1):
            if side > 0:
                if l[j] <= stop:
                    px = stop; jx = j; break
                if h[j] >= tgt:
                    px = tgt; jx = j; break
            else:
                if h[j] >= stop:
                    px = stop; jx = j; break
                if l[j] <= tgt:
                    px = tgt; jx = j; break
        gross = side * (px - e) / d
        fund = side * (fund_cum[jx] - fund_cum[i + 1]) * e / d     # long pays positive funding
        out[i] = gross - cost * e / d - fund
        ex[i] = jx
    return out, ex


def build(tf):
    rule, mb = TF[tf]
    frames = []
    for coin in coins():
        try:
            b = bars(coin, tf)
            hh, fund = derivs(coin)
        except FileNotFoundError:
            print("skip", coin); continue
        b = b[b.index >= "2021-01-01"]
        close_t = b.index + pd.Timedelta(rule)
        dv = hh.reindex(hh.index.union(close_t)).ffill().reindex(close_t)
        dv.index = b.index
        c = b["close"]
        tr = pd.concat([b["high"] - b["low"], (b["high"] - c.shift()).abs(), (b["low"] - c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
        x = pd.DataFrame(index=b.index)
        for k in (1, 4, 16, 64):
            x[f"ret{k}"] = np.log(c / c.shift(k)) / (atr / c)
        x["atr_pct"] = atr / c * 100
        dlt = c.diff()
        up, dn = dlt.clip(lower=0).ewm(alpha=1 / 14).mean(), (-dlt.clip(upper=0)).ewm(alpha=1 / 14).mean()
        x["rsi"] = 100 - 100 / (1 + up / (dn + 1e-12))
        for n in (20, 50, 200):
            x[f"ema{n}"] = (c - c.ewm(span=n).mean()) / atr
        x["vol_z"] = _z(np.log1p(b["quote_volume"]), 96)
        x["taker_buy"] = b["taker_buy_volume"] / (b["volume"] + 1e-12)
        per_day = {"15m": 96, "1h": 24, "4h": 6}[tf]
        x["funding"] = dv["funding"] * 1e4
        x["funding_z"] = _z(dv["funding"], 30 * per_day)
        oi = dv["sum_open_interest_value"]
        for k in (1, 4, 24):
            x[f"oi_chg{k}"] = np.log(oi / oi.shift(k))
        x["oi_chg24h"] = np.log(oi / oi.shift(per_day))
        x["oi_z"] = _z(np.log(oi), 30 * per_day)
        x["top_ls_z"] = _z(dv["sum_toptrader_long_short_ratio"], 30 * per_day)
        x["acc_ls_z"] = _z(dv["count_long_short_ratio"], 30 * per_day)
        x["taker_ls"] = dv["taker_long_short_vol_ratio_avg"]
        x["premium"] = dv["premium_index_close"] * 1e4
        x["premium_z"] = _z(dv["premium_index_close"], 30 * per_day)
        x["hi48"] = (c >= c.rolling(48).max()).astype(float) - (c <= c.rolling(48).min()).astype(float)
        # funding cumulative on bar index (funding events between bar opens)
        fc = fund.reindex(fund.index.union(b.index)).fillna(0).cumsum().reindex(b.index, method="ffill").fillna(0).values
        arr = [b[k].values.astype(np.float64) for k in ("open", "high", "low", "close")]
        for side, nm in ((1, "L"), (-1, "S")):
            r, e = label(*arr, atr.values, mb, side, COST, fc)
            x[f"y{nm}"], x[f"ex{nm}"] = r, e
        x["stop_pct"] = SL_ATR * atr / c.shift(-1).reindex(b.index) * 100
        x["coin"] = coin
        x["i"] = np.arange(len(x))
        x["t_exitL"] = pd.Series(b.index).reindex(x["exL"].clip(lower=0)).values
        x["t_exitS"] = pd.Series(b.index).reindex(x["exS"].clip(lower=0)).values
        frames.append(x.replace([np.inf, -np.inf], np.nan))
        print(coin, len(x), flush=True)
    df = pd.concat(frames).reset_index().rename(columns={"index": "t"})
    for k in ("funding_z", "oi_chg24h", "top_ls_z", "premium_z", "ret16", "ret64", "vol_z"):
        df[f"rk_{k}"] = df.groupby("t")[k].rank(pct=True)
    df = df[df["t"] >= "2021-02-01"].dropna(subset=["yL", "yS"])
    df.to_pickle(os.path.join(W, f"feat_{tf}.pkl"))
    print("rows", len(df))


FEATS = None


def feats(df):
    skip = {"t", "yL", "yS", "exL", "exS", "coin", "i", "t_exitL", "t_exitS", "stop_pct"}
    return [c for c in df.columns if c not in skip]


def select(df, score, side_col, thr):
    """Signals with score >= thr, one open trade per coin, <= MAX_PER_BAR per bar. Returns trades frame."""
    s = df.assign(score=score, side=side_col)
    s = s[s["score"] >= thr].sort_values(["t", "score"], ascending=[True, False])
    busy, taken, per_bar = {}, [], {}
    for row in s.itertuples(index=False):
        if busy.get(row.coin) is not None and row.t < busy[row.coin]:
            continue
        if per_bar.get(row.t, 0) >= MAX_PER_BAR:
            continue
        y = row.yL if row.side > 0 else row.yS
        te = row.t_exitL if row.side > 0 else row.t_exitS
        busy[row.coin] = te
        per_bar[row.t] = per_bar.get(row.t, 0) + 1
        taken.append((row.t, row.coin, row.side, y, row.stop_pct))
    return pd.DataFrame(taken, columns=["t", "coin", "side", "r", "stop_pct"])


def stats(tr):
    if tr.empty:
        return {"signals": 0}
    r = tr["r"].values
    eq = np.cumsum(r)
    dd = float((eq - np.maximum.accumulate(np.concatenate([[0], eq]))[1:]).min())
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    years = {str(y): {"signals": int(len(g)), "avg_net_r": round(float(g["r"].mean()), 3)}
             for y, g in tr.groupby(tr["t"].dt.year)}
    return {"signals": int(len(r)), "avg_net_r": round(float(r.mean()), 3), "total_net_r": round(float(r.sum()), 1),
            "profit_factor": round(float(pos / neg), 2) if neg > 0 else None, "max_drawdown_r": round(dd, 1),
            "win_rate": round(float((r > 0).mean() * 100), 1), "avg_stop_pct": round(float(tr["stop_pct"].mean()), 2),
            "years": years, "cost_round_trip": COST}


def rule_scores(df, name):
    """+1 long / -1 short / 0 for H1 and H2; score = 1 when a signal."""
    if name == "H1":
        lo = (df["funding_z"] < -2) & (df["top_ls_z"] < -1.5) & (df["oi_chg24h"] > np.log(1.1))
        sh = (df["funding_z"] > 2) & (df["top_ls_z"] > 1.5) & (df["oi_chg24h"] > np.log(1.1))
    else:
        lo = (df["hi48"] > 0) & (df["oi_chg24h"] > 0) & (df["funding"] < 0)
        sh = (df["hi48"] < 0) & (df["oi_chg24h"] > 0) & (df["funding"] > 0)
    side = np.where(lo, 1, np.where(sh, -1, 0))
    return np.where(side != 0, 1.0, 0.0), side


def model_preds(df, tf, fit_before, rows):
    import lightgbm as lgb
    mb = TF[tf][1]
    F = feats(df)
    tr = df[(df["t_exitL"] < fit_before) & (df["t_exitS"] < fit_before)]
    if tf == "15m":
        tr = tr.iloc[::3]
    out = {}
    for nm in ("L", "S"):
        m = lgb.LGBMRegressor(**PARAMS).fit(tr[F], tr[f"y{nm}"].clip(-3, 3))
        out[nm] = m.predict(rows[F])
    return out


def walk_forward(df, tf):
    parts = []
    for a, b in zip(FOLDS[:-1], FOLDS[1:]):
        rows = df[(df["t"] >= a) & (df["t"] < b)]
        p = model_preds(df, tf, a, rows)
        parts.append(pd.DataFrame({"pL": p["L"], "pS": p["S"]}, index=rows.index))
        print("fold", a.date(), flush=True)
    return pd.concat(parts)


def best_side(df, pr):
    side = np.where(pr["pL"].values >= pr["pS"].values, 1, -1)
    score = np.maximum(pr["pL"].values, pr["pS"].values)
    return score, side


def qualifies(st):
    return (st.get("signals", 0) >= 200 and st["avg_net_r"] >= 0.05 and (st["profit_factor"] or 0) >= 1.1
            and all(v["avg_net_r"] > 0 for k, v in st["years"].items() if k in ("2022", "2023", "2024"))
            and all(k in st["years"] for k in ("2022", "2023", "2024")))


def val(tf):
    df = pd.read_pickle(os.path.join(W, f"feat_{tf}.pkl"))
    v = df[(df["t"] >= VAL_START) & (df["t"] < HOLD_START)]
    res = {"tf": tf, "candidates": {}}
    pr = walk_forward(df, tf)
    score, side = best_side(v, pr.loc[v.index])
    for q in (0.99, 0.98, 0.95, 0.90):
        thr = float(np.quantile(score, q))
        st = stats(select(v, score, side, thr))
        st["thr"] = thr
        res["candidates"][f"M_top{round((1 - q) * 100)}"] = st
        print(tf, f"M top{round((1-q)*100)}%", {k: st.get(k) for k in ("signals", "avg_net_r", "profit_factor", "years")}, flush=True)
    for h in ("H1", "H2"):
        sc, sd = rule_scores(v, h)
        st = stats(select(v, sc, sd, 0.5))
        res["candidates"][h] = st
        print(tf, h, {k: st.get(k) for k in ("signals", "avg_net_r", "profit_factor", "years")}, flush=True)
    chosen = None
    # M: the lowest qualifying threshold = the largest share (10% before 5% ...)
    for k in ("M_top10", "M_top5", "M_top2", "M_top1"):
        if qualifies(res["candidates"][k]):
            chosen = k; break
    best = [chosen] if chosen else []
    best += [h for h in ("H1", "H2") if qualifies(res["candidates"][h])]
    best.sort(key=lambda k: -res["candidates"][k]["avg_net_r"])
    res["chosen"] = best[0] if best else None
    print(tf, "CHOSEN for holdout:", res["chosen"])
    json.dump(res, open(os.path.join(HERE, f"val_{tf}.json"), "w"), indent=1, default=str)


def holdout(tf):
    import streams
    res = json.load(open(os.path.join(HERE, f"val_{tf}.json")))
    ch = res["chosen"]
    out_path = os.path.join(HERE, "results.json")
    allr = json.load(open(out_path)) if os.path.exists(out_path) else {}
    if not ch:
        allr[tf] = {"chosen": None, "pass": False, "reason": "no candidate met the validation bar"}
    else:
        df = pd.read_pickle(os.path.join(W, f"feat_{tf}.pkl"))
        h = df[df["t"] >= HOLD_START]
        if ch.startswith("M"):
            p = model_preds(df, tf, HOLD_START, h)
            score = np.maximum(p["L"], p["S"]); side = np.where(p["L"] >= p["S"], 1, -1)
            st = stats(select(h, score, side, res["candidates"][ch]["thr"]))
        else:
            sc, sd = rule_scores(h, ch)
            st = stats(select(h, sc, sd, 0.5))
        st["period"] = f"{h['t'].min().date()} to {h['t'].max().date()}"
        st["pass"] = bool(streams.passed_test(st, f"crypto_{tf}"))
        allr[tf] = {"chosen": ch, "validation": res["candidates"][ch], "holdout": st, "pass": st["pass"]}
    json.dump(allr, open(out_path, "w"), indent=1, default=str)
    print(json.dumps(allr[tf], indent=1, default=str))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    {"build": build, "val": val, "holdout": holdout}[sys.argv[1]](sys.argv[2])
