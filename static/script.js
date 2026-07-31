/* ================================================================
   SIGNAL/FM — MULTI-CHANNEL MARKET READOUT TERMINAL (CLIENT JS)
================================================================ */

document.addEventListener("DOMContentLoaded", () => {
  // Live Clock
  const clockText = document.getElementById("clock-text");
  if (clockText) {
    setInterval(() => {
      const now = new Date();
      clockText.textContent = now.toUTCString().split(" ")[4] + " UTC";
    }, 1000);
  }

  // DOM Elements
  const coinSelectNative = document.getElementById("coin-select");
  const coinPicker = document.getElementById("coin-picker");
  const coinPickerTrigger = document.getElementById("coin-picker-trigger");
  const coinPickerMenu = document.getElementById("coin-picker-menu");
  const coinPickerIcon = document.getElementById("coin-picker-icon");
  const coinPickerLabel = document.getElementById("coin-picker-label");
  const getSignalBtn = document.getElementById("get-signal-btn");
  const errorTextEl = document.getElementById("error-text");
  const resultBox = document.getElementById("result-box");
  const emptyState = document.getElementById("empty-state");

  // Hero elements
  const gaugeFill = document.getElementById("gauge-fill");
  const gaugeVerdict = document.getElementById("gauge-verdict");
  const gaugeConfidence = document.getElementById("gauge-confidence");
  const heroPrice = document.getElementById("hero-price");
  const heroRsi = document.getElementById("hero-rsi");
  const heroMacd = document.getElementById("hero-macd");
  const heroTrendTag = document.getElementById("hero-trend-tag");

  // Tier containers
  const tier1El = document.getElementById("tier-1");
  const tier2El = document.getElementById("tier-2");
  const tier3El = document.getElementById("tier-3");
  const liqScannerEl = document.getElementById("liquidity-scanner");

  // Tabs
  const panelTabs = document.querySelectorAll(".panel-tab");
  const tabPanels = document.querySelectorAll(".tab-panel");

  panelTabs.forEach(tab => {
    tab.addEventListener("click", () => {
      panelTabs.forEach(t => t.classList.remove("active"));
      tabPanels.forEach(p => p.classList.remove("active"));
      tab.classList.add("active");
      const target = document.getElementById(`panel-${tab.dataset.panel}`);
      if (target) target.classList.add("active");
    });
  });

  // Custom Coin Picker Setup
  function initCoinPicker() {
    if (!coinSelectNative || !coinPickerMenu) return;
    const groups = coinSelectNative.querySelectorAll("optgroup");
    let menuHtml = "";

    groups.forEach(group => {
      menuHtml += `<div class="coin-picker-group-label">${group.label}</div>`;
      const options = group.querySelectorAll("option");
      options.forEach(opt => {
        const symbol = opt.value.split("/")[0].toLowerCase();
        // Fallback icon source or placeholder
        const iconUrl = `https://assets.coincap.io/assets/icons/${symbol}@2x.png`;
        menuHtml += `
          <div class="coin-picker-item" data-value="${opt.value}">
            <img class="coin-picker-icon" src="${iconUrl}" onerror="this.src=''" alt="" />
            <span>${opt.text}</span>
          </div>`;
      });
    });

    coinPickerMenu.innerHTML = menuHtml;

    // Set initial selection
    updatePickerSelection(coinSelectNative.value);

    // Trigger toggle
    coinPickerTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      coinPickerMenu.classList.toggle("open");
      coinPickerTrigger.setAttribute("aria-expanded", coinPickerMenu.classList.contains("open"));
    });

    // Item click
    coinPickerMenu.querySelectorAll(".coin-picker-item").forEach(item => {
      item.addEventListener("click", () => {
        const val = item.dataset.value;
        coinSelectNative.value = val;
        updatePickerSelection(val);
        coinPickerMenu.classList.remove("open");
        coinPickerTrigger.setAttribute("aria-expanded", "false");
      });
    });

    document.addEventListener("click", () => {
      coinPickerMenu.classList.remove("open");
      coinPickerTrigger.setAttribute("aria-expanded", "false");
    });
  }

  function updatePickerSelection(val) {
    if (!coinPickerLabel || !coinPickerIcon) return;
    const option = coinSelectNative.querySelector(`option[value="${val}"]`);
    if (option) {
      coinPickerLabel.textContent = option.text;
      const symbol = val.split("/")[0].toLowerCase();
      coinPickerIcon.src = `https://assets.coincap.io/assets/icons/${symbol}@2x.png`;
    }
  }

  initCoinPicker();

  // Helper formatters
  window.na = function(val) {
    return val === null || val === undefined || isNaN(val);
  };

  window.fmtPrice = function(val) {
    if (window.na(val)) return "--";
    return Number(val).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  };

  window.fmtPct = function(val, decimals = 1) {
    if (window.na(val)) return "--";
    const num = Number(val);
    return (num > 0 ? "+" : "") + num.toFixed(decimals) + "%";
  };

  window.badge = function(text, tone = "flat") {
    return `<span class="badge ${tone}">${text}</span>`;
  };

  // Run Analysis Button Handler
  if (getSignalBtn) {
    getSignalBtn.addEventListener("click", async () => {
      const pair = coinSelectNative.value;
      if (errorTextEl) errorTextEl.classList.add("hidden");
      getSignalBtn.disabled = true;
      getSignalBtn.querySelector(".scan-btn-text").textContent = "SCANNING...";

      try {
        const res = await fetch(`/signal?pair=${encodeURIComponent(pair)}`);
        const json = await res.json();

        if (!res.ok || json.error) {
          throw new Error(json.error || "Failed to fetch signal data.");
        }

        renderDashboard(json);
      } catch (err) {
        if (errorTextEl) {
          errorTextEl.textContent = err.message;
          errorTextEl.classList.remove("hidden");
        }
      } finally {
        getSignalBtn.disabled = false;
        getSignalBtn.querySelector(".scan-btn-text").textContent = "RUN ANALYSIS";
      }
    });
  }

  function renderDashboard(data) {
    if (emptyState) emptyState.classList.add("hidden");
    if (resultBox) resultBox.classList.remove("hidden");

    // Render Hero Gauge & Price
    const verdict = data.verdict || "FLAT";
    const confidence = data.confidence_score || 0;
    if (gaugeVerdict) gaugeVerdict.textContent = verdict;
    if (gaugeConfidence) gaugeConfidence.textContent = `${confidence.toFixed(1)}% confidence`;

    if (gaugeFill) {
      const radius = 48;
      const circumference = 2 * Math.PI * radius;
      const offset = circumference - (confidence / 100) * circumference;
      gaugeFill.style.strokeDashoffset = offset;
      gaugeFill.style.stroke = verdict === "LONG" ? "var(--long)" : verdict === "SHORT" ? "var(--short)" : "var(--wait)";
    }

    if (heroPrice) heroPrice.textContent = fmtPrice(data.last_price);
    if (heroRsi && data.technical_summary) heroRsi.textContent = data.technical_summary.rsi?.toFixed(1) || "--";
    if (heroMacd && data.technical_summary) heroMacd.textContent = data.technical_summary.macd_histogram?.toFixed(4) || "--";
    if (heroTrendTag) {
      heroTrendTag.textContent = data.market_regime || "NEUTRAL REGIME";
    }

    // Render Tier 1 (Ch. 01–05)
    if (tier1El) {
      tier1El.innerHTML = renderChannels([
        { id: "CH.01", title: "Hawkes Intensity Jumps", data: data.hawkes_intensity },
        { id: "CH.02", title: "Bayesian State Classifier", data: data.bayesian_state },
        { id: "CH.03", title: "Conformal Prediction Bounds", data: data.conformal_bounds },
        { id: "CH.04", title: "Order Flow Imbalance", data: data.ofi_metrics },
        { id: "CH.05", title: "Volatility Surface Regime", data: data.vol_surface }
      ]);
    }

    // Render Tier 2 (Ch. 06–10)
    if (tier2El) {
      tier2El.innerHTML = renderChannels([
        { id: "CH.06", title: "Microstructure Depth", data: data.microstructure_depth },
        { id: "CH.07", title: "Liquidity Cluster Radar", data: data.liquidity_cluster },
        { id: "CH.08", title: "Spread Dynamics", data: data.spread_dynamics },
        { id: "CH.09", title: "Markov Regime Transition", data: data.markov_regime },
        { id: "CH.10", title: "Multi-Timeframe Trend", data: data.mtf_trend }
      ]);
    }

    // Render Tier 3 (Ch. 11–18)
    if (tier3El) {
      tier3El.innerHTML = renderChannels([
        { id: "CH.11", title: "Cross-Asset Correlation", data: data.cross_asset },
        { id: "CH.12", title: "Entropy & Complexity", data: data.entropy_metrics },
        { id: "CH.13", title: "Wavelet Energy Spectrum", data: data.wavelet_spectrum },
        { id: "CH.14", title: "Risk Agent Telemetry", data: data.risk_agent },
        { id: "CH.15", title: "Momentum Divergence", data: data.momentum_div },
        { id: "CH.16", title: "Volume Profile Nodes", data: data.volume_profile },
        { id: "CH.17", title: "Funding Rate Arbitrage", data: data.funding_rate },
        { id: "CH.18", title: "Tail Risk Probability", data: data.tail_risk }
      ]);
    }

    // Render Liquidity Scanner (Ch. 19)
    renderLiquidityScanner(data);

    // Initialize/Update Live Chart if available
    initLiveChart(coinSelectNative.value);
  }

  function renderChannels(channels) {
    return channels.map(ch => {
      const d = ch.data || {};
      const signal = d.signal || d.regime || d.state || "FLAT";
      const tone = signal === "LONG" || signal === "BULLISH" ? "long" : signal === "SHORT" || signal === "BEARISH" ? "short" : signal === "WAIT" ? "wait" : "flat";

      return `
        <div class="channel-card">
          <div>
            <div class="channel-header">
              <div>
                <span class="channel-id">${ch.id}</span>
                <div class="channel-title">${ch.title}</div>
              </div>
              ${badge(signal, tone)}
            </div>
            <div class="channel-body">
              ${d.description || d.summary || "Telemetry feed operational. Monitoring cluster conditions."}
            </div>
          </div>
          <div class="channel-metrics">
            <span>Score: <b>${d.score !== undefined ? Number(d.score).toFixed(2) : "--"}</b></span>
            <span>Confidence: <b>${d.confidence !== undefined ? Number(d.confidence).toFixed(1) + "%" : "--"}</b></span>
          </div>
        </div>`;
    }).join("");
  });
});

/* ---------------------------- liquidity scanner: CH.19 (Radar Mode) ---------------------------- */

function renderLiquidityScanner(data) {
  const liqScannerEl = document.getElementById("liquidity-scanner");
  if (!liqScannerEl) return;

  const sweep = data.liquidity_sweep || {};
  const price = data.last_price;

  const hasRange = !window.na(sweep.swing_high) && !window.na(sweep.swing_low) && !window.na(price) && sweep.swing_high > sweep.swing_low;
  let markerPct = 50;
  if (hasRange) {
    const range = sweep.swing_high - sweep.swing_low;
    markerPct = Math.max(2, Math.min(98, ((price - sweep.swing_low) / range) * 100));
  }

  const detected = !!sweep.liquidity_sweep_detected;
  const tone = detected ? "wait" : "flat";
  const statusLabel = detected ? (sweep.sweep_direction || "SWEEP").replace(/_/g, " ") : "RADAR ACTIVE · NO SWEEP";

  liqScannerEl.innerHTML = `
    <div class="liq-panel">
      <div class="liq-header">
        <div class="liq-header-left">
          <span class="liq-icon">✈</span>
          <div>
            <div class="liq-title">Liquidity Radar (Aircraft Sweep Scanner)</div>
            <div class="liq-subtitle">CH.19 · real-time cluster telemetry &amp; sweep detection</div>
          </div>
        </div>
        ${window.badge(statusLabel, tone)}
      </div>

      <!-- Aircraft Radar Screen UI -->
      <div class="radar-container">
        <div class="radar-grid-ring radar-ring-1"></div>
        <div class="radar-grid-ring radar-ring-2"></div>
        <div class="radar-grid-ring radar-ring-3"></div>
        <div class="radar-crosshair-h"></div>
        <div class="radar-crosshair-v"></div>
        <div class="radar-sweep-arm"></div>
        
        <!-- Blips representing Swing High, Low & Mark Price -->
        <div class="radar-blip radar-blip-high" title="Swing High Zone"></div>
        <div class="radar-blip radar-blip-low" title="Swing Low Zone"></div>
        <div class="radar-blip radar-blip-price" title="Current Price Center"></div>
      </div>

      <div class="liq-range">
        <div class="liq-range-track">
          <div class="liq-range-fill"></div>
          <div class="liq-range-endpoint" style="left:0%;"></div>
          <div class="liq-range-endpoint" style="left:100%;"></div>
          <div class="liq-range-endpoint-label" style="left:0%;">${window.fmtPrice(sweep.swing_low)}</div>
          <div class="liq-range-endpoint-label" style="left:100%;">${window.fmtPrice(sweep.swing_high)}</div>
          ${hasRange ? `
            <div class="liq-range-marker" style="left:${markerPct}%;"></div>
            <div class="liq-range-marker-label" style="left:${markerPct}%;">${window.fmtPrice(price)}</div>
          ` : ""}
        </div>
      </div>

      <div class="liq-stats-grid">
        <div class="liq-stat">
          <span class="liq-stat-label">SWING HIGH</span>
          <span class="liq-stat-value">${window.fmtPrice(sweep.swing_high)}</span>
        </div>
        <div class="liq-stat">
          <span class="liq-stat-label">SWING LOW</span>
          <span class="liq-stat-value">${window.fmtPrice(sweep.swing_low)}</span>
        </div>
        <div class="liq-stat">
          <span class="liq-stat-label">DIST → HIGH</span>
          <span class="liq-stat-value text-long">${window.fmtPct(sweep.distance_to_high_pct, 2)}</span>
        </div>
        <div class="liq-stat">
          <span class="liq-stat-label">DIST → LOW</span>
          <span class="liq-stat-value text-short">${window.fmtPct(sweep.distance_to_low_pct, 2)}</span>
        </div>
      </div>
    </div>`;
}

/* ---------------------------- live chart initializer ---------------------------- */
let chartInstance = null;
let candleSeries = null;

function initLiveChart(pair) {
  const container = document.getElementById("candle-chart");
  if (!container || typeof LightweightCharts === "undefined") return;

  if (!chartInstance) {
    chartInstance = LightweightCharts.createChart(container, {
      layout: {
        background: { color: "#0a0e0b" },
        textColor: "#6b8271",
      },
      grid: {
        vertLines: { color: "#1a261e" },
        horzLines: { color: "#1a261e" },
      },
      timeScale: {
        borderColor: "#1a261e",
        timeVisible: true,
      },
      rightPriceScale: {
        borderColor: "#1a261e",
      }
    });

    candleSeries = chartInstance.addCandlestickSeries({
      upColor: "#36e0a0",
      downColor: "#ff4d6d",
      borderVisible: false,
      wickUpColor: "#36e0a0",
      wickDownColor: "#ff4d6d",
    });

    // Resize observer
    window.addEventListener("resize", () => {
      if (container && chartInstance) {
        chartInstance.applyOptions({ width: container.clientWidth, height: container.clientHeight });
      }
    });
  }

  // Fetch candles
  fetch(`/candles?pair=${encodeURIComponent(pair)}&tf=1h`)
    .then(res => res.json())
    .then(data => {
      if (data && Array.isArray(data.candles) && candleSeries) {
        candleSeries.setData(data.candles);
        document.getElementById("chart-status").textContent = "feed active · connected";
      }
    })
    .catch(() => {
      document.getElementById("chart-status").textContent = "feed connection error";
    });
}
