import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import aiosqlite
from cryptography.fernet import Fernet


TEST_ROOT = Path(tempfile.mkdtemp(prefix="bibitasks_reliability_tests_"))
os.environ.setdefault("BOT_TOKEN", "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
os.environ.setdefault("DATA_DIR", str(TEST_ROOT))
os.environ.setdefault("WITHDRAW_ACCOUNT_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("TELEGRAM_INBOX_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("HEALTH_TOKEN", "health_" + "h" * 40)
os.environ.setdefault("MEDIA_SIGNING_KEY", "media_" + "m" * 40)
os.environ.setdefault("BIBITASKS_ENVIRONMENT", "test")

import main  # noqa: E402


class ReliabilityP1Tests(unittest.IsolatedAsyncioTestCase):
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
        main._shutdown_event.clear()

    async def asyncTearDown(self):
        main._shutdown_event.set()
        for job in tuple(main._telegram_timed_out_jobs.values()):
            job.cancel()
        if main._telegram_timed_out_jobs:
            await asyncio.gather(
                *tuple(main._telegram_timed_out_jobs.values()),
                return_exceptions=True,
            )
        main._telegram_timed_out_jobs.clear()
        main._shutdown_event.clear()

    def test_timeout_config_and_production_privacy_are_fail_closed(self):
        with patch.dict(os.environ, {"TELEGRAM_HANDLER_TIMEOUT_SEC": "9"}):
            with self.assertRaisesRegex(RuntimeError, "between 10 and 300"):
                main._bounded_int_env("TELEGRAM_HANDLER_TIMEOUT_SEC", 120, 10, 300)
        with patch.dict(os.environ, {"TELEGRAM_HANDLER_TIMEOUT_SEC": "slow"}):
            with self.assertRaisesRegex(RuntimeError, "must be an integer"):
                main._bounded_int_env("TELEGRAM_HANDLER_TIMEOUT_SEC", 120, 10, 300)
        with patch.multiple(
            main,
            BIBITASKS_ENVIRONMENT="production",
            TELEGRAM_UPDATE_MODE="webhook",
            PRIVACY_URL_RAW="",
            PRIVACY_URL=None,
        ):
            with self.assertRaisesRegex(RuntimeError, "PRIVACY_URL is required"):
                main._validate_update_receiver_config()

    def test_telegram_log_identity_is_stable_and_hides_raw_id(self):
        raw = "987654321012345678"
        first = main._telegram_log_identity(raw)
        self.assertEqual(first, main._telegram_log_identity(int(raw)))
        self.assertNotIn(raw, first)
        self.assertRegex(first, r"^tg:[0-9a-f]{16}$")

    async def test_handler_timeout_retries_safely_and_next_update_is_processed(self):
        first_started = asyncio.Event()
        second_processed = asyncio.Event()

        async def handler(_bot, payload):
            if payload["update_id"] == 91_001:
                first_started.set()
                await asyncio.Event().wait()
            second_processed.set()

        stamp = main.now_iso()
        async with aiosqlite.connect(main.DB_PATH, timeout=15) as db:
            for update_id in (91_001, 91_002):
                payload = {"update_id": update_id, "message": {"text": "/start"}}
                canonical = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
                await db.execute(
                    "INSERT INTO telegram_update_inbox "
                    "(update_id,payload_json,payload_sha256,status,attempts,"
                    "available_at,received_at) VALUES (?,?,?,'pending',0,?,?)",
                    (
                        update_id, main._encrypt_telegram_payload(canonical),
                        main._telegram_payload_fingerprint(canonical), stamp, stamp,
                    ),
                )
            await db.commit()

        with (
            patch.object(main, "TELEGRAM_HANDLER_TIMEOUT_SEC", 0.05),
            patch.object(main.dp, "feed_raw_update", side_effect=handler),
        ):
            worker = asyncio.create_task(main.telegram_inbox_worker())
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await asyncio.wait_for(second_processed.wait(), timeout=2)
            for _ in range(100):
                with sqlite3.connect(main.DB_PATH) as db:
                    rows = dict(db.execute(
                        "SELECT update_id,status FROM telegram_update_inbox"
                    ).fetchall())
                if rows.get(91_001) == "pending" and rows.get(91_002) == "done":
                    break
                await asyncio.sleep(0.01)
            main._shutdown_event.set()
            await asyncio.wait_for(worker, timeout=2)

        with sqlite3.connect(main.DB_PATH) as db:
            rows = {
                row[0]: row[1:]
                for row in db.execute(
                    "SELECT update_id,status,attempts,last_error,locked_by "
                    "FROM telegram_update_inbox ORDER BY update_id"
                )
            }
        self.assertEqual(rows[91_001], ("pending", 1, "TimeoutError", None))
        self.assertEqual(rows[91_002], ("done", 1, None, None))

    async def test_overview_keeps_all_active_corrections_plus_100_inactive(self):
        maker, recipient = 701, 702
        stamp = datetime(2026, 7, 28, tzinfo=timezone.utc)
        async with aiosqlite.connect(main.DB_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            await db.executemany(
                "INSERT INTO members "
                "(user_id,full_name,role,status,bonus,created_at) VALUES (?,?,?,?,?,?)",
                [
                    (maker, "Ответственный", "admin", "approved", 0, stamp.isoformat()),
                    (recipient, "Исполнитель", "helper", "approved", 106, stamp.isoformat()),
                ],
            )
            for number in range(105):
                created_at = (stamp + timedelta(minutes=number + 1)).isoformat()
                operation_id = f"ordinary-{number:03d}"
                cursor = await db.execute(
                    "INSERT INTO bonus_ledger "
                    "(user_id,amount,reason,created_by,created_at,operation_id,balance_after) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (recipient, 1, "Обычное начисление", maker, created_at, operation_id, number + 1),
                )
                await db.execute(
                    "INSERT INTO manual_grant_commands "
                    "(operation_id,request_hash,user_id,amount,reason,maker_id,created_at,"
                    "ledger_id,result_balance) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        operation_id, str(number) * 64, recipient, 1,
                        "Обычное начисление", maker, created_at,
                        cursor.lastrowid, number + 1,
                    ),
                )
            active_grant = str(uuid.uuid4())
            active_request = str(uuid.uuid4())
            ledger = await db.execute(
                "INSERT INTO bonus_ledger "
                "(user_id,amount,reason,created_by,created_at,operation_id,balance_after) "
                "VALUES (?,?,?,?,?,?,?)",
                (recipient, 1, "Старое начисление", maker, stamp.isoformat(), active_grant, 106),
            )
            await db.execute(
                "INSERT INTO manual_grant_commands "
                "(operation_id,request_hash,user_id,amount,reason,maker_id,created_at,"
                "ledger_id,result_balance) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    active_grant, "a" * 64, recipient, 1, "Старое начисление",
                    maker, stamp.isoformat(), ledger.lastrowid, 106,
                ),
            )
            await db.execute(
                "INSERT INTO manual_grant_reversals "
                "(grant_operation_id,original_ledger_id,user_id,amount,reason,status,"
                "requested_by,requested_at,request_operation_id,request_hash) "
                "VALUES (?,?,?,?,?,'pending',?,?,?,?)",
                (
                    active_grant, ledger.lastrowid, recipient, 1, "Ошибка начисления",
                    maker, stamp.isoformat(), active_request, "b" * 64,
                ),
            )
            await db.commit()
            rows = await main._manual_grants_for_overview_in_tx(db)

        operations = [row["operation_id"] for row in rows]
        self.assertEqual(len(rows), 101)
        self.assertEqual(operations[0], active_grant)
        self.assertEqual(sum(item.startswith("ordinary-") for item in operations), 100)
        self.assertNotIn("ordinary-000", operations)
        self.assertNotIn("ordinary-004", operations)
        self.assertIn("ordinary-104", operations)


if __name__ == "__main__":
    unittest.main()
