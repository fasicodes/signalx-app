# Research: how the signal stream models are trained and tested

These scripts are not part of the web app. They produce `models/<market>_<timeframe>.json` (the models the site
runs) and `models/research.json` (every version that was tested, including the ones that failed, shown on the
Signals page).

## Data

| Market | Source | Candles | Period |
|---|---|---|---|
| Crypto, 53 coins | Binance spot 1-minute klines from public GitHub repositories (`fetch_crypto.py COIN 2020`), aggregated to 15 minutes | 15m, 1h and 4h built from them | 2020-03 to 2026-10-11 |
| Forex, 11 pairs + gold | MetaTrader 15-minute candles from github.com/ejtraderLabs/historical-data (broker time = New York + 7 h, converted to UTC) | 15m, 1h, 4h | 2012-11 to 2022-03-04 |

Put the files in `research/work/data/crypto/<COIN>_15m.csv.gz` and `research/work/data/fx/<PAIR>m15.csv`
(or set `RESEARCH_DIR`). Nothing else is needed: features come from the app's own `features.py`, the trade
plan and the selection policy from the app's own `streams.py`, so the research and the site run the same code.

## Protocol (no look-ahead, no tuning on the test period)

* **Label**: the trade's first target (TP1 = 0.5 R) is reached before the stop (stop = `sl_atr` x ATR). A candle
  that touches both counts as a stop (pessimistic).
* **Trade plan** (the same on the site): targets 0.5 R / 1 R / 2 R, a third closed at each, the stop moves to the
  entry after TP1 and to TP1 after TP2, a time limit per stream. A **win** is a signal that reached at least TP1.
  Results are after costs (crypto 0.12% round trip; forex the typical spread per pair).
* **Walk-forward** over the validation years (crypto 2023-01-01 to 2025-10-01, forex 2017-01-01 to 2021-01-01):
  for every 6-month fold the model is trained only on earlier data, with a purge gap of one label horizon.
* **Selection rule (v2)**, applied to the walk-forward predictions only: the lowest score threshold whose
  validation result has a win rate of at least 73%, an average of at least +0.03 R after costs, every validation
  year at least 70% with a positive average, and at least 100 signals. Grades: A / B = the score levels whose
  validation win rate reached 80% / 75%. (Rule v1 - 72%, +0.02 R, no yearly check - failed the crypto 1h
  holdout; v2 was fixed before any other holdout result was seen.)
* **Multi-timeframe variant**: the signal also needs the higher-timeframe model (1h for 15m, 4h for 1h) to lean
  the same way with a score of at least a gate level; gate level and threshold are chosen on validation only.
* **Which version goes live** (fixed before the new holdout results): among the versions that pass the
  selection rule on validation, the multi-timeframe one is preferred, else the plain one. That one version is
  scored once on the holdout (crypto 2025-10-01 to 2026-10-11, forex 2021-01-01 to 2022-03-05) with the final
  model trained on everything before it. The stream goes live only if the holdout shows at least 70% wins, a
  positive average after costs and at least 50 signals. No other version is promoted because of its holdout.
* **Before**: `lab.py rules <stream>` replays the rules engine R1 (the first version of the streams) on the same
  periods with the same plan and costs.

## Commands

```bash
python lab.py build   crypto_1h                    # features + labels per symbol
python lab.py wf      crypto_1h s2                 # walk-forward + holdout predictions
python lab.py eval    crypto_1h s2                 # plain version: threshold on validation, then the holdout
python lab.py mtf     crypto_1h s2 crypto_4h s2    # multi-timeframe version
python lab.py export  crypto_1h s2                 # or export_mtf: writes models/crypto_1h.json
python lab.py rules   crypto_1h                    # rules R1 baseline
python lab.py summary all                          # models/research.json (what is live and why)
python legacy_prompt.py parity 4h                  # the site's first engine (the 5-model prompt), vectorised == main.py
python legacy_prompt.py run 4h                     # its backtest (1h too); nothing is fitted
python prompt_lab.py pbuild 4h && python prompt_lab.py run 4h nb   # the prompt's ideas as a calibrated model
```

`crypto_1hf` / `crypto_4hf` are the same models with order-flow inputs (taker buy volume, trade counts; `flow=True`).

Geometries: `s2` = stop 2 ATR, `s3` = stop 3 ATR, `s15` = stop 1.5 ATR (targets always 0.5 / 1 / 2 R).

## Results

See `models/research.json` (written by `lab.py summary`) and the final report in the main README.

### Experiments after the first release (all with the protocol above; the holdout is never used to choose)

**1. The "Professional AI Trading Prompt" (Hawkes / Bayesian / quantile volatility / conformal / Kelly)**
is the site's first engine (`main.py`: `hawkes_pressure`, `bayesian_bullish_bearish`, `quantile_volatility`,
`conformal_confidence`, `fractional_kelly`). `legacy_prompt.py` runs those exact functions on the last 200
closed candles of every coin (a vectorised copy that gives the same verdict as `main.py` on 300 random
windows, 0 differences). Nothing in it is fitted, so 2023-01-01 to 2026-10-11 is all out of sample.

| 53 coins | next-candle direction right (trade verdicts) | candles the up-share was | own plan (stop = 95th pct move, target 1.5x): wins / average after costs | common plan (stop 2 ATR, 0.5/1/2 R): wins / average |
|---|---|---|---|---|
| 4h, 2023-01 to 2025-09 | 49.5 % | 49.2 % | 40.0 % / -0.033 R | 66.6 % / -0.018 R |
| 4h, holdout | 50.8 % | 47.5 % | 41.3 % / -0.004 R | 68.1 % / +0.003 R |
| 1h, 2023-01 to 2025-09 | 49.5 % | 48.6 % | 40.0 % / -0.073 R | 66.4 % / -0.055 R |
| 1h, holdout | 51.4 % | 46.9 % | 39.8 % / -0.081 R | 67.6 % / -0.041 R |

It says trade on about half of all candles (30 signals a day on 4h, 120 on 1h) and is right about as often as a
coin flip. "Bullish 70 %+" was followed by an up candle 48 % of the time, so its Kelly sizing (which uses that
percentage as the win probability) is not usable. In the common plan the win rate is the base rate of the plan
(66 %), not skill.

**2. The prompt's ideas rebuilt as a calibrated model** (`prompt_lab.py`: Hawkes-style buy / sell intensity from
taker volume, naive Bayes or gradient boosting on those inputs plus trend / momentum / volatility, conformal
confidence 70 %, volatility filter, Kelly from the validation results):
naive Bayes with the prompt's rule (confidence >= 70 %): validation 71.4 % / +0.059 R, holdout **65.0 % / -0.056 R**;
gradient boosting on the same inputs (v2 rule): validation 73.5 %, holdout **68.9 % / +0.015 R** (fails 70 %).
Without the market-wide and higher-timeframe inputs the models do not hold on unseen data.

**3. Order-flow inputs** (taker buy volume, trade counts, `features.flow_block`) do not help.
4h: at the same number of signals the plain and the order-flow model are the same (validation at 1.9 / day:
73.7 % vs 73.0 %; holdout at 1.8 / day: 81.0 % vs 79.0 %). 1h: validation 73.3 %, holdout 69.8 % / +0.021 R
alone and 69.9 % / +0.035 R with the 4h gate, against 72.6 % / +0.083 R without order flow. They are not used,
so the live engine does not need taker volume.

**4. Crypto 4h research model vs engine v3.1.** The research pipeline gives a 4h model that also passes
(validation 73.7 % at 1.9 signals / day; holdout 86.5 % at 0.7 / day, 81.0 % / +0.257 R at 1.8 / day when the
threshold of the 98th/95th validation percentile is used); engine v3.1 (live) had 73.6 % / +0.105 R at 2.0 / day.
Both hold above 70 %, so the 4h edge is real, but holdout signals come in bursts (the 649-signal order-flow
variant fired on only 61 of 375 days, up to 30 in one day), so the holdout numbers are less certain than the
signal counts suggest. The research 4h model is used as the gate of the 1h stream; engine v3.1 stays the 4h stream.

**5. Crypto 1h with the 4h model agreeing** (the multi-timeframe variant; the gate level and the threshold come
from the validation years only): validation 73.3 % / +0.073 R (9.1 / day; 74.4 %, 71.8 %, 72.8 % per year),
holdout **72.6 % / +0.083 R, 1,691 signals (4.5 / day)** -> passes the live bar. The same model without the gate
fails (holdout 68.1 % / -0.004 R).

**6. Crypto 15m model** (same protocol; 53 coins, 2023-01 to 2025-09 walk-forward; the holdout was not opened because no
version passed validation). Four versions: stop 3 ATR and stop 2 ATR, each alone and with the 1h model agreeing
(`python lab.py wf crypto_15m s3`, `eval crypto_15m s3`, `mtf crypto_15m s3 crypto_1h s2`, the same for `s2`).
Closest level of each (at least 100 signals, best average result): s3 alone 71.1 % wins / +0.026 R at 6.2 signals a day;
s3 with the 1h model 71.4 % / +0.029 R at 6.9 a day; s2 alone 71.3 % / -0.033 R; s2 with the 1h model 71.6 % / -0.034 R.
The target was 73 % and +0.03 R with every year above 70 % and positive. In the best version 2023 and 2024 were about 72 %
and +0.04 R, and 2025 fell to 62-64 % and -0.1 R, so the edge is small and shrinking. Not used; the stream runs rules R1
and is labelled untested.
