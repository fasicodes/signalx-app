"""Top-down agent: before taking a trade on the entry chart, a trader checks the two higher timeframes
(15m -> 1h and 4h, 1h -> 4h and daily, 4h -> daily and weekly). Trades with the bigger trend are easier; a trade
against both higher timeframes needs a very good reason.

Only higher-timeframe candles that had already CLOSED when the entry candle closed are used.
"""
import numpy as np


class TopDownAgent:
    name = "topdown"

    def __init__(self, htf_books, aligns, names):
        self.books, self.aligns, self.names = htf_books, aligns, names

    def state(self, i):
        out = []
        for bk, al, nm in zip(self.books, self.aligns, self.names):
            j = int(al[i])
            if j < 0:
                out.append((nm, 0, 0))
                continue
            ch = bk.ch
            t = int(bk.st.trend[j])
            ema = 1 if ch.c[j] > ch.ema50[j] else -1
            out.append((nm, t, ema))
        return out

    def opinion(self, i, side):
        st = self.state(i)
        notes = []
        score = 0.0
        w_t, w_e = (0.45, 0.35), (0.1, 0.1)
        agree, against = [], []
        for k, (nm, t, ema) in enumerate(st):
            if t == side:
                score += w_t[k]
                agree.append(nm)
            elif t == -side:
                score -= w_t[k] + 0.05
                against.append(nm)
            score += w_e[k] * (1 if ema == side else -1)
        word = "uptrend" if side == 1 else "downtrend"
        if len(agree) == 2:
            notes.append(f"{agree[0]} and {agree[1]} are both in an {word}" if side == 1 else
                         f"{agree[0]} and {agree[1]} are both in a {word}")
        elif agree:
            notes.append(f"{agree[0]} is in an {word}" if side == 1 else f"{agree[0]} is in a {word}")
        if against:
            notes.append(" and ".join(against) + " trend " + ("is" if len(against) == 1 else "are") +
                         " against the trade")
        feats = {"htf1": st[0][1] * side if st else 0, "htf2": st[1][1] * side if len(st) > 1 else 0,
                 "htf1_ema": st[0][2] * side if st else 0, "htf2_ema": st[1][2] * side if len(st) > 1 else 0}
        return float(np.clip(score, -1, 1)), notes, feats
