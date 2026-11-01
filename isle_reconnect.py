"""
isle_reconnect.py - fully automated join / rejoin bot for The Isle (Evrima).

Start it once and leave it running:

    python isle_reconnect.py                 opens a window (so does double-clicking the exe)
    python isle_reconnect.py run --server "part of the server name"     same bot, console only

What it does on its own:
  * Knows what the game is doing from its own log file (instant, never misreads):
    joining, join failed (e.g. server full), in game, disconnected, closed.
  * Navigates the menus with Windows' built-in OCR: finds buttons by their text
    (Play, the server in the list, Join, OK...), so no click recording is needed
    and resolution / window size doesn't matter.
  * Retries the moment a join fails, rejoins after a disconnect, and relaunches
    the game through Steam if it crashes.

Hotkeys (global, work while the game has focus):  F10 quit   F11 pause / resume
(change them under "hotkeys" in isle_reconnect.json)

Other commands:
    python isle_reconnect.py stop       stop a running bot without the hotkey
    python isle_reconnect.py scan       OCR the game window once and print what it sees
    python isle_reconnect.py status     show what the log says the game is doing

Needs: Windows 10/11, Python 3.9+, and
    pip install winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging
                winrt-Windows.Storage.Streams winrt-Windows.Foundation
                winrt-Windows.Foundation.Collections winrt-Windows.Globalization
Run the game windowed or borderless (exclusive fullscreen captures as black).
Menu labels live in isle_reconnect.json (created on first run) if the game renames a button.
"""

import argparse
import asyncio
import collections
import ctypes
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes

GAME_EXE = "theisleclient-win64-shipping.exe"
STEAM_LAUNCH = "steam://rungameid/376210"
LOG_PATH = os.path.expandvars(r"%LOCALAPPDATA%\TheIsle\Saved\Logs\TheIsle.log")
# Next to the .exe when built with PyInstaller (__file__ is then a temp unpack folder).
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "isle_reconnect.json")

DEFAULT_CONFIG = {
    "server": "",
    "session_filter": "Unofficial",  # server browser dropdown: Official / Unofficial / Favourites / History
    # Where the "Server Name Filter" box is, as a fraction of the window. Relearned whenever
    # its hint text is visible; needed to retype once the box already holds text.
    "search_box": [0.797, 0.178],
    "ocr_scale": 2.0,  # the list font is small; OCR reads it much better enlarged
    # Button texts, in priority order. Matching ignores case and tolerates small OCR errors.
    "labels": {
        "any_key": ["PRESS ANY KEY", "CLICK TO CONTINUE", "CLICK ANYWHERE"],
        "dismiss": ["OK", "OKAY", "CLOSE", "DISMISS", "CONTINUE"],
        "join": ["CONNECT", "JOIN", "JOIN SERVER"],
        "confirm": ["YES", "CONFIRM", "JOIN ANYWAY"],
        "search": ["SERVER NAME FILTER", "SEARCH", "SEARCH SERVERS"],
        "refresh": ["REFRESH"],
        "browser": ["SESSION FILTER", "REFRESH", "SERVER NAME FILTER"],  # proves we're in the server list
        "resume": ["RESUME"],  # the in-game pause menu (Esc)
        "to_browser": ["PLAY", "SERVER BROWSER", "SERVERS", "BROWSE SERVERS", "MULTIPLAYER",
                       "FIND SERVER", "COMMUNITY"],
    },
    "hotkeys": {"quit": "F10", "pause": "F11"},  # any of F1-F24, PAUSE, SCROLLLOCK, INSERT, HOME, END
    "join_timeout": 90,       # seconds without a result before navigating again
    "launch_timeout": 240,    # seconds to wait for a relaunched game
    "rejoin_after_leave": False,
    "relaunch_after_quit": False,
    # Safe-log when hunger or thirst (read from the HUD) drops to this many percent; 0 turns it off.
    # Safe-logging taps the key to rest, then holds it to sleep; the server lets you out after 60 s.
    "safe_log": {"hunger": 0, "thirst": 0, "key": "H", "hold_seconds": 2.0},
    # Brightens the screen in dark scenes while the game is in front: Off / Low / Medium / High.
    "adaptive_brightness": "Off",
}

VK_RETURN = 0x0D


log_sink = None  # the GUI points this at its log view; otherwise lines go to the console


def log(msg):
    line = time.strftime("[%H:%M:%S] ") + msg
    if log_sink:
        log_sink(line)
    else:
        print(line, flush=True)


# --- Win32 -------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
kernel32 = ctypes.WinDLL("kernel32")
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels on scaled displays
except Exception:
    user32.SetProcessDPIAware()

HGDIOBJ = wintypes.HGDIOBJ
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.GetForegroundWindow.restype = wintypes.HWND
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, HGDIOBJ]
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.StretchBlt.argtypes = [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.DWORD]
gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
user32.IsHungAppWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _key(vk=0, scan=0, flags=0):
    return INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))


def _mouse(flags):
    return INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(0, 0, 0, flags, 0, 0)))


def process_name(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid_name(pid.value)


def pid_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(len(buf))
        kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)


class Game:
    """The game window: finding it, capturing it, and sending it input."""

    def __init__(self):
        self.hwnd = None
        self.use_screen = False  # fall back to screen capture if PrintWindow keeps giving black
        self.black_frames = 0

    def find(self):
        if self.hwnd and user32.IsWindow(self.hwnd):
            return self.hwnd
        found = []

        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd) and process_name(hwnd) == GAME_EXE:
                rect = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(rect))
                if rect.right > 200 and rect.bottom > 200:  # skip splash / helper windows
                    found.append(hwnd)
                    return False
            return True

        user32.EnumWindows(EnumWindowsProc(cb), 0)
        self.hwnd = found[0] if found else None
        return self.hwnd

    def client_size(self):
        rect = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rect))
        return rect.right, rect.bottom

    def to_screen(self, x, y):
        pt = wintypes.POINT(int(x), int(y))
        user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    def focus(self):
        if user32.GetForegroundWindow() == self.hwnd:
            return
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 9)  # SW_RESTORE
        _send(_key(0x12), _key(0x12, flags=2))  # tapping Alt allows taking the foreground
        user32.SetForegroundWindow(self.hwnd)
        time.sleep(0.15)

    def capture(self, scale=1.0):
        """Returns (width, height, BGRA bytes, scale) of the client area, enlarged by scale."""
        w, h = self.client_size()
        mem, bits, bmp, old = _dib(w, h)
        big = None
        try:
            if not self.use_screen:
                # PW_CLIENTONLY | PW_RENDERFULLCONTENT: works for DirectX windows, even in the background.
                user32.PrintWindow(self.hwnd, mem, 3)
                self.black_frames = self.black_frames + 1 if is_black(ctypes.string_at(bits, w * h * 4)) else 0
                if self.black_frames >= 10:  # a few dark frames are just loading screens
                    self.use_screen = True
                    log("Window capture keeps coming back black; switching to screen capture "
                        "(game must be visible)")
            if self.use_screen:
                self.focus()
                sx, sy = self.to_screen(0, 0)
                screen = user32.GetDC(None)
                gdi32.BitBlt(mem, 0, 0, w, h, screen, sx, sy, 0x00CC0020)  # SRCCOPY
                user32.ReleaseDC(None, screen)
            if scale == 1.0:
                return w, h, ctypes.string_at(bits, w * h * 4), 1.0
            W, H = int(w * scale), int(h * scale)
            big = _dib(W, H)
            gdi32.SetStretchBltMode(big[0], 4)  # HALFTONE: smooth enlargement
            gdi32.StretchBlt(big[0], 0, 0, W, H, mem, 0, 0, w, h, 0x00CC0020)
            return W, H, ctypes.string_at(big[1], W * H * 4), scale
        finally:
            for d in (mem, bits, bmp, old), big:
                if d:
                    gdi32.SelectObject(d[0], d[3])
                    gdi32.DeleteObject(d[2])
                    gdi32.DeleteDC(d[0])

    def click(self, x, y, double=False):
        self.focus()
        user32.SetCursorPos(*self.to_screen(x, y))
        time.sleep(0.04)  # let the UI register the hover
        for _ in range(2 if double else 1):
            _send(_mouse(0x0002), _mouse(0x0004))  # left down, left up
            time.sleep(0.05)

    def move(self, x, y):
        """Moves the mouse as real movement (SendInput), which the game's hover needs;
        SetCursorPos alone doesn't bring up its tooltips."""
        sx, sy = self.to_screen(x, y)
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        _send(INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(int(sx * 65535 / (sw - 1)), int(sy * 65535 / (sh - 1)),
                                                         0, 0x8001, 0, 0))))  # MOVE | ABSOLUTE

    def tap(self, vk):
        scan = user32.MapVirtualKeyW(vk, 0)
        _send(_key(scan=scan, flags=0x8))
        time.sleep(0.05)
        _send(_key(scan=scan, flags=0x8 | 2))

    def clear_line(self):
        """Empties the text box that has focus (Ctrl+A, Backspace)."""
        _send(_key(0x11), _key(0x41), _key(0x41, flags=2), _key(0x11, flags=2))
        time.sleep(0.03)
        self.tap(0x08)

    def hung(self):
        return bool(user32.IsHungAppWindow(self.hwnd))

    def kill(self):
        """Ends a game that's frozen or stuck; the bot then relaunches it through Steam."""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x0001, False, pid.value)  # PROCESS_TERMINATE
        if h:
            kernel32.TerminateProcess(h, 1)
            kernel32.CloseHandle(h)
        self.hwnd = None

    def post_key(self, vk, seconds=0.05):
        """Presses a key for `seconds`, posted to the game window alone: it works with the game in
        the background, needs no focus, and can't land in another app."""
        down = 1 | user32.MapVirtualKeyW(vk, 0) << 16  # repeat count 1, scan code
        user32.PostMessageW(self.hwnd, 0x0100, vk, down)  # WM_KEYDOWN
        end = time.time() + seconds
        while time.time() < end:  # repeats while held, as a real key does
            time.sleep(0.05)
            user32.PostMessageW(self.hwnd, 0x0100, vk, down | 1 << 30)  # was already down
        user32.PostMessageW(self.hwnd, 0x0101, vk, down | 3 << 30)  # WM_KEYUP

    def hold_key(self, key, seconds):
        self.post_key(ord(key.upper()), seconds)

    def type_text(self, text):
        self.focus()
        _send(_key(0x11), _key(0x41), _key(0x41, flags=2), _key(0x11, flags=2))  # Ctrl+A
        time.sleep(0.03)
        for ch in text:
            _send(_key(scan=ord(ch), flags=4), _key(scan=ord(ch), flags=4 | 2))  # KEYEVENTF_UNICODE
            time.sleep(0.01)
        _send(_key(VK_RETURN), _key(VK_RETURN, flags=2))


def _dib(w, h):
    """A memory DC with a top-down 32-bit bitmap selected: (dc, bits, bitmap, old object)."""
    mem = gdi32.CreateCompatibleDC(None)
    bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(mem, ctypes.byref(bih), 0, ctypes.byref(bits), None, 0)
    return mem, bits, bmp, gdi32.SelectObject(mem, bmp)


def is_black(data):
    sample = data[::4 * 997]  # every ~1000th pixel, blue channel
    return max(sample, default=0) < 8


KEY_NAMES = {**{f"F{i}": 0x6F + i for i in range(1, 25)},
             "PAUSE": 0x13, "SCROLLLOCK": 0x91, "INSERT": 0x2D, "HOME": 0x24, "END": 0x23}


class Hotkeys:
    """Global hotkeys on their own thread, so a press is never missed while the bot is busy
    reading the screen. Uses RegisterHotKey (delivered even over a focused full-screen game);
    if another program already owns the key, falls back to watching the key directly."""

    def __init__(self, keys):  # {"quit": "F10", "pause": "F11"}
        self.keys = keys
        self.hits = []
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.ready.wait(2)

    def close(self):
        self.closed.set()
        self.thread.join(1)

    def _loop(self):
        owned, polled, down = {}, [], {}
        for i, (action, key) in enumerate(self.keys.items(), 1):
            vk = KEY_NAMES.get(key.upper())
            if vk is None:
                log(f"Unknown hotkey '{key}' for {action} - use one of: {', '.join(KEY_NAMES)}")
            elif user32.RegisterHotKey(None, i, 0x4000, vk):  # MOD_NOREPEAT
                owned[i] = action
            else:
                log(f"{key} is already taken by another program (an overlay?) - "
                    f"watching the key directly instead. Change it in isle_reconnect.json if it misbehaves.")
                polled.append((action, vk))
        self.ready.set()
        msg = wintypes.MSG()
        while not self.closed.is_set():
            while user32.PeekMessageW(ctypes.byref(msg), None, 0x0312, 0x0312, 1):  # WM_HOTKEY, PM_REMOVE
                self._hit(owned.get(msg.wParam))
            for action, vk in polled:
                state = user32.GetAsyncKeyState(vk)
                now = bool(state & 0x8000)
                if (now and not down.get(vk)) or (state & 1 and not now):  # held, or tapped between polls
                    self._hit(action)
                down[vk] = now
            time.sleep(0.01)
        for i in owned:  # hotkeys belong to the thread that registered them
            user32.UnregisterHotKey(None, i)

    def _hit(self, action):
        if action:
            with self.lock:
                self.hits.append(action)

    def take(self, action):
        with self.lock:
            if action in self.hits:
                self.hits.remove(action)
                return True
        return False


STOP_EVENT = "Local\\isle_reconnect_stop"  # 'python isle_reconnect.py stop' signals this
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]


# --- Hunger / thirst ---------------------------------------------------------

# The HUD's stomach and water-drop icons (bottom right) fill with pale cyan from the bottom up.
# The band each fill moves in from 0 to 100%, as (left, top, right, bottom) pixels in from the
# window's bottom-right corner at 1080p; the HUD scales with the window height. Fitted to the
# game's own tooltip values: the stomach's 0% line sits a few pixels below its drawn bottom.
STAT_ICONS = {"hunger": (163, 236, 86, 169), "thirst": (114, 163, 54, 99)}


def read_stats(game):
    """{"hunger": %, "thirst": %} from the HUD; None for an icon that isn't on screen
    (menus, the map, a hidden HUD). At 0 the game draws the icon solid red instead."""
    w, h, data, _ = game.capture()
    k = h / 1080
    stats = {}
    for stat, (left, top, right, bottom) in STAT_ICONS.items():
        x0, x1 = w - round(left * k), w - round(right * k)
        top, bottom = h - round(top * k), h - round(bottom * k)
        drawn, fill_top, gap, red = 0, None, 0, 0
        for y in range(bottom, top - 1, -1):  # bottom up, to the top of the fill
            row = data[(y * w + x0) * 4:(y * w + x1) * 4]
            cyan = fill = 0
            for i in range(0, len(row), 4):
                b, g, r = row[i], row[i + 1], row[i + 2]
                cyan += g > 170 and b > 170 and b > r + 20  # the glowing outline, or the fill
                # The fill is near-white (its brightness follows the scene); the outline's glow can
                # be as bright but is a saturated cyan, so whiteness tells them apart.
                fill += r > 180 and g > 210 and b > 210 and g - r < 40
                red += r >= 230 and g < 70 and b < 70  # empty: the icon turns red
            drawn += cyan >= 3
            if gap <= 3 * k and fill >= 4 * k and fill >= 0.35 * cyan:
                fill_top, gap = y, 0
            elif fill_top is not None:
                gap += 1  # a few rows of slack for the icon's ragged edge; then the fill is over
        if red > 0.15 * (x1 - x0) * (bottom - top + 1):
            stats[stat] = 0
        elif drawn < 0.6 * (bottom - top + 1):
            stats[stat] = None
        elif fill_top is None:
            stats[stat] = 0
        else:
            stats[stat] = min(100, round(100 * (bottom - fill_top) / (bottom - top)))
    return stats


RE_PERCENT = re.compile(r"(\d{1,3}[.,]\d{1,2})")


def crop(image, x0, y0, w, h, factor):
    """A piece of a capture, enlarged by factor (nearest neighbour), as an image for Ocr.read."""
    iw, ih, data, _ = image
    x0, y0 = max(0, min(x0, iw - w)), max(0, min(y0, ih - h))
    out = bytearray()
    for y in range(h * factor):
        row = data[((y0 + y // factor) * iw + x0) * 4:((y0 + y // factor) * iw + x0 + w) * 4]
        line = b"".join(row[i:i + 4] * factor for i in range(0, len(row), 4))
        out += line
    return w * factor, h * factor, bytes(out), factor


def chat_open(image):
    """Whether chat is taking input: its white typing bar, along the bottom left, is showing."""
    w, h, data, scale = image
    k = h / 1080
    y = h - round(16 * k)
    row = [data[(y * w + round(x * k)) * 4:(y * w + round(x * k)) * 4 + 3] for x in range(100, 400, 25)]
    return min(sum(px) / 3 for px in row) > 150


def read_exact(game, ocr, stats):  # only for `stats --exact`; the bot itself never needs it
    """Exact values from the game's own tooltips: open chat (Enter) to free the cursor, hover each
    icon, OCR the "16.63 %" that appears up and to the right of it, close chat (Enter again; Esc
    would open the pause menu). The tooltip is its own window, so this captures the screen; the
    game must be visible. Returns {stat: percent} for the ones it could read."""
    w, h = game.client_size()
    k = h / 1080
    found = {}
    use_screen, game.use_screen = game.use_screen, True
    try:
        game.focus()
        if chat_open(game.capture()):
            game.clear_line()  # already open: empty it, so closing it below sends nothing
        else:
            game.tap(VK_RETURN)
            time.sleep(0.8)
            if not chat_open(game.capture()):
                log("Couldn't open chat to read the exact hunger / thirst - using the HUD icons")
                return found
        try:
            for stat in stats:
                left, top, right, bottom = STAT_ICONS[stat]
                x, y = w - (left + right) / 2 * k, h - (top + bottom) / 2 * k
                for d in (-20, -10, -4, 0, 2, 0):  # a little real movement, so the game registers the hover
                    game.move(x + d, y + d)
                    time.sleep(0.08)
                for _ in range(4):  # the tooltip takes a moment to appear
                    time.sleep(0.6)
                    image = game.capture(2.0)
                    piece = crop(image, int(2 * x), int(2 * (y - 45 * k)), int(2 * 150 * k), int(2 * 55 * k), 2)
                    m = RE_PERCENT.search(" ".join(line.text for line in ocr.read(piece)))
                    if m and 0 <= float(m.group(1).replace(",", ".")) <= 100:
                        found[stat] = float(m.group(1).replace(",", "."))
                        break
        finally:
            game.tap(VK_RETURN)  # closes chat; it's empty, so nothing is sent
            time.sleep(0.5)
            if chat_open(game.capture()):
                game.tap(VK_RETURN)
    finally:
        game.use_screen = use_screen
    return found


# --- OCR ---------------------------------------------------------------------


class Word:
    __slots__ = ("text", "x", "y", "w", "h")

    def __init__(self, text, x, y, w, h):
        self.text, self.x, self.y, self.w, self.h = text, x, y, w, h


class Line:
    def __init__(self, words):
        self.words = words
        self.text = " ".join(w.text for w in words)
        self.x0 = min(w.x for w in words)
        self.y0 = min(w.y for w in words)
        self.x1 = max(w.x + w.w for w in words)
        self.y1 = max(w.y + w.h for w in words)

    @property
    def center(self):
        return (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2


class Ocr:
    """Windows.Media.Ocr - built into Windows, ~15-40 ms per 1080p frame."""

    def __init__(self):
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter

        self._bitmap = lambda buf, w, h: SoftwareBitmap.create_copy_from_buffer(
            buf, BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.IGNORE)
        self._writer = DataWriter
        self.engine = (OcrEngine.try_create_from_language(Language("en-US"))
                       or OcrEngine.try_create_from_user_profile_languages())
        if self.engine is None:
            sys.exit("No Windows OCR language installed (Settings > Time & language > Language > add English).")
        self.loop = asyncio.new_event_loop()

    async def _recognize(self, bmp):
        return await self.engine.recognize_async(bmp)

    def read(self, image):
        """Text lines with word boxes in window coordinates (undoing any capture scale)."""
        w, h, data = image[:3]
        k = 1 / (image[3] if len(image) > 3 else 1.0)
        dw = self._writer()
        dw.write_bytes(data)
        result = self.loop.run_until_complete(self._recognize(self._bitmap(dw.detach_buffer(), w, h)))
        lines = []
        for ln in result.lines:
            words = [Word(wd.text, wd.bounding_rect.x * k, wd.bounding_rect.y * k,
                          wd.bounding_rect.width * k, wd.bounding_rect.height * k) for wd in ln.words]
            if words:
                lines.append(Line(words))
        return lines


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


OCR_LOOKALIKES = str.maketrans("0158", "OISB")  # e.g. "OK" is often read as "0K"


def word_matches(seen, want):
    a, b = norm(seen).translate(OCR_LOOKALIKES), norm(want).translate(OCR_LOOKALIKES)
    if a == b:
        return True
    if len(b) < 4 or abs(len(a) - len(b)) > max(1, len(b) // 8):
        return False
    if a in b or b in a:  # a different word, not a misread: "OFFICIAL" vs "UNOFFICIAL"
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def find_label(lines, label):
    """Centre of a stand-alone button text (not a word inside a sentence), or None."""
    target = [t for t in label.split() if norm(t)]
    n = len(target)
    for line in lines:
        ws = line.words
        for i in range(len(ws) - n + 1):
            if not all(word_matches(ws[i + k].text, target[k]) for k in range(n)):
                continue
            seg = ws[i:i + n]
            gap = max(w.h for w in seg) * 1.2
            if i > 0 and seg[0].x - (ws[i - 1].x + ws[i - 1].w) < gap:
                continue
            if i + n < len(ws) and ws[i + n].x - (seg[-1].x + seg[-1].w) < gap:
                continue
            x0, x1 = seg[0].x, seg[-1].x + seg[-1].w
            y0, y1 = min(w.y for w in seg), max(w.y + w.h for w in seg)
            return (x0 + x1) / 2, (y0 + y1) / 2
    return None


def find_any(lines, labels):
    for label in labels:
        pos = find_label(lines, label)
        if pos:
            return label, pos
    return None, None


def group_rows(lines):
    """OCR splits a list row into pieces (name, tags, players...); glue pieces on the same band."""
    rows = []
    for line in sorted(lines, key=lambda l: l.center[1]):
        if rows and abs(rows[-1][0] - line.center[1]) < max(line.y1 - line.y0, 8) * 0.6:
            rows[-1][1].append(line)
        else:
            rows.append([line.center[1], [line]])
    return [Line([w for l in sorted(ls, key=lambda l: l.x0) for w in l.words]) for _, ls in rows]


RE_PLAYERS = re.compile(r"(\d{1,3})\s*/\s*(\d{1,3})")


def server_rows(lines, width):
    """(row, name text) for each list row. The name is what sits left of the player count,
    or failing that, left of the filter panel - so the search box itself never matches."""
    out = []
    for row in group_rows(lines):
        count = next((w for w in row.words if RE_PLAYERS.search(w.text)), None)
        limit = count.x if count else width * 0.7
        name = " ".join(w.text for w in row.words if w.x < limit)
        if name:
            out.append((row, re.sub(r"[^a-z0-9]", "", name.lower().replace("|", "i"))))
    return out


def find_server(lines, name, width):
    """The list row for the server. Fuzzy (OCR drops/garbles letters) but the digits must
    match exactly, so "BOSCH-ISLAND.COM 3" never picks "BOSCH-ISLAND.COM 2"."""
    target = re.sub(r"[^a-z0-9]", "", name.lower())
    if not target:
        return None
    rows = server_rows(lines, width)
    for row, text in rows:
        if target in text:
            return row
    if len(target) < 6:
        return None
    digits = re.sub(r"\D", "", target)
    for row, text in rows:
        for size in range(len(target) - 2, len(target) + 3):
            for i in range(max(1, len(text) - size + 1)):
                window = text[i:i + size]
                if re.sub(r"\D", "", window) == digits and \
                        difflib.SequenceMatcher(None, window, target).ratio() >= 0.82:
                    return row
    return None


def row_players(row):
    """'97/100' style count on the server's row, if visible."""
    m = RE_PLAYERS.search(row.text)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def dropdown(lines, label, want):
    """For a 'Label: value' dropdown, returns (current_ok, click_pos) - where to click to
    open it, or the wanted option once it's open. None if the label isn't on screen."""
    words = [w for l in lines for w in l.words]
    # Anchor on the label's last word plus its colon ("Filter:"): OCR sometimes mangles the
    # first word ("Session" -> "Sion"), and the colon tells it apart from "Server Name Filter".
    key = norm(label.split()[-1])
    anchor = next((w for w in words if w.text.rstrip().endswith(":") and norm(w.text).endswith(key)), None)
    if not anchor:
        return None
    fx, fy = anchor.x + anchor.w, anchor.y + anchor.h / 2
    value = [w for w in words if abs(w.y + w.h / 2 - fy) < w.h and w.x > fx]
    if not value:
        return None
    current = min(value, key=lambda w: w.x)
    if word_matches(current.text, want):
        return True, None
    vx = current.x + current.w / 2
    for w in words:  # options listed under the open dropdown
        if w.y > fy + w.h * 0.8 and abs(w.x + w.w / 2 - vx) < 200 and word_matches(w.text, want):
            return False, (w.x + w.w / 2, w.y + w.h / 2)
    return False, (vx, current.y + current.h / 2)


# --- Game log ----------------------------------------------------------------


class LogTail:
    """Follows the Unreal log; notices when the game starts a fresh one."""

    def __init__(self, path, from_start=True):
        self.path = path
        self.ino = None
        self.pos = 0
        self.buf = b""
        self.restarted = False
        if not from_start and os.path.exists(path):
            self.ino, self.pos = os.stat(path).st_ino, os.path.getsize(path)

    def read_lines(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return []
        if st.st_ino != self.ino or st.st_size < self.pos:  # new game session
            self.restarted = self.ino is not None
            self.ino, self.pos, self.buf = st.st_ino, 0, b""
        if st.st_size == self.pos:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read()
            self.pos = f.tell()
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        return [l.decode("utf-8", "replace") for l in lines]


# These markers come from real Evrima client logs.
RE_JOIN = re.compile(r'Browse Started Browse: "(\d+\.\d+\.\d+\.\d+)')
RE_JOIN_FAILED = re.compile(r"Name:PendingNetDriver .* shut down")
RE_DROPPED = re.compile(r'Browse Started Browse: "/Game/TheIsle/Maps/TitleMap\?closed')
RE_LEFT = re.compile(r'Browse Started Browse: "/Game/TheIsle/Maps/TitleMap"')
RE_MENU = re.compile(r"Load map complete /Game/TheIsle/Maps/TitleMap")
RE_INGAME = re.compile(r"PendingNetGame::TravelCompleted")
RE_EXIT = re.compile(r"LogExit: Preparing to exit")


class GameLog:
    """Turns log lines into a game state: starting, menu, joining, ingame, closed."""

    def __init__(self, path):
        self.tail = LogTail(path)
        self.state, self.since, self.server_ip = "starting", time.time(), None
        self.poll()  # replay the current session silently to learn where we are

    def _set(self, state):
        self.state, self.since = state, time.time()

    def poll(self):
        events = []
        lines = self.tail.read_lines()
        if self.tail.restarted:
            self.tail.restarted = False
            self._set("starting")
        for line in lines:
            m = RE_JOIN.search(line)
            if m:
                self.server_ip = m.group(1)
                self._set("joining")
                events.append("joining")
            elif RE_JOIN_FAILED.search(line):
                if self.state == "joining":
                    events.append("join_failed")
                self._set("loading")
            elif RE_DROPPED.search(line):  # can be logged just before the driver shutdown
                if self.state == "ingame":
                    events.append("disconnected")
                elif self.state == "joining":
                    events.append("join_failed")
                self._set("loading")
            elif RE_LEFT.search(line):
                if self.state == "ingame":
                    events.append("left")
                self._set("loading")
            elif RE_MENU.search(line):
                self._set("menu")
                events.append("menu")
            elif RE_INGAME.search(line):
                self._set("ingame")
                events.append("ingame")
            elif RE_EXIT.search(line):
                self._set("closed")
                events.append("closed")
        return events


# --- Menu navigation ---------------------------------------------------------


class Navigator:
    """One OCR pass per tick; does the single most useful click for what's on screen."""

    def __init__(self, cfg, game, ocr, debug=False):
        self.cfg, self.labels, self.game, self.ocr, self.debug = cfg, cfg["labels"], game, ocr, debug
        self.reset()

    def reset(self):
        self.typed_at = 0.0
        self.row_clicked_at = 0.0
        self.join_clicked_at = 0.0  # set while waiting to see if Connect started a join
        self.connect_at = 0.0
        self.need_refresh = False
        self.refreshes = 0
        self.last_players = ""
        self.refresh_at = 0.0
        self.last_seen = time.time()
        self.warned = False
        self.esc_sent = False  # the bot's one Esc when the screen is unrecognisable
        self.last_action = None

    def act(self, what):
        if what != self.last_action:  # don't spam the same line every pass
            log(what)
        self.last_action = what

    def tick(self):
        now = time.time()
        if self.join_clicked_at:
            if now - self.join_clicked_at < 1.5:
                return  # give the log a moment to report the join
            # Still on the menu: the game won't even try while its cached list says the
            # server is full, so the list needs a refresh before Connect means anything.
            self.join_clicked_at, self.need_refresh = 0.0, True
        width, height = self.game.client_size()
        lines = self.ocr.read(self.game.capture(self.cfg.get("ocr_scale", 1.0)))
        if self.debug:
            log("OCR: " + " | ".join(l.text for l in lines)[:300])
        L = self.labels
        server = self.cfg["server"]

        label, pos = find_any(lines, L["any_key"])
        if pos:
            self.act("Start screen - clicking to continue")
            return self._click(pos)

        if now - self.connect_at < 10:
            label, pos = find_any(lines, L["confirm"])
            if pos:
                self.act(f"Confirming join ({label})")
                return self._click(pos)

        in_browser = find_any(lines, L["browser"])[1] is not None

        # The list has to show the right category first (the game resets it to Official
        # after every failed join). Click the value to open it, then the wanted option.
        want = self.cfg.get("session_filter")
        if want:
            dd = dropdown(lines, "SESSION FILTER", want)
            if dd and not dd[0]:
                self.act(f"Setting session filter to {want}")
                self.row_clicked_at = 0.0
                return self._click(dd[1], wait=0.5)

        if self.need_refresh and in_browser:
            _, pos = find_any(lines, L["refresh"])
            if pos:
                self.refreshes += 1
                self.act(f"Still full{self.last_players} - refresh #{self.refreshes}")
                self.need_refresh, self.refresh_at = False, time.time()
                return self._click(pos, wait=0.5)

        row = find_server(lines, server, width)
        if row:
            _, join = find_any(lines, L["join"])
            players = row_players(row)
            info = self.last_players = f" [{players}]" if players else ""
            # Click the row first every time so the Connect panel is for *this* server.
            click_at = (row.words[0].x + row.words[0].w / 2, row.center[1])
            if join:
                self.act(f"Connecting{info}")
                self.game.click(*click_at)
                time.sleep(0.25)
                self.game.click(*join)
            elif now - self.row_clicked_at > 4 and self.row_clicked_at:
                self.act(f"No Connect button - trying double-click{info}")
                self.game.click(*click_at, double=True)
            else:
                if not self.row_clicked_at:
                    self.row_clicked_at = now
                self.act(f"Selecting server{info}")
                self.game.click(*click_at)
                return self._seen()
            self.join_clicked_at = self.connect_at = time.time()
            return self._seen()

        if now - self.refresh_at < 6:
            return  # list is reloading; the row will show up shortly

        _, hint = find_any(lines, L["search"])
        if hint:  # empty box shows its hint text: remember where the box is
            self.cfg["search_box"] = [round(hint[0] / width, 4), round(hint[1] / height, 4)]
        if (in_browser or hint) and now - self.typed_at > 8:
            box = hint or (self.cfg["search_box"][0] * width, self.cfg["search_box"][1] * height)
            self.act("Searching for the server")
            self.game.click(*box)
            time.sleep(0.1)
            self.game.type_text(server)  # Ctrl+A first, so old text is replaced
            self.typed_at = time.time()
            return self._seen()

        if self.typed_at and now - self.typed_at > 3 and now - self.refresh_at > 5:
            label, pos = find_any(lines, L["refresh"])
            if pos:
                self.act("Refreshing the server list")
                self.refresh_at = now
                return self._click(pos)

        # Inside the server browser a "Close" button could close the browser, so only
        # treat these as popups when we're clearly not in the server list.
        if not in_browser and not hint:
            label, pos = find_any(lines, L["dismiss"])
            if pos:
                self.act(f"Closing popup ({label})")
                return self._click(pos)

        label, pos = find_any(lines, L["to_browser"])
        if pos:
            self.act(f"Opening the server list ({label})")
            return self._click(pos)

        if not self.warned and now - self.last_seen > 15:
            log("Nothing recognisable on screen for 15s. Run 'scan' to see what OCR reads "
                "and add the button text to isle_reconnect.json.")
            self.warned = True

    def _click(self, pos, wait=0.3):
        self.game.click(*pos)
        time.sleep(wait)  # let menu transitions start so the same button isn't hit twice
        self._seen()

    def _seen(self):
        self.last_seen, self.warned, self.esc_sent = time.time(), False, False


# --- The game's own settings -------------------------------------------------

GAME_CONFIG_DIR = os.path.expandvars(r"%LOCALAPPDATA%\TheIsle\Saved\Config\WindowsClient")
GAME_SETTINGS = os.path.join(GAME_CONFIG_DIR, "GameUserSettings.ini")
GAME_ENGINE = os.path.join(GAME_CONFIG_DIR, "Engine.ini")
# The game's graphics levels, as its menu shows them (0 Low .. 3 Epic), in [ScalabilityGroups].
GAME_QUALITY = [("sg.ViewDistanceQuality", "View distance"), ("sg.ShadowQuality", "Shadows"),
                ("sg.TextureQuality", "Textures"), ("sg.EffectsQuality", "Effects"),
                ("sg.FoliageQuality", "Foliage"), ("sg.ShadingQuality", "Shading"),
                ("sg.LandscapeQuality", "Landscape")]
# Read by the game from [/Script/Engine.RendererSettings] in GameUserSettings.ini, but missing from
# its menu (found in the exe: each runs the r.* command of the same name). 0 turns the effect off;
# no key leaves it to the game.
HIDDEN_EFFECTS = [("BloomQuality", "Bloom", "glow around the sun and bright light"),
                  ("LensFlareQuality", "Lens flare", "rings and streaks from the sun")]
# The game's foliage physics switches (True/False in [/Script/Engine.GameUserSettings]); all cost CPU.
FOLIAGE_PHYSICS = [("PhysicsFoliage", "Plants move as you pass", "your dino only"),
                   ("PhysicsFoliageAllPlayers", "Plants move for everyone", "costs the most CPU"),
                   ("PhysicsGrass", "Grass bends", "")]
# Engine.ini choices shown as tabs, as (name, hint, {label: {(section, key): value}}). The first label
# is "Game": it removes the keys, leaving the game's own value.
SYSTEM, ENGINE = "SystemSettings", "/Script/Engine.Engine"
# Volumetric fog drawn on a coarser grid: as thick as before, cheaper, slightly blockier at the edges.
FOG_DETAIL = ("Fog detail", None, {
    "Game": {},
    "Lower": {(SYSTEM, "r.VolumetricFog.GridPixelSize"): "12", (SYSTEM, "r.VolumetricFog.GridSizeZ"): "96"},
    "Lowest": {(SYSTEM, "r.VolumetricFog.GridPixelSize"): "16", (SYSTEM, "r.VolumetricFog.GridSizeZ"): "64"}})
# Settings servers ban for, for private / sandbox servers. DisplayGamma is a plain engine property
# (default 2.2), so it doesn't depend on ini cvars being honoured. Grass and foliage density are left
# out: the game resets grass.*, foliage.DensityScale and r.ShadowQuality on every map load.
VISIBILITY_CHOICES = [
    ("Night brightness", "display gamma", {"Game": {}, "Brighter": {(ENGINE, "DisplayGamma"): "2.8"},
                                           "Brightest": {(ENGINE, "DisplayGamma"): "3.4"}}),
    ("Draw distance", "everything, farther", {"Game": {}, "Far": {(SYSTEM, "r.ViewDistanceScale"): "2"},
                                              "Farthest": {(SYSTEM, "r.ViewDistanceScale"): "4"}})]
VISIBILITY_TWEAKS = [("r.Fog", "0", "Height fog off", "the haze over distance"),
                     ("r.VolumetricFog", "0", "Volumetric fog off", "fog banks and mist"),
                     ("r.VolumetricCloud", "0", "Clouds off", "also saves GPU")]
# Engine.ini [SystemSettings] cvars, as (cvar, value, name, hint). Untested: a build can ignore ini cvars.
ENGINE_TWEAKS = [("r.AmbientOcclusionLevels", "0", "Ambient occlusion off", "saves ~0.3-0.8 ms"),
                 ("r.DepthOfFieldQuality", "0", "Depth of field off", "no background blur"),
                 ("r.SceneColorFringeQuality", "0", "Chromatic aberration off", "no colour fringes"),
                 ("r.FilmGrain", "0", "Film grain off", "a cleaner image"),
                 ("r.Tonemapper.Sharpen", "0.5", "Sharpen", "offsets DLSS softness")]

kernel32.K32EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD)]


def game_process_running():
    """Whether The Isle's process exists at all, window or not: it rewrites its ini files on exit."""
    pids = (wintypes.DWORD * 8192)()
    size = wintypes.DWORD()
    if not kernel32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(size)):
        return False
    return any(pid_name(pid) == GAME_EXE for pid in pids[:size.value // 4])


class GameIni:
    """One of the game's ini files, edited line by line. Unreal's inis repeat keys (ActionMappings),
    so a parser would mangle them; everything not set here is kept exactly as it was."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                self.lines = f.read().splitlines()
        except FileNotFoundError:
            self.lines = [";METADATA=(Diff=true, UseCommands=true)"]
        self.original = list(self.lines)

    def _section(self, section):
        """(header index, end index) of [section], or None. The end includes trailing blank lines."""
        start = None
        for i, line in enumerate(self.lines):
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                if start is not None:
                    return start, i
                if s[1:-1] == section:
                    start = i
        return None if start is None else (start, len(self.lines))

    def _find(self, section, key):
        span = self._section(section)
        if span:
            for i in range(span[0] + 1, span[1]):
                k, sep, _ = self.lines[i].partition("=")
                if sep and k.strip() == key:
                    return i
        return None

    def get(self, section, key):
        i = self._find(section, key)
        return None if i is None else self.lines[i].partition("=")[2].strip()

    def set(self, section, key, value):
        """Sets section.key to value; None removes the key, and the section once it's empty."""
        i = self._find(section, key)
        if i is not None:
            if value is not None:
                self.lines[i] = f"{key}={value}"
                return
            del self.lines[i]
            start, end = self._section(section)
            if not any(line.strip() for line in self.lines[start + 1:end]):
                del self.lines[start:end]
        elif value is not None:
            span = self._section(section)
            if span:
                end = span[1]
                while end > span[0] + 1 and not self.lines[end - 1].strip():
                    end -= 1
                self.lines.insert(end, f"{key}={value}")
            else:
                if self.lines and self.lines[-1].strip():
                    self.lines.append("")
                self.lines += [f"[{section}]", f"{key}={value}", ""]

    def changed(self):
        return self.lines != self.original

    def save(self):
        """Writes the file if anything changed. The first write keeps the file as it was in .bak."""
        if not self.changed():
            return False
        backup = self.path + ".bak"
        if os.path.exists(self.path) and not os.path.exists(backup):
            with open(self.path, "rb") as src, open(backup, "wb") as dst:
                dst.write(src.read())
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("\n".join(self.lines) + "\n")
        self.original = list(self.lines)
        return True


# --- Adaptive brightness -----------------------------------------------------

# How far dark scenes are brightened: the screen gamma at full darkness (1.0 = unchanged).
BRIGHTNESS_LEVELS = {"Off": 1.0, "Low": 1.35, "Medium": 1.7, "High": 2.2}
GAMMA_BACKUP = os.path.join(APP_DIR, "isle_reconnect_gamma.bin")  # your own ramp, while the brightener runs
gdi32.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
GammaRamp = wintypes.WORD * 768  # red, green, blue: 256 entries each


class AutoBrightness:
    """Brightens the screen while the game shows a dark scene, through the display's gamma ramp.

    The ramp is applied on the way to the monitor, after Windows has composed the screen, so the
    picture measured here (and every capture the bot takes) is never affected by it. It only acts while
    the game is the foreground window, and puts your own ramp back otherwise. A copy of that ramp sits
    in GAMMA_BACKUP from the first boost until the brightener stops, so if the process is killed, the
    next start puts it back."""

    DARK, LIGHT = 12, 60  # mean brightness (0-255) of the middle of the picture: full boost .. none

    def __init__(self):
        self.peak = 1.0  # the gamma at full darkness
        self.gamma = 1.0  # what's applied now
        self.ceiling = None  # the most Windows has accepted, once it has refused one
        self.thread, self.halt = None, threading.Event()

    def set_level(self, level):
        self.peak = BRIGHTNESS_LEVELS.get(level, 1.0)
        if self.peak > 1.0 and not (self.thread and self.thread.is_alive()):
            self.halt.clear()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        elif self.peak == 1.0:
            self.stop()

    def stop(self):
        self.halt.set()
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(3)

    def _run(self):
        screen = user32.GetDC(None)
        original = GammaRamp()
        try:
            if os.path.exists(GAMMA_BACKUP):  # the last run was killed with our ramp applied
                with open(GAMMA_BACKUP, "rb") as f:
                    ctypes.memmove(original, f.read(ctypes.sizeof(original)), ctypes.sizeof(original))
                gdi32.SetDeviceGammaRamp(screen, original)
            elif not gdi32.GetDeviceGammaRamp(screen, original):
                log("Adaptive brightness: Windows won't share the screen's gamma; it stays off.")
                return
            game, last = Game(), time.time()
            while not self.halt.wait(0.5):
                now, target, step = time.time(), 1.0, 1.0  # tabbed out: your own screen back at once
                if game.find() and user32.GetForegroundWindow() == game.hwnd:
                    level = self._scene_level(game)
                    if level is not None:
                        t = min(max((self.LIGHT - level) / (self.LIGHT - self.DARK), 0.0), 1.0)
                        target = min(1.0 + t * (self.peak - 1.0), self.ceiling or 9.0)
                    step = min(1.0, (now - last) / 1.5)  # eases over ~1.5 s, so a flash doesn't flicker it
                last = now
                wanted = self.gamma + (target - self.gamma) * step
                if abs(wanted - target) < 0.01:
                    wanted = target
                if abs(wanted - self.gamma) >= 0.01 or (wanted == target and wanted != self.gamma):
                    self._apply(screen, original, wanted)
        finally:
            gdi32.SetDeviceGammaRamp(screen, original)
            self.gamma = 1.0
            try:
                os.remove(GAMMA_BACKUP)
            except OSError:
                pass
            user32.ReleaseDC(None, screen)

    def _scene_level(self, game):
        """Mean brightness of the middle of the game's picture, read small and fast from the screen."""
        w, h = game.client_size()
        if w < 200 or h < 200:
            return None
        sx, sy = game.to_screen(w * 0.2, h * 0.2)  # the middle 60%: no HUD, no chat
        mem, bits, bmp, old = _dib(48, 27)
        try:
            src = user32.GetDC(None)
            gdi32.SetStretchBltMode(mem, 4)  # HALFTONE: each pixel averages the area it covers
            gdi32.StretchBlt(mem, 0, 0, 48, 27, src, sx, sy, int(w * 0.6), int(h * 0.6), 0x00CC0020)
            user32.ReleaseDC(None, src)
            data = ctypes.string_at(bits, 48 * 27 * 4)
        finally:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
        return sum(0.114 * data[i] + 0.587 * data[i + 1] + 0.299 * data[i + 2]
                   for i in range(0, len(data), 4)) / (48 * 27)

    def _apply(self, screen, original, gamma):
        """Your ramp raised to 1/gamma. Windows refuses ramps that stray too far from normal unless
        the GdiIcmGammaRange registry value allows it, so a refusal lowers the ceiling and retries."""
        if gamma > 1.0 and not os.path.exists(GAMMA_BACKUP):
            with open(GAMMA_BACKUP, "wb") as f:
                f.write(bytes(original))
        capped = False
        while True:
            ramp = GammaRamp(*(round(65535 * (v / 65535) ** (1 / gamma)) for v in original))
            if gdi32.SetDeviceGammaRamp(screen, ramp) or gamma <= 1.0:
                self.gamma = gamma
                if capped:
                    log(f"Adaptive brightness: Windows caps how far it can brighten the screen, at "
                        f"gamma {gamma:.2f} here. The GdiIcmGammaRange registry value lifts the cap.")
                return
            self.ceiling, gamma, capped = max(1.0, gamma - 0.05), max(1.0, gamma - 0.05), True


# --- Commands ----------------------------------------------------------------


def load_config(args):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if isinstance(cfg.get(k), dict) and k != "labels":  # yours over the defaults, key by key
                cfg[k] = {**cfg[k], **v}
            elif k != "labels":
                cfg[k] = v
        for key, words in saved.get("labels", {}).items():  # your labels first, then any new defaults
            cfg["labels"][key] = words + [w for w in cfg["labels"].get(key, []) if w not in words]
    if getattr(args, "server", None):
        cfg["server"] = args.server
    if getattr(args, "rejoin_after_leave", False):
        cfg["rejoin_after_leave"] = True
    save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def cmd_status(args):
    gl = GameLog(args.log)
    running = Game().find() is not None
    print(f"Game running: {running}\nLog state:    {gl.state}\nLast server:  {gl.server_ip}")


def scan(ocr_scale):
    """OCR the game window once, enlarged like the bot does; returns the report as lines of text."""
    game = Game()
    if not game.find():
        sys.exit("Game window not found.")
    image = game.capture(ocr_scale)
    lines = Ocr().read(image)
    return [f"{game.client_size()}, {len(lines)} text lines:"] + [
        f"  ({line.center[0]:5.0f},{line.center[1]:5.0f})  {line.text}" for line in lines]


def cmd_scan(args):
    print("\n".join(scan(load_config(args)["ocr_scale"])))


def cmd_stats(args):
    game = Game()
    if not game.find():
        sys.exit("Game window not found.")
    estimate = read_stats(game)
    exact = {}
    if args.exact:
        print("Opening chat to read the exact values from the tooltips...")
        exact = read_exact(game, Ocr(), list(STAT_ICONS))
    for stat, value in estimate.items():
        shown = "icon not on screen" if value is None else f"{value}% from the icon"
        if args.exact:
            shown += ", " + (f"{exact[stat]:g}% exact" if stat in exact else "tooltip not read")
        print(f"{stat.capitalize():7} {shown}")


def cmd_stop(args):
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    h = kernel32.OpenEventW(0x0002, False, STOP_EVENT)  # EVENT_MODIFY_STATE
    if not h:
        sys.exit("No bot is running.")
    kernel32.SetEvent(h)
    print("Stop signal sent - the bot exits within a second.")


class Bot:
    """The join / rejoin loop. run() blocks until the bot stops; stop() and toggle_pause()
    can be called from any thread (the GUI's buttons). Quitting via the hotkey or the
    'stop' command also ends run()."""

    def __init__(self, cfg, log_path=LOG_PATH, debug=False, exit_on_join=False):
        self.cfg, self.log_path, self.debug, self.exit_on_join = cfg, log_path, debug, exit_on_join
        self.paused = False
        self.attempts = 0
        self.joins = 0
        # Mirrors of the game's state from its log, for the GUI.
        self.state, self.since, self.server_ip = "starting", time.time(), None
        self.game_running = None  # unknown until the first check
        self.started_at = self.ended_at = None
        self.events = collections.deque(maxlen=5000)  # (time, kind) for the GUI's attempt strip
        self.stats, self.stats_at = {}, 0.0  # last hunger / thirst reading
        self.armed, self.low = set(STAT_ICONS), {}  # see _watch_stats
        self.safe_log_started, self.safe_log_tries = None, 0
        self._stop = threading.Event()
        self._toggle = threading.Event()
        self._wake = threading.Event()  # cuts a sleep short so the buttons respond at once

    def stop(self):
        self._stop.set()
        self._wake.set()

    def toggle_pause(self):
        self._toggle.set()
        self._wake.set()

    def _sleep(self, seconds):
        self._wake.wait(seconds)
        self._wake.clear()

    def run(self):
        # One bot at a time: two copies fight over the mouse.
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        mutex = kernel32.CreateMutexW(None, False, "Local\\isle_reconnect_bot")
        if ctypes.get_last_error() == 183 or kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(mutex)
            sys.exit("Another copy of the bot is already running - stop it with:  python isle_reconnect.py stop")
        stop_event = kernel32.CreateEventW(None, True, False, STOP_EVENT)
        kernel32.ResetEvent(stop_event)
        keys = None
        try:
            if not self.cfg["server"]:
                sys.exit('Tell it which server once:  python isle_reconnect.py run --server "part of the name"')
            keys = Hotkeys(self.cfg["hotkeys"])
            self._loop(keys, stop_event)
        finally:
            if keys:
                keys.close()
            kernel32.CloseHandle(stop_event)
            kernel32.CloseHandle(mutex)  # lets the GUI start the bot again
            self.ended_at = time.time()

    def _loop(self, keys, stop_event):
        cfg = self.cfg
        self.game, self.ocr, self.gl = Game(), Ocr(), GameLog(self.log_path)
        self.nav = Navigator(cfg, self.game, self.ocr, debug=self.debug)
        hk = cfg["hotkeys"]
        self.started_at = time.time()
        self.join_started, self.launched_at, self.last_seen_game = None, 0.0, time.time()
        self.hud_seen_at, self.hud_checked_at, self.overlay_at, self.hung_since = time.time(), 0.0, 0.0, None
        log(f"Bot running for '{cfg['server']}'. {hk['quit']} quit, {hk['pause']} pause "
            f"(or: python isle_reconnect.py stop).")
        if self.gl.state == "ingame":
            self.events.append((time.time(), "ingame"))
            log("Already in game - standing by, will rejoin if you get disconnected.")

        errors = 0
        while True:
            self.state, self.since, self.server_ip = self.gl.state, self.gl.since, self.gl.server_ip
            if keys.take("quit"):
                log(f"Stopped ({hk['quit']} pressed).")
                return
            if self._stop.is_set():
                log("Stopped.")
                return
            if kernel32.WaitForSingleObject(stop_event, 0) == 0:
                log("Stopped (stop command).")
                return
            if keys.take("pause") or self._toggle.is_set():
                self._toggle.clear()
                self.paused = not self.paused
                log("Paused." if self.paused else "Resumed.")
                self.nav.reset()
            try:
                if self._step():
                    return
                errors = 0
            except Exception as e:  # one bad pass mustn't end the bot; back off and carry on
                errors += 1
                wait = min(60, 2 ** errors)
                log(f"Error: {e!r} - carrying on in {wait} s")
                self.nav.reset()
                self._sleep(wait)

    def _step(self):
        """One pass of the loop. Returns True when the bot should stop."""
        cfg, game, gl, nav = self.cfg, self.game, self.gl, self.nav
        for ev in gl.poll():
            if self._on_event(ev):
                return True

        if self.paused:
            self._sleep(0.1)
            return False

        now = time.time()
        self.game_running = bool(game.find())
        if not self.game_running:
            if now - self.last_seen_game > 5 and now - self.launched_at > cfg["launch_timeout"]:
                log("Game is not running - launching it through Steam")
                os.startfile(STEAM_LAUNCH)
                self.launched_at = now
                nav.reset()
            self._sleep(1)
            return False
        self.last_seen_game = now

        if game.hung():  # "Not Responding": nothing on screen will change, so restart it
            self.hung_since = self.hung_since or now
            if now - self.hung_since > 300:  # map loads can freeze the window for a while
                log("Game has been frozen (Not Responding) for 5 min - closing it so it can be relaunched")
                game.kill()
                self.hung_since = None
            self._sleep(1)
            return False
        self.hung_since = None

        if gl.state == "ingame":
            self._check_ingame(now)
        if gl.state == "ingame":
            self._watch_stats(game, now)
        if gl.state == "joining" and now - gl.since > cfg["join_timeout"]:
            log("No join result for a while - checking the screen")
            gl._set("menu")
        if gl.state == "loading" and now - gl.since > 60:  # the log never said the menu loaded
            where = self._where()
            log("Stuck loading according to the game's log - the screen shows "
                + {"ingame": "you in game", "menu": "the menu"}.get(where, "nothing recognisable"))
            gl._set("ingame" if where == "ingame" else "menu")
            self.hud_seen_at = now
        if gl.state == "menu" or (gl.state == "starting" and now - gl.since > 20):
            try:
                nav.tick()
            except OSError as e:
                log(f"Capture/OCR hiccup: {e}")
            # Escalate when nothing on screen is recognised: Esc for a stray popup or overlay,
            # then restart the game. A starting game can sit on loading screens, so wait longer.
            idle = now - max(nav.last_seen, gl.since)
            if gl.state == "menu" and idle > 45 and not nav.esc_sent:
                log("Still nothing recognisable - pressing Esc in case a popup is in the way")
                game.focus()
                game.tap(0x1B)
                nav.esc_sent = True
            if idle > (180 if gl.state == "menu" else cfg["launch_timeout"] + 180):
                log(f"Stuck on an unrecognised screen for {idle / 60:.0f} min - restarting the game")
                game.kill()
                nav.reset()
            self._sleep(0.05)
        else:
            self._sleep(0.1)
        return False

    def _on_event(self, ev):
        """Reacts to a game event from the log. Returns True when the bot should stop."""
        hk = self.cfg["hotkeys"]
        self.events.append((time.time(), ev))
        if self.safe_log_started and ev in ("left", "disconnected", "menu", "closed"):
            self.safe_log_started, self.safe_log_tries = None, 0
            self.paused = True
            log(f"Safe-logged out. Paused - {hk['pause']} to rejoin.")
        elif ev == "joining":
            self.attempts += 1
            self.join_started = time.time()
            log(f"Attempt #{self.attempts}: connecting to {self.gl.server_ip}...")
        elif ev == "join_failed":
            took = time.time() - self.join_started if self.join_started else 0
            log(f"Attempt #{self.attempts} rejected after {took:.1f}s (server full?) - retrying")
        elif ev == "ingame":
            log(f"IN GAME after {self.attempts} attempt(s).")
            self.joins += 1
            self.hud_seen_at = time.time()
            if self.exit_on_join:
                return True
            self.attempts = 0
        elif ev == "disconnected":
            log("Disconnected from the server - rejoining")
        elif ev == "left":
            if self.cfg["rejoin_after_leave"]:
                log("Left the server - rejoining")
            else:
                self.paused = True
                log(f"You left the server yourself - paused ({hk['pause']} to rejoin).")
        elif ev == "closed":
            if not self.cfg["relaunch_after_quit"]:
                self.paused = True
                log(f"Game was closed normally - paused ({hk['pause']} to relaunch and rejoin).")
        elif ev == "menu":
            self.nav.reset()
        return False

    def _where(self):
        """Where the game really is, judged from the screen: "ingame" (the HUD is showing),
        "menu" (title screen or server list), or None."""
        if any(v is not None for v in read_stats(self.game).values()):
            return "ingame"
        lines = self.ocr.read(self.game.capture(self.cfg.get("ocr_scale", 1.0)))
        L = self.cfg["labels"]
        if any(find_any(lines, L[key])[0] for key in ("browser", "to_browser", "any_key")):
            return "menu"
        return None

    def _check_ingame(self, now):
        """The log says in game. If the HUD has been gone for 2 minutes and the screen shows the
        menu, a disconnect went unlogged (or unrecognised): treat it as one."""
        if now - self.hud_checked_at < 30:
            return
        self.hud_checked_at = now
        if any(v is not None for v in read_stats(self.game).values()):
            self.hud_seen_at = now
            return
        if now - self.hud_seen_at < 120:
            return
        self.hud_seen_at = now  # look again in 2 minutes, not every pass
        if self._where() == "menu":
            log("The game's log says in game, but the screen shows the menu - treating it as a disconnect")
            self.gl._set("menu")
            self._on_event("disconnected")

    def _clear_overlays(self, close_chat):
        """Closes the pause menu, which blurs the HUD (wrong readings) and swallows keys, and if
        close_chat also chat, which would take the safe-log keys as text. True if it closed one."""
        game = self.game
        image = game.capture()
        if close_chat and chat_open(image):
            log("Chat is open - clearing and closing it")
            for _ in range(100):  # posted Ctrl+A isn't seen as held, so empty it key by key
                game.post_key(0x08, 0)
            game.post_key(VK_RETURN)  # empty, so nothing is sent
            time.sleep(0.4)
            return True
        if find_any(self.ocr.read(image), self.cfg["labels"]["resume"])[0]:
            log("Pause menu is open - closing it (Esc)")
            game.post_key(0x1B)
            time.sleep(0.6)
            return True
        return False

    def _watch_stats(self, game, now):
        """Safe-logs once hunger or thirst drops to its limit. After a safe-log a stat only counts
        again once it has been seen above its limit, so rejoining while still hungry doesn't log
        you straight back out."""
        sl = self.cfg["safe_log"]
        limits = {stat: sl[stat] for stat in STAT_ICONS if sl[stat] > 0}
        if not limits or now - self.stats_at < 5:
            return
        self.stats_at = now
        if self.safe_log_started:
            if now - self.safe_log_started < 90:  # the server lets you out after 60 s asleep
                return
            if self.safe_log_tries >= 3:
                self.safe_log_started, self.paused = None, True
                log(f"Safe-log didn't work after 3 tries - paused. Is {sl['key']} your rest key?")
                return
            self._safe_log(game, "Still in game 90 s after safe-logging - trying again")
            return
        if now - self.overlay_at >= 30:
            self.overlay_at = now
            if self._clear_overlays(close_chat=False):
                return  # read again next pass, with the HUD unblurred
        try:
            # The icon estimate is within about a point of the game's own value, so the bot never
            # needs focus or the tooltip: everything here works with the game in the background.
            self.stats = read_stats(game)
        except OSError:
            return
        for stat, limit in limits.items():
            value = self.stats.get(stat)
            if value is None:
                continue
            if value > limit:
                self.armed.add(stat)
                self.low[stat] = 0
            elif stat not in self.armed:
                if stat not in self.low:
                    log(f"{stat.capitalize()} is still at {value:g}% after the last safe-log - "
                        f"auto safe-log for it restarts once it's above {limit}%")
                    self.low[stat] = 0
            elif value == 0:  # the icon turned red: empty, nothing to wait for
                self._safe_log(game, f"{stat.capitalize()} is empty (limit {limit}%)")
                return
            else:
                self.low[stat] = self.low.get(stat, 0) + 1
                if self.low[stat] >= 3:  # three readings in a row, so a stray frame can't log you out
                    self._safe_log(game, f"{stat.capitalize()} at {value}% (limit {limit}%)")
                    return

    def _safe_log(self, game, why):
        key = self.cfg["safe_log"]["key"]
        log(f"{why} - safe-logging: resting ({key}), then holding {key} to sleep")
        self._clear_overlays(close_chat=True)  # the keys must reach the dino, not chat or a menu
        game.hold_key(key, 0.1)
        time.sleep(2.5)  # let the rest animation finish
        game.hold_key(key, float(self.cfg["safe_log"]["hold_seconds"]))
        self.safe_log_started = time.time()
        self.safe_log_tries += 1
        self.armed.clear()  # rejoining still hungry mustn't log you straight back out
        self.low.clear()


def cmd_run(args):
    Bot(load_config(args), args.log, debug=args.debug, exit_on_join=args.exit_on_join).run()


# --- GUI ---------------------------------------------------------------------


def hide_own_console():
    """When the exe is double-clicked Windows opens a console just for it; close that one.
    A terminal you started it from has other processes in it and is left alone."""
    own = {os.getpid()}
    if getattr(sys, "frozen", False) and pid_name(os.getppid()) == pid_name(os.getpid()):
        own.add(os.getppid())  # a one-file exe runs as two processes: the unpacker and Python
    procs = (wintypes.DWORD * 8)()
    n = kernel32.GetConsoleProcessList(procs, len(procs))
    if 0 < n <= len(procs) and set(procs[:n]) <= own:
        kernel32.FreeConsole()
        sys.stdout = sys.stderr = open(os.devnull, "w")


# The data-terminal style (.taste-skill/direction.json): true-neutral darks with cards a step darker
# than the ground, figures in a monospace, colour only on data and status, and every selection marked
# by one 1px accent line.
GROUND, SURFACE, RAISED = "#171719", "#131217", "#212023"
BORDER, CONTROL, GRID = "#222224", "#3a3a3e", "#232327"
TEXT, MUTED = "#ffffff", "#8a8c92"
ACCENT, POSITIVE, NEGATIVE, WARNING = "#4a8ad6", "#44a282", "#e5566f", "#d9a441"
SESSIONS = ["Official", "Unofficial", "Favourites", "History"]
OCR_SCALES = [1.0, 1.5, 2.0, 2.5, 3.0]
LABEL_NAMES = {  # what each group of button texts is for, in the order the bot meets them
    "any_key": "Start screen prompt", "to_browser": "Way to the server list",
    "browser": "Proof you're in the server list", "search": "Server name filter", "refresh": "Refresh",
    "join": "Join button", "confirm": "Confirm a join", "dismiss": "Close a popup",
}
STRIP_SPAN = 30 * 60  # seconds of history in the attempt strip

LOG_COLOURS = [  # first match wins; routine retries stay grey so a real problem stands out
    (MUTED, ("OCR:", "rejected after")),
    (POSITIVE, ("IN GAME", "Already in game")),
    (NEGATIVE, ("Disconnected", "Error", "not found", "already running", "hiccup", "No Windows OCR",
                "Unknown hotkey", "frozen", "Stuck")),
    (WARNING, ("Paused", "paused", "Stopped", "launching", "already taken", "safe-log")),
]


def mix(a, b, t):
    """Blend two #rrggbb colours; t=0 gives a, t=1 gives b."""
    a, b = int(a[1:], 16), int(b[1:], 16)
    return "#" + "".join(f"{round(((a >> s) & 255) * (1 - t) + ((b >> s) & 255) * t):02x}" for s in (16, 8, 0))


def app_icon(size=64):
    """The window / taskbar icon, drawn here so there's no image file to ship:
    an accent ring around a dot on a dark rounded square."""
    import tkinter
    img = tkinter.PhotoImage(width=size, height=size)
    c, rows, clear = (size - 1) / 2, [], []
    for y in range(size):
        row = []
        for x in range(size):
            dx, dy = abs(x - c), abs(y - c)
            d = (dx * dx + dy * dy) ** 0.5
            ring = (size * 0.055 - abs(d - size * 0.3)) + 0.5  # each shape fades out over one pixel
            dot = (size * 0.13 - d) + 0.5
            row.append(mix(GROUND, ACCENT, min(max(ring, dot, 0.0), 1.0)))
            if max(dx - size * 0.3, 0) ** 2 + max(dy - size * 0.3, 0) ** 2 > (size * 0.2) ** 2:
                clear.append((x, y))  # outside the rounded corners
        rows.append("{" + " ".join(row) + "}")
    img.put(" ".join(rows))
    for x, y in clear:
        img.transparency_set(x, y, True)
    return img


def show_tab(items, name):
    for n, (tab, line) in items.items():
        tab.configure(text_color=TEXT if n == name else MUTED)
        line.configure(bg=ACCENT if n == name else BORDER)


def clock(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def hhmm(t, seconds=False):
    return time.strftime("%H:%M:%S" if seconds else "%H:%M", time.localtime(t))


def join_attempts(events):
    """Each join attempt with how it ended: [start, end or None, "rejected" / "joined" / None]."""
    out = []
    for t, ev in events:
        if ev == "joining":
            out.append([t, None, None])
        elif out and out[-1][1] is None and ev in ("join_failed", "ingame"):
            out[-1][1:] = [t, "joined" if ev == "ingame" else "rejected"]
    return out


def game_spans(events, end):
    """(start, end) of each stretch spent in game."""
    spans, start = [], None
    for t, ev in events:
        if ev == "ingame" and start is None:
            start = t
        elif ev in ("disconnected", "left", "closed", "joining", "menu") and start is not None:
            spans.append((start, t))
            start = None
    if start is not None:
        spans.append((start, end))
    return spans


class App:
    """A window around Bot: what it's doing, its recent join attempts, the log and the settings."""

    def __init__(self, log_path):
        import queue
        import tkinter as tk
        import tkinter.font as tkfont
        try:
            import customtkinter as ctk
            from customtkinter.windows.widgets.scaling import ScalingTracker
        except ImportError:
            sys.exit("The window needs customtkinter:  pip install customtkinter")

        self.ctk, self.tk, self.log_path = ctk, tk, log_path
        self.bot, self.thread, self.was_running, self.dialog = None, None, None, None
        self.hover_x, self.ticks = None, 0
        self.lines = queue.Queue()
        global log_sink
        log_sink = self.lines.put  # log() is called from the bot's thread; Tk is drained on ours

        cfg = load_config(None)
        self.hotkeys = cfg["hotkeys"]
        self.brightness = AutoBrightness()
        self.brightness.set_level(cfg["adaptive_brightness"])
        ctk.set_appearance_mode("dark")
        self.root = root = ctk.CTk(fg_color=GROUND)
        root.title("Isle Auto-Connect")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.icon = app_icon()
        root.iconphoto(True, self.icon)
        root._iconbitmap_method_called = True  # stops customtkinter swapping in its own icon
        k = self.scale = ScalingTracker.get_widget_scaling(root)

        def px(v):  # plain Tk widgets aren't scaled by customtkinter; these are
            return max(1, round(v * k))

        families = set(tkfont.families(root))
        mono = next((f for f in ("Cascadia Mono", "Consolas") if f in families), "Courier New")
        # The log in a lighter cut of the same face, so the figures stay the loudest type on screen.
        log_face = mono + " SemiLight" if mono + " SemiLight" in families else mono
        semi = "Segoe UI Semibold" if "Segoe UI Semibold" in families else "Segoe UI"

        def font(size, family="Segoe UI"):
            return ctk.CTkFont(family=family, size=size)

        self.tick_font = tkfont.Font(family=mono, size=-px(11))
        self.note_font = tkfont.Font(family="Segoe UI", size=-px(12))

        def card(parent, **grid):
            frame = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=6, border_width=1, border_color=BORDER)
            frame.grid(**grid)
            return frame

        def card_header(parent, title, note=None):
            head = ctk.CTkFrame(parent, fg_color="transparent")
            head.grid(row=0, column=0, columnspan=9, sticky="ew", padx=20, pady=(14, 6))
            ctk.CTkLabel(head, text=title, font=font(16, semi), text_color=TEXT, height=24).pack(side="left")
            if note:
                ctk.CTkLabel(head, text=note, font=font(13), text_color=MUTED, height=24).pack(side="left", padx=(8, 0))
            return head

        def text_button(parent, text, command):
            return ctk.CTkButton(parent, text=text, command=command, width=0, height=28, corner_radius=14,
                                 font=font(13), fg_color="transparent", hover_color=RAISED, text_color=MUTED)

        def underline_tabs(parent, options, on_pick):
            """Options as underline tabs: the selection is a 1px accent line under its label, on a
            full-width 1px rule. Returns the frame and {option: (label, line)} for show_tab()."""
            tabs = ctk.CTkFrame(parent, fg_color="transparent")
            tk.Frame(tabs, height=1, bg=BORDER).grid(row=1, column=0, columnspan=2 * len(options), sticky="ew")
            tabs.grid_columnconfigure(2 * len(options) - 1, weight=1)
            items = {}
            for i, name in enumerate(options):
                if i:
                    tabs.grid_columnconfigure(2 * i - 1, minsize=px(18))
                tab = ctk.CTkLabel(tabs, text=name, font=font(13), height=28, cursor="hand2")
                tab.grid(row=0, column=2 * i, sticky="w")
                tab.bind("<Button-1>", lambda e, n=name: on_pick(n))
                line = tk.Frame(tabs, height=1, bg=BORDER)
                line.grid(row=1, column=2 * i, sticky="ew")
                items[name] = (tab, line)
            return tabs, items

        self.font, self.px, self.semi, self.mono = font, px, semi, mono
        self.text_button, self.underline_tabs = text_button, underline_tabs

        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(2, weight=1)

        # Top bar: the product, and the global hotkeys that work while the game has focus.
        bar = ctk.CTkFrame(root, fg_color=GROUND, height=48, corner_radius=0)
        bar.grid(row=0, column=0, sticky="ew", padx=32)
        mark = tk.Canvas(bar, width=px(20), height=px(20), bg=GROUND, highlightthickness=0)
        mark.create_oval(px(2), px(2), px(18), px(18), outline=ACCENT, width=px(2))
        mark.create_oval(px(7), px(7), px(13), px(13), fill=ACCENT, width=0)
        mark.pack(side="left", pady=px(13))
        ctk.CTkLabel(bar, text="Isle Auto-Connect", font=font(14, semi), text_color=TEXT).pack(side="left", padx=(10, 0))
        ctk.CTkLabel(bar, text="for The Isle: Evrima", font=font(13), text_color=MUTED).pack(side="left", padx=(8, 0))
        text_button(bar, "Config file", self.edit_config).pack(side="right", padx=(16, 0))
        self.key_chips = {}
        for action, what in (("pause", "Pause"), ("quit", "Stop")):  # packed right to left
            ctk.CTkLabel(bar, text=what, font=font(12), text_color=MUTED).pack(side="right", padx=(6, 14))
            self.key_chips[action] = ctk.CTkLabel(bar, text=self.hotkeys[action], font=font(12, mono),
                                                  text_color=TEXT, fg_color=RAISED, corner_radius=4, width=34, height=22)
            self.key_chips[action].pack(side="right")
        tk.Frame(root, height=1, bg=BORDER).grid(row=1, column=0, sticky="ew")

        page = ctk.CTkFrame(root, fg_color="transparent")
        page.grid(row=2, column=0, sticky="nsew", padx=32, pady=(24, 28))
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(4, weight=1)

        # Title row: the server is the object this screen is about; the finding sits under it.
        title = ctk.CTkFrame(page, fg_color="transparent")
        title.grid(row=0, column=0, columnspan=2, sticky="ew")
        title.grid_columnconfigure(0, weight=1)
        self.h1 = ctk.CTkLabel(title, text="", font=font(26, semi), text_color=TEXT, height=34, anchor="w")
        self.h1.grid(row=0, column=0, sticky="w")
        self.finding = ctk.CTkLabel(title, text="", font=font(14), text_color=MUTED, height=20, anchor="w")
        self.finding.grid(row=1, column=0, sticky="w", pady=(2, 0))
        actions = ctk.CTkFrame(title, fg_color="transparent")
        actions.grid(row=0, column=1, rowspan=2, sticky="e")
        self.pause_btn = ctk.CTkButton(actions, text="Pause", command=self.pause, width=92, height=34,
                                       corner_radius=17, font=font(13, semi), fg_color="transparent",
                                       hover_color=RAISED, border_width=1, border_color=CONTROL,
                                       text_color=TEXT, text_color_disabled=MUTED)
        self.pause_btn.pack(side="left")
        self.start_btn = ctk.CTkButton(actions, text="Start", command=self.start_stop, width=92, height=34,
                                       corner_radius=17, font=font(13, semi), border_width=1)
        self.start_btn.pack(side="left", padx=(10, 0))

        # KPIs: one card split by 1px rules. The status word is the answer, so it leads.
        kpis = card(page, row=1, column=0, columnspan=2, sticky="ew", pady=(24, 0))
        self.kpi = {}
        for i, (key, label) in enumerate([("status", "Status"), ("attempts", "Attempts this streak"),
                                          ("joins", "Times joined"), ("uptime", "Watching for")]):
            if i:
                tk.Frame(kpis, width=1, bg=BORDER).grid(row=0, column=2 * i - 1, sticky="ns", pady=px(16))
            kpis.grid_columnconfigure(2 * i, weight=1, uniform="kpi")
            cell = ctk.CTkFrame(kpis, fg_color="transparent")
            cell.grid(row=0, column=2 * i, sticky="nsew", padx=20, pady=(14, 14))
            ctk.CTkLabel(cell, text=label, font=font(12), text_color=MUTED, height=16, anchor="w").pack(anchor="w")
            line = ctk.CTkFrame(cell, fg_color="transparent")
            line.pack(anchor="w", fill="x")
            if key == "status":
                self.dot = tk.Canvas(line, width=px(8), height=px(38), bg=SURFACE, highlightthickness=0)
                self.dot.create_oval(0, px(17), px(8), px(25), fill=MUTED, width=0, tags="dot")
                self.dot.pack(side="left", padx=(0, px(10)))
                value = ctk.CTkLabel(line, text="", font=font(28, semi), text_color=TEXT, height=38, anchor="w")
            else:
                value = ctk.CTkLabel(line, text="", font=font(20, mono), text_color=TEXT, height=38, anchor="sw")
            value.pack(side="left")
            sub = ctk.CTkLabel(cell, text="", font=font(12, mono), text_color=MUTED, height=16, anchor="w")
            sub.pack(anchor="w")
            self.kpi[key] = (value, sub)

        # The attempt strip: every join attempt of the last half hour as a tick, time in game as a bar.
        strip_card = card(page, row=2, column=0, columnspan=2, sticky="ew", pady=(24, 0))
        strip_card.grid_columnconfigure(0, weight=1)
        head = card_header(strip_card, "Join attempts", "last 30 minutes")
        self.legend = {}
        for kind, label in (("dropped", "Dropped"), ("game", "In game"), ("attempt", "Attempt")):
            item = ctk.CTkFrame(head, fg_color="transparent")
            sw = tk.Canvas(item, width=px(12), height=px(12), bg=SURFACE, highlightthickness=0)
            if kind == "game":
                sw.create_rectangle(0, px(4), px(12), px(10), fill=POSITIVE, width=0)
            else:
                sw.create_line(px(6), 0, px(6), px(12), fill=ACCENT if kind == "attempt" else NEGATIVE, width=px(2))
            sw.pack(side="left")
            ctk.CTkLabel(item, text=label, font=font(12), text_color=MUTED, height=24).pack(side="left", padx=(6, 0))
            self.legend[kind] = item
        self.strip = tk.Canvas(strip_card, height=px(84), bg=SURFACE, highlightthickness=0)
        self.strip.grid(row=1, column=0, sticky="ew", padx=px(20), pady=(px(4), px(16)))
        self.strip.bind("<Configure>", lambda e: self._draw_strip())
        self.strip.bind("<Motion>", self._hover)
        self.strip.bind("<Leave>", self._hover)

        # Activity (the log) beside the settings.
        activity = card(page, row=4, column=0, sticky="nsew", pady=(24, 0), padx=(0, 24))
        activity.grid_columnconfigure(0, weight=1)
        activity.grid_rowconfigure(1, weight=1)
        head = card_header(activity, "Activity")
        text_button(head, "Clear", self.clear).pack(side="right")
        text_button(head, "Scan screen", self.scan).pack(side="right", padx=(0, 4))
        self.view = ctk.CTkTextbox(activity, fg_color=SURFACE, corner_radius=0, border_width=0, height=120,
                                   font=font(12, log_face), text_color=TEXT, wrap="word", state="disabled",
                                   scrollbar_button_color=RAISED, scrollbar_button_hover_color=CONTROL)
        self.view.grid(row=1, column=0, sticky="nsew", padx=(12, 6), pady=(0, 10))
        self.view.tag_config("time", foreground=MUTED)
        indent = tkfont.Font(family=log_face, size=-px(12)).measure("00:00:00  ")  # wrapped lines hang under the text
        for colour in (TEXT, MUTED, POSITIVE, NEGATIVE, WARNING):
            self.view.tag_config(colour, foreground=colour, lmargin2=indent)

        settings = card(page, row=4, column=1, sticky="n", pady=(24, 0))
        settings.grid_columnconfigure(0, weight=1)
        head = card_header(settings, "Settings")
        self.locked = ctk.CTkLabel(head, text="", font=font(12), text_color=MUTED, height=24)
        self.more_btn = text_button(head, "More settings", self.open_settings)
        self.more_btn.pack(side="right")

        def label(text, row, top=14):
            ctk.CTkLabel(settings, text=text, font=font(13), text_color=MUTED, height=18, anchor="w").grid(
                row=row, column=0, sticky="w", padx=20, pady=(top, 6))

        label("Server name, or part of it", 1, top=4)
        self.server = ctk.CTkEntry(settings, width=300, height=34, corner_radius=6, font=font(13),
                                   fg_color=RAISED, border_width=1, border_color=CONTROL, text_color=TEXT,
                                   placeholder_text="as shown in the server list", placeholder_text_color=MUTED)
        self.server.grid(row=2, column=0, sticky="ew", padx=20)
        if cfg["server"]:
            self.server.insert(0, cfg["server"])
        self.server.bind("<Return>", lambda e: self.start_stop() if not self.running() else None)

        label("Session filter", 3)
        tabs, self.tabs = underline_tabs(settings, SESSIONS, self._pick_session)
        tabs.grid(row=4, column=0, sticky="ew", padx=20)
        self.session = cfg["session_filter"]
        self._pick_session(self.session)

        self.rejoin = ctk.BooleanVar(value=cfg["rejoin_after_leave"])
        self.relaunch = ctk.BooleanVar(value=cfg["relaunch_after_quit"])
        self.exit_on_join = ctk.BooleanVar(value=False)
        self.debug = ctk.BooleanVar(value=False)
        self.switches = []
        rows = [(self.rejoin, "Rejoin after I leave myself"), (self.relaunch, "Relaunch the game if it closes"),
                (self.exit_on_join, "Stop once I'm in"), (self.debug, "Log what OCR reads")]
        for i, (var, text) in enumerate(rows):
            row = ctk.CTkFrame(settings, fg_color="transparent")
            row.grid(row=5 + i, column=0, sticky="ew", padx=20, pady=(16 if i == 0 else 0, 16 if i == 3 else 0))
            ctk.CTkLabel(row, text=text, font=font(13), text_color=TEXT, height=32).pack(side="left")
            sw = ctk.CTkSwitch(row, text="", variable=var, width=34, switch_width=32, switch_height=16,
                               progress_color=POSITIVE, fg_color=CONTROL, button_color=TEXT,
                               button_hover_color="#e6e6e6")
            sw.pack(side="right")
            self.switches.append(sw)

        label("Auto safe-log at, 0 = off", 9, top=0)
        limits = ctk.CTkFrame(settings, fg_color="transparent")
        limits.grid(row=10, column=0, sticky="ew", padx=20, pady=(0, 18))
        self.limits, self.readings = {}, {}
        for i, stat in enumerate(STAT_ICONS):
            ctk.CTkLabel(limits, text=stat.capitalize(), font=font(13), text_color=TEXT, height=34).grid(
                row=0, column=3 * i, sticky="w", padx=(0 if i == 0 else 20, 8))
            entry = ctk.CTkEntry(limits, width=48, height=34, corner_radius=6, font=font(13, mono), justify="right",
                                 fg_color=RAISED, border_width=1, border_color=CONTROL, text_color=TEXT)
            entry.insert(0, str(cfg["safe_log"][stat]))
            entry.grid(row=0, column=3 * i + 1)
            self.limits[stat] = entry
            self.readings[stat] = ctk.CTkLabel(limits, text="%", font=font(12), text_color=MUTED, height=34)
            self.readings[stat].grid(row=0, column=3 * i + 2, sticky="w", padx=(6, 0))

        self.server.bind("<KeyRelease>", lambda e: self._title())
        log("Ready.")
        self._title()
        self._refresh()
        root.update_idletasks()  # open at the height the content asks for; customtkinter sizes are unscaled
        height = round(root.winfo_reqheight() / k)
        root.geometry(f"1120x{height}")
        root.minsize(980, height)

    # --- actions

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start_stop(self):
        if self.running():
            self.bot.stop()
            return
        server = self.server.get().strip()
        if not server:
            log("Type part of the server's name first, as shown in the in-game server list.")
            self.server.focus_set()
            return
        try:
            limits = {stat: int(entry.get() or 0) for stat, entry in self.limits.items()}
        except ValueError:
            log("Safe-log levels are whole percents (0 turns one off).")
            return
        if not all(0 <= v <= 95 for v in limits.values()):
            log("Safe-log levels run from 0 (off) to 95 %.")
            return
        cfg = load_config(None)
        cfg["safe_log"].update(limits)
        if any(limits.values()):
            log("Auto safe-log on: " + ", ".join(f"{s} at {v}%" for s, v in limits.items() if v))
        cfg.update(server=server, session_filter=self.session,
                   rejoin_after_leave=self.rejoin.get(), relaunch_after_quit=self.relaunch.get())
        save_config(cfg)
        self.bot = Bot(cfg, self.log_path, debug=self.debug.get(), exit_on_join=self.exit_on_join.get())
        self.thread = self._background(self.bot.run)

    def pause(self):
        if self.running():
            self.bot.toggle_pause()

    def scan(self):
        self._background(lambda: [log(line) for line in scan(load_config(None)["ocr_scale"])])

    def edit_config(self):
        subprocess.Popen(["notepad.exe", CONFIG_PATH])

    def open_settings(self):
        """The settings that otherwise live only in isle_reconnect.json, and the game's own graphics
        settings, in a window of their own with a tab for each."""
        if self.running():
            return
        if self.dialog and self.dialog.winfo_exists():
            self.dialog.focus()
            return
        ctk, tk, font, semi, mono = self.ctk, self.tk, self.font, self.semi, self.mono
        cfg = load_config(None)
        top = self.dialog = ctk.CTkToplevel(self.root, fg_color=GROUND)
        top._iconbitmap_method_called = True  # keep the app's icon instead of customtkinter's
        top.after(300, lambda: top.iconphoto(False, self.icon))
        top.title("More settings")
        top.transient(self.root)
        top.geometry("600x720")
        top.minsize(540, 480)
        top.grid_columnconfigure(0, weight=1)
        top.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(top, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=32, pady=(24, 8))
        ctk.CTkLabel(head, text="More settings", font=font(20, semi), text_color=TEXT, height=28).pack(anchor="w")
        notes = {"Bot": "Saved to isle_reconnect.json. They apply the next time you press Start.",
                 "Game": "Written to The Isle's own settings files. Close the game first; "
                         "it picks them up on its next start."}
        note = ctk.CTkLabel(head, text="", font=font(13), text_color=MUTED, height=20)
        note.pack(anchor="w")

        def form():
            body = ctk.CTkScrollableFrame(top, fg_color="transparent", corner_radius=0,
                                          scrollbar_button_color=RAISED, scrollbar_button_hover_color=CONTROL)
            body.grid_columnconfigure(0, weight=1)
            return body, iter(range(1000))

        pages = {"Bot": form(), "Game": form()}

        def pick_page(name):
            for n, (body, _) in pages.items():
                if n == name:
                    body.grid(row=2, column=0, sticky="nsew", padx=(24, 8))
                else:
                    body.grid_forget()
            note.configure(text=notes[name])
            show_tab(page_tabs, name)

        tabs, page_tabs = self.underline_tabs(top, list(pages), pick_page)
        tabs.grid(row=1, column=0, sticky="ew", padx=32, pady=(4, 0))

        def place(page, widget, **grid):
            widget.grid(row=next(page[1]), column=0, **grid)
            return widget

        def section(page, title, note):
            place(page, ctk.CTkLabel(page[0], text=title, font=font(16, semi), text_color=TEXT, height=24,
                                     anchor="w"), sticky="w", padx=8, pady=(20, 0))
            place(page, ctk.CTkLabel(page[0], text=note, font=font(13), text_color=MUTED, anchor="w",
                                     justify="left", wraplength=480), sticky="w", padx=8, pady=(2, 8))

        def setting(page, text, hint=None):
            row = place(page, ctk.CTkFrame(page[0], fg_color="transparent"), sticky="ew", padx=8, pady=3)
            ctk.CTkLabel(row, text=text, font=font(13), text_color=TEXT, height=34).pack(side="left")
            if hint:
                ctk.CTkLabel(row, text=hint, font=font(12), text_color=MUTED, height=34).pack(side="left", padx=(8, 0))
            return row

        def number(row, value, unit):
            ctk.CTkLabel(row, text=unit, font=font(13), text_color=MUTED, width=16).pack(side="right", padx=(8, 0))
            entry = ctk.CTkEntry(row, width=72, height=34, corner_radius=6, font=font(13, mono), justify="right",
                                 fg_color=RAISED, border_width=1, border_color=CONTROL, text_color=TEXT)
            entry.insert(0, str(value))
            entry.pack(side="right")
            return entry

        def switch(row, on):
            var = ctk.BooleanVar(value=on)
            ctk.CTkSwitch(row, text="", variable=var, width=34, switch_width=32, switch_height=16,
                          progress_color=POSITIVE, fg_color=CONTROL, button_color=TEXT,
                          button_hover_color="#e6e6e6").pack(side="right")
            return var

        bot = pages["Bot"]
        section(bot, "Hotkeys", "Global keys that work while the game has focus.")
        keys = {}
        for action, text in (("quit", "Stop the bot"), ("pause", "Pause or resume")):
            keys[action] = ctk.StringVar(value=cfg["hotkeys"][action].upper())
            ctk.CTkOptionMenu(setting(bot, text), values=list(KEY_NAMES), variable=keys[action], width=120,
                              height=34, corner_radius=6, font=font(13, mono), dropdown_font=font(13, mono),
                              fg_color=RAISED, button_color=RAISED, button_hover_color=CONTROL, text_color=TEXT,
                              dropdown_fg_color=RAISED, dropdown_hover_color=CONTROL,
                              dropdown_text_color=TEXT).pack(side="right")

        section(bot, "Timing", "How long the bot waits before it stops trusting the game's log.")
        join_timeout = number(setting(bot, "Check the screen again after no join result for"),
                              cfg["join_timeout"], "s")
        launch_timeout = number(setting(bot, "Wait for a relaunched game for"), cfg["launch_timeout"], "s")

        section(bot, "Screen reading", "Screenshots are enlarged before OCR. Larger reads the server list's small "
                                       "font better, but each pass is slower.")
        scales = sorted(set(OCR_SCALES) | {float(cfg["ocr_scale"])})
        names = {f"{v:g}×": v for v in scales}
        scale = {"value": float(cfg["ocr_scale"])}

        def pick_scale(name):
            scale["value"] = names[name]
            show_tab(scale_tabs, name)

        tabs, scale_tabs = self.underline_tabs(bot[0], list(names), pick_scale)
        place(bot, tabs, sticky="w", padx=8, pady=(0, 4))
        pick_scale(f"{scale['value']:g}×")

        section(bot, "Button texts", "The bot finds buttons by their text. If the game renames one, add the new "
                                     "text here; separate several with commas. Yours are tried before the "
                                     "built-in ones.")
        texts = {}
        for key, name in LABEL_NAMES.items():
            built_in = DEFAULT_CONFIG["labels"][key]
            yours = [w for w in cfg["labels"].get(key, []) if w not in built_in]
            place(bot, ctk.CTkLabel(bot[0], text=name, font=font(13), text_color=TEXT, height=20, anchor="w"),
                  sticky="w", padx=8, pady=(8, 4))
            texts[key] = ctk.CTkEntry(bot[0], height=34, corner_radius=6, font=font(13), fg_color=RAISED,
                                      border_width=1, border_color=CONTROL, text_color=TEXT,
                                      placeholder_text="none of your own", placeholder_text_color=MUTED)
            if yours:
                texts[key].insert(0, ", ".join(yours))
            place(bot, texts[key], sticky="ew", padx=8)
            place(bot, ctk.CTkLabel(bot[0], text="Built in: " + ", ".join(built_in), font=font(12),
                                    text_color=MUTED, anchor="w", justify="left", wraplength=480),
                  sticky="w", padx=8, pady=(4, 0))
        place(bot, ctk.CTkFrame(bot[0], fg_color="transparent", height=16))

        # The game's own settings. Each control remembers what the file held, so saving only rewrites
        # the values you actually changed.
        game, game_ini = pages["Game"], {}
        quality, effects, tweaks, physics, choices, brightness, fps = {}, {}, {}, {}, [], {}, None
        if not os.path.exists(GAME_SETTINGS):
            section(game, "No game settings yet", "The Isle hasn't saved its settings on this PC. "
                                                  "Start it once, close it, and come back here.")
        else:
            gus = game_ini["settings"] = GameIni(GAME_SETTINGS)
            eng = game_ini["engine"] = GameIni(GAME_ENGINE)
            levels = ["Low", "Medium", "High", "Epic"]
            section(game, "Quality", "The same levels as the game's menu. Foliage and landscape cost the most "
                                     "frames; textures cost almost none on a card with 8 GB.")
            for key, name in GAME_QUALITY:
                held = gus.get("ScalabilityGroups", key)
                state = quality[key] = {"was": held, "value": held}

                def pick_level(level, state=state):
                    state["value"] = str(levels.index(level))
                    show_tab(state["items"], level)

                tabs, state["items"] = self.underline_tabs(setting(game, name), levels, pick_level)
                tabs.pack(side="right")
                # Unset, or a level the menu doesn't offer, shows no choice and is left as it is.
                show_tab(state["items"], levels[int(held)] if held in ("0", "1", "2", "3") else None)
            held = gus.get("/Script/Engine.GameUserSettings", "FrameRateLimit")
            try:
                held = float(held or 0)
            except ValueError:
                held = 0.0
            fps = (held, number(setting(game, "Frame rate limit", "0 = none"), f"{held:g}", "fps"))

            section(game, "Foliage", "Plants and grass that move cost CPU every frame, more so with players "
                                     "around. Density can't be changed: the game resets it on every map load.")
            for key, name, hint in FOLIAGE_PHYSICS:
                held = gus.get("/Script/Engine.GameUserSettings", key)
                physics[key] = (held, switch(setting(game, name, hint or None), held == "True"))

            def choice(name, hint, presets):
                """A row of Engine.ini presets; shows the one the file matches (none if set by hand)."""
                keys = {key for preset in presets.values() for key in preset}
                held = {key: eng.get(*key) for key in keys}
                current = next((label for label, preset in presets.items()
                                if all(held[key] == preset.get(key) for key in keys)), None)
                state = {"was": current, "value": current, "presets": presets}

                def pick(label):
                    state["value"] = label
                    show_tab(state["items"], label)

                tabs, state["items"] = self.underline_tabs(setting(game, name, hint), list(presets), pick)
                tabs.pack(side="right")
                show_tab(state["items"], current)
                choices.append(state)

            section(game, "Fog", "Draws volumetric fog on a coarser grid: just as thick, cheaper, a little "
                                 "blockier at the edges. Written to Engine.ini, untested. With Effects on Low the "
                                 "game may skip this fog already.")
            choice(*FOG_DETAIL)

            section(game, "Visibility", "Servers ban for these: for private and sandbox servers only. All but "
                                        "adaptive brightness go to Engine.ini, untested. Grass and foliage density "
                                        "aren't here because the game resets them on every map load. Adaptive "
                                        "brightness raises the screen's gamma in dark scenes while the game is in "
                                        "front, and works as soon as you save.")
            for name, hint, presets in VISIBILITY_CHOICES:
                choice(name, hint, presets)

            def pick_brightness(level):
                brightness["value"] = level
                show_tab(brightness["items"], level)

            brightness["value"] = cfg["adaptive_brightness"]
            tabs, brightness["items"] = self.underline_tabs(setting(game, "Adaptive brightness", "live"),
                                                            list(BRIGHTNESS_LEVELS), pick_brightness)
            tabs.pack(side="right")
            show_tab(brightness["items"], brightness["value"])
            for key, value, name, hint in VISIBILITY_TWEAKS:
                held = eng.get(SYSTEM, key)
                tweaks[key] = (value, held, switch(setting(game, name, hint), held == value))

            section(game, "Hidden effects", "The game reads these from its settings file, but its menu doesn't "
                                            "show them. Turning one off saves a little GPU time.")
            for key, name, hint in HIDDEN_EFFECTS:
                held = gus.get("/Script/Engine.RendererSettings", key)
                effects[key] = (held, switch(setting(game, name, hint), held != "0"))

            section(game, "Engine tweaks", "Written to Engine.ini. Untested on this build of the game, which may "
                                           "ignore them.")
            for key, value, name, hint in ENGINE_TWEAKS:
                held = eng.get(SYSTEM, key)
                tweaks[key] = (value, held, switch(setting(game, name, hint), held == value))
            place(game, ctk.CTkLabel(game[0], text="The first change keeps a copy of each file next to it, "
                                                   "ending in .bak.", font=font(12), text_color=MUTED, anchor="w",
                                     justify="left", wraplength=480), sticky="w", padx=8, pady=(16, 0))
        place(game, ctk.CTkFrame(game[0], fg_color="transparent", height=16))
        pick_page("Bot")

        tk.Frame(top, height=1, bg=BORDER).grid(row=3, column=0, sticky="ew")
        foot = ctk.CTkFrame(top, fg_color="transparent")
        foot.grid(row=4, column=0, sticky="ew", padx=32, pady=16)
        self.text_button(foot, "Open config file", self.edit_config).pack(side="left")

        def game_changes():
            """Applies the Game tab to the ini files in memory. Returns an error, or None."""
            if not game_ini:
                return None
            gus, eng = game_ini["settings"], game_ini["engine"]
            for key, state in quality.items():
                if state["value"] != state["was"]:
                    gus.set("ScalabilityGroups", key, state["value"])
            try:
                limit = float(fps[1].get() or 0)
            except ValueError:
                return "The frame rate limit is a number (0 for none)."
            if not 0 <= limit <= 1000:
                return "The frame rate limit runs from 0 (none) to 1000."
            if abs(limit - fps[0]) > 1e-6:
                gus.set("/Script/Engine.GameUserSettings", "FrameRateLimit", f"{limit:.6f}")
            for key, (held, var) in effects.items():
                if var.get() and held == "0":
                    gus.set("/Script/Engine.RendererSettings", key, None)  # back to the game's own value
                elif not var.get() and held != "0":
                    gus.set("/Script/Engine.RendererSettings", key, "0")
            for key, (held, var) in physics.items():
                if var.get() != (held == "True"):
                    gus.set("/Script/Engine.GameUserSettings", key, "True" if var.get() else "False")
            for state in choices:
                if state["value"] != state["was"]:
                    preset = state["presets"][state["value"]]
                    for key in {key for p in state["presets"].values() for key in p}:
                        eng.set(*key, preset.get(key))
            for key, (value, held, var) in tweaks.items():
                if var.get() and held != value:
                    eng.set(SYSTEM, key, value)
                elif not var.get() and held == value:
                    eng.set(SYSTEM, key, None)
            if any(ini.changed() for ini in game_ini.values()) and game_process_running():
                for ini in game_ini.values():
                    ini.lines = list(ini.original)
                return "Close The Isle first: it rewrites its settings files when it exits."
            return None

        def save():
            if keys["quit"].get() == keys["pause"].get():
                return error.configure(text="Stop and pause need different keys.")
            try:
                waits = int(join_timeout.get()), int(launch_timeout.get())
            except ValueError:
                return error.configure(text="Waits are whole seconds.")
            if not (10 <= waits[0] <= 3600 and 30 <= waits[1] <= 3600):
                return error.configure(text="Waits run from 10 s (30 s for a relaunch) to 3600 s.")
            problem = game_changes()
            if problem:
                pick_page("Game")
                return error.configure(text=problem)
            try:
                written = [os.path.basename(ini.path) for ini in game_ini.values() if ini.save()]
            except OSError as e:
                return error.configure(text=f"Couldn't write the game's settings: {e.strerror}")
            if written:
                log("Game settings written to " + " and ".join(written) + ". They apply when The Isle starts.")
            cfg = load_config(None)  # fresh, so nothing the main window saved since is lost
            cfg["hotkeys"] = {action: var.get() for action, var in keys.items()}
            cfg["join_timeout"], cfg["launch_timeout"] = waits
            cfg["ocr_scale"] = scale["value"]
            if brightness:
                cfg["adaptive_brightness"] = brightness["value"]
                self.brightness.set_level(brightness["value"])
            for key, entry in texts.items():
                words = [w.strip() for w in entry.get().split(",") if w.strip()]
                seen = {w.upper() for w in words}
                cfg["labels"][key] = words + [w for w in DEFAULT_CONFIG["labels"][key] if w.upper() not in seen]
            save_config(cfg)
            self.hotkeys = cfg["hotkeys"]
            for action, chip in self.key_chips.items():
                chip.configure(text=self.hotkeys[action])
            log("Settings saved.")
            top.destroy()

        pill = dict(height=34, corner_radius=17, font=font(13, semi), border_width=1)
        ctk.CTkButton(foot, text="Save", command=save, width=92, fg_color=TEXT, hover_color="#e2e2e4",
                      border_color=TEXT, text_color="#000000", **pill).pack(side="right")
        ctk.CTkButton(foot, text="Cancel", command=top.destroy, width=92, fg_color="transparent",
                      hover_color=RAISED, border_color=CONTROL, text_color=TEXT, **pill).pack(side="right", padx=(0, 10))
        error = ctk.CTkLabel(foot, text="", font=font(12), text_color=NEGATIVE, height=34)
        error.pack(side="right", padx=(0, 16))
        top.bind("<Escape>", lambda e: top.destroy())
        top.after(150, lambda: (top.lift(), top.focus_force(), top.grab_set()))  # modal once it's on screen

    def clear(self):
        self.view.configure(state="normal")
        self.view.delete("1.0", "end")
        self.view.configure(state="disabled")

    def close(self):
        self.brightness.stop()  # your own screen gamma back
        if self.running():
            self.bot.stop()
            self.thread.join(2)
        self.root.destroy()

    def _pick_session(self, name):
        if self.running():
            return
        self.session = name
        show_tab(self.tabs, name)

    def _background(self, work):
        def guarded():
            try:
                work()
            except SystemExit as e:  # the CLI's error exits; here they're just messages
                log(str(e.code))
            except Exception as e:
                log(f"Error: {e!r}")
        thread = threading.Thread(target=guarded, daemon=True)
        thread.start()
        return thread

    # --- drawing

    def _title(self):
        server = self.bot.cfg["server"] if self.running() else self.server.get().strip()
        self.h1.configure(text=server or "No server chosen", text_color=TEXT if server else MUTED)

    def _append(self, lines):
        at_bottom = self.view.yview()[1] > 0.99
        self.view.configure(state="normal")
        for line in lines:
            stamp, _, msg = line.partition("] ")
            colour = next((c for c, words in LOG_COLOURS if any(w in msg for w in words)), TEXT)
            self.view.insert("end", stamp.lstrip("[") + "  ", "time")
            self.view.insert("end", msg + "\n", colour)
        if int(self.view.index("end-1c").split(".")[0]) > 2000:
            self.view.delete("1.0", "500.0")
        self.view.configure(state="disabled")
        if at_bottom:
            self.view.see("end")

    def _status(self, running):
        """The status word, its dot colour, how long it has held, and the finding under the title."""
        bot, now = self.bot, time.time()
        if bot is None:
            return "Idle", MUTED, "", "Not watching the game yet."
        events = list(bot.events)
        tries = sum(ev == "joining" for _, ev in events)
        if not running:
            ended = bot.ended_at or now
            return ("Stopped", MUTED, f"at {hhmm(ended)}",
                    f"Stopped at {hhmm(ended)} after {tries} attempt{'s' * (tries != 1)}"
                    f" and {bot.joins} join{'s' * (bot.joins != 1)}.")
        held = f"for {clock(now - bot.since)}"
        if bot.paused:
            return "Paused", WARNING, "", f"Paused. {self.hotkeys['pause']} or Resume carries on."
        if bot.game_running is False and bot.state != "ingame":
            return "No game", WARNING, "", "The Isle isn't running. Waiting for it to start."
        last_in = max((t for t, ev in events if ev == "ingame"), default=0)
        rejected = [t for t, ev in events if ev == "join_failed" and t > last_in]
        streak = (f"{len(rejected)} attempt{'s' * (len(rejected) != 1)} rejected since {hhmm(rejected[0])}, "
                  "probably full" if rejected else "")
        ip = bot.server_ip or "the server"
        if bot.state == "ingame":
            return "In game", POSITIVE, held, f"In game on {ip} since {hhmm(bot.since)}."
        if bot.state == "joining":
            return "Joining", ACCENT, held, f"Attempt {bot.attempts} on {ip}" + (f". {streak}." if streak else ".")
        if bot.state in ("menu", "loading"):
            return ("In menus" if bot.state == "menu" else "Loading", ACCENT, held,
                    streak + "." if streak else f"Looking for “{bot.cfg['server']}” in the server list.")
        if bot.state == "closed":
            return "Closed", MUTED, held, "The game was closed."
        return "Waiting", ACCENT, held, "Waiting for the game to reach its menus."

    def _refresh(self):
        lines = []
        while not self.lines.empty():
            lines.append(self.lines.get())
        if lines:
            self._append(lines)

        running, bot, now = self.running(), self.bot, time.time()
        word, colour, held, finding = self._status(running)
        self.dot.itemconfigure("dot", fill=colour)
        value, sub = self.kpi["status"]
        value.configure(text=word)
        sub.configure(text=held)
        self.finding.configure(text=finding)
        if bot:
            events = list(bot.events)
            first = [t for t, ev in events if ev == "joining" and t > max(
                (t2 for t2, e2 in events if e2 == "ingame"), default=0)]
            last_in = [t for t, ev in events if ev == "ingame"]
            stats = {"attempts": (str(bot.attempts), f"since {hhmm(first[0])}" if first else "none yet"),
                     "joins": (str(bot.joins), f"last at {hhmm(last_in[-1])}" if last_in else "none yet"),
                     "uptime": (clock((now if running else bot.ended_at or now) - bot.started_at)
                                if bot.started_at else "–",
                                f"since {hhmm(bot.started_at)}" if bot.started_at else "")}
        else:
            stats = {key: ("–", "") for key in ("attempts", "joins", "uptime")}
        for key, (v, s) in stats.items():
            self.kpi[key][0].configure(text=v, text_color=TEXT if v != "–" else MUTED)
            self.kpi[key][1].configure(text=s)

        for stat, reading in self.readings.items():  # what the bot last read from the HUD
            value = bot.stats.get(stat) if running and bot else None
            reading.configure(text="%" if value is None else f"%  now {value:g}%")

        self.pause_btn.configure(text="Resume" if running and bot.paused else "Pause")
        if running != self.was_running:  # settings are locked while the bot uses them
            self.was_running = running
            if running:
                self.start_btn.configure(text="Stop", fg_color="transparent", hover_color=RAISED,
                                         border_color=CONTROL, text_color=TEXT)
            else:  # the one filled control: inverted white, as the style's primary
                self.start_btn.configure(text="Start", fg_color=TEXT, hover_color="#e2e2e4",
                                         border_color=TEXT, text_color="#000000")
            self.pause_btn.configure(state="normal" if running else "disabled")
            self.server.configure(state="disabled" if running else "normal",
                                  text_color=MUTED if running else TEXT)
            for sw in self.switches:
                sw.configure(state="disabled" if running else "normal")
            for entry in self.limits.values():
                entry.configure(state="disabled" if running else "normal", text_color=MUTED if running else TEXT)
            for tab, _ in self.tabs.values():
                tab.configure(cursor="arrow" if running else "hand2")
            self.locked.configure(text="Locked while running" if running else "")
            (self.more_btn if running else self.locked).pack_forget()
            (self.locked if running else self.more_btn).pack(side="right")
            self._title()
        self.ticks += 1
        if self.ticks % 5 == 0:
            self._draw_strip()
        self.root.after(100, self._refresh)

    def _hover(self, event):
        self.hover_x = event.x if str(event.type) == "Motion" else None
        self._draw_strip()

    def _draw_strip(self):
        c, k = self.strip, self.scale
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50:
            return
        c.delete("all")
        now, bot = time.time(), self.bot
        t0 = now - STRIP_SPAN
        events = list(bot.events) if bot else []
        end = now if self.running() or not bot else (bot.ended_at or now)
        base, top = h - 20 * k, 2 * k

        def x_of(t):
            return (t - t0) / STRIP_SPAN * (w - 2) + 1

        # Five-minute grid, labelled on the minute, first and last label pulled inside the plot.
        for t in range((int(t0) // 300 + 1) * 300, int(now) + 1, 300):
            x = x_of(t)
            c.create_line(x, top, x, base, fill=GRID)
            anchor = "nw" if x < 24 * k else "ne" if x > w - 24 * k else "n"
            c.create_text(x, base + 5 * k, text=hhmm(t), fill=MUTED, font=self.tick_font, anchor=anchor)

        spans = [(max(a, t0), b) for a, b in game_spans(events, end) if b > t0]
        for a, b in spans:
            c.create_rectangle(x_of(a), base - 8 * k, max(x_of(b), x_of(a) + 2), base, fill=POSITIVE, width=0)
        marks = []
        for start, stop, outcome in join_attempts(events):
            if start >= t0:
                marks.append((x_of(start), start, stop, outcome or "pending"))
        for t, ev in events:
            if ev == "disconnected" and t >= t0:
                marks.append((x_of(t), t, None, "dropped"))
        hovered = min(marks, key=lambda m: abs(m[0] - self.hover_x), default=None) if self.hover_x is not None else None
        if hovered and abs(hovered[0] - self.hover_x) > 8 * k:
            hovered = None
        for m in marks:
            x = m[0]
            colour = TEXT if m is hovered else NEGATIVE if m[3] == "dropped" else ACCENT
            c.create_line(x, top if m[3] == "dropped" else base - 34 * k, x, base, fill=colour, width=max(1, round(2 * k)))
        c.create_line(0, base, w, base, fill=CONTROL)  # the baseline, the one heavier line

        if not marks and not spans:
            note = c.create_text(w / 2, (top + base) / 2, text="No join attempts in the last 30 minutes.",
                                 fill=MUTED, font=self.note_font)
            x1, y1, x2, y2 = c.bbox(note)
            c.tag_lower(c.create_rectangle(x1 - 12 * k, y1, x2 + 12 * k, y2, fill=SURFACE, width=0), note)
        if hovered:
            x, start, stop, outcome = hovered
            text = {"rejected": f"Rejected after {stop - start:.1f} s" if stop else "Rejected",
                    "joined": f"Joined after {stop - start:.1f} s" if stop else "Joined",
                    "pending": "Connecting…", "dropped": "Dropped from the server"}[outcome]
            label = f"{hhmm(start, seconds=True)}  {text}"
            tx = x + 8 * k if x < w / 2 else x - 8 * k
            item = c.create_text(tx, top + 4 * k, text=label, fill=TEXT, font=self.note_font,
                                 anchor="nw" if x < w / 2 else "ne")
            x1, y1, x2, y2 = c.bbox(item)
            box = c.create_rectangle(x1 - 6 * k, y1 - 3 * k, x2 + 6 * k, y2 + 3 * k, fill=RAISED, width=0)
            c.tag_lower(box, item)

        shown = {"attempt": any(m[3] != "dropped" for m in marks), "game": bool(spans),
                 "dropped": any(m[3] == "dropped" for m in marks)}
        for kind in ("dropped", "game", "attempt"):
            self.legend[kind].pack_forget()
        for kind in ("dropped", "game", "attempt"):
            if shown[kind]:
                self.legend[kind].pack(side="right", padx=(14, 0))


def cmd_gui(args):
    hide_own_console()
    App(args.log).root.mainloop()


def main():
    if sys.stdout:  # None in an exe built without a console
        sys.stdout.reconfigure(errors="replace")  # OCR text can contain characters the console can't show
    p = argparse.ArgumentParser(description="Fully automated join/rejoin bot for The Isle (Evrima).")
    p.add_argument("--log", default=LOG_PATH, help="path to TheIsle.log")
    sub = p.add_subparsers(dest="cmd")  # no command: open the window
    sub.add_parser("gui", help="open the window (the default with no command)")
    r = sub.add_parser("run", help="join and keep rejoining automatically")
    r.add_argument("--server", help="part of the server's name as shown in the server list (saved)")
    r.add_argument("--exit-on-join", action="store_true", help="stop once you're in")
    r.add_argument("--rejoin-after-leave", action="store_true",
                   help="rejoin even when you leave / safe-log yourself (saved)")
    r.add_argument("--debug", action="store_true", help="print what OCR reads each pass")
    sub.add_parser("scan", help="OCR the game window once and print the text found")
    sub.add_parser("status", help="show the game state from the log")
    st = sub.add_parser("stats", help="read hunger and thirst from the HUD once")
    st.add_argument("--exact", action="store_true",
                    help="also read the game's exact tooltip values to check the estimate (opens chat)")
    sub.add_parser("stop", help="stop a running bot (no hotkey needed)")
    args = p.parse_args()
    commands = {"run": cmd_run, "scan": cmd_scan, "status": cmd_status, "stop": cmd_stop, "stats": cmd_stats,
                "gui": cmd_gui}
    commands[args.cmd or "gui"](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
