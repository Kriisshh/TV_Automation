# Auto-updates via GitHub Releases

The app checks GitHub for a newer version **every launch**. If one exists it
downloads the new `.exe`, swaps itself, and relaunches. `config.json` sits next
to the `.exe`, so all your saved settings survive updates.

You publish an update once; every device picks it up on its next launch. After
the first manual install on a device, you never copy an `.exe` by hand again.

---

## One-time setup

1. Create a **free GitHub account** and a **new repository** (Public is simplest;
   Private also works for update checks).
2. Open `updater.py` and set the three values at the top:
   ```python
   GITHUB_OWNER = "your-github-username"
   GITHUB_REPO  = "ChromeSequencer"     # match your repo name
   ASSET_NAME   = "ChromeSequencer.exe" # keep this name identical every release
   ```
   Leave `ASSET_NAME` as-is unless you rename the build output.

---

## Publishing a new version (repeat for every update)

1. Bump the version in `updater.py`:
   ```python
   __version__ = "1.1.0"
   ```
2. Rebuild the `.exe`:
   ```
   build.bat
   ```
   (produces `dist\ChromeSequencer.exe`)
3. On GitHub: **Releases -> Draft a new release**.
   - **Tag**: `v1.1.0`  (must match `__version__`; the leading `v` is fine)
   - **Attach** `dist\ChromeSequencer.exe` as a release asset — the file name
     must equal `ASSET_NAME` (`ChromeSequencer.exe`).
   - **Publish**.

That's it. The next time any installed copy launches, it sees `v1.1.0` is newer,
updates itself, and restarts.

---

## Version rules

- Compared as numbers: `1.10.0` is newer than `1.9.0`.
- The app only updates **upward**. Re-publishing the same or an older tag does
  nothing.
- The very first `.exe` you hand out should be built from a `__version__` that
  is **older than or equal to** your first release tag, so it updates on launch.

## Notes / troubleshooting

- **Offline or GitHub unreachable?** The check fails silently and the app just
  runs normally.
- **Rate limit**: unauthenticated GitHub API allows ~60 checks/hour per IP —
  far more than launch checks need.
- **Nothing happens on update**: confirm the release tag is newer than the
  running version, and that the attached asset is named exactly
  `ChromeSequencer.exe`.
- **Antivirus / SmartScreen** may warn on an unsigned downloaded `.exe`. Code
  signing removes this but costs money; otherwise allow it once per machine.
- Until you set `GITHUB_OWNER`, the updater stays dormant and never blocks the app.
