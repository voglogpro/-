"""Create a consistent SQLite backup with media and a checksum manifest.

Run while the single-instance pilot is online:
    python scripts/backup.py --data-dir data --output-dir backups

The output directory must live off-host in production. This script never deletes
old backups; retention is controlled by the backup destination.
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


def create_backup(data_dir: Path, output_dir: Path) -> Path:
    data_dir = data_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir == data_dir or output_dir.is_relative_to(data_dir):
        raise ValueError("Output directory must not be inside the data directory")

    database = data_dir / "bibitasks.db"
    if not database.is_file():
        raise FileNotFoundError(f"Database not found: {database}")
    if database.is_symlink():
        raise ValueError(f"Database must not be a symbolic link: {database}")

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = output_dir / stamp
    staging = output_dir / f".{stamp}.{uuid.uuid4().hex}.tmp"
    staging.mkdir(mode=0o700, exist_ok=False)
    database_copy = staging / "bibitasks.db"

    try:
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
                try:
                    content = response["Body"].read()
                finally:
                    response["Body"].close()
                if (
                    len(content) != item["bytes"]
                    or hashlib.sha256(content).hexdigest() != item["sha256"]
                ):
                    raise RuntimeError(f"Ready S3 media missing or corrupt: {item['id']}")
                media_copy = staging / "s3_media" / f"{item['id']}.jpg"
                media_copy.parent.mkdir(parents=True, exist_ok=True)
                media_copy.write_bytes(content)
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
            },
            "media": media,
            "media_objects": media_objects,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            manifest_path.chmod(0o600)
        staging.replace(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
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
    args = parser.parse_args()
    if args.env_file:
        if not args.env_file.is_file():
            parser.error(f"Environment file not found: {args.env_file}")
    _load_environment(args.env_file)
    destination = create_backup(args.data_dir.resolve(), args.output_dir.resolve())
    print(destination)


if __name__ == "__main__":
    main()
