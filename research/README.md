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
```

Geometries: `s2` = stop 2 ATR, `s3` = stop 3 ATR, `s15` = stop 1.5 ATR (targets always 0.5 / 1 / 2 R).

## Results

See `models/research.json` (written by `lab.py summary`) and the final report in the main README.
