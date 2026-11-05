/* Signals FM dashboard: "AI agents at work" and "Live chart analysis" cards (templates/dashboard.html).
   Five agents each own one job. They read real data only: /candles for the coin on screen (1h, refreshed every 10 s)
   plus the engine and board events that app.js dispatches (sfm:coin, sfm:engine, sfm:board).
   Every number shown is computed from those candles; nothing is invented. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  if (!$("ag-list") || !$("la-canvas")) return;

  const reduce = (() => { try { return matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; } })();
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const S = { coin: null, bars: [], last: null, eng: null, board: null, loading: false, cycle: 0, an: null };

  // ------------------------------------------------------------ agents
  const AGENTS = [
    { id: "scout", name: "Scout", role: "Collects live candles", hue: 160 },
    { id: "trend", name: "Atlas", role: "Reads the trend", hue: 200 },
    { id: "pulse", name: "Pulse", role: "Measures momentum", hue: 275 },
    { id: "guard", name: "Guard", role: "Checks risk and volatility", hue: 35 },
    { id: "core", name: "Core", role: "Combines it with the signal engine", hue: 330 },
  ];
  function avatar(h) {
    // a small robot face; colours come from the agent's hue
    return `<svg viewBox="0 0 48 48" aria-hidden="true">
      <defs><linearGradient id="g${h}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(${h} 90% 62%)"/><stop offset="1" stop-color="hsl(${h + 40} 85% 45%)"/></linearGradient></defs>
      <line x1="24" y1="4" x2="24" y2="10" stroke="hsl(${h} 90% 70%)" stroke-width="2" stroke-linecap="round"/>
      <circle class="ag-ant" cx="24" cy="4" r="2.6" fill="hsl(${h} 95% 70%)"/>
      <rect x="7" y="10" width="34" height="28" rx="10" fill="url(#g${h})"/>
      <rect x="11" y="15" width="26" height="15" rx="6" fill="#06110d" opacity=".88"/>
      <rect class="ag-eye" x="15.5" y="19" width="5" height="6" rx="2.2" fill="hsl(${h} 100% 75%)"/>
      <rect class="ag-eye" x="27.5" y="19" width="5" height="6" rx="2.2" fill="hsl(${h} 100% 75%)"/>
      <rect x="4" y="19" width="3" height="9" rx="1.5" fill="hsl(${h} 70% 50%)"/><rect x="41" y="19" width="3" height="9" rx="1.5" fill="hsl(${h} 70% 50%)"/>
      <rect class="ag-mouth" x="19" y="33" width="10" height="2" rx="1" fill="#06110d" opacity=".6"/>
      <rect x="14" y="40" width="20" height="5" rx="2.5" fill="hsl(${h} 60% 35%)"/>
    </svg>`;
  }
  $("ag-list").innerHTML = AGENTS.map((a) => `
    <div class="ag-row" id="ag-${a.id}" data-st="idle" style="--h:${a.hue}">
      <div class="ag-av">${avatar(a.hue)}<span class="ag-ring"></span></div>
      <div class="ag-txt">
        <div class="ag-top"><b>${a.name}</b><span class="ag-role">${a.role}</span><span class="ag-st">Idle</span></div>
        <div class="ag-bar"><i></i></div>
        <small class="ag-out">Waiting for data…</small>
      </div>
    </div>`).join("");

  function setAgent(id, st, out) {
    const el = $("ag-" + id); if (!el) return;
    el.dataset.st = st;
    el.querySelector(".ag-st").textContent = { idle: "Idle", work: "Collecting", think: "Analyzing", done: "Done", err: "No data" }[st] || st;
    if (out != null) el.querySelector(".ag-out").innerHTML = out;
  }
  const clock = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  function log(agent, html) {
    const box = $("ag-feed");
    const a = AGENTS.find((x) => x.id === agent) || AGENTS[0];
    const line = document.createElement("div");
    line.className = "ag-line";
    line.style.setProperty("--h", a.hue);
    line.innerHTML = `<span class="t">${clock()}</span><b>${a.name}</b><span>${html}</span>`;
    box.appendChild(line);
    while (box.children.length > 40) box.firstChild.remove();
    box.scrollTop = box.scrollHeight;
  }
  const wait = (ms) => new Promise((r) => setTimeout(r, reduce ? 0 : ms));

  // ------------------------------------------------------------ maths on real candles
  function decimals(v) {
    const a = Math.abs(Number(v));
    return a >= 1000 ? 2 : a >= 10 ? 3 : a >= 1 ? 4 : a >= 0.01 ? 5 : 8;
  }
  const fmt = (v) => (v == null || isNaN(v) ? "--" : Number(v).toLocaleString("en-US", { maximumFractionDigits: decimals(v), minimumFractionDigits: Math.min(2, decimals(v)) }));
  function ema(vals, n) {
    const k = 2 / (n + 1), out = []; let e = null;
    vals.forEach((v, i) => { e = e == null ? v : v * k + e * (1 - k); out.push(i >= n - 1 ? e : null); });
    return out;
  }
  function rsi(vals, n = 14) {
    if (vals.length <= n) return null;
    let g = 0, l = 0;
    for (let i = 1; i <= n; i++) { const d = vals[i] - vals[i - 1]; if (d > 0) g += d; else l -= d; }
    g /= n; l /= n;
    for (let i = n + 1; i < vals.length; i++) {
      const d = vals[i] - vals[i - 1];
      g = (g * (n - 1) + Math.max(d, 0)) / n; l = (l * (n - 1) + Math.max(-d, 0)) / n;
    }
    return l === 0 ? 100 : 100 - 100 / (1 + g / l);
  }
  function atrPct(b, n = 14) {
    if (b.length <= n) return null;
    let s = 0;
    for (let i = b.length - n; i < b.length; i++) {
      const p = b[i - 1].close;
      s += Math.max(b[i].high - b[i].low, Math.abs(b[i].high - p), Math.abs(b[i].low - p));
    }
    return (s / n) / b[b.length - 1].close * 100;
  }
  function analyse(b) {
    const c = b.map((x) => x.close);
    const e20 = ema(c, 20), e50 = ema(c, 50);
    const win = b.slice(-48);
    const support = Math.min(...win.map((x) => x.low)), resistance = Math.max(...win.map((x) => x.high));
    const px = c[c.length - 1], f = e20[e20.length - 1], s = e50[e50.length - 1];
    const trend = f == null || s == null ? "sideways" : px > f && f > s ? "up" : px < f && f < s ? "down" : "sideways";
    const r = rsi(c), atr = atrPct(b);
    const chg24 = c.length > 24 ? (px / c[c.length - 25] - 1) * 100 : null;
    const pos = resistance > support ? (px - support) / (resistance - support) : 0.5;
    let score = 0;
    score += trend === "up" ? 1 : trend === "down" ? -1 : 0;
    score += r == null ? 0 : r > 55 ? 0.5 : r < 45 ? -0.5 : 0;
    score += chg24 == null ? 0 : chg24 > 0 ? 0.5 : -0.5;
    const bias = score >= 1 ? "Leaning long" : score <= -1 ? "Leaning short" : "No clear edge";
    return { e20, e50, support, resistance, px, trend, rsi: r, atr, chg24, pos, bias, score };
  }

  // ------------------------------------------------------------ data
  async function getJSON(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok && !d.error) d.error = `Request failed (${r.status})`;
    return d;
  }
  async function loadCandles() {
    const sym = S.coin;
    if (!sym || S.loading) return null;
    S.loading = true;
    try {
      const d = await getJSON(`/candles?coin=${encodeURIComponent(sym)}&timeframe=1h&limit=150`);
      if (sym !== S.coin) return null;
      if (d.error || !d.candles || d.candles.length < 30) return { error: d.error || "Not enough candles yet" };
      S.bars = d.candles.map((c) => ({ time: c.time, open: +c.open, high: +c.high, low: +c.low, close: +c.close }));
      S.last = d.last_price != null ? +d.last_price : S.bars[S.bars.length - 1].close;
      return { ok: true };
    } catch (e) { return { error: "Could not reach the server" }; }
    finally { S.loading = false; }
  }

  // ------------------------------------------------------------ one agent cycle
  let running = false;
  async function runCycle(full) {
    if (running || !S.coin || document.visibilityState === "hidden") return;
    running = true;
    const sym = S.coin, base = sym.split("/")[0];
    S.cycle++;
    $("ag-cycle").textContent = `Cycle ${S.cycle} · ${sym}`;
    try {
      setAgent("scout", "work");
      if (full) log("scout", `Connecting to the ${esc(sym)} feed…`);
      const res = await loadCandles();
      if (sym !== S.coin) return;
      if (!res || res.error) {
        setAgent("scout", "err", esc(res ? res.error : "No data"));
        if (full) log("scout", `No candles: ${esc(res ? res.error : "unknown")}`);
        return;
      }
      const a = analyse(S.bars);
      S.an = a;
      drawAnalysis(true);
      setAgent("scout", "done", `${S.bars.length} candles · last <b class="tnum">${fmt(S.last)}</b>`);
      log("scout", `Pulled ${S.bars.length} × 1h candles for ${esc(base)}. Last price <b class="tnum">${fmt(S.last)}</b>.`);

      await wait(500); setAgent("trend", "think");
      await wait(700);
      const tw = { up: "Uptrend: price above EMA 20 and EMA 50", down: "Downtrend: price below EMA 20 and EMA 50", sideways: "Sideways: averages are mixed" }[a.trend];
      setAgent("trend", "done", `<span class="${a.trend === "up" ? "c-long" : a.trend === "down" ? "c-short" : ""}">${tw}</span>`);
      if (full || S.cycle % 3 === 0) log("trend", `${tw}. Support <b class="tnum">${fmt(a.support)}</b>, resistance <b class="tnum">${fmt(a.resistance)}</b>.`);

      await wait(400); setAgent("pulse", "think");
      await wait(600);
      const mw = a.rsi == null ? "--" : a.rsi > 70 ? "overbought" : a.rsi < 30 ? "oversold" : a.rsi > 55 ? "buyers in control" : a.rsi < 45 ? "sellers in control" : "neutral";
      setAgent("pulse", "done", `RSI <b class="tnum">${a.rsi == null ? "--" : a.rsi.toFixed(1)}</b>, ${mw}${a.chg24 == null ? "" : ` · 24h <span class="${a.chg24 >= 0 ? "c-long" : "c-short"}">${a.chg24 >= 0 ? "+" : ""}${a.chg24.toFixed(2)}%</span>`}`);
      if (full || S.cycle % 2 === 0) log("pulse", `RSI 14 at ${a.rsi == null ? "--" : a.rsi.toFixed(1)}: ${mw}.`);

      await wait(400); setAgent("guard", "think");
      await wait(600);
      const vol = a.atr == null ? "--" : a.atr > 1.5 ? "high" : a.atr > 0.6 ? "normal" : "low";
      const room = a.pos > 0.85 ? "price is pressing resistance" : a.pos < 0.15 ? "price is sitting on support" : `price is ${Math.round(a.pos * 100)}% up the 2-day range`;
      setAgent("guard", "done", `Volatility ${vol} (${a.atr == null ? "--" : a.atr.toFixed(2)}% per hour) · ${room}`);
      if (full || S.cycle % 3 === 1) log("guard", `Average move ${a.atr == null ? "--" : a.atr.toFixed(2)}% per hour, ${room}.`);

      await wait(400); setAgent("core", "think");
      await wait(700);
      setAgent("core", "done", coreSummary(a));
      log("core", coreSummary(a));
    } finally { running = false; }
  }
  function coreSummary(a) {
    const e = S.eng && S.eng.coin === S.coin ? S.eng.data : null;
    const act = e && e.engine && e.engine.active;
    if (act) return `Engine has an active <b class="${act.side === "LONG" ? "c-long" : "c-short"}">${act.side === "LONG" ? "long" : "short"}</b> signal from entry <b class="tnum">${fmt(act.entry)}</b>. Agents read: ${a.bias.toLowerCase()}.`;
    const rows = (S.board && S.board.rows) || [];
    const n = rows.filter((r) => r.state === "ACTIVE").length, f = rows.filter((r) => r.setup_forming).length;
    const scan = rows.length ? ` Scanned ${rows.length} coins: ${n} active, ${f} forming.` : "";
    return `No engine signal on this coin. Agents read: <b>${a.bias.toLowerCase()}</b>.${scan}`;
  }

  // ------------------------------------------------------------ live analysis canvas
  const cv = $("la-canvas"), ctx = cv.getContext("2d");
  let scanX = 0, lastT = 0;
  function css(n, fb) { const v = getComputedStyle(document.documentElement).getPropertyValue(n).trim(); return v || fb; }
  function drawAnalysis() {
    const r = cv.getBoundingClientRect();
    if (!r.width) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (cv.width !== Math.round(r.width * dpr)) { cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = r.width, H = r.height;
    ctx.clearRect(0, 0, W, H);
    const a = S.an;
    // grid
    ctx.strokeStyle = "rgba(52,211,153,.07)"; ctx.lineWidth = 1;
    for (let x = 0; x < W; x += 32) { ctx.beginPath(); ctx.moveTo(x + .5, 0); ctx.lineTo(x + .5, H); ctx.stroke(); }
    for (let y = 0; y < H; y += 32) { ctx.beginPath(); ctx.moveTo(0, y + .5); ctx.lineTo(W, y + .5); ctx.stroke(); }
    if (!a || !S.bars.length) return;
    const N = Math.min(70, S.bars.length), b = S.bars.slice(-N), off = S.bars.length - N;
    const padR = 64, padT = 14, padB = 14;
    let lo = Math.min(...b.map((x) => x.low), a.support), hi = Math.max(...b.map((x) => x.high), a.resistance);
    const m = (hi - lo) * 0.06; lo -= m; hi += m;
    const y = (v) => padT + (hi - v) / (hi - lo) * (H - padT - padB);
    const step = (W - padR) / N, bw = Math.max(2, step * 0.62);
    const up = css("--long", "#34d399"), dn = css("--short", "#f4566c");
    // support / resistance
    const level = (v, col, label) => {
      ctx.setLineDash([5, 5]); ctx.strokeStyle = col; ctx.globalAlpha = .75;
      ctx.beginPath(); ctx.moveTo(0, y(v)); ctx.lineTo(W - padR, y(v)); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
      ctx.fillStyle = col; ctx.font = "600 10px Inter, sans-serif"; ctx.fillText(label, 6, y(v) - 4);
    };
    level(a.resistance, dn, "Resistance " + fmt(a.resistance));
    level(a.support, up, "Support " + fmt(a.support));
    // candles
    b.forEach((c, i) => {
      const x = i * step + step / 2, col = c.close >= c.open ? up : dn;
      ctx.strokeStyle = col; ctx.fillStyle = col;
      ctx.beginPath(); ctx.moveTo(x, y(c.high)); ctx.lineTo(x, y(c.low)); ctx.stroke();
      const t = y(Math.max(c.open, c.close)), h = Math.max(1, Math.abs(y(c.open) - y(c.close)));
      if (i === N - 1) { ctx.shadowColor = col; ctx.shadowBlur = 14; }
      ctx.fillRect(x - bw / 2, t, bw, h);
      ctx.shadowBlur = 0;
    });
    // EMAs
    const line = (arr, col) => {
      ctx.strokeStyle = col; ctx.lineWidth = 1.6; ctx.beginPath(); let s = false;
      for (let i = 0; i < N; i++) { const v = arr[off + i]; if (v == null) continue; const x = i * step + step / 2; s ? ctx.lineTo(x, y(v)) : ctx.moveTo(x, y(v)); s = true; }
      ctx.stroke(); ctx.lineWidth = 1;
    };
    line(a.e20, "#60a5fa"); line(a.e50, "#c084fc");
    // live price tag
    const py = y(S.last), lc = S.last >= b[N - 1].open ? up : dn;
    ctx.strokeStyle = lc; ctx.globalAlpha = .5; ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(W - padR, py); ctx.stroke(); ctx.globalAlpha = 1;
    ctx.fillStyle = lc; ctx.beginPath(); ctx.roundRect ? ctx.roundRect(W - padR + 4, py - 9, padR - 6, 18, 5) : ctx.rect(W - padR + 4, py - 9, padR - 6, 18); ctx.fill();
    ctx.fillStyle = "#04110b"; ctx.font = "700 10px 'IBM Plex Mono', monospace"; ctx.fillText(fmt(S.last), W - padR + 8, py + 3.5);
    // scanning beam (the agents "reading" the chart)
    if (!reduce) {
      const sx = scanX * (W - padR);
      const g = ctx.createLinearGradient(sx - 60, 0, sx, 0);
      g.addColorStop(0, "rgba(52,211,153,0)"); g.addColorStop(1, "rgba(52,211,153,.22)");
      ctx.fillStyle = g; ctx.fillRect(sx - 60, 0, 60, H);
      ctx.strokeStyle = "rgba(110,255,200,.8)"; ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, H); ctx.stroke();
      const i = Math.min(N - 1, Math.max(0, Math.floor(sx / step)));
      const c = b[i]; ctx.fillStyle = "rgba(110,255,200,.95)"; ctx.beginPath(); ctx.arc(i * step + step / 2, y(c.close), 3, 0, 7); ctx.fill();
    }
    // reading strip + badge
    $("la-badge").textContent = a.bias;
    $("la-badge").dataset.b = a.score >= 1 ? "long" : a.score <= -1 ? "short" : "flat";
  }
  function renderRead() {
    const a = S.an; if (!a) return;
    const cell = (k, v, cls) => `<div><span>${k}</span><b class="tnum ${cls || ""}">${v}</b></div>`;
    $("la-read").innerHTML =
      cell("Price", fmt(S.last)) +
      cell("Trend", a.trend[0].toUpperCase() + a.trend.slice(1), a.trend === "up" ? "c-long" : a.trend === "down" ? "c-short" : "") +
      cell("RSI 14", a.rsi == null ? "--" : a.rsi.toFixed(1)) +
      cell("Vol / hr", a.atr == null ? "--" : a.atr.toFixed(2) + "%") +
      cell("24h", a.chg24 == null ? "--" : (a.chg24 >= 0 ? "+" : "") + a.chg24.toFixed(2) + "%", a.chg24 >= 0 ? "c-long" : "c-short");
    $("la-time").textContent = "Updated " + clock();
  }
  function frame(t) {
    const dt = Math.min(0.1, (t - lastT) / 1000 || 0); lastT = t;
    scanX = (scanX + dt * 0.28) % 1;
    if (S.an && document.visibilityState !== "hidden") drawAnalysis();
    requestAnimationFrame(frame);
  }
  if (!reduce) requestAnimationFrame(frame);
  window.addEventListener("resize", () => drawAnalysis());

  // fast refresh of the analysis only (between full agent cycles)
  async function quick() {
    if (!S.coin || running || document.visibilityState === "hidden") return;
    const res = await loadCandles();
    if (res && res.ok) { S.an = analyse(S.bars); drawAnalysis(); renderRead(); }
  }

  // ------------------------------------------------------------ wiring to app.js
  let started = false;
  function onCoin(sym) {
    if (!sym || sym === S.coin) return;
    S.coin = sym; S.bars = []; S.an = null; S.cycle = 0;
    $("ag-feed").innerHTML = "";
    AGENTS.forEach((a) => setAgent(a.id, "idle", "Waiting for data…"));
    log("core", `New target: <b>${esc(sym)}</b>. Dispatching agents.`);
    runCycle(true).then(renderRead);
    if (!started) {
      started = true;
      setInterval(quick, 10000);
      setInterval(() => runCycle(false).then(renderRead), 30000);
    }
  }
  window.addEventListener("sfm:coin", (e) => onCoin(e.detail && e.detail.coin));
  window.addEventListener("sfm:engine", (e) => { S.eng = e.detail; onCoin(e.detail && e.detail.coin); });
  window.addEventListener("sfm:board", (e) => {
    const first = !S.board;
    S.board = e.detail;
    const rows = (S.board && S.board.rows) || [];
    if (first && rows.length) log("core", `Signal board online: ${rows.length} coins watched.`);
  });
})();
