/* Strategies pages: small helpers and the SVG charts (no chart library). */
(function () {
  "use strict";
  var SF = {};
  var NS = "http://www.w3.org/2000/svg";
  var OV = ["var(--ov1)", "var(--ov2)", "var(--ov3)", "var(--ov4)"];
  SF.OV = OV;

  SF.esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  SF.fmtR = function (x, d) {
    if (x == null || isNaN(x)) return "--";
    var v = Number(x);
    return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d == null ? 2 : d) + "R";
  };
  SF.pct = function (x) { return x == null || isNaN(x) ? "--" : Number(x).toFixed(1) + "%"; };
  SF.date = function (iso) {
    var d = new Date(iso);
    return isNaN(d) ? "--" : d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
  };
  SF.ago = function (iso) {
    var d = new Date(iso);
    if (isNaN(d)) return "";
    var s = (Date.now() - d.getTime()) / 1000;
    if (s < 90) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400 * 2) return Math.round(s / 3600) + " hours ago";
    if (s < 86400 * 30) return Math.round(s / 86400) + " days ago";
    return SF.date(iso);
  };

  function el(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function finite(v) { return v != null && isFinite(v); }
  function mapY(arr, y) { return (arr || []).map(function (v) { return finite(v) ? y(v) : null; }); }
  function range(arrs, pad) {
    var lo = Infinity, hi = -Infinity;
    arrs.forEach(function (a) { (a || []).forEach(function (v) { if (finite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; } }); });
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (hi - lo < 1e-12) { hi = lo + Math.abs(lo || 1) * 0.01; lo = lo - Math.abs(lo || 1) * 0.01; }
    var p = (hi - lo) * (pad || 0.06);
    return [lo - p, hi + p];
  }
  function line(xs, ys) {
    var d = "", pen = false;
    for (var i = 0; i < ys.length; i++) {
      if (!finite(ys[i])) { pen = false; continue; }
      d += (pen ? "L" : "M") + xs[i].toFixed(1) + " " + ys[i].toFixed(1);
      pen = true;
    }
    return d;
  }
  var uid = 0;
  function clip(svg, x, y, w, h) {
    var id = "sfclip" + (++uid);
    var defs = el("defs", {}, svg);
    var cp = el("clipPath", { id: id }, defs);
    el("rect", { x: x, y: y, width: w, height: h }, cp);
    return "url(#" + id + ")";
  }
  function clear(svg) { while (svg.firstChild) svg.removeChild(svg.firstChild); }
  /* drawing width = the element's real width, so the labels keep their size on phones */
  function widthOf(svg, fallback) {
    var w = svg.getBoundingClientRect ? svg.getBoundingClientRect().width : 0;
    return Math.round(w > 200 ? w : fallback);
  }

  /* priceBox: {x, y, w, h} ; values: closes; returns y mapper. Overlays may stretch the scale only a little. */
  function priceScale(closes, extra, box) {
    var base = range([closes], 0.04);
    var all = range([closes].concat(extra || []), 0.04);
    var span = base[1] - base[0];
    var lo = Math.max(all[0], base[0] - span * 0.35), hi = Math.min(all[1], base[1] + span * 0.35);
    return function (v) { return box.y + box.h - (v - lo) / (hi - lo) * box.h; };
  }

  function oscPanel(svg, osc, xs, box, legend) {
    var lines = osc.lines || {};
    var names = Object.keys(lines);
    var vals = names.map(function (k) { return lines[k]; }).concat([osc.levels || []]);
    var r = range(vals, 0.08);
    var y = function (v) { return box.y + box.h - (v - r[0]) / (r[1] - r[0]) * box.h; };
    el("rect", { x: box.x, y: box.y, width: box.w, height: box.h, fill: "var(--panel-raised)", "fill-opacity": "0.5", rx: 4 }, svg);
    (osc.levels || []).forEach(function (lv) {
      el("line", { x1: box.x, x2: box.x + box.w, y1: y(lv), y2: y(lv), stroke: "var(--text-faint)", "stroke-dasharray": "3 4",
        "stroke-width": 1, "vector-effect": "non-scaling-stroke" }, svg);
    });
    var cp = clip(svg, box.x, box.y, box.w, box.h);
    names.forEach(function (k, i) {
      el("path", { d: line(xs, mapY(lines[k], y)), fill: "none", stroke: OV[i % OV.length], "stroke-width": 1.4,
        "clip-path": cp, "vector-effect": "non-scaling-stroke" }, svg);
      if (legend) legend.push({ name: k, color: OV[i % OV.length] });
    });
  }

  function marker(svg, x, yv, side, size) {
    var s = size || 5;
    var p = side === "L"
      ? [x, yv + 3, x - s, yv + 3 + s * 1.6, x + s, yv + 3 + s * 1.6]
      : [x, yv - 3, x - s, yv - 3 - s * 1.6, x + s, yv - 3 - s * 1.6];
    el("polygon", { points: p.map(function (v) { return v.toFixed(1); }).join(" "),
      fill: side === "L" ? "var(--long)" : "var(--short)" }, svg);
  }

  /* Card chart: closing line, up to two lines of the strategy, its entries (and an oscillator panel if no lines). */
  SF.mini = function (svg, pv) {
    clear(svg);
    var W = 320, H = 180;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("preserveAspectRatio", "none");
    if (!pv || !pv.c || pv.c.length < 2) return;
    var n = pv.c.length, pad = 6;
    var hasOsc = pv.osc && pv.osc.lines && Object.keys(pv.osc.lines).length;
    var pbox = { x: pad, y: 10, w: W - pad * 2, h: hasOsc ? 104 : 156 };
    var xs = pv.c.map(function (_, i) { return pad + i * (W - pad * 2) / (n - 1); });
    var ov = pv.overlays || {};
    var names = Object.keys(ov);
    var y = priceScale(pv.c, names.map(function (k) { return ov[k]; }), pbox);
    var cp = clip(svg, 0, pbox.y - 4, W, pbox.h + 8);
    var ys = mapY(pv.c, y);
    var d = line(xs, ys);
    el("path", { d: d + "L" + xs[n - 1].toFixed(1) + " " + (pbox.y + pbox.h) + "L" + xs[0].toFixed(1) + " " + (pbox.y + pbox.h) + "Z",
      fill: "var(--chart-price)", "fill-opacity": "0.08", stroke: "none", "clip-path": cp }, svg);
    el("path", { d: d, fill: "none", stroke: "var(--chart-price)", "stroke-width": 1.3, "clip-path": cp,
      "vector-effect": "non-scaling-stroke" }, svg);
    names.forEach(function (k, i) {
      el("path", { d: line(xs, mapY(ov[k], y)), fill: "none", stroke: OV[i % OV.length], "stroke-width": 1.3,
        "clip-path": cp, "vector-effect": "non-scaling-stroke" }, svg);
    });
    (pv.entries || []).forEach(function (m) {
      var i = m[0];
      if (i >= 0 && i < n && finite(pv.c[i])) marker(svg, xs[i], ys[i], m[1], 4);
    });
    if (hasOsc) oscPanel(svg, pv.osc, xs, { x: pad, y: 124, w: W - pad * 2, h: 48 });
  };

  /* Detail chart: candles, every line of the strategy, entries and exits, oscillator panel. Returns the legend. */
  SF.candles = function (svg, pv) {
    clear(svg);
    var legend = [];
    if (!pv || !pv.c || pv.c.length < 2) return legend;
    var n = pv.c.length;
    var hasOsc = pv.osc && pv.osc.lines && Object.keys(pv.osc.lines).length;
    var W = widthOf(svg, 1000), padL = 8, padR = W < 600 ? 58 : 70;
    var ph = Math.round(Math.max(220, Math.min(330, W * 0.33))), oh = Math.round(ph * 0.33);
    var H = hasOsc ? ph + oh + 32 : ph + 30;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    var pbox = { x: padL, y: 10, w: W - padL - padR, h: ph };
    var step = pbox.w / n;
    var xs = pv.c.map(function (_, i) { return padL + step * (i + 0.5); });
    var ov = pv.overlays || {};
    var names = Object.keys(ov);
    var y = priceScale((pv.h || pv.c).concat(pv.l || []), names.map(function (k) { return ov[k]; }), pbox);
    // price labels on the right
    var lo = Infinity, hi = -Infinity;
    (pv.l || pv.c).forEach(function (v) { if (finite(v) && v < lo) lo = v; });
    (pv.h || pv.c).forEach(function (v) { if (finite(v) && v > hi) hi = v; });
    for (var t = 0; t <= 4; t++) {
      var v = lo + (hi - lo) * t / 4;
      el("line", { x1: pbox.x, x2: pbox.x + pbox.w, y1: y(v), y2: y(v), stroke: "var(--line)", "stroke-width": 1 }, svg);
      var tx = el("text", { x: W - padR + 8, y: y(v) + 4, fill: "var(--text-faint)", "font-size": 12, "font-family": "IBM Plex Mono, monospace" }, svg);
      tx.textContent = Number(v.toPrecision(6)).toString();
    }
    var cp = clip(svg, pbox.x, pbox.y - 4, pbox.w, pbox.h + 8);
    var g = el("g", { "clip-path": cp }, svg);
    var bw = Math.max(1, step * 0.62);
    for (var i = 0; i < n; i++) {
      var o = (pv.o || [])[i], h = (pv.h || [])[i], l = (pv.l || [])[i], c = pv.c[i];
      if (![o, h, l, c].every(finite)) continue;
      var up = c >= o, col = up ? "var(--long)" : "var(--short)";
      el("line", { x1: xs[i], x2: xs[i], y1: y(h), y2: y(l), stroke: col, "stroke-width": 1 }, g);
      var top = y(Math.max(o, c)), bot = y(Math.min(o, c));
      el("rect", { x: (xs[i] - bw / 2).toFixed(1), y: top.toFixed(1), width: bw.toFixed(1), height: Math.max(1, bot - top).toFixed(1),
        fill: col, "fill-opacity": up ? 0.85 : 0.85 }, g);
    }
    names.forEach(function (k, j) {
      var col = OV[j % OV.length];
      el("path", { d: line(xs, mapY(ov[k], y)), fill: "none", stroke: col, "stroke-width": 1.6 }, g);
      legend.push({ name: k, color: col });
    });
    (pv.exits || []).forEach(function (m) {
      var i = m[0];
      if (i >= 0 && i < n && finite(pv.c[i])) el("circle", { cx: xs[i], cy: y(pv.c[i]), r: 3.2, fill: "var(--void)",
        stroke: "var(--text-dim)", "stroke-width": 1.4 }, svg);
    });
    (pv.entries || []).forEach(function (m) {
      var i = m[0];
      if (i >= 0 && i < n) marker(svg, xs[i], m[1] === "L" ? y(pv.l[i]) : y(pv.h[i]), m[1], 6);
    });
    if (hasOsc) oscPanel(svg, pv.osc, xs, { x: pbox.x, y: pbox.y + ph + 12, w: pbox.w, h: oh }, legend);
    return legend;
  };

  /* Equity curve in R (cumulative result of every trade, all symbols together). */
  SF.curve = function (svg, curve) {
    clear(svg);
    var W = widthOf(svg, 1000), H = Math.round(Math.max(180, Math.min(240, W * 0.3))), padL = 56, padR = 12, top = 12, bot = 24;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    if (!curve || curve.length < 2) {
      var tx = el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", fill: "var(--text-faint)", "font-size": 14 }, svg);
      tx.textContent = "Not enough trades for a curve.";
      return;
    }
    var vals = [0].concat(curve.map(function (p) { return p[1]; }));
    var r = range([vals], 0.08);
    var n = curve.length;
    var x = function (i) { return padL + (W - padL - padR) * (i + 1) / n; };
    var y = function (v) { return top + (H - top - bot) * (1 - (v - r[0]) / (r[1] - r[0])); };
    var last = curve[n - 1][1];
    var col = last >= 0 ? "var(--long)" : "var(--short)";
    el("line", { x1: padL, x2: W - padR, y1: y(0), y2: y(0), stroke: "var(--text-faint)", "stroke-dasharray": "4 4", "stroke-width": 1 }, svg);
    [r[0], r[1]].forEach(function (v) {
      var t = el("text", { x: padL - 8, y: y(v) + 4, "text-anchor": "end", fill: "var(--text-faint)", "font-size": 12,
        "font-family": "IBM Plex Mono, monospace" }, svg);
      t.textContent = SF.fmtR(v, 0);
    });
    var t0 = el("text", { x: padL - 8, y: y(0) + 4, "text-anchor": "end", fill: "var(--text-faint)", "font-size": 12,
      "font-family": "IBM Plex Mono, monospace" }, svg);
    t0.textContent = "0R";
    var xs = [padL].concat(curve.map(function (_, i) { return x(i); }));
    var ys = vals.map(y);
    var d = line(xs, ys);
    el("path", { d: d + "L" + xs[xs.length - 1].toFixed(1) + " " + y(0).toFixed(1) + "L" + padL + " " + y(0).toFixed(1) + "Z",
      fill: col, "fill-opacity": 0.12, stroke: "none" }, svg);
    el("path", { d: d, fill: "none", stroke: col, "stroke-width": 2 }, svg);
  };

  SF.api = function (url, opts) {
    var o = Object.assign({ credentials: "same-origin", headers: { Accept: "application/json" } }, opts || {});
    return fetch(url, o).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: "Unexpected answer from the server (" + r.status + ")." }; })
        .then(function (d) { d.status = r.status; return d; });
    }, function () { return { ok: false, error: "Could not reach the server. Check your connection.", status: 0 }; });
  };
  SF.postJSON = function (url, body) {
    return SF.api(url, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}) });
  };
  SF.toLogin = function () { location.href = "/login?next=" + encodeURIComponent(location.pathname); };

  /* Like buttons: <button data-like data-target="lib:slug" data-logged-in="1"> with a <b> for the count. */
  SF.bindLike = function (btn) {
    if (!btn) return;
    btn.addEventListener("click", function () {
      if (btn.getAttribute("data-logged-in") !== "1") return SF.toLogin();
      btn.disabled = true;
      SF.postJSON("/api/strategies/like", { target: btn.getAttribute("data-target") }).then(function (d) {
        btn.disabled = false;
        if (d.status === 401) return SF.toLogin();
        if (!d.ok) return alert(d.error || "Could not save.");
        btn.classList.toggle("liked", !!d.liked);
        btn.setAttribute("aria-pressed", d.liked ? "true" : "false");
        var b = btn.querySelector("b");
        if (b) b.textContent = d.likes;
      });
    });
  };

  window.SF = SF;
})();
