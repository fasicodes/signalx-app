/* Pro insights page. Data: /api/insights/live, /api/insights/performance, /api/insights/size. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d) => (v == null || !isFinite(v) ? "--" : Number(v).toLocaleString("en-US", { minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d }));
  const signed = (v, d) => (v == null || !isFinite(v) ? "--" : (v > 0 ? "+" : "") + num(v, d));
  const cls = (v) => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");
  const pct = (v) => (v == null ? "--" : num(v, 1) + "%");

  async function getJSON(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    let d = null;
    try { d = await r.json(); } catch (e) {}
    if (r.status === 401) { location.href = "/login"; throw new Error("login"); }
    if (!d) throw new Error("Server error " + r.status);
    if (!r.ok && d.ok !== true) { const e = new Error(d.error || ("Error " + r.status)); e.payload = d; throw e; }
    return d;
  }

  function table(head, rows, empty) {
    if (!rows.length) return '<p class="empty">' + esc(empty) + "</p>";
    return '<table class="gtable"><thead><tr>' + head.map((h) => "<th>" + esc(h) + "</th>").join("") + "</tr></thead><tbody>" +
      rows.map((r) => "<tr>" + r.map((c) => "<td" + (c && c.wrap ? ' class="wrap"' : "") + ">" + (c && c.html != null ? c.html : esc(c)) + "</td>").join("") + "</tr>").join("") +
      "</tbody></table>";
  }
  const H = (html, wrap) => ({ html, wrap });
  const sideTag = (s) => H('<span class="side ' + esc(s) + '">' + (s === "LONG" ? "Long" : "Short") + "</span>");
  const pill = (t, kind) => H('<span class="pill ' + (kind || "") + '">' + esc(t) + "</span>");

  /* ------------------------------------------------------------------ live part */
  let LIVE = null;

  function renderCluster(c) {
    const part = (name, x) => {
      if (!x || !x.open) return "";
      const warn = x.risk_multiplier && x.risk_multiplier >= 1.25 ? ' <span class="pill watch">risk is ' + num(x.risk_multiplier, 1) + " times what it looks like</span>" : "";
      return "<div><p><b>" + esc(name) + "</b>: " + x.long + " long, " + x.short + " short. " + esc(x.text || "") + warn + "</p></div>";
    };
    const out = part("Crypto", c.crypto) + part("Forex", c.forex);
    $("cluster").innerHTML = out || '<p class="empty">No open signals right now.</p>';
  }

  function renderFunding(items) {
    const rows = items.filter((s) => s.market === "crypto").map((s) => {
      const f = s.funding || {};
      if (!f.available) return [s.symbol, sideTag(s.side), H("<span class='faint'>" + esc(f.reason || "Not available") + "</span>", true), "--", "--"];
      const kind = f.crowd === "longs_crowded" || f.crowd === "shorts_crowded" ? "watch" : "";
      const oi = f.oi_change ? signed(f.oi_change.pct, 1) + "% in " + num(f.oi_change.hours, 0) + "h" : "building up";
      return [s.symbol, sideTag(s.side), H(num(f.funding_pct, 4) + "% <span class='faint'>per 8h</span>"), pill(f.label || "", kind),
        H("<span class='dim'>" + esc(oi) + "</span>")];
    });
    $("funding").innerHTML = table(["Coin", "Signal", "Funding", "Crowd", "Open interest"], rows, "No open crypto signals.") +
      notesFor(items.filter((s) => s.market === "crypto" && s.funding && s.funding.available && s.funding.text && s.funding.crowd !== "neutral"), (s) => s.funding.text);
  }

  function notesFor(list, fn) {
    return list.map((s) => '<p class="warn"><b>' + esc(s.symbol) + ":</b> " + esc(fn(s)) + "</p>").join("");
  }

  function renderLiquidity(items) {
    const rows = items.map((s) => {
      const l = s.liquidity || {};
      if (!l.available) return [s.symbol, H("<span class='faint'>" + esc(l.reason || "Not available") + "</span>", true), "", "", ""];
      if (l.kind === "forex") {
        return [s.symbol, H("typical " + num(l.round_trip_pips, 1) + " pips round trip"), H("stop " + num(l.stop_pips, 1) + " pips"),
          H('<b class="num">' + num(l.cost_r, 2) + " R</b>"), pill(l.cost_r > 0.25 ? "costly" : l.cost_r > 0.12 ? "watch" : "fine", l.cost_r > 0.25 ? "bad" : l.cost_r > 0.12 ? "watch" : "ok")];
      }
      const cell = (n) => {
        const x = (l.sizes || []).find((z) => z.notional === n);
        if (!x) return "--";
        if (x.cost_r == null) return H('<span class="neg">book too thin</span>');
        return H('<span class="num">' + num(x.cost_r, 2) + " R</span> <span class='faint'>(" + num(x.bps, 1) + " bps)</span>");
      };
      const k = l.flag === "costly" ? "bad" : l.flag === "watch" ? "watch" : "ok";
      return [s.symbol, H("spread " + num(l.spread_bps, 1) + " bps"), cell(1000), cell(10000), cell(100000), pill(l.flag === "costly" ? "costly" : l.flag === "watch" ? "watch" : "fine", k)];
    });
    const crypto = items.some((s) => s.market === "crypto");
    $("liquidity").innerHTML = table(crypto ? ["Coin", "Spread", "$1k order", "$10k order", "$100k order", ""] : ["Pair", "Cost", "Stop", "In R", ""], rows, "No open signals.") +
      '<p class="note">Order sizes are in USDT. Cost = fees on both sides plus the slippage of a market order, divided by the distance to the stop.</p>';
  }

  function renderEvents(events, items) {
    if (!events.available) { $("events").innerHTML = '<p class="empty">' + esc(events.reason || "Not available.") + "</p>"; return; }
    const byEv = {};
    items.forEach((s) => (s.event_hits || []).forEach((h) => { (byEv[h.title + " (" + h.currency + ")"] = byEv[h.title + " (" + h.currency + ")"] || []).push(s.symbol + (s.side === "LONG" ? " long" : " short")); }));
    let out = Object.keys(byEv).map((t) => '<p class="warn"><b>' + esc(t) + "</b> is due while these signals are still open: " + esc(byEv[t].join(", ")) + ".</p>").join("");
    if (!events.events.length) out += '<p class="empty">No high-impact events for ' + esc(events.currencies.join(", ")) + " in the next 48 hours.</p>";
    else out += events.events.map((e) => '<div class="ev"><time datetime="' + esc(e.time) + '">' + esc(new Date(e.time).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })) +
      "</time><span><b>" + esc(e.currency) + "</b> " + esc(e.title) + "</span></div>").join("");
    $("events").innerHTML = out;
  }

  function fillSignalPicker(items) {
    const sel = $("k-sig"), keep = sel.value;
    sel.innerHTML = '<option value="">Enter the prices yourself</option>' + items.map((s) =>
      '<option value="' + s.id + '">' + esc(s.symbol + " " + s.timeframe + " " + (s.side === "LONG" ? "long" : "short")) + "</option>").join("");
    if (items.some((s) => String(s.id) === keep)) sel.value = keep;
  }

  async function loadLive() {
    try {
      const d = await getJSON("/api/insights/live");
      LIVE = d;
      $("live-state").hidden = true;
      $("live-grid").hidden = false;
      renderCluster(d.cluster);
      renderFunding(d.signals);
      renderLiquidity(d.signals);
      renderEvents(d.events, d.signals);
      fillSignalPicker(d.signals);
      calc();
    } catch (e) {
      if (e.message === "login") return;
      $("live-state").hidden = false;
      $("live-state").textContent = e.message;
      $("live-grid").hidden = LIVE == null;
    }
  }

  /* ------------------------------------------------------------------ position size */
  let calcTimer = null, calcSeq = 0;
  function calc() {
    clearTimeout(calcTimer);
    calcTimer = setTimeout(async () => {
      const entry = parseFloat($("k-entry").value), stop = parseFloat($("k-stop").value);
      const out = $("calc-out");
      if (!(entry > 0) || !(stop > 0)) { out.innerHTML = '<p class="empty">Enter an entry and a stop price.</p>'; return; }
      const seq = ++calcSeq;
      const q = new URLSearchParams({ balance: $("k-bal").value, risk: $("k-risk").value, entry: entry, stop: stop, leverage: $("k-lev").value || "", open: LIVE ? LIVE.signals.length : 0 });
      let d;
      try { d = await getJSON("/api/insights/size?" + q); } catch (e) { d = e.payload || { ok: false, error: e.message }; }
      if (seq !== calcSeq) return;
      if (!d.ok) { out.innerHTML = '<p class="warn">' + esc(d.error || "Could not calculate.") + "</p>"; return; }
      const dec = entry < 1 ? 6 : entry < 100 ? 4 : 2;
      out.innerHTML = "<dl>" +
        "<dt>Direction</dt><dd>" + (d.side === "LONG" ? "Long (buy)" : "Short (sell)") + "</dd>" +
        "<dt>Money at risk</dt><dd>" + num(d.risk_amount) + "</dd>" +
        "<dt>Amount to trade</dt><dd>" + num(d.units, Math.abs(d.units) < 1 ? 6 : 4) + "</dd>" +
        "<dt>Position value</dt><dd>" + num(d.notional) + "</dd>" +
        "<dt>Stop distance</dt><dd>" + num(d.stop_pct, 2) + "%</dd>" +
        "<dt>Leverage needed</dt><dd>" + num(d.leverage_needed, 2) + "x</dd>" +
        (d.margin != null ? "<dt>Margin to put up</dt><dd>" + num(d.margin) + "</dd>" : "") +
        "<dt>Fees, both sides</dt><dd>" + num(d.fees) + "</dd>" +
        "<dt>Gain if 1R is reached</dt><dd>" + num(d.gain_at_1r) + "</dd></dl>" +
        (d.warnings || []).map((w) => '<p class="warn">' + esc(w) + "</p>").join("") +
        (d.suggest && LIVE && LIVE.signals.length > 1 ?
          '<p class="warn">You have ' + LIVE.signals.length + " signals open. If you take them all and all hit the stop, " + num(d.suggest.worst_case, 1) +
          "% is lost with " + num(d.suggest.per_signal, 2) + "% on each. Keeping the total near " + d.suggest.cap + "% means " + num(d.suggest.per_signal, 2) + "% per signal.</p>" : "");
    }, 250);
  }

  function pickSignal() {
    const id = $("k-sig").value;
    if (!id || !LIVE) return;
    const s = LIVE.signals.find((x) => String(x.id) === id);
    if (!s) return;
    $("k-entry").value = s.entry;
    $("k-stop").value = s.stop_loss;
    calc();
  }

  /* ------------------------------------------------------------------ results part */
  const ST = { market: "crypto", tf: "4h", src: "live" };
  let PERF = null;

  function rangeTxt(lo, hi, f) { return lo == null ? "" : '<span class="range">range ' + f(lo) + " to " + f(hi) + "</span>"; }

  function renderSure(s) {
    const need = s.signals_needed == null ? "" : s.signals_missing > 0 ?
      "About " + num(s.signals_needed, 0) + " signals are needed to be sure the average is above zero. " + num(s.signals_missing, 0) + " to go." :
      "Enough signals to be sure the average is above zero.";
    $("sure").innerHTML =
      '<dl class="strip">' +
      "<div><dt>Closed signals</dt><dd>" + num(s.n, 0) + "</dd><small>" + (s.n < 30 ? "too few to judge" : "enough to start judging") + "</small></div>" +
      "<div><dt>Win rate</dt><dd>" + pct(s.win_rate) + "</dd><small>" + rangeTxt(s.win_low, s.win_high, pct) + "</small></div>" +
      '<div><dt>Average result per signal</dt><dd class="' + cls(s.avg_r) + '">' + (s.avg_r == null ? "--" : signed(s.avg_r, 2) + " R") + "</dd><small>" +
      rangeTxt(s.avg_low, s.avg_high, (v) => signed(v, 2) + " R") + "</small></div>" +
      "<div><dt>Still needed</dt><dd>" + (s.signals_missing == null ? "--" : num(s.signals_missing, 0)) + "</dd><small>" + esc(s.signals_needed == null ? "" : "signals to be sure") + "</small></div></dl>" +
      '<p class="verdict ' + esc(s.verdict) + '">' + esc(s.text) + (need ? " " + esc(need) : "") + "</p>";
  }

  function renderDrift(d, modelTest) {
    if (!d) { $("drift").innerHTML = ""; return; }
    const kind = d.status === "in_line" || d.status === "above" ? "ok" : d.status === "below" ? "bad" : "watch";
    const label = { in_line: "In line", above: "Better than the test", below: "Worse than the test", too_early: "Too early", no_test: "No test to compare" }[d.status] || d.status;
    let out = '<p><span class="pill ' + kind + '">' + esc(label) + "</span></p><p>" + esc(d.text) + "</p>";
    if (d.expected_avg_r != null) {
      out += "<div class='res' style='margin-top:8px'><dl><dt>Test: average per signal</dt><dd>" + signed(d.expected_avg_r, 2) + " R</dd>" +
        (d.expected_win_rate != null ? "<dt>Test: win rate</dt><dd>" + pct(d.expected_win_rate) + "</dd>" : "") +
        (d.expected_signals != null ? "<dt>Test: signals</dt><dd>" + num(d.expected_signals, 0) + "</dd>" : "") + "</dl></div>";
    }
    if (modelTest && modelTest.win_low != null) out += '<p class="note">Even the test has a range: its ' + pct(modelTest.win_rate) + " win rate means " + pct(modelTest.win_low) + " to " + pct(modelTest.win_high) + " in the long run.</p>";
    $("drift").innerHTML = out;
  }

  function renderStreaks(s) {
    if (!s || !s.available) { $("streaks").innerHTML = '<p class="empty">' + esc((s && s.reason) || "Not enough closed signals yet.") + "</p>"; return; }
    const dd = s.drawdown_100;
    $("streaks").innerHTML = "<div class='res'><dl>" +
      "<dt>Longest losing run so far</dt><dd>" + s.longest_losing + "</dd>" +
      "<dt>Losing run now</dt><dd>" + s.current_losing + "</dd>" +
      "<dt>Worst drawdown so far</dt><dd class='neg'>" + num(s.max_drawdown_r, 1) + " R</dd>" +
      "<dt>Typical longest run in 100 signals</dt><dd>" + s.expected_longest_100 + "</dd>" +
      (dd ? "<dt>Drawdown in the next 100 (usual / bad case)</dt><dd>" + num(dd.median, 1) + " / " + num(dd.p95, 1) + " R</dd>" : "") + "</dl></div>" +
      "<p class='note' style='margin-top:10px'>Chance of at least this many losses in a row within 100 signals: " +
      s.chance.map((c) => "<b>" + c.run + "</b> in a row " + num(c.pct, 0) + "%").join(", ") + ".</p>" +
      (dd ? "<p class='note'>At 1% risk per signal, a " + num(dd.p95, 0) + " R drawdown is about " + num(dd.p95, 0) + "% of the account. Choose a risk you can sit through.</p>" : "");
  }

  function groupTable(list, firstHead) {
    const rows = list.map((g) => [g.label, g.n && !g.enough ? H(g.n + ' <span class="faint" title="Fewer than 30 signals: treat as a hint, not a result">(few)</span>') : g.n,
      g.n ? H(pct(g.win_rate) + (g.win_low != null ? " <span class='faint'>(" + num(g.win_low, 0) + "-" + num(g.win_high, 0) + ")</span>" : "")) : "--",
      g.avg_r == null ? "--" : H('<span class="' + cls(g.avg_r) + '">' + signed(g.avg_r, 2) + "</span>")]);
    return table([firstHead, "Signals", "Win rate (range)", "Avg R"], rows, "No data.");
  }

  function render() {
    if (!PERF) return;
    const set = PERF[ST.src] || PERF.live;
    $("src-wrap").hidden = !PERF.backtest;
    renderSure(set.sure);
    renderDrift(ST.src === "live" ? PERF.drift : null, PERF.model_test_sure);
    if (ST.src !== "live") $("drift").innerHTML = '<p class="empty">The comparison is for the real record only.</p>';
    renderStreaks(set.streaks);
    $("sessions").innerHTML = groupTable(set.sessions, "Session");
    const rg = set.regimes;
    $("regime-why").textContent = "Trend and volatility of " + PERF.benchmark + " (4h candles) when each signal came. A system that only works in one condition is a trap in the others.";
    if (!PERF.regime_available || !rg) $("regimes").innerHTML = '<p class="empty">Benchmark candles are not available right now.</p>';
    else if (!rg.labelled) $("regimes").innerHTML = '<p class="empty">The signals are older than the candles the server can load, so they could not be labelled.</p>';
    else $("regimes").innerHTML = groupTable(rg.trend, "Trend") + '<div style="height:10px"></div>' + groupTable(rg.vol, "Volatility") +
      '<p class="note">' + rg.labelled + " of " + set.sure.n + " signals could be labelled.</p>";
  }

  async function loadPerf() {
    $("perf-state").hidden = false;
    $("perf-state").textContent = "Loading…";
    $("perf").hidden = true;
    try {
      PERF = await getJSON("/api/insights/performance?market=" + ST.market + "&timeframe=" + ST.tf);
      if (ST.src === "backtest" && !PERF.backtest) { ST.src = "live"; $("src").value = "live"; }
      $("perf-state").hidden = true;
      $("perf").hidden = false;
      render();
    } catch (e) {
      if (e.message === "login") return;
      $("perf-state").hidden = false;
      $("perf-state").textContent = e.message;
    }
  }

  function tabs(id, key) {
    $(id).addEventListener("click", (ev) => {
      const b = ev.target.closest("button");
      if (!b) return;
      ST[key] = b.dataset.v;
      ST.src = "live";
      $("src").value = "live";
      $(id).querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", x === b ? "true" : "false"));
      loadPerf();
    });
  }

  tabs("mk", "market");
  tabs("tf", "tf");
  $("src").addEventListener("change", () => { ST.src = $("src").value; render(); });
  $("calc").addEventListener("input", (ev) => { if (ev.target.id !== "k-sig") calc(); });
  $("k-sig").addEventListener("change", pickSignal);
  $("calc").addEventListener("submit", (ev) => ev.preventDefault());

  loadLive();
  loadPerf();
  setInterval(() => { if (!document.hidden) loadLive(); }, 60000);
})();
