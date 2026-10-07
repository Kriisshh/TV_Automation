# Stream Suite — Handoff / Transfer Guide

A Windows desktop automation app (Python + tkinter, shipped as a single
PyInstaller `.exe`) plus a companion Chrome extension. It opens Chrome profiles,
navigates and sends keystrokes, types stream-chat messages on hotkeys, and
records/replays mouse macros. It **self-updates from GitHub Releases** on every
launch.

- **Repo:** https://github.com/Kriisshh/TV_Automation  (must stay **public** — see Auto-update)
- **Current version:** see `__version__` in `updater.py` (e.g. 1.3.0)
- **Platform:** Windows only. GUI is tkinter. Run **as administrator** for the
  most reliable global hotkeys + keystroke sending.
- **Built exe name:** `ChromeSequencer.exe` (unchanged across versions — the
  updater and CI depend on this constant name).

---

## Repo layout
```
main.py                         # The whole GUI app (4 tabs) + automation logic
updater.py                      # Self-update via GitHub Releases; holds __version__
requirements.txt                # keyboard, pywin32, pynput, pyinstaller
build.bat                       # optional local one-off build
.github/workflows/release.yml   # CI: builds .exe + publishes a Release on tag push
chat_focus_extension/           # Companion Chrome extension (unpacked)
  manifest.json                 #   MV3 manifest
  content.js                    #   chat autofocus
  watchdog.js                   #   stream health watchdog + auto-unmute
  background.js                 #   auto-zoom Twitch tabs
  options.html / options.js     #   per-profile settings UI
  README.md
.gitignore                      # ignores runtime config + build artifacts
HANDOFF.md                      # this file

# Runtime-generated next to the .exe (NOT committed — in .gitignore):
config.json                     # Chrome Sequencer + Application + Macro settings
stream_groups.json              # Typer groups
macro_recording.json            # Macro recordings
```

---

## The app: 4 tabs (detached rounded pill tabs, Apple/teal theme)

### Tab 1 — Chrome Sequencer
Opens Chrome profiles and drives them. Flow (per selected profile, **one at a
time**): open a window → wait (extensions/VPN) → navigate to the next URL
(assigned **sequentially**, wrapping if fewer URLs than profiles) → wait → send
per-profile keys (`m`/`c`) → delay before next profile. Then optional final key
presses. When finished it **stays open and switches to the Typer tab**.
- **URLs:** multi-line box, one URL per line (sequential assignment).
- **Chrome path**, profile checkboxes (two columns, name only), **Tile windows
  in a grid** (near-square auto grid across the primary monitor), **Load helper
  extension** (`--load-extension`, off by recommendation — install per profile
  instead), per-tab keys (`m`/`c`) + delay, four delay cards (fixed/random),
  collapsible **Final key presses** (click-to-record keys), Start/Abort, global **Save**.
- **Abort / Start hotkey** is configured on the Application tab; Abort stops the
  sequence only (does not quit the app).

### Tab 2 — Typer
Stream-chat message sequencer (adapted from TyperV9). Profile "groups", each with
its own global hotkey, switch key, loop settings, and ordered message steps
(each step: message, delay range, multi-line Sequential/Random). "Show Active
Macros" monitor window. Import/Export groups via the ☰ menu. Stored in
`stream_groups.json`.

### Tab 3 — Macros
Record the mouse and replay it.
- **Multiple named recordings**, each with its own **play hotkey** and an **On**
  checkbox (for play-all).
- **One record hotkey** (default `Ctrl+Shift+R`) records into the **selected**
  recording (radio).
- **Capture:** Movement / Clicks / Scroll (toggles).
- **Playback:** Once / Repeat N / Loop-until-stopped.
- **Play all** (default `Ctrl+Shift+G`): plays every checked recording in order
  with a **fixed/random delay after each macro**; press again to stop.
- Needs **pynput**. Coordinates replay in absolute screen pixels (best on the
  same resolution they were recorded on). Stored in `macro_recording.json`.

### Tab 4 — Application
App-wide settings:
- **Startup:** launch on Windows start (HKCU Run key), auto-run the sequence on launch.
- **Start/Abort hotkey** (default `Ctrl+Shift+Q`) with optional "also start" toggle.
- **Updates:** version display + manual "Check for updates".
- **Configuration:** Export/Import `config.json`.
- **Play together (one hotkey):** one keybind fires a **checked Typer group +
  the Macro** together; at most one Typer group checkable, Macro free. Press
  again to stop both.

### Typer ↔ Macro cooperation
They share a single input lock. The Typer holds it only while sending a message;
the Macro holds it only while playing a recording. During each other's
delay/idle phases the other runs — so they **interleave** (never driving input
at the same instant) instead of one fully pausing the other.

---

## The Chrome extension (`chat_focus_extension/`)
Install per profile: `chrome://extensions` → Developer mode → **Load unpacked** →
select the `chat_focus_extension` folder. Repeat per profile (unpacked
extensions load from that folder on disk, so **keep the folder in a permanent
location**). Features:
1. **Chat autofocus** — re-focuses the chat box whenever it loses focus.
2. **Auto-zoom** — sets Twitch tabs to a user-defined zoom (default 75%).
3. **Watchdog** — normal check every *N* min (fixed or random range); if the
   video is paused/frozen/black/errored/gone, enters recovery and **reloads
   every *R* seconds** until it recovers (defaults: 2 min / 10 s).
4. **Auto-unmute** — every 3 s, if the player is muted, unmute and set volume 50%.
All timings/zoom are set on the extension's **options page**, per profile.

---

## Settings persistence
All three JSON files live **next to the `.exe`** (or next to `main.py` in dev)
and survive updates because they sit beside the swapped exe. They are
`.gitignore`d. `config.json` holds Chrome Sequencer + Application + Macro
settings; `stream_groups.json` holds Typer groups; `macro_recording.json` holds
mouse recordings.

---

## Auto-update (updater.py) — IMPORTANT
- `__version__` in `updater.py` is the source of truth for the running build.
- On launch, `main()` calls `run_update_flow()`: `check_for_update()` hits
  `releases/latest`, compares `tag_name` to `__version__` (numeric, v-prefix
  tolerant, arbitrary segment count). If newer, `download_and_apply()` downloads
  `ChromeSequencer.exe` to `<exe>.new`, writes a temp `.bat` that waits for the
  PID to exit, swaps `<exe>.new` → `<exe>` (retries up to 15×, logs to
  `%TEMP%\chromeseq_update.log`), relaunches via `explorer.exe`, and the app exits.
- Config constants: `GITHUB_OWNER="Kriisshh"`, `GITHUB_REPO="TV_Automation"`,
  `ASSET_NAME="ChromeSequencer.exe"`.
- Dormant/safe when: not frozen (running the `.py` never swaps), offline, or
  owner placeholder. Uses only stdlib.

### Two hard invariants (learned the hard way)
1. **The repo MUST stay public.** The shipped exe calls `releases/latest`
   *unauthenticated*; a private repo returns 404 and the updater silently
   no-ops (all exceptions are swallowed by design).
2. **Updater/relaunch-mechanism fixes take effect one release later.** The swap
   `.bat` is generated by the **currently-running** copy's code, so a fix to the
   update/relaunch logic only proves out on the release *after* the one that
   contains it. Budget two release hops when changing updater internals.

---

## Release process (CI builds the exe)
`.github/workflows/release.yml` triggers on pushing a `v*` tag. It runs on
`windows-latest`, Python **3.12**, installs `requirements.txt`, runs
`python -m PyInstaller --onefile --noconsole --name ChromeSequencer main.py`,
then `softprops/action-gh-release@v2` publishes the Release with the `.exe`.

To ship an update (tag MUST equal `__version__`):
```bash
# 1. edit __version__ in updater.py to e.g. 1.3.1
# 2:
git commit -am "v1.3.1"
git tag v1.3.1
git push origin main v1.3.1
# CI builds + publishes. Installed copies auto-update on next launch.
```
Watch the build: `gh run watch <run-id> --exit-status`. The exe name/asset name
must stay `ChromeSequencer.exe`.

---

## Local dev quickstart
```bash
pip install -r requirements.txt    # keyboard, pywin32, pynput, pyinstaller
python main.py                     # runs the GUI; updater stays dormant (not frozen)
build.bat                          # optional local one-off build -> dist\ChromeSequencer.exe
```
Notes:
- If **pynput** is missing, the Macros tab shows a "missing dependency" note.
- Headless sanity check (no window): construct `main.CombinedApp(tk.Tk())` with
  the root withdrawn; the repo history has example smoke tests.

---

## Transferring to another PC
1. **Clone** the repo: `git clone https://github.com/Kriisshh/TV_Automation`.
2. Ensure Git + `gh` (authenticated) are set up if you'll cut releases from the
   new PC, and the GitHub account can push to `Kriisshh/TV_Automation`.
3. For **using** the app: either `pip install -r requirements.txt && python main.py`,
   or download `ChromeSequencer.exe` from the latest Release and run it (it will
   self-update thereafter). Put the exe in a stable folder; its 3 JSON configs
   are created beside it.
4. For the **extension**: copy the `chat_focus_extension/` folder to a permanent
   location and Load unpacked in each Chrome profile. Set options per profile.
5. Keep the repo **public** or the auto-updater will stop working.

---

## Known caveats / constraints
- **Unsigned `.exe`** → SmartScreen/AV may warn; allow once per machine.
- Run **as admin** for hotkey + keystroke reliability.
- Window tiling uses the primary monitor's work area; a near-square grid can make
  cells narrow with many profiles.
- `c` key = focus Twitch chat (needs **BetterTwitchControls**); `m` = native mute.
- Macro replay uses absolute pixel coordinates — recorded and replayed best on
  the same screen/resolution.
- Chrome ignores `--load-extension` if that profile's Chrome is already running;
  installing the extension per profile is the reliable path.

## PyInstaller note (pynput)
If Macro replay ever does nothing in the packaged exe (PyInstaller sometimes
misses pynput's Windows backend), add a hidden-import for the backend in the
workflow's PyInstaller command and re-release.
