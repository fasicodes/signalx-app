const coinSelect = document.getElementById("coin-select");
const runBtn = document.getElementById("get-signal-btn");
const errorText = document.getElementById("error-text");
const resultBox = document.getElementById("result-box");
const alertStack = document.getElementById("alert-stack");

function fmtPrice(v) {
  if (v === null || v === undefined) return "--";
  return "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function fmtPct(v) {
  if (v === null || v === undefined) return "--";
  return v + "%";
}

function clearAlerts() {
  alertStack.innerHTML = "";
}

function addAlert(type, text) {
  const el = document.createElement("div");
  el.className = "alert-banner " + type;
  el.innerHTML = `<span class="alert-dot"></span><span>${text}</span>`;
  alertStack.appendChild(el);
}

async function runAnalysis() {
  const coin = coinSelect.value;

  errorText.classList.add("hidden");
  errorText.textContent = "";
  runBtn.disabled = true;
  runBtn.querySelector(".scan-btn-text").textContent = "SCANNING...";

  try {
    const res = await fetch(`/signal?coin=${encodeURIComponent(coin)}&timeframe=1h`);
    const data = await res.json();

    if (!res.ok || data.error) {
      throw new Error(data.error || "Signal fetch failed");
    }

    renderResult(data);
    resultBox.classList.remove("hidden");
  } catch (err) {
    errorText.textContent = "⚠ " + err.message;
    errorText.classList.remove("hidden");
    resultBox.classList.add("hidden");
  } finally {
    runBtn.disabled = false;
    runBtn.querySelector(".scan-btn-text").textContent = "RUN ANALYSIS";
  }
}

function renderResult(data) {
  clearAlerts();

  // ---------- warning banners (new) ----------
  const jump = data.jump_shock || {};
  if (jump.jump_detected) {
    addAlert("danger", `HIGH VOLATILITY SHOCK — ${jump.jump_direction} jump detected (z=${jump.jump_zscore})`);
  }
  if (data.fake_breakout_warning) {
    addAlert("warning", "FAKE BREAKOUT RISK — order flow disagrees with price direction");
  }

  // ---------- price / rsi / macd (old) ----------
  document.getElementById("price-value").textContent = fmtPrice(data.last_price);
  document.getElementById("rsi-value").textContent = data.rsi ?? "--";
  document.getElementById("macd-value").textContent = data.macd ?? "--";

  // ---------- verdict (old) ----------
  const verdictBlock = document.getElementById("verdict-block");
  const verdictText = document.getElementById("verdict-text");
  verdictBlock.classList.remove("long", "short", "wait");

  const verdict = (data.final_verdict || "--").toLowerCase();
  if (verdict === "long") verdictBlock.classList.add("long");
  else if (verdict === "short") verdictBlock.classList.add("short");
  else verdictBlock.classList.add("wait");

  verdictText.textContent = data.final_verdict || "--";
  document.getElementById("confidence-value").textContent = fmtPct(data.confidence_pct);

  // ---------- market bias (old) ----------
  document.getElementById("bias-fill").style.width = `${data.bullish_pct ?? 50}%`;
  document.getElementById("bullish-label").textContent = `${data.bullish_pct ?? "--"}% BULLISH`;
  document.getElementById("bearish-label").textContent = `BEARISH ${data.bearish_pct ?? "--"}%`;

  // ---------- hawkes pressure (old) ----------
  document.getElementById("buy-pressure-fill").style.width = `${(data.buying_pressure ?? 0) * 10}%`;
  document.getElementById("buy-pressure-value").textContent = `${data.buying_pressure ?? "--"} / 10`;
  document.getElementById("sell-pressure-fill").style.width = `${(data.selling_pressure ?? 0) * 10}%`;
  document.getElementById("sell-pressure-value").textContent = `${data.selling_pressure ?? "--"} / 10`;

  // ---------- SL / TP (old) ----------
  document.getElementById("sl-value").textContent = fmtPrice(data.stop_loss);
  document.getElementById("tp-value").textContent = fmtPrice(data.take_profit);

  // ---------- volatility metrics (old) ----------
  document.getElementById("expected-vol-value").textContent = data.expected_volatility_pct != null ? data.expected_volatility_pct + "%" : "--";
  document.getElementById("extreme-vol-value").textContent = data.extreme_volatility_95_pct != null ? data.extreme_volatility_95_pct + "%" : "--";
  document.getElementById("risk-value").textContent = data.suggested_risk_pct != null ? data.suggested_risk_pct + "%" : "--";

  // ---------- order flow imbalance (new) ----------
  const ofi = data.order_flow || {};
  const ofiFill = document.getElementById("ofi-fill");
  const ofiScore = ofi.ofi_score;
  if (ofiScore === null || ofiScore === undefined) {
    ofiFill.style.width = "0%";
    document.getElementById("ofi-value").textContent = "-- (no data)";
  } else {
    const pct = Math.min(Math.abs(ofiScore), 10) / 10 * 50; // half-track max
    ofiFill.style.width = `${pct}%`;
    if (ofiScore >= 0) {
      ofiFill.style.left = "50%";
      ofiFill.style.background = "#16d97a";
    } else {
      ofiFill.style.left = `${50 - pct}%`;
      ofiFill.style.background = "#ff4d5e";
    }
    document.getElementById("ofi-value").textContent = `${ofiScore > 0 ? "+" : ""}${ofiScore} (buyers ${ofiScore >= 0 ? "aggressive" : "passive"})`;
  }

  // ---------- toxic flow / VPIN (new) ----------
  const toxic = data.toxic_flow || {};
  const vpinFill = document.getElementById("vpin-fill");
  if (toxic.vpin_score === null || toxic.vpin_score === undefined) {
    vpinFill.style.width = "0%";
    document.getElementById("vpin-value").textContent = "-- (no data)";
  } else {
    vpinFill.style.width = `${Math.min(toxic.vpin_score, 1) * 100}%`;
    document.getElementById("vpin-value").textContent = `${toxic.vpin_score} — ${toxic.toxicity}`;
  }

  // ---------- market regime / HMM (new) ----------
  const regime = data.market_regime || {};
  const regimeBadge = document.getElementById("regime-badge");
  regimeBadge.classList.remove("trending", "ranging");
  if (regime.regime === "Trending") {
    regimeBadge.classList.add("trending");
    regimeBadge.textContent = "TRENDING";
  } else if (regime.regime === "Ranging") {
    regimeBadge.classList.add("ranging");
    regimeBadge.textContent = "RANGING";
  } else {
    regimeBadge.textContent = "N/A";
  }
  document.getElementById("regime-detail").textContent =
    regime.state_mean_return_pct != null ? `avg state return ${regime.state_mean_return_pct}%` : "insufficient data";

  // ---------- meta-labeling / ML filter (new) ----------
  const meta = data.meta_label || {};
  const metaBadge = document.getElementById("meta-badge");
  const metaFill = document.getElementById("meta-fill");
  metaBadge.classList.remove("execute", "skip", "insufficient");

  if (meta.meta_decision === "EXECUTE") {
    metaBadge.classList.add("execute");
    metaBadge.textContent = "EXECUTE";
  } else if (meta.meta_decision === "SKIP") {
    metaBadge.classList.add("skip");
    metaBadge.textContent = "SKIP";
  } else {
    metaBadge.classList.add("insufficient");
    metaBadge.textContent = "N/A";
  }

  metaFill.style.width = meta.meta_win_probability != null ? `${meta.meta_win_probability}%` : "0%";
  document.getElementById("meta-value").textContent =
    meta.meta_win_probability != null ? `${meta.meta_win_probability}% win probability` : "insufficient data";
}

runBtn.addEventListener("click", runAnalysis);
