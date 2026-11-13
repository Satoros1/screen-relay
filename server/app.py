"""
Screen relay server.

  streamer (PC)  --ws /ingest-->  server  --ws /ws-->  viewers (site / Telegram Mini App)

The streamer sends JPEG frames as binary WebSocket messages.
Every viewer always receives the *latest* frame, so slow connections
skip frames instead of building up delay.
"""
import asyncio
import hashlib
import hmac
import secrets
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

import httpx

import bot as tgbot
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

log = logging.getLogger("relay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # do not log signed download URLs

PUSH_TOKEN = os.environ.get("PUSH_TOKEN", "")
VIEW_KEY = os.environ.get("VIEW_KEY", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_IDS = {
    int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x
}
MAX_FRAME_BYTES = int(os.environ.get("MAX_FRAME_BYTES", 3_000_000))
MAX_VIEWERS = int(os.environ.get("MAX_VIEWERS", 20))
INITDATA_TTL = int(os.environ.get("INITDATA_TTL", 86400))
STATUS_EVERY = 2.0
# Updates for the desktop app when the GitHub repo is private (token with read access to it)
GH_TOKEN = os.environ.get("GH_TOKEN", "")
UPDATE_REPO = os.environ.get("UPDATE_REPO", "Satoros1/screen-relay")
EXE_NAME = "ScreenRelay.exe"
# Telegram: menu button that opens the Mini App + alerts when the stream goes down
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SET_MENU_BUTTON = os.environ.get("SET_MENU_BUTTON", "1") != "0"
ALERTS = os.environ.get("ALERTS", "1") != "0"
ALERT_AFTER = int(os.environ.get("ALERT_AFTER", 60))   # seconds a state must last before we alert
TG_API = os.environ.get("TG_API", "https://api.telegram.org")

if len(PUSH_TOKEN) < 16:
    raise SystemExit("PUSH_TOKEN не задан или короче 16 символов")
if not VIEW_KEY and not (BOT_TOKEN and tgbot.ADMINS):
    raise SystemExit("Нужен VIEW_KEY и/или BOT_TOKEN + ALLOWED_IDS, иначе никто не сможет смотреть")
SEND_TIMEOUT = 15          # a viewer that can't take a frame for this long is dropped (frozen tab, dead network)

STATIC = Path(__file__).parent / "static"


# ------------------------------------------------------------ telegram
async def tg(method: str, payload: dict) -> dict | None:
    if not BOT_TOKEN:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{TG_API}/bot{BOT_TOKEN}/{method}", json=payload)
        data = r.json()
        if not data.get("ok"):
            log.warning("Telegram %s: %s", method, data.get("description"))
        return data
    except Exception as e:
        log.warning("Telegram %s недоступен: %s", method, e)
        return None


def watch_button() -> dict | None:
    if not PUBLIC_URL.startswith("https://"):
        return None
    return {"inline_keyboard": [[{"text": "📺 Смотреть", "web_app": {"url": PUBLIC_URL + "/"}}]]}


async def setup_menu_button():
    if BOT_TOKEN and SET_MENU_BUTTON and PUBLIC_URL.startswith("https://"):
        res = await tg("setChatMenuButton", {"menu_button": {
            "type": "web_app", "text": "📺 Live", "web_app": {"url": PUBLIC_URL + "/"}}})
        if res and res.get("ok"):
            log.info("кнопка Mini App в боте настроена: %s", PUBLIC_URL)


def stream_state() -> str:
    if not hub.publisher_online:
        return "offline"
    if not hub.frame_ts or time.time() - hub.frame_ts > 30:
        return "no_frames"
    return "live"


ALERT_TEXT = {
    "offline": "🔴 Трансляция прервалась: ПК с эмулятором не на связи.",
    "no_frames": "🟠 ПК на связи, но кадров нет: окно эмулятора не найдено или закрыто.",
    "live": "🟢 Трансляция снова идёт.",
}


async def watchdog():
    """Message admins when the stream goes down / comes back (after ALERT_AFTER seconds)."""
    reported = None              # nothing is reported until the stream has been live once
    candidate, since = None, time.time()
    while True:
        await asyncio.sleep(5)
        st = stream_state()
        if st != candidate:
            candidate, since = st, time.time()
        if reported is None:
            if st == "live":
                reported = "live"
            continue
        if candidate != reported and time.time() - since >= ALERT_AFTER:
            reported = candidate
            log.info("состояние трансляции: %s", candidate)
            for uid in tgbot.ADMINS:
                payload = {"chat_id": uid, "text": ALERT_TEXT[candidate]}
                if candidate == "live" and watch_button():
                    payload["reply_markup"] = watch_button()
                await tg("sendMessage", payload)


def status_text() -> str:
    st = stream_state()
    head = {"live": "🟢 В эфире", "no_frames": "🟠 ПК на связи, кадров нет", "offline": "🔴 ПК не в сети"}[st]
    age = f", последний кадр {round(time.time() - hub.frame_ts)} с назад" if hub.frame_ts else ""
    text = f"{head}{age}\nСмотрят сейчас: {hub.viewers}\nЗрителей с доступом: {len(tgbot.store.users)}"
    t = tgbot.store.timer
    if tgbot.store.timer_active():
        text += (f"\n🏋️ {t['name']} в {tgbot.hhmm(t['start'])}, осталось {tgbot.left_text(t['end'] - time.time())}"
                 f" (до {tgbot.hhmm(t['end'])})")
    return text


BOT = None                          # tgbot.Bot when BOT_TOKEN is set
VIEWER_SOCKETS: dict[int, set] = {}
GUEST_SOCKETS: set = set()


async def kick_guests():
    for ws in list(GUEST_SOCKETS):
        try:
            await ws.send_text(json.dumps({"type": "auth", "ok": False, "reason": "expired"}))
            await ws.close(code=4410)
        except Exception:
            pass


async def kick_viewer(uid: int):
    for ws in list(VIEWER_SOCKETS.get(uid, ())):
        try:
            await ws.close(code=4403)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_app):
    tasks = []
    if BOT_TOKEN:
        tasks.append(asyncio.create_task(setup_menu_button()))
        if ALERTS and tgbot.ADMINS:
            tasks.append(asyncio.create_task(watchdog()))
        global BOT
        b = BOT = tgbot.Bot(BOT_TOKEN, TG_API, PUBLIC_URL, status_text, kick_viewer,
                            lambda: (hub.frame, hub.frame_ts), kick_guests, remote)
        tasks.append(asyncio.create_task(b.run()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def check_init_data(init_data: str) -> int | None:
    """Validate Telegram Mini App initData, return user id or None."""
    if not BOT_TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > INITDATA_TTL:
            return None
        return int(json.loads(pairs.get("user", "{}"))["id"])
    except (ValueError, KeyError, TypeError):
        return None


class Hub:
    def __init__(self) -> None:
        self.frame: bytes | None = None
        self.seq = 0
        self.frame_ts = 0.0
        self.cond = asyncio.Condition()
        self.publisher_gen = 0
        self.publisher_online = False
        self.viewers = 0

    async def publish(self, data: bytes) -> None:
        async with self.cond:
            self.frame = data
            self.seq += 1
            self.frame_ts = time.time()
            self.cond.notify_all()

    async def next_frame(self, last_seq: int, timeout: float) -> tuple[int, bytes | None]:
        async with self.cond:
            try:
                await asyncio.wait_for(self.cond.wait_for(lambda: self.seq != last_seq), timeout)
            except asyncio.TimeoutError:
                pass
            return self.seq, self.frame

    def status(self) -> dict:
        age = round(time.time() - self.frame_ts, 1) if self.frame_ts else None
        st = {
            "type": "status",
            "online": self.publisher_online,
            "age": age,
            "viewers": self.viewers,
        }
        if tgbot.store.timer_active():
            st["busy_until"] = tgbot.store.timer["end"]
            st["busy_name"] = tgbot.store.timer["name"]
        return st


hub = Hub()


def frac(v) -> float | None:
    """A coordinate as a fraction of the window: a real number in 0..1, else None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 0.0 <= f <= 1.0 else None


class Remote:
    """Admin channel to the desktop app: click / Esc commands and whole-window frames.

    Whole-window frames never go to ordinary viewers - only to admins (control view, bot).
    """

    def __init__(self) -> None:
        self.sender = None                 # async callable(dict) of the connected desktop app
        self.caps: set[str] = set()
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.last_cmd = 0.0
        self.full: bytes | None = None
        self.full_seq = 0
        self.cond = asyncio.Condition()
        self.controllers = 0
        self.tasks: set = set()

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def _send(self, obj: dict) -> bool:
        if self.sender is None:
            return False
        try:
            await self.sender(obj)
            return True
        except Exception:
            return False

    async def want_full(self, on: bool = True) -> None:
        await self._send({"cmd": "control", "on": on})

    async def command(self, cmd: dict, timeout: float = 12, need: str = "control") -> dict:
        if self.sender is None:
            return {"ok": False, "msg": "ПК не в сети"}
        if need not in self.caps:
            return {"ok": False, "msg": "обнови программу на ПК: «Проверить обновления»"}
        now = time.monotonic()
        if now - self.last_cmd < 0.7:
            return {"ok": False, "msg": "слишком часто, подожди секунду"}
        self.last_cmd = now
        self.next_id += 1
        cid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        try:
            if not await self._send({**cmd, "id": cid}):
                return {"ok": False, "msg": "ПК не в сети"}
            res = await asyncio.wait_for(fut, timeout)
            out = {"ok": bool(res.get("ok")), "msg": str(res.get("msg", ""))[:200]}
            if isinstance(res.get("tpl"), str) and len(res["tpl"]) < 200_000:
                out["tpl"] = res["tpl"]
            return out
        except asyncio.TimeoutError:
            return {"ok": False, "msg": "ПК не ответил"}
        finally:
            self.pending.pop(cid, None)

    async def push_rules(self, new: str | None = None) -> None:
        if "auto" in self.caps:
            await self._send({"cmd": "rules", "rules": tgbot.store.rules, "new": new})

    def on_result(self, m: dict) -> None:
        fut = self.pending.get(m.get("id"))
        if fut and not fut.done():
            fut.set_result(m)

    def drop(self) -> None:
        self.sender, self.caps = None, set()
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result({"ok": False, "msg": "ПК отключился"})

    async def publish_full(self, data: bytes) -> None:
        async with self.cond:
            self.full = data
            self.full_seq += 1
            self.cond.notify_all()

    async def next_full(self, last_seq: int, timeout: float) -> tuple[int, bytes | None]:
        async with self.cond:
            try:
                await asyncio.wait_for(self.cond.wait_for(lambda: self.full_seq != last_seq), timeout)
            except asyncio.TimeoutError:
                pass
            return self.full_seq, self.full

    async def fresh_full(self, timeout: float = 5) -> bytes | None:
        """Ask the PC for the whole window as it is right now (None if it didn't come)."""
        seq = self.full_seq
        await self.want_full()
        new_seq, frame = await self.next_full(seq, timeout)
        return frame if new_seq != seq else None


remote = Remote()


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health():
    return JSONResponse(hub.status())


# ------------------------------------------------------------ desktop app updates
_release_cache: dict = {"at": 0.0, "data": None}


def _push_auth(request: Request) -> bool:
    h = request.headers.get("authorization", "")
    return h.startswith("Bearer ") and same(h[7:], PUSH_TOKEN)


async def _latest_release() -> dict | None:
    if time.time() - _release_cache["at"] < 300 and _release_cache["data"]:
        return _release_cache["data"]
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                        headers={"Authorization": f"Bearer {GH_TOKEN}",
                                 "Accept": "application/vnd.github+json"})
        r.raise_for_status()
        rel = r.json()
    asset = next((a for a in rel.get("assets", []) if a["name"].lower() == EXE_NAME.lower()), None)
    if not asset:
        return None
    data = {"version": rel["tag_name"].lstrip("v"), "size": asset["size"], "asset_url": asset["url"]}
    _release_cache.update(at=time.time(), data=data)
    return data


@app.get("/update/latest")
async def update_latest(request: Request):
    if not _push_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not GH_TOKEN:
        return JSONResponse({"error": "GH_TOKEN not set"}, status_code=404)
    try:
        rel = await _latest_release()
    except httpx.HTTPError as e:
        log.warning("GitHub недоступен: %s", e)
        return JSONResponse({"error": "github unavailable"}, status_code=502)
    if not rel:
        return JSONResponse({"error": "no exe in latest release"}, status_code=404)
    return {"version": rel["version"], "size": rel["size"]}


@app.get("/update/download")
async def update_download(request: Request):
    if not _push_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not GH_TOKEN:
        return JSONResponse({"error": "GH_TOKEN not set"}, status_code=404)
    rel = await _latest_release()
    if not rel:
        return JSONResponse({"error": "no exe in latest release"}, status_code=404)
    client = httpx.AsyncClient(timeout=60, follow_redirects=True)
    req = client.build_request("GET", rel["asset_url"], headers={
        "Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/octet-stream"})
    resp = await client.send(req, stream=True)
    if resp.status_code != 200:
        await resp.aclose()
        await client.aclose()
        return JSONResponse({"error": f"github {resp.status_code}"}, status_code=502)

    async def body():
        try:
            async for chunk in resp.aiter_bytes(256 * 1024):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(body(), media_type="application/octet-stream",
                             headers={"Content-Length": str(rel["size"])})


@app.websocket("/ingest")
async def ingest(ws: WebSocket):
    await ws.accept()
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
    except Exception:
        await ws.close(code=4400)
        return
    if not same(str(hello.get("token", "")), PUSH_TOKEN):
        log.warning("ingest: неверный токен от %s", ws.client)
        await ws.close(code=4401)
        return

    hub.publisher_gen += 1
    my_gen = hub.publisher_gen
    hub.publisher_online = True
    await ws.send_text(json.dumps({"ok": True}))
    log.info("стример подключился: %s (v%s)", ws.client, hello.get("v", "?"))
    send_lock = asyncio.Lock()

    async def pub_send(obj: dict):
        async with send_lock:
            await asyncio.wait_for(ws.send_text(json.dumps(obj)), SEND_TIMEOUT)

    caps = hello.get("caps")
    remote.caps = {str(c) for c in caps} if isinstance(caps, list) else set()
    remote.sender = pub_send
    if remote.controllers:
        await remote.want_full()
    await remote.push_rules()

    async def report():   # tell the desktop app how many people are watching
        while True:
            await asyncio.sleep(3)
            await pub_send({"viewers": hub.viewers})

    reporter = asyncio.create_task(report())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            text = msg.get("text")
            if text:
                try:
                    m = json.loads(text)
                    if isinstance(m, dict) and m.get("type") == "result":
                        remote.on_result(m)
                    elif isinstance(m, dict) and m.get("type") == "event":
                        if BOT:
                            remote.spawn(BOT.on_auto_event(m))
                        else:
                            log.info("авто-нажатие: %s", m.get("msg"))
                except ValueError:
                    pass
                continue
            data = msg.get("bytes")
            if data:
                if data[:3] == b"F\xff\xd8":         # whole window, for admins only
                    if len(data) <= MAX_FRAME_BYTES:
                        await remote.publish_full(data[1:])
                    continue
                if data[:2] != b"\xff\xd8":
                    continue                     # not a JPEG - ignore garbage
                if len(data) > MAX_FRAME_BYTES:
                    log.warning("кадр слишком большой (%d байт), пропускаю", len(data))
                    continue
                await hub.publish(data)
    except WebSocketDisconnect:
        pass
    finally:
        reporter.cancel()
        if hub.publisher_gen == my_gen:
            hub.publisher_online = False
            remote.drop()
        log.info("стример отключился")


@app.websocket("/ws")
async def view(ws: WebSocket):
    await ws.accept()
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
    except Exception:
        await ws.close(code=4400)
        return

    who, uid, guest_until = None, None, None
    if hello.get("guest"):
        guest_until = tgbot.store.guest_until(str(hello["guest"]))
        if guest_until:
            who = "guest"
        else:
            await ws.send_text(json.dumps({"type": "auth", "ok": False, "reason": "expired"}))
            await ws.close(code=4410)
            return
    elif hello.get("initData"):
        uid = check_init_data(str(hello["initData"]))
        if uid is not None and tgbot.store.allowed(uid):
            who = f"tg:{uid}"
        else:
            uid = None
    elif hello.get("key") and same(str(hello["key"]), VIEW_KEY):
        who = "key"

    if who is None:
        await ws.send_text(json.dumps({"type": "auth", "ok": False}))
        await ws.close(code=4403)
        return
    if hub.viewers >= MAX_VIEWERS:
        await ws.send_text(json.dumps({"type": "auth", "ok": False, "reason": "full"}))
        await ws.close(code=4429)
        return

    await ws.send_text(json.dumps({"type": "auth", "ok": True, "until": guest_until,
                                   "admin": uid is not None and uid in tgbot.ADMINS}))
    if guest_until:
        GUEST_SOCKETS.add(ws)
    if uid is not None:
        VIEWER_SOCKETS.setdefault(uid, set()).add(ws)
    hub.viewers += 1
    log.info("зритель %s подключился (всего %d)", who, hub.viewers)
    last_seq = -1
    last_status = 0.0
    try:
        while True:
            seq, frame = await hub.next_frame(last_seq, STATUS_EVERY)
            if seq != last_seq and frame is not None:
                await asyncio.wait_for(ws.send_bytes(frame), SEND_TIMEOUT)
            last_seq = seq
            if guest_until and (time.time() >= guest_until or not tgbot.store.guest_until(str(hello["guest"]))):
                await ws.send_text(json.dumps({"type": "auth", "ok": False, "reason": "expired"}))
                await ws.close(code=4410)
                break
            now = time.monotonic()
            if now - last_status >= STATUS_EVERY:
                await asyncio.wait_for(ws.send_text(json.dumps(hub.status())), SEND_TIMEOUT)
                last_status = now
    except asyncio.TimeoutError:
        log.info("зритель %s не принимает кадры - отключаю", who)
        try:
            await ws.close(code=1011)
        except Exception:
            pass
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        GUEST_SOCKETS.discard(ws)
        if uid is not None:
            VIEWER_SOCKETS.get(uid, set()).discard(ws)
        hub.viewers -= 1
        log.info("зритель %s отключился (всего %d)", who, hub.viewers)


@app.websocket("/control")
async def control(ws: WebSocket):
    """Admin's control view: sees the whole window, a tap on the picture = a click on the PC."""
    await ws.accept()
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
    except Exception:
        await ws.close(code=4400)
        return
    uid = check_init_data(str(hello.get("initData", "")))
    if uid is None or uid not in tgbot.ADMINS:
        await ws.send_text(json.dumps({"type": "auth", "ok": False}))
        await ws.close(code=4403)
        return
    lock = asyncio.Lock()

    async def send(obj: dict):
        async with lock:
            await asyncio.wait_for(ws.send_text(json.dumps(obj, ensure_ascii=False)), SEND_TIMEOUT)

    await send({"type": "auth", "ok": True, "taps": tgbot.store.taps, "rules": tgbot.store.rules_public()})
    remote.controllers += 1
    log.info("админ %s открыл управление", uid)

    async def frames():
        last, asked = -1, 0.0
        while True:
            now = time.monotonic()
            if now - asked >= 10:                    # keep the PC sending whole-window frames
                await remote.want_full()
                asked = now
            seq, frame = await remote.next_full(last, 2.0)
            if seq != last and frame is not None:
                async with lock:
                    await asyncio.wait_for(ws.send_bytes(frame), SEND_TIMEOUT)
            last = seq
            await send({"type": "status", "online": hub.publisher_online,
                        "supported": "control" in remote.caps})

    pump = asyncio.create_task(frames())
    try:
        while True:
            m = json.loads(await ws.receive_text())
            if not isinstance(m, dict):
                continue
            kind = m.get("type")
            if kind == "tap":
                x, y = frac(m.get("x")), frac(m.get("y"))
                if x is None or y is None:
                    continue
                res = await remote.command({"cmd": "tap", "x": x, "y": y})
                log.info("админ %s: нажатие %.3f,%.3f -> %s", uid, x, y, res["msg"])
                await send({"type": "result", **res})
            elif kind == "key":
                res = await remote.command({"cmd": "key", "key": "esc"})
                log.info("админ %s: Esc -> %s", uid, res["msg"])
                await send({"type": "result", **res})
            elif kind == "save":
                x, y = frac(m.get("x")), frac(m.get("y"))
                name = " ".join(str(m.get("name", "")).split())[:24]
                if x is None or y is None or not name:
                    continue
                ok = tgbot.store.add_tap(name, x, y)
                await send({"type": "taps", "taps": tgbot.store.taps, "saved": ok})
            elif kind == "del":
                tgbot.store.del_tap(m.get("i"))
                await send({"type": "taps", "taps": tgbot.store.taps})
            elif kind == "learn":
                rect = [frac(v) for v in (m.get("rect") or [])[:4]]
                tap = [frac(v) for v in (m.get("tap") or [])[:2]]
                name = " ".join(str(m.get("name", "")).split())[:24] or "Окно"
                try:
                    hours = min(24.0, max(0.0, float(m.get("timer_h") or 0)))
                except (TypeError, ValueError):
                    hours = 0.0
                if len(rect) != 4 or None in rect or len(tap) != 2 or None in tap \
                        or rect[2] - rect[0] < 0.02 or rect[3] - rect[1] < 0.02:
                    await send({"type": "result", "ok": False, "msg": "обведи окно побольше"})
                    continue
                res = await remote.command({"cmd": "learn", "rect": rect}, need="auto")
                if not res["ok"] or not res.get("tpl"):
                    await send({"type": "result", "ok": False, "msg": res["msg"]})
                    continue
                rule = {"id": secrets.token_hex(4), "name": name, "rect": rect, "tap": tap,
                        "timer_h": round(hours, 2), "tpl": res["tpl"]}
                saved = tgbot.store.add_rule(rule)
                if saved:
                    await remote.push_rules(new=rule["id"])
                log.info("админ %s: авто-нажатие «%s» %s", uid, name, "сохранено" if saved else "НЕ сохранено")
                await send({"type": "rules", "rules": tgbot.store.rules_public(), "saved": saved,
                            "new": rule["id"] if saved else None})
            elif kind == "fire":
                res = await remote.command({"cmd": "fire", "rule": str(m.get("id", ""))}, need="auto")
                await send({"type": "result", **res})
            elif kind == "rule_del":
                if tgbot.store.del_rule(str(m.get("id", ""))):
                    await remote.push_rules()
                await send({"type": "rules", "rules": tgbot.store.rules_public()})
    except (WebSocketDisconnect, RuntimeError, OSError, ValueError, asyncio.TimeoutError):
        pass
    finally:
        pump.cancel()
        remote.controllers -= 1
        if remote.controllers <= 0:
            await remote.want_full(False)
        log.info("админ %s закрыл управление", uid)
