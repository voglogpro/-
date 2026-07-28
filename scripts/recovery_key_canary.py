"""Persistent, non-secret proof that the configured recovery keys still match.

The canary contains only Fernet ciphertexts and a random public nonce.  It is
created once in ``DATA_DIR`` by the application keys and is subsequently
verified before the database is opened.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


CANARY_FILENAME = "recovery-key-canaries.json"
CANARY_VERSION = 1
CANARY_DOMAIN = "bibitasks.recovery-key-canary"
CANARY_PURPOSE = "pre-disaster-key-binding"
_FIELDS = {
    "version", "domain", "purpose", "nonce",
    "telegram_inbox_ciphertext", "withdraw_account_ciphertext",
}
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# One-time bridge only: v2.9.1 is schema 293, while the destination release
# migrates to schema 295 after this canary has been enrolled.
LEGACY_V291_ENROLL_SCHEMA_VERSION = 293


def _plaintext(nonce: str, role: str) -> bytes:
    return json.dumps({
        "domain": CANARY_DOMAIN,
        "nonce": nonce,
        "purpose": CANARY_PURPOSE,
        "role": role,
        "version": CANARY_VERSION,
    }, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _canonical_bytes(document: dict) -> bytes:
    return (
        json.dumps(
            document, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ) + "\n"
    ).encode("ascii")


def _valid_fernet_token(value: object) -> bool:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        return False
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeError):
        return False
    # version + timestamp + IV + at least one AES block + HMAC
    return len(decoded) >= 73 and decoded[0] == 0x80


def validate_canary_bytes(raw: bytes) -> dict:
    """Validate the public format and canonical representation, without keys."""
    try:
        document = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Recovery-key canary is not valid canonical JSON") from exc
    if not isinstance(document, dict) or set(document) != _FIELDS:
        raise ValueError("Recovery-key canary has an invalid schema")
    if (
        type(document["version"]) is not int
        or document["version"] != CANARY_VERSION
        or document["domain"] != CANARY_DOMAIN
        or document["purpose"] != CANARY_PURPOSE
        or not isinstance(document["nonce"], str)
        or not _NONCE_RE.fullmatch(document["nonce"])
        or not _valid_fernet_token(document["telegram_inbox_ciphertext"])
        or not _valid_fernet_token(document["withdraw_account_ciphertext"])
    ):
        raise ValueError("Recovery-key canary contains invalid values")
    try:
        nonce = base64.urlsafe_b64decode(document["nonce"] + "=")
    except ValueError as exc:
        raise ValueError("Recovery-key canary nonce is invalid") from exc
    if len(nonce) != 32 or _canonical_bytes(document) != raw:
        raise ValueError("Recovery-key canary is not canonical")
    return document


def verify_canary_bytes(raw: bytes, telegram: Fernet, withdrawal: Fernet) -> dict:
    document = validate_canary_bytes(raw)
    nonce = document["nonce"]
    bindings = (
        ("telegram_inbox_ciphertext", telegram, "telegram-inbox"),
        ("withdraw_account_ciphertext", withdrawal, "withdraw-account"),
    )
    try:
        for field, cipher, role in bindings:
            recovered = cipher.decrypt(document[field].encode("ascii"))
            if not secrets.compare_digest(recovered, _plaintext(nonce, role)):
                raise ValueError("Recovery-key canary plaintext binding is invalid")
    except InvalidToken as exc:
        raise ValueError("Configured recovery key does not match persistent canary") from exc
    return document


def _read_safe(path: Path, *, production: bool) -> bytes:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("Recovery-key canary must be a regular non-symlink file")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        after = path.lstat()
        identity = lambda value: (value.st_dev, value.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or identity(before) != identity(opened)
            or identity(after) != identity(opened)
        ):
            raise ValueError("Recovery-key canary changed during secure open")
        if production and opened.st_nlink != 1:
            raise ValueError("Recovery-key canary must have exactly one hard link")
        if os.name != "nt" and stat.S_IMODE(opened.st_mode) != 0o600:
            if production:
                raise PermissionError("Recovery-key canary must have mode 0600")
            os.fchmod(descriptor, 0o600)
        if os.name != "nt" and production and opened.st_uid != os.geteuid():
            raise PermissionError("Recovery-key canary must be owned by the app user")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read()
    finally:
        os.close(descriptor)


def _new_document(telegram: Fernet, withdrawal: Fernet) -> dict:
    nonce = secrets.token_urlsafe(32)
    return {
        "version": CANARY_VERSION,
        "domain": CANARY_DOMAIN,
        "purpose": CANARY_PURPOSE,
        "nonce": nonce,
        "telegram_inbox_ciphertext": telegram.encrypt(
            _plaintext(nonce, "telegram-inbox")
        ).decode("ascii"),
        "withdraw_account_ciphertext": withdrawal.encrypt(
            _plaintext(nonce, "withdraw-account")
        ).decode("ascii"),
    }


def _fernet_instance_material(value: Fernet) -> bytes:
    try:
        material = bytes(value._signing_key) + bytes(value._encryption_key)
    except (AttributeError, TypeError) as exc:
        raise ValueError("Valid Fernet recovery keys are required") from exc
    if len(material) != 32:
        raise ValueError("Valid Fernet recovery keys are required")
    return material


def require_independent_fernet_instances(telegram: Fernet, withdrawal: Fernet) -> None:
    if (
        not isinstance(telegram, Fernet) or not isinstance(withdrawal, Fernet)
        or secrets.compare_digest(
            _fernet_instance_material(telegram),
            _fernet_instance_material(withdrawal),
        )
    ):
        raise ValueError("Recovery keys must contain independent key material")


def _publish_new_canary(
    root: Path, telegram: Fernet, withdrawal: Fernet, *, fail_if_exists: bool,
    raw: bytes | None = None,
) -> tuple[Path, bytes, os.stat_result]:
    path = root / CANARY_FILENAME
    require_independent_fernet_instances(telegram, withdrawal)
    raw = raw if raw is not None else _canonical_bytes(_new_document(telegram, withdrawal))
    verify_canary_bytes(raw, telegram, withdrawal)
    temp = root / f".{CANARY_FILENAME}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temp, flags, 0o600)
    created_info = None
    published = False
    publication_error = None
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if os.name != "nt":
            temp.chmod(0o600, follow_symlinks=False)
        created_info = temp.lstat()
        try:
            # Atomic create-only publication: a concurrent writer can never be
            # overwritten, nor silently accepted by explicit enrollment.
            os.link(temp, path, follow_symlinks=False)
            published = True
        except FileExistsError:
            if fail_if_exists:
                raise FileExistsError("Recovery-key canary already exists")
        if os.name != "nt":
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception as exc:
        publication_error = exc
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    if publication_error is not None:
        if published and created_info is not None:
            _unlink_exact(path, created_info, "new recovery-key canary")
        raise publication_error
    try:
        stored = _read_safe(path, production=True)
        verify_canary_bytes(stored, telegram, withdrawal)
        info = path.lstat()
        if info.st_nlink != 1:
            raise ValueError("Published recovery-key canary has an unsafe link count")
        return path, stored, info
    except Exception:
        if published and created_info is not None:
            _unlink_exact(path, created_info, "new recovery-key canary")
        raise


def _production_directory_is_new(root: Path) -> bool:
    """Allow first boot only; never mint recovery proof beside existing data."""
    for entry in root.iterdir():
        # main.py creates this empty directory while loading configuration,
        # before the startup canary check runs.
        if entry.name == "task_photos":
            try:
                if (
                    not entry.is_symlink()
                    and entry.is_dir()
                    and next(entry.iterdir(), None) is None
                ):
                    continue
            except OSError:
                pass
        return False
    return True


def ensure_recovery_key_canary(
    data_dir: str | Path, telegram: Fernet, withdrawal: Fernet, *,
    production: bool = True,
) -> Path:
    """Create-once atomically, or verify, the persistent key-binding canary."""
    require_independent_fernet_instances(telegram, withdrawal)
    root = Path(data_dir)
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("DATA_DIR must be a regular non-symlink directory")
    if os.name != "nt" and production:
        if stat.S_IMODE(root_info.st_mode) != 0o700:
            raise PermissionError("DATA_DIR must have mode 0700 in production")
        if root_info.st_uid != os.geteuid():
            raise PermissionError("DATA_DIR must be owned by the app user")
    path = root / CANARY_FILENAME
    try:
        raw = _read_safe(path, production=production)
    except FileNotFoundError:
        if production and not _production_directory_is_new(root):
            raise RuntimeError(
                "Recovery-key canary is missing beside existing production data"
            )
        _, raw, _ = _publish_new_canary(
            root, telegram, withdrawal, fail_if_exists=False,
        )
    verify_canary_bytes(raw, telegram, withdrawal)
    return path


def _open_stable_regular(path: Path, label: str) -> tuple[int, os.stat_result]:
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} must be a regular non-symlink file")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        after = path.lstat()
        identity = lambda value: (value.st_dev, value.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode) or stat.S_ISLNK(after.st_mode)
            or identity(before) != identity(opened)
            or identity(after) != identity(opened)
        ):
            raise ValueError(f"{label} changed during secure open")
        if opened.st_nlink != 1:
            raise ValueError(f"{label} must have exactly one hard link")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def _read_explicit_private_file(path: Path, label: str, *, limit: int) -> bytes:
    descriptor, opened = _open_stable_regular(path, label)
    try:
        if os.name != "nt":
            if opened.st_uid not in {0, os.geteuid()}:
                raise PermissionError(f"{label} has an untrusted owner")
            if stat.S_IMODE(opened.st_mode) & 0o077:
                raise PermissionError(f"{label} must not be accessible by group/others")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(limit + 1)
        if len(raw) > limit:
            raise ValueError(f"{label} is unexpectedly large")
        current = os.fstat(descriptor)
        if (
            current.st_size != opened.st_size
            or current.st_mtime_ns != opened.st_mtime_ns
        ):
            raise ValueError(f"{label} changed while it was read")
        return raw
    finally:
        os.close(descriptor)


def _fernet_pair_from_explicit_source(
    *, env_file: Path | None, telegram_key_file: Path | None,
    withdrawal_key_file: Path | None,
) -> tuple[Fernet, Fernet, bytes, bytes]:
    if env_file is not None:
        if telegram_key_file is not None or withdrawal_key_file is not None:
            raise ValueError("Use either --env-file or both explicit key files")
        raw = _read_explicit_private_file(env_file, "key env file", limit=64 * 1024)
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError("Key env file is not valid UTF-8") from exc
        values: dict[str, str] = {}
        wanted = {"TELEGRAM_INBOX_KEY", "WITHDRAW_ACCOUNT_KEY"}
        for source_line in text.splitlines():
            line = source_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, separator, value = line.partition("=")
            name = name.strip()
            if not separator or name not in wanted:
                continue
            if name in values:
                raise ValueError("Key env file contains a duplicate recovery key")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[name] = value
        if set(values) != wanted:
            raise ValueError("Key env file lacks both recovery keys")
        telegram_raw = values["TELEGRAM_INBOX_KEY"].encode("ascii", "strict")
        withdrawal_raw = values["WITHDRAW_ACCOUNT_KEY"].encode("ascii", "strict")
    else:
        if telegram_key_file is None or withdrawal_key_file is None:
            raise ValueError("Both explicit recovery key files are required")
        telegram_raw = _read_explicit_private_file(
            telegram_key_file, "Telegram key file", limit=1024,
        ).strip()
        withdrawal_raw = _read_explicit_private_file(
            withdrawal_key_file, "withdrawal key file", limit=1024,
        ).strip()
        if b"\n" in telegram_raw or b"\r" in telegram_raw:
            raise ValueError("Telegram key file must contain one key")
        if b"\n" in withdrawal_raw or b"\r" in withdrawal_raw:
            raise ValueError("Withdrawal key file must contain one key")
    if not telegram_raw or not withdrawal_raw:
        raise ValueError("Recovery keys must be present and independent")
    try:
        telegram_material = base64.urlsafe_b64decode(telegram_raw)
        withdrawal_material = base64.urlsafe_b64decode(withdrawal_raw)
        telegram = Fernet(telegram_raw)
        withdrawal = Fernet(withdrawal_raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("Explicit recovery key source contains an invalid Fernet key") from exc
    if (
        len(telegram_material) != 32 or len(withdrawal_material) != 32
        or secrets.compare_digest(telegram_material, withdrawal_material)
    ):
        raise ValueError("Recovery keys must contain independent key material")
    return telegram, withdrawal, telegram_raw, withdrawal_raw


def _digest_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _bytes_from_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _require_no_sqlite_sidecars(root: Path) -> None:
    for suffix in ("-wal", "-shm"):
        try:
            (root / f"bibitasks.db{suffix}").lstat()
        except FileNotFoundError:
            continue
        raise RuntimeError("Database WAL/SHM state exists; stop writers and checkpoint first")


def _unlink_exact(path: Path, expected: os.stat_result, label: str) -> None:
    """Remove only the exact regular inode created by this enrollment process."""
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)
        or not _same_file(current, expected) or current.st_nlink != 1
    ):
        raise RuntimeError(f"Refusing to remove replaced {label}")
    if os.name == "nt":
        path.unlink()
        return
    parent_fd = os.open(path.parent, os.O_RDONLY)
    try:
        if os.unlink in os.supports_dir_fd:
            at = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_file(at, expected) or stat.S_ISLNK(at.st_mode):
                raise RuntimeError(f"Refusing to remove replaced {label}")
            os.unlink(path.name, dir_fd=parent_fd)
        else:  # Windows has no dir_fd unlink; the private parent limits races.
            path.unlink()
        if os.name != "nt":
            os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _verify_existing_ciphertexts(
    db: sqlite3.Connection, telegram: Fernet, withdrawal: Fernet,
    telegram_raw: bytes, withdrawal_raw: bytes,
) -> None:
    try:
        telegram_active_null = int(db.execute(
            "SELECT COUNT(*) FROM telegram_update_inbox "
            "WHERE status IN ('pending','processing') AND payload_json IS NULL"
        ).fetchone()[0])
        withdrawal_active_null = int(db.execute(
            "SELECT COUNT(*) FROM withdrawal_requests "
            "WHERE status IN ('pending','processing') AND account_ciphertext IS NULL"
        ).fetchone()[0])
        inbox_rows = db.execute(
            "SELECT update_id,payload_json,payload_sha256 "
            "FROM telegram_update_inbox WHERE payload_json IS NOT NULL"
        ).fetchall()
        withdrawal_rows = db.execute(
            "SELECT account_ciphertext,account_fingerprint "
            "FROM withdrawal_requests WHERE account_ciphertext IS NOT NULL"
        ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError("Database lacks the v2.9.1 recovery-key tables") from exc
    if telegram_active_null or withdrawal_active_null:
        raise RuntimeError("Active recovery-sensitive rows are missing ciphertext")
    try:
        for update_id, ciphertext, fingerprint in inbox_rows:
            plaintext = telegram.decrypt(str(ciphertext).encode("ascii")).decode("utf-8")
            payload = json.loads(plaintext)
            if (
                not isinstance(payload, dict)
                or type(payload.get("update_id")) is not int
                or int(payload["update_id"]) != int(update_id)
            ):
                raise ValueError("Telegram ciphertext payload has an invalid shape")
            canonical = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            expected = "h1:" + hmac.new(
                telegram_raw, canonical.encode("utf-8"), hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(str(fingerprint or ""), expected):
                raise ValueError("Telegram ciphertext fingerprint mismatch")
        for ciphertext, fingerprint in withdrawal_rows:
            plaintext = withdrawal.decrypt(
                str(ciphertext).encode("ascii")
            ).decode("utf-8")
            normalized = " ".join(plaintext.strip().split())
            if (
                normalized != plaintext or not 3 <= len(plaintext) <= 100
                or any(ord(char) < 32 for char in plaintext)
            ):
                raise ValueError("Withdrawal ciphertext plaintext has an invalid shape")
            expected = hmac.new(
                withdrawal_raw, plaintext.casefold().encode("utf-8"), hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(str(fingerprint or ""), expected):
                raise ValueError("Withdrawal ciphertext fingerprint mismatch")
    except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Recovery keys do not verify all encrypted database rows") from exc


def _validate_enrollment_database(
    root: Path, expected_sha256: str, telegram: Fernet, withdrawal: Fernet,
    telegram_raw: bytes, withdrawal_raw: bytes,
    publish,
) -> tuple[str, int, dict]:
    if not _SHA256_RE.fullmatch(str(expected_sha256 or "")):
        raise ValueError("Database confirmation must be 64 lowercase SHA-256 characters")
    database = root / "bibitasks.db"
    _require_no_sqlite_sidecars(root)
    descriptor, opened = _open_stable_regular(database, "SQLite database")
    try:
        if os.name != "nt":
            if opened.st_uid != os.geteuid():
                raise PermissionError("SQLite database must be owned by the enrollment user")
            if stat.S_IMODE(opened.st_mode) & 0o077:
                raise PermissionError("SQLite database must not be accessible by group/others")
        database_bytes = _bytes_from_descriptor(descriptor)
        digest_before = hashlib.sha256(database_bytes).hexdigest()
        if not secrets.compare_digest(digest_before, expected_sha256):
            raise ValueError("Database SHA-256 confirmation does not match current bytes")
        with closing(sqlite3.connect(":memory:")) as db:
            try:
                db.deserialize(database_bytes)
            except (AttributeError, sqlite3.Error) as exc:
                raise RuntimeError("SQLite database snapshot cannot be inspected") from exc
            db.execute("PRAGMA query_only=ON")
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            schema_version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError("SQLite integrity_check is not ok")
            if schema_version != LEGACY_V291_ENROLL_SCHEMA_VERSION:
                raise RuntimeError("SQLite schema is not the exact v2.9.1 enrollment schema")
            _verify_existing_ciphertexts(
                db, telegram, withdrawal, telegram_raw, withdrawal_raw,
            )
        del database_bytes

        def require_unchanged() -> None:
            _require_no_sqlite_sidecars(root)
            after_path = database.lstat()
            after_fd = os.fstat(descriptor)
            if (
                stat.S_ISLNK(after_path.st_mode)
                or not _same_file(after_path, after_fd)
                or after_fd.st_size != opened.st_size
                or after_fd.st_mtime_ns != opened.st_mtime_ns
                or after_fd.st_nlink != 1
                or _digest_descriptor(descriptor) != digest_before
            ):
                raise RuntimeError("SQLite database changed during enrollment validation")

        require_unchanged()
        publication = publish(digest_before, schema_version)
        try:
            # The exact descriptor inspected above stays open across report and
            # canary publication; sidecars and bytes are rechecked afterwards.
            require_unchanged()
        except Exception:
            publication["cleanup"]()
            raise
        return digest_before, schema_version, publication
    finally:
        os.close(descriptor)


def _reserve_enrollment_report(path: Path) -> dict:
    repo = Path(__file__).resolve().parents[1]
    if not path.is_absolute():
        raise ValueError("Enrollment report path must be absolute")
    requested = Path(os.path.abspath(path))
    requested_parent = requested.parent
    requested_parent_info = requested_parent.lstat()
    if (
        stat.S_ISLNK(requested_parent_info.st_mode)
        or not stat.S_ISDIR(requested_parent_info.st_mode)
    ):
        raise ValueError("Enrollment report parent must be a non-symlink directory")
    parent = requested_parent.resolve()
    target = parent / requested.name
    if target.resolve(strict=False).is_relative_to(repo):
        raise ValueError("Enrollment report must be outside the repository")
    parent_info = parent.lstat()
    if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
        raise ValueError("Enrollment report parent must be a non-symlink directory")
    if os.name != "nt" and (
        parent_info.st_uid != os.geteuid()
        or stat.S_IMODE(parent_info.st_mode) & 0o077
    ):
        raise PermissionError("Enrollment report parent must be private and owned")
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    parent_fd = None if os.name == "nt" else os.open(parent, directory_flags)
    opened_parent = parent_info if parent_fd is None else os.fstat(parent_fd)
    if not _same_file(parent_info, opened_parent):
        if parent_fd is not None:
            os.close(parent_fd)
        raise ValueError("Enrollment report parent changed during secure open")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        if parent_fd is not None and os.open in os.supports_dir_fd:
            descriptor = os.open(target.name, flags, 0o600, dir_fd=parent_fd)
        else:
            descriptor = os.open(target, flags, 0o600)
    except Exception:
        if parent_fd is not None:
            os.close(parent_fd)
        raise
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
        or (os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600)
    ):
        os.close(descriptor)
        os.close(parent_fd)
        try:
            target.unlink()
        except OSError:
            pass
        raise PermissionError("Enrollment report reservation is not a private file")
    if os.name != "nt":
        os.fsync(descriptor)
        os.fsync(parent_fd)
    return {
        "target": target, "descriptor": descriptor, "info": info,
        "parent": parent, "parent_fd": parent_fd, "parent_info": opened_parent,
    }


def _verify_report_reservation(reservation: dict) -> None:
    parent_now = reservation["parent"].lstat()
    target_now = reservation["target"].lstat()
    descriptor_now = os.fstat(reservation["descriptor"])
    if (
        stat.S_ISLNK(parent_now.st_mode)
        or not _same_file(parent_now, reservation["parent_info"])
        or stat.S_ISLNK(target_now.st_mode)
        or not stat.S_ISREG(target_now.st_mode)
        or not _same_file(target_now, reservation["info"])
        or not _same_file(descriptor_now, reservation["info"])
        or descriptor_now.st_nlink != 1
    ):
        raise RuntimeError("Enrollment report reservation changed")


def _write_reserved_report(reservation: dict, report: dict) -> None:
    _verify_report_reservation(reservation)
    descriptor = reservation["descriptor"]
    raw = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    written = 0
    while written < len(raw):
        count = os.write(descriptor, raw[written:])
        if count <= 0:
            raise RuntimeError("Enrollment report write stopped unexpectedly")
        written += count
    os.fsync(descriptor)
    if os.fstat(descriptor).st_size != len(raw):
        raise RuntimeError("Enrollment report could not be persisted exactly")
    _verify_report_reservation(reservation)


def _close_report_reservation(reservation: dict) -> None:
    descriptor = reservation.get("descriptor")
    if descriptor is not None:
        os.close(descriptor)
        reservation["descriptor"] = None
    parent_fd = reservation.get("parent_fd")
    if parent_fd is not None:
        os.close(parent_fd)
        reservation["parent_fd"] = None


def _remove_report_reservation(reservation: dict) -> None:
    descriptor = reservation.get("descriptor")
    if descriptor is not None:
        os.close(descriptor)
        reservation["descriptor"] = None
    parent_fd = reservation.get("parent_fd")
    try:
        if parent_fd is not None and os.unlink in os.supports_dir_fd:
            current = os.stat(
                reservation["target"].name, dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                stat.S_ISLNK(current.st_mode)
                or not _same_file(current, reservation["info"])
                or current.st_nlink != 1
            ):
                raise RuntimeError("Refusing to remove replaced enrollment report")
            os.unlink(reservation["target"].name, dir_fd=parent_fd)
            if os.name != "nt":
                os.fsync(parent_fd)
        else:
            _unlink_exact(
                reservation["target"], reservation["info"], "enrollment report",
            )
    except FileNotFoundError:
        pass
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
            reservation["parent_fd"] = None


def enroll_existing(
    *, data_dir: Path, confirm_database_sha256: str,
    env_file: Path | None = None, telegram_key_file: Path | None = None,
    withdrawal_key_file: Path | None = None, report_path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    root = data_dir
    if not root.is_absolute():
        raise ValueError("DATA_DIR path must be absolute")
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("DATA_DIR must be a regular non-symlink directory")
    if os.name != "nt" and (
        root_info.st_uid != os.geteuid() or stat.S_IMODE(root_info.st_mode) != 0o700
    ):
        raise PermissionError("Production DATA_DIR must be owned and have mode 0700")
    canary = root / CANARY_FILENAME
    try:
        canary.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError("Recovery-key canary already exists")
    reservation = _reserve_enrollment_report(report_path) if report_path else None
    completed = False
    try:
        telegram, withdrawal, telegram_raw, withdrawal_raw = (
            _fernet_pair_from_explicit_source(
                env_file=env_file, telegram_key_file=telegram_key_file,
                withdrawal_key_file=withdrawal_key_file,
            )
        )
        canary_raw = _canonical_bytes(_new_document(telegram, withdrawal))
        enrolled_at = (now or datetime.now(timezone.utc)).isoformat()

        def publish(database_sha256: str, schema_version: int) -> dict:
            result = {
                "canary_sha256": hashlib.sha256(canary_raw).hexdigest(),
                "database_sha256": database_sha256,
                "enrolled_at": enrolled_at,
                "schema_version": schema_version,
            }
            if reservation is not None:
                _write_reserved_report(reservation, result)
            try:
                canary_path, stored, canary_info = _publish_new_canary(
                    root, telegram, withdrawal, fail_if_exists=True, raw=canary_raw,
                )
            except Exception:
                if reservation is not None:
                    _remove_report_reservation(reservation)
                raise

            def cleanup() -> None:
                failures = []
                try:
                    _unlink_exact(
                        canary_path, canary_info, "new recovery-key canary",
                    )
                except Exception as exc:
                    failures.append(exc)
                if reservation is not None:
                    try:
                        _remove_report_reservation(reservation)
                    except Exception as exc:
                        failures.append(exc)
                if failures:
                    raise RuntimeError(
                        "Enrollment rollback could not remove exact published artifacts"
                    ) from failures[0]

            return {
                "result": result, "canary_path": canary_path,
                "canary_bytes": stored, "canary_info": canary_info,
                "cleanup": cleanup,
            }

        _, _, publication = _validate_enrollment_database(
            root, confirm_database_sha256, telegram, withdrawal,
            telegram_raw, withdrawal_raw, publish,
        )
        try:
            if reservation is not None:
                _verify_report_reservation(reservation)
                _close_report_reservation(reservation)
        except Exception:
            publication["cleanup"]()
            raise
        completed = True
        return publication["result"]
    finally:
        if reservation is not None and not completed:
            try:
                _remove_report_reservation(reservation)
            except FileNotFoundError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a recovery-key canary for explicitly approved existing data",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    enroll = subcommands.add_parser("enroll-existing")
    enroll.add_argument("--data-dir", type=Path, required=True)
    enroll.add_argument("--confirm-database-sha256", required=True)
    enroll.add_argument("--env-file", type=Path)
    enroll.add_argument("--telegram-inbox-key-file", type=Path)
    enroll.add_argument("--withdraw-account-key-file", type=Path)
    enroll.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = enroll_existing(
            data_dir=args.data_dir,
            confirm_database_sha256=args.confirm_database_sha256,
            env_file=args.env_file,
            telegram_key_file=args.telegram_inbox_key_file,
            withdrawal_key_file=args.withdraw_account_key_file,
            report_path=args.report,
        )
    except Exception as exc:
        print(f"enroll-existing failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
