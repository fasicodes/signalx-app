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

/* ---------------------------- liquidity scanner: CH.19 (RADAR) ---------------------------- */

let radarAnimationId = null;
let radarSweepAngle = -Math.PI / 2; // Start at top (12 o'clock)
let radarLastTime = 0;

function renderLiquidityScanner(data) {
  if (!liqScannerEl) return;

  const sweep = data.liquidity_sweep || {};
  const price = data.last_price;

  const hasRange = !na(sweep.swing_high) && !na(sweep.swing_low) && !na(price) && sweep.swing_high > sweep.swing_low;

  const detected = !!sweep.liquidity_sweep_detected;
  const tone = detected ? "wait" : "flat";
  const statusLabel = detected ? (sweep.sweep_direction || "SWEEP").replace(/_/g, " ") : "SCANNING...";

  // Calculate blip positions for radar
  // Radar angle: 0 = right (3 o'clock), -PI/2 = top (12 o'clock), sweep goes clockwise
  // Map price levels to radar angles (0 to 2π)
  // Current price: between swing low and swing high
  // Swing low: at bottom-ish, swing high: at top-ish
  let blips = [];
  if (hasRange) {
    const range = sweep.swing_high - sweep.swing_low;
    // Map price to angle: swing_low -> -PI/2 (bottom), swing_high -> PI/2 (top)
    // Actually let's make it more intuitive: sweep the full circle
    // Map price ratio to angle (0 to 2π)
    const priceRatio = Math.max(0, Math.min(1, (price - sweep.swing_low) / range));
    const priceAngle = -Math.PI / 2 + priceRatio * 2 * Math.PI;

    blips = [
      { angle: -Math.PI / 2, radius: 0.9, label: "SWING LOW", value: fmtPrice(sweep.swing_low), type: "low" },
      { angle: Math.PI / 2, radius: 0.9, label: "SWING HIGH", value: fmtPrice(sweep.swing_high), type: "high" },
      { angle: priceAngle, radius: 0.6, label: "CURRENT", value: fmtPrice(price), type: "current" },
    ];
  } else {
    blips = [
      { angle: -Math.PI / 2, radius: 0.8, label: "SWING LOW", value: fmtPrice(sweep.swing_low) || "--", type: "low" },
      { angle: Math.PI / 2, radius: 0.8, label: "SWING HIGH", value: fmtPrice(sweep.swing_high) || "--", type: "high" },
      { angle: 0, radius: 0.5, label: "CURRENT", value: fmtPrice(price) || "--", type: "current" },
    ];
  }

  // Add sweep detection indicator blip if sweep detected
  if (detected && hasRange) {
    const sweepAngle = sweep.sweep_direction === "SWEPT_HIGH_REVERSED_DOWN" ? Math.PI / 2 : -Math.PI / 2;
    blips.push({
      angle: sweepAngle,
      radius: 1.05,
      label: "SWEEP DETECTED",
      value: sweep.sweep_direction.replace(/_/g, " "),
      type: "sweep"
    });
  }

  liqScannerEl.innerHTML = `
    <div class="liq-panel">
      <div class="liq-header">
        <div class="liq-header-left">
          <span class="liq-icon">⌁</span>
          <div>
            <div class="liq-title">Liquidity Radar Scanner</div>
            <div class="liq-subtitle">CH.19 · rotating sweep · display-only</div>
          </div>
        </div>
        ${badge(statusLabel, tone)}
      </div>

      <div class="radar-container">
        <canvas id="liquidity-radar" class="radar-canvas" width="320" height="320"></canvas>
        <div class="radar-legend" id="radar-legend"></div>
      </div>

      <div class="liq-stats-grid">
        <div class="liq-stat">
          <span class="liq-stat-label">SWING HIGH</span>
          <span class="liq-stat-value">${fmtPrice(sweep.swing_high)}</span>
        </div>
        <div class="liq-stat">
          <span class="liq-stat-label">SWING LOW</span>
          <span class="liq-stat-value">${fmtPrice(sweep.swing_low)}</span>
        </div>
        <div class="liq-stat">
          <span class="liq-stat-label">DIST → HIGH</span>
          <span class="liq-stat-value text-long">${fmtPct(sweep.distance_to_high_pct, 2)}</span>
        </div>
        <div class="liq-stat">
          <span class="liq-stat-label">DIST → LOW</span>
          <span class="liq-stat-value text-short">${fmtPct(sweep.distance_to_low_pct, 2)}</span>
        </div>
      </div>
    </div>`;

  // Initialize radar animation after DOM is ready
  requestAnimationFrame(() => initRadar(blips, detected));
}

function initRadar(blips, detected) {
  const canvas = document.getElementById("liquidity-radar");
  if (!canvas) return;

  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  // Stop any existing animation
  if (radarAnimationId) {
    cancelAnimationFrame(radarAnimationId);
    radarAnimationId = null;
  }

  // Setup canvas for high DPI
  const dpr = window.devicePixelRatio || 1;
  const size = canvas.clientWidth || 320;
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  ctx.scale(dpr, dpr);

  // Colors from CSS variables
  const style = getComputedStyle(document.documentElement);
  const colors = {
    void: style.getPropertyValue("--void").trim() || "#070a0f",
    panel: style.getPropertyValue("--panel").trim() || "#11161e",
    line: style.getPropertyValue("--line").trim() || "#27303b",
    lineSoft: style.getPropertyValue("--line-soft").trim() || "#1a222c",
    text: style.getPropertyValue("--text").trim() || "#e9eef5",
    textDim: style.getPropertyValue("--text-dim").trim() || "#8b96a5",
    textFaint: style.getPropertyValue("--text-faint").trim() || "#566171",
    long: style.getPropertyValue("--long").trim() || "#36e0a0",
    short: style.getPropertyValue("--short").trim() || "#ff526b",
    wait: style.getPropertyValue("--wait").trim() || "#ffc857",
    accent: style.getPropertyValue("--accent").trim() || "#f5a623",
    longDim: style.getPropertyValue("--long-dim").trim() || "rgba(54, 224, 160, 0.12)",
    shortDim: style.getPropertyValue("--short-dim").trim() || "rgba(255, 82, 107, 0.12)",
    waitDim: style.getPropertyValue("--wait-dim").trim() || "rgba(255, 200, 87, 0.12)",
    accentDim: style.getPropertyValue("--accent-dim").trim() || "rgba(245, 166, 35, 0.12)",
  };

  const centerX = size / 2;
  const centerY = size / 2;
  const maxRadius = (size / 2) - 20; // Leave padding for labels

  // Radar sweep speed (radians per second) - slow like real radar
  const SWEEP_SPEED = Math.PI / 3; // ~3 seconds per full rotation

  function drawRadar(timestamp) {
    if (!radarLastTime) radarLastTime = timestamp;
    const delta = (timestamp - radarLastTime) / 1000; // seconds
    radarLastTime = timestamp;

    // Update sweep angle
    radarSweepAngle += SWEEP_SPEED * delta;
    if (radarSweepAngle > Math.PI * 3/2) radarSweepAngle = -Math.PI / 2;

    // Clear
    ctx.clearRect(0, 0, size, size);

    // Draw background circle
    ctx.beginPath();
    ctx.arc(centerX, centerY, maxRadius, 0, Math.PI * 2);
    ctx.fillStyle = colors.void;
    ctx.fill();
    ctx.strokeStyle = colors.line;
    ctx.lineWidth = 2;
    ctx.stroke();

    // Draw concentric circles (radar rings) - 4 rings
    const ringCount = 4;
    for (let i = 1; i <= ringCount; i++) {
      const r = (maxRadius / ringCount) * i;
      ctx.beginPath();
      ctx.arc(centerX, centerY, r, 0, Math.PI * 2);
      ctx.strokeStyle = colors.lineSoft;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 6]);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Draw crosshairs (cardinal directions)
    ctx.strokeStyle = colors.lineSoft;
    ctx.lineWidth = 1;
    ctx.setLineDash([6, 6]);
    // Horizontal
    ctx.beginPath();
    ctx.moveTo(centerX - maxRadius, centerY);
    ctx.lineTo(centerX + maxRadius, centerY);
    ctx.stroke();
    // Vertical
    ctx.beginPath();
    ctx.moveTo(centerX, centerY - maxRadius);
    ctx.lineTo(centerX, centerY + maxRadius);
    ctx.stroke();
    ctx.setLineDash([]);

    // Draw diagonal crosshairs
    ctx.beginPath();
    ctx.moveTo(centerX - maxRadius * 0.707, centerY - maxRadius * 0.707);
    ctx.lineTo(centerX + maxRadius * 0.707, centerY + maxRadius * 0.707);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(centerX - maxRadius * 0.707, centerY + maxRadius * 0.707);
    ctx.lineTo(centerX + maxRadius * 0.707, centerY - maxRadius * 0.707);
    ctx.stroke();

    // Draw sweep line (rotating beam)
    const sweepX = centerX + Math.cos(radarSweepAngle) * maxRadius;
    const sweepY = centerY + Math.sin(radarSweepAngle) * maxRadius;

    // Sweep trail (fading triangle/fan)
    const trailAngle = 0.3; // ~17 degrees
    ctx.beginPath();
    ctx.moveTo(centerX, centerY);
    ctx.arc(centerX, centerY, maxRadius, radarSweepAngle - trailAngle/2, radarSweepAngle + trailAngle/2);
    ctx.closePath();

    // Gradient for sweep trail
    const sweepGrad = ctx.createRadialGradient(centerX, centerY, 0, centerX, centerY, maxRadius);
    sweepGrad.addColorStop(0, colors.long.includes("rgba") ? colors.long.replace("0.12", "0.25") : `rgba(54, 224, 160, 0.25)`);
    sweepGrad.addColorStop(1, colors.long.includes("rgba") ? colors.long.replace("0.12", "0") : `rgba(54, 224, 160, 0)`);
    ctx.fillStyle = sweepGrad;
    ctx.fill();

    // Bright sweep line
    ctx.beginPath();
    ctx.moveTo(centerX, centerY);
    ctx.lineTo(sweepX, sweepY);
    ctx.strokeStyle = colors.long;
    ctx.lineWidth = 2.5;
    ctx.lineCap = "round";
    ctx.stroke();

    // Sweep tip glow
    ctx.beginPath();
    ctx.arc(sweepX, sweepY, 4, 0, Math.PI * 2);
    ctx.fillStyle = colors.long;
    ctx.shadowColor = colors.long;
    ctx.shadowBlur = 8;
    ctx.fill();
    ctx.shadowBlur = 0;

    // Draw blips (detected targets)
    blips.forEach(blip => {
      const blipX = centerX + Math.cos(blip.angle) * maxRadius * blip.radius;
      const blipY = centerY + Math.sin(blip.angle) * maxRadius * blip.radius;

      // Check if sweep is near this blip (for highlight effect)
      const angleDiff = Math.abs(radarSweepAngle - blip.angle);
      const normalizedDiff = Math.min(angleDiff, Math.PI * 2 - angleDiff);
      const isHighlighted = normalizedDiff < 0.2; // ~11 degrees

      // Blip pulse when highlighted
      const pulseScale = isHighlighted ? 1 + Math.sin(timestamp / 100) * 0.3 : 1;
      const blipRadius = 6 * pulseScale;

      // Resolve blip color from type
      const typeColorMap = {
        low: colors.short,
        high: colors.long,
        current: colors.accent,
        sweep: colors.wait
      };
      const blipColor = typeColorMap[blip.type] || colors.accent;

      // Draw blip
      ctx.beginPath();
      ctx.arc(blipX, blipY, blipRadius, 0, Math.PI * 2);
      ctx.fillStyle = isHighlighted ? blipColor : blipColor + "CC";
      ctx.shadowColor = blipColor;
      ctx.shadowBlur = isHighlighted ? 12 : 6;
      ctx.fill();
      ctx.shadowBlur = 0;

      // Blip ring when highlighted
      if (isHighlighted) {
        ctx.beginPath();
        ctx.arc(blipX, blipY, blipRadius + 4, 0, Math.PI * 2);
        ctx.strokeStyle = blipColor + "80";
        ctx.lineWidth = 2;
        ctx.stroke();
      }

      // Draw label for blip (outside radar)
      const labelRadius = maxRadius * blip.radius + 20;
      const labelX = centerX + Math.cos(blip.angle) * labelRadius;
      const labelY = centerY + Math.sin(blip.angle) * labelRadius;

      ctx.font = "10px 'IBM Plex Mono', monospace";
      ctx.textAlign = blip.angle > -Math.PI/2 && blip.angle < Math.PI/2 ? "left" : "right";
      ctx.textBaseline = "middle";
      ctx.fillStyle = colors.textDim;
      ctx.fillText(blip.label, labelX + (blip.angle > -Math.PI/2 && blip.angle < Math.PI/2 ? 8 : -8), labelY);
    });

    // Draw center dot
    ctx.beginPath();
    ctx.arc(centerX, centerY, 4, 0, Math.PI * 2);
    ctx.fillStyle = colors.textDim;
    ctx.fill();

    // Draw distance labels on rings
    ctx.font = "8px 'IBM Plex Mono', monospace";
    ctx.fillStyle = colors.textFaint;
    ctx.textAlign = "center";
    for (let i = 1; i <= ringCount; i++) {
      const r = (maxRadius / ringCount) * i;
      const pct = Math.round((i / ringCount) * 100);
      ctx.fillText(`${pct}%`, centerX + r + 12, centerY - 2);
    }

    radarAnimationId = requestAnimationFrame(drawRadar);
  }

  // Build legend
  buildRadarLegend(blips, colors);

  radarAnimationId = requestAnimationFrame(drawRadar);
}

function buildRadarLegend(blips, colors) {
  const legendEl = document.getElementById("radar-legend");
  if (!legendEl) return;

  const uniqueTypes = [...new Set(blips.map(b => b.type))];
  const typeLabels = {
    high: { label: "Swing High", color: colors.long },
    low: { label: "Swing Low", color: colors.short },
    current: { label: "Current Price", color: colors.accent },
    sweep: { label: "Sweep Detected", color: colors.wait },
  };

  legendEl.innerHTML = uniqueTypes.map(type => {
    const info = typeLabels[type];
    return `<div class="radar-legend-item">
      <span class="radar-legend-dot" style="background:${info.color}; box-shadow: 0 0 8px ${info.color};"></span>
      <span>${info.label}</span>
    </div>`;
  }).join("");
}

// Cleanup radar animation when panel is hidden
function stopRadarAnimation() {
  if (radarAnimationId) {
    cancelAnimationFrame(radarAnimationId);
    radarAnimationId = null;
  }
}

// Hook into panel tab switching to stop radar when leaving liquidity panel
const originalActivatePanel = activatePanel;
activatePanel = function(name) {
  if (name !== "liquidity") {
    stopRadarAnimation();
  }
  originalActivatePanel(name);
};
