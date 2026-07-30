"""Create an authenticated encrypted SQLite/media backup.

Run while the single-instance pilot is online:
    python scripts/backup.py --data-dir data --output-dir backups

Production/pilot use is fail-closed without a versioned key file. Plaintext
output exists only behind the explicit ``--allow-plaintext-dev`` compatibility
flag. This script never deletes old backups; retention belongs to the target.
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
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_MANIFEST_BYTES = 1024 * 1024

from dotenv import load_dotenv
try:
    from .recovery_key_canary import CANARY_FILENAME, validate_canary_bytes
    from .backup_crypto import (
        PAYLOAD_NAME, cleanup_private_tree, encrypt_directory, load_backup_key,
        require_explicit_dev_environment, require_memory_backed_temp, sha256_bytes,
    )
except ImportError:  # direct ``python scripts/backup.py`` execution
    from recovery_key_canary import CANARY_FILENAME, validate_canary_bytes
    from backup_crypto import (
        PAYLOAD_NAME, cleanup_private_tree, encrypt_directory, load_backup_key,
        require_explicit_dev_environment, require_memory_backed_temp, sha256_bytes,
    )


def _load_environment(env_file: Path | None) -> None:
    # An explicit file must not be shadowed by an automatically loaded cwd
    # `.env`. Real process variables still win for orchestrated deployments.
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


def _write_json_fsync(path: Path, value: dict) -> None:
    raw = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if len(raw.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ValueError("backup manifest is unexpectedly large")
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    if os.name != "nt":
        path.chmod(0o600)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_canary(data_dir: Path) -> bytes:
    path = data_dir / CANARY_FILENAME
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Recovery-key canary not found: {path}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("Recovery-key canary must be a regular non-symlink file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Recovery-key canary not found: {path}") from exc
    try:
        info = os.fstat(descriptor)
        after = path.lstat()
        identity = lambda value: (value.st_dev, value.st_ino)
        if (
            not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(after.st_mode)
            or identity(before) != identity(info) or identity(after) != identity(info)
            or info.st_nlink != 1
        ):
            raise ValueError("Recovery-key canary changed during secure open")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError("Recovery-key canary must have mode 0600")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read()
    finally:
        os.close(descriptor)
    validate_canary_bytes(raw)
    return raw


def _canonical_media_id(value: object) -> str:
    raw = str(value or "")
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("S3 media ID is not a canonical UUID") from exc
    canonical = str(parsed)
    if raw != canonical:
        raise ValueError("S3 media ID is not a canonical UUID")
    return canonical


def create_backup(
    data_dir: Path, output_dir: Path, *, encryption_key_file: Path | None = None,
    key_version: str | None = None, plaintext_tmp_dir: Path | None = None,
    allow_plaintext_dev: bool = False, allow_unverified_temp_dev: bool = False,
    allow_s3_dev: bool = False,
) -> Path:
    if allow_plaintext_dev:
        require_explicit_dev_environment("plaintext backup compatibility")
    if allow_unverified_temp_dev:
        require_explicit_dev_environment("unverified plaintext scratch")
    if allow_s3_dev:
        require_explicit_dev_environment("S3 backup compatibility")
    raw_data_dir = data_dir.expanduser()
    raw_output_dir = output_dir.expanduser()
    if raw_data_dir.is_symlink() or raw_output_dir.is_symlink():
        raise ValueError("backup data/output directories must not be symbolic links")
    data_dir = raw_data_dir.resolve()
    output_dir = raw_output_dir.resolve()
    if output_dir == data_dir or output_dir.is_relative_to(data_dir):
        raise ValueError("Output directory must not be inside the data directory")

    database = data_dir / "bibitasks.db"
    if not database.is_file():
        raise FileNotFoundError(f"Database not found: {database}")
    if database.is_symlink():
        raise ValueError(f"Database must not be a symbolic link: {database}")
    canary_raw = _read_canary(data_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = output_dir / stamp
    publish_staging = output_dir / f".{stamp}.{uuid.uuid4().hex}.tmp"
    if encryption_key_file is not None:
        if plaintext_tmp_dir is None:
            raise RuntimeError("memory-backed plaintext scratch directory is required")
        if allow_unverified_temp_dev:
            scratch_root = plaintext_tmp_dir.expanduser().resolve()
            if not scratch_root.is_dir() or scratch_root.is_symlink():
                raise ValueError("dev plaintext scratch directory is unsafe")
        else:
            scratch_root = require_memory_backed_temp(plaintext_tmp_dir)
    elif allow_plaintext_dev:
        scratch_root = plaintext_tmp_dir.expanduser().resolve() if plaintext_tmp_dir else None
    else:
        scratch_root = None
    publish_staging.mkdir(mode=0o700, exist_ok=False)
    temporary_root = Path(tempfile.mkdtemp(
        prefix="bibitasks-backup-", dir=str(scratch_root) if scratch_root else None,
    ))
    if os.name != "nt":
        temporary_root.chmod(0o700)
    staging = temporary_root / "bundle"
    staging.mkdir(mode=0o700, exist_ok=False)
    database_copy = staging / "bibitasks.db"

    try:
        if encryption_key_file is None and not allow_plaintext_dev:
            raise RuntimeError(
                "backup encryption key file is required outside explicit dev/test mode"
            )
        if encryption_key_file is not None and allow_plaintext_dev:
            raise ValueError("encrypted and plaintext backup modes are mutually exclusive")
        encryption_key = None
        resolved_key_version = None
        if encryption_key_file is not None:
            encryption_key, resolved_key_version = load_backup_key(
                encryption_key_file, expected_version=key_version,
            )
        source_uri = f"{database.as_uri()}?mode=ro"
        with (
            closing(sqlite3.connect(source_uri, uri=True)) as source,
            closing(sqlite3.connect(database_copy)) as target,
        ):
            source.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Backup integrity check failed: {integrity}")
        if os.name != "nt":
            database_copy.chmod(0o600)
        canary_copy = staging / CANARY_FILENAME
        canary_copy.write_bytes(canary_raw)
        if os.name != "nt":
            canary_copy.chmod(0o600)

        media = []
        for folder_name in ("task_photos", "proof_photos"):
            folder = data_dir / folder_name
            if not folder.is_dir():
                continue
            for source_file in sorted(folder.rglob("*")):
                if source_file.is_symlink():
                    raise ValueError(
                        f"Media must not contain symbolic links: {source_file}"
                    )
                if not source_file.is_file():
                    continue
                relative = source_file.relative_to(data_dir)
                media_copy = staging / relative
                media_copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, media_copy)
                if os.name != "nt":
                    media_copy.chmod(0o600)
                media.append({
                    "path": relative.as_posix(),
                    "bytes": media_copy.stat().st_size,
                    "sha256": sha256(media_copy),
                })

        with closing(sqlite3.connect(database_copy)) as snapshot:
            schema_version = int(snapshot.execute("PRAGMA user_version").fetchone()[0])
            try:
                recovery_key_counts = {
                    "telegram_ciphertext_count": int(snapshot.execute(
                        "SELECT COUNT(*) FROM telegram_update_inbox "
                        "WHERE payload_json IS NOT NULL"
                    ).fetchone()[0]),
                    "telegram_active_null_count": int(snapshot.execute(
                        "SELECT COUNT(*) FROM telegram_update_inbox "
                        "WHERE status IN ('pending','processing') AND payload_json IS NULL"
                    ).fetchone()[0]),
                    "withdrawal_ciphertext_count": int(snapshot.execute(
                        "SELECT COUNT(*) FROM withdrawal_requests "
                        "WHERE account_ciphertext IS NOT NULL"
                    ).fetchone()[0]),
                    "withdrawal_active_null_count": int(snapshot.execute(
                        "SELECT COUNT(*) FROM withdrawal_requests "
                        "WHERE status IN ('pending','processing') "
                        "AND account_ciphertext IS NULL"
                    ).fetchone()[0]),
                }
            except sqlite3.Error as exc:
                raise RuntimeError(
                    "Database lacks the encrypted recovery-count contract"
                ) from exc
            if (
                recovery_key_counts["telegram_active_null_count"] > 0
                or recovery_key_counts["withdrawal_active_null_count"] > 0
            ):
                raise RuntimeError(
                    "Active recovery-sensitive rows are missing ciphertext"
                )
            has_media_table = snapshot.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_objects'"
            ).fetchone()
            media_objects = []
            if has_media_table:
                media_objects = [
                    {
                        "id": row[0], "backend": row[1], "object_key": row[2],
                        "state": row[3], "bytes": row[4], "sha256": row[5],
                        "version_id": row[6],
                    }
                    for row in snapshot.execute(
                        "SELECT id,backend,object_key,state,size_bytes,sha256,version_id "
                        "FROM media_objects ORDER BY id"
                    )
                ]
        copied_by_name = {Path(item["path"]).name: item for item in media}
        for item in media_objects:
            if item["backend"] != "local" or item["state"] != "ready":
                continue
            copied = copied_by_name.get(Path(item["object_key"]).name)
            if (
                not copied or copied["bytes"] != item["bytes"]
                or copied["sha256"] != item["sha256"]
            ):
                raise RuntimeError(
                    f"Ready local media missing or corrupt: {item['id']}"
                )
        s3_ready = [
            item for item in media_objects
            if item["backend"] == "s3" and item["state"] == "ready"
        ]
        if s3_ready:
            if not allow_s3_dev:
                raise RuntimeError(
                    "S3 backup is disabled for the network-isolated pilot"
                )
            for item in s3_ready:
                item["id"] = _canonical_media_id(item.get("id"))
                object_key = str(item.get("object_key") or "")
                if Path(object_key).name != object_key or not object_key:
                    raise ValueError("S3 media object key is unsafe")
                if (
                    type(item.get("bytes")) is not int or item["bytes"] < 0
                    or not isinstance(item.get("sha256"), str)
                    or len(item["sha256"]) != 64
                ):
                    raise ValueError("S3 media size/digest contract is invalid")
            import boto3
            from botocore.config import Config

            bucket = (os.getenv("S3_BUCKET", "") or "").strip()
            if not bucket:
                raise RuntimeError("S3_BUCKET is required to back up ready S3 media")
            endpoint = (os.getenv("S3_ENDPOINT_URL", "") or "").strip() or None
            region = (os.getenv("S3_REGION", "us-east-1") or "us-east-1").strip()
            prefix = (os.getenv("S3_PREFIX", "bibitasks") or "bibitasks").strip("/")
            addressing = (
                os.getenv("S3_ADDRESSING_STYLE", "auto") or "auto"
            ).strip().lower()
            s3 = boto3.client(
                "s3", region_name=region, endpoint_url=endpoint,
                config=Config(
                    signature_version="s3v4", s3={"addressing_style": addressing},
                    connect_timeout=5, read_timeout=30, retries={"max_attempts": 3},
                ),
            )
            for item in s3_ready:
                key = f"{prefix}/{item['object_key']}" if prefix else item["object_key"]
                request = {"Bucket": bucket, "Key": key}
                if item["version_id"]:
                    request["VersionId"] = item["version_id"]
                response = s3.get_object(**request)
                media_copy = staging / "s3_media" / f"{item['id']}.jpg"
                media_copy.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                total = 0
                try:
                    with media_copy.open("xb") as output:
                        while total <= item["bytes"]:
                            limit = min(1024 * 1024, item["bytes"] - total + 1)
                            chunk = response["Body"].read(limit)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > item["bytes"]:
                                raise RuntimeError(
                                    f"Ready S3 media exceeds expected size: {item['id']}"
                                )
                            output.write(chunk)
                            digest.update(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    response["Body"].close()
                if (
                    total != item["bytes"] or digest.hexdigest() != item["sha256"]
                ):
                    raise RuntimeError(f"Ready S3 media missing or corrupt: {item['id']}")
                if os.name != "nt":
                    media_copy.chmod(0o600)
                item["backup_path"] = media_copy.relative_to(staging).as_posix()
                item["retrieved_version_id"] = response.get("VersionId")

        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": {
                "path": database_copy.name,
                "bytes": database_copy.stat().st_size,
                "sha256": sha256(database_copy),
                "integrity_check": "ok",
                "schema_version": schema_version,
                **recovery_key_counts,
            },
            "recovery_key_canary": {
                "path": CANARY_FILENAME,
                "bytes": len(canary_raw),
                "sha256": hashlib.sha256(canary_raw).hexdigest(),
            },
            "media": media,
            "media_objects": media_objects,
        }
        manifest_path = staging / "manifest.json"
        _write_json_fsync(manifest_path, manifest)
        if encryption_key is not None:
            inner_manifest_raw = manifest_path.read_bytes()
            protected_digest = sha256_bytes(inner_manifest_raw)
            encryption = encrypt_directory(
                staging, publish_staging / PAYLOAD_NAME,
                key=encryption_key, key_version=resolved_key_version,
                protected_manifest_sha256=protected_digest,
            )
            outer_manifest = {**manifest, "encryption": encryption}
            outer_manifest_path = publish_staging / "manifest.json"
            _write_json_fsync(outer_manifest_path, outer_manifest)
        else:
            for source in staging.iterdir():
                target = publish_staging / source.name
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    shutil.copy2(source, target)
        cleanup_private_tree(
            temporary_root, scratch_root or temporary_root.parent,
            "plaintext backup scratch",
        )
        temporary_root = None
        _fsync_directory(publish_staging)
        publish_staging.replace(destination)
        _fsync_directory(output_dir)
    except Exception:
        cleanup_failures = []
        for path, parent, label in (
            (temporary_root, scratch_root or temporary_root.parent,
             "plaintext backup scratch") if temporary_root is not None else (None, None, None),
            (publish_staging, output_dir, "partial encrypted backup"),
        ):
            if path is None:
                continue
            try:
                cleanup_private_tree(path, parent, label)
            except Exception as cleanup_error:
                cleanup_failures.append(cleanup_error)
        if cleanup_failures:
            raise RuntimeError("backup failed and secure cleanup was incomplete") from cleanup_failures[0]
        raise
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--env-file", type=Path,
        help="Optional explicit .env file; existing process variables win",
    )
    parser.add_argument(
        "--encryption-key-file", type=Path,
        help="Root-managed versioned 256-bit backup key file",
    )
    parser.add_argument("--key-version")
    parser.add_argument(
        "--plaintext-tmp-dir", type=Path,
        help="Dedicated tmpfs/ramfs mount used only for transient plaintext",
    )
    parser.add_argument(
        "--allow-plaintext-dev", action="store_true",
        help="Explicit legacy compatibility for non-production dev/tests only",
    )
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
    key_version = args.key_version or os.environ.get("BACKUP_ENCRYPTION_KEY_VERSION")
    if args.allow_plaintext_dev and (
        os.environ.get("BIBITASKS_ENVIRONMENT", "").strip().lower()
        in {"production", "pilot"}
    ):
        parser.error("--allow-plaintext-dev is forbidden in production/pilot")
    destination = create_backup(
        args.data_dir.resolve(), args.output_dir.resolve(),
        encryption_key_file=key_file_value, key_version=key_version,
        plaintext_tmp_dir=args.plaintext_tmp_dir or (
            Path(os.environ["BACKUP_PLAINTEXT_TMP_DIR"])
            if os.environ.get("BACKUP_PLAINTEXT_TMP_DIR") else None
        ),
        allow_plaintext_dev=args.allow_plaintext_dev,
        allow_s3_dev=args.allow_s3_dev,
    )
    print(destination)


if __name__ == "__main__":
    main()
