"""
Site-wide AI assistant (chat widget on every main page) - Signals FM.

POST /api/assistant/chat    streams the answer (text/event-stream: data: {"t": "..."} ... data: {"done": true})
POST /api/assistant/ask     the same answer as one JSON reply (for clients without streaming)

* Provider: the Claude API (ANTHROPIC_API_KEY, model ASSISTANT_MODEL, default claude-haiku-5-5), server side only.
  Streaming, a timeout, one retry on overload, and a friendly built-in answer when the AI is not configured,
  busy or fails - the user never sees an empty reply or a raw error.
* Live context for every question: the signal the user is looking at (symbol, timeframe, entry, stop, targets,
  status, grade), the newest active signals, each stream's results (live record + backtest), the next
  high-impact economic events and the latest headlines.
* Conversation history: the widget sends the last messages (kept in the browser); the server trims it.
* Rate limits: ASSISTANT_PER_HOUR (default 30) answers per logged-in user per hour, ASSISTANT_GUEST_PER_HOUR
  (default 8) per visitor IP; built-in answers do not count.
* English only; every answer is information, not financial advice.
"""
import json
import os
import re
import threading
import time
from collections import deque

from flask import Blueprint, Response, jsonify, request, session, stream_with_context

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    from glossary import GLOSSARY
except Exception:  # pragma: no cover
    GLOSSARY = {}

assistant_bp = Blueprint("site_assistant", __name__)

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"
MAX_QUESTION = 1200
MAX_HISTORY = 10
MAX_HISTORY_CHARS = 6000
_hooks = {"context": None}
_usage = {}
_usage_lock = threading.Lock()

DISCLAIMER = "Information only, not financial advice."

SYSTEM = """You are the assistant of Signals FM, a website with trading signals for crypto and forex on 15-minute,
1-hour and 4-hour candles, a news and economic calendar section, demo trading and an auto-trade bot.

Answer every question helpfully and completely: trading and markets, how to read and use a signal, indicators
(RSI, MACD, moving averages, ATR, ADX, support and resistance), risk management and position sizing, news and
economic calendar events, how to use the website and the auto-trade bot, and general questions too. Never refuse an
ordinary question. Keep answers clear and well structured (short paragraphs or a short list; usually under 220 words
unless the user asks for more).

Always write in English, even when the question is in another language (the website is English-only).

How the website works (use this when relevant):
- Signals: separate streams for 15m, 1h and 4h, crypto and forex. Each signal has an entry (the close of the signal
  candle), a stop loss, three targets (TP1, TP2, TP3), a grade (A best, then B, then C), an expected holding time and a
  status (active, TP hit, stop hit, expired, cancelled). Plan: close a third at each target; after TP1 move the stop to
  the entry, after TP2 to TP1; the rest closes when the time limit ends. "Win rate" counts signals that reached at least TP1.
- Forex signals pause around high-impact news for that currency and are closed before the weekend.
- News: an economic calendar (impact, actual / forecast / previous) and a news feed; high-impact events matter most.
- Auto-trade bot: takes the site's signals automatically on the accounts the user connects (Site demo with virtual
  money, Binance demo / live, OANDA demo / live for forex) with risk % per trade, max open trades, a daily loss limit
  and a kill switch. Paper (demo) first; live needs an explicit confirmation.

Rules:
- Use the live data below for anything about current signals, results and news; never invent signals, prices,
  statistics or events that are not in it. If the data does not contain the answer, say what you do know.
- You cannot place or change trades; point to the right page or button instead.
- Explain risk honestly: never promise profit, never say a trade is certain, and do not give personalised
  financial advice (you can explain how others commonly approach a decision).
"""


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


# ---------------------------------------------------------------------------
# rate limit
# ---------------------------------------------------------------------------
def _limit_key():
    uid = session.get("user_id")
    if uid:
        return f"u:{uid}", int(os.environ.get("ASSISTANT_PER_HOUR", "30") or 30)
    # main.py runs behind ProxyFix, so remote_addr is already the visitor's real address as seen by Railway's
    # proxy. The X-Forwarded-For header must not be read here: its first entry is written by the visitor,
    # who could change it on every request and so never reach the limit.
    ip = request.remote_addr or "?"
    return f"ip:{ip}", int(os.environ.get("ASSISTANT_GUEST_PER_HOUR", "8") or 8)


def _allowed(key, per_hour):
    now = time.time()
    with _usage_lock:
        q = _usage.setdefault(key, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= per_hour:
            return False, int(3600 - (now - q[0]))
        q.append(now)
        if len(_usage) > 5000:
            for k in [k for k, v in _usage.items() if not v][:2000]:
                _usage.pop(k, None)
        return True, 0


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------
def build_context(page=None, signal_id=None, symbol=None):
    fn = _hooks.get("context")
    if not fn:
        return {}
    try:
        return fn(page=page, signal_id=signal_id, symbol=symbol) or {}
    except Exception as e:
        print(f"[assistant] context failed: {type(e).__name__}: {e}")
        return {}


def _sig_line(s):
    tps = " / ".join(str(x) for x in (s.get("targets") or []))
    res = ""
    if s.get("r") is not None:
        res = f", result {s['r']:+}R"
    return (f"#{s.get('id')} {s.get('symbol')} {s.get('timeframe')} {s.get('side')} grade {s.get('grade')}, "
            f"entry {s.get('entry')}, stop {s.get('stop_loss')}, targets {tps}, status {s.get('status')}{res}, "
            f"started {s.get('signal_at')}, expected hold {s.get('hold_minutes')} min"
            + (f", now {s.get('price')} ({s.get('r_now'):+}R)" if s.get("price") is not None and s.get("r_now") is not None else "")
            + (f", news warning: {s['news'].get('title')} at {s['news'].get('at')}" if isinstance(s.get("news"), dict) else ""))


def context_text(ctx):
    lines = []
    if ctx.get("now"):
        lines.append(f"Current time (UTC): {ctx['now']}")
    if ctx.get("page"):
        lines.append(f"The user is on the page: {ctx['page']}")
    if ctx.get("viewing_signal"):
        lines.append("Signal the user is looking at: " + _sig_line(ctx["viewing_signal"]))
    if ctx.get("symbol"):
        lines.append(f"Symbol on screen: {ctx['symbol']}")
    if ctx.get("active_signals"):
        lines.append("Newest active signals:")
        lines += ["- " + _sig_line(s) for s in ctx["active_signals"][:8]]
    for st in ctx.get("stream_stats") or []:
        w = (st.get("windows") or {}).get("all") or {}
        t = st.get("test") or {}
        lines.append(f"Stream {st.get('market')} {st.get('timeframe')}: live record {w.get('closed', 0)} closed signals, "
                     f"win rate {w.get('win_rate')}%, avg {w.get('avg_r')}R; "
                     f"backtest on unseen data: {t.get('signals')} signals, win rate {t.get('win_rate')}%, "
                     f"avg {t.get('avg_net_r')}R ({t.get('period')})")
    if ctx.get("next_events"):
        lines.append("Next high-impact economic events (UTC):")
        lines += [f"- {e.get('time')} {e.get('currency')} {e.get('title')} (forecast {e.get('forecast')}, previous {e.get('previous')})"
                  for e in ctx["next_events"][:6]]
    if ctx.get("headlines"):
        lines.append("Latest headlines:")
        lines += [f"- {h.get('title')} ({h.get('source')}, {h.get('published_at')})" for h in ctx["headlines"][:6]]
    return "\n".join(lines) if lines else "No live data is available right now."


# ---------------------------------------------------------------------------
# built-in answers (no AI key, AI busy or failed)
# ---------------------------------------------------------------------------
SITE_FAQ = [
    (r"\b(grade|grades|a b c)\b", "Every signal has a grade. A means the most confirming factors and the highest model score, "
                                  "B is solid, C meets the minimum. You can filter the Signals page by grade, and the bot can be "
                                  "set to take only A, A and B, or all grades."),
    (r"\b(how|what).*(use|read|follow|trade).*signal|\bsignal\b.*\b(mean|work)",
     "How to use a signal: enter near the entry price, put the stop loss where the signal says, and close a third of the "
     "position at each target (TP1, TP2, TP3). After TP1 move the stop to the entry, after TP2 to TP1. If nothing is reached "
     "before the time limit, close the rest. Size the trade so that hitting the stop loses only a small part of your account "
     "(for example 0.5-1%)."),
    (r"\b(15m|1h|4h|timeframe|time frame)\b", "The site has separate signal streams for 15-minute, 1-hour and 4-hour candles. "
                                               "Shorter timeframes give more signals with smaller moves and shorter holding times; "
                                               "4-hour signals move further and are held for days. Each stream has its own results."),
    (r"\b(win rate|accuracy|results?|track record|performance)\b",
     "Win rate counts signals that reached at least TP1. The Signals page shows, per market and timeframe, the live record "
     "from real closed signals (all time, 30 days, 7 days, with the sample size) and, separately, the backtest on data the "
     "model never saw. Past results do not guarantee future results."),
    (r"\b(news|calendar|nfp|cpi|fomc|interest rate|central bank)\b",
     "The News page has the economic calendar (time, currency, impact, actual / forecast / previous) and a news feed. "
     "High-impact releases can move prices sharply; forex signals for that currency are paused from 30 minutes before to "
     "15 minutes after them."),
    (r"\b(bot|auto.?trade|autotrade)\b",
     "The auto-trade bot copies the site's signals onto the account you connect (Site demo with virtual money, Binance, or "
     "OANDA for forex). Choose markets, timeframes, minimum grade, risk % per trade, max open trades and the daily loss limit, "
     "then turn it on. The kill switch stops it and closes every position. Start on a demo account."),
    (r"\b(risk|position size|lot size|how much)\b",
     "A common approach is to risk a small, fixed part of the account per trade, for example 0.5-1%. Position size = "
     "(account x risk %) / distance to the stop. Never move the stop further away, and keep the total risk of open trades limited."),
    (r"\b(forex|pip|pips|spread|session)\b",
     "Forex prices move in pips (0.0001 for most pairs, 0.01 for JPY pairs, 0.1 for gold here). The spread is the cost of "
     "every trade. The London and New York sessions are the most active; the market is closed from Friday 17:00 to Sunday "
     "17:00 New York time, so forex signals close before the weekend."),
]


def _terms():
    out = []
    for key, v in GLOSSARY.items():
        if isinstance(v, dict) and v.get("term") and v.get("text"):
            out.append((str(v["term"]), str(v["text"])))
    return out


def builtin_answer(question, ctx):
    q = _norm(question).lower()
    if re.search(r"\b(this|the) signal\b|explain (it|this)", q) and ctx.get("viewing_signal"):
        s = ctx["viewing_signal"]
        return ("This signal: " + _sig_line(s) + ". Follow the plan: a third closes at each target, after TP1 the stop moves to "
                "the entry. Grade " + str(s.get("grade")) + " (A is the strongest). This is information, not advice.")
    if re.search(r"\b(latest|active|current|new|today'?s?)\b.*\bsignals?\b", q) and ctx.get("active_signals"):
        return "Newest active signals:\n" + "\n".join("- " + _sig_line(s) for s in ctx["active_signals"][:5])
    if re.search(r"\b(next|upcoming)\b.*\b(news|event|release|calendar)\b", q) and ctx.get("next_events"):
        return "Next high-impact events (UTC):\n" + "\n".join(
            f"- {e.get('time')} {e.get('currency')} {e.get('title')}" for e in ctx["next_events"][:5])
    defining = re.search(r"\b(what|meaning|mean|explain|define|definition)\b", q)
    terms = sorted(_terms(), key=lambda t: -len(t[0]))
    if defining:
        for name, text in terms:
            if re.search(r"(?<![a-z])" + re.escape(name.lower()) + r"(?![a-z])", q):
                return f"{name}: {text}"
    for pattern, text in SITE_FAQ:
        if re.search(pattern, q):
            return text
    for name, text in terms:
        if re.search(r"(?<![a-z])" + re.escape(name.lower()) + r"(?![a-z])", q):
            return f"{name}: {text}"
    return None


def fallback_text(question, ctx, busy=False):
    text = builtin_answer(question, ctx)
    head = ("The AI assistant is busy right now, so here is a short answer from the site's own help. "
            if busy else "")
    if text:
        return (head + "\n\n" + text) if busy else text
    return (head + "I can explain signals (entry, stop, targets, grades, timeframes), the results of each stream, news and "
            "economic events, risk management and position sizing, trading terms, and how to use the website and the "
            "auto-trade bot. Try asking, for example: \"How do I use a signal?\" or \"What is a stop loss?\"")


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------
def ai_enabled():
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()) and requests is not None


def _messages(question, history):
    msgs, total = [], 0
    for h in reversed((history or [])[-MAX_HISTORY:]):
        role = "assistant" if h.get("role") in ("assistant", "ai") else "user"
        text = _norm(h.get("text") or h.get("content"))[:1500]
        if not text:
            continue
        total += len(text)
        if total > MAX_HISTORY_CHARS:
            break
        msgs.insert(0, {"role": role, "content": text})
    merged = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(dict(m))
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    if merged and merged[-1]["role"] == "user":
        merged[-1]["content"] += "\n" + question
    else:
        merged.append({"role": "user", "content": question})
    return merged


def stream_ai(question, ctx, history=None, timeout=(8, 40)):
    """Yields text chunks from Claude (streaming). Raises on failure before any text."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    body = {"model": (os.environ.get("ASSISTANT_MODEL") or DEFAULT_MODEL).strip(), "max_tokens": 900, "stream": True,
            "system": SYSTEM + "\n\nLive data from the website:\n" + context_text(ctx),
            "messages": _messages(question, history)}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    last = None
    for attempt in range(2):
        try:
            r = requests.post(API_URL, headers=headers, json=body, timeout=timeout, stream=True)
        except Exception as e:
            last = e
            time.sleep(1.0)
            continue
        if r.status_code in (429, 500, 502, 503, 529) and attempt == 0:
            last = RuntimeError(f"HTTP {r.status_code}")
            time.sleep(1.0)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"AI request failed: HTTP {r.status_code}")
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            try:
                ev = json.loads(raw[5:].strip())
            except ValueError:
                continue
            if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                yield ev["delta"].get("text", "")
            elif ev.get("type") == "error":
                raise RuntimeError("AI stream error")
            elif ev.get("type") == "message_stop":
                return
        return
    raise RuntimeError(f"AI not reachable: {last}")


def answer_stream(question, ctx, history=None, use_ai=True):
    """Yields ("text", chunk) ... then ("done", source). Never empty."""
    if use_ai and ai_enabled():
        got = False
        try:
            for chunk in stream_ai(question, ctx, history):
                if chunk:
                    got = True
                    yield "text", chunk
            if got:
                yield "done", "ai"
                return
        except Exception as e:
            print(f"[assistant] AI failed: {type(e).__name__}: {e}")
            if got:
                yield "text", "\n\n(The answer was cut short. Please ask again.)"
                yield "done", "ai"
                return
        yield "text", fallback_text(question, ctx, busy=True)
        yield "done", "built-in"
        return
    yield "text", fallback_text(question, ctx)
    yield "done", "built-in"


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
def _read_request():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, "Invalid request."
    q = _norm(data.get("question"))[:MAX_QUESTION]
    if not q:
        return None, "Type a question first."
    hist = data.get("history") if isinstance(data.get("history"), list) else []
    page = str(data.get("page") or "")[:80]
    sid = data.get("signal_id")
    try:
        sid = int(sid) if sid not in (None, "") else None
    except (TypeError, ValueError):
        sid = None
    sym = str(data.get("symbol") or "").upper()[:20] or None
    return {"q": q, "history": hist[-MAX_HISTORY:], "page": page, "signal_id": sid, "symbol": sym}, None


@assistant_bp.route("/api/assistant/chat", methods=["POST"])
def chat():
    req, err = _read_request()
    if err:
        return jsonify({"ok": False, "error": err}), 400
    key, per_hour = _limit_key()
    use_ai = ai_enabled()
    if use_ai:
        ok, wait = _allowed(key, per_hour)
        if not ok:
            use_ai = False
    ctx = build_context(req["page"], req["signal_id"], req["symbol"])

    def gen():
        if not use_ai and ai_enabled():
            yield "data: " + json.dumps({"notice": "You reached the hourly limit for AI answers, so this answer comes from the site's help."}) + "\n\n"
        for kind, val in answer_stream(req["q"], ctx, req["history"], use_ai=use_ai):
            if kind == "text":
                yield "data: " + json.dumps({"t": val}) + "\n\n"
            else:
                yield "data: " + json.dumps({"done": True, "source": val, "disclaimer": DISCLAIMER}) + "\n\n"

    resp = Response(stream_with_context(gen()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@assistant_bp.route("/api/assistant/ask", methods=["POST"])
def ask():
    req, err = _read_request()
    if err:
        return jsonify({"ok": False, "error": err}), 400
    key, per_hour = _limit_key()
    use_ai = ai_enabled() and _allowed(key, per_hour)[0]
    ctx = build_context(req["page"], req["signal_id"], req["symbol"])
    parts, source = [], "built-in"
    for kind, val in answer_stream(req["q"], ctx, req["history"], use_ai=use_ai):
        if kind == "text":
            parts.append(val)
        else:
            source = val
    return jsonify({"ok": True, "reply": "".join(parts).strip(), "source": source, "disclaimer": DISCLAIMER})


@assistant_bp.route("/api/assistant/status", methods=["GET"])
def status():
    return jsonify({"ok": True, "ai": ai_enabled(), "disclaimer": DISCLAIMER})


def init_site_assistant(app=None, *, context=None):
    if context is not None:
        _hooks["context"] = context
