"""Validate phase-two evidence without authorizing or writing a release record."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

try:
    from .release_candidate import (
        PINNED_REPOSITORY, PINNED_SIGNER_WORKFLOW, file_sha256, validate_candidate,
    )
    from .release_record import verify_image_attestation
except ImportError:  # pragma: no cover
    from release_candidate import (
        PINNED_REPOSITORY, PINNED_SIGNER_WORKFLOW, file_sha256, validate_candidate,
    )
    from release_record import verify_image_attestation


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}$")
MAX_FUTURE = timedelta(minutes=5)


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _json(path: Path, label: str) -> tuple[Path, dict, str]:
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
        current_stat = os.stat(candidate, follow_symlinks=False)
        if (current_stat.st_dev, current_stat.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError(f"{label} path changed while being read")
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    resolved = candidate.resolve()
    return resolved, value, hashlib.sha256(raw).hexdigest()


def _fresh(value, label: str, now: datetime, max_age: timedelta) -> datetime:
    try:
        when = datetime.fromisoformat(str(value)).astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise ValueError(f"{label} timestamp is invalid") from None
    if when < now - max_age or when > now + MAX_FUTURE:
        raise ValueError(f"{label} is stale or from the future")
    return when


def _load_trust_roots(path: Path) -> tuple[dict[tuple[str, str], dict], Path, str]:
    trust_path, value, trust_sha = _json(path, "trust roots")
    if value.get("trust_roots_version") != 1 or not isinstance(value.get("keys"), list):
        raise ValueError("trust roots contract is unsupported")
    result = {}
    for item in value["keys"]:
        if not isinstance(item, dict) or item.get("enabled") is not True:
            continue
        kind, key_id, issuer = item.get("kind"), item.get("key_id"), item.get("issuer")
        if not all(SAFE_RE.fullmatch(str(part or "")) for part in (kind, key_id, issuer)):
            raise ValueError("trust root identity is invalid")
        try:
            raw = base64.b64decode(str(item.get("public_key_base64") or ""), validate=True)
            key = Ed25519PublicKey.from_public_bytes(raw)
        except (ValueError, TypeError):
            raise ValueError("trust root public key is invalid") from None
        identity = (kind, key_id)
        if identity in result:
            raise ValueError("trust root key identity is duplicated")
        result[identity] = {
            "key": key, "issuer": issuer,
            "public_key_sha256": hashlib.sha256(raw).hexdigest(),
        }
    return result, trust_path, trust_sha


def _verify_signed(
    value: dict, *, kind: str, roots: dict, candidate: dict,
    challenge: str, now: datetime,
) -> dict:
    signature = value.get("signature")
    if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519":
        raise ValueError(f"{kind} evidence lacks an Ed25519 signature")
    key_id = str(signature.get("key_id") or "")
    trusted = roots.get((kind, key_id))
    if trusted is None or value.get("issuer") != trusted["issuer"]:
        raise ValueError(f"{kind} evidence signer is not trusted")
    if (
        value.get("evidence_version") != 1 or value.get("kind") != kind
        or value.get("promotion_subject_sha256") != candidate["promotion_subject_sha256"]
        or value.get("software_subject_sha256") != candidate["software_subject_sha256"]
        or value.get("challenge") != challenge
    ):
        raise ValueError(f"{kind} evidence subject or challenge mismatch")
    max_age = timedelta(minutes=5 if kind == "release_authorization" else 60)
    generated_at = _fresh(
        value.get("generated_at"), f"{kind} evidence", now, max_age,
    )
    unsigned = dict(value)
    unsigned.pop("signature", None)
    try:
        raw_signature = base64.b64decode(str(signature.get("value_base64") or ""), validate=True)
        trusted["key"].verify(raw_signature, _canonical(unsigned))
    except (ValueError, InvalidSignature):
        raise ValueError(f"{kind} evidence signature is invalid") from None
    return {
        "key_id": key_id, "issuer": trusted["issuer"],
        "public_key_sha256": trusted["public_key_sha256"],
        "generated_at": generated_at.isoformat(),
    }


def _green_live(value: dict, label: str, now: datetime) -> datetime:
    generated_at = _fresh(
        value.get("generated_at"), label, now, timedelta(minutes=5),
    )
    if value.get("ok") is not True:
        raise ValueError(f"{label} is not green")
    return generated_at


def build_final_record(
    *, candidate_file: Path, candidate_sha256: str, rollback_verify: Path,
    secret_recovery: Path, preflight: Path, readiness: Path, monitor: Path,
    external_deadman: Path, live_e2e: Path, authorization: Path,
    trust_roots: Path, challenge: str, expected_trust_roots_sha256: str,
    now: datetime | None = None, attestation_runner=None,
) -> dict:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    challenge = str(challenge or "").lower()
    if not SHA256_RE.fullmatch(challenge):
        raise ValueError("challenge must be an independently generated 32-byte hex value")
    candidate_path, raw_candidate, parsed_candidate_sha = _json(candidate_file, "release candidate")
    if not SHA256_RE.fullmatch(candidate_sha256) or parsed_candidate_sha != candidate_sha256:
        raise ValueError("release candidate digest mismatch")
    candidate = validate_candidate(raw_candidate)
    attestation_raw = verify_image_attestation(
        commit=candidate["commit"], image=candidate["image"],
        repository=PINNED_REPOSITORY, signer_workflow=PINNED_SIGNER_WORKFLOW,
        runner=attestation_runner,
    )
    paths_values = {}
    for name, path in (
        ("rollback", rollback_verify), ("secret", secret_recovery),
        ("preflight", preflight), ("readiness", readiness), ("monitor", monitor),
        ("deadman", external_deadman), ("e2e", live_e2e),
        ("authorization", authorization),
    ):
        paths_values[name] = _json(path, name)
    roots, trust_path, trust_sha = _load_trust_roots(trust_roots)
    if (
        not SHA256_RE.fullmatch(str(expected_trust_roots_sha256 or ""))
        or trust_sha != expected_trust_roots_sha256
    ):
        raise ValueError("trust roots do not match the protected policy digest")
    rollback = paths_values["rollback"][1]
    if (
        rollback.get("report_version") != 1 or rollback.get("ok") is not True
        or rollback.get("production_activation_enabled") is not False
        or rollback.get("candidate_sha256") != candidate_sha256
        or rollback.get("software_subject_sha256") != candidate["software_subject_sha256"]
        or rollback.get("promotion_subject_sha256") != candidate["promotion_subject_sha256"]
    ):
        raise ValueError("rollback verification does not bind the candidate")
    rollback_time = _fresh(
        rollback.get("generated_at"), "rollback verification", current,
        timedelta(hours=4),
    )
    final = rollback.get("final_validation") or {}
    backup = candidate["backup"]
    rollback_target = rollback.get("target") or {}
    if (
        (rollback.get("current") or {}).get("present") is not True
        or rollback_target.get("commit") != candidate["commit"]
        or rollback_target.get("image") != candidate["image"]
        or rollback_target.get("schema_version") != candidate["schema_version"]
        or rollback_target.get("application_version") != candidate["application_version"]
        or final.get("integrity_check") != "ok"
        or final.get("manifest_sha256") != backup["manifest_sha256"]
        or final.get("database_sha256") != backup["database"]["sha256"]
        or final.get("canary_sha256") != backup["recovery_key_canary"]["sha256"]
        or final.get("canary_ok") is not True
        or final.get("local_media_count") != backup["local_media"]["count"]
        or final.get("local_media_bytes") != backup["local_media"]["bytes"]
        or final.get("local_media_ok") is not True
        or final.get("all_owned") is not True or final.get("all_readable") is not True
    ):
        raise ValueError("rollback verification differs from candidate backup")
    secret = paths_values["secret"][1]
    release = secret.get("release") or {}
    if (
        secret.get("report_version") != 2 or secret.get("ok") is not True
        or release.get("commit_sha") != candidate["commit"]
        or release.get("immutable_image_sha256") != candidate["image"].rsplit("@sha256:", 1)[1]
        or release.get("schema_version") != candidate["schema_version"]
        or release.get("release_version_sha256") != hashlib.sha256(candidate["application_version"].encode()).hexdigest()
        or (secret.get("backup") or {}).get("manifest_sha256") != backup["manifest_sha256"]
    ):
        raise ValueError("secret recovery v2 does not bind the candidate")
    recovered_database = secret.get("database") or {}
    expected_counts = {
        "telegram_ciphertext_expected": backup["database"]["telegram_ciphertext_count"],
        "telegram_active_null_expected": backup["database"]["telegram_active_null_count"],
        "withdrawal_ciphertext_expected": backup["database"]["withdrawal_ciphertext_count"],
        "withdrawal_active_null_expected": backup["database"]["withdrawal_active_null_count"],
    }
    if (
        any(recovered_database.get(key) != value for key, value in expected_counts.items())
        or recovered_database.get("expected_counts_verified") is not True
        or recovered_database.get("telegram_row_binding_verified") is not True
        or recovered_database.get("telegram_hmac_verified") is not True
        or recovered_database.get("withdrawal_hmac_verified") is not True
        or (secret.get("keys") or {}).get("pre_disaster_canary_verified") is not True
        or (secret.get("recovery_key_canary") or {}).get("sha256")
        != backup["recovery_key_canary"]["sha256"]
    ):
        raise ValueError("secret recovery counts/key bindings are incomplete")
    secret_time = _fresh(
        secret.get("generated_at"), "secret recovery", current,
        timedelta(hours=4),
    )
    secret_live = secret.get("live_evidence") or {}
    for secret_name, input_name in (
        ("telegram_preflight", "preflight"), ("readiness", "readiness"),
        ("monitor_canary", "monitor"),
    ):
        if (secret_live.get(secret_name) or {}).get("sha256") != paths_values[input_name][2]:
            raise ValueError("secret recovery does not bind the exact live evidence")
    live_times = {
        name: _green_live(paths_values[name][1], name, current)
        for name in ("preflight", "readiness", "monitor")
    }
    preflight_value = paths_values["preflight"][1]
    summary = preflight_value.get("summary") or {}
    if (
        preflight_value.get("report_version") != 1
        or any(type(summary.get(key)) is not int or summary[key] < 0
               for key in ("pass", "warn", "fail"))
        or summary.get("fail") != 0
    ):
        raise ValueError("Telegram preflight contract is incomplete")
    ready = paths_values["readiness"][1]
    if (
        ready.get("application_version") != candidate["application_version"]
        or ready.get("telegram_update_mode") != "webhook"
        or ready.get("telegram_receiver_ready") is not True
        or ready.get("webhook_configured") is not True
        or ready.get("lifecycle_worker_alive") is not True
        or ready.get("outbox_worker_alive") is not True
        or ready.get("telegram_inbox_worker_alive") is not True
        or ready.get("withdrawal_encryption_ready") is not True
        or ready.get("telegram_inbox_encryption_ready") is not True
        or ready.get("outbox_dead") != 0 or ready.get("telegram_inbox_dead") != 0
    ):
        raise ValueError("readiness contract differs from candidate or is incomplete")
    monitor_value = paths_values["monitor"][1]
    monitor_checks = monitor_value.get("checks") or {}
    if (
        monitor_value.get("schema_version") != 1
        or monitor_value.get("heartbeat_ok") is not True
        or monitor_value.get("alert_delivery_ok") is not True
        or any(
            not isinstance(monitor_checks.get(key), dict)
            or monitor_checks[key].get("last_healthy") is not True
            or monitor_checks[key].get("alert_active") is not False
            for key in ("application", "dead_queues", "backup")
        )
    ):
        raise ValueError("monitor report contract is incomplete")
    deadman = paths_values["deadman"][1]
    e2e = paths_values["e2e"][1]
    authorization = paths_values["authorization"][1]
    signers = {
        kind: _verify_signed(value, kind=kind, roots=roots, candidate=candidate,
                             challenge=challenge, now=current)
        for kind, value in (
            ("external_deadman", deadman), ("live_e2e", e2e),
            ("release_authorization", authorization),
        )
    }
    if (
        len({item["key_id"] for item in signers.values()}) != 3
        or len({item["public_key_sha256"] for item in signers.values()}) != 3
    ):
        raise ValueError("dead-man, E2E and authorization require distinct trusted keys")
    authorization_time = datetime.fromisoformat(
        signers["release_authorization"]["generated_at"]
    )
    evidence_times = [
        rollback_time, secret_time, *live_times.values(),
        datetime.fromisoformat(signers["external_deadman"]["generated_at"]),
        datetime.fromisoformat(signers["live_e2e"]["generated_at"]),
    ]
    if authorization_time < max(evidence_times):
        raise ValueError("release authorization predates required evidence")
    if (
        deadman.get("result") != {
            "observer_external": True, "incident_delivered": True,
            "recovery_delivered": True,
        }
    ):
        raise ValueError("external dead-man drill is incomplete")
    if e2e.get("result") != {
        "bot_join": True, "group_flow": True, "miniapp_flow": True,
        "task_photo_flow": True, "bonus_flow": True,
    }:
        raise ValueError("live E2E evidence is incomplete")
    evidence_hashes = {
        name: digest for name, (path, value, digest) in paths_values.items()
        if name != "authorization"
    }
    if authorization.get("decision") != "authorize" or authorization.get("evidence_sha256") != evidence_hashes:
        raise ValueError("signed authorization does not bind all exact evidence")

    stable_inputs = [(candidate_path, candidate_sha256), (trust_path, trust_sha)] + [
        (path, digest) for path, value, digest in paths_values.values()
    ]
    if any(file_sha256(path) != expected for path, expected in stable_inputs):
        raise ValueError("release evidence changed during final verification")
    # Deliberate terminal NO-GO.  The repository does not yet have a
    # cryptographically verifiable two-custodian recovery ceremony, a protected
    # single-use challenge ledger, or a deployment controller that verifies a
    # signed final record immediately before changing production.  Emitting
    # ``go: true`` before all three exist would turn this validator into a paper
    # approval.  Keep the complete validation above so the remaining integration
    # can be exercised, but never manufacture an authorization artifact.
    raise ValueError(
        "release authorization is not implemented: cryptographic custodian "
        "quorum, protected single-use challenge ledger and signed deployment "
        "enforcement are required"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("candidate-file", "rollback-verify", "secret-recovery", "preflight",
                 "readiness", "monitor", "external-deadman", "live-e2e",
                 "authorization", "trust-roots"):
        parser.add_argument("--" + flag, required=True, type=Path)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--challenge", required=True)
    args = parser.parse_args()
    try:
        expected_trust_sha = os.environ.get("BIBITASKS_RELEASE_TRUST_ROOT_SHA256", "")
        build_final_record(
            candidate_file=args.candidate_file, candidate_sha256=args.candidate_sha256,
            rollback_verify=args.rollback_verify, secret_recovery=args.secret_recovery,
            preflight=args.preflight, readiness=args.readiness, monitor=args.monitor,
            external_deadman=args.external_deadman, live_e2e=args.live_e2e,
            authorization=args.authorization, trust_roots=args.trust_roots,
            challenge=args.challenge,
            expected_trust_roots_sha256=expected_trust_sha,
        )
    except (ValueError, FileExistsError, OSError):
        print("final release gate failed", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit("final release gate is non-authorizing")


if __name__ == "__main__":
    main()
