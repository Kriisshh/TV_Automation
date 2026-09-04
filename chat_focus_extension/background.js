// Sets Twitch tabs to 75% zoom automatically when they finish loading.
// Edit ZOOM to change the level (1.0 = 100%).

const ZOOM = 0.75;

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete" && tab.url && tab.url.includes("twitch.tv")) {
    try {
      const p = chrome.tabs.setZoom(tabId, ZOOM);
      if (p && p.catch) p.catch(() => {});
    } catch (e) { /* ignore */ }
  }
});
