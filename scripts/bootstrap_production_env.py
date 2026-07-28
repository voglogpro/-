"""Create a fail-closed production secrets file for a single-instance pilot."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet


TOKEN_RE = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
DEFAULT_BOT_DESCRIPTION = (
    "Выполняйте задания Бибибайка в своём городе, прикладывайте фото результата "
    "и получайте бибибонусы на поездки. 1 бибибонус заменяет 1 ₽, минута стоит 8,5 ₽."
)
DEFAULT_BOT_SHORT_DESCRIPTION = (
    "Задания Бибибайка: помогайте городу и получайте бибибонусы на поездки."
)
DEFAULT_BOT_MENU_TEXT = "Открыть задания"


@dataclass(frozen=True)
class BootstrapConfig:
    public_base_url: str
    group_id: int
    ops_group_id: int
    admin_ids: tuple[int, ...]
    webapp_shortname: str
    topic_news: int
    topic_chat: int
    topic_work: int
    topic_franchise: int
    ops_topic_tasks: int
    bot_username: str = "BbGalterbot"
    group_username: str = "bbbikefan"
    expected_bot_name: str = "Бибибайк"
    expected_group_title: str = "Бибибайк"
    withdraw_contact: str = "KiriLegenda"


def _clean_username(value):
    return str(value or "").strip().lstrip("@")


def validate_config(config: BootstrapConfig, bot_token: str):
    errors = []
    parsed = urlparse(config.public_base_url.strip())
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme != "https" or not parsed.netloc or port not in (None, 443)
        or parsed.username or parsed.password
        or parsed.path not in ("", "/") or parsed.query or parsed.fragment
    ):
        errors.append("PUBLIC_BASE_URL must be an HTTPS origin without path/query")
    for name, value in (("GROUP_ID", config.group_id), ("OPS_GROUP_ID", config.ops_group_id)):
        if not str(value).startswith("-100"):
            errors.append(f"{name} must be a numeric Telegram supergroup ID starting with -100")
    if config.group_id == config.ops_group_id:
        errors.append("public group and private OPS group must differ")
    unique_admins = tuple(dict.fromkeys(config.admin_ids))
    if len(unique_admins) < 2 or any(value <= 0 for value in unique_admins):
        errors.append("at least two distinct positive ADMIN_IDS are required")
    for name, value in (
        ("BOT_USERNAME", _clean_username(config.bot_username)),
        ("GROUP_USERNAME", _clean_username(config.group_username)),
        ("WITHDRAW_CONTACT", _clean_username(config.withdraw_contact)),
    ):
        if not USERNAME_RE.fullmatch(value):
            errors.append(f"{name} is malformed")
    if not re.fullmatch(r"[a-z0-9_]{3,32}", config.webapp_shortname):
        errors.append("WEBAPP_SHORTNAME is malformed")
    if not config.expected_bot_name.strip() or not config.expected_group_title.strip():
        errors.append("expected Telegram brand names must not be empty")
    if not TOKEN_RE.fullmatch(bot_token.strip()):
        errors.append("BOT_TOKEN is missing or malformed")
    topics = (
        config.topic_news, config.topic_chat, config.topic_work,
        config.topic_franchise, config.ops_topic_tasks,
    )
    if any(not isinstance(value, int) or value <= 0 for value in topics):
        errors.append("all topic IDs must be positive integers")
    if len(set(topics[:4])) != 4:
        errors.append("public topic IDs must be distinct")
    if any("\n" in value or "\r" in value for value in (
        config.expected_bot_name, config.expected_group_title,
        config.withdraw_contact,
    )):
        errors.append("text values must not contain line breaks")
    if errors:
        raise ValueError("; ".join(errors))


def build_environment(config: BootstrapConfig, bot_token: str):
    validate_config(config, bot_token)
    origin = config.public_base_url.strip().rstrip("/")
    bot_username = _clean_username(config.bot_username)
    group_username = _clean_username(config.group_username)
    generated = {
        "MEDIA_SIGNING_KEY": secrets.token_urlsafe(48),
        "ANALYTICS_SECRET": secrets.token_urlsafe(48),
        "WEBHOOK_ROUTE_ID": secrets.token_urlsafe(48),
        "WEBHOOK_SECRET": secrets.token_urlsafe(48),
        "HEALTH_TOKEN": secrets.token_urlsafe(48),
        "TELEGRAM_INBOX_KEY": Fernet.generate_key().decode("ascii"),
        "WITHDRAW_ACCOUNT_KEY": Fernet.generate_key().decode("ascii"),
    }
    if len(set(generated.values())) != len(generated):  # practically impossible, fail closed
        raise RuntimeError("secret generator produced a collision")
    values = {
        "BOT_TOKEN": bot_token.strip(),
        "BOT_USERNAME": bot_username,
        "WEBAPP_SHORTNAME": config.webapp_shortname,
        "MINI_APP_URL": f"{origin}/",
        "PREFLIGHT_REQUIRE_MAIN_MINI_APP": "true",
        "PREFLIGHT_EXPECTED_BOT_NAME": config.expected_bot_name,
        "PREFLIGHT_EXPECTED_GROUP_TITLE": config.expected_group_title,
        "BOT_PROFILE_DESCRIPTION": DEFAULT_BOT_DESCRIPTION,
        "BOT_PROFILE_SHORT_DESCRIPTION": DEFAULT_BOT_SHORT_DESCRIPTION,
        "BOT_MENU_TEXT": DEFAULT_BOT_MENU_TEXT,
        "REQUIRED_CHAT": f"@{group_username}",
        "REQUIRED_CHAT_URL": f"https://t.me/{group_username}",
        "GROUP_USERNAME": group_username,
        "GROUP_ID": str(config.group_id),
        "TOPIC_NEWS": str(config.topic_news),
        "TOPIC_CHAT": str(config.topic_chat),
        "TOPIC_WORK": str(config.topic_work),
        "TOPIC_FRANCHISE": str(config.topic_franchise),
        "OPS_GROUP_ID": str(config.ops_group_id),
        "OPS_GROUP_USERNAME": "",
        "OPS_TOPIC_TASKS": str(config.ops_topic_tasks),
        "BIBITASKS_ENVIRONMENT": "production",
        "ADMIN_IDS": ",".join(str(value) for value in dict.fromkeys(config.admin_ids)),
        "PORT": "3000",
        "DATA_DIR": "/app/data",
        "MEDIA_STORAGE": "local",
        "TELEGRAM_UPDATE_MODE": "webhook",
        "TELEGRAM_RETRY_BASE_SECONDS": "2",
        "TELEGRAM_RETRY_MAX_SECONDS": "3600",
        "TELEGRAM_RETRY_MAX_ATTEMPTS": "10",
        "PUBLIC_BASE_URL": origin,
        "WEBHOOK_MAX_CONNECTIONS": "8",
        "RIDE_RUB_PER_MIN": "8.5",
        "WITHDRAW_MIN": "1000",
        "WITHDRAW_PROCESSING_LEASE_MIN": "30",
        "WITHDRAW_CONTACT": _clean_username(config.withdraw_contact),
        "WITHDRAW_ACCOUNT_RETENTION_DAYS": "90",
        **generated,
    }
    return values


def render_environment(values):
    lines = [
        "# Generated by scripts/bootstrap_production_env.py",
        "# Keep outside Git. Never send this file through chat or email.",
    ]
    for name, value in values.items():
        raw = str(value)
        if "\n" in raw or "\r" in raw:
            raise ValueError(f"{name} contains a line break")
        lines.append(f"{name}={raw}")
    return "\n".join(lines) + "\n"


def write_environment(path: Path, values, *, repository_root=None):
    target = path.expanduser().resolve()
    repo = (repository_root or Path(__file__).resolve().parents[1]).resolve()
    try:
        target.relative_to(repo)
    except ValueError:
        pass
    else:
        raise ValueError("refusing to write a production secrets file inside the repository")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(render_environment(values))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(target, 0o600)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _supergroup_id(value):
    try:
        return int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a numeric -100... supergroup ID") from exc


def _positive_id(value):
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive Telegram user ID") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive Telegram user ID")
    return parsed


def main():
    parser = argparse.ArgumentParser(description="Generate a production BibiTasks env file")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-base-url", required=True)
    parser.add_argument("--group-id", type=_supergroup_id, required=True)
    parser.add_argument("--ops-group-id", type=_supergroup_id, required=True)
    parser.add_argument("--admin-id", type=_positive_id, action="append", required=True)
    parser.add_argument("--webapp-shortname", required=True)
    parser.add_argument("--topic-news", type=_positive_id, required=True)
    parser.add_argument("--topic-chat", type=_positive_id, required=True)
    parser.add_argument("--topic-work", type=_positive_id, required=True)
    parser.add_argument("--topic-franchise", type=_positive_id, required=True)
    parser.add_argument("--ops-topic-tasks", type=_positive_id, required=True)
    parser.add_argument("--bot-username", default="BbGalterbot")
    parser.add_argument("--group-username", default="bbbikefan")
    parser.add_argument("--token-env", default="BOT_TOKEN")
    args = parser.parse_args()
    token = str(os.environ.get(args.token_env, "")).strip()
    config = BootstrapConfig(
        public_base_url=args.public_base_url,
        group_id=args.group_id,
        ops_group_id=args.ops_group_id,
        admin_ids=tuple(args.admin_id),
        webapp_shortname=args.webapp_shortname,
        topic_news=args.topic_news,
        topic_chat=args.topic_chat,
        topic_work=args.topic_work,
        topic_franchise=args.topic_franchise,
        ops_topic_tasks=args.ops_topic_tasks,
        bot_username=args.bot_username,
        group_username=args.group_username,
    )
    try:
        values = build_environment(config, token)
        target = write_environment(args.output, values)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Created {target} with {len(values)} variables; secret values were not printed.")
    print(f"Next: python scripts/telegram_preflight.py --env-file {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
