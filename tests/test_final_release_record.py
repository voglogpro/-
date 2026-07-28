import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.final_release_record import build_final_record
from scripts.release_candidate import canonical_sha256


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


class FinalReleaseRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        software = {
            "commit": "a" * 40,
            "image": "ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
            "schema_version": 295, "application_version": "v2.10.0",
        }
        backup = {
            "id": "backup-001", "manifest_sha256": "c" * 64,
            "database": {
                "sha256": "d" * 64, "bytes": 100,
                "telegram_ciphertext_count": 2, "telegram_active_null_count": 0,
                "withdrawal_ciphertext_count": 1, "withdrawal_active_null_count": 0,
            },
            "recovery_key_canary": {"sha256": "e" * 64, "bytes": 200},
            "local_media": {"count": 1, "bytes": 12},
        }
        deployment = {
            "telegram_bot_id": 123456, "telegram_group_id": -1001234567890,
            "miniapp_origin": "https://tasks.example.com",
            "health_origin": "https://health.example.com",
        }
        software_hash = canonical_sha256(software)
        self.candidate = {
            "candidate_version": 1, **software, "deployment": deployment,
            "backup": backup,
            "image_attestation": {
                "verified_output_sha256": "9" * 64,
                "predicate_type": "https://slsa.dev/provenance/v1",
                "repository": "voglogpro/-",
                "signer_workflow": "github.com/voglogpro/-/.github/workflows/release.yml",
            },
            "software_subject_sha256": software_hash,
            "promotion_subject_sha256": canonical_sha256({
                "software_subject_sha256": software_hash,
                "deployment": deployment, "backup": backup,
            }),
            "deployment_authorized": False,
        }
        self.files = {}
        self.write("candidate", self.candidate)
        self.candidate_sha = self.digest(self.files["candidate"])
        self.write("rollback", {
            "report_version": 1, "generated_at": NOW.isoformat(), "ok": True,
            "production_activation_enabled": False,
            "candidate_sha256": self.candidate_sha,
            "software_subject_sha256": software_hash,
            "promotion_subject_sha256": self.candidate["promotion_subject_sha256"],
            "current": {"present": True},
            "target": {
                "commit": "a" * 40,
                "image": "ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
                "schema_version": 295, "application_version": "v2.10.0",
            },
            "final_validation": {
                "integrity_check": "ok", "manifest_sha256": "c" * 64,
                "database_sha256": "d" * 64, "canary_sha256": "e" * 64,
                "canary_ok": True,
                "local_media_count": 1, "local_media_bytes": 12,
                "local_media_ok": True, "all_owned": True, "all_readable": True,
            },
        })
        self.write("secret", {
            "report_version": 2, "generated_at": NOW.isoformat(), "ok": True,
            "release": {
                "commit_sha": "a" * 40, "immutable_image_sha256": "b" * 64,
                "release_version_sha256": hashlib.sha256(b"v2.10.0").hexdigest(),
                "schema_version": 295,
            },
            "backup": {"manifest_sha256": "c" * 64},
            "database": {
                "expected_counts_verified": True,
                "telegram_ciphertext_expected": 2,
                "telegram_active_null_expected": 0,
                "withdrawal_ciphertext_expected": 1,
                "withdrawal_active_null_expected": 0,
                "telegram_row_binding_verified": True,
                "telegram_hmac_verified": True,
                "withdrawal_hmac_verified": True,
            },
            "keys": {"pre_disaster_canary_verified": True},
            "recovery_key_canary": {"sha256": "e" * 64},
            "operator_assertions": {"custodian_quorum_cryptographically_verified": False},
        })
        self.write("preflight", {
            "report_version": 1, "generated_at": NOW.isoformat(), "ok": True,
            "summary": {"pass": 1, "warn": 0, "fail": 0},
        })
        self.write("readiness", {
            "report_version": 1, "generated_at": NOW.isoformat(), "ok": True,
            "application_version": "v2.10.0", "telegram_update_mode": "webhook",
            "telegram_receiver_ready": True, "webhook_configured": True,
            "lifecycle_worker_alive": True, "outbox_worker_alive": True,
            "telegram_inbox_worker_alive": True,
            "withdrawal_encryption_ready": True,
            "telegram_inbox_encryption_ready": True,
            "outbox_dead": 0, "telegram_inbox_dead": 0,
        })
        healthy = {"last_healthy": True, "alert_active": False}
        self.write("monitor", {
            "schema_version": 1, "generated_at": NOW.isoformat(), "ok": True,
            "heartbeat_ok": True, "alert_delivery_ok": True,
            "checks": {key: dict(healthy) for key in ("application", "dead_queues", "backup")},
        })
        secret_value = json.loads(self.files["secret"].read_text())
        secret_value["live_evidence"] = {
            "telegram_preflight": {"sha256": self.digest(self.files["preflight"])},
            "readiness": {"sha256": self.digest(self.files["readiness"])},
            "monitor_canary": {"sha256": self.digest(self.files["monitor"])},
        }
        self.write("secret", secret_value)
        self.challenge = "f" * 64
        self.keys = {kind: Ed25519PrivateKey.generate() for kind in (
            "external_deadman", "live_e2e", "release_authorization",
        )}
        self.write("deadman", self.signed("external_deadman", {
            "observer_external": True, "incident_delivered": True,
            "recovery_delivered": True,
        }))
        self.write("e2e", self.signed("live_e2e", {
            "bot_join": True, "group_flow": True, "miniapp_flow": True,
            "task_photo_flow": True, "bonus_flow": True,
        }))
        evidence = {name: self.digest(path) for name, path in self.files.items()
                    if name not in ("candidate", "authorization")}
        authorization = self.signed("release_authorization", None)
        authorization.pop("result")
        authorization["decision"] = "authorize"
        authorization["evidence_sha256"] = evidence
        self.resign(authorization, "release_authorization")
        self.write("authorization", authorization)
        roots = {"trust_roots_version": 1, "keys": []}
        for kind, private in self.keys.items():
            public = private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw,
            )
            roots["keys"].append({
                "kind": kind, "key_id": f"{kind}-key", "issuer": f"{kind}.example",
                "public_key_base64": base64.b64encode(public).decode(), "enabled": True,
            })
        self.write("trust", roots)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(value), "utf-8")
        self.files[name] = path

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def signed(self, kind, result):
        value = {
            "evidence_version": 1, "kind": kind, "issuer": f"{kind}.example",
            "generated_at": NOW.isoformat(),
            "promotion_subject_sha256": self.candidate["promotion_subject_sha256"],
            "software_subject_sha256": self.candidate["software_subject_sha256"],
            "challenge": self.challenge, "result": result,
        }
        self.resign(value, kind)
        return value

    def resign(self, value, kind):
        value.pop("signature", None)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        value["signature"] = {
            "algorithm": "ed25519", "key_id": f"{kind}-key",
            "value_base64": base64.b64encode(self.keys[kind].sign(raw)).decode(),
        }

    def args(self):
        return {
            "candidate_file": self.files["candidate"],
            "candidate_sha256": self.candidate_sha,
            "rollback_verify": self.files["rollback"],
            "secret_recovery": self.files["secret"], "preflight": self.files["preflight"],
            "readiness": self.files["readiness"], "monitor": self.files["monitor"],
            "external_deadman": self.files["deadman"], "live_e2e": self.files["e2e"],
            "authorization": self.files["authorization"], "trust_roots": self.files["trust"],
            "expected_trust_roots_sha256": self.digest(self.files["trust"]),
            "challenge": self.challenge, "now": NOW,
            "attestation_runner": self.attestation_runner,
        }

    @staticmethod
    def attestation_runner(command, **kwargs):
        commit = command[command.index("--source-digest") + 1]
        image = command[3][len("oci://"):]
        name, digest = image.rsplit("@sha256:", 1)
        report = [{"verificationResult": {
            "signature": {"certificate": {"sourceRepositoryDigest": commit}},
            "verifiedTimestamps": [{"type": "transparency-log"}],
            "statement": {
                "predicateType": "https://slsa.dev/provenance/v1",
                "subject": [{"name": name, "digest": {"sha256": digest}}],
            },
        }}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(report).encode(), stderr=b"")

    def test_even_complete_evidence_is_terminal_no_go_until_enforcement_exists(self):
        target = self.root / "final.json"
        with self.assertRaisesRegex(ValueError, "release authorization is not implemented"):
            build_final_record(**self.args())
        self.assertFalse(target.exists())

    def test_missing_or_untrusted_signature_fails_closed(self):
        value = json.loads(self.files["deadman"].read_text())
        value.pop("signature")
        self.write("deadman", value)
        with self.assertRaisesRegex(ValueError, "lacks an Ed25519"):
            build_final_record(**self.args())

    def test_trust_root_must_match_out_of_band_pin(self):
        args = self.args()
        args["expected_trust_roots_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "protected policy digest"):
            build_final_record(**args)

    def test_final_reverifies_pinned_image_provenance(self):
        args = self.args()
        args["attestation_runner"] = lambda *a, **k: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"untrusted detail",
        )
        with self.assertRaisesRegex(ValueError, "verification failed") as caught:
            build_final_record(**args)
        self.assertNotIn("untrusted detail", str(caught.exception))

    def test_different_key_ids_with_same_public_key_are_not_independent(self):
        deadman_private = self.keys["external_deadman"]
        value = json.loads(self.files["e2e"].read_text())
        value.pop("signature")
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        value["signature"] = {
            "algorithm": "ed25519", "key_id": "live_e2e-key",
            "value_base64": base64.b64encode(deadman_private.sign(raw)).decode(),
        }
        self.write("e2e", value)
        roots = json.loads(self.files["trust"].read_text())
        roots["keys"][1]["public_key_base64"] = roots["keys"][0]["public_key_base64"]
        self.write("trust", roots)
        with self.assertRaisesRegex(ValueError, "distinct trusted keys"):
            build_final_record(**self.args())

    def test_stale_and_subject_mismatch_fail_closed(self):
        value = json.loads(self.files["monitor"].read_text())
        value["generated_at"] = (NOW - timedelta(hours=25)).isoformat()
        self.write("monitor", value)
        secret = json.loads(self.files["secret"].read_text())
        secret["live_evidence"]["monitor_canary"]["sha256"] = self.digest(self.files["monitor"])
        self.write("secret", secret)
        with self.assertRaisesRegex(ValueError, "stale"):
            build_final_record(**self.args())
        healthy = {"last_healthy": True, "alert_active": False}
        self.write("monitor", {
            "schema_version": 1, "generated_at": NOW.isoformat(), "ok": True,
            "heartbeat_ok": True, "alert_delivery_ok": True,
            "checks": {key: dict(healthy) for key in ("application", "dead_queues", "backup")},
        })
        secret = json.loads(self.files["secret"].read_text())
        secret["live_evidence"]["monitor_canary"]["sha256"] = self.digest(self.files["monitor"])
        self.write("secret", secret)
        value = json.loads(self.files["e2e"].read_text())
        value["promotion_subject_sha256"] = "0" * 64
        self.resign(value, "live_e2e")
        self.write("e2e", value)
        with self.assertRaisesRegex(ValueError, "subject or challenge mismatch"):
            build_final_record(**self.args())

    def test_tampered_authorization_evidence_map_and_injection_fail(self):
        value = json.loads(self.files["authorization"].read_text())
        value["evidence_sha256"]["monitor"] = "0" * 64
        self.resign(value, "release_authorization")
        self.write("authorization", value)
        with self.assertRaisesRegex(ValueError, "does not bind"):
            build_final_record(**self.args())
        roots = json.loads(self.files["trust"].read_text())
        roots["keys"][0]["issuer"] = "bad\nissuer"
        self.write("trust", roots)
        with self.assertRaisesRegex(ValueError, "identity is invalid") as caught:
            build_final_record(**self.args())
        self.assertNotIn("bad\nissuer", str(caught.exception))

    def test_authorization_must_postdate_all_evidence(self):
        value = json.loads(self.files["authorization"].read_text())
        value["generated_at"] = (NOW - timedelta(minutes=1)).isoformat()
        self.resign(value, "release_authorization")
        self.write("authorization", value)
        with self.assertRaisesRegex(ValueError, "predates"):
            build_final_record(**self.args())


if __name__ == "__main__":
    unittest.main()
