// Sets Twitch tabs to a user-defined zoom automatically when they finish
// loading. The zoom percent is stored in chrome.storage.local as "zoomPct"
// (default 75).

const DEFAULT_ZOOM = 75;

function applyZoom(tabId) {
  chrome.storage.local.get({ zoomPct: DEFAULT_ZOOM }, (r) => {
    let z = parseFloat(r.zoomPct);
    if (!(z > 0)) z = DEFAULT_ZOOM;
    // Chrome allows ~25%–500%. Clamp for safety.
    z = Math.max(25, Math.min(500, z)) / 100;
    try {
      const p = chrome.tabs.setZoom(tabId, z);
      if (p && p.catch) p.catch(() => {});
    } catch (e) { /* ignore */ }
  });
}

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete" && tab.url && tab.url.includes("twitch.tv")) {
    applyZoom(tabId);
  }
});

// If the user changes the zoom setting, re-apply it to any open Twitch tabs
// immediately (no reload needed).
chrome.storage.onChanged.addListener((ch, area) => {
  if (area !== "local" || !ch.zoomPct) return;
  try {
    chrome.tabs.query({ url: "*://*.twitch.tv/*" }, (tabs) => {
      for (const t of tabs) applyZoom(t.id);
    });
  } catch (e) { /* ignore */ }
});
