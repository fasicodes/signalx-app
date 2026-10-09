/* Signals FM Liquidity scanner (templates/liquidity-scanner.html).
   Its own page with a coin switcher; it never runs the Pro terminal's analysis.
   Data: /api/liquidity/map (order book, trades, levels, estimated liquidations and the scanner cards), /api/market/tickers, /api/alerts. */
(function () {
  "use strict";
  const F = window.SFM;
  const $ = (id) => document.getElementById(id);
  const enc = encodeURIComponent;
  const S = { coins: { crypto: [], forex: [] }, coin: "BTC/USDT", tf: "1h", every: 10, paused: false, win: 0, data: null, seq: 0,
              loading: false, fetchedAt: 0, err: null, tickers: {}, alerts: [], timer: null };
  const fmt = (v) => F.price(v, S.coin);
  const num = (v) => v != null && !isNaN(v);
  const visible = () => document.visibilityState !== "hidden";
  const TF_LABEL = { "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1D" };

  // ------------------------------------------------------------------ coin + controls
  const picker = F.coinPicker({ current: () => S.coin, onSelect: (s) => selectCoin(s) });
  function selectCoin(sym, initial) {
    if (!picker.valid(sym)) sym = "BTC/USDT";
    if (sym === S.coin && !initial) return;
    S.coin = sym; S.data = null;
    picker.remember(sym);
    F.store.set("sfm-coin", sym);
    $("coin-sym").textContent = sym;
    $("coin-kind").textContent = F.isForex(sym) ? (sym.startsWith("XAU") ? "Gold" : "Forex") : "Crypto";
    F.setIcon($("coin-icon"), sym);
    $("al-sym").textContent = sym;
    $("chart-link").href = `/chart?coin=${enc(sym)}`;
    try { history.replaceState(null, "", `/liquidity-scanner?coin=${enc(sym)}&tf=${S.tf}`); } catch (e) {}
    document.title = `${sym} liquidity | Signals FM`;
    renderRecent();
    ["say", "depth", "bands", "ladder", "trades", "liq", "levels", "sweep", "pools", "cards"].forEach((id) => {
      const el = $(id);
      if (id === "say") el.textContent = `Reading the ${sym} order book…`;
      else if (id === "bands" || id === "pools" || id === "cards") el.innerHTML = "";
      else el.innerHTML = `<div class="lq-unavail">Loading ${F.esc(sym)}…</div>`;
    });
    $("kpis").innerHTML = "";
    updateQuote();
    load();
    loadAlerts();
  }
  function renderRecent() {
    const r = picker.recent().filter((s) => s !== S.coin).slice(0, 5);
    $("recent").innerHTML = r.map((s) => `<button type="button" class="mk-chip" data-sym="${F.esc(s)}">${F.esc(F.base(s))}</button>`).join("");
  }
  function setSeg(id, attr, val) { document.querySelectorAll(`#${id} button`).forEach((b) => b.setAttribute("aria-pressed", b.dataset[attr] === String(val) ? "true" : "false")); }
  function updateQuote() {
    const t = S.tickers[S.coin], d = S.data && S.data.coin === S.coin ? S.data : null;
    const px = d ? d.price : t && t.last;
    $("q-price").textContent = fmt(px);
    const ch = t && t.change_pct;
    $("q-chg").textContent = ch == null ? " " : `${F.pct(ch)} 24h`;
    $("q-chg").className = "tnum mk-chg " + (ch == null ? "" : ch >= 0 ? "c-long" : "c-short");
    const b = d && d.book && d.book.available ? d.book : null;
    $("q-spread").textContent = b ? `${b.spread_bps.toFixed(2)} bps` : "--";
    $("q-spread").title = b ? `${fmt(b.spread)} between the best bid and ask` : "";
    $("q-reach").textContent = b ? `−${b.reach_pct.bids.toFixed(2)}% / +${b.reach_pct.asks.toFixed(2)}%` : "--";
    $("q-book").textContent = b ? `${F.usd(b.total_usd.bids)} bids · ${F.usd(b.total_usd.asks)} asks` : "--";
  }

  // ------------------------------------------------------------------ data
  async function load(quiet) {
    if (S.loading) return;
    S.loading = true;
    const seq = ++S.seq, sym = S.coin, tf = S.tf;
    let d;
    try { d = await F.getJSON(`/api/liquidity/map?coin=${enc(sym)}&tf=${tf}`); }
    catch (e) { S.loading = false; if (seq === S.seq) setLive(false, "Connection lost, retrying…"); return; }
    S.loading = false;
    if (seq !== S.seq || sym !== S.coin || tf !== S.tf) return;
    if (!d.ok) {
      setLive(false, d.error || "The scanner did not answer, retrying…");
      if (!S.data) $("say").textContent = d.error || "The scanner is not available right now. It retries automatically.";
      return;
    }
    S.data = d;
    S.fetchedAt = Date.now() - (d.age_sec || 0) * 1000;
    setLive(true);
    render();
  }
  function schedule() {
    clearInterval(S.timer);
    S.timer = setInterval(() => { if (!S.paused && visible()) load(true); }, S.every * 1000);
  }
  function setLive(ok, why) {
    S.err = ok ? null : why;
    $("live-dot").className = "mk-live " + (S.paused ? "" : ok ? "on" : "err");
    tick();
  }
  function tick() {
    const t = $("live-text");
    if (S.paused) { t.textContent = "Paused"; return; }
    if (S.err) { t.textContent = S.err; return; }
    if (!S.fetchedAt) { t.textContent = "Connecting…"; return; }
    const age = Math.max(0, Math.round((Date.now() - S.fetchedAt) / 1000));
    t.textContent = `Live · updated ${age < 2 ? "just now" : age + "s ago"} · every ${S.every}s`;
  }

  // ------------------------------------------------------------------ render everything
  function render() {
    const d = S.data;
    if (!d) return;
    updateQuote();
    renderSummary(d); renderDepth(d); renderBands(d); renderLadder(d); renderTrades(d); renderLiq(d); renderLevels(d); renderSweep(d); renderCards(d);
    $("sum-time").textContent = `${TF_LABEL[d.timeframe]} levels`;
    $("sweep-tf").textContent = `${TF_LABEL[d.timeframe]} candles`;
  }
  function band(b, pct) { return b && b.bands ? b.bands.find((x) => x.pct === pct) : null; }
  function mainBand(b) {
    if (!b || !b.available) return null;
    const cov = b.bands.filter((x) => x.covered && x.imbalance != null);
    return cov.find((x) => x.pct === 1) || cov[cov.length - 1] || b.bands[0];
  }
  function nearestWall(d, side) {
    const b = d.book;
    if (!b || !b.available) return null;
    return b.walls.filter((w) => w.side === side && w.is_wall).sort((a, c) => a.distance_pct - c.distance_pct)[0] || null;
  }
  function renderSummary(d) {
    const b = d.book, tr = d.trades, sc = d.scanner || {}, lq = d.liquidations, sw = d.levels && d.levels.swing;
    const parts = [];
    if (d.asset === "forex") parts.push("Forex and gold have no public order book or trade tape, so this page shows price-based levels: the recent range, sweeps and stop pools.");
    const mb = mainBand(b);
    if (mb && mb.imbalance != null) {
      const share = Math.round((1 + mb.imbalance) / 2 * 100);
      const who = share >= 58 ? "<b class=\"c-long\">Buyers are stronger</b> near the price" : share <= 42 ? "<b class=\"c-short\">Sellers are stronger</b> near the price" : "The order book is <b>about balanced</b>";
      parts.push(`${who}: ${share}% of the visible orders within ±${mb.pct}% are bids.`);
    }
    if (tr && tr.available && tr.buy_pct != null) {
      const span = tr.span_sec >= 120 ? `${Math.round(tr.span_sec / 60)} minutes` : `${Math.round(tr.span_sec)} seconds`;
      parts.push(`In the last ${span}, ${tr.buy_pct >= 55 ? "<b class=\"c-long\">buyers took</b>" : tr.buy_pct <= 45 ? "<b class=\"c-short\">sellers took</b>" : "trades were split,"} ${tr.buy_pct >= 55 ? tr.buy_pct.toFixed(0) + "% of the traded volume" : tr.buy_pct <= 45 ? (100 - tr.buy_pct).toFixed(0) + "% of the traded volume" : `${tr.buy_pct.toFixed(0)}% buys`}.`);
    }
    const wa = nearestWall(d, "ASK"), wb = nearestWall(d, "BID");
    if (wa || wb) parts.push(`Nearest big walls: ${wa ? `sell <b>${fmt(wa.price)}</b> (+${wa.distance_pct.toFixed(2)}%, ${F.usd(wa.usd)})` : "no big sell wall"}${wb ? `, buy <b>${fmt(wb.price)}</b> (−${wb.distance_pct.toFixed(2)}%, ${F.usd(wb.usd)})` : ", no big buy wall"}.`);
    const swp = sw && sw.available && sw.sweeps && sw.sweeps[0];
    if (swp) parts.push(`${swp.forming ? "The current candle" : swp.bars_ago === 1 ? "The last closed candle" : `A candle ${swp.bars_ago} bars ago`} <b>swept the ${swp.side === "HIGH" ? "recent high" : "recent low"}</b> at ${fmt(swp.level)} and closed back ${swp.side === "HIGH" ? "below" : "above"} it.`);
    if (lq && lq.available) {
      const up = lq.above[0], dn = lq.below[0];
      if (up || dn) parts.push(`Estimated liquidations cluster ${up ? `above at <b>${fmt(up.price)}</b> (+${up.distance_pct.toFixed(1)}%)` : ""}${up && dn ? " and " : ""}${dn ? `below at <b>${fmt(dn.price)}</b> (${dn.distance_pct.toFixed(1)}%)` : ""}.`);
    }
    $("say").innerHTML = parts.length ? parts.join(" ") : "Not enough data to read this market right now.";

    const kp = [];
    const k = (label, val, sub, cls, tipKey, bar) => kp.push(`<div class="mk-tile"><dt>${label}${tipKey ? F.tip(tipKey, label) : ""}</dt><dd class="tnum ${cls || ""}">${val}<small>${sub || ""}</small>${bar != null ? `<span class="lq-bar"><i style="width:${Math.max(0, Math.min(100, bar))}%"></i></span>` : ""}</dd></div>`);
    if (mb && mb.imbalance != null) { const s = (1 + mb.imbalance) / 2 * 100; k(`Bids within ±${mb.pct}%`, `${s.toFixed(0)}%`, `${F.usd(mb.bid_usd)} vs ${F.usd(mb.ask_usd)}`, s >= 58 ? "c-long" : s <= 42 ? "c-short" : "", "imbalance", s); }
    else k("Book balance", "--", d.asset === "forex" ? "no order book for forex" : "no order book data", "", "imbalance");
    if (tr && tr.available) k("Taker buys", `${tr.buy_pct.toFixed(0)}%`, `last ${tr.count} trades`, tr.buy_pct >= 55 ? "c-long" : tr.buy_pct <= 45 ? "c-short" : "", "taker_flow", tr.buy_pct);
    else k("Taker buys", "--", "no trade data", "", "taker_flow");
    const ms = sc.market_strength || {};
    k("Market strength", num(ms.score) ? `${Math.round(ms.score)}/100` : "--", ms.label ? ms.label.charAt(0) + ms.label.slice(1).toLowerCase() : "no data", ms.bias === "BUY" ? "c-long" : ms.bias === "SELL" ? "c-short" : "", "market_strength");
    const fo = sc.funding_open_interest || {};
    const nf = fo.next_funding_ts ? Math.max(0, Math.round((fo.next_funding_ts - Date.now()) / 60000)) : null;
    k("Funding rate", num(fo.funding_rate_pct) ? `${F.signed(fo.funding_rate_pct, 4)}%` : "--",
      num(fo.funding_rate_pct) ? `${fo.funding_rate_pct >= 0 ? "longs pay shorts" : "shorts pay longs"}${nf != null ? ` · next in ${nf >= 60 ? Math.floor(nf / 60) + "h " : ""}${nf % 60}m` : ""}` : "no perpetual market", "", "funding");
    k("Open interest", num(fo.open_interest) ? Number(fo.open_interest).toLocaleString("en-US", { maximumFractionDigits: 0 }) : "--", num(fo.open_interest) ? `${F.base(S.coin)} in open futures` : "no perpetual market", "", "open_interest");
    const cr = sc.crash_risk || {};
    k("Crash risk", num(cr.score) ? `${cr.score}/100` : "--", cr.label ? cr.label.charAt(0) + cr.label.slice(1).toLowerCase() : "no data", cr.label === "ELEVATED" ? "c-short" : cr.label === "WATCH" ? "c-amber" : "", "crash_risk");
    $("kpis").innerHTML = kp.join("");
  }

  // ------------------------------------------------------------------ depth chart (SVG, hover for details)
  function renderDepth(d) {
    const box = $("depth"), b = d.book;
    if (!b || !b.available) { box.innerHTML = `<div class="lq-unavail">${F.esc((b && b.reason) || "No order book for this market.")}</div>`; return; }
    const W = Math.max(280, box.clientWidth || 600), H = Math.max(200, box.clientHeight || 290);
    const padL = 8, padR = 56, padT = 12, padB = 26;
    const mid = b.mid, maxReach = Math.max(b.reach_pct.bids, b.reach_pct.asks);
    const win = S.win > 0 ? Math.min(S.win, maxReach) : maxReach;
    const lo = mid * (1 - win / 100), hi = mid * (1 + win / 100);
    const bids = b.depth.bids.filter((p) => p[0] >= lo), asks = b.depth.asks.filter((p) => p[0] <= hi);
    const ymax = Math.max(1, ...bids.map((p) => p[1]), ...asks.map((p) => p[1]));
    const X = (p) => padL + ((p - lo) / (hi - lo)) * (W - padL - padR), Y = (u) => padT + (1 - u / ymax) * (H - padT - padB);
    const step = (pts, side) => {
      if (!pts.length) return "";
      let dd = `M${X(side === "b" ? b.bid : b.ask).toFixed(1)} ${Y(0).toFixed(1)}`;
      let prevY = Y(0);
      pts.forEach((p) => { const x = X(p[0]).toFixed(1), y = Y(p[1]).toFixed(1); dd += `L${x} ${prevY}L${x} ${y}`; prevY = y; });
      const endX = X(side === "b" ? lo : hi).toFixed(1);
      return dd + `L${endX} ${prevY}L${endX} ${Y(0).toFixed(1)}Z`;
    };
    const L = getComputedStyle(document.documentElement);
    const long = L.getPropertyValue("--long").trim(), short = L.getPropertyValue("--short").trim(), amber = L.getPropertyValue("--amber").trim(), line = L.getPropertyValue("--line").trim();
    const ticks = [];
    for (let i = 0; i <= 4; i++) { const p = lo + ((hi - lo) * i) / 4; ticks.push(`<text class="axis" x="${X(p).toFixed(1)}" y="${H - 8}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${i === 2 ? fmt(p) : F.pct((p / mid - 1) * 100, 2)}</text>`); }
    const yt = [0.5, 1].map((f) => `<text class="axis" x="${W - padR + 6}" y="${(Y(ymax * f) + 4).toFixed(1)}">${F.usd(ymax * f)}</text><line x1="${padL}" x2="${W - padR}" y1="${Y(ymax * f).toFixed(1)}" y2="${Y(ymax * f).toFixed(1)}" stroke="${line}" stroke-dasharray="2 4"/>`).join("");
    const cumAt = (side, price) => { const arr = side === "BID" ? b.depth.bids : b.depth.asks; let c = 0; for (const p of arr) { if (side === "BID" ? p[0] >= price : p[0] <= price) c = p[1]; else break; } return c; };
    const walls = b.walls.filter((w) => w.is_wall && w.price >= lo && w.price <= hi).slice(0, 6).map((w) => {
      const x = X(w.price), y = Y(cumAt(w.side, w.price));
      return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="4.5" fill="${amber}" stroke="var(--surface)" stroke-width="2"/><text class="axis" x="${x.toFixed(1)}" y="${(y - 9).toFixed(1)}" text-anchor="middle" style="fill:${amber};font-weight:600">${F.usd(w.usd)}</text>`;
    }).join("");
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Order book depth chart for ${F.esc(S.coin)}">
      ${yt}
      <path d="${step(bids, "b")}" fill="${long}" fill-opacity="0.18" stroke="${long}" stroke-width="1.6"/>
      <path d="${step(asks, "a")}" fill="${short}" fill-opacity="0.18" stroke="${short}" stroke-width="1.6"/>
      <line x1="${X(mid).toFixed(1)}" x2="${X(mid).toFixed(1)}" y1="${padT}" y2="${H - padB}" stroke="var(--text)" stroke-opacity="0.45" stroke-dasharray="3 3"/>
      ${walls}${ticks.join("")}
      <line id="dp-x" x1="0" x2="0" y1="${padT}" y2="${H - padB}" stroke="var(--text)" stroke-opacity="0.5" visibility="hidden"/>
    </svg><div class="lq-tt" id="dp-tt" hidden></div>`;
    const svg = box.querySelector("svg"), tt = $("dp-tt"), cross = svg.querySelector("#dp-x");
    const onMove = (cx) => {
      const r = svg.getBoundingClientRect(), x = ((cx - r.left) / r.width) * W;
      if (x < padL || x > W - padR) { tt.hidden = true; cross.setAttribute("visibility", "hidden"); return; }
      const p = lo + ((x - padL) / (W - padL - padR)) * (hi - lo), side = p < mid ? "BID" : "ASK", cum = cumAt(side, p);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      tt.hidden = false;
      tt.innerHTML = `<b>${fmt(p)}</b> <span class="c-dim">(${F.pct((p / mid - 1) * 100, 2)})</span><br>${side === "BID" ? "Bids from here up to the price" : "Asks from the price up to here"}: <b class="${side === "BID" ? "c-long" : "c-short"}">${F.usd(cum)}</b>`;
      tt.style.left = `${Math.max(90, Math.min(r.width - 90, cx - r.left))}px`;
      tt.style.top = `${Math.max(40, Y(cum) / H * r.height)}px`;
    };
    svg.addEventListener("mousemove", (e) => onMove(e.clientX));
    svg.addEventListener("touchmove", (e) => { if (e.touches[0]) onMove(e.touches[0].clientX); }, { passive: true });
    svg.addEventListener("mouseleave", () => { tt.hidden = true; cross.setAttribute("visibility", "hidden"); });
  }
  function renderBands(d) {
    const b = d.book, box = $("bands");
    if (!b || !b.available) { box.innerHTML = ""; return; }
    box.innerHTML = b.bands.map((x) => {
      if (!x.covered || x.imbalance == null) return `<div class="lq-band na"><span>±${x.pct}%</span><span class="mk-note">beyond the visible book</span><b>--</b></div>`;
      const s = (1 + x.imbalance) / 2 * 100;
      return `<div class="lq-band"><span>±${x.pct}%</span><span class="lq-bar"><i style="width:${s.toFixed(1)}%"></i></span><b class="${s >= 58 ? "c-long" : s <= 42 ? "c-short" : ""}">${s.toFixed(0)}% bids</b></div>`;
    }).join("");
  }

  // ------------------------------------------------------------------ book map (ladder)
  function renderLadder(d) {
    const b = d.book, box = $("ladder");
    if (!b || !b.available || !b.ladder || !b.ladder.rows.length) { box.innerHTML = `<div class="lq-unavail">${F.esc((b && b.reason) || "No order book for this market.")}</div>`; $("map-step").textContent = ""; return; }
    const rows = b.ladder.rows, max = Math.max(1, ...rows.map((r) => r.usd));
    $("map-step").textContent = `steps of ${fmt(b.ladder.step)}`;
    const wallIn = (r) => b.walls.filter((w) => w.is_wall && ((w.side === "BID" && r.side === "bid") || (w.side === "ASK" && r.side === "ask")) && w.price >= r.lo && w.price <= r.hi);
    let html = "";
    let nowDone = false;
    rows.forEach((r) => {
      if (!nowDone && r.side === "bid") {
        html += `<div class="lq-lrow now"><span class="p">${fmt(b.mid)}</span><span></span><span></span></div>`;
        nowDone = true;
      }
      const w = (r.usd / max) * 100, tags = wallIn(r).map((x) => `<span class="lq-tag ${x.side === "BID" ? "wall-b" : "wall-a"}">${x.side === "BID" ? "Buy" : "Sell"} wall ${F.usd(x.usd)}</span>`).join("");
      html += `<div class="lq-lrow" title="${fmt(r.lo)} – ${fmt(r.hi)}: ${F.usd(r.usd)}"><span class="p">${fmt(r.side === "ask" ? r.lo : r.hi)}</span>
        <span class="bb">${r.side === "bid" && r.usd > 0 ? `<i style="width:${w.toFixed(1)}%"></i>` : ""}</span>
        <span class="ba">${r.side === "ask" && r.usd > 0 ? `<i style="width:${w.toFixed(1)}%"></i>` : ""}</span>${tags ? `<span class="tag">${tags}</span>` : ""}</div>`;
    });
    box.innerHTML = html;
  }

  // ------------------------------------------------------------------ trades
  function renderTrades(d) {
    const t = d.trades, box = $("trades");
    if (!t || !t.available) { box.innerHTML = `<div class="lq-unavail">${F.esc((t && t.reason) || "No trade data.")}</div>`; $("trades-span").textContent = ""; return; }
    $("trades-span").textContent = `last ${t.count} trades · ${t.span_sec >= 120 ? Math.round(t.span_sec / 60) + " min" : Math.round(t.span_sec) + "s"}`;
    const bp = t.buy_pct;
    const pts = t.delta || [], vals = pts.map((p) => p[1]);
    let spark = "";
    if (vals.length > 1) {
      const mn = Math.min(0, ...vals), mx = Math.max(0, ...vals), rng = mx - mn || 1, w = 300, h = 54;
      const xy = vals.map((v, i) => `${((i / (vals.length - 1)) * w).toFixed(1)},${(h - ((v - mn) / rng) * h).toFixed(1)}`).join(" ");
      const zero = (h - ((0 - mn) / rng) * h).toFixed(1), end = vals[vals.length - 1];
      spark = `<svg class="lq-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-label="Buy minus sell volume over the recent trades"><line x1="0" x2="${w}" y1="${zero}" y2="${zero}" stroke="var(--line)" stroke-dasharray="3 3"/><polyline points="${xy}" fill="none" stroke="${end >= 0 ? "var(--long)" : "var(--short)"}" stroke-width="2"/></svg>`;
    }
    const time = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    box.innerHTML = `<div class="lq-flow">
        <div class="lq-flowbar" aria-label="Taker buys ${bp.toFixed(0)}%"><span class="b" style="width:${bp}%">Buys ${bp.toFixed(0)}% · ${F.usd(t.buy_usd)}</span><span class="s" style="width:${100 - bp}%">${F.usd(t.sell_usd)} · ${(100 - bp).toFixed(0)}% sells</span></div>
        <p class="mk-note">Average trade ${F.usd(t.avg_usd)}. A trade counts as big above ${F.usd(t.threshold_usd)} (top 2%): big buys ${F.usd(t.large_buy_usd)}, big sells ${F.usd(t.large_sell_usd)}.</p>
        ${spark}<p class="mk-note">Running buy minus sell volume over these trades.</p>
      </div>
      <h3 class="mk-h2" style="font-size:14.5px;margin:14px 0 0">Big trades{{TIP}}</h3>
      ${t.large.length ? `<table class="lq-tape"><thead><tr><th>Time</th><th>Side</th><th>Price</th><th>Size</th></tr></thead><tbody>
        ${t.large.map((x) => `<tr><td>${time(x.time)}</td><td class="${x.side === "buy" ? "c-long" : "c-short"}">${x.side === "buy" ? "Buy" : "Sell"}</td><td>${fmt(x.price)}</td><td><b>${F.usd(x.usd)}</b></td></tr>`).join("")}</tbody></table>`
        : `<p class="mk-empty" style="margin-top:8px">No unusually big trades in this window.</p>`}`.replace("{{TIP}}", F.tip("large_trades", "big trades"));
  }

  // ------------------------------------------------------------------ estimated liquidations
  function renderLiq(d) {
    const q = d.liquidations, box = $("liq");
    if (!q || !q.available) { box.innerHTML = `<div class="lq-unavail">${F.esc((q && q.reason) || "Not available.")}</div>`; return; }
    const price = d.price, group = 4, rows = [];
    const heat = q.heat.slice().sort((a, b) => b.lo - a.lo);
    for (let i = 0; i < heat.length; i += group) {
      const g = heat.slice(i, i + group);
      rows.push({ lo: Math.min(...g.map((x) => x.lo)), hi: Math.max(...g.map((x) => x.hi)), long: g.reduce((s, x) => s + x.long, 0), short: g.reduce((s, x) => s + x.short, 0) });
    }
    const max = Math.max(0.0001, ...rows.map((r) => r.long + r.short));
    let html = "", nowDone = false;
    rows.forEach((r) => {
      if (!nowDone && r.hi <= price) { html += `<div class="lq-hrow now"><span class="p">${fmt(price)}</span><span class="h"></span></div>`; nowDone = true; }
      const w = ((r.long + r.short) / max) * 100, col = r.short > r.long ? "var(--short)" : "var(--long)";
      html += `<div class="lq-hrow" title="${fmt(r.lo)} – ${fmt(r.hi)}"><span class="p">${fmt((r.lo + r.hi) / 2)}</span><span class="h"><i style="width:${w.toFixed(1)}%;background:${col};opacity:${(0.35 + 0.65 * w / 100).toFixed(2)}"></i></span></div>`;
    });
    if (!nowDone) html += `<div class="lq-hrow now"><span class="p">${fmt(price)}</span><span class="h"></span></div>`;
    const up = q.above[0], dn = q.below[0];
    box.innerHTML = `<p class="mk-note" style="margin:0 0 10px">${up ? `Biggest estimated cluster above: <b class="c-short">${fmt(up.price)}</b> (+${up.distance_pct.toFixed(2)}%, shorts would be squeezed). ` : ""}${dn ? `Below: <b class="c-long">${fmt(dn.price)}</b> (${dn.distance_pct.toFixed(2)}%, longs would be flushed). ` : ""}Estimated long share ${q.long_share_pct}%.</p>
      <div class="lq-heat">${html}</div>
      <table class="lq-lev"><thead><tr><th>Leverage</th><th>A long opened now is closed at</th><th>A short opened now is closed at</th></tr></thead><tbody>
      ${q.by_leverage.map((x) => `<tr><td><b>${x.leverage}x</b></td><td class="c-long">${fmt(x.long_liq_now)} <span class="c-dim">(${F.pct((x.long_liq_now / price - 1) * 100, 1)})</span></td><td class="c-short">${fmt(x.short_liq_now)} <span class="c-dim">(${F.pct((x.short_liq_now / price - 1) * 100, 1)})</span></td></tr>`).join("")}</tbody></table>
      <p class="mk-note" style="margin-top:8px">Model: each candle's volume is treated as new positions at 10x/25x/50x/100x, ${q.maintenance_margin_pct}% maintenance margin; levels price already crossed are removed. Red = shorts' liquidations (above), green = longs' (below).</p>`;
  }

  // ------------------------------------------------------------------ key levels list
  function renderLevels(d) {
    const box = $("levels"), price = d.price, items = [];
    const add = (p, label, sub, color, strength) => { if (num(p) && Math.abs(p / price - 1) <= 0.15) items.push({ p, label, sub, color, strength }); };
    const b = d.book, sc = d.scanner || {}, sw = d.levels && d.levels.swing;
    if (b && b.available) b.walls.filter((w) => w.is_wall).slice(0, 6).forEach((w) => add(w.price, w.side === "BID" ? "Buy wall" : "Sell wall", `${F.usd(w.usd)} · ${w.x_median}× a normal level`, w.side === "BID" ? "var(--long)" : "var(--short)"));
    if (sc.magnet) add(sc.magnet.price, "Liquidity magnet", `${F.usd(sc.magnet.usd_size)} cluster, ${sc.magnet.side === "SUPPORT" ? "support" : "resistance"}`, "#60a5fa");
    if (sc.likely_target) add(sc.likely_target.price, "Likely target", `${sc.likely_target.type}, score ${Math.round(sc.likely_target.score)}/100`, "#60a5fa");
    if (sw && sw.available) { add(sw.swing_high, "Range high", `top of the last ${sw.lookback} candles · stops above`, "var(--amber)"); add(sw.swing_low, "Range low", `bottom of the last ${sw.lookback} candles · stops below`, "var(--amber)"); }
    (d.levels.pools || []).forEach((p) => add(p.price, p.type === "EQH" ? "Equal highs" : "Equal lows", `${p.touches} touches · ${p.type === "EQH" ? "buy stops above" : "sell stops below"}`, "var(--amber)"));
    const q = d.liquidations;
    if (q && q.available) {
      q.above.slice(0, 2).forEach((c) => add(c.price, "Est. short liquidations", `intensity ${Math.round(c.intensity * 100)}%`, "#a78bfa"));
      q.below.slice(0, 2).forEach((c) => add(c.price, "Est. long liquidations", `intensity ${Math.round(c.intensity * 100)}%`, "#a78bfa"));
    }
    if (!items.length) { box.innerHTML = `<div class="lq-unavail">No levels within 15% of the price.</div>`; return; }
    const above = items.filter((x) => x.p >= price).sort((a, c) => c.p - a.p), below = items.filter((x) => x.p < price).sort((a, c) => c.p - a.p);
    const row = (x) => `<div class="lq-lv"><i class="dot" style="background:${x.color}"></i><span><b>${F.esc(x.label)}</b><small>${F.esc(x.sub)}</small></span><span class="r"><b>${fmt(x.p)}</b><small>${F.pct((x.p / price - 1) * 100, 2)}</small></span></div>`;
    box.innerHTML = above.slice(-7).map(row).join("") + `<div class="lq-lv now"><i class="dot" style="background:var(--text)"></i><span><b>Price now</b><small>mid of the best bid and ask</small></span><span class="r"><b>${fmt(price)}</b></span></div>` + below.slice(0, 7).map(row).join("");
  }

  // ------------------------------------------------------------------ sweeps + stop pools
  function renderSweep(d) {
    const sw = d.levels && d.levels.swing, box = $("sweep");
    if (!sw || !sw.available) { box.innerHTML = `<div class="lq-unavail">Not enough candles.</div>`; $("pools").innerHTML = ""; return; }
    const pos = Math.max(2, Math.min(98, sw.position_pct));
    const ev = sw.sweeps || [];
    box.innerHTML = `<div class="lq-range" aria-label="Price inside the recent range">
        <span class="track"></span><span class="mark" style="left:${pos}%"></span>
        <span class="lab now" style="left:${pos}%">${fmt(sw.price)}</span>
        <span class="lab l" style="left:0">${fmt(sw.swing_low)} (−${sw.to_low_pct.toFixed(2)}%)</span><span class="lab r">${fmt(sw.swing_high)} (+${sw.to_high_pct.toFixed(2)}%)</span>
      </div>
      ${ev.length ? ev.map((e) => `<p class="eng-copy">${e.forming ? "<b class=\"c-amber\">Now:</b> the current candle" : `<b>${e.bars_ago} candle${e.bars_ago === 1 ? "" : "s"} ago:</b> a candle`} took the ${e.side === "HIGH" ? "high" : "low"} at <b>${fmt(e.level)}</b> (wick to ${fmt(e.extreme)}) and ${e.forming ? "is" : "closed"} back ${e.side === "HIGH" ? "below" : "above"} it at ${fmt(e.close)}. ${e.side === "HIGH" ? "Stops above were taken; this often marks a short-term top." : "Stops below were taken; this often marks a short-term bottom."}${e.forming ? " Not confirmed until the candle closes." : ""}</p>`).join("")
        : `<p class="eng-copy">No sweep in the last 6 candles. Price is ${sw.position_pct.toFixed(0)}% of the way from the range low to the range high.</p>`}`;
    const pools = d.levels.pools || [];
    $("pools").innerHTML = pools.length ? `<div class="lq-levels">${pools.map((p) => `<div class="lq-lv"><i class="dot" style="background:var(--amber)"></i><span><b>${p.type === "EQH" ? "Equal highs" : "Equal lows"} ×${p.touches}</b><small>${p.type === "EQH" ? "buy stops likely just above" : "sell stops likely just below"}</small></span><span class="r"><b>${fmt(p.price)}</b><small>${F.pct(p.distance_pct, 2)}</small></span></div>`).join("")}</div>`
      : `<p class="mk-empty">No untaken equal highs or lows near the price on ${TF_LABEL[d.timeframe]} candles.</p>`;
  }

  // ------------------------------------------------------------------ scanner cards (same readings as the Pro terminal)
  function renderCards(d) {
    const sc = d.scanner || {}, box = $("cards");
    if (sc.error) { box.innerHTML = `<div class="lq-unavail" style="grid-column:1/-1">${F.esc(sc.error)}</div>`; return; }
    const card = (title, tipKey, body, wide) => `<div class="lq-c${wide ? " w2" : ""}"><h3>${title}${tipKey ? F.tip(tipKey, title) : ""}</h3>${body}</div>`;
    const mg = sc.magnet || {}, tg = sc.likely_target || {};
    const magnet = card("Liquidity magnet and likely target", "liquidity_magnet", mg.price ? `<div class="lq-pair">
        <div><span>Magnet (biggest cluster)</span><b class="tnum">${fmt(mg.price)}</b><small>${F.usd(mg.usd_size)} · ${num(mg.distance_pct) ? mg.distance_pct.toFixed(2) : "--"}% away · ${mg.side === "SUPPORT" ? "support" : "resistance"}</small></div>
        <div><span>Likely target${F.tip("likely_target", "likely target")}</span><b class="tnum">${fmt(tg.price)}</b><small>${F.esc(tg.type || "")} · score ${num(tg.score) ? Math.round(tg.score) : "--"}/100 · ${num(tg.distance_pct) ? tg.distance_pct.toFixed(2) : "--"}% away</small></div></div>`
      : `<p class="mk-empty">No order-book clusters for this market.</p>`, true);
    const ms = sc.market_strength || {};
    const sv = num(ms.score) ? ms.score : null, ang = sv == null ? 0 : (sv / 100) * 180;
    const gx = 60 + 46 * Math.cos(Math.PI - (ang * Math.PI) / 180), gy = 60 - 46 * Math.sin(Math.PI - (ang * Math.PI) / 180);
    const strength = card("Market strength", "market_strength", `<div class="lq-gauge"><svg viewBox="0 0 120 66"><path d="M14 60 A46 46 0 0 1 106 60" fill="none" stroke="var(--surface-2)" stroke-width="10" stroke-linecap="round"/>
        ${sv == null ? "" : `<path d="M14 60 A46 46 0 0 1 ${gx.toFixed(1)} ${gy.toFixed(1)}" fill="none" stroke="${sv >= 55 ? "var(--long)" : sv <= 45 ? "var(--short)" : "var(--amber)"}" stroke-width="10" stroke-linecap="round"/>`}</svg>
        <b class="${sv >= 55 ? "c-long" : sv <= 45 ? "c-short" : ""}">${sv == null ? "--" : Math.round(sv)}</b></div><p class="mk-note" style="text-align:center">${F.esc(ms.label ? ms.label.charAt(0) + ms.label.slice(1).toLowerCase() : "No data")} · pressure, order flow and depth</p>`);
    const sp = sc.possible_spoofing || {};
    let spBody;
    if (!sp.available) spBody = `<p class="mk-empty">Needs order-book data.</p>`;
    else if (sp.note) spBody = `<p class="mk-empty">${F.esc(sp.note === "Collecting baseline snapshot..." ? "Comparing two snapshots of the book; ready on the next refresh." : sp.note)}</p>`;
    else if (sp.spoof_detected && sp.top_vanished_level) { const t = sp.top_vanished_level; spBody = `<p class="lq-big c-amber">${fmt(t.price)}</p><p class="mk-note">${F.usd(t.usd_size_before)} pulled within ${Math.round(t.seconds_ago)}s (${Math.round(t.cancelled_pct)}% of it). ${sp.vanished_count} level${sp.vanished_count === 1 ? "" : "s"} vanished.</p>`; }
    else spBody = `<p class="lq-big">None</p><p class="mk-note">The biggest orders near the price stayed in place between snapshots.</p>`;
    const spoof = card("Possible spoofing", "spoofing", spBody);
    const ts = sc.trap_squeeze || {};
    const meter = (label, v, col) => `<div class="lq-meter"><span>${label}</span><span class="t"><i style="width:${num(v) ? v : 0}%;background:${col}"></i></span><b>${num(v) ? v : "--"}</b></div>`;
    const trap = card("Trap and squeeze risk", "trap", meter("Bull trap", ts.bull_trap, "var(--short)") + meter("Bear trap", ts.bear_trap, "var(--long)") + meter("Short squeeze", ts.short_squeeze, "var(--long)") + meter("Long squeeze", ts.long_squeeze, "var(--short)")
      + `<p class="mk-note">0–100 hints from sweeps, order flow, walls and funding. Not a forecast.</p>`, true);
    const zones = sc.liquidity_zones || [];
    const zoneCard = card("Liquidity target zones", "wall", zones.length ? `<table class="lq-tape"><thead><tr><th>Wall</th><th>Price</th><th>Size</th><th>Away</th><th>Score</th></tr></thead><tbody>
        ${zones.slice(0, 8).map((z) => `<tr><td class="${z.side === "BUY_WALL" ? "c-long" : "c-short"}">${z.side === "BUY_WALL" ? "Buy" : "Sell"}</td><td>${fmt(z.price)}</td><td>${F.usd(z.usd_size)}</td><td>${num(z.distance_pct) ? Math.abs(z.distance_pct).toFixed(2) + "%" : "--"}</td><td><b>${Math.round(z.score)}</b></td></tr>`).join("")}</tbody></table>`
      : `<p class="mk-empty">No order-book walls for this market.</p>`, true);
    const fo = sc.funding_open_interest || {};
    const funding = card("Funding and open interest", "funding", fo.available ? `<div class="lq-pair"><div><span>Funding rate</span><b class="${fo.funding_rate_pct >= 0 ? "c-long" : "c-short"}">${num(fo.funding_rate_pct) ? F.signed(fo.funding_rate_pct, 4) + "%" : "--"}</b><small>${num(fo.funding_rate_pct) ? (fo.funding_rate_pct >= 0 ? "longs pay shorts" : "shorts pay longs") : ""}</small></div>
        <div><span>Open interest${F.tip("open_interest", "open interest")}</span><b>${num(fo.open_interest) ? Number(fo.open_interest).toLocaleString("en-US", { maximumFractionDigits: 0 }) : "--"}</b><small>${F.esc(fo.perp_symbol || "")}</small></div></div>` : `<p class="mk-empty">No perpetual futures market for this pair.</p>`);
    const cv = sc.cvd || {}, ser = cv.series || [];
    let cspark = "";
    if (ser.length > 1) { const mn = Math.min(...ser), mx = Math.max(...ser), r = mx - mn || 1; cspark = `<svg class="lq-spark" viewBox="0 0 160 40" preserveAspectRatio="none"><polyline points="${ser.map((v, i) => `${((i / (ser.length - 1)) * 160).toFixed(1)},${(40 - ((v - mn) / r) * 40).toFixed(1)}`).join(" ")}" fill="none" stroke="${cv.trend === "RISING" ? "var(--long)" : cv.trend === "FALLING" ? "var(--short)" : "var(--dim)"}" stroke-width="2"/></svg>`; }
    const cvd = card("Volume delta (CVD)", "cvd", `<p class="lq-big ${cv.trend === "RISING" ? "c-long" : cv.trend === "FALLING" ? "c-short" : ""}">${cv.trend ? cv.trend.charAt(0) + cv.trend.slice(1).toLowerCase() : "--"}</p>${cspark}<p class="mk-note">From candle direction on ${TF_LABEL[d.timeframe]} candles (an approximation). The real taker flow is in "Taker flow and big trades".</p>`);
    const cr = sc.crash_risk || {};
    const crash = card("Crash risk", "crash_risk", `<p class="lq-big ${cr.label === "ELEVATED" ? "c-short" : cr.label === "WATCH" ? "c-amber" : "c-long"}">${num(cr.score) ? cr.score : "--"}<span style="font-size:14px;font-family:var(--body);font-weight:600;margin-left:8px">${F.esc(cr.label ? cr.label.charAt(0) + cr.label.slice(1).toLowerCase() : "")}</span></p>
      ${(cr.factors || []).length ? `<ul class="lq-factors">${cr.factors.map((f) => `<li>${F.esc(f)}</li>`).join("")}</ul>` : `<p class="mk-note">No stress factors right now.</p>`}`, true);
    box.innerHTML = magnet + strength + spoof + trap + funding + cvd + zoneCard + crash;
  }

  // ------------------------------------------------------------------ liquidity alerts (LIQUIDITY_WALL, LIQUIDITY_IMBALANCE)
  const AL = [["LIQUIDITY_WALL", "A big wall appears near the price", "an order-book wall scoring 80 or more out of 100"],
              ["LIQUIDITY_IMBALANCE", "The book turns one-sided", "70% or more of the orders within 1% on one side"]];
  async function loadAlerts() {
    const sym = S.coin;
    if (F.isForex(sym)) { $("al-box").innerHTML = `<p class="mk-empty">Liquidity alerts need an order book, so they are for crypto only.</p>`; return; }
    let d;
    try { d = await F.getJSON("/api/alerts"); } catch (e) { return; }
    if (sym !== S.coin) return;
    S.alerts = (d.alerts || []).filter((a) => a.symbol === sym && a.is_enabled);
    $("al-box").innerHTML = AL.map(([type, title, sub]) => {
      const on = S.alerts.find((a) => a.alert_type === type);
      return `<div class="lq-al"><span>${title}<small>${sub}</small></span><button type="button" class="mk-btn" data-type="${type}" aria-pressed="${!!on}"${on ? ` data-id="${on.id}"` : ""}>${on ? "On" : "Turn on"}</button></div>`;
    }).join("");
  }
  $("al-box").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-type]");
    if (!b) return;
    b.disabled = true;
    const r = b.dataset.id ? await F.sendJSON(`/api/alerts/${b.dataset.id}`, "DELETE") : await F.sendJSON("/api/alerts", "POST", { symbol: S.coin, alert_type: b.dataset.type });
    if (!r._ok) F.toast(r.error || "The alert could not be changed");
    else F.toast(b.dataset.id ? "Alert turned off" : "Alert turned on");
    loadAlerts();
  });

  // ------------------------------------------------------------------ wiring + start
  $("coin-btn").addEventListener("click", () => picker.open($("coin-btn")));
  $("recent").addEventListener("click", (e) => { const b = e.target.closest("[data-sym]"); if (b) selectCoin(b.dataset.sym); });
  document.querySelectorAll("#tf-seg button").forEach((b) => b.addEventListener("click", () => {
    S.tf = b.dataset.tf; F.store.set("sfm-liq-tf", S.tf); setSeg("tf-seg", "tf", S.tf);
    try { history.replaceState(null, "", `/liquidity-scanner?coin=${enc(S.coin)}&tf=${S.tf}`); } catch (e) {}
    S.fetchedAt = 0; setLive(true); load();
  }));
  document.querySelectorAll("#every-seg button").forEach((b) => b.addEventListener("click", () => {
    S.every = Number(b.dataset.every); F.store.set("sfm-liq-every", S.every); setSeg("every-seg", "every", S.every); schedule(); tick();
  }));
  document.querySelectorAll("#win-seg button").forEach((b) => b.addEventListener("click", () => {
    S.win = Number(b.dataset.win); F.store.set("sfm-liq-win", S.win); setSeg("win-seg", "win", S.win); if (S.data) renderDepth(S.data);
  }));
  $("pause-btn").addEventListener("click", () => {
    S.paused = !S.paused;
    $("pause-btn").setAttribute("aria-pressed", S.paused ? "true" : "false");
    $("pause-btn").querySelector("span").textContent = S.paused ? "Resume" : "Pause";
    $("pause-btn").querySelector("svg").innerHTML = S.paused ? '<path d="M8 5l11 7-11 7z"/>' : '<path d="M9 6v12M15 6v12"/>';
    setLive(!S.err);
    if (!S.paused) load(true);
  });
  let resizeT = null;
  window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (S.data) renderDepth(S.data); }, 150); });
  document.addEventListener("themechange", () => { if (S.data) render(); });
  document.addEventListener("visibilitychange", () => { if (visible() && !S.paused) load(true); });

  async function loadTickers() {
    try { const d = await F.getJSON("/api/market/tickers"); if (d.ok) { S.tickers = d.tickers || {}; picker.setTickers(S.tickers); updateQuote(); } } catch (e) {}
  }
  async function start() {
    let coins;
    try { coins = await F.getJSON("/coins"); } catch (e) { coins = {}; }
    S.coins = { crypto: coins.crypto || ["BTC/USDT"], forex: coins.forex || [] };
    picker.setCoins(S.coins);
    const qs = new URLSearchParams(location.search);
    const tf = qs.get("tf") || F.store.get("sfm-liq-tf") || "1h";
    S.tf = TF_LABEL[tf] ? tf : "1h";
    const ev = Number(F.store.get("sfm-liq-every"));
    S.every = [5, 10, 30].includes(ev) ? ev : 10;
    const w = Number(F.store.get("sfm-liq-win"));
    S.win = [0.25, 0.5, 1, 0].includes(w) ? w : 0;
    setSeg("tf-seg", "tf", S.tf); setSeg("every-seg", "every", S.every); setSeg("win-seg", "win", S.win);
    const want = (qs.get("coin") || F.store.get("sfm-coin") || "BTC/USDT").toUpperCase();
    selectCoin(picker.valid(want) ? want : "BTC/USDT", true);
    schedule();
    loadTickers();
    setInterval(() => { if (visible()) loadTickers(); }, 15000);
    setInterval(tick, 1000);
    try { const b = await F.getJSON("/api/signals/board"); if (b.ok) picker.setBoard(b); } catch (e) {}
  }
  start();
})();
