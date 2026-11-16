/* Heartbeat for the admin panel's "Currently online" (admin_tracking.py, POST /api/presence): while a logged-in
   member has a page open it says so every 45 seconds, and at once when the tab is hidden or shown again. No
   personal data is sent: only whether the tab is visible and the page path. Stops when the session has ended. */
(function () {
  "use strict";
  if (window.__sfmPresence) return;
  window.__sfmPresence = true;
  var EVERY = 45000, timer = null, stopped = false;
  function ping() {
    if (stopped) return;
    try {
      fetch("/api/presence", {
        method: "POST", credentials: "same-origin", keepalive: true, headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ visible: document.visibilityState !== "hidden", path: location.pathname })
      }).then(function (r) { if (r.status === 401) stop(); }).catch(function () { /* offline: try again next time */ });
    } catch (e) { /* very old browser */ }
  }
  function stop() { stopped = true; if (timer) clearInterval(timer); timer = null; }
  timer = setInterval(ping, EVERY);
  document.addEventListener("visibilitychange", ping);
})();
