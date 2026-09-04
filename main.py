"""
Stream Suite - Windows desktop automation app (three tabs in one window).

Tab 1 - Chrome Sequencer:
  Initial wait, then for EACH selected profile in turn: open one window ->
  wait (extensions/VPN) -> navigate to the next URL (assigned in order) -> wait
  -> send per-profile keys ("m"/"c") -> delay between profiles. Then final key
  presses. When it finishes the app stays open and switches to the Typer tab.
  The abort hotkey can optionally double as a start hotkey (toggle start/stop).

Tab 2 - Typer:
  Stream-chat message sequencer. Profile "groups" each own a global hotkey, a
  switch key, loop settings, and an ordered list of message steps. Adapted from
  TyperV9.

Tab 3 - Application:
  App-wide settings: launch on startup, auto-run, abort hotkey, update check,
  and config import/export.

Settings persist next to the app:
  - config.json          (Chrome Sequencer + app settings)
  - stream_groups.json   (Typer)

UI: Apple / macOS-inspired light theme (flat, minimal, card-based, generous
whitespace, Segoe UI as the SF-Pro substitute, teal accent).

Deps: pip install keyboard pywin32   (tkinter ships with Python)
Windows only.
"""

import os
import sys
import json
import time
import random
import shutil
import itertools
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter import font as tkfont

import keyboard
import win32gui
import win32con
import win32api
import win32process
import ctypes
import winreg

from updater import __version__, check_for_update, download_and_apply


# ----------------------------------------------------------------------------
# Config file locations (next to the .exe / script)
# ----------------------------------------------------------------------------

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "config.json")               # Chrome + app
STREAM_CONFIG_PATH = os.path.join(app_dir(), "stream_groups.json")  # Typer


def extension_dir():
    """Unpacked helper extension, expected next to the app."""
    return os.path.join(app_dir(), "chat_focus_extension")


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
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def is_run_on_startup():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, RUN_VALUE_NAME)
            return bool(val)
    except OSError:
        return False


def set_run_on_startup(enable):
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


def _work_area():
    """Primary monitor work area (screen minus taskbar): (left, top, right, bottom)."""
    import ctypes.wintypes
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


def tile_windows(hwnds):
    """Arrange the given windows into a near-square grid across the work area."""
    import math
    n = len(hwnds)
    if n == 0:
        return
    l, t, r, b = _work_area()
    # Chrome refuses to make a window narrower than ~500px; if a cell would be
    # smaller it force-widens the window and the right column spills off-screen.
    # Cap the column count so each cell stays at least this wide.
    MIN_CELL_W = 500
    max_cols = max(1, (r - l) // MIN_CELL_W)
    cols = min(math.ceil(math.sqrt(n)), max_cols)
    rows = math.ceil(n / cols)
    w = (r - l) // cols
    h = (b - t) // rows
    for i, hwnd in enumerate(hwnds):
        x = l + (i % cols) * w
        y = t + (i // cols) * h
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetWindowPos(hwnd, 0, x, y, w, h,
                                  win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        except Exception:
            pass


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
# Design tokens (Apple/macOS-inspired light theme, teal accent)
# ----------------------------------------------------------------------------

MAC_BG = "#F5F5F7"            # window canvas
MAC_CARD = "#FFFFFF"         # cards / content surfaces
MAC_TEXT = "#1D1D1F"         # primary near-black
MAC_TEXT_SUB = "#6E6E73"     # secondary gray
MAC_BORDER = "#E3E3E6"       # separators / card borders
MAC_ACCENT = "#0D9488"       # teal
MAC_ACCENT_HOVER = "#0B7C71"
MAC_CONTROL_MUTED = "#EEEEF0"  # subtle neutral button bg
MAC_CONTROL_HOVER = "#E2E2E6"
MAC_SUCCESS = "#2E9E5B"
MAC_DANGER = "#E5484D"
MAC_DANGER_HOVER = "#CE3F44"
MAC_DISABLED_BG = "#E5E5EA"
MAC_DISABLED_TEXT = "#B0B0B5"

FONT_FAMILY = "Segoe UI"
FONT_H1 = (FONT_FAMILY, 16, "bold")
FONT_H2 = (FONT_FAMILY, 12, "bold")
FONT_BODY = (FONT_FAMILY, 10)
FONT_BODY_MED = (FONT_FAMILY, 10, "bold")
FONT_SMALL = (FONT_FAMILY, 9)

# --- Aliases so the ported Typer code keeps using its original names ---
COLOR_BG = MAC_BG
COLOR_WHITE = MAC_CARD
COLOR_TEXT_MAIN = MAC_TEXT
COLOR_TEXT_SUB = MAC_TEXT_SUB
COLOR_BTN_BG = MAC_CONTROL_MUTED
COLOR_BTN_HOVER = MAC_CONTROL_HOVER
COLOR_BTN_TEXT = MAC_TEXT
COLOR_ACCENT = MAC_ACCENT
COLOR_BORDER_LIGHT = MAC_BORDER
COLOR_STATUS_ACTIVE = MAC_SUCCESS
COLOR_STATUS_INACTIVE = "#C77700"

FONT_TITLE = FONT_H2
FONT_MAIN = FONT_BODY
FONT_SUB = FONT_SMALL
FONT_MONITOR = (FONT_FAMILY, 10, "bold")


def apply_mac_theme(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=MAC_BG)

    style.configure(".", background=MAC_BG, foreground=MAC_TEXT, font=FONT_BODY)
    style.configure("TFrame", background=MAC_BG)

    style.configure("TLabel", background=MAC_BG, foreground=MAC_TEXT, font=FONT_BODY)
    style.configure("Sub.TLabel", background=MAC_BG, foreground=MAC_TEXT_SUB, font=FONT_SMALL)
    style.configure("H1.TLabel", background=MAC_BG, foreground=MAC_ACCENT, font=FONT_H1)

    # Card-context styles (white background)
    style.configure("Card.TLabel", background=MAC_CARD, foreground=MAC_TEXT, font=FONT_BODY)
    style.configure("CardTitle.TLabel", background=MAC_CARD, foreground=MAC_ACCENT, font=FONT_BODY_MED)
    style.configure("CardSub.TLabel", background=MAC_CARD, foreground=MAC_TEXT_SUB, font=FONT_SMALL)
    style.configure("Card.TCheckbutton", background=MAC_CARD, foreground=MAC_TEXT, font=FONT_BODY)
    style.map("Card.TCheckbutton", background=[("active", MAC_CARD)])
    style.configure("Card.TRadiobutton", background=MAC_CARD, foreground=MAC_TEXT, font=FONT_BODY)
    style.map("Card.TRadiobutton", background=[("active", MAC_CARD)])

    style.configure("TEntry", fieldbackground=MAC_CARD, background=MAC_CARD,
                    bordercolor=MAC_BORDER, lightcolor=MAC_BORDER, darkcolor=MAC_BORDER,
                    foreground=MAC_TEXT, insertcolor=MAC_TEXT, padding=5, relief="flat")
    style.map("TEntry",
              bordercolor=[("focus", MAC_ACCENT)],
              lightcolor=[("focus", MAC_ACCENT)],
              darkcolor=[("focus", MAC_ACCENT)])

    style.configure("TButton", background=MAC_CONTROL_MUTED, foreground=MAC_TEXT,
                    bordercolor=MAC_BORDER, focuscolor=MAC_ACCENT, relief="flat",
                    padding=(10, 5), font=FONT_BODY)
    style.map("TButton",
              background=[("active", MAC_CONTROL_HOVER), ("disabled", MAC_DISABLED_BG)],
              foreground=[("disabled", MAC_DISABLED_TEXT)])

    style.configure("TCheckbutton", background=MAC_BG, foreground=MAC_TEXT, font=FONT_BODY)
    style.map("TCheckbutton", background=[("active", MAC_BG)])
    style.configure("TRadiobutton", background=MAC_BG, foreground=MAC_TEXT, font=FONT_BODY)
    style.map("TRadiobutton", background=[("active", MAC_BG)])

    style.configure("TNotebook", background=MAC_BG, borderwidth=0, tabmargins=(4, 6, 4, 0))
    style.configure("TNotebook.Tab", background=MAC_BG, foreground=MAC_TEXT_SUB,
                    padding=(18, 9), font=FONT_BODY_MED, borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", MAC_BG)],
              foreground=[("selected", MAC_ACCENT), ("active", MAC_TEXT)])

    style.configure("TScrollbar", background=MAC_CONTROL_MUTED, troughcolor=MAC_BG,
                    bordercolor=MAC_BG, arrowcolor=MAC_TEXT_SUB, relief="flat")
    return style


def _round_points(x1, y1, x2, y2, r):
    return [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
            x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]


def make_card(parent, title):
    """A white, thin-bordered card. Build content into the returned .body frame."""
    c = tk.Frame(parent, bg=MAC_CARD, highlightbackground=MAC_BORDER,
                 highlightcolor=MAC_BORDER, highlightthickness=1, bd=0)
    inner = tk.Frame(c, bg=MAC_CARD)
    inner.pack(fill="both", expand=True, padx=12, pady=9)
    ttk.Label(inner, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=(0, 6))
    c.body = inner
    return c


class MiniScrollbar(tk.Canvas):
    """Thin, rounded, teal scrollbar (no arrows, no track)."""

    def __init__(self, parent, command, width=8, bg=MAC_BG):
        super().__init__(parent, width=width, bg=bg, highlightthickness=0, bd=0)
        self.command = command
        self.first, self.last = 0.0, 1.0
        self._thick = width
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._jump)
        self.bind("<B1-Motion>", self._jump)

    def set(self, first, last):
        self.first, self.last = float(first), float(last)
        self._redraw()

    def _redraw(self):
        self.delete("thumb")
        if self.first <= 0.0 and self.last >= 1.0:
            return  # content fits; no thumb
        h = self.winfo_height()
        if h <= 1:
            return
        y1 = self.first * h + 1
        y2 = self.last * h - 1
        w = self._thick
        r = (w - 3) / 2
        self.create_polygon(_round_points(2, y1, w - 1, y2, r), fill=MAC_ACCENT,
                            outline="", smooth=True, tags="thumb")

    def _jump(self, e):
        h = self.winfo_height() or 1
        span = self.last - self.first
        if span >= 1.0:
            return
        frac = max(0.0, min(1.0 - span, e.y / h - span / 2))
        self.command("moveto", frac)


class PillTab(tk.Canvas):
    """A rounded, detached tab 'pill' that fills teal when selected."""

    def __init__(self, parent, text, command):
        f = tkfont.Font(family=FONT_FAMILY, size=10, weight="bold")
        w = f.measure(text) + 36
        super().__init__(parent, width=w, height=34, bg=MAC_BG, highlightthickness=0, bd=0)
        self.command = command
        self.selected = False
        self.rect = self.create_polygon(_round_points(2, 2, w - 2, 32, 15), fill=MAC_BG,
                                        outline="", smooth=True)
        self.tid = self.create_text(w / 2, 18, text=text, fill=MAC_TEXT_SUB, font=f)
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", self._hover)
        self.bind("<Leave>", self._leave)

    def set_selected(self, sel):
        self.selected = sel
        self.itemconfig(self.rect, fill=MAC_ACCENT if sel else MAC_BG)
        self.itemconfig(self.tid, fill="#FFFFFF" if sel else MAC_TEXT_SUB)

    def _hover(self, e):
        if not self.selected:
            self.itemconfig(self.rect, fill=MAC_CONTROL_MUTED)

    def _leave(self, e):
        if not self.selected:
            self.itemconfig(self.rect, fill=MAC_BG)


# ----------------------------------------------------------------------------
# Custom rounded button (shared for a consistent look)
# ----------------------------------------------------------------------------

class RoundedButton(tk.Canvas):
    def __init__(self, parent, width, height, text, command=None, radius=8,
                 bg_color=COLOR_BTN_BG, hover_color=COLOR_BTN_HOVER,
                 text_color=COLOR_BTN_TEXT, canvas_bg=COLOR_BG):
        super().__init__(parent, width=width, height=height, bg=canvas_bg, highlightthickness=0)
        self.command = command
        self.radius = radius
        self.bg_color = bg_color
        self.hover_color = hover_color
        self.text_color = text_color
        self.text = text
        self._enabled = True

        self.rect = self._draw_rounded_rect(2, 2, width - 2, height - 2, radius, bg_color)
        self.text_id = self.create_text(width / 2, height / 2, text=text, fill=text_color, font=FONT_BODY_MED)

        self.bind("<Enter>", self._on_hover)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _draw_rounded_rect(self, x1, y1, x2, y2, r, fill):
        points = [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
                  x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
                  x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]
        return self.create_polygon(points, fill=fill, smooth=True)

    def set_enabled(self, enabled):
        self._enabled = enabled
        self.itemconfig(self.rect, fill=self.bg_color if enabled else MAC_DISABLED_BG)
        self.itemconfig(self.text_id, fill=self.text_color if enabled else MAC_DISABLED_TEXT)

    def _on_hover(self, event):
        if self._enabled:
            self.itemconfig(self.rect, fill=self.hover_color)

    def _on_leave(self, event):
        if self._enabled:
            self.itemconfig(self.rect, fill=self.bg_color)

    def _on_click(self, event):
        if self._enabled:
            self.itemconfig(self.rect, fill=self.hover_color)

    def _on_release(self, event):
        if not self._enabled:
            return
        self.itemconfig(self.rect, fill=self.hover_color)
        if self.command:
            self.command()


class CustomOptionMenu(tk.Frame):
    def __init__(self, parent, var, options, width=15, command=None):
        super().__init__(parent, bg=COLOR_WHITE, bd=1, relief="solid", highlightthickness=0)
        self.var = var
        self.options = options
        self.command = command

        content_frame = tk.Frame(self, bg=COLOR_WHITE)
        content_frame.pack(fill="both", expand=True, padx=2, pady=2)

        self.label = tk.Label(content_frame, textvariable=var, bg=COLOR_WHITE, fg=COLOR_TEXT_MAIN,
                              font=FONT_MAIN, width=width, anchor="w", cursor="hand2")
        self.label.pack(side="left", fill="x", expand=True)

        self.arrow = tk.Label(content_frame, text="▼", bg=COLOR_WHITE, fg=COLOR_TEXT_SUB,
                              font=("Arial", 8), cursor="hand2")
        self.arrow.pack(side="right", padx=5)

        self.bind("<Button-1>", self._show_menu)
        content_frame.bind("<Button-1>", self._show_menu)
        self.label.bind("<Button-1>", self._show_menu)
        self.arrow.bind("<Button-1>", self._show_menu)

        self.menu = tk.Menu(self, tearoff=0, bg=COLOR_WHITE, font=FONT_MAIN)
        self._update_menu()

        var.trace_add("write", lambda *args: self.label.config(text=var.get()))

    def _update_menu(self):
        self.menu.delete(0, "end")
        for choice in self.options:
            self.menu.add_command(label=choice, command=lambda value=choice: self._select_choice(value))

    def _select_choice(self, value):
        self.var.set(value)
        if self.command:
            self.command(value)

    def _show_menu(self, event=None):
        try:
            x = self.winfo_rootx()
            y = self.winfo_rooty() + self.winfo_height()
            self.menu.post(x, y)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Delay widget (fixed or random) - rendered as a card
# ----------------------------------------------------------------------------

class DelayInput:
    """Compact single-row delay control inside a bordered card."""

    def __init__(self, parent, label, default_fixed="2", default_min="2", default_max="5"):
        self.frame = tk.Frame(parent, bg=MAC_CARD, highlightbackground=MAC_BORDER,
                              highlightcolor=MAC_BORDER, highlightthickness=1, bd=0)
        inner = tk.Frame(self.frame, bg=MAC_CARD)
        inner.pack(fill="both", expand=True, padx=12, pady=8)
        ttk.Label(inner, text=label, style="CardTitle.TLabel").pack(anchor="w", pady=(0, 4))

        self.mode = tk.StringVar(value="fixed")
        row = tk.Frame(inner, bg=MAC_CARD)
        row.pack(fill="x")

        ttk.Radiobutton(row, text="Fixed", style="Card.TRadiobutton", variable=self.mode,
                        value="fixed", command=self._sync).grid(row=0, column=0, sticky="w")
        self.fixed = ttk.Entry(row, width=5)
        self.fixed.insert(0, default_fixed)
        self.fixed.grid(row=0, column=1, padx=(6, 3))
        ttk.Label(row, text="sec", style="CardSub.TLabel").grid(row=0, column=2, sticky="w")

        ttk.Radiobutton(row, text="Random", style="Card.TRadiobutton", variable=self.mode,
                        value="random", command=self._sync).grid(row=0, column=3, sticky="w", padx=(16, 0))
        self.rmin = ttk.Entry(row, width=5)
        self.rmin.insert(0, default_min)
        self.rmin.grid(row=0, column=4, padx=(6, 2))
        ttk.Label(row, text="to", style="CardSub.TLabel").grid(row=0, column=5)
        self.rmax = ttk.Entry(row, width=5)
        self.rmax.insert(0, default_max)
        self.rmax.grid(row=0, column=6, padx=(2, 3))
        ttk.Label(row, text="sec", style="CardSub.TLabel").grid(row=0, column=7, sticky="w")

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

_MODIFIER_KEYSYMS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R",
    "Alt_L", "Alt_R", "Win_L", "Win_R", "Super_L", "Super_R",
    "Caps_Lock", "Num_Lock", "Scroll_Lock",
}

_KEYSYM_MAP = {
    "next": "page down", "prior": "page up",
    "return": "enter", "escape": "esc",
    "space": "space", "delete": "delete", "insert": "insert",
    "home": "home", "end": "end",
}


def _keysym_to_key(event):
    ks = event.keysym
    if ks in _MODIFIER_KEYSYMS:
        return ""
    name = ks.lower()
    return _KEYSYM_MAP.get(name, name)


class KeyRecorder:
    PLACEHOLDER = "Click, then press a key"

    def __init__(self, parent, on_remove, key=""):
        self.frame = tk.Frame(parent, bg=MAC_CARD)
        self.key = key
        self.recording = False
        self.var = tk.StringVar(value=(key if key else self.PLACEHOLDER))

        self.entry = ttk.Entry(self.frame, textvariable=self.var, width=22, justify="center")
        self.entry.pack(side="left", padx=(0, 6))
        self.entry.bind("<Button-1>", self._begin_record)
        self.entry.bind("<FocusIn>", self._begin_record)
        self.entry.bind("<FocusOut>", self._end_record)
        self.entry.bind("<KeyPress>", self._on_key)

        ttk.Button(self.frame, text="Record", width=7, command=self._focus).pack(side="left", padx=(0, 6))
        ttk.Button(self.frame, text="✕", width=3, command=lambda: on_remove(self)).pack(side="left")

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
        if self.recording:
            name = _keysym_to_key(event)
            if name:
                self.key = name
                self.var.set(name)
                self.recording = False
                self.frame.focus_set()
        return "break"

    def get_key(self):
        return self.key

    def pack(self, **kw):
        self.frame.pack(**kw)

    def destroy(self):
        self.frame.destroy()


# ----------------------------------------------------------------------------
# TAB 1 - Chrome Sequencer
# ----------------------------------------------------------------------------

class ChromeTab:
    def __init__(self, parent, root, global_save=None, on_done=None):
        self.parent = parent
        self.root = root
        self.global_save = global_save
        self.on_done = on_done

        self.stop_event = threading.Event()
        self.worker = None
        self.hotkey_handle = None
        # When True, the abort hotkey also starts the sequence if it isn't
        # running (same key toggles start/stop). Set from the Application tab.
        self.hotkey_toggle_start = False

        self.chrome_exe = find_chrome_exe()
        self.profiles = list_profiles()
        self.profile_vars = {}
        self.scroll_canvas = None

        self._build_ui()
        self._load_settings()

    def _card(self, parent, title):
        return make_card(parent, title)

    def _build_ui(self):
        outer = tk.Canvas(self.parent, highlightthickness=0, bg=MAC_BG)
        vsb = MiniScrollbar(self.parent, command=outer.yview, bg=MAC_BG)
        outer.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y", padx=(2, 0))
        outer.pack(side="left", fill="both", expand=True)
        self.scroll_canvas = outer
        body = ttk.Frame(outer, padding=(14, 10))
        body_win = outer.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: outer.configure(scrollregion=outer.bbox("all")))
        outer.bind("<Configure>", lambda e: outer.itemconfigure(body_win, width=e.width))

        ttk.Label(body, text="Chrome Sequencer", style="H1.TLabel").pack(anchor="w")
        ttk.Label(body, text="Open Chrome profiles, navigate them, and send keystrokes.",
                  style="Sub.TLabel").pack(anchor="w", pady=(1, 10))

        # Target card (full width)
        tgt = self._card(body, "Target")
        tgt.pack(fill="x", pady=(0, 8))
        trow1 = tk.Frame(tgt.body, bg=MAC_CARD); trow1.pack(fill="x", pady=(0, 2))
        ttk.Label(trow1, text="URLs", style="Card.TLabel", width=11).pack(side="left", anchor="n")
        urlbox = tk.Frame(trow1, bg=MAC_CARD)
        urlbox.pack(side="left", fill="x", expand=True, padx=6)
        self.url_text = tk.Text(urlbox, height=3, font=FONT_BODY, relief="flat",
                                bg=MAC_CARD, fg=MAC_TEXT, insertbackground=MAC_TEXT,
                                highlightbackground=MAC_BORDER, highlightcolor=MAC_ACCENT,
                                highlightthickness=1, bd=0, padx=6, pady=4, wrap="none")
        self.url_text.insert("1.0", "https://")
        self.url_text.pack(fill="x", expand=True)
        ttk.Label(tgt.body, text="One URL per line — assigned to profiles in order (wraps if fewer URLs).",
                  style="CardSub.TLabel").pack(anchor="w", pady=(2, 6))
        trow2 = tk.Frame(tgt.body, bg=MAC_CARD); trow2.pack(fill="x")
        ttk.Label(trow2, text="Chrome path", style="Card.TLabel", width=11).pack(side="left")
        self.chrome_entry = ttk.Entry(trow2)
        self.chrome_entry.insert(0, self.chrome_exe)
        self.chrome_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(trow2, text="Browse", width=8, command=self._browse_chrome).pack(side="left")

        # Two-column grid
        grid = ttk.Frame(body)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1, uniform="col")
        grid.columnconfigure(1, weight=1, uniform="col")
        left = ttk.Frame(grid); left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        right = ttk.Frame(grid); right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        # LEFT column - compact single-row cards
        kf = self._card(left, "Keys to send per tab")
        kf.pack(fill="x", pady=(0, 8))
        krow = tk.Frame(kf.body, bg=MAC_CARD); krow.pack(fill="x")
        self.send_m = tk.BooleanVar(value=True)
        self.send_c = tk.BooleanVar(value=True)
        ttk.Checkbutton(krow, text='"m"', style="Card.TCheckbutton", variable=self.send_m).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(krow, text='"c"', style="Card.TCheckbutton", variable=self.send_c).pack(side="left", padx=(0, 16))
        ttk.Label(krow, text="Delay", style="CardSub.TLabel").pack(side="left")
        self.key_delay = ttk.Entry(krow, width=5)
        self.key_delay.insert(0, "0.3")
        self.key_delay.pack(side="left", padx=5)
        ttk.Label(krow, text="sec", style="CardSub.TLabel").pack(side="left")

        self.d_initial = DelayInput(left, "Initial wait", "3", "3", "6")
        self.d_initial.pack(fill="x", pady=(0, 8))
        self.d_ext = DelayInput(left, "Wait after opening profile (extensions / VPN)", "10", "8", "15")
        self.d_ext.pack(fill="x", pady=(0, 8))
        self.d_afterurl = DelayInput(left, "Wait after typing URL", "5", "4", "8")
        self.d_afterurl.pack(fill="x", pady=(0, 8))
        self.d_pertab = DelayInput(left, "Delay between profiles", "2", "2", "5")
        self.d_pertab.pack(fill="x", pady=(0, 8))

        # RIGHT column
        pf = self._card(right, "Chrome profiles")
        pf.pack(fill="both", expand=True, pady=(0, 8))
        ptop = tk.Frame(pf.body, bg=MAC_CARD); ptop.pack(fill="x", pady=(0, 4))
        ttk.Button(ptop, text="All", width=6, command=lambda: self._set_all(True)).pack(side="left", padx=(0, 4))
        ttk.Button(ptop, text="None", width=6, command=lambda: self._set_all(False)).pack(side="left")
        self.tile_grid = tk.BooleanVar(value=True)
        ttk.Checkbutton(ptop, text="Tile windows in a grid", style="Card.TCheckbutton",
                        variable=self.tile_grid).pack(side="left", padx=(14, 0))
        self.load_ext = tk.BooleanVar(value=True)
        ttk.Checkbutton(ptop, text="Load helper extension", style="Card.TCheckbutton",
                        variable=self.load_ext).pack(side="left", padx=(14, 0))
        pwrap = tk.Frame(pf.body, bg=MAC_CARD); pwrap.pack(fill="both", expand=True)
        pcanvas = tk.Canvas(pwrap, height=150, highlightthickness=0, bg=MAC_CARD)
        pscroll = MiniScrollbar(pwrap, command=pcanvas.yview, bg=MAC_CARD)
        pinner = tk.Frame(pcanvas, bg=MAC_CARD)
        pinner.bind("<Configure>", lambda e: pcanvas.configure(scrollregion=pcanvas.bbox("all")))
        pcanvas.create_window((0, 0), window=pinner, anchor="nw")
        pcanvas.configure(yscrollcommand=pscroll.set)
        pcanvas.pack(side="left", fill="both", expand=True)
        pscroll.pack(side="right", fill="y", padx=(2, 0))
        if not self.profiles:
            ttk.Label(pinner, text="No Chrome profiles found.", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        pinner.grid_columnconfigure(0, weight=1, uniform="p")
        pinner.grid_columnconfigure(1, weight=1, uniform="p")
        for i, (directory, name) in enumerate(self.profiles):
            var = tk.BooleanVar(value=True)
            self.profile_vars[directory] = var
            ttk.Checkbutton(pinner, text=name, style="Card.TCheckbutton",
                            variable=var).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 14), pady=1)

        # Final key presses - collapsible (only expands when enabled)
        fk = self._card(right, "Final key presses")
        fk.pack(fill="x")
        frow = tk.Frame(fk.body, bg=MAC_CARD); frow.pack(fill="x")
        self.final_keys_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(frow, text="Enable", style="Card.TCheckbutton",
                        variable=self.final_keys_enabled,
                        command=self._toggle_final_panel).pack(side="left")
        self.fk_delay_row = tk.Frame(frow, bg=MAC_CARD)
        ttk.Label(self.fk_delay_row, text="Delay", style="CardSub.TLabel").pack(side="left", padx=(16, 4))
        self.final_key_delay = ttk.Entry(self.fk_delay_row, width=5)
        self.final_key_delay.insert(0, "0.5")
        self.final_key_delay.pack(side="left")
        ttk.Label(self.fk_delay_row, text="sec between keys", style="CardSub.TLabel").pack(side="left", padx=(4, 0))

        self.fk_panel = tk.Frame(fk.body, bg=MAC_CARD)
        self.final_keys_inner = tk.Frame(self.fk_panel, bg=MAC_CARD)
        self.final_keys_inner.pack(fill="x", pady=(6, 0))
        ttk.Button(self.fk_panel, text="+ Add key", command=self._add_final_key).pack(anchor="w", pady=(6, 0))
        self.final_key_recorders = []

        # Bottom action bar
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(12, 0))
        self.start_btn = RoundedButton(bar, 150, 38, "Start Sequence", command=self._start, radius=10,
                                       bg_color=MAC_ACCENT, hover_color=MAC_ACCENT_HOVER, text_color="#FFFFFF")
        self.start_btn.pack(side="left")
        RoundedButton(bar, 90, 38, "Abort", command=self._abort, radius=10,
                      bg_color=MAC_DANGER, hover_color=MAC_DANGER_HOVER, text_color="#FFFFFF").pack(side="left", padx=8)
        self.status_label = ttk.Label(bar, text="", style="Sub.TLabel")
        self.status_label.pack(side="left", padx=12)
        self.save_btn = RoundedButton(bar, 110, 38, "Save", command=self._save_clicked, radius=10,
                                      bg_color=MAC_CONTROL_MUTED, hover_color=MAC_CONTROL_HOVER, text_color=MAC_TEXT)
        self.save_btn.pack(side="right")

    # ---- settings ----
    def _get_urls(self):
        """URLs from the multi-line box: one per line, stripped, non-empty."""
        raw = self.url_text.get("1.0", "end-1c")
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _collect_settings(self):
        return {
            "urls": self._get_urls(),
            "chrome": self.chrome_entry.get(),
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
            "tile_grid": self.tile_grid.get(),
            "load_extension": self.load_ext.get(),
            "final_keys_enabled": self.final_keys_enabled.get(),
            "final_key_delay": self.final_key_delay.get(),
            "final_keys": [r.get_key() for r in self.final_key_recorders if r.get_key()],
        }

    def _load_settings(self):
        cfg = load_config()
        if not cfg:
            return
        # URLs: new "urls" list, or legacy single "url" string.
        urls = cfg.get("urls")
        if urls is None and "url" in cfg:
            urls = [cfg["url"]] if cfg["url"] else []
        if urls is not None:
            self.url_text.delete("1.0", "end")
            self.url_text.insert("1.0", "\n".join(urls))
        if cfg.get("chrome"):
            self.chrome_entry.delete(0, "end"); self.chrome_entry.insert(0, cfg["chrome"])
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
        if "tile_grid" in cfg:
            self.tile_grid.set(bool(cfg["tile_grid"]))
        if "load_extension" in cfg:
            self.load_ext.set(bool(cfg["load_extension"]))
        if "final_keys_enabled" in cfg:
            self.final_keys_enabled.set(bool(cfg["final_keys_enabled"]))
        if "final_key_delay" in cfg:
            self.final_key_delay.delete(0, "end")
            self.final_key_delay.insert(0, str(cfg["final_key_delay"]))
        for k in cfg.get("final_keys", []):
            if k:
                self._add_final_key(k)
        self._toggle_final_panel()

    def _save_clicked(self):
        if self.global_save:
            self.global_save()

    def _browse_chrome(self):
        p = filedialog.askopenfilename(filetypes=[("chrome.exe", "chrome.exe"), ("Executable", "*.exe")])
        if p:
            self.chrome_entry.delete(0, "end"); self.chrome_entry.insert(0, p)

    def _set_all(self, val):
        for v in self.profile_vars.values():
            v.set(val)

    def _toggle_final_panel(self):
        if self.final_keys_enabled.get():
            self.fk_delay_row.pack(side="left")
            self.fk_panel.pack(fill="x")
        else:
            self.fk_delay_row.pack_forget()
            self.fk_panel.pack_forget()

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

    def _log(self, msg):
        self.root.after(0, lambda: self.status_label.config(text=msg))

    # ---- hotkey (widget lives on the Application tab; logic stays here) ----
    def _register_hotkey(self, combo):
        if self.hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self.hotkey_handle)
            except Exception:
                pass
            self.hotkey_handle = None
        if not combo:
            return
        try:
            self.hotkey_handle = keyboard.add_hotkey(combo, self._hotkey_fired)
        except Exception:
            pass

    def _hotkey_fired(self):
        running = bool(self.worker and self.worker.is_alive())
        if running:
            self.stop_event.set()
            self._log("Hotkey pressed - stopping sequence.")
        elif self.hotkey_toggle_start:
            # Same key also starts the sequence when nothing is running.
            self._log("Hotkey pressed - starting sequence.")
            self.root.after(0, self._start)
        else:
            self._log("Hotkey pressed - no sequence running.")

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
        urls = self._get_urls()
        if not urls or urls == ["https://"]:
            if auto:
                self._log("Auto-run: no URLs entered, skipping.")
                return
            if not messagebox.askyesno("URLs", "No URLs entered. Continue anyway?"):
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

        if self.global_save:
            self.global_save(flash=False)
        self.stop_event.clear()
        self.start_btn.set_enabled(False)
        self.worker = threading.Thread(
            target=self._run_sequence,
            args=(chrome, urls, selected, self.send_m.get(), self.send_c.get(),
                  key_delay, final_keys, final_delay, self.tile_grid.get(),
                  self.load_ext.get()),
            daemon=True,
        )
        self.worker.start()

    def _abort(self):
        self.stop_event.set()
        self._log("Abort requested - stopping sequence.")

    def _run_sequence(self, chrome, urls, profiles, send_m, send_c, key_delay,
                      final_keys=None, final_delay=0.5, tile_grid=False, load_ext=False):
        try:
            keys = []
            if send_m:
                keys.append("m")
            if send_c:
                keys.append("c")

            urls = list(urls) if urls else [""]

            # Optionally load the unpacked helper extension. Note: Chrome only
            # honors this when it starts the browser process (i.e. that profile's
            # Chrome isn't already running); the first window we open sets it.
            ext_args = []
            if load_ext:
                ep = extension_dir()
                if os.path.isdir(ep):
                    ext_args = [f"--load-extension={ep}"]
                else:
                    self._log("Helper extension folder not found next to the app.")

            s = self.d_initial.seconds()
            self._log(f"Initial wait {s:.1f}s")
            self._sleep(s)

            # Per-profile pipeline: fully process one profile before the next.
            opened_hwnds = []
            total = len(profiles)
            for idx, directory in enumerate(profiles):
                if self.stop_event.is_set():
                    raise KeyboardInterrupt

                # 1) Open one profile window.
                self._log(f"[{idx + 1}/{total}] Opening [{directory}]...")
                before = chrome_window_handles()
                subprocess.Popen(
                    [chrome, f"--profile-directory={directory}", "--new-window"]
                    + ext_args + ["about:blank"]
                )
                hwnd = self._wait_for_new_window(before, timeout=15)
                if not hwnd:
                    self._log(f"WARNING: window not detected for [{directory}], skipping")
                    continue
                opened_hwnds.append(hwnd)
                if tile_grid:
                    tile_windows(opened_hwnds)

                # 2) Wait for extensions / VPN to come up.
                s = self.d_ext.seconds()
                self._log(f"[{directory}] wait (extensions/VPN) {s:.1f}s")
                self._sleep(s)

                # 3) Type the URL (assigned sequentially, wrapping if fewer
                #    URLs than profiles).
                if self.stop_event.is_set():
                    raise KeyboardInterrupt
                url = urls[idx % len(urls)]
                force_foreground(hwnd)
                self._sleep(0.4)
                keyboard.send("ctrl+l")
                self._sleep(0.2)
                keyboard.write(url, delay=0.01)
                self._sleep(0.1)
                keyboard.send("enter")
                self._log(f"[{directory}] navigated to {url or '(blank)'}")

                # 4) Wait after typing the URL.
                s = self.d_afterurl.seconds()
                self._log(f"[{directory}] wait after URL {s:.1f}s")
                self._sleep(s)

                # 5) Send the per-profile keys ("m"/"c").
                if self.stop_event.is_set():
                    raise KeyboardInterrupt
                force_foreground(hwnd)
                self._sleep(0.3)
                for i, k in enumerate(keys):
                    if self.stop_event.is_set():
                        raise KeyboardInterrupt
                    keyboard.send(k)
                    if i < len(keys) - 1:
                        self._sleep(key_delay)
                if keys:
                    self._log(f"[{directory}] sent keys: {keys}")

                # 6) Delay before moving on to the next profile.
                if idx < total - 1:
                    s = self.d_pertab.seconds()
                    self._log(f"Delay before next profile {s:.1f}s")
                    self._sleep(s)

            if final_keys:
                self._log(f"Final key presses: {final_keys}")
                self._sleep(1.0)
                for i, k in enumerate(final_keys):
                    if self.stop_event.is_set():
                        raise KeyboardInterrupt
                    try:
                        keyboard.send(k)
                    except Exception as e:
                        self._log(f"failed to send {k}: {e}")
                    if i < len(final_keys) - 1:
                        self._sleep(final_delay)

            self._log("Done.")
            self.root.after(0, self._sequence_finished)

        except KeyboardInterrupt:
            self._log("Aborted.")
            self.root.after(0, lambda: self.start_btn.set_enabled(True))
        except Exception as e:
            self._log(f"Error: {e}")
            self.root.after(0, lambda: self.start_btn.set_enabled(True))

    def _sequence_finished(self):
        self.start_btn.set_enabled(True)
        if self.on_done:
            self.on_done()

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


# ----------------------------------------------------------------------------
# TAB 3 - Application (startup, hotkey, updates, config)
# ----------------------------------------------------------------------------

class ApplicationTab:
    def __init__(self, parent, root, chrome, global_save=None):
        self.parent = parent
        self.root = root
        self.chrome = chrome
        self.global_save = global_save
        self._build_ui()
        self._load_settings()
        self._register_hotkey_from_field()

    def _card(self, parent, title):
        return make_card(parent, title)

    def _build_ui(self):
        body = ttk.Frame(self.parent, padding=(18, 16))
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Application", style="H1.TLabel").pack(anchor="w")
        ttk.Label(body, text="App-wide settings, abort hotkey, and updates.",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 14))

        grid = ttk.Frame(body)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1, uniform="col")
        grid.columnconfigure(1, weight=1, uniform="col")
        left = ttk.Frame(grid); left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right = ttk.Frame(grid); right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        # Startup
        sc = self._card(left, "Startup")
        sc.pack(fill="x", pady=(0, 12))
        self.run_on_startup = tk.BooleanVar(value=is_run_on_startup())
        ttk.Checkbutton(sc.body, text="Launch this app when Windows starts", style="Card.TCheckbutton",
                        variable=self.run_on_startup, command=self._toggle_startup).pack(anchor="w")
        self.autorun = tk.BooleanVar(value=False)
        ttk.Checkbutton(sc.body, text="Run the sequence automatically when the app launches",
                        style="Card.TCheckbutton", variable=self.autorun).pack(anchor="w", pady=(6, 0))

        # Abort hotkey
        hc = self._card(left, "Start / Abort hotkey")
        hc.pack(fill="x", pady=(0, 12))
        hrow = tk.Frame(hc.body, bg=MAC_CARD); hrow.pack(fill="x")
        self.hotkey_entry = ttk.Entry(hrow, width=20)
        self.hotkey_entry.insert(0, "ctrl+shift+q")
        self.hotkey_entry.pack(side="left")
        ttk.Button(hrow, text="Set", width=6, command=self._apply_hotkey).pack(side="left", padx=6)
        self.hotkey_toggle = tk.BooleanVar(value=False)
        ttk.Checkbutton(hc.body, text="Also use this hotkey to start the sequence (toggle start/stop)",
                        style="Card.TCheckbutton", variable=self.hotkey_toggle,
                        command=self._apply_hotkey_toggle).pack(anchor="w", pady=(6, 0))
        ttk.Label(hc.body, text="Press once to start; press again to stop a running sequence.",
                  style="CardSub.TLabel").pack(anchor="w", pady=(4, 0))

        # Updates
        uc = self._card(right, "Updates")
        uc.pack(fill="x", pady=(0, 12))
        ttk.Label(uc.body, text=f"Version {__version__}", style="Card.TLabel").pack(anchor="w")
        ttk.Button(uc.body, text="Check for updates", command=self._check_updates).pack(anchor="w", pady=(8, 0))

        # Configuration
        cc = self._card(right, "Configuration")
        cc.pack(fill="x")
        crow = tk.Frame(cc.body, bg=MAC_CARD); crow.pack(fill="x")
        ttk.Button(crow, text="Export", width=10, command=self._export).pack(side="left", padx=(0, 6))
        ttk.Button(crow, text="Import", width=10, command=self._import).pack(side="left")
        ttk.Label(cc.body, text="Backup or restore your settings (config.json).",
                  style="CardSub.TLabel").pack(anchor="w", pady=(6, 0))

        # Save bar
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(14, 0))
        self.status_label = ttk.Label(bar, text="", style="Sub.TLabel")
        self.status_label.pack(side="left")
        self.save_btn = RoundedButton(bar, 120, 40, "Save", command=self._save_clicked, radius=10,
                                      bg_color=MAC_CONTROL_MUTED, hover_color=MAC_CONTROL_HOVER, text_color=MAC_TEXT)
        self.save_btn.pack(side="right")

    def _collect_settings(self):
        return {
            "hotkey": self.hotkey_entry.get(),
            "hotkey_toggle": self.hotkey_toggle.get(),
            "run_on_startup": self.run_on_startup.get(),
            "autorun": self.autorun.get(),
        }

    def _load_settings(self):
        cfg = load_config()
        if cfg.get("hotkey"):
            self.hotkey_entry.delete(0, "end"); self.hotkey_entry.insert(0, cfg["hotkey"])
        if "hotkey_toggle" in cfg:
            self.hotkey_toggle.set(bool(cfg["hotkey_toggle"]))
        self.chrome.hotkey_toggle_start = self.hotkey_toggle.get()
        if "autorun" in cfg:
            self.autorun.set(bool(cfg["autorun"]))
        if "run_on_startup" in cfg:
            want = bool(cfg["run_on_startup"])
            if want != is_run_on_startup():
                set_run_on_startup(want)
            self.run_on_startup.set(want)

    def _register_hotkey_from_field(self):
        self.chrome._register_hotkey(self.hotkey_entry.get().strip().lower())

    def _apply_hotkey(self):
        combo = self.hotkey_entry.get().strip().lower()
        self.chrome._register_hotkey(combo)
        self._flash(f"Hotkey set: {combo}")

    def _apply_hotkey_toggle(self):
        self.chrome.hotkey_toggle_start = self.hotkey_toggle.get()
        self._flash("Start hotkey " + ("enabled" if self.hotkey_toggle.get() else "disabled"))

    def _toggle_startup(self):
        ok = set_run_on_startup(self.run_on_startup.get())
        if ok:
            self._flash("Startup " + ("enabled" if self.run_on_startup.get() else "disabled"))
        else:
            self._flash("Could not update startup setting.")
            self.run_on_startup.set(is_run_on_startup())

    def _check_updates(self):
        if not check_for_update():
            messagebox.showinfo("Up to date", f"You're on the latest version (v{__version__}).")
            return
        if run_update_flow(self.root):
            os._exit(0)

    def _export(self):
        if self.global_save:
            self.global_save(flash=False)
        fn = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if fn:
            try:
                shutil.copyfile(CONFIG_PATH, fn)
                self._flash("Exported.")
            except Exception as e:
                messagebox.showerror("Export", str(e))

    def _import(self):
        fn = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if fn:
            try:
                with open(fn, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                messagebox.showerror("Import", f"Not valid JSON: {e}")
                return
            save_config(data)
            messagebox.showinfo("Import", "Imported. Restart the app to apply.")

    def _save_clicked(self):
        if self.global_save:
            self.global_save()

    def _flash(self, msg):
        self.status_label.config(text=msg)
        self.root.after(2500, lambda: self.status_label.config(text=""))


# ----------------------------------------------------------------------------
# TAB 2 - Typer (adapted from TyperV9)
# ----------------------------------------------------------------------------

class TyperTab:
    def __init__(self, parent, root, global_save=None):
        self.parent = parent
        self.root = root
        self.global_save = global_save

        self.current_group_name = None
        self.editor_loaded = False

        self.groups = self._load_config()
        if not self.groups:
            self._create_default_group()

        self.current_group_name = next(iter(self.groups)) if self.groups else None

        self.hotkey_lock = threading.Lock()
        self._hotkey_handles = []
        self.editor_hotkey_label = None
        self.editor_step_entries = []

        self.ctrl_var = None
        self.shift_var = None
        self.loop_enabled_var = None
        self.loop_count_var = None

        self.step_iterators = {}
        self.running_events = {}
        self.macro_monitor_window = None

        self._setup_ui()
        self._load_group_to_editor()
        self._setup_all_hotkeys()

    def _create_default_group(self):
        default_name = "Group A: New Profile"
        self.groups[default_name] = {
            "hotkey": "",
            "switch_key": "alt+esc",
            "loop_enabled": False,
            "loop_count": 0,
            "steps": [{"msg": "", "delay": "1-2", "multi_enabled": False, "multi_mode": "Sequential"}],
        }
        return default_name

    def _load_config(self):
        if os.path.exists(STREAM_CONFIG_PATH):
            try:
                with open(STREAM_CONFIG_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except json.JSONDecodeError as e:
                messagebox.showerror("Error", f"Failed to load config (JSON format error): {e}")
                return {}
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load config (File/Encoding error): {e}")
                return {}

            processed_data = {}
            try:
                for name, group in data.items():
                    if "loop_enabled" not in group: group["loop_enabled"] = False
                    if "loop_count" not in group: group["loop_count"] = 0

                    for step in group.get("steps", []):
                        if "multi_enabled" not in step: step["multi_enabled"] = False
                        if "multi_mode" not in step: step["multi_mode"] = "Sequential"
                        if not step.get('delay'): step['delay'] = '1-2'

                    processed_data[name] = group

                if processed_data:
                    return processed_data

            except Exception as e:
                messagebox.showerror("Error", f"Failed to process config data structure. Error details: {e}")
                return {}

        return {}

    def _save_config(self):
        if self.groups:
            try:
                with open(STREAM_CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump(self.groups, f, indent=4, ensure_ascii=False)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to save: {e}")

    def persist(self):
        self._save_editor_to_group()
        self._save_config()

    # --- UI CONSTRUCTION ---

    def _setup_ui(self):
        self.main_container = tk.Frame(self.parent, bg=COLOR_BG)
        self.main_container.pack(fill="both", expand=True, padx=24, pady=16)

        top_row = tk.Frame(self.main_container, bg=COLOR_BG)
        top_row.pack(fill="x", anchor="w", pady=(0, 2))

        tk.Label(top_row, text="Typer", font=FONT_H1, bg=COLOR_BG, fg=COLOR_ACCENT).pack(side="left", anchor="w")

        self.menu_btn = RoundedButton(top_row, 34, 30, "☰", command=self._show_hamburger_menu, radius=8)
        self.menu_btn.pack(side="right", padx=(10, 0))

        self.status_btn = RoundedButton(top_row, 160, 30, "Show Active Macros", command=self._show_active_macros_window,
                                        radius=8, bg_color=COLOR_ACCENT, hover_color=MAC_ACCENT_HOVER, text_color="#FFFFFF")
        self.status_btn.pack(side="right", padx=10)

        self.hamburger_menu = tk.Menu(self.root, tearoff=0, bg=COLOR_WHITE, font=FONT_MAIN)
        self.hamburger_menu.add_command(label="Export Groups", command=self._export_config)
        self.hamburger_menu.add_command(label="Import Groups", command=self._import_config)

        tk.Label(self.main_container, text="Stream-chat message sequencer. Each profile owns a hotkey.",
                 font=FONT_SUB, bg=COLOR_BG, fg=COLOR_TEXT_SUB).pack(anchor="w", pady=(2, 14))

        group_frame = tk.Frame(self.main_container, bg=COLOR_BG)
        group_frame.pack(fill="x", anchor="w", pady=(0, 16))

        self.group_options = list(self.groups.keys())
        self.group_var = tk.StringVar(self.root)
        if self.current_group_name:
            self.group_var.set(self.current_group_name)

        self.group_dropdown = CustomOptionMenu(group_frame, self.group_var, self.group_options,
                                               width=30, command=self._switch_group)
        self.group_dropdown.pack(side="left", padx=(0, 20))

        RoundedButton(group_frame, 60, 30, "New", command=self._add_group, radius=8).pack(side="left", padx=5)
        RoundedButton(group_frame, 80, 30, "Rename", command=self._rename_group, radius=8).pack(side="left", padx=5)
        RoundedButton(group_frame, 70, 30, "Delete", command=self._delete_group, radius=8).pack(side="left", padx=5)

        # Settings inside a card
        settings_card = tk.Frame(self.main_container, bg=COLOR_WHITE, highlightbackground=COLOR_BORDER_LIGHT,
                                 highlightcolor=COLOR_BORDER_LIGHT, highlightthickness=1, bd=0)
        settings_card.pack(fill="x", anchor="w")
        self.settings_grid = tk.Frame(settings_card, bg=COLOR_WHITE)
        self.settings_grid.pack(anchor="w", fill="x", padx=14, pady=12)

        tk.Frame(self.main_container, height=16, bg=COLOR_BG).pack()

        step_header_frame = tk.Frame(self.main_container, bg=COLOR_BG)
        step_header_frame.pack(fill="x", anchor="w")
        tk.Label(step_header_frame, text="Message Sequence", font=FONT_TITLE, bg=COLOR_BG, fg=COLOR_ACCENT).pack(side="left")

        list_border_frame = tk.Frame(self.main_container, bg=COLOR_BORDER_LIGHT, bd=1)
        list_border_frame.pack(fill="both", expand=True, pady=(6, 10), anchor="w")

        self.canvas = tk.Canvas(list_border_frame, bg=COLOR_WHITE, highlightthickness=0)
        self.scrollbar = MiniScrollbar(list_border_frame, command=self.canvas.yview, bg=COLOR_WHITE)
        self.scrollable_frame = tk.Frame(self.canvas, bg=COLOR_WHITE)

        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y", padx=(2, 0))

        footer_frame = tk.Frame(self.main_container, bg=COLOR_BG)
        footer_frame.pack(fill="x", pady=10)

        RoundedButton(footer_frame, 120, 35, "+ Add Step", command=lambda: self._add_step_row(len(self.editor_step_entries) + 1), radius=10).pack(side="left")

        right_footer = tk.Frame(footer_frame, bg=COLOR_BG)
        right_footer.pack(side="right")

        self.status_label = tk.Label(right_footer, text="Ready", fg=COLOR_TEXT_SUB, bg=COLOR_BG, font=FONT_SUB)
        self.status_label.pack(side="left", padx=15)

        self.save_button = RoundedButton(right_footer, 130, 40, "Save", command=self._save_clicked,
                                         radius=10, bg_color=MAC_ACCENT, hover_color=MAC_ACCENT_HOVER, text_color="#FFFFFF")
        self.save_button.pack(side="left")

    def _save_clicked(self):
        if self.global_save:
            self.global_save()

    def _show_hamburger_menu(self):
        try:
            btn_x = self.menu_btn.winfo_rootx()
            btn_w = self.menu_btn.winfo_width()
            menu_w = 150
            x = btn_x - menu_w + btn_w
            y = self.menu_btn.winfo_rooty() + self.menu_btn.winfo_height()
            self.hamburger_menu.post(x, y)
        except Exception:
            pass

    def _show_active_macros_window(self):
        if self.macro_monitor_window and self.macro_monitor_window.winfo_exists():
            self.macro_monitor_window.lift()
            return

        self.macro_monitor_window = tk.Toplevel(self.root)
        self.macro_monitor_window.title("Active Macro Status")
        self.macro_monitor_window.geometry("350x300")
        self.macro_monitor_window.configure(bg=COLOR_WHITE)
        self.macro_monitor_window.resizable(False, False)

        tk.Label(self.macro_monitor_window, text="Active Macro Status", font=FONT_TITLE, bg=COLOR_WHITE, fg=COLOR_TEXT_MAIN, padx=10, pady=10).pack(fill="x")

        monitor_frame = tk.Frame(self.macro_monitor_window, bg=COLOR_WHITE)
        monitor_frame.pack(fill="both", expand=True, padx=10, pady=5)

        tk.Label(monitor_frame, text="Group", font=FONT_SUB, bg=COLOR_WHITE, fg=COLOR_TEXT_SUB, anchor="w", width=20).grid(row=0, column=0, sticky="w")
        tk.Label(monitor_frame, text="Hotkey", font=FONT_SUB, bg=COLOR_WHITE, fg=COLOR_TEXT_SUB, anchor="w", width=10).grid(row=0, column=1, sticky="w")
        tk.Label(monitor_frame, text="Status", font=FONT_SUB, bg=COLOR_WHITE, fg=COLOR_TEXT_SUB, anchor="w").grid(row=0, column=2, sticky="w")

        self.macro_status_labels = {}
        row_num = 1

        for name, data in self.groups.items():
            hotkey_str = data.get('hotkey', 'N/A')
            if hotkey_str == "": hotkey_str = "N/A"

            tk.Label(monitor_frame, text=name, font=FONT_MAIN, bg=COLOR_WHITE, fg=COLOR_TEXT_MAIN, anchor="w").grid(row=row_num, column=0, sticky="w", pady=2)
            tk.Label(monitor_frame, text=hotkey_str.upper(), font=FONT_MONITOR, bg=COLOR_WHITE, fg=COLOR_TEXT_MAIN, anchor="w").grid(row=row_num, column=1, sticky="w")

            status_label = tk.Label(monitor_frame, text="INACTIVE", font=FONT_MAIN, bg=COLOR_WHITE, fg=COLOR_STATUS_INACTIVE, anchor="w")
            status_label.grid(row=row_num, column=2, sticky="w", padx=10)

            self.macro_status_labels[name] = status_label
            row_num += 1

        self._update_macro_status()

    def _update_macro_status(self):
        if not self.macro_monitor_window or not self.macro_monitor_window.winfo_exists():
            return

        for name, label in self.macro_status_labels.items():
            ev = self.running_events.get(name)
            is_active = getattr(ev, 'active', False) if ev else False

            if is_active:
                label.config(text="ACTIVE", fg=COLOR_STATUS_ACTIVE)
            else:
                label.config(text="INACTIVE", fg=COLOR_STATUS_INACTIVE)

        self.root.after(500, self._update_macro_status)

    def _load_group_to_editor(self):
        for widget in self.scrollable_frame.winfo_children(): widget.destroy()
        for widget in self.settings_grid.winfo_children(): widget.destroy()

        if not self.current_group_name or self.current_group_name not in self.groups: return
        data = self.groups[self.current_group_name]

        full_hotkey = data.get('hotkey', '').lower()
        parts = full_hotkey.split('+')
        self.ctrl_var = tk.BooleanVar(value='ctrl' in parts)
        self.shift_var = tk.BooleanVar(value='shift' in parts)
        base_key = next((p for p in parts if p not in ['ctrl', 'shift', 'alt', 'lcontrol', 'rcontrol', 'lshift', 'rshift', 'lalt', 'ralt', 'alt gr', '']), '')

        tk.Label(self.settings_grid, text="Hotkey Base:", font=FONT_MAIN, bg=COLOR_WHITE, fg=COLOR_TEXT_SUB).grid(row=0, column=0, sticky="w", padx=(0, 15), pady=5)

        self.editor_hotkey_label = tk.Label(self.settings_grid, text=base_key, font=(FONT_FAMILY, 11, "bold"),
                                          bg="#FFF3CD", fg="#856404", width=12, relief="flat", pady=5)
        self.editor_hotkey_label.grid(row=0, column=1, sticky="w", pady=5)
        self.editor_hotkey_label.bind("<Button-1>", self._start_hotkey_capture)

        tk.Label(self.settings_grid, text="Modifiers:", font=FONT_MAIN, bg=COLOR_WHITE, fg=COLOR_TEXT_SUB).grid(row=1, column=0, sticky="w", padx=(0, 15), pady=5)
        mod_frame = tk.Frame(self.settings_grid, bg=COLOR_WHITE)
        mod_frame.grid(row=1, column=1, sticky="w")
        tk.Checkbutton(mod_frame, text="Ctrl", variable=self.ctrl_var, bg=COLOR_WHITE, activebackground=COLOR_WHITE).pack(side="left", padx=(0, 10))
        tk.Checkbutton(mod_frame, text="Shift", variable=self.shift_var, bg=COLOR_WHITE, activebackground=COLOR_WHITE).pack(side="left")

        tk.Label(self.settings_grid, text="Switch Key:", font=FONT_MAIN, bg=COLOR_WHITE, fg=COLOR_TEXT_SUB).grid(row=2, column=0, sticky="w", padx=(0, 15), pady=10)
        self.switch_key_var = tk.StringVar(value=data.get('switch_key', 'alt+esc'))
        switch_menu = CustomOptionMenu(self.settings_grid, self.switch_key_var, ["alt+tab", "alt+esc"], width=15)
        switch_menu.grid(row=2, column=1, sticky="w")

        tk.Label(self.settings_grid, text="Loop Settings:", font=FONT_MAIN, bg=COLOR_WHITE, fg=COLOR_TEXT_SUB).grid(row=3, column=0, sticky="w", padx=(0, 15), pady=5)
        self.loop_enabled_var = tk.BooleanVar(value=data.get('loop_enabled', False))
        tk.Checkbutton(self.settings_grid, text="Loop", variable=self.loop_enabled_var, bg=COLOR_WHITE, activebackground=COLOR_WHITE).grid(row=3, column=1, sticky="w")

        tk.Label(self.settings_grid, text="Loop Count:", font=FONT_MAIN, bg=COLOR_WHITE, fg=COLOR_TEXT_SUB).grid(row=4, column=0, sticky="w", padx=(0, 15), pady=5)
        count_frame = tk.Frame(self.settings_grid, bg=COLOR_WHITE)
        count_frame.grid(row=4, column=1, sticky="w")

        self.loop_count_var = tk.Entry(count_frame, width=6, relief="solid", bd=1)
        self.loop_count_var.insert(0, str(data.get('loop_count', 0)))
        self.loop_count_var.pack(side="left")
        tk.Label(count_frame, text="(0 = Infinite)", font=FONT_SUB, bg=COLOR_WHITE, fg="#888").pack(side="left", padx=5)

        self.editor_step_entries = []
        for i, step in enumerate(data['steps']):
            delay_val = step.get('delay') if step.get('delay') else '1-2'
            message = str(step.get('msg', ''))
            self._add_step_row(i + 1, message, delay_val, step.get('multi_enabled', False), step.get('multi_mode', 'Sequential'))

        self.editor_loaded = True

    def _add_step_row(self, index, message="", delay="", multi_enabled=False, multi_mode="Sequential"):
        if not delay: delay = '1-2'

        row = tk.Frame(self.scrollable_frame, bg=COLOR_WHITE, pady=10)
        row.pack(fill="x", padx=10)

        if index > 1:
            tk.Frame(self.scrollable_frame, height=1, bg="#F0F0F0").pack(fill="x", padx=20, before=row)

        tk.Label(row, text=f"{index}.", font=(FONT_FAMILY, 14, "bold"), bg=COLOR_WHITE, fg="#CCC", width=3).pack(side="left", anchor="n")

        content_grid = tk.Frame(row, bg=COLOR_WHITE)
        content_grid.pack(side="left", fill="x", expand=True, padx=10)

        tk.Label(content_grid, text="Message", font=FONT_SUB, bg=COLOR_WHITE, fg=COLOR_TEXT_SUB).grid(row=0, column=0, sticky="w")
        tk.Label(content_grid, text="Delay Range", font=FONT_SUB, bg=COLOR_WHITE, fg=COLOR_TEXT_SUB).grid(row=0, column=1, sticky="w", padx=20)
        tk.Label(content_grid, text="Multi Line", font=FONT_SUB, bg=COLOR_WHITE, fg=COLOR_TEXT_SUB).grid(row=0, column=2, sticky="w", padx=10)
        tk.Label(content_grid, text="Delete", font=FONT_SUB, bg=COLOR_WHITE, fg=COLOR_TEXT_SUB).grid(row=0, column=3, sticky="w", padx=(30, 0))

        msg_entry = tk.Text(content_grid, height=3, font=FONT_MAIN, relief="solid", bd=1, padx=5, pady=5)
        msg_entry.delete("1.0", tk.END)
        msg_entry.insert("1.0", message)
        msg_entry.grid(row=1, column=0, sticky="w", rowspan=2, pady=(5, 0))
        content_grid.grid_columnconfigure(0, weight=1)

        delay_entry = tk.Entry(content_grid, width=10, relief="solid", bd=1)
        delay_entry.insert(0, delay)
        delay_entry.grid(row=1, column=1, sticky="w", padx=20, pady=(5, 5))

        multi_var = tk.BooleanVar(value=multi_enabled)
        tk.Checkbutton(content_grid, text="", variable=multi_var, bg=COLOR_WHITE).grid(row=1, column=2, sticky="w", padx=10, pady=(5, 5))

        del_btn = tk.Button(content_grid, text="✕", font=("Arial", 16), fg="#FF6B6B", bg=COLOR_WHITE, bd=0,
                            command=lambda r=row: self._remove_step_row(r), cursor="hand2")
        del_btn.grid(row=1, column=3, sticky="w", padx=(30, 0))

        mode_var = tk.StringVar(value=multi_mode)
        mode_dropdown = CustomOptionMenu(content_grid, mode_var, ["Sequential", "Random"], width=12)
        mode_dropdown.grid(row=2, column=2, sticky="w", padx=10)

        self.editor_step_entries.append({
            "frame": row, "msg": msg_entry, "delay": delay_entry,
            "multi_enabled": multi_var, "multi_mode": mode_var
        })
        self._reindex_rows()

    def _remove_step_row(self, row):
        self.editor_step_entries = [e for e in self.editor_step_entries if e["frame"] != row]
        row.destroy()
        self._reindex_rows()

    def _reindex_rows(self):
        for i, entry in enumerate(self.editor_step_entries):
            entry["frame"].winfo_children()[0].config(text=f"{i + 1}.")

    # --- Group Management ---

    def _update_dropdown(self):
        self.group_dropdown.options = list(self.groups.keys())
        self.group_dropdown._update_menu()

    def _switch_group(self, new_name):
        if not new_name: return

        if self.editor_loaded:
            self._save_editor_to_group()

        self.current_group_name = new_name
        self._load_group_to_editor()
        self._setup_all_hotkeys()

    def _add_group(self):
        new_name = simpledialog.askstring("New", "Profile Name:")
        if new_name and new_name not in self.groups:
            self.groups[new_name] = {
                "hotkey": "", "switch_key": "alt+esc", "loop_enabled": False, "loop_count": 0,
                "steps": [{"msg": "", "delay": "1-2", "multi_enabled": False, "multi_mode": "Sequential"}]
            }
            self._update_dropdown()
            self.group_var.set(new_name)
            self._switch_group(new_name)

    def _rename_group(self):
        if not self.current_group_name: return
        new = simpledialog.askstring("Rename", f"Rename '{self.current_group_name}' to:")
        if new and new not in self.groups:
            self.groups[new] = self.groups.pop(self.current_group_name)
            self.current_group_name = new
            self._update_dropdown()
            self.group_var.set(new)
            self._setup_all_hotkeys()

    def _delete_group(self):
        if len(self.groups) <= 1: return
        if messagebox.askyesno("Delete", "Delete this profile?"):
            del self.groups[self.current_group_name]
            self.current_group_name = next(iter(self.groups))
            self._update_dropdown()
            self.group_var.set(self.current_group_name)
            self._switch_group(self.current_group_name)

    def _export_config(self):
        self._save_config()
        fn = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if fn: shutil.copyfile(STREAM_CONFIG_PATH, fn)

    def _import_config(self):
        fn = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if fn:
            try:
                with open(fn, 'r', encoding='utf-8') as f:
                    imported_data = json.load(f)
            except json.JSONDecodeError as e:
                messagebox.showerror("Error", f"Failed to import config: The file is not valid JSON. {e}")
                return
            except Exception as e:
                messagebox.showerror("Error", f"An unexpected error occurred while reading the file: {e}")
                return

            try:
                with open(STREAM_CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump(imported_data, f, indent=4, ensure_ascii=False)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to write imported file to config path: {e}")
                return

            self.groups = self._load_config()

            if not self.groups:
                messagebox.showerror("Import Failed", "The imported file was read, but the data structure is invalid or empty.")
                self.groups = self._load_config()
                if not self.groups:
                    self._create_default_group()
                self.current_group_name = next(iter(self.groups)) if self.groups else None
                self._update_dropdown()
                self.group_var.set(self.current_group_name)
                self._load_group_to_editor()
                self._setup_all_hotkeys()
                return

            self._save_config()
            self._update_dropdown()
            first_group_name = next(iter(self.groups))
            self.group_var.set(first_group_name)
            self.current_group_name = first_group_name
            self._load_group_to_editor()
            self._setup_all_hotkeys()
            messagebox.showinfo("Success", "Configuration imported and active.")

    def _save_editor_to_group(self):
        if not self.current_group_name or not self.editor_hotkey_label:
            return

        new_steps = []
        for e in self.editor_step_entries:
            msg = e['msg'].get("1.0", "end-1c").strip()
            delay_val = e['delay'].get()

            if e['frame'].winfo_exists():
                new_steps.append({
                    "msg": msg,
                    "delay": delay_val if delay_val else '1-2',
                    "multi_enabled": e['multi_enabled'].get(),
                    "multi_mode": e['multi_mode'].get()
                })

        mods = []
        if self.ctrl_var.get(): mods.append('ctrl')
        if self.shift_var.get(): mods.append('shift')
        base = self.editor_hotkey_label.cget("text").strip().lower()
        hk = "+".join(mods + ([base] if base else []))

        try: lc = int(self.loop_count_var.get())
        except: lc = 0

        self.groups[self.current_group_name].update({
            'hotkey': hk, 'switch_key': self.switch_key_var.get(),
            'loop_enabled': self.loop_enabled_var.get(), 'loop_count': lc,
            'steps': new_steps
        })
        self.step_iterators.clear()

    # --- Hotkey and Execution ---

    def _start_hotkey_capture(self, e):
        self.editor_hotkey_label.config(text="...", bg="#FFCCCC")
        self.editor_hotkey_label.unbind("<Button-1>")
        threading.Thread(target=self._cap_key, daemon=True).start()

    def _cap_key(self):
        try:
            e = keyboard.read_event(suppress=True)
            while e.event_type != keyboard.KEY_DOWN: e = keyboard.read_event(suppress=True)
            k = e.name.lower()
            if k in ['ctrl', 'shift', 'alt', 'lcontrol', 'rcontrol', 'lshift', 'rshift']: k = ""
            self.root.after(0, lambda: self._upd_hk_lbl(k))
        except: self.root.after(0, lambda: self._upd_hk_lbl(""))

    def _upd_hk_lbl(self, k):
        self.editor_hotkey_label.config(text=k, bg="#FFF3CD")
        self.editor_hotkey_label.bind("<Button-1>", self._start_hotkey_capture)

    def _setup_all_hotkeys(self):
        with self.hotkey_lock:
            for h in self._hotkey_handles:
                try:
                    keyboard.remove_hotkey(h)
                except Exception:
                    pass
            self._hotkey_handles = []

            new_running_events = {}
            for n in self.groups.keys():
                old_ev = self.running_events.get(n)
                if old_ev and getattr(old_ev, 'active', False):
                    new_running_events[n] = old_ev
                else:
                    new_running_events[n] = threading.Event()
                    setattr(new_running_events[n], 'active', False)

            self.running_events = new_running_events

            for n, d in self.groups.items():
                hotkey_to_register = d.get('hotkey')
                if hotkey_to_register:
                    try:
                        h = keyboard.add_hotkey(hotkey_to_register, lambda n=n: self._handle_hk(n))
                        self._hotkey_handles.append(h)
                    except Exception as e:
                        print(f"Error registering hotkey for {n}: {e}")

    def _handle_hk(self, name):
        ev = self.running_events.get(name)
        if not ev: return

        if getattr(ev, 'active', False):
            ev.set()
            self.root.after(0, lambda: self.status_label.config(text=f"Stopping {name}...", fg="red"))
        else:
            ev.clear()
            setattr(ev, 'active', True)
            threading.Thread(target=self._run_seq, args=(self.groups[name], name, ev)).start()

    def _run_seq(self, data, name, ev):
        self.root.after(0, lambda: self.status_label.config(text=f"Running: {name}", fg=COLOR_ACCENT))
        steps = data['steps']
        cnt = data.get('loop_count', 0)
        loops = 1 if not data.get('loop_enabled') else (999999 if cnt == 0 else cnt)

        cur = 0
        while cur < loops and not ev.is_set():
            for i, s in enumerate(steps):
                if ev.is_set(): break
                msg = self._get_msg(name, i, s)
                if msg:
                    keyboard.write(msg); time.sleep(0.05); keyboard.send("enter")
                    d = self._parse_delay(s.get('delay', '1-2'))
                    t = time.time()
                    while time.time() - t < d:
                        if ev.is_set(): break
                        time.sleep(0.1)
                if i < len(steps) - 1 and not ev.is_set():
                    keyboard.send(data.get('switch_key', 'alt+esc')); time.sleep(0.2)
            if data.get('loop_enabled') and not ev.is_set():
                keyboard.send(data.get('switch_key', 'alt+esc')); time.sleep(0.2)
            cur += 1

        setattr(ev, 'active', False)
        self.root.after(0, lambda: self.status_label.config(text="Ready", fg=COLOR_TEXT_SUB))

    def _get_msg(self, name, idx, step):
        lines = [l for l in step['msg'].split('\n') if l.strip()]
        if not lines: return ""
        if not step['multi_enabled']: return step['msg'].strip()

        key = (name, idx)
        if step['multi_mode'] == 'Random':
            if key not in self.step_iterators: self.step_iterators[key] = self._shuf(lines)
            return next(self.step_iterators[key])
        else:
            if key not in self.step_iterators: self.step_iterators[key] = itertools.cycle(lines)
            return next(self.step_iterators[key])

    def _shuf(self, d):
        x = list(d)
        while True:
            random.shuffle(x)
            for i in x: yield i

    def _parse_delay(self, d):
        try:
            if '-' in d: a, b = map(float, d.split('-')); return random.uniform(a, b)
            return float(d)
        except: return 1.5


# ----------------------------------------------------------------------------
# Combined tabbed application shell
# ----------------------------------------------------------------------------

class CombinedApp:
    def __init__(self, root):
        self.root = root
        root.title(f"Stream Suite v{__version__}")
        root.geometry("1060x900")
        root.minsize(820, 640)
        self.style = apply_mac_theme(root)

        container = tk.Frame(root, bg=MAC_BG)
        container.pack(fill="both", expand=True, padx=16, pady=12)

        # Detached, rounded pill tab bar
        tabbar = tk.Frame(container, bg=MAC_BG)
        tabbar.pack(fill="x", pady=(0, 10))
        self.pills = {}
        for key, label in (("chrome", "Chrome Sequencer"), ("typer", "Typer"), ("app", "Application")):
            p = PillTab(tabbar, label, lambda k=key: self.select(k))
            p.pack(side="left", padx=(0, 8))
            self.pills[key] = p

        # Content area (separated from the tab bar by the gap above)
        content = tk.Frame(container, bg=MAC_BG)
        content.pack(fill="both", expand=True)
        self.chrome_frame = tk.Frame(content, bg=MAC_BG)
        self.typer_frame = tk.Frame(content, bg=MAC_BG)
        self.app_frame = tk.Frame(content, bg=MAC_BG)
        self._frames = {"chrome": self.chrome_frame, "typer": self.typer_frame, "app": self.app_frame}

        self.chrome = ChromeTab(self.chrome_frame, root, global_save=self.save_all, on_done=self._focus_typer)
        self.typer = TyperTab(self.typer_frame, root, global_save=self.save_all)
        self.app = ApplicationTab(self.app_frame, root, self.chrome, global_save=self.save_all)

        self.current = None
        self.select("chrome")

        root.bind_all("<MouseWheel>", self._on_wheel)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.app.autorun.get():
            root.after(1200, lambda: self.chrome._start(auto=True))

    def select(self, key):
        for k, frame in self._frames.items():
            frame.pack_forget()
        self._frames[key].pack(fill="both", expand=True)
        for k, pill in self.pills.items():
            pill.set_selected(k == key)
        self.current = key

    def _focus_typer(self):
        self.select("typer")

    def _on_wheel(self, event):
        c = None
        if self.current == "chrome":
            c = self.chrome.scroll_canvas
        elif self.current == "typer":
            c = getattr(self.typer, "canvas", None)
        if c is None:
            return
        first, last = c.yview()
        if first <= 0.0 and last >= 1.0:
            return  # everything already visible; nothing to scroll
        c.yview_scroll(int(-event.delta / 120), "units")

    def save_all(self, flash=True):
        cfg = {}
        cfg.update(self.chrome._collect_settings())
        cfg.update(self.app._collect_settings())
        save_config(cfg)
        try:
            self.typer.persist()
            self.typer._setup_all_hotkeys()
        except Exception:
            pass
        if flash:
            self._flash_saved()

    def _flash_saved(self):
        for lbl in (self.chrome.status_label, self.app.status_label):
            try:
                lbl.config(text="Saved ✓")
            except Exception:
                pass
        try:
            self.typer.status_label.config(text="Saved ✓", fg=MAC_SUCCESS)
        except Exception:
            pass
        self.root.after(1800, self._clear_saved)

    def _clear_saved(self):
        for lbl in (self.chrome.status_label, self.app.status_label):
            try:
                lbl.config(text="")
            except Exception:
                pass
        try:
            self.typer.status_label.config(text="Ready", fg=COLOR_TEXT_SUB)
        except Exception:
            pass

    def _on_close(self):
        try:
            self.save_all(flash=False)
        except Exception:
            pass
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        os._exit(0)


# ----------------------------------------------------------------------------
# Self-update flow + entry point
# ----------------------------------------------------------------------------

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
    tk.Label(top, text=f"Updating to {tag}...", font=(FONT_FAMILY, 10, "bold")).pack(pady=(16, 6))
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
            os._exit(0)
    except Exception:
        pass
    root.deiconify()
    CombinedApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
