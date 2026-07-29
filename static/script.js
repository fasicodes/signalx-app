/*
SIGNAL/FM — Complete Frontend JS
Compatible with:

* HTML provided by user
* GET /signal
* GET /coins
* GET /candles
* GET /liquidity
* Lightweight Charts 4.1.3

IMPORTANT:
This file does NOT call POST /analyze.
*/

document.addEventListener("DOMContentLoaded", () => {

```
// ============================================================
// CONFIG
// ============================================================

const API_BASE = "";

let currentCoin = "BTC/USDT";
let currentTimeframe = "1h";

let analysisRunning = false;
let candleTimer = null;
let liquidityTimer = null;

let candleChart = null;
let candleSeries = null;

// ============================================================
// HELPERS
// ============================================================

function $(id) {
    return document.getElementById(id);
}

function formatNumber(value, decimals = 2) {
    if (
        value === null ||
        value === undefined ||
        value === "" ||
        Number.isNaN(Number(value))
    ) {
        return "--";
    }

    return Number(value).toLocaleString("en-US", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
    });
}

function formatPercent(value, decimals = 2) {
    if (
        value === null ||
        value === undefined ||
        value === "" ||
        Number.isNaN(Number(value))
    ) {
        return "--";
    }

    return `${formatNumber(value, decimals)}%`;
}

function formatPrice(value) {
    if (
        value === null ||
        value === undefined ||
        value === "" ||
        Number.isNaN(Number(value))
    ) {
        return "--";
    }

    const number = Number(value);

    if (number >= 1000) {
        return number.toLocaleString("en-US", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    if (number >= 1) {
        return number.toLocaleString("en-US", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 4
        });
    }

    return number.toLocaleString("en-US", {
        minimumFractionDigits: 4,
        maximumFractionDigits: 8
    });
}

function setText(id, value) {
    const element = $(id);

    if (element) {
        element.textContent =
            value === null ||
            value === undefined ||
            value === ""
                ? "--"
                : value;
    }
}

function show(id) {
    const element = $(id);

    if (element) {
        element.classList.remove("hidden");
    }
}

function hide(id) {
    const element = $(id);

    if (element) {
        element.classList.add("hidden");
    }
}

function showError(message) {
    const error = $("error-text");

    if (error) {
        error.textContent = message;
        error.classList.remove("hidden");
    }

    console.error(message);
}

function clearError() {
    const error = $("error-text");

    if (error) {
        error.textContent = "";
        error.classList.add("hidden");
    }
}

// ============================================================
// CLOCK
// ============================================================

function updateClock() {
    const clock = $("clock-text");

    if (!clock) return;

    const now = new Date();

    clock.textContent = now.toLocaleTimeString("en-US", {
        hour12: false
    });
}

updateClock();
setInterval(updateClock, 1000);

// ============================================================
// GET SELECTED COIN
// ============================================================

function getCoin() {
    const select = $("coin-select");

    if (select && select.value) {
        return select.value;
    }

    return currentCoin;
}

// ============================================================
// GET TIMEFRAME
// ============================================================

function getTimeframe() {
    return currentTimeframe;
}

// ============================================================
// COIN LOGO
// ============================================================

function getCoinSymbol(coin) {
    if (!coin) return "BTC";

    return coin
        .split("/")[0]
        .trim()
        .toLowerCase();
}

function getCoinIcon(coin) {
    const symbol = getCoinSymbol(coin);

    return `https://raw.githubusercontent.com/spothq/cryptocurrency-icons/master/128/color/${symbol}.png`;
}

// ============================================================
// CUSTOM COIN PICKER
// ============================================================

function initializeCoinPicker() {

    const select = $("coin-select");
    const picker = $("coin-picker");
    const trigger = $("coin-picker-trigger");
    const menu = $("coin-picker-menu");
    const icon = $("coin-picker-icon");
    const label = $("coin-picker-label");

    if (
        !select ||
        !picker ||
        !trigger ||
        !menu
    ) {
        console.warn("Custom coin picker elements not found.");
        return;
    }

    function updatePicker() {

        const selectedOption =
            select.options[select.selectedIndex];

        if (!selectedOption) return;

        const coin = selectedOption.value;

        currentCoin = coin;

        if (label) {
            label.textContent =
                coin.replace("/", " / ");
        }

        if (icon) {
            icon.src = getCoinIcon(coin);

            icon.onerror = () => {
                icon.style.display = "none";
            };

            icon.style.display = "";
        }

        menu
            .querySelectorAll(".coin-picker-option")
            .forEach(option => {
                option.classList.toggle(
                    "active",
                    option.dataset.value === coin
                );
            });
    }

    function buildMenu() {

        menu.innerHTML = "";

        const groups = {};

        Array.from(select.children).forEach(child => {

            if (child.tagName === "OPTGROUP") {

                const groupName =
                    child.label || "COINS";

                groups[groupName] =
                    Array.from(child.options);

            } else if (child.tagName === "OPTION") {

                if (!groups["COINS"]) {
                    groups["COINS"] = [];
                }

                groups["COINS"].push(child);
            }
        });

        Object.entries(groups).forEach(
            ([groupName, options]) => {

                const groupLabel =
                    document.createElement("div");

                groupLabel.className =
                    "coin-picker-group-label";

                groupLabel.textContent =
                    groupName;

                menu.appendChild(groupLabel);

                options.forEach(option => {

                    const button =
                        document.createElement("button");

                    button.type = "button";

                    button.className =
                        "coin-picker-option";

                    button.dataset.value =
                        option.value;

                    const coinIcon =
                        document.createElement("img");

                    coinIcon.className =
                        "coin-picker-option-icon";

                    coinIcon.alt = "";

                    coinIcon.src =
                        getCoinIcon(option.value);

                    coinIcon.onerror = () => {
                        coinIcon.style.display = "none";
                    };

                    const text =
                        document.createElement("span");

                    text.textContent =
                        option.textContent;

                    button.appendChild(coinIcon);
                    button.appendChild(text);

                    button.addEventListener(
                        "click",
                        () => {

                            select.value =
                                option.value;

                            currentCoin =
                                option.value;

                            select.dispatchEvent(
                                new Event("change")
                            );

                            updatePicker();

                            picker.classList.remove(
                                "open"
                            );

                            trigger.setAttribute(
                                "aria-expanded",
                                "false"
                            );
                        }
                    );

                    menu.appendChild(button);
                });
            }
        );

        updatePicker();
    }

    trigger.addEventListener(
        "click",
        () => {

            const isOpen =
                picker.classList.toggle("open");

            trigger.setAttribute(
                "aria-expanded",
                String(isOpen)
            );
        }
    );

    document.addEventListener(
        "click",
        event => {

            if (!picker.contains(event.target)) {

                picker.classList.remove(
                    "open"
                );

                trigger.setAttribute(
                    "aria-expanded",
                    "false"
                );
            }
        }
    );

    select.addEventListener(
        "change",
        () => {

            currentCoin =
                select.value;

            updatePicker();

            loadLiquidity();
            loadCandles();
        }
    );

    buildMenu();
}

// ============================================================
// LOAD COINS
// ============================================================

async function loadCoins() {

    const select = $("coin-select");

    if (!select) return;

    try {

        const response =
            await fetch(
                `${API_BASE}/coins`
            );

        if (!response.ok) {
            throw new Error(
                `Coins API error: ${response.status}`
            );
        }

        const coins =
            await response.json();

        if (!Array.isArray(coins)) {
            return;
        }

        const oldValue =
            select.value;

        select.innerHTML = "";

        coins.forEach(coin => {

            const option =
                document.createElement("option");

            option.value = coin;

            option.textContent =
                coin.replace("/", " / ");

            select.appendChild(option);
        });

        if (coins.includes(oldValue)) {

            select.value =
                oldValue;

        } else if (
            coins.includes("BTC/USDT")
        ) {

            select.value =
                "BTC/USDT";
        }

        currentCoin =
            select.value;

    } catch (error) {

        console.warn(
            "Could not load coins from backend:",
            error
        );

        // HTML already has coin options.
        // So app can continue working.
        currentCoin =
            select.value ||
            "BTC/USDT";
    }
}

// ============================================================
// RUN MAIN ANALYSIS
// GET /signal
// ============================================================

async function runAnalysis() {

    if (analysisRunning) {
        return;
    }

    analysisRunning = true;

    clearError();

    const button =
        $("get-signal-btn");

    const buttonText =
        button
            ? button.querySelector(
                ".scan-btn-text"
            )
            : null;

    if (button) {
        button.disabled = true;
    }

    if (buttonText) {
        buttonText.textContent =
            "ANALYZING...";
    }

    currentCoin =
        getCoin();

    show("result-box");
    hide("empty-state");

    try {

        const url =
            `${API_BASE}/signal` +
            `?coin=${encodeURIComponent(
                currentCoin
            )}` +
            `&timeframe=${encodeURIComponent(
                currentTimeframe
            )}` +
            `&orderbook=true`;

        console.log(
            "Calling Signal API:",
            url
        );

        const response =
            await fetch(url, {
                method: "GET",
                headers: {
                    "Accept":
                        "application/json"
                }
            });

        const data =
            await response.json();

        if (
            !response.ok ||
            data.error
        ) {
            throw new Error(
                data.error ||
                `Server error: ${response.status}`
            );
        }

        console.log(
            "Signal result:",
            data
        );

        updateMainUI(data);

        // Also load liquidity
        // immediately after analysis.
        await loadLiquidity();

        await loadCandles();

    } catch (error) {

        console.error(
            "Analysis failed:",
            error
        );

        showError(
            `Analysis failed: ${error.message}`
        );

    } finally {

        analysisRunning =
            false;

        if (button) {
            button.disabled =
                false;
        }

        if (buttonText) {
            buttonText.textContent =
                "RUN ANALYSIS";
        }
    }
}

// ============================================================
// UPDATE MAIN UI
// ============================================================

function updateMainUI(data) {

    show("result-box");
    hide("empty-state");

    // Hero
    setText(
        "hero-price",
        formatPrice(
            data.last_price
        )
    );

    setText(
        "hero-rsi",
        formatNumber(
            data.rsi,
            2
        )
    );

    setText(
        "hero-macd",
        formatNumber(
            data.macd,
            4
        )
    );

    setText(
        "hero-trend-tag",
        data.trend ||
        "--"
    );

    // Verdict
    updateVerdict(
        data.final_verdict
    );

    // Tier 1
    renderTier1(data);

    // Tier 2
    renderTier2(data);

    // Tier 3
    renderTier3(data);
}

// ============================================================
// VERDICT
// ============================================================

function updateVerdict(verdict) {

    const gaugeVerdict =
        $("gauge-verdict");

    const gaugeConfidence =
        $("gauge-confidence");

    const gaugeFill =
        $("gauge-fill");

    const confidence =
        Number(
            dataSafeConfidence
        );

    const finalVerdict =
        verdict || "WAIT";

    if (gaugeVerdict) {

        gaugeVerdict.textContent =
            finalVerdict;

        gaugeVerdict.classList.remove(
            "text-long",
            "text-short",
            "text-wait"
        );

        if (
            finalVerdict
                .toUpperCase()
                .includes("LONG")
        ) {

            gaugeVerdict.classList.add(
                "text-long"
            );

        } else if (
            finalVerdict
                .toUpperCase()
                .includes("SHORT")
        ) {

            gaugeVerdict.classList.add(
                "text-short"
            );

        } else {

            gaugeVerdict.classList.add(
                "text-wait"
            );
        }
    }

    if (gaugeConfidence) {

        gaugeConfidence.textContent =
            `${formatPercent(
                confidence,
                1
            )} confidence`;
    }

    if (gaugeFill) {

        const circumference =
            301.6;

        const percentage =
            Math.max(
                0,
                Math.min(
                    100,
                    confidence
                )
            );

        gaugeFill.style.strokeDashoffset =
            circumference -
            (
                circumference *
                percentage /
                100
            );

        if (
            finalVerdict
                .toUpperCase()
                .includes("LONG")
        ) {

            gaugeFill.style.stroke =
                "var(--long)";

        } else if (
            finalVerdict
                .toUpperCase()
                .includes("SHORT")
        ) {

            gaugeFill.style.stroke =
                "var(--short)";

        } else {

            gaugeFill.style.stroke =
                "var(--wait)";
        }
    }
}

// ============================================================
// GLOBAL CONFIDENCE VALUE
// ============================================================

let dataSafeConfidence = 0;

// ============================================================
// TIER 1
// ============================================================

function renderTier1(data) {

    const container =
        $("tier-1");

    if (!container) return;

    dataSafeConfidence =
        Number(
            data.confidence_pct ??
            data.confidence ??
            0
        );

    const cards = [

        {
            id: "CH.01",
            title: "Hawkes Buying Pressure",
            model: "HAWKES",
            value: formatNumber(
                data.buying_pressure,
                1
            ),
            detail: "Buying intensity",
            cls: "text-long"
        },

        {
            id: "CH.02",
            title: "Hawkes Selling Pressure",
            model: "HAWKES",
            value: formatNumber(
                data.selling_pressure,
                1
            ),
            detail: "Selling intensity",
            cls: "text-short"
        },

        {
            id: "CH.03",
            title: "Bayesian Classifier",
            model: "BAYES",
            value: `${formatPercent(
                data.bullish_pct,
                1
            )} BULLISH`,
            detail: `${formatPercent(
                data.bearish_pct,
                1
            )} bearish`,
            cls: "text-long"
        },

        {
            id: "CH.04",
            title: "Quantile Volatility",
            model: "Q-VOL",
            value: formatPercent(
                data.expected_volatility_pct,
                2
            ),
            detail:
                `95% extreme: ${formatPercent(
                    data.extreme_volatility_95_pct,
                    2
                )}`,
            cls: "text-wait"
        },

        {
            id: "CH.05",
            title: "Conformal Prediction",
            model: "CONFORMAL",
            value: formatPercent(
                data.confidence_pct,
                1
            ),
            detail:
                data.final_verdict ||
                "WAIT",
            cls: "text-long"
        }
    ];

    container.innerHTML =
        cards.map(card => `
            <div class="channel-card">

                <div class="channel-head">
                    <div class="channel-id-group">
                        <div class="scope-ticks">
                            <span style="height:5px"></span>
                            <span style="height:8px"></span>
                            <span style="height:11px"></span>
                        </div>

                        <div>
                            <div class="channel-id">
                                ${card.id}
                            </div>

                            <div class="channel-title">
                                ${card.title}
                            </div>
                        </div>
                    </div>

                    <div class="channel-model">
                        ${card.model}
                    </div>
                </div>

                <div class="channel-main ${card.cls}">
                    ${card.value}
                </div>

                <div class="channel-detail">
                    ${card.detail}
                </div>

            </div>
        `)
        .join("");

    // Risk card
    container.insertAdjacentHTML(
        "beforeend",
        `
        <div class="channel-card span-2">

            <div class="channel-head">
                <div class="channel-id-group">
                    <div class="channel-id">
                        RISK
                    </div>

                    <div class="channel-title">
                        Fractional Kelly Risk
                    </div>
                </div>

                <div class="channel-model">
                    KELLY
                </div>
            </div>

            <div class="channel-main text-wait">
                ${formatPercent(
                    data.suggested_risk_pct,
                    2
                )}
            </div>

            <div class="channel-detail">
                Suggested account risk
            </div>

        </div>

        <div class="channel-card span-2">

            <div class="channel-head">
                <div class="channel-id-group">
                    <div class="channel-id">
                        SL / TP
                    </div>

                    <div class="channel-title">
                        Risk Levels
                    </div>
                </div>

                <div class="channel-model">
                    QUANTILE
                </div>
            </div>

            <div class="dual-split">

                <div class="dual-item">
                    <span class="dual-label">
                        STOP LOSS
                    </span>

                    <span class="dual-value text-short">
                        ${formatPrice(
                            data.stop_loss
                        )}
                    </span>
                </div>

                <div class="dual-item">
                    <span class="dual-label">
                        TAKE PROFIT
                    </span>

                    <span class="dual-value text-long">
                        ${formatPrice(
                            data.take_profit
                        )}
                    </span>
                </div>

            </div>

        </div>
        `
    );

    updateVerdict(
        data.final_verdict
    );
}

// ============================================================
// TIER 2
// ============================================================

function renderTier2(data) {

    const container =
        $("tier-2");

    if (!container) return;

    const order =
        data.order_flow || {};

    const toxic =
        data.toxic_flow || {};

    const regime =
        data.market_regime || {};

    const jump =
        data.jump_shock || {};

    container.innerHTML = `

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.06
                    </div>
                    <div class="channel-title">
                        Order Flow
                    </div>
                </div>

                <div class="channel-model">
                    OFI
                </div>
            </div>

            <div class="channel-main">
                ${formatNumber(
                    order.ofi_score,
                    2
                )}
            </div>

            <div class="channel-detail">
                Raw: ${formatNumber(
                    order.ofi_raw,
                    4
                )}
            </div>

        </div>

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.07
                    </div>
                    <div class="channel-title">
                        Toxic Flow
                    </div>
                </div>

                <div class="channel-model">
                    VPIN
                </div>
            </div>

            <div class="channel-main text-wait">
                ${formatNumber(
                    toxic.vpin_score,
                    3
                )}
            </div>

            <div class="channel-detail">
                ${toxic.toxicity || "--"}
            </div>

        </div>

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.08
                    </div>
                    <div class="channel-title">
                        Market Regime
                    </div>
                </div>

                <div class="channel-model">
                    HMM
                </div>
            </div>

            <div class="channel-main">
                ${regime.regime || "--"}
            </div>

            <div class="channel-detail">
                State: ${regime.state ?? "--"}
            </div>

        </div>

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.09
                    </div>
                    <div class="channel-title">
                        Jump Shock
                    </div>
                </div>

                <div class="channel-model">
                    JUMP
                </div>
            </div>

            <div class="channel-main ${
                jump.jump_detected
                    ? "text-short"
                    : "text-long"
            }">
                ${
                    jump.jump_detected
                        ? "DETECTED"
                        : "NONE"
                }
            </div>

            <div class="channel-detail">
                Z-score:
                ${formatNumber(
                    jump.jump_zscore,
                    2
                )}
            </div>

        </div>

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.10
                    </div>
                    <div class="channel-title">
                        Meta Decision
                    </div>
                </div>

                <div class="channel-model">
                    META
                </div>
            </div>

            <div class="channel-main">
                ${
                    data.meta_label ||
                    data.meta_decision ||
                    "--"
                }
            </div>

            <div class="channel-detail">
                Win probability:
                ${formatPercent(
                    data.meta_win_probability,
                    1
                )}
            </div>

        </div>
    `;
}

// ============================================================
// TIER 3
// ============================================================

function renderTier3(data) {

    const container =
        $("tier-3");

    if (!container) return;

    const div =
        data.intermarket_divergence || {};

    const entropy =
        data.entropy || {};

    const depth =
        data.depth_profile || {};

    const vwap =
        data.vwap_deviation || {};

    const rl =
        data.rl_risk_agent || {};

    const hurst =
        data.hurst || {};

    const wavelet =
        data.wavelet_trend || {};

    const structural =
        data.structural_break || {};

    const sweep =
        data.liquidity_sweep || {};

    container.innerHTML = `

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.11
                    </div>
                    <div class="channel-title">
                        Intermarket Divergence
                    </div>
                </div>
            </div>

            <div class="channel-main">
                ${formatNumber(
                    div.divergence_score,
                    3
                )}
            </div>

            <div class="channel-detail">
                ${div.interpretation || "--"}
            </div>

        </div>

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.12
                    </div>
                    <div class="channel-title">
                        Entropy
                    </div>
                </div>
            </div>

            <div class="channel-main">
                ${formatNumber(
                    entropy.entropy_avg,
                    3
                )}
            </div>

            <div class="channel-detail">
                Regime:
                ${entropy.regime || "--"}
            </div>

        </div>

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.13
                    </div>
                    <div class="channel-title">
                        Depth Profile
                    </div>
                </div>
            </div>

            <div class="channel-main">
                ${depth.wall_bias || "--"}
            </div>

            <div class="channel-detail">
                Slope:
                ${formatNumber(
                    depth.depth_slope,
                    4
                )}
            </div>

        </div>

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.14
                    </div>
                    <div class="channel-title">
                        VWAP Deviation
                    </div>
                </div>
            </div>

            <div class="channel-main">
                ${formatNumber(
                    vwap.vwap_deviation_z,
                    2
                )}
            </div>

            <div class="channel-detail">
                ${vwap.signal || "--"}
            </div>

        </div>

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.15
                    </div>
                    <div class="channel-title">
                        RL Risk Agent
                    </div>
                </div>
            </div>

            <div class="channel-main">
                ${rl.rl_state || "--"}
            </div>

            <div class="channel-detail">
                Risk:
                ${formatPercent(
                    rl.rl_adjusted_risk_pct,
                    2
                )}
            </div>

        </div>

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.16
                    </div>
                    <div class="channel-title">
                        Hurst Memory
                    </div>
                </div>
            </div>

            <div class="channel-main">
                ${formatNumber(
                    hurst.hurst,
                    3
                )}
            </div>

            <div class="channel-detail">
                ${hurst.memory || "--"}
            </div>

        </div>

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.17
                    </div>
                    <div class="channel-title">
                        Wavelet Trend
                    </div>
                </div>
            </div>

            <div class="channel-main">
                ${
                    wavelet.wavelet_trend_direction ||
                    "--"
                }
            </div>

            <div class="channel-detail">
                Slope:
                ${formatNumber(
                    wavelet.wavelet_trend_slope,
                    4
                )}
            </div>

        </div>

        <div class="channel-card">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.18
                    </div>
                    <div class="channel-title">
                        Structural Break
                    </div>
                </div>
            </div>

            <div class="channel-main ${
                structural.structural_break
                    ? "text-short"
                    : "text-long"
            }">
                ${
                    structural.structural_break
                        ? "DETECTED"
                        : "NO BREAK"
                }
            </div>

            <div class="channel-detail">
                Recent breaks:
                ${
                    structural.recent_break_count ??
                    "--"
                }
            </div>

        </div>

        <div class="channel-card span-2">

            <div class="channel-head">
                <div>
                    <div class="channel-id">
                        CH.19
                    </div>
                    <div class="channel-title">
                        Liquidity Sweep
                    </div>
                </div>
            </div>

            <div class="channel-main">
                ${
                    sweep.liquidity_sweep_detected
                        ? "DETECTED"
                        : "NONE"
                }
            </div>

            <div class="channel-detail">
                Direction:
                ${
                    sweep.sweep_direction ||
                    "--"
                }
            </div>

        </div>

    `;
}

// ============================================================
// LIQUIDITY SCANNER
// GET /liquidity
// ============================================================

async function loadLiquidity() {

    const coin =
        getCoin();

    const scanner =
        $("liquidity-scanner");

    if (!scanner) {
        console.warn(
            "#liquidity-scanner not found"
        );
        return;
    }

    try {

        const url =
            `${API_BASE}/liquidity` +
            `?coin=${encodeURIComponent(
                coin
            )}`;

        console.log(
            "Calling Liquidity API:",
            url
        );

        const response =
            await fetch(url, {
                method: "GET",
                headers: {
                    "Accept":
                        "application/json"
                }
            });

        const data =
            await response.json();

        if (
            !response.ok ||
            data.error
        ) {
            throw new Error(
                data.error ||
                `Liquidity server error: ${response.status}`
            );
        }

        console.log(
            "Liquidity result:",
            data
        );

        renderLiquidity(data);

    } catch (error) {

        console.error(
            "Liquidity scanner failed:",
            error
        );

        scanner.innerHTML = `
            <div class="liq-panel">

                <div class="liq-header">
                    <div class="liq-header-left">

                        <div class="liq-icon">
                            ◉
                        </div>

                        <div>
                            <div class="liq-title">
                                Liquidity Scanner
                            </div>

                            <div class="liq-subtitle">
                                ${coin} · Binance USD-M Futures
                            </div>
                        </div>

                    </div>
                </div>

                <div class="liq-spoof-empty">
                    Unable to load liquidity data:
                    ${error.message}
                </div>

            </div>
        `;
    }
}

function renderLiquidity(data) {

    const container =
        $("liquidity-scanner");

    if (!container) return;

    const buyPct =
        Number(data.buy_pct ?? 50);

    const sellPct =
        Number(data.sell_pct ?? 50);

    // Convert buy/sell into 0-100
    // position for the bias marker.
    const total =
        buyPct + sellPct;

    const markerPosition =
        total > 0
            ? (
                buyPct /
                total
            ) * 100
            : 50;

    const bias =
        data.bias_tag ||
        "NEUTRAL";

    const spoofFlags =
        Array.isArray(
            data.spoof_flags
        )
            ? data.spoof_flags
            : [];

    const spoofHtml =
        spoofFlags.length > 0
            ? spoofFlags.map(
                flag => `
                    <div class="liq-spoof-item">
                        <span>
                            POSSIBLE SPOOFING
                        </span>

                        <span>
                            ${String(
                                flag
                            )}
                        </span>
                    </div>
                `
            ).join("")
            : `
                <div class="liq-spoof-empty">
                    No spoofing flags detected
                </div>
            `;

    container.innerHTML = `

        <div class="liq-panel">

            <div class="liq-header">

                <div class="liq-header-left">

                    <div class="liq-icon">
                        ◉
                    </div>

                    <div>

                        <div class="liq-title">
                            LIQUIDITY SCANNER
                        </div>

                        <div class="liq-subtitle">
                            ${data.coin || currentCoin}
                            · Binance USD-M Futures
                        </div>

                    </div>

                </div>

                <div class="liq-live-tag">

                    <span class="live-clock-dot"></span>

                    LIVE

                </div>

            </div>


            <div class="liq-price-row">

                <div class="liq-price">
                    ${formatPrice(
                        data.price
                    )}
                </div>

                <div class="liq-symbol">
                    ${data.coin || currentCoin}
                </div>

            </div>


            <div class="liq-stats-grid">

                <div class="liq-stat">

                    <div class="liq-stat-label">
                        24H HIGH
                    </div>

                    <div class="liq-stat-value">
                        ${formatPrice(
                            data.high_24h
                        )}
                    </div>

                </div>


                <div class="liq-stat">

                    <div class="liq-stat-label">
                        24H LOW
                    </div>

                    <div class="liq-stat-value">
                        ${formatPrice(
                            data.low_24h
                        )}
                    </div>

                </div>


                <div class="liq-stat">

                    <div class="liq-stat-label">
                        24H VOLUME
                    </div>

                    <div class="liq-stat-value">
                        ${
                            data.volume_usd_24h !==
                            null &&
                            data.volume_usd_24h !==
                            undefined
                                ? "$" +
                                  formatNumber(
                                      data.volume_usd_24h,
                                      0
                                  )
                                : "--"
                        }
                    </div>

                </div>


                <div class="liq-stat">

                    <div class="liq-stat-label">
                        24H CHANGE
                    </div>

                    <div class="liq-stat-value">
                        ${formatPercent(
                            data.change_pct_24h,
                            2
                        )}
                    </div>

                </div>


                <div class="liq-stat">

                    <div class="liq-stat-label">
                        MARK PRICE
                    </div>

                    <div class="liq-stat-value">
                        ${formatPrice(
                            data.mark_price
                        )}
                    </div>

                </div>


                <div class="liq-stat">

                    <div class="liq-stat-label">
                        FUNDING RATE
                    </div>

                    <div class="liq-stat-value">
                        ${formatPercent(
                            data.funding_rate_pct,
                            4
                        )}
                    </div>

                </div>


                <div class="liq-stat">

                    <div class="liq-stat-label">
                        OPEN INTEREST
                    </div>

                    <div class="liq-stat-value">
                        ${
                            data.open_interest_usd !==
                            null &&
                            data.open_interest_usd !==
                            undefined
                                ? "$" +
                                  formatNumber(
                                      data.open_interest_usd,
                                      0
                                  )
                                : "--"
                        }
                    </div>

                </div>


                <div class="liq-stat">

                    <div class="liq-stat-label">
                        BIAS
                    </div>

                    <div class="liq-stat-value">
                        ${bias}
                    </div>

                </div>

            </div>


            <div class="liq-bias-card">

                <div class="liq-bias-heading">

                    <span class="liq-bias-title">
                        MARKET LIQUIDITY BIAS
                    </span>

                    <span class="badge ${
                        buyPct >= sellPct
                            ? "on-long"
                            : "on-short"
                    }">
                        ${bias}
                    </span>

                </div>


                <div class="liq-bias-track">

                    <div
                        class="liq-bias-marker"
                        style="
                            left:
                            ${markerPosition}%;
                        "
                    ></div>

                </div>


                <div class="liq-bias-labels">

                    <span class="text-long">
                        BUY
                        ${formatPercent(
                            buyPct,
                            1
                        )}
                    </span>

                    <span class="text-short">
                        SELL
                        ${formatPercent(
                            sellPct,
                            1
                        )}
                    </span>

                </div>

            </div>


            <div class="liq-walls-grid">

                <div class="liq-wall-card">

                    <div class="liq-wall-head">

                        <span class="liq-wall-label">
                            BID WALL
                        </span>

                        <span class="badge on-long">
                            SUPPORT
                        </span>

                    </div>

                    <div class="liq-wall-price text-long">
                        ${
                            data.bid_wall
                                ? formatPrice(
                                    data.bid_wall.price
                                )
                                : "--"
                        }
                    </div>

                    <div class="liq-wall-detail">
                        Quantity:
                        ${
                            data.bid_wall
                                ? formatNumber(
                                    data.bid_wall.qty,
                                    4
                                )
                                : "--"
                        }

                        <br>

                        Notional:
                        ${
                            data.bid_wall
                                ? "$" +
                                  formatNumber(
                                      data.bid_wall.notional,
                                      0
                                  )
                                : "--"
                        }
                    </div>

                </div>


                <div class="liq-wall-card">

                    <div class="liq-wall-head">

                        <span class="liq-wall-label">
                            ASK WALL
                        </span>

                        <span class="badge on-short">
                            RESISTANCE
                        </span>

                    </div>

                    <div class="liq-wall-price text-short">
                        ${
                            data.ask_wall
                                ? formatPrice(
                                    data.ask_wall.price
                                )
                                : "--"
                        }
                    </div>

                    <div class="liq-wall-detail">
                        Quantity:
                        ${
                            data.ask_wall
                                ? formatNumber(
                                    data.ask_wall.qty,
                                    4
                                )
                                : "--"
                        }

                        <br>

                        Notional:
                        ${
                            data.ask_wall
                                ? "$" +
                                  formatNumber(
                                      data.ask_wall.notional,
                                      0
                                  )
                                : "--"
                        }
                    </div>

                </div>

            </div>


            <div class="liq-spoof-card">

                <div class="liq-spoof-heading">

                    <span class="liq-spoof-title">
                        SPOOFING HEURISTIC
                    </span>

                    <span class="badge ${
                        spoofFlags.length > 0
                            ? "on-short"
                            : "on-flat"
                    }">
                        ${
                            spoofFlags.length > 0
                                ? `${spoofFlags.length} FLAG(S)`
                                : "NO FLAG"
                        }
                    </span>

                </div>

                <div class="liq-spoof-list">
                    ${spoofHtml}
                </div>

            </div>


            <div class="liq-footnote">
                Exploratory display only — liquidity diagnostics
                do not change the final Tier 01 verdict.
            </div>

        </div>
    `;
}

// ============================================================
// LIVE CANDLE CHART
// Lightweight Charts
// ============================================================

async function loadCandles() {

    const chartContainer =
        $("candle-chart");

    if (!chartContainer) {
        return;
    }

    const coin =
        getCoin();

    try {

        const url =
            `${API_BASE}/candles` +
            `?coin=${encodeURIComponent(
                coin
            )}` +
            `&timeframe=${encodeURIComponent(
                currentTimeframe
            )}` +
            `&limit=200`;

        const response =
            await fetch(url);

        const data =
            await response.json();

        if (
            !response.ok ||
            data.error
        ) {
            throw new Error(
                data.error ||
                `Candles error: ${response.status}`
            );
        }

        updateLightweightChart(
            data
        );

    } catch (error) {

        console.error(
            "Candles failed:",
            error
        );

        setText(
            "chart-status",
            `Chart error: ${error.message}`
        );
    }
}

function initializeChart() {

    const container =
        $("candle-chart");

    if (!container) {
        return;
    }

    if (
        typeof LightweightCharts ===
        "undefined"
    ) {

        console.error(
            "Lightweight Charts library not loaded."
        );

        setText(
            "chart-status",
            "Chart library not loaded"
        );

        return;
    }

    if (candleChart) {
        return;
    }

    candleChart =
        LightweightCharts.createChart(
            container,
            {
                width:
                    container.clientWidth,

                height:
                    container.clientHeight,

                layout: {
                    background: {
                        type:
                            LightweightCharts.ColorType
                                ? LightweightCharts.ColorType.Solid
                                : "solid",

                        color:
                            "#070a0f"
                    },

                    textColor:
                        "#8b96a5"
                },

                grid: {
                    vertLines: {
                        color:
                            "#1a222c"
                    },

                    horzLines: {
                        color:
                            "#1a222c"
                    }
                },

                rightPriceScale: {
                    borderColor:
                        "#27303b"
                },

                timeScale: {
                    borderColor:
                        "#27303b",

                    timeVisible:
                        true,

                    secondsVisible:
                        false
                }
            }
        );

    candleSeries =
        candleChart.addCandlestickSeries(
            {
                upColor:
                    "#36e0a0",

                downColor:
                    "#ff526b",

                borderUpColor:
                    "#36e0a0",

                borderDownColor:
                    "#ff526b",

                wickUpColor:
                    "#36e0a0",

                wickDownColor:
                    "#ff526b"
            }
        );

    window.addEventListener(
        "resize",
        () => {

            if (
                candleChart &&
                container
            ) {

                candleChart.resize(
                    container.clientWidth,
                    container.clientHeight
                );
            }
        }
    );
}

function updateLightweightChart(data) {

    initializeChart();

    if (
        !candleChart ||
        !candleSeries
    ) {
        return;
    }

    const candles =
        Array.isArray(data.candles)
            ? data.candles
            : [];

    const formatted =
        candles
            .map(candle => ({

                time: Number(
                    candle.time
                ),

                open: Number(
                    candle.open
                ),

                high: Number(
                    candle.high
                ),

                low: Number(
                    candle.low
                ),

                close: Number(
                    candle.close
                )
            }))
            .filter(
                candle =>
                    Number.isFinite(
                        candle.time
                    ) &&
                    Number.isFinite(
                        candle.open
                    ) &&
                    Number.isFinite(
                        candle.high
                    ) &&
                    Number.isFinite(
                        candle.low
                    ) &&
                    Number.isFinite(
                        candle.close
                    )
            );

    if (formatted.length === 0) {

        setText(
            "chart-status",
            "No candle data available"
        );

        return;
    }

    candleSeries.setData(
        formatted
    );

    candleChart.timeScale()
        .fitContent();

    const last =
        formatted[
            formatted.length - 1
        ];

    setText(
        "chart-price",
        formatPrice(
            last.close
        )
    );

    if (
        data.change_pct !==
        undefined
    ) {

        const change =
            Number(
                data.change_pct
            );

        setText(
            "chart-change",
            formatPercent(
                change,
                3
            )
        );

        const changeEl =
            $("chart-change");

        if (changeEl) {

            changeEl.classList.remove(
                "up",
                "down"
            );

            if (change > 0) {
                changeEl.classList.add(
                    "up"
                );
            } else if (
                change < 0
            ) {
                changeEl.classList.add(
                    "down"
                );
            }
        }
    }

    setText(
        "chart-title",
        `${(
            data.coin ||
            currentCoin
        ).replace(
            "/",
            " / "
        )} · ${currentTimeframe.toUpperCase()}`
    );

    setText(
        "chart-status",
        `LIVE · ${formatted.length} candles loaded`
    );
}

// ============================================================
// TIMEFRAME BUTTONS
// ============================================================

function initializeTimeframeButtons() {

    const buttons =
        document.querySelectorAll(
            ".chart-tf-btn"
        );

    buttons.forEach(button => {

        button.addEventListener(
            "click",
            () => {

                buttons.forEach(
                    btn =>
                        btn.classList.remove(
                            "active"
                        )
                );

                button.classList.add(
                    "active"
                );

                currentTimeframe =
                    button.dataset.tf ||
                    "1h";

                loadCandles();
            }
        );
    });
}

// ============================================================
// TABS
// ============================================================

function initializeTabs() {

    const tabs =
        document.querySelectorAll(
            ".panel-tab"
        );

    const panels =
        document.querySelectorAll(
            ".tab-panel"
        );

    tabs.forEach(tab => {

        tab.addEventListener(
            "click",
            () => {

                const panelName =
                    tab.dataset.panel;

                if (!panelName) {
                    return;
                }

                tabs.forEach(
                    item =>
                        item.classList.remove(
                            "active"
                        )
                );

                panels.forEach(
                    panel =>
                        panel.classList.remove(
                            "active"
                        )
                );

                tab.classList.add(
                    "active"
                );

                const target =
                    $(
                        `panel-${panelName}`
                    );

                if (target) {

                    target.classList.add(
                        "active"
                    );
                }

                if (
                    panelName ===
                    "liquidity"
                ) {

                    loadLiquidity();
                }

                if (
                    panelName ===
                    "livechart"
                ) {

                    initializeChart();

                    setTimeout(
                        () => {
                            if (
                                candleChart
                            ) {
                                const container =
                                    $("candle-chart");

                                candleChart.resize(
                                    container.clientWidth,
                                    container.clientHeight
                                );

                                candleChart
                                    .timeScale()
                                    .fitContent();
                            }

                            loadCandles();

                        },
                        100
                    );
                }
            }
        );
    });
}

// ============================================================
// AUTO REFRESH
// ============================================================

function startAutoRefresh() {

    stopAutoRefresh();

    candleTimer =
        setInterval(
            () => {
                loadCandles();
            },
            10000
        );

    liquidityTimer =
        setInterval(
            () => {
                loadLiquidity();
            },
            10000
        );
}

function stopAutoRefresh() {

    if (candleTimer) {

        clearInterval(
            candleTimer
        );

        candleTimer = null;
    }

    if (liquidityTimer) {

        clearInterval(
            liquidityTimer
        );

        liquidityTimer = null;
    }
}

// ============================================================
// ANALYSIS BUTTON
// ============================================================

function initializeAnalysisButton() {

    const button =
        $("get-signal-btn");

    if (!button) {

        console.error(
            "RUN ANALYSIS button not found."
        );

        return;
    }

    button.addEventListener(
        "click",
        runAnalysis
    );
}

// ============================================================
// INITIALIZE
// ============================================================

async function initialize() {

    console.log(
        "SIGNAL/FM frontend starting..."
    );

    await loadCoins();

    currentCoin =
        getCoin();

    initializeCoinPicker();

    initializeTabs();

    initializeTimeframeButtons();

    initializeAnalysisButton();

    // Do NOT show result before analysis.
    hide("result-box");
    show("empty-state");

    // Load background data.
    // Liquidity and chart are available
    // when their tabs are opened.
    loadLiquidity();

    loadCandles();

    startAutoRefresh();

    console.log(
        "SIGNAL/FM frontend initialized."
    );

    console.log(
        "Selected coin:",
        currentCoin
    );

    console.log(
        "Selected timeframe:",
        currentTimeframe
    );
}

initialize();

// ============================================================
// EXPOSE FUNCTIONS
// ============================================================

window.runAnalysis =
    runAnalysis;

window.loadLiquidity =
    loadLiquidity;

window.loadCandles =
    loadCandles;

window.loadCoins =
    loadCoins;
```

});
