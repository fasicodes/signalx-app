"""Settings of the human-style chart engine (chart_agents).

Every number here was fixed BEFORE the engine was tested (the way a trader sets up their chart once): nothing in this
file was tuned on the holdout period. The only things learned from data are the head trader's weights, and those are
learned on the validation years only (see research/human_lab/lab.py).
"""

VERSION = "1.1"   # 1.1: failed-breakout (trap) setup added for round 2

# Higher timeframes a trader looks at before the entry chart (top-down analysis).
HIGHER_TF = {"15m": ("1h", "4h"), "1h": ("4h", "1d"), "4h": ("1d", "1w")}
TF_SEC = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}
PANDAS_RULE = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D", "1w": "W-MON"}

# Candles of history the live engine replays before it judges the newest candle (the backtest replays everything;
# the engine only ever looks back ZONE_LOOKBACK / PATTERN_MAX_BARS candles, so both see the same chart).
LIVE_HISTORY = {"15m": 3000, "1h": 3000, "4h": 3000, "1d": 1000, "1w": 400}

# ----------------------------------------------------------------------------------------------- swings
PIVOT_K = 3               # a swing high/low needs 3 lower highs / higher lows on each side (confirmed 3 candles later)
MINOR_PIVOT_K = 2         # smaller swings, used for support/resistance touches and liquidity
ZIGZAG_ATR = 1.5          # a new swing must be at least 1.5 ATR away from the previous opposite swing

# ----------------------------------------------------------------------------------------------- zones
ZONE_LOOKBACK = 400       # candles of history used to draw support / resistance zones
ZONE_MERGE_ATR = 0.6      # swing prices closer than 0.6 ATR belong to the same zone
ZONE_PAD_ATR = 0.1        # a zone is drawn this much wider than its swing prices
ZONE_MIN_TOUCHES = 2      # a zone needs 2 touches ... or one swing with a reaction of at least
ZONE_MIN_REACTION_ATR = 2.5
ZONE_BREAK_ATR = 0.25     # a close this far beyond a zone breaks it
ZONE_POINT_REACTION_ATR = 1.0   # only swings price really turned from (ran at least 1 ATR away) make zones
MAX_ZONES = 10            # like a trader, keep only the strongest levels on the chart ...
ZONE_SPACING_ATR = 1.5    # ... with room between them: one zone per area (in the zone's own timeframe's ATR)
HTF_MAX_ZONES = 6         # strongest zones taken from each higher timeframe
HTF_LEVEL_REACTION_ATR = 1.5    # a wide higher-timeframe zone is split into the prices price turned hard from
HTF_ZONE_WEIGHT = {0: 1.0, 1: 1.6, 2: 2.2}     # entry chart, first and second higher timeframe

# ----------------------------------------------------------------------------------------------- patterns
PATTERN_TOL_ATR = 0.8     # two bottoms / tops / shoulders count as "equal" within 0.8 ATR
PATTERN_MIN_HEIGHT_ATR = 2.0
PATTERN_MAX_BARS = 160    # a pattern must form within this many candles
PATTERN_BREAKOUT_WAIT = 30   # the breakout must come within this many candles after the pattern is complete
FLAG_POLE_ATR = 3.0       # flag: a pole of at least 3 ATR ...
FLAG_POLE_MAX_BARS = 12   # ... made within 12 candles,
FLAG_MAX_RETRACE = 0.5    # a consolidation that gives back at most half of it ...
FLAG_MAX_BARS = 20        # ... and lasts at most 20 candles

# ----------------------------------------------------------------------------------------------- setups
RETEST_MIN_BARS = 2
RETEST_MAX_BARS = 30
SWEEP_MAX_ATR = 1.5       # a liquidity sweep pokes at most 1.5 ATR beyond the swing it takes out
SWEEP_LOOKBACK = 120
OB_MAX_BARS = 40          # an order block after a change of character stays valid this long

# ----------------------------------------------------------------------------------------------- risk
STOP_BUFFER_ATR = 0.2     # stop goes this far beyond the structure that invalidates the idea
MIN_STOP_ATR = 0.6
MAX_STOP_ATR = 4.0
MIN_NET_RR = 1.5          # reward / risk after costs
MAX_TARGET_R = 4.0
MAX_COST_R = 0.25         # costs may eat at most a quarter of the risk
NO_ROOM_TARGET_R = 2.0    # no resistance above (or support below): target 2R
TIME_LIMIT_BARS = {"15m": 96, "1h": 72, "4h": 42}   # 24 hours, 3 days, 7 days
# portfolio rules (the same in the backtest and on the site)
MAX_NEW_PER_BAR = 3
MAX_OPEN = 8
MAX_OPEN_SAME_SIDE = 5
DAILY_LOSS_LIMIT_R = -4.0
COOLDOWN_BARS_AFTER_LOSS = 4

# ----------------------------------------------------------------------------------------------- session / volatility
ATR_RANK_WINDOW = 500
ATR_RANK_LOW = 0.08       # dead market
ATR_RANK_HIGH = 0.97      # panic / news spike
SHOCK_RANGE_ATR = 4.0     # a candle this big is news: wait for the dust to settle
