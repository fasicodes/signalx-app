"""
Model features for the signal streams (15m / 1h / 4h, crypto and forex).

The SAME code builds the features for the research (backtests and
walk-forward tests) and for the live site, so what was tested is what runs.
Everything is scale-free (distances in ATR, percent ranks, oscillators), so
one model works for every coin or pair of a stream.

Per symbol, from closed candles of the stream's timeframe (oldest first):
  * base block: signal_engine.base_features (trend, momentum, volatility,
    range position, candle shape, volume) on the stream timeframe;
  * higher-timeframe blocks, "h_" and "D_" (15m -> 1h and 4h,
    1h -> 4h and 1d, 4h -> 1d), built from the same candles; each
    candle only sees higher-timeframe candles that had CLOSED by its close;
  * long-horizon returns and z-scores (r42, r180, z6..z180);
  * session block (hour and weekday in New York time, London / New York
    session flags) for 15m and 1h;
  * crypto: market block (median and dispersion of every coin's z-scores,
    breadth, the coin's rank vs the others) and the Bitcoin block ("b_", "bh_");
  * forex: currency-strength block (the base and quote currency's average
    move against the other pairs).
Shorts use the mirror image of the features (signal_engine.flip_for_short),
so one model scores both sides.
"""
import re
import warnings

import numpy as np
import pandas as pd

import markets
from signal_engine import EPS, _HTF_KEEP, base_features, flip_for_short  # noqa: F401  (re-exported)

TF_SEC = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}
RULE = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D", "1w": "W-MON"}
HTFS = {"15m": (("1h", "h_"), ("4h", "D_")), "1h": (("4h", "h_"), ("1d", "D_")), "4h": (("1d", "h_"),)}
# candles of the stream timeframe the live engine keeps per symbol (enough for the slowest feature)
HISTORY = {"15m": 2600, "1h": 3700, "4h": 1000}
ZK = (6, 24, 42, 180)
_ZC = [f"z{k}" for k in ZK]
CS_K = (6, 24, 42)
# order-flow inputs (crypto): taker buy volume and trade counts from the exchange's candles
FLOW_RAW = ["quote_volume", "trade_count", "taker_buy_volume"]
FLOW_COLS = ["f_tbr", "f_tbr_z", "f_cd6", "f_cd24", "f_sz_z", "f_tc_z"]


def epoch_s(ts):
    return pd.to_datetime(pd.Series(ts)).astype("datetime64[s]").astype("int64").to_numpy()


def resample_closed(df, tf, last_close=None, min_share=0.25):
    """OHLCV candles of timeframe `tf` from smaller candles. A bucket is kept
    when it has ended (by the close time of the last small candle) and has at
    least `min_share` of its candles (forex weekends and holidays leave short
    buckets; an exchange outage leaves a few missing candles)."""
    if df is None or not len(df):
        return df
    extra = [c for c in FLOW_RAW if c in df.columns]
    d = df[["timestamp", "open", "high", "low", "close", "volume"] + extra].copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"])
    small = int((d["timestamp"].diff().dt.total_seconds()).median() or 900)
    if tf == "1w":
        g = d.set_index("timestamp").resample("W-MON", label="left", closed="left")
    else:
        g = d.set_index("timestamp").resample(RULE[tf], label="left", closed="left", origin="epoch")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", **{c: "sum" for c in extra}})
    cnt = g["close"].count()
    expected = max(1, TF_SEC[tf] // max(small, 1))
    out = out[cnt >= max(1, int(expected * min_share))].dropna(subset=["open", "close"]).reset_index()
    if last_close is None:
        last_close = d["timestamp"].iloc[-1] + pd.Timedelta(seconds=small)
    ends = out["timestamp"] + pd.Timedelta(seconds=TF_SEC[tf])
    return out[ends <= pd.Timestamp(last_close)].reset_index(drop=True)


def _htf_block(df, tf, htf, prefix):
    bars = resample_closed(df, htf)
    if bars is None or len(bars) < 30:
        return pd.DataFrame(index=df.index, columns=[prefix + k for k in _HTF_KEEP], dtype=float)
    hf = base_features(bars)[_HTF_KEEP].copy()
    hf.columns = [prefix + k for k in _HTF_KEEP]
    hf["ct"] = epoch_s(bars["timestamp"]) + TF_SEC[htf]
    left = pd.DataFrame({"ct": epoch_s(df["timestamp"]) + TF_SEC[tf], "_i": np.arange(len(df))})
    m = pd.merge_asof(left, hf.sort_values("ct"), on="ct", direction="backward")
    m = m.sort_values("_i").drop(columns=["ct", "_i"])
    m.index = df.index
    return m


def _session_block(ts, tf):
    """Hour and weekday in New York time (daylight saving included) at the candle close."""
    t = pd.to_datetime(pd.Series(ts)).reset_index(drop=True) + pd.Timedelta(seconds=TF_SEC[tf])
    ny = t.dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    ldn = t.dt.tz_localize("UTC").dt.tz_convert("Europe/London")
    h = ny.dt.hour + ny.dt.minute / 60.0
    out = pd.DataFrame({
        "s_hsin": np.sin(2 * np.pi * h / 24), "s_hcos": np.cos(2 * np.pi * h / 24),
        "s_dow": ny.dt.weekday.astype(float),
        "s_london": ((ldn.dt.hour >= 8) & (ldn.dt.hour < 17) & (ldn.dt.weekday < 5)).astype(float),
        "s_ny": ((ny.dt.hour >= 8) & (ny.dt.hour < 17) & (ny.dt.weekday < 5)).astype(float),
    })
    return out


def flow_block(df):
    """Order-flow readings from the candle's taker data (scale-free):
    f_tbr   share of the candle's volume bought by takers (0.5 = balanced), f_tbr_z its z-score vs the last 50 candles,
    f_cd6 / f_cd24  cumulative buy-minus-sell volume over 6 / 24 candles as a share of volume,
    f_sz_z  average trade size vs normal (z), f_tc_z  number of trades vs normal (z)."""
    v = df["volume"].astype(float)
    tb = df["taker_buy_volume"].astype(float)
    tc = df["trade_count"].astype(float)
    qv = df["quote_volume"].astype(float)
    tbr = (tb / v.replace(0, np.nan)).clip(0, 1)
    out = pd.DataFrame(index=df.index)
    out["f_tbr"] = tbr
    out["f_tbr_z"] = (tbr - tbr.rolling(50).mean()) / (tbr.rolling(50).std(ddof=0) + 1e-9)
    delta = 2 * tb - v
    for k in (6, 24):
        out[f"f_cd{k}"] = delta.rolling(k).sum() / (v.rolling(k).sum() + 1e-9)
    sz = (qv / tc.replace(0, np.nan))
    lsz = np.log(sz.where(sz > 0))
    out["f_sz_z"] = (lsz - lsz.rolling(50).mean()) / (lsz.rolling(50).std(ddof=0) + 1e-9)
    ltc = np.log1p(tc)
    out["f_tc_z"] = (ltc - ltc.rolling(50).mean()) / (ltc.rolling(50).std(ddof=0) + 1e-9)
    return out


def symbol_features(df, tf, session=True, volume=True, flow=False):
    """All per-symbol features on candles of timeframe `tf` (closed, oldest first)."""
    df = df.reset_index(drop=True)
    flow = bool(flow) and all(c in df.columns for c in FLOW_RAW)
    d = df[["timestamp", "open", "high", "low", "close", "volume"] + (FLOW_RAW if flow else [])].copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"])
    for k in ("open", "high", "low", "close", "volume"):
        d[k] = d[k].astype(float)
    if not volume:
        d["volume"] = 1.0
    F = base_features(d[["timestamp", "open", "high", "low", "close", "volume"]])
    if not volume:
        F["vol_z"] = np.nan
        F["vol_trend"] = np.nan
    blocks = [F]
    for htf, prefix in HTFS[tf]:
        blocks.append(_htf_block(d, tf, htf, prefix))
    F = pd.concat(blocks, axis=1)
    c = d["close"]
    a = F["atr"]
    for k in (42, 180):
        F[f"r{k}"] = (c - c.shift(k)) / a
    lr = np.log(c)
    vol = lr.diff().rolling(42).std()
    for k in ZK:
        F[f"z{k}"] = (lr - lr.shift(k)) / (vol * np.sqrt(k))
    if session:
        S = _session_block(d["timestamp"], tf)
        S.index = F.index
        F = pd.concat([F, S], axis=1)
    if flow:
        Fl = flow_block(d)
        Fl.index = F.index
        F = pd.concat([F, Fl], axis=1)
    F["timestamp"] = d["timestamp"].values
    for k in ("open", "high", "low", "close"):
        F[k] = d[k].values
    return F


# ---------------------------------------------------------------------------
# cross-symbol blocks
# ---------------------------------------------------------------------------
def market_block(frames):
    """Crypto: market-wide z-score medians/dispersion, breadth, and each coin's z values."""
    parts = []
    for sym, F in frames.items():
        if F is None or not len(F):
            continue
        p = F[["timestamp"] + _ZC + ["d_sma200", "h_d_sma50"]].copy()
        p["sym"] = sym
        parts.append(p)
    if not parts:
        return None
    A = pd.concat(parts, ignore_index=True)
    A["above200"] = (A["d_sma200"] > 0).astype(float)
    A["h_up"] = (A["h_d_sma50"] > 0).astype(float)
    g = A.groupby("timestamp")
    M = pd.DataFrame(index=g.size().index)
    for zc in _ZC:
        M["m_" + zc] = g[zc].median()
        M["disp_" + zc] = g[zc].std(ddof=0)
    M["breadth200"] = g["above200"].mean()
    M["breadth_h50"] = g["h_up"].mean()
    M["n_syms"] = g.size()
    Z = {zc: A.pivot_table(index="timestamp", columns="sym", values=zc, aggfunc="last") for zc in _ZC}
    return {"M": M, "Z": Z}


def anchor_block(F):
    """Bitcoin block for crypto (b_ = BTC on the stream timeframe, bh_ = BTC's h_ block)."""
    b = F[["timestamp", "r6", "r24", "d_sma50", "rsi14", "slope50", "atr_rank",
           "h_d_sma50", "h_slope50", "h_rsi14", "h_r6"]].copy()
    b.columns = ["timestamp", "b_r6", "b_r24", "b_d_sma50", "b_rsi14", "b_slope50", "b_atr_rank",
                 "bh_d_sma50", "bh_slope50", "bh_rsi14", "bh_r6"]
    return b.set_index("timestamp")


def currency_block(frames):
    """Forex: average move (in each pair's own volatility units) of every
    currency against the others, per candle, over CS_K candles."""
    rows = {}
    for sym, F in frames.items():
        if F is None or not len(F):
            continue
        cur = markets.currencies(sym)
        if len(cur) != 2:
            continue
        lr = np.log(F["close"].astype(float))
        vol = lr.diff().rolling(42).std()
        s = F[["timestamp"]].copy()
        for k in CS_K:
            s[f"m{k}"] = ((lr - lr.shift(k)) / (vol * np.sqrt(k))).values
        rows[sym] = (cur, s.set_index("timestamp"))
    if not rows:
        return None
    idx = sorted(set().union(*[s.index for _, s in rows.values()]))
    out = {}
    for k in CS_K:
        acc, cnt = {}, {}
        for (b, q), s in rows.values():
            v = s[f"m{k}"].reindex(idx)
            for c, sg in ((b, 1.0), (q, -1.0)):
                acc[c] = acc.get(c, 0) + (sg * v).fillna(0)
                cnt[c] = cnt.get(c, 0) + v.notna().astype(float)
        out[k] = {c: (acc[c] / cnt[c].replace(0, np.nan)) for c in acc}
    return {"idx": pd.Index(idx), "cs": out}


def assemble(F, features, sym=None, anchor=None, mkt=None, cs=None):
    """Model input rows (one symbol) in the model's feature order."""
    X = F.set_index("timestamp")
    if anchor is not None:
        X = X.join(anchor, how="left")
    if mkt is not None:
        X = X.join(mkt["M"], how="left")
        for zc in _ZC:
            Zt = mkt["Z"][zc].reindex(X.index)
            own = X[zc].to_numpy()
            if sym in Zt.columns:
                others = Zt.drop(columns=[sym]).to_numpy()
            else:
                others = Zt.to_numpy()
            with np.errstate(all="ignore"), warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                med = np.nanmedian(np.column_stack([others, own]), axis=1) if others.size else own
                X["rel_" + zc] = own - med
                cntv = np.isfinite(others).sum(axis=1) if others.size else np.zeros(len(own))
                X["rk_" + zc] = np.where(cntv > 0, (others < own[:, None]).sum(axis=1) / np.maximum(cntv, 1), np.nan)
    if cs is not None and sym:
        cur = markets.currencies(sym)
        if len(cur) == 2:
            b, q = cur
            for k in CS_K:
                tb = cs["cs"][k].get(b)
                tq = cs["cs"][k].get(q)
                vb = tb.reindex(X.index).to_numpy() if tb is not None else np.full(len(X), np.nan)
                vq = tq.reindex(X.index).to_numpy() if tq is not None else np.full(len(X), np.nan)
                X[f"cs_b{k}"] = vb
                X[f"cs_q{k}"] = vq
                X[f"cs_d{k}"] = vb - vq
    for col in features:
        if col not in X.columns:
            X[col] = np.nan
    return X[features]


_CS_SWAP = re.compile(r"cs_([bq])(\d+)$")


def flip(X):
    """Mirror image for the short side (adds the currency-strength columns)."""
    G = flip_for_short(X)
    for col in X.columns:
        m = _CS_SWAP.match(col)
        if m:
            other = f"cs_{'q' if m.group(1) == 'b' else 'b'}{m.group(2)}"
            if other in X.columns:
                G[col] = X[other]
        elif col.startswith("cs_d"):
            G[col] = -X[col]
        elif col in ("s_hsin", "s_hcos", "s_dow", "s_london", "s_ny", "n_syms", "f_sz_z", "f_tc_z"):
            G[col] = X[col]
        elif col == "f_tbr":
            G[col] = 1 - X[col]
        elif col in ("f_tbr_z", "f_cd6", "f_cd24"):
            G[col] = -X[col]
    return G
