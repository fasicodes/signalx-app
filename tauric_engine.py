"""Signal provider: Tauric Research "TradingAgents" (multi-agent LLM) decisions -> Signals FM signals.

Switch: SIGNAL_PROVIDER=tauric. With the switch off nothing in this file runs and the old engines are untouched.

What it does
------------
Once per symbol per UTC day (the first scan of the day), TradingAgents is asked for a BUY / SELL / HOLD decision.
BUY (or OVERWEIGHT) -> LONG, SELL (or UNDERWEIGHT) -> SHORT, anything else -> WAIT. The trade plan is the one the
site already tracks: entry = close of the last closed candle, stop = TAURIC_SL_ATR x ATR(14) (clamped 1.0-2.5),
targets at TARGETS_R (1R / 2R / 3R) with a part closed at each, time stop after the stream's max_bars.

Things to know
--------------
* TradingAgents decides on DAILY data and news. It is not a 15m / 1h model, so by default only 4h streams
  (TAURIC_TIMEFRAMES=4h) get signals, and at most one decision per symbol per day.
* Every decision is many LLM calls and costs money. Symbols are limited to TAURIC_SYMBOLS and the number of
  decisions per UTC day to TAURIC_MAX_RUNS_PER_DAY.
* These signals have NO backtest. They are never marked "tested", so live (real-money) bot accounts do not copy
  them (streams.validated() returns False while this provider is on); the Site demo / demo accounts can.
* The LLM gives no calibrated probability, so confidence is left empty and grade is always "C".
"""
import os
import re
import threading
import time
import traceback
from datetime import timedelta

import pandas as pd

import markets
import streams

ENGINE_TAG = "tauric"
DEFAULT_SYMBOLS = {"crypto": "BTC/USDT,ETH/USDT,SOL/USDT", "forex": "EUR/USD,GBP/USD,XAU/USD"}


def active():
    return (os.environ.get("SIGNAL_PROVIDER") or "").strip().lower() == ENGINE_TAG


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def allowed_timeframes():
    raw = (os.environ.get("TAURIC_TIMEFRAMES") or "4h").lower()
    return {x.strip() for x in raw.split(",") if x.strip()}


def symbols_for(market):
    raw = os.environ.get("TAURIC_SYMBOLS") or ""
    wanted = [s.strip().upper() for s in raw.split(",") if s.strip()] or \
        [s.strip() for s in DEFAULT_SYMBOLS.get(market, "").split(",") if s.strip()]
    pool = set(markets.CRYPTO_COINS) if market == "crypto" else set(markets.forex_signal_pairs())
    return [s for s in wanted if s in pool]


def yahoo_ticker(symbol):
    """The ticker TradingAgents (which reads Yahoo Finance) expects."""
    if markets.is_forex(symbol):
        base, quote = symbol.split("/")
        if base == "XAU":
            return "GC=F"
        if base == "XAG":
            return "SI=F"
        if base == "USD":
            return f"{quote}=X"
        return f"{base}{quote}=X"
    return symbol.split("/")[0] + "-USD"


_WORDS = re.compile(r"\b(BUY|OVERWEIGHT|SELL|UNDERWEIGHT|HOLD|NEUTRAL)\b", re.I)


def parse_decision(decision):
    """'BUY' / 'OVERWEIGHT' -> 'LONG'; 'SELL' / 'UNDERWEIGHT' -> 'SHORT'; else None. The first keyword wins."""
    if isinstance(decision, dict):
        decision = decision.get("decision") or decision.get("action") or ""
    m = _WORDS.search(str(decision or ""))
    if not m:
        return None
    w = m.group(1).upper()
    return "LONG" if w in ("BUY", "OVERWEIGHT") else "SHORT" if w in ("SELL", "UNDERWEIGHT") else None


# ---------------------------------------------------------------------------
# the decider: (yahoo_ticker, 'YYYY-MM-DD') -> text such as 'BUY'
# ---------------------------------------------------------------------------
_decider = None
_graph = None
_graph_lock = threading.Lock()


def set_decider(fn):
    """Tests (or another LLM stack) can plug in their own decider."""
    global _decider
    _decider = fn


def default_decider(ticker, date_str):
    """Runs the real TradingAgents graph. Needs `pip install tradingagents` (or the repo) and the LLM API key in the
    environment. Config names follow TradingAgents' DEFAULT_CONFIG; check them against the installed version."""
    global _graph
    with _graph_lock:
        if _graph is None:
            from tradingagents.default_config import DEFAULT_CONFIG
            from tradingagents.graph.trading_graph import TradingAgentsGraph
            cfg = dict(DEFAULT_CONFIG)
            for env, key in (("TAURIC_LLM_PROVIDER", "llm_provider"), ("TAURIC_DEEP_MODEL", "deep_think_llm"),
                             ("TAURIC_QUICK_MODEL", "quick_think_llm"), ("TAURIC_BACKEND_URL", "backend_url")):
                if os.environ.get(env):
                    cfg[key] = os.environ[env]
            cfg["max_debate_rounds"] = _env_int("TAURIC_DEBATE_ROUNDS", 1)
            cfg["max_risk_discuss_rounds"] = _env_int("TAURIC_DEBATE_ROUNDS", 1)
            _graph = TradingAgentsGraph(debug=False, config=cfg)
        graph = _graph
        _, decision = graph.propagate(ticker, date_str)
    return decision


def _decide(ticker, date_str):
    return (_decider or default_decider)(ticker, date_str)


# ---------------------------------------------------------------------------
# the engine (same interface as streams.StreamEngine: board(now, force) -> {symbol: result})
# ---------------------------------------------------------------------------
class TauricEngine:
    def __init__(self, market, tf, store, symbols=None, blocked=None):
        self.market, self.tf = market, tf
        self.key = streams.stream_key(market, tf)
        self.cfg = streams.STREAMS[self.key]
        self.store = store
        self.symbols = symbols_for(market)
        self.blocked = blocked
        self._shape = streams.StreamEngine(market, tf, store, self.symbols)   # reuses its result layout
        self._lock = threading.Lock()
        self._board = None
        self._decisions = {}        # (symbol, 'YYYY-MM-DD') -> 'LONG' | 'SHORT' | None
        self._runs = {}             # 'YYYY-MM-DD' -> decisions made that day (cost cap)
        self.errors = {}

    # one decision per symbol per UTC day, capped per day
    def _decision(self, symbol, day):
        key = (symbol, day)
        if key in self._decisions:
            return self._decisions[key], False
        if self._runs.get(day, 0) >= _env_int("TAURIC_MAX_RUNS_PER_DAY", 24):
            raise RuntimeError("daily Tauric run limit reached (TAURIC_MAX_RUNS_PER_DAY)")
        self._runs[day] = self._runs.get(day, 0) + 1
        side = parse_decision(_decide(yahoo_ticker(symbol), day))
        self._decisions[key] = side
        return side, True

    def _has_active(self, symbol):
        try:
            import signal_hub
            return bool(signal_hub.active_for(symbol))
        except Exception:
            return False

    def _plan(self):
        return streams.plan_for(self.market, self.tf)

    def board(self, now=None, force=False):
        now = now or streams._utcnow()
        bar = streams.last_closed_bar(self.tf, now)
        if not force and self._board and self._board[0] == bar:
            return self._board[1]
        with self._lock:
            if not force and self._board and self._board[0] == bar:
                return self._board[1]
            out = self._run(now, bar)
            self._board = (bar, out)
            return out

    def _run(self, now, bar):
        if self.tf not in allowed_timeframes():
            return {}
        out, errors = {}, {}
        plan = self._plan()
        tf_sec = markets.TF_SEC[self.tf]
        t0 = time.time()
        budget = _env_float("TAURIC_MAX_SECONDS", 900)
        sl_mult = min(max(_env_float("TAURIC_SL_ATR", 1.5), streams.SL_ATR_MIN), streams.SL_ATR_MAX)
        for sym in self.symbols:
            try:
                df = self.store.frame(sym, self.tf, now, bars=250)
                if len(df) < 40:
                    raise ValueError(f"Not enough {self.tf} history for {sym}.")
                d = streams.indicators(df)
                bar_time = pd.Timestamp(d["timestamp"].iloc[-1]).to_pydatetime()
                if bar_time < bar:
                    raise ValueError(f"The latest {self.tf} candle for {sym} is not available yet.")
                a = None
                if self._has_active(sym):
                    side = None
                elif time.time() - t0 > budget:
                    raise RuntimeError("time budget used up (TAURIC_MAX_SECONDS)")
                else:
                    side, fresh_run = self._decision(sym, bar_time.strftime("%Y-%m-%d"))
                    if side and not fresh_run:
                        side = None          # already decided earlier today: one signal per symbol per day
                if side:
                    entry, atr = float(d["close"].iloc[-1]), float(d["atr"].iloc[-1])
                    dist = sl_mult * atr
                    if not dist > 0:
                        raise ValueError("ATR is not available.")
                    sgn = 1 if side == "LONG" else -1
                    tps = [entry + sgn * k * dist for k in plan["targets_r"]]
                    signal_at = bar_time + timedelta(seconds=tf_sec)
                    expires = signal_at + timedelta(seconds=tf_sec * plan["max_bars"])
                    if self.market == "forex":
                        expires = min(expires, markets.forex_week_close(signal_at))
                    a = {"side": side, "entry": entry, "stop_loss": entry - sgn * dist, "stop_now": entry - sgn * dist,
                         "take_profit": tps[-1], "targets": tps, "targets_r": list(plan["targets_r"]),
                         "fractions": list(plan["fractions"]), "hits": 0, "atr": atr, "confidence": None,
                         "grade": "C", "setup": "tauric", "tier": None, "market": self.market, "timeframe": self.tf,
                         "bar_time": bar_time, "signal_at": signal_at, "expires_at": expires,
                         "rr": plan["targets_r"][-1],
                         "hold_minutes": streams.hold_minutes(self.tf, max(1, plan["max_bars"] // 3))}
                    if self.market == "forex":
                        a["sl_pips"] = round(markets.to_pips(sym, dist), 1)
                        a["tp_pips"] = [round(markets.to_pips(sym, k * dist), 1) for k in plan["targets_r"]]
                pl, ps = (100.0, 0.0) if a and a["side"] == "LONG" else (0.0, 100.0) if a else (0.0, 0.0)
                out[sym] = self._shape._result(d, a, None, [], pl, ps, 1.0, 1.0, ENGINE_TAG, plan)
                if a:
                    out[sym]["confidence"] = None
            except Exception as e:
                errors[sym] = str(e)[:160]
                if not isinstance(e, (ValueError, RuntimeError)):
                    print(f"[tauric] {sym} failed:\n{traceback.format_exc()}")
        for sym, err in errors.items():
            out[sym] = {"error": err, "market": self.market, "timeframe": self.tf}
        self.errors = errors
        return out

    def signal(self, symbol, now=None):
        res = self.board(now).get(symbol)
        if res is None:
            raise ValueError(f"{symbol} is not covered by the Tauric {self.market} {self.tf} stream.")
        return res
