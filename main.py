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
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
)

BUILD_VERSION = "2026-07-24 · БибиЗадачи v1.4.1 (сборка для BotHost)"

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
WITHDRAW_MIN = max(1, int(os.getenv("WITHDRAW_MIN", "1000") or "1000"))
WITHDRAW_CONTACT = os.getenv("WITHDRAW_CONTACT", "KiriLegenda").strip().lstrip("@")

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
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

# Ограничение перебора общего пароля ответственных. Состояние хранится только
# в памяти процесса: после пяти ошибок вход блокируется на 15 минут.
ADMIN_LOGIN_MAX_ATTEMPTS = 5
ADMIN_LOGIN_BLOCK_SEC = 15 * 60
_admin_login_attempts = {}

DEFAULT_REFERRAL_MILESTONES = [
    (5, 50),
    (25, 500),
    (50, 1500),
    (100, 4000),
]


def _load_referral_milestones():
    """Позволяет менять экономику без правки и публикации исходников."""
    raw = os.getenv("REFERRAL_MILESTONES_JSON", "").strip()
    if not raw:
        return DEFAULT_REFERRAL_MILESTONES
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            pairs = [(int(k), int(v)) for k, v in data.items()]
        else:
            pairs = []
            for item in data:
                if isinstance(item, dict):
                    pairs.append((int(item["count"]), int(item["reward"])))
                else:
                    pairs.append((int(item[0]), int(item[1])))
        pairs = sorted(set(pairs))
        if not pairs or any(count <= 0 or reward < 0 for count, reward in pairs):
            raise ValueError
        return pairs
    except Exception:
        logger.warning(
            "REFERRAL_MILESTONES_JSON некорректен — используются значения по умолчанию."
        )
        return DEFAULT_REFERRAL_MILESTONES


REFERRAL_MILESTONES = _load_referral_milestones()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# BotHost сохраняет папку /app/data между обновлениями. Поскольку код на
# платформе лежит в /app, этот путь получается автоматически. Локально база
# будет создана в папке data рядом с main.py.
DATA_DIR = os.getenv("DATA_DIR") or os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bibitasks.db")
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

print("=" * 60, flush=True)
print(f"== {BUILD_VERSION}", flush=True)
print(f"== рабочая папка: {BASE_DIR}", flush=True)
print(f"== база: {DB_PATH}", flush=True)
print(f"== index.html рядом: {os.path.exists(INDEX_PATH)}", flush=True)
print(f"== порт: {WEBAPP_PORT}", flush=True)
print(f"== токен найден: {'да' if BOT_TOKEN else 'НЕТ'}", flush=True)
print(f"== админов в ADMIN_IDS: {len(ADMIN_IDS)}", flush=True)
print(f"== пароль ответственных настроен: {'да' if ADMIN_PASSWORD else 'НЕТ'}", flush=True)
print(f"== реферальных ступеней: {len(REFERRAL_MILESTONES)}", flush=True)
print(f"== минимальный вывод: {WITHDRAW_MIN}", flush=True)
print(f"== контакт вывода настроен: {'да' if WITHDRAW_CONTACT else 'НЕТ'}", flush=True)
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
    "community": {"title": "Сообщество", "emoji": "📣",
                  "desc": "Подписка, отзыв или помощь сообществу"},
    "referral": {"title": "Приглашение", "emoji": "👥",
                 "desc": "Пригласить нового участника"},
    "photo_check": {"title": "Фото-проверка", "emoji": "📷",
                    "desc": "Проверить точку и отправить фото"},
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
                role        TEXT NOT NULL DEFAULT 'candidate',  -- candidate|applicant|helper|employee|admin
                status      TEXT NOT NULL DEFAULT 'pending',    -- pending|approved|blocked
                bonus       INTEGER NOT NULL DEFAULT 0,
                done_count  INTEGER NOT NULL DEFAULT 0,
                referred_by INTEGER,
                created_at  TEXT,
                approved_at TEXT,
                approved_by INTEGER,
                applied_at  TEXT
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
                proof_note  TEXT,
                assigned_to INTEGER,
                slot_start  TEXT,
                slot_end    TEXT,
                repeatable  INTEGER NOT NULL DEFAULT 0
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referral_rewards (
                referee_id  INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                amount      INTEGER NOT NULL,
                created_at  TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referral_milestone_rewards (
                user_id     INTEGER NOT NULL,
                threshold   INTEGER NOT NULL,
                amount      INTEGER NOT NULL,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (user_id, threshold)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_assignments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'claimed',
                claimed_at  TEXT NOT NULL,
                done_at     TEXT,
                proof_note  TEXT,
                UNIQUE(task_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                amount      INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT NOT NULL,
                decided_by  INTEGER,
                decided_at  TEXT,
                note        TEXT
            )
        """)
        member_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(members)")
            ).fetchall()
        }
        if "applied_at" not in member_columns:
            await db.execute("ALTER TABLE members ADD COLUMN applied_at TEXT")
        task_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(tasks)")
            ).fetchall()
        }
        for name, sql_type in (
            ("assigned_to", "INTEGER"),
            ("slot_start", "TEXT"),
            ("slot_end", "TEXT"),
            ("repeatable", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in task_columns:
                await db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {sql_type}")
        # Заявки из предыдущей версии, где был указан телефон, не должны снова
        # превращаться в пустую форму после миграции.
        await db.execute(
            "UPDATE members SET role='applicant', applied_at=COALESCE(created_at, ?) "
            "WHERE status='pending' AND role='candidate' "
            "AND phone IS NOT NULL AND TRIM(phone)<>''",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_assigned "
            "ON tasks(assigned_to, status)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_assignments_user "
            "ON task_assignments(user_id, status)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_assignments_review "
            "ON task_assignments(status, task_id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_withdrawals_user "
            "ON withdrawal_requests(user_id, created_at)")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_withdrawals_one_pending "
            "ON withdrawal_requests(user_id) WHERE status='pending'")
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


def parse_slot_iso(value):
    """Принимает ISO-время из Mini App и нормализует его в UTC."""
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("Некорректное время слота.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def slot_text(start, end):
    if not start or not end:
        return ""
    moscow = timezone(timedelta(hours=3))
    start_dt = datetime.fromisoformat(start).astimezone(moscow)
    end_dt = datetime.fromisoformat(end).astimezone(moscow)
    if start_dt.date() == end_dt.date():
        return f"{start_dt:%d.%m, %H:%M}–{end_dt:%H:%M} МСК"
    return f"{start_dt:%d.%m %H:%M}–{end_dt:%d.%m %H:%M} МСК"


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
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT bonus FROM members WHERE user_id = ?", (uid,)
        )).fetchone()
        if not row:
            await db.rollback()
            raise ValueError("Участник не найден.")
        new_balance = int(row[0]) + int(amount)
        if new_balance < 0:
            await db.rollback()
            raise ValueError("На балансе недостаточно бибибонусов.")
        await db.execute(
            "UPDATE members SET bonus = ? WHERE user_id = ?", (new_balance, uid))
        await db.execute(
            "INSERT INTO bonus_ledger (user_id, amount, reason, task_id, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, amount, reason, task_id, by, now_iso()))
        await db.commit()
    return new_balance


async def _grant_referral_milestones_in_tx(db, user_id, by=None):
    """Начисляет все достигнутые, но ещё не выданные ступени в транзакции."""
    row = await (await db.execute(
        "SELECT status FROM members WHERE user_id=?", (user_id,)
    )).fetchone()
    if not row or row[0] != "approved":
        return 0, 0
    count = int((await (await db.execute(
        "SELECT COUNT(*) FROM members WHERE referred_by=? AND status='approved'",
        (user_id,),
    )).fetchone())[0])
    total = 0
    for threshold, amount in REFERRAL_MILESTONES:
        if count < threshold:
            continue
        cur = await db.execute(
            "INSERT OR IGNORE INTO referral_milestone_rewards "
            "(user_id, threshold, amount, created_at) VALUES (?,?,?,?)",
            (user_id, threshold, amount, now_iso()),
        )
        if cur.rowcount != 1:
            continue
        total += amount
        await db.execute(
            "INSERT INTO bonus_ledger "
            "(user_id, amount, reason, task_id, created_by, created_at) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (
                user_id, amount,
                f"Реферальная ступень: {threshold} друзей", by, now_iso(),
            ),
        )
    if total:
        await db.execute(
            "UPDATE members SET bonus=bonus+? WHERE user_id=?",
            (total, user_id),
        )
    return count, total


async def sync_referral_milestones(user_id, by=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        count, total = await _grant_referral_milestones_in_tx(db, user_id, by)
        await db.commit()
    return count, total


async def get_referral_progress(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        count = int((await (await db.execute(
            "SELECT COUNT(*) FROM members "
            "WHERE referred_by=? AND status='approved'",
            (user_id,),
        )).fetchone())[0])
        awarded_rows = await (await db.execute(
            "SELECT threshold FROM referral_milestone_rewards WHERE user_id=?",
            (user_id,),
        )).fetchall()
    awarded = {int(row[0]) for row in awarded_rows}
    milestones = [
        {
            "count": threshold,
            "reward": amount,
            "reached": count >= threshold,
            "awarded": threshold in awarded,
        }
        for threshold, amount in REFERRAL_MILESTONES
    ]
    next_item = next((item for item in milestones if not item["reached"]), None)
    return {
        "count": count,
        "milestones": milestones,
        "next_count": next_item["count"] if next_item else None,
        "next_reward": next_item["reward"] if next_item else None,
    }


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
        "applied": bool(m.get("applied_at") or m.get("role") == "applicant"),
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
    if m["status"] == "approved":
        await sync_referral_milestones(uid)
        m = await get_member(uid)
    referral = await get_referral_progress(uid)
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
        "referral": referral,
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
    if m and (m.get("applied_at") or m.get("role") == "applicant"):
        return _json({"ok": True, "already": True})
    await upsert_member(
        uid, full_name=name, phone=phone,
        username=tg.get("username", ""),
        role="applicant", status="pending", applied_at=now_iso())
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
    uid, err = await _require_worker(request)
    if err is not None:
        return err
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        open_rows = await (await db.execute(
            "SELECT t.*, m.full_name AS assigned_name FROM tasks t "
            "LEFT JOIN members m ON m.user_id=t.assigned_to "
            "LEFT JOIN task_assignments a ON a.task_id=t.id AND a.user_id=? "
            "WHERE t.status='open' AND (t.assigned_to IS NULL OR t.assigned_to=?) "
            "AND (t.repeatable=0 OR a.id IS NULL) "
            "ORDER BY CASE WHEN t.assigned_to=? THEN 0 ELSE 1 END, "
            "t.slot_start IS NULL, t.slot_start, t.created_at DESC LIMIT 100",
            (uid, uid, uid),
        )).fetchall()
        mine_rows = await (await db.execute(
            "SELECT t.*, m.full_name AS assigned_name FROM tasks t "
            "LEFT JOIN members m ON m.user_id=t.assigned_to "
            "WHERE t.claimed_by=? AND t.status IN ('claimed','review') "
            "ORDER BY t.claimed_at DESC", (uid,)
        )).fetchall()
        repeat_rows = await (await db.execute(
            "SELECT t.*, NULL AS assigned_name, a.id AS assignment_id, "
            "a.status AS assignment_status, a.user_id AS assignment_user_id, "
            "a.claimed_at AS assignment_claimed_at, "
            "a.done_at AS assignment_done_at, "
            "a.proof_note AS assignment_proof_note "
            "FROM task_assignments a JOIN tasks t ON t.id=a.task_id "
            "WHERE a.user_id=? AND a.status IN ('claimed','review') "
            "ORDER BY a.claimed_at DESC",
            (uid,),
        )).fetchall()
    return _json({
        "ok": True,
        "available": [_task_public(dict(r)) for r in open_rows],
        "mine": [
            _task_public(dict(r)) for r in [*mine_rows, *repeat_rows]
        ],
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
        "status": t.get("assignment_status") or t.get("status"),
        "claimed_by": t.get("assignment_user_id") or t.get("claimed_by"),
        "assigned_to": t.get("assigned_to"),
        "assigned_name": t.get("assigned_name") or "",
        "is_personal": bool(t.get("assigned_to")),
        "slot_start": t.get("slot_start"),
        "slot_end": t.get("slot_end"),
        "repeatable": bool(t.get("repeatable")),
        "assignment_id": t.get("assignment_id"),
        "claimed_name": t.get("claimed_name") or "",
        "proof_note": (
            t.get("assignment_proof_note")
            if t.get("assignment_id") else t.get("proof_note")
        ) or "",
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
    if err is not None:
        return err
    body = await _body(request)
    tid = body.get("task_id")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT status, assigned_to, repeatable FROM tasks WHERE id=?",
            (tid,),
        )).fetchone()
        if not row:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if row[0] != "open":
            await db.rollback()
            return _json({"error": "taken", "message": "Задание уже взято."}, status=409)
        if row[1] is not None and int(row[1]) != int(uid):
            await db.rollback()
            return _json({
                "error": "not_assigned",
                "message": "Это задание назначено другому сотруднику.",
            }, status=403)
        if row[2]:
            if row[1] is not None:
                await db.rollback()
                return _json({"error": "bad_task"}, status=409)
            try:
                await db.execute(
                    "INSERT INTO task_assignments "
                    "(task_id, user_id, status, claimed_at) "
                    "VALUES (?, ?, 'claimed', ?)",
                    (tid, uid, now_iso()),
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                return _json({
                    "error": "already_joined",
                    "message": "Ты уже выполнял это задание.",
                }, status=409)
            await db.commit()
            return _json({"ok": True, "repeatable": True})
        cur = await db.execute(
            "UPDATE tasks SET status='claimed', claimed_by=?, claimed_at=? "
            "WHERE id=? AND status='open' "
            "AND (assigned_to IS NULL OR assigned_to=?)",
            (uid, now_iso(), tid, uid))
        if cur.rowcount != 1:
            await db.rollback()
            return _json({"error": "taken", "message": "Задание уже взято."}, status=409)
        await db.commit()
    return _json({"ok": True})


async def api_task_complete(request):
    uid, err = await _require_worker(request)
    if err is not None:
        return err
    body = await _body(request)
    tid = body.get("task_id")
    note = (body.get("note") or "").strip()[:300]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM tasks WHERE id=?", (tid,))).fetchone()
        if not row:
            return _json({"error": "not_found"}, status=404)
        if row["repeatable"]:
            cur = await db.execute(
                "UPDATE task_assignments SET status='review', done_at=?, proof_note=? "
                "WHERE task_id=? AND user_id=? AND status='claimed'",
                (now_iso(), note, tid, uid),
            )
            if cur.rowcount != 1:
                await db.rollback()
                return _json({"error": "not_yours"}, status=403)
            await db.commit()
        elif row["claimed_by"] != uid or row["status"] != "claimed":
            return _json({"error": "not_yours"}, status=403)
        else:
            cur = await db.execute(
                "UPDATE tasks SET status='review', done_at=?, proof_note=? "
                "WHERE id=? AND claimed_by=? AND status='claimed'",
                (now_iso(), note, tid, uid))
            if cur.rowcount != 1:
                await db.rollback()
                return _json({"error": "stale"}, status=409)
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
        withdrawals = await (await db.execute(
            "SELECT id, amount, status, created_at, decided_at, note "
            "FROM withdrawal_requests WHERE user_id=? "
            "ORDER BY id DESC LIMIT 20",
            (uid,),
        )).fetchall()
    return _json({
        "ok": True,
        "bonus": m.get("bonus", 0),
        "history": [dict(r) for r in rows],
        "withdraw_min": WITHDRAW_MIN,
        "withdraw_contact": WITHDRAW_CONTACT,
        "withdrawals": [dict(r) for r in withdrawals],
    })


async def api_withdraw_request(request):
    """Резервирует бонусы и создаёт одну активную заявку пользователя."""
    uid, err = await _require_worker(request)
    if err is not None:
        return err
    body = await _body(request)
    try:
        amount = int(body.get("amount"))
    except (TypeError, ValueError):
        return _json({"error": "amount", "message": "Укажи сумму вывода."}, status=400)
    if amount < WITHDRAW_MIN:
        return _json({
            "error": "minimum",
            "message": f"Минимальная сумма вывода — {WITHDRAW_MIN} бонусов.",
        }, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        member = await (await db.execute(
            "SELECT full_name, username, bonus FROM members "
            "WHERE user_id=? AND status='approved'",
            (uid,),
        )).fetchone()
        if not member:
            await db.rollback()
            return _json({"error": "not_approved"}, status=403)
        pending = await (await db.execute(
            "SELECT id FROM withdrawal_requests "
            "WHERE user_id=? AND status='pending'",
            (uid,),
        )).fetchone()
        if pending:
            await db.rollback()
            return _json({
                "error": "pending",
                "message": "У тебя уже есть заявка на рассмотрении.",
            }, status=409)
        balance = int(member["bonus"])
        if amount > balance:
            await db.rollback()
            return _json({
                "error": "balance",
                "message": "На балансе недостаточно бонусов.",
            }, status=409)
        cur = await db.execute(
            "INSERT INTO withdrawal_requests "
            "(user_id, amount, status, created_at) "
            "VALUES (?, ?, 'pending', ?)",
            (uid, amount, now_iso()),
        )
        request_id = cur.lastrowid
        new_balance = balance - amount
        await db.execute(
            "UPDATE members SET bonus=? WHERE user_id=?",
            (new_balance, uid),
        )
        await db.execute(
            "INSERT INTO bonus_ledger "
            "(user_id, amount, reason, task_id, created_by, created_at) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (
                uid, -amount, f"Резерв на вывод #{request_id}", uid, now_iso(),
            ),
        )
        await db.commit()
    for admin_id in await _all_admin_ids():
        try:
            await bot.send_message(
                admin_id,
                f"💸 Новая заявка на вывод #{request_id}\n"
                f"Участник: {member['full_name'] or '—'}\n"
                f"Сумма: {amount} бибибонусов\n"
                f"Открой приложение → Модерация → Выводы.",
            )
        except Exception:
            pass
    return _json({
        "ok": True,
        "request_id": request_id,
        "amount": amount,
        "balance": new_balance,
        "contact": WITHDRAW_CONTACT,
    })


# ── Админ: модерация заявок и заданий ─────────────────────────
def _admin_login_wait(uid):
    state = _admin_login_attempts.get(uid)
    if not state:
        return 0
    blocked_until = state.get("blocked_until", 0)
    wait = int(blocked_until - time.monotonic())
    if wait <= 0 and blocked_until:
        _admin_login_attempts.pop(uid, None)
        return 0
    return max(0, wait)


def _admin_login_failed(uid):
    state = _admin_login_attempts.setdefault(
        uid, {"attempts": 0, "blocked_until": 0}
    )
    state["attempts"] += 1
    if state["attempts"] >= ADMIN_LOGIN_MAX_ATTEMPTS:
        state["attempts"] = 0
        state["blocked_until"] = time.monotonic() + ADMIN_LOGIN_BLOCK_SEC


async def api_admin_login(request):
    """Первичный вход ответственного по серверному паролю."""
    tg = await _auth_user(request)
    if not tg:
        return _json({"error": "auth"}, status=401)
    if await is_admin(tg["id"]):
        return _json({"ok": True, "already": True})
    if not ADMIN_PASSWORD:
        return _json({
            "error": "not_configured",
            "message": "Пароль ответственных не настроен на хостинге.",
        }, status=503)
    wait = _admin_login_wait(tg["id"])
    if wait:
        return _json({
            "error": "rate_limit",
            "message": f"Слишком много попыток. Повтори через {max(1, wait // 60 + 1)} мин.",
        }, status=429)
    body = await _body(request)
    password = str(body.get("password") or "")
    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        _admin_login_failed(tg["id"])
        return _json({
            "error": "bad_password",
            "message": "Неверный пароль ответственного.",
        }, status=403)
    _admin_login_attempts.pop(tg["id"], None)
    name = (
        (tg.get("first_name", "") + " " + tg.get("last_name", "")).strip()
        or "Ответственный"
    )
    current = await get_member(tg["id"])
    await upsert_member(
        tg["id"],
        full_name=(current.get("full_name") if current and current.get("full_name") else name),
        username=tg.get("username", ""),
        role="admin",
        status="approved",
        approved_at=now_iso(),
        approved_by=tg["id"],
    )
    return _json({"ok": True})


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
    if err is not None:
        return err
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        pending = await (await db.execute(
            "SELECT user_id, full_name, phone, username, created_at FROM members "
            "WHERE status='pending' AND (applied_at IS NOT NULL OR role='applicant') "
            "ORDER BY applied_at DESC, created_at DESC")).fetchall()
        review = await (await db.execute(
            "SELECT t.*, m.full_name AS assigned_name FROM tasks t "
            "LEFT JOIN members m ON m.user_id=t.assigned_to "
            "WHERE t.status='review' ORDER BY t.done_at DESC")).fetchall()
        repeat_review = await (await db.execute(
            "SELECT t.*, NULL AS assigned_name, "
            "a.id AS assignment_id, a.status AS assignment_status, "
            "a.user_id AS assignment_user_id, a.proof_note AS assignment_proof_note, "
            "u.full_name AS claimed_name "
            "FROM task_assignments a "
            "JOIN tasks t ON t.id=a.task_id "
            "LEFT JOIN members u ON u.user_id=a.user_id "
            "WHERE a.status='review' ORDER BY a.done_at DESC"
        )).fetchall()
        open_tasks = await (await db.execute(
            "SELECT t.*, m.full_name AS assigned_name FROM tasks t "
            "LEFT JOIN members m ON m.user_id=t.assigned_to "
            "WHERE t.status IN ('open','claimed') "
            "ORDER BY t.created_at DESC LIMIT 100")).fetchall()
        team = await (await db.execute(
            "SELECT user_id, full_name, role, bonus, done_count FROM members "
            "WHERE status='approved' ORDER BY done_count DESC LIMIT 100")).fetchall()
        withdrawals = await (await db.execute(
            "SELECT w.id, w.user_id, w.amount, w.status, w.created_at, "
            "w.decided_at, w.note, m.full_name, m.username "
            "FROM withdrawal_requests w "
            "LEFT JOIN members m ON m.user_id=w.user_id "
            "ORDER BY CASE WHEN w.status='pending' THEN 0 ELSE 1 END, "
            "w.id DESC LIMIT 100"
        )).fetchall()
    return _json({
        "ok": True,
        "pending": [dict(r) for r in pending],
        "review": [
            _task_public(dict(r)) for r in [*review, *repeat_review]
        ],
        "open_tasks": [_task_public(dict(r)) for r in open_tasks],
        "team": [{
            "user_id": r["user_id"], "name": r["full_name"], "role": r["role"],
            "bonus": r["bonus"], "done_count": r["done_count"],
            "trust_name": trust_for(r["done_count"])[1],
            "trust_emoji": trust_for(r["done_count"])[2],
        } for r in team],
        "withdrawals": [dict(r) for r in withdrawals],
    })


async def api_admin_decide(request):
    """Одобрить или отклонить заявку кандидата."""
    admin_id, err = await _require_admin(request)
    if err is not None:
        return err
    body = await _body(request)
    uid = body.get("user_id")
    decision = body.get("decision")   # approve | reject
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return _json({"error": "bad_user"}, status=400)
    referral_count = 0
    rewarded_total = 0
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT * FROM members WHERE user_id=?", (uid,)
        )).fetchone()
        if not row:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        m = dict(row)
        if m["status"] != "pending":
            await db.rollback()
            return _json({
                "error": "already_decided",
                "message": "Эта заявка уже обработана.",
            }, status=409)
        if decision == "approve":
            cur = await db.execute(
                "UPDATE members SET status='approved', role='helper', "
                "approved_at=?, approved_by=? "
                "WHERE user_id=? AND status='pending'",
                (now_iso(), admin_id, uid),
            )
        elif decision == "reject":
            cur = await db.execute(
                "UPDATE members SET status='blocked' "
                "WHERE user_id=? AND status='pending'",
                (uid,),
            )
        else:
            await db.rollback()
            return _json({"error": "bad_decision"}, status=400)
        if cur.rowcount != 1:
            await db.rollback()
            return _json({"error": "already_decided"}, status=409)
        if decision == "approve" and m.get("referred_by") and m["referred_by"] != uid:
            referral_count, rewarded_total = await _grant_referral_milestones_in_tx(
                db, m["referred_by"], by=admin_id
            )
        await db.commit()
    if decision == "approve":
        if (
            m.get("referred_by")
            and m["referred_by"] != uid
            and referral_count > 0
        ):
            try:
                if rewarded_total:
                    ref_message = (
                        f"🎉 Реферальная ступень достигнута!\n"
                        f"Одобрено друзей: {referral_count}\n"
                        f"Начислено: +{rewarded_total} бибибонусов."
                    )
                else:
                    next_threshold = next(
                        (count for count, _ in REFERRAL_MILESTONES
                         if count > referral_count),
                        None,
                    )
                    ref_message = (
                        f"👥 Новый друг одобрен: {referral_count}"
                        + (
                            f" из {next_threshold} до следующей награды."
                            if next_threshold else ". Все ступени пройдены!"
                        )
                    )
                await bot.send_message(m["referred_by"], ref_message)
            except Exception:
                pass
        try:
            await bot.send_message(
                uid, "🎉 Заявка одобрена! Открой приложение — задания уже доступны.")
        except Exception:
            pass
    elif decision == "reject":
        try:
            await bot.send_message(uid, "К сожалению, заявка отклонена.")
        except Exception:
            pass
    return _json({"ok": True})


async def api_admin_task_create(request):
    admin_id, err = await _require_admin(request)
    if err is not None:
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
    assigned_to = body.get("assigned_to")
    if assigned_to in ("", None):
        assigned_to = None
    else:
        try:
            assigned_to = int(assigned_to)
        except (TypeError, ValueError):
            return _json({"error": "assignee"}, status=400)
        assignee = await get_member(assigned_to)
        if not assignee or assignee["status"] != "approved" or assignee["role"] not in (
            "helper", "employee"
        ):
            return _json({
                "error": "assignee",
                "message": "Можно назначить только одобренного участника.",
            }, status=400)
    repeatable = bool(body.get("repeatable"))
    if repeatable and assigned_to is not None:
        return _json({
            "error": "mode",
            "message": "Многоразовое задание должно быть доступно всем.",
        }, status=400)
    try:
        slot_start = parse_slot_iso(body.get("slot_start"))
        slot_end = parse_slot_iso(body.get("slot_end"))
    except ValueError as e:
        return _json({"error": "slot", "message": str(e)}, status=400)
    if bool(slot_start) != bool(slot_end):
        return _json({
            "error": "slot",
            "message": "Укажи и начало, и окончание слота.",
        }, status=400)
    if slot_start and datetime.fromisoformat(slot_end) <= datetime.fromisoformat(slot_start):
        return _json({
            "error": "slot",
            "message": "Окончание слота должно быть позже начала.",
        }, status=400)
    lat = body.get("lat")
    lng = body.get("lng")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tasks (type, title, details, lat, lng, address, reward, "
            "status, created_by, created_at, assigned_to, slot_start, slot_end, repeatable) "
            "VALUES (?,?,?,?,?,?,?, 'open', ?, ?, ?, ?, ?, ?)",
            (
                ttype, title, details, lat, lng, address, reward, admin_id,
                now_iso(), assigned_to, slot_start, slot_end, int(repeatable),
            ))
        await db.commit()
        tid = cur.lastrowid
    if assigned_to:
        parts = [
            "📌 Тебе назначено новое задание",
            title,
        ]
        if details:
            parts.append(details)
        if address:
            parts.append(f"📍 {address}")
        if slot_start:
            parts.append(f"🕒 {slot_text(slot_start, slot_end)}")
        parts.append(f"Награда: {reward} бибибонусов")
        try:
            await bot.send_message(
                assigned_to,
                "\n".join(parts),
                reply_markup=_open_app_kb(),
            )
        except Exception:
            logger.warning(
                "Не удалось отправить назначение пользователю %s", assigned_to
            )
    return _json({
        "ok": True,
        "task_id": tid,
        "personal": bool(assigned_to),
        "repeatable": repeatable,
    })


async def api_admin_task_approve(request):
    """Подтвердить выполнение → начислить бонусы исполнителю."""
    admin_id, err = await _require_admin(request)
    if err is not None:
        return err
    body = await _body(request)
    tid = body.get("task_id")
    assignment_id = body.get("assignment_id")
    ok = body.get("approve", True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT * FROM tasks WHERE id=?", (tid,))).fetchone()
        if not row:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        t = dict(row)
        if assignment_id is not None:
            try:
                assignment_id = int(assignment_id)
            except (TypeError, ValueError):
                await db.rollback()
                return _json({"error": "assignment"}, status=400)
            assignment = await (await db.execute(
                "SELECT * FROM task_assignments WHERE id=? AND task_id=?",
                (assignment_id, tid),
            )).fetchone()
            if not assignment or assignment["status"] != "review" or not t["repeatable"]:
                await db.rollback()
                return _json({
                    "error": "already_decided",
                    "message": "Это выполнение уже обработано.",
                }, status=409)
            claimed_by = assignment["user_id"]
            if ok:
                cur = await db.execute(
                    "UPDATE task_assignments SET status='done' "
                    "WHERE id=? AND status='review'",
                    (assignment_id,),
                )
            else:
                cur = await db.execute(
                    "UPDATE task_assignments SET status='claimed', "
                    "done_at=NULL, proof_note=NULL "
                    "WHERE id=? AND status='review'",
                    (assignment_id,),
                )
        else:
            if t["status"] != "review" or not t.get("claimed_by") or t["repeatable"]:
                await db.rollback()
                return _json({
                    "error": "already_decided",
                    "message": "Это задание уже обработано.",
                }, status=409)
            claimed_by = t["claimed_by"]
            if ok:
                cur = await db.execute(
                    "UPDATE tasks SET status='done' WHERE id=? AND status='review'",
                    (tid,),
                )
            else:
                cur = await db.execute(
                    "UPDATE tasks SET status='open', claimed_by=NULL, claimed_at=NULL, "
                    "done_at=NULL, proof_note=NULL WHERE id=? AND status='review'",
                    (tid,),
                )
        if cur.rowcount != 1:
            await db.rollback()
            return _json({"error": "already_decided"}, status=409)
        if ok:
            await db.execute(
                "UPDATE members SET done_count=done_count+1, bonus=bonus+? "
                "WHERE user_id=?",
                (int(t.get("reward") or 0), claimed_by),
            )
            if t.get("reward"):
                await db.execute(
                    "INSERT INTO bonus_ledger "
                    "(user_id, amount, reason, task_id, created_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        claimed_by, int(t["reward"]),
                        f"Задание: {t.get('title','')}", tid, admin_id, now_iso(),
                    ),
                )
        await db.commit()
    if ok:
        try:
            await bot.send_message(
                claimed_by,
                f"✅ Задание подтверждено! +{t.get('reward',0)} бибибонусов.")
        except Exception:
            pass
    else:
        try:
            await bot.send_message(
                claimed_by, "Задание вернули на доработку — посмотри детали.")
        except Exception:
            pass
    return _json({"ok": True})


async def api_admin_grant(request):
    """Ручное начисление/списание бонусов (напр. отоварить поездку)."""
    admin_id, err = await _require_admin(request)
    if err is not None:
        return err
    body = await _body(request)
    uid = body.get("user_id")
    try:
        amount = int(body.get("amount"))
    except (TypeError, ValueError):
        return _json({"error": "amount"}, status=400)
    if amount == 0 or abs(amount) > 1_000_000:
        return _json({
            "error": "amount",
            "message": "Укажи сумму от 1 до 1 000 000 бонусов.",
        }, status=400)
    reason = (body.get("reason") or "Ручная корректировка").strip()[:120]
    member = await get_member(uid)
    if not member or member["status"] != "approved":
        return _json({"error": "not_found"}, status=404)
    try:
        balance = await add_bonus(uid, amount, reason, by=admin_id)
    except ValueError as e:
        return _json({"error": "balance", "message": str(e)}, status=409)
    try:
        sign = "+" if amount > 0 else ""
        await bot.send_message(
            int(uid),
            f"💰 Баланс изменён: {sign}{amount} бибибонусов.\n"
            f"Причина: {reason}\n"
            f"Новый баланс: {balance}.",
        )
    except Exception:
        pass
    return _json({"ok": True, "balance": balance})


async def api_admin_withdraw_decide(request):
    """Подтверждает вывод либо возвращает зарезервированные бонусы."""
    admin_id, err = await _require_admin(request)
    if err is not None:
        return err
    body = await _body(request)
    try:
        request_id = int(body.get("request_id"))
    except (TypeError, ValueError):
        return _json({"error": "request"}, status=400)
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        return _json({"error": "decision"}, status=400)
    note = (body.get("note") or "").strip()[:200]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        item = await (await db.execute(
            "SELECT * FROM withdrawal_requests WHERE id=?",
            (request_id,),
        )).fetchone()
        if not item:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if item["status"] != "pending":
            await db.rollback()
            return _json({
                "error": "already_decided",
                "message": "Эта заявка уже обработана.",
            }, status=409)
        final_status = "approved" if decision == "approve" else "rejected"
        cur = await db.execute(
            "UPDATE withdrawal_requests SET status=?, decided_by=?, "
            "decided_at=?, note=? WHERE id=? AND status='pending'",
            (final_status, admin_id, now_iso(), note, request_id),
        )
        if cur.rowcount != 1:
            await db.rollback()
            return _json({"error": "already_decided"}, status=409)
        if decision == "reject":
            await db.execute(
                "UPDATE members SET bonus=bonus+? WHERE user_id=?",
                (item["amount"], item["user_id"]),
            )
            await db.execute(
                "INSERT INTO bonus_ledger "
                "(user_id, amount, reason, task_id, created_by, created_at) "
                "VALUES (?, ?, ?, NULL, ?, ?)",
                (
                    item["user_id"], item["amount"],
                    f"Возврат по заявке на вывод #{request_id}",
                    admin_id, now_iso(),
                ),
            )
        balance_row = await (await db.execute(
            "SELECT bonus FROM members WHERE user_id=?",
            (item["user_id"],),
        )).fetchone()
        await db.commit()
    try:
        if decision == "approve":
            message = (
                f"✅ Заявка на вывод #{request_id} одобрена.\n"
                f"Сумма: {item['amount']} бибибонусов."
            )
        else:
            message = (
                f"↩️ Заявка на вывод #{request_id} отклонена.\n"
                f"{item['amount']} бонусов возвращены на баланс."
            )
        if note:
            message += f"\nКомментарий: {note}"
        await bot.send_message(item["user_id"], message)
    except Exception:
        pass
    return _json({
        "ok": True,
        "status": final_status,
        "balance": int(balance_row[0]) if balance_row else 0,
    })


# ============================================================
# ВЕБ-СЕРВЕР
# ============================================================
async def serve_index(request):
    if os.path.exists(INDEX_PATH):
        return web.FileResponse(INDEX_PATH, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache", "Expires": "0",
        })
    return web.Response(text="index.html не найден", status=404)


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
        app.router.add_post("/api/withdraw/request", api_withdraw_request)
        app.router.add_post("/api/admin/login", api_admin_login)
        app.router.add_get("/api/admin/overview", api_admin_overview)
        app.router.add_post("/api/admin/decide", api_admin_decide)
        app.router.add_post("/api/admin/task/create", api_admin_task_create)
        app.router.add_post("/api/admin/task/approve", api_admin_task_approve)
        app.router.add_post("/api/admin/grant", api_admin_grant)
        app.router.add_post("/api/admin/withdraw/decide", api_admin_withdraw_decide)
        app.router.add_get("/health", api_health)
        app.router.add_get("/index.html", serve_index)
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
