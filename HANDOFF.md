# ChromeSequencer — Handoff Brief

## What this is
A Windows-only desktop automation app (Python + tkinter, shipped as a single
PyInstaller `.exe`). It opens Chrome profiles, navigates them to a URL, sends
keystrokes per tab, launches an `.exe`, then closes itself. It self-updates from
GitHub Releases on every launch.

- **Repo:** https://github.com/Kriisshh/TV_Automation
- **Current release:** v1.0.0 (built + published by GitHub Actions)
- **Platform:** Windows only. GUI is tkinter. Runtime needs the app run **as
  administrator** for global hotkey + sending keystrokes to reliably work.

## Repo layout
```
main.py                        # GUI + automation sequence (entry point)
updater.py                     # self-update via GitHub Releases; holds __version__
build.bat                      # local build (optional; CI is the real path)
requirements.txt               # keyboard, pywin32, pyinstaller
UPDATES_README.md              # release/how-to notes
.github/workflows/release.yml  # CI: builds .exe + publishes release on tag push
config.json                    # RUNTIME-GENERATED next to the .exe; not committed
```

## How the app works (main.py)
Sequence run in a worker thread (`App._run_sequence`):
1. Initial wait.
2. Open one Chrome window per selected profile (`--profile-directory=<dir>
   --new-window about:blank`). Each new window's HWND is captured by diffing
   `chrome_window_handles()` before/after launch.
3. Wait for extensions / VPN (timed — see caveats).
4. Focus each window, `Ctrl+L`, type URL, Enter (keeps exactly 1 tab/profile).
5. Wait.
6. Per window: wait, focus, send enabled keys — `m` and/or `c` — with a fixed
   "delay between keys" in between.
7. Launch target `.exe` via `os.startfile`, then `os._exit(0)`.

Other pieces:
- **Profiles** are read from Chrome's `Local State` (`profile.info_cache`), with a
  folder-scan fallback. Shown as checkboxes.
- **Delays**: reusable `DelayInput` widget, each supports Fixed or Random(min–max).
  Four instances: initial, ext/VPN, after-URL, per-tab.
- **Abort hotkey**: global via `keyboard.add_hotkey` (default `ctrl+shift+q`);
  editable in GUI. Firing it saves settings and `os._exit(0)`.
- **Window focus**: `force_foreground()` uses `AttachThreadInput` +
  `SetForegroundWindow` to beat Windows' foreground lock.
- **Interruptible sleep**: `App._sleep` polls `stop_event` so aborts are responsive.

## Settings persistence
`config.json` is written next to the executable (`app_dir()` handles frozen vs
script). Saved/loaded keys: `url`, `chrome`, `exe`, `hotkey`, `send_m`, `send_c`,
`key_delay`, `delays{initial,ext,afterurl,pertab}` (each `{mode,fixed,min,max}`),
`profiles` (list of selected profile directories). Saved on Start, Abort, hotkey,
and window close. Survives updates because it sits beside the swapped `.exe`.

## Auto-update (updater.py)
- `__version__` is the source of truth for the running build.
- On launch, `main()` withdraws the root, calls `run_update_flow()`:
  `check_for_update()` hits `releases/latest`, compares `tag_name` to
  `__version__` (numeric, v-prefix tolerant). If newer, `download_and_apply()`
  downloads `ASSET_NAME` to `<exe>.new`, writes a temp `.bat` that waits for the
  PID to exit, swaps `<exe>.new` -> `<exe>`, and relaunches. App then exits.
- Config constants (already set): `GITHUB_OWNER="Kriisshh"`,
  `GITHUB_REPO="TV_Automation"`, `ASSET_NAME="ChromeSequencer.exe"`.
- Dormant/safe when: not frozen (dev `.py` never swaps), offline, or owner still
  placeholder. Uses only stdlib (`urllib`).

## Release process (CI does the build)
`.github/workflows/release.yml` triggers on pushing a `v*` tag. It runs on
`windows-latest`, Python **3.12** (avoids PyInstaller lag on newer Python),
installs `requirements.txt`, runs
`python -m PyInstaller --onefile --noconsole --name ChromeSequencer main.py`,
then `softprops/action-gh-release@v2` publishes the release with the `.exe`.

To ship an update:
```
# edit __version__ in updater.py to e.g. 1.1.0
git commit -am "v1.1.0"
git tag v1.1.0
git push origin main v1.1.0
```
**Invariant:** the pushed tag must equal `__version__` (tag `v1.1.0` ↔
`__version__="1.1.0"`), or installed copies won't see the build as newer.

## Known caveats / constraints
- **VPN "connected" is a timed wait, not a real check.** No reliable signal that
  a VPN extension has connected.
- **Keystrokes hit whatever window is focused.** `force_foreground` mitigates but
  slow machines can misfire; larger delays help.
- **`c` key** = focus Twitch chat, and only works with the **BetterTwitchControls**
  extension installed (not BTTV). `m` = Twitch native mute. `c` only lands in
  chat when not already typing in an input.
- **Unsigned `.exe`** → SmartScreen/AV may warn; allow once per machine, or add
  code signing (paid).
- **First install per device is manual** (one copy); updates after are automatic.
- Run as admin for hotkey + keystroke reliability.

## Suggested next tasks (not yet done)
- Optional "You're on the latest version ✓" indicator and a manual
  "Check for updates" button in the GUI.
- Per-profile URL support, or multiple tabs per profile.
- Make the sent keys fully configurable (arbitrary key list), not just m/c.
- Retry/verify navigation succeeded (e.g., re-focus if a window didn't accept keys).
- Optional logging to a file for debugging unattended runs.
- Code signing to remove SmartScreen warnings.

## Local dev quickstart
```
pip install -r requirements.txt
python main.py            # runs GUI; updater stays dormant when not frozen
build.bat                 # optional local one-off build -> dist\ChromeSequencer.exe
```
