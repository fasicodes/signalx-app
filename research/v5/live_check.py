"""Does the site's live engine give the same crypto 1h signals as the research test?

Runs streams.StreamEngine (the code the site runs) on the research candles as if they came from the exchange, at
several moments of the unseen period, and compares every signal of the newest candles with the research trades
(work/results/crypto_1h_rolling_holdout_trades.pkl). The compared part is the last (replay - time limit) candles
of each replay: from there on the replay knows every trade that can still be running.

  python live_check.py [moments=12]
"""
import os
import pickle
import sys
import time
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lab5 as L  # noqa: E402
from lab5 import FE, ST  # noqa: E402

warnings.filterwarnings("ignore")
C = L.CFG["crypto_4h"]
NOW = [None]
_cache = {}


def candles(symbol, timeframe, limit, since=None):
    if symbol not in _cache:
        d15 = L.load15(C, symbol)
        _cache[symbol] = FE.resample_closed(d15[["timestamp", "open", "high", "low", "close", "volume"]], "1h")
    df = _cache[symbol]
    df = df[df["timestamp"] + pd.Timedelta(hours=1) <= pd.Timestamp(NOW[0])]
    if since is not None:
        return df[df["timestamp"] >= pd.Timestamp(since, unit="ms")].head(limit)
    return df.tail(limit)


def main(n=12):
    m = ST.load_model("crypto_1h")
    replay = ST.replay_bars(m)
    safe = replay - m.max_bars - m.cooldown - 2
    research = pickle.load(open(f"{L.W}/results/crypto_1h_rolling_holdout_trades.pkl", "rb"))
    res = {(t["symbol"], pd.Timestamp(t["bar_time"])): t for t in research}
    eng = ST.StreamEngine("crypto", "1h", ST.CandleStore(candles, sleep=0), L.symbols(C))
    # moments in the newest walk-forward fold (the exported model is that fold's model), spread over its busy and
    # quiet weeks, at changing hours so that every grid offset is the newest one somewhere
    start, end = datetime(2026, 4, 25, 0, 5), datetime(2026, 10, 25, 0, 5)
    rng = np.random.default_rng(5)
    busy = sorted({pd.Timestamp(t["bar_time"]).to_pydatetime() + timedelta(hours=int(rng.integers(3, 60)), minutes=5)
                   for t in research if start <= pd.Timestamp(t["bar_time"]) <= end})
    picks = [busy[i] for i in np.linspace(0, len(busy) - 1, n * 2 // 3).astype(int)]
    picks += [start + (end - start) * float(x) for x in rng.uniform(0, 1, n - len(picks))]
    total = both = only_live = only_res = same = 0
    for now in sorted(picks):
        now = now.replace(minute=5, second=0, microsecond=0)
        NOW[0] = now
        t0 = time.time()
        board = eng.board(now, force=True)
        bar = pd.Timestamp(ST.last_closed_bar("1h", now))
        cut = bar - pd.Timedelta(hours=safe - 1)
        live = {}
        for sym, r in board.items():
            if r.get("error"):
                continue
            for t in (r.get("recent") or []) + ([r["active"]] if r.get("active") else []):
                if pd.Timestamp(t["bar_time"]) >= cut:
                    live[(sym, pd.Timestamp(t["bar_time"]))] = t
        ref = {k: v for k, v in res.items() if cut <= k[1] <= bar}
        a, b = set(live), set(ref)
        ok = sum(1 for k in a & b if live[k]["side"] == ref[k]["side"]
                 and abs(live[k]["entry"] / ref[k]["entry"] - 1) < 1e-9
                 and abs(live[k]["stop_loss"] / ref[k]["stop_loss"] - 1) < 2e-3)
        total += len(a | b)
        both += len(a & b)
        only_live += len(a - b)
        only_res += len(b - a)
        same += ok
        print(f"{now}  live {len(a):3d}  research {len(b):3d}  in both {len(a & b):3d}  same levels {ok:3d}  "
              f"errors {len(eng.errors)}  {time.time() - t0:.0f}s", flush=True)
        for k in sorted(a - b)[:3]:
            print("   only live:", k[0], k[1], live[k]["side"], round(live[k]["score"], 4))
        for k in sorted(b - a)[:3]:
            print("   only research:", k[0], k[1], ref[k]["side"], round(ref[k]["score"], 4))
    print(f"TOTAL signals {total}: in both {both}, same side / entry / stop {same}, only live {only_live}, "
          f"only research {only_res}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 12)
