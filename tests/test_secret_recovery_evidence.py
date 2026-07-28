import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from types import SimpleNamespace
import unittest

from cryptography.fernet import Fernet

from scripts.secret_recovery_evidence import (
    SANITIZED_AGE_ENV,
    TRUSTED_AGE,
    build_report,
    write_report,
)


COMMIT = "a" * 40
IMAGE = "ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64
SCHEMA_VERSION = 295
RELEASE_VERSION = "v2.10.0"
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SecretRecoveryEvidenceTests(unittest.TestCase):
    def fixtures(self, root, *, telegram_rows=1, withdrawal_rows=1):
        root = Path(root)
        inbox_text = Fernet.generate_key()
        withdrawal_text = Fernet.generate_key()
        inbox = Fernet(inbox_text)
        withdrawal = Fernet(withdrawal_text)
        recovered_env = (
            b"BOT_TOKEN=must-not-enter-report\n"
            + b"TELEGRAM_INBOX_KEY=" + inbox_text + b"\n"
            + b"WITHDRAW_ACCOUNT_KEY=" + withdrawal_text + b"\n"
        )

        bundles = [root / "copy-a.age", root / "copy-b.age"]
        bundle_bytes = b"synthetic-age-ciphertext-must-not-enter-report" * 4
        for path in bundles:
            path.write_bytes(bundle_bytes)
        identity = root / "identity.agekey"
        identity.write_text("AGE-SECRET-KEY-synthetic-identity", "ascii")
        if os.name != "nt":
            identity.chmod(0o600)

        database = root / "bibitasks.db"
        with closing(sqlite3.connect(database)) as db:
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.execute(
                "CREATE TABLE telegram_update_inbox ("
                "update_id INTEGER PRIMARY KEY,payload_json TEXT,payload_sha256 TEXT,"
                "status TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE withdrawal_requests ("
                "id INTEGER PRIMARY KEY,account_ciphertext TEXT,"
                "account_fingerprint TEXT,status TEXT NOT NULL)"
            )
            for number in range(telegram_rows):
                payload = {"update_id": number + 1, "message": {"text": "safe fixture"}}
                canonical = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
                fingerprint = "h1:" + hmac.new(
                    inbox_text, canonical.encode("utf-8"), hashlib.sha256,
                ).hexdigest()
                db.execute(
                    "INSERT INTO telegram_update_inbox VALUES (?,?,?,'done')",
                    (
                        number + 1,
                        inbox.encrypt(canonical.encode("utf-8")).decode("ascii"),
                        fingerprint,
                    ),
                )
            for number in range(withdrawal_rows):
                account = f"account-{number + 1}"
                fingerprint = hmac.new(
                    withdrawal_text, account.casefold().encode("utf-8"), hashlib.sha256,
                ).hexdigest()
                db.execute(
                    "INSERT INTO withdrawal_requests VALUES (?,?,?,'completed')",
                    (
                        number + 1,
                        withdrawal.encrypt(account.encode("utf-8")).decode("ascii"),
                        fingerprint,
                    ),
                )
            db.commit()

        nonce = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
        common = {
            "domain": "bibitasks.recovery-key-canary",
            "nonce": nonce,
            "purpose": "pre-disaster-key-binding",
            "version": 1,
        }

        def canary_plaintext(role):
            return json.dumps(
                {**common, "role": role}, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")

        canary_doc = {
            **common,
            "telegram_inbox_ciphertext": inbox.encrypt(
                canary_plaintext("telegram-inbox")
            ).decode("ascii"),
            "withdraw_account_ciphertext": withdrawal.encrypt(
                canary_plaintext("withdraw-account")
            ).decode("ascii"),
        }
        canary = root / "recovery-key-canaries.json"
        canary.write_bytes((json.dumps(
            canary_doc, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ) + "\n").encode("ascii"))

        preflight = root / "preflight.json"
        preflight.write_text(json.dumps({
            "report_version": 1, "generated_at": NOW.isoformat(), "ok": True,
            "summary": {"pass": 20, "warn": 0, "fail": 0},
            "checks": [
                {"name": f"check-{index}", "status": "pass", "detail": "ok"}
                for index in range(20)
            ],
        }), "utf-8")
        readiness = root / "readiness.json"
        readiness.write_text(json.dumps({
            "report_version": 1, "generated_at": NOW.isoformat(), "ok": True,
            "version": "2026-07-28 · БибиЗадачи v2.10.0 (release gate)",
            "application_version": RELEASE_VERSION,
            "telegram_update_mode": "webhook",
            "telegram_receiver_ready": True, "webhook_configured": True,
            "lifecycle_worker_alive": True, "outbox_worker_alive": True,
            "telegram_inbox_worker_alive": True,
            "withdrawal_encryption_ready": True,
            "telegram_inbox_encryption_ready": True,
            "outbox_dead": 0, "telegram_inbox_dead": 0,
        }), "utf-8")
        healthy = {"last_healthy": True, "alert_active": False}
        drilled = {
            **healthy,
            "last_incident_delivered_at": (NOW - timedelta(minutes=10)).isoformat(),
            "last_recovery_delivered_at": (NOW - timedelta(minutes=5)).isoformat(),
        }
        monitor = root / "monitor.json"
        monitor.write_text(json.dumps({
            "schema_version": 1, "generated_at": NOW.isoformat(), "ok": True,
            "heartbeat_ok": True, "alert_delivery_ok": True,
            "checks": {
                "application": drilled, "backup": drilled,
                "dead_queues": healthy,
            },
        }), "utf-8")
        manifest = root / "manifest.json"
        restore = root / "restore-report.json"
        fixture = {
            "root": root, "inbox_text": inbox_text,
            "withdrawal_text": withdrawal_text, "recovered_env": recovered_env,
            "bundles": bundles, "bundle_bytes": bundle_bytes,
            "identity": identity, "database": database, "canary": canary,
            "manifest": manifest, "restore": restore, "preflight": preflight,
            "readiness": readiness, "monitor": monitor,
        }
        self.refresh_chain(fixture)
        return fixture

    def database_counts(self, database):
        with closing(sqlite3.connect(database)) as db:
            return {
                "telegram_ciphertext_count": db.execute(
                    "SELECT COUNT(*) FROM telegram_update_inbox "
                    "WHERE payload_json IS NOT NULL"
                ).fetchone()[0],
                "telegram_active_null_count": db.execute(
                    "SELECT COUNT(*) FROM telegram_update_inbox "
                    "WHERE status IN ('pending','processing') AND payload_json IS NULL"
                ).fetchone()[0],
                "withdrawal_ciphertext_count": db.execute(
                    "SELECT COUNT(*) FROM withdrawal_requests "
                    "WHERE account_ciphertext IS NOT NULL"
                ).fetchone()[0],
                "withdrawal_active_null_count": db.execute(
                    "SELECT COUNT(*) FROM withdrawal_requests "
                    "WHERE status IN ('pending','processing') AND account_ciphertext IS NULL"
                ).fetchone()[0],
            }

    def refresh_chain(self, fixture, *, count_overrides=None):
        counts = self.database_counts(fixture["database"])
        counts.update(count_overrides or {})
        database_sha = digest(fixture["database"])
        canary_raw = fixture["canary"].read_bytes()
        canary_sha = hashlib.sha256(canary_raw).hexdigest()
        manifest = {
            "created_at": (NOW - timedelta(minutes=20)).isoformat(),
            "database": {
                "path": "bibitasks.db", "bytes": fixture["database"].stat().st_size,
                "sha256": database_sha, "integrity_check": "ok",
                "schema_version": SCHEMA_VERSION, **counts,
            },
            "recovery_key_canary": {
                "path": "recovery-key-canaries.json", "bytes": len(canary_raw),
                "sha256": canary_sha,
            },
        }
        fixture["manifest"].write_text(json.dumps(manifest), "utf-8")
        fixture["restore"].write_text(json.dumps({
            "restored_at": (NOW - timedelta(minutes=15)).isoformat(),
            "source_manifest_sha256": digest(fixture["manifest"]),
            "database_sha256_after_restore": database_sha,
            "schema_version": SCHEMA_VERSION, "integrity_check": "ok",
            "s3_versions_rewritten": 0,
            "recovery_key_canary": {"sha256": canary_sha, "ok": True},
        }), "utf-8")

    def runner(self, fixture, *, returncode=0, stdout=None, stderr=b""):
        expected_stdout = fixture["recovered_env"] if stdout is None else stdout

        def execute(command, **kwargs):
            self.assertEqual(command[0:3], [str(TRUSTED_AGE), "--decrypt", "--identity"])
            self.assertEqual(kwargs["env"], SANITIZED_AGE_ENV)
            self.assertEqual(kwargs["timeout"], 30)
            self.assertEqual(len(kwargs["pass_fds"]), 2)
            self.assertNotIn(fixture["identity"].read_text("ascii"), " ".join(command))
            return SimpleNamespace(
                returncode=returncode, stdout=expected_stdout, stderr=stderr,
            )

        return execute

    def build(self, fixture, **overrides):
        arguments = {
            "encrypted_recovery_bundles": fixture["bundles"],
            "age_identity_file": fixture["identity"],
            "database": fixture["database"],
            "recovery_key_canaries": fixture["canary"],
            "backup_manifest": fixture["manifest"],
            "restore_report": fixture["restore"],
            "commit": COMMIT, "image": IMAGE, "schema_version": SCHEMA_VERSION,
            "release_version": RELEASE_VERSION,
            "preflight_report": fixture["preflight"],
            "readiness_report": fixture["readiness"],
            "monitor_canary_report": fixture["monitor"], "now": NOW,
            "age_runner": self.runner(fixture), "age_validator": lambda: None,
        }
        arguments.update(overrides)
        return build_report(**arguments)

    def test_happy_path_binds_two_bundles_canary_backup_restore_rows_and_live(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root, telegram_rows=2, withdrawal_rows=3)
            report = self.build(fixture)
            self.assertTrue(report["ok"])
            self.assertEqual(
                report["encrypted_recovery_bundle"]["copy_count_verified"], 2,
            )
            self.assertEqual(report["database"]["telegram_ciphertext_verified"], 2)
            self.assertEqual(report["database"]["withdrawal_ciphertext_verified"], 3)
            self.assertTrue(report["keys"]["pre_disaster_canary_verified"])
            self.assertTrue(report["backup"]["expected_counts_present"])
            self.assertTrue(report["restore"]["canary_binding_verified"])
            serialized = json.dumps(report, ensure_ascii=False)
            for secret in (
                fixture["inbox_text"].decode(), fixture["withdrawal_text"].decode(),
                fixture["identity"].read_text("ascii"),
                fixture["bundle_bytes"].decode(), "must-not-enter-report",
                str(fixture["database"]), str(fixture["identity"]),
            ):
                self.assertNotIn(secret, serialized)

    def test_synthetic_age_failure_is_generic_and_stops(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root)
            failing = self.runner(
                fixture, returncode=1, stdout=b"", stderr=b"identity secret leaked",
            )
            with self.assertRaisesRegex(ValueError, "trusted age decryption failed") as error:
                self.build(fixture, age_runner=failing)
            self.assertNotIn("identity secret leaked", str(error.exception))

    def test_two_bundle_mismatch_or_same_file_stops(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root)
            fixture["bundles"][1].write_bytes(b"different encrypted bundle")
            with self.assertRaisesRegex(ValueError, "not byte-identical"):
                self.build(fixture)
            with self.assertRaisesRegex(ValueError, "distinct files"):
                self.build(
                    fixture,
                    encrypted_recovery_bundles=[fixture["bundles"][0]] * 2,
                )

    def test_random_keys_fail_canary_even_when_database_is_empty(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root, telegram_rows=0, withdrawal_rows=0)
            random_env = (
                b"TELEGRAM_INBOX_KEY=" + Fernet.generate_key() + b"\n"
                b"WITHDRAW_ACCOUNT_KEY=" + Fernet.generate_key() + b"\n"
            )
            with self.assertRaisesRegex(ValueError, "pre-disaster"):
                self.build(fixture, age_runner=self.runner(fixture, stdout=random_env))

    def test_active_null_telegram_or_withdrawal_stops(self):
        for table, columns in (
            ("telegram_update_inbox", "(99,NULL,'h1:dead','pending')"),
            ("withdrawal_requests", "(99,NULL,'dead','processing')"),
        ):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as root:
                fixture = self.fixtures(root, telegram_rows=0, withdrawal_rows=0)
                with closing(sqlite3.connect(fixture["database"])) as db:
                    db.execute(f"INSERT INTO {table} VALUES {columns}")
                    db.commit()
                self.refresh_chain(fixture)
                with self.assertRaisesRegex(ValueError, "active NULL"):
                    self.build(fixture)

    def test_row_binding_and_fingerprint_mismatches_stop(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root)
            with closing(sqlite3.connect(fixture["database"])) as db:
                db.execute(
                    "UPDATE telegram_update_inbox SET payload_sha256='h1:bad'"
                )
                db.commit()
            self.refresh_chain(fixture)
            with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                self.build(fixture)

        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root)
            wrong_payload = {"update_id": 2, "message": {"text": "wrong row"}}
            canonical = json.dumps(
                wrong_payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            ciphertext = Fernet(fixture["inbox_text"]).encrypt(
                canonical.encode("utf-8")
            ).decode("ascii")
            fingerprint = "h1:" + hmac.new(
                fixture["inbox_text"], canonical.encode("utf-8"), hashlib.sha256,
            ).hexdigest()
            with closing(sqlite3.connect(fixture["database"])) as db:
                db.execute(
                    "UPDATE telegram_update_inbox SET payload_json=?,payload_sha256=? "
                    "WHERE update_id=1", (ciphertext, fingerprint),
                )
                db.commit()
            self.refresh_chain(fixture)
            with self.assertRaisesRegex(ValueError, "row binding failed"):
                self.build(fixture)

        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root)
            with closing(sqlite3.connect(fixture["database"])) as db:
                db.execute(
                    "UPDATE withdrawal_requests SET account_fingerprint='bad'"
                )
                db.commit()
            self.refresh_chain(fixture)
            with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                self.build(fixture)

    def test_manifest_count_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root)
            self.refresh_chain(fixture, count_overrides={"telegram_ciphertext_count": 9})
            with self.assertRaisesRegex(ValueError, "counts differ"):
                self.build(fixture)

    def test_stale_future_and_release_replay_evidence_stop(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root)
            value = json.loads(fixture["preflight"].read_text("utf-8"))
            value["generated_at"] = (NOW - timedelta(hours=25)).isoformat()
            fixture["preflight"].write_text(json.dumps(value), "utf-8")
            with self.assertRaisesRegex(ValueError, "stale or from the future"):
                self.build(fixture)

        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root)
            value = json.loads(fixture["monitor"].read_text("utf-8"))
            value["checks"]["backup"]["last_recovery_delivered_at"] = (
                NOW + timedelta(minutes=6)
            ).isoformat()
            fixture["monitor"].write_text(json.dumps(value), "utf-8")
            with self.assertRaisesRegex(ValueError, "not green"):
                self.build(fixture)

        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root)
            value = json.loads(fixture["readiness"].read_text("utf-8"))
            value["application_version"] = "v99.0.0"
            fixture["readiness"].write_text(json.dumps(value), "utf-8")
            with self.assertRaisesRegex(ValueError, "not green"):
                self.build(fixture)

    def test_restore_or_canary_chain_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root)
            restore = json.loads(fixture["restore"].read_text("utf-8"))
            restore["source_manifest_sha256"] = "0" * 64
            fixture["restore"].write_text(json.dumps(restore), "utf-8")
            with self.assertRaisesRegex(ValueError, "does not bind"):
                self.build(fixture)

    def test_output_is_exclusive_private_and_outside_repository(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = self.fixtures(root)
            report = self.build(fixture)
            forbidden = Path(__file__).resolve().parents[1] / "secret-recovery-forbidden.json"
            with self.assertRaisesRegex(ValueError, "inside repository"):
                write_report(forbidden, report)
            self.assertFalse(forbidden.exists())
            output = Path(root) / "evidence.json"
            write_report(output, report)
            with self.assertRaises(FileExistsError):
                write_report(output, report)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
