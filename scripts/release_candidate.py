"""Create the immutable software/backup subject used by promotion evidence.

This is phase one of the release gate.  It does not authorize deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from .release_record import verify_image_attestation
except ImportError:  # pragma: no cover - direct script execution
    from release_record import verify_image_attestation


CANDIDATE_VERSION = 1
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(r"^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^v?[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
ORIGIN_RE = re.compile(r"^https://[a-z0-9.-]+(?::[0-9]{2,5})?$")
COUNT_FIELDS = (
    "telegram_ciphertext_count", "telegram_active_null_count",
    "withdrawal_ciphertext_count", "withdrawal_active_null_count",
)
PINNED_REPOSITORY = "voglogpro/-"
PINNED_SIGNER_WORKFLOW = "github.com/voglogpro/-/.github/workflows/release.yml"


def canonical_sha256(value: dict) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_file(path: Path, label: str) -> tuple[Path, dict, bytes]:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
            raise ValueError(f"{label} must be a small regular file")
        chunks = []
        remaining = 1024 * 1024 + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk); remaining -= len(chunk)
        after = os.fstat(descriptor)
        raw = b"".join(chunks)
        if len(raw) > 1024 * 1024 or (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError(f"{label} changed while being read")
        current = os.stat(candidate, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError(f"{label} path changed while being read")
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must contain valid UTF-8 JSON") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    resolved = candidate.resolve()
    return resolved, value, raw


def build_candidate(
    *, commit: str, image: str, schema_version: int, application_version: str,
    telegram_bot_id: int, telegram_group_id: int, miniapp_origin: str,
    health_origin: str,
    backup_manifest: Path, repository: str, signer_workflow: str,
    attestation_runner=None, now: datetime | None = None,
) -> dict:
    commit = str(commit or "").strip().lower()
    image = str(image or "").strip().lower()
    application_version = str(application_version or "").strip()
    if not COMMIT_RE.fullmatch(commit):
        raise ValueError("commit must be a full lowercase SHA")
    if not IMAGE_RE.fullmatch(image):
        raise ValueError("image must be an immutable lowercase GHCR digest reference")
    if type(schema_version) is not int or schema_version <= 0:
        raise ValueError("schema version must be a positive integer")
    if not VERSION_RE.fullmatch(application_version):
        raise ValueError("application version must be a safe semantic version")
    if repository != PINNED_REPOSITORY or signer_workflow != PINNED_SIGNER_WORKFLOW:
        raise ValueError("image provenance repository/workflow differs from release policy")
    deployment = {
        "telegram_bot_id": telegram_bot_id, "telegram_group_id": telegram_group_id,
        "miniapp_origin": str(miniapp_origin or "").strip().lower(),
        "health_origin": str(health_origin or "").strip().lower(),
    }
    if (
        type(telegram_bot_id) is not int or telegram_bot_id <= 0
        or type(telegram_group_id) is not int or telegram_group_id >= 0
        or not ORIGIN_RE.fullmatch(deployment["miniapp_origin"])
        or not ORIGIN_RE.fullmatch(deployment["health_origin"])
    ):
        raise ValueError("deployment identity is invalid")

    manifest_path, manifest, manifest_raw = _json_file(backup_manifest, "backup manifest")
    database = manifest.get("database")
    canary = manifest.get("recovery_key_canary")
    if not isinstance(database, dict) or not isinstance(canary, dict):
        raise ValueError("backup manifest lacks database or recovery-key canary")
    database_sha = str(database.get("sha256") or "").lower()
    canary_sha = str(canary.get("sha256") or "").lower()
    if (
        database.get("path") != "bibitasks.db"
        or type(database.get("bytes")) is not int or database["bytes"] <= 0
        or not SHA256_RE.fullmatch(database_sha)
        or database.get("integrity_check") != "ok"
        or database.get("schema_version") != schema_version
    ):
        raise ValueError("backup database contract is invalid")
    counts = {name: database.get(name) for name in COUNT_FIELDS}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("backup manifest lacks exact encrypted-row counts")
    if counts["telegram_active_null_count"] or counts["withdrawal_active_null_count"]:
        raise ValueError("backup contains active recovery-sensitive rows without ciphertext")
    if (
        set(canary) != {"path", "bytes", "sha256"}
        or canary.get("path") != "recovery-key-canaries.json"
        or type(canary.get("bytes")) is not int or canary["bytes"] <= 0
        or not SHA256_RE.fullmatch(canary_sha)
    ):
        raise ValueError("backup recovery-key canary contract is invalid")
    backup_id = manifest_path.parent.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", backup_id):
        raise ValueError("backup directory name is unsafe")

    attestation = verify_image_attestation(
        commit=commit, image=image, repository=repository,
        signer_workflow=signer_workflow, runner=attestation_runner,
    )
    software = {
        "commit": commit,
        "image": image,
        "schema_version": schema_version,
        "application_version": application_version,
    }
    software_sha = canonical_sha256(software)
    media_items = manifest.get("media") or []
    if not isinstance(media_items, list) or any(
        not isinstance(item, dict) or type(item.get("bytes")) is not int
        or item["bytes"] < 0 for item in media_items
    ):
        raise ValueError("backup local media metadata is invalid")
    backup = {
        "id": backup_id,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "database": {
            "sha256": database_sha, "bytes": database["bytes"], **counts,
        },
        "recovery_key_canary": {
            "sha256": canary_sha, "bytes": canary["bytes"],
        },
        "local_media": {
            "count": len(media_items),
            "bytes": sum(item["bytes"] for item in media_items),
        },
    }
    promotion = {
        "software_subject_sha256": software_sha, "deployment": deployment,
        "backup": backup,
    }
    result = {
        "candidate_version": CANDIDATE_VERSION,
        "created_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        **software,
        "deployment": deployment,
        "backup": backup,
        "image_attestation": {
            "verified_output_sha256": hashlib.sha256(attestation).hexdigest(),
            "predicate_type": "https://slsa.dev/provenance/v1",
            "repository": str(repository).strip(),
            "signer_workflow": str(signer_workflow).strip(),
        },
        "software_subject_sha256": software_sha,
        "promotion_subject_sha256": canonical_sha256(promotion),
        "deployment_authorized": False,
    }
    if file_sha256(manifest_path) != backup["manifest_sha256"]:
        raise ValueError("backup manifest changed during candidate verification")
    return result


def validate_candidate(value: dict) -> dict:
    """Validate a candidate without trusting its stored subject hashes."""
    if value.get("candidate_version") != CANDIDATE_VERSION:
        raise ValueError("release candidate version is unsupported")
    software = {
        "commit": str(value.get("commit") or "").lower(),
        "image": str(value.get("image") or "").lower(),
        "schema_version": value.get("schema_version"),
        "application_version": value.get("application_version"),
    }
    if not COMMIT_RE.fullmatch(software["commit"]) or not IMAGE_RE.fullmatch(software["image"]):
        raise ValueError("release candidate software identity is invalid")
    if type(software["schema_version"]) is not int or software["schema_version"] <= 0:
        raise ValueError("release candidate schema is invalid")
    if not VERSION_RE.fullmatch(str(software["application_version"] or "")):
        raise ValueError("release candidate application version is invalid")
    backup = value.get("backup")
    deployment = value.get("deployment")
    if (
        not isinstance(deployment, dict)
        or type(deployment.get("telegram_bot_id")) is not int
        or deployment["telegram_bot_id"] <= 0
        or type(deployment.get("telegram_group_id")) is not int
        or deployment["telegram_group_id"] >= 0
        or not ORIGIN_RE.fullmatch(str(deployment.get("miniapp_origin") or ""))
        or not ORIGIN_RE.fullmatch(str(deployment.get("health_origin") or ""))
    ):
        raise ValueError("release candidate deployment identity is invalid")
    if not isinstance(backup, dict):
        raise ValueError("release candidate backup is missing")
    database = backup.get("database") or {}
    canary = backup.get("recovery_key_canary") or {}
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", str(backup.get("id") or ""))
        or not SHA256_RE.fullmatch(str(backup.get("manifest_sha256") or ""))
        or not SHA256_RE.fullmatch(str(database.get("sha256") or ""))
        or type(database.get("bytes")) is not int or database["bytes"] <= 0
        or not SHA256_RE.fullmatch(str(canary.get("sha256") or ""))
        or type(canary.get("bytes")) is not int or canary["bytes"] <= 0
    ):
        raise ValueError("release candidate backup binding is invalid")
    counts = {name: database.get(name) for name in COUNT_FIELDS}
    if any(type(item) is not int or item < 0 for item in counts.values()):
        raise ValueError("release candidate encrypted-row counts are invalid")
    if counts["telegram_active_null_count"] or counts["withdrawal_active_null_count"]:
        raise ValueError("release candidate records active NULL ciphertext")
    media = backup.get("local_media") or {}
    if any(type(media.get(key)) is not int or media[key] < 0 for key in ("count", "bytes")):
        raise ValueError("release candidate local media binding is invalid")
    attestation = value.get("image_attestation")
    if (
        not isinstance(attestation, dict)
        or not SHA256_RE.fullmatch(str(attestation.get("verified_output_sha256") or ""))
        or attestation.get("predicate_type") != "https://slsa.dev/provenance/v1"
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(attestation.get("repository") or ""))
        or not re.fullmatch(
            r"github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml",
            str(attestation.get("signer_workflow") or ""),
        )
    ):
        raise ValueError("release candidate image attestation binding is invalid")
    software_sha = canonical_sha256(software)
    promotion_sha = canonical_sha256({
        "software_subject_sha256": software_sha, "deployment": deployment,
        "backup": backup,
    })
    if value.get("software_subject_sha256") != software_sha:
        raise ValueError("release candidate software subject hash is invalid")
    if value.get("promotion_subject_sha256") != promotion_sha:
        raise ValueError("release candidate promotion subject hash is invalid")
    if value.get("deployment_authorized") is not False:
        raise ValueError("release candidate must not authorize deployment")
    return {**software, "deployment": deployment, "backup": backup,
            "software_subject_sha256": software_sha,
            "promotion_subject_sha256": promotion_sha}


def write_candidate(path: Path, candidate: dict) -> Path:
    target = path.expanduser().resolve()
    repo = Path(__file__).resolve().parents[1]
    if target == repo or target.is_relative_to(repo):
        raise ValueError("refusing to write release candidate inside the repository")
    parent = target.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("output parent must be an existing non-symlink directory")
    if os.name != "nt":
        info = parent.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("output parent must be owned by the invoking user and mode 0700")
    if target.exists() or target.is_symlink():
        raise FileExistsError("release candidate target already exists")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(candidate, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(target, 0o600)
        if os.name != "nt":
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--schema-version", required=True, type=int)
    parser.add_argument("--application-version", required=True)
    parser.add_argument("--telegram-bot-id", required=True, type=int)
    parser.add_argument("--telegram-group-id", required=True, type=int)
    parser.add_argument("--miniapp-origin", required=True)
    parser.add_argument("--health-origin", required=True)
    parser.add_argument("--backup-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        candidate = build_candidate(
            commit=args.commit, image=args.image, schema_version=args.schema_version,
            application_version=args.application_version,
            telegram_bot_id=args.telegram_bot_id,
            telegram_group_id=args.telegram_group_id,
            miniapp_origin=args.miniapp_origin, health_origin=args.health_origin,
            backup_manifest=args.backup_manifest, repository=PINNED_REPOSITORY,
            signer_workflow=PINNED_SIGNER_WORKFLOW,
        )
        write_candidate(args.output, candidate)
    except (ValueError, RuntimeError, FileExistsError, OSError):
        print("release candidate failed", file=sys.stderr)
        raise SystemExit(1) from None
    print("release candidate written")


if __name__ == "__main__":
    main()
