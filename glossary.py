"""
Plain-language explanations for the "?" buttons across Signals FM.

One list, used in two places:
  * GET /glossary.json  -> the "?" popovers on every page load it once
  * /faq#glossary       -> the full list, with an anchor per term (/faq#g-<key>)

Keep each text short (one or two sentences) and free of jargon.
"""

GROUPS = [
    ("Signals", [
        ("signal", "Signal",
         "A trade idea from the signal engine: Long or Short, with an entry, a stop loss and a target. "
         "It is an estimate, not a promise, and signals lose regularly."),
        ("long", "Long",
         "A trade that profits if the price goes up. You buy now and aim to sell higher, at the target."),
        ("short", "Short",
         "A trade that profits if the price goes down. You sell now (usually with futures) and aim to buy back lower, at the target."),
        ("wait", "Wait",
         "No trade right now. The engine only gives a signal for its strongest setups, so most of the time the answer is Wait. "
         "That is the engine skipping weak setups, not a fault."),
        ("setup_forming", "Setup forming",
         "The engine is close to a signal but not there yet. It is not a trade: when this band was tested as signals, "
         "it lost money after fees."),
        ("entry", "Entry",
         "The price where a signal starts: the close of the 4-hour candle that gave the signal."),
        ("stop_loss", "Stop loss",
         "The price where you accept the trade was wrong and exit, so the loss stays small. Every signal has one. Always use it."),
        ("target", "Target",
         "The price where you take the profit. Also called take profit."),
        ("win_chance", "Win chance",
         "The model's estimate of how often signals like this one reach the target before the stop. "
         "In testing about 7 in 10 signals won, but each loss was about twice the size of each win."),
        ("progress", "Progress",
         "Where the live price is between the stop and the target. 100% to target means the target is reached; "
         "100% to stop means the stop is hit."),
        ("time_limit", "Time limit",
         "If neither the stop nor the target is hit within 48 four-hour candles (8 days), the signal closes at that candle's close."),
        ("candle_4h", "4-hour candle",
         "One bar on the chart that covers 4 hours. The engine only reads closed candles, so new signals appear right after "
         "each 4-hour close (six times a day)."),
        ("leaning", "Leaning",
         "The side the model favours right now, Long or Short, with its probability. It shows the direction even when it is "
         "not strong enough for a signal."),
        ("closeness", "Closeness to a signal",
         "How close the model's conviction came to the signal line at the last candle close. 100% means a signal."),
    ]),
    ("Risk and results", [
        ("r", "R (risk unit)",
         "1R is the amount you lose if the stop is hit. If you risk $10 on a signal, +0.5R is a $5 gain and −1R is a $10 loss. "
         "Keep 1R small, for example 1% of your account."),
        ("win_rate", "Win rate",
         "The share of closed signals that reached the target. A high win rate alone does not mean profit when losses are bigger than wins."),
        ("avg_r", "Average result (R)",
         "The average result per signal in R, after fees. Above 0 means the signals made money overall in that period."),
        ("profit_factor", "Profit factor",
         "Total gains divided by total losses. Above 1 means the gains were bigger than the losses."),
        ("drawdown", "Drawdown",
         "The biggest drop in results from a high point before a new high was made. It shows how bad a losing streak got."),
        ("backtest", "Backtest",
         "Running the same rules over past data to see how they would have done. Useful, but real trading can do worse."),
        ("unseen_data", "Tested on unseen data",
         "The model learned from January 2022 to June 2025 and was then tested once on July 2025 to October 2026, "
         "data it had never seen. That keeps the test honest."),
        ("fees", "Fees",
         "What the exchange charges to open and close a trade. Every tested result already takes off 0.12% per trade for fees and slippage."),
        ("leverage", "Leverage",
         "Borrowed money that makes a position bigger. At 10x, a 10% move against you wipes out the margin. Keep leverage low."),
    ]),
    ("Market brief", [
        ("trend", "Trend",
         "The general direction of the price on 4-hour candles: up when the price is above its 50- and 200-candle averages, "
         "down when it is below them, sideways when they disagree."),
        ("daily_trend", "Daily trend",
         "The same idea on daily candles. It shows the bigger picture."),
        ("momentum", "Momentum (RSI)",
         "RSI measures how strongly the price has moved lately, from 0 to 100. Around 50 is neutral; above 70 is often called "
         "overbought and below 30 oversold."),
        ("volatility", "Volatility",
         "How much the price usually moves per candle. Higher volatility means wider stops and bigger swings, so trade a smaller size."),
        ("range", "Recent range",
         "The highest and lowest prices of the recent period, and how far the current price is from each."),
    ]),
    ("Market internals", [
        ("order_flow", "Order flow",
         "Compares aggressive buying and selling in the order book. Positive means buyers are pushing harder; negative means sellers are."),
        ("vpin", "Toxic flow (VPIN)",
         "Estimates how much recent trading comes from well-informed traders. High values often come before sharp moves."),
        ("regime", "Market regime",
         "Whether the market is trending or moving sideways (ranging), estimated by a statistical model on 1-hour candles."),
        ("market_strength", "Market strength",
         "A 0 to 100 score from buying pressure, order flow and order-book depth. Above 50 leans up, below 50 leans down."),
        ("funding", "Funding rate",
         "A small payment between futures traders every few hours. Positive means longs pay shorts (more traders are long); "
         "negative means shorts pay longs."),
        ("liquidity_magnet", "Liquidity magnet",
         "A price level with a large cluster of orders. Prices are often pulled toward these levels."),
        ("crash_risk", "Crash risk",
         "A 0 to 100 warning score from sudden jumps, breaks in the price pattern and toxic flow. Higher means a sharp drop is more likely than usual."),
        ("channels", "Channels agreeing",
         "How many of the analysis channels in the Pro terminal point the same way as the current verdict. "
         "More agreement means a clearer picture, not a guarantee."),
    ]),
]

GLOSSARY = {key: {"term": term, "text": text} for _group, items in GROUPS for key, term, text in items}


def groups():
    """[(group name, [{"key", "term", "text"}, ...]), ...] for the FAQ page."""
    return [(name, [{"key": k, "term": t, "text": x} for k, t, x in items]) for name, items in GROUPS]
