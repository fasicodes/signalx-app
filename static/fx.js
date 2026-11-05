/* Signals FM futuristic AI motion, loaded on every page after ds.js (styles in fx.css).
   1. Page progress   a thin neon bar at the top that follows the scroll
   2. Boot line       a small "system ready" chip once per browser session
   3. Headings        the page title gets a neon gradient on its last words and "decodes" in; section headings decode
                      when they scroll into view. The final text is set as aria-label during the effect.
   4. Card spotlight  a soft glow follows the pointer over cards
   5. Reveal          cards below the fold fade up when they scroll into view
   Only headings made of plain text are touched. Everything is skipped for prefers-reduced-motion
   (except the static gradient) and nothing here changes data or layout. */
(function () {
  "use strict";
  if (window.__fx) return;
  window.__fx = true;
  const reduce = (() => { try { return matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; } })();
  const fine = (() => { try { return matchMedia("(hover: hover) and (pointer: fine)").matches; } catch (e) { return false; } })();
  const ready = (fn) => (document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", fn) : fn());

  const CARDS = ".d-card, .res-card, .coins-demo, .spec, .icard, .scard, .plan, .ex-card, .panel, .box, .about-card, .leadership-card, .mk-card, " +
    ".lq-card, .perf-metric-card, .tool, .mbox, .qa, .card, .feat, .stat";

  ready(() => {
    // 1. progress
    let bar = document.querySelector(".fx-progress");
    if (!bar) { bar = document.createElement("div"); bar.className = "fx-progress"; bar.setAttribute("aria-hidden", "true"); document.body.appendChild(bar); }
    const prog = () => {
      const h = document.documentElement, max = h.scrollHeight - innerHeight;
      bar.style.transform = `scaleX(${max > 40 ? Math.min(1, h.scrollTop / max) : 0})`;
    };
    addEventListener("scroll", prog, { passive: true }); prog();

    // 2. boot line
    let seen = false;
    try { seen = sessionStorage.getItem("fx-boot") === "1"; sessionStorage.setItem("fx-boot", "1"); } catch (e) { seen = true; }
    if (!seen && !reduce) {
      const b = document.createElement("div");
      b.className = "fx-boot"; b.setAttribute("aria-hidden", "true");
      let name = (document.title.split(/[|:·]/)[0] || "Signals FM").trim();
      if (name.length > 28) name = name.slice(0, 27).trim() + "…";
      b.innerHTML = `<i></i>System ready &middot; ${name.replace(/[<>&]/g, "")}`;
      document.body.appendChild(b);
      setTimeout(() => b.classList.add("fx-gone"), 1900);
      setTimeout(() => b.remove(), 2600);
    }

    // 3. headings
    const plain = (el) => el && el.children.length === 0 && el.textContent.trim().length > 2 && el.textContent.length < 90;
    const h1 = document.querySelector("main h1, .lp h1, h1");
    if (h1 && !h1.querySelector(".fx-grad") && plain(h1)) {
      const words = h1.textContent.trim().split(/\s+/);
      if (words.length >= 2) {
        const n = words.length > 6 ? 3 : words.length > 2 ? 2 : 1;
        const head = words.slice(0, -n).join(" "), tail = words.slice(-n).join(" ");
        h1.textContent = head + " ";
        const sp = document.createElement("span"); sp.className = "fx-grad"; sp.textContent = tail;
        h1.appendChild(sp);
      }
    }
    if (!reduce) {
      const GLYPHS = "01<>/\\[]{}#$%*+=?ABCDEFXYZ";
      function decode(el) {
        if (el.dataset.fxDone) return;
        el.dataset.fxDone = "1";
        // decode each plain text node inside the heading, keeping any spans (like .fx-grad)
        const nodes = [];
        const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
        while (walk.nextNode()) if (walk.currentNode.nodeValue.trim()) nodes.push([walk.currentNode, walk.currentNode.nodeValue]);
        if (!nodes.length) return;
        const label = el.textContent;
        el.setAttribute("aria-label", label);
        el.classList.add("fx-decoding");
        const total = nodes.reduce((s, [, t]) => s + t.length, 0), dur = Math.min(900, 300 + total * 14), t0 = performance.now();
        function step(now) {
          const k = Math.min(1, (now - t0) / dur);
          let seenN = 0;
          nodes.forEach(([node, txt]) => {
            let out = "";
            for (let i = 0; i < txt.length; i++) {
              const ch = txt[i], pos = (seenN + i) / total;
              out += ch === " " || pos < k ? ch : (pos < k + 0.18 ? GLYPHS[(Math.random() * GLYPHS.length) | 0] : ch === "\n" ? ch : " ");
            }
            node.nodeValue = out; seenN += txt.length;
          });
          if (k < 1) requestAnimationFrame(step);
          else { nodes.forEach(([node, txt]) => (node.nodeValue = txt)); el.classList.remove("fx-decoding"); el.removeAttribute("aria-label"); }
        }
        requestAnimationFrame(step);
      }
      const heads = Array.from(document.querySelectorAll("h1, main h2, .lp h2, .d-h2, .mk-h2")).filter((el) => {
        const txt = el.textContent;
        return txt.trim().length > 2 && txt.length < 90 && !el.querySelector("button, a, input, svg, img") && !el.closest("[aria-live], .chap");
      });
      if ("IntersectionObserver" in window) {
        const io = new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { io.unobserve(e.target); decode(e.target); } }), { threshold: 0.4 });
        heads.forEach((h) => io.observe(h));
      }
    }

    // 4. spotlight
    if (fine && !reduce) {
      let hot = null;
      document.addEventListener("pointermove", (e) => {
        const el = e.target && e.target.closest ? e.target.closest(CARDS) : null;
        if (hot && hot !== el) hot.classList.remove("fx-hot");
        hot = el;
        if (!el) return;
        if (!el.classList.contains("fx-spot")) el.classList.add("fx-spot");
        const r = el.getBoundingClientRect();
        el.style.setProperty("--fx-mx", (e.clientX - r.left) + "px");
        el.style.setProperty("--fx-my", (e.clientY - r.top) + "px");
        el.classList.add("fx-hot");
      }, { passive: true });
    }

    // 5. reveal (only cards that start below the fold, so nothing visible ever blinks)
    if (!reduce && "IntersectionObserver" in window) {
      const io = new IntersectionObserver((es) => es.forEach((e) => {
        if (!e.isIntersecting) return;
        io.unobserve(e.target);
        e.target.classList.add("fx-in");
        e.target.classList.remove("fx-pre");
        setTimeout(() => { e.target.classList.remove("fx-in"); e.target.style.transitionDelay = ""; }, 1200);
      }), { rootMargin: "0px 0px -8% 0px" });
      let i = 0;
      document.querySelectorAll(CARDS).forEach((el) => {
        if (el.closest("[data-reveal], .story, .an-side, .an-pop, [hidden]")) return;
        const r = el.getBoundingClientRect();
        if (r.top > innerHeight * 1.02 && r.height > 0) {
          el.classList.add("fx-pre");
          el.style.transitionDelay = ((i++ % 4) * 70) + "ms";
          io.observe(el);
        }
      });
    }
  });
})();
