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
    ("Signals on 102 coins", "Long or Short with entry, stop loss and targets: checked every 4 hours, every hour and every 15 minutes"),
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
         "<p>Two machine-learning models read <b>closed candles</b> of 102 crypto coins. For each coin they look at the coin's own "
         "price action, its daily trend, Bitcoin and the whole market (82 measurements), and estimate the chance that a trade "
         "reaches its first target before its stop. Only the best setups become signals.</p>"
         "<ul><li><b>4-hour stream</b>: read at every 4-hour close. Setups in the model's top 3% become <b>strong</b> signals; if no "
         "coin has had a signal for 24 hours, the best setup (with at least an average score) becomes a <b>standard</b> signal. At "
         "most 5 new signals start on one candle.</li>"
         "<li><b>1-hour stream</b>: a second 4-hour model, read at every hourly close on the 4-hour candle that ends at that hour, "
         "so a setup can be found up to three hours earlier. A coin gets a signal when its score is in the model's top 2%. At most "
         "3 new signals start in one hour.</li>"
         "<li><b>15-minute stream</b>: the same model as the 1-hour stream, read at every 15-minute close on the 4-hour candle "
         "that ends then. It takes only the model's strongest setups (its top 0.5%), so it gives fewer signals. At most 1 new "
         "signal starts on one 15-minute candle.</li></ul>"),
        ("Which coins have signals?",
         "<p>All 102 crypto coins in the list, against USDT, on all three streams. The models were tested on the 94 coins that have "
         "enough history; the newest coins have a short history, so check their results on the "
         "<a href=\"/track-record\">Track record</a> and the Signals page.</p>"),
        ("How often do new signals come?",
         "<p>The 4-hour stream gave about 2 signals a day in its test, and the longest wait between two new signals was under 2 "
         "days. The 1-hour stream gave about 2 a day on average too, but in bursts: many signals in one market move, then nothing "
         "for days or even weeks (it had a signal on about 1 day in 8). We tested giving it a signal every day; those extra "
         "signals lost money, so it stays quiet when there is no strong setup. The 15-minute stream takes only the strongest "
         "setups: about 1 signal every 2 days in its test, also in bursts. About 6 in 10 of its signals are the same trade as a "
         "1-hour signal (same coin and side within 4 hours).</p>"),
        ("Why does a coin say Wait?",
         "<p>Each coin has at most one signal at a time on each stream, and it only gets one when its setup is good enough, so a "
         "single coin often says Wait. The <a href=\"/signals\">Signals board</a> shows which coins have an active signal right "
         "now and which are closest to one.</p>"),
        ("How are the entry, stop loss and targets set?",
         "<p>The entry is the close of the candle that gave the signal. The stop loss is 2 times the coin's average 4-hour move "
         "(ATR) away from the entry; that distance is 1R.</p>"
         "<ul><li><b>4-hour signals</b> have three targets: TP1 at 0.5R, TP2 at 1R and TP3 at 2R. Close a third of the position at "
         "each. After TP1 move the stop to the entry, after TP2 to TP1.</li>"
         "<li><b>1-hour and 15-minute signals</b> have one target at 0.5R: close the whole position there; the stop does not "
         "move.</li></ul>"
         "<p>The first target is half as far as the stop. That is why about 3 in 4 signals reach it, and also why one full loss "
         "costs as much as two first-target wins. Each 4-hour signal also shows an optional <b>patient entry</b> for the "
         "15-minute chart: a limit order a quarter of the ATR better than the entry, valid for 4 hours; about 4 in 10 signals "
         "never come back to it.</p>"),
        ("How long does a signal last?",
         "<p>Until the stop or its last target is hit, or for at most 9 days. After 9 days the trade closes at that candle's close. "
         "In the tests half of the 1-hour signals were finished within about 14 hours, and half of the 15-minute signals within "
         "about 16 hours.</p>"),
        ("What does \"Setup forming\" mean?",
         "<p>The model is close to a signal but still below the line. <b>It is not a trade.</b> Treat it only as a heads-up "
         "to watch the next candle close.</p>"),
        ("Can I still take a signal that started hours ago?",
         "<p>You can, but the price has moved since the entry, so your risk and reward are different from the signal's. The signal "
         "card shows how far the price has moved and how close it is to the next target or the stop. If the price is already near "
         "the target, little reward is left for the same risk.</p>"),
        ("Are there signals for forex, gold or 15-minute charts?",
         "<p><b>15-minute crypto: yes</b>, since November 2026. A separate 15-minute model passed its validation years but not "
         "the test on data it had never seen (69% wins, about zero profit after costs), so it never went live. Then we read the "
         "4-hour model of the 1-hour stream at every 15-minute close instead and took only its strongest setups (a choice made "
         "on the validation years): on the same unseen year 82.9% of its 199 signals reached the target, +0.21R per signal "
         "after costs.</p>"
         "<p><b>Forex and gold: no.</b> In November 2026 we trained new models for forex and gold (15-minute, 1-hour and 4-hour) "
         "and tested them the same way as the others. They did not even reach the target on their validation years (about 66 "
         "to 71% wins and about zero profit after spreads). Publishing them would mean more stop-losses, so they stay off. The "
         "forex and gold charts and market briefs are still there, and the <a href=\"/signals\">Signals page</a> says what was "
         "tested. A stream comes back only when a model for it passes the same test.</p>"),
    ]),
    ("results", "Results and risk", [
        ("How accurate are the signals?",
         "<p>Each model was tested on October 2025 to November 2026, data it had never seen, on 94 coins, with real trading costs "
         "(0.30% per round trip plus funding) and entries one minute after the candle close:</p>"
         "<ul><li><b>4-hour stream</b>: 852 signals, <b>75.5% reached target 1 or better</b>, +0.12R average per signal after "
         "costs, profit factor 1.48, worst drawdown &minus;14R. The weakest quarter (April to June 2026) had 65% and +0.04R.</li>"
         "<li><b>1-hour stream</b>: 781 signals, <b>82.5% reached the target</b>, +0.19R average per signal, profit factor "
         "2.03, worst drawdown &minus;32R.</li>"
         "<li><b>15-minute stream</b>: 199 signals, <b>82.9% reached the target</b>, +0.21R average per signal, profit factor "
         "2.32, worst drawdown &minus;11R.</li></ul>"
         "<p>Read the win rate with care: the first target is half as far as the stop, so a random entry would reach it about 2 "
         "times in 3. What the models add is the difference above that, and the average result after costs is the better "
         "measure of what a stream earns. Past results do not guarantee future results.</p>"
         "<p>Strong and standard 4-hour signals are not equal. In the test the strong ones (496 signals) reached target 1 "
         "78.6% of the time with +0.17R on average; the standard ones (356 signals, given when no coin has had a signal for 24 "
         "hours) 71.1% with +0.05R. Treat a standard signal as lower conviction: use a smaller size or skip it.</p>"),
        ("If about 3 in 4 signals win, why can I still lose money?",
         "<p>Because a full loss (&minus;1R) is bigger than most wins. A 4-hour signal that reaches TP1 and then comes back to the "
         "entry earns only about +0.17R, and a 1-hour or 15-minute win is +0.5R, so one stop-loss wipes out several wins. Losses also come in "
         "groups, because many coins fall together. Keep the risk per signal small and the same every time.</p>"),
        ("Why did the 4-hour signals change to three targets?",
         "<p>From October to November 2026 a 4-hour signal had one target as far as the stop (1R). On the unseen year that plan "
         "won 59.4% of the time and earned +0.19R per signal: about 4 in 10 signals hit the stop. With three targets (0.5R, 1R, "
         "2R) the same signals reach target 1 75.5% of the time and earn +0.12R per signal. So the three-target plan has fewer "
         "stop-losses and a smoother record, and it earns less per signal. Both plans made money in the test.</p>"),
        ("How much should I risk on each signal?",
         "<p>Keep it small and the same every time; that amount is 1R. Losing streaks happen: the worst drop was 14R on the 4-hour "
         "stream, 32R on the 1-hour stream and 11R on the 15-minute stream in the test, and a longer bad stretch can be deeper. At 1% per signal, a 30R drop "
         "is nearly a third of the account, so keep the risk well below that. There are often many signals open at once and they "
         "usually move together with Bitcoin, so 0.25% to 0.5% per signal (and a limit on open trades) is a sensible start. Only "
         "trade money you can afford to lose. The <a href=\"/tools\">position size calculator</a> works out the size for you.</p>"),
        ("Where can I see the live results?",
         "<p>On the <a href=\"/track-record\">Track record</a> page (4-hour stream: every signal the engine gives on a fixed list "
         "of coins, live, with its result in R after fees, next to a 365-day backtest that is refreshed every week) and on the "
         "<a href=\"/signals\">Signals page</a> (every stream: live win rate and results of the last 7 and 30 days).</p>"),
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
         "<p>Alerts go out only for new signals on the coins and timeframes you chose, right after a candle closes. Check that "
         "alerts are on and that the coin is in your list. For email, also check your spam folder.</p>"),
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
