from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from scripts.backup_scheduler import cadence_delay, check_status, utc_now, write_status


class BackupSchedulerTests(unittest.TestCase):
    def test_backup_duration_is_not_added_to_fixed_cadence(self):
        self.assertEqual(cadence_delay(100.0, 600, now=250.0), 450.0)
        self.assertEqual(cadence_delay(100.0, 600, now=750.0), 0.0)

    def test_atomic_status_is_healthy_inside_rpo_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "backup-status.json"
            write_status(
                status,
                last_success_at=utc_now().isoformat(),
                attempted_at=utc_now().isoformat(),
                destination="20260728T000000.000000Z",
                consecutive_failures=0,
                error=None,
            )
            check_status(status, 900)
            self.assertEqual(json.loads(status.read_text("utf-8"))["error"], None)

    def test_stale_backup_fails_health_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "backup-status.json"
            write_status(
                status,
                last_success_at=(utc_now() - timedelta(hours=2)).isoformat(),
                attempted_at=utc_now().isoformat(),
                destination="old",
                consecutive_failures=1,
                error="OSError",
            )
            with self.assertRaisesRegex(RuntimeError, "older than the RPO"):
                check_status(status, 900)

    def test_latest_failed_attempt_fails_health_before_rpo_is_missed(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "backup-status.json"
            write_status(
                status,
                last_success_at=utc_now().isoformat(),
                attempted_at=utc_now().isoformat(),
                destination=None,
                consecutive_failures=1,
                error="OSError",
            )
            with self.assertRaisesRegex(RuntimeError, "most recent backup attempt failed"):
                check_status(status, 900)
