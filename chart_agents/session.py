"""Session / volatility agent and news agent.

Session / volatility (works in the backtest and live):
* a dead market (ATR in the lowest 8% of the last 500 candles) rarely reaches a target in time;
* a panic candle (range of 4 ATR or more, or ATR in the top 3%) is usually news: a trader waits for it to settle;
* crypto trades 24/7 but weekends are thinner; forex: the London and New York sessions move, the Asian session
  chops, and Friday evening / the Sunday open have wide spreads.

News (live only): SignalX's economic calendar (news.py) blocks forex signals around high-impact events. There is no
historical calendar in the backtest, so there the news agent is neutral - one more reason why a stream also runs on
the demo account before it counts.
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config as C


class SessionAgent:
    name = "session"

    def __init__(self, ch):
        self.ch = ch
        ts = pd.to_datetime(ch.t_close)
        self.hour = ts.hour.to_numpy()
        self.dow = ts.dayofweek.to_numpy()

    def opinion(self, i, side):
        ch = self.ch
        notes, veto = [], None
        score = 0.0
        rk = ch.atr_rank[i]
        a = ch.a(i)
        shock = ch.rng[i] >= C.SHOCK_RANGE_ATR * a or (i and ch.rng[i - 1] >= C.SHOCK_RANGE_ATR * ch.a(i - 1))
        if shock:
            veto = "a very large candle just printed (news?): wait for it to settle"
        if rk == rk:
            if rk < C.ATR_RANK_LOW:
                score -= 0.5
                notes.append("the market is very quiet (low volatility): a target may take too long")
            elif rk > C.ATR_RANK_HIGH:
                score -= 0.4
                notes.append("volatility is extreme right now")
        hr, dw = int(self.hour[i]), int(self.dow[i])
        intraday = C.TF_SEC[ch.tf] < 14400
        if ch.market == "crypto":
            if intraday and dw >= 5:
                score -= 0.1
                notes.append("weekend: thinner crypto market")
        else:
            if (dw == 4 and hr >= 20) or (dw == 6) or (dw == 0 and hr < 1):
                veto = veto or "forex weekend gap risk (Friday evening / Sunday open)"
            elif intraday and 0 <= hr < 7:
                score -= 0.2
                notes.append("Asian session: forex pairs often chop")
            elif 12 <= hr < 16:
                score += 0.1
                notes.append("London / New York overlap: the most liquid hours")
        feats = {"atr_rank": float(rk) if rk == rk else 0.5, "weekend": int(dw >= 5),
                 "hour_asia": int(0 <= hr < 7), "hour_us": int(13 <= hr < 21)}
        return float(np.clip(score, -1, 1)), notes, feats, veto


class NewsAgent:
    """Live: high-impact events from SignalX's calendar. Backtest: neutral (no historical calendar)."""
    name = "news"

    def __init__(self, symbol, live=False):
        self.symbol, self.live = symbol, live

    def opinion(self, at=None):
        if not self.live:
            return 0.0, [], None
        try:
            import news            # SignalX news.py (needs the database)
        except Exception:
            return 0.0, [], None
        at = at or datetime.now(timezone.utc).replace(tzinfo=None)
        notes, veto = [], None
        try:
            r = news.blackout_reason(self.symbol, at)
            if r:
                veto = f"news: {r}"
            up = news.upcoming_for_symbol(self.symbol, at)
            if up:
                notes.append("high-impact news coming up: " + str(up)[:120])
        except Exception:
            pass
        return (-0.3 if notes else 0.0), notes, veto
