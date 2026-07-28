import hashlib
import hmac
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qsl

from scripts import pilot_load_test as load


def arguments(**overrides):
    values = {
        "base_url": "https://staging.example.test",
        "apply": False,
        "confirm_base_url": "",
        "bot_token_file": None,
        "health_token_file": None,
        "webhook_secret_file": None,
        "webhook_path": "",
        "admin_user_id": None,
        "user_id_start": load.SYNTHETIC_USER_ID_START,
        "update_id_start": 1_900_000_000,
        "first_opens": 2,
        "applications": 1,
        "application_window_seconds": 1.0,
        "photo_reports": 1,
        "webhook_rate": 2.0,
        "webhook_seconds": 1.0,
        "memory_limit_bytes": 600 * 1024 * 1024,
        "webhook_p95_ms": 500.0,
        "queue_drain_seconds": 5.0,
        "report": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class PilotLoadTestUnitTests(unittest.TestCase):
    def test_signed_init_data_matches_telegram_algorithm(self):
        token = "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        encoded = load.signed_init_data(token, 3_900_000_000_000_123, query_id="q1")
        values = dict(parse_qsl(encoded, keep_blank_values=True))
        received = values.pop("hash")
        data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(received, expected))
        self.assertLessEqual(int(time.time()) - int(values["auth_date"]), 1)
        self.assertEqual(json.loads(values["user"])["id"], 3_900_000_000_000_123)

    def test_apply_requires_exact_https_origin_and_secret_files(self):
        with self.assertRaisesRegex(load.ConfigurationError, "exactly match"):
            load.validate_target(
                "https://staging.example.test", "https://other.example.test",
            )
        with self.assertRaisesRegex(load.ConfigurationError, "HTTPS origin"):
            load.validate_target("http://staging.example.test", "http://staging.example.test")
        with self.assertRaisesRegex(load.ConfigurationError, "missing apply arguments"):
            load.validate_args(arguments(
                apply=True,
                confirm_base_url="https://staging.example.test",
            ))

    def test_dry_run_plan_contains_the_full_required_workload(self):
        plan = load.build_plan(arguments(
            first_opens=100, applications=50, photo_reports=10,
            webhook_rate=20, webhook_seconds=5,
        ))
        self.assertEqual(plan["mode"], "dry_run")
        self.assertEqual(plan["first_opens"], 100)
        self.assertEqual(plan["applications"], 50)
        self.assertEqual(plan["photos_per_report"], 4)
        self.assertEqual(plan["expected_webhook_updates"], 100)
        self.assertTrue(plan["destructive_fixture"])

    def test_report_fails_closed_without_memory_and_passes_with_all_evidence(self):
        args = arguments()
        run = load.LoadRun(args, None, "token", "health", "webhook")
        scenarios = ["first_open", "first_open", "application", "photo_report",
                     "webhook", "webhook"]
        run.samples = [load.Sample(name, 200, 100.0) for name in scenarios]
        logical = [(200, {"ok": True}) for _ in scenarios]
        missing_memory = run.report(logical, True, 0.1)
        self.assertFalse(missing_memory["ok"])
        self.assertFalse(missing_memory["checks"]["memory_within_limit"])

        run.health_samples = [{"rss": 100 * 1024 * 1024}]
        complete = run.report(logical, True, 0.1)
        self.assertTrue(complete["ok"])
        self.assertEqual(complete["metrics"]["webhook_p95_ms"], 100.0)

    def test_percentile_uses_nearest_rank(self):
        self.assertEqual(load.percentile([1, 2, 3, 100], 0.95), 100.0)
        self.assertIsNone(load.percentile([], 0.95))

    def test_report_is_exclusive_and_cannot_be_written_inside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            target = f"{directory}/load-report.json"
            written = load.write_report_exclusive(target, '{"ok":true}')
            self.assertEqual(written.read_text("utf-8"), '{"ok":true}\n')
            with self.assertRaisesRegex(load.ConfigurationError, "already exists"):
                load.write_report_exclusive(target, '{"ok":false}')
        repository_target = load.Path(__file__).resolve().parents[1] / "load-report.json"
        with self.assertRaisesRegex(load.ConfigurationError, "outside the repository"):
            load.write_report_exclusive(str(repository_target), '{}')


if __name__ == "__main__":
    unittest.main()
