/* Signals FM design system: small progressive enhancements. Nothing here is required for a page to work.
   1. Number fields   <input type="number" data-prefix="$" data-suffix="%" data-ds-stepper="0.25" data-ds-slider="0.25,5,0.25">
                      get a unit adornment, + / - buttons (hold to repeat), an optional slider and a plain-language range message.
   2. Live results    [data-ds-live]  highlights only the result values that really changed.
   3. Price ticks     [data-ds-tick]  shows a brief up / down mark when a displayed number changes.
   4. Scroll reveal   [data-reveal]   fades in sections that start below the fold.
   5. Count-up        [data-ds-count] a final, published figure counts up once when it first scrolls into view.
   Everything respects prefers-reduced-motion and never delays input. */
(function () {
  "use strict";
  var reduce = false;
  try { reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) {}

  function decimals(step) {
    var s = String(step);
    var i = s.indexOf(".");
    return i < 0 ? 0 : s.length - i - 1;
  }
  function fire(el) {
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* ---------- 1. number fields ---------- */
  var uid = 0;
  function enhanceNumber(input) {
    if (!input || input.dataset.dsDone) return;
    var wantsStep = input.hasAttribute("data-ds-stepper");
    var attrNum = function (n) { var a = input.getAttribute(n); var v = a == null || a === "" ? null : parseFloat(a); return v == null || isNaN(v) ? null : v; };
    var clean = function () { return parseFloat(String(input.value).replace(/,/g, "")); };
    var pre = input.getAttribute("data-prefix");
    var suf = input.getAttribute("data-suffix");
    var slider = input.getAttribute("data-ds-slider");
    if (!wantsStep && !pre && !suf && !slider) return;
    input.dataset.dsDone = "1";
    uid += 1;

    var wrap = el("div", "ds-num");
    input.parentNode.insertBefore(wrap, input);
    if (pre) wrap.appendChild(el("span", "ds-num-pre", pre));
    wrap.appendChild(input);
    if (suf) wrap.appendChild(el("span", "ds-num-suf", suf));
    input.classList.add("ds-num-input");
    if (input.getAttribute("inputmode") == null) input.setAttribute("inputmode", "decimal");

    var prev = wrap.previousElementSibling;
    if (prev && prev.tagName === "LABEL" && !prev.htmlFor && input.id) prev.htmlFor = input.id;

    var stepBase = parseFloat(input.getAttribute("data-ds-stepper"));
    if (!(stepBase > 0)) stepBase = parseFloat(input.getAttribute("step")) > 0 ? parseFloat(input.getAttribute("step")) : 1;
    var dp = decimals(stepBase);

    function nudge(dir) {
      var v = clean();
      if (isNaN(v)) v = 0;
      v = v + dir * stepBase;
      var min = attrNum("min");
      var max = attrNum("max");
      if (min != null && v < min) v = min;
      if (max != null && v > max) v = max;
      input.value = String(+v.toFixed(Math.min(8, Math.max(dp, 2))));
      fire(input);
    }

    if (wantsStep) {
      var steps = el("span", "ds-num-steps");
      [["Increase", 1, "M12 5v14M5 12h14"], ["Decrease", -1, "M5 12h14"]].forEach(function (d) {
        var b = el("button", "ds-num-step");
        b.type = "button";
        b.tabIndex = -1;
        b.setAttribute("aria-label", d[0]);
        b.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="' + d[2] + '"/></svg>';
        var timer = null, rep = null;
        function stop() { clearTimeout(timer); clearInterval(rep); timer = rep = null; }
        b.addEventListener("pointerdown", function (e) {
          if (e.button !== 0) return;
          nudge(d[1]);
          timer = setTimeout(function () { rep = setInterval(function () { nudge(d[1]); }, 70); }, 420);
        });
        ["pointerup", "pointerleave", "pointercancel", "blur"].forEach(function (n) { b.addEventListener(n, stop); });
        b.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); nudge(d[1]); } });
        steps.appendChild(b);
      });
      wrap.appendChild(steps);
    }

    var msg = el("div", "ds-num-msg");
    msg.id = "dsmsg" + uid;
    msg.setAttribute("role", "alert");
    msg.hidden = true;

    var range = null;
    if (slider) {
      var p = slider.split(",").map(parseFloat);
      range = el("input", "ds-range");
      range.type = "range";
      range.min = p[0]; range.max = p[1]; range.step = p[2] || stepBase;
      var lbl = prev && prev.tagName === "LABEL" ? prev.textContent.trim() : "Value";
      range.setAttribute("aria-label", lbl + " slider");
      range.tabIndex = 0;
      var paint = function () {
        var lo = parseFloat(range.min), hi = parseFloat(range.max);
        var v = Math.min(hi, Math.max(lo, parseFloat(range.value)));
        range.style.setProperty("--fill", ((v - lo) / (hi - lo) * 100).toFixed(1) + "%");
      };
      range.addEventListener("input", function () { input.value = range.value; paint(); fire(input); });
      input.addEventListener("input", function () {
        var v = clean();
        if (!isNaN(v)) { range.value = v; paint(); }
      });
      range.value = isNaN(clean()) ? range.min : clean();
      paint();
      /* a page that fills the field from code (loading saved settings, a reset button) does not fire "input": follow it anyway */
      var native = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
      if (native && native.set) {
        Object.defineProperty(input, "value", {
          configurable: true,
          get: function () { return native.get.call(this); },
          set: function (v) {
            native.set.call(this, v);
            var n = clean();
            if (!isNaN(n)) { range.value = n; paint(); }
          }
        });
      }
      wrap.parentNode.insertBefore(range, wrap.nextSibling);
    }
    (range || wrap).parentNode.insertBefore(msg, (range || wrap).nextSibling);

    function check() {
      var raw = input.value;
      var text = "";
      if (raw !== "") {
        var v = clean();
        var min = attrNum("min");
        var max = attrNum("max");
        if (isNaN(v) || (input.validity && input.validity.badInput)) text = "Enter a number.";
        else if (min != null && v < min) text = "Must be " + min + " or more.";
        else if (max != null && v > max) text = "Must be " + max + " or less.";
      }
      wrap.classList.toggle("is-invalid", !!text);
      if (text) { input.setAttribute("aria-invalid", "true"); input.setAttribute("aria-describedby", msg.id); }
      else { input.removeAttribute("aria-invalid"); input.removeAttribute("aria-describedby"); }
      msg.textContent = text;
      msg.hidden = !text;
    }
    input.addEventListener("blur", check);
    input.addEventListener("input", function () { if (wrap.classList.contains("is-invalid")) check(); });
    input.addEventListener("focus", function () { wrap.classList.add("is-focus"); });
    input.addEventListener("blur", function () { wrap.classList.remove("is-focus"); });
  }

  function enhance(root) {
    (root || document).querySelectorAll("input[type=number], input[data-ds-num]").forEach(enhanceNumber);
  }

  /* ---------- 2. live results ---------- */
  /* Inside a [data-ds-live] box, a value that really changed gets one soft highlight. Three kinds are tracked:
     metric cards (.perf-metric-card), label/value pairs (dt + dd) and headline numbers (.big, [data-ds-flash]). */
  function flash(node, cls) {
    node.classList.remove(cls);
    void node.offsetWidth;
    node.classList.add(cls);
  }
  function watchLive(box) {
    if (box.dataset.dsWatch) return;
    box.dataset.dsWatch = "1";
    var last = {};
    function snapshot(mark) {
      var seen = {};
      box.querySelectorAll(".perf-metric-card").forEach(function (card) {
        var lab = card.querySelector(".perf-metric-label");
        var val = card.querySelector(".perf-metric-value");
        if (!lab || !val) return;
        var k = "c:" + lab.textContent.trim(), v = val.textContent.trim();
        seen[k] = v;
        if (mark && !reduce && last[k] != null && last[k] !== v) flash(card, "ds-changed");
      });
      box.querySelectorAll("dl dt").forEach(function (dt) {
        var dd = dt.nextElementSibling;
        if (!dd || dd.tagName !== "DD") return;
        var k = "d:" + dt.textContent.trim(), v = dd.textContent.trim();
        seen[k] = v;
        if (mark && !reduce && last[k] != null && last[k] !== v) flash(dd, "ds-changed-text");
      });
      box.querySelectorAll(".big, [data-ds-flash]").forEach(function (big, i) {
        var k = "b:" + (big.id || i), v = big.textContent.trim();
        seen[k] = v;
        big.classList.toggle("is-empty", v === "\u2014" || v === "");
        if (mark && !reduce && last[k] != null && last[k] !== v) flash(big, "ds-changed-text");
      });
      last = seen;
    }
    snapshot(false);
    var mo = new MutationObserver(function () { mo.disconnect(); snapshot(true); mo.observe(box, { childList: true, subtree: true, characterData: true }); });
    mo.observe(box, { childList: true, subtree: true, characterData: true });
  }

  /* ---------- 3. price ticks ---------- */
  function num(t) {
    var n = parseFloat(String(t).replace(/[^0-9.\-]/g, ""));
    return isNaN(n) ? null : n;
  }
  function watchTick(node) {
    if (node.dataset.dsTickWatch) return;
    node.dataset.dsTickWatch = "1";
    var prev = num(node.textContent);
    var t = null;
    var mo = new MutationObserver(function () {
      var cur = num(node.textContent);
      if (cur == null) { prev = null; return; }
      if (prev != null && cur !== prev && !reduce) {
        node.classList.remove("ds-tick-up", "ds-tick-down");
        void node.offsetWidth;
        node.classList.add(cur > prev ? "ds-tick-up" : "ds-tick-down");
        clearTimeout(t);
        t = setTimeout(function () { node.classList.remove("ds-tick-up", "ds-tick-down"); }, 900);
      }
      prev = cur;
    });
    mo.observe(node, { childList: true, characterData: true, subtree: true });
  }

  /* ---------- 4. scroll reveal ---------- */
  function reveal() {
    var nodes = document.querySelectorAll("[data-reveal]");
    if (!nodes.length || reduce || !("IntersectionObserver" in window)) return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.05 });
    nodes.forEach(function (n) {
      if (n.getBoundingClientRect().top > window.innerHeight * 0.92) { n.classList.add("ds-reveal"); io.observe(n); }
    });
  }

  /* ---------- 5. count-up ---------- */
  /* [data-ds-count] on an element whose first text is a number ("63.4%", "1,204", "+0.34R"): the number counts up once,
     the first time it is on screen, and always ends on exactly the text that was in the page. Only for figures that are
     already final (a published test result); live prices and signals never count up. */
  var NUM = /^([^0-9+\-\u2212]*)([+\-\u2212]?)(\d[\d,]*)(\.\d+)?([\s\S]*)$/;
  function countUp(node) {
    if (node.dataset.dsCounted) return;
    node.dataset.dsCounted = "1";
    if (reduce) return;
    var tn = null;
    for (var i = 0; i < node.childNodes.length; i++) {
      if (node.childNodes[i].nodeType === 3 && /\d/.test(node.childNodes[i].nodeValue)) { tn = node.childNodes[i]; break; }
    }
    if (!tn) return;
    var original = tn.nodeValue;
    var m = NUM.exec(original);
    if (!m) return;
    var grouped = m[3].indexOf(",") >= 0;
    var dec = m[4] ? m[4].length - 1 : 0;
    var target = parseFloat(m[3].replace(/,/g, "") + (m[4] || ""));
    if (!isFinite(target) || target === 0) return;
    var w = node.getBoundingClientRect().width;
    if (w) node.style.minWidth = Math.ceil(w) + "px";
    var start = null, dur = 750;
    function fmt(v) {
      var t = v.toFixed(dec);
      if (grouped) { var p = t.split("."); p[0] = p[0].replace(/\B(?=(\d{3})+(?!\d))/g, ","); t = p.join("."); }
      return m[1] + m[2] + t + m[5];
    }
    function step(ts) {
      if (start == null) start = ts;
      var k = Math.min(1, (ts - start) / dur);
      var e = 1 - Math.pow(1 - k, 3);
      tn.nodeValue = k < 1 ? fmt(target * e) : original;
      if (k < 1) requestAnimationFrame(step);
      else node.style.minWidth = "";
    }
    tn.nodeValue = fmt(0);
    requestAnimationFrame(step);
  }
  function counts() {
    var nodes = document.querySelectorAll("[data-ds-count]");
    if (!nodes.length || reduce || !("IntersectionObserver" in window)) return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { countUp(en.target); io.unobserve(en.target); }
      });
    }, { threshold: 0.6 });
    nodes.forEach(function (n) { io.observe(n); });
  }

  function init() {
    enhance(document);
    document.querySelectorAll("[data-ds-live]").forEach(watchLive);
    document.querySelectorAll("[data-ds-tick]").forEach(watchTick);
    reveal();
    counts();
  }
  window.DS = { enhance: enhance, watchLive: watchLive, watchTick: watchTick, countUp: countUp };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
