// Twitch Chat Autofocus
// Keeps the chat message box focused at all times. Event-driven (near-zero
// cost): the persistent mechanism is a single "focusout" listener that pulls
// focus back to chat. A short startup poll only waits for the input to appear
// (first load / SPA route change), then stops.

(function () {
  "use strict";

  // Twitch has changed its chat input markup over time; try a few selectors.
  const SELECTORS = [
    'div[data-a-target="chat-input"][contenteditable="true"]',
    'textarea[data-a-target="chat-input"]',
    '.chat-wysiwyg-input__editor',
    'div[role="textbox"][contenteditable="true"]',
  ];

  function findChat() {
    for (const sel of SELECTORS) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function focusChat() {
    const el = findChat();
    if (el && document.activeElement !== el) {
      try { el.focus(); } catch (e) { /* ignore */ }
    }
  }

  // Core: whenever focus leaves the chat box, pull it straight back.
  // Deferred to the next tick so the new focus target settles first.
  document.addEventListener("focusout", () => setTimeout(focusChat, 0), true);

  // Startup: the input may not exist yet (late load / channel switch).
  // Poll briefly until it shows up, then stop to stay lightweight.
  let tries = 0;
  const iv = setInterval(() => {
    if (findChat()) focusChat();
    if (++tries > 60) clearInterval(iv); // give up after ~30s
  }, 500);

  focusChat();
})();
