import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.release_candidate import (
    build_candidate, canonical_sha256, validate_candidate, write_candidate,
)


class ReleaseCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backup = self.root / "backup-001"
        self.backup.mkdir()
        self.manifest = self.backup / "manifest.json"
        self.payload = {
            "database": {
                "path": "bibitasks.db", "bytes": 123, "sha256": "d" * 64,
                "integrity_check": "ok", "schema_version": 295,
                "telegram_ciphertext_count": 7, "telegram_active_null_count": 0,
                "withdrawal_ciphertext_count": 3, "withdrawal_active_null_count": 0,
            },
            "recovery_key_canary": {
                "path": "recovery-key-canaries.json", "bytes": 321,
                "sha256": "e" * 64,
            },
            "media": [{"path": "proof.jpg", "bytes": 12, "sha256": "1" * 64}],
        }
        self.manifest.write_text(json.dumps(self.payload), "utf-8")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def runner(command, **kwargs):
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

    def build(self):
        return build_candidate(
            commit="a" * 40,
            image="ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
            schema_version=295, application_version="v2.10.0",
            telegram_bot_id=123456, telegram_group_id=-1001234567890,
            miniapp_origin="https://tasks.example.com",
            health_origin="https://health.example.com",
            backup_manifest=self.manifest, repository="voglogpro/-",
            signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
            attestation_runner=self.runner,
            now=datetime(2026, 7, 28, tzinfo=timezone.utc),
        )

    def test_candidate_binds_software_backup_counts_and_attestation(self):
        value = self.build()
        validated = validate_candidate(value)
        self.assertFalse(value["deployment_authorized"])
        self.assertEqual(validated["application_version"], "v2.10.0")
        self.assertEqual(validated["backup"]["database"]["telegram_ciphertext_count"], 7)
        software = {key: value[key] for key in (
            "commit", "image", "schema_version", "application_version",
        )}
        self.assertEqual(value["software_subject_sha256"], canonical_sha256(software))
        self.assertRegex(value["promotion_subject_sha256"], r"^[0-9a-f]{64}$")

    def test_manifest_mismatch_and_active_null_fail_closed(self):
        self.payload["database"]["schema_version"] = 292
        self.manifest.write_text(json.dumps(self.payload), "utf-8")
        with self.assertRaisesRegex(ValueError, "database contract"):
            self.build()
        self.payload["database"]["schema_version"] = 295
        self.payload["database"]["telegram_active_null_count"] = 1
        self.manifest.write_text(json.dumps(self.payload), "utf-8")
        with self.assertRaisesRegex(ValueError, "without ciphertext"):
            self.build()

    def test_subject_tamper_is_rejected(self):
        value = self.build()
        value["backup"]["database"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "promotion subject"):
            validate_candidate(value)
        value = self.build()
        value["commit"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "software subject"):
            validate_candidate(value)

    def test_output_is_exclusive_and_outside_repository(self):
        target = self.root / "candidate.json"
        write_candidate(target, self.build())
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), hashlib.sha256(target.read_bytes()).hexdigest())
        with self.assertRaises(FileExistsError):
            write_candidate(target, self.build())
        repo_target = Path(__file__).resolve().parents[1] / "forbidden-candidate.json"
        with self.assertRaisesRegex(ValueError, "inside the repository"):
            write_candidate(repo_target, self.build())

    def test_attestation_failure_and_unsafe_version_are_redacted_by_contract(self):
        def bad_runner(*args, **kwargs):
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"SECRET")
        with self.assertRaisesRegex(ValueError, "verification failed") as caught:
            build_candidate(
                commit="a" * 40,
                image="ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
                schema_version=295, application_version="v2.10.0",
                telegram_bot_id=123456, telegram_group_id=-1001234567890,
                miniapp_origin="https://tasks.example.com",
                health_origin="https://health.example.com",
                backup_manifest=self.manifest, repository="voglogpro/-",
                signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                attestation_runner=bad_runner,
            )
        self.assertNotIn("SECRET", str(caught.exception))
        with self.assertRaisesRegex(ValueError, "release policy"):
            build_candidate(
                commit="a" * 40,
                image="ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
                schema_version=295, application_version="v2.10.0",
                telegram_bot_id=123456, telegram_group_id=-1001234567890,
                miniapp_origin="https://tasks.example.com",
                health_origin="https://health.example.com",
                backup_manifest=self.manifest, repository="attacker/repo",
                signer_workflow="github.com/attacker/repo/.github/workflows/release.yml",
                attestation_runner=self.runner,
            )
        with self.assertRaisesRegex(ValueError, "semantic version"):
            build_candidate(
                commit="a" * 40,
                image="ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
                schema_version=295, application_version="v2.10.0\nINJECT",
                telegram_bot_id=123456, telegram_group_id=-1001234567890,
                miniapp_origin="https://tasks.example.com",
                health_origin="https://health.example.com",
                backup_manifest=self.manifest, repository="voglogpro/-",
                signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                attestation_runner=self.runner,
            )


if __name__ == "__main__":
    unittest.main()
