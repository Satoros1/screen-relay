"""
Screen Relay - desktop app that streams one window (e.g. BlueStacks)
to the relay server, which shows it live on a website / Telegram Mini App.

  * pick the window from a list, select the area with the mouse
  * captures the window even when it is covered by other windows (PrintWindow),
    falls back to screen capture automatically if the window renders black
  * auto-reconnect, auto-find the window again after the emulator restarts
  * settings are saved; optional autostart with Windows
"""
import asyncio
import hashlib
import io
import json
import os
import base64
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import ttk

import mss
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageStat, ImageTk
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidURI

try:                      # tray icon (optional - without it the app just minimizes normally)
    import pystray
except Exception:
    pystray = None

APP_NAME = "Screen Relay"
APP_VERSION = "0.1.7"
GITHUB_REPO = "Satoros1/screen-relay"
EXE_NAME = "ScreenRelay.exe"
INSTANCE_PORT = 47613     # second launch tells the running copy to show its window
IS_WIN = sys.platform == "win32"
SCREEN = "🖥  Весь экран"

DEFAULTS = {
    "server": "wss://live.example.com/ingest",
    "token": "",
    "window_title": "",
    "crop": None,            # [x0, y0, x1, y1] as fractions of the window, None = whole window
    "fps": 8,
    "quality": 70,
    "max_width": "1280",
    "start_on_launch": False,
    "minimize_to_tray": True,
    "emulator_shortcut": "",     # BlueStacks app shortcut (.lnk) to (re)start the emulator with the app
    "auto_launch": True,         # start it by itself when the window is missing
    "keep_awake": True,          # don't let the PC sleep / turn the screen off while streaming
    "remote_control": True,      # admins may click / press Esc in the streamed window from the bot
    "auto_rules": [],            # auto-click rules from the server (cached so they work right after start)
    "hide_on_minimize": True,    # minimizing the emulator hides it off-screen instead (keeps streaming)
}


# ============================================================ icon
def make_icon(dot="#888888", size=64) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((s * .06, s * .12, s * .94, s * .76), radius=s * .1,
                        fill="#1f2430", outline="#dfe3ea", width=max(2, s // 16))
    d.rectangle((s * .40, s * .76, s * .60, s * .86), fill="#dfe3ea")
    d.rounded_rectangle((s * .24, s * .84, s * .76, s * .94), radius=s * .04, fill="#dfe3ea")
    d.ellipse((s * .34, s * .27, s * .66, s * .59), fill=dot)
    return img


# ============================================================ updates
def parse_ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def http_get(url: str, headers: dict | None = None, timeout: float = 15):
    req = urllib.request.Request(url, headers={"User-Agent": f"ScreenRelay/{APP_VERSION}", **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def server_http_base(ws_url: str) -> str | None:
    """wss://live.example.com/ingest -> https://live.example.com"""
    m = re.match(r"^(wss?)://([^/]+)", ws_url.strip())
    if not m:
        return None
    return ("https" if m.group(1) == "wss" else "http") + "://" + m.group(2)


def decode_code(text: str) -> tuple[str, str] | None:
    """'sr1:<base64url json {"s": server, "t": token}>' from deploy/setup.sh -> (server, token)."""
    text = (text or "").strip()
    if not text.startswith("sr1:"):
        return None
    try:
        raw = text[4:].strip()
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        server, token = str(data["s"]).strip(), str(data["t"]).strip()
        if server.startswith(("ws://", "wss://")) and token:
            return server, token
    except Exception:
        pass
    return None


def ps_quote(x) -> str:
    return "'" + str(x).replace("'", "''") + "'"


def run_powershell(script: str, wait: bool, env: dict | None = None):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
           "-EncodedCommand", encoded]
    kw = dict(creationflags=0x08000000 | 0x00000200,   # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, env=env)
    if wait:
        return subprocess.run(cmd, timeout=30, **kw).returncode
    subprocess.Popen(cmd, **kw)
    return None


class Updater:
    """Checks GitHub Releases (or the relay server, if the repo is private) for a newer exe."""

    def __init__(self, post):
        self.post = post            # puts a callable into the GUI queue
        self.busy = False
        self.latest = None          # {"version", "url", "headers", "size"} if newer than ours

    def check(self, server: str, token: str, on_done):
        if self.busy:
            return
        self.busy = True

        def work():
            result, error = None, None
            try:
                result = self._find_latest(server, token)
            except Exception as e:
                error = str(e) or type(e).__name__
            self.busy = False
            if result and parse_ver(result["version"]) > parse_ver(APP_VERSION):
                self.latest = result
            elif result:
                self.latest = None
            self.post(lambda: on_done(self.latest, error))

        threading.Thread(target=work, daemon=True).start()

    def _find_latest(self, server: str, token: str) -> dict:
        try:   # 1) public GitHub release
            with http_get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                          {"Accept": "application/vnd.github+json"}) as r:
                rel = json.load(r)
            asset = next(a for a in rel.get("assets", []) if a["name"].lower() == EXE_NAME.lower())
            return {"version": rel["tag_name"].lstrip("v"), "url": asset["browser_download_url"],
                    "headers": {}, "size": asset["size"]}
        except urllib.error.HTTPError as e:
            if e.code not in (403, 404):
                raise
        except StopIteration:
            raise RuntimeError("в релизе нет ScreenRelay.exe")
        # 2) private repo -> ask our relay server, it has a GitHub token
        base = server_http_base(server)
        if not base or not token:
            raise RuntimeError("репозиторий приватный - нужен адрес сервера и токен")
        auth = {"Authorization": f"Bearer {token}"}
        try:
            with http_get(f"{base}/update/latest", auth) as r:
                info = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError("репозиторий приватный, а на сервере не задан GH_TOKEN")
            raise
        return {"version": info["version"], "url": f"{base}/update/download",
                "headers": auth, "size": info["size"]}

    def download(self, on_progress, on_done):
        info = self.latest
        target = Path(sys.executable).with_name("ScreenRelay.update.exe")

        def work():
            try:
                got = 0
                with http_get(info["url"], info["headers"], timeout=60) as r, open(target, "wb") as f:
                    while chunk := r.read(256 * 1024):
                        f.write(chunk)
                        got += len(chunk)
                        pct = int(got * 100 / info["size"]) if info["size"] else 0
                        self.post(lambda p=pct: on_progress(p))
                with open(target, "rb") as f:
                    if f.read(2) != b"MZ" or (info["size"] and got != info["size"]):
                        raise RuntimeError("скачанный файл повреждён")
                self.post(lambda: on_done(target, None))
            except Exception as e:
                target.unlink(missing_ok=True)
                msg = str(e) or type(e).__name__
                self.post(lambda: on_done(None, msg))

        threading.Thread(target=work, daemon=True).start()


def launch_swap_and_restart(new_exe: Path, args: list[str]) -> None:
    """Hidden PowerShell waits until we exit, replaces the exe and starts the new one."""
    q = ps_quote
    old = Path(sys.executable)
    log = Path(os.environ.get("TEMP", str(old.parent))) / "ScreenRelay-update.log"
    arglist = ",".join(q(a) for a in args) if args else "@()"
    script = f"""
$old = {q(old)}; $new = {q(new_exe)}; $log = {q(log)}
"$(Get-Date -Format s) start" | Out-File -LiteralPath $log -Encoding utf8
for ($i = 0; $i -lt 120; $i++) {{
  try {{ Move-Item -LiteralPath $new -Destination $old -Force -ErrorAction Stop; "moved after $i tries" | Out-File -LiteralPath $log -Append -Encoding utf8; break }}
  catch {{ Start-Sleep -Milliseconds 500 }}
}}
try {{ Start-Process -FilePath $old -ArgumentList {arglist} -ErrorAction Stop; "restarted" | Out-File -LiteralPath $log -Append -Encoding utf8 }}
catch {{ "restart failed: $_" | Out-File -LiteralPath $log -Append -Encoding utf8 }}
"""
    # the new exe must start as an independent app, not as a child of this PyInstaller bootloader
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_PYI_", "_MEIPASS"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    run_powershell(script, wait=False, env=env)


# ============================================================ settings
def settings_path() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "ScreenRelay"
    base.mkdir(parents=True, exist_ok=True)
    return base / "settings.json"


def load_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    except Exception:
        return dict(DEFAULTS)


def save_settings(cfg: dict) -> None:
    try:
        tmp = settings_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(settings_path())
    except Exception:
        pass


# ============================================================ Windows API
if IS_WIN:
    import ctypes
    from ctypes import wintypes as W

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32")
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        dwmapi.DwmGetWindowAttribute.argtypes = [W.HWND, W.DWORD, ctypes.c_void_p, W.DWORD]
    except Exception:
        dwmapi = None

    WNDENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

    def _sig(fn, res, *args):
        fn.restype, fn.argtypes = res, list(args)

    _sig(user32.EnumWindows, W.BOOL, WNDENUMPROC, W.LPARAM)
    _sig(user32.GetWindowTextLengthW, ctypes.c_int, W.HWND)
    _sig(user32.GetWindowTextW, ctypes.c_int, W.HWND, W.LPWSTR, ctypes.c_int)
    _sig(user32.IsWindowVisible, W.BOOL, W.HWND)
    _sig(user32.IsWindow, W.BOOL, W.HWND)
    _sig(user32.IsIconic, W.BOOL, W.HWND)
    _sig(user32.ShowWindow, W.BOOL, W.HWND, ctypes.c_int)
    _sig(user32.GetWindowRect, W.BOOL, W.HWND, ctypes.POINTER(W.RECT))
    _sig(user32.SetWindowPos, W.BOOL, W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT)
    _sig(user32.GetForegroundWindow, W.HWND)
    _sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    _sig(user32.SetForegroundWindow, W.BOOL, W.HWND)
    _sig(user32.GetCursorPos, W.BOOL, ctypes.POINTER(W.POINT))
    _sig(user32.SetCursorPos, W.BOOL, ctypes.c_int, ctypes.c_int)
    _sig(user32.mouse_event, None, W.DWORD, W.DWORD, W.DWORD, W.DWORD, ctypes.c_void_p)
    _sig(user32.keybd_event, None, W.BYTE, W.BYTE, W.DWORD, ctypes.c_void_p)
    _sig(user32.GetClientRect, W.BOOL, W.HWND, ctypes.POINTER(W.RECT))
    _sig(user32.ClientToScreen, W.BOOL, W.HWND, ctypes.POINTER(W.POINT))
    _sig(user32.GetDC, W.HDC, W.HWND)
    _sig(user32.ReleaseDC, ctypes.c_int, W.HWND, W.HDC)
    _sig(user32.PrintWindow, W.BOOL, W.HWND, W.HDC, W.UINT)
    _sig(gdi32.CreateCompatibleDC, W.HDC, W.HDC)
    _sig(gdi32.CreateCompatibleBitmap, W.HBITMAP, W.HDC, ctypes.c_int, ctypes.c_int)
    _sig(gdi32.SelectObject, W.HGDIOBJ, W.HDC, W.HGDIOBJ)
    _sig(gdi32.DeleteObject, W.BOOL, W.HGDIOBJ)
    _sig(gdi32.DeleteDC, W.BOOL, W.HDC)
    _sig(gdi32.GetDIBits, ctypes.c_int, W.HDC, W.HBITMAP, W.UINT, W.UINT,
         ctypes.c_void_p, ctypes.c_void_p, W.UINT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG),
                    ("biPlanes", W.WORD), ("biBitCount", W.WORD), ("biCompression", W.DWORD),
                    ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG),
                    ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", W.DWORD * 3)]

    def _is_cloaked(hwnd) -> bool:
        if not dwmapi:
            return False
        val = ctypes.c_int(0)
        try:
            dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            return False
        return val.value != 0

    SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10

    def window_rect(hwnd):
        r = W.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def virtual_screen():
        x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)     # SM_X/YVIRTUALSCREEN
        return x, y, x + user32.GetSystemMetrics(78), y + user32.GetSystemMetrics(79)

    def is_offscreen(hwnd) -> bool:
        l, t, r, b = window_rect(hwnd)
        vl, vt, vr, vb = virtual_screen()
        return l >= vr or r <= vl or t >= vb or b <= vt

    def move_window(hwnd, x, y):
        user32.SetWindowPos(hwnd, None, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def client_size(hwnd):
        r = W.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(r)):
            return 0, 0
        return r.right - r.left, r.bottom - r.top

    def list_windows() -> list[tuple[int, str]]:
        out = []

        def cb(hwnd, _):
            try:
                if not user32.IsWindowVisible(hwnd) or _is_cloaked(hwnd):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if not n:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value.strip()
                w, h = client_size(hwnd)
                if title and title != APP_NAME and (user32.IsIconic(hwnd) or (w >= 100 and h >= 100)):
                    out.append((hwnd, title))
            except Exception:
                pass
            return True

        proc = WNDENUMPROC(cb)   # keep a reference while EnumWindows runs
        user32.EnumWindows(proc, 0)
        return out

    def find_window(title: str):
        wins = list_windows()
        for hwnd, t in wins:
            if t == title:
                return hwnd
        low = title.lower()
        for hwnd, t in wins:
            if low and (low in t.lower() or t.lower() in low):
                return hwnd
        return None

    def screen_rect(hwnd):
        w, h = client_size(hwnd)
        pt = W.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return {"left": pt.x, "top": pt.y, "width": w, "height": h}

    def print_window(hwnd, w, h):
        """Render the window's client area even if it is covered by other windows."""
        hdc = user32.GetDC(hwnd)
        if not hdc:
            return None
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old = gdi32.SelectObject(mem, bmp)
        try:
            ok = user32.PrintWindow(hwnd, mem, 3)   # PW_CLIENTONLY | PW_RENDERFULLCONTENT
            gdi32.SelectObject(mem, old)            # GetDIBits needs the bitmap deselected
            old = None
            if not ok:
                return None
            bi = BITMAPINFO()
            bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bi.bmiHeader.biWidth = w
            bi.bmiHeader.biHeight = -h              # top-down
            bi.bmiHeader.biPlanes = 1
            bi.bmiHeader.biBitCount = 32
            buf = ctypes.create_string_buffer(w * h * 4)
            if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0) != h:
                return None
            return Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)
        finally:
            if old:
                gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(hwnd, hdc)
else:
    def list_windows():
        return []


def is_black(img: Image.Image) -> bool:
    small = img.resize((64, 64)).convert("L")
    return small.getextrema()[1] < 8


# ============================================================ capture
class WindowSource:
    """Finds the window by title and grabs it. Picks the best method by itself."""

    def __init__(self):
        self.hwnd = None
        self.title = None
        self.mode = "print"
        self.black_count = 0
        self.retry_print_at = 0.0
        self.last_find = 0.0
        self.missing_since = None     # monotonic time since the window can't be found
        self.hide_on_minimize = True
        self.hidden = False           # window parked off-screen by us
        self.home = None              # (x, y) where to put it back
        self.request = None           # "hide" / "show" from the GUI thread
        self.busy = False             # remote click in progress - hide logic must not interfere
        self.was_fg = False
        self.input_lock = threading.Lock()

    def grab(self, title: str, sct) -> tuple[Image.Image | None, str]:
        if not title:
            return None, "Выбери окно"
        if title == SCREEN or not IS_WIN:
            self.missing_since = None
            mon = sct.monitors[1]
            shot = sct.grab(mon)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"), "Захват: весь экран"

        if title != self.title or self.hwnd is None or not user32.IsWindow(self.hwnd):
            if time.monotonic() - self.last_find < 1.0 and title == self.title:
                return None, f"Окно «{title}» не найдено - жду…"
            self.last_find = time.monotonic()
            self.title = title
            self.hwnd = find_window(title)
            self.mode, self.black_count = "print", 0
            if self.hwnd is None:
                if self.missing_since is None:
                    self.missing_since = time.monotonic()
                return None, f"Окно «{title}» не найдено - жду…"
            self.missing_since = None

        msg = self._hide_logic()
        if msg:
            return None, msg

        w, h = client_size(self.hwnd)
        if w < 10 or h < 10:
            return None, "Окно слишком маленькое"

        now = time.monotonic()
        if self.mode == "screen" and now >= self.retry_print_at:
            self.mode = "print"

        if self.mode == "print":
            img = print_window(self.hwnd, w, h)
            if img is not None and not is_black(img):
                self.black_count = 0
                return img, ("Захват: окно спрятано за краем экрана (клик по нему на панели задач - показать)"
                             if self.hidden else "Захват: окно (можно перекрывать и сворачивать)")
            self.black_count += 1
            if self.black_count < 3:
                return None, "Пробую захватить окно…"
            self.mode = "screen"
            self.retry_print_at = now + 30

        if self.hidden:
            self.request = "show"
            return None, "Этот эмулятор не отдаёт картинку, когда спрятан - показываю окно обратно"
        shot = sct.grab(screen_rect(self.hwnd))
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return img, "Захват: с экрана - не перекрывай окно эмулятора"


def _wsrc_hide_logic(self) -> str | None:
    """Minimized windows are not rendered by Windows, so 'minimize' = park the window off-screen."""
    hwnd = self.hwnd
    if self.busy:
        return None
    if not self.hidden and is_offscreen(hwnd):        # left there by a previous run / update restart
        self.hidden = True
    req, self.request = self.request, None
    fg = user32.GetForegroundWindow() == hwnd
    activated, self.was_fg = fg and not getattr(self, "was_fg", False), fg   # it just got focus (taskbar click)
    if req == "show" or (self.hidden and activated and req != "hide"):
        if self.hidden:                                # user clicked it on the taskbar -> bring it back
            vl, vt, vr, vb = virtual_screen()
            x, y = self.home or (vl + 80, vt + 60)
            move_window(hwnd, x, y)
            self.hidden = False
            if req == "show":
                user32.SetForegroundWindow(hwnd)
        return None
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 4)                     # SW_SHOWNOACTIVATE
        if self.hide_on_minimize:
            req = "hide"
        else:
            return "Окно было свёрнуто - разворачиваю"
    if req == "hide" and not self.hidden:
        l, t, r, b = window_rect(hwnd)
        if l > -30000:                                 # -32000 = Windows' "minimized" position
            self.home = (l, t)
        vl, vt, vr, vb = virtual_screen()
        move_window(hwnd, vr + 200, t if vt <= t < vb else vt)
        self.hidden = True
        self.mode, self.black_count = "print", 0
    return None


WindowSource._hide_logic = _wsrc_hide_logic


def _wsrc_remote_input(self, action: str, fx: float = 0.0, fy: float = 0.0) -> tuple[bool, str]:
    """Click at (fx, fy) - fractions of the window's client area - or press Esc in the window.

    A real mouse click, so it works for anything shown in the window (Android popup or the
    emulator's own dialog). A hidden window is brought back for a moment and parked again.
    Only ever clicks inside the streamed window.
    """
    if not IS_WIN:
        return False, "работает только на Windows"
    hwnd = self.hwnd
    if not hwnd or not user32.IsWindow(hwnd):
        return False, "окно не найдено (в программе должно быть выбрано окно, а не «Весь экран»)"
    if action == "tap" and not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
        return False, "точка вне окна"
    if not self.input_lock.acquire(timeout=6):
        return False, "ещё выполняю прошлое нажатие"
    self.busy = True
    parked = False
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 4)
            time.sleep(0.3)
        was_hidden = self.hidden or is_offscreen(hwnd)
        prev_fg = user32.GetForegroundWindow()
        vl, vt, vr, vb = virtual_screen()
        if action == "tap":
            if was_hidden:
                x, y = self.home or (vl + 80, vt + 60)
                move_window(hwnd, x, y)
                parked = True
            flags = SWP_NOSIZE | 0x2 | SWP_NOACTIVATE            # 0x2 = SWP_NOMOVE
            user32.SetWindowPos(hwnd, W.HWND(-1), 0, 0, 0, 0, flags)   # above everything for the click
            time.sleep(0.25)
            r = screen_rect(hwnd)
            px = int(r["left"] + fx * max(1, r["width"] - 1))
            py = int(r["top"] + fy * max(1, r["height"] - 1))
            try:
                if not (vl <= px < vr and vt <= py < vb):
                    return False, "эта точка окна не помещается на экране ПК"
                old = W.POINT()
                user32.GetCursorPos(ctypes.byref(old))
                user32.SetCursorPos(px, py)
                time.sleep(0.06)
                user32.mouse_event(0x0002, 0, 0, 0, None)        # left down
                time.sleep(0.07)
                user32.mouse_event(0x0004, 0, 0, 0, None)        # left up
                time.sleep(0.25)
                user32.SetCursorPos(old.x, old.y)
            finally:
                user32.SetWindowPos(hwnd, W.HWND(-2), 0, 0, 0, 0, flags)   # HWND_NOTOPMOST
            msg = f"нажал в точку {round(fx * 100)}% × {round(fy * 100)}%"
        elif action == "esc":
            if user32.GetForegroundWindow() != hwnd:
                user32.keybd_event(0x12, 0, 0, None)             # Alt: lets a background app take focus
                user32.SetForegroundWindow(hwnd)
                user32.keybd_event(0x12, 0, 0x0002, None)
                time.sleep(0.3)
            if user32.GetForegroundWindow() != hwnd:
                return False, "не смог активировать окно эмулятора"
            user32.keybd_event(0x1B, 0, 0, None)                 # Esc = Android «Назад» in BlueStacks
            time.sleep(0.06)
            user32.keybd_event(0x1B, 0, 0x0002, None)
            time.sleep(0.25)
            msg = "нажал «Назад» (Esc)"
        else:
            return False, "неизвестная команда"
        if prev_fg and prev_fg != hwnd and user32.IsWindow(prev_fg):
            user32.SetForegroundWindow(prev_fg)
        return True, msg
    except Exception as e:
        return False, f"ошибка: {e}"
    finally:
        try:
            if parked:                                           # it was hidden - hide it again
                l, t, _, _ = window_rect(hwnd)
                self.home = (l, t)
                vl, vt, vr, vb = virtual_screen()
                move_window(hwnd, vr + 200, t if vt <= t < vb else vt)
                self.hidden = True
            self.was_fg = user32.GetForegroundWindow() == hwnd
        except Exception:
            pass
        self.busy = False
        self.input_lock.release()


WindowSource.remote_input = _wsrc_remote_input


class Capture(threading.Thread):
    def __init__(self, cfg: dict):
        super().__init__(daemon=True)
        self.cfg = dict(cfg)
        self.streaming = False
        self.stop_ev = threading.Event()
        self.lock = threading.Lock()
        self.preview = None
        self.preview_seq = 0
        self.jpeg = None
        self.jpeg_seq = 0
        self.out_size = (0, 0)
        self.scale = 1.0              # auto quality: 1.0 = as set, lower on a slow connection
        self.heartbeat = time.monotonic()
        self.status = "Запуск…"
        self.src = WindowSource()
        self.full_until = 0.0         # admin's control view is open: also send the whole window
        self.full_jpeg = None
        self.full_seq = 0
        self.last_img = None          # latest whole-window image (auto rules, learning)
        self.auto = AutoRules(self)

    def get_jpeg(self):
        with self.lock:
            return self.jpeg_seq, self.jpeg

    def get_full(self):
        with self.lock:
            return self.full_seq, self.full_jpeg

    def run(self):
        sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        last_digest, last_sent, last_preview, last_full = None, 0.0, 0.0, 0.0
        while not self.stop_ev.is_set():
            t0 = time.monotonic()
            self.heartbeat = t0
            cfg = self.cfg
            try:
                img, self.status = self.src.grab(cfg.get("window_title", ""), sct)
            except Exception as e:
                img, self.status = None, f"Ошибка захвата: {e}"
                self.src.hwnd = None
            if img is not None:
                self.last_img = img
                if self.streaming:
                    self.auto.check(img, t0)
                if t0 - last_preview >= 0.25:
                    pv = img.copy()
                    pv.thumbnail((1000, 1000))
                    with self.lock:
                        self.preview = pv
                        self.preview_seq += 1
                    last_preview = t0
                if self.streaming and t0 < self.full_until and t0 - last_full >= 0.6:
                    full = img.copy()
                    full.thumbnail((1280, 1280))
                    buf = io.BytesIO()
                    full.save(buf, "JPEG", quality=60)
                    with self.lock:
                        self.full_jpeg = buf.getvalue()
                        self.full_seq += 1
                    last_full = t0
                if self.streaming:
                    out = img
                    c = cfg.get("crop")
                    if c:
                        box = (round(c[0] * img.width), round(c[1] * img.height),
                               round(c[2] * img.width), round(c[3] * img.height))
                        if box[2] - box[0] >= 8 and box[3] - box[1] >= 8:
                            out = img.crop(box)
                    mw = cfg.get("max_width", "")
                    limit = int(mw) if str(mw).isdigit() else out.width
                    if self.scale < 0.8:
                        limit = max(480, int(limit * (self.scale + 0.2)))
                    if out.width > limit:
                        mw = limit
                        out = out.resize((mw, max(1, round(out.height * mw / out.width))), Image.LANCZOS)
                    digest = hashlib.blake2b(out.tobytes(), digest_size=16).digest()
                    if digest != last_digest or t0 - last_sent >= 2:
                        buf = io.BytesIO()
                        q = int(cfg.get("quality", 70))
                        out.save(buf, "JPEG", quality=max(25, int(q * self.scale)))
                        with self.lock:
                            self.jpeg = buf.getvalue()
                            self.jpeg_seq += 1
                            self.out_size = out.size
                        last_digest, last_sent = digest, t0
            fps = max(1, int(cfg.get("fps", 8))) if self.streaming else 4
            self.stop_ev.wait(max(0.005, 1 / fps - (time.monotonic() - t0)))


# ============================================================ auto-click: "this popup appeared -> press its button"
TPL_W = 64            # templates are stored this wide (grayscale), height keeps the aspect ratio
MATCH_DIFF = 16.0     # mean pixel difference (0..255) below which the popup counts as "on screen"


def tpl_region(img: Image.Image, rect) -> Image.Image:
    w, h = img.size
    x0, y0 = int(rect[0] * w), int(rect[1] * h)
    return img.crop((x0, y0, max(x0 + 2, int(rect[2] * w)), max(y0 + 2, int(rect[3] * h))))


def make_template(img: Image.Image, rect) -> Image.Image:
    reg = tpl_region(img, rect).convert("L")
    th = max(8, min(128, round(reg.height * TPL_W / max(1, reg.width))))
    return reg.resize((TPL_W, th), Image.BILINEAR).filter(ImageFilter.BoxBlur(1))


def tpl_diff(img: Image.Image, rect, tpl: Image.Image) -> float:
    reg = tpl_region(img, rect).convert("L").resize(tpl.size, Image.BILINEAR).filter(ImageFilter.BoxBlur(1))
    return ImageStat.Stat(ImageChops.difference(reg, tpl)).mean[0]


def tpl_to_b64(tpl: Image.Image) -> str:
    buf = io.BytesIO()
    tpl.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def tpl_from_b64(data: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(data))).convert("L")


class AutoRules:
    """Watches the whole window for learned popups; when one shows up, clicks its button,
    checks that it went away and queues an event for the server (which runs the timer)."""

    def __init__(self, cap):
        self.cap = cap
        self.rules: list[dict] = []
        self.state: dict[str, dict] = {}
        self.busy = False
        self.events: queue.Queue = queue.Queue(maxsize=20)
        self.last_check = 0.0
        self.last_msg = ""
        self.set(cap.cfg.get("auto_rules") or [])

    def set(self, raw: list, unarm: str | None = None) -> None:
        rules = []
        for r in raw or []:
            try:
                rect = [float(v) for v in r["rect"]]
                tap = [float(v) for v in r["tap"]]
                rules.append({"id": str(r["id"]), "name": str(r.get("name", "?")), "rect": rect, "tap": tap,
                              "tpl": tpl_from_b64(r["tpl"])})
            except Exception:
                continue
        old = self.state
        self.state = {r["id"]: old.get(r["id"], {"hits": 0, "miss": 0, "armed": True}) for r in rules}
        if unarm in self.state:          # just learned while the popup is open: wait for the next time
            self.state[unarm]["armed"] = False
        self.rules = rules

    def find(self, rule_id: str) -> dict | None:
        return next((r for r in self.rules if r["id"] == rule_id), None)

    def check(self, img: Image.Image, now: float) -> None:
        if not self.rules or self.busy or now - self.last_check < 1.0:
            return
        self.last_check = now
        for r in self.rules:
            st = self.state.setdefault(r["id"], {"hits": 0, "miss": 0, "armed": True})
            try:
                matched = tpl_diff(img, r["rect"], r["tpl"]) < MATCH_DIFF
            except Exception:
                matched = False
            if matched:
                st["hits"] += 1
                st["miss"] = 0
            else:
                st["hits"] = 0
                st["miss"] += 1
                if st["miss"] >= 3:                    # it was gone for a while: next appearance counts again
                    st["armed"] = True
            if matched and st["hits"] >= 2 and st["armed"]:
                st["armed"] = False
                self.fire(r)
                return

    def fire(self, rule: dict) -> bool:
        if self.busy:
            return False
        self.busy = True
        threading.Thread(target=self._fire, args=(rule,), daemon=True).start()
        return True

    def still_there(self, rule: dict) -> bool:
        img = self.cap.last_img
        return img is not None and tpl_diff(img, rule["rect"], rule["tpl"]) < MATCH_DIFF

    def _fire(self, rule: dict) -> None:
        ok, msg = False, ""
        try:
            if not self.cap.cfg.get("remote_control", True):
                msg = "нажатия выключены в программе (галочка «Разрешить нажатия из бота»)"
            else:
                for _ in range(3):
                    _, msg = self.cap.src.remote_input("tap", rule["tap"][0], rule["tap"][1])
                    for _ in range(8):                 # give the app up to 2 s to close the popup
                        time.sleep(0.25)
                        if not self.still_there(rule):
                            ok, msg = True, "нажал, окно закрылось"
                            break
                    if ok:
                        break
                if not ok:
                    msg = f"окно не закрылось ({msg})"
        except Exception as e:
            msg = f"ошибка: {e}"
        finally:
            self.last_msg = f"{time.strftime('%H:%M:%S')} авто «{rule['name']}»: {msg}"
            ev = {"type": "event", "rule": rule["id"], "ok": ok, "msg": msg, "at": time.time()}
            try:
                self.events.put_nowait(ev)
            except queue.Full:
                try:
                    self.events.get_nowait()
                    self.events.put_nowait(ev)
                except queue.Empty:
                    pass
            self.busy = False


# ============================================================ network
class Net(threading.Thread):
    def __init__(self, cap: Capture, server: str, token: str, on_rules=None):
        super().__init__(daemon=True)
        self.cap, self.server, self.token = cap, server.strip(), token.strip()
        self.on_rules = on_rules      # GUI callback: cache the rules in the settings
        self.stop_ev = threading.Event()
        self.state = "connecting"     # connecting | live | retry | fatal
        self.message = "Подключение…"
        self.viewers = None
        self.frames = 0
        self.bytes = 0
        self.send_avg = 0.0           # smoothed seconds per frame upload
        self.last_sent = time.monotonic()
        self.last_remote = ""         # last remote action, shown in the status line
        self._tasks: set = set()

    def stop(self):
        self.stop_ev.set()

    def run(self):
        asyncio.run(self._main())

    async def _sleep(self, sec):
        end = time.monotonic() + sec
        while not self.stop_ev.is_set() and time.monotonic() < end:
            await asyncio.sleep(0.1)

    async def _reader(self, ws):
        async for msg in ws:
            if isinstance(msg, str):
                try:
                    m = json.loads(msg)
                    if "viewers" in m:
                        self.viewers = m["viewers"]
                    if isinstance(m, dict) and m.get("cmd"):
                        t = asyncio.create_task(self._handle_cmd(ws, m))
                        self._tasks.add(t)
                        t.add_done_callback(self._tasks.discard)
                except ValueError:
                    pass

    async def _handle_cmd(self, ws, m: dict):
        """Commands from the bot / admin's control view: whole-window frames, click, Esc."""
        cmd = m.get("cmd")
        if cmd == "control":
            self.cap.full_until = time.monotonic() + 30 if m.get("on") else 0.0
            return
        if cmd == "rules":
            raw = m.get("rules") or []
            self.cap.auto.set(raw, unarm=m.get("new"))
            if self.on_rules:
                self.on_rules(raw)
            return
        if cmd in ("learn", "fire"):
            ok, msg, extra = False, "", {}
            if cmd == "learn":
                img = self.cap.last_img
                try:
                    rect = [float(v) for v in m.get("rect", [])]
                    valid = len(rect) == 4 and 0 <= rect[0] < rect[2] <= 1 and 0 <= rect[1] < rect[3] <= 1
                except (TypeError, ValueError):
                    valid = False
                if img is None:
                    msg = "нет картинки окна - трансляция идёт?"
                elif not valid:
                    msg = "неправильная область"
                else:
                    tpl = make_template(img, rect)
                    if ImageStat.Stat(tpl).stddev[0] < 10:
                        msg = "в обведённой области почти ничего нет - обведи окно вместе с текстом и кнопкой"
                    else:
                        ok, msg, extra = True, "запомнил окно", {"tpl": tpl_to_b64(tpl)}
            else:
                rule = self.cap.auto.find(str(m.get("rule", "")))
                if not rule:
                    msg = "правило ещё не дошло до ПК, попробуй через пару секунд"
                elif self.cap.auto.fire(rule):
                    ok, msg = True, "нажимаю"
                else:
                    msg = "уже нажимаю"
            try:
                await ws.send(json.dumps({"type": "result", "id": m.get("id"), "ok": ok, "msg": msg, **extra}))
            except Exception:
                pass
            return
        if cmd not in ("tap", "key"):
            return
        if not self.cap.cfg.get("remote_control", True):
            ok, msg = False, "нажатия выключены в программе на ПК (галочка «Разрешить нажатия из бота»)"
        else:
            loop = asyncio.get_running_loop()
            try:
                if cmd == "tap":
                    ok, msg = await loop.run_in_executor(
                        None, self.cap.src.remote_input, "tap", float(m.get("x", -1)), float(m.get("y", -1)))
                else:
                    ok, msg = await loop.run_in_executor(None, self.cap.src.remote_input, "esc")
            except Exception as e:
                ok, msg = False, f"ошибка: {e}"
        self.last_remote = f"{time.strftime('%H:%M:%S')} бот: {msg}"
        try:
            await ws.send(json.dumps({"type": "result", "id": m.get("id"), "ok": ok, "msg": msg}))
        except Exception:
            pass

    async def _main(self):
        backoff = 1
        while not self.stop_ev.is_set():
            reader = None
            try:
                self.state, self.message = "connecting", "Подключение к серверу…"
                async with connect(self.server, max_size=None, ping_interval=20,
                                   open_timeout=10, close_timeout=2) as ws:
                    await ws.send(json.dumps({"token": self.token, "v": APP_VERSION, "caps": ["control", "auto"]}))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if not reply.get("ok"):
                        raise ConnectionError("сервер не принял токен")
                    self.state, self.message, backoff = "live", "В эфире", 1
                    reader = asyncio.create_task(self._reader(ws))
                    last_seq = -1
                    last_full = self.cap.get_full()[0]
                    while not self.stop_ev.is_set():
                        if reader.done():
                            reader.result()          # raises if the connection died
                            raise ConnectionError("соединение закрыто")
                        while True:                       # auto-click events -> server (timer)
                            try:
                                ev = self.cap.auto.events.get_nowait()
                            except queue.Empty:
                                break
                            await asyncio.wait_for(ws.send(json.dumps(ev)), 20)
                        fseq, fdata = self.cap.get_full()
                        if fdata and fseq != last_full:
                            last_full = fseq
                            await asyncio.wait_for(ws.send(b"F" + fdata), 20)   # "F" = whole window, admins only
                        seq, data = self.cap.get_jpeg()
                        if data and seq != last_seq:
                            ts = time.monotonic()
                            await asyncio.wait_for(ws.send(data), 20)
                            self.send_avg = self.send_avg * 0.8 + (time.monotonic() - ts) * 0.2
                            self.last_sent = time.monotonic()
                            self.frames += 1
                            self.bytes += len(data)
                            last_seq = seq
                        else:
                            await asyncio.sleep(0.015)
                    return
            except InvalidURI:
                self.state, self.message = "fatal", "Неверный адрес сервера (нужен wss://…/ingest)"
                return
            except ConnectionClosed as e:
                if e.rcvd is not None and e.rcvd.code == 4401:
                    self.state, self.message = "fatal", "Неверный токен"
                    return
                self.state, self.message = "retry", f"Связь прервалась, повтор через {backoff} с"
            except Exception as e:
                err = str(e) or type(e).__name__
                self.state, self.message = "retry", f"Нет связи ({err[:60]}), повтор через {backoff} с"
            finally:
                if reader:
                    reader.cancel()
            await self._sleep(backoff)
            backoff = min(backoff * 2, 30)


# ============================================================ emulator / power
def set_keep_awake(on: bool) -> None:
    """Keep the PC awake and the screen on (only while we are streaming)."""
    if not IS_WIN:
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.SetThreadExecutionState.argtypes = [W.DWORD]
        k32.SetThreadExecutionState.restype = W.DWORD
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
        k32.SetThreadExecutionState(ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if on else 0))
    except Exception:
        pass


def find_emulator_shortcuts() -> list[dict]:
    """BlueStacks shortcuts on the desktop / in the Start menu: [{"name", "path", "app"}]."""
    if not IS_WIN:
        return []
    import tempfile
    out = Path(tempfile.gettempdir()) / "ScreenRelay-shortcuts.json"
    out.unlink(missing_ok=True)
    run_powershell(f"""
$sh = New-Object -ComObject WScript.Shell
$dirs = @('Desktop','CommonDesktopDirectory','Programs','CommonPrograms') | ForEach-Object {{ [Environment]::GetFolderPath($_) }}
$res = @()
foreach ($d in $dirs) {{
  if (-not $d -or -not (Test-Path -LiteralPath $d)) {{ continue }}
  Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | ForEach-Object {{
    try {{
      $l = $sh.CreateShortcut($_.FullName)
      if ($l.TargetPath -match 'HD-Player\\.exe|HD-RunApp\\.exe') {{
        $res += [pscustomobject]@{{ name = $_.BaseName; path = $_.FullName; args = $l.Arguments }}
      }}
    }} catch {{}}
  }}
}}
ConvertTo-Json -InputObject @($res) -Compress | Out-File -LiteralPath {ps_quote(out)} -Encoding utf8
""", wait=True)
    try:
        items = json.loads(out.read_text(encoding="utf-8-sig") or "[]")
    except Exception:
        return []
    if isinstance(items, dict):
        items = [items]
    seen, result = set(), []
    for it in items:
        if it.get("path") in seen:
            continue
        seen.add(it.get("path"))
        args = (it.get("args") or "").lower()
        is_app = "launchapp" in args or "package" in args or "app_pkg" in args
        result.append({"name": it.get("name", "?"), "path": it.get("path", ""), "app": is_app})
    result.sort(key=lambda x: (not x["app"], x["name"].lower()))     # app shortcuts first
    return result


def launch_shortcut(path: str) -> None:
    if not IS_WIN:
        raise RuntimeError("только на Windows")
    if not Path(path).exists():
        raise RuntimeError("ярлык не найден - выбери его заново")
    os.startfile(path)   # noqa - Windows only


# ============================================================ autostart
def startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_file() -> Path:
    return startup_dir() / "Screen Relay.lnk"


def autostart_enabled() -> bool:
    return startup_file().exists() or (startup_dir() / "ScreenRelay.bat").exists()


def set_autostart(on: bool) -> None:
    """Shortcut in the Startup folder (no console window flashing at logon)."""
    (startup_dir() / "ScreenRelay.bat").unlink(missing_ok=True)      # old versions used a .bat
    lnk = startup_file()
    if not on:
        lnk.unlink(missing_ok=True)
        return
    if getattr(sys, "frozen", False):
        target, args = Path(sys.executable), "--autostart"
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        target = pyw if pyw.exists() else Path(sys.executable)
        args = f'"{Path(__file__).resolve()}" --autostart'
    q = ps_quote
    run_powershell(
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({q(lnk)}); "
        f"$s.TargetPath = {q(target)}; $s.Arguments = {q(args)}; "
        f"$s.WorkingDirectory = {q(target.parent)}; $s.Description = {q(APP_NAME)}; $s.Save()",
        wait=True)
    if not lnk.exists():
        raise RuntimeError("ярлык не создался")


# ============================================================ GUI
class App:
    def __init__(self, root: tk.Tk, autostart: bool):
        self.root = root
        self.cfg = load_settings()
        self.net: Net | None = None
        self.cap = Capture(self.cfg)
        self.cap.start()
        self.photo = None
        self.view = None            # (offset_x, offset_y, scale, img_w, img_h) of the preview on canvas
        self.drag_start = None
        self.drag_rect = None
        self.save_job = None
        self.last_stats = (time.monotonic(), 0, 0)
        self.shown_seq = -1
        self.scaled = None
        self.dimmed = None
        self.ui_q: queue.Queue = queue.Queue()   # actions from other threads (tray, 2nd launch)
        self.tray = None
        self.tray_state = None
        self.hint_shown = False
        self.updater = Updater(self.ui_q.put)
        self.shortcuts: dict[str, str] = {}
        self.last_launch = -1e9
        self.last_wall = time.time()
        self.can_update = IS_WIN and getattr(sys, "frozen", False)

        root.title(f"{APP_NAME} {APP_VERSION}")
        self.win_icon = ImageTk.PhotoImage(make_icon("#27ae60"))
        root.iconphoto(True, self.win_icon)
        h = max(600, min(840, root.winfo_screenheight() - 90))   # fits small laptop screens
        root.geometry(f"840x{h}")
        root.minsize(700, 600)
        self._build()
        self.refresh_windows()
        self.setup_tray()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Unmap>", self.on_unmap)
        root.after(100, self.tick_preview)
        root.after(100, self.pump)
        root.after(1000, self.tick_status)
        if autostart or self.cfg["start_on_launch"]:
            root.after(1500, self.start)
        if autostart:      # started with Windows - go straight to the tray
            self.hint_shown = True
            if self.tray:
                root.withdraw()
            else:
                root.after(500, root.iconify)
        if "--updated" in sys.argv:
            self.upd_label.config(text=f"Обновлено до {APP_VERSION} ✓", foreground="#27ae60")
            self.notify(f"Обновлено до версии {APP_VERSION}")
        if self.can_update:
            root.after(5000, self.auto_check)
        if IS_WIN and not self.cfg["emulator_shortcut"]:
            root.after(800, self.detect_shortcuts)

    # ---------------------------------------------------- layout
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        style = ttk.Style()
        try:
            style.theme_use("vista" if IS_WIN else "clam")
        except tk.TclError:
            pass
        style.configure("Big.TButton", font=("Segoe UI", 12, "bold"), padding=8)

        conn = ttk.LabelFrame(self.root, text="Сервер")
        conn.pack(fill="x", **pad)
        ttk.Label(conn, text="Адрес:").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        self.server_var = tk.StringVar(value=self.cfg["server"])
        self.server_entry = ttk.Entry(conn, textvariable=self.server_var)
        self.server_entry.grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(conn, text="🌐 Смотреть", command=self.open_viewer).grid(row=0, column=2, padx=6, sticky="ew")
        ttk.Label(conn, text="Токен:").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        self.token_var = tk.StringVar(value=self.cfg["token"])
        self.token_entry = ttk.Entry(conn, textvariable=self.token_var, show="•")
        self.token_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(conn, text="👁 Показать", command=self.toggle_token).grid(row=1, column=2, padx=6, sticky="ew")
        hint = ttk.Frame(conn)
        hint.grid(row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 4))
        self.code_label = ttk.Label(hint, foreground="#666", wraplength=620,
                                    text="Есть код подключения sr1:… от setup.sh? Вставь его в поле «Адрес» или нажми →")
        self.code_label.pack(side="left")
        ttk.Button(hint, text="📋 Вставить код", command=self.paste_code).pack(side="right")
        conn.columnconfigure(1, weight=1)

        src = ttk.LabelFrame(self.root, text="Что транслировать")
        src.pack(fill="both", expand=True, **pad)
        top = ttk.Frame(src)
        top.pack(fill="x", padx=6, pady=4)
        ttk.Label(top, text="Окно:").pack(side="left")
        self.win_var = tk.StringVar(value=self.cfg["window_title"])
        self.win_box = ttk.Combobox(top, textvariable=self.win_var, state="readonly")
        self.win_box.pack(side="left", fill="x", expand=True, padx=6)
        self.win_box.bind("<<ComboboxSelected>>", self.on_window_selected)
        self.win_box.bind("<Button-1>", lambda e: self.refresh_windows())
        ttk.Button(top, text="⟳ Обновить", command=self.refresh_windows).pack(side="left")
        if IS_WIN:
            self.hide_btn = ttk.Button(top, text="🙈 Спрятать окно", command=self.toggle_hide)
            self.hide_btn.pack(side="left", padx=(4, 0))

        self.canvas = tk.Canvas(src, bg="#15171c", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=6, pady=4)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", lambda e: self.reset_crop())
        self.canvas.bind("<Configure>", lambda e: self.draw_preview(force=True))

        hint = ttk.Frame(src)
        hint.pack(fill="x", padx=6)
        self.area_label = ttk.Label(hint, text="", foreground="#666")
        self.area_label.pack(side="left")
        ttk.Button(hint, text="Всё окно", command=self.reset_crop).pack(side="right")

        opts = ttk.Frame(src)
        opts.pack(fill="x", padx=6, pady=6)
        ttk.Label(opts, text="FPS:").pack(side="left")
        self.fps_var = tk.IntVar(value=self.cfg["fps"])
        ttk.Spinbox(opts, from_=1, to=30, width=4, textvariable=self.fps_var,
                    command=self.on_option).pack(side="left", padx=(4, 14))
        ttk.Label(opts, text="Качество:").pack(side="left")
        self.q_var = tk.IntVar(value=self.cfg["quality"])
        ttk.Scale(opts, from_=30, to=95, variable=self.q_var, length=120,
                  command=lambda v: self.on_option()).pack(side="left", padx=(4, 4))
        self.q_label = ttk.Label(opts, width=3)
        self.q_label.pack(side="left", padx=(0, 14))
        ttk.Label(opts, text="Ширина:").pack(side="left")
        self.w_var = tk.StringVar(value=str(self.cfg["max_width"]))
        wb = ttk.Combobox(opts, textvariable=self.w_var, width=9, state="readonly",
                          values=["Оригинал", "1920", "1280", "960", "720"])
        wb.pack(side="left", padx=4)
        wb.bind("<<ComboboxSelected>>", lambda e: self.on_option())
        self.fps_var.trace_add("write", lambda *a: self.on_option())
        self.server_var.trace_add("write", lambda *a: self.on_server_typed())
        self.token_var.trace_add("write", lambda *a: self.on_option())

        emu = ttk.LabelFrame(self.root, text="Автовосстановление (BlueStacks)")
        emu.pack(fill="x", **pad)
        row = ttk.Frame(emu)
        row.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Label(row, text="Ярлык приложения:").pack(side="left")
        self.sc_var = tk.StringVar(value=Path(self.cfg["emulator_shortcut"]).stem if self.cfg["emulator_shortcut"] else "")
        self.sc_box = ttk.Combobox(row, textvariable=self.sc_var, state="readonly")
        self.sc_box.pack(side="left", fill="x", expand=True, padx=6)
        self.sc_box.bind("<<ComboboxSelected>>", self.on_shortcut_selected)
        ttk.Button(row, text="Найти", command=self.detect_shortcuts).pack(side="left")
        ttk.Button(row, text="Файл…", command=self.pick_shortcut).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="▶ Проверить", command=lambda: self.launch_emulator("проверка")).pack(side="left", padx=(4, 0))
        self.launch_var = tk.BooleanVar(value=self.cfg["auto_launch"])
        ttk.Checkbutton(emu, text="Если окна нет (ПК включили, BlueStacks закрылся) - запускать его самому",
                        variable=self.launch_var, command=self.on_option).pack(anchor="w", padx=6)
        self.emu_label = ttk.Label(emu, foreground="#666", wraplength=780,
                                   text="Ярлык создаётся в BlueStacks: долгое нажатие на иконку приложения → "
                                        "«Desktop shortcut». Потом «Найти».")
        self.emu_label.pack(anchor="w", padx=6, pady=(0, 4))
        if not IS_WIN:
            for w in row.winfo_children():
                try:
                    w.state(["disabled"])
                except Exception:
                    pass

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)
        upd = ttk.Frame(bottom)
        upd.pack(side="right", anchor="ne")
        self.upd_btn = ttk.Button(upd, text="⟳ Проверить обновления", command=self.check_updates)
        self.upd_btn.pack(anchor="e")
        self.upd_label = ttk.Label(upd, text=f"Версия {APP_VERSION}", foreground="#666")
        self.upd_label.pack(anchor="e", pady=(2, 0))
        self.auto_var = tk.BooleanVar(value=self.cfg["start_on_launch"])
        ttk.Checkbutton(bottom, text="Начинать трансляцию сразу при запуске",
                        variable=self.auto_var, command=self.on_option).pack(anchor="w")
        self.tray_var = tk.BooleanVar(value=self.cfg["minimize_to_tray"])
        cb = ttk.Checkbutton(bottom, text="Сворачивать в трей у часов (трансляция продолжается)",
                             variable=self.tray_var, command=self.on_option)
        cb.pack(anchor="w")
        if not pystray:
            cb.state(["disabled"])
        self.hidemin_var = tk.BooleanVar(value=self.cfg["hide_on_minimize"])
        ttk.Checkbutton(bottom, text="Свернул BlueStacks - прятать за край экрана (трансляция идёт дальше)",
                        variable=self.hidemin_var, command=self.on_option).pack(anchor="w")
        self.remote_var = tk.BooleanVar(value=self.cfg["remote_control"])
        ttk.Checkbutton(bottom, text="Разрешить нажатия из бота (закрыть всплывшее окно кнопкой или тапом по картинке)",
                        variable=self.remote_var, command=self.on_option).pack(anchor="w")
        self.awake_var = tk.BooleanVar(value=self.cfg["keep_awake"])
        ttk.Checkbutton(bottom, text="Не давать ПК засыпать и гасить экран во время трансляции",
                        variable=self.awake_var, command=self.on_awake).pack(anchor="w")
        if IS_WIN:
            self.boot_var = tk.BooleanVar(value=autostart_enabled())
            ttk.Checkbutton(bottom, text="Запускать вместе с Windows",
                            variable=self.boot_var, command=self.on_boot).pack(anchor="w")

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=8, pady=(4, 10))
        self.btn = ttk.Button(bar, text="▶  Начать трансляцию", style="Big.TButton", command=self.toggle)
        self.btn.pack(side="left")
        ttk.Button(bar, text="🔄 Восстановить", style="Big.TButton", command=self.restore).pack(side="left", padx=(6, 0))
        st = ttk.Frame(bar)
        st.pack(side="left", fill="x", expand=True, padx=12)
        self.state_label = tk.Label(st, text="● Остановлено", fg="#888", font=("Segoe UI", 11, "bold"), anchor="w")
        self.state_label.pack(fill="x")
        self.info_label = ttk.Label(st, text="", foreground="#666")
        self.info_label.pack(fill="x")
        self.update_area_label()
        self.q_label.config(text=str(self.q_var.get()))

        # buttons and options always stay visible; the preview takes whatever height is left
        for w in (src, emu, bottom, bar):
            w.pack_forget()
        bar.pack(side="bottom", fill="x", padx=8, pady=(4, 10))
        bottom.pack(side="bottom", fill="x", **pad)
        emu.pack(side="bottom", fill="x", **pad)
        src.pack(fill="both", expand=True, **pad)

    # ---------------------------------------------------- settings
    def toggle_token(self):
        self.token_entry.config(show="" if self.token_entry.cget("show") else "•")

    def push_cfg(self):
        self.cap.cfg = dict(self.cfg)
        self.cap.src.hide_on_minimize = bool(self.cfg.get("hide_on_minimize", True))
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(500, lambda: save_settings(self.cfg))

    def on_option(self):
        try:
            fps = max(1, min(30, int(self.fps_var.get())))
        except (tk.TclError, ValueError):
            return
        q = int(float(self.q_var.get()))
        self.q_label.config(text=str(q))
        self.cfg.update(fps=fps, quality=q, max_width=self.w_var.get(),
                        start_on_launch=bool(self.auto_var.get()),
                        minimize_to_tray=bool(self.tray_var.get()),
                        auto_launch=bool(self.launch_var.get()),
                        keep_awake=bool(self.awake_var.get()),
                        hide_on_minimize=bool(self.hidemin_var.get()),
                        remote_control=bool(self.remote_var.get()),
                        server=self.server_var.get().strip(), token=self.token_var.get().strip())
        self.push_cfg()

    def apply_code(self, text) -> bool:
        res = decode_code(text)
        if not res:
            return False
        server, token = res
        self.server_var.set(server)
        self.token_var.set(token)
        self.code_label.config(text="Код подключения применён ✓ Адрес и токен заполнены.", foreground="#27ae60")
        self.on_option()
        return True

    def on_server_typed(self):
        val = self.server_var.get()
        if val.strip().startswith("sr1:"):
            self.root.after(1, lambda: self.apply_code(val))
        else:
            self.on_option()

    def paste_code(self):
        if self.net:
            self.code_label.config(text="Сначала останови трансляцию", foreground="#e67e22")
            return
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        if not self.apply_code(text):
            self.code_label.config(text="В буфере обмена нет кода sr1:… - скопируй его из вывода setup.sh",
                                   foreground="#e74c3c")

    def open_viewer(self):
        base = server_http_base(self.server_var.get())
        if not base:
            self.code_label.config(text="Сначала укажи адрес сервера", foreground="#e74c3c")
            return
        webbrowser.open(base + "/")

    def on_boot(self):
        try:
            set_autostart(self.boot_var.get())
        except Exception as e:
            self.info_label.config(text=f"Не удалось настроить автозапуск: {e}")

    def refresh_windows(self):
        titles = [SCREEN] + sorted({t for _, t in list_windows()}, key=str.lower)
        cur = self.win_var.get()
        if cur and cur not in titles:
            titles.insert(1, cur)      # keep the saved one even if the emulator isn't running yet
        self.win_box["values"] = titles
        if not cur:
            guess = next((t for t in titles if "bluestacks" in t.lower()), None)
            if guess:
                self.win_var.set(guess)
                self.on_window_selected()

    def on_window_selected(self, _=None):
        self.cfg["window_title"] = self.win_var.get()
        self.cfg["crop"] = None
        self.update_area_label()
        self.push_cfg()

    # ---------------------------------------------------- crop selection
    def canvas_to_frac(self, x, y):
        if not self.view:
            return None
        ox, oy, s, w, h = self.view
        fx = min(1.0, max(0.0, (x - ox) / (w * s)))
        fy = min(1.0, max(0.0, (y - oy) / (h * s)))
        return fx, fy

    def on_press(self, e):
        p = self.canvas_to_frac(e.x, e.y)
        if p:
            self.drag_start = p
            self.drag_rect = (*p, *p)

    def on_drag(self, e):
        if not self.drag_start:
            return
        p = self.canvas_to_frac(e.x, e.y)
        x0, y0 = self.drag_start
        self.drag_rect = (min(x0, p[0]), min(y0, p[1]), max(x0, p[0]), max(y0, p[1]))
        self.draw_overlay()

    def on_release(self, e):
        if not self.drag_start:
            return
        r = self.drag_rect
        self.drag_start, self.drag_rect = None, None
        if r and (r[2] - r[0]) > 0.02 and (r[3] - r[1]) > 0.02:
            self.cfg["crop"] = [round(v, 4) for v in r]
            self.push_cfg()
        self.update_area_label()
        self.draw_overlay()

    def reset_crop(self):
        self.cfg["crop"] = None
        self.push_cfg()
        self.update_area_label()
        self.draw_overlay()

    def update_area_label(self):
        c = self.cfg.get("crop")
        if c:
            self.area_label.config(text=f"Транслируется выделенная область "
                                        f"({round((c[2]-c[0])*100)}% × {round((c[3]-c[1])*100)}% окна). "
                                        f"Правый клик - сбросить.")
        else:
            self.area_label.config(text="Транслируется всё окно. Выдели мышкой область, если нужна только часть.")

    # ---------------------------------------------------- preview
    def draw_preview(self, force=False):
        with self.cap.lock:
            img, seq = self.cap.preview, self.cap.preview_seq
        if img is None or (seq == self.shown_seq and not force):
            return
        self.shown_seq = seq
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        s = min(cw / img.width, ch / img.height)
        w, h = max(1, int(img.width * s)), max(1, int(img.height * s))
        self.scaled = img.resize((w, h), Image.BILINEAR)
        self.dimmed = None
        ox, oy = (cw - w) // 2, (ch - h) // 2
        self.view = (ox, oy, s, img.width, img.height)
        self.draw_overlay()

    def draw_overlay(self):
        """Preview with everything outside the selected area darkened."""
        if not self.view or getattr(self, "scaled", None) is None:
            return
        ox, oy, s, w, h = self.view
        sw, sh = self.scaled.size
        r = self.drag_rect or self.cfg.get("crop")
        if r:
            if self.dimmed is None:
                self.dimmed = ImageEnhance.Brightness(self.scaled).enhance(0.35)
            box = (int(r[0] * sw), int(r[1] * sh), max(int(r[0] * sw) + 1, int(r[2] * sw)),
                   max(int(r[1] * sh) + 1, int(r[3] * sh)))
            frame = self.dimmed.copy()
            frame.paste(self.scaled.crop(box), box[:2])
        else:
            frame = self.scaled
        self.photo = ImageTk.PhotoImage(frame)
        self.canvas.delete("img", "ov")
        self.canvas.create_image(ox, oy, image=self.photo, anchor="nw", tags="img")
        if r:
            self.canvas.create_rectangle(ox + box[0], oy + box[1], ox + box[2], oy + box[3],
                                         outline="#2ecc71", width=2, tags="ov")

    def tick_preview(self):
        try:
            self.draw_preview()
            if self.cap.preview is None:
                self.canvas.delete("msg")
                self.canvas.create_text(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                        text=self.cap.status, fill="#aaa", font=("Segoe UI", 12), tags="msg")
            else:
                self.canvas.delete("msg")
        finally:
            self.root.after(150, self.tick_preview)

    # ---------------------------------------------------- start / stop
    def toggle(self):
        if self.net:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.net:
            return
        self.on_option()
        if not self.cfg["token"]:
            self.set_state("Впиши токен", "#e74c3c")
            return
        if not self.cfg["window_title"]:
            self.set_state("Выбери окно", "#e74c3c")
            return
        self.cap.streaming = True
        self.net = self.make_net()
        self.net.start()
        self.btn.config(text="■  Остановить")
        for w in (self.server_entry, self.token_entry):
            w.config(state="disabled")
        self.last_stats = (time.monotonic(), 0, 0)
        self.update_tray("connecting")
        set_keep_awake(self.cfg["keep_awake"])

    def stop(self):
        if self.net:
            self.net.stop()
            self.net = None
        self.cap.streaming = False
        self.btn.config(text="▶  Начать трансляцию")
        for w in (self.server_entry, self.token_entry):
            w.config(state="normal")
        self.set_state("Остановлено", "#888")
        self.info_label.config(text="")
        self.update_tray("stopped")
        set_keep_awake(False)

    def set_state(self, text, color):
        self.state_label.config(text=f"● {text}", fg=color)

    def make_net(self) -> Net:
        return Net(self.cap, self.cfg["server"], self.cfg["token"],
                   on_rules=lambda raw: self.ui_q.put(lambda: self.save_rules(raw)))

    def save_rules(self, raw: list):
        self.cfg["auto_rules"] = raw
        self.push_cfg()

    def health_check(self):
        """Self-healing: restart a dead capture thread, reconnect a stuck upload, adapt quality."""
        now = time.monotonic()
        if not self.cap.is_alive() or now - self.cap.heartbeat > 15:
            streaming = self.cap.streaming
            self.cap.stop_ev.set()
            old_src, old_auto = self.cap.src, self.cap.auto
            self.cap = Capture(self.cfg)
            self.cap.src = old_src
            self.cap.auto = old_auto
            old_auto.cap = self.cap
            self.cap.streaming = streaming
            self.cap.start()
            if self.net:
                self.net.cap = self.cap
            self.set_state("Захват завис - перезапустил", "#e67e22")
            return
        net = self.net
        if not net or net.state != "live":
            return
        seq, _ = self.cap.get_jpeg()
        if now - net.last_sent > 25 and seq and self.cap.preview is not None and self.cap.src.missing_since is None:
            self.reconnect("Отправка застряла - переподключаюсь")
            return
        budget = 1.0 / max(1, int(self.cfg.get("fps", 8)))
        if net.send_avg > budget * 1.2 and self.cap.scale > 0.4:
            self.cap.scale = round(max(0.4, self.cap.scale - 0.1), 2)
        elif net.send_avg < budget * 0.4 and self.cap.scale < 1.0:
            self.cap.scale = round(min(1.0, self.cap.scale + 0.05), 2)

    def tick_status(self):
        try:
            wall = time.time()
            if self.net and wall - self.last_wall > 20:     # the PC was asleep - reconnect right away
                self.reconnect("ПК проснулся - переподключаюсь")
            self.last_wall = wall
            self.maybe_launch_emulator()
            self.health_check()
            self.update_hide_btn()
            net = self.net
            src_status = self.cap.status
            if net:
                t, f0, b0 = self.last_stats
                now = time.monotonic()
                dt = max(0.001, now - t)
                fps = (net.frames - f0) / dt
                kbps = (net.bytes - b0) / dt / 1024
                self.last_stats = (now, net.frames, net.bytes)
                if net.state == "live":
                    viewers = f" · зрителей: {net.viewers}" if net.viewers is not None else ""
                    self.set_state(f"В ЭФИРЕ · {fps:.0f} fps · {kbps:.0f} КБ/с{viewers}", "#27ae60")
                    self.update_tray("live", f"{APP_NAME}: в эфире{viewers}")
                elif net.state == "fatal":
                    self.stop()
                    self.set_state(net.message, "#e74c3c")
                else:
                    self.set_state(net.message, "#e67e22")
                    self.update_tray("retry", f"{APP_NAME}: {net.message}")
                w, h = self.cap.out_size
                auto = f" · авто-качество {int(self.cap.scale * 100)}% (медленный интернет)" if self.cap.scale < 0.99 else ""
                remote = f" · {net.last_remote}" if net.last_remote else ""
                if self.cap.auto.rules:
                    remote += f" · авто-нажатий: {len(self.cap.auto.rules)}"
                if self.cap.auto.last_msg:
                    remote += f" · {self.cap.auto.last_msg}"
                self.info_label.config(text=f"{src_status}" + (f" · кадр {w}×{h}" if w else "") + auto + remote)
            else:
                self.info_label.config(text=src_status)
        finally:
            self.root.after(1000, self.tick_status)

    # ---------------------------------------------------- emulator / recovery
    def detect_shortcuts(self):
        if not IS_WIN:
            return
        self.emu_label.config(text="Ищу ярлыки BlueStacks…", foreground="#666")

        def work():
            try:
                found = find_emulator_shortcuts()
            except Exception:
                found = []
            self.ui_q.put(lambda: self.on_shortcuts_found(found))

        threading.Thread(target=work, daemon=True).start()

    def on_shortcuts_found(self, found):
        self.shortcuts = {f["name"]: f["path"] for f in found}
        cur = self.cfg["emulator_shortcut"]
        if cur and Path(cur).stem not in self.shortcuts:
            self.shortcuts[Path(cur).stem] = cur
        self.sc_box["values"] = list(self.shortcuts)
        if not found:
            self.emu_label.config(text="Ярлыки BlueStacks не найдены. В BlueStacks: долгое нажатие на иконку "
                                       "приложения → Desktop shortcut, потом «Найти».", foreground="#e67e22")
            return
        self.emu_label.config(text=f"Найдено ярлыков: {len(found)}. Выбери тот, что открывает нужное приложение.",
                              foreground="#666")
        if not cur:
            first = found[0]
            self.sc_var.set(first["name"])
            self.on_shortcut_selected()

    def on_shortcut_selected(self, _=None):
        path = self.shortcuts.get(self.sc_var.get(), "")
        if path:
            self.cfg["emulator_shortcut"] = path
            self.emu_label.config(text=f"Будет запускаться: {path}", foreground="#666")
            self.push_cfg()

    def pick_shortcut(self):
        from tkinter import filedialog
        desktop = Path.home() / "Desktop"
        path = filedialog.askopenfilename(
            title="Ярлык приложения BlueStacks", initialdir=str(desktop if desktop.exists() else Path.home()),
            filetypes=[("Ярлык или программа", "*.lnk *.exe"), ("Все файлы", "*.*")])
        if path:
            path = str(Path(path))
            self.shortcuts[Path(path).stem] = path
            self.sc_box["values"] = list(self.shortcuts)
            self.sc_var.set(Path(path).stem)
            self.on_shortcut_selected()

    def launch_emulator(self, why: str) -> bool:
        path = self.cfg.get("emulator_shortcut")
        if not path:
            self.emu_label.config(text="Сначала выбери ярлык приложения", foreground="#e74c3c")
            return False
        try:
            launch_shortcut(path)
        except Exception as e:
            self.emu_label.config(text=f"Не удалось запустить: {e}", foreground="#e74c3c")
            return False
        self.last_launch = time.monotonic()
        self.emu_label.config(text=f"Запустил BlueStacks ({why}) в {time.strftime('%H:%M')}", foreground="#27ae60")
        return True

    def maybe_launch_emulator(self):
        if not (IS_WIN and self.net and self.cfg.get("auto_launch") and self.cfg.get("emulator_shortcut")):
            return
        since = self.cap.src.missing_since
        now = time.monotonic()
        if since and now - since > 15 and now - self.last_launch > 180:
            self.launch_emulator("окна не было")

    def reconnect(self, why: str):
        self.cap.src.hwnd = None            # look for the window again
        if self.net:
            self.net.stop()
            self.net = self.make_net()
            self.net.start()
            self.last_stats = (time.monotonic(), 0, 0)
        self.set_state(why, "#e67e22")

    def restore(self):
        """One button: (re)start BlueStacks with the app, find the window, reconnect, start streaming."""
        if IS_WIN and self.cfg.get("emulator_shortcut"):
            self.launch_emulator("кнопка «Восстановить»")
        if self.net:
            self.reconnect("Восстанавливаю связь…")
        else:
            self.cap.src.hwnd = None
            self.start()

    def on_awake(self):
        self.on_option()
        if self.net:
            set_keep_awake(self.cfg["keep_awake"])

    # ---------------------------------------------------- tray / window
    def setup_tray(self):
        if not pystray:
            return
        post = self.ui_q.put
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Открыть", lambda i, it: post(self.show_window), default=True),
                pystray.MenuItem(lambda it: "Остановить трансляцию" if self.net else "Начать трансляцию",
                                 lambda i, it: post(self.toggle)),
                pystray.MenuItem("🔄 Восстановить", lambda i, it: post(self.restore)),
                pystray.MenuItem(lambda it: "👁 Показать BlueStacks" if self.cap.src.hidden else "🙈 Спрятать BlueStacks",
                                 lambda i, it: post(self.toggle_hide)),
                pystray.MenuItem(lambda it: f"Обновить до {self.updater.latest['version']}"
                                 if self.updater.latest else "Проверить обновления",
                                 lambda i, it: post(self.check_updates)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", lambda i, it: post(self.quit)),
            )
            self.tray = pystray.Icon("ScreenRelay", make_icon(), APP_NAME, menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception:
            self.tray = None

    def update_tray(self, state, title=None):
        if not self.tray:
            return
        title = (title or APP_NAME)[:120]
        if (state, title) == self.tray_state:
            return
        self.tray_state = (state, title)
        colors = {"live": "#27ae60", "connecting": "#e67e22", "retry": "#e67e22", "stopped": "#888888"}
        try:
            self.tray.icon = make_icon(colors.get(state, "#888888"))
            self.tray.title = title
            self.tray.update_menu()
        except Exception:
            pass

    def pump(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def show_window(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def hide_to_tray(self):
        self.root.withdraw()
        if not self.hint_shown and self.tray:
            self.hint_shown = True
            try:
                if getattr(self.tray, "HAS_NOTIFICATION", False):
                    self.tray.notify("Работаю в фоне. Значок у часов - открыть или выйти.", APP_NAME)
            except Exception:
                pass

    def use_tray(self) -> bool:
        return bool(self.tray) and self.cfg.get("minimize_to_tray", True)

    def on_unmap(self, e):
        if e.widget is self.root and self.use_tray() and self.root.state() == "iconic":
            self.root.after(10, self.hide_to_tray)

    def on_close(self):
        if self.use_tray():
            self.hide_to_tray()
        else:
            self.quit()

    # ---------------------------------------------------- updates
    def notify(self, text):
        try:
            if self.tray and getattr(self.tray, "HAS_NOTIFICATION", False):
                self.tray.notify(text, APP_NAME)
        except Exception:
            pass

    def auto_check(self):
        self.updater.check(self.cfg["server"], self.cfg["token"], self.on_checked_silent)
        self.root.after(6 * 3600 * 1000, self.auto_check)

    def on_checked_silent(self, latest, error):
        if latest:
            self.show_update_available(latest)
            if self.root.state() == "withdrawn":
                self.notify(f"Доступна версия {latest['version']} - нажми «Обновить» в меню значка")

    def show_update_available(self, latest):
        self.upd_btn.config(text=f"⬇ Обновить до {latest['version']}", state="normal")
        self.upd_label.config(text=f"Сейчас {APP_VERSION}", foreground="#e67e22")
        if self.tray:
            try:
                self.tray.update_menu()
            except Exception:
                pass

    def check_updates(self):
        if self.updater.latest:
            self.install_update()
            return
        if not self.can_update:
            self.upd_label.config(text="Обновление работает только в exe", foreground="#e67e22")
            return
        self.on_option()
        self.upd_btn.config(state="disabled")
        self.upd_label.config(text="Проверяю…", foreground="#666")
        self.updater.check(self.cfg["server"], self.cfg["token"], self.on_checked_manual)

    def on_checked_manual(self, latest, error):
        self.upd_btn.config(state="normal")
        if error:
            self.upd_label.config(text=f"Не удалось: {error[:70]}", foreground="#e74c3c")
        elif latest:
            self.show_update_available(latest)
        else:
            self.upd_label.config(text=f"У тебя последняя версия ({APP_VERSION}) ✓", foreground="#27ae60")

    def install_update(self):
        self.upd_btn.config(state="disabled")
        self.upd_label.config(text="Скачиваю… 0%", foreground="#666")
        self.updater.download(
            lambda p: self.upd_label.config(text=f"Скачиваю… {p}%"),
            self.on_downloaded)

    def on_downloaded(self, path, error):
        if error:
            self.upd_btn.config(state="normal")
            self.upd_label.config(text=f"Ошибка загрузки: {error[:60]}", foreground="#e74c3c")
            return
        self.upd_label.config(text="Перезапускаю…")
        args = ["--updated"]
        if self.net:                       # keep streaming after the restart
            args.append("--autostart")
        try:
            launch_swap_and_restart(path, args)
        except Exception as e:
            self.upd_btn.config(state="normal")
            self.upd_label.config(text=f"Не удалось перезапустить: {e}", foreground="#e74c3c")
            return
        self.quit(show_emulator=False)

    def toggle_hide(self):
        src = self.cap.src
        if not src.hwnd:
            self.set_state("Окно не найдено", "#e74c3c")
            return
        src.request = "show" if src.hidden else "hide"
        self.root.after(400, self.update_hide_btn)

    def update_hide_btn(self):
        if hasattr(self, "hide_btn"):
            self.hide_btn.config(text="👁 Показать окно" if self.cap.src.hidden else "🙈 Спрятать окно")
        if self.tray:
            try:
                self.tray.update_menu()
            except Exception:
                pass

    def quit(self, show_emulator: bool = True):
        if show_emulator and IS_WIN and self.cap.src.hidden and self.cap.src.hwnd:
            try:                     # never leave the emulator lost off-screen
                vl, vt, _, _ = virtual_screen()
                x, y = self.cap.src.home or (vl + 80, vt + 60)
                move_window(self.cap.src.hwnd, x, y)
            except Exception:
                pass
        self.on_option()
        save_settings(self.cfg)
        if self.net:
            self.net.stop()
        self.cap.stop_ev.set()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        if getattr(self, "instance_sock", None):
            try:
                self.instance_sock.close()
            except OSError:
                pass
        self.root.destroy()

    def listen_instances(self, srv: socket.socket):
        self.instance_sock = srv

        def loop():
            while True:
                try:
                    conn, _ = srv.accept()
                    conn.close()
                    self.ui_q.put(self.show_window)
                except OSError:
                    return
        threading.Thread(target=loop, daemon=True).start()


def selftest_match() -> str:
    """Popup detection must see the popup, ignore the screen under it and survive small differences."""
    def screen(popup: bool, shift=0) -> Image.Image:
        im = Image.new("RGB", (540, 960), "#ff7a00")
        d = ImageDraw.Draw(im)
        for i in range(12):
            d.text((40, 120 + i * 60), f"Stat {i}: {i * 37 % 91}", fill="#ffffff")
        if popup:
            d.rectangle((30, 560 + shift, 510, 800 + shift), fill="#ffffff")
            d.text((170, 590 + shift), "ENJOY YOUR WORKOUT!", fill="#000000")
            d.text((60, 630 + shift), "The gate remains open for a maximum of 10 seconds.", fill="#333333")
            d.rounded_rectangle((50, 700 + shift, 490, 770 + shift), radius=12, fill="#5b2ca0")
            d.text((250, 728 + shift), "GOT IT", fill="#ffffff")
        return im

    rect = [30 / 540, 560 / 960, 510 / 540, 800 / 960]
    tpl = make_template(screen(True), rect)
    tpl = tpl_from_b64(tpl_to_b64(tpl))
    same = tpl_diff(screen(True, shift=2), rect, tpl)
    gone = tpl_diff(screen(False), rect, tpl)
    scaled = tpl_diff(screen(True).resize((720, 1280)), rect, tpl)
    assert same < MATCH_DIFF and scaled < MATCH_DIFF, f"popup not recognized same={same:.1f} scaled={scaled:.1f}"
    assert gone > MATCH_DIFF * 2, f"false match gone={gone:.1f}"
    return f"ok({same:.1f}/{scaled:.1f}/{gone:.1f})"


def selftest() -> int:
    """Used by the GitHub build to make sure everything got packed into the exe."""
    log = Path(sys.executable if getattr(sys, "frozen", False) else __file__).with_name("selftest.log")
    try:
        assert pystray is not None, "pystray not bundled"
        if IS_WIN:
            assert type(pystray.Icon).__module__ != "pystray._dummy", "pystray win32 backend missing"
            titles = list_windows()
        else:
            titles = []
        with (mss.MSS() if hasattr(mss, "MSS") else mss.mss()) as sct:
            shot = sct.grab({"left": 0, "top": 0, "width": 64, "height": 64})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        img.save(io.BytesIO(), "JPEG")
        make_icon()
        assert decode_code("sr1:eyJzIjoid3NzOi8veC95IiwidCI6ImFiYyJ9") == ("wss://x/y", "abc"), "connection code"
        extra = " match=" + selftest_match()
        if IS_WIN:
            set_autostart(True)
            assert startup_file().exists(), "autostart shortcut not created"
            set_autostart(False)
            assert not startup_file().exists(), "autostart shortcut not removed"
            # fake BlueStacks app shortcut on the desktop -> must be found and flagged as an app shortcut
            import tempfile
            fake = Path(tempfile.gettempdir()) / "fakebs" / "HD-Player.exe"
            fake.parent.mkdir(exist_ok=True)
            fake.write_bytes(b"MZ")
            run_powershell(
                "$d = [Environment]::GetFolderPath('Desktop'); New-Item -ItemType Directory -Force -Path $d | Out-Null; "
                "$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'SR Test App.lnk')); "
                f"$s.TargetPath = {ps_quote(fake)}; $s.Arguments = '--instance Pie64 --cmd launchApp --package com.test'; $s.Save()",
                wait=True)
            found = find_emulator_shortcuts()
            hit = [f for f in found if f["name"] == "SR Test App"]
            for f in hit:
                Path(f["path"]).unlink(missing_ok=True)
            assert hit and hit[0]["app"], f"shortcut detection failed: {found}"
            set_keep_awake(True)
            set_keep_awake(False)
            extra = " autostart=ok shortcuts=ok"
            # parked off-screen window must still be capturable
            t = tk.Tk()
            t.title("SR hide test")
            t.geometry("300x200+100+100")
            tk.Frame(t, bg="#ff0000", width=300, height=200).pack(fill="both", expand=True)
            hits = []
            tk.Button(t, bg="#ff0000", activebackground="#ff0000", relief="flat", bd=0,
                      command=lambda: hits.append("tap")).place(relx=0.05, rely=0.3, relwidth=0.4, relheight=0.4)
            t.bind("<Escape>", lambda e: hits.append("esc"))

            def pump(n=10):
                for _ in range(n):
                    t.update()
                    time.sleep(0.05)

            def remote_do(*args):
                """Like in real life: the target window handles its messages while we click."""
                box = []
                th = threading.Thread(target=lambda: box.append(src.remote_input(*args)), daemon=True)
                th.start()
                while th.is_alive():
                    t.update()
                    time.sleep(0.01)
                pump()
                return box[0] if box else (False, "no result")

            pump(20)
            hw = find_window("SR hide test")
            src = WindowSource()
            src.hwnd, src.title = hw, "SR hide test"
            r1 = remote_do("tap", 0.25, 0.5)
            tap_ok = "tap" in hits
            r2 = remote_do("esc")
            esc_ok = "esc" in hits
            remote = f" tap={'ok' if tap_ok else 'FAIL ' + r1[1]} esc={'ok' if esc_ok else 'FAIL ' + r2[1]}"
            src.request = "hide"
            src._hide_logic()
            for _ in range(10):
                t.update()
                time.sleep(0.05)
            off = is_offscreen(hw)
            w, h = client_size(hw)
            img = print_window(hw, w, h)
            red = img is not None and img.resize((1, 1)).getpixel((0, 0))[0] > 150
            del hits[:]
            r3 = remote_do("tap", 0.25, 0.5)             # hidden: comes back, gets clicked, hides again
            hid_click, hid_off = "tap" in hits, is_offscreen(hw)
            remote += (" tap_hidden=ok" if hid_click and hid_off else
                       f" tap_hidden=FAIL(click={hid_click} parked_again={hid_off} {r3[1]})")
            src.request = "show"
            src._hide_logic()
            back = not is_offscreen(hw)
            t.destroy()
            assert off and back, f"hide/show failed off={off} back={back}"
            extra += f" hide=ok offscreen_capture={'ok' if red else 'BLACK'}{remote}"
        log.write_text(f"OK windows={len(titles)} tray={pystray.Icon.__module__}{extra}\n", encoding="utf-8")
        return 0
    except Exception as e:
        import traceback
        log.write_text(f"FAIL {e!r}\n{traceback.format_exc()}", encoding="utf-8")
        return 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--selftest-swap" in sys.argv:          # CI: exercise the real update/restart path
        import shutil
        import traceback
        try:
            new = Path(sys.executable).with_name("ScreenRelay.update.exe")
            shutil.copyfile(sys.executable, new)
            launch_swap_and_restart(new, ["--selftest-marker"])
        except Exception:
            Path(sys.executable).with_name("swap-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return
    if "--selftest-marker" in sys.argv:
        Path(sys.executable).with_name("swap-ok.txt").write_text(APP_VERSION, encoding="utf-8")
        return
    autostart = "--autostart" in sys.argv
    # one copy only: if it's already running (e.g. hidden in the tray), just show it
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if IS_WIN:
        srv.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", 0x4), 1)
    try:
        srv.bind(("127.0.0.1", INSTANCE_PORT))
        srv.listen(4)
    except OSError:
        srv.close()
        if not autostart:
            try:
                socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=2).close()
            except OSError:
                pass
        return
    root = tk.Tk()
    app = App(root, autostart)
    app.listen_instances(srv)
    root.mainloop()


if __name__ == "__main__":
    main()
