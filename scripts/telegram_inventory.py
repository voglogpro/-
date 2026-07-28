"""Discover Telegram group and topic IDs before the first deployment.

The tool calls only getMe, getWebhookInfo and getUpdates. It never sends,
edits, deletes or acknowledges a Telegram update and never prints BOT_TOKEN or
message text. Admin IDs are returned only for an exact private marker and an
explicit --include-admin-ids opt-in.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - production lock includes python-dotenv
    load_dotenv = None


def telegram_call(token: str, method: str, params=None, timeout=15):
    endpoint = f"https://api.telegram.org/bot{token}/{method}"
    payload = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Telegram API transport error: {type(exc).__name__}",
        ) from None
    if not data.get("ok"):
        code = data.get("error_code", "unknown")
        description = str(data.get("description", "request rejected"))[:160]
        raise RuntimeError(f"Telegram API {code}: {description}")
    return data.get("result")


TOPIC_MARKERS = {
    "/inventory_news": "TOPIC_NEWS",
    "/inventory_chat": "TOPIC_CHAT",
    "/inventory_work": "TOPIC_WORK",
    "/inventory_franchise": "TOPIC_FRANCHISE",
    "/inventory_ops_tasks": "OPS_TOPIC_TASKS",
}
ADMIN_MARKER = "/inventory_admin"


def _marker(text, bot_username):
    raw = str(text or "").strip().casefold()
    if not raw or any(character.isspace() for character in raw):
        return ""
    command, separator, addressed_to = raw.partition("@")
    if separator != "@" or addressed_to != bot_username.casefold().lstrip("@"):
        return ""
    return command


def inventory_from_updates(
    updates, *, include_admin_ids=False, bot_username="BbGalterbot",
):
    """Return uniquely labelled topic coordinates and opted-in admin IDs."""
    found = {}
    admin_ids = set()
    for update in updates if isinstance(updates, list) else []:
        if not isinstance(update, dict):
            continue
        message = next(
            (
                update.get(key)
                for key in ("message", "edited_message", "channel_post")
                if isinstance(update.get(key), dict)
            ),
            None,
        )
        if not message:
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict):
            continue
        marker = _marker(message.get("text"), bot_username)
        if (
            include_admin_ids
            and marker == ADMIN_MARKER
            and chat.get("type") == "private"
        ):
            sender = message.get("from")
            try:
                admin_id = int((sender or {})["id"])
            except (KeyError, TypeError, ValueError):
                pass
            else:
                if admin_id > 0:
                    admin_ids.add(admin_id)
            continue
        variable = TOPIC_MARKERS.get(marker)
        if variable is None or chat.get("type") not in {"group", "supergroup"}:
            continue
        try:
            chat_id = int(chat["id"])
        except (KeyError, TypeError, ValueError):
            continue
        raw_thread = message.get("message_thread_id")
        try:
            thread_id = int(raw_thread) if raw_thread is not None else None
        except (TypeError, ValueError):
            thread_id = None
        key = (chat_id, thread_id)
        found[key] = {
            "chat_id": chat_id,
            "chat_type": chat.get("type"),
            "chat_title": str(chat.get("title") or "")[:100],
            "chat_username": (
                f"@{chat['username']}" if chat.get("username") else None
            ),
            "message_thread_id": thread_id,
            "is_topic_message": bool(message.get("is_topic_message")),
            "variable": variable,
        }
    locations = [
        found[key] for key in sorted(found, key=lambda item: (item[0], item[1] or 0))
    ]
    by_variable = {
        variable: [item for item in locations if item["variable"] == variable]
        for variable in TOPIC_MARKERS.values()
    }
    public_variables = {
        "TOPIC_NEWS", "TOPIC_CHAT", "TOPIC_WORK", "TOPIC_FRANCHISE",
    }
    public_chat_ids = {
        item["chat_id"]
        for variable in public_variables
        for item in by_variable[variable]
    }
    ops_chat_ids = {item["chat_id"] for item in by_variable["OPS_TOPIC_TASKS"]}
    topics_complete = (
        all(len(items) == 1 for items in by_variable.values())
        and all(
            item["message_thread_id"] is not None and item["is_topic_message"]
            for item in locations
        )
        and len(public_chat_ids) == 1
        and len(ops_chat_ids) == 1
        and public_chat_ids.isdisjoint(ops_chat_ids)
    )
    return {
        "locations": locations,
        "admin_ids": sorted(admin_ids),
        "topics_complete": topics_complete,
    }


def collect_inventory(env=None, api_call=None, *, include_admin_ids=False):
    env = os.environ if env is None else env
    token = str(env.get("BOT_TOKEN", "")).strip()
    expected = str(env.get("BOT_USERNAME", "BbGalterbot")).strip().lstrip("@")
    if not token:
        return {"ok": False, "status": "invalid_config", "error": "BOT_TOKEN is required"}
    call = api_call or (lambda method, params=None: telegram_call(token, method, params))
    try:
        bot = call("getMe")
        actual = str((bot or {}).get("username") or "")
        if actual.casefold() != expected.casefold():
            return {
                "ok": False,
                "status": "wrong_bot",
                "error": f"token belongs to @{actual or 'unknown'}, expected @{expected}",
            }
        webhook = call("getWebhookInfo")
        if str((webhook or {}).get("url") or "").strip():
            return {
                "ok": False,
                "status": "webhook_active",
                "error": "inventory is only allowed before a webhook is configured",
            }
        try:
            pending = int((webhook or {}).get("pending_update_count") or 0)
        except (TypeError, ValueError):
            pending = 0
        if pending >= 100:
            return {
                "ok": False,
                "status": "backlog_requires_operator",
                "error": (
                    "100 or more updates are pending; a developer must perform "
                    "a controlled backlog cutover before inventory"
                ),
            }
        updates = call("getUpdates", {"timeout": 0, "limit": 100})
        inventory = inventory_from_updates(
            updates, include_admin_ids=include_admin_ids, bot_username=expected,
        )
        locations = inventory["locations"]
        admin_ids = inventory["admin_ids"]
        complete = inventory["topics_complete"] and (
            not include_admin_ids or len(admin_ids) >= 2
        )
        return {
            "ok": complete,
            "status": "found" if complete else "markers_missing",
            "bot": f"@{actual}",
            "locations": locations,
            "admin_ids": admin_ids if include_admin_ids else "not_requested",
            "next": (
                "Copy verified chat/topic IDs into the production environment."
                if complete
                else "Send every documented topic marker and two private admin markers, then run again."
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "telegram_error",
            "error": str(exc).replace(token, "[redacted]")[:240],
        }


def main():
    parser = argparse.ArgumentParser(
        description="Read pending updates to discover Telegram chat/topic IDs",
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--include-admin-ids", action="store_true",
        help="include IDs of users who sent the exact marker in private chat",
    )
    args = parser.parse_args()
    if load_dotenv is not None:
        if args.env_file:
            path = args.env_file.expanduser().resolve()
            if not path.is_file():
                parser.error("--env-file must point to an existing regular file")
            load_dotenv(dotenv_path=path, override=False)
        else:
            load_dotenv(override=False)
    report = collect_inventory(include_admin_ids=args.include_admin_ids)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
