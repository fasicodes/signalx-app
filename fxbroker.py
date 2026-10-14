"""
Forex execution for the auto-trade bot (autotrade.py).

* FxPaperClient - forex on the Site demo account: a virtual USD account
  (FOREX_PAPER_BALANCE, default 10,000 USD) inside the bot's own position
  table. Fills at the live price plus half the pair's typical spread.
* OandaClient   - OANDA v20 REST API (practice = demo account, or live).
  The token and account id are stored encrypted by autotrade.py; OANDA's
  API has no withdrawal endpoint. Every trade gets its stop-loss attached
  on OANDA itself, so the stop holds even if this server is down.

Both expose the same methods as autotrade.BinanceClient, so the bot's
engine, sizing, partial targets, monitor and logs work unchanged.
Profit and loss of a pair is in its quote currency; fx_rate() converts it
to USD (the account currency) at the time of entry.
"""
import os
import threading
import time
import uuid

import markets

FX_MAX_LEVERAGE = 20          # notional cap: 20 x free balance (forex brokers allow 30:1 or more)
OANDA_HOSTS = {"practice": "https://api-fxpractice.oanda.com/v3", "live": "https://api-fxtrade.oanda.com/v3"}


def instrument(symbol):
    return symbol.upper().replace("/", "_")


def fx_rate(symbol, price_fn):
    """USD value of 1 unit of the pair's quote currency (price_fn(pair) -> mid price)."""
    base, quote = markets.currencies(symbol)
    if quote == "USD":
        return 1.0
    if base == "USD":
        p = price_fn(symbol)
        return 1.0 / p if p else None
    direct = f"{quote}/USD"
    inverse = f"USD/{quote}"
    if direct in markets.FOREX_PAIRS:
        p = price_fn(direct)
        return float(p) if p else None
    if inverse in markets.FOREX_PAIRS:
        p = price_fn(inverse)
        return 1.0 / p if p else None
    return None


class FxPaperClient:
    """Virtual forex account for the Site demo bot. Positions live in autotrade_positions (market_type 'forex')."""

    is_paper = False          # not papertrade.py: the generic close/monitor paths apply
    is_fx = True

    def __init__(self, user_id, price_fn, cursor_factory):
        self.user_id = int(user_id)
        self.price_fn = price_fn
        self._cursor = cursor_factory
        self.mode = "paper"
        self.market_type = "forex"

    def has_symbol(self, symbol):
        return markets.is_forex(symbol)

    def market_rules(self, symbol):
        return {"min_amount": 1.0, "min_cost": 0.0, "taker": 0.0}

    def amount_to_precision(self, symbol, amount):
        return float(int(max(0.0, float(amount))))

    def price_to_precision(self, symbol, price):
        return round(float(price), markets.price_decimals(symbol))

    def price(self, symbol):
        p = self.price_fn(symbol)
        if p is None:
            raise RuntimeError(f"Live price for {symbol} is not available right now.")
        return float(p)

    def prices(self, symbols):
        out = {}
        for s in symbols:
            try:
                out[s] = self.price(s)
            except Exception:
                pass
        return out

    def balance_usdt(self):
        start = float(os.environ.get("FOREX_PAPER_BALANCE", "10000") or 10000)
        with self._cursor() as cur:
            cur.execute("""SELECT COALESCE(SUM(pnl_usdt),0) AS s FROM autotrade_positions WHERE user_id=%s AND mode='paper'
                           AND market_type='forex' AND status='CLOSED'""", (self.user_id,))
            realized = float((cur.fetchone() or {}).get("s") or 0)
            cur.execute("""SELECT COALESCE(SUM(notional_usdt),0) AS n, COALESCE(SUM(unrealized_pnl),0) AS u,
                           COALESCE(SUM(realized_usdt),0) AS r FROM autotrade_positions WHERE user_id=%s AND mode='paper'
                           AND market_type='forex' AND status IN ('OPEN','CLOSING')""", (self.user_id,))
            row = cur.fetchone() or {}
        equity = start + realized + float(row.get("r") or 0) + float(row.get("u") or 0)
        free = max(0.0, equity - float(row.get("n") or 0) / FX_MAX_LEVERAGE)
        return free, equity

    def prepare_futures(self, symbol, leverage):
        pass

    def market_order(self, symbol, side, amount, reduce_only=False):
        mid = self.price(symbol)
        half = markets.cost_price(symbol) / 2
        fill = mid + half if side == "buy" else mid - half
        return {"id": "fxp-" + uuid.uuid4().hex[:12], "average": round(fill, markets.price_decimals(symbol) + 1),
                "filled": float(amount)}

    def set_stop(self, trade_id, symbol, stop_price):
        return trade_id       # the bot watches the stop itself (it runs every few seconds)

    def backup_stop(self, symbol, close_side, amount, stop_price):
        return None

    def futures_positions(self, symbols):
        return None           # positions exist only in the bot's table

    def order_average(self, order_id, symbol):
        return None

    def cancel_all(self, symbol):
        pass

    def api_restrictions(self):
        return None


class OandaClient:
    """OANDA v20 REST. env 'practice' (demo) or 'live'."""

    is_paper = False
    is_fx = True

    def __init__(self, token, account_id, env="practice", session=None, timeout=12):
        if env not in OANDA_HOSTS:
            raise ValueError("OANDA environment must be practice or live.")
        self.token = token
        self.account_id = account_id
        self.base = OANDA_HOSTS[env]
        self.env = env
        self.timeout = timeout
        if session is None:
            import requests
            session = requests.Session()
        self.s = session
        self._lock = threading.Lock()

    # ---- http
    def _req(self, method, path, body=None, params=None, tries=3):
        url = f"{self.base}/accounts/{self.account_id}{path}"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                   "Accept-Datetime-Format": "RFC3339"}
        delay = 0.5
        for k in range(tries):
            try:
                r = self.s.request(method, url, json=body, params=params, headers=headers, timeout=self.timeout)
            except Exception as e:          # network error: retry with backoff
                if k == tries - 1:
                    raise RuntimeError(f"OANDA is not reachable: {type(e).__name__}")
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code in (429, 502, 503, 504) and k < tries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            try:
                data = r.json()
            except ValueError:
                data = {}
            if r.status_code >= 400:
                msg = data.get("errorMessage") or data.get("message") or f"HTTP {r.status_code}"
                if r.status_code == 401:
                    msg = "OANDA rejected the token (check that it is valid and belongs to this account)."
                raise RuntimeError(f"OANDA: {msg}")
            return data
        raise RuntimeError("OANDA: no answer")

    # ---- interface
    def has_symbol(self, symbol):
        return markets.is_forex(symbol)

    def market_rules(self, symbol):
        return {"min_amount": 1.0, "min_cost": 0.0, "taker": 0.0}

    def amount_to_precision(self, symbol, amount):
        return float(int(max(0.0, float(amount))))

    def price_to_precision(self, symbol, price):
        return round(float(price), markets.price_decimals(symbol))

    def api_restrictions(self):
        return {"enableWithdrawals": False, "note": "The OANDA v20 API cannot withdraw funds."}

    def balance_usdt(self):
        a = self._req("GET", "/summary").get("account") or {}
        return float(a.get("marginAvailable") or 0), float(a.get("NAV") or a.get("balance") or 0)

    def prices(self, symbols):
        if not symbols:
            return {}
        data = self._req("GET", "/pricing", params={"instruments": ",".join(instrument(s) for s in symbols)})
        out = {}
        for p in data.get("prices") or []:
            try:
                bid = float(p["bids"][0]["price"])
                ask = float(p["asks"][0]["price"])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            out[p["instrument"].replace("_", "/")] = (bid + ask) / 2
        return out

    def price(self, symbol):
        p = self.prices([symbol]).get(symbol.upper())
        if p is None:
            raise RuntimeError(f"OANDA has no price for {symbol} right now.")
        return p

    def prepare_futures(self, symbol, leverage):
        pass

    def _open_trades(self):
        return self._req("GET", "/openTrades").get("trades") or []

    def futures_positions(self, symbols):
        want = {instrument(s): s for s in symbols}
        out = {s: 0.0 for s in symbols}
        for t in self._open_trades():
            s = want.get(t.get("instrument"))
            if s:
                out[s] = out.get(s, 0.0) + abs(float(t.get("currentUnits") or 0))
        return out

    def market_order(self, symbol, side, amount, reduce_only=False):
        units = int(amount)
        if units <= 0:
            raise RuntimeError("Trade size is zero.")
        if reduce_only:
            trades = [t for t in self._open_trades() if t.get("instrument") == instrument(symbol)]
            if not trades:
                raise RuntimeError(f"No open OANDA trade on {symbol}.")
            t = trades[0]
            left = abs(int(float(t.get("currentUnits") or 0)))
            body = {"units": "ALL" if units >= left else str(units)}
            data = self._req("PUT", f"/trades/{t['id']}/close", body=body)
            fill = data.get("orderFillTransaction") or {}
            return {"id": fill.get("id") or t["id"], "average": float(fill.get("price") or 0) or None,
                    "filled": float(min(units, left))}
        signed = units if side == "buy" else -units
        body = {"order": {"type": "MARKET", "instrument": instrument(symbol), "units": str(signed),
                          "timeInForce": "FOK", "positionFill": "DEFAULT"}}
        data = self._req("POST", "/orders", body=body)
        fill = data.get("orderFillTransaction")
        if not fill:
            why = (data.get("orderCancelTransaction") or {}).get("reason") or "the order was not filled"
            raise RuntimeError(f"OANDA: {why}")
        trade_id = (fill.get("tradeOpened") or {}).get("tradeID") or fill.get("id")
        return {"id": str(trade_id), "average": float(fill.get("price")), "filled": abs(float(fill.get("units") or units))}

    def set_stop(self, trade_id, symbol, stop_price):
        """Attaches / moves the stop-loss on OANDA (held by the broker)."""
        self._req("PUT", f"/trades/{trade_id}/orders",
                  body={"stopLoss": {"price": f"{self.price_to_precision(symbol, stop_price)}", "timeInForce": "GTC"}})
        return str(trade_id)

    def backup_stop(self, symbol, close_side, amount, stop_price):
        return None

    def order_average(self, trade_id, symbol):
        if not trade_id:
            return None
        t = self._req("GET", f"/trades/{trade_id}").get("trade") or {}
        p = t.get("averageClosePrice")
        return float(p) if p else None

    def cancel_all(self, symbol):
        pass
