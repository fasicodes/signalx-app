# Human lab: does a team of chart-reading agents make money?

Not part of the web app. It tests `chart_agents/` (the human-style chart engine) with the same honest rules as every
SignalX stream, and writes `results.json`. A stream may send signals only if its one holdout test passes
`streams.passed_test()`; the head trader it would run is then written to `models/human_crypto_<tf>.json`.

## The agents (chart_agents/)

| Agent | File | What it does, like a trader |
|---|---|---|
| Swings | `swings.py` | marks swing highs / lows (confirmed 3 candles later), zigzag of the swings that matter |
| Market structure | `structure.py` | higher highs / higher lows, break of structure, change of character, order blocks, premium / discount |
| Support / resistance | `zones.py` | zones where price turned (entry chart + two higher timeframes), touches, flips, previous day / week high and low, round numbers; one zone per area, the 10 strongest |
| Chart patterns | `patterns.py` | double / triple bottom and top, head and shoulders (and inverse), triangles, wedges, flags, ranges, channels, breakout of their line |
| Candlesticks | `candles.py` | engulfing, hammer / shooting star, morning / evening star, piercing / dark cloud, tweezers, inside-bar breaks, three soldiers / crows, marubozu, doji, rejection wicks |
| Momentum | `momentum.py` | regular and hidden RSI divergence, RSI levels, MACD turn / cross, stochastic, ADX / DI |
| Volume | `volume.py` | breakout volume, pullback volume, climax, OBV and OBV divergence, taker buy share, VWAP |
| Liquidity | `liquidity.py` | 24 h USD volume (thin markets), buy- / sell-side pools, equal highs / lows, sweeps (stop hunts), draw on liquidity, stops next to a pool, fair value gaps |
| Top-down | `topdown.py` | trend of the two higher timeframes (only closed candles) |
| Session / news | `session.py` | dead or panicking market, weekends, forex sessions; live: SignalX's economic calendar blackout |
| Critic | `critic.py` | reasons NOT to trade: against both higher timeframes, against the trend, chasing, exhaustion, chop, wide stop |
| Risk manager | `risk.py` | stop beyond the structure (and beyond nearby liquidity), target at the measured move / liquidity / next zone, never past a strong zone, reward-to-risk after costs, portfolio rules |
| Setups | `setups.py` | trend pullback, breakout and retest, pattern breakout, liquidity sweep, range / channel edge, order block |
| Head trader | `head.py` | learned weights or a human checklist -> BUY / SELL / WAIT + an explanation in plain English |
| Journal | `journal.py` | which agents and setups actually help |

The backtest and the site run the same code (`engine.py`). Checked in `tests/test_chart_agents.py`: cutting the
future away changes nothing before the cut (no look-ahead), and a live replay of the last 3,000 candles gives the
same decision as the full backtest.

## Data

28 coins (BTC ETH SOL BNB XRP DOGE ADA AVAX LINK LTC DOT TRX BCH ATOM NEAR UNI ETC FIL APT ARB OP INJ SUI AAVE XLM ICP
SHIB PEPE), Binance spot 15-minute candles Jan 2021 - Oct 2026 from `../fetch_crypto.py COIN 2021` (with taker buy
volume and quote volume). Put the files in `work/data/crypto/` (or set `HUMAN_LAB_DIR`).

## Protocol (fixed before the holdout was looked at)

See the header of `lab.py`. In short: every trade idea of every coin is played out with real costs (0.30% per round
trip + funding for longs) and pessimistic fills; validation 2021-03 to 2024-12 with a half-yearly walk-forward for
the learned head trader; the rule (>= 80 trades, >= +0.05R, profit factor >= 1.1, every year 2022-2024 positive) picks
the version; the holdout (2025-01 to Oct 2026) is scored once, and only if a version passed validation.

## Commands

```bash
python lab.py prep          # frames from the 15m files
python lab.py cands 4h      # every trade idea, played out (also 1h, 15m)
python lab.py val 4h        # validation years only (used while building; never shows the holdout)
python lab.py test 4h       # the protocol -> results.json
python charts.py work/data/crypto/BTC_15m.csv.gz 4h 2021-08-20 200 btc.png   # what the agents see
```

## Result (Nov 2026): nothing goes live

| Stream | Trade ideas 2021-2024 | Every idea, 2022-2024 (portfolio rules) | Best head trader on 2022-2024 | Holdout (2025-01 to 2026-10) |
|---|---|---|---|---|
| crypto 4h | 14,241 | 30.7% wins, -0.10R per trade | learned: 298 trades, -0.04R (no version passes) | not run (nothing passed) |
| crypto 1h | 40,929 | 29.7% wins, -0.14R per trade | learned >= 0.55 passed by a hair (186 trades, +0.07R; its neighbours fail) | 40 signals, 32.5% wins, -0.04R, PF 0.94: **failed** |
| crypto 15m | 64,742 | 28.4% wins, -0.23R per trade | learned: 292 trades, -0.11R (no version passes) | not run (nothing passed) |

What the numbers say:
* Before costs the agents' trade ideas are worth about 0R (4h: -0.04R gross). With the stop where the agents put it
  and a target of just 1R, 48% of the 4h ideas reach the target first - a coin flip.
* More agents agreeing does not help (4h checklist: 3 agents -0.08R, 6 agents -0.19R). The learned head trader finds
  no stable edge in 60 agent features either.
* Most ideas are rejected by the risk manager before they become trades: a strong zone too close for a worthwhile
  target, or (15m) a stop too close for the 0.30% cost.
* This matches the repo lab: classic chart reading (support / resistance, patterns, candlesticks, smart-money
  liquidity, divergences) does not, by itself, make money on these coins after real costs.

The engine is still useful as an analyst: `chart_agents.live.analyze()` returns what every agent sees on a chart
right now (trend per timeframe, zones, patterns, liquidity pools, fair value gaps, swings) and explains any trade idea
in plain English - as analysis, labelled as not tested, never as a signal.

## Round 2 (Nov 2026): one fix for every reason round 1 failed - `lab2.py`, `results2.json`

| Why round 1 failed | Fix tested in round 2 |
|---|---|
| entries were a coin flip | gradient-boosting and logistic head traders on the agents' readings + market context (Bitcoin trend / momentum, breadth of the 28 coins, median move, dispersion, strength vs Bitcoin and vs the other coins) |
| costs ate 0.11-0.16R a trade | the cost is part of every trade's expected result; variant "cheap": only trades whose costs are at most 0.08R (wide enough stop); a 0.12% futures-like cost reported for information |
| obvious levels get hunted | new setup `failed_breakout` (trap) in `setups.py` |
| late entries | plan "patient" (limit 0.25 ATR better, 3 candles, unfilled = no trade) and plan "1R" (one target at 1R) |
| a lucky single setting | plateau rule: both neighbouring thresholds must also make money |

12 variants per timeframe (3 plans x 2 head traders x all / cheap trades), chosen on the 2022-2024 walk-forward only.
Because the 1h holdout had been seen once in round 1, the holdout had to pass `streams.passed_test()` AND be positive
in 2025 AND in 2026.

| Stream | Variants passing validation | Chosen | Holdout 2025-01 to 2026-10 |
|---|---|---|---|
| crypto 4h | 3 (all "cheap") | agents plan, gradient boosting, cheap, >= 0.35 | 384 signals, 30.7% wins, -0.14R, PF 0.80, both years negative: **failed** |
| crypto 1h | 1 | agents plan, logistic, cheap, >= 0.50 | 114 signals, 38.6% wins, -0.15R, PF 0.73: **failed** |
| crypto 15m | 0 | - | not run (nothing passed) |

Information only (never used to choose): the other two 4h variants that passed validation also lost on the holdout
(patient / logistic / cheap: 405 signals, -0.00R; 1R / gradient boosting / cheap: 20 signals, -0.02R).

The trap setup on its own (2021-2024): 4h 328 trades -0.03R, 1h 1,446 trades -0.12R, 15m 3,638 trades -0.19R.

The cost filter was the one fix that turned validation results positive (4h: every patient-entry idea with costs
<= 0.08R made +0.03R on 2022-2024, the learned head trader +0.17R), but none of it held up on 2025-2026: what the
models learned on 2022-2024 did not carry over.

## The research loop (Nov 2026): 117 tries, every one in `loop_log.json`

Problem -> fix -> test, again and again, on the validation years only; anything promising then gets ONE
pre-registered final test (2025-2026 and coins never used anywhere: 38 fresh coins downloaded for this).

| Round | Problem found | Fix tested | Result on validation | Final test |
|---|---|---|---|---|
| 1 (`loop.py`) | the market's mood changes | regime filter (Bitcoin + breadth), relative strength, cheap trades | losses shrink to about 0R, never positive enough | - |
| 2 | human rules fight crypto: buying the dip / not chasing lose, momentum wins (every year) | momentum filters (RSI, stretch, ADX) | best +0.02R | - |
| 3 | the agents' stops are tight (3.5% vs ~7% for engine 3.2) | 2.5 ATR stop, 1R / 1.5R | regime + wide stop +0.03R, positive every year 2021-2024 | - |
| 4 | is it the agents or the market? | same trades on RANDOM candles in the same regime | random = agents (+0.02 vs +0.03R): **the agents add nothing**, the small edge is the market's trend | - |
| 5 (`trend_lab.py`) | so trade the market's trend directly | trend trader: bull market -> buy a coin's N-candle breakout, 3 ATR stop, 2R | 4h: 7 of 8 variants positive in EVERY year 2021-2024 (+0.07 to +0.11R) | **failed** (`trend_final.json`): 28 coins 2025-26 -0.05R; 38 fresh coins +0.04R over 2021-26 but 0.00R in 2025-26 |
| 6 (`fx_lab.py`) | crypto 2025-26 was hostile to trends; try forex | the same trend trader on 12 forex pairs + gold, 2013-2021 | about 0R in all 12 variants | - |
| 7 (`ml2r_lab.py`) | rules do not survive; engine 3.2 (a model on 82 measurements) did | the 3.2 measurements + gradient boosting, ONE target at 2R | +0.01 to +0.07R, 2022 or 2024 negative | - |
| 8 | is 2R the problem? | same model learning TP1, plans 1R / half at 1R + half at 2R / 2R | about 0R for all three (top 1% looks good only because of 2024) | - |

What 117 tries say: on these coins and pairs, with real costs, candles alone do not hold a lasting edge for the
agents - what worked in 2021-2024 (riding the crypto market's trend) did not work in 2025-2026. The next round needs
information that is not in the candles: Binance futures positioning (funding rate, open interest, long/short
ratios, premium index) - `download_derivs.py` fetches it (run it on a computer that can reach data.binance.vision;
it is blocked from the research sandbox). Liquidation history is not in Binance's public data.
