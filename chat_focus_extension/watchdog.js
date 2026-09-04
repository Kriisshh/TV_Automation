// Twitch Stream Watchdog
// Every N minutes it inspects the <video>. If it's paused it first tries to
// resume; if still paused at the next check it reloads. If currentTime has not
// advanced between checks (frozen / black / stuck scene or ad), it reloads.
// Interval N is set on the extension's options page (default 2 minutes).

(function () {
  "use strict";

  const DEFAULT_MIN = 2;
  let checkMs = DEFAULT_MIN * 60000;
  let lastTime = null;    // video.currentTime at the previous check
  let sawPaused = false;  // was it paused at the previous check?
  let timer = null;

  function video() {
    return document.querySelector("video");
  }

  function doReload() {
    // Guard against reload loops: never reload twice within one interval.
    try {
      const now = Date.now();
      const last = parseInt(sessionStorage.getItem("tsw_last_reload") || "0", 10);
      if (now - last < checkMs) return;
      sessionStorage.setItem("tsw_last_reload", String(now));
    } catch (e) { /* ignore */ }
    location.reload();
  }

  function tick() {
    const v = video();

    // No player element at all -> page is broken / not loaded -> reload.
    if (!v) {
      doReload();
      return;
    }

    // Paused: try to resume once; reload if it's still paused next time.
    if (v.paused) {
      if (sawPaused) {
        doReload();
        return;
      }
      sawPaused = true;
      try { const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {}
      lastTime = v.currentTime;
      return;
    }
    sawPaused = false;

    // Playing but currentTime hasn't moved since last check -> frozen/stuck.
    // (A different value — even a reset after an ad — counts as activity.)
    if (lastTime !== null && Math.abs(v.currentTime - lastTime) < 0.1) {
      doReload();
      return;
    }
    lastTime = v.currentTime;
  }

  function start() {
    if (timer) clearInterval(timer);
    lastTime = null;
    sawPaused = false;
    timer = setInterval(tick, checkMs);
  }

  try {
    chrome.storage.local.get({ checkMinutes: DEFAULT_MIN }, (r) => {
      const m = parseFloat(r.checkMinutes);
      if (m && m > 0) checkMs = m * 60000;
      start();
    });
    chrome.storage.onChanged.addListener((ch, area) => {
      if (area === "local" && ch.checkMinutes) {
        const m = parseFloat(ch.checkMinutes.newValue);
        if (m && m > 0) { checkMs = m * 60000; start(); }
      }
    });
  } catch (e) {
    start();
  }
})();
