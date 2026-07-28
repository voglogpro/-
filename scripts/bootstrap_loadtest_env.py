#!/usr/bin/env python3
"""Create a sealed, disposable load-test staging configuration bundle."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path

from cryptography.fernet import Fernet

try:
    from scripts.telegram_preflight import telegram_call
except ModuleNotFoundError:
    from telegram_preflight import telegram_call


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(
    r"^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[0-9a-f]{64}$"
)
BOT_TOKEN_RE = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{30,128}$")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
    r"[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
RESOURCE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
SYNTHETIC_ADMIN_MIN = 3_800_000_000_000_000
SYNTHETIC_ADMIN_MAX = 4_503_599_627_370_000


class ConfigurationError(ValueError):
    pass


def _validate(args):
    domain = args.domain.casefold().rstrip(".")
    production_domain = args.production_domain.casefold().rstrip(".")
    if not DOMAIN_RE.fullmatch(domain) or not DOMAIN_RE.fullmatch(production_domain):
        raise ConfigurationError("staging and production domains must be hostnames")
    if domain == production_domain:
        raise ConfigurationError("staging domain must differ from production")
    if args.confirm_domain.casefold().rstrip(".") != domain:
        raise ConfigurationError("--confirm-domain must exactly match --domain")
    if not COMMIT_RE.fullmatch(args.release_commit):
        raise ConfigurationError("release commit must be 40 lowercase hex")
    if not IMAGE_RE.fullmatch(args.image):
        raise ConfigurationError("image must be an immutable lowercase GHCR digest")
    if not USERNAME_RE.fullmatch(args.bot_username.lstrip("@")):
        raise ConfigurationError("staging bot username is malformed")
    if not SYNTHETIC_ADMIN_MIN <= args.admin_user_id < SYNTHETIC_ADMIN_MAX:
        raise ConfigurationError("admin ID must be in the synthetic 52-bit high range")
    for value, label in (
        (args.production_volume, "production volume"),
        (args.production_network, "production network"),
    ):
        if not RESOURCE_RE.fullmatch(value):
            raise ConfigurationError(f"{label} name is malformed")
    if not 2 <= len(args.privacy_contact) <= 160 or any(
        character in args.privacy_contact for character in ("\r", "\n", "\x00", "$", "#")
    ):
        raise ConfigurationError("privacy contact must be a plain single-line value")
    return domain, production_domain


def _absolute_unresolved(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _trusted_directory_chain(path: Path, *, expected_uid: int | None):
    """Reject symlinked or writable ancestors before resolving a sensitive path."""
    if os.name == "nt":
        return
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ConfigurationError("sensitive path ancestry is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ConfigurationError("sensitive path ancestry must contain only directories")
        if expected_uid is not None and info.st_uid not in {0, expected_uid}:
            raise ConfigurationError("sensitive path ancestry has an untrusted owner")
        mode = stat.S_IMODE(info.st_mode)
        root_sticky_directory = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
        if mode & 0o022 and not root_sticky_directory:
            raise ConfigurationError("sensitive path ancestry must not be group/world writable")


def _read_bot_token(path: Path, *, expected_uid: int | None):
    unresolved = _absolute_unresolved(path)
    _trusted_directory_chain(unresolved.parent, expected_uid=expected_uid)
    try:
        info = unresolved.lstat()
    except OSError as exc:
        raise ConfigurationError("bot token file is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
        raise ConfigurationError("bot token file must be a small regular non-symlink file")
    if os.name != "nt":
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ConfigurationError("bot token file permissions must be 0600 or stricter")
        if expected_uid is not None and info.st_uid != expected_uid:
            raise ConfigurationError("bot token file has an unexpected owner")
    try:
        token = unresolved.read_text("utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("cannot read bot token file") from exc
    if not BOT_TOKEN_RE.fullmatch(token):
        raise ConfigurationError("bot token file is malformed")
    return token


def _verified_bot(token: str, call):
    try:
        identity = call(token, "getMe", None)
        bot_id = int((identity or {}).get("id") or 0)
        username = str((identity or {}).get("username") or "").lstrip("@")
    except Exception as exc:
        raise ConfigurationError("could not verify Telegram bot identity") from exc
    if not isinstance(identity, dict) or identity.get("is_bot") is not True:
        raise ConfigurationError("Telegram token does not identify a bot")
    if bot_id <= 0 or not USERNAME_RE.fullmatch(username):
        raise ConfigurationError("Telegram bot identity is malformed")
    return bot_id, username


def _write_private(path: Path, content: str):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            target.write(content)
            if not content.endswith("\n"):
                target.write("\n")
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _render_env(values):
    return "\n".join(f"{name}={value}" for name, value in values.items()) + "\n"


def _output_directory(raw: Path, *, expected_uid: int | None):
    unresolved = _absolute_unresolved(raw)
    repository = Path(__file__).resolve().parents[1]
    resolved = unresolved.resolve(strict=False)
    if resolved == repository or repository in resolved.parents:
        raise ConfigurationError("output directory must be outside the repository")
    if not unresolved.parent.is_dir():
        raise ConfigurationError("output parent directory does not exist")
    _trusted_directory_chain(unresolved.parent, expected_uid=expected_uid)
    if unresolved.exists() or unresolved.is_symlink():
        raise ConfigurationError("output directory already exists")
    return unresolved


def build_bundle(
    args, api_call=None, *, expected_owner_uid: int | None = None,
    evidence_gid: int | None = None,
):
    domain, production_domain = _validate(args)
    output = _output_directory(args.output_dir, expected_uid=expected_owner_uid)
    staging_token_path = _absolute_unresolved(args.bot_token_file)
    production_token_path = _absolute_unresolved(args.production_bot_token_file)
    if staging_token_path == production_token_path:
        raise ConfigurationError("staging and production bot token files must differ")
    staging_token = _read_bot_token(
        staging_token_path, expected_uid=expected_owner_uid,
    )
    production_token = _read_bot_token(
        production_token_path, expected_uid=expected_owner_uid,
    )
    if secrets.compare_digest(staging_token, production_token):
        raise ConfigurationError("staging and production bot tokens must differ")
    call = api_call or (
        lambda token, method, params=None: telegram_call(token, method, params)
    )
    staging_bot_id, staging_username = _verified_bot(staging_token, call)
    production_bot_id, _production_username = _verified_bot(production_token, call)
    if staging_username.casefold() != args.bot_username.lstrip("@").casefold():
        raise ConfigurationError("staging token does not belong to the confirmed bot")
    if staging_bot_id == production_bot_id:
        raise ConfigurationError("staging and production bot IDs must differ")

    suffix = secrets.token_hex(4)
    stem = f"bibitasks_loadtest_{args.release_commit[:12]}_{suffix}"
    resources = {
        "project": stem + "_project",
        "network": stem + "_network",
        "data": stem + "_data",
        "caddy_data": stem + "_caddy_data",
        "caddy_config": stem + "_caddy_config",
    }
    if args.production_volume in resources.values():
        raise ConfigurationError("generated load-test volume collides with production")
    if args.production_network in resources.values():
        raise ConfigurationError("generated load-test network collides with production")

    generated = {
        "MEDIA_SIGNING_KEY": secrets.token_urlsafe(48),
        "ANALYTICS_SECRET": secrets.token_urlsafe(48),
        "WEBHOOK_ROUTE_ID": secrets.token_urlsafe(48),
        "WEBHOOK_SECRET": secrets.token_urlsafe(48),
        "HEALTH_TOKEN": secrets.token_urlsafe(48),
        "TELEGRAM_INBOX_KEY": Fernet.generate_key().decode("ascii"),
        "WITHDRAW_ACCOUNT_KEY": Fernet.generate_key().decode("ascii"),
    }
    if len(set(generated.values())) != len(generated):
        raise RuntimeError("secret generator collision")
    origin = f"https://{domain}"
    public_username = f"bibitasks_lt_{suffix}_public"
    ops_username = f"bibitasks_lt_{suffix}_ops"
    group_base = 4_200_000_000_000_000 + int(suffix, 16) * 2
    group_id = -group_base
    ops_group_id = -(group_base + 1)
    staging_values = {
        "BOT_TOKEN": staging_token,
        "BOT_USERNAME": staging_username,
        "WEBAPP_SHORTNAME": "bibibike",
        "MINI_APP_URL": origin + "/",
        "PREFLIGHT_REQUIRE_MAIN_MINI_APP": "false",
        "REQUIRED_CHAT": "@" + public_username,
        "REQUIRED_CHAT_URL": "https://t.me/" + public_username,
        "JOIN_REQUEST_ADMISSION_ENABLED": "false",
        "GROUP_USERNAME": public_username,
        "GROUP_ID": str(group_id),
        "TOPIC_NEWS": "1",
        "TOPIC_CHAT": "3",
        "TOPIC_WORK": "4",
        "TOPIC_FRANCHISE": "43",
        "OPS_GROUP_USERNAME": ops_username,
        "OPS_GROUP_ID": str(ops_group_id),
        "OPS_TOPIC_TASKS": "1",
        "BIBITASKS_ENVIRONMENT": "staging",
        "PILOT_LOAD_TEST_ENABLED": "true",
        "PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED": "true",
        "ADMIN_IDS": f"{args.admin_user_id},{args.admin_user_id + 1}",
        "PORT": "3000",
        "DATA_DIR": "/app/data",
        "INIT_DATA_MAX_AGE_SEC": "900",
        "PHOTO_URL_TTL_SEC": "900",
        "MEDIA_STORAGE": "local",
        "API_READS_PER_MIN": "600",
        "API_WRITES_PER_MIN": "600",
        "API_READ_INFLIGHT_MAX": "32",
        "API_WRITE_INFLIGHT_MAX": "16",
        "API_HEAVY_INFLIGHT_MAX": "4",
        "MEDIA_NORMALIZE_CONCURRENCY": "1",
        "MEDIA_NORMALIZE_MAX_WAITERS": "3",
        "MEDIA_NORMALIZE_WAIT_TIMEOUT_SEC": "5",
        "TELEGRAM_INBOX_SOFT_LIMIT": "100",
        "TELEGRAM_INBOX_HARD_LIMIT": "500",
        "TELEGRAM_OUTBOX_SOFT_LIMIT": "100",
        "TELEGRAM_QUEUE_OLDEST_SOFT_SEC": "300",
        "PRIVACY_URL": origin + "/privacy",
        "PRIVACY_CONTROLLER_NAME": "Disposable BibiTasks load-test staging",
        "PRIVACY_CONTACT": args.privacy_contact,
        "EVIDENCE_RETENTION_DAYS": "30",
        "DISPUTE_OPEN_DAYS": "7",
        "PUBLIC_BASE_URL": origin,
        "TELEGRAM_UPDATE_MODE": "webhook",
        "WEBHOOK_MAX_CONNECTIONS": "100",
        "TELEGRAM_HANDLER_TIMEOUT_SEC": "120",
        **generated,
    }
    evidence = output / "evidence"
    deploy_values = {
        "BIBITASKS_IMAGE": args.image,
        "BIBITASKS_RELEASE_COMMIT": args.release_commit,
        "BIBITASKS_LOADTEST_ENV_FILE": str(output / "staging.env"),
        "BIBITASKS_LOADTEST_DOMAIN": domain,
        "BIBITASKS_PRODUCTION_DOMAIN": production_domain,
        "BIBITASKS_PRODUCTION_BOT_ID": str(production_bot_id),
        "BIBITASKS_LOADTEST_PROJECT": resources["project"],
        "BIBITASKS_LOADTEST_NETWORK": resources["network"],
        "BIBITASKS_PRODUCTION_NETWORK": args.production_network,
        "BIBITASKS_LOADTEST_DATA_VOLUME": resources["data"],
        "BIBITASKS_LOADTEST_CADDY_DATA_VOLUME": resources["caddy_data"],
        "BIBITASKS_LOADTEST_CADDY_CONFIG_VOLUME": resources["caddy_config"],
        "BIBITASKS_PRODUCTION_DATA_VOLUME": args.production_volume,
        "BIBITASKS_LOADTEST_EVIDENCE_DIR": str(evidence),
    }
    files = {
        "staging.env": _render_env(staging_values),
        "deploy.env": _render_env(deploy_values),
        "bot-token": staging_token + "\n",
        "health-token": generated["HEALTH_TOKEN"] + "\n",
        "webhook-secret": generated["WEBHOOK_SECRET"] + "\n",
        "webhook-path": "/telegram/webhook/" + generated["WEBHOOK_ROUTE_ID"] + "\n",
    }
    operator = {
        "report_version": 2,
        "scope": "disposable_loadtest_bundle_paths_no_secret_values",
        "domain": domain,
        "release_commit": args.release_commit,
        "image": args.image,
        "staging_bot_id": staging_bot_id,
        "staging_bot_username": staging_username,
        "production_bot_id": production_bot_id,
        "resources": resources,
        "production_data_volume": args.production_volume,
        "production_network": args.production_network,
        "admin_user_id": args.admin_user_id,
        "evidence_directory": str(evidence),
        "files": {name: str(output / name) for name in files},
    }
    files["operator.json"] = json.dumps(operator, ensure_ascii=False, indent=2) + "\n"
    created = []
    try:
        output.mkdir(mode=0o700)
        if os.name != "nt":
            output.chmod(0o700)
        evidence.mkdir(mode=0o770)
        evidence.chmod(0o770)
        if os.name != "nt" and evidence_gid is not None:
            os.chown(evidence, expected_owner_uid if expected_owner_uid is not None else -1, evidence_gid)
        for name, content in files.items():
            path = output / name
            _write_private(path, content)
            created.append(path)
    except Exception:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            evidence.rmdir()
            output.rmdir()
        except OSError:
            pass
        raise
    return operator


def plan(args):
    domain, production_domain = _validate(args)
    return {
        "mode": "apply" if args.apply else "dry_run",
        "scope": "create_disposable_loadtest_bundle",
        "domain": domain,
        "production_domain": production_domain,
        "release_commit": args.release_commit,
        "image": args.image,
        "output_directory": str(args.output_dir.expanduser().resolve(strict=False)),
        "reads_two_bot_tokens": bool(args.apply),
        "creates_owner_only_files": bool(args.apply),
        "production_data_access": False,
    }


def parser():
    result = argparse.ArgumentParser(
        description="Create a disposable staging/load-test configuration bundle",
    )
    result.add_argument("--apply", action="store_true")
    result.add_argument("--domain", required=True)
    result.add_argument("--confirm-domain", required=True)
    result.add_argument("--production-domain", required=True)
    result.add_argument("--production-volume", required=True)
    result.add_argument("--production-network", required=True)
    result.add_argument("--release-commit", required=True)
    result.add_argument("--image", required=True)
    result.add_argument("--bot-token-file", type=Path, required=True)
    result.add_argument("--production-bot-token-file", type=Path, required=True)
    result.add_argument("--bot-username", required=True)
    result.add_argument("--admin-user-id", type=int, required=True)
    result.add_argument("--privacy-contact", default="@loadtest_operator")
    result.add_argument("--output-dir", type=Path, required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        description = plan(args)
        if not args.apply:
            print(json.dumps(description, ensure_ascii=False, indent=2))
            return 0
        if os.name != "nt" and os.geteuid() != 0:
            raise ConfigurationError("--apply must run as root")
        result = build_bundle(
            args, expected_owner_uid=0,
            evidence_gid=10001 if os.name != "nt" else None,
        )
        print(json.dumps({
            "ok": True,
            "scope": result["scope"],
            "domain": result["domain"],
            "release_commit": result["release_commit"],
            "staging_bot_id": result["staging_bot_id"],
            "staging_bot_username": result["staging_bot_username"],
            "resources": result["resources"],
            "operator_file": str(args.output_dir.expanduser().resolve() / "operator.json"),
        }, ensure_ascii=False, indent=2))
        return 0
    except (ConfigurationError, OSError, RuntimeError) as exc:
        print(json.dumps({
            "ok": False, "error": type(exc).__name__, "detail": str(exc),
        }, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
