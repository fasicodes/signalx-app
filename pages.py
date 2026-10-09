"""
Info pages - Signals FM
=======================

Public pages (logged-in users see them inside the app menu, visitors with the public navigation):
    GET  /about           who we are, how the engine works, what we promise
    GET  /faq             questions and answers + the full glossary (/faq#g-<term>)
    GET  /pricing         plans (free during the beta)
    GET  /contact         contact form, email and social links
    GET  /glossary.json   the "?" explanations used all over the site (glossary.py)
    POST /api/contact     contact form -> stored in `contact_messages` and emailed to the support address
"""
import hashlib
import os
import re
import threading
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request, session

from db import get_db_connection
import glossary

pages_bp = Blueprint("pages", __name__)

DEFAULT_SUPPORT_EMAIL = "signalfm01@gmail.com"
SOCIAL = [
    ("TikTok", "@signalfm1", "https://www.tiktok.com/@signalfm1"),
    ("Instagram", "@signalfm01", "https://www.instagram.com/signalfm01/"),
    ("Facebook", "Signals FM", "https://www.facebook.com/profile.php?id=61593016040987"),
    ("LinkedIn", "Signals FM", "https://www.linkedin.com/in/signal-fm-459324427/"),
]
CONTACT_TOPICS = ["General question", "Signals", "Alerts and Telegram", "Demo trading", "Auto-trade bot",
                  "Account and login", "Business or partnership", "Report a problem"]
EMAIL_RE = re.compile(r"^[^@\s<>\"']{1,64}@[^@\s<>\"']{1,190}\.[A-Za-z]{2,24}$")
_hooks = {"send_email": None}

# Every feature, as shown on the pricing page. Edit here when plans change.
PLAN_FEATURES = [
    ("Signals on 30 coins", "Long or Short with entry, stop loss and target, every 4 hours"),
    ("Live signals board", "Every coin at a glance, active signals with live progress"),
    ("Signal alerts", "In the app, by email and by Telegram"),
    ("Pro terminal", "Advanced chart, liquidity scanner and 27-channel analysis"),
    ("Demo trading", "Practice with virtual money and live prices"),
    ("Auto-trade bot", "Places signals on your own Binance account, Demo or Live"),
    ("Track record and trading tools", "Every signal's result, plus position-size and risk calculators"),
    ("Forex and gold charts", "Live chart and a plain-language market brief"),
]

FAQ = [
    ("start", "Getting started", [
        ("What is Signals FM?",
         "<p>Signals FM gives crypto trading signals from a tested statistical model, plus the tools around them: a live signals "
         "board, alerts, a Pro terminal with advanced charts and order-book analysis, demo trading with virtual money and an "
         "optional auto-trade bot for Binance.</p>"),
        ("How do I start?",
         "<ul><li>Create a free account.</li><li>Read the short welcome guide (Help &rarr; Getting started).</li>"
         "<li>Open the <a href=\"/signals\">Signals board</a> to see which coins have an active signal.</li>"
         "<li>Practise in <a href=\"/demo-trading\">Demo trading</a> before you use real money.</li>"
         "<li>Turn on <a href=\"/signals#alerts\">signal alerts</a> so you hear about new signals.</li></ul>"),
        ("Is it free?",
         "<p>Yes. Every feature is free while Signals FM is in beta, and no card is needed. Paid plans will be announced in "
         "advance, and nothing changes without notice. See <a href=\"/pricing\">Pricing</a>.</p>"),
        ("Do I need trading experience?",
         "<p>No, but you do need to understand risk. Start with the welcome guide and the glossary below, and use Demo trading "
         "first. Tap any <b>?</b> next to a word on the site to see what it means.</p>"),
    ]),
    ("signals", "Signals", [
        ("How are signals made?",
         "<p>The signal engine reads <b>closed 4-hour candles</b> for 30 coins. For each one it looks at the coin's own price action, "
         "its daily trend, Bitcoin and the market of 16 large coins (82 measurements in all), and a machine-learning model estimates the chance "
         "that a trade reaches its target before its stop. Only setups above a high line, roughly the top 3%, become signals.</p>"),
        ("Which coins have signals?",
         "<p>All 30 crypto coins on the <a href=\"/signals\">Signals board</a>, against USDT. The model was trained and tested on 16 "
         "large coins: ADA, ATOM, AVAX, BCH, BNB, BTC, DOGE, DOT, ETH, LINK, LTC, NEAR, SOL, TRX, XLM and XRP. The other coins use the "
         "same model and rules but were not part of that test, so their results may differ; the signal card tells you when a coin "
         "was not tested. Forex pairs and gold show the chart and a market brief, but no signal.</p>"),
        ("How often do new signals come?",
         "<p>In the 15-month test there were 437 signals across the 16 tested coins, about one a day on average. Some days have none and "
         "some have several. A signal can only start right after a 4-hour candle closes.</p>"),
        ("Why does it say Wait most of the time?",
         "<p>Because the engine only trades its strongest setups. Wait means no trade, which keeps you out of weak setups. The "
         "<a href=\"/signals\">Signals board</a> shows which coins have an active signal right now and which are closest to one.</p>"),
        ("How are the entry, stop loss and target set?",
         "<p>The entry is the close of the 4-hour candle that gave the signal. The stop loss is 3 times the coin's average 4-hour "
         "move (ATR) away from the entry, and the target is 1.5 times. That is why the target is closer than the stop, and why a "
         "loss is about twice the size of a win.</p>"),
        ("How long does a signal last?",
         "<p>Until the price reaches the target or the stop, or for at most 48 four-hour candles (8 days). After 8 days it closes "
         "at that candle's close.</p>"),
        ("What does \"Setup forming\" mean?",
         "<p>The model is close to a signal but still below the line. <b>It is not a trade.</b> We tested this band as signals and "
         "it lost money after fees, so treat it only as a heads-up to watch the next 4-hour close.</p>"),
        ("Can I still take a signal that started hours ago?",
         "<p>You can, but the price has moved since the entry, so your risk and reward are different from the signal's. The signal "
         "card shows how far the price has moved and how close it is to the target or stop. If the price is already most of the "
         "way to the target, little reward is left for the same risk.</p>"),
        ("Are there signals for forex or gold?",
         "<p>Not yet. For forex pairs and gold you get the live chart and a plain-language market brief. Signals are made for "
         "crypto only for now.</p>"),
    ]),
    ("results", "Results and risk", [
        ("How accurate are the signals?",
         "<p>The model was trained on January 2022 to June 2025 and then tested once on July 2025 to October 2026, data it had "
         "never seen. In that test: <b>437 signals, 69.8% won, +0.052R average per signal after fees</b>, profit factor 1.19, a "
         "worst drawdown of &minus;19.6R, and 5 of the 13 full months lost money. Past results do not guarantee future results.</p>"),
        ("If about 70% win, why is the profit small?",
         "<p>Because each loss (&minus;1R) is about twice the size of each win (+0.5R). Seventy wins of +0.5R and thirty losses of "
         "&minus;1R come to only about +0.05R per signal, and a few extra losses can turn a month negative.</p>"),
        ("How much should I risk on each signal?",
         "<p>Keep it small and the same every time, for example 1% of your account per signal; that amount is 1R. Losing streaks "
         "happen: in testing the worst drop was about 20R, which is about 20% of an account at 1% per signal. Only trade money "
         "you can afford to lose. The <a href=\"/tools\">position size calculator</a> works out the size for you.</p>"),
        ("Where can I see the live results?",
         "<p>On the <a href=\"/track-record\">Track record</a> page. It records every signal the engine gives on a fixed list of "
         "coins, live, with its result in R after fees, next to a 365-day backtest that is refreshed every week.</p>"),
        ("Is this financial advice?",
         "<p>No. Signals FM gives algorithmic analysis for education. You decide every trade and you are responsible for it. "
         "Please read the <a href=\"/risk-disclosure\">risk disclosure</a>.</p>"),
    ]),
    ("alerts", "Alerts", [
        ("How do I get alerts for new signals?",
         "<p>Open the <a href=\"/signals#alerts\">Signals board</a> and go to Signal alerts. Turn alerts on, choose all coins or "
         "only some, and pick how you want them: the bell in the app, email, or Telegram.</p>"),
        ("How do I connect Telegram?",
         "<p>In Signal alerts, press <b>Connect Telegram</b>, then <b>Open Telegram</b>, and press <b>Start</b> in the chat with "
         "the Signals FM bot. New signals then arrive in that chat. In the chat, send /signals to see the active signals or "
         "/stop to pause alerts; you can also press Disconnect on the website.</p>"),
        ("How many emails will I get?",
         "<p>One email when new signals start, at most 12 a day. Every email has a one-click link to stop them.</p>"),
        ("Why didn't I get an alert?",
         "<p>Alerts go out only for new signals on the coins you chose, right after a 4-hour candle closes. Check that alerts are "
         "on and that the coin is in your list. For email, also check your spam folder.</p>"),
    ]),
    ("trading", "Demo trading and the auto-trade bot", [
        ("What is Demo trading?",
         "<p>A practice exchange with virtual USDT and live prices: spot and futures, limit and stop orders, take profit and stop "
         "loss, fees, funding and liquidation. Nothing there uses real money, and you can reset it any time.</p>"),
        ("What does the auto-trade bot do?",
         "<p>It can place the engine's new signals as orders on your own Binance account, on the Demo account or Live, with your "
         "own risk settings. Every bot trade has a stop loss and a target. Start on Demo and use small sizes.</p>"),
        ("Are my exchange API keys safe?",
         "<p>Keys are encrypted on the server and never sent back to your browser. Live keys with withdrawal permission are "
         "refused, so the bot can trade but cannot move money out. Disconnecting deletes the keys.</p>"),
        ("Why can't the bot connect to Binance?",
         "<p>Binance is not available in every country. If Binance does not serve your country, or the API key's permissions are "
         "wrong, the connection fails. The auto-trade page shows the exact error.</p>"),
    ]),
    ("account", "Account and contact", [
        ("How do I contact you?",
         "<p>Use the <a href=\"/contact\">Contact page</a>. We aim to reply within 24 to 48 hours, Monday to Friday.</p>"),
        ("What data do you keep, and can I delete it?",
         "<p>The <a href=\"/privacy\">privacy policy</a> explains what is stored and why. You can ask us through the "
         "<a href=\"/contact\">Contact page</a> to export or delete your account and all its data.</p>"),
    ]),
]


def _utcnow():
    return datetime.utcnow().replace(microsecond=0)


def support_email():
    for v in (os.environ.get("SUPPORT_EMAIL"), os.environ.get("MAIL_USERNAME")):
        v = (v or "").strip()
        if v and EMAIL_RE.match(v):
            return v
    return DEFAULT_SUPPORT_EMAIL


def public_email():
    """Address shown on the contact page: SUPPORT_EMAIL if set, else the published Signals FM address."""
    v = (os.environ.get("SUPPORT_EMAIL") or "").strip()
    return v if v and EMAIL_RE.match(v) else DEFAULT_SUPPORT_EMAIL


class _Cursor:
    def __enter__(self):
        self.conn = get_db_connection()
        self.cur = self.conn.cursor()
        return self.cur

    def __exit__(self, *a):
        try:
            self.cur.close()
        finally:
            self.conn.close()
        return False


def init_tables():
    with _Cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_messages (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NULL,
                name VARCHAR(80) NOT NULL,
                email VARCHAR(254) NOT NULL,
                topic VARCHAR(60) NOT NULL,
                message TEXT NOT NULL,
                ip_hash VARCHAR(16) NULL,
                emailed TINYINT(1) NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL
            )
            """
        )


def init_pages(app=None, *, send_email=None):
    if send_email:
        _hooks["send_email"] = send_email
    try:
        init_tables()
    except Exception as e:
        print(f"[pages] WARNING: could not create the contact table: {e}")


# ===========================================================================
# pages
# ===========================================================================
def _test_stats():
    try:
        import signal_engine
        return signal_engine.test_stats() or {}
    except Exception:
        return {}


@pages_bp.route("/about", methods=["GET"])
def about_page():
    return render_template("about.html", active="about", stats=_test_stats())


@pages_bp.route("/faq", methods=["GET"])
def faq_page():
    return render_template("faq.html", active="faq", faq=FAQ, glossary_groups=glossary.groups())


@pages_bp.route("/pricing", methods=["GET"])
def pricing_page():
    return render_template("pricing.html", active="pricing", features=PLAN_FEATURES)


@pages_bp.route("/contact", methods=["GET"])
def contact_page():
    return render_template("contact.html", active="contact", topics=CONTACT_TOPICS, social=SOCIAL,
                           contact_email=public_email(), prefill_email=session.get("email") or "")


@pages_bp.route("/glossary.json", methods=["GET"])
def glossary_json():
    resp = jsonify({"terms": glossary.GLOSSARY})
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ===========================================================================
# contact form
# ===========================================================================
def _clean(v, limit, multiline=False):
    """Removes control characters; single-line fields also lose line breaks (they end up in email headers)."""
    v = str(v or "")
    if multiline:
        v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v.replace("\r\n", "\n"))
    else:
        v = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", v))
    return v.strip()[:limit]


def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _contact_html(m):
    body = _esc(m["message"]).replace("\n", "<br>")
    who = f"user #{m['user_id']}" if m.get("user_id") else "a visitor (not logged in)"
    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;color:#0d1c15;max-width:640px">
      <h2 style="margin:0 0 8px">New message from the Signals FM contact form</h2>
      <p style="margin:0 0 4px"><b>From:</b> {_esc(m['name'])} &lt;{_esc(m['email'])}&gt; ({who})</p>
      <p style="margin:0 0 14px"><b>Topic:</b> {_esc(m['topic'])}</p>
      <div style="border:1px solid #dbe6e0;border-radius:8px;padding:12px 14px;line-height:1.6">{body}</div>
      <p style="color:#6f8279;font-size:12px;margin-top:14px">Reply to this email to answer {_esc(m['name'])} directly.</p>
    </div>"""


@pages_bp.route("/api/contact", methods=["POST"])
def api_contact():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Invalid request."}), 400
    if _clean(data.get("company"), 200):          # hidden field only bots fill in
        return jsonify({"ok": True, "message": "Thanks, your message was sent."})
    name = _clean(data.get("name"), 80)
    email = _clean(data.get("email"), 254)
    topic = _clean(data.get("topic"), 60)
    message = _clean(data.get("message"), 4000, multiline=True)
    if len(name) < 2:
        return jsonify({"ok": False, "error": "Please enter your name."}), 400
    if not EMAIL_RE.match(email):
        return jsonify({"ok": False, "error": "Please enter a valid email address, so we can reply."}), 400
    if topic not in CONTACT_TOPICS:
        topic = CONTACT_TOPICS[0]
    if len(message) < 10:
        return jsonify({"ok": False, "error": "Please write a little more (at least 10 characters)."}), 400
    if len(message) > 3000:
        return jsonify({"ok": False, "error": "Please keep the message under 3,000 characters."}), 400
    ip = (request.remote_addr or "")
    m = {"user_id": session.get("user_id"), "name": name, "email": email, "topic": topic, "message": message,
         "ip_hash": hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else None}
    stored = False
    try:
        with _Cursor() as cur:
            cur.execute("""INSERT INTO contact_messages (user_id, name, email, topic, message, ip_hash, emailed, created_at)
                           VALUES (%s,%s,%s,%s,%s,%s,0,%s)""",
                        (m["user_id"], name, email, topic, message, m["ip_hash"], _utcnow()))
            m["id"] = cur.lastrowid if hasattr(cur, "lastrowid") else None
        stored = True
    except Exception as e:
        print(f"[pages] could not store a contact message: {e}")
    send = _hooks.get("send_email")
    sent = False
    if send:
        try:
            sent = bool(send(support_email(), f"[Signals FM] {topic}: message from {name}", _contact_html(m), reply_to=email))
        except TypeError:   # an older mailer without reply_to
            sent = bool(send(support_email(), f"[Signals FM] {topic}: message from {name} <{email}>", _contact_html(m)))
        except Exception as e:
            print(f"[pages] contact email failed: {e}")
    if sent and stored and m.get("id"):
        try:
            with _Cursor() as cur:
                cur.execute("UPDATE contact_messages SET emailed=1 WHERE id=%s", (m["id"],))
        except Exception:
            pass
    if not sent and not stored:
        return jsonify({"ok": False, "error": f"Your message could not be sent right now. Please email us at {public_email()}."}), 503
    return jsonify({"ok": True, "message": "Thanks, your message was sent. We aim to reply within 24 to 48 hours, Monday to Friday."})
