"""Live use of the chart engine on the site: fetch closed candles for the entry timeframe and its higher timeframes,
replay the last LIVE_HISTORY candles and return what the agents see now (and the newest candle's trade idea, if
there is one).

    from chart_agents.live import analyze
    out = analyze(get_candles, "BTC/USDT", "4h")       # get_candles: main.get_candles (OKX via ccxt)
    out["report"]       # trend per timeframe, zones, patterns, liquidity, fair value gaps, swings
    out["idea"]         # None, or the judged trade idea with its explanation (tested=False unless the stream
                        # passed its test: research/human_lab/results.json / models/human_crypto_<tf>.json)

A stream's ideas may only be shown as signals if models/human_<market>_<tf>.json exists (written by the lab only
when the holdout test passed). Otherwise the idea is analysis, labelled as not tested.
"""
import json
import os
import time
from datetime import datetime, timedelta

import pandas as pd

from . import config as C
from .engine import Engine
from .head import HeadTrader

MODELS_DIR = os.environ.get("MODELS_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                          "models")
_cache = {}


def head_trader(market, tf):
    """The tested head trader for a stream, or None (the stream has not passed its test)."""
    path = os.path.join(MODELS_DIR, f"human_{market}_{tf}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            ht = HeadTrader.from_json(json.load(fh))
        return ht if ht.meta.get("live") else None
    except Exception:
        return None


def fetch(get_candles, symbol, tf, bars, now=None, page=300):
    """Closed candles of one timeframe, paged from the past (exchanges return at most a few hundred per call)."""
    now = now or datetime.utcnow().replace(microsecond=0)
    sec = C.TF_SEC[tf]
    cursor = int((now - timedelta(seconds=sec * (bars + 2)) - datetime(1970, 1, 1)).total_seconds() * 1000)
    end_ms = (now - datetime(1970, 1, 1)).total_seconds() * 1000
    frames, last = [], None
    for _ in range(40):
        df = get_candles(symbol=symbol, timeframe=tf, limit=page, since=cursor)
        if df is None or not len(df):
            break
        frames.append(df)
        last_ms = int(pd.Timestamp(df["timestamp"].iloc[-1]).value // 1_000_000)
        if last is not None and last_ms <= last:
            break
        last = last_ms
        cursor = last_ms + sec * 1000
        if cursor > end_ms:
            break
        time.sleep(0.1)
    if not frames:
        return None
    df = pd.concat(frames).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    done = df[df["timestamp"] + pd.Timedelta(seconds=sec) <= pd.Timestamp(now)]
    return done.tail(bars).reset_index(drop=True)


def analyze(get_candles, symbol, tf, market="crypto", now=None, live_news=True):
    need = (tf,) + tuple(C.HIGHER_TF[tf]) + tuple(h for h in ("1d", "1w") if h not in C.HIGHER_TF[tf] and h != tf)
    now = now or datetime.utcnow().replace(microsecond=0)
    bar = int((now - datetime(1970, 1, 1)).total_seconds()) // C.TF_SEC[tf]
    key = (symbol, tf, bar)
    if key in _cache:
        return _cache[key]
    frames = {}
    for h in need:
        frames[h] = fetch(get_candles, symbol, h, C.LIVE_HISTORY.get(h, 3000), now)
        if frames[h] is None or len(frames[h]) < 60:
            if h in (tf,) + tuple(C.HIGHER_TF[tf]):
                raise ValueError(f"Not enough {h} history for {symbol}.")
            frames[h] = None
    eng = Engine({k: v for k, v in frames.items() if v is not None}, tf, symbol, market, live=live_news)
    ideas = eng.run(evaluate_from=0, last_only=True)
    ht = head_trader(market, tf)
    idea = None
    if ideas:
        c = ideas[-1]
        judge = ht or HeadTrader("checklist")
        c["tested"] = ht is not None
        c["take"] = bool(judge.take(c)) if ht is not None else False
        c["grade"] = judge.grade(c) if ht is not None else None
        c["explanation"] = judge.explain(c)
        idea = {k: c[k] for k in ("symbol", "tf", "time", "side", "setups", "desc", "plan", "scores", "notes",
                                  "vetoes", "tested", "take", "grade", "explanation")}
    out = {"report": eng.report(), "idea": idea, "tested_stream": ht is not None}
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = out
    return out
