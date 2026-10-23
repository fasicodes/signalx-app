"""
More coins for the two tested crypto streams (1h model + 4h gate, 4h engine v3.1)?

Question: does the site get more signals, at the same quality, if the tested models also look at coins they were
never trained on? The models use only scale-free inputs (distances in ATR, ranks, z-scores) and the market-wide
inputs come from the coins they were trained with, so a new coin only adds its own score.

Test (decided before any result was seen):
  * period: 2025-10-01 to the end of the data (2026-10-17): neither model was trained on it, and the new coins
    were never seen at all;
  * the live code: the 1h stream is replayed with models/crypto_1h.json (its 4h gate, threshold, one trade at a
    time per coin, cooldown 2, at most 4 new signals per candle), the 4h stream with signal_engine v3.1 (strong
    signals + the 24-hour standard signal, at most 5 new signals per candle); costs 0.12% round trip;
  * run A = the coins the site runs today (those with history data), run B = A plus the new coins, each stream
    replayed with ALL its coins together (the per-candle limit is shared, as on the site);
  * new coins: Binance spot USDT coins with history data from 2025-05-01 or earlier to the end of the period, not
    on the site yet. None is picked or dropped because of its own result.
  * a stream gets the new coins only if, in run B, (1) the new coins' signals reach >= 70% wins (TP1 or better),
    a positive average after costs and >= 50 signals, and (2) the whole stream still reaches >= 70% wins and a
    positive average. Otherwise that stream keeps its coin list.

Data: 1-minute Binance klines from public GitHub repositories (github.com/parshant231234-eng/<COIN>USDT-Historical-Data),
aggregated to 1-hour candles; 4-hour candles are built from them (UTC buckets).

Usage: python coin_expansion.py DATA_DIR NEW_COINS_FILE [OUT_JSON]
  DATA_DIR holds <COIN>_1h.csv.gz (timestamp, open, high, low, close, volume, ...).
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
import features as FE  # noqa: E402
import markets  # noqa: E402
import signal_engine as SE  # noqa: E402
import streams as ST  # noqa: E402

T0 = pd.Timestamp("2025-10-01")
FIRST_NEEDED = pd.Timestamp("2025-05-01")      # 3,700 1h candles before T0 (what the live 1h engine keeps)
COLS = ["timestamp", "open", "high", "low", "close", "volume"]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load(data_dir, coin):
    p = os.path.join(data_dir, f"{coin}_1h.csv.gz")
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p, usecols=COLS, parse_dates=["timestamp"])
    return d.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def eligible(df, t1):
    """History from FIRST_NEEDED or earlier, trading to the end, and at least 95% of the test hours present."""
    if df is None or not len(df):
        return False, "no data"
    if df["timestamp"].iloc[0] > FIRST_NEEDED:
        return False, f"history starts {df['timestamp'].iloc[0].date()}"
    if df["timestamp"].iloc[-1] < t1 - pd.Timedelta(hours=2):
        return False, f"history ends {df['timestamp'].iloc[-1].date()}"
    hours = (t1 - T0).total_seconds() / 3600
    have = int(((df["timestamp"] >= T0) & (df["timestamp"] < t1)).sum())
    if have < 0.95 * hours:
        return False, f"only {have / hours:.0%} of the test hours"
    return True, ""


def stats(trades, t0, t1):
    days = (t1 - t0).total_seconds() / 86400
    n = len(trades)
    if not n:
        return {"signals": 0, "signals_per_day": 0.0, "win_rate": None, "avg_net_r": None}
    r = np.array([t["r"] for t in trades], float)
    wins = sum(1 for t in trades if str(t["status"]).startswith("TP"))
    order = np.argsort([pd.Timestamp(t.get("closed_at") or t["signal_at"]).value for t in trades], kind="stable")
    eq = np.cumsum(r[order])
    dd = float(np.min(eq - np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]))
    gp, gl = r[r > 0].sum(), -r[r < 0].sum()
    out = {"signals": n, "signals_per_day": round(n / days, 2), "win_rate": round(wins / n * 100, 1),
           "stop_rate": round(sum(1 for t in trades if t["status"] == "SL") / n * 100, 1),
           "tp2_rate": round(sum(1 for t in trades if int(t.get("hits") or 0) >= 2) / n * 100, 1),
           "avg_net_r": round(float(r.mean()), 3), "total_net_r": round(float(r.sum()), 1),
           "profit_factor": round(float(gp / gl), 2) if gl > 0 else None, "max_drawdown_r": round(dd, 1),
           "coins_with_signals": len({t["symbol"] for t in trades})}
    q = {}
    for t in trades:
        ts = pd.Timestamp(t["signal_at"])
        q.setdefault(f"{ts.year}-Q{(ts.month - 1) // 3 + 1}", []).append(t)
    out["quarters"] = {k: {"signals": len(v), "win_rate": round(sum(1 for t in v if str(t["status"]).startswith("TP")) / len(v) * 100, 1),
                           "avg_net_r": round(float(np.mean([t["r"] for t in v])), 3)} for k, v in sorted(q.items())}
    return out


def passes(s):
    return bool(s.get("signals", 0) >= 50 and (s.get("win_rate") or 0) >= 70 and (s.get("avg_net_r") or 0) > 0)


# ---------------------------------------------------------------------------
# 1h stream: models/crypto_1h.json with its 4h gate (StreamEngine._model_board, over the whole period)
# ---------------------------------------------------------------------------
def stream_1h(frames, coins, t1):
    m = ST.load_model("crypto_1h")
    gm = m.gate
    trained = set(m.meta.get("trained_symbols") or [])
    F, G = {}, {}
    for sym in coins:
        df = frames[sym]
        F[sym] = FE.symbol_features(df, "1h", session=m.session, volume=m.volume)
        G[sym] = FE.symbol_features(FE.resample_closed(df[COLS], "4h"), "4h", session=gm.session, volume=gm.volume)
    ref = {k: v for k, v in F.items() if k in trained} or F
    mkt = FE.market_block(ref)
    anchor = FE.anchor_block(F["BTC/USDT"]) if m.anchor and "BTC/USDT" in F else None
    gref = {k: v for k, v in G.items() if k in trained} or G
    gmkt = FE.market_block(gref)
    ganchor = FE.anchor_block(G["BTC/USDT"]) if gm.anchor and "BTC/USDT" in G else None
    data = {}
    keep_from = T0 - pd.Timedelta(days=3)
    for sym in coins:
        Gf = G[sym]
        Xg = FE.assemble(Gf, gm.features, sym=sym, anchor=ganchor, mkt=gmkt)
        gate = (pd.to_datetime(Gf["timestamp"]).to_numpy(), gm.raw_proba(Xg.to_numpy(dtype=float)),
                gm.raw_proba(FE.flip(Xg).to_numpy(dtype=float)))
        Fs = F[sym]
        Fs = Fs[pd.to_datetime(Fs["timestamp"]) >= keep_from].reset_index(drop=True)
        X = FE.assemble(Fs, m.features, sym=sym, anchor=anchor, mkt=mkt)
        pl = m.raw_proba(X.to_numpy(dtype=float))
        ps = m.raw_proba(FE.flip(X).to_numpy(dtype=float))
        g = ST.align_gate(Fs, "1h", gate, gm.tf)
        data[sym] = ST.model_data(m, Fs, pl, ps, gate=g)
    plan = ST.plan_for("crypto", "1h")
    plan["hold_bars"] = m.hold_bars
    policy = ST.policy_for("crypto", "1h")
    return data, plan, policy


def run_1h(data, plan, policy, coins, t1):
    sub = {s: data[s] for s in coins if s in data}
    _, _, closed = ST.run_policy(sub, "crypto", "1h", plan, policy, start_time=T0, end_time=t1, lookback=10 ** 9)
    out = []
    for sym, lst in closed.items():
        for t in lst:
            t["symbol"] = sym
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# 4h stream: signal_engine v3.1 (LiveEngine.board, over the whole period)
# ---------------------------------------------------------------------------
def scores_4h(frames, coins):
    F4 = {}
    for sym in coins:
        d4 = FE.resample_closed(frames[sym][COLS], "4h")
        F4[sym] = SE.coin_features(d4)
    mkt = SE.market_block({s: F4.get(s) for s in SE.UNIVERSE})
    btc = SE.btc_block(F4["BTC/USDT"])
    Xs = {sym: SE.assemble(F4[sym], btc, mkt, sym=sym) for sym in coins if len(F4[sym]) > SE.WARMUP_BARS}
    sc = SE.score_many(Xs)
    return {sym: {"F": F4[sym], "pl": sc[sym][0], "ps": sc[sym][1], "cl": sc[sym][2], "cs": sc[sym][3]} for sym in sc}


def run_4h(S, coins):
    sub = {s: S[s] for s in coins if s in S}
    closed = {}
    SE.replay_all(sub, start_time=T0, closed=closed)
    out = []
    for sym, lst in closed.items():
        for t in lst:
            t["symbol"] = sym
            out.append(t)
    return out


def main(data_dir, new_file, out_json=None):
    current = [s for s in markets.CRYPTO_COINS]
    new = [x.strip().upper() + "/USDT" for x in open(new_file).read().split() if x.strip()]
    new = [s for s in new if s not in current]
    frames, skipped = {}, {}
    t1 = None
    for sym in current + new:
        df = load(data_dir, sym.split("/")[0])
        if df is not None and len(df):
            t1 = df["timestamp"].iloc[-1] + pd.Timedelta(hours=1) if t1 is None else max(t1, df["timestamp"].iloc[-1] + pd.Timedelta(hours=1))
        frames[sym] = df
    assert frames.get("BTC/USDT") is not None, "BTC data is needed"
    t1 = pd.Timestamp(frames["BTC/USDT"]["timestamp"].iloc[-1]) + pd.Timedelta(hours=1)
    ok_cur, ok_new = [], []
    for sym in current + new:
        good, why = eligible(frames.get(sym), t1)
        if good:
            (ok_cur if sym in current else ok_new).append(sym)
        else:
            skipped[sym] = why
    log("period", T0.date(), "to", t1, "| current coins with data", len(ok_cur), "| new coins", len(ok_new))
    allc = ok_cur + ok_new
    res = {"period": f"{T0.date()} to {t1.date()}", "current_coins": ok_cur, "new_coins": ok_new, "skipped": skipped,
           "rule": "new coins are added to a stream only if, with all coins replayed together, the new coins' signals "
                   "reach >= 70% wins, a positive average after costs and >= 50 signals, and the whole stream still "
                   "reaches >= 70% wins and a positive average"}

    only = (os.environ.get("ONLY_STREAM") or "").strip()      # "4h" = replay only the 4h stream
    b = []
    if only != "4h":
        log("1h: features + scores for", len(allc), "coins")
        data, plan, policy = stream_1h(frames, allc, t1)
        a = run_1h(data, plan, policy, ok_cur, t1)
        b = run_1h(data, plan, policy, allc, t1)
        res["crypto_1h"] = {"A_current": stats(a, T0, t1), "B_all": stats(b, T0, t1),
                            "B_new_only": stats([t for t in b if t["symbol"] in ok_new], T0, t1),
                            "B_current_only": stats([t for t in b if t["symbol"] in ok_cur], T0, t1)}
        r1 = res["crypto_1h"]
        r1["passes"] = passes(r1["B_new_only"]) and (r1["B_all"].get("win_rate") or 0) >= 70 and (r1["B_all"].get("avg_net_r") or 0) > 0
        log("1h A", json.dumps({k: r1["A_current"][k] for k in ("signals", "signals_per_day", "win_rate", "avg_net_r")}))
        log("1h B", json.dumps({k: r1["B_all"][k] for k in ("signals", "signals_per_day", "win_rate", "avg_net_r")}))
        log("1h B new", json.dumps({k: r1["B_new_only"].get(k) for k in ("signals", "signals_per_day", "win_rate", "avg_net_r")}))

    log("4h: features + scores")
    S = scores_4h(frames, allc)
    a4 = run_4h(S, ok_cur)
    b4 = run_4h(S, allc)
    res["crypto_4h"] = {"A_current": stats(a4, T0, t1), "B_all": stats(b4, T0, t1),
                        "B_new_only": stats([t for t in b4 if t["symbol"] in ok_new], T0, t1),
                        "B_current_only": stats([t for t in b4 if t["symbol"] in ok_cur], T0, t1)}
    r4 = res["crypto_4h"]
    for k in ("A_current", "B_all", "B_new_only"):
        lst = a4 if k == "A_current" else (b4 if k == "B_all" else [t for t in b4 if t["symbol"] in ok_new])
        for tier in ("strong", "standard"):
            sel = [t for t in lst if t.get("tier") == tier]
            r4[k][tier] = {"signals": len(sel), "win_rate": round(sum(1 for t in sel if str(t["status"]).startswith("TP")) / len(sel) * 100, 1) if sel else None,
                           "avg_net_r": round(float(np.mean([t["r"] for t in sel])), 3) if sel else None}
    r4["passes"] = passes(r4["B_new_only"]) and (r4["B_all"].get("win_rate") or 0) >= 70 and (r4["B_all"].get("avg_net_r") or 0) > 0
    log("4h A", json.dumps({k: r4["A_current"][k] for k in ("signals", "signals_per_day", "win_rate", "avg_net_r")}))
    log("4h B", json.dumps({k: r4["B_all"][k] for k in ("signals", "signals_per_day", "win_rate", "avg_net_r")}))
    log("4h B new", json.dumps({k: r4["B_new_only"].get(k) for k in ("signals", "signals_per_day", "win_rate", "avg_net_r")}))
    # per coin (shown for information only - nothing is chosen by it)
    per = {}
    for name, lst in (("1h", b), ("4h", b4)):
        for t in lst:
            p = per.setdefault(t["symbol"], {}).setdefault(name, {"n": 0, "w": 0, "r": 0.0})
            p["n"] += 1
            p["w"] += int(str(t["status"]).startswith("TP"))
            p["r"] += float(t["r"])
    res["per_coin"] = {s: {k: {"signals": v["n"], "win_rate": round(v["w"] / v["n"] * 100, 1), "total_r": round(v["r"], 2)}
                           for k, v in d.items()} for s, d in sorted(per.items())}
    if out_json:
        json.dump(res, open(out_json, "w"), indent=1, default=str)
        log("saved", out_json)
    return res


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
