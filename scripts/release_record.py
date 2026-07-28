"""Build a fail-closed, redacted promotion record from verified live evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(r"^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORKFLOW_RE = re.compile(
    r"^github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/"
    r"[A-Za-z0-9_.-]+\.ya?ml$"
)


def sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes):
    return hashlib.sha256(value).hexdigest()


def _json_file(path: Path, label: str, expected_type=dict):
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"{label} must be an existing regular file")
    try:
        value = json.loads(resolved.read_text("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must contain valid UTF-8 JSON") from exc
    if not isinstance(value, expected_type):
        expected = "object" if expected_type is dict else "array"
        raise ValueError(f"{label} must contain a JSON {expected}")
    return resolved, value


def _contains_string(value, expected):
    if isinstance(value, dict):
        return any(_contains_string(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_string(item, expected) for item in value)
    return str(value or "").strip().lower() == expected


def _verified_attestation(report, *, commit, image):
    subject_name, image_digest = image.rsplit("@sha256:", 1)
    for item in report:
        verification = item.get("verificationResult") if isinstance(item, dict) else None
        statement = (verification or {}).get("statement") or {}
        signature = (verification or {}).get("signature") or {}
        subjects = statement.get("subject") or []
        subject_matches = any(
            isinstance(subject, dict)
            and str(subject.get("name", "")).strip().lower() == subject_name
            and str((subject.get("digest") or {}).get("sha256", "")).lower() == image_digest
            for subject in subjects
        )
        if (
            subject_matches
            and statement.get("predicateType") == "https://slsa.dev/provenance/v1"
            and isinstance(signature.get("certificate"), dict)
            and (verification or {}).get("verifiedTimestamps")
            and _contains_string(signature["certificate"], commit)
        ):
            return True
    return False


def verify_image_attestation(
    *, commit, image, repository, signer_workflow, runner=None,
):
    """Cryptographically verify GHCR provenance with GitHub CLI, fail closed."""
    if not REPOSITORY_RE.fullmatch(str(repository or "").strip()):
        raise ValueError("repository must use owner/name format")
    if not WORKFLOW_RE.fullmatch(str(signer_workflow or "").strip()):
        raise ValueError("signer workflow must be an exact github.com workflow path")
    command = [
        "gh", "attestation", "verify", f"oci://{image}",
        "--repo", str(repository).strip(),
        "--source-digest", commit,
        "--signer-workflow", str(signer_workflow).strip(),
        "--deny-self-hosted-runners", "--format", "json",
    ]
    execute = runner or subprocess.run
    try:
        result = execute(
            command, capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(
            f"GitHub attestation verification could not run: {type(exc).__name__}"
        ) from None
    if result.returncode != 0:
        raise ValueError("GitHub attestation verification failed")
    raw = result.stdout if isinstance(result.stdout, bytes) else str(result.stdout).encode()
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("GitHub attestation verification returned invalid JSON") from None
    if not isinstance(report, list) or not _verified_attestation(
        report, commit=commit, image=image,
    ):
        raise ValueError("verified attestation does not bind image digest to commit")
    return raw


def build_release_record(
    *, commit: str, image: str, schema_version: int,
    backup_manifest: Path, restore_report: Path, preflight_report: Path,
    readiness_report: Path, repository: str, signer_workflow: str,
    approved_by: str, second_approved_by: str,
    attestation_runner=None,
):
    commit = str(commit or "").strip().lower()
    image = str(image or "").strip().lower()
    first = " ".join(str(approved_by or "").split())[:80]
    second = " ".join(str(second_approved_by or "").split())[:80]
    if not COMMIT_RE.fullmatch(commit):
        raise ValueError("commit must be a full 40-character SHA")
    if not IMAGE_RE.fullmatch(image):
        raise ValueError("image must be an immutable lowercase GHCR @sha256 reference")
    if int(schema_version) <= 0:
        raise ValueError("schema version must be positive")
    if not first or not second or first.casefold() == second.casefold():
        raise ValueError("two distinct approvers are required")

    manifest_path, manifest = _json_file(backup_manifest, "backup manifest")
    restore_path, restore = _json_file(restore_report, "restore report")
    preflight_path, preflight = _json_file(preflight_report, "preflight report")
    readiness_path, readiness = _json_file(readiness_report, "readiness report")
    attestation_raw = verify_image_attestation(
        commit=commit, image=image, repository=repository,
        signer_workflow=signer_workflow, runner=attestation_runner,
    )
    database = manifest.get("database") or {}
    manifest_digest = sha256(manifest_path)
    if database.get("integrity_check") != "ok":
        raise ValueError("backup manifest does not prove database integrity")
    if int(database.get("schema_version", -1)) != int(schema_version):
        raise ValueError("backup schema version differs from release schema")
    database_digest = str(database.get("sha256", "")).strip().lower()
    restored_database_digest = str(
        restore.get("database_sha256_after_restore", "")
    ).strip().lower()
    if (
        restore.get("integrity_check") != "ok"
        or int(restore.get("schema_version", -1)) != int(schema_version)
        or restore.get("source_manifest_sha256") != manifest_digest
        or not SHA256_RE.fullmatch(database_digest)
        or restored_database_digest != database_digest
    ):
        raise ValueError("restore rehearsal does not match the release backup")
    if preflight.get("ok") is not True or int(
        (preflight.get("summary") or {}).get("fail", -1)
    ) != 0:
        raise ValueError("Telegram preflight is not green")
    required_ready = (
        readiness.get("ok") is True
        and readiness.get("telegram_update_mode") == "webhook"
        and readiness.get("telegram_receiver_ready") is True
        and readiness.get("webhook_configured") is True
        and readiness.get("lifecycle_worker_alive") is True
        and readiness.get("outbox_worker_alive") is True
        and readiness.get("telegram_inbox_worker_alive") is True
        and int(readiness.get("outbox_dead", -1)) == 0
        and int(readiness.get("telegram_inbox_dead", -1)) == 0
    )
    if not required_ready:
        raise ValueError("readiness report does not prove a healthy webhook deployment")

    return {
        "record_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "commit": commit,
        "image": image,
        "schema_version": int(schema_version),
        "backup": {
            "id": manifest_path.parent.name,
            "manifest_sha256": manifest_digest,
            "database_sha256": database_digest,
        },
        "restore": {
            "report_sha256": sha256(restore_path),
            "integrity_check": "ok",
            "database_sha256_after_restore": restored_database_digest,
        },
        "image_attestation": {
            "verified_output_sha256": sha256_bytes(attestation_raw),
            "predicate_type": "https://slsa.dev/provenance/v1",
            "repository": str(repository).strip(),
            "signer_workflow": str(signer_workflow).strip(),
        },
        "telegram_preflight": {
            "report_sha256": sha256(preflight_path),
            "summary": preflight["summary"],
        },
        "readiness": {
            "report_sha256": sha256(readiness_path),
            "version": str(readiness.get("version", "")),
            "telegram_update_mode": str(readiness.get("telegram_update_mode", "")),
        },
        "approvals": [first, second],
    }


def write_record(path: Path, record):
    target = path.expanduser().resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(target, 0o600)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def main():
    parser = argparse.ArgumentParser(description="Create a BibiTasks promotion record")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--schema-version", type=int, required=True)
    parser.add_argument("--backup-manifest", type=Path, required=True)
    parser.add_argument("--restore-report", type=Path, required=True)
    parser.add_argument("--preflight-report", type=Path, required=True)
    parser.add_argument("--readiness-report", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--signer-workflow", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--second-approved-by", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        record = build_release_record(
            commit=args.commit, image=args.image, schema_version=args.schema_version,
            backup_manifest=args.backup_manifest,
            restore_report=args.restore_report,
            preflight_report=args.preflight_report,
            readiness_report=args.readiness_report,
            repository=args.repository, signer_workflow=args.signer_workflow,
            approved_by=args.approved_by,
            second_approved_by=args.second_approved_by,
        )
        target = write_record(args.output, record)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
