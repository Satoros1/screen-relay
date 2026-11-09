"""
Telegram bot for access control.

Admins (ADMIN_IDS / ALLOWED_IDS from .env) manage who may watch:
  /invite  - one-time link, whoever opens it gets access
  /users   - list of viewers with a "remove" button for each
  /status  - is the stream live, how many viewers
Anyone else who presses Start -> admins get a request with Allow / Deny buttons.

Viewers are stored in DATA_DIR/users.json, so the list survives restarts and updates.
"""
import asyncio
import html
import json
import logging
import os
import secrets
import time
from pathlib import Path

import httpx

log = logging.getLogger("relay.bot")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
STORE = DATA_DIR / "users.json"
INVITE_TTL = 24 * 3600
REQUEST_COOLDOWN = 600


def _ids(raw: str) -> set[int]:
    out = set()
    for x in raw.replace(" ", "").split(","):
        if x.lstrip("-").isdigit():
            out.add(int(x))
    return out


ADMINS = _ids(os.environ.get("ADMIN_IDS", "")) or _ids(os.environ.get("ALLOWED_IDS", ""))


class Store:
    def __init__(self):
        self.users: dict[str, dict] = {}       # "id" -> {"name", "added"}
        self.invites: dict[str, float] = {}    # code -> expires_at
        self.requests: dict[str, float] = {}   # "id" -> last request time (not saved)
        self.load()

    def load(self):
        try:
            data = json.loads(STORE.read_text(encoding="utf-8"))
            self.users = {str(k): v for k, v in data.get("users", {}).items()}
            now = time.time()
            self.invites = {k: v for k, v in data.get("invites", {}).items() if v > now}
        except FileNotFoundError:
            pass
        except Exception as e:
            log.error("не смог прочитать %s: %s", STORE, e)

    def save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STORE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"users": self.users, "invites": self.invites},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(STORE)
        except Exception as e:
            log.error("не смог сохранить %s: %s", STORE, e)

    def allowed(self, uid: int) -> bool:
        return uid in ADMINS or str(uid) in self.users

    def add(self, uid: int, name: str):
        self.users[str(uid)] = {"name": name, "added": int(time.time())}
        self.requests.pop(str(uid), None)
        self.save()

    def remove(self, uid: int) -> bool:
        ok = self.users.pop(str(uid), None) is not None
        if ok:
            self.save()
        return ok

    def new_invite(self) -> str:
        now = time.time()
        self.invites = {k: v for k, v in self.invites.items() if v > now}
        code = secrets.token_urlsafe(9)
        self.invites[code] = now + INVITE_TTL
        self.save()
        return code

    def use_invite(self, code: str) -> bool:
        exp = self.invites.pop(code, None)
        if exp is None:
            return False
        self.save()
        return exp > time.time()


store = Store()


def display_name(u: dict) -> str:
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or "без имени"
    if u.get("username"):
        name += f" (@{u['username']})"
    return name


class Bot:
    """Long-polling bot. `hooks` gives access to the relay: status text, kicking viewers."""

    def __init__(self, token: str, api: str, public_url: str, status_text, kick, get_frame=None):
        self.base = f"{api}/bot{token}"
        self.public_url = public_url
        self.status_text = status_text
        self.kick = kick
        self.get_frame = get_frame        # () -> (jpeg bytes | None, unix time of the frame)
        self.shot_at: dict[int, float] = {}
        self.username = ""
        self.client = httpx.AsyncClient(timeout=70)

    async def call(self, method: str, **payload):
        try:
            r = await self.client.post(f"{self.base}/{method}", json=payload)
            data = r.json()
            if not data.get("ok"):
                log.warning("Telegram %s: %s", method, data.get("description"))
            return data
        except Exception as e:
            log.warning("Telegram %s недоступен: %s", method, e)
            return None

    def watch_kb(self):
        row = [{"text": "📸 Снимок", "callback_data": "shot"}]
        if self.public_url.startswith("https://"):
            row.insert(0, {"text": "📺 Смотреть", "web_app": {"url": self.public_url + "/"}})
        return {"inline_keyboard": [row]}

    def admin_kb(self):
        return {"inline_keyboard": [
            [{"text": "➕ Пригласить", "callback_data": "invite"},
             {"text": "👥 Зрители", "callback_data": "users"}],
            [{"text": "📊 Статус", "callback_data": "status"}],
        ] + self.watch_kb()["inline_keyboard"]}

    async def send(self, chat_id: int, text: str, kb=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if kb:
            payload["reply_markup"] = kb
        return await self.call("sendMessage", **payload)

    async def notify_admins(self, text: str, kb=None):
        for a in ADMINS:
            await self.send(a, text, kb)

    # ------------------------------------------------------------ handlers
    async def on_message(self, m: dict):
        u = m.get("from") or {}
        uid, chat = u.get("id"), m["chat"]["id"]
        if m["chat"].get("type") != "private" or not uid:
            return
        text = (m.get("text") or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()

        if cmd == "/shot":
            if store.allowed(uid):
                return await self.send_shot(chat, uid)
            return await self.send(chat, "Нет доступа. Нажми /start, чтобы запросить.")

        if uid in ADMINS:
            if cmd == "/invite":
                return await self.send_invite(chat)
            if cmd == "/users":
                return await self.send_users(chat)
            if cmd == "/status":
                return await self.send(chat, self.status_text())
            if cmd == "/add" and arg.strip().isdigit():
                store.add(int(arg), f"id {arg.strip()}")
                return await self.send(chat, f"✅ Добавил <code>{arg.strip()}</code>")
            return await self.send(
                chat,
                "<b>Ты админ трансляции.</b>\n\n"
                "➕ <b>Пригласить</b>: одноразовая ссылка на 24 ч, отправь её человеку.\n"
                "👥 <b>Зрители</b>: список, удалить в один клик.\n"
                "Если кто-то сам нажмёт Start, тебе придёт запрос с кнопками.\n\n"
                "📸 <b>Снимок</b> (/shot): текущий кадр картинкой.\n\n"
                "Команды: /shot /invite /users /status /add &lt;id&gt;",
                self.admin_kb())

        if cmd == "/start" and arg.startswith("inv_"):
            if store.use_invite(arg[4:]):
                store.add(uid, display_name(u))
                await self.notify_admins(f"✅ По приглашению добавлен: {html.escape(display_name(u))} "
                                         f"<code>{uid}</code>")
                return await self.send(chat, "Готово, доступ открыт 🎉", self.watch_kb())
            return await self.send(chat, "Ссылка уже использована или устарела. Попроси новую.")

        if store.allowed(uid):
            return await self.send(chat, "Трансляция тут 👇 (или кнопка «📺 Live» слева от поля ввода).\n"
                                         "📸 /shot - прислать текущий кадр картинкой.",
                                   self.watch_kb())

        # unknown user -> request to admins
        key = str(uid)
        if time.time() - store.requests.get(key, 0) < REQUEST_COOLDOWN:
            return await self.send(chat, "Запрос уже отправлен, жди ответа.")
        store.requests[key] = time.time()
        await self.notify_admins(
            f"🙋 Просит доступ: {html.escape(display_name(u))} <code>{uid}</code>",
            {"inline_keyboard": [[{"text": "✅ Разрешить", "callback_data": f"allow:{uid}"},
                                  {"text": "❌ Отклонить", "callback_data": f"deny:{uid}"}]]})
        await self.send(chat, "Запрос отправлен владельцу. Как только он разрешит, я напишу.")

    async def on_callback(self, q: dict):
        uid = (q.get("from") or {}).get("id")
        data = q.get("data") or ""
        msg = q.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        await self.call("answerCallbackQuery", callback_query_id=q["id"])
        if data == "shot" and chat and uid and store.allowed(uid):
            return await self.send_shot(chat, uid)
        if uid not in ADMINS or not chat:
            return
        action, _, arg = data.partition(":")
        if action == "invite":
            return await self.send_invite(chat)
        if action == "users":
            return await self.send_users(chat)
        if action == "status":
            return await self.send(chat, self.status_text())
        if not arg.lstrip("-").isdigit():
            return
        target = int(arg)
        if action == "allow":
            name = self.request_name(msg) or f"id {target}"
            store.add(target, name)
            await self.edit(msg, f"✅ Разрешено: {html.escape(name)} <code>{target}</code>")
            await self.send(target, "Доступ открыт 🎉", self.watch_kb())
        elif action == "deny":
            store.requests[str(target)] = time.time() + 86400   # silence for a day
            await self.edit(msg, f"❌ Отклонено: <code>{target}</code>")
        elif action == "del":
            if store.remove(target):
                await self.kick(target)
                await self.send(chat, f"🗑 Удалён <code>{target}</code>, трансляция у него закрыта.")
            await self.send_users(chat, edit=msg)

    @staticmethod
    def request_name(msg: dict) -> str:
        t = msg.get("text") or ""
        if ":" in t:
            return t.split(":", 1)[1].rsplit(" ", 1)[0].strip()
        return ""

    async def edit(self, msg: dict, text: str):
        await self.call("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                        text=text, parse_mode="HTML")

    async def send_shot(self, chat: int, uid: int):
        now = time.time()
        if now - self.shot_at.get(uid, 0) < 3:          # no spamming
            return
        self.shot_at[uid] = now
        frame, ts = self.get_frame() if self.get_frame else (None, 0)
        if not frame:
            return await self.send(chat, "🔴 Кадров пока нет: ПК не в сети или трансляция не запущена.")
        age = int(now - ts)
        when = time.strftime("%H:%M:%S", time.localtime(ts))
        caption = f"📸 {when}" + (f"  ⚠️ кадр старый ({age // 60} мин) - ПК не в сети?" if age > 60 else "")
        try:
            r = await self.client.post(
                f"{self.base}/sendPhoto",
                data={"chat_id": str(chat), "caption": caption,
                      "reply_markup": json.dumps(self.watch_kb())},
                files={"photo": ("shot.jpg", frame, "image/jpeg")})
            if not r.json().get("ok"):
                log.warning("sendPhoto: %s", r.json().get("description"))
        except Exception as e:
            log.warning("sendPhoto недоступен: %s", e)

    async def send_invite(self, chat: int):
        if not self.username:
            return await self.send(chat, "Не знаю имя бота, проверь BOT_TOKEN")
        link = f"https://t.me/{self.username}?start=inv_{store.new_invite()}"
        await self.send(chat, "Одноразовая ссылка, действует 24 часа. Перешли её человеку:\n\n"
                              f"{link}")

    async def send_users(self, chat: int, edit: dict | None = None):
        rows, lines = [], []
        for k, v in sorted(store.users.items(), key=lambda kv: kv[1].get("added", 0)):
            lines.append(f"• {html.escape(v.get('name', '?'))} <code>{k}</code>")
            rows.append([{"text": f"❌ {v.get('name', k)[:30]}", "callback_data": f"del:{k}"}])
        text = ("<b>Зрители</b> (админы видят всегда):\n" + "\n".join(lines)) if lines \
            else "Зрителей пока нет. Нажми «➕ Пригласить»."
        rows.append([{"text": "➕ Пригласить", "callback_data": "invite"}])
        kb = {"inline_keyboard": rows}
        if edit:
            await self.call("editMessageText", chat_id=edit["chat"]["id"], message_id=edit["message_id"],
                            text=text, parse_mode="HTML", reply_markup=kb)
        else:
            await self.send(chat, text, kb)

    # ------------------------------------------------------------ loop
    async def run(self):
        me = await self.call("getMe")
        self.username = ((me or {}).get("result") or {}).get("username", "")
        await self.call("deleteWebhook")
        await self.call("setMyCommands", commands=[
            {"command": "start", "description": "Открыть трансляцию"},
            {"command": "shot", "description": "Снимок: текущий кадр"},
            {"command": "invite", "description": "Пригласить (админ)"},
            {"command": "users", "description": "Зрители (админ)"},
            {"command": "status", "description": "Статус трансляции"},
        ])
        log.info("бот @%s запущен, админы: %s", self.username, sorted(ADMINS))
        offset = 0
        while True:
            try:
                r = await self.client.post(f"{self.base}/getUpdates", json={
                    "offset": offset, "timeout": 50, "allowed_updates": ["message", "callback_query"]})
                data = r.json()
                if not data.get("ok"):
                    if data.get("error_code") == 409:
                        log.error("этот BOT_TOKEN уже используется другим ботом/вебхуком - нужен отдельный бот")
                    await asyncio.sleep(10)
                    continue
                for upd in data["result"]:
                    offset = upd["update_id"] + 1
                    try:
                        if "message" in upd:
                            await self.on_message(upd["message"])
                        elif "callback_query" in upd:
                            await self.on_callback(upd["callback_query"])
                    except Exception:
                        log.exception("ошибка обработки апдейта")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("getUpdates: %s", e)
                await asyncio.sleep(5)
