/* Signals FM: endless chart history for any lightweight-charts chart.
   When the user scrolls or zooms close to the oldest loaded candle, the next page of older candles is
   fetched from /candles?before=<oldest time> and added in front, keeping the view where it was.
   Used by the dashboard, Demo trading and the Auto-trade bot charts (the Live chart and the Pro terminal
   have the same logic built in).

   const h = SFMHistory.attach(chart, {
     coin: () => "BTC/USDT", tf: () => "1h",        // what the chart shows right now
     oldest: () => bars[0] && bars[0].time,          // oldest loaded candle time (unix seconds)
     prepend: (older) => { bars = older.concat(bars); series.setData(bars); },   // older = [{time, open, ...}]
     page: 500, max: 20000, threshold: 30,           // optional
   });
   h.reset();   // after switching coin/timeframe (forgets "no more history" for the old chart)
*/
(function () {
  "use strict";
  function attach(chart, o) {
    const page = o.page || 500, max = o.max || 20000, threshold = o.threshold == null ? 30 : o.threshold;
    const st = { loading: false, done: new Set(), gen: 0 };
    const key = () => `${o.coin()}|${o.tf()}`;
    async function check() {
      if (st.loading || !chart) return;
      const k = key();
      if (st.done.has(k)) return;
      let r;
      try { r = chart.timeScale().getVisibleLogicalRange(); } catch (e) { return; }
      if (!r || r.from > threshold) return;
      const oldest = o.oldest();
      if (oldest == null) return;
      if (o.count && o.count() >= max) { st.done.add(k); return; }
      st.loading = true;
      const gen = st.gen;
      try {
        const url = `/candles?coin=${encodeURIComponent(o.coin())}&timeframe=${encodeURIComponent(o.tf())}&limit=${page}&before=${oldest}`;
        const res = await fetch(url, { credentials: "same-origin" });
        const d = await res.json();
        if (gen !== st.gen || k !== key() || o.oldest() !== oldest) return;   // the chart changed meanwhile
        if (!res.ok || d.error) return;                                      // try again on the next scroll
        const older = (d.candles || []).filter((c) => c.time < oldest)
          .map((c) => ({ time: c.time, open: +c.open, high: +c.high, low: +c.low, close: +c.close, volume: +c.volume || 0 }));
        if (!older.length) { st.done.add(k); if (o.onEnd) o.onEnd(); return; }
        const saved = chart.timeScale().getVisibleLogicalRange();
        o.prepend(older);
        if (saved) chart.timeScale().setVisibleLogicalRange({ from: saved.from + older.length, to: saved.to + older.length });
      } catch (e) {
        /* network hiccup: the next scroll tries again */
      } finally {
        st.loading = false;
      }
    }
    try { chart.timeScale().subscribeVisibleLogicalRangeChange(() => { check(); }); } catch (e) { /* no chart */ }
    return {
      check,
      reset() { st.gen++; st.loading = false; st.done.clear(); },
    };
  }
  window.SFMHistory = { attach };
})();
