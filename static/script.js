/* ==========================================================================
   Signals FM - Core Terminal Script & Chart Logic
   ========================================================================== */

document.addEventListener("DOMContentLoaded", () => {
  // 1. Sidebar Navigation Switching
  const navItems = document.querySelectorAll(".sidebar-nav .nav-item");
  const panels = document.querySelectorAll(".content-body .panel");

  window.activatePanel = function(panelId) {
    navItems.forEach(nav => {
      if (nav.dataset.panel === panelId) {
        nav.classList.add("active");
      } else {
        nav.classList.remove("active");
      }
    });

    panels.forEach(panel => {
      if (panel.id === `panel-${panelId}`) {
        panel.classList.add("active");
        if (panelId === "livechart" && window.chartInstance) {
          window.chartInstance.timeScale().fitContent();
        }
      } else {
        panel.classList.remove("active");
      }
    });
  };

  navItems.forEach(item => {
    item.addEventListener("click", (e) => {
      e.preventDefault();
      const targetPanel = item.dataset.panel;
      if (targetPanel) {
        activatePanel(targetPanel);
      }
    });
  });

  // 2. Live UTC Clock
  const clockElement = document.getElementById("live-clock");
  if (clockElement) {
    setInterval(() => {
      const now = new Date();
      const utcString = now.toUTCString().split(" ")[4];
      clockElement.textContent = `UTC ${utcString}`;
    }, 1000);
  }

  // 3. Lightweight Charts Initialization (Live Chart Feed)
  const chartContainer = document.getElementById("chart-canvas-area");
  let chart;
  let candlestickSeries;
  let volumeSeries;

  if (chartContainer && typeof LightweightCharts !== "undefined") {
    chart = LightweightCharts.createChart(chartContainer, {
      width: chartContainer.clientWidth,
      height: chartContainer.clientHeight,
      layout: {
        background: { type: 'solid', color: '#121821' },
        textColor: '#9ca3af',
      },
      grid: {
        vertLines: { color: '#1a222d' },
        horzLines: { color: '#1a222d' },
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
      },
      rightPriceScale: {
        borderColor: '#273343',
      },
      timeScale: {
        borderColor: '#273343',
        timeVisible: true,
        secondsVisible: false,
      },
    });

    candlestickSeries = chart.addCandlestickSeries({
      upColor: '#10b981',
      downColor: '#ef4444',
      borderVisible: false,
      wickUpColor: '#10b981',
      wickDownColor: '#ef4444',
    });

    // Sample initial dummy candles to render chart layout immediately
    const dummyData = [
      { time: '2026-08-01', open: 64200, high: 65100, low: 63800, close: 64800 },
      { time: '2026-08-02', open: 64800, high: 65500, low: 64400, close: 65200 },
      { time: '2026-08-03', open: 65200, high: 66000, low: 64900, close: 65700 },
      { time: '2026-08-04', open: 65700, high: 66400, low: 65300, close: 66100 },
    ];
    candlestickSeries.setData(dummyData);
    chart.timeScale().fitContent();

    window.chartInstance = chart;

    // Window Resize Handling
    window.addEventListener("resize", () => {
      if (chartContainer && chart) {
        chart.applyOptions({
          width: chartContainer.clientWidth,
          height: chartContainer.clientHeight,
        });
      }
    });
  }

  // 4. Fullscreen Toggle Logic
  const fullscreenBtn = document.getElementById("chart-fullscreen-btn");
  const backBtn = document.getElementById("chart-back-btn");
  const wrapper = document.getElementById("chart-container-wrapper");

  function toggleFullscreen() {
    if (!wrapper) return;
    wrapper.classList.toggle("fullscreen-mode");
    const isFullscreen = wrapper.classList.contains("fullscreen-mode");
    if (backBtn) {
      if (isFullscreen) {
        backBtn.classList.remove("hidden");
      } else {
        backBtn.classList.add("hidden");
      }
    }
    setTimeout(() => {
      if (chart && chartContainer) {
        chart.applyOptions({
          width: chartContainer.clientWidth,
          height: chartContainer.clientHeight,
        });
        chart.timeScale().fitContent();
      }
    }, 100);
  }

  if (fullscreenBtn) fullscreenBtn.addEventListener("click", toggleFullscreen);
  if (backBtn) backBtn.addEventListener("click", toggleFullscreen);

  // ==========================================================================
  // 5. NEW: Indicators Modal, Search Engine & Smart Navigation Controller
  // ==========================================================================
  const indicatorsBtn = document.getElementById("chart-indicators-btn");
  const modal = document.getElementById("indicator-modal");
  const backdrop = document.getElementById("indicator-modal-backdrop");
  const closeBtn = document.getElementById("indicator-modal-close");
  const searchInput = document.getElementById("indicator-search-input");
  const indicatorList = document.getElementById("indicator-list");

  function openIndicatorModal() {
    // Smart Navigation: Agar user kisi aur tab par ho toh pehle Live Chart mein le aaye aur modal open kare ("ander hu ja kar open hu")
    activatePanel("livechart");
    
    if (modal && backdrop) {
      modal.classList.remove("hidden");
      backdrop.classList.remove("hidden");
      if (searchInput) {
        searchInput.value = "";
        searchInput.focus();
        filterIndicators("");
      }
    }
  }

  function closeIndicatorModal() {
    if (modal && backdrop) {
      modal.classList.add("hidden");
      backdrop.classList.add("hidden");
    }
  }

  function filterIndicators(query) {
    const q = query.toLowerCase().trim();
    if (!indicatorList) return;
    const items = indicatorList.querySelectorAll(".indicator-item");
    const categories = indicatorList.querySelectorAll(".indicator-category");
    
    items.forEach(item => {
      const text = item.textContent.toLowerCase();
      const match = text.includes(q);
      item.style.display = match ? "flex" : "none";
    });

    // Hide empty category headers if all children are hidden
    categories.forEach(cat => {
      let nextElem = cat.nextElementSibling;
      let hasVisible = false;
      while (nextElem && !nextElem.classList.contains("indicator-category")) {
        if (nextElem.style.display !== "none") {
          hasVisible = true;
          break;
        }
        nextElem = nextElem.nextElementSibling;
      }
      cat.style.display = hasVisible ? "block" : "none";
    });
  }

  if (indicatorsBtn) {
    indicatorsBtn.addEventListener("click", openIndicatorModal);
  }
  if (closeBtn) closeBtn.addEventListener("click", closeIndicatorModal);
  if (backdrop) backdrop.addEventListener("click", closeIndicatorModal);
  
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      filterIndicators(e.target.value);
    });
  }

  // Handle toggling indicators checkbox actions
  if (indicatorList) {
    indicatorList.querySelectorAll("input[type='checkbox']").forEach(chk => {
      chk.addEventListener("change", (e) => {
        const indName = e.target.dataset.indicator;
        const active = e.target.checked;
        console.log(`Indicator ${indName} status changed:`, active);
        // Yahan ap technical indicator series add/remove kar sakte hain lightweight charts mein
      });
    });
  }
});
