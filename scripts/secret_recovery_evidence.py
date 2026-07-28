"""Fail-closed offline evidence for BibiTasks recovery-key restoration.

Two independently stored, byte-identical age bundles are opened through stable
file descriptors and decrypted in memory.  The recovered Fernet keys must bind
to a pre-disaster canary and every retained encrypted database row.  Output is
aggregate evidence only: never plaintext, ciphertext, record IDs, key hashes,
identity contents, input paths, or subprocess stderr.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import selectors
import sqlite3
import stat
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Callable

from cryptography.fernet import Fernet, InvalidToken


# The recovery image must provision this exact binary ahead of the ceremony.
TRUSTED_AGE = Path("/usr/local/bin/age")
MAX_ENV_BYTES = 64 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_BUNDLE_BYTES = 1024 * 1024
MAX_IDENTITY_BYTES = 64 * 1024
AGE_TIMEOUT_SECONDS = 30
FRESHNESS = timedelta(hours=24)
FUTURE_SKEW = timedelta(minutes=5)
REPORT_VERSION = 2
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(
    r"^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[0-9a-f]{64}$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
REQUIRED_KEYS = ("TELEGRAM_INBOX_KEY", "WITHDRAW_ACCOUNT_KEY")
COUNT_FIELDS = (
    "telegram_ciphertext_count",
    "telegram_active_null_count",
    "withdrawal_ciphertext_count",
    "withdrawal_active_null_count",
)
SANITIZED_AGE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


@dataclass
class SecureFile:
    path: Path
    label: str
    fd: int
    info: os.stat_result

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def read(self, *, limit: int) -> bytes:
        if self.info.st_size > limit:
            raise ValueError(f"{self.label} is unexpectedly large")
        os.lseek(self.fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(self.fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise ValueError(f"{self.label} is unexpectedly large")
        self._unchanged()
        return raw

    def sha256(self) -> str:
        os.lseek(self.fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(self.fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        self._unchanged()
        return digest.hexdigest()

    def proc_path(self) -> str:
        if os.name != "posix" or not Path("/proc/self/fd").is_dir():
            raise RuntimeError("stable recovery descriptors require Linux /proc")
        return f"/proc/self/fd/{self.fd}"

    def _unchanged(self) -> None:
        current = os.fstat(self.fd)
        if (
            current.st_dev != self.info.st_dev
            or current.st_ino != self.info.st_ino
            or current.st_size != self.info.st_size
            or current.st_mtime_ns != self.info.st_mtime_ns
        ):
            raise ValueError(f"{self.label} changed during verification")


@dataclass(frozen=True)
class RecoveredKeys:
    telegram: Fernet
    telegram_text: bytes
    withdrawal: Fernet
    withdrawal_text: bytes


def open_secure(
    path: Path, label: str, *, private: bool = False,
    max_bytes: int | None = None,
) -> SecureFile:
    candidate = path.expanduser()
    try:
        before = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{label} must be a readable regular non-symlink file") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} must be a regular non-symlink file")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(candidate, flags)
    except OSError as exc:
        raise ValueError(f"{label} must be a readable regular non-symlink file") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if (
            info.st_dev != before.st_dev or info.st_ino != before.st_ino
            or info.st_size != before.st_size
        ):
            raise ValueError(f"{label} changed while it was opened")
        if info.st_size <= 0:
            raise ValueError(f"{label} must not be empty")
        if max_bytes is not None and info.st_size > max_bytes:
            raise ValueError(f"{label} is unexpectedly large")
        if private and os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError(f"{label} permissions must be 0600 or stricter")
        return SecureFile(candidate.resolve(), label, fd, info)
    except Exception:
        os.close(fd)
        raise


def _same_file(first: SecureFile, second: SecureFile) -> bool:
    return (
        first.info.st_dev == second.info.st_dev
        and first.info.st_ino == second.info.st_ino
    )


def _validate_trusted_age() -> None:
    if os.name != "posix":
        raise RuntimeError("trusted age recovery is supported only on Linux")
    if not TRUSTED_AGE.is_absolute():  # defensive constant check
        raise RuntimeError("trusted age path is not absolute")
    try:
        info = TRUSTED_AGE.lstat()
    except OSError as exc:
        raise RuntimeError("trusted age binary is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("trusted age binary must be a regular non-symlink file")
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError("trusted age binary has unsafe ownership or permissions")
    if not stat.S_IMODE(info.st_mode) & 0o111:
        raise RuntimeError("trusted age binary is not executable")


def _bounded_age_runner(command, *, env, timeout, pass_fds):
    """Run age with bounded output; stderr is intentionally discarded."""
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd="/", env=env, close_fds=True,
        pass_fds=pass_fds,
    )
    output = bytearray()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("age recovery timed out")
            events = selector.select(remaining)
            if not events:
                raise TimeoutError("age recovery timed out")
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), MAX_ENV_BYTES + 1 - len(output))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > MAX_ENV_BYTES:
                    raise ValueError("age recovery output is too large")
        remaining = max(0.0, deadline - time.monotonic())
        returncode = process.wait(timeout=remaining)
        return SimpleNamespace(returncode=returncode, stdout=bytes(output))
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()


def decrypt_bundle(
    bundle: SecureFile, identity: SecureFile, *,
    runner: Callable | None = None,
    age_validator: Callable[[], None] | None = None,
) -> bytes:
    (age_validator or _validate_trusted_age)()
    if os.name == "posix":
        identity_argument = identity.proc_path()
        bundle_argument = bundle.proc_path()
    elif runner is not None:
        # Unit-test seam only. Production validation rejects non-Linux hosts.
        identity_argument = str(identity.path)
        bundle_argument = str(bundle.path)
    else:  # pragma: no cover - guarded by the trusted-age validator
        raise RuntimeError("stable recovery descriptors require Linux /proc")
    command = [
        str(TRUSTED_AGE), "--decrypt", "--identity", identity_argument,
        bundle_argument,
    ]
    execute = runner or _bounded_age_runner
    try:
        result = execute(
            command, env=dict(SANITIZED_AGE_ENV), timeout=AGE_TIMEOUT_SECONDS,
            pass_fds=(identity.fd, bundle.fd),
        )
    except (OSError, subprocess.SubprocessError, TimeoutError, ValueError):
        raise ValueError("trusted age decryption could not complete") from None
    raw = result.stdout if isinstance(result.stdout, bytes) else bytes(result.stdout)
    if result.returncode != 0 or not raw or len(raw) > MAX_ENV_BYTES:
        raise ValueError("trusted age decryption failed")
    bundle._unchanged()
    identity._unchanged()
    return raw


def parse_recovered_keys(raw: bytes) -> RecoveredKeys:
    if not raw or len(raw) > MAX_ENV_BYTES or b"\x00" in raw:
        raise ValueError("recovered production env is invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("recovered production env must use UTF-8") from exc
    values: dict[str, str] = {}
    seen: set[str] = set()
    for number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"recovered production env line {number} is malformed")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError("recovered production env contains an invalid variable name")
        if name in seen:
            raise ValueError("recovered production env contains a duplicate variable")
        seen.add(name)
        if name in REQUIRED_KEYS:
            if not value or value[:1] in {'"', "'"} or any(c.isspace() for c in value):
                raise ValueError("recovered production env contains an invalid recovery key")
            values[name] = value
    if set(values) != set(REQUIRED_KEYS):
        raise ValueError("recovered production env lacks required recovery keys")
    try:
        inbox_raw = values["TELEGRAM_INBOX_KEY"].encode("ascii")
        withdrawal_raw = values["WITHDRAW_ACCOUNT_KEY"].encode("ascii")
        inbox_bytes = base64.b64decode(inbox_raw, altchars=b"-_", validate=True)
        withdrawal_bytes = base64.b64decode(
            withdrawal_raw, altchars=b"-_", validate=True,
        )
        inbox = Fernet(inbox_raw)
        withdrawal = Fernet(withdrawal_raw)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("recovered production env contains an invalid Fernet key") from exc
    if len(inbox_bytes) != 32 or len(withdrawal_bytes) != 32:
        raise ValueError("recovered production env contains an invalid Fernet key")
    if hmac.compare_digest(inbox_bytes, withdrawal_bytes):
        raise ValueError("recovered production keys must be distinct")
    return RecoveredKeys(inbox, inbox_raw, withdrawal, withdrawal_raw)


def _json_bytes(raw: bytes, label: str) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _count(value, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _timestamp(value, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _fresh(value, label: str, now: datetime) -> str:
    parsed = _timestamp(value, label)
    if parsed > now + FUTURE_SKEW or parsed < now - FRESHNESS:
        raise ValueError(f"{label} evidence is stale or from the future")
    return parsed.isoformat()


def _live_evidence(
    source: SecureFile, kind: str, *, now: datetime, release_version: str,
) -> dict:
    raw = source.read(limit=MAX_JSON_BYTES)
    value = _json_bytes(raw, f"{kind} report")
    generated_at = _fresh(value.get("generated_at"), f"{kind} report", now)
    if kind == "telegram_preflight":
        summary = value.get("summary")
        checks = value.get("checks")
        calculated = None
        if isinstance(checks, list) and all(isinstance(item, dict) for item in checks):
            calculated = {
                status: sum(item.get("status") == status for item in checks)
                for status in ("pass", "warn", "fail")
            }
        green = (
            value.get("report_version") == 1
            and value.get("ok") is True and isinstance(summary, dict)
            and all(type(summary.get(key)) is int and summary[key] >= 0
                    for key in ("pass", "warn", "fail"))
            and summary["fail"] == 0
            and calculated == {
                key: summary[key] for key in ("pass", "warn", "fail")
            }
        )
    elif kind == "readiness":
        green = (
            value.get("report_version") == 1 and value.get("ok") is True
            and value.get("application_version") == release_version
            and value.get("telegram_update_mode") == "webhook"
            and value.get("telegram_receiver_ready") is True
            and value.get("webhook_configured") is True
            and value.get("lifecycle_worker_alive") is True
            and value.get("outbox_worker_alive") is True
            and value.get("telegram_inbox_worker_alive") is True
            and value.get("withdrawal_encryption_ready") is True
            and value.get("telegram_inbox_encryption_ready") is True
            and type(value.get("outbox_dead")) is int and value["outbox_dead"] == 0
            and type(value.get("telegram_inbox_dead")) is int
            and value["telegram_inbox_dead"] == 0
        )
    elif kind == "monitor_canary":
        checks = value.get("checks")
        green = (
            value.get("schema_version") == 1
            and value.get("ok") is True and value.get("heartbeat_ok") is True
            and value.get("alert_delivery_ok") is True and isinstance(checks, dict)
        )
        if green:
            for key in ("application", "dead_queues", "backup"):
                item = checks.get(key)
                green = bool(
                    green and isinstance(item, dict)
                    and item.get("last_healthy") is True
                    and item.get("alert_active") is False
                )
            for key in ("application", "backup"):
                item = checks.get(key) if isinstance(checks, dict) else None
                if not isinstance(item, dict):
                    green = False
                    continue
                incident = _timestamp(
                    item.get("last_incident_delivered_at"), f"monitor {key} incident",
                )
                recovery = _timestamp(
                    item.get("last_recovery_delivered_at"), f"monitor {key} recovery",
                )
                if (
                    recovery < incident or incident < now - FRESHNESS
                    or recovery < now - FRESHNESS
                    or incident > now + FUTURE_SKEW or recovery > now + FUTURE_SKEW
                ):
                    green = False
    else:  # pragma: no cover - internal constant contract
        raise ValueError("unknown live evidence type")
    if not green:
        raise ValueError(f"{kind} report is not green")
    return {"present": True, "green": True, "generated_at": generated_at,
            "sha256": _digest(raw)}


def _sqlite_uri(database: SecureFile) -> str:
    if os.name == "posix":
        return f"file:{database.proc_path()}?mode=ro&immutable=1"
    # Development tests only; production recovery is Linux and uses the stable fd.
    return f"{database.path.as_uri()}?mode=ro&immutable=1"


def verify_database(
    database: SecureFile, keys: RecoveredKeys,
    *, schema_version: int, expected_counts: dict[str, int], expected_sha256: str,
) -> dict:
    if database.sha256() != expected_sha256:
        raise ValueError("restored database digest differs from backup manifest")
    try:
        with closing(sqlite3.connect(_sqlite_uri(database), uri=True, timeout=5)) as db:
            with closing(db.execute("PRAGMA integrity_check")) as cursor:
                integrity = cursor.fetchone()
            if not integrity or integrity[0] != "ok":
                raise ValueError("restored database failed integrity_check")
            with closing(db.execute("PRAGMA user_version")) as cursor:
                row = cursor.fetchone()
            actual_schema = int(row[0]) if row else -1
            if actual_schema != schema_version:
                raise ValueError("restored database schema differs from release")

            queries = {
                "telegram_ciphertext_count": (
                    "SELECT COUNT(*) FROM telegram_update_inbox "
                    "WHERE payload_json IS NOT NULL"
                ),
                "telegram_active_null_count": (
                    "SELECT COUNT(*) FROM telegram_update_inbox "
                    "WHERE status IN ('pending','processing') AND payload_json IS NULL"
                ),
                "withdrawal_ciphertext_count": (
                    "SELECT COUNT(*) FROM withdrawal_requests "
                    "WHERE account_ciphertext IS NOT NULL"
                ),
                "withdrawal_active_null_count": (
                    "SELECT COUNT(*) FROM withdrawal_requests "
                    "WHERE status IN ('pending','processing') AND account_ciphertext IS NULL"
                ),
            }
            actual_counts = {}
            for name, query in queries.items():
                with closing(db.execute(query)) as cursor:
                    actual_counts[name] = int(cursor.fetchone()[0])
            if actual_counts != expected_counts:
                raise ValueError("restored encrypted-row counts differ from backup manifest")
            if (
                actual_counts["telegram_active_null_count"] != 0
                or actual_counts["withdrawal_active_null_count"] != 0
            ):
                raise ValueError("active recovery-sensitive rows are missing ciphertext")

            telegram_verified = 0
            with closing(db.execute(
                "SELECT update_id,payload_json,payload_sha256 "
                "FROM telegram_update_inbox WHERE payload_json IS NOT NULL"
            )) as cursor:
                for update_id, ciphertext, fingerprint in cursor:
                    try:
                        plaintext = keys.telegram.decrypt(
                            str(ciphertext).encode("ascii")
                        ).decode("utf-8")
                        payload = json.loads(plaintext)
                    except (InvalidToken, ValueError, TypeError, UnicodeError,
                            json.JSONDecodeError) as exc:
                        raise ValueError("Telegram ciphertext verification failed") from exc
                    if (
                        not isinstance(payload, dict)
                        or type(payload.get("update_id")) is not int
                        or type(update_id) is not int
                        or payload["update_id"] != update_id
                    ):
                        raise ValueError("Telegram ciphertext row binding failed")
                    canonical = json.dumps(
                        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    )
                    expected = "h1:" + hmac.new(
                        keys.telegram_text, canonical.encode("utf-8"), hashlib.sha256,
                    ).hexdigest()
                    if not isinstance(fingerprint, str) or not hmac.compare_digest(
                        fingerprint, expected,
                    ):
                        raise ValueError("Telegram ciphertext fingerprint mismatch")
                    telegram_verified += 1

            withdrawal_verified = 0
            with closing(db.execute(
                "SELECT account_ciphertext,account_fingerprint "
                "FROM withdrawal_requests WHERE account_ciphertext IS NOT NULL"
            )) as cursor:
                for ciphertext, fingerprint in cursor:
                    try:
                        plaintext = keys.withdrawal.decrypt(
                            str(ciphertext).encode("ascii")
                        ).decode("utf-8")
                    except (InvalidToken, ValueError, TypeError, UnicodeError) as exc:
                        raise ValueError("withdrawal ciphertext verification failed") from exc
                    if not plaintext or "\x00" in plaintext:
                        raise ValueError("withdrawal plaintext shape is invalid")
                    expected = hmac.new(
                        keys.withdrawal_text, plaintext.casefold().encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()
                    if not isinstance(fingerprint, str) or not hmac.compare_digest(
                        fingerprint, expected,
                    ):
                        raise ValueError("withdrawal ciphertext fingerprint mismatch")
                    withdrawal_verified += 1
    except sqlite3.Error as exc:
        raise ValueError("restored database could not be safely verified") from exc
    if (
        telegram_verified != expected_counts["telegram_ciphertext_count"]
        or withdrawal_verified != expected_counts["withdrawal_ciphertext_count"]
    ):
        raise ValueError("verified ciphertext counts differ from backup manifest")
    final_digest = database.sha256()
    if final_digest != expected_sha256:
        raise ValueError("restored database changed during verification")
    return {
        "sha256": final_digest,
        "integrity_check_ok": True,
        "schema_version": schema_version,
        "expected_counts_verified": True,
        "telegram_ciphertext_expected": expected_counts["telegram_ciphertext_count"],
        "telegram_ciphertext_verified": telegram_verified,
        "telegram_active_null_expected": expected_counts["telegram_active_null_count"],
        "telegram_active_null_verified": actual_counts["telegram_active_null_count"],
        "telegram_row_binding_verified": True,
        "telegram_hmac_verified": True,
        "withdrawal_ciphertext_expected": expected_counts["withdrawal_ciphertext_count"],
        "withdrawal_ciphertext_verified": withdrawal_verified,
        "withdrawal_active_null_expected": expected_counts["withdrawal_active_null_count"],
        "withdrawal_active_null_verified": actual_counts["withdrawal_active_null_count"],
        "withdrawal_hmac_verified": True,
    }


def build_report(
    *, encrypted_recovery_bundles: list[Path], age_identity_file: Path,
    database: Path, recovery_key_canaries: Path, backup_manifest: Path,
    restore_report: Path, commit: str, image: str, schema_version: int,
    release_version: str, preflight_report: Path, readiness_report: Path,
    monitor_canary_report: Path, now: datetime | None = None,
    age_runner: Callable | None = None,
    age_validator: Callable[[], None] | None = None,
) -> dict:
    commit = str(commit or "").strip().lower()
    image = str(image or "").strip().lower()
    release_version = str(release_version or "").strip()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not COMMIT_RE.fullmatch(commit):
        raise ValueError("commit must be a full 40-character SHA")
    if not IMAGE_RE.fullmatch(image):
        raise ValueError("image must be an immutable lowercase GHCR digest reference")
    if type(schema_version) is not int or schema_version <= 0:
        raise ValueError("schema version must be a positive integer")
    if not release_version or len(release_version) > 160 or any(
        ord(character) < 32 or ord(character) == 127 for character in release_version
    ):
        raise ValueError("release version has an unsafe shape")
    if len(encrypted_recovery_bundles) != 2:
        raise ValueError("exactly two encrypted recovery bundles are required")

    with ExitStack() as stack:
        bundles = [
            stack.enter_context(open_secure(
                item, f"encrypted recovery bundle {index}",
                max_bytes=MAX_BUNDLE_BYTES,
            ))
            for index, item in enumerate(encrypted_recovery_bundles, 1)
        ]
        identity = stack.enter_context(open_secure(
            age_identity_file, "age identity", private=True,
            max_bytes=MAX_IDENTITY_BYTES,
        ))
        db_file = stack.enter_context(open_secure(database, "restored database"))
        canary_file = stack.enter_context(open_secure(
            recovery_key_canaries, "recovery-key canary", max_bytes=MAX_JSON_BYTES,
        ))
        manifest_file = stack.enter_context(open_secure(
            backup_manifest, "backup manifest", max_bytes=MAX_JSON_BYTES,
        ))
        restore_file = stack.enter_context(open_secure(
            restore_report, "restore report", max_bytes=MAX_JSON_BYTES,
        ))
        preflight_file = stack.enter_context(open_secure(
            preflight_report, "Telegram preflight report", max_bytes=MAX_JSON_BYTES,
        ))
        readiness_file = stack.enter_context(open_secure(
            readiness_report, "readiness report", max_bytes=MAX_JSON_BYTES,
        ))
        monitor_file = stack.enter_context(open_secure(
            monitor_canary_report, "monitor canary report", max_bytes=MAX_JSON_BYTES,
        ))

        if _same_file(bundles[0], bundles[1]):
            raise ValueError("encrypted recovery bundles must be distinct files")
        bundle_digests = [item.sha256() for item in bundles]
        bundle_sizes = [item.info.st_size for item in bundles]
        if bundle_digests[0] != bundle_digests[1] or bundle_sizes[0] != bundle_sizes[1]:
            raise ValueError("encrypted recovery bundle copies are not byte-identical")
        recovered = [
            decrypt_bundle(
                item, identity, runner=age_runner, age_validator=age_validator,
            )
            for item in bundles
        ]
        if not hmac.compare_digest(recovered[0], recovered[1]):
            raise ValueError("encrypted recovery bundle decryptions differ")
        keys = parse_recovered_keys(recovered[0])

        manifest_raw = manifest_file.read(limit=MAX_JSON_BYTES)
        restore_raw = restore_file.read(limit=MAX_JSON_BYTES)
        canary_raw = canary_file.read(limit=MAX_JSON_BYTES)
        manifest = _json_bytes(manifest_raw, "backup manifest")
        restore = _json_bytes(restore_raw, "restore report")
        database_meta = manifest.get("database")
        canary_meta = manifest.get("recovery_key_canary")
        if not isinstance(database_meta, dict) or not isinstance(canary_meta, dict):
            raise ValueError("backup manifest lacks recovery metadata")
        if database_meta.get("path") != "bibitasks.db":
            raise ValueError("backup manifest database path is invalid")
        database_sha = str(database_meta.get("sha256", "")).lower()
        if not SHA256_RE.fullmatch(database_sha):
            raise ValueError("backup manifest database digest is invalid")
        if _count(database_meta.get("bytes"), "database bytes") != db_file.info.st_size:
            raise ValueError("restored database size differs from backup manifest")
        if database_meta.get("integrity_check") != "ok":
            raise ValueError("backup manifest does not prove database integrity")
        if database_meta.get("schema_version") != schema_version:
            raise ValueError("backup manifest schema differs from release")
        expected_counts = {
            name: _count(database_meta.get(name), f"manifest {name}")
            for name in COUNT_FIELDS
        }
        if (
            expected_counts["telegram_active_null_count"] != 0
            or expected_counts["withdrawal_active_null_count"] != 0
        ):
            raise ValueError("backup manifest contains active NULL recovery rows")

        canary_sha = _digest(canary_raw)
        if set(canary_meta) != {"path", "bytes", "sha256"}:
            raise ValueError("backup manifest canary metadata is invalid")
        if (
            canary_meta.get("path") != "recovery-key-canaries.json"
            or canary_meta.get("bytes") != len(canary_raw)
            or canary_meta.get("sha256") != canary_sha
        ):
            raise ValueError("recovery-key canary differs from backup manifest")
        try:
            # Root is inserted for direct ``python scripts/...`` execution.
            repository_root = Path(__file__).resolve().parents[1]
            if str(repository_root) not in sys.path:
                sys.path.insert(0, str(repository_root))
            try:
                from scripts.recovery_key_canary import verify_canary_bytes
            except ImportError:  # direct execution from the scripts directory
                from recovery_key_canary import verify_canary_bytes
            verify_canary_bytes(canary_raw, keys.telegram, keys.withdrawal)
        except (ImportError, ValueError) as exc:
            raise ValueError("pre-disaster recovery-key canary verification failed") from exc

        manifest_sha = _digest(manifest_raw)
        restored_canary = restore.get("recovery_key_canary")
        if (
            restore.get("source_manifest_sha256") != manifest_sha
            or restore.get("database_sha256_after_restore") != database_sha
            or restore.get("schema_version") != schema_version
            or restore.get("integrity_check") != "ok"
            or not isinstance(restored_canary, dict)
            or restored_canary != {"sha256": canary_sha, "ok": True}
        ):
            raise ValueError("restore report does not bind manifest, database and canary")

        database_result = verify_database(
            db_file, keys, schema_version=schema_version,
            expected_counts=expected_counts, expected_sha256=database_sha,
        )
        live = {
            "telegram_preflight": _live_evidence(
                preflight_file, "telegram_preflight", now=current,
                release_version=release_version,
            ),
            "readiness": _live_evidence(
                readiness_file, "readiness", now=current,
                release_version=release_version,
            ),
            "monitor_canary": _live_evidence(
                monitor_file, "monitor_canary", now=current,
                release_version=release_version,
            ),
        }
        stable_documents = (
            (manifest_file, manifest_sha),
            (restore_file, _digest(restore_raw)),
            (canary_file, canary_sha),
            (preflight_file, live["telegram_preflight"]["sha256"]),
            (readiness_file, live["readiness"]["sha256"]),
            (monitor_file, live["monitor_canary"]["sha256"]),
        )
        if any(source.sha256() != expected for source, expected in stable_documents):
            raise ValueError("recovery evidence input changed during verification")
        final_bundle_digests = [item.sha256() for item in bundles]
        if final_bundle_digests != bundle_digests:
            raise ValueError("encrypted recovery bundles changed during verification")

    image_digest = image.rsplit("@sha256:", 1)[1]
    return {
        "report_version": REPORT_VERSION,
        "generated_at": current.isoformat(),
        "ok": True,
        "release": {
            "commit_sha": commit,
            "immutable_image_sha256": image_digest,
            "immutable_image_reference_sha256": _digest(image.encode("utf-8")),
            "release_version_sha256": _digest(release_version.encode("utf-8")),
            "schema_version": schema_version,
        },
        "encrypted_recovery_bundle": {
            "format": "age-v1",
            "sha256": bundle_digests[0],
            "bytes": bundle_sizes[0],
            "copy_count_verified": 2,
            "distinct_files_verified": True,
            "byte_identical_verified": True,
            "decryption_verified_count": 2,
        },
        "keys": {
            "valid_distinct_fernet_keys": True,
            "pre_disaster_canary_verified": True,
        },
        "backup": {
            "manifest_sha256": manifest_sha,
            "database_binding_verified": True,
            "canary_binding_verified": True,
            "expected_counts_present": True,
        },
        "restore": {
            "report_sha256": _digest(restore_raw),
            "manifest_binding_verified": True,
            "database_binding_verified": True,
            "canary_binding_verified": True,
        },
        "recovery_key_canary": {
            "version": 1,
            "sha256": canary_sha,
            "bytes": len(canary_raw),
            "format_verified": True,
            "key_binding_verified": True,
        },
        "database": database_result,
        "live_evidence": {"required": True, "max_age_seconds": 86400, **live},
        "operator_assertions": {
            "custodian_quorum_cryptographically_verified": False,
        },
    }


def write_report(path: Path, report: dict) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise FileExistsError("secret-recovery evidence target already exists")
    target = candidate.resolve()
    repository_root = Path(__file__).resolve().parents[1]
    try:
        target.relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise ValueError("refusing to write secret-recovery evidence inside repository")
    parent = target.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("output parent must be an existing non-symlink directory")
    if os.name != "nt" and stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise ValueError("output parent permissions must be 0700 or stricter")
    if target.exists() or target.is_symlink():
        raise FileExistsError("secret-recovery evidence target already exists")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(target, 0o600)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create fail-closed offline BibiTasks recovery-key evidence",
    )
    parser.add_argument(
        "--encrypted-recovery-bundle", action="append", type=Path, required=True,
        help="exactly two independent, byte-identical age bundle paths",
    )
    parser.add_argument("--age-identity-file", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--recovery-key-canaries", type=Path, required=True)
    parser.add_argument("--backup-manifest", type=Path, required=True)
    parser.add_argument("--restore-report", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--schema-version", type=int, required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--preflight-report", type=Path, required=True)
    parser.add_argument("--readiness-report", type=Path, required=True)
    parser.add_argument("--monitor-canary-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_report(
            encrypted_recovery_bundles=args.encrypted_recovery_bundle,
            age_identity_file=args.age_identity_file, database=args.database,
            recovery_key_canaries=args.recovery_key_canaries,
            backup_manifest=args.backup_manifest, restore_report=args.restore_report,
            commit=args.commit, image=args.image, schema_version=args.schema_version,
            release_version=args.release_version,
            preflight_report=args.preflight_report,
            readiness_report=args.readiness_report,
            monitor_canary_report=args.monitor_canary_report,
        )
        write_report(args.output, report)
    except (ValueError, RuntimeError, FileExistsError, OSError) as exc:
        # Never print exception text: it may originate in an untrusted input.
        print(f"secret recovery evidence failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    print("secret recovery evidence written")


if __name__ == "__main__":
    main()
