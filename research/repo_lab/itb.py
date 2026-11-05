"""asavinov/intelligent-trading-bot, configs/config-sample-1h.jsonc, reproduced on the lab's data.

Same as the bot: features close SMA (1,3,6,12,24,168), LINEARREG_SLOPE (3..168), STDDEV (3..168), scaled;
labels high_30 / low_30 (price reaches +3% / -3% within 24 candles before moving 0.6% the other way);
trade_score = P(high) - P(low); buy above +threshold, sell below -threshold.
Differences (this environment cannot install TA-Lib / LightGBM): indicators are re-implemented in pandas, and the
classifier is the bot's own 'lc' algorithm (logistic regression, its 1-minute sample) instead of SVC.
Walk-forward like the bot's rolling_predict: retrain every 30 days on the previous 3 years only (minus the label
horizon), so every prediction is made by a model that never saw that candle's future.
Writes masks itb_lc_t{2,4,6,8,10}|L/S into the bots mask files of the 1h stream."""
import os
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab  # noqa: E402

TRAIN_LEN, STEP, HORIZON = 26280, 720, 24
THRESHOLDS = [0.02, 0.04, 0.06, 0.08, 0.10]


def linreg_slope(s, n):
    x = np.arange(n, dtype=float)
    x -= x.mean()
    w = x / (x ** 2).sum()
    v = np.convolve(s.to_numpy(float), w[::-1], mode="full")[:len(s)]
    v[:n - 1] = np.nan
    return pd.Series(v, index=s.index)


def features(d):
    c = d.close
    F = {}
    for n in (1, 3, 6, 12, 24, 168):
        F[f"close_SMA_{n}"] = c.rolling(n).mean()
    for n in (3, 6, 12, 24, 168):
        F[f"close_LINEARREG_SLOPE_{n}"] = linreg_slope(c, n)
        F[f"close_STDDEV_{n}"] = c.rolling(n).std(ddof=0)
    return pd.DataFrame(F)


def first_cross(d, up_pct, tol_pct, horizon):
    """1 when, within `horizon` candles, price moves up_pct (signed) before moving tol_pct the other way."""
    c, h, l = (d[k].to_numpy(float) for k in ("close", "high", "low"))
    n = len(c)
    out = np.zeros(n)
    sign = 1 if up_pct > 0 else -1
    for i in range(n - horizon):
        tgt = c[i] * (1 + up_pct / 100)
        bad = c[i] * (1 + tol_pct / 100)
        hh, ll = h[i + 1:i + 1 + horizon], l[i + 1:i + 1 + horizon]
        if sign > 0:
            hit, stop = hh >= tgt, ll <= bad
        else:
            hit, stop = ll <= tgt, hh >= bad
        fh = np.argmax(hit) if hit.any() else 10 ** 9
        fs = np.argmax(stop) if stop.any() else 10 ** 9
        out[i] = 1.0 if fh < fs else 0.0
    out[n - horizon:] = np.nan
    return out


def scores(d):
    X = features(d).to_numpy(float)
    y_hi = first_cross(d, 3.0, -0.6, HORIZON)
    y_lo = first_cross(d, -3.0, 0.6, HORIZON)
    n = len(d)
    out = np.full(n, np.nan)
    ok = np.isfinite(X).all(axis=1)
    start = 168 + 24 * 60          # at least ~2 months of training data before the first prediction
    for t0 in range(start, n, STEP):
        tr = np.arange(max(0, t0 - HORIZON - TRAIN_LEN), t0 - HORIZON)
        tr = tr[ok[tr] & np.isfinite(y_hi[tr])]
        te = np.arange(t0, min(n, t0 + STEP))
        te = te[ok[te]]
        if len(tr) < 500 or not len(te):
            continue
        sc = StandardScaler().fit(X[tr])
        Xtr, Xte = sc.transform(X[tr]), sc.transform(X[te])
        p = []
        for y in (y_hi, y_lo):
            yy = y[tr]
            if yy.min() == yy.max():
                p.append(np.full(len(te), yy[0]))
                continue
            m = LogisticRegression(C=1.0, max_iter=200).fit(Xtr, yy)
            p.append(m.predict_proba(Xte)[:, 1])
        out[te] = p[0] - p[1]
    return out


def run(market, sym):
    d = lab.frame(market, sym, "1h")
    s = pd.Series(scores(d))
    path = f"{lab.MK}/{market}_1h_{sym}.npz"
    z = dict(np.load(path))
    for t in THRESHOLDS:
        lg, sh = s > t, s < -t
        z[f"itb_lc_t{int(t * 100)}|L"] = np.asarray(lg & ~lg.shift(1, fill_value=False), bool)
        z[f"itb_lc_t{int(t * 100)}|S"] = np.asarray(sh & ~sh.shift(1, fill_value=False), bool)
    np.savez_compressed(path, **z)
    print("itb", market, sym, int(np.isfinite(s).sum()), "scored", flush=True)


if __name__ == "__main__":
    import strategies_bots
    lab.S = strategies_bots
    lab.MK = f"{lab.D}/masks_bots"
    from multiprocessing import Pool
    jobs = [(m, s) for m in ("crypto", "forex") for s in lab.symbols(m)]
    with Pool(2) as p:
        p.starmap(run, jobs)
