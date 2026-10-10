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
    return (fresh || act || {}).symbol || "BTC/USDT";
  }

  function selectCoin(sym) {
    S.coin = sym;
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
    if (d.asset === "forex") renderForex(d);
    else if (!d.engine || d.engine.error || d.error) renderError((d.engine && d.engine.error) || d.error || "The signal engine did not answer.");
    else if (d.engine.active) renderActive(d);
    else renderWait(d);
    const old = document.querySelector("#sig-body .d-untested");
    if (old) old.remove();
    if (d.asset === "crypto" && d.tested === false && d.engine && !d.engine.error && !d.error) {
      $("sig-body").insertAdjacentHTML("beforeend", `<p class="d-note d-untested">${esc(base(sym))} was not one of the 16 coins in the engine's test, so its results may differ from the tested numbers below.</p>`);
    }
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
    setState("FOREX", "Chart and market brief",
      d.error ? d.error : `Signals are made for crypto only for now. For ${gold ? "gold" : "forex pairs"} you get the live chart and a plain-language brief of trend, momentum and volatility.`);
    $("sig-pill").hidden = true;
    $("sig-prob").hidden = true;
    $("sig-body").innerHTML = `<div class="d-actions"><a class="d-btn" href="/signals">See crypto signals</a></div>`;
  }

  function renderWait(d) {
    const e = d.engine;
    const close = Math.max(0, Math.min(100, e.strength || 0));
    const lean = e.bias === "LONG" ? "long" : "short";
    const leanPct = e.bias === "LONG" ? e.p_long : e.p_short;
    setState("WAIT", "Wait", `No strong setup on ${base(S.coin)} right now. Next check at ${clock(e.next_update)}.`, "wait");
    const pill = $("sig-pill");
    pill.hidden = !e.setup_forming;
    pill.className = "d-pill";
    pill.innerHTML = "Setup forming" + tip("setup_forming", "setup forming");
    $("sig-prob").hidden = true;
    const last = e.last_closed;
    const lastTxt = last
      ? `Last signal: ${last.side.toLowerCase()} from ${when(last.signal_at)}, ${last.status === "TP" ? "hit its target" : last.status === "SL" ? "hit its stop" : "closed at the time limit"} (${signed(last.r, 2)}R after fees).`
      : "";
    $("sig-body").innerHTML = `
      <div class="d-meter" aria-label="How close the model is to a signal">
        <div class="d-meter-bar"><span class="d-meter-fill" style="width:${close}%"></span><span class="d-meter-goal"></span></div>
        <div class="d-meter-lbl"><span>${Math.round(close)}% of the way to a signal${tip("closeness", "closeness to a signal")}</span><span>signal</span></div>
      </div>
      <p class="d-copy">The model leans <b class="${lean === "long" ? "c-long" : "c-short"}">${lean}</b>${tip("leaning", "leaning")} (${leanPct}% estimated win chance), below the level it needs before it calls a trade.
        ${e.setup_forming ? "It is close, so keep an eye on the next 4-hour close. This is not a trade yet." : ""}</p>
      ${lastTxt ? `<p class="d-copy">${esc(lastTxt)}</p>` : ""}
      <div class="d-actions"><a class="d-btn primary" href="#others" id="go-others">See coins with signals</a></div>`;
  }

  function renderActive(d) {
    const e = d.engine, a = e.active, p = e.progress || {};
    const long = a.side === "LONG";
    setState(a.side, long ? "Long" : "Short",
      `Started ${ago(a.signal_at)} at the ${clock(a.signal_at)} close. Ends in ${left(a.expires_at)} if neither level is hit.`, long ? "long" : "short");
    const pill = $("sig-pill");
    pill.hidden = false;
    pill.className = e.fresh ? "d-pill new" : "d-pill";
    pill.textContent = e.fresh ? "New signal" : "Active signal";
    $("sig-prob").hidden = false;
    $("sig-prob-v").textContent = `${Math.round(a.confidence)}%`;
    $("sig-prob-l").innerHTML = "win chance" + tip("win_chance", "win chance");

    const now = p.price != null ? p.price : e.last_close;
    const hi = Math.max(a.stop_loss, a.take_profit), lo = Math.min(a.stop_loss, a.take_profit);
    const pad = (hi - lo) * 0.09;
    const top = Math.max(hi + pad, now), bot = Math.min(lo - pad, now);
    const y = (v) => ((top - v) / (top - bot)) * 100;
    const yT = y(a.take_profit), yE = y(a.entry), yS = y(a.stop_loss), yN = y(now);
    const toward = (now - a.entry) * (long ? 1 : -1) >= 0;
    const pctFrom = (v) => signed(((v / now) - 1) * 100, 2) + "%";
    let state = "";
    if (p.state === "target_touched") state = `<b class="c-long">Target touched</b>, confirms at the 4-hour close`;
    else if (p.state === "stop_touched") state = `<b class="c-short">Stop touched</b>, confirms at the 4-hour close`;
    $("sig-body").innerHTML = `
      <div class="d-ladder" role="img" aria-label="${esc(`Price ${price(now)}, entry ${price(a.entry)}, target ${price(a.take_profit)}, stop ${price(a.stop_loss)}`)}">
        <div style="position:relative">
          <div class="d-lv left now" style="top:${yN}%"><span>Now</span><b class="tnum">${price(now)}</b></div>
        </div>
        <div class="d-track">
          <span class="d-zone win" style="top:${Math.min(yT, yE)}%;height:${Math.abs(yE - yT)}%"></span>
          <span class="d-zone loss" style="top:${Math.min(yS, yE)}%;height:${Math.abs(yE - yS)}%"></span>
          <span class="d-fill" style="top:${Math.min(yE, yN)}%;height:${Math.abs(yN - yE)}%;--fill:${toward ? "var(--long)" : "var(--short)"}"></span>
          <span class="d-tick" style="top:${yT}%"></span><span class="d-tick" style="top:${yE}%"></span><span class="d-tick" style="top:${yS}%"></span>
          <span class="d-now" style="top:${yN}%"></span>
        </div>
        <div style="position:relative">
          <div class="d-lv" style="top:${yT}%"><span>Target</span><b class="tnum c-long">${price(a.take_profit)}</b><small class="c-dim tnum">${pctFrom(a.take_profit)}</small></div>
          <div class="d-lv" style="top:${yE}%"><span>Entry</span><b class="tnum">${price(a.entry)}</b></div>
          <div class="d-lv" style="top:${yS}%"><span>Stop</span><b class="tnum c-short">${price(a.stop_loss)}</b><small class="c-dim tnum">${pctFrom(a.stop_loss)}</small></div>
        </div>
      </div>
      ${state ? `<p class="d-copy">${state}</p>` : ""}
      <dl class="d-facts">
        <div><dt>Since entry${tip("entry", "entry")}</dt><dd class="tnum ${p.move_pct >= 0 ? "c-long" : "c-short"}">${p.move_pct == null ? "--" : signed(p.move_pct) + "%"}</dd></div>
        <div><dt>Progress${tip("progress", "progress")}</dt><dd>${p.pct == null ? "--" : `${Math.abs(p.pct)}% to ${p.pct >= 0 ? "target" : "stop"}`}</dd></div>
        <div><dt>If the target is hit${tip("target", "target")}</dt><dd class="c-long">+0.5R</dd></div>
        <div><dt>If the stop is hit${tip("stop_loss", "stop loss")}</dt><dd class="c-short">−1R</dd></div>
      </dl>
      <p class="d-copy">1R${tip("r", "R")} is what you risk. Keep it small, for example 1% of your account per signal.</p>
      <div class="d-actions">
        <button class="d-btn" type="button" id="copy-levels">Copy levels</button>
        <a class="d-btn primary" href="/demo-trading">Practice in demo</a>
      </div>`;
    $("copy-levels").addEventListener("click", () => {
      const txt = `${S.coin} ${a.side} (Signals FM, 4h)\nEntry ${price(a.entry)}\nStop ${price(a.stop_loss)}\nTarget ${price(a.take_profit)}`;
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
    const span = per.length === 2 ? `${mon(per[0])} to ${mon(per[1])}` : "Jul 2025 onward";
    $("proof-text").textContent = `The model was trained on Jan 2022 to Jun 2025, then tested once on ${span}, data it had never seen: one signal at a time per coin, ${t.coins} coins, fees included.`;
    $("proof-stats").innerHTML = `
      <div><b class="tnum">${t.win_rate}%</b><span>signals won${tip("win_rate", "win rate")}</span></div>
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
    add(a.take_profit, css("--long"), "Target");
    add(a.entry, css("--dim"), "Entry");
    add(a.stop_loss, css("--short"), "Stop");
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
      <small>${p.move_pct == null ? "started " + ago(a.signal_at) : `${signed(p.move_pct)}% since entry, ${Math.abs(p.pct)}% to ${p.pct >= 0 ? "target" : "stop"}`}</small>
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
    let html = act.map(mini).join("");
    if (!act.length) {
      html = `<p class="d-empty">No active signals right now. The engine checks every coin at each 4-hour close and only calls strong setups.${forming.length ? " Closest to a signal:" : ""}</p>`;
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
    const r = boardRow(sym);
    if (!r) return "";
    if (r.state === "ACTIVE") {
      const s = r.active.side;
      return `<span class="d-pill" style="background:var(--${s === "LONG" ? "long" : "short"}-bg);color:var(--${s === "LONG" ? "long" : "short"})">${r.fresh ? "New " + (s === "LONG" ? "long" : "short") : (s === "LONG" ? "Long" : "Short")}</span>`;
    }
    if (r.setup_forming) return `<span class="d-pill" style="background:var(--amber-bg);color:var(--amber)">Forming</span>`;
    return "";
  }
  function renderList() {
    const q = $("coin-search").value.trim().toUpperCase();
    const list = (S.coins[S.sheetKind] || []).filter((s) => !q || s.replace("/", "").includes(q.replace("/", "")) || s.includes(q));
    if (!list.length) { $("coin-list").innerHTML = `<p class="d-empty" style="padding:16px 6px">Nothing matches “${esc(q)}”.</p>`; return; }
    const sorted = S.sheetKind === "crypto" ? list.slice().sort((a, b) => {
      const rank = (s) => { const r = boardRow(s); return r && r.state === "ACTIVE" ? 0 : r && r.setup_forming ? 1 : 2; };
      return rank(a) - rank(b);
    }) : list;
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
