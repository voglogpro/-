"""Restore a verified BibiTasks SQLite/media backup into a fresh target.

S3 restores are deliberately fail-closed: the destination object key must not
exist. Uploads are checksum-verified, new VersionId values are written to the
restored database, and partially uploaded versions are removed on failure.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
try:
    from .recovery_key_canary import CANARY_FILENAME, validate_canary_bytes
    from .backup_crypto import (
        cleanup_private_tree, decrypt_directory, load_backup_key,
        require_explicit_dev_environment, require_memory_backed_temp,
    )
except ImportError:  # direct ``python scripts/restore.py`` execution
    from recovery_key_canary import CANARY_FILENAME, validate_canary_bytes
    from backup_crypto import (
        cleanup_private_tree, decrypt_directory, load_backup_key,
        require_explicit_dev_environment, require_memory_backed_temp,
    )


MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CIPHERTEXT_BYTES = 16 * 1024 * 1024 * 1024


def _read_manifest(path: Path) -> dict:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FileNotFoundError("Backup manifest not found") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_size > MAX_MANIFEST_BYTES
        ):
            raise ValueError("Backup manifest is unexpectedly large or unsafe")
        chunks = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise ValueError("Backup manifest changed while being read")
        raw = b"".join(chunks)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("Backup manifest is unexpectedly large")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Backup manifest is invalid") from exc
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError("Backup manifest must be a JSON object")
    return value


def _load_environment(env_file: Path | None) -> None:
    # Explicit restore configuration must not inherit a different cwd `.env`.
    # Values exported by the invoking process intentionally keep precedence.
    if env_file is not None:
        load_dotenv(env_file, override=False)
    else:
        load_dotenv(override=False)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_file(root: Path, relative_value: str, expected_size: int, digest: str) -> Path:
    relative = Path(relative_value)
    if (
        not relative_value or relative.is_absolute() or ".." in relative.parts
        or "\\" in relative_value
    ):
        raise ValueError("Backup manifest contains an unsafe path")
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Backup file is missing or unsafe: {relative.as_posix()}")
    source = candidate.resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError(f"Backup file is missing or unsafe: {relative.as_posix()}")
    if source.stat().st_nlink != 1:
        raise ValueError(f"Backup file has multiple hard links: {relative.as_posix()}")
    if source.stat().st_size != int(expected_size) or sha256(source) != str(digest):
        raise RuntimeError(f"Backup checksum mismatch: {relative.as_posix()}")
    return source


def _recovery_key_counts(db: sqlite3.Connection) -> dict[str, int]:
    try:
        return {
            "telegram_ciphertext_count": int(db.execute(
                "SELECT COUNT(*) FROM telegram_update_inbox "
                "WHERE payload_json IS NOT NULL"
            ).fetchone()[0]),
            "telegram_active_null_count": int(db.execute(
                "SELECT COUNT(*) FROM telegram_update_inbox "
                "WHERE status IN ('pending','processing') AND payload_json IS NULL"
            ).fetchone()[0]),
            "withdrawal_ciphertext_count": int(db.execute(
                "SELECT COUNT(*) FROM withdrawal_requests "
                "WHERE account_ciphertext IS NOT NULL"
            ).fetchone()[0]),
            "withdrawal_active_null_count": int(db.execute(
                "SELECT COUNT(*) FROM withdrawal_requests "
                "WHERE status IN ('pending','processing') AND account_ciphertext IS NULL"
            ).fetchone()[0]),
        }
    except sqlite3.Error as exc:
        raise RuntimeError(
            "Restored database lacks the encrypted recovery-count contract"
        ) from exc


def _missing_s3(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    code = str(((response or {}).get("Error") or {}).get("Code") or "")
    return code in ("NoSuchKey", "NoSuchVersion", "404", "NotFound")


def _s3_client_and_settings():
    import boto3
    from botocore.config import Config

    bucket = (os.getenv("S3_BUCKET", "") or "").strip()
    if not bucket:
        raise RuntimeError("S3_BUCKET is required to restore S3 media")
    endpoint = (os.getenv("S3_ENDPOINT_URL", "") or "").strip() or None
    region = (os.getenv("S3_REGION", "us-east-1") or "us-east-1").strip()
    prefix = (os.getenv("S3_PREFIX", "bibitasks") or "bibitasks").strip("/")
    addressing = (os.getenv("S3_ADDRESSING_STYLE", "auto") or "auto").strip().lower()
    sse = (os.getenv("S3_SSE", "AES256") or "").strip()
    privacy_mode = (
        os.getenv("S3_PRIVACY_MODE", "public_access_block") or "public_access_block"
    ).strip().lower()
    attested = (os.getenv("S3_PRIVATE_BUCKET_CONFIRMED", "") or "").strip().lower()
    if addressing not in ("auto", "path", "virtual"):
        raise RuntimeError("Invalid S3_ADDRESSING_STYLE")
    if sse not in ("AES256", "aws:kms"):
        raise RuntimeError("Invalid S3_SSE")
    if privacy_mode not in ("public_access_block", "operator_attested"):
        raise RuntimeError("Invalid S3_PRIVACY_MODE")
    if privacy_mode == "operator_attested" and attested not in ("1", "true", "yes"):
        raise RuntimeError("S3 private bucket attestation is required")
    client = boto3.client(
        "s3", region_name=region, endpoint_url=endpoint,
        config=Config(
            signature_version="s3v4", s3={"addressing_style": addressing},
            connect_timeout=5, read_timeout=30, retries={"max_attempts": 3},
        ),
    )
    client.head_bucket(Bucket=bucket)
    if privacy_mode == "public_access_block":
        response = client.get_public_access_block(Bucket=bucket)
        block = (response or {}).get("PublicAccessBlockConfiguration") or {}
        required = (
            "BlockPublicAcls", "IgnorePublicAcls",
            "BlockPublicPolicy", "RestrictPublicBuckets",
        )
        if not all(block.get(name) is True for name in required):
            raise RuntimeError("S3 bucket is not fail-closed against public access")
    return client, bucket, prefix, sse


def _restore_plaintext_backup(
    backup_dir: Path, restore_dir: Path, *,
    source_manifest_sha256: str | None = None,
    encryption_report: dict | None = None,
    allow_s3_dev: bool = False,
) -> Path:
    backup_dir = backup_dir.resolve()
    restore_dir = restore_dir.resolve()
    if not backup_dir.is_dir() or backup_dir.is_symlink():
        raise ValueError("Backup directory is missing or unsafe")
    if restore_dir.exists():
        raise FileExistsError("Restore target must not exist")
    if restore_dir == backup_dir or restore_dir.is_relative_to(backup_dir):
        raise ValueError("Restore target must not be inside the backup")

    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise FileNotFoundError("Backup manifest not found")
    manifest = _read_manifest(manifest_path)
    database_meta = manifest.get("database") or {}
    database_source = _verified_file(
        backup_dir, database_meta.get("path", ""),
        database_meta.get("bytes", -1), database_meta.get("sha256", ""),
    )
    canary_meta = manifest.get("recovery_key_canary") or {}
    if set(canary_meta) != {"path", "bytes", "sha256"}:
        raise ValueError("Backup manifest lacks the recovery-key canary contract")
    if canary_meta.get("path") != CANARY_FILENAME:
        raise ValueError("Backup manifest contains an invalid recovery-key canary path")
    canary_source = _verified_file(
        backup_dir, canary_meta["path"], canary_meta["bytes"],
        canary_meta["sha256"],
    )
    if canary_source.stat().st_nlink != 1:
        raise ValueError("Backup recovery-key canary must have exactly one hard link")
    canary_raw = canary_source.read_bytes()
    validate_canary_bytes(canary_raw)

    staging = restore_dir.parent / f".{restore_dir.name}.{uuid.uuid4().hex}.tmp"
    staging.mkdir(mode=0o700, parents=True, exist_ok=False)
    uploaded = []
    try:
        database_target = staging / "bibitasks.db"
        shutil.copy2(database_source, database_target)
        if os.name != "nt":
            database_target.chmod(0o600)
        canary_target = staging / CANARY_FILENAME
        canary_target.write_bytes(canary_raw)
        if os.name != "nt":
            canary_target.chmod(0o600)
        with closing(sqlite3.connect(database_target)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Restored database failed integrity_check")
            restored_schema_version = int(
                db.execute("PRAGMA user_version").fetchone()[0]
            )
            restored_recovery_counts = _recovery_key_counts(db)
        expected_recovery_counts = {}
        for name in (
            "telegram_ciphertext_count", "telegram_active_null_count",
            "withdrawal_ciphertext_count", "withdrawal_active_null_count",
        ):
            value = database_meta.get(name)
            if type(value) is not int or value < 0:
                raise ValueError("Backup manifest contains invalid recovery counts")
            expected_recovery_counts[name] = value
        if (
            expected_recovery_counts["telegram_active_null_count"] != 0
            or expected_recovery_counts["withdrawal_active_null_count"] != 0
            or restored_recovery_counts != expected_recovery_counts
        ):
            raise RuntimeError("Restored encrypted recovery counts differ from manifest")
        expected_schema_version = database_meta.get("schema_version")
        if (
            expected_schema_version is not None
            and restored_schema_version != int(expected_schema_version)
        ):
            raise RuntimeError("Restored database schema version differs from manifest")

        for item in manifest.get("media") or []:
            source = _verified_file(
                backup_dir, item["path"], item["bytes"], item["sha256"],
            )
            relative = Path(item["path"])
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if os.name != "nt":
                target.chmod(0o600)

        ready_s3 = [
            item for item in (manifest.get("media_objects") or [])
            if item.get("backend") == "s3" and item.get("state") == "ready"
        ]
        restored_versions = {}
        if ready_s3:
            if not allow_s3_dev:
                raise RuntimeError("S3 restore is disabled outside explicit dev tests")
            client, bucket, prefix, sse = _s3_client_and_settings()
            for item in ready_s3:
                source = _verified_file(
                    backup_dir, item.get("backup_path", ""),
                    item["bytes"], item["sha256"],
                )
                object_name = str(item["object_key"])
                if Path(object_name).name != object_name:
                    raise ValueError("Unsafe S3 object key in backup manifest")
                key = f"{prefix}/{object_name}" if prefix else object_name
                try:
                    client.head_object(Bucket=bucket, Key=key)
                except Exception as exc:
                    if not _missing_s3(exc):
                        raise
                else:
                    raise RuntimeError(f"S3 restore target already exists: {item['id']}")
                with source.open("rb") as content:
                    response = client.put_object(
                        Bucket=bucket, Key=key, Body=content,
                        ContentType="image/jpeg", Metadata={"sha256": item["sha256"]},
                        ServerSideEncryption=sse,
                    ) or {}
                version_id = response.get("VersionId")
                uploaded.append((client, bucket, key, version_id))
                head_request = {"Bucket": bucket, "Key": key}
                if version_id:
                    head_request["VersionId"] = version_id
                head = client.head_object(**head_request)
                if (
                    int(head["ContentLength"]) != int(item["bytes"])
                    or str((head.get("Metadata") or {}).get("sha256") or "")
                    != str(item["sha256"])
                ):
                    raise RuntimeError(f"Restored S3 media verification failed: {item['id']}")
                restored_versions[str(item["id"])] = version_id

            with closing(sqlite3.connect(database_target)) as db:
                db.execute("BEGIN IMMEDIATE")
                for media_id, version_id in restored_versions.items():
                    cursor = db.execute(
                        "UPDATE media_objects SET version_id=?,checked_at=NULL,last_error=NULL "
                        "WHERE id=? AND backend='s3' AND state='ready'",
                        (version_id, media_id),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(f"Restored media row missing: {media_id}")
                db.commit()

        report = {
            "restored_at": datetime.now(timezone.utc).isoformat(),
            "source_manifest_sha256": source_manifest_sha256 or sha256(manifest_path),
            "database_sha256_after_restore": sha256(database_target),
            "schema_version": restored_schema_version,
            "s3_versions_rewritten": len(restored_versions),
            "integrity_check": "ok",
            "recovery_key_canary": {
                "sha256": hashlib.sha256(canary_raw).hexdigest(),
                "ok": True,
            },
        }
        if encryption_report is not None:
            report["encryption"] = encryption_report
        (staging / "restore-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        staging.replace(restore_dir)
        return restore_dir
    except Exception:
        for client, bucket, key, version_id in reversed(uploaded):
            try:
                request = {"Bucket": bucket, "Key": key}
                if version_id:
                    request["VersionId"] = version_id
                client.delete_object(**request)
            except Exception:
                pass
        try:
            cleanup_private_tree(staging, restore_dir.parent, "restore staging")
        except Exception as cleanup_error:
            raise RuntimeError("restore failed and staging cleanup was incomplete") from cleanup_error
        raise


def _expected_bundle_files(manifest: dict) -> set[str]:
    expected = {"manifest.json"}
    database = manifest.get("database") or {}
    canary = manifest.get("recovery_key_canary") or {}
    for value in (database.get("path"), canary.get("path")):
        if isinstance(value, str):
            expected.add(value)
    for item in manifest.get("media") or []:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            expected.add(item["path"])
    for item in manifest.get("media_objects") or []:
        if isinstance(item, dict) and isinstance(item.get("backup_path"), str):
            expected.add(item["backup_path"])
    return expected


def restore_backup(
    backup_dir: Path, restore_dir: Path, *, encryption_key_file: Path | None = None,
    plaintext_tmp_dir: Path | None = None, allow_plaintext_dev: bool = False,
    allow_unverified_temp_dev: bool = False, allow_s3_dev: bool = False,
) -> Path:
    """Restore authenticated ciphertext, or explicit legacy dev/test plaintext."""
    if allow_plaintext_dev:
        require_explicit_dev_environment("plaintext restore compatibility")
    if allow_unverified_temp_dev:
        require_explicit_dev_environment("unverified plaintext scratch")
    if allow_s3_dev:
        require_explicit_dev_environment("S3 restore compatibility")
    raw_backup_dir = backup_dir.expanduser()
    if raw_backup_dir.is_symlink():
        raise ValueError("Backup directory is missing or unsafe")
    backup_dir = raw_backup_dir.resolve()
    restore_dir = restore_dir.expanduser().resolve()
    if not backup_dir.is_dir() or backup_dir.is_symlink():
        raise ValueError("Backup directory is missing or unsafe")
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise FileNotFoundError("Backup manifest not found")
    outer_manifest = _read_manifest(manifest_path)
    encryption = outer_manifest.get("encryption")
    if encryption is None:
        if not allow_plaintext_dev:
            raise RuntimeError(
                "plaintext backup restore requires explicit non-production dev/test mode"
            )
        return _restore_plaintext_backup(
            backup_dir, restore_dir, allow_s3_dev=allow_s3_dev,
        )
    if allow_plaintext_dev:
        raise ValueError("encrypted and plaintext restore modes are mutually exclusive")
    if encryption_key_file is None:
        raise RuntimeError("backup decryption key file is required")
    if not isinstance(encryption, dict):
        raise ValueError("Backup encryption contract is invalid")
    ciphertext_meta = encryption.get("ciphertext") or {}
    ciphertext_bytes = ciphertext_meta.get("bytes")
    if (
        type(ciphertext_bytes) is not int or ciphertext_bytes <= 0
        or ciphertext_bytes > MAX_CIPHERTEXT_BYTES
    ):
        raise ValueError("Encrypted backup ciphertext size is invalid")
    ciphertext = _verified_file(
        backup_dir, ciphertext_meta.get("path", ""),
        ciphertext_meta.get("bytes", -1), ciphertext_meta.get("sha256", ""),
    )
    key, key_version = load_backup_key(
        encryption_key_file, expected_version=encryption.get("key_version"),
    )
    if plaintext_tmp_dir is None:
        raise RuntimeError("memory-backed plaintext scratch directory is required")
    if allow_unverified_temp_dev:
        scratch_root = plaintext_tmp_dir.expanduser().resolve()
        if not scratch_root.is_dir() or scratch_root.is_symlink():
            raise ValueError("dev plaintext scratch directory is unsafe")
    else:
        scratch_root = require_memory_backed_temp(plaintext_tmp_dir)
    decrypted = scratch_root / f"bibitasks-restore-{uuid.uuid4().hex}"
    restore_published = False
    try:
        decrypt_directory(ciphertext, decrypted, key=key, encryption=encryption)
        inner_manifest_path = decrypted / "manifest.json"
        if not inner_manifest_path.is_file() or inner_manifest_path.is_symlink():
            raise ValueError("encrypted backup payload lacks its protected manifest")
        protected_digest = sha256(inner_manifest_path)
        if protected_digest != encryption.get("protected_manifest_sha256"):
            raise RuntimeError("encrypted backup protected manifest mismatch")
        try:
            inner_manifest = _read_manifest(inner_manifest_path)
        except (OSError, ValueError) as exc:
            raise ValueError("encrypted backup protected manifest is invalid") from exc
        outer_core = dict(outer_manifest)
        outer_core.pop("encryption", None)
        if inner_manifest != outer_core:
            raise RuntimeError("encrypted backup outer manifest is not authenticated")
        actual_files = {
            item.relative_to(decrypted).as_posix()
            for item in decrypted.rglob("*") if item.is_file()
        }
        if actual_files != _expected_bundle_files(inner_manifest):
            raise ValueError("encrypted backup payload has unexpected or missing files")
        result = _restore_plaintext_backup(
            decrypted, restore_dir,
            source_manifest_sha256=sha256(manifest_path),
            encryption_report={
                "method": encryption.get("method"),
                "key_version": key_version,
                "ciphertext_sha256": ciphertext_meta.get("sha256"),
                "authenticated": True,
            },
            allow_s3_dev=allow_s3_dev,
        )
        restore_published = True
        cleanup_private_tree(decrypted, scratch_root, "decrypted backup scratch")
        return result
    except Exception:
        cleanup_failures = []
        for path, parent, label in (
            (decrypted, scratch_root, "decrypted backup scratch"),
            (restore_dir, restore_dir.parent, "published restore")
            if restore_published else (None, None, None),
        ):
            if path is None:
                continue
            try:
                cleanup_private_tree(path, parent, label)
            except Exception as cleanup_error:
                cleanup_failures.append(cleanup_error)
        if cleanup_failures:
            raise RuntimeError("restore failed and secure cleanup was incomplete") from cleanup_failures[0]
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--restore-dir", type=Path, required=True)
    parser.add_argument(
        "--env-file", type=Path,
        help="Optional explicit .env file; existing process variables win",
    )
    parser.add_argument("--encryption-key-file", type=Path)
    parser.add_argument("--plaintext-tmp-dir", type=Path)
    parser.add_argument("--allow-plaintext-dev", action="store_true")
    parser.add_argument("--allow-s3-dev", action="store_true")
    args = parser.parse_args()
    if args.env_file:
        if not args.env_file.is_file():
            parser.error(f"Environment file not found: {args.env_file}")
    _load_environment(args.env_file)
    key_file_value = args.encryption_key_file or (
        Path(os.environ["BACKUP_ENCRYPTION_KEY_FILE"])
        if os.environ.get("BACKUP_ENCRYPTION_KEY_FILE") else None
    )
    if args.allow_plaintext_dev and (
        os.environ.get("BIBITASKS_ENVIRONMENT", "").strip().lower()
        in {"production", "pilot"}
    ):
        parser.error("--allow-plaintext-dev is forbidden in production/pilot")
    restored = restore_backup(
        args.backup_dir, args.restore_dir,
        encryption_key_file=key_file_value,
        plaintext_tmp_dir=args.plaintext_tmp_dir or (
            Path(os.environ["BACKUP_PLAINTEXT_TMP_DIR"])
            if os.environ.get("BACKUP_PLAINTEXT_TMP_DIR") else None
        ),
        allow_plaintext_dev=args.allow_plaintext_dev,
        allow_s3_dev=args.allow_s3_dev,
    )
    print(restored)


if __name__ == "__main__":
    main()
