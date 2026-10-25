"""
isle_reconnect.py - fully automated join / rejoin bot for The Isle (Evrima).

Start it once and leave it running:

    python isle_reconnect.py run --server "part of the server name"

What it does on its own:
  * Knows what the game is doing from its own log file (instant, never misreads):
    joining, join failed (e.g. server full), in game, disconnected, closed.
  * Navigates the menus with Windows' built-in OCR: finds buttons by their text
    (Play, the server in the list, Join, OK...), so no click recording is needed
    and resolution / window size doesn't matter.
  * Retries the moment a join fails, rejoins after a disconnect, and relaunches
    the game through Steam if it crashes.

Hotkeys (work while the game has focus):  F10 quit   F11 pause / resume

Other commands:
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
import ctypes
import difflib
import json
import os
import re
import sys
import time
from ctypes import wintypes

GAME_EXE = "theisleclient-win64-shipping.exe"
STEAM_LAUNCH = "steam://rungameid/376210"
LOG_PATH = os.path.expandvars(r"%LOCALAPPDATA%\TheIsle\Saved\Logs\TheIsle.log")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "isle_reconnect.json")

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
    "join_timeout": 90,       # seconds without a result before navigating again
    "launch_timeout": 240,    # seconds to wait for a relaunched game
    "rejoin_after_leave": False,
    "relaunch_after_quit": False,
}

VK_F10, VK_F11, VK_RETURN = 0x79, 0x7A, 0x0D


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


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
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
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


class Hotkeys:
    def __init__(self, *vks):
        self.down = {vk: False for vk in vks}

    def pressed(self, vk):
        now = bool(user32.GetAsyncKeyState(vk) & 0x8000)
        hit = now and not self.down[vk]
        self.down[vk] = now
        return hit


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
            print(" | ".join(l.text for l in lines)[:300])
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
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def cmd_status(args):
    gl = GameLog(args.log)
    running = Game().find() is not None
    print(f"Game running: {running}\nLog state:    {gl.state}\nLast server:  {gl.server_ip}")


def cmd_scan(args):
    game = Game()
    if not game.find():
        sys.exit("Game window not found.")
    image = game.capture(DEFAULT_CONFIG["ocr_scale"])
    lines = Ocr().read(image)
    print(f"{game.client_size()}, {len(lines)} text lines:")
    for line in lines:
        print(f"  ({line.center[0]:5.0f},{line.center[1]:5.0f})  {line.text}")


def cmd_run(args):
    # One bot at a time: two copies fight over the mouse.
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    _mutex = kernel32.CreateMutexW(None, False, "Local\\isle_reconnect_bot")
    if ctypes.get_last_error() == 183 or kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit("Another copy of the bot is already running - stop it first (F10 in game).")
    cfg = load_config(args)
    if not cfg["server"]:
        sys.exit('Tell it which server once:  python isle_reconnect.py run --server "part of the name"')
    game, ocr, gl = Game(), Ocr(), GameLog(args.log)
    nav = Navigator(cfg, game, ocr, debug=args.debug)
    keys = Hotkeys(VK_F10, VK_F11)
    paused = False
    attempts, join_started, launched_at, last_seen_game = 0, None, 0.0, time.time()
    log(f"Bot running for '{cfg['server']}'. F10 quit, F11 pause.")
    if gl.state == "ingame":
        log("Already in game - standing by, will rejoin if you get disconnected.")

    while True:
        if keys.pressed(VK_F10):
            log("Stopped.")
            return
        if keys.pressed(VK_F11):
            paused = not paused
            log("Paused." if paused else "Resumed.")
            nav.reset()

        for ev in gl.poll():
            if ev == "joining":
                attempts += 1
                join_started = time.time()
                log(f"Attempt #{attempts}: connecting to {gl.server_ip}...")
            elif ev == "join_failed":
                took = time.time() - join_started if join_started else 0
                log(f"Attempt #{attempts} rejected after {took:.1f}s (server full?) - retrying")
            elif ev == "ingame":
                log(f"IN GAME after {attempts} attempt(s).")
                if args.exit_on_join:
                    return
                attempts = 0
            elif ev == "disconnected":
                log("Disconnected from the server - rejoining")
            elif ev == "left":
                if cfg["rejoin_after_leave"]:
                    log("Left the server - rejoining")
                else:
                    paused = True
                    log("You left the server yourself - paused (F11 to rejoin).")
            elif ev == "closed":
                if not cfg["relaunch_after_quit"]:
                    paused = True
                    log("Game was closed normally - paused (F11 to relaunch and rejoin).")
            elif ev == "menu":
                nav.reset()

        if paused:
            time.sleep(0.1)
            continue

        now = time.time()
        if not game.find():
            if now - last_seen_game > 5 and now - launched_at > cfg["launch_timeout"]:
                log("Game is not running - launching it through Steam")
                os.startfile(STEAM_LAUNCH)
                launched_at = now
                nav.reset()
            time.sleep(1)
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
            time.sleep(0.05)
        else:
            time.sleep(0.1)


def main():
    sys.stdout.reconfigure(errors="replace")  # OCR text can contain characters the console can't show
    p = argparse.ArgumentParser(description="Fully automated join/rejoin bot for The Isle (Evrima).")
    p.add_argument("--log", default=LOG_PATH, help="path to TheIsle.log")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="join and keep rejoining automatically")
    r.add_argument("--server", help="part of the server's name as shown in the server list (saved)")
    r.add_argument("--exit-on-join", action="store_true", help="stop once you're in")
    r.add_argument("--rejoin-after-leave", action="store_true",
                   help="rejoin even when you leave / safe-log yourself (saved)")
    r.add_argument("--debug", action="store_true", help="print what OCR reads each pass")
    sub.add_parser("scan", help="OCR the game window once and print the text found")
    sub.add_parser("status", help="show the game state from the log")
    args = p.parse_args()
    {"run": cmd_run, "scan": cmd_scan, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
