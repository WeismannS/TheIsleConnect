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
        "to_browser": ["PLAY", "SERVER BROWSER", "SERVERS", "BROWSE SERVERS", "MULTIPLAYER",
                       "FIND SERVER", "COMMUNITY"],
    },
    "hotkeys": {"quit": "F10", "pause": "F11"},  # any of F1-F24, PAUSE, SCROLLLOCK, INSERT, HOME, END
    "join_timeout": 90,       # seconds without a result before navigating again
    "launch_timeout": 240,    # seconds to wait for a relaunched game
    "rejoin_after_leave": False,
    "relaunch_after_quit": False,
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
        self.last_seen, self.warned = time.time(), False


# --- Commands ----------------------------------------------------------------


def load_config(args):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k != "labels"})
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


def scan():
    """OCR the game window once; returns the report as lines of text."""
    game = Game()
    if not game.find():
        sys.exit("Game window not found.")
    image = game.capture(DEFAULT_CONFIG["ocr_scale"])
    lines = Ocr().read(image)
    return [f"{game.client_size()}, {len(lines)} text lines:"] + [
        f"  ({line.center[0]:5.0f},{line.center[1]:5.0f})  {line.text}" for line in lines]


def cmd_scan(args):
    print("\n".join(scan()))


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
        game, ocr, gl = Game(), Ocr(), GameLog(self.log_path)
        nav = Navigator(cfg, game, ocr, debug=self.debug)
        hk = cfg["hotkeys"]
        self.started_at = time.time()
        join_started, launched_at, last_seen_game = None, 0.0, time.time()
        log(f"Bot running for '{cfg['server']}'. {hk['quit']} quit, {hk['pause']} pause "
            f"(or: python isle_reconnect.py stop).")
        if gl.state == "ingame":
            self.events.append((time.time(), "ingame"))
            log("Already in game - standing by, will rejoin if you get disconnected.")

        while True:
            self.state, self.since, self.server_ip = gl.state, gl.since, gl.server_ip
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
                nav.reset()

            for ev in gl.poll():
                self.events.append((time.time(), ev))
                if ev == "joining":
                    self.attempts += 1
                    join_started = time.time()
                    log(f"Attempt #{self.attempts}: connecting to {gl.server_ip}...")
                elif ev == "join_failed":
                    took = time.time() - join_started if join_started else 0
                    log(f"Attempt #{self.attempts} rejected after {took:.1f}s (server full?) - retrying")
                elif ev == "ingame":
                    log(f"IN GAME after {self.attempts} attempt(s).")
                    self.joins += 1
                    if self.exit_on_join:
                        return
                    self.attempts = 0
                elif ev == "disconnected":
                    log("Disconnected from the server - rejoining")
                elif ev == "left":
                    if cfg["rejoin_after_leave"]:
                        log("Left the server - rejoining")
                    else:
                        self.paused = True
                        log(f"You left the server yourself - paused ({hk['pause']} to rejoin).")
                elif ev == "closed":
                    if not cfg["relaunch_after_quit"]:
                        self.paused = True
                        log(f"Game was closed normally - paused ({hk['pause']} to relaunch and rejoin).")
                elif ev == "menu":
                    nav.reset()

            if self.paused:
                self._sleep(0.1)
                continue

            now = time.time()
            self.game_running = bool(game.find())
            if not self.game_running:
                if now - last_seen_game > 5 and now - launched_at > cfg["launch_timeout"]:
                    log("Game is not running - launching it through Steam")
                    os.startfile(STEAM_LAUNCH)
                    launched_at = now
                    nav.reset()
                self._sleep(1)
                continue
            last_seen_game = now

            if gl.state == "joining" and now - gl.since > cfg["join_timeout"]:
                log("No join result for a while - checking the screen")
                gl.state = "menu"
            if gl.state == "menu" or (gl.state == "starting" and now - gl.since > 20):
                try:
                    nav.tick()
                except OSError as e:
                    log(f"Capture/OCR hiccup: {e}")
                self._sleep(0.05)
            else:
                self._sleep(0.1)


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
STRIP_SPAN = 30 * 60  # seconds of history in the attempt strip

LOG_COLOURS = [  # first match wins; routine retries stay grey so a real problem stands out
    (MUTED, ("OCR:", "rejected after")),
    (POSITIVE, ("IN GAME", "Already in game")),
    (NEGATIVE, ("Disconnected", "Error", "not found", "already running", "hiccup", "No Windows OCR",
                "Unknown hotkey")),
    (WARNING, ("Paused", "paused", "Stopped", "launching", "already taken")),
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

        self.log_path = log_path
        self.bot, self.thread, self.was_running = None, None, None
        self.hover_x, self.ticks = None, 0
        self.lines = queue.Queue()
        global log_sink
        log_sink = self.lines.put  # log() is called from the bot's thread; Tk is drained on ours

        cfg = load_config(None)
        self.hotkeys = cfg["hotkeys"]
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
        for key, what in ((self.hotkeys["pause"], "Pause"), (self.hotkeys["quit"], "Stop")):  # packed right to left
            ctk.CTkLabel(bar, text=what, font=font(12), text_color=MUTED).pack(side="right", padx=(6, 14))
            ctk.CTkLabel(bar, text=key, font=font(12, mono), text_color=TEXT, fg_color=RAISED,
                         corner_radius=4, width=34, height=22).pack(side="right")
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
        self.locked.pack(side="right")

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

        # Session filter as underline tabs: the selection is a 1px accent line under its label,
        # on a full-width 1px rule.
        label("Session filter", 3)
        tabs = ctk.CTkFrame(settings, fg_color="transparent")
        tabs.grid(row=4, column=0, sticky="ew", padx=20)
        tk.Frame(tabs, height=1, bg=BORDER).grid(row=1, column=0, columnspan=2 * len(SESSIONS), sticky="ew")
        tabs.grid_columnconfigure(2 * len(SESSIONS) - 1, weight=1)
        self.session = cfg["session_filter"]
        self.tabs = {}
        for i, name in enumerate(SESSIONS):
            if i:
                tabs.grid_columnconfigure(2 * i - 1, minsize=px(18))
            tab = ctk.CTkLabel(tabs, text=name, font=font(13), height=28, cursor="hand2")
            tab.grid(row=0, column=2 * i, sticky="w")
            tab.bind("<Button-1>", lambda e, n=name: self._pick_session(n))
            line = tk.Frame(tabs, height=1, bg=BORDER)
            line.grid(row=1, column=2 * i, sticky="ew")
            self.tabs[name] = (tab, line)
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
        cfg = load_config(None)
        cfg.update(server=server, session_filter=self.session,
                   rejoin_after_leave=self.rejoin.get(), relaunch_after_quit=self.relaunch.get())
        save_config(cfg)
        self.bot = Bot(cfg, self.log_path, debug=self.debug.get(), exit_on_join=self.exit_on_join.get())
        self.thread = self._background(self.bot.run)

    def pause(self):
        if self.running():
            self.bot.toggle_pause()

    def scan(self):
        self._background(lambda: [log(line) for line in scan()])

    def edit_config(self):
        subprocess.Popen(["notepad.exe", CONFIG_PATH])

    def clear(self):
        self.view.configure(state="normal")
        self.view.delete("1.0", "end")
        self.view.configure(state="disabled")

    def close(self):
        if self.running():
            self.bot.stop()
            self.thread.join(2)
        self.root.destroy()

    def _pick_session(self, name):
        if self.running():
            return
        self.session = name
        for n, (tab, line) in self.tabs.items():
            tab.configure(text_color=TEXT if n == name else MUTED)
            line.configure(bg=ACCENT if n == name else BORDER)

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
            for tab, _ in self.tabs.values():
                tab.configure(cursor="arrow" if running else "hand2")
            self.locked.configure(text="Locked while running" if running else "")
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
    sub.add_parser("stop", help="stop a running bot (no hotkey needed)")
    args = p.parse_args()
    commands = {"run": cmd_run, "scan": cmd_scan, "status": cmd_status, "stop": cmd_stop, "gui": cmd_gui}
    commands[args.cmd or "gui"](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
