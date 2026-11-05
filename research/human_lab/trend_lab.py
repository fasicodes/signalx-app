"""Loop round 5: the trend trader.

Loop round 4 showed the only edge in the agents' crypto trades came from trading WITH the whole market (Bitcoin and
most coins in the same direction), not from the chart reading itself. So this agent is built on that, the way a
trend trader works - and with the 2R target the site wants:
* market regime: bull when Bitcoin is above its 50 average AND more than 60% of the coins are above theirs; bear
  when Bitcoin is below AND fewer than 40% are above. No trades in between;
* in a bull market buy a coin that is in its own uptrend (20 average above the 50) and closes at a new N-candle high
  (a breakout); in a bear market the mirror for shorts;
* optional: only the market's leaders (top 30% by 20-candle return; bottom 30% for shorts);
* stop 3 ATR, target 2R (or a trailing stop), time limit 40 candles;
* SignalX portfolio rules and costs (0.30% per round trip + funding for longs), pessimistic fills.
Validation only (2021-03 .. 2024-12); every variant is logged in loop_log.json.

Usage: python trend_lab.py run
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lab  # noqa: E402
import loop  # noqa: E402
from chart_agents.risk import simulate  # noqa: E402

TF_SEC = {"4h": 14400, "1d": 86400}


class _A:
    pass


def load(tf, data_dir=None, syms=None):
    d = data_dir or lab.W
    syms = syms or sorted({f.split("_")[0] for f in os.listdir(f"{d}/frames") if f.endswith(f"_{tf}.pkl")})
    out = {}
    for s in syms:
        f = pd.read_pickle(f"{d}/frames/{s}_{tf}.pkl").sort_values("timestamp").reset_index(drop=True)
        out[s] = f
    return out


def market(frames, tf):
    cl = pd.DataFrame({s: f.set_index("timestamp")["close"] for s, f in frames.items()}).sort_index()
    ema = cl.ewm(span=50, adjust=False, ignore_na=True).mean().where(cl.notna())
    above = (cl > ema).astype(float).where(cl.notna())
    breadth = above.mean(axis=1)
    btc_up = cl["BTC"] > ema["BTC"]
    reg = pd.Series(0, index=cl.index)
    reg[(btc_up) & (breadth > 0.6)] = 1
    reg[(~btc_up) & (breadth < 0.4)] = -1
    r20 = cl.pct_change(20, fill_method=None)
    rank = r20.rank(axis=1, pct=True)
    return reg, rank


def trades(frames, tf, n=20, leaders=False, k_atr=3.0, target_r=2.0, trail=False, max_bars=40, btc_frames=None):
    reg, rank = market(btc_frames or frames, tf)
    out = []
    for s, f in frames.items():
        c, h, lo, o = (f[k].to_numpy(float) for k in ("close", "high", "low", "open"))
        ts = f["timestamp"]
        pc = np.r_[c[0], c[:-1]]
        tr = np.maximum(h - lo, np.maximum(np.abs(h - pc), np.abs(lo - pc)))
        atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy(float)
        e20 = pd.Series(c).ewm(span=20, adjust=False).mean().to_numpy(float)
        e50 = pd.Series(c).ewm(span=50, adjust=False).mean().to_numpy(float)
        hi_n = pd.Series(c).shift(1).rolling(n).max().to_numpy(float)
        lo_n = pd.Series(c).shift(1).rolling(n).min().to_numpy(float)
        rg = reg.reindex(ts).to_numpy()
        rk = rank[s].reindex(ts).to_numpy() if s in rank else np.full(len(f), np.nan)
        a = _A()
        a.o, a.h, a.l, a.c, a.n, a.tf = o, h, lo, c, len(f), tf
        t_close = (ts + pd.Timedelta(seconds=TF_SEC[tf])).to_numpy("datetime64[ns]")
        for i in range(60, len(f) - 1):
            side = 0
            if rg[i] == 1 and c[i] > e50[i] and e20[i] > e50[i] and c[i] > hi_n[i]:
                side = 1
            elif rg[i] == -1 and c[i] < e50[i] and e20[i] < e50[i] and c[i] < lo_n[i]:
                side = -1
            if not side:
                continue
            if leaders and not (rk[i] == rk[i] and ((side == 1 and rk[i] >= 0.7) or (side == -1 and rk[i] <= 0.3))):
                continue
            entry, risk = c[i], k_atr * atr[i]
            stop = entry - side * risk
            if trail:
                x = _trail(a, i, side, entry, stop, atr, max_bars, s)
            else:
                x = simulate(a, i, side, entry, stop, entry + side * target_r * risk, max_bars, "crypto", f"{s}/USDT")
            if x is None:
                continue
            out.append({"symbol": f"{s}/USDT", "side": side, "t": t_close[i], "exit_t": t_close[min(i + x[2], len(f) - 1)],
                        "net_r": x[0], "outcome": x[1], "setups": ["trend breakout"], "scores": {}})
    return out


def _trail(a, i, side, entry, stop, atr, max_bars, s):
    """Stop to entry at +1R, then 3 ATR behind the best close (set at a candle's close, used from the next)."""
    from chart_agents.risk import cost_in_r
    risk = (entry - stop) * side
    cur, best = stop, entry
    end = min(a.n - 1, i + max_bars)
    for j in range(i + 1, end + 1):
        if side == 1 and (a.o[j] <= cur or a.l[j] <= cur):
            px = a.o[j] if a.o[j] <= cur else cur
            break
        if side == -1 and (a.o[j] >= cur or a.h[j] >= cur):
            px = a.o[j] if a.o[j] >= cur else cur
            break
        best = max(best, a.c[j]) if side == 1 else min(best, a.c[j])
        if (best - entry) * side >= risk:
            cur = max(cur, entry) if side == 1 else min(cur, entry)
        t = best - side * 3.0 * atr[j]
        cur = max(cur, t) if side == 1 else min(cur, t)
    else:
        if end < i + max_bars:
            return None
        px, j = a.c[end], end
    hours = (j - i) * TF_SEC[a.tf] / 3600
    net = (px - entry) * side / risk - cost_in_r("crypto", f"{s}/USDT", entry, risk, hours, side)
    return float(net), ("target" if net > 0 else "stop"), int(j - i), float(px)


def run():
    for tf in ("1d", "4h"):
        frames = load(tf)
        for n in (20, 55):
            for leaders in (False, True):
                for trail in (False, True):
                    tr = trades(frames, tf, n=n, leaders=leaders, trail=trail)
                    val = [t for t in tr if lab.VAL_START <= t["t"] < lab.HOLD_START]
                    res = loop.judge(lab.portfolio(val, [0.0] * len(val), "4h" if tf == "4h" else "4h"))
                    loop.log({"tf": tf, "name": f"trend: {n}-candle breakout{' leaders' if leaders else ''}"
                                               f"{' trailing' if trail else ' 2R'}",
                              "exit": "trailing 3 ATR" if trail else "stop 3 ATR, target 2R", "result": res})


if __name__ == "__main__" and sys.argv[1] == "run":
    run()
