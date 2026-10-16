"""
Backtest of the "Professional AI Trading Prompt" (5 models) on the same crypto data and protocol as lab.py.

The prompt asks an LLM for: (1) Hawkes-process buying / selling intensity, (2) a Bayesian bull / bear classifier,
(3) quantile volatility, (4) conformal confidence with a trade / skip decision, (5) fractional-Kelly risk.
An LLM instruction cannot be backtested as text, so every part is rebuilt as a number:

  1 Hawkes     discrete-time exponential-kernel intensity of taker-buy and taker-sell volume (decay half-lives 3 and 12
               candles, scaled by the 100-candle mean volume); buying / selling pressure 0-10 = percentile of the
               intensity over the last 250 candles; excitation = total intensity vs normal.
  2 Bayes      Gaussian naive Bayes on quantile-normalised inputs (mirrored for shorts, trained walk-forward);
               a gradient-boosting model on the same inputs is the 'flexible' comparison.
  3 Volatility 90th percentile of ATR% over the last 500 candles; 'expected volatility' = current ATR% / that quantile;
               high volatility (ATR rank > 0.90) = WAIT.
  4 Conformal  trade only when the calibrated win probability is at least the confidence level (0.70) - the
               split-conformal 'singleton prediction set' rule - and the model is calibrated on the validation years.
  5 Kelly      risk = 0.25 x mu / sigma^2 of the R results (validation), floored at 0 and capped at 2 %.

  python prompt_lab.py pbuild 4h            # dataset: prompt inputs + Hawkes columns (ds/crypto_4hp)
  python prompt_lab.py run 4h nb            # walk-forward + holdout with naive Bayes ('hgb' = gradient boosting)
  python prompt_lab.py literal 4h nb        # the prompt 'as written': confidence >= 70 %, no threshold search
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab  # noqa: E402
from lab import CFG, FE, R, log  # noqa: E402

PROMPT_BASE = ["r3", "r6", "r12", "r24", "rsi14", "macd_h", "d_sma50", "d_sma200", "slope50", "adx", "di_diff",
               "atr_pct", "atr_rank", "bbw_rank", "f_tbr", "f_tbr_z", "f_cd6", "f_cd24", "f_tc_z"]
HAWKES = ["p_neg_imb3", "p_neg_imb12", "p_id_exc3", "p_id_exc12", "p_neg_pdiff", "p_id_psum", "p_id_volq"]

for tf in ("1h", "4h"):
    CFG[f"crypto_{tf}p"] = dict(CFG[f"crypto_{tf}f"])
    CFG[f"crypto_{tf}q"] = dict(CFG[f"crypto_{tf}f"])


def hawkes_block(b):
    v = b["volume"].astype(float).clip(lower=0)
    buy = np.minimum(b["taker_buy_volume"].astype(float).clip(lower=0), v)
    sell = v - buy
    vb = v.rolling(100, min_periods=50).mean().replace(0, np.nan)
    ub, us = buy / vb, sell / vb
    out = pd.DataFrame({"timestamp": pd.to_datetime(b["timestamp"]).to_numpy()})
    lam = {}
    for h in (3, 12):
        lb = ub.ewm(halflife=h, adjust=False).mean()
        ls = us.ewm(halflife=h, adjust=False).mean()
        lam[h] = (lb, ls)
        out[f"p_neg_imb{h}"] = ((lb - ls) / (lb + ls + 1e-9)).to_numpy()
        out[f"p_id_exc{h}"] = (lb + ls).to_numpy()

    def rank250(s):
        return s.rolling(250, min_periods=100).apply(lambda x: (x[:-1] < x[-1]).mean(), raw=True)
    bp = 10 * rank250(lam[3][0])
    sp = 10 * rank250(lam[3][1])
    out["p_neg_pdiff"] = (bp - sp).to_numpy()
    out["p_id_psum"] = (bp + sp).to_numpy()
    return out


def pbuild(tf, full=False):
    src = f"crypto_{tf}f"
    dst = f"crypto_{tf}{'q' if full else 'p'}"
    meta = json.load(open(f"{R}/ds/{src}/meta.json"))
    cols_all = meta["cols"]
    out = f"{R}/ds/{dst}"
    os.makedirs(out, exist_ok=True)
    keep_cols = cols_all if full else PROMPT_BASE
    ci = [cols_all.index(c) for c in keep_cols]
    for sym in meta["symbols"]:
        key = sym.replace("/", "")
        z = np.load(f"{R}/ds/{src}/{key}.npz")
        ts = z["ts"].astype("int64")
        xp = f"{R}/ds/{src}/X_{key}.npy"
        if not os.path.exists(xp):
            np.save(xp + ".tmp.npy", z["X"])
            os.replace(xp + ".tmp.npy", xp)
        X = np.load(xp, mmap_mode="r")
        base = np.asarray(X[:, ci], dtype=np.float32)
        raw = lab.load_crypto(sym, CFG[src]["data_start"], flow=True)
        b = FE.resample_closed(raw, tf)
        H = hawkes_block(b)
        bts = H["timestamp"].to_numpy().astype("datetime64[ns]").astype("int64")
        idx = np.searchsorted(bts, ts)
        idx = np.clip(idx, 0, len(bts) - 1)
        assert (bts[idx] == ts).all(), (sym, int((bts[idx] != ts).sum()))
        hk = np.column_stack([H[c].to_numpy(np.float32)[idx] for c in HAWKES if c != "p_id_volq"])
        atrp = pd.Series(base[:, keep_cols.index("atr_pct")] if "atr_pct" in keep_cols else X[:, cols_all.index("atr_pct")])
        q90 = atrp.rolling(500, min_periods=200).quantile(0.9).to_numpy(np.float32)
        volq = (atrp.to_numpy(np.float32) / np.where(q90 > 0, q90, np.nan)).astype(np.float32)
        Xn = np.column_stack([base, hk, volq]).astype(np.float32)
        d = {k: z[k] for k in z.files if k != "X"}
        np.savez(f"{out}/{key}.npz", X=Xn, **d)
        log("pbuild", dst, sym, Xn.shape)
    json.dump({"cols": keep_cols + HAWKES, "symbols": meta["symbols"]}, open(f"{out}/meta.json", "w"))
    log("pbuild done", dst)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class NB:
    """Quantile-normalised inputs + Gaussian naive Bayes (the 'Bayesian classifier')."""

    def __init__(self):
        from sklearn.naive_bayes import GaussianNB
        from sklearn.preprocessing import QuantileTransformer
        self.q = QuantileTransformer(n_quantiles=200, output_distribution="normal", subsample=300000, random_state=0)
        self.nb = GaussianNB()

    def fit(self, X, y):
        X = np.nan_to_num(np.asarray(X, np.float32), nan=0.0)
        self.q.fit(X)
        self.nb.fit(self.q.transform(X), y)
        return self

    def predict_proba(self, X):
        X = np.nan_to_num(np.asarray(X, np.float32), nan=0.0)
        return self.nb.predict_proba(self.q.transform(X))


def use_model(kind):
    if kind == "nb":
        lab.fit = lambda X, y: NB().fit(X, y)


_orig_flip = lab.flip_np


def flip_np(X, cols):
    """lab.flip_np plus the rule for the prompt columns: p_neg_* change sign, p_id_* stay."""
    key = ("p",) + tuple(cols)
    if key not in lab._FLIP_CACHE:
        ops = []
        base_ops = None
        if any(c.startswith("p_") for c in cols):
            plain = [c for c in cols if not c.startswith("p_")]
            base_ops = {}
            if plain:
                _orig_flip(np.zeros((2, len(plain)), np.float32), plain)
                bo = lab._FLIP_CACHE[tuple(plain)]
                base_ops = {plain[j]: (op, s) for j, (op, s) in enumerate(bo)}
                # swap ops refer to indices inside `plain`; map them back to indices in `cols`
            for j, c in enumerate(cols):
                if c.startswith("p_neg_"):
                    ops.append(("neg", j))
                elif c.startswith("p_"):
                    ops.append(("id", j))
                else:
                    op, s = base_ops[c]
                    ops.append((op, cols.index(plain[s]) if op == "swap" else j))
            lab._FLIP_CACHE[key] = ops
        else:
            return _orig_flip(X, cols)
    ops = lab._FLIP_CACHE[key]
    G = np.empty_like(X)
    for j, (op, s) in enumerate(ops):
        if op == "id":
            G[:, j] = X[:, j]
        elif op == "neg":
            G[:, j] = -X[:, j]
        elif op == "inv1":
            G[:, j] = 1 - X[:, j]
        elif op == "inv100":
            G[:, j] = 100 - X[:, j]
        else:
            G[:, j] = X[:, s]
    return G


lab.flip_np = flip_np


# ---------------------------------------------------------------------------
# the prompt as written
# ---------------------------------------------------------------------------
def kelly_from(trades, cap=0.02, frac=0.25):
    r = np.array([t["r"] for t in trades], float)
    if len(r) < 20 or r.var() <= 0:
        return 0.0
    return float(min(cap, max(0.0, frac * r.mean() / r.var())))


def equity(trades, risk):
    """Compound the trades in time order risking `risk` (fraction of equity per 1R) - returns total return and max drawdown."""
    tr = sorted(trades, key=lambda t: t["signal_at"])
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in tr:
        eq *= max(0.0, 1.0 + risk * t["r"])
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return {"total_return_pct": round((eq - 1) * 100, 1), "max_drawdown_pct": round(mdd * 100, 1)}


def literal(stream, g, conf=0.70, vol_cap=0.90):
    """Prompt as written: LONG / SHORT only when the calibrated probability >= conf (conformal singleton at
    alpha = 1 - conf), the volatility is not extreme (ATR rank <= vol_cap) and Kelly > 0; otherwise WAIT."""
    c = CFG[stream]
    cols, D = lab.load_ds(stream)
    preds = dict(np.load(f"{R}/wf/{stream}_{g}.npz"))
    ds, de = c["dev"]
    hs, he = c["hold"]
    vals, labs = [], []
    for sym, d in D.items():
        key = sym.replace("/", "")
        m = (d["ts"] >= np.datetime64(ds)) & (d["ts"] < np.datetime64(de)) & d["elig"]
        for side in ("L", "S"):
            vals.append(preds[f"{key}_{side}"][m])
            labs.append(d[f"y{side}_{g}"][m])
    pv, yv = np.concatenate(vals), np.concatenate(labs)
    ok = np.isfinite(pv) & (yv >= 0)
    cal = lab.calibration(pv, yv)
    # conformal check on the validation years: coverage of the 'win' decision
    grid_x = np.linspace(np.nanmin(pv[ok]), np.nanmax(pv[ok]), 2000)
    pc = cal(grid_x)
    thr = float(grid_x[np.argmax(pc >= conf)]) if (pc >= conf).any() else None
    res = {"stream": stream, "geom": g, "confidence": conf, "calibrated_share_above_conf": float((cal(pv[ok]) >= conf).mean())}
    if thr is None:
        res["note"] = "the model never reaches the confidence level on validation -> the prompt always says WAIT"
        return res
    ai = cols.index("atr_rank")

    def gate(sym, longs):
        return np.asarray(D[sym]["X"][:, ai], dtype=float) <= vol_cap
    dev_tr = lab.simulate(stream, g, D, preds, thr, ds, de, cal, gate=gate)
    hold_tr = lab.simulate(stream, g, D, preds, thr, hs, he, cal, gate=gate)
    k = kelly_from(dev_tr)
    res.update({"threshold_raw": thr, "validation": lab.stats(dev_tr, ds, de), "holdout": lab.stats(hold_tr, hs, he),
                "kelly_risk_pct": round(k * 100, 2)})
    if hold_tr:
        res["holdout_equity_kelly"] = equity(hold_tr, k)
        res["holdout_equity_flat1pct"] = equity(hold_tr, 0.01)
    return res


def main():
    cmd = sys.argv[1]
    if cmd == "pbuild":
        pbuild(sys.argv[2], full=len(sys.argv) > 3 and sys.argv[3] == "full")
    elif cmd == "run":                      # run <tf> <nb|hgb> [p|q]
        tf, kind = sys.argv[2], sys.argv[3]
        suffix = sys.argv[4] if len(sys.argv) > 4 else "p"
        stream = f"crypto_{tf}{suffix}"
        use_model(kind)
        g = "s2"
        out_name = f"{stream}_{kind}"
        # keep the model files of different models apart
        orig_np_save = np.savez
        CFG[out_name] = CFG[stream]
        os.makedirs(f"{R}/ds", exist_ok=True)
        if not os.path.exists(f"{R}/ds/{out_name}"):
            os.symlink(f"{R}/ds/{stream}", f"{R}/ds/{out_name}")
        lab.wf(out_name, g)
        lab.evaluate(out_name, g)
        res = literal(out_name, g)
        json.dump(res, open(f"{R}/results/{out_name}_literal.json", "w"), indent=1, default=str)
        log(out_name, "LITERAL", json.dumps({k: res.get(k) for k in ("threshold_raw", "kelly_risk_pct", "note")}))
        for part in ("validation", "holdout"):
            if part in res:
                s = res[part]
                log(out_name, "LITERAL", part, json.dumps({x: s.get(x) for x in ("signals", "signals_per_day", "win_rate", "avg_net_r", "profit_factor")}))
        for k2 in ("holdout_equity_kelly", "holdout_equity_flat1pct"):
            if k2 in res:
                log(out_name, k2, res[k2])
    elif cmd == "literal":
        tf, kind = sys.argv[2], sys.argv[3]
        stream = f"crypto_{tf}p_{kind}"
        CFG[stream] = CFG[f"crypto_{tf}p"]
        print(json.dumps(literal(stream, "s2"), indent=1, default=str))


if __name__ == "__main__":
    main()
