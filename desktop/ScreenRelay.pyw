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
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import mss
from PIL import Image, ImageDraw, ImageEnhance, ImageTk
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidURI

try:                      # tray icon (optional - without it the app just minimizes normally)
    import pystray
except Exception:
    pystray = None

APP_NAME = "Screen Relay"
APP_VERSION = "0.1.0"
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

    def grab(self, title: str, sct) -> tuple[Image.Image | None, str]:
        if not title:
            return None, "Выбери окно"
        if title == SCREEN or not IS_WIN:
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
                return None, f"Окно «{title}» не найдено - жду…"

        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 4)    # SW_SHOWNOACTIVATE - unminimize without stealing focus
            return None, "Окно было свёрнуто - разворачиваю"

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
                return img, "Захват: окно (можно перекрывать другими окнами)"
            self.black_count += 1
            if self.black_count < 3:
                return None, "Пробую захватить окно…"
            self.mode = "screen"
            self.retry_print_at = now + 30

        shot = sct.grab(screen_rect(self.hwnd))
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return img, "Захват: с экрана - не перекрывай окно эмулятора"


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
        self.status = "Запуск…"
        self.src = WindowSource()

    def get_jpeg(self):
        with self.lock:
            return self.jpeg_seq, self.jpeg

    def run(self):
        sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        last_digest, last_sent, last_preview = None, 0.0, 0.0
        while not self.stop_ev.is_set():
            t0 = time.monotonic()
            cfg = self.cfg
            try:
                img, self.status = self.src.grab(cfg.get("window_title", ""), sct)
            except Exception as e:
                img, self.status = None, f"Ошибка захвата: {e}"
                self.src.hwnd = None
            if img is not None:
                if t0 - last_preview >= 0.25:
                    pv = img.copy()
                    pv.thumbnail((1000, 1000))
                    with self.lock:
                        self.preview = pv
                        self.preview_seq += 1
                    last_preview = t0
                if self.streaming:
                    out = img
                    c = cfg.get("crop")
                    if c:
                        box = (round(c[0] * img.width), round(c[1] * img.height),
                               round(c[2] * img.width), round(c[3] * img.height))
                        if box[2] - box[0] >= 8 and box[3] - box[1] >= 8:
                            out = img.crop(box)
                    mw = cfg.get("max_width", "")
                    if str(mw).isdigit() and out.width > int(mw):
                        mw = int(mw)
                        out = out.resize((mw, max(1, round(out.height * mw / out.width))), Image.LANCZOS)
                    digest = hashlib.blake2b(out.tobytes(), digest_size=16).digest()
                    if digest != last_digest or t0 - last_sent >= 2:
                        buf = io.BytesIO()
                        out.save(buf, "JPEG", quality=int(cfg.get("quality", 70)))
                        with self.lock:
                            self.jpeg = buf.getvalue()
                            self.jpeg_seq += 1
                            self.out_size = out.size
                        last_digest, last_sent = digest, t0
            fps = max(1, int(cfg.get("fps", 8))) if self.streaming else 4
            self.stop_ev.wait(max(0.005, 1 / fps - (time.monotonic() - t0)))


# ============================================================ network
class Net(threading.Thread):
    def __init__(self, cap: Capture, server: str, token: str):
        super().__init__(daemon=True)
        self.cap, self.server, self.token = cap, server.strip(), token.strip()
        self.stop_ev = threading.Event()
        self.state = "connecting"     # connecting | live | retry | fatal
        self.message = "Подключение…"
        self.viewers = None
        self.frames = 0
        self.bytes = 0

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
                except ValueError:
                    pass

    async def _main(self):
        backoff = 1
        while not self.stop_ev.is_set():
            reader = None
            try:
                self.state, self.message = "connecting", "Подключение к серверу…"
                async with connect(self.server, max_size=None, ping_interval=20,
                                   open_timeout=10, close_timeout=2) as ws:
                    await ws.send(json.dumps({"token": self.token}))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if not reply.get("ok"):
                        raise ConnectionError("сервер не принял токен")
                    self.state, self.message, backoff = "live", "В эфире", 1
                    reader = asyncio.create_task(self._reader(ws))
                    last_seq = -1
                    while not self.stop_ev.is_set():
                        if reader.done():
                            reader.result()          # raises if the connection died
                            raise ConnectionError("соединение закрыто")
                        seq, data = self.cap.get_jpeg()
                        if data and seq != last_seq:
                            await ws.send(data)
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


# ============================================================ autostart
def startup_file() -> Path:
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / "ScreenRelay.bat")


def set_autostart(on: bool) -> None:
    f = startup_file()
    if not on:
        f.unlink(missing_ok=True)
        return
    if getattr(sys, "frozen", False):
        cmd = f'start "" "{sys.executable}" --autostart'
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = pyw if pyw.exists() else Path(sys.executable)
        cmd = f'start "" "{exe}" "{Path(__file__).resolve()}" --autostart'
    f.write_text("@echo off\r\n" + cmd + "\r\n", encoding="utf-8")


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

        root.title(f"{APP_NAME} {APP_VERSION}")
        self.win_icon = ImageTk.PhotoImage(make_icon("#27ae60"))
        root.iconphoto(True, self.win_icon)
        root.geometry("820x720")
        root.minsize(640, 560)
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
        self.server_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=6, pady=3)
        ttk.Label(conn, text="Токен:").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        self.token_var = tk.StringVar(value=self.cfg["token"])
        self.token_entry = ttk.Entry(conn, textvariable=self.token_var, show="•")
        self.token_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(conn, text="👁", width=3, command=self.toggle_token).grid(row=1, column=2, padx=6)
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
        self.server_var.trace_add("write", lambda *a: self.on_option())
        self.token_var.trace_add("write", lambda *a: self.on_option())

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)
        self.auto_var = tk.BooleanVar(value=self.cfg["start_on_launch"])
        ttk.Checkbutton(bottom, text="Начинать трансляцию сразу при запуске",
                        variable=self.auto_var, command=self.on_option).pack(anchor="w")
        self.tray_var = tk.BooleanVar(value=self.cfg["minimize_to_tray"])
        cb = ttk.Checkbutton(bottom, text="Сворачивать в трей у часов (трансляция продолжается)",
                             variable=self.tray_var, command=self.on_option)
        cb.pack(anchor="w")
        if not pystray:
            cb.state(["disabled"])
        if IS_WIN:
            self.boot_var = tk.BooleanVar(value=startup_file().exists())
            ttk.Checkbutton(bottom, text="Запускать вместе с Windows",
                            variable=self.boot_var, command=self.on_boot).pack(anchor="w")

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=8, pady=(4, 10))
        self.btn = ttk.Button(bar, text="▶  Начать трансляцию", style="Big.TButton", command=self.toggle)
        self.btn.pack(side="left")
        st = ttk.Frame(bar)
        st.pack(side="left", fill="x", expand=True, padx=12)
        self.state_label = tk.Label(st, text="● Остановлено", fg="#888", font=("Segoe UI", 11, "bold"), anchor="w")
        self.state_label.pack(fill="x")
        self.info_label = ttk.Label(st, text="", foreground="#666")
        self.info_label.pack(fill="x")
        self.update_area_label()
        self.q_label.config(text=str(self.q_var.get()))

    # ---------------------------------------------------- settings
    def toggle_token(self):
        self.token_entry.config(show="" if self.token_entry.cget("show") else "•")

    def push_cfg(self):
        self.cap.cfg = dict(self.cfg)
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
                        server=self.server_var.get().strip(), token=self.token_var.get().strip())
        self.push_cfg()

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
        self.net = Net(self.cap, self.cfg["server"], self.cfg["token"])
        self.net.start()
        self.btn.config(text="■  Остановить")
        for w in (self.server_entry, self.token_entry):
            w.config(state="disabled")
        self.last_stats = (time.monotonic(), 0, 0)
        self.update_tray("connecting")

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

    def set_state(self, text, color):
        self.state_label.config(text=f"● {text}", fg=color)

    def tick_status(self):
        try:
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
                self.info_label.config(text=f"{src_status}" + (f" · кадр {w}×{h}" if w else ""))
            else:
                self.info_label.config(text=src_status)
        finally:
            self.root.after(1000, self.tick_status)

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

    def quit(self):
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
        self.root.destroy()

    def listen_instances(self, srv: socket.socket):
        def loop():
            while True:
                try:
                    conn, _ = srv.accept()
                    conn.close()
                    self.ui_q.put(self.show_window)
                except OSError:
                    return
        threading.Thread(target=loop, daemon=True).start()


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
        log.write_text(f"OK windows={len(titles)} tray={pystray.Icon.__module__}\n", encoding="utf-8")
        return 0
    except Exception as e:
        import traceback
        log.write_text(f"FAIL {e!r}\n{traceback.format_exc()}", encoding="utf-8")
        return 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
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
