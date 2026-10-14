/* Signals FM dashboard (templates/dashboard.html).
   Data: /coins, /api/engine?coin=, /candles, /api/signals/board. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const S = { coins: { crypto: [], forex: [] }, coin: null, kind: "crypto", tf: "4h", board: null, eng: null,
              chart: null, series: null, lines: [], sheetKind: "crypto", loadingCoin: null, bars: [], hist: null };

  // ---------------------------------------------------------------- helpers
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // "?" button that opens a plain-language explanation (glossary.py, shown by shell.js)
  const tip = (key, label) => `<button type="button" class="tip" data-tip="${key}" aria-label="What does ${esc(label)} mean?">?</button>`;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  };
  function decimals(v, sym) {
    const a = Math.abs(Number(v));
    if (sym && S.coins.forex.includes(sym)) return sym.startsWith("XAU") ? 2 : sym.includes("JPY") ? 3 : 5;
    if (a >= 1000) return 2;
    if (a >= 10) return 3;
    if (a >= 1) return 4;
    if (a >= 0.01) return 5;
    return 8;
  }
  function price(v, sym) {
    if (v == null || isNaN(v)) return "--";
    const d = decimals(v, sym || S.coin);
    return Number(v).toLocaleString("en-US", { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d });
  }
  const signed = (v, d = 2) => (v == null || isNaN(v) ? "--" : `${v > 0 ? "+" : ""}${Number(v).toFixed(d)}`);
  const toDate = (iso) => new Date(/Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z");
  const clock = (iso) => { const d = toDate(iso); return isNaN(d) ? "--" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
  const when = (iso) => { const d = toDate(iso); return isNaN(d) ? "--" : d.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }); };
  function ago(iso) {
    const m = Math.max(0, Math.round((Date.now() - toDate(iso)) / 60000));
    if (m < 60) return `${m} min ago`;
    const h = Math.floor(m / 60);
    return h < 48 ? `${h} h ago` : `${Math.floor(h / 24)} days ago`;
  }
  function left(iso) {
    const m = Math.max(0, Math.round((toDate(iso) - Date.now()) / 60000));
    if (m < 60) return `${m} min`;
    const h = Math.floor(m / 60);
    return h < 48 ? `${h} h` : `${Math.floor(h / 24)} d ${h % 24} h`;
  }
  const base = (sym) => String(sym || "").split("/")[0];
  const ICON_SLUG = { HYPE: "hype", GRAM: "gram", ASTER: "aster", ONDO: "ondo", TAO: "tao" };
  function badgeIcon(t) {
    const pal = ["#3b82f6", "#8b5cf6", "#ec4899", "#f97316", "#14b8a6", "#eab308", "#ef4444", "#22c55e"];
    let sum = 0; for (const ch of t) sum += ch.charCodeAt(0);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><circle cx="20" cy="20" r="19" fill="${pal[sum % pal.length]}"/><text x="20" y="25" font-family="Arial" font-size="11" font-weight="800" fill="#fff" text-anchor="middle">${t.slice(0, 3)}</text></svg>`;
    return "data:image/svg+xml;base64," + btoa(svg);
  }
  function setIcon(img, sym) {
    const t = base(sym).toUpperCase();
    img.onerror = null;
    if (S.coins.forex.includes(sym)) { img.src = badgeIcon(t); return; }
    img.onerror = () => { img.onerror = null; img.src = badgeIcon(t); };
    img.src = `https://assets.coincap.io/assets/icons/${(ICON_SLUG[t] || t).toLowerCase()}@2x.png`;
  }
  async function getJSON(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    if (r.status === 401) { location.href = "/login"; throw new Error("login"); }
    const d = await r.json().catch(() => ({}));
    if (!r.ok && !d.error) d.error = `Request failed (${r.status})`;
    return d;
  }
  function toast(msg) {
    const t = document.createElement("div");
    t.className = "d-toast"; t.setAttribute("role", "status"); t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2200);
  }
  const visible = () => document.visibilityState !== "hidden";

  // ---------------------------------------------------------------- coin choice
  function boardRow(sym) {
    return S.board && S.board.rows ? S.board.rows.find((r) => r.symbol === sym) : null;
  }
  function pickDefault() {
    const url = new URLSearchParams(location.search).get("coin");
    const all = S.coins.crypto.concat(S.coins.forex);
    if (url && all.includes(url.toUpperCase())) return url.toUpperCase();
    const saved = store.get("sfm-coin");
    if (saved && all.includes(saved)) return saved;
    const rows = (S.board && S.board.rows) || [];
    const fresh = rows.find((r) => r.state === "ACTIVE" && r.fresh);
    const act = rows.find((r) => r.state === "ACTIVE");
    const other = streamsActive().find((x) => all.includes(x.symbol));
    return (fresh || act || other || {}).symbol || "BTC/USDT";
  }
  // active signals of the other streams (crypto 15m / 1h, forex), newest first - from /api/signals/board
  function streamsActive() { return ((S.board && S.board.streams_active) || []).filter((x) => x && x.status === "ACTIVE"); }
  function streamFor(sym) { return streamsActive().find((x) => x.symbol === sym); }

  function selectCoin(sym) {
    S.coin = sym;
    window.SFM_ASSIST = { symbol: sym };          // the assistant widget explains the coin on screen
    S.kind = S.coins.forex.includes(sym) ? "forex" : "crypto";
    store.set("sfm-coin", sym);
    try { history.replaceState(null, "", `/?coin=${encodeURIComponent(sym)}`); } catch (e) {}
    $("coin-sym").textContent = sym;
    $("coin-kind").textContent = S.kind === "forex" ? (sym.startsWith("XAU") ? "Gold" : "Forex") : "Crypto";
    $("coin-price").textContent = "--";
    $("coin-chg").innerHTML = "&nbsp;";
    setIcon($("coin-icon"), sym);
    document.title = `${sym} | Signals FM`;
    $("chart-title").textContent = `${base(sym)} chart`;
    renderLoading();
    updateProLinks();
    loadEngine();
    loadChart();
    loadInternals();
    renderOthers();
  }

  // ---------------------------------------------------------------- tool links (carry the coin)
  function updateProLinks() {
    const q = `?coin=${encodeURIComponent(S.coin)}`;
    document.querySelectorAll(".d-tool[data-pro]").forEach((a) => { a.href = `/advanced${q}#${a.dataset.pro}`; });
    document.querySelectorAll(".d-tool[data-page]").forEach((a) => { a.href = `${a.dataset.page}${q}`; });
    const link = (id, href) => { const el = $(id); if (el) el.href = href; };
    link("pt-open", `/advanced${q}`);
    link("adv-chart-link", `/chart${q}&tf=${encodeURIComponent(S.tf)}`);
    link("internals-link", `/advanced${q}#microstructure`);
  }

  // ---------------------------------------------------------------- market internals (the analysis channels)
  const cap = (x) => (x ? String(x).charAt(0).toUpperCase() + String(x).slice(1).toLowerCase().replace(/_/g, " ") : "");
  async function loadInternals(quiet) {
    const sym = S.coin;
    if (!$("internals") || !$("internals-tiles")) return;      // page without the card
    if (S.kind !== "crypto") { $("internals").hidden = true; return; }
    if (!quiet) { $("internals").hidden = false; $("internals-tiles").innerHTML = '<p class="d-empty">Reading the order book…</p>'; }
    let d;
    try { d = await getJSON(`/signal?coin=${encodeURIComponent(sym)}&timeframe=1h`); } catch (e) { return; }
    if (sym !== S.coin) return;
    if (d.error) { $("internals-tiles").innerHTML = `<p class="d-empty">These readings are not available right now: ${esc(d.error)}</p>`; return; }
    const ofi = d.order_flow || {}, vp = d.toxic_flow || {}, rg = d.market_regime || {}, ms = d.market_strength || {};
    const f = d.funding_open_interest || {}, mg = (d.liquidity_magnet_target || {}).magnet, cr = d.market_crash_risk || {};
    const num = (v) => v != null && !isNaN(v);
    const tox = { HIGH_TOXICITY: "High: informed traders are active", MODERATE_TOXICITY: "Moderate", LOW_TOXICITY: "Low: calm, mixed flow" };
    const verdict = String(d.final_verdict || "WAIT").toLowerCase();
    const KEYS = { "Order flow": "order_flow", "Toxic flow (VPIN)": "vpin", "Market regime": "regime", "Market strength": "market_strength",
                   "Funding rate": "funding", "Liquidity magnet": "liquidity_magnet", "Crash risk": "crash_risk", "Channels agreeing": "channels" };
    const tiles = [
      ["Order flow", num(ofi.ofi_score) ? signed(ofi.ofi_score, 2) : "--", num(ofi.ofi_score) ? (ofi.ofi_score >= 0 ? "Buyers more aggressive" : "Sellers more aggressive") : "No order-book data",
       num(ofi.ofi_score) ? (ofi.ofi_score >= 0 ? "c-long" : "c-short") : ""],
      ["Toxic flow (VPIN)", num(vp.vpin_score) ? Number(vp.vpin_score).toFixed(2) : "--", tox[vp.toxicity] || "No trade data", ""],
      ["Market regime", rg.regime === "Trending" || rg.regime === "Ranging" ? rg.regime : "--", "Hidden Markov model on 1h candles", ""],
      ["Market strength", num(ms.score) ? `${Math.round(ms.score)}/100` : "--", cap(ms.label) || "No data",
       ms.bias === "BUY" ? "c-long" : ms.bias === "SELL" ? "c-short" : ""],
      ["Funding rate", num(f.funding_rate_pct) ? signed(f.funding_rate_pct, 4) + "%" : "--",
       num(f.funding_rate_pct) ? (f.funding_rate_pct >= 0 ? "Longs pay shorts" : "Shorts pay longs") : "No perpetual market", ""],
      ["Liquidity magnet", mg ? price(mg.price) : "--", mg ? `${mg.side === "SUPPORT" ? "Support" : "Resistance"}, ${mg.distance_pct}% away` : "No large order cluster",
       mg ? (mg.side === "SUPPORT" ? "c-long" : "c-short") : ""],
      ["Crash risk", num(cr.score) ? `${Math.round(cr.score)}/100` : "--", cap(cr.label) || "No data",
       cr.label === "ELEVATED" ? "c-short" : cr.label === "WATCH" ? "c-amber" : ""],
      ["Channels agreeing", d.concept_total ? `${d.concept_agree_count} of ${d.concept_total}` : "--",
       d.concept_total ? `with the ${verdict} verdict` : "No directional verdict to compare", ""],
    ];
    $("internals").hidden = false;
    $("internals-tiles").innerHTML = tiles.map(([k, v, sub, cls]) =>
      `<dl class="d-tile"><dt>${esc(k)}${tip(KEYS[k], k)}</dt><dd class="tnum ${cls}">${esc(v)}<small>${esc(sub)}</small></dd></dl>`).join("");
  }

  // ---------------------------------------------------------------- signal panel
  function setState(state, verdict, status, tipKey) {
    $("sig").dataset.state = state;
    $("sig-verdict").innerHTML = esc(verdict) + (tipKey ? tip(tipKey, verdict) : "");
    $("sig-status").textContent = status;
  }
  function renderLoading() {
    setState("LOADING", "Loading", "Reading the market…");
    $("sig-pill").hidden = true;
    $("sig-prob").hidden = true;
    $("sig-body").innerHTML = "";
    $("brief-card").hidden = true;
  }

  async function loadEngine(quiet) {
    const sym = S.coin;
    let d;
    try { d = await getJSON(`/api/engine?coin=${encodeURIComponent(sym)}`); }
    catch (e) { if (sym === S.coin && !quiet) renderError("Could not reach the server. Check your connection; this page retries every 30 seconds."); return; }
    if (sym !== S.coin) return;
    S.eng = d;
    renderHeaderPrice(d);
    const other = (d.other_active || []).filter((x) => x && x.status === "ACTIVE");
    if (d.asset === "forex") (other.length ? renderOther(d, other) : renderForex(d));
    else if (d.engine && d.engine.active) renderActive(d);
    else if (other.length) renderOther(d, other);
    else if (!d.engine || d.engine.error || d.error) renderError((d.engine && d.engine.error) || d.error || "The signal engine did not answer.");
    else renderWait(d);
    renderBrief(d.brief);
    renderProof(d.test);
    drawLevels();
  }

  function renderHeaderPrice(d) {
    const p = d.price != null ? d.price : d.brief && d.brief.price;
    $("coin-price").textContent = price(p);
    const ch = d.brief && d.brief.change_24h_pct;
    $("coin-chg").innerHTML = ch == null ? "&nbsp;" : `<span class="${ch >= 0 ? "c-long" : "c-short"}">${signed(ch)}% 24h</span>`;
  }

  function renderError(msg) {
    setState("ERROR", "Unavailable", msg);
    $("sig-pill").hidden = true;
    $("sig-prob").hidden = true;
    $("sig-body").innerHTML = `<div class="d-actions"><button class="d-btn" type="button" id="retry">Try again</button></div>`;
    $("retry").addEventListener("click", () => { renderLoading(); loadEngine(); });
  }

  function renderForex(d) {
    const gold = S.coin.startsWith("XAU");
    const on = ((S.board && S.board.streams_enabled) || []).filter((k) => k.startsWith("forex_")).map((k) => k.split("_")[1]);
    const names = { "15m": "15-minute", "1h": "1-hour", "4h": "4-hour" };
    const how = !S.board ? "" : on.length
      ? ` Forex signals run on ${on.map((t) => names[t] || t).join(" and ")} candles during the London and New York sessions.`
      : " Forex signals are switched off on this site until a forex model passes its test on unseen data.";
    setState("FOREX", "No active signal", d.error ? d.error : `No active ${gold ? "gold" : "forex"} signal on ${S.coin} right now.${how}`);
    $("sig-pill").hidden = true;
    $("sig-prob").hidden = true;
    $("sig-body").innerHTML = `${liveElsewhere()}<div class="d-actions"><a class="d-btn" href="/signals?market=forex">See forex signals</a></div>`;
    wireJump();
  }

  function renderOther(d, list) {
    const a = list[0];
    const long = a.side === "LONG";
    const dec = a.decimals;
    const px = (v) => dec != null ? Number(v).toFixed(dec) : price(v);
    setState(a.side, `${long ? "Long" : "Short"} · ${a.timeframe} signal`,
      `Started ${ago(a.signal_at)} on the ${a.timeframe} chart. Expires ${when(a.expires_at)}.`, long ? "long" : "short");
    const pill = $("sig-pill");
    pill.hidden = false; pill.className = "d-pill";
    pill.textContent = `Grade ${a.grade}`;
    $("sig-prob").hidden = a.confidence == null;
    if (a.confidence != null) { $("sig-prob-v").textContent = `${Math.round(a.confidence)}%`; $("sig-prob-l").textContent = "chance of TP1"; }
    const tps = (a.targets || []).map((v, k) => `<div><span class="c-dim">TP${k + 1}${k < (a.hits || 0) ? " ✓" : ""}</span> <b class="tnum c-long">${px(v)}</b></div>`).join("");
    const now = a.price != null ? `<p class="d-copy">Now ${px(a.price)}${a.r_now != null ? ` (${a.r_now > 0 ? "+" : ""}${a.r_now}R)` : ""}.</p>` : "";
    const more = list.length > 1 ? `<p class="d-copy">Also active: ${list.slice(1).map((x) => `${x.timeframe} ${x.side.toLowerCase()}`).join(", ")}.</p>` : "";
    const fourH = d.engine && !d.engine.active && d.asset !== "forex" ? `<p class="d-copy c-dim">The 4-hour engine has no trade on ${esc(base(S.coin))} right now.</p>` : "";
    $("sig-body").innerHTML = `
      <div class="d-tiles" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr))">
        <div><span class="c-dim">Entry</span> <b class="tnum">${px(a.entry)}</b></div>
        <div><span class="c-dim">Stop</span> <b class="tnum c-short">${px(a.stop_now != null ? a.stop_now : a.stop_loss)}</b></div>${tps}
      </div>
      ${now}${more}${fourH}
      <p class="d-copy">Close a third at each target; after TP1 move the stop to the entry, after TP2 to TP1.</p>
      <div class="d-actions"><a class="d-btn primary" href="/signals?market=${esc(a.market)}&timeframe=${esc(a.timeframe)}">Open on the signals page</a></div>`;
  }

  function renderWait(d) {
    const e = d.engine;
    const close = Math.max(0, Math.min(100, e.strength || 0));
    const lean = e.bias === "LONG" ? "long" : "short";
    const leanPct = e.bias === "LONG" ? e.p_long : e.p_short;
    const pill = $("sig-pill");
    $("sig-prob").hidden = true;
    if (e.covered === false) {
      setState("WAIT", "No signals for this coin", `${base(S.coin)} is not in the signal list. You get the chart and the market brief.`, "");
      pill.hidden = true;
      $("sig-body").innerHTML = `
        <p class="d-copy">The model leans <b class="${lean === "long" ? "c-long" : "c-short"}">${lean}</b>${tip("leaning", "leaning")} on ${esc(base(S.coin))}, but this coin is not in the signal list, so it gets no trades here.</p>
        <div class="d-actions"><a class="d-btn primary" href="#others" id="go-others">See coins with signals</a></div>`;
      return;
    }
    setState("WAIT", "Wait", `No signal on ${base(S.coin)} right now. Next check at ${clock(e.next_update)}.`, "wait");
    pill.hidden = !e.setup_forming;
    pill.className = "d-pill";
    pill.innerHTML = "Setup forming" + tip("setup_forming", "setup forming");
    const last = e.last_closed;
    const how = (t) => ({ TP3: "reached all three targets", TP2: "reached TP2, then closed at TP1", TP1: "reached TP1, then closed at the entry",
                          SL: "hit its stop", EXPIRED: "closed at the time limit" }[t.status] || "closed");
    const lastTxt = last ? `Last signal: ${last.side.toLowerCase()} from ${when(last.signal_at)}, ${how(last)} (${signed(last.r, 2)}R after fees).` : "";
    $("sig-body").innerHTML = `
      <div class="d-meter" aria-label="How close the model is to a strong signal">
        <div class="d-meter-bar"><span class="d-meter-fill" style="width:${close}%"></span><span class="d-meter-goal"></span></div>
        <div class="d-meter-lbl"><span>${Math.round(close)}% of the way to a strong signal${tip("closeness", "closeness to a signal")}</span><span>signal</span></div>
      </div>
      <p class="d-copy">The model leans <b class="${lean === "long" ? "c-long" : "c-short"}">${lean}</b>${tip("leaning", "leaning")} (${leanPct}% estimated chance of reaching target 1).
        ${e.setup_forming ? "It is close, so keep an eye on the next 4-hour close. This is not a trade yet." : "The engine reads every coin in the list together and rarely goes a day without a signal on one of them."}</p>
      ${lastTxt ? `<p class="d-copy">${esc(lastTxt)}</p>` : ""}
      ${liveElsewhere()}
      <div class="d-actions"><a class="d-btn primary" href="#others" id="go-others">See coins with signals</a></div>`;
    wireJump();
  }
  // "Active now" buttons on the Wait card: up to three live signals on other coins (any timeframe)
  function liveElsewhere() {
    const list = [];
    ((S.board && S.board.rows) || []).forEach((r) => { if (r.state === "ACTIVE" && r.symbol !== S.coin) list.push({ symbol: r.symbol, side: r.active.side, timeframe: "4h", at: r.active.signal_at }); });
    streamsActive().forEach((x) => { if (x.symbol !== S.coin) list.push({ symbol: x.symbol, side: x.side, timeframe: x.timeframe, at: x.signal_at }); });
    if (!list.length) return "";
    list.sort((a, b) => toDate(b.at) - toDate(a.at));
    const seen = new Set();
    const top = list.filter((x) => (seen.has(x.symbol) ? false : seen.add(x.symbol))).slice(0, 3);
    return `<p class="d-copy" style="margin-bottom:6px">Live signals on other ${list.length === 1 ? "symbol" : "symbols"} right now:</p>
      <div class="d-actions" style="margin-top:0">${top.map((x) => `<button type="button" class="d-btn" data-jump="${esc(x.symbol)}">${esc(base(x.symbol))} · ${esc(x.timeframe)} ${x.side === "LONG" ? "long" : "short"}</button>`).join("")}</div>`;
  }
  function wireJump() {
    document.querySelectorAll("#sig-body [data-jump]").forEach((el) => el.addEventListener("click", () => {
      selectCoin(el.dataset.jump);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }));
  }

  function levelsOf(a) {
    const tps = a.targets && a.targets.length ? a.targets : [a.take_profit];
    const hits = a.hits || 0;
    const stopNow = a.stop_now != null ? a.stop_now : a.stop_loss;
    return { tps, hits, stopNow, moved: hits > 0 && Math.abs(stopNow - a.stop_loss) > 1e-12 };
  }

  function renderActive(d) {
    const e = d.engine, a = e.active, p = e.progress || {};
    const long = a.side === "LONG";
    const L = levelsOf(a);
    setState(a.side, long ? "Long" : "Short",
      `Started ${ago(a.signal_at)} at the ${clock(a.signal_at)} close. Closes in ${left(a.expires_at)} at the latest.`, long ? "long" : "short");
    const pill = $("sig-pill");
    pill.hidden = false;
    pill.className = e.fresh ? "d-pill new" : "d-pill";
    pill.innerHTML = (e.fresh ? "New signal" : "Active signal") + ` · ${a.tier === "standard" ? "Standard" : "Strong"}` + tip("tier", "strong and standard signals");
    $("sig-prob").hidden = false;
    $("sig-prob-v").textContent = `${Math.round(a.confidence)}%`;
    $("sig-prob-l").innerHTML = "chance of TP1" + tip("win_chance", "win chance");

    const now = p.price != null ? p.price : e.last_close;
    const all = L.tps.concat([a.stop_loss, a.entry]);
    const hi = Math.max(...all), lo = Math.min(...all);
    const pad = (hi - lo) * 0.07;
    const top = Math.max(hi + pad, now), bot = Math.min(lo - pad, now);
    const y = (v) => ((top - v) / (top - bot)) * 100;
    const yE = y(a.entry), yS = y(L.stopNow), yN = y(now), yLast = y(L.tps[L.tps.length - 1]);
    const toward = (now - a.entry) * (long ? 1 : -1) >= 0;
    const pctFrom = (v) => signed(((v / now) - 1) * 100, 2) + "%";
    const rOf = (k) => (a.targets_r || [0.5, 1, 2])[k];
    let state = "";
    if (p.state === "target_touched") state = `<b class="c-long">TP${p.next} touched</b>, confirms at the 4-hour close`;
    else if (p.state === "stop_touched") state = `<b class="c-short">Stop touched</b>, confirms at the 4-hour close`;
    const tpRows = L.tps.map((v, k) => {
      const done = k < L.hits;
      return `<div class="d-lv${done ? " done" : ""}" style="top:${y(v)}%"><span>TP${k + 1}${done ? " ✓" : ""}</span><b class="tnum c-long">${price(v)}</b><small class="c-dim tnum">${pctFrom(v)} · +${rOf(k)}R</small></div>`;
    }).join("");
    const stopLbl = L.moved ? (L.hits === 1 ? "Stop (at entry)" : "Stop (at TP1)") : "Stop";
    const fr = (a.fractions || [1 / 3]).map((x) => Math.round(x * 100));
    const plan = L.tps.length > 1 ? `Close ${fr[0]}% at each target. After TP1 the stop moves to the entry, after TP2 to TP1.` : "";
    const ifStop = L.hits === 0 ? "−1R" : L.hits === 1 ? `+${(rOf(0) * (fr[0] / 100)).toFixed(2)}R` : `+${((rOf(0) + rOf(1) + rOf(0)) / 3).toFixed(2)}R`;
    const allR = L.tps.reduce((acc, _v, k) => acc + rOf(k) * ((a.fractions || [])[k] || 1 / L.tps.length), 0);
    $("sig-body").innerHTML = `
      <div class="d-ladder d-ladder-3" role="img" aria-label="${esc(`Price ${price(now)}, entry ${price(a.entry)}, targets ${L.tps.map((v) => price(v)).join(", ")}, stop ${price(L.stopNow)}`)}">
        <div style="position:relative">
          <div class="d-lv left now" style="top:${yN}%"><span>Now</span><b class="tnum">${price(now)}</b></div>
        </div>
        <div class="d-track">
          <span class="d-zone win" style="top:${Math.min(yLast, yE)}%;height:${Math.abs(yE - yLast)}%"></span>
          <span class="d-zone loss" style="top:${Math.min(yS, yE)}%;height:${Math.abs(yE - yS)}%"></span>
          <span class="d-fill" style="top:${Math.min(yE, yN)}%;height:${Math.abs(yN - yE)}%;--fill:${toward ? "var(--long)" : "var(--short)"}"></span>
          ${L.tps.map((v) => `<span class="d-tick" style="top:${y(v)}%"></span>`).join("")}<span class="d-tick" style="top:${yE}%"></span><span class="d-tick" style="top:${yS}%"></span>
          <span class="d-now" style="top:${yN}%"></span>
        </div>
        <div style="position:relative">
          ${tpRows}
          <div class="d-lv" style="top:${yE}%"><span>Entry</span><b class="tnum">${price(a.entry)}</b></div>
          <div class="d-lv" style="top:${yS}%"><span>${stopLbl}</span><b class="tnum c-short">${price(L.stopNow)}</b><small class="c-dim tnum">${pctFrom(L.stopNow)}</small></div>
        </div>
      </div>
      ${state ? `<p class="d-copy">${state}</p>` : ""}
      <dl class="d-facts">
        <div><dt>Since entry${tip("entry", "entry")}</dt><dd class="tnum ${p.move_pct >= 0 ? "c-long" : "c-short"}">${p.move_pct == null ? "--" : signed(p.move_pct) + "%"}</dd></div>
        <div><dt>Progress${tip("progress", "progress")}</dt><dd>${p.pct == null ? "--" : `${Math.abs(p.pct)}% to ${p.pct >= 0 ? "TP" + p.next : "stop"}`}</dd></div>
        <div><dt>Targets reached${tip("targets", "targets reached")}</dt><dd class="${L.hits ? "c-long" : ""}">${L.hits} of ${L.tps.length}</dd></div>
        <div><dt>If the stop is hit now${tip("stop_loss", "stop loss")}</dt><dd class="${L.hits ? "c-long" : "c-short"}">${ifStop}</dd></div>
        <div><dt>If every target is hit${tip("target", "targets")}</dt><dd class="c-long">+${allR.toFixed(2)}R</dd></div>
        <div><dt>Stop distance</dt><dd class="tnum">${e.sl_pct != null ? e.sl_pct.toFixed(2) + "%" : "--"}</dd></div>
      </dl>
      <p class="d-copy">${plan} 1R${tip("r", "R")} is what you risk. Keep it small, for example 1% of your account per signal.</p>
      <div class="d-actions">
        <button class="d-btn" type="button" id="copy-levels">Copy levels</button>
        <a class="d-btn primary" href="/demo-trading">Practice in demo</a>
      </div>`;
    $("copy-levels").addEventListener("click", () => {
      const txt = `${S.coin} ${a.side} (Signals FM, 4h)\nEntry ${price(a.entry)}\nStop ${price(a.stop_loss)}\n` +
        L.tps.map((v, k) => `TP${k + 1} ${price(v)}`).join("\n") + (plan ? `\n${plan}` : "");
      (navigator.clipboard ? navigator.clipboard.writeText(txt) : Promise.reject()).then(() => toast("Levels copied")).catch(() => toast("Copy is not available in this browser"));
    });
  }

  function renderBrief(b) {
    if (!b) { $("brief-card").hidden = true; return; }
    $("brief-card").hidden = false;
    $("brief-time").textContent = b.bar_time ? `4h close ${clock(new Date(toDate(b.bar_time).getTime() + 4 * 3600e3).toISOString())}` : "";
    const trendTxt = { up: "Up: price is above its 50- and 200-candle averages", down: "Down: price is below its 50- and 200-candle averages",
                       sideways: "Sideways: the averages disagree" }[b.trend] || "--";
    const mom = b.rsi == null ? "--" : `RSI ${b.rsi}, ${{ overbought: "overbought", oversold: "oversold", strong: "buyers in control", weak: "sellers in control", neutral: "neutral" }[b.momentum]}`;
    const vol = b.volatility ? `${b.volatility[0].toUpperCase() + b.volatility.slice(1)}, about ${b.atr_pct}% per 4-hour candle` : (b.atr_pct ? `About ${b.atr_pct}% per 4-hour candle` : "--");
    const rows = [
      ["Trend" + tip("trend", "trend"), `<b class="${b.trend === "up" ? "c-long" : b.trend === "down" ? "c-short" : ""}">${esc(trendTxt)}</b>`],
      ["Daily trend" + tip("daily_trend", "daily trend"), b.daily_trend ? `<b class="${b.daily_trend === "up" ? "c-long" : "c-short"}">${b.daily_trend === "up" ? "Up" : "Down"}</b>` : "--"],
      ["Momentum" + tip("momentum", "momentum"), esc(mom)],
      ["Volatility" + tip("volatility", "volatility"), esc(vol)],
      ["Recent range" + tip("range", "recent range"), `High <b class="tnum">${price(b.range_high)}</b> (${signed(b.to_high_pct)}%), low <b class="tnum">${price(b.range_low)}</b> (${signed(b.to_low_pct)}%)`],
    ];
    $("brief").innerHTML = rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");
  }

  function renderProof(t) {
    if (!t || !t.signals || S.kind !== "crypto") { $("proof").hidden = true; return; }
    $("proof").hidden = false;
    const mon = (x) => { const d = new Date(x + "T00:00:00Z"); return isNaN(d) ? x : d.toLocaleDateString("en-US", { month: "short", year: "numeric", timeZone: "UTC" }); };
    const per = (t.period || "").split(" to ");
    const span = per.length === 2 ? `${mon(per[0])} to ${mon(per[1])}` : "Oct 2025 onward";
    $("proof-text").textContent = `The model was trained on Jul 2020 to Sep 2025, then tested once on ${span}, data it had never seen: ${t.coins} coins, one signal at a time per coin, fees included. About ${t.signals_per_day} signals a day; the longest wait for a new one was ${t.max_gap_days} days.`;
    $("proof-stats").innerHTML = `
      <div><b class="tnum">${t.win_rate}%</b><span>signals won${tip("win_rate", "win rate")}</span></div>
      <div><b class="tnum">${t.tp1_rate}%</b><span>reached TP1${tip("targets", "targets reached")}</span></div>
      <div><b class="tnum">${signed(t.avg_net_r, 3)}R</b><span>average per signal${tip("avg_r", "average result")}</span></div>
      <div><b class="tnum">${t.signals}</b><span>signals tested${tip("backtest", "tested signals")}</span></div>`;
  }

  // ---------------------------------------------------------------- chart
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function chartColors() {
    return {
      layout: { background: { color: "transparent" }, textColor: css("--dim"), fontFamily: "Inter, system-ui, sans-serif", attributionLogo: false },
      grid: { vertLines: { color: css("--line-soft") }, horzLines: { color: css("--line-soft") } },
      rightPriceScale: { borderColor: css("--line") }, timeScale: { borderColor: css("--line"), timeVisible: true, secondsVisible: false },
    };
  }
  function ensureChart() {
    if (S.chart || !window.LightweightCharts) return !!S.chart;
    const el = $("chart");
    S.chart = LightweightCharts.createChart(el, { autoSize: true, ...chartColors(), crosshair: { mode: 0 } });
    S.series = S.chart.addSeries(LightweightCharts.CandlestickSeries, {
      upColor: css("--long"), downColor: css("--short"), borderVisible: false, wickUpColor: css("--long"), wickDownColor: css("--short"),
    });
    // endless history: older candles load as the chart is scrolled left (static/chart-history.js)
    if (window.SFMHistory) {
      S.hist = window.SFMHistory.attach(S.chart, {
        coin: () => S.coin, tf: () => S.tf, oldest: () => (S.bars.length ? S.bars[0].time : null), count: () => S.bars.length,
        prepend: (older) => { S.bars = older.map((c) => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close })).concat(S.bars); S.series.setData(S.bars); },
      });
    }
    document.addEventListener("themechange", () => {
      S.chart.applyOptions(chartColors());
      S.series.applyOptions({ upColor: css("--long"), downColor: css("--short"), wickUpColor: css("--long"), wickDownColor: css("--short") });
      drawLevels();
    });
    return true;
  }
  async function loadChart() {
    const sym = S.coin, tf = S.tf;
    const msg = $("chart-msg");
    msg.hidden = false; msg.textContent = "Loading chart…";
    if (!ensureChart()) { msg.textContent = "The chart library could not load. Check your connection and refresh."; return; }
    let d;
    try { d = await getJSON(`/candles?coin=${encodeURIComponent(sym)}&timeframe=${tf}&limit=300`); }
    catch (e) { if (sym === S.coin) msg.textContent = "The chart could not load. It retries in 30 seconds."; return; }
    if (sym !== S.coin || tf !== S.tf) return;
    if (d.error || !d.candles || !d.candles.length) { msg.textContent = d.error ? `Chart data is not available: ${d.error}` : "No candles for this market yet."; S.series.setData([]); return; }
    const dec = decimals(d.candles[d.candles.length - 1].close, sym);
    S.series.applyOptions({ priceFormat: { type: "price", precision: dec, minMove: Math.pow(10, -dec) } });
    S.bars = d.candles.map((c) => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close }));
    if (S.hist) S.hist.reset();
    S.series.setData(S.bars);
    S.chart.timeScale().fitContent();
    msg.hidden = true;
    drawLevels();
  }
  async function refreshChart() {
    if (!S.series || !visible()) return;
    const sym = S.coin, tf = S.tf;
    try {
      const d = await getJSON(`/candles?coin=${encodeURIComponent(sym)}&timeframe=${tf}&limit=2`);
      if (sym !== S.coin || tf !== S.tf || !d.candles) return;
      d.candles.forEach((c) => {
        const b = { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close };
        const last = S.bars[S.bars.length - 1];
        if (last && b.time === last.time) S.bars[S.bars.length - 1] = b; else if (!last || b.time > last.time) S.bars.push(b);
        try { S.series.update(b); } catch (e) {}
      });
      if (d.last_price != null && !(S.eng && S.eng.price != null)) $("coin-price").textContent = price(d.last_price);
    } catch (e) {}
  }
  function drawLevels() {
    if (!S.series) return;
    S.lines.forEach((l) => { try { S.series.removePriceLine(l); } catch (e) {} });
    S.lines = [];
    const a = S.eng && S.eng.engine && S.eng.engine.active;
    if (!a || S.eng.coin !== S.coin) return;
    const add = (p, color, title) => S.lines.push(S.series.createPriceLine({ price: p, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title }));
    const L = levelsOf(a);
    L.tps.forEach((v, k) => add(v, css("--long"), `TP${k + 1}${k < L.hits ? " ✓" : ""}`));
    add(a.entry, css("--dim"), "Entry");
    add(L.stopNow, css("--short"), L.moved ? "Stop (moved)" : "Stop");
  }
  document.querySelectorAll(".d-tf button").forEach((b) => b.addEventListener("click", () => {
    S.tf = b.dataset.tf;
    document.querySelectorAll(".d-tf button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
    updateProLinks();
    loadChart();
  }));

  // ---------------------------------------------------------------- other signals (board)
  async function loadBoard() {
    try { S.board = await getJSON("/api/signals/board"); } catch (e) { return; }
    renderOthers();
    if (S.board && S.board.loading) setTimeout(loadBoard, 6000);
  }
  function mini(r) {
    const a = r.active, p = r.progress || {};
    const pos = p.pct == null ? 0 : p.pct;
    const bar = pos >= 0
      ? `<i style="left:50%;width:${pos / 2}%;background:var(--long)"></i>`
      : `<i style="right:50%;width:${-pos / 2}%;background:var(--short)"></i>`;
    const side = a.side === "LONG" ? "Long" : "Short";
    return `<button type="button" class="d-mini" data-sym="${esc(r.symbol)}" aria-current="${r.symbol === S.coin}">
      <span class="d-mini-top"><b>${esc(base(r.symbol))}</b><span class="d-pill ${r.fresh ? "new" : ""}" style="${r.fresh ? "" : `background:var(--${a.side === "LONG" ? "long" : "short"}-bg);color:var(--${a.side === "LONG" ? "long" : "short"})`}">${r.fresh ? "New " + side.toLowerCase() : side}</span></span>
      <span class="d-mini-bar">${bar}</span>
      <small>${p.move_pct == null ? "started " + ago(a.signal_at) : `${signed(p.move_pct)}% since entry, ${Math.abs(p.pct)}% to ${p.pct >= 0 ? "TP" + (p.next || 1) : "stop"}${a.hits ? ` · TP${a.hits} ✓` : ""}`}</small>
    </button>`;
  }
  function miniStream(x) {
    const long = x.side === "LONG";
    const hits = x.hits ? ` · TP${x.hits} ✓` : "";
    return `<button type="button" class="d-mini" data-sym="${esc(x.symbol)}" aria-current="${x.symbol === S.coin}">
      <span class="d-mini-top"><b>${esc(base(x.symbol))}</b><span class="d-pill" style="background:var(--${long ? "long" : "short"}-bg);color:var(--${long ? "long" : "short"})">${esc(x.timeframe)} ${long ? "long" : "short"}</span></span>
      <span class="d-mini-bar"><i style="left:0;width:${x.grade === "A" ? 100 : x.grade === "B" ? 70 : 40}%;background:var(--${long ? "long" : "short"})"></i></span>
      <small>Grade ${esc(x.grade || "C")} · started ${ago(x.signal_at)}${hits}</small>
    </button>`;
  }
  function renderOthers() {
    const box = $("others-list");
    const b = S.board;
    if (!b) return;
    if (b.loading) { box.innerHTML = `<p class="d-empty">Checking every coin. This takes a few seconds after a restart.</p>`; return; }
    const rows = b.rows || [];
    const act = rows.filter((r) => r.state === "ACTIVE");
    const forming = rows.filter((r) => r.state === "WAIT" && r.setup_forming);
    const w = b.same_side_warning;
    $("others-warn").hidden = !w;
    if (w) $("others-warn").textContent = `${w.count} of ${w.total} active signals are ${w.side.toLowerCase()}. Coins tend to move together, so these can win or lose at the same time. Don't put full risk on all of them.`;
    const others = streamsActive();
    let html = act.map(mini).join("") + others.slice(0, 12).map(miniStream).join("");
    if (!act.length && !others.length) {
      html = `<p class="d-empty">No active signals right now. Each stream checks every symbol at its candle close (15 minutes, 1 hour, 4 hours).${forming.length ? " Closest to a strong 4-hour signal:" : ""}</p>`;
    }
    if (forming.length) {
      html += forming.slice(0, 4).map((r) => `<button type="button" class="d-mini" data-sym="${esc(r.symbol)}" aria-current="${r.symbol === S.coin}">
        <span class="d-mini-top"><b>${esc(base(r.symbol))}</b><span class="d-pill" style="background:var(--amber-bg);color:var(--amber)">Setup forming</span></span>
        <span class="d-mini-bar"><i style="left:0;width:${Math.min(100, r.strength || 0)}%;background:var(--amber)"></i></span>
        <small>Leaning ${String(r.bias || "").toLowerCase()}, not a trade yet</small></button>`).join("");
    }
    box.innerHTML = html;
    box.querySelectorAll(".d-mini").forEach((el) => el.addEventListener("click", () => {
      selectCoin(el.dataset.sym);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }));
  }

  // ---------------------------------------------------------------- coin sheet
  function statusPill(sym) {
    const r = boardRow(sym) || {};
    if (!r.state && !streamFor(sym)) return "";
    if (r.state === "ACTIVE") {
      const s = r.active.side;
      return `<span class="d-pill" style="background:var(--${s === "LONG" ? "long" : "short"}-bg);color:var(--${s === "LONG" ? "long" : "short"})">${r.fresh ? "New " + (s === "LONG" ? "long" : "short") : (s === "LONG" ? "Long" : "Short")}</span>`;
    }
    const o = streamFor(sym);
    if (o) {
      const l = o.side === "LONG";
      return `<span class="d-pill" style="background:var(--${l ? "long" : "short"}-bg);color:var(--${l ? "long" : "short"})">${esc(o.timeframe)} ${l ? "long" : "short"}</span>`;
    }
    if (r.setup_forming) return `<span class="d-pill" style="background:var(--amber-bg);color:var(--amber)">Forming</span>`;
    return "";
  }
  function renderList() {
    const q = $("coin-search").value.trim().toUpperCase();
    const list = (S.coins[S.sheetKind] || []).filter((s) => !q || s.replace("/", "").includes(q.replace("/", "")) || s.includes(q));
    if (!list.length) { $("coin-list").innerHTML = `<p class="d-empty" style="padding:16px 6px">Nothing matches “${esc(q)}”.</p>`; return; }
    const sorted = list.slice().sort((a, b) => {
      const rank = (s) => { const r = boardRow(s); return (r && r.state === "ACTIVE") || streamFor(s) ? 0 : r && r.setup_forming ? 1 : 2; };
      return rank(a) - rank(b);
    });
    $("coin-list").innerHTML = sorted.map((s) => `<button type="button" class="d-row" data-sym="${esc(s)}">
      <img alt="" data-icon="${esc(s)}"><span><b>${esc(s)}</b><small>${S.sheetKind === "forex" ? (s.startsWith("XAU") ? "Gold" : "Forex") : "Crypto"}</small></span>${statusPill(s)}</button>`).join("");
    $("coin-list").querySelectorAll("img[data-icon]").forEach((img) => setIcon(img, img.dataset.icon));
    $("coin-list").querySelectorAll(".d-row").forEach((el) => el.addEventListener("click", () => { closeSheet(); selectCoin(el.dataset.sym); }));
  }
  function openSheet() {
    S.sheetKind = S.kind;
    document.querySelectorAll(".d-tabs2 button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.kind === S.sheetKind ? "true" : "false"));
    $("coin-search").value = "";
    $("coin-sheet").hidden = false;
    renderList();
    setTimeout(() => $("coin-search").focus(), 30);
  }
  function closeSheet() { $("coin-sheet").hidden = true; $("coin-btn").focus(); }
  $("coin-btn").addEventListener("click", openSheet);
  $("coin-close").addEventListener("click", closeSheet);
  $("coin-sheet").addEventListener("click", (e) => { if (e.target === $("coin-sheet")) closeSheet(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("coin-sheet").hidden) closeSheet(); });
  $("coin-search").addEventListener("input", renderList);
  document.querySelectorAll(".d-tabs2 button").forEach((b) => b.addEventListener("click", () => {
    S.sheetKind = b.dataset.kind;
    document.querySelectorAll(".d-tabs2 button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
    renderList();
  }));

  // ---------------------------------------------------------------- start
  async function start() {
    try {
      const c = await getJSON("/coins");
      S.coins = { crypto: c.crypto || [], forex: c.forex || [] };
    } catch (e) { S.coins = { crypto: ["BTC/USDT"], forex: [] }; }
    // the board decides the default coin (one with a live signal), but never hold the page for long
    await Promise.race([loadBoard(), new Promise((r) => setTimeout(r, 2500))]);
    selectCoin(pickDefault());
    setInterval(() => { if (visible()) loadEngine(true); }, 30000);
    setInterval(() => { if (visible()) loadBoard(); }, 60000);
    setInterval(() => { if (visible()) loadInternals(true); }, 60000);
    setInterval(refreshChart, 30000);
    document.addEventListener("visibilitychange", () => { if (visible()) { loadEngine(true); refreshChart(); } });
  }
  start();
})();
