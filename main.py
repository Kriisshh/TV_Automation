"""
ChromeSequencer - Windows desktop automation app.

Sequence:
  1. Initial wait (fixed or random)
  2. Open selected Chrome profiles (1 window each, blank tab)
  3. Wait for extensions / VPN to load (fixed or random)
  4. Navigate every profile window to the target URL (1 tab per profile)
  5. Wait (fixed or random)
  6. For every profile window: wait, focus it, send the enabled keys ("m" and/or "c")
  7. Launch a target .exe
  Then the app terminates itself.

Settings are saved to config.json next to the app and reloaded on launch.
A global hotkey (default Ctrl+Shift+Q) aborts the sequence and closes the app.

Deps: pip install keyboard pywin32   (tkinter ships with Python)
Windows only.
"""

import os
import sys
import json
import time
import random
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import keyboard
import win32gui
import win32con
import win32api
import win32process
import ctypes

from updater import __version__, check_for_update, download_and_apply


# ----------------------------------------------------------------------------
# Config file location (next to the .exe / script)
# ----------------------------------------------------------------------------

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "config.json")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(data):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Chrome discovery
# ----------------------------------------------------------------------------

def find_chrome_exe():
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


def chrome_user_data_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")


def list_profiles():
    """Return list of (directory, display_name)."""
    base = chrome_user_data_dir()
    profiles = []
    local_state = os.path.join(base, "Local State")
    info = {}
    if os.path.isfile(local_state):
        try:
            with open(local_state, "r", encoding="utf-8") as f:
                data = json.load(f)
            info = data.get("profile", {}).get("info_cache", {})
        except Exception:
            info = {}

    if info:
        for directory, meta in info.items():
            name = meta.get("name", directory)
            profiles.append((directory, name))
    else:
        if os.path.isdir(base):
            for entry in os.listdir(base):
                p = os.path.join(base, entry)
                if os.path.isdir(p) and os.path.isfile(os.path.join(p, "Preferences")):
                    if entry == "Default" or entry.startswith("Profile"):
                        profiles.append((entry, entry))

    profiles.sort(key=lambda x: (x[0] != "Default", x[0].lower()))
    return profiles


# ----------------------------------------------------------------------------
# Window helpers
# ----------------------------------------------------------------------------

def chrome_window_handles():
    handles = set()

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            try:
                cls = win32gui.GetClassName(hwnd)
                title = win32gui.GetWindowText(hwnd)
            except Exception:
                return True
            if cls == "Chrome_WidgetWin_1" and title.strip().endswith("Google Chrome"):
                handles.add(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return handles


def force_foreground(hwnd):
    """Reliably bring a window to the foreground and focus it."""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        fg = win32gui.GetForegroundWindow()
        cur_thread = win32api.GetCurrentThreadId()
        fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        tgt_thread = win32process.GetWindowThreadProcessId(hwnd)[0]

        attached = []
        for t in (fg_thread, tgt_thread):
            if t and t != cur_thread:
                try:
                    ctypes.windll.user32.AttachThreadInput(cur_thread, t, True)
                    attached.append(t)
                except Exception:
                    pass
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        for t in attached:
            try:
                ctypes.windll.user32.AttachThreadInput(cur_thread, t, False)
            except Exception:
                pass
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Delay widget (fixed or random)
# ----------------------------------------------------------------------------

class DelayInput:
    def __init__(self, parent, label, default_fixed="2", default_min="2", default_max="5"):
        self.frame = ttk.LabelFrame(parent, text=label)
        self.mode = tk.StringVar(value="fixed")

        row = ttk.Frame(self.frame)
        row.pack(fill="x", padx=6, pady=4)

        ttk.Radiobutton(row, text="Fixed", variable=self.mode, value="fixed",
                        command=self._sync).grid(row=0, column=0, sticky="w")
        self.fixed = ttk.Entry(row, width=7)
        self.fixed.insert(0, default_fixed)
        self.fixed.grid(row=0, column=1, padx=(4, 12))
        ttk.Label(row, text="sec").grid(row=0, column=2, sticky="w")

        ttk.Radiobutton(row, text="Random", variable=self.mode, value="random",
                        command=self._sync).grid(row=0, column=3, sticky="w", padx=(16, 0))
        ttk.Label(row, text="min").grid(row=0, column=4, padx=(6, 2))
        self.rmin = ttk.Entry(row, width=6)
        self.rmin.insert(0, default_min)
        self.rmin.grid(row=0, column=5)
        ttk.Label(row, text="max").grid(row=0, column=6, padx=(8, 2))
        self.rmax = ttk.Entry(row, width=6)
        self.rmax.insert(0, default_max)
        self.rmax.grid(row=0, column=7)
        ttk.Label(row, text="sec").grid(row=0, column=8, padx=(4, 0))

        self._sync()

    def _sync(self):
        fixed = self.mode.get() == "fixed"
        self.fixed.config(state="normal" if fixed else "disabled")
        for w in (self.rmin, self.rmax):
            w.config(state="disabled" if fixed else "normal")

    def seconds(self):
        try:
            if self.mode.get() == "fixed":
                return max(0.0, float(self.fixed.get()))
            lo = float(self.rmin.get())
            hi = float(self.rmax.get())
            if hi < lo:
                lo, hi = hi, lo
            return random.uniform(max(0.0, lo), max(0.0, hi))
        except ValueError:
            return 0.0

    def get_state(self):
        return {
            "mode": self.mode.get(),
            "fixed": self.fixed.get(),
            "min": self.rmin.get(),
            "max": self.rmax.get(),
        }

    def set_state(self, d):
        if not isinstance(d, dict):
            return
        for w in (self.fixed, self.rmin, self.rmax):
            w.config(state="normal")
        self.mode.set(d.get("mode", "fixed"))
        self.fixed.delete(0, "end"); self.fixed.insert(0, d.get("fixed", "2"))
        self.rmin.delete(0, "end"); self.rmin.insert(0, d.get("min", "2"))
        self.rmax.delete(0, "end"); self.rmax.insert(0, d.get("max", "5"))
        self._sync()

    def pack(self, **kw):
        self.frame.pack(**kw)


# ----------------------------------------------------------------------------
# Main app
# ----------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        root.title(f"ChromeSequencer v{__version__}")
        root.geometry("640x780")

        self.stop_event = threading.Event()
        self.worker = None
        self.hotkey_handle = None

        self.chrome_exe = find_chrome_exe()
        self.profiles = list_profiles()
        self.profile_vars = {}

        self._build_ui()
        self._load_settings()
        self._register_hotkey(self.hotkey_entry.get())
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI ----
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        url_f = ttk.Frame(self.root)
        url_f.pack(fill="x", **pad)
        ttk.Label(url_f, text="URL:").pack(side="left")
        self.url_entry = ttk.Entry(url_f)
        self.url_entry.insert(0, "https://")
        self.url_entry.pack(side="left", fill="x", expand=True, padx=6)

        cx = ttk.Frame(self.root)
        cx.pack(fill="x", **pad)
        ttk.Label(cx, text="Chrome:").pack(side="left")
        self.chrome_entry = ttk.Entry(cx)
        self.chrome_entry.insert(0, self.chrome_exe)
        self.chrome_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(cx, text="...", width=3, command=self._browse_chrome).pack(side="left")

        ex = ttk.Frame(self.root)
        ex.pack(fill="x", **pad)
        ttk.Label(ex, text=".exe to launch:").pack(side="left")
        self.exe_entry = ttk.Entry(ex)
        self.exe_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(ex, text="...", width=3, command=self._browse_exe).pack(side="left")

        # Keys to send per tab
        kf = ttk.LabelFrame(self.root, text="6. Keys to send per tab")
        kf.pack(fill="x", padx=10, pady=4)
        krow = ttk.Frame(kf)
        krow.pack(fill="x", padx=6, pady=4)
        self.send_m = tk.BooleanVar(value=True)
        self.send_c = tk.BooleanVar(value=True)
        ttk.Checkbutton(krow, text='Send  "m"', variable=self.send_m).pack(side="left", padx=(0, 20))
        ttk.Checkbutton(krow, text='Send  "c"', variable=self.send_c).pack(side="left", padx=(0, 24))
        ttk.Label(krow, text="Delay between keys:").pack(side="left")
        self.key_delay = ttk.Entry(krow, width=6)
        self.key_delay.insert(0, "0.3")
        self.key_delay.pack(side="left", padx=6)
        ttk.Label(krow, text="sec").pack(side="left")

        # Hotkey
        hk = ttk.Frame(self.root)
        hk.pack(fill="x", **pad)
        ttk.Label(hk, text="Abort hotkey:").pack(side="left")
        self.hotkey_entry = ttk.Entry(hk, width=20)
        self.hotkey_entry.insert(0, "ctrl+shift+q")
        self.hotkey_entry.pack(side="left", padx=6)
        ttk.Button(hk, text="Set", command=self._apply_hotkey).pack(side="left")

        # Delays
        self.d_initial = DelayInput(self.root, "1. Initial wait", "3", "3", "6")
        self.d_initial.pack(fill="x", padx=10, pady=4)
        self.d_ext = DelayInput(self.root, "3. Wait for extensions / VPN", "10", "8", "15")
        self.d_ext.pack(fill="x", padx=10, pady=4)
        self.d_afterurl = DelayInput(self.root, "5. Wait after opening URLs", "5", "4", "8")
        self.d_afterurl.pack(fill="x", padx=10, pady=4)
        self.d_pertab = DelayInput(self.root, "6. Wait before each tab's keys", "1", "1", "3")
        self.d_pertab.pack(fill="x", padx=10, pady=4)

        # Profiles
        pf = ttk.LabelFrame(self.root, text="2. Chrome profiles (1 window each)")
        pf.pack(fill="both", expand=True, padx=10, pady=6)
        top = ttk.Frame(pf)
        top.pack(fill="x")
        ttk.Button(top, text="All", command=lambda: self._set_all(True)).pack(side="left", padx=2, pady=2)
        ttk.Button(top, text="None", command=lambda: self._set_all(False)).pack(side="left", padx=2, pady=2)
        canvas = tk.Canvas(pf, height=120, highlightthickness=0)
        scroll = ttk.Scrollbar(pf, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        if not self.profiles:
            ttk.Label(inner, text="No Chrome profiles found.").pack(anchor="w")
        for directory, name in self.profiles:
            var = tk.BooleanVar(value=True)
            self.profile_vars[directory] = var
            ttk.Checkbutton(inner, text=f"{name}   [{directory}]", variable=var).pack(anchor="w")

        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill="x", padx=10, pady=6)
        self.start_btn = ttk.Button(ctrl, text="START", command=self._start)
        self.start_btn.pack(side="left")
        ttk.Button(ctrl, text="ABORT", command=self._abort).pack(side="left", padx=6)

        self.log = tk.Text(self.root, height=7, state="disabled")
        self.log.pack(fill="both", expand=False, padx=10, pady=(0, 10))

    # ---- settings persistence ----
    def _collect_settings(self):
        return {
            "url": self.url_entry.get(),
            "chrome": self.chrome_entry.get(),
            "exe": self.exe_entry.get(),
            "hotkey": self.hotkey_entry.get(),
            "send_m": self.send_m.get(),
            "send_c": self.send_c.get(),
            "key_delay": self.key_delay.get(),
            "delays": {
                "initial": self.d_initial.get_state(),
                "ext": self.d_ext.get_state(),
                "afterurl": self.d_afterurl.get_state(),
                "pertab": self.d_pertab.get_state(),
            },
            "profiles": [d for d, v in self.profile_vars.items() if v.get()],
        }

    def _load_settings(self):
        cfg = load_config()
        if not cfg:
            return
        if "url" in cfg:
            self.url_entry.delete(0, "end"); self.url_entry.insert(0, cfg["url"])
        if cfg.get("chrome"):
            self.chrome_entry.delete(0, "end"); self.chrome_entry.insert(0, cfg["chrome"])
        if "exe" in cfg:
            self.exe_entry.delete(0, "end"); self.exe_entry.insert(0, cfg["exe"])
        if cfg.get("hotkey"):
            self.hotkey_entry.delete(0, "end"); self.hotkey_entry.insert(0, cfg["hotkey"])
        if "send_m" in cfg:
            self.send_m.set(bool(cfg["send_m"]))
        if "send_c" in cfg:
            self.send_c.set(bool(cfg["send_c"]))
        if "key_delay" in cfg:
            self.key_delay.delete(0, "end"); self.key_delay.insert(0, str(cfg["key_delay"]))
        d = cfg.get("delays", {})
        self.d_initial.set_state(d.get("initial"))
        self.d_ext.set_state(d.get("ext"))
        self.d_afterurl.set_state(d.get("afterurl"))
        self.d_pertab.set_state(d.get("pertab"))
        if "profiles" in cfg:
            saved = set(cfg["profiles"])
            for directory, var in self.profile_vars.items():
                var.set(directory in saved)

    def _save_settings(self):
        save_config(self._collect_settings())

    def _on_close(self):
        self._save_settings()
        os._exit(0)

    def _browse_chrome(self):
        p = filedialog.askopenfilename(filetypes=[("chrome.exe", "chrome.exe"), ("Executable", "*.exe")])
        if p:
            self.chrome_entry.delete(0, "end"); self.chrome_entry.insert(0, p)

    def _browse_exe(self):
        p = filedialog.askopenfilename(filetypes=[("Executable", "*.exe"), ("All", "*.*")])
        if p:
            self.exe_entry.delete(0, "end"); self.exe_entry.insert(0, p)

    def _set_all(self, val):
        for v in self.profile_vars.values():
            v.set(val)

    def _log(self, msg):
        def do():
            self.log.config(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.config(state="disabled")
        self.root.after(0, do)

    # ---- hotkey ----
    def _register_hotkey(self, combo):
        if self.hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self.hotkey_handle)
            except Exception:
                pass
            self.hotkey_handle = None
        try:
            self.hotkey_handle = keyboard.add_hotkey(combo, self._hotkey_fired)
            self._log(f"Abort hotkey set: {combo}")
        except Exception as e:
            self._log(f"Failed to set hotkey '{combo}': {e}")

    def _apply_hotkey(self):
        self._register_hotkey(self.hotkey_entry.get().strip().lower())

    def _hotkey_fired(self):
        self.stop_event.set()
        self._save_settings()
        os._exit(0)

    # ---- run ----
    def _sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_event.is_set():
                raise KeyboardInterrupt
            time.sleep(0.05)

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        selected = [d for d, v in self.profile_vars.items() if v.get()]
        if not selected:
            messagebox.showwarning("No profiles", "Select at least one Chrome profile.")
            return
        chrome = self.chrome_entry.get().strip()
        if not os.path.isfile(chrome):
            messagebox.showwarning("Chrome", "chrome.exe not found. Set the path.")
            return
        url = self.url_entry.get().strip()
        if not url or url == "https://":
            if not messagebox.askyesno("URL", "URL looks empty. Continue anyway?"):
                return

        try:
            key_delay = max(0.0, float(self.key_delay.get()))
        except ValueError:
            key_delay = 0.0

        self._save_settings()
        self.stop_event.clear()
        self.start_btn.config(state="disabled")
        self.worker = threading.Thread(
            target=self._run_sequence,
            args=(chrome, url, selected, self.send_m.get(), self.send_c.get(),
                  key_delay, self.exe_entry.get().strip()),
            daemon=True,
        )
        self.worker.start()

    def _abort(self):
        self.stop_event.set()
        self._save_settings()
        os._exit(0)

    def _run_sequence(self, chrome, url, profiles, send_m, send_c, key_delay, target_exe):
        try:
            keys = []
            if send_m:
                keys.append("m")
            if send_c:
                keys.append("c")

            s = self.d_initial.seconds()
            self._log(f"[1] Initial wait {s:.1f}s")
            self._sleep(s)

            self._log("[2] Opening Chrome profiles...")
            profile_hwnds = {}
            for directory in profiles:
                if self.stop_event.is_set():
                    raise KeyboardInterrupt
                before = chrome_window_handles()
                subprocess.Popen([
                    chrome, f"--profile-directory={directory}", "--new-window", "about:blank",
                ])
                hwnd = self._wait_for_new_window(before, timeout=15)
                if hwnd:
                    profile_hwnds[directory] = hwnd
                    self._log(f"    opened [{directory}]")
                else:
                    self._log(f"    WARNING: window not detected for [{directory}]")
                self._sleep(1.0)

            s = self.d_ext.seconds()
            self._log(f"[3] Wait for extensions / VPN {s:.1f}s")
            self._sleep(s)

            self._log("[4] Navigating profiles to URL...")
            for directory, hwnd in profile_hwnds.items():
                if self.stop_event.is_set():
                    raise KeyboardInterrupt
                force_foreground(hwnd)
                self._sleep(0.4)
                keyboard.send("ctrl+l")
                self._sleep(0.2)
                keyboard.write(url, delay=0.01)
                self._sleep(0.1)
                keyboard.send("enter")
                self._log(f"    navigated [{directory}]")
                self._sleep(0.5)

            s = self.d_afterurl.seconds()
            self._log(f"[5] Wait after opening URLs {s:.1f}s")
            self._sleep(s)

            self._log(f"[6] Sending keys per tab: {keys or '(none)'}")
            for directory, hwnd in profile_hwnds.items():
                if self.stop_event.is_set():
                    raise KeyboardInterrupt
                s = self.d_pertab.seconds()
                self._log(f"    [{directory}] wait {s:.1f}s then send {keys or '(none)'}")
                self._sleep(s)
                force_foreground(hwnd)
                self._sleep(0.3)
                for i, k in enumerate(keys):
                    if self.stop_event.is_set():
                        raise KeyboardInterrupt
                    keyboard.send(k)
                    if i < len(keys) - 1:
                        self._sleep(key_delay)

            if target_exe:
                if os.path.isfile(target_exe):
                    self._log(f"[7] Launching {target_exe}")
                    try:
                        os.startfile(target_exe)
                    except Exception as e:
                        self._log(f"    Failed to launch exe: {e}")
                else:
                    self._log(f"[7] WARNING: exe not found: {target_exe}")
            else:
                self._log("[7] No exe specified, skipping.")

            self._log("Done. Terminating app.")
            self._save_settings()
            time.sleep(1.0)
            os._exit(0)

        except KeyboardInterrupt:
            self._log("Aborted.")
            self._save_settings()
            os._exit(0)
        except Exception as e:
            self._log(f"Error: {e}")
            self.root.after(0, lambda: self.start_btn.config(state="normal"))

    def _wait_for_new_window(self, before, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if self.stop_event.is_set():
                raise KeyboardInterrupt
            new = chrome_window_handles() - before
            if new:
                return sorted(new)[-1]
            time.sleep(0.2)
        return None


def run_update_flow(root):
    """Check GitHub for a newer release; if found, download + relaunch.
    Returns True if an update was started (caller should exit)."""
    info = check_for_update()
    if not info:
        return False
    tag, url = info

    top = tk.Toplevel(root)
    top.title("Updating")
    top.geometry("340x100")
    top.resizable(False, False)
    tk.Label(top, text=f"Updating to {tag}...", font=("Segoe UI", 10, "bold")).pack(pady=(16, 6))
    status = tk.Label(top, text="Starting download...")
    status.pack()
    top.update()

    def cb(done, total):
        if total:
            pct = int(done * 100 / total)
            status.config(text=f"{pct}%   ({done // 1024} / {total // 1024} KB)")
        else:
            status.config(text=f"{done // 1024} KB")
        top.update()

    try:
        if download_and_apply(url, cb):
            status.config(text="Restarting...")
            top.update()
            return True
    except Exception:
        pass
    top.destroy()
    return False


def main():
    if not sys.platform.startswith("win"):
        print("Windows only.")
        return
    root = tk.Tk()
    root.withdraw()
    try:
        if run_update_flow(root):
            return  # process exits so the swap script can replace the .exe
    except Exception:
        pass
    root.deiconify()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
