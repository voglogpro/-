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
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


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
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Backup manifest contains an unsafe path")
    source = (root / relative).resolve()
    if not source.is_relative_to(root) or not source.is_file() or source.is_symlink():
        raise ValueError(f"Backup file is missing or unsafe: {relative.as_posix()}")
    if source.stat().st_size != int(expected_size) or sha256(source) != str(digest):
        raise RuntimeError(f"Backup checksum mismatch: {relative.as_posix()}")
    return source


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


def restore_backup(backup_dir: Path, restore_dir: Path) -> Path:
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
    manifest = json.loads(manifest_path.read_text("utf-8"))
    database_meta = manifest.get("database") or {}
    database_source = _verified_file(
        backup_dir, database_meta.get("path", ""),
        database_meta.get("bytes", -1), database_meta.get("sha256", ""),
    )

    staging = restore_dir.parent / f".{restore_dir.name}.{uuid.uuid4().hex}.tmp"
    staging.mkdir(mode=0o700, parents=True, exist_ok=False)
    uploaded = []
    try:
        database_target = staging / "bibitasks.db"
        shutil.copy2(database_source, database_target)
        if os.name != "nt":
            database_target.chmod(0o600)
        with closing(sqlite3.connect(database_target)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Restored database failed integrity_check")

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
                content = source.read_bytes()
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
            "source_manifest_sha256": sha256(manifest_path),
            "database_sha256_after_restore": sha256(database_target),
            "s3_versions_rewritten": len(restored_versions),
            "integrity_check": "ok",
        }
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
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--restore-dir", type=Path, required=True)
    parser.add_argument(
        "--env-file", type=Path,
        help="Optional explicit .env file; existing process variables win",
    )
    args = parser.parse_args()
    if args.env_file:
        if not args.env_file.is_file():
            parser.error(f"Environment file not found: {args.env_file}")
    _load_environment(args.env_file)
    restored = restore_backup(args.backup_dir, args.restore_dir)
    print(restored)


if __name__ == "__main__":
    main()
