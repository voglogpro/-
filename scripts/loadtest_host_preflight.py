#!/usr/bin/env python3
"""Fail-closed preflight/apply/destroy controller for disposable load staging."""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import platform
import re
import socket
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    from scripts.telegram_preflight import telegram_call
except ModuleNotFoundError:
    from telegram_preflight import telegram_call


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(
    r"^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[0-9a-f]{64}$"
)
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
LOAD_RESOURCE_RE = re.compile(
    r"^bibitasks_loadtest_[0-9a-f]{12}_[0-9a-f]{8}_"
    r"(?:project|network|data|caddy_data|caddy_config)$"
)
GENERIC_RESOURCE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
BOT_TOKEN_RE = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{30,128}$")
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{4,31}$", re.IGNORECASE)
STAGING_CHAT_RE = re.compile(r"^bibitasks_lt_([0-9a-f]{8})_(public|ops)$")
SYNTHETIC_MIN = 3_800_000_000_000_000
SYNTHETIC_MAX = 4_503_599_627_370_000

DEPLOY_KEYS = (
    "BIBITASKS_IMAGE",
    "BIBITASKS_RELEASE_COMMIT",
    "BIBITASKS_LOADTEST_ENV_FILE",
    "BIBITASKS_LOADTEST_DOMAIN",
    "BIBITASKS_PRODUCTION_DOMAIN",
    "BIBITASKS_PRODUCTION_BOT_ID",
    "BIBITASKS_LOADTEST_PROJECT",
    "BIBITASKS_LOADTEST_NETWORK",
    "BIBITASKS_PRODUCTION_NETWORK",
    "BIBITASKS_LOADTEST_DATA_VOLUME",
    "BIBITASKS_LOADTEST_CADDY_DATA_VOLUME",
    "BIBITASKS_LOADTEST_CADDY_CONFIG_VOLUME",
    "BIBITASKS_PRODUCTION_DATA_VOLUME",
    "BIBITASKS_LOADTEST_EVIDENCE_DIR",
)
STAGING_KEYS = (
    "BOT_TOKEN", "BOT_USERNAME", "WEBAPP_SHORTNAME", "MINI_APP_URL",
    "PREFLIGHT_REQUIRE_MAIN_MINI_APP", "REQUIRED_CHAT", "REQUIRED_CHAT_URL",
    "JOIN_REQUEST_ADMISSION_ENABLED", "GROUP_USERNAME", "GROUP_ID",
    "TOPIC_NEWS", "TOPIC_CHAT", "TOPIC_WORK", "TOPIC_FRANCHISE",
    "OPS_GROUP_USERNAME", "OPS_GROUP_ID", "OPS_TOPIC_TASKS",
    "BIBITASKS_ENVIRONMENT", "PILOT_LOAD_TEST_ENABLED",
    "PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED", "ADMIN_IDS", "PORT",
    "DATA_DIR", "INIT_DATA_MAX_AGE_SEC", "PHOTO_URL_TTL_SEC", "MEDIA_STORAGE",
    "API_READS_PER_MIN", "API_WRITES_PER_MIN", "API_READ_INFLIGHT_MAX",
    "API_WRITE_INFLIGHT_MAX", "API_HEAVY_INFLIGHT_MAX",
    "MEDIA_NORMALIZE_CONCURRENCY", "MEDIA_NORMALIZE_MAX_WAITERS",
    "MEDIA_NORMALIZE_WAIT_TIMEOUT_SEC", "TELEGRAM_INBOX_SOFT_LIMIT",
    "TELEGRAM_INBOX_HARD_LIMIT", "TELEGRAM_OUTBOX_SOFT_LIMIT",
    "TELEGRAM_QUEUE_OLDEST_SOFT_SEC", "PRIVACY_URL",
    "PRIVACY_CONTROLLER_NAME", "PRIVACY_CONTACT", "EVIDENCE_RETENTION_DAYS",
    "DISPUTE_OPEN_DAYS", "PUBLIC_BASE_URL", "TELEGRAM_UPDATE_MODE",
    "WEBHOOK_MAX_CONNECTIONS", "TELEGRAM_HANDLER_TIMEOUT_SEC",
    "MEDIA_SIGNING_KEY", "ANALYTICS_SECRET", "WEBHOOK_ROUTE_ID",
    "WEBHOOK_SECRET", "HEALTH_TOKEN", "TELEGRAM_INBOX_KEY",
    "WITHDRAW_ACCOUNT_KEY",
)
INDEPENDENT_SECRET_KEYS = (
    "MEDIA_SIGNING_KEY", "ANALYTICS_SECRET", "WEBHOOK_ROUTE_ID",
    "WEBHOOK_SECRET", "HEALTH_TOKEN", "TELEGRAM_INBOX_KEY",
    "WITHDRAW_ACCOUNT_KEY",
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


class HostProbe:
    def command(self, args, *, cwd=None, timeout=30):
        return subprocess.run(
            [str(value) for value in args], cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    def system(self):
        return platform.system()

    def machine(self):
        return platform.machine()

    def resolve(self, domain):
        return {
            item[4][0]
            for item in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
        }

    def telegram_get_me(self, token):
        return telegram_call(token, "getMe")


def _absolute_unresolved(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _trusted_directory(path: Path, *, expected_uid: int | None,
                       final_gid: int | None = None, final_mode: int | None = None):
    unresolved = _absolute_unresolved(path)
    if os.name == "nt":
        return unresolved.is_dir() and not unresolved.is_symlink()
    current = Path(unresolved.anchor)
    try:
        parts = unresolved.parts[1:]
        for index, part in enumerate(parts):
            current /= part
            info = current.lstat()
            final = index == len(parts) - 1
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                return False
            if expected_uid is not None and info.st_uid not in {0, expected_uid}:
                return False
            mode = stat.S_IMODE(info.st_mode)
            if final and final_mode is not None:
                if mode != final_mode:
                    return False
                if final_gid is not None and info.st_gid != final_gid:
                    return False
            elif expected_uid is not None and mode & 0o022:
                return False
        return True
    except OSError:
        return False


def _plain_env(path: Path, required: tuple[str, ...], *, max_bytes: int,
               exact_keys: bool = True):
    unresolved = _absolute_unresolved(path)
    try:
        info = unresolved.lstat()
    except OSError as exc:
        raise ValueError("env is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("env must be a regular non-symlink file")
    if info.st_size > max_bytes:
        raise ValueError("env is unexpectedly large")
    try:
        text = unresolved.read_text("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("env must use UTF-8") from exc
    if "\x00" in text:
        raise ValueError("env contains a NUL byte")
    values = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"env line {number} is malformed")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not ENV_NAME_RE.fullmatch(name) or name in values:
            raise ValueError(f"env line {number} has an invalid or duplicate name")
        if not value or value[:1] in {'"', "'"} or any(
            character in value for character in ("\r", "\n", "\x00")
        ):
            raise ValueError(f"env {name} must be a plain non-empty value")
        values[name] = value
    missing = [name for name in required if not values.get(name)]
    extras = sorted(set(values).difference(required)) if exact_keys else []
    if missing:
        raise ValueError("env is missing required keys: " + ", ".join(missing))
    if extras:
        raise ValueError("env contains unexpected keys: " + ", ".join(extras))
    return unresolved, values


def _secure_file(path: Path, *, expected_uid=0):
    unresolved = _absolute_unresolved(path)
    try:
        info = unresolved.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return False
    if os.name != "nt":
        if stat.S_IMODE(info.st_mode) & 0o077:
            return False
        if expected_uid is not None and info.st_uid != expected_uid:
            return False
    return _trusted_directory(unresolved.parent, expected_uid=expected_uid)


def _trusted_config_file(path: Path, *, expected_uid=0):
    unresolved = _absolute_unresolved(path)
    try:
        info = unresolved.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return False
    if os.name != "nt":
        if stat.S_IMODE(info.st_mode) & 0o022:
            return False
        if expected_uid is not None and info.st_uid != expected_uid:
            return False
    return _trusted_directory(unresolved.parent, expected_uid=expected_uid)


def _domain(value):
    if not value or len(value) > 253 or value.endswith(".") or any(
        marker in value for marker in ("://", "/", ":")
    ):
        return False
    labels = value.split(".")
    return len(labels) >= 2 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in labels
    )


def _synthetic_ids(raw, *, negative=False, exact_count=None):
    try:
        values = [int(value) for value in raw.split(",")]
    except (TypeError, ValueError):
        return False
    if exact_count is not None and len(values) != exact_count:
        return False
    expected_sign = (lambda value: value < 0) if negative else (lambda value: value > 0)
    return bool(values) and len(values) == len(set(values)) and all(
        expected_sign(value) and SYNTHETIC_MIN <= abs(value) <= SYNTHETIC_MAX
        for value in values
    )


def _fernet_key(value):
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeError):
        return False
    return len(decoded) == 32 and len(value) == 44


def _report(checks, operation):
    summary = {
        status: sum(item.status == status for item in checks)
        for status in ("pass", "warn", "fail")
    }
    return {
        "report_version": 2,
        "scope": "disposable_loadtest_host_no_secret_values",
        "operation": operation,
        "ok": summary["fail"] == 0,
        "summary": summary,
        "checks": [asdict(item) for item in checks],
    }


def _network_names(service):
    networks = service.get("networks") or {}
    return set(networks if isinstance(networks, dict) else networks)


def _mount_signature(mount):
    if not isinstance(mount, dict):
        return None
    return (
        mount.get("type"), mount.get("source"), mount.get("target"),
        bool(mount.get("read_only")),
    )


def _resource_inspection(probe, kind, name):
    return probe.command(["docker", kind, "inspect", name])


def _resource_labelled(result, *, release_commit=None):
    if result.returncode != 0:
        return False
    try:
        document = json.loads(result.stdout)
        item = document[0]
        labels = item.get("Labels") or item.get("Config", {}).get("Labels") or {}
    except (json.JSONDecodeError, IndexError, TypeError, AttributeError):
        return False
    return labels.get("com.bibitasks.purpose") == "loadtest" and (
        release_commit is None
        or labels.get("com.bibitasks.release-commit") == release_commit
    )


def _resource_missing(result):
    diagnostic = (str(result.stdout or "") + "\n" + str(result.stderr or "")).casefold()
    return result.returncode != 0 and (
        "not found" in diagnostic or "no such volume" in diagnostic
        or "no such network" in diagnostic
    )


def _compose_command(deploy_path, project, *tail):
    return [
        "docker", "compose", "--env-file", str(deploy_path),
        "--project-name", project, "-f", "compose.loadtest.yaml", *tail,
    ]


def run_preflight(
    *, deploy_env: Path, repo: Path, expected_commit: str,
    expected_image: str, probe=None, expected_owner_uid=0,
    operation="check", confirm_domain="",
):
    probe = probe or HostProbe()
    checks: list[Check] = []

    def add(name, ok, good, bad):
        checks.append(Check(name, "pass" if ok else "fail", good if ok else bad))

    if operation not in {"check", "apply", "destroy"}:
        add("operation", False, "", "operation is invalid")
        return _report(checks, operation)
    expected_commit = str(expected_commit or "").strip().lower()
    expected_image = str(expected_image or "").strip().lower()
    add("expected commit", bool(COMMIT_RE.fullmatch(expected_commit)),
        "full release commit supplied", "expected commit must be 40 lowercase hex")
    add("expected image", bool(IMAGE_RE.fullmatch(expected_image)),
        "immutable GHCR digest supplied", "expected image must be GHCR @sha256")
    if any(item.status == "fail" for item in checks):
        return _report(checks, operation)

    try:
        deploy_path, deploy = _plain_env(deploy_env, DEPLOY_KEYS, max_bytes=16 * 1024)
        add("deploy env", True, "exact non-secret deploy env parsed", "")
    except (OSError, ValueError):
        add("deploy env", False, "", "exact non-secret deploy env is invalid")
        return _report(checks, operation)
    bundle = deploy_path.parent
    add("bundle trust", _secure_file(deploy_path, expected_uid=expected_owner_uid),
        "bundle and deploy env have trusted ownership and ancestry",
        "bundle/deploy env ownership, mode or ancestry is unsafe")
    add("release binding",
        deploy["BIBITASKS_RELEASE_COMMIT"].lower() == expected_commit
        and deploy["BIBITASKS_IMAGE"].lower() == expected_image,
        "deploy env matches release commit and digest", "deploy env release binding differs")

    staging_domain = deploy["BIBITASKS_LOADTEST_DOMAIN"].casefold()
    production_domain = deploy["BIBITASKS_PRODUCTION_DOMAIN"].casefold()
    add("separate staging domain",
        _domain(staging_domain) and _domain(production_domain)
        and staging_domain != production_domain,
        "staging and production domains are distinct", "domains are invalid or equal")
    if operation in {"apply", "destroy"}:
        add("explicit domain confirmation",
            confirm_domain.casefold().rstrip(".") == staging_domain,
            "mutation target domain explicitly confirmed",
            "--confirm-domain must exactly match staging domain")

    resource_keys = {
        "project": "BIBITASKS_LOADTEST_PROJECT",
        "network": "BIBITASKS_LOADTEST_NETWORK",
        "data": "BIBITASKS_LOADTEST_DATA_VOLUME",
        "caddy_data": "BIBITASKS_LOADTEST_CADDY_DATA_VOLUME",
        "caddy_config": "BIBITASKS_LOADTEST_CADDY_CONFIG_VOLUME",
    }
    resources = {name: deploy[key] for name, key in resource_keys.items()}
    resource_stem = resources["project"][:-len("_project")]
    expected_resources = {
        "project": resource_stem + "_project",
        "network": resource_stem + "_network",
        "data": resource_stem + "_data",
        "caddy_data": resource_stem + "_caddy_data",
        "caddy_config": resource_stem + "_caddy_config",
    }
    production_volume = deploy["BIBITASKS_PRODUCTION_DATA_VOLUME"]
    production_network = deploy["BIBITASKS_PRODUCTION_NETWORK"]
    add("isolated resource namespace",
        all(LOAD_RESOURCE_RE.fullmatch(value) for value in resources.values())
        and resources == expected_resources
        and len(set(resources.values())) == len(resources)
        and GENERIC_RESOURCE_RE.fullmatch(production_volume) is not None
        and GENERIC_RESOURCE_RE.fullmatch(production_network) is not None
        and production_volume not in resources.values()
        and production_network not in resources.values(),
        "unique load-test resources differ from production",
        "load-test resource names are inconsistent, unsafe or collide with production")

    staging_path = Path(deploy["BIBITASKS_LOADTEST_ENV_FILE"])
    evidence_path = Path(deploy["BIBITASKS_LOADTEST_EVIDENCE_DIR"])
    if not staging_path.is_absolute() or not evidence_path.is_absolute():
        staging = None
        add("bundle paths", False, "", "staging env and evidence paths must be absolute")
    else:
        try:
            resolved_staging, staging = _plain_env(
                staging_path, STAGING_KEYS, max_bytes=64 * 1024,
            )
            paths_ok = (
                resolved_staging.parent == bundle
                and evidence_path.parent == bundle
                and _secure_file(resolved_staging, expected_uid=expected_owner_uid)
                and _trusted_directory(
                    evidence_path, expected_uid=expected_owner_uid,
                    final_gid=10001 if expected_owner_uid is not None else None,
                    final_mode=0o770 if expected_owner_uid is not None else None,
                )
            )
            add("bundle paths", paths_ok,
                "staging env and evidence directory are sealed inside the bundle",
                "staging env or evidence directory ownership/path/mode is unsafe")
        except (OSError, ValueError):
            staging = None
            add("bundle paths", False, "", "staging env is invalid")

    if staging is not None:
        origin = f"https://{staging_domain}"
        staging_bot = staging["BOT_USERNAME"].lstrip("@").casefold()
        try:
            production_bot_id = int(deploy["BIBITASKS_PRODUCTION_BOT_ID"])
            identity = probe.telegram_get_me(staging["BOT_TOKEN"])
            actual_bot = str((identity or {}).get("username") or "").lstrip("@").casefold()
            actual_bot_id = int((identity or {}).get("id") or 0)
            bot_ok = (
                production_bot_id > 0 and isinstance(identity, dict)
                and identity.get("is_bot") is True and actual_bot_id > 0
                and actual_bot == staging_bot and actual_bot_id != production_bot_id
                and USERNAME_RE.fullmatch(actual_bot) is not None
            )
        except Exception:
            bot_ok = False
        add("live staging bot identity", bot_ok,
            "getMe bot ID differs from immutable production bot ID",
            "staging token identity is invalid or equals production bot ID")
        add("staging runtime mode",
            staging["BIBITASKS_ENVIRONMENT"] == "staging"
            and staging["PILOT_LOAD_TEST_ENABLED"].casefold() == "true"
            and staging["PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED"].casefold() == "true"
            and staging["TELEGRAM_UPDATE_MODE"] == "webhook"
            and staging["DATA_DIR"] == "/app/data",
            "staging and destructive load switch are explicit",
            "staging runtime flags are not fail-closed")
        add("staging URL binding",
            staging["PUBLIC_BASE_URL"].rstrip("/") == origin
            and staging["MINI_APP_URL"].rstrip("/") == origin
            and staging["PRIVACY_URL"] == origin + "/privacy",
            "runtime URLs match the isolated staging domain",
            "staging URLs differ from the load-test domain")
        public_match = STAGING_CHAT_RE.fullmatch(staging["GROUP_USERNAME"])
        ops_match = STAGING_CHAT_RE.fullmatch(staging["OPS_GROUP_USERNAME"])
        chat_ids = f'{staging["GROUP_ID"]},{staging["OPS_GROUP_ID"]}'
        add("synthetic Telegram destinations",
            bool(public_match and ops_match)
            and public_match.group(1) == ops_match.group(1)
            and public_match.group(2) == "public" and ops_match.group(2) == "ops"
            and _synthetic_ids(chat_ids, negative=True, exact_count=2)
            and staging["REQUIRED_CHAT"] == "@" + staging["GROUP_USERNAME"]
            and staging["REQUIRED_CHAT_URL"] == "https://t.me/" + staging["GROUP_USERNAME"]
            and staging["JOIN_REQUEST_ADMISSION_ENABLED"].casefold() == "false",
            "group/ops fixtures are synthetic and join admission is disabled",
            "Telegram destinations could address non-staging chats")
        secret_values = [staging[name] for name in INDEPENDENT_SECRET_KEYS]
        add("staging secret independence",
            len(set(secret_values)) == len(secret_values)
            and all(len(value) >= 32 for value in secret_values)
            and _fernet_key(staging["TELEGRAM_INBOX_KEY"])
            and _fernet_key(staging["WITHDRAW_ACCOUNT_KEY"])
            and bool(BOT_TOKEN_RE.fullmatch(staging["BOT_TOKEN"])),
            "runtime secrets are present and mutually distinct",
            "runtime secrets are missing, short, duplicated or malformed")
        add("staging admin fixture",
            _synthetic_ids(staging["ADMIN_IDS"], exact_count=2),
            "two synthetic high-range staging admins are configured",
            "ADMIN_IDS must contain two unique synthetic high-range IDs")

    repo = _absolute_unresolved(repo)
    repo_trusted = _trusted_directory(repo, expected_uid=expected_owner_uid)
    add("repository trust", repo_trusted,
        "repository ownership and ancestry are trusted",
        "repository is missing, symlinked, writable by others or untrusted")
    approved_caddyfile = repo / "deploy" / "Caddyfile.loadtest"
    add("approved load-test Caddyfile",
        _trusted_config_file(approved_caddyfile, expected_uid=expected_owner_uid),
        "load-test Caddyfile is a trusted regular file",
        "load-test Caddyfile is missing, writable or symlinked")
    if repo.is_dir():
        revision = probe.command(["git", "rev-parse", "HEAD"], cwd=repo)
        status = probe.command(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo,
        )
        add("repository release state",
            revision.returncode == 0 and revision.stdout.strip().lower() == expected_commit
            and status.returncode == 0 and not status.stdout.strip(),
            "clean checkout matches release commit", "checkout is dirty or on another commit")
    add("host platform",
        probe.system().casefold() == "linux"
        and probe.machine().casefold() in {"x86_64", "amd64"},
        "Linux amd64 host", "load-test host must be Linux amd64")
    try:
        docker_ok = (
            probe.command(["docker", "version", "--format", "{{.Server.Version}}"]).returncode == 0
            and probe.command(["docker", "compose", "version", "--short"]).returncode == 0
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        docker_ok = False
    add("Docker runtime", docker_ok, "Docker and Compose are available",
        "Docker or Compose is unavailable")
    image_result = probe.command(
        ["docker", "image", "inspect", expected_image, "--format",
         "{{.Os}}/{{.Architecture}} {{join .RepoDigests \",\"}}"],
    )
    image_text = image_result.stdout.strip().lower()
    add("local release image",
        image_result.returncode == 0 and image_text.startswith("linux/amd64 ")
        and expected_image in image_text,
        "approved image is present", "approved Linux image is not present")

    inspections = {
        "network": _resource_inspection(probe, "network", resources["network"]),
        "data": _resource_inspection(probe, "volume", resources["data"]),
        "caddy_data": _resource_inspection(probe, "volume", resources["caddy_data"]),
        "caddy_config": _resource_inspection(probe, "volume", resources["caddy_config"]),
    }
    project_containers = probe.command([
        "docker", "ps", "-a", "--filter",
        f'label=com.docker.compose.project={resources["project"]}',
        "--format", '{{.Label "com.docker.compose.service"}}',
    ])
    actual_services = {
        line.strip() for line in project_containers.stdout.splitlines() if line.strip()
    } if project_containers.returncode == 0 else {"<inspect-failed>"}
    if operation == "destroy":
        state_ok = (
            _resource_labelled(inspections["network"])
            and all(_resource_labelled(inspections[name], release_commit=expected_commit)
                    for name in ("data", "caddy_data", "caddy_config"))
            and actual_services == {"bibitasks", "caddy"}
        )
        add("existing labelled load resources", state_ok,
            "exact disposable project resources are present and labelled",
            "destroy target resources are absent, unlabelled or contain unexpected services")
    else:
        state_ok = all(_resource_missing(result) for result in inspections.values()) and not actual_services
        add("fresh load resources", state_ok,
            "all load-test resources and project containers are absent",
            "a generated load-test volume/network/project already exists")

    compose_result = probe.command(
        _compose_command(deploy_path, resources["project"],
                         "--profile", "loadtest", "config", "--format", "json"),
        cwd=repo,
    )
    isolated = False
    if compose_result.returncode == 0:
        try:
            manifest = json.loads(compose_result.stdout)
            services = manifest.get("services") or {}
            volumes = manifest.get("volumes") or {}
            networks = manifest.get("networks") or {}
            app = services.get("bibitasks") or {}
            caddy = services.get("caddy") or {}
            runner = services.get("loadtest-runner") or {}
            caddyfile = str(approved_caddyfile)
            evidence = str(evidence_path.resolve())
            expected_mounts = {
                "bibitasks": {("volume", "loadtest_data", "/app/data", False)},
                "caddy": {
                    ("bind", caddyfile, "/etc/caddy/Caddyfile", True),
                    ("volume", "loadtest_caddy_data", "/data", False),
                    ("volume", "loadtest_caddy_config", "/config", False),
                },
                "loadtest-runner": {("bind", evidence, "/evidence", False)},
            }
            mounts_ok = all(
                {_mount_signature(item) for item in services[name].get("volumes", [])}
                == expected
                for name, expected in expected_mounts.items()
            )
            volume_names = {
                "loadtest_data": resources["data"],
                "loadtest_caddy_data": resources["caddy_data"],
                "loadtest_caddy_config": resources["caddy_config"],
            }
            volumes_ok = set(volumes) == set(volume_names) and all(
                volumes[key].get("name") == name
                and (volumes[key].get("labels") or {}).get("com.bibitasks.purpose") == "loadtest"
                and (volumes[key].get("labels") or {}).get("com.bibitasks.release-commit") == expected_commit
                for key, name in volume_names.items()
            )
            network = networks.get("loadtest") or {}
            network_ok = (
                set(networks) == {"loadtest"}
                and network.get("name") == resources["network"]
                and (network.get("labels") or {}).get("com.bibitasks.purpose") == "loadtest"
                and all(_network_names(service) == {"loadtest"}
                        for service in (app, caddy, runner))
            )
            environment_ok = (
                (app.get("environment") or {}).get("BIBITASKS_ENVIRONMENT") == "staging"
                and (app.get("environment") or {}).get("PILOT_LOAD_TEST_ENABLED") == "true"
                and (app.get("environment") or {}).get(
                    "PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED"
                ) == "true"
                and (runner.get("environment") or {}).get("BIBITASKS_ENVIRONMENT") == "staging"
                and (runner.get("environment") or {}).get("PILOT_LOAD_TEST_ENABLED") == "true"
                and (runner.get("environment") or {}).get(
                    "PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED"
                ) == "true"
                and (caddy.get("environment") or {}).get("BIBITASKS_DOMAIN") == staging_domain
            )
            excluded = all(
                value not in compose_result.stdout
                for value in (production_volume, production_network)
            )
            isolated = (
                manifest.get("name") == resources["project"]
                and set(services) == {"bibitasks", "caddy", "loadtest-runner"}
                and app.get("image", "").lower() == expected_image
                and runner.get("image", "").lower() == expected_image
                and set(runner.get("profiles") or []) == {"loadtest"}
                and mounts_ok and volumes_ok and network_ok and environment_ok and excluded
            )
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
            isolated = False
    add("isolated Compose render", isolated,
        "exact app/Caddy/runner mounts, networks and labelled resources validated",
        "Compose render differs from the sealed disposable topology")

    try:
        addresses = probe.resolve(staging_domain)
        public_dns = bool(addresses) and all(
            ipaddress.ip_address(value).is_global for value in addresses
        )
    except (OSError, ValueError, socket.gaierror):
        public_dns = False
    add("staging DNS", public_dns,
        "staging hostname resolves only to public addresses",
        "staging hostname is unresolved or includes a private address")

    preliminary = _report(checks, operation)
    if operation == "check" or not preliminary["ok"]:
        return preliminary
    if operation == "apply":
        result = probe.command(
            _compose_command(deploy_path, resources["project"],
                             "up", "-d", "--wait", "--wait-timeout", "180"),
            cwd=repo, timeout=300,
        )
        add("compose apply", result.returncode == 0,
            "disposable staging started after the green preflight",
            "Compose failed to start disposable staging")
    else:
        result = probe.command(
            _compose_command(deploy_path, resources["project"],
                             "down", "--volumes", "--remove-orphans"),
            cwd=repo, timeout=300,
        )
        post = {
            "network": _resource_inspection(probe, "network", resources["network"]),
            "data": _resource_inspection(probe, "volume", resources["data"]),
            "caddy_data": _resource_inspection(probe, "volume", resources["caddy_data"]),
            "caddy_config": _resource_inspection(probe, "volume", resources["caddy_config"]),
        }
        removed = result.returncode == 0 and all(_resource_missing(item) for item in post.values())
        add("compose destroy", removed,
            "containers, network and all three disposable volumes were removed",
            "Compose destroy failed or a disposable Docker resource remains")
    return _report(checks, operation)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Sealed preflight/apply/destroy for disposable load staging",
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--apply", action="store_true")
    operation.add_argument("--destroy", action="store_true")
    parser.add_argument("--confirm-domain", default="")
    parser.add_argument("--deploy-env", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--expected-owner-uid", type=int, default=0)
    args = parser.parse_args(argv)
    selected = "apply" if args.apply else "destroy" if args.destroy else "check"
    if selected != "check" and (
        os.name != "nt" and (os.geteuid() != 0 or args.expected_owner_uid != 0)
    ):
        print(json.dumps({
            "report_version": 2, "ok": False, "operation": selected,
            "error": "mutation_requires_root",
        }))
        return 2
    report = run_preflight(
        deploy_env=args.deploy_env, repo=args.repo,
        expected_commit=args.expected_commit, expected_image=args.expected_image,
        expected_owner_uid=args.expected_owner_uid, operation=selected,
        confirm_domain=args.confirm_domain,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
