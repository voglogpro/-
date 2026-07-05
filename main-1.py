# -*- coding: utf-8 -*-
# ============================================================
# BibiBike Bot — ФИНАЛЬНАЯ ВЕРСИЯ (обновление поверх рабочей).
#
# СОХРАНЕНО ИЗ ОРИГИНАЛА (логика не менялась):
#   - схема БД и файл bibibike_work.db (старые данные продолжают работать)
#   - парсер parse_message / get_action_type (весь словарь глаголов)
#   - /setname, /status, /help, /fix с ручным переопределением цифр
#   - автоудаление команд и служебных ответов (auto_delete)
#   - пересчёт при редактировании сообщений
#   - work-роутер слушает ВСЕ темы, кроме ОТЧЕТОВ
#
# ДОБАВЛЕНО (помечено "# === НОВОЕ ==="):
#   1. ЖИВОЕ СООБЩЕНИЕ: одна смена = одно сообщение в ОТЧЕТАХ,
#      бот сам редактирует его по мере действий (дебаунс 20 сек),
#      при закрытии дописывает конец смены и отработанные часы.
#   2. Тема NPB: голые 4-значные номера = замена АКБ ("Поменял АКБ").
#   3. Район при открытии смены — ЛЮБОЙ текст (или пусто).
#      Чарджер может писать зону и порог: /20:55 весь город, загрузил 35
#   4. /fix удаляет старое сообщение отчёта и присылает новое
#      (+ необязательное 6-е число — АКБ; без него АКБ сохраняется).
#   5. Роль "Чарджер" ⚡ в /setname (в дополнение к Скауту и Водителю).
#   6. /topicid — узнать ID темы для настройки конфига.
#
# ФИЛОСОФИЯ:
#   - Бот реагирует ТОЛЬКО на сообщения со слешем при управлении сменой.
#     Кто не хочет пользоваться — пишет как раньше, бот не мешает.
#   - Роль — это подпись в отчёте, а не ограничение: считается любое
#     действие любому сотруднику; чего не делал — той графы просто нет.
#
# Формат /fix: /fix перем поправ рем в_СЦ из_СЦ [акб] Комментарий
#   ВАЖНО: если комментарий начинается с числа, оно посчитается как АКБ —
#   в таком случае укажи АКБ явно шестым числом.
# ============================================================

import asyncio
import logging
import re
import os
import sys
import json
import hmac
import hashlib
import aiosqlite
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
# Бот безопасно берет токен из настроек окружения BotHost
BOT_TOKEN = os.getenv("BOT_TOKEN")

GROUP_ID = -1003431950710
CHAT1_THREAD_ID = 1        # Тех. Задания (рабочий)
CHAT2_THREAD_ID = 3        # ОТЧЕТЫ

# === НОВОЕ: тема NPB (замены АКБ). ID узнать командой /topicid в самой теме ===
NPB_THREAD_ID = 2          # <-- ЗАМЕНИ на реальный ID темы NPB!

# === НОВОЕ: живое сообщение обновляется не чаще, чем раз в N секунд ===
DEBOUNCE_SEC = 20

# ============================================================
# === НОВОЕ: МИНИ-ПРИЛОЖЕНИЕ (ЗАРПЛАТА) =====================
# ============================================================
# Порт, который BotHost проксирует на публичный HTTPS-домен бота.
# На большинстве хостингов он приходит в переменной окружения PORT.
WEBAPP_PORT = int(os.getenv("PORT", "8080"))

# Имя бота (без @) и short-name Mini App из BotFather (/newapp) —
# нужны, чтобы под отчётом появилась кнопка «Моя зарплата».
BOT_USERNAME = os.getenv("BOT_USERNAME", "bbbotdelaetbot")
WEBAPP_SHORTNAME = os.getenv("WEBAPP_SHORTNAME", "zp")

# Домен, с которого открывается сама страница мини-приложения (GitHub Pages).
# Нужен для CORS, чтобы браузер разрешил запросы к API бота.
WEBAPP_ALLOW_ORIGIN = os.getenv("WEBAPP_ALLOW_ORIGIN", "https://voglogpro.github.io")

# === НОВОЕ: бот сам отдаёт страницу мини-приложения (index.html рядом с этим файлом) ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

# Краснодар = московское время (UTC+3)
MSK = timezone(timedelta(hours=3))

# Модель оплаты по умолчанию для новых сотрудников
DEFAULT_PAY_TYPE = "hourly"       # hourly | salary | piece
DEFAULT_PAY_AMOUNT = 350.0        # ₽/час, ₽/смену или ₽/замену — зависит от типа

def _webapp_button():
    """Кнопка-ссылка под отчётом, открывающая мини-приложение прямо из группы."""
    if not BOT_USERNAME:
        return None
    url = f"https://t.me/{BOT_USERNAME}/{WEBAPP_SHORTNAME}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💰 Моя зарплата", url=url)]]
    )

# Список районов больше не ограничивает открытие смены (район — любой текст),
# оставлен для истории:
DISTRICTS = ["красная", "фмр", "юмр", "восточка", "ставрополька", "гмр"]

# ИНИЦИАЛИЗАЦИЯ РОУТЕРОВ
work_router = Router()
cmd_router = Router()

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Проверка наличия токена перед запуском
if not BOT_TOKEN:
    logger.error("КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения BOT_TOKEN не задана в панели BotHost!")
    sys.exit(1)

# === НОВОЕ: бот создаётся на уровне модуля, чтобы редактировать живое сообщение из любых функций ===
bot = Bot(token=BOT_TOKEN)

# ============================================================
# БАЗА ДАННЫХ
# ============================================================
DB_PATH = "bibibike_work.db"   # тот же файл — старые данные сохраняются

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                role TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                full_name TEXT,
                role TEXT,
                start_time TEXT,
                end_time TEXT,
                district TEXT,
                comment TEXT,
                is_active INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                shift_id INTEGER,
                message_id INTEGER,
                action_type TEXT,
                bike_codes TEXT,
                quantity INTEGER DEFAULT 0
            )
        """)
        await db.commit()

        # Автоматическая миграция для старых баз данных
        try:
            await db.execute("ALTER TABLE actions ADD COLUMN message_id INTEGER")
            await db.commit()
            logger.info("Миграция: Колонка message_id успешно добавлена в таблицу actions.")
        except aiosqlite.OperationalError:
            pass

        # === НОВОЕ: миграция под живое сообщение — храним id сообщения-отчёта смены ===
        try:
            await db.execute("ALTER TABLE shifts ADD COLUMN report_msg_id INTEGER")
            await db.commit()
            logger.info("Миграция: Колонка report_msg_id успешно добавлена в таблицу shifts.")
        except aiosqlite.OperationalError:
            pass

        # === НОВОЕ: модель оплаты у сотрудника (для мини-приложения) ===
        for ddl in [
            "ALTER TABLE users ADD COLUMN pay_type TEXT DEFAULT 'hourly'",
            "ALTER TABLE users ADD COLUMN pay_amount REAL DEFAULT 350",
        ]:
            try:
                await db.execute(ddl); await db.commit()
            except aiosqlite.OperationalError:
                pass

        # === НОВОЕ: дата смены + замороженный заработок (для истории/зарплаты) ===
        for ddl in [
            "ALTER TABLE shifts ADD COLUMN created_at TEXT",
            "ALTER TABLE shifts ADD COLUMN earned REAL DEFAULT 0",
            "ALTER TABLE shifts ADD COLUMN pay_type_snap TEXT",
            "ALTER TABLE shifts ADD COLUMN pay_amount_snap REAL",
        ]:
            try:
                await db.execute(ddl); await db.commit()
            except aiosqlite.OperationalError:
                pass

    logger.info("БД готова")

async def add_user(uid, name, role):
    # ВАЖНО: не используем INSERT OR REPLACE — иначе стёрлись бы pay_type/pay_amount.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, full_name, role) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, role=excluded.role",
            (uid, name, role)
        )
        await db.commit()

# === НОВОЕ: сохранить модель оплаты (из настроек мини-приложения) ===
async def set_user_pay(uid, pay_type, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, full_name, role, pay_type, pay_amount) "
            "VALUES (?, '', '', ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET pay_type=excluded.pay_type, pay_amount=excluded.pay_amount",
            (uid, pay_type, amount)
        )
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM users WHERE user_id = ?", (uid,))
        r = await c.fetchone()
        return dict(r) if r else None

async def get_active_shift(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM shifts WHERE user_id = ? AND is_active = 1", (uid,))
        r = await c.fetchone()
        return dict(r) if r else None

async def get_last_shift(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND is_active = 0 ORDER BY id DESC LIMIT 1",
            (uid,)
        )
        r = await c.fetchone()
        return dict(r) if r else None

# === НОВОЕ: смена по id (нужно живому сообщению) ===
async def get_shift_by_id(sid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM shifts WHERE id = ?", (sid,))
        r = await c.fetchone()
        return dict(r) if r else None

async def start_shift(uid, name, role, time, district):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE shifts SET is_active = 0 WHERE user_id = ? AND is_active = 1", (uid,))
        # === НОВОЕ: сохраняем дату старта (для истории/зарплаты) ===
        now_iso = datetime.now(MSK).isoformat()
        c = await db.execute(
            "INSERT INTO shifts (user_id, full_name, role, start_time, district, is_active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (uid, name, role, time, district, now_iso)
        )
        await db.commit()
        return c.lastrowid

# === НОВОЕ: расчёт заработка ===
def _worked_min(start_time, end_time):
    sp = start_time.split(':'); ep = end_time.split(':')
    sm = int(sp[0]) * 60 + int(sp[1])
    em = int(ep[0]) * 60 + int(ep[1])
    if em < sm:
        em += 24 * 60
    return em - sm

def compute_earned(pay_type, amount, worked_min, battery_count):
    amount = amount or 0
    if pay_type == "salary":              # оклад за смену — фикс
        return round(amount, 2)
    if pay_type == "piece":               # сделка — за каждую замену АКБ
        return round(amount * (battery_count or 0), 2)
    return round(amount * (worked_min or 0) / 60.0, 2)   # почасовая

async def freeze_earned(sid):
    """Фиксируем сумму на момент закрытия смены — потом ставку можно менять, история не перепишется."""
    shift = await get_shift_by_id(sid)
    if not shift:
        return
    user = await get_user(shift['user_id']) or {}
    pay_type = user.get('pay_type') or DEFAULT_PAY_TYPE
    amount = user.get('pay_amount')
    if amount is None:
        amount = DEFAULT_PAY_AMOUNT
    stats = await get_stats(sid)
    wm = _worked_min(shift['start_time'], shift['end_time']) if shift.get('end_time') else 0
    earned = compute_earned(pay_type, amount, wm, stats.get('battery', 0))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE shifts SET earned = ?, pay_type_snap = ?, pay_amount_snap = ? WHERE id = ?",
            (earned, pay_type, amount, sid)
        )
        await db.commit()

async def end_shift(uid, time, comment=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE shifts SET is_active = 0, end_time = ?, comment = ? WHERE user_id = ? AND is_active = 1",
            (time, comment, uid)
        )
        await db.commit()
        c = await db.execute("SELECT id FROM shifts WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,))
        r = await c.fetchone()
        sid = r[0] if r else None
    # === НОВОЕ: заморозить заработок закрытой смены ===
    if sid:
        await freeze_earned(sid)
    return sid

# === НОВОЕ: запомнить id живого сообщения смены ===
async def set_report_msg_id(sid, mid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE shifts SET report_msg_id = ? WHERE id = ?", (mid, sid))
        await db.commit()

async def add_action(uid, sid, mid, atype, codes=None, qty=0):
    cstr = ",".join(codes) if codes else ""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, sid, mid, atype, cstr, qty)
        )
        await db.commit()

async def delete_actions_by_message(uid, mid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM actions WHERE user_id = ? AND message_id = ?",
            (uid, mid)
        )
        await db.commit()

async def get_stats(sid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute(
            "SELECT action_type, bike_codes, quantity FROM actions WHERE shift_id = ?",
            (sid,)
        )
        rows = await c.fetchall()
        # === НОВОЕ: добавлен счётчик 'battery' (замены АКБ из темы NPB) ===
        s = {'move': 0, 'fix': 0, 'repair': 0, 'to_sc': 0, 'from_sc': 0, 'battery': 0}
        for r in rows:
            atype = r['action_type']
            if atype in s:
                codes = r['bike_codes']
                if codes:
                    s[atype] += len(codes.split(','))
                if r['quantity']:
                    s[atype] += r['quantity']
        return s

# ============================================================
# ПАРСИНГ ТЕКСТА О ТЕКУЩЕЙ РАБОТЕ  (оригинальный, без изменений)
# ============================================================
def parse_message(text):
    text = text.lower().strip()
    all_codes = re.findall(r'\b(\d{4})\b', text)
    lines = text.split('\n')

    repair_codes = []
    for line in lines:
        if any(kw in line for kw in ['ремонт', 'поломк', 'сломан']):
            repair_codes.extend(re.findall(r'\b(\d{4})\b', line))

    keywords_found = []

    for kw in ['привез на сц', 'привёз на сц', 'на сц привез',
               'вывез из сц', 'вывёз из сц', 'из сц вывез', 'вывез с сц',
               'ремонт', 'поломк', 'сломан',
               'переместил', 'перенес', 'перенёс', 'переставил', 'перемещ',
               'поправил', 'выровнял', 'чист', 'поправ',
               'на сц', 'из сц']:
        if kw in text:
            atype = get_action_type(kw)
            if atype and atype not in [a['action_type'] for a in keywords_found]:
                qty = 0
                for line in lines:
                    if kw in line:
                        qty_match = re.search(r'(?<!\d)(\d{1,3})(?!\d)(?![а-яa-z])', line)
                        if qty_match:
                            num = int(qty_match.group(1))
                            if not re.search(r'\b\d{4}\b', line):
                                qty = num
                        break
                keywords_found.append({'action_type': atype, 'quantity': qty})

    if not keywords_found:
        return []

    qty_actions = [kw for kw in keywords_found if kw['quantity'] > 0]
    code_actions = [kw for kw in keywords_found if kw['quantity'] == 0]
    results = []

    for kw in qty_actions:
        results.append({'action_type': kw['action_type'], 'bike_codes': [], 'quantity': kw['quantity']})

    for kw in code_actions:
        if kw['action_type'] == 'repair':
            codes = repair_codes.copy() if repair_codes else []
        else:
            codes = all_codes.copy() if all_codes else []
        results.append({'action_type': kw['action_type'], 'bike_codes': codes, 'quantity': 0})

    return results


def get_action_type(kw):
    if kw in ['привез на сц', 'привёз на сц', 'на сц привез', 'на сц']:
        return 'to_sc'
    if kw in ['вывез из сц', 'вывёз из сц', 'из сц вывез', 'вывез с сц', 'из сц']:
        return 'from_sc'
    if kw in ['ремонт', 'поломк', 'сломан']:
        return 'repair'
    if kw in ['переместил', 'перенес', 'перенёс', 'переставил', 'перемещ']:
        return 'move'
    if kw in ['поправил', 'выровнял', 'чист', 'поправ']:
        return 'fix'
    return None

# === НОВОЕ: парсер темы NPB — голые 4-значные номера = замены АКБ ===
def parse_npb_message(text):
    codes = re.findall(r'\b(\d{4})\b', text)
    if not codes:
        return []
    return [{'action_type': 'battery', 'bike_codes': codes, 'quantity': 0}]

# ============================================================
# ФУНКЦИЯ АВТОУДАЛЕНИЯ КОМАНД  (оригинальная)
# ============================================================
async def auto_delete(msg: Message, delay: int = 60):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass

# ============================================================
# === НОВОЕ: ЖИВОЕ СООБЩЕНИЕ СМЕНЫ =========================
# ============================================================
_pending_updates = {}   # shift_id -> asyncio.Task (дебаунс)

def _role_text(role):
    role_emoji = ""
    if role == "Скаут":
        role_emoji = " 🚶"
    elif role == "Водитель":
        role_emoji = " 🚚"
    elif role == "Чарджер":       # === НОВОЕ: роль чарджера ===
        role_emoji = " ⚡"
    return f" | {role}{role_emoji}" if role else ""

def _duration(start_time, end_time):
    sp = start_time.split(':')
    ep = end_time.split(':')
    sm = int(sp[0]) * 60 + int(sp[1])
    em = int(ep[0]) * 60 + int(ep[1])
    if em < sm:
        em += 24 * 60
    diff = em - sm
    return f"{diff // 60} ч. {diff % 60} мин."

def build_report_text(shift, stats):
    """Формат сохранён из оригинального отчёта + строка АКБ."""
    full_name = shift.get('full_name') or "Сотрудник"
    report = f"<b>{full_name}</b>{_role_text(shift.get('role'))}\n"
    report += f"Начал: {shift['start_time']}\n"

    closed = not shift.get('is_active') and shift.get('end_time')
    if closed:
        report += f"Закончил: {shift['end_time']}\n"
        report += f"Отработано: {_duration(shift['start_time'], shift['end_time'])}\n"

    if shift.get('district'):
        report += f"Район: {shift['district'].upper()}\n"

    report += "\nСтатистика за смену:\n"

    has_any = False
    if stats['move'] > 0:
        report += f"Перемещено: {stats['move']}\n"; has_any = True
    if stats['fix'] > 0:
        report += f"Поправлено: {stats['fix']}\n"; has_any = True
    if stats['repair'] > 0:
        report += f"Ремонт: {stats['repair']}\n"; has_any = True
    if stats['battery'] > 0:
        report += f"Поменял АКБ: {stats['battery']}\n"; has_any = True
    if stats['to_sc'] > 0:
        report += f"Привез на СЦ: {stats['to_sc']}\n"; has_any = True
    if stats['from_sc'] > 0:
        report += f"Вывез из СЦ: {stats['from_sc']}\n"; has_any = True
    if not has_any:
        report += "— пока нет действий\n"

    if closed and shift.get('comment'):
        report += f"\nКомментарий: {shift['comment']}"

    return report

async def update_report_message(shift_id, force_new=False):
    """Отредактировать живое сообщение смены (или пересоздать при /fix)."""
    shift = await get_shift_by_id(shift_id)
    if not shift:
        return
    stats = await get_stats(shift_id)
    text = build_report_text(shift, stats)
    msg_id = shift.get('report_msg_id')
    markup = _webapp_button()   # === НОВОЕ: кнопка «Моя зарплата» ===

    if force_new and msg_id:
        try:
            await bot.delete_message(GROUP_ID, msg_id)
        except TelegramBadRequest:
            pass
        msg_id = None

    if msg_id:
        try:
            await bot.edit_message_text(
                text, chat_id=GROUP_ID, message_id=msg_id,
                parse_mode="HTML", reply_markup=markup
            )
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return
            # сообщение удалили вручную — пришлём новое ниже

    msg = await bot.send_message(
        GROUP_ID, text, message_thread_id=CHAT2_THREAD_ID,
        parse_mode="HTML", reply_markup=markup
    )
    await set_report_msg_id(shift_id, msg.message_id)

def schedule_report_update(shift_id):
    """Дебаунс: не редактируем чаще, чем раз в DEBOUNCE_SEC (защита от флуд-лимита)."""
    task = _pending_updates.get(shift_id)
    if task and not task.done():
        return

    async def _later():
        await asyncio.sleep(DEBOUNCE_SEC)
        _pending_updates.pop(shift_id, None)
        try:
            await update_report_message(shift_id)
        except Exception as e:
            logger.error(f"Не удалось обновить отчёт смены {shift_id}: {e}")

    _pending_updates[shift_id] = asyncio.create_task(_later())

async def flush_report_update(shift_id, force_new=False):
    """Немедленное обновление (открытие/закрытие смены, /fix) — отменяем дебаунс."""
    task = _pending_updates.pop(shift_id, None)
    if task and not task.done():
        task.cancel()
    await update_report_message(shift_id, force_new=force_new)

# ============================================================
# ОБРАБОТКА РАБОЧЕГО СООБЩЕНИЯ  (оригинальная + триггер живого отчёта)
# ============================================================
async def process_work_message(message: Message, npb=False):
    text = message.text or message.caption or ""
    if not text:
        return
    if text.startswith('/'):
        return
    if re.match(r'^\d{1,2}:\d{2}\s*', text):
        return

    shift = await get_active_shift(message.from_user.id)
    if not shift:
        return

    await delete_actions_by_message(message.from_user.id, message.message_id)

    # === НОВОЕ: в теме NPB считаем голые номера как замены АКБ ===
    actions = parse_npb_message(text) if npb else parse_message(text)
    logger.info(f"Распаршено (msg={message.message_id}, npb={npb}): {actions}")

    for action in actions:
        await add_action(
            message.from_user.id,
            shift['id'],
            message.message_id,
            action['action_type'],
            action.get('bike_codes', []),
            action.get('quantity', 0)
        )
        logger.info(f"Записано: {shift['full_name']} — {action}")

    # === НОВОЕ: обновляем живое сообщение (с дебаунсом) ===
    if actions:
        schedule_report_update(shift['id'])

# ============================================================
# ЧАТ 1 (и остальные темы, кроме ОТЧЕТОВ) — НОВЫЕ СООБЩЕНИЯ
# ============================================================
@work_router.message(F.chat.id == GROUP_ID)
async def work_chat(message: Message):
    if message.message_thread_id == CHAT2_THREAD_ID:
        return

    # === НОВОЕ: /topicid — узнать ID темы (для настройки конфига) ===
    if (message.text or "") == "/topicid":
        msg = await message.answer(
            f"chat_id: {message.chat.id}\nmessage_thread_id: {message.message_thread_id}"
        )
        asyncio.create_task(auto_delete(msg))
        return

    # === НОВОЕ: тема NPB обрабатывается своим парсером ===
    npb = (message.message_thread_id == NPB_THREAD_ID)
    await process_work_message(message, npb=npb)

# ============================================================
# РЕДАКТИРОВАННЫЕ РАБОЧИЕ СООБЩЕНИЯ  (оригинал + NPB)
# ============================================================
@work_router.edited_message(F.chat.id == GROUP_ID)
async def work_chat_edit(message: Message):
    if message.message_thread_id == CHAT2_THREAD_ID:
        return
    logger.info(f"СООБЩЕНИЕ ОТРЕДАКТИРОВАНО: {message.message_id}")
    npb = (message.message_thread_id == NPB_THREAD_ID)
    await process_work_message(message, npb=npb)

# ============================================================
# ЧАТ 2 — УПРАВЛЕНИЕ СМЕНАМИ И ОТЧЕТАМИ
# ============================================================
@cmd_router.message(F.chat.id == GROUP_ID, F.message_thread_id == CHAT2_THREAD_ID)
async def cmd_chat(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    full_name = user['full_name'] if user else message.from_user.full_name
    role = user['role'] if user else ""
    text = (message.text or message.caption or "").strip()

    # Игнорируем РУЧНЫЕ отчёты старого образца со словом "чарджер".
    # === ИСПРАВЛЕНО: раньше это блокировало и команду /setname ... чарджер ===
    if "чарджер" in text.lower() and not text.startswith('/'):
        return

    # /help
    if text == "/help":
        try:
            await message.delete()
        except:
            pass
        msg = await message.answer(
            "BibiBike - команды:\n\n"
            "Начать смену (район — любое слово или без него):\n"
            "/09:00 фмр\n/09:00 весь город, загрузил 35\n/09:00\n\n"
            "Закончить смену:\n/18:00\n/18:00 Комментарий\n\n"
            "Установить имя и роль:\n/setname Фамилия И.О. скаут\n"
            "(роли: скаут, водитель, чарджер)\n\n"
            "Исправить последний отчёт (5 цифр: перем. поправ. рем. в_СЦ из_СЦ, "
            "6-я необязательная — АКБ):\n"
            "/fix 11 5 1 2 0 Комментарий\n"
            "/fix 11 5 1 2 0 40 Комментарий\n\n"
            "Статус: /status\n"
            "ID темы: /topicid"
        )
        asyncio.create_task(auto_delete(msg))
        return

    # === НОВОЕ: /topicid и в теме отчётов ===
    if text == "/topicid":
        try:
            await message.delete()
        except:
            pass
        msg = await message.answer(
            f"chat_id: {message.chat.id}\nmessage_thread_id: {message.message_thread_id}"
        )
        asyncio.create_task(auto_delete(msg))
        return

    # /status  (оригинальный)
    if text == "/status":
        try:
            await message.delete()
        except:
            pass
        shift = await get_active_shift(user_id)
        if shift:
            msg = await message.answer(
                f"{full_name}{_role_text(shift.get('role'))}\n"
                f"Активная смена с {shift['start_time']}\n"
                + (f"Район: {shift['district'].upper()}" if shift.get('district') else "")
            )
        else:
            msg = await message.answer("Нет активной смены.")
        asyncio.create_task(auto_delete(msg))
        return

    # /fix [move] [fix] [repair] [to_sc] [from_sc] [battery] Комментарий
    if text.startswith("/fix"):
        try:
            await message.delete()
        except:
            pass

        shift = await get_active_shift(user_id)
        if shift:
            msg = await message.answer("У вас активная смена. Завершите её сначала.")
            asyncio.create_task(auto_delete(msg))
            return

        last_shift = await get_last_shift(user_id)
        if not last_shift:
            msg = await message.answer("Нет завершённых смен.")
            asyncio.create_task(auto_delete(msg))
            return

        parts = text.split(maxsplit=1)
        args = parts[1].split() if len(parts) > 1 else []

        try:
            new_move = int(args[0]) if len(args) > 0 else 0
            new_fix = int(args[1]) if len(args) > 1 else 0
            new_repair = int(args[2]) if len(args) > 2 else 0
            new_to_sc = int(args[3]) if len(args) > 3 else 0
            new_from_sc = int(args[4]) if len(args) > 4 else 0
        except ValueError:
            msg = await message.answer("Ошибка: первые 5 аргументов должны быть числами.\nПример: /fix 11 5 1 2 0 Комментарий")
            asyncio.create_task(auto_delete(msg))
            return

        # === НОВОЕ: необязательное 6-е число — АКБ; без него АКБ сохраняется как было ===
        old_stats = await get_stats(last_shift['id'])
        if len(args) > 5 and args[5].isdigit():
            new_battery = int(args[5])
            new_comment = " ".join(args[6:]) if len(args) > 6 else last_shift.get('comment', '')
        else:
            new_battery = old_stats['battery']
            new_comment = " ".join(args[5:]) if len(args) > 5 else last_shift.get('comment', '')

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM actions WHERE shift_id = ?", (last_shift['id'],))

            if new_move > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, 0, 'move', '', ?)",
                    (user_id, last_shift['id'], new_move)
                )
            if new_fix > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, 0, 'fix', '', ?)",
                    (user_id, last_shift['id'], new_fix)
                )
            if new_repair > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, 0, 'repair', '', ?)",
                    (user_id, last_shift['id'], new_repair)
                )
            if new_to_sc > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, 0, 'to_sc', '', ?)",
                    (user_id, last_shift['id'], new_to_sc)
                )
            if new_from_sc > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, 0, 'from_sc', '', ?)",
                    (user_id, last_shift['id'], new_from_sc)
                )
            # === НОВОЕ: перезаписываем АКБ ===
            if new_battery > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, 0, 'battery', '', ?)",
                    (user_id, last_shift['id'], new_battery)
                )

            await db.execute("UPDATE shifts SET comment = ? WHERE id = ?", (new_comment, last_shift['id']))
            await db.commit()

        # === НОВОЕ: /fix удаляет старое сообщение отчёта и присылает новое ===
        await flush_report_update(last_shift['id'], force_new=True)
        logger.info(f"Отчёт полностью пересчитан: {full_name}")
        return

    # /setname ...  (оригинальный + роль Чарджер)
    if text.startswith("/setname"):
        try:
            await message.delete()
        except:
            pass
        parts = text.split(maxsplit=1)
        if len(parts) >= 2:
            args = parts[1].strip().split()
            if len(args) >= 2:
                new_role = args[-1].lower()
                if new_role in ["скаут", "scout"]:
                    new_role = "Скаут"
                elif new_role in ["водитель", "driver", "вод"]:
                    new_role = "Водитель"
                elif new_role in ["чарджер", "charger", "чардж"]:   # === НОВОЕ ===
                    new_role = "Чарджер"
                else:
                    msg = await message.answer("Укажите роль: скаут, водитель или чарджер\nПример: /setname Иванов И.И. чарджер")
                    asyncio.create_task(auto_delete(msg))
                    return
                new_name = " ".join(args[:-1])
                await add_user(user_id, new_name, new_role)
                msg = await message.answer(f"Сохранено: {new_name} | {new_role}")
            else:
                msg = await message.answer("Формат: /setname Фамилия И.О. роль\nПример: /setname Иванов И.И. скаут")
        else:
            msg = await message.answer("Формат: /setname Фамилия И.О. роль\nПример: /setname Иванов И.И. скаут")
        asyncio.create_task(auto_delete(msg))
        return

    # Обработка команд времени (Начало / Конец смены)
    # Бот реагирует ТОЛЬКО на слеш — кто не хочет пользоваться,
    # пишет отчёты вручную как раньше, бот его не трогает.
    if not text.startswith('/'):
        return

    text = text[1:]
    active_shift = await get_active_shift(user_id)
    time_match = re.match(r'(\d{1,2}:\d{2})\s*(.*)', text)

    if time_match:
        try:
            await message.delete()
        except:
            pass

        time_str = time_match.group(1)
        extra = time_match.group(2).strip()

        if not active_shift:
            # НАЧАЛО СМЕНЫ
            # === НОВОЕ: район/зона — любой текст целиком (или пусто).
            # Чарджер может указать зону и порог: /20:55 весь город, загрузил 35 ===
            district = extra.lower()
            role_for_shift = role if role else ""
            sid = await start_shift(user_id, full_name, role_for_shift, time_str, district)

            # === НОВОЕ: создаётся ЖИВОЕ сообщение, бот редактирует его всю смену ===
            await flush_report_update(sid)
            logger.info(f"Смена начата: {full_name}, {time_str}, {district or '—'}")
            return

        else:
            # КОНЕЦ СМЕНЫ
            comment = extra if extra else ""
            sid = await end_shift(user_id, time_str, comment)
            if not sid:
                msg = await message.answer("Ошибка завершения смены.")
                asyncio.create_task(auto_delete(msg))
                return

            # === НОВОЕ: финальная правка живого сообщения ===
            await flush_report_update(sid)
            logger.info(f"Смена завершена: {full_name}")
            return

    return

# ============================================================
# === НОВОЕ: HTTP API ДЛЯ МИНИ-ПРИЛОЖЕНИЯ ===================
# ============================================================
LEVEL_TITLES = [
    (1, "Новичок"), (3, "Курьер зон"), (5, "Гонщик зон"),
    (8, "Мастер смены"), (12, "Легенда парка"),
]

def _title_for_level(lvl):
    title = LEVEL_TITLES[0][1]
    for need, name in LEVEL_TITLES:
        if lvl >= need:
            title = name
    return title

def _level_from_xp(total):
    lvl, need, rem = 1, 100, total
    while rem >= need:
        rem -= need; lvl += 1; need = lvl * 100
    return lvl, rem, need

async def get_lifetime(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT bike_codes, quantity FROM actions WHERE user_id = ?", (uid,))
        total = 0
        for r in await c.fetchall():
            if r['bike_codes']:
                total += len(r['bike_codes'].split(','))
            if r['quantity']:
                total += r['quantity']
        c2 = await db.execute(
            "SELECT COALESCE(SUM(earned), 0) FROM shifts WHERE user_id = ? AND is_active = 0", (uid,)
        )
        total_earned = (await c2.fetchone())[0]
        return total, total_earned

async def get_history(uid, limit=90):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND is_active = 0 ORDER BY id DESC LIMIT ?",
            (uid, limit)
        )
        return [dict(r) for r in await c.fetchall()]

def _fmt_date(created_at):
    if not created_at:
        return "—"
    try:
        return datetime.fromisoformat(created_at).strftime("%d.%m.%Y")
    except Exception:
        return "—"

def _check_webapp_auth(init_data: str):
    """Проверяем подпись Telegram initData — так мы точно знаем, кто открыл приложение."""
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
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None

def _get_init_data(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("tma "):
        return auth[4:]
    return request.headers.get("X-Init-Data", "") or request.query.get("_auth", "")

async def _auth_user(request):
    tg_user = _check_webapp_auth(_get_init_data(request))
    if not tg_user or "id" not in tg_user:
        return None
    return tg_user

@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = WEBAPP_ALLOW_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Init-Data"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

async def api_state(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    user = await get_user(uid)
    pay_type = (user or {}).get("pay_type") or DEFAULT_PAY_TYPE
    pay_amount = (user or {}).get("pay_amount")
    if pay_amount is None:
        pay_amount = DEFAULT_PAY_AMOUNT
    name = (user or {}).get("full_name") or ""
    role = (user or {}).get("role") or ""

    total, total_earned = await get_lifetime(uid)
    lvl, xp, need = _level_from_xp(total)

    shift = await get_active_shift(uid)
    shift_data = None
    if shift:
        stats = await get_stats(shift["id"])
        shift_data = {
            "start_time": shift["start_time"],
            "district": (shift.get("district") or "").upper(),
            "stats": stats,
            "server_now": datetime.now(MSK).strftime("%H:%M"),
        }

    last = await get_last_shift(uid)
    last_data = None
    if last:
        last_data = {
            "date": _fmt_date(last.get("created_at")),
            "earned": last.get("earned") or 0,
            "worked": _duration(last["start_time"], last["end_time"]) if last.get("end_time") else "—",
        }

    return web.json_response({
        "user": {
            "id": uid,
            "name": name or tg_user.get("first_name", ""),
            "role": role,
            "pay_type": pay_type,
            "pay_amount": pay_amount,
        },
        "registered": bool(user and name),
        "active": bool(shift),
        "shift": shift_data,
        "last": last_data,
        "level": {"level": lvl, "xp": xp, "need": need, "title": _title_for_level(lvl)},
        "total_earned": total_earned,
    })

async def api_settings(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    try:
        body = await request.json()
    except Exception:
        body = {}

    pay_type = body.get("pay_type", DEFAULT_PAY_TYPE)
    if pay_type not in ("hourly", "salary", "piece"):
        return web.json_response({"error": "pay_type"}, status=400)
    try:
        pay_amount = float(body.get("pay_amount", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "pay_amount"}, status=400)
    if pay_amount < 0:
        pay_amount = 0.0

    await set_user_pay(uid, pay_type, pay_amount)

    # Имя и роль — необязательно (можно зарегистрироваться прямо в приложении)
    name = (body.get("name") or "").strip()
    role = (body.get("role") or "").strip().lower()
    role_map = {"скаут": "Скаут", "водитель": "Водитель", "чарджер": "Чарджер"}
    if name and role in role_map:
        await add_user(uid, name, role_map[role])

    return web.json_response({"ok": True})

async def api_history(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    rows = await get_history(uid)
    items = []
    for s in rows:
        worked = _duration(s["start_time"], s["end_time"]) if s.get("end_time") else "—"
        items.append({
            "date": _fmt_date(s.get("created_at")),
            "start": s.get("start_time"),
            "end": s.get("end_time"),
            "worked": worked,
            "earned": s.get("earned") or 0,
            "pay_type": s.get("pay_type_snap") or "hourly",
            "district": (s.get("district") or "").upper(),
        })
    return web.json_response({"items": items})

async def serve_index(request):
    # Отдаём саму страницу мини-приложения с того же адреса, что и API —
    # тогда не нужен ни GitHub Pages, ни CORS.
    if os.path.exists(INDEX_PATH):
        return web.FileResponse(INDEX_PATH)
    return web.Response(text="BibiBike API ok")

async def start_api_server():
    try:
        app = web.Application(middlewares=[cors_mw])
        app.router.add_get("/api/state", api_state)
        app.router.add_post("/api/settings", api_settings)
        app.router.add_get("/api/history", api_history)
        app.router.add_get("/", serve_index)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT)
        await site.start()
        logger.info(f"API мини-приложения слушает 0.0.0.0:{WEBAPP_PORT}")
    except Exception as e:
        # Веб-сервер не критичен для работы бота: если порт занят/закрыт —
        # просто пишем предупреждение, а бот продолжает работать как раньше.
        logger.warning(f"API мини-приложения не запустился ({e}). Бот работает без него.")

# ============================================================
# ЗАПУСК БОТА
# ============================================================
async def main():
    await init_db()
    await start_api_server()   # === НОВОЕ: поднимаем API рядом с ботом ===
    dp = Dispatcher()
    dp.include_router(cmd_router)
    dp.include_router(work_router)

    logger.info("=" * 50)
    logger.info("BibiBike Bot запущен! (живое сообщение + NPB + роль Чарджер)")
    logger.info(f"Группа: {GROUP_ID}")
    logger.info(f"Чат 1 (рабочий): тред {CHAT1_THREAD_ID}")
    logger.info(f"Чат 2 (отчеты): тред {CHAT2_THREAD_ID}")
    logger.info(f"NPB (замены АКБ): тред {NPB_THREAD_ID}")
    logger.info("=" * 50)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
