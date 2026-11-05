"""chart_agents: a human-style chart engine for SignalX.

A team of agents reads the chart the way a price-action trader does - top-down trend, market structure, support /
resistance zones, chart patterns, candlesticks, momentum, volume, liquidity (stops, sweeps, fair value gaps,
market depth), session / volatility and news - a critic looks for reasons not to trade, a risk manager places the
stop and target after costs, and a head trader decides BUY / SELL / WAIT and explains why in plain English.
A journal records which agents really help.

    from chart_agents import Engine, HeadTrader
    eng = Engine(frames, "4h", "BTC/USDT")       # frames: {"4h": df, "1d": df, "1w": df}
    cands = eng.run(last_only=True)              # the newest candle's trade idea, fully judged (or nothing)
    eng.report()                                 # what every agent sees on the chart right now

Whether a timeframe may send signals is decided by the honest test in research/human_lab (same rules as every
SignalX stream: unseen data, real costs).
"""
from .config import VERSION  # noqa: F401
from .engine import Engine  # noqa: F401
from .head import HeadTrader  # noqa: F401
