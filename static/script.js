// ================================================================
// SIGNAL/FM — 19 CHANNEL MARKET READOUT
// Frontend Controller
// ================================================================


// ================================================================
// DOM ELEMENTS
// ================================================================

const coinSelect = document.getElementById("coin-select");
const runButton = document.getElementById("get-signal-btn");

const resultBox = document.getElementById("result-box");
const emptyState = document.getElementById("empty-state");

const errorText = document.getElementById("error-text");
const alertStack = document.getElementById("alert-stack");

const clockText = document.getElementById("clock-text");

const gaugeVerdict = document.getElementById("gauge-verdict");
const gaugeConfidence = document.getElementById("gauge-confidence");
const gaugeFill = document.getElementById("gauge-fill");

const heroPrice = document.getElementById("hero-price");
const heroRsi = document.getElementById("hero-rsi");
const heroMacd = document.getElementById("hero-macd");
const heroTrendTag = document.getElementById("hero-trend-tag");

const tier1 = document.getElementById("tier-1");
const tier2 = document.getElementById("tier-2");
const tier3 = document.getElementById("tier-3");


// ================================================================
// LIVE CLOCK
// ================================================================

function updateClock() {

    const now = new Date();

    const hours = String(now.getHours()).padStart(2, "0");
    const minutes = String(now.getMinutes()).padStart(2, "0");
    const seconds = String(now.getSeconds()).padStart(2, "0");

    if (clockText) {
        clockText.textContent =
            `${hours}:${minutes}:${seconds}`;
    }
}


// Update every second
setInterval(updateClock, 1000);

updateClock();


// ================================================================
// HELPER — HIDE / SHOW
// ================================================================

function showElement(element) {

    if (!element) return;

    element.classList.remove("hidden");
}


function hideElement(element) {

    if (!element) return;

    element.classList.add("hidden");
}


// ================================================================
// ERROR HANDLING
// ================================================================

function showError(message) {

    if (!errorText) return;

    errorText.textContent = message;

    showElement(errorText);
}


function clearError() {

    if (!errorText) return;

    errorText.textContent = "";

    hideElement(errorText);
}


// ================================================================
// ALERT SYSTEM
// ================================================================

function clearAlerts() {

    if (!alertStack) return;

    alertStack.innerHTML = "";
}


function addAlert(message, type = "warning") {

    if (!alertStack) return;

    const alert = document.createElement("div");

    alert.className =
        `alert-banner ${type}`;

    alert.innerHTML = `
        <span class="alert-dot"></span>
        <span>${escapeHTML(message)}</span>
    `;

    alertStack.appendChild(alert);
}


// ================================================================
// SECURITY HELPER
// ================================================================

function escapeHTML(value) {

    if (value === null || value === undefined) {
        return "";
    }

    const div = document.createElement("div");

    div.textContent = String(value);

    return div.innerHTML;
}


// ================================================================
// NUMBER FORMATTER
// ================================================================

function formatNumber(value, decimals = 2) {

    if (
        value === null ||
        value === undefined ||
        value === "" ||
        Number.isNaN(Number(value))
    ) {
        return "--";
    }

    return Number(value).toFixed(decimals);
}


function formatPercent(value) {

    if (
        value === null ||
        value === undefined ||
        value === "" ||
        Number.isNaN(Number(value))
    ) {
        return "--";
    }

    return `${Number(value).toFixed(1)}%`;
}


// ================================================================
// VERDICT COLOR
// ================================================================

function getVerdictClass(verdict) {

    const value =
        String(verdict || "")
            .toUpperCase();

    if (
        value.includes("LONG") ||
        value.includes("BUY")
    ) {
        return "long";
    }

    if (
        value.includes("SHORT") ||
        value.includes("SELL")
    ) {
        return "short";
    }

    return "wait";
}


// ================================================================
// VERDICT COLOR APPLY
// ================================================================

function applyVerdictColor(element, verdict) {

    if (!element) return;

    element.classList.remove(
        "text-long",
        "text-short",
        "text-wait"
    );

    const type =
        getVerdictClass(verdict);

    if (type === "long") {

        element.classList.add(
            "text-long"
        );

    } else if (type === "short") {

        element.classList.add(
            "text-short"
        );

    } else {

        element.classList.add(
            "text-wait"
        );
    }
}


// ================================================================
// GAUGE
// ================================================================

function updateGauge(
    verdict,
    confidence
) {

    if (!gaugeFill) return;

    let confidenceNumber =
        Number(confidence);

    if (Number.isNaN(confidenceNumber)) {
        confidenceNumber = 0;
    }

    // Support both:
    // 0.75
    // 75
    if (confidenceNumber <= 1) {
        confidenceNumber *= 100;
    }

    confidenceNumber =
        Math.max(
            0,
            Math.min(
                100,
                confidenceNumber
            )
        );

    const circumference =
        2 * Math.PI * 48;

    const offset =
        circumference -
        (
            circumference *
            confidenceNumber /
            100
        );

    gaugeFill.style.strokeDasharray =
        circumference;

    gaugeFill.style.strokeDashoffset =
        offset;


    const type =
        getVerdictClass(verdict);


    if (type === "long") {

        gaugeFill.style.stroke =
            "var(--long)";

    } else if (type === "short") {

        gaugeFill.style.stroke =
            "var(--short)";

    } else {

        gaugeFill.style.stroke =
            "var(--wait)";
    }
}


// ================================================================
// HERO SECTION
// ================================================================

function updateHero(data) {

    const verdict =
        data.verdict ||
        data.final_verdict ||
        data.signal ||
        "WAIT";

    const confidence =
        data.confidence ??
        data.confidence_score ??
        0;

    const price =
        data.price ??
        data.mark_price ??
        data.current_price;

    const rsi =
        data.rsi;

    const macd =
        data.macd;

    const trend =
        data.trend ||
        data.market_trend ||
        "NEUTRAL";


    // Verdict

    if (gaugeVerdict) {

        gaugeVerdict.textContent =
            String(verdict).toUpperCase();

        applyVerdictColor(
            gaugeVerdict,
            verdict
        );
    }


    // Confidence

    if (gaugeConfidence) {

        let conf =
            Number(confidence);

        if (!Number.isNaN(conf)) {

            if (conf <= 1) {
                conf *= 100;
            }

            gaugeConfidence.textContent =
                `${conf.toFixed(1)}% CONFIDENCE`;

        } else {

            gaugeConfidence.textContent =
                "-- CONFIDENCE";
        }
    }


    // Price

    if (heroPrice) {

        if (
            price !== null &&
            price !== undefined
        ) {

            heroPrice.textContent =
                `$${formatNumber(price, 2)}`;

        } else {

            heroPrice.textContent =
                "$--";
        }
    }


    // RSI

    if (heroRsi) {

        heroRsi.textContent =
            formatNumber(rsi, 2);
    }


    // MACD

    if (heroMacd) {

        heroMacd.textContent =
            formatNumber(macd, 4);
    }


    // Trend

    if (heroTrendTag) {

        heroTrendTag.textContent =
            `${String(trend).toUpperCase()} · ${String(verdict).toUpperCase()} · ${formatPercent(confidence)}`;

        applyVerdictColor(
            heroTrendTag,
            verdict
        );
    }


    // Gauge

    updateGauge(
        verdict,
        confidence
    );
}


// ================================================================
// CHANNEL DATA
// ================================================================

const channels = [

    {
        id: "CH.01",
        title: "Hawkes Process",
        model: "INTENSITY",
        tier: 1
    },

    {
        id: "CH.02",
        title: "Bayesian Classifier",
        model: "PROBABILITY",
        tier: 1
    },

    {
        id: "CH.03",
        title: "Quantile Volatility",
        model: "VOLATILITY",
        tier: 1
    },

    {
        id: "CH.04",
        title: "Conformal Prediction",
        model: "CONFIDENCE",
        tier: 1
    },

    {
        id: "CH.05",
        title: "Fractional Kelly",
        model: "RISK",
        tier: 1
    },

    {
        id: "CH.06",
        title: "Order Flow",
        model: "MICROSTRUCTURE",
        tier: 2
    },

    {
        id: "CH.07",
        title: "Liquidity",
        model: "MICROSTRUCTURE",
        tier: 2
    },

    {
        id: "CH.08",
        title: "Momentum",
        model: "MICROSTRUCTURE",
        tier: 2
    },

    {
        id: "CH.09",
        title: "Volume",
        model: "MICROSTRUCTURE",
        tier: 2
    },

    {
        id: "CH.10",
        title: "Market Regime",
        model: "MICROSTRUCTURE",
        tier: 2
    },

    {
        id: "CH.11",
        title: "RSI Signal",
        model: "TECHNICAL",
        tier: 3
    },

    {
        id: "CH.12",
        title: "MACD Signal",
        model: "TECHNICAL",
        tier: 3
    },

    {
        id: "CH.13",
        title: "Trend Strength",
        model: "TECHNICAL",
        tier: 3
    },

    {
        id: "CH.14",
        title: "Volatility Regime",
        model: "EXTENDED",
        tier: 3
    },

    {
        id: "CH.15",
        title: "Dynamic Risk Agent",
        model: "RISK",
        tier: 3
    },

    {
        id: "CH.16",
        title: "Market Sentiment",
        model: "EXTENDED",
        tier: 3
    },

    {
        id: "CH.17",
        title: "Price Action",
        model: "EXTENDED",
        tier: 3
    },

    {
        id: "CH.18",
        title: "Regime Filter",
        model: "EXTENDED",
        tier: 3
    },

    {
        id: "CH.19",
        title: "Final Risk Score",
        model: "RISK",
        tier: 3
    }

];


// ================================================================
// FIND CHANNEL VALUE
// ================================================================

function findChannelData(data, channel) {

    const possibleKeys = [

        channel.id,

        channel.id.toLowerCase(),

        channel.title,

        channel.title
            .toLowerCase()
            .replaceAll(" ", "_"),

        channel.id
            .replace(".", "")
            .toLowerCase()

    ];


    for (const key of possibleKeys) {

        if (
            data[key] !== undefined
        ) {
            return data[key];
        }

        if (
            data.channels &&
            data.channels[key] !== undefined
        ) {
            return data.channels[key];
        }
    }


    return null;
}


// ================================================================
// CREATE CHANNEL CARD
// ================================================================

function createChannelCard(
    channel,
    value
) {

    const card =
        document.createElement("article");

    card.className =
        "channel-card";


    const valueObject =
        (
            value &&
            typeof value === "object"
        )
            ? value
            : {
                value: value
            };


    const mainValue =
        valueObject.value ??
        valueObject.signal ??
        valueObject.verdict ??
        valueObject.score ??
        "--";


    const detail =
        valueObject.detail ??
        valueObject.description ??
        valueObject.reason ??
        "";


    const badge =
        valueObject.badge ??
        valueObject.status ??
        "";


    const verdictClass =
        getVerdictClass(mainValue);


    let valueClass =
        "";


    if (verdictClass === "long") {
        valueClass = "text-long";
    }

    if (verdictClass === "short") {
        valueClass = "text-short";
    }

    if (verdictClass === "wait") {
        valueClass = "text-wait";
    }


    card.innerHTML = `

        <div class="channel-head">

            <div class="channel-id-group">

                <div class="scope-ticks">

                    <span style="height:5px"></span>
                    <span style="height:8px"></span>
                    <span style="height:11px"></span>

                </div>

                <div>

                    <div class="channel-id">
                        ${channel.id}
                    </div>

                    <div class="channel-title">
                        ${escapeHTML(channel.title)}
                    </div>

                </div>

            </div>


            <div class="channel-model">
                ${escapeHTML(channel.model)}
            </div>

        </div>


        <div class="channel-main ${valueClass}">
            ${escapeHTML(mainValue)}
        </div>


        ${
            badge
                ? `
                <div class="badge ${getBadgeClass(mainValue)}">
                    ${escapeHTML(badge)}
                </div>
                `
                : ""
        }


        ${
            detail
                ? `
                <div class="channel-detail">
                    ${escapeHTML(detail)}
                </div>
                `
                : ""
        }

    `;


    return card;
}


// ================================================================
// BADGE CLASS
// ================================================================

function getBadgeClass(value) {

    const type =
        getVerdictClass(value);

    if (type === "long") {
        return "on-long";
    }

    if (type === "short") {
        return "on-short";
    }

    if (type === "wait") {
        return "on-wait";
    }

    return "on-flat";
}


// ================================================================
// RENDER CHANNELS
// ================================================================

function renderChannels(data) {

    if (!tier1 || !tier2 || !tier3) {
        return;
    }


    tier1.innerHTML = "";
    tier2.innerHTML = "";
    tier3.innerHTML = "";


    channels.forEach(channel => {

        const value =
            findChannelData(
                data,
                channel
            );


        const card =
            createChannelCard(
                channel,
                value
            );


        if (channel.tier === 1) {

            tier1.appendChild(card);

        } else if (channel.tier === 2) {

            tier2.appendChild(card);

        } else {

            tier3.appendChild(card);
        }

    });

}


// ================================================================
// EXTRA DATA → ALERTS
// ================================================================

function renderAlerts(data) {

    clearAlerts();


    const alerts =
        data.alerts ||
        data.warnings ||
        [];


    if (
        Array.isArray(alerts)
    ) {

        alerts.forEach(
            message => {

                addAlert(
                    message,
                    "warning"
                );

            }
        );
    }


    if (
        data.risk_alert
    ) {

        addAlert(
            data.risk_alert,
            "danger"
        );
    }

}


// ================================================================
// MAIN ANALYSIS
// ================================================================

async function runAnalysis() {

    clearError();

    clearAlerts();


    const symbol =
        coinSelect
            ? coinSelect.value
            : "BTC/USDT";


    // Button loading state

    if (runButton) {

        runButton.disabled = true;

        runButton.textContent =
            "ANALYZING...";
    }


    try {

        const response =
            await fetch(
                "/analyze",
                {

                    method: "POST",

                    headers: {

                        "Content-Type":
                            "application/json"

                    },

                    body: JSON.stringify({

                        symbol: symbol

                    })

                }
            );


        if (!response.ok) {

            throw new Error(
                `Server error: ${response.status}`
            );
        }


        const data =
            await response.json();


        console.log(
            "Analysis response:",
            data
        );


        // Check backend error

        if (
            data.error
        ) {

            throw new Error(
                data.error
            );
        }


        // Show dashboard

        hideElement(emptyState);

        showElement(resultBox);


        // Update sections

        updateHero(data);

        renderChannels(data);

        renderAlerts(data);


    } catch (error) {

        console.error(
            "Analysis failed:",
            error
        );


        showError(
            error.message ||
            "Unable to run market analysis."
        );


        hideElement(resultBox);

        showElement(emptyState);


    } finally {

        if (runButton) {

            runButton.disabled = false;

            runButton.textContent =
                "RUN ANALYSIS";
        }

    }

}


// ================================================================
// BUTTON EVENT
// ================================================================

if (runButton) {

    runButton.addEventListener(
        "click",
        runAnalysis
    );

}


// ================================================================
// ENTER KEY SUPPORT
// ================================================================

if (coinSelect) {

    coinSelect.addEventListener(
        "keydown",
        event => {

            if (
                event.key === "Enter"
            ) {

                runAnalysis();
            }

        }
    );

}


// ================================================================
// INITIAL STATE
// ================================================================

hideElement(resultBox);

showElement(emptyState);

clearError();

clearAlerts();
