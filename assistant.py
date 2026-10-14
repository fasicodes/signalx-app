"""
Auto-trade assistant - Signals FM
=================================

Answers free questions on the Auto-trade bot page ("why did the bot not trade?", "what is leverage?",
"BTC price?", "kya settings rakhun?") instead of only understanding fixed commands. The page still runs
its own commands first (start bot, buy btc, signal eth, ...); anything else comes here.

Two ways to answer:
  1. AI (optional): when ANTHROPIC_API_KEY is set in Railway Variables, the question goes to Claude together
     with a short, read-only summary of the user's bot account (settings, positions, last scan, recent errors).
     The model only explains: it cannot place, change or close anything.
       ANTHROPIC_API_KEY    (optional) turns the AI answers on
       ASSISTANT_MODEL      (optional) default "claude-haiku-5-5"
  2. Built-in (always available, no cost): plain-language answers from the user's own bot data, the
     glossary (glossary.py) and the bot's rules. Used when no key is set or the AI call fails.

answer(question, ctx, history) -> {"reply": str, "source": "ai" | "built-in", "suggest": [str, ...]}
ctx is a plain dict built by autotrade.assistant_route (see _ctx_lines for the fields read).
"""

import os
import re

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    from glossary import GLOSSARY
except Exception:  # pragma: no cover
    GLOSSARY = {}

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"
MAX_QUESTION = 600
MAX_HISTORY = 8

SUGGEST = ["Why did the bot not trade?", "How does the bot work?", "What settings should I use?",
           "What is leverage?", "Explain my open positions", "help"]

# extra terms the bot page uses (glossary.py covers the rest)
BOT_TERMS = {
    "spot": ("Spot", "Buying the coin itself with no borrowed money. You can only profit when the price goes up (LONG), "
                     "and you can never lose more than you put in."),
    "futures": ("Futures", "Contracts that follow the coin's price. They let you go LONG or SHORT and use leverage, so "
                           "profits and losses both grow faster."),
    "risk_pct": ("Risk per trade", "How much of your balance one trade may lose if its stop-loss is hit. The bot sizes every "
                                   "trade from it. 0.5% to 1% is a common choice; 2% is already aggressive."),
    "daily_loss": ("Daily loss limit", "If closed trades lose this % of the balance in one UTC day, the bot pauses until "
                                       "00:00 UTC. It stops a bad day from turning into a disaster."),
    "min_confidence": ("Min signal confidence", "The bot only trades signals whose confidence is at least this high. "
                                                 "Higher means fewer but more selective trades. Set it above every "
                                                 "signal's confidence and the bot never trades; the settings form shows the range."),
    "reward_risk": ("Reward : Risk", "How far the take-profit is compared with the stop. 1.5 means the target is 1.5 times "
                                     "the stop distance. Signal engine v3 sets its own levels (a stop and three targets) when it has them."),
    "max_positions": ("Max open positions", "The most trades the bot keeps open at the same time on this account."),
    "max_position": ("Max position size", "The biggest single trade (in USDT) the bot may open, whatever the risk % says."),
    "site_demo": ("Site demo account", "The bot trades your Demo trading account on this site: virtual USDT, live prices, "
                                       "real fees and stops. No Binance keys needed. Open Demo trading to see its trades."),
    "binance_demo": ("Binance Demo account", "Binance's own demo exchange (demo.binance.com). Needs an API key from there; "
                                             "orders go to the real Binance engine with fake money."),
    "live": ("Live account", "Your real Binance account. Every order uses real money. Test on a demo account first."),
    "isolated": ("Isolated margin", "Each position has its own margin. If it is liquidated you only lose that margin. "
                                    "The bot always uses isolated margin."),
    "cooldown": ("Cooldown", "After a trade on a coin closes, the bot waits one candle of your timeframe before it trades "
                             "that coin again."),
}

# words (English + Roman Urdu) -> intent
INTENTS = [
    ("greet", r"^(hi|hello|hey|salam|assalam|aoa|hola)\b"),
    ("thanks", r"\b(thanks|thank you|shukriya|jazakallah)\b"),
    ("why_no_trade", r"\b(why|kyun|kyon|kiun|q)\b.*\b(no|not|nahi|nai|nahin|didn'?t|didnt|isn'?t|won'?t)\b.*\b(trade|trades|trading|order|buy|position|lag|open)"
                     r"|\b(trade|trades|trading|order)\b.*\b(kyun|kyon|kiun|nahi|nai|nahin)\b"
                     r"|\b(no|nahi|nai|zero)\b.*\btrades?\b|why .*(idle|waiting|nothing|not working)|kuch nahi kar|not trading"),
    ("how_bot", r"how (does|do) (the |this |my )?bot|bot (kaise|kese|kis tarah)|how .*bot work|what does the bot do|bot kya karta"),
    ("settings", r"(best|good|recommend|suggest|safe|kya|konsi|kaunsi|which).*(setting|settings|risk|leverage)|settings? (kya|kaise)"),
    ("positions", r"(explain|show|what|meri|my).*(position|positions|trade|trades|pnl|profit|loss)|open (position|trade)"),
    ("performance", r"(win ?rate|results?|performance|profitable|profit hoga|accurate|accuracy|kitna kama|returns?)"),
    ("connect", r"(connect|api ?key|secret|binance key|link binance|kaise connect)"),
    ("accounts", r"(difference|farq|fark|vs|versus).*(demo|live|site|paper)|which account|kaunsa account|site demo|paper"),
    ("risk", r"(how much|kitna|kitni).*(risk|invest|lagaun|lagau|paisa|money)|risk management"),
    ("safe", r"\b(safe|guarantee|sure|pakka|loss nahi)\b"),
]


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip())


def _money(v, sign=False):
    if v is None:
        return "-"
    v = float(v)
    s = f"{abs(v):,.2f}"
    return ("-" if v < 0 else "+" if sign and v > 0 else "") + s + " USDT"


def _price(v):
    if v is None:
        return "-"
    v = float(v)
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return f"{v:.6g}"


# ------------------------------------------------------------------------------------------------ built-in answers
def _find_term(q):
    ql = q.lower()
    best = None
    for key, (term, text) in BOT_TERMS.items():
        if term.lower() in ql or key.replace("_", " ") in ql:
            if best is None or len(term) > len(best[0]):
                best = (term, text)
    for key, item in GLOSSARY.items():
        term = item["term"]
        names = {term.lower(), key.replace("_", " ")}
        names |= {re.sub(r"\s*\(.*?\)", "", term).strip().lower()}
        if any(n and re.search(r"\b" + re.escape(n) + r"\b", ql) for n in names):
            if best is None or len(term) > len(best[0]):
                best = (term, item["text"])
    return best


def _why_no_trade(ctx):
    acct, bot, st = ctx.get("account_label", "this"), ctx.get("bot") or {}, ctx.get("settings") or {}
    lines = []
    if not ctx.get("connected"):
        return f"The {acct} account is not connected yet, so the bot cannot trade. Connect it with the button at the top."
    if not bot.get("enabled"):
        return (f"The {acct} bot is OFF, so it does not open trades. Save your settings, then switch the bot on. "
                "Open positions are still watched for their stop-loss and targets.")
    if bot.get("paused_until"):
        return f"The bot is paused ({bot.get('pause_reason') or 'daily loss limit'}) until {bot['paused_until']} UTC. It starts again by itself."
    if not ctx.get("engine_online"):
        lines.append("The background engine is offline right now, so no scans run. It restarts by itself; if it stays "
                     "offline, check the server logs on Railway.")
    scan = ctx.get("last_scan")
    if scan:
        notes = [n.strip() for n in scan.split(":", 1)[-1].split("|") if n.strip()]
        waits = [n for n in notes if "WAIT" in n]
        low = [n for n in notes if "low confidence" in n]
        nospot = [n for n in notes if "no shorts on spot" in n]
        full = [n for n in notes if "max positions" in n]
        skipped = [n for n in notes if "skipped" in n]
        lines.append(f"Last scan: {scan.split(':', 1)[-1].strip()}")
        if waits and len(waits) == len(notes):
            lines.append("Every coin was WAIT: signal engine v3 gives a coin a signal only when its setup is good enough (on closed "
                         "4-hour candles), so many scans end with no trade. That is normal, not a fault.")
        elif waits:
            lines.append(f"{len(waits)} coin(s) were WAIT (no setup).")
        if low:
            lines.append(f"{len(low)} signal(s) were below your minimum confidence of {st.get('min_confidence', '?')}%. "
                         "Lowering it gives more (weaker) trades.")
        if nospot:
            lines.append("Some signals were SHORT, which spot cannot trade. Switch the market to Futures to take shorts.")
        if full:
            lines.append(f"You already have the maximum of {st.get('max_open_positions', '?')} open position(s).")
        if skipped:
            lines.append("Some trades were skipped: see the WARN lines in the Activity log for the exact reason "
                         "(often a trade size below the minimum or not enough balance).")
    else:
        lines.append("No scan has run yet. The bot scans every few minutes after it is switched on.")
    lines.append("The bot only enters on a signal from the 4-hour candle that just closed, never late.")
    return "\n".join(lines)


def _how_bot(ctx):
    st = ctx.get("settings") or {}
    return ("Here is what the bot does on every scan:\n"
            f"1. It checks each coin you ticked ({len(st.get('coins') or [])} now) with signal engine v3 (closed 4-hour candles).\n"
            f"2. It trades only a fresh LONG or SHORT with at least {st.get('min_confidence', 65)}% confidence"
            f"{' (no shorts on spot)' if st.get('market_type') == 'spot' else ''}.\n"
            f"3. The size is set so that hitting the stop-loss loses about {st.get('risk_pct', 1)}% of the balance, "
            f"capped at {st.get('max_position_usdt', 100)} USDT.\n"
            "4. Every trade gets a stop-loss and three targets. The bot closes a third at each target, moves the stop to the entry "
            "after TP1 and to TP1 after TP2, and watches the levels all the time.\n"
            f"5. It pauses for the day after a {st.get('daily_loss_limit_pct', 5)}% daily loss, and keeps at most "
            f"{st.get('max_open_positions', 2)} trades open.\n"
            "Results are never guaranteed: signals lose regularly, and a full loss is bigger than most wins.")


def _settings(ctx):
    st = ctx.get("settings") or {}
    cur = (f"Yours now: {st.get('market_type', 'spot')} {st.get('leverage', 1)}x, risk {st.get('risk_pct')}%, "
           f"min confidence {st.get('min_confidence')}%, daily limit {st.get('daily_loss_limit_pct')}%.") if st else ""
    return ("A careful starting point:\n"
            "- Account: Site demo or Binance Demo first, for a few weeks.\n"
            "- Market: Spot (no leverage) or Futures at 1x to 3x.\n"
            "- Risk per trade: 0.5% to 1%.\n"
            "- Max open positions: 2. Daily loss limit: 3% to 5%.\n"
            "- Min confidence: 65% or more. Coins: the big ones (BTC, ETH, SOL) trade most cleanly.\n"
            + cur)


def _positions(ctx):
    ps = ctx.get("positions") or []
    if not ps:
        s = ctx.get("stats") or {}
        done = (f" Closed so far: {s['closed_count']} trades, total {_money(s.get('total_pnl'), True)}, today "
                f"{_money(s.get('realized_today'), True)}.") if s.get("closed_count") else ""
        return f"There are no open positions on the {ctx.get('account_label', 'this')} account right now." + done
    out = []
    for p in ps:
        tps = p.get("targets") or [p.get("take_profit")]
        tp_txt = " / ".join(_price(v) for v in tps if v is not None)
        hits = f" ({p['hits']} target{'s' if p['hits'] != 1 else ''} reached)" if p.get("hits") else ""
        out.append(f"{p['side']} {p['symbol']}: entry {_price(p.get('entry_price'))}, now {_price(p.get('last_price'))}, "
                   f"stop {_price(p.get('stop_loss'))}, targets {tp_txt}{hits}, open PnL {_money(p.get('unrealized_pnl'), True)}.")
    return "\n".join(out) + ("\nThe bot closes a third at each target and the rest at the stop or the last target. "
                             "You can also close a position by hand with its Close button.")


def _performance(ctx):
    s = ctx.get("stats") or {}
    if not s.get("closed_count"):
        mine = "This account has no closed bot trades yet."
    else:
        mine = (f"This account: {s['closed_count']} closed trades, {s.get('wins', 0)} won, win rate {s.get('win_rate')}%, "
                f"total {_money(s.get('total_pnl'), True)}.")
    return (mine + "\nIn testing on data the model never saw (Oct 2025 to Oct 2026), about 3 in 4 engine signals closed in "
            "profit, +0.13R per signal on average after fees. A full loss (-1R) is bigger than most wins, and there were "
            "losing months. Past results do not guarantee future results.")


def _connect(ctx):
    return ("Site demo needs no keys: pick it in the account switcher and turn the bot on.\n"
            "For Binance Demo: on demo.binance.com open Profile > API Management, create a key, then click Connect and paste the key and secret.\n"
            "For Live: create the key on binance.com with only Reading and Spot/Futures Trading enabled. Keep withdrawals OFF "
            "(the server refuses keys that can withdraw), and restrict the key to the server's IP if you can.")


def _accounts(ctx):
    return ("There are three accounts, each with its own bot, settings and history:\n"
            "- Site demo: trades your Demo trading account on this site. Virtual money, live prices, no keys.\n"
            "- Binance Demo: Binance's demo exchange, connected with a demo API key. Fake money on the real Binance engine.\n"
            "- Live: your real Binance account and real money.\n"
            "Start on a demo account and move to Live only after weeks of results you understand.")


def _risk(ctx):
    bal = ctx.get("balance")
    extra = f" With {_money(bal)} free, 1% is about {_money(bal * 0.01)} per trade." if bal else ""
    return ("Risk only money you can afford to lose. A common rule is to risk 0.5% to 1% of the account per trade, so a "
            "losing streak of 10 trades costs about 10%, not the whole account." + extra)


def _coin_answer(ctx):
    c = ctx.get("coin") or {}
    if not c:
        return None
    parts = [f"{c['symbol']}: {_price(c.get('price'))} USDT"]
    if c.get("change_pct") is not None:
        parts[0] += f" ({c['change_pct']:+.2f}% in 24h)"
    sg = c.get("signal")
    if sg:
        verdict = sg.get("verdict")
        txt = f"Signal engine v3: {verdict}"
        if sg.get("confidence") is not None:
            txt += f", {round(sg['confidence'])}% confidence"
        if verdict == "WAIT" and sg.get("active_side"):
            txt += f" (an earlier {sg['active_side']} signal is still running)"
        parts.append(txt + ".")
    return " ".join(parts)


DEFINE = re.compile(r"^(what\s+is|what's|whats|what\s+are|what\s+does|define|meaning\s+of|explain|tell\s+me\s+about)\b"
                    r"|\b(kya\s+hai|kya\s+hota|kya\s+hain|matlab|means?|meaning)\b")


def builtin_answer(question, ctx):
    q = _norm(question)
    ql = q.lower()
    if DEFINE.search(ql) and not re.search(r"\b(my|meri|mera|mere|bot)\b", ql):
        term = _find_term(q)
        if term:
            return f"{term[0]}: {term[1]}"
    for name, pattern in INTENTS:
        if re.search(pattern, ql):
            if name == "greet":
                return "Hi! Ask me anything about the bot, your account or trading terms. For example: \"why did the bot not trade?\""
            if name == "thanks":
                return "You're welcome. Trade safely!"
            if name == "why_no_trade":
                return _why_no_trade(ctx)
            if name == "how_bot":
                return _how_bot(ctx)
            if name == "settings":
                return _settings(ctx)
            if name == "positions":
                return _positions(ctx)
            if name == "performance":
                return _performance(ctx)
            if name == "connect":
                return _connect(ctx)
            if name == "accounts":
                return _accounts(ctx)
            if name == "risk":
                return _risk(ctx)
            if name == "safe":
                return ("No trading bot is safe or guaranteed. Signals lose regularly and leverage makes losses bigger. "
                        "Use a demo account first, keep risk per trade small (0.5% to 1%), and never trade money you need.")
    term = _find_term(q)
    if term:
        return f"{term[0]}: {term[1]}"
    coin = _coin_answer(ctx)
    if coin:
        return coin
    return None


# ------------------------------------------------------------------------------------------------ AI answers
def _ctx_lines(ctx):
    st = ctx.get("settings") or {}
    bot = ctx.get("bot") or {}
    lines = [
        f"Account: {ctx.get('account_label')} ({'connected' if ctx.get('connected') else 'not connected'}), "
        f"free balance {_money(ctx.get('balance'))}, engine {'online' if ctx.get('engine_online') else 'offline'}.",
        f"Bot: {'ON' if bot.get('enabled') else 'OFF'}"
        + (f", paused until {bot.get('paused_until')} ({bot.get('pause_reason')})" if bot.get("paused_until") else "")
        + f", last scan {bot.get('last_scan_at') or 'never'}.",
        f"Settings: {st.get('market_type')} {st.get('leverage')}x, risk {st.get('risk_pct')}%, max position "
        f"{st.get('max_position_usdt')} USDT, max open {st.get('max_open_positions')}, daily loss limit "
        f"{st.get('daily_loss_limit_pct')}%, min confidence {st.get('min_confidence')}%, reward:risk {st.get('reward_risk')}, "
        f"timeframe {st.get('timeframe')}, coins {', '.join(st.get('coins') or [])}.",
    ]
    ps = ctx.get("positions") or []
    lines.append("Open positions: " + ("; ".join(
        f"{p['side']} {p['symbol']} entry {_price(p.get('entry_price'))} now {_price(p.get('last_price'))} SL {_price(p.get('stop_loss'))} "
        f"TP {_price(p.get('take_profit'))} PnL {_money(p.get('unrealized_pnl'), True)}" for p in ps) if ps else "none"))
    s = ctx.get("stats") or {}
    lines.append(f"Closed trades: {s.get('closed_count', 0)}, win rate {s.get('win_rate')}, total {_money(s.get('total_pnl'), True)}, "
                 f"today {_money(s.get('realized_today'), True)}.")
    if ctx.get("last_scan"):
        lines.append(f"Last scan log: {ctx['last_scan']}")
    for l in (ctx.get("recent_logs") or [])[:5]:
        lines.append(f"Log {l}")
    coin = _coin_answer(ctx)
    if coin:
        lines.append("Coin asked about: " + coin)
    return "\n".join(lines)


SYSTEM = """You are the assistant on the Auto-trade bot page of Signals FM, a crypto and forex signals website.
Answer the user's question clearly and briefly (usually 2 to 6 short sentences or a short list). Always reply in
English, even when the question is written in another language (the website is English-only).

What you know about the product:
- The site gives separate signal streams for 15-minute, 1-hour and 4-hour candles, for crypto (62 coins) and forex
  (the 7 majors, popular crosses and gold). Every signal has an entry, a stop, three targets (TP1, TP2, TP3), a grade
  (A, B or C) and an expected holding time. The test results of each stream are on the Signals page.
- The bot takes every new site signal that matches the account's settings (markets, timeframes, minimum grade,
  minimum confidence, all symbols or only the ticked ones), at once, with the signal's own stop and targets. It only
  enters while the price is still close to the entry (within 0.35 of the stop distance, before TP1, at most two candles
  after the signal). It sizes the trade so hitting the stop loses about "risk per trade" % of the balance, closes a third
  at each target, moves the stop to the entry after TP1 and to TP1 after TP2. Every signal is ordered at most once per account.
- Accounts: "Site demo" (virtual money on this site, crypto + forex, no keys), "Binance Demo" (demo.binance.com key),
  "Live" (real money on Binance), "OANDA demo" and "OANDA live" (forex). Each has its own bot, settings and history.
- Safety: max open positions, daily loss limit (pauses the bot until 00:00 UTC), the kill switch (bot off + every position
  closed), spot cannot short, leverage capped at 20x, live keys with withdrawal permission are refused, keys are encrypted.
- Never promise profit; never give personalised financial advice. Past results do not guarantee future results.

Rules:
- You can only explain. You cannot place, change or close trades or settings. If the user wants an action, tell them the
  exact command to type here ("start bot", "stop bot", "buy btc", "short eth", "signal sol", "status", "stop all") or the button to use.
- Use the account summary below for anything about their bot; do not invent numbers that are not in it.
- If you do not know, say so briefly.

The user's bot account right now:
"""


def ai_answer(question, ctx, history=None, timeout=25):
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key or requests is None:
        return None
    msgs = []
    for h in (history or [])[-MAX_HISTORY:]:
        role = "assistant" if h.get("who") == "ai" else "user"
        text = _norm(h.get("text"))[:800]
        if not text:
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + text
        else:
            msgs.append({"role": role, "content": text})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n" + question
    else:
        msgs.append({"role": "user", "content": question})
    try:
        r = requests.post(
            API_URL,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": (os.environ.get("ASSISTANT_MODEL") or DEFAULT_MODEL).strip(), "max_tokens": 700,
                  "system": SYSTEM + _ctx_lines(ctx), "messages": msgs},
            timeout=timeout,
        )
        if r.status_code != 200:
            print(f"[assistant] AI request failed: {r.status_code} {r.text[:200]}")
            return None
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        return text or None
    except Exception as e:  # network, timeout, bad JSON: fall back to the built-in answers
        print(f"[assistant] AI request error: {type(e).__name__}: {e}")
        return None


def ai_enabled():
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()) and requests is not None


def answer(question, ctx, history=None, use_ai=True):
    question = _norm(question)[:MAX_QUESTION]
    if not question:
        return {"reply": "Type a question, for example: why did the bot not trade?", "source": "built-in", "suggest": SUGGEST[:4]}
    text = ai_answer(question, ctx, history) if use_ai else None
    if text:
        return {"reply": text, "source": "ai", "suggest": []}
    text = builtin_answer(question, ctx)
    if text:
        return {"reply": text, "source": "built-in", "suggest": []}
    return {
        "reply": ("I'm not sure about that one. I can explain your bot (why it did or did not trade, your positions and "
                  "results), its settings, the accounts, and trading terms like leverage, stop-loss or funding. "
                  "Commands like \"start bot\" or \"buy btc\" also work here; type \"help\" to see them."),
        "source": "built-in",
        "suggest": SUGGEST[:4],
    }
