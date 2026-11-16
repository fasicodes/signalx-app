# Signals FM (SignalX)

Crypto trading signals on 15-minute, 1-hour and 4-hour candles (engine 5), forex and gold charts, a news and economic
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
| Signal scanner + tracker | `signal_hub.py` | After every candle close of a switched-on stream (engine 5: crypto 15m and 1h): runs it, stores new signals, sends alerts; follows every active signal to its result (TP1/TP2/TP3, stop, expiry). | `SIGNAL_HUB`, `SIGNAL_STREAMS` |
| Crypto 4h engine | `signalboard.py` + `signal_engine.py` | The 4-hour engine (5.0) board after each 4h close. | `SIGNAL_BOARD` |
| Track record | `trackrecord.py` | Live record + backtest of the 4-hour engine. | `TRACK_RECORD_ENGINE` |
| Strategy library | `strategies.py` + `strategy_library.py` | Once a day: backtests the 30 library strategies on the last year of 1h / 4h candles (20 crypto coins, 6 forex pairs) and stores the results for the Strategies page. | `STRATEGIES_ENGINE`, `STRATEGIES_REFRESH_HOURS` |
| News + calendar | `news.py` | News every 10 min, calendar every 30 min (de-duplicated). | `NEWS_JOB` |
| Auto-trade engine | `autotrade.py` | Hands every new site signal to the bots that want it, places orders, follows stops and targets. | `AUTOTRADE_ENGINE` |
| Demo trading engine | `papertrade.py` | The site's paper exchange. | `PAPER_ENGINE` |

## Signal streams

* `markets.py` - every symbol (62 crypto coins, 44 forex pairs), the forex signal list (7 majors, crosses, gold;
  `FOREX_SIGNAL_PAIRS`), pip sizes, spreads, the forex week (DST-aware) and sessions.
* `streams.py` - the six streams, the shared selection policy (one trade per symbol, cooldown, max signals per
  candle, cost cap, forex sessions, weekend exit, news blackout) and the trade plan follower used by both the live
  site and the research. A stream runs only when its model passed its one test on unseen data (`passed_test`).
* `features.py` - model inputs (the same code for research and live).
* `signal_model.json` - the crypto 4h engine (trees of engine 3.1, three targets since engine 5.0).
* `models/crypto_1h.json` - the crypto 1h stream: the engine 5 4-hour model read at every hourly close
  (`StreamEngine._reading_board`).
* `models/crypto_15m.json` - the crypto 15m stream: the same model (same trees) read at every 15-minute close, only
  its top 0.5% of scores, 1 new signal per candle. No forex stream has a model file, so they stay off.
* A stream that reads 4-hour candles built from its own needs months of them at its first scan after a restart
  (15m: about 16,000 candles per coin); `signal_hub.warm` loads that history in slices after the due scans, so it
  never delays the other streams, and the stream gives no signals until it is loaded (the Signals page shows the
  progress). A copy of each coin's history is kept in the database (`candle_cache`, about 50 MB for every coin of
  the 15m and 1h streams, rewritten once a day), so after a restart the history is read from there in seconds and
  only the candles since the copy come from the exchange.
* `models/research.json` - what was tested for every stream and why it is on or off (shown on the Signals page).
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

## Premium chart workspace (`/premium-chart`, free for members right now)

Opened by the **Premium chart Access** button (FREE NOW label) on the Live chart, the Home chart card and the side
menu. `premium_chart.py` + `static/premium-chart.js` (pure parts in `static/premium-chart-core.js`, tested with
Node in `tests/test_premium_chart_core.js`):

* up to 16 charts per tab, each with its own coin, interval and chart type; crosshair / scroll / coin / interval sync;
* one shared price feed (`/api/premium/prices`, one exchange call for every coin) keeps every chart live, up to
  200 charts across a member's open tabs (one tab polls, the others listen through a BroadcastChannel);
* custom intervals from 1 minute to 12 months (`/api/premium/bars` builds them from the largest exchange interval
  that divides them; intraday buckets restart at 00:00 UTC), second intervals (predefined and custom) and tick bars
  built in the browser from the exchange's public trades (`/api/premium/trades`, crypto only);
* range bars, Renko, Kagi, Line break and Point & figure on any interval, bar replay (one or all charts), spread
  charts (A / B, A - B, A + B, A × B) and CSV export;
* a bond screener (`bonds.py`): end-of-day government bond yields from the U.S. Treasury, the ECB, the Bank of
  Canada, Japan's Ministry of Finance and the Reserve Bank of Australia, cached for 6 hours.

## News and economic calendar

`news.py`: calendar from the first of Financial Modeling Prep, Finnhub and Trading Economics (in that order, each
with its key) that answers with events. Each needs a plan that includes the economic calendar (FMP's and Finnhub's
free plans do not): `/admin/news` shows why a source gave nothing, without the key, and the News page says the
calendar is not available instead of waiting for it. News from Finnhub, FMP and central-bank RSS feeds (licensed
APIs and RSS only, nothing scraped; third-party items keep only headline, summary, source and link). Admins (`ADMIN_EMAILS`) write their own articles at `/admin/news`
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

## Email (sign-up links, password resets, contact form, alerts)

`mailer.py`. Railway's Free, Trial and Hobby plans block outbound SMTP, so Gmail SMTP cannot send from there and
reset links never arrive. These ways send over HTTPS instead (the first one set up is used):

- **Connect Gmail** (free, no new account): the site sends from the owner's own Gmail through the Gmail API. Once, in
  Google Cloud, in the project of the Google login (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`): APIs & Services →
  Library → **Gmail API → Enable**, and keep the OAuth consent screen **In production** (in "Testing" Google ends the
  permission after 7 days). Then on `/admin/news` press **Connect Gmail**, pick the Gmail, press Advanced → Go to …
  if Google says the app is not verified, and tick "Send email on your behalf". It comes back through the Google
  login's return address (`/api/auth/google/callback`), so nothing new is registered with Google. The permission
  (refresh token) is stored encrypted with `SECRET_KEY` in the MySQL table `mail_settings`; **Disconnect Gmail**
  removes it and cancels it at Google. A Gmail account sends up to about 500 emails a day.
- **Brevo** (`BREVO_API_KEY`, `MAIL_FROM`): best with a domain of your own (Brevo rewrites a gmail.com sender
  address). Turn off "Authorised IPs" blocking under Security, since Railway's outgoing IP changes.
- **Resend** (`RESEND_API_KEY`, `MAIL_FROM` = `Signals FM <no-reply@yourdomain>`): needs a domain verified in Resend.
- **Gmail SMTP** (`MAIL_USERNAME` + a 16-character Gmail App Password in `MAIL_PASSWORD`): only on Railway Pro or locally.

Check it on `/admin/news` (admins): the "Email delivery" box shows the service in use, the connected Gmail, the last
email's result or error, and sends a test email to your own address. Failures are also printed to the Railway logs
as `[mailer] ERROR`.

## Tests

```bash
for t in tests/test_*.py; do python "$t" || echo "FAILED: $t"; done
```

The tests use a SQLite stand-in for MySQL (`tests/dbshim.py`) and fake brokers / APIs: no network needed. GitHub
runs every test file on each push and pull request (`.github/workflows/tests.yml`).
`tests/test_main_endpoints.py` loads the whole app (`main.py`), so it needs the packages from `requirements.txt`
installed (`pip install -r requirements.txt`); it checks the login rule and the coin / timeframe checks on the data
endpoints, the security headers and the analysis functions that once had bugs.

## Results: what is live and what is not (engine 5, Nov 2026)

Only streams whose model passed its one test on data it never saw run on the site (`streams.passed_test`: at least 50
signals, +0.05R per signal after costs, profit factor 1.1, drawdown at most half the total result). `SIGNAL_STREAMS` can
only narrow that list down. Test period: 2025-10-01 to 2026-11-08, 94 coins, 0.30% per round trip plus funding for
longs, entries one minute after the candle close.

| Stream | On the site | Plan | Unseen-data result |
|---|---|---|---|
| Crypto 4h | **on** | stop 2 ATR, targets 0.5R / 1R / 2R (a third at each) | 852 signals (2.1 a day), **75.5 %** reach TP1, +0.12R per signal, PF 1.48 |
| Crypto 1h | **on** | the 4h model read every hour; one target at 0.5R | 781 signals (1.9 a day, in bursts), **82.5 %** reach the target, +0.19R per signal, PF 2.03 |
| Crypto 15m | **on** | the 4h model read every 15 minutes, its top 0.5% only; one target at 0.5R | 199 signals (0.5 a day, in bursts), **82.9 %** reach the target, +0.21R per signal, PF 2.32; about 6 in 10 are trades the 1h stream gives too. (A separate 15m model failed first: 69.4 %, +0.01R.) |
| Forex / gold 15m, 1h, 4h | off | - | no setting reached the validation target (best about 70 %, +0.03R); the unseen years were not opened |

Plainly:

* A win is the first target (half the stop distance). A random entry reaches it about 2 times in 3, and one full
  loss costs as much as two first-target wins, so the average result after costs is the measure that matters.
* The 1h and 15m streams have no filler signal: they can be quiet for days and then give many signals in one
  market move. A daily filler was tested and lost money on the validation years.
* The 15m stream is the second 15m variant scored on the unseen period, and its selection was chosen on the
  validation years after the setting written down first gave 11.5 signals a day (research/v5/rolling15.py says how).
* With the single 1R target of engine 3.2 the same 4h signals won 59.4 % (+0.19R per signal): engine 5 trades some
  profit per signal for fewer stop-losses.
* The numbers come from history; live results will differ. The Signals page and the Track record keep the live record.

## Research (how the stream models were trained and tested)

Engine 5: `research/v5` (`fetch.py` data, `lab5.py` models and the pre-registered selection, `rolling.py` the hourly
reading of the 4h model, `rolling15.py` its 15-minute reading, `engine4h.py` the 4h plan re-test, `live_check.py` live engine == research, `summary.py`
writes `models/research.json`). The text below describes the earlier research.

The research scripts are not part of the web app. Data: Binance spot 1-minute candles (public CC repositories
on GitHub, aggregated to 15m) for 53 coins, 2020-2026, and 15-minute forex/gold candles (2012-2022-03).
Protocol: features from `features.py`; label = first target (0.5 R) before the stop; walk-forward over the
validation years (6-month folds, each trained only on earlier data with a purge gap); threshold, grades and
calibration chosen on validation only; one final test on a later holdout period the model never saw.
Results are written into each model file (`test` = holdout, `walk_forward` = validation) and shown on the Signals page.
