/* Signals FM Premium chart workspace: the pure functions (no DOM), shared by static/premium-chart.js and the Node
   tests (tests/test_premium_chart_core.js). window.PCC in the browser, module.exports in Node.
   Intervals and buckets mirror premium_chart.py exactly (same grammar, same bucket starts). */
(function (root) {
  "use strict";

  const UNIT_SEC = { s: 1, m: 60, h: 3600, D: 86400, W: 604800 };
  const LIMITS = { s: [1, 300], m: [1, 1440], h: [1, 168], D: [1, 365], W: [1, 52], M: [1, 12], T: [1, 10000] };
  const MONTH_STEPS = [1, 2, 3, 4, 6, 12];
  const ALIASES = {
    s: "s", sec: "s", secs: "s", second: "s", seconds: "s", m: "m", min: "m", mins: "m", minute: "m", minutes: "m",
    h: "h", hr: "h", hrs: "h", hour: "h", hours: "h", d: "D", day: "D", days: "D", w: "W", wk: "W", week: "W",
    weeks: "W", mo: "M", mon: "M", month: "M", months: "M", t: "T", tick: "T", ticks: "T",
  };
  const NATIVE = { "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
                   "6h": 21600, "12h": 43200 };
  const MONDAY0 = 4 * 86400;
  const UNIT_WORD = { s: "second", m: "minute", h: "hour", D: "day", W: "week", M: "month", T: "tick" };
  const mod = (a, b) => ((a % b) + b) % b;

  // ------------------------------------------------------------------ intervals
  function makeInterval(n, unit) {
    const kind = unit === "s" ? "second" : unit === "T" ? "tick" : unit === "M" ? "month" : "time";
    const seconds = UNIT_SEC[unit] ? n * UNIT_SEC[unit] : null;
    let native = null;
    if (unit === "D" || unit === "W" || unit === "M") native = n === 1 ? { D: "1d", W: "1w", M: "1M" }[unit] : null;
    else if (kind === "time") for (const k in NATIVE) if (NATIVE[k] === seconds) native = k;
    return { n, unit, kind, seconds, native, name: n + unit,
             short: unit === "T" ? `${n}T` : n + unit,
             long: `${n} ${UNIT_WORD[unit]}${n === 1 ? "" : "s"}` };
  }
  function parseInterval(raw) {
    const m = /^\s*(\d{1,5})\s*([A-Za-z]+)\s*$/.exec(String(raw == null ? "" : raw));
    if (!m) return null;
    const n = parseInt(m[1], 10), word = m[2];
    // "M" is a month and "m" a minute; every other word is read without caring about upper / lower case
    const unit = word === "M" ? "M" : ALIASES[word] || ALIASES[word.toLowerCase()];
    if (!unit) return null;
    const lim = LIMITS[unit];
    if (n < lim[0] || n > lim[1]) return null;
    if (unit === "M" && !MONTH_STEPS.includes(n)) return null;
    return makeInterval(n, unit);
  }
  function bucketStart(ts, iv) {
    ts = Math.floor(ts);
    if (iv.unit === "M") {
      const d = new Date(ts * 1000);
      let idx = d.getUTCFullYear() * 12 + d.getUTCMonth();
      idx -= mod(idx, iv.n);
      return Date.UTC(Math.floor(idx / 12), mod(idx, 12), 1) / 1000;
    }
    if (iv.unit === "W") {
      const w = Math.floor((ts - MONDAY0) / 604800);
      return MONDAY0 + (w - mod(w, iv.n)) * 604800;
    }
    const sec = iv.seconds;
    if (sec < 86400) {                                   // intraday (and seconds): restart at 00:00 UTC
      const day = ts - mod(ts, 86400);
      return day + Math.floor((ts - day) / sec) * sec;
    }
    if (sec % 86400 === 0) {
      const days = sec / 86400, d = Math.floor(ts / 86400);
      return (d - mod(d, days)) * 86400;
    }
    return ts - mod(ts, sec);
  }
  function bucketEnd(start, iv) {
    if (iv.unit === "M") {
      const d = new Date(start * 1000);
      return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + iv.n, 1) / 1000;
    }
    if (iv.unit === "W") return start + iv.n * 604800;
    const sec = iv.seconds;
    if (sec < 86400) return Math.min(start + sec, start - mod(start, 86400) + 86400);
    return start + sec;
  }

  // ------------------------------------------------------------------ bars from trades (second and tick charts)
  // trade = [time_ms, price, amount, side, id]; bars keep their real start time in `t` (seconds) and, for ticks,
  // the number of trades in `count`.
  function TradeBars(iv) {
    this.iv = iv;
    this.bars = [];
    this.lastId = null;
    this.lastMs = 0;
  }
  TradeBars.prototype.push = function (tr) {
    const ms = tr[0], px = +tr[1], qty = +tr[2] || 0;
    if (!(px > 0)) return null;
    const iv = this.iv, bars = this.bars;
    let cur = bars.length ? bars[bars.length - 1] : null;
    this.lastId = tr[4]; this.lastMs = ms;
    if (iv.kind === "tick") {
      if (!cur || cur.count >= iv.n) {
        cur = { t: Math.floor(ms / 1000), open: px, high: px, low: px, close: px, volume: qty, count: 1 };
        bars.push(cur);
        return "new";
      }
    } else {
      const b = bucketStart(Math.floor(ms / 1000), iv);
      if (!cur || b > cur.t) {
        cur = { t: b, open: px, high: px, low: px, close: px, volume: qty, count: 1 };
        bars.push(cur);
        return "new";
      }
    }
    if (px > cur.high) cur.high = px;
    if (px < cur.low) cur.low = px;
    cur.close = px;
    cur.volume += qty;
    cur.count += 1;
    return "update";
  };
  // newer trades only (ids grow; time breaks ties); returns how many were added
  TradeBars.prototype.add = function (trades) {
    let added = 0;
    for (const tr of trades || []) {
      if (this.lastId !== null && !isNewer(tr, this.lastMs, this.lastId)) continue;
      if (this.push(tr)) added++;
    }
    return added;
  };
  function idNum(id) { const n = Number(id); return isFinite(n) ? n : 0; }
  function isNewer(tr, lastMs, lastId) {
    if (tr[0] !== lastMs) return tr[0] > lastMs;
    return idNum(tr[4]) > idNum(lastId);
  }
  function barsFromTrades(trades, iv) {
    const tb = new TradeBars(iv);
    tb.add(trades);
    return tb.bars;
  }

  // ------------------------------------------------------------------ helpers
  function atr(bars, n) {
    n = n || 14;
    if (!bars || bars.length < 2) return bars && bars.length ? bars[0].high - bars[0].low : null;
    let a = null;
    for (let i = 1; i < bars.length; i++) {
      const b = bars[i], pc = bars[i - 1].close;
      const tr = Math.max(b.high - b.low, Math.abs(b.high - pc), Math.abs(b.low - pc));
      a = a === null ? tr : a + (tr - a) / n;
    }
    return a;
  }
  function niceStep(x) {
    if (!(x > 0) || !isFinite(x)) return null;
    const p = Math.pow(10, Math.floor(Math.log10(x))), m = x / p;
    const s = m < 1.5 ? 1 : m < 2.25 ? 2 : m < 3.5 ? 2.5 : m < 7.5 ? 5 : 10;
    return +(s * p).toPrecision(6);
  }
  const roundTo = (x, step) => +(Math.round(x / step) * step).toPrecision(12);

  // ------------------------------------------------------------------ price-based charts
  // Each returns items { t (real time of the base bar where it formed), open, high, low, close, volume, ... };
  // they have no time axis of their own, so the chart shows them one after another (see indexed()).
  function heikinAshi(bars) {
    const out = new Array(bars.length);
    let po = null, pc = null;
    for (let i = 0; i < bars.length; i++) {
      const b = bars[i], c = (b.open + b.high + b.low + b.close) / 4;
      const o = po === null ? (b.open + b.close) / 2 : (po + pc) / 2;
      out[i] = { time: b.time, open: o, high: Math.max(b.high, o, c), low: Math.min(b.low, o, c), close: c, volume: b.volume };
      po = o; pc = c;
    }
    return out;
  }

  const MAX_ITEMS = 20000;

  // Range bars: every bar spans exactly `size` from its low to its high; a new bar opens where the last one closed.
  // The price path inside a base candle is taken as open -> low -> high -> close (rising) or open -> high -> low ->
  // close (falling), the usual approximation without tick data.
  function rangeBars(bars, size) {
    const out = [];
    if (!(size > 0) || !bars.length) return out;
    let cur = null, capped = false;
    const start = (t, p) => ({ t, open: p, high: p, low: p, close: p, volume: 0 });
    for (const b of bars) {
      const path = b.close >= b.open ? [b.open, b.low, b.high, b.close] : [b.open, b.high, b.low, b.close];
      for (const p of path) {
        if (!cur) { cur = start(b.time, p); continue; }
        for (;;) {
          if (p > cur.high && p - cur.low > size) {
            const edge = +(cur.low + size).toPrecision(12);
            cur.high = edge; cur.close = edge; out.push(cur);
            cur = start(b.time, edge);
          } else if (p < cur.low && cur.high - p > size) {
            const edge = +(cur.high - size).toPrecision(12);
            cur.low = edge; cur.close = edge; out.push(cur);
            cur = start(b.time, edge);
          } else {
            if (p > cur.high) cur.high = p;
            if (p < cur.low) cur.low = p;
            cur.close = p;
            break;
          }
          if (out.length > MAX_ITEMS) { capped = true; break; }
        }
        if (capped) break;
      }
      if (capped) break;
      cur.volume += b.volume || 0;
    }
    if (cur && !capped) { cur.forming = true; out.push(cur); }
    out.capped = capped;
    return out;
  }

  // Renko on closes: a new brick each time the close moves one box beyond the last brick; a reversal needs two.
  function renko(bars, box) {
    const out = [];
    if (!(box > 0) || !bars.length) return out;
    let hi = roundTo(bars[0].close, box), lo = hi, vol = 0;
    for (const b of bars) {
      const c = b.close;
      vol += b.volume || 0;
      const made = [];
      while (c >= hi + box && out.length + made.length < MAX_ITEMS) {
        made.push({ t: b.time, open: hi, close: +(hi + box).toPrecision(12), up: true });
        lo = hi; hi = +(hi + box).toPrecision(12);
      }
      while (c <= lo - box && out.length + made.length < MAX_ITEMS) {
        made.push({ t: b.time, open: lo, close: +(lo - box).toPrecision(12), up: false });
        hi = lo; lo = +(lo - box).toPrecision(12);
      }
      if (made.length) {
        for (const m of made) { m.high = Math.max(m.open, m.close); m.low = Math.min(m.open, m.close); m.volume = vol / made.length; out.push(m); }
        vol = 0;
      }
    }
    out.capped = out.length >= MAX_ITEMS;
    return out;
  }

  // Three-line break: a new line when the close goes past the last line; a reversal needs a close past the
  // extreme of the last `lines` lines.
  function lineBreak(bars, lines) {
    lines = lines || 3;
    const out = [];
    if (!bars.length) return out;
    let first = bars[0].close, vol = 0;
    for (let i = 1; i < bars.length; i++) {
      const b = bars[i], c = b.close;
      vol += b.volume || 0;
      let add = null;
      if (!out.length) {
        if (c !== first) add = { open: first, close: c };
      } else {
        const last = out[out.length - 1], up = last.close > last.open;
        const recent = out.slice(-lines);
        const hiR = Math.max.apply(null, recent.map((x) => Math.max(x.open, x.close)));
        const loR = Math.min.apply(null, recent.map((x) => Math.min(x.open, x.close)));
        if (up) { if (c > last.close) add = { open: last.close, close: c }; else if (c < loR) add = { open: last.open, close: c }; }
        else { if (c < last.close) add = { open: last.close, close: c }; else if (c > hiR) add = { open: last.open, close: c }; }
      }
      if (add) {
        add.t = b.time; add.high = Math.max(add.open, add.close); add.low = Math.min(add.open, add.close);
        add.up = add.close > add.open; add.volume = vol; vol = 0;
        out.push(add);
      }
    }
    return out;
  }

  // Kagi: the line keeps its direction until the close turns back by `rev` from the extreme. It turns thick
  // (yang, shown green) above the last shoulder and thin (yin, red) below the last waist.
  function kagi(bars, rev) {
    const cols = [];
    if (!(rev > 0) || !bars.length) return cols;
    let dir = 0, start = bars[0].close, ext = start, t0 = bars[0].time, tExt = t0;
    for (const b of bars) {
      const c = b.close;
      if (dir === 0) {
        if (c >= start + rev) { dir = 1; ext = c; tExt = b.time; } else if (c <= start - rev) { dir = -1; ext = c; tExt = b.time; }
        continue;
      }
      if (dir === 1) {
        if (c > ext) { ext = c; tExt = b.time; } else if (c <= ext - rev) { cols.push({ t: tExt, open: start, close: ext, dir: 1 }); start = ext; dir = -1; ext = c; tExt = b.time; }
      } else if (c < ext) { ext = c; tExt = b.time; } else if (c >= ext + rev) { cols.push({ t: tExt, open: start, close: ext, dir: -1 }); start = ext; dir = 1; ext = c; tExt = b.time; }
      if (cols.length >= MAX_ITEMS) break;
    }
    if (dir !== 0) cols.push({ t: tExt, open: start, close: ext, dir, forming: true });
    // thick (yang) after a rise above the last shoulder (top of the previous rising line), thin (yin) after a fall
    // below the last waist (bottom of the previous falling line)
    let yang = cols.length ? cols[0].dir === 1 : true, shoulder = null, waist = null;
    for (const k of cols) {
      if (k.dir === 1) { if (shoulder !== null && k.close > shoulder) yang = true; shoulder = k.close; }
      else { if (waist !== null && k.close < waist) yang = false; waist = k.close; }
      k.yang = yang;
      k.high = Math.max(k.open, k.close); k.low = Math.min(k.open, k.close);
    }
    return cols;
  }

  // Point & figure (close method): X columns rise and O columns fall in steps of `box`; a new column needs a
  // reversal of `reversal` boxes.
  function pointFigure(bars, box, reversal) {
    reversal = reversal || 3;
    const cols = [];
    if (!(box > 0) || !bars.length) return cols;
    const fl = (x) => +(Math.floor(x / box + 1e-9) * box).toPrecision(12);
    const ce = (x) => +(Math.ceil(x / box - 1e-9) * box).toPrecision(12);
    const anchor = fl(bars[0].close);
    let cur = null;
    for (const b of bars) {
      const c = b.close;
      if (!cur) {
        if (c >= anchor + box) cur = { t: b.time, x: true, low: anchor, high: fl(c) };
        else if (c <= anchor - box) cur = { t: b.time, x: false, high: anchor, low: ce(c) };
        continue;
      }
      if (cur.x) {
        if (c >= cur.high + box) cur.high = fl(c);
        else if (c <= cur.high - reversal * box) { cols.push(cur); cur = { t: b.time, x: false, high: +(cur.high - box).toPrecision(12), low: ce(c) }; }
      } else if (c <= cur.low - box) cur.low = ce(c);
      else if (c >= cur.low + reversal * box) { cols.push(cur); cur = { t: b.time, x: true, low: +(cur.low + box).toPrecision(12), high: fl(c) }; }
      if (cols.length >= MAX_ITEMS) break;
    }
    if (cur) { cur.forming = true; cols.push(cur); }
    for (const k of cols) { k.open = k.x ? k.low : k.high; k.close = k.x ? k.high : k.low; k.boxes = Math.round((k.high - k.low) / box) + 1; }
    return cols;
  }

  // ------------------------------------------------------------------ spread charts
  const OPS = { "/": (a, b) => (b ? a / b : NaN), "-": (a, b) => a - b, "+": (a, b) => a + b, "*": (a, b) => a * b };
  function spreadBars(a, b, op) {
    const f = OPS[op] || OPS["/"], other = new Map();
    for (const y of b) other.set(y.time, y);
    const out = [];
    for (const x of a) {
      const y = other.get(x.time);
      if (!y) continue;
      const o = f(x.open, y.open), h = f(x.high, y.high), l = f(x.low, y.low), c = f(x.close, y.close);
      if (![o, h, l, c].every(isFinite)) continue;
      out.push({ time: x.time, open: o, high: Math.max(o, h, l, c), low: Math.min(o, h, l, c), close: c, volume: 0 });
    }
    return out;
  }

  // ------------------------------------------------------------------ the chart's time axis for non-time charts
  // Range, Renko, Kagi, Line break, Point & figure and tick bars are drawn one after another: item i gets the
  // made-up time i + 1, and `real[i]` keeps when it formed for the axis labels and the crosshair.
  function indexed(items) {
    const real = new Array(items.length), data = new Array(items.length);
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      real[i] = it.t;
      data[i] = Object.assign({}, it, { time: i + 1 });
    }
    return { data, real };
  }

  // ------------------------------------------------------------------ export
  function csvCell(v) {
    if (v === null || v === undefined) return "";
    const s = String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }
  function toCSV(header, rows) {
    return [header].concat(rows).map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n";
  }
  const isoUTC = (sec) => new Date(sec * 1000).toISOString().replace(".000Z", "Z");

  const API = { UNIT_SEC, LIMITS, MONTH_STEPS, NATIVE, parseInterval, makeInterval, bucketStart, bucketEnd,
                TradeBars, barsFromTrades, isNewer, atr, niceStep, heikinAshi, rangeBars, renko, lineBreak, kagi,
                pointFigure, spreadBars, OPS, indexed, toCSV, isoUTC, MAX_ITEMS };
  if (typeof module === "object" && module.exports) module.exports = API;
  else root.PCC = API;
})(typeof window !== "undefined" ? window : this);
