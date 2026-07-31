/* ==========================================================================
   SIGNAL/FM — dynamic 19-channel renderer
   ========================================================================== */

const coinSelect   = document.getElementById("coin-select");
const runBtn       = document.getElementById("get-signal-btn");
const errorText    = document.getElementById("error-text");
const resultBox    = document.getElementById("result-box");
const alertStack   = document.getElementById("alert-stack");
const emptyState   = document.getElementById("empty-state");
const clockText    = document.getElementById("clock-text");
const tier1El      = document.getElementById("tier-1");
const tier2El      = document.getElementById("tier-2");
const tier3El      = document.getElementById("tier-3");
const liqScannerEl = document.getElementById("liquidity-scanner");
const panelTabs    = document.querySelectorAll(".panel-tab");
const tabPanels    = document.querySelectorAll(".tab-panel");

// coin picker elements
const coinPicker        = document.getElementById("coin-picker");
const coinPickerTrigger = document.getElementById("coin-picker-trigger");
const coinPickerIcon    = document.getElementById("coin-picker-icon");
const coinPickerLabel   = document.getElementById("coin-picker-label");
const coinPickerMenu    = document.getElementById("coin-picker-menu");

// live chart elements
const chartTitleEl  = document.getElementById("chart-title");
const chartPriceEl  = document.getElementById("chart-price");
const chartChangeEl = document.getElementById("chart-change");
const chartStatusEl = document.getElementById("chart-status");
const chartTfRow    = document.getElementById("chart-tf-row");
const candleChartEl = document.getElementById("candle-chart");

const GAUGE_CIRCUMFERENCE = 2 * Math.PI * 48; // r=48

/* If any of these are missing, design.html doesn't match this script.js —
   make sure both files were replaced together and the browser isn't serving
   a cached copy. */
{
  const required = { coinSelect, runBtn, errorText, resultBox, alertStack, emptyState, clockText, tier1El, tier2El, tier3El, liqScannerEl };
  const missing = Object.keys(required).filter((k) => !required[k]);
  if (missing.length) {
    console.error("SIGNAL/FM: design.html is missing element(s) for:", missing.join(", "), "— check that templates/design.html matches this static/script.js and clear the browser cache.");
  }
}

/* ---------------------------- utils ---------------------------- */

const na = (v) => v === null || v === undefined || Number.isNaN(v);

function fmtPrice(v) {
  if (na(v)) return "--";
  return "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
}
function fmtPct(v, digits = 1) {
  if (na(v)) return "--";
  return Number(v).toFixed(digits) + "%";
}
function fmtNum(v, digits = 2) {
  if (na(v)) return "--";
  return Number(v).toFixed(digits);
}
function fmtSigned(v, digits = 2, suffix = "") {
  if (na(v)) return "--";
  const n = Number(v);
  return (n > 0 ? "+" : "") + n.toFixed(digits) + suffix;
}

function scopeTicks(seed) {
  const heights = [4, 8, 5, 11, 6];
  let out = "";
  for (let i = 0; i < 5; i++) {
    const h = heights[(i + seed) % heights.length];
    out += `<span style="height:${h}px"></span>`;
  }
  return `<span class="scope-ticks">${out}</span>`;
}

function channelCard({ id, title, model, body, span2 = false }) {
  return `
    <div class="channel-card${span2 ? " span-2" : ""}">
      <div class="channel-head">
        <div class="channel-id-group">
          ${scopeTicks(id)}
          <div>
            <div class="channel-id">CH.${String(id).padStart(2, "0")}</div>
            <div class="channel-title">${title}</div>
          </div>
        </div>
        <span class="channel-model">${model}</span>
      </div>
      ${body}
    </div>`;
}

function meterBar(pct, colorClass) {
  const clamped = Math.max(0, Math.min(100, pct));
  return `<div class="meter-track"><div class="meter-fill ${colorClass}" style="width:${clamped}%"></div></div>`;
}

function centeredMeter(value, max, colorClass) {
  if (na(value)) {
    return `<div class="meter-track centered"><div class="center-tick"></div></div>`;
  }
  const pct = Math.min(Math.abs(value), max) / max * 50;
  const style = value >= 0
    ? `left:50%; width:${pct}%;`
    : `left:${50 - pct}%; width:${pct}%;`;
  return `<div class="meter-track centered"><div class="center-tick"></div><div class="meter-fill ${value >= 0 ? "c-long" : "c-short"}" style="${style}"></div></div>`;
}

function badge(text, tone) {
  return `<span class="badge on-${tone}">${text}</span>`;
}

/* ---------------------------- tabs ---------------------------- */

function activatePanel(name) {
  panelTabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.panel === name));
  tabPanels.forEach((panel) => panel.classList.toggle("active", panel.id === "panel-" + name));
  if (name === "livechart") {
    ensureChartInitialized();
    resizeChart();
    loadChartData();
  } else if (name === "liquidity") {
    loadLiquidityRadarData();
    startLiquidityPolling();
  } else {
    stopLiquidityPolling();
  }
}

panelTabs.forEach((tab) => {
  tab.addEventListener("click", () => activatePanel(tab.dataset.panel));
});

/* ---------------------------- clock ---------------------------- */

function tickClock() {
  if (!clockText) return;
  const now = new Date();
  clockText.textContent = now.toLocaleTimeString("en-GB", { hour12: false });
}
tickClock();
setInterval(tickClock, 1000);

/* ---------------------------- alerts ---------------------------- */

function clearAlerts() { alertStack.innerHTML = ""; }
function addAlert(type, text) {
  const el = document.createElement("div");
  el.className = "alert-banner " + type;
  el.innerHTML = `<span class="alert-dot"></span><span>${text}</span>`;
  alertStack.appendChild(el);
}

/* ==========================================================================
   COIN PICKER — custom dropdown with logos, built on top of the hidden
   native <select id="coin-select">. script.js everywhere else keeps using
   coinSelect.value, so nothing downstream needs to change.
   ========================================================================== */

// Known ticker -> CoinCap icon slug overrides (most tickers map 1:1 already).
const COIN_ICON_OVERRIDES = {
  HYPE: "hype",
  GRAM: "gram",
  ASTER: "aster",
  ONDO: "ondo",
  TAO: "tao",
};

function coinIconUrl(ticker) {
  const slug = (COIN_ICON_OVERRIDES[ticker] || ticker).toLowerCase();
  return `https://assets.coincap.io/assets/icons/${slug}@2x.png`;
}

// Tiny inline SVG fallback (colored ring + ticker initials) used when a
// coin's logo can't be fetched from the icon CDN.
function fallbackIconDataUrl(ticker) {
  const initials = (ticker || "?").slice(0, 3).toUpperCase();
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">
    <circle cx="20" cy="20" r="19" fill="#151b24" stroke="#27303b" stroke-width="1.5"/>
    <text x="20" y="25" font-family="monospace" font-size="11" font-weight="700"
      fill="#8b96a5" text-anchor="middle">${initials}</text>
  </svg>`;
  return "data:image/svg+xml;base64," + btoa(svg);
}

function attachIconFallback(imgEl, ticker) {
  imgEl.addEventListener("error", () => {
    imgEl.onerror = null;
    imgEl.src = fallbackIconDataUrl(ticker);
  }, { once: true });
}

function buildCoinPicker() {
  if (!coinPicker || !coinSelect) return;

  coinPickerMenu.innerHTML = "";

  Array.from(coinSelect.children).forEach((group) => {
    if (group.tagName !== "OPTGROUP") return;

    const groupLabel = document.createElement("div");
    groupLabel.className = "coin-picker-group-label";
    groupLabel.textContent = group.label;
    coinPickerMenu.appendChild(groupLabel);

    Array.from(group.children).forEach((option) => {
      const value = option.value;
      const ticker = value.split("/")[0];

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "coin-picker-option";
      btn.dataset.value = value;
      btn.setAttribute("role", "option");

      const img = document.createElement("img");
      img.className = "coin-picker-option-icon";
      img.alt = "";
      img.src = coinIconUrl(ticker);
      attachIconFallback(img, ticker);

      const label = document.createElement("span");
      label.textContent = option.textContent;

      btn.appendChild(img);
      btn.appendChild(label);
      btn.addEventListener("click", () => selectCoin(value));

      coinPickerMenu.appendChild(btn);
    });
  });

  syncCoinPickerTrigger();
}

function syncCoinPickerTrigger() {
  const value = coinSelect.value;
  const ticker = value.split("/")[0];
  coinPickerLabel.textContent = value.replace("/", " / ");
  coinPickerIcon.src = coinIconUrl(ticker);
  attachIconFallback(coinPickerIcon, ticker);

  coinPickerMenu.querySelectorAll(".coin-picker-option").forEach((opt) => {
    opt.classList.toggle("active", opt.dataset.value === value);
  });
}

function selectCoin(value) {
  coinSelect.value = value;
  coinSelect.dispatchEvent(new Event("change"));
  syncCoinPickerTrigger();
  closeCoinPicker();
}

function openCoinPicker() {
  coinPicker.classList.add("open");
  coinPickerTrigger.setAttribute("aria-expanded", "true");
}
function closeCoinPicker() {
  coinPicker.classList.remove("open");
  coinPickerTrigger.setAttribute("aria-expanded", "false");
}

if (coinPickerTrigger) {
  coinPickerTrigger.addEventListener("click", () => {
    coinPicker.classList.contains("open") ? closeCoinPicker() : openCoinPicker();
  });
  document.addEventListener("click", (e) => {
    if (!coinPicker.contains(e.target)) closeCoinPicker();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeCoinPicker();
  });
}

if (coinSelect) {
  coinSelect.addEventListener("change", () => {
    syncCoinPickerTrigger();
    // Keep the live chart in sync if the coin changes while that tab is open.
    const livePanel = document.getElementById("panel-livechart");
    if (livePanel && livePanel.classList.contains("active")) {
      loadChartData();
    }
  });
}

buildCoinPicker();

/* ---------------------------- hero ---------------------------- */

function renderHero(data) {
  const verdict = (data.final_verdict || "--").toUpperCase();
  const gaugeVerdict = document.getElementById("gauge-verdict");
  const gaugeConfidence = document.getElementById("gauge-confidence");
  const gaugeFill = document.getElementById("gauge-fill");

  gaugeVerdict.textContent = verdict;
  gaugeConfidence.textContent = fmtPct(data.confidence_pct) + " confidence";

  let color = "var(--wait)";
  if (verdict === "LONG") color = "var(--long)";
  else if (verdict === "SHORT") color = "var(--short)";
  gaugeVerdict.style.color = color;
  gaugeFill.style.stroke = color;

  const confRatio = na(data.confidence_pct) ? 0 : Math.max(0, Math.min(100, data.confidence_pct)) / 100;
  const offset = GAUGE_CIRCUMFERENCE * (1 - confRatio);
  gaugeFill.style.strokeDasharray = GAUGE_CIRCUMFERENCE;
  gaugeFill.style.strokeDashoffset = offset;

  document.getElementById("hero-price").textContent = fmtPrice(data.last_price);
  document.getElementById("hero-rsi").textContent = fmtNum(data.rsi);
  document.getElementById("hero-macd").textContent = fmtNum(data.macd, 4);

  const tag = document.getElementById("hero-trend-tag");
  tag.textContent = `${data.coin || ""} · ${data.timeframe || ""} · ${data.trend || "--"}`.toUpperCase();
}

/* ---------------------------- tier 1: core verdict engine ---------------------------- */

function renderTier1(data) {
  const cards = [];

  cards.push(channelCard({
    id: 1, title: "Buying / Selling Pressure", model: "hawkes process",
    span2: true,
    body: `
      <div class="dual-split">
        <div class="dual-item">
          <span class="dual-label">BUY PRESSURE</span>
          <span class="dual-value text-long">${fmtNum(data.buying_pressure, 1)} / 10</span>
          ${meterBar((data.buying_pressure ?? 0) * 10, "c-long")}
        </div>
        <div class="dual-item">
          <span class="dual-label">SELL PRESSURE</span>
          <span class="dual-value text-short">${fmtNum(data.selling_pressure, 1)} / 10</span>
          ${meterBar((data.selling_pressure ?? 0) * 10, "c-short")}
        </div>
      </div>`
  }));

  cards.push(channelCard({
    id: 2, title: "Market Bias", model: "bayesian classifier",
    span2: true,
    body: `
      ${meterBar(data.bullish_pct ?? 50, "c-long")}
      <div class="dual-split">
        <div class="dual-item"><span class="dual-label">BULLISH</span><span class="dual-value text-long">${fmtPct(data.bullish_pct)}</span></div>
        <div class="dual-item"><span class="dual-label">BEARISH</span><span class="dual-value text-short">${fmtPct(data.bearish_pct)}</span></div>
      </div>`
  }));

  cards.push(channelCard({
    id: 3, title: "Quantile Volatility", model: "95th pctile · SL/TP",
    span2: true,
    body: `
      <div class="dual-split">
        <div class="dual-item"><span class="dual-label">EXPECTED MOVE</span><span class="dual-value">${fmtPct(data.expected_volatility_pct, 2)}</span></div>
        <div class="dual-item"><span class="dual-label">EXTREME MOVE (95%)</span><span class="dual-value">${fmtPct(data.extreme_volatility_95_pct, 2)}</span></div>
        <div class="dual-item"><span class="dual-label">STOP LOSS</span><span class="dual-value text-short">${fmtPrice(data.stop_loss)}</span></div>
        <div class="dual-item"><span class="dual-label">TAKE PROFIT</span><span class="dual-value text-long">${fmtPrice(data.take_profit)}</span></div>
      </div>`
  }));

  const decision = data.confidence_pct != null && data.confidence_pct < 55 ? "SKIP" : "TRADE";
  cards.push(channelCard({
    id: 4, title: "Conformal Decision", model: "conformal prediction",
    body: `
      ${badge(decision, decision === "TRADE" ? "long" : "wait")}
      <div class="channel-main">${fmtPct(data.confidence_pct)}</div>
      <div class="channel-detail">confidence score</div>`
  }));

  cards.push(channelCard({
    id: 5, title: "Suggested Risk", model: "fractional kelly",
    body: `
      <div class="channel-main text-wait">${fmtPct(data.suggested_risk_pct, 2)}</div>
      <div class="channel-detail">of account per trade</div>`
  }));

  tier1El.innerHTML = cards.join("");
}

/* ---------------------------- tier 2: microstructure ---------------------------- */

function renderTier2(data) {
  const cards = [];
  const ofi = data.order_flow || {};
  const toxic = data.toxic_flow || {};
  const regime = data.market_regime || {};
  const jump = data.jump_shock || {};
  const meta = data.meta_label || {};

  cards.push(channelCard({
    id: 6, title: "Order Flow Imbalance", model: "L1 snapshot delta",
    body: `
      ${centeredMeter(ofi.ofi_score, 10)}
      <div class="channel-main">${fmtSigned(ofi.ofi_score, 2)}</div>
      <div class="channel-detail">${ofi.ofi_score == null ? "no data" : (ofi.ofi_score >= 0 ? "buyers aggressive" : "sellers aggressive")}</div>`
  }));

  const toxTone = toxic.toxicity === "HIGH_TOXICITY" ? "short" : toxic.toxicity === "MODERATE_TOXICITY" ? "wait" : "long";
  cards.push(channelCard({
    id: 7, title: "Toxic Flow", model: "VPIN",
    body: `
      ${meterBar(na(toxic.vpin_score) ? 0 : toxic.vpin_score * 100, "c-" + toxTone)}
      <div class="channel-main">${na(toxic.vpin_score) ? "--" : fmtNum(toxic.vpin_score, 3)}</div>
      ${badge(toxic.toxicity || "N/A", toxic.toxicity ? toxTone : "flat")}`
  }));

  const regTone = regime.regime === "Trending" ? "long" : regime.regime === "Ranging" ? "wait" : "flat";
  cards.push(channelCard({
    id: 8, title: "Market Regime", model: "gaussian HMM",
    body: `
      ${badge((regime.regime || "N/A").toUpperCase(), regTone)}
      <div class="channel-detail">${regime.state_mean_return_pct != null ? `avg state return ${fmtSigned(regime.state_mean_return_pct, 3, "%")}` : "insufficient data"}</div>`
  }));

  const jumpTone = jump.jump_detected ? "short" : "flat";
  cards.push(channelCard({
    id: 9, title: "Jump Diffusion", model: "z-score shock detector",
    body: `
      ${badge(jump.jump_detected ? `JUMP · ${jump.jump_direction}` : "NO JUMP", jumpTone)}
      <div class="channel-detail">z-score ${fmtNum(jump.jump_zscore, 2)}</div>`
  }));

  const metaTone = meta.meta_decision === "EXECUTE" ? "long" : meta.meta_decision === "SKIP" ? "flat" : "flat";
  cards.push(channelCard({
    id: 10, title: "Meta-Labeling Filter", model: "random forest",
    body: `
      ${badge(meta.meta_decision || "N/A", metaTone)}
      ${meterBar(meta.meta_win_probability ?? 0, "c-accent")}
      <div class="channel-detail">${meta.meta_win_probability != null ? `${fmtPct(meta.meta_win_probability)} win probability` : "insufficient data"}</div>`
  }));

  tier2El.innerHTML = cards.join("");
}

/* ---------------------------- tier 3: extended signals (Ch.11–18) ---------------------------- */

function renderTier3(data) {
  const cards = [];
  const div = data.intermarket_divergence || {};
  const ent = data.entropy || {};
  const depth = data.depth_profile || {};
  const vwap = data.vwap_deviation || {};
  const rl = data.rl_risk_agent || {};
  const hurst = data.hurst || {};
  const wave = data.wavelet_trend || {};
  const cusum = data.structural_break || {};

  const divTone = (div.interpretation || "").includes("OUTPERFORM") ? "long" : (div.interpretation || "").includes("UNDERPERFORM") ? "short" : "flat";
  cards.push(channelCard({
    id: 11, title: "Intermarket Divergence", model: `vs ${div.benchmark || "--"}`,
    body: `
      <div class="channel-main">${fmtSigned(div.divergence_score, 2)}</div>
      ${badge((div.interpretation || "N/A").replace(/_/g, " "), divTone)}`
  }));

  const entTone = ent.regime === "LOW_ENTROPY_TRENDING" ? "long" : ent.regime === "HIGH_ENTROPY_CHOPPY" ? "short" : "wait";
  cards.push(channelCard({
    id: 12, title: "Permutation Entropy", model: "multi-order avg",
    body: `
      <div class="channel-main">${fmtNum(ent.entropy_avg, 3)}</div>
      ${badge((ent.regime || "N/A").replace(/_/g, " "), entTone)}`
  }));

  const wallTone = depth.wall_bias === "BID_WALL_HEAVIER" ? "long" : depth.wall_bias === "ASK_WALL_HEAVIER" ? "short" : "flat";
  cards.push(channelCard({
    id: 13, title: "Order Book Depth (L2)", model: "weighted slope",
    body: `
      <div class="channel-main">${fmtNum(depth.depth_slope, 3)}</div>
      ${badge((depth.wall_bias || "N/A").replace(/_/g, " "), wallTone)}`
  }));

  const vwapTone = vwap.signal === "MEAN_REVERSION_LIKELY" ? "wait" : "flat";
  cards.push(channelCard({
    id: 14, title: "VWAP Deviation", model: "z-score",
    body: `
      <div class="channel-main">${fmtSigned(vwap.vwap_deviation_z, 2)}</div>
      ${badge((vwap.signal || "N/A").replace(/_/g, " "), vwapTone)}
      ${vwap.toxic_reversion_flag ? `<div class="channel-detail text-short">toxic reversion flagged</div>` : ""}`
  }));

  cards.push(channelCard({
    id: 15, title: "Dynamic Risk Agent", model: "RL-style heuristic",
    body: `
      ${badge((rl.rl_state || "N/A").replace(/_/g, " "), "accent")}
      <div class="dual-split">
        <div class="dual-item"><span class="dual-label">MULTIPLIER</span><span class="dual-value">${fmtNum(rl.rl_risk_multiplier, 2)}x</span></div>
        <div class="dual-item"><span class="dual-label">ADJ. RISK</span><span class="dual-value text-wait">${fmtPct(rl.rl_adjusted_risk_pct, 2)}</span></div>
      </div>`
  }));

  const hurstTone = hurst.memory === "TRENDING_PERSISTENT" ? "long" : hurst.memory === "MEAN_REVERTING" ? "short" : "flat";
  cards.push(channelCard({
    id: 16, title: "Hurst Exponent", model: "variance scaling",
    body: `
      <div class="channel-main">${fmtNum(hurst.hurst, 3)}</div>
      ${badge((hurst.memory || "N/A").replace(/_/g, " "), hurstTone)}`
  }));

  const waveTone = wave.wavelet_trend_direction === "UP" ? "long" : wave.wavelet_trend_direction === "DOWN" ? "short" : "flat";
  cards.push(channelCard({
    id: 17, title: "Wavelet Denoised Trend", model: "db4 · level 2",
    body: `
      ${badge(wave.wavelet_trend_direction || "N/A", waveTone)}
      <div class="channel-detail">slope ${fmtSigned(wave.wavelet_trend_slope, 4)}</div>`
  }));

  cards.push(channelCard({
    id: 18, title: "Structural Break", model: "CUSUM test",
    body: `
      ${badge(cusum.structural_break ? "BREAK DETECTED" : "STABLE", cusum.structural_break ? "short" : "long")}
      <div class="dual-split">
        <div class="dual-item"><span class="dual-label">CUSUM+</span><span class="dual-value">${fmtNum(cusum.cusum_pos, 4)}</span></div>
        <div class="dual-item"><span class="dual-label">CUSUM-</span><span class="dual-value">${fmtNum(cusum.cusum_neg, 4)}</span></div>
      </div>`
  }));

  tier3El.innerHTML = cards.join("");
}

/* ==========================================================================
   LIQUIDITY RADAR DASHBOARD — full multi-card scanner (Ch.19 tab).
   Lazy-loaded from /liquidity-radar only when this tab is opened (same
   pattern as the Live Chart tab's /candles), then polled every few
   seconds so it's a real, moving scan — not a static snapshot. When a
   fresh liquidity sweep is detected between polls, it fires a red-dot
   alert (both a banner and a flashing marker on the radar).
   ========================================================================== */

let liqPollTimer = null;
let liqLastSnapshot = null; // { sweepDetected, sweptPriceKeys: Set }

function toneForScore(score, invert = false) {
  if (na(score)) return "flat";
  const s = invert ? 100 - score : score;
  if (s >= 66) return "short";
  if (s >= 33) return "wait";
  return "long";
}

/* Generic 6-axis spider/radar chart. axes = [{ label, value(0-100), color }] */
function buildSpiderChart(axes, liveAlert) {
  const size = 220;
  const cx = size / 2;
  const cy = size / 2;
  const maxR = 88;
  const rings = [0.25, 0.5, 0.75, 1];
  const count = axes.length;

  function point(i, valuePct) {
    const angle = (-90 + i * (360 / count)) * (Math.PI / 180);
    const r = (Math.max(0, Math.min(100, valuePct)) / 100) * maxR;
    return { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) };
  }

  let ringShapes = "";
  rings.forEach((frac) => {
    const pts = axes.map((_, i) => {
      const angle = (-90 + i * (360 / count)) * (Math.PI / 180);
      const r = frac * maxR;
      return `${cx + r * Math.cos(angle)},${cy + r * Math.sin(angle)}`;
    }).join(" ");
    ringShapes += `<polygon points="${pts}" class="spider-ring" />`;
  });

  let spokes = "";
  let labels = "";
  axes.forEach((a, i) => {
    const outer = point(i, 100);
    const angle = (-90 + i * (360 / count)) * (Math.PI / 180);
    const labelR = maxR + 20;
    const lx = cx + labelR * Math.cos(angle);
    const ly = cy + labelR * Math.sin(angle);
    spokes += `<line x1="${cx}" y1="${cy}" x2="${outer.x}" y2="${outer.y}" class="spider-spoke" />`;
    labels += `<text x="${lx}" y="${ly}" class="spider-label" text-anchor="middle" dominant-baseline="middle">${a.label}</text>`;
  });

  const dataPts = axes.map((a, i) => point(i, a.value)).map((p) => `${p.x},${p.y}`).join(" ");
  const dots = axes.map((a, i) => {
    const p = point(i, a.value);
    const alertClass = liveAlert && a.label === "SWEEP" ? " spider-dot-alert" : "";
    return `<circle cx="${p.x}" cy="${p.y}" r="4" class="spider-dot${alertClass}" style="fill:${a.color};" />`;
  }).join("");

  return `
    <svg viewBox="0 0 ${size} ${size}" class="spider-svg">
      ${ringShapes}
      ${spokes}
      <polygon points="${dataPts}" class="spider-shape" />
      ${dots}
      ${labels}
    </svg>`;
}

function radialGauge(score, label) {
  const r = 34;
  const circumference = 2 * Math.PI * r;
  const pct = na(score) ? 0 : Math.max(0, Math.min(100, score));
  const offset = circumference * (1 - pct / 100);
  const tone = toneForScore(pct);
  return `
    <div class="lr-gauge-wrap">
      <svg viewBox="0 0 80 80" class="lr-gauge-svg">
        <circle cx="40" cy="40" r="${r}" class="lr-gauge-track" />
        <circle cx="40" cy="40" r="${r}" class="lr-gauge-fill c-${tone}"
          stroke-dasharray="${circumference}" stroke-dashoffset="${offset}" />
      </svg>
      <div class="lr-gauge-center">
        <span class="lr-gauge-value">${na(score) ? "--" : score}</span>
        <span class="lr-gauge-label">${label}</span>
      </div>
    </div>`;
}

function meterRow(label, value, tone) {
  return `
    <div class="lr-meter-row">
      <span class="lr-meter-label">${label}</span>
      ${meterBar(value || 0, "c-" + tone)}
      <span class="lr-meter-value">${na(value) ? "--" : value}</span>
    </div>`;
}

function renderLiquidityRadarDashboard(data, liveAlert) {
  if (!liqScannerEl) return;

  const price = data.last_price;
  const sweep = data.liquidity_sweep || {};
  const pools = Array.isArray(data.pools) ? data.pools : [];
  const magnet = data.magnet;
  const target = data.target;
  const spoof = data.spoofing || {};
  const trap = data.trap_squeeze || {};
  const funding = data.funding_open_interest || {};
  const cvd = data.cvd || {};

  const bullish = data.bullish_pct;
  const bearish = data.bearish_pct;
  const biasTone = !na(bullish) && !na(bearish) ? (bullish > bearish ? "long" : bullish < bearish ? "short" : "wait") : "flat";
  const biasLabel = biasTone === "long" ? "BULLISH BIAS" : biasTone === "short" ? "BEARISH BIAS" : "NEUTRAL";

  const sweepActive = !!sweep.liquidity_sweep_detected;
  const nearestSwept = pools.filter((p) => p.swept).sort((a, b) => a.distance_pct - b.distance_pct)[0];

  const spiderAxes = [
    { label: "STRENGTH", value: data.market_strength ?? 0, color: "var(--accent)" },
    { label: "BULL", value: bullish ?? 0, color: "var(--long)" },
    { label: "SWEEP", value: sweepActive ? 100 : (nearestSwept ? 40 : 8), color: "var(--short)" },
    { label: "BEAR", value: bearish ?? 0, color: "var(--short)" },
    { label: "SPOOF", value: spoof.spoof_score ?? 0, color: "var(--wait)" },
    { label: "TARGET", value: target ? Math.round((target.strength || 0) * 100) : 0, color: "var(--long-bright)" },
  ];

  const zoneRows = pools.length ? pools.map((p) => {
    const isSell = p.side === "SELL_SIDE";
    const tone = p.swept ? "flat" : (isSell ? "short" : "long");
    const wallLabel = p.swept ? "SWEPT" : (isSell ? "SELL WALL" : "BUY WALL");
    const scorePct = Math.round((p.strength || 0) * 100);
    return `
      <div class="lr-zone-row">
        <span class="lr-zone-price">${fmtPrice(p.price)}</span>
        ${badge(wallLabel, tone)}
        <span class="lr-zone-dist">${fmtPct(p.distance_pct, 2)}</span>
        <div class="lr-zone-score-track"><div class="lr-zone-score-fill c-${tone === "flat" ? "accent" : tone}" style="width:${scorePct}%;"></div></div>
        <span class="lr-zone-score-num">${scorePct}/100</span>
      </div>`;
  }).join("") : `<div class="liq-radar-empty">No clustered liquidity zones detected in current window.</div>`;

  const cvdPoints = Array.isArray(cvd.cvd_points) ? cvd.cvd_points : [];
  let cvdSvg = `<div class="liq-radar-empty">CVD data unavailable.</div>`;
  if (cvdPoints.length > 1) {
    const w = 260, h = 60;
    const min = Math.min(...cvdPoints), max = Math.max(...cvdPoints);
    const range = max - min || 1;
    const stepX = w / (cvdPoints.length - 1);
    const linePts = cvdPoints.map((v, i) => `${(i * stepX).toFixed(1)},${(h - ((v - min) / range) * h).toFixed(1)}`).join(" ");
    const cvdTone = (cvd.cvd_last || 0) >= 0 ? "var(--long)" : "var(--short)";
    cvdSvg = `
      <svg viewBox="0 0 ${w} ${h}" class="lr-cvd-svg" preserveAspectRatio="none">
        <polyline points="${linePts}" fill="none" stroke="${cvdTone}" stroke-width="1.6" />
      </svg>`;
  }

  const fundingTone = na(funding.funding_rate_pct) ? "flat" : (funding.funding_rate_pct > 0.02 ? "short" : funding.funding_rate_pct < 0 ? "long" : "wait");

  liqScannerEl.innerHTML = `
    <div class="lr-dash${liveAlert ? " lr-flash" : ""}">

      <div class="lr-header">
        <div class="lr-header-left">
          <span class="lr-coin-badge">${(data.coin || "").replace("/", "")} · PERPETUAL</span>
          ${badge(biasLabel, biasTone)}
          ${sweepActive ? `<span class="lr-live-alert-dot" title="Live liquidity sweep detected"></span>` : ""}
        </div>
        <div class="lr-header-right">
          <div class="lr-price">${fmtPrice(price)}</div>
          <div class="lr-confidence">${na(data.market_strength) ? "--" : data.market_strength}<span>STRENGTH / 100</span></div>
        </div>
      </div>

      <div class="lr-bias-bar">
        <div class="lr-bias-fill" style="width:${na(bullish) ? 50 : bullish}%;"></div>
        <span class="lr-bias-label left">${na(bullish) ? "--" : fmtPct(bullish, 1)} BUY</span>
        <span class="lr-bias-label right">${na(bearish) ? "--" : fmtPct(bearish, 1)} SELL</span>
      </div>

      <div class="lr-grid-top">
        <div class="lr-card">
          <div class="lr-card-title">Liquidity Magnet</div>
          ${magnet ? `
            <div class="lr-card-big text-long">${fmtPrice(magnet.price)}</div>
            <div class="lr-card-sub">Nearest strong cluster · price pulled toward this level</div>
            <div class="lr-card-foot">
              <span>Distance <b>${fmtPct(magnet.distance_pct, 2)}</b></span>
              <span>Score <b>${Math.round((magnet.strength || 0) * 100)}/100</b></span>
            </div>` : `<div class="liq-radar-empty">No magnet level found.</div>`}
        </div>

        <div class="lr-card">
          <div class="lr-card-title">Likely Target</div>
          ${target ? `
            <div class="lr-card-big text-wait">${fmtPrice(target.price)}</div>
            <div class="lr-card-sub">Highest-probability level beyond the magnet</div>
            <div class="lr-card-foot">
              <span>Distance <b>${fmtPct(target.distance_pct, 2)}</b></span>
              <span>Score <b>${Math.round((target.strength || 0) * 100)}/100</b></span>
            </div>` : `<div class="liq-radar-empty">No secondary target found.</div>`}
        </div>

        <div class="lr-card lr-card-center">
          <div class="lr-card-title">Market Strength</div>
          ${radialGauge(data.market_strength, "SCORE")}
        </div>

        <div class="lr-card lr-card-span2">
          <div class="lr-card-title">Live Radar</div>
          <div class="lr-spider-wrap">${buildSpiderChart(spiderAxes, liveAlert)}</div>
        </div>
      </div>

      <div class="lr-grid-mid">
        <div class="lr-card">
          <div class="lr-card-title">Possible Spoofing <span class="lr-hint" title="Heuristic estimate from public order-book snapshots — cannot confirm intent, no exchange labels orders as spoofed.">ⓘ</span></div>
          ${na(spoof.spoof_score) ? `<div class="liq-radar-empty">Order-book snapshot unavailable.</div>` : `
            <div class="lr-card-big text-${toneForScore(spoof.spoof_score)}">${spoof.spoof_score}/100</div>
            <div class="lr-card-sub">${spoof.flagged_price ? `${fmtPrice(spoof.flagged_price)} · ${(spoof.flagged_side || "").replace(/_/g, " ")}` : "no large order vanished this scan"}</div>
            ${spoof.flagged_price ? `<div class="lr-card-foot"><span>Cancelled <b>${spoof.cancelled_pct}%</b></span><span>Size <b>${fmtNum(spoof.vanished_size, 2)}</b></span></div>` : ""}`}
        </div>

        <div class="lr-card">
          <div class="lr-card-title">Last Liquidity Sweep</div>
          ${sweepActive ? `
            ${badge((sweep.sweep_direction || "SWEEP").replace(/_/g, " "), "short")}
            <div class="lr-card-sub">Price swept ${fmtPrice(sweep.swing_high > price ? sweep.swing_high : sweep.swing_low)} this candle</div>
          ` : nearestSwept ? `
            ${badge("EARLIER SWEEP", "flat")}
            <div class="lr-card-sub">Nearest swept level: ${fmtPrice(nearestSwept.price)} · ${fmtPct(nearestSwept.distance_pct, 2)} away</div>
          ` : `<div class="liq-radar-empty">No sweep detected in current window.</div>`}
        </div>

        <div class="lr-card">
          <div class="lr-card-title">Trap &amp; Squeeze Risk</div>
          ${meterRow("BULL TRAP", trap.bull_trap, "short")}
          ${meterRow("BEAR TRAP", trap.bear_trap, "wait")}
          ${meterRow("SHORT SQUEEZE", trap.short_squeeze, "long")}
          ${meterRow("LONG SQUEEZE", trap.long_squeeze, "accent")}
        </div>
      </div>

      <div class="lr-card">
        <div class="lr-card-title">Liquidity Target Zones</div>
        <div class="lr-zones-list">${zoneRows}</div>
      </div>

      <div class="lr-grid-bottom">
        <div class="lr-card">
          <div class="lr-card-title">Funding Rate &amp; Open Interest</div>
          ${funding.available ? `
            <div class="lr-dual-split">
              <div><span class="lr-dual-label">FUNDING</span><span class="lr-dual-value text-${fundingTone}">${fmtSigned(funding.funding_rate_pct, 4, "%")}</span></div>
              <div><span class="lr-dual-label">OPEN INTEREST</span><span class="lr-dual-value">${na(funding.open_interest) ? "--" : fmtNum(funding.open_interest, 2)}</span></div>
            </div>
            <div class="lr-card-sub">${fundingTone === "short" ? "Elevated positive funding — longs paying, long-squeeze risk" : fundingTone === "long" ? "Negative funding — shorts paying, short-squeeze risk" : "Neutral funding rate, no extreme lean"}</div>
          ` : `<div class="liq-radar-empty">No perpetual swap listing found for this coin on OKX — funding/OI unavailable.</div>`}
        </div>

        <div class="lr-card">
          <div class="lr-card-title">CVD · Volume Delta</div>
          ${cvdSvg}
          <div class="lr-card-sub">Cumulative delta ${na(cvd.cvd_last) ? "--" : fmtNum(cvd.cvd_last, 2)} · ${(cvd.cvd_last || 0) >= 0 ? "net buy pressure" : "net sell pressure"}</div>
        </div>
      </div>

    </div>`;
}

async function loadLiquidityRadarData() {
  if (!liqScannerEl) return;
  const coin = coinSelect.value;

  if (!liqScannerEl.dataset.loadedOnce) {
    liqScannerEl.innerHTML = `<div class="liq-radar-empty">Scanning ${coin} order book, funding, and recent trades…</div>`;
  }

  try {
    const res = await fetch(`/liquidity-radar?coin=${encodeURIComponent(coin)}&timeframe=1h`);
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "liquidity radar fetch failed");

    const sweptKeys = new Set((data.pools || []).filter((p) => p.swept).map((p) => p.price));
    const sweepDetected = !!(data.liquidity_sweep || {}).liquidity_sweep_detected;

    let liveAlert = false;
    if (liqLastSnapshot && liqLastSnapshot.coin === coin) {
      const newSweep = sweepDetected && !liqLastSnapshot.sweepDetected;
      const newlySweptPool = [...sweptKeys].some((k) => !liqLastSnapshot.sweptKeys.has(k));
      if (newSweep || newlySweptPool) {
        liveAlert = true;
        addAlert("danger", `🔴 LIQUIDITY SWEEP — fresh liquidity taken on ${coin}`);
      }
    }
    liqLastSnapshot = { coin, sweepDetected, sweptKeys };

    renderLiquidityRadarDashboard(data, liveAlert);
    liqScannerEl.dataset.loadedOnce = "1";
    if (liveAlert) setTimeout(() => renderLiquidityRadarDashboard(data, false), 2200);
  } catch (err) {
    liqScannerEl.innerHTML = `<div class="liq-radar-empty">⚠ ${err.message}</div>`;
  }
}

function startLiquidityPolling() {
  stopLiquidityPolling();
  liqPollTimer = setInterval(() => {
    const panel = document.getElementById("panel-liquidity");
    if (!panel || !panel.classList.contains("active")) return; // pause when tab hidden
    loadLiquidityRadarData();
  }, 6000);
}

function stopLiquidityPolling() {
  if (liqPollTimer) clearInterval(liqPollTimer);
  liqPollTimer = null;
}

/* ==========================================================================
   LIVE CHART TAB — real candlesticks via TradingView's lightweight-charts,
   fed from the /candles endpoint and polled every few seconds for live
   price + last-candle updates.
   ========================================================================== */


let lwChart = null;
let lwCandleSeries = null;
let chartPollTimer = null;
let currentChartTimeframe = "1h";

function ensureChartInitialized() {
  if (lwChart || !candleChartEl || typeof LightweightCharts === "undefined") return;

  lwChart = LightweightCharts.createChart(candleChartEl, {
    layout: {
      background: { type: "solid", color: "transparent" },
      textColor: "#8b96a5",
      fontFamily: "IBM Plex Mono, monospace",
      fontSize: 11,
    },
    grid: {
      vertLines: { color: "rgba(39, 48, 59, 0.5)" },
      horzLines: { color: "rgba(39, 48, 59, 0.5)" },
    },
    rightPriceScale: { borderColor: "#27303b" },
    timeScale: { borderColor: "#27303b", timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });

  lwCandleSeries = lwChart.addCandlestickSeries({
    upColor: "#36e0a0",
    downColor: "#ff526b",
    borderUpColor: "#28f3a5",
    borderDownColor: "#ff6b7e",
    wickUpColor: "#36e0a0",
    wickDownColor: "#ff526b",
  });

  window.addEventListener("resize", resizeChart);
}

function resizeChart() {
  if (!lwChart || !candleChartEl) return;
  lwChart.resize(candleChartEl.clientWidth, candleChartEl.clientHeight);
}

if (chartTfRow) {
  chartTfRow.querySelectorAll(".chart-tf-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      chartTfRow.querySelectorAll(".chart-tf-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      currentChartTimeframe = btn.dataset.tf;
      loadChartData();
    });
  });
}

async function loadChartData() {
  if (!candleChartEl) return;
  ensureChartInitialized();
  if (!lwChart) {
    if (chartStatusEl) chartStatusEl.textContent = "chart library failed to load — check your connection";
    return;
  }

  const coin = coinSelect.value;
  if (chartTitleEl) chartTitleEl.textContent = `${coin} · ${currentChartTimeframe.toUpperCase()}`;
  if (chartStatusEl) chartStatusEl.textContent = "loading candles…";

  try {
    const res = await fetch(`/candles?coin=${encodeURIComponent(coin)}&timeframe=${currentChartTimeframe}&limit=300`);
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "candle fetch failed");

    const bars = (data.candles || []).map((c) => ({
      time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
    }));
    lwCandleSeries.setData(bars);
    lwChart.timeScale().fitContent();

    updateChartPrice(data);
    if (chartStatusEl) {
      chartStatusEl.textContent = `live · ${coin} · ${currentChartTimeframe.toUpperCase()} · updates every 5s`;
    }

    restartChartPolling(coin);
  } catch (err) {
    if (chartStatusEl) chartStatusEl.textContent = "⚠ " + err.message;
  }
}

function updateChartPrice(data) {
  if (chartPriceEl) chartPriceEl.textContent = fmtPrice(data.last_price);
  if (chartChangeEl) {
    const chg = data.change_pct;
    chartChangeEl.textContent = na(chg) ? "--" : (chg > 0 ? "+" : "") + chg.toFixed(2) + "%";
    chartChangeEl.classList.remove("up", "down");
    if (!na(chg)) chartChangeEl.classList.add(chg >= 0 ? "up" : "down");
  }
}

function restartChartPolling(coin) {
  if (chartPollTimer) clearInterval(chartPollTimer);
  chartPollTimer = setInterval(async () => {
    const livePanel = document.getElementById("panel-livechart");
    if (!livePanel || !livePanel.classList.contains("active")) return; // pause when tab hidden
    try {
      const res = await fetch(`/candles?coin=${encodeURIComponent(coin)}&timeframe=${currentChartTimeframe}&limit=2`);
      const data = await res.json();
      if (!res.ok || data.error || !data.candles || !data.candles.length) return;

      const last = data.candles[data.candles.length - 1];
      lwCandleSeries.update({
        time: last.time, open: last.open, high: last.high, low: last.low, close: last.close,
      });
      updateChartPrice(data);
    } catch (e) {
      // Silent — a single missed poll shouldn't spam the UI; next tick retries.
    }
  }, 5000);
}

/* ---------------------------- main render ---------------------------- */

function renderResult(data) {
  clearAlerts();

  const jump = data.jump_shock || {};
  if (jump.jump_detected) {
    addAlert("danger", `VOLATILITY SHOCK — ${jump.jump_direction} jump detected (z=${fmtNum(jump.jump_zscore, 2)})`);
  }
  if (data.fake_breakout_warning) {
    addAlert("warning", "FAKE BREAKOUT RISK — order flow disagrees with price direction");
  }

  renderHero(data);
  renderTier1(data);
  renderTier2(data);
  renderTier3(data);

  // Liquidity Radar dashboard loads independently from /liquidity-radar
  // (see loadLiquidityRadarData) — but refresh it immediately if that tab
  // happens to already be open so it doesn't wait for the next poll tick.
  const liquidityPanel = document.getElementById("panel-liquidity");
  if (liquidityPanel && liquidityPanel.classList.contains("active")) {
    loadLiquidityRadarData();
  }

  if (data.disclaimer) {
    document.getElementById("disclaimer-text").textContent = "⚠ " + data.disclaimer;
  }

  // Keep the live chart's coin/title in sync even if the user hasn't opened
  // that tab yet — it'll be correct the moment they click it.
  if (chartTitleEl) chartTitleEl.textContent = `${data.coin || coinSelect.value} · ${currentChartTimeframe.toUpperCase()}`;
}

/* ---------------------------- fetch flow ---------------------------- */

async function runAnalysis() {
  const coin = coinSelect.value;

  errorText.classList.add("hidden");
  errorText.textContent = "";
  runBtn.disabled = true;
  runBtn.querySelector(".scan-btn-text").textContent = "SCANNING…";

  try {
    const res = await fetch(`/signal?coin=${encodeURIComponent(coin)}&timeframe=1h`);
    const data = await res.json();

    if (!res.ok || data.error) {
      throw new Error(data.error || "Signal fetch failed");
    }

    renderResult(data);
    resultBox.classList.remove("hidden");
    emptyState.classList.add("hidden");
  } catch (err) {
    errorText.textContent = "⚠ " + err.message;
    errorText.classList.remove("hidden");
    resultBox.classList.add("hidden");
    emptyState.classList.remove("hidden");
  } finally {
    runBtn.disabled = false;
    runBtn.querySelector(".scan-btn-text").textContent = "RUN ANALYSIS";
  }
}

runBtn.addEventListener("click", runAnalysis);
