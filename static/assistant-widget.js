/* Signals FM assistant widget: a chat button on every main page.
   Streams answers from /api/assistant/chat (server-sent events) and falls back to /api/assistant/ask.
   Pages can pass context with window.SFM_ASSIST = { page, signal_id, symbol } or open it with
   window.sfmAssistant.open({ signal_id, symbol, question }). History stays in this browser only. */
(function () {
  "use strict";
  if (window.sfmAssistant) return;
  var KEY = "sfm-assistant-history";
  var MAX_KEEP = 20;
  var css = "" +
    ".sfa-btn{position:fixed;right:18px;bottom:18px;z-index:70;width:56px;height:56px;border-radius:50%;border:0;cursor:pointer;" +
    "background:var(--accent,#16a34a);color:#03140b;box-shadow:0 8px 24px rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center}" +
    ".sfa-btn svg{width:26px;height:26px}" +
    "@media (max-width:760px){.sfa-btn{bottom:78px;right:14px;width:52px;height:52px}}" +
    ".sfa-panel{position:fixed;right:18px;bottom:86px;z-index:71;width:min(400px,calc(100vw - 24px));height:min(600px,calc(100vh - 120px));" +
    "background:var(--panel,#0b1210);color:var(--text,#e8f0ec);border:1px solid var(--line,#1f2a25);border-radius:16px;display:none;" +
    "flex-direction:column;overflow:hidden;box-shadow:0 18px 50px rgba(0,0,0,.45);font:14px/1.5 var(--body,Inter,system-ui,sans-serif)}" +
    ".sfa-panel.open{display:flex}" +
    "@media (max-width:760px){.sfa-panel{right:0;left:0;bottom:0;top:0;width:100%;height:100%;border-radius:0}}" +
    ".sfa-head{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--line,#1f2a25)}" +
    ".sfa-head b{flex:1;font-size:15px}" +
    ".sfa-head button{background:transparent;border:1px solid var(--line,#1f2a25);color:var(--text-dim,#9fb1a8);border-radius:8px;padding:5px 9px;cursor:pointer;font:12.5px var(--body,inherit)}" +
    ".sfa-msgs{flex:1;overflow:auto;padding:12px 14px;display:flex;flex-direction:column;gap:10px}" +
    ".sfa-m{max-width:88%;padding:9px 12px;border-radius:12px;white-space:pre-wrap;word-wrap:break-word}" +
    ".sfa-m.u{align-self:flex-end;background:var(--long-dim,rgba(22,163,74,.18))}" +
    ".sfa-m.a{align-self:flex-start;background:var(--panel-raised,rgba(127,127,127,.12))}" +
    ".sfa-m.n{align-self:center;font-size:12.5px;color:var(--text-faint,#7d8f86);background:transparent;padding:0}" +
    ".sfa-sugg{display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 8px}" +
    ".sfa-sugg button{border:1px solid var(--line,#1f2a25);background:transparent;color:var(--text-dim,#9fb1a8);border-radius:14px;padding:5px 10px;cursor:pointer;font:12.5px var(--body,inherit)}" +
    ".sfa-form{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--line,#1f2a25)}" +
    ".sfa-form textarea{flex:1;resize:none;height:42px;max-height:120px;background:var(--void,#05080a);color:var(--text,#e8f0ec);border:1px solid var(--line,#1f2a25);border-radius:10px;padding:10px;font:14px var(--body,inherit)}" +
    ".sfa-form button{border:0;border-radius:10px;padding:0 14px;background:var(--accent,#16a34a);color:#03140b;font-weight:600;cursor:pointer}" +
    ".sfa-form button:disabled{opacity:.5;cursor:default}" +
    ".sfa-foot{padding:0 14px 10px;font-size:11.5px;color:var(--text-faint,#7d8f86)}" +
    ".sfa-ctx{padding:6px 14px;font-size:12.5px;color:var(--text-dim,#9fb1a8);border-bottom:1px solid var(--line,#1f2a25)}";
  var st = document.createElement("style");
  st.textContent = css;
  document.head.appendChild(st);

  var btn = document.createElement("button");
  btn.className = "sfa-btn";
  btn.type = "button";
  btn.setAttribute("aria-label", "Open the assistant");
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M21 12a8 8 0 01-11.6 7.1L4 20l1-4.6A8 8 0 1121 12z"/><path d="M8.5 11h.01M12 11h.01M15.5 11h.01"/></svg>';
  var panel = document.createElement("div");
  panel.className = "sfa-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Signals FM assistant");
  panel.innerHTML = '<div class="sfa-head"><b>Assistant</b><button type="button" data-a="clear">Clear</button><button type="button" data-a="close" aria-label="Close">Close</button></div>' +
    '<div class="sfa-ctx" hidden></div><div class="sfa-msgs" aria-live="polite"></div><div class="sfa-sugg"></div>' +
    '<form class="sfa-form"><textarea maxlength="1200" placeholder="Ask anything about trading, signals, news or the site" aria-label="Your question"></textarea><button type="submit">Send</button></form>' +
    '<div class="sfa-foot">Answers are information, not financial advice. Trading involves risk.</div>';
  document.body.appendChild(btn);
  document.body.appendChild(panel);

  var msgs = panel.querySelector(".sfa-msgs");
  var ta = panel.querySelector("textarea");
  var send = panel.querySelector(".sfa-form button");
  var sugg = panel.querySelector(".sfa-sugg");
  var ctxBar = panel.querySelector(".sfa-ctx");
  var history = [];
  var extra = {};
  var busy = false;
  try { history = JSON.parse(localStorage.getItem(KEY) || "[]") || []; } catch (e) { history = []; }

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(history.slice(-MAX_KEEP))); } catch (e) {}
  }
  function add(role, text) {
    var d = document.createElement("div");
    d.className = "sfa-m " + (role === "user" ? "u" : role === "note" ? "n" : "a");
    d.textContent = text;
    msgs.appendChild(d);
    msgs.scrollTop = msgs.scrollHeight;
    return d;
  }
  function render() {
    msgs.innerHTML = "";
    if (!history.length) add("assistant", "Hi! Ask me about a signal, a trading term, risk management, the news or how to use the site.");
    history.forEach(function (h) { add(h.role, h.text); });
    var s = ["How do I use a signal?", "What do grades A, B and C mean?", "How much should I risk per trade?", "What is the next big news event?"];
    var c = context();
    if (c.signal_id) s.unshift("Explain this signal");
    sugg.innerHTML = "";
    if (history.length < 2) s.slice(0, 4).forEach(function (q) {
      var b = document.createElement("button"); b.type = "button"; b.textContent = q;
      b.addEventListener("click", function () { ask(q); });
      sugg.appendChild(b);
    });
  }
  function context() {
    var base = window.SFM_ASSIST || {};
    var out = { page: location.pathname };
    ["signal_id", "symbol"].forEach(function (k) { if (extra[k] != null) out[k] = extra[k]; else if (base[k] != null) out[k] = base[k]; });
    return out;
  }
  function showCtx() {
    var c = context();
    if (c.signal_id || c.symbol) {
      ctxBar.hidden = false;
      ctxBar.textContent = c.signal_id ? "Looking at signal #" + c.signal_id + (c.symbol ? " (" + c.symbol + ")" : "") : "Symbol on screen: " + c.symbol;
    } else ctxBar.hidden = true;
  }
  function open(opts) {
    opts = opts || {};
    if (opts.signal_id != null || opts.symbol != null) extra = { signal_id: opts.signal_id, symbol: opts.symbol };
    panel.classList.add("open");
    btn.setAttribute("aria-expanded", "true");
    showCtx(); render();
    if (opts.question) ask(opts.question); else setTimeout(function () { ta.focus(); }, 30);
  }
  function close() { panel.classList.remove("open"); btn.setAttribute("aria-expanded", "false"); }

  async function viaJSON(body, el) {
    var r = await fetch("/api/assistant/ask", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    var d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || "failed");
    el.textContent = d.reply;
    return d.reply;
  }
  async function ask(q) {
    q = String(q || "").trim();
    if (!q || busy) return;
    busy = true; send.disabled = true; sugg.innerHTML = "";
    add("user", q);
    var past = history.slice(-10);
    history.push({ role: "user", text: q });
    var el = add("assistant", "…");
    var c = context();
    var body = { question: q, history: past, page: c.page, signal_id: c.signal_id, symbol: c.symbol };
    var text = "";
    try {
      var r = await fetch("/api/assistant/chat", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok || !r.body || !window.TextDecoder) {
        text = await viaJSON(body, el);
      } else {
        var reader = r.body.getReader(), dec = new TextDecoder(), buf = "";
        while (true) {
          var x = await reader.read();
          if (x.done) break;
          buf += dec.decode(x.value, { stream: true });
          var parts = buf.split("\n\n"); buf = parts.pop();
          parts.forEach(function (p) {
            var line = p.trim(); if (line.indexOf("data:") !== 0) return;
            var ev; try { ev = JSON.parse(line.slice(5)); } catch (e) { return; }
            if (ev.t) { text += ev.t; el.textContent = text; msgs.scrollTop = msgs.scrollHeight; }
            if (ev.notice) add("note", ev.notice);
          });
        }
        if (!text) text = await viaJSON(body, el);
      }
    } catch (e) {
      try { text = await viaJSON(body, el); }
      catch (e2) { text = "I could not reach the server. Check your connection and try again."; el.textContent = text; }
    }
    history.push({ role: "assistant", text: text });
    save();
    busy = false; send.disabled = false; ta.focus();
  }

  btn.addEventListener("click", function () { panel.classList.contains("open") ? close() : open(); });
  panel.querySelector('[data-a="close"]').addEventListener("click", close);
  panel.querySelector('[data-a="clear"]').addEventListener("click", function () { history = []; save(); extra = {}; showCtx(); render(); });
  panel.querySelector("form").addEventListener("submit", function (e) { e.preventDefault(); var q = ta.value; ta.value = ""; ask(q); });
  ta.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); var q = ta.value; ta.value = ""; ask(q); } });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && panel.classList.contains("open")) close(); });
  window.sfmAssistant = { open: open, close: close, ask: function (q) { open({ question: q }); } };
})();
