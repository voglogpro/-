import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.release_record import (
    LEGACY_BUILD_VERSION,
    LEGACY_COMMIT_SHA,
    LEGACY_IMAGE,
    build_release_record,
    write_record,
)


class ReleaseRecordTests(unittest.TestCase):
    def fixtures(self, root):
        root = Path(root)
        backup = root / "backup-001"
        backup.mkdir()
        manifest = backup / "manifest.json"
        manifest.write_text(json.dumps({
            "database": {
                "integrity_check": "ok", "schema_version": 293,
                "sha256": "d" * 64,
            },
        }), "utf-8")
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        restore = root / "restore.json"
        restore.write_text(json.dumps({
            "integrity_check": "ok", "schema_version": 293,
            "source_manifest_sha256": digest,
            "database_sha256_after_restore": "d" * 64,
        }), "utf-8")
        preflight = root / "preflight.json"
        preflight.write_text(json.dumps({
            "ok": True, "summary": {"pass": 20, "warn": 0, "fail": 0},
        }), "utf-8")
        readiness = root / "readiness.json"
        readiness.write_text(json.dumps({
            "ok": True, "version": LEGACY_BUILD_VERSION, "telegram_update_mode": "webhook",
            "telegram_receiver_ready": True, "webhook_configured": True,
            "lifecycle_worker_alive": True, "outbox_worker_alive": True,
            "telegram_inbox_worker_alive": True, "outbox_dead": 0,
            "telegram_inbox_dead": 0,
        }), "utf-8")
        return manifest, restore, preflight, readiness

    def attestation(self, source=None, digest=None):
        source = source or LEGACY_COMMIT_SHA
        digest = digest or LEGACY_IMAGE.rsplit("@sha256:", 1)[1]
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
            commit=LEGACY_COMMIT_SHA,
            image=LEGACY_IMAGE,
            schema_version=293,
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
            self.assertEqual(record["schema_version"], 293)
            self.assertEqual(record["backup"]["id"], "backup-001")
            target = Path(root) / "release-record.json"
            write_record(target, record)
            with self.assertRaises(FileExistsError):
                write_record(target, record)

    def test_legacy_builder_rejects_any_other_release_subject(self):
        with tempfile.TemporaryDirectory() as root:
            manifest, restore, preflight, readiness = self.fixtures(root)
            common = dict(
                schema_version=293, backup_manifest=manifest,
                restore_report=restore, preflight_report=preflight,
                readiness_report=readiness, repository="voglogpro/-",
                signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                approved_by="Скаут 1", second_approved_by="Скаут 2",
                attestation_runner=self.runner(),
            )
            with self.assertRaisesRegex(ValueError, "exact verified v2.9.1"):
                build_release_record(
                    commit="a" * 40, image=LEGACY_IMAGE, **common,
                )
            with self.assertRaisesRegex(ValueError, "exact verified v2.9.1"):
                build_release_record(
                    commit=LEGACY_COMMIT_SHA,
                    image="ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64,
                    **common,
                )

    def test_mismatched_restore_or_single_approver_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            manifest, restore, preflight, readiness = self.fixtures(root)
            data = json.loads(restore.read_text("utf-8"))
            data["source_manifest_sha256"] = "0" * 64
            restore.write_text(json.dumps(data), "utf-8")
            with self.assertRaisesRegex(ValueError, "restore rehearsal"):
                build_release_record(
                    commit=LEGACY_COMMIT_SHA,
                    image=LEGACY_IMAGE,
                    schema_version=293,
                    backup_manifest=manifest, restore_report=restore,
                    preflight_report=preflight, readiness_report=readiness,
                    repository="voglogpro/-",
                    signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                    approved_by="Скаут 1", second_approved_by="Скаут 2",
                    attestation_runner=self.runner(),
                )
            with self.assertRaisesRegex(ValueError, "distinct approvers"):
                build_release_record(
                    commit=LEGACY_COMMIT_SHA,
                    image=LEGACY_IMAGE,
                    schema_version=293,
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
                    commit=LEGACY_COMMIT_SHA,
                    image=LEGACY_IMAGE,
                    schema_version=293, backup_manifest=manifest,
                    restore_report=restore, preflight_report=preflight,
                    readiness_report=readiness, repository="voglogpro/-",
                    signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                    approved_by="Скаут 1", second_approved_by="Скаут 2",
                    attestation_runner=self.runner(),
                )

    def test_untrusted_evidence_fields_are_validated_and_sanitized(self):
        with tempfile.TemporaryDirectory() as root:
            manifest, restore, preflight, readiness = self.fixtures(root)
            data = json.loads(preflight.read_text("utf-8"))
            data["summary"]["token"] = "123456:" + "a" * 40
            preflight.write_text(json.dumps(data), "utf-8")
            record = build_release_record(
                commit=LEGACY_COMMIT_SHA,
                image=LEGACY_IMAGE,
                schema_version=293, backup_manifest=manifest,
                restore_report=restore, preflight_report=preflight,
                readiness_report=readiness, repository="voglogpro/-",
                signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                approved_by="Scout 1", second_approved_by="Scout 2",
                attestation_runner=self.runner(),
            )
            self.assertEqual(
                set(record["telegram_preflight"]["summary"]),
                {"pass", "warn", "fail"},
            )
            self.assertNotIn("123456:", json.dumps(record))

            ready_data = json.loads(readiness.read_text("utf-8"))
            ready_data["version"] = "v2.9.1\nBOT_TOKEN=secret"
            readiness.write_text(json.dumps(ready_data), "utf-8")
            with self.assertRaisesRegex(ValueError, "version"):
                build_release_record(
                    commit=LEGACY_COMMIT_SHA,
                    image=LEGACY_IMAGE,
                    schema_version=293, backup_manifest=manifest,
                    restore_report=restore, preflight_report=preflight,
                    readiness_report=readiness, repository="voglogpro/-",
                    signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                    approved_by="Scout 1", second_approved_by="Scout 2",
                    attestation_runner=self.runner(),
                )

        with self.assertRaisesRegex(ValueError, "inside the repository"):
            write_record(
                Path(__file__).resolve().parents[1] / "release-record-forbidden.json",
                {"safe": True},
            )

        with tempfile.TemporaryDirectory() as root:
            manifest, restore, preflight, readiness = self.fixtures(root)
            with self.assertRaisesRegex(ValueError, "attestation"):
                build_release_record(
                    commit=LEGACY_COMMIT_SHA,
                    image=LEGACY_IMAGE,
                    schema_version=293, backup_manifest=manifest,
                    restore_report=restore, preflight_report=preflight,
                    readiness_report=readiness, repository="voglogpro/-",
                    signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                    approved_by="Скаут 1", second_approved_by="Скаут 2",
                    attestation_runner=self.runner(self.attestation(source="c" * 40)),
                )

            with self.assertRaisesRegex(ValueError, "verification failed"):
                build_release_record(
                    commit=LEGACY_COMMIT_SHA,
                    image=LEGACY_IMAGE,
                    schema_version=293, backup_manifest=manifest,
                    restore_report=restore, preflight_report=preflight,
                    readiness_report=readiness, repository="voglogpro/-",
                    signer_workflow="github.com/voglogpro/-/.github/workflows/release.yml",
                    approved_by="Скаут 1", second_approved_by="Скаут 2",
                    attestation_runner=self.runner(returncode=1),
                )


if __name__ == "__main__":
    unittest.main()
