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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("relay.bot")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
STORE = DATA_DIR / "users.json"
INVITE_TTL = 24 * 3600
REQUEST_COOLDOWN = 600
MAX_TAPS = 12
MAX_RULES = 5

try:
    TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Amsterdam"))
except Exception:
    TZ = None


def hhmm(ts: float, seconds: bool = False) -> str:
    """Clock time in the owner's time zone (the server itself runs in UTC)."""
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M:%S" if seconds else "%H:%M")


def left_text(sec: float) -> str:
    m = max(0, int((sec + 59) // 60))
    return f"{m // 60} ч {m % 60:02d} мин" if m >= 60 else f"{m} мин"


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
        self.guests: dict[str, float] = {}     # browser guest token -> access ends at
        self.taps: list[dict] = []             # saved click points: {"name", "x", "y"} (fractions of the window)
        self.rules: list[dict] = []            # auto-click rules: popup template + where to click + timer hours
        self.timer: dict | None = None         # running timer: {"name", "start", "end", "hours", "warn", "msgs"}
        self.requests: dict[str, float] = {}   # "id" -> last request time (not saved)
        self.load()

    def load(self):
        try:
            data = json.loads(STORE.read_text(encoding="utf-8"))
            self.users = {str(k): v for k, v in data.get("users", {}).items()}
            now = time.time()
            self.invites = {k: v for k, v in data.get("invites", {}).items() if v > now}
            self.guests = {k: v for k, v in data.get("guests", {}).items() if v > now}
            self.taps = [t for t in data.get("taps", []) if isinstance(t, dict) and "name" in t][:MAX_TAPS]
            self.rules = [r for r in data.get("rules", []) if isinstance(r, dict) and r.get("tpl")][:MAX_RULES]
            self.timer = data.get("timer") or None
        except FileNotFoundError:
            pass
        except Exception as e:
            log.error("не смог прочитать %s: %s", STORE, e)

    def save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STORE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"users": self.users, "invites": self.invites, "guests": self.guests,
                                       "taps": self.taps, "rules": self.rules, "timer": self.timer},
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

    def add_tap(self, name: str, x: float, y: float) -> bool:
        self.taps = [t for t in self.taps if t["name"] != name]       # same name = replace
        if len(self.taps) >= MAX_TAPS:
            return False
        self.taps.append({"name": name, "x": round(x, 4), "y": round(y, 4)})
        self.save()
        return True

    def del_tap(self, i) -> bool:
        if isinstance(i, int) and 0 <= i < len(self.taps):
            self.taps.pop(i)
            self.save()
            return True
        return False

    def add_rule(self, rule: dict) -> bool:
        self.rules = [r for r in self.rules if r["name"] != rule["name"]]      # same name = replace
        if len(self.rules) >= MAX_RULES:
            return False
        self.rules.append(rule)
        self.save()
        return True

    def del_rule(self, rule_id) -> bool:
        n = len(self.rules)
        self.rules = [r for r in self.rules if r["id"] != rule_id]
        if len(self.rules) != n:
            self.save()
            return True
        return False

    def rule(self, rule_id) -> dict | None:
        return next((r for r in self.rules if r["id"] == rule_id), None)

    def rules_public(self) -> list[dict]:
        return [{k: r[k] for k in ("id", "name", "timer_h")} for r in self.rules]

    def timer_active(self) -> bool:
        return bool(self.timer) and self.timer["end"] > time.time()

    def new_guest(self, minutes: int) -> tuple[str, float]:
        now = time.time()
        self.guests = {k: v for k, v in self.guests.items() if v > now}
        token = secrets.token_urlsafe(12)
        self.guests[token] = now + minutes * 60
        self.save()
        return token, self.guests[token]

    def guest_until(self, token: str) -> float | None:
        exp = self.guests.get(token)
        return exp if exp and exp > time.time() else None

    def close_guests(self) -> int:
        n = len(self.guests)
        self.guests = {}
        self.save()
        return n

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

    def __init__(self, token: str, api: str, public_url: str, status_text, kick, get_frame=None,
                 kick_guests=None, remote=None):
        self.base = f"{api}/bot{token}"
        self.public_url = public_url
        self.status_text = status_text
        self.kick = kick
        self.get_frame = get_frame        # () -> (jpeg bytes | None, unix time of the frame)
        self.shot_at: dict[int, float] = {}
        self._kick_guests = kick_guests
        self.remote = remote              # click / Esc on the PC, whole-window snapshots
        self._timer_task = None
        self.username = ""
        self.client = httpx.AsyncClient(timeout=70)

    async def kick_guests(self):
        if self._kick_guests:
            await self._kick_guests()

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
            [{"text": "⏱ Гость на 15 мин", "callback_data": "guest:15"},
             {"text": "⏱ на 1 час", "callback_data": "guest:60"}],
            [{"text": "🎮 Управление", "callback_data": "ctl"},
             {"text": "📊 Статус", "callback_data": "status"}],
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
            if cmd == "/control":
                return await self.send_control(chat)
            if cmd == "/tap":
                parts = arg.replace(",", " ").split()
                if len(parts) == 2 and all(p.replace(".", "", 1).isdigit() for p in parts) \
                        and all(0 <= float(p) <= 100 for p in parts):
                    return await self.do_remote(chat, {"cmd": "tap", "x": float(parts[0]) / 100,
                                                       "y": float(parts[1]) / 100})
                return await self.send(chat, "Формат: <code>/tap 85 12</code> - 85% слева, 12% сверху окна.")
            if cmd == "/guest":
                mins = int(arg) if arg.strip().isdigit() and 1 <= int(arg) <= 1440 else 15
                return await self.send_guest(chat, mins)
            if cmd == "/add" and arg.strip().isdigit():
                store.add(int(arg), f"id {arg.strip()}")
                return await self.send(chat, f"✅ Добавил <code>{arg.strip()}</code>")
            return await self.send(
                chat,
                "<b>Ты админ трансляции.</b>\n\n"
                "➕ <b>Пригласить</b>: одноразовая ссылка на 24 ч, отправь её человеку.\n"
                "👥 <b>Зрители</b>: список, удалить в один клик.\n"
                "Если кто-то сам нажмёт Start, тебе придёт запрос с кнопками.\n\n"
                "⏱ <b>Гость</b>: ссылка для браузера на 15 мин / 1 час, без Telegram и ключа. "
                "Скинул другу, он посмотрел, потом доступ сам пропадает (/guest 30 - на 30 мин).\n"
                "📸 <b>Снимок</b> (/shot): текущий кадр картинкой.\n"
                "🎮 <b>Управление</b> (/control): выскочило окно и закрыло статистику - нажми «Назад» "
                "или свою сохранённую кнопку, я кликну на ПК и пришлю снимок.\n"
                "🎯 <b>Авто-нажатия</b>: программа сама замечает окно (например, «Enjoy your workout»), "
                "нажимает кнопку и запускает таймер. Настройка: 🎮 → Mini App → «🎯 Авто-нажатие».\n\n"
                "Команды: /control /guest /shot /invite /users /status /add &lt;id&gt;",
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
        if action == "ctl":
            return await self.send_control(chat)
        if action == "esc":
            return await self.do_remote(chat, {"cmd": "key", "key": "esc"})
        if action == "tap" and arg.isdigit() and int(arg) < len(store.taps):
            t = store.taps[int(arg)]
            return await self.do_remote(chat, {"cmd": "tap", "x": t["x"], "y": t["y"]}, t["name"])
        if action == "tapdel":
            if arg.isdigit():
                store.del_tap(int(arg))
            if not store.taps:
                return await self.send_control(chat)
            rows = [[{"text": f"❌ {t['name']}", "callback_data": f"tapdel:{i}"}] for i, t in enumerate(store.taps)]
            rows.append([{"text": "← Готово", "callback_data": "ctl"}])
            return await self.send(chat, "Какую кнопку удалить?", {"inline_keyboard": rows})
        if action == "auto":
            return await self.send_rules(chat)
        if action == "ruledel" and arg:
            if store.del_rule(arg) and self.remote:
                await self.remote.push_rules()
            return await self.send_rules(chat)
        if action == "rulefire" and arg:
            if not store.rule(arg):
                return await self.send_rules(chat)
            res = await self.remote.command({"cmd": "fire", "rule": arg}, need="auto") if self.remote \
                else {"ok": False, "msg": "управление не настроено"}
            if not res["ok"]:
                return await self.send(chat, f"⚠️ {html.escape(res['msg'])}")
            return await self.send(chat, "⏳ Нажимаю на ПК… Таймер придёт отдельным сообщением.")
        if action == "timerstart" and arg:
            rule = store.rule(arg)
            if rule:
                await self.start_timer(rule, True, "запущен вручную")
            return
        if action == "timerstop":
            await self.stop_timer(chat)
            return
        if action == "guest" and arg.isdigit():
            return await self.send_guest(chat, int(arg))
        if action == "guestclose":
            n = store.close_guests()
            await self.kick_guests()
            return await self.send(chat, f"🔒 Гостевые ссылки закрыты ({n}), гости отключены.")
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

    # ------------------------------------------------------------ remote control
    def control_kb(self):
        rows = [[{"text": "⬅️ Назад (Esc)", "callback_data": "esc"},
                 {"text": "🔄 Обновить снимок", "callback_data": "ctl"}]]
        taps = [{"text": f"🖱 {t['name']}", "callback_data": f"tap:{i}"} for i, t in enumerate(store.taps)]
        rows += [taps[i:i + 2] for i in range(0, len(taps), 2)]
        if self.public_url.startswith("https://"):
            rows.append([{"text": "👆 Нажать по картинке / добавить кнопку",
                          "web_app": {"url": self.public_url + "/?control=1"}}])
        last = []
        if store.taps:
            last.append({"text": "🗑 Удалить кнопку", "callback_data": "tapdel"})
        last.append({"text": f"🎯 Авто-нажатия ({len(store.rules)})", "callback_data": "auto"})
        rows.append(last)
        return {"inline_keyboard": rows}

    # ------------------------------------------------------------ auto rules + timer
    async def send_rules(self, chat: int):
        rows, lines = [], []
        for r in store.rules:
            t = f", таймер {r['timer_h']:g} ч" if r.get("timer_h") else ""
            lines.append(f"• <b>{html.escape(r['name'])}</b>: нажать в ({round(r['tap'][0] * 100)}%, "
                         f"{round(r['tap'][1] * 100)}%){t}")
            row = [{"text": f"▶ Нажать сейчас: {r['name'][:18]}", "callback_data": f"rulefire:{r['id']}"}]
            if r.get("timer_h"):
                row.append({"text": "⏱ Только таймер", "callback_data": f"timerstart:{r['id']}"})
            rows.append(row)
            rows.append([{"text": f"❌ Удалить «{r['name'][:20]}»", "callback_data": f"ruledel:{r['id']}"}])
        text = ("🎯 <b>Авто-нажатия</b>\nПрограмма на ПК раз в секунду ищет окно; увидела - нажимает "
                "кнопку, проверяет, что окно пропало, и запускает таймер.\n\n" +
                ("\n".join(lines) if lines else "Пока нет ни одного.") +
                "\n\nДобавить: «👆 Нажать по картинке» → «🎯 Авто-нажатие», пока окно открыто на ПК.")
        if self.public_url.startswith("https://"):
            rows.append([{"text": "👆 Настроить в Mini App", "web_app": {"url": self.public_url + "/?control=1"}}])
        rows.append([{"text": "← Управление", "callback_data": "ctl"}])
        await self.send(chat, text, {"inline_keyboard": rows})

    async def on_auto_event(self, ev: dict):
        """The PC saw a learned popup and pressed its button."""
        rule = store.rule(ev.get("rule"))
        if not rule:
            return
        ok, msg = bool(ev.get("ok")), str(ev.get("msg", ""))[:150]
        log.info("авто «%s»: %s", rule["name"], msg)
        if rule.get("timer_h"):
            if store.timer_active():
                log.info("таймер уже идёт - повторное окно игнорирую")
                return
            await self.start_timer(rule, ok, msg, ev.get("at"))
        elif not ok:
            for a in ADMINS:
                await self.send(a, f"⚠️ Авто-нажатие «{html.escape(rule['name'])}»: {html.escape(msg)}",
                                self.control_kb())

    def timer_text(self) -> str:
        t = store.timer
        left = t["end"] - time.time()
        text = (f"🏋️ <b>{html.escape(t['name'])}</b> в {hhmm(t['start'])}\n"
                f"⏳ Осталось: <b>{left_text(left)}</b>\n"
                f"🕒 {t['hours']:g} ч пройдут в <b>{hhmm(t['end'])}</b>")
        if t.get("warn"):
            text += f"\n⚠️ {html.escape(t['warn'])}"
        return text

    @staticmethod
    def timer_kb():
        return {"inline_keyboard": [[{"text": "⏹ Сбросить таймер", "callback_data": "timerstop"}]]}

    async def start_timer(self, rule: dict, ok: bool, msg: str, at: float | None = None):
        start = at if at and abs(time.time() - at) < 600 else time.time()
        hours = float(rule.get("timer_h") or 3)
        store.timer = {"name": rule["name"], "start": start, "end": start + hours * 3600, "hours": hours,
                       "warn": "" if ok else f"окно не закрылось сам ({msg}) - открой 🎮 Управление",
                       "msgs": {}}
        for a in ADMINS:
            res = await self.send(a, self.timer_text(), self.timer_kb())
            mid = ((res or {}).get("result") or {}).get("message_id")
            if mid:
                store.timer["msgs"][str(a)] = mid
        store.save()
        self.ensure_timer_loop()

    def ensure_timer_loop(self):
        if store.timer and (not self._timer_task or self._timer_task.done()):
            self._timer_task = asyncio.create_task(self.timer_loop())

    async def timer_loop(self):
        """Edit the timer messages every minute; when time is up delete them and say so."""
        t = store.timer
        while store.timer is t:
            left = t["end"] - time.time()
            if left <= 0:
                await self.finish_timer(t)
                return
            await asyncio.sleep(min(left, (left % 60) or 60) + 0.5)
            if store.timer is not t or t["end"] - time.time() <= 0:
                continue
            for chat, mid in list(t["msgs"].items()):
                await self.call("editMessageText", chat_id=int(chat), message_id=mid, text=self.timer_text(),
                                parse_mode="HTML", reply_markup=self.timer_kb())

    async def delete_timer_msgs(self, t: dict):
        for chat, mid in t.get("msgs", {}).items():
            await self.call("deleteMessage", chat_id=int(chat), message_id=mid)

    async def finish_timer(self, t: dict):
        store.timer = None
        store.save()
        await self.delete_timer_msgs(t)
        for a in ADMINS:
            await self.send(a, f"✅ {t['hours']:g} ч прошли ({html.escape(t['name'])} в {hhmm(t['start'])}) - "
                               f"можно уходить.")

    async def stop_timer(self, chat: int):
        t = store.timer
        if not t:
            return await self.send(chat, "Таймер не идёт.")
        store.timer = None
        store.save()
        await self.delete_timer_msgs(t)
        await self.send(chat, "⏹ Таймер сброшен.")

    async def send_photo(self, chat: int, frame: bytes, caption: str, kb=None) -> bool:
        try:
            data = {"chat_id": str(chat), "caption": caption}
            if kb:
                data["reply_markup"] = json.dumps(kb)
            r = await self.client.post(f"{self.base}/sendPhoto", data=data,
                                       files={"photo": ("shot.jpg", frame, "image/jpeg")})
            if r.json().get("ok"):
                return True
            log.warning("sendPhoto: %s", r.json().get("description"))
        except Exception as e:
            log.warning("sendPhoto недоступен: %s", e)
        return False

    async def send_control(self, chat: int, note: str = ""):
        """Whole window as it is on the PC right now + the control buttons."""
        frame = await self.remote.fresh_full() if self.remote else None
        head = (note + "\n" if note else "") + "Окно на ПК сейчас. Что нажать?"
        if frame and await self.send_photo(chat, frame, head, self.control_kb()):
            return
        why = "ПК не в сети или программа на ПК старее 0.1.6, снимка окна нет."
        await self.send(chat, (note + "\n" if note else "") + f"🎮 Управление. {why}", self.control_kb())

    async def do_remote(self, chat: int, cmd: dict, label: str = ""):
        if not self.remote:
            return await self.send(chat, "Управление не настроено.")
        res = await self.remote.command(cmd)
        if not res["ok"]:
            return await self.send(chat, f"⚠️ Не получилось: {html.escape(res['msg'])}", self.control_kb())
        await asyncio.sleep(1.2)                       # let the window react before the snapshot
        await self.send_control(chat, f"✅ {label + ': ' if label else ''}{res['msg']}")

    async def send_shot(self, chat: int, uid: int):
        now = time.time()
        if now - self.shot_at.get(uid, 0) < 3:          # no spamming
            return
        self.shot_at[uid] = now
        frame, ts = self.get_frame() if self.get_frame else (None, 0)
        if not frame:
            return await self.send(chat, "🔴 Кадров пока нет: ПК не в сети или трансляция не запущена.")
        age = int(now - ts)
        when = hhmm(ts, seconds=True)
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

    async def send_guest(self, chat: int, minutes: int):
        if not self.public_url.startswith("https://"):
            return await self.send(chat, "Не задан PUBLIC_URL в .env")
        token, until = store.new_guest(minutes)
        link = f"{self.public_url}/?g={token}"
        end = hhmm(until)
        await self.send(chat,
                        f"⏱ Гостевая ссылка на <b>{minutes} мин</b> (до {end}). "
                        f"Откроется в любом браузере, Telegram не нужен:\n\n{link}\n\n"
                        "Потом доступ пропадёт сам, открытая страница закроется.",
                        {"inline_keyboard": [[{"text": "🔒 Закрыть всех гостей сейчас",
                                               "callback_data": "guestclose"}]]})

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
            {"command": "control", "description": "Управление: закрыть окно на ПК (админ)"},
            {"command": "guest", "description": "Гостевая ссылка на 15 мин (админ)"},
            {"command": "invite", "description": "Пригласить (админ)"},
            {"command": "users", "description": "Зрители (админ)"},
            {"command": "status", "description": "Статус трансляции"},
        ])
        log.info("бот @%s запущен, админы: %s", self.username, sorted(ADMINS))
        self.ensure_timer_loop()          # a timer that was running before a restart / update
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
