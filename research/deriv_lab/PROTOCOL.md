# Derivatives lab: does futures positioning give an edge on 15m / 1h / 4h? (pre-registered 2026-11-06)

Written BEFORE any result was computed. Nothing below may change after the holdout is looked at.

## Why
117 candle-only tries (research/human_lab) and 47 public strategies (research/repo_lab) found no lasting edge
outside crypto 4h engine 3.2. The open idea left there: information that is not in the candles - Binance USD-M
futures positioning (funding rate, open interest, top-trader and all-account long/short ratios, taker buy/sell
ratio, premium index).

## Data
* 40 coins (`work/coins.txt`), Binance USD-M perpetual 15m klines 2021-01 to 2026-10 (`fetch_klines.py`),
  1h and 4h bars resampled from them.
* Positioning from `../human_lab/download_derivs.py` (hourly). A bar only sees positioning stamped before it
  closed (hourly value of hour H is used from H+1h on).

## Trade plan (fixed, per timeframe)
Entry at the NEXT bar's open after the signal bar closes. Stop 2 x ATR(14), one target at 2R, time exit after
max_bars (15m: 32, 1h: 36, 4h: 30). A bar touching stop and target counts as stop. Costs: 0.30% per round trip
(markets.CRYPTO_COST_ROUND_TRIP) plus the real funding paid / received while the trade is open. Result in R.
One open trade per coin and timeframe; at most 3 new signals per bar (best score first).

## Periods
* Validation: 2022-01-01 to 2024-12-31 (positioning metrics start around Dec 2021). Walk-forward in 6-month
  folds, each fold trained only on earlier data, purge gap = max_bars.
* Holdout (looked at ONCE): 2025-01-01 to the last full month. Final model trained on everything before 2025.

## Candidates (all fixed now)
* M (model): LightGBM regressor per side (long / short) predicting the trade's net R from price features
  (returns 1/4/16/64 bars, ATR %, RSI 14, distance to EMA 20/50/200 in ATR, volume z-score, taker buy share)
  and positioning features (funding, funding z 30d, OI change 1/4/24 bars, OI z 30d, top-trader L/S z,
  account L/S z, taker L/S ratio, premium, premium z) plus their cross-sectional ranks across coins on the bar.
  Fixed hyper-parameters: 300 trees, lr 0.03, 31 leaves, min 200 samples per leaf, subsample 0.8, colsample 0.8.
* H1 (crowding fade): funding z > 2 AND top-trader L/S z > 1.5 AND OI up > 10% in 24h -> short; mirror -> long.
* H2 (squeeze): close above the 48-bar high AND OI up over 24h AND funding < 0 -> long; mirror -> short.

## Choosing (validation only)
For M, the score threshold is one of the top 1% / 2% / 5% / 10% of validation predictions: the lowest one whose
validation result is >= +0.05R per signal, profit factor >= 1.1, positive in every validation year, >= 200
signals. If none qualifies, M is not tested on the holdout for that timeframe. H1 / H2 go to the holdout only if
they meet the same validation bar. Among qualifying candidates for a timeframe, the one with the highest
validation avg R is the ONE sent to the holdout.

## Amendment 1 (2026-11-06, after the first validation run, BEFORE any holdout number was computed)
A data bug, not a rule change: ICP's futures candles are flat for weeks in 2022 (no trading), the ATR decays to
~1e-64 and a "trade" there loses ~-1e62 R (1h M top10% showed -346R per signal). Fix: no trade on a bar without
volume or whose 2-ATR stop is under 0.3% of the price (the cost alone would be more than 1R). The selection rules
above are unchanged; validation is re-run for every timeframe with the fix and the holdout still runs once.

## Going live
The holdout result must pass `streams.passed_test()` (>= 50 signals, >= +0.05R per signal after costs, profit
factor >= 1.1, drawdown < half of the total). Fail = the stream stays off, and that is written down.
