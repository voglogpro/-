"""Fail-closed host preflight for the controlled BibiTasks pilot.

Run this on the Ubuntu VPS after pulling the immutable image and before enabling
the systemd unit.  The report is safe to retain as release evidence: this
script never opens the production secrets file and never prints environment
values that may contain secrets.
"""

from __future__ import annotations

import argparse
import hmac
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
    from .backup_crypto import KEY_VERSION_RE, load_backup_key
except ImportError:
    from backup_crypto import KEY_VERSION_RE, load_backup_key


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(
    r"^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[0-9a-f]{64}$"
)
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
VOLUME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
REQUIRED_DEPLOY_KEYS = (
    "BIBITASKS_IMAGE",
    "BIBITASKS_RELEASE_COMMIT",
    "BIBITASKS_ENV_FILE",
    "BIBITASKS_DOMAIN",
    "BACKUP_DIR",
    "BACKUP_SENTINEL",
    "BACKUP_SENTINEL_VALUE",
    "BACKUP_EXPECTED_SOURCE",
    "BACKUP_ENCRYPTION_KEY_FILE",
    "BACKUP_ENCRYPTION_KEY_VERSION",
    "BIBITASKS_DATA_VOLUME",
    "MONITOR_ALERT_BOT_TOKEN_FILE",
    "MONITOR_HEALTH_TOKEN_FILE",
    "MONITOR_ALERT_CHAT_ID",
)
FORBIDDEN_DEPLOY_KEYS = {
    "BOT_TOKEN",
    "MEDIA_SIGNING_KEY",
    "ANALYTICS_SECRET",
    "WEBHOOK_ROUTE_ID",
    "WEBHOOK_SECRET",
    "HEALTH_TOKEN",
    "TELEGRAM_INBOX_KEY",
    "WITHDRAW_ACCOUNT_KEY",
}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


class HostProbe:
    """Small injectable boundary around host-dependent reads."""

    def command(self, args, *, cwd=None, timeout=30):
        return subprocess.run(
            [str(value) for value in args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def machine(self):
        return platform.machine()

    def system(self):
        return platform.system()

    def os_release(self):
        values = {}
        for line in Path("/etc/os-release").read_text("utf-8").splitlines():
            if "=" not in line:
                continue
            name, value = line.split("=", 1)
            values[name] = value.strip().strip('"')
        return values

    def resolve(self, domain):
        return {
            item[4][0]
            for item in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
        }

    def mount(self, target):
        result = self.command(
            ["findmnt", "--json", "--target", str(target),
             "--output", "TARGET,SOURCE,FSTYPE"],
        )
        if result.returncode != 0:
            raise RuntimeError("findmnt_failed")
        payload = json.loads(result.stdout)
        filesystems = payload.get("filesystems") or []
        if len(filesystems) != 1:
            raise RuntimeError("mount_not_unique")
        return {
            "target": str(filesystems[0].get("target") or ""),
            "source": str(filesystems[0].get("source") or ""),
            "fstype": str(filesystems[0].get("fstype") or ""),
        }


def parse_deploy_env(path: Path):
    """Parse a deliberately strict, non-secret Compose env file."""
    resolved = path.expanduser().resolve()
    if path.is_symlink() or not resolved.is_file():
        raise ValueError("deploy env must be a regular non-symlink file")
    if resolved.stat().st_size > 32 * 1024:
        raise ValueError("deploy env is unexpectedly large")
    try:
        text = resolved.read_text("utf-8")
    except UnicodeError as exc:
        raise ValueError("deploy env must use UTF-8") from exc
    if "\x00" in text:
        raise ValueError("deploy env contains a NUL byte")
    values = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"deploy env line {number} is malformed")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"deploy env line {number} has an invalid name")
        if name in values:
            raise ValueError(f"deploy env contains duplicate {name}")
        if not value or value[0:1] in {'\"', "'"} or any(
            char in value for char in ("\r", "\n", "\x00")
        ):
            raise ValueError(f"deploy env {name} must be a plain non-empty value")
        values[name] = value
    missing = [name for name in REQUIRED_DEPLOY_KEYS if not values.get(name)]
    if missing:
        raise ValueError("deploy env is missing required keys: " + ", ".join(missing))
    forbidden = sorted(FORBIDDEN_DEPLOY_KEYS.intersection(values))
    if forbidden:
        raise ValueError(
            "deploy env must not contain production secrets: " + ", ".join(forbidden)
        )
    return resolved, values


def _is_https_domain(value):
    if (
        not value or len(value) > 253 or value.endswith(".")
        or "://" in value or "/" in value or ":" in value
    ):
        return False
    labels = value.split(".")
    return len(labels) >= 2 and all(
        1 <= len(label) <= 63
        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in labels
    )


def _path_is_within(child: Path, parent: Path):
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _file_security(path: Path, *, secret=False, expected_uid=0):
    if path.is_symlink():
        return False, "must not be a symlink"
    try:
        info = path.stat()
    except OSError:
        return False, "is missing or unreadable"
    if not stat.S_ISREG(info.st_mode):
        return False, "must be a regular file"
    if os.name != "nt":
        forbidden = 0o077 if secret else 0o022
        if stat.S_IMODE(info.st_mode) & forbidden:
            return False, "permissions are too broad"
        if (
            expected_uid is not None and hasattr(info, "st_uid")
            and info.st_uid != expected_uid
        ):
            return False, f"must be owned by uid {expected_uid}"
    return True, "owner and permissions are restricted"


def _command_ok(probe, args, *, cwd=None, timeout=30):
    try:
        result = probe.command(args, cwd=cwd, timeout=timeout)
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return False
    return result.returncode == 0


def run_preflight(
    *, deploy_env: Path, repo: Path, expected_commit: str,
    expected_image: str, probe=None, expected_owner_uid=0,
):
    probe = probe or HostProbe()
    checks: list[Check] = []

    def add(name, ok, good, bad, *, warning=False):
        status = "pass" if ok else ("warn" if warning else "fail")
        checks.append(Check(name, status, good if ok else bad))

    expected_commit = str(expected_commit or "").strip().lower()
    expected_image = str(expected_image or "").strip().lower()
    add(
        "expected commit", bool(COMMIT_RE.fullmatch(expected_commit)),
        "full release commit supplied", "must be 40 lowercase hex characters",
    )
    add(
        "expected image", bool(IMAGE_RE.fullmatch(expected_image)),
        "immutable GHCR digest supplied", "must be a lowercase GHCR @sha256 reference",
    )
    if any(item.status == "fail" for item in checks):
        return _report(checks)

    try:
        deploy_path, env = parse_deploy_env(deploy_env)
        add("deploy env structure", True, "strict non-secret env parsed", "")
    except (OSError, ValueError) as exc:
        add("deploy env structure", False, "", str(exc))
        return _report(checks)

    secure, detail = _file_security(
        deploy_path, secret=False, expected_uid=expected_owner_uid,
    )
    add("deploy env permissions", secure, detail, detail)

    production_path = Path(env["BIBITASKS_ENV_FILE"])
    add(
        "production env path", production_path.is_absolute(),
        "absolute path configured", "BIBITASKS_ENV_FILE must be absolute",
    )
    if production_path.is_absolute():
        secure, detail = _file_security(
            production_path, secret=True, expected_uid=expected_owner_uid,
        )
        add("production env permissions", secure, detail, detail)

    backup_key_path = Path(env["BACKUP_ENCRYPTION_KEY_FILE"])
    backup_key_version = env["BACKUP_ENCRYPTION_KEY_VERSION"]
    key_path_ok = backup_key_path.is_absolute()
    add(
        "backup encryption key path", key_path_ok,
        "absolute private key path configured",
        "BACKUP_ENCRYPTION_KEY_FILE must be absolute",
    )
    key_version_ok = bool(KEY_VERSION_RE.fullmatch(backup_key_version))
    add(
        "backup encryption key version", key_version_ok,
        "canonical key version configured",
        "BACKUP_ENCRYPTION_KEY_VERSION is invalid",
    )
    if key_path_ok:
        secure, detail = _file_security(
            backup_key_path, secret=True, expected_uid=expected_owner_uid,
        )
        add("backup encryption key permissions", secure, detail, detail)
        key_contract_ok = False
        if secure and key_version_ok:
            try:
                load_backup_key(
                    backup_key_path, expected_version=backup_key_version,
                    expected_uid=expected_owner_uid,
                )
                key_contract_ok = True
            except (OSError, ValueError, PermissionError):
                pass
        add(
            "backup encryption key contract", key_contract_ok,
            "versioned 256-bit backup key verified",
            "backup key contract/owner/version could not be verified",
        )

    for key, check_name in (
        ("MONITOR_ALERT_BOT_TOKEN_FILE", "monitor alert token permissions"),
        ("MONITOR_HEALTH_TOKEN_FILE", "monitor health token permissions"),
    ):
        secret_path = Path(env[key])
        if not secret_path.is_absolute():
            add(check_name, False, "", f"{key} must be absolute")
        else:
            secure, detail = _file_security(
                secret_path, secret=True, expected_uid=expected_owner_uid,
            )
            add(check_name, secure, detail, detail)
    add(
        "monitor alert chat",
        bool(re.fullmatch(r"-100[0-9]{6,16}", env["MONITOR_ALERT_CHAT_ID"])),
        "private supergroup-shaped alert target configured",
        "MONITOR_ALERT_CHAT_ID must be a numeric -100... supergroup ID",
    )

    add(
        "release image binding", env["BIBITASKS_IMAGE"].lower() == expected_image,
        "deploy env matches approved digest", "deploy env image differs from approved digest",
    )
    add(
        "release commit binding",
        env["BIBITASKS_RELEASE_COMMIT"].lower() == expected_commit,
        "deploy env matches approved commit",
        "deploy env commit differs from approved release commit",
    )
    domain = env["BIBITASKS_DOMAIN"].strip().lower()
    add(
        "domain syntax", _is_https_domain(domain),
        "canonical HTTPS hostname configured", "domain must be a hostname without scheme, port or path",
    )
    add(
        "data volume name", bool(VOLUME_RE.fullmatch(env["BIBITASKS_DATA_VOLUME"])),
        "stable Docker volume name configured", "invalid Docker volume name",
    )

    backup_dir = Path(env["BACKUP_DIR"])
    sentinel = Path(env["BACKUP_SENTINEL"])
    backup_ok = (
        backup_dir.is_absolute() and not backup_dir.is_symlink()
        and backup_dir.is_dir()
    )
    add(
        "backup directory", backup_ok,
        "absolute existing non-symlink directory", "must be an existing absolute non-symlink directory",
    )
    sentinel_ok = (
        sentinel.is_absolute() and not sentinel.is_symlink()
        and sentinel.is_file() and backup_ok
        and _path_is_within(sentinel.resolve(), backup_dir.resolve())
    )
    if sentinel_ok:
        try:
            sentinel_ok = (
                sentinel.read_text("utf-8").rstrip("\r\n")
                == env["BACKUP_SENTINEL_VALUE"]
            )
        except (OSError, UnicodeError):
            sentinel_ok = False
    add(
        "backup sentinel", sentinel_ok,
        "regular sentinel exists inside backup mount", "must be a regular non-symlink file inside BACKUP_DIR",
    )
    if backup_ok:
        try:
            mount = probe.mount(backup_dir.resolve())
            mount_target = Path(mount["target"]).resolve()
            separate = mount_target.parent != mount_target and _path_is_within(
                backup_dir.resolve(), mount_target,
            )
            remote = mount.get("fstype", "").casefold() in {
                "nfs", "nfs4", "cifs", "smb3", "fuse.sshfs",
            }
            source_matches = hmac.compare_digest(
                str(mount.get("source") or ""), env["BACKUP_EXPECTED_SOURCE"],
            )
            add(
                "backup mount", separate and remote and source_matches,
                f"approved remote mount detected ({mount['fstype'] or 'unknown'})",
                "BACKUP_DIR is not the approved remote mount/source",
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            add("backup mount", False, "", "findmnt could not prove a separate mount")

    system = probe.system().casefold()
    machine = probe.machine().casefold()
    add("host OS", system == "linux", "Linux host", f"expected Linux, got {system or 'unknown'}")
    add(
        "host architecture", machine in {"x86_64", "amd64"},
        "linux/amd64 compatible", f"expected x86_64/amd64, got {machine or 'unknown'}",
    )
    try:
        release = probe.os_release()
        ubuntu_2404 = release.get("ID", "").casefold() == "ubuntu" and release.get(
            "VERSION_ID", ""
        ) == "24.04"
    except (OSError, UnicodeError, ValueError):
        ubuntu_2404 = False
    add("Ubuntu release", ubuntu_2404, "Ubuntu 24.04", "pilot requires Ubuntu 24.04")

    repo = repo.expanduser().resolve()
    add("repository", repo.is_dir(), "repository directory exists", "repository directory is missing")
    if repo.is_dir():
        revision = probe.command(["git", "rev-parse", "HEAD"], cwd=repo)
        actual_commit = revision.stdout.strip().lower() if revision.returncode == 0 else ""
        add(
            "repository commit", actual_commit == expected_commit,
            "checkout matches approved release commit", "checkout differs from approved release commit",
        )
        status_result = probe.command(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo,
        )
        clean = status_result.returncode == 0 and not status_result.stdout.strip()
        add("repository state", clean, "working tree is clean", "working tree has local or untracked changes")

    add(
        "Docker daemon",
        _command_ok(probe, ["docker", "version", "--format", "{{.Server.Version}}"]),
        "Docker daemon is reachable", "Docker daemon is unavailable",
    )
    add(
        "Compose plugin",
        _command_ok(probe, ["docker", "compose", "version", "--short"]),
        "Docker Compose plugin is available", "Docker Compose plugin is unavailable",
    )
    try:
        image_result = probe.command(
            ["docker", "image", "inspect", expected_image, "--format",
             "{{.Os}}/{{.Architecture}} {{join .RepoDigests \",\"}}"],
        )
        image_output = image_result.stdout.strip().lower()
        image_ok = (
            image_result.returncode == 0
            and image_output.startswith("linux/amd64 ")
            and expected_image in image_output
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        image_ok = False
    add(
        "local release image", image_ok,
        "approved image is present locally", "pull and verify the approved immutable image first",
    )
    compose_payload = None
    if repo.is_dir():
        try:
            compose_result = probe.command(
                ["docker", "compose", "--env-file", str(deploy_path),
                 "-f", "compose.pilot.yaml", "config", "--format", "json"],
                cwd=repo,
            )
            if compose_result.returncode == 0:
                compose_payload = json.loads(compose_result.stdout)
        except (OSError, subprocess.SubprocessError, TimeoutError,
                json.JSONDecodeError, TypeError):
            compose_payload = None
    compose_ok = isinstance(compose_payload, dict)
    add(
        "Compose render", compose_ok,
        "pilot manifest renders with approved env", "pilot manifest failed strict rendering",
    )
    backup_contract_ok = False
    if compose_ok:
        backup_service = ((compose_payload.get("services") or {}).get("backup") or {})
        environment = backup_service.get("environment") or {}
        if isinstance(environment, list):
            environment = dict(
                item.split("=", 1) for item in environment
                if isinstance(item, str) and "=" in item
            )
        tmpfs = backup_service.get("tmpfs") or []
        secrets = backup_service.get("secrets") or []
        secret_targets = {
            (item.get("target") or item.get("source")) if isinstance(item, dict) else item
            for item in secrets
        }
        backup_contract_ok = (
            backup_service.get("user") in {"0:0", "0"}
            and environment.get("BACKUP_ENCRYPTION_KEY_FILE")
                == "/run/secrets/backup_encryption_key"
            and environment.get("BACKUP_ENCRYPTION_KEY_VERSION") == backup_key_version
            and environment.get("BACKUP_PLAINTEXT_TMP_DIR")
                == "/run/bibitasks-backup-plaintext"
            and any(
                str(item).split(":", 1)[0] == "/run/bibitasks-backup-plaintext"
                for item in tmpfs
            )
            and "backup_encryption_key" in secret_targets
            and backup_service.get("network_mode") == "none"
            and backup_service.get("read_only") is True
            and (backup_service.get("ulimits") or {}).get("core")
                in (0, {"soft": 0, "hard": 0})
        )
    add(
        "backup encryption runtime", backup_contract_ok,
        "root key mount and memory-only plaintext scratch are rendered",
        "Compose does not prove encrypted backup with dedicated tmpfs scratch",
    )
    try:
        swap_result = probe.command(["swapon", "--show", "--noheadings", "--raw"])
        no_swap = swap_result.returncode == 0 and not swap_result.stdout.strip()
    except (OSError, subprocess.SubprocessError, TimeoutError):
        no_swap = False
    add(
        "plaintext scratch swap safety", no_swap,
        "host has no active swap for tmpfs plaintext pages",
        "disable swap before using tmpfs plaintext scratch",
    )

    if _is_https_domain(domain):
        try:
            addresses = probe.resolve(domain)
            public = bool(addresses) and all(
                ipaddress.ip_address(value).is_global for value in addresses
            )
        except (OSError, ValueError, socket.gaierror):
            public = False
        add(
            "public DNS", public,
            "hostname resolves only to public addresses", "hostname is unresolved or includes a non-public address",
        )

    return _report(checks)


def _report(checks):
    counts = {
        status: sum(item.status == status for item in checks)
        for status in ("pass", "warn", "fail")
    }
    return {
        "ok": counts["fail"] == 0,
        "summary": counts,
        "checks": [asdict(item) for item in checks],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Read-only Ubuntu VPS preflight for the BibiTasks pilot",
    )
    parser.add_argument("--deploy-env", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--expected-owner-uid", type=int, default=0)
    args = parser.parse_args()
    report = run_preflight(
        deploy_env=args.deploy_env,
        repo=args.repo,
        expected_commit=args.expected_commit,
        expected_image=args.expected_image,
        expected_owner_uid=args.expected_owner_uid,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
