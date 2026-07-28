import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import bootstrap_loadtest_env as bootstrap
from scripts.loadtest_host_preflight import _absolute_unresolved, run_preflight


COMMIT = "b" * 40
IMAGE = "ghcr.io/voglogpro/bibitasks@sha256:" + "a" * 64
TOKEN = "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
PRODUCTION_TOKEN = "654321:" + "zyxwvutsrqponmlkjihgfedcba987654321"


def parse_env(path):
    return dict(line.split("=", 1) for line in path.read_text("utf-8").splitlines())


class FakeProbe:
    def __init__(self, deploy, repo, *, resources_exist=False, contaminated="",
                 dirty=False, telegram_id=900001, labels_ok=True):
        self.deploy = deploy
        self.repo = repo
        self.resources_exist = resources_exist
        self.contaminated = contaminated
        self.dirty = dirty
        self.telegram_id = telegram_id
        self.labels_ok = labels_ok
        self.destroyed = False
        self.applied = False

    def system(self):
        return "Linux"

    def machine(self):
        return "x86_64"

    def resolve(self, _domain):
        return {"8.8.8.8"}

    def telegram_get_me(self, _token):
        return {"id": self.telegram_id, "is_bot": True, "username": "BibiLoadTestBot"}

    def _exists(self):
        return self.resources_exist and not self.destroyed

    def _manifest(self):
        evidence = str(Path(self.deploy["BIBITASKS_LOADTEST_EVIDENCE_DIR"]).resolve())
        caddyfile = str(_absolute_unresolved(self.repo) / "deploy" / "Caddyfile.loadtest")
        purpose = "production" if self.contaminated == "labels" else "loadtest"
        manifest = {
            "name": self.deploy["BIBITASKS_LOADTEST_PROJECT"],
            "services": {
                "bibitasks": {
                    "image": IMAGE,
                    "environment": {
                        "BIBITASKS_ENVIRONMENT": "staging",
                        "PILOT_LOAD_TEST_ENABLED": "true",
                        "PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED": "true",
                    },
                    "networks": {"loadtest": None},
                    "volumes": [{"type": "volume", "source": "loadtest_data", "target": "/app/data"}],
                },
                "caddy": {
                    "environment": {
                        "BIBITASKS_DOMAIN": self.deploy["BIBITASKS_LOADTEST_DOMAIN"],
                    },
                    "networks": {"loadtest": {"aliases": [self.deploy["BIBITASKS_LOADTEST_DOMAIN"]]}},
                    "volumes": [
                        {"type": "bind", "source": caddyfile, "target": "/etc/caddy/Caddyfile", "read_only": True},
                        {"type": "volume", "source": "loadtest_caddy_data", "target": "/data"},
                        {"type": "volume", "source": "loadtest_caddy_config", "target": "/config"},
                    ],
                },
                "loadtest-runner": {
                    "image": IMAGE,
                    "profiles": ["loadtest"],
                    "environment": {
                        "BIBITASKS_ENVIRONMENT": "staging",
                        "PILOT_LOAD_TEST_ENABLED": "true",
                        "PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED": "true",
                    },
                    "networks": {"loadtest": None},
                    "volumes": [{"type": "bind", "source": evidence, "target": "/evidence"}],
                },
            },
            "networks": {
                "loadtest": {
                    "name": self.deploy["BIBITASKS_LOADTEST_NETWORK"],
                    "labels": {"com.bibitasks.purpose": purpose},
                },
            },
            "volumes": {},
        }
        for logical, key in (
            ("loadtest_data", "BIBITASKS_LOADTEST_DATA_VOLUME"),
            ("loadtest_caddy_data", "BIBITASKS_LOADTEST_CADDY_DATA_VOLUME"),
            ("loadtest_caddy_config", "BIBITASKS_LOADTEST_CADDY_CONFIG_VOLUME"),
        ):
            manifest["volumes"][logical] = {
                "name": self.deploy[key],
                "labels": {
                    "com.bibitasks.purpose": purpose,
                    "com.bibitasks.release-commit": COMMIT,
                },
            }
        if self.contaminated == "bind":
            manifest["services"]["bibitasks"]["volumes"].append({
                "type": "bind", "source": "/", "target": "/host",
            })
        if self.contaminated == "production_network":
            manifest["networks"]["loadtest"]["name"] = self.deploy["BIBITASKS_PRODUCTION_NETWORK"]
        return manifest

    def command(self, args, *, cwd=None, timeout=30):
        command = [str(value) for value in args]
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=COMMIT + "\n", stderr="")
        if command[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout=" M main.py\n" if self.dirty else "", stderr="")
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=f"linux/amd64 {IMAGE}\n", stderr="")
        if command[:3] in (["docker", "volume", "inspect"], ["docker", "network", "inspect"]):
            if not self._exists():
                return SimpleNamespace(returncode=1, stdout="", stderr="not found")
            labels = {
                "com.bibitasks.purpose": "loadtest" if self.labels_ok else "production",
            }
            if command[1] == "volume":
                labels["com.bibitasks.release-commit"] = COMMIT
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Labels": labels}]), stderr="")
        if command[:3] == ["docker", "ps", "-a"]:
            output = "bibitasks\ncaddy\n" if self._exists() else ""
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        if command[:2] == ["docker", "compose"]:
            if "config" in command:
                return SimpleNamespace(returncode=0, stdout=json.dumps(self._manifest()), stderr="")
            if "up" in command:
                self.applied = True
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "down" in command:
                self.destroyed = True
                return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")


class LoadtestHostPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        (self.repo / "deploy").mkdir(parents=True)
        (self.repo / "deploy" / "Caddyfile.loadtest").write_text("fixture\n", encoding="utf-8")
        self.token = self.root / "staging-token"
        self.production_token = self.root / "production-token"
        self.token.write_text(TOKEN + "\n", encoding="utf-8")
        self.production_token.write_text(PRODUCTION_TOKEN + "\n", encoding="utf-8")
        if os.name != "nt":
            self.token.chmod(0o600)
            self.production_token.chmod(0o600)
        self.bundle = self.root / "bundle"
        args = SimpleNamespace(
            apply=True, domain="load.tasks.example.test",
            confirm_domain="load.tasks.example.test",
            production_domain="tasks.example.test", production_volume="bibitasks_data",
            production_network="bibitasks-pilot_pilot", release_commit=COMMIT,
            image=IMAGE, bot_token_file=self.token,
            production_bot_token_file=self.production_token,
            bot_username="BibiLoadTestBot", admin_user_id=4_400_000_000_000_000,
            privacy_contact="@loadtest_operator", output_dir=self.bundle,
        )
        bootstrap.build_bundle(args, api_call=self._bot_call)
        self.deploy_path = self.bundle / "deploy.env"
        self.deploy = parse_env(self.deploy_path)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _bot_call(token, _method, _params=None):
        if token == TOKEN:
            return {"id": 900001, "is_bot": True, "username": "BibiLoadTestBot"}
        return {"id": 900002, "is_bot": True, "username": "BbGalterbot"}

    def _run(self, probe=None, **kwargs):
        return run_preflight(
            deploy_env=self.deploy_path, repo=self.repo,
            expected_commit=COMMIT, expected_image=IMAGE,
            probe=probe or FakeProbe(self.deploy, self.repo),
            expected_owner_uid=None, **kwargs,
        )

    @staticmethod
    def _status(report, name):
        return next(item["status"] for item in report["checks"] if item["name"] == name)

    def test_fresh_exact_staging_passes_without_secret_values(self):
        report = self._run()
        self.assertTrue(report["ok"], report)
        rendered = json.dumps(report)
        self.assertNotIn(TOKEN, rendered)
        self.assertNotIn("4400000000000000", rendered)
        self.assertEqual(self._status(report, "isolated Compose render"), "pass")

    def test_extra_bind_or_production_network_fails_render(self):
        for contamination in ("bind", "production_network", "labels"):
            with self.subTest(contamination=contamination):
                probe = FakeProbe(self.deploy, self.repo, contaminated=contamination)
                report = self._run(probe)
                self.assertEqual(self._status(report, "isolated Compose render"), "fail")

    def test_existing_any_resource_fails_fresh_check(self):
        report = self._run(FakeProbe(self.deploy, self.repo, resources_exist=True))
        self.assertEqual(self._status(report, "fresh load resources"), "fail")

    def test_production_bot_id_and_telegram_destinations_are_fail_closed(self):
        production_id = int(self.deploy["BIBITASKS_PRODUCTION_BOT_ID"])
        report = self._run(FakeProbe(self.deploy, self.repo, telegram_id=production_id))
        self.assertEqual(self._status(report, "live staging bot identity"), "fail")
        staging_path = self.bundle / "staging.env"
        text = staging_path.read_text("utf-8").replace(
            "JOIN_REQUEST_ADMISSION_ENABLED=false", "JOIN_REQUEST_ADMISSION_ENABLED=true",
        )
        staging_path.write_text(text, encoding="utf-8")
        if os.name != "nt":
            staging_path.chmod(0o600)
        report = self._run()
        self.assertEqual(self._status(report, "synthetic Telegram destinations"), "fail")

    def test_unexpected_env_key_and_dirty_repo_fail(self):
        staging_path = self.bundle / "staging.env"
        staging_path.write_text(staging_path.read_text("utf-8") + "HTTP_PROXY=https://bad.test\n", encoding="utf-8")
        if os.name != "nt":
            staging_path.chmod(0o600)
        report = self._run(FakeProbe(self.deploy, self.repo, dirty=True))
        self.assertEqual(self._status(report, "bundle paths"), "fail")
        self.assertEqual(self._status(report, "repository release state"), "fail")

    def test_apply_requires_confirmation_and_runs_only_after_green(self):
        denied_probe = FakeProbe(self.deploy, self.repo)
        denied = self._run(denied_probe, operation="apply", confirm_domain="wrong.example.test")
        self.assertFalse(denied["ok"])
        self.assertFalse(denied_probe.applied)
        probe = FakeProbe(self.deploy, self.repo)
        report = self._run(
            probe, operation="apply", confirm_domain="load.tasks.example.test",
        )
        self.assertTrue(report["ok"], report)
        self.assertTrue(probe.applied)
        self.assertEqual(self._status(report, "compose apply"), "pass")

    def test_destroy_requires_exact_labelled_resources_and_verifies_absence(self):
        probe = FakeProbe(self.deploy, self.repo, resources_exist=True)
        report = self._run(
            probe, operation="destroy", confirm_domain="load.tasks.example.test",
        )
        self.assertTrue(report["ok"], report)
        self.assertTrue(probe.destroyed)
        self.assertEqual(self._status(report, "compose destroy"), "pass")
        denied_probe = FakeProbe(
            self.deploy, self.repo, resources_exist=True, labels_ok=False,
        )
        denied = self._run(
            denied_probe, operation="destroy", confirm_domain="load.tasks.example.test",
        )
        self.assertFalse(denied["ok"])
        self.assertFalse(denied_probe.destroyed)


if __name__ == "__main__":
    unittest.main()
