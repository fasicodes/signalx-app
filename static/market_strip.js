/* Market strip: last price, 24h change and a 24-hour line for the main coins.
   Data comes only from /market-pulse (prices) and /market-pulse/lines (hourly closes), which read the exchange.
   Nothing is invented: when the data cannot be loaded the strip says so, and when a later refresh fails it keeps the
   last values and marks them as out of date. A price flashes green or red only when the new price really differs from
   the one shown before. Markup: <div class="mstrip" data-market-strip> with an empty <ul class="mstrip-list"> and a <p class="mstrip-note">. */
(function () {
  "use strict";
  var roots = Array.prototype.slice.call(document.querySelectorAll("[data-market-strip]"));
  if (!roots.length) return;

  var PRICE_EVERY = 15000, LINES_EVERY = 300000, STALE_AFTER = 60000, RETRY_EVERY = 30000;
  var S = { prices: null, lines: {}, shown: {}, updated: 0, timer: 0, linesAt: 0, busy: false, failed: false };

  function money(p) {
    var d = p >= 1000 ? 0 : p >= 100 ? 2 : p >= 1 ? 3 : p >= 0.1 ? 4 : 5;
    return "$" + p.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function pct(v) {
    if (typeof v !== "number" || !isFinite(v)) return null;
    return { text: (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(2) + "%", up: v >= 0, flat: Math.abs(v) < 0.005 };
  }
  function clock(ms) {
    var d = new Date(ms), z = function (n) { return n < 10 ? "0" + n : "" + n; };
    return z(d.getHours()) + ":" + z(d.getMinutes()) + ":" + z(d.getSeconds());
  }
  function spark(values, up) {
    if (!values || values.length < 2) return "";
    var w = 44, h = 20, pad = 2, lo = Math.min.apply(null, values), hi = Math.max.apply(null, values), span = hi - lo || 1;
    var pts = values.map(function (v, i) {
      return (pad + (i / (values.length - 1)) * (w - 2 * pad)).toFixed(1) + "," + (h - pad - ((v - lo) / span) * (h - 2 * pad)).toFixed(1);
    }).join(" ");
    return '<svg class="mi-spark ' + (up ? "up" : "down") + '" viewBox="0 0 ' + w + " " + h + '" width="' + w + '" height="' + h +
      '" aria-hidden="true" focusable="false"><polyline points="' + pts + '" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }
  function say(root, text, retry) {
    var note = root.querySelector(".mstrip-note");
    if (!note) return;
    note.textContent = text || "";
    if (retry) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "mstrip-retry"; b.textContent = "Try again";
      b.addEventListener("click", function () { load(true); });
      note.appendChild(document.createTextNode(" ")); note.appendChild(b);
    }
  }

  function render(root) {
    var list = root.querySelector(".mstrip-list");
    if (!list || !S.prices) return;
    root.classList.remove("is-loading", "is-failed");
    var first = !list.querySelector(".mi:not(.is-skel)");
    if (first) list.innerHTML = "";
    S.prices.forEach(function (it) {
      var li = list.querySelector('.mi[data-sym="' + it.symbol + '"]');
      if (!li) {
        li = document.createElement("li");
        li.className = "mi"; li.setAttribute("data-sym", it.symbol);
        li.innerHTML = '<span class="mi-sym"></span><span class="mi-ch"></span><span class="mi-px"></span><span class="mi-line"></span>';
        li.querySelector(".mi-sym").textContent = it.symbol;
        list.appendChild(li);
      }
      var c = pct(it.changePercent), px = li.querySelector(".mi-px"), ch = li.querySelector(".mi-ch");
      var prev = S.shown[it.symbol], text = money(it.last);
      if (px.textContent !== text) {
        px.textContent = text;
        if (typeof prev === "number" && prev !== it.last) {
          var cls = it.last > prev ? "ds-tick-up" : "ds-tick-down";
          px.classList.remove("ds-tick-up", "ds-tick-down"); void px.offsetWidth; px.classList.add(cls);
        }
      }
      S.shown[it.symbol] = it.last;
      ch.className = "mi-ch " + (c ? (c.flat ? "flat" : c.up ? "up" : "down") : "none");
      ch.innerHTML = c ? '<i aria-hidden="true">' + (c.flat ? "" : c.up ? "▲" : "▼") + "</i>" + c.text : "—";
      var line = S.lines[it.symbol];
      li.querySelector(".mi-line").innerHTML = line ? spark(line, c ? c.up : line[line.length - 1] >= line[0]) : "";
      li.setAttribute("aria-label", it.symbol + " " + text + (c ? ", " + (c.up ? "up " : "down ") + c.text.replace(/^[+−]/, "") + " in 24 hours" : ", 24 hour change not available"));
    });
  }

  function paintAll() { roots.forEach(render); }
  function status() {
    var age = Date.now() - S.updated;
    roots.forEach(function (root) {
      var stale = S.updated && age > STALE_AFTER;
      root.classList.toggle("is-stale", !!stale);
      if (!S.prices) return;
      say(root, stale
        ? "Prices may be out of date. Last update " + clock(S.updated) + "."
        : "24-hour change, prices from the exchange. Updated " + clock(S.updated) + ".", false);
    });
  }

  function fail() {
    S.failed = true;
    if (S.prices) { status(); return; }
    roots.forEach(function (root) {
      root.classList.remove("is-loading"); root.classList.add("is-failed");
      var list = root.querySelector(".mstrip-list"); if (list) list.innerHTML = "";
      say(root, "Live prices are not available right now.", true);
    });
  }

  function getJSON(url) {
    return fetch(url, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error("http " + r.status); return r.json(); });
  }
  function loadLines() {
    if (Date.now() - S.linesAt < LINES_EVERY - 1000) return;
    S.linesAt = Date.now();
    getJSON("/market-pulse/lines").then(function (d) {
      if (d && d.lines && typeof d.lines === "object") { S.lines = d.lines; paintAll(); }
    }).catch(function () { S.linesAt = Date.now() - LINES_EVERY + 60000; });
  }
  function load(force) {
    if (S.busy || (document.hidden && !force)) return;
    S.busy = true;
    getJSON("/market-pulse").then(function (d) {
      var items = d && Array.isArray(d.items) ? d.items.filter(function (i) { return i && typeof i.last === "number"; }) : [];
      if (!items.length) throw new Error("empty");
      S.prices = items; S.updated = Date.now(); S.failed = false;
      paintAll(); status(); loadLines();
    }).catch(fail).then(function () { S.busy = false; schedule(); });
  }
  function schedule() {
    clearTimeout(S.timer);
    S.timer = setTimeout(function () { load(false); }, S.failed ? RETRY_EVERY : PRICE_EVERY);
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && Date.now() - S.updated > PRICE_EVERY - 1000) load(true);
    else status();
  });
  roots.forEach(function (r) { r.classList.add("is-loading"); });
  load(true);
})();
