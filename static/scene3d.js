/* Signals FM 3D background and interactive card tilt.
   One fixed WebGL canvas sits behind the page content. It draws one of four decorative scenes:
     candles  a moving 3D landscape of green and red candles
     globe    a dotted globe with orbit rings, coins and trade arcs
     coins    floating, spinning coins
     wave     a market-wave field of points and lines
   The scenes are art, not market data: no price, number or label is drawn, and the candle shapes come from a fixed pattern.
   The landing page changes scene per section; other pages get one scene each (see PAGES). A page can choose its own
   scene with <html data-scene3d="globe"> or turn it off with data-scene3d="off".
   Plain WebGL, no library. It stops when the tab is hidden, draws one still frame for prefers-reduced-motion, runs at
   30 fps on phones and small CPUs, skips Save-Data connections, and leaves the page untouched when WebGL is missing. */
(function () {
  "use strict";
  if (window.__ds3d) return;
  window.__ds3d = true;

  var root = document.documentElement;
  var mm = function (q) { try { return window.matchMedia(q).matches; } catch (e) { return false; } };
  var reduce = mm("(prefers-reduced-motion: reduce)");
  var finePointer = mm("(hover: hover) and (pointer: fine)");

  /* ---------------- interactive card tilt (works even without WebGL) ---------------- */
  var TILT = ".lp .ex-card, .lp .res-card, .lp .feat, .lp .coins-demo, .lp .spec, .lp .steps li, .plan, .icard, .scard, [data-tilt]";
  function setupTilt() {
    if (reduce || !finePointer) return;
    var MAX = 6;
    document.addEventListener("pointermove", function (e) {
      var el = e.target && e.target.closest ? e.target.closest(TILT) : null;
      if (!el) return;
      var r = el.getBoundingClientRect();
      if (r.width < 40 || r.height < 40) return;
      var x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
      // big blocks tilt less so text stays easy to read
      var k = Math.min(1, 420 / Math.max(r.width, r.height));
      el.classList.add("ds-tilt");
      el.style.setProperty("--tilt-x", ((0.5 - y) * MAX * k).toFixed(2) + "deg");
      el.style.setProperty("--tilt-y", ((x - 0.5) * MAX * k).toFixed(2) + "deg");
      el.style.setProperty("--glare-x", (x * 100).toFixed(1) + "%");
      el.style.setProperty("--glare-y", (y * 100).toFixed(1) + "%");
    }, { passive: true });
    document.addEventListener("pointerout", function (e) {
      var el = e.target && e.target.closest ? e.target.closest(TILT) : null;
      if (!el || (e.relatedTarget && el.contains(e.relatedTarget))) return;
      el.style.setProperty("--tilt-x", "0deg");
      el.style.setProperty("--tilt-y", "0deg");
      el.classList.remove("ds-tilt");
    }, { passive: true });
  }
  setupTilt();

  /* ---------------- which scene ---------------- */
  var PAGES = [
    [/^\/$/, "landing"],
    [/^\/(advanced|chart|dashboard)/, "wave"],
    [/^\/track-record/, "wave"],
    [/^\/signals/, "candles"],
    [/^\/strateg/, "candles"],
    [/^\/(coins|tools)/, "coins"],
    [/^\/(login|reset-password|register|signup)/, "coins"],
    [/^\/(demo-trading|auto-trading|pro-insights|liquidity-scanner)/, "globe"]
  ];
  // the landing page: the scene follows the section in the middle of the screen
  var LANDING = [["hero", "candles"], ["how", "wave"], ["coins", "coins"], ["terminal", "globe"], ["results", "wave"],
    ["features", "candles"], ["faq", "globe"], ["pricing", "coins"]];
  // how strong the scene shows through; data-heavy pages stay quiet
  var STRENGTH = { landing: 0.9, wave: 0.55, candles: 0.6, coins: 0.7, globe: 0.65 };
  var QUIET = /^\/(advanced|chart|dashboard|liquidity-scanner)/;

  var path = location.pathname.replace(/\/+$/, "") || "/";
  var choice = root.getAttribute("data-scene3d") || (document.body && document.body.getAttribute("data-scene3d")) || "";
  if (!choice) {
    choice = "globe";
    for (var i = 0; i < PAGES.length; i++) if (PAGES[i][0].test(path)) { choice = PAGES[i][1]; break; }
  }
  if (choice === "off") return;
  var conn = navigator.connection || navigator.mozConnection;
  if (conn && conn.saveData) return;

  var small = Math.min(window.innerWidth, window.innerHeight) < 700 || (navigator.hardwareConcurrency || 4) <= 2;
  var FPS = small ? 30 : 60;

  /* ---------------- WebGL setup ---------------- */
  var canvas = document.createElement("canvas");
  canvas.className = "ds3d";
  canvas.setAttribute("aria-hidden", "true");
  var gl = null;
  try {
    gl = canvas.getContext("webgl", { antialias: !small, alpha: true, premultipliedAlpha: true, powerPreference: "low-power" }) ||
      canvas.getContext("experimental-webgl");
  } catch (e) { gl = null; }
  if (!gl) return;

  var VS = [
    "attribute vec3 aPos; attribute vec3 aNor; attribute vec3 aCol;",
    "uniform mat4 uP, uV, uM; uniform float uTime, uWave, uPt; uniform mediump float uMode;",
    "varying vec3 vCol; varying float vLit; varying float vFog;",
    "void main(){",
    "  vec3 p = aPos;",
    "  if (uWave > 0.5) {",
    "    p.y += sin(p.x*0.55 + uTime*0.9)*0.42 + cos(p.z*0.7 + uTime*0.6)*0.32 + sin((p.x+p.z)*0.23 + uTime*0.4)*0.55;",
    "  }",
    "  vec4 w = uM * vec4(p, 1.0);",
    "  vec4 v = uV * w;",
    "  gl_Position = uP * v;",
    "  if (uMode > 0.5) { vLit = 1.0; } else {",
    "    vec3 n = normalize((uM * vec4(aNor, 0.0)).xyz);",
    "    vLit = 0.38 + 0.62 * max(dot(n, normalize(vec3(0.35, 0.85, 0.55))), 0.0);",
    "    vLit += 0.25 * pow(max(dot(n, normalize(vec3(-0.6, 0.2, 0.8))), 0.0), 6.0);",
    "  }",
    "  vCol = aCol;",
    "  vFog = clamp((-v.z - 7.0) / 20.0, 0.0, 0.92);",
    "  gl_PointSize = uPt * 12.0 / max(-v.z, 0.5);",
    "}"
  ].join("\n");
  var FS = [
    "precision mediump float;",
    "varying vec3 vCol; varying float vLit; varying float vFog;",
    "uniform float uAlpha, uMode;",
    "void main(){",
    "  float a = uAlpha * (1.0 - vFog);",
    "  if (uMode > 0.5 && uMode < 1.5) {",
    "    vec2 d = gl_PointCoord - 0.5; float r = dot(d, d);",
    "    if (r > 0.25) discard;",
    "    a *= smoothstep(0.25, 0.02, r);",
    "  }",
    "  vec3 c = vCol * vLit;",
    "  gl_FragColor = vec4(c * a, a);",
    "}"
  ].join("\n");

  function shader(type, src) {
    var s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  }
  var prog, A = {}, U = {};
  try {
    prog = gl.createProgram();
    gl.attachShader(prog, shader(gl.VERTEX_SHADER, VS));
    gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error("link");
  } catch (e) { if (window.console) console.warn("3D background off:", e && e.message); return; }
  gl.useProgram(prog);
  ["aPos", "aNor", "aCol"].forEach(function (n) { A[n] = gl.getAttribLocation(prog, n); });
  ["uP", "uV", "uM", "uTime", "uWave", "uPt", "uMode", "uAlpha"].forEach(function (n) { U[n] = gl.getUniformLocation(prog, n); });
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
  gl.enable(gl.DEPTH_TEST);
  gl.depthFunc(gl.LEQUAL);

  /* ---------------- small matrix kit (column-major) ---------------- */
  function ident() { return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]; }
  function mul(a, b) {
    var o = new Array(16);
    for (var c = 0; c < 4; c++) for (var r = 0; r < 4; r++) {
      o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
    }
    return o;
  }
  function persp(fov, asp, n, f) {
    var t = 1 / Math.tan(fov / 2), d = 1 / (n - f);
    return [t / asp, 0, 0, 0, 0, t, 0, 0, 0, 0, (f + n) * d, -1, 0, 0, 2 * f * n * d, 0];
  }
  function lookAt(e, c) {
    var z = norm([e[0] - c[0], e[1] - c[1], e[2] - c[2]]), x = norm(cross([0, 1, 0], z)), y = cross(z, x);
    return [x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0,
      -dot(x, e), -dot(y, e), -dot(z, e), 1];
  }
  function norm(v) { var l = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; }
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
  function trans(x, y, z) { var m = ident(); m[12] = x; m[13] = y; m[14] = z; return m; }
  function rotX(a) { var c = Math.cos(a), s = Math.sin(a); return [1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1]; }
  function rotY(a) { var c = Math.cos(a), s = Math.sin(a); return [c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]; }
  function rotZ(a) { var c = Math.cos(a), s = Math.sin(a); return [c, s, 0, 0, -s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]; }
  function scl(s) { return [s, 0, 0, 0, 0, s, 0, 0, 0, 0, s, 0, 0, 0, 0, 1]; }
  function chain() { var m = ident(); for (var i = 0; i < arguments.length; i++) m = mul(m, arguments[i]); return m; }

  /* ---------------- colours from the design tokens ---------------- */
  var COL = {};
  function hexOf(v, fallback) {
    v = (v || "").trim();
    var m = v.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
    if (m) {
      var h = m[1].length === 3 ? m[1].replace(/./g, "$&$&") : m[1];
      return [parseInt(h.slice(0, 2), 16) / 255, parseInt(h.slice(2, 4), 16) / 255, parseInt(h.slice(4, 6), 16) / 255];
    }
    m = v.match(/rgba?\(([^)]+)\)/);
    if (m) { var p = m[1].split(",").map(parseFloat); return [p[0] / 255, p[1] / 255, p[2] / 255]; }
    return fallback;
  }
  function readColors() {
    var cs = getComputedStyle(root);
    var light = root.getAttribute("data-theme") === "light";
    COL.light = light;
    COL.up = hexOf(cs.getPropertyValue("--ds-up"), [0, 0.94, 0.54]);
    COL.down = hexOf(cs.getPropertyValue("--ds-down"), [1, 0.23, 0.3]);
    COL.gold = hexOf(cs.getPropertyValue("--ds-gold"), [1, 0.83, 0.23]);
    COL.brand = hexOf(cs.getPropertyValue("--ds-brand"), [0, 1, 0.53]);
    COL.line = hexOf(cs.getPropertyValue("--ds-line"), [0.15, 0.17, 0.15]);
    COL.dim = light ? [0.55, 0.6, 0.56] : [0.32, 0.38, 0.34];
  }
  function shade(c, k) { return [c[0] * k, c[1] * k, c[2] * k]; }
  function mix(a, b, t) { return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]; }

  /* ---------------- geometry ---------------- */
  function Builder() { this.d = []; }
  Builder.prototype.v = function (p, n, c) { this.d.push(p[0], p[1], p[2], n[0], n[1], n[2], c[0], c[1], c[2]); };
  Builder.prototype.tri = function (a, b, c, n, col) { this.v(a, n, col); this.v(b, n, col); this.v(c, n, col); };
  Builder.prototype.quad = function (a, b, c, d, n, col) { this.tri(a, b, c, n, col); this.tri(a, c, d, n, col); };
  Builder.prototype.box = function (cx, cy, cz, sx, sy, sz, col) {
    var x0 = cx - sx / 2, x1 = cx + sx / 2, y0 = cy - sy / 2, y1 = cy + sy / 2, z0 = cz - sz / 2, z1 = cz + sz / 2;
    var top = shade(col, 1.08), side = col;
    this.quad([x0, y1, z1], [x1, y1, z1], [x1, y1, z0], [x0, y1, z0], [0, 1, 0], top);
    this.quad([x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1], [0, -1, 0], side);
    this.quad([x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1], [0, 0, 1], side);
    this.quad([x1, y0, z0], [x0, y0, z0], [x0, y1, z0], [x1, y1, z0], [0, 0, -1], side);
    this.quad([x1, y0, z1], [x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [1, 0, 0], side);
    this.quad([x0, y0, z0], [x0, y0, z1], [x0, y1, z1], [x0, y1, z0], [-1, 0, 0], side);
  };
  // a coin: a short cylinder on the Y axis with a raised inner ring on both faces
  Builder.prototype.coin = function (r, h, seg, face, edge) {
    var self = this;
    function disc(y, rad, ny, col) {
      for (var i = 0; i < seg; i++) {
        var a0 = i / seg * Math.PI * 2, a1 = (i + 1) / seg * Math.PI * 2;
        var p0 = [Math.cos(a0) * rad, y, Math.sin(a0) * rad], p1 = [Math.cos(a1) * rad, y, Math.sin(a1) * rad];
        if (ny > 0) self.tri([0, y, 0], p1, p0, [0, 1, 0], col); else self.tri([0, y, 0], p0, p1, [0, -1, 0], col);
      }
    }
    function band(y0, y1, rad, col) {
      for (var i = 0; i < seg; i++) {
        var a0 = i / seg * Math.PI * 2, a1 = (i + 1) / seg * Math.PI * 2;
        var n0 = [Math.cos(a0), 0, Math.sin(a0)], n1 = [Math.cos(a1), 0, Math.sin(a1)];
        var b0 = [n0[0] * rad, y0, n0[2] * rad], b1 = [n1[0] * rad, y0, n1[2] * rad];
        var t0 = [n0[0] * rad, y1, n0[2] * rad], t1 = [n1[0] * rad, y1, n1[2] * rad];
        self.v(b0, n0, col); self.v(t1, n1, col); self.v(b1, n1, col);
        self.v(b0, n0, col); self.v(t0, n0, col); self.v(t1, n1, col);
      }
    }
    var hh = h / 2, lip = h * 0.22;
    band(-hh, hh, r, edge);
    disc(hh, r, 1, shade(face, 0.82)); disc(-hh, r, -1, shade(face, 0.82));
    band(hh, hh + lip, r * 0.74, shade(face, 0.9)); disc(hh + lip, r * 0.74, 1, face);
    band(-hh - lip, -hh, r * 0.74, shade(face, 0.9)); disc(-hh - lip, r * 0.74, -1, face);
    band(hh + lip, hh + lip * 1.6, r * 0.3, edge); disc(hh + lip * 1.6, r * 0.3, 1, shade(face, 1.1));
    band(-hh - lip * 1.6, -hh - lip, r * 0.3, edge); disc(-hh - lip * 1.6, r * 0.3, -1, shade(face, 1.1));
  };
  function mesh(b, mode) {
    var buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(b.d), gl.STATIC_DRAW);
    return { buf: buf, count: b.d.length / 9, mode: mode == null ? gl.TRIANGLES : mode };
  }
  function draw(m, model, opt) {
    opt = opt || {};
    gl.bindBuffer(gl.ARRAY_BUFFER, m.buf);
    gl.vertexAttribPointer(A.aPos, 3, gl.FLOAT, false, 36, 0);
    gl.vertexAttribPointer(A.aNor, 3, gl.FLOAT, false, 36, 12);
    gl.vertexAttribPointer(A.aCol, 3, gl.FLOAT, false, 36, 24);
    gl.uniformMatrix4fv(U.uM, false, new Float32Array(model || ident()));
    gl.uniform1f(U.uMode, m.mode === gl.POINTS ? 1 : (m.mode === gl.LINES || m.mode === gl.LINE_STRIP || m.mode === gl.LINE_LOOP) ? 2 : 0);
    gl.uniform1f(U.uWave, opt.wave ? 1 : 0);
    gl.uniform1f(U.uPt, opt.pt || 1);
    gl.uniform1f(U.uAlpha, opt.alpha == null ? 1 : opt.alpha);
    gl.depthMask(m.mode === gl.TRIANGLES);
    gl.drawArrays(m.mode, 0, m.count);
  }
  [A.aPos, A.aNor, A.aCol].forEach(function (l) { gl.enableVertexAttribArray(l); });

  function rng(seed) {
    return function () {
      seed |= 0; seed = seed + 0x6D2B79F5 | 0;
      var t = Math.imul(seed ^ seed >>> 15, 1 | seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  var Z = [0, 0, 0];

  /* ---------------- scenes ---------------- */
  var SCENES = {};

  // a decorative landscape of candles; the shape repeats so it can scroll forever
  SCENES.candles = function () {
    var r = rng(7), N = small ? 48 : 72, lanes = small ? 3 : 5, gap = 0.44, L = N * gap;
    var amp = [], ph = [];
    for (var k = 1; k <= 6; k++) { amp.push((0.9 + r()) / (k * 0.8)); ph.push(r() * 6.28); }
    var b = new Builder();
    for (var lane = 0; lane < lanes; lane++) {
      var z = -lane * 1.5, off = lane * 1.7;
      var price = function (i) {
        var s = 0;
        for (var k = 0; k < 6; k++) s += amp[k] * Math.sin(2 * Math.PI * (k + 1) * (i / N) + ph[k] + off);
        return s * 0.55;
      };
      for (var i = 0; i < N; i++) {
        var o = price(i - 1), c = price(i), up = c >= o;
        var hi = Math.max(o, c) + 0.05 + 0.18 * r(), lo = Math.min(o, c) - 0.05 - 0.18 * r();
        var col = up ? COL.up : COL.down;
        var x = -L / 2 + i * gap;
        b.box(x, (o + c) / 2, z, 0.26, Math.max(Math.abs(c - o), 0.05), 0.26, col);
        b.box(x, (hi + lo) / 2, z, 0.045, hi - lo, 0.045, shade(col, 0.8));
      }
    }
    var solid = mesh(b);
    var g = new Builder(), gy = -2.2, gc = shade(COL.brand, COL.light ? 0.5 : 0.28);
    for (var gx = -L / 2; gx <= L / 2 + 0.01; gx += gap * 4) { g.v([gx, gy, 2], Z, gc); g.v([gx, gy, -lanes * 1.5 - 2], Z, gc); }
    for (var gz = 2; gz >= -lanes * 1.5 - 2; gz -= 1.2) { g.v([-L / 2, gy, gz], Z, gc); g.v([L / 2, gy, gz], Z, gc); }
    var grid = mesh(g, gl.LINES);
    return {
      eye: [0, 2.6, 8.2], at: [0, 0.2, -2.5],
      frame: function (t) {
        var x = -((t * 0.42) % L);
        draw(grid, trans(x, 0, 0), { alpha: 0.7 }); draw(grid, trans(x + L, 0, 0), { alpha: 0.7 });
        draw(solid, trans(x, 0, 0)); draw(solid, trans(x + L, 0, 0));
      }
    };
  };

  // a dotted globe with orbit rings, coins riding the rings and arcs between points
  SCENES.globe = function () {
    var r = rng(11), n = small ? 760 : 1500, R = 2.7, b = new Builder();
    var pts = [];
    for (var i = 0; i < n; i++) {
      var y = 1 - (i / (n - 1)) * 2, rad = Math.sqrt(1 - y * y), th = i * 2.399963;
      var p = [Math.cos(th) * rad * R, y * R, Math.sin(th) * rad * R];
      pts.push(p);
      var k = r();
      b.v(p, Z, k > 0.94 ? COL.gold : shade(COL.brand, 0.6 + 0.4 * r()));
    }
    var dots = mesh(b, gl.POINTS);
    var a = new Builder();
    for (var j = 0; j < 7; j++) {
      var p0 = pts[(r() * n) | 0], p1 = pts[(r() * n) | 0], prev = null;
      for (var s = 0; s <= 28; s++) {
        var t = s / 28, q = norm([p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t, p0[2] + (p1[2] - p0[2]) * t]);
        var lift = R * (1 + 0.32 * Math.sin(Math.PI * t)), cur = [q[0] * lift, q[1] * lift, q[2] * lift];
        if (prev) { a.v(prev, Z, COL.gold); a.v(cur, Z, COL.gold); }
        prev = cur;
      }
    }
    var arcs = mesh(a, gl.LINES);
    var rg = new Builder();
    [3.6, 4.3].forEach(function (rr) {
      for (var s = 0; s < 96; s++) {
        var a0 = s / 96 * Math.PI * 2, a1 = (s + 1) / 96 * Math.PI * 2;
        rg.v([Math.cos(a0) * rr, 0, Math.sin(a0) * rr], Z, shade(COL.brand, 0.55));
        rg.v([Math.cos(a1) * rr, 0, Math.sin(a1) * rr], Z, shade(COL.brand, 0.55));
      }
    });
    var rings = mesh(rg, gl.LINES);
    var cb = new Builder(); cb.coin(0.26, 0.06, 28, COL.gold, shade(COL.gold, 0.62));
    var coin = mesh(cb);
    var gb = new Builder(); gb.coin(0.2, 0.05, 24, COL.brand, shade(COL.brand, 0.55));
    var gcoin = mesh(gb);
    var tilt = rotX(0.38);
    return {
      eye: [0, 0.6, 9.4], at: [0, 0, 0],
      frame: function (t) {
        var spin = rotY(t * 0.12);
        var m = chain(rotZ(-0.18), tilt, spin);
        draw(dots, m, { pt: small ? 4 : 3.6 });
        draw(arcs, m, { alpha: 0.75 });
        var ringM = chain(rotZ(-0.18), rotX(1.18));
        draw(rings, ringM, { alpha: 0.6 });
        for (var i = 0; i < 4; i++) {
          var rr = i % 2 ? 4.3 : 3.6, a = t * (0.35 - i * 0.05) + i * 1.6;
          var pos = chain(ringM, trans(Math.cos(a) * rr, 0, Math.sin(a) * rr), rotX(-1.18), rotY(t * 1.2 + i), rotX(1.5708));
          draw(i % 2 ? gcoin : coin, pos);
        }
      }
    };
  };

  // floating coins with drifting specks behind them
  SCENES.coins = function () {
    var r = rng(5), b = new Builder(), g = new Builder(), p = new Builder();
    b.coin(0.62, 0.12, 40, COL.gold, shade(COL.gold, 0.6));
    g.coin(0.5, 0.1, 36, COL.brand, shade(COL.brand, 0.5));
    var gold = mesh(b), green = mesh(g);
    for (var i = 0; i < (small ? 160 : 320); i++) {
      p.v([(r() - 0.5) * 22, (r() - 0.5) * 12, -2 - r() * 10], Z, r() > 0.8 ? COL.gold : shade(COL.brand, 0.5 + r() * 0.4));
    }
    var specks = mesh(p, gl.POINTS);
    var list = [];
    var spots = [[-5.2, 1.6, -1], [-2.6, -1.8, 0.6], [0.4, 2.4, -3], [2.8, -0.6, 0.8], [5.4, 1.8, -1.6],
      [-6.4, -2.4, -4], [6.6, -2.2, -3.4], [-0.8, -3, -5], [1.6, 0.6, -6.5], [4.2, 3.4, -6]];
    var count = small ? 6 : spots.length;
    for (var j = 0; j < count; j++) {
      list.push({ p: spots[j], gold: j % 3 !== 1, s: 0.4 + r() * 0.6, ph: r() * 6.28, tilt: (r() - 0.5) * 0.9 });
    }
    return {
      eye: [0, 0, 9], at: [0, 0, -1],
      frame: function (t) {
        draw(specks, trans(0, (t * 0.12) % 3 - 1.5, 0), { pt: small ? 3.6 : 3.2, alpha: 0.85 });
        list.forEach(function (c) {
          var m = chain(trans(c.p[0], c.p[1] + Math.sin(t * 0.8 + c.ph) * 0.28, c.p[2]), rotZ(c.tilt),
            rotY(t * c.s + c.ph), rotX(1.5708));
          draw(c.gold ? gold : green, m);
        });
      }
    };
  };

  // a market-wave field: points plus a few running lines, moved in the vertex shader
  SCENES.wave = function () {
    var cols = small ? 60 : 96, rows = small ? 28 : 44, sp = 0.32, b = new Builder(), l = new Builder();
    for (var zi = 0; zi < rows; zi++) for (var xi = 0; xi < cols; xi++) {
      var x = (xi - cols / 2) * sp, z = -zi * sp + 2.5, t = xi / cols;
      b.v([x, -1.4, z], Z, mix(shade(COL.brand, 0.55), COL.gold, Math.max(0, t - 0.7) * 2.2));
    }
    for (var row = 2; row < rows; row += 6) for (var xj = 0; xj < cols - 1; xj++) {
      var z2 = -row * sp + 2.5, c = shade(COL.brand, 0.42);
      l.v([(xj - cols / 2) * sp, -1.4, z2], Z, c); l.v([(xj + 1 - cols / 2) * sp, -1.4, z2], Z, c);
    }
    var field = mesh(b, gl.POINTS), lines = mesh(l, gl.LINES);
    return {
      eye: [0, 2.4, 7.4], at: [0, -0.6, -3],
      frame: function (t) {
        draw(lines, ident(), { wave: true, alpha: 0.6 });
        draw(field, ident(), { wave: true, pt: small ? 3.8 : 3.4 });
      }
    };
  };

  /* ---------------- render loop ---------------- */
  var built = {}, current = null, scene = null, raf = 0, last = 0, start = performance.now(), frozenAt = 1.5;
  var pointer = { x: 0, y: 0, tx: 0, ty: 0 }, W = 0, H = 0;

  function getScene(name) {
    if (!built[name]) built[name] = SCENES[name]();
    return built[name];
  }
  function size() {
    var dpr = Math.min(window.devicePixelRatio || 1, small ? 1.25 : 1.6);
    W = Math.round(window.innerWidth * dpr); H = Math.round(window.innerHeight * dpr);
    if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
    gl.viewport(0, 0, W, H);
  }
  function render(now) {
    if (!scene) return;
    var t = reduce ? frozenAt : (now - start) / 1000;
    pointer.x += (pointer.tx - pointer.x) * 0.05;
    pointer.y += (pointer.ty - pointer.y) * 0.05;
    var asp = W / Math.max(H, 1);
    // narrow screens: step back so the scene still fits
    var back = asp < 1 ? 1 + (1 - asp) * 0.9 : 1;
    var e = scene.eye, at = scene.at;
    var eye = [e[0] * back + pointer.x * 0.9, e[1] * back + pointer.y * 0.55, e[2] * back];
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.uniformMatrix4fv(U.uP, false, new Float32Array(persp(0.82, asp, 0.1, 60)));
    gl.uniformMatrix4fv(U.uV, false, new Float32Array(lookAt(eye, at)));
    gl.uniform1f(U.uTime, t);
    scene.frame(t);
  }
  function loop(now) {
    raf = 0;
    if (document.hidden) return;
    if (now - last >= 1000 / FPS - 2) { last = now; render(now); }
    raf = requestAnimationFrame(loop);
  }
  function kick() {
    if (reduce) { render(performance.now()); return; }
    if (!raf && !document.hidden) raf = requestAnimationFrame(loop);
  }

  var fadeTimer = 0;
  function show(name) {
    if (!SCENES[name] || name === current) return;
    var first = current === null;
    current = name;
    var swap = function () {
      try { scene = getScene(name); } catch (e) { scene = null; return; }
      canvas.style.setProperty("--ds3d-alpha", strength(name));
      render(performance.now());
      canvas.classList.add("is-on");
      kick();
    };
    clearTimeout(fadeTimer);
    if (first || reduce) { swap(); return; }
    canvas.classList.remove("is-on");
    fadeTimer = setTimeout(swap, 320);
  }
  function strength(name) {
    var s = choice === "landing" ? STRENGTH.landing : (STRENGTH[name] || 0.6);
    if (QUIET.test(path)) s *= 0.6;
    if (COL.light) s *= 0.55;
    return s.toFixed(2);
  }

  function boot() {
    readColors();
    document.body.insertBefore(canvas, document.body.firstChild);
    root.classList.add("has-3d");
    size();
    if (choice === "landing") {
      var marks = [];
      LANDING.forEach(function (pair) {
        var el = pair[0] === "hero" ? document.querySelector(".lp .hero, .hero") : document.getElementById(pair[0]);
        if (el) { el.setAttribute("data-scene3d-part", pair[1]); marks.push(el); }
      });
      show(marks.length ? marks[0].getAttribute("data-scene3d-part") : "candles");
      if (marks.length && "IntersectionObserver" in window) {
        var io = new IntersectionObserver(function (entries) {
          entries.forEach(function (en) { if (en.isIntersecting) show(en.target.getAttribute("data-scene3d-part")); });
        }, { rootMargin: "-45% 0px -45% 0px" });
        marks.forEach(function (m) { io.observe(m); });
      }
    } else {
      show(choice);
    }

    window.addEventListener("resize", function () { size(); if (reduce) render(performance.now()); }, { passive: true });
    if (!reduce) {
      window.addEventListener("pointermove", function (e) {
        pointer.tx = (e.clientX / window.innerWidth - 0.5) * 2;
        pointer.ty = -(e.clientY / window.innerHeight - 0.5) * 2;
      }, { passive: true });
    }
    document.addEventListener("visibilitychange", function () { if (!document.hidden) kick(); });
    // theme switch: rebuild with the new colours
    new MutationObserver(function () {
      readColors();
      for (var k in built) if (built.hasOwnProperty(k)) delete built[k];
      var name = current; current = null; clearTimeout(fadeTimer);
      if (name) {
        current = name;
        try { scene = getScene(name); } catch (e) { scene = null; }
        canvas.style.setProperty("--ds3d-alpha", strength(name));
        render(performance.now());
      }
    }).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    canvas.addEventListener("webglcontextlost", function (e) {
      e.preventDefault(); scene = null; cancelAnimationFrame(raf); raf = 0;
      canvas.classList.remove("is-on"); root.classList.remove("has-3d");
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
