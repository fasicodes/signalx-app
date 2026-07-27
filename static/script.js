// Backend URL - khali chhod dein, kyunke HTML aur API ab same server se serve ho rahe hain
const API_BASE_URL = "";

const coinSelect = document.getElementById("coin-select");
const getSignalBtn = document.getElementById("get-signal-btn");
const resultBox = document.getElementById("result-box");
const errorText = document.getElementById("error-text");

const priceValue = document.getElementById("price-value");
const rsiValue = document.getElementById("rsi-value");
const macdValue = document.getElementById("macd-value");

const verdictBlock = document.getElementById("verdict-block");
const verdictText = document.getElementById("verdict-text");
const confidenceValue = document.getElementById("confidence-value");

const biasFill = document.getElementById("bias-fill");
const bullishLabel = document.getElementById("bullish-label");
const bearishLabel = document.getElementById("bearish-label");

const buyPressureFill = document.getElementById("buy-pressure-fill");
const sellPressureFill = document.getElementById("sell-pressure-fill");
const buyPressureValue = document.getElementById("buy-pressure-value");
const sellPressureValue = document.getElementById("sell-pressure-value");

const slValue = document.getElementById("sl-value");
const tpValue = document.getElementById("tp-value");
const expectedVolValue = document.getElementById("expected-vol-value");
const extremeVolValue = document.getElementById("extreme-vol-value");
const riskValue = document.getElementById("risk-value");

getSignalBtn.addEventListener("click", async () => {
  const coin = coinSelect.value;

  getSignalBtn.querySelector(".scan-btn-text").textContent = "SCANNING...";
  getSignalBtn.disabled = true;
  errorText.classList.add("hidden");

  try {
    const response = await fetch(`${API_BASE_URL}/signal?coin=${encodeURIComponent(coin)}`);
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || "Signal fetch nahi ho saka");
    }

    updateUI(data);
  } catch (err) {
    errorText.textContent = err.message;
    errorText.classList.remove("hidden");
    resultBox.classList.add("hidden");
  } finally {
    getSignalBtn.querySelector(".scan-btn-text").textContent = "RUN ANALYSIS";
    getSignalBtn.disabled = false;
  }
});

function updateUI(data) {
  resultBox.classList.remove("hidden");

  priceValue.textContent = `$${data.last_price.toLocaleString()}`;
  rsiValue.textContent = data.rsi;
  macdValue.textContent = data.macd;

  verdictText.textContent = data.final_verdict;
  confidenceValue.textContent = `${data.confidence_pct}%`;
  verdictBlock.classList.remove("long", "short", "wait");
  if (data.final_verdict === "LONG") verdictBlock.classList.add("long");
  else if (data.final_verdict === "SHORT") verdictBlock.classList.add("short");
  else verdictBlock.classList.add("wait");

  biasFill.style.width = `${data.bullish_pct}%`;
  bullishLabel.textContent = `${data.bullish_pct}% BULLISH`;
  bearishLabel.textContent = `BEARISH ${data.bearish_pct}%`;

  buyPressureFill.style.width = `${(data.buying_pressure / 10) * 100}%`;
  sellPressureFill.style.width = `${(data.selling_pressure / 10) * 100}%`;
  buyPressureValue.textContent = `${data.buying_pressure} / 10`;
  sellPressureValue.textContent = `${data.selling_pressure} / 10`;

  slValue.textContent = data.stop_loss ? `$${data.stop_loss.toLocaleString()}` : "N/A — WAIT";
  tpValue.textContent = data.take_profit ? `$${data.take_profit.toLocaleString()}` : "N/A — WAIT";

  expectedVolValue.textContent = `${data.expected_volatility_pct}%`;
  extremeVolValue.textContent = `${data.extreme_volatility_95_pct}%`;
  riskValue.textContent = `${data.suggested_risk_pct}%`;
}
