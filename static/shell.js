/* Signals FM app shell: side menu, popovers, notifications, "?" explanations and the welcome guide.
   Every part checks that its elements exist, so the same file also runs on public pages (only the "?" tips are used there). */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var root = document.documentElement;
  var body = document.body;
  var wide = window.matchMedia ? window.matchMedia("(min-width: 1024px)") : { matches: false };
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ------------------------------------------------------------------ theme
  var themeBtn = $("an-theme");
  if (themeBtn) themeBtn.addEventListener("click", function () {
    var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    store("signalsfm-theme", next);
    document.dispatchEvent(new CustomEvent("themechange"));
  });

  // ------------------------------------------------------------------ side menu
  var side = $("an-side"), back = $("an-side-backdrop"), burger = $("an-burger"), menuTab = $("an-menu-tab");
  var lastFocus = null;
  function setSide(open) {
    if (!side) return;
    side.classList.toggle("open", open);
    if (back) back.hidden = !open;
    if (menuTab) menuTab.setAttribute("aria-expanded", open ? "true" : "false");
    if (burger && !wide.matches) burger.setAttribute("aria-expanded", open ? "true" : "false");
    body.classList.toggle("an-lock", open);
    if (open) {
      lastFocus = document.activeElement;
      var first = side.querySelector("a, button");
      if (first) first.focus({ preventScroll: true });
    } else if (lastFocus && lastFocus.focus) {
      lastFocus.focus({ preventScroll: true });
      lastFocus = null;
    }
  }
  function syncBurger() {
    if (!burger) return;
    if (wide.matches) {
      var hidden = body.classList.contains("an-collapsed");
      burger.setAttribute("aria-expanded", hidden ? "false" : "true");
      burger.setAttribute("aria-label", hidden ? "Show the menu" : "Hide the menu");
    } else {
      burger.setAttribute("aria-expanded", side && side.classList.contains("open") ? "true" : "false");
      burger.setAttribute("aria-label", "Open menu");
    }
  }
  if (side) {
    if (burger) burger.addEventListener("click", function () {
      if (wide.matches) {
        var collapse = !body.classList.contains("an-collapsed");
        body.classList.toggle("an-collapsed", collapse);
        store("sfm-side", collapse ? "collapsed" : "open");
        syncBurger();
        window.dispatchEvent(new Event("resize"));   // let charts take the new width
      } else {
        setSide(!side.classList.contains("open"));
      }
    });
    if (menuTab) menuTab.addEventListener("click", function () { setSide(!side.classList.contains("open")); });
    if (back) back.addEventListener("click", function () { setSide(false); });
    side.addEventListener("click", function (e) { if (e.target.closest("a")) setSide(false); });
    if (wide.addEventListener) wide.addEventListener("change", function () { setSide(false); syncBurger(); });
    syncBurger();
  }

  // ------------------------------------------------------------------ popovers: notifications + account
  function toggle(btn, pop, open) {
    var willOpen = open === undefined ? pop.hidden : open;
    pop.hidden = !willOpen;
    btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
    return willOpen;
  }
  var pops = [[$("an-bell"), $("an-notes")], [$("an-user"), $("an-menu")]].filter(function (p) { return p[0] && p[1]; });
  pops.forEach(function (p) {
    p[0].addEventListener("click", function (e) {
      e.stopPropagation();
      pops.forEach(function (q) { if (q !== p) toggle(q[0], q[1], false); });
      if (toggle(p[0], p[1]) && p[1].id === "an-notes") loadNotes();
    });
  });
  document.addEventListener("click", function (e) {
    pops.forEach(function (p) { if (!p[1].contains(e.target)) toggle(p[0], p[1], false); });
  });

  function logout() {
    fetch("/api/logout", { method: "POST", credentials: "same-origin" }).finally(function () { location.href = "/"; });
  }
  ["an-logout", "an-side-logout"].forEach(function (id) { if ($(id)) $(id).addEventListener("click", logout); });

  function ago(iso) {
    if (!iso) return "";
    var d = new Date(/Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z");
    var m = Math.max(0, Math.round((Date.now() - d) / 60000));
    if (m < 60) return m + " min ago";
    var h = Math.floor(m / 60);
    return h < 48 ? h + " h ago" : Math.floor(h / 24) + " days ago";
  }
  function badge() {
    if (!$("an-badge")) return;
    fetch("/api/notifications/unread-count", { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
      var n = d.unread_count || 0, b = $("an-badge");
      b.hidden = !n; b.textContent = n > 9 ? "9+" : n;
    }).catch(function () {});
  }
  function loadNotes() {
    fetch("/api/notifications?limit=12", { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
      var list = d.notifications || [];
      $("an-notes-list").innerHTML = list.length ? list.map(function (n) {
        return '<div class="an-note' + (n.is_read ? "" : " unread") + '"><b>' + esc(n.title) + "</b><span>" + esc(n.message) + "</span><small>" + ago(n.created_at) + "</small></div>";
      }).join("") : '<p class="an-empty">No notifications yet. Turn on signal alerts to hear about new signals.</p>';
    }).catch(function () { $("an-notes-list").innerHTML = '<p class="an-empty">Notifications could not load. Try again in a moment.</p>'; });
  }
  if ($("an-readall")) $("an-readall").addEventListener("click", function () {
    fetch("/api/notifications/read-all", { method: "POST", credentials: "same-origin" }).then(function () { badge(); loadNotes(); });
  });
  if ($("an-badge")) { badge(); setInterval(badge, 60000); }

  // ------------------------------------------------------------------ "?" explanations
  var GLOSS = null, glossLoading = null, tipPop = null, tipBtn = null;
  function glossary() {
    if (GLOSS) return Promise.resolve(GLOSS);
    if (!glossLoading) {
      glossLoading = fetch("/glossary.json", { credentials: "same-origin" }).then(function (r) {
        if (!r.ok) throw new Error("bad status");
        return r.json();
      }).then(function (d) { GLOSS = d.terms || d; return GLOSS; }).catch(function (e) { glossLoading = null; throw e; });
    }
    return glossLoading;
  }
  function closeTip(refocus) {
    if (!tipPop || tipPop.hidden) return;
    tipPop.hidden = true;
    if (tipBtn) {
      tipBtn.setAttribute("aria-expanded", "false");
      if (refocus) tipBtn.focus({ preventScroll: true });
    }
    tipBtn = null;
  }
  function placeTip(btn) {
    var r = btn.getBoundingClientRect(), w = tipPop.offsetWidth, h = tipPop.offsetHeight, gap = 8;
    var left = Math.max(12, Math.min(r.left + r.width / 2 - w / 2, window.innerWidth - w - 12));
    var top = r.bottom + gap;
    if (top + h > window.innerHeight - 12 && r.top - gap - h > 12) top = r.top - gap - h;
    tipPop.style.left = left + "px";
    tipPop.style.top = Math.max(12, top) + "px";
  }
  function fillTip(key, g) {
    var t = g && g[key];
    tipPop.setAttribute("aria-label", t ? t.term : "Explanation");
    tipPop.innerHTML = t
      ? "<b>" + esc(t.term) + "</b><p>" + esc(t.text) + '</p><a href="/faq#g-' + encodeURIComponent(key) + '">More in the FAQ</a>'
      : "<p>No explanation for this yet.</p>";
  }
  function openTip(btn) {
    if (!tipPop) {
      tipPop = document.createElement("div");
      tipPop.className = "an-tip-pop";
      tipPop.id = "an-tip-pop";
      tipPop.setAttribute("role", "dialog");
      tipPop.hidden = true;
      document.body.appendChild(tipPop);
    }
    closeTip(false);
    tipBtn = btn;
    btn.setAttribute("aria-expanded", "true");
    btn.setAttribute("aria-controls", "an-tip-pop");
    var key = btn.getAttribute("data-tip");
    tipPop.innerHTML = "<p>Loading…</p>";
    tipPop.hidden = false;
    placeTip(btn);
    glossary().then(function (g) {
      if (tipBtn !== btn) return;
      fillTip(key, g);
      placeTip(btn);
    }).catch(function () {
      if (tipBtn !== btn) return;
      tipPop.innerHTML = "<p>The explanation could not load. Check your connection and try again.</p>";
      placeTip(btn);
    });
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest(".tip[data-tip]") : null;
    if (btn) {
      e.preventDefault();
      e.stopPropagation();
      if (tipBtn === btn && tipPop && !tipPop.hidden) closeTip(false); else openTip(btn);
      return;
    }
    if (tipPop && !tipPop.hidden && !tipPop.contains(e.target)) closeTip(false);
  }, true);
  window.addEventListener("scroll", function () { closeTip(false); }, { passive: true, capture: true });
  window.addEventListener("resize", function () { closeTip(false); });

  // ------------------------------------------------------------------ welcome guide
  var guide = $("an-guide");
  var GUIDE_KEY = "sfm-guide-v1";
  var step = 0, guideOpener = null;
  function steps() { return guide ? guide.querySelectorAll("[data-step]") : []; }
  function showStep(i) {
    var all = steps();
    step = Math.max(0, Math.min(i, all.length - 1));
    Array.prototype.forEach.call(all, function (s, k) { s.hidden = k !== step; });
    var dots = guide.querySelectorAll(".an-gdots i");
    Array.prototype.forEach.call(dots, function (d, k) { d.classList.toggle("on", k === step); });
    $("an-guide-n").textContent = "Step " + (step + 1) + " of " + all.length;
    $("an-guide-back").hidden = step === 0;
    $("an-guide-next").textContent = step === all.length - 1 ? "Start using Signals FM" : "Next";
    var h = all[step].querySelector("h2");
    if (h) { h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: true }); }
  }
  function openGuide(from) {
    if (!guide) return;
    guideOpener = from || document.activeElement;
    setSide(false);
    pops.forEach(function (p) { toggle(p[0], p[1], false); });
    guide.hidden = false;
    body.classList.add("an-lock");
    showStep(0);
  }
  function closeGuide() {
    if (!guide || guide.hidden) return;
    guide.hidden = true;
    body.classList.remove("an-lock");
    store(GUIDE_KEY, "done");
    if (guideOpener && guideOpener.focus) guideOpener.focus({ preventScroll: true });
  }
  if (guide) {
    $("an-guide-next").addEventListener("click", function () {
      if (step >= steps().length - 1) closeGuide(); else showStep(step + 1);
    });
    $("an-guide-back").addEventListener("click", function () { showStep(step - 1); });
    $("an-guide-x").addEventListener("click", closeGuide);
    guide.addEventListener("click", function (e) {
      if (e.target === guide) closeGuide();
      if (e.target.closest("a")) store(GUIDE_KEY, "done");
    });
    guide.addEventListener("keydown", function (e) {
      if (e.key !== "Tab") return;
      var f = Array.prototype.filter.call(guide.querySelectorAll("a[href], button:not([hidden])"), function (el) {
        return el.offsetParent !== null;
      });
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || !guide.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    document.addEventListener("click", function (e) {
      var o = e.target.closest ? e.target.closest("[data-guide-open]") : null;
      if (o) { e.preventDefault(); openGuide(o); }
    });
    if (guide.hasAttribute("data-auto") && store(GUIDE_KEY) !== "done") setTimeout(function () { openGuide(null); }, 700);
  }

  // ------------------------------------------------------------------ Escape closes whatever is open
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (tipPop && !tipPop.hidden) { closeTip(true); return; }
    if (guide && !guide.hidden) { closeGuide(); return; }
    if (side && side.classList.contains("open")) setSide(false);
    pops.forEach(function (p) { toggle(p[0], p[1], false); });
  });
})();
