"""Site-wide assistant (site_assistant.py): streaming, fallback, history, context, rate limit.
No network: the Claude API is replaced by a fake. Run from the project root:
    python tests/test_site_assistant.py
"""
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.pop("ANTHROPIC_API_KEY", None)
import site_assistant as sa  # noqa: E402

FAILS = []


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


from flask import Flask  # noqa: E402

app = Flask(__name__)
app.secret_key = "t"
app.register_blueprint(sa.assistant_bp)
SIG = {"id": 7, "symbol": "EUR/USD", "timeframe": "1h", "side": "LONG", "grade": "A", "entry": 1.081, "stop_loss": 1.079,
       "targets": [1.082, 1.083, 1.085], "status": "ACTIVE", "signal_at": "2026-10-14T12:00:00Z", "hold_minutes": 600}
CALLS = []


def ctx(page=None, signal_id=None, symbol=None):
    from flask import session
    out = {"now": "2026-10-14T12:30:00Z", "page": page, "symbol": symbol,
           "next_events": [{"time": "2026-10-14T12:30:00Z", "currency": "USD", "title": "CPI m/m", "forecast": "0.3", "previous": "0.2"}],
           "headlines": [{"title": "Dollar firm before CPI", "source": "Reuters", "published_at": "2026-10-14T11:00:00Z"}],
           "stream_stats": [{"market": "forex", "timeframe": "1h", "windows": {"all": {"closed": 40, "win_rate": 72.5, "avg_r": 0.08}},
                             "test": {"signals": 900, "win_rate": 71.2, "avg_net_r": 0.05, "period": "2021-01-01 to 2022-03-05"}}]}
    if session.get("user_id"):
        out["active_signals"] = [SIG]
        if signal_id == 7:
            out["viewing_signal"] = SIG
    return out


sa.init_site_assistant(app, context=ctx)
cl = app.test_client()


def sse(resp):
    text, done, notes = "", None, []
    for block in resp.get_data(as_text=True).split("\n\n"):
        block = block.strip()
        if not block.startswith("data:"):
            continue
        ev = json.loads(block[5:])
        text += ev.get("t", "")
        if ev.get("done"):
            done = ev
        if ev.get("notice"):
            notes.append(ev["notice"])
    return text, done, notes


# ---- no AI key: built-in answers, never empty
r = cl.post("/api/assistant/chat", json={"question": "What is a stop loss?"})
t, d, _ = sse(r)
check(r.status_code == 200 and r.mimetype == "text/event-stream" and t.startswith("Stop loss:") and d["source"] == "built-in",
      "no key: streamed built-in answer from the glossary")
check("not financial advice" in d["disclaimer"].lower(), "every answer carries the disclaimer")
r = cl.post("/api/assistant/chat", json={"question": "Write me a poem about the sea"})
t, d, _ = sse(r)
check(len(t) > 40 and "I can explain" in t, "unknown question without AI: a helpful pointer, never empty")
check(cl.post("/api/assistant/chat", json={"question": "   "}).status_code == 400, "empty question rejected politely")
check(cl.post("/api/assistant/chat", data="x").status_code == 400, "invalid body rejected")
d = cl.post("/api/assistant/ask", json={"question": "how do I use a signal"}).get_json()
check(d["ok"] and "close a third" in d["reply"], "JSON endpoint gives the same answer")
with cl.session_transaction() as s:
    s["user_id"] = 1
t, d, _ = sse(cl.post("/api/assistant/chat", json={"question": "Explain this signal", "signal_id": 7}))
check("EUR/USD 1h LONG grade A" in t, "built-in answer explains the signal on screen")
t, d, _ = sse(cl.post("/api/assistant/chat", json={"question": "what is the next big news event?"}))
check("CPI m/m" in t, "built-in answer reads the calendar")


# ---- with an AI key: streaming from the fake API
class FakeStream:
    def __init__(self, code=200, chunks=("Hello", " there"), fail_after=None):
        self.status_code = code
        self.chunks = chunks
        self.fail_after = fail_after
        self.text = "err"

    def iter_lines(self, decode_unicode=True):
        yield 'event: message_start'
        yield 'data: {"type": "message_start"}'
        for k, c in enumerate(self.chunks):
            if self.fail_after is not None and k >= self.fail_after:
                raise ConnectionError("dropped")
            yield "data: " + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": c}})
        yield 'data: {"type": "message_stop"}'


PLAN = []


def fake_post(url, headers=None, json=None, timeout=None, stream=None):
    CALLS.append({"url": url, "headers": headers, "body": json, "stream": stream})
    return PLAN.pop(0) if PLAN else FakeStream()


sa.requests = types.SimpleNamespace(post=fake_post)
sa.time.sleep = lambda s: None
os.environ["ANTHROPIC_API_KEY"] = "sk-test"
hist = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "Hello!"}] * 8
r = cl.post("/api/assistant/chat", json={"question": "Is this a good entry?", "history": hist, "signal_id": 7, "page": "/signals"})
t, d, _ = sse(r)
body = CALLS[-1]["body"]
check(t == "Hello there" and d["source"] == "ai", "AI answer streamed chunk by chunk")
check(body["stream"] is True and body["model"] == sa.DEFAULT_MODEL and CALLS[-1]["headers"]["x-api-key"] == "sk-test",
      "Claude API called with streaming, the configured model and the key (server side)")
check("Always write in English" in body["system"], "the assistant answers in English only")
check("EUR/USD 1h LONG grade A" in body["system"] and "CPI m/m" in body["system"] and "Dollar firm before CPI" in body["system"]
      and "win rate 72.5%" in body["system"], "live context: viewed signal, calendar, headlines and stream results")
check(len(body["messages"]) <= sa.MAX_HISTORY + 1 and body["messages"][-1]["role"] == "user"
      and body["messages"][-1]["content"].endswith("Is this a good entry?") and body["messages"][0]["role"] == "user",
      "conversation history is trimmed and alternates correctly")
PLAN[:] = [FakeStream(code=529), FakeStream(chunks=("Retried ok",))]
t, d, _ = sse(cl.post("/api/assistant/chat", json={"question": "Explain RSI"}))
check(t == "Retried ok", "overloaded API: one retry, then the answer")
PLAN[:] = [FakeStream(code=500), FakeStream(code=500)]
t, d, _ = sse(cl.post("/api/assistant/chat", json={"question": "What is a stop loss?"}))
check(d["source"] == "built-in" and "Stop loss:" in t and "busy" in t, "AI down: a friendly built-in answer, no raw error")
PLAN[:] = [FakeStream(chunks=("Part one", "part two"), fail_after=1)]
t, d, _ = sse(cl.post("/api/assistant/chat", json={"question": "Explain MACD"}))
check(t.startswith("Part one") and "cut short" in t, "a dropped stream keeps the text and says so")


def boom(*a, **k):
    raise TimeoutError("timed out")


sa.requests = types.SimpleNamespace(post=boom)
d = cl.post("/api/assistant/ask", json={"question": "What is leverage?"}).get_json()
check(d["ok"] and d["reply"] and d["source"] == "built-in", "timeout: JSON endpoint still answers")
sa.requests = types.SimpleNamespace(post=fake_post)

# ---- visitors: no member data in the context, a lower hourly limit
with cl.session_transaction() as s:
    s.clear()
sa._usage.clear()
os.environ["ASSISTANT_GUEST_PER_HOUR"] = "2"
for i in range(2):
    sse(cl.post("/api/assistant/chat", json={"question": f"Explain ATR {i}", "signal_id": 7}))
body = CALLS[-1]["body"]
check("EUR/USD 1h LONG" not in body["system"], "visitors do not get member signal levels in the context")
n = len(CALLS)
t, d, notes = sse(cl.post("/api/assistant/chat", json={"question": "What is a stop loss?"}))
check(len(CALLS) == n and d["source"] == "built-in" and notes and "hourly limit" in notes[0],
      "hourly limit reached: built-in answer with a notice, no API call")
check(cl.get("/api/assistant/status").get_json()["ai"] is True, "status endpoint")

# ---- a visitor cannot dodge the limit by changing the X-Forwarded-For header
n = len(CALLS)
for i in range(3):
    t, d, notes = sse(cl.post("/api/assistant/chat", json={"question": f"Explain RSI {i}"},
                              headers={"X-Forwarded-For": f"10.9.8.{i}"}))
check(len(CALLS) == n, "changing X-Forwarded-For does not give a visitor a fresh hourly limit")

print()
print(f"{len(FAILS)} failed" if FAILS else "ALL PASSED")
sys.exit(1 if FAILS else 0)
