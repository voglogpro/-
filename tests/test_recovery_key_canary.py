import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch
from pathlib import Path

from cryptography.fernet import Fernet

from scripts import recovery_key_canary as canary_module
from scripts.recovery_key_canary import (
    CANARY_FILENAME,
    enroll_existing,
    ensure_recovery_key_canary,
    validate_canary_bytes,
    verify_canary_bytes,
)
from scripts.backup import create_backup
from scripts.restore import restore_backup


class RecoveryKeyCanaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.telegram = Fernet(Fernet.generate_key())
        self.withdrawal = Fernet(Fernet.generate_key())

    def tearDown(self):
        self.temporary.cleanup()

    def create(self, root=None):
        return ensure_recovery_key_canary(
            root or self.root, self.telegram, self.withdrawal, production=True,
        )

    def test_create_is_canonical_private_and_reused_byte_for_byte(self):
        path = self.create()
        first = path.read_bytes()
        document = validate_canary_bytes(first)
        self.assertEqual(document["version"], 1)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.create().read_bytes(), first)

    def test_wrong_key_cannot_rebind_existing_canary(self):
        path = self.create()
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "does not match"):
            ensure_recovery_key_canary(
                self.root, self.telegram, Fernet(Fernet.generate_key()),
                production=True,
            )
        self.assertEqual(path.read_bytes(), original)

    def test_production_never_mints_canary_beside_existing_data(self):
        (self.root / "bibitasks.db").write_bytes(b"existing-production-data")
        with self.assertRaisesRegex(RuntimeError, "missing beside existing"):
            self.create()
        self.assertFalse((self.root / CANARY_FILENAME).exists())

    def test_production_allows_empty_precreated_photo_directory_on_first_boot(self):
        (self.root / "task_photos").mkdir()
        self.assertTrue(self.create().is_file())

    def test_tampered_ciphertext_is_rejected(self):
        path = self.create()
        document = json.loads(path.read_text("ascii"))
        token = document["telegram_inbox_ciphertext"]
        document["telegram_inbox_ciphertext"] = (
            ("A" if token[0] != "A" else "B") + token[1:]
        )
        path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
        if os.name != "nt":
            path.chmod(0o600)
        with self.assertRaises(ValueError):
            self.create()

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not enforced on Windows")
    def test_production_rejects_permissive_canary_mode(self):
        path = self.create()
        path.chmod(0o644)
        with self.assertRaisesRegex(PermissionError, "mode 0600"):
            self.create()

    def test_symlink_canary_is_rejected(self):
        target = self.root / "outside.json"
        target.write_text("{}\n", encoding="ascii")
        try:
            (self.root / CANARY_FILENAME).symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            self.create()


class RecoveryKeyBackupBindingTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ, {"BIBITASKS_ENVIRONMENT": "test"}, clear=False,
        )
        self.environment.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        if os.name != "nt":
            self.data.chmod(0o700)
        self.backups = self.root / "backups"
        telegram = Fernet(Fernet.generate_key())
        withdrawal = Fernet(Fernet.generate_key())
        self.canary = ensure_recovery_key_canary(
            self.data, telegram, withdrawal, production=True,
        )
        with closing(sqlite3.connect(self.data / "bibitasks.db")) as db:
            db.executescript("""
                PRAGMA user_version=293;
                CREATE TABLE telegram_update_inbox (
                    update_id INTEGER PRIMARY KEY, payload_json TEXT, status TEXT
                );
                CREATE TABLE withdrawal_requests (
                    id INTEGER PRIMARY KEY, account_ciphertext TEXT, status TEXT
                );
                INSERT INTO telegram_update_inbox VALUES (1, 'cipher-a', 'pending');
                INSERT INTO telegram_update_inbox VALUES (2, NULL, 'done');
                INSERT INTO withdrawal_requests VALUES (1, 'cipher-b', 'processing');
                INSERT INTO withdrawal_requests VALUES (2, NULL, 'done');
            """)

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_backup_manifest_and_restore_bind_exact_canary_and_counts(self):
        original = self.canary.read_bytes()
        backup = create_backup(self.data, self.backups, allow_plaintext_dev=True)
        manifest = json.loads((backup / "manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["recovery_key_canary"], {
            "path": CANARY_FILENAME,
            "bytes": len(original),
            "sha256": hashlib.sha256(original).hexdigest(),
        })
        database = manifest["database"]
        self.assertEqual(database["telegram_ciphertext_count"], 1)
        self.assertEqual(database["telegram_active_null_count"], 0)
        self.assertEqual(database["withdrawal_ciphertext_count"], 1)
        self.assertEqual(database["withdrawal_active_null_count"], 0)

        restored = restore_backup(
            backup, self.root / "restored", allow_plaintext_dev=True,
        )
        self.assertEqual((restored / CANARY_FILENAME).read_bytes(), original)
        report = json.loads((restored / "restore-report.json").read_text("utf-8"))
        self.assertEqual(report["recovery_key_canary"], {
            "sha256": hashlib.sha256(original).hexdigest(), "ok": True,
        })

    def test_backup_fails_closed_for_missing_corrupt_or_active_null(self):
        original = self.canary.read_bytes()
        self.canary.unlink()
        with self.assertRaises(FileNotFoundError):
            create_backup(self.data, self.backups, allow_plaintext_dev=True)
        self.canary.write_text("{}\n", encoding="ascii")
        if os.name != "nt":
            self.canary.chmod(0o600)
        with self.assertRaises(ValueError):
            create_backup(self.data, self.backups, allow_plaintext_dev=True)

        self.canary.unlink()
        self.canary.write_bytes(original)
        if os.name != "nt":
            self.canary.chmod(0o600)
        with closing(sqlite3.connect(self.data / "bibitasks.db")) as db:
            db.execute(
                "INSERT INTO telegram_update_inbox VALUES (3, NULL, 'pending')"
            )
            db.commit()
        with self.assertRaisesRegex(RuntimeError, "missing ciphertext"):
            create_backup(self.data, self.backups, allow_plaintext_dev=True)

    def test_restore_rejects_canary_bytes_not_bound_to_manifest(self):
        backup = create_backup(self.data, self.backups, allow_plaintext_dev=True)
        canary = backup / CANARY_FILENAME
        canary.write_bytes(canary.read_bytes() + b" ")
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            restore_backup(
                backup, self.root / "restored", allow_plaintext_dev=True,
            )


class ExistingDataEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "existing-data"
        self.data.mkdir(mode=0o700)
        if os.name != "nt":
            self.data.chmod(0o700)
        self.telegram_raw = Fernet.generate_key()
        self.withdrawal_raw = Fernet.generate_key()
        self.telegram = Fernet(self.telegram_raw)
        self.withdrawal = Fernet(self.withdrawal_raw)
        payload = {"update_id": 7001, "message": {"text": "safe fixture"}}
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        inbox_ciphertext = self.telegram.encrypt(canonical.encode("utf-8")).decode("ascii")
        inbox_fingerprint = "h1:" + hmac.new(
            self.telegram_raw, canonical.encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        account = "BB-ACCOUNT-7001"
        account_ciphertext = self.withdrawal.encrypt(account.encode("utf-8")).decode("ascii")
        account_fingerprint = hmac.new(
            self.withdrawal_raw, account.casefold().encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        self.database = self.data / "bibitasks.db"
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript("""
                PRAGMA user_version=293;
                CREATE TABLE telegram_update_inbox (
                    update_id INTEGER PRIMARY KEY,payload_json TEXT,
                    payload_sha256 TEXT,status TEXT
                );
                CREATE TABLE withdrawal_requests (
                    id INTEGER PRIMARY KEY,account_ciphertext TEXT,
                    account_fingerprint TEXT,status TEXT
                );
            """)
            db.execute(
                "INSERT INTO telegram_update_inbox VALUES (?,?,?,'pending')",
                (7001, inbox_ciphertext, inbox_fingerprint),
            )
            db.execute(
                "INSERT INTO withdrawal_requests VALUES (1,?,?,'processing')",
                (account_ciphertext, account_fingerprint),
            )
            db.commit()
        if os.name != "nt":
            self.database.chmod(0o600)
        self.database_sha = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.env_file = self.root / "recovery.env"
        self.env_file.write_text(
            f"TELEGRAM_INBOX_KEY={self.telegram_raw.decode('ascii')}\n"
            f"WITHDRAW_ACCOUNT_KEY={self.withdrawal_raw.decode('ascii')}\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            self.env_file.chmod(0o600)

    def tearDown(self):
        self.temporary.cleanup()

    def enroll(self, **overrides):
        values = {
            "data_dir": self.data,
            "confirm_database_sha256": self.database_sha,
            "env_file": self.env_file,
            "now": datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
        }
        values.update(overrides)
        return enroll_existing(**values)

    def test_explicit_enrollment_binds_database_keys_and_private_report(self):
        report_path = self.root / "enrollment-report.json"
        real_publish = canary_module._publish_new_canary

        def assert_report_precedes_canary(*args, **kwargs):
            prepublished = json.loads(report_path.read_text("ascii"))
            self.assertEqual(set(prepublished), {
                "canary_sha256", "database_sha256", "enrolled_at", "schema_version",
            })
            self.assertFalse((self.data / CANARY_FILENAME).exists())
            return real_publish(*args, **kwargs)

        with patch.object(
            canary_module, "_publish_new_canary", assert_report_precedes_canary,
        ):
            result = self.enroll(report_path=report_path)
        self.assertEqual(set(result), {
            "canary_sha256", "database_sha256", "enrolled_at", "schema_version",
        })
        self.assertEqual(result["database_sha256"], self.database_sha)
        self.assertEqual(result["schema_version"], 293)
        canary = self.data / CANARY_FILENAME
        self.assertEqual(
            hashlib.sha256(canary.read_bytes()).hexdigest(), result["canary_sha256"],
        )
        verify_canary_bytes(canary.read_bytes(), self.telegram, self.withdrawal)
        self.assertEqual(
            json.loads(report_path.read_text("ascii")), result,
        )
        if os.name != "nt":
            self.assertEqual(canary.stat().st_mode & 0o777, 0o600)
            self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)

    def test_wrong_hash_or_wrong_keys_never_create_canary(self):
        with self.assertRaisesRegex(ValueError, "confirmation does not match"):
            self.enroll(confirm_database_sha256="0" * 64)
        self.assertFalse((self.data / CANARY_FILENAME).exists())

        wrong = self.root / "wrong.env"
        wrong.write_text(
            f"TELEGRAM_INBOX_KEY={Fernet.generate_key().decode('ascii')}\n"
            f"WITHDRAW_ACCOUNT_KEY={Fernet.generate_key().decode('ascii')}\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            wrong.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "do not verify"):
            self.enroll(env_file=wrong)
        self.assertFalse((self.data / CANARY_FILENAME).exists())

    def test_database_change_during_atomic_publication_removes_new_canary(self):
        real_publish = canary_module._publish_new_canary

        def publish_then_race(*args, **kwargs):
            result = real_publish(*args, **kwargs)
            with self.database.open("ab") as database:
                database.write(b"concurrent-change")
                database.flush()
                os.fsync(database.fileno())
            return result

        with patch.object(canary_module, "_publish_new_canary", publish_then_race):
            with self.assertRaisesRegex(RuntimeError, "changed during enrollment"):
                self.enroll()
        self.assertFalse((self.data / CANARY_FILENAME).exists())

    def test_late_wal_after_inspection_removes_report_and_canary(self):
        real_publish = canary_module._publish_new_canary
        report = self.root / "late-wal-report.json"

        def publish_then_add_wal(*args, **kwargs):
            result = real_publish(*args, **kwargs)
            (self.data / "bibitasks.db-wal").write_bytes(b"late-wal")
            return result

        with patch.object(canary_module, "_publish_new_canary", publish_then_add_wal):
            with self.assertRaisesRegex(RuntimeError, "WAL/SHM"):
                self.enroll(report_path=report)
        self.assertFalse((self.data / CANARY_FILENAME).exists())
        self.assertFalse(report.exists())

    @unittest.skipIf(os.name == "nt", "open SQLite inode replacement is POSIX-only")
    def test_late_path_swap_is_detected_from_stable_descriptor(self):
        real_publish = canary_module._publish_new_canary

        def publish_then_swap(*args, **kwargs):
            result = real_publish(*args, **kwargs)
            original = self.data / "original.db"
            self.database.replace(original)
            self.database.write_bytes(original.read_bytes())
            self.database.chmod(0o600)
            return result

        with patch.object(canary_module, "_publish_new_canary", publish_then_swap):
            with self.assertRaisesRegex(RuntimeError, "changed during enrollment"):
                self.enroll()
        self.assertFalse((self.data / CANARY_FILENAME).exists())

    def test_report_finalize_failure_rolls_back_exact_published_canary(self):
        report = self.root / "finalize-failure.json"
        with patch.object(
            canary_module, "_close_report_reservation",
            side_effect=RuntimeError("synthetic report finalize failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic report"):
                self.enroll(report_path=report)
        self.assertFalse((self.data / CANARY_FILENAME).exists())
        self.assertFalse(report.exists())

    @unittest.skipIf(os.name == "nt", "directory fsync is POSIX-only")
    def test_directory_fsync_failure_after_link_removes_new_canary(self):
        real_fsync = canary_module.os.fsync
        calls = 0

        def fail_second_fsync(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic directory fsync failure")
            return real_fsync(descriptor)

        with patch.object(canary_module.os, "fsync", fail_second_fsync):
            with self.assertRaisesRegex(OSError, "synthetic directory"):
                self.enroll()
        self.assertFalse((self.data / CANARY_FILENAME).exists())

    def test_active_null_ciphertext_rows_are_rejected(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(
                "INSERT INTO telegram_update_inbox VALUES (7002,NULL,'h1:none','processing')"
            )
            db.commit()
        if os.name != "nt":
            self.database.chmod(0o600)
        digest = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "missing ciphertext"):
            self.enroll(confirm_database_sha256=digest)
        self.assertFalse((self.data / CANARY_FILENAME).exists())
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DELETE FROM telegram_update_inbox WHERE update_id=7002")
            db.execute(
                "INSERT INTO withdrawal_requests VALUES (2,NULL,'none','pending')"
            )
            db.commit()
        if os.name != "nt":
            self.database.chmod(0o600)
        digest = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "missing ciphertext"):
            self.enroll(confirm_database_sha256=digest)
        self.assertFalse((self.data / CANARY_FILENAME).exists())

    def test_alternate_text_encoding_of_same_key_material_is_not_independent(self):
        alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        key = Fernet.generate_key()
        index = alphabet.index(key[-2])
        alternate_index = (index & 0b111100) | ((index + 1) & 0b11)
        alternate = key[:-2] + bytes([alphabet[alternate_index]]) + b"="
        self.assertNotEqual(key, alternate)
        self.assertEqual(
            canary_module.base64.urlsafe_b64decode(key),
            canary_module.base64.urlsafe_b64decode(alternate),
        )
        same_material = self.root / "same-material.env"
        same_material.write_text(
            f"TELEGRAM_INBOX_KEY={key.decode('ascii')}\n"
            f"WITHDRAW_ACCOUNT_KEY={alternate.decode('ascii')}\n",
            encoding="ascii",
        )
        if os.name != "nt":
            same_material.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "independent key material"):
            self.enroll(env_file=same_material)
        self.assertFalse((self.data / CANARY_FILENAME).exists())
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, "independent key material"):
            ensure_recovery_key_canary(
                empty, Fernet(key), Fernet(alternate), production=False,
            )
        self.assertFalse((empty / CANARY_FILENAME).exists())

    def test_database_hardlink_and_destination_schema_are_rejected(self):
        hardlink = self.data / "database-hardlink"
        try:
            os.link(self.database, hardlink)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "exactly one hard link"):
            self.enroll()
        hardlink.unlink()

        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA user_version=294")
        if os.name != "nt":
            self.database.chmod(0o600)
        digest = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "exact v2.9.1"):
            self.enroll(confirm_database_sha256=digest)
        self.assertFalse((self.data / CANARY_FILENAME).exists())

    @unittest.skipIf(os.name == "nt", "directory inode replacement is POSIX-only")
    def test_report_parent_swap_is_detected_and_never_mints_canary(self):
        evidence = self.root / "evidence"
        evidence.mkdir(mode=0o700)
        old_evidence = self.root / "evidence-original"
        report = evidence / "report.json"
        real_keys = canary_module._fernet_pair_from_explicit_source

        def keys_then_swap(**kwargs):
            result = real_keys(**kwargs)
            evidence.replace(old_evidence)
            evidence.mkdir(mode=0o700)
            return result

        with patch.object(
            canary_module, "_fernet_pair_from_explicit_source", keys_then_swap,
        ):
            with self.assertRaisesRegex(RuntimeError, "reservation changed"):
                self.enroll(report_path=report)
        self.assertFalse((self.data / CANARY_FILENAME).exists())
        self.assertFalse((old_evidence / "report.json").exists())
        self.assertFalse(report.exists())

    def test_missing_reserved_report_path_is_a_domain_failure(self):
        evidence = self.root / "missing-report-evidence"
        evidence.mkdir(mode=0o700)
        if os.name != "nt":
            evidence.chmod(0o700)
        report = evidence / "report.json"
        reservation = canary_module._reserve_enrollment_report(report)
        try:
            # Windows cannot unlink an open file. Closing only the target
            # descriptor still exercises the missing-path revalidation branch;
            # the separately held parent descriptor remains available.
            os.close(reservation["descriptor"])
            reservation["descriptor"] = None
            report.unlink()
            with self.assertRaisesRegex(RuntimeError, "reservation changed"):
                canary_module._verify_report_reservation(reservation)
        finally:
            canary_module._close_report_reservation(reservation)

    def test_existing_canary_wal_and_invalid_schema_fail_closed(self):
        (self.data / "bibitasks.db-wal").write_bytes(b"not-checkpointed")
        with self.assertRaisesRegex(RuntimeError, "WAL/SHM"):
            self.enroll()
        (self.data / "bibitasks.db-wal").unlink()
        ensure_recovery_key_canary(
            self.data, self.telegram, self.withdrawal, production=False,
        )
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            self.enroll()

    def test_schema_zero_is_rejected_and_explicit_key_files_are_supported(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA user_version=0")
        if os.name != "nt":
            self.database.chmod(0o600)
        zero_sha = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "exact v2.9.1"):
            self.enroll(confirm_database_sha256=zero_sha)
        self.assertFalse((self.data / CANARY_FILENAME).exists())

        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA user_version=293")
        if os.name != "nt":
            self.database.chmod(0o600)
        current_sha = hashlib.sha256(self.database.read_bytes()).hexdigest()
        telegram_file = self.root / "telegram.key"
        withdrawal_file = self.root / "withdrawal.key"
        telegram_file.write_bytes(self.telegram_raw + b"\n")
        withdrawal_file.write_bytes(self.withdrawal_raw + b"\n")
        if os.name != "nt":
            telegram_file.chmod(0o600)
            withdrawal_file.chmod(0o600)
        result = self.enroll(
            confirm_database_sha256=current_sha, env_file=None,
            telegram_key_file=telegram_file,
            withdrawal_key_file=withdrawal_file,
        )
        self.assertEqual(result["database_sha256"], current_sha)

    def test_symlink_key_source_is_rejected(self):
        real = self.root / "real.env"
        self.env_file.replace(real)
        try:
            self.env_file.symlink_to(real)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            self.enroll()
        self.assertFalse((self.data / CANARY_FILENAME).exists())

    def test_report_inside_repo_is_rejected_before_canary_publication(self):
        repo_report = Path(__file__).resolve().parents[1] / "enrollment-report-test.json"
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            self.enroll(report_path=repo_report)
        self.assertFalse((self.data / CANARY_FILENAME).exists())
        self.assertFalse(repo_report.exists())

    def test_cli_errors_are_generic_and_never_fall_back_to_process_keys(self):
        clean_env = {
            **os.environ,
            "TELEGRAM_INBOX_KEY": self.telegram_raw.decode("ascii"),
            "WITHDRAW_ACCOUNT_KEY": self.withdrawal_raw.decode("ascii"),
        }
        result = subprocess.run(
            [
                sys.executable, "scripts/recovery_key_canary.py", "enroll-existing",
                "--data-dir", str(self.data),
                "--confirm-database-sha256", self.database_sha,
            ],
            cwd=Path(__file__).resolve().parents[1], env=clean_env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertRegex(result.stderr, r"^enroll-existing failed: ValueError\s*$")
        self.assertNotIn(self.telegram_raw.decode("ascii"), result.stderr)
        self.assertFalse((self.data / CANARY_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
