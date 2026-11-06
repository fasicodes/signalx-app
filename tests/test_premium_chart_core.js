/* Tests for static/premium-chart-core.js (the Premium chart workspace's pure functions). No browser needed:
       node tests/test_premium_chart_core.js
   tests/test_premium_chart.py also runs it and checks that the interval buckets match premium_chart.py. */
"use strict";
const path = require("path");
const P = require(path.join(__dirname, "..", "static", "premium-chart-core.js"));

let passed = 0;
function check(cond, label) {
  if (!cond) throw new Error("FAIL: " + label);
  passed += 1;
  console.log("  ok -", label);
}
const utc = (y, mo, d, h = 0, mi = 0, s = 0) => Date.UTC(y, mo - 1, d, h, mi, s) / 1000;
const close = (a, b, eps = 1e-9) => Math.abs(a - b) <= eps;

if (process.argv[2] === "--buckets") {        // used by the Python test: print bucket starts / ends as JSON
  const cases = JSON.parse(process.argv[3]);
  const out = cases.map(([ts, raw]) => { const iv = P.parseInterval(raw); const b = P.bucketStart(ts, iv); return [b, P.bucketEnd(b, iv)]; });
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}

console.log("\n[1] intervals");
const want = { "7m": ["7m", "time", null], "60m": ["60m", "time", "1h"], "1d": ["1D", "time", "1d"], "7D": ["7D", "time", null],
               "2 weeks": ["2W", "time", null], "1M": ["1M", "month", "1M"], "6M": ["6M", "month", null],
               "10s": ["10s", "second", null], "100T": ["100T", "tick", null], "15 min": ["15m", "time", "15m"] };
for (const raw in want) {
  const iv = P.parseInterval(raw);
  check(iv && iv.name === want[raw][0] && iv.kind === want[raw][1] && iv.native === want[raw][2], `${raw} -> ${want[raw]}`);
}
for (const bad of ["", "x", "0m", "1441m", "5M", "13M", "301s", "10001T", "2y", "3.5h", null]) check(P.parseInterval(bad) === null, `${bad} rejected`);
check(P.parseInterval("1m").name === "1m" && P.parseInterval("1M").name === "1M", "m is a minute, M a month");
check(P.parseInterval("100T").long === "100 ticks" && P.parseInterval("1h").long === "1 hour", "long labels");

console.log("\n[2] buckets");
const m7 = P.parseInterval("7m");
check(P.bucketStart(utc(2026, 11, 6, 23, 58), m7) === utc(2026, 11, 6, 23, 55) && P.bucketEnd(utc(2026, 11, 6, 23, 55), m7) === utc(2026, 11, 7), "7m: last bar of the day ends at midnight");
const s7 = P.parseInterval("7s");
check(P.bucketStart(utc(2026, 11, 6, 0, 0, 15), s7) === utc(2026, 11, 6, 0, 0, 14), "7s buckets");
const w2 = P.parseInterval("2W"), wb = P.bucketStart(utc(2026, 11, 6, 12), w2);
check(new Date(wb * 1000).getUTCDay() === 1 && P.bucketEnd(wb, w2) - wb === 14 * 86400, "2W starts on a Monday");
check(P.bucketStart(utc(2026, 11, 6), P.parseInterval("6M")) === utc(2026, 7, 1) && P.bucketEnd(utc(2026, 7, 1), P.parseInterval("6M")) === utc(2027, 1, 1), "6M");
check(P.bucketEnd(utc(2026, 12, 1), P.parseInterval("1M")) === utc(2027, 1, 1), "month end rolls the year");

console.log("\n[3] second and tick bars from trades");
const T0 = utc(2026, 11, 6, 12) * 1000;
const trades = [];
for (let i = 0; i < 100; i++) trades.push([T0 + i * 370, 100 + (i % 7) * 0.5 - (i % 3) * 0.25, 0.1, i % 2 ? 1 : -1, String(5000 + i)]);
const s5 = P.barsFromTrades(trades, P.parseInterval("5s"));
check(s5.length === Math.ceil((99 * 370) / 5000) || s5.length === Math.floor((99 * 370) / 5000) + 1, "5s bars cover the trades");
check(s5.every((b) => b.t % 5 === 0) && s5[0].t === T0 / 1000, "5s bars start on 5-second marks");
const inFirst = trades.filter((t) => t[0] < T0 + 5000);
check(s5[0].open === inFirst[0][1] && s5[0].close === inFirst[inFirst.length - 1][1] && s5[0].count === inFirst.length
      && s5[0].high === Math.max(...inFirst.map((t) => t[1])) && close(s5[0].volume, inFirst.length * 0.1), "first 5s bar OHLCV");
const t10 = P.barsFromTrades(trades, P.parseInterval("10T"));
check(t10.length === 10 && t10.every((b) => b.count === 10), "10-tick bars: 10 trades each");
const tb = new P.TradeBars(P.parseInterval("10T"));
tb.add(trades.slice(0, 55));
check(tb.add(trades.slice(40, 60)) === 5 && tb.bars.length === 6 && tb.bars[5].count === 10, "live: only newer trades are added");
check(tb.add([[T0 + 99 * 370, 100, 1, 1, "5099"], [T0 + 99 * 370, 100, 1, 1, "5099"]]) === 1, "a repeated trade counts once");

console.log("\n[4] price-based charts");
const flat = (closes, t0 = 1000) => closes.map((c, i) => ({ time: t0 + i * 60, open: c, high: c, low: c, close: c, volume: 1 }));
let rk = P.renko(flat([100, 101, 102.5, 103.2, 101.9, 100.9, 99.5]), 1);
check(rk.map((b) => (b.up ? "u" : "d")).join("") === "uuudd" && rk[0].open === 100 && rk[2].close === 103, "Renko: up bricks, a reversal needs two boxes");
check(rk[3].open === 102 && rk[3].close === 101 && rk.every((b) => close(b.high - b.low, 1)), "Renko: reversal brick starts at the last brick's bottom");
rk = P.renko(flat([100, 100.5, 99.7]), 1);
check(rk.length === 0, "Renko: moves smaller than a box make no brick");

const rb = P.rangeBars([{ time: 1, open: 100, high: 112, low: 99, close: 111, volume: 5 }, { time: 2, open: 111, high: 111.5, low: 104, close: 105, volume: 3 }], 5);
check(rb.slice(0, -1).every((b) => close(b.high - b.low, 5)), "range bars span exactly the range size");
check(rb.every((b, i) => i === 0 || close(b.open, rb[i - 1].close)), "each range bar opens where the last one closed");
check(rb[rb.length - 1].forming && rb[rb.length - 1].high - rb[rb.length - 1].low <= 5 + 1e-9, "the last range bar is still forming");
check(P.rangeBars(flat([100, 200]), 0.000001).capped === true, "a tiny range size is capped instead of freezing the page");

const lb = P.lineBreak(flat([10, 11, 12, 13, 12.5, 11.5, 9.5, 9]), 3);
check(lb.map((b) => (b.up ? "u" : "d")).join("") === "uuudd" && lb[3].open === 12 && lb[3].close === 9.5, "three-line break: reversal past three lines");

const kg = P.kagi(flat([100, 103, 106, 104, 101, 102, 108, 107, 99]), 3);
check(kg.map((k) => k.dir).join(",") === "1,-1,1,-1" && kg[0].close === 106 && kg[1].close === 101 && kg[2].close === 108, "Kagi: turns after a 3-point reversal");
check(kg[0].yang === true && kg[2].yang === true && kg[3].forming && kg[3].close === 99, "Kagi: yang above the shoulder, last line still forming");
check(kg[3].yang === false, "Kagi: yin below the waist");

const pf = P.pointFigure(flat([100, 102, 104.2, 103, 100.9, 99.5, 101, 103.4]), 1, 3);
check(pf[0].x && pf[0].low === 100 && pf[0].high === 104, "P&F: X column up to 104");
check(!pf[1].x && pf[1].high === 103 && pf[1].low === 100 && pf[1].boxes === 4, "P&F: O column after a 3-box reversal");
check(pf[2].x && pf[2].low === 101 && pf[2].high === 103 && pf[2].forming, "P&F: new X column");

const ha = P.heikinAshi([{ time: 1, open: 10, high: 12, low: 9, close: 11 }, { time: 2, open: 11, high: 13, low: 10, close: 12 }]);
check(close(ha[0].close, 10.5) && close(ha[1].open, (ha[0].open + ha[0].close) / 2), "Heikin Ashi");

console.log("\n[5] spread charts, helpers, export");
const A = [{ time: 1, open: 100, high: 110, low: 95, close: 105 }, { time: 2, open: 105, high: 106, low: 100, close: 102 }, { time: 3, open: 1, high: 1, low: 1, close: 1 }];
const B = [{ time: 1, open: 50, high: 52, low: 49, close: 50 }, { time: 2, open: 51, high: 51, low: 50, close: 51 }];
const sp = P.spreadBars(A, B, "/");
check(sp.length === 2 && close(sp[0].open, 2) && close(sp[0].close, 2.1) && sp[0].high >= sp[0].close && sp[0].low <= sp[0].open, "A / B, bars only where both have a candle");
check(close(P.spreadBars(A, B, "-")[1].close, 51) && close(P.spreadBars(A, B, "*")[0].open, 5000), "A - B and A * B");
check(P.spreadBars(A, [{ time: 1, open: 0, high: 0, low: 0, close: 0 }], "/").length === 0, "division by zero dropped");
check(P.niceStep(37) === 50 && P.niceStep(0.00123) === 0.001 && P.niceStep(2.4) === 2.5 && P.niceStep(0) === null, "nice box sizes");
check(close(P.atr(flat([1, 1, 1])), 0), "ATR of flat candles is 0");
const ix = P.indexed([{ t: 50, open: 1 }, { t: 50, open: 2 }, { t: 70, open: 3 }]);
check(ix.data.map((d) => d.time).join() === "1,2,3" && ix.real.join() === "50,50,70", "non-time charts get one slot per item, real times kept");
const csv = P.toCSV(["time", "note"], [["2026-11-06T00:00:00Z", 'a "quoted", text']]);
check(csv === 'time,note\r\n2026-11-06T00:00:00Z,"a ""quoted"", text"\r\n', "CSV quoting");
check(P.isoUTC(utc(2026, 11, 6, 12)) === "2026-11-06T12:00:00Z", "ISO time in UTC");

console.log(`\nALL ${passed} CHECKS PASSED`);
