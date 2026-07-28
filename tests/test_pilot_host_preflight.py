import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.pilot_host_preflight import parse_deploy_env, run_preflight


COMMIT = "a" * 40
IMAGE = "ghcr.io/voglogpro/bibitasks@sha256:" + "b" * 64


class FakeProbe:
    def __init__(self, *, commit=COMMIT, dirty=False, mount_target=None,
                 machine="x86_64", system="Linux", dns=None,
                 mount_source="backup.example:/bibitasks", mount_fstype="nfs4"):
        self.commit = commit
        self.dirty = dirty
        self.mount_target = mount_target
        self._machine = machine
        self._system = system
        self.dns = dns or {"8.8.8.8"}
        self.mount_source = mount_source
        self.mount_fstype = mount_fstype

    def command(self, args, *, cwd=None, timeout=30):
        args = [str(value) for value in args]
        stdout = ""
        returncode = 0
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            stdout = self.commit + "\n"
        elif args[:3] == ["git", "status", "--porcelain"]:
            stdout = "?? unexpected.txt\n" if self.dirty else ""
        elif args[:3] == ["docker", "image", "inspect"]:
            stdout = f"linux/amd64 {IMAGE}\n"
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    def machine(self):
        return self._machine

    def system(self):
        return self._system

    def os_release(self):
        return {"ID": "ubuntu", "VERSION_ID": "24.04"}

    def resolve(self, domain):
        return self.dns

    def mount(self, target):
        return {
            "target": str(self.mount_target or target),
            "source": self.mount_source, "fstype": self.mount_fstype,
        }


class PilotHostPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.backup = self.root / "remote-backup"
        self.backup.mkdir()
        self.sentinel = self.backup / ".bibitasks-offhost"
        self.sentinel.write_text("bibitasks-offhost-v1\n", encoding="utf-8")
        self.production = self.root / "bibitasks.env"
        self.production.write_text("BOT_TOKEN=not-read-by-preflight\n", encoding="utf-8")
        self.deploy = self.root / "deploy.env"
        self._write_deploy()
        os.chmod(self.production, 0o600)
        os.chmod(self.deploy, 0o640)
        self.owner = self.deploy.stat().st_uid if hasattr(self.deploy.stat(), "st_uid") else None

    def tearDown(self):
        self.temp.cleanup()

    def _write_deploy(self, extra="", *, image=IMAGE):
        self.deploy.write_text(
            "\n".join([
                f"BIBITASKS_IMAGE={image}",
                f"BIBITASKS_RELEASE_COMMIT={COMMIT}",
                f"BIBITASKS_ENV_FILE={self.production}",
                "BIBITASKS_DOMAIN=tasks.example.com",
                f"BACKUP_DIR={self.backup}",
                f"BACKUP_SENTINEL={self.sentinel}",
                "BACKUP_SENTINEL_VALUE=bibitasks-offhost-v1",
                "BACKUP_EXPECTED_SOURCE=backup.example:/bibitasks",
                "BIBITASKS_DATA_VOLUME=bibitasks_data",
                extra,
                "",
            ]),
            encoding="utf-8",
        )

    def _run(self, probe=None, **overrides):
        values = {
            "deploy_env": self.deploy,
            "repo": self.repo,
            "expected_commit": COMMIT,
            "expected_image": IMAGE,
            "probe": probe or FakeProbe(mount_target=self.backup),
            "expected_owner_uid": self.owner,
        }
        values.update(overrides)
        return run_preflight(**values)

    def _status(self, report, name):
        return next(item["status"] for item in report["checks"] if item["name"] == name)

    def test_green_host_passes_without_exposing_secret_file(self):
        report = self._run()
        self.assertTrue(report["ok"], json.dumps(report, indent=2))
        self.assertNotIn("not-read-by-preflight", json.dumps(report))

    def test_rejects_mutable_or_mismatched_image(self):
        self._write_deploy(image="ghcr.io/voglogpro/bibitasks:latest")
        os.chmod(self.deploy, 0o640)
        report = self._run()
        self.assertFalse(report["ok"])
        self.assertEqual(self._status(report, "release image binding"), "fail")

    def test_rejects_secret_in_deploy_env_without_printing_value(self):
        self._write_deploy(extra="BOT_TOKEN=123456:should-never-be-here")
        os.chmod(self.deploy, 0o640)
        report = self._run()
        rendered = json.dumps(report)
        self.assertFalse(report["ok"])
        self.assertIn("BOT_TOKEN", rendered)
        self.assertNotIn("should-never-be-here", rendered)

    def test_rejects_duplicate_variable(self):
        self._write_deploy(extra="BIBITASKS_DOMAIN=other.example.com")
        with self.assertRaisesRegex(ValueError, "duplicate BIBITASKS_DOMAIN"):
            parse_deploy_env(self.deploy)

    def test_rejects_quoted_value(self):
        self._write_deploy(image='"' + IMAGE + '"')
        with self.assertRaisesRegex(ValueError, "plain non-empty value"):
            parse_deploy_env(self.deploy)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are authoritative on the VPS")
    def test_rejects_broad_secret_permissions(self):
        os.chmod(self.production, 0o644)
        report = self._run()
        self.assertEqual(self._status(report, "production env permissions"), "fail")

    def test_rejects_root_filesystem_as_backup(self):
        report = self._run(FakeProbe(mount_target=Path("/")))
        self.assertEqual(self._status(report, "backup mount"), "fail")

    def test_rejects_separate_local_filesystem_as_offhost_backup(self):
        report = self._run(FakeProbe(
            mount_target=self.backup, mount_source="/dev/sdb1", mount_fstype="ext4",
        ))
        self.assertEqual(self._status(report, "backup mount"), "fail")

    def test_rejects_wrong_remote_export(self):
        report = self._run(FakeProbe(
            mount_target=self.backup, mount_source="backup.example:/other",
        ))
        self.assertEqual(self._status(report, "backup mount"), "fail")

    def test_rejects_private_dns_answer(self):
        report = self._run(FakeProbe(mount_target=self.backup, dns={"127.0.0.1"}))
        self.assertEqual(self._status(report, "public DNS"), "fail")

    def test_rejects_dirty_checkout(self):
        report = self._run(FakeProbe(mount_target=self.backup, dirty=True))
        self.assertEqual(self._status(report, "repository state"), "fail")

    def test_rejects_wrong_architecture(self):
        report = self._run(FakeProbe(mount_target=self.backup, machine="aarch64"))
        self.assertEqual(self._status(report, "host architecture"), "fail")

    def test_rejects_short_commit_before_host_commands(self):
        report = self._run(expected_commit="abc123")
        self.assertFalse(report["ok"])
        self.assertEqual(self._status(report, "expected commit"), "fail")

    def test_sentinel_must_be_inside_backup_directory(self):
        outside = self.root / "outside-sentinel"
        outside.write_text("wrong\n", encoding="utf-8")
        text = self.deploy.read_text("utf-8").replace(
            f"BACKUP_SENTINEL={self.sentinel}", f"BACKUP_SENTINEL={outside}",
        )
        self.deploy.write_text(text, encoding="utf-8")
        os.chmod(self.deploy, 0o640)
        report = self._run()
        self.assertEqual(self._status(report, "backup sentinel"), "fail")


if __name__ == "__main__":
    unittest.main()
