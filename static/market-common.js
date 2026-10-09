/* Signals FM: helpers shared by the Live chart (chart-page.js) and the Liquidity scanner (liquidity-page.js).
   window.SFM = { esc, tip, store, decimals, price, signed, pct, usd, ago, getJSON, sendJSON, setIcon, toast,
                  base, isForex, coinPicker }.
   The coin picker reuses the dashboard's sheet styles (app.css: .d-sheet, .d-row ...). */
(function () {
  "use strict";

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // "?" button that opens a plain-language explanation (glossary.py, shown by shell.js)
  const tip = (key, label) => `<button type="button" class="tip" data-tip="${key}" aria-label="What does ${esc(label)} mean?">?</button>`;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
    getJSON(k, def) { try { const v = JSON.parse(localStorage.getItem(k)); return v == null ? def : v; } catch (e) { return def; } },
    setJSON(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  };

  let FOREX = [];
  const isForex = (sym) => FOREX.includes(sym);
  const base = (sym) => String(sym || "").split("/")[0];

  function decimals(v, sym) {
    if (sym && isForex(sym)) return sym.startsWith("XAU") ? 2 : sym.includes("JPY") ? 3 : 5;
    const a = Math.abs(Number(v));
    if (!isFinite(a) || a === 0) return 2;
    if (a >= 1000) return 2;
    if (a >= 10) return 3;
    if (a >= 1) return 4;
    if (a >= 0.01) return 5;
    if (a >= 0.0001) return 7;
    return 9;
  }
  function price(v, sym) {
    if (v == null || isNaN(v)) return "--";
    const d = decimals(v, sym);
    return Number(v).toLocaleString("en-US", { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d });
  }
  const signed = (v, d = 2) => (v == null || isNaN(v) ? "--" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(Number(v)).toFixed(d)}`);
  const pct = (v, d = 2) => (v == null || isNaN(v) ? "--" : `${signed(v, d)}%`);
  function usd(v, d = 1) {
    if (v == null || isNaN(v)) return "--";
    const a = Math.abs(v), s = v < 0 ? "−" : "";
    if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(d)}B`;
    if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(d)}M`;
    if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(d)}K`;
    return `${s}$${a.toFixed(0)}`;
  }
  function ago(sec) {
    if (sec == null || isNaN(sec)) return "";
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return `${sec}s ago`;
    const m = Math.round(sec / 60);
    if (m < 60) return `${m} min ago`;
    const h = Math.floor(m / 60);
    return h < 48 ? `${h} h ago` : `${Math.floor(h / 24)} days ago`;
  }

  async function getJSON(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    if (r.status === 401) { location.href = "/login"; throw new Error("login"); }
    const d = await r.json().catch(() => ({}));
    if (!r.ok && !d.error) d.error = `Request failed (${r.status})`;
    if (!r.ok) d._status = r.status;
    return d;
  }
  async function sendJSON(url, method, body) {
    const r = await fetch(url, { method, credentials: "same-origin", headers: { "Content-Type": "application/json" },
                                 body: body === undefined ? undefined : JSON.stringify(body) });
    if (r.status === 401) { location.href = "/login"; throw new Error("login"); }
    const d = await r.json().catch(() => ({}));
    if (!r.ok && !d.error) d.error = `Request failed (${r.status})`;
    d._ok = r.ok;
    return d;
  }

  const ICON_SLUG = { HYPE: "hype", GRAM: "gram", ASTER: "aster", ONDO: "ondo", TAO: "tao" };
  function badgeIcon(t) {
    const pal = ["#3b82f6", "#8b5cf6", "#ec4899", "#f97316", "#14b8a6", "#eab308", "#ef4444", "#22c55e"];
    let sum = 0; for (const ch of t) sum += ch.charCodeAt(0);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><circle cx="20" cy="20" r="19" fill="${pal[sum % pal.length]}"/><text x="20" y="25" font-family="Arial" font-size="11" font-weight="800" fill="#fff" text-anchor="middle">${t.slice(0, 3)}</text></svg>`;
    return "data:image/svg+xml;base64," + btoa(svg);
  }
  function setIcon(img, sym) {
    if (!img) return;
    const t = base(sym).toUpperCase();
    img.onerror = null;
    if (isForex(sym)) { img.src = badgeIcon(t); return; }
    img.onerror = () => { img.onerror = null; img.src = badgeIcon(t); };
    img.src = `https://assets.coincap.io/assets/icons/${(ICON_SLUG[t] || t).toLowerCase()}@2x.png`;
  }
  function toast(msg) {
    const t = document.createElement("div");
    t.className = "d-toast"; t.setAttribute("role", "status"); t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2400);
  }

  // ------------------------------------------------------------------ coin picker (bottom sheet on phones, dialog on desktop)
  // opts: { onSelect(sym), current() }. Data setters: setCoins({crypto, forex}), setTickers({sym: {last, change_pct}}),
  //       setBoard(boardJson), setWatch(Set). Recent coins are remembered in this browser (sfm-recent).
  function coinPicker(opts) {
    const P = { coins: { crypto: [], forex: [] }, tickers: {}, board: null, watch: new Set(), kind: "crypto", opener: null };
    const sheet = document.createElement("div");
    sheet.className = "d-sheet";
    sheet.hidden = true;
    sheet.innerHTML = `
      <div class="d-sheet-panel mk-sheet" role="dialog" aria-modal="true" aria-label="Choose a market">
        <span class="an-grip" aria-hidden="true"></span>
        <div class="d-sheet-head">
          <input class="d-search" type="search" placeholder="Search coins and pairs" autocomplete="off" aria-label="Search coins and pairs">
          <button class="d-close" type="button" aria-label="Close">&times;</button>
        </div>
        <div class="mk-recent-row" hidden></div>
        <div class="d-tabs2" role="group" aria-label="Market">
          <button type="button" data-kind="watch" aria-pressed="false" hidden>Watchlist</button>
          <button type="button" data-kind="crypto" aria-pressed="true">Crypto</button>
          <button type="button" data-kind="forex" aria-pressed="false">Forex &amp; gold</button>
        </div>
        <div class="d-list" role="listbox" aria-label="Markets"></div>
      </div>`;
    document.body.appendChild(sheet);
    const q = sheet.querySelector(".d-search"), list = sheet.querySelector(".d-list"), recentRow = sheet.querySelector(".mk-recent-row");
    const tabs = sheet.querySelectorAll(".d-tabs2 button");

    const recent = () => (store.getJSON("sfm-recent", []) || []).filter((s) => P.coins.crypto.includes(s) || P.coins.forex.includes(s));
    function remember(sym) {
      const r = [sym].concat(recent().filter((s) => s !== sym)).slice(0, 8);
      store.setJSON("sfm-recent", r);
    }
    function pill(sym) {
      const rows = P.board && P.board.rows;
      const r = rows ? rows.find((x) => x.symbol === sym) : null;
      if (!r) return "";
      if (r.state === "ACTIVE") {
        const s = r.active.side === "LONG" ? "long" : "short";
        return `<span class="d-pill" style="background:var(--${s}-bg);color:var(--${s})">${r.fresh ? "New " + s : s === "long" ? "Long" : "Short"}</span>`;
      }
      if (r.setup_forming) return `<span class="d-pill" style="background:var(--amber-bg);color:var(--amber)">Forming</span>`;
      return "";
    }
    function quote(sym) {
      const t = P.tickers[sym];
      if (!t || t.last == null) return "";
      const ch = t.change_pct;
      return `<span class="mk-q"><b class="tnum">${price(t.last, sym)}</b>${ch == null ? "" : `<small class="tnum ${ch >= 0 ? "c-long" : "c-short"}">${pct(ch)}</small>`}</span>`;
    }
    function setKind(kind) {
      P.kind = kind;
      tabs.forEach((b) => b.setAttribute("aria-pressed", b.dataset.kind === kind ? "true" : "false"));
      render();
    }
    function render() {
      const text = q.value.trim().toUpperCase().replace("/", "");
      let pool;
      if (text) pool = P.coins.crypto.concat(P.coins.forex);           // search looks everywhere
      else if (P.kind === "watch") pool = [...P.watch].filter((s) => P.coins.crypto.includes(s) || P.coins.forex.includes(s));
      else pool = P.coins[P.kind] || [];
      const rows = pool.filter((s) => !text || s.replace("/", "").includes(text) || base(s).includes(text));
      const cur = opts.current();
      if (!rows.length) {
        list.innerHTML = `<p class="d-empty" style="padding:16px 6px">${text ? `Nothing matches “${esc(q.value.trim())}”.` : "Your watchlist is empty. Tap the star next to a coin to add it."}</p>`;
        return;
      }
      list.innerHTML = rows.map((s) => `<button type="button" class="d-row" role="option" data-sym="${esc(s)}" aria-selected="${s === cur}">
        <img alt="" data-icon="${esc(s)}"><span><b>${esc(s)}</b><small>${isForex(s) ? (s.startsWith("XAU") ? "Gold" : "Forex") : "Crypto"}${P.watch.has(s) ? " · ★" : ""}</small></span>
        ${quote(s)}${pill(s)}</button>`).join("");
      list.querySelectorAll("img[data-icon]").forEach((img) => setIcon(img, img.dataset.icon));
    }
    function renderRecent() {
      const r = recent().filter((s) => s !== opts.current()).slice(0, 6);
      recentRow.hidden = !r.length;
      recentRow.innerHTML = r.length ? `<span>Recent</span>` + r.map((s) => `<button type="button" class="mk-chip" data-sym="${esc(s)}">${esc(base(s))}</button>`).join("") : "";
    }
    function open(opener) {
      P.opener = opener || document.activeElement;
      q.value = "";
      setKind(isForex(opts.current()) ? "forex" : "crypto");
      renderRecent();
      sheet.hidden = false;
      document.body.classList.add("an-lock");
      setTimeout(() => q.focus(), 30);
      if (opts.onOpen) opts.onOpen();
    }
    function close() {
      if (sheet.hidden) return;
      sheet.hidden = true;
      document.body.classList.remove("an-lock");
      if (P.opener && P.opener.focus) P.opener.focus({ preventScroll: true });
    }
    function choose(sym) {
      close();
      remember(sym);
      opts.onSelect(sym);
    }
    sheet.addEventListener("click", (e) => {
      if (e.target === sheet) { close(); return; }
      const row = e.target.closest("[data-sym]");
      if (row) choose(row.dataset.sym);
    });
    sheet.querySelector(".d-close").addEventListener("click", close);
    tabs.forEach((b) => b.addEventListener("click", () => setKind(b.dataset.kind)));
    q.addEventListener("input", render);
    q.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); const first = list.querySelector("[data-sym]"); if (first) choose(first.dataset.sym); }
      if (e.key === "ArrowDown") { const first = list.querySelector("[data-sym]"); if (first) { e.preventDefault(); first.focus(); } }
    });
    list.addEventListener("keydown", (e) => {
      const items = [...list.querySelectorAll("[data-sym]")];
      const i = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown" && i >= 0 && i < items.length - 1) { e.preventDefault(); items[i + 1].focus(); }
      if (e.key === "ArrowUp") { e.preventDefault(); if (i > 0) items[i - 1].focus(); else q.focus(); }
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !sheet.hidden) { e.stopPropagation(); close(); } }, true);

    return {
      open, close, remember, recent,
      isOpen: () => !sheet.hidden,
      setCoins(c) {
        P.coins = { crypto: (c && c.crypto) || [], forex: (c && c.forex) || [] };
        FOREX = P.coins.forex.slice();
        if (!sheet.hidden) render();
      },
      setTickers(t) { P.tickers = t || {}; if (!sheet.hidden) render(); },
      setBoard(b) { P.board = b; if (!sheet.hidden) render(); },
      setWatch(set) {
        P.watch = set || new Set();
        sheet.querySelector('[data-kind="watch"]').hidden = !P.watch.size;
        if (!sheet.hidden) render();
      },
      valid: (sym) => P.coins.crypto.includes(sym) || P.coins.forex.includes(sym),
    };
  }

  window.SFM = { esc, tip, store, decimals, price, signed, pct, usd, ago, getJSON, sendJSON, setIcon, toast, base, isForex,
                 coinPicker, setForex(list) { FOREX = (list || []).slice(); } };
})();
