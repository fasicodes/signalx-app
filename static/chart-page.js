/* Signals FM Live chart (templates/chart.html).
   Its own page with a coin switcher; it never runs the Pro terminal's analysis.
   Data: /coins, /candles, /api/market/tickers, /api/engine, /api/chart/drawings, /api/alerts, /api/watchlist, /api/signals/board.
   Chart: TradingView lightweight-charts 5 (chart, panes, series, markers); drawings and SVG indicators on overlays. */
(function () {
  "use strict";
  const F = window.SFM;
  const LW = window.LightweightCharts;
  const $ = (id) => document.getElementById(id);
  const enc = encodeURIComponent;
  const na = (v) => v === null || v === undefined || Number.isNaN(v);

  const TF_SEC = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800 };
  const TF_LABEL = { "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1D", "1w": "1W" };
  const RANGES = { "1D": ["5m", 288], "5D": ["30m", 240], "1M": ["4h", 186], "3M": ["1d", 92], "6M": ["1d", 183], "1Y": ["1d", 366], "5Y": ["1w", 262] };
  const TYPE_LABEL = { candles: "Candles", hollow: "Hollow", heikin: "Heikin Ashi", bars: "Bars", line: "Line", area: "Area" };
  const SETTINGS_KEY = "sfm-chart-set-v1", IND_KEY = "sfm-chart-ind-v1";
  const OLDER_PAGE = 500, MAX_BARS = 20000;   // endless history: 500 older candles per page, up to 20,000 in memory
  const phone = () => window.matchMedia("(max-width: 760px)").matches;

  const S = {
    coins: { crypto: [], forex: [] }, coin: "BTC/USDT", tf: "1h", limit: 300, range: null,
    bars: [], oldest: null, noMore: null, loadingOlder: false, seq: 0,
    chart: null, main: null, vol: null, type: "candles", watermark: null, markers: null,
    eng: null, engLines: [], alerts: [], alertLines: [], watch: new Set(), tickers: {}, board: null, boardAt: 0,
    set: { volume: true, levels: true, history: true, alerts: true, log: false, magnet: false, watermark: true },
    ind: {}, indSeries: {}, indMeta: {},
    hover: null, pickPrice: false, lastUpdate: 0, serverOffset: 0, live: false, err: null, pollTimer: null,
  };

  // ------------------------------------------------------------------ small helpers
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function alpha(hex, a) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim());
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
  const fmt = (v) => F.price(v, S.coin);
  function showMsg(text) { const m = $("ch-msg"); m.textContent = text; m.hidden = false; }
  function hideMsg() { $("ch-msg").hidden = true; }
  function toast(msg) { F.toast(msg); }
  function barIndex(time) {
    let lo = 0, hi = S.bars.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1, t = S.bars[mid].time;
      if (t === time) return mid;
      if (t < time) lo = mid + 1; else hi = mid - 1;
    }
    return -1;
  }
  function barAtOrBefore(ts) {            // last bar whose open time <= ts
    let lo = 0, hi = S.bars.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (S.bars[mid].time <= ts) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }
  const isoSec = (iso) => { if (!iso) return null; const d = new Date(/Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z"); return isNaN(d) ? null : Math.floor(d / 1000); };
  function countdown(sec) {
    sec = Math.max(0, Math.floor(sec));
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    const two = (x) => String(x).padStart(2, "0");
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}:${two(m)}:${two(s)}`;
    return `${m}:${two(s)}`;
  }

  // ------------------------------------------------------------------ settings + persistence
  function loadPrefs() {
    Object.assign(S.set, F.store.getJSON(SETTINGS_KEY, {}));
    const t = F.store.get("sfm-chart-type");
    if (t && TYPE_LABEL[t]) S.type = t;
    const ind = F.store.getJSON(IND_KEY, {});
    S.ind = ind && typeof ind === "object" ? ind : {};
  }
  const savePrefs = () => { F.store.setJSON(SETTINGS_KEY, S.set); F.store.set("sfm-chart-type", S.type); F.store.setJSON(IND_KEY, S.ind); };

  // ================================================================== chart setup
  function theme() {
    return {
      layout: { background: { type: "solid", color: "transparent" }, textColor: css("--dim"), fontFamily: "Inter, system-ui, sans-serif", fontSize: 11.5,
                attributionLogo: false, panes: { separatorColor: css("--line"), separatorHoverColor: alpha(css("--accent") || "#22c55e", 0.25), enableResize: true } },
      grid: { vertLines: { color: css("--line-soft") }, horzLines: { color: css("--line-soft") } },
      rightPriceScale: { borderColor: css("--line"), mode: S.set.log ? 1 : 0 },
      timeScale: { borderColor: css("--line"), timeVisible: true, secondsVisible: false, rightOffset: 6 },
      crosshair: { mode: S.set.magnet ? 1 : 0,
                   vertLine: { color: css("--faint"), labelBackgroundColor: css("--surface-2") },
                   horzLine: { color: css("--faint"), labelBackgroundColor: css("--surface-2") } },
    };
  }
  function priceFormat() {
    const last = S.bars.length ? S.bars[S.bars.length - 1].close : 1;
    const d = F.decimals(last, S.coin);
    const nf = new Intl.NumberFormat("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
    return { type: "custom", minMove: Math.pow(10, -d), formatter: (p) => nf.format(p) };
  }
  function makeMain() {
    const up = css("--long"), down = css("--short"), acc = css("--accent");
    let s;
    if (S.type === "bars") s = S.chart.addSeries(LW.BarSeries, { upColor: up, downColor: down, thinBars: false });
    else if (S.type === "line") s = S.chart.addSeries(LW.LineSeries, { color: acc, lineWidth: 2 });
    else if (S.type === "area") s = S.chart.addSeries(LW.AreaSeries, { lineColor: acc, topColor: alpha(acc, 0.28), bottomColor: alpha(acc, 0.02), lineWidth: 2 });
    else if (S.type === "hollow") s = S.chart.addSeries(LW.CandlestickSeries, { upColor: "rgba(0,0,0,0)", downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
    else s = S.chart.addSeries(LW.CandlestickSeries, { upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down });
    s.applyOptions({ priceFormat: priceFormat() });
    try { s.setSeriesOrder(1); } catch (e) { /* older library: order stays as created */ }
    return s;
  }
  function heikin(bars) {
    const out = new Array(bars.length);
    let po = null, pc = null;
    for (let i = 0; i < bars.length; i++) {
      const b = bars[i];
      const c = (b.open + b.high + b.low + b.close) / 4;
      const o = po === null ? (b.open + b.close) / 2 : (po + pc) / 2;
      out[i] = { time: b.time, open: o, high: Math.max(b.high, o, c), low: Math.min(b.low, o, c), close: c };
      po = o; pc = c;
    }
    return out;
  }
  function mainPoints() {
    if (S.type === "line" || S.type === "area") return S.bars.map((b) => ({ time: b.time, value: b.close }));
    const src = S.type === "heikin" ? heikin(S.bars) : S.bars;
    return src.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close }));
  }
  function volPoints() {
    const up = alpha(css("--long"), 0.42), down = alpha(css("--short"), 0.42);
    return S.bars.map((b) => ({ time: b.time, value: b.volume || 0, color: b.close >= b.open ? up : down }));
  }
  function setAllData() {
    if (!S.main) return;
    S.main.applyOptions({ priceFormat: priceFormat() });
    S.main.setData(mainPoints());
    S.vol.setData(S.set.volume ? volPoints() : []);
  }
  function ensureChart() {
    if (S.chart) return true;
    if (!LW) { showMsg("The chart library could not load. Check your connection and refresh the page."); return false; }
    S.chart = LW.createChart($("ch-chart"), { autoSize: true, ...theme() });
    S.vol = S.chart.addSeries(LW.HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    S.chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });
    S.main = makeMain();
    try {
      S.watermark = LW.createTextWatermark(S.chart.panes()[0], { horzAlign: "center", vertAlign: "center", lines: [] });
    } catch (e) { S.watermark = null; }
    S.chart.subscribeCrosshairMove(onCrosshair);
    S.chart.subscribeClick(onChartClick);
    S.chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
      renderDrawings(); renderIndicatorOverlay(); maybeLoadOlder(); updateLatestBtn();
    });
    new ResizeObserver(() => { renderDrawings(); renderIndicatorOverlay(); }).observe($("ch-chart"));
    document.addEventListener("themechange", () => {
      S.chart.applyOptions(theme());
      rebuildMain();
      refreshIndicators(true);
      setWatermark();
    });
    return true;
  }
  function rebuildMain() {
    if (!S.chart) return;
    if (S.markers) { try { S.markers.detach(); } catch (e) {} S.markers = null; }
    S.engLines = []; S.alertLines = [];
    try { S.chart.removeSeries(S.main); } catch (e) {}
    S.main = makeMain();
    if (S.bars.length) setAllData();
    drawEngine(); drawAlertLines(); renderDrawings();
  }
  function setWatermark() {
    if (!S.watermark) return;
    S.watermark.applyOptions({ visible: !!S.set.watermark, lines: [
      { text: `${S.coin} · ${TF_LABEL[S.tf] || S.tf}`, color: alpha(css("--text") || "#e8f1ec", 0.06), fontSize: phone() ? 26 : 44, fontStyle: "700" },
      { text: "Signals FM", color: alpha(css("--text") || "#e8f1ec", 0.045), fontSize: phone() ? 13 : 17 },
    ] });
  }
  function showRecent() {
    const n = S.bars.length;
    if (!n) return;
    const w = $("ch-chart").clientWidth || 600;
    const count = Math.max(40, Math.min(170, Math.round(w / 7.5)));
    S.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - count), to: n + 4 });
  }

  // ================================================================== candles: load, live updates, older history
  const norm = (c) => ({ time: c.time, open: +c.open, high: +c.high, low: +c.low, close: +c.close, volume: +c.volume || 0 });
  async function loadCandles() {
    if (!ensureChart()) return;
    const seq = ++S.seq, sym = S.coin, tf = S.tf;
    showMsg(`Loading ${sym} ${TF_LABEL[tf]}…`);
    setWatermark();
    let d;
    try { d = await F.getJSON(`/candles?coin=${enc(sym)}&timeframe=${tf}&limit=${S.limit}`); }
    catch (e) { if (seq === S.seq) { showMsg("The chart could not load. Check your connection; it will try again shortly."); setLive(false, "Offline, retrying…"); } return; }
    if (seq !== S.seq) return;
    if (d.error || !d.candles || !d.candles.length) {
      S.bars = []; S.main.setData([]); S.vol.setData([]);
      showMsg(d.error ? `Chart data for ${sym} is not available right now: ${d.error}` : `No ${TF_LABEL[tf]} candles for ${sym} yet.`);
      setLive(false, "No data");
      return;
    }
    S.bars = d.candles.map(norm);
    S.oldest = S.bars[0].time; S.noMore = null;
    if (d.server_time) S.serverOffset = d.server_time * 1000 - Date.now();
    setAllData();
    showRecent();
    hideMsg();
    refreshIndicators(true);
    drawEngine(); drawAlertLines();
    S.hover = null; legend(); snapshot(); updateHeader();
    S.lastUpdate = Date.now(); setLive(true);
    loadDrawings();
    schedulePoll();
  }
  function mergeBar(b) {
    const n = S.bars.length;
    if (!n) { S.bars.push(b); return; }
    const last = S.bars[n - 1];
    if (b.time === last.time) S.bars[n - 1] = b;
    else if (b.time > last.time) S.bars.push(b);
    else { const i = barIndex(b.time); if (i >= 0) S.bars[i] = b; }
  }
  async function poll() {
    if (!S.bars.length || document.hidden || !S.chart) return;
    const seq = S.seq, sym = S.coin, tf = S.tf;
    let d;
    try { d = await F.getJSON(`/candles?coin=${enc(sym)}&timeframe=${tf}&limit=2`); }
    catch (e) { setLive(false, "Connection lost, retrying…"); return; }
    if (seq !== S.seq) return;
    if (d.error || !d.candles || !d.candles.length) { setLive(false, d.error ? "The exchange did not answer, retrying…" : "Waiting for data…"); return; }
    if (d.server_time) S.serverOffset = d.server_time * 1000 - Date.now();
    const lastBefore = S.bars[S.bars.length - 1].time;
    d.candles.map(norm).forEach(mergeBar);
    const pts = mainPoints(), vols = volPoints();
    const from = Math.max(0, barAtOrBefore(Math.min(lastBefore, d.candles[0].time)));
    for (let i = from; i < S.bars.length; i++) {
      try { S.main.update(pts[i], i < S.bars.length - 1); } catch (e) { S.main.setData(pts); break; }
      if (S.set.volume) { try { S.vol.update(vols[i], i < S.bars.length - 1); } catch (e) { S.vol.setData(vols); } }
    }
    if (S.bars[S.bars.length - 1].time !== lastBefore) drawEngine();   // a new candle: markers may move
    refreshIndicators(false);
    if (S.hover == null) legend();
    updateHeader();
    S.lastUpdate = Date.now();
    setLive(true);
  }
  function schedulePoll() {
    clearInterval(S.pollTimer);
    S.pollTimer = setInterval(poll, F.isForex(S.coin) ? 30000 : (S.tf === "1m" ? 3000 : 5000));
  }
  async function maybeLoadOlder() {
    if (!S.chart || S.loadingOlder || !S.bars.length || S.oldest == null) return;
    const r = S.chart.timeScale().getVisibleLogicalRange();
    if (!r || r.from > 15) return;
    const key = `${S.coin}|${S.tf}`;
    if (S.noMore === key || S.bars.length >= MAX_BARS) return;
    S.loadingOlder = true;
    const seq = S.seq;
    try {
      const d = await F.getJSON(`/candles?coin=${enc(S.coin)}&timeframe=${S.tf}&limit=${OLDER_PAGE}&before=${S.oldest}`);
      if (seq !== S.seq) return;
      if (d.error || !Array.isArray(d.candles)) return;   // exchange hiccup: the next scroll tries again
      const older = (d.candles || []).map(norm).filter((b) => b.time < S.oldest);
      if (!older.length) { S.noMore = key; return; }
      const saved = S.chart.timeScale().getVisibleLogicalRange();
      S.bars = older.concat(S.bars);
      S.oldest = S.bars[0].time;
      setAllData();
      refreshIndicators(true);
      drawEngine();
      if (saved) S.chart.timeScale().setVisibleLogicalRange({ from: saved.from + older.length, to: saved.to + older.length });
    } catch (e) { /* the live chart keeps working */ } finally { S.loadingOlder = false; }
  }
  function setLive(ok, why) {
    S.live = ok; S.err = ok ? null : why;
    $("live-dot").className = "mk-live " + (ok ? "on" : "err");
    tickStatus();
  }
  function tickStatus() {
    const el = $("status-text");
    if (!S.bars.length) { el.textContent = S.err || "Connecting…"; return; }
    const now = Date.now() + S.serverOffset;
    const closeAt = (S.bars[S.bars.length - 1].time + TF_SEC[S.tf]) * 1000;
    const left = (closeAt - now) / 1000;
    const age = Math.round((Date.now() - S.lastUpdate) / 1000);
    const forex = F.isForex(S.coin);
    const parts = [S.err || `Live · updated ${age < 2 ? "just now" : age + "s ago"}`];
    if (left > 0 && left < TF_SEC[S.tf] * 2) parts.push(`${TF_LABEL[S.tf]} candle closes in ${countdown(left)}`);
    if (forex) parts.push("forex prices refresh every 30s");
    el.textContent = parts.join(" · ");
  }
  function updateLatestBtn() {
    if (!S.chart) return;
    let pos = 0;
    try { pos = S.chart.timeScale().scrollPosition(); } catch (e) { pos = 0; }
    $("latest-btn").hidden = !(pos < -8);
  }

  // ================================================================== header: price, 24h stats, signal chip, star
  function change24() {
    const n = S.bars.length;
    if (n < 2) return null;
    const last = S.bars[n - 1];
    const i = barAtOrBefore(last.time + TF_SEC[S.tf] - 86400 - 1);
    if (i < 0 || i >= n - 1 || TF_SEC[S.tf] > 86400) return null;
    return (last.close / S.bars[i].close - 1) * 100;
  }
  function updateHeader() {
    const t = S.tickers[S.coin];
    const last = S.bars.length ? S.bars[S.bars.length - 1].close : null;
    const px = last != null ? last : t && t.last;
    $("q-price").textContent = fmt(px);
    const ch = t && t.change_pct != null ? t.change_pct : change24();
    const chg = $("q-chg");
    chg.textContent = ch == null ? "\u00a0" : `${F.pct(ch)} 24h`;
    chg.className = "tnum mk-chg " + (ch == null ? "" : ch >= 0 ? "c-long" : "c-short");
    $("q-high").textContent = t && t.high != null ? fmt(t.high) : "--";
    $("q-low").textContent = t && t.low != null ? fmt(t.low) : "--";
    $("q-vol").textContent = t && t.quote_volume ? F.usd(t.quote_volume) : "--";
    $("q-stats").hidden = !t;
    document.title = `${S.coin} ${px != null ? fmt(px) : ""} | Live chart | Signals FM`;
  }
  function renderRecent() {
    const r = picker.recent().filter((s) => s !== S.coin).slice(0, 5);
    $("recent").innerHTML = r.map((s) => `<button type="button" class="mk-chip" data-sym="${F.esc(s)}" title="${F.esc(s)}">${F.esc(F.base(s))}</button>`).join("");
  }
  function updateStar() {
    const on = S.watch.has(S.coin);
    const b = $("star-btn");
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.setAttribute("aria-label", on ? "Remove from watchlist" : "Add to watchlist");
  }
  async function toggleStar() {
    const sym = S.coin, on = S.watch.has(sym);
    const d = on ? await F.sendJSON(`/api/watchlist/${encodeURI(sym)}`, "DELETE") : await F.sendJSON("/api/watchlist", "POST", { symbol: sym });
    if (d._ok || (!on && /already/i.test(d.error || ""))) {
      if (on) S.watch.delete(sym); else S.watch.add(sym);
      picker.setWatch(S.watch);
      updateStar();
      toast(on ? `${sym} removed from your watchlist` : `${sym} added to your watchlist`);
    } else toast(d.error || "The watchlist could not be updated");
  }

  // ================================================================== coin + timeframe switching
  const picker = F.coinPicker({
    current: () => S.coin,
    onSelect: (sym) => selectCoin(sym),
    onOpen: () => { if (Date.now() - S.boardAt > 60000) loadBoard(); },
  });
  function selectCoin(sym, initial) {
    if (!picker.valid(sym)) sym = "BTC/USDT";
    const changed = sym !== S.coin || initial;
    S.coin = sym;
    picker.remember(sym);
    F.store.set("sfm-coin", sym);
    $("coin-sym").textContent = sym;
    $("coin-kind").textContent = F.isForex(sym) ? (sym.startsWith("XAU") ? "Gold" : "Forex") : "Crypto";
    F.setIcon($("coin-icon"), sym);
    $("alert-sym").textContent = sym;
    syncUrl(); renderRecent(); updateStar(); updateLinks();
    if (!changed) return;
    S.eng = null; S.alerts = []; S.bars = [];
    clearDrawingsLocal();
    renderEngineCard(); renderAlertList();
    loadCandles(); loadEngine(); loadAlerts();
  }
  function selectTf(tf, limit) {
    if (!TF_SEC[tf]) return;
    S.tf = tf; S.limit = limit || 300;
    if (!limit) S.range = null;
    F.store.set("sfm-chart-tf", tf);
    document.querySelectorAll("#tf-seg button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.tf === tf ? "true" : "false"));
    document.querySelectorAll("#range-seg button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.range === S.range ? "true" : "false"));
    $("snap-tf").textContent = `on ${TF_LABEL[tf]} candles`;
    syncUrl(); updateLinks();
    clearDrawingsLocal();
    loadCandles();
  }
  function syncUrl() {
    try { history.replaceState(null, "", `/chart?coin=${enc(S.coin)}&tf=${enc(S.tf)}`); } catch (e) {}
  }
  function updateLinks() {
    $("liq-link").href = `/liquidity-scanner?coin=${enc(S.coin)}`;
    $("pro-link").href = `/advanced?coin=${enc(S.coin)}`;
    $("eng-link").href = `/?coin=${enc(S.coin)}`;
    $("sig-chip").href = `/?coin=${enc(S.coin)}`;
  }

  // ================================================================== legend (OHLC + indicator values at the crosshair)
  function onCrosshair(p) {
    S.hover = p && p.time !== undefined ? p.time : null;
    legend();
    if (pendingPoints.length && p && p.point && p.time !== undefined) {
      const price = S.main.coordinateToPrice(p.point.y);
      if (price != null) renderDrawings({ time: p.time, price });
    } else if (pendingPoints.length) renderDrawings();
  }
  function legend() {
    const el = $("legend");
    if (!S.bars.length) { el.innerHTML = ""; return; }
    let i = S.hover == null ? S.bars.length - 1 : barIndex(S.hover);
    if (i < 0) i = S.bars.length - 1;
    const b = S.bars[i], p = i > 0 ? S.bars[i - 1] : null;
    const ch = p ? b.close - p.close : null, chp = p ? (b.close / p.close - 1) * 100 : null;
    const cls = ch == null ? "" : ch >= 0 ? "c-long" : "c-short";
    let html = `<div class="l1"><span class="sym">${F.esc(S.coin)} · ${TF_LABEL[S.tf]}${F.isForex(S.coin) ? "" : " · OKX"}</span>
      <span><span class="k">O</span><b>${fmt(b.open)}</b></span><span><span class="k">H</span><b>${fmt(b.high)}</b></span>
      <span><span class="k">L</span><b>${fmt(b.low)}</b></span><span><span class="k">C</span><b class="${cls}">${fmt(b.close)}</b></span>
      ${ch == null ? "" : `<span class="${cls}">${ch >= 0 ? "+" : "−"}${fmt(Math.abs(ch))} (${F.pct(chp)})</span>`}
      ${b.volume ? `<span><span class="k">Vol</span><b>${compact(b.volume)}</b></span>` : ""}</div>`;
    const rows = [];
    Object.keys(S.ind).forEach((key) => {
      const meta = S.indMeta[key];
      const def = IND[key];
      if (!def) return;
      const short = F.esc(def.short(paramsFor(key)));
      if (!meta || !meta.maps.length) { rows.push(`<span class="ind"><i style="background:${def.color}"></i>${short}</span>`); return; }
      const vals = meta.maps.map((m, k) => {
        const v = m.get(b.time);
        return v == null ? null : `<b style="color:${meta.colors[k]}">${meta.fmt ? meta.fmt(v) : fmtVal(v)}</b>`;
      }).filter(Boolean);
      rows.push(`<span class="ind"><i style="background:${def.color}"></i>${short} ${vals.join(" ")}</span>`);
    });
    if (rows.length) html += `<div class="row">${rows.join("")}</div>`;
    el.innerHTML = html;
  }
  function compact(v) {
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(2) + "K";
    return a >= 10 ? v.toFixed(0) : v.toFixed(2);
  }
  function fmtVal(v) {
    const a = Math.abs(v);
    if (a >= 1e6) return compact(v);
    if (a >= 1000) return fmt(v);
    return a >= 1 ? v.toFixed(2) : fmt(v);
  }

  // ================================================================== indicators
  function sma(values, p) {
    const out = new Array(values.length).fill(null);
    let sum = 0, cnt = 0;
    for (let i = 0; i < values.length; i++) {
      const v = values[i];
      if (v == null) { sum = 0; cnt = 0; continue; }
      sum += v; cnt++;
      if (cnt > p) { sum -= values[i - p]; cnt = p; }
      if (cnt === p) out[i] = sum / p;
    }
    return out;
  }
  function ema(values, p) {
    const out = new Array(values.length).fill(null);
    const k = 2 / (p + 1);
    let prev = null, seed = [], i0 = -1;
    for (let i = 0; i < values.length; i++) {
      const v = values[i];
      if (v == null) continue;
      if (prev === null) {
        seed.push(v);
        if (seed.length === p) { prev = seed.reduce((a, b) => a + b, 0) / p; out[i] = prev; i0 = i; }
        continue;
      }
      prev = v * k + prev * (1 - k);
      out[i] = prev;
    }
    return i0 >= 0 ? out : out;
  }
  function rsi(closes, p) {
    const out = new Array(closes.length).fill(null);
    let g = 0, l = 0;
    for (let i = 1; i < closes.length; i++) {
      const ch = closes[i] - closes[i - 1], up = Math.max(ch, 0), dn = Math.max(-ch, 0);
      if (i <= p) { g += up / p; l += dn / p; } else { g = (g * (p - 1) + up) / p; l = (l * (p - 1) + dn) / p; }
      if (i >= p) out[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l);
    }
    return out;
  }
  function atr(bars, p) {
    const out = new Array(bars.length).fill(null);
    let a = null, sum = 0;
    for (let i = 1; i < bars.length; i++) {
      const b = bars[i], pc = bars[i - 1].close;
      const tr = Math.max(b.high - b.low, Math.abs(b.high - pc), Math.abs(b.low - pc));
      if (i <= p) { sum += tr; if (i === p) { a = sum / p; out[i] = a; } continue; }
      a = (a * (p - 1) + tr) / p;
      out[i] = a;
    }
    return out;
  }
  function stdev(values, mid, p, i) {
    let s = 0;
    for (let j = i - p + 1; j <= i; j++) s += (values[j] - mid) * (values[j] - mid);
    return Math.sqrt(s / p);
  }
  function rollMax(arr, p, i) { let m = -Infinity; for (let j = Math.max(0, i - p + 1); j <= i; j++) if (arr[j] > m) m = arr[j]; return m; }
  function rollMin(arr, p, i) { let m = Infinity; for (let j = Math.max(0, i - p + 1); j <= i; j++) if (arr[j] < m) m = arr[j]; return m; }
  function macd(closes, f, s, sig) {
    const ef = ema(closes, f), es = ema(closes, s);
    const m = closes.map((_, i) => (ef[i] != null && es[i] != null ? ef[i] - es[i] : null));
    const sg = ema(m, sig);
    return { macd: m, signal: sg, hist: m.map((v, i) => (v != null && sg[i] != null ? v - sg[i] : null)) };
  }
  function adx(bars, p) {
    const n = bars.length, pdi = new Array(n).fill(null), mdi = new Array(n).fill(null), ad = new Array(n).fill(null);
    let pdm = 0, mdm = 0, tr = 0;
    for (let i = 1; i < n; i++) {
      const up = bars[i].high - bars[i - 1].high, dn = bars[i - 1].low - bars[i].low;
      const a = up > dn && up > 0 ? up : 0, b = dn > up && dn > 0 ? dn : 0;
      const t = Math.max(bars[i].high - bars[i].low, Math.abs(bars[i].high - bars[i - 1].close), Math.abs(bars[i].low - bars[i - 1].close));
      if (i <= p) { pdm += a; mdm += b; tr += t; if (i !== p) continue; }
      else { pdm = pdm - pdm / p + a; mdm = mdm - mdm / p + b; tr = tr - tr / p + t; }
      const P = tr ? 100 * pdm / tr : 0, M = tr ? 100 * mdm / tr : 0;
      const dx = P + M ? 100 * Math.abs(P - M) / (P + M) : 0;
      pdi[i] = P; mdi[i] = M;
      ad[i] = i === p ? dx : ad[i - 1] != null ? (ad[i - 1] * (p - 1) + dx) / p : dx;
    }
    return { adx: ad, pdi, mdi };
  }
  function supertrend(bars, p, mult) {
    const a = atr(bars, p), n = bars.length, val = new Array(n).fill(null), dir = new Array(n).fill(1);
    let fu = null, fl = null;
    for (let i = 0; i < n; i++) {
      if (a[i] == null) continue;
      const mid = (bars[i].high + bars[i].low) / 2, bu = mid + mult * a[i], bl = mid - mult * a[i];
      let nu, nl;
      if (fu === null) { nu = bu; nl = bl; }
      else { nu = bu < fu || bars[i - 1].close > fu ? bu : fu; nl = bl > fl || bars[i - 1].close < fl ? bl : fl; }
      let d;
      if (fu === null) d = 1;
      else if (dir[i - 1] === 1) d = bars[i].close < nl ? -1 : 1;
      else d = bars[i].close > nu ? 1 : -1;
      val[i] = d === 1 ? nl : nu; dir[i] = d; fu = nu; fl = nl;
    }
    return { val, dir };
  }
  function psar(bars, step, max) {
    const n = bars.length, out = new Array(n).fill(null), up = new Array(n).fill(true);
    if (n < 3) return { out, up };
    let bull = bars[1].close >= bars[0].close, af = step, ep = bull ? bars[0].high : bars[0].low, sar = bull ? bars[0].low : bars[0].high;
    for (let i = 1; i < n; i++) {
      sar = sar + af * (ep - sar);
      if (bull) {
        sar = Math.min(sar, bars[i - 1].low, i > 1 ? bars[i - 2].low : bars[i - 1].low);
        if (bars[i].low < sar) { bull = false; sar = ep; ep = bars[i].low; af = step; }
        else if (bars[i].high > ep) { ep = bars[i].high; af = Math.min(af + step, max); }
      } else {
        sar = Math.max(sar, bars[i - 1].high, i > 1 ? bars[i - 2].high : bars[i - 1].high);
        if (bars[i].high > sar) { bull = true; sar = ep; ep = bars[i].high; af = step; }
        else if (bars[i].low < ep) { ep = bars[i].low; af = Math.min(af + step, max); }
      }
      out[i] = sar; up[i] = bull;
    }
    return { out, up };
  }
  function vwap(bars, anchor) {
    const out = new Array(bars.length).fill(null);
    let pv = 0, vv = 0, key = null;
    for (let i = 0; i < bars.length; i++) {
      const b = bars[i];
      const k = anchor === "day" ? Math.floor(b.time / 86400) : anchor === "week" ? Math.floor((b.time - 345600) / 604800) : 0;
      if (k !== key) { pv = 0; vv = 0; key = k; }
      if (b.volume > 0) { const tp = (b.high + b.low + b.close) / 3; pv += tp * b.volume; vv += b.volume; }
      out[i] = vv > 0 ? pv / vv : null;
    }
    return out;
  }
  function mfi(bars, p) {
    const n = bars.length, out = new Array(n).fill(null), tp = bars.map((b) => (b.high + b.low + b.close) / 3);
    for (let i = p; i < n; i++) {
      let pos = 0, neg = 0;
      for (let j = i - p + 1; j <= i; j++) {
        const f = tp[j] * (bars[j].volume || 0);
        if (tp[j] > tp[j - 1]) pos += f; else if (tp[j] < tp[j - 1]) neg += f;
      }
      out[i] = neg === 0 ? 100 : 100 - 100 / (1 + pos / neg);
    }
    return out;
  }
  function stoch(bars, k, smooth, d) {
    const raw = bars.map((b, i) => {
      if (i < k - 1) return null;
      let hh = -Infinity, ll = Infinity;
      for (let j = i - k + 1; j <= i; j++) { hh = Math.max(hh, bars[j].high); ll = Math.min(ll, bars[j].low); }
      return hh === ll ? 50 : ((b.close - ll) / (hh - ll)) * 100;
    });
    const K = sma(raw, smooth);
    return { k: K, d: sma(K, d) };
  }
  function cci(bars, p) {
    const tp = bars.map((b) => (b.high + b.low + b.close) / 3), m = sma(tp, p), out = new Array(bars.length).fill(null);
    for (let i = p - 1; i < bars.length; i++) {
      if (m[i] == null) continue;
      let md = 0;
      for (let j = i - p + 1; j <= i; j++) md += Math.abs(tp[j] - m[i]);
      md /= p;
      out[i] = md ? (tp[i] - m[i]) / (0.015 * md) : 0;
    }
    return out;
  }
  function obv(bars) {
    const out = new Array(bars.length).fill(0);
    for (let i = 1; i < bars.length; i++) {
      const v = bars[i].volume || 0;
      out[i] = out[i - 1] + (bars[i].close > bars[i - 1].close ? v : bars[i].close < bars[i - 1].close ? -v : 0);
    }
    return out;
  }
  function swings(bars, k) {
    const hi = [], lo = [];
    for (let i = k; i < bars.length - k; i++) {
      let h = true, l = true;
      for (let j = i - k; j <= i + k; j++) {
        if (j === i) continue;
        if (bars[j].high >= bars[i].high) h = false;
        if (bars[j].low <= bars[i].low) l = false;
      }
      if (h) hi.push(i);
      if (l) lo.push(i);
    }
    return { hi, lo };
  }
  function zigzag(bars, k) {
    const { hi, lo } = swings(bars, k);
    const all = hi.map((i) => ({ i, dir: 1, price: bars[i].high })).concat(lo.map((i) => ({ i, dir: -1, price: bars[i].low }))).sort((a, b) => a.i - b.i);
    const pts = [];
    for (const e of all) {
      const last = pts[pts.length - 1];
      if (!last) { pts.push(e); continue; }
      if (e.dir === last.dir) { if ((e.dir === 1 && e.price > last.price) || (e.dir === -1 && e.price < last.price)) pts[pts.length - 1] = e; }
      else pts.push(e);
    }
    return pts;
  }
  function structureEvents(pts) {
    const ev = [];
    if (pts.length < 3) return ev;
    let trend = pts[1].price > pts[0].price ? 1 : -1;
    for (let i = 2; i < pts.length; i++) {
      const c = pts[i], p2 = pts[i - 2];
      if (trend === 1) {
        if (c.dir === -1 && c.price < p2.price) { ev.push({ i: c.i, type: "CHoCH", price: c.price }); trend = -1; }
        else if (c.dir === 1 && c.price > p2.price) ev.push({ i: c.i, type: "BOS", price: c.price });
      } else {
        if (c.dir === 1 && c.price > p2.price) { ev.push({ i: c.i, type: "CHoCH", price: c.price }); trend = 1; }
        else if (c.dir === -1 && c.price < p2.price) ev.push({ i: c.i, type: "BOS", price: c.price });
      }
    }
    return ev;
  }
  function cluster(prices, tolPct) {
    const g = [];
    prices.forEach(([price, support]) => {
      for (const x of g) if (Math.abs(x.price - price) / price <= tolPct) { x.price = (x.price * x.n + price) / (x.n + 1); x.n++; return; }
      g.push({ price, n: 1, support });
    });
    return g;
  }
  const pts = (bars, vals, colorFn) => {
    const out = [];
    for (let i = 0; i < bars.length; i++) {
      const v = vals[i];
      if (v == null || Number.isNaN(v)) continue;
      const p = { time: bars[i].time, value: v };
      if (colorFn) { const c = colorFn(i, v); if (c) p.color = c; }
      out.push(p);
    }
    return out;
  };
  const L = (color, width, extra) => Object.assign({ color, lineWidth: width || 1.5, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }, extra || {});
  const C = { gold: "#e2b93b", blue: "#4dabf7", purple: "#c084fc", orange: "#f5a623", cyan: "#2dd4bf", pink: "#f472b6", gray: "#8a9aa8", up: "#36e0a0", down: "#ff526b" };
  const num = (k, label, def, min, max, step) => ({ k, label, def, min, max, step: step || 1 });
  const sel = (k, label, def, opts) => ({ k, label, def, opts });

  const INDICATORS = [
    // ---- Trend (on the price)
    { key: "ema", name: "EMA", group: "Trend", color: C.gold, type: "overlay", params: [num("a", "Fast", 20, 2, 500), num("b", "Mid", 50, 2, 500), num("c", "Slow", 200, 2, 1000)],
      desc: (p) => `Exponential moving averages ${p.a} / ${p.b} / ${p.c}`, short: (p) => `EMA ${p.a} ${p.b} ${p.c}`,
      compute(bars, p) { const c = bars.map((b) => b.close), cols = [C.gold, C.blue, C.purple];
        return { series: [p.a, p.b, p.c].map((n, i) => ({ kind: "line", options: L(cols[i], i === 2 ? 2 : 1.4), data: pts(bars, ema(c, n)) })) }; } },
    { key: "sma", name: "SMA", group: "Trend", color: C.orange, type: "overlay", params: [num("a", "Length", 50, 2, 500), num("b", "Length 2", 200, 0, 1000)],
      desc: (p) => `Simple moving averages ${p.a}${p.b ? " / " + p.b : ""}`, short: (p) => `SMA ${p.a}${p.b ? " " + p.b : ""}`,
      compute(bars, p) { const c = bars.map((b) => b.close), s = [{ kind: "line", options: L(C.orange, 1.6), data: pts(bars, sma(c, p.a)) }];
        if (p.b) s.push({ kind: "line", options: L(C.pink, 2, { lineStyle: 2 }), data: pts(bars, sma(c, p.b)) });
        return { series: s }; } },
    { key: "vwap", name: "VWAP", group: "Trend", color: C.cyan, type: "overlay", params: [sel("anchor", "Resets", "day", [["day", "Every day (UTC)"], ["week", "Every week"], ["all", "Never"]])],
      desc: () => "Volume-weighted average price", short: (p) => `VWAP (${p.anchor})`,
      compute(bars, p) { return { series: [{ kind: "line", options: L(C.cyan, 2), data: pts(bars, vwap(bars, p.anchor)) }] }; } },
    { key: "supertrend", name: "Supertrend", group: "Trend", color: C.up, type: "overlay", params: [num("p", "ATR length", 10, 2, 100), num("m", "Multiplier", 3, 0.5, 10, 0.1)],
      desc: (p) => `ATR trend follower ${p.p} / ${p.m}`, short: (p) => `Supertrend ${p.p} ${p.m}`,
      compute(bars, p) { const st = supertrend(bars, p.p, p.m);
        return { series: [{ kind: "line", options: L(C.up, 2), data: pts(bars, st.val, (i) => (st.dir[i] === 1 ? C.up : C.down)) }] }; } },
    { key: "bollinger", name: "Bollinger Bands", group: "Trend", color: C.purple, type: "overlay", params: [num("p", "Length", 20, 2, 300), num("m", "Std dev", 2, 0.5, 5, 0.1)],
      desc: (p) => `Volatility bands ${p.p} · ${p.m}σ`, short: (p) => `BB ${p.p} ${p.m}`,
      compute(bars, p) { const c = bars.map((b) => b.close), mid = sma(c, p.p);
        const up = mid.map((m, i) => (m == null ? null : m + p.m * stdev(c, m, p.p, i))), lo = mid.map((m, i) => (m == null ? null : m - p.m * stdev(c, m, p.p, i)));
        return { series: [{ kind: "line", options: L(C.purple, 1.2, { lineStyle: 2 }), data: pts(bars, up) }, { kind: "line", options: L(C.gold, 1.3), data: pts(bars, mid) },
                          { kind: "line", options: L(C.purple, 1.2, { lineStyle: 2 }), data: pts(bars, lo) }], band: [up, lo, "rgba(192,132,252,0.08)"] }; },
      svg(bars, ctx, p, computed) { if (computed && computed.band) ctx.fillBand(computed.band[0], computed.band[1], computed.band[2]); } },
    { key: "keltner", name: "Keltner Channel", group: "Trend", color: C.blue, type: "overlay", params: [num("p", "Length", 20, 2, 300), num("m", "ATR mult", 2, 0.5, 6, 0.1)],
      desc: (p) => `EMA ${p.p} ± ${p.m} × ATR`, short: (p) => `KC ${p.p} ${p.m}`,
      compute(bars, p) { const mid = ema(bars.map((b) => b.close), p.p), a = atr(bars, p.p);
        const up = mid.map((m, i) => (m == null || a[i] == null ? null : m + p.m * a[i])), lo = mid.map((m, i) => (m == null || a[i] == null ? null : m - p.m * a[i]));
        return { series: [{ kind: "line", options: L(C.blue, 1.2), data: pts(bars, up) }, { kind: "line", options: L(C.blue, 1, { lineStyle: 2 }), data: pts(bars, mid) },
                          { kind: "line", options: L(C.blue, 1.2), data: pts(bars, lo) }] }; } },
    { key: "donchian", name: "Donchian Channel", group: "Trend", color: C.cyan, type: "overlay", params: [num("p", "Length", 20, 2, 300)],
      desc: (p) => `Highest high / lowest low of ${p.p} candles`, short: (p) => `DC ${p.p}`,
      compute(bars, p) { const h = bars.map((b) => b.high), l = bars.map((b) => b.low);
        const up = bars.map((_, i) => (i < p.p - 1 ? null : rollMax(h, p.p, i))), lo = bars.map((_, i) => (i < p.p - 1 ? null : rollMin(l, p.p, i)));
        return { series: [{ kind: "line", options: L(C.cyan, 1.2), data: pts(bars, up) }, { kind: "line", options: L(C.gray, 1, { lineStyle: 2 }), data: pts(bars, up.map((u, i) => (u == null ? null : (u + lo[i]) / 2))) },
                          { kind: "line", options: L(C.cyan, 1.2), data: pts(bars, lo) }] }; } },
    { key: "psar", name: "Parabolic SAR", group: "Trend", color: C.orange, type: "overlay", params: [num("s", "Step", 0.02, 0.005, 0.2, 0.005), num("m", "Max", 0.2, 0.05, 1, 0.05)],
      desc: (p) => `Stop-and-reverse dots ${p.s} / ${p.m}`, short: (p) => `SAR ${p.s} ${p.m}`,
      compute(bars, p) { const r = psar(bars, p.s, p.m);
        return { series: [{ kind: "line", options: L(C.orange, 1, { lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 1.6 }), data: pts(bars, r.out, (i) => (r.up[i] ? C.up : C.down)) }] }; } },
    { key: "ichimoku", name: "Ichimoku Cloud", group: "Trend", color: C.blue, type: "svg", params: [num("t", "Tenkan", 9, 2, 100), num("k", "Kijun", 26, 2, 200), num("s", "Senkou B", 52, 2, 300)],
      desc: (p) => `Tenkan ${p.t} · Kijun ${p.k} · cloud ${p.s}`, short: () => "Ichimoku",
      svg(bars, ctx, p) {
        const n = bars.length, h = bars.map((b) => b.high), l = bars.map((b) => b.low);
        const mid = (q) => bars.map((_, i) => (i < q - 1 ? null : (rollMax(h, q, i) + rollMin(l, q, i)) / 2));
        const tk = mid(p.t), kj = mid(p.k), sb = mid(p.s), a = new Array(n).fill(null), b = new Array(n).fill(null);
        for (let i = 0; i + p.k < n; i++) { a[i + p.k] = tk[i] != null && kj[i] != null ? (tk[i] + kj[i]) / 2 : null; b[i + p.k] = sb[i]; }
        ctx.fillBand(a, b, "rgba(77,171,247,0.10)");
        ctx.polyline(tk, C.blue, 1.1); ctx.polyline(kj, C.down, 1.1);
        const chikou = new Array(n).fill(null);
        for (let i = p.k; i < n; i++) chikou[i - p.k] = bars[i].close;
        ctx.polyline(chikou, C.gray, 1);
      } },
    { key: "pivot", name: "Pivot Points", group: "Trend", color: C.orange, type: "svg", params: [],
      desc: () => "Classic pivots from the previous day (previous week on daily charts)", short: () => "Pivots",
      svg(bars, ctx) {
        if (bars.length < 3) return;
        const per = TF_SEC[S.tf] >= 86400 ? 604800 : 86400, off = per === 604800 ? 345600 : 0;
        const key = (t) => Math.floor((t - off) / per), cur = key(bars[bars.length - 1].time);
        let H = -Infinity, Lw = Infinity, Cl = null;
        for (const b of bars) if (key(b.time) === cur - 1) { H = Math.max(H, b.high); Lw = Math.min(Lw, b.low); Cl = b.close; }
        if (Cl == null) return;
        const P = (H + Lw + Cl) / 3;
        [[H + 2 * (P - Lw), "R3", C.down], [P + (H - Lw), "R2", C.down], [2 * P - Lw, "R1", C.down], [P, "P", C.gold], [2 * P - H, "S1", C.up], [P - (H - Lw), "S2", C.up], [Lw - 2 * (H - P), "S3", C.up]]
          .forEach(([v, lab, col]) => ctx.hline(v, col, `${lab} ${fmt(v)}`, lab === "P"));
      } },
    // ---- Momentum (own panes)
    { key: "rsi", name: "RSI", group: "Momentum", color: C.purple, type: "pane", params: [num("p", "Length", 14, 2, 100), num("hi", "Upper", 70, 50, 95), num("lo", "Lower", 30, 5, 50)],
      desc: (p) => `Relative Strength Index ${p.p}`, short: (p) => `RSI ${p.p}`,
      compute(bars, p) { return { series: [{ kind: "line", options: L(C.purple, 1.8, { lastValueVisible: true }), data: pts(bars, rsi(bars.map((b) => b.close), p.p)),
        refs: [[p.hi, "rgba(255,82,107,0.55)"], [p.lo, "rgba(54,224,160,0.55)"], [50, "rgba(150,160,170,0.25)"]] }], height: 110, fmt: (v) => v.toFixed(1) }; } },
    { key: "macd", name: "MACD", group: "Momentum", color: C.cyan, type: "pane", params: [num("f", "Fast", 12, 2, 100), num("s", "Slow", 26, 3, 200), num("g", "Signal", 9, 2, 50)],
      desc: (p) => `${p.f} · ${p.s} · ${p.g}`, short: (p) => `MACD ${p.f} ${p.s} ${p.g}`,
      compute(bars, p) { const m = macd(bars.map((b) => b.close), p.f, p.s, p.g);
        return { series: [{ kind: "histogram", options: { priceLineVisible: false, lastValueVisible: false, base: 0 }, data: pts(bars, m.hist, (i, v) => (v >= 0 ? "rgba(54,224,160,0.75)" : "rgba(255,82,107,0.75)")) },
                          { kind: "line", options: L(C.blue, 1.4), data: pts(bars, m.macd) }, { kind: "line", options: L(C.orange, 1.4), data: pts(bars, m.signal) }], height: 130 }; } },
    { key: "stoch", name: "Stochastic", group: "Momentum", color: C.blue, type: "pane", params: [num("k", "%K length", 14, 2, 100), num("s", "%K smoothing", 3, 1, 20), num("d", "%D", 3, 1, 20)],
      desc: (p) => `${p.k} · ${p.s} · ${p.d}`, short: (p) => `Stoch ${p.k} ${p.s} ${p.d}`,
      compute(bars, p) { const r = stoch(bars, p.k, p.s, p.d);
        return { series: [{ kind: "line", options: L(C.blue, 1.5), data: pts(bars, r.k), refs: [[80, "rgba(255,82,107,0.5)"], [20, "rgba(54,224,160,0.5)"]] },
                          { kind: "line", options: L(C.orange, 1.3), data: pts(bars, r.d) }], height: 110, fmt: (v) => v.toFixed(1) }; } },
    { key: "cci", name: "CCI", group: "Momentum", color: C.gold, type: "pane", params: [num("p", "Length", 20, 2, 200)],
      desc: (p) => `Commodity Channel Index ${p.p}`, short: (p) => `CCI ${p.p}`,
      compute(bars, p) { return { series: [{ kind: "line", options: L(C.gold, 1.6), data: pts(bars, cci(bars, p.p)), refs: [[100, "rgba(255,82,107,0.5)"], [-100, "rgba(54,224,160,0.5)"], [0, "rgba(150,160,170,0.25)"]] }], height: 110, fmt: (v) => v.toFixed(0) }; } },
    { key: "adx", name: "ADX", group: "Momentum", color: C.orange, type: "pane", params: [num("p", "Length", 14, 2, 100)],
      desc: (p) => `Trend strength (ADX, +DI, −DI) ${p.p}`, short: (p) => `ADX ${p.p}`,
      compute(bars, p) { const d = adx(bars, p.p);
        return { series: [{ kind: "line", options: L(C.orange, 1.8), data: pts(bars, d.adx), refs: [[25, "rgba(150,160,170,0.3)"]] }, { kind: "line", options: L(C.up, 1), data: pts(bars, d.pdi) },
                          { kind: "line", options: L(C.down, 1), data: pts(bars, d.mdi) }], height: 110, fmt: (v) => v.toFixed(1) }; } },
    { key: "mfi", name: "MFI", group: "Momentum", color: C.gold, type: "pane", params: [num("p", "Length", 14, 2, 100)],
      desc: (p) => `Money Flow Index ${p.p}`, short: (p) => `MFI ${p.p}`,
      compute(bars, p) { return { series: [{ kind: "line", options: L(C.gold, 1.7), data: pts(bars, mfi(bars, p.p)), refs: [[80, "rgba(255,82,107,0.5)"], [20, "rgba(54,224,160,0.5)"]] }], height: 110, fmt: (v) => v.toFixed(1) }; } },
    // ---- Volatility
    { key: "atr", name: "ATR", group: "Volatility", color: C.blue, type: "pane", params: [num("p", "Length", 14, 2, 100)],
      desc: (p) => `Average True Range ${p.p}`, short: (p) => `ATR ${p.p}`,
      compute(bars, p) { return { series: [{ kind: "line", options: L(C.blue, 1.7), data: pts(bars, atr(bars, p.p)) }], height: 100 }; } },
    // ---- Volume
    { key: "obv", name: "OBV", group: "Volume", color: C.purple, type: "pane", params: [],
      desc: () => "On-Balance Volume", short: () => "OBV",
      compute(bars) { return { series: [{ kind: "line", options: L(C.purple, 1.7), data: pts(bars, obv(bars)) }], height: 100, fmt: compact }; } },
    { key: "cvd", name: "Volume Delta", group: "Volume", color: C.gold, type: "pane", params: [],
      desc: () => "Cumulative volume delta (candle direction)", short: () => "CVD",
      compute(bars) { let c = 0; const v = [], d = [];
        bars.forEach((b) => { const x = b.close >= b.open ? (b.volume || 0) : -(b.volume || 0); c += x; v.push(c); d.push(x); });
        return { series: [{ kind: "histogram", options: { priceLineVisible: false, lastValueVisible: false, base: 0 }, data: pts(bars, v, (i) => (d[i] >= 0 ? "rgba(54,224,160,0.7)" : "rgba(255,82,107,0.7)")) }], height: 110, fmt: compact }; } },
    { key: "volprofile", name: "Volume Profile", group: "Volume", color: C.blue, type: "svg", params: [num("rows", "Rows", 26, 8, 80), num("n", "Candles", 150, 30, 1000)],
      desc: (p) => `Volume at price, last ${p.n} candles, with the busiest price (POC)`, short: () => "Volume profile",
      svg(bars, ctx, p) {
        const from = Math.max(0, bars.length - p.n);
        let hi = -Infinity, lo = Infinity;
        for (let i = from; i < bars.length; i++) { hi = Math.max(hi, bars[i].high); lo = Math.min(lo, bars[i].low); }
        if (!(hi > lo)) return;
        const N = p.rows, bw = (hi - lo) / N, bucket = new Array(N).fill(0);
        for (let i = from; i < bars.length; i++) {
          const k = Math.min(N - 1, Math.max(0, Math.floor(((bars[i].high + bars[i].low) / 2 - lo) / bw)));
          bucket[k] += bars[i].volume || 0;
        }
        const maxV = Math.max(...bucket, 1), poc = bucket.indexOf(maxV);
        for (let k = 0; k < N; k++) if (bucket[k] > 0) ctx.hBar(lo + bw * (k + 0.5), bw / 2, Math.max(4, (bucket[k] / maxV) * ctx.width * 0.22), k === poc ? "rgba(242,184,75,0.55)" : "rgba(77,171,247,0.38)");
        ctx.hline(lo + bw * (poc + 0.5), C.gold, `POC ${fmt(lo + bw * (poc + 0.5))}`, true);
      } },
    // ---- Structure (drawn over the chart)
    { key: "sr", name: "Support & Resistance", group: "Structure", color: C.orange, type: "svg", params: [num("k", "Swing size", 2, 1, 10)],
      desc: () => "Levels where price turned more than once", short: () => "S/R",
      svg(bars, ctx, p) {
        const { hi, lo } = swings(bars, p.k);
        const g = cluster(hi.map((i) => [bars[i].high, false]).concat(lo.map((i) => [bars[i].low, true])), 0.0015);
        const top = g.filter((x) => x.n >= 2).sort((a, b) => b.n - a.n).slice(0, 8);
        top.forEach((x) => ctx.hline(x.price, x.support ? "rgba(54,224,160,0.6)" : "rgba(255,82,107,0.6)", `${x.support ? "Support" : "Resistance"} ×${x.n}`, false, true));
      } },
    { key: "fvg", name: "Fair Value Gaps", group: "Structure", color: C.cyan, type: "svg", params: [sel("open", "Show", "open", [["open", "Unfilled only"], ["all", "All recent"]])],
      desc: () => "Three-candle gaps price left behind", short: () => "FVG",
      svg(bars, ctx, p) {
        const n = bars.length, out = [];
        for (let i = 0; i < n - 2; i++) {
          const a = bars[i], c = bars[i + 2];
          let g = null;
          if (a.high < c.low) g = { bull: true, top: c.low, bot: a.high };
          else if (a.low > c.high) g = { bull: false, top: a.low, bot: c.high };
          if (!g) continue;
          let filled = false;
          for (let j = i + 3; j < n && !filled; j++) filled = g.bull ? bars[j].low <= g.bot : bars[j].high >= g.top;
          if (p.open === "open" && filled) continue;
          out.push({ ...g, i });
        }
        out.slice(-30).forEach((g) => ctx.box(bars[g.i].time, p.open === "open" ? null : bars[g.i + 2].time, g.top, g.bot,
          g.bull ? "rgba(54,224,160,0.12)" : "rgba(255,82,107,0.12)", g.bull ? C.up : C.down, "FVG"));
      } },
    { key: "orderblocks", name: "Order Blocks", group: "Structure", color: C.pink, type: "svg", params: [],
      desc: () => "Last opposite candle before a strong move", short: () => "Order blocks",
      svg(bars, ctx) {
        const n = bars.length, blocks = [];
        for (let i = 1; i < n - 1; i++) {
          const body = Math.abs(bars[i].close - bars[i].open), next = Math.abs(bars[i + 1].close - bars[i + 1].open);
          if (body > 0 && next > 1.4 * body) {
            const bull = bars[i].close < bars[i].open && bars[i + 1].close > bars[i + 1].open;
            const bear = bars[i].close > bars[i].open && bars[i + 1].close < bars[i + 1].open;
            if (bull || bear) blocks.push({ i, top: Math.max(bars[i].open, bars[i].close), bot: Math.min(bars[i].open, bars[i].close), bull });
          }
        }
        blocks.slice(-12).forEach((b) => ctx.box(bars[b.i].time, null, b.top, b.bot, b.bull ? "rgba(54,224,160,0.10)" : "rgba(255,82,107,0.10)", b.bull ? C.up : C.down, "OB"));
      } },
    { key: "structure", name: "Market Structure", group: "Structure", color: C.blue, type: "svg", params: [num("k", "Swing size", 2, 1, 10)],
      desc: () => "Swings with breaks of structure (BOS) and changes of character (CHoCH)", short: () => "Structure",
      svg(bars, ctx, p) {
        const zz = zigzag(bars, p.k);
        ctx.polylinePivots(zz, "rgba(141,158,171,0.6)");
        structureEvents(zz).forEach((e) => ctx.marker(e.i, e.price, e.type, e.type === "BOS" ? C.blue : C.orange));
      } },
    { key: "liqradar", name: "Liquidity Radar", group: "Structure", color: C.gold, type: "svg", params: [],
      desc: () => "Equal highs and lows where stops cluster", short: () => "Liquidity radar",
      svg(bars, ctx) {
        const n = bars.length, tol = 0.0008, pools = [];
        const add = (price, eqh) => { for (const x of pools) if (Math.abs(x.price - price) / price <= tol) { x.price = (x.price * x.n + price) / (x.n + 1); x.n++; return; } pools.push({ price, n: 1, eqh }); };
        for (let i = 3; i < n - 3; i++) {
          if (bars.slice(i - 3, i).some((b) => Math.abs(b.high - bars[i].high) / bars[i].high <= tol)) add(bars[i].high, true);
          if (bars.slice(i - 3, i).some((b) => Math.abs(b.low - bars[i].low) / bars[i].low <= tol)) add(bars[i].low, false);
        }
        pools.filter((x) => x.n >= 2).slice(-14).forEach((x) => ctx.hline(x.price, x.eqh ? "rgba(255,82,107,0.5)" : "rgba(54,224,160,0.5)", `${x.eqh ? "Equal highs" : "Equal lows"} ×${x.n}`));
      } },
  ];
  const IND = {};
  INDICATORS.forEach((d) => { IND[d.key] = d; });
  const GROUPS = ["Trend", "Momentum", "Volatility", "Volume", "Structure"];
  const defaults = (def) => Object.fromEntries((def.params || []).map((p) => [p.k, p.def]));
  function paramsFor(key) { const def = IND[key]; return Object.assign(defaults(def), S.ind[key] || {}); }

  function clearIndicatorSeries(key) {
    (S.indSeries[key] || []).forEach((s) => { try { S.chart.removeSeries(s); } catch (e) {} });
    delete S.indSeries[key];
    delete S.indMeta[key];
  }
  function drawIndicator(key, recreate) {
    const def = IND[key];
    if (!def || !S.chart) return;
    const p = paramsFor(key);
    const out = def.compute ? def.compute(S.bars, p) : null;
    S.indMeta[key] = { maps: [], colors: [], fmt: out && out.fmt, computed: out };
    if (!out) { renderIndicatorOverlay(); return; }
    let list = S.indSeries[key];
    if (recreate || !list || list.length !== out.series.length) {
      clearIndicatorSeries(key);
      S.indMeta[key] = { maps: [], colors: [], fmt: out.fmt, computed: out };
      const pane = def.type === "pane" ? S.chart.panes().length : 0;
      list = out.series.map((sp) => {
        const kind = sp.kind === "histogram" ? LW.HistogramSeries : LW.LineSeries;
        const s = S.chart.addSeries(kind, sp.options, pane);
        (sp.refs || []).forEach(([price, color]) => { try { s.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: false }); } catch (e) {} });
        return s;
      });
      S.indSeries[key] = list;
      if (def.type === "pane" && list.length) { try { list[0].getPane().setHeight(out.height || 110); } catch (e) {} }
    }
    out.series.forEach((sp, i) => {
      try { list[i].setData(sp.data); } catch (e) {}
      S.indMeta[key].maps.push(new Map(sp.data.map((d) => [d.time, d.value])));
      S.indMeta[key].colors.push((sp.options && sp.options.color) || def.color);
    });
  }
  function refreshIndicators(recreate) {
    if (!S.chart) return;
    Object.keys(S.ind).forEach((k) => { if (IND[k]) drawIndicator(k, recreate); else delete S.ind[k]; });
    renderIndicatorOverlay();
    updateIndCount();
  }
  function toggleIndicator(key, on) {
    if (on) { S.ind[key] = S.ind[key] || {}; drawIndicator(key, true); }
    else { delete S.ind[key]; clearIndicatorSeries(key); }
    savePrefs();
    renderIndicatorOverlay(); legend(); renderIndPanel(); updateIndCount();
  }
  function updateIndCount() {
    const n = Object.keys(S.ind).length, c = $("ind-count");
    c.hidden = !n; c.textContent = n;
  }
  function renderIndPanel() {
    const q = $("ind-search").value.trim().toLowerCase();
    const act = Object.keys(S.ind);
    $("ind-active").hidden = !act.length;
    $("ind-active").innerHTML = act.map((k) => {
      const d = IND[k];
      return d ? `<div class="ch-ind-act"><i style="background:${d.color}"></i><b>${F.esc(d.name)}</b><small>${F.esc(d.desc(paramsFor(k)))}</small>
        ${d.params.length ? `<button type="button" data-set="${k}">Settings</button>` : ""}<button type="button" data-off="${k}" aria-label="Remove ${F.esc(d.name)}">✕</button></div>
        <div class="ch-ind-set" id="set-${k}" hidden></div>` : "";
    }).join("");
    let html = "", shown = 0;
    GROUPS.forEach((g) => {
      const items = INDICATORS.filter((d) => d.group === g && (!q || `${d.name} ${d.desc(defaults(d))} ${g}`.toLowerCase().includes(q)));
      if (!items.length) return;
      html += `<div class="ch-ind-grp">${g}</div>`;
      items.forEach((d) => {
        shown++;
        html += `<button type="button" class="ch-ind-row" data-key="${d.key}" aria-pressed="${!!S.ind[d.key]}"><span><b>${F.esc(d.name)}</b><small>${F.esc(d.desc(paramsFor(d.key)))}</small></span><i class="ch-sw" aria-hidden="true"></i></button>`;
      });
    });
    $("ind-list").innerHTML = shown ? html : `<p class="ch-ind-empty">No indicator matches “${F.esc(q)}”.</p>`;
  }
  function openSettings(key) {
    const box = $(`set-${key}`), d = IND[key];
    if (!box || !d) return;
    if (!box.hidden) { box.hidden = true; return; }
    const p = paramsFor(key);
    box.innerHTML = d.params.map((x) => x.opts
      ? `<label>${F.esc(x.label)}<select data-k="${x.k}">${x.opts.map(([v, t]) => `<option value="${v}"${p[x.k] === v ? " selected" : ""}>${F.esc(t)}</option>`).join("")}</select></label>`
      : `<label>${F.esc(x.label)}<input type="number" data-k="${x.k}" value="${p[x.k]}" min="${x.min}" max="${x.max}" step="${x.step}" inputmode="decimal"></label>`).join("")
      + `<button type="button" class="mk-btn ch-ind-reset" data-reset="${key}">Default</button>`;
    box.hidden = false;
    box.querySelectorAll("input, select").forEach((inp) => inp.addEventListener("change", () => {
      const spec = d.params.find((x) => x.k === inp.dataset.k);
      let v = inp.value;
      if (!spec.opts) { v = Number(v); if (!isFinite(v)) return; v = Math.max(spec.min, Math.min(spec.max, v)); inp.value = v; }
      S.ind[key] = Object.assign({}, S.ind[key] || {}, { [spec.k]: v });
      savePrefs(); drawIndicator(key, true); renderIndicatorOverlay(); legend();
      const small = box.previousElementSibling && box.previousElementSibling.querySelector("small");
      if (small) small.textContent = d.desc(paramsFor(key));
    }));
  }
  $("ind-list").addEventListener("click", (e) => {
    const row = e.target.closest(".ch-ind-row");
    if (row) toggleIndicator(row.dataset.key, !S.ind[row.dataset.key]);
  });
  $("ind-active").addEventListener("click", (e) => {
    const off = e.target.closest("[data-off]"), set = e.target.closest("[data-set]"), rs = e.target.closest("[data-reset]");
    if (off) toggleIndicator(off.dataset.off, false);
    else if (set) openSettings(set.dataset.set);
    else if (rs) { S.ind[rs.dataset.reset] = {}; savePrefs(); drawIndicator(rs.dataset.reset, true); renderIndicatorOverlay(); legend(); renderIndPanel(); }
  });
  $("ind-search").addEventListener("input", renderIndPanel);
  $("ind-clear").addEventListener("click", () => { Object.keys(S.ind).forEach((k) => clearIndicatorSeries(k)); S.ind = {}; savePrefs(); renderIndicatorOverlay(); legend(); renderIndPanel(); updateIndCount(); });

  // SVG overlay for drawn indicators (Ichimoku, pivots, structure, volume profile, Bollinger fill)
  function paneSize() {
    let w = $("ch-chart").clientWidth, h = $("ch-chart").clientHeight;
    try { w = S.chart.timeScale().width() || w; h = S.chart.panes()[0].getHeight() || h; } catch (e) {}
    return { w, h };
  }
  function renderIndicatorOverlay() {
    const svg = $("ind-svg");
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (!S.chart || !S.main || !S.bars.length) return;
    const keys = Object.keys(S.ind).filter((k) => IND[k] && IND[k].svg);
    if (!keys.length) return;
    const { w: width, h: height } = paneSize();
    const NS = "http://www.w3.org/2000/svg";
    const X = (t) => S.chart.timeScale().timeToCoordinate(t), Y = (p) => S.main.priceToCoordinate(p);
    const ok = (v) => v !== null && v !== undefined && !Number.isNaN(v);
    const bars = S.bars;
    const mk = (tag, attrs) => { const n = document.createElementNS(NS, tag); Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); svg.appendChild(n); return n; };
    const clipId = "ind-clip";
    const defs = mk("defs", {});
    const cp = document.createElementNS(NS, "clipPath"); cp.setAttribute("id", clipId);
    const cr = document.createElementNS(NS, "rect"); cr.setAttribute("x", 0); cr.setAttribute("y", 0); cr.setAttribute("width", width); cr.setAttribute("height", height);
    cp.appendChild(cr); defs.appendChild(cp);
    const g = mk("g", { "clip-path": `url(#${clipId})` });
    const add = (tag, attrs) => { const n = document.createElementNS(NS, tag); Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); g.appendChild(n); return n; };
    const text = (x, y, s, color, anchor, size) => { const t = add("text", { x, y, fill: color, "font-size": size || 10, "font-family": "Inter, sans-serif", "font-weight": 600, "text-anchor": anchor || "start" }); t.textContent = s; return t; };
    const ctx = {
      width, height,
      polyline(vals, color, lw) {
        let d = "";
        for (let i = 0; i < vals.length; i++) { const v = vals[i]; if (v == null) continue; const x = X(bars[i].time), y = Y(v); if (!ok(x) || !ok(y)) continue; d += (d ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1); }
        if (d) add("path", { d, fill: "none", stroke: color, "stroke-width": lw || 1, "stroke-linejoin": "round" });
      },
      polylinePivots(zz, color) {
        let d = "";
        zz.forEach((p) => { const x = X(bars[p.i].time), y = Y(p.price); if (ok(x) && ok(y)) d += (d ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1); });
        if (d) add("path", { d, fill: "none", stroke: color, "stroke-width": 1.2, "stroke-dasharray": "5 4" });
      },
      fillBand(top, bot, fill) {
        const up = [], dn = [];
        for (let i = 0; i < top.length; i++) {
          if (top[i] == null || bot[i] == null) continue;
          const x = X(bars[i].time), a = Y(top[i]), b = Y(bot[i]);
          if (!ok(x) || !ok(a) || !ok(b)) continue;
          up.push(`${x.toFixed(1)} ${a.toFixed(1)}`); dn.push(`${x.toFixed(1)} ${b.toFixed(1)}`);
        }
        if (up.length > 1) add("path", { d: "M" + up.join("L") + "L" + dn.reverse().join("L") + "Z", fill, stroke: "none" });
      },
      hline(price, color, label, bold, dashed) {
        const y = Y(price);
        if (!ok(y)) return;
        add("line", { x1: 0, x2: width, y1: y, y2: y, stroke: color, "stroke-width": bold ? 1.6 : 1.1, "stroke-dasharray": dashed ? "6 4" : "8 4" });
        if (label) text(width - 6, y - 4, label, color, "end", 10);
      },
      hBar(price, half, w, fill) {
        const a = Y(price + half), b = Y(price - half);
        if (!ok(a) || !ok(b)) return;
        add("rect", { x: width - w, y: Math.min(a, b) + 0.5, width: w, height: Math.max(1.5, Math.abs(b - a) - 1), fill, rx: 1 });
      },
      box(t0, t1, top, bottom, fill, stroke, label) {
        const x0 = X(t0), x1 = t1 == null ? width : X(t1), y0 = Y(top), y1 = Y(bottom);
        if (!ok(x0) || !ok(y0) || !ok(y1)) return;
        const xe = ok(x1) && x1 > x0 ? x1 : width;
        add("rect", { x: x0.toFixed(1), y: Math.min(y0, y1).toFixed(1), width: Math.max(1, xe - x0).toFixed(1), height: Math.max(2, Math.abs(y1 - y0)).toFixed(1),
                      fill, stroke, "stroke-width": 1, "stroke-dasharray": "3 3" });
        if (label) text(x0 + 3, Math.min(y0, y1) - 3, label, stroke, "start", 8);
      },
      marker(i, price, label, color) {
        const x = X(bars[i].time), y = Y(price);
        if (!ok(x) || !ok(y)) return;
        add("circle", { cx: x.toFixed(1), cy: y.toFixed(1), r: 2.4, fill: color });
        text(x + 5, y - 5, label, color, "start", 9);
      },
    };
    keys.forEach((k) => {
      try { IND[k].svg(bars, ctx, paramsFor(k), S.indMeta[k] && S.indMeta[k].computed); } catch (e) { /* one indicator never breaks the chart */ }
    });
  }

  // ================================================================== drawing tools (same tools and storage as the Pro terminal)
  const chartEl = $("ch-chart"), drawSvg = $("draw-svg"), toolsEl = $("ch-tools");
  let drawingIdSeq = 0, selectedId = null, dragging = false, dragLast = null, activeTool = "cursor";
  let drawings = [], pendingPoints = [], scopeKey = null, brushing = false, brushPts = [];
  let undoStack = [], redoStack = [], undoBtn = null, redoBtn = null;
  const HIT = 10;
  const FIB = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  const FIB_EXT = [0, 0.382, 0.618, 1, 1.272, 1.618, 2, 2.618];
  const FIB_SEQ = [1, 2, 3, 5, 8, 13, 21, 34, 55];
  const ARITY = {
    cursor: 0, brush: 0, path: 0, horizontal: 1, vertical: 1, hray: 1, crossline: 1, text: 1, note: 1, icon: 1,
    trendline: 2, ray: 2, extended: 2, trendangle: 2, rectangle: 2, ellipse: 2, arrow: 2, measure: 2, fib: 2, fibtimezone: 2, fibfan: 2,
    fibcircles: 2, fibspiral: 2, fibarcs: 2, gannbox: 2, longpos: 2, shortpos: 2, pricerange: 2, daterange: 2, callout: 2,
    support_zone: 2, resistance_zone: 2, fibext: 3, fibchannel: 3, fibwedge: 3, pitchfork: 3, triangle: 3,
    // added in update 12
    highlighter: 0, avwap: 1, arrowup: 1, arrowdown: 1, pricelabel: 1, parallel: 3, regression: 2, infoline: 2,
    datepricerange: 2, circle: 2, cyclic: 2, abcd: 4, xabcd: 5, elliott: 6,
  };
  const FREEHAND = new Set(["brush", "highlighter"]);           // drag to draw
  const MULTI = new Set(["path", "abcd", "xabcd", "elliott"]);   // stored as a list of points
  const ic = (inner) => `<svg width="17" height="17" viewBox="0 0 24 24" fill="none">${inner}</svg>`;
  const ICON = {
    cursor: ic('<path d="M5 3l14 7-6 2-2 6-6-15z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>'),
    trendline: ic('<circle cx="5" cy="19" r="2" fill="currentColor"/><circle cx="19" cy="5" r="2" fill="currentColor"/><line x1="6.5" y1="17.5" x2="17.5" y2="6.5" stroke="currentColor" stroke-width="1.8"/>'),
    ray: ic('<circle cx="4" cy="20" r="2" fill="currentColor"/><line x1="5.5" y1="18.5" x2="19" y2="5" stroke="currentColor" stroke-width="1.8"/><path d="M13 5h6v6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'),
    hray: ic('<circle cx="4" cy="12" r="2" fill="currentColor"/><line x1="6" y1="12" x2="21" y2="12" stroke="currentColor" stroke-width="1.8"/>'),
    extended: ic('<circle cx="5" cy="19" r="1.6" fill="currentColor"/><circle cx="19" cy="5" r="1.6" fill="currentColor"/><line x1="2" y1="22" x2="22" y2="2" stroke="currentColor" stroke-width="1.6"/>'),
    trendangle: ic('<line x1="4" y1="20" x2="20" y2="6" stroke="currentColor" stroke-width="1.8"/><path d="M4 20h9" stroke="currentColor" stroke-width="1.2" stroke-dasharray="2 2"/><path d="M9 20a5 5 0 0 1 2-4" stroke="currentColor" stroke-width="1.2" fill="none"/>'),
    horizontal: ic('<line x1="3" y1="12" x2="21" y2="12" stroke="currentColor" stroke-width="2" stroke-dasharray="3 2.5"/>'),
    vertical: ic('<line x1="12" y1="3" x2="12" y2="21" stroke="currentColor" stroke-width="2" stroke-dasharray="3 2.5"/>'),
    crossline: ic('<line x1="12" y1="3" x2="12" y2="21" stroke="currentColor" stroke-width="1.6"/><line x1="3" y1="12" x2="21" y2="12" stroke="currentColor" stroke-width="1.6"/>'),
    fib: ic('<line x1="3" y1="5" x2="21" y2="5" stroke="currentColor" stroke-width="1.4"/><line x1="3" y1="10.3" x2="17" y2="10.3" stroke="currentColor" stroke-width="1.4"/><line x1="3" y1="14.3" x2="21" y2="14.3" stroke="currentColor" stroke-width="1.4"/><line x1="3" y1="19" x2="14" y2="19" stroke="currentColor" stroke-width="1.4"/>'),
    fibext: ic('<line x1="3" y1="6" x2="12" y2="6" stroke="currentColor" stroke-width="1.4"/><line x1="3" y1="11" x2="16" y2="11" stroke="currentColor" stroke-width="1.4"/><line x1="3" y1="16" x2="20" y2="16" stroke="currentColor" stroke-width="1.4"/><path d="M4 4l16 16" stroke="currentColor" stroke-width="1" stroke-dasharray="2 2"/>'),
    fibchannel: ic('<line x1="3" y1="18" x2="17" y2="4" stroke="currentColor" stroke-width="1.6"/><line x1="7" y1="20" x2="21" y2="6" stroke="currentColor" stroke-width="1.6"/>'),
    fibtimezone: ic('<line x1="4" y1="3" x2="4" y2="21" stroke="currentColor" stroke-width="1.4"/><line x1="9" y1="3" x2="9" y2="21" stroke="currentColor" stroke-width="1.4"/><line x1="15" y1="3" x2="15" y2="21" stroke="currentColor" stroke-width="1.4"/><line x1="21" y1="3" x2="21" y2="21" stroke="currentColor" stroke-width="1.4"/>'),
    fibfan: ic('<path d="M4 20L20 4M4 20L20 11M4 20L20 16.5" stroke="currentColor" stroke-width="1.4"/><circle cx="4" cy="20" r="1.6" fill="currentColor"/>'),
    fibcircles: ic('<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.3"/><circle cx="12" cy="12" r="5.5" stroke="currentColor" stroke-width="1.3"/><circle cx="12" cy="12" r="2" stroke="currentColor" stroke-width="1.3"/>'),
    fibspiral: ic('<path d="M12 12c3 0 4-2 3-4s-4-2-5 1 1 6 5 5 6-5 3-9-9-4-11 2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>'),
    fibarcs: ic('<path d="M3 20a17 17 0 0 1 17-17" stroke="currentColor" stroke-width="1.3"/><path d="M3 20a11 11 0 0 1 11-11" stroke="currentColor" stroke-width="1.3"/><path d="M3 20a5.5 5.5 0 0 1 5.5-5.5" stroke="currentColor" stroke-width="1.3"/>'),
    fibwedge: ic('<path d="M3 20L20 5M3 20L20 15" stroke="currentColor" stroke-width="1.6"/><line x1="12.5" y1="13.5" x2="15" y2="16.7" stroke="currentColor" stroke-width="1.1" stroke-dasharray="2 2"/>'),
    pitchfork: ic('<path d="M4 20L14 4M4 20L20 8M4 20L20 14" stroke="currentColor" stroke-width="1.5"/>'),
    gannbox: ic('<rect x="4" y="4" width="16" height="16" stroke="currentColor" stroke-width="1.4"/><path d="M4 10.7h16M4 17.3h16M10.7 4v16M17.3 4v16M4 4l16 16" stroke="currentColor" stroke-width="0.9"/>'),
    rectangle: ic('<rect x="4" y="6" width="16" height="12" rx="1.5" stroke="currentColor" stroke-width="1.6"/>'),
    support_zone: ic('<rect x="4" y="10" width="16" height="7" rx="1" fill="rgba(54,224,160,0.25)" stroke="#36e0a0" stroke-width="1.4"/><line x1="3" y1="17" x2="21" y2="17" stroke="#36e0a0" stroke-width="1.4"/>'),
    resistance_zone: ic('<rect x="4" y="7" width="16" height="7" rx="1" fill="rgba(255,82,107,0.25)" stroke="#ff526b" stroke-width="1.4"/><line x1="3" y1="7" x2="21" y2="7" stroke="#ff526b" stroke-width="1.4"/>'),
    ellipse: ic('<ellipse cx="12" cy="12" rx="9" ry="6.5" stroke="currentColor" stroke-width="1.6"/>'),
    triangle: ic('<path d="M12 4l9 16H3z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>'),
    arrow: ic('<line x1="4" y1="20" x2="18" y2="6" stroke="currentColor" stroke-width="1.8"/><path d="M11 6h7v7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'),
    path: ic('<path d="M4 18l6-10 5 6 5-10" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linejoin="round"/><circle cx="4" cy="18" r="1.4" fill="currentColor"/><circle cx="10" cy="8" r="1.4" fill="currentColor"/><circle cx="15" cy="14" r="1.4" fill="currentColor"/><circle cx="20" cy="4" r="1.4" fill="currentColor"/>'),
    measure: ic('<rect x="3" y="9" width="18" height="6" rx="1" stroke="currentColor" stroke-width="1.6"/><path d="M7 9v2.5M11 9v2.5M15 9v2.5" stroke="currentColor" stroke-width="1.6"/>'),
    longpos: ic('<path d="M4 9h16v6H4z" fill="rgba(54,224,160,0.25)" stroke="#36e0a0" stroke-width="1.3"/><path d="M8 17l4-4 4 4" stroke="#36e0a0" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'),
    shortpos: ic('<path d="M4 9h16v6H4z" fill="rgba(255,82,107,0.25)" stroke="#ff526b" stroke-width="1.3"/><path d="M8 7l4 4 4-4" stroke="#ff526b" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'),
    pricerange: ic('<line x1="12" y1="3" x2="12" y2="21" stroke="currentColor" stroke-width="1.6"/><path d="M8 6l4-3 4 3M8 18l4 3 4-3" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'),
    daterange: ic('<line x1="3" y1="12" x2="21" y2="12" stroke="currentColor" stroke-width="1.6"/><path d="M6 8l-3 4 3 4M18 8l3 4-3 4" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'),
    brush: ic('<path d="M4 20l4-1 10-10-3-3L5 16l-1 4z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>'),
    text: ic('<path d="M5 5h14M12 5v14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'),
    note: ic('<path d="M5 4h14v13l-4 3H5z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M7 9h10M7 13h6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>'),
    callout: ic('<path d="M4 5h16v9H10l-4 4v-4H4z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>'),
    icon: ic('<circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="1.6"/><circle cx="9" cy="10" r="1.1" fill="currentColor"/><circle cx="15" cy="10" r="1.1" fill="currentColor"/><path d="M8.5 14.5c1 1.4 5.9 1.4 7 0" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round"/>'),
    parallel: ic('<line x1="3" y1="15" x2="17" y2="5" stroke="currentColor" stroke-width="1.6"/><line x1="7" y1="20" x2="21" y2="10" stroke="currentColor" stroke-width="1.6"/><circle cx="3" cy="15" r="1.4" fill="currentColor"/><circle cx="17" cy="5" r="1.4" fill="currentColor"/>'),
    regression: ic('<path d="M3 17L21 7" stroke="currentColor" stroke-width="1.7"/><path d="M3 12L21 2M3 22L21 12" stroke="currentColor" stroke-width="1" stroke-dasharray="2 2"/>'),
    infoline: ic('<line x1="4" y1="19" x2="16" y2="7" stroke="currentColor" stroke-width="1.7"/><rect x="13" y="13" width="8" height="6" rx="1.2" stroke="currentColor" stroke-width="1.2"/><circle cx="4" cy="19" r="1.5" fill="currentColor"/><circle cx="16" cy="7" r="1.5" fill="currentColor"/>'),
    avwap: ic('<path d="M3 16c3-1 4-6 7-6s4 4 7 3 3-5 4-6" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round"/><path d="M3 21V9" stroke="currentColor" stroke-width="1.4"/><path d="M1.5 9h3" stroke="currentColor" stroke-width="1.4"/>'),
    datepricerange: ic('<rect x="4" y="5" width="16" height="14" rx="1.5" stroke="currentColor" stroke-width="1.4" stroke-dasharray="3 2"/><path d="M12 8v8M8 12h8" stroke="currentColor" stroke-width="1.4"/>'),
    circle: ic('<circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="1.6"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/>'),
    cyclic: ic('<path d="M4 3v18M9 3v18M14 3v18M19 3v18" stroke="currentColor" stroke-width="1.4"/><path d="M4 7h5" stroke="currentColor" stroke-width="1.2" stroke-dasharray="2 1.5"/>'),
    highlighter: ic('<path d="M5 19l3-1 9-9-2-2-9 9-1 3z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M3 21h9" stroke="currentColor" stroke-width="3" stroke-linecap="round" opacity="0.45"/>'),
    arrowup: ic('<path d="M12 4l6 7h-4v8h-4v-8H6z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>'),
    arrowdown: ic('<path d="M12 20l6-7h-4V5h-4v8H6z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>'),
    pricelabel: ic('<path d="M3 12l5-6h13v12H8z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><circle cx="8" cy="12" r="1.3" fill="currentColor"/>'),
    abcd: ic('<path d="M3 18L9 7l5 7 7-10" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/><path d="M3 18L14 14M9 7L21 4" stroke="currentColor" stroke-width="0.9" stroke-dasharray="2 2"/>'),
    xabcd: ic('<path d="M2 14L7 5l4 10 5-7 6 11" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linejoin="round"/><path d="M2 14L11 15L7 5M11 15L22 19" stroke="currentColor" stroke-width="0.9" stroke-dasharray="2 2" fill="none"/>'),
    elliott: ic('<path d="M2 19l4-8 3 4 5-11 3 6 5-7" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/>'),
    clear: ic('<path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m-9 0 1 12a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-12" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>'),
    undo: ic('<path d="M7 8H4V5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 8c2-3 5.5-4.5 9-3.5A8 8 0 1 1 5 18" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round"/>'),
    redo: ic('<path d="M17 8h3V5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M20 8c-2-3-5.5-4.5-9-3.5A8 8 0 1 0 19 18" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round"/>'),
  };
  const TOOL_GROUPS = [
    { id: "cursor", tools: [["cursor", "Cursor (select and move)"]] },
    { id: "lines", tools: [["trendline", "Trend line"], ["ray", "Ray"], ["hray", "Horizontal ray"], ["extended", "Extended line"], ["trendangle", "Trend angle"],
                           ["infoline", "Info line"], ["horizontal", "Horizontal line"], ["vertical", "Vertical line"], ["crossline", "Cross line"],
                           ["parallel", "Parallel channel", "CHANNELS"], ["regression", "Regression trend"], ["avwap", "Anchored VWAP", "VOLUME"]] },
    { id: "fib", tools: [["fib", "Fib retracement"], ["fibext", "Trend-based fib extension"], ["fibchannel", "Fib channel"], ["fibtimezone", "Fib time zone"],
                         ["fibfan", "Fib speed resistance fan"], ["fibcircles", "Fib circles"], ["fibspiral", "Fib spiral"], ["fibarcs", "Fib speed resistance arcs"],
                         ["fibwedge", "Fib wedge"], ["pitchfork", "Pitchfan"], ["gannbox", "Gann box", "GANN"], ["cyclic", "Cyclic lines"]] },
    { id: "patterns", tools: [["xabcd", "XABCD pattern"], ["abcd", "ABCD pattern"], ["elliott", "Elliott impulse wave (12345)"]] },
    { id: "shapes", tools: [["rectangle", "Rectangle"], ["ellipse", "Ellipse"], ["circle", "Circle"], ["triangle", "Triangle"], ["arrow", "Arrow"], ["path", "Path (double-click to finish)"],
                            ["support_zone", "Support zone"], ["resistance_zone", "Resistance zone"]] },
    { id: "measure", tools: [["measure", "Measure"], ["longpos", "Long position"], ["shortpos", "Short position"], ["pricerange", "Price range"], ["daterange", "Date range"],
                             ["datepricerange", "Date and price range"]] },
    { id: "brush", tools: [["brush", "Brush (freehand)"], ["highlighter", "Highlighter"]] },
    { id: "text", tools: [["text", "Text"], ["note", "Note"], ["callout", "Callout"], ["pricelabel", "Price label"]] },
    { id: "icons", tools: [["icon", "Icon (emoji)"], ["arrowup", "Arrow mark up"], ["arrowdown", "Arrow mark down"]] },
  ];
  const groupOf = {}, labelOf = {}, groupCurrent = {};
  TOOL_GROUPS.forEach((g) => { g.tools.forEach(([id, label]) => { groupOf[id] = g.id; labelOf[id] = label; }); groupCurrent[g.id] = g.tools[0][0]; });
  const COLORS = ["#4dabf7", "#36e0a0", "#ff526b", "#f5a623", "#c084fc", "#e8f1ec"];

  function renderToolbar() {
    toolsEl.innerHTML = "";
    TOOL_GROUPS.forEach((g) => {
      const wrap = document.createElement("div");
      wrap.className = "ch-tg"; wrap.dataset.group = g.id;
      const cur = groupCurrent[g.id];
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "ch-tb"; btn.title = labelOf[cur]; btn.setAttribute("aria-label", labelOf[cur]); btn.innerHTML = ICON[cur];
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const again = activeTool === groupCurrent[g.id] && g.tools.length > 1 && !wrap.classList.contains("open");
        closeFlyouts();
        selectTool(groupCurrent[g.id]);
        if (again) openFlyout(wrap);
      });
      wrap.appendChild(btn);
      if (g.tools.length > 1) {
        const arrow = document.createElement("span");
        arrow.className = "ch-tarrow"; arrow.title = "More tools"; arrow.setAttribute("role", "button"); arrow.setAttribute("aria-label", "More tools");
        arrow.innerHTML = '<svg width="7" height="7" viewBox="0 0 24 24"><path d="M4 4l16 8-16 8z" fill="currentColor"/></svg>';
        arrow.addEventListener("click", (e) => { e.stopPropagation(); const open = wrap.classList.contains("open"); closeFlyouts(); if (!open) openFlyout(wrap); });
        wrap.appendChild(arrow);
        const fly = document.createElement("div");
        fly.className = "ch-fly";
        g.tools.forEach(([id, label, section]) => {
          if (section) { const s = document.createElement("div"); s.className = "ch-fly-sec"; s.textContent = section; fly.appendChild(s); }
          const row = document.createElement("button");
          row.type = "button"; row.className = "ch-fly-item" + (id === activeTool ? " active" : "");
          row.innerHTML = `${ICON[id]}<span>${label}</span>`;
          row.addEventListener("click", (e) => { e.stopPropagation(); groupCurrent[g.id] = id; closeFlyouts(); renderToolbar(); selectTool(id); });
          fly.appendChild(row);
        });
        wrap.appendChild(fly);
        btn.addEventListener("contextmenu", (e) => { e.preventDefault(); closeFlyouts(); openFlyout(wrap); });
        // long press (touch) or a second click on the tool that is already active also opens the list
        let pressT = null;
        btn.addEventListener("touchstart", () => { pressT = setTimeout(() => { pressT = null; closeFlyouts(); openFlyout(wrap); }, 450); }, { passive: true });
        ["touchend", "touchmove", "touchcancel"].forEach((ev) => btn.addEventListener(ev, () => { clearTimeout(pressT); pressT = null; }, { passive: true }));
      }
      toolsEl.appendChild(wrap);
    });
    const sep = () => { const s = document.createElement("span"); s.className = "ch-tsep"; toolsEl.appendChild(s); };
    const util = (icon, title, fn, cls) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "ch-tb" + (cls ? " " + cls : ""); b.title = title; b.setAttribute("aria-label", title); b.innerHTML = ICON[icon];
      b.addEventListener("click", fn); toolsEl.appendChild(b); return b;
    };
    sep();
    undoBtn = util("undo", "Undo (Ctrl+Z)", undo);
    redoBtn = util("redo", "Redo (Ctrl+Y)", redo);
    sep();
    util("clear", "Delete all drawings on this chart", clearAll, "danger");
    updateUndoButtons();
    highlightTool();
  }
  function closeFlyouts() { toolsEl.querySelectorAll(".ch-tg.open").forEach((g) => g.classList.remove("open")); }
  // The flyout is position:fixed (the tool column scrolls and would clip it). On wide screens it sits to the right
  // of its button, kept inside the window; on phones the CSS turns it into a sheet above the bottom tabs.
  function openFlyout(wrap) {
    const fly = wrap.querySelector(".ch-fly");
    if (!fly) return;
    wrap.classList.add("open");
    if (phone()) { fly.style.left = fly.style.top = ""; return; }
    const r = wrap.getBoundingClientRect(), h = fly.offsetHeight, w = fly.offsetWidth;
    const left = Math.min(r.right + 8, window.innerWidth - w - 8);
    const top = Math.max(8, Math.min(r.top - 4, window.innerHeight - h - 8));
    fly.style.left = `${Math.max(8, left)}px`; fly.style.top = `${top}px`;
  }
  document.addEventListener("click", (e) => { if (!toolsEl.contains(e.target)) closeFlyouts(); });
  toolsEl.addEventListener("scroll", closeFlyouts, { passive: true });
  window.addEventListener("resize", closeFlyouts);
  function highlightTool() {
    toolsEl.querySelectorAll(".ch-tb").forEach((b) => b.classList.remove("active"));
    const g = groupOf[activeTool];
    const b = g ? toolsEl.querySelector(`.ch-tg[data-group="${g}"] .ch-tb`) : null;
    if (b) b.classList.add("active");
  }
  function selectTool(tool) {
    activeTool = tool; pendingPoints = []; selectedId = null; dragging = false;
    chartEl.style.cursor = tool === "cursor" ? "default" : "crosshair";
    setInteractions(!FREEHAND.has(tool));
    highlightTool(); renderDrawings();
  }
  function setInteractions(on) { if (S.chart) S.chart.applyOptions({ handleScroll: on, handleScale: on }); }

  // ---- persistence (per user + coin + timeframe; shared with the Pro terminal's chart)
  const coordsOnly = (d) => { const { id, serverId, _resave, _style, ...c } = d; return c; };
  async function saveCreate(d) {
    try {
      const r = await F.sendJSON("/api/chart/drawings", "POST", { symbol: S.coin, timeframe: S.tf, type: d.type, data: coordsOnly(d), style: d._style || undefined });
      if (!r._ok) throw new Error(r.error);
      d.serverId = r.id;
      if (d._resave) { d._resave = false; saveUpdate(d); }
    } catch (e) { setDrawStatus("Drawing kept on this page, but it could not be saved."); }
  }
  async function saveUpdate(d) {
    if (!d) return;
    if (!d.serverId) { d._resave = true; return; }
    try {
      const r = await F.sendJSON(`/api/chart/drawings/${d.serverId}`, "PATCH", { data: coordsOnly(d), style: d._style || undefined });
      if (!r._ok) throw new Error(r.error);
    } catch (e) { setDrawStatus("A drawing change could not be saved."); }
  }
  async function deleteOnServer(d) {
    if (!d || !d.serverId) return;
    try { await F.sendJSON(`/api/chart/drawings/${d.serverId}`, "DELETE"); } catch (e) {}
  }
  function setDrawStatus(msg) { toast(msg); }
  function clearDrawingsLocal() { drawings = []; pendingPoints = []; selectedId = null; scopeKey = null; undoStack = []; redoStack = []; updateUndoButtons(); renderDrawings(); }
  async function loadDrawings() {
    const key = `${S.coin}|${S.tf}`;
    if (scopeKey === key) { renderDrawings(); return; }
    scopeKey = key;
    drawings = []; pendingPoints = []; selectedId = null; renderDrawings();
    try {
      const d = await F.getJSON(`/api/chart/drawings?symbol=${enc(S.coin)}&timeframe=${enc(S.tf)}`);
      if (scopeKey !== key || d.error) return;
      drawings = (d.drawings || []).map((r) => { const { id, type, ...c } = r; return { ...c, type, id: ++drawingIdSeq, serverId: id }; });
      renderDrawings();
    } catch (e) {}
  }
  function clearAll() {
    if (!drawings.length) return;
    if (!window.confirm("Delete all drawings on this chart?")) return;
    drawings = []; pendingPoints = []; selectedId = null; renderDrawings();
    F.sendJSON(`/api/chart/drawings?symbol=${enc(S.coin)}&timeframe=${enc(S.tf)}`, "DELETE").catch(() => {});
  }

  // ---- undo / redo (create, delete, move, restyle)
  const clone = (d) => JSON.parse(JSON.stringify(d));
  function pushUndo(entry) { undoStack.push(entry); if (undoStack.length > 50) undoStack.shift(); redoStack = []; updateUndoButtons(); }
  function applyEntry(e) {
    if (e.action === "create") {
      const d = drawings.find((x) => x.id === e.id);
      drawings = drawings.filter((x) => x.id !== e.id); selectedId = null; renderDrawings(); deleteOnServer(d);
      return { action: "delete", drawing: d ? clone(d) : e.drawing };
    }
    if (e.action === "delete") {
      const d = clone(e.drawing); delete d.serverId; delete d._resave;
      drawings.push(d); renderDrawings(); saveCreate(d);
      return { action: "create", id: d.id, drawing: clone(d) };
    }
    if (e.action === "move") {
      const d = drawings.find((x) => x.id === e.id);
      if (!d) return null;
      const cur = clone(d); Object.assign(d, e.before); renderDrawings(); saveUpdate(d);
      return { action: "move", id: d.id, before: cur };
    }
    return null;
  }
  function undo() { const e = undoStack.pop(); if (!e) return; const inv = applyEntry(e); if (inv) redoStack.push(inv); updateUndoButtons(); }
  function redo() { const e = redoStack.pop(); if (!e) return; const inv = applyEntry(e); if (inv) undoStack.push(inv); updateUndoButtons(); }
  function updateUndoButtons() { if (undoBtn) undoBtn.disabled = !undoStack.length; if (redoBtn) redoBtn.disabled = !redoStack.length; }

  // ---- text prompt (notes, callouts, icons)
  function askText(title, def) {
    return new Promise((resolve) => {
      const box = $("ask"), inp = $("ask-input"), form = $("ask-form");
      $("ask-title").textContent = title; inp.value = def || ""; box.hidden = false;
      setTimeout(() => inp.focus(), 20);
      const done = (v) => { box.hidden = true; form.onsubmit = null; $("ask-cancel").onclick = null; box.onclick = null; resolve(v); };
      form.onsubmit = (e) => { e.preventDefault(); done(inp.value.trim() || null); };
      $("ask-cancel").onclick = () => done(null);
      box.onclick = (e) => { if (e.target === box) done(null); };
      inp.onkeydown = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(null); } };
    });
  }

  // ---- coordinates + hit testing
  const tX = (t) => (S.chart ? S.chart.timeScale().timeToCoordinate(t) : null);
  const pY = (p) => (S.main ? S.main.priceToCoordinate(p) : null);
  const shiftT = (t, dt) => (typeof t === "number" && typeof dt === "number" ? t + dt : t);
  function segDist(px, py, x1, y1, x2, y2) {
    const dx = x2 - x1, dy = y2 - y1, l = dx * dx + dy * dy;
    let t = l ? ((px - x1) * dx + (py - y1) * dy) / l : 0;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  }
  function bboxHit(x, y, P, pad) {
    const xs = P.map((p) => p && p.x).filter((v) => v != null && !Number.isNaN(v)), ys = P.map((p) => p && p.y).filter((v) => v != null && !Number.isNaN(v));
    if (!xs.length || !ys.length) return false;
    return x >= Math.min(...xs) - pad && x <= Math.max(...xs) + pad && y >= Math.min(...ys) - pad && y <= Math.max(...ys) + pad;
  }
  function lerpY(a, b, x) { return b.x === a.x ? a.y : a.y + ((x - a.x) / (b.x - a.x)) * (b.y - a.y); }
  function extendRay(a, b, width) {
    let ex = b.x, ey = b.y;
    const dx = b.x - a.x, dy = b.y - a.y;
    if (Math.abs(dx) > 0.0001) { const t = dx > 0 ? (width - a.x) / dx : (0 - a.x) / dx; ex = a.x + t * dx; ey = a.y + t * dy; }
    return { x: ex, y: ey };
  }
  function spiral(c, e) {
    const r0 = Math.max(6, Math.hypot(e.x - c.x, e.y - c.y)), base = Math.atan2(e.y - c.y, e.x - c.x), b = Math.log(1.6180339887) / (Math.PI / 2), out = [];
    for (let i = 0; i <= 80; i++) { const th = (i / 80) * Math.PI * 4, r = r0 * Math.exp(-b * th); out.push({ x: c.x + r * Math.cos(th + base), y: c.y + r * Math.sin(th + base) }); }
    return out;
  }
  // anchored VWAP from the anchor candle to the newest one (typical price x volume)
  function avwapLine(d) {
    const i0 = barAtOrBefore(d.time);
    if (i0 < 0) return [];
    const out = [];
    let pv = 0, vv = 0;
    for (let i = i0; i < S.bars.length; i++) {
      const b = S.bars[i], v = b.volume || 1, tp = (b.high + b.low + b.close) / 3;
      pv += tp * v; vv += v;
      const xx = tX(b.time), yy = pY(pv / vv);
      if (xx != null && yy != null) out.push({ x: xx, y: yy, v: pv / vv });
    }
    return out;
  }
  // linear regression of closes between the two anchors, with +/- 2 standard deviation lines
  function regressionGeom(d) {
    if (!d.p1 || !d.p2) return null;
    let i0 = barAtOrBefore(Math.min(d.p1.time, d.p2.time)), i1 = barAtOrBefore(Math.max(d.p1.time, d.p2.time));
    if (i0 < 0) i0 = 0;
    if (i1 - i0 < 2) return null;
    const n = i1 - i0 + 1;
    let sx = 0, sy = 0, sxy = 0, sxx = 0;
    for (let k = 0; k < n; k++) { const yv = S.bars[i0 + k].close; sx += k; sy += yv; sxy += k * yv; sxx += k * k; }
    const slope = (n * sxy - sx * sy) / (n * sxx - sx * sx || 1), icpt = (sy - slope * sx) / n;
    let ss = 0;
    for (let k = 0; k < n; k++) { const e = S.bars[i0 + k].close - (icpt + slope * k); ss += e * e; }
    const sd = Math.sqrt(ss / n), y0 = icpt, y1 = icpt + slope * (n - 1);
    const xa = tX(S.bars[i0].time), xb = tX(S.bars[i1].time);
    const P = (xx, v) => ({ x: xx, y: pY(v), v });
    if (xa == null || xb == null) return null;
    const g = { a: P(xa, y0), b: P(xb, y1), au: P(xa, y0 + 2 * sd), bu: P(xb, y1 + 2 * sd), al: P(xa, y0 - 2 * sd), bl: P(xb, y1 - 2 * sd), sd, slope, n };
    return [g.a, g.b, g.au, g.bu, g.al, g.bl].every((p) => p.y != null) ? g : null;
  }
  function hitTest(x, y) {
    if (!S.chart || !S.main) return null;
    const { w: width } = paneSize();
    const okv = (v) => v != null && !Number.isNaN(v);
    for (let i = drawings.length - 1; i >= 0; i--) {
      const d = drawings[i];
      const P1 = d.p1 ? { x: tX(d.p1.time), y: pY(d.p1.price) } : null, P2 = d.p2 ? { x: tX(d.p2.time), y: pY(d.p2.price) } : null, P3 = d.p3 ? { x: tX(d.p3.time), y: pY(d.p3.price) } : null;
      const ok2 = P1 && P2 && [P1.x, P1.y, P2.x, P2.y].every(okv);
      const t = d.type;
      if (t === "horizontal") { const y0 = pY(d.price); if (y0 != null && Math.abs(y - y0) <= HIT) return d; }
      else if (t === "vertical") { const x0 = tX(d.time); if (x0 != null && Math.abs(x - x0) <= HIT) return d; }
      else if (t === "hray") { const x0 = tX(d.time), y0 = pY(d.price); if (x0 != null && y0 != null && x >= x0 - HIT && Math.abs(y - y0) <= HIT) return d; }
      else if (t === "crossline") { const x0 = tX(d.time), y0 = pY(d.price); if ((x0 != null && Math.abs(x - x0) <= HIT) || (y0 != null && Math.abs(y - y0) <= HIT)) return d; }
      else if (["text", "note", "icon", "arrowup", "arrowdown", "pricelabel"].includes(t)) { const a = tX(d.time), b = pY(d.price); if (a != null && b != null && Math.hypot(x - a, y - b) <= HIT + 14) return d; }
      else if (t === "avwap") { const line = avwapLine(d); for (let j = 0; j < line.length - 1; j++) { const a = line[j], b = line[j + 1]; if (segDist(x, y, a.x, a.y, b.x, b.y) <= HIT) return d; } }
      else if (t === "regression") { const r = regressionGeom(d); if (r && [[r.a, r.b], [r.au, r.bu], [r.al, r.bl]].some(([a, b]) => segDist(x, y, a.x, a.y, b.x, b.y) <= HIT)) return d; }
      else if (t === "parallel") {
        if (!P1 || !P2 || !P3 || ![P1.x, P1.y, P2.x, P2.y, P3.x, P3.y].every(okv)) continue;
        const off = P3.y - lerpY(P1, P2, P3.x);
        if (segDist(x, y, P1.x, P1.y, P2.x, P2.y) <= HIT || segDist(x, y, P1.x, P1.y + off, P2.x, P2.y + off) <= HIT || bboxHit(x, y, [P1, P2, { x: P1.x, y: P1.y + off }, { x: P2.x, y: P2.y + off }], 0)) return d;
      } else if (t === "circle") { if (ok2) { const r = Math.hypot(P2.x - P1.x, P2.y - P1.y); if (Math.abs(Math.hypot(x - P1.x, y - P1.y) - r) <= HIT || Math.hypot(x - P1.x, y - P1.y) < r) return d; } }
      else if (t === "datepricerange") { if (ok2 && x >= Math.min(P1.x, P2.x) - HIT && x <= Math.max(P1.x, P2.x) + HIT && y >= Math.min(P1.y, P2.y) - HIT && y <= Math.max(P1.y, P2.y) + HIT) return d; }
      else if (t === "infoline") { if (ok2 && segDist(x, y, P1.x, P1.y, P2.x, P2.y) <= HIT) return d; }
      else if (t === "cyclic") {
        if (!ok2) continue;
        const unit = typeof d.p2.time === "number" && typeof d.p1.time === "number" ? Math.abs(d.p2.time - d.p1.time) : 0;
        if (!unit) continue;
        const t0 = Math.min(d.p1.time, d.p2.time);
        for (let k = 0; k < 400; k++) { const x0 = tX(t0 + k * unit); if (x0 == null) break; if (Math.abs(x - x0) <= HIT) return d; }
      }
      else if (Array.isArray(d.points)) {
        for (let j = 0; j < d.points.length - 1; j++) {
          const a = { x: tX(d.points[j].time), y: pY(d.points[j].price) }, b = { x: tX(d.points[j + 1].time), y: pY(d.points[j + 1].price) };
          if ([a.x, a.y, b.x, b.y].every((v) => v != null) && segDist(x, y, a.x, a.y, b.x, b.y) <= HIT) return d;
        }
      } else if (["rectangle", "gannbox", "longpos", "shortpos", "ellipse", "support_zone", "resistance_zone"].includes(t)) {
        if (ok2 && x >= Math.min(P1.x, P2.x) - HIT && x <= Math.max(P1.x, P2.x) + HIT && y >= Math.min(P1.y, P2.y) - HIT && y <= Math.max(P1.y, P2.y) + HIT) return d;
      } else if (t === "triangle") {
        if (!P1 || !P2 || !P3) continue;
        const ps = [P1, P2, P3];
        for (let j = 0; j < 3; j++) { const a = ps[j], b = ps[(j + 1) % 3]; if (a.x != null && b.x != null && segDist(x, y, a.x, a.y, b.x, b.y) <= HIT) return d; }
      } else if (t === "fib" || t === "pricerange") {
        if (!ok2) continue;
        for (const lv of FIB) { const py = pY(d.p1.price + (d.p2.price - d.p1.price) * lv); if (py != null && Math.abs(y - py) <= HIT && x >= Math.min(P1.x, P2.x) - HIT && x <= Math.max(P1.x, P2.x) + 60) return d; }
      } else if (t === "fibext") {
        if (!d.p3) continue;
        const x3 = tX(d.p3.time), ys = FIB_EXT.map((lv) => pY(d.p3.price + (d.p2.price - d.p1.price) * lv));
        if (bboxHit(x, y, [{ x: x3, y: ys[0] }, ...ys.map((py) => ({ x: width, y: py }))], HIT)) return d;
      } else if (t === "fibtimezone") {
        const unit = typeof d.p2.time === "number" && typeof d.p1.time === "number" ? d.p2.time - d.p1.time : null;
        if (unit == null) continue;
        for (const n of FIB_SEQ) { const x0 = tX(d.p1.time + n * unit); if (x0 != null && Math.abs(x - x0) <= HIT) return d; }
      } else if (t === "daterange") {
        if (ok2 && Math.abs(y - (P1.y + P2.y) / 2) <= HIT + 6 && x >= Math.min(P1.x, P2.x) - HIT && x <= Math.max(P1.x, P2.x) + HIT) return d;
      } else if (t === "fibfan") {
        if (!ok2) continue;
        const ext = [0.236, 0.382, 0.5, 0.618, 0.786].map((lv) => { const py = pY(d.p1.price + (d.p2.price - d.p1.price) * lv); return okv(py) ? extendRay(P1, { x: P2.x, y: py }, width) : null; }).filter(Boolean);
        if (bboxHit(x, y, [P1, P2, ...ext], HIT)) return d;
      } else if (t === "fibcircles" || t === "fibarcs") {
        if (!ok2) continue;
        const r = Math.hypot(P2.x - P1.x, P2.y - P1.y) * 2.618;
        if (bboxHit(x, y, [{ x: P1.x - r, y: P1.y - r }, { x: P1.x + r, y: P1.y + r }], HIT)) return d;
      } else if (t === "fibspiral") { if (ok2 && bboxHit(x, y, spiral(P1, P2), HIT)) return d; }
      else if (t === "fibwedge") { if (P1 && P2 && P3 && bboxHit(x, y, [P1, P2, P3], HIT)) return d; }
      else if (t === "pitchfork") {
        if (!P1 || !P2 || !P3) continue;
        const med = extendRay(P1, { x: (P2.x + P3.x) / 2, y: (P2.y + P3.y) / 2 }, width);
        if (bboxHit(x, y, [P1, P2, P3, med, { x: P2.x + (med.x - P1.x), y: P2.y + (med.y - P1.y) }, { x: P3.x + (med.x - P1.x), y: P3.y + (med.y - P1.y) }], HIT)) return d;
      } else if (t === "fibchannel") {
        if (!P1 || !P2 || !P3) continue;
        const off = P3.y - lerpY(P1, P2, P3.x);
        if (bboxHit(x, y, [P1, P2, { x: P1.x, y: P1.y + off }, { x: P2.x, y: P2.y + off }], HIT)) return d;
      } else if (["extended", "trendangle", "arrow", "trendline", "measure", "callout"].includes(t)) {
        if (ok2 && segDist(x, y, P1.x, P1.y, P2.x, P2.y) <= HIT) return d;
      } else if (t === "ray") {
        if (!ok2) continue;
        const e = extendRay(P1, P2, width);
        if (segDist(x, y, P1.x, P1.y, e.x, e.y) <= HIT) return d;
      }
    }
    return null;
  }
  function moveBy(d, dt, dp) {
    if (d.type === "horizontal") d.price += dp;
    else if (d.type === "vertical") d.time = shiftT(d.time, dt);
    else if (["hray", "crossline", "text", "note", "icon", "avwap", "arrowup", "arrowdown", "pricelabel"].includes(d.type)) { d.time = shiftT(d.time, dt); d.price += dp; }
    else if (Array.isArray(d.points)) d.points.forEach((p) => { p.time = shiftT(p.time, dt); p.price += dp; });
    else ["p1", "p2", "p3"].forEach((k) => { if (d[k]) { d[k].time = shiftT(d[k].time, dt); d[k].price += dp; } });
  }

  // ---- pointer handling: click-to-place, select/drag, brush, path
  async function onChartClick(p) {
    if (S.pickPrice && activeTool === "cursor" && p && p.point) {
      const price = S.main.coordinateToPrice(p.point.y);
      if (price != null) { $("alert-price").value = Number(price.toFixed(F.decimals(price, S.coin))); updateAlertHint(); }
      return;
    }
    if (!S.main || activeTool === "cursor" || FREEHAND.has(activeTool) || activeTool === "path") return;
    if (!p || !p.point || p.time === undefined) return;
    const price = S.main.coordinateToPrice(p.point.y);
    if (price == null) return;
    const pt = { time: p.time, price };
    const arity = ARITY[activeTool] || 2, tool = activeTool;
    if (arity === 1) {
      let made = null;
      if (tool === "text" || tool === "note") { const txt = await askText(tool === "note" ? "Note" : "Text", ""); if (txt) made = { type: tool, ...pt, text: txt.slice(0, 120) }; }
      else if (tool === "icon") { const em = await askText("Icon (an emoji, for example ⭐ 🚀 🔥 ⚠️ ✅)", "⭐"); if (em) made = { type: "icon", ...pt, emoji: em.slice(0, 4) }; }
      else if (tool === "pricelabel") made = { type: "pricelabel", ...pt };
      else if (tool === "horizontal") made = { type: "horizontal", price };
      else if (tool === "vertical") made = { type: "vertical", time: p.time };
      else made = { type: tool, ...pt };
      if (made) { made.id = ++drawingIdSeq; drawings.push(made); saveCreate(made); pushUndo({ action: "create", id: made.id }); }
      renderDrawings();
      return;
    }
    pendingPoints.push(pt);
    if (pendingPoints.length < arity) { renderDrawings(); return; }
    const d = MULTI.has(tool) ? { type: tool, id: ++drawingIdSeq, points: pendingPoints.slice() }
      : { type: tool, id: ++drawingIdSeq, p1: pendingPoints[0], p2: pendingPoints[1] };
    if (arity === 3) d.p3 = pendingPoints[2];
    if (tool === "callout") { const txt = await askText("Callout text", ""); d.text = (txt || "").slice(0, 120); }
    pendingPoints = [];
    drawings.push(d); saveCreate(d); pushUndo({ action: "create", id: d.id });
    renderDrawings();
  }
  function pixToTP(cx, cy) {
    const r = chartEl.getBoundingClientRect();
    return { time: S.chart ? S.chart.timeScale().coordinateToTime(cx - r.left) : null, price: S.main ? S.main.coordinateToPrice(cy - r.top) : null };
  }
  function pixXY(cx, cy) { const r = chartEl.getBoundingClientRect(); return { x: cx - r.left, y: cy - r.top }; }
  let dragBefore = null;
  function down(cx, cy) {
    if (!S.chart || !S.main) return;
    if (activeTool === "cursor") {
      const { x, y } = pixXY(cx, cy);
      const hit = hitTest(x, y);
      if (hit) { selectedId = hit.id; dragging = true; dragLast = pixToTP(cx, cy); dragBefore = clone(hit); setInteractions(false); }
      else if (selectedId != null) selectedId = null;
      renderDrawings();
      return;
    }
    if (!FREEHAND.has(activeTool)) return;
    brushing = true; brushPts = [];
    const pt = pixToTP(cx, cy);
    if (pt.time != null && pt.price != null) brushPts.push(pt);
  }
  function move(cx, cy) {
    if (dragging) {
      const pt = pixToTP(cx, cy);
      if (pt.time != null && pt.price != null && dragLast) {
        const dt = typeof pt.time === "number" && typeof dragLast.time === "number" ? pt.time - dragLast.time : 0;
        const d = drawings.find((x) => x.id === selectedId);
        if (d) moveBy(d, dt, pt.price - dragLast.price);
        dragLast = pt; renderDrawings();
      }
      return true;
    }
    if (!brushing) return false;
    const pt = pixToTP(cx, cy);
    if (pt.time != null && pt.price != null) { brushPts.push(pt); renderDrawings(null, brushPts); }
    return true;
  }
  function up() {
    if (dragging) {
      dragging = false; dragLast = null; setInteractions(!FREEHAND.has(activeTool));
      const d = drawings.find((x) => x.id === selectedId);
      if (d && dragBefore && JSON.stringify(coordsOnly(d)) !== JSON.stringify(coordsOnly(dragBefore))) {
        const { id, serverId, ...before } = dragBefore;
        pushUndo({ action: "move", id: d.id, before });
        saveUpdate(d);
      }
      dragBefore = null;
      renderDrawings();
      return;
    }
    if (!brushing) return;
    brushing = false;
    if (brushPts.length > 1) { const d = { type: FREEHAND.has(activeTool) ? activeTool : "brush", points: brushPts.slice(), id: ++drawingIdSeq }; drawings.push(d); saveCreate(d); pushUndo({ action: "create", id: d.id }); }
    brushPts = []; renderDrawings();
  }
  chartEl.addEventListener("mousedown", (e) => { if (activeTool === "cursor" || FREEHAND.has(activeTool)) down(e.clientX, e.clientY); });
  chartEl.addEventListener("touchstart", (e) => {
    if (activeTool !== "cursor" && !FREEHAND.has(activeTool)) return;
    const t = e.touches[0]; if (!t) return;
    down(t.clientX, t.clientY);
    if (dragging || brushing) e.preventDefault();
  }, { passive: false });
  chartEl.addEventListener("click", (e) => {
    if (activeTool !== "path") return;
    const pt = pixToTP(e.clientX, e.clientY);
    if (pt.time != null && pt.price != null) { pendingPoints.push(pt); renderDrawings(); }
  });
  chartEl.addEventListener("dblclick", (e) => {
    if (activeTool !== "path") return;
    e.preventDefault();
    if (pendingPoints.length > 1) { const d = { type: "path", points: pendingPoints.slice(), id: ++drawingIdSeq }; drawings.push(d); saveCreate(d); pushUndo({ action: "create", id: d.id }); }
    pendingPoints = []; renderDrawings();
  });
  window.addEventListener("mousemove", (e) => move(e.clientX, e.clientY));
  window.addEventListener("touchmove", (e) => { const t = e.touches[0]; if (t && move(t.clientX, t.clientY)) e.preventDefault(); }, { passive: false });
  window.addEventListener("mouseup", up);
  window.addEventListener("touchend", up);
  window.addEventListener("touchcancel", up);

  // ---- selected drawing bar: colour, line width, delete
  let selBar = null;
  function selectionBar(anchor, d) {
    if (!selBar) {
      selBar = document.createElement("div");
      selBar.className = "ch-sel";
      selBar.addEventListener("mousedown", (e) => e.stopPropagation());
      selBar.addEventListener("touchstart", (e) => e.stopPropagation(), { passive: true });
      selBar.addEventListener("click", (e) => {
        e.stopPropagation();
        const cur = drawings.find((x) => x.id === selectedId);
        if (!cur) return;
        const b = e.target.closest("button");
        if (!b) return;
        if (b.dataset.color) { cur._style = Object.assign({}, cur._style, { color: b.dataset.color }); saveUpdate(cur); }
        else if (b.dataset.w) { cur._style = Object.assign({}, cur._style, { width: Number(b.dataset.w) }); saveUpdate(cur); }
        else if (b.dataset.del) { removeSelected(); return; }
        renderDrawings();
      });
      $("ch-canvas").appendChild(selBar);
    }
    const st = d._style || {};
    selBar.innerHTML = COLORS.map((c) => `<button type="button" class="sw${st.color === c ? " on" : ""}" data-color="${c}" style="background:${c}" aria-label="Colour"></button>`).join("")
      + "<i></i>" + [1, 2, 3].map((w) => `<button type="button" class="w${(st.width || 1) === w ? " on" : ""}" data-w="${w}" aria-label="Line width ${w}">${w}</button>`).join("")
      + `<i></i><button type="button" class="del" data-del="1" aria-label="Delete this drawing" title="Delete (Del)">×</button>`;
    selBar.hidden = false;
    selBar.style.left = `${Math.max(90, Math.min(anchor.x, chartEl.clientWidth - 90))}px`;
    selBar.style.top = `${Math.max(46, anchor.y)}px`;
  }
  function hideSelBar() { if (selBar) selBar.hidden = true; }
  function removeSelected() {
    const d = drawings.find((x) => x.id === selectedId);
    drawings = drawings.filter((x) => x.id !== selectedId);
    selectedId = null; renderDrawings(); deleteOnServer(d);
    if (d) pushUndo({ action: "delete", drawing: clone(d) });
  }

  // ---- render all drawings
  function describeArc(cx, cy, r, a0, a1) {
    const rad = (d) => (d * Math.PI) / 180, s = { x: cx + r * Math.cos(rad(a0)), y: cy + r * Math.sin(rad(a0)) }, e = { x: cx + r * Math.cos(rad(a1)), y: cy + r * Math.sin(rad(a1)) };
    return `M ${s.x} ${s.y} A ${r} ${r} 0 ${a1 - a0 <= 180 ? 0 : 1} 1 ${e.x} ${e.y}`;
  }
  function renderDrawings(preview, liveBrush) {
    while (drawSvg.firstChild) drawSvg.removeChild(drawSvg.firstChild);
    if (!S.chart || !S.main) { hideSelBar(); return; }
    const { w: width, h: height } = paneSize();
    const NS = "http://www.w3.org/2000/svg";
    const okXY = (...v) => v.every((x) => x !== null && x !== undefined && !Number.isNaN(x));
    const el = (tag, attrs) => { const n = document.createElementNS(NS, tag); Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); drawSvg.appendChild(n); return n; };
    const font = { "font-family": "Inter, sans-serif" };
    let anchor = null;
    drawings.forEach((d) => {
      const sel = d.id === selectedId, st = d._style || {};
      const col = (def) => st.color || def;
      const sw = (base) => (base * (st.width || 1)) + (sel ? 1 : 0);
      const P1 = d.p1 ? { x: tX(d.p1.time), y: pY(d.p1.price) } : null, P2 = d.p2 ? { x: tX(d.p2.time), y: pY(d.p2.price) } : null, P3 = d.p3 ? { x: tX(d.p3.time), y: pY(d.p3.price) } : null;
      const ok2 = P1 && P2 && okXY(P1.x, P1.y, P2.x, P2.y), ok3 = ok2 && P3 && okXY(P3.x, P3.y);
      const t = d.type;
      if (t === "horizontal") {
        const y = pY(d.price); if (!okXY(y)) return;
        const c = col("#f5a623");
        el("line", { x1: 0, x2: width, y1: y, y2: y, stroke: c, "stroke-width": sw(1.4), "stroke-dasharray": "4 3" });
        el("text", { x: width - 8, y: y - 6, "text-anchor": "end", fill: c, "font-size": 10, "font-weight": 700, ...font }).textContent = fmt(d.price);
        if (sel) anchor = { x: width - 120, y: y - 8 };
        return;
      }
      if (t === "vertical") { const x = tX(d.time); if (!okXY(x)) return; el("line", { x1: x, x2: x, y1: 0, y2: height, stroke: col("#4dabf7"), "stroke-width": sw(1.4), "stroke-dasharray": "4 3" }); if (sel) anchor = { x, y: 50 }; return; }
      if (t === "hray") { const x = tX(d.time), y = pY(d.price); if (!okXY(x, y)) return; const c = col("#f5a623"); el("line", { x1: x, x2: width, y1: y, y2: y, stroke: c, "stroke-width": sw(1.4) }); el("circle", { cx: x, cy: y, r: 3, fill: c }); if (sel) anchor = { x, y }; return; }
      if (t === "crossline") {
        const x = tX(d.time), y = pY(d.price); if (!okXY(x, y)) return;
        const c = col("#c084fc");
        el("line", { x1: x, x2: x, y1: 0, y2: height, stroke: c, "stroke-width": sw(1.2), "stroke-dasharray": "4 3" });
        el("line", { x1: 0, x2: width, y1: y, y2: y, stroke: c, "stroke-width": sw(1.2), "stroke-dasharray": "4 3" });
        if (sel) anchor = { x, y }; return;
      }
      if (t === "text" || t === "note") {
        const x = tX(d.time), y = pY(d.price); if (!okXY(x, y)) return;
        const c = col("#f5a623");
        if (sel) el("circle", { cx: x, cy: y, r: 9, fill: "none", stroke: "#fff", "stroke-dasharray": "3 2", opacity: 0.8 });
        el("circle", { cx: x, cy: y, r: 2.5, fill: c });
        if (t === "note") {
          const w = Math.max(40, String(d.text || "").length * 6.4 + 14);
          el("rect", { x: x + 6, y: y - 26, width: w, height: 20, rx: 5, fill: "rgba(17,26,23,0.92)", stroke: c, "stroke-width": 1 });
          el("text", { x: x + 13, y: y - 12, fill: c, "font-size": 11, ...font }).textContent = d.text;
        } else el("text", { x: x + 8, y: y - 8, fill: c, "font-size": 12 + 2 * ((st.width || 1) - 1), "font-weight": 700, ...font }).textContent = d.text;
        if (sel) anchor = { x, y: y - 14 }; return;
      }
      if (t === "icon") { const x = tX(d.time), y = pY(d.price); if (!okXY(x, y)) return; el("text", { x, y: y + 6, "text-anchor": "middle", "font-size": 20 }).textContent = d.emoji; if (sel) anchor = { x, y: y - 16 }; return; }
      if (t === "brush" || t === "path") {
        const ps = d.points.map((p) => ({ x: tX(p.time), y: pY(p.price) })).filter((p) => okXY(p.x, p.y));
        if (ps.length < 2) return;
        const c = col(t === "brush" ? "#c084fc" : "#4dabf7");
        el("polyline", { points: ps.map((p) => `${p.x},${p.y}`).join(" "), fill: "none", stroke: c, "stroke-width": sw(2), "stroke-linejoin": "round", "stroke-linecap": "round" });
        if (t === "path") ps.forEach((p) => el("circle", { cx: p.x, cy: p.y, r: 2.5, fill: c }));
        if (sel) anchor = ps[0]; return;
      }
      if (t === "rectangle" || t === "ellipse") {
        if (!ok2) return;
        const x = Math.min(P1.x, P2.x), y = Math.min(P1.y, P2.y), w = Math.abs(P2.x - P1.x), h = Math.abs(P2.y - P1.y), c = col("#4dabf7");
        if (t === "rectangle") el("rect", { x, y, width: w, height: h, fill: alpha(c, 0.12), stroke: c, "stroke-width": sw(1.4) });
        else el("ellipse", { cx: x + w / 2, cy: y + h / 2, rx: w / 2, ry: h / 2, fill: alpha(c, 0.12), stroke: c, "stroke-width": sw(1.4) });
        if (sel) anchor = { x: x + w / 2, y }; return;
      }
      if (t === "support_zone" || t === "resistance_zone") {
        if (!ok2) return;
        const sup = t === "support_zone", c = col(sup ? "#36e0a0" : "#ff526b");
        const x = Math.min(P1.x, P2.x), y = Math.min(P1.y, P2.y), w = Math.abs(P2.x - P1.x), h = Math.abs(P2.y - P1.y);
        el("rect", { x, y, width: w, height: h, fill: alpha(c, 0.14), stroke: c, "stroke-width": sw(1.4) });
        el("text", { x: x + 6, y: y + 14, fill: c, "font-size": 10, "font-weight": 700, ...font }).textContent =
          `${sup ? "SUPPORT" : "RESISTANCE"}  ${fmt(Math.max(d.p1.price, d.p2.price))} – ${fmt(Math.min(d.p1.price, d.p2.price))}`;
        if (sel) anchor = { x: x + w / 2, y }; return;
      }
      if (t === "triangle") {
        const ps = [P1, P2, P3].filter(Boolean), c = col("#4dabf7");
        if (ps.length === 3 && okXY(P3.x, P3.y) && ok2) { el("polygon", { points: ps.map((p) => `${p.x},${p.y}`).join(" "), fill: alpha(c, 0.12), stroke: c, "stroke-width": sw(1.5) }); if (sel) anchor = P1; }
        return;
      }
      if (t === "arrow") {
        if (!ok2) return;
        const c = col("#36e0a0"), a = Math.atan2(P2.y - P1.y, P2.x - P1.x), ah = 9 + 2 * ((st.width || 1) - 1);
        el("line", { x1: P1.x, y1: P1.y, x2: P2.x, y2: P2.y, stroke: c, "stroke-width": sw(1.8) });
        el("polygon", { points: `${P2.x},${P2.y} ${P2.x - ah * Math.cos(a - 0.4)},${P2.y - ah * Math.sin(a - 0.4)} ${P2.x - ah * Math.cos(a + 0.4)},${P2.y - ah * Math.sin(a + 0.4)}`, fill: c });
        if (sel) anchor = P2; return;
      }
      if (["trendline", "extended", "trendangle", "measure", "callout"].includes(t)) {
        if (!ok2) return;
        let x1 = P1.x, y1 = P1.y, x2 = P2.x, y2 = P2.y;
        if (t === "extended") { const dx = P2.x - P1.x, dy = P2.y - P1.y; if (Math.abs(dx) > 0.0001) { x1 = 0; y1 = P1.y + ((0 - P1.x) / dx) * dy; x2 = width; y2 = P1.y + ((width - P1.x) / dx) * dy; } }
        const upMove = d.p2.price >= d.p1.price;
        const c = t === "measure" ? (upMove ? "#36e0a0" : "#ff526b") : col(t === "callout" ? "#f5a623" : "#4dabf7");
        el("line", { x1, y1, x2, y2, stroke: c, "stroke-width": sw(1.8) });
        if (t === "trendline" || t === "extended") { el("circle", { cx: P1.x, cy: P1.y, r: 2.6, fill: c }); el("circle", { cx: P2.x, cy: P2.y, r: 2.6, fill: c }); }
        if (t === "trendangle") el("text", { x: (P1.x + P2.x) / 2, y: (P1.y + P2.y) / 2 - 6, fill: c, "font-size": 10, ...font }).textContent = `${((Math.atan2(-(P2.y - P1.y), P2.x - P1.x) * 180) / Math.PI).toFixed(1)}°`;
        if (t === "measure") {
          const pc = ((d.p2.price - d.p1.price) / d.p1.price) * 100, bars = Math.round(Math.abs(d.p2.time - d.p1.time) / TF_SEC[S.tf]);
          const label = `${pc >= 0 ? "+" : ""}${pc.toFixed(2)}% · ${bars} bars`;
          const w = label.length * 6 + 14;
          el("rect", { x: (x1 + x2) / 2 - w / 2, y: (y1 + y2) / 2 - 20, width: w, height: 17, rx: 4, fill: c, opacity: 0.92 });
          el("text", { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 8, "text-anchor": "middle", fill: "#06110c", "font-size": 10, "font-weight": 700, ...font }).textContent = label;
        }
        if (t === "callout") {
          const w = Math.max(50, String(d.text || "").length * 6.4 + 14);
          el("rect", { x: P2.x, y: P2.y - 20, width: w, height: 20, rx: 5, fill: "rgba(17,26,23,0.92)", stroke: c, "stroke-width": 1 });
          el("text", { x: P2.x + 6, y: P2.y - 6, fill: c, "font-size": 11, ...font }).textContent = d.text || "";
        }
        if (sel) anchor = { x: (x1 + x2) / 2, y: Math.min(y1, y2) }; return;
      }
      if (t === "ray") {
        if (!ok2) return;
        const e = extendRay(P1, P2, width), c = col("#36e0a0");
        el("line", { x1: P1.x, y1: P1.y, x2: e.x, y2: e.y, stroke: c, "stroke-width": sw(1.8) });
        el("circle", { cx: P1.x, cy: P1.y, r: 3.5, fill: c });
        if (sel) anchor = P1; return;
      }
      if (t === "fib" || t === "pricerange") {
        if (!ok2) return;
        const c = col("#f5a623");
        FIB.forEach((lv) => {
          const pr = d.p1.price + (d.p2.price - d.p1.price) * lv, py = pY(pr);
          if (!okXY(py)) return;
          el("line", { x1: Math.min(P1.x, P2.x), x2: Math.max(P1.x, P2.x), y1: py, y2: py, stroke: c, "stroke-width": lv === 0 || lv === 1 ? sw(1.6) : sw(1), opacity: 0.85 });
          if (t === "fib") el("text", { x: Math.max(P1.x, P2.x) + 4, y: py + 3, fill: c, "font-size": 9.5, ...font }).textContent = `${(lv * 100).toFixed(1)}% · ${fmt(pr)}`;
        });
        if (t === "pricerange") el("text", { x: P2.x + 6, y: (P1.y + P2.y) / 2, fill: c, "font-size": 10, ...font }).textContent = `${fmt(Math.abs(d.p2.price - d.p1.price))} (${(((d.p2.price - d.p1.price) / d.p1.price) * 100).toFixed(2)}%)`;
        if (sel) anchor = { x: (P1.x + P2.x) / 2, y: Math.min(P1.y, P2.y) }; return;
      }
      if (t === "fibext") {
        if (!ok3) return;
        const c = col("#c084fc");
        FIB_EXT.forEach((lv) => {
          const py = pY(d.p3.price + (d.p2.price - d.p1.price) * lv);
          if (!okXY(py)) return;
          el("line", { x1: P3.x, x2: width, y1: py, y2: py, stroke: c, "stroke-width": lv === 1 ? sw(1.6) : sw(1), opacity: 0.85 });
          el("text", { x: width - 6, y: py - 3, "text-anchor": "end", fill: c, "font-size": 9.5, ...font }).textContent = `${(lv * 100).toFixed(1)}% · ${fmt(d.p3.price + (d.p2.price - d.p1.price) * lv)}`;
        });
        if (sel) anchor = P3; return;
      }
      if (t === "fibchannel") {
        if (!ok3) return;
        const off = P3.y - lerpY(P1, P2, P3.x), c = col("#4dabf7");
        FIB.forEach((lv) => el("line", { x1: P1.x, y1: P1.y + off * lv, x2: P2.x, y2: P2.y + off * lv, stroke: c, "stroke-width": lv === 0 || lv === 1 ? sw(1.6) : sw(1), opacity: 0.85 }));
        if (sel) anchor = P3; return;
      }
      if (t === "fibtimezone") {
        if (!ok2) return;
        const unit = typeof d.p2.time === "number" && typeof d.p1.time === "number" ? d.p2.time - d.p1.time : null;
        if (unit == null) return;
        const c = col("#4dabf7");
        FIB_SEQ.forEach((n) => { const x0 = tX(d.p1.time + n * unit); if (!okXY(x0)) return; el("line", { x1: x0, x2: x0, y1: 0, y2: height, stroke: c, "stroke-width": sw(1), opacity: 0.75 }); el("text", { x: x0 + 3, y: 14, fill: c, "font-size": 9.5, ...font }).textContent = n; });
        if (sel) anchor = P1; return;
      }
      if (t === "daterange") {
        if (!ok2) return;
        const yM = (P1.y + P2.y) / 2, c = col("#4dabf7"), bars = Math.round(Math.abs(d.p2.time - d.p1.time) / TF_SEC[S.tf]);
        el("line", { x1: P1.x, x2: P2.x, y1: yM, y2: yM, stroke: c, "stroke-width": sw(1.6) });
        el("text", { x: (P1.x + P2.x) / 2, y: yM - 8, "text-anchor": "middle", fill: c, "font-size": 10, ...font }).textContent = `${bars} bars`;
        if (sel) anchor = { x: (P1.x + P2.x) / 2, y: yM - 12 }; return;
      }
      if (t === "fibfan") {
        if (!ok2) return;
        const c = col("#f5a623");
        [0.236, 0.382, 0.5, 0.618, 0.786].forEach((lv) => { const py = pY(d.p1.price + (d.p2.price - d.p1.price) * lv); if (!okXY(py)) return; const e = extendRay(P1, { x: P2.x, y: py }, width); el("line", { x1: P1.x, y1: P1.y, x2: e.x, y2: e.y, stroke: c, "stroke-width": sw(1.2), opacity: 0.85 }); });
        el("circle", { cx: P1.x, cy: P1.y, r: 3, fill: c });
        if (sel) anchor = P1; return;
      }
      if (t === "fibcircles" || t === "fibarcs") {
        if (!ok2) return;
        const r0 = Math.hypot(P2.x - P1.x, P2.y - P1.y), c = col("#36e0a0");
        [0.382, 0.618, 1, 1.618, 2.618].forEach((lv) => {
          if (t === "fibcircles") el("circle", { cx: P1.x, cy: P1.y, r: r0 * lv, fill: "none", stroke: c, "stroke-width": sw(1.1), opacity: 0.85 });
          else el("path", { d: describeArc(P1.x, P1.y, r0 * lv, 180, 360), fill: "none", stroke: c, "stroke-width": sw(1.1), opacity: 0.85 });
        });
        if (sel) anchor = P1; return;
      }
      if (t === "fibspiral") { if (!ok2) return; el("polyline", { points: spiral(P1, P2).map((p) => `${p.x},${p.y}`).join(" "), fill: "none", stroke: col("#c084fc"), "stroke-width": sw(1.4) }); if (sel) anchor = P1; return; }
      if (t === "fibwedge") {
        if (!ok3) return;
        const c = col("#f5a623");
        el("line", { x1: P1.x, y1: P1.y, x2: P2.x, y2: P2.y, stroke: c, "stroke-width": sw(1.6) });
        el("line", { x1: P1.x, y1: P1.y, x2: P3.x, y2: P3.y, stroke: c, "stroke-width": sw(1.6) });
        [0.382, 0.618, 1].forEach((lv) => el("line", { x1: P1.x + (P2.x - P1.x) * lv, y1: P1.y + (P2.y - P1.y) * lv, x2: P1.x + (P3.x - P1.x) * lv, y2: P1.y + (P3.y - P1.y) * lv, stroke: c, "stroke-width": sw(1), opacity: 0.7 }));
        if (sel) anchor = P1; return;
      }
      if (t === "pitchfork") {
        if (!ok3) return;
        const c = col("#36e0a0"), med = extendRay(P1, { x: (P2.x + P3.x) / 2, y: (P2.y + P3.y) / 2 }, width);
        el("line", { x1: P1.x, y1: P1.y, x2: med.x, y2: med.y, stroke: c, "stroke-width": sw(1.6) });
        el("line", { x1: P2.x, y1: P2.y, x2: P2.x + (med.x - P1.x), y2: P2.y + (med.y - P1.y), stroke: c, "stroke-width": sw(1.2), opacity: 0.8 });
        el("line", { x1: P3.x, y1: P3.y, x2: P3.x + (med.x - P1.x), y2: P3.y + (med.y - P1.y), stroke: c, "stroke-width": sw(1.2), opacity: 0.8 });
        if (sel) anchor = P1; return;
      }
      if (t === "gannbox") {
        if (!ok2) return;
        const x = Math.min(P1.x, P2.x), y = Math.min(P1.y, P2.y), w = Math.abs(P2.x - P1.x), h = Math.abs(P2.y - P1.y), c = col("#f5a623");
        el("rect", { x, y, width: w, height: h, fill: "none", stroke: c, "stroke-width": sw(1.4) });
        for (let i = 1; i < 8; i++) {
          el("line", { x1: x, x2: x + w, y1: y + (h * i) / 8, y2: y + (h * i) / 8, stroke: c, "stroke-width": 0.7, opacity: 0.55 });
          el("line", { x1: x + (w * i) / 8, x2: x + (w * i) / 8, y1: y, y2: y + h, stroke: c, "stroke-width": 0.7, opacity: 0.55 });
        }
        el("line", { x1: x, y1: y, x2: x + w, y2: y + h, stroke: c, "stroke-width": 0.9, opacity: 0.8 });
        if (sel) anchor = { x: x + w / 2, y }; return;
      }
      if (t === "highlighter") {
        const ps = d.points.map((p) => ({ x: tX(p.time), y: pY(p.price) })).filter((p) => okXY(p.x, p.y));
        if (ps.length < 2) return;
        el("polyline", { points: ps.map((p) => `${p.x},${p.y}`).join(" "), fill: "none", stroke: col("#f5d90a"), "stroke-width": sw(12), "stroke-linejoin": "round", "stroke-linecap": "round", opacity: 0.32 });
        if (sel) anchor = ps[0]; return;
      }
      if (t === "abcd" || t === "xabcd" || t === "elliott") {
        const raw = d.points || [], ps = raw.map((p) => ({ x: tX(p.time), y: pY(p.price) }));
        if (ps.some((p) => !okXY(p.x, p.y)) || ps.length < 2) return;
        const c = col(t === "elliott" ? "#4dabf7" : t === "abcd" ? "#36e0a0" : "#c084fc");
        if (t === "xabcd" && ps.length === 5) {
          el("polygon", { points: [ps[0], ps[1], ps[2]].map((p) => `${p.x},${p.y}`).join(" "), fill: alpha(c, 0.12), stroke: "none" });
          el("polygon", { points: [ps[2], ps[3], ps[4]].map((p) => `${p.x},${p.y}`).join(" "), fill: alpha(c, 0.12), stroke: "none" });
        }
        el("polyline", { points: ps.map((p) => `${p.x},${p.y}`).join(" "), fill: "none", stroke: c, "stroke-width": sw(1.8), "stroke-linejoin": "round" });
        const names = t === "elliott" ? ["0", "1", "2", "3", "4", "5"] : t === "abcd" ? ["A", "B", "C", "D"] : ["X", "A", "B", "C", "D"];
        ps.forEach((p, i) => {
          const up = i === 0 ? raw[1] && raw[1].price < raw[0].price : raw[i].price >= raw[i - 1].price;
          el("circle", { cx: p.x, cy: p.y, r: 3, fill: c });
          el("text", { x: p.x, y: p.y + (up ? -9 : 17), "text-anchor": "middle", fill: c, "font-size": 11.5, "font-weight": 700, ...font }).textContent = t === "elliott" ? `(${names[i]})` : names[i];
        });
        // Fibonacci ratios between the legs (how harmonic traders read the pattern)
        const leg = (i) => Math.abs(raw[i + 1].price - raw[i].price);
        const ratio = (a, b, i, j) => {
          const r = leg(b) / (leg(a) || 1), m = { x: (ps[i].x + ps[j].x) / 2, y: (ps[i].y + ps[j].y) / 2 };
          el("line", { x1: ps[i].x, y1: ps[i].y, x2: ps[j].x, y2: ps[j].y, stroke: c, "stroke-width": 0.9, "stroke-dasharray": "3 3", opacity: 0.8 });
          el("text", { x: m.x, y: m.y - 4, "text-anchor": "middle", fill: c, "font-size": 9.5, ...font }).textContent = r.toFixed(3);
        };
        if (t === "xabcd" && ps.length === 5) { ratio(0, 1, 0, 2); ratio(1, 2, 1, 3); ratio(2, 3, 2, 4); const xd = Math.abs(raw[4].price - raw[1].price) / (leg(0) || 1);
          el("line", { x1: ps[0].x, y1: ps[0].y, x2: ps[4].x, y2: ps[4].y, stroke: c, "stroke-width": 0.9, "stroke-dasharray": "3 3", opacity: 0.8 });
          el("text", { x: (ps[0].x + ps[4].x) / 2, y: (ps[0].y + ps[4].y) / 2 - 4, "text-anchor": "middle", fill: c, "font-size": 9.5, ...font }).textContent = xd.toFixed(3); }
        if (t === "abcd" && ps.length === 4) { ratio(0, 1, 0, 2); ratio(1, 2, 1, 3); }
        if (sel) anchor = ps[0]; return;
      }
      if (t === "avwap") {
        const line = avwapLine(d); if (line.length < 1) return;
        const c = col("#f5a623");
        el("polyline", { points: line.map((p) => `${p.x},${p.y}`).join(" "), fill: "none", stroke: c, "stroke-width": sw(1.8) });
        el("circle", { cx: line[0].x, cy: line[0].y, r: 3.5, fill: c });
        const last = line[line.length - 1];
        el("text", { x: Math.min(last.x + 4, width - 4), y: last.y - 6, "text-anchor": last.x + 90 > width ? "end" : "start", fill: c, "font-size": 10, "font-weight": 700, ...font }).textContent = `AVWAP ${fmt(last.v)}`;
        if (sel) anchor = line[0]; return;
      }
      if (t === "regression") {
        const r = regressionGeom(d); if (!r) return;
        const c = col("#4dabf7");
        el("polygon", { points: [r.au, r.bu, r.bl, r.al].map((p) => `${p.x},${p.y}`).join(" "), fill: alpha(c, 0.08), stroke: "none" });
        el("line", { x1: r.a.x, y1: r.a.y, x2: r.b.x, y2: r.b.y, stroke: c, "stroke-width": sw(1.8) });
        [[r.au, r.bu], [r.al, r.bl]].forEach(([a, b]) => el("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, stroke: c, "stroke-width": sw(1), "stroke-dasharray": "5 3" }));
        el("text", { x: r.b.x + 4, y: r.b.y + 3, fill: c, "font-size": 9.5, ...font }).textContent = `${r.n} bars · σ ${fmt(r.sd)}`;
        if (sel) anchor = r.a; return;
      }
      if (t === "parallel") {
        if (!ok3) return;
        const off = P3.y - lerpY(P1, P2, P3.x), c = col("#4dabf7");
        el("polygon", { points: `${P1.x},${P1.y} ${P2.x},${P2.y} ${P2.x},${P2.y + off} ${P1.x},${P1.y + off}`, fill: alpha(c, 0.1), stroke: "none" });
        el("line", { x1: P1.x, y1: P1.y, x2: P2.x, y2: P2.y, stroke: c, "stroke-width": sw(1.6) });
        el("line", { x1: P1.x, y1: P1.y + off, x2: P2.x, y2: P2.y + off, stroke: c, "stroke-width": sw(1.6) });
        el("line", { x1: P1.x, y1: P1.y + off / 2, x2: P2.x, y2: P2.y + off / 2, stroke: c, "stroke-width": sw(1), "stroke-dasharray": "4 3", opacity: 0.8 });
        if (sel) anchor = P1; return;
      }
      if (t === "circle") {
        if (!ok2) return;
        const c = col("#4dabf7"), r = Math.hypot(P2.x - P1.x, P2.y - P1.y);
        el("circle", { cx: P1.x, cy: P1.y, r, fill: alpha(c, 0.1), stroke: c, "stroke-width": sw(1.4) });
        if (sel) anchor = { x: P1.x, y: P1.y - r }; return;
      }
      if (t === "infoline") {
        if (!ok2) return;
        const c = col("#4dabf7"), dp = d.p2.price - d.p1.price, pc = (dp / d.p1.price) * 100;
        const bars = typeof d.p1.time === "number" ? Math.round(Math.abs(d.p2.time - d.p1.time) / TF_SEC[S.tf]) : 0;
        const ang = (Math.atan2(-(P2.y - P1.y), P2.x - P1.x) * 180) / Math.PI;
        el("line", { x1: P1.x, y1: P1.y, x2: P2.x, y2: P2.y, stroke: c, "stroke-width": sw(1.8) });
        el("circle", { cx: P1.x, cy: P1.y, r: 2.6, fill: c }); el("circle", { cx: P2.x, cy: P2.y, r: 2.6, fill: c });
        const lines = [`${dp >= 0 ? "+" : ""}${fmt(dp)} (${pc >= 0 ? "+" : ""}${pc.toFixed(2)}%)`, `${bars} bars · ${ang.toFixed(1)}°`];
        const w = Math.max(...lines.map((l) => l.length)) * 6.2 + 14, bx = Math.min(P2.x + 8, width - w - 4), by = P2.y - 18;
        el("rect", { x: bx, y: by, width: w, height: 34, rx: 5, fill: "rgba(17,26,23,0.92)", stroke: c, "stroke-width": 1 });
        lines.forEach((l, i) => { el("text", { x: bx + 7, y: by + 14 + i * 14, fill: i ? "#9fb3a8" : (dp >= 0 ? "#36e0a0" : "#ff526b"), "font-size": 10.5, "font-weight": i ? 500 : 700, ...font }).textContent = l; });
        if (sel) anchor = { x: (P1.x + P2.x) / 2, y: Math.min(P1.y, P2.y) }; return;
      }
      if (t === "datepricerange") {
        if (!ok2) return;
        const up = d.p2.price >= d.p1.price, c = col(up ? "#36e0a0" : "#ff526b");
        const x = Math.min(P1.x, P2.x), y = Math.min(P1.y, P2.y), w = Math.abs(P2.x - P1.x), h = Math.abs(P2.y - P1.y);
        el("rect", { x, y, width: w, height: h, fill: alpha(c, 0.12), stroke: c, "stroke-width": sw(1), "stroke-dasharray": "4 3" });
        el("line", { x1: x + w / 2, x2: x + w / 2, y1: y, y2: y + h, stroke: c, "stroke-width": 1 });
        el("line", { x1: x, x2: x + w, y1: y + h / 2, y2: y + h / 2, stroke: c, "stroke-width": 1 });
        const dp = d.p2.price - d.p1.price, pc = (dp / d.p1.price) * 100;
        const i0 = barAtOrBefore(Math.min(d.p1.time, d.p2.time)), i1 = barAtOrBefore(Math.max(d.p1.time, d.p2.time));
        let vol = 0; if (i0 >= 0) for (let i = i0; i <= i1; i++) vol += S.bars[i].volume || 0;
        const secs = Math.abs(d.p2.time - d.p1.time), bars = Math.round(secs / TF_SEC[S.tf]);
        const span = secs >= 86400 ? `${(secs / 86400).toFixed(1)}d` : `${(secs / 3600).toFixed(1)}h`;
        const label = [`${dp >= 0 ? "+" : ""}${fmt(dp)} (${pc >= 0 ? "+" : ""}${pc.toFixed(2)}%)`, `${bars} bars, ${span}`, `Vol ${compact(vol)}`];
        const lw = Math.max(...label.map((l) => l.length)) * 6.1 + 14, lx = x + w / 2 - lw / 2, ly = up ? y - 50 : y + h + 6;
        el("rect", { x: lx, y: ly, width: lw, height: 46, rx: 5, fill: c, opacity: 0.92 });
        label.forEach((l, i) => { el("text", { x: lx + lw / 2, y: ly + 14 + i * 13, "text-anchor": "middle", fill: "#06110c", "font-size": 10.5, "font-weight": i ? 600 : 700, ...font }).textContent = l; });
        if (sel) anchor = { x: x + w / 2, y }; return;
      }
      if (t === "cyclic") {
        if (!ok2) return;
        const unit = typeof d.p2.time === "number" && typeof d.p1.time === "number" ? Math.abs(d.p2.time - d.p1.time) : 0;
        if (!unit) return;
        const c = col("#4dabf7"), t0 = Math.min(d.p1.time, d.p2.time);
        for (let k = 0; k < 400; k++) { const x0 = tX(t0 + k * unit); if (x0 == null) break; el("line", { x1: x0, x2: x0, y1: 0, y2: height, stroke: c, "stroke-width": sw(k ? 1 : 1.4), opacity: k ? 0.6 : 0.9 }); }
        if (sel) anchor = P1; return;
      }
      if (t === "arrowup" || t === "arrowdown") {
        const x = tX(d.time), y = pY(d.price); if (!okXY(x, y)) return;
        const upA = t === "arrowup", c = col(upA ? "#36e0a0" : "#ff526b"), k = 1 + 0.25 * ((st.width || 1) - 1);
        const pts = upA ? [[0, 0], [9, 10], [3.5, 10], [3.5, 22], [-3.5, 22], [-3.5, 10], [-9, 10]] : [[0, 0], [9, -10], [3.5, -10], [3.5, -22], [-3.5, -22], [-3.5, -10], [-9, -10]];
        el("polygon", { points: pts.map(([a, b]) => `${x + a * k},${y + b * k}`).join(" "), fill: c, stroke: sel ? "#fff" : "none", "stroke-width": 1 });
        if (sel) anchor = { x, y: y - 26 }; return;
      }
      if (t === "pricelabel") {
        const x = tX(d.time), y = pY(d.price); if (!okXY(x, y)) return;
        const c = col("#4dabf7"), txt = fmt(d.price), w = txt.length * 6.8 + 16;
        el("path", { d: `M ${x} ${y} L ${x + 8} ${y - 10} H ${x + 8 + w} V ${y + 10} H ${x + 8} Z`, fill: c, stroke: sel ? "#fff" : "none" });
        el("text", { x: x + 14, y: y + 4, fill: "#06110c", "font-size": 11, "font-weight": 700, ...font }).textContent = txt;
        if (sel) anchor = { x: x + w / 2, y: y - 12 }; return;
      }
      if (t === "longpos" || t === "shortpos") {
        if (!ok2) return;
        const lg = t === "longpos", x = Math.min(P1.x, P2.x), w = Math.abs(P2.x - P1.x), eY = P1.y, tY = P2.y, mY = eY - (tY - eY);
        el("rect", { x, y: Math.min(tY, eY), width: w, height: Math.abs(eY - tY), fill: lg ? "rgba(54,224,160,0.22)" : "rgba(255,82,107,0.22)", stroke: lg ? "#36e0a0" : "#ff526b", "stroke-width": 1 });
        el("rect", { x, y: Math.min(eY, mY), width: w, height: Math.abs(eY - mY), fill: lg ? "rgba(255,82,107,0.18)" : "rgba(54,224,160,0.18)", stroke: lg ? "#ff526b" : "#36e0a0", "stroke-width": 1 });
        el("line", { x1: x, x2: x + w, y1: eY, y2: eY, stroke: "#e6ecf5", "stroke-width": 1.4 });
        el("text", { x: x + w / 2, y: Math.min(tY, eY) - 4, "text-anchor": "middle", fill: lg ? "#36e0a0" : "#ff526b", "font-size": 10, "font-weight": 700, ...font }).textContent =
          `1:1 · ${Math.abs(((d.p2.price - d.p1.price) / d.p1.price) * 100).toFixed(2)}%`;
        if (sel) anchor = { x: x + w / 2, y: Math.min(tY, eY, mY) }; return;
      }
    });
    if (preview && pendingPoints.length) {
      const chain = [...pendingPoints, preview];
      for (let i = 0; i < chain.length - 1; i++) {
        const a = { x: tX(chain[i].time), y: pY(chain[i].price) }, b = { x: tX(chain[i + 1].time), y: pY(chain[i + 1].price) };
        if (okXY(a.x, a.y, b.x, b.y)) el("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, stroke: "#8aa0b8", "stroke-width": 1.2, "stroke-dasharray": "3 3" });
      }
    } else if (activeTool === "path" && pendingPoints.length) {
      const ps = pendingPoints.map((p) => ({ x: tX(p.time), y: pY(p.price) })).filter((p) => okXY(p.x, p.y));
      if (ps.length) el("polyline", { points: ps.map((p) => `${p.x},${p.y}`).join(" "), fill: "none", stroke: "#4dabf7", "stroke-width": 1.6, "stroke-dasharray": "3 3" });
    }
    if (liveBrush && liveBrush.length > 1) {
      const ps = liveBrush.map((p) => ({ x: tX(p.time), y: pY(p.price) })).filter((p) => okXY(p.x, p.y));
      if (ps.length > 1) el("polyline", { points: ps.map((p) => `${p.x},${p.y}`).join(" "), fill: "none", stroke: "#c084fc", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
    }
    const selD = drawings.find((x) => x.id === selectedId);
    if (selD && anchor && !dragging) selectionBar(anchor, selD); else hideSelBar();
  }

  // ================================================================== signal engine: levels, history markers, card
  async function loadEngine() {
    const sym = S.coin;
    let d;
    try { d = await F.getJSON(`/api/engine?coin=${enc(sym)}`); } catch (e) { return; }
    if (sym !== S.coin) return;
    S.eng = d;
    drawEngine(); renderEngineCard(); snapshot();
  }
  function drawEngine() {
    if (!S.main) return;
    S.engLines.forEach((l) => { try { S.main.removePriceLine(l); } catch (e) {} });
    S.engLines = [];
    const e = S.eng && S.eng.coin === S.coin && S.eng.engine && !S.eng.engine.error ? S.eng.engine : null;
    const a = e && e.active;
    if (a && S.set.levels) {
      const add = (p, color, title, style) => S.engLines.push(S.main.createPriceLine({ price: p, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
      add(a.take_profit, css("--long"), "Target", 2);
      add(a.entry, css("--dim"), a.side === "LONG" ? "Long entry" : "Short entry", 2);
      add(a.stop_loss, css("--short"), "Stop", 2);
    }
    const marks = [];
    if (e && S.set.history && S.bars.length) {
      const first = S.bars[0].time;
      const at = (iso) => { const ts = isoSec(iso); if (ts == null || ts - 1 < first) return null; const i = barAtOrBefore(ts - 1); return i >= 0 ? S.bars[i].time : null; };
      (e.recent || []).forEach((t) => {
        const tin = at(t.signal_at), tout = at(t.closed_at), long = t.side === "LONG";
        if (tin != null) marks.push({ time: tin, position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown", color: long ? css("--long") : css("--short"), text: long ? "Long" : "Short" });
        if (tout != null) {
          const r = Number(t.r);
          const lab = t.status === "TP" ? "Target" : t.status === "SL" ? "Stop" : "Time";
          marks.push({ time: tout, position: long ? "aboveBar" : "belowBar", shape: "circle", color: r >= 0 ? css("--long") : css("--short"), text: `${lab} ${r >= 0 ? "+" : "−"}${Math.abs(r).toFixed(2)}R` });
        }
      });
      if (a) {
        const tin = at(a.signal_at), long = a.side === "LONG";
        if (tin != null) marks.push({ time: tin, position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown", color: long ? css("--long") : css("--short"), text: `${long ? "Long" : "Short"} (open)` });
      }
    }
    marks.sort((x, y) => x.time - y.time);
    try {
      if (!S.markers) S.markers = LW.createSeriesMarkers(S.main, marks);
      else S.markers.setMarkers(marks);
    } catch (err) { /* markers are optional */ }
    const on = S.set.levels || S.set.history;
    $("levels-btn").setAttribute("aria-pressed", on ? "true" : "false");
  }
  function renderEngineCard() {
    const box = $("eng-body"), chip = $("sig-chip");
    const d = S.eng;
    if (!d || d.coin !== S.coin) { box.innerHTML = `<p class="mk-empty">Loading…</p>`; chip.hidden = true; return; }
    if (d.asset === "forex") {
      box.innerHTML = `<p class="eng-copy">Signals are made for crypto only for now. For ${S.coin.startsWith("XAU") ? "gold" : "forex pairs"} you get this chart and the market brief on the dashboard.</p>`;
      chip.hidden = false; chip.className = "mk-sig"; chip.innerHTML = "<i></i>Chart only";
      return;
    }
    const e = d.engine;
    if (!e || e.error || d.error) {
      box.innerHTML = `<p class="eng-copy">${F.esc((e && e.error) || d.error || "The signal engine did not answer.")}</p>`;
      chip.hidden = true; return;
    }
    const a = e.active, rec = e.recent || [];
    const wins = rec.filter((t) => Number(t.r) > 0).length, net = rec.reduce((s, t) => s + Number(t.r || 0), 0);
    const hist = rec.length
      ? `<div class="eng-hist" aria-label="Recent signals">${rec.slice(-20).map((t) => `<i title="${F.esc(t.side)} ${F.esc(t.status)} ${Number(t.r).toFixed(2)}R" style="background:${Number(t.r) > 0 ? "var(--long)" : "var(--short)"}"></i>`).join("")}</div>
         <p class="eng-copy">Last ${rec.length} signal${rec.length === 1 ? "" : "s"} on ${F.esc(F.base(S.coin))} (about 50 days, replayed, fees included): <b>${wins} won</b>, ${rec.length - wins} lost, net <b class="${net >= 0 ? "c-long" : "c-short"}">${net >= 0 ? "+" : "−"}${Math.abs(net).toFixed(2)}R</b>.</p>`
      : `<p class="eng-copy">No signals on ${F.esc(F.base(S.coin))} in the last ~50 days. The engine skips weak setups.</p>`;
    const untested = d.tested === false ? `<p class="mk-note" style="margin-top:8px">${F.esc(F.base(S.coin))} was not one of the 16 coins in the engine's test, so its results may differ.</p>` : "";
    if (a) {
      const long = a.side === "LONG", p = e.progress || {};
      box.innerHTML = `<div class="eng-state"><span class="eng-verdict ${long ? "c-long" : "c-short"}">${long ? "Long" : "Short"}</span>
          <span class="mk-sig ${long ? "long" : "short"}"><i></i>${e.fresh ? "New signal" : "Active signal"} · ${Math.round(a.confidence)}% win chance</span></div>
        <div class="eng-lv"><div><span>Entry</span><b class="tnum">${fmt(a.entry)}</b></div><div><span>Target</span><b class="tnum c-long">${fmt(a.take_profit)}</b></div><div><span>Stop</span><b class="tnum c-short">${fmt(a.stop_loss)}</b></div></div>
        ${p.move_pct != null ? `<p class="eng-copy">Price is ${p.move_pct >= 0 ? "+" : "−"}${Math.abs(p.move_pct).toFixed(2)}% since entry, ${Math.abs(p.pct)}% of the way to the ${p.pct >= 0 ? "target" : "stop"}.</p>` : ""}${hist}${untested}`;
      chip.hidden = false; chip.className = `mk-sig ${long ? "long" : "short"}`; chip.innerHTML = `<i></i>${long ? "Long" : "Short"} signal`;
    } else {
      const close = Math.max(0, Math.min(100, e.strength || 0));
      box.innerHTML = `<div class="eng-state"><span class="eng-verdict c-amber">Wait</span><span class="mk-sig wait"><i></i>${Math.round(close)}% of the way to a signal${e.setup_forming ? " · setup forming" : ""}</span></div>
        <p class="eng-copy">The model leans ${String(e.bias || "").toLowerCase()} (${e.bias === "LONG" ? e.p_long : e.p_short}% estimated win chance), below the level it needs for a signal. It checks again at each 4-hour close.</p>${hist}${untested}`;
      chip.hidden = false; chip.className = "mk-sig wait"; chip.innerHTML = `<i></i>Wait · ${Math.round(close)}%`;
    }
  }

  // ================================================================== price alerts
  async function loadAlerts() {
    const sym = S.coin;
    let d;
    try { d = await F.getJSON("/api/alerts"); } catch (e) { return; }
    if (sym !== S.coin || d.error) return;
    S.alerts = (d.alerts || []).filter((a) => a.symbol === sym && /^PRICE_/.test(a.alert_type) && a.is_enabled && a.status !== "TRIGGERED");
    drawAlertLines(); renderAlertList();
  }
  function drawAlertLines() {
    if (!S.main) return;
    S.alertLines.forEach((l) => { try { S.main.removePriceLine(l); } catch (e) {} });
    S.alertLines = [];
    if (!S.set.alerts) return;
    S.alerts.forEach((a) => S.alertLines.push(S.main.createPriceLine({ price: Number(a.target_value), color: css("--amber"), lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: "Alert" })));
  }
  function renderAlertList() {
    const n = S.alerts.length, c = $("alert-count");
    c.hidden = !n; c.textContent = n;
    $("alert-list").innerHTML = n ? S.alerts.map((a) => `<div class="ch-alert-item"><b class="tnum">${fmt(a.target_value)}</b><small>${a.alert_type === "PRICE_ABOVE" ? "when price rises to it" : a.alert_type === "PRICE_BELOW" ? "when price falls to it" : "when price crosses it"}</small>
      <button type="button" data-del="${a.id}">Delete</button></div>`).join("") : `<p class="mk-note">No price alerts on ${F.esc(S.coin)} yet.</p>`;
  }
  function currentPrice() { return S.bars.length ? S.bars[S.bars.length - 1].close : null; }
  function updateAlertHint() {
    const v = Number($("alert-price").value), now = currentPrice();
    if (!isFinite(v) || !v || now == null) { $("alert-hint").textContent = "Tip: while this box is open, tap the chart to pick a price."; return; }
    const pc = (v / now - 1) * 100;
    $("alert-hint").textContent = `Fires when the price ${v >= now ? "rises" : "falls"} to ${fmt(v)} (${F.pct(pc)} from now).`;
  }
  $("alert-price").addEventListener("input", updateAlertHint);
  $("alert-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const v = Number(String($("alert-price").value).replace(/,/g, "")), now = currentPrice(), msg = $("alert-msg");
    if (!isFinite(v) || v <= 0) { msg.className = "mk-msg bad"; msg.textContent = "Enter a price above 0."; return; }
    const type = now == null || v >= now ? "PRICE_ABOVE" : "PRICE_BELOW";
    const d = await F.sendJSON("/api/alerts", "POST", { symbol: S.coin, alert_type: type, target_value: v });
    if (!d._ok) { msg.className = "mk-msg bad"; msg.textContent = d.error || "The alert could not be created."; return; }
    msg.className = "mk-msg ok"; msg.textContent = `Alert set at ${fmt(v)}.`;
    if (typeof Notification !== "undefined" && Notification.permission === "default") { try { Notification.requestPermission(); } catch (err) {} }
    loadAlerts();
  });
  $("alert-list").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-del]");
    if (!b) return;
    b.disabled = true;
    await F.sendJSON(`/api/alerts/${b.dataset.del}`, "DELETE");
    loadAlerts();
  });
  document.addEventListener("alertstriggered", () => loadAlerts());

  // ================================================================== snapshot under the chart
  function snapshot() {
    const box = $("snap"), n = S.bars.length;
    if (n < 30) { box.innerHTML = `<p class="mk-empty">Waiting for candles…</p>`; return; }
    const closes = S.bars.map((b) => b.close), last = S.bars[n - 1];
    const r = rsi(closes, 14)[n - 1];
    const a = atr(S.bars, 14), atrPct = a[n - 1] != null ? (a[n - 1] / last.close) * 100 : null;
    const pcts = [];
    for (let i = 0; i < n; i++) if (a[i] != null) pcts.push((a[i] / S.bars[i].close) * 100);
    const rank = atrPct == null ? null : Math.round((pcts.filter((v) => v <= atrPct).length / pcts.length) * 100);
    const volState = rank == null ? "--" : rank < 25 ? "Low" : rank < 70 ? "Normal" : rank < 90 ? "High" : "Extreme";
    const e50 = ema(closes, 50)[n - 1], e200 = ema(closes, 200)[n - 1];
    const trend = e50 == null || e200 == null ? "Not enough data" : last.close > e50 && e50 > e200 ? "Up" : last.close < e50 && e50 < e200 ? "Down" : "Sideways";
    const look = S.bars.slice(-50), hi = Math.max(...look.map((b) => b.high)), lo = Math.min(...look.map((b) => b.low));
    const vAvg = S.bars.slice(-21, -1).reduce((s, b) => s + (b.volume || 0), 0) / 20;
    const vx = vAvg > 0 ? (last.volume || 0) / vAvg : null;
    const tile = (k, v, sub, cls, tipKey) => `<div class="mk-tile"><dt>${k}${tipKey ? F.tip(tipKey, k) : ""}</dt><dd class="tnum ${cls || ""}">${v}<small>${sub}</small></dd></div>`;
    box.innerHTML = [
      tile("Trend", trend, "price vs EMA 50 and EMA 200", trend === "Up" ? "c-long" : trend === "Down" ? "c-short" : "", "trend"),
      tile("RSI 14", r == null ? "--" : r.toFixed(1), r == null ? "" : r >= 70 ? "overbought" : r <= 30 ? "oversold" : r >= 55 ? "buyers in control" : r <= 45 ? "sellers in control" : "neutral", "", "momentum"),
      tile("Volatility", volState, atrPct == null ? "" : `ATR ${atrPct.toFixed(2)}% per candle, ${rank}th percentile`, volState === "Extreme" ? "c-short" : volState === "High" ? "c-amber" : "", "volatility"),
      tile("Volume", vx == null ? "--" : `${vx.toFixed(2)}×`, "this candle vs the 20-candle average", vx != null && vx >= 2 ? "c-amber" : "", "volume"),
      tile("Range high", fmt(hi), `${F.pct((hi / last.close - 1) * 100)} away (50 candles)`, "", "range"),
      tile("Range low", fmt(lo), `${F.pct((lo / last.close - 1) * 100)} away (50 candles)`, ""),
      tile("Candle", last.close >= last.open ? "Green" : "Red", `${F.pct((last.close / last.open - 1) * 100)} since it opened`, last.close >= last.open ? "c-long" : "c-short"),
      tile("Loaded", `${n} candles`, `from ${new Date(S.bars[0].time * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })}`, ""),
    ].join("");
  }

  // ================================================================== popovers, settings, full screen, screenshot, keys
  const POPS = [["type-btn", "type-pop"], ["ind-btn", "ind-pop"], ["alert-btn", "alert-pop"], ["more-btn", "more-pop"]];
  let backdrop = null;
  function closePops(except) {
    POPS.forEach(([b, p]) => { if (p !== except) { $(p).hidden = true; $(b).setAttribute("aria-expanded", "false"); } });
    S.pickPrice = except === "alert-pop";
    if (backdrop && !except) backdrop.hidden = true;
  }
  function openPop(btnId, popId) {
    const open = $(popId).hidden;
    closePops(open ? popId : null);
    $(popId).hidden = !open;
    $(btnId).setAttribute("aria-expanded", open ? "true" : "false");
    if (open && phone()) {
      if (!backdrop) { backdrop = document.createElement("div"); backdrop.className = "mk-backdrop"; backdrop.addEventListener("click", () => closePops()); document.body.appendChild(backdrop); }
      backdrop.hidden = false;
    } else if (backdrop) backdrop.hidden = true;
    if (open && popId === "ind-pop") { renderIndPanel(); if (!phone()) setTimeout(() => $("ind-search").focus(), 20); }
    if (open && popId === "alert-pop") { const p = currentPrice(); if (p != null && !$("alert-price").value) $("alert-price").value = Number(p.toFixed(F.decimals(p, S.coin))); updateAlertHint(); $("alert-msg").textContent = ""; }
    if (open && popId === "type-pop") $("type-pop").querySelectorAll("[data-type]").forEach((b) => b.setAttribute("aria-checked", b.dataset.type === S.type ? "true" : "false"));
  }
  POPS.forEach(([b, p]) => {
    $(b).addEventListener("click", (e) => { e.stopPropagation(); openPop(b, p); });
    $(p).addEventListener("click", (e) => { e.stopPropagation(); if (e.target.closest("[data-close]")) closePops(); });
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".mk-pop") && !e.target.closest(".an-tip-pop")) closePops(); });
  $("type-pop").addEventListener("click", (e) => {
    const b = e.target.closest("[data-type]");
    if (!b) return;
    S.type = b.dataset.type; savePrefs();
    $("type-label").textContent = TYPE_LABEL[S.type];
    closePops(); rebuildMain(); legend();
  });
  function applySetting(key, on) {
    S.set[key] = on; savePrefs();
    if (key === "volume") S.vol.setData(on ? volPoints() : []);
    if (key === "levels" || key === "history") drawEngine();
    if (key === "alerts") drawAlertLines();
    if (key === "log") { S.chart.applyOptions({ rightPriceScale: { mode: on ? 1 : 0 } }); $("log-btn").setAttribute("aria-pressed", on ? "true" : "false"); }
    if (key === "magnet") S.chart.applyOptions({ crosshair: { mode: on ? 1 : 0 } });
    if (key === "watermark") setWatermark();
    const box = document.querySelector(`#more-pop input[data-set="${key}"]`);
    if (box) box.checked = on;
  }
  document.querySelectorAll('#more-pop input[data-set]').forEach((inp) => {
    inp.checked = !!S.set[inp.dataset.set];
    inp.addEventListener("change", () => { if (S.chart) applySetting(inp.dataset.set, inp.checked); });
  });
  $("log-btn").addEventListener("click", () => { if (S.chart) applySetting("log", !S.set.log); });
  $("levels-btn").addEventListener("click", () => { const on = !(S.set.levels || S.set.history); if (!S.chart) return; S.set.levels = on; S.set.history = on; savePrefs(); drawEngine();
    document.querySelectorAll('#more-pop input[data-set="levels"], #more-pop input[data-set="history"]').forEach((i) => { i.checked = on; }); });
  $("reset-btn").addEventListener("click", () => {
    if (!window.confirm("Reset the chart? This removes your indicators and chart settings in this browser (your drawings and alerts stay).")) return;
    Object.keys(S.ind).forEach((k) => clearIndicatorSeries(k)); S.ind = {};
    S.set = { volume: true, levels: true, history: true, alerts: true, log: false, magnet: false, watermark: true };
    S.type = "candles"; savePrefs();
    document.querySelectorAll('#more-pop input[data-set]').forEach((i) => { i.checked = !!S.set[i.dataset.set]; });
    $("type-label").textContent = TYPE_LABEL[S.type];
    S.chart.applyOptions(theme()); rebuildMain(); setAllData(); refreshIndicators(true); setWatermark(); applySetting("log", false); legend(); closePops();
  });
  $("keys-btn").addEventListener("click", () => { $("keys").hidden = !$("keys").hidden; });
  function setFullscreen(on) {
    S.fs = on;
    $("ch-card").classList.toggle("is-fs", on);
    document.body.classList.toggle("an-lock", on);
    $("fs-btn").setAttribute("aria-pressed", on ? "true" : "false");
    $("fs-btn").title = on ? "Exit full screen (Esc)" : "Full screen (F)";
    setTimeout(() => { renderDrawings(); renderIndicatorOverlay(); }, 60);
  }
  $("fs-btn").addEventListener("click", () => setFullscreen(!S.fs));
  $("latest-btn").addEventListener("click", () => { if (S.chart) S.chart.timeScale().scrollToRealTime(); });
  function setDraw(on) {
    $("ch-tools").hidden = !on;
    $("draw-btn").setAttribute("aria-pressed", on ? "true" : "false");
    F.store.set("sfm-chart-draw", on ? "on" : "off");
    if (!on && activeTool !== "cursor") selectTool("cursor");
  }
  $("draw-btn").addEventListener("click", () => setDraw($("ch-tools").hidden));
  $("shot-btn").addEventListener("click", screenshot);
  async function screenshot() {
    if (!S.chart) return;
    try {
      const shot = S.chart.takeScreenshot(true);
      const host = $("ch-chart"), scale = shot.width / (host.clientWidth || shot.width), head = Math.round(34 * scale);
      const out = document.createElement("canvas");
      out.width = shot.width; out.height = shot.height + head;
      const c = out.getContext("2d");
      c.fillStyle = css("--surface") || "#111a17"; c.fillRect(0, 0, out.width, out.height);
      c.fillStyle = css("--text") || "#e8f1ec"; c.font = `600 ${Math.round(14 * scale)}px Inter, sans-serif`; c.textBaseline = "middle";
      const last = currentPrice();
      c.fillText(`${S.coin} · ${TF_LABEL[S.tf]} · ${fmt(last)} · Signals FM · ${new Date().toUTCString().slice(5, 22)} UTC`, Math.round(10 * scale), head / 2);
      c.drawImage(shot, 0, head);
      for (const svg of [$("ind-svg"), $("draw-svg")]) {
        if (!svg.childNodes.length) continue;
        const copy = svg.cloneNode(true);
        copy.setAttribute("xmlns", "http://www.w3.org/2000/svg");
        copy.setAttribute("width", host.clientWidth); copy.setAttribute("height", host.clientHeight);
        const url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(new XMLSerializer().serializeToString(copy));
        await new Promise((res) => { const img = new Image(); img.onload = () => { c.drawImage(img, 0, head, host.clientWidth * scale, host.clientHeight * scale); res(); }; img.onerror = res; img.src = url; });
      }
      out.toBlob((b) => {
        if (!b) { toast("The picture could not be made in this browser"); return; }
        const a = document.createElement("a");
        a.href = URL.createObjectURL(b); a.download = `SignalsFM-${F.base(S.coin)}-${S.tf}-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.png`;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 4000);
        toast("Chart picture saved");
      }, "image/png");
    } catch (e) { toast("The picture could not be made in this browser"); }
  }
  document.addEventListener("keydown", (e) => {
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if (e.key === "Escape") {
      if (!$("ask").hidden) return;
      if (picker.isOpen()) return;
      if (pendingPoints.length) { pendingPoints = []; renderDrawings(); return; }
      if (POPS.some(([, p]) => !$(p).hidden)) { closePops(); return; }
      if (selectedId != null) { selectedId = null; renderDrawings(); return; }
      if (S.fs) { setFullscreen(false); return; }
      return;
    }
    if (typing || !$("ask").hidden || picker.isOpen()) return;
    if ((e.ctrlKey || e.metaKey) && /^[zZyY]$/.test(e.key)) { e.preventDefault(); if (/[yY]/.test(e.key) || e.shiftKey) redo(); else undo(); return; }
    if ((e.key === "Delete" || e.key === "Backspace") && selectedId != null) { e.preventDefault(); removeSelected(); return; }
    if (e.key === "/") { e.preventDefault(); openPop("ind-btn", "ind-pop"); return; }
    if ((e.key === "f" || e.key === "F") && !e.ctrlKey && !e.metaKey && !e.altKey) { setFullscreen(!S.fs); return; }
    if (e.altKey && /^[1-8]$/.test(e.key)) { e.preventDefault(); selectTf(Object.keys(TF_SEC)[Number(e.key) - 1]); return; }
    if (e.altKey && (e.key === "r" || e.key === "R")) { e.preventDefault(); if (S.chart) S.chart.timeScale().scrollToRealTime(); }
  });

  // ================================================================== wiring + start
  $("coin-btn").addEventListener("click", () => picker.open($("coin-btn")));
  $("recent").addEventListener("click", (e) => { const b = e.target.closest("[data-sym]"); if (b) selectCoin(b.dataset.sym); });
  $("star-btn").addEventListener("click", toggleStar);
  document.querySelectorAll("#tf-seg button").forEach((b) => b.addEventListener("click", () => selectTf(b.dataset.tf)));
  document.querySelectorAll("#range-seg button").forEach((b) => b.addEventListener("click", () => {
    const r = RANGES[b.dataset.range];
    if (!r) return;
    S.range = b.dataset.range;
    selectTf(r[0], r[1]);
    S.range = b.dataset.range;
    document.querySelectorAll("#range-seg button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
  }));
  async function loadTickers() {
    try { const d = await F.getJSON("/api/market/tickers"); if (d.ok) { S.tickers = d.tickers || {}; picker.setTickers(S.tickers); updateHeader(); } } catch (e) {}
  }
  async function loadWatch() {
    try { const d = await F.getJSON("/api/watchlist"); if (!d.error) { S.watch = new Set((d.watchlist || []).map((w) => w.symbol)); picker.setWatch(S.watch); updateStar(); } } catch (e) {}
  }
  async function loadBoard() {
    S.boardAt = Date.now();
    try { const d = await F.getJSON("/api/signals/board"); if (d.ok) picker.setBoard(d); } catch (e) {}
  }
  const visible = () => document.visibilityState !== "hidden";

  async function start() {
    loadPrefs();
    document.querySelectorAll("#more-pop input[data-set]").forEach((i) => { i.checked = !!S.set[i.dataset.set]; });
    $("type-label").textContent = TYPE_LABEL[S.type];
    $("log-btn").setAttribute("aria-pressed", S.set.log ? "true" : "false");
    const drawPref = F.store.get("sfm-chart-draw");
    setDraw(drawPref ? drawPref === "on" : !phone());
    renderToolbar();
    let coins;
    try { coins = await F.getJSON("/coins"); } catch (e) { coins = {}; }
    S.coins = { crypto: coins.crypto || ["BTC/USDT"], forex: coins.forex || [] };
    picker.setCoins(S.coins);
    const qs = new URLSearchParams(location.search);
    const want = (qs.get("coin") || F.store.get("sfm-coin") || "BTC/USDT").toUpperCase();
    const tf = qs.get("tf") || F.store.get("sfm-chart-tf") || "1h";
    S.tf = TF_SEC[tf] ? tf : "1h";
    document.querySelectorAll("#tf-seg button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.tf === S.tf ? "true" : "false"));
    $("snap-tf").textContent = `on ${TF_LABEL[S.tf]} candles`;
    if (!ensureChart()) { selectCoin(picker.valid(want) ? want : "BTC/USDT", true); return; }
    selectCoin(picker.valid(want) ? want : "BTC/USDT", true);
    loadTickers(); loadWatch();
    setInterval(() => { if (visible()) loadTickers(); }, 15000);
    setInterval(() => { if (visible()) loadEngine(); }, 60000);
    setInterval(() => { if (visible()) loadAlerts(); }, 60000);
    setInterval(() => { tickStatus(); if (S.bars.length && Date.now() - S.lastUpdate > 25000 && S.live && !F.isForex(S.coin)) setLive(false, "Live updates paused, retrying…"); }, 1000);
    setInterval(() => { if (visible()) snapshot(); }, 30000);
    document.addEventListener("visibilitychange", () => { if (visible()) { poll(); loadTickers(); } });
  }
  start();
})();
