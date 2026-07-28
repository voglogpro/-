#!/usr/bin/env python3
"""Safely remove a disposable staging bot webhook without exposing its token."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

try:
    from scripts.telegram_preflight import telegram_call
except ModuleNotFoundError:
    from telegram_preflight import telegram_call


TOKEN_RE = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{30,128}$")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


class CleanupError(ValueError):
    pass


def _read_private_token(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        info = resolved.stat()
    except OSError as exc:
        raise CleanupError("staging token file is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > 512:
        raise CleanupError("staging token must be a small regular non-symlink file")
    if os.name != "nt" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077):
        raise CleanupError("staging token must be root-owned and mode 0600 or stricter")
    try:
        token = resolved.read_text("utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise CleanupError("staging token file cannot be read") from exc
    if not TOKEN_RE.fullmatch(token):
        raise CleanupError("staging token is malformed")
    return token


def reconcile(
    *, token_file: Path, expected_bot_id: int, expected_username: str,
    apply: bool, confirm_username: str = "", api_call=None,
) -> dict:
    username = expected_username.strip().lstrip("@")
    confirmation = confirm_username.strip().lstrip("@")
    if not USERNAME_RE.fullmatch(username):
        raise CleanupError("expected staging bot username is malformed")
    if not 1 <= int(expected_bot_id) <= 4_503_599_627_370_495:
        raise CleanupError("expected staging bot ID is malformed")
    if apply and confirmation.casefold() != username.casefold():
        raise CleanupError("apply requires exact staging bot username confirmation")
    token = _read_private_token(token_file)
    call = api_call or (lambda method, params=None: telegram_call(token, method, params))
    try:
        identity = call("getMe", None)
        actual_id = int((identity or {}).get("id") or 0)
        actual_username = str((identity or {}).get("username") or "").lstrip("@")
    except Exception as exc:
        raise CleanupError("staging bot identity could not be verified") from exc
    if (
        not isinstance(identity, dict) or identity.get("is_bot") is not True
        or actual_id != int(expected_bot_id)
        or actual_username.casefold() != username.casefold()
    ):
        raise CleanupError("token identity does not match the confirmed staging bot")
    if not apply:
        return {
            "ok": True,
            "mode": "dry_run",
            "scope": "staging_webhook_cleanup",
            "bot_id": actual_id,
            "bot_username": actual_username,
            "would_delete_webhook": True,
            "drop_pending_updates": False,
        }
    try:
        deleted = call("deleteWebhook", {"drop_pending_updates": "false"})
        status = call("getWebhookInfo", None)
    except Exception as exc:
        raise CleanupError("staging webhook cleanup was not confirmed") from exc
    result = deleted.get("value") if isinstance(deleted, dict) else None
    if result is not True or not isinstance(status, dict) or str(status.get("url") or ""):
        raise CleanupError("staging webhook cleanup was not confirmed")
    return {
        "ok": True,
        "mode": "apply",
        "scope": "staging_webhook_cleanup",
        "bot_id": actual_id,
        "bot_username": actual_username,
        "webhook_removed": True,
        "drop_pending_updates": False,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Remove a disposable staging bot webhook")
    result.add_argument("--bot-token-file", type=Path, required=True)
    result.add_argument("--expected-bot-id", type=int, required=True)
    result.add_argument("--expected-bot-username", required=True)
    result.add_argument("--apply", action="store_true")
    result.add_argument("--confirm-bot-username", default="")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        report = reconcile(
            token_file=args.bot_token_file,
            expected_bot_id=args.expected_bot_id,
            expected_username=args.expected_bot_username,
            apply=args.apply,
            confirm_username=args.confirm_bot_username,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except CleanupError as exc:
        print(json.dumps({
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
