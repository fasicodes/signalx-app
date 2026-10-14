"""
Markets, symbols and forex rules for SignalX - the one place to configure them.

* CRYPTO_COINS / FOREX_PAIRS: every symbol the site knows (charts, pickers).
* FOREX_SIGNAL_PAIRS: the forex symbols that get trade signals. The default is
  the 7 majors, the popular crosses and gold; override it with the env var
  FOREX_SIGNAL_PAIRS="EUR/USD,GBP/USD,..." (only symbols in FOREX_PAIRS count).
* Pip sizes, typical spread (in pips) per pair, the forex trading week
  (Sunday 17:00 to Friday 17:00 New York time, so daylight saving is handled
  by the time-zone database) and the trading sessions.

Every datetime here is naive UTC, like the rest of the code base.
"""
import os
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

CRYPTO_COINS = [
    # TOP 1-10
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
    "TRX/USDT", "HYPE/USDT", "DOGE/USDT", "ZEC/USDT", "DOT/USDT",
    # TOP 11-20
    "XLM/USDT", "XMR/USDT", "LINK/USDT", "ADA/USDT", "BCH/USDT",
    "GRAM/USDT", "LTC/USDT", "SUI/USDT", "HBAR/USDT", "FIL/USDT",
    # TOP 21-30
    "AVAX/USDT", "CRO/USDT", "NEAR/USDT", "SHIB/USDT", "UNI/USDT",
    "TAO/USDT", "ONDO/USDT", "OKB/USDT", "ASTER/USDT", "ATOM/USDT",
    # 31-62 (all on OKX spot)
    "ETC/USDT", "AAVE/USDT", "ICP/USDT", "APT/USDT", "ARB/USDT",
    "OP/USDT", "FET/USDT", "TIA/USDT", "INJ/USDT", "SEI/USDT",
    "STX/USDT", "IMX/USDT", "GRT/USDT", "ALGO/USDT", "LDO/USDT",
    "CRV/USDT", "THETA/USDT", "SAND/USDT", "MANA/USDT", "AXS/USDT",
    "GALA/USDT", "CHZ/USDT", "APE/USDT", "EGLD/USDT", "XTZ/USDT",
    "SNX/USDT", "DYDX/USDT", "FLOW/USDT", "NEO/USDT", "IOTA/USDT",
    "CFX/USDT", "MINA/USDT",
]

FOREX_MAJORS = ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD"]
FOREX_CROSSES = ["EUR/JPY", "GBP/JPY", "EUR/GBP", "AUD/JPY", "EUR/AUD", "EUR/CHF", "GBP/CHF", "CAD/JPY"]
FOREX_METALS = ["XAU/USD", "XAG/USD"]

FOREX_PAIRS = [
    # Metals
    "XAU/USD", "XAG/USD",
    # Majors
    "EUR/USD", "USD/JPY", "GBP/USD", "USD/CHF", "AUD/USD",
    "USD/CAD", "NZD/USD",
    # Minors / crosses
    "EUR/JPY", "GBP/JPY", "EUR/GBP", "EUR/CHF", "AUD/JPY",
    "EUR/AUD", "GBP/CHF", "AUD/NZD", "NZD/JPY", "CAD/JPY",
    "CHF/JPY", "EUR/CAD", "GBP/CAD", "EUR/NZD", "AUD/CAD",
    "GBP/AUD", "GBP/NZD", "AUD/CHF", "NZD/CAD", "NZD/CHF",
    "CAD/CHF",
    # Exotics
    "USD/TRY", "USD/ZAR", "USD/MXN", "USD/SGD", "USD/HKD", "USD/SEK",
    "USD/NOK", "USD/DKK", "USD/PLN", "USD/CNH", "EUR/SEK", "EUR/NOK",
    "EUR/PLN", "EUR/TRY",
]

DEFAULT_FOREX_SIGNAL_PAIRS = FOREX_MAJORS + ["EUR/JPY", "GBP/JPY", "EUR/GBP", "AUD/JPY", "EUR/AUD", "EUR/CHF",
                                             "XAU/USD"]

# typical round-trip cost in pips (spread + slippage) used for results; real brokers vary
FOREX_COST_PIPS = {
    "EUR/USD": 1.2, "GBP/USD": 1.6, "USD/JPY": 1.4, "USD/CHF": 1.8, "AUD/USD": 1.5, "USD/CAD": 1.9,
    "NZD/USD": 2.0, "EUR/JPY": 2.0, "GBP/JPY": 3.0, "EUR/GBP": 1.6, "AUD/JPY": 2.2, "EUR/AUD": 2.6,
    "EUR/CHF": 2.0, "GBP/CHF": 3.0, "CAD/JPY": 2.4, "XAU/USD": 3.5, "XAG/USD": 3.0,
}
DEFAULT_COST_PIPS = 3.0
CRYPTO_COST_ROUND_TRIP = 0.0012   # 0.05% taker in + 0.05% out + 0.02% slippage

MARKETS = ("crypto", "forex")
TIMEFRAMES = ("15m", "1h", "4h")
TF_SEC = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def forex_signal_pairs():
    raw = (os.environ.get("FOREX_SIGNAL_PAIRS") or "").strip()
    if not raw:
        return list(DEFAULT_FOREX_SIGNAL_PAIRS)
    out = []
    for s in raw.split(","):
        s = s.strip().upper()
        if s in FOREX_PAIRS and s not in out:
            out.append(s)
    return out or list(DEFAULT_FOREX_SIGNAL_PAIRS)


def is_forex(symbol):
    return (symbol or "").upper() in FOREX_PAIRS


def is_crypto(symbol):
    return (symbol or "").upper() in CRYPTO_COINS


def market_of(symbol):
    return "forex" if is_forex(symbol) else "crypto"


def currencies(symbol):
    """'EUR/USD' -> ('EUR', 'USD'); gold counts as XAU."""
    parts = (symbol or "").upper().split("/")
    return tuple(parts) if len(parts) == 2 else ()


def pip_size(symbol):
    s = (symbol or "").upper()
    if s.startswith("XAU/"):
        return 0.1
    if s.startswith("XAG/"):
        return 0.01
    if s.endswith("/JPY"):
        return 0.01
    return 0.0001


def price_decimals(symbol):
    """Display decimals for a forex price (one more than the pip, like brokers)."""
    p = pip_size(symbol)
    return {0.1: 2, 0.01: 3, 0.0001: 5}.get(p, 5)


def cost_pips(symbol):
    return FOREX_COST_PIPS.get((symbol or "").upper(), DEFAULT_COST_PIPS)


def cost_price(symbol):
    return cost_pips(symbol) * pip_size(symbol)


def to_pips(symbol, price_diff):
    return float(price_diff) / pip_size(symbol)


# ---------------------------------------------------------------------------
# forex week + sessions (naive UTC in, naive UTC out)
# ---------------------------------------------------------------------------
def _tz(name):
    return ZoneInfo(name) if ZoneInfo else None


_UTC = _tz("UTC")
_NY = _tz("America/New_York")
SESSIONS = (  # name, time zone, local open hour, local close hour
    ("Sydney", "Australia/Sydney", 7, 16),
    ("Tokyo", "Asia/Tokyo", 9, 18),
    ("London", "Europe/London", 8, 17),
    ("New York", "America/New_York", 8, 17),
)


def _local(dt, tz):
    return dt.replace(tzinfo=_UTC).astimezone(tz)


def _utc(dt_local):
    return dt_local.astimezone(_UTC).replace(tzinfo=None)


def forex_open(dt):
    """True while the forex market is open: Sunday 17:00 to Friday 17:00 New York time."""
    ny = _local(dt, _NY)
    wd, h = ny.weekday(), ny.hour  # Mon=0 .. Sun=6
    if wd == 5:
        return False
    if wd == 4 and h >= 17:
        return False
    if wd == 6 and h < 17:
        return False
    return True


def forex_week_close(dt):
    """The next weekly close (Friday 17:00 New York) at or after dt, in UTC."""
    ny = _local(dt, _NY)
    days = (4 - ny.weekday()) % 7
    close = (ny + timedelta(days=days)).replace(hour=17, minute=0, second=0, microsecond=0)
    if close <= ny:
        close = (ny + timedelta(days=days + 7)).replace(hour=17, minute=0, second=0, microsecond=0)
    # rebuild with the right UTC offset for that date (daylight saving)
    close = datetime(close.year, close.month, close.day, 17, 0, tzinfo=_NY)
    return _utc(close)


def forex_week_open(dt):
    """The next weekly open (Sunday 17:00 New York) at or after dt, in UTC."""
    ny = _local(dt, _NY)
    days = (6 - ny.weekday()) % 7
    d = ny + timedelta(days=days)
    op = datetime(d.year, d.month, d.day, 17, 0, tzinfo=_NY)
    if op < ny:
        d = d + timedelta(days=7)
        op = datetime(d.year, d.month, d.day, 17, 0, tzinfo=_NY)
    return _utc(op)


def hours_to_close(dt):
    if not forex_open(dt):
        return 0.0
    return (forex_week_close(dt) - dt).total_seconds() / 3600


def sessions_at(dt):
    """Names of the forex sessions open at dt (weekdays only)."""
    if not forex_open(dt):
        return []
    out = []
    for name, tzname, a, b in SESSIONS:
        loc = _local(dt, _tz(tzname))
        if loc.weekday() < 5 and a <= loc.hour < b:
            out.append(name)
    return out


def session_label(dt):
    s = sessions_at(dt)
    if not s:
        return "Closed" if not forex_open(dt) else "Off-hours"
    return " + ".join(s)
