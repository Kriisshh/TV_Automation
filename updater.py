"""
Self-update for ChromeSequencer via GitHub Releases.

How it works:
  - On launch the app calls check_for_update(). It reads the *latest* GitHub
    release for your repo and compares its tag (e.g. "v1.2.0") to __version__.
  - If the release is newer, download_and_apply() downloads the new .exe next
    to the running one, launches a tiny swap script, and the app exits so the
    script can replace the old .exe and relaunch it.
  - config.json lives next to the .exe, so all saved settings survive updates.

SET THESE THREE before your first release, then rebuild:
    GITHUB_OWNER  -> your GitHub username
    GITHUB_REPO   -> your repo name
    ASSET_NAME    -> the file name you attach to each release (keep it constant)

Only the built .exe self-updates. Running the .py directly never swaps itself.
Uses only the Python standard library (no extra deps).
"""

import os
import sys
import json
import tempfile
import subprocess
import urllib.request

# ---- bump this each release; must match the release tag (v-prefix optional) ----
__version__ = "1.0.2"

# ---- configure these ----
GITHUB_OWNER = "Kriisshh"
GITHUB_REPO = "TV_Automation"
ASSET_NAME = "ChromeSequencer.exe"

API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def _parse_version(v):
    v = (v or "").strip().lstrip("vV")
    parts = []
    for chunk in v.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _is_newer(latest, current):
    a, b = _parse_version(latest), _parse_version(current)
    # pad the shorter tuple with zeros so e.g. (1,0) vs (1,0,0) compares equal
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def _get_latest_release():
    req = urllib.request.Request(API_URL, headers={
        "User-Agent": "ChromeSequencer-Updater",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def _find_asset_url(release):
    for a in release.get("assets", []):
        if a.get("name") == ASSET_NAME:
            return a.get("browser_download_url")
    return None


def check_for_update():
    """Return (latest_tag, download_url) if a newer release exists, else None."""
    if GITHUB_OWNER.startswith("YOUR_GITHUB"):
        return None  # not configured yet -> never blocks the app
    try:
        rel = _get_latest_release()
    except Exception:
        return None  # offline / rate-limited / any error -> just run the app
    tag = rel.get("tag_name", "")
    if not tag or not _is_newer(tag, __version__):
        return None
    url = _find_asset_url(rel)
    if not url:
        return None
    return (tag, url)


def download_and_apply(url, progress_cb=None):
    """Download the new .exe beside the running one and launch a swap script.
    Returns True if the swap was started (the caller must then exit at once)."""
    if not getattr(sys, "frozen", False):
        return False  # only the packaged .exe can swap itself
    cur = sys.executable
    new = cur + ".new"

    req = urllib.request.Request(url, headers={"User-Agent": "ChromeSequencer-Updater"})
    with urllib.request.urlopen(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length", 0) or 0)
        done = 0
        with open(new, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)

    pid = os.getpid()
    log = os.path.join(tempfile.gettempdir(), "chromeseq_update.log")
    bat = os.path.join(tempfile.gettempdir(), "chromeseq_update.bat")
    with open(bat, "w", encoding="utf-8") as f:
        f.write(
            "@echo off\r\n"
            f'echo [%date% %time%] update started, waiting on PID {pid} > "{log}"\r\n'
            ":wait\r\n"
            f'tasklist /fi "PID eq {pid}" | find "{pid}" >nul\r\n'
            "if not errorlevel 1 (\r\n"
            "  timeout /t 1 /nobreak >nul\r\n"
            "  goto wait\r\n"
            ")\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            "set tries=0\r\n"
            ":trymove\r\n"
            "set /a tries=tries+1\r\n"
            f'move /y "{new}" "{cur}" >nul 2>>"{log}"\r\n'
            f'if exist "{new}" (\r\n'
            "  if %tries% lss 15 (\r\n"
            "    timeout /t 1 /nobreak >nul\r\n"
            "    goto trymove\r\n"
            "  )\r\n"
            f'  echo [%date% %time%] move FAILED after %tries% tries >> "{log}"\r\n'
            "  exit /b 1\r\n"
            ")\r\n"
            f'echo [%date% %time%] move OK after %tries% tries, relaunching >> "{log}"\r\n'
            f'start "" "{cur}"\r\n'
            'del "%~f0"\r\n'
        )
    subprocess.Popen(
        ["cmd", "/c", bat],
        creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
        close_fds=True,
    )
    return True
