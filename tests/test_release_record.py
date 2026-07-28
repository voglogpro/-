import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.release_record import build_release_record, write_record


class ReleaseRecordTests(unittest.TestCase):
    def fixtures(self, root):
        root = Path(root)
        backup = root / "backup-001"
        backup.mkdir()
        manifest = backup / "manifest.json"
        manifest.write_text(json.dumps({
            "database": {
                "integrity_check": "ok", "schema_version": 290,
                "sha256": "d" * 64,
            },
        }), "utf-8")
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        restore = root / "restore.json"
        restore.write_text(json.dumps({
            "integrity_check": "ok", "schema_version": 290,
            "source_manifest_sha256": digest,
            "database_sha256_after_restore": "d" * 64,
        }), "utf-8")
        preflight = root / "preflight.json"
        preflight.write_text(json.dumps({
            "ok": True, "summary": {"pass": 20, "warn": 0, "fail": 0},
        }), "utf-8")
        readiness = root / "readiness.json"
        readiness.write_text(json.dumps({
            "ok": True, "version": "v2.9.0", "telegram_update_mode": "webhook",
            "telegram_receiver_ready": True, "webhook_configured": True,
            "lifecycle_worker_alive": True, "outbox_worker_alive": True,
            "telegram_inbox_worker_alive": True, "outbox_dead": 0,
            "telegram_inbox_dead": 0,
        }), "utf-8")
        return manifest, restore, preflight, readiness

    def attestation(self, source="a" * 40, digest="b" * 64):
        return json.dumps([{
            "attestation": {"bundle": "verified"},
            "verificationResult": {
                "signature": {"certificate": {"sourceRepositoryDigest": source}},
                "verifiedTimestamps": [{"type": "transparency-log"}],
                "statement": {
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "subject": [{
                        "name": "ghcr.io/voglogpro/bibitasks",
                        "digest": {"sha256": digest},
                    }],
                },
            },
        }]).encode("utf-8")

    def runner(self, raw=None, returncode=0):
        payload = self.attestation() if raw is None else raw

        def execute(command, **kwargs):
            self.assertEqual(command[:3], ["gh", "attestation", "verify"])
            self.assertIn("--source-digest", command)
            self.assertIn("--signer-workflow", command)
            self.assertTrue(kwargs["capture_output"])
            return SimpleNamespace(returncode=returncode, stdout=payload, stderr=b"")

        return execute

    def build(self, root):
        manifest, restore, preflight, readiness = self.fixtures(root)
        return build_release_record(
            commit="a" * 40,
            image="ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
            schema_version=290,
            backup_manifest=manifest,
            restore_report=restore,
            preflight_report=preflight,
            readiness_report=readiness,
            repository="voglogpro/-",
            signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
            approved_by="Скаут 1", second_approved_by="Скаут 2",
            attestation_runner=self.runner(),
        )

    def test_record_binds_all_promotion_evidence_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as root:
            record = self.build(root)
            self.assertEqual(record["schema_version"], 290)
            self.assertEqual(record["backup"]["id"], "backup-001")
            target = Path(root) / "release-record.json"
            write_record(target, record)
            with self.assertRaises(FileExistsError):
                write_record(target, record)

    def test_mismatched_restore_or_single_approver_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            manifest, restore, preflight, readiness = self.fixtures(root)
            data = json.loads(restore.read_text("utf-8"))
            data["source_manifest_sha256"] = "0" * 64
            restore.write_text(json.dumps(data), "utf-8")
            with self.assertRaisesRegex(ValueError, "restore rehearsal"):
                build_release_record(
                    commit="a" * 40,
                    image="ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
                    schema_version=290,
                    backup_manifest=manifest, restore_report=restore,
                    preflight_report=preflight, readiness_report=readiness,
                    repository="voglogpro/-",
                    signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                    approved_by="Скаут 1", second_approved_by="Скаут 2",
                    attestation_runner=self.runner(),
                )
            with self.assertRaisesRegex(ValueError, "distinct approvers"):
                build_release_record(
                    commit="a" * 40,
                    image="ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
                    schema_version=290,
                    backup_manifest=manifest, restore_report=restore,
                    preflight_report=preflight, readiness_report=readiness,
                    repository="voglogpro/-",
                    signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                    approved_by="Один", second_approved_by="Один",
                    attestation_runner=self.runner(),
                )

    def test_restore_digest_and_attested_source_are_required(self):
        with tempfile.TemporaryDirectory() as root:
            manifest, restore, preflight, readiness = self.fixtures(root)
            restore_data = json.loads(restore.read_text("utf-8"))
            restore_data["database_sha256_after_restore"] = "0" * 64
            restore.write_text(json.dumps(restore_data), "utf-8")
            with self.assertRaisesRegex(ValueError, "restore rehearsal"):
                build_release_record(
                    commit="a" * 40,
                    image="ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
                    schema_version=290, backup_manifest=manifest,
                    restore_report=restore, preflight_report=preflight,
                    readiness_report=readiness, repository="voglogpro/-",
                    signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                    approved_by="Скаут 1", second_approved_by="Скаут 2",
                    attestation_runner=self.runner(),
                )

        with tempfile.TemporaryDirectory() as root:
            manifest, restore, preflight, readiness = self.fixtures(root)
            with self.assertRaisesRegex(ValueError, "attestation"):
                build_release_record(
                    commit="a" * 40,
                    image="ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
                    schema_version=290, backup_manifest=manifest,
                    restore_report=restore, preflight_report=preflight,
                    readiness_report=readiness, repository="voglogpro/-",
                    signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                    approved_by="Скаут 1", second_approved_by="Скаут 2",
                    attestation_runner=self.runner(self.attestation(source="c" * 40)),
                )

            with self.assertRaisesRegex(ValueError, "verification failed"):
                build_release_record(
                    commit="a" * 40,
                    image="ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
                    schema_version=290, backup_manifest=manifest,
                    restore_report=restore, preflight_report=preflight,
                    readiness_report=readiness, repository="voglogpro/-",
                    signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                    approved_by="Скаут 1", second_approved_by="Скаут 2",
                    attestation_runner=self.runner(returncode=1),
                )


if __name__ == "__main__":
    unittest.main()
