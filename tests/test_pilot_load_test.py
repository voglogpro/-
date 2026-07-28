import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qsl
from unittest.mock import patch

from aiogram.types import Update
from PIL import Image
from scripts import pilot_load_test as load


def arguments(**overrides):
    values = {
        "base_url": "https://staging.example.test",
        "health_base_url": load.INTERNAL_HEALTH_BASE_URL,
        "apply": False,
        "confirm_base_url": "",
        "bot_token_file": None,
        "health_token_file": None,
        "webhook_secret_file": None,
        "secrets_from_environment": False,
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
    @staticmethod
    def healthy_sample(rss=100 * 1024 * 1024, **overrides):
        sample = {
            "status": 200,
            "ok": True,
            "telegram_stub": True,
            "database": True,
            "database_error": "",
            "database_locked_errors": 0,
            "storage_writable": True,
            "receiver_ready": True,
            "lifecycle_worker_alive": True,
            "outbox_worker_alive": True,
            "telegram_inbox_worker_alive": True,
            "rss": rss,
            "inbox_pending": 0,
            "inbox_dead": 0,
            "outbox_pending": 0,
            "outbox_dead": 0,
        }
        sample.update(overrides)
        return sample

    @staticmethod
    def healthy_payload(**overrides):
        payload = {
            "ok": True,
            "environment": "staging",
            "pilot_load_test_enabled": True,
            "pilot_load_test_telegram_stub_enabled": True,
            "process_rss_bytes": 100 * 1024 * 1024,
            "application_version": "v-test",
            "database": True,
            "database_error": "",
            "database_locked_errors": 0,
            "storage_writable": True,
            "telegram_receiver_ready": True,
            "lifecycle_worker_alive": True,
            "outbox_worker_alive": True,
            "telegram_inbox_worker_alive": True,
            "telegram_inbox_pending": 0,
            "telegram_inbox_dead": 0,
            "outbox_pending": 0,
            "outbox_dead": 0,
        }
        payload.update(overrides)
        return payload

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

    def test_apply_requires_exact_internal_health_transport(self):
        complete = arguments(
            apply=True,
            confirm_base_url="https://staging.example.test",
            health_base_url="http://127.0.0.1:3000",
            secrets_from_environment=True,
            webhook_path="/telegram/webhook/" + "r" * 40,
            admin_user_id=4_400_000_000_000_000,
        )
        with self.assertRaisesRegex(load.ConfigurationError, "exactly"):
            load.validate_args(complete)

    def test_environment_and_file_secret_sources_are_mutually_exclusive(self):
        complete = arguments(
            apply=True,
            confirm_base_url="https://staging.example.test",
            secrets_from_environment=True,
            bot_token_file="bot", health_token_file="health",
            webhook_secret_file="webhook",
            webhook_path="/telegram/webhook/" + "r" * 40,
            admin_user_id=4_400_000_000_000_000,
        )
        with self.assertRaisesRegex(load.ConfigurationError, "mutually exclusive"):
            load.validate_args(complete)
        with patch.dict(os.environ, {
            "BOT_TOKEN": "bot-secret", "HEALTH_TOKEN": "health-secret",
            "WEBHOOK_SECRET": "webhook-secret",
        }, clear=False):
            self.assertEqual(load._read_environment_secret("BOT_TOKEN"), "bot-secret")

    def test_photo_fixture_is_deterministic_realistic_jpeg(self):
        first = load.synthetic_photo_data_url()
        second = load.synthetic_photo_data_url()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("data:image/jpeg;base64,"))
        raw = base64.b64decode(first.split(",", 1)[1], validate=True)
        self.assertGreaterEqual(len(raw), 700_000)
        self.assertLessEqual(len(raw), 2_500_000)
        with Image.open(io.BytesIO(raw)) as image:
            self.assertGreaterEqual(image.width, 1280)
            self.assertGreaterEqual(image.height, 960)

    def test_first_open_fetches_shell_before_state_and_shell_failure_is_logical(self):
        run = load.LoadRun(arguments(), None, "token", "health", "webhook")
        calls = []

        async def request(scenario, method, path, **kwargs):
            calls.append((scenario, method, path, kwargs))
            return 200, {"ok": True}

        run.request = request
        status, _payload = asyncio.run(run.first_open(123))
        self.assertEqual(status, 200)
        self.assertEqual([item[2] for item in calls], ["/", "/api/state"])
        self.assertFalse(calls[0][3]["expect_json"])

        async def failed_shell(*_args, **_kwargs):
            return 503, {"error": "edge_busy"}

        run.request = failed_shell
        status, payload = asyncio.run(run.first_open(123))
        self.assertEqual((status, payload["error"]), (503, "edge_busy"))

    def test_webhook_fixture_has_no_outbound_handler_and_retries_same_update(self):
        run = load.LoadRun(arguments(), None, "token", "health", "webhook")
        captured = {}

        async def request(_scenario, _method, _path, **kwargs):
            captured.update(kwargs)
            return 200, {"ok": True}

        run.request = request
        asyncio.run(run.webhook(7, 0))
        body = captured["body"]
        self.assertIn("poll_answer", body)
        self.assertNotIn("message", body)
        self.assertEqual(body["poll_answer"]["option_ids"], [0])
        self.assertEqual(
            body["poll_answer"]["option_persistent_ids"], ["capacity-option-0"],
        )
        self.assertEqual(Update.model_validate(body).update_id, body["update_id"])
        self.assertEqual(captured["retries"], 40)

    def test_health_uses_fresh_internal_readiness_and_preflight_is_fail_closed(self):
        run = load.LoadRun(arguments(), None, "token", "health", "webhook")
        captured = {}

        async def request(_scenario, _method, path, **kwargs):
            captured.update(path=path, **kwargs)
            return 200, self.healthy_payload()

        run.request = request
        asyncio.run(run.preflight())
        self.assertEqual(captured["path"], "/health/ready?refresh=1")
        self.assertEqual(captured["base_url"], load.INTERNAL_HEALTH_BASE_URL)

        async def unavailable():
            return 503, self.healthy_payload(ok=False)

        run.health = unavailable
        with self.assertRaisesRegex(load.ConfigurationError, "HTTP 503"):
            asyncio.run(run.preflight())

        async def dead_queue():
            return 200, self.healthy_payload(telegram_inbox_dead=1)

        run.health = dead_queue
        with self.assertRaisesRegex(load.ConfigurationError, "not healthy"):
            asyncio.run(run.preflight())

        async def stale_fixture():
            return 200, self.healthy_payload(outbox_pending=1)

        run.health = stale_fixture
        with self.assertRaisesRegex(load.ConfigurationError, "must be empty"):
            asyncio.run(run.preflight())

    def test_queue_recovery_requires_a_fresh_fully_drained_snapshot(self):
        run = load.LoadRun(arguments(), None, "token", "health", "webhook")

        async def drained():
            return 200, self.healthy_payload()

        run.health = drained
        recovered, seconds = asyncio.run(run.wait_for_queue_recovery())
        self.assertTrue(recovered)
        self.assertGreaterEqual(seconds, 0)
        self.assertEqual(run.final_health["inbox_pending"], 0)
        self.assertEqual(run.final_health["outbox_pending"], 0)

    def test_dry_run_plan_contains_the_full_required_workload(self):
        plan = load.build_plan(arguments(
            first_opens=100, applications=50, photo_reports=10,
            webhook_rate=20, webhook_seconds=5,
            ))
        with self.assertRaisesRegex(load.ConfigurationError, "derives the webhook"):
            load.validate_args(arguments(
                apply=True,
                confirm_base_url="https://staging.example.test",
                secrets_from_environment=True,
                webhook_path="/telegram/webhook/must-not-enter-process-list",
                admin_user_id=4_400_000_000_000_000,
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

        run.health_samples = [self.healthy_sample()]
        run.final_health = self.healthy_sample()
        complete = run.report(logical, True, 0.1)
        self.assertTrue(complete["ok"])
        self.assertEqual(complete["metrics"]["webhook_p95_ms"], 100.0)

        for unhealthy in (
            self.healthy_sample(status=503, ok=False),
            self.healthy_sample(inbox_dead=1),
            self.healthy_sample(outbox_dead=1),
            self.healthy_sample(database=False, database_error="OperationalError"),
            self.healthy_sample(telegram_inbox_worker_alive=False),
        ):
            run.health_samples = [unhealthy]
            run.final_health = self.healthy_sample()
            failed = run.report(logical, True, 0.1)
            self.assertFalse(failed["ok"], unhealthy)
            self.assertFalse(failed["checks"]["health_remained_ready"])

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
