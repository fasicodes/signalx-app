/*
Trading Signal Frontend - Compatible with main.py v8

Backend endpoints:
GET /signal
GET /coins
GET /candles
GET /liquidity

IMPORTANT:
This file uses GET /signal.
It does NOT call POST /analyze.
*/

document.addEventListener("DOMContentLoaded", () => {
// ============================================================
// CONFIG
// ============================================================

```
const API_BASE = "";

let currentCoin = "BTC/USDT";
let currentTimeframe = "1h";
let analysisRunning = false;
let candleTimer = null;
let liquidityTimer = null;

// ============================================================
// HELPER FUNCTIONS
// ============================================================

function $(id) {
    return document.getElementById(id);
}

function findElement(...ids) {
    for (const id of ids) {
        const el = $(id);
        if (el) return el;
    }
    return null;
}

function setText(ids, value) {
    const el = findElement(...ids);
    if (el) {
        el.textContent = value ?? "--";
    }
}

function showElement(ids, show = true) {
    const el = findElement(...ids);
    if (el) {
        el.style.display = show ? "" : "none";
    }
}

function formatNumber(value, decimals = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
        return "--";
    }

    return Number(value).toLocaleString("en-US", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
    });
}

function formatPercent(value, decimals = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
        return "--";
    }

    return `${formatNumber(value, decimals)}%`;
}

function formatPrice(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
        return "--";
    }

    const number = Number(value);

    if (number >= 1000) return number.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });

    if (number >= 1) return number.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 4
    });

    return number.toLocaleString("en-US", {
        minimumFractionDigits: 4,
        maximumFractionDigits: 8
    });
}

function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value ?? "";
    return div.innerHTML;
}

function setLoading(message = "Analyzing market...") {
    const el = findElement(
        "analysisStatus",
        "status",
        "loadingMessage",
        "analysis-status"
    );

    if (el) {
        el.textContent = message;
    }
}

function setError(message) {
    const el = findElement(
        "analysisStatus",
        "status",
        "errorMessage",
        "analysis-status"
    );

    if (el) {
        el.textContent = message;
    }

    console.error(message);
}

// ============================================================
// GET SELECTED COIN
// ============================================================

function getCoin() {
    const select = findElement(
        "coinSelect",
        "coin",
        "symbol",
        "coin-selector"
    );

    if (select && select.value) {
        return select.value;
    }

    return currentCoin;
}

function getTimeframe() {
    const select = findElement(
        "timeframeSelect",
        "timeframe",
        "timeFrame",
        "timeframe-selector"
    );

    if (select && select.value) {
        return select.value;
    }

    return currentTimeframe;
}

// ============================================================
// LOAD COINS
// ============================================================

async function loadCoins() {
    try {
        const response = await fetch(`${API_BASE}/coins`);

        if (!response.ok) {
            throw new Error(`Coins API error: ${response.status}`);
        }

        const coins = await response.json();

        const select = findElement(
            "coinSelect",
            "coin",
            "symbol",
            "coin-selector"
        );

        if (!select || !Array.isArray(coins)) {
            return;
        }

        const oldValue = select.value;

        select.innerHTML = "";

        coins.forEach(coin => {
            const option = document.createElement("option");
            option.value = coin;
            option.textContent = coin;
            select.appendChild(option);
        });

        if (coins.includes(oldValue)) {
            select.value = oldValue;
        } else if (coins.includes("BTC/USDT")) {
            select.value = "BTC/USDT";
        }

        currentCoin = select.value;

    } catch (error) {
        console.error("Failed to load coins:", error);
    }
}

// ============================================================
// MAIN ANALYSIS
// IMPORTANT:
// OLD:
// POST /analyze
//
// NEW:
// GET /signal?coin=BTC/USDT&timeframe=1h
// ============================================================

async function runAnalysis() {

    if (analysisRunning) {
        return;
    }

    analysisRunning = true;

    currentCoin = getCoin();
    currentTimeframe = getTimeframe();

    setLoading("Analyzing market data...");

    try {

        const url =
            `${API_BASE}/signal` +
            `?coin=${encodeURIComponent(currentCoin)}` +
            `&timeframe=${encodeURIComponent(currentTimeframe)}` +
            `&orderbook=true`;

        console.log("Calling Signal API:", url);

        const response = await fetch(url, {
            method: "GET",
            headers: {
                "Accept": "application/json"
            }
        });

        const data = await response.json();

        if (!response.ok || data.error) {
            throw new Error(
                data.error || `Server error: ${response.status}`
            );
        }

        console.log("Analysis result:", data);

        updateSignalUI(data);

        setLoading(
            `Analysis completed: ${data.coin || currentCoin} • ${data.timeframe || currentTimeframe}`
        );

    } catch (error) {

        console.error("Analysis failed:", error);

        setError(`Analysis failed: ${error.message}`);

        alert(`Analysis failed:\n${error.message}`);

    } finally {
        analysisRunning = false;
    }
}

// ============================================================
// UPDATE MAIN SIGNAL UI
// ============================================================

function updateSignalUI(data) {

    // Basic information
    setText(["coinName", "selectedCoin", "currentCoin"], data.coin);
    setText(["timeframeValue", "currentTimeframe"], data.timeframe);

    setText(
        ["lastPrice", "currentPrice", "price"],
        formatPrice(data.last_price)
    );

    setText(
        ["trend", "trendValue"],
        data.trend || "--"
    );

    // Hawkes
    setText(
        ["buyingPressure", "buyPressure"],
        formatNumber(data.buying_pressure, 1)
    );

    setText(
        ["sellingPressure", "sellPressure"],
        formatNumber(data.selling_pressure, 1)
    );

    // Bayesian
    setText(
        ["bullishPct", "bullishProbability", "bullish"],
        formatPercent(data.bullish_pct, 1)
    );

    setText(
        ["bearishPct", "bearishProbability", "bearish"],
        formatPercent(data.bearish_pct, 1)
    );

    // Conformal
    setText(
        ["confidencePct", "confidence", "confidenceValue"],
        formatPercent(data.confidence_pct, 1)
    );

    // Kelly
    setText(
        ["suggestedRisk", "riskPct", "risk"],
        formatPercent(data.suggested_risk_pct, 2)
    );

    // RSI
    setText(
        ["rsi", "rsiValue"],
        formatNumber(data.rsi, 2)
    );

    // MACD
    setText(
        ["macd", "macdValue"],
        formatNumber(data.macd, 4)
    );

    // Quantile Volatility
    setText(
        ["expectedVolatility", "expectedMove"],
        formatPercent(data.expected_volatility_pct, 2)
    );

    setText(
        ["extremeVolatility", "extremeMove"],
        formatPercent(data.extreme_volatility_95_pct, 2)
    );

    setText(
        ["stopLoss", "sl"],
        formatPrice(data.stop_loss)
    );

    setText(
        ["takeProfit", "tp"],
        formatPrice(data.take_profit)
    );

    // Final Verdict
    updateVerdict(data.final_verdict);

    // Extra panels
    updateOrderFlow(data.order_flow);
    updateToxicFlow(data.toxic_flow);
    updateRegime(data.market_regime);
    updateJump(data.jump_shock);
    updateMeta(data.meta_label);

    updateDivergence(data.intermarket_divergence);
    updateEntropy(data.entropy);
    updateDepth(data.depth_profile);
    updateVWAP(data.vwap_deviation);
    updateRL(data.rl_risk_agent);
    updateHurst(data.hurst);
    updateWavelet(data.wavelet_trend);
    updateStructuralBreak(data.structural_break);
    updateLiquiditySweep(data.liquidity_sweep);

    // Fake breakout
    setText(
        ["fakeBreakout", "fakeBreakoutWarning"],
        data.fake_breakout_warning ? "WARNING" : "NO WARNING"
    );

    const fakeEl = findElement(
        "fakeBreakout",
        "fakeBreakoutWarning"
    );

    if (fakeEl) {
        fakeEl.classList.toggle(
            "warning",
            Boolean(data.fake_breakout_warning)
        );
    }
}

// ============================================================
// FINAL VERDICT
// ============================================================

function updateVerdict(verdict) {

    const verdictEl = findElement(
        "finalVerdict",
        "verdict",
        "signal",
        "tradeSignal"
    );

    if (!verdictEl) {
        return;
    }

    verdictEl.textContent = verdict || "WAIT";

    verdictEl.classList.remove(
        "long",
        "short",
        "wait",
        "bullish",
        "bearish"
    );

    if (verdict === "LONG") {
        verdictEl.classList.add("long");
    } else if (verdict === "SHORT") {
        verdictEl.classList.add("short");
    } else {
        verdictEl.classList.add("wait");
    }
}

// ============================================================
// EXTRA PANEL UPDATES
// ============================================================

function updateOrderFlow(data) {
    if (!data) return;

    setText(
        ["ofiScore", "orderFlowScore"],
        formatNumber(data.ofi_score, 2)
    );

    setText(
        ["ofiRaw", "orderFlowRaw"],
        formatNumber(data.ofi_raw, 4)
    );
}

function updateToxicFlow(data) {
    if (!data) return;

    setText(
        ["vpinScore", "vpin"],
        formatNumber(data.vpin_score, 3)
    );

    setText(
        ["toxicity", "toxicityValue"],
        data.toxicity || "--"
    );
}

function updateRegime(data) {
    if (!data) return;

    setText(
        ["marketRegime", "regime"],
        data.regime || "--"
    );

    setText(
        ["hmmState", "state"],
        data.state ?? "--"
    );

    setText(
        ["stateMeanReturn"],
        data.state_mean_return_pct !== undefined
            ? formatPercent(data.state_mean_return_pct, 3)
            : "--"
    );
}

function updateJump(data) {
    if (!data) return;

    setText(
        ["jumpDetected", "jump"],
        data.jump_detected ? "YES" : "NO"
    );

    setText(
        ["jumpZscore"],
        formatNumber(data.jump_zscore, 2)
    );

    setText(
        ["jumpDirection"],
        data.jump_direction || "--"
    );
}

function updateMeta(data) {
    if (!data) return;

    setText(
        ["metaWinProbability", "metaProbability"],
        data.meta_win_probability !== null &&
        data.meta_win_probability !== undefined
            ? formatPercent(data.meta_win_probability, 1)
            : "--"
    );

    setText(
        ["metaDecision"],
        data.meta_decision || "--"
    );
}

function updateDivergence(data) {
    if (!data) return;

    setText(
        ["divergenceScore"],
        formatNumber(data.divergence_score, 3)
    );

    setText(
        ["benchmark"],
        data.benchmark || "--"
    );

    setText(
        ["divergenceInterpretation"],
        data.interpretation || "--"
    );
}

function updateEntropy(data) {
    if (!data) return;

    setText(
        ["entropyAverage", "entropy"],
        formatNumber(data.entropy_avg, 3)
    );

    setText(
        ["entropyRegime"],
        data.regime || "--"
    );
}

function updateDepth(data) {
    if (!data) return;

    setText(
        ["depthSlope"],
        formatNumber(data.depth_slope, 4)
    );

    setText(
        ["wallBias"],
        data.wall_bias || "--"
    );

    setText(
        ["depthLevels"],
        data.depth_levels_used ?? "--"
    );
}

function updateVWAP(data) {
    if (!data) return;

    setText(
        ["vwapDeviation", "vwapZ"],
        formatNumber(data.vwap_deviation_z, 2)
    );

    setText(
        ["vwapSignal"],
        data.signal || "--"
    );

    setText(
        ["toxicReversionFlag"],
        data.toxic_reversion_flag ? "YES" : "NO"
    );
}

function updateRL(data) {
    if (!data) return;

    setText(
        ["rlState"],
        data.rl_state || "--"
    );

    setText(
        ["rlMultiplier"],
        data.rl_risk_multiplier !== undefined
            ? `${data.rl_risk_multiplier}x`
            : "--"
    );

    setText(
        ["rlAdjustedRisk"],
        formatPercent(data.rl_adjusted_risk_pct, 2)
    );
}

function updateHurst(data) {
    if (!data) return;

    setText(
        ["hurstValue", "hurst"],
        formatNumber(data.hurst, 3)
    );

    setText(
        ["hurstMemory"],
        data.memory || "--"
    );
}

function updateWavelet(data) {
    if (!data) return;

    setText(
        ["waveletDirection"],
        data.wavelet_trend_direction || "--"
    );

    setText(
        ["waveletSlope"],
        formatNumber(data.wavelet_trend_slope, 4)
    );

    setText(
        ["waveletLast"],
        formatPrice(data.wavelet_denoised_last)
    );
}

function updateStructuralBreak(data) {
    if (!data) return;

    setText(
        ["structuralBreak"],
        data.structural_break ? "DETECTED" : "NO BREAK"
    );

    setText(
        ["cusumPositive"],
        formatNumber(data.cusum_pos, 6)
    );

    setText(
        ["cusumNegative"],
        formatNumber(data.cusum_neg, 6)
    );

    setText(
        ["recentBreakCount"],
        data.recent_break_count ?? "--"
    );
}

function updateLiquiditySweep(data) {
    if (!data) return;

    setText(
        ["swingHigh"],
        formatPrice(data.swing_high)
    );

    setText(
        ["swingLow"],
        formatPrice(data.swing_low)
    );

    setText(
        ["distanceToHigh"],
        formatPercent(data.distance_to_high_pct, 2)
    );

    setText(
        ["distanceToLow"],
        formatPercent(data.distance_to_low_pct, 2)
    );

    setText(
        ["liquiditySweep"],
        data.liquidity_sweep_detected
            ? "DETECTED"
            : "NONE"
    );

    setText(
        ["sweepDirection"],
        data.sweep_direction || "--"
    );
}

// ============================================================
// LIQUIDITY SCANNER
// GET /liquidity
// ============================================================

async function loadLiquidity() {

    const coin = getCoin();

    try {

        const url =
            `${API_BASE}/liquidity` +
            `?coin=${encodeURIComponent(coin)}`;

        console.log("Calling Liquidity API:", url);

        const response = await fetch(url, {
            method: "GET",
            headers: {
                "Accept": "application/json"
            }
        });

        const data = await response.json();

        if (!response.ok || data.error) {
            throw new Error(
                data.error || `Liquidity server error: ${response.status}`
            );
        }

        updateLiquidityUI(data);

    } catch (error) {

        console.error("Liquidity scanner failed:", error);

        setText(
            ["liquidityStatus"],
            `Error: ${error.message}`
        );
    }
}

function updateLiquidityUI(data) {

    setText(
        ["liquidityPrice", "scannerPrice"],
        formatPrice(data.price)
    );

    setText(
        ["high24h"],
        formatPrice(data.high_24h)
    );

    setText(
        ["low24h"],
        formatPrice(data.low_24h)
    );

    setText(
        ["volume24h"],
        data.volume_usd_24h !== null
            ? `$${formatNumber(data.volume_usd_24h, 0)}`
            : "--"
    );

    setText(
        ["change24h"],
        formatPercent(data.change_pct_24h, 2)
    );

    setText(
        ["markPrice"],
        formatPrice(data.mark_price)
    );

    setText(
        ["fundingRate"],
        formatPercent(data.funding_rate_pct, 4)
    );

    setText(
        ["openInterest"],
        data.open_interest_usd !== null
            ? `$${formatNumber(data.open_interest_usd, 0)}`
            : "--"
    );

    setText(
        ["buyPct", "liquidityBuyPct"],
        formatPercent(data.buy_pct, 2)
    );

    setText(
        ["sellPct", "liquiditySellPct"],
        formatPercent(data.sell_pct, 2)
    );

    setText(
        ["liquidityBias", "biasTag"],
        data.bias_tag || "--"
    );

    // Bid wall
    if (data.bid_wall) {

        setText(
            ["bidWallPrice"],
            formatPrice(data.bid_wall.price)
        );

        setText(
            ["bidWallQty"],
            formatNumber(data.bid_wall.qty, 4)
        );

        setText(
            ["bidWallNotional"],
            `$${formatNumber(data.bid_wall.notional, 0)}`
        );

    } else {

        setText(["bidWallPrice"], "--");
        setText(["bidWallQty"], "--");
        setText(["bidWallNotional"], "--");
    }

    // Ask wall
    if (data.ask_wall) {

        setText(
            ["askWallPrice"],
            formatPrice(data.ask_wall.price)
        );

        setText(
            ["askWallQty"],
            formatNumber(data.ask_wall.qty, 4)
        );

        setText(
            ["askWallNotional"],
            `$${formatNumber(data.ask_wall.notional, 0)}`
        );

    } else {

        setText(["askWallPrice"], "--");
        setText(["askWallQty"], "--");
        setText(["askWallNotional"], "--");
    }

    // Spoofing
    const spoofCount = Array.isArray(data.spoof_flags)
        ? data.spoof_flags.length
        : 0;

    setText(
        ["spoofingStatus", "spoofStatus"],
        spoofCount > 0
            ? `POSSIBLE SPOOFING (${spoofCount})`
            : "NO FLAG"
    );
}

// ============================================================
// LIVE CANDLES
// GET /candles
//
// This function updates simple chart data if your HTML has
// a canvas element with id="liveChart".
// ============================================================

let chartInstance = null;

async function loadCandles() {

    const coin = getCoin();
    const timeframe = getTimeframe();

    try {

        const url =
            `${API_BASE}/candles` +
            `?coin=${encodeURIComponent(coin)}` +
            `&timeframe=${encodeURIComponent(timeframe)}` +
            `&limit=200`;

        const response = await fetch(url);

        const data = await response.json();

        if (!response.ok || data.error) {
            throw new Error(
                data.error || `Candles error: ${response.status}`
            );
        }

        updateChart(data);

        setText(
            ["chartPrice", "livePrice"],
            formatPrice(data.last_price)
        );

        setText(
            ["chartChange", "liveChange"],
            formatPercent(data.change_pct, 3)
        );

    } catch (error) {

        console.error("Candles failed:", error);

    }
}

function updateChart(data) {

    const canvas = $("liveChart");

    if (!canvas) {
        return;
    }

    if (typeof Chart === "undefined") {
        console.warn(
            "Chart.js is not loaded. Add Chart.js before script.js."
        );
        return;
    }

    const candles = data.candles || [];

    const labels = candles.map(candle => {
        return new Date(candle.time * 1000).toLocaleString(
            "en-US",
            {
                month: "short",
                day: "numeric",
                hour: "2-digit",
                minute: "2-digit"
            }
        );
    });

    const prices = candles.map(candle => candle.close);

    if (chartInstance) {
        chartInstance.data.labels = labels;
        chartInstance.data.datasets[0].data = prices;
        chartInstance.update("none");
        return;
    }

    chartInstance = new Chart(canvas, {
        type: "line",

        data: {
            labels: labels,

            datasets: [
                {
                    label: `${data.coin} Close Price`,
                    data: prices,
                    borderWidth: 2,
                    pointRadius: 0,
                    tension: 0.2,
                    fill: false
                }
            ]
        },

        options: {
            responsive: true,

            maintainAspectRatio: false,

            interaction: {
                intersect: false,
                mode: "index"
            },

            plugins: {
                legend: {
                    display: true
                }
            },

            scales: {
                x: {
                    ticks: {
                        maxTicksLimit: 10
                    }
                },

                y: {
                    beginAtZero: false
                }
            }
        }
    });
}

// ============================================================
// AUTO REFRESH
// ============================================================

function startAutoRefresh() {

    stopAutoRefresh();

    // Candles refresh every 10 seconds
    candleTimer = setInterval(() => {
        loadCandles();
    }, 10000);

    // Liquidity refresh every 10 seconds
    liquidityTimer = setInterval(() => {
        loadLiquidity();
    }, 10000);
}

function stopAutoRefresh() {

    if (candleTimer) {
        clearInterval(candleTimer);
        candleTimer = null;
    }

    if (liquidityTimer) {
        clearInterval(liquidityTimer);
        liquidityTimer = null;
    }
}

// ============================================================
// BUTTON EVENTS
// ============================================================

const analyzeButton = findElement(
    "analyzeBtn",
    "analyzeButton",
    "runAnalysis",
    "run-analysis",
    "analyze"
);

if (analyzeButton) {

    analyzeButton.addEventListener("click", () => {

        runAnalysis();
        loadCandles();
        loadLiquidity();

    });

} else {

    console.warn(
        "Analysis button not found. Expected id: analyzeBtn"
    );
}

// ============================================================
// COIN CHANGE
// ============================================================

const coinSelect = findElement(
    "coinSelect",
    "coin",
    "symbol",
    "coin-selector"
);

if (coinSelect) {

    coinSelect.addEventListener("change", () => {

        currentCoin = coinSelect.value;

        loadCandles();
        loadLiquidity();

    });

}

// ============================================================
// TIMEFRAME CHANGE
// ============================================================

const timeframeSelect = findElement(
    "timeframeSelect",
    "timeframe",
    "timeFrame",
    "timeframe-selector"
);

if (timeframeSelect) {

    timeframeSelect.addEventListener("change", () => {

        currentTimeframe = timeframeSelect.value;

        loadCandles();

    });

}

// ============================================================
// TAB CHANGE SUPPORT
// ============================================================

document.addEventListener("click", event => {

    const target = event.target.closest(
        "[data-tab], .tab-button, .tab-btn"
    );

    if (!target) {
        return;
    }

    const tabName =
        target.dataset.tab ||
        target.dataset.target ||
        target.getAttribute("data-tab");

    if (
        tabName &&
        tabName.toLowerCase().includes("liquidity")
    ) {
        loadLiquidity();
    }

    if (
        tabName &&
        tabName.toLowerCase().includes("chart")
    ) {
        loadCandles();
    }
});

// ============================================================
// INITIALIZE APP
// ============================================================

async function initialize() {

    console.log("Trading Signal Frontend v8 starting...");

    await loadCoins();

    currentCoin = getCoin();
    currentTimeframe = getTimeframe();

    console.log("Selected coin:", currentCoin);
    console.log("Selected timeframe:", currentTimeframe);

    // Initial data
    loadCandles();
    loadLiquidity();

    // Start live updates
    startAutoRefresh();

    console.log("Trading Signal Frontend initialized.");
}

initialize();

// ============================================================
// EXPOSE FUNCTIONS FOR HTML onclick=""
// ============================================================

window.runAnalysis = runAnalysis;
window.loadLiquidity = loadLiquidity;
window.loadCandles = loadCandles;
window.loadCoins = loadCoins;
```

});
