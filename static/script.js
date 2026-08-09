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
const chartCoinIconEl = document.getElementById("chart-coin-icon");
const chartPriceEl  = document.getElementById("chart-price");
const chartChangeEl = document.getElementById("chart-change");
const chartStatusEl = document.getElementById("chart-status");
const chartTfRow    = document.getElementById("chart-tf-row");
const candleChartEl = document.getElementById("candle-chart");
const chartPanelEl  = document.getElementById("chart-panel");
const chartFullscreenBtn = document.getElementById("chart-fullscreen-btn");
const chartBackBtn = document.getElementById("chart-back-btn");
const chartToolsEl = document.getElementById("chart-tools");
const drawOverlayEl = document.getElementById("draw-overlay");

// indicator panel elements
const indicatorBtnEl = document.getElementById("indicator-btn");
const indicatorBtnCountEl = document.getElementById("indicator-btn-count");
const indicatorPanelEl = document.getElementById("indicator-panel");
const indicatorSearchEl = document.getElementById("indicator-search");
const indicatorClearAllEl = document.getElementById("indicator-clear-all");
const indicatorListEl = document.getElementById("indicator-list");
const indicatorActiveCountEl = document.getElementById("indicator-active-count");
const indicatorListCountEl = document.getElementById("indicator-list-count");
const indicatorOverlayEl = document.getElementById("indicator-overlay");

const GAUGE_CIRCUMFERENCE = 2 * Math.PI * 48; // r=48

// Liquidity scanner (CH.19-27) auto-refresh state.
let currentCoin = null;
let lastLiquidityExtra = null;
let liquidityFetchInFlight = false;
let liquidityPollTimer = null;
const LIQUIDITY_POLL_MS = 15000;

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
  if (name === "liquidity" && currentCoin) {
    fetchAndRenderLiquidity(currentCoin);
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

// Keep the live-chart header's coin logo (top-left of the Live Chart tab,
// and visible in fullscreen too) in sync with whichever pair is selected.
function syncChartCoinIcon() {
  if (!chartCoinIconEl || !coinSelect) return;
  const ticker = coinSelect.value.split("/")[0];
  chartCoinIconEl.src = coinIconUrl(ticker);
  attachIconFallback(chartCoinIconEl, ticker);
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
    syncChartCoinIcon();
    // Keep the live chart in sync if the coin changes while that tab is open.
    const livePanel = document.getElementById("panel-livechart");
    if (livePanel && livePanel.classList.contains("active")) {
      loadChartData();
    }
  });
}

buildCoinPicker();
syncChartCoinIcon();

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
   LIQUIDITY SCANNER — CH.19-27, now dynamic: sweep bar renders instantly
   from /signal, then /liquidity fills in magnet/target/strength/spoofing/
   trap-squeeze/zones/funding/CVD/crash-risk and keeps polling every
   LIQUIDITY_POLL_MS so the tab stays live without re-running full analysis.
   ========================================================================== */

function toneForScore(score) {
  if (na(score)) return "flat";
  if (score >= 65) return "long";
  if (score <= 35) return "short";
  return "wait";
}

function liqCard({ icon, title, subtitle, body, span2 = false }) {
  return `
    <div class="liq-card${span2 ? " span-2" : ""}">
      <div class="liq-card-head">
        <span class="liq-card-icon">${icon}</span>
        <div>
          <div class="liq-card-title">${title}</div>
          ${subtitle ? `<div class="liq-card-subtitle">${subtitle}</div>` : ""}
        </div>
      </div>
      <div class="liq-card-body">${body}</div>
    </div>`;
}

function sparkline(series, tone) {
  if (!series || !series.length) return `<div class="liq-empty-note">Not enough data yet</div>`;
  const w = 160, h = 34;
  const min = Math.min(...series), max = Math.max(...series);
  const range = max - min || 1;
  const step = w / (series.length - 1 || 1);
  const points = series.map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - min) / range) * h).toFixed(1)}`).join(" ");
  const stroke = tone === "long" ? "var(--long)" : tone === "short" ? "var(--short)" : "var(--text-dim)";
  return `<svg class="cvd-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline points="${points}" fill="none" stroke="${stroke}" stroke-width="2"/></svg>`;
}

function renderMagnetTargetCard(extra) {
  const magnet = extra.magnet || {};
  const target = extra.likely_target || {};
  return liqCard({
    icon: "⊙", title: "Liquidity Magnet & Likely Target", subtitle: "CH.20 · order-book cluster pull", span2: true,
    body: `
      <div class="liq-dual-stat">
        <div class="liq-dual-col">
          <span class="liq-dual-label">LIQUIDITY MAGNET</span>
          <span class="liq-dual-value">${fmtPrice(magnet.price)}</span>
          <span class="liq-dual-detail">${na(magnet.usd_size) ? "--" : "$" + Number(magnet.usd_size).toLocaleString()} cluster · ${fmtPct(magnet.distance_pct)} away</span>
          ${magnet.side ? badge(magnet.side, magnet.side === "SUPPORT" ? "long" : "short") : ""}
        </div>
        <div class="liq-dual-col">
          <span class="liq-dual-label">LIKELY TARGET</span>
          <span class="liq-dual-value">${fmtPrice(target.price)}</span>
          <span class="liq-dual-detail">Score ${fmtNum(target.score, 0)}/100 · ${fmtPct(target.distance_pct)} away</span>
          ${target.type ? badge(target.type.toUpperCase(), target.type.includes("Resistance") ? "short" : "long") : ""}
        </div>
      </div>`,
  });
}

function renderMarketStrengthCard(extra) {
  const s = extra.market_strength || {};
  const tone = toneForScore(s.score);
  return liqCard({
    icon: "◎", title: "Market Strength", subtitle: "CH.22 · blended pressure dial",
    body: `
      <div class="strength-gauge">
        <div class="strength-ring"></div>
        <div class="strength-center">
          <span class="strength-score text-${tone}">${na(s.score) ? "--" : Math.round(s.score)}</span>
          <span class="strength-label">${s.label || "--"}</span>
        </div>
      </div>`,
  });
}

function renderSpoofingCard(extra) {
  const sp = extra.possible_spoofing || {};
  const top = sp.top_vanished_level;
  let body;
  if (!sp.available) {
    body = `<div class="liq-empty-note">Order-book data unavailable</div>`;
  } else if (sp.note) {
    body = `<div class="liq-empty-note">${sp.note}</div>`;
  } else if (sp.spoof_detected && top) {
    body = `
      ${badge("POSSIBLE SPOOF", "wait")}
      <span class="liq-dual-value">${fmtPrice(top.price)}</span>
      <span class="liq-dual-detail">${"$" + Number(top.usd_size_before).toLocaleString()} pulled · cancelled after ${fmtNum(top.seconds_ago, 0)}s · ${fmtPct(top.cancelled_pct, 0)} of size gone</span>`;
  } else {
    body = `${badge("NO SPOOFING DETECTED", "flat")}<div class="liq-empty-note">Order-book levels stable between polls</div>`;
  }
  return liqCard({ icon: "⚠", title: "Possible Spoofing", subtitle: "CH.21 · resting-order vanish scan", body });
}

function renderTrapSqueezeCard(extra) {
  const t = extra.trap_squeeze || {};
  const rows = [
    ["BULL TRAP", t.bull_trap, "c-short"],
    ["BEAR TRAP", t.bear_trap, "c-long"],
    ["SHORT SQUEEZE", t.short_squeeze, "c-long"],
    ["LONG SQUEEZE", t.long_squeeze, "c-short"],
  ];
  return liqCard({
    icon: "⤬", title: "Trap & Squeeze Risk", subtitle: "CH.23 · reversal & liquidation pressure", span2: true,
    body: `<div class="trap-rows">${rows.map(([label, v, cls]) => `
      <div class="trap-row">
        <span class="trap-label">${label}</span>
        ${meterBar(na(v) ? 0 : v, cls)}
        <span class="trap-value">${na(v) ? "--" : v}</span>
      </div>`).join("")}</div>`,
  });
}

function renderZonesCard(extra) {
  const zones = extra.liquidity_zones || [];
  const rows = zones.length ? zones.map((z) => `
    <div class="zone-row">
      ${badge(z.side === "BUY_WALL" ? "BUY WALL" : "SELL WALL", z.side === "BUY_WALL" ? "long" : "short")}
      <span class="zone-price">${fmtPrice(z.price)}</span>
      <span class="zone-usd">${"$" + Number(z.usd_size).toLocaleString()}</span>
      <span class="zone-dist text-dim">${fmtPct(z.distance_pct)}</span>
      <span class="zone-score">${fmtNum(z.score, 0)}/100</span>
    </div>`).join("") : `<div class="liq-empty-note">No order-book data yet</div>`;
  return liqCard({
    icon: "▤", title: "Liquidity Target Zones", subtitle: "CH.24 · largest resting order-book walls", span2: true,
    body: `<div class="zones-list">${rows}</div>`,
  });
}

function renderFundingCard(extra) {
  const f = extra.funding_open_interest || {};
  const body = f.available ? `
    <div class="liq-dual-stat">
      <div class="liq-dual-col">
        <span class="liq-dual-label">FUNDING RATE</span>
        <span class="liq-dual-value ${na(f.funding_rate_pct) ? "" : (f.funding_rate_pct >= 0 ? "text-long" : "text-short")}">${na(f.funding_rate_pct) ? "--" : fmtSigned(f.funding_rate_pct, 4, "%")}</span>
      </div>
      <div class="liq-dual-col">
        <span class="liq-dual-label">OPEN INTEREST</span>
        <span class="liq-dual-value">${na(f.open_interest) ? "--" : Number(f.open_interest).toLocaleString()}</span>
      </div>
    </div>` : `<div class="liq-empty-note">Perpetual market not available for this pair on OKX</div>`;
  return liqCard({ icon: "%", title: "Funding Rate + Open Interest", subtitle: "CH.25 · perpetual swap data", body });
}

function renderCvdCard(extra) {
  const c = extra.cvd || {};
  const tone = c.trend === "RISING" ? "long" : c.trend === "FALLING" ? "short" : "flat";
  return liqCard({
    icon: "∿", title: "CVD · Volume Delta", subtitle: "CH.26 · approximate cumulative delta",
    body: `
      <div class="cvd-top">
        <span class="liq-dual-value">${na(c.cvd) ? "--" : fmtNum(c.cvd, 2)}</span>
        ${badge(c.trend || "--", tone)}
      </div>
      ${sparkline(c.series, tone)}`,
  });
}

function renderCrashCard(extra) {
  const c = extra.crash_risk || {};
  const tone = c.label === "ELEVATED" ? "short" : c.label === "WATCH" ? "wait" : "long";
  const factors = c.factors || [];
  return liqCard({
    icon: "☢", title: "Market Crash Risk", subtitle: "CH.27 · composite down-side stress checklist", span2: true,
    body: `
      <div class="crash-head">
        <span class="crash-score text-${tone}">${na(c.score) ? "--" : c.score}</span>
        ${badge(c.label || "--", tone)}
      </div>
      ${factors.length ? `<ul class="crash-factors">${factors.map((f) => `<li>${f}</li>`).join("")}</ul>` : `<div class="liq-empty-note">No elevated stress factors right now</div>`}`,
  });
}

function renderLiquidityExtras(extraRaw) {
  const extra = extraRaw || {};
  return [
    renderMagnetTargetCard(extra),
    renderMarketStrengthCard(extra),
    renderSpoofingCard(extra),
    renderTrapSqueezeCard(extra),
    renderZonesCard(extra),
    renderFundingCard(extra),
    renderCvdCard(extra),
    renderCrashCard(extra),
  ].join("");
}

function paintLiquidityScanner(sweep, price, extra) {
  if (!liqScannerEl) return;

  const hasRange = !na(sweep.swing_high) && !na(sweep.swing_low) && !na(price) && sweep.swing_high > sweep.swing_low;
  let markerPct = 50;
  if (hasRange) {
    const range = sweep.swing_high - sweep.swing_low;
    markerPct = Math.max(2, Math.min(98, ((price - sweep.swing_low) / range) * 100));
  }

  const detected = !!sweep.liquidity_sweep_detected;
  const tone = detected ? "wait" : "flat";
  const statusLabel = detected ? (sweep.sweep_direction || "SWEEP").replace(/_/g, " ") : "NO SWEEP DETECTED";

  liqScannerEl.innerHTML = `
    <div class="liq-panel">
      <div class="liq-header">
        <div class="liq-header-left">
          <span class="liq-icon">⌁</span>
          <div>
            <div class="liq-title">Liquidity Sweep Scanner</div>
            <div class="liq-subtitle"><span class="live-dot"></span>CH.19 · swing high/low · auto-refreshing</div>
          </div>
        </div>
        ${badge(statusLabel, tone)}
      </div>

      <div class="liq-range">
        <div class="liq-range-track">
          <div class="liq-range-fill"></div>
          <div class="liq-range-endpoint" style="left:0%;"></div>
          <div class="liq-range-endpoint" style="left:100%;"></div>
          <div class="liq-range-endpoint-label" style="left:0%;">${fmtPrice(sweep.swing_low)}</div>
          <div class="liq-range-endpoint-label" style="left:100%;">${fmtPrice(sweep.swing_high)}</div>
          ${hasRange ? `
            <div class="liq-range-marker" style="left:${markerPct}%;"></div>
            <div class="liq-range-marker-label" style="left:${markerPct}%;">${fmtPrice(price)}</div>
          ` : ""}
        </div>
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
    </div>

    <div class="liq-extra-grid">${renderLiquidityExtras(extra)}</div>`;
}

function renderLiquidityScanner(data) {
  paintLiquidityScanner(data.liquidity_sweep || {}, data.last_price, lastLiquidityExtra);
}

async function fetchAndRenderLiquidity(coin) {
  if (!coin || liquidityFetchInFlight) return;
  liquidityFetchInFlight = true;
  try {
    const res = await fetch(`/liquidity?coin=${encodeURIComponent(coin)}&timeframe=1h`);
    const data = await res.json();
    if (!res.ok || data.error || coin !== currentCoin) return;
    lastLiquidityExtra = data;
    paintLiquidityScanner(data.liquidity_sweep || {}, data.last_price, data);
  } catch (e) {
    // Silent — a single missed poll shouldn't spam the UI; next tick retries.
  } finally {
    liquidityFetchInFlight = false;
  }
}

function startLiquidityPolling() {
  if (liquidityPollTimer) return;
  liquidityPollTimer = setInterval(() => {
    if (currentCoin) fetchAndRenderLiquidity(currentCoin);
  }, LIQUIDITY_POLL_MS);
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
let chartLimit = 300;

// Range presets pick a candle interval big enough that the requested
// history actually fits in one request (OKX has no native 6M/1Y/5Y candle
// size, so longer ranges fall back to daily/weekly candles instead).
const CHART_RANGE_PRESETS = {
  "1D": { tf: "5m", limit: 288 },
  "1M": { tf: "4h", limit: 186 },
  "3M": { tf: "1d", limit: 92 },
  "6M": { tf: "1d", limit: 183 },
  "1Y": { tf: "1d", limit: 366 },
  "5Y": { tf: "1w", limit: 262 },
};

function ensureChartInitialized() {
  if (lwChart || !candleChartEl || typeof LightweightCharts === "undefined") return;

  lwChart = LightweightCharts.createChart(candleChartEl, {
    layout: {
      background: { type: "solid", color: "transparent" },
      textColor: "#8b96a5",
      fontFamily: "IBM Plex Mono, monospace",
      fontSize: 11,
      attributionLogo: false,
    },
    grid: {
      vertLines: { color: "rgba(39, 48, 59, 0.5)" },
      horzLines: { color: "rgba(39, 48, 59, 0.5)" },
    },
    rightPriceScale: { borderColor: "#27303b" },
    timeScale: { borderColor: "#27303b", timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });

  // lightweight-charts v5 series API — a paneIndex can be passed as the 3rd
  // arg so oscillator indicators can live on their own panes below the price.
  lwCandleSeries = lwChart.addSeries(LightweightCharts.CandlestickSeries, {
    upColor: "#36e0a0",
    downColor: "#ff526b",
    borderUpColor: "#28f3a5",
    borderDownColor: "#ff6b7e",
    wickUpColor: "#36e0a0",
    wickDownColor: "#ff526b",
  });

  lwChart.subscribeClick(handleChartClick);
  lwChart.subscribeCrosshairMove(handleChartCrosshairMove);
  lwChart.timeScale().subscribeVisibleLogicalRangeChange(() => {
    renderDrawings();
    maybeLoadOlderCandles();
  });

  window.addEventListener("resize", resizeChart);

  renderIndicatorOverlay();
}

function resizeChart() {
  if (!lwChart || !candleChartEl) return;
  lwChart.resize(candleChartEl.clientWidth, candleChartEl.clientHeight);
  renderDrawings();
  renderIndicatorOverlay();
}

/* ==========================================================================
   CHART FULLSCREEN — expands the chart panel to fill the screen with a
   back button top-left to return to the normal layout.
   ========================================================================== */

function setChartFullscreen(on) {
  if (!chartPanelEl) return;
  chartPanelEl.classList.toggle("fullscreen", on);
  document.body.classList.toggle("chart-fullscreen-lock", on);
  // chart canvas size changed — resize on next frame once layout settles
  requestAnimationFrame(() => requestAnimationFrame(resizeChart));
}

if (chartFullscreenBtn) {
  chartFullscreenBtn.addEventListener("click", () => setChartFullscreen(true));
}
if (chartBackBtn) {
  chartBackBtn.addEventListener("click", () => setChartFullscreen(false));
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && indicatorPanelEl && !indicatorPanelEl.hidden) return; // panel handles it
  if (e.key === "Escape" && chartPanelEl && chartPanelEl.classList.contains("fullscreen")) {
    setChartFullscreen(false);
  }
});

/* ==========================================================================
   CHART DRAWING TOOLS — 10 tools drawn on an SVG overlay positioned on top
   of the chart canvas: Cursor, Trend Line, Ray, Horizontal Line, Vertical
   Line, Rectangle, Fibonacci Retracement, Brush (freehand), Text Note, and
   Measure — plus a Clear-All utility. Coordinates are converted through the
   chart's own time/price scales so drawings stay correctly placed while
   panning, zooming, or resizing.
   ========================================================================== */

/* ==========================================================================
   CHART DRAWING TOOLS — a TradingView-style grouped tool palette. Each
   group button shows the last-used tool in that group; hovering or
   clicking the small arrow opens a flyout to pick a different tool from
   the group. Coordinates are converted through the chart's own time/price
   scales so drawings stay correctly placed while panning, zooming, or
   resizing.
   ========================================================================== */

let drawingIdSeq = 0;

// ---- Selection / move / individual-delete state (cursor tool) ----
let selectedDrawingId = null;
let isDraggingDrawing = false;
let dragLastPoint = null;
const HIT_TOLERANCE = 10;

// ---- Multi-click drawing-in-progress state ----
let activeChartTool = "cursor";
let chartDrawings = [];
let pendingPoints = [];      // points collected so far for the drawing being placed
let drawingsCoinKey = null;

// Freehand brush state — driven by native mouse/touch events since the
// chart library's own click/crosshair subscriptions don't expose drag.
let isBrushing = false;
let currentBrushPoints = [];

const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
const FIB_EXT_LEVELS = [0, 0.382, 0.618, 1, 1.272, 1.618, 2, 2.618];
const FIB_SEQUENCE = [1, 2, 3, 5, 8, 13, 21, 34, 55];

// how many chart-clicks each tool needs before the drawing is finalized
const TOOL_ARITY = {
  cursor: 0, brush: 0, path: 0,
  horizontal: 1, vertical: 1, hray: 1, crossline: 1, text: 1, note: 1, icon: 1,
  trendline: 2, ray: 2, extended: 2, trendangle: 2, rectangle: 2, ellipse: 2,
  arrow: 2, measure: 2, fib: 2, fibtimezone: 2, fibfan: 2, fibcircles: 2,
  fibspiral: 2, fibarcs: 2, gannbox: 2, longpos: 2, shortpos: 2,
  pricerange: 2, daterange: 2, callout: 2,
  fibext: 3, fibchannel: 3, fibwedge: 3, pitchfork: 3, triangle: 3,
};

function ic(inner) { return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none">${inner}</svg>`; }

const TOOL_ICONS = {
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
  clear: ic('<path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m-9 0 1 12a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-12" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>'),
};

// Groups drive both the toolbar UI and which tools live together in a
// flyout. `sections` lets a group show a divider + label partway through
// its list (used for the GANN block under the Fib flyout, like TradingView).
const TOOL_GROUPS = [
  { id: "cursor", tools: [{ id: "cursor", label: "Cursor" }] },
  { id: "lines", tools: [
      { id: "trendline", label: "Trend Line" },
      { id: "ray", label: "Ray" },
      { id: "hray", label: "Horizontal Ray" },
      { id: "extended", label: "Extended Line" },
      { id: "trendangle", label: "Trend Angle" },
      { id: "horizontal", label: "Horizontal Line" },
      { id: "vertical", label: "Vertical Line" },
      { id: "crossline", label: "Cross Line" },
  ]},
  { id: "fib", tools: [
      { id: "fib", label: "Fib Retracement" },
      { id: "fibext", label: "Trend-based Fib Extension" },
      { id: "fibchannel", label: "Fib Channel" },
      { id: "fibtimezone", label: "Fib Time Zone" },
      { id: "fibfan", label: "Fib Speed Resistance Fan" },
      { id: "fibcircles", label: "Fib Circles" },
      { id: "fibspiral", label: "Fib Spiral" },
      { id: "fibarcs", label: "Fib Speed Resistance Arcs" },
      { id: "fibwedge", label: "Fib Wedge" },
      { id: "pitchfork", label: "Pitchfan" },
      { id: "gannbox", label: "Gann Box", sectionTitle: "GANN" },
  ]},
  { id: "shapes", tools: [
      { id: "rectangle", label: "Rectangle" },
      { id: "ellipse", label: "Ellipse" },
      { id: "triangle", label: "Triangle" },
      { id: "arrow", label: "Arrow" },
      { id: "path", label: "Path" },
  ]},
  { id: "measuregrp", tools: [
      { id: "measure", label: "Measure" },
      { id: "longpos", label: "Long Position" },
      { id: "shortpos", label: "Short Position" },
      { id: "pricerange", label: "Price Range" },
      { id: "daterange", label: "Date Range" },
  ]},
  { id: "brush", tools: [{ id: "brush", label: "Brush (Freehand)" }] },
  { id: "textgrp", tools: [
      { id: "text", label: "Text" },
      { id: "note", label: "Note" },
      { id: "callout", label: "Callout" },
  ]},
  { id: "icongrp", tools: [{ id: "icon", label: "Icon" }] },
];

const groupOfTool = {};
TOOL_GROUPS.forEach((g) => g.tools.forEach((t) => { groupOfTool[t.id] = g.id; }));
const labelOfTool = {};
TOOL_GROUPS.forEach((g) => g.tools.forEach((t) => { labelOfTool[t.id] = t.label; }));
const groupCurrentTool = {};
TOOL_GROUPS.forEach((g) => { groupCurrentTool[g.id] = g.tools[0].id; });

function renderToolbar() {
  if (!chartToolsEl) return;
  chartToolsEl.innerHTML = "";

  TOOL_GROUPS.forEach((group) => {
    const wrap = document.createElement("div");
    wrap.className = "chart-tool-group";
    wrap.dataset.group = group.id;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chart-tool-btn";
    btn.dataset.tool = groupCurrentTool[group.id];
    btn.title = labelOfTool[groupCurrentTool[group.id]];
    btn.innerHTML = TOOL_ICONS[groupCurrentTool[group.id]];
    wrap.appendChild(btn);

    if (group.tools.length > 1) {
      const arrow = document.createElement("span");
      arrow.className = "chart-tool-arrow";
      arrow.innerHTML = '<svg width="7" height="7" viewBox="0 0 24 24" fill="none"><path d="M4 4l16 8-16 8V4z" fill="currentColor"/></svg>';
      wrap.appendChild(arrow);

      const flyout = document.createElement("div");
      flyout.className = "chart-tool-flyout";
      group.tools.forEach((tool) => {
        if (tool.sectionTitle) {
          const divider = document.createElement("div");
          divider.className = "chart-flyout-section";
          divider.textContent = tool.sectionTitle;
          flyout.appendChild(divider);
        }
        const row = document.createElement("button");
        row.type = "button";
        row.className = "chart-flyout-item";
        row.dataset.tool = tool.id;
        row.innerHTML = `<span class="chart-flyout-icon">${TOOL_ICONS[tool.id]}</span><span class="chart-flyout-label">${tool.label}</span>`;
        row.addEventListener("click", (e) => {
          e.stopPropagation();
          groupCurrentTool[group.id] = tool.id;
          selectChartTool(tool.id);
          closeAllFlyouts();
          renderToolbar();
          // re-apply the active highlight after the toolbar is rebuilt
          const rebuiltBtn = chartToolsEl.querySelector(`.chart-tool-group[data-group="${group.id}"] .chart-tool-btn`);
          if (rebuiltBtn) rebuiltBtn.classList.add("active");
        });
        flyout.appendChild(row);
      });
      wrap.appendChild(flyout);

      let hoverTimer = null;
      wrap.addEventListener("mouseenter", () => {
        hoverTimer = setTimeout(() => openFlyout(wrap), 350);
      });
      wrap.addEventListener("mouseleave", () => {
        clearTimeout(hoverTimer);
      });
      arrow.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleFlyout(wrap);
      });
    }

    btn.addEventListener("click", () => {
      closeAllFlyouts();
      selectChartTool(groupCurrentTool[group.id]);
    });

    chartToolsEl.appendChild(wrap);
  });

  const sep = document.createElement("span");
  sep.className = "chart-tool-sep";
  chartToolsEl.appendChild(sep);

  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "chart-tool-btn chart-tool-danger";
  clearBtn.dataset.tool = "clear";
  clearBtn.title = "Clear all drawings";
  clearBtn.innerHTML = TOOL_ICONS.clear;
  clearBtn.addEventListener("click", () => {
    chartDrawings = [];
    pendingPoints = [];
    isBrushing = false;
    currentBrushPoints = [];
    selectedDrawingId = null;
    isDraggingDrawing = false;
    hideDrawDeleteBtn();
    renderDrawings();
  });
  chartToolsEl.appendChild(clearBtn);

  highlightActiveToolButton();
}

function openFlyout(groupEl) {
  closeAllFlyouts();
  groupEl.classList.add("flyout-open");
}
function toggleFlyout(groupEl) {
  const wasOpen = groupEl.classList.contains("flyout-open");
  closeAllFlyouts();
  if (!wasOpen) groupEl.classList.add("flyout-open");
}
function closeAllFlyouts() {
  if (!chartToolsEl) return;
  chartToolsEl.querySelectorAll(".chart-tool-group.flyout-open").forEach((g) => g.classList.remove("flyout-open"));
}
document.addEventListener("click", (e) => {
  if (chartToolsEl && !chartToolsEl.contains(e.target)) closeAllFlyouts();
});

function highlightActiveToolButton() {
  if (!chartToolsEl) return;
  chartToolsEl.querySelectorAll(".chart-tool-btn").forEach((b) => b.classList.remove("active"));
  const groupId = groupOfTool[activeChartTool];
  const sel = groupId
    ? chartToolsEl.querySelector(`.chart-tool-group[data-group="${groupId}"] .chart-tool-btn`)
    : null;
  if (sel) sel.classList.add("active");
}

function selectChartTool(tool) {
  activeChartTool = tool;
  pendingPoints = [];
  selectedDrawingId = null;
  isDraggingDrawing = false;
  hideDrawDeleteBtn();
  if (candleChartEl) candleChartEl.style.cursor = tool === "cursor" ? "default" : "crosshair";
  setChartInteractionsEnabled(tool !== "brush");
  highlightActiveToolButton();
  renderDrawings();
}

function setChartInteractionsEnabled(enabled) {
  if (!lwChart) return;
  lwChart.applyOptions({ handleScroll: enabled, handleScale: enabled });
}

// small floating "×" button shown next to whichever drawing is selected —
// tapping it removes ONLY that drawing, unlike the toolbar's Clear-All.
let drawDeleteBtnEl = null;
function ensureDrawDeleteBtn() {
  if (drawDeleteBtnEl) return drawDeleteBtnEl;
  const host = drawOverlayEl && drawOverlayEl.parentElement;
  if (!host) return null;
  drawDeleteBtnEl = document.createElement("button");
  drawDeleteBtnEl.type = "button";
  drawDeleteBtnEl.className = "draw-delete-btn";
  drawDeleteBtnEl.title = "Delete this drawing";
  drawDeleteBtnEl.textContent = "×";
  drawDeleteBtnEl.hidden = true;
  drawDeleteBtnEl.addEventListener("mousedown", (e) => e.stopPropagation());
  drawDeleteBtnEl.addEventListener("touchstart", (e) => e.stopPropagation(), { passive: true });
  drawDeleteBtnEl.addEventListener("click", (e) => {
    e.stopPropagation();
    if (selectedDrawingId == null) return;
    chartDrawings = chartDrawings.filter((d) => d.id !== selectedDrawingId);
    selectedDrawingId = null;
    renderDrawings();
  });
  host.appendChild(drawDeleteBtnEl);
  return drawDeleteBtnEl;
}
function positionDrawDeleteBtn(x, y) {
  const btn = ensureDrawDeleteBtn();
  if (!btn) return;
  btn.hidden = false;
  btn.style.left = `${x}px`;
  btn.style.top = `${y}px`;
}
function hideDrawDeleteBtn() {
  if (drawDeleteBtnEl) drawDeleteBtnEl.hidden = true;
}

function chartTimeX(t) { return lwChart ? lwChart.timeScale().timeToCoordinate(t) : null; }
function chartPriceY(p) { return lwCandleSeries ? lwCandleSeries.priceToCoordinate(p) : null; }
function shiftTime(t, deltaTime) { return (typeof t === "number" && typeof deltaTime === "number") ? t + deltaTime : t; }

function distPointToSegment(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1;
  const lenSq = dx * dx + dy * dy;
  let t = lenSq ? ((px - x1) * dx + (py - y1) * dy) / lenSq : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

// Generous bounding-box hit test — used for multi-line drawings (fib
// channel, fan, wedge, pitchfork, circles, spiral, extension) where a
// click could land on any one of several parallel/radiating lines. Testing
// each line individually was fragile (missed the middle lines); treating
// the whole shape like a clickable box — the way rectangle/gann-box
// already work — is what actually matches how people try to select them.
function bboxHit(x, y, points, pad) {
  const xs = points.map((p) => p && p.x).filter((v) => v != null && !Number.isNaN(v));
  const ys = points.map((p) => p && p.y).filter((v) => v != null && !Number.isNaN(v));
  if (!xs.length || !ys.length) return false;
  const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
  const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
  return x >= minX && x <= maxX && y >= minY && y <= maxY;
}

if (chartToolsEl) renderToolbar();

function handleChartClick(param) {
  if (!lwCandleSeries) return;
  if (activeChartTool === "cursor" || activeChartTool === "brush" || activeChartTool === "path") return;
  if (!param.point || param.time === undefined) return;
  const price = lwCandleSeries.coordinateToPrice(param.point.y);
  if (price === null || price === undefined) return;
  const pt = { time: param.time, price };
  const arity = TOOL_ARITY[activeChartTool] || 2;

  if (arity === 1) {
    if (activeChartTool === "text") {
      const text = window.prompt("Note text:");
      if (text && text.trim()) chartDrawings.push({ type: "text", ...pt, text: text.trim(), id: ++drawingIdSeq });
    } else if (activeChartTool === "note") {
      const text = window.prompt("Note:");
      if (text && text.trim()) chartDrawings.push({ type: "note", ...pt, text: text.trim(), id: ++drawingIdSeq });
    } else if (activeChartTool === "icon") {
      const emoji = window.prompt("Icon (emoji), e.g. ⭐ 🚀 🔥 ⚠️ ✅:", "⭐");
      if (emoji && emoji.trim()) chartDrawings.push({ type: "icon", ...pt, emoji: emoji.trim().slice(0, 4), id: ++drawingIdSeq });
    } else if (activeChartTool === "horizontal") {
      chartDrawings.push({ type: "horizontal", price, id: ++drawingIdSeq });
    } else if (activeChartTool === "vertical") {
      chartDrawings.push({ type: "vertical", time: param.time, id: ++drawingIdSeq });
    } else if (activeChartTool === "hray") {
      chartDrawings.push({ type: "hray", ...pt, id: ++drawingIdSeq });
    } else if (activeChartTool === "crossline") {
      chartDrawings.push({ type: "crossline", ...pt, id: ++drawingIdSeq });
    }
    renderDrawings();
    return;
  }

  // 2-point and 3-point tools accumulate clicks in pendingPoints
  pendingPoints.push(pt);
  if (pendingPoints.length < arity) { renderDrawings(); return; }

  const [p1, p2, p3] = pendingPoints;
  let d = { type: activeChartTool, id: ++drawingIdSeq };
  if (arity === 2) { d.p1 = p1; d.p2 = p2; }
  if (arity === 3) { d.p1 = p1; d.p2 = p2; d.p3 = p3; }
  chartDrawings.push(d);
  pendingPoints = [];
  renderDrawings();
}

function handleChartCrosshairMove(param) {
  if (!pendingPoints.length || !lwCandleSeries) return;
  if (!param.point || param.time === undefined) { renderDrawings(); return; }
  const price = lwCandleSeries.coordinateToPrice(param.point.y);
  if (price === null || price === undefined) return;
  renderDrawings({ time: param.time, price });
}

// ---- pointer handling shared by mouse + touch: cursor select/drag, brush,
// and click-to-build path ----

function chartPixelToTimePrice(clientX, clientY) {
  const rect = candleChartEl.getBoundingClientRect();
  const x = clientX - rect.left;
  const y = clientY - rect.top;
  const time = lwChart ? lwChart.timeScale().coordinateToTime(x) : null;
  const price = lwCandleSeries ? lwCandleSeries.coordinateToPrice(y) : null;
  return { time, price };
}
function chartPixelXY(clientX, clientY) {
  const rect = candleChartEl.getBoundingClientRect();
  return { x: clientX - rect.left, y: clientY - rect.top };
}

function onDrawPointerDown(clientX, clientY) {
  if (!lwChart || !lwCandleSeries) return;

  if (activeChartTool === "cursor") {
    const { x, y } = chartPixelXY(clientX, clientY);
    const hit = hitTestDrawing(x, y);
    if (hit) {
      selectedDrawingId = hit.id;
      isDraggingDrawing = true;
      dragLastPoint = chartPixelToTimePrice(clientX, clientY);
      setChartInteractionsEnabled(false);
    } else if (selectedDrawingId !== null) {
      selectedDrawingId = null;
      hideDrawDeleteBtn();
    }
    renderDrawings();
    return;
  }

  if (activeChartTool !== "brush") return;
  isBrushing = true;
  currentBrushPoints = [];
  const pt = chartPixelToTimePrice(clientX, clientY);
  if (pt.time != null && pt.price != null) currentBrushPoints.push(pt);
}

function onDrawPointerMove(clientX, clientY) {
  if (isDraggingDrawing) {
    const pt = chartPixelToTimePrice(clientX, clientY);
    if (pt.time != null && pt.price != null && dragLastPoint) {
      const deltaTime = (typeof pt.time === "number" && typeof dragLastPoint.time === "number")
        ? pt.time - dragLastPoint.time : 0;
      const deltaPrice = pt.price - dragLastPoint.price;
      const d = chartDrawings.find((dd) => dd.id === selectedDrawingId);
      if (d) moveDrawingBy(d, deltaTime, deltaPrice);
      dragLastPoint = pt;
      renderDrawings();
    }
    return true;
  }
  if (!isBrushing) return false;
  const pt = chartPixelToTimePrice(clientX, clientY);
  if (pt.time != null && pt.price != null) {
    currentBrushPoints.push(pt);
    renderDrawings(null, currentBrushPoints);
  }
  return true;
}

function onDrawPointerUp() {
  if (isDraggingDrawing) {
    isDraggingDrawing = false;
    dragLastPoint = null;
    setChartInteractionsEnabled(activeChartTool !== "brush");
    return;
  }
  if (!isBrushing) return;
  isBrushing = false;
  if (currentBrushPoints.length > 1) {
    chartDrawings.push({ type: "brush", points: currentBrushPoints.slice(), id: ++drawingIdSeq });
  }
  currentBrushPoints = [];
  renderDrawings();
}

if (candleChartEl) {
  candleChartEl.addEventListener("mousedown", (e) => {
    if (activeChartTool !== "cursor" && activeChartTool !== "brush") return;
    onDrawPointerDown(e.clientX, e.clientY);
  });
  candleChartEl.addEventListener("touchstart", (e) => {
    if (activeChartTool !== "cursor" && activeChartTool !== "brush") return;
    const t = e.touches[0];
    if (!t) return;
    onDrawPointerDown(t.clientX, t.clientY);
    if (isDraggingDrawing || isBrushing) e.preventDefault();
  }, { passive: false });

  // Path tool: click to add each point, double-click to finish.
  candleChartEl.addEventListener("click", (e) => {
    if (activeChartTool !== "path") return;
    const pt = chartPixelToTimePrice(e.clientX, e.clientY);
    if (pt.time != null && pt.price != null) {
      pendingPoints.push(pt);
      renderDrawings();
    }
  });
  candleChartEl.addEventListener("dblclick", (e) => {
    if (activeChartTool !== "path") return;
    e.preventDefault();
    if (pendingPoints.length > 1) {
      chartDrawings.push({ type: "path", points: pendingPoints.slice(), id: ++drawingIdSeq });
    }
    pendingPoints = [];
    renderDrawings();
  });

  window.addEventListener("mousemove", (e) => onDrawPointerMove(e.clientX, e.clientY));
  window.addEventListener("touchmove", (e) => {
    const t = e.touches[0];
    if (!t) return;
    if (onDrawPointerMove(t.clientX, t.clientY)) e.preventDefault();
  }, { passive: false });

  window.addEventListener("mouseup", onDrawPointerUp);
  window.addEventListener("touchend", onDrawPointerUp);
  window.addEventListener("touchcancel", onDrawPointerUp);
}

// Escape cancels an in-progress multi-click drawing (fib/path/pitchfork/etc).
// Delete/Backspace removes only the currently-selected drawing.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && pendingPoints.length) {
    pendingPoints = [];
    renderDrawings();
    return;
  }
  if (selectedDrawingId == null) return;
  const tag = (document.activeElement && document.activeElement.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  if (e.key === "Delete" || e.key === "Backspace") {
    e.preventDefault();
    chartDrawings = chartDrawings.filter((d) => d.id !== selectedDrawingId);
    selectedDrawingId = null;
    hideDrawDeleteBtn();
    renderDrawings();
  }
});

// ============================== HIT TESTING ==============================

function hitTestDrawing(x, y) {
  if (!lwChart || !lwCandleSeries || !candleChartEl) return null;
  const width = candleChartEl.clientWidth, height = candleChartEl.clientHeight;
  const okXYVal = (v) => v != null && !Number.isNaN(v);
  for (let i = chartDrawings.length - 1; i >= 0; i--) {
    const d = chartDrawings[i];
    const P1 = d.p1 ? { x: chartTimeX(d.p1.time), y: chartPriceY(d.p1.price) } : null;
    const P2 = d.p2 ? { x: chartTimeX(d.p2.time), y: chartPriceY(d.p2.price) } : null;
    const P3 = d.p3 ? { x: chartTimeX(d.p3.time), y: chartPriceY(d.p3.price) } : null;
    const ok2 = P1 && P2 && [P1.x, P1.y, P2.x, P2.y].every((v) => v != null && !Number.isNaN(v));

    if (d.type === "horizontal") {
      const y0 = chartPriceY(d.price);
      if (y0 != null && Math.abs(y - y0) <= HIT_TOLERANCE) return d;
    } else if (d.type === "vertical") {
      const x0 = chartTimeX(d.time);
      if (x0 != null && Math.abs(x - x0) <= HIT_TOLERANCE) return d;
    } else if (d.type === "hray") {
      const x0 = chartTimeX(d.time), y0 = chartPriceY(d.price);
      if (x0 != null && y0 != null && x >= x0 - HIT_TOLERANCE && Math.abs(y - y0) <= HIT_TOLERANCE) return d;
    } else if (d.type === "crossline") {
      const x0 = chartTimeX(d.time), y0 = chartPriceY(d.price);
      if ((x0 != null && Math.abs(x - x0) <= HIT_TOLERANCE) || (y0 != null && Math.abs(y - y0) <= HIT_TOLERANCE)) return d;
    } else if (d.type === "text" || d.type === "note" || d.type === "icon") {
      const tx = chartTimeX(d.time), ty = chartPriceY(d.price);
      if (tx != null && ty != null && Math.hypot(x - tx, y - ty) <= HIT_TOLERANCE + 10) return d;
    } else if (d.type === "brush" || d.type === "path") {
      for (let j = 0; j < d.points.length - 1; j++) {
        const a = { x: chartTimeX(d.points[j].time), y: chartPriceY(d.points[j].price) };
        const b = { x: chartTimeX(d.points[j + 1].time), y: chartPriceY(d.points[j + 1].price) };
        if ([a.x, a.y, b.x, b.y].every((v) => v != null) && distPointToSegment(x, y, a.x, a.y, b.x, b.y) <= HIT_TOLERANCE) return d;
      }
    } else if (d.type === "rectangle" || d.type === "gannbox" || d.type === "longpos" || d.type === "shortpos" || d.type === "ellipse") {
      if (!ok2) continue;
      const minX = Math.min(P1.x, P2.x) - HIT_TOLERANCE, maxX = Math.max(P1.x, P2.x) + HIT_TOLERANCE;
      const minY = Math.min(P1.y, P2.y) - HIT_TOLERANCE, maxY = Math.max(P1.y, P2.y) + HIT_TOLERANCE;
      if (x >= minX && x <= maxX && y >= minY && y <= maxY) return d;
    } else if (d.type === "triangle") {
      if (!P1 || !P2 || !P3) continue;
      const pts = [P1, P2, P3];
      for (let j = 0; j < 3; j++) {
        const a = pts[j], b = pts[(j + 1) % 3];
        if (a.x != null && b.x != null && distPointToSegment(x, y, a.x, a.y, b.x, b.y) <= HIT_TOLERANCE) return d;
      }
    } else if (d.type === "fib" || d.type === "pricerange") {
      if (!ok2) continue;
      for (const lvl of FIB_LEVELS) {
        const py = chartPriceY(d.p1.price + (d.p2.price - d.p1.price) * lvl);
        if (py != null && Math.abs(y - py) <= HIT_TOLERANCE) return d;
      }
    } else if (d.type === "fibext") {
      if (!d.p3) continue;
      const x3 = chartTimeX(d.p3.time);
      const levelYs = FIB_EXT_LEVELS.map((lvl) => chartPriceY(d.p3.price + (d.p2.price - d.p1.price) * lvl));
      if (bboxHit(x, y, [{ x: x3, y: levelYs[0] }, { x: width, y: levelYs[0] }, ...levelYs.map((py) => ({ x: width, y: py }))], HIT_TOLERANCE)) return d;
    } else if (d.type === "fibtimezone") {
      const unit = (typeof d.p2.time === "number" && typeof d.p1.time === "number") ? d.p2.time - d.p1.time : null;
      if (unit == null) continue;
      for (const n of FIB_SEQUENCE) {
        const x0 = chartTimeX(d.p1.time + n * unit);
        if (x0 != null && Math.abs(x - x0) <= HIT_TOLERANCE) return d;
      }
    } else if (d.type === "daterange") {
      if (!ok2) continue;
      const yMid = (P1.y + P2.y) / 2;
      if (Math.abs(y - yMid) <= HIT_TOLERANCE + 6 && x >= Math.min(P1.x, P2.x) - HIT_TOLERANCE && x <= Math.max(P1.x, P2.x) + HIT_TOLERANCE) return d;
    } else if (d.type === "fibfan") {
      if (!ok2) continue;
      const extPts = [0.236, 0.382, 0.5, 0.618, 0.786].map((lvl) => {
        const py = chartPriceY(d.p1.price + (d.p2.price - d.p1.price) * lvl);
        return okXYVal(py) ? extendRay(P1, { x: P2.x, y: py }, width) : null;
      }).filter(Boolean);
      if (bboxHit(x, y, [P1, P2, ...extPts], HIT_TOLERANCE)) return d;
    } else if (d.type === "fibcircles" || d.type === "fibarcs") {
      if (!ok2) continue;
      const r0 = Math.hypot(P2.x - P1.x, P2.y - P1.y) * 2.618;
      if (bboxHit(x, y, [{ x: P1.x - r0, y: P1.y - r0 }, { x: P1.x + r0, y: P1.y + r0 }], HIT_TOLERANCE)) return d;
    } else if (d.type === "fibspiral") {
      if (!ok2) continue;
      const pts = fibSpiralPoints(P1, P2);
      if (bboxHit(x, y, pts, HIT_TOLERANCE)) return d;
    } else if (d.type === "fibwedge") {
      if (!P1 || !P2 || !P3) continue;
      if (bboxHit(x, y, [P1, P2, P3], HIT_TOLERANCE)) return d;
    } else if (d.type === "pitchfork") {
      if (!P1 || !P2 || !P3) continue;
      const mid = { x: (P2.x + P3.x) / 2, y: (P2.y + P3.y) / 2 };
      const medExt = extendRay(P1, mid, width);
      const p2Par = { x: P2.x + (medExt.x - P1.x), y: P2.y + (medExt.y - P1.y) };
      const p3Par = { x: P3.x + (medExt.x - P1.x), y: P3.y + (medExt.y - P1.y) };
      if (bboxHit(x, y, [P1, P2, P3, medExt, p2Par, p3Par], HIT_TOLERANCE)) return d;
    } else if (d.type === "fibchannel") {
      if (!P1 || !P2 || !P3) continue;
      const offsetY = P3.y - lerpY(P1, P2, P3.x);
      if (bboxHit(x, y, [P1, P2, { x: P1.x, y: P1.y + offsetY }, { x: P2.x, y: P2.y + offsetY }], HIT_TOLERANCE)) return d;
    } else if (d.type === "extended" || d.type === "trendangle" || d.type === "arrow" || d.type === "trendline" || d.type === "measure" || d.type === "callout") {
      if (!ok2) continue;
      if (distPointToSegment(x, y, P1.x, P1.y, P2.x, P2.y) <= HIT_TOLERANCE) return d;
    } else if (d.type === "ray") {
      if (!ok2) continue;
      const ext = extendRay(P1, P2, width);
      if (distPointToSegment(x, y, P1.x, P1.y, ext.x, ext.y) <= HIT_TOLERANCE) return d;
    }
  }
  return null;
}

function lerpY(p1, p2, atX) {
  if (p2.x === p1.x) return p1.y;
  const t = (atX - p1.x) / (p2.x - p1.x);
  return p1.y + t * (p2.y - p1.y);
}
function extendRay(p1, p2, width) {
  let ex = p2.x, ey = p2.y;
  const dx = p2.x - p1.x, dy = p2.y - p1.y;
  if (Math.abs(dx) > 0.0001) {
    const t = dx > 0 ? (width - p1.x) / dx : (0 - p1.x) / dx;
    ex = p1.x + t * dx; ey = p1.y + t * dy;
  }
  return { x: ex, y: ey };
}
function fibSpiralPoints(center, edge) {
  const r0 = Math.max(6, Math.hypot(edge.x - center.x, edge.y - center.y));
  const baseAngle = Math.atan2(edge.y - center.y, edge.x - center.x);
  const phi = 1.6180339887;
  const b = Math.log(phi) / (Math.PI / 2);
  const pts = [];
  for (let i = 0; i <= 80; i++) {
    const theta = (i / 80) * Math.PI * 4;
    const r = r0 * Math.exp(-b * theta) ;
    pts.push({ x: center.x + r * Math.cos(theta + baseAngle), y: center.y + r * Math.sin(theta + baseAngle) });
  }
  return pts;
}

// ============================== DRAG / MOVE ==============================

function moveDrawingBy(d, deltaTime, deltaPrice) {
  if (d.type === "horizontal") {
    d.price += deltaPrice;
  } else if (d.type === "vertical") {
    d.time = shiftTime(d.time, deltaTime);
  } else if (d.type === "hray" || d.type === "crossline" || d.type === "text" || d.type === "note" || d.type === "icon") {
    d.time = shiftTime(d.time, deltaTime);
    d.price += deltaPrice;
  } else if (d.type === "brush" || d.type === "path") {
    d.points.forEach((p) => { p.time = shiftTime(p.time, deltaTime); p.price += deltaPrice; });
  } else {
    ["p1", "p2", "p3"].forEach((k) => {
      if (d[k]) { d[k].time = shiftTime(d[k].time, deltaTime); d[k].price += deltaPrice; }
    });
  }
}

function renderDrawings(previewPoint, liveBrushPoints) {
  if (!drawOverlayEl || !lwChart || !lwCandleSeries || !candleChartEl) return;
  while (drawOverlayEl.firstChild) drawOverlayEl.removeChild(drawOverlayEl.firstChild);

  const width = candleChartEl.clientWidth, height = candleChartEl.clientHeight;
  drawOverlayEl.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const NS = "http://www.w3.org/2000/svg";
  const timeX = chartTimeX, priceY = chartPriceY;
  const okXY = (...vals) => vals.every((v) => v !== null && v !== undefined && !Number.isNaN(v));
  const el = (tag, attrs) => {
    const n = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v));
    drawOverlayEl.appendChild(n);
    return n;
  };

  let selectedAnchor = null;
  function ring(cx, cy, r) {
    el("circle", { cx, cy, r, fill: "none", stroke: "#ffffff", "stroke-width": "1.4", "stroke-dasharray": "3 2", opacity: "0.85" });
  }

  chartDrawings.forEach((d) => {
    const isSel = d.id === selectedDrawingId;
    const P1 = d.p1 ? { x: timeX(d.p1.time), y: priceY(d.p1.price) } : null;
    const P2 = d.p2 ? { x: timeX(d.p2.time), y: priceY(d.p2.price) } : null;
    const P3 = d.p3 ? { x: timeX(d.p3.time), y: priceY(d.p3.price) } : null;
    const ok2 = P1 && P2 && okXY(P1.x, P1.y, P2.x, P2.y);
    const ok3 = ok2 && P3 && okXY(P3.x, P3.y);

    if (d.type === "horizontal") {
      const y = priceY(d.price);
      if (!okXY(y)) return;
      el("line", { x1: 0, x2: width, y1: y, y2: y, stroke: "#f5a623", "stroke-width": isSel ? "2.4" : "1.4", "stroke-dasharray": "4 3" });
      el("text", { x: width - 8, y: y - 6, "text-anchor": "end", fill: "#f5a623", "font-size": "10", "font-family": "IBM Plex Mono, monospace", "font-weight": "700" }).textContent = fmtPrice(d.price);
      if (isSel) selectedAnchor = { x: width - 20, y: y - 6 };
      return;
    }

    if (d.type === "vertical") {
      const x = timeX(d.time);
      if (!okXY(x)) return;
      el("line", { x1: x, x2: x, y1: 0, y2: height, stroke: "#4dabf7", "stroke-width": isSel ? "2.4" : "1.4", "stroke-dasharray": "4 3" });
      if (isSel) selectedAnchor = { x, y: 20 };
      return;
    }

    if (d.type === "hray") {
      const x = timeX(d.time), y = priceY(d.price);
      if (!okXY(x, y)) return;
      el("line", { x1: x, x2: width, y1: y, y2: y, stroke: "#f5a623", "stroke-width": isSel ? "2.4" : "1.4" });
      el("circle", { cx: x, cy: y, r: "3", fill: "#f5a623" });
      if (isSel) selectedAnchor = { x, y };
      return;
    }

    if (d.type === "crossline") {
      const x = timeX(d.time), y = priceY(d.price);
      if (!okXY(x, y)) return;
      el("line", { x1: x, x2: x, y1: 0, y2: height, stroke: "#c084fc", "stroke-width": isSel ? "2.2" : "1.2", "stroke-dasharray": "4 3" });
      el("line", { x1: 0, x2: width, y1: y, y2: y, stroke: "#c084fc", "stroke-width": isSel ? "2.2" : "1.2", "stroke-dasharray": "4 3" });
      if (isSel) selectedAnchor = { x, y };
      return;
    }

    if (d.type === "text" || d.type === "note") {
      const x = timeX(d.time), y = priceY(d.price);
      if (!okXY(x, y)) return;
      if (isSel) ring(x, y, 9);
      el("circle", { cx: x, cy: y, r: "2.5", fill: "#f5a623" });
      if (d.type === "note") {
        const w = Math.max(40, d.text.length * 6 + 12);
        el("rect", { x: x + 6, y: y - 26, width: w, height: 20, rx: 4, fill: "rgba(20,24,32,0.92)", stroke: "#f5a623", "stroke-width": "1" });
        el("text", { x: x + 12, y: y - 12, fill: "#f5a623", "font-size": "11", "font-family": "IBM Plex Mono, monospace" }).textContent = d.text;
      } else {
        el("text", { x: x + 8, y: y - 8, fill: "#f5a623", "font-size": "12", "font-family": "IBM Plex Mono, monospace", "font-weight": "700" }).textContent = d.text;
      }
      if (isSel) selectedAnchor = { x, y };
      return;
    }

    if (d.type === "icon") {
      const x = timeX(d.time), y = priceY(d.price);
      if (!okXY(x, y)) return;
      if (isSel) ring(x, y, 13);
      el("text", { x, y: y + 6, "text-anchor": "middle", "font-size": "20" }).textContent = d.emoji;
      if (isSel) selectedAnchor = { x, y: y - 14 };
      return;
    }

    if (d.type === "brush" || d.type === "path") {
      const pts = d.points.map((p) => ({ x: timeX(p.time), y: priceY(p.price) })).filter((p) => okXY(p.x, p.y));
      if (pts.length < 2) return;
      const pointsAttr = pts.map((p) => `${p.x},${p.y}`).join(" ");
      const color = d.type === "brush" ? "#c084fc" : "#4dabf7";
      el("polyline", { points: pointsAttr, fill: "none", stroke: color, "stroke-width": isSel ? "3.2" : "2", "stroke-linejoin": "round", "stroke-linecap": "round" });
      if (d.type === "path") pts.forEach((p) => el("circle", { cx: p.x, cy: p.y, r: "2.5", fill: color }));
      if (isSel) selectedAnchor = pts[0];
      return;
    }

    if (d.type === "rectangle" || d.type === "ellipse") {
      if (!ok2) return;
      const x = Math.min(P1.x, P2.x), y = Math.min(P1.y, P2.y);
      const w = Math.abs(P2.x - P1.x), h = Math.abs(P2.y - P1.y);
      if (d.type === "rectangle") {
        el("rect", { x, y, width: w, height: h, fill: "rgba(77,171,247,0.12)", stroke: "#4dabf7", "stroke-width": isSel ? "2.6" : "1.4", ...(isSel ? { "stroke-dasharray": "5 3" } : {}) });
      } else {
        el("ellipse", { cx: x + w / 2, cy: y + h / 2, rx: w / 2, ry: h / 2, fill: "rgba(77,171,247,0.12)", stroke: "#4dabf7", "stroke-width": isSel ? "2.6" : "1.4" });
      }
      if (isSel) selectedAnchor = { x: Math.max(P1.x, P2.x), y };
      return;
    }

    if (d.type === "triangle") {
      const pts = [P1, P2, P3].filter(Boolean);
      if (pts.length < 2) return;
      if (pts.length === 3 && okXY(P3.x, P3.y)) {
        el("polygon", { points: pts.map((p) => `${p.x},${p.y}`).join(" "), fill: "rgba(77,171,247,0.12)", stroke: "#4dabf7", "stroke-width": isSel ? "2.4" : "1.5" });
        if (isSel) selectedAnchor = P1;
      } else if (ok2) {
        el("line", { x1: P1.x, y1: P1.y, x2: P2.x, y2: P2.y, stroke: "#4dabf7", "stroke-width": "1.2", "stroke-dasharray": "3 3" });
      }
      return;
    }

    if (d.type === "arrow") {
      if (!ok2) return;
      el("line", { x1: P1.x, y1: P1.y, x2: P2.x, y2: P2.y, stroke: "#36e0a0", "stroke-width": isSel ? "2.8" : "1.8" });
      const ang = Math.atan2(P2.y - P1.y, P2.x - P1.x);
      const ah = 9;
      const a1 = { x: P2.x - ah * Math.cos(ang - 0.4), y: P2.y - ah * Math.sin(ang - 0.4) };
      const a2 = { x: P2.x - ah * Math.cos(ang + 0.4), y: P2.y - ah * Math.sin(ang + 0.4) };
      el("polygon", { points: `${P2.x},${P2.y} ${a1.x},${a1.y} ${a2.x},${a2.y}`, fill: "#36e0a0" });
      if (isSel) selectedAnchor = P2;
      return;
    }

    if (d.type === "trendline" || d.type === "extended" || d.type === "trendangle" || d.type === "measure" || d.type === "callout") {
      if (!ok2) return;
      let x1 = P1.x, y1 = P1.y, x2 = P2.x, y2 = P2.y;
      if (d.type === "extended") {
        const dx = P2.x - P1.x, dy = P2.y - P1.y;
        if (Math.abs(dx) > 0.0001) {
          const tA = (0 - P1.x) / dx, tB = (width - P1.x) / dx;
          x1 = 0; y1 = P1.y + tA * dy; x2 = width; y2 = P1.y + tB * dy;
        }
      }
      const up = d.p2.price >= d.p1.price;
      const color = d.type === "measure" ? (up ? "#36e0a0" : "#ff526b") : (d.type === "callout" ? "#f5a623" : "#4dabf7");
      el("line", { x1, y1, x2, y2, stroke: color, "stroke-width": isSel ? "2.8" : "1.8" });
      if (d.type === "trendline" || d.type === "extended") {
        el("circle", { cx: P1.x, cy: P1.y, r: "2.6", fill: color });
        el("circle", { cx: P2.x, cy: P2.y, r: "2.6", fill: color });
      }
      if (d.type === "trendangle") {
        const angDeg = (Math.atan2(-(P2.y - P1.y), P2.x - P1.x) * 180) / Math.PI;
        el("text", { x: (P1.x + P2.x) / 2, y: (P1.y + P2.y) / 2 - 6, fill: color, "font-size": "10", "font-family": "IBM Plex Mono, monospace" }).textContent = `${angDeg.toFixed(1)}°`;
      }
      if (d.type === "measure") {
        const pct = ((d.p2.price - d.p1.price) / d.p1.price) * 100;
        el("rect", { x: (x1 + x2) / 2 - 34, y: (y1 + y2) / 2 - 18, width: 68, height: 16, rx: 3, fill: color, opacity: "0.9" });
        el("text", { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 6, "text-anchor": "middle", fill: "#0b0f14", "font-size": "10", "font-weight": "700", "font-family": "IBM Plex Mono, monospace" }).textContent = `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
      }
      if (d.type === "callout") {
        const w = Math.max(50, (d.text || "").length * 6 + 14);
        el("rect", { x: P2.x, y: P2.y - 20, width: w, height: 20, rx: 4, fill: "rgba(20,24,32,0.92)", stroke: color, "stroke-width": "1" });
        el("text", { x: P2.x + 6, y: P2.y - 6, fill: color, "font-size": "11", "font-family": "IBM Plex Mono, monospace" }).textContent = d.text || "";
      }
      if (isSel) selectedAnchor = { x: Math.max(x1, x2), y: Math.min(y1, y2) };
      return;
    }

    if (d.type === "ray") {
      if (!ok2) return;
      const ext = extendRay(P1, P2, width);
      el("line", { x1: P1.x, y1: P1.y, x2: ext.x, y2: ext.y, stroke: "#36e0a0", "stroke-width": isSel ? "2.8" : "1.8" });
      el("circle", { cx: P1.x, cy: P1.y, r: "3.5", fill: "#36e0a0" });
      if (isSel) selectedAnchor = P1;
      return;
    }

    if (d.type === "fib" || d.type === "pricerange") {
      if (!ok2) return;
      FIB_LEVELS.forEach((lvl) => {
        const price = d.p1.price + (d.p2.price - d.p1.price) * lvl;
        const py = priceY(price);
        if (!okXY(py)) return;
        el("line", { x1: Math.min(P1.x, P2.x), x2: Math.max(P1.x, P2.x), y1: py, y2: py, stroke: "#f5a623", "stroke-width": lvl === 0 || lvl === 1 ? "1.6" : "1", opacity: "0.85" });
        if (d.type === "fib") el("text", { x: Math.max(P1.x, P2.x) + 4, y: py + 3, fill: "#f5a623", "font-size": "9", "font-family": "IBM Plex Mono, monospace" }).textContent = `${(lvl * 100).toFixed(1)}% · ${fmtPrice(price)}`;
      });
      if (d.type === "pricerange") {
        const pct = ((d.p2.price - d.p1.price) / d.p1.price) * 100;
        el("text", { x: P2.x + 6, y: (P1.y + P2.y) / 2, fill: "#f5a623", "font-size": "10", "font-family": "IBM Plex Mono, monospace" }).textContent = `${fmtPrice(Math.abs(d.p2.price - d.p1.price))} (${pct.toFixed(2)}%)`;
      }
      if (isSel) { const yMid = priceY((d.p1.price + d.p2.price) / 2); if (okXY(yMid)) selectedAnchor = { x: Math.max(P1.x, P2.x) + 16, y: yMid }; }
      return;
    }

    if (d.type === "fibext") {
      if (!ok3) return;
      FIB_EXT_LEVELS.forEach((lvl) => {
        const price = d.p3.price + (d.p2.price - d.p1.price) * lvl;
        const py = priceY(price);
        if (!okXY(py)) return;
        el("line", { x1: P3.x, x2: width, y1: py, y2: py, stroke: "#c084fc", "stroke-width": lvl === 1 ? "1.6" : "1", opacity: "0.85" });
        el("text", { x: width - 6, y: py - 3, "text-anchor": "end", fill: "#c084fc", "font-size": "9", "font-family": "IBM Plex Mono, monospace" }).textContent = `${(lvl * 100).toFixed(1)}%`;
      });
      if (isSel) selectedAnchor = P3;
      return;
    }

    if (d.type === "fibchannel") {
      if (!ok3) return;
      const offsetY = P3.y - lerpY(P1, P2, P3.x);
      FIB_LEVELS.forEach((lvl) => {
        el("line", { x1: P1.x, y1: P1.y + offsetY * lvl, x2: P2.x, y2: P2.y + offsetY * lvl, stroke: "#4dabf7", "stroke-width": lvl === 0 || lvl === 1 ? "1.6" : "1", opacity: "0.85" });
      });
      if (isSel) selectedAnchor = P3;
      return;
    }

    if (d.type === "fibtimezone") {
      if (!ok2) return;
      const unit = (typeof d.p2.time === "number" && typeof d.p1.time === "number") ? d.p2.time - d.p1.time : null;
      if (unit == null) return;
      FIB_SEQUENCE.forEach((n) => {
        const x0 = timeX(d.p1.time + n * unit);
        if (!okXY(x0)) return;
        el("line", { x1: x0, x2: x0, y1: 0, y2: height, stroke: "#4dabf7", "stroke-width": "1", opacity: "0.75" });
        el("text", { x: x0 + 3, y: 14, fill: "#4dabf7", "font-size": "9", "font-family": "IBM Plex Mono, monospace" }).textContent = n;
      });
      if (isSel) selectedAnchor = P1;
      return;
    }

    if (d.type === "daterange") {
      if (!ok2) return;
      const yMid = (P1.y + P2.y) / 2;
      el("line", { x1: P1.x, x2: P2.x, y1: yMid, y2: yMid, stroke: "#4dabf7", "stroke-width": "1.6" });
      const bars = Math.round(Math.abs((d.p2.time - d.p1.time)) / 60);
      el("text", { x: (P1.x + P2.x) / 2, y: yMid - 8, "text-anchor": "middle", fill: "#4dabf7", "font-size": "10", "font-family": "IBM Plex Mono, monospace" }).textContent = `${bars} bars`;
      if (isSel) selectedAnchor = { x: (P1.x + P2.x) / 2, y: yMid };
      return;
    }

    if (d.type === "fibfan") {
      if (!ok2) return;
      [0.236, 0.382, 0.5, 0.618, 0.786].forEach((lvl) => {
        const price = d.p1.price + (d.p2.price - d.p1.price) * lvl;
        const py = priceY(price);
        if (!okXY(py)) return;
        const ext = extendRay(P1, { x: P2.x, y: py }, width);
        el("line", { x1: P1.x, y1: P1.y, x2: ext.x, y2: ext.y, stroke: "#f5a623", "stroke-width": "1.2", opacity: "0.85" });
      });
      el("circle", { cx: P1.x, cy: P1.y, r: "3", fill: "#f5a623" });
      if (isSel) selectedAnchor = P1;
      return;
    }

    if (d.type === "fibcircles" || d.type === "fibarcs") {
      if (!ok2) return;
      const r0 = Math.hypot(P2.x - P1.x, P2.y - P1.y);
      [0.382, 0.618, 1, 1.618, 2.618].forEach((lvl) => {
        const r = r0 * lvl;
        if (d.type === "fibcircles") {
          el("circle", { cx: P1.x, cy: P1.y, r, fill: "none", stroke: "#36e0a0", "stroke-width": "1.1", opacity: "0.85" });
        } else {
          const path = describeArc(P1.x, P1.y, r, 180, 360);
          el("path", { d: path, fill: "none", stroke: "#36e0a0", "stroke-width": "1.1", opacity: "0.85" });
        }
      });
      if (isSel) selectedAnchor = P1;
      return;
    }

    if (d.type === "fibspiral") {
      if (!ok2) return;
      const pts = fibSpiralPoints(P1, P2);
      el("polyline", { points: pts.map((p) => `${p.x},${p.y}`).join(" "), fill: "none", stroke: "#c084fc", "stroke-width": isSel ? "2.4" : "1.4" });
      if (isSel) selectedAnchor = P1;
      return;
    }

    if (d.type === "fibwedge") {
      if (!ok3) return;
      el("line", { x1: P1.x, y1: P1.y, x2: P2.x, y2: P2.y, stroke: "#f5a623", "stroke-width": "1.6" });
      el("line", { x1: P1.x, y1: P1.y, x2: P3.x, y2: P3.y, stroke: "#f5a623", "stroke-width": "1.6" });
      [0.382, 0.618, 1].forEach((lvl) => {
        const a = { x: P1.x + (P2.x - P1.x) * lvl, y: P1.y + (P2.y - P1.y) * lvl };
        const b = { x: P1.x + (P3.x - P1.x) * lvl, y: P1.y + (P3.y - P1.y) * lvl };
        el("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, stroke: "#f5a623", "stroke-width": "1", opacity: "0.7" });
      });
      if (isSel) selectedAnchor = P1;
      return;
    }

    if (d.type === "pitchfork") {
      if (!ok3) return;
      const mid = { x: (P2.x + P3.x) / 2, y: (P2.y + P3.y) / 2 };
      const medExt = extendRay(P1, mid, width);
      const p2Ext = extendRay(P1, P2, width);
      const p3Ext = extendRay(P1, P3, width);
      el("line", { x1: P1.x, y1: P1.y, x2: medExt.x, y2: medExt.y, stroke: "#36e0a0", "stroke-width": isSel ? "2.4" : "1.6" });
      el("line", { x1: P2.x, y1: P2.y, x2: P2.x + (medExt.x - P1.x), y2: P2.y + (medExt.y - P1.y), stroke: "#36e0a0", "stroke-width": "1.2", opacity: "0.8" });
      el("line", { x1: P3.x, y1: P3.y, x2: P3.x + (medExt.x - P1.x), y2: P3.y + (medExt.y - P1.y), stroke: "#36e0a0", "stroke-width": "1.2", opacity: "0.8" });
      if (isSel) selectedAnchor = P1;
      return;
    }

    if (d.type === "gannbox") {
      if (!ok2) return;
      const x = Math.min(P1.x, P2.x), y = Math.min(P1.y, P2.y);
      const w = Math.abs(P2.x - P1.x), h = Math.abs(P2.y - P1.y);
      el("rect", { x, y, width: w, height: h, fill: "none", stroke: "#f5a623", "stroke-width": isSel ? "2.2" : "1.4" });
      for (let i = 1; i < 8; i++) {
        el("line", { x1: x, x2: x + w, y1: y + (h * i) / 8, y2: y + (h * i) / 8, stroke: "#f5a623", "stroke-width": "0.7", opacity: "0.55" });
        el("line", { x1: x + (w * i) / 8, x2: x + (w * i) / 8, y1: y, y2: y + h, stroke: "#f5a623", "stroke-width": "0.7", opacity: "0.55" });
      }
      el("line", { x1: x, y1: y, x2: x + w, y2: y + h, stroke: "#f5a623", "stroke-width": "0.9", opacity: "0.8" });
      if (isSel) selectedAnchor = { x: x + w, y };
      return;
    }

    if (d.type === "longpos" || d.type === "shortpos") {
      if (!ok2) return;
      const isLong = d.type === "longpos";
      const x = Math.min(P1.x, P2.x), w = Math.abs(P2.x - P1.x);
      const entryY = P1.y, targetY = P2.y;
      const mirrorY = entryY - (targetY - entryY);
      const profitTop = isLong ? Math.min(targetY, entryY) : Math.min(entryY, mirrorY);
      const lossTop = isLong ? Math.min(entryY, mirrorY) : Math.min(targetY, entryY);
      el("rect", { x, y: Math.min(targetY, entryY), width: w, height: Math.abs(entryY - targetY), fill: isLong ? "rgba(54,224,160,0.22)" : "rgba(255,82,107,0.22)", stroke: isLong ? "#36e0a0" : "#ff526b", "stroke-width": "1" });
      el("rect", { x, y: Math.min(entryY, mirrorY), width: w, height: Math.abs(entryY - mirrorY), fill: isLong ? "rgba(255,82,107,0.18)" : "rgba(54,224,160,0.18)", stroke: isLong ? "#ff526b" : "#36e0a0", "stroke-width": "1" });
      el("line", { x1: x, x2: x + w, y1: entryY, y2: entryY, stroke: "#e6ecf5", "stroke-width": "1.4" });
      const pct = Math.abs(((d.p2.price - d.p1.price) / d.p1.price) * 100).toFixed(2);
      el("text", { x: x + w / 2, y: Math.min(targetY, entryY) - 4, "text-anchor": "middle", fill: isLong ? "#36e0a0" : "#ff526b", "font-size": "10", "font-family": "IBM Plex Mono, monospace", "font-weight": "700" }).textContent = `1:1 · ${pct}%`;
      if (isSel) selectedAnchor = { x: x + w, y: entryY };
      return;
    }
  });

  if (previewPoint && pendingPoints.length) {
    const chain = [...pendingPoints, previewPoint];
    for (let i = 0; i < chain.length - 1; i++) {
      const a = { x: timeX(chain[i].time), y: priceY(chain[i].price) };
      const b = { x: timeX(chain[i + 1].time), y: priceY(chain[i + 1].price) };
      if (okXY(a.x, a.y, b.x, b.y)) el("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, stroke: "#8aa0b8", "stroke-width": "1.2", "stroke-dasharray": "3 3" });
    }
  } else if (activeChartTool === "path" && pendingPoints.length) {
    const pts = pendingPoints.map((p) => ({ x: timeX(p.time), y: priceY(p.price) })).filter((p) => okXY(p.x, p.y));
    if (pts.length) el("polyline", { points: pts.map((p) => `${p.x},${p.y}`).join(" "), fill: "none", stroke: "#4dabf7", "stroke-width": "1.6", "stroke-dasharray": "3 3" });
  }

  if (liveBrushPoints && liveBrushPoints.length > 1) {
    const pts = liveBrushPoints.map((p) => ({ x: timeX(p.time), y: priceY(p.price) })).filter((p) => okXY(p.x, p.y));
    if (pts.length > 1) el("polyline", { points: pts.map((p) => `${p.x},${p.y}`).join(" "), fill: "none", stroke: "#c084fc", "stroke-width": "2", "stroke-linejoin": "round", "stroke-linecap": "round" });
  }

  if (selectedDrawingId != null && selectedAnchor) {
    positionDrawDeleteBtn(selectedAnchor.x, selectedAnchor.y);
  } else {
    hideDrawDeleteBtn();
  }

  renderIndicatorOverlay();
}

function describeArc(cx, cy, r, startDeg, endDeg) {
  const toRad = (d) => (d * Math.PI) / 180;
  const start = { x: cx + r * Math.cos(toRad(startDeg)), y: cy + r * Math.sin(toRad(startDeg)) };
  const end = { x: cx + r * Math.cos(toRad(endDeg)), y: cy + r * Math.sin(toRad(endDeg)) };
  const largeArc = endDeg - startDeg <= 180 ? 0 : 1;
  return `M ${start.x} ${start.y} A ${r} ${r} 0 ${largeArc} 1 ${end.x} ${end.y}`;
}

if (chartTfRow) {
  chartTfRow.querySelectorAll(".chart-tf-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      chartTfRow.querySelectorAll(".chart-tf-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      chartTfRow.querySelectorAll(".chart-range-btn").forEach((b) => b.classList.remove("active"));
      currentChartTimeframe = btn.dataset.tf;
      chartLimit = 300;
      loadChartData();
    });
  });

  chartTfRow.querySelectorAll(".chart-range-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const preset = CHART_RANGE_PRESETS[btn.dataset.range];
      if (!preset) return;
      chartTfRow.querySelectorAll(".chart-range-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      chartTfRow.querySelectorAll(".chart-tf-btn").forEach((b) => {
        b.classList.toggle("active", b.dataset.tf === preset.tf);
      });
      currentChartTimeframe = preset.tf;
      chartLimit = preset.limit;
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
  syncChartCoinIcon();
  if (chartStatusEl) chartStatusEl.textContent = "loading candles…";

  try {
    const res = await fetch(`/candles?coin=${encodeURIComponent(coin)}&timeframe=${currentChartTimeframe}&limit=${chartLimit}`);
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "candle fetch failed");

    lastBars = (data.candles || []).map((c) => ({
      time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume || 0,
    }));
    oldestLoadedTime = lastBars.length ? lastBars[0].time : null;
    noMoreHistoryKey = null;
    lwCandleSeries.setData(lastBars);
    lwChart.timeScale().fitContent();
    refreshIndicators();

    // drawings are per-coin — switching pairs clears the board, but stay
    // put when only the timeframe changes for the same coin.
    if (drawingsCoinKey !== coin) {
      chartDrawings = [];
      pendingPoints = [];
      selectedDrawingId = null;
      hideDrawDeleteBtn();
      drawingsCoinKey = coin;
    }
    renderDrawings();

    updateChartPrice(data);
    if (chartStatusEl) {
      chartStatusEl.textContent = `live · ${coin} · ${currentChartTimeframe.toUpperCase()} · updates every 5s`;
    }

    restartChartPolling(coin);
  } catch (err) {
    if (chartStatusEl) chartStatusEl.textContent = "⚠ " + err.message;
  }
}

// Infinite scroll-back: fires whenever the visible range changes. When the
// user pans/zooms close to the oldest bar currently loaded, page further
// into the past instead of leaving them stuck at a hard wall.
async function maybeLoadOlderCandles() {
  if (!lwChart || !lwCandleSeries || isLoadingOlderCandles) return;
  if (!lastBars.length || oldestLoadedTime == null) return;

  const range = lwChart.timeScale().getVisibleLogicalRange();
  if (!range || range.from > 20) return;

  const coin = coinSelect.value;
  const key = `${coin}|${currentChartTimeframe}`;
  if (noMoreHistoryKey === key) return;

  isLoadingOlderCandles = true;
  try {
    const res = await fetch(`/candles?coin=${encodeURIComponent(coin)}&timeframe=${currentChartTimeframe}&limit=${chartLimit}&before=${oldestLoadedTime}`);
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "older candle fetch failed");

    const older = (data.candles || []).map((c) => ({
      time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume || 0,
    }));

    if (!older.length) {
      noMoreHistoryKey = key;
      return;
    }

    const addedCount = older.length;
    lastBars = [...older, ...lastBars];
    oldestLoadedTime = lastBars[0].time;

    const savedRange = lwChart.timeScale().getVisibleLogicalRange();
    lwCandleSeries.setData(lastBars);
    if (savedRange) {
      lwChart.timeScale().setVisibleLogicalRange({ from: savedRange.from + addedCount, to: savedRange.to + addedCount });
    }
    renderDrawings();
  } catch (err) {
    // silent — a failed older-history fetch shouldn't disrupt the live chart
  } finally {
    isLoadingOlderCandles = false;
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
      const lastBar = {
        time: last.time, open: last.open, high: last.high, low: last.low,
        close: last.close, volume: last.volume || 0,
      };
      lwCandleSeries.update(lastBar);
      if (lastBars.length) {
        const prevLast = lastBars[lastBars.length - 1];
        if (prevLast.time === lastBar.time) lastBars[lastBars.length - 1] = lastBar;
        else if (lastBar.time > prevLast.time) lastBars.push(lastBar);
      }
      refreshIndicators();
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
  renderLiquidityScanner(data);

  if (data.disclaimer) {
    document.getElementById("disclaimer-text").textContent = "⚠ " + data.disclaimer;
  }

  // Keep the live chart's coin/title in sync even if the user hasn't opened
  // that tab yet — it'll be correct the moment they click it.
  if (chartTitleEl) chartTitleEl.textContent = `${data.coin || coinSelect.value} · ${currentChartTimeframe.toUpperCase()}`;
  syncChartCoinIcon();

  // Active Trade Tracking: agar is coin par pehle se ek active trade nahi
  // hai, "Track This Trade" button dikhate hain. Agar hai, to yeh naya
  // signal sirf "CURRENT MARKET ANALYSIS" ke tor par dikhta hai, active
  // trade ko replace nahi karta.
  if (window.SignalXTrades) {
    window.SignalXTrades.onSignalRendered(data, data.coin || coinSelect.value);
  }
}

/* ---------------------------- fetch flow ---------------------------- */

async function runAnalysis(forceAnalyze) {
  const coin = coinSelect.value;

  // Active Trade Tracking: agar is coin par pehle se ek ACTIVE trade
  // tracked hai, aur user ne explicitly "Analyze Market Anyway" nahi
  // dabaya, to naya signal fetch karne ke bajaye ek prompt dikhate hain -
  // taake user confuse na ho ke uski active trade replace ho gayi.
  if (!forceAnalyze && window.SignalXTrades) {
    const existing = window.SignalXTrades.getActiveTradeForCoin(coin);
    if (existing) {
      window.SignalXTrades.showAlreadyActivePrompt(coin, existing);
      return;
    }
  }

  errorText.classList.add("hidden");
  errorText.textContent = "";
  runBtn.disabled = true;
  runBtn.querySelector(".scan-btn-text").textContent = "SCANNING…";

  if (coin !== currentCoin) lastLiquidityExtra = null;
  currentCoin = coin;

  try {
    const res = await fetch(`/signal?coin=${encodeURIComponent(coin)}&timeframe=1h`);
    const data = await res.json();

    if (!res.ok || data.error) {
      throw new Error(data.error || "Signal fetch failed");
    }

    renderResult(data);
    resultBox.classList.remove("hidden");
    emptyState.classList.add("hidden");

    fetchAndRenderLiquidity(coin);
    startLiquidityPolling();
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

/* ==========================================================================
   INDICATORS ENGINE — 20 professional indicators
   --------------------------------------------------------------------------
   The INDICATORS button lives in the chart header and is visible in BOTH the
   normal layout and the fullscreen chart. Pressing it opens a searchable
   panel anchored INSIDE the chart panel. Each row toggles one indicator.

   Rendering model (lightweight-charts v5):
   - "overlay" indicators (EMA/SMA/VWAP/Supertrend/Bollinger lines)
     -> lightweight-charts series on the main pane (price-synced).
   - "pane" oscillators (RSI/MACD/ATR/ADX/OBV/MFI/CVD)
     -> lightweight-charts series on their own pane, auto-created when
        enabled and auto-removed when emptied.
   - "svg" structural indicators (FVG / Order Blocks / S&R / Liquidity /
     Market Structure / Volume Profile / Pivot Points / Ichimoku) plus the
     Bollinger band fill -> drawn into #indicator-overlay, an SVG that sits
     under the drawing overlay, so everything pans/zooms/resizes with the
     candles.
   ========================================================================== */

// ------------------------------ state -----------------------------------

let lastBars = [];            // OHLCV series currently rendered on the chart
let oldestLoadedTime = null;  // unix seconds of the earliest bar in lastBars
let isLoadingOlderCandles = false;
let noMoreHistoryKey = null;  // `${coin}|${timeframe}` once the exchange has no earlier data
let activeIndicators = {};    // key -> true (toggled ON)
let indicatorSeries = {};     // key -> [ISeriesApi] (series-type indicators)
const IND_PALETTE = {
  up: "#36e0a0", down: "#ff526b", gold: "#e2b93b", blue: "#4dabf7",
  purple: "#c084fc", cyan: "#2dd4bf", orange: "#f5a623", pink: "#f472b6",
  gray: "#7d8ea3", red: "#ff6b6b", green: "#2ecc71",
};

// --------------------------- math helpers -------------------------------

function smaVals(values, period) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (na(v)) continue;
    sum += v;
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function emaVals(values, period) {
  const out = new Array(values.length).fill(null);
  const k = 2 / (period + 1);
  let prev = null;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (na(v)) continue;
    prev = prev === null ? v : v * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

function rsiVals(closes, period = 14) {
  const out = new Array(closes.length).fill(null);
  let avgGain = 0, avgLoss = 0;
  for (let i = 1; i < closes.length; i++) {
    const change = closes[i] - closes[i - 1];
    const gain = Math.max(change, 0), loss = Math.max(-change, 0);
    if (i <= period) {
      avgGain += gain / period;
      avgLoss += loss / period;
    } else {
      avgGain = (avgGain * (period - 1) + gain) / period;
      avgLoss = (avgLoss * (period - 1) + loss) / period;
    }
    if (i >= period) {
      out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
    }
  }
  return out;
}

function atrVals(bars, period = 14) {
  const out = new Array(bars.length).fill(null);
  let prevClose = null, atr = null;
  for (let i = 0; i < bars.length; i++) {
    const h = bars[i].high, l = bars[i].low, c = bars[i].close;
    if (prevClose === null) { prevClose = c; continue; }
    const tr = Math.max(h - l, Math.abs(h - prevClose), Math.abs(l - prevClose));
    atr = atr === null ? tr : (atr * (period - 1) + tr) / period;
    out[i] = atr;
    prevClose = c;
  }
  return out;
}

function macdVals(closes, fast = 12, slow = 26, signal = 9) {
  const ef = emaVals(closes, fast);
  const es = emaVals(closes, slow);
  const macd = closes.map((_, i) => (ef[i] !== null && es[i] !== null) ? ef[i] - es[i] : null);
  const sig = emaVals(macd, signal);
  const hist = macd.map((m, i) => (m !== null && sig[i] !== null) ? m - sig[i] : null);
  return { macd, signal: sig, histogram: hist };
}

function adxVals(bars, period = 14) {
  const n = bars.length;
  const plusDI = new Array(n).fill(null);
  const minusDI = new Array(n).fill(null);
  const adx = new Array(n).fill(null);
  let pDM = 0, mDM = 0, tr = 0;
  for (let i = 1; i < n; i++) {
    const up = bars[i].high - bars[i - 1].high;
    const down = bars[i - 1].low - bars[i].low;
    const pdm = (up > down && up > 0) ? up : 0;
    const mdm = (down > up && down > 0) ? down : 0;
    const trv = Math.max(bars[i].high - bars[i].low, Math.abs(bars[i].high - bars[i - 1].close), Math.abs(bars[i].low - bars[i - 1].close));
    if (i <= period) {
      pDM += pdm; mDM += mdm; tr += trv;
      if (i !== period) continue;
    } else {
      pDM = pDM - pDM / period + pdm;
      mDM = mDM - mDM / period + mdm;
      tr = tr - tr / period + trv;
    }
    const pdi = tr ? 100 * pDM / tr : 0;
    const mdi = tr ? 100 * mDM / tr : 0;
    const dx = (pdi + mdi) ? 100 * Math.abs(pdi - mdi) / (pdi + mdi) : 0;
    plusDI[i] = pdi;
    minusDI[i] = mdi;
    adx[i] = (i === period) ? dx : ((adx[i - 1] !== null) ? (adx[i - 1] * (period - 1) + dx) / period : dx);
  }
  return { adx, plusDI, minusDI };
}

function bollingerVals(closes, period = 20, mult = 2) {
  const mid = smaVals(closes, period);
  const upper = new Array(closes.length).fill(null);
  const lower = new Array(closes.length).fill(null);
  for (let i = 0; i < closes.length; i++) {
    if (mid[i] === null) continue;
    let s = 0;
    for (let j = i - period + 1; j <= i; j++) s += (closes[j] - mid[i]) * (closes[j] - mid[i]);
    const std = Math.sqrt(s / period);
    upper[i] = mid[i] + mult * std;
    lower[i] = mid[i] - mult * std;
  }
  return { upper, mid, lower };
}

function vwapVals(bars) {
  const out = new Array(bars.length).fill(null);
  let cumPV = 0, cumV = 0;
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i];
    if (b.volume > 0) {
      const tp = (b.high + b.low + b.close) / 3;
      cumPV += tp * b.volume;
      cumV += b.volume;
      out[i] = cumV > 0 ? cumPV / cumV : null;
    }
  }
  return out;
}

function supertrendVals(bars, period = 10, mult = 3) {
  const atr = atrVals(bars, period);
  const n = bars.length;
  const values = new Array(n).fill(null);
  const colors = new Array(n).fill(1);
  let prevFU = null, prevFL = null;
  for (let i = 0; i < n; i++) {
    const a = atr[i];
    if (na(a)) { continue; }
    const mid = (bars[i].high + bars[i].low) / 2;
    const bu = mid + mult * a;
    const bl = mid - mult * a;
    let fu, fl;
    if (prevFU === null) { fu = bu; fl = bl; }
    else {
      fu = (bu < prevFU || bars[i - 1].close > prevFU) ? bu : prevFU;
      fl = (bl > prevFL || bars[i - 1].close < prevFL) ? bl : prevFL;
    }
    let st, dir;
    if (prevFU === null) { st = fl; dir = 1; }
    else if (colors[i - 1] === 1) {
      st = (bars[i].close < fl) ? fu : fl;
      dir = (bars[i].close < fl) ? -1 : 1;
    } else {
      st = (bars[i].close > fu) ? fl : fu;
      dir = (bars[i].close > fu) ? 1 : -1;
    }
    values[i] = st;
    colors[i] = dir;
    prevFU = fu; prevFL = fl;
  }
  return { values, colors };
}

function obvVals(bars) {
  const out = new Array(bars.length).fill(0);
  for (let i = 1; i < bars.length; i++) {
    const b = bars[i], p = bars[i - 1];
    if (b.close > p.close) out[i] = out[i - 1] + b.volume;
    else if (b.close < p.close) out[i] = out[i - 1] - b.volume;
    else out[i] = out[i - 1];
  }
  return out;
}

function mfiVals(bars, period = 14) {
  const n = bars.length;
  const out = new Array(n).fill(null);
  const tps = bars.map((b) => (b.high + b.low + b.close) / 3);
  let posFlow = 0, negFlow = 0;
  for (let i = 1; i < n; i++) {
    const diff = tps[i] - tps[i - 1];
    const flow = tps[i] * (bars[i].volume || 0);
    if (i <= period) {
      if (diff > 0) posFlow += flow;
      else if (diff < 0) negFlow += flow;
      if (i !== period) continue;
    } else {
      const posToday = diff > 0 ? flow : 0;
      const negToday = diff < 0 ? flow : 0;
      posFlow = posFlow - posFlow / period + posToday;
      negFlow = negFlow - negFlow / period + negToday;
    }
    out[i] = negFlow === 0 ? 100 : 100 - 100 / (1 + posFlow / negFlow);
  }
  return out;
}

function cvdVals(bars) {
  const out = new Array(bars.length).fill(0);
  const deltas = new Array(bars.length).fill(0);
  for (let i = 0; i < bars.length; i++) {
    const delta = bars[i].close >= bars[i].open ? (bars[i].volume || 0) : -(bars[i].volume || 0);
    deltas[i] = delta;
    out[i] = (i === 0 ? 0 : out[i - 1]) + delta;
  }
  return { values: out, deltas };
}

function toPoints(bars, vals, colorFn) {
  const out = [];
  for (let i = 0; i < bars.length; i++) {
    const v = vals[i];
    if (na(v)) continue;
    const pt = { time: bars[i].time, value: v };
    if (colorFn) { const c = colorFn(i, v, bars[i]); if (c) pt.color = c; }
    out.push(pt);
  }
  return out;
}

function rollingMid(highs, lows, period) {
  const n = highs.length;
  const out = new Array(n).fill(null);
  for (let i = period - 1; i < n; i++) {
    let h = -Infinity, l = Infinity;
    for (let j = i - period + 1; j <= i; j++) {
      if (highs[j] > h) h = highs[j];
      if (lows[j] < l) l = lows[j];
    }
    out[i] = (h + l) / 2;
  }
  return out;
}

// ------------------------------ registry ---------------------------------

const INDICATOR_LIB = [
  // --- Trend
  {
    key: "ema", name: "EMA", group: "Trend", desc: "Exponential Moving Average · 20 / 50 / 200",
    color: IND_PALETTE.gold, type: "overlay",
    compute(bars) {
      const closes = bars.map((b) => b.close);
      const colors = ["#e2b93b", "#4dabf7", "#c084fc"];
      return {
        series: [20, 50, 200].map((p, i) => ({
          kind: "line",
          options: { color: colors[i], lineWidth: i === 2 ? 2 : 1, priceLineVisible: false, lineStyle: i === 2 ? 2 : 0, crosshairMarkerVisible: false },
          data: toPoints(bars, emaVals(closes, p)),
        })),
      };
    },
  },
  {
    key: "sma", name: "SMA", group: "Trend", desc: "Simple Moving Average · 200",
    color: IND_PALETTE.orange, type: "overlay",
    compute(bars) {
      return {
        series: [{
          kind: "line",
          options: { color: "#f5a623", lineWidth: 2, priceLineVisible: false, lineStyle: 2, crosshairMarkerVisible: false },
          data: toPoints(bars, smaVals(bars.map((b) => b.close), 200)),
        }],
      };
    },
  },
  {
    key: "vwap", name: "VWAP", group: "Trend", desc: "Volume Weighted Average Price",
    color: IND_PALETTE.cyan, type: "overlay",
    compute(bars) {
      return {
        series: [{
          kind: "line",
          options: { color: "#2dd4bf", lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false },
          data: toPoints(bars, vwapVals(bars)),
        }],
      };
    },
  },
  {
    key: "supertrend", name: "Supertrend", group: "Trend", desc: "ATR trend follow · 10 / 3",
    color: IND_PALETTE.up, type: "overlay",
    compute(bars) {
      const st = supertrendVals(bars, 10, 3);
      const data = [];
      for (let i = 0; i < bars.length; i++) {
        if (st.values[i] === null) continue;
        data.push({ time: bars[i].time, value: st.values[i], color: st.colors[i] >= 1 ? "#36e0a0" : "#ff526b" });
      }
      return {
        series: [{
          kind: "line",
          options: { color: "#36e0a0", lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false },
          data,
        }],
      };
    },
  },
  {
    key: "bollinger", name: "Bollinger Bands", group: "Trend", desc: "20 · 2σ volatility bands",
    color: IND_PALETTE.purple, type: "overlay",
    compute(bars) {
      const closes = bars.map((b) => b.close);
      const bb = bollingerVals(closes, 20, 2);
      return {
        series: [
          { kind: "line", options: { color: "#c084fc", lineWidth: 1.2, priceLineVisible: false, lineStyle: 2, crosshairMarkerVisible: false }, data: toPoints(bars, bb.upper) },
          { kind: "line", options: { color: "#e2b93b", lineWidth: 1.4, priceLineVisible: false, crosshairMarkerVisible: false }, data: toPoints(bars, bb.mid) },
          { kind: "line", options: { color: "#c084fc", lineWidth: 1.2, priceLineVisible: false, lineStyle: 2, crosshairMarkerVisible: false }, data: toPoints(bars, bb.lower) },
        ],
      };
    },
    svg(bars, ctx) {
      const closes = bars.map((b) => b.close);
      const bb = bollingerVals(closes, 20, 2);
      ctx.fillBand(bb.upper, bb.lower, "rgba(192, 132, 252, 0.09)");
    },
  },
  {
    key: "ichimoku", name: "Ichimoku Cloud", group: "Trend", desc: "Tenkan · Kijun · Cloud · Chikou",
    color: IND_PALETTE.blue, type: "svg",
    svg(bars, ctx) {
      const n = bars.length;
      const highs = bars.map((b) => b.high), lows = bars.map((b) => b.low), closes = bars.map((b) => b.close);
      const tenkan = rollingMid(highs, lows, 9);
      const kijun = rollingMid(highs, lows, 26);
      const senkouB = rollingMid(highs, lows, 52);
      const senkouA = new Array(n).fill(null);
      for (let i = 0; i < n; i++) {
        if (tenkan[i] !== null && kijun[i] !== null) senkouA[i] = (tenkan[i] + kijun[i]) / 2;
      }
      const aShift = new Array(n).fill(null), bShift = new Array(n).fill(null);
      for (let i = 0; i + 26 < n; i++) { aShift[i + 26] = senkouA[i]; bShift[i + 26] = senkouB[i]; }
      ctx.fillBand(aShift, bShift, "rgba(77, 171, 247, 0.10)");
      ctx.polyline(tenkan, "#4dabf7", 1.1);
      ctx.polyline(kijun, "#ff526b", 1.1);
      const chikou = new Array(n).fill(null);
      for (let i = 26; i < n; i++) chikou[i] = closes[i - 26];
      ctx.polyline(chikou, "#7d8ea3", 1.1);
    },
  },
  {
    key: "pivot", name: "Pivot Points", group: "Trend", desc: "Classic daily pivot · R1–R3 / S1–S3",
    color: IND_PALETTE.orange, type: "svg",
    svg(bars, ctx) {
      if (!bars.length) return;
      const H = Math.max(...bars.map((b) => b.high));
      const L = Math.min(...bars.map((b) => b.low));
      const C = bars[bars.length - 1].close;
      const P = (H + L + C) / 3;
      const levels = [
        { p: H + 2 * (P - L), label: "R3", color: "#ff6b6b" },
        { p: P + (H - L), label: "R2", color: "#ff6b6b" },
        { p: 2 * P - L, label: "R1", color: "#ff6b6b" },
        { p, label: "P", color: "#e2b93b" },
        { p: 2 * P - H, label: "S1", color: "#2ecc71" },
        { p: P - (H - L), label: "S2", color: "#2ecc71" },
        { p: L - 2 * (H - P), label: "S3", color: "#2ecc71" },
      ];
      levels.forEach((lv) => ctx.hline(lv.p, lv.color, lv.label, lv.p === P));
    },
  },
  // --- Momentum
  {
    key: "rsi", name: "RSI", group: "Momentum", desc: "Relative Strength Index · 14",
    color: IND_PALETTE.purple, type: "pane",
    compute(bars) {
      const series = [{
        kind: "line",
        options: { color: "#c084fc", lineWidth: 1.8, priceLineVisible: false, crosshairMarkerVisible: false },
        data: toPoints(bars, rsiVals(bars.map((b) => b.close), 14)),
        refs: [{ price: 70, color: "rgba(255,82,107,0.55)", lineStyle: 2 }, { price: 30, color: "rgba(54,224,160,0.55)", lineStyle: 2 }, { price: 50, color: "rgba(255,255,255,0.14)", lineStyle: 3 }],
      }];
      return { series, paneHeight: 110 };
    },
  },
  {
    key: "macd", name: "MACD", group: "Momentum", desc: "12 · 26 · 9",
    color: IND_PALETTE.cyan, type: "pane",
    compute(bars) {
      const closes = bars.map((b) => b.close);
      const m = macdVals(closes);
      const hist = [];
      for (let i = 0; i < bars.length; i++) {
        if (m.histogram[i] === null) continue;
        hist.push({ time: bars[i].time, value: m.histogram[i], color: m.histogram[i] >= 0 ? "rgba(54,224,160,0.85)" : "rgba(255,82,107,0.85)" });
      }
      return {
        series: [
          { kind: "histogram", options: { base: 0, priceLineVisible: false, lastValueVisible: false }, data: hist },
          { kind: "line", options: { color: "#4dabf7", lineWidth: 1.4, priceLineVisible: false, crosshairMarkerVisible: false }, data: toPoints(bars, m.macd) },
          { kind: "line", options: { color: "#f5a623", lineWidth: 1.4, priceLineVisible: false, crosshairMarkerVisible: false }, data: toPoints(bars, m.signal) },
        ],
        paneHeight: 140,
      };
    },
  },
  {
    key: "adx", name: "ADX", group: "Momentum", desc: "Average Directional Index · 14",
    color: IND_PALETTE.orange, type: "pane",
    compute(bars) {
      const d = adxVals(bars, 14);
      return {
        series: [
          { kind: "line", options: { color: "#f5a623", lineWidth: 1.8, priceLineVisible: false, crosshairMarkerVisible: false }, data: toPoints(bars, d.adx) },
          { kind: "line", options: { color: "#36e0a0", lineWidth: 1, priceLineVisible: false, crosshairMarkerVisible: false }, data: toPoints(bars, d.plusDI) },
          { kind: "line", options: { color: "#ff526b", lineWidth: 1, priceLineVisible: false, crosshairMarkerVisible: false }, data: toPoints(bars, d.minusDI) },
        ],
        paneHeight: 110,
      };
    },
  },
  {
    key: "mfi", name: "MFI", group: "Momentum", desc: "Money Flow Index · 14",
    color: IND_PALETTE.gold, type: "pane",
    compute(bars) {
      return {
        series: [{
          kind: "line",
          options: { color: "#e2b93b", lineWidth: 1.8, priceLineVisible: false, crosshairMarkerVisible: false },
          data: toPoints(bars, mfiVals(bars, 14)),
          refs: [{ price: 80, color: "rgba(255,82,107,0.55)", lineStyle: 2 }, { price: 20, color: "rgba(54,224,160,0.55)", lineStyle: 2 }],
        }],
        paneHeight: 110,
      };
    },
  },
  // --- Volatility
  {
    key: "atr", name: "ATR", group: "Volatility", desc: "Average True Range · 14",
    color: IND_PALETTE.blue, type: "pane",
    compute(bars) {
      return {
        series: [{
          kind: "line",
          options: { color: "#4dabf7", lineWidth: 1.8, priceLineVisible: false, crosshairMarkerVisible: false },
          data: toPoints(bars, atrVals(bars, 14)),
        }],
        paneHeight: 110,
      };
    },
  },
  // --- Volume
  {
    key: "obv", name: "OBV", group: "Volume", desc: "On-Balance Volume",
    color: IND_PALETTE.purple, type: "pane",
    compute(bars) {
      return {
        series: [{
          kind: "line",
          options: { color: "#c084fc", lineWidth: 1.8, priceLineVisible: false, crosshairMarkerVisible: false },
          data: toPoints(bars, obvVals(bars)),
        }],
        paneHeight: 110,
      };
    },
  },
  {
    key: "cvd", name: "Volume Delta", group: "Volume", desc: "Cumulative Volume Delta / CVD",
    color: IND_PALETTE.gold, type: "pane",
    compute(bars) {
      const { values, deltas } = cvdVals(bars);
      const data = [];
      for (let i = 0; i < bars.length; i++) {
        if (na(values[i])) continue;
        data.push({ time: bars[i].time, value: values[i], color: deltas[i] >= 0 ? "rgba(54,224,160,0.85)" : "rgba(255,82,107,0.85)" });
      }
      return {
        series: [{
          kind: "histogram",
          options: { base: 0, priceLineVisible: false, lastValueVisible: false },
          data,
        }],
        paneHeight: 120,
      };
    },
  },
  {
    key: "volprofile", name: "Volume Profile", group: "Volume", desc: "Price-level volume distribution",
    color: IND_PALETTE.blue, type: "svg",
    svg(bars, ctx) {
      const N = 26;
      const vols = bars.map((b) => b.volume || 0);
      let hi = -Infinity, lo = Infinity;
      for (let i = Math.max(0, bars.length - 120); i < bars.length; i++) {
        if (bars[i].high > hi) hi = bars[i].high;
        if (bars[i].low < lo) lo = bars[i].low;
      }
      if (hi === -Infinity || lo === Infinity) return;
      const bucket = new Array(N).fill(0);
      for (let i = Math.max(0, bars.length - 120); i < bars.length; i++) {
        const idx = Math.min(N - 1, Math.max(0, Math.floor(((bars[i].high + bars[i].low) / 2 - lo) / ((hi - lo) / N))));
        bucket[idx] += vols[i];
      }
      const maxV = Math.max(...bucket, 1);
      const bw = (hi - lo) / N;
      for (let i = 0; i < N; i++) {
        if (bucket[i] <= 0) continue;
        const pMid = lo + bw * (i + 0.5);
        const w = Math.max(6, (bucket[i] / maxV) * ctx.width * 0.22);
        ctx.hBar(pMid, bw / 2, w, "rgba(77, 171, 247, 0.45)", i === 0 || i === N - 1 ? "#2dd4bf" : null);
      }
    },
  },
  // --- Structure
  {
    key: "sr", name: "Support & Resistance", group: "Structure", desc: "Swing-based key levels",
    color: IND_PALETTE.orange, type: "svg",
    svg(bars, ctx) {
      const { highs, lows } = detectSwings(bars, 2);
      const levels = clusterLevels(bars, highs.map((i) => bars[i].high), lows.map((i) => bars[i].low));
      levels.forEach((lv) => {
        ctx.hline(lv.price, lv.support ? "rgba(54,224,160,0.55)" : "rgba(255,82,107,0.55)", lv.support ? `SUP ${lv.n}` : `RES ${lv.n}`, false, true);
      });
    },
  },
  {
    key: "fvg", name: "Fair Value Gap", group: "Structure", desc: "Three-candle inefficiency zones",
    color: IND_PALETTE.cyan, type: "svg",
    svg(bars, ctx) {
      const n = bars.length;
      for (let i = 0; i < n - 2; i++) {
        const b0 = bars[i], b2 = bars[i + 2];
        if (b0.low > b2.high) {
          ctx.box(bars[i].time, bars[i + 2].time, b2.high, b0.low, "rgba(54,224,160,0.12)", "#36e0a0", "FVG");
        } else if (b0.high < b2.low) {
          ctx.box(bars[i].time, bars[i + 2].time, b0.high, b2.low, "rgba(255,82,107,0.12)", "#ff526b", "FVG");
        }
      }
    },
  },
  {
    key: "orderblocks", name: "Order Blocks", group: "Structure", desc: "Institutional supply / demand zones",
    color: IND_PALETTE.pink, type: "svg",
    svg(bars, ctx) {
      const atr = atrVals(bars, 14);
      const n = bars.length;
      const blocks = [];
      for (let i = 1; i < n - 1; i++) {
        const a = atr[i];
        if (na(a)) continue;
        const body = Math.abs(bars[i].close - bars[i].open);
        const nxtBody = Math.abs(bars[i + 1].close - bars[i + 1].open);
        if (body > 0 && nxtBody > 1.4 * body) {
          const bullish = bars[i].close < bars[i].open && bars[i + 1].close > bars[i + 1].open;
          const bearish = bars[i].close > bars[i].open && bars[i + 1].close < bars[i + 1].open;
          if (bullish || bearish) {
            blocks.push({
              time: bars[i].time,
              top: Math.max(bars[i].open, bars[i].close),
              bottom: Math.min(bars[i].open, bars[i].close),
              bull: bullish,
            });
          }
        }
      }
      const recent = blocks.slice(-12);
      recent.forEach((bl) => {
        ctx.box(bl.time, bars[n - 1].time, bl.top, bl.bottom, bl.bull ? "rgba(54,224,160,0.10)" : "rgba(255,82,107,0.10)", bl.bull ? "#36e0a0" : "#ff526b", bl.bull ? "OB" : "OB");
      });
    },
  },
  {
    key: "marketstructure", name: "Market Structure", group: "Structure", desc: "BOS / CHoCH swing analysis",
    color: IND_PALETTE.blue, type: "svg",
    svg(bars, ctx) {
      const pts = zigzag(bars, 2);
      const events = structureEvents(pts);
      ctx.polylinePivots(pts, "rgba(141, 158, 171, 0.65)");
      events.forEach((ev) => {
        const bar = bars[ev.i];
        if (!bar) return;
        const price = ev.type === "BOS" ? bar.high : bar.low;
        ctx.marker(ev.i, price, ev.type, ev.type === "BOS" ? "#4dabf7" : "#f5a623");
      });
    },
  },
  {
    key: "liquidity", name: "Liquidity Radar", group: "Structure", desc: "Equal highs / lows liquidity pools",
    color: IND_PALETTE.gold, type: "svg",
    svg(bars, ctx) {
      const pools = liquidityPools(bars);
      pools.forEach((p) => {
        ctx.hline(p.price, p.eqh ? "rgba(255,82,107,0.5)" : "rgba(54,224,160,0.5)", p.eqh ? "EQH" : "EQL", false, false, p.count);
      });
    },
  },
];

const IND_BY_KEY = {};
INDICATOR_LIB.forEach((d) => { IND_BY_KEY[d.key] = d; });

// --------------------------- series helpers -----------------------------

const LW_SERIES_KIND = {
  line: () => LightweightCharts.LineSeries,
  histogram: () => LightweightCharts.HistogramSeries,
  area: () => LightweightCharts.AreaSeries,
};

function nextPaneIndex() {
  return lwChart ? lwChart.panes().length : 1;
}

function renderIndicator(def) {
  if (!lwChart || typeof LightweightCharts === "undefined") return;
  const computed = def.compute ? def.compute(lastBars) : null;
  const specs = computed ? computed.series || [] : [];

  // drop any stale series for this indicator first (idempotent re-enable)
  (indicatorSeries[def.key] || []).forEach((s) => {
    try { lwChart.removeSeries(s); } catch (e) { /* noop */ }
  });

  // one pane per indicator — every series of this indicator shares it
  const paneIndex = def.type === "pane" ? nextPaneIndex() : 0;
  const created = [];
  specs.forEach((spec, idx) => {
    const kindDef = LW_SERIES_KIND[spec.kind];
    if (!kindDef) return;
    const series = lwChart.addSeries(kindDef(), spec.options || {}, paneIndex);
    series.setData(spec.data || []);
    (spec.refs || []).forEach((ref) => {
      try {
        series.createPriceLine({ price: ref.price, color: ref.color, lineWidth: 1, lineStyle: ref.lineStyle || 0, axisLabelVisible: false });
      } catch (e) { /* noop */ }
    });
    created.push(series);
  });
  if (def.type === "pane" && created.length) {
    try {
      created[0].getPane().setHeight((computed && computed.paneHeight) || 110);
    } catch (e) { /* noop */ }
  }
  indicatorSeries[def.key] = created;
  renderIndicatorOverlay();
}

function clearIndicator(defKey) {
  (indicatorSeries[defKey] || []).forEach((s) => {
    try { lwChart.removeSeries(s); } catch (e) { /* noop */ }
  });
  delete indicatorSeries[defKey];
}

function refreshIndicators() {
  if (!lwChart || typeof LightweightCharts === "undefined") return;
  const keys = Object.keys(activeIndicators);
  if (!keys.length) { renderIndicatorOverlay(); return; }
  keys.forEach((key) => {
    const def = IND_BY_KEY[key];
    if (!def) return;
    if (def.compute) {
      const computed = def.compute(lastBars);
      const specs = computed ? computed.series || [] : [];
      const seriesList = indicatorSeries[key] || [];
      specs.forEach((spec, i) => {
        const s = seriesList[i];
        if (s) {
          try { s.setData(spec.data || []); } catch (e) { /* noop */ }
        }
      });
    }
  });
  renderIndicatorOverlay();
}

// ----------------------- SVG indicator overlay ---------------------------

function renderIndicatorOverlay() {
  if (!indicatorOverlayEl || !lwChart || !lwCandleSeries || !candleChartEl) return;
  const hasSvg = Object.keys(activeIndicators).some((k) => {
    const d = IND_BY_KEY[k];
    return d && !!d.svg;
  });
  if (!hasSvg) {
    indicatorOverlayEl.replaceChildren();
    return;
  }

  while (indicatorOverlayEl.firstChild) indicatorOverlayEl.removeChild(indicatorOverlayEl.firstChild);

  const width = candleChartEl.clientWidth;
  const height = candleChartEl.clientHeight;
  if (!width || !height) return;
  indicatorOverlayEl.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const NS = "http://www.w3.org/2000/svg";
  const timeX = (t) => lwChart.timeScale().timeToCoordinate(t);
  const priceY = (p) => lwCandleSeries.priceToCoordinate(p);
  const ok = (v) => v !== null && v !== undefined && !Number.isNaN(v);
  const lastTime = lastBars.length ? lastBars[lastBars.length - 1].time : null;

  const ctx = {
    width, height,
    polyline(vals, color, lw) {
      let d = "";
      for (let i = 0; i < vals.length; i++) {
        const v = vals[i];
        if (na(v)) continue;
        const x = timeX(lastBars[i].time), y = priceY(v);
        if (!ok(x) || !ok(y)) continue;
        d += (d ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
      }
      if (!d) return;
      const el = document.createElementNS(NS, "path");
      el.setAttribute("d", d);
      el.setAttribute("fill", "none");
      el.setAttribute("stroke", color);
      el.setAttribute("stroke-width", String(lw || 1));
      el.setAttribute("stroke-linejoin", "round");
      indicatorOverlayEl.appendChild(el);
    },
    polylinePivots(pts, color) {
      let d = "";
      pts.forEach((p) => {
        const x = timeX(lastBars[p.i].time), y = priceY(p.price);
        if (!ok(x) || !ok(y)) return;
        d += (d ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
      });
      if (!d) return;
      const el = document.createElementNS(NS, "path");
      el.setAttribute("d", d);
      el.setAttribute("fill", "none");
      el.setAttribute("stroke", color);
      el.setAttribute("stroke-width", "1.2");
      el.setAttribute("stroke-dasharray", "5 4");
      indicatorOverlayEl.appendChild(el);
    },
    fillBand(topVals, bottomVals, fill) {
      let path = "";
      let started = false;
      for (let i = 0; i < topVals.length; i++) {
        const a = topVals[i], b = bottomVals[i];
        if (na(a) || na(b)) continue;
        const x = timeX(lastBars[i].time);
        if (!ok(x)) continue;
        const yTop = priceY(a), yBot = priceY(b);
        if (!ok(yTop) || !ok(yBot)) continue;
        if (!started) { path += `M${x.toFixed(1)} ${yBot.toFixed(1)}`; started = true; }
        else path += `L${x.toFixed(1)} ${yBot.toFixed(1)}`;
      }
      // reverse along the top edge
      let topPath = "";
      for (let i = topVals.length - 1; i >= 0; i--) {
        const a = topVals[i], b = bottomVals[i];
        if (na(a) || na(b)) continue;
        const x = timeX(lastBars[i].time);
        if (!ok(x)) continue;
        const yTop = priceY(a);
        if (!ok(yTop)) continue;
        topPath += `L${x.toFixed(1)} ${yTop.toFixed(1)}`;
      }
      if (!path) return;
      const el = document.createElementNS(NS, "path");
      el.setAttribute("d", path + topPath + "Z");
      el.setAttribute("fill", fill);
      el.setAttribute("stroke", "none");
      indicatorOverlayEl.appendChild(el);
    },
    hline(price, color, label, bold, extended, count) {
      const y = priceY(price);
      if (!ok(y)) return;
      const el = document.createElementNS(NS, "line");
      el.setAttribute("x1", "0"); el.setAttribute("x2", String(width));
      el.setAttribute("y1", y.toFixed(1)); el.setAttribute("y2", y.toFixed(1));
      el.setAttribute("stroke", color);
      el.setAttribute("stroke-width", bold ? "1.6" : "1.1");
      el.setAttribute("stroke-dasharray", extended ? "6 4" : "8 4");
      indicatorOverlayEl.appendChild(el);
      if (label) {
        const t = document.createElementNS(NS, "text");
        t.setAttribute("x", String(width - 6)); t.setAttribute("y", (y - 5).toFixed(1));
        t.setAttribute("text-anchor", "end");
        t.setAttribute("fill", color);
        t.setAttribute("font-size", count ? "9" : "10");
        t.setAttribute("font-family", "IBM Plex Mono, monospace");
        t.setAttribute("font-weight", "700");
        t.textContent = count ? `${label} ×${count}` : label;
        indicatorOverlayEl.appendChild(t);
      }
    },
    hBar(price, halfBand, widthPx, fill, edgeColor) {
      const y = priceY(price);
      if (!ok(y)) return;
      const top = priceY(price + halfBand), bottom = priceY(price - halfBand);
      if (!ok(top) || !ok(bottom)) return;
      const h = Math.max(1.5, Math.abs(bottom - top) - 1);
      const rect = document.createElementNS(NS, "rect");
      rect.setAttribute("x", String(width - widthPx));
      rect.setAttribute("y", String(Math.min(top, bottom) + 0.5));
      rect.setAttribute("width", String(widthPx));
      rect.setAttribute("height", String(h));
      rect.setAttribute("fill", fill);
      rect.setAttribute("rx", "1");
      indicatorOverlayEl.appendChild(rect);
      if (edgeColor) {
        const edge = document.createElementNS(NS, "line");
        edge.setAttribute("x1", String(width - widthPx)); edge.setAttribute("x2", String(width));
        edge.setAttribute("y1", y.toFixed(1)); edge.setAttribute("y2", y.toFixed(1));
        edge.setAttribute("stroke", edgeColor);
        edge.setAttribute("stroke-width", "1");
        indicatorOverlayEl.appendChild(edge);
      }
    },
    box(t0, t1, top, bottom, fill, stroke, label) {
      const x0 = timeX(t0), x1 = t1 !== null ? timeX(t1) : (lastTime !== null ? timeX(lastTime) : null);
      const y0 = priceY(top), y1 = priceY(bottom);
      if (!ok(x0) || !ok(y0) || !ok(y1)) return;
      const x2 = (ok(x1) && x1 > x0) ? x1 : width;
      const rect = document.createElementNS(NS, "rect");
      rect.setAttribute("x", x0.toFixed(1));
      rect.setAttribute("y", Math.min(y0, y1).toFixed(1));
      rect.setAttribute("width", (x2 - x0).toFixed(1));
      rect.setAttribute("height", Math.max(2, Math.abs(y1 - y0)).toFixed(1));
      rect.setAttribute("fill", fill);
      rect.setAttribute("stroke", stroke);
      rect.setAttribute("stroke-width", "1");
      rect.setAttribute("stroke-dasharray", "3 3");
      indicatorOverlayEl.appendChild(rect);
      if (label) {
        const t = document.createElementNS(NS, "text");
        t.setAttribute("x", (x0 + 3).toFixed(1));
        t.setAttribute("y", (Math.min(y0, y1) - 3).toFixed(1));
        t.setAttribute("fill", stroke);
        t.setAttribute("font-size", "8");
        t.setAttribute("font-family", "IBM Plex Mono, monospace");
        t.setAttribute("font-weight", "700");
        t.textContent = label;
        indicatorOverlayEl.appendChild(t);
      }
    },
    marker(i, price, label, color) {
      const x = timeX(lastBars[i].time);
      const y = priceY(price);
      if (!ok(x) || !ok(y)) return;
      const dot = document.createElementNS(NS, "circle");
      dot.setAttribute("cx", x.toFixed(1)); dot.setAttribute("cy", y.toFixed(1)); dot.setAttribute("r", "2.4");
      dot.setAttribute("fill", color);
      indicatorOverlayEl.appendChild(dot);
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", (x + 5).toFixed(1)); t.setAttribute("y", (y - 5).toFixed(1));
      t.setAttribute("fill", color);
      t.setAttribute("font-size", "9");
      t.setAttribute("font-family", "IBM Plex Mono, monospace");
      t.setAttribute("font-weight", "700");
      t.textContent = label;
      indicatorOverlayEl.appendChild(t);
    },
  };

  Object.keys(activeIndicators).forEach((key) => {
    const def = IND_BY_KEY[key];
    if (def && def.svg) {
      try { def.svg(lastBars, ctx); } catch (e) { /* noop */ }
    }
  });
}

// ----------------------- structure helpers ------------------------------

function detectSwings(bars, k) {
  const highs = [], lows = [];
  for (let i = k; i < bars.length - k; i++) {
    let isHigh = true, isLow = true;
    for (let j = i - k; j <= i + k; j++) {
      if (j === i) continue;
      if (bars[j].high >= bars[i].high) isHigh = false;
      if (bars[j].low <= bars[i].low) isLow = false;
    }
    if (isHigh) highs.push(i);
    if (isLow) lows.push(i);
  }
  return { highs, lows };
}

function zigzag(bars, k) {
  const { highs, lows } = detectSwings(bars, k);
  const idx = [];
  highs.forEach((i) => idx.push({ i, dir: 1, price: bars[i].high }));
  lows.forEach((i) => idx.push({ i, dir: -1, price: bars[i].low }));
  idx.sort((a, b) => a.i - b.i);
  const pts = [];
  for (const e of idx) {
    if (!pts.length) { pts.push(e); continue; }
    const last = pts[pts.length - 1];
    if (e.dir === last.dir) {
      if ((e.dir === 1 && e.price > last.price) || (e.dir === -1 && e.price < last.price)) pts[pts.length - 1] = e;
    } else {
      pts.push(e);
    }
  }
  return pts;
}

function structureEvents(pts) {
  const events = [];
  if (pts.length < 3) return events;
  let trend = pts[1].price > pts[0].price ? 1 : -1;
  for (let i = 2; i < pts.length; i++) {
    const cur = pts[i], prev2 = pts[i - 2];
    if (trend === 1) {
      if (cur.dir === -1 && cur.price < prev2.price) { events.push({ i: cur.i, type: "CHoCH" }); trend = -1; }
      else if (cur.dir === 1 && cur.price > prev2.price) events.push({ i: cur.i, type: "BOS" });
    } else {
      if (cur.dir === 1 && cur.price > prev2.price) { events.push({ i: cur.i, type: "CHoCH" }); trend = 1; }
      else if (cur.dir === -1 && cur.price < prev2.price) events.push({ i: cur.i, type: "BOS" });
    }
  }
  return events;
}

function clusterLevels(bars, highs, lows) {
  const tolPct = 0.0012;
  const groups = [];
  const add = (price, support) => {
    for (const g of groups) {
      if (Math.abs(g.price - price) / price <= tolPct) {
        g.price = (g.price * g.count + price) / (g.count + 1);
        g.count += 1;
        g.support = support;
        return;
      }
    }
    groups.push({ price, count: 1, support });
  };
  highs.forEach((h) => add(h, false));
  lows.forEach((l) => add(l, true));
  const sorted = groups.sort((a, b) => b.count - a.count || b.price - a.price);
  const res = sorted.filter((g) => !g.support).slice(0, 4);
  const sup = sorted.filter((g) => g.support).slice(0, 4);
  return [...res, ...sup].filter((g) => g.count >= 2).sort((a, b) => b.price - a.price);
}

function liquidityPools(bars) {
  const tolPct = 0.0008;
  const n = bars.length;
  const pools = [];
  const add = (price, eqh) => {
    for (const p of pools) {
      if (Math.abs(p.price - price) / price <= tolPct) {
        p.price = (p.price * p.count + price) / (p.count + 1);
        p.count += 1;
        return;
      }
    }
    pools.push({ price, count: 1, eqh });
  };
  for (let i = 3; i < n - 3; i++) {
    const nearH = bars.slice(i - 3, i).some((b) => Math.abs(b.high - bars[i].high) / bars[i].high <= tolPct);
    const nearL = bars.slice(i - 3, i).some((b) => Math.abs(b.low - bars[i].low) / bars[i].low <= tolPct);
    if (nearH) add(bars[i].high, true);
    if (nearL) add(bars[i].low, false);
  }
  return pools.filter((p) => p.count >= 2).sort((a, b) => b.price - a.price);
}

// -------------------------- panel UI -------------------------------------

function buildIndicatorList(filter) {
  if (!indicatorListEl) return;
  const q = (filter || "").trim().toLowerCase();
  const matches = (d) => {
    if (!q) return true;
    return (d.name + " " + d.desc + " " + d.group).toLowerCase().includes(q);
  };

  let html = "";
  const groups = ["Trend", "Momentum", "Volatility", "Volume", "Structure"];
  let shown = 0;
  groups.forEach((g) => {
    const items = INDICATOR_LIB.filter((d) => d.group === g && matches(d));
    if (!items.length) return;
    html += `<div class="indicator-group">${g}</div>`;
    items.forEach((d) => {
      const on = !!activeIndicators[d.key];
      shown += 1;
      html += `
        <button type="button" class="indicator-item ${on ? "on" : "off"}" data-key="${d.key}">
          <div class="indicator-item-main">
            <span class="indicator-item-name">${d.name}</span>
            <span class="indicator-item-desc">${d.desc}</span>
          </div>
          ${d.badge ? `<span class="indicator-item-badge">${d.badge}</span>` : ""}
          <span class="indicator-switch"><span class="indicator-switch-knob"></span></span>
        </button>`;
    });
  });

  if (!shown) {
    html = `<div class="indicator-empty">No indicators match “${escapeHtml(filter)}”</div>`;
  }
  indicatorListEl.innerHTML = html;
  if (indicatorListCountEl) indicatorListCountEl.textContent = shown + " of " + INDICATOR_LIB.length + " indicators";
  updateIndicatorCounts();
}

function updateIndicatorCounts() {
  const active = Object.keys(activeIndicators).length;
  if (indicatorActiveCountEl) indicatorActiveCountEl.textContent = String(active);
  if (indicatorBtnCountEl) {
    indicatorBtnCountEl.textContent = String(active);
    indicatorBtnCountEl.classList.toggle("zero", active === 0);
  }
  if (indicatorBtnEl) {
    indicatorBtnEl.classList.toggle("active", (indicatorPanelEl && !indicatorPanelEl.hidden) || active > 0);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function setIndicatorPanelOpen(open) {
  if (!indicatorPanelEl) return;
  indicatorPanelEl.hidden = !open;
  if (indicatorBtnEl) indicatorBtnEl.classList.toggle("active", open || Object.keys(activeIndicators).length > 0);
  if (open && indicatorSearchEl) {
    indicatorSearchEl.focus();
    try { indicatorSearchEl.select(); } catch (e) { /* input-only method — safe to ignore */ }
  }
}

function toggleIndicator(key) {
  const def = IND_BY_KEY[key];
  if (!def) return;
  ensureChartInitialized();
  if (activeIndicators[key]) {
    delete activeIndicators[key];
    clearIndicator(key);
  } else {
    activeIndicators[key] = true;
    renderIndicator(def);
  }
  renderIndicatorOverlay();
  buildIndicatorList(indicatorSearchEl ? indicatorSearchEl.value : "");
  updateIndicatorCounts();
}

function clearAllIndicators() {
  Object.keys(activeIndicators).forEach((key) => clearIndicator(key));
  activeIndicators = {};
  renderIndicatorOverlay();
  buildIndicatorList(indicatorSearchEl ? indicatorSearchEl.value : "");
  updateIndicatorCounts();
}

// --- wire up the panel ---------------------------------------------------

if (indicatorBtnEl) {
  indicatorBtnEl.addEventListener("click", (e) => {
    e.stopPropagation();
    setIndicatorPanelOpen(indicatorPanelEl.hidden);
  });
}
if (indicatorPanelEl) {
  indicatorPanelEl.addEventListener("click", (e) => e.stopPropagation());
}
document.addEventListener("click", (e) => {
  if (indicatorPanelEl && !indicatorPanelEl.hidden && !indicatorPanelEl.contains(e.target) && !(indicatorBtnEl && indicatorBtnEl.contains(e.target))) {
    setIndicatorPanelOpen(false);
  }
});
if (indicatorSearchEl) {
  indicatorSearchEl.addEventListener("input", () => buildIndicatorList(indicatorSearchEl.value));
  indicatorSearchEl.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setIndicatorPanelOpen(false);
  });
}
if (indicatorClearAllEl) {
  indicatorClearAllEl.addEventListener("click", () => clearAllIndicators());
}
if (indicatorListEl) {
  indicatorListEl.addEventListener("click", (e) => {
    const row = e.target.closest(".indicator-item");
    if (row) toggleIndicator(row.dataset.key);
  });
}
document.addEventListener("keydown", (e) => {
  if (indicatorPanelEl && !indicatorPanelEl.hidden && e.key === "Escape") {
    setIndicatorPanelOpen(false);
    return;
  }
  if (indicatorBtnEl && e.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
    const livePanel = document.getElementById("panel-livechart");
    if (livePanel && livePanel.classList.contains("active")) {
      e.preventDefault();
      setIndicatorPanelOpen(true);
    }
  }
});

buildIndicatorList("");
