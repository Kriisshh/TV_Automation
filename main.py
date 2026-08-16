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
  8. Optionally send a user-defined list of final key presses (e.g. f8, f9, f10)
  Then the app terminates itself.

Settings are saved to config.json next to the app and reloaded on launch.
A global hotkey (default Ctrl+Shift+Q) aborts the sequence and closes the app.

Optional conveniences:
  - "Launch on Windows startup" registers the app under the HKCU Run key.
  - "Run the sequence automatically when the app launches" starts step 1 on
    open (skipping confirmation dialogs), so startup + autorun = hands-free.

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
import winreg

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
# "Launch on Windows startup" (HKCU Run key)
# ----------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "ChromeSequencer"


def _startup_command():
    """Command Windows should run at logon."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # Dev mode: run this script with the current interpreter.
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def is_run_on_startup():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, RUN_VALUE_NAME)
            return bool(val)
    except OSError:
        return False


def set_run_on_startup(enable):
    """Add or remove the HKCU Run entry. Returns True on success."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, RUN_VALUE_NAME, 0, winreg.REG_SZ, _startup_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


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
# Key recorder (click, then press a key to capture it, e.g. f8/f9/f10)
# ----------------------------------------------------------------------------

# tkinter keysyms that are modifier keys on their own -> ignore as captures
_MODIFIER_KEYSYMS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R",
    "Alt_L", "Alt_R", "Win_L", "Win_R", "Super_L", "Super_R",
    "Caps_Lock", "Num_Lock", "Scroll_Lock",
}

# tkinter keysym -> keyboard-library key name, where they differ
_KEYSYM_MAP = {
    "next": "page down", "prior": "page up",
    "return": "enter", "escape": "esc",
    "space": "space", "delete": "delete", "insert": "insert",
    "home": "home", "end": "end",
}


def _keysym_to_key(event):
    """Translate a tkinter key event into a keyboard-lib key name, or '' to ignore."""
    ks = event.keysym
    if ks in _MODIFIER_KEYSYMS:
        return ""
    name = ks.lower()
    return _KEYSYM_MAP.get(name, name)


class KeyRecorder:
    """One row: a box that records a single key press, plus a remove button."""

    PLACEHOLDER = "Click, then press a key"

    def __init__(self, parent, on_remove, key=""):
        self.frame = ttk.Frame(parent)
        self.key = key
        self.recording = False
        self.var = tk.StringVar(value=(key if key else self.PLACEHOLDER))

        self.entry = ttk.Entry(self.frame, textvariable=self.var, width=22,
                               justify="center")
        self.entry.pack(side="left", padx=(0, 6))
        self.entry.bind("<Button-1>", self._begin_record)
        self.entry.bind("<FocusIn>", self._begin_record)
        self.entry.bind("<FocusOut>", self._end_record)
        # Swallow all typing; we set the text ourselves from the keysym.
        self.entry.bind("<KeyPress>", self._on_key)

        ttk.Button(self.frame, text="Record", width=7,
                   command=self._focus).pack(side="left", padx=(0, 6))
        ttk.Button(self.frame, text="✕", width=3,
                   command=lambda: on_remove(self)).pack(side="left")

    def _focus(self):
        self.entry.focus_set()
        self._begin_record()

    def _begin_record(self, event=None):
        self.recording = True
        self.var.set("Press a key...")

    def _end_record(self, event=None):
        self.recording = False
        self.var.set(self.key if self.key else self.PLACEHOLDER)

    def _on_key(self, event):
        # Always block the keystroke from editing the entry.
        if self.recording:
            name = _keysym_to_key(event)
            if name:  # ignore lone modifier presses
                self.key = name
                self.var.set(name)
                self.recording = False
                self.frame.focus_set()  # drop focus off the entry
        return "break"

    def get_key(self):
        return self.key

    def pack(self, **kw):
        self.frame.pack(**kw)

    def destroy(self):
        self.frame.destroy()


# ----------------------------------------------------------------------------
# Main app
# ----------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        root.title(f"ChromeSequencer v{__version__}")
        root.geometry("660x860")
        root.minsize(560, 600)

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
        self._maybe_autorun()

    # ---- UI ----
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        # Scrollable body so the (now taller) form never gets cut off.
        outer = tk.Canvas(self.root, highlightthickness=0)
        vsb = ttk.Scrollbar(self.root, orient="vertical", command=outer.yview)
        outer.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        outer.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(outer)
        body_win = outer.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: outer.configure(scrollregion=outer.bbox("all")))
        outer.bind("<Configure>", lambda e: outer.itemconfigure(body_win, width=e.width))
        outer.bind_all("<MouseWheel>", lambda e: outer.yview_scroll(int(-e.delta / 120), "units"))

        url_f = ttk.Frame(body)
        url_f.pack(fill="x", **pad)
        ttk.Label(url_f, text="URL:").pack(side="left")
        self.url_entry = ttk.Entry(url_f)
        self.url_entry.insert(0, "https://")
        self.url_entry.pack(side="left", fill="x", expand=True, padx=6)

        cx = ttk.Frame(body)
        cx.pack(fill="x", **pad)
        ttk.Label(cx, text="Chrome:").pack(side="left")
        self.chrome_entry = ttk.Entry(cx)
        self.chrome_entry.insert(0, self.chrome_exe)
        self.chrome_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(cx, text="...", width=3, command=self._browse_chrome).pack(side="left")

        ex = ttk.Frame(body)
        ex.pack(fill="x", **pad)
        ttk.Label(ex, text=".exe to launch:").pack(side="left")
        self.exe_entry = ttk.Entry(ex)
        self.exe_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(ex, text="...", width=3, command=self._browse_exe).pack(side="left")

        # Startup options
        opt = ttk.LabelFrame(body, text="Startup options")
        opt.pack(fill="x", padx=10, pady=4)
        self.run_on_startup = tk.BooleanVar(value=is_run_on_startup())
        ttk.Checkbutton(opt, text="Launch this app when Windows starts",
                        variable=self.run_on_startup,
                        command=self._toggle_startup).pack(anchor="w", padx=6, pady=(4, 0))
        self.autorun = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Run the sequence automatically when the app launches",
                        variable=self.autorun).pack(anchor="w", padx=6, pady=(0, 4))

        # Keys to send per tab
        kf = ttk.LabelFrame(body, text="6. Keys to send per tab")
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
        hk = ttk.Frame(body)
        hk.pack(fill="x", **pad)
        ttk.Label(hk, text="Abort hotkey:").pack(side="left")
        self.hotkey_entry = ttk.Entry(hk, width=20)
        self.hotkey_entry.insert(0, "ctrl+shift+q")
        self.hotkey_entry.pack(side="left", padx=6)
        ttk.Button(hk, text="Set", command=self._apply_hotkey).pack(side="left")

        # Delays
        self.d_initial = DelayInput(body, "1. Initial wait", "3", "3", "6")
        self.d_initial.pack(fill="x", padx=10, pady=4)
        self.d_ext = DelayInput(body, "3. Wait for extensions / VPN", "10", "8", "15")
        self.d_ext.pack(fill="x", padx=10, pady=4)
        self.d_afterurl = DelayInput(body, "5. Wait after opening URLs", "5", "4", "8")
        self.d_afterurl.pack(fill="x", padx=10, pady=4)
        self.d_pertab = DelayInput(body, "6. Wait before each tab's keys", "1", "1", "3")
        self.d_pertab.pack(fill="x", padx=10, pady=4)

        # Profiles
        pf = ttk.LabelFrame(body, text="2. Chrome profiles (1 window each)")
        pf.pack(fill="x", padx=10, pady=6)
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

        # Final key presses (sent before the app exits)
        fk = ttk.LabelFrame(body, text="8. Final key presses (sent before the app exits)")
        fk.pack(fill="x", padx=10, pady=4)
        frow = ttk.Frame(fk)
        frow.pack(fill="x", padx=6, pady=(4, 2))
        self.final_keys_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(frow, text="Enable final key presses",
                        variable=self.final_keys_enabled).pack(side="left")
        ttk.Label(frow, text="Delay between:").pack(side="left", padx=(16, 2))
        self.final_key_delay = ttk.Entry(frow, width=6)
        self.final_key_delay.insert(0, "0.5")
        self.final_key_delay.pack(side="left")
        ttk.Label(frow, text="sec").pack(side="left", padx=(2, 0))

        list_area = ttk.Frame(fk)
        list_area.pack(fill="x", padx=6)
        fkcanvas = tk.Canvas(list_area, height=90, highlightthickness=0)
        fkscroll = ttk.Scrollbar(list_area, orient="vertical", command=fkcanvas.yview)
        self.final_keys_inner = ttk.Frame(fkcanvas)
        self.final_keys_inner.bind(
            "<Configure>", lambda e: fkcanvas.configure(scrollregion=fkcanvas.bbox("all")))
        fkcanvas.create_window((0, 0), window=self.final_keys_inner, anchor="nw")
        fkcanvas.configure(yscrollcommand=fkscroll.set)
        fkcanvas.pack(side="left", fill="x", expand=True)
        fkscroll.pack(side="right", fill="y")
        ttk.Button(fk, text="+ Add key", command=self._add_final_key).pack(anchor="w", padx=6, pady=(2, 6))
        self.final_key_recorders = []

        ctrl = ttk.Frame(body)
        ctrl.pack(fill="x", padx=10, pady=6)
        self.start_btn = ttk.Button(ctrl, text="START", command=self._start)
        self.start_btn.pack(side="left")
        ttk.Button(ctrl, text="ABORT", command=self._abort).pack(side="left", padx=6)

        self.log = tk.Text(body, height=6, state="disabled")
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
            "run_on_startup": self.run_on_startup.get(),
            "autorun": self.autorun.get(),
            "final_keys_enabled": self.final_keys_enabled.get(),
            "final_key_delay": self.final_key_delay.get(),
            "final_keys": [r.get_key() for r in self.final_key_recorders if r.get_key()],
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
        if "autorun" in cfg:
            self.autorun.set(bool(cfg["autorun"]))
        if "final_keys_enabled" in cfg:
            self.final_keys_enabled.set(bool(cfg["final_keys_enabled"]))
        if "final_key_delay" in cfg:
            self.final_key_delay.delete(0, "end")
            self.final_key_delay.insert(0, str(cfg["final_key_delay"]))
        for k in cfg.get("final_keys", []):
            if k:
                self._add_final_key(k)
        # Registry is the source of truth for startup; reconcile it with config
        # so the setting follows the app across machines / reinstalls.
        if "run_on_startup" in cfg:
            want = bool(cfg["run_on_startup"])
            if want != is_run_on_startup():
                set_run_on_startup(want)
            self.run_on_startup.set(want)

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

    # ---- startup / autorun / final keys ----
    def _toggle_startup(self):
        ok = set_run_on_startup(self.run_on_startup.get())
        if ok:
            self._log("Windows startup: " +
                      ("enabled" if self.run_on_startup.get() else "disabled"))
        else:
            self._log("Could not update the Windows startup setting.")
            # revert the checkbox to reflect the real (unchanged) state
            self.run_on_startup.set(is_run_on_startup())
        self._save_settings()

    def _add_final_key(self, key=""):
        rec = KeyRecorder(self.final_keys_inner, self._remove_final_key, key=key)
        rec.pack(anchor="w", pady=2)
        self.final_key_recorders.append(rec)

    def _remove_final_key(self, rec):
        try:
            self.final_key_recorders.remove(rec)
        except ValueError:
            pass
        rec.destroy()

    def _maybe_autorun(self):
        if self.autorun.get():
            self._log("Auto-run enabled: starting sequence shortly...")
            self.root.after(1200, lambda: self._start(auto=True))

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

    def _start(self, auto=False):
        if self.worker and self.worker.is_alive():
            return
        selected = [d for d, v in self.profile_vars.items() if v.get()]
        if not selected:
            if auto:
                self._log("Auto-run: no Chrome profiles selected, skipping.")
                return
            messagebox.showwarning("No profiles", "Select at least one Chrome profile.")
            return
        chrome = self.chrome_entry.get().strip()
        if not os.path.isfile(chrome):
            if auto:
                self._log("Auto-run: chrome.exe not found, skipping.")
                return
            messagebox.showwarning("Chrome", "chrome.exe not found. Set the path.")
            return
        url = self.url_entry.get().strip()
        if not url or url == "https://":
            # Don't block an unattended auto-run with a dialog.
            if not auto and not messagebox.askyesno("URL", "URL looks empty. Continue anyway?"):
                return

        try:
            key_delay = max(0.0, float(self.key_delay.get()))
        except ValueError:
            key_delay = 0.0
        try:
            final_delay = max(0.0, float(self.final_key_delay.get()))
        except ValueError:
            final_delay = 0.0
        final_keys = ([r.get_key() for r in self.final_key_recorders if r.get_key()]
                      if self.final_keys_enabled.get() else [])

        self._save_settings()
        self.stop_event.clear()
        self.start_btn.config(state="disabled")
        self.worker = threading.Thread(
            target=self._run_sequence,
            args=(chrome, url, selected, self.send_m.get(), self.send_c.get(),
                  key_delay, self.exe_entry.get().strip(), final_keys, final_delay),
            daemon=True,
        )
        self.worker.start()

    def _abort(self):
        self.stop_event.set()
        self._save_settings()
        os._exit(0)

    def _run_sequence(self, chrome, url, profiles, send_m, send_c, key_delay, target_exe,
                      final_keys=None, final_delay=0.5):
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

            if final_keys:
                self._log(f"[8] Final key presses before exit: {final_keys}")
                # give the just-launched exe a moment to take focus
                self._sleep(1.0)
                for i, k in enumerate(final_keys):
                    if self.stop_event.is_set():
                        raise KeyboardInterrupt
                    try:
                        keyboard.send(k)
                        self._log(f"    sent {k}")
                    except Exception as e:
                        self._log(f"    failed to send {k}: {e}")
                    if i < len(final_keys) - 1:
                        self._sleep(final_delay)

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
            # Force-exit immediately so the swap script's wait loop sees this
            # PID disappear. A plain return can hang under --noconsole because
            # the withdrawn root + update Toplevel keep the interpreter alive.
            os._exit(0)
    except Exception:
        pass
    root.deiconify()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
