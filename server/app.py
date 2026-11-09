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
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
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

if len(PUSH_TOKEN) < 16:
    raise SystemExit("PUSH_TOKEN не задан или короче 16 символов")
if not VIEW_KEY and not (BOT_TOKEN and ALLOWED_IDS):
    raise SystemExit("Нужен VIEW_KEY и/или BOT_TOKEN + ALLOWED_IDS, иначе никто не сможет смотреть")

STATIC = Path(__file__).parent / "static"
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


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
        return {
            "type": "status",
            "online": self.publisher_online,
            "age": age,
            "viewers": self.viewers,
        }


hub = Hub()


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
    log.info("стример подключился: %s", ws.client)

    async def report():   # tell the desktop app how many people are watching
        while True:
            await asyncio.sleep(3)
            await ws.send_text(json.dumps({"viewers": hub.viewers}))

    reporter = asyncio.create_task(report())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data:
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
        log.info("стример отключился")


@app.websocket("/ws")
async def view(ws: WebSocket):
    await ws.accept()
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
    except Exception:
        await ws.close(code=4400)
        return

    who = None
    if hello.get("initData"):
        uid = check_init_data(str(hello["initData"]))
        if uid is not None and uid in ALLOWED_IDS:
            who = f"tg:{uid}"
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

    await ws.send_text(json.dumps({"type": "auth", "ok": True}))
    hub.viewers += 1
    log.info("зритель %s подключился (всего %d)", who, hub.viewers)
    last_seq = -1
    last_status = 0.0
    try:
        while True:
            seq, frame = await hub.next_frame(last_seq, STATUS_EVERY)
            if seq != last_seq and frame is not None:
                await ws.send_bytes(frame)
            last_seq = seq
            now = time.monotonic()
            if now - last_status >= STATUS_EVERY:
                await ws.send_text(json.dumps(hub.status()))
                last_status = now
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.viewers -= 1
        log.info("зритель %s отключился (всего %d)", who, hub.viewers)
