/* Signals FM Premium chart workspace (templates/premium-chart.html, premium_chart.py) - "Premium chart Access".
   Up to 16 charts per tab. Each chart has its own coin (or a spread of two coins), interval (any time interval,
   seconds or ticks) and chart type (candles ... or Range, Renko, Kagi, Line break, Point & figure). One shared
   price feed keeps every chart of every open tab live (up to 200 charts); second and tick charts also follow the
   exchange's trades. Bar replay, CSV export and the bond screener are here too.
   Data: /coins, /api/premium/bars, /api/premium/trades, /api/premium/prices, /api/premium/bonds(/history).
   Pure functions (intervals, buckets, Renko, Kagi ...): static/premium-chart-core.js (window.PCC). */
(function () {
  "use strict";
  const F = window.SFM, P = window.PCC, LW = window.LightweightCharts;
  if (!F || !P) return;
  const $ = (id) => document.getElementById(id);
  const enc = encodeURIComponent;
  const esc = F.esc;

  const STORE_KEY = "sfm-premium-v1";
  const MAX_LIVE = 200;
  const LAYOUTS = [
    { key: "1", n: 1, cols: 1 }, { key: "2", n: 2, cols: 2 }, { key: "2v", n: 2, cols: 1 }, { key: "3", n: 3, cols: 3 },
    { key: "4", n: 4, cols: 2 }, { key: "6", n: 6, cols: 3 }, { key: "8", n: 8, cols: 4 }, { key: "9", n: 9, cols: 3 },
    { key: "12", n: 12, cols: 4 }, { key: "16", n: 16, cols: 4 },
  ];
  const TYPES = [
    ["candles", "Candles", "time"], ["hollow", "Hollow candles", "time"], ["bars", "Bars", "time"], ["line", "Line", "time"],
    ["area", "Area", "time"], ["heikin", "Heikin Ashi", "time"], ["range", "Range bars", "price"], ["renko", "Renko", "price"],
    ["kagi", "Kagi", "price"], ["linebreak", "Line break", "price"], ["pnf", "Point & figure", "price"],
  ];
  const TYPE_NAME = Object.fromEntries(TYPES.map((t) => [t[0], t[1]]));
  const TYPE_SHORT = { hollow: "Hollow", heikin: "Heikin Ashi", range: "Range", linebreak: "Line break", pnf: "P&F" };
  const PRICE_TYPES = new Set(TYPES.filter((t) => t[2] === "price").map((t) => t[0]));
  const IV_GROUPS = [
    ["Ticks", ["10T", "100T", "1000T"]],
    ["Seconds", ["1s", "5s", "10s", "15s", "30s", "45s"]],
    ["Minutes", ["1m", "3m", "5m", "15m", "30m", "45m"]],
    ["Hours", ["1h", "2h", "3h", "4h", "6h", "8h", "12h"]],
    ["Days, weeks, months", ["1D", "2D", "3D", "1W", "2W", "1M", "3M", "6M", "12M"]],
  ];
  const OP_SIGN = { "/": "÷", "-": "−", "+": "+", "*": "×" };
  const DEFAULTS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT", "ADA/USDT", "AVAX/USDT",
                    "LINK/USDT", "LTC/USDT", "DOT/USDT", "TRX/USDT", "BCH/USDT", "ATOM/USDT", "NEAR/USDT", "UNI/USDT"];
  const ICON = {
    dl: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
    max: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>',
    min: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/></svg>',
    caret: '<svg class="mk-caret" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>',
  };

  const W = {
    coins: { crypto: [], forex: [] }, layout: "1", sync: { symbol: false, interval: false, crosshair: true, time: false },
    cfgs: [], cells: [], active: 0, view: "charts", serverOffset: 0, replay: null, picker: null, pickFor: null,
    feature: null, allow: undefined, totals: { charts: 0, tabs: 1 }, liveOk: null, maxCell: null,
    bonds: { data: null, cc: "", mat: "", q: "", sort: "years", dir: 1, sel: null, loading: false, chart: null, series: null },
  };

  // ================================================================== helpers
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  function alpha(hex, a) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim());
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
  const serverNow = () => Date.now() / 1000 + W.serverOffset;
  function noteServer(t) { if (t) W.serverOffset = Number(t) - Date.now() / 1000; }
  const cfgOf = (cell) => W.cfgs[cell.i];
  const ivOf = (cell) => P.parseInterval(cfgOf(cell).iv) || P.parseInterval("1h");
  const fromTrades = (iv) => iv.kind === "second" || iv.kind === "tick";
  const isForex = (s) => F.isForex(s);
  const validSym = (s) => W.coins.crypto.includes(s) || W.coins.forex.includes(s);
  function pad(n) { return String(n).padStart(2, "0"); }
  function fmtStamp(sec, iv) {
    if (sec == null) return "";
    const d = new Date(sec * 1000);
    const day = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
    if (iv && (iv.unit === "D" || iv.unit === "W" || iv.unit === "M")) return day;
    const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    return iv && iv.kind !== "time" && iv.kind !== "month" ? `${day} ${hm}:${pad(d.getUTCSeconds())}` : `${day} ${hm}`;
  }
  function decimalsFor(cell, v) {
    const c = cfgOf(cell);
    return c.symB ? F.decimals(v) : F.decimals(v, c.sym);
  }
  function fmtPrice(cell, v) {
    if (v == null || isNaN(v)) return "--";
    const d = decimalsFor(cell, v);
    return Number(v).toLocaleString("en-US", { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d });
  }
  const fmtNum = (v) => (v == null ? "auto" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 10 }));
  function symLabel(c) { return c.symB ? `${F.base(c.sym)} ${OP_SIGN[c.op] || "÷"} ${F.base(c.symB)}` : c.sym; }
  function fileSym(c) {
    const s = (x) => x.replace("/", "-");
    return c.symB ? `${s(c.sym)}_${{ "/": "div", "-": "minus", "+": "plus", "*": "times" }[c.op]}_${s(c.symB)}` : s(c.sym);
  }
  function download(name, text) {
    const blob = new Blob(["﻿" + text], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
  }

  // ================================================================== settings
  function defaultCfg(i) {
    return { sym: DEFAULTS[i % DEFAULTS.length], symB: null, op: "/", iv: "1h", type: "candles", box: null, rev: 3, lines: 3, range: null, volume: true };
  }
  function cleanCfg(c, i) {
    const d = defaultCfg(i), o = Object.assign({}, d, c && typeof c === "object" ? c : {});
    if (typeof o.sym !== "string" || (W.coins.crypto.length && !validSym(o.sym))) o.sym = d.sym;
    if (o.symB && (typeof o.symB !== "string" || (W.coins.crypto.length && !validSym(o.symB)) || o.symB === o.sym)) o.symB = null;
    if (!OP_SIGN[o.op]) o.op = "/";
    if (!P.parseInterval(o.iv)) o.iv = "1h";
    o.iv = P.parseInterval(o.iv).name;
    if (!TYPE_NAME[o.type]) o.type = "candles";
    for (const k of ["box", "range"]) o[k] = Number(o[k]) > 0 ? Number(o[k]) : null;
    o.rev = Math.max(1, Math.min(10, parseInt(o.rev, 10) || 3));
    o.lines = Math.max(1, Math.min(10, parseInt(o.lines, 10) || 3));
    o.volume = o.volume !== false;
    return o;
  }
  function loadPrefs() {
    const s = F.store.getJSON(STORE_KEY, {}) || {};
    if (LAYOUTS.some((l) => l.key === s.layout)) W.layout = s.layout;
    if (s.sync && typeof s.sync === "object") for (const k in W.sync) if (typeof s.sync[k] === "boolean") W.sync[k] = s.sync[k];
    W.cfgs = Array.isArray(s.cfgs) ? s.cfgs.slice(0, 16) : [];
    W.active = Number.isInteger(s.active) ? s.active : 0;
  }
  const save = () => F.store.setJSON(STORE_KEY, { layout: W.layout, sync: W.sync, cfgs: W.cfgs.slice(0, 16), active: W.active });
  function applyUrl() {
    const q = new URLSearchParams(location.search);
    const coin = (q.get("coin") || "").toUpperCase(), iv = q.get("interval") || q.get("tf");
    if (!W.cfgs.length) W.cfgs.push(defaultCfg(0));
    const c0 = W.cfgs[Math.min(W.active, W.cfgs.length - 1)] || W.cfgs[0];
    if (coin) { c0.sym = coin; c0.symB = null; }
    if (iv && P.parseInterval(iv)) c0.iv = P.parseInterval(iv).name;
    const lay = q.get("layout");
    if (lay && LAYOUTS.some((l) => l.key === lay)) W.layout = lay;
    W.feature = q.get("feature");
    if (q.get("view") === "bonds") W.feature = "bonds";
  }

  // ================================================================== chart look
  function theme() {
    return {
      layout: { background: { type: "solid", color: "transparent" }, textColor: css("--dim"), fontFamily: "IBM Plex Mono, ui-monospace, monospace",
                fontSize: 11, attributionLogo: false },
      grid: { vertLines: { color: css("--line-soft") }, horzLines: { color: css("--line-soft") } },
      rightPriceScale: { borderColor: css("--line") },
      timeScale: { borderColor: css("--line"), timeVisible: true, secondsVisible: false, rightOffset: 4 },
      crosshair: { mode: 0, vertLine: { color: css("--faint"), labelBackgroundColor: css("--surface-2") },
                   horzLine: { color: css("--faint"), labelBackgroundColor: css("--surface-2") } },
    };
  }
  function tickMark(cell, t, type) {
    if (!cell.real) return null;
    const sec = cell.real[Math.round(t) - 1];
    if (sec == null) return "";
    const d = new Date(sec * 1000);
    if (type <= 1) return `${d.getUTCFullYear()}`;                                // years
    if (type === 2) return `${pad(d.getUTCDate())} ${d.toLocaleString("en-US", { month: "short", timeZone: "UTC" })}`;
    return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  }
  function crossTime(cell, t) {
    const iv = ivOf(cell);
    if (cell.real) { const sec = cell.real[Math.round(t) - 1]; return sec == null ? "" : fmtStamp(sec, iv) + " UTC"; }
    return fmtStamp(t, iv) + " UTC";
  }
  function priceFormat(cell) {
    const v = cell.view && cell.view.length ? cell.view[cell.view.length - 1].close : 1;
    const d = decimalsFor(cell, v);
    return { type: "custom", minMove: Math.pow(10, -d), formatter: (p) => fmtPrice(cell, p) };
  }

  // ================================================================== P&F glyphs (a series primitive draws the X and O boxes)
  class PnFView {
    constructor(src) { this.src = src; }
    renderer() {
      const src = this.src;
      return {
        draw(target) {
          const cell = src.cell, pnf = cell.pnf, chart = cell.chart, series = cell.main;
          if (!pnf || !pnf.cols.length || !chart || !series) return;
          target.useMediaCoordinateSpace(({ context: ctx }) => {
            const ts = chart.timeScale(), lr = ts.getVisibleLogicalRange();
            if (!lr) return;
            const x0 = ts.logicalToCoordinate(0), x1 = ts.logicalToCoordinate(1);
            const spacing = x0 != null && x1 != null ? Math.abs(x1 - x0) : 8;
            const up = css("--long"), down = css("--short");
            ctx.lineWidth = Math.max(1, Math.min(2.2, spacing / 8));
            const from = Math.max(0, Math.floor(lr.from) - 1), to = Math.min(pnf.cols.length - 1, Math.ceil(lr.to) + 1);
            for (let i = from; i <= to; i++) {
              const col = pnf.cols[i], x = ts.logicalToCoordinate(i);
              if (x == null) continue;
              const ya = series.priceToCoordinate(col.low), yb = series.priceToCoordinate(col.low + pnf.box);
              if (ya == null || yb == null) continue;
              const boxH = Math.abs(ya - yb), half = Math.max(1.5, Math.min(spacing * 0.42, boxH * 0.42));
              ctx.strokeStyle = col.x ? up : down;
              for (let k = 0; k < col.boxes; k++) {
                const y = series.priceToCoordinate(col.low + k * pnf.box);
                if (y == null) continue;
                ctx.beginPath();
                if (col.x) { ctx.moveTo(x - half, y - half); ctx.lineTo(x + half, y + half); ctx.moveTo(x + half, y - half); ctx.lineTo(x - half, y + half); }
                else ctx.ellipse(x, y, half, half, 0, 0, Math.PI * 2);
                ctx.stroke();
              }
            }
          });
        },
      };
    }
  }
  class PnFPrimitive {
    constructor(cell) { this.cell = cell; this.views = [new PnFView(this)]; }
    attached(p) { this.request = p.requestUpdate; }
    detached() { this.request = null; }
    updateAllViews() {}
    paneViews() { return this.views; }
  }

  // ================================================================== cells
  function layoutDef(key) { return LAYOUTS.find((l) => l.key === key) || LAYOUTS[0]; }
  function buildGrid() {
    const L = layoutDef(W.layout), grid = $("pc-grid");
    grid.dataset.layout = L.key;
    grid.style.setProperty("--cols", L.cols);
    grid.style.setProperty("--rows", Math.ceil(L.n / L.cols));
    $("layout-count").textContent = L.n;
    while (W.cfgs.length < L.n) W.cfgs.push(defaultCfg(W.cfgs.length));
    W.cfgs = W.cfgs.map(cleanCfg);
    while (W.cells.length > L.n) destroyCell(W.cells.pop());
    for (let i = W.cells.length; i < L.n; i++) W.cells.push(createCell(i));
    if (W.active >= L.n) W.active = 0;
    if (W.maxCell !== null && W.maxCell >= L.n) setMax(null);
    markActive();
    renderLayouts();
    liveRefresh();
    save();
  }
  function createCell(i) {
    const el = document.createElement("div");
    el.className = "pc-cell";
    el.dataset.i = i;
    el.setAttribute("role", "group");
    el.setAttribute("aria-label", `Chart ${i + 1}`);
    el.innerHTML = `
      <div class="pc-cell-head">
        <button type="button" class="pc-sym" data-act="sym" title="Change coin or pair"><img alt="" width="18" height="18"><b></b>${ICON.caret}</button>
        <button type="button" class="pc-chip" data-act="iv" title="Interval" aria-haspopup="dialog"></button>
        <button type="button" class="pc-chip" data-act="type" title="Chart type" aria-haspopup="dialog"></button>
        <button type="button" class="pc-chip pc-chip-spread" data-act="spread" title="Spread chart: one market against another" aria-pressed="false">A÷B</button>
        <span class="pc-q"><b class="tnum"></b><small class="tnum"></small></span>
        <span class="pc-cell-r">
          <button type="button" class="pc-ico-btn" data-act="export" title="Download this chart (CSV)" aria-label="Download this chart as CSV">${ICON.dl}</button>
          <button type="button" class="pc-ico-btn" data-act="max" title="Make this chart big" aria-label="Make this chart big">${ICON.max}</button>
        </span>
      </div>
      <div class="pc-cell-body">
        <div class="pc-chart"></div>
        <div class="pc-legend" aria-hidden="true"></div>
        <div class="pc-msg">Loading…</div>
        <button type="button" class="pc-latest" hidden title="Back to the latest bars" aria-label="Back to the latest bars">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
        </button>
      </div>`;
    $("pc-grid").appendChild(el);
    const cell = { i, el, chart: null, main: null, vol: null, kind: null, A: [], B: [], tb: null, feed: null, real: null, view: [],
                   items: [], seq: 0, more: true, loadingOlder: false, nextStart: null, lastSync: 0, syncing: false, err: null,
                   paused: false, price: null, chg: null, pnf: null, prim: null, timeBased: true, renderTimer: null,
                   lastRender: 0, retry: null, ignoreRangeUntil: 0, hover: null };
    el.addEventListener("pointerdown", () => setActive(cell.i), true);
    el.querySelector(".pc-cell-head").addEventListener("click", (e) => onHead(cell, e));
    el.querySelector(".pc-latest").addEventListener("click", () => cell.chart && cell.chart.timeScale().scrollToRealTime());
    makeChart(cell);
    paintHead(cell);
    load(cell);
    return cell;
  }
  function destroyCell(cell) {
    cell.seq++;
    Trades.unsub(cell);
    clearTimeout(cell.retry);
    clearTimeout(cell.renderTimer);
    if (cell.chart) { try { cell.chart.remove(); } catch (e) { /* already gone */ } }
    cell.el.remove();
  }
  function makeChart(cell) {
    if (!LW) { msg(cell, "The chart library could not load. Check your connection and reload the page."); return; }
    const box = cell.el.querySelector(".pc-chart");
    const t = theme();
    t.localization = { timeFormatter: (x) => crossTime(cell, x), priceFormatter: (p) => fmtPrice(cell, p) };
    t.timeScale.tickMarkFormatter = (x, type) => tickMark(cell, x, type);
    cell.chart = LW.createChart(box, Object.assign({ autoSize: true }, t));
    cell.chart.subscribeCrosshairMove((p) => onCrosshair(cell, p));
    cell.chart.timeScale().subscribeVisibleLogicalRangeChange((r) => onRange(cell, r));
    cell.chart.subscribeClick((p) => onChartClick(cell, p));
  }
  function msg(cell, text) {
    const m = cell.el.querySelector(".pc-msg");
    m.textContent = text;
    m.hidden = false;
  }
  function hideMsg(cell) { cell.el.querySelector(".pc-msg").hidden = true; }
  function setActive(i) {
    if (W.active === i) return;
    W.active = i;
    markActive();
    save();
  }
  function markActive() {
    W.cells.forEach((c) => c.el.classList.toggle("is-active", c.i === W.active && W.cells.length > 1));
    const c = W.cells[W.active];
    $("export-active-label").textContent = c ? `Active chart (${symLabel(cfgOf(c))}, ${ivOf(c).short}, ${TYPE_NAME[cfgOf(c).type]})` : "Active chart";
  }
  function paintHead(cell) {
    const c = cfgOf(cell), iv = ivOf(cell), el = cell.el;
    const sym = el.querySelector(".pc-sym");
    sym.querySelector("b").textContent = symLabel(c);
    F.setIcon(sym.querySelector("img"), c.sym);
    el.querySelector('[data-act="iv"]').textContent = iv.short;
    el.querySelector('[data-act="iv"]').title = `Interval: ${iv.long}`;
    el.querySelector('[data-act="type"]').textContent = TYPE_SHORT[c.type] || TYPE_NAME[c.type];
    el.querySelector('[data-act="type"]').title = `Chart type: ${TYPE_NAME[c.type]}`;
    el.querySelector('[data-act="spread"]').setAttribute("aria-pressed", c.symB ? "true" : "false");
    paintQuote(cell);
  }
  function paintQuote(cell) {
    const q = cell.el.querySelector(".pc-q"), c = cfgOf(cell);
    const last = cell.view.length ? cell.view[cell.view.length - 1].close : null;
    const price = c.symB ? last : (cell.price != null ? cell.price : last);
    q.querySelector("b").textContent = price == null ? "" : fmtPrice(cell, price);
    const ch = c.symB ? null : cell.chg;
    const s = q.querySelector("small");
    s.textContent = ch == null ? "" : F.pct(ch);
    s.className = "tnum " + (ch == null ? "" : ch >= 0 ? "c-long" : "c-short");
    cell.el.classList.toggle("is-paused", !!cell.paused);
  }

  // ================================================================== data
  function getBars(sym, iv, limit, before) {
    return F.getJSON(`/api/premium/bars?coin=${enc(sym)}&interval=${enc(iv)}&limit=${limit}${before ? `&before=${before}` : ""}`);
  }
  async function load(cell) {
    const c = cfgOf(cell), iv = ivOf(cell), seq = ++cell.seq;
    clearTimeout(cell.retry);
    Trades.unsub(cell);
    Object.assign(cell, { A: [], B: [], tb: null, more: true, err: null, nextStart: null, view: [], items: [], real: null, price: null, chg: null });
    paintHead(cell);
    msg(cell, "Loading…");
    if (cell.main) cell.main.setData([]);
    if (cell.vol) cell.vol.setData([]);
    if (fromTrades(iv)) {
      if (isForex(c.sym) || (c.symB && isForex(c.symB))) { msg(cell, "Second and tick charts are built from the exchange's trades, so they work for crypto coins only. Forex pairs have no public trades."); return; }
      if (c.symB) { msg(cell, "Spread charts need a time interval of 1 minute or more. Change the interval or remove the spread."); return; }
      Trades.sub(cell);
      liveRefresh();
      return;
    }
    const limit = PRICE_TYPES.has(c.type) ? 1000 : 500;
    try {
      const res = await Promise.all([getBars(c.sym, iv.name, limit)].concat(c.symB ? [getBars(c.symB, iv.name, limit)] : []));
      if (seq !== cell.seq) return;
      const bad = res.find((r) => !r || !r.ok);
      if (bad) throw new Error(bad.error || "Data is not available right now.");
      cell.A = res[0].bars || [];
      cell.B = res[1] ? res[1].bars || [] : [];
      cell.more = !!res[0].more;
      cell.nextStart = res[0].next_start;
      cell.lastSync = Date.now();
      noteServer(res[0].server_time);
      if (!cell.A.length || (c.symB && !cell.B.length)) { msg(cell, "No bars for this market and interval yet."); return; }
      hideMsg(cell);
      render(cell, { fit: true });
    } catch (e) {
      if (seq !== cell.seq || e.message === "login") return;
      msg(cell, `${e.message && e.message.length < 160 ? e.message : "Data is not available right now."} Trying again…`);
      cell.retry = setTimeout(() => { if (seq === cell.seq) load(cell); }, 8000);
    }
    liveRefresh();
  }
  function merge(bars, fresh) {
    if (!fresh || !fresh.length) return bars;
    const first = fresh[0].time;
    let k = bars.length;
    while (k > 0 && bars[k - 1].time >= first) k--;
    return bars.slice(0, k).concat(fresh);
  }
  async function resync(cell) {
    const c = cfgOf(cell), iv = ivOf(cell), seq = cell.seq;
    if (cell.syncing || fromTrades(iv) || !cell.A.length) return;
    cell.syncing = true;
    cell.lastSync = Date.now();
    try {
      const res = await Promise.all([getBars(c.sym, iv.name, 3)].concat(c.symB ? [getBars(c.symB, iv.name, 3)] : []));
      if (seq !== cell.seq || !res[0] || !res[0].ok) return;
      cell.A = merge(cell.A, res[0].bars);
      if (res[1] && res[1].ok) cell.B = merge(cell.B, res[1].bars);
      cell.nextStart = res[0].next_start;
      noteServer(res[0].server_time);
      render(cell);
    } catch (e) { /* the next tick tries again */ } finally { cell.syncing = false; }
  }
  async function loadOlder(cell) {
    const c = cfgOf(cell), iv = ivOf(cell), seq = cell.seq;
    if (cell.loadingOlder || !cell.more || fromTrades(iv) || !cell.A.length || cell.A.length > 20000) return;
    cell.loadingOlder = true;
    try {
      const before = cell.A[0].time;
      const res = await Promise.all([getBars(c.sym, iv.name, 500, before)].concat(c.symB ? [getBars(c.symB, iv.name, 500, cell.B.length ? cell.B[0].time : before)] : []));
      if (seq !== cell.seq || !res[0] || !res[0].ok) return;
      const older = (res[0].bars || []).filter((b) => b.time < before);
      if (!older.length) { cell.more = false; return; }
      const n0 = cell.view.length;
      cell.A = older.concat(cell.A);
      if (res[1] && res[1].ok && cell.B.length) cell.B = (res[1].bars || []).filter((b) => b.time < cell.B[0].time).concat(cell.B);
      cell.more = !!res[0].more;
      const ts = cell.chart.timeScale(), lr = ts.getVisibleLogicalRange();
      render(cell);
      const added = cell.view.length - n0;
      if (lr && added > 0) ts.setVisibleLogicalRange({ from: lr.from + added, to: lr.to + added });
    } catch (e) { /* scrolling again retries */ } finally { cell.loadingOlder = false; }
  }

  // ================================================================== drawing a cell
  function replayOn(cell) { return W.replay && W.replay.time != null && (W.replay.all || W.replay.cell === cell); }
  function baseBars(cell, full) {
    const c = cfgOf(cell), iv = ivOf(cell);
    let base;
    if (fromTrades(iv)) base = cell.tb ? cell.tb.bars.map((b) => ({ time: b.t, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume, count: b.count })) : [];
    else base = c.symB ? P.spreadBars(cell.A, cell.B, c.op) : cell.A;
    if (!full && replayOn(cell)) { const t = W.replay.time; base = base.filter((b) => b.time <= t); }
    return base;
  }
  function autoBox(base) {
    const tail = base.slice(-300);
    return P.niceStep(P.atr(tail, 14)) || P.niceStep(Math.abs(tail.length ? tail[tail.length - 1].close : 1) * 0.002) || 1;
  }
  function seriesKey(type) { return { bars: "bar", line: "line", area: "area", kagi: "kagi", pnf: "pnf", hollow: "hollow" }[type] || "candle"; }
  function ensureSeries(cell) {
    const c = cfgOf(cell), key = seriesKey(c.type) + (c.volume && hasVolume(cell) ? "+v" : "");
    if (cell.kind === key || !cell.chart) return;
    if (cell.main) { cell.chart.removeSeries(cell.main); cell.main = null; cell.prim = null; }
    if (cell.vol) { cell.chart.removeSeries(cell.vol); cell.vol = null; }
    const up = css("--long"), down = css("--short"), acc = css("--accent"), k = seriesKey(c.type);
    let s;
    if (k === "bar") s = cell.chart.addSeries(LW.BarSeries, { upColor: up, downColor: down, thinBars: false });
    else if (k === "line") s = cell.chart.addSeries(LW.LineSeries, { color: acc, lineWidth: 2 });
    else if (k === "area") s = cell.chart.addSeries(LW.AreaSeries, { lineColor: acc, topColor: alpha(acc, 0.28), bottomColor: alpha(acc, 0.02), lineWidth: 2 });
    else if (k === "kagi") s = cell.chart.addSeries(LW.LineSeries, { color: up, lineWidth: 2, lineType: LW.LineType ? LW.LineType.WithSteps : 1, crosshairMarkerVisible: true });
    else if (k === "pnf") {
      const clear = "rgba(0,0,0,0)";
      s = cell.chart.addSeries(LW.CandlestickSeries, { upColor: clear, downColor: clear, borderVisible: false, wickVisible: false, lastValueVisible: false, priceLineVisible: false });
      cell.prim = new PnFPrimitive(cell);
      if (s.attachPrimitive) s.attachPrimitive(cell.prim);
    } else if (k === "hollow") s = cell.chart.addSeries(LW.CandlestickSeries, { upColor: "rgba(0,0,0,0)", downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
    else s = cell.chart.addSeries(LW.CandlestickSeries, { upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down });
    cell.main = s;
    const withVol = c.volume && hasVolume(cell);
    if (withVol) {
      cell.vol = cell.chart.addSeries(LW.HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
      cell.chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
    }
    // the price keeps clear of the legend at the top and of the volume bars at the bottom
    cell.chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.14, bottom: withVol ? 0.2 : 0.06 } });
    cell.kind = key;
  }
  function hasVolume(cell) {
    const c = cfgOf(cell);
    return !c.symB && !["kagi", "pnf", "linebreak", "line", "area"].includes(c.type);
  }
  function build(cell, base) {
    const c = cfgOf(cell), type = c.type;
    let items = base, extra = "";
    cell.pnf = null;
    cell.capped = false;
    if (type === "heikin") items = P.heikinAshi(base);
    else if (PRICE_TYPES.has(type)) {
      const auto = autoBox(base);
      if (type === "range") { const size = c.range || auto; items = P.rangeBars(base, size); extra = `range ${fmtNum(size)}${c.range ? "" : " (auto)"}`; }
      else if (type === "renko") { const box = c.box || auto; items = P.renko(base, box); extra = `box ${fmtNum(box)}${c.box ? "" : " (auto)"}`; }
      else if (type === "kagi") {
        const rev = c.box || auto, cols = P.kagi(base, rev);
        items = cols.length ? [{ t: cols[0].t, close: cols[0].open, open: cols[0].open, high: cols[0].open, low: cols[0].open, yang: cols[0].yang }].concat(cols) : [];
        extra = `reversal ${fmtNum(rev)}${c.box ? "" : " (auto)"}`;
      } else if (type === "linebreak") { items = P.lineBreak(base, c.lines); extra = `${c.lines}-line break`; }
      else if (type === "pnf") { const box = c.box || auto; items = P.pointFigure(base, box, c.rev); cell.pnf = { cols: items, box }; extra = `box ${fmtNum(box)}, ${c.rev}-box reversal${c.box ? "" : " (auto)"}`; }
      cell.capped = !!items.capped;
    }
    return { items, extra };
  }
  function render(cell, opts) {
    opts = opts || {};
    if (!cell.chart) return;
    const c = cfgOf(cell), iv = ivOf(cell), type = c.type;
    const base = baseBars(cell);
    ensureSeries(cell);
    const { items, extra } = build(cell, base);
    const timeBased = iv.kind !== "tick" && !PRICE_TYPES.has(type);
    let data;
    if (!timeBased) {
      const ix = P.indexed(items.map((x) => (x.t === undefined ? Object.assign({}, x, { t: x.time }) : x)));
      data = ix.data;
      cell.real = ix.real;
    } else { data = items; cell.real = null; }
    cell.timeBased = timeBased;
    cell.items = items;
    cell.view = data;
    cell.extra = extra;
    const k = seriesKey(type), up = css("--long"), down = css("--short");
    let sd;
    if (k === "line" || k === "area") sd = data.map((b) => ({ time: b.time, value: b.close }));
    else if (k === "kagi") sd = data.map((b) => ({ time: b.time, value: b.close, color: b.yang ? up : down }));
    else if (k === "pnf") sd = data.map((b) => ({ time: b.time, open: b.open, close: b.close, high: b.high + cell.pnf.box / 2, low: b.low - cell.pnf.box / 2 }));
    else sd = data.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close }));
    cell.chart.applyOptions({ timeScale: { secondsVisible: iv.kind === "second" } });
    cell.main.applyOptions({ priceFormat: priceFormat(cell) });
    cell.main.setData(sd);
    if (cell.vol) {
      const upA = alpha(up, 0.35), downA = alpha(down, 0.35);
      cell.vol.setData(data.map((b) => ({ time: b.time, value: b.volume || 0, color: b.close >= b.open ? upA : downA })));
    }
    if (cell.prim && cell.prim.request) cell.prim.request();
    if (opts.fit) {
      const n = data.length, show = Math.min(n, cell.el.clientWidth > 700 ? 160 : 90);
      if (n) cell.chart.timeScale().setVisibleLogicalRange({ from: n - show - 0.5, to: n + 3 });
    }
    if (replayOn(cell)) cell.chart.timeScale().scrollToRealTime();
    cell.lastRender = Date.now();
    if (!data.length) msg(cell, PRICE_TYPES.has(type) ? "Not enough price movement for this box size yet. Try a smaller box or a longer history." : "No bars yet.");
    else if (cell.capped) msg(cell, "This box size is too small for the history: only the first 20,000 items are shown. Choose a bigger size.");
    else hideMsg(cell);
    legend(cell, null);
    paintQuote(cell);
  }
  // live updates: the last bar of a plain time chart is updated in place; everything else is rebuilt (at most twice a second)
  function liveRender(cell) {
    if (replayOn(cell) || !cell.main) return;
    const c = cfgOf(cell), iv = ivOf(cell), k = seriesKey(c.type);
    const plain = iv.kind !== "tick" && !c.symB && ["candle", "hollow", "bar", "line", "area"].includes(k) && c.type !== "heikin";
    const bars = fromTrades(iv) ? null : cell.A;
    if (plain && bars && bars.length && cell.view.length && bars[bars.length - 1].time >= cell.view[cell.view.length - 1].time) {
      const b = bars[bars.length - 1];
      const point = k === "line" || k === "area" ? { time: b.time, value: b.close } : { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close };
      try {
        cell.main.update(point);
        if (cell.vol) cell.vol.update({ time: b.time, value: b.volume || 0, color: alpha(b.close >= b.open ? css("--long") : css("--short"), 0.35) });
        if (b.time > cell.view[cell.view.length - 1].time) cell.view.push(b); else cell.view[cell.view.length - 1] = b;
        cell.items = cell.view;
        legend(cell, cell.hover);
        paintQuote(cell);
        return;
      } catch (e) { /* fall back to a full redraw */ }
    }
    const wait = Math.max(0, 500 - (Date.now() - cell.lastRender));
    if (cell.renderTimer) return;
    cell.renderTimer = setTimeout(() => { cell.renderTimer = null; render(cell); }, wait);
  }
  function legend(cell, p) {
    const el = cell.el.querySelector(".pc-legend"), c = cfgOf(cell), iv = ivOf(cell);
    cell.hover = p;
    let idx = -1;
    if (p && p.time != null && cell.view.length) idx = cell.real ? Math.round(p.time) - 1 : findIndex(cell.view, p.time);
    if (idx < 0 || idx >= cell.view.length) idx = cell.view.length - 1;
    const item = idx >= 0 ? cell.view[idx] : null;
    const head = `<b>${esc(symLabel(c))}</b> · ${esc(iv.short)} · ${esc(TYPE_NAME[c.type])}${cell.extra ? ` · ${esc(cell.extra)}` : ""}${cell.paused ? ' · <span class="c-short">paused</span>' : ""}`;
    if (!item) { el.innerHTML = head; return; }
    const f = (v) => fmtPrice(cell, v), cls = item.close >= item.open ? "c-long" : "c-short";
    let body;
    if (c.type === "kagi") body = `<span class="${item.yang ? "c-long" : "c-short"}">${item.yang ? "Yang" : "Yin"}</span> ${f(item.close)}`;
    else if (c.type === "pnf") body = `<span class="${item.x ? "c-long" : "c-short"}">${item.x ? "X" : "O"} column</span> ${f(item.low)} – ${f(item.high)} (${item.boxes} boxes)`;
    else if (c.type === "line" || c.type === "area") body = `<span class="${cls}">${f(item.close)}</span>`;
    else if (cell.el.clientWidth < 430) body = `C <span class="${cls}">${f(item.close)}</span>`;     // small charts: the close only
    else body = `O <span class="${cls}">${f(item.open)}</span> H <span class="${cls}">${f(item.high)}</span> L <span class="${cls}">${f(item.low)}</span> C <span class="${cls}">${f(item.close)}</span>`;
    const when = cell.real ? cell.real[idx] : item.time;
    el.innerHTML = `${head}<br>${body}${item.count ? ` · ${item.count} trades` : ""} <span class="pc-legend-t">${esc(fmtStamp(when, iv))} UTC</span>`;
  }
  function findIndex(arr, time) {
    let lo = 0, hi = arr.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1, t = arr[mid].time;
      if (t === time) return mid;
      if (t < time) lo = mid + 1; else hi = mid - 1;
    }
    return -1;
  }

  // ================================================================== chart events: crosshair / scroll sync, older history, replay start
  // the crosshair follows the same moment on every chart: the bar (or brick, column ...) that was forming then
  function onCrosshair(cell, p) {
    legend(cell, p && p.time != null ? p : null);
    if (!p || !p.sourceEvent || !W.sync.crosshair) return;          // only real mouse moves are passed on
    const at = p.time == null ? null : realTimeAt(cell, p.time);
    for (const o of W.cells) {
      if (o === cell || !o.chart || !o.main || !o.view.length) continue;
      const k = at == null ? -1 : indexAt(o, at);
      if (k < 0) { o.chart.clearCrosshairPosition(); legend(o, null); continue; }
      const b = o.view[k];
      o.chart.setCrosshairPosition(b.close, b.time, o.main);
      legend(o, { time: b.time });                                    // the library does not report programmatic moves
    }
  }
  function indexAt(o, sec) {                                          // last item that started at or before `sec`
    const t = (i) => (o.real ? o.real[i] : o.view[i].time);
    let lo = 0, hi = o.view.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (t(mid) <= sec) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }
  function onRange(cell, r) {
    if (!r) return;
    const btn = cell.el.querySelector(".pc-latest");
    btn.hidden = !(r.to < cell.view.length - 2);
    if (r.from < 12 && !replayOn(cell)) loadOlder(cell);
    if (!W.sync.time || !cell.timeBased || Date.now() < cell.ignoreRangeUntil) return;
    const vr = cell.chart.timeScale().getVisibleRange();
    if (!vr) return;
    for (const o of W.cells) {
      if (o === cell || !o.chart || !o.timeBased || !o.view.length) continue;
      if (o.view[0].time > vr.to || o.view[o.view.length - 1].time < vr.from) continue;
      o.ignoreRangeUntil = Date.now() + 250;
      try { o.chart.timeScale().setVisibleRange(vr); } catch (e) { /* range outside this chart's data */ }
    }
  }
  function realTimeAt(cell, t) { return cell.real ? cell.real[Math.round(t) - 1] : t; }
  function onChartClick(cell, p) {
    if (!W.replay || W.replay.state !== "pick" || !p || p.time == null) return;
    setActive(cell.i);
    const t = realTimeAt(cell, p.time);
    if (t != null) replayAt(cell, t);
  }

  // ================================================================== the cell header: coin, interval, type, spread, export, maximize
  function onHead(cell, e) {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    setActive(cell.i);
    const act = b.dataset.act;
    if (act === "sym") { W.pickFor = { cell, field: "sym" }; W.picker.open(b); }
    else if (act === "iv") openIv(cell, b);
    else if (act === "type") openType(cell, b);
    else if (act === "spread") openSpread(cell);
    else if (act === "export") exportCells([cell], false);
    else if (act === "max") setMax(W.maxCell === cell.i ? null : cell.i);
  }
  function setMax(i) {
    W.maxCell = i;
    $("pc-grid").classList.toggle("is-max", i !== null);
    W.cells.forEach((c) => {
      const on = c.i === i;
      c.el.classList.toggle("is-max", on);
      const btn = c.el.querySelector('[data-act="max"]');
      btn.innerHTML = on ? ICON.min : ICON.max;
      btn.title = on ? "Back to all charts" : "Make this chart big";
      btn.setAttribute("aria-label", btn.title);
    });
  }
  function setCfg(cell, patch, opts) {
    opts = opts || {};
    const targets = opts.all ? W.cells : [cell];
    for (const t of targets) {
      const c = cfgOf(t);
      if (opts.all && t !== cell && c.symB && ("sym" in patch)) continue;            // spread charts keep their pair
      Object.assign(c, patch);
      W.cfgs[t.i] = cleanCfg(c, t.i);
      paintHead(t);
      if (opts.reload === false) render(t); else load(t);
    }
    markActive();
    save();
    liveRefresh();
  }
  function onPick(sym) {
    const pf = W.pickFor;
    W.pickFor = null;
    if (!pf) return;
    if (pf.field === "spreadA" || pf.field === "spreadB") { spreadPick(pf.field, sym); return; }
    const cell = pf.cell;
    setCfg(cell, { sym }, { all: W.sync.symbol });
  }
  function pickCurrent() {
    const pf = W.pickFor;
    if (!pf) return null;
    if (pf.field === "spreadA") return SP.a;
    if (pf.field === "spreadB") return SP.b;
    return cfgOf(pf.cell).sym;
  }

  // ---------------------------------------------------------------- floating popovers (interval, chart type)
  let floatOpen = null;
  function openFloat(id, anchor) {
    closeFloat();
    const pop = $(id);
    pop.hidden = false;
    floatOpen = { pop, anchor };
    placeFloat();
    anchor.setAttribute("aria-expanded", "true");
  }
  function placeFloat() {
    if (!floatOpen) return;
    const { pop, anchor } = floatOpen;
    if (window.matchMedia("(max-width: 760px)").matches) { pop.style.left = ""; pop.style.top = ""; return; }
    const r = anchor.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
    let top = r.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  }
  function closeFloat() {
    if (!floatOpen) return;
    floatOpen.pop.hidden = true;
    floatOpen.anchor.setAttribute("aria-expanded", "false");
    floatOpen = null;
  }
  let ivCell = null;
  function openIv(cell, anchor, focusCustom, unit) {
    ivCell = cell;
    const cur = ivOf(cell).name, forex = isForex(cfgOf(cell).sym) || !!cfgOf(cell).symB;
    $("iv-groups").innerHTML = IV_GROUPS.map(([name, list]) => {
      const off = forex && (name === "Ticks" || name === "Seconds");
      return `<div class="pc-iv-group"><span>${esc(name)}${off ? " · crypto only" : ""}</span><div>${list.map((n) => {
        const iv = P.parseInterval(n);
        return `<button type="button" data-iv="${esc(iv.name)}" aria-pressed="${iv.name === cur}" ${off ? "disabled" : ""} title="${esc(iv.long)}">${esc(iv.short)}</button>`;
      }).join("")}</div></div>`;
    }).join("");
    const iv = P.parseInterval(cur);
    $("iv-n").value = iv.n;
    $("iv-unit").value = unit || iv.unit;
    openFloat("iv-pop", anchor);
    if (focusCustom) setTimeout(() => { $("iv-n").focus(); $("iv-n").select(); }, 30);
  }
  function chooseIv(name) {
    closeFloat();
    const cell = ivCell;
    if (!cell) return;
    const iv = P.parseInterval(name);
    if (!iv) { F.toast("That interval is not supported."); return; }
    const c = cfgOf(cell);
    if (fromTrades(iv) && (isForex(c.sym) || c.symB)) { F.toast("Second and tick charts are for single crypto coins."); return; }
    setCfg(cell, { iv: iv.name }, { all: W.sync.interval });
  }
  let typeCell = null;
  function openType(cell, anchor) {
    typeCell = cell;
    const c = cfgOf(cell);
    $("type-list").innerHTML = `<div class="pc-type-group"><span>Time-based</span>${TYPES.filter((t) => t[2] === "time").map(typeBtn).join("")}</div>
      <div class="pc-type-group"><span>Price-based (no time axis)</span>${TYPES.filter((t) => t[2] === "price").map(typeBtn).join("")}</div>`;
    function typeBtn(t) { return `<button type="button" data-type="${t[0]}" aria-checked="${c.type === t[0]}" role="menuitemradio">${esc(t[1])}</button>`; }
    paintTypeSet(cell);
    openFloat("type-pop", anchor);
  }
  function paintTypeSet(cell) {
    const c = cfgOf(cell), set = $("type-set");
    const auto = cell.items && cell.items.length ? autoBox(baseBars(cell, true)) : null;
    const field = (label, key, hint) => `<label class="pc-field"><span>${label}</span>
      <input class="mk-input tnum" type="number" min="0" step="any" data-set="${key}" value="${c[key] || ""}" placeholder="auto${auto ? ` (${fmtNum(auto)})` : ""}">
      ${hint ? `<small>${hint}</small>` : ""}</label>`;
    let html = "";
    if (c.type === "range") html = field("Range size (price)", "range", "Each bar covers this much price. Empty = based on the average candle (ATR 14).");
    else if (c.type === "renko") html = field("Box size (price)", "box", "Empty = based on the average candle (ATR 14).");
    else if (c.type === "kagi") html = field("Reversal amount (price)", "box", "The line turns after a move back of this size.");
    else if (c.type === "pnf") html = field("Box size (price)", "box", "") + `<label class="pc-field"><span>Reversal (boxes)</span><input class="mk-input tnum" type="number" min="1" max="10" step="1" data-set="rev" value="${c.rev}"></label>`;
    else if (c.type === "linebreak") html = `<label class="pc-field"><span>Lines to break</span><input class="mk-input tnum" type="number" min="1" max="10" step="1" data-set="lines" value="${c.lines}"></label>`;
    if (PRICE_TYPES.has(c.type)) html += `<p class="mk-note">Built from the ${esc(ivOf(cell).long)} bars of this chart. Pick 1m or 5m for intraday detail.</p>`;
    if (hasVolume(cell)) html += `<label class="pc-check"><input type="checkbox" data-set="volume" ${c.volume ? "checked" : ""}> <span>Show volume</span></label>`;
    set.innerHTML = html;
    set.hidden = !html;
  }
  function chooseType(type) {
    const cell = typeCell;
    if (!cell) return;
    const c = cfgOf(cell), wasPrice = PRICE_TYPES.has(c.type), nowPrice = PRICE_TYPES.has(type);
    c.type = type;
    W.cfgs[cell.i] = cleanCfg(c, cell.i);
    paintHead(cell);
    save();
    if (wasPrice !== nowPrice && !fromTrades(ivOf(cell))) load(cell); else render(cell, { fit: true });
    openType(cell, floatOpen ? floatOpen.anchor : cell.el.querySelector('[data-act="type"]'));
  }

  // ---------------------------------------------------------------- spread dialog
  const SP = { cell: null, a: null, b: null };
  function openSpread(cell) {
    const c = cfgOf(cell);
    SP.cell = cell;
    SP.a = c.sym;
    SP.b = c.symB || (c.sym === "ETH/USDT" ? "BTC/USDT" : "ETH/USDT");
    $("spread-a").textContent = SP.a;
    $("spread-b").textContent = SP.b;
    $("spread-op").value = c.op || "/";
    $("spread-off").hidden = !c.symB;
    $("spread-dlg").hidden = false;
    setTimeout(() => $("spread-op").focus(), 30);
  }
  function spreadPick(field, sym) {
    if (field === "spreadA") { SP.a = sym; $("spread-a").textContent = sym; } else { SP.b = sym; $("spread-b").textContent = sym; }
    $("spread-dlg").hidden = false;
  }
  function closeSpread() { $("spread-dlg").hidden = true; }

  // ================================================================== live: one price feed shared by every tab
  const Hub = (() => {
    const chan = "BroadcastChannel" in window ? new BroadcastChannel("sfm-premium-feed") : null;
    const tab = Math.random().toString(36).slice(2), born = Date.now();
    const peers = new Map(), listeners = [];
    let leader = false, asking = false, timer = null, syms = [], charts = 0, lastMsg = 0, release = null;
    function alive(p) { return Date.now() - p.ts < 9000; }
    function announce() { if (chan) chan.postMessage({ type: "want", tab, born, syms, charts, ts: Date.now() }); }
    function union() {
      const set = new Set(syms);
      for (const p of peers.values()) if (alive(p)) p.syms.forEach((s) => set.add(s));
      return [...set].slice(0, 200);
    }
    function budget() {
      const list = [{ tab, born, charts }].concat([...peers.values()].filter(alive)).sort((a, b) => a.born - b.born);
      let used = 0;
      const allow = {};
      for (const p of list) { allow[p.tab] = Math.max(0, Math.min(p.charts, MAX_LIVE - used)); used += p.charts; }
      return { allow, charts: used, tabs: list.length };
    }
    function deliver(m) {
      lastMsg = Date.now();
      listeners.forEach((f) => f(m));
    }
    async function poll() {
      const list = union();
      if (!list.length || document.hidden && !peers.size) return;
      try {
        const d = await F.getJSON(`/api/premium/prices?coins=${enc(list.join(","))}`);
        const m = Object.assign({ type: "prices", ok: !!(d && d.ok), prices: (d && d.prices) || {}, server_time: d && d.server_time }, budget());
        deliver(m);
        if (chan) chan.postMessage(m);
      } catch (e) {
        if (e.message !== "login") deliver(Object.assign({ type: "prices", ok: false, prices: {} }, budget()));
      }
    }
    function becomeLeader() {
      leader = true;
      poll();
      timer = setInterval(poll, 2000);
    }
    function stopLeading() {
      leader = false;
      clearInterval(timer);
      if (release) { const r = release; release = null; r(); }
    }
    function ask() {
      if (asking || leader || document.hidden) return;
      if (navigator.locks && chan) {
        asking = true;
        navigator.locks.request("sfm-premium-feed", () => new Promise((resolve) => { asking = false; release = resolve; becomeLeader(); }))
          .catch(() => { asking = false; });
      } else becomeLeader();
    }
    if (chan) {
      chan.onmessage = (e) => {
        const m = e.data || {};
        if (m.type === "want") peers.set(m.tab, m);
        else if (m.type === "bye") peers.delete(m.tab);
        else if (m.type === "prices" && !leader) deliver(m);
      };
    }
    document.addEventListener("visibilitychange", () => {
      if (document.hidden && leader && navigator.locks && chan) stopLeading();          // a hidden tab hands the feed over
      else if (!document.hidden) ask();
    });
    window.addEventListener("pagehide", () => { if (chan) chan.postMessage({ type: "bye", tab }); stopLeading(); });
    setInterval(() => {
      announce();
      // nobody has sent prices for 10 seconds (no lock support, or the lock is stuck): this tab polls itself
      if (!leader && !document.hidden && Date.now() - lastMsg > 10000) becomeLeader();
    }, 3000);
    return {
      tab,
      want(list, n) { syms = list; charts = n; announce(); },
      on(f) { listeners.push(f); },
      start() { announce(); ask(); },
      isLeader: () => leader,
    };
  })();

  function liveRefresh() {
    const syms = new Set();
    for (const cell of W.cells) {
      const c = cfgOf(cell);
      if (fromTrades(ivOf(cell))) continue;
      syms.add(c.sym);
      if (c.symB) syms.add(c.symB);
    }
    Hub.want([...syms], W.cells.length);
  }
  function applyPrice(bars, price, now, iv) {
    const last = bars[bars.length - 1];
    if (!last || !(price > 0)) return false;
    if (now >= P.bucketEnd(last.time, iv)) {
      const t = P.bucketStart(now, iv);
      if (t <= last.time) return false;
      bars.push({ time: t, open: price, high: price, low: price, close: price, volume: 0 });
      return true;
    }
    if (last.close === price) return false;
    last.close = price;
    if (price > last.high) last.high = price;
    if (price < last.low) last.low = price;
    return true;
  }
  function onPrices(m) {
    noteServer(m.server_time);
    W.totals = { charts: m.charts || W.cells.length, tabs: m.tabs || 1 };
    W.allow = m.allow ? m.allow[Hub.tab] : undefined;
    W.liveOk = !!m.ok;
    const now = serverNow();
    W.cells.forEach((cell, k) => {
      const was = cell.paused;
      cell.paused = W.allow !== undefined && k >= W.allow;
      if (was !== cell.paused) { paintQuote(cell); legend(cell, cell.hover); }
      const c = cfgOf(cell), iv = ivOf(cell);
      if (cell.paused || fromTrades(iv) || !cell.A.length || !m.ok) return;
      const pa = m.prices[c.sym], pb = c.symB ? m.prices[c.symB] : null;
      let changed = false;
      if (pa && pa.last != null) changed = applyPrice(cell.A, pa.last, now, iv) || changed;
      if (pb && pb.last != null && cell.B.length) changed = applyPrice(cell.B, pb.last, now, iv) || changed;
      if (pa && !c.symB) { cell.price = pa.last; if (pa.change_pct != null) cell.chg = pa.change_pct; }
      if (changed) liveRender(cell); else paintQuote(cell);
      if ((cell.nextStart && now >= cell.nextStart + 4) || Date.now() - cell.lastSync > 60000) resync(cell);
    });
    paintLive();
  }
  function paintLive() {
    const paused = W.cells.filter((c) => c.paused).length, n = W.cells.length;
    const ok = W.liveOk !== false;
    $("pc-live-dot").className = "mk-live " + (W.liveOk === null ? "" : ok ? "on" : "err");
    let text = W.liveOk === null ? "Connecting…" : ok ? `Live · ${n} chart${n === 1 ? "" : "s"}` : "Live prices paused, retrying…";
    if (ok && W.totals.tabs > 1) text += ` · ${W.totals.charts} in ${W.totals.tabs} tabs`;
    if (paused) text += ` · ${paused} paused`;
    $("pc-live-text").textContent = text;
    const tf = Trades.count();
    $("live-detail").textContent = `${W.totals.charts || n} live chart${(W.totals.charts || n) === 1 ? "" : "s"} in ${W.totals.tabs} tab${W.totals.tabs === 1 ? "" : "s"} share one price connection (limit ${MAX_LIVE}).`
      + (tf ? ` ${tf} coin${tf === 1 ? "" : "s"} also stream${tf === 1 ? "s" : ""} trades for second and tick charts.` : "")
      + (paused ? ` ${paused} chart${paused === 1 ? " is" : "s are"} paused because ${MAX_LIVE} live charts are already open in your other tabs.` : "")
      + (Hub.isLeader() ? " This tab runs the shared connection." : "");
  }

  // ---------------------------------------------------------------- trades for second and tick charts
  const Trades = (() => {
    const feeds = new Map();
    let timer = null;
    function sub(cell) {
      const coin = cfgOf(cell).sym;
      let f = feeds.get(coin);
      if (!f) { f = { coin, cells: new Set(), rows: [], ready: false, busy: false, err: null }; feeds.set(coin, f); boot(f); }
      f.cells.add(cell);
      cell.feed = f;
      if (f.ready) buildFrom(cell, f);
      schedule();
    }
    function unsub(cell) {
      const f = cell.feed;
      if (!f) return;
      f.cells.delete(cell);
      cell.feed = null;
      if (!f.cells.size) feeds.delete(f.coin);
    }
    async function boot(f) {
      try {
        const d = await F.getJSON(`/api/premium/trades?coin=${enc(f.coin)}&depth=10`);
        if (feeds.get(f.coin) !== f) return;
        if (!d.ok) { f.err = d.error || "Trades are not available right now."; f.cells.forEach((c) => msg(c, f.err)); return; }
        noteServer(d.server_time);
        f.rows = d.trades || [];
        f.ready = true;
        f.cells.forEach((c) => buildFrom(c, f));
      } catch (e) {
        if (e.message === "login" || feeds.get(f.coin) !== f) return;
        f.cells.forEach((c) => msg(c, "Trades are not available right now. Trying again…"));
        setTimeout(() => { if (feeds.get(f.coin) === f) boot(f); }, 6000);
      }
    }
    function buildFrom(cell, f) {
      cell.tb = new P.TradeBars(ivOf(cell));
      cell.tb.add(f.rows);
      if (!cell.tb.bars.length) { msg(cell, "Waiting for trades…"); return; }
      const first = f.rows.length ? Math.floor(f.rows[0][0] / 1000) : null;
      cell.tradesFrom = first;
      render(cell, { fit: true });
    }
    async function tick() {
      timer = null;
      const list = [...feeds.values()].filter((f) => f.ready && !f.busy);
      if (!document.hidden) {
        for (const f of list) {
          f.busy = true;
          try {
            const d = await F.getJSON(`/api/premium/trades?coin=${enc(f.coin)}`);
            if (d && d.ok && d.trades && d.trades.length) {
              noteServer(d.server_time);
              const last = f.rows[f.rows.length - 1];
              const fresh = last ? d.trades.filter((t) => P.isNewer(t, last[0], last[4])) : d.trades;
              if (fresh.length) {
                f.rows = f.rows.concat(fresh);
                if (f.rows.length > 30000) f.rows = f.rows.slice(-30000);
                f.cells.forEach((cell) => {
                  if (!cell.tb || cell.paused) return;
                  if (cell.tb.add(fresh)) liveRender(cell);
                  cell.price = fresh[fresh.length - 1][1];
                  paintQuote(cell);
                });
              }
            }
          } catch (e) { /* next round */ } finally { f.busy = false; }
        }
      }
      schedule();
    }
    function schedule() {
      if (timer || !feeds.size) return;
      timer = setTimeout(tick, Math.max(1000, 600 * feeds.size));
    }
    return { sub, unsub, count: () => feeds.size };
  })();

  // ================================================================== bar replay
  function replayToggle() { if (W.replay) replayExit(); else replayStart(); }
  function replayStart() {
    const cell = W.cells[W.active];
    if (!cell || !cell.view.length) { F.toast("Load a chart first."); return; }
    W.replay = { state: "pick", time: null, playing: false, speed: Number(($("rp-speed").querySelector('[aria-pressed="true"]') || {}).dataset?.speed || 1), all: $("rp-all").checked, cell, timer: null };
    $("pc-replay").hidden = false;
    $("replay-btn").setAttribute("aria-pressed", "true");
    document.body.classList.add("pc-picking");
    replayInfo();
  }
  function replayTimes(cell) { return baseBars(cell, true).map((b) => b.time); }
  function replayAt(cell, t) {
    const R = W.replay;
    if (!R) return;
    R.cell = cell;
    const times = replayTimes(cell);
    let k = times.findIndex((x) => x >= t);
    if (k < 0) k = times.length - 1;
    R.time = times[Math.max(0, k)];
    R.state = "on";
    document.body.classList.remove("pc-picking");
    replayRender();
  }
  function replayRender() {
    for (const cell of W.cells) if (W.replay && (W.replay.all || W.replay.cell === cell) || !W.replay) render(cell);
    replayInfo();
  }
  function replayStep(dir) {
    const R = W.replay;
    if (!R || R.state !== "on") return false;
    const times = replayTimes(R.cell);
    const k = times.indexOf(R.time);
    const next = k + dir;
    if (next < 0 || next >= times.length) { if (dir > 0) { replayPause(); F.toast("Replay reached the latest bar."); } return false; }
    R.time = times[next];
    replayRender();
    return true;
  }
  function replayPlay() {
    const R = W.replay;
    if (!R || R.state !== "on") return;
    if (R.playing) { replayPause(); return; }
    R.playing = true;
    const loop = () => {
      if (!W.replay || !W.replay.playing) return;
      if (replayStep(1)) W.replay.timer = setTimeout(loop, Math.max(60, 1000 / W.replay.speed));
    };
    W.replay.timer = setTimeout(loop, Math.max(60, 1000 / R.speed));
    replayInfo();
  }
  function replayPause() {
    const R = W.replay;
    if (!R) return;
    R.playing = false;
    clearTimeout(R.timer);
    replayInfo();
  }
  function replayExit() {
    if (!W.replay) return;
    clearTimeout(W.replay.timer);
    const cells = W.cells.filter((c) => replayOn(c));
    W.replay = null;
    $("pc-replay").hidden = true;
    $("replay-btn").setAttribute("aria-pressed", "false");
    document.body.classList.remove("pc-picking");
    cells.forEach((c) => { render(c); c.chart && c.chart.timeScale().scrollToRealTime(); });
  }
  function replayInfo() {
    const R = W.replay, info = $("replay-info"), play = $("rp-play");
    if (!R) return;
    const iv = ivOf(R.cell);
    if (R.state === "pick") info.textContent = "Click a bar on any chart where the replay should start, or press “Start point” to go back 200 bars.";
    else {
      const times = replayTimes(R.cell), k = times.indexOf(R.time);
      info.textContent = `${symLabel(cfgOf(R.cell))} · ${fmtStamp(R.time, iv)} UTC · ${Math.max(0, times.length - 1 - k)} bars to go${R.all ? " · all charts" : ""}`;
    }
    play.innerHTML = R.playing ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg>' : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
    play.setAttribute("aria-label", R.playing ? "Pause" : "Play");
    play.disabled = R.state !== "on";
    $("rp-back").disabled = $("rp-step").disabled = R.state !== "on";
  }
  function replayBack200() {
    const R = W.replay;
    if (!R) return;
    const cell = W.cells[W.active] || R.cell, times = replayTimes(cell);
    if (!times.length) return;
    replayAt(cell, times[Math.max(0, times.length - 201)]);
  }

  // ================================================================== export
  function rowsOf(cell, visibleOnly) {
    let idx = cell.view.map((_, i) => i);
    if (visibleOnly && cell.chart) {
      const lr = cell.chart.timeScale().getVisibleLogicalRange();
      if (lr) idx = idx.filter((i) => i >= Math.floor(lr.from) && i <= Math.ceil(lr.to));
    }
    const c = cfgOf(cell), when = (i) => P.isoUTC(cell.real ? cell.real[i] : cell.view[i].time);
    const num = (v) => (v == null || isNaN(v) ? "" : +Number(v).toPrecision(12));
    if (c.type === "kagi") return { header: ["time_utc", "price", "line"], rows: idx.map((i) => [when(i), num(cell.view[i].close), cell.view[i].yang ? "yang" : "yin"]) };
    if (c.type === "pnf") return { header: ["time_utc", "column", "low", "high", "boxes"], rows: idx.map((i) => { const b = cell.view[i]; return [when(i), b.x ? "X" : "O", num(b.low), num(b.high), b.boxes]; }) };
    const header = ["time_utc", "open", "high", "low", "close"].concat(c.symB ? [] : ["volume"]).concat(ivOf(cell).kind === "tick" || ivOf(cell).kind === "second" ? ["trades"] : []);
    return { header, rows: idx.map((i) => { const b = cell.view[i]; const r = [when(i), num(b.open), num(b.high), num(b.low), num(b.close)]; if (!c.symB) r.push(num(b.volume)); if (header.includes("trades")) r.push(b.count || ""); return r; }) };
  }
  function exportCells(cells, visibleOnly) {
    let n = 0;
    cells.forEach((cell, k) => {
      if (!cell.view.length) return;
      const c = cfgOf(cell), { header, rows } = rowsOf(cell, visibleOnly);
      const day = new Date().toISOString().slice(0, 10);
      const name = `${fileSym(c)}_${ivOf(cell).name}_${c.type}_${day}.csv`;
      setTimeout(() => download(name, P.toCSV(header, rows)), k * 350);
      n++;
    });
    F.toast(n ? `Downloading ${n} file${n === 1 ? "" : "s"}…` : "Nothing to export yet.");
  }

  // ================================================================== bond screener
  function showView(v) {
    W.view = v;
    $("pc-charts").hidden = v !== "charts";
    $("pc-bonds").hidden = v !== "bonds";
    for (const [id, key] of [["tab-charts", "charts"], ["tab-bonds", "bonds"]]) {
      $(id).setAttribute("aria-selected", v === key ? "true" : "false");
      $(id).setAttribute("aria-pressed", v === key ? "true" : "false");
    }
    if (v === "bonds" && !W.bonds.data && !W.bonds.loading) loadBonds();
  }
  async function loadBonds() {
    const B = W.bonds;
    B.loading = true;
    $("bond-rows").innerHTML = '<tr><td colspan="9" class="pc-empty">Loading bond yields from the official sources…</td></tr>';
    try {
      const d = await F.getJSON("/api/premium/bonds");
      if (!d || !d.bonds || !d.bonds.length) throw new Error(d && d.error ? d.error : "Bond data is not available right now.");
      B.data = d;
      paintBondFilters();
      paintBonds();
      paintCurve();
      paintSources();
      $("bond-note").textContent = d.note || "";
      selectBond(B.sel || (d.bonds.find((b) => b.id === "US10Y") || d.bonds[0]).id);
    } catch (e) {
      if (e.message === "login") return;
      $("bond-rows").innerHTML = `<tr><td colspan="9" class="pc-empty">${esc(e.message || "Bond data is not available right now.")} <button type="button" class="mk-link" id="bond-retry">Try again</button></td></tr>`;
      const r = $("bond-retry");
      if (r) r.addEventListener("click", loadBonds);
    } finally { B.loading = false; }
  }
  const CC_COLOR = { US: "#60a5fa", EU: "#facc15", CA: "#f87171", JP: "#f472b6", AU: "#34d399" };
  function paintBondFilters() {
    const B = W.bonds, list = [["", "All"]].concat(B.data.sources.filter((s) => s.ok).map((s) => [s.cc, s.country]));
    $("bond-countries").innerHTML = list.map(([cc, name]) => `<button type="button" class="mk-chip" data-cc="${cc}" aria-pressed="${B.cc === cc}">${cc ? `<i style="background:${CC_COLOR[cc] || "var(--faint)"}"></i>` : ""}${esc(name)}</button>`).join("");
    const newest = B.data.sources.map((s) => s.as_of).filter(Boolean).sort().pop();
    $("bond-asof").textContent = newest ? `End-of-day yields, latest ${newest}` : "";
  }
  function bondRows() {
    const B = W.bonds, q = B.q.trim().toLowerCase();
    let rows = B.data.bonds.filter((b) => (!B.cc || b.cc === B.cc) && (!B.mat || b.bucket === B.mat)
      && (!q || `${b.name} ${b.country} ${b.tenor} ${b.cc}`.toLowerCase().includes(q)));
    const k = B.sort, dir = B.dir;
    rows = rows.slice().sort((a, b) => {
      const x = a[k], y = b[k];
      if (k === "name") return dir * String(a.country + a.years).localeCompare(String(b.country + b.years)) || dir * (a.years - b.years);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return dir * (x - y) || a.cc.localeCompare(b.cc);
    });
    return rows;
  }
  function bp(v) {
    if (v == null) return '<td class="num tnum c-faint">--</td>';
    return `<td class="num tnum ${v > 0 ? "c-long" : v < 0 ? "c-short" : ""}">${F.signed(v, 1)}</td>`;
  }
  function spark(vals) {
    if (!vals || vals.length < 2) return "";
    const lo = Math.min(...vals), hi = Math.max(...vals), w = 96, h = 26, span = hi - lo || 1;
    const pts = vals.map((v, i) => `${((i / (vals.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - lo) / span) * (h - 4)).toFixed(1)}`).join(" ");
    const col = vals[vals.length - 1] >= vals[0] ? "var(--long)" : "var(--short)";
    return `<svg class="pc-spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
  }
  function range52(b) {
    if (b.lo_52w == null || b.hi_52w == null) return "<td></td>";
    const span = b.hi_52w - b.lo_52w || 1, at = Math.max(0, Math.min(100, ((b.yield - b.lo_52w) / span) * 100));
    return `<td><span class="pc-range" title="52-week low ${b.lo_52w}% · high ${b.hi_52w}%"><small class="tnum">${b.lo_52w.toFixed(2)}</small><span class="pc-range-bar"><i style="left:${at.toFixed(1)}%"></i></span><small class="tnum">${b.hi_52w.toFixed(2)}</small></span></td>`;
  }
  function paintBonds() {
    const B = W.bonds, rows = bondRows();
    document.querySelectorAll("#bond-table th button").forEach((b) => {
      b.parentElement.setAttribute("aria-sort", b.dataset.sort === B.sort ? (B.dir > 0 ? "ascending" : "descending") : "none");
    });
    $("bond-rows").innerHTML = rows.length ? rows.map((b) => `<tr data-id="${esc(b.id)}" tabindex="0" aria-selected="${B.sel === b.id}">
        <td><span class="pc-bond-name"><i style="background:${CC_COLOR[b.cc] || "var(--faint)"}"></i><span><b>${esc(b.name)}</b><small>${esc(b.country)} · ${esc(b.date)}</small></span></span></td>
        <td class="tnum">${esc(b.tenor)}</td>
        <td class="num tnum"><b>${b.yield.toFixed(3)}%</b></td>
        ${bp(b.chg_1d)}${bp(b.chg_1w)}${bp(b.chg_1m)}${bp(b.chg_1y)}${range52(b)}
        <td>${spark(b.spark)}</td></tr>`).join("")
      : '<tr><td colspan="9" class="pc-empty">No bond matches these filters.</td></tr>';
  }
  function paintSources() {
    const S = W.bonds.data.sources;
    $("bond-sources").innerHTML = "Sources: " + S.map((s) => `<a href="${esc(s.link)}" target="_blank" rel="noopener">${esc(s.name)}</a>${s.ok ? ` <small>(${esc(s.as_of || "")})</small>` : ` <small class="c-short">(not available right now)</small>`}`).join(" · ");
  }
  function paintCurve() {
    const B = W.bonds, el = $("bond-curve"), curves = B.data.curves || {};
    const ccs = Object.keys(curves).filter((cc) => !B.cc || cc === B.cc);
    const all = ccs.flatMap((cc) => curves[cc]);
    if (!all.length) { el.innerHTML = '<p class="mk-empty">No curve to show.</p>'; return; }
    const W0 = 520, H0 = 240, L = 40, R = 12, T = 12, Bm = 28;
    const lx = (y) => Math.log(y + 0.25), x0 = lx(Math.min(...all.map((p) => p[0]))), x1 = lx(Math.max(...all.map((p) => p[0])));
    let y0 = Math.min(...all.map((p) => p[1])), y1 = Math.max(...all.map((p) => p[1]));
    const pad = (y1 - y0) * 0.12 || 0.5; y0 -= pad; y1 += pad;
    const X = (y) => L + ((lx(y) - x0) / (x1 - x0 || 1)) * (W0 - L - R), Y = (v) => T + (1 - (v - y0) / (y1 - y0)) * (H0 - T - Bm);
    const ticksX = [0.25, 1, 2, 5, 10, 30].filter((t) => lx(t) >= x0 - 0.01 && lx(t) <= x1 + 0.01);
    const stepY = P.niceStep((y1 - y0) / 4) || 0.5, ticksY = [];
    for (let v = Math.ceil(y0 / stepY) * stepY; v <= y1; v += stepY) ticksY.push(+v.toFixed(4));
    const grid = ticksY.map((v) => `<line x1="${L}" x2="${W0 - R}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" class="pc-curve-grid"/><text x="${L - 6}" y="${(Y(v) + 3.5).toFixed(1)}" text-anchor="end">${v.toFixed(stepY < 0.1 ? 2 : 1)}%</text>`).join("")
      + ticksX.map((t) => `<text x="${X(t).toFixed(1)}" y="${H0 - 8}" text-anchor="middle">${t < 1 ? `${Math.round(t * 12)}M` : `${t}Y`}</text>`).join("");
    const lines = ccs.map((cc) => {
      const pts = curves[cc].slice().sort((a, b) => a[0] - b[0]);
      return `<polyline points="${pts.map((p) => `${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ")}" fill="none" stroke="${CC_COLOR[cc] || "var(--accent)"}" stroke-width="2" stroke-linejoin="round"/>`
        + pts.map((p) => `<circle cx="${X(p[0]).toFixed(1)}" cy="${Y(p[1]).toFixed(1)}" r="2.4" fill="${CC_COLOR[cc] || "var(--accent)"}"><title>${cc} ${p[0] < 1 ? Math.round(p[0] * 12) + "M" : p[0] + "Y"}: ${p[1]}%</title></circle>`).join("");
    }).join("");
    const keys = ccs.map((cc) => `<span><i style="background:${CC_COLOR[cc] || "var(--accent)"}"></i>${esc((B.data.sources.find((s) => s.cc === cc) || {}).country || cc)}</span>`).join("");
    el.innerHTML = `<svg viewBox="0 0 ${W0} ${H0}" class="pc-curve-svg" role="img" aria-label="Government bond yield curves by maturity">${grid}${lines}</svg><div class="pc-curve-keys">${keys}</div>`;
  }
  async function selectBond(id) {
    const B = W.bonds;
    B.sel = id;
    document.querySelectorAll("#bond-rows tr[data-id]").forEach((tr) => tr.setAttribute("aria-selected", tr.dataset.id === id ? "true" : "false"));
    const row = B.data.bonds.find((b) => b.id === id);
    $("bond-hist-title").textContent = row ? `${row.name}: daily yield` : "History";
    const box = $("bond-hist");
    try {
      const d = await F.getJSON(`/api/premium/bonds/history?id=${enc(id)}`);
      if (B.sel !== id) return;
      if (!d.ok || !LW) { box.innerHTML = `<p class="mk-empty">${esc(d.error || "No history.")}</p>`; return; }
      if (!B.chart) {
        box.innerHTML = "";
        const t = theme();
        t.timeScale.timeVisible = false;
        B.chart = LW.createChart(box, Object.assign({ autoSize: true }, t, { localization: { priceFormatter: (p) => `${p.toFixed(3)}%` } }));
        B.series = B.chart.addSeries(LW.AreaSeries, { lineColor: css("--accent"), topColor: alpha(css("--accent"), 0.25), bottomColor: alpha(css("--accent"), 0.02), lineWidth: 2 });
      }
      B.series.setData(d.points.map(([day, v]) => ({ time: day, value: v })));
      B.chart.timeScale().fitContent();
    } catch (e) {
      if (e.message !== "login") box.innerHTML = '<p class="mk-empty">History is not available right now.</p>';
    }
  }

  // ================================================================== toolbar, popovers, features
  function renderLayouts() {
    $("layouts").innerHTML = LAYOUTS.map((l) => {
      const rows = Math.ceil(l.n / l.cols);
      const cells = Array.from({ length: l.n }, () => "<i></i>").join("");
      return `<button type="button" data-layout="${l.key}" aria-pressed="${l.key === W.layout}" title="${l.n} chart${l.n === 1 ? "" : "s"}${l.key === "2v" ? " stacked" : ""}">
        <span class="pc-lay" style="--c:${l.cols};--r:${rows}">${cells}</span><b>${l.n}</b></button>`;
    }).join("");
  }
  function setLayout(key) {
    if (!LAYOUTS.some((l) => l.key === key)) return;
    W.layout = key;
    buildGrid();
  }
  function closePops(except) {
    document.querySelectorAll(".pc-bar .mk-pop").forEach((p) => {
      if (p === except) return;
      p.hidden = true;
      const b = document.querySelector(`[aria-controls="${p.id}"]`);
      if (b) b.setAttribute("aria-expanded", "false");
    });
  }
  function togglePop(btnId, popId) {
    const btn = $(btnId), pop = $(popId);
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = pop.hidden;
      closePops(pop);
      closeFloat();
      pop.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  function feature(key) {
    const cell = W.cells[W.active] || W.cells[0];
    const head = (act) => cell && cell.el.querySelector(`[data-act="${act}"]`);
    if (key !== "bonds") showView("charts");
    const crypto = () => {
      const c = cfgOf(cell);
      if (isForex(c.sym) || c.symB) { setCfg(cell, { sym: "BTC/USDT", symB: null }, {}); F.toast("Second and tick charts use crypto trades: switched to BTC/USDT."); }
    };
    switch (key) {
      case "multi": setLayout("16"); F.toast("16 charts in this tab. Pick a coin, interval and type on each one."); break;
      case "live": { const b = $("pc-live"); closePops(); $("live-pop").hidden = false; b.setAttribute("aria-expanded", "true"); paintLive(); break; }
      case "custom": openIv(cell, head("iv"), true, "m"); break;
      case "customsec": crypto(); openIv(cell, head("iv"), true, "s"); $("iv-n").value = 7; break;
      case "seconds": crypto(); setCfg(cell, { iv: "5s" }, {}); break;
      case "ticks": crypto(); setCfg(cell, { iv: "100T" }, {}); break;
      case "range": setCfg(cell, Object.assign({ type: "range" }, ["time", "month"].includes(ivOf(cell).kind) && ivOf(cell).seconds && ivOf(cell).seconds >= 3600 ? { iv: "1m" } : {}), {}); break;
      case "pricebased": setCfg(cell, Object.assign({ type: "renko" }, ivOf(cell).kind === "month" || (ivOf(cell).seconds || 0) >= 3600 ? { iv: "5m" } : {}), {}); break;
      case "replay": if (!W.replay) replayStart(); break;
      case "spread": openSpread(cell); break;
      case "export": openExport(); break;
      case "bonds": showView("bonds"); break;
      default: break;
    }
  }
  function openExport() { markActive(); $("export-dlg").hidden = false; setTimeout(() => $("export-form").querySelector("button[type=submit]").focus(), 30); }

  function bind() {
    togglePop("layout-btn", "layout-pop");
    togglePop("sync-btn", "sync-pop");
    togglePop("pc-live", "live-pop");
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".pc-bar .mk-pop-wrap, .pc-bar .mk-pop, #pc-live")) closePops();
      if (floatOpen && !floatOpen.pop.contains(e.target) && !floatOpen.anchor.contains(e.target)) closeFloat();
    });
    document.querySelectorAll(".pc-bar [data-close], .pc-float [data-close]").forEach((b) => b.addEventListener("click", () => { closePops(); closeFloat(); }));
    $("layouts").addEventListener("click", (e) => { const b = e.target.closest("[data-layout]"); if (b) { setLayout(b.dataset.layout); closePops(); } });
    document.querySelectorAll("#sync-pop [data-sync]").forEach((inp) => {
      inp.checked = !!W.sync[inp.dataset.sync];
      inp.addEventListener("change", () => {
        W.sync[inp.dataset.sync] = inp.checked;
        save();
        const cell = W.cells[W.active];
        if (inp.checked && cell && inp.dataset.sync === "symbol") setCfg(cell, { sym: cfgOf(cell).sym }, { all: true });
        if (inp.checked && cell && inp.dataset.sync === "interval") setCfg(cell, { iv: cfgOf(cell).iv }, { all: true });
      });
    });
    $("replay-btn").addEventListener("click", replayToggle);
    $("rp-exit").addEventListener("click", replayExit);
    $("rp-play").addEventListener("click", replayPlay);
    $("rp-step").addEventListener("click", () => { replayPause(); replayStep(1); });
    $("rp-back").addEventListener("click", () => { replayPause(); replayStep(-1); });
    $("rp-pick").addEventListener("click", () => {
      if (!W.replay) return;
      if (W.replay.state === "pick") replayBack200();
      else { replayPause(); W.replay.state = "pick"; document.body.classList.add("pc-picking"); replayInfo(); }
    });
    $("rp-all").addEventListener("change", () => { if (W.replay) { W.replay.all = $("rp-all").checked; W.cells.forEach((c) => render(c)); replayInfo(); } });
    $("rp-speed").addEventListener("click", (e) => {
      const b = e.target.closest("[data-speed]");
      if (!b) return;
      $("rp-speed").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
      if (W.replay) W.replay.speed = Number(b.dataset.speed);
    });
    $("export-btn").addEventListener("click", openExport);
    $("export-cancel").addEventListener("click", () => { $("export-dlg").hidden = true; });
    $("export-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const all = $("export-form").querySelector('input[name="exp"]:checked').value === "all";
      $("export-dlg").hidden = true;
      exportCells(all ? W.cells : [W.cells[W.active]].filter(Boolean), $("export-visible").checked);
    });
    // interval popover
    $("iv-groups").addEventListener("click", (e) => { const b = e.target.closest("[data-iv]"); if (b && !b.disabled) chooseIv(b.dataset.iv); });
    $("iv-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const name = `${parseInt($("iv-n").value, 10)}${$("iv-unit").value}`;
      if (!P.parseInterval(name)) { $("iv-hint").classList.add("c-short"); F.toast("That interval is outside the allowed range."); return; }
      $("iv-hint").classList.remove("c-short");
      chooseIv(name);
    });
    // chart type popover
    $("type-list").addEventListener("click", (e) => { const b = e.target.closest("[data-type]"); if (b) chooseType(b.dataset.type); });
    $("type-set").addEventListener("change", (e) => {
      const inp = e.target.closest("[data-set]");
      if (!inp || !typeCell) return;
      const c = cfgOf(typeCell), k = inp.dataset.set;
      if (k === "volume") { c.volume = inp.checked; W.cfgs[typeCell.i] = cleanCfg(c, typeCell.i); save(); render(typeCell); return; }
      c[k] = inp.value === "" ? null : Number(inp.value);
      W.cfgs[typeCell.i] = cleanCfg(c, typeCell.i);
      save();
      render(typeCell);
    });
    // spread dialog
    $("spread-a").addEventListener("click", () => { W.pickFor = { field: "spreadA" }; $("spread-dlg").hidden = true; W.picker.open($("spread-a")); });
    $("spread-b").addEventListener("click", () => { W.pickFor = { field: "spreadB" }; $("spread-dlg").hidden = true; W.picker.open($("spread-b")); });
    $("spread-cancel").addEventListener("click", closeSpread);
    $("spread-off").addEventListener("click", () => { closeSpread(); if (SP.cell) setCfg(SP.cell, { symB: null }, {}); });
    $("spread-form").addEventListener("submit", (e) => {
      e.preventDefault();
      if (SP.a === SP.b) { F.toast("Choose two different markets."); return; }
      closeSpread();
      const cell = SP.cell;
      if (!cell) return;
      const patch = { sym: SP.a, symB: SP.b, op: $("spread-op").value };
      if (fromTrades(ivOf(cell))) patch.iv = "1h";
      setCfg(cell, patch, {});
    });
    document.querySelectorAll(".mk-modal").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m) m.hidden = true; }));
    // views
    $("tab-charts").addEventListener("click", () => showView("charts"));
    $("tab-bonds").addEventListener("click", () => showView("bonds"));
    $("bond-countries").addEventListener("click", (e) => { const b = e.target.closest("[data-cc]"); if (!b) return; W.bonds.cc = b.dataset.cc; paintBondFilters(); paintBonds(); paintCurve(); });
    $("bond-mat").addEventListener("click", (e) => {
      const b = e.target.closest("[data-mat]");
      if (!b) return;
      W.bonds.mat = b.dataset.mat;
      $("bond-mat").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
      paintBonds();
    });
    $("bond-q").addEventListener("input", () => { W.bonds.q = $("bond-q").value; if (W.bonds.data) paintBonds(); });
    document.querySelector("#bond-table thead").addEventListener("click", (e) => {
      const b = e.target.closest("[data-sort]");
      if (!b || !W.bonds.data) return;
      const B = W.bonds;
      B.dir = B.sort === b.dataset.sort ? -B.dir : (b.dataset.sort === "name" || b.dataset.sort === "years" ? 1 : -1);
      B.sort = b.dataset.sort;
      paintBonds();
    });
    $("bond-rows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) selectBond(tr.dataset.id); });
    $("bond-rows").addEventListener("keydown", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); selectBond(tr.dataset.id); } });
    // keyboard
    document.addEventListener("keydown", (e) => {
      const typing = /^(INPUT|SELECT|TEXTAREA)$/.test((e.target && e.target.tagName) || "");
      if (e.key === "Escape") {
        if (!$("spread-dlg").hidden) { closeSpread(); return; }
        if (!$("export-dlg").hidden) { $("export-dlg").hidden = true; return; }
        if (floatOpen) { closeFloat(); return; }
        closePops();
        if (W.replay && W.replay.state === "pick" && W.replay.time == null) replayExit();
        return;
      }
      if (typing || !W.replay || W.replay.state !== "on") return;
      if (e.key === " ") { e.preventDefault(); replayPlay(); }
      if (e.key === "ArrowRight") { e.preventDefault(); replayPause(); replayStep(1); }
      if (e.key === "ArrowLeft") { e.preventDefault(); replayPause(); replayStep(-1); }
    });
    window.addEventListener("resize", placeFloat);
    window.addEventListener("scroll", placeFloat, true);
    document.addEventListener("themechange", () => {
      W.cells.forEach((cell) => {
        if (!cell.chart) return;
        cell.chart.applyOptions(theme());
        cell.kind = null;
        render(cell);
      });
      if (W.bonds.chart) { W.bonds.chart.applyOptions(theme()); W.bonds.series.applyOptions({ lineColor: css("--accent"), topColor: alpha(css("--accent"), 0.25), bottomColor: alpha(css("--accent"), 0.02) }); }
      if (W.bonds.data) paintCurve();
    });
  }

  async function start() {
    loadPrefs();
    applyUrl();
    W.picker = F.coinPicker({ onSelect: onPick, current: pickCurrent });
    try {
      const c = await F.getJSON("/coins");
      W.coins = { crypto: (c && c.crypto) || [], forex: (c && c.forex) || [] };
      W.picker.setCoins(W.coins);
    } catch (e) { if (e.message === "login") return; }
    try { const t = await F.getJSON("/api/market/tickers"); if (t && t.tickers) W.picker.setTickers(t.tickers); } catch (e) { /* prices in the picker are optional */ }
    bind();
    Hub.on(onPrices);
    buildGrid();
    Hub.start();
    window.PremiumWorkspace = { feature, setLayout, showView };
    if (W.feature) setTimeout(() => feature(W.feature), 400);
    if (W.feature) history.replaceState(null, "", location.pathname);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
