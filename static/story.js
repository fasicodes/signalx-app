/* Signals FM landing page: the scroll story (templates/landing.html, section#story).
   A sticky canvas stage changes scene as the reader scrolls through five chapters:
     0 noise    candles scattered and flickering
     1 agents   five AI agents orbit a core and pull data in
     2 pattern  the candles line up into a chart; a trend line draws itself
     3 signal   entry, stop-loss and target appear on the chart
     4 truth    ten dots show how often signals won in testing (data-win), the rest lost
   Illustration only: the chart shape is a fixed pattern, not market data. Also drives the page's top progress bar.
   Plain canvas, no library; one still frame per chapter for prefers-reduced-motion. */
(function () {
  "use strict";
  const sec = document.getElementById("story"), cv = document.getElementById("story-canvas");
  if (!sec || !cv) return;
  const ctx = cv.getContext("2d");
  const reduce = (() => { try { return matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; } })();
  const chaps = Array.from(sec.querySelectorAll(".chap"));
  const NAMES = ["The noise", "The agents wake up", "The pattern", "The signal", "The truth"];
  const WIN = parseFloat(sec.dataset.win) || 60;

  // top-of-page reading progress
  const bar = document.createElement("div");
  bar.className = "fx-progress"; bar.setAttribute("aria-hidden", "true");
  document.body.appendChild(bar);

  const G = [0, 255, 136], R = [255, 70, 90];
  const AG = [["Scout", 160], ["Atlas", 200], ["Pulse", 275], ["Guard", 35], ["Core", 330]];
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  const hsl = (h, a) => `hsla(${h},90%,62%,${a})`;
  const lerp = (a, b, t) => a + (b - a) * t;
  const clamp = (v, a = 0, b = 1) => Math.max(a, Math.min(b, v));
  const ease = (t) => t * t * (3 - 2 * t);

  // a fixed, deterministic chart shape (seeded) so the story looks the same for everyone
  let seed = 7;
  const rand = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const N = 42, candles = [];
  let p = 50;
  for (let i = 0; i < N; i++) {
    const drift = i < 14 ? -0.5 : i < 24 ? 0.2 : 0.95;      // dip, base, breakout
    const o = p; p = p + drift + (rand() - 0.5) * 2.4;
    candles.push({ o, c: p, h: Math.max(o, p) + rand() * 1.4, l: Math.min(o, p) - rand() * 1.4,
      rx: rand(), ry: rand(), rs: 0.6 + rand() * 1.2, ph: rand() * 6.28 });
  }
  const lo = Math.min(...candles.map((c) => c.l)) - 2, hi = Math.max(...candles.map((c) => c.h)) + 4;
  const entryI = 27, entry = candles[entryI].c, stop = entry - 3.2, target = entry + 6.4;

  let W = 0, H = 0;
  function size() {
    const r = cv.getBoundingClientRect(), d = Math.min(2, devicePixelRatio || 1);
    W = r.width; H = r.height;
    cv.width = Math.round(W * d); cv.height = Math.round(H * d);
    ctx.setTransform(d, 0, 0, d, 0, 0);
  }
  size(); addEventListener("resize", () => { size(); if (reduce) draw(0); });

  // scroll -> story position (0..5) from where each chapter sits in the viewport
  let pos = 0, shown = 0;
  function readScroll() {
    const st = sec.querySelector(".story-stage").getBoundingClientRect();
    const mid = innerWidth <= 980 ? (st.bottom + innerHeight) / 2 : innerHeight * 0.55;
    let s = 0;
    chaps.forEach((c, i) => {
      const r = c.getBoundingClientRect();
      if (r.top < mid) s = i + clamp((mid - r.top) / Math.max(1, r.height));
    });
    pos = clamp(s, 0, chaps.length - 0.001);
    const k = Math.floor(pos);
    chaps.forEach((c, i) => c.classList.toggle("on", i === k));
    document.getElementById("story-ch").textContent = `CH 0${k + 1} / 05`;
    document.getElementById("story-name").textContent = NAMES[k];
    document.getElementById("story-prog").style.width = (pos / chaps.length * 100).toFixed(1) + "%";
    const h = document.documentElement;
    bar.style.transform = `scaleX(${clamp(h.scrollTop / Math.max(1, h.scrollHeight - innerHeight))})`;
    if (reduce) draw(0);
  }
  addEventListener("scroll", readScroll, { passive: true });
  readScroll();

  function draw(t) {
    ctx.clearRect(0, 0, W, H);
    const s = reduce ? Math.floor(pos) : shown;            // smoothed story position
    const order = ease(clamp(s - 1.3, 0, 1));             // 0 scattered .. 1 lined up as a chart
    const agentsIn = clamp(s - 0.5, 0, 1) * (1 - 0.65 * clamp(s - 1.6, 0, 1));
    const levels = ease(clamp(s - 2.6, 0, 1));
    const truth = ease(clamp(s - 3.6, 0, 1));
    const padX = W * 0.08, padT = H * 0.14, padB = H * 0.12;
    const cw = (W - padX * 2) / N, Y = (v) => padT + (hi - v) / (hi - lo) * (H - padT - padB);

    // grid
    ctx.strokeStyle = "rgba(0,255,136,.05)"; ctx.lineWidth = 1;
    for (let x = (t * 12) % 40; x < W; x += 40) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
    for (let y = 0; y < H; y += 40) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }

    // candles: scattered noise -> ordered chart
    const dim = 1 - truth * 0.75;
    candles.forEach((c, i) => {
      const jx = Math.sin(t * 2 * c.rs + c.ph) * 10 * (1 - order), jy = Math.cos(t * 1.7 * c.rs + c.ph) * 12 * (1 - order);
      const sx = c.rx * W, sy = c.ry * H, sc = 0.4 + c.rs * 0.5;
      const ox = padX + i * cw + cw / 2;
      const x = lerp(sx + jx, ox, order);
      const ymid = lerp(sy + jy, Y((c.o + c.c) / 2), order);
      const scale = lerp(sc * 0.55, 1, order);
      const bodyH = Math.abs(Y(c.o) - Y(c.c)) * scale + 2, wickH = (Y(c.l) - Y(c.h)) * scale;
      const up = c.c >= c.o, col = up ? G : R;
      const flick = order < 1 ? 0.5 + 0.5 * Math.sin(t * 6 * c.rs + c.ph) : 1;
      const a = lerp(0.25 + 0.35 * flick, 0.95, order) * dim;
      ctx.strokeStyle = rgba(col, a); ctx.fillStyle = rgba(col, a);
      ctx.beginPath(); ctx.moveTo(x, ymid - wickH / 2); ctx.lineTo(x, ymid + wickH / 2); ctx.stroke();
      const bw = Math.max(3, cw * 0.6 * scale);
      if (order > 0.95 && i === N - 1) { ctx.shadowColor = rgba(col, 1); ctx.shadowBlur = 16; }
      ctx.fillRect(x - bw / 2, ymid - bodyH / 2, bw, bodyH);
      ctx.shadowBlur = 0;
    });

    // trend line drawing itself
    if (order > 0.6) {
      const k = clamp((order - 0.6) / 0.4) * (reduce ? 1 : clamp((s - 2) * 1.6));
      ctx.strokeStyle = rgba([94, 234, 212], 0.85 * dim); ctx.lineWidth = 2; ctx.shadowColor = "#5eead4"; ctx.shadowBlur = 10;
      ctx.beginPath();
      const last = Math.max(1, Math.floor(N * k));
      let e = candles[0].c;
      for (let i = 0; i < last; i++) { e = e * 0.8 + candles[i].c * 0.2; const x = padX + i * cw + cw / 2; i ? ctx.lineTo(x, Y(e)) : ctx.moveTo(x, Y(e)); }
      ctx.stroke(); ctx.shadowBlur = 0; ctx.lineWidth = 1;
      // scan beam
      if (!reduce && s > 1.8 && s < 3.2) {
        const bx = padX + ((t * 0.35) % 1) * (W - padX * 2);
        const g = ctx.createLinearGradient(bx - 70, 0, bx, 0);
        g.addColorStop(0, "rgba(0,255,136,0)"); g.addColorStop(1, "rgba(0,255,136,.2)");
        ctx.fillStyle = g; ctx.fillRect(bx - 70, 0, 70, H);
        ctx.strokeStyle = "rgba(140,255,210,.8)"; ctx.beginPath(); ctx.moveTo(bx, 0); ctx.lineTo(bx, H); ctx.stroke();
      }
    }

    // signal levels
    if (levels > 0) {
      const x0 = padX + entryI * cw + cw / 2, x1 = lerp(x0, W - padX, levels);
      const lvl = (v, col, label) => {
        ctx.setLineDash([6, 6]); ctx.strokeStyle = rgba(col, 0.9 * levels * dim); ctx.beginPath(); ctx.moveTo(x0, Y(v)); ctx.lineTo(x1, Y(v)); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = rgba(col, levels * dim); ctx.font = "600 11px 'IBM Plex Mono', monospace"; ctx.fillText(label, x0 + 6, Y(v) - 6);
      };
      ctx.fillStyle = rgba(G, 0.08 * levels * dim); ctx.fillRect(x0, Y(target), x1 - x0, Y(entry) - Y(target));
      ctx.fillStyle = rgba(R, 0.08 * levels * dim); ctx.fillRect(x0, Y(entry), x1 - x0, Y(stop) - Y(entry));
      lvl(target, G, "TARGET"); lvl(entry, [230, 255, 245], "ENTRY"); lvl(stop, R, "STOP-LOSS");
      // BUY marker
      const pulse = reduce ? 1 : 1 + Math.sin(t * 5) * 0.15;
      ctx.fillStyle = rgba(G, levels * dim); ctx.shadowColor = "#00ff88"; ctx.shadowBlur = 18;
      ctx.beginPath(); ctx.arc(x0, Y(entry), 6 * pulse, 0, 7); ctx.fill(); ctx.shadowBlur = 0;
      ctx.font = "800 13px Manrope, Inter, sans-serif";
      const tw = ctx.measureText("BUY").width + 20, bx = x0 - tw / 2, by = Y(entry) + 16;
      ctx.fillStyle = rgba(G, 0.9 * levels * dim); ctx.beginPath(); ctx.roundRect ? ctx.roundRect(bx, by, tw, 24, 12) : ctx.rect(bx, by, tw, 24); ctx.fill();
      ctx.fillStyle = `rgba(4,19,11,${levels})`; ctx.fillText("BUY", bx + 10, by + 16.5);
    }

    // agents orbiting a core, pulling data from the candles
    if (agentsIn > 0) {
      const cx = W / 2, cy = lerp(H * 0.5, H * 0.16, clamp(s - 1.6, 0, 1)), rad = lerp(Math.min(W, H) * 0.3, Math.min(W, H) * 0.09, clamp(s - 1.6, 0, 1));
      ctx.fillStyle = `rgba(167,139,250,${0.25 * agentsIn})`; ctx.shadowColor = "#a78bfa"; ctx.shadowBlur = 30;
      ctx.beginPath(); ctx.arc(cx, cy, 10 + rad * 0.08, 0, 7); ctx.fill(); ctx.shadowBlur = 0;
      ctx.strokeStyle = `rgba(167,139,250,${0.25 * agentsIn})`; ctx.beginPath(); ctx.arc(cx, cy, rad, 0, 7); ctx.stroke();
      AG.forEach(([name, h], i) => {
        const ang = (reduce ? 0 : t * 0.4) + i / AG.length * Math.PI * 2;
        const x = cx + Math.cos(ang) * rad, y = cy + Math.sin(ang) * rad * 0.85;
        // data beam from a candle to the agent
        const c = candles[(i * 9 + Math.floor(t * 2)) % N];
        ctx.strokeStyle = hsl(h, 0.35 * agentsIn); ctx.setLineDash([3, 5]); ctx.lineDashOffset = -t * 30;
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(lerp(c.rx * W, padX + candles.indexOf(c) * cw, order), lerp(c.ry * H, Y(c.c), order)); ctx.stroke();
        ctx.setLineDash([]);
        ctx.strokeStyle = hsl(h, 0.3 * agentsIn); ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(cx, cy); ctx.stroke();
        robot(x, y, lerp(15, 10, clamp(s - 1.6, 0, 1)), h, agentsIn, t + i);
        if (s < 2.2) { ctx.fillStyle = hsl(h, agentsIn); ctx.font = "600 11px 'IBM Plex Mono', monospace"; ctx.textAlign = "center"; ctx.fillText(name.toUpperCase(), x, y + 30); ctx.textAlign = "left"; }
      });
    }

    // truth: ten dots, wins and losses
    if (truth > 0) {
      const wins = Math.round(WIN / 10), r = Math.min(W / 34, H * 0.035), gap = r * 3, x0 = W / 2 - gap * 4.5, y0 = H * 0.5;
      for (let i = 0; i < 10; i++) {
        const k = clamp(truth * 10 - i * 0.6), win = i < wins, col = win ? G : R;
        ctx.fillStyle = rgba(col, 0.9 * k); ctx.shadowColor = rgba(col, 1); ctx.shadowBlur = 14 * k;
        ctx.beginPath(); ctx.arc(x0 + i * gap, y0, r * (0.6 + 0.4 * k), 0, 7); ctx.fill(); ctx.shadowBlur = 0;
      }
      ctx.textAlign = "center"; ctx.fillStyle = `rgba(233,255,245,${truth})`; ctx.font = `800 ${Math.round(r * 1.5)}px Manrope, Inter, sans-serif`;
      ctx.fillText(`${wins} in 10 reached the target`, W / 2, y0 - r * 2.6);
      ctx.fillStyle = `rgba(255,120,135,${truth})`; ctx.font = `600 ${Math.round(r * 0.9)}px Inter, sans-serif`;
      ctx.fillText(`${10 - wins} in 10 hit the stop · fees included`, W / 2, y0 + r * 3.2);
      ctx.textAlign = "left";
    }
  }
  function robot(x, y, r, h, a, t) {
    ctx.save(); ctx.globalAlpha = a;
    ctx.shadowColor = hsl(h, 1); ctx.shadowBlur = 16;
    ctx.fillStyle = hsl(h, 1); ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x - r, y - r * 0.8, r * 2, r * 1.6, r * 0.5) : ctx.rect(x - r, y - r * 0.8, r * 2, r * 1.6); ctx.fill();
    ctx.shadowBlur = 0;
    ctx.fillStyle = "#06110d"; ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x - r * 0.72, y - r * 0.5, r * 1.44, r * 0.9, r * 0.3) : ctx.rect(x - r * 0.72, y - r * 0.5, r * 1.44, r * 0.9); ctx.fill();
    const blink = Math.sin(t * 1.3) > 0.97 ? 0.15 : 1;
    ctx.fillStyle = `hsla(${h},100%,78%,1)`;
    ctx.fillRect(x - r * 0.45, y - r * 0.25, r * 0.25, r * 0.38 * blink); ctx.fillRect(x + r * 0.2, y - r * 0.25, r * 0.25, r * 0.38 * blink);
    ctx.strokeStyle = hsl(h, 1); ctx.beginPath(); ctx.moveTo(x, y - r * 0.8); ctx.lineTo(x, y - r * 1.25); ctx.stroke();
    ctx.beginPath(); ctx.arc(x, y - r * 1.3, r * 0.15, 0, 7); ctx.fill();
    ctx.restore();
  }

  let last = 0, on = true;
  if ("IntersectionObserver" in window) new IntersectionObserver((e) => { on = e[0].isIntersecting; }).observe(sec);
  function loop(ms) {
    const t = ms / 1000, dt = Math.min(0.05, t - last); last = t;
    shown += (pos - shown) * Math.min(1, dt * 4);
    if (on && document.visibilityState !== "hidden") draw(t);
    requestAnimationFrame(loop);
  }
  if (reduce) draw(0); else requestAnimationFrame(loop);
})();
