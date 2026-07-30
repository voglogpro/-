"""Fail-closed SQLite rollback into a new Docker named volume.

The workflow is deliberately split into three explicit phases:

* ``plan`` binds the currently deployed commit/image/volume to a previously
  approved release record and an exact backup;
* ``apply`` creates a random, previously absent volume, restores into it and
  validates the restore as the image's unprivileged UID/GID;
* ``verify-stage`` revalidates the actual volume and explicitly reports that
  production activation is disabled until post-backup financial/message
  reconciliation has a separately reviewed machine-verifiable contract.

No command removes a Docker volume.  In particular, the current data volume is
never mounted by ``apply`` and remains available for roll-forward/recovery.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import AbstractContextManager
from functools import wraps
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from .backup_crypto import (
        BACKUP_FORMAT, ENCRYPTION_METHOD, KEY_VERSION_RE, PAYLOAD_NAME,
        encryption_aad, load_backup_key,
    )
    from .pilot_host_preflight import IMAGE_RE, COMMIT_RE, VOLUME_RE, parse_deploy_env
    from .recovery_key_canary import CANARY_FILENAME, validate_canary_bytes
    from .release_candidate import validate_candidate
except ImportError:  # pragma: no cover - direct script execution
    from backup_crypto import (
        BACKUP_FORMAT, ENCRYPTION_METHOD, KEY_VERSION_RE, PAYLOAD_NAME,
        encryption_aad, load_backup_key,
    )
    from pilot_host_preflight import IMAGE_RE, COMMIT_RE, VOLUME_RE, parse_deploy_env
    from recovery_key_canary import CANARY_FILENAME, validate_canary_bytes
    from release_candidate import validate_candidate


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9@_][A-Za-z0-9@_.-]{0,126}\.service$")
PLAN_VERSION = 1
STAGE_VERSION = 1
DEFAULT_PLAN_MINUTES = 30
DEFAULT_LOCK_FILE = Path("/run/lock/bibitasks-sqlite-rollback.lock")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _secure_regular(path: Path, label: str, *, private: bool = False) -> Path:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"{label} must be an existing regular file")
    info = resolved.stat()
    if private and os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(f"{label} permissions must be 0600 or stricter")
    return resolved


def _json_file(path: Path, label: str, *, private: bool = False) -> tuple[Path, dict]:
    resolved = _secure_regular(path, label, private=private)
    if resolved.stat().st_size > 1024 * 1024:
        raise ValueError(f"{label} is unexpectedly large")
    try:
        value = json.loads(resolved.read_text("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return resolved, value


def _write_exclusive(path: Path, payload: dict, *, mode: int = 0o600) -> Path:
    target = path.expanduser().resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return target


def _preflight_new_output(path: Path, label: str) -> Path:
    target = path.expanduser().resolve()
    parent = target.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError(f"{label} parent must be an existing non-symlink directory")
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"{label} target already exists")
    if os.name != "nt":
        info = parent.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError(f"{label} parent must be owned by the invoking user and mode 0700")
    return target


class Runner:
    def command(self, args, *, cwd=None, timeout=120):
        values = [str(value) for value in args]
        if os.name != "nt" and values:
            trusted = {
                "docker": "/usr/bin/docker",
                "git": "/usr/bin/git",
            }
            executable = trusted.get(values[0])
            if executable:
                executable_path = Path(executable)
                if not executable_path.is_file() or executable_path.is_symlink():
                    raise RuntimeError(f"trusted executable is missing: {executable}")
                info = executable_path.stat()
                if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                    raise RuntimeError(f"trusted executable has unsafe ownership/mode: {executable}")
                values[0] = executable
        return subprocess.run(
            values,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


class HostLock(AbstractContextManager):
    """Crash-safe, process-wide host lock; the inode may remain after a crash."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.descriptor = None

    def __enter__(self):
        parent = self.path.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("rollback lock parent must be an existing non-symlink directory")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("rollback lock must be a regular file")
            if os.name != "nt":
                if stat.S_IMODE(info.st_mode) & 0o077:
                    raise ValueError("rollback lock permissions must be 0600 or stricter")
                if info.st_uid != os.geteuid():
                    raise ValueError("rollback lock must be owned by the invoking user")
                import fcntl
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError("another rollback operation holds the host lock") from None
            else:  # exercised by the Windows development test suite
                import msvcrt
                if info.st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise RuntimeError("another rollback operation holds the host lock") from None
            os.ftruncate(descriptor, 0)
            os.write(
                descriptor,
                f"pid={os.getpid()} acquired_at={utc_now().isoformat()}\n".encode("ascii"),
            )
            os.fsync(descriptor)
            self.descriptor = descriptor
            return self
        except Exception:
            os.close(descriptor)
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        if self.descriptor is None:
            return False
        try:
            if os.name != "nt":
                import fcntl
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            else:
                import msvcrt
                os.lseek(self.descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self.descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self.descriptor)
            self.descriptor = None
        return False


def host_locked(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        lock_file = Path(kwargs.pop("lock_file", DEFAULT_LOCK_FILE))
        with HostLock(lock_file):
            return function(*args, **kwargs)
    return wrapped


def _run(runner, args, *, cwd=None, timeout=120, label="command"):
    try:
        result = runner.command(args, cwd=cwd, timeout=timeout)
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        raise RuntimeError(f"{label} could not run: {type(exc).__name__}") from None
    if result.returncode != 0:
        detail = " ".join(str(result.stderr or "").strip().split())[:300]
        raise RuntimeError(f"{label} failed" + (f": {detail}" if detail else ""))
    return result


def _git(runner, repo: Path, *args, label="git") -> str:
    return _run(
        runner, ["git", *args], cwd=repo, timeout=60, label=label,
    ).stdout.strip()


def _validate_repo_current(runner, repo: Path, expected_commit: str) -> None:
    if not repo.is_dir() or repo.is_symlink():
        raise ValueError("repo must be an existing non-symlink directory")
    head = _git(runner, repo, "rev-parse", "HEAD", label="git HEAD").lower()
    if head != expected_commit:
        raise ValueError("repository HEAD differs from current deploy commit")
    if _git(runner, repo, "status", "--porcelain", label="git status"):
        raise ValueError("repository worktree must be clean")


def _validate_target_commit(runner, repo: Path, commit: str) -> None:
    resolved = _git(
        runner, repo, "rev-parse", "--verify", f"{commit}^{{commit}}",
        label="target commit lookup",
    ).lower()
    if resolved != commit:
        raise ValueError("target commit is not available exactly")


def _validate_record(record: dict, expected_hash: str) -> dict:
    if not SHA256_RE.fullmatch(expected_hash):
        raise ValueError("release-artifact-sha256 must be an exact lowercase SHA-256")
    if record.get("candidate_version") == 1:
        candidate = validate_candidate(record)
        backup = candidate["backup"]
        return {
            "artifact_type": "release_candidate_v1",
            "candidate_sha256": expected_hash,
            "software_subject_sha256": candidate["software_subject_sha256"],
            "promotion_subject_sha256": candidate["promotion_subject_sha256"],
            "application_version": candidate["application_version"],
            "commit": candidate["commit"],
            "image": candidate["image"],
            "schema_version": candidate["schema_version"],
            "backup_id": str(backup["id"]),
            "manifest_sha256": str(backup["manifest_sha256"]),
            "database_sha256": str(backup["database"]["sha256"]),
        }
    raise ValueError(
        "legacy release records are unsupported; create a new candidate bound "
        "to an authenticated encrypted backup"
    )


def _validate_backup(backup_dir: Path, target: dict) -> tuple[Path, Path, dict]:
    if backup_dir.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    root = backup_dir.expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("backup directory must be an existing regular directory")
    if root.name != target["backup_id"]:
        raise ValueError("backup directory name differs from release record backup ID")
    if "," in str(root):
        raise ValueError("backup directory path must not contain a comma")
    manifest_path, manifest = _json_file(root / "manifest.json", "backup manifest")
    if sha256(manifest_path) != target["manifest_sha256"]:
        raise ValueError("backup manifest digest differs from release record")
    database = manifest.get("database") or {}
    if (
        database.get("integrity_check") != "ok"
        or int(database.get("schema_version", -1)) != target["schema_version"]
        or str(database.get("sha256") or "").lower() != target["database_sha256"]
    ):
        raise ValueError("backup database contract differs from release record")
    count_names = (
        "telegram_ciphertext_count", "telegram_active_null_count",
        "withdrawal_ciphertext_count", "withdrawal_active_null_count",
    )
    if any(type(database.get(name)) is not int or database[name] < 0 for name in count_names):
        raise ValueError("backup database lacks valid encrypted recovery counts")
    if database["telegram_active_null_count"] or database["withdrawal_active_null_count"]:
        raise ValueError("backup database contains active rows without ciphertext")
    canary = manifest.get("recovery_key_canary") or {}
    if (
        set(canary) != {"path", "bytes", "sha256"}
        or canary.get("path") != CANARY_FILENAME
        or type(canary.get("bytes")) is not int or canary["bytes"] <= 0
        or not SHA256_RE.fullmatch(str(canary.get("sha256") or ""))
    ):
        raise ValueError("backup manifest lacks a valid recovery-key canary")
    encryption = manifest.get("encryption")
    if encryption is not None:
        if not isinstance(encryption, dict):
            raise ValueError("backup encryption contract is invalid")
        ciphertext = encryption.get("ciphertext") or {}
        if (
            encryption.get("format") != BACKUP_FORMAT
            or encryption.get("method") != ENCRYPTION_METHOD
            or not KEY_VERSION_RE.fullmatch(str(encryption.get("key_version") or ""))
            or not SHA256_RE.fullmatch(
                str(encryption.get("protected_manifest_sha256") or "")
            )
            or not SHA256_RE.fullmatch(str(encryption.get("aad_sha256") or ""))
            or ciphertext.get("path") != PAYLOAD_NAME
            or type(ciphertext.get("bytes")) is not int
            or ciphertext["bytes"] <= 0
            or not SHA256_RE.fullmatch(str(ciphertext.get("sha256") or ""))
        ):
            raise ValueError("backup encryption contract is invalid")
        try:
            nonce = base64.b64decode(
                encryption.get("nonce_b64", ""), altchars=b"-_", validate=True,
            )
            tag = base64.b64decode(
                encryption.get("tag_b64", ""), altchars=b"-_", validate=True,
            )
        except (ValueError, TypeError):
            raise ValueError("backup encryption contract is invalid") from None
        aad = encryption_aad(
            encryption["key_version"], encryption["protected_manifest_sha256"],
        )
        if (
            len(nonce) != 12 or len(tag) != 16
            or hashlib.sha256(aad).hexdigest() != encryption["aad_sha256"]
        ):
            raise ValueError("backup encryption contract is invalid")
        payload = root / PAYLOAD_NAME
        if (
            not payload.is_file() or payload.is_symlink()
            or payload.stat().st_nlink != 1
            or payload.stat().st_size != ciphertext["bytes"]
            or sha256(payload) != ciphertext["sha256"]
        ):
            raise ValueError("encrypted backup payload differs from manifest")
    else:
        raise ValueError("production rollback requires an encrypted backup")
    ready_s3 = [
        item for item in (manifest.get("media_objects") or [])
        if item.get("backend") == "s3" and item.get("state") == "ready"
    ]
    if ready_s3:
        raise ValueError("SQLite named-volume rollback supports local media only")
    return root, manifest_path, manifest


def _inspect_volume(runner, name: str, *, required: bool) -> dict | None:
    result = runner.command(["docker", "volume", "inspect", name], timeout=30)
    if result.returncode != 0:
        if not required:
            return None
        raise RuntimeError(f"required Docker volume is missing: {name}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Docker volume inspect returned invalid JSON") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeError("Docker volume inspect returned an unexpected result")
    return payload[0]


def _expected_volume_labels(plan: dict) -> dict[str, str]:
    target = plan["target"]
    return {
        "com.bibitasks.rollback.plan": plan["plan_id"],
        "com.bibitasks.rollback.source-volume": plan["current"]["volume"],
        "com.bibitasks.rollback.target-commit": target["commit"],
        "com.bibitasks.rollback.target-image-sha256": target["image"].rsplit(":", 1)[1],
        "com.bibitasks.rollback.manifest-sha256": target["manifest_sha256"],
    }


def _volume_fingerprint(inspected: dict) -> str:
    contract = {
        key: inspected.get(key)
        for key in ("Name", "Driver", "Mountpoint", "CreatedAt", "Scope", "Options", "Labels")
    }
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _inspect_image(runner, image: str, commit: str, *, pull: bool) -> tuple[int, int]:
    if pull:
        _run(runner, ["docker", "pull", image], timeout=600, label="target image pull")
    result = _run(
        runner, ["docker", "image", "inspect", image],
        timeout=60, label="target image inspect",
    )
    try:
        payload = json.loads(result.stdout)
        inspected = payload[0]
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
        raise RuntimeError("target image inspect returned invalid JSON") from exc
    digests = {str(item).lower() for item in inspected.get("RepoDigests") or []}
    labels = ((inspected.get("Config") or {}).get("Labels") or {})
    if image not in digests:
        raise ValueError("local image does not expose the exact approved RepoDigest")
    if str(labels.get("org.opencontainers.image.revision") or "").lower() != commit:
        raise ValueError("image OCI revision label differs from target commit")

    def identity(flag: str) -> int:
        value = _run(
            runner,
            ["docker", "run", "--rm", "--network", "none", "--read-only",
             "--cap-drop", "ALL", "--entrypoint", "id", image, flag],
            timeout=60, label=f"image identity {flag}",
        ).stdout.strip()
        if not value.isdigit() or int(value) <= 0:
            raise ValueError("target image must run as a non-root numeric UID/GID")
        return int(value)

    return identity("-u"), identity("-g")


def _confirmation(plan: dict, action: str) -> str:
    return (
        f"{action} {plan['plan_id']} TO {plan['target']['commit']} "
        f"ON {plan['target']['volume']}"
    )


def _subject_bindings(target: dict) -> dict:
    """Return non-secret artifact bindings for every rollback evidence phase."""
    return {
        key: target[key] for key in (
            "candidate_sha256", "software_subject_sha256",
            "promotion_subject_sha256",
        ) if key in target
    }


@host_locked
def build_plan(
    *, deploy_env: Path, repo: Path, release_record: Path,
    release_record_sha256: str, backup_dir: Path, output: Path,
    target_volume: str | None = None, runner=None, now=None,
) -> dict:
    runner = runner or Runner()
    now = now or utc_now()
    deploy_path, current = parse_deploy_env(deploy_env)
    _secure_regular(deploy_path, "deploy env", private=True)
    current_commit = current["BIBITASKS_RELEASE_COMMIT"].lower()
    current_image = current["BIBITASKS_IMAGE"].lower()
    current_volume = current["BIBITASKS_DATA_VOLUME"]
    if not COMMIT_RE.fullmatch(current_commit) or not IMAGE_RE.fullmatch(current_image):
        raise ValueError("current deploy env lacks immutable commit/image guards")
    if not VOLUME_RE.fullmatch(current_volume):
        raise ValueError("current deploy env has an invalid data volume")
    repo = repo.expanduser().resolve()
    _validate_repo_current(runner, repo, current_commit)
    _inspect_image(runner, current_image, current_commit, pull=False)

    record_path, record = _json_file(release_record, "target release record")
    expected_record_hash = str(release_record_sha256 or "").strip().lower()
    if sha256(record_path) != expected_record_hash:
        raise ValueError("target release record digest does not match operator input")
    target = _validate_record(record, expected_record_hash)
    if target["commit"] == current_commit or target["image"] == current_image:
        raise ValueError("rollback target must differ from the current release")
    backup_root, manifest_path, manifest = _validate_backup(backup_dir, target)
    target["canary_sha256"] = manifest["recovery_key_canary"]["sha256"]
    target["backup_key_version"] = manifest["encryption"]["key_version"]
    _validate_target_commit(runner, repo, target["commit"])
    _inspect_volume(runner, current_volume, required=True)
    uid, gid = _inspect_image(runner, target["image"], target["commit"], pull=True)

    plan_id = secrets.token_hex(16)
    volume = target_volume or f"bibitasks_rollback_{target['commit'][:12]}_{plan_id[:12]}"
    if not VOLUME_RE.fullmatch(volume):
        raise ValueError("target volume name is invalid")
    if volume == current_volume:
        raise ValueError("target volume must differ from the current data volume")
    if _inspect_volume(runner, volume, required=False) is not None:
        raise FileExistsError("target volume already exists; choose a fresh name")

    plan = {
        "plan_version": PLAN_VERSION,
        "plan_id": plan_id,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=DEFAULT_PLAN_MINUTES)).isoformat(),
        "repo": str(repo),
        "deploy_env": str(deploy_path),
        "deploy_env_sha256": sha256(deploy_path),
        "current": {
            "commit": current_commit, "image": current_image, "volume": current_volume,
        },
        "target": {
            **target, "volume": volume, "uid": uid, "gid": gid,
            "release_record": str(record_path),
            "release_record_sha256": expected_record_hash,
            "backup_dir": str(backup_root),
            "manifest": str(manifest_path),
        },
    }
    plan["apply_confirmation"] = _confirmation(plan, "APPLY")
    _write_exclusive(output, plan)
    return plan


def _load_plan(path: Path, *, now=None, enforce_expiry=True) -> tuple[Path, dict]:
    plan_path, plan = _json_file(path, "rollback plan", private=True)
    if int(plan.get("plan_version", -1)) != PLAN_VERSION:
        raise ValueError("rollback plan version is unsupported")
    if not re.fullmatch(r"[0-9a-f]{32}", str(plan.get("plan_id") or "")):
        raise ValueError("rollback plan ID is invalid")
    try:
        expires = datetime.fromisoformat(str(plan["expires_at"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("rollback plan expiry is invalid") from exc
    now = now or utc_now()
    if expires.tzinfo is None:
        raise ValueError("rollback plan expiry must include a timezone")
    if enforce_expiry and now >= expires:
        raise ValueError("rollback plan has expired; create a new plan")
    if plan.get("apply_confirmation") != _confirmation(plan, "APPLY"):
        raise ValueError("rollback plan apply confirmation is corrupt")
    return plan_path, plan


def _revalidate_plan(plan: dict, runner, *, require_current=True) -> tuple[Path | None, bytes | None]:
    repo = Path(plan["repo"])
    deploy_path = None
    original = None
    if require_current:
        deploy_path, current = parse_deploy_env(Path(plan["deploy_env"]))
        original = deploy_path.read_bytes()
        if hashlib.sha256(original).hexdigest() != plan["deploy_env_sha256"]:
            raise ValueError("deploy env changed after rollback planning")
        for key, planned in (
            ("BIBITASKS_RELEASE_COMMIT", plan["current"]["commit"]),
            ("BIBITASKS_IMAGE", plan["current"]["image"]),
            ("BIBITASKS_DATA_VOLUME", plan["current"]["volume"]),
        ):
            if current[key].lower() != str(planned).lower():
                raise ValueError(f"current deploy {key} differs from rollback plan")
        _validate_repo_current(runner, repo, plan["current"]["commit"])
        _inspect_image(
            runner, plan["current"]["image"], plan["current"]["commit"], pull=False,
        )
    _validate_target_commit(runner, repo, plan["target"]["commit"])
    record_path, record = _json_file(
        Path(plan["target"]["release_record"]), "target release record",
    )
    if sha256(record_path) != plan["target"]["release_record_sha256"]:
        raise ValueError("target release record changed after planning")
    target = _validate_record(record, plan["target"]["release_record_sha256"])
    for key in ("artifact_type", "application_version", "commit", "image",
                "schema_version", "backup_id", "manifest_sha256",
                "database_sha256", "candidate_sha256",
                "software_subject_sha256", "promotion_subject_sha256"):
        if key not in target and key not in plan["target"]:
            continue
        if target[key] != plan["target"][key]:
            raise ValueError(f"target release {key} differs from rollback plan")
    _, _, manifest = _validate_backup(Path(plan["target"]["backup_dir"]), target)
    if manifest["recovery_key_canary"]["sha256"] != plan["target"].get("canary_sha256"):
        raise ValueError("target recovery-key canary differs from rollback plan")
    if manifest["encryption"]["key_version"] != plan["target"].get("backup_key_version"):
        raise ValueError("target backup key version differs from rollback plan")
    if require_current:
        _inspect_volume(runner, plan["current"]["volume"], required=True)
    uid, gid = _inspect_image(
        runner, target["image"], target["commit"], pull=False,
    )
    if (uid, gid) != (int(plan["target"]["uid"]), int(plan["target"]["gid"])):
        raise ValueError("target image UID/GID changed after planning")
    return deploy_path, original


def _validate_restore_report(report: dict, target: dict) -> None:
    canary = report.get("recovery_key_canary") or {}
    if (
        report.get("integrity_check") != "ok"
        or int(report.get("schema_version", -1)) != int(target["schema_version"])
        or str(report.get("source_manifest_sha256") or "").lower()
        != target["manifest_sha256"]
        or str(report.get("database_sha256_after_restore") or "").lower()
        != target["database_sha256"]
        or int(report.get("s3_versions_rewritten", -1)) != 0
        or set(canary) != {"sha256", "ok"}
        or canary.get("ok") is not True
        or canary.get("sha256") != target.get("canary_sha256")
    ):
        raise ValueError("restored volume report differs from approved release evidence")


READ_REPORT_CODE = (
    "from pathlib import Path; print(Path('/target/restored/restore-report.json')"
    ".read_text(encoding='utf-8'))"
)

PROMOTE_CODE = r"""
import os, stat
from pathlib import Path
root=Path('/target')
stage=root/'restored'
uid=int(os.environ['TARGET_UID']); gid=int(os.environ['TARGET_GID'])
if not stage.is_dir() or stage.is_symlink(): raise SystemExit('unsafe restore staging')
if [p.name for p in root.iterdir() if p.name != 'restored']:
    raise SystemExit('target volume is not fresh')
paths=[stage, *stage.rglob('*')]
if any(p.is_symlink() for p in paths): raise SystemExit('symlink in restored data')
for child in list(stage.iterdir()): os.replace(child, root/child.name)
stage.rmdir()
paths=sorted(root.rglob('*'),key=lambda p:len(p.parts),reverse=True)+[root]
for path in paths:
    os.chmod(path, 0o700 if path.is_dir() else 0o600, follow_symlinks=False)
    os.chown(path, uid, gid, follow_symlinks=False)
""".strip()

FINAL_CHECK_CODE = r"""
import hashlib,json,os,sqlite3,stat
from pathlib import Path
root=Path('/target'); dbp=root/'bibitasks.db'; report=root/'restore-report.json'; manifestp=Path('/backup/manifest.json')
def digest(path):
 d=hashlib.sha256()
 with path.open('rb') as f:
  for chunk in iter(lambda:f.read(1048576),b''): d.update(chunk)
 return d.hexdigest()
with sqlite3.connect(f'file:{dbp}?mode=ro',uri=True) as db:
 integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
 schema=int(db.execute('PRAGMA user_version').fetchone()[0])
files=list(root.rglob('*'))
manifest=json.loads(manifestp.read_text('utf-8'))
expected={}
safe=True
canary_meta=manifest.get('recovery_key_canary') or {}
canary_path=root/'recovery-key-canaries.json'
canary_sha256=''
canary_ok=(set(canary_meta)=={'path','bytes','sha256'} and
 canary_meta.get('path')=='recovery-key-canaries.json' and
 type(canary_meta.get('bytes')) is int and canary_meta['bytes']>0 and
 canary_path.is_file() and not canary_path.is_symlink())
if canary_ok:
 canary_sha256=digest(canary_path)
 canary_ok=(canary_path.stat().st_size==canary_meta['bytes'] and
  canary_sha256==canary_meta.get('sha256'))
for item in manifest.get('media') or []:
 rel=Path(str(item.get('path') or ''))
 if rel.is_absolute() or '..' in rel.parts or not rel.parts or rel.parts[0] not in ('task_photos','proof_photos'):
  safe=False; continue
 expected[rel.as_posix()]=(int(item.get('bytes',-1)),str(item.get('sha256') or ''))
actual={}
for folder in ('task_photos','proof_photos'):
 base=root/folder
 if base.exists():
  for path in base.rglob('*'):
   if path.is_symlink(): safe=False
   elif path.is_file(): actual[path.relative_to(root).as_posix()]=path
media_ok=safe and set(actual)==set(expected)
media_bytes=0
if media_ok:
 for name,path in actual.items():
  size,want=expected[name]; media_bytes+=size
  if path.stat().st_size!=size or digest(path)!=want: media_ok=False
print(json.dumps({'report':json.loads(report.read_text('utf-8')),
 'database_sha256':digest(dbp),'schema_version':schema,'integrity_check':integrity,
 'manifest_sha256':digest(manifestp),'local_media_ok':media_ok,
 'local_media_count':len(actual),'local_media_bytes':media_bytes,
 'canary_sha256':canary_sha256,'canary_ok':canary_ok,
 'all_owned':all(p.stat().st_uid==os.getuid() and p.stat().st_gid==os.getgid() for p in [root,*files]),
 'all_readable':all(os.access(p,os.R_OK) for p in files)}))
""".strip()


def _common_docker_run() -> list[str]:
    return [
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:size=64m,mode=1777", "--cap-drop", "ALL",
        "--ulimit", "core=0:0",
        "--security-opt", "no-new-privileges:true",
    ]


def _final_volume_check(plan: dict, runner) -> dict:
    target = plan["target"]
    volume = target["volume"]
    raw = _run(
        runner,
        [*_common_docker_run(), "--user", f"{target['uid']}:{target['gid']}",
         "--mount", f"type=volume,src={volume},dst=/target,readonly",
         "--mount", f"type=bind,src={target['backup_dir']},dst=/backup,readonly",
         target["image"], "python", "-c", FINAL_CHECK_CODE],
        timeout=300, label="unprivileged restored volume validation",
    ).stdout
    try:
        final = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("final volume validation returned invalid JSON") from exc
    _validate_restore_report(final.get("report") or {}, target)
    if (
        final.get("integrity_check") != "ok"
        or int(final.get("schema_version", -1)) != int(target["schema_version"])
        or str(final.get("database_sha256") or "").lower() != target["database_sha256"]
        or str(final.get("manifest_sha256") or "").lower() != target["manifest_sha256"]
        or final.get("local_media_ok") is not True
        or final.get("canary_ok") is not True
        or final.get("canary_sha256") != target.get("canary_sha256")
        or int(final.get("local_media_count", -1)) < 0
        or int(final.get("local_media_bytes", -1)) < 0
        or final.get("all_owned") is not True or final.get("all_readable") is not True
    ):
        raise ValueError("unprivileged final volume validation failed")
    return final


def _revalidate_staged_volume(plan: dict, stage: dict, runner) -> dict:
    """Read the actual volume again; never trust stage-report as live state."""
    inspected = _inspect_volume(runner, plan["target"]["volume"], required=True)
    expected_labels = _expected_volume_labels(plan)
    if (inspected.get("Labels") or {}) != expected_labels:
        raise ValueError("staged volume labels differ from the exact rollback plan")
    if _volume_fingerprint(inspected) != stage.get("volume_fingerprint"):
        raise ValueError("staged volume was deleted, recreated, or otherwise replaced")
    final = _final_volume_check(plan, runner)
    expected_final = stage.get("final_validation") or {}
    for key in (
        "database_sha256", "schema_version", "integrity_check",
        "manifest_sha256", "local_media_ok", "local_media_count",
        "local_media_bytes", "canary_sha256", "canary_ok",
        "all_owned", "all_readable",
    ):
        if final.get(key) != expected_final.get(key):
            raise ValueError("live staged volume differs from its final validation evidence")
    return final


@host_locked
def apply_plan(
    *, plan_file: Path, confirmation: str, stage_report: Path,
    runner=None, now=None, backup_key_file: Path | None = None,
) -> dict:
    runner = runner or Runner()
    plan_path, plan = _load_plan(plan_file, now=now)
    if not secrets.compare_digest(str(confirmation or ""), plan["apply_confirmation"]):
        raise ValueError("exact apply confirmation does not match rollback plan")
    stage_target = _preflight_new_output(stage_report, "stage report")
    _revalidate_plan(plan, runner)
    target = plan["target"]
    _, deploy_values = parse_deploy_env(Path(plan["deploy_env"]))
    configured_key = Path(deploy_values["BACKUP_ENCRYPTION_KEY_FILE"])
    if backup_key_file is None and (
        deploy_values["BACKUP_ENCRYPTION_KEY_VERSION"]
        != target["backup_key_version"]
    ):
        raise ValueError(
            "encrypted backup uses an older key version; provide --backup-key-file"
        )
    backup_key_source = _secure_regular(
        backup_key_file or configured_key,
        "backup encryption key", private=True,
    )
    load_backup_key(
        backup_key_source, expected_version=target["backup_key_version"],
    )
    if "," in str(backup_key_source):
        raise ValueError("backup encryption key path must not contain a comma")
    _, _, active_manifest = _validate_backup(
        Path(target["backup_dir"]), target,
    )
    if active_manifest["encryption"]["key_version"] != target["backup_key_version"]:
        raise ValueError("backup key version differs from encrypted manifest")
    volume = target["volume"]
    if _inspect_volume(runner, volume, required=False) is not None:
        raise FileExistsError("target volume already exists; plan cannot be replayed")

    labels = _expected_volume_labels(plan)
    create = ["docker", "volume", "create"]
    for name, value in labels.items():
        create.extend(["--label", f"{name}={value}"])
    create.append(volume)
    created = _run(runner, create, timeout=60, label="fresh volume create").stdout.strip()
    if created != volume:
        raise RuntimeError("Docker did not return the exact requested volume name")
    inspected = _inspect_volume(runner, volume, required=True)
    actual_labels = inspected.get("Labels") or {}
    if actual_labels != labels:
        raise RuntimeError("new volume labels do not match rollback plan")

    common = _common_docker_run()
    mounts = [
        "--mount", f"type=bind,src={target['backup_dir']},dst=/backup,readonly",
        "--mount", f"type=volume,src={volume},dst=/target",
        "--mount", (
            f"type=bind,src={backup_key_source},"
            "dst=/run/secrets/backup_encryption_key,readonly"
        ),
    ]
    _run(
        runner,
        [*common, "--tmpfs",
         "/run/bibitasks-backup-plaintext:size=512m,mode=0700,noexec,nosuid,nodev",
         "--user", "0:0", *mounts, target["image"], "python",
         "scripts/restore.py", "--backup-dir", "/backup", "--restore-dir",
         "/target/restored", "--encryption-key-file",
         "/run/secrets/backup_encryption_key", "--plaintext-tmp-dir",
         "/run/bibitasks-backup-plaintext"],
        timeout=1800, label="isolated backup restore",
    )
    raw_report = _run(
        runner, [*common, "--user", "0:0", *mounts, target["image"],
                 "python", "-c", READ_REPORT_CODE],
        timeout=60, label="restore report read",
    ).stdout
    try:
        restore_report = json.loads(raw_report)
    except json.JSONDecodeError as exc:
        raise RuntimeError("restored volume produced invalid restore-report.json") from exc
    _validate_restore_report(restore_report, target)

    _run(
        runner,
        [*common, "--cap-add", "CHOWN", "--user", "0:0",
         "--env", f"TARGET_UID={target['uid']}",
         "--env", f"TARGET_GID={target['gid']}",
         "--mount", f"type=volume,src={volume},dst=/target",
         target["image"], "python", "-c", PROMOTE_CODE],
        timeout=300, label="restore ownership promotion",
    )
    final = _final_volume_check(plan, runner)

    report = {
        "stage_version": STAGE_VERSION,
        "created_at": utc_now().isoformat(),
        "plan": str(plan_path),
        "plan_sha256": sha256(plan_path),
        "plan_id": plan["plan_id"],
        "volume": volume,
        "current_volume_preserved": plan["current"]["volume"],
        "target_commit": target["commit"],
        "target_image": target["image"],
        "target_uid": target["uid"],
        "target_gid": target["gid"],
        **_subject_bindings(target),
        "volume_labels": labels,
        "volume_fingerprint": _volume_fingerprint(inspected),
        "restore_report": restore_report,
        "final_validation": {
            key: final[key] for key in (
                "database_sha256", "schema_version", "integrity_check",
                "manifest_sha256", "local_media_ok", "local_media_count",
                "local_media_bytes", "canary_sha256", "canary_ok",
                "all_owned", "all_readable",
            )
        },
        "ready_for_point_in_time_recovery_review": True,
        "production_activation_enabled": False,
    }
    _write_exclusive(stage_target, report)
    return report


def _validate_stage(plan: dict, stage_file: Path) -> dict:
    _, stage = _json_file(stage_file, "rollback stage report", private=True)
    if (
        int(stage.get("stage_version", -1)) != STAGE_VERSION
        or stage.get("ready_for_point_in_time_recovery_review") is not True
        or stage.get("production_activation_enabled") is not False
        or stage.get("plan_id") != plan["plan_id"]
        or stage.get("target_commit") != plan["target"]["commit"]
        or stage.get("target_image") != plan["target"]["image"]
        or stage.get("volume") != plan["target"]["volume"]
        or any(stage.get(key) != value for key, value in _subject_bindings(plan["target"]).items())
    ):
        raise ValueError("stage report does not match rollback plan")
    _validate_restore_report(stage.get("restore_report") or {}, plan["target"])
    final = stage.get("final_validation") or {}
    if (
        final.get("integrity_check") != "ok"
        or final.get("database_sha256") != plan["target"]["database_sha256"]
        or final.get("manifest_sha256") != plan["target"]["manifest_sha256"]
        or final.get("local_media_ok") is not True
        or final.get("canary_ok") is not True
        or final.get("canary_sha256") != plan["target"].get("canary_sha256")
        or int(final.get("local_media_count", -1)) < 0
        or int(final.get("local_media_bytes", -1)) < 0
        or int(final.get("schema_version", -1)) != plan["target"]["schema_version"]
        or final.get("all_owned") is not True or final.get("all_readable") is not True
    ):
        raise ValueError("stage report final validation is not green")
    if stage.get("volume_labels") != _expected_volume_labels(plan):
        raise ValueError("stage report volume labels do not match rollback plan")
    if not SHA256_RE.fullmatch(str(stage.get("volume_fingerprint") or "")):
        raise ValueError("stage report lacks a valid volume fingerprint")
    return stage


def _verify_staged(
    plan_file: Path, stage_report: Path, stage_report_sha256: str, runner, now=None,
) -> dict:
    runner = runner or Runner()
    plan_path, plan = _load_plan(plan_file, now=now, enforce_expiry=False)
    expected_stage_hash = str(stage_report_sha256 or "").strip().lower()
    if not SHA256_RE.fullmatch(expected_stage_hash):
        raise ValueError("stage-report-sha256 must be an exact lowercase SHA-256")
    stage_path = _secure_regular(stage_report, "rollback stage report", private=True)
    if sha256(stage_path) != expected_stage_hash:
        raise ValueError("stage report digest differs from operator evidence")
    _revalidate_plan(plan, runner, require_current=False)
    stage = _validate_stage(plan, stage_report)
    if stage.get("plan_sha256") != sha256(plan_path):
        raise ValueError("stage report is not bound to the exact rollback plan")
    current_volume = _inspect_volume(runner, plan["current"]["volume"], required=True)
    if str(current_volume.get("Name") or "") != plan["current"]["volume"]:
        raise ValueError("current preserved volume identity is not exact")
    final = _revalidate_staged_volume(plan, stage, runner)
    return {
        "report_version": 1,
        "generated_at": utc_now().isoformat(),
        "ok": True,
        "plan_sha256": sha256(plan_path),
        "stage_report_sha256": expected_stage_hash,
        "current": {
            "commit": plan["current"]["commit"],
            "image": plan["current"]["image"],
            "volume": plan["current"]["volume"],
            "present": True,
        },
        "target": {
            "commit": plan["target"]["commit"],
            "image": plan["target"]["image"],
            "schema_version": plan["target"]["schema_version"],
            "application_version": plan["target"]["application_version"],
            "volume": plan["target"]["volume"],
            **_subject_bindings(plan["target"]),
        },
        **_subject_bindings(plan["target"]),
        "final_validation": {
            key: final[key] for key in (
                "database_sha256", "schema_version", "integrity_check",
                "manifest_sha256", "local_media_ok", "local_media_count",
                "local_media_bytes", "canary_sha256", "canary_ok",
                "all_owned", "all_readable",
            )
        },
        "production_activation_enabled": False,
    }


@host_locked
def verify_stage(
    *, plan_file: Path, stage_report: Path, stage_report_sha256: str,
    output: Path, runner=None, now=None,
) -> dict:
    """Re-read and validate the actual staged PIT volume without host mutation."""
    _, plan = _load_plan(plan_file, now=now, enforce_expiry=False)
    output_target = output.expanduser().resolve()
    repo = Path(plan["repo"]).expanduser().resolve()
    if output_target == repo or output_target.is_relative_to(repo):
        raise ValueError("verify report must be written outside the repository")
    output_target = _preflight_new_output(output_target, "verify report")
    report = _verify_staged(
        plan_file, stage_report, stage_report_sha256, runner or Runner(), now=now,
    )
    _write_exclusive(output_target, report)
    return report


@host_locked
def activate_plan(
    *, plan_file: Path, stage_report: Path, confirmation: str = "",
    activation_report: Path | None = None,
    service: str = "bibitasks-pilot.service", stage_report_sha256: str = "",
    runner=None, now=None,
) -> dict:
    """Compatibility API that always refuses production activation."""
    if not SERVICE_RE.fullmatch(service):
        raise ValueError("systemd service name is invalid")
    _verify_staged(
        plan_file, stage_report, stage_report_sha256, runner or Runner(), now=now,
    )
    raise RuntimeError(
        "production activation is disabled: this point-in-time backup does not "
        "prove reconciliation of post-backup ledger, withdrawals, payouts, inbox "
        "and outbox operations; no host state was changed"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--deploy-env", type=Path, required=True)
    plan_parser.add_argument("--repo", type=Path, required=True)
    plan_parser.add_argument(
        "--release-record", "--release-candidate", dest="release_record",
        type=Path, required=True,
    )
    plan_parser.add_argument(
        "--release-record-sha256", "--release-candidate-sha256",
        dest="release_record_sha256", required=True,
    )
    plan_parser.add_argument("--backup-dir", type=Path, required=True)
    plan_parser.add_argument("--target-volume")
    plan_parser.add_argument("--output", type=Path, required=True)
    plan_parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--confirm", required=True)
    apply_parser.add_argument("--stage-report", type=Path, required=True)
    apply_parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    apply_parser.add_argument(
        "--backup-key-file", type=Path,
        help="Explicit retained key file for the manifest key_version",
    )

    verify_parser = subparsers.add_parser("verify-stage")
    verify_parser.add_argument("--plan", type=Path, required=True)
    verify_parser.add_argument("--stage-report", type=Path, required=True)
    verify_parser.add_argument("--stage-report-sha256", required=True)
    verify_parser.add_argument("--output", type=Path, required=True)
    verify_parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)

    args = parser.parse_args()
    if args.command == "plan":
        value = build_plan(
            deploy_env=args.deploy_env, repo=args.repo,
            release_record=args.release_record,
            release_record_sha256=args.release_record_sha256,
            backup_dir=args.backup_dir, target_volume=args.target_volume,
            output=args.output, lock_file=args.lock_file,
        )
    elif args.command == "apply":
        value = apply_plan(
            plan_file=args.plan, confirmation=args.confirm,
            stage_report=args.stage_report, lock_file=args.lock_file,
            backup_key_file=args.backup_key_file,
        )
    else:
        value = verify_stage(
            plan_file=args.plan, stage_report=args.stage_report,
            stage_report_sha256=args.stage_report_sha256,
            output=args.output, lock_file=args.lock_file,
        )
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
