import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

from scripts import sqlite_volume_rollback as rollback
from scripts.recovery_key_canary import ensure_recovery_key_canary
from scripts.release_candidate import canonical_sha256
from scripts.release_record import LEGACY_BUILD_VERSION, LEGACY_COMMIT_SHA, LEGACY_IMAGE


CURRENT_COMMIT = "a" * 40
TARGET_COMMIT = LEGACY_COMMIT_SHA
CURRENT_IMAGE = "ghcr.io/voglogpro/bibitasks@sha256:" + "c" * 64
TARGET_IMAGE = LEGACY_IMAGE
CURRENT_VOLUME = "bibitasks_data"
TARGET_VOLUME = "bibitasks_rollback_test_001"


class FakeRunner:
    def __init__(
        self, manifest_hash, database_hash, canary_hash, *, fail_target_start=False,
        schema_version=293,
    ):
        self.commands = []
        self.head = CURRENT_COMMIT
        self.volumes = {CURRENT_VOLUME: {"Name": CURRENT_VOLUME, "Labels": {}}}
        self.manifest_hash = manifest_hash
        self.database_hash = database_hash
        self.canary_hash = canary_hash
        self.schema_version = schema_version
        self.fail_target_start = fail_target_start
        self.target_checked_out = False
        self.media_ok = True
        self.canary_ok = True

    @staticmethod
    def result(returncode=0, stdout="", stderr=""):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def command(self, args, *, cwd=None, timeout=120):
        args = [str(value) for value in args]
        self.commands.append((args, str(cwd) if cwd else None))
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return self.result(stdout=self.head + "\n")
        if args[:3] == ["git", "status", "--porcelain"]:
            return self.result(stdout="")
        if args[:3] == ["git", "rev-parse", "--verify"]:
            return self.result(stdout=TARGET_COMMIT + "\n")
        if args[:2] == ["git", "show"]:
            return self.result(stdout="${BIBITASKS_IMAGE} ${BIBITASKS_DATA_VOLUME}\n")
        if args[:3] == ["git", "checkout", "--detach"]:
            self.head = args[3]
            self.target_checked_out = self.head == TARGET_COMMIT
            return self.result(stdout=self.head + "\n")
        if args[:3] == ["docker", "volume", "inspect"]:
            value = self.volumes.get(args[3])
            return self.result(
                returncode=0 if value else 1,
                stdout=json.dumps([value]) if value else "",
                stderr="not found" if not value else "",
            )
        if args[:3] == ["docker", "volume", "create"]:
            name = args[-1]
            labels = {}
            for index, value in enumerate(args):
                if value == "--label":
                    key, item = args[index + 1].split("=", 1)
                    labels[key] = item
            self.volumes[name] = {"Name": name, "Labels": labels}
            return self.result(stdout=name + "\n")
        if args[:2] == ["docker", "pull"]:
            return self.result(stdout="pulled\n")
        if args[:3] == ["docker", "image", "inspect"]:
            image = args[3]
            revision = TARGET_COMMIT if image == TARGET_IMAGE else CURRENT_COMMIT
            return self.result(stdout=json.dumps([{
                "RepoDigests": [image],
                "Config": {"Labels": {"org.opencontainers.image.revision": revision}},
            }]))
        if args[:2] == ["docker", "run"] and "--entrypoint" in args:
            return self.result(stdout="1001\n" if args[-1] == "-u" else "1002\n")
        if args[:2] == ["docker", "run"]:
            if rollback.READ_REPORT_CODE in args:
                return self.result(stdout=json.dumps({
                    "integrity_check": "ok",
                    "schema_version": self.schema_version,
                    "source_manifest_sha256": self.manifest_hash,
                    "database_sha256_after_restore": self.database_hash,
                    "s3_versions_rewritten": 0,
                    "recovery_key_canary": {
                        "sha256": self.canary_hash, "ok": True,
                    },
                }))
            if rollback.FINAL_CHECK_CODE in args:
                return self.result(stdout=json.dumps({
                    "report": {
                        "integrity_check": "ok", "schema_version": self.schema_version,
                        "source_manifest_sha256": self.manifest_hash,
                        "database_sha256_after_restore": self.database_hash,
                        "s3_versions_rewritten": 0,
                        "recovery_key_canary": {
                            "sha256": self.canary_hash, "ok": True,
                        },
                    },
                    "database_sha256": self.database_hash,
                    "manifest_sha256": self.manifest_hash,
                    "local_media_ok": self.media_ok,
                    "local_media_count": 1,
                    "local_media_bytes": 12,
                    "canary_sha256": self.canary_hash,
                    "canary_ok": self.canary_ok,
                    "schema_version": self.schema_version,
                    "integrity_check": "ok",
                    "all_owned": True,
                    "all_readable": True,
                }))
            return self.result()
        if args[:2] == ["systemctl", "stop"]:
            return self.result()
        if args[:2] == ["systemctl", "start"]:
            if self.fail_target_start and self.target_checked_out:
                return self.result(returncode=1, stderr="target failed")
            return self.result()
        if args[:2] == ["systemctl", "is-active"]:
            return self.result(stdout="active\n")
        raise AssertionError(f"unexpected command: {args}")


class SQLiteVolumeRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.backup = self.root / "backup-001"
        self.backup.mkdir()
        if os.name != "nt":
            self.backup.chmod(0o700)
        canary = ensure_recovery_key_canary(
            self.backup, Fernet(Fernet.generate_key()), Fernet(Fernet.generate_key()),
            production=True,
        )
        self.canary_hash = hashlib.sha256(canary.read_bytes()).hexdigest()
        self.database_hash = "e" * 64
        media_content = b"photo-report"
        media_path = self.backup / "proof_photos" / "proof.jpg"
        media_path.parent.mkdir()
        media_path.write_bytes(media_content)
        manifest = {
            "database": {
                "path": "bibitasks.db", "bytes": 42,
                "sha256": self.database_hash,
                "integrity_check": "ok", "schema_version": 293,
                "telegram_ciphertext_count": 0,
                "telegram_active_null_count": 0,
                "withdrawal_ciphertext_count": 0,
                "withdrawal_active_null_count": 0,
            },
            "recovery_key_canary": {
                "path": "recovery-key-canaries.json",
                "bytes": canary.stat().st_size,
                "sha256": self.canary_hash,
            },
            "media": [{
                "path": "proof_photos/proof.jpg",
                "bytes": len(media_content),
                "sha256": hashlib.sha256(media_content).hexdigest(),
            }],
            "media_objects": [],
        }
        self.manifest_path = self.backup / "manifest.json"
        self.manifest_path.write_text(json.dumps(manifest), "utf-8")
        self.manifest_hash = hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()
        record = {
            "record_version": 2,
            "commit": TARGET_COMMIT,
            "image": TARGET_IMAGE,
            "schema_version": 293,
            "backup": {
                "id": self.backup.name,
                "manifest_sha256": self.manifest_hash,
                "database_sha256": self.database_hash,
            },
            "approvals": ["S1", "S2"],
            "readiness": {"version": LEGACY_BUILD_VERSION},
        }
        self.record = self.root / "release-record.json"
        self.record.write_text(json.dumps(record), "utf-8")
        self.record_hash = hashlib.sha256(self.record.read_bytes()).hexdigest()
        self.deploy = self.root / "deploy.env"
        self.deploy.write_text("\n".join([
            f"BIBITASKS_IMAGE={CURRENT_IMAGE}",
            f"BIBITASKS_RELEASE_COMMIT={CURRENT_COMMIT}",
            "BIBITASKS_ENV_FILE=/etc/bibitasks/bibitasks.env",
            "BIBITASKS_DOMAIN=tasks.example.com",
            "BACKUP_DIR=/mnt/backups",
            "BACKUP_SENTINEL=/mnt/backups/.sentinel",
            "BACKUP_SENTINEL_VALUE=test-sentinel",
            "BACKUP_EXPECTED_SOURCE=backup.example:/bibitasks",
            f"BIBITASKS_DATA_VOLUME={CURRENT_VOLUME}",
            "MONITOR_ALERT_BOT_TOKEN_FILE=/etc/bibitasks/monitor-bot-token",
            "MONITOR_HEALTH_TOKEN_FILE=/etc/bibitasks/monitor-health-token",
            "MONITOR_ALERT_CHAT_ID=-1001234567890",
            "",
        ]), "utf-8")
        self.deploy.chmod(0o600)
        self.plan = self.root / "plan.json"
        self.stage = self.root / "stage.json"
        self.lock = self.root / "rollback.lock"
        self.now = datetime(2026, 7, 28, 6, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    def make_plan(self, runner):
        return rollback.build_plan(
            deploy_env=self.deploy, repo=self.repo,
            release_record=self.record,
            release_record_sha256=self.record_hash,
            backup_dir=self.backup, target_volume=TARGET_VOLUME,
            output=self.plan, runner=runner, now=self.now, lock_file=self.lock,
        )

    def stage_hash(self):
        return hashlib.sha256(self.stage.read_bytes()).hexdigest()

    def verify_output(self, name="verify-report.json"):
        return self.root / name

    def test_plan_and_apply_restore_only_to_fresh_volume(self):
        runner = FakeRunner(self.manifest_hash, self.database_hash, self.canary_hash)
        plan = self.make_plan(runner)
        report = rollback.apply_plan(
            plan_file=self.plan, confirmation=plan["apply_confirmation"],
            stage_report=self.stage, runner=runner, now=self.now,
            lock_file=self.lock,
        )
        self.assertTrue(report["ready_for_point_in_time_recovery_review"])
        self.assertFalse(report["production_activation_enabled"])
        self.assertEqual(report["current_volume_preserved"], CURRENT_VOLUME)
        commands = [args for args, _ in runner.commands]
        self.assertFalse(any(args[:3] == ["docker", "volume", "rm"] for args in commands))
        restore = next(
            args for args in commands
            if args[:2] == ["docker", "run"] and "scripts/restore.py" in args
        )
        joined = " ".join(restore)
        self.assertIn(f"src={TARGET_VOLUME},dst=/target", joined)
        self.assertNotIn(f"src={CURRENT_VOLUME},dst=", joined)
        self.assertIn("--user 0:0", joined)
        final = next(args for args in commands if rollback.FINAL_CHECK_CODE in args)
        self.assertIn("--user 1001:1002", " ".join(final))
        promotion = next(args for args in commands if rollback.PROMOTE_CODE in args)
        self.assertIn("--cap-drop ALL", " ".join(promotion))
        self.assertIn("--cap-add CHOWN", " ".join(promotion))
        self.assertNotIn("DAC_OVERRIDE", " ".join(promotion))
        self.assertNotIn("FOWNER", " ".join(promotion))

    def test_candidate_subjects_propagate_through_plan_stage_and_verify(self):
        manifest = json.loads(self.manifest_path.read_text("utf-8"))
        manifest["database"]["schema_version"] = 295
        self.manifest_path.write_text(json.dumps(manifest), "utf-8")
        self.manifest_hash = hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()
        software = {
            "commit": TARGET_COMMIT, "image": TARGET_IMAGE,
            "schema_version": 295, "application_version": "v2.10.0",
        }
        backup = {
            "id": self.backup.name, "manifest_sha256": self.manifest_hash,
            "database": {
                "sha256": self.database_hash, "bytes": 42,
                "telegram_ciphertext_count": 0, "telegram_active_null_count": 0,
                "withdrawal_ciphertext_count": 0, "withdrawal_active_null_count": 0,
            },
            "recovery_key_canary": {
                "sha256": self.canary_hash,
                "bytes": (self.backup / "recovery-key-canaries.json").stat().st_size,
            },
            "local_media": {"count": 1, "bytes": 12},
        }
        software_hash = canonical_sha256(software)
        candidate = {
            "candidate_version": 1, **software,
            "deployment": {
                "telegram_bot_id": 123456, "telegram_group_id": -1001234567890,
                "miniapp_origin": "https://tasks.example.com",
                "health_origin": "https://health.example.com",
            },
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
                "deployment": {
                    "telegram_bot_id": 123456, "telegram_group_id": -1001234567890,
                    "miniapp_origin": "https://tasks.example.com",
                    "health_origin": "https://health.example.com",
                }, "backup": backup,
            }),
            "deployment_authorized": False,
        }
        self.record.write_text(json.dumps(candidate), "utf-8")
        self.record_hash = hashlib.sha256(self.record.read_bytes()).hexdigest()
        runner = FakeRunner(
            self.manifest_hash, self.database_hash, self.canary_hash,
            schema_version=295,
        )
        plan = self.make_plan(runner)
        self.assertEqual(plan["target"]["candidate_sha256"], self.record_hash)
        self.assertEqual(plan["target"]["software_subject_sha256"], software_hash)
        rollback.apply_plan(
            plan_file=self.plan, confirmation=plan["apply_confirmation"],
            stage_report=self.stage, runner=runner, now=self.now,
            lock_file=self.lock,
        )
        stage = json.loads(self.stage.read_text("utf-8"))
        self.assertEqual(stage["promotion_subject_sha256"], candidate["promotion_subject_sha256"])
        verified = rollback.verify_stage(
            plan_file=self.plan, stage_report=self.stage,
            stage_report_sha256=self.stage_hash(), output=self.verify_output(),
            runner=runner, now=self.now, lock_file=self.lock,
        )
        self.assertEqual(verified["candidate_sha256"], self.record_hash)
        self.assertEqual(verified["target"]["software_subject_sha256"], software_hash)

    def test_v2_legacy_reader_is_restricted_to_v291(self):
        record = json.loads(self.record.read_text("utf-8"))
        record["readiness"]["version"] = "v2.9.0"
        self.record.write_text(json.dumps(record), "utf-8")
        self.record_hash = hashlib.sha256(self.record.read_bytes()).hexdigest()
        runner = FakeRunner(self.manifest_hash, self.database_hash, self.canary_hash)
        with self.assertRaisesRegex(ValueError, "restricted"):
            self.make_plan(runner)

    def test_v2_legacy_reader_requires_exact_verified_subject(self):
        record = json.loads(self.record.read_text("utf-8"))
        record["commit"] = "b" * 40
        self.record.write_text(json.dumps(record), "utf-8")
        self.record_hash = hashlib.sha256(self.record.read_bytes()).hexdigest()
        runner = FakeRunner(self.manifest_hash, self.database_hash, self.canary_hash)
        with self.assertRaisesRegex(ValueError, "exact verified v2.9.1"):
            self.make_plan(runner)

    def test_wrong_confirmation_and_modified_record_fail_before_volume_create(self):
        runner = FakeRunner(self.manifest_hash, self.database_hash, self.canary_hash)
        self.make_plan(runner)
        with self.assertRaisesRegex(ValueError, "exact apply confirmation"):
            rollback.apply_plan(
                plan_file=self.plan, confirmation="yes",
                stage_report=self.stage, runner=runner, now=self.now,
                lock_file=self.lock,
            )
        record = json.loads(self.record.read_text("utf-8"))
        record["commit"] = "f" * 40
        self.record.write_text(json.dumps(record), "utf-8")
        plan = json.loads(self.plan.read_text("utf-8"))
        with self.assertRaisesRegex(ValueError, "record changed"):
            rollback.apply_plan(
                plan_file=self.plan, confirmation=plan["apply_confirmation"],
                stage_report=self.stage, runner=runner, now=self.now,
                lock_file=self.lock,
            )
        creates = [
            args for args, _ in runner.commands
            if args[:3] == ["docker", "volume", "create"]
        ]
        self.assertEqual(creates, [])

    def test_verify_rechecks_live_volume_and_production_activation_is_disabled(self):
        runner = FakeRunner(self.manifest_hash, self.database_hash, self.canary_hash)
        plan = self.make_plan(runner)
        rollback.apply_plan(
            plan_file=self.plan, confirmation=plan["apply_confirmation"],
            stage_report=self.stage, runner=runner, now=self.now,
            lock_file=self.lock,
        )
        result = rollback.verify_stage(
            plan_file=self.plan, stage_report=self.stage,
            stage_report_sha256=self.stage_hash(),
            output=self.verify_output(),
            runner=runner, now=self.now, lock_file=self.lock,
        )
        self.assertFalse(result["production_activation_enabled"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["report_version"], 1)
        self.assertTrue(result["current"]["present"])
        self.assertNotIn(str(self.root), json.dumps(result))
        final_checks = [args for args, _ in runner.commands if rollback.FINAL_CHECK_CODE in args]
        self.assertEqual(len(final_checks), 2)
        with self.assertRaisesRegex(RuntimeError, "production activation is disabled"):
            rollback.activate_plan(
                plan_file=self.plan, stage_report=self.stage,
                stage_report_sha256=self.stage_hash(), runner=runner,
                now=self.now, lock_file=self.lock,
            )
        self.assertEqual(runner.head, CURRENT_COMMIT)
        deployed = self.deploy.read_text("utf-8")
        self.assertIn(f"BIBITASKS_RELEASE_COMMIT={CURRENT_COMMIT}", deployed)
        self.assertFalse(any(args[0] == "systemctl" for args, _ in runner.commands))

    def test_verify_detects_deleted_and_recreated_staged_volume(self):
        runner = FakeRunner(self.manifest_hash, self.database_hash, self.canary_hash)
        plan = self.make_plan(runner)
        rollback.apply_plan(
            plan_file=self.plan, confirmation=plan["apply_confirmation"],
            stage_report=self.stage, runner=runner, now=self.now,
            lock_file=self.lock,
        )
        runner.volumes[TARGET_VOLUME]["Labels"]["unexpected"] = "tampered"
        with self.assertRaisesRegex(ValueError, "labels differ"):
            rollback.verify_stage(
                plan_file=self.plan, stage_report=self.stage,
                stage_report_sha256=self.stage_hash(), runner=runner,
                output=self.verify_output(),
                now=self.now, lock_file=self.lock,
            )
        runner.volumes[TARGET_VOLUME]["Labels"].pop("unexpected")
        runner.volumes[TARGET_VOLUME]["CreatedAt"] = "recreated-later"
        with self.assertRaisesRegex(ValueError, "deleted, recreated"):
            rollback.verify_stage(
                plan_file=self.plan, stage_report=self.stage,
                stage_report_sha256=self.stage_hash(), runner=runner,
                output=self.verify_output(),
                now=self.now, lock_file=self.lock,
            )

    def test_verify_detects_local_media_tamper(self):
        runner = FakeRunner(self.manifest_hash, self.database_hash, self.canary_hash)
        plan = self.make_plan(runner)
        rollback.apply_plan(
            plan_file=self.plan, confirmation=plan["apply_confirmation"],
            stage_report=self.stage, runner=runner, now=self.now,
            lock_file=self.lock,
        )
        runner.media_ok = False
        with self.assertRaisesRegex(ValueError, "final volume validation failed"):
            rollback.verify_stage(
                plan_file=self.plan, stage_report=self.stage,
                stage_report_sha256=self.stage_hash(), runner=runner,
                output=self.verify_output(),
                now=self.now, lock_file=self.lock,
            )

    def test_verify_detects_recovery_key_canary_tamper(self):
        runner = FakeRunner(self.manifest_hash, self.database_hash, self.canary_hash)
        plan = self.make_plan(runner)
        rollback.apply_plan(
            plan_file=self.plan, confirmation=plan["apply_confirmation"],
            stage_report=self.stage, runner=runner, now=self.now,
            lock_file=self.lock,
        )
        runner.canary_ok = False
        with self.assertRaisesRegex(ValueError, "final volume validation failed"):
            rollback.verify_stage(
                plan_file=self.plan, stage_report=self.stage,
                stage_report_sha256=self.stage_hash(), runner=runner,
                output=self.verify_output(), now=self.now, lock_file=self.lock,
            )

    def test_expiry_blocks_apply_but_not_read_only_verify(self):
        from datetime import timedelta

        runner = FakeRunner(self.manifest_hash, self.database_hash, self.canary_hash)
        plan = self.make_plan(runner)
        late = self.now + timedelta(minutes=31)
        with self.assertRaisesRegex(ValueError, "plan has expired"):
            rollback.apply_plan(
                plan_file=self.plan, confirmation=plan["apply_confirmation"],
                stage_report=self.stage, runner=runner, now=late,
                lock_file=self.lock,
            )
        rollback.apply_plan(
            plan_file=self.plan, confirmation=plan["apply_confirmation"],
            stage_report=self.stage, runner=runner, now=self.now,
            lock_file=self.lock,
        )
        result = rollback.verify_stage(
            plan_file=self.plan, stage_report=self.stage,
            stage_report_sha256=self.stage_hash(), runner=runner,
            output=self.verify_output(),
            now=late, lock_file=self.lock,
        )
        self.assertFalse(result["production_activation_enabled"])

    def test_verify_report_never_overwrites_or_writes_inside_repo(self):
        runner = FakeRunner(self.manifest_hash, self.database_hash, self.canary_hash)
        plan = self.make_plan(runner)
        rollback.apply_plan(
            plan_file=self.plan, confirmation=plan["apply_confirmation"],
            stage_report=self.stage, runner=runner, now=self.now,
            lock_file=self.lock,
        )
        output = self.verify_output()
        rollback.verify_stage(
            plan_file=self.plan, stage_report=self.stage,
            stage_report_sha256=self.stage_hash(), output=output,
            runner=runner, now=self.now, lock_file=self.lock,
        )
        with self.assertRaisesRegex(FileExistsError, "verify report target already exists"):
            rollback.verify_stage(
                plan_file=self.plan, stage_report=self.stage,
                stage_report_sha256=self.stage_hash(), output=output,
                runner=runner, now=self.now, lock_file=self.lock,
            )
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            rollback.verify_stage(
                plan_file=self.plan, stage_report=self.stage,
                stage_report_sha256=self.stage_hash(),
                output=self.repo / "verify.json", runner=runner,
                now=self.now, lock_file=self.lock,
            )

    def test_verify_report_hash_mismatch_writes_nothing(self):
        runner = FakeRunner(self.manifest_hash, self.database_hash, self.canary_hash)
        plan = self.make_plan(runner)
        rollback.apply_plan(
            plan_file=self.plan, confirmation=plan["apply_confirmation"],
            stage_report=self.stage, runner=runner, now=self.now,
            lock_file=self.lock,
        )
        output = self.verify_output()
        with self.assertRaisesRegex(ValueError, "digest differs"):
            rollback.verify_stage(
                plan_file=self.plan, stage_report=self.stage,
                stage_report_sha256="0" * 64, output=output,
                runner=runner, now=self.now, lock_file=self.lock,
            )
        self.assertFalse(output.exists())

    def test_host_lock_is_process_wide_and_service_name_is_fail_closed(self):
        with rollback.HostLock(self.lock):
            with self.assertRaisesRegex(RuntimeError, "holds the host lock"):
                with rollback.HostLock(self.lock):
                    pass
        self.assertIsNotNone(rollback.SERVICE_RE.fullmatch("bibitasks-pilot.service"))
        self.assertIsNone(rollback.SERVICE_RE.fullmatch("-evil.service"))

    def test_existing_stage_report_fails_before_volume_create(self):
        runner = FakeRunner(self.manifest_hash, self.database_hash, self.canary_hash)
        plan = self.make_plan(runner)
        self.stage.write_text("do not overwrite", "utf-8")
        with self.assertRaisesRegex(FileExistsError, "stage report target already exists"):
            rollback.apply_plan(
                plan_file=self.plan, confirmation=plan["apply_confirmation"],
                stage_report=self.stage, runner=runner, now=self.now,
                lock_file=self.lock,
            )
        creates = [
            args for args, _ in runner.commands
            if args[:3] == ["docker", "volume", "create"]
        ]
        self.assertEqual(creates, [])

    def test_report_mismatch_quarantines_new_volume_without_switch(self):
        runner = FakeRunner(self.manifest_hash, "0" * 64, self.canary_hash)
        plan = self.make_plan(runner)
        with self.assertRaisesRegex(ValueError, "report differs"):
            rollback.apply_plan(
                plan_file=self.plan, confirmation=plan["apply_confirmation"],
                stage_report=self.stage, runner=runner, now=self.now,
                lock_file=self.lock,
            )
        self.assertIn(TARGET_VOLUME, runner.volumes)
        self.assertIn(f"BIBITASKS_DATA_VOLUME={CURRENT_VOLUME}", self.deploy.read_text("utf-8"))
        self.assertFalse(any(args[0] == "systemctl" for args, _ in runner.commands))


if __name__ == "__main__":
    unittest.main()
