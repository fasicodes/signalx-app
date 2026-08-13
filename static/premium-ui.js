/* Signal FM Premium UI helpers — presentation only. */
(function(){
  "use strict";
  if (window.__signalFmPremiumUI) return;
  window.__signalFmPremiumUI = true;

  const makeTip = (el) => {
    if (!el || el.dataset.premiumTipReady === "1") return;
    const raw = el.getAttribute("title") || el.getAttribute("aria-label");
    if (!raw || raw.length > 80) return;

    el.dataset.premiumTipReady = "1";
    el.setAttribute("data-premium-tooltip", raw);
    if (el.hasAttribute("title")) el.dataset.originalTitle = raw;
    el.removeAttribute("title");

    const show = () => {
      if (!document.body || el.disabled) return;
      document.querySelectorAll(".premium-tooltip").forEach(n => n.remove());
      const tip = document.createElement("div");
      tip.className = "premium-tooltip";
      tip.textContent = raw;
      document.body.appendChild(tip);
      const r = el.getBoundingClientRect();
      const t = tip.getBoundingClientRect();
      let left = r.left + (r.width - t.width) / 2;
      left = Math.max(8, Math.min(left, window.innerWidth - t.width - 8));
      let top = r.top - t.height - 10;
      if (top < 8) top = r.bottom + 10;
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
      requestAnimationFrame(()=>tip.classList.add("is-visible"));
      el.__premiumTip = tip;
    };
    const hide = () => {
      const tip = el.__premiumTip;
      if (tip) {
        tip.classList.remove("is-visible");
        setTimeout(()=>tip.remove(), 140);
        el.__premiumTip = null;
      }
    };

    el.addEventListener("mouseenter", show);
    el.addEventListener("mouseleave", hide);
    el.addEventListener("focus", show);
    el.addEventListener("blur", hide);
    el.addEventListener("click", hide);
  };

  const scan = () => {
    document.querySelectorAll("[title], [aria-label]").forEach(makeTip);
  };

  const inject = () => {
    if (!document.getElementById("premium-tooltip-style")) {
      const s = document.createElement("style");
      s.id = "premium-tooltip-style";
      s.textContent = `
        .premium-tooltip{
          position:fixed;z-index:99999;max-width:240px;padding:8px 11px;
          border-radius:9px;border:1px solid color-mix(in srgb,var(--long) 24%,var(--line));
          background:color-mix(in srgb,var(--panel-raised) 97%,transparent);
          color:var(--text);box-shadow:0 12px 32px rgba(0,0,0,.24);
          backdrop-filter:blur(12px);font:600 11px/1.35 var(--mono,monospace);
          letter-spacing:.15px;pointer-events:none;opacity:0;
          transform:translateY(4px);transition:opacity .14s ease,transform .14s ease;
        }
        .premium-tooltip.is-visible{opacity:1;transform:translateY(0);}
      `;
      document.head.appendChild(s);
    }
  };

  const start = () => {
    inject();
    scan();
    const mo = new MutationObserver(() => scan());
    mo.observe(document.body, {childList:true, subtree:true});
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, {once:true});
  } else {
    start();
  }
})();
