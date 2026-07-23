# -*- coding: utf-8 -*-
# ============================================================
# БибиЗадачи — бот и мини-приложение для заданий и бибибонусов.
# Отдельный бот: НЕ трекер смен. Здесь пользователи из группы
# регистрируются, берут задания на карте, выполняют и получают
# бибибонусы (внутренняя валюта на бесплатные поездки).
#
# Дизайн и концепт взяты из рабочего трекера смен, механика — новая.
# Стек тот же: Aiogram 3 + aiohttp + SQLite. Данные — в DATA_DIR.
# ============================================================
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import secrets
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
)

BUILD_VERSION = "2026-07-23 · БибиЗадачи v1 (задания + бибибонусы)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("bibitasks")

# ── Конфигурация из окружения ─────────────────────────────────
BOT_TOKEN = (
    os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("API_TOKEN")
)
BOT_USERNAME = os.getenv("BOT_USERNAME", "")            # без @, для ссылки на Mini App
WEBAPP_SHORTNAME = os.getenv("WEBAPP_SHORTNAME", "app")  # Direct Link короткое имя
WEBAPP_PORT = int(os.getenv("PORT") or os.getenv("WEB_PORT") or 3000)
INIT_DATA_MAX_AGE_SEC = int(os.getenv("INIT_DATA_MAX_AGE_SEC", "86400"))

# Группа сообщества и тема «Работа» — для приветствия и ссылок.
COMMUNITY_CHAT_ID = int(os.getenv("COMMUNITY_CHAT_ID", "0") or "0")

# Кто может модерировать заявки и подтверждать задания (Telegram user_id
# через запятую). На старте — вручную; позже свяжем с ролями в БД.
def _parse_ids(raw):
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out

ADMIN_IDS = _parse_ids(os.getenv("ADMIN_IDS", ""))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR") or BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bibitasks.db")
INDEX_PATH = os.path.join(BASE_DIR, "bibitasks.html")

print("=" * 60, flush=True)
print(f"== {BUILD_VERSION}", flush=True)
print(f"== рабочая папка: {BASE_DIR}", flush=True)
print(f"== база: {DB_PATH}", flush=True)
print(f"== bibitasks.html рядом: {os.path.exists(INDEX_PATH)}", flush=True)
print(f"== порт: {WEBAPP_PORT}", flush=True)
print(f"== токен найден: {'да' if BOT_TOKEN else 'НЕТ'}", flush=True)
print(f"== админов в ADMIN_IDS: {len(ADMIN_IDS)}", flush=True)
print("=" * 60, flush=True)

if not BOT_TOKEN:
    print("КРИТИЧЕСКАЯ ОШИБКА: не задан токен бота (BOT_TOKEN / TOKEN).", flush=True)
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)

# ── Каталог типов заданий (можно расширять) ───────────────────
TASK_TYPES = {
    "relocate": {"title": "Развоз с сервиса", "emoji": "📦",
                 "desc": "Забрать байки с СЦ и расставить по точкам"},
    "fix_zone": {"title": "Обслуживание зоны", "emoji": "🔧",
                 "desc": "Проверить и поправить байки в районе"},
    "charge":   {"title": "Подзарядка", "emoji": "🔋",
                 "desc": "Заменить батареи в зоне"},
    "rescue":   {"title": "Спасение байка", "emoji": "🆘",
                 "desc": "Поднять/перевезти упавший или проблемный байк"},
}

# Уровни доверия: (ключ, название, эмодзи, порог выполненных задач)
TRUST_LEVELS = [
    ("novice",   "Новичок",     "🌱", 0),
    ("trusted",  "Проверенный", "⭐", 10),
    ("ambassador", "Амбассадор", "👑", 40),
]


def trust_for(count):
    """Уровень доверия по числу подтверждённых заданий."""
    level = TRUST_LEVELS[0]
    for item in TRUST_LEVELS:
        if count >= item[3]:
            level = item
    return level


def next_trust(count):
    """Следующий уровень и сколько заданий до него (или None)."""
    for item in TRUST_LEVELS:
        if count < item[3]:
            return item
    return None


# ============================================================
# БАЗА ДАННЫХ
# ============================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS members (
                user_id     INTEGER PRIMARY KEY,
                full_name   TEXT,
                username    TEXT,
                phone       TEXT,
                role        TEXT NOT NULL DEFAULT 'candidate',  -- candidate|helper|employee|admin
                status      TEXT NOT NULL DEFAULT 'pending',    -- pending|approved|blocked
                bonus       INTEGER NOT NULL DEFAULT 0,
                done_count  INTEGER NOT NULL DEFAULT 0,
                referred_by INTEGER,
                created_at  TEXT,
                approved_at TEXT,
                approved_by INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT NOT NULL,
                title       TEXT NOT NULL,
                details     TEXT,
                lat         REAL,
                lng         REAL,
                address     TEXT,
                reward      INTEGER NOT NULL DEFAULT 0,
                status      TEXT NOT NULL DEFAULT 'open',   -- open|claimed|review|done|cancelled
                created_by  INTEGER,
                created_at  TEXT,
                claimed_by  INTEGER,
                claimed_at  TEXT,
                done_at     TEXT,
                proof_note  TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bonus_ledger (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                amount     INTEGER NOT NULL,           -- + начисление / - списание
                reason     TEXT NOT NULL,
                task_id    INTEGER,
                created_by INTEGER,
                created_at TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_user ON bonus_ledger(user_id)")
        await db.commit()

        # Сидим админов из ADMIN_IDS как одобренных с ролью admin.
        for uid in ADMIN_IDS:
            await db.execute(
                "INSERT INTO members (user_id, role, status, created_at) "
                "VALUES (?, 'admin', 'approved', ?) "
                "ON CONFLICT(user_id) DO UPDATE SET role='admin', status='approved'",
                (uid, datetime.now(timezone.utc).isoformat())
            )
        await db.commit()
    logger.info("База БибиЗадачи готова.")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


async def get_member(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM members WHERE user_id = ?", (uid,))).fetchone()
        return dict(row) if row else None


async def is_admin(uid):
    if uid in ADMIN_IDS:
        return True
    m = await get_member(uid)
    return bool(m and m["role"] == "admin" and m["status"] == "approved")


async def upsert_member(uid, **fields):
    m = await get_member(uid)
    async with aiosqlite.connect(DB_PATH) as db:
        if not m:
            await db.execute(
                "INSERT INTO members (user_id, created_at) VALUES (?, ?)",
                (uid, now_iso()))
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            await db.execute(
                f"UPDATE members SET {cols} WHERE user_id = ?",
                (*fields.values(), uid))
        await db.commit()


async def add_bonus(uid, amount, reason, task_id=None, by=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE members SET bonus = bonus + ? WHERE user_id = ?", (amount, uid))
        await db.execute(
            "INSERT INTO bonus_ledger (user_id, amount, reason, task_id, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, amount, reason, task_id, by, now_iso()))
        await db.commit()


# ============================================================
# ПРОВЕРКА ПОДПИСИ TELEGRAM (как в рабочем боте)
# ============================================================
def _check_webapp_auth(init_data: str):
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    recv_hash = parsed.pop("hash", None)
    if not recv_hash:
        return None
    data_check = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return None
    try:
        auth_date = int(parsed.get("auth_date", "0"))
        age = int(datetime.now(timezone.utc).timestamp()) - auth_date
        if auth_date <= 0 or age < -60 or age > INIT_DATA_MAX_AGE_SEC:
            return None
    except (TypeError, ValueError):
        return None
    try:
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None


def _get_init_data(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("tma "):
        return auth[4:]
    return request.headers.get("X-Init-Data", "")


async def _auth_user(request):
    tg_user = _check_webapp_auth(_get_init_data(request))
    if not tg_user or "id" not in tg_user:
        return None
    return tg_user


# ============================================================
# API МИНИ-ПРИЛОЖЕНИЯ
# ============================================================
def _json(data, status=200):
    resp = web.json_response(data, status=status)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, X-Init-Data, X-Admin-Token")
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


async def _body(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _member_public(m):
    """Что отдаём во фронт о самом пользователе."""
    done = m.get("done_count", 0)
    key, name, emoji, _ = trust_for(done)
    nxt = next_trust(done)
    return {
        "user_id": m["user_id"],
        "name": m.get("full_name") or "",
        "role": m.get("role"),
        "status": m.get("status"),
        "bonus": m.get("bonus", 0),
        "done_count": done,
        "trust_key": key,
        "trust_name": name,
        "trust_emoji": emoji,
        "next_trust_name": (nxt[1] if nxt else None),
        "next_trust_at": (nxt[3] if nxt else None),
    }


async def api_state(request):
    """Главное состояние: кто пользователь, его статус, бонусы, доступ."""
    tg = await _auth_user(request)
    if not tg:
        return _json({"error": "auth"}, status=401)
    uid = tg["id"]
    m = await get_member(uid)
    if not m:
        # Первый визит — заводим кандидата, но без заявки (status pending, role candidate)
        await upsert_member(
            uid,
            full_name=(tg.get("first_name", "") + " " + tg.get("last_name", "")).strip(),
            username=tg.get("username", ""))
        m = await get_member(uid)
    admin = await is_admin(uid)
    can_work = admin or (m["status"] == "approved" and m["role"] in ("helper", "employee", "admin"))
    return _json({
        "ok": True,
        "build_version": BUILD_VERSION,
        "bot_username": BOT_USERNAME,
        "me": _member_public(m),
        "is_admin": admin,
        "can_work": can_work,
        "task_types": [
            {"key": k, **v} for k, v in TASK_TYPES.items()
        ],
        "trust_levels": [
            {"key": k, "name": n, "emoji": e, "at": t} for k, n, e, t in TRUST_LEVELS
        ],
    })


async def api_apply(request):
    """Заявка «Хочу помогать»: кандидат отправляет имя и телефон."""
    tg = await _auth_user(request)
    if not tg:
        return _json({"error": "auth"}, status=401)
    body = await _body(request)
    name = (body.get("name") or "").strip()[:80]
    phone = (body.get("phone") or "").strip()[:32]
    if len(name) < 2:
        return _json({"error": "name", "message": "Укажите имя."}, status=400)
    uid = tg["id"]
    m = await get_member(uid)
    if m and m["status"] == "approved":
        return _json({"ok": True, "already": True})
    await upsert_member(
        uid, full_name=name, phone=phone,
        username=tg.get("username", ""),
        role="candidate", status="pending")
    # Уведомляем админов о новой заявке.
    for admin_id in await _all_admin_ids():
        try:
            await bot.send_message(
                admin_id,
                f"🆕 Новая заявка на помощь\n"
                f"Имя: {name}\n"
                f"Телефон: {phone or '—'}\n"
                f"Ник: @{tg.get('username','') or '—'}\n"
                f"ID: {uid}\n\n"
                f"Открой приложение → Модерация, чтобы одобрить.")
        except Exception:
            pass
    return _json({"ok": True})


async def _all_admin_ids():
    ids = set(ADMIN_IDS)
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT user_id FROM members WHERE role='admin' AND status='approved'"
        )).fetchall()
        ids.update(r[0] for r in rows)
    return ids


async def api_tasks_available(request):
    """Список открытых заданий + задания, взятые этим пользователем."""
    tg = await _auth_user(request)
    if not tg:
        return _json({"error": "auth"}, status=401)
    uid = tg["id"]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        open_rows = await (await db.execute(
            "SELECT * FROM tasks WHERE status='open' ORDER BY created_at DESC LIMIT 100"
        )).fetchall()
        mine_rows = await (await db.execute(
            "SELECT * FROM tasks WHERE claimed_by=? AND status IN ('claimed','review') "
            "ORDER BY claimed_at DESC", (uid,)
        )).fetchall()
    return _json({
        "ok": True,
        "available": [_task_public(dict(r)) for r in open_rows],
        "mine": [_task_public(dict(r)) for r in mine_rows],
    })


def _task_public(t):
    meta = TASK_TYPES.get(t.get("type"), {})
    return {
        "id": t["id"],
        "type": t.get("type"),
        "type_title": meta.get("title", t.get("type")),
        "emoji": meta.get("emoji", "📍"),
        "title": t.get("title"),
        "details": t.get("details") or "",
        "lat": t.get("lat"), "lng": t.get("lng"),
        "address": t.get("address") or "",
        "reward": t.get("reward", 0),
        "status": t.get("status"),
        "claimed_by": t.get("claimed_by"),
    }


async def _require_worker(request):
    """Пропускает только одобренных работников/админов."""
    tg = await _auth_user(request)
    if not tg:
        return None, _json({"error": "auth"}, status=401)
    uid = tg["id"]
    m = await get_member(uid)
    admin = await is_admin(uid)
    ok = admin or (m and m["status"] == "approved"
                   and m["role"] in ("helper", "employee", "admin"))
    if not ok:
        return None, _json(
            {"error": "not_approved", "message": "Заявка ещё не одобрена."}, status=403)
    return uid, None


async def api_task_claim(request):
    uid, err = await _require_worker(request)
    if err:
        return err
    body = await _body(request)
    tid = body.get("task_id")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,))).fetchone()
        if not row:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if row[0] != "open":
            await db.rollback()
            return _json({"error": "taken", "message": "Задание уже взято."}, status=409)
        await db.execute(
            "UPDATE tasks SET status='claimed', claimed_by=?, claimed_at=? WHERE id=?",
            (uid, now_iso(), tid))
        await db.commit()
    return _json({"ok": True})


async def api_task_complete(request):
    uid, err = await _require_worker(request)
    if err:
        return err
    body = await _body(request)
    tid = body.get("task_id")
    note = (body.get("note") or "").strip()[:300]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM tasks WHERE id=?", (tid,))).fetchone()
        if not row or row["claimed_by"] != uid:
            return _json({"error": "not_yours"}, status=403)
        await db.execute(
            "UPDATE tasks SET status='review', done_at=?, proof_note=? WHERE id=?",
            (now_iso(), note, tid))
        await db.commit()
    for admin_id in await _all_admin_ids():
        try:
            await bot.send_message(
                admin_id,
                f"✅ Задание #{tid} отправлено на проверку.\n"
                f"Комментарий: {note or '—'}\n"
                f"Открой Модерацию, чтобы подтвердить и начислить бонусы.")
        except Exception:
            pass
    return _json({"ok": True})


async def api_wallet(request):
    tg = await _auth_user(request)
    if not tg:
        return _json({"error": "auth"}, status=401)
    uid = tg["id"]
    m = await get_member(uid) or {}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT amount, reason, task_id, created_at FROM bonus_ledger "
            "WHERE user_id=? ORDER BY id DESC LIMIT 100", (uid,))).fetchall()
    return _json({
        "ok": True,
        "bonus": m.get("bonus", 0),
        "history": [dict(r) for r in rows],
    })


# ── Админ: модерация заявок и заданий ─────────────────────────
async def _require_admin(request):
    tg = await _auth_user(request)
    if not tg:
        return None, _json({"error": "auth"}, status=401)
    if not await is_admin(tg["id"]):
        return None, _json({"error": "not_admin"}, status=403)
    return tg["id"], None


async def api_admin_overview(request):
    """Сводка для админа: заявки, задания на проверке, открытые задания."""
    uid, err = await _require_admin(request)
    if err:
        return err
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        pending = await (await db.execute(
            "SELECT user_id, full_name, phone, username, created_at FROM members "
            "WHERE status='pending' ORDER BY created_at DESC")).fetchall()
        review = await (await db.execute(
            "SELECT * FROM tasks WHERE status='review' ORDER BY done_at DESC")).fetchall()
        open_tasks = await (await db.execute(
            "SELECT * FROM tasks WHERE status IN ('open','claimed') "
            "ORDER BY created_at DESC LIMIT 100")).fetchall()
        team = await (await db.execute(
            "SELECT user_id, full_name, role, bonus, done_count FROM members "
            "WHERE status='approved' ORDER BY done_count DESC LIMIT 100")).fetchall()
    return _json({
        "ok": True,
        "pending": [dict(r) for r in pending],
        "review": [_task_public(dict(r)) for r in review],
        "open_tasks": [_task_public(dict(r)) for r in open_tasks],
        "team": [{
            "user_id": r["user_id"], "name": r["full_name"], "role": r["role"],
            "bonus": r["bonus"], "done_count": r["done_count"],
            "trust_name": trust_for(r["done_count"])[1],
            "trust_emoji": trust_for(r["done_count"])[2],
        } for r in team],
    })


async def api_admin_decide(request):
    """Одобрить или отклонить заявку кандидата."""
    admin_id, err = await _require_admin(request)
    if err:
        return err
    body = await _body(request)
    uid = body.get("user_id")
    decision = body.get("decision")   # approve | reject
    m = await get_member(uid)
    if not m:
        return _json({"error": "not_found"}, status=404)
    if decision == "approve":
        await upsert_member(
            uid, status="approved", role="helper",
            approved_at=now_iso(), approved_by=admin_id)
        # Реферальный бонус пригласившему — при первом одобрении.
        if m.get("referred_by"):
            await add_bonus(m["referred_by"], 50, "Реферал одобрен", by=admin_id)
            try:
                await bot.send_message(
                    m["referred_by"], "🎉 Твой друг одобрен — тебе +50 бибибонусов!")
            except Exception:
                pass
        try:
            await bot.send_message(
                uid, "🎉 Заявка одобрена! Открой приложение — задания уже доступны.")
        except Exception:
            pass
    elif decision == "reject":
        await upsert_member(uid, status="blocked")
        try:
            await bot.send_message(uid, "К сожалению, заявка отклонена.")
        except Exception:
            pass
    else:
        return _json({"error": "bad_decision"}, status=400)
    return _json({"ok": True})


async def api_admin_task_create(request):
    admin_id, err = await _require_admin(request)
    if err:
        return err
    body = await _body(request)
    ttype = body.get("type")
    if ttype not in TASK_TYPES:
        return _json({"error": "type"}, status=400)
    title = (body.get("title") or TASK_TYPES[ttype]["title"]).strip()[:120]
    details = (body.get("details") or "").strip()[:500]
    address = (body.get("address") or "").strip()[:200]
    try:
        reward = max(0, int(body.get("reward") or 0))
    except (TypeError, ValueError):
        reward = 0
    lat = body.get("lat")
    lng = body.get("lng")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tasks (type, title, details, lat, lng, address, reward, "
            "status, created_by, created_at) VALUES (?,?,?,?,?,?,?, 'open', ?, ?)",
            (ttype, title, details, lat, lng, address, reward, admin_id, now_iso()))
        await db.commit()
        tid = cur.lastrowid
    return _json({"ok": True, "task_id": tid})


async def api_admin_task_approve(request):
    """Подтвердить выполнение → начислить бонусы исполнителю."""
    admin_id, err = await _require_admin(request)
    if err:
        return err
    body = await _body(request)
    tid = body.get("task_id")
    ok = body.get("approve", True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        t = await (await db.execute(
            "SELECT * FROM tasks WHERE id=?", (tid,))).fetchone()
        if not t:
            return _json({"error": "not_found"}, status=404)
        t = dict(t)
    if ok:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (tid,))
            await db.execute(
                "UPDATE members SET done_count = done_count + 1 WHERE user_id=?",
                (t["claimed_by"],))
            await db.commit()
        if t.get("claimed_by") and t.get("reward"):
            await add_bonus(t["claimed_by"], t["reward"],
                            f"Задание: {t.get('title','')}", task_id=tid, by=admin_id)
        try:
            await bot.send_message(
                t["claimed_by"],
                f"✅ Задание подтверждено! +{t.get('reward',0)} бибибонусов.")
        except Exception:
            pass
    else:
        # Вернуть в открытые — переделать.
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tasks SET status='open', claimed_by=NULL, claimed_at=NULL, "
                "done_at=NULL, proof_note=NULL WHERE id=?", (tid,))
            await db.commit()
        if t.get("claimed_by"):
            try:
                await bot.send_message(
                    t["claimed_by"], "Задание вернули на доработку — посмотри детали.")
            except Exception:
                pass
    return _json({"ok": True})


async def api_admin_grant(request):
    """Ручное начисление/списание бонусов (напр. отоварить поездку)."""
    admin_id, err = await _require_admin(request)
    if err:
        return err
    body = await _body(request)
    uid = body.get("user_id")
    try:
        amount = int(body.get("amount"))
    except (TypeError, ValueError):
        return _json({"error": "amount"}, status=400)
    reason = (body.get("reason") or "Ручная корректировка").strip()[:120]
    if not await get_member(uid):
        return _json({"error": "not_found"}, status=404)
    await add_bonus(uid, amount, reason, by=admin_id)
    return _json({"ok": True})


# ============================================================
# ВЕБ-СЕРВЕР
# ============================================================
async def serve_index(request):
    if os.path.exists(INDEX_PATH):
        return web.FileResponse(INDEX_PATH, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache", "Expires": "0",
        })
    return web.Response(text="bibitasks.html не найден", status=404)


async def api_health(request):
    return _json({
        "ok": True, "version": BUILD_VERSION,
        "index_html": os.path.exists(INDEX_PATH),
        "token_present": bool(BOT_TOKEN), "port": WEBAPP_PORT,
    })


async def _options(request):
    return _json({"ok": True})


async def start_api_server():
    try:
        app = web.Application()
        app.router.add_route("OPTIONS", "/{tail:.*}", _options)
        app.router.add_get("/api/state", api_state)
        app.router.add_post("/api/apply", api_apply)
        app.router.add_get("/api/tasks/available", api_tasks_available)
        app.router.add_post("/api/tasks/claim", api_task_claim)
        app.router.add_post("/api/tasks/complete", api_task_complete)
        app.router.add_get("/api/wallet", api_wallet)
        app.router.add_get("/api/admin/overview", api_admin_overview)
        app.router.add_post("/api/admin/decide", api_admin_decide)
        app.router.add_post("/api/admin/task/create", api_admin_task_create)
        app.router.add_post("/api/admin/task/approve", api_admin_task_approve)
        app.router.add_post("/api/admin/grant", api_admin_grant)
        app.router.add_get("/health", api_health)
        app.router.add_get("/bibitasks.html", serve_index)
        app.router.add_get("/", serve_index)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT)
        await site.start()
        logger.info(f"API БибиЗадачи слушает 0.0.0.0:{WEBAPP_PORT}")
    except Exception as e:
        logger.warning(f"API не запустился ({e}). Бот работает без него.")


# ============================================================
# БОТ: приветствие и текстовые сообщения
# ============================================================
dp = Dispatcher()


def _app_url():
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}/{WEBAPP_SHORTNAME}"
    return None


def _open_app_kb():
    url = _app_url()
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚲 Открыть задания", url=url)
    ]])


WELCOME = (
    "Привет! Это <b>БибиЗадачи</b> — здесь можно помогать Бибибайку и "
    "получать <b>бибибонусы</b> на бесплатные поездки. 🚲\n\n"
    "Как это работает:\n"
    "1️⃣ Открываешь приложение и подаёшь заявку\n"
    "2️⃣ Ответственный её одобряет\n"
    "3️⃣ Берёшь задания на карте — развоз байков, обслуживание зон, подзарядка\n"
    "4️⃣ Выполняешь и получаешь бибибонусы\n\n"
    "Чем больше и честнее помогаешь — тем выше уровень доверия и доступнее "
    "крупные задания. Жми кнопку ниже 👇"
)

ALREADY_APPROVED = (
    "С возвращением! Ты уже в команде помощников. "
    "Открой приложение и бери задания 👇"
)


@dp.message(CommandStart(deep_link=True))
async def start_ref(message: Message, command=None):
    """Старт с реферальной ссылкой: /start ref_<id>."""
    uid = message.from_user.id
    payload = ""
    try:
        payload = (command.args or "") if command else ""
    except Exception:
        payload = ""
    ref_id = None
    if payload.startswith("ref_") and payload[4:].isdigit():
        ref_id = int(payload[4:])
    m = await get_member(uid)
    if not m:
        await upsert_member(
            uid,
            full_name=(message.from_user.full_name or ""),
            username=(message.from_user.username or ""))
        if ref_id and ref_id != uid:
            await upsert_member(uid, referred_by=ref_id)
    await _greet(message)


@dp.message(CommandStart())
async def start_plain(message: Message):
    uid = message.from_user.id
    if not await get_member(uid):
        await upsert_member(
            uid,
            full_name=(message.from_user.full_name or ""),
            username=(message.from_user.username or ""))
    await _greet(message)


async def _greet(message: Message):
    m = await get_member(message.from_user.id)
    kb = _open_app_kb()
    if m and m["status"] == "approved":
        await message.answer(ALREADY_APPROVED, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(WELCOME, reply_markup=kb, parse_mode="HTML")


# Ключевые слова в личке — мягкая текстовая навигация.
@dp.message(F.chat.type == "private", F.text)
async def private_text(message: Message):
    text = (message.text or "").lower()
    uid = message.from_user.id
    if any(w in text for w in ("бонус", "баланс", "сколько")):
        m = await get_member(uid) or {}
        await message.answer(
            f"💰 Твои бибибонусы: <b>{m.get('bonus', 0)}</b>\n"
            f"Выполнено заданий: {m.get('done_count', 0)}",
            reply_markup=_open_app_kb(), parse_mode="HTML")
    elif any(w in text for w in ("задан", "работа", "помощь", "помогать")):
        await message.answer(
            "Задания и заявки — в приложении. Жми кнопку 👇",
            reply_markup=_open_app_kb())
    elif any(w in text for w in ("привет", "start", "начать", "старт")):
        await _greet(message)
    else:
        await message.answer(
            "Я помогаю с заданиями Бибибайка. Открой приложение — там заявки, "
            "задания и бибибонусы 👇", reply_markup=_open_app_kb())


# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    await init_db()
    await start_api_server()
    logger.info("=" * 50)
    logger.info("БибиЗадачи запущен!")
    logger.info(f"Версия сборки: {BUILD_VERSION}")
    me = await bot.get_me()
    logger.info(f"Бот @{me.username} id={me.id}")
    logger.info("=" * 50)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        print(f"ФАТАЛЬНАЯ ОШИБКА: {e}", flush=True)
        traceback.print_exc()
        raise
