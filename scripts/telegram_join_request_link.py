"""Safely create or reconcile the public Telegram join-request invite link.

Dry-run is the default.  A write requires both ``--apply`` and an exact
``--confirm-bot`` username.  The tool always verifies the token with ``getMe``
before planning or applying a change and never includes ``BOT_TOKEN`` in its
JSON report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - locked production requirements include it
    load_dotenv = None

try:
    from scripts.telegram_preflight import telegram_call
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from telegram_preflight import telegram_call


TOKEN_RE = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
INVITE_PATH_RE = re.compile(r"^/\+[A-Za-z0-9_-]{16,128}$")
JOIN_LINK_NAME = "БибиЗадачи · регистрация"


def _username(value):
    return str(value or "").strip().lstrip("@")


def _group_id(value):
    raw = str(value or "").strip()
    if not re.fullmatch(r"-100[0-9]{6,16}", raw):
        return None
    try:
        result = int(raw)
    except ValueError:  # pragma: no cover - guarded by the regular expression
        return None
    return result if result < 0 else None


def _invite_url(value):
    """Return a canonical private t.me invite URL or an empty string."""
    raw = str(value or "")
    if not raw:
        return ""
    if raw != raw.strip() or any(character in raw for character in "\r\n\t"):
        return ""
    try:
        parsed = urllib.parse.urlparse(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold().rstrip(".") != "t.me"
        or parsed.netloc.casefold() != "t.me"
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not INVITE_PATH_RE.fullmatch(parsed.path)
        or urllib.parse.unquote(parsed.path) != parsed.path
    ):
        return ""
    return f"https://t.me{parsed.path}"


def _configuration(env):
    token = str(env.get("BOT_TOKEN", "") or "").strip()
    username = _username(env.get("BOT_USERNAME"))
    group_id = _group_id(env.get("GROUP_ID"))
    configured_raw = str(env.get("JOIN_REQUEST_INVITE_URL", "") or "")
    configured_url = _invite_url(configured_raw)
    errors = []
    if not TOKEN_RE.fullmatch(token):
        errors.append("BOT_TOKEN is missing or malformed")
    if not USERNAME_RE.fullmatch(username):
        errors.append("BOT_USERNAME is missing or malformed")
    if group_id is None:
        errors.append("GROUP_ID must be a numeric -100... supergroup ID")
    if configured_raw and not configured_url:
        errors.append("JOIN_REQUEST_INVITE_URL must be a canonical private https://t.me invite URL")
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "token": token,
        "username": username,
        "group_id": group_id,
        "invite_url": configured_url,
        "action": "edit" if configured_url else "create",
    }


def _validated_link(result, *, bot_id, expected_url=""):
    if not isinstance(result, dict):
        raise RuntimeError("Telegram did not return a ChatInviteLink")
    invite_url = _invite_url(result.get("invite_link"))
    creator = result.get("creator")
    try:
        creator_id = int((creator or {}).get("id") or 0)
    except (TypeError, ValueError, AttributeError):
        creator_id = 0
    if not invite_url:
        raise RuntimeError("Telegram returned an invalid invite URL")
    if expected_url and invite_url != expected_url:
        raise RuntimeError("Telegram returned a different invite URL")
    if creator_id != bot_id:
        raise RuntimeError("invite link was not created by the confirmed bot")
    if result.get("creates_join_request") is not True:
        raise RuntimeError("invite link does not require join requests")
    if result.get("is_revoked") is not False:
        raise RuntimeError("invite link is revoked or its status is unknown")
    if str(result.get("name") or "") != JOIN_LINK_NAME:
        raise RuntimeError("invite link name differs from the fixed operator name")
    return invite_url


def reconcile_join_request_link(
    env=None, api_call=None, *, apply=False, confirm_bot="",
):
    """Plan or apply exactly one join-request invite-link operation."""
    env = os.environ if env is None else env
    token = str(env.get("BOT_TOKEN", "") or "").strip()
    try:
        config = _configuration(env)
    except ValueError as exc:
        return {"ok": False, "status": "invalid_config", "error": str(exc)}
    call = api_call or (
        lambda method, params=None: telegram_call(config["token"], method, params)
    )
    try:
        me = call("getMe", None)
        if not isinstance(me, dict):
            raise RuntimeError("Telegram getMe returned an invalid result")
        actual_username = _username(me.get("username"))
        try:
            bot_id = int(me.get("id") or 0)
        except (TypeError, ValueError):
            bot_id = 0
        if (
            bot_id <= 0
            or me.get("is_bot") is not True
            or actual_username.casefold() != config["username"].casefold()
        ):
            return {
                "ok": False,
                "status": "wrong_bot",
                "error": f"token belongs to @{actual_username or 'unknown'}",
            }
        plan = {
            "action": config["action"],
            "chat_id": config["group_id"],
            "name": JOIN_LINK_NAME,
            "creates_join_request": True,
        }
        if config["invite_url"]:
            plan["invite_url"] = config["invite_url"]
        if not apply:
            return {
                "ok": False,
                "status": "changes_required",
                "applied": False,
                "bot": f"@{config['username']}",
                "plan": plan,
            }
        if _username(confirm_bot).casefold() != config["username"].casefold():
            return {
                "ok": False,
                "status": "confirmation_required",
                "applied": False,
                "error": "--confirm-bot must exactly match BOT_USERNAME",
                "plan": plan,
            }
        params = {
            "chat_id": config["group_id"],
            "name": JOIN_LINK_NAME,
            "creates_join_request": True,
        }
        if config["action"] == "edit":
            method = "editChatInviteLink"
            params["invite_link"] = config["invite_url"]
        else:
            method = "createChatInviteLink"
        result = call(method, params)
        invite_url = _validated_link(
            result, bot_id=bot_id,
            expected_url=config["invite_url"] if config["action"] == "edit" else "",
        )
        return {
            "ok": True,
            "status": "applied",
            "applied": True,
            "bot": f"@{config['username']}",
            "action": config["action"],
            "chat_id": config["group_id"],
            "invite_url": invite_url,
            "creates_join_request": True,
            "creator_verified": True,
            "revoked": False,
            "persist_invite_url": config["action"] == "create",
        }
    except Exception as exc:
        error = str(exc).replace(token, "[redacted]")[:240]
        return {"ok": False, "status": "telegram_error", "error": error}


def main():
    parser = argparse.ArgumentParser(
        description="Dry-run or reconcile the BibiTasks join-request invite link",
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-bot", default="")
    args = parser.parse_args()
    if load_dotenv is not None:
        if args.env_file:
            env_path = args.env_file.expanduser().resolve()
            if not env_path.is_file():
                parser.error("--env-file must point to an existing regular file")
            load_dotenv(dotenv_path=env_path, override=False)
        else:
            load_dotenv(override=False)
    report = reconcile_join_request_link(
        apply=args.apply, confirm_bot=args.confirm_bot,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["ok"]:
        return 0
    return 2 if report["status"] == "changes_required" else 1


if __name__ == "__main__":
    sys.exit(main())
