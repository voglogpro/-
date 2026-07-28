#!/usr/bin/env python3
"""Destructive, staging-only capacity gate for the BibiTasks pilot.

The command is dry-run by default.  An applied run creates synthetic members,
applications, tasks, evidence and Telegram updates, so it must target a
disposable staging copy whose database and media bucket will be destroyed
after the report is saved.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import io
import json
import math
import os
import random
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import aiohttp
from PIL import Image


DEFAULT_FIRST_OPENS = 100
DEFAULT_APPLICATIONS = 50
DEFAULT_APPLICATION_WINDOW_SEC = 60.0
DEFAULT_PHOTO_REPORTS = 10
DEFAULT_WEBHOOK_RATE = 20.0
DEFAULT_WEBHOOK_SECONDS = 5.0
DEFAULT_MEMORY_LIMIT_BYTES = 600 * 1024 * 1024
DEFAULT_WEBHOOK_P95_MS = 500.0
SYNTHETIC_USER_ID_START = 3_900_000_000_000_000
INTERNAL_HEALTH_BASE_URL = "http://bibitasks:3000"


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Sample:
    scenario: str
    status: int
    latency_ms: float
    error: str = ""
    retryable: bool = False


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(len(ordered) * quantile))
    return round(ordered[rank - 1], 3)


def signed_init_data(bot_token: str, user_id: int, *, query_id: str) -> str:
    user = json.dumps(
        {
            "id": int(user_id),
            "is_bot": False,
            "first_name": "Capacity",
            "last_name": "Fixture",
            "username": f"capacity_{user_id}",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    values = {
        "auth_date": str(int(time.time())),
        "query_id": query_id,
        "user": user,
    }
    data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256,
    ).digest()
    values["hash"] = hmac.new(
        secret, data_check.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    return urlencode(values)


@lru_cache(maxsize=1)
def synthetic_photo_data_url() -> str:
    """Return a deterministic phone-sized fixture that exercises JPEG work."""
    width, height = 1280, 960
    pixels = random.Random(0xB1B1CAFE).randbytes(width * height * 3)
    image = Image.frombytes("RGB", (width, height), pixels)
    output = io.BytesIO()
    image.save(
        output, format="JPEG", quality=88, optimize=True, progressive=True,
    )
    return "data:image/jpeg;base64," + base64.b64encode(
        output.getvalue()
    ).decode("ascii")


def _read_secret(path: str, label: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(f"cannot read {label} file") from exc
    if not value:
        raise ConfigurationError(f"{label} file is empty")
    return value


def _read_environment_secret(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ConfigurationError(f"environment variable {name} is empty")
    return value


def write_report_exclusive(path: str, rendered: str) -> Path:
    target = Path(path).resolve(strict=False)
    repository = Path(__file__).resolve().parents[1]
    if target == repository or repository in target.parents:
        raise ConfigurationError("load report must be written outside the repository")
    if not target.parent.is_dir():
        raise ConfigurationError("load report parent directory does not exist")
    try:
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ConfigurationError("load report already exists") from exc
    except OSError as exc:
        raise ConfigurationError("cannot create load report") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as report:
            report.write(rendered)
            report.write("\n")
            report.flush()
            os.fsync(report.fileno())
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return target


def validate_target(base_url: str, confirmation: str) -> str:
    normalized = base_url.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path:
        raise ConfigurationError("base URL must be an HTTPS origin without a path")
    if normalized != confirmation.rstrip("/"):
        raise ConfigurationError("--confirm-base-url must exactly match --base-url")
    return normalized


def build_plan(args) -> dict:
    return {
        "mode": "apply" if args.apply else "dry_run",
        "target": args.base_url.rstrip("/"),
        "health_target": args.health_base_url,
        "secret_source": (
            "environment" if args.secrets_from_environment else "files"
        ),
        "staging_only": True,
        "destructive_fixture": True,
        "first_opens": args.first_opens,
        "applications": args.applications,
        "application_window_seconds": args.application_window_seconds,
        "photo_reports": args.photo_reports,
        "photos_per_report": 4,
        "webhook_rate_per_second": args.webhook_rate,
        "webhook_seconds": args.webhook_seconds,
        "expected_webhook_updates": math.ceil(
            args.webhook_rate * args.webhook_seconds
        ),
        "pass_limits": {
            "webhook_p95_ms": args.webhook_p95_ms,
            "process_rss_bytes": args.memory_limit_bytes,
            "queue_drain_seconds": args.queue_drain_seconds,
        },
        "operator_warning": (
            "Use a disposable staging database/media bucket and destroy them "
            "after the run. Never point this command at production."
        ),
    }


class LoadRun:
    def __init__(
        self, args, session: aiohttp.ClientSession, bot_token: str,
        health_token: str, webhook_secret: str,
    ):
        self.args = args
        self.session = session
        self.bot_token = bot_token
        self.health_token = health_token
        self.webhook_secret = webhook_secret
        self.base_url = args.base_url.rstrip("/")
        self.health_base_url = args.health_base_url
        self.run_id = str(uuid.uuid4())
        self.samples: list[Sample] = []
        self.setup_samples: list[Sample] = []
        self.health_samples: list[dict] = []
        self.final_health: dict = {}
        self.stop_health = asyncio.Event()
        self.photo = synthetic_photo_data_url()

    def auth(self, user_id: int, suffix: str) -> dict[str, str]:
        init_data = signed_init_data(
            self.bot_token, user_id,
            query_id=f"{self.run_id}:{suffix}:{uuid.uuid4()}",
        )
        return {"Authorization": f"tma {init_data}"}

    async def request(
        self, scenario: str, method: str, path: str, *,
        headers: dict[str, str] | None = None, body: dict | None = None,
        retries: int = 0, measured: bool = True, base_url: str | None = None,
        expect_json: bool = True,
    ) -> tuple[int, dict]:
        target = self.samples if measured else self.setup_samples
        for attempt in range(retries + 1):
            started = time.perf_counter()
            error = ""
            payload: dict = {}
            status = 0
            response_headers = {}
            try:
                async with self.session.request(
                    method, (base_url or self.base_url) + path,
                    headers=headers, json=body,
                ) as response:
                    status = response.status
                    response_headers = response.headers
                    raw = await response.text()
                    if expect_json:
                        try:
                            decoded = json.loads(raw) if raw else {}
                            payload = decoded if isinstance(decoded, dict) else {}
                        except json.JSONDecodeError:
                            error = "non_json_response"
                        if not error:
                            error = str(payload.get("error") or "")
                    if "database is locked" in raw.casefold():
                        error = "database_is_locked"
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                error = type(exc).__name__
            latency = (time.perf_counter() - started) * 1000
            retryable = status in {429, 503} or status == 0
            target.append(Sample(scenario, status, latency, error, retryable))
            if not retryable or attempt >= retries:
                return status, payload
            retry_after = response_headers.get("Retry-After", "1")
            try:
                delay = max(0.05, min(3.0, float(retry_after)))
            except ValueError:
                delay = 1.0
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def health(self) -> tuple[int, dict]:
        return await self.request(
            "health", "GET", "/health/ready?refresh=1",
            headers={"X-Health-Token": self.health_token}, measured=False,
            base_url=self.health_base_url,
        )

    @staticmethod
    def healthy_snapshot(status: int, payload: dict) -> bool:
        return bool(
            status == 200
            and payload.get("ok") is True
            and payload.get("pilot_load_test_telegram_stub_enabled") is True
            and payload.get("database") is True
            and not payload.get("database_error")
            and payload.get("database_locked_errors") == 0
            and payload.get("storage_writable") is True
            and payload.get("telegram_receiver_ready") is True
            and payload.get("lifecycle_worker_alive") is True
            and payload.get("outbox_worker_alive") is True
            and payload.get("telegram_inbox_worker_alive") is True
            and payload.get("telegram_inbox_dead") == 0
            and payload.get("outbox_dead") == 0
        )

    async def preflight(self):
        status, payload = await self.health()
        if status != 200:
            raise ConfigurationError(f"health endpoint returned HTTP {status}")
        if payload.get("environment") != "staging":
            raise ConfigurationError(
                "load test requires health.environment=staging"
            )
        if payload.get("pilot_load_test_enabled") is not True:
            raise ConfigurationError(
                "staging must explicitly set PILOT_LOAD_TEST_ENABLED=true"
            )
        if not isinstance(payload.get("process_rss_bytes"), int):
            raise ConfigurationError(
                "staging health must expose Linux process_rss_bytes"
            )
        if not payload.get("application_version"):
            raise ConfigurationError("staging health has no application version")
        if not self.healthy_snapshot(status, payload):
            raise ConfigurationError("staging readiness is not healthy")
        if (
            payload.get("telegram_inbox_pending") != 0
            or payload.get("outbox_pending") != 0
        ):
            raise ConfigurationError("staging queues must be empty before the load test")

    async def _expect_ok(self, *args, **kwargs) -> dict:
        status, payload = await self.request(*args, measured=False, **kwargs)
        if status != 200 or not payload.get("ok"):
            raise RuntimeError(
                f"fixture setup failed for {args[0]}: HTTP {status} "
                f"{payload.get('error', '')}"
            )
        return payload

    async def prepare_photo_fixtures(self) -> list[tuple[int, int, int]]:
        admin_headers = self.auth(self.args.admin_user_id, "admin")
        await self._expect_ok(
            "setup_admin_state", "GET", "/api/state", headers=admin_headers,
        )
        fixtures = []
        for offset in range(self.args.photo_reports):
            user_id = self.args.user_id_start + offset
            user_headers = self.auth(user_id, f"worker-{offset}")
            await self._expect_ok(
                "setup_worker_state", "GET", "/api/state", headers=user_headers,
            )
            await self._expect_ok(
                "setup_worker_apply", "POST", "/api/apply", headers=user_headers,
                body={
                    "name": f"Capacity Worker {offset}",
                    "city": "Москва",
                    "about": "Синтетический профиль нагрузочного теста",
                },
            )
            await self._expect_ok(
                "setup_worker_approve", "POST", "/api/admin/decide",
                headers=admin_headers,
                body={"user_id": user_id, "decision": "approve", "note": ""},
            )
            task = await self._expect_ok(
                "setup_task_create", "POST", "/api/admin/task/create",
                headers=admin_headers,
                body={
                    "operation_id": str(uuid.uuid4()),
                    "type": "photo_check",
                    "title": f"Capacity photo report {offset}",
                    "details": "Disposable staging capacity fixture",
                    "address": f"Тестовая точка {offset}",
                    "city": "Москва",
                    "reward": 1,
                    "assigned_to": user_id,
                    "repeatable": False,
                    "evidence_policy": "after_required",
                    "announce": False,
                },
            )
            claimed = await self._expect_ok(
                "setup_task_claim", "POST", "/api/tasks/claim",
                headers=user_headers, body={"task_id": task["task_id"]},
            )
            fixtures.append((user_id, int(task["task_id"]), int(claimed["assignment_id"])))
        return fixtures

    async def first_open(self, user_id: int):
        shell_status, shell_payload = await self.request(
            "first_open_shell", "GET", "/", retries=3, expect_json=False,
        )
        if shell_status != 200:
            return shell_status, shell_payload
        return await self.request(
            "first_open", "GET", "/api/state",
            headers=self.auth(user_id, "first-open"), retries=3,
        )

    async def application(self, user_id: int, delay: float):
        await asyncio.sleep(delay)
        return await self.request(
            "application", "POST", "/api/apply",
            headers=self.auth(user_id, "application"),
            body={
                "name": f"Capacity Applicant {user_id % 1000}",
                "city": "Москва",
                "about": "Синтетическая анкета нагрузочного теста",
            },
            retries=3,
        )

    async def photo_report(self, fixture: tuple[int, int, int]):
        user_id, task_id, assignment_id = fixture
        operation_id = str(uuid.uuid4())
        return await self.request(
            "photo_report", "POST", "/api/tasks/complete",
            headers=self.auth(user_id, "photo-report"),
            body={
                "task_id": task_id,
                "assignment_id": assignment_id,
                "operation_id": operation_id,
                "note": "Парковка проверена в нагрузочном тесте",
                "proof_photos": [
                    {"data_url": self.photo} for _ in range(4)
                ],
            },
            retries=40,
        )

    async def webhook(self, index: int, delay: float):
        await asyncio.sleep(delay)
        update_id = self.args.update_id_start + index
        user_id = self.args.user_id_start + 20_000 + index
        payload = {
            "update_id": update_id,
            "poll_answer": {
                "poll_id": f"capacity-{self.run_id}-{index}",
                "user": {
                    "id": user_id, "is_bot": False,
                    "first_name": "Capacity",
                },
                "option_ids": [0],
                "option_persistent_ids": ["capacity-option-0"],
            },
        }
        return await self.request(
            "webhook", "POST", self.args.webhook_path,
            headers={
                "X-Telegram-Bot-Api-Secret-Token": self.webhook_secret,
            },
            body=payload, retries=40,
        )

    @staticmethod
    def health_sample(status: int, payload: dict) -> dict:
        return {
            "status": status,
            "ok": payload.get("ok"),
            "telegram_stub": payload.get("pilot_load_test_telegram_stub_enabled"),
            "database": payload.get("database"),
            "database_error": payload.get("database_error"),
            "database_locked_errors": payload.get("database_locked_errors"),
            "storage_writable": payload.get("storage_writable"),
            "receiver_ready": payload.get("telegram_receiver_ready"),
            "lifecycle_worker_alive": payload.get("lifecycle_worker_alive"),
            "outbox_worker_alive": payload.get("outbox_worker_alive"),
            "telegram_inbox_worker_alive": payload.get("telegram_inbox_worker_alive"),
            "rss": payload.get("process_rss_bytes"),
            "inbox_pending": payload.get("telegram_inbox_pending"),
            "inbox_dead": payload.get("telegram_inbox_dead"),
            "outbox_pending": payload.get("outbox_pending"),
            "outbox_dead": payload.get("outbox_dead"),
        }

    async def health_sampler(self):
        while not self.stop_health.is_set():
            status, payload = await self.health()
            self.health_samples.append(self.health_sample(status, payload))
            try:
                await asyncio.wait_for(self.stop_health.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def wait_for_queue_recovery(self) -> tuple[bool, float]:
        started = time.monotonic()
        while time.monotonic() - started <= self.args.queue_drain_seconds:
            status, payload = await self.health()
            snapshot = self.health_sample(status, payload)
            self.final_health = snapshot
            inbox = payload.get("telegram_inbox_pending")
            outbox = payload.get("outbox_pending")
            if (
                self.healthy_snapshot(status, payload)
                and inbox == 0 and outbox == 0
            ):
                return True, round(time.monotonic() - started, 3)
            await asyncio.sleep(2)
        return False, round(time.monotonic() - started, 3)

    async def run(self) -> dict:
        await self.preflight()
        fixtures = await self.prepare_photo_fixtures()
        fresh_start = self.args.user_id_start + 1_000
        monitor = asyncio.create_task(self.health_sampler())
        try:
            first_open_tasks = [
                asyncio.create_task(self.first_open(fresh_start + index))
                for index in range(self.args.first_opens)
            ]
            spacing = (
                self.args.application_window_seconds / self.args.applications
                if self.args.applications else 0
            )
            application_tasks = [
                asyncio.create_task(self.application(
                    fresh_start + index, spacing * index,
                ))
                for index in range(self.args.applications)
            ]
            photo_tasks = [
                asyncio.create_task(self.photo_report(fixture))
                for fixture in fixtures
            ]
            webhook_count = math.ceil(
                self.args.webhook_rate * self.args.webhook_seconds
            )
            webhook_tasks = [
                asyncio.create_task(self.webhook(
                    index, index / self.args.webhook_rate,
                ))
                for index in range(webhook_count)
            ]
            logical_results = await asyncio.gather(
                *first_open_tasks, *application_tasks,
                *photo_tasks, *webhook_tasks,
            )
        finally:
            self.stop_health.set()
            await monitor
        queue_recovered, queue_recovery_sec = await self.wait_for_queue_recovery()
        return self.report(logical_results, queue_recovered, queue_recovery_sec)

    def report(
        self, logical_results: list[tuple[int, dict]],
        queue_recovered: bool, queue_recovery_sec: float,
    ) -> dict:
        plan = build_plan(self.args)
        expected = {
            "first_open": self.args.first_opens,
            "application": self.args.applications,
            "photo_report": self.args.photo_reports,
            "webhook": plan["expected_webhook_updates"],
        }
        final_index = 0
        final_statuses = {}
        for scenario, count in expected.items():
            statuses = [
                logical_results[final_index + offset][0] for offset in range(count)
            ]
            final_statuses[scenario] = statuses
            final_index += count
        webhook_latencies = [
            sample.latency_ms for sample in self.samples
            if sample.scenario == "webhook"
        ]
        maximum_rss = max(
            (
                int(item["rss"]) for item in self.health_samples
                if isinstance(item.get("rss"), int)
            ),
            default=None,
        )
        database_locked = any(
            sample.error == "database_is_locked" for sample in self.samples
        )
        health_evidence = [*self.health_samples]
        if self.final_health:
            health_evidence.append(self.final_health)
        health_evidence_ok = bool(health_evidence) and all(
            item.get("status") == 200
            and item.get("ok") is True
            and item.get("telegram_stub") is True
            and item.get("database") is True
            and not item.get("database_error")
            and item.get("database_locked_errors") == 0
            and item.get("storage_writable") is True
            and item.get("receiver_ready") is True
            and item.get("lifecycle_worker_alive") is True
            and item.get("outbox_worker_alive") is True
            and item.get("telegram_inbox_worker_alive") is True
            and item.get("inbox_dead") == 0
            and item.get("outbox_dead") == 0
            for item in health_evidence
        )
        unhandled_500 = sum(sample.status == 500 for sample in self.samples)
        controlled_busy = sum(
            sample.status == 503 and sample.retryable for sample in self.samples
        )
        webhook_p95 = percentile(webhook_latencies, 0.95)
        checks = {
            "all_logical_requests_succeeded": all(
                all(status == 200 for status in statuses)
                for statuses in final_statuses.values()
            ),
            "no_database_locked": not database_locked,
            "no_unhandled_http_500": unhandled_500 == 0,
            "webhook_p95_within_limit": (
                webhook_p95 is not None
                and webhook_p95 <= self.args.webhook_p95_ms
            ),
            "memory_within_limit": (
                maximum_rss is not None
                and maximum_rss < self.args.memory_limit_bytes
            ),
            "health_remained_ready": health_evidence_ok,
            "queues_fully_drained": queue_recovered,
        }
        return {
            "report_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "target": self.base_url,
            "ok": all(checks.values()),
            "plan": plan,
            "checks": checks,
            "metrics": {
                "logical_final_statuses": {
                    key: {
                        "count": len(values),
                        "http_200": sum(value == 200 for value in values),
                    }
                    for key, values in final_statuses.items()
                },
                "request_attempts": len(self.samples),
                "controlled_busy_retries": controlled_busy,
                "unhandled_http_500": unhandled_500,
                "webhook_p95_ms": webhook_p95,
                "max_process_rss_bytes": maximum_rss,
                "queue_recovery_seconds": queue_recovery_sec,
            },
            "failures": [
                asdict(sample) for sample in self.samples
                if sample.status not in {200, 429, 503} or sample.error == "database_is_locked"
            ][:50],
            "cleanup_required": (
                "Destroy the disposable staging database and media bucket; "
                "this report does not delete fixtures."
            ),
        }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Staging-only BibiTasks pilot capacity gate (dry-run by default)",
    )
    result.add_argument("--base-url", default="https://staging.example.invalid")
    result.add_argument("--health-base-url", default=INTERNAL_HEALTH_BASE_URL)
    result.add_argument("--apply", action="store_true")
    result.add_argument("--confirm-base-url", default="")
    result.add_argument("--bot-token-file")
    result.add_argument("--health-token-file")
    result.add_argument("--webhook-secret-file")
    result.add_argument("--secrets-from-environment", action="store_true")
    result.add_argument("--webhook-path", default="")
    result.add_argument("--admin-user-id", type=int)
    result.add_argument("--user-id-start", type=int, default=SYNTHETIC_USER_ID_START)
    result.add_argument("--update-id-start", type=int, default=1_900_000_000)
    result.add_argument("--first-opens", type=int, default=DEFAULT_FIRST_OPENS)
    result.add_argument("--applications", type=int, default=DEFAULT_APPLICATIONS)
    result.add_argument(
        "--application-window-seconds", type=float,
        default=DEFAULT_APPLICATION_WINDOW_SEC,
    )
    result.add_argument("--photo-reports", type=int, default=DEFAULT_PHOTO_REPORTS)
    result.add_argument("--webhook-rate", type=float, default=DEFAULT_WEBHOOK_RATE)
    result.add_argument("--webhook-seconds", type=float, default=DEFAULT_WEBHOOK_SECONDS)
    result.add_argument(
        "--memory-limit-bytes", type=int, default=DEFAULT_MEMORY_LIMIT_BYTES,
    )
    result.add_argument("--webhook-p95-ms", type=float, default=DEFAULT_WEBHOOK_P95_MS)
    result.add_argument("--queue-drain-seconds", type=float, default=300.0)
    result.add_argument("--report")
    return result


def validate_args(args):
    for name in ("first_opens", "applications", "photo_reports"):
        if getattr(args, name) < 1:
            raise ConfigurationError(f"--{name.replace('_', '-')} must be positive")
    for name in (
        "application_window_seconds", "webhook_rate", "webhook_seconds",
        "webhook_p95_ms", "queue_drain_seconds",
    ):
        if getattr(args, name) <= 0:
            raise ConfigurationError(f"--{name.replace('_', '-')} must be positive")
    if not 1 <= args.user_id_start <= 4_503_599_627_370_000:
        raise ConfigurationError("synthetic user IDs must stay inside Telegram's 52-bit range")
    if args.apply:
        validate_target(args.base_url, args.confirm_base_url)
        if args.health_base_url != INTERNAL_HEALTH_BASE_URL:
            raise ConfigurationError(
                "--health-base-url must be exactly http://bibitasks:3000"
            )
        secret_files = {
            "--bot-token-file": args.bot_token_file,
            "--health-token-file": args.health_token_file,
            "--webhook-secret-file": args.webhook_secret_file,
        }
        if args.secrets_from_environment and any(secret_files.values()):
            raise ConfigurationError(
                "--secrets-from-environment is mutually exclusive with secret files"
            )
        if args.secrets_from_environment and args.webhook_path:
            raise ConfigurationError(
                "--secrets-from-environment derives the webhook path internally"
            )
        required = {
            "--admin-user-id": args.admin_user_id,
        }
        if not args.secrets_from_environment:
            required.update(secret_files)
            required["--webhook-path"] = args.webhook_path
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ConfigurationError("missing apply arguments: " + ", ".join(missing))
        if not args.webhook_path.startswith("/telegram/webhook/"):
            raise ConfigurationError("--webhook-path must be the private webhook route")


async def async_main(args) -> dict:
    if args.secrets_from_environment:
        bot_token = _read_environment_secret("BOT_TOKEN")
        health_token = _read_environment_secret("HEALTH_TOKEN")
        webhook_secret = _read_environment_secret("WEBHOOK_SECRET")
        route_id = _read_environment_secret("WEBHOOK_ROUTE_ID")
        args.webhook_path = "/telegram/webhook/" + route_id
    else:
        bot_token = _read_secret(args.bot_token_file, "bot token")
        health_token = _read_secret(args.health_token_file, "health token")
        webhook_secret = _read_secret(args.webhook_secret_file, "webhook secret")
    timeout = aiohttp.ClientTimeout(total=20, connect=5)
    connector = aiohttp.TCPConnector(limit=256, ssl=True)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        return await LoadRun(
            args, session, bot_token, health_token, webhook_secret,
        ).run()


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        validate_args(args)
        if not args.apply:
            print(json.dumps(build_plan(args), ensure_ascii=False, indent=2))
            return 0
        report = asyncio.run(async_main(args))
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report:
            write_report_exclusive(args.report, rendered)
        print(rendered)
        return 0 if report["ok"] else 3
    except (ConfigurationError, RuntimeError) as exc:
        print(json.dumps({
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
