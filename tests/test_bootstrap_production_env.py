import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.bootstrap_production_env import (
    BootstrapConfig,
    build_environment,
    write_environment,
)


class BootstrapProductionEnvTests(unittest.TestCase):
    def config(self):
        return BootstrapConfig(
            public_base_url="https://tasks.example.test",
            group_id=-1001111111111,
            ops_group_id=-1002222222222,
            admin_ids=(101, 202),
            webapp_shortname="bibibike",
            topic_news=11,
            topic_chat=12,
            topic_work=13,
            topic_franchise=14,
            ops_topic_tasks=21,
        )

    def test_generated_production_secrets_are_independent_and_complete(self):
        token = "123456:" + "abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMN"
        values = build_environment(self.config(), token)
        secret_names = (
            "MEDIA_SIGNING_KEY", "ANALYTICS_SECRET", "WEBHOOK_ROUTE_ID",
            "WEBHOOK_SECRET", "HEALTH_TOKEN", "TELEGRAM_INBOX_KEY",
            "WITHDRAW_ACCOUNT_KEY",
        )
        self.assertEqual(len({values[name] for name in secret_names}), len(secret_names))
        self.assertEqual(values["BIBITASKS_ENVIRONMENT"], "production")
        self.assertEqual(values["TELEGRAM_UPDATE_MODE"], "webhook")
        self.assertEqual(values["DATA_DIR"], "/app/data")
        self.assertEqual(values["MINI_APP_URL"], "https://tasks.example.test/")
        self.assertEqual(values["TOPIC_WORK"], "13")
        self.assertEqual(values["ADMIN_IDS"], "101,202")
        self.assertNotIn(token, "\n".join(values[name] for name in secret_names))
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as data_dir:
            process_env = {**os.environ, **values, "DATA_DIR": data_dir}
            result = subprocess.run(
                [
                    sys.executable, "-c",
                    "import main; main._validate_update_receiver_config(); print('CONFIG_OK')",
                ],
                cwd=repo_root, env=process_env, capture_output=True, text=True,
                timeout=30,
            )
        self.assertEqual(
            result.returncode, 0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("CONFIG_OK", result.stdout)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_writer_refuses_repository_and_never_overwrites(self):
        token = "123456:" + "abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMN"
        values = build_environment(self.config(), token)
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            repository = root_path / "repo"
            repository.mkdir()
            with self.assertRaisesRegex(ValueError, "inside the repository"):
                write_environment(repository / "production.env", values, repository_root=repository)
            target = root_path / "secure" / "production.env"
            write_environment(target, values, repository_root=repository)
            self.assertTrue(target.is_file())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                write_environment(target, values, repository_root=repository)

    def test_nonstandard_https_port_is_rejected(self):
        token = "123456:" + "abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMN"
        config = BootstrapConfig(
            **{**self.config().__dict__, "public_base_url": "https://tasks.example.test:9443"}
        )
        with self.assertRaisesRegex(ValueError, "HTTPS origin"):
            build_environment(config, token)


if __name__ == "__main__":
    unittest.main()
