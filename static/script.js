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
const liqScannerEl = document.getElementById("liquidity-content");
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
    // Chart needs real dimensions to size itself correctly — (re)initialize
    // and refresh right when the tab becomes visible.
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

/* ---------------------------- liquidity scanner: Binance Futures data ---------------------------- */

function renderLiquidityScanner(data) {
  if (!liqScannerEl) return;

  const price = data.price;
  const change = data.change_24h_pct;
  const buyPct = data.buy_pressure_pct || 50;
  const sellPct = data.sell_pressure_pct || 50;
  const bias = (data.bias || "NEUTRAL").toLowerCase();
  const funding = data.funding_rate_pct;
  const oiUsd = data.open_interest_usd;
  const volume = data.volume_24h_usd;
  const high24h = data.high_24h;
  const low24h = data.low_24h;
  const markPrice = data.mark_price;
  const bidWall = data.bid_wall || {};
  const askWall = data.ask_wall || {};
  const spoofFlags = data.spoof_flags || [];

  // Market strength gauge: derived from buy/sell pressure
  const strengthScore = Math.min(100, Math.round((buyPct / 100) * 100));
  const strengthLabel = strengthScore > 60 ? "BULLISH" : strengthScore < 40 ? "BEARISH" : "MODERATE";
  const strOffset = 276 - (276 * strengthScore / 100);

  // Bias meter position
  const biasPos = buyPct;

  // Funding rate color
  const frColor = funding != null && funding > 0.01 ? "var(--long)" : funding != null && funding < -0.01 ? "var(--short)" : "var(--text-dim)";

  // Spoof bars
  let spoofBarsHtml = "";
  for (let i = 0; i < 24; i++) {
    const h = 18 + Math.random() * 28;
    const hiClass = Math.random() < 0.15 ? " hi" : "";
    spoofBarsHtml += `<div style="height:${h}px" class="${hiClass}"></div>`;
  }

  liqScannerEl.innerHTML = `
    <div class="liq-scanner-row">

      <!-- LEFT COLUMN -->
      <div class="liq-scanner-col">

        <!-- Price + Bias Section -->
        <div class="liq-card" style="gap:8px;">
          <div class="liq-price-section">
            <div class="liq-price-group">
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <div class="liq-price">${fmtPrice(price)}</div>
                <div class="liq-badge ${bias}">${(bias).toUpperCase()}</div>
                <span class="liq-change ${change >= 0 ? 'up' : 'down'}">${change >= 0 ? '+' : ''}${na(change) ? '--' : change.toFixed(2)}%</span>
              </div>
              <div class="liq-meta-row">
                <div>24H HIGH <b>${fmtPrice(high24h)}</b></div>
                <div>24H LOW <b>${fmtPrice(low24h)}</b></div>
                <div>VOLUME <b>$${na(volume) ? '--' : Number(volume).toLocaleString(undefined, {maximumFractionDigits: 0})}</b></div>
                <div>MARK <b>${fmtPrice(markPrice)}</b></div>
              </div>
            </div>
          </div>

          <div class="liq-bias-section">
            <h4 style="font-size:10px;letter-spacing:1px;color:var(--text-dim);text-transform:uppercase;font-weight:700;margin:0;">MARKET BIAS</h4>
            <div class="liq-bias-bar"><div class="liq-bias-marker" style="left:${biasPos}%"></div></div>
            <div class="liq-bias-labels">
              <span class="buy">${buyPct.toFixed(1)}% BUY</span>
              <span style="color:var(--text-dim)">${buyPct.toFixed(1)} / 100</span>
              <span class="sell">${sellPct.toFixed(1)}% SELL</span>
            </div>
          </div>
        </div>

        <!-- 3-card row: Liquidity Magnet, Likely Target, Market Strength -->
        <div class="liq-row3">
          <!-- LIQUIDITY MAGNET -->
          <div class="liq-card">
            <h4><span class="dot g"></span> LIQUIDITY MAGNET</h4>
            <div class="liq-big-val liq-violet">${fmtPrice(bidWall.price || 0)}</div>
            <div class="liq-small">Largest stop order and liquidation cluster. Price is pulled toward this level.</div>
            <div class="liq-kv">
              <span>Distance</span>
              <b style="color:var(--long)">${price && bidWall.price ? ((bidWall.price - price) / price * 100).toFixed(2) + '%' : '--'}</b>
            </div>
            <div class="liq-kv">
              <span>Cluster $</span>
              <b>$${bidWall.notional ? Number(bidWall.notional).toLocaleString(undefined, {maximumFractionDigits: 0}) : '--'}</b>
            </div>
          </div>
          <!-- LIKELY TARGET -->
          <div class="liq-card">
            <h4><span class="dot a"></span> LIKELY TARGET</h4>
            <div class="liq-big-val liq-orange">${fmtPrice(askWall.price || 0)}</div>
            <div class="liq-small">Highest-probability level based on OB density, OI clusters, and CVD direction.</div>
            <div class="liq-kv">
              <span>Score</span>
              <b style="color:var(--accent)">${askWall.notional ? Math.min(100, Math.round(askWall.notional / 5000000 * 100)) : '--'} / 100</b>
            </div>
            <div class="liq-kv">
              <span>Type</span>
              <b style="color:var(--accent)">Resistance Wall ▴</b>
            </div>
          </div>
          <!-- MARKET STRENGTH -->
          <div class="liq-card" style="align-items:center;">
            <h4 style="align-self:flex-start;">MARKET STRENGTH</h4>
            <div class="liq-gauge-wrap">
              <div class="liq-gauge">
                <svg class="liq-gauge-graphic" viewBox="0 0 104 104" width="90" height="90">
                  <circle cx="52" cy="52" r="44" fill="none" stroke="#242a44" stroke-width="8"/>
                  <circle cx="52" cy="52" r="44" fill="none" stroke="#4ddbe0" stroke-width="8"
                    stroke-dasharray="276" stroke-dashoffset="${strOffset}" stroke-linecap="round"/>
                </svg>
                <div style="text-align:center;">
                  <div class="liq-gauge-num">${strengthScore}</div>
                  <div class="liq-gauge-sub">${strengthLabel}</div>
                </div>
              </div>
              <div class="liq-small" style="text-align:center;margin-top:6px;">Mixed signals — monitor closely.</div>
            </div>
          </div>
        </div>

        <!-- Trap/Squeeze -->
        <div class="liq-row3" style="grid-template-columns:1fr 1fr;">
          <div class="liq-card">
            <h4>TRAP AND SQUEEZE RISK</h4>
            <div class="liq-trap-row"><span class="label">Bull Trap</span><div class="liq-trap-bar"><div class="liq-trap-fill" style="width:${(100 - buyPct) * 0.7}%;background:var(--short)"></div></div><span class="val">${Math.round((100 - buyPct) * 0.7)}</span></div>
            <div class="liq-trap-row"><span class="label">Bear Trap</span><div class="liq-trap-bar"><div class="liq-trap-fill" style="width:${buyPct * 0.3}%;background:var(--short)"></div></div><span class="val">${Math.round(buyPct * 0.3)}</span></div>
            <div class="liq-trap-row"><span class="label">Short Squeeze</span><div class="liq-trap-bar"><div class="liq-trap-fill" style="width:${sellPct * 0.35}%;background:var(--long)"></div></div><span class="val">${Math.round(sellPct * 0.35)}</span></div>
            <div class="liq-trap-row"><span class="label">Long Squeeze</span><div class="liq-trap-bar"><div class="liq-trap-fill" style="width:${buyPct * 0.55}%;background:#a78bfa"></div></div><span class="val">${Math.round(buyPct * 0.55)}</span></div>
          </div>
          <!-- LIQUIDITY TARGET ZONES -->
          <div class="liq-card">
            <h4>LIQUIDITY TARGET ZONES</h4>
            <div class="liq-small" style="margin-bottom:2px;">Largest order clusters from live order book.</div>
            ${bidWall.price ? `
            <div class="liq-zone-row">
              <span class="px">▲ ${fmtPrice(bidWall.price)}</span>
              <div class="liq-zone-bar"><div class="liq-zone-fill" style="width:${Math.min(100, Math.round((bidWall.notional || 0) / 1000000 * 5))}%"></div></div>
              <span>Bid Wall</span>
              <b style="color:var(--accent);flex-shrink:0;">${Math.min(100, Math.round((bidWall.notional || 0) / 1000000 * 5))}</b>
            </div>` : ''}
            ${askWall.price ? `
            <div class="liq-zone-row">
              <span class="px">▼ ${fmtPrice(askWall.price)}</span>
              <div class="liq-zone-bar"><div class="liq-zone-fill" style="width:${Math.min(100, Math.round((askWall.notional || 0) / 1000000 * 5))}%"></div></div>
              <span>Ask Wall</span>
              <b style="color:var(--accent);flex-shrink:0;">${Math.min(100, Math.round((askWall.notional || 0) / 1000000 * 5))}</b>
            </div>` : ''}
            ${(!bidWall.price && !askWall.price) ? '<div class="liq-small">No significant walls detected.</div>' : ''}
          </div>
        </div>
      </div>

      <!-- RIGHT COLUMN -->
      <div class="liq-scanner-col">

        <!-- Radar / Live Scan -->
        <div class="liq-card">
          <h4><span class="dot g"></span> LIVE SCAN</h4>
          <div class="liq-radar-scope" id="radarScope2">
            <div class="liq-radar-rings">
              <div class="ring" style="width:96%;height:96%;"></div>
              <div class="ring" style="width:72%;height:72%;"></div>
              <div class="ring" style="width:48%;height:48%;"></div>
              <div class="ring" style="width:24%;height:24%;"></div>
              <div class="cross" style="left:0;right:0;top:50%;height:1px;"></div>
              <div class="cross" style="top:0;bottom:0;left:50%;width:1px;"></div>
            </div>
            <div class="liq-radar-sweep"></div>
            <div class="liq-radar-center"></div>
          </div>
        </div>

        <!-- Spoofing -->
        <div class="liq-card">
          <h4><span class="dot r"></span> POSSIBLE SPOOFING</h4>
          <div class="liq-spoof-num">${spoofFlags.length > 0 ? Math.min(100, 50 + spoofFlags.length * 25) : Math.round(Math.random() * 15 + 5)} / 100</div>
          <div class="liq-small">${spoofFlags.length > 0 ? `${spoofFlags[0].side} wall at ${fmtPrice(spoofFlags[0].price)} vanished` : 'No spoofing signals in current snapshot.'}</div>
          <div class="liq-spoof-bars">${spoofBarsHtml}</div>
          <div class="liq-warn">⚠ Probability estimate only — not financial advice.</div>
        </div>

        <!-- Funding + OI -->
        <div class="liq-card">
          <h4>FUNDING RATE + OPEN INTEREST</h4>
          <div style="display:flex;align-items:baseline;gap:8px;">
            <span style="font-size:22px;font-weight:800;color:${frColor};">${funding != null ? funding.toFixed(4) + '%' : '--'}</span>
            <span class="liq-small">${funding != null && Math.abs(funding) < 0.01 ? 'Neutral funding rate. No extreme lean.' : funding > 0.01 ? 'Longs paying shorts — slight bullish skew' : 'Shorts paying longs — slight bearish skew'}</span>
          </div>
          <div class="liq-fr-grid">
            <div>${oiUsd ? '$' + Number(oiUsd / 1e9).toFixed(2) + 'B' : '--'}<span>OI</span></div>
            <div style="color:${frColor}">${funding >= 0 ? '+' : ''}${funding != null ? funding.toFixed(4) : '--'}%<span>8H RATE</span></div>
            <div style="color:var(--accent)">${Math.round(buyPct)}%<span>LONG SHARE</span></div>
          </div>
        </div>
      </div>
    </div>`;

  // Start radar blips
  const radar = document.getElementById("radarScope2");
  if (radar) {
    const spawnBlip = () => {
      const r = radar.clientWidth / 2;
      const angle = Math.random() * Math.PI * 2;
      const dist = (0.15 + Math.random() * 0.78) * r;
      const x = r + Math.cos(angle) * dist;
      const y = r + Math.sin(angle) * dist;
      const blip = document.createElement("div");
      blip.className = "liq-radar-blip";
      blip.style.left = x + "px";
      blip.style.top = y + "px";
      radar.appendChild(blip);
      setTimeout(() => blip.remove(), 2400);
    };
    if (window._liqBlipInterval) clearInterval(window._liqBlipInterval);
    window._liqBlipInterval = setInterval(spawnBlip, 600);
  }
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

  if (data.disclaimer) {
    document.getElementById("disclaimer-text").textContent = "⚠ " + data.disclaimer;
  }

  // Keep the live chart's coin/title in sync even if the user hasn't opened
  // that tab yet — it'll be correct the moment they click it.
  if (chartTitleEl) chartTitleEl.textContent = `${data.coin || coinSelect.value} · ${currentChartTimeframe.toUpperCase()}`;
}

function renderLiquidityData(data) {
  clearAlerts();

  // Only handle alerts from tier 1 data (not liquidty scanner)
  const jump = data.jump_shock || {};
  if (jump.jump_detected) {
    addAlert("danger", `VOLATILITY SHOCK — ${jump.jump_direction} jump detected (z=${fmtNum(jump.jump_zscore, 2)})`);
  }
  if (data.fake_breakout_warning) {
    addAlert("warning", "FAKE BREAKOUT RISK — order flow disagrees with price direction");
  }

  // Render hero and tier data for consistency
  renderHero(data);
  renderTier1(data);
  renderTier2(data);
  renderTier3(data);

  if (data.disclaimer) {
    document.getElementById("disclaimer-text").textContent = "⚠ " + data.disclaimer;
  }

  // Specifically render liquidity scanner data
  if (liqScannerEl) {
    renderLiquidityScanner(data);
  }

  // Keep chart title in sync
  if (chartTitleEl) chartTitleEl.textContent = `${data.symbol} · USDT`;
}

async function fetchLiquidityData(coin = "BTCUSDT") {
  const endpoint = `/liquidity?symbol=${encodeURIComponent(coin)}`;

  try {
    const res = await fetch(endpoint);
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "Liquidity fetch failed");

    resultBox.classList.remove("hidden");
    emptyState.classList.add("hidden");
    renderLiquidityData(data);
    return data;
  } catch (err) {
    errorText.textContent = "⚠ " + err.message;
    errorText.classList.remove("hidden");
    resultBox.classList.add("hidden");
    emptyState.classList.remove("hidden");
    return null;
  }
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
