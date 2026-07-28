"""Idempotently reconcile the public Telegram bot surface for a release.

Dry-run is the default. Writes require both --apply and an exact --confirm-bot
username, so a token copied from another bot cannot silently rebrand it.
The script does not touch the webhook, groups, topics or messages. An avatar is
uploaded only when --avatar-file is explicitly combined with --apply.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - locked production requirements include it
    load_dotenv = None

try:
    from scripts.telegram_preflight import BOT_COMMANDS, telegram_call
except ModuleNotFoundError:  # direct `python scripts/...` execution
    from telegram_preflight import BOT_COMMANDS, telegram_call


TOKEN_RE = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
COMMANDS = BOT_COMMANDS


def _username(value):
    return str(value or "").strip().lstrip("@")


def _root_url(value):
    parsed = urllib.parse.urlparse(str(value or "").strip())
    try:
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https" or not parsed.netloc or port not in (None, 443)
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        return ""
    return f"https://{parsed.netloc}/"


def desired_surface(env):
    token = str(env.get("BOT_TOKEN", "")).strip()
    username = _username(env.get("BOT_USERNAME"))
    name = str(env.get("PREFLIGHT_EXPECTED_BOT_NAME", "")).strip()
    description = str(env.get("BOT_PROFILE_DESCRIPTION", "")).strip()
    short_description = str(env.get("BOT_PROFILE_SHORT_DESCRIPTION", "")).strip()
    menu_text = str(env.get("BOT_MENU_TEXT", "")).strip()
    mini_app_url = _root_url(env.get("MINI_APP_URL"))
    errors = []
    if not TOKEN_RE.fullmatch(token):
        errors.append("BOT_TOKEN is missing or malformed")
    if not USERNAME_RE.fullmatch(username):
        errors.append("BOT_USERNAME is missing or malformed")
    if not 1 <= len(name) <= 64:
        errors.append("bot name must contain 1-64 characters")
    if not 1 <= len(description) <= 512:
        errors.append("bot description must contain 1-512 characters")
    if not 1 <= len(short_description) <= 120:
        errors.append("bot short description must contain 1-120 characters")
    if not 1 <= len(menu_text) <= 64:
        errors.append("bot menu text must contain 1-64 characters")
    if not mini_app_url:
        errors.append("MINI_APP_URL must be an HTTPS root URL on port 443")
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "token": token,
        "username": username,
        "name": name,
        "description": description,
        "short_description": short_description,
        "commands": COMMANDS,
        "menu": {
            "type": "web_app",
            "text": menu_text,
            "web_app": {"url": mini_app_url},
        },
    }


def _avatar_path(value):
    path = Path(value).expanduser()
    if path.is_symlink():
        raise ValueError("avatar must not be a symlink")
    resolved = path.resolve()
    if not resolved.is_file() or resolved.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("avatar must be a regular JPG file up to 10 MB")
    try:
        with Image.open(resolved) as image:
            image.verify()
            if image.format != "JPEG":
                raise ValueError("avatar must use the JPG format")
    except (OSError, Image.UnidentifiedImageError) as exc:
        raise ValueError("avatar must be a valid JPG image") from exc
    return resolved


def telegram_upload_profile_photo(token, avatar_file, timeout=30):
    """Upload a new static bot avatar using Bot API multipart/form-data."""
    boundary = "----bibitasks-" + secrets.token_hex(16)
    photo = json.dumps(
        {"type": "static", "photo": "attach://avatar"},
        ensure_ascii=False,
    ).encode("utf-8")
    content = avatar_file.read_bytes()
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="photo"\r\n',
        b"Content-Type: application/json; charset=utf-8\r\n\r\n",
        photo, b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="avatar"; filename="avatar.jpg"\r\n',
        b"Content-Type: image/jpeg\r\n\r\n",
        content, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setMyProfilePhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Telegram avatar transport error: {type(exc).__name__}"
        ) from None
    if data.get("ok") is not True or data.get("result") is not True:
        raise RuntimeError(
            f"Telegram avatar API {data.get('error_code', 'unknown')}: "
            f"{str(data.get('description', 'request rejected'))[:160]}"
        )
    return {"value": True}


def _current_avatar_id(call, bot_id):
    profile = call("getUserProfilePhotos", {"user_id": bot_id, "limit": 1})
    photos = profile.get("photos") or []
    sizes = photos[0] if photos and isinstance(photos[0], list) else []
    largest = sizes[-1] if sizes and isinstance(sizes[-1], dict) else {}
    return str(largest.get("file_unique_id", ""))


def _list_result(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("value"), list):
        return value["value"]
    return []


def inspect_surface(call):
    me = call("getMe", None)
    name = call("getMyName", None)
    description = call("getMyDescription", None)
    short_description = call("getMyShortDescription", None)
    commands = call("getMyCommands", None)
    menu = call("getChatMenuButton", None)
    return {
        "bot_id": int(me.get("id") or 0),
        "username": _username(me.get("username")),
        "name": str(name.get("name", "")).strip(),
        "description": str(description.get("description", "")).strip(),
        "short_description": str(
            short_description.get("short_description", "")
        ).strip(),
        "commands": [
            {
                "command": str(item.get("command", "")),
                "description": str(item.get("description", "")),
            }
            for item in _list_result(commands) if isinstance(item, dict)
        ],
        "menu": {
            "type": menu.get("type"),
            "text": str(menu.get("text", "")).strip(),
            "web_app": {
                "url": _root_url((menu.get("web_app") or {}).get("url", "")),
            },
        },
    }


def _changes(desired, current):
    changes = []
    specs = (
        ("name", "setMyName", {"name": desired["name"]}),
        (
            "description", "setMyDescription",
            {"description": desired["description"]},
        ),
        (
            "short_description", "setMyShortDescription",
            {"short_description": desired["short_description"]},
        ),
        (
            "commands", "setMyCommands",
            {"commands": json.dumps(desired["commands"], ensure_ascii=False)},
        ),
        (
            "menu", "setChatMenuButton",
            {"menu_button": json.dumps(desired["menu"], ensure_ascii=False)},
        ),
    )
    for setting, method, params in specs:
        if current.get(setting) != desired.get(setting):
            changes.append({"setting": setting, "method": method, "params": params})
    return changes


def reconcile_surface(
    env=None, api_call=None, *, apply=False, confirm_bot="",
    avatar_file=None, avatar_upload=None, avatar_sleep=None,
):
    env = os.environ if env is None else env
    token = str(env.get("BOT_TOKEN", "")).strip()
    try:
        desired = desired_surface(env)
        avatar_path = _avatar_path(avatar_file) if avatar_file else None
    except ValueError as exc:
        return {"ok": False, "status": "invalid_config", "error": str(exc)}
    call = api_call or (
        lambda method, params=None: telegram_call(desired["token"], method, params)
    )
    try:
        current = inspect_surface(call)
        if current["username"].casefold() != desired["username"].casefold():
            return {
                "ok": False,
                "status": "wrong_bot",
                "error": f"token belongs to @{current['username'] or 'unknown'}",
            }
        changes = _changes(desired, current)
        if not changes and not avatar_path:
            return {
                "ok": True, "status": "already_current", "applied": False,
                "bot": f"@{desired['username']}", "changes": [],
            }
        names = [item["setting"] for item in changes]
        if avatar_path:
            names.append("avatar")
        if not apply:
            return {
                "ok": False, "status": "changes_required", "applied": False,
                "bot": f"@{desired['username']}", "changes": names,
            }
        if _username(confirm_bot).casefold() != desired["username"].casefold():
            return {
                "ok": False, "status": "confirmation_required", "applied": False,
                "error": "--confirm-bot must exactly match BOT_USERNAME",
                "changes": names,
            }
        for item in changes:
            result = call(item["method"], item["params"])
            if result.get("value") is not True:
                raise RuntimeError(f"{item['method']} was not confirmed by Telegram")
        avatar_unique_id = ""
        avatar_verification = "not_requested"
        if avatar_path:
            before_avatar = _current_avatar_id(call, current["bot_id"])
            uploader = avatar_upload or telegram_upload_profile_photo
            result = uploader(desired["token"], avatar_path)
            if result.get("value") is not True:
                raise RuntimeError("setMyProfilePhoto was not confirmed by Telegram")
            pause = avatar_sleep or time.sleep
            for attempt in range(4):
                avatar_unique_id = _current_avatar_id(call, current["bot_id"])
                if avatar_unique_id and avatar_unique_id != before_avatar:
                    avatar_verification = "new_file_unique_id"
                    break
                if attempt < 3:
                    pause(0.25 * (2 ** attempt))
            else:
                # Telegram already confirmed the irreversible write. Identical
                # content can legitimately keep the same file_unique_id, and a
                # profile read may lag; do not invite a duplicate re-upload.
                avatar_unique_id = avatar_unique_id or before_avatar
                avatar_verification = (
                    "api_confirmed_same_file_unique_id"
                    if avatar_unique_id else "api_confirmed_pending_visibility"
                )
        remaining = _changes(desired, inspect_surface(call))
        return {
            "ok": not remaining,
            "status": "applied" if not remaining else "verification_failed",
            "applied": True,
            "bot": f"@{desired['username']}",
            "changes": names,
            "remaining": [item["setting"] for item in remaining],
            "avatar_file_unique_id": avatar_unique_id,
            "avatar_verification": avatar_verification,
        }
    except Exception as exc:
        error = str(exc).replace(token, "[redacted]")[:240]
        return {"ok": False, "status": "telegram_error", "error": error}


def main():
    parser = argparse.ArgumentParser(
        description="Dry-run or reconcile BibiTasks bot profile/menu/commands",
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-bot", default="")
    parser.add_argument(
        "--avatar-file", type=Path,
        help="one-time static JPG avatar upload; requires --apply and confirmation",
    )
    args = parser.parse_args()
    if load_dotenv is not None:
        if args.env_file:
            env_path = args.env_file.expanduser().resolve()
            if not env_path.is_file():
                parser.error("--env-file must point to an existing regular file")
            load_dotenv(dotenv_path=env_path, override=False)
        else:
            load_dotenv(override=False)
    report = reconcile_surface(
        apply=args.apply, confirm_bot=args.confirm_bot,
        avatar_file=args.avatar_file,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["ok"]:
        return 0
    return 2 if report["status"] == "changes_required" else 1


if __name__ == "__main__":
    sys.exit(main())
