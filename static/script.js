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

// Liquidity scanner (CH.19-27) auto-refresh state.
let currentCoin = null;
let lastLiquidityExtra = null;
let liquidityFetchInFlight = false;
let liquidityPollTimer = null;

// Radar liquidity scanner state (CH.19-27)
let radarCanvas = null;
let radarCtx = null;
let radarAnimId = null;
let radarAngle = 0;
let radarLastTime = 0;
let radarDots = []; // {side, angle, radius, pulsing}
let radarVisible = false;
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
  // Radar visibility management
  if (name === "liquidity") {
    resumeRadar();
    // Init canvas on first show if not already done
    setTimeout(initRadarCanvas, 50);
    if (currentCoin) fetchAndRenderLiquidity(currentCoin);
  } else {
    pauseRadar();
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

function radarRender(timestamp) {
  if (!radarVisible || !radarCanvas || !radarCtx) return;

  // Delta time for smooth animation
  if (radarLastTime === 0) radarLastTime = timestamp;
  const delta = timestamp - radarLastTime;
  radarLastTime = timestamp;

  // Rotation: full circle every 3 seconds
  radarAngle = (radarAngle + delta * 0.0021) % (Math.PI * 2);

  const W = radarCanvas.width;
  const H = radarCanvas.height;
  const CX = W / 2;
  const CY = H / 2;
  const maxR = Math.min(CX, CY) - 10;

  // Clear
  radarCtx.clearRect(0, 0, W, H);
  radarCtx.fillStyle = 'rgba(3, 10, 7, 0.96)';
  radarCtx.fillRect(0, 0, W, H);

  // Concentric rings (20/40/60/80/100%)
  for (let i = 5; i >= 1; i--) {
    const r = maxR * i / 5;
    radarCtx.beginPath();
    radarCtx.arc(CX, CY, r, 0, Math.PI * 2);
    radarCtx.strokeStyle = 'rgba(54, 224, 160, ' + (0.06 + 0.03 * (6 - i)) + ')';
    radarCtx.lineWidth = i === 5 ? 1.5 : 1;
    radarCtx.stroke();
  }

  // Crosshair lines
  radarCtx.strokeStyle = 'rgba(54, 224, 160, 0.16)';
  radarCtx.lineWidth = 1;
  radarCtx.beginPath();
  radarCtx.moveTo(0, CY); radarCtx.lineTo(W, CY);
  radarCtx.moveTo(CX, 0); radarCtx.lineTo(CX, H);
  radarCtx.stroke();

  // Diagonal lines (like a real radar)
  for (let d = 0; d < 4; d++) {
    const a = d * Math.PI / 4;
    radarCtx.beginPath();
    radarCtx.moveTo(CX - Math.cos(a) * maxR, CY - Math.sin(a) * maxR);
    radarCtx.lineTo(CX + Math.cos(a) * maxR, CY + Math.sin(a) * maxR);
    radarCtx.strokeStyle = 'rgba(54, 224, 160, 0.08)';
    radarCtx.stroke();
  }

  // Rotating sweep beam with 5 trailing segments
  for (let i = 0; i < 5; i++) {
    const a = radarAngle - i * 0.16;
    const len = maxR * (1 - i * 0.18);
    const alpha = 0.75 - i * 0.14;
    radarCtx.save();
    radarCtx.translate(CX, CY);
    radarCtx.rotate(a);
    radarCtx.beginPath();
    radarCtx.moveTo(0, 0);
    radarCtx.lineTo(len, 0);
    radarCtx.strokeStyle = 'rgba(54, 224, 160, ' + Math.max(alpha, 0.05) + ')';
    radarCtx.lineWidth = 3 - i * 0.5;
    radarCtx.shadowBlur = 18;
    radarCtx.shadowColor = 'rgba(54, 224, 160, 0.8)';
    radarCtx.stroke();
    radarCtx.restore();
  }

  // Leading edge glow
  radarCtx.save();
  radarCtx.translate(CX, CY);
  radarCtx.rotate(radarAngle);
  radarCtx.beginPath();
  radarCtx.moveTo(0, 0);
  radarCtx.lineTo(maxR, 0);
  radarCtx.strokeStyle = 'rgba(80, 255, 190, 0.9)';
  radarCtx.lineWidth = 2.5;
  radarCtx.shadowBlur = 30;
  radarCtx.shadowColor = 'rgba(54, 224, 160, 1)';
  radarCtx.stroke();
  radarCtx.restore();

  // Blips / dots
  const now = Date.now();
  for (const dot of radarDots) {
    const a = dot.angle;
    const r = dot.radius;
    const x = CX + Math.cos(a) * r;
    const y = CY + Math.sin(a) * r;

    const isBuy = dot.side === 'buy';
    const isSell = dot.side === 'sell';
    const isSweep = dot.side === 'sweep';
    const isPrice = dot.side === 'price';

    let col, glow;
    if (isBuy || (isSweep && dot.sweepDir === 'buy')) {
      col = '54, 224, 160';
      glow = 'rgba(54, 224, 160, 0.9)';
    } else if (isSell || (isSweep && dot.sweepDir === 'sell')) {
      col = '255, 82, 107';
      glow = 'rgba(255, 82, 107, 0.9)';
    } else {
      col = '245, 166, 35';
      glow = 'rgba(245, 166, 35, 0.9)';
    }

    // Pulse on sweep blips
    const pulse = isSweep ? 1 + 0.25 * Math.sin(now / 250) : 1;
    const br = (isSweep ? 5 : isPrice ? 3.5 : 4.5) * pulse;

    radarCtx.beginPath();
    radarCtx.arc(x, y, br, 0, Math.PI * 2);
    radarCtx.fillStyle = 'rgba(' + col + ', 0.95)';
    radarCtx.shadowBlur = isSweep ? 24 : 12;
    radarCtx.shadowColor = glow;
    radarCtx.fill();

    // Outer halo ring
    radarCtx.beginPath();
    radarCtx.arc(x, y, br + 4, 0, Math.PI * 2);
    radarCtx.strokeStyle = 'rgba(' + col + ', 0.3)';
    radarCtx.lineWidth = 1;
    radarCtx.stroke();

    radarCtx.shadowBlur = 0;
  }

  // Center dot
  radarCtx.beginPath();
  radarCtx.arc(CX, CY, 2.5, 0, Math.PI * 2);
  radarCtx.fillStyle = 'rgba(54, 224, 160, 0.9)';
  radarCtx.shadowBlur = 12;
  radarCtx.shadowColor = 'rgba(54, 224, 160, 0.9)';
  radarCtx.fill();
  radarCtx.shadowBlur = 0;

  radarAnimId = requestAnimationFrame(radarRender);
}

function initRadarCanvas() {
  const canvas = document.getElementById('radar-canvas');
  if (!canvas || radarCtx) return;
  radarCanvas = canvas;
  radarCtx = canvas.getContext('2d');

  // Set canvas resolution
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  radarCtx.scale(dpr, dpr);

  radarVisible = true;
  radarLastTime = 0;
  radarAnimId = requestAnimationFrame(radarRender);
}

function updateRadarDots(sweep, price, zones) {
  if (!radarCanvas) return;
  const W = radarCanvas.width;
  const H = radarCanvas.height;
  const maxR = Math.min(W, H) / 2 - 10;
  if (maxR <= 10) return;

  radarDots = [];

  // Central sweep blip
  if (sweep && sweep.liquidity_sweep_detected && sweep.sweep_direction) {
    const dir = String(sweep.sweep_direction);
    radarDots.push({
      side: 'sweep',
      sweepDir: dir.indexOf('HIGH') >= 0 ? 'sell' : 'buy',
      angle: Math.PI * 1.25,
      radius: maxR * 0.12
    });
  }

  // Liquidity zones -> position around radar
  if (zones && Array.isArray(zones) && zones.length) {
    const buyZones = zones.filter((z) => z.side === 'BUY_WALL');
    const sellZones = zones.filter((z) => z.side === 'SELL_WALL');

    buyZones.forEach((z, i) => {
      const dist = Math.min(Math.max(z.distance_pct || 5, 2), 100);
      const radius = maxR * 0.25 + (dist / 100) * maxR * 0.65;
      const angle = Math.PI + (i + 1) * (Math.PI / (buyZones.length + 1)); // bottom half
      radarDots.push({ side: 'buy', angle: angle, radius: radius });
    });

    sellZones.forEach((z, i) => {
      const dist = Math.min(Math.max(z.distance_pct || 5, 2), 100);
      const radius = maxR * 0.25 + (dist / 100) * maxR * 0.65;
      const angle = (i + 1) * (Math.PI / (sellZones.length + 1)); // top half
      radarDots.push({ side: 'sell', angle: angle, radius: radius });
    });
  }

  // Current price marker (bottom center)
  if (!na(price) && sweep && !na(sweep.swing_low) && !na(sweep.swing_high)) {
    radarDots.push({ side: 'price', angle: Math.PI * 1.5, radius: maxR * 0.28 });
  }
}

function pauseRadar() {
  radarVisible = false;
  if (radarAnimId) {
    cancelAnimationFrame(radarAnimId);
    radarAnimId = null;
  }
}

function resumeRadar() {
  if (!radarCanvas) {
    initRadarCanvas();
    return;
  }
  radarVisible = true;
  radarLastTime = 0;
  if (!radarAnimId) radarAnimId = requestAnimationFrame(radarRender);
}

function paintLiquidityScanner(sweep, price, extra) {
  if (!liqScannerEl) return;

  // Feed radar with current liquidity data
  const zones = (extra && extra.liquidity_zones) || (extra && extra.liquidity_target_zones) || [];
  updateRadarDots(sweep, price, zones);

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
  renderLiquidityScanner(data);

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
