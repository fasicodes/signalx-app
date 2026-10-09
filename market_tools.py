"""
Live chart and Liquidity scanner as their own pages in the app (same theme and menu as the dashboard,
demo trading and the auto-trade bot), each with its own coin switcher. They no longer send people to the
Pro terminal and run its full 27-channel analysis first. The Pro terminal keeps its own tabs.

Pages (login required):
  GET /chart               Live chart          templates/chart.html             + static/chart-page.js
  GET /liquidity-scanner   Liquidity scanner   templates/liquidity-scanner.html + static/liquidity-page.js
  Both take ?coin=ETH/USDT and &tf=4h.

API:
  GET /api/market/tickers              last price, 24h change, high, low and volume for every crypto coin
                                       (one exchange call, cached 15 s and shared by everyone)
  GET /api/liquidity/map?coin=&tf=     everything the Liquidity scanner shows, in one call (login required),
                                       cached a few seconds per coin so many viewers cost one set of exchange calls

What the numbers are (the pages say this too):
  * Order book: the exchange's visible book (OKX, up to 400 price levels a side), a snapshot.
  * Large trades and taker flow: the exchange's latest public trades, so the last few minutes at most.
  * Stop pools: equal highs / equal lows on the candles that price has not traded through yet.
  * Liquidation zones: ESTIMATES from price, volume and common leverage levels. Not real liquidation data.
  * The scanner cards (magnet, strength, spoofing, traps, zones, funding, CVD, crash risk) come from the same
    functions as the Pro terminal's /liquidity (main.build_liquidity_payload).
"""
import math
import threading
import time

from flask import Blueprint, jsonify, redirect, render_template, request, session

market_bp = Blueprint("market_tools", __name__)

SCAN_TFS = ("15m", "1h", "4h", "1d")
CHART_TFS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
BOOK_LEVELS = 400          # OKX serves up to 400 levels a side
TRADES_LIMIT = 500         # OKX serves the latest 500 public trades
CANDLES = 300
MAP_TTL = 5.0
TICKERS_TTL = 15.0
BANDS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0)                 # +/- % from the mid price
LEVERAGES = ((10, 0.35), (25, 0.30), (50, 0.20), (100, 0.15))   # leverage, share of positions assumed
MAINT_MARGIN = 0.005       # maintenance margin used in the liquidation estimate (0.5%)

_hooks = {}
_cache = {}
_cache_lock = threading.Lock()
_key_locks = {}


class MapError(Exception):
    """A clean, user-facing reason the scanner cannot show this market right now."""


# ---------------------------------------------------------------------------------------------- helpers
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _sig(v, digits=10):
    """Round to significant digits, so cheap coins (0.000012) and forex (1.08345) keep their precision."""
    v = _num(v)
    return None if v is None else float(f"{v:.{digits}g}")


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2


def clean_side(levels):
    """[[price, amount, ...], ...] -> [(price, amount)] with positive, finite numbers only."""
    out = []
    for lvl in levels or []:
        if not isinstance(lvl, (list, tuple)) or len(lvl) < 2:
            continue
        p, q = _num(lvl[0]), _num(lvl[1])
        if p is not None and q is not None and p > 0 and q > 0:
            out.append((p, q))
    return out


def _epoch(ts):
    """pandas/numpy/datetime timestamp (naive = UTC) -> unix seconds."""
    try:
        import pandas as pd  # local import keeps the module light for tests
        return int(pd.Timestamp(ts).value // 1_000_000_000)
    except Exception:
        return None


def _arrays(df):
    t = [_epoch(x) for x in df["timestamp"].tolist()]
    o = [float(x) for x in df["open"].tolist()]
    h = [float(x) for x in df["high"].tolist()]
    low = [float(x) for x in df["low"].tolist()]
    c = [float(x) for x in df["close"].tolist()]
    v = [float(x) if x == x else 0.0 for x in df["volume"].tolist()]
    return t, o, h, low, c, v


def _atr(h, low, c, n=14):
    trs = []
    for i in range(1, len(c)):
        trs.append(max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1])))
    if not trs:
        return None
    tail = trs[-n:]
    return sum(tail) / len(tail)


# ---------------------------------------------------------------------------------------------- order book
def _cum_points(levels, max_points):
    """Cumulative USD from the best price outward, thinned to at most max_points (first and last kept)."""
    pts, cum = [], 0.0
    for p, q in levels:
        cum += p * q
        pts.append((p, cum))
    if len(pts) <= max_points:
        keep = pts
    else:
        step = (len(pts) - 1) / (max_points - 1)
        idx = sorted({int(round(i * step)) for i in range(max_points)})
        keep = [pts[i] for i in idx]
    return [[_sig(p), round(u, 2)] for p, u in keep]


def _ladder(bids, asks, mid, reach_pct, buckets):
    """Visible book grouped into equal price steps on each side of the mid price (for the liquidity map)."""
    if reach_pct <= 0 or buckets <= 0:
        return {"step": None, "rows": []}
    step = mid * reach_pct / 100 / buckets
    rows = {}
    for side, levels in (("bid", bids), ("ask", asks)):
        for p, q in levels:
            k = min(buckets - 1, int(abs(p - mid) / step)) if step > 0 else 0   # the farthest level sits on the edge
            key = (side, k)
            rows[key] = rows.get(key, 0.0) + p * q
    out = []
    for k in range(buckets - 1, -1, -1):
        out.append({"side": "ask", "lo": _sig(mid + k * step), "hi": _sig(mid + (k + 1) * step),
                    "usd": round(rows.get(("ask", k), 0.0), 2)})
    for k in range(buckets):
        out.append({"side": "bid", "lo": _sig(mid - (k + 1) * step), "hi": _sig(mid - k * step),
                    "usd": round(rows.get(("bid", k), 0.0), 2)})
    return {"step": _sig(step), "rows": out}


def book_stats(bids, asks, max_points=90, ladder_buckets=20):
    """Spread, how far the visible book reaches, bid/ask imbalance per band, the biggest walls,
    the cumulative depth curve and a bucketed ladder."""
    bids = sorted(clean_side(bids), key=lambda x: -x[0])
    asks = sorted(clean_side(asks), key=lambda x: x[0])
    if not bids or not asks:
        return {"available": False, "reason": "The order book is empty right now."}
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    spread = max(0.0, best_ask - best_bid)
    reach_b = max(0.0, (mid - bids[-1][0]) / mid * 100)
    reach_a = max(0.0, (asks[-1][0] - mid) / mid * 100)
    covered = min(reach_b, reach_a)

    bands = []
    for pct in BANDS:
        lo, hi = mid * (1 - pct / 100), mid * (1 + pct / 100)
        b = sum(p * q for p, q in bids if p >= lo)
        a = sum(p * q for p, q in asks if p <= hi)
        tot = b + a
        bands.append({"pct": pct, "bid_usd": round(b, 2), "ask_usd": round(a, 2),
                      "imbalance": round((b - a) / tot, 4) if tot > 0 else None,
                      "covered": pct <= covered + 1e-9})

    usd_b = [p * q for p, q in bids]
    usd_a = [p * q for p, q in asks]
    med = _median(usd_b + usd_a) or 1e-9
    walls = []
    for side, levels in (("BID", bids), ("ASK", asks)):
        for p, q in sorted(levels, key=lambda x: -(x[0] * x[1]))[:6]:
            usd = p * q
            walls.append({"side": side, "price": _sig(p), "amount": _sig(q, 8), "usd": round(usd, 2),
                          "distance_pct": round(abs(p - mid) / mid * 100, 4), "x_median": round(usd / med, 1),
                          "is_wall": usd / med >= 4})
    walls.sort(key=lambda w: -w["usd"])

    return {
        "available": True, "bid": _sig(best_bid), "ask": _sig(best_ask), "mid": _sig(mid), "spread": _sig(spread),
        "spread_bps": round(spread / mid * 1e4, 3), "levels": {"bids": len(bids), "asks": len(asks)},
        "reach_pct": {"bids": round(reach_b, 4), "asks": round(reach_a, 4)}, "covered_pct": round(covered, 4),
        "total_usd": {"bids": round(sum(usd_b), 2), "asks": round(sum(usd_a), 2)},
        "median_level_usd": round(med, 2), "bands": bands, "walls": walls,
        "depth": {"bids": _cum_points(bids, max_points), "asks": _cum_points(asks, max_points)},
        "ladder": _ladder(bids, asks, mid, max(reach_b, reach_a), ladder_buckets),
    }


# ---------------------------------------------------------------------------------------------- candles
def swing_levels(df, lookback=50, scan=6):
    """Range high/low of the last `lookback` candles and any liquidity sweep in the last `scan` candles:
    a wick beyond the previous high (or low) that closed back inside, i.e. stops taken, then rejected."""
    t, o, h, low, c, v = _arrays(df)
    n = len(c)
    if n < 5:
        return {"available": False, "reason": "Not enough candles."}
    w = min(lookback, n)
    hi, lo, price = max(h[-w:]), min(low[-w:]), c[-1]
    sweeps = []
    for j in range(max(1, n - scan), n):
        a = max(0, j - lookback)
        prev_h, prev_l = max(h[a:j]), min(low[a:j])
        ev = None
        if h[j] > prev_h and c[j] < prev_h:
            ev = {"side": "HIGH", "level": _sig(prev_h), "extreme": _sig(h[j])}
        elif low[j] < prev_l and c[j] > prev_l:
            ev = {"side": "LOW", "level": _sig(prev_l), "extreme": _sig(low[j])}
        if ev:
            ev.update({"time": t[j], "close": _sig(c[j]), "bars_ago": n - 1 - j, "forming": j == n - 1})
            sweeps.append(ev)
    sweeps.reverse()
    pos = (price - lo) / (hi - lo) * 100 if hi > lo else 50.0
    return {"available": True, "swing_high": _sig(hi), "swing_low": _sig(lo), "price": _sig(price),
            "to_high_pct": round((hi - price) / price * 100, 3), "to_low_pct": round((price - lo) / price * 100, 3),
            "position_pct": round(pos, 1), "lookback": w, "sweeps": sweeps}


def _swing_points(h, low, k):
    highs, lows = [], []
    for i in range(k, len(h) - k):
        if all(h[i] > h[j] for j in range(i - k, i)) and all(h[i] >= h[j] for j in range(i + 1, i + k + 1)):
            highs.append(i)
        if all(low[i] < low[j] for j in range(i - k, i)) and all(low[i] <= low[j] for j in range(i + 1, i + k + 1)):
            lows.append(i)
    return highs, lows


def stop_pools(df, price, k=2, lookback=240, max_pools=8):
    """Equal highs (buy stops above) and equal lows (sell stops below) that price has not traded through
    since the last touch. Tolerance: a quarter of the average candle range (ATR 14)."""
    d = df.tail(lookback)
    t, o, h, low, c, v = _arrays(d)
    n = len(c)
    if n < 2 * k + 3 or not price:
        return []
    atr = _atr(h, low, c) or price * 0.002
    tol = max(0.25 * atr, price * 0.0005)
    hi_idx, lo_idx = _swing_points(h, low, k)
    pools = []
    for kind, idx, arr in (("EQH", hi_idx, h), ("EQL", lo_idx, low)):
        clusters = []
        for i in idx:
            for cl in clusters:
                if abs(cl["mean"] - arr[i]) <= tol:
                    cl["idx"].append(i)
                    cl["mean"] = sum(arr[j] for j in cl["idx"]) / len(cl["idx"])
                    break
            else:
                clusters.append({"idx": [i], "mean": arr[i]})
        for cl in clusters:
            if len(cl["idx"]) < 2:
                continue
            last = max(cl["idx"])
            if kind == "EQH":
                level = max(arr[j] for j in cl["idx"])
                if last + 1 < n and max(h[last + 1:]) > level:
                    continue            # already swept: the stops above are gone
                if level <= price:
                    continue
            else:
                level = min(arr[j] for j in cl["idx"])
                if last + 1 < n and min(low[last + 1:]) < level:
                    continue
                if level >= price:
                    continue
            pools.append({"type": kind, "price": _sig(level), "touches": len(cl["idx"]),
                          "distance_pct": round((level - price) / price * 100, 3), "last_touch": t[last],
                          "first_touch": t[min(cl["idx"])]})
    pools.sort(key=lambda p: abs(p["distance_pct"]))
    return pools[:max_pools]


def liquidation_estimates(df, price, lookback=300, half_life=96, bucket_pct=0.25, span_pct=12.0):
    """ESTIMATED liquidation levels of leveraged positions opened on recent candles.

    Each candle's volume is treated as positions opened at its typical price, split across 10x/25x/50x/100x
    (LEVERAGES). A long at leverage L is liquidated near entry*(1 - 1/L + maintenance margin), a short near
    entry*(1 + 1/L - maintenance margin). Levels price already traded through are dropped (those positions
    are gone), and older candles count less (half-life `half_life` candles). This is a model, not exchange data."""
    d = df.tail(lookback)
    t, o, h, low, c, v = _arrays(d)
    n = len(c)
    if n < 20 or not price or sum(v) <= 0:
        return {"available": False, "reason": "Liquidation estimates need crypto volume data."}
    suf_max = [float("-inf")] * n
    suf_min = [float("inf")] * n
    run_max, run_min = float("-inf"), float("inf")
    for i in range(n - 1, -1, -1):
        suf_max[i], suf_min[i] = run_max, run_min       # extremes AFTER candle i
        run_max, run_min = max(run_max, h[i]), min(run_min, low[i])

    nb = int(round(2 * span_pct / bucket_pct))
    lo_edge = price * (1 - span_pct / 100)
    width = price * bucket_pct / 100
    long_w, short_w = [0.0] * nb, [0.0] * nb
    lev_long = {L: 0.0 for L, _ in LEVERAGES}
    lev_short = {L: 0.0 for L, _ in LEVERAGES}
    for i in range(n):
        tp = (h[i] + low[i] + c[i]) / 3
        usd = v[i] * tp
        if usd <= 0:
            continue
        age_w = 0.5 ** ((n - 1 - i) / half_life)
        for lev, share in LEVERAGES:
            w = usd * share * age_w
            long_liq = tp * (1 - 1 / lev + MAINT_MARGIN)
            short_liq = tp * (1 + 1 / lev - MAINT_MARGIN)
            if suf_min[i] > long_liq and long_liq < price:
                b = int((long_liq - lo_edge) // width)
                if 0 <= b < nb:
                    long_w[b] += w
                    lev_long[lev] += w
            if suf_max[i] < short_liq and short_liq > price:
                b = int((short_liq - lo_edge) // width)
                if 0 <= b < nb:
                    short_w[b] += w
                    lev_short[lev] += w

    top = max(max(long_w), max(short_w))
    if top <= 0:
        return {"available": False, "reason": "No open leveraged positions are estimated near the price right now."}

    def clusters(weights, side):
        found = []
        for b in range(nb):
            w = weights[b]
            if w <= 0:
                continue
            left = weights[b - 1] if b > 0 else 0
            right = weights[b + 1] if b + 1 < nb else 0
            if w >= left and w > right:
                lo_b, hi_b = max(0, b - 1), min(nb - 1, b + 1)
                tot = sum(weights[lo_b:hi_b + 1])
                center = sum((lo_edge + (j + 0.5) * width) * weights[j] for j in range(lo_b, hi_b + 1)) / tot
                found.append({"side": side, "price": _sig(center), "intensity": round(w / top, 3),
                              "distance_pct": round((center - price) / price * 100, 3)})
        found.sort(key=lambda x: -x["intensity"])
        return found[:4]

    heat = []
    for b in range(nb):
        if long_w[b] > 0 or short_w[b] > 0:
            heat.append({"lo": _sig(lo_edge + b * width), "hi": _sig(lo_edge + (b + 1) * width),
                         "long": round(long_w[b] / top, 4), "short": round(short_w[b] / top, 4)})
    tl, ts_ = sum(long_w), sum(short_w)
    return {
        "available": True, "model": "estimate", "bucket_pct": bucket_pct, "span_pct": span_pct,
        "below": clusters(long_w, "LONG"), "above": clusters(short_w, "SHORT"), "heat": heat,
        "long_share_pct": round(tl / (tl + ts_) * 100, 1) if tl + ts_ > 0 else None,
        "by_leverage": [{"leverage": lev, "long_liq_now": _sig(price * (1 - 1 / lev + MAINT_MARGIN)),
                         "short_liq_now": _sig(price * (1 + 1 / lev - MAINT_MARGIN)),
                         "long_weight": round(lev_long[lev] / (tl or 1), 3),
                         "short_weight": round(lev_short[lev] / (ts_ or 1), 3)} for lev, _ in LEVERAGES],
        "maintenance_margin_pct": MAINT_MARGIN * 100,
    }


# ---------------------------------------------------------------------------------------------- trades
def trade_flow(trades, max_large=12, points=40):
    """Taker buy vs sell volume, the biggest recent prints and a cumulative delta line, from public trades."""
    rows = []
    for tr in trades or []:
        if not isinstance(tr, dict):
            continue
        p, a, ts = _num(tr.get("price")), _num(tr.get("amount")), _num(tr.get("timestamp"))
        side = tr.get("side")
        cost = _num(tr.get("cost"))
        if cost is None and p is not None and a is not None:
            cost = p * a
        if p is None or a is None or ts is None or cost is None or cost <= 0 or side not in ("buy", "sell"):
            continue
        rows.append((int(ts), side, p, a, cost))
    if not rows:
        return {"available": False, "reason": "No recent trades from the exchange."}
    rows.sort(key=lambda r: r[0])
    buy = sum(r[4] for r in rows if r[1] == "buy")
    sell = sum(r[4] for r in rows if r[1] == "sell")
    costs = sorted(r[4] for r in rows)
    med = _median(costs) or 0.0
    q98 = costs[int(0.98 * (len(costs) - 1))]
    thr = max(q98, med * 8)
    big = [r for r in rows if r[4] >= thr]
    span = (rows[-1][0] - rows[0][0]) / 1000.0
    delta, cum = [], 0.0
    step = max(1, len(rows) // points)
    for i, r in enumerate(rows):
        cum += r[4] if r[1] == "buy" else -r[4]
        if i % step == 0 or i == len(rows) - 1:
            delta.append([r[0], round(cum, 2)])
    return {
        "available": True, "count": len(rows), "span_sec": round(span, 1),
        "buy_usd": round(buy, 2), "sell_usd": round(sell, 2),
        "buy_pct": round(buy / (buy + sell) * 100, 1) if buy + sell > 0 else None,
        "avg_usd": round((buy + sell) / len(rows), 2), "median_usd": round(med, 2), "threshold_usd": round(thr, 2),
        "large_buy_usd": round(sum(r[4] for r in big if r[1] == "buy"), 2),
        "large_sell_usd": round(sum(r[4] for r in big if r[1] == "sell"), 2),
        "large": [{"time": r[0], "side": r[1], "price": _sig(r[2]), "amount": _sig(r[3], 8), "usd": round(r[4], 2)}
                  for r in reversed(big[-max_large:])],
        "delta": delta,
    }


# ---------------------------------------------------------------------------------------------- the map
def _forex_scanner_note():
    return "Forex and gold have no public order book or trade tape, so only price-based levels are shown."


def build_map(coin, tf):
    ex = _hooks["exchange"]
    is_fx = _hooks["is_forex"](coin)
    try:
        df = _hooks["get_candles"](symbol=coin, timeframe=tf, limit=CANDLES)
    except Exception as e:
        print(f"[market_tools] candles for {coin} {tf} failed: {e}")
        raise MapError(f"Price data for {coin} is not available right now. Try again in a minute.")
    if df is None or len(df) < 30:
        raise MapError(f"There are not enough {tf} candles for {coin} yet.")
    last_close = float(df["close"].iloc[-1])
    out = {"ok": True, "coin": coin, "asset": "forex" if is_fx else "crypto", "timeframe": tf,
           "server_time": int(time.time()), "candle_time": _epoch(df["timestamp"].iloc[-1])}

    ob, trades = None, None
    if is_fx:
        out["book"] = {"available": False, "reason": _forex_scanner_note()}
        out["trades"] = {"available": False, "reason": _forex_scanner_note()}
    else:
        try:
            raw = ex.fetch_order_book(coin, limit=BOOK_LEVELS) or {}
            ob = {"bids": [list(x) for x in clean_side(raw.get("bids"))], "asks": [list(x) for x in clean_side(raw.get("asks"))]}
            out["book"] = book_stats(ob["bids"], ob["asks"])
        except Exception as e:
            out["book"] = {"available": False, "reason": f"The order book could not be read right now ({str(e)[:120]})."}
        try:
            trades = ex.fetch_trades(coin, limit=TRADES_LIMIT)
            out["trades"] = trade_flow(trades)
        except Exception as e:
            trades = None
            out["trades"] = {"available": False, "reason": f"Recent trades could not be read right now ({str(e)[:120]})."}

    price = out["book"]["mid"] if out["book"].get("available") else last_close
    out["price"] = _sig(price)
    swing = swing_levels(df.tail(120))
    out["levels"] = {"swing": swing, "pools": stop_pools(df, price)}
    out["liquidations"] = ({"available": False, "reason": "Liquidation estimates are for crypto only."} if is_fx
                           else liquidation_estimates(df, price))
    try:
        out["scanner"] = _hooks["liquidity_payload"](coin, tf, df=df.tail(120).reset_index(drop=True),
                                                     order_book=ob, trades=trades)
    except Exception as e:
        out["scanner"] = {"error": f"The scanner cards are not available right now ({str(e)[:140]})."}
    return out


def fetch_tickers():
    ex = _hooks["exchange"]
    coins = [c for c in _hooks["coins"] if not _hooks["is_forex"](c)]
    try:
        ex.load_markets()
    except Exception:
        pass
    markets = getattr(ex, "markets", None) or {}
    syms = [c for c in coins if not markets or c in markets]
    raw = ex.fetch_tickers(syms) or {}
    out = {}
    for s in syms:
        t = raw.get(s) or {}
        last = _num(t.get("last"))
        if last is None:
            continue
        pct = _num(t.get("percentage"))
        out[s] = {"last": _sig(last), "change_pct": round(pct, 3) if pct is not None else None,
                  "high": _sig(t.get("high")), "low": _sig(t.get("low")),
                  "quote_volume": round(_num(t.get("quoteVolume")) or 0.0, 2) or None}
    return out


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _cache_lock:
        lock = _key_locks.setdefault(key, threading.Lock())
    with lock:                                   # one computation per key, others wait and reuse it
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        with _cache_lock:
            _cache[key] = (time.time(), val)
            if len(_cache) > 300:
                for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:100]:
                    _cache.pop(k, None)
        return val


def _clean_coin(raw):
    coin = (raw or "").strip().upper()
    if coin in _hooks.get("coins", []) or coin in _hooks.get("forex_pairs", []):
        return coin
    return None


# ---------------------------------------------------------------------------------------------- routes
def _page(template, active):
    if not session.get("user_id"):
        return redirect("/login")
    return render_template(template, active=active, user_email=session.get("email", ""),
                           user_avatar=session.get("avatar_url"))


@market_bp.route("/chart", methods=["GET"])
def chart_page():
    return _page("chart.html", "chart")


@market_bp.route("/liquidity-scanner", methods=["GET"])
def liquidity_page():
    return _page("liquidity-scanner.html", "liquidity")


@market_bp.route("/api/market/tickers", methods=["GET"])
def api_tickers():
    try:
        data = _cached(("tickers",), TICKERS_TTL, fetch_tickers)
        return jsonify({"ok": True, "tickers": data, "server_time": int(time.time())})
    except Exception as e:
        stale = _cache.get(("tickers",))
        if stale:
            return jsonify({"ok": True, "tickers": stale[1], "stale": True, "server_time": int(time.time())})
        return jsonify({"ok": False, "error": f"Prices are not available right now ({str(e)[:120]})."}), 502


@market_bp.route("/api/liquidity/map", methods=["GET"])
def api_liquidity_map():
    if not session.get("user_id"):
        return jsonify({"ok": False, "error": "Login required."}), 401
    coin = _clean_coin(request.args.get("coin"))
    if not coin:
        return jsonify({"ok": False, "error": "Unknown coin or pair."}), 400
    tf = (request.args.get("tf") or "1h").strip()
    if tf not in SCAN_TFS:
        return jsonify({"ok": False, "error": f"Timeframe must be one of {', '.join(SCAN_TFS)}."}), 400
    try:
        data = _cached(("map", coin, tf), MAP_TTL, lambda: build_map(coin, tf))
    except MapError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"The scanner hit an error ({str(e)[:140]}). It retries automatically."}), 502
    hit = _cache.get(("map", coin, tf))
    resp = dict(data)
    resp["age_sec"] = round(max(0.0, time.time() - hit[0]), 1) if hit else 0.0
    return jsonify(resp)


def init_market_tools(app, *, exchange, get_candles, liquidity_payload, coins, forex_pairs, is_forex):
    _hooks.update(exchange=exchange, get_candles=get_candles, liquidity_payload=liquidity_payload,
                  coins=list(coins), forex_pairs=list(forex_pairs), is_forex=is_forex)
