/* Signals FM admin panel (templates/admin/*): every page reads /api/admin/... (admin_panel.py). No library: the
   charts are small SVG drawings with hover tooltips. Changes send the page's CSRF token. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const B = document.body;
  const PAGE = B.dataset.page || "";
  const CUR = B.dataset.currency || "USD";
  const CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const COLORS = { green: "#00ff88", blue: "#3ea8ff", gold: "#ffc53d", purple: "#b36bff", red: "#ff5a6a", teal: "#2ee6d0", orange: "#ff9f43" };

  // ------------------------------------------------------------------ api
  let ended = false;
  async function api(path, opts) {
    opts = opts || {};
    const o = { credentials: "same-origin", headers: { Accept: "application/json" }, method: opts.method || (opts.body !== undefined ? "POST" : "GET") };
    if (opts.body !== undefined) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(opts.body); }
    if (o.method !== "GET") o.headers["X-CSRF-Token"] = CSRF;
    let r;
    try { r = await fetch(path, o); } catch (e) { throw new Error("Could not reach the server. Check the connection and try again."); }
    let d = {};
    try { d = await r.json(); } catch (e) { /* not JSON */ }
    if (r.status === 404 && d.error === "Not found.") { sessionEnded(); throw new Error("Your admin session has ended. Log in again."); }
    if (!r.ok || d.ok === false) { const err = new Error(d.error || `Request failed (${r.status}).`); err.data = d; err.status = r.status; throw err; }
    return d;
  }
  function sessionEnded() {
    if (ended) return;
    ended = true;
    const c = $("adm-content");
    if (c) c.insertAdjacentHTML("afterbegin", '<div class="err fade-in">Your admin session has ended or this account is no longer an admin. ' +
      '<a href="/login">Log in again</a>.</div>');
  }

  // ------------------------------------------------------------------ formatting
  function parseT(s) {
    if (!s) return null;
    let t = String(s);
    if (/^\d{4}-\d{2}-\d{2}$/.test(t)) t += "T00:00:00Z";
    else if (!/(Z|[+-]\d\d:?\d\d)$/.test(t)) t = t.replace(" ", "T") + "Z";
    const d = new Date(t);
    return isNaN(d) ? null : d;
  }
  const fmtDT = (s) => { const d = parseT(s); return d ? d.toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"; };
  const fmtD = (s) => { const d = parseT(s); return d ? d.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" }) : "—"; };
  const fmtDay = (s) => { const d = parseT(s); return d ? d.toLocaleDateString([], { month: "short", day: "numeric", timeZone: "UTC" }) : ""; };
  function ago(s) {
    const d = parseT(s);
    if (!d) return "—";
    const sec = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
    if (sec < 45) return "just now";
    if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
    if (sec < 86400) return `${Math.round(sec / 3600)} h ago`;
    if (sec < 86400 * 30) return `${Math.round(sec / 86400)} days ago`;
    return fmtD(s);
  }
  const fmtN = (v) => Number(v || 0).toLocaleString();
  function compact(v) {
    v = Number(v || 0);
    if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + "M";
    if (Math.abs(v) >= 1e4) return (v / 1e3).toFixed(v >= 1e5 ? 0 : 1) + "K";
    return fmtN(Math.round(v * 100) / 100);
  }
  function money(v, cur) {
    try { return new Intl.NumberFormat([], { style: "currency", currency: cur || CUR, maximumFractionDigits: 2 }).format(Number(v || 0)); }
    catch (e) { return `${Number(v || 0).toFixed(2)} ${cur || CUR}`; }
  }
  const price = (v, cur) => (v == null ? "—" : money(v, cur));
  const nf = (v, d) => (v == null || isNaN(v) ? "—" : Number(v).toLocaleString([], { maximumFractionDigits: d == null ? 8 : d }));
  const initials = (name, email) => {
    const src = (name || "").trim() || (email || "?").split("@")[0];
    const p = src.split(/[\s._-]+/).filter(Boolean);
    return ((p[0] || "?")[0] + ((p[1] || "")[0] || "")).toUpperCase();
  };
  const pretty = (s) => String(s || "").replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());

  const BADGE = { active: "b-green", online: "b-green", verified: "b-green", yes: "b-green", open: "b-green", target_reached: "b-green",
    expired: "b-red", blocked: "b-red", rejected: "b-red", stop_loss_reached: "b-red", no: "b-gold",
    pending: "b-gold", idle: "b-gold", cancelled: "b-grey", refunded: "b-purple", away: "b-grey", "logged out": "b-grey", offline: "b-grey",
    never: "b-grey", none: "b-grey", closed: "b-grey", admin: "b-blue", google: "b-blue", password: "b-grey" };
  const LABEL = { blocked: "suspended", idle: "idle (background tab)", target_reached: "target reached", stop_loss_reached: "stop hit",
    manually_closed: "closed by member", none: "no subscription" };
  function badge(status, label) {
    const k = String(status || "none").toLowerCase();
    let t = String(label || LABEL[k] || pretty(k));
    if (!t.includes("@")) t = t.charAt(0).toUpperCase() + t.slice(1);      // "Suspended", "Never used" (emails stay as they are)
    return `<span class="badge ${BADGE[k] || "b-grey"}">${esc(t)}</span>`;
  }

  // ------------------------------------------------------------------ icons + events
  const IC = {
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0116 0"/>',
    userplus: '<circle cx="10" cy="8" r="4"/><path d="M2 21a8 8 0 0116 0M19 8v6M16 11h6"/>',
    login: '<path d="M10 4H5a1 1 0 00-1 1v14a1 1 0 001 1h5M15 17l5-5-5-5M20 12H9"/>',
    crown: '<path d="M3 8l4.5 3.5L12 5l4.5 6.5L21 8l-2 10H5z"/>',
    x: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    cash: '<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="2.5"/>',
    key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3"/>',
    ban: '<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/>',
    check: '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>',
    edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
    plus: '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    download: '<path d="M12 3v12M7 10l5 5 5-5M4 21h16"/>',
    dot: '<circle cx="12" cy="12" r="4"/>',
    empty: '<path d="M4 7h16v12H4z"/><path d="M4 7l3-4h10l3 4M9 12h6"/>',
  };
  const svg = (k) => `<svg viewBox="0 0 24 24" aria-hidden="true">${IC[k] || IC.dot}</svg>`;
  const EV = {
    register: ["New registration", "#00ff88", "userplus"], login: ["Logged in", "#3ea8ff", "login"], logout: ["Logged out", "#8a948d", "login"],
    password_reset: ["Password reset", "#ffc53d", "key"],
    subscription_new: ["New subscription", "#ffc53d", "crown"], subscription_activated: ["Subscription activated", "#00ff88", "crown"],
    subscription_cancelled: ["Subscription cancelled", "#ff5a6a", "x"], subscription_expired: ["Subscription expired", "#ff5a6a", "x"],
    subscription_extended: ["Subscription extended", "#00ff88", "plus"], subscription_pending: ["Subscription set to pending", "#ffc53d", "clock"],
    payment_recorded: ["Payment recorded", "#ffc53d", "cash"], payment_pending: ["Payment set to pending", "#ffc53d", "cash"],
    payment_verified: ["Payment verified", "#00ff88", "cash"], payment_refunded: ["Payment refunded", "#b36bff", "cash"],
    payment_rejected: ["Payment rejected", "#ff5a6a", "cash"],
    user_blocked: ["Account suspended", "#ff5a6a", "ban"], user_unblocked: ["Account active again", "#00ff88", "check"],
    user_logged_out: ["Signed out everywhere", "#ffc53d", "login"], user_renamed: ["Name changed", "#3ea8ff", "edit"],
    plan_created: ["Plan created", "#2ee6d0", "plus"], plan_updated: ["Plan updated", "#2ee6d0", "edit"],
    export_users: ["User data exported", "#b36bff", "download"], export_subscriptions: ["Subscription data exported", "#b36bff", "download"],
    export_activity: ["Activity log exported", "#b36bff", "download"],
  };
  function evCell(kind) {
    const e = EV[kind] || [pretty(kind), "#8a948d", "dot"];
    return `<span class="ev"><span class="ev-ic" style="--c:${e[1]}">${svg(e[2])}</span>${esc(e[0])}</span>`;
  }

  // ------------------------------------------------------------------ UI helpers
  function toast(msg, bad) {
    const box = $("adm-toast");
    if (!box) return;
    const d = document.createElement("div");
    d.className = bad ? "bad" : "";
    d.textContent = msg;
    box.appendChild(d);
    setTimeout(() => d.remove(), 4200);
  }
  function emptyHtml(title, text) {
    return `<div class="empty fade-in">${svg("empty")}<b>${esc(title)}</b>${text ? `<span>${text}</span>` : ""}</div>`;
  }
  const errHtml = (e) => `<div class="err fade-in">${esc(e.message || e)}</div>`;
  function table(cols, rows, empty) {
    if (!rows || !rows.length) return empty || emptyHtml("Nothing here yet");
    return `<div class="tbl-wrap fade-in"><table class="tbl"><thead><tr>${cols.map((c) => `<th class="${c.cls || ""}" scope="col">${esc(c.h)}</th>`).join("")}</tr></thead>` +
      `<tbody>${rows.map((r, i) => `<tr>${cols.map((c) => `<td class="${c.cls || ""}">${c.f(r, i)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  }
  function pager(el, d, go) {
    if (!el) return;
    el.hidden = !(d.pages > 1);
    if (el.hidden) return;
    el.innerHTML = `<button class="btn sm" type="button" data-p="${d.page - 1}"${d.page <= 1 ? " disabled" : ""}>← Newer</button>` +
      `<span>Page ${fmtN(d.page)} of ${fmtN(d.pages)} · ${fmtN(d.total)} in all</span>` +
      `<button class="btn sm" type="button" data-p="${d.page + 1}"${d.page >= d.pages ? " disabled" : ""}>Older →</button>`;
    el.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => go(Number(b.dataset.p))));
  }
  function segment(id, onChange) {
    const el = $(id);
    if (!el) return () => "";
    el.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b || !el.contains(b)) return;
      el.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      onChange(b.dataset.v != null ? b.dataset.v : b.dataset.r);
    });
    return () => { const b = el.querySelector('button[aria-pressed="true"]'); return b ? (b.dataset.v != null ? b.dataset.v : b.dataset.r) : ""; };
  }
  function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

  function modal(title, bodyHtml, buttons, opts) {
    opts = opts || {};
    const bg = document.createElement("div");
    bg.className = "modal-bg";
    bg.innerHTML = `<div class="modal${opts.wide ? " wide" : ""}" role="dialog" aria-modal="true" aria-labelledby="mdl-t">` +
      `<div class="modal-h"><h3 id="mdl-t">${esc(title)}</h3><button type="button" class="modal-x" aria-label="Close">×</button></div>` +
      `<div class="modal-b">${bodyHtml}<p class="msg" role="status" aria-live="polite"></p></div>` +
      `<div class="modal-f">${(buttons || []).map((b, i) => `<button type="button" class="btn ${b.cls || ""}" data-i="${i}">${esc(b.label)}</button>`).join("")}</div></div>`;
    const prev = document.activeElement;
    const close = () => { bg.remove(); document.removeEventListener("keydown", onKey); if (prev && prev.focus) prev.focus(); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    bg.addEventListener("mousedown", (e) => { if (e.target === bg) close(); });
    bg.querySelector(".modal-x").addEventListener("click", close);
    const msgEl = bg.querySelector(".msg");
    const ctl = {
      el: bg, close,
      msg(t, bad) { msgEl.textContent = t || ""; msgEl.className = "msg " + (bad ? "err" : ""); },
      q: (sel) => bg.querySelector(sel),
    };
    bg.querySelectorAll(".modal-f button").forEach((btn) => btn.addEventListener("click", async () => {
      const b = buttons[Number(btn.dataset.i)];
      if (!b.run) { close(); return; }
      bg.querySelectorAll(".modal-f button").forEach((x) => (x.disabled = true));
      ctl.msg("");
      try { await b.run(ctl); } catch (e) { ctl.msg(e.message, true); }
      bg.querySelectorAll(".modal-f button").forEach((x) => (x.disabled = false));
    }));
    document.body.appendChild(bg);
    const f = bg.querySelector("input, select, textarea, .modal-f button");
    if (f) f.focus();
    return ctl;
  }
  function confirmBox(title, text, label, cls) {
    return new Promise((resolve) => {
      let done = false;
      const m = modal(title, `<p style="margin:0">${text}</p>`, [
        { label: "Cancel", run: (c) => { done = true; c.close(); resolve(false); } },
        { label, cls: cls || "primary", run: (c) => { done = true; c.close(); resolve(true); } },
      ]);
      const obs = new MutationObserver(() => { if (!document.body.contains(m.el)) { obs.disconnect(); if (!done) resolve(false); } });
      obs.observe(document.body, { childList: true });
    });
  }

  // ------------------------------------------------------------------ charts (SVG)
  const NS = "http://www.w3.org/2000/svg";
  function niceMax(v, ints) {
    if (!(v > 0)) return ints ? 4 : 1;
    if (ints && v <= 8) return v <= 4 ? 4 : 8;           // counts: whole-number ticks (4 steps)
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v && (!ints || Number.isInteger((m * p) / 4))) return m * p;
    return 10 * p;
  }
  const yLbl = (v, fmt) => (fmt ? fmt(v) : compact(v));
  function drawChart(el, cfg) {
    // cfg: {labels, series:[{name, values, color}], type:"line"|"bar", area, fmt, xfmt, legend}
    if (!el) return;
    const state = { hidden: new Set() };
    const render = () => {
      const W = Math.max(260, el.clientWidth || 600), H = Math.max(140, el.clientHeight || 260);
      const series = cfg.series.filter((s) => !state.hidden.has(s.name));
      const n = cfg.labels.length;
      const isInt = (list) => list.every((s) => s.values.every((v) => Number.isInteger(Number(v))));
      let left = series.filter((s) => s.axis !== "right"), right = series.filter((s) => s.axis === "right");
      if (!left.length) { left = right; right = []; }           // only right-scale series shown: one scale
      const onRight = new Set(right);
      const max = niceMax(Math.max(0, ...left.flatMap((s) => s.values.map(Number))), isInt(left));
      const maxR = right.length ? niceMax(Math.max(0, ...right.flatMap((s) => s.values.map(Number))), isInt(right)) : 0;
      const pad = { l: 46, r: right.length ? 40 : 12, t: 12, b: 26 };
      const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
      const x = (i) => pad.l + (n <= 1 ? iw / 2 : (cfg.type === "bar" ? (i + 0.5) * iw / n : i * iw / (n - 1)));
      const yFor = (s) => (v) => pad.t + ih - (Number(v) / (onRight.has(s) ? maxR : max)) * ih;
      const y = (v) => pad.t + ih - (Number(v) / max) * ih;
      let out = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(cfg.title || "Chart")}"><defs>`;
      series.forEach((s, k) => { out += `<linearGradient id="g${cfg.uid}${k}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${s.color}" stop-opacity=".32"/><stop offset="1" stop-color="${s.color}" stop-opacity="0"/></linearGradient>`; });
      out += "</defs>";
      for (let t = 0; t <= 4; t++) {
        const v = (max * t) / 4, yy = y(v);
        out += `<line class="grid-l" x1="${pad.l}" x2="${W - pad.r}" y1="${yy}" y2="${yy}"/><text class="axis" x="${pad.l - 8}" y="${yy + 4}" text-anchor="end">${esc(yLbl(v, cfg.fmt))}</text>`;
        if (right.length) out += `<text class="axis" x="${W - pad.r + 8}" y="${yy + 4}" text-anchor="start" style="fill:${right[0].color}">${esc(yLbl((maxR * t) / 4))}</text>`;
      }
      const step = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(iw / 72))));
      for (let i = 0; i < n; i += step) out += `<text class="axis" x="${x(i)}" y="${H - 6}" text-anchor="middle">${esc((cfg.xfmt || fmtDay)(cfg.labels[i]))}</text>`;
      if (cfg.type === "bar") {
        const bw = Math.max(2, Math.min(34, (iw / n) * 0.62 / Math.max(1, series.length)));
        series.forEach((s, k) => s.values.forEach((v, i) => {
          const h = Math.max(0, pad.t + ih - y(v)), xx = x(i) - (bw * series.length) / 2 + k * bw;
          if (h > 0) out += `<rect x="${xx}" y="${y(v)}" width="${bw - 1}" height="${h}" rx="${Math.min(5, bw / 3)}" fill="${s.color}" opacity=".9"/>`;
        }));
      } else {
        series.forEach((s, k) => {
          const ys = yFor(s);
          const pts = s.values.map((v, i) => [x(i), ys(v)]);
          const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
          if (cfg.area !== false && pts.length) out += `<path d="${line} L${pts[pts.length - 1][0]} ${pad.t + ih} L${pts[0][0]} ${pad.t + ih} Z" fill="url(#g${cfg.uid}${k})"/>`;
          out += `<path d="${line}" fill="none" stroke="${s.color}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" style="filter:drop-shadow(0 0 6px ${s.color}66)"/>`;
          if (n <= 31) pts.forEach((p) => { out += `<circle cx="${p[0]}" cy="${p[1]}" r="3.2" fill="#050505" stroke="${s.color}" stroke-width="2"/>`; });
        });
      }
      out += `<line class="hover-l" x1="0" x2="0" y1="${pad.t}" y2="${pad.t + ih}" stroke="rgba(255,255,255,.25)" stroke-dasharray="3 3" visibility="hidden"/>`;
      out += `<rect class="hit" x="${pad.l}" y="${pad.t}" width="${iw}" height="${ih}" fill="transparent"/></svg>`;
      const total = series.reduce((a, s) => a + s.values.reduce((b, v) => b + Number(v || 0), 0), 0);
      el.innerHTML = (cfg.legend !== false && cfg.series.length > 1 ? `<div class="legend">${cfg.series.map((s) =>
        `<button type="button" aria-pressed="${!state.hidden.has(s.name)}" data-s="${esc(s.name)}"><i style="background:${s.color}"></i>${esc(s.name)}${s.axis === "right" ? ' <small class="faint">(right scale)</small>' : ""}</button>`).join("")}</div>` : "") +
        `<div style="position:relative;height:calc(100% - ${cfg.legend !== false && cfg.series.length > 1 ? 30 : 0}px)">${out}<div class="tip" hidden></div>` +
        (total === 0 ? `<div style="position:absolute;inset:0;display:grid;place-items:center;color:var(--faint);font-size:13px;pointer-events:none">No data for this period yet</div>` : "") + "</div>";
      if (cfg.legend !== false && cfg.series.length > 1) {
        el.querySelectorAll(".legend button").forEach((b) => b.addEventListener("click", () => {
          const s = b.dataset.s;
          if (state.hidden.has(s)) state.hidden.delete(s); else if (state.hidden.size < cfg.series.length - 1) state.hidden.add(s);
          render();
        }));
      }
      const svgEl = el.querySelector("svg"), tip = el.querySelector(".tip"), hl = el.querySelector(".hover-l"), hit = el.querySelector(".hit");
      const box = svgEl.parentElement;
      const move = (ev) => {
        const r = svgEl.getBoundingClientRect();
        const px = ((ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left) * (W / r.width);
        let i = cfg.type === "bar" ? Math.floor((px - pad.l) / (iw / n)) : Math.round(((px - pad.l) / iw) * (n - 1));
        i = Math.max(0, Math.min(n - 1, i));
        hl.setAttribute("x1", x(i)); hl.setAttribute("x2", x(i)); hl.setAttribute("visibility", "visible");
        tip.hidden = false;
        tip.innerHTML = `<b>${esc((cfg.tipfmt || cfg.xfmt || fmtDay)(cfg.labels[i]))}</b>` + series.map((s) =>
          `<div><i style="background:${s.color}"></i>${esc(s.name)}: <span class="num">${esc(cfg.fmt ? cfg.fmt(s.values[i]) : fmtN(s.values[i]))}</span></div>`).join("");
        const left = (x(i) / W) * r.width;
        tip.style.left = Math.max(70, Math.min(r.width - 70, left)) + "px";
        tip.style.top = Math.max(40, (Math.min(...series.map((s) => yFor(s)(s.values[i]))) / H) * r.height) + "px";
      };
      const leave = () => { tip.hidden = true; hl.setAttribute("visibility", "hidden"); };
      hit.addEventListener("mousemove", move); hit.addEventListener("touchstart", move, { passive: true });
      hit.addEventListener("touchmove", move, { passive: true }); box.addEventListener("mouseleave", leave);
    };
    cfg.uid = cfg.uid || Math.random().toString(36).slice(2, 7);
    render();
    if (!el._ro && window.ResizeObserver) {
      let w = el.clientWidth;
      el._ro = new ResizeObserver(() => { if (Math.abs(el.clientWidth - w) > 8) { w = el.clientWidth; el._render(); } });
      el._ro.observe(el);
    }
    el._render = render;
  }
  function sparkline(svgEl, values, color) {
    if (!svgEl) return;
    const vals = (values || []).map(Number);
    if (!vals.length) { svgEl.innerHTML = ""; return; }
    const max = Math.max(...vals), min = Math.min(...vals);
    const span = max - min || 1;
    const pts = vals.map((v, i) => [(i / Math.max(1, vals.length - 1)) * 116 + 2, 36 - ((v - min) / span) * 30]);
    const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    const id = "s" + Math.random().toString(36).slice(2, 7);
    svgEl.innerHTML = `<defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".35"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>` +
      `<path d="${d} L118 40 L2 40 Z" fill="url(#${id})"/><path d="${d}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="filter:drop-shadow(0 0 4px ${color})"/>`;
  }
  function donut(el, parts, center) {
    const total = parts.reduce((a, p) => a + p.value, 0);
    const R = 42, C = 2 * Math.PI * R;
    let off = 0, arcs = "";
    parts.forEach((p) => {
      if (!p.value) return;
      const len = (p.value / total) * C;
      arcs += `<circle cx="50" cy="50" r="${R}" fill="none" stroke="${p.color}" stroke-width="11" stroke-dasharray="${len} ${C - len}" stroke-dashoffset="${-off}" stroke-linecap="butt"/>`;
      off += len;
    });
    el.innerHTML = `<div class="donut-wrap fade-in"><div class="donut"><svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="${R}" fill="none" stroke="#1a1f1b" stroke-width="11"/>${arcs}</svg>` +
      `<div class="mid"><b>${esc(center.value)}</b><span>${esc(center.label)}</span></div></div>` +
      `<div class="donut-legend">${parts.map((p) => `<div><i style="background:${p.color}"></i><span>${esc(p.label)}</span><b class="num">${fmtN(p.value)}</b></div>`).join("")}</div></div>`;
  }

  // ------------------------------------------------------------------ shell
  function shell() {
    const burger = $("adm-burger");
    let bd = null;
    const close = () => { B.classList.remove("side-open"); if (burger) burger.setAttribute("aria-expanded", "false"); if (bd) { bd.remove(); bd = null; } };
    if (burger) burger.addEventListener("click", () => {
      if (B.classList.contains("side-open")) { close(); return; }
      B.classList.add("side-open"); burger.setAttribute("aria-expanded", "true");
      bd = document.createElement("div"); bd.className = "adm-backdrop"; bd.addEventListener("click", close); document.body.appendChild(bd);
    });
    const lo = $("adm-logout");
    if (lo) lo.addEventListener("click", async () => {
      try { await fetch("/api/logout", { method: "POST", credentials: "same-origin" }); } catch (e) { /* go anyway */ }
      location.href = "/login";
    });
    document.querySelectorAll("time[data-time]").forEach((t) => { t.textContent = fmtDT(t.dataset.time); t.setAttribute("datetime", t.dataset.time); });
  }

  // ------------------------------------------------------------------ shared renderers
  function onlineRows(list, compactView) {
    return table(compactView ? [
      { h: "Member", f: (m) => `<span class="who"><span class="live-dot${m.status === "idle" ? " idle" : ""}"></span><span><b><a href="/admin/users/${m.user_id}">${esc(m.email)}</a></b><small>${esc(m.page || "")}</small></span></span>` },
      { h: "Last activity", f: (m) => esc(ago(m.last_seen)) },
      { h: "Session", f: (m) => `${badge(m.status)} <span class="faint">${m.session_minutes} min</span>` },
    ] : [
      { h: "Member", f: (m) => `<span class="who"><span class="live-dot${m.status === "idle" ? " idle" : m.status === "online" ? "" : " off"}"></span><span><b><a href="/admin/users/${m.user_id}">${esc(m.email)}</a></b><small>${esc(m.name || "")}</small></span></span>` },
      { h: "Status", f: (m) => badge(m.status) },
      { h: "Page", f: (m) => `<span class="mono" style="font-size:13px">${esc(m.page || "—")}</span>` },
      { h: "Last activity", f: (m) => `${esc(ago(m.last_seen))}<br><small class="faint">${esc(fmtDT(m.last_seen))}</small>` },
      { h: "Session", f: (m) => `${m.session_minutes} min<br><small class="faint">since ${esc(fmtDT(m.session_started))}</small>` },
      { h: "Device", f: (m) => esc(m.device || "—") },
    ], list, emptyHtml("Nobody is online right now", "A member counts as online while a page of the site is open (heartbeat every 45 seconds)."));
  }
  function userCard(u) {
    const sub = u.current_subscription;
    return `<div class="ucard fade-in"><div class="ava lg">${esc(initials(u.full_name, u.email))}</div><div class="info">` +
      `<div class="head">${esc(u.full_name || u.email)} ${badge(u.account_status)} ${badge(u.presence)}</div>` +
      `<span>${svg("user")}${esc(u.email)}</span>` +
      `<span>${svg("crown")}Subscription: ${sub ? `${esc(sub.plan_name)} · ${badge(sub.status)}` : "none"}</span>` +
      `<span>${svg("clock")}Joined: ${esc(fmtDT(u.created_at))}</span>` +
      `<span>${svg("check")}Last active: ${esc(ago(u.last_active))}</span></div>` +
      `<a class="btn outline-green" href="/admin/users/${u.id}">View details →</a></div>`;
  }

  // ------------------------------------------------------------------ dashboard
  async function pageDashboard() {
    const setStat = (key, value, delta, series, color) => {
      const c = document.querySelector(`[data-stat="${key}"]`);
      if (!c) return;
      c.querySelector(".val").innerHTML = `<span class="fade-in">${value}</span>`;
      c.querySelector(".delta").innerHTML = delta || "";
      if (series) sparkline(c.querySelector(".spark"), series, color);
    };
    const chg = (p) => (p == null ? "" : `<b class="${p >= 0 ? "up" : "down"}">${p >= 0 ? "↑" : "↓"} ${Math.abs(p)}%</b>`);
    async function overview() {
      try {
        const d = await api("/api/admin/overview");
        const s = d.series;
        setStat("users", fmtN(d.users_total), `${chg(d.registered_change)} +${fmtN(d.registered_7d)} this week`, s.registrations, COLORS.green);
        setStat("active", fmtN(d.subscriptions_active), `${fmtN(d.subscriptions_total)} subscriptions in all`, s.subscriptions, COLORS.gold);
        setStat("online", fmtN(d.online), `heartbeat in the last ${Math.round(d.online_window_sec / 60)} min`, s.active_users, COLORS.blue);
        setStat("visitors", compact(d.visitors_total), d.visitor_tracking ? `${chg(d.visitors_change)} ${fmtN(d.visitors_7d)} in 7 days` : "visitor counting is off", s.visitors, COLORS.purple);
        const others = Object.keys(d.revenue || {}).filter((c) => c !== d.currency);
        setStat("revenue", esc(money(d.revenue_main, d.currency)), `${chg(d.revenue_change)} ${esc(money(d.revenue_7d, d.currency))} in 7 days` +
          (others.length ? ` · +${others.map((c) => esc(money(d.revenue[c], c))).join(", ")}` : ""), s.revenue, COLORS.teal);
        setStat("expired", fmtN(d.subscriptions_expired), `${fmtN(d.subscriptions_pending)} pending · ${fmtN(d.subscriptions_cancelled)} cancelled`, null);
        setStat("unused", fmtN(d.subscriptions_never_used), "active or expired, never used", null);
        $("recent").innerHTML = table([
          { h: "#", f: (r, i) => `<span class="faint">${i + 1}</span>` },
          { h: "Action", f: (r) => evCell(r.kind) },
          { h: "User", f: (r) => r.user_id ? `<a class="trunc" href="/admin/users/${r.user_id}" title="${esc(r.email || "")}">${esc(r.email || "—")}</a>` : `<span class="trunc">${esc(r.email || r.actor || "—")}</span>` },
          { h: "Time", cls: "nw", f: (r) => `<span title="${esc(fmtDT(r.at))}">${esc(ago(r.at))}</span>` },
        ], d.recent, emptyHtml("No activity recorded yet", "Registrations, logins, subscriptions and admin actions appear here as they happen."));
        const tot = d.subscriptions_total;
        donut($("sub-donut"), [
          { label: "Active", value: d.subscriptions_active, color: COLORS.green },
          { label: "Expired", value: d.subscriptions_expired, color: COLORS.gold },
          { label: "Pending", value: d.subscriptions_pending, color: COLORS.blue },
          { label: "Cancelled", value: d.subscriptions_cancelled, color: "#5b655e" },
        ], { value: tot ? Math.round((d.subscriptions_active / tot) * 100) + "%" : "–", label: tot ? "active" : "no subscriptions" });
        $("sub-donut").insertAdjacentHTML("beforeend", `<p class="faint" style="font-size:13px;margin:12px 0 0">Never used: <b class="num">${fmtN(d.subscriptions_never_used)}</b> · ` +
          `<a href="/admin/subscriptions">Manage →</a></p>`);
      } catch (e) {
        document.querySelectorAll("[data-stat] .val").forEach((v) => { if (v.querySelector(".sk")) v.textContent = "—"; });
        $("recent").innerHTML = errHtml(e);
      }
    }
    async function chart(range) {
      const el = $("ov-chart");
      try {
        const d = await api(`/api/admin/analytics?range=${range}`);
        drawChart(el, { title: "Visitors, registrations and purchases", labels: d.days, series: [
          { name: "Visitors", values: d.visitors, color: COLORS.blue },
          { name: "Registrations", values: d.registrations, color: COLORS.green, axis: "right" },
          { name: "Subscriptions", values: d.subscriptions, color: COLORS.gold, axis: "right" }] });
      } catch (e) { el.innerHTML = errHtml(e); }
    }
    async function online() {
      try {
        const d = await api("/api/admin/online");
        $("online-list").innerHTML = onlineRows(d.online, true);
        $("online-note").textContent = `${fmtN(d.count)} online · updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
      } catch (e) { $("online-list").innerHTML = errHtml(e); }
    }
    segment("ov-range", (r) => chart(r));
    $("find-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const email = $("find-email").value.trim();
      const out = $("find-out");
      out.innerHTML = '<span class="sk block" style="height:120px;margin-top:14px"></span>';
      try {
        const d = await api(`/api/admin/users/lookup?email=${encodeURIComponent(email)}`);
        out.innerHTML = userCard(d.user);
      } catch (err) {
        const sim = (err.data && err.data.similar) || [];
        out.innerHTML = `<div class="err fade-in" style="margin-top:14px">${esc(err.message)}</div>` + (sim.length ? `<p class="dim" style="margin:12px 0 6px;font-size:13px">Similar:</p>` +
          sim.map((u) => `<div class="list-row"><div class="ava">${esc(initials(u.full_name, u.email))}</div><div class="grow"><b><a href="/admin/users/${u.id}">${esc(u.email)}</a></b><small>Joined ${esc(fmtD(u.created_at))}</small></div></div>`).join("") : "");
      }
    });
    overview(); chart(7); online();
    setInterval(online, 30000);
    setInterval(overview, 60000);
  }

  // ------------------------------------------------------------------ users
  async function pageUsers() {
    let page = 1;
    const getStatus = segment("u-status", () => { page = 1; load(); });
    async function load() {
      const el = $("u-table");
      try {
        const d = await api(`/api/admin/users?q=${encodeURIComponent($("u-q").value.trim())}&status=${getStatus()}&page=${page}`);
        $("u-total").textContent = fmtN(d.total);
        el.innerHTML = table([
          { h: "Member", f: (u) => `<span class="who"><span class="ava">${esc(initials(u.full_name, u.email))}</span><span><b><a href="/admin/users/${u.id}">${esc(u.email)}</a></b><small>${esc(u.full_name || "No name")}</small></span></span>` },
          { h: "Registered", f: (u) => esc(fmtD(u.created_at)) },
          { h: "Account", f: (u) => badge(u.account_status) + (u.email_verified ? "" : ` ${badge("pending", "email not confirmed")}`) },
          { h: "Subscription", f: (u) => u.subscription ? `${badge(u.subscription.status)}<br><small class="faint">${esc(u.subscription.plan)}${u.subscription.expires_at ? " · until " + esc(fmtD(u.subscription.expires_at)) : ""}</small>` : badge("none") },
          { h: "Analyses", cls: "num", f: (u) => fmtN(u.analyses) },
          { h: "Last seen", f: (u) => `${badge(u.presence)}<br><small class="faint">${esc(u.last_seen ? ago(u.last_seen) : "—")}</small>` },
          { h: "", f: (u) => `<a class="btn sm" href="/admin/users/${u.id}">View</a>` },
        ], d.users, emptyHtml("No members match", "Try another part of the email address."));
        pager($("u-pager"), d, (p) => { page = p; load(); });
      } catch (e) { el.innerHTML = errHtml(e); }
    }
    async function online() {
      try {
        const d = await api("/api/admin/online");
        $("online-count").textContent = fmtN(d.count);
        $("online-note").textContent = `Online = a heartbeat in the last ${Math.round(d.window_sec / 60)} min · updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
        $("online-table").innerHTML = onlineRows(d.online.concat(d.recent), false);
      } catch (e) { $("online-table").innerHTML = errHtml(e); }
    }
    $("u-q").addEventListener("input", debounce(() => { page = 1; load(); }, 300));
    load(); online();
    setInterval(online, 20000);
  }

  // ------------------------------------------------------------------ subscription dialogs (users + subscriptions pages)
  let PLANS = null;
  async function plans() { if (!PLANS) PLANS = (await api("/api/admin/plans")).plans; return PLANS; }
  const METHODS = ["Bank transfer", "JazzCash", "Easypaisa", "Card", "Cash", "Crypto", "Other"];

  async function newSubscription(email, onDone) {
    let list;
    try { list = await plans(); } catch (e) { toast(e.message, true); return; }
    const usable = list.filter((p) => p.active);
    if (!usable.length) {
      modal("No plans yet", `<p style="margin:0">Create the plans you sell first (name, price, days). Then you can add subscriptions here.</p>`,
        [{ label: "Close" }, { label: "Go to Settings", cls: "primary", run: () => { location.href = "/admin/settings#plans"; } }]);
      return;
    }
    const opt = usable.map((p) => `<option value="${p.id}" data-price="${p.price}" data-cur="${esc(p.currency)}" data-days="${p.duration_days}">${esc(p.name)} · ${esc(money(p.price, p.currency))} · ${p.duration_days} days</option>`).join("");
    const m = modal("New subscription", `
      <div class="form-grid">
        <label class="field full">Member email<input class="input" id="ns-email" type="email" value="${esc(email || "")}" placeholder="member@gmail.com" required></label>
        <label class="field full">Plan<select class="select input" id="ns-plan">${opt}</select></label>
        <label class="field">Status<select class="select input" id="ns-status"><option value="active">Active now</option><option value="pending">Pending (waiting for payment)</option></select></label>
        <label class="field">Days<input class="input" id="ns-days" type="number" min="1" max="3660"></label>
        <label class="field">Price<input class="input" id="ns-price" type="number" min="0" step="0.01"></label>
        <label class="field">Currency<input class="input" id="ns-cur" maxlength="3" style="text-transform:uppercase"></label>
        <label class="field full">Start date (optional, default now)<input class="input" id="ns-start" type="date"></label>
        <label class="field full">Note (optional)<input class="input" id="ns-note" maxlength="300"></label>
      </div>
      <label class="check"><input type="checkbox" id="ns-pay" checked> Record a payment for it</label>
      <div class="form-grid" id="ns-paybox">
        <label class="field">Amount paid<input class="input" id="ns-amount" type="number" min="0" step="0.01"></label>
        <label class="field">Method<select class="select input" id="ns-method">${METHODS.map((x) => `<option>${x}</option>`).join("")}</select></label>
        <label class="field">Reference / transaction id<input class="input" id="ns-ref" maxlength="120"></label>
        <label class="field">Payment status<select class="select input" id="ns-pstatus"><option value="verified">Verified (counts as revenue)</option><option value="pending">Pending (not counted yet)</option></select></label>
      </div>`, [
      { label: "Cancel" },
      { label: "Create subscription", cls: "primary", run: async (c) => {
        const body = { email: c.q("#ns-email").value.trim(), plan_id: Number(c.q("#ns-plan").value), status: c.q("#ns-status").value,
          days: c.q("#ns-days").value, price: c.q("#ns-price").value, currency: c.q("#ns-cur").value.trim().toUpperCase(),
          starts_at: c.q("#ns-start").value ? c.q("#ns-start").value + " 00:00:00" : null, note: c.q("#ns-note").value };
        if (c.q("#ns-pay").checked) body.payment = { amount: c.q("#ns-amount").value, method: c.q("#ns-method").value,
          reference: c.q("#ns-ref").value, status: c.q("#ns-pstatus").value, currency: body.currency };
        await api("/api/admin/subscriptions", { body });
        c.close(); toast("Subscription created."); if (onDone) onDone();
      } },
    ]);
    const fill = () => {
      const o = m.q("#ns-plan").selectedOptions[0];
      m.q("#ns-days").value = o.dataset.days; m.q("#ns-price").value = o.dataset.price; m.q("#ns-cur").value = o.dataset.cur;
      m.q("#ns-amount").value = o.dataset.price;
    };
    m.q("#ns-plan").addEventListener("change", fill);
    m.q("#ns-price").addEventListener("input", () => { m.q("#ns-amount").value = m.q("#ns-price").value; });
    m.q("#ns-pay").addEventListener("change", () => { m.q("#ns-paybox").hidden = !m.q("#ns-pay").checked; });
    fill();
  }

  async function subscriptionDialog(sid, onChange) {
    let s;
    try { s = (await api(`/api/admin/subscriptions/${sid}`)).subscription; } catch (e) { toast(e.message, true); return; }
    const pays = table([
      { h: "Date", f: (p) => esc(fmtDT(p.paid_at)) },
      { h: "Amount", cls: "num", f: (p) => esc(money(p.amount, p.currency)) },
      { h: "Method", f: (p) => esc(p.method || "—") + (p.reference ? `<br><small class="faint mono">${esc(p.reference)}</small>` : "") },
      { h: "Status", f: (p) => badge(p.status) + (p.verified_by ? `<br><small class="faint">${esc(p.verified_by)}</small>` : "") },
      { h: "", f: (p) => ["verified", "refunded", "rejected"].filter((x) => x !== p.status).map((x) =>
        `<button class="btn sm${x === "verified" ? " outline-green" : " ghost"}" type="button" data-pay="${p.id}" data-st="${x}">${x === "verified" ? "Verify" : pretty(x === "refunded" ? "refund" : "reject")}</button>`).join(" ") },
    ], s.payments, `<p class="faint" style="margin:0">No payments recorded for this subscription.</p>`);
    const acts = [];
    if (s.status !== "active") acts.push(["active", "Activate", "outline-green"]);
    if (s.status === "active") acts.push(["expired", "Mark expired", "ghost"]);
    if (s.status !== "cancelled") acts.push(["cancelled", "Cancel subscription", "danger"]);
    const m = modal(`Subscription #${s.id} · ${s.plan_name}`, `
      <div class="kv">
        <div><dt>Member</dt><dd><a href="/admin/users/${s.user_id}">${esc(s.email || "—")}</a></dd></div>
        <div><dt>Status</dt><dd>${badge(s.status)}</dd></div>
        <div><dt>Price</dt><dd>${esc(money(s.price, s.currency))} · ${s.days} days</dd></div>
        <div><dt>Purchased</dt><dd>${esc(fmtDT(s.purchased_at))}</dd></div>
        <div><dt>Starts</dt><dd>${esc(fmtDT(s.starts_at))}</dd></div>
        <div><dt>Expires</dt><dd>${esc(fmtDT(s.expires_at))}</dd></div>
        <div><dt>Used</dt><dd>${s.ever_used ? `Yes · ${fmtN(s.use_count)} uses` : "Never used"}</dd></div>
        <div><dt>First / last use</dt><dd>${esc(fmtDT(s.first_used_at))}<br>${esc(fmtDT(s.last_used_at))}</dd></div>
        <div><dt>Created by</dt><dd>${esc(s.created_by || s.source)}</dd></div>
        ${s.cancel_reason ? `<div><dt>Cancel reason</dt><dd>${esc(s.cancel_reason)}</dd></div>` : ""}
        ${s.note ? `<div><dt>Note</dt><dd>${esc(s.note)}</dd></div>` : ""}
      </div>
      <div class="toolbar">${acts.map((a) => `<button class="btn sm ${a[2]}" type="button" data-st="${a[0]}">${a[1]}</button>`).join("")}
        <span class="spacer" style="flex:1"></span>
        <input class="input" id="sd-days" type="number" min="1" max="3660" value="30" style="width:90px" aria-label="Days to add">
        <button class="btn sm" type="button" id="sd-extend">Extend by days</button></div>
      <h4 style="margin:6px 0 0;font:700 15px var(--head)">Payments</h4>${pays}
      <details><summary class="dim" style="cursor:pointer;font-weight:600">Record a payment</summary>
        <div class="form-grid" style="margin-top:12px">
          <label class="field">Amount<input class="input" id="sd-amount" type="number" min="0" step="0.01" value="${esc(s.price)}"></label>
          <label class="field">Currency<input class="input" id="sd-cur" maxlength="3" value="${esc(s.currency)}"></label>
          <label class="field">Method<select class="select input" id="sd-method">${METHODS.map((x) => `<option>${x}</option>`).join("")}</select></label>
          <label class="field">Reference<input class="input" id="sd-ref" maxlength="120"></label>
          <label class="field">Status<select class="select input" id="sd-pstatus"><option value="verified">Verified</option><option value="pending">Pending</option></select></label>
          <div class="field" style="align-self:end"><button class="btn primary" type="button" id="sd-pay">Save payment</button></div>
        </div></details>
      <h4 style="margin:6px 0 0;font:700 15px var(--head)">History</h4>
      ${s.events.length ? s.events.map((e) => `<div class="list-row">${evCell(e.kind)}<div class="grow"><small>${esc(e.detail || "")}${e.actor ? " · by " + esc(e.actor) : ""}</small></div><small class="faint">${esc(fmtDT(e.at))}</small></div>`).join("") : '<p class="faint" style="margin:0">No history yet.</p>'}
    `, [{ label: "Close" }], { wide: true });
    const after = async (msg) => { m.close(); toast(msg); if (onChange) await onChange(); subscriptionDialog(sid, onChange); };
    m.el.querySelectorAll("[data-st]:not([data-pay])").forEach((b) => b.addEventListener("click", async () => {
      const st = b.dataset.st;
      let reason = null;
      if (st === "cancelled") {
        if (!(await confirmBox("Cancel this subscription?", "The member loses it at once. You can activate it again later.", "Cancel subscription", "danger"))) return;
        reason = "cancelled by the admin";
      }
      try { await api(`/api/admin/subscriptions/${sid}/status`, { body: { status: st, reason } }); await after("Subscription updated."); }
      catch (e) { m.msg(e.message, true); }
    }));
    m.q("#sd-extend").addEventListener("click", async () => {
      try { await api(`/api/admin/subscriptions/${sid}/extend`, { body: { days: m.q("#sd-days").value } }); await after("Subscription extended."); }
      catch (e) { m.msg(e.message, true); }
    });
    m.q("#sd-pay").addEventListener("click", async () => {
      try {
        await api(`/api/admin/subscriptions/${sid}/payments`, { body: { amount: m.q("#sd-amount").value, currency: m.q("#sd-cur").value.trim().toUpperCase(),
          method: m.q("#sd-method").value, reference: m.q("#sd-ref").value, status: m.q("#sd-pstatus").value } });
        await after("Payment saved.");
      } catch (e) { m.msg(e.message, true); }
    });
    m.el.querySelectorAll("[data-pay]").forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/api/admin/payments/${b.dataset.pay}/status`, { body: { status: b.dataset.st } }); await after("Payment updated."); }
      catch (e) { m.msg(e.message, true); }
    }));
  }

  // ------------------------------------------------------------------ member profile
  async function pageUser() {
    const uid = B.dataset.userId;
    const el = $("profile");
    async function load() {
      let u;
      try { u = (await api(`/api/admin/users/${uid}`)).user; } catch (e) { el.innerHTML = errHtml(e); return; }
      const sub = u.current_subscription;
      const t = u.trades || {};
      const tc = (x) => (x ? `${fmtN(x.total)}${x.open ? ` <small class="faint">(${fmtN(x.open)} open)</small>` : ""}` : "—");
      const kv = [
        ["Full name", esc(u.full_name || "Not given")], ["Gmail / email", esc(u.email)], ["Registration date", esc(fmtDT(u.created_at))],
        ["Account status", badge(u.account_status)], ["Email confirmed", u.email_verified ? badge("yes") : badge("no")],
        ["Sign-in", badge(u.auth_provider || "password")],
        ["Subscription status", badge(u.subscription_status)], ["Subscription plan", esc(sub ? sub.plan_name : "—")],
        ["Purchase date", esc(sub ? fmtDT(sub.purchased_at) : "—")], ["Expiry date", esc(sub ? fmtDT(sub.expires_at) : "—")],
        ["Ever used the subscription", u.ever_used_subscription == null ? "No subscription" : (u.ever_used_subscription ? badge("yes") : badge("no", "never used"))],
        ["Verified payments", esc(money(u.paid_verified, sub ? sub.currency : CUR))],
        ["Analyses performed", `<span class="num">${fmtN(u.analyses)}</span>`], ["Last active", `${esc(ago(u.last_active))}<br><small class="faint">${esc(fmtDT(u.last_active))}</small>`],
        ["Last login", esc(fmtDT(u.last_login))], ["Logins / active days", `<span class="num">${fmtN(u.logins)} / ${fmtN(u.active_days)}</span>`],
        ["Online now", badge(u.presence) + (u.path ? `<br><small class="faint mono">${esc(u.path)}</small>` : "")],
        ["Tracked trades", tc(t.tracked)], ["Auto-trade bot trades", tc(t.bot)],
        ["Demo trades", t.demo_closed || t.demo_open ? `${fmtN((t.demo_closed || {}).total)} closed · ${fmtN((t.demo_open || {}).total)} open` : "—"],
      ];
      el.innerHTML = `
        <article class="card fade-in"><div class="ucard" style="margin:0;background:none;border:0;padding:0">
          <div class="ava lg">${esc(initials(u.full_name, u.email))}</div>
          <div class="info"><div class="head">${esc(u.full_name || u.email)} ${badge(u.account_status)} ${badge(u.presence)} ${u.is_admin ? badge("admin") : ""}</div>
            <span>${svg("user")}${esc(u.email)} · member #${u.id}</span></div>
          <div class="toolbar">
            <button class="btn sm" type="button" id="p-name">${svg("edit")}Edit name</button>
            <button class="btn sm" type="button" id="p-logout">${svg("login")}Sign out everywhere</button>
            ${u.is_admin ? "" : (u.account_status === "blocked" ? `<button class="btn sm outline-green" type="button" id="p-block" data-to="active">${svg("check")}Re-activate</button>`
              : `<button class="btn sm danger" type="button" id="p-block" data-to="blocked">${svg("ban")}Suspend</button>`)}
            <button class="btn sm primary" type="button" id="p-sub">${svg("plus")}Add subscription</button>
          </div></div></article>
        <article class="card fade-in"><div class="card-h"><h2>Account and usage</h2></div>
          <dl class="kv">${kv.map((k) => `<div><dt>${k[0]}</dt><dd>${k[1]}</dd></div>`).join("")}</dl></article>
        <article class="card fade-in"><div class="card-h"><h2>Subscriptions</h2></div>${table([
          { h: "Plan", f: (s) => `<b>${esc(s.plan_name)}</b><br><small class="faint">#${s.id} · ${esc(money(s.price, s.currency))} · ${s.days} days</small>` },
          { h: "Status", f: (s) => badge(s.status) },
          { h: "Purchased", f: (s) => esc(fmtD(s.purchased_at)) },
          { h: "Expires", f: (s) => esc(fmtDT(s.expires_at)) },
          { h: "Used", f: (s) => (s.first_used_at ? `Yes · ${fmtN(s.use_count)}<br><small class="faint">last ${esc(ago(s.last_used_at))}</small>` : badge("no", "never used")) },
          { h: "", f: (s) => `<button class="btn sm" type="button" data-sub="${s.id}">Manage</button>` },
        ], u.subscriptions, `<p class="faint" style="margin:0">No subscriptions. Use "Add subscription" to give or sell one.</p>`)}</article>
        <section class="grid g-half">
          <article class="card fade-in"><div class="card-h"><h2>Payments</h2></div>${table([
            { h: "Date", f: (p) => esc(fmtDT(p.paid_at)) },
            { h: "Amount", cls: "num", f: (p) => esc(money(p.amount, p.currency)) },
            { h: "Method", f: (p) => esc(p.method || "—") },
            { h: "Status", f: (p) => badge(p.status) },
          ], u.payments, `<p class="faint" style="margin:0">No payments recorded.</p>`)}</article>
          <article class="card fade-in"><div class="card-h"><h2>Recent activity</h2><span class="spacer"></span>
            <a href="/admin/trades?q=${encodeURIComponent(u.email)}">Trade history →</a></div>
            ${u.events.length ? u.events.map((e) => `<div class="list-row">${evCell(e.kind)}<div class="grow"><small>${esc(e.detail || "")}${e.actor ? " · by " + esc(e.actor) : ""}</small></div><small class="faint">${esc(fmtDT(e.at))}</small></div>`).join("")
              : '<p class="faint" style="margin:0">No activity recorded yet.</p>'}</article>
        </section>`;
      el.querySelectorAll("[data-sub]").forEach((b) => b.addEventListener("click", () => subscriptionDialog(b.dataset.sub, load)));
      $("p-sub").addEventListener("click", () => newSubscription(u.email, load));
      $("p-name").addEventListener("click", () => {
        modal("Edit name", `<label class="field">Full name<input class="input" id="pn" maxlength="120" value="${esc(u.full_name || "")}"></label>`, [
          { label: "Cancel" },
          { label: "Save", cls: "primary", run: async (c) => { await api(`/api/admin/users/${uid}/name`, { body: { full_name: c.q("#pn").value } }); c.close(); toast("Name saved."); load(); } }]);
      });
      $("p-logout").addEventListener("click", async () => {
        if (!(await confirmBox("Sign out everywhere?", `${esc(u.email)} is logged out on every device and has to log in again.`, "Sign out everywhere"))) return;
        try { await api(`/api/admin/users/${uid}/logout`, { body: {} }); toast("The member was signed out everywhere."); load(); } catch (e) { toast(e.message, true); }
      });
      const bl = $("p-block");
      if (bl) bl.addEventListener("click", async () => {
        const to = bl.dataset.to;
        if (to === "blocked" && !(await confirmBox("Suspend this account?", `${esc(u.email)} is logged out at once and cannot log in (password or Google) until you re-activate the account. Their data stays.`, "Suspend", "danger"))) return;
        try { await api(`/api/admin/users/${uid}/status`, { body: { status: to } }); toast(to === "blocked" ? "Account suspended." : "Account active again."); load(); }
        catch (e) { toast(e.message, true); }
      });
    }
    load();
  }

  // ------------------------------------------------------------------ subscriptions page
  async function pageSubscriptions() {
    let page = 1;
    const getStatus = segment("s-status", () => { page = 1; load(); });
    async function load() {
      const el = $("s-table");
      try {
        const d = await api(`/api/admin/subscriptions?status=${getStatus()}&q=${encodeURIComponent($("s-q").value.trim())}&page=${page}`);
        const sm = d.summary;
        const set = (k, v) => { const b = document.querySelector(`#sub-summary [data-k="${k}"] b`); if (b) b.textContent = v; };
        set("active", fmtN(sm.active)); set("expired", fmtN(sm.expired)); set("pending", fmtN(sm.pending)); set("cancelled", fmtN(sm.cancelled));
        set("unused", fmtN(sm.unused)); set("usage_rate", sm.usage_rate == null ? "—" : sm.usage_rate + "%"); set("uses", fmtN(sm.uses));
        $("s-total").textContent = fmtN(d.total);
        el.innerHTML = table([
          { h: "#", f: (s) => `<span class="faint">${s.id}</span>` },
          { h: "Member", f: (s) => `<a href="/admin/users/${s.user_id}">${esc(s.email || "—")}</a>` },
          { h: "Plan", f: (s) => `<b>${esc(s.plan_name)}</b><br><small class="faint">${esc(money(s.price, s.currency))} · ${s.days} days</small>` },
          { h: "Status", f: (s) => badge(s.status) },
          { h: "Purchased", f: (s) => esc(fmtD(s.purchased_at)) },
          { h: "Expires", f: (s) => esc(fmtD(s.expires_at)) },
          { h: "Used", f: (s) => (s.ever_used ? `Yes · ${fmtN(s.use_count)}` : badge("no", "never")) },
          { h: "Paid (verified)", cls: "num", f: (s) => esc(money(s.paid_verified, s.currency)) + (s.payments_pending ? `<br><small class="warn-t">${s.payments_pending} pending</small>` : "") },
          { h: "", f: (s) => `<button class="btn sm" type="button" data-sub="${s.id}">Details</button>` },
        ], d.subscriptions, emptyHtml("No subscriptions here", sm.total ? "Try another filter." : 'Give or sell the first one with "New subscription". Plans are set up on the Settings page.'));
        el.querySelectorAll("[data-sub]").forEach((b) => b.addEventListener("click", () => subscriptionDialog(b.dataset.sub, load)));
        pager($("s-pager"), d, (p) => { page = p; load(); });
      } catch (e) { el.innerHTML = errHtml(e); }
    }
    $("s-q").addEventListener("input", debounce(() => { page = 1; load(); }, 300));
    $("s-new").addEventListener("click", () => newSubscription("", load));
    load();
  }

  // ------------------------------------------------------------------ analytics
  async function pageAnalytics() {
    async function load(range) {
      $("a-note").textContent = "Loading…";
      try {
        const d = await api(`/api/admin/analytics?range=${range}`);
        const t = d.totals;
        $("a-tot").innerHTML = [["Visitors", compact(t.visitors)], ["New registrations", fmtN(t.registrations)], ["Subscription purchases", fmtN(t.subscriptions)],
          ["Revenue", money(t.revenue, d.currency)], ["Logins", fmtN(t.logins)], ["Analyses", fmtN(t.analyses)]]
          .map((x) => `<div class="fade-in"><span>${esc(x[0])}</span><b>${esc(x[1])}</b></div>`).join("");
        $("rev-cur").textContent = `(${d.currency})`;
        const L = d.days;
        drawChart($("c-visitors"), { title: "Daily visitors", labels: L, series: [{ name: "Visitors", values: d.visitors, color: COLORS.blue }] });
        drawChart($("c-reg"), { title: "New registrations", type: "bar", labels: L, series: [{ name: "Registrations", values: d.registrations, color: COLORS.green }] });
        drawChart($("c-weekly"), { title: "Weekly visitors", type: "bar", labels: d.weekly_visitors.map((w) => w.week), xfmt: (s) => fmtDay(s),
          tipfmt: (s) => "Week of " + fmtDay(s), series: [{ name: "Visitors", values: d.weekly_visitors.map((w) => w.n), color: COLORS.purple }] });
        drawChart($("c-monthly"), { title: "Monthly visitors", type: "bar", labels: d.monthly_visitors.map((m) => m.month),
          xfmt: (s) => { const dd = parseT(s + "-01"); return dd ? dd.toLocaleDateString([], { month: "short", year: "2-digit", timeZone: "UTC" }) : s; },
          series: [{ name: "Visitors", values: d.monthly_visitors.map((m) => m.n), color: COLORS.teal }] });
        drawChart($("c-subs"), { title: "Subscription purchases", type: "bar", labels: L, series: [{ name: "Purchases", values: d.subscriptions, color: COLORS.gold }] });
        drawChart($("c-rev"), { title: "Revenue", labels: L, fmt: (v) => money(v, d.currency), series: [{ name: "Revenue", values: d.revenue, color: COLORS.teal }] });
        drawChart($("c-activity"), { title: "Member activity", labels: L, area: false, series: [
          { name: "Active members", values: d.active_users, color: COLORS.green }, { name: "Logins", values: d.logins, color: COLORS.blue },
          { name: "Analyses", values: d.analyses, color: COLORS.orange }] });
        const others = Object.keys(d.revenue_by_currency).filter((c) => c !== d.currency);
        $("a-note").textContent = `Updated ${fmtDT(d.generated_at)}` + (others.length ? ` · other currencies: ${others.map((c) => money(d.revenue_by_currency[c], c)).join(", ")}` : "");
      } catch (e) { $("a-note").textContent = e.message; }
    }
    segment("a-range", (r) => load(r));
    load(30);
  }

  // ------------------------------------------------------------------ trade history
  async function pageTrades() {
    let page = 1;
    const qp = new URLSearchParams(location.search);
    if (qp.get("q")) $("t-q").value = qp.get("q");
    const getSource = segment("t-source", () => { page = 1; load(); });
    const getStatus = segment("t-status", () => { page = 1; load(); });
    const NOTES = { tracked: "Trades members track from the Pro terminal: the signal it showed (direction, confidence, timeframe) and how the trade went. P&L is the estimated % move.",
      bot: "Orders the auto-trade bot placed on members' accounts (demo or live) from the site's signals. P&L in USDT.",
      demo: "Positions in Demo trading (virtual money). P&L in demo USDT." };
    const tstatus = (s) => {
      const k = String(s || "").toLowerCase();
      return badge(k === "active" || k === "open" ? "open" : k, k === "active" ? "open" : null);
    };
    async function load() {
      const el = $("t-table");
      const src = getSource();
      $("t-note").textContent = NOTES[src] || "";
      try {
        const d = await api(`/api/admin/trades?source=${src}&status=${getStatus()}&q=${encodeURIComponent($("t-q").value.trim())}&page=${page}`);
        Object.entries(d.counts || {}).forEach(([k, v]) => { const n = document.querySelector(`[data-c="${k}"]`); if (n) n.textContent = v ? fmtN(v.total) : "0"; });
        el.innerHTML = table([
          { h: "Member", f: (t) => `<a href="/admin/users/${t.user_id}">${esc(t.email)}</a>` },
          { h: "Coin", f: (t) => `<b>${esc(t.symbol)}</b>${t.mode ? `<br><small class="faint">${esc(t.mode)}</small>` : ""}` },
          { h: "Signal", f: (t) => `<span class="${String(t.side).toUpperCase() === "LONG" || String(t.side).toUpperCase() === "BUY" ? "up" : "down"}" style="font-weight:700">${esc(t.signal || t.side)}</span>` },
          { h: "Signal date", f: (t) => esc(fmtDT(t.opened_at)) },
          { h: "Status", f: (t) => tstatus(t.status) },
          { h: "Entry / stop / target", cls: "num", f: (t) => `${nf(t.entry)}<br><small class="faint">${nf(t.stop)} / ${nf(t.target)}</small>` },
          { h: "Closed", f: (t) => (t.closed_at ? `${esc(fmtDT(t.closed_at))}<br><small class="faint">exit ${nf(t.exit)}</small>` : "—") },
          { h: "Result", cls: "num", f: (t) => (t.pnl == null ? "—" : `<b class="${t.pnl >= 0 ? "up" : "down"}">${t.pnl >= 0 ? "+" : ""}${nf(t.pnl, 2)}${t.pnl_unit === "%" ? "%" : ""}</b>${t.pnl_unit && t.pnl_unit !== "%" ? ` <small class="faint">${esc(t.pnl_unit)}</small>` : ""}`) +
            (t.result ? `<br><small class="faint">${esc(pretty(t.result))}</small>` : "") },
        ], d.trades, emptyHtml("No trades here", d.note || "Nothing matches these filters yet."));
        pager($("t-pager"), d, (p) => { page = p; load(); });
      } catch (e) { el.innerHTML = errHtml(e); }
    }
    $("t-q").addEventListener("input", debounce(() => { page = 1; load(); }, 300));
    load();
  }

  // ------------------------------------------------------------------ activity log
  async function pageActivity() {
    let page = 1;
    const getGroup = segment("l-group", () => { page = 1; load(); });
    async function load() {
      const el = $("l-table");
      try {
        const d = await api(`/api/admin/activity?group=${getGroup()}&q=${encodeURIComponent($("l-q").value.trim())}&page=${page}`);
        el.innerHTML = table([
          { h: "Time", cls: "nw", f: (r) => `${esc(fmtDT(r.at))}<br><small class="faint">${esc(ago(r.at))}</small>` },
          { h: "Event", f: (r) => evCell(r.kind) },
          { h: "Member", f: (r) => (r.user_id ? `<a href="/admin/users/${r.user_id}">${esc(r.email || "#" + r.user_id)}</a>` : esc(r.email || "—")) },
          { h: "Details", f: (r) => `<span class="dim">${esc(r.detail || "")}</span>` },
          { h: "By", f: (r) => (r.actor ? badge("admin", r.actor) : `<span class="faint">member / system</span>`) },
        ], d.events, emptyHtml("Nothing recorded here yet", "Events are recorded from the moment the admin panel was installed."));
        pager($("l-pager"), d, (p) => { page = p; load(); });
      } catch (e) { el.innerHTML = errHtml(e); }
    }
    $("l-q").addEventListener("input", debounce(() => { page = 1; load(); }, 300));
    load();
  }

  // ------------------------------------------------------------------ settings
  async function pageSettings() {
    async function load() {
      try {
        const d = await api("/api/admin/settings");
        $("access").innerHTML = `<dl class="kv fade-in">
          <div><dt>Admin emails (ADMIN_EMAILS)</dt><dd>${d.admin_emails.map((e) => esc(e)).join("<br>")}</dd></div>
          <div><dt>You</dt><dd>${esc(d.you)}<br><small class="faint">signed in with ${esc(d.login_method)}</small></dd></div>
          <div><dt>Google login required</dt><dd>${d.require_google ? badge("yes", "on") : badge("none", "off")}</dd></div></dl>`;
        $("recording").innerHTML = `<dl class="kv fade-in">
          <div><dt>Online means</dt><dd>a heartbeat in the last ${Math.round(d.online_window_sec / 60)} min (pages ping every ${d.heartbeat_sec} s)</dd></div>
          <div><dt>Visitor counting</dt><dd>${d.visitor_tracking ? badge("yes", "on") : badge("none", "off")}<br><small class="faint">VISITOR_TRACKING; Do-Not-Track and bots are never counted</small></dd></div>
          <div><dt>Main currency</dt><dd>${esc(d.currency)} <small class="faint">(ADMIN_CURRENCY)</small></dd></div>
          ${Object.entries(d.records).map(([k, v]) => `<div><dt>${esc(pretty(k))} rows</dt><dd class="num">${fmtN(v)}</dd></div>`).join("")}</dl>`;
        $("plans").innerHTML = table([
          { h: "Plan", f: (p) => `<b>${esc(p.name)}</b>${p.description ? `<br><small class="faint">${esc(p.description)}</small>` : ""}` },
          { h: "Price", cls: "num", f: (p) => esc(money(p.price, p.currency)) },
          { h: "Days", cls: "num", f: (p) => fmtN(p.duration_days) },
          { h: "Shown", f: (p) => (p.active ? badge("active", "available") : badge("none", "hidden")) },
          { h: "", f: (p) => `<button class="btn sm" type="button" data-plan="${p.id}">Edit</button>` },
        ], d.plans, emptyHtml("No plans yet", 'Create the plans you sell with "New plan" (for example Monthly, 30 days).'));
        $("plans").querySelectorAll("[data-plan]").forEach((b) => b.addEventListener("click", () => planDialog(d.plans.find((p) => String(p.id) === b.dataset.plan))));
      } catch (e) { $("access").innerHTML = errHtml(e); }
    }
    function planDialog(p) {
      p = p || {};
      modal(p.id ? `Edit plan · ${p.name}` : "New plan", `<div class="form-grid">
          <label class="field full">Name<input class="input" id="pl-name" maxlength="80" value="${esc(p.name || "")}" placeholder="Monthly"></label>
          <label class="field">Price<input class="input" id="pl-price" type="number" min="0" step="0.01" value="${esc(p.price != null ? p.price : "")}"></label>
          <label class="field">Currency<input class="input" id="pl-cur" maxlength="3" value="${esc(p.currency || CUR)}"></label>
          <label class="field">Days<input class="input" id="pl-days" type="number" min="1" max="3660" value="${esc(p.duration_days || 30)}"></label>
          <label class="field">Shown when adding subscriptions<select class="select input" id="pl-active"><option value="1">Yes</option><option value="0"${p.id && !p.active ? " selected" : ""}>No (hidden)</option></select></label>
          <label class="field full">Description (optional)<input class="input" id="pl-desc" maxlength="300" value="${esc(p.description || "")}"></label></div>`, [
        { label: "Cancel" },
        { label: p.id ? "Save plan" : "Create plan", cls: "primary", run: async (c) => {
          const body = { name: c.q("#pl-name").value, price: c.q("#pl-price").value, currency: c.q("#pl-cur").value.trim().toUpperCase(),
            duration_days: c.q("#pl-days").value, active: c.q("#pl-active").value, description: c.q("#pl-desc").value };
          await api(p.id ? `/api/admin/plans/${p.id}` : "/api/admin/plans", { body });
          c.close(); toast(p.id ? "Plan saved." : "Plan created."); PLANS = null; load();
        } }]);
    }
    $("p-new").addEventListener("click", () => planDialog(null));
    load();
  }

  shell();
  ({ dashboard: pageDashboard, users: pageUsers, user: pageUser, subscriptions: pageSubscriptions, analytics: pageAnalytics,
     trades: pageTrades, activity: pageActivity, settings: pageSettings }[PAGE] || (() => {}))();
})();
