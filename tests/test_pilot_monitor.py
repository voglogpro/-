from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.pilot_monitor import (
    Config,
    READINESS_URL,
    backup_observation,
    check_heartbeat,
    load_config,
    load_state,
    monitor_report,
    run_cycle,
    send_alert,
)


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


class Response:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def getcode(self):
        return self.status

    def read(self, amount=-1):
        return self.payload[:amount] if amount >= 0 else self.payload


class PilotMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.backup_status = self.root / "backup-status.json"
        self.state_file = self.root / "monitor-state.json"
        self.alerts = []
        self.write_backup()
        self.config = Config(
            alert_token="123456:" + "a" * 40,
            alert_chat_id="-1001234567890",
            health_token="h" * 48,
            backup_status_file=self.backup_status,
            state_file=self.state_file,
            interval_seconds=30,
            timeout_seconds=5,
            failure_threshold=2,
            delivery_failure_threshold=3,
            reminder_seconds=900,
            backup_rpo_seconds=900,
            instance_label="pilot-1",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def write_backup(self, *, success=NOW, attempted=None, failures=0, error=None):
        self.backup_status.write_text(json.dumps({
            "last_success_at": success.isoformat(),
            "attempted_at": (attempted or success - timedelta(seconds=10)).isoformat(),
            "destination": "verified-copy",
            "consecutive_failures": failures,
            "error": error,
        }), encoding="utf-8")

    @staticmethod
    def ready(payload=None, status=200):
        body = {
            "ok": True,
            "outbox_dead": 0,
            "telegram_inbox_dead": 0,
        }
        body.update(payload or {})
        return lambda request, timeout: Response(body, status)

    def alert_opener(self, request, timeout):
        self.alerts.append(json.loads(request.data.decode("utf-8"))["text"])
        return Response({
            "ok": True,
            "result": {"message_id": len(self.alerts), "chat": {"id": int(self.config.alert_chat_id)}},
        })

    def test_config_is_fail_closed_and_secrets_come_from_read_only_files(self):
        alert = self.root / "alert-token"
        health = self.root / "health-token"
        alert.write_text(self.config.alert_token + "\n", encoding="utf-8")
        health.write_text(self.config.health_token + "\n", encoding="utf-8")
        os.chmod(alert, 0o444)
        os.chmod(health, 0o444)
        env = {
            "BIBITASKS_ENVIRONMENT": "production",
            "MONITOR_READINESS_URL": READINESS_URL,
            "MONITOR_ALERT_TOKEN_FILE": str(alert),
            "MONITOR_HEALTH_TOKEN_FILE": str(health),
            "MONITOR_BACKUP_STATUS_FILE": str(self.backup_status),
            "MONITOR_STATE_FILE": str(self.state_file),
            "MONITOR_ALERT_CHAT_ID": self.config.alert_chat_id,
            "MONITOR_INSTANCE_LABEL": "pilot-1",
            "MONITOR_INTERVAL_SECONDS": "30",
            "MONITOR_TIMEOUT_SECONDS": "5",
            "MONITOR_FAILURE_THRESHOLD": "2",
            "MONITOR_DELIVERY_FAILURE_THRESHOLD": "3",
            "MONITOR_REMINDER_SECONDS": "900",
            "BACKUP_RPO_SECONDS": "900",
        }
        loaded = load_config(env)
        self.assertEqual(loaded.health_token, self.config.health_token)
        with self.assertRaisesRegex(ValueError, "fixed internal endpoint"):
            load_config({**env, "MONITOR_READINESS_URL": "https://evil.test/"})
        with self.assertRaisesRegex(ValueError, "production"):
            load_config({**env, "BIBITASKS_ENVIRONMENT": "development"})
        os.chmod(alert, 0o644)
        with self.assertRaisesRegex(ValueError, "read-only"):
            load_config(env)

    def test_incident_is_debounced_deduplicated_then_recovers(self):
        broken = self.ready({"ok": False}, status=503)
        run_cycle(
            self.config, now=NOW, readiness_opener=broken,
            alert_opener=self.alert_opener,
        )
        self.assertEqual(self.alerts, [])
        run_cycle(
            self.config, now=NOW + timedelta(seconds=30),
            readiness_opener=broken, alert_opener=self.alert_opener,
        )
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("СБОЙ · приложение", self.alerts[0])
        run_cycle(
            self.config, now=NOW + timedelta(seconds=60),
            readiness_opener=broken, alert_opener=self.alert_opener,
        )
        self.assertEqual(len(self.alerts), 1)
        run_cycle(
            self.config, now=NOW + timedelta(seconds=90),
            readiness_opener=self.ready(), alert_opener=self.alert_opener,
        )
        self.assertEqual(len(self.alerts), 2)
        self.assertIn("ВОССТАНОВЛЕНО · приложение", self.alerts[1])
        state, recovered = load_state(self.state_file)
        self.assertFalse(recovered)
        self.assertFalse(state["checks"]["application"]["alert_active"])
        report = monitor_report(self.config, now=NOW + timedelta(seconds=90))
        check = report["checks"]["application"]
        self.assertEqual(check["last_incident_delivered_at"], (NOW + timedelta(seconds=30)).isoformat())
        self.assertEqual(check["last_recovery_delivered_at"], (NOW + timedelta(seconds=90)).isoformat())
        self.assertTrue(report["heartbeat_ok"])
        self.assertTrue(report["ok"])

    def test_dead_queues_are_a_separate_deduplicated_signal(self):
        queues = self.ready({"outbox_dead": 2, "telegram_inbox_dead": 1})
        for offset in (0, 30):
            run_cycle(
                self.config, now=NOW + timedelta(seconds=offset),
                readiness_opener=queues, alert_opener=self.alert_opener,
            )
        self.assertEqual(len(self.alerts), 1)
        self.assertNotIn("СБОЙ · приложение", self.alerts[0])
        self.assertIn("СБОЙ · очереди Telegram: outbox dead=2, inbox dead=1", self.alerts[0])

    def test_stale_or_failed_backup_alerts_even_when_app_is_ready(self):
        self.write_backup(
            success=NOW - timedelta(hours=1), failures=1, error="OSError",
        )
        observation = backup_observation(self.config, now=NOW)
        self.assertFalse(observation.healthy)
        for offset in (0, 30):
            run_cycle(
                self.config, now=NOW + timedelta(seconds=offset),
                readiness_opener=self.ready(), alert_opener=self.alert_opener,
            )
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("СБОЙ · резервное копирование", self.alerts[0])

    def test_delivery_failure_is_retried_and_never_marks_alert_delivered(self):
        def failed(request, timeout):
            raise OSError("offline")

        broken = self.ready({"ok": False}, status=503)
        run_cycle(self.config, now=NOW, readiness_opener=broken, alert_opener=failed)
        _, events, error = run_cycle(
            self.config, now=NOW + timedelta(seconds=30),
            readiness_opener=broken, alert_opener=failed,
        )
        self.assertTrue(events)
        self.assertEqual(error, "Telegram alert transport failed")
        self.assertNotIn(self.config.alert_token, error)
        state, _ = load_state(self.state_file)
        self.assertFalse(state["checks"]["application"]["alert_active"])
        self.assertTrue(state["checks"]["application"]["pending_incident"])

    def test_recovery_waits_until_telegram_confirms_delivery(self):
        broken = self.ready({"ok": False}, status=503)
        for offset in (0, 30):
            run_cycle(
                self.config, now=NOW + timedelta(seconds=offset),
                readiness_opener=broken, alert_opener=self.alert_opener,
            )

        def failed(request, timeout):
            raise OSError("offline")

        _, events, error = run_cycle(
            self.config, now=NOW + timedelta(seconds=60),
            readiness_opener=self.ready(), alert_opener=failed,
        )
        self.assertEqual(events[0][0], "recovery")
        self.assertIsNotNone(error)
        state, _ = load_state(self.state_file)
        self.assertTrue(state["checks"]["application"]["alert_active"])

    def test_persistent_delivery_failure_fails_health_and_report(self):
        def failed(request, timeout):
            raise OSError("offline")

        broken = self.ready({"ok": False}, status=503)
        for offset in (0, 30, 60, 90):
            run_cycle(
                self.config, now=NOW + timedelta(seconds=offset),
                readiness_opener=broken, alert_opener=failed,
            )
        state, _ = load_state(self.state_file)
        self.assertEqual(state["delivery"]["consecutive_failures"], 3)
        with self.assertRaisesRegex(RuntimeError, "persistently failing"):
            check_heartbeat(self.config, now=NOW + timedelta(seconds=90))
        report = monitor_report(self.config, now=NOW + timedelta(seconds=90))
        self.assertFalse(report["alert_delivery_ok"])
        self.assertFalse(report["ok"])

    def test_pending_incident_is_delivered_before_recovery(self):
        broken = self.ready({"ok": False}, status=503)

        def failed(request, timeout):
            raise OSError("offline")

        run_cycle(self.config, now=NOW, readiness_opener=broken, alert_opener=failed)
        run_cycle(
            self.config, now=NOW + timedelta(seconds=30),
            readiness_opener=broken, alert_opener=failed,
        )
        run_cycle(
            self.config, now=NOW + timedelta(seconds=60),
            readiness_opener=self.ready(), alert_opener=self.alert_opener,
        )
        self.assertIn("СБОЙ · приложение", self.alerts[0])
        run_cycle(
            self.config, now=NOW + timedelta(seconds=90),
            readiness_opener=self.ready(), alert_opener=self.alert_opener,
        )
        self.assertIn("ВОССТАНОВЛЕНО · приложение", self.alerts[1])

    def test_heartbeat_requires_recent_successful_cycle(self):
        run_cycle(
            self.config, now=NOW, readiness_opener=self.ready(),
            alert_opener=self.alert_opener,
        )
        check_heartbeat(self.config, now=NOW + timedelta(seconds=89))
        with self.assertRaisesRegex(RuntimeError, "stale"):
            check_heartbeat(self.config, now=NOW + timedelta(seconds=91))

    def test_telegram_error_text_does_not_contain_token(self):
        def failed(request, timeout):
            raise OSError(str(request.full_url))

        with self.assertRaisesRegex(RuntimeError, "transport failed") as captured:
            send_alert(self.config, "test", opener=failed)
        self.assertNotIn(self.config.alert_token, str(captured.exception))

    def test_corrupt_state_is_alerted_and_delivery_retried(self):
        self.state_file.write_text("not json", encoding="utf-8")

        def failed(request, timeout):
            raise OSError("offline")

        _, events, error = run_cycle(
            self.config, now=NOW, readiness_opener=self.ready(), alert_opener=failed,
        )
        self.assertEqual(events[0][1].key, "monitor_state")
        self.assertIsNotNone(error)
        _, retried, error = run_cycle(
            self.config, now=NOW + timedelta(seconds=30),
            readiness_opener=self.ready(), alert_opener=self.alert_opener,
        )
        self.assertEqual(retried[0][1].key, "monitor_state")
        self.assertIsNone(error)
        self.assertFalse(
            monitor_report(self.config, now=NOW + timedelta(seconds=30))["ok"],
        )
        run_cycle(
            self.config, now=NOW + timedelta(seconds=60),
            readiness_opener=self.ready(), alert_opener=self.alert_opener,
        )
        self.assertTrue(
            monitor_report(self.config, now=NOW + timedelta(seconds=60))["ok"],
        )

    def test_semantically_corrupt_state_is_not_trusted(self):
        self.state_file.write_text(json.dumps({
            "version": 1,
            "last_check_at": NOW.isoformat(),
            "checks": {"application": {"failures": "not-an-integer"}},
        }), encoding="utf-8")
        _, recovered = load_state(self.state_file)
        self.assertTrue(recovered)


if __name__ == "__main__":
    unittest.main()
