import base64
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import tarfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.backup import create_backup
from scripts.backup_crypto import (
    BACKUP_FORMAT, ENCRYPTION_METHOD, PAYLOAD_NAME, encryption_aad,
    key_document, load_backup_key,
)
from scripts.recovery_key_canary import ensure_recovery_key_canary
from scripts.restore import restore_backup
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class BackupEncryptionTests(unittest.TestCase):
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
        ensure_recovery_key_canary(
            self.data, Fernet(Fernet.generate_key()), Fernet(Fernet.generate_key()),
            production=True,
        )
        with closing(sqlite3.connect(self.data / "bibitasks.db")) as db:
            db.executescript("""
                PRAGMA user_version=299;
                CREATE TABLE telegram_update_inbox (
                    update_id INTEGER PRIMARY KEY, payload_json TEXT, status TEXT
                );
                CREATE TABLE withdrawal_requests (
                    id INTEGER PRIMARY KEY, account_ciphertext TEXT, status TEXT
                );
                INSERT INTO telegram_update_inbox VALUES (1, 'cipher', 'pending');
                INSERT INTO withdrawal_requests VALUES (1, 'cipher', 'processing');
            """)
        photos = self.data / "task_photos"
        photos.mkdir()
        (photos / "proof.jpg").write_bytes(b"not-real-image-but-valid-backup-bytes")
        self.backups = self.root / "backups"
        self.key = self.root / "backup.key"
        self.key.write_bytes(key_document(os.urandom(32), "pilot-2026-07"))
        if os.name != "nt":
            self.key.chmod(0o600)

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def backup(self):
        return create_backup(
            self.data, self.backups, encryption_key_file=self.key,
            key_version="pilot-2026-07",
            plaintext_tmp_dir=self.root, allow_unverified_temp_dev=True,
        )

    def test_encrypted_round_trip_and_manifest_attestation(self):
        backup = self.backup()
        manifest = json.loads((backup / "manifest.json").read_text("utf-8"))
        encryption = manifest["encryption"]
        self.assertEqual(encryption["method"], ENCRYPTION_METHOD)
        self.assertEqual(encryption["key_version"], "pilot-2026-07")
        ciphertext = backup / PAYLOAD_NAME
        self.assertEqual(
            hashlib.sha256(ciphertext.read_bytes()).hexdigest(),
            encryption["ciphertext"]["sha256"],
        )
        self.assertEqual(
            {item.name for item in backup.iterdir()}, {"manifest.json", PAYLOAD_NAME},
        )
        self.assertNotIn(b"SQLite format 3", ciphertext.read_bytes())
        self.assertFalse(any(backup.rglob("bibitasks.db")))
        self.assertFalse(any(self.root.glob("bibitasks-backup-*")))

        restored = restore_backup(
            backup, self.root / "restored", encryption_key_file=self.key,
            plaintext_tmp_dir=self.root, allow_unverified_temp_dev=True,
        )
        self.assertEqual((restored / "task_photos" / "proof.jpg").read_bytes(),
                         b"not-real-image-but-valid-backup-bytes")
        report = json.loads((restored / "restore-report.json").read_text("utf-8"))
        self.assertEqual(report["encryption"], {
            "method": ENCRYPTION_METHOD,
            "key_version": "pilot-2026-07",
            "ciphertext_sha256": encryption["ciphertext"]["sha256"],
            "authenticated": True,
        })
        self.assertNotIn(str(self.key), json.dumps(report))

    def test_missing_key_and_plaintext_are_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "encryption key file is required"):
            create_backup(self.data, self.backups)
        self.assertFalse(self.backups.exists() and any(self.backups.iterdir()))

        with self.assertRaisesRegex(RuntimeError, "memory-backed plaintext scratch"):
            create_backup(
                self.data, self.backups, encryption_key_file=self.key,
                key_version="pilot-2026-07",
            )
        self.assertFalse(self.backups.exists() and any(self.backups.iterdir()))

        plaintext = create_backup(
            self.data, self.backups, allow_plaintext_dev=True,
        )
        with self.assertRaisesRegex(RuntimeError, "explicit non-production"):
            restore_backup(plaintext, self.root / "denied")
        self.assertFalse((self.root / "denied").exists())

    def test_wrong_key_and_ciphertext_tamper_leave_no_plaintext(self):
        backup = self.backup()
        wrong = self.root / "wrong.key"
        wrong.write_bytes(key_document(os.urandom(32), "pilot-2026-07"))
        if os.name != "nt":
            wrong.chmod(0o600)
        target = self.root / "wrong-restore"
        with patch(
            "scripts.backup_crypto.tarfile.open",
            side_effect=AssertionError("tar must not parse before authentication"),
        ):
            with self.assertRaises(Exception) as raised:
                restore_backup(
                    backup, target, encryption_key_file=wrong,
                    plaintext_tmp_dir=self.root, allow_unverified_temp_dev=True,
                )
        self.assertNotIsInstance(raised.exception, AssertionError)
        self.assertFalse(target.exists())
        self.assertFalse(any(self.root.glob("bibitasks-restore-*")))

        payload = backup / PAYLOAD_NAME
        raw = bytearray(payload.read_bytes())
        raw[len(raw) // 2] ^= 1
        payload.write_bytes(raw)
        tampered = self.root / "tampered-restore"
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            restore_backup(
                backup, tampered, encryption_key_file=self.key,
                plaintext_tmp_dir=self.root, allow_unverified_temp_dev=True,
            )
        self.assertFalse(tampered.exists())
        self.assertFalse(any(self.root.glob("bibitasks-restore-*")))

    def test_manifest_binding_and_unsafe_legacy_path_are_rejected(self):
        backup = self.backup()
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["database"]["schema_version"] = 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(Exception):
            restore_backup(
                backup, self.root / "manifest-tamper", encryption_key_file=self.key,
                plaintext_tmp_dir=self.root, allow_unverified_temp_dev=True,
            )
        self.assertFalse((self.root / "manifest-tamper").exists())

        legacy = create_backup(
            self.data, self.root / "legacy", allow_plaintext_dev=True,
        )
        legacy_manifest = json.loads((legacy / "manifest.json").read_text("utf-8"))
        legacy_manifest["database"]["path"] = "../outside.db"
        (legacy / "manifest.json").write_text(json.dumps(legacy_manifest), "utf-8")
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            restore_backup(
                legacy, self.root / "unsafe-restore", allow_plaintext_dev=True,
            )
        self.assertFalse((self.root / "unsafe-restore").exists())

    def test_plaintext_temporary_tree_is_removed_when_encryption_fails(self):
        with patch("scripts.backup.encrypt_directory", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                self.backup()
        self.assertFalse(any(self.root.glob("bibitasks-backup-*")))
        self.assertFalse(self.backups.exists() and any(self.backups.iterdir()))

    def test_authenticated_archive_with_traversal_path_is_rejected(self):
        malicious = self.root / "malicious"
        malicious.mkdir()
        tar_bytes = io.BytesIO()
        with tarfile.open(fileobj=tar_bytes, mode="w") as archive:
            info = tarfile.TarInfo("../escaped.db")
            content = b"forbidden"
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        protected = "1" * 64
        aad = encryption_aad("pilot-2026-07", protected)
        nonce = os.urandom(12)
        key, _ = load_backup_key(self.key)
        sealed = AESGCM(key).encrypt(nonce, tar_bytes.getvalue(), aad)
        ciphertext, tag = sealed[:-16], sealed[-16:]
        payload = malicious / PAYLOAD_NAME
        payload.write_bytes(ciphertext)
        encryption = {
            "format": BACKUP_FORMAT,
            "method": ENCRYPTION_METHOD,
            "key_version": "pilot-2026-07",
            "nonce_b64": base64.urlsafe_b64encode(nonce).decode("ascii"),
            "tag_b64": base64.urlsafe_b64encode(tag).decode("ascii"),
            "protected_manifest_sha256": protected,
            "aad_sha256": hashlib.sha256(aad).hexdigest(),
            "ciphertext": {
                "path": PAYLOAD_NAME, "bytes": len(ciphertext),
                "sha256": hashlib.sha256(ciphertext).hexdigest(),
            },
        }
        (malicious / "manifest.json").write_text(
            json.dumps({"encryption": encryption}), encoding="utf-8",
        )
        escaped = self.root / "escaped.db"
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            restore_backup(
                malicious, self.root / "malicious-restore",
                encryption_key_file=self.key, plaintext_tmp_dir=self.root,
                allow_unverified_temp_dev=True,
            )
        self.assertFalse(escaped.exists())
        self.assertFalse(any(self.root.glob("bibitasks-restore-*")))

    def _insert_s3_media(self, media_id, content):
        digest = hashlib.sha256(content).hexdigest()
        with closing(sqlite3.connect(self.data / "bibitasks.db")) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS media_objects (
                    id TEXT PRIMARY KEY, backend TEXT, object_key TEXT, state TEXT,
                    size_bytes INTEGER, sha256 TEXT, version_id TEXT
                )
            """)
            db.execute(
                "INSERT INTO media_objects VALUES (?,?,?,?,?,?,?)",
                (media_id, "s3", "safe.jpg", "ready", len(content), digest, None),
            )
            db.commit()

    def test_s3_media_id_cannot_escape_memory_scratch(self):
        self._insert_s3_media("../../outside", b"abc")
        with self.assertRaisesRegex(ValueError, "canonical UUID"):
            create_backup(
                self.data, self.backups, encryption_key_file=self.key,
                key_version="pilot-2026-07", plaintext_tmp_dir=self.root,
                allow_unverified_temp_dev=True, allow_s3_dev=True,
            )
        self.assertFalse((self.root.parent / "outside.jpg").exists())
        self.assertFalse(any(self.root.glob("bibitasks-backup-*")))

    def test_s3_download_is_bounded_to_expected_size_plus_one(self):
        media_id = "12345678-1234-4234-8234-123456789abc"
        self._insert_s3_media(media_id, b"abc")

        class Body:
            def __init__(self):
                self.content = b"abcX"
                self.read_sizes = []

            def read(self, amount):
                self.read_sizes.append(amount)
                result, self.content = self.content[:amount], self.content[amount:]
                return result

            def close(self):
                pass

        body = Body()
        client = SimpleNamespace(get_object=lambda **_kwargs: {"Body": body})
        boto3 = SimpleNamespace(client=lambda *_args, **_kwargs: client)
        botocore = SimpleNamespace()
        botocore_config = SimpleNamespace(Config=lambda **kwargs: kwargs)
        with patch.dict(sys.modules, {
            "boto3": boto3, "botocore": botocore,
            "botocore.config": botocore_config,
        }), patch.dict(os.environ, {"S3_BUCKET": "private-test"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "exceeds expected size"):
                create_backup(
                    self.data, self.backups, encryption_key_file=self.key,
                    key_version="pilot-2026-07", plaintext_tmp_dir=self.root,
                    allow_unverified_temp_dev=True, allow_s3_dev=True,
                )
        self.assertTrue(body.read_sizes)
        self.assertLessEqual(max(body.read_sizes), 4)
        self.assertFalse(any(self.root.glob("bibitasks-backup-*")))

    def test_cleanup_failure_is_explicit_and_never_reports_success(self):
        with patch(
            "scripts.backup.cleanup_private_tree",
            side_effect=RuntimeError("synthetic cleanup denial"),
        ):
            with self.assertRaisesRegex(RuntimeError, "secure cleanup was incomplete"):
                self.backup()

    def test_empty_environment_cannot_enable_dev_plaintext(self):
        with patch.dict(os.environ, {"BIBITASKS_ENVIRONMENT": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "explicit dev/test"):
                create_backup(
                    self.data, self.backups, allow_plaintext_dev=True,
                )

    def test_oversized_outer_manifest_is_rejected_before_json_parse(self):
        oversized = self.root / "oversized"
        oversized.mkdir()
        (oversized / "manifest.json").write_bytes(b"{" + b" " * (1024 * 1024))
        with self.assertRaisesRegex(ValueError, "unexpectedly large"):
            restore_backup(oversized, self.root / "oversized-target")

    def test_s3_is_fail_closed_by_default_for_network_isolated_pilot(self):
        self._insert_s3_media("12345678-1234-4234-8234-123456789abc", b"abc")
        with self.assertRaisesRegex(RuntimeError, "network-isolated pilot"):
            self.backup()

    def test_production_image_and_compose_include_crypto_runtime_guards(self):
        repo = Path(__file__).resolve().parents[1]
        dockerfile = (repo / "Dockerfile").read_text("utf-8")
        compose = (repo / "compose.pilot.yaml").read_text("utf-8")
        self.assertIn("scripts/backup_crypto.py", dockerfile)
        self.assertIn("network_mode: none", compose)
        self.assertIn("mem_limit: 768m", compose)
        self.assertIn("/run/bibitasks-backup-plaintext:size=512m", compose)
        self.assertIn("core: 0", compose)


if __name__ == "__main__":
    unittest.main()
