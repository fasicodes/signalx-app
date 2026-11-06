/* "Premium chart Access" button (templates/_premium_access.html): opens its menu of premium chart features.
   An item opens the Live chart, /chart?feature=<key>, for the current coin and interval (read from the page via
   data-coin-from / data-tf-from); on the Live chart itself (window.PremiumWorkspace) it switches the feature on in
   place. */
(function () {
  "use strict";
  if (window.__sfmPremiumAccess) return;
  window.__sfmPremiumAccess = true;

  function close(root, focus) {
    const btn = root.querySelector(".pa-btn"), pop = root.querySelector(".pa-pop");
    if (pop.hidden) return;
    pop.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    document.body.classList.remove("pa-lock");
    if (focus) btn.focus({ preventScroll: true });
  }
  function open(root) {
    document.querySelectorAll("[data-pa]").forEach((r) => { if (r !== root) close(r); });
    const btn = root.querySelector(".pa-btn"), pop = root.querySelector(".pa-pop");
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    if (window.matchMedia("(max-width: 760px)").matches) document.body.classList.add("pa-lock");
    place(root);
    const first = pop.querySelector(".pa-item");
    if (first) setTimeout(() => first.focus({ preventScroll: true }), 20);
  }
  // keep the menu on screen: open to the left when the button sits near the right edge
  function place(root) {
    const pop = root.querySelector(".pa-pop");
    pop.classList.remove("pa-left");
    if (window.matchMedia("(max-width: 760px)").matches) return;
    const r = root.getBoundingClientRect();
    if (r.left + 380 > window.innerWidth - 8) pop.classList.add("pa-left");
  }
  function current(root) {
    let coin = null, tf = null;
    const cSel = root.dataset.coinFrom, tSel = root.dataset.tfFrom;
    try {
      const c = cSel && document.querySelector(cSel);
      if (c) coin = (c.dataset.coin || c.textContent || "").trim().replace(/\s+chart$/i, "");
      const t = tSel && document.querySelector(tSel);
      if (t) tf = t.dataset.tf || t.textContent.trim();
    } catch (e) { /* a bad selector: open without them */ }
    if (coin && !coin.includes("/")) coin = coin.toUpperCase() + "/USDT";
    return { coin, tf };
  }
  function go(root, key) {
    if (window.PremiumWorkspace && typeof window.PremiumWorkspace.feature === "function") {
      close(root);
      window.PremiumWorkspace.feature(key);
      return true;
    }
    const { coin, tf } = current(root), q = new URLSearchParams();
    if (key) q.set("feature", key);
    if (coin) q.set("coin", coin);
    if (tf) q.set("tf", tf);
    location.href = "/chart" + (q.toString() ? "?" + q.toString() : "");
    return true;
  }

  function init(root) {
    const btn = root.querySelector(".pa-btn"), pop = root.querySelector(".pa-pop");
    btn.addEventListener("click", (e) => { e.stopPropagation(); if (pop.hidden) open(root); else close(root, true); });
    pop.addEventListener("click", (e) => {
      if (e.target.closest("[data-pa-close]")) { close(root, true); return; }
      const a = e.target.closest("a[data-feature]");
      if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
      e.preventDefault();
      go(root, a.dataset.feature);
    });
    pop.addEventListener("keydown", (e) => {
      const items = [...pop.querySelectorAll(".pa-item, .pa-open")];
      const i = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); (items[i + 1] || items[0]).focus(); }
      if (e.key === "ArrowUp") { e.preventDefault(); (items[i - 1] || items[items.length - 1]).focus(); }
    });
  }
  document.addEventListener("click", (e) => {
    document.querySelectorAll("[data-pa]").forEach((root) => {
      if (!root.contains(e.target)) close(root);
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    document.querySelectorAll("[data-pa]").forEach((root) => {
      if (!root.querySelector(".pa-pop").hidden) { e.stopPropagation(); close(root, true); }
    });
  }, true);
  window.addEventListener("resize", () => document.querySelectorAll("[data-pa]").forEach((r) => { if (!r.querySelector(".pa-pop").hidden) place(r); }));

  function start() { document.querySelectorAll("[data-pa]").forEach(init); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
