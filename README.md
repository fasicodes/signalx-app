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
* Research code that trains and tests the models: see "Research" below.

Database: every signal is a row in `signal_events` (market, timeframe, grade, plan, status, result). The
migration only adds columns/indexes; signals from before it are crypto 4h signals and keep their data.

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

Rule fixed before the results: a stream counts as *tested* only if the model version picked on the validation years
(2023-01 to 2025-09) also reaches **at least 70 % wins (TP1 or better), a positive average result after costs and
at least 50 signals** on a later holdout (2025-10-01 to 2026-10-11, crypto; 2021-01 to 2022-03, forex).
Crypto costs 0.12 % per round trip; the holdout was scored once per picked version.

| Stream | On the site | Unseen-data result |
|---|---|---|
| Crypto 4h (engine v3.1) | **on** | 73.6 % wins, +0.105 R per signal, 735 signals (about 2 a day) |
| Crypto 1h, with the 4h model agreeing | **on** | 72.6 % wins, +0.083 R per signal, 1,691 signals (about 4.5 a day) |
| Crypto 1h alone | off | 68.1 %, -0.004 R |
| Crypto 15m | on, **untested** (simple rules R1) | a 15m model was trained (stop 2 and 3 ATR, alone and with the 1h model agreeing) and none reached the validation target: closest 71.4 % wins but only +0.029 R, and 2025 fell to about 64 % and a loss. Rules replay 2023-2025: ~84 signals a day, 48.1 % reach TP1, **-0.139 R** per signal |
| Forex 15m / 1h / 4h and gold | on, **untested** (simple rules R1) | no model version reached the validation target, or lost money on unseen data (best: 1h, 70.8 % but -0.072 R). Rules replay 2017-2020: 15m ~9 a day **-0.218 R**, 1h ~2.9 a day **-0.112 R**, 4h ~1 a day **-0.082 R** |

Things the numbers say, plainly:

* The simple rules (R1) that the untested streams run lose money on average in every validation year and every grade (see `models/research.json`, `rules_replay`; reproduce with `research/rules_replay.py`). Loosening them (lower score, weaker higher-timeframe filter) gives more signals but the same average loss, so more signals on every timeframe needs a trained model that passes the test, not looser rules.

* A "win" is TP1 (half of the stop distance) or better; about 66 % of all trades in the data reach TP1 whatever
  the signal, so a high win rate alone is not skill. The measure that matters is the average result after
  costs (R = the risk of one trade). The two live models add about 6-7 points of win rate and +0.08 to +0.1 R per signal.
* More signals cost accuracy: the 1h model alone (13 signals a day) is close to the base rate and earns about
  nothing on unseen data. With both live streams expect roughly 6-7 signals a day across all coins, not 2-3 per coin.
* Crypto signals come in bursts: on busy days many coins fire together, so the unseen-data samples are smaller than
  the signal counts suggest. Treat the averages as indications, not promises.
* Not used because they did not help or did not hold: order-flow inputs, the Hawkes / Bayesian / conformal /
  Kelly prompt (the site's first engine: 49.5-51 % right on the next candle), forex models (see `research/README.md`).
* The numbers come from history; live results will differ. The Track record page keeps the live record.

## Research (how the stream models were trained and tested)

The research scripts are not part of the web app. Data: Binance spot 1-minute candles (public CC repositories
on GitHub, aggregated to 15m) for 53 coins, 2020-2026, and 15-minute forex/gold candles (2012-2022-03).
Protocol: features from `features.py`; label = first target (0.5 R) before the stop; walk-forward over the
validation years (6-month folds, each trained only on earlier data with a purge gap); threshold, grades and
calibration chosen on validation only; one final test on a later holdout period the model never saw.
Results are written into each model file (`test` = holdout, `walk_forward` = validation) and shown on the Signals page.
