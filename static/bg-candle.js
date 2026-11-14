/* Signals FM dashboard background: one large candle that forms live, fast.
   Decorative only: the price is a simulated random walk, no number is drawn and it is not market data.
   Each candle lives a few seconds, then closes, flashes and a new one opens at the last price.
   Canvas 2D, no library. Pauses when the tab is hidden; one still frame for prefers-reduced-motion. */
(function () {
  "use strict";
  if (window.__bgCandle) return;
  window.__bgCandle = true;
  const reduce = (() => { try { return matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; } })();

  const cv = document.createElement("canvas");
  cv.className = "bgc"; cv.setAttribute("aria-hidden", "true");
  document.body.prepend(cv);
  const ctx = cv.getContext("2d");
  let W = 0, H = 0, dpr = 1;
  function size() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = innerWidth; H = innerHeight;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  size(); addEventListener("resize", size);

  const GREEN = [52, 211, 153], RED = [244, 86, 108];
  const PERIOD = 5.5;                 // seconds per candle
  // price in abstract units around 0; the view keeps the candle centred
  let open = 0, price = 0, hi = 0, lo = 0, vel = 0, age = 0, flash = 0, trail = [];
  let view = 0, scale = 1;            // smoothed vertical centre and size of the range
  const rnd = () => (Math.random() + Math.random() + Math.random() - 1.5) / 1.5;
  let drift = 0;
  function tick(dt) {
    age += dt;
    // fast, jumpy walk with momentum, occasional bursts, slight pull back to the open so it stays a candle
    if (Math.random() < dt * 0.6) drift = rnd() * 2.2;
    drift *= Math.pow(0.4, dt);
    vel += (rnd() * 9 + drift * 4 - (price - open) * 0.6) * dt;
    vel *= Math.pow(0.08, dt);
    price += vel * dt * 6;
    hi = Math.max(hi, price); lo = Math.min(lo, price);
    trail.push(price); if (trail.length > 260) trail.shift();
    if (age >= PERIOD) { age = 0; flash = 1; open = price; hi = lo = price; }
    flash = Math.max(0, flash - dt * 2.2);
  }

  let t0 = 0, gridOff = 0;
  const parts = Array.from({ length: 46 }, () => ({ x: Math.random(), y: Math.random(), s: Math.random() * 0.6 + 0.2, v: Math.random() * 0.05 + 0.02 }));
  function rgba(c, a) { return `rgba(${c[0]},${c[1]},${c[2]},${a})`; }

  function draw(dt) {
    ctx.clearRect(0, 0, W, H);
    const bull = price >= open, C = bull ? GREEN : RED;
    // ambient glow behind the candle
    const cx = W < 760 ? W * 0.5 : W * 0.68, cy = H * 0.5;
    const glow = ctx.createRadialGradient(cx, cy, 10, cx, cy, Math.max(W, H) * 0.55);
    glow.addColorStop(0, rgba(C, 0.13 + flash * 0.12)); glow.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = glow; ctx.fillRect(0, 0, W, H);

    // perspective grid floor moving towards the viewer
    gridOff = (gridOff + dt * 0.35) % 1;
    const hz = H * 0.62;
    ctx.lineWidth = 1;
    for (let i = 0; i < 14; i++) {
      const z = (i + gridOff) / 14, yy = hz + Math.pow(z, 2.2) * (H - hz);
      ctx.strokeStyle = rgba(GREEN, 0.03 + z * 0.12);
      ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(W, yy); ctx.stroke();
    }
    for (let i = -14; i <= 14; i++) {
      ctx.strokeStyle = rgba(GREEN, 0.07);
      ctx.beginPath(); ctx.moveTo(cx + i * 18, hz); ctx.lineTo(cx + i * (W / 7), H); ctx.stroke();
    }
    // horizon fade
    const hf = ctx.createLinearGradient(0, hz - 40, 0, hz + 60);
    hf.addColorStop(0, "rgba(5,5,5,0)"); hf.addColorStop(0.5, rgba(GREEN, 0.05)); hf.addColorStop(1, "rgba(5,5,5,0)");
    ctx.fillStyle = hf; ctx.fillRect(0, hz - 40, W, 100);

    // floating data particles
    parts.forEach((p) => {
      p.y -= p.v * dt; if (p.y < 0) { p.y = 1; p.x = Math.random(); }
      ctx.fillStyle = rgba(GREEN, 0.18 * p.s);
      ctx.fillRect(p.x * W, p.y * H, 1.5 * p.s + 0.5, 6 * p.s);
    });

    // view follows the candle smoothly
    const range = Math.max(hi - lo, 1.2);
    view += ((hi + lo) / 2 - view) * Math.min(1, dt * 3);
    scale += (range - scale) * Math.min(1, dt * 1.5);
    const span = H * (W < 760 ? 0.34 : 0.46);
    const Y = (v) => cy - (v - view) / Math.max(scale, 1.2) * span * 0.9;

    // price trace leading into the candle
    if (trail.length > 2) {
      const tx0 = cx - Math.min(W * 0.55, 620), n = trail.length;
      const g = ctx.createLinearGradient(tx0, 0, cx, 0);
      g.addColorStop(0, rgba(C, 0)); g.addColorStop(1, rgba(C, 0.55));
      ctx.strokeStyle = g; ctx.lineWidth = 1.5; ctx.beginPath();
      trail.forEach((v, i) => { const x = tx0 + (i / (n - 1)) * (cx - 40 - tx0); i ? ctx.lineTo(x, Y(v)) : ctx.moveTo(x, Y(v)); });
      ctx.stroke();
    }

    // the candle
    const bw = Math.max(46, Math.min(120, W * 0.07)), depth = bw * 0.28;
    const top = Y(Math.max(open, price)), bot = Y(Math.min(open, price)), bh = Math.max(4, bot - top);
    ctx.shadowColor = rgba(C, 0.9); ctx.shadowBlur = 40 + flash * 40;
    // wick
    ctx.strokeStyle = rgba(C, 0.9); ctx.lineWidth = 3;
    ctx.beginPath(); ctx.moveTo(cx, Y(hi)); ctx.lineTo(cx, Y(lo)); ctx.stroke();
    // body (front face)
    const fg = ctx.createLinearGradient(cx - bw / 2, 0, cx + bw / 2, 0);
    fg.addColorStop(0, rgba(C, 1)); fg.addColorStop(0.55, rgba(C.map((x) => x * 0.85), 1)); fg.addColorStop(1, rgba(C.map((x) => x * 0.55), 1));
    ctx.fillStyle = fg; ctx.fillRect(cx - bw / 2, top, bw, bh);
    ctx.shadowBlur = 0;
    // side and top faces for a 3D feel
    ctx.fillStyle = rgba(C.map((x) => x * 0.4), 0.95);
    ctx.beginPath(); ctx.moveTo(cx + bw / 2, top); ctx.lineTo(cx + bw / 2 + depth, top - depth * 0.6); ctx.lineTo(cx + bw / 2 + depth, top + bh - depth * 0.6); ctx.lineTo(cx + bw / 2, top + bh); ctx.closePath(); ctx.fill();
    ctx.fillStyle = rgba(C.map((x) => Math.min(255, x * 1.2 + 30)), 0.95);
    ctx.beginPath(); ctx.moveTo(cx - bw / 2, top); ctx.lineTo(cx - bw / 2 + depth, top - depth * 0.6); ctx.lineTo(cx + bw / 2 + depth, top - depth * 0.6); ctx.lineTo(cx + bw / 2, top); ctx.closePath(); ctx.fill();
    // scanline shimmer over the body
    const sy = top + ((performance.now() / 900) % 1) * bh;
    ctx.fillStyle = "rgba(255,255,255,.18)"; ctx.fillRect(cx - bw / 2, sy, bw, 2);
    // live price line with a pulsing dot
    const py = Y(price);
    ctx.setLineDash([6, 6]); ctx.strokeStyle = rgba(C, 0.45); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(cx + bw / 2 + depth + 10, py); ctx.lineTo(W, py); ctx.stroke(); ctx.setLineDash([]);
    const pr = 4 + Math.sin(performance.now() / 120) * 1.5;
    ctx.fillStyle = rgba(C, 1); ctx.beginPath(); ctx.arc(cx + bw / 2 + depth + 10, py, pr, 0, 7); ctx.fill();
    // candle timer ring
    ctx.strokeStyle = rgba(C, 0.5); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(cx, Y(lo) + 30, 10, -Math.PI / 2, -Math.PI / 2 + (age / PERIOD) * Math.PI * 2); ctx.stroke();

    // dim the left side so cards stay easy to read
    const shade = ctx.createLinearGradient(0, 0, W, 0);
    shade.addColorStop(0, "rgba(5,5,5,.55)"); shade.addColorStop(W < 760 ? 1 : 0.45, "rgba(5,5,5,.2)"); shade.addColorStop(1, "rgba(5,5,5,0)");
    ctx.fillStyle = shade; ctx.fillRect(0, 0, W, H);
  }

  function loop(t) {
    const dt = Math.min(0.05, (t - t0) / 1000 || 0.016); t0 = t;
    if (document.visibilityState !== "hidden") { tick(dt); draw(dt); }
    requestAnimationFrame(loop);
  }
  if (reduce) { for (let i = 0; i < 180; i++) tick(0.016); draw(0); }
  else requestAnimationFrame(loop);
})();
