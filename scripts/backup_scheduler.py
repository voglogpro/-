"""Run verified pilot backups on a fixed cadence and expose a health check."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

try:
    from .backup import create_backup, _load_environment
except ImportError:  # Direct execution from /app/scripts.
    from backup import create_backup, _load_environment


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def write_status(path: Path, **values) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(values, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def check_status(path: Path, rpo_seconds: int) -> None:
    state = json.loads(path.read_text("utf-8"))
    if not state.get("last_success_at"):
        raise RuntimeError("no verified backup has completed")
    last_success = datetime.fromisoformat(state["last_success_at"])
    age = (utc_now() - last_success).total_seconds()
    if age > rpo_seconds:
        raise RuntimeError("latest verified backup is older than the RPO health window")
    if int(state.get("consecutive_failures") or 0) > 0:
        raise RuntimeError("the most recent backup attempt failed")


def cadence_delay(cycle_started: float, cadence_seconds: int, now: float | None = None) -> float:
    """Return start-to-start delay; backup duration is never added to cadence."""
    current = time.monotonic() if now is None else now
    return max(0.0, cycle_started + cadence_seconds - current)


def run() -> None:
    data_dir = Path(os.environ.get("BACKUP_DATA_DIR", "/app/data")).resolve()
    output_dir = Path(os.environ.get("BACKUP_OUTPUT_DIR", "/app/backups")).resolve()
    status_path = Path(os.environ.get("BACKUP_STATUS_FILE", "/tmp/backup-status.json"))
    interval = max(60, int(os.environ.get("BACKUP_INTERVAL_SECONDS", "600")))
    retry_interval = min(60, max(15, interval // 4))
    max_failures = max(1, int(os.environ.get("BACKUP_MAX_CONSECUTIVE_FAILURES", "3")))
    key_file_value = (os.environ.get("BACKUP_ENCRYPTION_KEY_FILE", "") or "").strip()
    key_version = (os.environ.get("BACKUP_ENCRYPTION_KEY_VERSION", "") or "").strip()
    plaintext_tmp_value = (os.environ.get("BACKUP_PLAINTEXT_TMP_DIR", "") or "").strip()
    if not key_file_value or not key_version or not plaintext_tmp_value:
        raise RuntimeError(
            "versioned backup encryption and memory-backed scratch are required"
        )
    key_file = Path(key_file_value)
    plaintext_tmp_dir = Path(plaintext_tmp_value)
    failures = 0
    last_success_at = None
    while True:
        cycle_started = time.monotonic()
        attempted_at = utc_now().isoformat()
        succeeded = False
        try:
            destination = create_backup(
                data_dir, output_dir, encryption_key_file=key_file,
                key_version=key_version, plaintext_tmp_dir=plaintext_tmp_dir,
            )
            last_success_at = utc_now().isoformat()
            failures = 0
            succeeded = True
            write_status(
                status_path,
                attempted_at=attempted_at,
                last_success_at=last_success_at,
                destination=destination.name,
                consecutive_failures=0,
                error=None,
            )
        except Exception as exc:
            failures += 1
            write_status(
                status_path,
                attempted_at=attempted_at,
                last_success_at=last_success_at,
                destination=None,
                consecutive_failures=failures,
                error=type(exc).__name__,
            )
            if failures >= max_failures:
                raise
        cadence = interval if succeeded else retry_interval
        time.sleep(cadence_delay(cycle_started, cadence))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    _load_environment(args.env_file)
    status_path = Path(os.environ.get("BACKUP_STATUS_FILE", "/tmp/backup-status.json"))
    rpo = max(300, int(os.environ.get("BACKUP_RPO_SECONDS", "900")))
    if args.check:
        check_status(status_path, rpo)
    else:
        run()


if __name__ == "__main__":
    main()
