import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet


TEST_ROOT = Path(tempfile.mkdtemp(prefix="bibitasks_capacity_tests_"))
os.environ.setdefault(
    "BOT_TOKEN", "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
)
os.environ.setdefault("DATA_DIR", str(TEST_ROOT))
os.environ.setdefault("WITHDRAW_ACCOUNT_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("TELEGRAM_INBOX_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("HEALTH_TOKEN", "health_" + "h" * 40)
os.environ.setdefault("MEDIA_SIGNING_KEY", "media_" + "m" * 40)
os.environ.setdefault("BIBITASKS_ENVIRONMENT", "test")

import main  # noqa: E402


def webhook_payload(update_id):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": 123, "type": "private"},
            "from": {
                "id": 123, "is_bot": False, "first_name": "Тест",
            },
            "text": "/start",
        },
    }


class PilotAdmissionUnitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._api_capacity.update({
            "active_reads": 0, "active_writes": 0, "active_heavy": 0,
            "rejected_reads": 0, "rejected_writes": 0,
            "rejected_heavy": 0,
        })
        main._media_capacity.update({"active": 0, "waiters": 0, "rejected": 0})
        main._media_normalize_semaphore = None
        main._media_normalize_loop = None
        self.assertFalse(main._media_normalize_jobs)

    async def test_read_and_heavy_caps_fail_before_handler_body_work(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        called = 0

        async def blocked(_request):
            entered.set()
            await release.wait()
            return main._json({"ok": True})

        async def must_not_run(_request):
            nonlocal called
            called += 1
            return main._json({"ok": True})

        read = SimpleNamespace(path="/api/state", method="GET")
        with patch.object(main, "API_READ_INFLIGHT_MAX", 1):
            first = asyncio.create_task(main.capacity_middleware(read, blocked))
            await entered.wait()
            rejected = await main.capacity_middleware(read, must_not_run)
            self.assertEqual(rejected.status, 503)
            self.assertEqual(rejected.headers["Retry-After"], "3")
            self.assertEqual(called, 0)
            self.assertEqual(main._api_capacity["active_reads"], 1)
            release.set()
            self.assertEqual((await first).status, 200)
        self.assertEqual(main._api_capacity["active_reads"], 0)
        self.assertEqual(main._api_capacity["rejected_reads"], 1)

        entered.clear()
        release.clear()
        heavy = SimpleNamespace(path="/api/tasks/complete", method="POST")
        ordinary = SimpleNamespace(path="/api/apply", method="POST")
        with (
            patch.object(main, "API_HEAVY_INFLIGHT_MAX", 1),
            patch.object(main, "API_WRITE_INFLIGHT_MAX", 2),
        ):
            first = asyncio.create_task(main.capacity_middleware(heavy, blocked))
            await entered.wait()
            rejected = await main.capacity_middleware(heavy, must_not_run)
            admitted = await main.capacity_middleware(ordinary, must_not_run)
            self.assertEqual((rejected.status, admitted.status), (503, 200))
            self.assertEqual(called, 1)
            release.set()
            await first
        self.assertEqual(main.API_HEAVY_INFLIGHT_MAX, 4)
        self.assertEqual(main._api_capacity["active_writes"], 0)
        self.assertEqual(main._api_capacity["active_heavy"], 0)
        self.assertEqual(main._api_capacity["rejected_heavy"], 1)

    async def test_media_normalizer_has_one_active_and_only_three_waiters(self):
        release = asyncio.Event()

        async def blocked_to_thread(function, *args, **kwargs):
            await release.wait()
            return function(*args, **kwargs)

        async def run_one():
            return await main._normalize_media_bounded(lambda: "normalized")

        with (
            patch.object(main, "MEDIA_NORMALIZE_CONCURRENCY", 1),
            patch.object(main, "MEDIA_NORMALIZE_MAX_WAITERS", 3),
            patch.object(main, "MEDIA_NORMALIZE_WAIT_TIMEOUT_SEC", 5),
            patch.object(main.asyncio, "to_thread", blocked_to_thread),
        ):
            tasks = [asyncio.create_task(run_one())]
            for _ in range(100):
                if main._media_capacity["active"] == 1:
                    break
                await asyncio.sleep(0.001)
            tasks.extend(asyncio.create_task(run_one()) for _ in range(3))
            for _ in range(100):
                if main._media_capacity["waiters"] == 3:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(
                (main._media_capacity["active"], main._media_capacity["waiters"]),
                (1, 3),
            )
            with self.assertRaises(main.MediaProcessingBusy):
                await run_one()
            self.assertEqual(main._media_capacity["rejected"], 1)
            release.set()
            self.assertEqual(
                await asyncio.gather(*tasks), ["normalized"] * 4,
            )
        self.assertEqual(
            (main._media_capacity["active"], main._media_capacity["waiters"]),
            (0, 0),
        )

        async def busy(_request):
            raise main.MediaProcessingBusy("media_processing_busy")

        response = await main.error_middleware(
            SimpleNamespace(path="/api/tasks/complete", method="POST"), busy,
        )
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers["Retry-After"], "5")
        self.assertEqual(json.loads(response.text)["error"], "media_processing_busy")

    async def test_capacity_config_rejects_out_of_range_values(self):
        with patch.dict(os.environ, {"BIBITASKS_TEST_BOUND": "0"}):
            with self.assertRaisesRegex(RuntimeError, "between 1 and 4"):
                main._bounded_int_env("BIBITASKS_TEST_BOUND", 1, 1, 4)
        with patch.dict(os.environ, {"BIBITASKS_TEST_BOUND": "not-an-int"}):
            with self.assertRaisesRegex(RuntimeError, "must be an integer"):
                main._bounded_int_env("BIBITASKS_TEST_BOUND", 1, 1, 4)

    async def test_media_waiter_has_a_bounded_deadline(self):
        release = asyncio.Event()

        async def blocked_to_thread(function, *args, **kwargs):
            await release.wait()
            return function(*args, **kwargs)

        async def run_one():
            return await main._normalize_media_bounded(lambda: "normalized")

        with (
            patch.object(main, "MEDIA_NORMALIZE_CONCURRENCY", 1),
            patch.object(main, "MEDIA_NORMALIZE_MAX_WAITERS", 3),
            patch.object(main, "MEDIA_NORMALIZE_WAIT_TIMEOUT_SEC", 1),
            patch.object(main.asyncio, "to_thread", blocked_to_thread),
        ):
            active = asyncio.create_task(run_one())
            for _ in range(100):
                if main._media_capacity["active"] == 1:
                    break
                await asyncio.sleep(0.001)
            with self.assertRaises(main.MediaProcessingBusy):
                await run_one()
            self.assertEqual(main._media_capacity["rejected"], 1)
            self.assertEqual(main._media_capacity["waiters"], 0)
            release.set()
            self.assertEqual(await active, "normalized")

    async def test_cancelled_request_keeps_slot_until_normalizer_really_finishes(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def blocked_to_thread(function, *args, **kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return function(*args, **kwargs)

        async def run_one():
            return await main._normalize_media_bounded(lambda: "normalized")

        with (
            patch.object(main, "MEDIA_NORMALIZE_CONCURRENCY", 1),
            patch.object(main, "MEDIA_NORMALIZE_MAX_WAITERS", 3),
            patch.object(main, "MEDIA_NORMALIZE_WAIT_TIMEOUT_SEC", 5),
            patch.object(main.asyncio, "to_thread", blocked_to_thread),
        ):
            disconnected = asyncio.create_task(run_one())
            await started.wait()
            disconnected.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await disconnected
            self.assertEqual(main._media_capacity["active"], 1)
            queued = asyncio.create_task(run_one())
            for _ in range(100):
                if main._media_capacity["waiters"] == 1:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(calls, 1)
            self.assertEqual(main._media_capacity["waiters"], 1)
            release.set()
            self.assertEqual(await queued, "normalized")
        await asyncio.sleep(0)
        self.assertEqual(
            (main._media_capacity["active"], main._media_capacity["waiters"]),
            (0, 0),
        )
        self.assertFalse(main._media_normalize_jobs)


class TelegramBackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        case = TEST_ROOT / self.id().rsplit(".", 1)[-1]
        case.mkdir(parents=True, exist_ok=True)
        main.DB_PATH = str(case / "bibitasks.db")
        main.TASK_PHOTO_DIR = str(case / "task_photos")
        Path(main.TASK_PHOTO_DIR).mkdir(exist_ok=True)
        main.ensure_recovery_key_canary(
            case, main.TELEGRAM_INBOX_FERNET, main.WITHDRAW_FERNET,
            production=False,
        )
        await main.init_db()
        main._health_cache["checked_at"] = 0.0

    async def test_full_inbox_accepts_duplicate_but_returns_503_for_new_update(self):
        original_secret = main.WEBHOOK_SECRET
        original_rejected = main._telegram_runtime["overload_rejected"]
        main.WEBHOOK_SECRET = "secret_" + "z" * 40
        app = main.web.Application()
        app.router.add_post("/hook", main.telegram_webhook_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"X-Telegram-Bot-Api-Secret-Token": main.WEBHOOK_SECRET}
        accepted_payload = webhook_payload(71_001)
        try:
            with patch.object(main, "TELEGRAM_INBOX_HARD_LIMIT", 1):
                accepted = await client.post(
                    "/hook", json=accepted_payload, headers=headers,
                )
                overloaded = await client.post(
                    "/hook", json=webhook_payload(71_002), headers=headers,
                )
                duplicate = await client.post(
                    "/hook", json=accepted_payload, headers=headers,
                )
            self.assertEqual(
                (accepted.status, overloaded.status, duplicate.status),
                (200, 503, 200),
            )
            self.assertEqual(overloaded.headers["Retry-After"], "2")
            self.assertTrue((await duplicate.json())["duplicate"])
            with sqlite3.connect(main.DB_PATH) as db:
                ids = db.execute(
                    "SELECT update_id FROM telegram_update_inbox ORDER BY update_id"
                ).fetchall()
            self.assertEqual(ids, [(71_001,)])
            self.assertEqual(
                main._telegram_runtime["overload_rejected"],
                original_rejected + 1,
            )
        finally:
            main.WEBHOOK_SECRET = original_secret
            await client.close()

    async def test_readiness_reports_and_soft_fails_on_inbox_and_outbox_depth(self):
        stamp = main.now_iso()
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO telegram_update_inbox "
                "(update_id,payload_json,payload_sha256,status,attempts,"
                "available_at,received_at) VALUES (?,?,?,'pending',0,?,?)",
                (81_001, "encrypted", "h1:test", stamp, stamp),
            )
            db.execute(
                "INSERT INTO task_outbox "
                "(event_key,event_type,payload_json,status,attempts,available_at,"
                "created_at) VALUES (?,?,?,'pending',0,?,?)",
                ("capacity:test", "direct", "{}", stamp, stamp),
            )
            db.commit()
        request = SimpleNamespace(
            headers={"X-Health-Token": main.HEALTH_TOKEN},
            remote="203.0.113.10",
        )
        runtime = dict(main._telegram_runtime)
        main._telegram_runtime["receiver_ready"] = True
        try:
            with (
                patch.object(main, "TELEGRAM_INBOX_SOFT_LIMIT", 1),
                patch.object(main, "TELEGRAM_OUTBOX_SOFT_LIMIT", 1),
                patch.object(main, "_storage_healthcheck", AsyncMock(return_value=True)),
                patch.object(main, "_worker_alive", return_value=True),
            ):
                response = await main.api_health(request)
            payload = json.loads(response.text)
            self.assertEqual(response.status, 503)
            self.assertEqual(payload["telegram_inbox_pending"], 1)
            self.assertEqual(payload["outbox_pending"], 1)
            self.assertTrue(payload["telegram_inbox_backlogged"])
            self.assertTrue(payload["outbox_backlogged"])
            self.assertIn("active_reads", payload["api_capacity"])
            self.assertIn("rejected", payload["media_processing_capacity"])
        finally:
            main._telegram_runtime.update(runtime)


if __name__ == "__main__":
    unittest.main()
