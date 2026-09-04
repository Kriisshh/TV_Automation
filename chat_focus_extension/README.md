# Twitch Stream Helper

A tiny Chrome extension for unattended Twitch streams. No network access, no
libraries. One content script set scoped to `twitch.tv`.

## Features
1. **Chat autofocus** — re-focuses the chat message box whenever it loses focus
   (event-driven `focusout` listener; a short startup poll waits for the input to
   appear, then idles).
2. **Stream watchdog** — every *N* minutes it checks the `<video>`:
   - **Paused** → tries to resume; if still paused at the next check, reloads.
   - **Frozen / black / stuck scene or ad** → if `currentTime` hasn't advanced
     between checks, reloads.
   - **No player / broken page** → reloads.
   A reload-loop guard prevents reloading more than once per interval.

## Options
Set the check interval on the extension's options page
(`chrome://extensions` → this extension → **Details** → **Extension options**).
Default is **2 minutes**.

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
