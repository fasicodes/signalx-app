"""Draws what the agents see on a chart, to check by eye that they mark what a trader would mark: candles, swing
highs / lows, support / resistance zones (entry chart and higher timeframes), fair value gaps, liquidity pools and
the trade ideas with entry, stop and target.

Usage: python charts.py <15m csv.gz> <tf> <end date> [candles] [out.png]
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from chart_agents.data import resample  # noqa: E402
from chart_agents.engine import Engine  # noqa: E402


def draw(path, tf, end, bars=220, out="chart.png", symbol=None):
    d = pd.read_csv(path, parse_dates=["timestamp"])
    d = d[d.timestamp < pd.Timestamp(end)]
    frames = {"15m": d, **{h: resample(d, h) for h in ("1h", "4h", "1d", "1w")}}
    symbol = symbol or os.path.basename(path).split("_")[0] + "/USDT"
    eng = Engine(frames, tf, symbol)
    cands = eng.run()
    ch = eng.ch
    i1 = ch.n - 1
    i0 = max(0, i1 - bars)
    fig, ax = plt.subplots(figsize=(16, 8), dpi=110)
    xs = np.arange(i0, i1 + 1)
    up = ch.c[xs] >= ch.o[xs]
    ax.vlines(xs, ch.l[xs], ch.h[xs], color=np.where(up, "#26a69a", "#ef5350"), lw=0.8)
    ax.bar(xs, np.abs(ch.c[xs] - ch.o[xs]), bottom=np.minimum(ch.o[xs], ch.c[xs]), width=0.6,
           color=np.where(up, "#26a69a", "#ef5350"))
    ax.plot(xs, ch.ema50[xs], color="#ffb300", lw=1, label="EMA 50")
    ax.plot(xs, ch.ema200[xs], color="#7e57c2", lw=1, label="EMA 200")
    za = eng.zones_all(i1)
    colors = {0: "#42a5f5", 1: "#ab47bc", 2: "#8d6e63"}
    lo_view, hi_view = ch.l[xs].min(), ch.h[xs].max()
    for z in za:
        if z.hi < lo_view or z.lo > hi_view:
            continue
        if z.source == "swing":
            ax.axhspan(z.lo, z.hi, xmin=0, xmax=1, color=colors.get(z.level, "#42a5f5"), alpha=0.12 + 0.03 * min(
                z.touches, 4))
            ax.text(i1 + 1, z.mid, f" L{z.level} x{z.touches} s{z.strength:.0f}", fontsize=7, va="center")
        elif z.source in ("pdh", "pdl", "pwh", "pwl"):
            ax.axhline(z.mid, color="#757575", lw=0.7, ls=":")
            ax.text(i0, z.mid, z.source.upper(), fontsize=7, color="#616161", va="bottom")
    for s in eng.bk.sw.swings:
        if s.idx >= i0:
            ax.plot(s.idx, s.price, marker="v" if s.kind == "H" else "^", color="black", ms=6)
    for g in eng.bk.lq.fvgs:
        if g["bar"] >= i0:
            ax.add_patch(plt.Rectangle((g["bar"] - 2, g["lo"]), i1 - g["bar"] + 2, g["hi"] - g["lo"],
                                       color="#66bb6a" if g["side"] == 1 else "#ef9a9a", alpha=0.25, lw=0))
    for c in cands:
        if c["bar"] < i0 or c["vetoes"]:
            continue
        b, pl = c["bar"], c["plan"]
        col = "#1b5e20" if c["side"] == 1 else "#b71c1c"
        ax.annotate("BUY" if c["side"] == 1 else "SELL", (b, pl["entry"]), color=col, fontsize=8, weight="bold",
                    xytext=(b - 3, pl["entry"] * (0.985 if c["side"] == 1 else 1.015)))
        ax.hlines(pl["stop"], b, b + 12, color="#d32f2f", lw=1.2)
        ax.hlines(pl["target"], b, b + 12, color="#2e7d32", lw=1.2)
        ax.hlines(pl["entry"], b, b + 12, color="#424242", lw=0.8, ls="--")
        ax.text(b + 12.5, pl["target"], c["setup"].replace("_", " "), fontsize=6, color=col)
    ax.set_xlim(i0 - 2, i1 + 14)
    ax.set_ylim(lo_view * 0.985, hi_view * 1.015)
    ticks = xs[:: max(1, len(xs) // 10)]
    ax.set_xticks(ticks)
    ax.set_xticklabels([np.datetime_as_string(ch.t[t], unit="D") for t in ticks], fontsize=8)
    ax.set_title(f"{symbol} {tf} - what the agents see (zones: blue = {tf}, purple / brown = higher timeframes; "
                 f"triangles = swings; green / red boxes = fair value gaps)", fontsize=10)
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    return out, [c for c in cands if c["bar"] >= i0]


if __name__ == "__main__":
    a = sys.argv[1:]
    o, cs = draw(a[0], a[1], a[2], int(a[3]) if len(a) > 3 else 220, a[4] if len(a) > 4 else "chart.png")
    print(o, len(cs), "ideas in view")
