// Twitch Stream Watchdog
//
// Normal mode: inspect the <video> every N minutes (N is fixed, or a random
// value in a min-max range). If it's paused / frozen / gone, switch to recovery
// mode: reload the tab every R seconds until playback resumes, then go back to
// normal mode. All timings are set on the extension's options page.

(function () {
  "use strict";

  const DEFAULTS = { checkMode: "fixed", fixedMin: 2, randMin: 2, randMax: 5, retrySecs: 30 };
  let opts = Object.assign({}, DEFAULTS);
  let lastTime = null;    // video.currentTime at the previous sample
  let triedPlay = false;  // did we already attempt to resume a paused video?
  let timer = null;

  const video = () => document.querySelector("video");

  function normalDelayMs() {
    if (opts.checkMode === "random") {
      let a = parseFloat(opts.randMin), b = parseFloat(opts.randMax);
      if (!(a > 0)) a = DEFAULTS.randMin;
      if (!(b > 0)) b = a;
      if (b < a) { const t = a; a = b; b = t; }
      return (a + Math.random() * (b - a)) * 60000;
    }
    let m = parseFloat(opts.fixedMin);
    if (!(m > 0)) m = DEFAULTS.fixedMin;
    return m * 60000;
  }

  function retryMs() {
    let s = parseFloat(opts.retrySecs);
    if (!(s > 0)) s = DEFAULTS.retrySecs;
    return s * 1000;
  }

  const getMode = () => { try { return sessionStorage.getItem("tsw_mode") || "normal"; } catch (e) { return "normal"; } };
  const setMode = (m) => { try { sessionStorage.setItem("tsw_mode", m); } catch (e) {} };

  function schedule(ms) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(tick, ms);
  }

  function reloadNow() {
    setMode("recovery");   // so the freshly-loaded page verifies on the fast cadence
    location.reload();
  }

  function tick() {
    const v = video();
    const mode = getMode();

    // Healthy = present, not paused, and currentTime advanced since last sample.
    const progressing = (v && !v.paused && lastTime !== null &&
                         Math.abs(v.currentTime - lastTime) >= 0.1);

    if (progressing) {
      triedPlay = false;
      lastTime = v.currentTime;
      if (mode !== "normal") setMode("normal");
      schedule(normalDelayMs());
      return;
    }

    // Playing but no baseline yet (e.g. right after load): record it, judge next.
    if (v && !v.paused && lastTime === null) {
      lastTime = v.currentTime;
      schedule(mode === "recovery" ? retryMs() : normalDelayMs());
      return;
    }

    // Paused: try to resume once before giving up (handles autoplay hiccups).
    if (v && v.paused && !triedPlay) {
      triedPlay = true;
      try { const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {}
      lastTime = v.currentTime;
      schedule(retryMs());
      return;
    }

    // Unhealthy (gone, still paused after a resume attempt, or frozen/black) ->
    // reload and keep checking on the fast retry cadence until it recovers.
    reloadNow();
  }

  function start() {
    lastTime = null;
    triedPlay = false;
    schedule(getMode() === "recovery" ? retryMs() : normalDelayMs());
  }

  function loadOpts(cb) {
    try {
      chrome.storage.local.get(
        { checkMode: null, fixedMin: null, randMin: DEFAULTS.randMin,
          randMax: DEFAULTS.randMax, retrySecs: DEFAULTS.retrySecs, checkMinutes: null },
        (r) => {
          if (r.checkMode === null && r.checkMinutes) {
            // migrate the old single-value setting
            opts.checkMode = "fixed";
            opts.fixedMin = r.checkMinutes;
          } else {
            opts.checkMode = r.checkMode || DEFAULTS.checkMode;
            opts.fixedMin = (r.fixedMin != null ? r.fixedMin : DEFAULTS.fixedMin);
          }
          opts.randMin = r.randMin;
          opts.randMax = r.randMax;
          opts.retrySecs = r.retrySecs;
          if (cb) cb();
        }
      );
    } catch (e) { if (cb) cb(); }
  }

  loadOpts(start);
  try {
    chrome.storage.onChanged.addListener((ch, area) => {
      if (area === "local") loadOpts();  // next schedule uses the new timings
    });
  } catch (e) {}
})();
