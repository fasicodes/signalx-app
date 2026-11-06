/* Signals FM Live chart: the "Premium chart Access" features (free for members right now), used by chart-page.js.
   The main chart keeps every tool it had (indicators, drawings, signal levels, alerts); this file adds what it
   needs for the premium features and the extra charts of a multi-chart layout:

   window.SFMPremium = {
     TYPES, TYPE_NAME, TYPE_SHORT, PRICE_TYPES, OP_SIGN, LAYOUTS, MAX_LIVE,
     derive(base, type, cfg)      -> { items, extra, pnf, capped }   Range / Renko / Kagi / Line break / P&F / Heikin
     autoBox(base)                -> a round box size from the average candle (ATR 14)
     PnFPrimitive                 series primitive that draws Point & figure X and O boxes
     intervalsHTML(current, opts) / typeSettingsHTML(...)   menus shared by the main chart and the extra charts
     Trades.sub(coin, listener)   one trade feed per coin for second and tick charts (crypto only)
     Hub                          one price feed for every chart of every open tab (up to MAX_LIVE charts)
     Grid                         the extra charts next to the main chart (layouts of up to 16 charts)
     Bonds                        bond screener panel
     csv(name, header, rows)      CSV download
   }
   Data: /api/premium/bars, /api/premium/trades, /api/premium/prices, /api/premium/bonds(/history), /coins.
   Pure functions (intervals, buckets, Renko, Kagi ...): static/premium-chart-core.js (window.PCC). */
(function () {
  "use strict";
  const F = window.SFM, P = window.PCC;
  if (!F || !P) return;
  const LWlib = () => window.LightweightCharts;
  const $ = (id) => document.getElementById(id);
  const enc = encodeURIComponent;
  const esc = F.esc;

  const MAX_LIVE = 200;
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
  const LAYOUTS = [
    { key: "1", n: 1, cols: 1 }, { key: "2", n: 2, cols: 2 }, { key: "2v", n: 2, cols: 1 }, { key: "3", n: 3, cols: 3 },
    { key: "4", n: 4, cols: 2 }, { key: "6", n: 6, cols: 3 }, { key: "8", n: 8, cols: 4 }, { key: "9", n: 9, cols: 3 },
    { key: "12", n: 12, cols: 4 }, { key: "16", n: 16, cols: 4 },
  ];
  const DEFAULTS = ["ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT",
                    "LTC/USDT", "DOT/USDT", "TRX/USDT", "BCH/USDT", "ATOM/USDT", "NEAR/USDT", "UNI/USDT", "BTC/USDT"];
  const ICON = {
    dl: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
    caret: '<svg class="mk-caret" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>',
  };

  // ================================================================== helpers
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  function alpha(hex, a) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim());
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
  const pad = (n) => String(n).padStart(2, "0");
  function fmtStamp(sec, iv) {
    if (sec == null) return "";
    const d = new Date(sec * 1000);
    const day = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
    if (iv && (iv.unit === "D" || iv.unit === "W" || iv.unit === "M")) return day;
    const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    return iv && (iv.kind === "second" || iv.kind === "tick") ? `${day} ${hm}:${pad(d.getUTCSeconds())}` : `${day} ${hm}`;
  }
  const fmtNum = (v) => (v == null ? "auto" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 10 }));
  function csv(name, header, rows) {
    const blob = new Blob(["﻿" + P.toCSV(header, rows)], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
  }
  const fileSym = (sym, symB, op) => {
    const s = (x) => x.replace("/", "-");
    return symB ? `${s(sym)}_${{ "/": "div", "-": "minus", "+": "plus", "*": "times" }[op] || "div"}_${s(symB)}` : s(sym);
  };
  const symLabel = (sym, symB, op) => (symB ? `${F.base(sym)} ${OP_SIGN[op] || "÷"} ${F.base(symB)}` : sym);

  // ================================================================== price-based charts (shared by every chart)
  function autoBox(base) {
    const tail = base.slice(-300);
    return P.niceStep(P.atr(tail, 14)) || P.niceStep(Math.abs(tail.length ? tail[tail.length - 1].close : 1) * 0.002) || 1;
  }
  // base: time-based bars (time, open, high, low, close, volume). Items of price-based types carry `t` (the real
  // time they formed) and are drawn one after another (PCC.indexed).
  function derive(base, type, cfg) {
    cfg = cfg || {};
    let items = base, extra = "", pnf = null;
    if (type === "heikin") items = P.heikinAshi(base);
    else if (PRICE_TYPES.has(type)) {
      const auto = autoBox(base), tag = (v) => `${fmtNum(v)}${v === auto ? " (auto)" : ""}`;
      if (type === "range") { const s = cfg.range || auto; items = P.rangeBars(base, s); extra = `range ${tag(s)}`; }
      else if (type === "renko") { const b = cfg.box || auto; items = P.renko(base, b); extra = `box ${tag(b)}`; }
      else if (type === "kagi") {
        const r = cfg.box || auto, cols = P.kagi(base, r);
        items = cols.length ? [{ t: cols[0].t, open: cols[0].open, high: cols[0].open, low: cols[0].open, close: cols[0].open, yang: cols[0].yang, volume: 0 }]
          .concat(cols.map((k) => Object.assign({ volume: 0 }, k))) : [];
        extra = `reversal ${tag(r)}`;
      } else if (type === "linebreak") { items = P.lineBreak(base, cfg.lines || 3); extra = `${cfg.lines || 3}-line break`; }
      else if (type === "pnf") {
        const b = cfg.box || auto;
        items = P.pointFigure(base, b, cfg.rev || 3).map((k) => Object.assign({ volume: 0 }, k));
        pnf = { cols: items, box: b };
        extra = `box ${tag(b)}, ${cfg.rev || 3}-box reversal`;
      }
    }
    return { items, extra, pnf, capped: !!items.capped };
  }

  // Point & figure: the candles of a P&F chart are invisible (they only set the price scale); this draws the boxes
  class PnFView {
    constructor(src) { this.src = src; }
    renderer() {
      const src = this.src;
      return {
        draw(target) {
          const g = src.get(), pnf = g && g.pnf, chart = g && g.chart, series = g && g.series;
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
              const half = Math.max(1.5, Math.min(spacing * 0.42, Math.abs(ya - yb) * 0.42));
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
    constructor(get) { this.get = get; this.views = [new PnFView(this)]; this.request = null; }
    attached(p) { this.request = p.requestUpdate; }
    detached() { this.request = null; }
    updateAllViews() {}
    paneViews() { return this.views; }
  }

  // ================================================================== menus (HTML shared by the main chart and the extra charts)
  // opts: { tradesOk: false disables ticks and seconds (forex, spread charts) }
  function intervalsHTML(current, opts) {
    const off = opts && opts.tradesOk === false;
    return IV_GROUPS.map(([name, list]) => {
      const dis = off && (name === "Ticks" || name === "Seconds");
      return `<div class="pp-iv-group"><span>${esc(name)}${dis ? " · single crypto coins only" : ""}</span><div>${list.map((n) => {
        const iv = P.parseInterval(n);
        return `<button type="button" data-iv="${esc(iv.name)}" aria-pressed="${!!current && iv.name === current.name}" ${dis ? "disabled" : ""} title="${esc(iv.long)}">${esc(iv.short)}</button>`;
      }).join("")}</div></div>`;
    }).join("") + `<form class="pp-custom" data-iv-form autocomplete="off">
        <b>Custom interval</b>
        <div class="pp-custom-row">
          <input class="mk-input tnum" data-iv-n type="number" min="1" max="10000" step="1" value="${current ? current.n : 7}" aria-label="Number">
          <select class="mk-input" data-iv-unit aria-label="Unit">
            ${[["T", "ticks"], ["s", "seconds"], ["m", "minutes"], ["h", "hours"], ["D", "days"], ["W", "weeks"], ["M", "months"]]
              .map(([u, t]) => `<option value="${u}"${current && current.unit === u ? " selected" : ""}${off && (u === "T" || u === "s") ? " disabled" : ""}>${t}</option>`).join("")}
          </select>
          <button class="mk-btn primary" type="submit">Apply</button>
        </div>
        <p class="mk-note" data-iv-hint>Seconds 1–300, minutes 1–1440, hours 1–168, days 1–365, weeks 1–52, months 1, 2, 3, 4, 6 or 12, ticks 1–10,000.</p>
      </form>`;
  }
  // reads a custom interval from a form made by intervalsHTML(); null when it is outside the allowed range
  function intervalFromForm(form) {
    const n = parseInt(form.querySelector("[data-iv-n]").value, 10), unit = form.querySelector("[data-iv-unit]").value;
    const iv = P.parseInterval(`${n}${unit}`);
    form.querySelector("[data-iv-hint]").classList.toggle("c-short", !iv);
    return iv;
  }
  function typesHTML(current) {
    const btn = (t) => `<button type="button" role="menuitemradio" data-type="${t[0]}" aria-checked="${current === t[0]}">${esc(t[1])}</button>`;
    return `<div class="pp-type-group"><span>Time-based</span>${TYPES.filter((t) => t[2] === "time").map(btn).join("")}</div>
      <div class="pp-type-group"><span>Price-based (no time axis)</span>${TYPES.filter((t) => t[2] === "price").map(btn).join("")}</div>`;
  }
  // settings of the price-based types: box / range size, reversal, lines; `auto` is the automatic size
  function typeSettingsHTML(type, cfg, auto, ivLong, volume) {
    const field = (label, key, hint) => `<label class="pp-field"><span>${label}</span>
      <input class="mk-input tnum" type="number" min="0" step="any" data-set="${key}" value="${cfg[key] || ""}" placeholder="auto${auto ? ` (${fmtNum(auto)})` : ""}">
      ${hint ? `<small>${hint}</small>` : ""}</label>`;
    let html = "";
    if (type === "range") html = field("Range size (price)", "range", "Each bar covers this much price. Empty = from the average candle (ATR 14).");
    else if (type === "renko") html = field("Box size (price)", "box", "Empty = from the average candle (ATR 14).");
    else if (type === "kagi") html = field("Reversal amount (price)", "box", "The line turns after a move back of this size.");
    else if (type === "pnf") html = field("Box size (price)", "box", "") + `<label class="pp-field"><span>Reversal (boxes)</span><input class="mk-input tnum" type="number" min="1" max="10" step="1" data-set="rev" value="${cfg.rev || 3}"></label>`;
    else if (type === "linebreak") html = `<label class="pp-field"><span>Lines to break</span><input class="mk-input tnum" type="number" min="1" max="10" step="1" data-set="lines" value="${cfg.lines || 3}"></label>`;
    if (PRICE_TYPES.has(type)) html += `<p class="mk-note">Built from the ${esc(ivLong)} candles of this chart. Pick 1m or 5m for intraday detail.</p>`;
    if (volume != null) html += `<label class="pp-check"><input type="checkbox" data-set="volume" ${volume ? "checked" : ""}> <span>Show volume</span></label>`;
    return html;
  }

  // ---------------------------------------------------------------- a floating popover (the extra charts' menus)
  let floatOpen = null;
  function floatEl() {
    let el = $("pp-float");
    if (!el) {
      el = document.createElement("div");
      el.id = "pp-float";
      el.className = "mk-pop pp-float";
      el.hidden = true;
      el.setAttribute("role", "dialog");
      document.body.appendChild(el);
      el.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeFloat(); });
    }
    return el;
  }
  function openFloat(anchor, title, html, bind) {
    closeFloat();
    const el = floatEl();
    el.innerHTML = `<div class="mk-pop-head"><b>${esc(title)}</b><button type="button" class="mk-x" data-close aria-label="Close">&times;</button></div>${html}`;
    el.hidden = false;
    floatOpen = { el, anchor };
    anchor.setAttribute("aria-expanded", "true");
    placeFloat();
    if (bind) bind(el);
  }
  function placeFloat() {
    if (!floatOpen) return;
    const { el, anchor } = floatOpen;
    if (window.matchMedia("(max-width: 760px)").matches) { el.style.left = ""; el.style.top = ""; return; }
    const r = anchor.getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight;
    el.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - w - 8))}px`;
    let top = r.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
    el.style.top = `${top}px`;
  }
  function closeFloat() {
    if (!floatOpen) return;
    floatOpen.el.hidden = true;
    floatOpen.anchor.setAttribute("aria-expanded", "false");
    floatOpen = null;
  }
  document.addEventListener("click", (e) => {
    if (floatOpen && !floatOpen.el.contains(e.target) && !floatOpen.anchor.contains(e.target)) closeFloat();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && floatOpen) { e.stopPropagation(); closeFloat(); } }, true);
  window.addEventListener("resize", placeFloat);
  window.addEventListener("scroll", placeFloat, true);

  // ---------------------------------------------------------------- spread dialog
  const Spread = (() => {
    let opts = null, a = null, b = null, picker = null, picking = null;
    function el() {
      let m = $("pp-spread");
      if (m) return m;
      m = document.createElement("div");
      m.className = "mk-modal";
      m.id = "pp-spread";
      m.hidden = true;
      m.innerHTML = `<form class="mk-modal-box pp-spread-box" role="dialog" aria-modal="true" aria-labelledby="pp-spread-title">
          <h2 class="mk-h2" id="pp-spread-title">Spread chart</h2>
          <p class="mk-note">One market against another. Each bar is worked out from the two markets' bars of the same time.</p>
          <div class="pp-spread-row">
            <button type="button" class="mk-btn pp-spread-sym" data-leg="a">BTC/USDT</button>
            <select class="mk-input" data-op aria-label="Operation">
              <option value="/">÷ divided by</option><option value="-">− minus</option><option value="+">+ plus</option><option value="*">× times</option>
            </select>
            <button type="button" class="mk-btn pp-spread-sym" data-leg="b">ETH/USDT</button>
          </div>
          <div class="mk-modal-btns">
            <button type="button" class="mk-btn danger" data-off>Remove spread</button>
            <button type="button" class="mk-btn" data-cancel>Cancel</button>
            <button type="submit" class="mk-btn primary">Show spread</button>
          </div>
        </form>`;
      document.body.appendChild(m);
      m.addEventListener("click", (e) => {
        if (e.target === m || e.target.closest("[data-cancel]")) { close(); return; }
        if (e.target.closest("[data-off]")) { close(); if (opts && opts.onRemove) opts.onRemove(); return; }
        const leg = e.target.closest("[data-leg]");
        if (leg) { picking = leg.dataset.leg; m.hidden = true; pick().open(leg); }
      });
      m.querySelector("form").addEventListener("submit", (e) => {
        e.preventDefault();
        if (a === b) { F.toast("Choose two different markets."); return; }
        close();
        if (opts && opts.onApply) opts.onApply({ a, b, op: m.querySelector("[data-op]").value });
      });
      document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !m.hidden) { e.stopPropagation(); close(); } }, true);
      return m;
    }
    function pick() {
      if (!picker) {
        picker = F.coinPicker({
          current: () => (picking === "a" ? a : b),
          onSelect: (sym) => { if (picking === "a") a = sym; else b = sym; paint(); el().hidden = false; },
        });
        picker.setCoins(Grid.coins());
      }
      return picker;
    }
    function paint() {
      const m = el();
      m.querySelector('[data-leg="a"]').textContent = a;
      m.querySelector('[data-leg="b"]').textContent = b;
    }
    function open(o) {
      opts = o;
      a = o.a;
      b = o.b || (o.a === "ETH/USDT" ? "BTC/USDT" : "ETH/USDT");
      const m = el();
      paint();
      m.querySelector("[data-op]").value = o.op || "/";
      m.querySelector("[data-off]").hidden = !o.active;
      m.hidden = false;
      setTimeout(() => m.querySelector("[data-op]").focus(), 30);
    }
    function close() { el().hidden = true; }
    return { open, close };
  })();

  // ================================================================== trades for second and tick charts (one feed per coin)
  const Trades = (() => {
    const feeds = new Map();
    let timer = null;
    function sub(coin, listener) {
      let f = feeds.get(coin);
      if (!f) { f = { coin, subs: new Set(), rows: [], ready: false, busy: false }; feeds.set(coin, f); boot(f); }
      f.subs.add(listener);
      if (f.ready) listener.init(f.rows);
      schedule();
      return () => { f.subs.delete(listener); if (!f.subs.size && feeds.get(coin) === f) feeds.delete(coin); };
    }
    async function boot(f) {
      try {
        const d = await F.getJSON(`/api/premium/trades?coin=${enc(f.coin)}&depth=10`);
        if (feeds.get(f.coin) !== f) return;
        if (!d.ok) { f.subs.forEach((l) => l.error(d.error || "Trades are not available right now.")); return; }
        f.rows = d.trades || [];
        f.ready = true;
        f.server = d.server_time;
        f.subs.forEach((l) => l.init(f.rows, d.server_time));
      } catch (e) {
        if (e.message === "login" || feeds.get(f.coin) !== f) return;
        f.subs.forEach((l) => l.error("Trades are not available right now. Trying again…"));
        setTimeout(() => { if (feeds.get(f.coin) === f) boot(f); }, 6000);
      }
    }
    async function tick() {
      timer = null;
      if (!document.hidden) {
        for (const f of [...feeds.values()].filter((x) => x.ready && !x.busy)) {
          f.busy = true;
          try {
            const d = await F.getJSON(`/api/premium/trades?coin=${enc(f.coin)}`);
            if (d && d.ok && d.trades && d.trades.length) {
              const last = f.rows[f.rows.length - 1];
              const fresh = last ? d.trades.filter((t) => P.isNewer(t, last[0], last[4])) : d.trades;
              if (fresh.length) {
                f.rows = f.rows.concat(fresh);
                if (f.rows.length > 30000) f.rows = f.rows.slice(-30000);
                f.subs.forEach((l) => l.rows(fresh, d.server_time));
              }
            }
          } catch (e) { /* the next round tries again */ } finally { f.busy = false; }
        }
      }
      schedule();
    }
    function schedule() {
      if (timer || !feeds.size) return;
      timer = setTimeout(tick, Math.max(1000, 600 * feeds.size));
    }
    return { sub, count: () => feeds.size };
  })();

  // ================================================================== one price feed for every chart in every tab
  const Hub = (() => {
    const chan = "BroadcastChannel" in window ? new BroadcastChannel("sfm-premium-feed") : null;
    const tab = Math.random().toString(36).slice(2), born = Date.now();
    const peers = new Map(), listeners = [];
    let leader = false, asking = false, timer = null, syms = [], charts = 0, lastMsg = 0, release = null;
    const alive = (p) => Date.now() - p.ts < 9000;
    function announce() { if (chan) chan.postMessage({ type: "want", tab, born, syms, charts, ts: Date.now() }); }
    function union() {
      const set = new Set(syms);
      for (const p of peers.values()) if (alive(p)) p.syms.forEach((s) => set.add(s));
      return [...set].slice(0, 200);
    }
    function budget() {
      const list = [{ tab, born, charts }].concat([...peers.values()].filter(alive)).sort((x, y) => x.born - y.born);
      let used = 0;
      const allow = {};
      for (const p of list) { allow[p.tab] = Math.max(0, Math.min(p.charts, MAX_LIVE - used)); used += p.charts; }
      return { allow, charts: used, tabs: list.length };
    }
    function deliver(m) { lastMsg = Date.now(); listeners.forEach((f) => f(m)); }
    async function poll() {
      const list = union();
      if (!list.length || (document.hidden && !peers.size)) return;
      try {
        const d = await F.getJSON(`/api/premium/prices?coins=${enc(list.join(","))}`);
        const m = Object.assign({ type: "prices", ok: !!(d && d.ok), prices: (d && d.prices) || {}, server_time: d && d.server_time }, budget());
        deliver(m);
        if (chan) chan.postMessage(m);
      } catch (e) {
        if (e.message !== "login") deliver(Object.assign({ type: "prices", ok: false, prices: {} }, budget()));
      }
    }
    function becomeLeader() { if (leader) return; leader = true; poll(); timer = setInterval(poll, 2000); }
    function stopLeading() {
      leader = false;
      clearInterval(timer);
      if (release) { const r = release; release = null; r(); }
    }
    function ask() {
      if (asking || leader || document.hidden || !syms.length) return;
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
      if (document.hidden && leader && navigator.locks && chan) stopLeading();      // a hidden tab hands the feed over
      else if (!document.hidden) ask();
    });
    window.addEventListener("pagehide", () => { if (chan) chan.postMessage({ type: "bye", tab }); stopLeading(); });
    setInterval(() => {
      announce();
      // nobody has sent prices for 10 seconds (no lock support, or the lock is stuck): this tab polls itself
      if (syms.length && !leader && !document.hidden && Date.now() - lastMsg > 10000) becomeLeader();
    }, 3000);
    return {
      tab,
      want(list, n) { syms = list; charts = n; announce(); if (list.length) ask(); else if (leader) stopLeading(); },
      on(f) { listeners.push(f); },
      isLeader: () => leader,
    };
  })();

  // ================================================================== the extra charts of a multi-chart layout
  // init({ host: the grid element holding the main chart, main: adapter (see chart-page.js), coins })
  const Grid = (() => {
    const STORE = "sfm-chart-grid-v1";
    const G = { host: null, main: null, coins: { crypto: [], forex: [] }, layout: "1", cfgs: [], cells: [], picker: null, pickCell: null,
                sync: { crosshair: true, symbol: false, interval: false }, replay: null, allow: undefined, totals: { charts: 1, tabs: 1 },
                liveOk: null, serverOffset: 0 };
    const serverNow = () => Date.now() / 1000 + G.serverOffset;
    const noteServer = (t) => { if (t) G.serverOffset = Number(t) - Date.now() / 1000; };
    const validSym = (s) => G.coins.crypto.includes(s) || G.coins.forex.includes(s);
    const cfgOf = (cell) => G.cfgs[cell.k];
    const ivOf = (cell) => P.parseInterval(cfgOf(cell).iv) || P.parseInterval("1h");
    const fromTrades = (iv) => iv.kind === "second" || iv.kind === "tick";
    const layoutDef = (key) => LAYOUTS.find((l) => l.key === key) || LAYOUTS[0];

    function defaultCfg(k) {
      const main = G.main ? G.main.coin() : "BTC/USDT";
      const list = DEFAULTS.filter((s) => s !== main && (!G.coins.crypto.length || validSym(s)));
      return { sym: list[k % Math.max(1, list.length)] || "ETH/USDT", symB: null, op: "/", iv: G.main ? G.main.tfName() : "1h",
               type: "candles", box: null, rev: 3, lines: 3, range: null, volume: true };
    }
    function cleanCfg(c, k) {
      const d = defaultCfg(k), o = Object.assign({}, d, c && typeof c === "object" ? c : {});
      if (typeof o.sym !== "string" || (G.coins.crypto.length && !validSym(o.sym))) o.sym = d.sym;
      if (o.symB && (typeof o.symB !== "string" || (G.coins.crypto.length && !validSym(o.symB)) || o.symB === o.sym)) o.symB = null;
      if (!OP_SIGN[o.op]) o.op = "/";
      o.iv = (P.parseInterval(o.iv) || P.parseInterval("1h")).name;
      if (!TYPE_NAME[o.type]) o.type = "candles";
      for (const key of ["box", "range"]) o[key] = Number(o[key]) > 0 ? Number(o[key]) : null;
      o.rev = Math.max(1, Math.min(10, parseInt(o.rev, 10) || 3));
      o.lines = Math.max(1, Math.min(10, parseInt(o.lines, 10) || 3));
      o.volume = o.volume !== false;
      return o;
    }
    function load() {
      const s = F.store.getJSON(STORE, {}) || {};
      if (LAYOUTS.some((l) => l.key === s.layout)) G.layout = s.layout;
      if (s.sync && typeof s.sync === "object") for (const k in G.sync) if (typeof s.sync[k] === "boolean") G.sync[k] = s.sync[k];
      G.cfgs = Array.isArray(s.cfgs) ? s.cfgs.slice(0, 15) : [];
    }
    const save = () => F.store.setJSON(STORE, { layout: G.layout, sync: G.sync, cfgs: G.cfgs.slice(0, 15) });

    function init(o) {
      G.host = o.host;
      G.main = o.main;
      G.coins = o.coins || G.coins;
      G.picker = F.coinPicker({
        current: () => (G.pickCell ? cfgOf(G.pickCell).sym : null),
        onSelect: (sym) => { const cell = G.pickCell; G.pickCell = null; if (cell) setCfg(cell, { sym }, { all: G.sync.symbol }); },
      });
      G.picker.setCoins(G.coins);
      load();
      Hub.on(onPrices);
      setLayout(G.layout, true);
    }
    function setLayout(key, quiet) {
      if (!LAYOUTS.some((l) => l.key === key)) return;
      G.layout = key;
      const L = layoutDef(key), host = G.host, extra = L.n - 1;
      host.dataset.layout = L.key;
      host.style.setProperty("--cols", L.cols);
      host.style.setProperty("--rows", Math.ceil(L.n / L.cols));
      while (G.cfgs.length < extra) G.cfgs.push(defaultCfg(G.cfgs.length));
      G.cfgs = G.cfgs.map(cleanCfg);
      while (G.cells.length > extra) destroy(G.cells.pop());
      for (let k = G.cells.length; k < extra; k++) G.cells.push(create(k));
      host.classList.toggle("is-multi", extra > 0);
      refreshWant();
      save();
      if (!quiet && G.onChange) G.onChange();
    }

    // ------------------------------------------------------------ one extra chart
    function create(k) {
      const el = document.createElement("div");
      el.className = "pc-cell";
      el.dataset.k = k;
      el.setAttribute("role", "group");
      el.setAttribute("aria-label", `Chart ${k + 2}`);
      el.innerHTML = `
        <div class="pc-cell-head">
          <button type="button" class="pc-sym" data-act="sym" title="Change coin or pair"><img alt="" width="18" height="18"><b></b>${ICON.caret}</button>
          <button type="button" class="pc-chip" data-act="iv" title="Interval" aria-haspopup="dialog" aria-expanded="false"></button>
          <button type="button" class="pc-chip" data-act="type" title="Chart type" aria-haspopup="dialog" aria-expanded="false"></button>
          <button type="button" class="pc-chip pc-chip-spread" data-act="spread" title="Spread chart: one market against another" aria-pressed="false">A÷B</button>
          <span class="pc-q"><b class="tnum"></b><small class="tnum"></small></span>
          <span class="pc-cell-r">
            <button type="button" class="pc-ico-btn" data-act="export" title="Download this chart (CSV)" aria-label="Download this chart as CSV">${ICON.dl}</button>
          </span>
        </div>
        <div class="pc-cell-body">
          <div class="pc-chart"></div>
          <div class="pc-legend" aria-hidden="true"></div>
          <div class="pc-msg">Loading…</div>
        </div>`;
      G.host.appendChild(el);
      const cell = { k, el, chart: null, main: null, vol: null, kind: null, A: [], B: [], tb: null, unsubTrades: null, real: null, view: [],
                     items: [], seq: 0, more: true, loadingOlder: false, nextStart: null, lastSync: 0, syncing: false, paused: false,
                     price: null, chg: null, pnf: null, prim: null, timeBased: true, renderTimer: null, lastRender: 0, retry: null, hover: null, extra: "" };
      el.querySelector(".pc-cell-head").addEventListener("click", (e) => onHead(cell, e));
      makeChart(cell);
      paintHead(cell);
      loadCell(cell);
      return cell;
    }
    function destroy(cell) {
      cell.seq++;
      if (cell.unsubTrades) { cell.unsubTrades(); cell.unsubTrades = null; }
      clearTimeout(cell.retry);
      clearTimeout(cell.renderTimer);
      if (cell.chart) { try { cell.chart.remove(); } catch (e) { /* already gone */ } }
      cell.el.remove();
    }
    function theme() {
      return {
        layout: { background: { type: "solid", color: "transparent" }, textColor: css("--dim"), fontFamily: "IBM Plex Mono, ui-monospace, monospace", fontSize: 11, attributionLogo: false },
        grid: { vertLines: { color: css("--line-soft") }, horzLines: { color: css("--line-soft") } },
        rightPriceScale: { borderColor: css("--line") },
        timeScale: { borderColor: css("--line"), timeVisible: true, secondsVisible: false, rightOffset: 4 },
        crosshair: { mode: 0, vertLine: { color: css("--faint"), labelBackgroundColor: css("--surface-2") }, horzLine: { color: css("--faint"), labelBackgroundColor: css("--surface-2") } },
      };
    }
    function decimalsFor(cell, v) { const c = cfgOf(cell); return c.symB ? F.decimals(v) : F.decimals(v, c.sym); }
    function fmtPrice(cell, v) {
      if (v == null || isNaN(v)) return "--";
      const d = decimalsFor(cell, v);
      return Number(v).toLocaleString("en-US", { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d });
    }
    function makeChart(cell) {
      const LW = LWlib();
      if (!LW) { msg(cell, "The chart library could not load. Check your connection and reload the page."); return; }
      const t = theme();
      t.localization = {
        timeFormatter: (x) => { const sec = cell.real ? cell.real[Math.round(x) - 1] : x; return sec == null ? "" : `${fmtStamp(sec, ivOf(cell))} UTC`; },
        priceFormatter: (p) => fmtPrice(cell, p),
      };
      t.timeScale.tickMarkFormatter = (x, type) => tickMark(cell.real, x, type);
      cell.chart = LW.createChart(cell.el.querySelector(".pc-chart"), Object.assign({ autoSize: true }, t));
      cell.chart.subscribeCrosshairMove((p) => onCrosshair(cell, p));
      cell.chart.timeScale().subscribeVisibleLogicalRangeChange((r) => { if (r && r.from < 12 && !replayOn(cell)) loadOlder(cell); });
    }
    function msg(cell, text) { const m = cell.el.querySelector(".pc-msg"); m.textContent = text; m.hidden = false; }
    function hideMsg(cell) { cell.el.querySelector(".pc-msg").hidden = true; }
    function paintHead(cell) {
      const c = cfgOf(cell), iv = ivOf(cell), el = cell.el;
      const sym = el.querySelector(".pc-sym");
      sym.querySelector("b").textContent = symLabel(c.sym, c.symB, c.op);
      F.setIcon(sym.querySelector("img"), c.sym);
      const ivb = el.querySelector('[data-act="iv"]');
      ivb.textContent = iv.short; ivb.title = `Interval: ${iv.long}`;
      const tb = el.querySelector('[data-act="type"]');
      tb.textContent = TYPE_SHORT[c.type] || TYPE_NAME[c.type]; tb.title = `Chart type: ${TYPE_NAME[c.type]}`;
      el.querySelector('[data-act="spread"]').setAttribute("aria-pressed", c.symB ? "true" : "false");
      paintQuote(cell);
    }
    function paintQuote(cell) {
      const q = cell.el.querySelector(".pc-q"), c = cfgOf(cell);
      const last = cell.view.length ? cell.view[cell.view.length - 1].close : null;
      const price = c.symB ? last : (cell.price != null ? cell.price : last);
      q.querySelector("b").textContent = price == null ? "" : fmtPrice(cell, price);
      const ch = c.symB ? null : cell.chg, s = q.querySelector("small");
      s.textContent = ch == null ? "" : F.pct(ch);
      s.className = "tnum " + (ch == null ? "" : ch >= 0 ? "c-long" : "c-short");
      cell.el.classList.toggle("is-paused", !!cell.paused);
    }

    // ------------------------------------------------------------ data
    const getBars = (sym, iv, limit, before) =>
      F.getJSON(`/api/premium/bars?coin=${enc(sym)}&interval=${enc(iv)}&limit=${limit}${before ? `&before=${before}` : ""}`);
    async function loadCell(cell) {
      const c = cfgOf(cell), iv = ivOf(cell), seq = ++cell.seq;
      clearTimeout(cell.retry);
      if (cell.unsubTrades) { cell.unsubTrades(); cell.unsubTrades = null; }
      Object.assign(cell, { A: [], B: [], tb: null, more: true, nextStart: null, view: [], items: [], real: null, price: null, chg: null });
      paintHead(cell);
      msg(cell, "Loading…");
      if (cell.main) cell.main.setData([]);
      if (cell.vol) cell.vol.setData([]);
      if (fromTrades(iv)) {
        if (F.isForex(c.sym) || (c.symB && F.isForex(c.symB))) { msg(cell, "Second and tick charts are built from the exchange's trades, so they work for crypto coins only."); return; }
        if (c.symB) { msg(cell, "Spread charts need a time interval of 1 minute or more."); return; }
        cell.unsubTrades = Trades.sub(c.sym, {
          init(rows) { if (seq !== cell.seq) return; cell.tb = new P.TradeBars(ivOf(cell)); cell.tb.add(rows); if (!cell.tb.bars.length) { msg(cell, "Waiting for trades…"); return; } render(cell, { fit: true }); },
          rows(fresh) { if (seq !== cell.seq || !cell.tb || cell.paused) return; if (cell.tb.add(fresh)) liveRender(cell); cell.price = fresh[fresh.length - 1][1]; paintQuote(cell); },
          error(text) { if (seq === cell.seq) msg(cell, text); },
        });
        refreshWant();
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
        render(cell, { fit: true });
      } catch (e) {
        if (seq !== cell.seq || e.message === "login") return;
        msg(cell, `${e.message && e.message.length < 160 ? e.message : "Data is not available right now."} Trying again…`);
        cell.retry = setTimeout(() => { if (seq === cell.seq) loadCell(cell); }, 8000);
      }
      refreshWant();
    }
    function merge(bars, fresh) {
      if (!fresh || !fresh.length) return bars;
      const first = fresh[0].time;
      let i = bars.length;
      while (i > 0 && bars[i - 1].time >= first) i--;
      return bars.slice(0, i).concat(fresh);
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

    // ------------------------------------------------------------ drawing a cell
    const replayOn = (cell) => G.replay != null && !!cell;
    function baseBars(cell) {
      const c = cfgOf(cell), iv = ivOf(cell);
      let base;
      if (fromTrades(iv)) base = cell.tb ? cell.tb.bars.map((b) => ({ time: b.t, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume, count: b.count })) : [];
      else base = c.symB ? P.spreadBars(cell.A, cell.B, c.op) : cell.A;
      if (G.replay != null) { const t = G.replay; base = base.filter((b) => b.time <= t); }
      return base;
    }
    function seriesKey(type) { return { bars: "bar", line: "line", area: "area", kagi: "kagi", pnf: "pnf", hollow: "hollow" }[type] || "candle"; }
    function hasVolume(cell) { const c = cfgOf(cell); return !c.symB && !["kagi", "pnf", "linebreak", "line", "area"].includes(c.type); }
    function ensureSeries(cell) {
      const LW = LWlib(), c = cfgOf(cell), withVol = c.volume && hasVolume(cell), key = seriesKey(c.type) + (withVol ? "+v" : "");
      if (cell.kind === key || !cell.chart) return;
      if (cell.main) { cell.chart.removeSeries(cell.main); cell.main = null; cell.prim = null; }
      if (cell.vol) { cell.chart.removeSeries(cell.vol); cell.vol = null; }
      const up = css("--long"), down = css("--short"), acc = css("--accent"), k = seriesKey(c.type);
      let s;
      if (k === "bar") s = cell.chart.addSeries(LW.BarSeries, { upColor: up, downColor: down, thinBars: false });
      else if (k === "line") s = cell.chart.addSeries(LW.LineSeries, { color: acc, lineWidth: 2 });
      else if (k === "area") s = cell.chart.addSeries(LW.AreaSeries, { lineColor: acc, topColor: alpha(acc, 0.28), bottomColor: alpha(acc, 0.02), lineWidth: 2 });
      else if (k === "kagi") s = cell.chart.addSeries(LW.LineSeries, { color: up, lineWidth: 2, lineType: LW.LineType ? LW.LineType.WithSteps : 1 });
      else if (k === "pnf") {
        const clear = "rgba(0,0,0,0)";
        s = cell.chart.addSeries(LW.CandlestickSeries, { upColor: clear, downColor: clear, borderVisible: false, wickVisible: false, lastValueVisible: false, priceLineVisible: false });
        cell.prim = new PnFPrimitive(() => ({ chart: cell.chart, series: cell.main, pnf: cell.pnf }));
        if (s.attachPrimitive) s.attachPrimitive(cell.prim);
      } else if (k === "hollow") s = cell.chart.addSeries(LW.CandlestickSeries, { upColor: "rgba(0,0,0,0)", downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
      else s = cell.chart.addSeries(LW.CandlestickSeries, { upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down });
      cell.main = s;
      if (withVol) {
        cell.vol = cell.chart.addSeries(LW.HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
        cell.chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
      }
      cell.chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.14, bottom: withVol ? 0.2 : 0.06 } });
      cell.kind = key;
    }
    function render(cell, opts) {
      opts = opts || {};
      if (!cell.chart) return;
      const c = cfgOf(cell), iv = ivOf(cell), type = c.type;
      const base = baseBars(cell);
      ensureSeries(cell);
      const d = derive(base, type, c);
      cell.pnf = d.pnf;
      const timeBased = iv.kind !== "tick" && !PRICE_TYPES.has(type);
      let data;
      if (!timeBased) {
        const ix = P.indexed(d.items.map((x) => (x.t === undefined ? Object.assign({}, x, { t: x.time }) : x)));
        data = ix.data;
        cell.real = ix.real;
      } else { data = d.items; cell.real = null; }
      cell.timeBased = timeBased;
      cell.items = d.items;
      cell.view = data;
      cell.extra = d.extra;
      const k = seriesKey(type), up = css("--long"), down = css("--short");
      let sd;
      if (k === "line" || k === "area") sd = data.map((b) => ({ time: b.time, value: b.close }));
      else if (k === "kagi") sd = data.map((b) => ({ time: b.time, value: b.close, color: b.yang ? up : down }));
      else if (k === "pnf") sd = data.map((b) => ({ time: b.time, open: b.open, close: b.close, high: b.high + cell.pnf.box / 2, low: b.low - cell.pnf.box / 2 }));
      else sd = data.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close }));
      cell.chart.applyOptions({ timeScale: { secondsVisible: iv.kind === "second" } });
      const last = data.length ? data[data.length - 1].close : 1, dec = decimalsFor(cell, last);
      cell.main.applyOptions({ priceFormat: { type: "custom", minMove: Math.pow(10, -dec), formatter: (p) => fmtPrice(cell, p) } });
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
      if (G.replay != null) cell.chart.timeScale().scrollToRealTime();
      cell.lastRender = Date.now();
      if (!data.length) msg(cell, PRICE_TYPES.has(type) ? "Not enough price movement for this box size yet. Try a smaller box or a longer history." : "No bars yet.");
      else if (d.capped) msg(cell, "This box size is too small for the history: only the first 20,000 items are shown. Choose a bigger size.");
      else hideMsg(cell);
      legend(cell, null);
      paintQuote(cell);
    }
    function liveRender(cell) {
      if (G.replay != null || !cell.main) return;
      const c = cfgOf(cell), iv = ivOf(cell), k = seriesKey(c.type);
      const plain = !fromTrades(iv) && !c.symB && ["candle", "hollow", "bar", "line", "area"].includes(k) && c.type !== "heikin";
      if (plain && cell.A.length && cell.view.length && cell.A[cell.A.length - 1].time >= cell.view[cell.view.length - 1].time) {
        const b = cell.A[cell.A.length - 1];
        const point = k === "line" || k === "area" ? { time: b.time, value: b.close } : { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close };
        try {
          cell.main.update(point);
          if (cell.vol) cell.vol.update({ time: b.time, value: b.volume || 0, color: alpha(b.close >= b.open ? css("--long") : css("--short"), 0.35) });
          if (b.time > cell.view[cell.view.length - 1].time) cell.view.push(b); else cell.view[cell.view.length - 1] = b;
          legend(cell, cell.hover);
          paintQuote(cell);
          return;
        } catch (e) { /* fall back to a full redraw */ }
      }
      if (cell.renderTimer) return;
      cell.renderTimer = setTimeout(() => { cell.renderTimer = null; render(cell); }, Math.max(0, 500 - (Date.now() - cell.lastRender)));
    }
    function legend(cell, p) {
      const el = cell.el.querySelector(".pc-legend"), c = cfgOf(cell), iv = ivOf(cell);
      cell.hover = p;
      let idx = -1;
      if (p && p.time != null && cell.view.length) idx = cell.real ? Math.round(p.time) - 1 : findIndex(cell.view, p.time);
      if (idx < 0 || idx >= cell.view.length) idx = cell.view.length - 1;
      const item = idx >= 0 ? cell.view[idx] : null;
      const head = `<b>${esc(symLabel(c.sym, c.symB, c.op))}</b> · ${esc(iv.short)} · ${esc(TYPE_NAME[c.type])}${cell.extra ? ` · ${esc(cell.extra)}` : ""}${cell.paused ? ' · <span class="c-short">paused</span>' : ""}`;
      if (!item) { el.innerHTML = head; return; }
      const f = (v) => fmtPrice(cell, v), cls = item.close >= item.open ? "c-long" : "c-short";
      let body;
      if (c.type === "kagi") body = `<span class="${item.yang ? "c-long" : "c-short"}">${item.yang ? "Yang" : "Yin"}</span> ${f(item.close)}`;
      else if (c.type === "pnf") body = `<span class="${item.x ? "c-long" : "c-short"}">${item.x ? "X" : "O"} column</span> ${f(item.low)} – ${f(item.high)} (${item.boxes} boxes)`;
      else if (c.type === "line" || c.type === "area") body = `<span class="${cls}">${f(item.close)}</span>`;
      else if (cell.el.clientWidth < 430) body = `C <span class="${cls}">${f(item.close)}</span>`;
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
    function indexAt(view, real, sec) {                     // last item that started at or before `sec`
      const t = (i) => (real ? real[i] : view[i].time);
      let lo = 0, hi = view.length - 1, ans = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (t(mid) <= sec) { ans = mid; lo = mid + 1; } else hi = mid - 1;
      }
      return ans;
    }

    // ------------------------------------------------------------ crosshair follows the same moment on every chart
    function syncTo(sec, except) {
      for (const o of G.cells) {
        if (o === except || !o.chart || !o.main || !o.view.length) continue;
        const k = sec == null ? -1 : indexAt(o.view, o.real, sec);
        if (k < 0) { o.chart.clearCrosshairPosition(); legend(o, null); continue; }
        const b = o.view[k];
        o.chart.setCrosshairPosition(b.close, b.time, o.main);
        legend(o, { time: b.time });                 // the library does not report programmatic moves
      }
    }
    function onCrosshair(cell, p) {
      legend(cell, p && p.time != null ? p : null);
      if (!p || !p.sourceEvent || !G.sync.crosshair) return;          // only real mouse moves are passed on
      const sec = p.time == null ? null : (cell.real ? cell.real[Math.round(p.time) - 1] : p.time);
      syncTo(sec, cell);
      if (G.main && G.main.crosshairAt) G.main.crosshairAt(sec);
    }
    function fromMain(p) {
      if (!G.cells.length || !G.sync.crosshair || !p || !p.sourceEvent) return;
      syncTo(p.time == null ? null : G.main.timeAt(p.time), null);
    }

    // ------------------------------------------------------------ the cell header: coin, interval, type, spread, export
    function onHead(cell, e) {
      const b = e.target.closest("[data-act]");
      if (!b) return;
      const act = b.dataset.act;
      if (act === "sym") { G.pickCell = cell; G.picker.open(b); }
      else if (act === "iv") openIv(cell, b);
      else if (act === "type") openType(cell, b);
      else if (act === "spread") {
        const c = cfgOf(cell);
        Spread.open({ a: c.sym, b: c.symB, op: c.op, active: !!c.symB,
          onApply: ({ a, b: bb, op }) => setCfg(cell, Object.assign({ sym: a, symB: bb, op }, fromTrades(ivOf(cell)) ? { iv: "1h" } : {}), {}),
          onRemove: () => setCfg(cell, { symB: null }, {}) });
      } else if (act === "export") exportCells([cell]);
    }
    function setCfg(cell, patch, opts) {
      opts = opts || {};
      for (const t of opts.all ? G.cells : [cell]) {
        const c = cfgOf(t);
        if (opts.all && t !== cell && c.symB && "sym" in patch) continue;      // spread charts keep their pair
        G.cfgs[t.k] = cleanCfg(Object.assign(c, patch), t.k);
        paintHead(t);
        if (opts.reload === false) render(t); else loadCell(t);
      }
      save();
      refreshWant();
      if (opts.all && G.main && !opts.fromMain) {                // linked charts move the main chart too
        if ("sym" in patch && G.main.setCoin) G.main.setCoin(patch.sym);
        if ("iv" in patch && G.main.setTf) G.main.setTf(patch.iv);
      }
    }
    function openIv(cell, anchor) {
      const c = cfgOf(cell), iv = ivOf(cell);
      openFloat(anchor, "Interval", intervalsHTML(iv, { tradesOk: !F.isForex(c.sym) && !c.symB }), (el) => {
        el.addEventListener("click", (e) => {
          const b = e.target.closest("[data-iv]");
          if (b && !b.disabled) { closeFloat(); setCfg(cell, { iv: b.dataset.iv }, { all: G.sync.interval }); }
        });
        el.querySelector("[data-iv-form]").addEventListener("submit", (e) => {
          e.preventDefault();
          const v = intervalFromForm(e.target);
          if (!v) { F.toast("That interval is outside the allowed range."); return; }
          if (fromTrades(v) && (F.isForex(c.sym) || c.symB)) { F.toast("Second and tick charts are for single crypto coins."); return; }
          closeFloat();
          setCfg(cell, { iv: v.name }, { all: G.sync.interval });
        });
      });
    }
    function openType(cell, anchor) {
      const paint = (el) => {
        const c = cfgOf(cell);
        el.querySelector("[data-types]").innerHTML = typesHTML(c.type);
        const auto = cell.items && cell.items.length ? autoBox(baseBars(cell)) : null;
        const set = el.querySelector("[data-set-box]");
        set.innerHTML = typeSettingsHTML(c.type, c, auto, ivOf(cell).long, hasVolume(cell) ? c.volume : null);
        set.hidden = !set.innerHTML.trim();
      };
      openFloat(anchor, "Chart type", `<div data-types></div><div class="pp-type-set" data-set-box></div>`, (el) => {
        paint(el);
        el.addEventListener("click", (e) => {
          const b = e.target.closest("[data-type]");
          if (!b) return;
          const c = cfgOf(cell), wasPrice = PRICE_TYPES.has(c.type), nowPrice = PRICE_TYPES.has(b.dataset.type);
          G.cfgs[cell.k] = cleanCfg(Object.assign(c, { type: b.dataset.type }), cell.k);
          paintHead(cell);
          save();
          if (wasPrice !== nowPrice && !fromTrades(ivOf(cell))) loadCell(cell); else render(cell, { fit: true });
          paint(el);
        });
        el.addEventListener("change", (e) => {
          const inp = e.target.closest("[data-set]");
          if (!inp) return;
          const c = cfgOf(cell), key = inp.dataset.set;
          if (key === "volume") c.volume = inp.checked; else c[key] = inp.value === "" ? null : Number(inp.value);
          G.cfgs[cell.k] = cleanCfg(c, cell.k);
          save();
          render(cell);
        });
      });
    }

    // ------------------------------------------------------------ live prices (shared feed)
    function refreshWant() {
      const syms = new Set();
      for (const cell of G.cells) {
        const c = cfgOf(cell);
        if (fromTrades(ivOf(cell))) continue;
        syms.add(c.sym);
        if (c.symB) syms.add(c.symB);
      }
      Hub.want([...syms], G.cells.length + 1);              // + the main chart, which polls its own candles
      paintLive();
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
      G.totals = { charts: m.charts || G.cells.length + 1, tabs: m.tabs || 1 };
      G.allow = m.allow ? m.allow[Hub.tab] : undefined;
      G.liveOk = !!m.ok;
      const now = serverNow();
      G.cells.forEach((cell, k) => {
        const was = cell.paused;
        cell.paused = G.allow !== undefined && k + 1 >= G.allow;      // the main chart is chart number one
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
    function liveText() {
      const n = G.cells.length + 1, paused = G.cells.filter((c) => c.paused).length, tf = Trades.count();
      const total = Math.max(G.totals.charts || n, n);
      return `${total} live chart${total === 1 ? "" : "s"} in ${G.totals.tabs} tab${G.totals.tabs === 1 ? "" : "s"} share one price connection (limit ${MAX_LIVE}).`
        + (tf ? ` ${tf} coin${tf === 1 ? "" : "s"} also stream${tf === 1 ? "s" : ""} trades for second and tick charts.` : "")
        + (paused ? ` ${paused} chart${paused === 1 ? " is" : "s are"} paused because ${MAX_LIVE} live charts are open in your other tabs.` : "");
    }
    function paintLive() { const el = $("grid-live"); if (el) el.textContent = liveText(); }

    // ------------------------------------------------------------ export, replay, coins, main-chart changes
    function rowsOf(cell) {
      const c = cfgOf(cell), iv = ivOf(cell), when = (i) => P.isoUTC(cell.real ? cell.real[i] : cell.view[i].time);
      const num = (v) => (v == null || isNaN(v) ? "" : +Number(v).toPrecision(12));
      const idx = cell.view.map((_, i) => i);
      if (c.type === "kagi") return { header: ["time_utc", "price", "line"], rows: idx.map((i) => [when(i), num(cell.view[i].close), cell.view[i].yang ? "yang" : "yin"]) };
      if (c.type === "pnf") return { header: ["time_utc", "column", "low", "high", "boxes"], rows: idx.map((i) => { const b = cell.view[i]; return [when(i), b.x ? "X" : "O", num(b.low), num(b.high), b.boxes]; }) };
      const trades = iv.kind === "tick" || iv.kind === "second";
      const header = ["time_utc", "open", "high", "low", "close"].concat(c.symB ? [] : ["volume"]).concat(trades ? ["trades"] : []);
      return { header, rows: idx.map((i) => { const b = cell.view[i], r = [when(i), num(b.open), num(b.high), num(b.low), num(b.close)]; if (!c.symB) r.push(num(b.volume)); if (trades) r.push(b.count || ""); return r; }) };
    }
    function exportCells(cells) {
      let n = 0;
      const day = new Date().toISOString().slice(0, 10);
      cells.forEach((cell, k) => {
        if (!cell.view.length) return;
        const c = cfgOf(cell), { header, rows } = rowsOf(cell);
        setTimeout(() => csv(`${fileSym(c.sym, c.symB, c.op)}_${ivOf(cell).name}_${c.type}_${day}.csv`, header, rows), (k + 1) * 350);
        n++;
      });
      return n;
    }
    function setReplay(t) { G.replay = t; G.cells.forEach((c) => render(c)); }
    function mainChanged(what) {                             // the main chart's coin / interval changed (sync)
      if (!G.cells.length) return;
      if (what.coin && G.sync.symbol) setCfg(G.cells[0], { sym: what.coin }, { all: true, fromMain: true });
      if (what.tf && G.sync.interval) G.cells.forEach((cell) => { G.cfgs[cell.k] = cleanCfg(Object.assign(cfgOf(cell), { iv: what.tf }), cell.k); paintHead(cell); loadCell(cell); });
      save();
    }
    function setSync(key, on) { G.sync[key] = !!on; save(); }
    function retheme() {
      G.cells.forEach((cell) => { if (!cell.chart) return; cell.chart.applyOptions(theme()); cell.kind = null; render(cell); });
    }
    document.addEventListener("themechange", retheme);

    return {
      init, setLayout, fromMain, setReplay, exportCells, mainChanged, setSync, liveText, paintLive,
      layout: () => G.layout, cells: () => G.cells, sync: () => G.sync, count: () => G.cells.length + 1,
      coins: () => G.coins, setCoins(c) { G.coins = c; if (G.picker) G.picker.setCoins(c); },
      set onChange(fn) { G.onChange = fn; },
    };
  })();

  // ---------------------------------------------------------------- shared tick-mark formatter for charts without a time axis
  function tickMark(real, t, type) {
    if (!real) return null;
    const sec = real[Math.round(t) - 1];
    if (sec == null) return "";
    const d = new Date(sec * 1000);
    if (type <= 1) return `${d.getUTCFullYear()}`;
    if (type === 2) return `${pad(d.getUTCDate())} ${d.toLocaleString("en-US", { month: "short", timeZone: "UTC" })}`;
    return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  }

  // ================================================================== bond screener panel
  const Bonds = (() => {
    const B = { data: null, cc: "", mat: "", q: "", sort: "years", dir: 1, sel: null, loading: false, chart: null, series: null };
    const CC_COLOR = { US: "#60a5fa", EU: "#facc15", CA: "#f87171", JP: "#f472b6", AU: "#34d399" };
    let panel = null, opener = null;
    function el() {
      if (panel) return panel;
      panel = document.createElement("aside");
      panel.className = "pb-panel";
      panel.id = "pb-panel";
      panel.hidden = true;
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-label", "Bond screener");
      panel.innerHTML = `
        <div class="pb-head">
          <h2 class="mk-h2">Bond screener</h2><span class="pa-free">Free now</span>
          <button type="button" class="mk-x" data-close aria-label="Close">&times;</button>
        </div>
        <p class="mk-note pb-asof" data-asof></p>
        <div class="pb-filters">
          <div class="pb-chips" data-countries role="group" aria-label="Countries"></div>
          <div class="mk-seg mk-seg-sm" data-mat role="group" aria-label="Maturity">
            <button type="button" data-m="" aria-pressed="true">All</button><button type="button" data-m="short">Up to 2Y</button>
            <button type="button" data-m="medium">2Y–10Y</button><button type="button" data-m="long">Over 10Y</button>
          </div>
          <input type="search" class="mk-input pb-q" data-q placeholder="Search bonds" aria-label="Search bonds" autocomplete="off">
        </div>
        <div class="pb-table-wrap">
          <table class="pb-table">
            <thead><tr>
              <th scope="col"><button type="button" data-sort="name">Bond</button></th>
              <th scope="col" class="num"><button type="button" data-sort="yield">Yield</button></th>
              <th scope="col" class="num"><button type="button" data-sort="chg_1d">1D</button></th>
              <th scope="col" class="num"><button type="button" data-sort="chg_1w">1W</button></th>
              <th scope="col" class="num"><button type="button" data-sort="chg_1m">1M</button></th>
              <th scope="col" class="num"><button type="button" data-sort="chg_1y">1Y</button></th>
              <th scope="col">52 weeks</th>
              <th scope="col">3 months</th>
            </tr></thead>
            <tbody data-rows><tr><td colspan="8" class="pb-empty">Loading bond yields…</td></tr></tbody>
          </table>
        </div>
        <div class="pb-side">
          <div><h3 class="pb-h3">Yield curves</h3><div class="pb-curve" data-curve></div></div>
          <div><h3 class="pb-h3" data-hist-title>History</h3><div class="pb-hist" data-hist></div></div>
        </div>
        <p class="mk-note" data-note></p>
        <div class="pb-sources" data-sources></div>`;
      document.body.appendChild(panel);
      panel.addEventListener("click", (e) => {
        if (e.target.closest("[data-close]")) { close(); return; }
        const cc = e.target.closest("[data-cc]");
        if (cc) { B.cc = cc.dataset.cc; paintFilters(); paintRows(); paintCurve(); return; }
        const m = e.target.closest("[data-m]");
        if (m) { B.mat = m.dataset.m; panel.querySelectorAll("[data-m]").forEach((x) => x.setAttribute("aria-pressed", x === m ? "true" : "false")); paintRows(); return; }
        const s = e.target.closest("[data-sort]");
        if (s && B.data) { B.dir = B.sort === s.dataset.sort ? -B.dir : (s.dataset.sort === "name" ? 1 : -1); B.sort = s.dataset.sort; paintRows(); return; }
        const tr = e.target.closest("tr[data-id]");
        if (tr) select(tr.dataset.id);
      });
      panel.querySelector("[data-q]").addEventListener("input", (e) => { B.q = e.target.value; if (B.data) paintRows(); });
      panel.querySelector("[data-rows]").addEventListener("keydown", (e) => {
        const tr = e.target.closest("tr[data-id]");
        if (tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); select(tr.dataset.id); }
      });
      document.addEventListener("keydown", (e) => { if (e.key === "Escape" && panel && !panel.hidden) { e.stopPropagation(); close(); } }, true);
      document.addEventListener("themechange", () => {
        if (B.chart) { B.chart.applyOptions(chartTheme()); B.series.applyOptions({ lineColor: css("--accent"), topColor: alpha(css("--accent"), 0.25), bottomColor: alpha(css("--accent"), 0.02) }); }
        if (B.data) paintCurve();
      });
      return panel;
    }
    const q = (sel) => el().querySelector(sel);
    function open(from) {
      opener = from || document.activeElement;
      el().hidden = false;
      document.body.classList.add("pb-open");
      if (!B.data && !B.loading) load();
      setTimeout(() => { const x = q("[data-close]"); if (x) x.focus({ preventScroll: true }); }, 30);
    }
    function close() {
      if (!panel || panel.hidden) return;
      panel.hidden = true;
      document.body.classList.remove("pb-open");
      if (opener && opener.focus) opener.focus({ preventScroll: true });
    }
    async function load() {
      B.loading = true;
      q("[data-rows]").innerHTML = '<tr><td colspan="8" class="pb-empty">Loading bond yields from the official sources…</td></tr>';
      try {
        const d = await F.getJSON("/api/premium/bonds");
        if (!d || !d.bonds || !d.bonds.length) throw new Error(d && d.error ? d.error : "Bond data is not available right now.");
        B.data = d;
        paintFilters(); paintRows(); paintCurve(); paintSources();
        q("[data-note]").textContent = d.note || "";
        select(B.sel || (d.bonds.find((b) => b.id === "US10Y") || d.bonds[0]).id);
      } catch (e) {
        if (e.message === "login") return;
        q("[data-rows]").innerHTML = `<tr><td colspan="8" class="pb-empty">${esc(e.message || "Bond data is not available right now.")} <button type="button" class="mk-link" data-retry>Try again</button></td></tr>`;
        const r = q("[data-retry]");
        if (r) r.addEventListener("click", load);
      } finally { B.loading = false; }
    }
    function paintFilters() {
      const list = [["", "All"]].concat(B.data.sources.filter((s) => s.ok).map((s) => [s.cc, s.country]));
      q("[data-countries]").innerHTML = list.map(([cc, name]) => `<button type="button" class="mk-chip" data-cc="${cc}" aria-pressed="${B.cc === cc}">${cc ? `<i style="background:${CC_COLOR[cc] || "var(--faint)"}"></i>` : ""}${esc(name)}</button>`).join("");
      const newest = B.data.sources.map((s) => s.as_of).filter(Boolean).sort().pop();
      q("[data-asof]").textContent = newest ? `End-of-day government bond yields, latest ${newest}. Changes in basis points (1 bp = 0.01%).` : "";
    }
    function rows() {
      const s = B.q.trim().toLowerCase();
      const list = B.data.bonds.filter((b) => (!B.cc || b.cc === B.cc) && (!B.mat || b.bucket === B.mat) && (!s || `${b.name} ${b.country} ${b.tenor} ${b.cc}`.toLowerCase().includes(s)));
      const k = B.sort, dir = B.dir;
      return list.slice().sort((a, b) => {
        if (k === "name") return dir * String(a.country).localeCompare(String(b.country)) || dir * (a.years - b.years);
        const x = a[k], y = b[k];
        if (x == null && y == null) return 0;
        if (x == null) return 1;
        if (y == null) return -1;
        return dir * (x - y) || a.cc.localeCompare(b.cc);
      });
    }
    const bp = (v) => (v == null ? '<td class="num tnum c-faint">--</td>' : `<td class="num tnum ${v > 0 ? "c-long" : v < 0 ? "c-short" : ""}">${F.signed(v, 1)}</td>`);
    function spark(vals) {
      if (!vals || vals.length < 2) return "";
      const lo = Math.min(...vals), hi = Math.max(...vals), w = 88, h = 24, span = hi - lo || 1;
      const pts = vals.map((v, i) => `${((i / (vals.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - lo) / span) * (h - 4)).toFixed(1)}`).join(" ");
      const col = vals[vals.length - 1] >= vals[0] ? "var(--long)" : "var(--short)";
      return `<svg class="pb-spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
    }
    function range52(b) {
      if (b.lo_52w == null || b.hi_52w == null) return "<td></td>";
      const at = Math.max(0, Math.min(100, ((b.yield - b.lo_52w) / (b.hi_52w - b.lo_52w || 1)) * 100));
      return `<td><span class="pb-range" title="52-week low ${b.lo_52w}% · high ${b.hi_52w}%"><small class="tnum">${b.lo_52w.toFixed(2)}</small><span class="pb-range-bar"><i style="left:${at.toFixed(1)}%"></i></span><small class="tnum">${b.hi_52w.toFixed(2)}</small></span></td>`;
    }
    function paintRows() {
      el().querySelectorAll(".pb-table th button").forEach((b) => b.parentElement.setAttribute("aria-sort", b.dataset.sort === B.sort ? (B.dir > 0 ? "ascending" : "descending") : "none"));
      const list = rows();
      q("[data-rows]").innerHTML = list.length ? list.map((b) => `<tr data-id="${esc(b.id)}" tabindex="0" aria-selected="${B.sel === b.id}">
          <td><span class="pb-name"><i style="background:${CC_COLOR[b.cc] || "var(--faint)"}"></i><span><b>${esc(b.name)}</b><small>${esc(b.country)} · ${esc(b.date)}</small></span></span></td>
          <td class="num tnum"><b>${b.yield.toFixed(3)}%</b></td>${bp(b.chg_1d)}${bp(b.chg_1w)}${bp(b.chg_1m)}${bp(b.chg_1y)}${range52(b)}<td>${spark(b.spark)}</td></tr>`).join("")
        : '<tr><td colspan="8" class="pb-empty">No bond matches these filters.</td></tr>';
    }
    function paintSources() {
      q("[data-sources]").innerHTML = "Sources: " + B.data.sources.map((s) => `<a href="${esc(s.link)}" target="_blank" rel="noopener">${esc(s.name)}</a>${s.ok ? ` <small>(${esc(s.as_of || "")})</small>` : ' <small class="c-short">(not available right now)</small>'}`).join(" · ");
    }
    function paintCurve() {
      const box = q("[data-curve]"), curves = B.data.curves || {};
      const ccs = Object.keys(curves).filter((cc) => !B.cc || cc === B.cc), all = ccs.flatMap((cc) => curves[cc]);
      if (!all.length) { box.innerHTML = '<p class="mk-empty">No curve to show.</p>'; return; }
      const W0 = 520, H0 = 230, L = 40, R = 12, T = 12, Bm = 28;
      const lx = (y) => Math.log(y + 0.25), x0 = lx(Math.min(...all.map((p) => p[0]))), x1 = lx(Math.max(...all.map((p) => p[0])));
      let y0 = Math.min(...all.map((p) => p[1])), y1 = Math.max(...all.map((p) => p[1]));
      const padY = (y1 - y0) * 0.12 || 0.5; y0 -= padY; y1 += padY;
      const X = (y) => L + ((lx(y) - x0) / (x1 - x0 || 1)) * (W0 - L - R), Y = (v) => T + (1 - (v - y0) / (y1 - y0)) * (H0 - T - Bm);
      const ticksX = [0.25, 1, 2, 5, 10, 30].filter((t) => lx(t) >= x0 - 0.01 && lx(t) <= x1 + 0.01);
      const stepY = P.niceStep((y1 - y0) / 4) || 0.5, ticksY = [];
      for (let v = Math.ceil(y0 / stepY) * stepY; v <= y1; v += stepY) ticksY.push(+v.toFixed(4));
      const grid = ticksY.map((v) => `<line x1="${L}" x2="${W0 - R}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" class="pb-grid"/><text x="${L - 6}" y="${(Y(v) + 3.5).toFixed(1)}" text-anchor="end">${v.toFixed(stepY < 0.1 ? 2 : 1)}%</text>`).join("")
        + ticksX.map((t) => `<text x="${X(t).toFixed(1)}" y="${H0 - 8}" text-anchor="middle">${t < 1 ? `${Math.round(t * 12)}M` : `${t}Y`}</text>`).join("");
      const lines = ccs.map((cc) => {
        const pts = curves[cc].slice().sort((a, b) => a[0] - b[0]), col = CC_COLOR[cc] || "var(--accent)";
        return `<polyline points="${pts.map((p) => `${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ")}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round"/>`
          + pts.map((p) => `<circle cx="${X(p[0]).toFixed(1)}" cy="${Y(p[1]).toFixed(1)}" r="2.4" fill="${col}"><title>${cc} ${p[0] < 1 ? Math.round(p[0] * 12) + "M" : p[0] + "Y"}: ${p[1]}%</title></circle>`).join("");
      }).join("");
      const keys = ccs.map((cc) => `<span><i style="background:${CC_COLOR[cc] || "var(--accent)"}"></i>${esc((B.data.sources.find((s) => s.cc === cc) || {}).country || cc)}</span>`).join("");
      box.innerHTML = `<svg viewBox="0 0 ${W0} ${H0}" class="pb-curve-svg" role="img" aria-label="Government bond yield curves by maturity">${grid}${lines}</svg><div class="pb-keys">${keys}</div>`;
    }
    function chartTheme() {
      return {
        layout: { background: { type: "solid", color: "transparent" }, textColor: css("--dim"), fontFamily: "IBM Plex Mono, ui-monospace, monospace", fontSize: 11, attributionLogo: false },
        grid: { vertLines: { color: css("--line-soft") }, horzLines: { color: css("--line-soft") } },
        rightPriceScale: { borderColor: css("--line") }, timeScale: { borderColor: css("--line"), timeVisible: false },
      };
    }
    async function select(id) {
      B.sel = id;
      el().querySelectorAll("tr[data-id]").forEach((tr) => tr.setAttribute("aria-selected", tr.dataset.id === id ? "true" : "false"));
      const row = B.data.bonds.find((b) => b.id === id), box = q("[data-hist]");
      q("[data-hist-title]").textContent = row ? `${row.name}: daily yield` : "History";
      try {
        const d = await F.getJSON(`/api/premium/bonds/history?id=${enc(id)}`);
        if (B.sel !== id) return;
        const LW = LWlib();
        if (!d.ok || !LW) { box.innerHTML = `<p class="mk-empty">${esc(d.error || "No history.")}</p>`; return; }
        if (!B.chart) {
          box.innerHTML = "";
          B.chart = LW.createChart(box, Object.assign({ autoSize: true }, chartTheme(), { localization: { priceFormatter: (p) => `${p.toFixed(3)}%` } }));
          B.series = B.chart.addSeries(LW.AreaSeries, { lineColor: css("--accent"), topColor: alpha(css("--accent"), 0.25), bottomColor: alpha(css("--accent"), 0.02), lineWidth: 2 });
        }
        B.series.setData(d.points.map(([day, v]) => ({ time: day, value: v })));
        B.chart.timeScale().fitContent();
      } catch (e) { if (e.message !== "login") box.innerHTML = '<p class="mk-empty">History is not available right now.</p>'; }
    }
    return { open, close, toggle(from) { if (panel && !panel.hidden) close(); else open(from); }, isOpen: () => !!panel && !panel.hidden };
  })();

  window.SFMPremium = {
    TYPES, TYPE_NAME, TYPE_SHORT, PRICE_TYPES, OP_SIGN, LAYOUTS, MAX_LIVE,
    derive, autoBox, PnFPrimitive, intervalsHTML, intervalFromForm, typesHTML, typeSettingsHTML, tickMark, fmtStamp, symLabel, fileSym,
    Spread, Trades, Hub, Grid, Bonds, csv,
  };
})();
