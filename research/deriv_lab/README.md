# Derivatives lab (Nov 2026): does futures positioning give an edge on 15m / 1h / 4h?

Not part of the web app. The rules were written down before any result was computed (`PROTOCOL.md`, one
amendment for a data bug, made before the holdout was run). Result: **no new stream goes live.**

## What was tested
* 40 coins, Binance USD-M perpetual candles 2021-01 to 2026-10 (`fetch_klines.py`) and Binance's public
  positioning data (`../human_lab/download_derivs.py`): funding rate, open interest, top-trader and all-account
  long/short ratios, taker buy/sell ratio, premium index.
* Same trade for every idea: entry at the next candle's open, stop 2 ATR, target 2R, time limit, 0.30% cost per
  round trip plus the real funding paid or received, a candle touching stop and target counts as a stop.
* Ideas: M (a LightGBM model on price + positioning features), H1 (fade a crowded side), H2 (squeeze breakout:
  a 48-candle breakout while open interest rises against negative funding, and the mirror image).
* Validation 2022-2024 (walk-forward, each half-year trained only on earlier data); holdout 2025-01 to 2026-10,
  run once, only for the candidate the validation rule chose.

## Results (`results.json`, `val_*.json`)
| Timeframe | Chosen on validation | Validation 2022-2024 | Holdout 2025-2026 | Live |
|---|---|---|---|---|
| 15m | none: every idea lost money after costs (best: H1 -0.05R) | - | not run | no |
| 1h | none: the model was positive in 2022 only (2023 below zero, almost no 2024 signals) | - | not run | no |
| 4h | H2 squeeze breakout | 1,822 signals, +0.123R per signal, PF 1.22, positive every year | 1,152 signals, **+0.002R**, PF 1.00 | no |

The pass rule (`streams.passed_test`) needs at least +0.05R per signal after costs and a profit factor of 1.1 on
the holdout. What worked in 2022-2024 earned nothing in 2025-2026, the same pattern as the 117 candle-only tries in
`../human_lab`.

## Commands
```bash
python fetch_klines.py $(cat work/coins.txt)          # candles (about 270 MB)
python ../human_lab/download_derivs.py $(cat work/coins.txt)   # positioning, DERIVS_DIR=work/derivs
python lab.py build 4h && python lab.py val 4h        # features, labels, walk-forward, the one chosen candidate
python lab.py holdout 4h                              # the single holdout test -> results.json
```
