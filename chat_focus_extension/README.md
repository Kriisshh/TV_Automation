# Twitch Chat Autofocus

A tiny Chrome extension that keeps the Twitch chat message box focused at all
times. It has no popup, no options, no network access, and no libraries — just
one content script scoped to `twitch.tv`.

## How it works
- A single `focusout` listener re-focuses the chat input whenever focus leaves it.
- A short startup poll (max ~30s) waits for the chat input to appear on first
  load or channel switch, then stops. After that it is purely event-driven.

## Install (per Chrome profile)
1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and select this `chat_focus_extension` folder.
4. Repeat for each profile you use.

## Note
Because it always pulls focus back to chat, clicking elsewhere on the page
(e.g. video controls) will immediately return focus to the chat box. That is by
design. Remove/disable the extension if you need to interact with other controls.
