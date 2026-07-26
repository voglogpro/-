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
    CallbackQuery,
)

BUILD_VERSION = "2026-07-26 · БибиЗадачи v2.1.0 (пост в подтему — один раз)"

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
def _as_int_env(name, default=None):
    try:
        return int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default


def _clean_username(raw):
    """Принимает 'bbbikefan', '@bbbikefan' или 'https://t.me/bbbikefan'."""
    value = (raw or "").strip()
    if not value:
        return ""
    value = value.split("?")[0].rstrip("/")
    if "t.me/" in value:
        value = value.split("t.me/")[-1]
    return value.lstrip("@").split("/")[0]


# Ник бота для реферальных ссылок: https://t.me/<BOT_USERNAME>?start=ref_<id>
# ВНИМАНИЕ: bbbikefan — это ГРУППА, а не бот. Диплинк ?start=ref_<id>
# понимают только боты, поэтому реферальные ссылки идут на BbGalterbot.
BOT_USERNAME = _clean_username(os.getenv("BOT_USERNAME", "BbGalterbot"))

# Канал или группа, подписка на которую засчитывает реферала.
# Можно указать @username или числовой id вида -1001234567890.
# ВАЖНО: бот должен быть администратором этого чата, иначе Telegram
# не даст проверить подписку и вернёт ошибку.
REQUIRED_CHAT = (os.getenv("REQUIRED_CHAT", "@bbbikefan") or "").strip()

# ── Группа и её подтемы ───────────────────────────────────────
# Ссылка вида https://t.me/bbbikefan/3 — это id подтемы (message_thread_id).
GROUP_USERNAME = _clean_username(os.getenv("GROUP_USERNAME", "bbbikefan"))
GROUP_ID = _as_int_env("GROUP_ID")          # опционально, если группа закрытая
TOPIC_NEWS = _as_int_env("TOPIC_NEWS", 1)          # новости
TOPIC_CHAT = _as_int_env("TOPIC_CHAT", 3)          # беседа: за неё капает опыт
TOPIC_WORK = _as_int_env("TOPIC_WORK", 4)          # работа: сюда инструкция
TOPIC_FRANCHISE = _as_int_env("TOPIC_FRANCHISE", 43)  # франшиза: только одобренные

# ── Опыт за общение ───────────────────────────────────────────
# Сколько опыта равно одному выполненному заданию при расчёте уровня.
CHAT_XP_PER_TASK = _as_int_env("CHAT_XP_PER_TASK", 50)
XP_PER_MESSAGE = _as_int_env("XP_PER_MESSAGE", 1)    # обычное сообщение — мало
XP_PER_THANKS = _as_int_env("XP_PER_THANKS", 15)     # спасибо тебе — много
MSG_MIN_CHARS = _as_int_env("MSG_MIN_CHARS", 4)      # короче — не считаем
MSG_COOLDOWN_SEC = _as_int_env("MSG_COOLDOWN_SEC", 60)
MSG_XP_DAILY_CAP = _as_int_env("MSG_XP_DAILY_CAP", 20)
THANKS_XP_DAILY_CAP = _as_int_env("THANKS_XP_DAILY_CAP", 45)
THANKS_PAIR_COOLDOWN_H = _as_int_env("THANKS_PAIR_COOLDOWN_H", 12)


def _required_chat_id():
    """Telegram принимает и '@name', и число — приводим к нужному типу."""
    if not REQUIRED_CHAT:
        return None
    value = REQUIRED_CHAT
    if "t.me/" in value:
        value = "@" + _clean_username(value)
    if value.lstrip("-").isdigit():
        return int(value)
    return value if value.startswith("@") else "@" + value


def _required_chat_url():
    """Ссылка, которую показываем человеку в кнопке «Подписаться»."""
    explicit = (os.getenv("REQUIRED_CHAT_URL", "") or "").strip()
    if explicit:
        return explicit
    chat = _required_chat_id()
    if isinstance(chat, str) and chat.startswith("@"):
        return f"https://t.me/{chat[1:]}"
    return None
# Короткое имя Mini App: https://t.me/BbGalterbot/bibibike
WEBAPP_SHORTNAME = os.getenv("WEBAPP_SHORTNAME", "bibibike")
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

# Владелец проекта получает роль ответственного сразу, без пароля.
# Telegram ID не секрет, но помни: он остаётся в открытом репозитории.
# Чтобы добавить кого-то ещё — вписывай в переменную ADMIN_IDS на хостинге,
# а не сюда.
OWNER_IDS = {7785586524}
ADMIN_IDS = _parse_ids(os.getenv("ADMIN_IDS", "")) | OWNER_IDS
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

# Роли, которые ответственный может выдавать вручную.
ROLE_TITLES = {
    "helper": "Помощник",
    "employee": "Сотрудник",
    "admin": "Ответственный",
}

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


def trust_score(done_count, chat_xp=0):
    """Единая шкала уровня: задания плюс общение в беседе.

    CHAT_XP_PER_TASK опыта приравниваются к одному заданию. При chat_xp=0
    результат совпадает со старым поведением, поэтому у всех, кто был до
    обновления, уровень не съедет.
    """
    per = max(1, CHAT_XP_PER_TASK)
    return int(done_count or 0) + int(chat_xp or 0) // per


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


# ── Награды ───────────────────────────────────────────────────
# Стартовый каталог. Ответственный может править его прямо в приложении:
# менять эмодзи, название, размер бонуса и выключать ненужное. Награда с
# repeatable=1 выдаётся сколько угодно раз, с repeatable=0 — один раз.
# (code, emoji, title, description, bonus, repeatable)
DEFAULT_AWARDS = [
    ("volt_day", "⚡", "Молния",
     "Пять и больше заданий за один день", 150, 1),
    ("mechanic", "🛠", "Мастер",
     "Починил байк на месте, без сервисного центра", 80, 1),
    ("night", "🌙", "Ночная смена",
     "Работал после полуночи", 120, 1),
    ("rescue_hero", "🏅", "Спасатель",
     "Вытащил байк из совсем плохого места", 200, 1),
    ("sharp_eye", "📸", "Глаз-алмаз",
     "Первым заметил и передал проблему", 50, 1),
    ("mentor", "🤝", "Наставник",
     "Ввёл новичка в работу", 100, 1),
    ("legend", "👑", "Легенда месяца",
     "Лучший результат месяца по команде", 500, 1),
    ("first_task", "🌱", "Первое задание",
     "Закрыл своё самое первое задание", 30, 0),
]


# ============================================================
# БАЗА ДАННЫХ
# ============================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        # WAL позволяет читать во время записи. Без него параллельные
        # «Взять задание» упираются в блокировку и отдают 500.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_activity (
                user_id         INTEGER PRIMARY KEY,
                last_msg_at     TEXT,
                day             TEXT,
                msg_xp_today    INTEGER NOT NULL DEFAULT 0,
                thanks_xp_today INTEGER NOT NULL DEFAULT 0,
                messages_total  INTEGER NOT NULL DEFAULT 0,
                thanks_total    INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS published_posts (
                kind        TEXT PRIMARY KEY,
                chat_id     INTEGER NOT NULL,
                topic       INTEGER,
                message_ids TEXT NOT NULL,
                published_at TEXT NOT NULL,
                published_by INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS thanks_pairs (
                from_id INTEGER NOT NULL,
                to_id   INTEGER NOT NULL,
                last_at TEXT NOT NULL,
                PRIMARY KEY (from_id, to_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS awards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                code        TEXT UNIQUE,
                emoji       TEXT NOT NULL DEFAULT '🏅',
                title       TEXT NOT NULL,
                description TEXT,
                bonus       INTEGER NOT NULL DEFAULT 0,
                repeatable  INTEGER NOT NULL DEFAULT 1,
                active      INTEGER NOT NULL DEFAULT 1,
                created_by  INTEGER,
                created_at  TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS member_awards (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                award_id   INTEGER NOT NULL,
                slot       TEXT NOT NULL DEFAULT '',
                bonus      INTEGER NOT NULL DEFAULT 0,
                note       TEXT,
                granted_by INTEGER,
                granted_at TEXT NOT NULL,
                UNIQUE(user_id, award_id, slot)
            )
        """)
        member_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(members)")
            ).fetchall()
        }
        if "chat_xp" not in member_columns:
            await db.execute(
                "ALTER TABLE members ADD COLUMN chat_xp INTEGER NOT NULL DEFAULT 0")
            member_columns.add("chat_xp")
        if "ref_confirmed" not in member_columns:
            await db.execute(
                "ALTER TABLE members ADD COLUMN ref_confirmed INTEGER NOT NULL DEFAULT 0")
            # Уже одобренные приглашённые засчитываются задним числом,
            # иначе после обновления у всех обнулился бы прогресс-бар.
            await db.execute(
                "UPDATE members SET ref_confirmed=1 "
                "WHERE referred_by IS NOT NULL AND status='approved'")
            member_columns.add("ref_confirmed")
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
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_member_awards_user "
            "ON member_awards(user_id, granted_at)")
        await db.commit()

        # Стартовые награды добавляются один раз по code. Если ответственный
        # их переименовал или отключил — повторный запуск ничего не перезапишет.
        for code, emoji, title, desc, bonus, repeatable in DEFAULT_AWARDS:
            await db.execute(
                "INSERT OR IGNORE INTO awards "
                "(code, emoji, title, description, bonus, repeatable, active, created_at) "
                "VALUES (?,?,?,?,?,?,1,?)",
                (code, emoji, title, desc, bonus, repeatable, now_iso()),
            )
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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
        "SELECT COUNT(*) FROM members WHERE referred_by=? AND ref_confirmed=1",
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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute("BEGIN IMMEDIATE")
        count, total = await _grant_referral_milestones_in_tx(db, user_id, by)
        await db.commit()
    return count, total


async def get_referral_progress(user_id):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        count = int((await (await db.execute(
            "SELECT COUNT(*) FROM members "
            "WHERE referred_by=? AND ref_confirmed=1",
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


def _as_int(value, default=None):
    """Мягкое приведение к int: фронт может прислать строку или null."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default


async def _send(uid, text, kb=None):
    try:
        await bot.send_message(int(uid), text, reply_markup=kb)
    except Exception:
        logger.info("Уведомление не доставлено: %s", uid)


def _notify(uid, text, kb=None):
    """Отправляет сообщение в фоне, чтобы не задерживать ответ API."""
    asyncio.create_task(_send(uid, text, kb))


def _notify_admins(text):
    async def run():
        for admin_id in await _all_admin_ids():
            await _send(admin_id, text)
    asyncio.create_task(run())


def _member_public(m):
    """Что отдаём во фронт о самом пользователе."""
    done = m.get("done_count", 0)
    chat_xp = m.get("chat_xp", 0) or 0
    score = trust_score(done, chat_xp)
    key, name, emoji, _ = trust_for(score)
    nxt = next_trust(score)
    return {
        "user_id": m["user_id"],
        "name": m.get("full_name") or "",
        "role": m.get("role"),
        "status": m.get("status"),
        "bonus": m.get("bonus", 0),
        "done_count": done,
        "chat_xp": chat_xp,
        "trust_score": score,
        "chat_xp_per_task": max(1, CHAT_XP_PER_TASK),
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
        "my_awards": await _my_awards(uid),
        "withdraw_min": WITHDRAW_MIN,
        "roles": [{"key": k, "title": v} for k, v in ROLE_TITLES.items()],
        "referral_gate": {
            "required": bool(_required_chat_id()),
            "url": _required_chat_url(),
            "invited": bool(m["referred_by"]) and m["referred_by"] != uid,
            "confirmed": bool(m["ref_confirmed"]),
        },
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
    _notify_admins(
        f"🆕 Новая заявка на помощь\n"
        f"Имя: {name}\n"
        f"Телефон: {phone or '—'}\n"
        f"Ник: @{tg.get('username','') or '—'}\n"
        f"ID: {uid}\n\n"
        f"Открой приложение → Модерация, чтобы одобрить."
    )
    return _json({"ok": True})


async def _all_admin_ids():
    ids = set(ADMIN_IDS)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
    tid = _as_int(body.get("task_id"))
    if tid is None:
        return _json({"error": "task"}, status=400)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
    tid = _as_int(body.get("task_id"))
    if tid is None:
        return _json({"error": "task"}, status=400)
    note = (body.get("note") or "").strip()[:300]
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
    _notify_admins(
        f"✅ Задание #{tid} отправлено на проверку.\n"
        f"Комментарий: {note or '—'}\n"
        f"Открой Модерацию, чтобы подтвердить и начислить бонусы."
    )
    return _json({"ok": True})


async def api_wallet(request):
    tg = await _auth_user(request)
    if not tg:
        return _json({"error": "auth"}, status=401)
    uid = tg["id"]
    m = await get_member(uid) or {}
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
    _notify_admins(
        f"💸 Новая заявка на вывод #{request_id}\n"
        f"Участник: {member['full_name'] or '—'}\n"
        f"Сумма: {amount} бибибонусов\n"
        f"Открой приложение → Модерация → Выводы."
    )
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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
            "SELECT user_id, full_name, role, bonus, done_count, chat_xp FROM members "
            "WHERE status='approved' ORDER BY done_count DESC, chat_xp DESC "
            "LIMIT 100")).fetchall()
        withdrawals = await (await db.execute(
            "SELECT w.id, w.user_id, w.amount, w.status, w.created_at, "
            "w.decided_at, w.note, m.full_name, m.username "
            "FROM withdrawal_requests w "
            "LEFT JOIN members m ON m.user_id=w.user_id "
            "ORDER BY CASE WHEN w.status='pending' THEN 0 ELSE 1 END, "
            "w.id DESC LIMIT 100"
        )).fetchall()
        awards = await (await db.execute(
            "SELECT * FROM awards ORDER BY active DESC, bonus DESC, id"
        )).fetchall()
        granted = await (await db.execute(
            "SELECT ma.id, ma.user_id, ma.bonus, ma.note, ma.granted_at, "
            "a.emoji, a.title, m.full_name "
            "FROM member_awards ma "
            "JOIN awards a ON a.id=ma.award_id "
            "LEFT JOIN members m ON m.user_id=ma.user_id "
            "ORDER BY ma.id DESC LIMIT 40"
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
            "chat_xp": r["chat_xp"] or 0,
            "trust_name": trust_for(trust_score(r["done_count"], r["chat_xp"]))[1],
            "trust_emoji": trust_for(trust_score(r["done_count"], r["chat_xp"]))[2],
        } for r in team],
        "withdrawals": [dict(r) for r in withdrawals],
        "awards": [_award_public(dict(r)) for r in awards],
        "granted": [dict(r) for r in granted],
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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT * FROM members WHERE user_id=?", (uid,)
        )).fetchone()
        if not row:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        m = dict(row)
        if m["status"] == "approved":
            await db.rollback()
            return _json({
                "error": "already_decided",
                "message": "Участник уже одобрен.",
            }, status=409)
        if m["status"] not in ("pending", "blocked"):
            await db.rollback()
            return _json({
                "error": "already_decided",
                "message": "Эта заявка уже обработана.",
            }, status=409)
        if decision == "approve":
            # Одобрить можно и ранее отклонённого — иначе человека
            # невозможно вернуть в команду без правки базы.
            cur = await db.execute(
                "UPDATE members SET status='approved', role='helper', "
                "approved_at=?, approved_by=? "
                "WHERE user_id=? AND status IN ('pending','blocked')",
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
            # Одобрение в команду засчитывает реферала так же, как подписка.
            await db.execute(
                "UPDATE members SET ref_confirmed=1 WHERE user_id=?", (uid,))
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
                _notify(m["referred_by"], ref_message)
            except Exception:
                pass
        _notify(
            uid, "🎉 Заявка одобрена! Открой приложение — задания уже доступны.",
            _open_app_kb())
    elif decision == "reject":
        _notify(uid, "К сожалению, заявка отклонена.")
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
            "helper", "employee", "admin"
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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
        _notify(assigned_to, "\n".join(parts), _open_app_kb())
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
    tid = _as_int(body.get("task_id"))
    if tid is None:
        return _json({"error": "task"}, status=400)
    assignment_id = body.get("assignment_id")
    if assignment_id in ("", None):
        assignment_id = None
    ok = body.get("approve", True)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
        _notify(
            claimed_by,
            f"✅ Задание подтверждено! +{t.get('reward',0)} бибибонусов.")
    else:
        _notify(
            claimed_by, "Задание вернули на доработку — посмотри детали.")
    return _json({"ok": True})


async def api_admin_grant(request):
    """Ручное начисление/списание бонусов (напр. отоварить поездку)."""
    admin_id, err = await _require_admin(request)
    if err is not None:
        return err
    body = await _body(request)
    uid = _as_int(body.get("user_id"))
    if uid is None:
        return _json({"error": "bad_user"}, status=400)
    amount = _as_int(body.get("amount"))
    if amount is None:
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
    sign = "+" if amount > 0 else ""
    _notify(
        uid,
        f"💰 Баланс изменён: {sign}{amount} бибибонусов.\n"
        f"Причина: {reason}\n"
        f"Новый баланс: {balance}.",
    )
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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
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
    _notify(item["user_id"], message)
    return _json({
        "ok": True,
        "status": final_status,
        "balance": int(balance_row[0]) if balance_row else 0,
    })


# ============================================================
# ПОДПИСКА НА КАНАЛ
# ============================================================
async def check_subscription(uid):
    """True — подписан, False — нет, None — проверить не удалось.

    None означает «канал не настроен или бот не админ в нём» — в этом
    случае мы не наказываем человека и засчитываем реферала по старому
    правилу (одобрение в команду).
    """
    chat = _required_chat_id()
    if not chat:
        return None
    try:
        member = await bot.get_chat_member(chat, int(uid))
    except Exception as e:
        logger.warning("Не удалось проверить подписку %s: %s", uid, e)
        return None
    status = getattr(member, "status", "")
    status = getattr(status, "value", status)
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def confirm_referral(uid):
    """Засчитывает приглашённого пригласившему. Идемпотентно."""
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT referred_by, ref_confirmed FROM members WHERE user_id=?",
            (uid,))).fetchone()
        if not row or not row["referred_by"] or row["referred_by"] == uid:
            await db.rollback()
            return None, 0, 0
        if row["ref_confirmed"]:
            await db.rollback()
            return row["referred_by"], 0, 0
        cur = await db.execute(
            "UPDATE members SET ref_confirmed=1 WHERE user_id=? AND ref_confirmed=0",
            (uid,))
        if cur.rowcount != 1:
            await db.rollback()
            return row["referred_by"], 0, 0
        _, rewarded = await _grant_referral_milestones_in_tx(
            db, row["referred_by"], by=None)
        # Считаем сами: выплата ступеней возвращает 0, если сам
        # пригласивший ещё не одобрен, а прогресс-бар показывать надо.
        count = int((await (await db.execute(
            "SELECT COUNT(*) FROM members WHERE referred_by=? AND ref_confirmed=1",
            (row["referred_by"],))).fetchone())[0])
        await db.commit()
    referrer = row["referred_by"]
    if rewarded:
        text = (f"🎉 Реферальная ступень достигнута!\n"
                f"Друзей засчитано: {count}\n"
                f"Начислено: +{rewarded} бибибонусов.")
    else:
        nxt = next((c for c, _ in REFERRAL_MILESTONES if c > count), None)
        text = (f"👥 Друг подписался на канал. Засчитано: {count}"
                + (f" из {nxt} до следующей награды." if nxt
                   else ". Все ступени пройдены!"))
    _notify(referrer, text, _open_app_kb())
    return referrer, count, rewarded


async def api_referral_verify(request):
    """Кнопка «Я подписался» в приложении."""
    tg = await _auth_user(request)
    if not tg:
        return _json({"error": "auth"}, status=401)
    uid = tg["id"]
    member = await get_member(uid)
    if not member or not member["referred_by"]:
        return _json({
            "error": "no_referrer",
            "message": "Ты пришёл без ссылки друга — засчитывать нечего.",
        }, status=400)
    if member["ref_confirmed"]:
        return _json({"ok": True, "confirmed": True, "already": True})
    subscribed = await check_subscription(uid)
    if subscribed is None:
        return _json({
            "error": "unavailable",
            "message": "Не могу проверить подписку. Напиши ответственному.",
        }, status=503)
    if not subscribed:
        return _json({
            "error": "not_subscribed",
            "message": "Подписки пока не вижу. Подпишись и нажми ещё раз.",
        }, status=409)
    await confirm_referral(uid)
    return _json({"ok": True, "confirmed": True})


async def api_admin_set_role(request):
    """Меняет роль одобренного участника: помощник / сотрудник / ответственный."""
    admin_id, err = await _require_admin(request)
    if err is not None:
        return err
    body = await _body(request)
    uid = _as_int(body.get("user_id"))
    role = body.get("role")
    if uid is None:
        return _json({"error": "bad_user"}, status=400)
    if role not in ROLE_TITLES:
        return _json({"error": "role"}, status=400)
    if uid == admin_id:
        return _json({
            "error": "self",
            "message": "Свою роль менять нельзя — попроси другого ответственного.",
        }, status=400)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT role, status, full_name FROM members WHERE user_id=?",
            (uid,))).fetchone()
        if not row:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if row["status"] != "approved":
            await db.rollback()
            return _json({
                "error": "not_approved",
                "message": "Сначала одобри заявку — потом выдавай роль.",
            }, status=409)
        if row["role"] == role:
            await db.rollback()
            return _json({"ok": True, "already": True, "role": role})
        if row["role"] == "admin" and role != "admin":
            # Снять последнего ответственного нельзя: иначе модерировать
            # станет некому и заявки повиснут навсегда.
            left = int((await (await db.execute(
                "SELECT COUNT(*) FROM members "
                "WHERE role='admin' AND status='approved'"
            )).fetchone())[0])
            if left <= 1:
                await db.rollback()
                return _json({
                    "error": "last_admin",
                    "message": "Это последний ответственный. Сначала назначь другого.",
                }, status=409)
        await db.execute(
            "UPDATE members SET role=? WHERE user_id=? AND status='approved'",
            (role, uid))
        await db.commit()
    if role == "admin":
        text = ("🛡️ Тебе выдали роль ответственного.\n"
                "В приложении появилась вкладка «Модерация»: заявки, задания, "
                "награды и обмены.")
    else:
        text = (f"Твоя роль теперь — {ROLE_TITLES[role]}.\n"
                "Задания и бибибонусы работают как раньше.")
    _notify(uid, text, _open_app_kb())
    return _json({"ok": True, "role": role})


# ============================================================
# НАГРАДЫ
# ============================================================
def _award_public(a):
    return {
        "id": a["id"],
        "emoji": a["emoji"] or "🏅",
        "title": a["title"],
        "description": a["description"] or "",
        "bonus": int(a["bonus"] or 0),
        "repeatable": bool(a["repeatable"]),
        "active": bool(a["active"]),
    }


async def _my_awards(uid):
    """Полученные награды участника — для витрины в профиле."""
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT ma.id, ma.bonus, ma.note, ma.granted_at, "
            "a.emoji, a.title, a.description "
            "FROM member_awards ma JOIN awards a ON a.id=ma.award_id "
            "WHERE ma.user_id=? ORDER BY ma.id DESC LIMIT 60",
            (uid,),
        )).fetchall()
    return [dict(r) for r in rows]


async def api_awards(request):
    """Каталог наград и то, что уже получил сам участник."""
    tg = await _auth_user(request)
    if not tg:
        return _json({"error": "auth"}, status=401)
    admin = await is_admin(tg["id"])
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        # Ответственный видит и выключенные — их нужно уметь включить обратно.
        sql = "SELECT * FROM awards"
        if not admin:
            sql += " WHERE active=1"
        sql += " ORDER BY active DESC, bonus DESC, id"
        rows = await (await db.execute(sql)).fetchall()
    return _json({
        "ok": True,
        "catalog": [_award_public(dict(r)) for r in rows],
        "mine": await _my_awards(tg["id"]),
    })


async def api_admin_award_save(request):
    """Создать награду или отредактировать существующую."""
    admin_id, err = await _require_admin(request)
    if err is not None:
        return err
    body = await _body(request)
    title = (body.get("title") or "").strip()[:60]
    if len(title) < 2:
        return _json({
            "error": "title",
            "message": "Дай награде название — его увидит участник.",
        }, status=400)
    emoji = (body.get("emoji") or "🏅").strip()[:8] or "🏅"
    description = (body.get("description") or "").strip()[:200]
    bonus = _as_int(body.get("bonus"), 0)
    if bonus is None or bonus < 0 or bonus > 100000:
        return _json({
            "error": "bonus",
            "message": "Бонус — от 0 до 100 000.",
        }, status=400)
    repeatable = 1 if body.get("repeatable", True) else 0
    active = 1 if body.get("active", True) else 0
    award_id = _as_int(body.get("id"))
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        if award_id:
            cur = await db.execute(
                "UPDATE awards SET emoji=?, title=?, description=?, bonus=?, "
                "repeatable=?, active=? WHERE id=?",
                (emoji, title, description, bonus, repeatable, active, award_id),
            )
            if cur.rowcount != 1:
                await db.rollback()
                return _json({"error": "not_found"}, status=404)
        else:
            cur = await db.execute(
                "INSERT INTO awards "
                "(emoji, title, description, bonus, repeatable, active, "
                "created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (emoji, title, description, bonus, repeatable, active,
                 admin_id, now_iso()),
            )
            award_id = cur.lastrowid
        await db.commit()
    return _json({"ok": True, "award_id": award_id})


async def api_admin_award_grant(request):
    """Выдаёт награду участнику и сразу начисляет её бонус."""
    admin_id, err = await _require_admin(request)
    if err is not None:
        return err
    body = await _body(request)
    uid = _as_int(body.get("user_id"))
    award_id = _as_int(body.get("award_id"))
    if uid is None or award_id is None:
        return _json({"error": "bad_request"}, status=400)
    note = (body.get("note") or "").strip()[:200]
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        award = await (await db.execute(
            "SELECT * FROM awards WHERE id=?", (award_id,))).fetchone()
        if not award:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if not award["active"]:
            await db.rollback()
            return _json({
                "error": "inactive",
                "message": "Награда выключена — включи её в каталоге.",
            }, status=409)
        member = await (await db.execute(
            "SELECT full_name, bonus FROM members "
            "WHERE user_id=? AND status='approved'", (uid,))).fetchone()
        if not member:
            await db.rollback()
            return _json({
                "error": "not_found",
                "message": "Награду можно выдать только одобренному участнику.",
            }, status=404)
        bonus = int(award["bonus"] or 0)
        # Разовая награда занимает единственный слот '' — повторная выдача
        # упрётся в UNIQUE. У многоразовой слот уникален для каждой выдачи.
        slot = "" if not award["repeatable"] else f"{now_iso()}:{secrets.token_hex(4)}"
        try:
            await db.execute(
                "INSERT INTO member_awards "
                "(user_id, award_id, slot, bonus, note, granted_by, granted_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (uid, award_id, slot, bonus, note, admin_id, now_iso()),
            )
        except aiosqlite.IntegrityError:
            await db.rollback()
            return _json({
                "error": "already_granted",
                "message": "Эта награда у участника уже есть.",
            }, status=409)
        if bonus:
            await db.execute(
                "UPDATE members SET bonus=bonus+? WHERE user_id=?", (bonus, uid))
            await db.execute(
                "INSERT INTO bonus_ledger "
                "(user_id, amount, reason, task_id, created_by, created_at) "
                "VALUES (?,?,?,NULL,?,?)",
                (uid, bonus, f"Награда: {award['title']}", admin_id, now_iso()),
            )
        balance = int(member["bonus"]) + bonus
        await db.commit()
    text = f"{award['emoji']} Награда: {award['title']}"
    if award["description"]:
        text += f"\n{award['description']}"
    if note:
        text += f"\nОт ответственного: {note}"
    if bonus:
        text += f"\n\n+{bonus} бибибонусов. Баланс: {balance}."
    _notify(uid, text, _open_app_kb())
    return _json({"ok": True, "balance": balance, "bonus": bonus})


async def api_admin_award_revoke(request):
    """Снимает выданную награду и забирает её бонус обратно."""
    admin_id, err = await _require_admin(request)
    if err is not None:
        return err
    body = await _body(request)
    entry_id = _as_int(body.get("entry_id"))
    if entry_id is None:
        return _json({"error": "bad_request"}, status=400)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT ma.*, a.title FROM member_awards ma "
            "JOIN awards a ON a.id=ma.award_id WHERE ma.id=?",
            (entry_id,),
        )).fetchone()
        if not row:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        cur = await db.execute("DELETE FROM member_awards WHERE id=?", (entry_id,))
        if cur.rowcount != 1:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        take = int(row["bonus"] or 0)
        if take:
            balance_row = await (await db.execute(
                "SELECT bonus FROM members WHERE user_id=?",
                (row["user_id"],))).fetchone()
            # Бонус мог быть уже потрачен — уводить баланс в минус нельзя.
            take = min(take, int(balance_row[0]) if balance_row else 0)
        if take:
            await db.execute(
                "UPDATE members SET bonus=bonus-? WHERE user_id=?",
                (take, row["user_id"]))
            await db.execute(
                "INSERT INTO bonus_ledger "
                "(user_id, amount, reason, task_id, created_by, created_at) "
                "VALUES (?,?,?,NULL,?,?)",
                (row["user_id"], -take,
                 f"Снята награда: {row['title']}", admin_id, now_iso()),
            )
        await db.commit()
    _notify(
        row["user_id"],
        f"Награда «{row['title']}» снята ответственным."
        + (f"\nСписано {take} бибибонусов." if take else ""),
    )
    return _json({"ok": True, "taken": take})


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


@web.middleware
async def error_middleware(request, handler):
    """Любой сбой отдаём как JSON, иначе фронт показывает пустую «Ошибку»."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        logger.exception("Сбой в %s %s", request.method, request.path)
        return _json({
            "error": "server",
            "message": "Что-то сломалось на сервере. Попробуй ещё раз.",
        }, status=500)


async def start_api_server():
    try:
        app = web.Application(middlewares=[error_middleware])
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
        app.router.add_post("/api/referral/verify", api_referral_verify)
        app.router.add_post("/api/admin/role", api_admin_set_role)
        app.router.add_get("/api/awards", api_awards)
        app.router.add_post("/api/admin/award/save", api_admin_award_save)
        app.router.add_post("/api/admin/award/grant", api_admin_award_grant)
        app.router.add_post("/api/admin/award/revoke", api_admin_award_revoke)
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


def _subscribe_kb():
    """Кнопки «Подписаться» и «Я подписался»."""
    rows = []
    url = _required_chat_url()
    if url:
        rows.append([InlineKeyboardButton(text="📣 Подписаться на канал", url=url)])
    rows.append([InlineKeyboardButton(
        text="✅ Я подписался", callback_data="ref_check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


SUBSCRIBE_PROMPT = (
    "Ты пришёл по ссылке друга 👋\n\n"
    "Подпишись на канал Бибибайка — и другу засчитается приглашение, "
    "а ты первым увидишь новые задания и акции.\n\n"
    "Подписался? Жми кнопку ниже, я проверю."
)


async def _handle_referral_gate(uid, send):
    """Проверяет подписку и либо засчитывает друга, либо просит подписаться.

    send — функция отправки сообщения (message.answer или свой врапper).
    Возвращает True, если реферал засчитан прямо сейчас.
    """
    member = await get_member(uid)
    if not member or not member["referred_by"] or member["referred_by"] == uid:
        return False
    if member["ref_confirmed"]:
        return False
    subscribed = await check_subscription(uid)
    if subscribed is True:
        await confirm_referral(uid)
        await send("Спасибо, подписка на месте — другу засчитано приглашение ✅")
        return True
    if subscribed is False:
        await send(SUBSCRIBE_PROMPT, _subscribe_kb())
    # subscribed is None — канал не настроен или бот не админ в нём.
    # Молчим: реферал засчитается по старому правилу, при одобрении заявки.
    return False


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
    await _handle_referral_gate(
        uid,
        lambda text, kb=None: message.answer(text, reply_markup=kb),
    )


@dp.callback_query(F.data == "ref_check")
async def ref_check(call: CallbackQuery):
    """Кнопка «Я подписался» под приглашением."""
    uid = call.from_user.id
    member = await get_member(uid)
    if not member or not member["referred_by"]:
        await call.answer("Ты пришёл без ссылки друга.", show_alert=True)
        return
    if member["ref_confirmed"]:
        await call.answer("Уже засчитано ✅", show_alert=True)
        return
    subscribed = await check_subscription(uid)
    if subscribed is None:
        await call.answer(
            "Не могу проверить подписку. Напиши ответственному.", show_alert=True)
        return
    if not subscribed:
        await call.answer(
            "Подписки пока не вижу. Подпишись и нажми ещё раз.", show_alert=True)
        return
    await confirm_referral(uid)
    await call.answer("Готово! Другу засчитано приглашение ✅", show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=_open_app_kb())
    except Exception:
        pass


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


@dp.message(F.text.regexp(r"(?i)^/(инструкция|instruction)"))
async def post_instruction(message: Message):
    """Публикует инструкцию в подтему «Работа». Только для ответственных."""
    await _publish(message, [WORK_INSTRUCTION], TOPIC_WORK, "work")


@dp.message(F.text.regexp(r"(?i)^/(ранг|rank|уровень)"))
async def show_rank(message: Message):
    """Показывает уровень доверия — работает и в группе, и в личке."""
    user = message.from_user
    m = await _ensure_member(user)
    done = m["done_count"] if m else 0
    xp = (m["chat_xp"] if m else 0) or 0
    score = trust_score(done, xp)
    level = trust_for(score)
    nxt = next_trust(score)
    lines = [
        f"{level[2]} <b>{user.full_name}</b> — {level[1]}",
        f"Заданий выполнено: {done}",
        f"Опыт в беседе: {xp} "
        f"(≈{xp // max(1, CHAT_XP_PER_TASK)} к уровню)",
    ]
    if nxt:
        lines.append(f"До уровня «{nxt[1]}»: {max(0, nxt[3] - score)}")
    else:
        lines.append("Максимальный уровень 🎉")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ============================================================
# ПОСТЫ ДЛЯ ПОДТЕМ И КОМАНДЫ УЧАСТНИКОВ
# ============================================================
APP_STORE_URL = os.getenv(
    "APP_STORE_URL",
    "https://apps.apple.com/ru/app/id6751171018")
GOOGLE_PLAY_URL = os.getenv(
    "GOOGLE_PLAY_URL",
    "https://play.google.com/store/apps/details?id=tech.bumerang.bbbike")
FRANCHISE_URL = os.getenv("FRANCHISE_URL", "https://start.bb.bike/")
PARTNER_EMAIL = os.getenv("PARTNER_EMAIL", "partner@bb.bike")


def _topic_link(topic_id):
    """Ссылка на подтему: https://t.me/<группа>/<id>."""
    if not GROUP_USERNAME or not topic_id:
        return None
    return f"https://t.me/{GROUP_USERNAME}/{topic_id}"


def _link(url, text):
    """Ссылка для Telegram HTML, либо просто текст, если ссылки нет."""
    return f'<a href="{url}">{text}</a>' if url else text


def _nav_line():
    """Навигация по подтемам одной строкой."""
    parts = []
    for topic, label in (
        (TOPIC_NEWS, "📣 Новости"),
        (TOPIC_CHAT, "💬 Болталка"),
        (TOPIC_WORK, "🛠 Работа"),
        (TOPIC_FRANCHISE, "🚲 Франшиза"),
    ):
        url = _topic_link(topic)
        if url:
            parts.append(_link(url, label))
    return "  ·  ".join(parts)


def franchise_post():
    app = _app_url()
    return [
        (
            "🚲 <b>ФРАНШИЗА БИБИБАЙК</b>\n\n"
            "Шеринг электровелосипедов под ключ: техника, IT-платформа и "
            "отлаженные операционные процессы. Вы получаете один город "
            "в эксклюзив и запускаетесь за 15–28 дней.\n\n"
            "📌 <b>Условия</b>\n\n"
            "<b>Паушальный взнос — 0 ₽.</b> Стартового взноса нет вообще, "
            "мы намеренно снижаем порог входа.\n"
            "<b>Роялти — 17,3%</b> от валовой выручки, раз в месяц по данным "
            "биллинга. Без минимального платежа и маркетинговых сборов.\n"
            "<b>Договор</b> — коммерческая концессия на 5 лет "
            "с автопролонгацией.\n"
            "<b>Инвестиции — от 5 000 000 ₽:</b> байки с IoT, батареи и ЗИП, "
            "лицензии софта, брендирование, обучение команды.\n"
            "<b>Стартовый парк</b> — рекомендуем от 100 единиц. Меньше держать "
            "сложнее: растут удельные расходы, падает число поездок на байк.\n"
            "<b>EBITDA — 52–58%.</b>\n"
            "<b>Окупаемость — около 7 месяцев</b> по кейсам в городах "
            "на 100–500 тысяч жителей при 3,5–6 поездках на байк в день.\n\n"
            "🏙 <b>Эксклюзив на город</b>\n\n"
            "В каждом городе работает один партнёр. Границы территории и срок "
            "прописаны в договоре, продление автоматическое при соблюдении KPI "
            "и согласованного размера парка."
        ),
        (
            "⚙️ <b>Что вы получаете</b>\n\n"
            "<b>IT-платформа.</b> Клиентское приложение, админ-панель, трекинг. "
            "Тарифы, геозоны, платежи и аналитика в реальном времени, без "
            "релизов приложения. Тепловые карты спроса показывают, куда везти "
            "байки прямо сейчас.\n\n"
            "<b>Телематика в каждом байке.</b> Удалённая блокировка, "
            "геофенсинг, диагностика. Сигналы о падении, вскрытии отсека, "
            "попытке угона и потере связи приходят мгновенно. Обновления "
            "прошивки по воздуху.\n\n"
            "<b>Техника.</b> Батарея 24 Ah на 1,152 кВт⋅ч, защита IPX5, "
            "до 4 часов работы, полная зарядка за 3 часа, замена за минуту. "
            "Антивандальный корпус, свой склад запчастей — ждать поставок "
            "не нужно.\n\n"
            "<b>Поддержка.</b> Персональный менеджер, круглосуточная "
            "техподдержка по SLA, база регламентов и чек-листов. Обучение: "
            "2–3 дня теории плюс 2–4 полевые смены с наставником "
            "и короткая аттестация.\n\n"
            "📈 <b>Рынок</b>\n\n"
            "К 2027 году объём рынка краткосрочной аренды в России "
            "прогнозируется в 53,6 млрд ₽ — на 72% больше, чем в 2024-м. "
            "Пользователей ожидается 35 млн, плюс 38% к 2024 году. "
            "Электровелосипед выигрывает у самоката на длинных дистанциях, "
            "неровных дорогах и в холмистых городах: поездки дольше, "
            "средний чек выше — от 360 ₽.\n"
            "<i>Данные отраслевого портала «Трушеринг».</i>"
        ),
        (
            "🗺 <b>Уже работаем</b>\n\n"
            "Запустились в октябре 2025 года, открыто пять городов: Краснодар, "
            "Химки, Сочи, Красная Поляна, Ставрополь. Суммарный парк — около "
            "1 570 байков. Приложением пользуются более 100 000 человек.\n\n"
            "❓ <b>Частые вопросы</b>\n\n"
            "<b>Что зимой?</b> Парк консервируется: тёплое хранение, "
            "обслуживание, проверка батарей и прошивок. В тёплых зонах можно "
            "оставить минимальный парк.\n\n"
            "<b>Кражи и вандализм?</b> По кейсам списания по краже или утрате — "
            "0,8–1,8% парка за сезон, повреждения с ремонтом — 5–9%. Работают "
            "IoT-блокировка, геозоны, скрытые трекеры, сирены и фотофиксация.\n\n"
            "<b>Кто за что отвечает?</b> На нас — софт и инфраструктура, "
            "техника и ЗИП, обучение, регламенты, поддержка и аналитика. "
            "На партнёре — склад и зарядка, команда, логистика, KPI, отношения "
            "с городом и арендодателями.\n\n"
            "<b>Юридическая схема?</b> Договор коммерческой концессии, роялти "
            "проводятся официально. Обычно подходит ООО или ИП на УСН, финальную "
            "схему согласуем с вашим бухгалтером.\n\n"
            "✍️ <b>Как начать</b>\n\n"
            f"Условия, калькулятор доходности и пример P&amp;L — "
            f"{_link(FRANCHISE_URL, 'start.bb.bike')}\n"
            f"Почта для партнёров — {_link('mailto:' + PARTNER_EMAIL, PARTNER_EMAIL)}\n"
            "Вопросы можно задать прямо в этой подтеме.\n\n"
            "Писать сюда могут участники, прошедшие одобрение в приложении — "
            "так мы отсекаем спам и держим обсуждение по делу. Если сообщение "
            "удалилось, бот пришлёт в личку, что делать.\n\n"
            "<i>Расчёты ориентировочные, не являются публичной офертой "
            "и инвестиционной рекомендацией. Фактические результаты зависят "
            "от погоды, спроса, тарифов, расходов и качества локаций. Полный "
            "перечень прав и обязанностей — в договоре коммерческой концессии.</i>"
        ),
    ]


def chat_post():
    return [
        (
            "💬 <b>ЗДЕСЬ МОЖНО ПРОСТО ПООБЩАТЬСЯ</b>\n\n"
            "Это общий чат Бибибайка. Обсуждаем поездки, маршруты, технику, "
            "город, делимся находками и спрашиваем совета. Никакой "
            "обязательной повестки.\n\n"
            "⚡️ <b>За общение капает опыт</b>\n\n"
            "У каждого есть уровень доверия — 🌱 Новичок, ⭐ Проверенный, "
            "👑 Амбассадор. Он растёт от выполненных заданий и от живого "
            "участия в этом чате.\n\n"
            "• Сообщение в чате — небольшой опыт\n"
            "• Кто-то сказал вам спасибо — заметно больше\n\n"
            "Полезность важнее количества. Флудить бессмысленно: опыт капает "
            "не чаще раза в минуту и упирается в дневной потолок. Спасибо "
            "засчитывается только ответом на конкретное сообщение, и одна "
            "и та же пара людей может благодарить друг друга раз в 12 часов. "
            "Себе и боту — нельзя.\n\n"
            "Засчитанное спасибо бот отмечает реакцией 🙏. О переходе "
            "на новый уровень пишет сюда же.\n\n"
            "Чем выше уровень — тем крупнее задания доступны и меньше проверок."
        ),
        (
            "🤖 <b>КОМАНДЫ БОТА</b>\n\n"
            "<code>/профиль</code> — уровень и бибибонусы одним сообщением\n"
            "<code>/ранг</code> — ваш уровень доверия, опыт и сколько "
            "осталось до следующей ступени\n"
            "<code>/бонусы</code> — сколько у вас бибибонусов и что с ними "
            "делать\n"
            "<code>/задания</code> — открыть приложение с заданиями\n"
            "<code>/команды</code> — этот список\n\n"
            "Работают и здесь, и в личке с ботом. У команд есть синонимы: "
            "<code>/уровень</code>, <code>/баланс</code>, <code>/help</code>.\n\n"
            "📋 <b>Правила, их немного</b>\n\n"
            "• Без оскорблений, травли и переходов на личности\n"
            "• Без рекламы и спама\n"
            "• Рабочие вопросы — в «Работу», франшиза — во «Франшизу»\n"
            "• Уважайте чужое время: один вопрос лучше десяти сообщений подряд\n\n"
            "Всё остальное можно. Пишите 👋\n\n"
            + _nav_line()
        ),
    ]


def news_post():
    return [
        (
            "📣 <b>НОВОСТИ БИБИБАЙКА</b>\n\n"
            "Главный канал объявлений. Здесь публикуем то, что важно "
            "знать всем:\n\n"
            "• Запуски новых городов и расширение зон катания\n"
            "• Обновления приложения — новые функции каждые 2–4 недели\n"
            "• Изменения тарифов, акции и промокоды\n"
            "• Технические работы и всё, что влияет на поездку\n"
            "• Новости по франшизе и партнёрской программе\n\n"
            "Если вы пришли по приглашению друга — подпишитесь на группу. "
            "После подписки другу засчитывается приглашение, а вам "
            "открывается доступ к заданиям и бибибонусам.\n\n"
            "🧭 <b>Куда идти по вопросам</b>\n\n"
            + _nav_line() + "\n\n"
            "📱 <b>Приложение</b>\n\n"
            "Бибибайк есть в "
            + _link(APP_STORE_URL, "App Store") + " и "
            + _link(GOOGLE_PLAY_URL, "Google Play") + ". "
            "Регистрация по SMS или через Telegram занимает меньше минуты. "
            "Аренда работает даже без интернета — поездку можно начать "
            "и завершить по SMS.\n\n"
            "Хорошей дороги 🚲"
        ),
    ]


def _post_kb(kind):
    """Кнопки под постом: у каждой подтемы свои."""
    rows = []
    app = _app_url()
    if kind == "franchise":
        rows.append([InlineKeyboardButton(
            text="📊 Условия и калькулятор", url=FRANCHISE_URL)])
        if app:
            rows.append([InlineKeyboardButton(text="🚲 Открыть приложение", url=app)])
    elif kind == "news":
        rows.append([
            InlineKeyboardButton(text="App Store", url=APP_STORE_URL),
            InlineKeyboardButton(text="Google Play", url=GOOGLE_PLAY_URL),
        ])
        if app:
            rows.append([InlineKeyboardButton(text="🚲 Задания и бонусы", url=app)])
    else:
        if app:
            rows.append([InlineKeyboardButton(text="🚲 Открыть приложение", url=app)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# У General-подтемы форума thread_id = 1, но Telegram не принимает его
# в message_thread_id и отвечает «message thread not found».
# Туда нужно отправлять вообще без указания подтемы.
GENERAL_TOPIC_ID = 1


def _thread_kwargs(topic):
    if not topic or int(topic) == GENERAL_TOPIC_ID:
        return {}
    return {"message_thread_id": int(topic)}


async def _send_to_topic(chat_id, text, topic, **kw):
    """Отправка в подтему с запасным вариантом, если подтема не найдена.

    Так команда не падает, если id подтемы поменялся или это General.
    """
    try:
        return await bot.send_message(
            chat_id, text, **_thread_kwargs(topic), **kw)
    except Exception as e:
        if "thread not found" not in str(e).lower() or not topic:
            raise
        logger.info("Подтема %s не найдена, отправляю без неё", topic)
        return await bot.send_message(chat_id, text, **kw)


async def _get_published(kind):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            "SELECT * FROM published_posts WHERE kind=?", (kind,))).fetchone()


async def _remember_published(kind, chat_id, topic, ids, by):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            "INSERT INTO published_posts "
            "(kind, chat_id, topic, message_ids, published_at, published_by) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(kind) DO UPDATE SET chat_id=excluded.chat_id, "
            "topic=excluded.topic, message_ids=excluded.message_ids, "
            "published_at=excluded.published_at, "
            "published_by=excluded.published_by",
            (kind, chat_id, topic, json.dumps(ids), now_iso(), by))
        await db.commit()


def _post_link(row):
    """Ссылка на первое сообщение поста, если группа публичная."""
    if not GROUP_USERNAME:
        return None
    try:
        ids = json.loads(row["message_ids"])
    except Exception:
        return None
    if not ids:
        return None
    topic = row["topic"]
    if topic and int(topic) != GENERAL_TOPIC_ID:
        return f"https://t.me/{GROUP_USERNAME}/{topic}/{ids[0]}"
    return f"https://t.me/{GROUP_USERNAME}/{ids[0]}"


def _wants_repost(message):
    """Повторная публикация только по явному слову: /новости заново."""
    text = (message.text or "").lower()
    return any(w in text for w in ("заново", "переопубликовать", "force", "-f"))


async def _publish(message, parts, topic, kind):
    """Публикует пост в подтему. Один раз — повтор только по «заново»."""
    if not await is_admin(message.from_user.id):
        return
    target = message.chat.id
    if message.chat.type == "private":
        if not GROUP_ID and not GROUP_USERNAME:
            await message.answer("Группа не настроена: задай GROUP_USERNAME.")
            return
        target = GROUP_ID or f"@{GROUP_USERNAME}"

    old = await _get_published(kind)
    repost = _wants_repost(message)
    if old and not repost:
        link = _post_link(old)
        when = (old["published_at"] or "")[:16].replace("T", " ")
        await message.answer(
            f"Этот пост уже опубликован{f' — {when}' if when else ''}."
            + (f"\n{link}" if link else "")
            + "\n\nЧтобы заменить его новым, напиши команду со словом "
              "<b>заново</b> — старый пост я удалю.",
            parse_mode="HTML", disable_web_page_preview=True)
        return

    if old and repost:
        # Чистим прошлую версию, чтобы в подтеме не копились дубли.
        try:
            for mid in json.loads(old["message_ids"]):
                try:
                    await bot.delete_message(old["chat_id"], mid)
                except Exception:
                    pass
        except Exception:
            pass

    ids, sent = [], None
    try:
        for i, part in enumerate(parts):
            last = (i == len(parts) - 1)
            sent = await _send_to_topic(
                target, part, topic,
                reply_markup=(_post_kb(kind) if last else None),
                parse_mode="HTML",
                disable_web_page_preview=True)
            if sent is not None and getattr(sent, "message_id", None):
                ids.append(sent.message_id)
    except Exception as e:
        logger.warning("Публикация «%s» не прошла: %s", kind, e)
        await message.answer(
            "Не отправилось. Проверь, что бот — администратор группы "
            "и что id подтемы верный: зайди в нужную подтему и напиши "
            f"<code>/подтема</code>.\n\nТекст ошибки: {_html(e)}",
            parse_mode="HTML")
        return

    await _remember_published(kind, target if isinstance(target, int)
                              else message.chat.id, topic, ids,
                              message.from_user.id)
    if message.chat.type == "private" or repost:
        await message.answer(
            ("Переопубликовано ✅" if repost else "Опубликовано ✅")
            + f" Сообщений: {len(parts)}")
    return sent


@dp.message(F.text.regexp(r"(?i)^/(франшиза|franchise)"))
async def post_franchise(message: Message):
    await _publish(message, franchise_post(), TOPIC_FRANCHISE, "franchise")


@dp.message(F.text.regexp(r"(?i)^/(болталка|chat)"))
async def post_chat(message: Message):
    await _publish(message, chat_post(), TOPIC_CHAT, "chat")


@dp.message(F.text.regexp(r"(?i)^/(новости|news)"))
async def post_news(message: Message):
    await _publish(message, news_post(), TOPIC_NEWS, "news")


# ── Приветствие новичков ──────────────────────────────────────
WELCOME_JOIN = "{name}, добро пожаловать в фан-клуб Бибибайка 🚲"


@dp.message(F.new_chat_members)
async def greet_newcomers(message: Message):
    """Убирает служебное «вступил» и здоровается по-человечески."""
    if not _is_our_group(message):
        return
    # Служебное сообщение убираем сразу, чтобы лента не засорялась.
    try:
        await message.delete()
    except Exception as e:
        logger.info("Не смог удалить служебное сообщение о входе: %s", e)
    newcomers = [u for u in (message.new_chat_members or []) if not u.is_bot]
    if not newcomers:
        return
    for user in newcomers:
        await _ensure_member(user)
    names = ", ".join(
        f'<a href="tg://user?id={u.id}">{_html(u.full_name)}</a>'
        for u in newcomers)
    text = WELCOME_JOIN.format(name=names)
    rank_hint = _topic_link(TOPIC_CHAT)
    text += (
        "\n\nЗдесь можно общаться, за это растёт уровень доверия. "
        "Напиши <code>/команды</code> — покажу, что умею."
    )
    try:
        await _send_to_topic(
            message.chat.id, text, TOPIC_CHAT,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_post_kb("chat"))
    except Exception as e:
        logger.info("Не смог поздороваться с новичком: %s", e)


def _html(text):
    """Экранирование для Telegram HTML."""
    return (str(text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ── Команды участников ────────────────────────────────────────
@dp.message(F.text.regexp(r"(?i)^/(бонусы|баланс|balance)"))
async def show_bonus(message: Message):
    m = await _ensure_member(message.from_user)
    bonus = (m["bonus"] if m else 0) or 0
    done = (m["done_count"] if m else 0) or 0
    lines = [
        f"💰 <b>{_html(message.from_user.full_name)}</b> — "
        f"<b>{bonus}</b> бибибонусов",
        f"Выполнено заданий: {done}",
    ]
    if bonus < WITHDRAW_MIN:
        lines.append(f"До обмена не хватает {WITHDRAW_MIN - bonus}.")
    else:
        lines.append("Можно обменять — заявка в «Кошельке» приложения.")
    await message.answer("\n".join(lines), parse_mode="HTML",
                         reply_markup=_open_app_kb())


@dp.message(F.text.regexp(r"(?i)^/(профиль|profile)"))
async def show_profile(message: Message):
    """Уровень и бибибонусы одним сообщением."""
    user = message.from_user
    m = await _ensure_member(user)
    done = (m["done_count"] if m else 0) or 0
    xp = (m["chat_xp"] if m else 0) or 0
    bonus = (m["bonus"] if m else 0) or 0
    score = trust_score(done, xp)
    level = trust_for(score)
    nxt = next_trust(score)
    lines = [
        f"{level[2]} <b>{_html(user.full_name)}</b> — {level[1]}",
        "",
        f"💰 Бибибонусов: <b>{bonus}</b>",
        f"✅ Заданий выполнено: {done}",
        f"💬 Опыт в беседе: {xp}"
        + (f" (≈{xp // max(1, CHAT_XP_PER_TASK)} к уровню)"
           if xp >= CHAT_XP_PER_TASK else ""),
    ]
    lines.append(
        f"До уровня «{nxt[1]}»: {max(0, nxt[3] - score)}" if nxt
        else "Максимальный уровень 🎉")
    await message.answer("\n".join(lines), parse_mode="HTML",
                         reply_markup=_open_app_kb())


@dp.message(F.text.regexp(r"(?i)^/(подтема|topic|id)"))
async def show_topic_id(message: Message):
    """Показывает настоящие id чата и подтемы — для настройки."""
    if not await is_admin(message.from_user.id):
        return
    thread = getattr(message, "message_thread_id", None)
    known = {
        TOPIC_NEWS: "TOPIC_NEWS",
        TOPIC_CHAT: "TOPIC_CHAT",
        TOPIC_WORK: "TOPIC_WORK",
        TOPIC_FRANCHISE: "TOPIC_FRANCHISE",
    }
    if thread is None:
        where = ("General (общая подтема). В настройках ей соответствует "
                 f"id {GENERAL_TOPIC_ID}.")
        thread_value = GENERAL_TOPIC_ID
    else:
        where = f"id подтемы: <b>{thread}</b>"
        thread_value = thread
    ours = _is_our_group(message)
    lines = [
        "🔎 <b>Где мы сейчас</b>",
        f"chat_id: <code>{message.chat.id}</code>",
        f"username чата: <code>{message.chat.username or '—'}</code>",
        where,
        f"Сейчас настроено как: <b>{known.get(thread_value, 'нигде')}</b>",
        "",
        ("✅ Группа распознана — опыт, приветствие и правила подтем работают."
         if ours else
         "❌ <b>Группа НЕ распознана.</b> Опыт за общение, приветствие "
         "новичков и защита подтемы франшизы не работают. Поставь "
         f"<code>GROUP_ID={message.chat.id}</code> в переменных окружения."),
        "",
        "Текущие настройки:",
        f"GROUP_USERNAME = {GROUP_USERNAME or '—'}",
        f"GROUP_ID = {GROUP_ID or '—'}",
        f"TOPIC_NEWS = {TOPIC_NEWS}",
        f"TOPIC_CHAT = {TOPIC_CHAT}",
        f"TOPIC_WORK = {TOPIC_WORK}",
        f"TOPIC_FRANCHISE = {TOPIC_FRANCHISE}",
    ]
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(F.text.regexp(r"(?i)^/(задания|tasks)"))
async def show_tasks(message: Message):
    await message.answer(
        "🚲 Задания, бибибонусы и заявки — в приложении.",
        reply_markup=_open_app_kb())


@dp.message(F.text.regexp(r"(?i)^/(команды|help|старт)"))
async def show_commands(message: Message):
    text = (
        "🤖 <b>Что я умею</b>\n\n"
        "<code>/профиль</code> — уровень и бибибонусы одним сообщением\n"
        "<code>/ранг</code> — уровень доверия, опыт и прогресс\n"
        "<code>/бонусы</code> — сколько у вас бибибонусов\n"
        "<code>/задания</code> — открыть приложение\n"
        "<code>/команды</code> — этот список\n\n"
        "Синонимы: <code>/уровень</code>, <code>/баланс</code>, "
        "<code>/help</code>.\n\n"
        "Уровень растёт от выполненных заданий и от общения в беседе: "
        "обычное сообщение даёт немного опыта, а «спасибо» в ответ "
        "на ваше сообщение — заметно больше."
    )
    nav = _nav_line()
    if nav:
        text += "\n\n" + nav
    if await is_admin(message.from_user.id):
        text += (
            "\n\n🛡 <b>Для ответственных</b>\n"
            "<code>/инструкция</code> — опубликовать инструкцию в «Работу»\n"
            "<code>/франшиза</code> — пост в подтему «Франшиза»\n"
            "<code>/болталка</code> — пост в подтему «Болталка»\n"
            "<code>/новости</code> — пост в подтему «Новости»"
        )
    await message.answer(text, parse_mode="HTML",
                         disable_web_page_preview=True,
                         reply_markup=_open_app_kb())


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
# ГРУППА: опыт за общение, «спасибо», правила подтем
# ============================================================
import re as _re

# «Спасибо» ловим по корням, чтобы работали и опечатки, и склонения.
THANKS_RE = _re.compile(
    r"(спасиб|спсб|\bспс\b|благодар|признателен|признательна|"
    r"выруч|\bреспект\b|\bсэнкс\b|\bthanks?\b|\bthx\b|🙏)",
    _re.IGNORECASE,
)

_group_admins = {"ids": set(), "at": 0.0}


def _is_our_group(message):
    if message.chat.type not in ("group", "supergroup"):
        return False
    if GROUP_ID and message.chat.id == GROUP_ID:
        return True
    if GROUP_USERNAME and (message.chat.username or "").lower() == GROUP_USERNAME.lower():
        return True
    return not GROUP_ID and not GROUP_USERNAME


def _topic_of(message):
    """id подтемы; у General ветки message_thread_id пустой."""
    return getattr(message, "message_thread_id", None) or TOPIC_NEWS


async def _group_admin_ids(chat_id):
    """Список админов группы с кэшем на 5 минут — чтобы не дёргать API на каждое сообщение."""
    now = time.time()
    if now - _group_admins["at"] < 300 and _group_admins["ids"]:
        return _group_admins["ids"]
    try:
        admins = await bot.get_chat_administrators(chat_id)
        _group_admins["ids"] = {a.user.id for a in admins}
        _group_admins["at"] = now
    except Exception as e:
        logger.info("Не удалось получить админов группы: %s", e)
    return _group_admins["ids"]


async def _ensure_member(user):
    """Заводит запись под участника чата, если его ещё нет.

    Заявку при этом не создаём — в очередь модерации такие люди не попадают.
    """
    m = await get_member(user.id)
    if not m:
        await upsert_member(
            user.id,
            full_name=(user.full_name or ""),
            username=(user.username or ""))
        m = await get_member(user.id)
    return m


async def add_chat_xp(user, amount, kind):
    """Начисляет опыт и, если уровень вырос, возвращает новый уровень.

    kind: 'msg' или 'thanks' — у них отдельные дневные потолки.
    Возвращает (начислено, новый_уровень_или_None).
    """
    if amount <= 0:
        return 0, None
    m = await _ensure_member(user)
    if not m:
        return 0, None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cap = MSG_XP_DAILY_CAP if kind == "msg" else THANKS_XP_DAILY_CAP
    column = "msg_xp_today" if kind == "msg" else "thanks_xp_today"
    total_column = "messages_total" if kind == "msg" else "thanks_total"
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT * FROM chat_activity WHERE user_id=?", (user.id,))).fetchone()
        if not row:
            await db.execute(
                "INSERT INTO chat_activity (user_id, day) VALUES (?,?)",
                (user.id, today))
            row = await (await db.execute(
                "SELECT * FROM chat_activity WHERE user_id=?", (user.id,))).fetchone()
        if row["day"] != today:
            await db.execute(
                "UPDATE chat_activity SET day=?, msg_xp_today=0, thanks_xp_today=0 "
                "WHERE user_id=?", (today, user.id))
            used = 0
        else:
            used = int(row[column] or 0)
        left = max(0, cap - used)
        grant = min(amount, left)
        if grant <= 0:
            await db.rollback()
            return 0, None
        before = await (await db.execute(
            "SELECT done_count, chat_xp FROM members WHERE user_id=?",
            (user.id,))).fetchone()
        await db.execute(
            f"UPDATE chat_activity SET {column}={column}+?, "
            f"{total_column}={total_column}+1, last_msg_at=? WHERE user_id=?",
            (grant, now_iso(), user.id))
        await db.execute(
            "UPDATE members SET chat_xp=chat_xp+? WHERE user_id=?", (grant, user.id))
        await db.commit()
    old_level = trust_for(trust_score(before["done_count"], before["chat_xp"]))
    new_level = trust_for(trust_score(before["done_count"], (before["chat_xp"] or 0) + grant))
    return grant, (new_level if new_level[0] != old_level[0] else None)


async def _msg_cooldown_passed(uid):
    """Не чаще одного зачёта в MSG_COOLDOWN_SEC — чтобы не фармили флудом."""
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        row = await (await db.execute(
            "SELECT last_msg_at FROM chat_activity WHERE user_id=?", (uid,))).fetchone()
    if not row or not row[0]:
        return True
    try:
        last = datetime.fromisoformat(row[0])
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() >= MSG_COOLDOWN_SEC


async def _thanks_pair_allowed(from_id, to_id):
    """Один и тот же человек не может благодарить одного и того же по кругу."""
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        row = await (await db.execute(
            "SELECT last_at FROM thanks_pairs WHERE from_id=? AND to_id=?",
            (from_id, to_id))).fetchone()
        if row:
            try:
                last = datetime.fromisoformat(row[0])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                delta = (datetime.now(timezone.utc) - last).total_seconds()
                if delta < THANKS_PAIR_COOLDOWN_H * 3600:
                    return False
            except ValueError:
                pass
        await db.execute(
            "INSERT INTO thanks_pairs (from_id, to_id, last_at) VALUES (?,?,?) "
            "ON CONFLICT(from_id, to_id) DO UPDATE SET last_at=excluded.last_at",
            (from_id, to_id, now_iso()))
        await db.commit()
    return True


async def _announce_level(message, user, level):
    """Короткое поздравление в той же подтеме."""
    try:
        await message.answer(
            f"{level[2]} <b>{user.full_name}</b> — новый уровень доверия: "
            f"<b>{level[1]}</b>",
            parse_mode="HTML")
    except Exception:
        pass


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_watch(message: Message):
    """Единая точка для группы: франшиза, опыт за беседу, «спасибо»."""
    if not _is_our_group(message):
        return
    user = message.from_user
    if not user or user.is_bot:
        return
    topic = _topic_of(message)

    # ── подтема франшизы: пишут только одобренные
    if TOPIC_FRANCHISE and topic == TOPIC_FRANCHISE:
        m = await get_member(user.id)
        if not m or m["status"] != "approved":
            if user.id not in await _group_admin_ids(message.chat.id):
                try:
                    await message.delete()
                except Exception as e:
                    logger.info("Не смог удалить сообщение во франшизе: %s", e)
                    return
                _notify(
                    user.id,
                    "В подтеме «Франшиза» пишут только участники, прошедшие "
                    "одобрение. Подай заявку в приложении — как только "
                    "ответственный её примет, доступ откроется.",
                    _open_app_kb())
                return

    # ── подтема беседы: опыт
    if not TOPIC_CHAT or topic != TOPIC_CHAT:
        return
    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/"):
        return

    # «спасибо» в ответ на чьё-то сообщение — большой вес, получателю
    reply = message.reply_to_message
    if reply and reply.from_user and THANKS_RE.search(text):
        target = reply.from_user
        if (not target.is_bot and target.id != user.id
                and await _thanks_pair_allowed(user.id, target.id)):
            granted, level = await add_chat_xp(target, XP_PER_THANKS, "thanks")
            if granted:
                try:
                    await message.react([{"type": "emoji", "emoji": "🙏"}])
                except Exception:
                    pass
                if level:
                    await _announce_level(message, target, level)

    # обычное сообщение — маленький вес, автору
    if len(text) >= MSG_MIN_CHARS and await _msg_cooldown_passed(user.id):
        _, level = await add_chat_xp(user, XP_PER_MESSAGE, "msg")
        if level:
            await _announce_level(message, user, level)


WORK_INSTRUCTION = (
    "🚲 <b>Как работать с Бибибайком и получать бибибонусы</b>\n\n"
    "<b>1. Регистрация</b>\n"
    "Открой приложение по кнопке ниже и заполни заявку: имя и телефон. "
    "Ответственный её проверит — обычно в течение дня. Как только одобрит, "
    "во вкладке «Задания» появятся доступные задачи.\n\n"
    "<b>2. Как брать задания</b>\n"
    "Заходишь в «Задания» → выбираешь свободное → «Взять задание». "
    "Оно закрепляется за тобой, другие его уже не заберут. "
    "Бывают персональные задания — они приходят лично тебе в бот.\n\n"
    "<b>3. Как сдавать</b>\n"
    "Выполнил — жми «Готово» и коротко опиши результат. "
    "Ответственный подтверждает, и бибибонусы падают на баланс. "
    "Если что-то не так — задание вернётся на доработку с комментарием.\n\n"
    "<b>4. Бибибонусы</b>\n"
    "Копятся в «Кошельке» и меняются на бонусы приложения Бибибайк. "
    "Минимум для обмена указан там же. Заявку на обмен обрабатывает "
    "ответственный вручную.\n\n"
    "<b>5. Уровень доверия</b>\n"
    "Растёт за подтверждённые задания и за живое участие в беседе. "
    "Чем выше уровень — тем крупнее задания и меньше проверок.\n\n"
    "<b>6. Приглашай друзей</b>\n"
    "В «Профиле» есть личная ссылка. Друг переходит, подписывается на канал — "
    "тебе +1 к прогрессу, а на ступенях начисляются бибибонусы.\n\n"
    "Вопросы — пиши в этой подтеме, ответим."
)


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
