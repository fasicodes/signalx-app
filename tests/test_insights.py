"""
Pro insights - the ten things a professional checks before and after taking a signal.

Page /pro-insights (members) with these API routes:
  /api/insights/performance?market=&timeframe=   results of one stream, from the real closed signals
  /api/insights/live                             open signals: funding, liquidity, clusters, events
  /api/insights/size?balance=&risk=&entry=&stop= position size for a fixed risk

The ten checks
  1  Funding rate and open interest      live, crypto only (exchange data); open-interest change builds up over time
  2  Liquidity and spread                live order book; what a market order of 1k / 10k / 100k really costs, in R
  3  Signals that move together          how many independent bets the open signals really are
  4  Position size                       units, notional and margin for a fixed account risk
  5  How sure are the numbers            confidence range of win rate and average R, and how many signals are still needed
  6  Results by market condition         trend and volatility of the benchmark (BTC for crypto, EUR/USD for forex)
  7  Live record against the model test  is the real record in line with what the test promised
  8  Events calendar                     high-impact events in the next 48 hours (needs a calendar API key)
  9  Results by trading session          Asia / London / New York
  10 Losing streaks and drawdown         the worst runs to expect, from the stream's own win rate and results

The pure maths (wilson, mean_ci, effective_bets, walk_book, position_size, ...) has no database or network and is unit
tested (tests/test_insights.py). Everything that needs the exchange, candles or the calendar comes through hooks set
by init_insights(), and every live part fails soft: a missing source gives {"available": false, "reason": ...}.
"""
import math
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, redirect, render_template, request, session

import markets

insights_bp = Blueprint("insights", __name__)

_hooks = {"get_candles": None, "funding": None, "order_book": None, "calendar": None, "calendar_status": None}
_cache = {}
_lock = threading.Lock()
_oi_hist = {}            # symbol -> deque[(epoch, open interest)]

MIN_SIGNALS = 30         # below this the numbers are "too early to judge"
FEE_ONE_WAY_PCT = 0.05   # exchange taker fee per side, used for the live cost in R
BENCH = {"crypto": "BTC/USDT", "forex": "EUR/USD"}
SESSIONS = (("Asia (00-07 UTC)", 0, 7), ("London (07-12 UTC)", 7, 12), ("New York (12-21 UTC)", 12, 21),
            ("Late (21-24 UTC)", 21, 24))
CLOSED = ("TP1", "TP2", "TP3", "SL", "EXPIRED")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def _iso(d):
    return d.replace(microsecond=0).isoformat() + "Z" if isinstance(d, datetime) else d


def _parse(v):
    if isinstance(v, datetime):
        return v
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "").replace("T", " ")[:19])
    except ValueError:
        return None


def _r(x, n=3):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), n)


def cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
        if len(_cache) > 200:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:50]:
                _cache.pop(k, None)
    return val


# ---------------------------------------------------------------------------
# 5. how sure are the numbers
# ---------------------------------------------------------------------------
def wilson(wins, n, z=1.96):
    """95% range of a win rate (Wilson). -> (low, high) in percent, or (None, None) with no signals."""
    if n <= 0:
        return None, None
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, c - h) * 100, 1), round(min(1.0, c + h) * 100, 1)


def mean_ci(xs, z=1.96):
    """Average and its 95% range. -> (mean, low, high, sd); all None with fewer than 2 values."""
    a = np.asarray([x for x in xs if x is not None], dtype=float)
    n = len(a)
    if n == 0:
        return None, None, None, None
    m = float(a.mean())
    if n < 2:
        return m, None, None, None
    sd = float(a.std(ddof=1))
    h = z * sd / math.sqrt(n)
    return m, m - h, m + h, sd


def signals_needed(mean, sd, z=1.96):
    """How many signals are needed before the average result is sure to be above zero (95%), if the true average
    is what we see now. None when the average is not positive or the spread is unknown."""
    if mean is None or sd is None or mean <= 0:
        return None
    return int(math.ceil((z * sd / mean) ** 2))


def sure_summary(rs, wins):
    """The block for card 5: win rate and average R with their ranges, a plain verdict and the signals still needed."""
    n = len(rs)
    lo_w, hi_w = wilson(wins, n)
    m, lo, hi, sd = mean_ci(rs)
    if n < MIN_SIGNALS:
        verdict, text = "too_early", f"Only {n} closed signals. At least {MIN_SIGNALS} are needed before the numbers mean anything."
    elif lo is not None and lo > 0:
        verdict, text = "positive", "The average result is above zero even in the worst case of the range."
    elif hi is not None and hi < 0:
        verdict, text = "negative", "The average result is below zero even in the best case of the range."
    elif m is not None and m > 0:
        verdict, text = "likely_positive", "The average is positive, but the range still includes zero. More signals are needed to be sure."
    else:
        verdict, text = "unclear", "The average is not above zero and the range includes zero. Nothing can be said yet."
    need = signals_needed(m, sd)
    return {"n": n, "wins": wins, "win_rate": round(wins / n * 100, 1) if n else None, "win_low": lo_w, "win_high": hi_w,
            "avg_r": _r(m), "avg_low": _r(lo), "avg_high": _r(hi), "sd_r": _r(sd), "verdict": verdict, "text": text,
            "signals_needed": need, "signals_missing": max(0, need - n) if need is not None else None}


# ---------------------------------------------------------------------------
# 7. live record against the model test
# ---------------------------------------------------------------------------
def drift(live, expected):
    """Is the live record in line with the published test? `live` = sure_summary(); `expected` = {avg_net_r, win_rate, signals}."""
    if not expected or expected.get("avg_net_r") is None:
        return {"status": "no_test", "text": "This stream has no published test on unseen data to compare with."}
    exp = float(expected["avg_net_r"])
    base = {"expected_avg_r": _r(exp), "expected_win_rate": expected.get("win_rate"), "expected_signals": expected.get("signals")}
    if live["n"] < MIN_SIGNALS or live["avg_low"] is None:
        return dict(base, status="too_early", text=f"Only {live['n']} live signals so far. The comparison starts at {MIN_SIGNALS}.")
    if live["avg_high"] < exp:
        return dict(base, status="below", text="The live average is clearly below what the test promised. Treat the test numbers with caution.")
    if live["avg_low"] > exp:
        return dict(base, status="above", text="The live average is above the test. Good, but a short good run proves little.")
    return dict(base, status="in_line", text="The live average is within the range the test promised.")


# ---------------------------------------------------------------------------
# 9. sessions
# ---------------------------------------------------------------------------
def session_of(hour):
    for name, a, b in SESSIONS:
        if a <= hour < b:
            return name
    return SESSIONS[-1][0]


def by_session(trades):
    out = []
    for name, _a, _b in SESSIONS:
        sel = [t for t in trades if t["t"] is not None and session_of(t["t"].hour) == name]
        out.append(_group(name, sel))
    return out


def _group(label, sel):
    n = len(sel)
    wins = sum(1 for t in sel if t["win"])
    rs = [t["r"] for t in sel if t["r"] is not None]
    lo, hi = wilson(wins, n)
    return {"label": label, "n": n, "win_rate": round(wins / n * 100, 1) if n else None, "win_low": lo, "win_high": hi,
            "avg_r": _r(sum(rs) / len(rs)) if rs else None, "enough": n >= MIN_SIGNALS}


# ---------------------------------------------------------------------------
# 6. market condition
# ---------------------------------------------------------------------------
def regime_labels(df):
    """Trend and volatility label for every closed bar of the benchmark (4h candles).
    Trend: up / down when price is on that side of its 50-bar average and the average moved more than 1% in 12 bars,
    otherwise sideways. Volatility: wild when the bar range is above the 75th percentile of the last 180 bars."""
    d = df.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"])
    d = d.sort_values("timestamp").reset_index(drop=True)
    close = d["close"].astype(float)
    ema = close.ewm(span=50, adjust=False).mean()
    slope = ema / ema.shift(12) - 1
    trend = np.where((close > ema) & (slope > 0.01), "Uptrend", np.where((close < ema) & (slope < -0.01), "Downtrend", "Sideways"))
    rng = ((d["high"].astype(float) - d["low"].astype(float)) / close).rolling(14).mean()
    q75 = rng.rolling(180, min_periods=60).quantile(0.75)
    vol = np.where(rng > q75, "Wild", "Calm")
    out = pd.DataFrame({"ts": d["timestamp"], "trend": trend, "vol": vol})
    out.loc[slope.isna().values | q75.isna().values, ["trend", "vol"]] = None
    return out.dropna().reset_index(drop=True)


def attach_regimes(trades, labels, bar=timedelta(hours=4)):
    """Give every trade the label of the last bar that was already closed when the signal came (no look-ahead)."""
    if labels is None or not len(labels):
        return trades
    ts = labels["ts"].values.astype("datetime64[ns]")
    for t in trades:
        if t["t"] is None:
            continue
        i = np.searchsorted(ts, np.datetime64(t["t"] - bar), side="right") - 1
        if 0 <= i < len(labels) and np.datetime64(t["t"]) - ts[i] <= np.timedelta64(int(bar.total_seconds() * 3), "s"):
            t["trend"], t["vol"] = labels["trend"].iloc[i], labels["vol"].iloc[i]
    return trades


def by_regime(trades):
    out = {}
    for key, names in (("trend", ("Uptrend", "Sideways", "Downtrend")), ("vol", ("Calm", "Wild"))):
        out[key] = [_group(nm, [t for t in trades if t.get(key) == nm]) for nm in names]
    out["labelled"] = sum(1 for t in trades if t.get("trend"))
    return out


# ---------------------------------------------------------------------------
# 10. losing streaks and drawdown
# ---------------------------------------------------------------------------
def streaks(rs):
    """Longest losing run, the losing run going on now, and the worst drawdown in R, in the order they happened."""
    longest = run = 0
    peak = eq = dd = 0.0
    for r in rs:
        run = run + 1 if r <= 0 else 0
        longest = max(longest, run)
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"longest_losing": longest, "current_losing": run, "max_drawdown_r": round(dd, 2)}


def prob_run(p_loss, n, k):
    """Chance of at least one run of k losses in a row within n signals, when each loses with probability p_loss."""
    if k <= 0:
        return 1.0
    if p_loss <= 0 or n < k:
        return 0.0
    if p_loss >= 1:
        return 1.0
    state = np.zeros(k)          # state[j] = chance the current run of losses is j long and no run of k yet
    state[0] = 1.0
    hit = 0.0
    for _ in range(n):
        nxt = np.zeros(k)
        nxt[0] = state.sum() * (1 - p_loss)
        nxt[1:] = state[:-1] * p_loss
        hit += state[-1] * p_loss
        state = nxt
    return float(min(1.0, hit))


def drawdown_bootstrap(rs, n=100, sims=2000, seed=7):
    """Worst drawdown (R) over the next n signals if results are drawn at random from the ones seen so far.
    -> {"median": ..., "p95": ...} as positive numbers."""
    a = np.asarray(rs, dtype=float)
    if len(a) < 10:
        return None
    rng = np.random.default_rng(seed)
    draws = rng.choice(a, size=(sims, n), replace=True)
    eq = np.cumsum(draws, axis=1)
    peak = np.maximum.accumulate(np.maximum(eq, 0), axis=1)
    dd = (peak - eq).max(axis=1)
    return {"median": round(float(np.median(dd)), 2), "p95": round(float(np.percentile(dd, 95)), 2)}


def streak_block(rs):
    n = len(rs)
    if n < 10:
        return {"available": False, "reason": f"Only {n} closed signals. At least 10 are needed."}
    losses = sum(1 for r in rs if r <= 0)
    p = losses / n
    ks = [3, 5, 7, 10]
    out = dict(streaks(rs), available=True, n=n, loss_rate=round(p * 100, 1),
               chance=[{"run": k, "pct": round(prob_run(p, 100, k) * 100, 1)} for k in ks],
               expected_longest_100=next((k for k in range(1, 40) if prob_run(p, 100, k) < 0.5), 40) - 1)
    out["drawdown_100"] = drawdown_bootstrap(rs)
    return out


# ---------------------------------------------------------------------------
# 3. signals that move together
# ---------------------------------------------------------------------------
def effective_bets(corr, sides):
    """How many independent bets a set of open signals really is. corr = correlation matrix of the symbols' returns,
    sides = +1 (long) / -1 (short) in the same order. Two longs on coins that move together count as one bet; a long
    and a short on such coins offset each other. -> (n_eff between 1 and n, average pair correlation as seen by the trades)."""
    c = np.asarray(corr, dtype=float)
    n = len(sides)
    if n == 0:
        return 0.0, 0.0
    if n == 1:
        return 1.0, 0.0
    s = np.asarray(sides, dtype=float)
    adj = c * np.outer(s, s)
    tot = float(adj.sum())
    n_eff = n * n / tot if tot > 0 else float(n)
    avg = float((adj.sum() - n) / (n * (n - 1)))
    return float(min(float(n), max(1.0, n_eff))), avg


def cluster_block(rows_by_symbol, returns):
    """rows_by_symbol: list of (symbol, side) of the open signals; returns: DataFrame of returns, columns = symbols."""
    syms = [s for s, _ in rows_by_symbol]
    n = len(syms)
    longs = sum(1 for _, sd in rows_by_symbol if sd == "LONG")
    out = {"available": n > 0, "open": n, "long": longs, "short": n - longs}
    if n == 0:
        out["reason"] = "No open signals."
        return out
    if n == 1:
        out.update(n_eff=1.0, risk_multiplier=1.0, avg_corr=0.0, text="One open signal. Nothing to compare.")
        return out
    cols = [s for s in syms if returns is not None and s in returns.columns]
    if len(cols) < 2:
        same = max(longs, n - longs)
        out.update(n_eff=None, risk_multiplier=None, avg_corr=None,
                   text=f"{same} of {n} open signals point the same way. Price history was not available to measure how much they move together.")
        return out
    corr = returns[cols].corr().fillna(0.0).values
    sides = [1 if sd == "LONG" else -1 for s, sd in rows_by_symbol if s in cols]
    n_eff, avg = effective_bets(corr, sides)
    k = len(cols)
    mult = k / n_eff if n_eff else None
    if mult and mult >= 1.25:
        text = (f"{k} open signals behave like about {n_eff:.1f} independent bets. If they all went wrong together, "
                f"the loss would be about {mult:.1f} times what {k} separate trades would give.")
    else:
        text = f"{k} open signals are close to independent of each other. Their losses are unlikely to arrive together."
    out.update(n_eff=round(n_eff, 1), risk_multiplier=round(mult, 2) if mult else None, avg_corr=round(avg, 2), measured=k, text=text)
    return out


# ---------------------------------------------------------------------------
# 2. liquidity and 4. position size
# ---------------------------------------------------------------------------
def walk_book(levels, notional):
    """Fill `notional` (quote currency) against ordered levels [[price, amount], ...]. -> (average price, filled notional)."""
    left = float(notional)
    cost = qty = 0.0
    for lv in levels:
        p, a = float(lv[0]), float(lv[1])
        if p <= 0 or a <= 0:
            continue
        take = min(left, p * a)
        cost += take
        qty += take / p
        left -= take
        if left <= 1e-9:
            break
    return (cost / qty if qty else None), cost


def book_cost(side, bids, asks, notional):
    """Cost of a market order of `notional` against the book, in basis points of the mid price (half spread included).
    -> {"bps": ..., "filled_pct": ...} or None when the book is empty."""
    if not bids or not asks:
        return None
    mid = (float(bids[0][0]) + float(asks[0][0])) / 2
    levels = asks if side == "LONG" else bids
    avg, filled = walk_book(levels, notional)
    if avg is None or mid <= 0:
        return None
    bps = (avg - mid) / mid * 1e4 if side == "LONG" else (mid - avg) / mid * 1e4
    return {"bps": round(bps, 2), "filled_pct": round(filled / notional * 100, 1) if notional else 100.0}


def cost_in_r(entry, stop, bps_one_way, fee_pct=FEE_ONE_WAY_PCT):
    """Fees plus slippage for the way in and the way out, as a part of the risk (R)."""
    dist = abs(entry - stop) / entry if entry else 0
    if dist <= 0:
        return None
    return round((2 * fee_pct / 100 + 2 * bps_one_way / 1e4) / dist, 3)


def position_size(balance, risk_pct, entry, stop, fee_pct=FEE_ONE_WAY_PCT, leverage=None):
    """Units to trade so that hitting the stop (fees included) loses exactly risk_pct of the account."""
    for name, v in (("balance", balance), ("risk", risk_pct), ("entry", entry), ("stop", stop)):
        try:
            ok = v is not None and math.isfinite(float(v)) and float(v) > 0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            return {"ok": False, "error": f"Enter a number above zero for {name}."}
    balance, risk_pct, entry, stop = float(balance), float(risk_pct), float(entry), float(stop)
    if risk_pct > 10:
        return {"ok": False, "error": "Risk above 10% of the account per trade is not supported."}
    dist = abs(entry - stop)
    if dist == 0:
        return {"ok": False, "error": "Entry and stop cannot be the same price."}
    risk_amt = balance * risk_pct / 100
    per_unit_loss = dist + entry * 2 * fee_pct / 100
    units = risk_amt / per_unit_loss
    notional = units * entry
    out = {"ok": True, "side": "LONG" if stop < entry else "SHORT", "risk_amount": round(risk_amt, 2), "units": units,
           "notional": round(notional, 2), "stop_pct": round(dist / entry * 100, 3), "fees": round(notional * 2 * fee_pct / 100, 2),
           "gain_at_1r": round(risk_amt - notional * 2 * fee_pct / 100, 2), "warnings": []}
    lev = None if leverage in (None, "") else float(leverage)
    out["leverage_needed"] = round(notional / balance, 2)
    if lev and lev > 0:
        out["margin"] = round(notional / lev, 2)
    if notional > balance:
        out["warnings"].append(f"This size is {notional / balance:.1f} times your balance, so it needs leverage.")
    if risk_pct > 2:
        out["warnings"].append("Most professionals risk 0.5% to 2% of the account per trade.")
    return out


def suggest_risk(risk_pct, n_open, cap_pct=3.0):
    """Per-signal risk so that all open signals stopping together still lose at most cap_pct of the account."""
    if n_open <= 0:
        return {"per_signal": risk_pct, "cap": cap_pct, "worst_case": risk_pct}
    per = min(float(risk_pct), cap_pct / n_open)
    return {"per_signal": round(per, 2), "cap": cap_pct, "worst_case": round(per * n_open, 2)}


# ---------------------------------------------------------------------------
# 1. funding and open interest
# ---------------------------------------------------------------------------
def funding_state(rate_pct, side):
    """Crowding from the funding rate (percent per 8 hours) and how it sits with the signal's direction."""
    if rate_pct is None:
        return {"crowd": None, "text": "Funding rate not available."}
    if rate_pct >= 0.05:
        crowd, label = "longs_crowded", "Longs are crowded"
    elif rate_pct >= 0.02:
        crowd, label = "longs_leaning", "Leaning long"
    elif rate_pct <= -0.03:
        crowd, label = "shorts_crowded", "Shorts are crowded"
    elif rate_pct <= -0.01:
        crowd, label = "shorts_leaning", "Leaning short"
    else:
        crowd, label = "neutral", "Balanced"
    note = ""
    if side == "LONG":
        if crowd == "longs_crowded":
            note = "You would join a crowded trade: a drop can be sharp when longs close."
        elif crowd in ("shorts_crowded", "shorts_leaning"):
            note = "Shorts are crowded: a squeeze can help a long."
    elif side == "SHORT":
        if crowd == "shorts_crowded":
            note = "You would join a crowded trade: a bounce can be sharp when shorts close."
        elif crowd in ("longs_crowded", "longs_leaning"):
            note = "Longs are crowded: a flush can help a short."
    return {"crowd": crowd, "label": label, "text": note or label,
            "year_pct": round(rate_pct * 3 * 365, 1),
            "you_pay": (rate_pct > 0) == (side == "LONG") if side in ("LONG", "SHORT") and rate_pct != 0 else None}


def remember_oi(symbol, value, now=None):
    if value is None:
        return
    now = now or time.time()
    with _lock:
        d = _oi_hist.setdefault(symbol, deque(maxlen=400))
        if not d or now - d[-1][0] >= 600:
            d.append((now, float(value)))


def oi_change(symbol, hours=24, now=None):
    """Open-interest change over `hours` in percent, from what this server has seen; None until enough history exists."""
    now = now or time.time()
    with _lock:
        d = list(_oi_hist.get(symbol) or [])
    if len(d) < 2:
        return None
    old = [x for x in d if x[0] <= now - hours * 3600]
    ref = old[-1] if old else d[0]
    if now - ref[0] < 1800 or not ref[1]:
        return None
    return {"pct": round((d[-1][1] / ref[1] - 1) * 100, 2), "hours": round((d[-1][0] - ref[0]) / 3600, 1)}


# ---------------------------------------------------------------------------
# data: trades from the database
# ---------------------------------------------------------------------------
def _to_trade(row, source="live"):
    status = str(row.get("status") or "")
    if "bar_time" in row and row.get("bar_time") is not None:
        tf = row.get("timeframe") or "4h"
        t = _parse(row["bar_time"])
        t = t + timedelta(seconds=markets.TF_SEC.get(tf, 14400)) if t else None
    else:
        t = _parse(row.get("signal_at"))
    r = row.get("r_multiple")
    return {"t": t, "r": float(r) if r is not None else None, "win": status.startswith("TP"), "side": row.get("side"),
            "symbol": row.get("symbol"), "closed_at": _parse(row.get("closed_at") or row.get("exit_at")), "source": source}


def live_trades(market, tf):
    import signal_hub as hub
    with hub._Cursor() as cur:
        cur.execute("SELECT * FROM signal_events WHERE market=%s AND timeframe=%s", (market, tf))
        rows = cur.fetchall() or []
    out = [_to_trade(r) for r in rows if r.get("status") in CLOSED and r.get("r_multiple") is not None]
    out.sort(key=lambda t: (t["closed_at"] or t["t"] or datetime.min))
    return out


def backtest_trades():
    """The 4h crypto backtest of the track record (the only stream that has one in the database)."""
    try:
        import trackrecord as tr
        with tr._Cursor() as cur:
            cur.execute("SELECT * FROM track_signals WHERE source='BACKTEST' AND status<>'OPEN'")
            rows = cur.fetchall() or []
    except Exception:
        return []
    out = [_to_trade(r, "backtest") for r in rows if r.get("r_multiple") is not None]
    out.sort(key=lambda t: t["closed_at"] or t["t"] or datetime.min)
    return out


def _dataset(trades, with_regime=None):
    rs = [t["r"] for t in trades if t["r"] is not None]
    wins = sum(1 for t in trades if t["win"])
    out = {"sure": sure_summary(rs, wins), "sessions": by_session(trades), "streaks": streak_block(rs)}
    if with_regime is not None:
        out["regimes"] = by_regime(attach_regimes(trades, with_regime))
    return out


def _benchmark_labels(market):
    fn = _hooks.get("get_candles")
    if not fn:
        return None
    try:
        df = fn(BENCH[market], "4h", 1500)
        if df is None or len(df) < 120:
            return None
        return regime_labels(df)
    except Exception:
        return None


def performance(market, tf):
    import signal_hub as hub
    live = live_trades(market, tf)
    labels = cached(("labels", market), 900, lambda: _benchmark_labels(market))
    out = {"market": market, "timeframe": tf, "benchmark": BENCH[market], "regime_available": labels is not None,
           "live": _dataset(live, labels), "backtest": None}
    expected = None
    try:
        mt = hub.model_test(market, tf) or {}
        h = mt.get("holdout") or {}
        expected = {"avg_net_r": h.get("avg_net_r"), "win_rate": h.get("win_rate"), "signals": h.get("signals")}
    except Exception:
        pass
    out["drift"] = drift(out["live"]["sure"], expected)
    if market == "crypto" and tf == "4h":
        bt = backtest_trades()
        if bt:
            out["backtest"] = _dataset(bt, labels)
            out["backtest"]["note"] = "Simulation of the same rules on past candles (not live results)."
    out["model_test_sure"] = None
    try:
        if expected and expected.get("signals") and expected.get("win_rate") is not None:
            n = int(expected["signals"])
            lo, hi = wilson(round(n * float(expected["win_rate"]) / 100), n)
            out["model_test_sure"] = {"n": n, "win_rate": expected["win_rate"], "win_low": lo, "win_high": hi,
                                      "avg_r": expected.get("avg_net_r")}
    except Exception:
        pass
    out["generated_at"] = _iso(_utcnow())
    return out


# ---------------------------------------------------------------------------
# data: live view (open signals)
# ---------------------------------------------------------------------------
def _returns_frame(symbols):
    fn = _hooks.get("get_candles")
    if not fn:
        return None
    cols = {}
    for s in symbols:
        try:
            df = fn(s, "4h", 220)
            if df is None or len(df) < 60:
                continue
            ser = pd.Series(df["close"].astype(float).values, index=pd.to_datetime(df["timestamp"]))
            cols[s] = ser.pct_change()
        except Exception:
            continue
    if len(cols) < 2:
        return None
    frame = pd.DataFrame(cols).dropna().tail(180)
    # too few shared candles would make unrelated coins look independent: better to say "not measured"
    return frame if len(frame) >= 60 else None


def _book(symbol):
    fn = _hooks.get("order_book")
    if not fn:
        return None
    try:
        ob = fn(symbol) or {}
        return ob.get("bids") or [], ob.get("asks") or []
    except Exception:
        return None


def _liquidity(row):
    sym, side = row["symbol"], row["side"]
    entry, stop = float(row["entry"]), float(row["stop_loss"])
    if markets.is_forex(sym):
        cost_pips = markets.cost_pips(sym)
        sl_pips = markets.to_pips(sym, abs(entry - stop))
        return {"available": True, "kind": "forex", "round_trip_pips": cost_pips, "stop_pips": round(sl_pips, 1),
                "cost_r": round(cost_pips / sl_pips, 3) if sl_pips else None,
                "note": "Typical spread and slippage for this pair. Your broker's real spread can be wider around news and at the weekly open."}
    book = _book(sym)
    if not book or not book[0] or not book[1]:
        return {"available": False, "reason": "Order book not available right now."}
    bids, asks = book
    mid = (float(bids[0][0]) + float(asks[0][0])) / 2
    spread_bps = (float(asks[0][0]) - float(bids[0][0])) / mid * 1e4
    sizes = []
    for notional in (1_000, 10_000, 100_000):
        c = book_cost(side, bids, asks, notional)
        if c is None:
            continue
        sizes.append({"notional": notional, "bps": c["bps"], "filled_pct": c["filled_pct"],
                      "cost_r": cost_in_r(entry, stop, c["bps"]) if c["filled_pct"] >= 99 else None})
    worst = next((s for s in sizes if s["notional"] == 10_000), sizes[0] if sizes else None)
    flag = "ok"
    if worst and worst["cost_r"] is not None:
        flag = "costly" if worst["cost_r"] > 0.25 else "watch" if worst["cost_r"] > 0.12 else "ok"
    return {"available": True, "kind": "crypto", "spread_bps": round(spread_bps, 2), "sizes": sizes, "flag": flag,
            "levels": {"bids": len(bids), "asks": len(asks)}}


def _funding(row):
    sym = row["symbol"]
    if markets.is_forex(sym):
        return {"available": False, "reason": "Forex has no funding rate. Check your broker's swap (overnight) rate instead."}
    fn = _hooks.get("funding")
    if not fn:
        return {"available": False, "reason": "Funding data is not connected."}
    try:
        d = fn(sym) or {}
    except Exception:
        d = {}
    if not d.get("available"):
        return {"available": False, "reason": "The exchange did not return funding data."}
    remember_oi(sym, d.get("open_interest"))
    fr = d.get("funding_rate_pct")
    out = {"available": True, "funding_pct": fr, "open_interest": d.get("open_interest"), "oi_change": oi_change(sym),
           "next_funding_ts": d.get("next_funding_ts")}
    out.update(funding_state(fr, row["side"]))
    return out


def _events(rows, now):
    fn = _hooks.get("calendar")
    status = (_hooks.get("calendar_status") or (lambda: None))() or {}
    if not fn or status.get("configured") is False:
        return {"available": False, "reason": "No economic calendar is connected. Set FMP_API_KEY (or FINNHUB_API_KEY) to turn this on."}
    ccys = {"USD"}
    for r in rows:
        if markets.is_forex(r["symbol"]):
            ccys |= set(markets.currencies(r["symbol"]))
    try:
        ev = fn(now, now + timedelta(hours=48), None, ("high",)) or []
    except Exception:
        return {"available": False, "reason": "The calendar could not be read."}
    ev = [e for e in ev if (e.get("currency") or "").upper() in ccys]
    return {"available": True, "events": ev[:12], "currencies": sorted(ccys), "ok": status.get("ok", True)}


def _event_hits(row, events, now):
    """High-impact events that fall inside the signal's holding time."""
    if not events.get("available"):
        return []
    exp = _parse(row.get("expires_at")) or (now + timedelta(hours=48))
    ccy = set(markets.currencies(row["symbol"])) if markets.is_forex(row["symbol"]) else {"USD"}
    hits = []
    for e in events["events"]:
        t = _parse(e.get("time"))
        if t and now <= t <= exp and (e.get("currency") or "").upper() in ccy:
            hits.append({"title": e.get("title"), "time": e.get("time"), "currency": e.get("currency")})
    return hits


def live_view(now=None):
    import signal_hub as hub
    now = now or _utcnow()
    rows, _total = hub.list_signals(status="active", sort="newest", page=1, per_page=40)
    rows = [r for r in rows if hub._on(r)]
    crypto_syms = sorted({r["symbol"] for r in rows if not markets.is_forex(r["symbol"])})
    returns = _returns_frame(crypto_syms) if len(crypto_syms) > 1 else None
    events = _events(rows, now)
    items = []
    for r in rows:
        sj = hub.signal_json(r)
        item = {"id": r["id"], "symbol": r["symbol"], "market": sj["market"], "timeframe": sj["timeframe"], "side": r["side"],
                "grade": sj["grade"], "entry": sj["entry"], "stop_loss": sj["stop_loss"], "signal_at": sj["signal_at"],
                "funding": _funding(r), "liquidity": _liquidity(r), "event_hits": _event_hits(r, events, now)}
        items.append(item)
    crypto_rows = [(r["symbol"], r["side"]) for r in rows if not markets.is_forex(r["symbol"])]
    forex_rows = [(r["symbol"], r["side"]) for r in rows if markets.is_forex(r["symbol"])]
    cluster = {"crypto": cluster_block(crypto_rows, returns),
               "forex": cluster_block(forex_rows, _returns_frame_fx(forex_rows))}
    return {"signals": items, "cluster": cluster, "events": events, "generated_at": _iso(now)}


def _returns_frame_fx(rows):
    syms = sorted({s for s, _ in rows})
    return _returns_frame(syms) if len(syms) > 1 else None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
def _uid():
    return session.get("user_id")


def _need_login():
    return jsonify({"ok": False, "error": "Please log in to see the pro insights."}), 401


@insights_bp.route("/pro-insights", methods=["GET"])
def page():
    if not _uid():
        return redirect("/login")
    return render_template("pro-insights.html", active="insights")


@insights_bp.route("/api/insights/performance", methods=["GET"])
def api_performance():
    if not _uid():
        return _need_login()
    market = (request.args.get("market") or "crypto").strip()
    tf = (request.args.get("timeframe") or "4h").strip()
    if market not in markets.MARKETS or tf not in markets.TIMEFRAMES:
        return jsonify({"ok": False, "error": "Unknown market or timeframe."}), 400
    try:
        data = cached(("perf", market, tf), 60, lambda: performance(market, tf))
    except Exception as e:
        return jsonify({"ok": False, "error": f"Insights are not available right now: {str(e)[:120]}"}), 500
    return jsonify({"ok": True, **data})


@insights_bp.route("/api/insights/live", methods=["GET"])
def api_live():
    if not _uid():
        return _need_login()
    try:
        data = cached(("live",), 60, live_view)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Live insights are not available right now: {str(e)[:120]}"}), 500
    return jsonify({"ok": True, **data})


@insights_bp.route("/api/insights/size", methods=["GET"])
def api_size():
    if not _uid():
        return _need_login()

    def num(name):
        try:
            return float(request.args.get(name))
        except (TypeError, ValueError):
            return None
    out = position_size(num("balance"), num("risk"), num("entry"), num("stop"), leverage=num("leverage"))
    if out.get("ok"):
        n = request.args.get("open", type=int) or 0
        out["suggest"] = suggest_risk(num("risk"), max(0, min(n, 50)))
    return jsonify(out), (200 if out.get("ok") else 400)


def init_insights(app=None, *, get_candles=None, funding=None, order_book=None, calendar=None, calendar_status=None):
    _hooks.update(get_candles=get_candles, funding=funding, order_book=order_book, calendar=calendar,
                  calendar_status=calendar_status)
    if app is not None:
        app.register_blueprint(insights_bp)
