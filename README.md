# Signals FM (SignalX)

Trading signals for crypto and forex on 15-minute, 1-hour and 4-hour candles, a news and economic
calendar section, demo trading, an auto-trade bot and an AI assistant. Flask + MySQL, deployed on Railway.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in at least SECRET_KEY and the DB_* values
python main.py              # http://localhost:5000
```

Production (Railway) uses the `Procfile`: `gunicorn main:app --worker-class gthread --workers 1 --threads 8`.
Every variable is described in `.env.example`; secrets live only in Railway Variables.

## What runs in the background

All background jobs start inside the web process and are safe with several workers or instances: each one
takes a MySQL named lock (`GET_LOCK`), so only one copy runs and another takes over if it stops.

| Job | Module | What it does | Switch |
|---|---|---|---|
| Signal scanner + tracker | `signal_hub.py` | After every 15m / 1h / 4h candle close: runs each stream, stores new signals, sends alerts; follows every active signal to its result (TP1/TP2/TP3, stop, expiry). | `SIGNAL_HUB`, `SIGNAL_STREAMS` |
| Crypto 4h engine | `signalboard.py` + `signal_engine.py` | Signal engine v3.1 board after each 4h close. | `SIGNAL_BOARD` |
| Track record | `trackrecord.py` | Live record + backtest of engine v3.1. | `TRACK_RECORD_ENGINE` |
| Strategy library | `strategies.py` + `strategy_library.py` | Once a day: backtests the 30 library strategies on the last year of 1h / 4h candles (20 crypto coins, 6 forex pairs) and stores the results for the Strategies page. | `STRATEGIES_ENGINE`, `STRATEGIES_REFRESH_HOURS` |
| News + calendar | `news.py` | News every 10 min, calendar every 30 min (de-duplicated). | `NEWS_JOB` |
| Auto-trade engine | `autotrade.py` | Hands every new site signal to the bots that want it, places orders, follows stops and targets. | `AUTOTRADE_ENGINE` |
| Demo trading engine | `papertrade.py` | The site's paper exchange. | `PAPER_ENGINE` |

## Signal streams

* `markets.py` - every symbol (62 crypto coins, 44 forex pairs), the forex signal list (7 majors, crosses, gold;
  `FOREX_SIGNAL_PAIRS`), pip sizes, spreads, the forex week (DST-aware) and sessions.
* `streams.py` - the six streams, the shared selection policy (one trade per symbol, cooldown, max signals per
  candle, forex sessions, weekend exit, news blackout) and the trade plan follower used by both the live site and the research.
* `features.py` - model inputs (the same code for research and live).
* `models/<market>_<timeframe>.json` - trained stream models with their walk-forward and holdout tests
  (crypto 4h uses `signal_model.json`, engine v3.1). A stream without a model file falls back to rules engine R1.
* `/signals` runs a selectable stream and now includes an all-stream comparison for crypto/forex 15m, 1h and 4h:
  actual closed-signal records are shown separately from model holdouts or historical rules replays. Experimental
  streams are labelled as such; live data appears as those signals close.
* Research code that trains and tests the models: see "Research" below.

Database: every signal is a row in `signal_events` (market, timeframe, grade, plan, status, result). The
migration only adds columns/indexes; signals from before it are crypto 4h signals and keep their data.

## Strategies page (`/strategies`)

A TradingView-style library with two kinds of strategies, each clearly labelled:

* **By Signals FM, Backtested** (`strategy_library.py`): 30 well-known public strategies (EMA cross, golden cross,
  MACD, Supertrend, Turtle breakout, Ichimoku, Parabolic SAR, ADX, RSI, RSI(2), Bollinger, Stochastic, Williams %R,
  CCI, VWAP, Keltner, squeeze, opening range, NR7, Chandelier, inside bar, engulfing, pin bar, volume breakout ...).
  Each has its written rules and the same test: entry at the close, stop = k x ATR(14) (1R), the strategy's own
  exit or time limit, one trade per symbol, results in R after costs (crypto 0.12% round trip, forex spread).
  `strategies.py` reruns every strategy once a day (`STRATEGIES_REFRESH_HOURS`, default 24) on the last
  `STRATEGIES_DAYS` (365) days of 1h candles, with 4h candles built from them, for `STRATEGIES_COINS` (20 coins)
  and `STRATEGIES_FOREX` (6 pairs), and stores the results in `strategy_results`. Until the first run finishes,
  `models/strategies_seed.json` is shown (made with `research/strategies_seed.py` from Binance candles; its period
  is shown on the page). The card shows the crypto 4h result; the page shows all four markets / timeframes, the
  equity curve, every symbol and the latest trades on a BTC chart. These are plain backtests of public rules, not
  the site's tested signal models; the page says so.
* **By a user, Not tested**: logged-in members publish a title, summary, description, rules, market, timeframe and
  an optional chart image (PNG / JPEG / WebP, 1.5 MB, stored in the database). Links, e-mail addresses and @handles
  are refused; 3 posts a day per member; 3 reports hide a post until an admin (`ADMIN_EMAILS`) restores it; the
  author or an admin can delete it. Likes work on both kinds.

## News and economic calendar

`news.py`: calendar from Financial Modeling Prep, Finnhub or Trading Economics; news from Finnhub, FMP and
central-bank RSS feeds (licensed APIs and RSS only, nothing scraped; third-party items keep only headline,
summary, source and link). Admins (`ADMIN_EMAILS`) write their own articles at `/admin/news`
(Markdown, draft / scheduled / published, cover image). High-impact events pause new forex signals for that
currency (`NEWS_BLACKOUT_BEFORE_MIN` / `NEWS_BLACKOUT_AFTER_MIN`).

## Auto-trade bot

`autotrade.py` + `fxbroker.py`. Accounts: Site demo (virtual money: crypto via the demo exchange, forex via a
virtual USD account), Binance demo / live (ccxt), OANDA demo / live (forex, v20 REST). Every new site
signal is handed to every bot account whose filters match (markets, timeframes, minimum grade, minimum
confidence, all symbols or a list), once per account, with the signal's own stop and targets, while the price
is still close to the entry. Risk % per trade, max open trades, daily loss limit, kill switch, retries with
backoff, an activity log, and "Trade this signal" on the Signals page. Keys are encrypted with
`AUTOTRADE_ENCRYPTION_KEY` and never sent to the browser; Binance keys with withdrawal permission are refused.

## AI assistant

`site_assistant.py` + `static/assistant-widget.js`: a chat button on every main page. Uses the Claude API
(`ANTHROPIC_API_KEY`, `ASSISTANT_MODEL`) server side with streaming, conversation history, live context (the
signal on screen, the newest signals, each stream's results, upcoming high-impact events, headlines), a timeout
with one retry, hourly limits per user / visitor, and built-in answers when the AI is off or busy. English only.

## Tests

```bash
for t in tests/test_*.py; do python "$t" || echo "FAILED: $t"; done
```

The tests use a SQLite stand-in for MySQL (`tests/dbshim.py`) and fake brokers / APIs: no network needed.
`tests/test_main_endpoints.py` loads the whole app (`main.py`), so it needs the packages from `requirements.txt`
installed (`pip install -r requirements.txt`); it checks the login rule and the coin / timeframe checks on the data
endpoints, the security headers and the analysis functions that once had bugs.

## Results: what is live and what is not (tested on data the models never saw)

All six streams run by default. Streams that have not passed the test are marked **untested** on the Signals page and in alerts, and live auto-trading accounts copy only the tested ones (demo accounts copy all). Set `SIGNAL_STREAMS=validated` to run only the tested streams.

Selection is **profit-first, not a fixed 70% accuracy target**: validation favours the trade plan with the best
average result after costs; the selected version is then judged once on a separate holdout. The 4h stream's
current plan uses a 1R target (the same distance as the stop), so a win rate below 70% can still be profitable.
Crypto costs 0.12 % round trip. Current engine metrics below are from the bundled model reports; the live
Track record page continuously replays the current engine rules on recent prices.

| Stream | On the site | Unseen-data result (current 1R exit plan) |
|---|---|---|
| Crypto 4h (engine v3.1) | **on** | 59.6 % wins, +0.236 R average / +190.2 R total, profit factor 1.66, 805 signals (about 2.1 a day); max drawdown -17.5 R |
| Crypto 1h, gated by the 4h model | **on** | 55.8 % wins, +0.099 R average / +166.6 R total, profit factor 1.23, 1,690 signals (about 4.5 a day); max drawdown -118.8 R |
| Crypto 1h alone | off | 68.1 % wins, -0.004 R under the older evaluation; not enabled because its 4h gate improves expectancy |
| Crypto 15m | on, **untested** (simple rules R1) | Rules replay 2023-2025: ~84 signals a day, 48.1 % reach TP1, **-0.139 R** per signal |
| Forex 15m / 1h / 4h and gold | on, **untested** (simple rules R1) | Best forex model holdout had 70.8 % TP1 wins but **-0.072 R** after costs. Rules replay 2017-2020: 15m ~9 a day **-0.218 R**, 1h ~2.9 a day **-0.112 R**, 4h ~1 a day **-0.082 R** |

What matters for your stated goal:

* The 4h test does **not** meet a 70% win-rate target. It was kept because it made more than it lost in a later unseen period: 805 signals, +190.2 R net after estimated costs, and a 1.66 profit factor. Its walk-forward test also averaged +0.135 R across 1,772 signals, though the longest-period maximum drawdown was -79.7 R.
* A win rate is not the same as profitability. With a 1R target and 1R stop, results after fees and slippage, total net R, and drawdown are more useful than an arbitrary accuracy threshold. The Track record page shows cumulative results and the recent backtest.
* A historical backtest cannot guarantee a positive future balance. Live performance can be negative, losses can occur in streaks, and costs or fills can differ. Use the demo account first and keep risk small.
* Crypto signals can arrive in correlated bursts. These are historical signal counts, not independent bets or promises. The 1h stream's larger drawdown especially calls for conservative sizing.
* The simple rules (R1) that the untested streams run lose money on average in replay; do not treat those experimental streams as profitable. See `models/research.json` and `research/README.md`.
* The Track record page keeps the live record. Treat backtest performance as evidence to monitor—not a guarantee.

## Research (how the stream models were trained and tested)

The research scripts are not part of the web app. Data: Binance spot 1-minute candles (public CC repositories
on GitHub, aggregated to 15m) for 53 coins, 2020-2026, and 15-minute forex/gold candles (2012-2022-03).
Protocol: features from `features.py`; label = first target (0.5 R) before the stop; walk-forward over the
validation years (6-month folds, each trained only on earlier data with a purge gap); threshold, grades and
calibration chosen on validation only; one final test on a later holdout period the model never saw.
Results are written into each model file (`test` = holdout, `walk_forward` = validation) and shown on the Signals page.
