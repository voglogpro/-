import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import bootstrap_loadtest_env as bootstrap


COMMIT = "b" * 40
IMAGE = "ghcr.io/voglogpro/bibitasks@sha256:" + "a" * 64
TOKEN = "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
PRODUCTION_TOKEN = "654321:" + "zyxwvutsrqponmlkjihgfedcba987654321"


def parse_env(path):
    return dict(
        line.split("=", 1)
        for line in path.read_text("utf-8").splitlines()
        if line and not line.startswith("#")
    )


class BootstrapLoadtestEnvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.token = self.root / "input-token"
        self.production_token = self.root / "production-token"
        self.token.write_text(TOKEN + "\n", encoding="utf-8")
        self.production_token.write_text(PRODUCTION_TOKEN + "\n", encoding="utf-8")
        if os.name != "nt":
            self.token.chmod(0o600)
            self.production_token.chmod(0o600)
        self.output = self.root / "bundle"

    def tearDown(self):
        self.temp.cleanup()

    def args(self, **overrides):
        values = {
            "apply": True,
            "domain": "load.tasks.example.test",
            "confirm_domain": "load.tasks.example.test",
            "production_domain": "tasks.example.test",
            "production_volume": "bibitasks_data",
            "production_network": "bibitasks-pilot_pilot",
            "release_commit": COMMIT,
            "image": IMAGE,
            "bot_token_file": self.token,
            "production_bot_token_file": self.production_token,
            "bot_username": "BibiLoadTestBot",
            "admin_user_id": 4_400_000_000_000_000,
            "privacy_contact": "@loadtest_operator",
            "output_dir": self.output,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @staticmethod
    def api_call(token, _method, _params=None):
        if token == TOKEN:
            return {"id": 900001, "is_bot": True, "username": "BibiLoadTestBot"}
        if token == PRODUCTION_TOKEN:
            return {"id": 900002, "is_bot": True, "username": "BbGalterbot"}
        raise AssertionError("unexpected token")

    def test_dry_run_does_not_read_either_token_or_create_output(self):
        missing = self.root / "missing-token"
        args = self.args(
            apply=False, bot_token_file=missing,
            production_bot_token_file=self.root / "missing-production-token",
        )
        result = bootstrap.plan(args)
        self.assertFalse(result["reads_two_bot_tokens"])
        self.assertFalse(result["creates_owner_only_files"])
        self.assertFalse(self.output.exists())

    def test_bundle_is_private_complete_and_importable(self):
        result = bootstrap.build_bundle(self.args(), api_call=self.api_call)
        self.assertEqual(result["scope"], "disposable_loadtest_bundle_paths_no_secret_values")
        rendered_result = json.dumps(result)
        self.assertNotIn(TOKEN, rendered_result)
        self.assertNotIn(PRODUCTION_TOKEN, rendered_result)
        expected_files = {
            "staging.env", "deploy.env", "bot-token", "health-token",
            "webhook-secret", "webhook-path", "operator.json",
        }
        self.assertEqual(
            {path.name for path in self.output.iterdir() if path.is_file()},
            expected_files,
        )
        self.assertTrue((self.output / "evidence").is_dir())
        if os.name != "nt":
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.output / "evidence").stat().st_mode & 0o777, 0o770)
            for path in self.output.iterdir():
                if path.is_file():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        stage = parse_env(self.output / "staging.env")
        deploy = parse_env(self.output / "deploy.env")
        self.assertEqual(stage["BIBITASKS_ENVIRONMENT"], "staging")
        self.assertEqual(stage["PILOT_LOAD_TEST_ENABLED"], "true")
        self.assertEqual(stage["PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED"], "true")
        self.assertEqual(stage["JOIN_REQUEST_ADMISSION_ENABLED"], "false")
        self.assertEqual(stage["BOT_TOKEN"], TOKEN)
        self.assertEqual(deploy["BIBITASKS_PRODUCTION_BOT_ID"], "900002")
        self.assertEqual(result["staging_bot_id"], 900001)
        self.assertEqual(result["staging_bot_username"], "BibiLoadTestBot")
        self.assertNotIn("PRODUCTION_BOT_TOKEN", deploy)
        self.assertNotIn(PRODUCTION_TOKEN, (self.output / "deploy.env").read_text("utf-8"))
        self.assertEqual(deploy["BIBITASKS_LOADTEST_EVIDENCE_DIR"], str(self.output / "evidence"))
        resources = [
            deploy["BIBITASKS_LOADTEST_PROJECT"],
            deploy["BIBITASKS_LOADTEST_NETWORK"],
            deploy["BIBITASKS_LOADTEST_DATA_VOLUME"],
            deploy["BIBITASKS_LOADTEST_CADDY_DATA_VOLUME"],
            deploy["BIBITASKS_LOADTEST_CADDY_CONFIG_VOLUME"],
        ]
        self.assertEqual(len(resources), len(set(resources)))
        self.assertTrue(all(value.startswith("bibitasks_loadtest_" + COMMIT[:12]) for value in resources))
        repository = Path(__file__).resolve().parents[1]
        data = self.root / "runtime-data"
        data.mkdir()
        process_env = {**os.environ, **stage, "DATA_DIR": str(data)}
        imported = subprocess.run(
            [sys.executable, "-c", (
                "import main; main._validate_update_receiver_config(); "
                "assert main.PILOT_LOAD_TEST_ENABLED; print('LOADTEST_CONFIG_OK')"
            )],
            cwd=repository, env=process_env, capture_output=True, text=True,
            timeout=30,
        )
        self.assertEqual(
            imported.returncode, 0,
            f"stdout:\n{imported.stdout}\nstderr:\n{imported.stderr}",
        )
        self.assertIn("LOADTEST_CONFIG_OK", imported.stdout)
        self.assertNotIn(TOKEN, imported.stdout + imported.stderr)

    def test_output_is_exclusive_and_outside_repository(self):
        bootstrap.build_bundle(self.args(), api_call=self.api_call)
        with self.assertRaisesRegex(bootstrap.ConfigurationError, "already exists"):
            bootstrap.build_bundle(self.args(), api_call=self.api_call)
        repository_output = Path(__file__).resolve().parents[1] / "unsafe-load-bundle"
        with self.assertRaisesRegex(bootstrap.ConfigurationError, "outside"):
            bootstrap.build_bundle(
                self.args(output_dir=repository_output), api_call=self.api_call,
            )

    def test_domains_admin_contact_and_production_resources_fail_closed(self):
        with self.assertRaisesRegex(bootstrap.ConfigurationError, "differ"):
            bootstrap.plan(self.args(production_domain="load.tasks.example.test"))
        with self.assertRaisesRegex(bootstrap.ConfigurationError, "synthetic"):
            bootstrap.plan(self.args(admin_user_id=123456789))
        with self.assertRaisesRegex(bootstrap.ConfigurationError, "single-line"):
            bootstrap.plan(self.args(privacy_contact="bad$EXPANSION"))
        with self.assertRaisesRegex(bootstrap.ConfigurationError, "network"):
            bootstrap.plan(self.args(production_network="bad network"))

    def test_two_live_bot_ids_must_be_distinct_and_staging_name_confirmed(self):
        def same_id(token, _method, _params=None):
            username = "BibiLoadTestBot" if token == TOKEN else "BbGalterbot"
            return {"id": 42, "is_bot": True, "username": username}

        with self.assertRaisesRegex(bootstrap.ConfigurationError, "IDs must differ"):
            bootstrap.build_bundle(self.args(), api_call=same_id)
        with self.assertRaisesRegex(bootstrap.ConfigurationError, "confirmed bot"):
            bootstrap.build_bundle(
                self.args(bot_username="DifferentLoadBot"), api_call=self.api_call,
            )

    def test_token_files_and_values_must_be_independent(self):
        with self.assertRaisesRegex(bootstrap.ConfigurationError, "files must differ"):
            bootstrap.build_bundle(
                self.args(production_bot_token_file=self.token), api_call=self.api_call,
            )
        self.production_token.write_text(TOKEN + "\n", encoding="utf-8")
        if os.name != "nt":
            self.production_token.chmod(0o600)
        with self.assertRaisesRegex(bootstrap.ConfigurationError, "tokens must differ"):
            bootstrap.build_bundle(self.args(), api_call=self.api_call)


if __name__ == "__main__":
    unittest.main()
