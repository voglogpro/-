# -*- coding: utf-8 -*-
# ============================================================
# БибиЗадачи — бот и мини-приложение для заданий и бибибонусов.
# Отдельный бот: НЕ трекер смен. Здесь пользователи из группы
# регистрируются, берут задания по городу и адресу, выполняют и получают
# бибибонусы (внутренняя валюта на бесплатные поездки).
#
# Дизайн и концепт взяты из рабочего трекера смен, механика — новая.
# Стек тот же: Aiogram 3 + aiohttp + SQLite. Данные — в DATA_DIR.
# ============================================================
import asyncio
import base64
import binascii
import contextvars
import hashlib
import hmac
import html
import ipaddress
import io
import json
import logging
import math
import os
import re
import signal
import sqlite3
import sys
import secrets
import time
import unicodedata
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl, urlparse

import aiosqlite
from aiohttp import web
from dotenv import load_dotenv
from PIL import Image, ImageOps, UnidentifiedImageError
from cryptography.fernet import Fernet, InvalidToken
from scripts.recovery_key_canary import ensure_recovery_key_canary
from pydantic import ValidationError
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    CallbackQuery, BufferedInputFile, ChatJoinRequest, ChatMemberUpdated, Update,
)

APP_VERSION = "v2.10.0"
BUILD_VERSION = "2026-07-28 · БибиЗадачи v2.10.0 (release gate)"
SQLITE_SCHEMA_VERSION = 300
PUBLICATION_CLEANUP_MAX_ATTEMPTS = 10

# Local development follows the documented `.env` workflow. Existing process
# environment variables keep precedence, as required in containers.
load_dotenv(override=False)

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


def _bounded_int_env(name, default, minimum, maximum):
    """Read a capacity setting without silently accepting an unsafe value."""
    raw = str(os.getenv(name, str(default))).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _clean_username(raw):
    """Принимает 'bbbikefan', '@bbbikefan' или 'https://t.me/bbbikefan'."""
    value = (raw or "").strip()
    if not value:
        return ""
    value = value.split("?")[0].rstrip("/")
    if "t.me/" in value:
        value = value.split("t.me/")[-1]
    return value.lstrip("@").split("/")[0]


def _truthy_env(name, default=""):
    return str(os.getenv(name, default) or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _join_request_url(raw):
    value = str(raw or "").strip()
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https" or parsed.hostname not in {"t.me", "telegram.me"}
        or port not in (None, 443) or parsed.username or parsed.password
        or parsed.query or parsed.fragment
        or not re.fullmatch(r"/\+[A-Za-z0-9_-]{16,128}", parsed.path)
    ):
        return None
    return f"https://t.me{parsed.path}"


# Ник бота для непрозрачных реферальных ссылок: https://t.me/<BOT_USERNAME>?start=rf_<token>
# ВНИМАНИЕ: bbbikefan — это ГРУППА, а не бот. Диплинки понимают только боты,
# поэтому реферальные ссылки идут на BbGalterbot и никогда не содержат Telegram ID.
BOT_USERNAME = _clean_username(os.getenv("BOT_USERNAME", "BbGalterbot"))

# Канал или группа, подписка на которую засчитывает реферала.
# Можно указать @username или числовой id вида -1001234567890.
# ВАЖНО: бот должен быть администратором этого чата, иначе Telegram
# не даст проверить подписку и вернёт ошибку.
REQUIRED_CHAT = (os.getenv("REQUIRED_CHAT", "@bbbikefan") or "").strip()
JOIN_REQUEST_ADMISSION_ENABLED = _truthy_env(
    "JOIN_REQUEST_ADMISSION_ENABLED", "false",
)
JOIN_REQUEST_INVITE_URL_RAW = (
    os.getenv("JOIN_REQUEST_INVITE_URL", "") or ""
).strip()
JOIN_REQUEST_INVITE_URL = _join_request_url(JOIN_REQUEST_INVITE_URL_RAW)
JOIN_REQUEST_APPLICATION_SLA_HOURS = _bounded_int_env(
    "JOIN_REQUEST_APPLICATION_SLA_HOURS", 72, 1, 720,
)

# ── Группа и её подтемы ───────────────────────────────────────
# Ссылка вида https://t.me/bbbikefan/3 — это id подтемы (message_thread_id).
GROUP_USERNAME = _clean_username(os.getenv("GROUP_USERNAME", "bbbikefan"))
GROUP_ID = _as_int_env("GROUP_ID")          # опционально, если группа закрытая
TOPIC_NEWS = _as_int_env("TOPIC_NEWS", 1)          # новости
TOPIC_CHAT = _as_int_env("TOPIC_CHAT", 3)          # беседа: за неё капает опыт
TOPIC_WORK = _as_int_env("TOPIC_WORK", 4)          # работа: сюда инструкция
TOPIC_FRANCHISE = _as_int_env("TOPIC_FRANCHISE", 43)  # франшиза: только одобренные
TOPIC_CONFIG_EXPLICIT = {
    name: bool(str(os.getenv(name, "")).strip())
    for name in ("TOPIC_NEWS", "TOPIC_CHAT", "TOPIC_WORK", "TOPIC_FRANCHISE")
}

# Приватная рабочая supergroup. Точные адреса и фотографии заданий никогда не
# публикуются в публичный community chat.
OPS_GROUP_USERNAME = _clean_username(os.getenv("OPS_GROUP_USERNAME", ""))
OPS_GROUP_ID = _as_int_env("OPS_GROUP_ID")
OPS_TOPIC_TASKS = _as_int_env("OPS_TOPIC_TASKS", 1)
TOPIC_CONFIG_EXPLICIT["OPS_TOPIC_TASKS"] = bool(
    str(os.getenv("OPS_TOPIC_TASKS", "")).strip()
)

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
    if JOIN_REQUEST_ADMISSION_ENABLED:
        return JOIN_REQUEST_INVITE_URL
    explicit = (os.getenv("REQUIRED_CHAT_URL", "") or "").strip()
    if explicit:
        return _safe_https_url(explicit)
    chat = _required_chat_id()
    if isinstance(chat, str) and chat.startswith("@"):
        return f"https://t.me/{chat[1:]}"
    return None
# Короткое имя Mini App: https://t.me/BbGalterbot/bibibike
WEBAPP_SHORTNAME = os.getenv("WEBAPP_SHORTNAME", "bibibike")
WEBAPP_PORT = int(os.getenv("PORT") or os.getenv("WEB_PORT") or 3000)
TELEGRAM_UPDATE_MODE = (os.getenv("TELEGRAM_UPDATE_MODE", "polling") or "polling").strip().lower()
BIBITASKS_ENVIRONMENT = (
    os.getenv("BIBITASKS_ENVIRONMENT", "production") or "production"
).strip().lower()
PILOT_LOAD_TEST_ENABLED = _truthy_env("PILOT_LOAD_TEST_ENABLED", "false")
PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED = _truthy_env(
    "PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED", "false"
)
PRIVACY_URL_RAW = (os.getenv("PRIVACY_URL", "") or "").strip()
PRIVACY_CONTROLLER_NAME_RAW = os.getenv("PRIVACY_CONTROLLER_NAME", "") or ""
PRIVACY_CONTACT_RAW = os.getenv("PRIVACY_CONTACT", "") or ""
PRIVACY_CONTROLLER_NAME = " ".join(PRIVACY_CONTROLLER_NAME_RAW.split())[:160]
PRIVACY_CONTACT = " ".join(PRIVACY_CONTACT_RAW.split())[:160]


def _safe_https_url(raw):
    """Return a public HTTPS URL, never credentials or a malformed link."""
    value = str(raw or "").strip()
    if not value or any(character in value for character in "\r\n\t"):
        return None
    try:
        parsed = urlparse(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    try:
        public_host = ipaddress.ip_address(hostname).is_global
    except ValueError:
        public_host = bool(
            "." in hostname
            and hostname != "localhost"
            and not hostname.endswith((".localhost", ".local"))
        )
    if (
        parsed.scheme != "https" or not parsed.hostname
        or parsed.username or parsed.password
        or port not in (None, 443) or not public_host
    ):
        return None
    return value


PRIVACY_URL = _safe_https_url(PRIVACY_URL_RAW)
EVIDENCE_RETENTION_DAYS = _bounded_int_env(
    "EVIDENCE_RETENTION_DAYS", 90, 30, 365,
)
DISPUTE_OPEN_DAYS = _bounded_int_env(
    "DISPUTE_OPEN_DAYS", 30, 7, 90,
)
TELEGRAM_RETRY_BASE_SECONDS = max(
    1, min(60, _as_int_env("TELEGRAM_RETRY_BASE_SECONDS", 2) or 2)
)
TELEGRAM_RETRY_MAX_SECONDS = max(
    TELEGRAM_RETRY_BASE_SECONDS,
    min(3600, _as_int_env("TELEGRAM_RETRY_MAX_SECONDS", 3600) or 3600),
)
TELEGRAM_RETRY_MAX_ATTEMPTS = max(
    2, min(10, _as_int_env("TELEGRAM_RETRY_MAX_ATTEMPTS", 10) or 10)
)
TELEGRAM_HANDLER_TIMEOUT_SEC = _bounded_int_env(
    "TELEGRAM_HANDLER_TIMEOUT_SEC", 120, 10, 300,
)
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
WEBHOOK_ROUTE_ID = (os.getenv("WEBHOOK_ROUTE_ID", "") or "").strip()
WEBHOOK_PATH = f"/telegram/webhook/{WEBHOOK_ROUTE_ID}" if WEBHOOK_ROUTE_ID else ""
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET", "") or "").strip()
TELEGRAM_INBOX_KEY = (os.getenv("TELEGRAM_INBOX_KEY", "") or "").strip()
HEALTH_TOKEN = (os.getenv("HEALTH_TOKEN", "") or "").strip()
WEBHOOK_MAX_CONNECTIONS = _as_int_env("WEBHOOK_MAX_CONNECTIONS", 8)
try:
    TELEGRAM_INBOX_FERNET = Fernet(TELEGRAM_INBOX_KEY.encode("ascii"))
except (ValueError, TypeError, UnicodeError):
    TELEGRAM_INBOX_FERNET = None
INIT_DATA_MAX_AGE_SEC = int(os.getenv("INIT_DATA_MAX_AGE_SEC", "900"))
PHOTO_URL_TTL_SEC = max(60, int(os.getenv("PHOTO_URL_TTL_SEC", "900")))
MEDIA_STORAGE = (os.getenv("MEDIA_STORAGE", "local") or "local").strip().lower()
MEDIA_SIGNING_KEY = (os.getenv("MEDIA_SIGNING_KEY", "") or "").strip()
S3_BUCKET = (os.getenv("S3_BUCKET", "") or "").strip()
S3_PREFIX = (os.getenv("S3_PREFIX", "bibitasks") or "bibitasks").strip("/")
S3_REGION = (os.getenv("S3_REGION", "us-east-1") or "us-east-1").strip()
S3_ENDPOINT_URL = (os.getenv("S3_ENDPOINT_URL", "") or "").strip().rstrip("/")
S3_PUBLIC_ENDPOINT_URL = (
    os.getenv("S3_PUBLIC_ENDPOINT_URL", "") or S3_ENDPOINT_URL
).strip().rstrip("/")
S3_ADDRESSING_STYLE = (
    os.getenv("S3_ADDRESSING_STYLE", "auto") or "auto"
).strip().lower()
S3_SSE = (os.getenv("S3_SSE", "AES256") or "").strip()
S3_PRIVACY_MODE = (
    os.getenv("S3_PRIVACY_MODE", "public_access_block") or "public_access_block"
).strip().lower()
S3_PRIVATE_BUCKET_CONFIRMED = (
    os.getenv("S3_PRIVATE_BUCKET_CONFIRMED", "") or ""
).strip().lower() in ("1", "true", "yes")
_s3_clients = {}
API_READS_PER_MIN = max(30, int(os.getenv("API_READS_PER_MIN", "120")))
API_WRITES_PER_MIN = max(10, int(os.getenv("API_WRITES_PER_MIN", "40")))
API_READ_INFLIGHT_MAX = _bounded_int_env(
    "API_READ_INFLIGHT_MAX", 32, 4, 256,
)
API_WRITE_INFLIGHT_MAX = _bounded_int_env(
    "API_WRITE_INFLIGHT_MAX", 16, 2, 128,
)
API_HEAVY_INFLIGHT_MAX = _bounded_int_env(
    "API_HEAVY_INFLIGHT_MAX", 4, 1, 4,
)
MEDIA_NORMALIZE_CONCURRENCY = _bounded_int_env(
    "MEDIA_NORMALIZE_CONCURRENCY", 1, 1, 4,
)
MEDIA_NORMALIZE_MAX_WAITERS = _bounded_int_env(
    "MEDIA_NORMALIZE_MAX_WAITERS", 3, 0, 16,
)
MEDIA_NORMALIZE_WAIT_TIMEOUT_SEC = _bounded_int_env(
    "MEDIA_NORMALIZE_WAIT_TIMEOUT_SEC", 5, 1, 30,
)
TELEGRAM_INBOX_SOFT_LIMIT = _bounded_int_env(
    "TELEGRAM_INBOX_SOFT_LIMIT", 100, 10, 10_000,
)
TELEGRAM_INBOX_HARD_LIMIT = _bounded_int_env(
    "TELEGRAM_INBOX_HARD_LIMIT", 500, 20, 50_000,
)
TELEGRAM_OUTBOX_SOFT_LIMIT = _bounded_int_env(
    "TELEGRAM_OUTBOX_SOFT_LIMIT", 100, 10, 10_000,
)
TELEGRAM_QUEUE_OLDEST_SOFT_SEC = _bounded_int_env(
    "TELEGRAM_QUEUE_OLDEST_SOFT_SEC", 300, 30, 3_600,
)
if TELEGRAM_INBOX_HARD_LIMIT <= TELEGRAM_INBOX_SOFT_LIMIT:
    raise RuntimeError(
        "TELEGRAM_INBOX_HARD_LIMIT must be greater than TELEGRAM_INBOX_SOFT_LIMIT"
    )
if API_HEAVY_INFLIGHT_MAX > API_WRITE_INFLIGHT_MAX:
    raise RuntimeError(
        "API_HEAVY_INFLIGHT_MAX must not exceed API_WRITE_INFLIGHT_MAX"
    )
_api_rate_buckets = {}
_api_rate_requests = 0
_api_capacity = {
    "active_reads": 0, "active_writes": 0, "active_heavy": 0,
    "rejected_reads": 0, "rejected_writes": 0, "rejected_heavy": 0,
}
_media_capacity = {
    "active": 0, "waiters": 0, "rejected": 0,
}
_runtime_errors = {"database_locked": 0}
_media_normalize_semaphore = None
_media_normalize_loop = None
_media_normalize_jobs = set()
_health_cache = {
    "checked_at": 0.0, "database_ok": False,
    "database_error": "", "outbox_dead": 0,
    "outbox_pending": 0, "outbox_oldest_at": "",
    "inbox_dead": 0, "inbox_pending": 0, "inbox_oldest_at": "",
    "storage_ok": False,
    "media_quarantined": 0, "media_uploading_stale": 0,
}
_health_check_lock = asyncio.Lock()
_telegram_runtime = {
    "receiver_ready": False,
    "webhook_configured": False,
    "pending_update_count": 0,
    "overload_rejected": 0,
    "last_error": "",
    "last_update_at": "",
    "checked_at": "",
    "configured_at": "",
}
_background_tasks = {}


def _record_runtime_error(exc):
    """Keep a non-PII monotonic signal for SQLite lock failures."""
    current = exc
    visited = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if (
            isinstance(current, sqlite3.OperationalError)
            and "database is locked" in str(current).casefold()
        ):
            _runtime_errors["database_locked"] += 1
            return
        current = current.__cause__ or current.__context__
_shutdown_event = asyncio.Event()
_current_update_id = contextvars.ContextVar("telegram_update_id", default=None)
_telegram_timed_out_jobs = {}


def _worker_alive(name):
    task = _background_tasks.get(name)
    return bool(task and not task.done())

# Группа сообщества и тема «Работа» — для приветствия и ссылок.
COMMUNITY_CHAT_ID = int(os.getenv("COMMUNITY_CHAT_ID", "0") or "0")
WITHDRAW_MIN = max(1, int(os.getenv("WITHDRAW_MIN", "1000") or "1000"))
WITHDRAW_CONTACT = _clean_username(os.getenv("WITHDRAW_CONTACT", "KiriLegenda"))
WITHDRAW_ACCOUNT_RETENTION_DAYS = max(
    30, int(os.getenv("WITHDRAW_ACCOUNT_RETENTION_DAYS", "90") or "90")
)
WITHDRAW_PROCESSING_LEASE_MIN = max(
    5, int(os.getenv("WITHDRAW_PROCESSING_LEASE_MIN", "30") or "30")
)
WITHDRAW_ACCOUNT_KEY = os.getenv("WITHDRAW_ACCOUNT_KEY", "").strip()
try:
    WITHDRAW_FERNET = Fernet(WITHDRAW_ACCOUNT_KEY.encode("ascii"))
except (ValueError, TypeError, UnicodeError):
    WITHDRAW_FERNET = None
try:
    RIDE_RUB_PER_MIN = max(
        0.01, float((os.getenv("RIDE_RUB_PER_MIN", "8.5") or "8.5").replace(",", "."))
    )
except (TypeError, ValueError):
    RIDE_RUB_PER_MIN = 8.5


def ride_minutes_for(bonus):
    """Примерное число минут поездки: 1 бибибонус заменяет 1 рубль."""
    return max(0, round(float(bonus or 0) / RIDE_RUB_PER_MIN))


def _webhook_url():
    return f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}" if PUBLIC_BASE_URL else ""


def _validate_update_receiver_config():
    """Fail closed before touching Telegram when webhook configuration is unsafe."""
    if TELEGRAM_UPDATE_MODE not in ("polling", "webhook"):
        raise RuntimeError("TELEGRAM_UPDATE_MODE must be polling or webhook")
    if BIBITASKS_ENVIRONMENT not in ("production", "staging", "development", "test"):
        raise RuntimeError(
            "BIBITASKS_ENVIRONMENT must be production, staging, development or test"
        )
    if PILOT_LOAD_TEST_ENABLED and BIBITASKS_ENVIRONMENT != "staging":
        raise RuntimeError(
            "PILOT_LOAD_TEST_ENABLED is allowed only in staging"
        )
    if PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED and not (
        PILOT_LOAD_TEST_ENABLED and BIBITASKS_ENVIRONMENT == "staging"
    ):
        raise RuntimeError(
            "PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED requires staging load-test mode"
        )
    if PRIVACY_URL_RAW and PRIVACY_URL is None:
        raise RuntimeError("PRIVACY_URL must be a public HTTPS URL without credentials")
    if JOIN_REQUEST_INVITE_URL_RAW and JOIN_REQUEST_INVITE_URL is None:
        raise RuntimeError(
            "JOIN_REQUEST_INVITE_URL must be a modern https://t.me/+ invite link"
        )
    if JOIN_REQUEST_ADMISSION_ENABLED and JOIN_REQUEST_INVITE_URL is None:
        raise RuntimeError(
            "JOIN_REQUEST_INVITE_URL is required when join-request admission is enabled"
        )
    if BIBITASKS_ENVIRONMENT == "production" and (
        TELEGRAM_RETRY_BASE_SECONDS,
        TELEGRAM_RETRY_MAX_SECONDS,
        TELEGRAM_RETRY_MAX_ATTEMPTS,
    ) != (2, 3600, 10):
        raise RuntimeError(
            "Telegram retry overrides are forbidden in production; use staging/test"
        )
    if BIBITASKS_ENVIRONMENT == "production" and PRIVACY_URL is None:
        raise RuntimeError("PRIVACY_URL is required in production and must be a public HTTPS URL")
    if BIBITASKS_ENVIRONMENT == "production":
        if any(
            character in value
            for value in (PRIVACY_CONTROLLER_NAME_RAW, PRIVACY_CONTACT_RAW)
            for character in "\r\n\t"
        ):
            raise RuntimeError("privacy operator values must be single-line text")
        expected_privacy_url = (
            f"{PUBLIC_BASE_URL}/privacy" if PUBLIC_BASE_URL else ""
        )
        if PRIVACY_URL != expected_privacy_url:
            raise RuntimeError(
                "PRIVACY_URL must equal PUBLIC_BASE_URL + /privacy in production"
            )
        if not (3 <= len(PRIVACY_CONTROLLER_NAME) <= 160):
            raise RuntimeError(
                "PRIVACY_CONTROLLER_NAME must contain 3-160 characters in production"
            )
        if not (3 <= len(PRIVACY_CONTACT) <= 160):
            raise RuntimeError(
                "PRIVACY_CONTACT must contain 3-160 characters in production"
            )
    if TELEGRAM_INBOX_FERNET is None:
        raise RuntimeError("TELEGRAM_INBOX_KEY must be a valid Fernet key")
    if WITHDRAW_FERNET is None:
        raise RuntimeError("WITHDRAW_ACCOUNT_KEY must be a valid Fernet key")
    if TELEGRAM_INBOX_KEY == WITHDRAW_ACCOUNT_KEY:
        raise RuntimeError("TELEGRAM_INBOX_KEY must be independent from withdrawal encryption")
    if MEDIA_STORAGE not in ("local", "s3"):
        raise RuntimeError("MEDIA_STORAGE must be local or s3")
    if S3_ADDRESSING_STYLE not in ("auto", "path", "virtual"):
        raise RuntimeError("S3_ADDRESSING_STYLE must be auto, path or virtual")
    if MEDIA_STORAGE == "s3" and not S3_BUCKET:
        raise RuntimeError("S3_BUCKET is required when MEDIA_STORAGE=s3")
    if not 32 <= len(MEDIA_SIGNING_KEY) <= 256:
        raise RuntimeError("MEDIA_SIGNING_KEY must be 32-256 characters")
    media_secret_peers = (
        BOT_TOKEN, TELEGRAM_INBOX_KEY, WITHDRAW_ACCOUNT_KEY,
        WEBHOOK_ROUTE_ID, WEBHOOK_SECRET, HEALTH_TOKEN,
    )
    if MEDIA_SIGNING_KEY in {value for value in media_secret_peers if value}:
        raise RuntimeError("MEDIA_SIGNING_KEY must be independent from other secrets")
    if MEDIA_STORAGE == "s3" and S3_SSE not in ("AES256", "aws:kms"):
        raise RuntimeError("S3_SSE must be AES256 or aws:kms")
    if S3_PRIVACY_MODE not in ("public_access_block", "operator_attested"):
        raise RuntimeError(
            "S3_PRIVACY_MODE must be public_access_block or operator_attested"
        )
    if (
        MEDIA_STORAGE == "s3" and S3_PRIVACY_MODE == "operator_attested"
        and not S3_PRIVATE_BUCKET_CONFIRMED
    ):
        raise RuntimeError(
            "S3_PRIVATE_BUCKET_CONFIRMED=true is required for operator_attested mode"
        )
    for name, raw_endpoint in (
        ("S3_ENDPOINT_URL", S3_ENDPOINT_URL),
        ("S3_PUBLIC_ENDPOINT_URL", S3_PUBLIC_ENDPOINT_URL),
    ):
        if MEDIA_STORAGE == "s3" and raw_endpoint:
            endpoint = urlparse(raw_endpoint)
            if endpoint.scheme != "https" or not endpoint.netloc:
                raise RuntimeError(f"{name} must use HTTPS")
    if TELEGRAM_UPDATE_MODE == "polling":
        return
    if BIBITASKS_ENVIRONMENT == "production" and not JOIN_REQUEST_ADMISSION_ENABLED:
        raise RuntimeError(
            "Webhook production mode requires JOIN_REQUEST_ADMISSION_ENABLED=true"
        )
    if not OPS_GROUP_ID:
        raise RuntimeError("Webhook production mode requires numeric private OPS_GROUP_ID")
    if not GROUP_ID:
        raise RuntimeError("Webhook production mode requires numeric public GROUP_ID")
    if (
        OPS_GROUP_ID and GROUP_ID and OPS_GROUP_ID == GROUP_ID
    ) or (
        OPS_GROUP_USERNAME and GROUP_USERNAME
        and OPS_GROUP_USERNAME.casefold() == GROUP_USERNAME.casefold()
    ):
        raise RuntimeError("Private OPS group must differ from the public community")
    if len(ADMIN_IDS) < 2:
        raise RuntimeError("Webhook production mode requires at least two ADMIN_IDS")
    topics = (TOPIC_NEWS, TOPIC_CHAT, TOPIC_WORK, TOPIC_FRANCHISE, OPS_TOPIC_TASKS)
    if not all(TOPIC_CONFIG_EXPLICIT.values()) or any(
        not isinstance(value, int) or value <= 0 for value in topics
    ):
        raise RuntimeError("Webhook production mode requires explicit positive topic IDs")
    if len(set(topics[:4])) != 4:
        raise RuntimeError("Public Telegram topic IDs must be distinct")
    parsed = urlparse(PUBLIC_BASE_URL)
    try:
        public_port = parsed.port
    except ValueError:
        public_port = -1
    if (
        parsed.scheme != "https" or not parsed.netloc or public_port not in (None, 443)
        or parsed.username or parsed.password
        or parsed.path not in ("", "/") or parsed.query or parsed.fragment
    ):
        raise RuntimeError("PUBLIC_BASE_URL must be an HTTPS origin in webhook mode")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not 32 <= len(WEBHOOK_ROUTE_ID) <= 128 or any(
        ch not in allowed for ch in WEBHOOK_ROUTE_ID
    ):
        raise RuntimeError(
            "WEBHOOK_ROUTE_ID must contain 32-128 characters from A-Z a-z 0-9 _ -"
        )
    if not 32 <= len(WEBHOOK_SECRET) <= 256 or any(ch not in allowed for ch in WEBHOOK_SECRET):
        raise RuntimeError(
            "WEBHOOK_SECRET must contain 32-256 characters from A-Z a-z 0-9 _ -"
        )
    if WEBHOOK_ROUTE_ID in (WEBHOOK_SECRET, BOT_TOKEN):
        raise RuntimeError("WEBHOOK_ROUTE_ID must be independent from other secrets")
    if WEBHOOK_SECRET == BOT_TOKEN:
        raise RuntimeError("WEBHOOK_SECRET must be independent from BOT_TOKEN")
    if not isinstance(WEBHOOK_MAX_CONNECTIONS, int) or not 1 <= WEBHOOK_MAX_CONNECTIONS <= 100:
        raise RuntimeError("WEBHOOK_MAX_CONNECTIONS must be between 1 and 100")
    if not 32 <= len(HEALTH_TOKEN) <= 256 or any(ch not in allowed for ch in HEALTH_TOKEN):
        raise RuntimeError("HEALTH_TOKEN must contain 32-256 safe characters in webhook mode")
    secrets_in_use = (BOT_TOKEN, WEBHOOK_ROUTE_ID, WEBHOOK_SECRET, HEALTH_TOKEN)
    if len({value for value in secrets_in_use if value}) != len(secrets_in_use):
        raise RuntimeError("BOT_TOKEN and webhook/health secrets must all be independent")


def _validate_bot_identity(me):
    """Refuse to start when BOT_TOKEN belongs to a different Telegram bot."""
    actual_username = _clean_username(getattr(me, "username", ""))
    if not bool(getattr(me, "is_bot", False)) or not actual_username:
        raise RuntimeError("Telegram getMe did not return a valid bot identity")
    if not BOT_USERNAME or not hmac.compare_digest(
        actual_username.casefold(), BOT_USERNAME.casefold(),
    ):
        raise RuntimeError(
            "BOT_TOKEN belongs to a different bot than configured BOT_USERNAME"
        )


def _normalize_account_ref(value):
    value = " ".join(str(value or "").strip().split())
    if not 3 <= len(value) <= 100 or any(ord(char) < 32 for char in value):
        raise ValueError("Укажи корректный ID аккаунта Бибибайка.")
    return value


def _canonical_external_reference(value):
    """Stable comparison form for references copied from an external system."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split()).casefold()[:100]


def _mask_account_ref(value):
    value = str(value)
    if len(value) <= 4:
        return value[:1] + "•" * max(1, len(value) - 1)
    return value[:2] + "•" * min(12, len(value) - 4) + value[-2:]


def _account_fingerprint(value):
    if not WITHDRAW_ACCOUNT_KEY:
        raise ValueError("Шифрование переводов не настроено.")
    return hmac.new(
        WITHDRAW_ACCOUNT_KEY.encode("ascii"),
        value.casefold().encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _encrypt_account_ref(value):
    if WITHDRAW_FERNET is None:
        raise ValueError("Шифрование переводов не настроено.")
    return WITHDRAW_FERNET.encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt_account_ref(value):
    if WITHDRAW_FERNET is None or not value:
        raise ValueError("Данные аккаунта недоступны.")
    try:
        return WITHDRAW_FERNET.decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        raise ValueError("Не удалось безопасно расшифровать ID аккаунта.")

# Кто может модерировать заявки и подтверждать задания (Telegram user_id
# через запятую). На старте — вручную; позже свяжем с ролями в БД.
def _parse_ids(raw):
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out

# Ответственные задаются только в secret environment. Личные Telegram ID не
# должны попадать в публичный репозиторий.
ADMIN_IDS = _parse_ids(os.getenv("ADMIN_IDS", ""))

# Роли, которые ответственный может выдавать вручную.
ROLE_TITLES = {
    "helper": "Помощник",
    "employee": "Сотрудник",
    "admin": "Ответственный",
}

RBAC_POLICY_VERSION = 1
CAPABILITY_PRESETS = {
    "scout": frozenset({
        "application.queue.view", "application.review", "member.search",
        "member.tags.view", "member.tags.manage", "member.city.review",
        "member.role.manage_basic", "task.view", "task.create", "task.cancel",
        "task.delivery.view", "task.delivery.retry", "task.template.manage",
        "admission.view", "admission.retry", "telegram.publication.manage",
    }),
    "reviewer": frozenset({
        "task.review.queue", "task.review", "task.dispute.request",
        "task.dispute.decide", "bonus.grant.small",
        "bonus.reversal.request", "bonus.reversal.decide", "award.view",
        "award.grant", "award.revoke", "award.reversal.request",
        "award.reversal.decide", "member.task_summary.view",
    }),
    "cashier": frozenset({
        "withdrawal.queue.view", "withdrawal.account.reveal",
        "withdrawal.handoff", "withdrawal.decide",
        "member.financial_summary.view",
    }),
}
CAPABILITY_PRESETS["owner"] = frozenset().union(
    *CAPABILITY_PRESETS.values(), {
        "access.view", "access.request", "access.decide",
        "award.catalog.manage", "telegram.inbox.redrive",
        "operations.health.view",
    },
)
ALL_STAFF_CAPABILITIES = frozenset().union(*CAPABILITY_PRESETS.values())

# Fast manual thanks are intentionally small and positive-only.  Larger or
# negative corrections must go through a task/award/withdrawal reconciliation
# flow with its own business invariant.
MANUAL_GRANT_MAX_PER_OPERATION = 200
try:
    MANUAL_GRANT_DAILY_LIMIT = int(os.getenv("MANUAL_GRANT_DAILY_LIMIT", "300"))
except (TypeError, ValueError):
    MANUAL_GRANT_DAILY_LIMIT = 0
if not 1 <= MANUAL_GRANT_DAILY_LIMIT <= 10_000:
    # A broken limit must close the financial path, not silently remove it.
    MANUAL_GRANT_DAILY_LIMIT = 0

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
PRIVACY_TEMPLATE_PATH = os.path.join(BASE_DIR, "privacy.html")
LOGO_PATH = os.path.join(BASE_DIR, "logo.jpg")
TASK_PHOTO_DIR = os.path.join(DATA_DIR, "task_photos")
os.makedirs(TASK_PHOTO_DIR, exist_ok=True)

print("=" * 60, flush=True)
print(f"== {BUILD_VERSION}", flush=True)
print(f"== рабочая папка: {BASE_DIR}", flush=True)
print(f"== база: {DB_PATH}", flush=True)
print(f"== index.html рядом: {os.path.exists(INDEX_PATH)}", flush=True)
print(f"== logo.jpg рядом: {os.path.exists(LOGO_PATH)}", flush=True)
print(f"== порт: {WEBAPP_PORT}", flush=True)
print(f"== токен найден: {'да' if BOT_TOKEN else 'НЕТ'}", flush=True)
print(f"== админов в ADMIN_IDS: {len(ADMIN_IDS)}", flush=True)
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

# Готовые заготовки экономят ответственному время и оставляют место для
# конкретного адреса, города и фотографии текущей точки.
TASK_TEMPLATES = [
    {
        "key": "parking",
        "title": "Поправить парковку байков",
        "type": "fix_zone",
        "task_title": "Поправить парковку байков",
        "details": "Аккуратно выровнять байки, освободить проход и приложить фотоотчёт в задании.",
        "reward": 80,
        "mode": "open",
        "evidence_policy": "after_required",
    },
    {
        "key": "parking_photo",
        "title": "Проверить парковку и сделать фото",
        "type": "photo_check",
        "task_title": "Фото-проверка парковки",
        "details": "Проверить состояние парковки и отправить понятное фото результата.",
        "reward": 50,
        "mode": "all",
        "evidence_policy": "after_required",
    },
    {
        "key": "relocate",
        "title": "Переставить байки",
        "type": "relocate",
        "task_title": "Переставить байки на точке",
        "details": "Переместить байки по указанному адресу и убедиться, что они не мешают проходу.",
        "reward": 100,
        "mode": "open",
    },
    {
        "key": "charge",
        "title": "Заменить батареи",
        "type": "charge",
        "task_title": "Заменить батареи в байках",
        "details": "Заменить разряженные батареи и проверить, что байки снова доступны для поездки.",
        "reward": 120,
        "mode": "open",
    },
]

TASK_TEMPLATE_SEED_AT = "2026-07-28T00:00:00+00:00"
TASK_TEMPLATE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,49}$")
TASK_TEMPLATE_CONTENT_FIELDS = (
    "title", "task_type", "task_title", "details", "reward", "mode",
    "evidence_policy", "max_participants", "budget_cap", "photo_media_id",
    "photo_sha256",
)


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _task_template_content_hash(content):
    canonical = {key: content.get(key) for key in TASK_TEMPLATE_CONTENT_FIELDS}
    return hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()


def _task_template_seed_ids(key):
    return (
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"bibitasks:task-template:{key}")),
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"bibitasks:task-template:{key}:v1")),
    )

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
     "Пять и больше заданий за один день", 150, 0),
    ("mechanic", "🛠", "Мастер",
     "Починил байк на месте, без сервисного центра", 80, 0),
    ("night", "🌙", "Ночная смена",
     "Работал после полуночи", 120, 0),
    ("rescue_hero", "🏅", "Спасатель",
     "Вытащил байк из совсем плохого места", 200, 0),
    ("sharp_eye", "📸", "Глаз-алмаз",
     "Первым заметил и передал проблему", 50, 0),
    ("mentor", "🤝", "Наставник",
     "Ввёл новичка в работу", 100, 0),
    ("legend", "👑", "Легенда месяца",
     "Лучший результат месяца по команде", 200, 0),
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
        # Вся эволюция SQLite-схемы и backfill выполняются атомарно. Если
        # поздняя проверка обнаружит неоднозначные legacy-данные, закрытие
        # соединения откатит транзакцию целиком вместо частично обновлённой БД.
        await db.execute("BEGIN IMMEDIATE")
        schema_version = int((await (await db.execute("PRAGMA user_version")).fetchone())[0])
        if schema_version > SQLITE_SCHEMA_VERSION:
            raise RuntimeError(
                "SQLite schema is newer than this application build; refusing downgrade"
            )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS members (
                user_id     INTEGER PRIMARY KEY,
                full_name   TEXT,
                username    TEXT,
                phone       TEXT,
                city        TEXT,
                help_type   TEXT,
                transport   TEXT,
                availability TEXT,
                about       TEXT,
                tags        TEXT,
                application_note TEXT,
                role        TEXT NOT NULL DEFAULT 'candidate',  -- candidate|applicant|helper|employee|admin
                status      TEXT NOT NULL DEFAULT 'pending',    -- pending|approved|blocked
                bonus       INTEGER NOT NULL DEFAULT 0,
                done_count  INTEGER NOT NULL DEFAULT 0,
                referred_by INTEGER,
                created_at  TEXT,
                approved_at TEXT,
                approved_by INTEGER,
                applied_at  TEXT,
                city_change_requested TEXT,
                city_change_requested_at TEXT
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
                city        TEXT,
                reward      INTEGER NOT NULL DEFAULT 0,
                status      TEXT NOT NULL DEFAULT 'open',   -- open|claimed|review|done|cancelled
                created_by  INTEGER,
                created_at  TEXT,
                claimed_by  INTEGER,
                claimed_at  TEXT,
                done_at     TEXT,
                proof_note  TEXT,
                review_note TEXT,
                assigned_to INTEGER,
                slot_start  TEXT,
                slot_end    TEXT,
                repeatable  INTEGER NOT NULL DEFAULT 0,
                photo_file  TEXT,
                photo_media_id TEXT,
                operation_id TEXT UNIQUE,
                request_hash TEXT,
                completion_operation_id TEXT,
                completion_request_hash TEXT,
                submission_attempt INTEGER NOT NULL DEFAULT 0,
                evidence_policy TEXT NOT NULL DEFAULT 'none',
                max_participants INTEGER,
                budget_cap  INTEGER,
                cancel_operation_id TEXT,
                cancel_request_hash TEXT,
                cancelled_at TEXT,
                cancelled_by INTEGER,
                cancel_reason TEXT,
                expired_at TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                template_id TEXT,
                template_version_id TEXT,
                CHECK((template_id IS NULL)=(template_version_id IS NULL)),
                FOREIGN KEY(template_id,template_version_id)
                    REFERENCES task_template_versions(template_id,id)
                    DEFERRABLE INITIALLY DEFERRED
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bonus_ledger (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                amount     INTEGER NOT NULL,           -- + начисление / - списание
                reason     TEXT NOT NULL,
                task_id    INTEGER,
                assignment_id INTEGER,
                withdrawal_id INTEGER,
                created_by INTEGER,
                created_at TEXT,
                operation_id TEXT UNIQUE,
                balance_after INTEGER,
                reversal_of_ledger_id INTEGER
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
            CREATE TABLE IF NOT EXISTS referral_tokens (
                token       TEXT PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL
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
                review_note TEXT,
                completion_operation_id TEXT,
                completion_request_hash TEXT,
                submission_attempt INTEGER NOT NULL DEFAULT 0,
                reward_snapshot INTEGER,
                due_at TEXT,
                revision_due_at TEXT,
                release_operation_id TEXT,
                release_request_hash TEXT,
                released_at TEXT,
                release_reason TEXT,
                terminal_at TEXT,
                terminal_by INTEGER,
                terminal_reason TEXT,
                decision_operation_id TEXT,
                decision_request_hash TEXT,
                version INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_evidence (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                assignment_id INTEGER,
                task_id       INTEGER NOT NULL,
                user_id       INTEGER NOT NULL,
                kind          TEXT NOT NULL,
                photo_file    TEXT NOT NULL,
                media_id      TEXT,
                sha256        TEXT NOT NULL,
                submission_operation_id TEXT,
                attempt       INTEGER NOT NULL DEFAULT 1,
                is_current    INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS media_objects (
                id TEXT PRIMARY KEY,
                backend TEXT NOT NULL,
                object_key TEXT NOT NULL,
                purpose TEXT NOT NULL,
                state TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                upload_operation_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ready_at TEXT,
                delete_after TEXT,
                deleted_at TEXT,
                last_error TEXT,
                reconcile_attempts INTEGER NOT NULL DEFAULT 0,
                version_id TEXT,
                checked_at TEXT,
                UNIQUE(backend, object_key)
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
                note        TEXT,
                operation_id TEXT,
                request_hash TEXT,
                account_type TEXT,
                account_ciphertext TEXT,
                account_masked TEXT,
                account_fingerprint TEXT,
                key_version INTEGER,
                decision_operation_id TEXT,
                decision_request_hash TEXT,
                provider TEXT,
                external_reference TEXT,
                external_reference_canonical TEXT,
                reject_reason TEXT,
                account_purged_at TEXT,
                processing_by INTEGER,
                processing_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                withdrawal_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                actor_id INTEGER,
                operation_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_review_commands (
                operation_id TEXT PRIMARY KEY,
                assignment_id INTEGER NOT NULL,
                request_hash TEXT NOT NULL,
                result_status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_templates (
                id TEXT PRIMARY KEY,
                key TEXT NOT NULL UNIQUE,
                origin TEXT NOT NULL CHECK(origin IN ('system','manual')),
                status TEXT NOT NULL CHECK(status IN ('active','archived')),
                generation INTEGER NOT NULL CHECK(generation>0),
                current_version_id TEXT NOT NULL,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                updated_by INTEGER,
                updated_at TEXT NOT NULL,
                archived_by INTEGER,
                archived_at TEXT,
                CHECK(key=lower(key)),
                CHECK((status='active' AND archived_by IS NULL AND archived_at IS NULL)
                    OR (status='archived' AND archived_by IS NOT NULL AND archived_at IS NOT NULL)),
                FOREIGN KEY(id,current_version_id)
                    REFERENCES task_template_versions(template_id,id)
                    DEFERRABLE INITIALLY DEFERRED
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_template_versions (
                id TEXT PRIMARY KEY,
                template_id TEXT NOT NULL,
                version_number INTEGER NOT NULL CHECK(version_number>0),
                title TEXT NOT NULL,
                task_type TEXT NOT NULL,
                task_title TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                reward INTEGER NOT NULL CHECK(reward BETWEEN 1 AND 300),
                mode TEXT NOT NULL CHECK(mode IN ('open','personal','all')),
                evidence_policy TEXT NOT NULL CHECK(evidence_policy IN
                    ('none','comment_only','photo_required','before_after')),
                max_participants INTEGER NOT NULL,
                budget_cap INTEGER NOT NULL,
                photo_media_id TEXT,
                photo_sha256 TEXT,
                content_hash TEXT NOT NULL,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(template_id,version_number),
                UNIQUE(template_id,id),
                CHECK((photo_media_id IS NULL AND photo_sha256 IS NULL)
                    OR (photo_media_id IS NOT NULL AND photo_sha256 IS NOT NULL)),
                CHECK((mode IN ('open','personal') AND max_participants=1
                    AND budget_cap=reward) OR (mode='all'
                    AND max_participants BETWEEN 1 AND 500
                    AND budget_cap BETWEEN reward AND 150000
                    AND max_participants*reward<=budget_cap)),
                FOREIGN KEY(template_id) REFERENCES task_templates(id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(photo_media_id) REFERENCES media_objects(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_template_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id TEXT NOT NULL,
                template_version_id TEXT,
                event_type TEXT NOT NULL CHECK(event_type IN
                    ('created','version_created','archived','activated')),
                generation INTEGER NOT NULL CHECK(generation>0),
                actor_id INTEGER,
                operation_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(template_id,generation),
                FOREIGN KEY(template_id) REFERENCES task_templates(id),
                FOREIGN KEY(template_id,template_version_id)
                    REFERENCES task_template_versions(template_id,id)
                    DEFERRABLE INITIALLY DEFERRED
            )
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS task_template_versions_immutable_update
            BEFORE UPDATE ON task_template_versions BEGIN
                SELECT RAISE(ABORT,'task template versions are immutable');
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS task_template_versions_immutable_delete
            BEFORE DELETE ON task_template_versions BEGIN
                SELECT RAISE(ABORT,'task template versions are immutable');
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS task_templates_key_immutable
            BEFORE UPDATE OF key ON task_templates
            WHEN NEW.key<>OLD.key BEGIN
                SELECT RAISE(ABORT,'task template key is immutable');
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS task_template_events_immutable_update
            BEFORE UPDATE ON task_template_events BEGIN
                SELECT RAISE(ABORT,'task template events are immutable');
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS task_template_events_immutable_delete
            BEFORE DELETE ON task_template_events BEGIN
                SELECT RAISE(ABORT,'task template events are immutable');
            END
        """)
        # Existing SQLite installations cannot gain composite foreign keys via
        # ALTER TABLE. These triggers keep upgraded databases as strict as a
        # freshly created schema and as the PostgreSQL target.
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS tasks_template_provenance_insert
            BEFORE INSERT ON tasks
            WHEN (NEW.template_id IS NULL)<>(NEW.template_version_id IS NULL)
              OR (NEW.template_id IS NOT NULL AND NOT EXISTS (
                  SELECT 1 FROM task_template_versions v
                  WHERE v.template_id=NEW.template_id AND v.id=NEW.template_version_id
              ))
            BEGIN
                SELECT RAISE(ABORT,'invalid task template provenance');
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS tasks_template_provenance_update
            BEFORE UPDATE OF template_id,template_version_id ON tasks
            WHEN (NEW.template_id IS NULL)<>(NEW.template_version_id IS NULL)
              OR (NEW.template_id IS NOT NULL AND NOT EXISTS (
                  SELECT 1 FROM task_template_versions v
                  WHERE v.template_id=NEW.template_id AND v.id=NEW.template_version_id
              ))
            BEGIN
                SELECT RAISE(ABORT,'invalid task template provenance');
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS task_templates_current_version_update
            BEFORE UPDATE OF current_version_id ON task_templates
            WHEN NOT EXISTS (
                SELECT 1 FROM task_template_versions v
                WHERE v.template_id=NEW.id AND v.id=NEW.current_version_id
            )
            BEGIN
                SELECT RAISE(ABORT,'task template current version mismatch');
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS task_template_events_provenance_insert
            BEFORE INSERT ON task_template_events
            WHEN (NEW.template_version_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM task_template_versions v
                    WHERE v.template_id=NEW.template_id AND v.id=NEW.template_version_id
                )) OR NOT EXISTS (
                    SELECT 1 FROM task_templates t
                    JOIN task_template_versions v
                      ON v.template_id=t.id AND v.id=t.current_version_id
                    WHERE t.id=NEW.template_id
                )
            BEGIN
                SELECT RAISE(ABORT,'invalid task template event provenance');
            END
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_disputes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assignment_id INTEGER NOT NULL UNIQUE,
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reward INTEGER NOT NULL,
                reason TEXT NOT NULL,
                reconciliation_reason TEXT,
                reconciliation_reference TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                opened_by INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                open_operation_id TEXT NOT NULL UNIQUE,
                open_request_hash TEXT NOT NULL,
                decided_by INTEGER,
                decided_at TEXT,
                decision_note TEXT,
                decision_operation_id TEXT UNIQUE,
                decision_request_hash TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS manual_grant_commands (
                operation_id TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL CHECK(amount BETWEEN 1 AND 200),
                reason TEXT NOT NULL,
                maker_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                ledger_id INTEGER NOT NULL,
                result_balance INTEGER NOT NULL,
                CHECK(maker_id<>user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS manual_grant_reversals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grant_operation_id TEXT NOT NULL,
                original_ledger_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL CHECK(amount BETWEEN 1 AND 200),
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','manual_required','applied','rejected')),
                manual_reason TEXT,
                requested_by INTEGER NOT NULL,
                requested_at TEXT NOT NULL,
                request_operation_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                decided_by INTEGER,
                decided_at TEXT,
                decision_note TEXT,
                decision_operation_id TEXT UNIQUE,
                decision_hash TEXT,
                reversal_ledger_id INTEGER UNIQUE,
                result_balance INTEGER,
                CHECK(requested_by<>user_id),
                CHECK(decided_by IS NULL OR (
                    decided_by<>requested_by AND decided_by<>user_id
                )),
                FOREIGN KEY(grant_operation_id)
                    REFERENCES manual_grant_commands(operation_id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(original_ledger_id) REFERENCES bonus_ledger(id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(user_id) REFERENCES members(user_id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(requested_by) REFERENCES members(user_id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(decided_by) REFERENCES members(user_id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(reversal_ledger_id) REFERENCES bonus_ledger(id)
                    DEFERRABLE INITIALLY DEFERRED
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_role_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                from_role TEXT NOT NULL,
                to_role TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','applied','rejected')),
                requested_by INTEGER NOT NULL,
                requested_at TEXT NOT NULL,
                request_operation_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                decided_by INTEGER,
                decided_at TEXT,
                decision_note TEXT,
                decision_operation_id TEXT UNIQUE,
                decision_hash TEXT,
                CHECK(from_role<>to_role),
                CHECK(requested_by<>user_id),
                CHECK(decided_by IS NULL OR (
                    decided_by<>requested_by AND decided_by<>user_id
                ))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS operation_registry (
                operation_id TEXT PRIMARY KEY,
                command_type TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_authorities (
                user_id INTEGER NOT NULL,
                origin TEXT NOT NULL CHECK(origin IN ('env','manual')),
                granted_operation_id TEXT,
                granted_at TEXT NOT NULL,
                PRIMARY KEY(user_id, origin),
                FOREIGN KEY(user_id) REFERENCES members(user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS staff_access_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                preset TEXT NOT NULL CHECK(preset IN ('scout','reviewer','cashier','owner')),
                origin TEXT NOT NULL CHECK(origin IN ('env','manual')),
                status TEXT NOT NULL CHECK(status IN ('active','revoked')),
                policy_version INTEGER NOT NULL,
                generation INTEGER NOT NULL CHECK(generation>0),
                granted_by INTEGER,
                approved_by INTEGER,
                grant_operation_id TEXT NOT NULL UNIQUE,
                granted_at TEXT NOT NULL,
                revoked_by INTEGER,
                revoke_operation_id TEXT UNIQUE,
                revoked_at TEXT,
                UNIQUE(user_id,preset,origin,generation),
                FOREIGN KEY(user_id) REFERENCES members(user_id),
                CHECK(approved_by IS NULL OR granted_by IS NULL OR approved_by<>granted_by)
            )
        """)
        capability_sql = ",".join(
            "'" + item.replace("'", "''") + "'"
            for item in sorted(ALL_STAFF_CAPABILITIES)
        )
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS staff_grant_capabilities (
                grant_id INTEGER NOT NULL,
                capability TEXT NOT NULL CHECK(capability IN ({capability_sql})),
                PRIMARY KEY(grant_id,capability),
                FOREIGN KEY(grant_id) REFERENCES staff_access_grants(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS staff_access_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_user_id INTEGER NOT NULL,
                change_action TEXT NOT NULL CHECK(change_action IN ('assign','revoke')),
                preset TEXT NOT NULL CHECK(preset IN ('scout','reviewer','cashier','owner')),
                expected_generation INTEGER NOT NULL CHECK(expected_generation>=0),
                reason TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','applied','rejected')),
                requested_by INTEGER NOT NULL,
                requested_at TEXT NOT NULL,
                request_operation_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                decided_by INTEGER,
                decided_at TEXT,
                decision_note TEXT,
                decision_operation_id TEXT UNIQUE,
                decision_hash TEXT,
                result_json TEXT,
                FOREIGN KEY(target_user_id) REFERENCES members(user_id),
                CHECK(requested_by<>target_user_id),
                CHECK(decided_by IS NULL OR (
                    decided_by<>requested_by AND decided_by<>target_user_id
                ))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS staff_access_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_user_id INTEGER NOT NULL,
                preset TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor_id INTEGER,
                operation_id TEXT NOT NULL UNIQUE,
                policy_version INTEGER NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(target_user_id) REFERENCES members(user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_completion_commands (
                operation_id TEXT PRIMARY KEY,
                assignment_id INTEGER NOT NULL,
                request_hash TEXT NOT NULL,
                result_status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_outbox (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key      TEXT NOT NULL UNIQUE,
                event_type     TEXT NOT NULL,
                recipient_id   INTEGER,
                chat_id        TEXT,
                topic_id       INTEGER,
                media_id      TEXT,
                payload_json   TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'pending',
                attempts       INTEGER NOT NULL DEFAULT 0,
                available_at   TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                sent_at        TEXT,
                telegram_message_id INTEGER,
                telegram_thread_id INTEGER,
                last_error     TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS telegram_update_inbox (
                update_id INTEGER PRIMARY KEY,
                payload_json TEXT,
                payload_sha256 TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                processed_at TEXT,
                last_error TEXT,
                locked_by TEXT,
                locked_at TEXT,
                dead_at TEXT,
                redrive_operation_id TEXT,
                redrive_request_hash TEXT,
                redrive_reason TEXT,
                redriven_by INTEGER,
                redriven_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS telegram_update_effects (
                update_id INTEGER NOT NULL,
                effect_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (update_id, effect_key)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS telegram_update_redrive_commands (
                operation_id TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                update_id INTEGER NOT NULL,
                admin_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                result_status TEXT NOT NULL,
                created_at TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS analytics_subjects (
                subject_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS product_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                occurred_at TEXT NOT NULL,
                event_name TEXT NOT NULL,
                source TEXT NOT NULL,
                subject_id TEXT,
                session_id TEXT,
                task_id INTEGER,
                assignment_id INTEGER,
                outcome TEXT,
                reason_code TEXT,
                properties_json TEXT NOT NULL DEFAULT '{}',
                dedupe_key TEXT,
                schema_version INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS telegram_join_requests (
                request_key TEXT PRIMARY KEY,
                update_id INTEGER UNIQUE,
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                invite_link_sha256 TEXT,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                decision TEXT,
                decision_queued_at TEXT,
                decided_at TEXT,
                joined_at TEXT,
                manual_retry_reason TEXT,
                manual_retry_by INTEGER,
                manual_retry_at TEXT,
                last_error TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS published_posts (
                kind        TEXT PRIMARY KEY,
                chat_id     INTEGER NOT NULL,
                topic       INTEGER,
                message_ids TEXT NOT NULL,
                published_at TEXT NOT NULL,
                published_by INTEGER,
                operation_id TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS publication_jobs (
                kind TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                requested_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS publication_delivery_parts (
                operation_id TEXT NOT NULL,
                part_index INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (operation_id, part_index)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS publication_cleanup_messages (
                operation_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                final_job_status TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                deleted_at TEXT,
                PRIMARY KEY (operation_id, chat_id, message_id)
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
                operation_id TEXT UNIQUE,
                balance_after INTEGER,
                revoked_at TEXT,
                revoked_by INTEGER,
                revoke_note TEXT,
                revoke_operation_id TEXT,
                revoke_request_hash TEXT,
                UNIQUE(user_id, award_id, slot)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS award_reversals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_award_id INTEGER NOT NULL,
                original_ledger_id INTEGER,
                user_id INTEGER NOT NULL,
                award_id INTEGER NOT NULL,
                award_title TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK(
                    amount>=0 AND (origin<>'maker_checker' OR amount<=200)
                ),
                original_granted_by INTEGER,
                original_grant_operation_id TEXT,
                origin TEXT NOT NULL CHECK(origin IN (
                    'maker_checker','legacy_single_actor','legacy_unlinked'
                )),
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
                    'pending','manual_required','applied','rejected'
                )),
                manual_reason TEXT,
                reason TEXT NOT NULL,
                requested_by INTEGER,
                requested_at TEXT,
                request_operation_id TEXT UNIQUE,
                request_hash TEXT,
                decided_by INTEGER,
                decided_at TEXT,
                decision_note TEXT,
                decision_operation_id TEXT UNIQUE,
                decision_hash TEXT,
                reversal_ledger_id INTEGER UNIQUE,
                result_balance INTEGER,
                version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
                CHECK(origin<>'maker_checker' OR (
                    requested_by IS NOT NULL AND requested_at IS NOT NULL
                    AND request_operation_id IS NOT NULL AND request_hash IS NOT NULL
                    AND requested_by<>user_id
                    AND ((amount=0 AND original_ledger_id IS NULL)
                         OR (amount>0 AND original_ledger_id IS NOT NULL
                             AND original_grant_operation_id IS NOT NULL))
                )),
                CHECK(origin<>'maker_checker' OR decided_by IS NULL OR (
                    decided_by<>requested_by AND decided_by<>user_id
                    AND (original_granted_by IS NULL OR decided_by<>original_granted_by)
                )),
                CHECK(origin<>'maker_checker' OR (
                    (status IN ('pending','manual_required')
                     AND decided_by IS NULL AND decided_at IS NULL
                     AND decision_operation_id IS NULL AND decision_hash IS NULL
                     AND reversal_ledger_id IS NULL AND result_balance IS NULL)
                    OR
                    (status='applied' AND decided_by IS NOT NULL
                     AND decided_at IS NOT NULL AND decision_operation_id IS NOT NULL
                     AND decision_hash IS NOT NULL AND result_balance IS NOT NULL
                     AND ((amount=0 AND reversal_ledger_id IS NULL)
                          OR (amount>0 AND reversal_ledger_id IS NOT NULL)))
                    OR
                    (status='rejected' AND decided_by IS NOT NULL
                     AND decided_at IS NOT NULL AND decision_operation_id IS NOT NULL
                     AND decision_hash IS NOT NULL AND reversal_ledger_id IS NULL
                     AND result_balance IS NULL)
                )),
                FOREIGN KEY(member_award_id) REFERENCES member_awards(id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(original_ledger_id) REFERENCES bonus_ledger(id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(user_id) REFERENCES members(user_id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(award_id) REFERENCES awards(id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(original_granted_by) REFERENCES members(user_id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(requested_by) REFERENCES members(user_id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(decided_by) REFERENCES members(user_id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(reversal_ledger_id) REFERENCES bonus_ledger(id)
                    DEFERRABLE INITIALLY DEFERRED
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS award_reversal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reversal_id INTEGER NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN (
                    'requested','manual_required','applied','rejected','legacy_imported'
                )),
                from_status TEXT,
                to_status TEXT NOT NULL,
                actor_id INTEGER,
                operation_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                CHECK(from_status IS NULL OR from_status IN (
                    'pending','manual_required','applied','rejected'
                )),
                CHECK(to_status IN (
                    'pending','manual_required','applied','rejected'
                )),
                FOREIGN KEY(reversal_id) REFERENCES award_reversals(id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(actor_id) REFERENCES members(user_id)
                    DEFERRABLE INITIALLY DEFERRED
            )
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS award_reversal_events_immutable_update
            BEFORE UPDATE ON award_reversal_events BEGIN
                SELECT RAISE(ABORT,'award reversal events are immutable');
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS award_reversal_events_immutable_delete
            BEFORE DELETE ON award_reversal_events BEGIN
                SELECT RAISE(ABORT,'award reversal events are immutable');
            END
        """)
        if schema_version < 300:
            legacy_awards = await (await db.execute(
                "SELECT ma.id,ma.user_id,ma.award_id,a.title,ma.bonus,ma.granted_by,"
                "ma.operation_id,ma.revoked_by,ma.revoked_at,ma.revoke_note,"
                "ma.revoke_operation_id FROM member_awards ma "
                "JOIN awards a ON a.id=ma.award_id "
                "WHERE ma.revoked_at IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM award_reversals r WHERE r.member_award_id=ma.id)"
            )).fetchall()
            for legacy in legacy_awards:
                (
                    entry_id, user_id, award_id, title, amount, granted_by,
                    grant_operation, revoked_by, revoked_at, revoke_note,
                    revoke_operation,
                ) = legacy
                amount = max(0, int(amount or 0))
                original_ledger = None
                reversal_ledger = None
                debit_reversal_origin = None
                result_balance = None
                if amount and grant_operation:
                    candidate = await (await db.execute(
                        "SELECT id,user_id,amount,operation_id FROM bonus_ledger "
                        "WHERE operation_id=?",
                        (f"award:{grant_operation}",),
                    )).fetchone()
                    if candidate and (
                        int(candidate[1]) == int(user_id)
                        and int(candidate[2]) == amount
                        and candidate[3] == f"award:{grant_operation}"
                    ):
                        original_ledger = int(candidate[0])
                if amount and revoke_operation:
                    candidate = await (await db.execute(
                        "SELECT id,user_id,amount,operation_id,balance_after,"
                        "reversal_of_ledger_id "
                        "FROM bonus_ledger WHERE operation_id=?",
                        (f"award_revoke:{revoke_operation}",),
                    )).fetchone()
                    if candidate and (
                        int(candidate[1]) == int(user_id)
                        and int(candidate[2]) == -amount
                        and candidate[3] == f"award_revoke:{revoke_operation}"
                    ):
                        reversal_ledger = int(candidate[0])
                        result_balance = candidate[4]
                        debit_reversal_origin = candidate[5]
                linked = amount == 0
                if amount and original_ledger is not None and reversal_ledger is not None:
                    conflicting = await (await db.execute(
                        "SELECT 1 FROM bonus_ledger WHERE reversal_of_ledger_id=? "
                        "AND id<>? LIMIT 1", (original_ledger, reversal_ledger),
                    )).fetchone()
                    linked = (
                        not conflicting
                        and (
                            debit_reversal_origin is None
                            or int(debit_reversal_origin) == int(original_ledger)
                        )
                    )
                    if linked:
                        updated_ledger = await db.execute(
                            "UPDATE bonus_ledger SET reversal_of_ledger_id=? "
                            "WHERE id=? AND (reversal_of_ledger_id IS NULL "
                            "OR reversal_of_ledger_id=?)",
                            (original_ledger, reversal_ledger, original_ledger),
                        )
                        linked = updated_ledger.rowcount == 1
                safe_grantor = None
                if granted_by is not None and await (await db.execute(
                    "SELECT 1 FROM members WHERE user_id=?", (granted_by,),
                )).fetchone():
                    safe_grantor = int(granted_by)
                safe_revoker = None
                if revoked_by is not None and await (await db.execute(
                    "SELECT 1 FROM members WHERE user_id=?", (revoked_by,),
                )).fetchone():
                    safe_revoker = int(revoked_by)
                cursor = await db.execute(
                    "INSERT INTO award_reversals "
                    "(member_award_id,original_ledger_id,user_id,award_id,award_title,"
                    "amount,original_granted_by,original_grant_operation_id,origin,status,"
                    "reason,decided_by,decided_at,decision_note,reversal_ledger_id,"
                    "result_balance) VALUES (?,?,?,?,?,?,?,?,?,'applied',?,?,?,?,?,?)",
                    (
                        entry_id, original_ledger, user_id, award_id, title, amount,
                        safe_grantor, grant_operation,
                        "legacy_single_actor" if linked else "legacy_unlinked",
                        revoke_note or "Legacy award revocation", safe_revoker,
                        revoked_at, revoke_note, reversal_ledger, result_balance,
                    ),
                )
                await _award_reversal_event_in_tx(
                    db, cursor.lastrowid, "legacy_imported", None, "applied",
                    safe_revoker, metadata={"linked": linked},
                )
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
            member_columns.add("applied_at")
        for name in ("city_change_requested", "city_change_requested_at"):
            if name not in member_columns:
                await db.execute(f"ALTER TABLE members ADD COLUMN {name} TEXT")
                member_columns.add(name)
        for name, sql_type in (
            ("group_membership_status", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("group_joined_at", "TEXT"),
            ("group_left_at", "TEXT"),
        ):
            if name not in member_columns:
                await db.execute(
                    f"ALTER TABLE members ADD COLUMN {name} {sql_type}"
                )
                member_columns.add(name)
        for name in (
            "city", "help_type", "transport", "availability",
            "about", "tags", "application_note",
        ):
            if name not in member_columns:
                await db.execute(f"ALTER TABLE members ADD COLUMN {name} TEXT")
                member_columns.add(name)
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
            ("city", "TEXT"),
            ("review_note", "TEXT"),
            ("photo_file", "TEXT"),
            ("photo_media_id", "TEXT"),
            ("operation_id", "TEXT"),
            ("request_hash", "TEXT"),
            ("completion_operation_id", "TEXT"),
            ("completion_request_hash", "TEXT"),
            ("submission_attempt", "INTEGER NOT NULL DEFAULT 0"),
            ("evidence_policy", "TEXT NOT NULL DEFAULT 'none'"),
            ("max_participants", "INTEGER"),
            ("budget_cap", "INTEGER"),
            ("cancel_operation_id", "TEXT"),
            ("cancel_request_hash", "TEXT"),
            ("cancelled_at", "TEXT"),
            ("cancelled_by", "INTEGER"),
            ("cancel_reason", "TEXT"),
            ("expired_at", "TEXT"),
            ("template_id", "TEXT"),
            ("template_version_id", "TEXT"),
            ("version", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if name not in task_columns:
                await db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {sql_type}")
        assignment_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(task_assignments)")
            ).fetchall()
        }
        for name, sql_type in (
            ("review_note", "TEXT"),
            ("completion_operation_id", "TEXT"),
            ("completion_request_hash", "TEXT"),
            ("submission_attempt", "INTEGER NOT NULL DEFAULT 0"),
            ("reward_snapshot", "INTEGER"),
            ("due_at", "TEXT"),
            ("revision_due_at", "TEXT"),
            ("release_operation_id", "TEXT"),
            ("release_request_hash", "TEXT"),
            ("released_at", "TEXT"),
            ("release_reason", "TEXT"),
            ("terminal_at", "TEXT"),
            ("terminal_by", "INTEGER"),
            ("terminal_reason", "TEXT"),
            ("decision_operation_id", "TEXT"),
            ("decision_request_hash", "TEXT"),
            ("version", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if name not in assignment_columns:
                await db.execute(
                    f"ALTER TABLE task_assignments ADD COLUMN {name} {sql_type}")
        assignment_sql_row = await (await db.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='task_assignments'"
        )).fetchone()
        assignment_sql = "".join(
            str(assignment_sql_row[0] or "").lower().split()
        ) if assignment_sql_row else ""
        if "unique(task_id,user_id)" in assignment_sql:
            # v2.6 хранит каждую попытку отдельно. Старый UNIQUE не позволял
            # человеку снова взять задание после корректного освобождения.
            await db.execute("ALTER TABLE task_assignments RENAME TO task_assignments_v25")
            await db.execute("""
                CREATE TABLE task_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'claimed',
                    claimed_at TEXT NOT NULL,
                    done_at TEXT,
                    proof_note TEXT,
                    review_note TEXT,
                    completion_operation_id TEXT,
                    completion_request_hash TEXT,
                    submission_attempt INTEGER NOT NULL DEFAULT 0,
                    reward_snapshot INTEGER,
                    due_at TEXT,
                    revision_due_at TEXT,
                    release_operation_id TEXT,
                    release_request_hash TEXT,
                    released_at TEXT,
                    release_reason TEXT,
                    terminal_at TEXT,
                    terminal_by INTEGER,
                    terminal_reason TEXT,
                    decision_operation_id TEXT,
                    decision_request_hash TEXT,
                    version INTEGER NOT NULL DEFAULT 1
                )
            """)
            await db.execute("""
                INSERT INTO task_assignments (
                    id, task_id, user_id, status, claimed_at, done_at,
                    proof_note, review_note, completion_operation_id,
                    completion_request_hash, submission_attempt,
                    reward_snapshot, due_at, revision_due_at,
                    release_operation_id, release_request_hash, released_at,
                    release_reason, terminal_at, terminal_by, terminal_reason, version
                )
                SELECT id, task_id, user_id, status, claimed_at, done_at,
                    proof_note, review_note, completion_operation_id,
                    completion_request_hash, submission_attempt,
                    reward_snapshot, due_at, revision_due_at,
                    release_operation_id, release_request_hash, released_at,
                    release_reason, terminal_at, terminal_by, terminal_reason, version
                FROM task_assignments_v25
            """)
            await db.execute("DROP TABLE task_assignments_v25")
        evidence_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(task_evidence)")
            ).fetchall()
        }
        for name, sql_type in (
            ("submission_operation_id", "TEXT"),
            ("attempt", "INTEGER NOT NULL DEFAULT 1"),
            ("is_current", "INTEGER NOT NULL DEFAULT 1"),
            ("media_id", "TEXT"),
        ):
            if name not in evidence_columns:
                await db.execute(
                    f"ALTER TABLE task_evidence ADD COLUMN {name} {sql_type}")
        award_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(member_awards)")
            ).fetchall()
        }
        if "balance_after" not in award_columns:
            await db.execute(
                "ALTER TABLE member_awards ADD COLUMN balance_after INTEGER")
        ledger_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(bonus_ledger)")
            ).fetchall()
        }
        for name, sql_type in (
            ("operation_id", "TEXT"),
            ("balance_after", "INTEGER"),
            ("assignment_id", "INTEGER"),
            ("withdrawal_id", "INTEGER"),
            ("reversal_of_ledger_id", "INTEGER"),
        ):
            if name not in ledger_columns:
                await db.execute(
                    f"ALTER TABLE bonus_ledger ADD COLUMN {name} {sql_type}")
        withdrawal_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(withdrawal_requests)")
            ).fetchall()
        }
        for name, sql_type in (
            ("operation_id", "TEXT"),
            ("request_hash", "TEXT"),
            ("account_type", "TEXT"),
            ("account_ciphertext", "TEXT"),
            ("account_masked", "TEXT"),
            ("account_fingerprint", "TEXT"),
            ("key_version", "INTEGER"),
            ("decision_operation_id", "TEXT"),
            ("decision_request_hash", "TEXT"),
            ("provider", "TEXT"),
            ("external_reference", "TEXT"),
            ("external_reference_canonical", "TEXT"),
            ("reject_reason", "TEXT"),
            ("account_purged_at", "TEXT"),
            ("processing_by", "INTEGER"),
            ("processing_at", "TEXT"),
        ):
            if name not in withdrawal_columns:
                await db.execute(
                    f"ALTER TABLE withdrawal_requests ADD COLUMN {name} {sql_type}")
        inbox_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(telegram_update_inbox)")
            ).fetchall()
        }
        for name, sql_type in (
            ("locked_by", "TEXT"),
            ("locked_at", "TEXT"),
            ("dead_at", "TEXT"),
            ("redrive_operation_id", "TEXT"),
            ("redrive_request_hash", "TEXT"),
            ("redrive_reason", "TEXT"),
            ("redriven_by", "INTEGER"),
            ("redriven_at", "TEXT"),
        ):
            if name not in inbox_columns:
                await db.execute(
                    f"ALTER TABLE telegram_update_inbox ADD COLUMN {name} {sql_type}")
        # v2.7: upgrade the short-lived plaintext inbox prototype in place.
        # Corrupt legacy rows are quarantined without retaining personal payloads.
        legacy_rows = await (await db.execute(
            "SELECT update_id,payload_json,payload_sha256,status,received_at,processed_at "
            "FROM telegram_update_inbox WHERE payload_json IS NOT NULL"
        )).fetchall()
        if legacy_rows and TELEGRAM_INBOX_FERNET is None:
            raise RuntimeError(
                "TELEGRAM_INBOX_KEY is required to migrate Telegram inbox"
            )
        for update_id, stored, fingerprint, status, received_at, processed_at in legacy_rows:
            try:
                try:
                    decoded = TELEGRAM_INBOX_FERNET.decrypt(
                        str(stored).encode("ascii")
                    ).decode("utf-8")
                except (InvalidToken, UnicodeError, ValueError):
                    if str(fingerprint or "").startswith("h1:"):
                        raise RuntimeError(
                            "TELEGRAM_INBOX_KEY cannot decrypt existing inbox rows"
                        )
                    decoded = str(stored)
                payload = json.loads(decoded)
                if not isinstance(payload, dict) or type(payload.get("update_id")) is not int:
                    raise ValueError("legacy_update_shape")
                if int(payload["update_id"]) != int(update_id):
                    raise ValueError("legacy_update_id")
                canonical = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
                if str(fingerprint or "").startswith("h1:"):
                    if not hmac.compare_digest(
                        str(fingerprint), _telegram_payload_fingerprint(canonical),
                    ):
                        raise ValueError("legacy_hmac")
                elif not hmac.compare_digest(
                    str(fingerprint or ""),
                    hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                ):
                    raise ValueError("legacy_sha")
                migrated_status = "pending" if status == "processing" else status
                await db.execute(
                    "UPDATE telegram_update_inbox SET payload_json=?,payload_sha256=?,"
                    "status=?,locked_by=NULL,locked_at=NULL,"
                    "dead_at=CASE WHEN ?='dead' THEN COALESCE(dead_at,processed_at,received_at) "
                    "ELSE dead_at END WHERE update_id=?",
                    (
                        _encrypt_telegram_payload(canonical),
                        _telegram_payload_fingerprint(canonical), migrated_status,
                        migrated_status, update_id,
                    ),
                )
            except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                await db.execute(
                    "UPDATE telegram_update_inbox SET payload_json=NULL,status='dead',"
                    "last_error='legacy_payload_invalid',locked_by=NULL,locked_at=NULL,"
                    "dead_at=COALESCE(dead_at,processed_at,received_at,?) WHERE update_id=?",
                    (now_iso(), update_id),
                )
        await db.execute(
            "UPDATE telegram_update_inbox SET dead_at=COALESCE(dead_at,processed_at,received_at) "
            "WHERE status='dead' AND dead_at IS NULL"
        )
        # Old effect keys included raw Telegram IDs. The update_id already scopes
        # uniqueness, so the target identifier is unnecessary.
        old_effects = await (await db.execute(
            "SELECT update_id,effect_key FROM telegram_update_effects "
            "WHERE effect_key GLOB 'chat_xp:*:*'"
        )).fetchall()
        for effect_update_id, old_key in old_effects:
            safe_key = ":".join(str(old_key).split(":")[:2])
            await db.execute(
                "INSERT OR IGNORE INTO telegram_update_effects "
                "(update_id,effect_key,created_at) "
                "SELECT update_id,?,created_at FROM telegram_update_effects "
                "WHERE update_id=? AND effect_key=?",
                (safe_key, effect_update_id, old_key),
            )
            await db.execute(
                "DELETE FROM telegram_update_effects WHERE update_id=? AND effect_key=?",
                (effect_update_id, old_key),
            )
        member_award_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(member_awards)")
            ).fetchall()
        }
        for name, sql_type in (
            ("operation_id", "TEXT"),
            ("revoked_at", "TEXT"),
            ("revoked_by", "INTEGER"),
            ("revoke_note", "TEXT"),
            ("revoke_operation_id", "TEXT"),
            ("revoke_request_hash", "TEXT"),
        ):
            if name not in member_award_columns:
                await db.execute(
                    f"ALTER TABLE member_awards ADD COLUMN {name} {sql_type}")
        published_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(published_posts)")
            ).fetchall()
        }
        if "operation_id" not in published_columns:
            await db.execute("ALTER TABLE published_posts ADD COLUMN operation_id TEXT")
        outbox_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(task_outbox)")
            ).fetchall()
        }
        for name, sql_type in (
            ("media_id", "TEXT"),
            ("telegram_message_id", "INTEGER"),
            ("telegram_thread_id", "INTEGER"),
        ):
            if name not in outbox_columns:
                await db.execute(
                    f"ALTER TABLE task_outbox ADD COLUMN {name} {sql_type}"
                )
        media_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(media_objects)")
            ).fetchall()
        }
        for name, sql_type in (
            ("reconcile_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("version_id", "TEXT"),
            ("checked_at", "TEXT"),
        ):
            if name not in media_columns:
                await db.execute(
                    f"ALTER TABLE media_objects ADD COLUMN {name} {sql_type}"
                )
        dispute_columns = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(task_disputes)")
            ).fetchall()
        }
        for name in ("reconciliation_reason", "reconciliation_reference"):
            if name not in dispute_columns:
                await db.execute(f"ALTER TABLE task_disputes ADD COLUMN {name} TEXT")
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
            "CREATE INDEX IF NOT EXISTS idx_task_templates_status "
            "ON task_templates(status,id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_template_versions_template "
            "ON task_template_versions(template_id,version_number)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_template_versions_media "
            "ON task_template_versions(photo_media_id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_template_events_template "
            "ON task_template_events(template_id,generation,id)")
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
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_assignment_one_active "
            "ON task_assignments(task_id, user_id) "
            "WHERE status IN ('claimed','review')")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_assignment_one_done "
            "ON task_assignments(task_id, user_id) WHERE status='done'")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_assignment_decision_operation "
            "ON task_assignments(decision_operation_id) "
            "WHERE decision_operation_id IS NOT NULL")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_disputes_status "
            "ON task_disputes(status, opened_at, id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_manual_grants_maker_time "
            "ON manual_grant_commands(maker_id, created_at)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_manual_grants_recipient_time "
            "ON manual_grant_commands(user_id, created_at)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_manual_grant_reversals_status "
            "ON manual_grant_reversals(status, requested_at, id)")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_grant_one_pending_reversal "
            "ON manual_grant_reversals(grant_operation_id) "
            "WHERE status IN ('pending','manual_required')")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_role_changes_status "
            "ON admin_role_changes(status, requested_at, id)")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_role_change_one_pending "
            "ON admin_role_changes(user_id) WHERE status='pending'")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_access_one_active "
            "ON staff_access_grants(user_id,preset,origin) WHERE status='active'")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_staff_grant_capability "
            "ON staff_grant_capabilities(capability,grant_id)")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_access_one_pending "
            "ON staff_access_changes(target_user_id,preset) WHERE status='pending'")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_staff_access_changes_status "
            "ON staff_access_changes(status,requested_at,id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_withdrawals_user "
            "ON withdrawal_requests(user_id, created_at)")
        await db.execute("DROP INDEX IF EXISTS idx_withdrawals_one_pending")
        await db.execute(
            "CREATE UNIQUE INDEX idx_withdrawals_one_pending "
            "ON withdrawal_requests(user_id) "
            "WHERE status IN ('pending','processing')")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_withdrawals_operation "
            "ON withdrawal_requests(operation_id) WHERE operation_id IS NOT NULL")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_withdrawals_decision_operation "
            "ON withdrawal_requests(decision_operation_id) "
            "WHERE decision_operation_id IS NOT NULL")
        completed_refs = await (await db.execute(
            "SELECT id, external_reference FROM withdrawal_requests "
            "WHERE status='completed' AND external_reference IS NOT NULL "
            "AND external_reference_canonical IS NULL"
        )).fetchall()
        for withdrawal_id, reference in completed_refs:
            await db.execute(
                "UPDATE withdrawal_requests SET external_reference_canonical=? WHERE id=?",
                (_canonical_external_reference(reference), withdrawal_id),
            )
        duplicate_ref = await (await db.execute(
            "SELECT provider, external_reference_canonical, COUNT(*) "
            "FROM withdrawal_requests WHERE status='completed' "
            "AND external_reference_canonical IS NOT NULL "
            "GROUP BY provider, external_reference_canonical HAVING COUNT(*)>1 LIMIT 1"
        )).fetchone()
        if duplicate_ref:
            raise RuntimeError(
                "Duplicate canonical withdrawal reference requires manual reconciliation"
            )
        await db.execute("DROP INDEX IF EXISTS idx_withdrawals_external_reference")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_withdrawals_external_reference_canonical "
            "ON withdrawal_requests(provider, external_reference_canonical) "
            "WHERE status='completed' AND external_reference_canonical IS NOT NULL")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_withdrawal_events_request "
            "ON withdrawal_events(withdrawal_id, id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_user ON bonus_ledger(user_id)")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_operation "
            "ON bonus_ledger(operation_id) WHERE operation_id IS NOT NULL")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_reversal_origin "
            "ON bonus_ledger(reversal_of_ledger_id) "
            "WHERE reversal_of_ledger_id IS NOT NULL")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_operation "
            "ON tasks(operation_id) WHERE operation_id IS NOT NULL")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_evidence_task "
            "ON task_evidence(task_id, assignment_id, user_id)")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_completion_operation "
            "ON tasks(completion_operation_id) "
            "WHERE completion_operation_id IS NOT NULL")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_cancel_operation "
            "ON tasks(cancel_operation_id) "
            "WHERE cancel_operation_id IS NOT NULL")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_assignments_completion_operation "
            "ON task_assignments(completion_operation_id) "
            "WHERE completion_operation_id IS NOT NULL")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_assignments_release_operation "
            "ON task_assignments(release_operation_id) "
            "WHERE release_operation_id IS NOT NULL")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_member_awards_user "
            "ON member_awards(user_id, granted_at)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_member_awards_maker_time "
            "ON member_awards(granted_by, granted_at)")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_member_awards_operation "
            "ON member_awards(operation_id) WHERE operation_id IS NOT NULL")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_member_awards_revoke_operation "
            "ON member_awards(revoke_operation_id) "
            "WHERE revoke_operation_id IS NOT NULL")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_award_reversals_status "
            "ON award_reversals(status,requested_at,id)")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_award_reversal_one_pending "
            "ON award_reversals(member_award_id) "
            "WHERE status IN ('pending','manual_required')")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_award_reversal_one_applied "
            "ON award_reversals(member_award_id) WHERE status='applied'")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_award_reversal_events_reversal "
            "ON award_reversal_events(reversal_id,id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_outbox_delivery "
            "ON task_outbox(status, available_at, id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_join_requests_user_status "
            "ON telegram_join_requests(user_id,status,requested_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegram_inbox_delivery "
            "ON telegram_update_inbox(status, available_at, update_id)")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_inbox_redrive_operation "
            "ON telegram_update_inbox(redrive_operation_id) "
            "WHERE redrive_operation_id IS NOT NULL")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_gc "
            "ON media_objects(state,delete_after)"
        )
        # v2.6.1: legacy dedupe keys could contain raw Telegram IDs.
        await db.execute(
            "UPDATE product_events SET dedupe_key=NULL "
            "WHERE dedupe_key IS NOT NULL AND dedupe_key NOT LIKE 'h1:%'"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_product_events_dedupe "
            "ON product_events(dedupe_key) WHERE dedupe_key IS NOT NULL")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_events_funnel "
            "ON product_events(event_name, occurred_at)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_events_subject "
            "ON product_events(subject_id, occurred_at)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_events_task "
            "ON product_events(task_id, occurred_at) WHERE task_id IS NOT NULL")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_events_expiry "
            "ON product_events(expires_at)")
        await db.execute(
            "UPDATE withdrawal_requests SET status='completed' "
            "WHERE status='approved'")
        await db.execute(
            "UPDATE withdrawal_requests SET status='rejected_refunded' "
            "WHERE status='rejected'")

        # Старые многоразовые задания не должны оставаться с бесконечным
        # количеством исполнителей и неограниченным финансовым обязательством.
        await db.execute(
            "UPDATE tasks SET "
            "max_participants=COALESCE(max_participants, MAX(1, "
            "(SELECT COUNT(*) FROM task_assignments a WHERE a.task_id=tasks.id))), "
            "budget_cap=COALESCE(budget_cap, reward * COALESCE(max_participants, "
            "MAX(1, (SELECT COUNT(*) FROM task_assignments a WHERE a.task_id=tasks.id)))) "
            "WHERE repeatable=1 AND (max_participants IS NULL OR budget_cap IS NULL)"
        )

        # Каноническая модель v2.6: каждое выполнение — assignment, включая
        # одноразовые задания старых версий. ID сохраняются, фото привязываются
        # к созданному выполнению, а task становится только контейнером оффера.
        await db.execute(
            "UPDATE task_assignments SET "
            "reward_snapshot=COALESCE(reward_snapshot, "
            "(SELECT reward FROM tasks WHERE tasks.id=task_assignments.task_id)), "
            "due_at=COALESCE(due_at, "
            "(SELECT slot_end FROM tasks WHERE tasks.id=task_assignments.task_id))"
        )
        await db.execute("""
            INSERT INTO task_assignments (
                task_id, user_id, status, claimed_at, done_at, proof_note,
                review_note, completion_operation_id, completion_request_hash,
                submission_attempt, reward_snapshot, due_at, terminal_at
            )
            SELECT t.id, t.claimed_by,
                CASE t.status
                    WHEN 'review' THEN 'review'
                    WHEN 'done' THEN 'done'
                    ELSE 'claimed'
                END,
                COALESCE(t.claimed_at, t.created_at, ?),
                t.done_at, t.proof_note, t.review_note,
                t.completion_operation_id, t.completion_request_hash,
                COALESCE(t.submission_attempt, 0), t.reward, t.slot_end,
                CASE WHEN t.status='done' THEN COALESCE(t.done_at, ?) END
            FROM tasks t
            WHERE t.repeatable=0 AND t.claimed_by IS NOT NULL
              AND t.status IN ('claimed','review','done')
              AND NOT EXISTS (
                  SELECT 1 FROM task_assignments a
                  WHERE a.task_id=t.id AND a.user_id=t.claimed_by
              )
        """, (now_iso(), now_iso()))
        await db.execute("""
            UPDATE task_evidence SET assignment_id=(
                SELECT a.id FROM task_assignments a
                WHERE a.task_id=task_evidence.task_id
                  AND a.user_id=task_evidence.user_id
                ORDER BY a.id DESC LIMIT 1
            )
            WHERE assignment_id IS NULL AND kind='after'
              AND EXISTS (
                SELECT 1 FROM task_assignments a
                WHERE a.task_id=task_evidence.task_id
                  AND a.user_id=task_evidence.user_id
            )
        """)
        await db.execute("""
            UPDATE bonus_ledger SET assignment_id=CAST(
                substr(operation_id, length('task_reward:assignment:') + 1) AS INTEGER
            )
            WHERE assignment_id IS NULL
              AND operation_id LIKE 'task_reward:assignment:%'
              AND EXISTS (
                  SELECT 1 FROM task_assignments a
                  WHERE a.id=CAST(substr(
                      bonus_ledger.operation_id,
                      length('task_reward:assignment:') + 1
                  ) AS INTEGER)
                    AND a.task_id=bonus_ledger.task_id
              )
        """)
        ambiguous_legacy_reward = await (await db.execute("""
            SELECT l.id FROM bonus_ledger l
            WHERE l.assignment_id IS NULL
              AND l.operation_id LIKE 'task_reward:task:%'
              AND (SELECT COUNT(*) FROM task_assignments a
                   WHERE a.task_id=l.task_id)<>1
            LIMIT 1
        """)).fetchone()
        if ambiguous_legacy_reward:
            raise RuntimeError(
                "Ambiguous legacy task reward requires manual assignment reconciliation"
            )
        await db.execute("""
            UPDATE bonus_ledger SET assignment_id=(
                SELECT a.id FROM task_assignments a
                WHERE a.task_id=bonus_ledger.task_id LIMIT 1
            )
            WHERE assignment_id IS NULL
              AND operation_id LIKE 'task_reward:task:%'
        """)
        await db.execute("""
            UPDATE task_assignments SET terminal_by=(
                SELECT l.created_by FROM bonus_ledger l
                WHERE l.assignment_id=task_assignments.id
                  AND l.amount=task_assignments.reward_snapshot
                ORDER BY l.id DESC LIMIT 1
            )
            WHERE status='done' AND terminal_by IS NULL
              AND EXISTS (
                SELECT 1 FROM bonus_ledger l
                WHERE l.assignment_id=task_assignments.id
                  AND l.created_by IS NOT NULL
                  AND l.amount=task_assignments.reward_snapshot
              )
        """)
        await db.execute("""
            INSERT OR IGNORE INTO task_completion_commands
                (operation_id,assignment_id,request_hash,result_status,created_at)
            SELECT completion_operation_id,id,completion_request_hash,'review',
                   COALESCE(done_at,claimed_at,?)
            FROM task_assignments
            WHERE completion_operation_id IS NOT NULL
              AND completion_request_hash IS NOT NULL
        """, (now_iso(),))
        await db.execute(
            "UPDATE tasks SET status='closed' "
            "WHERE repeatable=0 AND status IN ('claimed','review','done') "
            "AND EXISTS (SELECT 1 FROM task_assignments a WHERE a.task_id=tasks.id)"
        )

        # Стартовые награды добавляются один раз по code. Если ответственный
        # их переименовал или отключил — повторный запуск ничего не перезапишет.
        for code, emoji, title, desc, bonus, repeatable in DEFAULT_AWARDS:
            await db.execute(
                "INSERT OR IGNORE INTO awards "
                "(code, emoji, title, description, bonus, repeatable, active, created_at) "
                "VALUES (?,?,?,?,?,?,1,?)",
                (code, emoji, title, desc, bonus, repeatable, now_iso()),
            )
        # A repeatable catalogue item may be issued repeatedly and therefore
        # cannot carry discretionary money without a separate approval flow.
        await db.execute("UPDATE awards SET repeatable=0 WHERE bonus>0 AND repeatable<>0")

        # Built-in templates are immutable version-1 seeds. Existing rows are
        # never overwritten; future catalogue changes must create a new version.
        for seed in TASK_TEMPLATES:
            template_id, version_id = _task_template_seed_ids(seed["key"])
            mode = seed.get("mode") or "open"
            reward = int(seed["reward"])
            max_participants = 10 if mode == "all" else 1
            budget_cap = reward * max_participants
            content = {
                "title": seed["title"], "task_type": seed["type"],
                "task_title": seed["task_title"],
                "details": seed.get("details") or "", "reward": reward,
                "mode": mode,
                "evidence_policy": _evidence_policy(
                    seed.get("evidence_policy") or "after_required"
                ),
                "max_participants": max_participants,
                "budget_cap": budget_cap, "photo_media_id": None,
                "photo_sha256": None,
            }
            content_hash = _task_template_content_hash(content)
            existing_template = await (await db.execute(
                "SELECT t.id,t.origin,v.id,v.content_hash FROM task_templates t "
                "LEFT JOIN task_template_versions v ON v.template_id=t.id "
                "AND v.version_number=1 WHERE t.key=?", (seed["key"],),
            )).fetchone()
            if existing_template:
                if (
                    existing_template[0] != template_id
                    or existing_template[1] != "system"
                    or existing_template[2] != version_id
                    or existing_template[3] != content_hash
                ):
                    raise RuntimeError(
                        f"Conflicting built-in task template seed: {seed['key']}"
                    )
                continue
            operation_id = f"task-template-seed:{seed['key']}:v1"
            request_hash = content_hash
            result = {
                "ok": True, "template_id": template_id,
                "generation": 1, "version_id": version_id,
                "version_number": 1, "status": "active",
                "idempotent": False,
            }
            after = {
                "id": template_id, "key": seed["key"], "origin": "system",
                "status": "active", "generation": 1,
                "current_version_id": version_id, "version": {
                    **content, "id": version_id, "version_number": 1,
                    "content_hash": content_hash,
                },
            }
            await db.execute(
                "INSERT INTO task_templates "
                "(id,key,origin,status,generation,current_version_id,created_by,"
                "created_at,updated_by,updated_at) "
                "VALUES (?,?,'system','active',1,?,NULL,?,NULL,?)",
                (template_id, seed["key"], version_id,
                 TASK_TEMPLATE_SEED_AT, TASK_TEMPLATE_SEED_AT),
            )
            await db.execute(
                "INSERT INTO task_template_versions "
                "(id,template_id,version_number,title,task_type,task_title,details,"
                "reward,mode,evidence_policy,max_participants,budget_cap,photo_media_id,"
                "photo_sha256,content_hash,created_by,created_at) "
                "VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?, ?,NULL,?)",
                (
                    version_id, template_id, content["title"], content["task_type"],
                    content["task_title"], content["details"], content["reward"],
                    content["mode"], content["evidence_policy"],
                    content["max_participants"], content["budget_cap"], None, None,
                    content_hash, TASK_TEMPLATE_SEED_AT,
                ),
            )
            await db.execute(
                "INSERT INTO task_template_events "
                "(template_id,template_version_id,event_type,generation,actor_id,"
                "operation_id,request_hash,note,before_json,after_json,result_json,created_at) "
                "VALUES (?,?,'created',1,NULL,?,?, '',?,?,?,?)",
                (
                    template_id, version_id, operation_id, request_hash,
                    _canonical_json({}), _canonical_json(after),
                    _canonical_json(result), TASK_TEMPLATE_SEED_AT,
                ),
            )

        # Keep independent authority sources. A person may be both env-backed
        # and maker-checker promoted; rotating ADMIN_IDS removes only env power.
        for uid in ADMIN_IDS:
            await db.execute(
                "INSERT INTO members (user_id, role, status, created_at) "
                "VALUES (?, 'admin', 'approved', ?) "
                "ON CONFLICT(user_id) DO UPDATE SET role='admin', status='approved'",
                (uid, now_iso()),
            )
            await db.execute(
                "INSERT INTO admin_authorities "
                "(user_id,origin,granted_operation_id,granted_at) "
                "VALUES (?,'env',NULL,?) ON CONFLICT(user_id,origin) DO NOTHING",
                (uid, now_iso()),
            )
        if ADMIN_IDS:
            placeholders = ",".join("?" for _ in ADMIN_IDS)
            await db.execute(
                f"DELETE FROM admin_authorities WHERE origin='env' "
                f"AND user_id NOT IN ({placeholders})", tuple(sorted(ADMIN_IDS)),
            )
        else:
            await db.execute("DELETE FROM admin_authorities WHERE origin='env'")
        # Legacy role-only rows have unverifiable provenance and are revoked
        # fail-secure. Operators must explicitly restore them through ADMIN_IDS
        # or the maker-checker flow.
        await db.execute(
            "UPDATE members SET role='helper' WHERE role='admin' AND NOT EXISTS "
            "(SELECT 1 FROM admin_authorities aa WHERE aa.user_id=members.user_id)"
        )
        await db.execute(
            "UPDATE members SET role='admin',status='approved' WHERE EXISTS "
            "(SELECT 1 FROM admin_authorities aa WHERE aa.user_id=members.user_id)"
        )
        await _reconcile_legacy_owner_grants_in_tx(db)
        await db.execute(f"PRAGMA user_version={SQLITE_SCHEMA_VERSION}")
        await db.commit()
    logger.info("База БибиЗадачи готова.")


async def _reconcile_stale_media_uploads():
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM media_objects WHERE state='uploading' AND created_at<? "
            "ORDER BY created_at LIMIT 100",
            (cutoff,),
        )).fetchall()
    for row in rows:
        try:
            size, digest, version_id = await _storage_head_details(
                row["object_key"], backend=row["backend"],
            )
            if int(size) != int(row["size_bytes"]) or not hmac.compare_digest(
                str(digest), str(row["sha256"]),
            ):
                async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                    await db.execute(
                        "UPDATE media_objects SET state='quarantined',"
                        "last_error='checksum_mismatch',delete_after=? "
                        "WHERE id=? AND state='uploading'",
                        (now_iso(), row["id"]),
                    )
                    await db.commit()
                continue
            async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                await db.execute(
                    "UPDATE media_objects SET state='ready',ready_at=?,last_error=NULL,"
                    "version_id=COALESCE(?,version_id) "
                    "WHERE id=? AND state='uploading'",
                    (now_iso(), version_id, row["id"]),
                )
                await db.commit()
        except Exception as exc:
            attempts = int(row["reconcile_attempts"] or 0) + 1
            quarantine = attempts >= 5
            async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                await db.execute(
                    "UPDATE media_objects SET reconcile_attempts=?,last_error=?,"
                    "state=CASE WHEN ? THEN 'quarantined' ELSE state END,"
                    "delete_after=CASE WHEN ? THEN ? ELSE delete_after END "
                    "WHERE id=? AND state='uploading'",
                    (
                        attempts, type(exc).__name__, int(quarantine),
                        int(quarantine), now_iso(), row["id"],
                    ),
                )
                await db.commit()


async def _reconcile_referenced_media():
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT m.* FROM media_objects m WHERE m.state='ready' AND ("
            "EXISTS (SELECT 1 FROM tasks t WHERE t.photo_media_id=m.id) OR "
            "EXISTS (SELECT 1 FROM task_template_versions tv "
            "WHERE tv.photo_media_id=m.id) OR "
            "EXISTS (SELECT 1 FROM task_evidence e WHERE e.media_id=m.id) OR "
            "EXISTS (SELECT 1 FROM task_outbox o WHERE o.media_id=m.id "
            "AND o.status IN ('pending','sending','dead'))) "
            "ORDER BY m.checked_at IS NOT NULL,m.checked_at LIMIT 100"
        )).fetchall()
    for row in rows:
        error = None
        quarantine = False
        try:
            size, digest = await _storage_head(
                row["object_key"], backend=row["backend"],
            )
            if int(size) != int(row["size_bytes"]) or not hmac.compare_digest(
                str(digest), str(row["sha256"]),
            ):
                error = "checksum_mismatch"
                quarantine = True
        except Exception as exc:
            error = "missing" if _storage_error_is_missing(exc) else type(exc).__name__
            quarantine = _storage_error_is_missing(exc)
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            await db.execute(
                "UPDATE media_objects SET checked_at=?,last_error=?,"
                "state=CASE WHEN ? THEN 'quarantined' ELSE state END "
                "WHERE id=? AND state='ready'",
                (now_iso(), error, int(quarantine), row["id"]),
            )
            await db.commit()


async def _schedule_expired_evidence_media():
    """Detach expired photos while preserving hashes and financial/audit rows."""
    evidence_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=EVIDENCE_RETENTION_DAYS)
    ).isoformat()
    terminal_cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=EVIDENCE_RETENTION_DAYS + DISPUTE_OPEN_DAYS)
    ).isoformat()
    delete_at = now_iso()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        eligible_assignment_rows = await (await db.execute(
            "SELECT a.id FROM task_assignments a "
            "WHERE a.status IN ('done','rejected','released','cancelled','expired','reversed') "
            "AND a.terminal_at GLOB '????-??-??T??:??:??*' "
            "AND datetime(a.terminal_at) IS NOT NULL "
            "AND datetime(a.terminal_at)<datetime(?) "
            "AND NOT EXISTS (SELECT 1 FROM task_disputes d "
            "WHERE d.assignment_id=a.id "
            "AND d.status IN ('pending','manual_required')) "
            "AND NOT EXISTS (SELECT 1 FROM task_disputes d "
            "WHERE d.assignment_id=a.id AND (d.decided_at IS NULL "
            "OR d.decided_at NOT GLOB '????-??-??T??:??:??*' "
            "OR datetime(d.decided_at) IS NULL "
            "OR datetime(d.decided_at)>=datetime(?))) "
            "AND NOT EXISTS (SELECT 1 FROM task_outbox o "
            "WHERE o.event_key LIKE ('assignment:' || a.id || ':%') "
            "AND o.status IN ('pending','sending','dead'))",
            (terminal_cutoff, evidence_cutoff),
        )).fetchall()
        assignment_ids = [int(row["id"]) for row in eligible_assignment_rows]
        proof_rows = await (await db.execute(
            "SELECT e.id,e.media_id FROM task_evidence e "
            "JOIN task_assignments a ON a.id=e.assignment_id "
            "WHERE e.media_id IS NOT NULL "
            "AND a.status IN ('done','rejected','released','cancelled','expired','reversed') "
            "AND a.terminal_at GLOB '????-??-??T??:??:??*' "
            "AND datetime(a.terminal_at) IS NOT NULL "
            "AND datetime(a.terminal_at)<datetime(?) "
            "AND NOT EXISTS (SELECT 1 FROM task_disputes d "
            "WHERE d.assignment_id=a.id "
            "AND d.status IN ('pending','manual_required')) "
            "AND NOT EXISTS (SELECT 1 FROM task_disputes d "
            "WHERE d.assignment_id=a.id AND (d.decided_at IS NULL "
            "OR d.decided_at NOT GLOB '????-??-??T??:??:??*' "
            "OR datetime(d.decided_at) IS NULL "
            "OR datetime(d.decided_at)>=datetime(?))) "
            "AND NOT EXISTS (SELECT 1 FROM task_outbox o "
            "WHERE o.media_id=e.media_id "
            "AND o.status IN ('pending','sending','dead')) "
            "AND NOT EXISTS (SELECT 1 FROM task_outbox o "
            "WHERE o.event_key LIKE ('assignment:' || a.id || ':%') "
            "AND o.status IN ('pending','sending','dead'))",
            (terminal_cutoff, evidence_cutoff),
        )).fetchall()
        proof_ids = [int(row["id"]) for row in proof_rows]
        proof_media = {str(row["media_id"]) for row in proof_rows}
        for evidence_id in proof_ids:
            await db.execute(
                "UPDATE task_evidence SET media_id=NULL,photo_file='' "
                "WHERE id=? AND media_id IS NOT NULL",
                (evidence_id,),
            )
        for assignment_id in assignment_ids:
            await db.execute(
                "UPDATE task_assignments SET proof_note=NULL,review_note=NULL,"
                "release_reason=NULL WHERE id=?",
                (assignment_id,),
            )
            await db.execute(
                "UPDATE task_disputes SET reason='',reconciliation_reason=NULL,"
                "reconciliation_reference=NULL,decision_note=NULL "
                "WHERE assignment_id=? AND status NOT IN "
                "('pending','manual_required')",
                (assignment_id,),
            )
            await db.execute(
                "UPDATE task_outbox SET payload_json='{\"redacted\":\"retention\"}',"
                "media_id=NULL WHERE status='sent' "
                "AND event_key LIKE ('assignment:' || ? || ':%')",
                (assignment_id,),
            )

        brief_rows = await (await db.execute(
            "SELECT t.id,t.photo_media_id FROM tasks t "
            "WHERE ((t.status='cancelled' "
            "AND t.cancelled_at GLOB '????-??-??T??:??:??*' "
            "AND datetime(t.cancelled_at) IS NOT NULL "
            "AND datetime(t.cancelled_at)<datetime(?)) "
            "OR (t.status='expired' "
            "AND t.expired_at GLOB '????-??-??T??:??:??*' "
            "AND datetime(t.expired_at) IS NOT NULL "
            "AND datetime(t.expired_at)<datetime(?)) "
            "OR (t.status='closed' AND ((EXISTS (SELECT 1 FROM task_assignments a "
            "WHERE a.task_id=t.id) AND NOT EXISTS (SELECT 1 FROM task_assignments a "
            "WHERE a.task_id=t.id AND (a.status NOT IN "
            "('done','rejected','released','cancelled','expired','reversed') "
            "OR a.terminal_at NOT GLOB '????-??-??T??:??:??*' "
            "OR datetime(a.terminal_at) IS NULL "
            "OR datetime(a.terminal_at)>=datetime(?)))) "
            "OR (NOT EXISTS (SELECT 1 FROM task_assignments a WHERE a.task_id=t.id) "
            "AND t.done_at GLOB '????-??-??T??:??:??*' "
            "AND datetime(t.done_at) IS NOT NULL "
            "AND datetime(t.done_at)<datetime(?))))) "
            "AND NOT EXISTS (SELECT 1 FROM task_assignments a "
            "WHERE a.task_id=t.id AND (a.status NOT IN "
            "('done','rejected','released','cancelled','expired','reversed') "
            "OR a.terminal_at NOT GLOB '????-??-??T??:??:??*' "
            "OR datetime(a.terminal_at) IS NULL "
            "OR datetime(a.terminal_at)>=datetime(?))) "
            "AND NOT EXISTS (SELECT 1 FROM task_disputes d "
            "WHERE d.task_id=t.id "
            "AND d.status IN ('pending','manual_required')) "
            "AND NOT EXISTS (SELECT 1 FROM task_disputes d "
            "WHERE d.task_id=t.id AND (d.decided_at IS NULL "
            "OR d.decided_at NOT GLOB '????-??-??T??:??:??*' "
            "OR datetime(d.decided_at) IS NULL "
            "OR datetime(d.decided_at)>=datetime(?))) "
            "AND NOT EXISTS (SELECT 1 FROM task_outbox o "
            "WHERE (o.media_id=t.photo_media_id "
            "OR o.event_key=('task:' || t.id || ':announcement')) "
            "AND o.status IN ('pending','sending','dead'))",
            (
                terminal_cutoff, terminal_cutoff, terminal_cutoff,
                terminal_cutoff, terminal_cutoff, evidence_cutoff,
            ),
        )).fetchall()
        brief_media = {
            str(row["photo_media_id"]) for row in brief_rows
            if row["photo_media_id"]
        }
        for row in brief_rows:
            await db.execute(
                "UPDATE tasks SET details=NULL,address=NULL,lat=NULL,lng=NULL,"
                "proof_note=NULL,review_note=NULL,cancel_reason=NULL,"
                "photo_media_id=NULL,photo_file=NULL "
                "WHERE id=? AND photo_media_id IS ?",
                (row["id"], row["photo_media_id"]),
            )
            if row["photo_media_id"]:
                await db.execute(
                    "UPDATE task_evidence SET media_id=NULL,photo_file='' "
                    "WHERE task_id=? AND kind='brief' AND media_id=?",
                    (row["id"], row["photo_media_id"]),
                )
            await db.execute(
                "UPDATE task_outbox SET payload_json='{\"redacted\":\"retention\"}',"
                "media_id=NULL WHERE status='sent' "
                "AND event_key=('task:' || ? || ':announcement')",
                (row["id"],),
            )

        await db.execute(
            "UPDATE telegram_join_requests SET manual_retry_reason=NULL,"
            "last_error=NULL WHERE status IN ('joined','declined') "
            "AND COALESCE(joined_at,decided_at,requested_at) "
            "GLOB '????-??-??T??:??:??*' "
            "AND datetime(COALESCE(joined_at,decided_at,requested_at)) "
            "IS NOT NULL AND datetime(COALESCE(joined_at,decided_at,requested_at))"
            "<datetime(?)",
            (evidence_cutoff,),
        )

        media_ids = sorted(proof_media | brief_media)
        for media_id in media_ids:
            await db.execute(
                "UPDATE media_objects SET delete_after=COALESCE(delete_after,?) "
                "WHERE id=? AND state IN ('ready','quarantined')",
                (delete_at, media_id),
            )
        await db.commit()
    return {
        "proof_records_detached": len(proof_ids),
        "task_briefs_detached": len(brief_rows),
        "media_scheduled": len(media_ids),
    }


async def _cleanup_media_objects():
    """Delete only old, unreferenced objects; never perform I/O in a DB transaction."""
    await _schedule_expired_evidence_media()
    await _reconcile_stale_media_uploads()
    await _reconcile_referenced_media()
    now = datetime.now(timezone.utc)
    orphan_before = (now - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT m.* FROM media_objects m WHERE "
            "((m.state='ready' AND (m.delete_after<=? OR m.created_at<?)) "
            "OR m.state IN ('delete_pending','quarantined')) "
            "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.photo_media_id=m.id) "
            "AND NOT EXISTS (SELECT 1 FROM task_template_versions tv "
            "WHERE tv.photo_media_id=m.id) "
            "AND NOT EXISTS (SELECT 1 FROM task_evidence e WHERE e.media_id=m.id) "
            "AND NOT EXISTS (SELECT 1 FROM task_outbox o WHERE o.media_id=m.id "
            "AND o.status IN ('pending','sending','dead')) LIMIT 100",
            (now.isoformat(), orphan_before),
        )).fetchall()
    for row in rows:
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            cur = await db.execute(
                "UPDATE media_objects SET state='delete_pending' WHERE id=? "
                "AND state IN ('ready','delete_pending','quarantined') "
                "AND NOT EXISTS (SELECT 1 FROM tasks WHERE photo_media_id=?) "
                "AND NOT EXISTS (SELECT 1 FROM task_template_versions "
                "WHERE photo_media_id=?) "
                "AND NOT EXISTS (SELECT 1 FROM task_evidence WHERE media_id=?) "
                "AND NOT EXISTS (SELECT 1 FROM task_outbox WHERE media_id=? "
                "AND status IN ('pending','sending','dead'))",
                (row["id"], row["id"], row["id"], row["id"], row["id"]),
            )
            await db.commit()
        if cur.rowcount != 1:
            continue
        try:
            await _storage_delete(
                row["object_key"], backend=row["backend"],
                version_id=row["version_id"],
            )
            async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                await db.execute(
                    "UPDATE media_objects SET state='deleted',deleted_at=?,last_error=NULL "
                    "WHERE id=? AND state='delete_pending'",
                    (now_iso(), row["id"]),
                )
                await db.commit()
        except Exception as exc:
            async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                await db.execute(
                    "UPDATE media_objects SET last_error=? "
                    "WHERE id=? AND state='delete_pending'",
                    (type(exc).__name__, row["id"]),
                )
                await db.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


ANALYTICS_EVENTS = {
    "group_join_requested", "group_join_request_expired",
    "group_join_request_retried", "group_member_joined", "group_member_left",
    "bot_started", "referral_bound", "referral_link_invalid",
    "miniapp_authenticated", "application_submitted", "application_resubmitted",
    "application_decided",
    "task_catalog_served", "task_created", "task_published",
    "task_announcement_retried", "task_claimed",
    "task_released", "task_cancelled", "task_expired", "proof_submitted",
    "task_reviewed", "task_reward_credited", "task_dispute_opened",
    "task_dispute_resolved", "city_change_requested", "city_change_decided",
    "manual_grant_credited", "manual_grant_reversal_requested",
    "manual_grant_reversal_resolved", "admin_role_change_requested",
    "admin_role_change_resolved", "award_granted", "award_revoked",
    "award_reversal_requested", "award_reversal_resolved",
    "withdrawal_requested",
    "withdrawal_decided", "referral_confirmed",
}


def _analytics_dedupe(value):
    """Never persist Telegram/user identifiers embedded in caller dedupe keys."""
    if value is None:
        return None
    configured = (os.getenv("ANALYTICS_SECRET", "") or "").encode("utf-8")
    key = configured or hashlib.sha256(
        b"bibitasks-analytics-v1:" + (BOT_TOKEN or "").encode("utf-8")
    ).digest()
    digest = hmac.new(key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"h1:{digest}"
ANALYTICS_SOURCES = {"group", "bot", "miniapp", "backend"}
ANALYTICS_PROPERTIES = {
    "entrypoint", "task_type", "evidence_policy", "repeatable",
    "photo_count_bucket", "available_count_bucket", "decision", "referral_format",
}


async def _analytics_subject_in_tx(db, user_id):
    if user_id is None:
        return None
    row = await (await db.execute(
        "SELECT subject_id FROM analytics_subjects WHERE user_id=?",
        (int(user_id),),
    )).fetchone()
    if row:
        return row[0]
    subject_id = str(uuid.uuid4())
    await db.execute(
        "INSERT OR IGNORE INTO analytics_subjects "
        "(subject_id,user_id,created_at) VALUES (?,?,?)",
        (subject_id, int(user_id), now_iso()),
    )
    row = await (await db.execute(
        "SELECT subject_id FROM analytics_subjects WHERE user_id=?",
        (int(user_id),),
    )).fetchone()
    return row[0]


async def _track_event_in_tx(
    db, event_name, source, *, user_id=None, session_id=None,
    task_id=None, assignment_id=None, outcome=None, reason_code=None,
    properties=None, dedupe_key=None, retention_days=90,
):
    """Пишет только разрешённые обезличенные свойства в той же транзакции."""
    if event_name not in ANALYTICS_EVENTS or source not in ANALYTICS_SOURCES:
        raise ValueError("Неизвестное аналитическое событие.")
    properties = properties or {}
    unknown = set(properties) - ANALYTICS_PROPERTIES
    if unknown:
        raise ValueError("Недопустимые свойства аналитики: " + ", ".join(sorted(unknown)))
    safe_properties = {}
    for key, value in properties.items():
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise ValueError("Свойства аналитики должны быть скалярными.")
        safe_properties[key] = value
    subject_id = await _analytics_subject_in_tx(db, user_id)
    occurred = datetime.now(timezone.utc)
    await db.execute(
        "INSERT OR IGNORE INTO product_events "
        "(event_id,occurred_at,event_name,source,subject_id,session_id,task_id,"
        "assignment_id,outcome,reason_code,properties_json,dedupe_key,expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()), occurred.isoformat(), event_name, source,
            subject_id, session_id, task_id, assignment_id, outcome, reason_code,
            json.dumps(safe_properties, ensure_ascii=False, separators=(",", ":")),
            _analytics_dedupe(dedupe_key),
            (occurred + timedelta(days=max(1, int(retention_days)))).isoformat(),
        ),
    )


async def cleanup_expired_analytics():
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            "DELETE FROM product_events WHERE expires_at<?", (now_iso(),),
        )
        await db.execute(
            "DELETE FROM analytics_subjects WHERE subject_id NOT IN "
            "(SELECT DISTINCT subject_id FROM product_events WHERE subject_id IS NOT NULL)"
        )
        current = datetime.now(timezone.utc)
        dead_payload_before = (current - timedelta(days=7)).isoformat()
        inbox_before = (current - timedelta(days=30)).isoformat()
        await db.execute(
            "UPDATE telegram_update_inbox SET payload_json=NULL "
            "WHERE status='dead' AND payload_json IS NOT NULL AND dead_at<?",
            (dead_payload_before,),
        )
        await db.execute(
            "DELETE FROM telegram_update_inbox "
            "WHERE (status='done' AND processed_at IS NOT NULL AND processed_at<?) "
            "OR (status='dead' AND payload_json IS NULL AND dead_at<?)",
            (inbox_before, inbox_before),
        )
        await db.execute(
            "DELETE FROM telegram_update_effects WHERE created_at<? "
            "OR update_id NOT IN (SELECT update_id FROM telegram_update_inbox)",
            (inbox_before,),
        )
        outbox_payload_before = (
            current - timedelta(days=30)
        ).isoformat()
        await db.execute(
            "UPDATE task_outbox SET payload_json='{\"redacted\":\"retention\"}',"
            "media_id=NULL WHERE status='sent' AND sent_at IS NOT NULL AND sent_at<? "
            "AND payload_json<>'{\"redacted\":\"retention\"}'",
            (outbox_payload_before,),
        )
        purge_before = (
            datetime.now(timezone.utc)
            - timedelta(days=WITHDRAW_ACCOUNT_RETENTION_DAYS)
        ).isoformat()
        rows = await (await db.execute(
            "SELECT id,status FROM withdrawal_requests "
            "WHERE status IN ('completed','rejected_refunded') "
            "AND decided_at IS NOT NULL AND decided_at<? "
            "AND account_ciphertext IS NOT NULL",
            (purge_before,),
        )).fetchall()
        for withdrawal_id, status in rows:
            purged_at = now_iso()
            await db.execute(
                "UPDATE withdrawal_requests SET account_ciphertext=NULL, "
                "account_purged_at=? WHERE id=? AND account_ciphertext IS NOT NULL",
                (purged_at, withdrawal_id),
            )
            await db.execute(
                "INSERT INTO withdrawal_events "
                "(withdrawal_id,event_type,from_status,to_status,created_at) "
                "VALUES (?,'account_purged',?,?,?)",
                (withdrawal_id, status, status, purged_at),
            )
        await db.commit()
    await _cleanup_media_objects()


async def _track_event(event_name, source, **kwargs):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await _track_event_in_tx(db, event_name, source, **kwargs)
        await db.commit()


async def _track_event_best_effort(event_name, source, **kwargs):
    """Observability must never break onboarding or a read-only user flow."""
    try:
        await _track_event(event_name, source, **kwargs)
    except Exception:
        logger.exception("Best-effort analytics event failed: %s", event_name)


def _tags_list(value):
    """Нормализует теги для поиска: без #, дублей и слишком длинных значений."""
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value or "").replace(";", ",").split(",")
    result = []
    seen = set()
    for item in raw:
        tag = " ".join(str(item).strip().lstrip("#").split())[:30]
        key = tag.casefold()
        if tag and key not in seen:
            result.append(tag)
            seen.add(key)
        if len(result) >= 12:
            break
    return result


def _has_exact_tag(value, expected):
    expected = str(expected or "").strip().lstrip("#").casefold()
    return bool(expected) and expected in {
        item.casefold() for item in _tags_list(value)
    }


def _city_display(value):
    """Canonical human-readable city without common Russian prefixes."""
    city = unicodedata.normalize("NFKC", str(value or ""))
    city = " ".join(city.strip().split())[:80]
    city = re.sub(r"^(?:г(?:ород)?\.?)[\s,-]+", "", city, flags=re.IGNORECASE)
    return city.strip(" .,-")[:80]


def _city_key(value):
    """Stable comparison key for legacy and free-text city values."""
    city = _city_display(value).casefold().replace("ё", "е")
    return "".join(character for character in city if character.isalnum())


def _operation_uuid(value):
    """Возвращает канонический UUID операции или None."""
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _request_fingerprint(data):
    """Стабильный hash нормализованной команды для idempotency conflict."""
    encoded = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _insert_access_grant_snapshot_in_tx(
    db, user_id, preset, origin, *, operation_id, granted_by=None,
    approved_by=None,
):
    """Create an immutable preset snapshot; existing active grants are idempotent."""
    if preset not in CAPABILITY_PRESETS or origin not in {"env", "manual"}:
        raise ValueError("invalid access grant")
    active = await (await db.execute(
        "SELECT id,generation FROM staff_access_grants "
        "WHERE user_id=? AND preset=? AND origin=? AND status='active'",
        (int(user_id), preset, origin),
    )).fetchone()
    if active:
        return int(active[0]), int(active[1]), False
    generation = int((await (await db.execute(
        "SELECT COALESCE(MAX(generation),0)+1 FROM staff_access_grants "
        "WHERE user_id=? AND preset=? AND origin=?",
        (int(user_id), preset, origin),
    )).fetchone())[0])
    cursor = await db.execute(
        "INSERT INTO staff_access_grants "
        "(user_id,preset,origin,status,policy_version,generation,granted_by,"
        "approved_by,grant_operation_id,granted_at) "
        "VALUES (?,?,?,'active',?,?,?,?,?,?)",
        (
            int(user_id), preset, origin, RBAC_POLICY_VERSION, generation,
            granted_by, approved_by, operation_id, now_iso(),
        ),
    )
    grant_id = int(cursor.lastrowid)
    await db.executemany(
        "INSERT INTO staff_grant_capabilities (grant_id,capability) VALUES (?,?)",
        [(grant_id, capability) for capability in sorted(CAPABILITY_PRESETS[preset])],
    )
    return grant_id, generation, True


async def _reconcile_legacy_owner_grants_in_tx(db):
    """Backfill owner snapshots from the rollback authority projection."""
    authorities = await (await db.execute(
        "SELECT aa.user_id,aa.origin,aa.granted_operation_id "
        "FROM admin_authorities aa JOIN members m ON m.user_id=aa.user_id "
        "WHERE m.status='approved' AND m.role='admin'"
    )).fetchall()
    authority_keys = {(int(row[0]), str(row[1])) for row in authorities}
    for user_id, origin, granted_operation_id in authorities:
        await _insert_access_grant_snapshot_in_tx(
            db, int(user_id), "owner", str(origin),
            operation_id=(
                f"rbac-v1-backfill:{int(user_id)}:{origin}:"
                f"{granted_operation_id or 'legacy'}"
            ),
        )
    active_owner_grants = await (await db.execute(
        "SELECT id,user_id,origin FROM staff_access_grants "
        "WHERE preset='owner' AND status='active'"
    )).fetchall()
    for grant_id, user_id, origin in active_owner_grants:
        if (int(user_id), str(origin)) in authority_keys:
            continue
        stamp = now_iso()
        await db.execute(
            "UPDATE staff_access_grants SET status='revoked',revoked_at=?,"
            "revoke_operation_id=? WHERE id=? AND status='active'",
            (stamp, f"rbac-v1-projection-revoke:{int(grant_id)}", int(grant_id)),
        )


async def _effective_staff_access_in_tx(db, user_id):
    rows = await (await db.execute(
        "SELECT DISTINCT g.preset,c.capability FROM staff_access_grants g "
        "JOIN staff_grant_capabilities c ON c.grant_id=g.id "
        "JOIN members m ON m.user_id=g.user_id "
        "WHERE g.user_id=? AND g.status='active' AND m.status='approved' "
        "AND (g.preset<>'owner' OR (m.role='admin' AND EXISTS (SELECT 1 FROM admin_authorities aa "
        "WHERE aa.user_id=g.user_id AND aa.origin=g.origin)))",
        (int(user_id),),
    )).fetchall()
    capabilities = {str(row[1]) for row in rows}
    # Policy-v1 snapshots only knew the coarse award.revoke capability.  Keep
    # them effective while all newly issued reviewer grants contain the split
    # request/decision capabilities explicitly.
    if "award.revoke" in capabilities:
        capabilities.update({"award.reversal.request", "award.reversal.decide"})
    return {
        "policy_version": RBAC_POLICY_VERSION,
        "presets": sorted({str(row[0]) for row in rows}),
        "capabilities": sorted(capabilities),
    }


async def _effective_staff_access(user_id):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        return await _effective_staff_access_in_tx(db, user_id)


async def _has_capability_in_tx(db, user_id, capability):
    if capability not in ALL_STAFF_CAPABILITIES:
        return False
    accepted = [capability]
    if capability in {"award.reversal.request", "award.reversal.decide"}:
        accepted.append("award.revoke")
    placeholders = ",".join("?" for _ in accepted)
    row = await (await db.execute(
        "SELECT 1 FROM staff_access_grants g "
        "JOIN staff_grant_capabilities c ON c.grant_id=g.id "
        "JOIN members m ON m.user_id=g.user_id "
        "WHERE g.user_id=? AND g.status='active' AND m.status='approved' "
        f"AND c.capability IN ({placeholders}) AND (g.preset<>'owner' OR (m.role='admin' AND EXISTS "
        "(SELECT 1 FROM admin_authorities aa WHERE aa.user_id=g.user_id "
        "AND aa.origin=g.origin))) LIMIT 1",
        (int(user_id), *accepted),
    )).fetchone()
    return bool(row)


async def _has_capability(user_id, capability):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        return await _has_capability_in_tx(db, user_id, capability)


async def _active_capability_holder_ids_in_tx(db, capability):
    if capability not in ALL_STAFF_CAPABILITIES:
        return set()
    accepted = [capability]
    if capability in {"award.reversal.request", "award.reversal.decide"}:
        accepted.append("award.revoke")
    placeholders = ",".join("?" for _ in accepted)
    rows = await (await db.execute(
        "SELECT DISTINCT g.user_id FROM staff_access_grants g "
        "JOIN staff_grant_capabilities c ON c.grant_id=g.id "
        "JOIN members m ON m.user_id=g.user_id "
        f"WHERE g.status='active' AND m.status='approved' AND c.capability IN ({placeholders}) "
        "AND (g.preset<>'owner' OR (m.role='admin' AND EXISTS (SELECT 1 FROM admin_authorities aa "
        "WHERE aa.user_id=g.user_id AND aa.origin=g.origin)))",
        accepted,
    )).fetchall()
    return {int(row[0]) for row in rows}


async def _admin_active_in_tx(db, admin_id):
    """Compatibility alias: any active capability opens the staff shell."""
    access = await _effective_staff_access_in_tx(db, admin_id)
    return bool(access["capabilities"])


async def _active_admin_ids_in_tx(db):
    """Compatibility list for presentation; writes use granular capabilities."""
    rows = await (await db.execute(
        "SELECT DISTINCT g.user_id FROM staff_access_grants g "
        "JOIN members m ON m.user_id=g.user_id "
        "WHERE g.status='active' AND m.status='approved' "
        "AND (g.preset<>'owner' OR (m.role='admin' AND EXISTS (SELECT 1 FROM admin_authorities aa "
        "WHERE aa.user_id=g.user_id AND aa.origin=g.origin)))"
    )).fetchall()
    return {int(row[0]) for row in rows}


async def _admin_task_has_independent_review_path_in_tx(
    db, creator_id, performer_id,
):
    """Require distinct actors for review and a possible later dispute."""
    reviewers = await _active_capability_holder_ids_in_tx(db, "task.review")
    dispute_checkers = await _active_capability_holder_ids_in_tx(
        db, "task.dispute.decide",
    )
    performer_id = int(performer_id)
    creator_id = int(creator_id) if creator_id is not None else None
    eligible_reviewers = reviewers - {performer_id, creator_id}
    return any(
        dispute_checkers - {performer_id, reviewer_id}
        for reviewer_id in eligible_reviewers
    )


async def _claim_operation_in_tx(db, operation_id, command_type, request_hash, actor_id):
    """Reserve a UUID globally so it cannot identify two command families."""
    existing = await (await db.execute(
        "SELECT command_type,request_hash,actor_id FROM operation_registry "
        "WHERE operation_id=?", (operation_id,),
    )).fetchone()
    if existing:
        exact = (
            existing["command_type"] == command_type
            and existing["request_hash"] == request_hash
            and int(existing["actor_id"]) == int(actor_id)
        )
        if not exact:
            return False
        domain_locations = {
            "task_create": ("tasks", "operation_id"),
            "task_review": ("task_review_commands", "operation_id"),
            "task_dispute_open": ("task_disputes", "open_operation_id"),
            "task_dispute_decide": ("task_disputes", "decision_operation_id"),
            "manual_grant": ("manual_grant_commands", "operation_id"),
            "manual_grant_reversal_request": (
                "manual_grant_reversals", "request_operation_id",
            ),
            "manual_grant_reversal_decision": (
                "manual_grant_reversals", "decision_operation_id",
            ),
            "admin_role_request": ("admin_role_changes", "request_operation_id"),
            "admin_role_decision": ("admin_role_changes", "decision_operation_id"),
            "staff_access_request": ("staff_access_changes", "request_operation_id"),
            "staff_access_decision": ("staff_access_changes", "decision_operation_id"),
            "task_template_create": ("task_template_events", "operation_id"),
            "task_template_version_create": ("task_template_events", "operation_id"),
            "task_template_status_change": ("task_template_events", "operation_id"),
            "award_grant": ("member_awards", "operation_id"),
            "award_revoke": ("member_awards", "revoke_operation_id"),
            "award_reversal_request": (
                "award_reversals", "request_operation_id",
            ),
            "award_reversal_decision": (
                "award_reversals", "decision_operation_id",
            ),
            "withdrawal_request": ("withdrawal_requests", "operation_id"),
            "withdrawal_decision": ("withdrawal_requests", "decision_operation_id"),
        }
        location = domain_locations.get(command_type)
        if location:
            domain = await (await db.execute(
                f"SELECT 1 FROM {location[0]} WHERE {location[1]}=? LIMIT 1",
                (operation_id,),
            )).fetchone()
            if not domain:
                return False
        return True
    legacy_checks = (
        ("tasks", "operation_id", "task_create"),
        ("task_review_commands", "operation_id", "task_review"),
        ("task_disputes", "open_operation_id", "task_dispute_open"),
        ("task_disputes", "decision_operation_id", "task_dispute_decide"),
        ("manual_grant_commands", "operation_id", "manual_grant"),
        (
            "manual_grant_reversals", "request_operation_id",
            "manual_grant_reversal_request",
        ),
        (
            "manual_grant_reversals", "decision_operation_id",
            "manual_grant_reversal_decision",
        ),
        ("admin_role_changes", "request_operation_id", "admin_role_request"),
        ("admin_role_changes", "decision_operation_id", "admin_role_decision"),
        ("staff_access_changes", "request_operation_id", "staff_access_request"),
        ("staff_access_changes", "decision_operation_id", "staff_access_decision"),
        ("member_awards", "operation_id", "award_grant"),
        ("member_awards", "revoke_operation_id", "award_revoke"),
        (
            "award_reversals", "request_operation_id",
            "award_reversal_request",
        ),
        (
            "award_reversals", "decision_operation_id",
            "award_reversal_decision",
        ),
        ("withdrawal_requests", "operation_id", "withdrawal_request"),
        ("withdrawal_requests", "decision_operation_id", "withdrawal_decision"),
    )
    owners = set()
    for table, column, owner in legacy_checks:
        found = await (await db.execute(
            f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (operation_id,),
        )).fetchone()
        if found:
            owners.add(owner)
    if owners and owners != {command_type}:
        return False
    await db.execute(
        "INSERT INTO operation_registry "
        "(operation_id,command_type,request_hash,actor_id,created_at) "
        "VALUES (?,?,?,?,?)",
        (operation_id, command_type, request_hash, actor_id, now_iso()),
    )
    return True


async def _discretionary_totals_in_tx(db, maker_id, user_id, cutoff):
    """Rolling positive discretionary totals across quick grants and awards."""
    maker_total = int((await (await db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM ("
        "SELECT amount FROM manual_grant_commands WHERE maker_id=? AND created_at>=? "
        "UNION ALL SELECT bonus AS amount FROM member_awards "
        "WHERE granted_by=? AND bonus>0 AND granted_at>=?)",
        (maker_id, cutoff, maker_id, cutoff),
    )).fetchone())[0] or 0)
    recipient_total = int((await (await db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM ("
        "SELECT amount FROM manual_grant_commands WHERE user_id=? AND created_at>=? "
        "UNION ALL SELECT bonus AS amount FROM member_awards "
        "WHERE user_id=? AND bonus>0 AND granted_at>=?)",
        (user_id, cutoff, user_id, cutoff),
    )).fetchone())[0] or 0)
    return maker_total, recipient_total


async def _reserved_bonus_in_tx(
    db, user_id, *, exclude_reversal_id=None, exclude_award_reversal_id=None,
):
    """Return liabilities that must survive every balance-decreasing command."""
    task_reserved = int((await (await db.execute(
        "SELECT COALESCE(SUM(CASE WHEN reward>0 THEN reward ELSE 0 END),0) "
        "FROM task_disputes WHERE user_id=? "
        "AND status IN ('pending','manual_required')",
        (int(user_id),),
    )).fetchone())[0] or 0)
    values = [int(user_id)]
    exclusion = ""
    if exclude_reversal_id is not None:
        exclusion = " AND id<>?"
        values.append(int(exclude_reversal_id))
    manual_reserved = int((await (await db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM manual_grant_reversals "
        "WHERE user_id=? AND status IN ('pending','manual_required')" + exclusion,
        values,
    )).fetchone())[0] or 0)
    award_values = [int(user_id)]
    award_exclusion = ""
    if exclude_award_reversal_id is not None:
        award_exclusion = " AND id<>?"
        award_values.append(int(exclude_award_reversal_id))
    award_reserved = int((await (await db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM award_reversals "
        "WHERE user_id=? AND status IN ('pending','manual_required')"
        + award_exclusion,
        award_values,
    )).fetchone())[0] or 0)
    return task_reserved + manual_reserved + award_reserved


EVIDENCE_POLICY_ALIASES = {
    "none": "none",
    "comment_only": "comment_only",
    "after_required": "photo_required",
    "photo_required": "photo_required",
    "before_after": "before_after",
    "before_and_after_required": "before_after",
}


def _evidence_policy(value):
    """Нормализует старые и frontend-названия политики фотоотчёта."""
    return EVIDENCE_POLICY_ALIASES.get(str(value or "none").strip().lower())


def _public_evidence_policy(value):
    return {
        "photo_required": "after_required",
        "before_after": "before_and_after_required",
        "comment_only": "comment_only",
        "none": "none",
    }.get(_evidence_policy(value), "none")


def _storage_object_key(filename):
    if (
        not filename or os.path.basename(filename) != filename
        or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for ch in filename.lower())
    ):
        raise ValueError("Unsafe media object key")
    return f"{S3_PREFIX}/{filename}" if S3_PREFIX else filename


def _storage_error_is_missing(exc):
    if isinstance(exc, FileNotFoundError):
        return True
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = str((response.get("Error") or {}).get("Code") or "")
    return code in ("NoSuchKey", "NoSuchVersion", "404", "NotFound")


def _s3_client(*, public=False):
    cache_key = "public" if public else "internal"
    if cache_key in _s3_clients:
        return _s3_clients[cache_key]
    import boto3
    from botocore.config import Config
    endpoint = S3_PUBLIC_ENDPOINT_URL if public else S3_ENDPOINT_URL
    client = boto3.client(
        "s3", region_name=S3_REGION, endpoint_url=endpoint or None,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": S3_ADDRESSING_STYLE},
            connect_timeout=3, read_timeout=10, retries={"max_attempts": 3},
        ),
    )
    _s3_clients[cache_key] = client
    return client


async def _storage_put(filename, content, sha256, *, backend=None):
    _storage_object_key(filename)
    backend = backend or MEDIA_STORAGE
    if backend == "local":
        path = os.path.join(TASK_PHOTO_DIR, filename)
        if os.path.islink(TASK_PHOTO_DIR) or os.path.islink(path):
            raise ValueError("Symlinks are not allowed in local media storage")

        def write_photo():
            if os.path.exists(path):
                with open(path, "rb") as existing:
                    if hashlib.sha256(existing.read()).hexdigest() == sha256:
                        return
                raise ValueError("Media object key collision")
            temporary = f"{path}.tmp-{secrets.token_hex(8)}"
            with open(temporary, "xb") as output:
                os.chmod(temporary, 0o600)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)

        await asyncio.to_thread(write_photo)
        return None

    def upload():
        kwargs = {
            "Bucket": S3_BUCKET, "Key": _storage_object_key(filename),
            "Body": content, "ContentType": "image/jpeg",
            "Metadata": {"sha256": sha256},
        }
        if S3_SSE:
            kwargs["ServerSideEncryption"] = S3_SSE
        return (_s3_client().put_object(**kwargs) or {}).get("VersionId")

    return await asyncio.to_thread(upload)


async def _storage_head_details(filename, *, backend=None):
    _storage_object_key(filename)
    backend = backend or MEDIA_STORAGE
    if backend == "local":
        path = os.path.join(TASK_PHOTO_DIR, filename)
        if os.path.islink(TASK_PHOTO_DIR) or os.path.islink(path):
            raise ValueError("Symlinks are not allowed in local media storage")

        def inspect():
            with open(path, "rb") as source:
                content = source.read()
            return len(content), hashlib.sha256(content).hexdigest()

        size, digest = await asyncio.to_thread(inspect)
        return size, digest, None

    def inspect():
        response = _s3_client().head_object(
            Bucket=S3_BUCKET, Key=_storage_object_key(filename),
        )
        return (
            int(response["ContentLength"]),
            str((response.get("Metadata") or {}).get("sha256") or ""),
            response.get("VersionId"),
        )

    return await asyncio.to_thread(inspect)


async def _storage_head(filename, *, backend=None):
    size, digest, _version_id = await _storage_head_details(
        filename, backend=backend,
    )
    return size, digest


async def _storage_delete(filename, *, backend=None, version_id=None):
    _storage_object_key(filename)
    backend = backend or MEDIA_STORAGE
    if backend == "local":
        path = os.path.join(TASK_PHOTO_DIR, filename)
        if os.path.islink(TASK_PHOTO_DIR) or os.path.islink(path):
            raise ValueError("Symlinks are not allowed in local media storage")
        try:
            await asyncio.to_thread(
                os.remove, path,
            )
        except FileNotFoundError:
            pass
        return
    object_key = _storage_object_key(filename)

    def list_exact_versions(client):
        found = []
        request = {"Bucket": S3_BUCKET, "Prefix": object_key}
        while True:
            response = client.list_object_versions(**request)
            for group in ("Versions", "DeleteMarkers"):
                for item in response.get(group) or []:
                    if item.get("Key") == object_key and item.get("VersionId") is not None:
                        found.append(str(item["VersionId"]))
            if not response.get("IsTruncated"):
                break
            next_key = response.get("NextKeyMarker")
            if not next_key:
                raise RuntimeError("S3 version listing pagination is incomplete")
            request["KeyMarker"] = next_key
            next_version = response.get("NextVersionIdMarker")
            if next_version:
                request["VersionIdMarker"] = next_version
            else:
                request.pop("VersionIdMarker", None)
        return list(dict.fromkeys(found))

    def delete_all_versions():
        client = _s3_client()
        versions = list_exact_versions(client)
        if version_id and str(version_id) not in versions:
            versions.append(str(version_id))
        if versions:
            for stored_version in versions:
                client.delete_object(
                    Bucket=S3_BUCKET, Key=object_key, VersionId=stored_version,
                )
        else:
            try:
                client.head_object(Bucket=S3_BUCKET, Key=object_key)
            except Exception as exc:
                if not _storage_error_is_missing(exc):
                    raise
            else:
                client.delete_object(Bucket=S3_BUCKET, Key=object_key)
        if list_exact_versions(client):
            raise RuntimeError("S3 object versions remain after deletion")
        try:
            client.head_object(Bucket=S3_BUCKET, Key=object_key)
        except Exception as exc:
            if _storage_error_is_missing(exc):
                return
            raise
        raise RuntimeError("S3 object still exists after deletion")

    await asyncio.to_thread(delete_all_versions)


async def _storage_read(filename, *, backend=None):
    _storage_object_key(filename)
    backend = backend or MEDIA_STORAGE
    if backend == "local":
        path = os.path.join(TASK_PHOTO_DIR, filename)
        if os.path.islink(TASK_PHOTO_DIR) or os.path.islink(path):
            raise ValueError("Symlinks are not allowed in local media storage")

        def read_local():
            with open(path, "rb") as source:
                return source.read()

        return await asyncio.to_thread(read_local)

    def download():
        response = _s3_client().get_object(
            Bucket=S3_BUCKET, Key=_storage_object_key(filename),
        )
        try:
            return response["Body"].read()
        finally:
            response["Body"].close()

    return await asyncio.to_thread(download)


async def _storage_healthcheck():
    if MEDIA_STORAGE == "local":
        return bool(
            os.path.isdir(DATA_DIR) and os.access(DATA_DIR, os.W_OK)
            and os.path.isdir(TASK_PHOTO_DIR) and os.access(TASK_PHOTO_DIR, os.W_OK)
        )
    try:
        await asyncio.to_thread(_s3_client().head_bucket, Bucket=S3_BUCKET)
        if S3_PRIVACY_MODE == "public_access_block":
            block = await asyncio.to_thread(
                _s3_client().get_public_access_block, Bucket=S3_BUCKET,
            )
            settings = (block or {}).get("PublicAccessBlockConfiguration") or {}
            if not all(settings.get(name) is True for name in (
                "BlockPublicAcls", "IgnorePublicAcls",
                "BlockPublicPolicy", "RestrictPublicBuckets",
            )):
                return False
        filename = f"health-{uuid.uuid4()}.bin"
        content = secrets.token_bytes(32)
        digest = hashlib.sha256(content).hexdigest()
        version_id = None
        try:
            version_id = await _storage_put(
                filename, content, digest, backend="s3",
            )
            size, stored_digest = await _storage_head(filename, backend="s3")
            restored = await _storage_read(filename, backend="s3")
            if (
                size != len(content)
                or not hmac.compare_digest(stored_digest, digest)
                or not hmac.compare_digest(restored, content)
            ):
                return False
        finally:
            await _storage_delete(
                filename, backend="s3", version_id=version_id,
            )
        return True
    except Exception:
        return False


async def _remove_saved_images(items):
    for item in items or []:
        media_id = item.get("media_id") if isinstance(item, dict) else None
        if media_id:
            async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                await db.execute(
                    "UPDATE media_objects SET delete_after=COALESCE(delete_after,?) "
                    "WHERE id=? AND state='ready'",
                    ((datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(), media_id),
                )
                await db.commit()
            continue
        filename = item.get("photo_file") if isinstance(item, dict) else item
        if not filename:
            continue
        try:
            await _storage_delete(filename)
        except Exception:
            logger.warning("Failed to remove orphan media object")
            pass


class MediaProcessingBusy(RuntimeError):
    """The bounded image-normalization lane has no safe capacity left."""


def _media_normalizer():
    """Return the semaphore for the current server loop (also test-loop safe)."""
    global _media_normalize_semaphore, _media_normalize_loop
    loop = asyncio.get_running_loop()
    if _media_normalize_loop is not loop:
        if _media_capacity["active"] or _media_capacity["waiters"]:
            raise RuntimeError("media normalizer event loop changed while busy")
        _media_normalize_loop = loop
        _media_normalize_semaphore = asyncio.Semaphore(
            MEDIA_NORMALIZE_CONCURRENCY
        )
    return _media_normalize_semaphore


async def _normalize_media_bounded(normalizer):
    """Run Pillow off-loop with one active job and at most three waiters by default."""
    capacity = MEDIA_NORMALIZE_CONCURRENCY + MEDIA_NORMALIZE_MAX_WAITERS
    if _media_capacity["active"] + _media_capacity["waiters"] >= capacity:
        _media_capacity["rejected"] += 1
        raise MediaProcessingBusy("media_processing_busy")
    semaphore = _media_normalizer()
    # Reserve a bounded ticket before the first await. This prevents an
    # unbounded asyncio.Semaphore waiter list during a photo-upload burst.
    _media_capacity["waiters"] += 1
    try:
        try:
            await asyncio.wait_for(
                semaphore.acquire(),
                timeout=MEDIA_NORMALIZE_WAIT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError as exc:
            _media_capacity["rejected"] += 1
            raise MediaProcessingBusy("media_processing_busy") from exc
    finally:
        _media_capacity["waiters"] -= 1
    _media_capacity["active"] += 1

    async def run_normalizer():
        try:
            return await asyncio.to_thread(normalizer)
        finally:
            _media_capacity["active"] -= 1
            semaphore.release()

    # A cancelled/disconnected HTTP request cannot stop a thread already
    # executing Pillow. Shield the owned job so its slot remains reserved
    # until the actual thread completes; otherwise cancellation could exceed
    # the configured CPU/RAM concurrency in the background.
    job = asyncio.create_task(run_normalizer())
    _media_normalize_jobs.add(job)

    def forget_finished(completed):
        _media_normalize_jobs.discard(completed)
        if not completed.cancelled():
            # Mark a post-disconnect exception as retrieved. A live caller
            # awaiting the shield still receives the same result/exception.
            completed.exception()

    job.add_done_callback(forget_finished)
    return await asyncio.shield(job)


async def _save_image(
    data_url, *, purpose="task_proof", upload_operation_id=None, request_hash=None,
    admin_id=None, required_capability=None,
):
    """Декодирует, ограничивает и перекодирует фото без EXIF."""
    if not data_url:
        return None
    if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
        raise ValueError("Не удалось прочитать фотографию.")
    try:
        header, encoded = data_url.split(",", 1)
        if ";base64" not in header:
            raise ValueError
        declared_type = header.split(";", 1)[0].lower()
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Фотография повреждена. Выбери файл ещё раз.")
    if len(payload) > 2_500_000:
        raise ValueError("Фотография слишком большая. Максимум — 2,5 МБ.")
    if payload.startswith(b"\xff\xd8\xff"):
        actual_types = {"data:image/jpeg", "data:image/jpg"}
    elif payload.startswith(b"\x89PNG\r\n\x1a\n"):
        actual_types = {"data:image/png"}
    elif (
        len(payload) >= 12
        and payload[:4] == b"RIFF"
        and payload[8:12] == b"WEBP"
    ):
        actual_types = {"data:image/webp"}
    else:
        raise ValueError("Поддерживаются фотографии JPG, PNG и WebP.")
    if declared_type not in actual_types:
        raise ValueError("Формат фотографии не совпадает с её содержимым.")

    def normalize_photo():
        try:
            with Image.open(io.BytesIO(payload)) as source:
                width, height = source.size
                if width < 1 or height < 1 or width * height > 20_000_000:
                    raise ValueError("Слишком большое разрешение фотографии.")
                source.load()
                image = ImageOps.exif_transpose(source)
                if image.mode not in ("RGB", "L"):
                    background = Image.new("RGB", image.size, "white")
                    if "A" in image.getbands():
                        background.paste(image, mask=image.getchannel("A"))
                    else:
                        background.paste(image.convert("RGB"))
                    image = background
                else:
                    image = image.convert("RGB")
                image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(
                    output, format="JPEG", quality=85, optimize=True,
                    progressive=True,
                )
                result = output.getvalue()
                if len(result) > 2_500_000:
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=70, optimize=True)
                    result = output.getvalue()
                if len(result) > 2_500_000:
                    raise ValueError("Фотография слишком большая после обработки.")
                return result
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise ValueError("Фотография повреждена или имеет опасный размер.")

    normalized = await _normalize_media_bounded(normalize_photo)
    digest = hashlib.sha256(normalized).hexdigest()
    upload_operation_id = upload_operation_id or f"adhoc:{uuid.uuid4()}"
    request_hash = request_hash or digest
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if admin_id is not None and (
            not required_capability
            or not await _has_capability_in_tx(db, admin_id, required_capability)
        ):
            await db.rollback()
            raise PermissionError("admin_revoked")
        existing = await (await db.execute(
            "SELECT * FROM media_objects WHERE upload_operation_id=?",
            (upload_operation_id,),
        )).fetchone()
        if existing:
            if existing["request_hash"] != request_hash or existing["sha256"] != digest:
                await db.rollback()
                raise ValueError("Этот upload operation уже использован для другого фото.")
            media_id = existing["id"]
            filename = existing["object_key"]
            backend = existing["backend"]
            ready = existing["state"] == "ready"
            if ready:
                await db.rollback()
                return {
                    "media_id": media_id, "photo_file": filename, "sha256": digest,
                }
            await db.execute(
                "UPDATE media_objects SET state='uploading',last_error=NULL,"
                "reconcile_attempts=0,delete_after=NULL,deleted_at=NULL "
                "WHERE id=?",
                (media_id,),
            )
            await db.commit()
        else:
            media_id = str(uuid.uuid4())
            filename = f"{media_id}.jpg"
            backend = MEDIA_STORAGE
            await db.execute(
                "INSERT INTO media_objects "
                "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
                "upload_operation_id,request_hash,created_at) "
                "VALUES (?,?,?,?,'uploading','image/jpeg',?,?,?,?,?)",
                (
                    media_id, MEDIA_STORAGE, filename, purpose, len(normalized), digest,
                    upload_operation_id, request_hash, now_iso(),
                ),
            )
            await db.commit()
    try:
        version_id = await _storage_put(filename, normalized, digest, backend=backend)
        stored_size, stored_sha = await _storage_head(filename, backend=backend)
        if stored_size != len(normalized) or not hmac.compare_digest(stored_sha, digest):
            raise ValueError("Хранилище вернуло фотографию с неверной контрольной суммой.")
    except Exception as exc:
        _record_runtime_error(exc)
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            await db.execute(
                "UPDATE media_objects SET last_error=? WHERE id=? AND state='uploading'",
                (type(exc).__name__, media_id),
            )
            await db.commit()
        raise
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE media_objects SET state='ready',ready_at=?,last_error=NULL,version_id=? "
            "WHERE id=? AND state='uploading'",
            (now_iso(), version_id, media_id),
        )
        await db.commit()
    return {"media_id": media_id, "photo_file": filename, "sha256": digest}


async def _save_proof_photos(raw_photos, *, operation_id=None, request_hash=None):
    """Сохраняет от нуля до четырёх after-фото, удаляя частичный результат при ошибке."""
    if raw_photos in (None, ""):
        raw_photos = []
    if not isinstance(raw_photos, list):
        raise ValueError("Фотографии результата должны быть списком.")
    if len(raw_photos) > 4:
        raise ValueError("К одному отчёту можно прикрепить не больше четырёх фотографий.")
    saved = []
    try:
        for index, item in enumerate(raw_photos):
            data_url = item.get("data_url") if isinstance(item, dict) else item
            image = await _save_image(
                data_url, purpose="task_proof",
                upload_operation_id=(
                    f"proof:{operation_id}:{index}" if operation_id else None
                ),
                request_hash=(f"{request_hash}:{index}" if request_hash else None),
            )
            if not image:
                raise ValueError("Одна из фотографий результата пустая.")
            saved.append(image)
    except Exception:
        await _remove_saved_images(saved)
        raise
    return saved


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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        return await _admin_active_in_tx(db, uid)


_DEFAULT_IS_ADMIN = is_admin


async def _has_command_capability(user_id, capability):
    allowed = await _has_capability(user_id, capability)
    # Existing isolated command tests replace the legacy checker. Production
    # always uses the persisted capability snapshot.
    if (
        not allowed and BIBITASKS_ENVIRONMENT == "test"
        and is_admin is not _DEFAULT_IS_ADMIN
    ):
        return bool(await is_admin(user_id))
    return allowed


async def upsert_member(uid, **fields):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            "INSERT INTO members (user_id, created_at) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (uid, now_iso()))
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            await db.execute(
                f"UPDATE members SET {cols} WHERE user_id = ?",
                (*fields.values(), uid))
            if (
                BIBITASKS_ENVIRONMENT != "production"
                and fields.get("role") == "admin"
            ):
                await db.execute(
                    "INSERT INTO admin_authorities "
                    "(user_id,origin,granted_operation_id,granted_at) "
                    "VALUES (?,'manual','test-bootstrap',?) "
                    "ON CONFLICT(user_id,origin) DO NOTHING",
                    (uid, now_iso()),
                )
                await _insert_access_grant_snapshot_in_tx(
                    db, uid, "owner", "manual",
                    operation_id=f"test-bootstrap:{int(uid)}",
                )
        await db.commit()


async def add_bonus(uid, amount, reason, task_id=None, by=None, operation_id=None):
    """Меняет баланс один раз на operation_id и возвращает исходный результат повтора."""
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if operation_id:
            existing = await (await db.execute(
                "SELECT user_id, amount, reason, task_id, created_by, balance_after "
                "FROM bonus_ledger WHERE operation_id=?",
                (operation_id,),
            )).fetchone()
            if existing:
                same = (
                    int(existing["user_id"]) == int(uid)
                    and int(existing["amount"]) == int(amount)
                    and (existing["task_id"] == task_id)
                    and (existing["created_by"] == by)
                    and existing["reason"] == reason
                )
                if not same:
                    await db.rollback()
                    raise ValueError("Этот operation_id уже использован для другой операции.")
                balance = existing["balance_after"]
                if balance is None:
                    row = await (await db.execute(
                        "SELECT bonus FROM members WHERE user_id=?", (uid,)
                    )).fetchone()
                    balance = int(row[0]) if row else 0
                await db.rollback()
                return {"balance": int(balance), "replayed": True}
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
        if int(amount) < 0:
            reserved = await _reserved_bonus_in_tx(db, uid)
            if new_balance < reserved:
                await db.rollback()
                raise ValueError(
                    "Часть баланса зарезервирована до решения спора по заданию."
                )
        await db.execute(
            "UPDATE members SET bonus = ? WHERE user_id = ?", (new_balance, uid))
        await db.execute(
            "INSERT INTO bonus_ledger "
            "(user_id, amount, reason, task_id, created_by, created_at, "
            "operation_id, balance_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, amount, reason, task_id, by, now_iso(), operation_id, new_balance))
        await db.commit()
    return {"balance": new_balance, "replayed": False}


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


async def _confirm_referral_if_ready_in_tx(db, user_id, by=None):
    """Confirm a referral exactly once after approval and authoritative join."""
    row = await (await db.execute(
        "SELECT status,group_membership_status,referred_by,ref_confirmed "
        "FROM members WHERE user_id=?",
        (int(user_id),),
    )).fetchone()
    if not row:
        return None, 0, 0
    referred_by = row[2]
    if (
        row[0] != "approved" or row[1] != "member"
        or not referred_by or int(referred_by) == int(user_id) or int(row[3] or 0)
    ):
        return None, 0, 0
    changed = await db.execute(
        "UPDATE members SET ref_confirmed=1 WHERE user_id=? "
        "AND ref_confirmed=0 AND status='approved' "
        "AND group_membership_status='member'",
        (int(user_id),),
    )
    if changed.rowcount != 1:
        return None, 0, 0
    count, total = await _grant_referral_milestones_in_tx(
        db, int(referred_by), by=by,
    )
    await _track_event_in_tx(
        db, "referral_confirmed", "backend", user_id=int(user_id),
        outcome="approved_and_joined",
        dedupe_key=f"referral_confirmed:{int(user_id)}",
    )
    return int(referred_by), count, total


def _referral_progress_message(count, total):
    if total:
        return (
            f"🎉 Реферальная ступень достигнута!\n"
            f"Одобрено друзей: {count}\n"
            f"Начислено: +{total} бибибонусов."
        )
    next_threshold = next(
        (item_count for item_count, _ in REFERRAL_MILESTONES if item_count > count),
        None,
    )
    return (
        f"👥 Новый друг одобрен и вступил в сообщество: {count}"
        + (
            f" из {next_threshold} до следующей награды."
            if next_threshold else ". Все ступени пройдены!"
        )
    )


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


async def get_referral_url(user_id):
    """Возвращает непрозрачную годовую ссылку вместо открытого Telegram ID."""
    if not BOT_USERNAME:
        return ""
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT token, expires_at FROM referral_tokens "
            "WHERE referrer_id=? ORDER BY expires_at DESC LIMIT 1",
            (user_id,),
        )).fetchone()
        token = None
        if row:
            try:
                if datetime.fromisoformat(row[1]) > now:
                    token = row[0]
            except (TypeError, ValueError):
                token = None
        if not token:
            token = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")
            await db.execute(
                "INSERT INTO referral_tokens "
                "(token, referrer_id, created_at, expires_at) VALUES (?,?,?,?)",
                (
                    token, user_id, now.isoformat(),
                    (now + timedelta(days=365)).isoformat(),
                ),
            )
        await db.commit()
    return f"https://t.me/{BOT_USERNAME}?start=rf_{token}"


async def _bind_referral_token(user_id, payload):
    """Одинаково привязывает opaque referral из bot start и Mini App startapp."""
    if not str(payload).startswith("rf_") or len(str(payload)) > 64:
        return False
    token = str(payload)[3:]
    if not token or not all(char.isalnum() or char in "_-" for char in token):
        return False
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute("BEGIN IMMEDIATE")
        token_row = await (await db.execute(
            "SELECT referrer_id FROM referral_tokens WHERE token=? AND expires_at>?",
            (token, now_iso()),
        )).fetchone()
        referrer_id = int(token_row[0]) if token_row else None
        referrer = await (await db.execute(
            "SELECT 1 FROM members WHERE user_id=?", (referrer_id,),
        )).fetchone() if referrer_id and referrer_id != int(user_id) else None
        if not referrer:
            await db.rollback()
            return False
        bound = await db.execute(
            "UPDATE members SET referred_by=? WHERE user_id=? AND referred_by IS NULL",
            (referrer_id, user_id),
        )
        if bound.rowcount == 1:
            await _track_event_in_tx(
                db, "referral_bound", "backend", user_id=user_id,
                dedupe_key=f"referral_bound:{user_id}",
            )
        await db.commit()
    return bound.rowcount == 1


# ============================================================
# ПРОВЕРКА ПОДПИСИ TELEGRAM (как в рабочем боте)
# ============================================================
def _check_webapp_context(init_data: str):
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
        user = json.loads(parsed.get("user", "{}"))
        if not isinstance(user, dict) or "id" not in user:
            return None
        return {"user": user, "signed": parsed}
    except Exception:
        return None


def _check_webapp_auth(init_data: str):
    context = _check_webapp_context(init_data)
    return context["user"] if context else None


def _get_init_data(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("tma "):
        return auth[4:]
    return request.headers.get("X-Init-Data", "")


async def _auth_user(request):
    context = _auth_context(request)
    tg_user = context["user"] if context else None
    if not tg_user or "id" not in tg_user:
        return None
    return tg_user


def _auth_context(request):
    cache_key = "bibitasks_auth_context"
    if cache_key in request:
        return request[cache_key]
    context = _check_webapp_context(_get_init_data(request))
    request[cache_key] = context
    return context


# ============================================================
# API МИНИ-ПРИЛОЖЕНИЯ
# ============================================================
def _json(data, status=200):
    # Mini App и API отдаются одним origin. Намеренно не открываем API через
    # wildcard CORS: Telegram initData является учётным контекстом пользователя.
    response = web.json_response(data, status=status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


async def _body(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _telegram_log_identity(uid):
    """Stable, non-reversible tag for correlating logs without Telegram IDs."""
    secret = (TELEGRAM_INBOX_KEY or BOT_TOKEN or MEDIA_SIGNING_KEY or "local-test").encode(
        "utf-8"
    )
    digest = hmac.new(
        hashlib.sha256(b"bibitasks-log-identity:" + secret).digest(),
        str(uid).encode("utf-8"), hashlib.sha256,
    ).hexdigest()[:16]
    return f"tg:{digest}"


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
        logger.info("Уведомление не доставлено: %s", _telegram_log_identity(uid))


def _notify(uid, text, kb=None):
    """Отправляет сообщение в фоне, чтобы не задерживать ответ API."""
    asyncio.create_task(_send(uid, text, kb))


def _notify_admins(text):
    async def run():
        for admin_id in await _all_admin_ids():
            await _send(admin_id, text)
    asyncio.create_task(run())


async def _enqueue_outbox_in_tx(
    db, event_key, event_type, payload, *, recipient_id=None,
    chat_id=None, topic_id=None, media_id=None,
):
    """Добавляет доставку в той же транзакции, что бизнес-переход."""
    await db.execute(
        "INSERT OR IGNORE INTO task_outbox "
        "(event_key,event_type,recipient_id,chat_id,topic_id,media_id,payload_json,"
        "status,attempts,available_at,created_at) "
        "VALUES (?,?,?,?,?,?,?,'pending',0,?,?)",
        (
            event_key, event_type, recipient_id,
            str(chat_id) if chat_id is not None else None, topic_id, media_id,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            now_iso(), now_iso(),
        ),
    )


async def _enqueue_admins_in_tx(db, event_key, text, *, start=None):
    """Compatibility broadcast for owner-only legacy events."""
    return await _enqueue_capability_holders_in_tx(
        db, event_key, text, "access.view", start=start,
    )


async def _enqueue_capability_holders_in_tx(
    db, event_key, text, capability, *, start=None,
):
    """Notify only approved staff whose immutable snapshot includes capability."""
    holder_ids = await _active_capability_holder_ids_in_tx(db, capability)
    for admin_id in holder_ids:
        await _enqueue_outbox_in_tx(
            db, f"{event_key}:admin:{admin_id}", "direct",
            {"text": text, "start": start}, recipient_id=admin_id,
        )


async def _queue_join_request_decision_in_tx(db, request_key, decision):
    """Durably queue one approve/decline without calling Telegram in a DB tx."""
    if decision not in {"approve", "decline"}:
        raise ValueError("unsupported join-request decision")
    row = await (await db.execute(
        "SELECT request_key,chat_id,user_id,source,status,decision "
        "FROM telegram_join_requests WHERE request_key=?",
        (request_key,),
    )).fetchone()
    if not row:
        return False
    terminal = "approved" if decision == "approve" else "declined"
    if row["status"] in {terminal, "joined"}:
        return True
    if decision == "approve" and row["source"] != "bot_invite":
        return False
    if row["decision"] and row["decision"] != decision and row["status"] in {
        "approve_queued", "decline_queued", "approved", "declined",
    }:
        return False
    event_key = f"join_request:{request_key}:{decision}"
    await _enqueue_outbox_in_tx(
        db, event_key, "join_request_decision",
        {
            "request_key": request_key,
            "decision": decision,
            "chat_id": str(row["chat_id"]),
            "user_id": int(row["user_id"]),
        },
    )
    await db.execute(
        "UPDATE task_outbox SET status='pending',attempts=0,available_at=?,"
        "last_error=NULL WHERE event_key=? AND status='dead'",
        (now_iso(), event_key),
    )
    await db.execute(
        "UPDATE telegram_join_requests SET status=?,decision=?,"
        "decision_queued_at=?,last_error=NULL WHERE request_key=?",
        (
            "approve_queued" if decision == "approve" else "decline_queued",
            decision, now_iso(), request_key,
        ),
    )
    return True


async def _queue_join_requests_for_user_in_tx(db, user_id, decision):
    """Queue the latest valid approval or every outstanding decline."""
    limit = " LIMIT 1" if decision == "approve" else ""
    rows = await (await db.execute(
        "SELECT request_key FROM telegram_join_requests "
        "WHERE user_id=? AND status IN "
        "('awaiting_application','awaiting_review','manual_required') "
        "ORDER BY requested_at DESC" + limit,
        (int(user_id),),
    )).fetchall()
    queued = 0
    for row in rows:
        if await _queue_join_request_decision_in_tx(db, row[0], decision):
            queued += 1
    return queued


def _chat_membership_is_active(member):
    status = getattr(member.status, "value", member.status)
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return status in {"member", "administrator", "creator"}


async def _join_request_delivery(item, payload):
    chat_id = payload["chat_id"]
    if str(chat_id).lstrip("-").isdigit():
        chat_id = int(chat_id)
    user_id = int(payload["user_id"])
    decision = payload["decision"]
    try:
        if decision == "approve":
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        elif decision == "decline":
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        else:
            raise ValueError("unsupported join-request decision")
    except TelegramBadRequest as exc:
        error_code = str(exc).upper()
        if decision == "decline" and "HIDE_REQUESTER_MISSING" in error_code:
            return None
        if decision == "approve" and "USER_ALREADY_PARTICIPANT" in error_code:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if _chat_membership_is_active(member):
                return None
        raise
    return None


async def _deliver_outbox_item(item):
    payload = json.loads(item["payload_json"])
    if PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED:
        # Disposable staging still exercises the durable outbox and worker,
        # but synthetic 52-bit recipients must never trigger real Bot API
        # delivery. The flag is rejected outside explicit staging load mode.
        return None
    if item["event_type"] == "direct":
        return await bot.send_message(
            int(item["recipient_id"]), payload["text"],
            reply_markup=_open_app_kb(payload.get("start")),
        )
    if item["event_type"] == "join_request_decision":
        return await _join_request_delivery(item, payload)
    if item["event_type"] == "group_task":
        chat_id = item["chat_id"]
        if str(chat_id).lstrip("-").isdigit():
            chat_id = int(chat_id)
        kwargs = {
            "parse_mode": "HTML",
            "reply_markup": _open_app_kb(payload.get("start")),
        }
        photo_file = payload.get("photo_file")
        if photo_file:
            return await _send_photo_to_topic(
                chat_id, photo_file,
                payload["text"], item["topic_id"],
                media_id=item["media_id"] or payload.get("media_id"), **kwargs,
            )
        return await _send_to_topic(
            chat_id, payload["text"], item["topic_id"],
            disable_web_page_preview=True, **kwargs,
        )
    if item["event_type"] == "group_publication":
        operation_id = item["event_key"]
        existing = await _get_published(payload["kind"])
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            job = await (await db.execute(
                "SELECT operation_id,status FROM publication_jobs WHERE kind=?",
                (payload["kind"],),
            )).fetchone()
            if not job or job["operation_id"] != operation_id:
                return None
            if existing and existing["operation_id"] == operation_id:
                # The new post is already durable/current. Cleanup belongs to
                # the lifecycle reconciler and must not consume delivery
                # attempts during crash recovery.
                return None
            await db.execute(
                "UPDATE publication_jobs SET status='sending' "
                "WHERE kind=? AND operation_id=?",
                (payload["kind"], operation_id),
            )
            await db.commit()
        target = payload["target"]
        if str(target).lstrip("-").isdigit():
            target = int(target)
        ids = []
        sent = None
        parts = payload["parts"]
        for index, part in enumerate(parts):
            async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                recorded = await (await db.execute(
                    "SELECT message_id FROM publication_delivery_parts "
                    "WHERE operation_id=? AND part_index=?",
                    (operation_id, index),
                )).fetchone()
            if recorded:
                ids.append(int(recorded[0]))
                continue
            sent = await _send_to_topic(
                target, part, payload.get("topic"),
                reply_markup=(
                    _post_kb(payload["kind"])
                    if index == len(parts) - 1 else None
                ),
                parse_mode="HTML", disable_web_page_preview=True,
            )
            if sent is not None and getattr(sent, "message_id", None):
                ids.append(sent.message_id)
                async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                    await db.execute(
                        "INSERT OR IGNORE INTO publication_delivery_parts "
                        "(operation_id,part_index,message_id,created_at) VALUES (?,?,?,?)",
                        (operation_id, index, sent.message_id, now_iso()),
                    )
                    await db.commit()
        cleanup_pending = await _remember_published(
            payload["kind"], target, payload.get("topic"), ids,
            int(payload["admin_id"]), operation_id,
        )
        if cleanup_pending:
            # Delivery is complete once the new publication is durable.  Old
            # post cleanup has its own retry lifecycle and must never turn a
            # successful delivery into a failed outbox item.
            try:
                await _run_publication_cleanup(operation_id)
            except Exception:
                logger.warning("Publication cleanup deferred after delivery")
        return sent
    raise ValueError("Неизвестный тип outbox-события.")


async def _handle_dead_publication_in_tx(db, item):
    failed_payload = json.loads(item["payload_json"])
    current = await (await db.execute(
        "SELECT operation_id FROM published_posts WHERE kind=?",
        (failed_payload["kind"],),
    )).fetchone()
    if current and current[0] == item["event_key"]:
        # Delivery became current before the crash. Keep its cleanup lifecycle
        # intact; the outbox dead state must not orphan or downgrade that job.
        return
    part_rows = await (await db.execute(
        "SELECT message_id FROM publication_delivery_parts WHERE operation_id=?",
        (item["event_key"],),
    )).fetchall()
    for part_row in part_rows:
        await db.execute(
            "INSERT OR IGNORE INTO publication_cleanup_messages "
            "(operation_id,chat_id,message_id,final_job_status,status) "
            "VALUES (?,?,?,'failed','pending')",
            (
                item["event_key"], str(failed_payload["target"]),
                int(part_row[0]),
            ),
        )
    await db.execute(
        "UPDATE publication_jobs SET status=? WHERE operation_id=?",
        (
            "failed_cleanup_pending" if part_rows else "failed",
            item["event_key"],
        ),
    )


def _telegram_retry_delay(attempts):
    """Одинаковый exponential backoff; staging может ускорить его явно."""
    attempt = max(1, int(attempts or 1))
    return min(
        TELEGRAM_RETRY_MAX_SECONDS,
        TELEGRAM_RETRY_BASE_SECONDS * (2 ** min(attempt - 1, 10)),
    )


def _telegram_delivery_ids(message):
    """Извлекает фактические идентификаторы сообщения, включая fallback темы."""
    if message is None:
        return None, None
    try:
        message_id = int(getattr(message, "message_id", 0) or 0)
    except (TypeError, ValueError):
        message_id = 0
    try:
        thread_id = int(getattr(message, "message_thread_id", 0) or 0)
    except (TypeError, ValueError):
        thread_id = 0
    return (message_id or None), (thread_id or None)


async def outbox_worker():
    """At-least-once доставка Telegram с backoff и восстановлением рестарта."""
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE task_outbox SET status='pending' "
            "WHERE status='sending' AND sent_at IS NULL"
        )
        await db.commit()
    while True:
        item = None
        try:
            async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN IMMEDIATE")
                item = await (await db.execute(
                    "SELECT * FROM task_outbox WHERE status='pending' "
                    "AND available_at<=? ORDER BY id LIMIT 1", (now_iso(),),
                )).fetchone()
                if item:
                    await db.execute(
                        "UPDATE task_outbox SET status='sending',attempts=attempts+1 "
                        "WHERE id=? AND status='pending'", (item["id"],),
                    )
                await db.commit()
            if not item:
                await asyncio.sleep(1)
                continue
            delivered = await _deliver_outbox_item(item)
            telegram_message_id, telegram_thread_id = _telegram_delivery_ids(delivered)
            if item["event_type"] == "group_task" and telegram_message_id is None:
                logger.warning("Telegram task delivery returned no message_id")
            async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                await db.execute(
                    "UPDATE task_outbox SET status='sent',sent_at=?,last_error=NULL,"
                    "telegram_message_id=?,telegram_thread_id=? WHERE id=?",
                    (
                        now_iso(), telegram_message_id, telegram_thread_id,
                        item["id"],
                    ),
                )
                if item["event_type"] == "join_request_decision":
                    payload = json.loads(item["payload_json"])
                    terminal = (
                        "approved" if payload["decision"] == "approve"
                        else "declined"
                    )
                    await db.execute(
                        "UPDATE telegram_join_requests SET "
                        "status=CASE WHEN status='joined' THEN status ELSE ? END,"
                        "decided_at=?,last_error=NULL WHERE request_key=?",
                        (terminal, now_iso(), payload["request_key"]),
                    )
                await db.commit()
            if item["event_type"] == "group_task":
                delivered_payload = json.loads(item["payload_json"])
                await _track_event_best_effort(
                    "task_published", "backend",
                    user_id=delivered_payload.get("admin_id"),
                    task_id=delivered_payload.get("task_id"),
                    dedupe_key=(
                        f"task_published:{delivered_payload.get('operation_id')}"
                    ),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _record_runtime_error(exc)
            logger.warning("Outbox delivery failed: %s", type(exc).__name__)
            if item:
                attempts = int(item["attempts"] or 0) + 1
                delay = _telegram_retry_delay(attempts)
                status = (
                    "dead" if attempts >= TELEGRAM_RETRY_MAX_ATTEMPTS else "pending"
                )
                available = datetime.now(timezone.utc) + timedelta(seconds=delay)
                async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                    await db.execute(
                        "UPDATE task_outbox SET status=?,available_at=?,last_error=? "
                        "WHERE id=?",
                        (status, available.isoformat(), type(exc).__name__, item["id"]),
                    )
                    if item["event_type"] == "join_request_decision":
                        payload = json.loads(item["payload_json"])
                        await db.execute(
                            "UPDATE telegram_join_requests SET status=?,last_error=? "
                            "WHERE request_key=?",
                            (
                                "manual_required" if status == "dead" else (
                                    "approve_queued"
                                    if payload["decision"] == "approve"
                                    else "decline_queued"
                                ),
                                type(exc).__name__, payload["request_key"],
                            ),
                        )
                    if status == "dead" and item["event_type"] == "group_publication":
                        await _handle_dead_publication_in_tx(db, item)
                    await db.commit()
            await asyncio.sleep(1)


def _telegram_payload_fingerprint(canonical):
    if not TELEGRAM_INBOX_KEY:
        raise RuntimeError("Telegram inbox encryption is not configured")
    digest = hmac.new(
        TELEGRAM_INBOX_KEY.encode("ascii"),
        canonical.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    return f"h1:{digest}"


def _validate_json_shape(payload, *, max_depth=20, max_nodes=5000):
    stack = [(payload, 1)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValueError("update_shape")
        if isinstance(value, dict):
            stack.extend((key, depth + 1) for key in value.keys())
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)


def _encrypt_telegram_payload(canonical):
    if TELEGRAM_INBOX_FERNET is None:
        raise RuntimeError("Telegram inbox encryption is not configured")
    return TELEGRAM_INBOX_FERNET.encrypt(canonical.encode("utf-8")).decode("ascii")


def _decrypt_telegram_payload(ciphertext):
    if TELEGRAM_INBOX_FERNET is None:
        raise RuntimeError("Telegram inbox encryption is not configured")
    try:
        return json.loads(
            TELEGRAM_INBOX_FERNET.decrypt(str(ciphertext).encode("ascii")).decode("utf-8")
        )
    except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Telegram inbox payload cannot be decrypted") from exc


async def telegram_webhook_handler(request):
    """Persist Telegram update before acknowledging it; never log its body or secret."""
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not supplied or not secrets.compare_digest(supplied, WEBHOOK_SECRET):
        raise web.HTTPUnauthorized()
    if request.content_type != "application/json":
        return _json({"error": "content_type"}, status=415)
    chunks = []
    size = 0
    async for chunk in request.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > 1024 * 1024:
            raise web.HTTPRequestEntityTooLarge(max_size=1024 * 1024, actual_size=size)
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        _validate_json_shape(payload)
        raw_update_id = payload["update_id"]
        if (
            not isinstance(payload, dict) or type(raw_update_id) is not int
            or raw_update_id < 0 or raw_update_id > 9_223_372_036_854_775_807
        ):
            raise ValueError
        update_id = raw_update_id
        Update.model_validate(payload, context={"bot": bot})
    except (
        ValueError, TypeError, KeyError, UnicodeDecodeError,
        json.JSONDecodeError, RecursionError, ValidationError,
    ):
        return _json({"error": "invalid_update"}, status=400)
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    payload_hash = _telegram_payload_fingerprint(canonical)
    encrypted_payload = _encrypt_telegram_payload(canonical)
    received_at = now_iso()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        existing = await (await db.execute(
            "SELECT payload_sha256 FROM telegram_update_inbox WHERE update_id=?",
            (update_id,),
        )).fetchone()
        if existing:
            await db.rollback()
            if not hmac.compare_digest(existing["payload_sha256"], payload_hash):
                logger.error("Telegram update_id collision detected: %s", update_id)
                return _json({"error": "update_conflict"}, status=409)
            _telegram_runtime["last_update_at"] = received_at
            return _json({"ok": True, "duplicate": True})
        active_count = int((await (await db.execute(
            "SELECT COUNT(*) FROM telegram_update_inbox "
            "WHERE status IN ('pending','processing')"
        )).fetchone())[0])
        if active_count >= TELEGRAM_INBOX_HARD_LIMIT:
            # Telegram retries every non-2xx webhook response. Refuse before
            # persistence when the durable local queue is full, leaving the
            # update in Telegram instead of growing SQLite without a bound.
            await db.rollback()
            _telegram_runtime["overload_rejected"] += 1
            response = _json({
                "error": "webhook_overloaded",
                "message": "Telegram update queue is temporarily full.",
            }, status=503)
            response.headers["Retry-After"] = "2"
            return response
        await db.execute(
            "INSERT INTO telegram_update_inbox "
            "(update_id,payload_json,payload_sha256,status,attempts,available_at,received_at) "
            "VALUES (?,?,?,'pending',0,?,?)",
            (update_id, encrypted_payload, payload_hash, received_at, received_at),
        )
        await db.commit()
    _telegram_runtime["last_update_at"] = received_at
    return _json({"ok": True, "duplicate": False})


async def _telegram_lease_heartbeat(update_id, worker_id, lease_lost):
    """Keep a long-running handler fenced from a second inbox worker."""
    while True:
        await asyncio.sleep(30)
        try:
            async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                cur = await db.execute(
                    "UPDATE telegram_update_inbox SET locked_at=? "
                    "WHERE update_id=? AND status='processing' AND locked_by=?",
                    (now_iso(), update_id, worker_id),
                )
                await db.commit()
                if cur.rowcount != 1:
                    lease_lost.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram inbox lease heartbeat failed")
            lease_lost.set()
            return


async def _mark_telegram_update_failed(item, worker_id, error_name):
    """Return an owned inbox item to retry/dead without exposing exception text."""
    attempts = int(item["attempts"] or 0) + 1
    status = "dead" if attempts >= TELEGRAM_RETRY_MAX_ATTEMPTS else "pending"
    delay = _telegram_retry_delay(attempts)
    available = datetime.now(timezone.utc) + timedelta(seconds=delay)
    failed_at = now_iso()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        cur = await db.execute(
            "UPDATE telegram_update_inbox SET status=?,available_at=?,"
            "last_error=?,locked_by=NULL,locked_at=NULL,"
            "dead_at=CASE WHEN ?='dead' THEN ? ELSE dead_at END "
            "WHERE update_id=? AND status='processing' AND locked_by=?",
            (
                status, available.isoformat(), error_name,
                status, failed_at, item["update_id"], worker_id,
            ),
        )
        await db.commit()
    return cur.rowcount == 1


async def _finalize_timed_out_telegram_update(item, worker_id, dispatch, heartbeat):
    """Keep the lease fenced until a cancelled handler has actually stopped."""
    try:
        try:
            await dispatch
        except BaseException:
            pass
        await _mark_telegram_update_failed(item, worker_id, "TimeoutError")
    except Exception as exc:
        logger.warning(
            "Telegram timeout finalizer failed: %s", type(exc).__name__,
        )
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


def _retain_timed_out_telegram_job(update_id, job):
    _telegram_timed_out_jobs[int(update_id)] = job

    def forget(completed):
        if _telegram_timed_out_jobs.get(int(update_id)) is completed:
            _telegram_timed_out_jobs.pop(int(update_id), None)
        try:
            completed.result()
        except BaseException:
            pass

    job.add_done_callback(forget)


async def telegram_inbox_worker():
    """Retry durable updates with an ownership lease safe for rolling restarts."""
    worker_id = str(uuid.uuid4())
    lease_seconds = 120
    try:
        while not _shutdown_event.is_set():
            item = None
            try:
                current = datetime.now(timezone.utc)
                stamp = current.isoformat()
                lease_before = (current - timedelta(seconds=lease_seconds)).isoformat()
                async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                    db.row_factory = aiosqlite.Row
                    await db.execute("BEGIN IMMEDIATE")
                    await db.execute(
                        "UPDATE telegram_update_inbox SET status='pending',locked_by=NULL,"
                        "locked_at=NULL,available_at=? WHERE status='processing' "
                        "AND (locked_at IS NULL OR locked_at<?)",
                        (stamp, lease_before),
                    )
                    item = await (await db.execute(
                        "SELECT * FROM telegram_update_inbox "
                        "WHERE status='pending' AND available_at<=? "
                        "ORDER BY update_id LIMIT 1",
                        (stamp,),
                    )).fetchone()
                    if item:
                        cur = await db.execute(
                            "UPDATE telegram_update_inbox SET status='processing',"
                            "attempts=attempts+1,last_error=NULL,locked_by=?,locked_at=? "
                            "WHERE update_id=? AND status='pending'",
                            (worker_id, stamp, item["update_id"]),
                        )
                        if cur.rowcount != 1:
                            item = None
                    await db.commit()
                if not item:
                    # Polling keeps the module-level shutdown flag loop-neutral.
                    # asyncio.Event.wait() permanently binds the event to the
                    # first loop that blocks on it, which breaks supervised
                    # restarts and IsolatedAsyncioTestCase on Python 3.12.
                    await asyncio.sleep(0.25)
                    continue
                payload = _decrypt_telegram_payload(item["payload_json"])
                lease_lost = asyncio.Event()
                heartbeat = asyncio.create_task(_telegram_lease_heartbeat(
                    item["update_id"], worker_id, lease_lost,
                ))
                try:
                    dispatch = asyncio.create_task(dp.feed_raw_update(bot, payload))
                    completed, _ = await asyncio.wait(
                        {dispatch}, timeout=TELEGRAM_HANDLER_TIMEOUT_SEC,
                    )
                    if not completed:
                        dispatch.cancel()
                        timed_out_item = item
                        finalizer = asyncio.create_task(
                            _finalize_timed_out_telegram_update(
                                timed_out_item, worker_id, dispatch, heartbeat,
                            )
                        )
                        _retain_timed_out_telegram_job(
                            timed_out_item["update_id"], finalizer,
                        )
                        logger.warning("Telegram inbox handler timed out")
                        heartbeat = None
                        item = None
                        continue
                    await dispatch
                finally:
                    if heartbeat is not None:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
                if lease_lost.is_set():
                    raise RuntimeError("Telegram inbox lease was lost during dispatch")
                async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                    cur = await db.execute(
                        "UPDATE telegram_update_inbox SET status='done',payload_json=NULL,"
                        "processed_at=?,last_error=NULL,locked_by=NULL,locked_at=NULL "
                        "WHERE update_id=? AND status='processing' AND locked_by=?",
                        (now_iso(), item["update_id"], worker_id),
                    )
                    await db.commit()
                    if cur.rowcount != 1:
                        logger.error("Telegram inbox lease lost before commit: %s", item["update_id"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _record_runtime_error(exc)
                logger.warning("Telegram inbox processing failed: %s", type(exc).__name__)
                if item:
                    await _mark_telegram_update_failed(
                        item, worker_id, type(exc).__name__,
                    )
                await asyncio.sleep(0.25)
    finally:
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            active_timeouts = tuple(_telegram_timed_out_jobs)
            if active_timeouts:
                placeholders = ",".join("?" for _ in active_timeouts)
                await db.execute(
                    "UPDATE telegram_update_inbox SET status='pending',locked_by=NULL,"
                    "locked_at=NULL,available_at=? "
                    "WHERE status='processing' AND locked_by=? "
                    f"AND update_id NOT IN ({placeholders})",
                    (now_iso(), worker_id, *active_timeouts),
                )
            else:
                await db.execute(
                    "UPDATE telegram_update_inbox SET status='pending',locked_by=NULL,"
                    "locked_at=NULL,available_at=? "
                    "WHERE status='processing' AND locked_by=?",
                    (now_iso(), worker_id),
                )
            await db.commit()


APPLICATION_RESUBMIT_COOLDOWN = timedelta(hours=24)


def _application_resubmit_state(m, *, current_time=None):
    """Fail-closed eligibility for a rejected application retry."""
    if m.get("status") != "blocked":
        return False, None, 0
    try:
        submitted_at = datetime.fromisoformat(str(m.get("applied_at") or ""))
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=timezone.utc)
        submitted_at = submitted_at.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return False, None, 0
    now = current_time or datetime.now(timezone.utc)
    available_at = submitted_at + APPLICATION_RESUBMIT_COOLDOWN
    retry_after = max(0, math.ceil((available_at - now).total_seconds()))
    return retry_after == 0, available_at.isoformat(), retry_after


def _help_links():
    community_url = _safe_https_url(_required_chat_url())
    work_topic_url = _safe_https_url(_topic_link(TOPIC_WORK))
    bot_url = _safe_https_url(f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "")
    support_url = _safe_https_url(
        f"https://t.me/{WITHDRAW_CONTACT}" if WITHDRAW_CONTACT else ""
    )
    return {
        "community_url": community_url,
        "work_topic_url": work_topic_url,
        "bot_url": bot_url,
        "support_url": support_url,
    }


def _member_public(m):
    """Что отдаём во фронт о самом пользователе."""
    done = m.get("done_count", 0)
    chat_xp = m.get("chat_xp", 0) or 0
    score = trust_score(done, chat_xp)
    key, name, emoji, _ = trust_for(score)
    nxt = next_trust(score)
    can_resubmit, resubmit_at, retry_after = _application_resubmit_state(m)
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
        "city": m.get("city") or "",
        "city_change_requested": m.get("city_change_requested") or "",
        "city_change_requested_at": m.get("city_change_requested_at") or "",
        "application_note": m.get("application_note") or "",
        "about": m.get("about") or "",
        "can_resubmit": can_resubmit,
        "resubmit_available_at": resubmit_at,
        "resubmit_retry_after": retry_after,
    }


async def api_state(request):
    """Главное состояние: кто пользователь, его статус, бонусы, доступ."""
    auth_context = _auth_context(request)
    if not auth_context:
        return _json({"error": "auth"}, status=401)
    tg = auth_context["user"]
    uid = tg["id"]
    m = await get_member(uid)
    if not m:
        # Первый визит — заводим кандидата, но без заявки (status pending, role candidate)
        await upsert_member(
            uid,
            full_name=(tg.get("first_name", "") + " " + tg.get("last_name", "")).strip(),
            username=tg.get("username", ""))
        m = await get_member(uid)
    signed = auth_context["signed"]
    launch_seed = str(
        signed.get("query_id")
        or f"{uid}:{signed.get('auth_date','')}:{signed.get('start_param','')}"
    )
    session_id = hmac.new(
        hashlib.sha256(BOT_TOKEN.encode("utf-8")).digest(),
        launch_seed.encode("utf-8"), hashlib.sha256,
    ).hexdigest()[:32]
    start_param = str(signed.get("start_param") or "")
    if start_param.startswith("rf_"):
        await _bind_referral_token(uid, start_param)
        m = await get_member(uid)
    entrypoint = "task" if start_param.startswith("task_") else (
        "referral" if start_param.startswith("rf_") else "direct"
    )
    await _track_event_best_effort(
        "miniapp_authenticated", "miniapp", user_id=uid,
        session_id=session_id, properties={"entrypoint": entrypoint},
        dedupe_key=f"launch:{session_id}", retention_days=30,
    )
    if m["status"] == "approved":
        await sync_referral_milestones(uid)
        m = await get_member(uid)
    referral = await get_referral_progress(uid)
    referral_url = await get_referral_url(uid)
    staff_access = await _effective_staff_access(uid)
    admin = bool(staff_access["capabilities"])
    can_work = admin or (m["status"] == "approved" and m["role"] in ("helper", "employee", "admin"))
    return _json({
        "ok": True,
        "application_version": APP_VERSION,
        "build_version": BUILD_VERSION,
        "bot_username": BOT_USERNAME,
        "me": _member_public(m),
        "is_admin": admin,
        "staff_access": staff_access,
        "can_work": can_work,
        "task_types": [
            {"key": k, **v} for k, v in TASK_TYPES.items()
        ],
        "trust_levels": [
            {"key": k, "name": n, "emoji": e, "at": t} for k, n, e, t in TRUST_LEVELS
        ],
        "referral": referral,
        "referral_url": referral_url,
        "my_awards": await _my_awards(uid),
        "withdraw_min": WITHDRAW_MIN,
        "ride_rub_per_min": RIDE_RUB_PER_MIN,
        "withdraw_min_minutes": ride_minutes_for(WITHDRAW_MIN),
        "support_username": WITHDRAW_CONTACT,
        "help": _help_links(),
        "privacy_url": PRIVACY_URL,
        "roles": [{"key": k, "title": v} for k, v in ROLE_TITLES.items()],
        "referral_gate": {
            "required": bool(_required_chat_id()),
            "url": _required_chat_url(),
            "managed_join_request": JOIN_REQUEST_ADMISSION_ENABLED,
            "membership_status": m.get("group_membership_status") or "unknown",
            "invited": bool(m["referred_by"]) and m["referred_by"] != uid,
            "confirmed": bool(m["ref_confirmed"]),
        },
    })


async def api_apply(request):
    """Короткая заявка: имя, город и чем человек может быть полезен."""
    tg = await _auth_user(request)
    if not tg:
        return _json({"error": "auth"}, status=401)
    body = await _body(request)
    name = (body.get("name") or "").strip()
    city = _city_display(body.get("city"))
    about = (body.get("about") or "").strip()
    if len(name) < 2:
        return _json({"error": "name", "message": "Укажите имя."}, status=400)
    if len(name) > 80:
        return _json({
            "error": "name_too_long", "message": "Имя — не более 80 символов.",
        }, status=400)
    if len(city) < 2:
        return _json({"error": "city", "message": "Укажите город."}, status=400)
    if len(about) < 5:
        return _json({
            "error": "about",
            "message": "Коротко напишите, что сможете выполнять и чем будете полезны.",
        }, status=400)
    if len(about) > 600:
        return _json({
            "error": "about_too_long",
            "message": "Комментарий — не более 600 символов.",
        }, status=400)
    uid = tg["id"]
    applied_at = now_iso()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        m = await (await db.execute(
            "SELECT status,role,applied_at FROM members WHERE user_id=?", (uid,),
        )).fetchone()
        is_resubmit = bool(m and m["status"] == "blocked")
        if m and m["status"] == "approved":
            await db.rollback()
            return _json({
                "error": "already_approved",
                "message": "Заявка уже одобрена — повторная подача не нужна.",
            }, status=409)
        if m and m["status"] == "pending" and (
            m["applied_at"] or m["role"] == "applicant"
        ):
            await db.rollback()
            return _json({
                "error": "application_pending",
                "message": "Заявка уже на рассмотрении.",
            }, status=409)
        if is_resubmit:
            allowed, available_at, retry_after = _application_resubmit_state(dict(m))
            if not allowed:
                await db.rollback()
                response = _json({
                    "error": "resubmit_cooldown",
                    "message": "Повторную заявку можно отправлять не чаще одного раза в сутки.",
                    "available_at": available_at,
                    "retry_after": retry_after,
                }, status=429)
                if retry_after:
                    response.headers["Retry-After"] = str(retry_after)
                return response
        elif m and (m["applied_at"] or m["role"] == "applicant"):
            await db.rollback()
            return _json({
                "error": "application_conflict",
                "message": "Текущую заявку нельзя подать повторно.",
            }, status=409)
        await db.execute(
            "INSERT INTO members (user_id,created_at) VALUES (?,?) "
            "ON CONFLICT(user_id) DO NOTHING", (uid, applied_at),
        )
        await db.execute(
            "UPDATE members SET full_name=?,city=?,about=?,application_note='',"
            "username=?,role='applicant',status='pending',applied_at=? WHERE user_id=?",
            (name, city, about, tg.get("username", ""), applied_at, uid),
        )
        await db.execute(
            "UPDATE telegram_join_requests SET status='awaiting_review' "
            "WHERE user_id=? AND status='awaiting_application'",
            (uid,),
        )
        await _track_event_in_tx(
            db, "application_resubmitted" if is_resubmit else "application_submitted",
            "backend", user_id=uid,
            dedupe_key=f"application:{uid}:{applied_at}",
        )
        await _enqueue_capability_holders_in_tx(
            db, f"application:{uid}:{applied_at}",
            f"{'🔁 Повторная' if is_resubmit else '🆕 Новая'} заявка на помощь\n"
            f"Имя: {name}\nГород: {city}\nЧем полезен: {about}\n"
            f"Ник: @{tg.get('username','') or '—'}\nID: {uid}\n\n"
            f"Открой приложение → Модерация, чтобы одобрить.",
            "application.review",
        )
        await db.commit()
    return _json({"ok": True, "resubmitted": is_resubmit})


async def api_profile_city(request):
    """Request an audited city correction; an admin must approve the gate change."""
    tg = await _auth_user(request)
    if not tg:
        return _json({"error": "auth"}, status=401)
    body = await _body(request)
    action = str(body.get("action") or "request").strip().lower()
    city = _city_display(body.get("city"))
    if action not in {"request", "cancel"}:
        return _json({"error": "action"}, status=400)
    if action == "request" and (len(city) < 2 or not _city_key(city)):
        return _json({"error": "city", "message": "Укажи город."}, status=400)
    uid = int(tg["id"])
    admin = await is_admin(uid)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        member = await (await db.execute(
            "SELECT status,city,city_change_requested,city_change_requested_at "
            "FROM members WHERE user_id=?", (uid,),
        )).fetchone()
        if not member or (member["status"] != "approved" and not admin):
            await db.rollback()
            return _json({
                "error": "not_approved",
                "message": "Город можно изменить после одобрения заявки.",
            }, status=403)
        if action == "cancel":
            expected_at = str(body.get("requested_at") or "")
            if (
                not member["city_change_requested"]
                or not expected_at
                or not hmac.compare_digest(
                    str(member["city_change_requested_at"] or ""), expected_at,
                )
            ):
                await db.rollback()
                return _json({
                    "error": "request_changed",
                    "message": "Запрос уже изменился или обработан. Обнови профиль.",
                }, status=409)
            await db.execute(
                "UPDATE members SET city_change_requested=NULL,"
                "city_change_requested_at=NULL WHERE user_id=? "
                "AND city_change_requested_at=?", (uid, expected_at),
            )
            await _track_event_in_tx(
                db, "city_change_decided", "miniapp", user_id=uid,
                outcome="cancel", dedupe_key=f"city_change_cancel:{uid}:{expected_at}",
            )
            await _enqueue_capability_holders_in_tx(
                db, f"city_change:{uid}:{expected_at}:cancelled",
                f"📍 Участник ID {uid} отменил запрос на смену города.",
                "member.city.review",
            )
            await db.commit()
            return _json({"ok": True, "city": member["city"] or "", "pending": False})
        if member["city_change_requested"]:
            if _city_key(member["city_change_requested"]) == _city_key(city):
                await db.rollback()
                return _json({
                    "ok": True, "city": member["city"] or "",
                    "requested_city": member["city_change_requested"],
                    "requested_at": member["city_change_requested_at"],
                    "pending": True,
                })
            await db.rollback()
            return _json({
                "error": "request_pending",
                "message": "Сначала отмени текущий запрос на смену города.",
            }, status=409)
        if _city_key(member["city"]) == _city_key(city):
            await db.rollback()
            return _json({"ok": True, "city": member["city"], "pending": False})
        active = await (await db.execute(
            "SELECT 1 FROM task_assignments WHERE user_id=? "
            "AND status IN ('claimed','review') UNION ALL "
            "SELECT 1 FROM tasks WHERE assigned_to=? AND status='open' LIMIT 1",
            (uid, uid),
        )).fetchone()
        if active:
            await db.rollback()
            return _json({
                "error": "active_assignment",
                "message": "Сначала заверши или освободи активное задание, затем запроси смену города.",
            }, status=409)
        requested_at = now_iso()
        await db.execute(
            "UPDATE members SET city_change_requested=?,city_change_requested_at=? "
            "WHERE user_id=?", (city, requested_at, uid),
        )
        await _track_event_in_tx(
            db, "city_change_requested", "miniapp", user_id=uid,
            outcome="pending", dedupe_key=f"city_change:{uid}:{requested_at}",
        )
        await _enqueue_capability_holders_in_tx(
            db, f"city_change:{uid}:{requested_at}",
            f"📍 Запрос на смену города\nУчастник: ID {uid}\n"
            f"Было: {member['city'] or 'не указан'}\nНовый город: {city}\n\n"
            "Подтверди изменение в разделе «Скаут».",
            "member.city.review",
        )
        await db.commit()
    return _json({
        "ok": True, "city": member["city"] or "", "requested_city": city,
        "requested_at": requested_at, "pending": True,
    })


async def _all_admin_ids():
    ids = set()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        sql = "SELECT m.user_id FROM members m WHERE m.role='admin' AND m.status='approved'"
        if BIBITASKS_ENVIRONMENT == "production":
            sql += " AND EXISTS (SELECT 1 FROM admin_authorities aa WHERE aa.user_id=m.user_id)"
        rows = await (await db.execute(sql)).fetchall()
        ids.update(r[0] for r in rows)
    return ids


async def _expire_due_tasks():
    """Expire offers and every claimed assignment by its own effective deadline."""
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()

    def is_due(raw):
        if not raw:
            return False
        try:
            value = datetime.fromisoformat(raw)
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc) <= now
        except (TypeError, ValueError):
            return False

    expired_task_ids = []
    expired_assignments = []
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        tasks = await (await db.execute(
            "SELECT id,slot_end FROM tasks "
            "WHERE status IN ('open','closed') AND slot_end IS NOT NULL"
        )).fetchall()
        expired_task_ids = [int(row["id"]) for row in tasks if is_due(row["slot_end"])]

        claims = await (await db.execute(
            "SELECT id,task_id,user_id,due_at,revision_due_at "
            "FROM task_assignments WHERE status='claimed'"
        )).fetchall()
        expired_assignments = [
            dict(row) for row in claims
            if is_due(row["revision_due_at"] or row["due_at"])
        ]

        if expired_task_ids:
            placeholders = ",".join("?" for _ in expired_task_ids)
            await db.execute(
                f"UPDATE tasks SET status='expired',expired_at=?,version=version+1 "
                f"WHERE status IN ('open','closed') AND id IN ({placeholders})",
                (stamp, *expired_task_ids),
            )
            for task_id in expired_task_ids:
                await _track_event_in_tx(
                    db, "task_expired", "backend", task_id=task_id,
                    dedupe_key=f"task_expired:{task_id}",
                )

        for item in expired_assignments:
            cur = await db.execute(
                "UPDATE task_assignments SET status='expired',terminal_at=?,"
                "terminal_reason='deadline',version=version+1 "
                "WHERE id=? AND status='claimed'",
                (stamp, item["id"]),
            )
            if cur.rowcount != 1:
                continue
            await _track_event_in_tx(
                db, "task_expired", "backend",
                user_id=item["user_id"], task_id=item["task_id"],
                assignment_id=item["id"], outcome="assignment_expired",
                dedupe_key=f"assignment_expired:{item['id']}",
            )
            await _enqueue_outbox_in_tx(
                db, f"assignment:{item['id']}:expired", "direct",
                {"text": (
                    f"Срок задания #{item['task_id']} закончился. "
                    "Выполнение освобождено."
                ), "start": None},
                recipient_id=item["user_id"],
            )
        await db.commit()
    return expired_task_ids, expired_assignments


async def _expire_stale_join_requests():
    """Decline managed requests that never received an application within SLA."""
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(hours=JOIN_REQUEST_APPLICATION_SLA_HOURS)
    ).isoformat()
    expired = []
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        rows = await (await db.execute(
            "SELECT request_key,user_id FROM telegram_join_requests "
            "WHERE source='bot_invite' AND status='awaiting_application' "
            "AND requested_at<=? ORDER BY requested_at ASC LIMIT 100",
            (cutoff,),
        )).fetchall()
        for row in rows:
            if not await _queue_join_request_decision_in_tx(
                db, row["request_key"], "decline",
            ):
                continue
            expired.append(row["request_key"])
            await _track_event_in_tx(
                db, "group_join_request_expired", "backend",
                user_id=int(row["user_id"]), outcome="decline_queued",
                dedupe_key=f"group_join_request_expired:{row['request_key']}",
            )
        await db.commit()
    return expired


async def lifecycle_worker():
    """Периодически применяет дедлайны без внешнего запроса к API."""
    cycles = 0
    while True:
        try:
            await _expire_due_tasks()
            await _expire_stale_join_requests()
            await _reconcile_publication_cleanups()
            cycles += 1
            if cycles % 120 == 1:
                await cleanup_expired_analytics()
            if TELEGRAM_UPDATE_MODE == "webhook" and cycles % 10 == 1:
                await _refresh_telegram_runtime()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _record_runtime_error(exc)
            logger.exception("Не удалось обработать сроки заданий")
        await asyncio.sleep(30)


async def api_tasks_available(request):
    """Каталог и активные выполнения из единой assignment-модели."""
    uid, err = await _require_worker(request)
    if err is not None:
        return err
    await _expire_due_tasks()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        member = await (await db.execute(
            "SELECT city FROM members WHERE user_id=?", (uid,)
        )).fetchone()
        worker_city = _city_key(member["city"] if member else "")
        task_rows = await (await db.execute(
            "SELECT t.*, m.full_name AS assigned_name FROM tasks t "
            "LEFT JOIN members m ON m.user_id=t.assigned_to "
            "WHERE t.status='open' AND (t.assigned_to IS NULL OR t.assigned_to=?) "
            "ORDER BY CASE WHEN t.assigned_to=? THEN 0 ELSE 1 END, "
            "t.slot_start IS NULL, t.slot_start, t.created_at DESC LIMIT 500",
            (uid, uid),
        )).fetchall()
        candidate_ids = [int(row["id"]) for row in task_rows]
        assignment_rows = []
        if candidate_ids:
            placeholders = ",".join("?" for _ in candidate_ids)
            assignment_rows = await (await db.execute(
                f"SELECT task_id, user_id, status, reward_snapshot "
                f"FROM task_assignments WHERE task_id IN ({placeholders})",
                candidate_ids,
            )).fetchall()
        by_task = {}
        for row in assignment_rows:
            by_task.setdefault(int(row["task_id"]), []).append(dict(row))
        available = []
        for source in task_rows:
            row = dict(source)
            if not worker_city or _city_key(row.get("city")) != worker_city:
                continue
            runs = by_task.get(int(row["id"]), [])
            committed = [
                item for item in runs
                if item["status"] in ("claimed", "review", "done")
            ]
            if any(
                int(item["user_id"]) == int(uid)
                and item["status"] in ("claimed", "review", "done")
                for item in runs
            ):
                continue
            slots_used = len(committed)
            budget_used = sum(
                int(item.get("reward_snapshot") or row.get("reward") or 0)
                for item in committed
            )
            if not row.get("repeatable") and slots_used:
                continue
            if row.get("max_participants") is not None and (
                slots_used >= int(row["max_participants"])
            ):
                continue
            if row.get("budget_cap") is not None and (
                budget_used + int(row.get("reward") or 0) > int(row["budget_cap"])
            ):
                continue
            row["slots_used"] = slots_used
            row["budget_used"] = budget_used
            available.append(row)
            if len(available) >= 100:
                break
        mine_rows = await (await db.execute(
            "SELECT t.*, m.full_name AS assigned_name, "
            "a.id AS assignment_id, a.status AS assignment_status, "
            "a.user_id AS assignment_user_id, a.claimed_at AS assignment_claimed_at, "
            "a.done_at AS assignment_done_at, a.proof_note AS assignment_proof_note, "
            "a.review_note AS assignment_review_note, a.reward_snapshot, "
            "a.due_at AS assignment_due_at, a.revision_due_at "
            "FROM task_assignments a JOIN tasks t ON t.id=a.task_id "
            "LEFT JOIN members m ON m.user_id=t.assigned_to "
            "WHERE a.user_id=? AND a.status IN ('claimed','review') "
            "ORDER BY a.claimed_at DESC",
            (uid,),
        )).fetchall()
        bucket = "0" if not available else ("1-5" if len(available) <= 5 else "6+")
        try:
            await _track_event_in_tx(
                db, "task_catalog_served", "backend", user_id=uid,
                properties={"available_count_bucket": bucket},
                dedupe_key=f"catalog:{uid}:{datetime.now(timezone.utc).date().isoformat()}",
                retention_days=30,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("Best-effort task catalog analytics failed")
    mine = [dict(row) for row in mine_rows]
    await _attach_task_evidence([*available, *mine])
    return _json({
        "ok": True,
        "available": [_task_public(row) for row in available],
        "mine": [_task_public(row) for row in mine],
    })


async def api_task_context(request):
    """Explain why a task deep link is not present in the worker catalog."""
    uid, err = await _require_worker(request)
    if err is not None:
        return err
    task_id = _as_int(request.rel_url.query.get("id"))
    if task_id is None or task_id <= 0:
        return _json({"error": "task", "message": "Некорректная ссылка на задание."}, status=400)
    await _expire_due_tasks()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        task = await (await db.execute(
            "SELECT id,status,city,assigned_to,repeatable,max_participants "
            "FROM tasks WHERE id=?", (task_id,),
        )).fetchone()
        if not task:
            return _json({
                "ok": True, "reason": "not_found",
                "message": "Задание по этой ссылке не найдено или уже удалено.",
            })
        member = await (await db.execute(
            "SELECT city FROM members WHERE user_id=?", (uid,),
        )).fetchone()
        own = await (await db.execute(
            "SELECT status FROM task_assignments WHERE task_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT 1", (task_id, uid),
        )).fetchone()
        active_count = int((await (await db.execute(
            "SELECT COUNT(*) FROM task_assignments WHERE task_id=? "
            "AND status IN ('claimed','review','done')", (task_id,),
        )).fetchone())[0])
    if own:
        messages = {
            "done": "Ты уже выполнил это задание — результат есть в истории бонусов.",
            "rejected": "Этот отчёт был окончательно отклонён ответственным.",
            "cancelled": "Твоё выполнение этого задания отменено.",
            "claimed": "Задание уже находится у тебя в работе.",
            "review": "Твой отчёт уже ожидает проверки.",
        }
        if own["status"] in messages:
            return _json({"ok": True, "reason": own["status"], "message": messages[own["status"]]})
    if _city_key(member["city"] if member else "") != _city_key(task["city"]):
        return _json({
            "ok": True, "reason": "city_mismatch",
            "message": "Задание относится к другому городу. Проверь город в профиле.",
        })
    if task["assigned_to"] is not None and int(task["assigned_to"]) != int(uid):
        return _json({
            "ok": True, "reason": "not_assigned",
            "message": "Это персональное задание назначено другому участнику.",
        })
    if task["status"] == "expired":
        message = "Срок задания по этой ссылке уже закончился."
    elif task["status"] != "open":
        message = "Задание по этой ссылке уже закрыто или отменено."
    elif not task["repeatable"] and active_count:
        message = "Это задание уже взял другой участник."
    elif task["max_participants"] is not None and active_count >= int(task["max_participants"]):
        message = "Все места в этом задании уже заняты."
    else:
        message = "Задание сейчас недоступно. Обнови каталог или уточни у ответственного."
    return _json({"ok": True, "reason": str(task["status"]), "message": message})


def _telegram_message_url(chat_id, message_id, thread_id=None, username=""):
    """Строит официальный t.me message link для public/private supergroup."""
    try:
        message_id = int(message_id or 0)
        thread_id = int(thread_id or 0)
    except (TypeError, ValueError):
        return ""
    if message_id <= 0:
        return ""
    chat_value = str(chat_id or "").strip()
    parts = []
    if chat_value.startswith("-100") and chat_value[4:].isdigit():
        parts = ["https://t.me/c", chat_value[4:]]
    else:
        clean_username = _clean_username(
            chat_value if chat_value.startswith("@") else username
        )
        if not clean_username:
            return ""
        parts = ["https://t.me", clean_username]
    if thread_id > 0:
        parts.append(str(thread_id))
    parts.append(str(message_id))
    return "/".join(parts)


def _task_public(t):
    meta = TASK_TYPES.get(t.get("type"), {})
    announcement_url = ""
    if t.get("announcement_status") == "sent":
        announcement_url = _telegram_message_url(
            t.get("announcement_chat_id"),
            t.get("announcement_message_id"),
            t.get("announcement_thread_id"),
            OPS_GROUP_USERNAME,
        )
    return {
        "id": t["id"],
        "type": t.get("type"),
        "type_title": meta.get("title", t.get("type")),
        "emoji": meta.get("emoji", "📍"),
        "title": t.get("title"),
        "details": t.get("details") or "",
        "lat": t.get("lat"), "lng": t.get("lng"),
        "address": t.get("address") or "",
        "reward": (
            t.get("reward_snapshot")
            if t.get("reward_snapshot") is not None else t.get("reward", 0)
        ),
        "status": t.get("assignment_status") or t.get("status"),
        "task_status": t.get("status"),
        "assignment_status": t.get("assignment_status"),
        "claimed_by": t.get("assignment_user_id") or t.get("claimed_by"),
        "assigned_to": t.get("assigned_to"),
        "assigned_name": t.get("assigned_name") or "",
        "is_personal": bool(t.get("assigned_to")),
        "slot_start": t.get("slot_start"),
        "slot_end": t.get("revision_due_at") or t.get("assignment_due_at") or t.get("slot_end"),
        "revision_due_at": t.get("revision_due_at"),
        "repeatable": bool(t.get("repeatable")),
        "evidence_policy": _public_evidence_policy(t.get("evidence_policy")),
        "max_participants": t.get("max_participants"),
        "budget_cap": t.get("budget_cap"),
        "city": t.get("city") or "",
        "photo_url": (
            _signed_media_url(t.get("photo_media_id"))
            if t.get("photo_media_id") else _signed_photo_url(t.get("photo_file"))
        ),
        "assignment_id": t.get("assignment_id"),
        "can_release": t.get("assignment_status") == "claimed",
        "slots_used": t.get("slots_used"),
        "slots_total": t.get("max_participants"),
        "budget_used": t.get("budget_used"),
        "claimed_name": t.get("claimed_name") or "",
        "proof_note": (
            t.get("assignment_proof_note")
            if t.get("assignment_id") else t.get("proof_note")
        ) or "",
        "review_note": (
            t.get("assignment_review_note")
            if t.get("assignment_id") else t.get("review_note")
        ) or "",
        "evidence": t.get("_evidence") or [],
        "evidence_urls": [
            item["photo_url"] for item in (t.get("_evidence") or [])
        ],
        "proof_photos": [
            item["photo_url"] for item in (t.get("_evidence") or [])
            if item.get("kind") == "after"
        ],
        "announcement_status": t.get("announcement_status") or "not_requested",
        "announcement_attempts": int(t.get("announcement_attempts") or 0),
        "announcement_error": t.get("announcement_error") or "",
        "announcement_sent_at": t.get("announcement_sent_at") or "",
        "announcement_message_id": t.get("announcement_message_id"),
        "announcement_thread_id": t.get("announcement_thread_id"),
        "announcement_url": announcement_url,
    }


def _admin_task_public(t, admin_id):
    """Add maker-checker UI state without exposing Telegram IDs."""
    item = _task_public(t)
    if t.get("assignment_status") == "review":
        self_review = int(t.get("assignment_user_id") or 0) == int(admin_id)
        maker_review = int(t.get("created_by") or 0) == int(admin_id)
        item["can_approve"] = not (self_review or maker_review)
        item["approval_block_reason"] = (
            "Нельзя подтверждать собственное выполнение — нужен второй ответственный."
            if self_review else
            "Создатель задания не подтверждает выплату — нужен второй ответственный."
            if maker_review else ""
        )
    return item


def _admin_decision_public(t, admin_id, active_admin_ids=()):
    item = _task_public(t)
    dispute_status = t.get("dispute_status") or ""
    assignment_user = int(t.get("assignment_user_id") or 0)
    eligible_deciders = {
        int(candidate) for candidate in active_admin_ids
        if int(candidate) not in {int(admin_id), assignment_user}
    }
    can_open = (
        t.get("assignment_status") == "done" and not dispute_status
        and assignment_user != int(admin_id) and bool(eligible_deciders)
    )
    item.update({
        "dispute_id": t.get("dispute_id"),
        "dispute_status": dispute_status,
        "dispute_reason": t.get("dispute_reason") or "",
        "dispute_reconciliation_reason": (
            t.get("dispute_reconciliation_reason") or ""
        ),
        "dispute_reconciliation_reference": (
            t.get("dispute_reconciliation_reference") or ""
        ),
        "dispute_decision_note": t.get("dispute_decision_note") or "",
        "dispute_opened_at": t.get("dispute_opened_at"),
        "dispute_decided_at": t.get("dispute_decided_at"),
        "dispute_opened_by_name": t.get("dispute_opened_by_name") or "",
        "dispute_decided_by_name": t.get("dispute_decided_by_name") or "",
        "assignment_terminal_by_name": t.get("assignment_terminal_by_name") or "",
        "can_open_dispute": can_open,
        "eligible_decider_count": len(eligible_deciders),
        "dispute_open_block_reason": (
            "Нужен ещё один действующий ответственный, который не является исполнителем."
            if t.get("assignment_status") == "done" and not dispute_status
            and assignment_user != int(admin_id) and not eligible_deciders else ""
        ),
        "can_decide_dispute": (
            dispute_status in {"pending", "manual_required"}
            and int(t.get("dispute_opened_by") or 0) != int(admin_id)
            and assignment_user != int(admin_id)
        ),
        "dispute_waits_for_second": (
            dispute_status in {"pending", "manual_required"}
            and int(t.get("dispute_opened_by") or 0) == int(admin_id)
        ),
    })
    return item


def _evidence_public(row):
    return {
        "id": row["id"],
        "assignment_id": row["assignment_id"],
        "task_id": row["task_id"],
        "user_id": row["user_id"],
        "kind": row["kind"],
        "attempt": row.get("attempt", 1),
        "created_at": row["created_at"],
        "photo_url": (
            _signed_media_url(row.get("media_id"))
            if row.get("media_id") else _signed_photo_url(row["photo_file"])
        ),
    }


def _signed_photo_url(filename):
    """Короткоживущая ссылка для <img>, где нельзя передать auth header."""
    if not filename:
        return ""
    expires = int(time.time()) + PHOTO_URL_TTL_SEC
    message = f"{filename}:{expires}".encode("utf-8")
    signature = hmac.new(
        (MEDIA_SIGNING_KEY or BOT_TOKEN).encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    return f"/task-photo/{filename}?expires={expires}&signature={signature}"


def _signed_media_url(media_id):
    if not media_id:
        return ""
    expires = int(time.time()) + PHOTO_URL_TTL_SEC
    message = f"media:{media_id}:{expires}".encode("utf-8")
    signature = hmac.new(
        (MEDIA_SIGNING_KEY or BOT_TOKEN).encode("utf-8"),
        message, hashlib.sha256,
    ).hexdigest()
    return f"/media/{media_id}?expires={expires}&signature={signature}"


async def _attach_task_evidence(tasks):
    """Добавляет к dict-задачам brief и evidence конкретного выполнения."""
    task_ids = sorted({int(t["id"]) for t in tasks if t.get("id") is not None})
    if not task_ids:
        return tasks
    placeholders = ",".join("?" for _ in task_ids)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            f"SELECT * FROM task_evidence WHERE task_id IN ({placeholders}) "
            "AND (kind='brief' OR is_current=1) "
            "ORDER BY id",
            task_ids,
        )).fetchall()
    by_task = {}
    for row in rows:
        by_task.setdefault(int(row["task_id"]), []).append(dict(row))
    for task in tasks:
        assignment_id = task.get("assignment_id")
        evidence = []
        for row in by_task.get(int(task["id"]), []):
            if row["kind"] == "brief":
                evidence.append(_evidence_public(row))
            elif assignment_id is not None and row["assignment_id"] == assignment_id:
                evidence.append(_evidence_public(row))
            elif assignment_id is None and row["assignment_id"] is None:
                evidence.append(_evidence_public(row))
        task["_evidence"] = evidence
    return tasks


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
    await _expire_due_tasks()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT * FROM tasks WHERE id=?",
            (tid,),
        )).fetchone()
        if not row:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if row["status"] != "open":
            await db.rollback()
            message = (
                "Срок задания уже закончился."
                if row["status"] == "expired" else "Задание уже недоступно."
            )
            return _json({"error": row["status"], "message": message}, status=409)
        if row["assigned_to"] is not None and int(row["assigned_to"]) != int(uid):
            await db.rollback()
            return _json({
                "error": "not_assigned",
                "message": "Это задание назначено другому сотруднику.",
            }, status=403)
        member = await (await db.execute(
            "SELECT city,role FROM members WHERE user_id=? AND status='approved'",
            (uid,),
        )).fetchone()
        worker_city = _city_key(member["city"] if member else "")
        task_city = _city_key(row["city"])
        if not worker_city or not task_city or worker_city != task_city:
            await db.rollback()
            return _json({
                "error": "city_mismatch",
                "message": "Это задание доступно только участникам из указанного города.",
            }, status=403)
        now = datetime.now(timezone.utc)
        try:
            slot_start = (
                datetime.fromisoformat(row["slot_start"]).astimezone(timezone.utc)
                if row["slot_start"] else None
            )
            slot_end = (
                datetime.fromisoformat(row["slot_end"]).astimezone(timezone.utc)
                if row["slot_end"] else None
            )
        except (TypeError, ValueError):
            await db.rollback()
            return _json({
                "error": "slot",
                "message": "Временное окно задания настроено неверно. Сообщи ответственному.",
            }, status=409)
        if slot_start and now < slot_start:
            await db.rollback()
            return _json({
                "error": "too_early",
                "message": "Взять задание можно после начала указанного временного окна.",
            }, status=409)
        if slot_end and now > slot_end:
            await db.rollback()
            return _json({
                "error": "expired",
                "message": "Временное окно задания уже закончилось.",
            }, status=409)
        runs = await (await db.execute(
            "SELECT id, user_id, status, reward_snapshot FROM task_assignments "
            "WHERE task_id=?", (tid,),
        )).fetchall()
        own_active = next((
            item for item in runs
            if int(item["user_id"]) == int(uid)
            and item["status"] in ("claimed", "review")
        ), None)
        if own_active:
            await db.rollback()
            return _json({
                "ok": True, "assignment_id": own_active["id"],
                "repeatable": bool(row["repeatable"]), "idempotent": True,
            })
        if any(
            int(item["user_id"]) == int(uid) and item["status"] == "done"
            for item in runs
        ):
            await db.rollback()
            return _json({
                "error": "already_completed",
                "message": "Ты уже выполнил это задание.",
            }, status=409)
        committed = [
            item for item in runs
            if item["status"] in ("claimed", "review", "done")
        ]
        if not row["repeatable"] and committed:
            await db.rollback()
            return _json({"error": "taken", "message": "Задание уже взято."}, status=409)
        if row["repeatable"] and row["assigned_to"] is not None:
            await db.rollback()
            return _json({"error": "bad_task"}, status=409)
        max_participants = int(row["max_participants"] or 1)
        if len(committed) >= max_participants:
            await db.rollback()
            return _json({
                "error": "participants_limit",
                "message": "Все места в этом задании уже заняты.",
            }, status=409)
        committed_budget = sum(
            int(item["reward_snapshot"] or row["reward"] or 0)
            for item in committed
        )
        budget_cap = int(row["budget_cap"] or row["reward"] or 0)
        if committed_budget + int(row["reward"] or 0) > budget_cap:
            await db.rollback()
            return _json({
                "error": "budget_limit",
                "message": "Бюджет этого задания уже распределён.",
            }, status=409)
        if (
            member["role"] == "admin"
            and not await _admin_task_has_independent_review_path_in_tx(
                db, row["created_by"], uid,
            )
        ):
            await db.rollback()
            return _json({
                "error": "admin_task_independence",
                "message": (
                    "Ответственный не может взять это задание: нужны два других "
                    "действующих администратора для независимой проверки и возможного спора."
                ),
            }, status=409)
        claimed_at = now_iso()
        try:
            cur = await db.execute(
                "INSERT INTO task_assignments "
                "(task_id, user_id, status, claimed_at, reward_snapshot, due_at) "
                "VALUES (?, ?, 'claimed', ?, ?, ?)",
                (tid, uid, claimed_at, int(row["reward"] or 0), row["slot_end"]),
            )
        except aiosqlite.IntegrityError:
            await db.rollback()
            return _json({
                "error": "claim_conflict",
                "message": "Задание только что изменилось. Обнови список.",
            }, status=409)
        assignment_id = cur.lastrowid
        if not row["repeatable"]:
            updated = await db.execute(
                "UPDATE tasks SET status='closed', version=version+1 "
                "WHERE id=? AND status='open'", (tid,),
            )
            if updated.rowcount != 1:
                await db.rollback()
                return _json({"error": "taken"}, status=409)
        await _track_event_in_tx(
            db, "task_claimed", "backend", user_id=uid, task_id=tid,
            assignment_id=assignment_id,
            properties={
                "task_type": row["type"], "repeatable": bool(row["repeatable"]),
            },
            dedupe_key=f"task_claim:{assignment_id}",
        )
        await db.commit()
    return _json({
        "ok": True, "assignment_id": assignment_id,
        "repeatable": bool(row["repeatable"]), "idempotent": False,
    })


async def api_task_release(request):
    """Исполнитель освобождает claim до отправки отчёта, сохраняя историю."""
    uid, err = await _require_worker(request)
    if err is not None:
        return err
    body = await _body(request)
    tid = _as_int(body.get("task_id"))
    operation_id = _operation_uuid(body.get("operation_id"))
    reason = " ".join(str(body.get("reason") or "").split())[:200]
    if tid is None or not operation_id:
        return _json({
            "error": "bad_request",
            "message": "Нужны task_id и operation_id в формате UUID.",
        }, status=400)
    if len(reason) < 3:
        return _json({
            "error": "reason", "message": "Коротко укажи причину отказа.",
        }, status=400)
    request_hash = _request_fingerprint({
        "task_id": tid, "user_id": int(uid), "reason": reason,
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        replay = await (await db.execute(
            "SELECT * FROM task_assignments WHERE release_operation_id=?",
            (operation_id,),
        )).fetchone()
        if replay:
            if (
                int(replay["task_id"]) != tid
                or int(replay["user_id"]) != int(uid)
                or replay["release_request_hash"] != request_hash
            ):
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            await db.rollback()
            return _json({
                "ok": True, "task_id": tid, "assignment_id": replay["id"],
                "status": "released", "idempotent": True,
            })
        task = await (await db.execute(
            "SELECT * FROM tasks WHERE id=?", (tid,),
        )).fetchone()
        assignment = await (await db.execute(
            "SELECT * FROM task_assignments WHERE task_id=? AND user_id=? "
            "AND status='claimed' ORDER BY id DESC LIMIT 1",
            (tid, uid),
        )).fetchone()
        if not task or not assignment:
            await db.rollback()
            return _json({
                "error": "not_releasable",
                "message": "Отказаться можно только до отправки отчёта.",
            }, status=409)
        released_at = now_iso()
        cur = await db.execute(
            "UPDATE task_assignments SET status='released', released_at=?, "
            "release_reason=?, release_operation_id=?, release_request_hash=?, "
            "terminal_at=?, terminal_reason='released_by_worker', version=version+1 "
            "WHERE id=? AND status='claimed'",
            (
                released_at, reason, operation_id, request_hash,
                released_at, assignment["id"],
            ),
        )
        if cur.rowcount != 1:
            await db.rollback()
            return _json({"error": "transition_conflict"}, status=409)
        if not task["repeatable"]:
            next_status = "open"
            if task["slot_end"]:
                try:
                    due = datetime.fromisoformat(task["slot_end"])
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=timezone.utc)
                    if due.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                        next_status = "expired"
                except (TypeError, ValueError):
                    next_status = "expired"
            await db.execute(
                "UPDATE tasks SET status=?, expired_at=CASE WHEN ?='expired' "
                "THEN ? ELSE expired_at END, version=version+1 WHERE id=?",
                (next_status, next_status, released_at, tid),
            )
        await _track_event_in_tx(
            db, "task_released", "backend", user_id=uid, task_id=tid,
            assignment_id=assignment["id"], outcome="released",
            dedupe_key=f"task_release:{operation_id}",
        )
        await _enqueue_capability_holders_in_tx(
            db, f"assignment:{assignment['id']}:released",
            f"↩️ Исполнитель отказался от задания #{tid}: {task['title']}\n"
            f"Причина: {reason}", "task.view",
        )
        await db.commit()
    return _json({
        "ok": True, "task_id": tid, "assignment_id": assignment["id"],
        "status": "released", "idempotent": False,
    })


async def api_admin_task_cancel(request):
    """Ответственный отменяет оффер; submitted review сначала надо решить."""
    admin_id, err = await _require_capability(request, "task.cancel")
    if err is not None:
        return err
    body = await _body(request)
    tid = _as_int(body.get("task_id"))
    operation_id = _operation_uuid(body.get("operation_id"))
    reason = " ".join(str(body.get("reason") or "").split())[:200]
    if tid is None or not operation_id:
        return _json({"error": "bad_request"}, status=400)
    if len(reason) < 3:
        return _json({
            "error": "reason", "message": "Укажи причину отмены.",
        }, status=400)
    request_hash = _request_fingerprint({
        "task_id": tid, "admin_id": int(admin_id), "reason": reason,
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "task.cancel"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        task = await (await db.execute(
            "SELECT * FROM tasks WHERE id=?", (tid,),
        )).fetchone()
        if not task:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if task["cancel_operation_id"] == operation_id:
            if task["cancel_request_hash"] != request_hash:
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            await db.rollback()
            return _json({
                "ok": True, "task_id": tid, "status": "cancelled",
                "idempotent": True,
            })
        used = await (await db.execute(
            "SELECT id FROM tasks WHERE cancel_operation_id=? AND id<>?",
            (operation_id, tid),
        )).fetchone()
        if used:
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        if task["status"] not in ("open", "closed"):
            await db.rollback()
            return _json({
                "error": "not_cancellable",
                "message": "Это задание уже закрыто или отменено.",
            }, status=409)
        review = await (await db.execute(
            "SELECT 1 FROM task_assignments "
            "WHERE task_id=? AND status='review' LIMIT 1", (tid,),
        )).fetchone()
        if review:
            await db.rollback()
            return _json({
                "error": "review_pending",
                "message": "Сначала реши все отчёты на проверке.",
            }, status=409)
        done_exists = await (await db.execute(
            "SELECT 1 FROM task_assignments WHERE task_id=? AND status='done' LIMIT 1",
            (tid,),
        )).fetchone()
        if not int(task["repeatable"] or 0) and done_exists:
            await db.rollback()
            return _json({
                "error": "already_completed",
                "message": "Выполненное одноразовое задание нельзя отменить после выплаты.",
            }, status=409)
        affected_rows = await (await db.execute(
            "SELECT user_id FROM task_assignments "
            "WHERE task_id=? AND status='claimed'", (tid,),
        )).fetchall()
        cancelled_at = now_iso()
        await db.execute(
            "UPDATE task_assignments SET status='cancelled', terminal_at=?, "
            "terminal_by=?, terminal_reason=?, version=version+1 "
            "WHERE task_id=? AND status='claimed'",
            (cancelled_at, admin_id, reason, tid),
        )
        cur = await db.execute(
            "UPDATE tasks SET status='cancelled', cancel_operation_id=?, "
            "cancel_request_hash=?, cancelled_at=?, cancelled_by=?, "
            "cancel_reason=?, version=version+1 "
            "WHERE id=? AND status IN ('open','closed')",
            (operation_id, request_hash, cancelled_at, admin_id, reason, tid),
        )
        if cur.rowcount != 1:
            await db.rollback()
            return _json({"error": "transition_conflict"}, status=409)
        await _track_event_in_tx(
            db, "task_cancelled", "backend", task_id=tid,
            outcome="cancelled", dedupe_key=f"task_cancel:{operation_id}",
        )
        affected = {int(row["user_id"]) for row in affected_rows}
        if task["assigned_to"]:
            affected.add(int(task["assigned_to"]))
        for user_id in affected:
            await _enqueue_outbox_in_tx(
                db, f"task:{tid}:cancelled:user:{user_id}", "direct",
                {"text": (
                    f"Задание #{tid} «{task['title']}» отменено.\nПричина: {reason}"
                ), "start": None},
                recipient_id=user_id,
            )
        await db.commit()
    return _json({
        "ok": True, "task_id": tid, "status": "cancelled",
        "affected": len(affected), "idempotent": False,
    })


async def api_task_complete(request):
    uid, err = await _require_worker(request)
    if err is not None:
        return err
    body = await _body(request)
    tid = _as_int(body.get("task_id"))
    if tid is None:
        return _json({"error": "task"}, status=400)
    requested_assignment_id = _as_int(body.get("assignment_id"))
    if requested_assignment_id is None:
        return _json({
            "error": "assignment_id",
            "message": "Не удалось определить конкретное выполнение задания. Обнови список.",
        }, status=400)
    operation_id = _operation_uuid(body.get("operation_id"))
    if not operation_id:
        return _json({
            "error": "operation_id",
            "message": "Для безопасной отправки отчёта нужен operation_id в формате UUID.",
        }, status=400)
    note = (body.get("note") or "").strip()[:300]
    raw_photos = body.get("proof_photos", [])
    if raw_photos in (None, ""):
        raw_photos = []
    if not isinstance(raw_photos, list):
        return _json({
            "error": "proof_photos",
            "message": "Фотографии результата должны быть списком.",
        }, status=400)
    if len(raw_photos) > 4:
        return _json({
            "error": "proof_photos",
            "message": "К одному выполнению можно прикрепить не больше четырёх фотографий.",
        }, status=400)
    request_hash = _request_fingerprint({
        "task_id": tid,
        "assignment_id": requested_assignment_id,
        "user_id": int(uid),
        "note": note,
        "proof_photos": [
            hashlib.sha256(
                json.dumps(
                    item, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            for item in raw_photos
        ],
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        prior = await (await db.execute(
            "SELECT * FROM task_completion_commands WHERE operation_id=?",
            (operation_id,),
        )).fetchone()
        if prior:
            if (
                int(prior["assignment_id"]) != requested_assignment_id
                or prior["request_hash"] != request_hash
            ):
                return _json({"error": "operation_conflict"}, status=409)
            evidence_rows = await (await db.execute(
                "SELECT * FROM task_evidence WHERE assignment_id=? "
                "AND submission_operation_id=? ORDER BY id",
                (requested_assignment_id, operation_id),
            )).fetchall()
            return _json({
                "ok": True, "assignment_id": requested_assignment_id,
                "operation_id": operation_id, "status": prior["result_status"],
                "idempotent": True,
                "evidence": [_evidence_public(dict(item)) for item in evidence_rows],
            })
    try:
        saved = await _save_proof_photos(
            raw_photos, operation_id=operation_id, request_hash=request_hash,
        )
    except ValueError as exc:
        status = 409 if "upload operation" in str(exc) else 400
        return _json({"error": "proof_photos", "message": str(exc)}, status=status)
    assignment_id = None
    try:
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            prior_command = await (await db.execute(
                "SELECT * FROM task_completion_commands WHERE operation_id=?",
                (operation_id,),
            )).fetchone()
            if prior_command:
                if (
                    int(prior_command["assignment_id"]) != requested_assignment_id
                    or prior_command["request_hash"] != request_hash
                ):
                    await db.rollback()
                    return _json({"error": "operation_conflict"}, status=409)
                evidence_rows = await (await db.execute(
                    "SELECT * FROM task_evidence "
                    "WHERE assignment_id=? AND submission_operation_id=? ORDER BY id",
                    (requested_assignment_id, operation_id),
                )).fetchall()
                await db.rollback()
                return _json({
                    "ok": True, "assignment_id": requested_assignment_id,
                    "operation_id": operation_id, "status": prior_command["result_status"],
                    "idempotent": True,
                    "evidence": [_evidence_public(dict(item)) for item in evidence_rows],
                })
            row = await (await db.execute(
                "SELECT * FROM tasks WHERE id=?", (tid,))).fetchone()
            if not row:
                await db.rollback()
                return _json({"error": "not_found"}, status=404)
            policy = _evidence_policy(row["evidence_policy"])
            if policy is None:
                await db.rollback()
                return _json({
                    "error": "evidence_policy",
                    "message": "Политика фотоотчёта настроена неверно. Сообщи ответственному.",
                }, status=409)
            if policy == "comment_only" and len(note) < 3:
                await db.rollback()
                return _json({
                    "error": "comment_required",
                    "message": "Коротко опиши результат выполнения.",
                }, status=400)
            assignment = await (await db.execute(
                "SELECT * FROM task_assignments "
                "WHERE id=? AND task_id=? AND user_id=?",
                (requested_assignment_id, tid, uid),
            )).fetchone()
            if not assignment:
                await db.rollback()
                return _json({"error": "not_yours"}, status=403)
            assignment_id = int(assignment["id"])
            previous_operation = assignment["completion_operation_id"]
            previous_hash = assignment["completion_request_hash"]
            target_status = assignment["status"]
            attempt = int(assignment["submission_attempt"] or 0) + 1

            if previous_operation == operation_id:
                if previous_hash and previous_hash != request_hash:
                    await db.rollback()
                    return _json({
                        "error": "operation_conflict",
                        "message": "Этот operation_id уже использован для другого отчёта.",
                    }, status=409)
                if target_status not in ("review", "done"):
                    await db.rollback()
                    return _json({
                        "error": "report_not_current",
                        "message": "Этот отчёт уже отклонён. Отправь исправленный отчёт заново.",
                    }, status=409)
                evidence_rows = await (await db.execute(
                    "SELECT * FROM task_evidence "
                    "WHERE task_id=? AND user_id=? AND submission_operation_id=? "
                    "ORDER BY id",
                    (tid, uid, operation_id),
                )).fetchall()
                await db.rollback()
                return _json({
                    "ok": True,
                    "assignment_id": assignment_id,
                    "operation_id": operation_id,
                    "idempotent": True,
                    "evidence": [
                        _evidence_public(dict(item)) for item in evidence_rows
                    ],
                })

            task_conflict = await (await db.execute(
                "SELECT id FROM tasks WHERE completion_operation_id=? LIMIT 1",
                (operation_id,),
            )).fetchone()
            assignment_conflict = await (await db.execute(
                "SELECT id FROM task_assignments "
                "WHERE completion_operation_id=? LIMIT 1",
                (operation_id,),
            )).fetchone()
            if task_conflict or assignment_conflict:
                await db.rollback()
                return _json({
                    "error": "operation_conflict",
                    "message": "Этот operation_id уже использован для другого отчёта.",
                }, status=409)
            if target_status != "claimed":
                await db.rollback()
                return _json({"error": "not_yours"}, status=403)
            due_raw = assignment["revision_due_at"] or assignment["due_at"]
            if due_raw:
                try:
                    due = datetime.fromisoformat(due_raw)
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) > due.astimezone(timezone.utc):
                        await db.rollback()
                        return _json({
                            "error": "expired",
                            "message": "Срок выполнения закончился. Свяжись с ответственным.",
                        }, status=409)
                except (TypeError, ValueError):
                    await db.rollback()
                    return _json({"error": "due_at"}, status=409)

            count_row = await (await db.execute(
                "SELECT COUNT(*) FROM task_evidence "
                "WHERE task_id=? AND assignment_id=? AND user_id=? "
                "AND kind='after' AND is_current=1",
                (tid, assignment_id, uid),
            )).fetchone()
            existing_after = int(count_row[0])
            if existing_after + len(raw_photos) > 4:
                await db.rollback()
                return _json({
                    "error": "proof_photos",
                    "message": "У этого выполнения уже есть фотографии: всего можно хранить не больше четырёх.",
                }, status=409)
            after_required = policy in ("photo_required", "before_after")
            if after_required and existing_after + len(raw_photos) < 1:
                await db.rollback()
                return _json({
                    "error": "photo_required",
                    "message": "Для этого задания нужно прикрепить хотя бы одно фото результата.",
                }, status=400)
            if policy == "before_after":
                brief = await (await db.execute(
                    "SELECT 1 FROM task_evidence "
                    "WHERE task_id=? AND kind='brief' LIMIT 1",
                    (tid,),
                )).fetchone()
                if not brief:
                    await db.rollback()
                    return _json({
                        "error": "brief_required",
                        "message": "У задания нет исходного фото. Сообщи ответственному.",
                    }, status=409)
            completed_at = now_iso()
            for image in saved:
                media_claim = await db.execute(
                    "UPDATE media_objects SET delete_after=NULL "
                    "WHERE id=? AND state='ready'",
                    (image["media_id"],),
                )
                if media_claim.rowcount != 1:
                    await db.rollback()
                    return _json({
                        "error": "media_not_ready",
                        "message": "Фотография ещё не готова или уже удаляется. Отправь отчёт заново.",
                    }, status=409)
            await db.execute(
                "UPDATE task_evidence SET is_current=0 "
                "WHERE task_id=? AND assignment_id=? AND user_id=? "
                "AND kind='after' AND is_current=1",
                (tid, assignment_id, uid),
            )
            for image in saved:
                await db.execute(
                    "INSERT INTO task_evidence "
                    "(assignment_id, task_id, user_id, kind, photo_file,media_id,sha256, "
                    "submission_operation_id, attempt, is_current, created_at) "
                    "VALUES (?, ?, ?, 'after', ?, ?, ?, ?, ?, 1, ?)",
                    (
                        assignment_id, tid, uid, image["photo_file"], image["media_id"],
                        image["sha256"], operation_id, attempt, completed_at,
                    ),
                )
            cur = await db.execute(
                "UPDATE task_assignments SET status='review', done_at=?, proof_note=?, "
                "review_note=NULL, completion_operation_id=?, "
                "completion_request_hash=?, submission_attempt=?, version=version+1 "
                "WHERE id=? AND status='claimed'",
                (
                    completed_at, note, operation_id, request_hash,
                    attempt, assignment_id,
                ),
            )
            if cur.rowcount != 1:
                await db.rollback()
                return _json({"error": "stale"}, status=409)
            await db.execute(
                "INSERT INTO task_completion_commands "
                "(operation_id,assignment_id,request_hash,result_status,created_at) "
                "VALUES (?,?,?,'review',?)",
                (operation_id, assignment_id, request_hash, completed_at),
            )
            photo_bucket = "0" if not saved else ("1" if len(saved) == 1 else "2-4")
            await _track_event_in_tx(
                db, "proof_submitted", "backend", user_id=uid, task_id=tid,
                assignment_id=assignment_id,
                properties={"photo_count_bucket": photo_bucket},
                dedupe_key=f"proof:{operation_id}",
            )
            await _enqueue_capability_holders_in_tx(
                db, f"assignment:{assignment_id}:proof:{operation_id}",
                f"✅ Задание #{tid} отправлено на проверку.\n"
                f"Комментарий: {note or '—'}\n"
                f"Фото результата: {len(saved)}\n"
                f"Открой Модерацию, чтобы подтвердить и начислить бонусы.",
                "task.review.queue",
            )
            await db.commit()
    except Exception:
        raise
    return _json({
        "ok": True,
        "assignment_id": assignment_id,
        "operation_id": operation_id,
        "idempotent": False,
        "evidence": [
            {"kind": "after", "photo_url": _signed_media_url(image["media_id"])}
            for image in saved
        ],
    })


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
            "SELECT id, amount, status, created_at, decided_at, note, "
            "account_masked, provider, external_reference, reject_reason "
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
        "ride_rub_per_min": RIDE_RUB_PER_MIN,
        "withdraw_min_minutes": ride_minutes_for(WITHDRAW_MIN),
        "withdrawals": [_withdrawal_public(dict(r)) for r in withdrawals],
    })


def _withdrawal_public(item, viewer_id=None):
    """Безопасное представление: ciphertext и полный внешний номер не выходят."""
    external = str(item.get("external_reference") or "")
    item = dict(item)
    item.pop("account_ciphertext", None)
    item["external_reference"] = (
        ("…" + external[-6:]) if len(external) > 6 else external
    )
    if "processing_by" in item:
        owner = _as_int(item.get("processing_by"))
        lease_until = None
        timestamp_valid = False
        raw_processing_at = item.get("processing_at")
        if owner is not None and raw_processing_at:
            try:
                processing_at = datetime.fromisoformat(str(raw_processing_at))
                if processing_at.tzinfo is None:
                    processing_at = processing_at.replace(tzinfo=timezone.utc)
                processing_at = processing_at.astimezone(timezone.utc)
                lease_until = processing_at + timedelta(
                    minutes=WITHDRAW_PROCESSING_LEASE_MIN,
                )
                timestamp_valid = True
            except (TypeError, ValueError):
                lease_until = None
        now = datetime.now(timezone.utc)
        remaining = (
            max(0, math.ceil((lease_until - now).total_seconds()))
            if lease_until is not None else 0
        )
        if item.get("status") != "processing":
            lease_state = "inactive"
        elif owner is None:
            lease_state = "unassigned"
        elif remaining <= 0:
            lease_state = "expired"
        elif viewer_id is not None and owner == int(viewer_id):
            lease_state = "held_by_me"
        else:
            lease_state = "held_by_other"
        owned_by_viewer = (
            viewer_id is not None and owner is not None and owner == int(viewer_id)
        )
        item.update({
            "processing_timestamp_valid": timestamp_valid,
            "lease_expires_at": lease_until.isoformat() if lease_until else None,
            "lease_remaining_seconds": remaining,
            "lease_state": lease_state,
            "can_continue": (
                item.get("status") == "pending"
                or lease_state in ("unassigned", "held_by_me")
                or (lease_state == "expired" and owned_by_viewer)
            ),
            "can_release": item.get("status") == "processing" and owned_by_viewer,
            "can_takeover": lease_state == "expired" and not owned_by_viewer,
            "can_reject": (
                item.get("status") == "pending"
                or (item.get("status") == "processing" and owned_by_viewer)
            ),
        })
    return item


async def api_withdraw_request(request):
    """Шифрует получателя и атомарно резервирует бонусы один раз."""
    uid, err = await _require_worker(request)
    if err is not None:
        return err
    body = await _body(request)
    try:
        amount = int(body.get("amount"))
        account_ref = _normalize_account_ref(body.get("account_ref"))
    except (TypeError, ValueError) as exc:
        message = str(exc) or "Укажи сумму перевода и ID аккаунта."
        return _json({"error": "request", "message": message}, status=400)
    operation_id = _operation_uuid(body.get("operation_id"))
    if not operation_id:
        return _json({"error": "amount", "message": "Укажи сумму перевода."}, status=400)
    if WITHDRAW_FERNET is None:
        return _json({
            "error": "withdrawal_encryption",
            "message": "Переводы временно недоступны: ответственный настраивает защищённое хранение.",
        }, status=503)
    if amount < WITHDRAW_MIN:
        return _json({
            "error": "minimum",
            "message": f"Минимальная сумма перевода — {WITHDRAW_MIN} бонусов.",
        }, status=400)
    fingerprint = _account_fingerprint(account_ref)
    masked = _mask_account_ref(account_ref)
    request_hash = _request_fingerprint({
        "user_id": int(uid), "amount": amount,
        "account_type": "bibibike_account_id", "account_fingerprint": fingerprint,
    })
    ciphertext = _encrypt_account_ref(account_ref)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _claim_operation_in_tx(
            db, operation_id, "withdrawal_request", request_hash, uid,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        replay = await (await db.execute(
            "SELECT * FROM withdrawal_requests WHERE operation_id=?",
            (operation_id,),
        )).fetchone()
        if replay:
            if replay["request_hash"] != request_hash or int(replay["user_id"]) != int(uid):
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            balance_row = await (await db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (uid,),
            )).fetchone()
            await db.rollback()
            return _json({
                "ok": True, "request_id": replay["id"],
                "amount": replay["amount"], "balance": int(balance_row[0]),
                "account_masked": replay["account_masked"],
                "operation_id": operation_id, "idempotent": True,
            })
        member = await (await db.execute(
            "SELECT full_name, username, bonus FROM members "
            "WHERE user_id=? AND status='approved'",
            (uid,),
        )).fetchone()
        if not member:
            await db.rollback()
            return _json({"error": "not_approved"}, status=403)
        disputed = await (await db.execute(
            "SELECT id FROM task_disputes WHERE user_id=? "
            "AND status IN ('pending','manual_required') LIMIT 1",
            (uid,),
        )).fetchone()
        if disputed:
            await db.rollback()
            return _json({
                "error": "disputed_reward",
                "message": "Перевод временно недоступен: ответственным нужно завершить проверку одной выплаты.",
            }, status=409)
        manual_correction = await (await db.execute(
            "SELECT id FROM manual_grant_reversals WHERE user_id=? "
            "AND status IN ('pending','manual_required') LIMIT 1",
            (uid,),
        )).fetchone()
        if manual_correction:
            await db.rollback()
            return _json({
                "error": "manual_grant_correction",
                "message": (
                    "Перевод временно недоступен: два ответственных проверяют "
                    "исправление ручного начисления."
                ),
            }, status=409)
        award_correction = await (await db.execute(
            "SELECT id FROM award_reversals WHERE user_id=? "
            "AND status IN ('pending','manual_required') LIMIT 1",
            (uid,),
        )).fetchone()
        if award_correction:
            await db.rollback()
            return _json({
                "error": "award_reversal_pending",
                "message": (
                    "Перевод временно недоступен: два ответственных проверяют "
                    "исправление награды."
                ),
            }, status=409)
        pending = await (await db.execute(
            "SELECT id FROM withdrawal_requests "
            "WHERE user_id=? AND status IN ('pending','processing')",
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
            "(user_id, amount, status, created_at, operation_id, request_hash, "
            "account_type, account_ciphertext, account_masked, account_fingerprint, "
            "key_version) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                uid, amount, now_iso(), operation_id, request_hash,
                "bibibike_account_id", ciphertext, masked, fingerprint,
            ),
        )
        request_id = cur.lastrowid
        new_balance = balance - amount
        await db.execute(
            "UPDATE members SET bonus=? WHERE user_id=?",
            (new_balance, uid),
        )
        await db.execute(
            "INSERT INTO bonus_ledger "
            "(user_id, amount, reason, task_id, withdrawal_id, created_by, "
            "created_at, operation_id, balance_after) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (
                uid, -amount, f"Резерв на перевод #{request_id}", request_id,
                uid, now_iso(), f"withdraw:{operation_id}:reserve", new_balance,
            ),
        )
        await db.execute(
            "INSERT INTO withdrawal_events "
            "(withdrawal_id,event_type,from_status,to_status,actor_id,operation_id,created_at) "
            "VALUES (?,'requested',NULL,'pending',?,?,?)",
            (request_id, uid, operation_id, now_iso()),
        )
        await _track_event_in_tx(
            db, "withdrawal_requested", "backend", user_id=uid,
            outcome="pending", dedupe_key=f"withdrawal:{operation_id}:requested",
        )
        await _enqueue_capability_holders_in_tx(
            db, f"withdrawal:{request_id}:requested",
            f"💸 Новая заявка на перевод в приложение #{request_id}\n"
            f"Участник: {member['full_name'] or '—'}\n"
            f"Сумма: {amount} бибибонусов\n"
            f"Аккаунт: {masked}\n"
            f"Открой приложение → Модерация → Выводы.",
            "withdrawal.queue.view",
        )
        await db.commit()
    return _json({
        "ok": True,
        "request_id": request_id,
        "amount": amount,
        "balance": new_balance,
        "account_masked": masked,
        "operation_id": operation_id,
        "idempotent": False,
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
    """Общий пароль удалён: роли выдаются только доверенным администратором."""
    return _json({
        "error": "admin_password_removed",
        "message": (
            "Вход по общему паролю отключён из соображений безопасности. "
            "Попроси владельца назначить роль ответственного."
        ),
    }, status=410)


async def _require_admin(request):
    tg = await _auth_user(request)
    if not tg:
        return None, _json({"error": "auth"}, status=401)
    if not await is_admin(tg["id"]):
        return None, _json({"error": "not_admin"}, status=403)
    return tg["id"], None


_DEFAULT_REQUIRE_ADMIN = _require_admin


async def _require_capability(request, capability):
    """Authenticate staff and require one effective immutable capability."""
    if capability not in ALL_STAFF_CAPABILITIES:
        return None, _json({"error": "capability_unknown"}, status=500)
    uid, err = await _require_admin(request)
    if err is not None:
        return uid, err
    # Existing unit tests replace the authentication seam with a trusted actor.
    # This compatibility is test-only; production can never bypass DB grants.
    if (
        BIBITASKS_ENVIRONMENT == "test"
        and _require_admin is not _DEFAULT_REQUIRE_ADMIN
    ):
        return uid, None
    if not await _has_capability(uid, capability):
        return None, _json({
            "error": "capability_required", "capability": capability,
        }, status=403)
    return uid, None


async def _manual_grants_for_overview_in_tx(db):
    """Return every active correction plus 100 most recent inactive grants."""
    return await (await db.execute(
        "SELECT c.operation_id,c.user_id,c.amount,c.reason,c.maker_id,"
        "c.created_at,c.result_balance,recipient.full_name,maker.full_name AS maker_name,"
        "r.id AS reversal_id,r.status AS reversal_status,"
        "r.reason AS reversal_reason,r.manual_reason,r.requested_by,"
        "r.requested_at,r.decided_by,r.decided_at,r.decision_note,"
        "r.result_balance AS reversal_balance,requester.full_name AS requester_name,"
        "checker.full_name AS checker_name "
        "FROM manual_grant_commands c "
        "LEFT JOIN members recipient ON recipient.user_id=c.user_id "
        "LEFT JOIN members maker ON maker.user_id=c.maker_id "
        "LEFT JOIN manual_grant_reversals r ON r.id=("
        "SELECT rr.id FROM manual_grant_reversals rr "
        "WHERE rr.grant_operation_id=c.operation_id ORDER BY rr.id DESC LIMIT 1) "
        "LEFT JOIN members requester ON requester.user_id=r.requested_by "
        "LEFT JOIN members checker ON checker.user_id=r.decided_by "
        "WHERE COALESCE(r.status,'') IN ('pending','manual_required') "
        "OR c.operation_id IN ("
        "SELECT c2.operation_id FROM manual_grant_commands c2 "
        "LEFT JOIN manual_grant_reversals r2 ON r2.id=("
        "SELECT rr2.id FROM manual_grant_reversals rr2 "
        "WHERE rr2.grant_operation_id=c2.operation_id ORDER BY rr2.id DESC LIMIT 1) "
        "WHERE COALESCE(r2.status,'') NOT IN ('pending','manual_required') "
        "ORDER BY c2.created_at DESC,c2.operation_id DESC LIMIT 100) "
        "ORDER BY CASE WHEN COALESCE(r.status,'') IN ('pending','manual_required') "
        "THEN 0 ELSE 1 END,"
        "CASE WHEN COALESCE(r.status,'') IN ('pending','manual_required') "
        "THEN r.requested_at END ASC,c.created_at DESC,c.operation_id DESC"
    )).fetchall()


async def _award_reversals_for_overview_in_tx(db, viewer_id, capabilities):
    history_limit = 100
    history_total = int((await (await db.execute(
        "SELECT COUNT(*) FROM award_reversals "
        "WHERE status IN ('applied','rejected')"
    )).fetchone())[0] or 0)
    rows = await (await db.execute(
        "SELECT r.id,r.member_award_id,r.user_id,r.award_id,r.award_title,"
        "r.amount,r.original_granted_by,r.origin,r.status,r.manual_reason,r.reason,"
        "r.requested_by,r.requested_at,r.decided_by,r.decided_at,r.decision_note,"
        "r.result_balance,ma.note AS original_note,ma.granted_at,a.emoji,"
        "recipient.full_name,granter.full_name AS granter_name,"
        "requester.full_name AS requester_name,checker.full_name AS checker_name,"
        "recipient.bonus AS current_balance "
        "FROM award_reversals r "
        "JOIN member_awards ma ON ma.id=r.member_award_id "
        "JOIN awards a ON a.id=r.award_id "
        "JOIN members recipient ON recipient.user_id=r.user_id "
        "LEFT JOIN members granter ON granter.user_id=r.original_granted_by "
        "LEFT JOIN members requester ON requester.user_id=r.requested_by "
        "LEFT JOIN members checker ON checker.user_id=r.decided_by "
        "WHERE r.status IN ('pending','manual_required') OR r.id IN ("
        "SELECT recent.id FROM award_reversals recent "
        "WHERE recent.status IN ('applied','rejected') "
        "ORDER BY recent.decided_at DESC,recent.id DESC LIMIT ?) "
        "ORDER BY CASE WHEN r.status IN ('pending','manual_required') THEN 0 ELSE 1 END,"
        "CASE WHEN r.status IN ('pending','manual_required') THEN r.requested_at END ASC,"
        "r.decided_at DESC,r.id DESC",
        (history_limit,),
    )).fetchall()
    checker_ids = await _active_capability_holder_ids_in_tx(
        db, "award.reversal.decide",
    )
    requester_ids = await _active_capability_holder_ids_in_tx(
        db, "award.reversal.request",
    )
    show_financial = "member.financial_summary.view" in capabilities
    open_items, history = [], []
    for source in rows:
        item = dict(source)
        reserved = await _reserved_bonus_in_tx(
            db, item["user_id"], exclude_award_reversal_id=item["id"],
        )
        current = int(item["current_balance"] or 0)
        available = max(0, current - reserved)
        excluded = {int(item["user_id"])}
        if item["requested_by"] is not None:
            excluded.add(int(item["requested_by"]))
        if item["original_granted_by"] is not None:
            excluded.add(int(item["original_granted_by"]))
        is_open = item["status"] in {"pending", "manual_required"}
        independent = (
            int(viewer_id) in checker_ids and int(viewer_id) not in excluded
        )
        requester_active = (
            item["requested_by"] is not None
            and int(item["requested_by"]) in requester_ids
        )
        deficit = max(0, int(item["amount"]) - available)
        can_reject = is_open and independent
        can_approve = can_reject and requester_active and deficit == 0
        if not is_open:
            reject_block_reason = "Запрос уже закрыт."
        elif int(viewer_id) not in checker_ids:
            reject_block_reason = "Нет права проверять исправления наград."
        elif int(viewer_id) in excluded:
            reject_block_reason = "Решение должен принять независимый ответственный."
        else:
            reject_block_reason = ""
        if reject_block_reason:
            approve_block_reason = reject_block_reason
        elif not requester_active:
            approve_block_reason = "Полномочие автора запроса отозвано."
        elif deficit:
            approve_block_reason = "Недостаточно свободного баланса для полного сторно."
        else:
            approve_block_reason = ""
        public = {
            "id": item["id"],
            "member_award_id": item["member_award_id"],
            "entry_id": item["member_award_id"],
            "status": item["status"],
            "user_id": item["user_id"],
            "full_name": item["full_name"],
            "award_id": item["award_id"],
            "award_title": item["award_title"],
            "emoji": item["emoji"],
            "amount": int(item["amount"]),
            "original_note": item["original_note"],
            "granted_at": item["granted_at"],
            "original_granted_by": item["original_granted_by"],
            "granter_name": item["granter_name"],
            "reason": item["reason"],
            "requested_by": item["requested_by"],
            "requester_name": item["requester_name"],
            "requested_at": item["requested_at"],
            "decided_by": item["decided_by"],
            "checker_name": item["checker_name"],
            "decided_at": item["decided_at"],
            "decision_note": item["decision_note"],
            "manual_reason": item["manual_reason"],
            "deficit": deficit,
            "can_approve": can_approve,
            "can_reject": can_reject,
            "can_decide": can_approve or can_reject,
            "approve_block_reason": approve_block_reason,
            "reject_block_reason": reject_block_reason,
            "wait_reason": approve_block_reason,
        }
        if show_financial:
            public.update({
                "current_balance": current,
                "reserved_amount": reserved,
                "available_balance": available,
                "result_balance": item["result_balance"],
            })
        if is_open:
            open_items.append(public)
        else:
            history.append(public)
    return {
        "open": open_items,
        "history": history,
        "history_limit": history_limit,
        "history_total": history_total,
        "history_truncated": history_total > len(history),
    }


async def api_admin_overview(request):
    """Сводка для админа: заявки, задания на проверке, открытые задания."""
    uid, err = await _require_admin(request)
    if err is not None:
        return err
    staff_access = await _effective_staff_access(uid)
    capabilities = set(staff_access["capabilities"])
    if BIBITASKS_ENVIRONMENT == "test" and _require_admin is not _DEFAULT_REQUIRE_ADMIN:
        capabilities = set(ALL_STAFF_CAPABILITIES)
    await _expire_due_tasks()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        pending = await (await db.execute(
            "SELECT user_id, full_name, username, city, about, created_at FROM members "
            "WHERE status='pending' AND (applied_at IS NOT NULL OR role='applicant') "
            "ORDER BY applied_at DESC, created_at DESC LIMIT 50")).fetchall()
        pending_total = int((await (await db.execute(
            "SELECT COUNT(*) FROM members WHERE status='pending' "
            "AND (applied_at IS NOT NULL OR role='applicant')"
        )).fetchone())[0])
        rejected = await (await db.execute(
            "SELECT user_id, full_name, username, city, about, "
            "application_note, created_at FROM members "
            "WHERE status='blocked' AND (applied_at IS NOT NULL OR role='applicant') "
            "ORDER BY applied_at DESC, created_at DESC LIMIT 50")).fetchall()
        city_changes = await (await db.execute(
            "SELECT user_id,full_name,username,city,city_change_requested,"
            "city_change_requested_at FROM members "
            "WHERE status='approved' AND city_change_requested IS NOT NULL "
            "AND TRIM(city_change_requested)<>'' "
            "ORDER BY city_change_requested_at ASC,user_id ASC"
        )).fetchall()
        review = await (await db.execute(
            "SELECT t.*, m.full_name AS assigned_name, "
            "a.id AS assignment_id, a.status AS assignment_status, "
            "a.user_id AS assignment_user_id, a.proof_note AS assignment_proof_note, "
            "a.review_note AS assignment_review_note, "
            "a.reward_snapshot, a.done_at AS assignment_done_at, "
            "u.full_name AS claimed_name "
            "FROM task_assignments a "
            "JOIN tasks t ON t.id=a.task_id "
            "LEFT JOIN members m ON m.user_id=t.assigned_to "
            "LEFT JOIN members u ON u.user_id=a.user_id "
            "WHERE a.status='review' ORDER BY a.done_at DESC LIMIT 50"
        )).fetchall()
        review_total = int((await (await db.execute(
            "SELECT COUNT(*) FROM task_assignments WHERE status='review'"
        )).fetchone())[0])
        pending_disputes = await (await db.execute(
            "SELECT t.*,a.id AS assignment_id,a.status AS assignment_status,"
            "a.user_id AS assignment_user_id,a.proof_note AS assignment_proof_note,"
            "a.review_note AS assignment_review_note,a.reward_snapshot,"
            "a.done_at AS assignment_done_at,a.terminal_by AS assignment_terminal_by,"
            "u.full_name AS claimed_name,"
            "d.id AS dispute_id,d.status AS dispute_status,d.reason AS dispute_reason,"
            "d.reconciliation_reason AS dispute_reconciliation_reason,"
            "d.reconciliation_reference AS dispute_reconciliation_reference,"
            "d.opened_by AS dispute_opened_by,d.opened_at AS dispute_opened_at,"
            "d.decision_note AS dispute_decision_note,d.decided_at AS dispute_decided_at,"
            "op.full_name AS dispute_opened_by_name,dc.full_name AS dispute_decided_by_name,"
            "rv.full_name AS assignment_terminal_by_name "
            "FROM task_assignments a JOIN tasks t ON t.id=a.task_id "
            "LEFT JOIN members u ON u.user_id=a.user_id "
            "LEFT JOIN task_disputes d ON d.assignment_id=a.id "
            "LEFT JOIN members op ON op.user_id=d.opened_by "
            "LEFT JOIN members dc ON dc.user_id=d.decided_by "
            "LEFT JOIN members rv ON rv.user_id=a.terminal_by "
            "WHERE d.status IN ('pending','manual_required') "
            "ORDER BY d.opened_at ASC,a.id ASC"
        )).fetchall()
        recent_decisions = await (await db.execute(
            "SELECT t.*,a.id AS assignment_id,a.status AS assignment_status,"
            "a.user_id AS assignment_user_id,a.proof_note AS assignment_proof_note,"
            "a.review_note AS assignment_review_note,a.reward_snapshot,"
            "a.done_at AS assignment_done_at,a.terminal_by AS assignment_terminal_by,"
            "u.full_name AS claimed_name,"
            "d.id AS dispute_id,d.status AS dispute_status,d.reason AS dispute_reason,"
            "d.reconciliation_reason AS dispute_reconciliation_reason,"
            "d.reconciliation_reference AS dispute_reconciliation_reference,"
            "d.opened_by AS dispute_opened_by,d.opened_at AS dispute_opened_at,"
            "d.decision_note AS dispute_decision_note,d.decided_at AS dispute_decided_at,"
            "op.full_name AS dispute_opened_by_name,dc.full_name AS dispute_decided_by_name,"
            "rv.full_name AS assignment_terminal_by_name "
            "FROM task_assignments a JOIN tasks t ON t.id=a.task_id "
            "LEFT JOIN members u ON u.user_id=a.user_id "
            "LEFT JOIN task_disputes d ON d.assignment_id=a.id "
            "LEFT JOIN members op ON op.user_id=d.opened_by "
            "LEFT JOIN members dc ON dc.user_id=d.decided_by "
            "LEFT JOIN members rv ON rv.user_id=a.terminal_by "
            "WHERE a.status IN ('done','reversed') AND "
            "(d.status IS NULL OR d.status NOT IN ('pending','manual_required')) "
            "ORDER BY COALESCE(d.opened_at,a.terminal_at,a.done_at) DESC,a.id DESC LIMIT 50"
        )).fetchall()
        open_tasks = await (await db.execute(
            "SELECT t.*, m.full_name AS assigned_name, "
            "a.id AS assignment_id, a.status AS assignment_status, "
            "a.user_id AS assignment_user_id, a.reward_snapshot, "
            "c.full_name AS claimed_name, o.status AS announcement_status, "
            "o.attempts AS announcement_attempts, "
            "o.last_error AS announcement_error, o.chat_id AS announcement_chat_id, "
            "o.sent_at AS announcement_sent_at, "
            "o.telegram_message_id AS announcement_message_id, "
            "o.telegram_thread_id AS announcement_thread_id FROM tasks t "
            "LEFT JOIN members m ON m.user_id=t.assigned_to "
            "LEFT JOIN task_assignments a ON a.id=("
            "SELECT aa.id FROM task_assignments aa WHERE aa.task_id=t.id "
            "AND aa.status IN ('claimed','review') ORDER BY aa.id DESC LIMIT 1) "
            "LEFT JOIN members c ON c.user_id=a.user_id "
            "LEFT JOIN task_outbox o ON o.event_key=('task:' || t.id || ':announcement') "
            "WHERE t.status='open' OR a.status='claimed' "
            "ORDER BY t.created_at DESC")).fetchall()
        team = await (await db.execute(
            "SELECT user_id, full_name, username, city, about, tags, role, bonus, "
            "done_count, chat_xp, created_at FROM members "
            "WHERE status='approved' ORDER BY done_count DESC, chat_xp DESC "
            "LIMIT 500")).fetchall()
        withdrawals = await (await db.execute(
            "SELECT w.id, w.user_id, w.amount, w.status, w.created_at, "
            "w.decided_at, w.note, w.account_masked, w.provider, "
            "w.external_reference, w.reject_reason, w.processing_by, "
            "w.processing_at, m.full_name, m.username, "
            "p.full_name AS processing_name, p.username AS processing_username "
            "FROM withdrawal_requests w "
            "LEFT JOIN members m ON m.user_id=w.user_id "
            "LEFT JOIN members p ON p.user_id=w.processing_by "
            "WHERE w.status IN ('pending','processing') OR w.id IN ("
            "SELECT recent.id FROM withdrawal_requests recent "
            "WHERE recent.status NOT IN ('pending','processing') "
            "ORDER BY recent.id DESC LIMIT 100) "
            "ORDER BY CASE w.status WHEN 'pending' THEN 0 WHEN 'processing' THEN 1 ELSE 2 END, "
            "w.id DESC"
        )).fetchall()
        awards = await (await db.execute(
            "SELECT * FROM awards ORDER BY active DESC, bonus DESC, id"
        )).fetchall()
        granted = await (await db.execute(
            "SELECT ma.id,ma.user_id,ma.award_id,ma.bonus,ma.note,ma.granted_at,"
            "ma.granted_by,ma.operation_id,a.emoji,a.title,m.full_name,"
            "r.id AS reversal_id,r.status AS reversal_status "
            "FROM member_awards ma "
            "JOIN awards a ON a.id=ma.award_id "
            "LEFT JOIN members m ON m.user_id=ma.user_id "
            "LEFT JOIN award_reversals r ON r.id=(SELECT rr.id FROM award_reversals rr "
            "WHERE rr.member_award_id=ma.id ORDER BY rr.id DESC LIMIT 1) "
            "WHERE ma.revoked_at IS NULL "
            "ORDER BY ma.id DESC"
        )).fetchall()
        manual_grants = await _manual_grants_for_overview_in_tx(db)
        award_reversal_context = await _award_reversals_for_overview_in_tx(
            db, uid, capabilities,
        )
        award_reversal_checkers = await _active_capability_holder_ids_in_tx(
            db, "award.reversal.decide",
        )
        active_admin_ids = await _active_admin_ids_in_tx(db)
        role_changes = await (await db.execute(
            "SELECT rc.*,target.full_name AS user_name,"
            "maker.full_name AS requested_by_name "
            "FROM admin_role_changes rc "
            "LEFT JOIN members target ON target.user_id=rc.user_id "
            "LEFT JOIN members maker ON maker.user_id=rc.requested_by "
            "WHERE rc.status='pending' ORDER BY rc.requested_at ASC,rc.id ASC"
        )).fetchall()
        join_requests = await (await db.execute(
            "SELECT jr.request_key,jr.user_id,jr.source,jr.status,jr.decision,"
            "jr.requested_at,jr.decision_queued_at,jr.decided_at,jr.joined_at,"
            "jr.manual_retry_reason,jr.manual_retry_by,jr.manual_retry_at,"
            "jr.last_error,m.full_name,m.username,m.city "
            "FROM telegram_join_requests jr "
            "LEFT JOIN members m ON m.user_id=jr.user_id "
            "WHERE jr.status NOT IN ('joined','declined') OR jr.request_key IN ("
            "SELECT recent.request_key FROM telegram_join_requests recent "
            "WHERE recent.status IN ('joined','declined') "
            "ORDER BY recent.requested_at DESC LIMIT 100) "
            "ORDER BY CASE jr.status "
            "WHEN 'manual_required' THEN 0 WHEN 'awaiting_review' THEN 1 "
            "WHEN 'awaiting_application' THEN 2 ELSE 3 END,"
            "jr.requested_at DESC LIMIT 200"
        )).fetchall()
    review_tasks = [dict(r) for r in review]
    pending_dispute_tasks = [dict(r) for r in pending_disputes]
    recent_decision_tasks = [dict(r) for r in recent_decisions]
    open_task_items = [dict(r) for r in open_tasks]
    evidence_tasks = []
    if capabilities & {"task.review.queue", "task.review", "task.dispute.request", "task.dispute.decide"}:
        evidence_tasks.extend([*review_tasks, *pending_dispute_tasks, *recent_decision_tasks])
    if "task.view" in capabilities:
        evidence_tasks.extend(open_task_items)
    if evidence_tasks:
        await _attach_task_evidence(evidence_tasks)
    task_templates = (
        await _task_templates_active_public()
        if "task.template.manage" in capabilities else []
    )
    return _json({
        "ok": True,
        "pending": [dict(r) for r in pending] if "application.queue.view" in capabilities else [],
        "pending_total": pending_total if "application.queue.view" in capabilities else 0,
        "rejected": [dict(r) for r in rejected] if "application.queue.view" in capabilities else [],
        "city_changes": [dict(r) for r in city_changes] if "member.city.review" in capabilities else [],
        "review": [_admin_task_public(r, uid) for r in review_tasks] if "task.review.queue" in capabilities else [],
        "review_total": review_total if "task.review.queue" in capabilities else 0,
        "recent_decisions": [
            _admin_decision_public(r, uid, active_admin_ids)
            for r in [*pending_dispute_tasks, *recent_decision_tasks]
        ] if capabilities & {"task.dispute.request", "task.dispute.decide"} else [],
        "pending_dispute_total": len(pending_dispute_tasks) if "task.dispute.decide" in capabilities else 0,
        "open_tasks": [_task_public(r) for r in open_task_items] if "task.view" in capabilities else [],
        "team": [{
            "user_id": r["user_id"], "name": r["full_name"], "role": r["role"],
            "bonus": (
                r["bonus"]
                if "member.financial_summary.view" in capabilities else None
            ),
            "done_count": r["done_count"],
            "chat_xp": r["chat_xp"] or 0,
            "city": r["city"] or "", "username": r["username"] or "",
            "about": r["about"] or "", "tags": _tags_list(r["tags"]),
            "created_at": r["created_at"],
            "trust_name": trust_for(trust_score(r["done_count"], r["chat_xp"]))[1],
            "trust_emoji": trust_for(trust_score(r["done_count"], r["chat_xp"]))[2],
        } for r in team] if "member.search" in capabilities else [],
        "withdrawals": [_withdrawal_public(dict(r), viewer_id=uid) for r in withdrawals] if "withdrawal.queue.view" in capabilities else [],
        "awards": [_award_public(dict(r)) for r in awards] if capabilities & {"award.view", "award.catalog.manage", "award.grant", "award.revoke", "award.reversal.request", "award.reversal.decide"} else [],
        "granted": [{
            **dict(r),
            "can_request_reversal": (
                "award.reversal.request" in capabilities
                and (r["reversal_status"] or "") in {"", "rejected"}
                and int(r["user_id"]) != int(uid)
                and bool(award_reversal_checkers - {
                    int(uid), int(r["user_id"]), int(r["granted_by"] or 0),
                })
            ),
        } for r in granted] if capabilities & {"award.grant", "award.revoke", "award.reversal.request", "award.reversal.decide"} else [],
        "award_reversals": award_reversal_context if capabilities & {
            "award.reversal.request", "award.reversal.decide", "award.revoke",
        } else {
            "open": [], "history": [], "history_limit": 100,
            "history_total": 0, "history_truncated": False,
        },
        "manual_grants": [{
            **dict(r),
            "can_request_reversal": (
                (r["reversal_status"] or "") in {"", "rejected"}
                and int(r["user_id"]) != int(uid)
                and bool(active_admin_ids - {int(uid), int(r["user_id"])})
            ),
            "can_decide_reversal": (
                (r["reversal_status"] or "") in {"pending", "manual_required"}
                and int(r["requested_by"] or 0) != int(uid)
                and int(r["user_id"]) != int(uid)
                and int(r["requested_by"] or 0) in active_admin_ids
            ),
            "reversal_wait_reason": (
                "Ожидается второй ответственный."
                if (r["reversal_status"] or "") in {"pending", "manual_required"}
                and int(r["requested_by"] or 0) == int(uid) else
                "Получатель не может проверять собственное исправление."
                if (r["reversal_status"] or "") in {"pending", "manual_required"}
                and int(r["user_id"]) == int(uid) else ""
            ),
        } for r in manual_grants] if capabilities & {"bonus.grant.small", "bonus.reversal.request", "bonus.reversal.decide"} else [],
        "role_changes": [{
            **dict(r),
            "can_decide": (
                int(r["requested_by"]) != int(uid)
                and int(r["user_id"]) != int(uid)
            ),
            "wait_reason": (
                "Ожидается другой ответственный."
                if int(r["requested_by"]) == int(uid) else
                "Нельзя подтверждать изменение собственной роли."
                if int(r["user_id"]) == int(uid) else ""
            ),
        } for r in role_changes] if "access.view" in capabilities else [],
        "join_requests": [dict(r) for r in join_requests] if "admission.view" in capabilities else [],
        "task_templates": task_templates,
    })


async def api_admin_queue(request):
    """Пагинация и поиск двух растущих очередей скаута."""
    kind = str(request.rel_url.query.get("kind") or "applications")
    capability = "task.review.queue" if kind == "reviews" else "application.queue.view"
    admin_id, err = await _require_capability(request, capability)
    if err is not None:
        return err
    params = request.rel_url.query
    kind = str(params.get("kind") or "")
    query = str(params.get("q") or "").strip()[:100].casefold()
    try:
        limit = min(100, max(1, int(params.get("limit", "50"))))
        offset = max(0, int(params.get("cursor", "0") or 0))
    except (TypeError, ValueError):
        return _json({"error": "cursor"}, status=400)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.create_function(
            "CASEFOLD", 1, lambda value: str(value or "").casefold(),
            deterministic=True,
        )
        if kind == "applications":
            where = [
                "status='pending'",
                "(applied_at IS NOT NULL OR role='applicant')",
            ]
            values = []
            if query:
                where.append(
                    "(CASEFOLD(full_name) LIKE ? OR CASEFOLD(username) LIKE ? "
                    "OR CASEFOLD(city) LIKE ? OR CASEFOLD(about) LIKE ?)"
                )
                values.extend([f"%{query}%"] * 4)
            where_sql = " AND ".join(where)
            total = int((await (await db.execute(
                f"SELECT COUNT(*) FROM members WHERE {where_sql}", values,
            )).fetchone())[0])
            rows = await (await db.execute(
                "SELECT user_id,full_name,username,city,about,created_at "
                f"FROM members WHERE {where_sql} "
                "ORDER BY applied_at DESC,created_at DESC,user_id DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            )).fetchall()
            items = [dict(row) for row in rows]
        elif kind == "reviews":
            where = ["a.status='review'"]
            values = []
            if query:
                where.append(
                    "(CASEFOLD(t.title) LIKE ? OR CASEFOLD(t.city) LIKE ? "
                    "OR CASEFOLD(t.address) LIKE ? OR CASEFOLD(u.full_name) LIKE ?)"
                )
                values.extend([f"%{query}%"] * 4)
            where_sql = " AND ".join(where)
            total = int((await (await db.execute(
                "SELECT COUNT(*) FROM task_assignments a JOIN tasks t ON t.id=a.task_id "
                "LEFT JOIN members u ON u.user_id=a.user_id "
                f"WHERE {where_sql}", values,
            )).fetchone())[0])
            rows = await (await db.execute(
                "SELECT t.*,m.full_name AS assigned_name,a.id AS assignment_id,"
                "a.status AS assignment_status,a.user_id AS assignment_user_id,"
                "a.proof_note AS assignment_proof_note,"
                "a.review_note AS assignment_review_note,a.reward_snapshot,"
                "a.done_at AS assignment_done_at,u.full_name AS claimed_name "
                "FROM task_assignments a JOIN tasks t ON t.id=a.task_id "
                "LEFT JOIN members m ON m.user_id=t.assigned_to "
                "LEFT JOIN members u ON u.user_id=a.user_id "
                f"WHERE {where_sql} ORDER BY a.done_at DESC,a.id DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            )).fetchall()
            raw_items = [dict(row) for row in rows]
            await _attach_task_evidence(raw_items)
            items = [_admin_task_public(row, admin_id) for row in raw_items]
        else:
            return _json({"error": "kind"}, status=400)
    next_offset = offset + len(items)
    return _json({
        "ok": True, "items": items, "total": total,
        "next_cursor": str(next_offset) if next_offset < total else None,
    })


async def api_admin_task_announcement_retry(request):
    """Повторяет только окончательно упавшую доставку задания в private OPS."""
    admin_id, err = await _require_capability(request, "task.delivery.retry")
    if err is not None:
        return err
    body = await _body(request)
    task_id = _as_int(body.get("task_id"))
    operation_id = _operation_uuid(body.get("operation_id"))
    if task_id is None or not operation_id:
        return _json({"error": "request"}, status=400)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "task.delivery.retry"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        row = await (await db.execute(
            "SELECT id,status FROM task_outbox WHERE event_key=? "
            "AND event_type='group_task'",
            (f"task:{task_id}:announcement",),
        )).fetchone()
        if not row:
            await db.rollback()
            return _json({
                "error": "not_found",
                "message": "Для задания нет объявления в приватной OPS-группе.",
            }, status=404)
        if row["status"] == "sent":
            await db.rollback()
            return _json({"ok": True, "status": "sent", "idempotent": True})
        if row["status"] != "dead":
            await db.rollback()
            return _json({
                "ok": True, "status": row["status"], "idempotent": True,
                "message": "Доставка уже находится в очереди.",
            })
        await db.execute(
            "UPDATE task_outbox SET status='pending',attempts=0,available_at=?,"
            "last_error=NULL,telegram_message_id=NULL,telegram_thread_id=NULL "
            "WHERE id=? AND status='dead'",
            (now_iso(), row["id"]),
        )
        await _track_event_in_tx(
            db, "task_announcement_retried", "backend",
            user_id=admin_id, task_id=task_id,
            dedupe_key=f"task_announcement_retry:{operation_id}",
        )
        await db.commit()
    return _json({"ok": True, "status": "pending", "idempotent": False})


async def api_admin_join_request_retry(request):
    """Requeue a failed/manual Telegram admission decision idempotently."""
    admin_id, err = await _require_capability(request, "admission.retry")
    if err is not None:
        return err
    body = await _body(request)
    request_key = str(body.get("request_key") or "").strip().lower()
    decision = str(body.get("decision") or "").strip().lower()
    reason = " ".join(str(body.get("reason") or "").split())[:300]
    operation_id = _operation_uuid(body.get("operation_id"))
    if not re.fullmatch(r"[a-f0-9]{64}", request_key):
        return _json({"error": "request_key"}, status=400)
    if decision not in {"approve", "decline"}:
        return _json({"error": "decision"}, status=400)
    if len(reason) < 3:
        return _json({
            "error": "reason",
            "message": "Коротко укажи причину ручного повтора.",
        }, status=400)
    if not operation_id:
        return _json({"error": "operation_id"}, status=400)
    request_hash = _request_fingerprint({
        "request_key": request_key, "decision": decision, "reason": reason,
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "admission.retry"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        replay = bool(await (await db.execute(
            "SELECT 1 FROM operation_registry WHERE operation_id=?",
            (operation_id,),
        )).fetchone())
        if not await _claim_operation_in_tx(
            db, operation_id, "join_request_retry", request_hash, admin_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        row = await (await db.execute(
            "SELECT status,source,decision FROM telegram_join_requests "
            "WHERE request_key=?",
            (request_key,),
        )).fetchone()
        if not row:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        terminal = "approved" if decision == "approve" else "declined"
        if row["status"] in {terminal, "joined"}:
            await db.commit()
            return _json({
                "ok": True, "status": row["status"], "idempotent": True,
            })
        if row["status"] not in {
            "manual_required", "approve_queued", "decline_queued",
        }:
            await db.rollback()
            return _json({
                "error": "not_retryable", "status": row["status"],
            }, status=409)
        queued = await _queue_join_request_decision_in_tx(
            db, request_key, decision,
        )
        if not queued:
            await db.rollback()
            return _json({
                "error": "unsafe_decision",
                "message": "Одобрение возможно только для проверенной ссылки бота.",
            }, status=409)
        await _track_event_in_tx(
            db, "group_join_request_retried", "backend",
            user_id=admin_id, outcome=decision,
            dedupe_key=f"group_join_request_retry:{operation_id}",
        )
        await db.execute(
            "UPDATE telegram_join_requests SET manual_retry_reason=?,"
            "manual_retry_by=?,manual_retry_at=? WHERE request_key=?",
            (reason, int(admin_id), now_iso(), request_key),
        )
        await db.commit()
    return _json({
        "ok": True,
        "status": "approve_queued" if decision == "approve" else "decline_queued",
        "idempotent": replay,
    })


async def api_admin_task_announcement_status(request):
    """Лёгкий опрос доставки объявлений без перезагрузки всей админской сводки."""
    _, err = await _require_capability(request, "task.delivery.view")
    if err is not None:
        return err
    raw_ids = str(request.rel_url.query.get("ids", "")).strip()
    tokens = [item.strip() for item in raw_ids.split(",") if item.strip()]
    if (
        not tokens or len(tokens) > 100
        or any(len(item) > 19 or not item.isdigit() for item in tokens)
    ):
        return _json({"error": "ids"}, status=400)
    task_ids = list(dict.fromkeys(int(item) for item in tokens))
    if any(item <= 0 for item in task_ids):
        return _json({"error": "ids"}, status=400)
    event_keys = [f"task:{task_id}:announcement" for task_id in task_ids]
    placeholders = ",".join("?" for _ in event_keys)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT event_key,status,attempts,last_error,sent_at,chat_id,"
            "telegram_message_id,telegram_thread_id FROM task_outbox "
            f"WHERE event_type='group_task' AND event_key IN ({placeholders})",
            event_keys,
        )).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        task_id = int(item["event_key"].split(":", 2)[1])
        announcement_url = ""
        if item["status"] == "sent":
            announcement_url = _telegram_message_url(
                item.get("chat_id"), item.get("telegram_message_id"),
                item.get("telegram_thread_id"), OPS_GROUP_USERNAME,
            )
        items.append({
            "task_id": task_id,
            "status": item["status"],
            "attempts": int(item.get("attempts") or 0),
            "error": item.get("last_error") or "",
            "sent_at": item.get("sent_at") or "",
            "message_id": item.get("telegram_message_id"),
            "thread_id": item.get("telegram_thread_id"),
            "url": announcement_url,
        })
    items.sort(key=lambda item: task_ids.index(item["task_id"]))
    return _json({"ok": True, "items": items})


async def api_admin_members(request):
    """Серверный поиск и пагинация команды без ограничения overview в 500 строк."""
    viewer_id, err = await _require_capability(request, "member.search")
    if err is not None:
        return err
    can_view_finances = await _has_capability(
        viewer_id, "member.financial_summary.view",
    )
    if (
        BIBITASKS_ENVIRONMENT == "test"
        and _require_admin is not _DEFAULT_REQUIRE_ADMIN
    ):
        can_view_finances = True
    params = request.rel_url.query
    query = str(params.get("q", "")).strip()[:100].lstrip("@#").casefold()
    tag = str(params.get("tag", "")).strip()[:30].lstrip("#").casefold()
    city = _city_display(params.get("city"))
    try:
        limit = min(100, max(1, int(params.get("limit", "50"))))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(params.get("cursor", "0") or 0))
    except (TypeError, ValueError):
        return _json({
            "error": "cursor",
            "message": "Курсор списка пользователей устарел. Обнови поиск.",
        }, status=400)
    requested_sort = str(params.get("sort", "done"))
    if requested_sort == "bonus" and not can_view_finances:
        requested_sort = "done"
    sort_sql = {
        "done": "done_count DESC, chat_xp DESC, user_id",
        "name": "CASEFOLD(full_name), user_id",
        "city": "CASEFOLD(city), CASEFOLD(full_name), user_id",
        "bonus": "bonus DESC, user_id",
    }.get(requested_sort, "done_count DESC, chat_xp DESC, user_id")
    where = ["status='approved'"]
    values = []
    if query:
        needle = f"%{query}%"
        where.append(
            "(CASEFOLD(full_name) LIKE ? OR CASEFOLD(username) LIKE ? "
            "OR CASEFOLD(city) LIKE ? OR CASEFOLD(tags) LIKE ? "
            "OR CAST(user_id AS TEXT) LIKE ? OR CASEFOLD(about) LIKE ?)"
        )
        values.extend([needle] * 6)
    if tag:
        where.append("HAS_EXACT_TAG(tags, ?)=1")
        values.append(tag)
    if city:
        where.append("CITY_KEY(city)=?")
        values.append(_city_key(city))
    where_sql = " AND ".join(where)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.create_function(
            "CASEFOLD", 1, lambda value: str(value or "").casefold(), deterministic=True)
        await db.create_function(
            "HAS_EXACT_TAG", 2, _has_exact_tag, deterministic=True)
        await db.create_function(
            "CITY_KEY", 1, _city_key, deterministic=True)
        total = int((await (await db.execute(
            f"SELECT COUNT(*) FROM members WHERE {where_sql}", values,
        )).fetchone())[0])
        rows = await (await db.execute(
            "SELECT user_id, full_name, username, city, about, tags, role, bonus, "
            f"done_count, chat_xp, created_at FROM members WHERE {where_sql} "
            f"ORDER BY {sort_sql} LIMIT ? OFFSET ?",
            [*values, limit, offset],
        )).fetchall()
    items = [{
        "user_id": row["user_id"], "name": row["full_name"] or "",
        "role": row["role"],
        "bonus": row["bonus"] if can_view_finances else None,
        "done_count": row["done_count"], "chat_xp": row["chat_xp"] or 0,
        "city": row["city"] or "", "username": row["username"] or "",
        "about": row["about"] or "", "tags": _tags_list(row["tags"]),
        "created_at": row["created_at"],
        "trust_name": trust_for(trust_score(row["done_count"], row["chat_xp"]))[1],
        "trust_emoji": trust_for(trust_score(row["done_count"], row["chat_xp"]))[2],
    } for row in rows]
    next_offset = offset + len(items)
    return _json({
        "ok": True,
        "items": items,
        "team": items,
        "total": total,
        "next_cursor": str(next_offset) if next_offset < total else None,
    })


async def api_admin_member_tags_catalog(request):
    """Unique tags across the entire approved team, not the current page."""
    _, err = await _require_capability(request, "member.tags.view")
    if err is not None:
        return err
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        rows = await (await db.execute(
            "SELECT tags FROM members WHERE status='approved' AND tags IS NOT NULL"
        )).fetchall()
    catalog = {}
    for row in rows:
        for tag in _tags_list(row[0]):
            key = tag.casefold()
            item = catalog.setdefault(key, {"tag": tag, "count": 0})
            item["count"] += 1
    items = sorted(
        catalog.values(), key=lambda item: (-item["count"], item["tag"].casefold()),
    )
    return _json({"ok": True, "items": items[:500], "total": len(items)})


async def api_admin_member_city_decide(request):
    """Approve or reject a participant's pending city-gate correction."""
    admin_id, err = await _require_capability(request, "member.city.review")
    if err is not None:
        return err
    body = await _body(request)
    user_id = _as_int(body.get("user_id"))
    decision = str(body.get("decision") or "").strip().lower()
    requested_at = str(body.get("requested_at") or "")
    if user_id is None or decision not in {"approve", "reject"} or not requested_at:
        return _json({"error": "decision"}, status=400)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "member.city.review"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        member = await (await db.execute(
            "SELECT full_name,city,city_change_requested,city_change_requested_at FROM members "
            "WHERE user_id=? AND status='approved'", (user_id,),
        )).fetchone()
        if not member:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        requested_city = _city_display(member["city_change_requested"])
        if not requested_city:
            await db.rollback()
            return _json({
                "error": "already_decided", "message": "Запрос на смену города уже обработан.",
            }, status=409)
        if not hmac.compare_digest(
            str(member["city_change_requested_at"] or ""), requested_at,
        ):
            await db.rollback()
            return _json({
                "error": "request_changed",
                "message": "Запрос изменился после загрузки карточки. Обнови очередь и проверь новый город.",
            }, status=409)
        active = await (await db.execute(
            "SELECT 1 FROM task_assignments WHERE user_id=? "
            "AND status IN ('claimed','review') UNION ALL "
            "SELECT 1 FROM tasks WHERE assigned_to=? AND status='open' LIMIT 1",
            (user_id, user_id),
        )).fetchone()
        if decision == "approve" and active:
            await db.rollback()
            return _json({
                "error": "active_assignment",
                "message": "У участника появилось активное задание. Сначала завершите или освободите его.",
            }, status=409)
        previous_city = member["city"] or ""
        if decision == "approve":
            await db.execute(
                "UPDATE members SET city=?,city_change_requested=NULL,"
                "city_change_requested_at=NULL WHERE user_id=?",
                (requested_city, user_id),
            )
            notification = f"✅ Город в профиле изменён: {requested_city}. Теперь задания будут подбираться для него."
        else:
            await db.execute(
                "UPDATE members SET city_change_requested=NULL,"
                "city_change_requested_at=NULL WHERE user_id=?", (user_id,),
            )
            notification = (
                f"Запрос на смену города отклонён. В профиле остался город: {previous_city or 'не указан'}."
            )
        await _track_event_in_tx(
            db, "city_change_decided", "backend", user_id=user_id,
            outcome=decision,
            dedupe_key=f"city_change_decide:{user_id}:{requested_city}:{decision}:{admin_id}",
        )
        await _enqueue_outbox_in_tx(
            db, f"city_change:{user_id}:{requested_city}:{decision}", "direct",
            {"text": notification}, recipient_id=user_id,
        )
        await db.commit()
    return _json({
        "ok": True, "user_id": user_id, "decision": decision,
        "city": requested_city if decision == "approve" else previous_city,
    })


async def api_admin_decide(request):
    """Одобрить или отклонить заявку кандидата."""
    admin_id, err = await _require_capability(request, "application.review")
    if err is not None:
        return err
    body = await _body(request)
    uid = body.get("user_id")
    decision = body.get("decision")   # approve | reject
    note = (body.get("note") or "").strip()[:300]
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return _json({"error": "bad_user"}, status=400)
    referral_count = 0
    rewarded_total = 0
    referrer_id = None
    join_requests_queued = 0
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "application.review"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
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
                "UPDATE members SET status='approved', role='helper', application_note='', "
                "approved_at=?, approved_by=? "
                "WHERE user_id=? AND status IN ('pending','blocked')",
                (now_iso(), admin_id, uid),
            )
        elif decision == "reject":
            if len(note) < 3:
                await db.rollback()
                return _json({
                    "error": "note",
                    "message": "Коротко укажи причину отклонения.",
                }, status=400)
            cur = await db.execute(
                "UPDATE members SET status='blocked', application_note=? "
                "WHERE user_id=? AND status='pending'",
                (note, uid),
            )
        else:
            await db.rollback()
            return _json({"error": "bad_decision"}, status=400)
        if cur.rowcount != 1:
            await db.rollback()
            return _json({"error": "already_decided"}, status=409)
        if decision == "approve":
            join_requests_queued = await _queue_join_requests_for_user_in_tx(
                db, uid, "approve",
            )
            referrer_id, referral_count, rewarded_total = (
                await _confirm_referral_if_ready_in_tx(db, uid, by=admin_id)
            )
        else:
            join_requests_queued = await _queue_join_requests_for_user_in_tx(
                db, uid, "decline",
            )
        await _track_event_in_tx(
            db, "application_decided", "backend", user_id=uid,
            outcome=decision, properties={"decision": decision},
            dedupe_key=f"application_decision:{uid}:{decision}:{now_iso()}",
        )
        user_message = (
            "🎉 Заявка одобрена! Открой приложение — задания уже доступны."
            if decision == "approve"
            else f"Заявка пока не одобрена.\nПричина: {note}"
        )
        await _enqueue_outbox_in_tx(
            db, f"application:{uid}:decision:{decision}", "direct",
            {"text": user_message, "start": None}, recipient_id=uid,
        )
        if referrer_id and referral_count > 0:
            await _enqueue_outbox_in_tx(
                db, f"referral:{uid}:confirmed:referrer:{referrer_id}",
                "direct",
                {
                    "text": _referral_progress_message(
                        referral_count, rewarded_total,
                    ),
                    "start": None,
                },
                recipient_id=referrer_id,
            )
        await db.commit()
    return _json({"ok": True, "join_requests_queued": join_requests_queued})


def _task_template_uuid(value):
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _task_template_route_id(request):
    return _task_template_uuid(
        (getattr(request, "match_info", {}) or {}).get("template_id")
    )


def _normalize_task_template_content(body):
    title = " ".join(str(body.get("title") or "").split())[:120]
    task_type = str(body.get("task_type") or body.get("type") or "").strip()
    task_title = " ".join(str(body.get("task_title") or "").split())[:120]
    details = str(body.get("details") or "").strip()[:500]
    mode = str(body.get("mode") or "").strip().lower()
    evidence_policy = _evidence_policy(body.get("evidence_policy"))
    reward = _as_int(body.get("reward"))
    if len(title) < 3 or len(task_title) < 3:
        return None, "title"
    if task_type not in TASK_TYPES:
        return None, "type"
    if reward is None or not 1 <= reward <= 300:
        return None, "reward"
    if mode not in {"open", "personal", "all"}:
        return None, "mode"
    if evidence_policy is None:
        return None, "evidence_policy"
    if mode == "all":
        max_participants = _as_int(body.get("max_participants"))
        budget_cap = _as_int(body.get("budget_cap"))
        if max_participants is None or not 1 <= max_participants <= 500:
            return None, "max_participants"
        if (
            budget_cap is None or budget_cap < reward or budget_cap > 150000
            or max_participants * reward > budget_cap
        ):
            return None, "budget_cap"
    else:
        max_participants = 1
        budget_cap = reward
    return {
        "title": title, "task_type": task_type, "task_title": task_title,
        "details": details, "reward": reward, "mode": mode,
        "evidence_policy": evidence_policy,
        "max_participants": max_participants, "budget_cap": budget_cap,
        "photo_media_id": None, "photo_sha256": None,
    }, None


def _task_template_version_public(row):
    return {
        "id": row["version_id"],
        "version_number": int(row["version_number"]),
        "title": row["title"], "type": row["task_type"],
        "task_title": row["task_title"], "details": row["details"] or "",
        "reward": int(row["reward"]), "mode": row["mode"],
        "evidence_policy": _public_evidence_policy(row["evidence_policy"]),
        "max_participants": int(row["max_participants"]),
        "budget_cap": int(row["budget_cap"]),
        "photo_url": _signed_media_url(row["photo_media_id"]),
        "has_photo": bool(row["photo_media_id"]),
        "content_hash": row["content_hash"],
        "created_by": row["version_created_by"],
        "created_at": row["version_created_at"],
    }


def _task_template_public(row):
    version = _task_template_version_public(row)
    return {
        "id": row["template_id"], "key": row["key"],
        "origin": row["origin"], "status": row["status"],
        "generation": int(row["generation"]),
        "current_version_id": row["current_version_id"],
        "created_by": row["template_created_by"],
        "created_at": row["template_created_at"],
        "updated_by": row["updated_by"], "updated_at": row["updated_at"],
        "archived_by": row["archived_by"], "archived_at": row["archived_at"],
        "version_id": version["id"], "version_number": version["version_number"],
        "title": version["title"], "type": version["type"],
        "task_title": version["task_title"], "details": version["details"],
        "reward": version["reward"], "mode": version["mode"],
        "evidence_policy": version["evidence_policy"],
        "max_participants": version["max_participants"],
        "budget_cap": version["budget_cap"], "photo_url": version["photo_url"],
        "has_photo": version["has_photo"], "content_hash": version["content_hash"],
        "current_version": version,
    }


def _task_template_audit_snapshot(row):
    content = {field: row[field] for field in TASK_TEMPLATE_CONTENT_FIELDS}
    return {
        "id": row["template_id"], "key": row["key"], "origin": row["origin"],
        "status": row["status"], "generation": int(row["generation"]),
        "current_version_id": row["current_version_id"],
        "version": {
            **content, "id": row["version_id"],
            "version_number": int(row["version_number"]),
            "content_hash": row["content_hash"],
        },
    }


async def _task_template_row_in_tx(db, template_id):
    return await (await db.execute(
        "SELECT t.id AS template_id,t.key,t.origin,t.status,t.generation,"
        "t.current_version_id,t.created_by AS template_created_by,"
        "t.created_at AS template_created_at,t.updated_by,t.updated_at,"
        "t.archived_by,t.archived_at,v.id AS version_id,v.version_number,"
        "v.title,v.task_type,v.task_title,v.details,v.reward,v.mode,"
        "v.evidence_policy,v.max_participants,v.budget_cap,v.photo_media_id,"
        "v.photo_sha256,v.content_hash,v.created_by AS version_created_by,"
        "v.created_at AS version_created_at FROM task_templates t "
        "JOIN task_template_versions v ON v.id=t.current_version_id "
        "WHERE t.id=?", (template_id,),
    )).fetchone()


async def _task_template_replay_in_tx(db, operation_id, request_hash, actor_id):
    event = await (await db.execute(
        "SELECT request_hash,actor_id,result_json FROM task_template_events "
        "WHERE operation_id=?", (operation_id,),
    )).fetchone()
    if not event:
        return None, None
    if event["request_hash"] != request_hash or int(event["actor_id"] or 0) != int(actor_id):
        return None, _json({"error": "operation_conflict"}, status=409)
    result = json.loads(event["result_json"])
    result["idempotent"] = True
    return result, None


async def _task_templates_active_public():
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT t.id AS template_id,t.key,t.origin,t.status,t.generation,"
            "t.current_version_id,t.created_by AS template_created_by,"
            "t.created_at AS template_created_at,t.updated_by,t.updated_at,"
            "t.archived_by,t.archived_at,v.id AS version_id,v.version_number,"
            "v.title,v.task_type,v.task_title,v.details,v.reward,v.mode,"
            "v.evidence_policy,v.max_participants,v.budget_cap,v.photo_media_id,"
            "v.photo_sha256,v.content_hash,v.created_by AS version_created_by,"
            "v.created_at AS version_created_at FROM task_templates t "
            "JOIN task_template_versions v ON v.id=t.current_version_id "
            "WHERE t.status='active' ORDER BY t.key"
        )).fetchall()
    return [_task_template_public(row) for row in rows]


async def api_admin_task_templates_list(request):
    _, err = await _require_capability(request, "task.template.manage")
    if err is not None:
        return err
    query = getattr(request, "query", {}) or {}
    status_filter = str(query.get("status") or "active").strip().lower()
    if status_filter not in {"active", "archived", "all"}:
        return _json({"error": "status"}, status=400)
    limit = min(100, max(1, _as_int(query.get("limit"), 50)))
    after_id = _task_template_uuid(query.get("after_id")) if query.get("after_id") else None
    if query.get("after_id") and not after_id:
        return _json({"error": "cursor"}, status=400)
    clauses, params = [], []
    if status_filter != "all":
        clauses.append("t.status=?")
        params.append(status_filter)
    if after_id:
        clauses.append("t.id>?")
        params.append(after_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT t.id AS template_id,t.key,t.origin,t.status,t.generation,"
            "t.current_version_id,t.created_by AS template_created_by,"
            "t.created_at AS template_created_at,t.updated_by,t.updated_at,"
            "t.archived_by,t.archived_at,v.id AS version_id,v.version_number,"
            "v.title,v.task_type,v.task_title,v.details,v.reward,v.mode,"
            "v.evidence_policy,v.max_participants,v.budget_cap,v.photo_media_id,"
            "v.photo_sha256,v.content_hash,v.created_by AS version_created_by,"
            "v.created_at AS version_created_at FROM task_templates t "
            "JOIN task_template_versions v ON v.id=t.current_version_id" + where
            + " ORDER BY t.id LIMIT ?", (*params, limit + 1),
        )).fetchall()
    has_more = len(rows) > limit
    items = [_task_template_public(row) for row in rows[:limit]]
    return _json({
        "ok": True, "items": items,
        "next_cursor": items[-1]["id"] if has_more and items else None,
    })


async def api_admin_task_template_get(request):
    _, err = await _require_capability(request, "task.template.manage")
    if err is not None:
        return err
    template_id = _task_template_route_id(request)
    if not template_id:
        return _json({"error": "template_id"}, status=400)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        row = await _task_template_row_in_tx(db, template_id)
        if not row:
            return _json({"error": "not_found"}, status=404)
        versions = await (await db.execute(
            "SELECT id AS version_id,version_number,title,task_type,task_title,"
            "details,reward,mode,evidence_policy,max_participants,budget_cap,"
            "photo_media_id,photo_sha256,content_hash,created_by AS version_created_by,"
            "created_at AS version_created_at FROM task_template_versions "
            "WHERE template_id=? ORDER BY version_number DESC", (template_id,),
        )).fetchall()
    return _json({
        "ok": True, "template": _task_template_public(row),
        "versions": [_task_template_version_public(item) for item in versions],
    })


async def api_admin_task_template_create(request):
    admin_id, err = await _require_capability(request, "task.template.manage")
    if err is not None:
        return err
    body = await _body(request)
    operation_id = _operation_uuid(body.get("operation_id"))
    requested_key = str(body.get("key") or "").strip().lower()
    key = (
        requested_key
        if requested_key else
        (f"custom_{uuid.UUID(operation_id).hex}" if operation_id else "")
    )
    content, validation_error = _normalize_task_template_content(body)
    photo_data = body.get("photo_data") or ""
    copied_from_id = (
        _task_template_uuid(body.get("copied_from_id"))
        if body.get("copied_from_id") else None
    )
    copied_from_version_id = (
        _task_template_uuid(body.get("copied_from_version_id"))
        if body.get("copied_from_version_id") else None
    )
    photo_action = str(body.get("photo_action") or (
        "replace" if photo_data else "keep" if copied_from_id else "remove"
    )).strip().lower()
    if (
        not operation_id or not TASK_TEMPLATE_KEY_RE.fullmatch(key) or validation_error
        or photo_action not in {"keep", "replace", "remove"}
        or (photo_action == "keep" and not copied_from_id)
        or (photo_action == "keep" and not copied_from_version_id)
        or (copied_from_version_id and not copied_from_id)
        or (photo_action == "replace" and not photo_data)
        or (photo_action != "replace" and photo_data)
    ):
        return _json({"error": validation_error or "template_identity"}, status=400)
    copied_photo = None
    if copied_from_id and photo_action == "keep":
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            copied_photo = await (await db.execute(
                "SELECT v.photo_media_id,v.photo_sha256,m.state,m.purpose "
                "FROM task_template_versions v LEFT JOIN media_objects m "
                "ON m.id=v.photo_media_id WHERE v.template_id=? AND v.id=?",
                (copied_from_id, copied_from_version_id),
            )).fetchone()
        if not copied_photo:
            return _json({"error": "copy_source_not_found"}, status=404)
        content["photo_media_id"] = copied_photo["photo_media_id"]
        content["photo_sha256"] = copied_photo["photo_sha256"]
    request_hash = _request_fingerprint({
        "command": "create", "actor_id": int(admin_id),
        "requested_key": requested_key or None, "key": key,
        "content": {**content, "photo_media_id": None, "photo_sha256": None},
        "copied_from_id": copied_from_id,
        "copied_from_version_id": copied_from_version_id,
        "photo_action": photo_action,
        "photo_input_sha256": hashlib.sha256(str(photo_data).encode("utf-8")).hexdigest()
        if photo_data else "",
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "task.template.manage"):
            await db.rollback()
            return _json({"error": "capability_revoked"}, status=403)
        replay, conflict = await _task_template_replay_in_tx(
            db, operation_id, request_hash, admin_id,
        )
        await db.rollback()
        if conflict:
            return conflict
        if replay:
            return _json(replay)
    image = None
    if photo_data:
        try:
            image = await _save_image(
                photo_data, purpose="task_template_brief",
                upload_operation_id=f"template:{operation_id}:brief",
                request_hash=request_hash, admin_id=admin_id,
                required_capability="task.template.manage",
            )
        except PermissionError:
            return _json({"error": "capability_revoked"}, status=403)
        except ValueError as exc:
            return _json({"error": "photo", "message": str(exc)}, status=400)
        content["photo_media_id"] = image["media_id"]
        content["photo_sha256"] = image["sha256"]
    if content["evidence_policy"] == "before_after" and not content["photo_media_id"]:
        await _remove_saved_images([image] if image else [])
        return _json({"error": "brief_required"}, status=400)
    template_id, version_id = str(uuid.uuid4()), str(uuid.uuid4())

    async def reject_after_template_upload(response):
        if image:
            await _remove_saved_images([image])
        return response

    try:
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if not await _has_capability_in_tx(db, admin_id, "task.template.manage"):
                await db.rollback()
                return await reject_after_template_upload(
                    _json({"error": "capability_revoked"}, status=403)
                )
            replay, conflict = await _task_template_replay_in_tx(
                db, operation_id, request_hash, admin_id,
            )
            if conflict:
                await db.rollback()
                return await reject_after_template_upload(conflict)
            if replay:
                await db.rollback()
                return _json(replay)
            if not await _claim_operation_in_tx(
                db, operation_id, "task_template_create", request_hash, admin_id,
            ):
                await db.rollback()
                return await reject_after_template_upload(
                    _json({"error": "operation_conflict"}, status=409)
                )
            if await (await db.execute(
                "SELECT 1 FROM task_templates WHERE key=?", (key,),
            )).fetchone():
                await db.rollback()
                return await reject_after_template_upload(
                    _json({"error": "template_key_exists"}, status=409)
                )
            if copied_from_id and photo_action == "keep":
                live_copy = await (await db.execute(
                    "SELECT photo_media_id,photo_sha256 FROM task_template_versions "
                    "WHERE template_id=? AND id=?",
                    (copied_from_id, copied_from_version_id),
                )).fetchone()
                if (
                    not live_copy
                    or live_copy["photo_media_id"] != content["photo_media_id"]
                    or live_copy["photo_sha256"] != content["photo_sha256"]
                ):
                    await db.rollback()
                    return await reject_after_template_upload(
                        _json({"error": "copy_source_changed"}, status=409)
                    )
            if content["photo_media_id"]:
                claimed = await db.execute(
                    "UPDATE media_objects SET delete_after=NULL WHERE id=? "
                    "AND state='ready' AND purpose='task_template_brief' AND sha256=?",
                    (content["photo_media_id"], content["photo_sha256"]),
                )
                if claimed.rowcount != 1:
                    await db.rollback()
                    return await reject_after_template_upload(
                        _json({"error": "media_not_ready"}, status=409)
                    )
            stamp = now_iso()
            content_hash = _task_template_content_hash(content)
            await db.execute(
                "INSERT INTO task_templates "
                "(id,key,origin,status,generation,current_version_id,created_by,created_at,"
                "updated_by,updated_at) VALUES (?,?,'manual','active',1,?,?,?,?,?)",
                (template_id, key, version_id, admin_id, stamp, admin_id, stamp),
            )
            await db.execute(
                "INSERT INTO task_template_versions "
                "(id,template_id,version_number,title,task_type,task_title,details,reward,"
                "mode,evidence_policy,max_participants,budget_cap,photo_media_id,photo_sha256,"
                "content_hash,created_by,created_at) VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, template_id, *(content[field] for field in
                    TASK_TEMPLATE_CONTENT_FIELDS), content_hash, admin_id, stamp),
            )
            after_row = await _task_template_row_in_tx(db, template_id)
            after = _task_template_public(after_row)
            result = {
                "ok": True, "template_id": template_id, "generation": 1,
                "version_id": version_id, "version_number": 1,
                "status": "active", "idempotent": False,
                "copied_from_id": copied_from_id,
                "copied_from_version_id": copied_from_version_id,
            }
            await db.execute(
                "INSERT INTO task_template_events "
                "(template_id,template_version_id,event_type,generation,actor_id,operation_id,"
                "request_hash,note,before_json,after_json,result_json,created_at) "
                "VALUES (?,?,'created',1,?,?,?,'',?,?,?,?)",
                (template_id, version_id, admin_id, operation_id, request_hash,
                 _canonical_json({}), _canonical_json(
                     _task_template_audit_snapshot(after_row)
                 ),
                 _canonical_json(result), stamp),
            )
            await db.commit()
    except Exception:
        await _remove_saved_images([image] if image else [])
        raise
    return _json(result)


async def api_admin_task_template_version_create(request):
    admin_id, err = await _require_capability(request, "task.template.manage")
    if err is not None:
        return err
    template_id = _task_template_route_id(request)
    body = await _body(request)
    operation_id = _operation_uuid(body.get("operation_id"))
    expected_generation = _as_int(body.get("expected_generation"))
    photo_action = str(body.get("photo_action") or "keep").strip().lower()
    photo_data = body.get("photo_data") or ""
    content, validation_error = _normalize_task_template_content(body)
    if (
        not template_id or not operation_id or expected_generation is None
        or expected_generation < 1 or photo_action not in {"keep", "replace", "remove"}
        or validation_error or (photo_action == "replace" and not photo_data)
        or (photo_action != "replace" and photo_data)
    ):
        return _json({"error": validation_error or "template_version"}, status=400)
    request_hash = _request_fingerprint({
        "command": "version_create", "actor_id": int(admin_id),
        "template_id": template_id, "expected_generation": expected_generation,
        "content": content, "photo_action": photo_action,
        "photo_input_sha256": hashlib.sha256(str(photo_data).encode("utf-8")).hexdigest()
        if photo_data else "",
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "task.template.manage"):
            await db.rollback()
            return _json({"error": "capability_revoked"}, status=403)
        replay, conflict = await _task_template_replay_in_tx(
            db, operation_id, request_hash, admin_id,
        )
        await db.rollback()
        if conflict:
            return conflict
        if replay:
            return _json(replay)
    image = None
    if photo_action == "replace":
        try:
            image = await _save_image(
                photo_data, purpose="task_template_brief",
                upload_operation_id=f"template:{operation_id}:brief",
                request_hash=request_hash, admin_id=admin_id,
                required_capability="task.template.manage",
            )
        except PermissionError:
            return _json({"error": "capability_revoked"}, status=403)
        except ValueError as exc:
            return _json({"error": "photo", "message": str(exc)}, status=400)
        content["photo_media_id"], content["photo_sha256"] = image["media_id"], image["sha256"]

    async def reject_after_template_upload(response):
        if image:
            await _remove_saved_images([image])
        return response

    try:
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if not await _has_capability_in_tx(db, admin_id, "task.template.manage"):
                await db.rollback()
                return await reject_after_template_upload(
                    _json({"error": "capability_revoked"}, status=403)
                )
            replay, conflict = await _task_template_replay_in_tx(
                db, operation_id, request_hash, admin_id,
            )
            if conflict:
                await db.rollback()
                return await reject_after_template_upload(conflict)
            if replay:
                await db.rollback()
                return _json(replay)
            current = await _task_template_row_in_tx(db, template_id)
            if not current:
                await db.rollback()
                return await reject_after_template_upload(
                    _json({"error": "not_found"}, status=404)
                )
            if int(current["generation"]) != expected_generation:
                await db.rollback()
                return await reject_after_template_upload(_json({
                    "error": "template_generation_conflict",
                    "generation": int(current["generation"]),
                }, status=409))
            if photo_action == "keep":
                content["photo_media_id"] = current["photo_media_id"]
                content["photo_sha256"] = current["photo_sha256"]
            if content["evidence_policy"] == "before_after" and not content["photo_media_id"]:
                await db.rollback()
                return await reject_after_template_upload(
                    _json({"error": "brief_required"}, status=400)
                )
            if content["photo_media_id"]:
                media = await (await db.execute(
                    "SELECT state,purpose,sha256 FROM media_objects WHERE id=?",
                    (content["photo_media_id"],),
                )).fetchone()
                if (
                    not media or media["state"] != "ready"
                    or media["purpose"] != "task_template_brief"
                    or media["sha256"] != content["photo_sha256"]
                ):
                    await db.rollback()
                    return await reject_after_template_upload(
                        _json({"error": "media_not_ready"}, status=409)
                    )
                await db.execute(
                    "UPDATE media_objects SET delete_after=NULL WHERE id=?",
                    (content["photo_media_id"],),
                )
            content_hash = _task_template_content_hash(content)
            if content_hash == current["content_hash"]:
                await db.rollback()
                return await reject_after_template_upload(
                    _json({"error": "template_unchanged"}, status=409)
                )
            if not await _claim_operation_in_tx(
                db, operation_id, "task_template_version_create", request_hash, admin_id,
            ):
                await db.rollback()
                return await reject_after_template_upload(
                    _json({"error": "operation_conflict"}, status=409)
                )
            version_id = str(uuid.uuid4())
            version_number = int(current["version_number"]) + 1
            generation = expected_generation + 1
            stamp = now_iso()
            await db.execute(
                "INSERT INTO task_template_versions "
                "(id,template_id,version_number,title,task_type,task_title,details,reward,"
                "mode,evidence_policy,max_participants,budget_cap,photo_media_id,photo_sha256,"
                "content_hash,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, template_id, version_number,
                 *(content[field] for field in TASK_TEMPLATE_CONTENT_FIELDS),
                 content_hash, admin_id, stamp),
            )
            updated = await db.execute(
                "UPDATE task_templates SET current_version_id=?,generation=?,updated_by=?,"
                "updated_at=? WHERE id=? AND generation=?",
                (version_id, generation, admin_id, stamp, template_id, expected_generation),
            )
            if updated.rowcount != 1:
                await db.rollback()
                return await reject_after_template_upload(
                    _json({"error": "template_generation_conflict"}, status=409)
                )
            after_row = await _task_template_row_in_tx(db, template_id)
            after = _task_template_public(after_row)
            result = {
                "ok": True, "template_id": template_id, "generation": generation,
                "version_id": version_id, "version_number": version_number,
                "status": after["status"], "idempotent": False,
            }
            await db.execute(
                "INSERT INTO task_template_events "
                "(template_id,template_version_id,event_type,generation,actor_id,operation_id,"
                "request_hash,note,before_json,after_json,result_json,created_at) "
                "VALUES (?,?,'version_created',?,?,?,?,?,?, ?,?,?)",
                (template_id, version_id, generation, admin_id, operation_id, request_hash,
                 " ".join(str(body.get("note") or "").split())[:300],
                 _canonical_json(_task_template_audit_snapshot(current)),
                 _canonical_json(_task_template_audit_snapshot(after_row)),
                 _canonical_json(result), stamp),
            )
            await db.commit()
    except Exception:
        await _remove_saved_images([image] if image else [])
        raise
    return _json(result)


async def api_admin_task_template_status(request):
    admin_id, err = await _require_capability(request, "task.template.manage")
    if err is not None:
        return err
    template_id = _task_template_route_id(request)
    body = await _body(request)
    operation_id = _operation_uuid(body.get("operation_id"))
    expected_generation = _as_int(body.get("expected_generation"))
    desired = str(body.get("status") or "").strip().lower()
    note = " ".join(str(body.get("note") or "").split())[:300]
    if (
        not template_id or not operation_id or expected_generation is None
        or expected_generation < 1 or desired not in {"active", "archived"}
        or len(note) < 3
    ):
        return _json({"error": "template_status"}, status=400)
    request_hash = _request_fingerprint({
        "command": "status_change", "actor_id": int(admin_id),
        "template_id": template_id, "expected_generation": expected_generation,
        "status": desired, "note": note,
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "task.template.manage"):
            await db.rollback()
            return _json({"error": "capability_revoked"}, status=403)
        replay, conflict = await _task_template_replay_in_tx(
            db, operation_id, request_hash, admin_id,
        )
        if conflict:
            await db.rollback()
            return conflict
        if replay:
            await db.rollback()
            return _json(replay)
        current = await _task_template_row_in_tx(db, template_id)
        if not current:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if int(current["generation"]) != expected_generation:
            await db.rollback()
            return _json({"error": "template_generation_conflict",
                          "generation": int(current["generation"])}, status=409)
        if current["status"] == desired:
            await db.rollback()
            return _json({"error": "template_status_unchanged"}, status=409)
        if desired == "active" and current["photo_media_id"]:
            media = await (await db.execute(
                "SELECT state,purpose,sha256 FROM media_objects WHERE id=?",
                (current["photo_media_id"],),
            )).fetchone()
            if (
                not media or media["state"] != "ready"
                or media["purpose"] != "task_template_brief"
                or media["sha256"] != current["photo_sha256"]
            ):
                await db.rollback()
                return _json({"error": "media_not_ready"}, status=409)
        if not await _claim_operation_in_tx(
            db, operation_id, "task_template_status_change", request_hash, admin_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        generation, stamp = expected_generation + 1, now_iso()
        archived_by = admin_id if desired == "archived" else None
        archived_at = stamp if desired == "archived" else None
        updated = await db.execute(
            "UPDATE task_templates SET status=?,generation=?,updated_by=?,updated_at=?,"
            "archived_by=?,archived_at=? WHERE id=? AND generation=?",
            (desired, generation, admin_id, stamp, archived_by, archived_at,
             template_id, expected_generation),
        )
        if updated.rowcount != 1:
            await db.rollback()
            return _json({"error": "template_generation_conflict"}, status=409)
        after_row = await _task_template_row_in_tx(db, template_id)
        after = _task_template_public(after_row)
        result = {
            "ok": True, "template_id": template_id, "generation": generation,
            "version_id": current["version_id"],
            "version_number": int(current["version_number"]),
            "status": desired, "idempotent": False,
        }
        await db.execute(
            "INSERT INTO task_template_events "
            "(template_id,template_version_id,event_type,generation,actor_id,operation_id,"
            "request_hash,note,before_json,after_json,result_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (template_id, current["version_id"],
             "archived" if desired == "archived" else "activated",
             generation, admin_id, operation_id, request_hash, note,
             _canonical_json(_task_template_audit_snapshot(current)),
             _canonical_json(_task_template_audit_snapshot(after_row)),
             _canonical_json(result), stamp),
        )
        await db.commit()
    return _json(result)


async def api_admin_task_create(request):
    admin_id, err = await _require_capability(request, "task.create")
    if err is not None:
        return err
    body = await _body(request)
    operation_id = _operation_uuid(body.get("operation_id"))
    if not operation_id:
        return _json({
            "error": "operation_id",
            "message": "Для безопасного создания задания нужен operation_id в формате UUID.",
        }, status=400)
    template_id = _task_template_uuid(body.get("template_id")) if body.get("template_id") else None
    template_version_id = (
        _task_template_uuid(body.get("template_version_id"))
        if body.get("template_version_id") else None
    )
    if bool(template_id) != bool(template_version_id):
        return _json({"error": "template_identity"}, status=400)
    template_row = None
    template_photo_action = "replace" if body.get("photo_data") else "inherit"
    if template_id:
        template_photo_action = str(
            body.get("template_photo_action") or body.get("photo_action")
            or template_photo_action
        ).strip().lower()
        if template_photo_action not in {"inherit", "replace", "remove"}:
            return _json({"error": "photo_action"}, status=400)
        if template_photo_action == "replace" and not body.get("photo_data"):
            return _json({"error": "photo"}, status=400)
        if template_photo_action != "replace" and body.get("photo_data"):
            return _json({"error": "photo_action"}, status=400)
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            template_row = await (await db.execute(
                "SELECT t.status,t.current_version_id,t.generation,v.*,"
                "m.object_key AS photo_object_key,m.state AS photo_state,"
                "m.purpose AS photo_purpose,m.sha256 AS media_sha256 "
                "FROM task_templates t JOIN task_template_versions v "
                "ON v.template_id=t.id LEFT JOIN media_objects m "
                "ON m.id=v.photo_media_id WHERE t.id=? AND v.id=?",
                (template_id, template_version_id),
            )).fetchone()
        if not template_row:
            return _json({"error": "template_not_found"}, status=404)
        body = dict(body)
        body.update({
            "type": template_row["task_type"], "title": template_row["task_title"],
            "details": template_row["details"], "reward": template_row["reward"],
            "repeatable": template_row["mode"] == "all",
            "evidence_policy": template_row["evidence_policy"],
            "max_participants": template_row["max_participants"],
            "budget_cap": template_row["budget_cap"],
        })
    ttype = body.get("type")
    if ttype not in TASK_TYPES:
        return _json({"error": "type"}, status=400)
    title = (body.get("title") or "").strip()[:120]
    details = (body.get("details") or "").strip()[:500]
    address = (body.get("address") or "").strip()[:200]
    city = _city_display(body.get("city"))
    announce = body.get("announce") is True
    if len(title) < 3:
        return _json({"error": "title", "message": "Укажи понятный заголовок задания."}, status=400)
    if len(city) < 2:
        return _json({"error": "city", "message": "Укажи город задания."}, status=400)
    if len(address) < 3:
        return _json({"error": "address", "message": "Укажи адрес или ориентир."}, status=400)
    try:
        reward = max(0, int(body.get("reward") or 0))
    except (TypeError, ValueError):
        reward = 0
    if reward < 1 or reward > 300:
        return _json({
            "error": "reward",
            "message": "Награда за задание должна быть от 1 до 300 бибибонусов.",
        }, status=400)
    assigned_to = body.get("assigned_to")
    if assigned_to in ("", None):
        assigned_to = None
    else:
        try:
            assigned_to = int(assigned_to)
        except (TypeError, ValueError):
            return _json({"error": "assignee"}, status=400)
    if template_row and template_row["mode"] == "personal" and assigned_to is None:
        return _json({
            "error": "assignee",
            "message": "Персональный шаблон требует исполнителя.",
        }, status=400)
    repeatable = bool(body.get("repeatable"))
    if repeatable and assigned_to is not None:
        return _json({
            "error": "mode",
            "message": "Многоразовое задание должно быть доступно всем.",
        }, status=400)
    evidence_policy = _evidence_policy(body.get("evidence_policy"))
    if evidence_policy is None:
        return _json({
            "error": "evidence_policy",
            "message": "Неизвестная политика фотоотчёта.",
        }, status=400)
    if (
        evidence_policy == "before_after" and not body.get("photo_data")
        and not (
            template_row and template_photo_action == "inherit"
            and template_row["photo_media_id"]
        )
    ):
        return _json({
            "error": "brief_required",
            "message": "Для отчёта «до/после» прикрепи исходное фото точки.",
        }, status=400)
    if repeatable:
        max_participants = _as_int(body.get("max_participants"))
        budget_cap = _as_int(body.get("budget_cap"))
        if max_participants is None or not 1 <= max_participants <= 500:
            return _json({
                "error": "max_participants",
                "message": "Для многоразового задания укажи от 1 до 500 участников.",
            }, status=400)
        if budget_cap is None or budget_cap < reward or budget_cap > 150000:
            return _json({
                "error": "budget_cap",
                "message": "Укажи общий бюджет многоразового задания от размера награды до 150 000.",
            }, status=400)
        if max_participants * reward > budget_cap:
            return _json({
                "error": "budget_cap",
                "message": "Общего бюджета не хватает на награду всем указанным участникам.",
            }, status=400)
    else:
        max_participants = 1
        budget_cap = reward
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
    photo_data = body.get("photo_data") or ""
    request_hash = _request_fingerprint({
        "type": ttype, "title": title, "details": details,
        "address": address, "city": city, "reward": reward,
        "assigned_to": assigned_to, "repeatable": repeatable,
        "evidence_policy": _public_evidence_policy(evidence_policy),
        "max_participants": max_participants, "budget_cap": budget_cap,
        "slot_start": slot_start, "slot_end": slot_end,
        "lat": lat, "lng": lng, "announce": announce,
        "template_id": template_id, "template_version_id": template_version_id,
        "template_photo_action": template_photo_action if template_id else None,
        "photo_sha256": (
            hashlib.sha256(str(photo_data).encode("utf-8")).hexdigest()
            if photo_data else ""
        ),
    })
    brief_image = None
    uploaded_brief_image = None
    photo_file = None
    announcement_status = "not_requested"
    announce_error = ""
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        existing = await (await db.execute(
            "SELECT id,created_by,repeatable,photo_file,photo_media_id,request_hash,"
            "template_id,template_version_id "
            "FROM tasks WHERE operation_id=?",
            (operation_id,),
        )).fetchone()
        if existing:
            if (
                int(existing["created_by"]) != int(admin_id)
                or existing["request_hash"] != request_hash
            ):
                await db.rollback()
                return _json({
                    "error": "operation_conflict",
                    "message": "Этот operation_id уже использован для другого задания.",
                }, status=409)
            if not await _claim_operation_in_tx(
                db, operation_id, "task_create", request_hash, admin_id,
            ):
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            delivery = await (await db.execute(
                "SELECT status FROM task_outbox WHERE event_key=?",
                (f"task:{int(existing['id'])}:announcement",),
            )).fetchone()
            await db.commit()
            return _json({
                "ok": True, "task_id": int(existing["id"]),
                "repeatable": bool(existing["repeatable"]),
                "has_photo": bool(existing["photo_media_id"] or existing["photo_file"]),
                "operation_id": operation_id, "idempotent": True,
                "announcement_status": delivery[0] if delivery else "not_requested",
                "template_id": existing["template_id"],
                "template_version_id": existing["template_version_id"],
            })
        await db.rollback()
    # Mutable member state is deliberately checked only after committed-command
    # replay. A lost response must remain replayable even if the assignee is
    # later blocked, changes role, or moves to another city.
    if assigned_to is not None:
        assignee = await get_member(assigned_to)
        if not assignee or assignee["status"] != "approved" or assignee["role"] not in (
            "helper", "employee", "admin"
        ):
            return _json({
                "error": "assignee",
                "message": "Можно назначить только одобренного участника.",
            }, status=400)
        if _city_key(assignee.get("city")) != _city_key(city):
            return _json({
                "error": "assignee_city",
                "message": "Город участника не совпадает с городом задания.",
            }, status=400)
    if template_row and (
        template_row["status"] != "active"
        or template_row["current_version_id"] != template_version_id
    ):
        return _json({
            "error": "template_archived" if template_row["status"] != "active"
            else "template_version_stale",
            "current_version_id": template_row["current_version_id"],
            "generation": int(template_row["generation"]),
        }, status=409)
    # Time-dependent validation applies only to a new command. A committed
    # operation must remain replayable after its deadline when the first HTTP
    # response was lost.
    if slot_end and datetime.fromisoformat(slot_end) <= datetime.now(timezone.utc):
        return _json({
            "error": "slot_expired",
            "message": "Окончание задания должно быть в будущем.",
        }, status=400)
    if photo_data:
        try:
            brief_image = await _save_image(
                photo_data, purpose="task_brief",
                upload_operation_id=f"task:{operation_id}:brief",
                request_hash=request_hash,
                admin_id=admin_id,
                required_capability="task.create",
            )
            uploaded_brief_image = brief_image
        except PermissionError:
            return _json({"error": "admin_revoked"}, status=403)
        except ValueError as exc:
            status = 409 if "upload operation" in str(exc) else 400
            return _json({"error": "photo", "message": str(exc)}, status=status)
        photo_file = brief_image["photo_file"]
    elif template_row and template_photo_action == "inherit" and template_row["photo_media_id"]:
        if (
            template_row["photo_state"] != "ready"
            or template_row["photo_purpose"] != "task_template_brief"
            or template_row["media_sha256"] != template_row["photo_sha256"]
        ):
            return _json({"error": "media_not_ready"}, status=409)
        brief_image = {
            "media_id": template_row["photo_media_id"],
            "photo_file": template_row["photo_object_key"],
            "sha256": template_row["photo_sha256"],
        }
        photo_file = brief_image["photo_file"]
    if evidence_policy == "before_after" and not brief_image:
        return _json({"error": "brief_required"}, status=400)

    async def reject_after_task_upload(response):
        if uploaded_brief_image:
            await _remove_saved_images([uploaded_brief_image])
        return response

    try:
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if not await _has_capability_in_tx(db, admin_id, "task.create"):
                await db.rollback()
                return await reject_after_task_upload(
                    _json({"error": "admin_revoked"}, status=403)
                )
            existing = await (await db.execute(
                "SELECT id, created_by, repeatable, photo_file, request_hash FROM tasks "
                "WHERE operation_id=?",
                (operation_id,),
            )).fetchone()
            if existing:
                if (
                    int(existing["created_by"]) != int(admin_id)
                    or existing["request_hash"] != request_hash
                ):
                    await db.rollback()
                    return await reject_after_task_upload(_json({
                        "error": "operation_conflict",
                        "message": "Этот operation_id уже использован для другого задания.",
                    }, status=409))
                if not await _claim_operation_in_tx(
                    db, operation_id, "task_create", request_hash, admin_id,
                ):
                    await db.rollback()
                    return await reject_after_task_upload(
                        _json({"error": "operation_conflict"}, status=409)
                    )
                delivery = await (await db.execute(
                    "SELECT status FROM task_outbox WHERE event_key=?",
                    (f"task:{int(existing['id'])}:announcement",),
                )).fetchone()
                await db.commit()
                return _json({
                    "ok": True,
                    "task_id": int(existing["id"]),
                    "repeatable": bool(existing["repeatable"]),
                    "has_photo": bool(existing["photo_file"]),
                    "operation_id": operation_id,
                    "idempotent": True,
                    "announcement_status": delivery[0] if delivery else "not_requested",
                    "template_id": template_id,
                    "template_version_id": template_version_id,
                })
            if template_id:
                live_template = await (await db.execute(
                    "SELECT status,current_version_id FROM task_templates WHERE id=?",
                    (template_id,),
                )).fetchone()
                if (
                    not live_template or live_template["status"] != "active"
                    or live_template["current_version_id"] != template_version_id
                ):
                    await db.rollback()
                    return await reject_after_task_upload(
                        _json({"error": "template_state_changed"}, status=409)
                    )
            if assigned_to is not None:
                current_assignee = await (await db.execute(
                    "SELECT status,role,city FROM members WHERE user_id=?",
                    (assigned_to,),
                )).fetchone()
                if (
                    not current_assignee
                    or current_assignee["status"] != "approved"
                    or current_assignee["role"] not in ("helper", "employee", "admin")
                ):
                    await db.rollback()
                    return await reject_after_task_upload(_json({
                        "error": "assignee_changed",
                        "message": "Участник больше недоступен для назначения. Обнови список.",
                    }, status=409))
                if _city_key(current_assignee["city"]) != _city_key(city):
                    await db.rollback()
                    return await reject_after_task_upload(_json({
                        "error": "assignee_city",
                        "message": "Город участника изменился. Обнови список исполнителей.",
                    }, status=409))
                if (
                    current_assignee["role"] == "admin"
                    and not await _admin_task_has_independent_review_path_in_tx(
                        db, admin_id, assigned_to,
                    )
                ):
                    await db.rollback()
                    return await reject_after_task_upload(_json({
                        "error": "admin_task_independence",
                        "message": (
                            "Нельзя назначить задание ответственному: нужны два других "
                            "действующих администратора для независимой проверки и возможного спора."
                        ),
                    }, status=409))
            if not await _claim_operation_in_tx(
                db, operation_id, "task_create", request_hash, admin_id,
            ):
                await db.rollback()
                return await reject_after_task_upload(
                    _json({"error": "operation_conflict"}, status=409)
                )
            if brief_image:
                if uploaded_brief_image:
                    media_claim = await db.execute(
                        "UPDATE media_objects SET delete_after=NULL "
                        "WHERE id=? AND state='ready' AND purpose='task_brief' "
                        "AND sha256=?",
                        (brief_image["media_id"], brief_image["sha256"]),
                    )
                else:
                    media_claim = await db.execute(
                        "UPDATE media_objects SET delete_after=NULL "
                        "WHERE id=? AND state='ready' "
                        "AND purpose='task_template_brief' AND sha256=?",
                        (brief_image["media_id"], brief_image["sha256"]),
                    )
                if media_claim.rowcount != 1:
                    await db.rollback()
                    return await reject_after_task_upload(_json({
                        "error": "media_not_ready",
                        "message": "Фотография ещё не готова или уже удаляется. Добавь её заново.",
                    }, status=409))
            cur = await db.execute(
                "INSERT INTO tasks (type, title, details, lat, lng, address, city, reward, "
                "status, created_by, created_at, assigned_to, slot_start, slot_end, "
                "repeatable, photo_file,photo_media_id, operation_id, request_hash, evidence_policy, "
                "max_participants, budget_cap,template_id,template_version_id) "
                "VALUES (?,?,?,?,?,?,?,?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ttype, title, details, lat, lng, address, city, reward, admin_id,
                    now_iso(), assigned_to, slot_start, slot_end, int(repeatable), photo_file,
                    brief_image["media_id"] if brief_image else None,
                    operation_id, request_hash, evidence_policy,
                    max_participants, budget_cap, template_id, template_version_id,
                ))
            tid = cur.lastrowid
            if brief_image:
                await db.execute(
                    "INSERT INTO task_evidence "
                    "(assignment_id, task_id, user_id, kind, photo_file,media_id,sha256, created_at) "
                    "VALUES (NULL, ?, ?, 'brief', ?, ?, ?, ?)",
                    (
                        tid, admin_id, brief_image["photo_file"], brief_image["media_id"],
                        brief_image["sha256"], now_iso(),
                    ),
                )
            await _track_event_in_tx(
                db, "task_created", "backend", user_id=admin_id, task_id=tid,
                properties={
                    "task_type": ttype,
                    "evidence_policy": _public_evidence_policy(evidence_policy),
                    "repeatable": bool(repeatable),
                },
                dedupe_key=f"task_created:{operation_id}",
            )
            if assigned_to:
                parts = ["📌 Тебе назначено новое задание", title]
                if details:
                    parts.append(details)
                if photo_file:
                    parts.append("📷 К заданию прикреплена фотография — открой карточку в приложении.")
                parts.append(f"📍 {city} · {address}")
                if slot_start:
                    parts.append(f"🕒 {slot_text(slot_start, slot_end)}")
                parts.append(f"Награда: {reward} бибибонусов")
                await _enqueue_outbox_in_tx(
                    db, f"task:{tid}:assigned:user:{assigned_to}", "direct",
                    {"text": "\n".join(parts), "start": f"task_{tid}"},
                    recipient_id=assigned_to,
                )
            elif announce:
                target = OPS_GROUP_ID or (
                    f"@{OPS_GROUP_USERNAME}" if OPS_GROUP_USERNAME else None
                )
                if target:
                    task_kind = "Для каждого участника" if repeatable else "Забирает первый"
                    lines = [
                        "📌 <b>Новое задание</b>", f"<b>{_html(title)}</b>",
                        f"{_html(TASK_TYPES[ttype]['title'])} · {_html(task_kind)}",
                    ]
                    if details:
                        lines.append(_html(details))
                    lines.append(f"📍 {_html(city)} · {_html(address)}")
                    if slot_start:
                        lines.append(f"🕒 {_html(slot_text(slot_start, slot_end))}")
                    lines.append(f"⚡ Награда: <b>{reward} бибибонусов</b>")
                    await _enqueue_outbox_in_tx(
                        db, f"task:{tid}:announcement", "group_task",
                        {
                            "text": "\n".join(lines), "start": f"task_{tid}",
                            "photo_file": photo_file,
                            "media_id": brief_image["media_id"] if brief_image else None,
                            "task_id": tid,
                            "admin_id": admin_id, "operation_id": operation_id,
                        },
                        chat_id=target, topic_id=OPS_TOPIC_TASKS,
                        media_id=brief_image["media_id"] if brief_image else None,
                    )
                    announcement_status = "queued"
                else:
                    announcement_status = "not_configured"
                    announce_error = "Приватная OPS-группа не настроена"
            await db.commit()
    except Exception:
        await _remove_saved_images(
            [uploaded_brief_image] if uploaded_brief_image else []
        )
        raise
    return _json({
        "ok": True,
        "task_id": tid,
        "operation_id": operation_id,
        "idempotent": False,
        "personal": bool(assigned_to),
        "repeatable": repeatable,
        "evidence_policy": evidence_policy,
        "max_participants": max_participants,
        "budget_cap": budget_cap,
        "has_photo": bool(photo_file),
        "announced": False,
        "announcement_status": announcement_status,
        "announce_error": announce_error,
        "template_id": template_id,
        "template_version_id": template_version_id,
    })


async def api_admin_task_approve(request):
    """Решение по конкретному assignment; выплата привязана к нему навсегда."""
    admin_id, err = await _require_capability(request, "task.review")
    if err is not None:
        return err
    body = await _body(request)
    tid = _as_int(body.get("task_id"))
    if tid is None:
        return _json({"error": "task"}, status=400)
    assignment_id = _as_int(body.get("assignment_id"))
    operation_id = _operation_uuid(body.get("operation_id"))
    if assignment_id is None or not operation_id:
        return _json({
            "error": "decision_identity",
            "message": "Для безопасного решения нужны assignment_id и operation_id UUID.",
        }, status=400)
    decision = body.get("decision")
    if decision is None:
        decision = "approve" if body.get("approve", True) else "revise"
    if decision not in ("approve", "revise", "reject"):
        return _json({"error": "decision"}, status=400)
    ok = decision == "approve"
    note = (body.get("note") or "").strip()[:300]
    if decision in ("revise", "reject") and len(note) < 3:
        return _json({
            "error": "note",
            "message": "Укажи, что именно нужно доработать.",
        }, status=400)
    decision_hash = _request_fingerprint({
        "task_id": tid, "assignment_id": assignment_id,
        "admin_id": int(admin_id), "decision": decision, "note": note,
        "revision_due_at": str(body.get("revision_due_at") or ""),
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "task.review"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        registered = await (await db.execute(
            "SELECT 1 FROM operation_registry WHERE operation_id=?",
            (operation_id,),
        )).fetchone()
        if not await _claim_operation_in_tx(
            db, operation_id, "task_review", decision_hash, admin_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        row = await (await db.execute(
            "SELECT * FROM tasks WHERE id=?", (tid,))).fetchone()
        if not row:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        t = dict(row)
        prior_command = await (await db.execute(
            "SELECT * FROM task_review_commands WHERE operation_id=?",
            (operation_id,),
        )).fetchone()
        if prior_command:
            if (
                prior_command["request_hash"] != decision_hash
                or int(prior_command["assignment_id"]) != assignment_id
            ):
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            await db.rollback()
            return _json({
                "ok": True, "assignment_id": assignment_id,
                "status": prior_command["result_status"],
                "operation_id": operation_id, "idempotent": True,
            })
        assignment = await (await db.execute(
            "SELECT * FROM task_assignments WHERE id=? AND task_id=?",
            (assignment_id, tid),
        )).fetchone()
        if not assignment:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if assignment["decision_operation_id"] == operation_id:
            if assignment["decision_request_hash"] != decision_hash:
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            status_value = assignment["status"]
            await db.rollback()
            return _json({
                "ok": True, "assignment_id": assignment_id,
                "status": status_value, "operation_id": operation_id,
                "idempotent": True,
            })
        operation_used = await (await db.execute(
            "SELECT id FROM task_assignments "
            "WHERE decision_operation_id=? AND id<>?",
            (operation_id, assignment_id),
        )).fetchone()
        if operation_used:
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        if assignment["status"] != "review":
            await db.rollback()
            return _json({
                "error": "already_decided",
                "message": "Это выполнение уже обработано.",
            }, status=409)
        claimed_by = int(assignment["user_id"])
        if ok and claimed_by == int(admin_id):
            await db.rollback()
            return _json({
                "error": "self_review",
                "message": "Нельзя подтверждать собственное выполнение. Нужен другой ответственный.",
            }, status=403)
        if ok and t.get("created_by") == admin_id:
            await db.rollback()
            return _json({
                "error": "maker_checker",
                "message": "Создатель задания не может подтверждать выплату. Нужен второй ответственный.",
            }, status=403)
        reward = int(assignment["reward_snapshot"] or t.get("reward") or 0)
        if ok:
            cur = await db.execute(
                "UPDATE task_assignments SET status='done', terminal_at=?, "
                "terminal_by=?, terminal_reason='approved', "
                "decision_operation_id=?,decision_request_hash=?,version=version+1 "
                "WHERE id=? AND status='review'",
                (now_iso(), admin_id, operation_id, decision_hash, assignment_id),
            )
        elif decision == "revise":
            revision_due = body.get("revision_due_at")
            base_due = assignment["revision_due_at"] or assignment["due_at"]
            due_expired = False
            if base_due:
                try:
                    due = datetime.fromisoformat(base_due)
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=timezone.utc)
                    due_expired = due.astimezone(timezone.utc) <= datetime.now(timezone.utc)
                except (TypeError, ValueError):
                    due_expired = True
            if due_expired:
                try:
                    parsed_revision = parse_slot_iso(revision_due)
                    if (
                        not parsed_revision
                        or datetime.fromisoformat(parsed_revision) <= datetime.now(timezone.utc)
                    ):
                        raise ValueError
                    revision_due = parsed_revision
                except (TypeError, ValueError):
                    await db.rollback()
                    return _json({
                        "error": "revision_due_at",
                        "message": "Срок задания истёк — укажи новый срок доработки.",
                    }, status=400)
            else:
                revision_due = assignment["revision_due_at"]
            cur = await db.execute(
                "UPDATE task_assignments SET status='claimed', done_at=NULL, "
                "proof_note=NULL, review_note=?, revision_due_at=?, "
                "decision_operation_id=?,decision_request_hash=?,version=version+1 "
                "WHERE id=? AND status='review'",
                (note, revision_due, operation_id, decision_hash, assignment_id),
            )
        else:
            cur = await db.execute(
                "UPDATE task_assignments SET status='rejected', terminal_at=?, "
                "terminal_by=?, terminal_reason=?, review_note=?, "
                "decision_operation_id=?,decision_request_hash=?,version=version+1 "
                "WHERE id=? AND status='review'",
                (now_iso(), admin_id, note, note, operation_id, decision_hash, assignment_id),
            )
        if cur.rowcount != 1:
            await db.rollback()
            return _json({"error": "already_decided"}, status=409)
        result_status = "done" if decision == "approve" else (
            "claimed" if decision == "revise" else "rejected"
        )
        await db.execute(
            "INSERT INTO task_review_commands "
            "(operation_id,assignment_id,request_hash,result_status,created_at) "
            "VALUES (?,?,?,?,?)",
            (operation_id, assignment_id, decision_hash, result_status, now_iso()),
        )
        if not ok:
            await db.execute(
                "UPDATE task_evidence SET is_current=0 "
                "WHERE task_id=? AND assignment_id=? AND kind='after' "
                "AND is_current=1",
                (tid, assignment_id),
            )
        if ok:
            await db.execute(
                "UPDATE members SET done_count=done_count+1, bonus=bonus+? "
                "WHERE user_id=?",
                (reward, claimed_by),
            )
            if reward:
                balance_row = await (await db.execute(
                    "SELECT bonus FROM members WHERE user_id=?", (claimed_by,)
                )).fetchone()
                reward_operation = f"task_reward:assignment:{assignment_id}"
                await db.execute(
                    "INSERT INTO bonus_ledger "
                    "(user_id, amount, reason, task_id, assignment_id, created_by, "
                    "created_at, operation_id, balance_after) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        claimed_by, reward,
                        f"Задание: {t.get('title','')}", tid, assignment_id,
                        admin_id, now_iso(),
                        reward_operation, int(balance_row[0]),
                    ),
                )
                await _track_event_in_tx(
                    db, "task_reward_credited", "backend", user_id=claimed_by,
                    task_id=tid, assignment_id=assignment_id,
                    dedupe_key=f"ledger:{reward_operation}",
                )
        await _track_event_in_tx(
            db, "task_reviewed", "backend", user_id=claimed_by,
            task_id=tid, assignment_id=assignment_id,
            outcome=decision,
            properties={"decision": decision},
            dedupe_key=f"review:{operation_id}",
        )
        if decision == "approve":
            notification_text = f"✅ Задание подтверждено! +{reward} бибибонусов."
        elif decision == "revise":
            notification_text = f"Задание вернули на доработку.\nЧто исправить: {note}"
        else:
            notification_text = f"Задание отклонено без начисления.\nПричина: {note}"
        await _enqueue_outbox_in_tx(
            db,
            f"assignment:{assignment_id}:review:{operation_id}",
            "direct", {
                "text": notification_text,
                "start": f"task_{tid}" if decision == "revise" else None,
            },
            recipient_id=claimed_by,
        )
        await db.commit()
    return _json({
        "ok": True, "assignment_id": assignment_id,
        "status": result_status,
        "operation_id": operation_id, "idempotent": False,
    })


async def api_admin_task_dispute(request):
    """Two-person, idempotent correction of an incorrectly approved task."""
    body = await _body(request)
    action = str(body.get("action") or "").strip().lower()
    capability = (
        "task.dispute.request" if action == "open" else "task.dispute.decide"
    )
    admin_id, err = await _require_capability(request, capability)
    if err is not None:
        return err
    operation_id = _operation_uuid(body.get("operation_id"))
    if action not in {"open", "decide"} or not operation_id:
        return _json({
            "error": "dispute_identity",
            "message": "Нужны action и уникальный operation_id UUID.",
        }, status=400)
    if action == "open":
        assignment_id = _as_int(body.get("assignment_id"))
        reason = " ".join(str(body.get("reason") or "").split())[:300]
        if assignment_id is None or len(reason) < 5:
            return _json({
                "error": "reason",
                "message": "Опиши ошибку решения минимум пятью символами.",
            }, status=400)
        request_hash = _request_fingerprint({
            "action": action, "assignment_id": assignment_id,
            "reason": reason, "admin_id": int(admin_id),
        })
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if not await _has_capability_in_tx(db, admin_id, "task.dispute.request"):
                await db.rollback()
                return _json({"error": "admin_revoked"}, status=403)
            if not await _claim_operation_in_tx(
                db, operation_id, "task_dispute_open", request_hash, admin_id,
            ):
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            replay = await (await db.execute(
                "SELECT * FROM task_disputes WHERE open_operation_id=?",
                (operation_id,),
            )).fetchone()
            if replay:
                if replay["open_request_hash"] != request_hash:
                    await db.rollback()
                    return _json({"error": "operation_conflict"}, status=409)
                await db.rollback()
                return _json({
                    "ok": True, "dispute_id": replay["id"],
                    "status": replay["status"], "idempotent": True,
                })
            assignment = await (await db.execute(
                "SELECT a.*,t.title,t.created_by FROM task_assignments a "
                "JOIN tasks t ON t.id=a.task_id WHERE a.id=?", (assignment_id,),
            )).fetchone()
            if not assignment:
                await db.rollback()
                return _json({"error": "not_found"}, status=404)
            if assignment["status"] != "done":
                await db.rollback()
                return _json({
                    "error": "not_disputable",
                    "message": "Исправить можно только уже подтверждённое выполнение.",
                }, status=409)
            try:
                terminal_at = datetime.fromisoformat(
                    str(assignment["terminal_at"] or "")
                )
                if terminal_at.tzinfo is None:
                    terminal_at = terminal_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                await db.rollback()
                return _json({
                    "error": "dispute_timestamp",
                    "message": "Не удалось проверить срок спора; нужна ручная сверка.",
                }, status=409)
            dispute_deadline = terminal_at.astimezone(timezone.utc) + timedelta(
                days=DISPUTE_OPEN_DAYS,
            )
            if datetime.now(timezone.utc) > dispute_deadline:
                await db.rollback()
                return _json({
                    "error": "dispute_window_closed",
                    "message": (
                        f"Срок открытия спора — {DISPUTE_OPEN_DAYS} дней после "
                        "подтверждения задания — уже закончился."
                    ),
                }, status=409)
            if int(assignment["user_id"]) == int(admin_id):
                await db.rollback()
                return _json({
                    "error": "self_dispute",
                    "message": "Исполнитель не может открыть спор по собственной выплате.",
                }, status=403)
            existing = await (await db.execute(
                "SELECT id,status FROM task_disputes WHERE assignment_id=?",
                (assignment_id,),
            )).fetchone()
            if existing:
                await db.rollback()
                return _json({
                    "error": "already_disputed", "dispute_id": existing["id"],
                    "status": existing["status"],
                    "message": "По этому выполнению спор уже зарегистрирован.",
                }, status=409)
            reward = int(assignment["reward_snapshot"] or 0)
            credit = await (await db.execute(
                "SELECT amount FROM bonus_ledger WHERE assignment_id=? "
                "AND operation_id=?",
                (assignment_id, f"task_reward:assignment:{assignment_id}"),
            )).fetchone()
            manual_reconciliation_reason = ""
            if assignment["terminal_by"] is None:
                manual_reconciliation_reason = "Не найден исходный проверяющий."
            if reward < 0 or not credit or int(credit["amount"]) != reward:
                manual_reconciliation_reason = "Исходная выплата не совпадает с выполнением."
            member_balance = await (await db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (assignment["user_id"],),
            )).fetchone()
            reserved = await _reserved_bonus_in_tx(db, assignment["user_id"])
            needs_manual_reconciliation = bool(
                not member_balance
                or int(member_balance["bonus"] or 0) < reserved + reward
            )
            if needs_manual_reconciliation and not manual_reconciliation_reason:
                manual_reconciliation_reason = "Свободный баланс ниже суммы спора."
            admin_rows = await (await db.execute(
                "SELECT m.user_id FROM members m WHERE m.role='admin' AND m.status='approved' "
                "AND EXISTS (SELECT 1 FROM admin_authorities aa WHERE aa.user_id=m.user_id)"
            )).fetchall()
            eligible_admins = {int(row[0]) for row in admin_rows}
            excluded_admins = {int(admin_id), int(assignment["user_id"])}
            eligible_admins.difference_update(excluded_admins)
            if not eligible_admins:
                await db.rollback()
                return _json({
                    "error": "no_independent_reviewer",
                    "message": "Нет второго действующего ответственного, который сможет решить спор.",
                }, status=409)
            opened_at = now_iso()
            dispute_status = (
                "manual_required" if manual_reconciliation_reason else "pending"
            )
            cursor = await db.execute(
                "INSERT INTO task_disputes "
                "(assignment_id,task_id,user_id,reward,reason,reconciliation_reason,"
                "status,opened_by,"
                "opened_at,open_operation_id,open_request_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    assignment_id, assignment["task_id"], assignment["user_id"],
                    reward, reason, manual_reconciliation_reason or None,
                    dispute_status, admin_id, opened_at,
                    operation_id, request_hash,
                ),
            )
            dispute_id = cursor.lastrowid
            await _track_event_in_tx(
                db, "task_dispute_opened", "backend",
                user_id=assignment["user_id"], task_id=assignment["task_id"],
                assignment_id=assignment_id, outcome=dispute_status,
                dedupe_key=f"task_dispute_open:{operation_id}",
            )
            await _enqueue_capability_holders_in_tx(
                db, f"task_dispute:{dispute_id}:opened",
                "⚠️ Открыт спор по подтверждённому заданию\n"
                f"Задание: {assignment['title']}\nПричина: {reason}\n"
                + ((manual_reconciliation_reason + " Нужна ручная сверка с Бибибайком.\n") if manual_reconciliation_reason else "")
                + "Нужен второй ответственный в приложении.",
                "task.dispute.decide",
            )
            await db.commit()
        return _json({
            "ok": True, "dispute_id": dispute_id,
            "status": dispute_status, "idempotent": False,
            "manual_reconciliation": bool(manual_reconciliation_reason),
        })

    dispute_id = _as_int(body.get("dispute_id"))
    decision = str(body.get("decision") or "").strip().lower()
    note = " ".join(str(body.get("note") or "").split())[:300]
    reconciliation_reference = str(
        body.get("reconciliation_reference") or ""
    ).strip()[:100]
    if dispute_id is None or decision not in {
        "approve", "reject", "manual_reversed", "manual_no_change",
    }:
        return _json({"error": "decision"}, status=400)
    if len(note) < 3:
        return _json({
            "error": "note", "message": "Укажи, что проверил второй ответственный.",
        }, status=400)
    if decision in {"manual_reversed", "manual_no_change"} and not re.fullmatch(
        r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9._:/-]{2,99}",
        reconciliation_reference,
    ):
        return _json({
            "error": "reconciliation_reference",
            "message": "Укажи номер операции или обращения из Бибибайка без пробелов.",
        }, status=400)
    decision_hash = _request_fingerprint({
        "action": action, "dispute_id": dispute_id, "decision": decision,
        "note": note, "admin_id": int(admin_id),
        "reconciliation_reference": reconciliation_reference,
    })
    await _expire_due_tasks()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "task.dispute.decide"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        if not await _claim_operation_in_tx(
            db, operation_id, "task_dispute_decide", decision_hash, admin_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        replay = await (await db.execute(
            "SELECT * FROM task_disputes WHERE decision_operation_id=?",
            (operation_id,),
        )).fetchone()
        if replay:
            if replay["decision_request_hash"] != decision_hash:
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            await db.rollback()
            return _json({
                "ok": True, "dispute_id": replay["id"],
                "status": replay["status"], "idempotent": True,
            })
        dispute = await (await db.execute(
            "SELECT d.*,a.status AS assignment_status,a.done_at,a.terminal_by,"
            "t.title,t.repeatable,t.status AS task_status "
            "FROM task_disputes d JOIN task_assignments a ON a.id=d.assignment_id "
            "JOIN tasks t ON t.id=d.task_id WHERE d.id=?", (dispute_id,),
        )).fetchone()
        if not dispute:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if dispute["status"] not in {"pending", "manual_required"}:
            await db.rollback()
            return _json({
                "error": "already_decided", "status": dispute["status"],
                "message": "Этот спор уже обработан.",
            }, status=409)
        if int(dispute["opened_by"]) == int(admin_id):
            await db.rollback()
            return _json({
                "error": "two_person_rule",
                "message": "Открывавший спор не может сам подтвердить исправление.",
            }, status=403)
        if int(dispute["user_id"]) == int(admin_id):
            await db.rollback()
            return _json({
                "error": "self_dispute",
                "message": "Исполнитель не может решать спор по своей выплате.",
            }, status=403)
        decided_at = now_iso()
        new_balance = None
        manual_decisions = {"manual_reversed", "manual_no_change"}
        if decision in manual_decisions and dispute["status"] != "manual_required":
            await db.rollback()
            return _json({"error": "not_manual_required"}, status=409)
        if dispute["status"] == "manual_required" and decision not in manual_decisions:
            await db.rollback()
            return _json({
                "error": "manual_reconciliation_required",
                "message": (
                    "Автоматическое сторно для этого случая заблокировано. "
                    "Сначала сверь баланс и проводки с Бибибайком, затем закрой ручную сверку."
                ),
            }, status=409)
        if decision == "manual_reversed":
            if dispute["assignment_status"] != "done":
                await db.rollback()
                return _json({"error": "assignment_changed"}, status=409)
            member = await (await db.execute(
                "SELECT bonus,done_count FROM members WHERE user_id=?",
                (dispute["user_id"],),
            )).fetchone()
            if not member or int(member["done_count"] or 0) < 1:
                await db.rollback()
                return _json({"error": "member_mismatch"}, status=409)
            task_other_reserved = int((await (await db.execute(
                "SELECT COALESCE(SUM(CASE WHEN reward>0 THEN reward ELSE 0 END),0) "
                "FROM task_disputes WHERE user_id=? "
                "AND status IN ('pending','manual_required') AND id<>?",
                (dispute["user_id"], dispute_id),
            )).fetchone())[0] or 0)
            manual_reserved = int((await (await db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM manual_grant_reversals "
                "WHERE user_id=? AND status IN ('pending','manual_required')",
                (dispute["user_id"],),
            )).fetchone())[0] or 0)
            award_reserved = int((await (await db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM award_reversals "
                "WHERE user_id=? AND status IN ('pending','manual_required')",
                (dispute["user_id"],),
            )).fetchone())[0] or 0)
            remaining_reserved = (
                task_other_reserved + manual_reserved + award_reserved
            )
            balance = int(member["bonus"] or 0)
            available = max(0, balance - remaining_reserved)
            taken = min(max(0, int(dispute["reward"] or 0)), available)
            new_balance = balance - taken
            changed = await db.execute(
                "UPDATE task_assignments SET status='reversed',terminal_at=?,"
                "terminal_by=?,terminal_reason=?,review_note=?,version=version+1 "
                "WHERE id=? AND status='done'",
                (
                    decided_at, admin_id, "manual_reward_reversed", note,
                    dispute["assignment_id"],
                ),
            )
            if changed.rowcount != 1:
                await db.rollback()
                return _json({"error": "transition_conflict"}, status=409)
            await db.execute(
                "UPDATE members SET bonus=?,done_count=done_count-1 WHERE user_id=?",
                (new_balance, dispute["user_id"]),
            )
            await db.execute(
                "INSERT INTO bonus_ledger "
                "(user_id,amount,reason,task_id,assignment_id,created_by,created_at,"
                "operation_id,balance_after) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    dispute["user_id"], -taken,
                    f"Ручная сверка задания: {dispute['title']}", dispute["task_id"],
                    dispute["assignment_id"], admin_id, decided_at,
                    f"task_reward_manual_reversal:assignment:{dispute['assignment_id']}",
                    new_balance,
                ),
            )
            if not int(dispute["repeatable"] or 0) and dispute["task_status"] == "closed":
                await db.execute(
                    "UPDATE tasks SET status='open',version=version+1 "
                    "WHERE id=? AND status='closed'", (dispute["task_id"],),
                )
        if decision == "approve":
            if dispute["assignment_status"] != "done":
                await db.rollback()
                return _json({
                    "error": "assignment_changed",
                    "message": "Статус выполнения изменился; повтори сверку.",
                }, status=409)
            reward = int(dispute["reward"])
            member = await (await db.execute(
                "SELECT bonus,done_count FROM members WHERE user_id=?",
                (dispute["user_id"],),
            )).fetchone()
            if not member or int(member["done_count"] or 0) < 1:
                await db.rollback()
                return _json({
                    "error": "member_mismatch",
                    "message": "Счётчик выполнений не совпадает; нужна ручная сверка.",
                }, status=409)
            if int(member["bonus"] or 0) < reward:
                await db.rollback()
                return _json({
                    "error": "manual_reconciliation",
                    "message": "Доступного баланса уже недостаточно для сторно. Нужна ручная сверка с Бибибайком.",
                }, status=409)
            reversal_operation = f"task_reward_reversal:assignment:{dispute['assignment_id']}"
            reversal = await (await db.execute(
                "SELECT id FROM bonus_ledger WHERE operation_id=?",
                (reversal_operation,),
            )).fetchone()
            if reversal:
                await db.rollback()
                return _json({"error": "ledger_already_reversed"}, status=409)
            new_balance = int(member["bonus"] or 0) - reward
            task_other_reserved = int((await (await db.execute(
                "SELECT COALESCE(SUM(CASE WHEN reward>0 THEN reward ELSE 0 END),0) "
                "FROM task_disputes "
                "WHERE user_id=? AND status IN ('pending','manual_required') AND id<>?",
                (dispute["user_id"], dispute_id),
            )).fetchone())[0] or 0)
            manual_reserved = int((await (await db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM manual_grant_reversals "
                "WHERE user_id=? AND status IN ('pending','manual_required')",
                (dispute["user_id"],),
            )).fetchone())[0] or 0)
            award_reserved = int((await (await db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM award_reversals "
                "WHERE user_id=? AND status IN ('pending','manual_required')",
                (dispute["user_id"],),
            )).fetchone())[0] or 0)
            remaining_reserved = (
                task_other_reserved + manual_reserved + award_reserved
            )
            if new_balance < remaining_reserved:
                await db.rollback()
                return _json({
                    "error": "manual_reconciliation",
                    "message": "Остатка недостаточно для других открытых споров. Нужна ручная сверка.",
                }, status=409)
            changed = await db.execute(
                "UPDATE task_assignments SET status='reversed',terminal_at=?,"
                "terminal_by=?,terminal_reason=?,review_note=?,version=version+1 "
                "WHERE id=? AND status='done'",
                (
                    decided_at, admin_id, "reward_reversed", dispute["reason"],
                    dispute["assignment_id"],
                ),
            )
            if changed.rowcount != 1:
                await db.rollback()
                return _json({"error": "transition_conflict"}, status=409)
            await db.execute(
                "UPDATE members SET bonus=?,done_count=done_count-1 WHERE user_id=?",
                (new_balance, dispute["user_id"]),
            )
            await db.execute(
                "INSERT INTO bonus_ledger "
                "(user_id,amount,reason,task_id,assignment_id,created_by,created_at,"
                "operation_id,balance_after) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    dispute["user_id"], -reward,
                    f"Сторно задания: {dispute['title']}", dispute["task_id"],
                    dispute["assignment_id"], admin_id, decided_at,
                    reversal_operation, new_balance,
                ),
            )
            if not int(dispute["repeatable"] or 0) and dispute["task_status"] == "closed":
                await db.execute(
                    "UPDATE tasks SET status='open',version=version+1 "
                    "WHERE id=? AND status='closed'", (dispute["task_id"],),
                )
        status_value = {
            "approve": "approved", "reject": "rejected",
            "manual_reversed": "manual_reversed",
            "manual_no_change": "manual_no_change",
        }[decision]
        updated = await db.execute(
            "UPDATE task_disputes SET status=?,decided_by=?,decided_at=?,"
            "decision_note=?,decision_operation_id=?,decision_request_hash=?,"
            "reconciliation_reference=? "
            "WHERE id=? AND status=?",
            (
                status_value, admin_id, decided_at, note, operation_id,
                decision_hash, reconciliation_reference or None,
                dispute_id, dispute["status"],
            ),
        )
        if updated.rowcount != 1:
            await db.rollback()
            return _json({"error": "transition_conflict"}, status=409)
        await _track_event_in_tx(
            db, "task_dispute_resolved", "backend", user_id=dispute["user_id"],
            task_id=dispute["task_id"], assignment_id=dispute["assignment_id"],
            outcome=status_value, dedupe_key=f"task_dispute_decide:{operation_id}",
        )
        if decision == "approve":
            notification = (
                "⚠️ Решение по заданию исправлено двумя ответственными.\n"
                f"Причина: {dispute['reason']}\n"
                f"Списано: {dispute['reward']} бибибонусов. Новый баланс: {new_balance}."
            )
            await _enqueue_outbox_in_tx(
                db, f"task_dispute:{dispute_id}:participant", "direct",
                {"text": notification, "start": None},
                recipient_id=dispute["user_id"],
            )
        elif decision == "manual_reversed":
            notification = (
                "↩️ Ручная сверка завершена: выплата и выполнение исправлены.\n"
                f"Итог: {note}\nВнутренний баланс: {new_balance}."
            )
            await _enqueue_outbox_in_tx(
                db, f"task_dispute:{dispute_id}:participant", "direct",
                {"text": notification, "start": None},
                recipient_id=dispute["user_id"],
            )
        elif decision == "manual_no_change":
            notification = (
                "✅ Ручная сверка завершена: исходное решение оставлено без изменений.\n"
                f"Итог: {note}"
            )
            await _enqueue_outbox_in_tx(
                db, f"task_dispute:{dispute_id}:participant", "direct",
                {"text": notification, "start": None},
                recipient_id=dispute["user_id"],
            )
        await db.commit()
    return _json({
        "ok": True, "dispute_id": dispute_id, "status": status_value,
        "balance": new_balance, "idempotent": False,
    })


async def api_admin_grant(request):
    """Small positive-only thanks with idempotency and rolling limits."""
    admin_id, err = await _require_capability(request, "bonus.grant.small")
    if err is not None:
        return err
    body = await _body(request)
    uid = _as_int(body.get("user_id"))
    if uid is None:
        return _json({"error": "bad_user"}, status=400)
    if uid == admin_id:
        return _json({
            "error": "self_grant",
            "message": "Ответственный не может начислять бонусы самому себе.",
        }, status=403)
    operation_id = _operation_uuid(body.get("operation_id"))
    if not operation_id:
        return _json({
            "error": "operation_id",
            "message": "Для безопасного начисления нужен operation_id в формате UUID.",
        }, status=400)
    amount = _as_int(body.get("amount"))
    if amount is None:
        return _json({"error": "amount"}, status=400)
    if amount <= 0:
        return _json({
            "error": "positive_only",
            "message": (
                "Быстрая благодарность работает только на начисление. "
                "Списание выполняется через отмену награды, спор по заданию или выплату."
            ),
        }, status=400)
    if amount > MANUAL_GRANT_MAX_PER_OPERATION:
        return _json({
            "error": "amount",
            "message": f"За одну быструю благодарность можно начислить до {MANUAL_GRANT_MAX_PER_OPERATION} бонусов.",
        }, status=400)
    reason = " ".join(str(body.get("reason") or "").split())[:120]
    if len(reason) < 3:
        return _json({
            "error": "reason", "message": "Укажи короткую причину начисления."
        }, status=400)
    request_hash = _request_fingerprint({
        "user_id": int(uid), "amount": amount, "reason": reason,
        "maker_id": int(admin_id),
    })
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "bonus.grant.small"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        registered = await (await db.execute(
            "SELECT 1 FROM operation_registry WHERE operation_id=?",
            (operation_id,),
        )).fetchone()
        if not await _claim_operation_in_tx(
            db, operation_id, "manual_grant", request_hash, admin_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        previous = await (await db.execute(
            "SELECT * FROM manual_grant_commands WHERE operation_id=?",
            (operation_id,),
        )).fetchone()
        if previous:
            if previous["request_hash"] != request_hash:
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            balance = int(previous["result_balance"])
            await db.rollback()
            return _json({
                "ok": True, "balance": balance, "operation_id": operation_id,
                "idempotent": True,
            })
        if registered:
            await db.rollback()
            return _json({"error": "operation_integrity"}, status=409)
        operation_used = await (await db.execute(
            "SELECT id FROM bonus_ledger WHERE operation_id=?", (operation_id,),
        )).fetchone()
        if operation_used:
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        member = await (await db.execute(
            "SELECT bonus,status,role FROM members WHERE user_id=?", (uid,),
        )).fetchone()
        if not member or member["status"] != "approved":
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if member["role"] == "admin" and await _admin_active_in_tx(db, uid):
            eligible_checkers = await _active_capability_holder_ids_in_tx(
                db, "bonus.reversal.decide",
            )
            eligible_checkers.difference_update({int(admin_id), int(uid)})
            if not eligible_checkers:
                await db.rollback()
                return _json({
                    "error": "admin_recipient",
                    "message": (
                        "Для начисления ответственному нужен третий действующий "
                        "администратор, который сможет независимо проверить исправление."
                    ),
                }, status=409)
        if MANUAL_GRANT_DAILY_LIMIT <= 0:
            await db.rollback()
            return _json({
                "error": "grant_limit_unavailable",
                "message": "Быстрые начисления временно заблокированы: лимит настроен неверно.",
            }, status=503)
        maker_total, recipient_total = await _discretionary_totals_in_tx(
            db, admin_id, uid, cutoff,
        )
        if (
            maker_total + amount > MANUAL_GRANT_DAILY_LIMIT
            or recipient_total + amount > MANUAL_GRANT_DAILY_LIMIT
        ):
            await db.rollback()
            return _json({
                "error": "daily_limit",
                "message": (
                    f"Суточный лимит быстрых благодарностей — {MANUAL_GRANT_DAILY_LIMIT} бонусов "
                    "для одного ответственного и одного получателя."
                ),
                "limit": MANUAL_GRANT_DAILY_LIMIT,
            }, status=409)
        balance = int(member["bonus"] or 0) + amount
        changed = await db.execute(
            "UPDATE members SET bonus=? WHERE user_id=? AND status='approved'",
            (balance, uid),
        )
        if changed.rowcount != 1:
            await db.rollback()
            return _json({"error": "transition_conflict"}, status=409)
        cursor = await db.execute(
            "INSERT INTO bonus_ledger "
            "(user_id,amount,reason,created_by,created_at,operation_id,balance_after) "
            "VALUES (?,?,?,?,?,?,?)",
            (uid, amount, reason, admin_id, now_iso(), operation_id, balance),
        )
        created_at = now_iso()
        await db.execute(
            "INSERT INTO manual_grant_commands "
            "(operation_id,request_hash,user_id,amount,reason,maker_id,created_at,"
            "ledger_id,result_balance) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                operation_id, request_hash, uid, amount, reason, admin_id,
                created_at, cursor.lastrowid, balance,
            ),
        )
        await _track_event_in_tx(
            db, "manual_grant_credited", "backend", user_id=uid,
            outcome="credited", dedupe_key=f"manual_grant:{operation_id}",
        )
        await _enqueue_outbox_in_tx(
            db, f"manual_grant:{operation_id}:recipient", "direct", {
                "text": (
                    f"💰 Благодарность: +{amount} бибибонусов.\n"
                    f"Причина: {reason}\nНовый баланс: {balance}."
                )
            }, recipient_id=uid,
        )
        await db.commit()
    return _json({
        "ok": True,
        "balance": balance,
        "operation_id": operation_id,
        "idempotent": False,
    })


async def _api_admin_grant_reversal_request(admin_id, body, operation_id):
    grant_operation_id = _operation_uuid(body.get("grant_operation_id"))
    reason = " ".join(str(body.get("reason") or "").split())[:300]
    if not grant_operation_id or len(reason) < 5:
        return _json({
            "error": "reason",
            "message": "Укажи исходную операцию и проверяемую причину исправления.",
        }, status=400)
    request_hash = _request_fingerprint({
        "action": "request", "grant_operation_id": grant_operation_id,
        "reason": reason, "requester_id": int(admin_id),
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "bonus.reversal.request"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        registered = await (await db.execute(
            "SELECT command_type,request_hash,actor_id FROM operation_registry "
            "WHERE operation_id=?",
            (operation_id,),
        )).fetchone()
        if not await _claim_operation_in_tx(
            db, operation_id, "manual_grant_reversal_request",
            request_hash, admin_id,
        ):
            await db.rollback()
            if registered and (
                registered["command_type"] == "manual_grant_reversal_request"
                and registered["request_hash"] == request_hash
                and int(registered["actor_id"]) == int(admin_id)
            ):
                return _json({"error": "operation_integrity"}, status=409)
            return _json({"error": "operation_conflict"}, status=409)
        replay = await (await db.execute(
            "SELECT * FROM manual_grant_reversals WHERE request_operation_id=?",
            (operation_id,),
        )).fetchone()
        if replay:
            if replay["request_hash"] != request_hash:
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            await db.rollback()
            return _json({
                "ok": True, "reversal_id": replay["id"],
                "status": replay["status"], "amount": replay["amount"],
                "operation_id": operation_id, "idempotent": True,
            })
        if registered:
            await db.rollback()
            return _json({"error": "operation_integrity"}, status=409)
        grant = await (await db.execute(
            "SELECT c.*,l.user_id AS ledger_user_id,l.amount AS ledger_amount,"
            "l.operation_id AS ledger_operation_id,m.full_name,m.bonus "
            "FROM manual_grant_commands c "
            "JOIN bonus_ledger l ON l.id=c.ledger_id "
            "JOIN members m ON m.user_id=c.user_id "
            "WHERE c.operation_id=?", (grant_operation_id,),
        )).fetchone()
        if not grant:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if (
            int(grant["ledger_user_id"]) != int(grant["user_id"])
            or int(grant["ledger_amount"]) != int(grant["amount"])
            or grant["ledger_operation_id"] != grant_operation_id
        ):
            await db.rollback()
            return _json({
                "error": "grant_integrity",
                "message": "Исходное начисление не совпадает с проводкой.",
            }, status=409)
        orphan_reversal = await (await db.execute(
            "SELECT id FROM bonus_ledger WHERE reversal_of_ledger_id=? LIMIT 1",
            (grant["ledger_id"],),
        )).fetchone()
        if orphan_reversal:
            await db.rollback()
            return _json({
                "error": "ledger_already_reversed",
                "message": "У исходного начисления уже есть сторнирующая проводка.",
            }, status=409)
        if int(grant["user_id"]) == int(admin_id):
            await db.rollback()
            return _json({
                "error": "self_correction",
                "message": "Получатель не может запрашивать исправление своей выплаты.",
            }, status=403)
        applied = await (await db.execute(
            "SELECT id FROM manual_grant_reversals "
            "WHERE grant_operation_id=? AND status='applied' LIMIT 1",
            (grant_operation_id,),
        )).fetchone()
        if applied:
            await db.rollback()
            return _json({
                "error": "already_reversed", "reversal_id": applied["id"],
                "message": "Это начисление уже полностью отменено.",
            }, status=409)
        pending = await (await db.execute(
            "SELECT id,status FROM manual_grant_reversals "
            "WHERE grant_operation_id=? "
            "AND status IN ('pending','manual_required') LIMIT 1",
            (grant_operation_id,),
        )).fetchone()
        if pending:
            await db.rollback()
            return _json({
                "error": "correction_pending", "reversal_id": pending["id"],
                "status": pending["status"],
                "message": "Исправление уже ждёт второго ответственного.",
            }, status=409)
        eligible = await _active_capability_holder_ids_in_tx(
            db, "bonus.reversal.decide",
        )
        eligible.difference_update({int(admin_id), int(grant["user_id"])})
        if not eligible:
            await db.rollback()
            return _json({
                "error": "no_independent_checker",
                "message": "Нет второго независимого ответственного для исправления.",
            }, status=409)
        reserved = await _reserved_bonus_in_tx(db, grant["user_id"])
        available = max(0, int(grant["bonus"] or 0) - reserved)
        status_value = (
            "pending" if available >= int(grant["amount"])
            else "manual_required"
        )
        manual_reason = (
            None if status_value == "pending"
            else "Незарезервированного баланса недостаточно для полного сторно."
        )
        requested_at = now_iso()
        cursor = await db.execute(
            "INSERT INTO manual_grant_reversals "
            "(grant_operation_id,original_ledger_id,user_id,amount,reason,status,"
            "manual_reason,requested_by,requested_at,request_operation_id,request_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                grant_operation_id, grant["ledger_id"], grant["user_id"],
                grant["amount"], reason, status_value, manual_reason,
                admin_id, requested_at, operation_id, request_hash,
            ),
        )
        reversal_id = cursor.lastrowid
        await _track_event_in_tx(
            db, "manual_grant_reversal_requested", "backend",
            user_id=grant["user_id"], outcome=status_value,
            dedupe_key=f"manual_grant_reversal_request:{operation_id}",
        )
        await _enqueue_capability_holders_in_tx(
            db, f"manual_grant_reversal:{reversal_id}:requested",
            f"⚠️ Нужна проверка исправления начисления #{reversal_id}\n"
            f"Участник: {grant['full_name'] or '—'}\n"
            f"Сумма: {grant['amount']} бибибонусов\nПричина: {reason}",
            "bonus.reversal.decide",
        )
        await _enqueue_outbox_in_tx(
            db, f"manual_grant_reversal:{reversal_id}:participant", "direct",
            {"text": (
                f"Проверяется исправление ручного начисления на "
                f"{grant['amount']} бибибонусов. До решения второго "
                "ответственного эта сумма зарезервирована."
            )}, recipient_id=grant["user_id"],
        )
        await db.commit()
    return _json({
        "ok": True, "reversal_id": reversal_id,
        "status": status_value, "amount": int(grant["amount"]),
        "operation_id": operation_id, "idempotent": False,
    })


async def _api_admin_grant_reversal_decide(admin_id, body, operation_id):
    reversal_id = _as_int(body.get("reversal_id"))
    decision = str(body.get("decision") or "").strip().lower()
    note = " ".join(str(body.get("note") or "").split())[:300]
    if reversal_id is None or decision not in {"approve", "reject"}:
        return _json({"error": "decision"}, status=400)
    if len(note) < 3:
        return _json({
            "error": "note", "message": "Укажи, что проверил второй ответственный.",
        }, status=400)
    decision_hash = _request_fingerprint({
        "action": "decide", "reversal_id": reversal_id,
        "decision": decision, "note": note, "checker_id": int(admin_id),
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "bonus.reversal.decide"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        registered = await (await db.execute(
            "SELECT 1 FROM operation_registry WHERE operation_id=?",
            (operation_id,),
        )).fetchone()
        if registered:
            if not await _claim_operation_in_tx(
                db, operation_id, "manual_grant_reversal_decision",
                decision_hash, admin_id,
            ):
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            replay = await (await db.execute(
                "SELECT * FROM manual_grant_reversals "
                "WHERE decision_operation_id=?", (operation_id,),
            )).fetchone()
            if not replay or replay["decision_hash"] != decision_hash:
                await db.rollback()
                return _json({"error": "operation_integrity"}, status=409)
            await db.rollback()
            return _json({
                "ok": True, "reversal_id": replay["id"],
                "status": replay["status"], "balance": replay["result_balance"],
                "operation_id": operation_id, "idempotent": True,
            })
        reversal = await (await db.execute(
            "SELECT r.*,c.maker_id,c.ledger_id AS grant_ledger_id,"
            "l.user_id AS ledger_user_id,l.amount AS ledger_amount,"
            "l.operation_id AS ledger_operation_id,m.full_name,m.bonus "
            "FROM manual_grant_reversals r "
            "JOIN manual_grant_commands c ON c.operation_id=r.grant_operation_id "
            "JOIN bonus_ledger l ON l.id=r.original_ledger_id "
            "JOIN members m ON m.user_id=r.user_id WHERE r.id=?",
            (reversal_id,),
        )).fetchone()
        if not reversal:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if reversal["status"] not in {"pending", "manual_required"}:
            await db.rollback()
            return _json({
                "error": "already_decided", "status": reversal["status"],
                "message": "Это исправление уже обработано.",
            }, status=409)
        if int(reversal["requested_by"]) == int(admin_id):
            await db.rollback()
            return _json({
                "error": "two_person_rule",
                "message": "Запрос должен подтвердить другой ответственный.",
            }, status=403)
        if int(reversal["user_id"]) == int(admin_id):
            await db.rollback()
            return _json({
                "error": "self_correction",
                "message": "Получатель не может решать исправление своей выплаты.",
            }, status=403)
        if decision == "approve" and not await _has_capability_in_tx(
            db, reversal["requested_by"], "bonus.reversal.request",
        ):
            await db.rollback()
            return _json({"error": "maker_revoked"}, status=409)
        if (
            int(reversal["grant_ledger_id"]) != int(reversal["original_ledger_id"])
            or int(reversal["ledger_user_id"]) != int(reversal["user_id"])
            or int(reversal["ledger_amount"]) != int(reversal["amount"])
            or reversal["ledger_operation_id"] != reversal["grant_operation_id"]
        ):
            await db.rollback()
            return _json({"error": "grant_integrity"}, status=409)
        orphan_reversal = await (await db.execute(
            "SELECT id FROM bonus_ledger WHERE reversal_of_ledger_id=? LIMIT 1",
            (reversal["original_ledger_id"],),
        )).fetchone()
        if orphan_reversal:
            await db.rollback()
            return _json({
                "error": "ledger_already_reversed",
                "message": "У исходного начисления уже есть сторнирующая проводка.",
            }, status=409)
        result_balance = None
        reversal_ledger_id = None
        if decision == "approve":
            reserved = await _reserved_bonus_in_tx(
                db, reversal["user_id"], exclude_reversal_id=reversal_id,
            )
            balance = int(reversal["bonus"] or 0)
            available = max(0, balance - reserved)
            if available < int(reversal["amount"]):
                if reversal["status"] != "manual_required":
                    await db.execute(
                        "UPDATE manual_grant_reversals SET status='manual_required',"
                        "manual_reason=? WHERE id=? AND status='pending'",
                        (
                            "Незарезервированного баланса недостаточно для полного сторно.",
                            reversal_id,
                        ),
                    )
                    await db.commit()
                else:
                    await db.rollback()
                return _json({
                    "error": "manual_required", "status": "manual_required",
                    "message": (
                        "Полное сторно пока невозможно: незарезервированного "
                        "баланса недостаточно. Частичное списание запрещено."
                    ),
                }, status=409)
        if not await _claim_operation_in_tx(
            db, operation_id, "manual_grant_reversal_decision",
            decision_hash, admin_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        decided_at = now_iso()
        status_value = "applied" if decision == "approve" else "rejected"
        if decision == "approve":
            result_balance = balance - int(reversal["amount"])
            changed = await db.execute(
                "UPDATE members SET bonus=? WHERE user_id=? AND bonus=?",
                (result_balance, reversal["user_id"], balance),
            )
            if changed.rowcount != 1:
                await db.rollback()
                return _json({"error": "transition_conflict"}, status=409)
            cursor = await db.execute(
                "INSERT INTO bonus_ledger "
                "(user_id,amount,reason,created_by,created_at,operation_id,"
                "balance_after,reversal_of_ledger_id) VALUES (?,?,?,?,?,?,?,?)",
                (
                    reversal["user_id"], -int(reversal["amount"]),
                    f"Исправление начисления: {reversal['reason']}", admin_id,
                    decided_at, f"manual_grant_reversal:{operation_id}",
                    result_balance, reversal["original_ledger_id"],
                ),
            )
            reversal_ledger_id = cursor.lastrowid
        updated = await db.execute(
            "UPDATE manual_grant_reversals SET status=?,decided_by=?,decided_at=?,"
            "decision_note=?,decision_operation_id=?,decision_hash=?,"
            "reversal_ledger_id=?,result_balance=?,manual_reason=NULL "
            "WHERE id=? AND status IN ('pending','manual_required')",
            (
                status_value, admin_id, decided_at, note, operation_id,
                decision_hash, reversal_ledger_id, result_balance, reversal_id,
            ),
        )
        if updated.rowcount != 1:
            await db.rollback()
            return _json({"error": "transition_conflict"}, status=409)
        await _track_event_in_tx(
            db, "manual_grant_reversal_resolved", "backend",
            user_id=reversal["user_id"], outcome=status_value,
            dedupe_key=f"manual_grant_reversal_decision:{operation_id}",
        )
        participant_text = (
            f"Ручное начисление на {reversal['amount']} бибибонусов исправлено "
            f"двумя ответственными. Новый баланс: {result_balance}.\nПричина: {note}"
            if decision == "approve" else
            f"Проверка ручного начисления завершена без изменения баланса.\nИтог: {note}"
        )
        await _enqueue_outbox_in_tx(
            db, f"manual_grant_reversal:{reversal_id}:resolved:participant",
            "direct", {"text": participant_text}, recipient_id=reversal["user_id"],
        )
        await _enqueue_admins_in_tx(
            db, f"manual_grant_reversal:{reversal_id}:resolved",
            f"Исправление начисления #{reversal_id}: {status_value}. "
            "Решение проверил второй ответственный.",
        )
        await db.commit()
    return _json({
        "ok": True, "reversal_id": reversal_id, "status": status_value,
        "balance": result_balance, "operation_id": operation_id,
        "idempotent": False,
    })


async def api_admin_grant_reversal(request):
    body = await _body(request)
    action = str(body.get("action") or "").strip().lower()
    capability = (
        "bonus.reversal.request" if action == "request"
        else "bonus.reversal.decide"
    )
    admin_id, err = await _require_capability(request, capability)
    if err is not None:
        return err
    operation_id = _operation_uuid(body.get("operation_id"))
    if action not in {"request", "decide"} or not operation_id:
        return _json({
            "error": "correction_identity",
            "message": "Нужны action и уникальный operation_id UUID.",
        }, status=400)
    if action == "request":
        return await _api_admin_grant_reversal_request(
            admin_id, body, operation_id,
        )
    return await _api_admin_grant_reversal_decide(admin_id, body, operation_id)


async def api_admin_withdraw_account(request):
    """Показывает полный ID только ответственному и журналирует доступ."""
    admin_id, err = await _require_capability(request, "withdrawal.account.reveal")
    if err is not None:
        return err
    body = await _body(request)
    request_id = _as_int(body.get("request_id"))
    if request_id is None:
        return _json({"error": "request"}, status=400)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "withdrawal.account.reveal"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        item = await (await db.execute(
            "SELECT * FROM withdrawal_requests WHERE id=?", (request_id,),
        )).fetchone()
        if not item:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if int(item["user_id"]) == int(admin_id):
            await db.rollback()
            return _json({"error": "self_review"}, status=403)
        if item["status"] not in ("pending", "processing"):
            await db.rollback()
            return _json({"error": "already_decided"}, status=409)
        if (
            item["status"] == "processing"
            and item["processing_by"] is not None
            and int(item["processing_by"]) != int(admin_id)
        ):
            await db.rollback()
            return _json({
                "error": "processing_locked",
                "message": "Эту заявку уже проводит другой ответственный.",
            }, status=409)
        try:
            account_ref = _decrypt_account_ref(item["account_ciphertext"])
        except ValueError as exc:
            await db.rollback()
            return _json({"error": "account_unavailable", "message": str(exc)}, status=409)
        previous = item["status"]
        stamp = now_iso()
        if previous == "pending":
            await db.execute(
                "UPDATE withdrawal_requests SET status='processing',"
                "processing_by=?,processing_at=? "
                "WHERE id=? AND status='pending'", (admin_id, stamp, request_id),
            )
        elif item["processing_by"] is None:
            await db.execute(
                "UPDATE withdrawal_requests SET processing_by=?,processing_at=? "
                "WHERE id=? AND status='processing' AND processing_by IS NULL",
                (admin_id, stamp, request_id),
            )
        else:
            await db.execute(
                "UPDATE withdrawal_requests SET processing_at=? "
                "WHERE id=? AND status='processing' AND processing_by=?",
                (stamp, request_id, admin_id),
            )
            await db.execute(
                "INSERT INTO withdrawal_events "
                "(withdrawal_id,event_type,from_status,to_status,actor_id,created_at) "
                "VALUES (?,'processing_renewed','processing','processing',?,?)",
                (request_id, admin_id, stamp),
            )
        await db.execute(
            "INSERT INTO withdrawal_events "
            "(withdrawal_id,event_type,from_status,to_status,actor_id,created_at) "
            "VALUES (?,'account_revealed',?,?,?,?)",
            (request_id, previous, "processing", admin_id, stamp),
        )
        await db.commit()
    lease = _withdrawal_public({
        **dict(item), "status": "processing", "processing_by": admin_id,
        "processing_at": stamp,
    }, viewer_id=admin_id)
    return _json({
        "ok": True, "request_id": request_id,
        "account_ref": account_ref, "account_masked": item["account_masked"],
        "status": "processing", "lease_expires_at": lease["lease_expires_at"],
        "lease_remaining_seconds": lease["lease_remaining_seconds"],
        "lease_state": lease["lease_state"],
    })


async def api_admin_withdraw_handoff(request):
    """Освобождает свою заявку или забирает просроченную processing-lease."""
    admin_id, err = await _require_capability(request, "withdrawal.handoff")
    if err is not None:
        return err
    body = await _body(request)
    request_id = _as_int(body.get("request_id"))
    operation_id = _operation_uuid(body.get("operation_id"))
    action = str(body.get("action") or "").strip()
    reason = str(body.get("reason") or "").strip()[:200]
    if request_id is None or not operation_id or action not in ("release", "takeover"):
        return _json({"error": "request"}, status=400)
    if len(reason) < 5:
        return _json({
            "error": "reason", "message": "Укажи причину передачи заявки.",
        }, status=400)
    request_hash = _request_fingerprint({
        "request_id": request_id, "action": action, "reason": reason,
        "admin_id": int(admin_id),
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "withdrawal.handoff"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        if not await _claim_operation_in_tx(
            db, operation_id, "withdrawal_handoff", request_hash, admin_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        item = await (await db.execute(
            "SELECT * FROM withdrawal_requests WHERE id=?", (request_id,),
        )).fetchone()
        if not item:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if int(item["user_id"]) == int(admin_id):
            await db.rollback()
            return _json({"error": "self_review"}, status=403)
        if item["status"] != "processing":
            await db.rollback()
            return _json({"error": "not_processing"}, status=409)
        owner = _as_int(item["processing_by"])
        invalid_processing_timestamp = False
        if action == "release":
            if owner is None:
                await db.rollback()
                return _json({"ok": True, "status": "processing", "idempotent": True})
            if owner != int(admin_id):
                await db.rollback()
                return _json({
                    "error": "not_owner",
                    "message": "Освободить заявку может текущий ответственный.",
                }, status=403)
            new_owner = None
            event_type = "processing_released"
        else:
            if owner == int(admin_id):
                await db.rollback()
                return _json({"ok": True, "status": "processing", "idempotent": True})
            if owner is not None:
                try:
                    processing_at = datetime.fromisoformat(item["processing_at"])
                    if processing_at.tzinfo is None:
                        processing_at = processing_at.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    processing_at = None
                    invalid_processing_timestamp = True
                lease_until = (
                    processing_at.astimezone(timezone.utc) + timedelta(
                        minutes=WITHDRAW_PROCESSING_LEASE_MIN,
                    ) if processing_at is not None else None
                )
                if lease_until is not None and datetime.now(timezone.utc) < lease_until:
                    await db.rollback()
                    return _json({
                        "error": "lease_active",
                        "message": "Ответственный ещё работает с заявкой. Передача будет доступна позже.",
                    }, status=409)
            new_owner = int(admin_id)
            event_type = "processing_taken_over"
        stamp = now_iso()
        await db.execute(
            "UPDATE withdrawal_requests SET processing_by=?,processing_at=? "
            "WHERE id=? AND status='processing'",
            (new_owner, stamp if new_owner is not None else None, request_id),
        )
        await db.execute(
            "INSERT INTO withdrawal_events "
            "(withdrawal_id,event_type,from_status,to_status,actor_id,metadata_json,created_at) "
            "VALUES (?,?, 'processing','processing',?,?,?)",
            (
                request_id, event_type, admin_id,
                json.dumps({
                    "reason": reason, "operation_id": operation_id,
                    "previous_owner": owner,
                    "invalid_processing_timestamp": invalid_processing_timestamp,
                }, ensure_ascii=False, separators=(",", ":")),
                stamp,
            ),
        )
        await db.commit()
    lease = _withdrawal_public({
        **dict(item), "status": "processing", "processing_by": new_owner,
        "processing_at": stamp if new_owner is not None else None,
    }, viewer_id=admin_id)
    return _json({
        "ok": True, "status": "processing", "processing_by": new_owner,
        "lease_expires_at": lease["lease_expires_at"],
        "lease_remaining_seconds": lease["lease_remaining_seconds"],
        "lease_state": lease["lease_state"], "idempotent": False,
    })


async def api_admin_withdraw_decide(request):
    """Идемпотентно завершает внешний перевод или делает ровно один возврат."""
    admin_id, err = await _require_capability(request, "withdrawal.decide")
    if err is not None:
        return err
    body = await _body(request)
    request_id = _as_int(body.get("request_id"))
    operation_id = _operation_uuid(body.get("operation_id"))
    if request_id is None or not operation_id:
        return _json({"error": "request"}, status=400)
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        return _json({"error": "decision"}, status=400)
    note = (body.get("note") or "").strip()[:200]
    provider = "bibibike"
    external_reference = " ".join(
        str(body.get("external_reference") or "").split()
    )[:100]
    external_reference_canonical = _canonical_external_reference(external_reference)
    if decision == "approve" and len(external_reference) < 3:
        return _json({
            "error": "external_reference",
            "message": "Укажи номер операции из системы Бибибайка.",
        }, status=400)
    if decision == "reject" and len(note) < 3:
        return _json({
            "error": "note", "message": "Укажи причину возврата.",
        }, status=400)
    decision_hash = _request_fingerprint({
        "request_id": request_id, "admin_id": int(admin_id),
        "decision": decision, "provider": provider,
        "external_reference": external_reference_canonical, "note": note,
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "withdrawal.decide"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        if not await _claim_operation_in_tx(
            db, operation_id, "withdrawal_decision", decision_hash, admin_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        item = await (await db.execute(
            "SELECT * FROM withdrawal_requests WHERE id=?",
            (request_id,),
        )).fetchone()
        if not item:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if item["decision_operation_id"] == operation_id:
            if item["decision_request_hash"] != decision_hash:
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            balance_row = await (await db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (item["user_id"],),
            )).fetchone()
            await db.rollback()
            return _json({
                "ok": True, "status": item["status"],
                "balance": int(balance_row[0]) if balance_row else 0,
                "operation_id": operation_id, "idempotent": True,
            })
        conflict = await (await db.execute(
            "SELECT id FROM withdrawal_requests "
            "WHERE decision_operation_id=? AND id<>?",
            (operation_id, request_id),
        )).fetchone()
        if conflict:
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        if int(item["user_id"]) == int(admin_id):
            await db.rollback()
            return _json({"error": "self_review"}, status=403)
        if item["status"] not in ("pending", "processing"):
            await db.rollback()
            return _json({
                "error": "already_decided", "message": "Эта заявка уже обработана.",
            }, status=409)
        if decision == "approve" and item["status"] != "processing":
            await db.rollback()
            return _json({
                "error": "account_not_verified",
                "message": "Сначала открой и сверь ID аккаунта получателя.",
            }, status=409)
        if item["status"] == "processing" and (
            item["processing_by"] is None
            or int(item["processing_by"]) != int(admin_id)
        ):
            await db.rollback()
            return _json({
                "error": "processing_locked",
                "message": "Решить заявку может только ответственный, который раскрыл ID.",
            }, status=409)
        if decision == "approve" and not item["account_ciphertext"]:
            await db.rollback()
            return _json({
                "error": "legacy_request",
                "message": "У старой заявки нет ID аккаунта — отклони её с возвратом.",
            }, status=409)
        if decision == "approve":
            external_used = await (await db.execute(
                "SELECT id FROM withdrawal_requests "
                "WHERE provider=? AND external_reference_canonical=? "
                "AND status='completed' "
                "AND id<>?",
                (provider, external_reference_canonical, request_id),
            )).fetchone()
            if external_used:
                await db.rollback()
                return _json({
                    "error": "external_reference_conflict",
                    "message": "Этот номер внешней операции уже использован.",
                }, status=409)
        final_status = "completed" if decision == "approve" else "rejected_refunded"
        previous_status = item["status"]
        cur = await db.execute(
            "UPDATE withdrawal_requests SET status=?, decided_by=?, "
            "decided_at=?, note=?, reject_reason=?, decision_operation_id=?, "
            "decision_request_hash=?, provider=?, external_reference=?, "
            "external_reference_canonical=? "
            "WHERE id=? AND status IN ('pending','processing')",
            (
                final_status, admin_id, now_iso(), note,
                note if decision == "reject" else None,
                operation_id, decision_hash, provider,
                external_reference if decision == "approve" else None,
                external_reference_canonical if decision == "approve" else None,
                request_id,
            ),
        )
        await _track_event_in_tx(
            db, "withdrawal_decided", "backend", user_id=item["user_id"],
            outcome=final_status,
            properties={"decision": decision},
            dedupe_key=f"withdrawal:{request_id}:{operation_id}",
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
                "(user_id, amount, reason, task_id, withdrawal_id, created_by, "
                "created_at, operation_id, balance_after) "
                "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, "
                "(SELECT bonus FROM members WHERE user_id=?))",
                (
                    item["user_id"], item["amount"],
                    f"Возврат по заявке на перевод #{request_id}",
                    request_id, admin_id, now_iso(),
                    f"withdraw:{item['operation_id'] or request_id}:refund",
                    item["user_id"],
                ),
            )
        await db.execute(
            "INSERT INTO withdrawal_events "
            "(withdrawal_id,event_type,from_status,to_status,actor_id,operation_id,"
            "created_at,metadata_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                request_id, "completed" if decision == "approve" else "refunded",
                previous_status, final_status, admin_id, operation_id, now_iso(),
                json.dumps({"provider": provider}, separators=(",", ":")),
            ),
        )
        balance_row = await (await db.execute(
            "SELECT bonus FROM members WHERE user_id=?",
            (item["user_id"],),
        )).fetchone()
        if decision == "approve":
            message = (
                f"✅ Перевод бибибонусов #{request_id} выполнен.\n"
                f"Сумма: {item['amount']} бибибонусов."
            )
        else:
            message = (
                f"↩️ Перевод бибибонусов #{request_id} пока не выполнен.\n"
                f"{item['amount']} бонусов возвращены на баланс."
            )
        if note:
            message += f"\nКомментарий: {note}"
        await _enqueue_outbox_in_tx(
            db, f"withdrawal:{request_id}:{final_status}", "direct",
            {"text": message, "start": None}, recipient_id=item["user_id"],
        )
        await db.commit()
    return _json({
        "ok": True,
        "status": final_status,
        "balance": int(balance_row[0]) if balance_row else 0,
        "operation_id": operation_id,
        "idempotent": False,
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
    except Exception as exc:
        logger.warning(
            "Не удалось проверить подписку %s (%s)",
            _telegram_log_identity(uid), type(exc).__name__,
        )
        return None
    status = getattr(member, "status", "")
    status = getattr(status, "value", status)
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def confirm_referral(uid):
    """Persist a successful getChatMember check and confirm both referral gates."""
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT referred_by, ref_confirmed, status FROM members WHERE user_id=?",
            (uid,))).fetchone()
        if not row or not row["referred_by"] or row["referred_by"] == uid:
            await db.rollback()
            return None, 0, 0
        if row["ref_confirmed"]:
            await db.rollback()
            return row["referred_by"], 0, 0
        await db.execute(
            "UPDATE members SET group_membership_status='member',"
            "group_joined_at=COALESCE(group_joined_at,?),group_left_at=NULL "
            "WHERE user_id=?",
            (now_iso(), uid),
        )
        referrer, count, rewarded = await _confirm_referral_if_ready_in_tx(
            db, uid,
        )
        if referrer:
            await _enqueue_outbox_in_tx(
                db, f"referral:{uid}:confirmed:referrer:{referrer}", "direct",
                {"text": _referral_progress_message(count, rewarded), "start": None},
                recipient_id=referrer,
            )
        await db.commit()
    return referrer or row["referred_by"], count, rewarded


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
    if member["status"] != "approved":
        return _json({
            "ok": True,
            "confirmed": False,
            "pending_approval": True,
            "message": "Подписка подтверждена. Приглашение засчитается после одобрения заявки.",
        })
    await confirm_referral(uid)
    return _json({"ok": True, "confirmed": True})


async def _admin_demotion_block_in_tx(db, user_id):
    """Return a fail-closed reason when removing this admin would strand work."""
    if await (await db.execute(
        "SELECT 1 FROM admin_authorities WHERE user_id=? AND origin='env'",
        (int(user_id),),
    )).fetchone():
        return "Этот ответственный закреплён в ADMIN_IDS. Сначала измени защищённую конфигурацию."
    active_admins = {
        int(row[0]) for row in await (await db.execute(
            "SELECT m.user_id FROM members m WHERE m.role='admin' AND m.status='approved' "
            "AND EXISTS (SELECT 1 FROM admin_authorities aa WHERE aa.user_id=m.user_id)"
        )).fetchall()
    }
    remaining = active_admins - {int(user_id)}
    if len(remaining) < 2:
        return "После снятия роли должны остаться минимум два действующих ответственных."
    processing = await (await db.execute(
        "SELECT id FROM withdrawal_requests WHERE status='processing' "
        "AND processing_by=? LIMIT 1", (user_id,),
    )).fetchone()
    if processing:
        return "Сначала передай активную заявку на выплату другому ответственному."
    disputes = await (await db.execute(
        "SELECT opened_by,user_id FROM task_disputes "
        "WHERE status IN ('pending','manual_required')"
    )).fetchall()
    for dispute in disputes:
        if not (remaining - {int(dispute["opened_by"]), int(dispute["user_id"])}):
            return "Снятие роли оставит открытый спор без независимого проверяющего."
    corrections = await (await db.execute(
        "SELECT requested_by,user_id FROM manual_grant_reversals "
        "WHERE status IN ('pending','manual_required')"
    )).fetchall()
    for correction in corrections:
        if int(correction["requested_by"]) == int(user_id):
            return "Сначала заверши запрошенное исправление ручного начисления."
        if not (
            remaining
            - {int(correction["requested_by"]), int(correction["user_id"])}
        ):
            return "Снятие роли оставит исправление начисления без второго проверяющего."
    return ""


async def _active_access_grant_in_tx(db, user_id, preset, origin="manual"):
    return await (await db.execute(
        "SELECT * FROM staff_access_grants WHERE user_id=? AND preset=? "
        "AND origin=? AND status='active'",
        (int(user_id), preset, origin),
    )).fetchone()


async def _access_generation_in_tx(db, user_id, preset, origin="manual"):
    return int((await (await db.execute(
        "SELECT COALESCE(MAX(generation),0) FROM staff_access_grants "
        "WHERE user_id=? AND preset=? AND origin=?",
        (int(user_id), preset, origin),
    )).fetchone())[0])


async def _api_admin_access_request(owner_id, body):
    change_action = str(body.get("change_action") or "").strip().lower()
    target_id = _as_int(body.get("target_user_id"))
    preset = str(body.get("preset") or "").strip().lower()
    expected_generation = _as_int(body.get("expected_generation"))
    reason = " ".join(str(body.get("reason") or "").split())[:300]
    operation_id = _operation_uuid(body.get("operation_id"))
    if (
        change_action not in {"assign", "revoke"} or target_id is None
        or target_id <= 0 or preset not in CAPABILITY_PRESETS
        or expected_generation is None or expected_generation < 0
        or len(reason) < 5 or not operation_id
    ):
        return _json({"error": "access_request"}, status=400)
    if int(target_id) == int(owner_id):
        return _json({"error": "self_access_change"}, status=403)
    canonical = {
        "change_action": change_action, "target_user_id": int(target_id),
        "preset": preset, "expected_generation": int(expected_generation),
        "reason": reason, "requested_by": int(owner_id),
    }
    request_hash = _request_fingerprint(canonical)
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, owner_id, "access.request"):
            await db.rollback()
            return _json({"error": "capability_revoked"}, status=403)
        replay = await (await db.execute(
            "SELECT * FROM staff_access_changes WHERE request_operation_id=?",
            (operation_id,),
        )).fetchone()
        if replay:
            await db.rollback()
            if replay["request_hash"] != request_hash:
                return _json({"error": "operation_conflict"}, status=409)
            return _json({
                "ok": True, "queued": True, "change_id": replay["id"],
                "status": replay["status"], "idempotent": True,
            })
        if not await _claim_operation_in_tx(
            db, operation_id, "staff_access_request", request_hash, owner_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        member = await (await db.execute(
            "SELECT status,role FROM members WHERE user_id=?", (target_id,),
        )).fetchone()
        if not member or member["status"] != "approved" or member["role"] not in ROLE_TITLES:
            await db.rollback()
            return _json({"error": "target_not_approved"}, status=409)
        generation = await _access_generation_in_tx(db, target_id, preset)
        active = await _active_access_grant_in_tx(db, target_id, preset)
        if generation != int(expected_generation):
            await db.rollback()
            return _json({"error": "access_generation_conflict", "generation": generation}, status=409)
        if (change_action == "assign" and active) or (change_action == "revoke" and not active):
            await db.rollback()
            return _json({
                "error": "access_already_assigned" if active else "access_not_assigned"
            }, status=409)
        checkers = await _active_capability_holder_ids_in_tx(db, "access.decide")
        checkers.difference_update({int(owner_id), int(target_id)})
        if not checkers:
            await db.rollback()
            return _json({"error": "no_independent_checker"}, status=409)
        try:
            cursor = await db.execute(
                "INSERT INTO staff_access_changes "
                "(target_user_id,change_action,preset,expected_generation,reason,status,"
                "requested_by,requested_at,request_operation_id,request_hash) "
                "VALUES (?,?,?,?,?,'pending',?,?,?,?)",
                (target_id, change_action, preset, expected_generation, reason,
                 owner_id, now_iso(), operation_id, request_hash),
            )
        except sqlite3.IntegrityError:
            await db.rollback()
            return _json({"error": "access_change_pending"}, status=409)
        change_id = int(cursor.lastrowid)
        await _enqueue_capability_holders_in_tx(
            db, f"staff_access:{change_id}:requested",
            f"Нужна проверка изменения доступа #{change_id}: {change_action} {preset}.",
            "access.view",
        )
        await db.commit()
    return _json({"ok": True, "queued": True, "change_id": change_id,
                  "status": "pending", "idempotent": False})


async def _api_admin_access_decide(owner_id, body):
    change_id = _as_int(body.get("change_id"))
    decision = str(body.get("decision") or "").strip().lower()
    note = " ".join(str(body.get("note") or "").split())[:300]
    operation_id = _operation_uuid(body.get("operation_id"))
    if change_id is None or decision not in {"approve", "reject"} or len(note) < 3 or not operation_id:
        return _json({"error": "access_decision"}, status=400)
    decision_hash = _request_fingerprint({
        "change_id": int(change_id), "decision": decision,
        "note": note, "decided_by": int(owner_id),
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, owner_id, "access.decide"):
            await db.rollback()
            return _json({"error": "capability_revoked"}, status=403)
        replay = await (await db.execute(
            "SELECT * FROM staff_access_changes WHERE decision_operation_id=?",
            (operation_id,),
        )).fetchone()
        if replay:
            await db.rollback()
            if replay["decision_hash"] != decision_hash:
                return _json({"error": "operation_conflict"}, status=409)
            return _json({**json.loads(replay["result_json"] or "{}"), "idempotent": True})
        if not await _claim_operation_in_tx(
            db, operation_id, "staff_access_decision", decision_hash, owner_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        change = await (await db.execute(
            "SELECT * FROM staff_access_changes WHERE id=?", (change_id,),
        )).fetchone()
        if not change:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if change["status"] != "pending":
            await db.rollback()
            return _json({"error": "already_decided"}, status=409)
        target_id = int(change["target_user_id"])
        if int(change["requested_by"]) == int(owner_id) or target_id == int(owner_id):
            await db.rollback()
            return _json({"error": "two_person_rule"}, status=403)
        if decision == "approve" and not await _has_capability_in_tx(
            db, change["requested_by"], "access.request",
        ):
            await db.rollback()
            return _json({"error": "maker_revoked"}, status=409)
        preset = str(change["preset"])
        active = await _active_access_grant_in_tx(db, target_id, preset)
        generation = await _access_generation_in_tx(db, target_id, preset)
        if decision == "approve" and generation != int(change["expected_generation"]):
            await db.rollback()
            return _json({"error": "access_generation_conflict"}, status=409)
        if decision == "approve" and (
            (change["change_action"] == "assign" and active)
            or (change["change_action"] == "revoke" and not active)
        ):
            await db.rollback()
            return _json({"error": "access_state_changed"}, status=409)
        if decision == "approve" and change["change_action"] == "revoke" and preset == "owner":
            owners = await _active_capability_holder_ids_in_tx(db, "access.view")
            other = await (await db.execute(
                "SELECT 1 FROM staff_access_grants WHERE user_id=? AND preset='owner' "
                "AND status='active' AND id<>? LIMIT 1", (target_id, active["id"]),
            )).fetchone()
            if len(owners if other else owners - {target_id}) < 2:
                await db.rollback()
                return _json({"error": "minimum_owners"}, status=409)
        before = await _effective_staff_access_in_tx(db, target_id)
        status_value = "applied" if decision == "approve" else "rejected"
        if decision == "approve" and change["change_action"] == "assign":
            await _insert_access_grant_snapshot_in_tx(
                db, target_id, preset, "manual", operation_id=operation_id,
                granted_by=change["requested_by"], approved_by=owner_id,
            )
            if preset == "owner":
                await db.execute(
                    "INSERT INTO admin_authorities (user_id,origin,granted_operation_id,granted_at) "
                    "VALUES (?,'manual',?,?) ON CONFLICT(user_id,origin) DO UPDATE SET "
                    "granted_operation_id=excluded.granted_operation_id,granted_at=excluded.granted_at",
                    (target_id, operation_id, now_iso()),
                )
                await db.execute("UPDATE members SET role='admin' WHERE user_id=?", (target_id,))
        elif decision == "approve":
            await db.execute(
                "UPDATE staff_access_grants SET status='revoked',revoked_by=?,"
                "revoke_operation_id=?,revoked_at=? WHERE id=? AND status='active'",
                (owner_id, operation_id, now_iso(), active["id"]),
            )
            if preset == "owner":
                await db.execute(
                    "DELETE FROM admin_authorities WHERE user_id=? AND origin='manual'", (target_id,),
                )
                still_owner = await (await db.execute(
                    "SELECT 1 FROM staff_access_grants WHERE user_id=? AND preset='owner' "
                    "AND status='active' LIMIT 1", (target_id,),
                )).fetchone()
                if not still_owner:
                    await db.execute(
                        "UPDATE members SET role='employee' WHERE user_id=? AND role='admin'", (target_id,),
                    )
        after = await _effective_staff_access_in_tx(db, target_id)
        result = {"ok": True, "change_id": int(change_id), "status": status_value,
                  "target_user_id": target_id, "preset": preset,
                  "change_action": change["change_action"], "idempotent": False}
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        await db.execute(
            "UPDATE staff_access_changes SET status=?,decided_by=?,decided_at=?,"
            "decision_note=?,decision_operation_id=?,decision_hash=?,result_json=? "
            "WHERE id=? AND status='pending'",
            (status_value, owner_id, now_iso(), note, operation_id,
             decision_hash, result_json, change_id),
        )
        await db.execute(
            "INSERT INTO staff_access_events (target_user_id,preset,event_type,actor_id,"
            "operation_id,policy_version,before_json,after_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (target_id, preset, f"{change['change_action']}_{status_value}", owner_id,
             operation_id, RBAC_POLICY_VERSION,
             json.dumps(before, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             json.dumps(after, ensure_ascii=False, sort_keys=True, separators=(",", ":")), now_iso()),
        )
        await _enqueue_outbox_in_tx(
            db, f"staff_access:{change_id}:target", "direct",
            {"text": f"Изменение доступа {preset}: {status_value}."}, recipient_id=target_id,
        )
        await _enqueue_capability_holders_in_tx(
            db, f"staff_access:{change_id}:resolved",
            f"Изменение доступа #{change_id}: {status_value}.", "access.view",
        )
        await db.commit()
    return _json(result)


async def api_admin_access_get(request):
    owner_id, err = await _require_capability(request, "access.view")
    if err is not None:
        return err
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        grants = await (await db.execute(
            "SELECT * FROM staff_access_grants WHERE status='active' "
            "ORDER BY user_id,preset,origin"
        )).fetchall()
        changes = await (await db.execute(
            "SELECT * FROM staff_access_changes "
            "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,id DESC LIMIT 200"
        )).fetchall()
        result_grants = []
        for grant in grants:
            caps = await (await db.execute(
                "SELECT capability FROM staff_grant_capabilities "
                "WHERE grant_id=? ORDER BY capability", (grant["id"],),
            )).fetchall()
            result_grants.append({
                **dict(grant), "capabilities": [str(row[0]) for row in caps],
            })
    return _json({
        "ok": True, "viewer_id": int(owner_id),
        "policy_version": RBAC_POLICY_VERSION,
        "presets": {key: sorted(value) for key, value in CAPABILITY_PRESETS.items()},
        "grants": result_grants, "changes": [dict(row) for row in changes],
    })


async def api_admin_access_post(request):
    body = await _body(request)
    action = str(body.get("action") or "").strip().lower()
    if action not in {"request", "decide"}:
        return _json({"error": "access_action"}, status=400)
    capability = "access.request" if action == "request" else "access.decide"
    owner_id, err = await _require_capability(request, capability)
    if err is not None:
        return err
    if action == "request":
        return await _api_admin_access_request(owner_id, body)
    return await _api_admin_access_decide(owner_id, body)


async def api_admin_set_role(request):
    """Immediate ordinary roles; admin elevation/demotion uses maker-checker."""
    body = await _body(request)
    action = str(body.get("action") or "request").strip().lower()
    requested_role = str(body.get("role") or "")
    capability = (
        "access.decide" if action == "decide" else
        "access.request" if requested_role == "admin" else
        "member.role.manage_basic"
    )
    admin_id, err = await _require_capability(request, capability)
    if err is not None:
        return err
    if action == "decide":
        change_id = _as_int(body.get("change_id"))
        decision = str(body.get("decision") or "").strip().lower()
        operation_id = _operation_uuid(body.get("operation_id"))
        note = " ".join(str(body.get("note") or "").split())[:200]
        if change_id is None or decision not in {"approve", "reject"} or not operation_id:
            return _json({"error": "decision_identity"}, status=400)
        if len(note) < 3:
            return _json({
                "error": "note", "message": "Коротко укажи, что проверил."
            }, status=400)
        decision_hash = _request_fingerprint({
            "change_id": change_id, "decision": decision,
            "checker_id": int(admin_id), "note": note,
        })
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if not await _has_capability_in_tx(db, admin_id, "access.decide"):
                await db.rollback()
                return _json({"error": "admin_revoked"}, status=403)
            if not await _claim_operation_in_tx(
                db, operation_id, "admin_role_decision", decision_hash, admin_id,
            ):
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            replay = await (await db.execute(
                "SELECT * FROM admin_role_changes WHERE decision_operation_id=?",
                (operation_id,),
            )).fetchone()
            if replay:
                if replay["decision_hash"] != decision_hash:
                    await db.rollback()
                    return _json({"error": "operation_conflict"}, status=409)
                await db.rollback()
                return _json({
                    "ok": True, "change_id": replay["id"],
                    "status": replay["status"], "role": replay["to_role"],
                    "idempotent": True,
                })
            collision = await (await db.execute(
                "SELECT id FROM admin_role_changes WHERE request_operation_id=?",
                (operation_id,),
            )).fetchone()
            if collision:
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            change = await (await db.execute(
                "SELECT * FROM admin_role_changes WHERE id=?", (change_id,),
            )).fetchone()
            if not change:
                await db.rollback()
                return _json({"error": "not_found"}, status=404)
            if change["status"] != "pending":
                await db.rollback()
                return _json({"error": "already_decided"}, status=409)
            if decision == "approve" and not await _has_capability_in_tx(
                db, change["requested_by"], "access.request",
            ):
                await db.rollback()
                return _json({"error": "maker_revoked"}, status=409)
            if int(change["requested_by"]) == int(admin_id):
                await db.rollback()
                return _json({
                    "error": "two_person_rule",
                    "message": "Запрос должен проверить другой ответственный.",
                }, status=403)
            if int(change["user_id"]) == int(admin_id):
                await db.rollback()
                return _json({
                    "error": "self_role_change",
                    "message": "Нельзя подтверждать изменение собственной роли.",
                }, status=403)
            target = await (await db.execute(
                "SELECT role,status,full_name FROM members WHERE user_id=?",
                (change["user_id"],),
            )).fetchone()
            if decision == "approve" and (
                not target or target["status"] != "approved"
                or target["role"] != change["from_role"]
            ):
                await db.rollback()
                return _json({"error": "role_changed"}, status=409)
            if decision == "approve" and change["from_role"] == "admin":
                block = await _admin_demotion_block_in_tx(db, change["user_id"])
                if block:
                    await db.rollback()
                    return _json({"error": "admin_demotion_blocked", "message": block}, status=409)
            decided_at = now_iso()
            status_value = "applied" if decision == "approve" else "rejected"
            if decision == "approve":
                if change["to_role"] == "admin":
                    await db.execute(
                        "INSERT INTO admin_authorities "
                        "(user_id,origin,granted_operation_id,granted_at) "
                        "VALUES (?,'manual',?,?) "
                        "ON CONFLICT(user_id,origin) DO UPDATE SET "
                        "granted_operation_id=excluded.granted_operation_id,"
                        "granted_at=excluded.granted_at",
                        (change["user_id"], operation_id, now_iso()),
                    )
                    await _insert_access_grant_snapshot_in_tx(
                        db, change["user_id"], "owner", "manual",
                        operation_id=operation_id,
                        granted_by=change["requested_by"], approved_by=admin_id,
                    )
                elif change["from_role"] == "admin":
                    await db.execute(
                        "UPDATE staff_access_grants SET status='revoked',revoked_by=?,"
                        "revoke_operation_id=?,revoked_at=? WHERE user_id=? "
                        "AND preset='owner' AND origin='manual' AND status='active'",
                        (admin_id, operation_id, now_iso(), change["user_id"]),
                    )
                    await db.execute(
                        "DELETE FROM admin_authorities WHERE user_id=? AND origin='manual'",
                        (change["user_id"],),
                    )
                updated = await db.execute(
                    "UPDATE members SET role=? WHERE user_id=? AND role=? AND status='approved'",
                    (change["to_role"], change["user_id"], change["from_role"]),
                )
                if updated.rowcount != 1:
                    await db.rollback()
                    return _json({"error": "transition_conflict"}, status=409)
            updated = await db.execute(
                "UPDATE admin_role_changes SET status=?,decided_by=?,decided_at=?,"
                "decision_note=?,decision_operation_id=?,decision_hash=? "
                "WHERE id=? AND status='pending'",
                (
                    status_value, admin_id, decided_at, note, operation_id,
                    decision_hash, change_id,
                ),
            )
            if updated.rowcount != 1:
                await db.rollback()
                return _json({"error": "transition_conflict"}, status=409)
            await _track_event_in_tx(
                db, "admin_role_change_resolved", "backend",
                user_id=change["user_id"], outcome=status_value,
                dedupe_key=f"admin_role_change_decision:{operation_id}",
            )
            result_text = (
                f"Роль изменена: {ROLE_TITLES[change['to_role']]}."
                if decision == "approve" else
                f"Изменение роли отклонено. Причина: {note}"
            )
            await _enqueue_outbox_in_tx(
                db, f"admin_role_change:{change_id}:result", "direct",
                {"text": result_text}, recipient_id=change["user_id"],
            )
            await _enqueue_admins_in_tx(
                db, f"admin_role_change:{change_id}:resolved",
                f"Изменение роли #{change_id}: {status_value}. Проверил второй ответственный.",
            )
            await db.commit()
        return _json({
            "ok": True, "change_id": change_id, "status": status_value,
            "role": change["to_role"], "idempotent": False,
        })

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
        required_role_capability = (
            "access.request" if row and (row["role"] == "admin" or role == "admin")
            else "member.role.manage_basic"
        )
        if not await _has_capability_in_tx(db, admin_id, required_role_capability):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        if not row:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if row["status"] != "approved":
            await db.rollback()
            return _json({
                "error": "not_approved",
                "message": "Сначала одобри заявку — потом выдавай роль.",
            }, status=409)
        supplied_operation = _operation_uuid(body.get("operation_id"))
        supplied_reason = " ".join(str(body.get("reason") or "").split())[:200]
        if supplied_operation:
            replay = await (await db.execute(
                "SELECT * FROM admin_role_changes WHERE request_operation_id=?",
                (supplied_operation,),
            )).fetchone()
            if replay:
                replay_hash = _request_fingerprint({
                    "user_id": int(uid), "from_role": replay["from_role"],
                    "to_role": role, "reason": supplied_reason,
                    "maker_id": int(admin_id),
                })
                if (
                    replay["request_hash"] != replay_hash
                    or int(replay["user_id"]) != int(uid)
                    or replay["to_role"] != role
                ):
                    await db.rollback()
                    return _json({"error": "operation_conflict"}, status=409)
                await db.rollback()
                return _json({
                    "ok": True, "queued": True, "change_id": replay["id"],
                    "status": replay["status"], "role": replay["to_role"],
                    "idempotent": True,
                })
        if row["role"] == role:
            await db.rollback()
            return _json({"ok": True, "already": True, "role": role})
        admin_transition = row["role"] == "admin" or role == "admin"
        pending_change = await (await db.execute(
            "SELECT id FROM admin_role_changes WHERE user_id=? AND status='pending'",
            (uid,),
        )).fetchone()
        if pending_change and not admin_transition:
            await db.rollback()
            return _json({
                "error": "role_change_pending",
                "message": "Сначала заверши ожидающее изменение роли ответственного.",
            }, status=409)
        if admin_transition:
            operation_id = supplied_operation
            reason = supplied_reason
            if not operation_id or len(reason) < 5:
                await db.rollback()
                return _json({
                    "error": "role_change_identity",
                    "message": "Для роли ответственного нужны причина и operation_id UUID.",
                }, status=400)
            request_hash = _request_fingerprint({
                "user_id": int(uid), "from_role": row["role"], "to_role": role,
                "reason": reason, "maker_id": int(admin_id),
            })
            if not await _claim_operation_in_tx(
                db, operation_id, "admin_role_request", request_hash, admin_id,
            ):
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            collision = await (await db.execute(
                "SELECT id FROM admin_role_changes WHERE decision_operation_id=?",
                (operation_id,),
            )).fetchone()
            if collision:
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            eligible_checkers = await _active_capability_holder_ids_in_tx(
                db, "access.decide",
            )
            eligible_checkers.difference_update({int(admin_id), int(uid)})
            if not eligible_checkers:
                await db.rollback()
                return _json({
                    "error": "no_independent_checker",
                    "message": "Нет второго независимого ответственного для проверки роли.",
                }, status=409)
            if row["role"] == "admin":
                block = await _admin_demotion_block_in_tx(db, uid)
                if block:
                    await db.rollback()
                    return _json({"error": "admin_demotion_blocked", "message": block}, status=409)
            requested_at = now_iso()
            try:
                cursor = await db.execute(
                    "INSERT INTO admin_role_changes "
                    "(user_id,from_role,to_role,reason,status,requested_by,requested_at,"
                    "request_operation_id,request_hash) "
                    "VALUES (?,?,?,?,'pending',?,?,?,?)",
                    (
                        uid, row["role"], role, reason, admin_id, requested_at,
                        operation_id, request_hash,
                    ),
                )
            except sqlite3.IntegrityError:
                await db.rollback()
                return _json({
                    "error": "role_change_pending",
                    "message": "Для участника уже ожидает проверки другое изменение роли.",
                }, status=409)
            change_id = cursor.lastrowid
            await _track_event_in_tx(
                db, "admin_role_change_requested", "backend", user_id=uid,
                outcome="pending", dedupe_key=f"admin_role_change:{operation_id}",
            )
            await _enqueue_admins_in_tx(
                db, f"admin_role_change:{change_id}:requested",
                f"🛡️ Нужна проверка изменения роли #{change_id}\n"
                f"Участник: {row['full_name'] or 'без имени'}\n"
                f"{ROLE_TITLES[row['role']]} → {ROLE_TITLES[role]}\nПричина: {reason}",
            )
            await db.commit()
            return _json({
                "ok": True, "queued": True, "change_id": change_id,
                "status": "pending", "role": role, "idempotent": False,
            })
        await db.execute(
            "UPDATE members SET role=? WHERE user_id=? AND status='approved'",
            (role, uid))
        await _enqueue_outbox_in_tx(
            db, f"ordinary_role:{uid}:{now_iso()}", "direct",
            {"text": f"Твоя роль теперь — {ROLE_TITLES[role]}."}, recipient_id=uid,
        )
        await db.commit()
    return _json({"ok": True, "role": role})


async def api_admin_member_tags(request):
    """Сохраняет короткие теги, по которым ответственный ищет людей."""
    admin_id, err = await _require_capability(request, "member.tags.manage")
    if err is not None:
        return err
    body = await _body(request)
    uid = _as_int(body.get("user_id"))
    if uid is None:
        return _json({"error": "bad_user"}, status=400)
    tags = _tags_list(body.get("tags"))
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "member.tags.manage"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        cur = await db.execute(
            "UPDATE members SET tags=? WHERE user_id=? AND status='approved'",
            (", ".join(tags), uid),
        )
        await db.commit()
    if cur.rowcount != 1:
        return _json({"error": "not_found"}, status=404)
    return _json({"ok": True, "tags": tags, "updated_by": admin_id})


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
            "WHERE ma.user_id=? AND ma.revoked_at IS NULL "
            "ORDER BY ma.id DESC LIMIT 60",
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
    admin_id, err = await _require_capability(request, "award.catalog.manage")
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
    if bonus is None or bonus < 0 or bonus > 200:
        return _json({
            "error": "bonus",
            "message": "Бонус награды — от 0 до 200. Более крупную выплату проводите с двойным согласованием.",
        }, status=400)
    repeatable = 1 if body.get("repeatable", True) else 0
    if bonus > 0 and repeatable:
        return _json({
            "error": "monetary_award_repeatable",
            "message": "Награда с бонусами может быть выдана участнику только один раз.",
        }, status=400)
    active = 1 if body.get("active", True) else 0
    award_id = _as_int(body.get("id"))
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "award.catalog.manage"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
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
    admin_id, err = await _require_capability(request, "award.grant")
    if err is not None:
        return err
    body = await _body(request)
    uid = _as_int(body.get("user_id"))
    award_id = _as_int(body.get("award_id"))
    if uid is None or award_id is None:
        return _json({"error": "bad_request"}, status=400)
    if uid == admin_id:
        return _json({
            "error": "self_grant",
            "message": "Ответственный не может выдавать награды самому себе.",
        }, status=403)
    operation_id = _operation_uuid(body.get("operation_id"))
    if not operation_id:
        return _json({
            "error": "operation_id",
            "message": "Для безопасной выдачи нужен operation_id в формате UUID.",
        }, status=400)
    note = (body.get("note") or "").strip()[:200]
    request_hash = _request_fingerprint({
        "user_id": int(uid), "award_id": int(award_id),
        "note": note, "maker_id": int(admin_id),
    })
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "award.grant"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        registered = await (await db.execute(
            "SELECT 1 FROM operation_registry WHERE operation_id=?",
            (operation_id,),
        )).fetchone()
        if not await _claim_operation_in_tx(
            db, operation_id, "award_grant", request_hash, admin_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        existing = await (await db.execute(
            "SELECT id, user_id, award_id, granted_by, note, bonus, balance_after "
            "FROM member_awards "
            "WHERE operation_id=?", (operation_id,),
        )).fetchone()
        if existing:
            if (
                int(existing["user_id"]) != int(uid)
                or int(existing["award_id"]) != int(award_id)
                or int(existing["granted_by"]) != int(admin_id)
                or (existing["note"] or "") != note
            ):
                await db.rollback()
                return _json({
                    "error": "operation_conflict",
                    "message": "Этот operation_id уже использован для другой награды.",
                }, status=409)
            if existing["balance_after"] is None:
                balance_row = await (await db.execute(
                    "SELECT bonus FROM members WHERE user_id=?", (uid,),
                )).fetchone()
                original_balance = int(balance_row[0]) if balance_row else 0
            else:
                original_balance = int(existing["balance_after"])
            await db.rollback()
            return _json({
                "ok": True,
                "balance": original_balance,
                "bonus": int(existing["bonus"] or 0),
                "operation_id": operation_id,
                "idempotent": True,
            })
        if registered:
            await db.rollback()
            return _json({"error": "operation_integrity"}, status=409)
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
        if bonus > 0 and len(note) < 3:
            await db.rollback()
            return _json({
                "error": "note",
                "message": "Для награды с бонусами укажи короткую причину.",
            }, status=400)
        if bonus > 0 and int(award["repeatable"] or 0):
            await db.rollback()
            return _json({"error": "monetary_award_repeatable"}, status=409)
        if bonus > 200:
            await db.rollback()
            return _json({
                "error": "bonus_limit",
                "message": "Эта старая награда превышает безопасный лимит 200 бонусов. Уменьшите её перед выдачей.",
            }, status=409)
        if bonus > 0:
            if MANUAL_GRANT_DAILY_LIMIT <= 0:
                await db.rollback()
                return _json({"error": "grant_limit_unavailable"}, status=503)
            maker_total, recipient_total = await _discretionary_totals_in_tx(
                db, admin_id, uid, cutoff,
            )
            if (
                maker_total + bonus > MANUAL_GRANT_DAILY_LIMIT
                or recipient_total + bonus > MANUAL_GRANT_DAILY_LIMIT
            ):
                await db.rollback()
                return _json({
                    "error": "daily_limit", "limit": MANUAL_GRANT_DAILY_LIMIT,
                    "message": "Суточный лимит быстрых начислений и наград исчерпан.",
                }, status=409)
        # Разовая награда занимает единственный слот '' — повторная выдача
        # упрётся в UNIQUE. У многоразовой слот уникален для каждой выдачи.
        slot = "" if not award["repeatable"] else f"{now_iso()}:{secrets.token_hex(4)}"
        try:
            await db.execute(
                "INSERT INTO member_awards "
                "(user_id, award_id, slot, bonus, note, granted_by, granted_at, "
                "operation_id, balance_after) VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, award_id, slot, bonus, note, admin_id, now_iso(),
                 operation_id, int(member["bonus"]) + bonus),
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
                "(user_id, amount, reason, task_id, created_by, created_at, "
                "operation_id, balance_after) VALUES (?,?,?,NULL,?,?,?,?)",
                (
                    uid, bonus, f"Награда: {award['title']}", admin_id,
                    now_iso(), f"award:{operation_id}", int(member["bonus"]) + bonus,
                ),
            )
        balance = int(member["bonus"]) + bonus
        await _track_event_in_tx(
            db, "award_granted", "backend", user_id=uid, outcome="granted",
            dedupe_key=f"award_granted:{operation_id}",
        )
        durable_text = f"{award['emoji']} Награда: {award['title']}"
        if award["description"]:
            durable_text += f"\n{award['description']}"
        if note:
            durable_text += f"\nОт ответственного: {note}"
        if bonus:
            durable_text += f"\n\n+{bonus} бибибонусов. Баланс: {balance}."
        await _enqueue_outbox_in_tx(
            db, f"award_grant:{operation_id}:participant", "direct",
            {"text": durable_text, "start": None}, recipient_id=uid,
        )
        await db.commit()
    return _json({
        "ok": True, "balance": balance, "bonus": bonus,
        "operation_id": operation_id, "idempotent": False,
    })


async def _award_reversal_event_in_tx(
    db, reversal_id, event_type, from_status, to_status, actor_id,
    operation_id=None, metadata=None,
):
    await db.execute(
        "INSERT INTO award_reversal_events "
        "(reversal_id,event_type,from_status,to_status,actor_id,operation_id,"
        "created_at,metadata_json) VALUES (?,?,?,?,?,?,?,?)",
        (
            reversal_id, event_type, from_status, to_status, actor_id,
            operation_id, now_iso(),
            json.dumps(
                metadata or {}, ensure_ascii=False, separators=(",", ":"),
                sort_keys=True,
            ),
        ),
    )


async def _api_admin_award_reversal_request(admin_id, body, operation_id):
    entry_id = _as_int(body.get("entry_id"))
    reason = " ".join(str(body.get("reason") or body.get("note") or "").split())[:300]
    if entry_id is None or len(reason) < 3:
        return _json({
            "error": "reason", "message": "Укажи проверенную причину снятия награды.",
        }, status=400)
    request_hash = _request_fingerprint({
        "action": "request", "entry_id": entry_id, "reason": reason,
        "requester_id": int(admin_id),
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "award.reversal.request"):
            await db.rollback()
            return _json({"error": "capability_revoked"}, status=403)
        registered = await (await db.execute(
            "SELECT 1 FROM operation_registry WHERE operation_id=?", (operation_id,),
        )).fetchone()
        if not await _claim_operation_in_tx(
            db, operation_id, "award_reversal_request", request_hash, admin_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        replay = await (await db.execute(
            "SELECT id,status,amount,request_hash FROM award_reversals "
            "WHERE request_operation_id=?", (operation_id,),
        )).fetchone()
        if replay:
            if replay["request_hash"] != request_hash:
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            await db.rollback()
            return _json({
                "ok": True, "reversal_id": replay["id"],
                "status": replay["status"], "amount": int(replay["amount"]),
                "operation_id": operation_id, "idempotent": True,
            })
        if registered:
            await db.rollback()
            return _json({"error": "operation_integrity"}, status=409)
        award = await (await db.execute(
            "SELECT ma.*,a.title,a.emoji,m.full_name,m.bonus AS current_balance,"
            "l.id AS ledger_id,l.user_id AS ledger_user_id,l.amount AS ledger_amount,"
            "l.operation_id AS ledger_operation_id,l.created_by AS ledger_created_by "
            "FROM member_awards ma JOIN awards a ON a.id=ma.award_id "
            "JOIN members m ON m.user_id=ma.user_id "
            "LEFT JOIN bonus_ledger l ON l.operation_id=('award:' || ma.operation_id) "
            "WHERE ma.id=?", (entry_id,),
        )).fetchone()
        if not award:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if award["revoked_at"] is not None:
            await db.rollback()
            return _json({"error": "already_revoked"}, status=409)
        if int(award["user_id"]) == int(admin_id):
            await db.rollback()
            return _json({"error": "self_correction"}, status=403)
        amount = int(award["bonus"] or 0)
        if amount and (
            not award["operation_id"] or award["ledger_id"] is None
            or int(award["ledger_user_id"]) != int(award["user_id"])
            or int(award["ledger_amount"]) != amount
            or award["ledger_operation_id"] != f"award:{award['operation_id']}"
            or (
                award["granted_by"] is not None
                and award["ledger_created_by"] is not None
                and int(award["ledger_created_by"]) != int(award["granted_by"])
            )
        ):
            await db.rollback()
            return _json({"error": "grant_integrity"}, status=409)
        prior = await (await db.execute(
            "SELECT id,status FROM award_reversals WHERE member_award_id=? "
            "AND status IN ('pending','manual_required','applied') "
            "ORDER BY id DESC LIMIT 1", (entry_id,),
        )).fetchone()
        if prior:
            await db.rollback()
            return _json({
                "error": "already_reversed" if prior["status"] == "applied"
                else "correction_pending",
                "reversal_id": prior["id"], "status": prior["status"],
            }, status=409)
        excluded = {int(admin_id), int(award["user_id"])}
        if award["granted_by"] is not None:
            excluded.add(int(award["granted_by"]))
        checkers = await _active_capability_holder_ids_in_tx(
            db, "award.reversal.decide",
        )
        checkers.difference_update(excluded)
        if not checkers:
            await db.rollback()
            return _json({"error": "no_independent_checker"}, status=409)
        reserved = await _reserved_bonus_in_tx(db, award["user_id"])
        available = max(0, int(award["current_balance"] or 0) - reserved)
        status_value = "pending" if available >= amount else "manual_required"
        manual_reason = None if status_value == "pending" else (
            "Незарезервированного баланса недостаточно для полного сторно."
        )
        requested_at = now_iso()
        cursor = await db.execute(
            "INSERT INTO award_reversals "
            "(member_award_id,original_ledger_id,user_id,award_id,award_title,amount,"
            "original_granted_by,original_grant_operation_id,origin,status,manual_reason,"
            "reason,requested_by,requested_at,request_operation_id,request_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry_id, award["ledger_id"], award["user_id"], award["award_id"],
                award["title"], amount, award["granted_by"], award["operation_id"],
                "maker_checker", status_value, manual_reason, reason, admin_id,
                requested_at, operation_id, request_hash,
            ),
        )
        reversal_id = cursor.lastrowid
        await _award_reversal_event_in_tx(
            db, reversal_id, "requested", None, "pending", admin_id,
            operation_id, {"amount": amount},
        )
        if status_value == "manual_required":
            await _award_reversal_event_in_tx(
                db, reversal_id, "manual_required", "pending", status_value,
                admin_id, metadata={"reason": "insufficient_unreserved_balance"},
            )
        await _track_event_in_tx(
            db, "award_reversal_requested", "backend", user_id=award["user_id"],
            outcome=status_value,
            dedupe_key=f"award_reversal_request:{operation_id}",
        )
        await _enqueue_capability_holders_in_tx(
            db, f"award_reversal:{reversal_id}:requested",
            f"⚠️ Нужна проверка снятия награды #{reversal_id}\n"
            f"Участник: {award['full_name'] or '—'}\n"
            f"Награда: {award['title']}\nСумма: {amount} бибибонусов\nПричина: {reason}",
            "award.reversal.decide",
        )
        await _enqueue_outbox_in_tx(
            db, f"award_reversal:{reversal_id}:participant", "direct",
            {"text": (
                f"Проверяется исправление награды «{award['title']}». "
                "До решения второго ответственного её бонусы зарезервированы."
            )}, recipient_id=award["user_id"],
        )
        await db.commit()
    return _json({
        "ok": True, "reversal_id": reversal_id, "status": status_value,
        "amount": amount, "operation_id": operation_id, "idempotent": False,
    })


async def _api_admin_award_reversal_decide(admin_id, body, operation_id):
    reversal_id = _as_int(body.get("reversal_id"))
    decision = str(body.get("decision") or "").strip().lower()
    note = " ".join(str(body.get("note") or "").split())[:300]
    if reversal_id is None or decision not in {"approve", "reject"}:
        return _json({"error": "decision"}, status=400)
    if len(note) < 3:
        return _json({"error": "note", "message": "Укажи, что проверил."}, status=400)
    decision_hash = _request_fingerprint({
        "action": "decide", "reversal_id": reversal_id, "decision": decision,
        "note": note, "checker_id": int(admin_id),
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "award.reversal.decide"):
            await db.rollback()
            return _json({"error": "capability_revoked"}, status=403)
        registered = await (await db.execute(
            "SELECT 1 FROM operation_registry WHERE operation_id=?", (operation_id,),
        )).fetchone()
        if registered:
            if not await _claim_operation_in_tx(
                db, operation_id, "award_reversal_decision", decision_hash, admin_id,
            ):
                await db.rollback()
                return _json({"error": "operation_conflict"}, status=409)
            replay = await (await db.execute(
                "SELECT id,status,result_balance,decision_hash FROM award_reversals "
                "WHERE decision_operation_id=?", (operation_id,),
            )).fetchone()
            if not replay or replay["decision_hash"] != decision_hash:
                await db.rollback()
                return _json({"error": "operation_integrity"}, status=409)
            await db.rollback()
            return _json({
                "ok": True, "reversal_id": replay["id"],
                "status": replay["status"], "balance": replay["result_balance"],
                "operation_id": operation_id, "idempotent": True,
            })
        reversal = await (await db.execute(
            "SELECT r.*,ma.user_id AS ma_user_id,ma.award_id AS ma_award_id,"
            "ma.bonus AS ma_bonus,ma.granted_by AS ma_granted_by,"
            "ma.operation_id AS ma_operation_id,ma.revoked_at,m.bonus AS current_balance,"
            "l.user_id AS ledger_user_id,l.amount AS ledger_amount,"
            "l.operation_id AS ledger_operation_id "
            "FROM award_reversals r JOIN member_awards ma ON ma.id=r.member_award_id "
            "JOIN members m ON m.user_id=r.user_id "
            "LEFT JOIN bonus_ledger l ON l.id=r.original_ledger_id WHERE r.id=?",
            (reversal_id,),
        )).fetchone()
        if not reversal:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if reversal["status"] not in {"pending", "manual_required"}:
            await db.rollback()
            return _json({"error": "already_decided", "status": reversal["status"]}, status=409)
        excluded = {
            int(reversal["requested_by"]), int(reversal["user_id"]),
        }
        if reversal["original_granted_by"] is not None:
            excluded.add(int(reversal["original_granted_by"]))
        if int(admin_id) in excluded:
            await db.rollback()
            return _json({"error": "two_person_rule"}, status=403)
        if decision == "approve" and not await _has_capability_in_tx(
            db, reversal["requested_by"], "award.reversal.request",
        ):
            await db.rollback()
            return _json({"error": "maker_revoked"}, status=409)
        amount = int(reversal["amount"])
        integrity_ok = (
            int(reversal["ma_user_id"]) == int(reversal["user_id"])
            and int(reversal["ma_award_id"]) == int(reversal["award_id"])
            and int(reversal["ma_bonus"] or 0) == amount
            and reversal["ma_granted_by"] == reversal["original_granted_by"]
            and reversal["ma_operation_id"] == reversal["original_grant_operation_id"]
            and reversal["revoked_at"] is None
        )
        if amount:
            integrity_ok = integrity_ok and (
                reversal["original_ledger_id"] is not None
                and int(reversal["ledger_user_id"]) == int(reversal["user_id"])
                and int(reversal["ledger_amount"]) == amount
                and reversal["ledger_operation_id"]
                == f"award:{reversal['original_grant_operation_id']}"
            )
        if not integrity_ok:
            await db.rollback()
            return _json({"error": "grant_integrity"}, status=409)
        if amount:
            prior_debit = await (await db.execute(
                "SELECT id FROM bonus_ledger WHERE reversal_of_ledger_id=? LIMIT 1",
                (reversal["original_ledger_id"],),
            )).fetchone()
            if prior_debit:
                await db.rollback()
                return _json({"error": "ledger_already_reversed"}, status=409)
        balance = int(reversal["current_balance"] or 0)
        if decision == "approve":
            reserved = await _reserved_bonus_in_tx(
                db, reversal["user_id"],
                exclude_award_reversal_id=reversal_id,
            )
            available = max(0, balance - reserved)
            if available < amount:
                if reversal["status"] != "manual_required":
                    manual_reason = (
                        "Незарезервированного баланса недостаточно для полного сторно."
                    )
                    await db.execute(
                        "UPDATE award_reversals SET status='manual_required',"
                        "manual_reason=?,version=version+1 WHERE id=? AND status='pending'",
                        (manual_reason, reversal_id),
                    )
                    await _award_reversal_event_in_tx(
                        db, reversal_id, "manual_required", "pending", "manual_required",
                        admin_id, metadata={"reason": "insufficient_unreserved_balance"},
                    )
                    await db.commit()
                else:
                    await db.rollback()
                return _json({
                    "error": "manual_required", "status": "manual_required",
                    "message": "Полное сторно пока невозможно; частичное списание запрещено.",
                }, status=409)
        if not await _claim_operation_in_tx(
            db, operation_id, "award_reversal_decision", decision_hash, admin_id,
        ):
            await db.rollback()
            return _json({"error": "operation_conflict"}, status=409)
        decided_at = now_iso()
        status_value = "applied" if decision == "approve" else "rejected"
        result_balance = None
        reversal_ledger_id = None
        if decision == "approve":
            result_balance = balance - amount
            changed = await db.execute(
                "UPDATE members SET bonus=? WHERE user_id=? AND bonus=?",
                (result_balance, reversal["user_id"], balance),
            )
            if changed.rowcount != 1:
                await db.rollback()
                return _json({"error": "transition_conflict"}, status=409)
            projected = await db.execute(
                "UPDATE member_awards SET revoked_at=?,revoked_by=?,revoke_note=?,"
                "revoke_operation_id=?,revoke_request_hash=? "
                "WHERE id=? AND revoked_at IS NULL",
                (
                    decided_at, admin_id, reversal["reason"], operation_id,
                    decision_hash, reversal["member_award_id"],
                ),
            )
            if projected.rowcount != 1:
                await db.rollback()
                return _json({"error": "transition_conflict"}, status=409)
            if amount:
                cursor = await db.execute(
                    "INSERT INTO bonus_ledger "
                    "(user_id,amount,reason,created_by,created_at,operation_id,"
                    "balance_after,reversal_of_ledger_id) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        reversal["user_id"], -amount,
                        f"Снята награда: {reversal['award_title']}. {reversal['reason']}",
                        admin_id, decided_at, f"award_reversal:{operation_id}",
                        result_balance, reversal["original_ledger_id"],
                    ),
                )
                reversal_ledger_id = cursor.lastrowid
        updated = await db.execute(
            "UPDATE award_reversals SET status=?,manual_reason=NULL,decided_by=?,"
            "decided_at=?,decision_note=?,decision_operation_id=?,decision_hash=?,"
            "reversal_ledger_id=?,result_balance=?,version=version+1 "
            "WHERE id=? AND status IN ('pending','manual_required')",
            (
                status_value, admin_id, decided_at, note, operation_id,
                decision_hash, reversal_ledger_id, result_balance, reversal_id,
            ),
        )
        if updated.rowcount != 1:
            await db.rollback()
            return _json({"error": "transition_conflict"}, status=409)
        await _award_reversal_event_in_tx(
            db, reversal_id, status_value, reversal["status"], status_value,
            admin_id, operation_id, {"amount": amount},
        )
        await _track_event_in_tx(
            db, "award_reversal_resolved", "backend", user_id=reversal["user_id"],
            outcome=status_value,
            dedupe_key=f"award_reversal_decision:{operation_id}",
        )
        if decision == "approve":
            await _track_event_in_tx(
                db, "award_revoked", "backend", user_id=reversal["user_id"],
                outcome="revoked", dedupe_key=f"award_revoked:{operation_id}",
            )
        participant_text = (
            f"Награда «{reversal['award_title']}» снята после проверки двумя ответственными."
            + (f"\nСписано {amount} бибибонусов. Баланс: {result_balance}." if amount else "")
            + f"\nПричина: {reversal['reason']}"
            if decision == "approve" else
            f"Проверка награды «{reversal['award_title']}» завершена без изменений.\nИтог: {note}"
        )
        await _enqueue_outbox_in_tx(
            db, f"award_reversal:{reversal_id}:resolved:participant", "direct",
            {"text": participant_text}, recipient_id=reversal["user_id"],
        )
        await _enqueue_capability_holders_in_tx(
            db, f"award_reversal:{reversal_id}:resolved",
            f"Исправление награды #{reversal_id}: {status_value}. Решение проверил второй ответственный.",
            "award.reversal.request",
        )
        await db.commit()
    return _json({
        "ok": True, "reversal_id": reversal_id, "status": status_value,
        "balance": result_balance, "operation_id": operation_id,
        "idempotent": False,
    })


async def api_admin_award_reversal(request):
    body = await _body(request)
    action = str(body.get("action") or "").strip().lower()
    capability = (
        "award.reversal.request" if action == "request"
        else "award.reversal.decide" if action == "decide" else None
    )
    if capability is None:
        return _json({"error": "action"}, status=400)
    admin_id, err = await _require_capability(request, capability)
    if err is not None:
        return err
    operation_id = _operation_uuid(body.get("operation_id"))
    if not operation_id:
        return _json({"error": "operation_id"}, status=400)
    if action == "request":
        return await _api_admin_award_reversal_request(
            admin_id, body, operation_id,
        )
    return await _api_admin_award_reversal_decide(admin_id, body, operation_id)


async def api_admin_award_revoke(request):
    """Legacy facade: create a two-person request and never debit immediately."""
    body = await _body(request)
    body = {
        **body, "action": "request",
        "reason": body.get("reason") or body.get("note"),
    }
    # Internal dispatch avoids parsing the request stream twice.
    admin_id, err = await _require_capability(request, "award.reversal.request")
    if err is not None:
        return err
    operation_id = _operation_uuid(body.get("operation_id"))
    if not operation_id:
        return _json({"error": "operation_id"}, status=400)
    entry_id = _as_int(body.get("entry_id"))
    legacy_note = " ".join(str(body.get("note") or "").split())[:200]
    if entry_id is not None:
        legacy_hash = _request_fingerprint({
            "entry_id": entry_id, "note": legacy_note,
            "admin_id": int(admin_id),
        })
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            legacy = await (await db.execute(
                "SELECT ma.id,ma.bonus,ma.revoke_request_hash,r.id AS reversal_id "
                "FROM member_awards ma LEFT JOIN award_reversals r "
                "ON r.member_award_id=ma.id AND r.status='applied' "
                "WHERE ma.revoke_operation_id=?", (operation_id,),
            )).fetchone()
        if legacy:
            if (
                int(legacy["id"]) != int(entry_id)
                or legacy["revoke_request_hash"] != legacy_hash
            ):
                return _json({"error": "operation_conflict"}, status=409)
            return _json({
                "ok": True, "reversal_id": legacy["reversal_id"],
                "status": "applied", "amount": int(legacy["bonus"] or 0),
                "operation_id": operation_id, "idempotent": True,
            })
    return await _api_admin_award_reversal_request(admin_id, body, operation_id)


async def api_admin_telegram_inbox_redrive(request):
    """Return one dead Telegram update to the durable queue with an audit trail."""
    admin_id, err = await _require_capability(request, "telegram.inbox.redrive")
    if err is not None:
        return err
    body = await _body(request)
    update_id = _as_int(body.get("update_id"))
    operation_id = _operation_uuid(body.get("operation_id"))
    reason = " ".join(str(body.get("reason") or "").split())[:200]
    if update_id is None or update_id < 0 or not operation_id:
        return _json({"error": "request"}, status=400)
    if len(reason) < 3:
        return _json({
            "error": "reason", "message": "Укажи причину повторной обработки.",
        }, status=400)
    request_hash = _request_fingerprint({
        "update_id": update_id, "admin_id": int(admin_id), "reason": reason,
    })
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if not await _has_capability_in_tx(db, admin_id, "telegram.inbox.redrive"):
            await db.rollback()
            return _json({"error": "admin_revoked"}, status=403)
        by_operation = await (await db.execute(
            "SELECT update_id,request_hash,result_status "
            "FROM telegram_update_redrive_commands WHERE operation_id=?",
            (operation_id,),
        )).fetchone()
        if by_operation:
            current = await (await db.execute(
                "SELECT status FROM telegram_update_inbox WHERE update_id=?",
                (update_id,),
            )).fetchone()
            await db.rollback()
            if (
                int(by_operation["update_id"]) != update_id
                or by_operation["request_hash"] != request_hash
            ):
                return _json({"error": "operation_conflict"}, status=409)
            return _json({
                "ok": True, "update_id": update_id,
                "status": current["status"] if current else by_operation["result_status"],
                "idempotent": True,
            })
        item = await (await db.execute(
            "SELECT status,payload_json FROM telegram_update_inbox WHERE update_id=?",
            (update_id,),
        )).fetchone()
        if not item:
            await db.rollback()
            return _json({"error": "not_found"}, status=404)
        if item["status"] != "dead":
            await db.rollback()
            return _json({"error": "not_dead", "status": item["status"]}, status=409)
        if not item["payload_json"]:
            await db.rollback()
            return _json({"error": "payload_expired"}, status=410)
        stamp = now_iso()
        await db.execute(
            "INSERT INTO telegram_update_redrive_commands "
            "(operation_id,request_hash,update_id,admin_id,reason,result_status,created_at) "
            "VALUES (?,?,?,?,?,'pending',?)",
            (operation_id, request_hash, update_id, int(admin_id), reason, stamp),
        )
        await db.execute(
            "UPDATE telegram_update_inbox SET status='pending',attempts=0,"
            "available_at=?,last_error=NULL,locked_by=NULL,locked_at=NULL,dead_at=NULL,"
            "redrive_operation_id=?,redrive_request_hash=?,redrive_reason=?,"
            "redriven_by=?,redriven_at=? WHERE update_id=? AND status='dead'",
            (
                stamp, operation_id, request_hash, reason,
                int(admin_id), stamp, update_id,
            ),
        )
        await db.commit()
    return _json({"ok": True, "update_id": update_id, "status": "pending"})


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


async def serve_privacy(request):
    """Render the versioned same-origin privacy notice without active content."""
    if not os.path.isfile(PRIVACY_TEMPLATE_PATH):
        return web.Response(text="Политика обработки данных не найдена", status=404)
    with open(PRIVACY_TEMPLATE_PATH, encoding="utf-8") as source:
        document = source.read()
    replacements = {
        "{{CONTROLLER_NAME}}": html.escape(
            PRIVACY_CONTROLLER_NAME or "Оператор пилота БибиЗадачи"
        ),
        "{{PRIVACY_CONTACT}}": html.escape(
            PRIVACY_CONTACT or f"@{BOT_USERNAME}"
        ),
        "{{EVIDENCE_RETENTION_DAYS}}": str(EVIDENCE_RETENTION_DAYS),
        "{{DISPUTE_OPEN_DAYS}}": str(DISPUTE_OPEN_DAYS),
        "{{WITHDRAW_RETENTION_DAYS}}": str(WITHDRAW_ACCOUNT_RETENTION_DAYS),
    }
    for marker, value in replacements.items():
        document = document.replace(marker, value)
    return web.Response(
        text=document,
        content_type="text/html",
        charset="utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def serve_logo(request):
    if os.path.exists(LOGO_PATH):
        return web.FileResponse(LOGO_PATH, headers={
            "Cache-Control": "public, max-age=86400",
        })
    return web.Response(text="logo.jpg не найден", status=404)


async def serve_task_photo(request):
    filename = request.match_info.get("filename", "")
    if (
        not filename
        or os.path.basename(filename) != filename
        or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for ch in filename.lower())
    ):
        raise web.HTTPNotFound()
    try:
        expires = int(request.rel_url.query.get("expires", "0"))
    except (TypeError, ValueError):
        raise web.HTTPForbidden()
    signature = request.rel_url.query.get("signature", "")
    if expires < int(time.time()) or expires > int(time.time()) + PHOTO_URL_TTL_SEC + 60:
        raise web.HTTPForbidden()
    message = f"{filename}:{expires}".encode("utf-8")
    expected = hmac.new(
        (MEDIA_SIGNING_KEY or BOT_TOKEN).encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise web.HTTPForbidden()
    path = os.path.join(TASK_PHOTO_DIR, filename)
    if not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


async def serve_media(request):
    media_id = request.match_info.get("media_id", "")
    try:
        uuid.UUID(media_id)
        expires = int(request.rel_url.query.get("expires", "0"))
    except (TypeError, ValueError):
        raise web.HTTPNotFound()
    if expires < int(time.time()) or expires > int(time.time()) + PHOTO_URL_TTL_SEC + 60:
        raise web.HTTPForbidden()
    signature = request.rel_url.query.get("signature", "")
    expected = hmac.new(
        (MEDIA_SIGNING_KEY or BOT_TOKEN).encode("utf-8"),
        f"media:{media_id}:{expires}".encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise web.HTTPForbidden()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        row = await (await db.execute(
            "SELECT backend,object_key,state,size_bytes,sha256 FROM media_objects WHERE id=?",
            (media_id,),
        )).fetchone()
    if not row or row[2] != "ready":
        raise web.HTTPNotFound()
    try:
        content = await _storage_read(row[1], backend=row[0])
        if len(content) != int(row[3]) or not hmac.compare_digest(
            hashlib.sha256(content).hexdigest(), str(row[4]),
        ):
            raise ValueError("checksum_mismatch")
    except Exception as exc:
        missing = _storage_error_is_missing(exc)
        corrupt = isinstance(exc, ValueError) and str(exc) == "checksum_mismatch"
        if missing or corrupt:
            async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                await db.execute(
                    "UPDATE media_objects SET state='quarantined',last_error=? "
                    "WHERE id=? AND state='ready'",
                    ("missing" if missing else "checksum_mismatch", media_id),
                )
                await db.commit()
            raise web.HTTPNotFound()
        raise web.HTTPServiceUnavailable()
    return web.Response(
        body=content, content_type="image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _process_rss_bytes():
    """Return current Linux RSS without adding a runtime dependency."""
    try:
        with open("/proc/self/status", encoding="ascii") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


async def api_health(request):
    supplied = request.headers.get("X-Health-Token", "")
    local_request = request.remote in ("127.0.0.1", "::1", None)
    authorized = bool(
        HEALTH_TOKEN and supplied
        and secrets.compare_digest(supplied, HEALTH_TOKEN)
    )
    if not authorized and (HEALTH_TOKEN or not local_request):
        raise web.HTTPUnauthorized()
    query = getattr(getattr(request, "rel_url", None), "query", {})
    refresh_requested = query.get("refresh") == "1"
    force_refresh = bool(
        refresh_requested and authorized
        and BIBITASKS_ENVIRONMENT == "staging"
        and PILOT_LOAD_TEST_ENABLED
    )
    if refresh_requested and not force_refresh:
        raise web.HTTPForbidden()
    now = time.monotonic()
    if force_refresh or now - float(_health_cache["checked_at"]) >= 30:
        # Single-flight: после истечения cache только один запрос проверяет БД,
        # остальные ждут тот же результат вместо параллельного quick_check.
        async with _health_check_lock:
            now = time.monotonic()
            if force_refresh or now - float(_health_cache["checked_at"]) >= 30:
                database_ok = False
                database_error = ""
                try:
                    async with aiosqlite.connect(DB_PATH, timeout=3) as db:
                        row = await (await db.execute("PRAGMA quick_check")).fetchone()
                        database_ok = bool(row and row[0] == "ok")
                        outbox_dead = int((await (await db.execute(
                            "SELECT COUNT(*) FROM task_outbox WHERE status='dead'"
                        )).fetchone())[0])
                        outbox_pending = int((await (await db.execute(
                            "SELECT COUNT(*) FROM task_outbox "
                            "WHERE status IN ('pending','sending')"
                        )).fetchone())[0])
                        outbox_oldest_row = await (await db.execute(
                            "SELECT MIN(created_at) FROM task_outbox "
                            "WHERE status IN ('pending','sending')"
                        )).fetchone()
                        outbox_oldest_at = str(outbox_oldest_row[0] or "")
                        inbox_dead = int((await (await db.execute(
                            "SELECT COUNT(*) FROM telegram_update_inbox WHERE status='dead'"
                        )).fetchone())[0])
                        inbox_pending = int((await (await db.execute(
                            "SELECT COUNT(*) FROM telegram_update_inbox "
                            "WHERE status IN ('pending','processing')"
                        )).fetchone())[0])
                        oldest_row = await (await db.execute(
                            "SELECT MIN(received_at) FROM telegram_update_inbox "
                            "WHERE status IN ('pending','processing')"
                        )).fetchone()
                        inbox_oldest_at = str(oldest_row[0] or "")
                        media_quarantined = int((await (await db.execute(
                            "SELECT COUNT(*) FROM media_objects WHERE state='quarantined'"
                        )).fetchone())[0])
                        uploading_before = (
                            datetime.now(timezone.utc) - timedelta(minutes=30)
                        ).isoformat()
                        media_uploading_stale = int((await (await db.execute(
                            "SELECT COUNT(*) FROM media_objects "
                            "WHERE state='uploading' AND created_at<?",
                            (uploading_before,),
                        )).fetchone())[0])
                except Exception as exc:
                    database_error = type(exc).__name__
                    outbox_dead = -1
                    outbox_pending = -1
                    outbox_oldest_at = ""
                    inbox_dead = -1
                    inbox_pending = -1
                    inbox_oldest_at = ""
                    media_quarantined = -1
                    media_uploading_stale = -1
                storage_ok = await _storage_healthcheck()
                _health_cache.update(
                    checked_at=now,
                    database_ok=database_ok,
                    database_error=database_error,
                    outbox_dead=outbox_dead,
                    outbox_pending=outbox_pending,
                    outbox_oldest_at=outbox_oldest_at,
                    inbox_dead=inbox_dead,
                    inbox_pending=inbox_pending,
                    inbox_oldest_at=inbox_oldest_at,
                    storage_ok=storage_ok,
                    media_quarantined=media_quarantined,
                    media_uploading_stale=media_uploading_stale,
                )
    database_ok = bool(_health_cache["database_ok"])
    database_error = str(_health_cache["database_error"])
    outbox_dead = int(_health_cache["outbox_dead"])
    outbox_pending = int(_health_cache["outbox_pending"])
    outbox_oldest_at = str(_health_cache["outbox_oldest_at"])
    inbox_dead = int(_health_cache["inbox_dead"])
    inbox_pending = int(_health_cache["inbox_pending"])
    inbox_oldest_at = str(_health_cache["inbox_oldest_at"])
    def queue_oldest_is_stale(value):
        if not value:
            return False
        try:
            oldest = datetime.fromisoformat(value)
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            return (
                datetime.now(timezone.utc) - oldest
            ).total_seconds() > TELEGRAM_QUEUE_OLDEST_SOFT_SEC
        except ValueError:
            return True
    inbox_stale = queue_oldest_is_stale(inbox_oldest_at)
    outbox_stale = queue_oldest_is_stale(outbox_oldest_at)
    inbox_backlogged = bool(
        inbox_pending >= TELEGRAM_INBOX_SOFT_LIMIT or inbox_stale
    )
    outbox_backlogged = bool(
        outbox_pending >= TELEGRAM_OUTBOX_SOFT_LIMIT or outbox_stale
    )
    storage_ok = bool(_health_cache["storage_ok"])
    media_quarantined = int(_health_cache["media_quarantined"])
    media_uploading_stale = int(_health_cache["media_uploading_stale"])
    workers_ok = _worker_alive("lifecycle") and _worker_alive("outbox")
    workers_ok = workers_ok and _worker_alive("telegram_inbox")
    receiver_ready = bool(_telegram_runtime["receiver_ready"])
    inbox_crypto_ok = TELEGRAM_INBOX_FERNET is not None or inbox_pending == 0
    healthy = bool(
        database_ok and storage_ok and os.path.exists(INDEX_PATH)
        and os.path.exists(PRIVACY_TEMPLATE_PATH)
        and os.path.exists(LOGO_PATH) and BOT_TOKEN and WITHDRAW_FERNET is not None
        and outbox_dead == 0 and not inbox_backlogged and not outbox_backlogged
        and inbox_crypto_ok
        and media_quarantined == 0 and media_uploading_stale == 0
        and receiver_ready and workers_ok
    )
    return _json({
        "ok": healthy, "version": BUILD_VERSION,
        "application_version": APP_VERSION,
        "environment": BIBITASKS_ENVIRONMENT,
        "pilot_load_test_enabled": PILOT_LOAD_TEST_ENABLED,
        "pilot_load_test_telegram_stub_enabled": (
            PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED
        ),
        "process_rss_bytes": _process_rss_bytes(),
        "report_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "index_html": os.path.exists(INDEX_PATH),
        "privacy_html": os.path.exists(PRIVACY_TEMPLATE_PATH),
        "logo": os.path.exists(LOGO_PATH),
        "token_present": bool(BOT_TOKEN), "port": WEBAPP_PORT,
        "database": database_ok,
        "storage_writable": storage_ok,
        "media_backend": MEDIA_STORAGE,
        "media_quarantined": media_quarantined,
        "media_uploading_stale": media_uploading_stale,
        "database_error": database_error,
        "database_locked_errors": _runtime_errors["database_locked"],
        "withdrawal_encryption_ready": WITHDRAW_FERNET is not None,
        "telegram_inbox_encryption_ready": TELEGRAM_INBOX_FERNET is not None,
        "outbox_dead": outbox_dead,
        "outbox_pending": outbox_pending,
        "outbox_oldest_at": outbox_oldest_at,
        "outbox_stale": outbox_stale,
        "outbox_backlogged": outbox_backlogged,
        "outbox_soft_limit": TELEGRAM_OUTBOX_SOFT_LIMIT,
        "telegram_update_mode": TELEGRAM_UPDATE_MODE,
        "telegram_receiver_ready": receiver_ready,
        "webhook_configured": bool(_telegram_runtime["webhook_configured"]),
        "webhook_pending_updates": _telegram_runtime["pending_update_count"],
        "webhook_last_error": _telegram_runtime["last_error"],
        "webhook_overload_rejected": _telegram_runtime["overload_rejected"],
        "telegram_checked_at": _telegram_runtime["checked_at"],
        "telegram_inbox_pending": inbox_pending,
        "telegram_inbox_dead": inbox_dead,
        "telegram_inbox_oldest_at": inbox_oldest_at,
        "telegram_inbox_stale": inbox_stale,
        "telegram_inbox_backlogged": inbox_backlogged,
        "telegram_inbox_soft_limit": TELEGRAM_INBOX_SOFT_LIMIT,
        "telegram_inbox_hard_limit": TELEGRAM_INBOX_HARD_LIMIT,
        "api_capacity": dict(_api_capacity),
        "media_processing_capacity": dict(_media_capacity),
        "lifecycle_worker_alive": _worker_alive("lifecycle"),
        "outbox_worker_alive": _worker_alive("outbox"),
        "telegram_inbox_worker_alive": _worker_alive("telegram_inbox"),
    }, status=200 if healthy else 503)


async def api_live(request):
    """Process liveness only; readiness (DB/storage/Telegram) is exposed by /health."""
    return _json({
        "ok": True, "version": BUILD_VERSION,
        "application_version": APP_VERSION,
        "report_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "telegram_update_mode": TELEGRAM_UPDATE_MODE,
    })


async def _options(request):
    return _json({"ok": True})


@web.middleware
async def error_middleware(request, handler):
    """Любой сбой отдаём как JSON, иначе фронт показывает пустую «Ошибку»."""
    try:
        return await handler(request)
    except MediaProcessingBusy:
        response = _json({
            "error": "media_processing_busy",
            "message": (
                "Сейчас одновременно обрабатывается много фотографий. "
                "Подожди несколько секунд и повтори отправку — отчёт не изменяй."
            ),
        }, status=503)
        response.headers["Retry-After"] = str(
            MEDIA_NORMALIZE_WAIT_TIMEOUT_SEC
        )
        return response
    except web.HTTPException:
        raise
    except Exception as exc:
        _record_runtime_error(exc)
        safe_path = request.path
        if safe_path.startswith("/telegram/webhook/"):
            safe_path = "/telegram/webhook/[redacted]"
        elif safe_path.startswith("/task-photo/"):
            safe_path = "/task-photo/[redacted]"
        elif safe_path.startswith("/media/"):
            safe_path = "/media/[redacted]"
        logger.exception("Сбой в %s %s", request.method, safe_path)
        return _json({
            "error": "server",
            "message": "Что-то сломалось на сервере. Попробуй ещё раз.",
        }, status=500)


@web.middleware
async def rate_limit_middleware(request, handler):
    """Пилотный per-session лимит; production перенесёт его на gateway."""
    if not request.path.startswith("/api/") or request.method == "OPTIONS":
        return await handler(request)
    global _api_rate_requests
    # A valid Telegram signature gives every person an independent bucket even
    # behind Caddy. Invalid/random Authorization values remain in one small
    # proxy/IP bucket and therefore cannot bypass the limiter.
    context = _auth_context(request)
    if context:
        user_id = str(int(context["user"]["id"]))
        identity_key = hmac.new(
            hashlib.sha256((BOT_TOKEN or "").encode("utf-8")).digest(),
            user_id.encode("ascii"), hashlib.sha256,
        ).hexdigest()[:24]
        identity = f"u:{identity_key}"
    else:
        identity = f"ip:{request.remote or 'unknown'}"
    identity += ":r" if request.method == "GET" else ":w"
    limit = API_WRITES_PER_MIN if request.method != "GET" else API_READS_PER_MIN
    now = time.monotonic()
    window, count = _api_rate_buckets.get(identity, (now, 0))
    if now - window >= 60:
        window, count = now, 0
    count += 1
    _api_rate_buckets[identity] = (window, count)
    _api_rate_requests += 1
    if _api_rate_requests % 256 == 0:
        cutoff = now - 120
        for key, value in list(_api_rate_buckets.items()):
            if value[0] < cutoff:
                _api_rate_buckets.pop(key, None)
        while len(_api_rate_buckets) > 10_000:
            _api_rate_buckets.pop(next(iter(_api_rate_buckets)))
    if count > limit:
        response = _json({
            "error": "rate_limit",
            "message": "Слишком много запросов. Подожди минуту и попробуй снова.",
        }, status=429)
        response.headers["Retry-After"] = str(max(1, round(60 - (now - window))))
        return response
    return await handler(request)


_HEAVY_API_PATHS = frozenset({
    "/api/tasks/complete",
    "/api/admin/task/create",
    "/api/admin/task-templates",
})
_TASK_TEMPLATE_VERSION_WRITE_RE = re.compile(
    r"^/api/admin/task-templates/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/versions$"
)


def _is_heavy_api_request(request):
    return request.method not in {"GET", "HEAD", "OPTIONS"} and (
        request.path in _HEAVY_API_PATHS
        or bool(_TASK_TEMPLATE_VERSION_WRITE_RE.fullmatch(request.path))
    )


def _capacity_response(kind):
    _api_capacity[f"rejected_{kind}"] += 1
    response = _json({
        "error": "server_busy",
        "message": (
            "Сейчас приложением одновременно пользуется много людей. "
            "Подожди несколько секунд и повтори запрос."
        ),
    }, status=503)
    response.headers["Retry-After"] = "3"
    return response


@web.middleware
async def capacity_middleware(request, handler):
    """Fail fast before handlers parse JSON; retain per-user rate limiting."""
    if not request.path.startswith("/api/") or request.method == "OPTIONS":
        return await handler(request)
    read = request.method in {"GET", "HEAD"}
    lane = "reads" if read else "writes"
    limit = API_READ_INFLIGHT_MAX if read else API_WRITE_INFLIGHT_MAX
    if _api_capacity[f"active_{lane}"] >= limit:
        return _capacity_response(lane)
    heavy = _is_heavy_api_request(request)
    if heavy and _api_capacity["active_heavy"] >= API_HEAVY_INFLIGHT_MAX:
        return _capacity_response("heavy")
    # No await occurs between checking and reserving. aiohttp runs one event
    # loop per pilot process, so the counters form a fail-fast admission gate
    # rather than an unbounded semaphore waiter queue.
    _api_capacity[f"active_{lane}"] += 1
    if heavy:
        _api_capacity["active_heavy"] += 1
    try:
        return await handler(request)
    finally:
        _api_capacity[f"active_{lane}"] -= 1
        if heavy:
            _api_capacity["active_heavy"] -= 1


async def start_api_server():
    runner = None
    try:
        app = web.Application(
            middlewares=[
                error_middleware, rate_limit_middleware, capacity_middleware,
            ],
            client_max_size=16 * 1024 * 1024,
        )
        app.router.add_route("OPTIONS", "/{tail:.*}", _options)
        app.router.add_get("/api/state", api_state)
        app.router.add_post("/api/apply", api_apply)
        app.router.add_post("/api/profile/city", api_profile_city)
        app.router.add_get("/api/tasks/available", api_tasks_available)
        app.router.add_get("/api/tasks/context", api_task_context)
        app.router.add_post("/api/tasks/claim", api_task_claim)
        app.router.add_post("/api/tasks/release", api_task_release)
        app.router.add_post("/api/tasks/complete", api_task_complete)
        app.router.add_get("/api/wallet", api_wallet)
        app.router.add_post("/api/withdraw/request", api_withdraw_request)
        app.router.add_post("/api/admin/login", api_admin_login)
        app.router.add_get("/api/admin/access", api_admin_access_get)
        app.router.add_post("/api/admin/access", api_admin_access_post)
        app.router.add_get("/api/admin/overview", api_admin_overview)
        app.router.add_get("/api/admin/queue", api_admin_queue)
        app.router.add_get("/api/admin/members", api_admin_members)
        app.router.add_get(
            "/api/admin/member/tags", api_admin_member_tags_catalog,
        )
        app.router.add_post(
            "/api/admin/member/city", api_admin_member_city_decide,
        )
        app.router.add_post("/api/admin/decide", api_admin_decide)
        app.router.add_post("/api/admin/task/create", api_admin_task_create)
        app.router.add_get(
            "/api/admin/task-templates", api_admin_task_templates_list,
        )
        app.router.add_get(
            "/api/admin/task-templates/{template_id}", api_admin_task_template_get,
        )
        app.router.add_post(
            "/api/admin/task-templates", api_admin_task_template_create,
        )
        app.router.add_post(
            "/api/admin/task-templates/{template_id}/versions",
            api_admin_task_template_version_create,
        )
        app.router.add_post(
            "/api/admin/task-templates/{template_id}/status",
            api_admin_task_template_status,
        )
        app.router.add_post(
            "/api/admin/task/announcement/retry",
            api_admin_task_announcement_retry,
        )
        app.router.add_post(
            "/api/admin/join-request/retry",
            api_admin_join_request_retry,
        )
        app.router.add_get(
            "/api/admin/task/announcement/status",
            api_admin_task_announcement_status,
        )
        app.router.add_post("/api/admin/task/cancel", api_admin_task_cancel)
        app.router.add_post("/api/admin/task/approve", api_admin_task_approve)
        app.router.add_post("/api/admin/task/dispute", api_admin_task_dispute)
        app.router.add_post("/api/admin/grant", api_admin_grant)
        app.router.add_post(
            "/api/admin/grant/reversal", api_admin_grant_reversal,
        )
        app.router.add_post("/api/admin/withdraw/account", api_admin_withdraw_account)
        app.router.add_post("/api/admin/withdraw/handoff", api_admin_withdraw_handoff)
        app.router.add_post("/api/admin/withdraw/decide", api_admin_withdraw_decide)
        app.router.add_post(
            "/api/admin/telegram-inbox/redrive", api_admin_telegram_inbox_redrive,
        )
        app.router.add_post("/api/referral/verify", api_referral_verify)
        app.router.add_post("/api/admin/role", api_admin_set_role)
        app.router.add_post("/api/admin/member/tags", api_admin_member_tags)
        app.router.add_get("/api/awards", api_awards)
        app.router.add_post("/api/admin/award/save", api_admin_award_save)
        app.router.add_post("/api/admin/award/grant", api_admin_award_grant)
        app.router.add_post(
            "/api/admin/award/reversal", api_admin_award_reversal,
        )
        app.router.add_post("/api/admin/award/revoke", api_admin_award_revoke)
        app.router.add_get("/health", api_health)
        app.router.add_get("/health/ready", api_health)
        app.router.add_get("/live", api_live)
        app.router.add_get("/health/live", api_live)
        app.router.add_get("/logo.jpg", serve_logo)
        app.router.add_get("/privacy", serve_privacy)
        app.router.add_get("/task-photo/{filename}", serve_task_photo)
        app.router.add_get("/media/{media_id}", serve_media)
        app.router.add_get("/index.html", serve_index)
        app.router.add_get("/", serve_index)
        if TELEGRAM_UPDATE_MODE == "webhook":
            app.router.add_post(WEBHOOK_PATH, telegram_webhook_handler)
        # WEBHOOK_ROUTE_ID and signed media query strings must never reach access logs.
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT)
        await site.start()
        logger.info(f"API БибиЗадачи слушает 0.0.0.0:{WEBAPP_PORT}")
        return runner
    except Exception:
        logger.exception("API не запустился; останавливаем процесс, чтобы мониторинг перезапустил его.")
        if runner is not None:
            await runner.cleanup()
        raise


# ============================================================
# БОТ: приветствие и текстовые сообщения
# ============================================================
dp = Dispatcher()


@dp.update.outer_middleware()
async def telegram_update_context(handler, event, data):
    """Expose update_id to stateful handlers so replayed DB effects can deduplicate."""
    token = _current_update_id.set(event.update_id)
    try:
        return await handler(event, data)
    finally:
        _current_update_id.reset(token)


def _app_url(start_param=None):
    if BOT_USERNAME:
        url = f"https://t.me/{BOT_USERNAME}/{WEBAPP_SHORTNAME}"
        if start_param:
            safe = "".join(
                char for char in str(start_param)
                if char.isalnum() or char in "_-"
            )[:64]
            if safe:
                url += f"?startapp={safe}"
        return url
    return None


def _open_app_kb(start_param=None):
    url = _app_url(start_param)
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚲 Открыть задания", url=url)
    ]])


WELCOME = (
    "Привет! Это <b>БибиЗадачи от Бибибайка</b> 🚲\n\n"
    "Выбирай задания в своём городе, прикладывай фото результата и получай "
    "бибибонусы на поездки. <b>1 бонус = 1 ₽.</b>\n\n"
    "Открой приложение и заполни короткую заявку."
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
        rows.append([InlineKeyboardButton(text="📣 Вступить в сообщество", url=url)])
    rows.append([InlineKeyboardButton(
        text="✅ Я подписался", callback_data="ref_check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _welcome_kb():
    """Два очевидных маршрута первого входа: Mini App и сообщество."""
    rows = []
    app_url = _app_url()
    if app_url:
        rows.append([InlineKeyboardButton(text="🚲 Открыть задания", url=app_url)])
    community_url = _required_chat_url()
    if community_url:
        rows.append([InlineKeyboardButton(
            text="📣 Вступить в сообщество", url=community_url,
        )])
    rows.append([InlineKeyboardButton(
        text="📖 Как выполнять задания", callback_data="how_tasks",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


SUBSCRIBE_PROMPT = (
    "Ты пришёл по ссылке друга 👋\n\n"
    "Чтобы приглашение засчиталось, вступи в сообщество Бибибайка и подай "
    "заявку. После одобрения заявки другу добавится реферальный прогресс, "
    "а тебе откроются задания.\n\n"
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
        if member["status"] == "approved":
            await confirm_referral(uid)
            await send("Спасибо, подписка на месте — другу засчитано приглашение ✅")
            return True
        await send(
            "Спасибо, подписка на месте ✅ Приглашение засчитается другу после одобрения твоей заявки."
        )
        return False
    if subscribed is False:
        await send(SUBSCRIBE_PROMPT, _subscribe_kb())
    # subscribed is None — канал не настроен или бот не админ в нём.
    # Молчим: реферал засчитается по старому правилу, при одобрении заявки.
    return False


@dp.message(CommandStart(deep_link=True))
async def start_ref(message: Message, command=None):
    """Старт только по непрозрачной ссылке rf_<token>."""
    uid = message.from_user.id
    await _track_event_best_effort(
        "bot_started", "bot", user_id=uid,
        properties={"entrypoint": "referral"},
        dedupe_key=f"bot_start:{message.chat.id}:{message.message_id}",
    )
    payload = ""
    try:
        payload = (command.args or "") if command else ""
    except Exception:
        payload = ""
    m = await get_member(uid)
    if not m:
        await upsert_member(
            uid,
            full_name=(message.from_user.full_name or ""),
            username=(message.from_user.username or ""))
    bound = await _bind_referral_token(uid, payload)
    member_after_bind = await get_member(uid)
    await _greet(message)
    if (
        not bound
        and (payload.startswith("rf_") or payload.startswith("ref_"))
        and not (member_after_bind and member_after_bind["referred_by"])
    ):
        await _track_event_best_effort(
            "referral_link_invalid", "bot", user_id=uid,
            properties={
                "referral_format": "legacy" if payload.startswith("ref_") else "opaque"
            },
            dedupe_key=f"referral_invalid:{uid}:{message.message_id}",
        )
        await message.answer(
            "Эта ссылка-приглашение устарела или недействительна. "
            "Попроси друга открыть приложение и отправить тебе новую ссылку."
        )
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
    if member["status"] == "approved":
        await confirm_referral(uid)
        answer = "Готово! Другу засчитано приглашение ✅"
    else:
        answer = "Подписка есть ✅ Приглашение засчитается после одобрения заявки."
    await call.answer(answer, show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=_open_app_kb())
    except Exception:
        pass


@dp.message(CommandStart())
async def start_plain(message: Message):
    uid = message.from_user.id
    await _track_event_best_effort(
        "bot_started", "bot", user_id=uid,
        properties={"entrypoint": "direct"},
        dedupe_key=f"bot_start:{message.chat.id}:{message.message_id}",
    )
    if not await get_member(uid):
        await upsert_member(
            uid,
            full_name=(message.from_user.full_name or ""),
            username=(message.from_user.username or ""))
    await _greet(message)


async def _greet(message: Message):
    m = await get_member(message.from_user.id)
    kb = _welcome_kb()
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
        f"{level[2]} <b>{_html(user.full_name)}</b> — {level[1]}",
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
            "Уровень показывает опыт участника и помогает ответственным видеть, "
            "кто давно и регулярно помогает."
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
            "Если вы пришли по приглашению друга — вступите в группу. "
            "Другу засчитается приглашение. Чтобы получить доступ к заданиям, "
            "откройте приложение, заполните заявку и дождитесь одобрения.\n\n"
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


async def _send_photo_to_topic(
    chat_id, filename, caption, topic, *, media_id=None, **kw,
):
    """Отправляет исходное фото задания в тему с тем же fallback."""
    if media_id:
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            row = await (await db.execute(
                "SELECT object_key,state,backend FROM media_objects WHERE id=?", (media_id,),
            )).fetchone()
        if not row or row[1] != "ready":
            raise ValueError("Media object is not ready")
        filename = row[0]
        backend = row[2]
    else:
        backend = None
    content = await _storage_read(filename, backend=backend)
    photo = BufferedInputFile(content, filename=filename)
    try:
        return await bot.send_photo(
            chat_id, photo, caption=caption, **_thread_kwargs(topic), **kw)
    except Exception as exc:
        if "thread not found" not in str(exc).lower() or not topic:
            raise
        logger.info("Подтема %s не найдена, отправляю фото без неё", topic)
        return await bot.send_photo(
            chat_id, BufferedInputFile(content, filename=filename), caption=caption, **kw,
        )


async def _get_published(kind):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            "SELECT * FROM published_posts WHERE kind=?", (kind,))).fetchone()


async def _remember_published(kind, chat_id, topic, ids, by, operation_id):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        previous = await (await db.execute(
            "SELECT chat_id,message_ids,operation_id FROM published_posts WHERE kind=?",
            (kind,),
        )).fetchone()
        await db.execute(
            "INSERT INTO published_posts "
            "(kind, chat_id, topic, message_ids, published_at, published_by,operation_id) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(kind) DO UPDATE SET chat_id=excluded.chat_id, "
            "topic=excluded.topic, message_ids=excluded.message_ids, "
            "published_at=excluded.published_at, "
            "published_by=excluded.published_by,operation_id=excluded.operation_id",
            (kind, chat_id, topic, json.dumps(ids), now_iso(), by, operation_id))
        cleanup_ids = []
        if previous and previous["operation_id"] != operation_id:
            try:
                cleanup_ids = [int(value) for value in json.loads(previous["message_ids"])]
            except (TypeError, ValueError, json.JSONDecodeError):
                cleanup_ids = []
        for message_id in cleanup_ids:
            await db.execute(
                "INSERT OR IGNORE INTO publication_cleanup_messages "
                "(operation_id,chat_id,message_id,final_job_status,status) "
                "VALUES (?,?,?,'done','pending')",
                (operation_id, str(previous["chat_id"]), message_id),
            )
        next_status = "cleanup_pending" if cleanup_ids else "done"
        await db.execute(
            "UPDATE publication_jobs SET status=?,completed_at=? "
            "WHERE kind=? AND operation_id=?",
            (next_status, now_iso() if next_status == "done" else None, kind, operation_id),
        )
        await db.commit()
    return bool(cleanup_ids)


async def _run_publication_cleanup(operation_id):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM publication_cleanup_messages "
            "WHERE operation_id=? AND status='pending' ORDER BY message_id",
            (operation_id,),
        )).fetchall()
    for row in rows:
        chat_id = row["chat_id"]
        if str(chat_id).lstrip("-").isdigit():
            chat_id = int(chat_id)
        try:
            await bot.delete_message(chat_id, int(row["message_id"]))
        except Exception as exc:
            text_value = str(exc).lower()
            if "message to delete not found" not in text_value:
                attempts = int(row["attempts"] or 0) + 1
                terminal = attempts >= PUBLICATION_CLEANUP_MAX_ATTEMPTS
                async with aiosqlite.connect(DB_PATH, timeout=15) as db:
                    await db.execute(
                        "UPDATE publication_cleanup_messages SET attempts=?,status=?,"
                        "last_error=? WHERE operation_id=? AND chat_id=? AND message_id=?",
                        (
                            attempts, "failed" if terminal else "pending",
                            type(exc).__name__, operation_id,
                            row["chat_id"], row["message_id"],
                        ),
                    )
                    await db.commit()
                if not terminal:
                    raise
                logger.error(
                    "Publication cleanup needs manual attention: operation=%s",
                    operation_id,
                )
                continue
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            await db.execute(
                "UPDATE publication_cleanup_messages SET status='deleted',deleted_at=?,"
                "last_error=NULL WHERE operation_id=? AND chat_id=? AND message_id=?",
                (now_iso(), operation_id, row["chat_id"], row["message_id"]),
            )
            await db.commit()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        remaining = int((await (await db.execute(
            "SELECT COUNT(*) FROM publication_cleanup_messages "
            "WHERE operation_id=? AND status='pending'",
            (operation_id,),
        )).fetchone())[0])
        failed = int((await (await db.execute(
            "SELECT COUNT(*) FROM publication_cleanup_messages "
            "WHERE operation_id=? AND status='failed'",
            (operation_id,),
        )).fetchone())[0])
        final_rows = await (await db.execute(
            "SELECT DISTINCT final_job_status FROM publication_cleanup_messages "
            "WHERE operation_id=?",
            (operation_id,),
        )).fetchall()
        if remaining == 0 and final_rows:
            final_status = (
                "cleanup_failed" if failed or len(final_rows) != 1
                else final_rows[0][0]
            )
            await db.execute(
                "UPDATE publication_jobs SET status=?,completed_at=? "
                "WHERE operation_id=?",
                (final_status, now_iso(), operation_id),
            )
            if final_status == "cleanup_failed":
                await _enqueue_capability_holders_in_tx(
                    db, f"publication-cleanup-failed:{operation_id}",
                    "⚠️ Не удалось удалить старое сообщение после 10 попыток. "
                    "Новая публикация сохранена; старое сообщение нужно удалить вручную. "
                    f"Операция: {operation_id}",
                    "telegram.publication.manage",
                )
        await db.commit()


async def _reconcile_publication_cleanups():
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        rows = await (await db.execute(
            "SELECT operation_id FROM publication_jobs "
            "WHERE status IN ('cleanup_pending','failed_cleanup_pending') LIMIT 20"
        )).fetchall()
    for row in rows:
        try:
            await _run_publication_cleanup(row[0])
        except Exception:
            logger.warning("Publication cleanup retry failed")


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
    if not await _has_command_capability(
        message.from_user.id, "telegram.publication.manage",
    ):
        return
    target = message.chat.id
    if message.chat.type == "private":
        if not GROUP_ID and not GROUP_USERNAME:
            await message.answer("Группа не настроена: задай GROUP_USERNAME.")
            return
        target = GROUP_ID or f"@{GROUP_USERNAME}"

    repost = _wants_repost(message)
    update_id = _current_update_id.get()
    source_key = (
        f"update:{int(update_id)}" if update_id is not None
        else f"message:{int(message.chat.id)}:{int(message.message_id)}"
    )
    event_key = f"{source_key}:publication:{kind}"
    outcome = "queued"
    old = None
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        job = await (await db.execute(
            "SELECT * FROM publication_jobs WHERE kind=?", (kind,),
        )).fetchone()
        old = await (await db.execute(
            "SELECT * FROM published_posts WHERE kind=?", (kind,),
        )).fetchone()
        if job and job["operation_id"] == event_key:
            if job["status"] in ("done", "cleanup_pending", "cleanup_failed") and old:
                outcome = "already_published"
            elif job["status"] in ("failed", "failed_cleanup_pending"):
                outcome = "already_failed"
            else:
                outcome = "already_queued"
            await db.rollback()
        elif job and job["status"] in (
            "pending", "sending", "cleanup_pending", "failed_cleanup_pending",
        ):
            outcome = "already_queued"
            await db.rollback()
        elif old and not repost:
            outcome = "already_published"
            await db.rollback()
        else:
            await db.execute(
                "INSERT INTO publication_jobs "
                "(kind,operation_id,status,requested_by,created_at,completed_at) "
                "VALUES (?,?,'pending',?,?,NULL) "
                "ON CONFLICT(kind) DO UPDATE SET operation_id=excluded.operation_id,"
                "status='pending',requested_by=excluded.requested_by,"
                "created_at=excluded.created_at,completed_at=NULL",
                (kind, event_key, int(message.from_user.id), now_iso()),
            )
            await _enqueue_outbox_in_tx(
                db, event_key, "group_publication",
                {
                    "target": target, "topic": topic, "kind": kind,
                    "parts": list(parts), "admin_id": int(message.from_user.id),
                    "repost": bool(repost),
                },
                chat_id=target, topic_id=topic,
            )
            await db.commit()
    if outcome == "already_queued":
        await message.answer("Публикация уже стоит в очереди — дождись результата.")
        return None
    if outcome == "already_failed":
        await message.answer(
            "Публикация не доставилась после повторов. Исправь причину и отправь новую команду."
        )
        return None
    if outcome == "already_published":
        link = _post_link(old)
        when = (old["published_at"] or "")[:16].replace("T", " ")
        await message.answer(
            f"Этот пост уже опубликован{f' — {when}' if when else ''}."
            + (f"\n{link}" if link else "")
            + "\n\nЧтобы заменить его новым, напиши команду со словом "
              "<b>заново</b> — старый пост я удалю.",
            parse_mode="HTML", disable_web_page_preview=True)
        return None
    await message.answer(
        ("Переопубликация поставлена в очередь ✅" if repost
         else "Публикация поставлена в очередь ✅")
        + f" Сообщений: {len(parts)}"
    )
    return None


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
WELCOME_JOIN = (
    "{name}, привет! 👋 Это сообщество Бибибайка. "
    "Задания и бибибонусы — по кнопке ниже."
)


def _join_request_invite_snapshot(request):
    invite = getattr(request, "invite_link", None)
    raw_link = str(getattr(invite, "invite_link", "") or "")
    link_sha256 = hashlib.sha256(raw_link.encode("utf-8")).hexdigest() if raw_link else None
    creator = getattr(invite, "creator", None)
    try:
        creator_id = int(getattr(creator, "id", 0) or 0)
        expected_bot_id = int(bot.id)
    except (TypeError, ValueError, AttributeError):
        creator_id = expected_bot_id = 0
    valid = bool(
        JOIN_REQUEST_ADMISSION_ENABLED
        and JOIN_REQUEST_INVITE_URL
        and raw_link
        and hmac.compare_digest(raw_link, JOIN_REQUEST_INVITE_URL)
        and getattr(invite, "creates_join_request", False) is True
        and not bool(getattr(invite, "is_revoked", False))
        and creator_id and creator_id == expected_bot_id
    )
    return ("bot_invite" if valid else "unverified"), link_sha256


@dp.chat_join_request()
async def handle_chat_join_request(request: ChatJoinRequest):
    """Persist a managed request; approval/decline is delivered by the outbox."""
    if not _is_our_group(request):
        return
    user = request.from_user
    if getattr(user, "is_bot", False):
        return
    await _ensure_member(user)
    occurred = getattr(request, "date", None)
    if occurred is None:
        occurred = datetime.now(timezone.utc)
    elif occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    requested_at = occurred.astimezone(timezone.utc).isoformat()
    source, link_sha256 = _join_request_invite_snapshot(request)
    update_id = _current_update_id.get()
    request_material = (
        f"{int(request.chat.id)}:{int(user.id)}:{requested_at}:"
        f"{link_sha256 or 'no-link'}"
    )
    request_key = hashlib.sha256(request_material.encode("utf-8")).hexdigest()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        inserted = await db.execute(
            "INSERT OR IGNORE INTO telegram_join_requests "
            "(request_key,update_id,chat_id,user_id,invite_link_sha256,source,status,"
            "requested_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                request_key, int(update_id) if update_id is not None else None,
                str(request.chat.id), int(user.id), link_sha256, source,
                "manual_required" if source != "bot_invite" else "awaiting_application",
                requested_at,
            ),
        )
        if inserted.rowcount != 1:
            await db.rollback()
            return
        member = await (await db.execute(
            "SELECT status,role,applied_at FROM members WHERE user_id=?",
            (int(user.id),),
        )).fetchone()
        status = member["status"] if member else "pending"
        if source != "bot_invite":
            await _enqueue_capability_holders_in_tx(
                db, f"join_request:{request_key}:unverified",
                "⚠️ Получена заявка в сообщество не через управляемую ссылку. "
                "Автоматическое решение заблокировано; проверь настройки входа.",
                "admission.view",
            )
        elif status == "approved":
            await _queue_join_request_decision_in_tx(db, request_key, "approve")
        elif status == "blocked":
            await _queue_join_request_decision_in_tx(db, request_key, "decline")
        else:
            applied = bool(member and (member["applied_at"] or member["role"] == "applicant"))
            await db.execute(
                "UPDATE telegram_join_requests SET status=? WHERE request_key=?",
                ("awaiting_review" if applied else "awaiting_application", request_key),
            )
            user_chat_id = int(getattr(request, "user_chat_id", 0) or 0)
            if user_chat_id:
                await _enqueue_outbox_in_tx(
                    db, f"join_request:{request_key}:participant", "direct",
                    {
                        "text": (
                            "Заявка в сообщество получена. Анкета уже на проверке."
                            if applied else
                            "Заявка в сообщество получена. Открой БибиЗадачи и заполни короткую анкету."
                        ),
                        "start": None,
                    },
                    recipient_id=user_chat_id,
                )
            await _enqueue_capability_holders_in_tx(
                db, f"join_request:{request_key}:admins",
                "Новая заявка на вступление в сообщество. "
                + ("Анкета уже ожидает проверки." if applied else "Анкета ещё не заполнена."),
                "admission.view",
            )
        await _track_event_in_tx(
            db, "group_join_requested", "group", user_id=int(user.id),
            outcome=source,
            dedupe_key=f"group_join_request:{request_key}",
        )
        await db.commit()


@dp.chat_member()
async def track_group_membership(update: ChatMemberUpdated):
    """Авторитетный переход членства; приветствие остаётся отдельным UX-событием."""
    if not _is_our_group(update):
        return
    old_active = _chat_membership_is_active(update.old_chat_member)
    new_active = _chat_membership_is_active(update.new_chat_member)
    joined = not old_active and new_active
    left = old_active and not new_active
    if (not joined and not left) or getattr(update.new_chat_member.user, "is_bot", False):
        return
    user = update.new_chat_member.user
    await _ensure_member(user)
    occurred = getattr(update, "date", None)
    if occurred is None:
        occurred = datetime.now(timezone.utc)
    elif occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    occurred_iso = occurred.astimezone(timezone.utc).isoformat()
    referrer_id = None
    referral_count = rewarded_total = 0
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if joined:
            await db.execute(
                "UPDATE members SET group_membership_status='member',"
                "group_joined_at=?,group_left_at=NULL WHERE user_id=?",
                (occurred_iso, int(user.id)),
            )
            await db.execute(
                "UPDATE telegram_join_requests SET status='joined',joined_at=?,"
                "last_error=NULL WHERE request_key=(SELECT request_key FROM "
                "telegram_join_requests WHERE chat_id=? AND user_id=? "
                "ORDER BY requested_at DESC LIMIT 1)",
                (occurred_iso, str(update.chat.id), int(user.id)),
            )
            referrer_id, referral_count, rewarded_total = (
                await _confirm_referral_if_ready_in_tx(db, int(user.id))
            )
            if referrer_id:
                await _enqueue_outbox_in_tx(
                    db, f"referral:{int(user.id)}:confirmed:referrer:{referrer_id}",
                    "direct",
                    {"text": _referral_progress_message(referral_count, rewarded_total), "start": None},
                    recipient_id=referrer_id,
                )
        else:
            await db.execute(
                "UPDATE members SET group_membership_status='left',group_left_at=? "
                "WHERE user_id=?",
                (occurred_iso, int(user.id)),
            )
        await db.commit()
    stamp = int(occurred.timestamp())
    await _track_event_best_effort(
        "group_member_joined" if joined else "group_member_left",
        "group", user_id=user.id,
        dedupe_key=(
            f"group_join:{update.chat.id}:{user.id}:{stamp}" if joined
            else f"group_left:{update.chat.id}:{user.id}:{stamp}"
        ),
    )


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
        f"Это примерно {ride_minutes_for(bonus)} минут поездки по тарифу "
        f"{RIDE_RUB_PER_MIN:g} ₽/мин.",
        f"Выполнено заданий: {done}",
    ]
    if bonus < WITHDRAW_MIN:
        lines.append(f"До перевода не хватает {WITHDRAW_MIN - bonus}.")
    else:
        lines.append("Можно перевести в клиентское приложение — заявка в «Кошельке».")
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
    if not await _has_command_capability(
        message.from_user.id, "operations.health.view",
    ):
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


def _how_tasks_kb():
    rows = []
    work_url = _safe_https_url(_topic_link(TOPIC_WORK))
    if work_url:
        rows.append([InlineKeyboardButton(text="🛠 Рабочая тема", url=work_url)])
    app_url = _app_url()
    if app_url:
        rows.append([InlineKeyboardButton(text="🚲 Открыть задания", url=app_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@dp.message(F.text.regexp(r"(?i)^/(как|how|как_выполнять)(?:@\w+)?$"))
async def show_how_to_tasks(message: Message):
    """Короткая рабочая инструкция доступна любому пользователю."""
    await message.answer(
        WORK_INSTRUCTION,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_how_tasks_kb(),
    )


@dp.callback_query(F.data == "how_tasks")
async def show_how_to_tasks_callback(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        WORK_INSTRUCTION,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_how_tasks_kb(),
    )


@dp.message(F.text.regexp(r"(?i)^/(команды|help|старт)"))
async def show_commands(message: Message):
    text = (
        "🤖 <b>Что я умею</b>\n\n"
        "<code>/профиль</code> — уровень и бибибонусы одним сообщением\n"
        "<code>/ранг</code> — уровень доверия, опыт и прогресс\n"
        "<code>/бонусы</code> — сколько у вас бибибонусов\n"
        "<code>/задания</code> — открыть приложение\n"
        "<code>/как</code> — как выполнять задания\n"
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
    if await _has_command_capability(
        message.from_user.id, "telegram.publication.manage",
    ):
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


async def add_chat_xp(user, amount, kind, *, thanks_from_id=None):
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
        update_id = _current_update_id.get()
        if update_id is not None:
            effect_key = f"chat_xp:{kind}"
            effect = await db.execute(
                "INSERT OR IGNORE INTO telegram_update_effects "
                "(update_id,effect_key,created_at) VALUES (?,?,?)",
                (int(update_id), effect_key, now_iso()),
            )
            if effect.rowcount != 1:
                await db.rollback()
                return 0, None
        if kind == "thanks" and thanks_from_id is not None:
            pair = await (await db.execute(
                "SELECT last_at FROM thanks_pairs WHERE from_id=? AND to_id=?",
                (int(thanks_from_id), int(user.id)),
            )).fetchone()
            if pair:
                try:
                    last = datetime.fromisoformat(pair["last_at"])
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if (
                        datetime.now(timezone.utc) - last
                    ).total_seconds() < THANKS_PAIR_COOLDOWN_H * 3600:
                        await db.rollback()
                        return 0, None
                except (TypeError, ValueError):
                    pass
            await db.execute(
                "INSERT INTO thanks_pairs (from_id,to_id,last_at) VALUES (?,?,?) "
                "ON CONFLICT(from_id,to_id) DO UPDATE SET last_at=excluded.last_at",
                (int(thanks_from_id), int(user.id), now_iso()),
            )
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


async def _announce_level(message, user, level):
    """Короткое поздравление в той же подтеме."""
    try:
        await message.answer(
            f"{level[2]} <b>{_html(user.full_name)}</b> — новый уровень доверия: "
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
        if not target.is_bot and target.id != user.id:
            granted, level = await add_chat_xp(
                target, XP_PER_THANKS, "thanks", thanks_from_id=user.id,
            )
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
    "🚲 <b>Как помогать Бибибайку и получать бибибонусы</b>\n\n"
    "<b>1. Подай заявку</b>\n"
    "Открой приложение и укажи имя, город и коротко напиши, что сможешь "
    "выполнять и чем будешь полезен компании. Номер телефона не требуется. "
    "Доступ выдаётся вручную, "
    "чтобы команда не набирала больше людей, чем может обеспечить задачами.\n\n"
    "<b>2. Возьми задание</b>\n"
    "После одобрения открой вкладку «Задания». Проверь город, адрес, время и "
    "награду, затем нажми «Взять задание». Персональные задания приходят "
    "отдельным сообщением в бот.\n\n"
    "<b>3. Отправь результат</b>\n"
    "После выполнения нажми «Добавить фотоотчёт», приложи от одной до четырёх "
    "фотографий результата и при необходимости оставь комментарий. Ответственный "
    "либо подтвердит результат, либо вернёт его на доработку и напишет причину. "
    "При доработке задание останется закреплено за тобой.\n\n"
    "<b>4. Потрать бибибонусы на поездки</b>\n"
    "1 бибибонус заменяет 1 ₽ при оплате минут в приложении Бибибайк. "
    f"Минута стоит {RIDE_RUB_PER_MIN:g} ₽: 1000 бибибонусов — это примерно "
    f"{ride_minutes_for(1000)} минут поездки. Когда накопишь минимальную сумму, "
    "создай заявку в «Кошельке» — ответственный переведёт бонусы в клиентское "
    "приложение.\n\n"
    "<b>5. Уровень и приглашения</b>\n"
    "Уровень растёт за подтверждённые задания и полезное общение. Он показывает "
    "опыт участника. В «Профиле» можно пригласить друга: после вступления в "
    "сообщество и одобрения его заявки приглашение засчитается, а награда "
    "придёт на достигнутой ступени.\n\n"
    "Вопросы по работе можно задать в этой подтеме."
)


# ============================================================
# ЗАПУСК
# ============================================================
async def _refresh_telegram_runtime():
    if TELEGRAM_UPDATE_MODE != "webhook":
        _telegram_runtime.update(
            receiver_ready=True,
            webhook_configured=False,
            pending_update_count=0,
            last_error="",
            checked_at=now_iso(),
        )
        return
    try:
        info = await bot.get_webhook_info()
        last_error_date = getattr(info, "last_error_date", None)
        last_error_dt = None
        if isinstance(last_error_date, datetime):
            last_error_dt = last_error_date
            if last_error_dt.tzinfo is None:
                last_error_dt = last_error_dt.replace(tzinfo=timezone.utc)
            last_error_dt = last_error_dt.astimezone(timezone.utc)
            last_error = last_error_dt.isoformat()
        elif isinstance(last_error_date, (int, float)):
            last_error_dt = datetime.fromtimestamp(
                last_error_date, timezone.utc,
            )
            last_error = last_error_dt.isoformat()
        elif last_error_date:
            last_error = str(last_error_date)
        else:
            last_error = ""
        expected_updates = set(dp.resolve_used_update_types())
        actual_updates = set(getattr(info, "allowed_updates", None) or [])
        url_matches = hmac.compare_digest(str(info.url or ""), _webhook_url())
        connections_match = (
            int(getattr(info, "max_connections", 0) or 0)
            == WEBHOOK_MAX_CONNECTIONS
        )
        updates_match = expected_updates.issubset(actual_updates)
        async with aiosqlite.connect(DB_PATH, timeout=15) as db:
            last_received = await (await db.execute(
                "SELECT MAX(received_at) FROM telegram_update_inbox"
            )).fetchone()
        def runtime_datetime(value):
            try:
                parsed = datetime.fromisoformat(str(value)) if value else None
                if parsed and parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc) if parsed else None
            except (TypeError, ValueError):
                return None

        update_candidates = [
            runtime_datetime(last_received[0]),
            runtime_datetime(_telegram_runtime.get("last_update_at")),
        ]
        last_update_dt = max(
            (item for item in update_candidates if item is not None), default=None,
        )
        last_update_at = last_update_dt.isoformat() if last_update_dt else ""
        configured_at_dt = runtime_datetime(_telegram_runtime.get("configured_at"))
        relevant_error = bool(
            last_error_dt
            and (not configured_at_dt or last_error_dt >= configured_at_dt)
        )
        active_delivery_error = bool(
            relevant_error and (not last_update_dt or last_update_dt <= last_error_dt)
        )
        configured = bool(
            url_matches and connections_match and updates_match
            and not active_delivery_error
        )
        _telegram_runtime.update(
            receiver_ready=configured,
            webhook_configured=configured,
            pending_update_count=int(info.pending_update_count or 0),
            last_error=last_error,
            last_update_at=last_update_at,
            checked_at=now_iso(),
        )
    except Exception:
        _telegram_runtime.update(
            receiver_ready=False,
            webhook_configured=False,
            last_error="status_unavailable",
            checked_at=now_iso(),
        )
        raise


async def _configure_update_receiver():
    allowed_updates = dp.resolve_used_update_types()
    if TELEGRAM_UPDATE_MODE == "webhook":
        _telegram_runtime["configured_at"] = now_iso()
        await bot.set_webhook(
            _webhook_url(),
            secret_token=WEBHOOK_SECRET,
            allowed_updates=allowed_updates,
            max_connections=WEBHOOK_MAX_CONNECTIONS,
            drop_pending_updates=False,
        )
        await _refresh_telegram_runtime()
        if not _telegram_runtime["receiver_ready"]:
            raise RuntimeError("Telegram did not confirm the configured webhook URL")
    else:
        await bot.delete_webhook(drop_pending_updates=False)
        await _refresh_telegram_runtime()


async def _wait_for_shutdown_signal():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered = []
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, stop.set)
            registered.append(signame)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        for signame in registered:
            loop.remove_signal_handler(signame)


async def _serve_with_critical_workers(service_awaitable):
    """Exit the process when any critical background worker stops unexpectedly."""
    service_task = asyncio.create_task(service_awaitable)
    workers = list(_background_tasks.items())
    try:
        done, _ = await asyncio.wait(
            [service_task, *(task for _, task in workers)],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if service_task in done:
            await service_task
            return
        if _shutdown_event.is_set():
            return
        for name, task in workers:
            if task not in done:
                continue
            if task.cancelled():
                raise RuntimeError(f"Critical worker stopped unexpectedly: {name}")
            error = task.exception()
            if error is not None:
                raise RuntimeError(f"Critical worker failed: {name}") from error
            raise RuntimeError(f"Critical worker exited unexpectedly: {name}")
    finally:
        if not service_task.done():
            service_task.cancel()
            await asyncio.gather(service_task, return_exceptions=True)


async def main():
    runner = None
    _shutdown_event.clear()
    try:
        _validate_update_receiver_config()
        me = await bot.get_me()
        _validate_bot_identity(me)
        ensure_recovery_key_canary(
            DATA_DIR, TELEGRAM_INBOX_FERNET, WITHDRAW_FERNET,
            production=BIBITASKS_ENVIRONMENT == "production",
        )
        await init_db()
        runner = await start_api_server()
        _background_tasks["lifecycle"] = asyncio.create_task(lifecycle_worker())
        _background_tasks["outbox"] = asyncio.create_task(outbox_worker())
        _background_tasks["telegram_inbox"] = asyncio.create_task(
            telegram_inbox_worker()
        )
        logger.info("=" * 50)
        logger.info("БибиЗадачи запущен!")
        logger.info(f"Версия сборки: {BUILD_VERSION}")
        logger.info(f"Бот @{me.username} id={me.id}")
        logger.info("Telegram update mode: %s", TELEGRAM_UPDATE_MODE)
        logger.info("=" * 50)
        await _configure_update_receiver()
        if TELEGRAM_UPDATE_MODE == "webhook":
            service = _wait_for_shutdown_signal()
        else:
            service = dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                close_bot_session=False,
            )
        await _serve_with_critical_workers(service)
    finally:
        _telegram_runtime["receiver_ready"] = False
        if runner is not None:
            await runner.cleanup()
        _shutdown_event.set()
        inbox_task = _background_tasks.get("telegram_inbox")
        if inbox_task and not inbox_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(inbox_task), timeout=20)
            except asyncio.TimeoutError:
                logger.warning("Telegram inbox graceful shutdown timed out")
                inbox_task.cancel()
        tasks = [task for task in _background_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*_background_tasks.values(), return_exceptions=True)
        _background_tasks.clear()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        print(f"ФАТАЛЬНАЯ ОШИБКА: {e}", flush=True)
        traceback.print_exc()
        raise
