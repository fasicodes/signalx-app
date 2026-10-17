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
    ("Signals on 62 coins", "Long or Short with entry, stop loss and target, every 4 hours"),
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
         "<p>The signal engine reads <b>closed 4-hour candles</b>. For each coin it looks at the coin's own price action, its daily "
         "trend, Bitcoin and the market of 16 large coins (82 measurements in all), and a machine-learning model estimates the chance "
         "that a trade reaches its first target before its stop. All 62 crypto coins are read together at every close: setups in the "
         "model's top 3% become <b>strong</b> signals, and if no coin has had a signal for 24 hours, the best setup (with at least an "
         "average score) becomes a <b>standard</b> signal. At most 5 new signals start on one candle (the highest scores), because "
         "many coins at once usually means one market move.</p>"),
        ("Which coins have signals?",
         "<p>All 62 crypto coins in the list, against USDT. The model was trained on 21 of them and then tested on 53 coins over a "
         "year it had never seen, 32 of which it had never seen at all; the results held up on those as well. The newest coins "
         "(such as HYPE, TAO, ONDO and ASTER) have a short history, so check their results on the "
         "<a href=\"/track-record\">Track record</a>. Forex pairs and gold have their own signal streams, which are marked untested "
         "(see the forex question below).</p>"),
        ("How often do new signals come?",
         "<p>In the 12-month test there were 735 signals across 53 coins, about 2 a day, a new signal on 369 of 374 days, and the "
         "longest wait between two new signals was 2.3 days. A signal can only start right after a 4-hour candle closes.</p>"),
        ("Why does a coin say Wait?",
         "<p>Each coin has at most one signal at a time, and it only gets one when its setup is good enough, so a single coin often "
         "says Wait. Across all the coins there is almost always at least one signal running. The <a href=\"/signals\">Signals "
         "board</a> shows which coins have an active signal right now and which are closest to one.</p>"),
        ("How are the entry, stop loss and targets set?",
         "<p>The entry is the close of the 4-hour candle that gave the signal. The stop loss is 2 times the coin's average 4-hour "
         "move (ATR) away from the entry; that distance is 1R. There are three targets: <b>TP1 at 0.5R, TP2 at 1R</b> (as far as the "
         "stop) and <b>TP3 at 2R</b>. Close a third of the position at each. After TP1 move the stop to the entry, after TP2 to TP1, "
         "so a signal that reached TP1 can no longer turn into a loss (apart from a price gap).</p>"),
        ("How long does a signal last?",
         "<p>Until the stop (or the moved stop) is hit or TP3 is reached, or for at most 54 four-hour candles (9 days). After 9 "
         "days, whatever is left closes at that candle's close.</p>"),
        ("What does \"Setup forming\" mean?",
         "<p>The model is close to a strong signal but still below the line. <b>It is not a trade.</b> Treat it only as a heads-up "
         "to watch the next 4-hour close.</p>"),
        ("Can I still take a signal that started hours ago?",
         "<p>You can, but the price has moved since the entry, so your risk and reward are different from the signal's. The signal "
         "card shows how far the price has moved and how close it is to the next target or the stop. If the price is already near "
         "TP1, little reward is left for the same risk.</p>"),
        ("Are there signals for forex or gold?",
         "<p>Yes, but they are marked <b>untested</b>. We built and tested a separate model for forex pairs and gold, and after "
         "spreads it did not make money: the best version reached 70.8% wins (TP1 or better) on data it had never seen, but the "
         "average result after costs was slightly below zero (-0.072 R per trade). The forex, gold and 15-minute signals therefore "
         "come from simple rules that have not passed our test, and every one of them carries an \"Untested\" label on the "
         "Signals page and in alerts. When we replayed those rules on history (forex 2017 to 2020, crypto 15m 2023 to 2025) they lost "
         "money on average: about -0.08 to -0.22 R per signal after costs, in every year. Their real results are tracked like every "
         "other signal, and live auto-trading accounts do not copy them (demo accounts do). Treat them with extra care.</p>"),
    ]),
    ("results", "Results and risk", [
        ("How accurate are the signals?",
         "<p>The model was trained on July 2020 to September 2025 and then tested once on October 2025 to October 2026, data it "
         "had never seen, on 53 coins (32 of them never used in training). In that test: <b>735 signals, 73.6% closed in profit "
         "(73.9% reached TP1), +0.105R average per signal after fees</b>, profit factor 1.40, a worst drawdown of &minus;23.9R, "
         "and 3 of the 12 full months lost money. Coins the model trained on did better (+0.15R) than new ones (+0.08R). A "
         "walk-forward test on January 2023 to October 2026 (21 coins, re-trained every 6 months, tested on the next 6) gave 2,495 "
         "signals, 72.7% won, +0.10R average, and every year was above zero. Past results do not guarantee future results.</p>"
         "<p>Read the win rate with care: TP1 is a small target (half the stop distance), so about 65% of all trades of this kind "
         "reach it even without any model. The model adds roughly 8 to 9 points on top of that, and the average result after fees "
         "(+0.10R per signal) is the better measure of what it earns.</p>"
         "<p>Strong and standard signals are not equal. In the 12-month test the strong ones (399 signals) won 77.2% with "
         "+0.158R on average; the standard ones (336 signals, given when no coin has had a signal for 24 hours) won 69.3% with only "
         "+0.042R, close to break-even after fees. Treat a standard signal as lower conviction: use a smaller size or skip it.</p>"),
        ("If about 3 in 4 signals win, why is the profit per signal modest?",
         "<p>Because many wins are small. A signal that reaches TP1 and then comes back to the entry makes about +0.17R, while a "
         "full loss is &minus;1R. The profit comes from the signals that go on to TP2 (+0.67R) and TP3 (+1.17R). In the test the "
         "average win was +0.50R and the average loss &minus;1.0R.</p>"),
        ("How much should I risk on each signal?",
         "<p>Keep it small and the same every time; that amount is 1R. Losing streaks happen: the worst drop was 23.9R in the "
         "12-month test and 41.5R in the longer walk-forward test. At 1% per signal, 41.5R is about 41% of an account (simple sum, "
         "before compounding), so plan for a drop at least that deep. There are often four or more signals open at once, so 0.5% per "
         "signal (or a limit on open trades) is a sensible start. Only trade money you can afford to lose. The <a href=\"/tools\">position size "
         "calculator</a> works out the size for you.</p>"),
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
