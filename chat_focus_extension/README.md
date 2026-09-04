# Twitch Stream Helper

A tiny Chrome extension for unattended Twitch streams. No network access, no
libraries. One content script set scoped to `twitch.tv`.

## Features
1. **Chat autofocus** — re-focuses the chat message box whenever it loses focus
   (event-driven `focusout` listener; a short startup poll waits for the input to
   appear, then idles).
2. **Auto-zoom** — sets Twitch tabs to 75% zoom on load (edit `ZOOM` in
   `background.js` to change). For a global default on *all* sites, set
   `chrome://settings` → Appearance → Page zoom → 75% instead.
3. **Stream watchdog** — two cadences:
   - **Normal:** every *N* minutes (fixed, or a random value in a min–max range)
     it checks the `<video>`.
   - **Recovery:** as soon as it finds the video paused (after a failed resume),
     frozen, black, errored, or gone, it reloads the tab every *R* seconds until
     playback resumes, then returns to the normal cadence.

## Options
On the extension's options page (`chrome://extensions` → this extension →
**Details** → **Extension options**) set:
- **Check interval** — fixed minutes, or a random min–max range.
- **Recovery reload interval** — seconds between reloads while recovering.

Defaults: check every 2 min, reload every 10 s. Settings are per profile.

## Install (per Chrome profile)
1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and select this `chat_focus_extension` folder.
4. Repeat for each profile you use.

## Limits
- True pixel-level black-screen detection isn't possible (CORS taints the video),
  but a frozen/black player is caught by the `currentTime` check.
- A *normal* ad can't be told apart from content; only a *frozen* ad is reloaded.
- Chat autofocus always pulls focus back to chat, so clicking video controls will
  immediately return focus to the chat box. That is by design.
