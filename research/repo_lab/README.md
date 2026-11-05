# Repo strategy lab: do public GitHub signal bots and strategies make money?

Not part of the web app. It ports the entry rules of public strategy repositories, backtests every one of them
with the same honest rules and real costs, and only lets a stream go live when the combination passes
`streams.passed_test()` on data that was never used for any choice.

## Sources (47 entry rules)

| Prefix | Repository | Rules |
|---|---|---|
| `ft_` | github.com/freqtrade/freqtrade-strategies | 23 (incl. berlinguyinca/ and futures/) |
| `js_` | github.com/jesse-ai/example-strategies | 9 |
| `zz_` | github.com/zeta-zetra/code (forex) | 9 |
| `cs_` | github.com/CryptoSignal/Crypto-Signal | 6 |

Left out: rules that need data we do not have (fear & greed, other pairs, order book) or that are only hyperopt
output (GodStra, Heracles, mabStra, MultiMa, Diamond, UniversalMACD). Volume rules are skipped on forex (no volume).

## Data

* Crypto: 12 coins (BTC ETH SOL BNB XRP DOGE ADA AVAX LINK LTC DOT TRX), Binance spot 2021-01 to 2026-10
  (`../fetch_crypto.py COIN 2021`, then put the files in `work/data/crypto/`).
* Forex: EURUSD GBPUSD USDJPY USDCHF USDCAD AUDUSD NZDUSD XAUUSD, 1-minute HistData 2023-01 to 2026-10 from
  github.com/mailztoganesh-civil/forex-data (EST without DST, shifted to UTC). Clone it to `work/forex-data`
  or set `FOREX_DATA_DIR`.

## Protocol (fixed before the holdout was looked at)

* Entry at the signal candle's close, one trade per symbol at a time.
* 4 exit plans per rule: stop 1.5 or 2.5 x ATR(14), one target at 1R or 2R, time limit 32 / 36 / 30 candles
  (15m / 1h / 4h). Pessimistic fills (stop first when a candle touches both, gaps exit at the open).
* Costs from `markets.py`: crypto 0.30% per round trip + funding for longs, forex spread + commission + slippage.
* Validation: crypto 2021-03 to 2024-12, forex 2023-02 to 2024-12. A rule passes with >= 80 trades,
  avg >= +0.05R, profit factor >= 1.1 and a positive average in every validation year.
* Per stream the passing rules are combined and scored once on the holdout (2025-01 to 2026-10).

## Result (Nov 2026): nothing goes live

| Stream | Rules | Passed validation | Combined on holdout |
|---|---|---|---|
| crypto 15m | 47 | 0 (best -0.05R) | - |
| crypto 1h | 47 | 0 (best -0.02R) | - |
| crypto 4h | 47 | 1 (ft_AwesomeMacd) | 547 trades, -0.26R each, PF 0.67 |
| forex 15m | 42 | 0 (best -0.08R) | - |
| forex 1h | 42 | 0 | - |
| forex 4h | 42 | 8 | 1077 trades, -0.03R each, PF 0.95 |

Full numbers: `results.json`. The site's own crypto 4h engine (v3.1) remains the only stream that passes with
real costs (+0.21R per signal on its holdout).

## Part 2: the top GitHub trading-bot repositories (Nov 2026)

Ranked list: github.com/merovinh/best-of-algorithmic-trading (weekly) plus the "trading signals" GitHub search.
Every bot whose signal logic can run on candles was ported with its default settings (`strategies_bots.py`,
`itb.py` for the machine-learning bot) and run through the same protocol (`python bots_lab.py ...`).

| # | Repository | What it is | Tested? | Result |
|---|---|---|---|---|
| 1 | freqtrade/freqtrade (55K) | crypto bot framework | yes, bundled strategies (part 1) | none passes |
| 2 | virattt/ai-hedge-fund | LLM agents, stocks | no: paid LLM keys, no reproducible history | - |
| 3 | TauricResearch/TradingAgents | LLM agents | no: same; a 2026 one-year re-test found it behind buy-and-hold after costs | - |
| 4 | QuantConnect/Lean (22K) | backtest/live engine | no: engine without built-in signals | - |
| 5 | hummingbot/hummingbot (20K) | market making | no: not a signal bot | - |
| 6 | OpenByteInc/QuantDinger (13K) | AI trading OS (LLM) | no: LLM keys | - |
| 7 | StockSharp/StockSharp (11K) | trading platform | no: platform | - |
| 8 | askmike/gekko (10K, archived) | crypto bot | yes, 9 strategies | none passes |
| 9 | jesse-ai/jesse (8.7K) | crypto bot | yes, example strategies (part 1) | none passes |
| 10 | DeviaVir/zenbot (8.3K, archived) | crypto bot | yes, 13 strategies | none passes |
| 11 | je-suis-tm/quant-trading | strategy collection | yes, 3 (MACD osc., Dual Thrust, London Breakout) | none passes |
| 12 | edeng23/binance-trade-bot | coin-rotation bot | no: portfolio rotation, not per-symbol signals | - |
| 13 | Drakkar-Software/OctoBot (6.8K) | crypto bot | yes, 5 TA evaluators | none passes |
| 14 | Superalgos/Superalgos (5.7K) + Trading-Signals-Plugins | visual bot platform | no: strategies live in UI workspaces | - |
| 15 | CryptoSignal/Crypto-Signal (5.6K) | signal bot | yes (part 1) | none passes |
| 16 | ctubio/Krypto-trading-bot (3.7K) | high-frequency market making | no: not a signal bot | - |
| 17 | Haehnchen/crypto-trading-bot (3.5K) | crypto bot | yes, 3 strategies | none passes |
| 18 | Lumiwealth/lumibot (2.1K) | framework / AI agents | no: framework | - |
| 19 | 6551Team/opennews-mcp (2.4K) | news + AI ratings | no: paid token, no history | - |
| 20 | asavinov/intelligent-trading-bot (1.9K) | ML signal bot | yes, its 1h config (walk-forward) | none passes |
| 21 | bitcoinvsalts/node-binance-trader (1.2K) | bot fed by an external signal server | no: signals come from a paid server | - |
| 22 | stellar-deprecated/kelp (1.1K) | market making | no | - |
| 23 | bennycode/trading-signals (1K) | indicator library + bot | yes, 3 strategies | none passes |
| 24 | JulyIghor/QtBitcoinTrader (800) | manual trading client | no | - |
| 25 | Ekliptor/WolfBot (790) | crypto bot | yes, 2 strategies | none passes |

Combined result per stream (bots only, `results_bots.json`):

| Stream | Passed validation | Combined on holdout 2025-26 |
|---|---|---|
| crypto 15m / 1h / 4h | 0 / 0 / 0 | - |
| forex 15m | 0 | - |
| forex 1h | hc_cci, itb (ML) x2 | 650 trades, -0.19R each, PF 0.73 |
| forex 4h | ob_EMADivergence, zb_srsi_macd | 542 trades, -0.03R each, PF 0.95 |

The machine-learning bot (intelligent-trading-bot) was re-created with the bot's own features, labels and its
logistic-regression algorithm (TA-Lib / LightGBM / SVC could not be installed here), retrained every 30 days on
the previous 3 years only.

## Run

```bash
cd research/repo_lab
python prep.py crypto && python prep.py forex
python lab.py masks crypto 4h && python lab.py run crypto 4h     # per market / timeframe
python lab.py report                                             # work/results/summary.json
python bots_lab.py masks crypto 1h && python itb.py && python bots_lab.py run crypto 1h && python bots_lab.py report
```
