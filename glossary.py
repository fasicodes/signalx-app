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
         "A trade idea from the signal engine: Long or Short, with an entry, a stop loss and one or three targets. "
         "It is an estimate, not a promise, and signals lose regularly."),
        ("long", "Long",
         "A trade that profits if the price goes up. You buy now and aim to sell higher, at the target."),
        ("short", "Short",
         "A trade that profits if the price goes down. You sell now (usually with futures) and aim to buy back lower, at the target."),
        ("wait", "Wait",
         "No trade on this coin right now. The models give signals only for their best setups. On the 4-hour stream, if "
         "no coin has had a signal for 24 hours, the best setup gets a standard signal, so there is rarely a long wait "
         "across all coins. The 1-hour stream has no such filler: it can be quiet for days and then give many signals "
         "in one market move."),
        ("setup_forming", "Setup forming",
         "The engine is close to a signal but not there yet. It is not a trade; keep an eye on the next candle close."),
        ("entry", "Entry",
         "The price where a signal starts: the close of the candle (1-hour or 4-hour) that gave the signal."),
        ("stop_loss", "Stop loss",
         "The price where you accept the trade was wrong and exit, so the loss stays small. Every signal has one. Always use it."),
        ("target", "Targets",
         "The prices where you take profit. 4-hour signals have three: TP1 at half the stop distance (0.5R), TP2 at 1R and "
         "TP3 at 2R; a third of the position closes at each, and the stop moves to the entry after TP1 and to TP1 after "
         "TP2. 1-hour signals have one target at 0.5R: the whole position closes there and the stop does not move."),
        ("targets", "Targets reached",
         "How many signals reached their first target (TP1) and the later ones. The first target is half as far as the "
         "stop, so a win is smaller than a full loss: a stream needs clearly more than 2 wins in every 3 signals to make "
         "money."),
        ("tier", "Strong and standard signals",
         "4-hour stream. Strong: the model's score is in its top 3%. Standard: when no coin has had a signal for 24 hours, "
         "the best coin gets one if its score is at least average. On unseen data strong signals reached target 1 about "
         "79% of the time (+0.17R each); standard ones keep signals coming but did less well (about 71%, +0.05R), so "
         "treat them as lower conviction."),
        ("win_chance", "Model confidence",
         "The model's estimate of how often the price reaches the first target (half the stop distance in the signal's "
         "direction) before the stop. A random entry does that about 2 times in 3; in testing the models' signals did it "
         "about 3 times in 4 or more. Higher confidence did better, but no signal is certain."),
        ("progress", "Progress",
         "Where the live price is between the current stop and the next target. 100% to a target means it is reached; "
         "100% to stop means the stop is hit."),
        ("time_limit", "Time limit",
         "If a signal has not closed within 9 days (54 four-hour candles, or 216 hourly candles on the 1-hour stream), "
         "whatever is left closes at that candle's close."),
        ("candle_4h", "4-hour candle",
         "One bar on the chart that covers 4 hours. The engine only reads closed candles: the 4-hour stream gives new "
         "signals right after each 4-hour close (six times a day), and the 1-hour stream reads the last four hours at "
         "every hourly close."),
        ("leaning", "Leaning",
         "The side the model favours right now, Long or Short, with its probability. It shows the direction even when it is "
         "not strong enough for a signal."),
        ("closeness", "Closeness to a signal",
         "How close the model's conviction came to the strong-signal line at the last candle close. 100% means a strong signal."),
    ]),
    ("Risk and results", [
        ("r", "R (risk unit)",
         "1R is the amount you lose if the stop is hit. If you risk $10 on a signal, +1R is a $10 gain and −1R is a $10 loss. "
         "Keep 1R small, for example 1% of your account."),
        ("win_rate", "Win rate",
         "The share of closed signals that reached their first target. A high win rate alone does not mean profit: it "
         "depends on where the target is. With the first target at half the stop distance, a random entry wins about 2 "
         "times in 3, and one full loss costs as much as two first-target wins, so look at the average R after fees too."),
        ("avg_r", "Average result (R)",
         "The average result per signal in R, after fees. Above 0 means the signals made money overall in that period."),
        ("profit_factor", "Profit factor",
         "Total gains divided by total losses. Above 1 means the gains were bigger than the losses."),
        ("drawdown", "Drawdown",
         "The biggest drop in results from a high point before a new high was made. It shows how bad a losing streak got."),
        ("backtest", "Backtest",
         "Running the same rules over past data to see how they would have done. Useful, but real trading can do worse."),
        ("unseen_data", "Tested on unseen data",
         "Each model learned from older data and was then tested once on later data it had never seen (October 2025 to "
         "November 2026), with trading costs. Only streams that passed that test give signals."),
        ("fees", "Fees",
         "What the exchange charges to open and close a trade. Every tested result already takes off 0.30% per trade for fees and slippage, plus funding for longs."),
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
    ("Liquidity scanner", [
        ("order_book", "Order book",
         "The buy orders (bids) and sell orders (asks) waiting on the exchange at each price. Large piles of orders can slow "
         "the price down or pull it toward them. Orders can be cancelled at any moment."),
        ("spread", "Spread",
         "The gap between the best buy price and the best sell price. A small spread means the market is liquid and cheap to trade."),
        ("imbalance", "Book imbalance",
         "How much more money is waiting to buy than to sell near the price, or the other way round. It can change in seconds."),
        ("wall", "Order wall",
         "An unusually large order at one price, many times bigger than a normal level. Price often pauses there, and "
         "sometimes the wall is pulled before price arrives."),
        ("depth_chart", "Depth chart",
         "Adds up the buy orders below the price (green) and the sell orders above it (red). Steep steps are walls; "
         "flat stretches are thin liquidity where price can move fast."),
        ("liquidity_sweep", "Liquidity sweep",
         "Price pokes past a recent high or low, sets off the stop orders resting there, then closes back inside the range. "
         "It often marks a short-term turn, but not always."),
        ("stop_pool", "Stop pool (equal highs and lows)",
         "Two or more highs, or lows, at about the same price that price has not broken yet. Many traders keep stops just "
         "beyond them, so price is often drawn there."),
        ("liquidation_zone", "Liquidation zones (estimated)",
         "Prices where traders who opened leveraged positions recently would be closed by force. Signals FM estimates them "
         "from price, volume and common leverage (10x to 100x). Exchanges do not publish the real ones."),
        ("taker_flow", "Taker buy and sell",
         "Who is crossing the spread right now. Takers buy at the ask or sell at the bid straight away; more taker buying means "
         "more urgent buyers."),
        ("large_trades", "Large trades",
         "The biggest recent trades on the exchange's public tape, larger than about 98% of all trades. Often big players, "
         "but you cannot see who placed them."),
        ("open_interest", "Open interest",
         "How many futures contracts are still open. Rising open interest with a move means new money is joining it."),
        ("cvd", "Volume delta (CVD)",
         "Buying volume minus selling volume, added up over time. A rising line means buyers have been the more aggressive side."),
        ("spoofing", "Possible spoofing",
         "A large order that vanished between two snapshots without being traded. It may be a fake order meant to push others, "
         "or a normal cancel; the data cannot tell which."),
        ("trap", "Trap and squeeze risk",
         "Rough scores for a false breakout (a trap) or a rush of forced exits from shorts or longs (a squeeze), from sweeps, "
         "order flow and funding. A hint, not a forecast."),
        ("likely_target", "Likely target",
         "The order-book level price seems most drawn to right now, weighing each cluster's size against its distance."),
    ]),
    ("Live chart", [
        ("signal_history", "Signal history",
         "The signals the engine gave on this coin with its normal rules over about the last 50 days, and how each one ended: "
         "all three targets, a target and then the moved stop, the stop, or the time limit. Replayed results, fees included."),
        ("heikin_ashi", "Heikin Ashi",
         "Averaged candles that smooth out the noise so a trend is easier to see. Their open and close are not real traded prices."),
        ("log_scale", "Log scale",
         "Each equal step on the price axis is the same percentage move. It makes long histories and big moves easier to compare."),
        ("volume", "Volume",
         "How much was traded in each candle. Big moves on high volume are more convincing than moves on low volume."),
    ]),
]

GLOSSARY = {key: {"term": term, "text": text} for _group, items in GROUPS for key, term, text in items}


def groups():
    """[(group name, [{"key", "term", "text"}, ...]), ...] for the FAQ page."""
    return [(name, [{"key": k, "term": t, "text": x} for k, t, x in items]) for name, items in GROUPS]
