import asyncio
import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from cryptography.fernet import Fernet
from aiohttp.test_utils import TestClient, TestServer


TEST_ROOT = Path(tempfile.mkdtemp(prefix="bibitasks_tests_"))
os.environ["BOT_TOKEN"] = "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
os.environ["DATA_DIR"] = str(TEST_ROOT)
os.environ["WITHDRAW_ACCOUNT_KEY"] = Fernet.generate_key().decode("ascii")
os.environ["TELEGRAM_INBOX_KEY"] = Fernet.generate_key().decode("ascii")
os.environ["HEALTH_TOKEN"] = "health_" + "h" * 40
os.environ["MEDIA_SIGNING_KEY"] = "media_" + "m" * 40
os.environ["BIBITASKS_ENVIRONMENT"] = "test"

import main  # noqa: E402  (environment must be set before application import)


class DummyRequest:
    def __init__(self, body, uid=None):
        self._body = body
        self.uid = uid

    async def json(self):
        return self._body


def response_json(response):
    import json
    return json.loads(response.body.decode("utf-8"))


class CoreSafetyTests(unittest.IsolatedAsyncioTestCase):
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

    async def _seed_admin(self, user_id):
        await main.upsert_member(
            user_id, full_name=f"Ответственный {user_id}",
            status="approved", role="admin",
        )

    @staticmethod
    def _join_request_event(
        user_id, invite_url, *, update_id, user_chat_id=None,
        revoked=False, creator_id=None, occurred_at=None,
    ):
        if creator_id is None:
            creator_id = main.bot.id
        return SimpleNamespace(
            chat=SimpleNamespace(
                id=-100_770_000_001, type="supergroup", username="bbbikefan",
            ),
            from_user=SimpleNamespace(
                id=user_id, full_name=f"Участник {user_id}",
                username=f"member_{user_id}", is_bot=False,
            ),
            user_chat_id=(2**51 + 12345 if user_chat_id is None else user_chat_id),
            date=occurred_at or datetime(2026, 7, 28, 9, 0, tzinfo=main.timezone.utc),
            invite_link=SimpleNamespace(
                invite_link=invite_url,
                creator=SimpleNamespace(id=creator_id),
                creates_join_request=True,
                is_revoked=revoked,
            ),
            _test_update_id=update_id,
        )

    @staticmethod
    async def _feed_join_request(request):
        token = main._current_update_id.set(request._test_update_id)
        try:
            await main.handle_chat_join_request(request)
        finally:
            main._current_update_id.reset(token)

    @staticmethod
    def _membership_update(user_id, *, occurred_at=None):
        user = SimpleNamespace(
            id=user_id, full_name=f"Участник {user_id}",
            username=f"member_{user_id}", is_bot=False,
        )
        return SimpleNamespace(
            chat=SimpleNamespace(
                id=-100_770_000_001, type="supergroup", username="bbbikefan",
            ),
            old_chat_member=SimpleNamespace(status="left", user=user),
            new_chat_member=SimpleNamespace(status="member", user=user),
            date=occurred_at or datetime(2026, 7, 28, 9, 5, tzinfo=main.timezone.utc),
        )

    async def test_join_request_handler_registers_required_update_type(self):
        self.assertIn("chat_join_request", main.dp.resolve_used_update_types())

    async def test_stale_join_request_is_declined_once_after_application_sla(self):
        user_id = 992_005_001
        request_key = "a" * 64
        await main.upsert_member(user_id, status="pending")
        requested_at = (
            main.datetime.now(main.timezone.utc)
            - main.timedelta(hours=main.JOIN_REQUEST_APPLICATION_SLA_HOURS + 1)
        ).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO telegram_join_requests "
                "(request_key,chat_id,user_id,source,status,requested_at) "
                "VALUES (?,? ,?,'bot_invite','awaiting_application',?)",
                (request_key, "-100770000001", user_id, requested_at),
            )
            db.commit()
        first = await main._expire_stale_join_requests()
        replay = await main._expire_stale_join_requests()
        with sqlite3.connect(main.DB_PATH) as db:
            row = db.execute(
                "SELECT status,decision FROM telegram_join_requests "
                "WHERE request_key=?", (request_key,),
            ).fetchone()
            commands = db.execute(
                "SELECT COUNT(*) FROM task_outbox WHERE event_key=?",
                (f"join_request:{request_key}:decline",),
            ).fetchone()[0]
            events = db.execute(
                "SELECT COUNT(*) FROM product_events "
                "WHERE event_name='group_join_request_expired'",
            ).fetchone()[0]
        self.assertEqual(first, [request_key])
        self.assertEqual(replay, [])
        self.assertEqual(row, ("decline_queued", "decline"))
        self.assertEqual((commands, events), (1, 1))

    async def test_admin_can_idempotently_retry_managed_join_decision(self):
        admin_id, user_id = 992_006_001, 992_006_002
        request_key = "b" * 64
        operation_id = str(uuid.uuid4())
        await self._seed_admin(admin_id)
        await main.upsert_member(user_id, status="approved")
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO telegram_join_requests "
                "(request_key,chat_id,user_id,source,status,requested_at,last_error) "
                "VALUES (?,? ,?,'bot_invite','manual_required',?,'TelegramBadRequest')",
                (request_key, "-100770000001", user_id, main.now_iso()),
            )
            db.commit()
        original_require = main._require_admin

        async def authorized(_request):
            return admin_id, None

        main._require_admin = authorized
        body = {
            "request_key": request_key,
            "decision": "approve",
            "reason": "Проверена заявка и членство",
            "operation_id": operation_id,
        }
        try:
            first = response_json(await main.api_admin_join_request_retry(
                DummyRequest(body, admin_id),
            ))
            replay = response_json(await main.api_admin_join_request_retry(
                DummyRequest(body, admin_id),
            ))
        finally:
            main._require_admin = original_require
        with sqlite3.connect(main.DB_PATH) as db:
            row = db.execute(
                "SELECT status,decision,manual_retry_reason,manual_retry_by "
                "FROM telegram_join_requests WHERE request_key=?", (request_key,),
            ).fetchone()
            commands = db.execute(
                "SELECT COUNT(*) FROM task_outbox WHERE event_key=?",
                (f"join_request:{request_key}:approve",),
            ).fetchone()[0]
        self.assertFalse(first["idempotent"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(row, (
            "approve_queued", "approve", body["reason"], admin_id,
        ))
        self.assertEqual(commands, 1)

    async def test_terminal_join_retry_free_text_expires_but_audit_state_remains(self):
        user_id = 992_007_001
        request_key = "c" * 64
        await main.upsert_member(user_id, status="approved")
        old = (
            main.datetime.now(main.timezone.utc)
            - main.timedelta(days=main.EVIDENCE_RETENTION_DAYS + 1)
        ).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO telegram_join_requests "
                "(request_key,chat_id,user_id,source,status,requested_at,decision,"
                "decided_at,manual_retry_reason,manual_retry_by,manual_retry_at,last_error) "
                "VALUES (?,? ,?,'bot_invite','declined',?,'decline',?, ?,?,?,?)",
                (
                    request_key, "-100770000001", user_id, old, old,
                    "Свободный комментарий скаута", user_id, old,
                    "TelegramBadRequest",
                ),
            )
            db.commit()
        await main._schedule_expired_evidence_media()
        with sqlite3.connect(main.DB_PATH) as db:
            row = db.execute(
                "SELECT status,decision,manual_retry_reason,last_error "
                "FROM telegram_join_requests WHERE request_key=?", (request_key,),
            ).fetchone()
        self.assertEqual(row, ("declined", "decline", None, None))

    async def test_managed_join_request_persists_hash_and_52_bit_user_chat_id_only(self):
        invite_url = "https://t.me/+ManagedJoinToken_1234567890"
        user_id = 992_010_001
        user_chat_id = 2**51 + 987_654_321
        names = (
            "GROUP_ID", "GROUP_USERNAME", "JOIN_REQUEST_ADMISSION_ENABLED",
            "JOIN_REQUEST_INVITE_URL",
        )
        original = {name: getattr(main, name) for name in names}
        try:
            main.GROUP_ID = -100_770_000_001
            main.GROUP_USERNAME = "bbbikefan"
            main.JOIN_REQUEST_ADMISSION_ENABLED = True
            main.JOIN_REQUEST_INVITE_URL = invite_url
            request = self._join_request_event(
                user_id, invite_url, update_id=992_010_101,
                user_chat_id=user_chat_id,
            )
            await self._feed_join_request(request)
        finally:
            for name, value in original.items():
                setattr(main, name, value)

        expected_hash = main.hashlib.sha256(invite_url.encode("utf-8")).hexdigest()
        with sqlite3.connect(main.DB_PATH) as db:
            join_row = db.execute(
                "SELECT invite_link_sha256,source,status FROM telegram_join_requests "
                "WHERE user_id=?", (user_id,),
            ).fetchone()
            direct_row = db.execute(
                "SELECT recipient_id,payload_json FROM task_outbox "
                "WHERE event_key LIKE 'join_request:%:participant'",
            ).fetchone()
            join_columns = {
                row[1] for row in db.execute("PRAGMA table_info(telegram_join_requests)")
            }
        self.assertEqual(join_row, (expected_hash, "bot_invite", "awaiting_application"))
        self.assertEqual(direct_row[0], user_chat_id)
        self.assertGreater(direct_row[0], 2**31)
        self.assertNotIn(invite_url, direct_row[1])
        self.assertIn("invite_link_sha256", join_columns)
        self.assertNotIn("invite_link", join_columns)

    async def test_unverified_or_revoked_join_link_requires_manual_review_without_approve(self):
        managed_url = "https://t.me/+ManagedJoinToken_1234567890"
        unverified_url = "https://t.me/+DifferentJoinToken_123456789"
        names = (
            "GROUP_ID", "GROUP_USERNAME", "JOIN_REQUEST_ADMISSION_ENABLED",
            "JOIN_REQUEST_INVITE_URL",
        )
        original = {name: getattr(main, name) for name in names}
        try:
            main.GROUP_ID = -100_770_000_001
            main.GROUP_USERNAME = "bbbikefan"
            main.JOIN_REQUEST_ADMISSION_ENABLED = True
            main.JOIN_REQUEST_INVITE_URL = managed_url
            await self._feed_join_request(self._join_request_event(
                992_020_001, unverified_url, update_id=992_020_101,
            ))
            await self._feed_join_request(self._join_request_event(
                992_020_002, managed_url, update_id=992_020_102,
                revoked=True,
                occurred_at=datetime(2026, 7, 28, 9, 1, tzinfo=main.timezone.utc),
            ))
        finally:
            for name, value in original.items():
                setattr(main, name, value)

        with sqlite3.connect(main.DB_PATH) as db:
            rows = db.execute(
                "SELECT user_id,source,status,decision FROM telegram_join_requests "
                "ORDER BY user_id",
            ).fetchall()
            decisions = db.execute(
                "SELECT COUNT(*) FROM task_outbox "
                "WHERE event_type='join_request_decision'",
            ).fetchone()[0]
        self.assertEqual(rows, [
            (992_020_001, "unverified", "manual_required", None),
            (992_020_002, "unverified", "manual_required", None),
        ])
        self.assertEqual(decisions, 0)

    async def test_application_approval_queues_join_but_referral_waits_for_chat_member(self):
        managed_url = "https://t.me/+ManagedJoinToken_1234567890"
        admin_id, referrer_id, applicant_id = 992_030_001, 992_030_002, 992_030_003
        await self._seed_admin(admin_id)
        await main.upsert_member(
            referrer_id, full_name="Пригласивший", status="approved",
            role="helper", bonus=0,
        )
        await main.upsert_member(
            applicant_id, full_name="Кандидат", status="pending", role="applicant",
            applied_at=main.now_iso(), referred_by=referrer_id, ref_confirmed=0,
        )
        names = (
            "GROUP_ID", "GROUP_USERNAME", "JOIN_REQUEST_ADMISSION_ENABLED",
            "JOIN_REQUEST_INVITE_URL", "REFERRAL_MILESTONES", "_require_admin",
        )
        original = {name: getattr(main, name) for name in names}

        async def authorized(_request):
            return admin_id, None

        try:
            main.GROUP_ID = -100_770_000_001
            main.GROUP_USERNAME = "bbbikefan"
            main.JOIN_REQUEST_ADMISSION_ENABLED = True
            main.JOIN_REQUEST_INVITE_URL = managed_url
            main.REFERRAL_MILESTONES = [(1, 50)]
            main._require_admin = authorized
            await self._feed_join_request(self._join_request_event(
                applicant_id, managed_url, update_id=992_030_101,
            ))
            response = await main.api_admin_decide(DummyRequest({
                "user_id": applicant_id, "decision": "approve",
            }, admin_id))
            with sqlite3.connect(main.DB_PATH) as db:
                before_join = (
                    db.execute(
                        "SELECT status,group_membership_status,ref_confirmed "
                        "FROM members WHERE user_id=?", (applicant_id,),
                    ).fetchone(),
                    db.execute(
                        "SELECT status,decision FROM telegram_join_requests "
                        "WHERE user_id=?", (applicant_id,),
                    ).fetchone(),
                    db.execute(
                        "SELECT bonus FROM members WHERE user_id=?", (referrer_id,),
                    ).fetchone()[0],
                    db.execute(
                        "SELECT COUNT(*) FROM task_outbox "
                        "WHERE event_type='join_request_decision'",
                    ).fetchone()[0],
                )
            await main.track_group_membership(self._membership_update(applicant_id))
            with sqlite3.connect(main.DB_PATH) as db:
                after_join = (
                    db.execute(
                        "SELECT group_membership_status,ref_confirmed "
                        "FROM members WHERE user_id=?", (applicant_id,),
                    ).fetchone(),
                    db.execute(
                        "SELECT status FROM telegram_join_requests WHERE user_id=?",
                        (applicant_id,),
                    ).fetchone()[0],
                    db.execute(
                        "SELECT bonus FROM members WHERE user_id=?", (referrer_id,),
                    ).fetchone()[0],
                )
        finally:
            for name, value in original.items():
                setattr(main, name, value)

        self.assertEqual(response.status, 200)
        self.assertEqual(response_json(response)["join_requests_queued"], 1)
        self.assertEqual(before_join, (
            ("approved", "unknown", 0), ("approve_queued", "approve"), 0, 1,
        ))
        self.assertEqual(after_join, (("member", 1), "joined", 50))

    async def test_replayed_authoritative_join_does_not_duplicate_referral_reward(self):
        managed_url = "https://t.me/+ManagedJoinToken_1234567890"
        referrer_id, applicant_id = 992_040_001, 992_040_002
        await main.upsert_member(
            referrer_id, full_name="Пригласивший", status="approved",
            role="helper", bonus=0,
        )
        await main.upsert_member(
            applicant_id, full_name="Одобренный", status="approved", role="helper",
            referred_by=referrer_id, ref_confirmed=0,
        )
        names = (
            "GROUP_ID", "GROUP_USERNAME", "JOIN_REQUEST_ADMISSION_ENABLED",
            "JOIN_REQUEST_INVITE_URL", "REFERRAL_MILESTONES",
        )
        original = {name: getattr(main, name) for name in names}
        try:
            main.GROUP_ID = -100_770_000_001
            main.GROUP_USERNAME = "bbbikefan"
            main.JOIN_REQUEST_ADMISSION_ENABLED = True
            main.JOIN_REQUEST_INVITE_URL = managed_url
            main.REFERRAL_MILESTONES = [(1, 50)]
            await self._feed_join_request(self._join_request_event(
                applicant_id, managed_url, update_id=992_040_101,
            ))
            update = self._membership_update(applicant_id)
            await main.track_group_membership(update)
            await main.track_group_membership(update)
        finally:
            for name, value in original.items():
                setattr(main, name, value)

        with sqlite3.connect(main.DB_PATH) as db:
            result = (
                db.execute(
                    "SELECT bonus FROM members WHERE user_id=?", (referrer_id,),
                ).fetchone()[0],
                db.execute(
                    "SELECT ref_confirmed FROM members WHERE user_id=?", (applicant_id,),
                ).fetchone()[0],
                db.execute(
                    "SELECT COUNT(*) FROM referral_milestone_rewards "
                    "WHERE user_id=? AND threshold=1", (referrer_id,),
                ).fetchone()[0],
                db.execute(
                    "SELECT COUNT(*) FROM product_events "
                    "WHERE event_name='referral_confirmed'",
                ).fetchone()[0],
                db.execute(
                    "SELECT COUNT(*) FROM task_outbox WHERE event_key=?",
                    (f"referral:{applicant_id}:confirmed:referrer:{referrer_id}",),
                ).fetchone()[0],
            )
        self.assertEqual(result, (50, 1, 1, 1, 1))

    async def test_test_environment_never_bypasses_admin_role(self):
        unknown_id, helper_id, admin_id = 899_900_001, 899_900_002, 899_900_003
        await main.upsert_member(
            helper_id, full_name="Помощник", status="approved", role="helper",
        )
        await self._seed_admin(admin_id)
        self.assertFalse(await main.is_admin(unknown_id))
        self.assertFalse(await main.is_admin(helper_id))
        self.assertTrue(await main.is_admin(admin_id))

    async def test_atomic_first_login(self):
        uid = 900_000_001
        await asyncio.gather(*(
            main.upsert_member(uid, full_name="Тест") for _ in range(50)
        ))
        with sqlite3.connect(main.DB_PATH) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM members WHERE user_id=?", (uid,)
            ).fetchone()[0]
        self.assertEqual(count, 1)

    async def test_privacy_link_accepts_only_public_https_without_credentials(self):
        self.assertEqual(
            main._safe_https_url("https://example.org/privacy"),
            "https://example.org/privacy",
        )
        for unsafe in (
            "http://example.org/privacy", "javascript:alert(1)",
            "https://user:password@example.org/privacy", "https://example.org:8443/privacy",
            "https://localhost/privacy", "https://127.0.0.1/privacy",
            "https://10.0.0.1/privacy", "https://policy.local/privacy",
        ):
            self.assertIsNone(main._safe_https_url(unsafe))
        original = (main.PRIVACY_URL_RAW, main.PRIVACY_URL)
        try:
            main.PRIVACY_URL_RAW = "https://user:password@example.org/privacy"
            main.PRIVACY_URL = None
            with self.assertRaisesRegex(RuntimeError, "PRIVACY_URL"):
                main._validate_update_receiver_config()
        finally:
            main.PRIVACY_URL_RAW, main.PRIVACY_URL = original

    async def test_same_origin_privacy_page_is_versioned_escaped_and_no_store(self):
        original = (
            main.PRIVACY_CONTROLLER_NAME, main.PRIVACY_CONTACT,
            main.EVIDENCE_RETENTION_DAYS,
        )
        try:
            main.PRIVACY_CONTROLLER_NAME = "ООО <Бибибайк>"
            main.PRIVACY_CONTACT = "privacy@example.test <script>"
            main.EVIDENCE_RETENTION_DAYS = 91
            response = await main.serve_privacy(None)
        finally:
            (
                main.PRIVACY_CONTROLLER_NAME, main.PRIVACY_CONTACT,
                main.EVIDENCE_RETENTION_DAYS,
            ) = original
        body = response.text
        self.assertEqual(response.status, 200)
        self.assertIn('data-bibitasks-privacy-version="1"', body)
        self.assertIn("ООО &lt;Бибибайк&gt;", body)
        self.assertIn("privacy@example.test &lt;script&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertIn("91 дней", body)
        self.assertEqual(response.headers["Cache-Control"].split(",")[0], "no-store")
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])

    async def test_webhook_configuration_is_fail_closed(self):
        names = (
            "TELEGRAM_UPDATE_MODE", "PUBLIC_BASE_URL", "WEBHOOK_ROUTE_ID",
            "WEBHOOK_PATH", "WEBHOOK_SECRET", "WEBHOOK_MAX_CONNECTIONS",
            "ADMIN_IDS",
            "OPS_GROUP_ID", "OPS_GROUP_USERNAME", "GROUP_ID", "GROUP_USERNAME",
            "TOPIC_CONFIG_EXPLICIT", "TOPIC_NEWS", "TOPIC_CHAT", "TOPIC_WORK",
            "TOPIC_FRANCHISE", "OPS_TOPIC_TASKS",
        )
        original = {name: getattr(main, name) for name in names}
        try:
            main.TELEGRAM_UPDATE_MODE = "webhook"
            main.PUBLIC_BASE_URL = "https://tasks.example"
            main.WEBHOOK_ROUTE_ID = "route_" + "a" * 40
            main.WEBHOOK_PATH = "/telegram/webhook/" + main.WEBHOOK_ROUTE_ID
            main.WEBHOOK_SECRET = "secret_" + "b" * 40
            main.WEBHOOK_MAX_CONNECTIONS = 8
            main.ADMIN_IDS = {42, 43}
            main.OPS_GROUP_ID = -1009000000001
            main.OPS_GROUP_USERNAME = ""
            main.GROUP_ID = -1009000000002
            main.GROUP_USERNAME = "bbbikefan"
            main.TOPIC_CONFIG_EXPLICIT = {
                "TOPIC_NEWS": True, "TOPIC_CHAT": True, "TOPIC_WORK": True,
                "TOPIC_FRANCHISE": True, "OPS_TOPIC_TASKS": True,
            }
            (
                main.TOPIC_NEWS, main.TOPIC_CHAT, main.TOPIC_WORK,
                main.TOPIC_FRANCHISE, main.OPS_TOPIC_TASKS,
            ) = (11, 12, 13, 14, 21)
            main._validate_update_receiver_config()
            main.TOPIC_CHAT = main.TOPIC_NEWS
            with self.assertRaisesRegex(RuntimeError, "topic IDs must be distinct"):
                main._validate_update_receiver_config()
            main.TOPIC_CHAT = 12
            main.GROUP_ID = main.OPS_GROUP_ID
            with self.assertRaisesRegex(RuntimeError, "must differ"):
                main._validate_update_receiver_config()
            main.GROUP_ID = -1009000000002
            main.PUBLIC_BASE_URL = "http://tasks.example"
            with self.assertRaisesRegex(RuntimeError, "HTTPS origin"):
                main._validate_update_receiver_config()
            main.PUBLIC_BASE_URL = "https://user:pass@tasks.example"
            with self.assertRaisesRegex(RuntimeError, "HTTPS origin"):
                main._validate_update_receiver_config()
            main.PUBLIC_BASE_URL = "https://tasks.example:9443"
            with self.assertRaisesRegex(RuntimeError, "HTTPS origin"):
                main._validate_update_receiver_config()
            main.PUBLIC_BASE_URL = "https://tasks.example"
            main.WEBHOOK_SECRET = "short"
            with self.assertRaisesRegex(RuntimeError, "WEBHOOK_SECRET"):
                main._validate_update_receiver_config()
            main.WEBHOOK_SECRET = main.WEBHOOK_ROUTE_ID
            with self.assertRaisesRegex(RuntimeError, "independent"):
                main._validate_update_receiver_config()
            main.WEBHOOK_SECRET = "secret_" + "b" * 40
            main.WEBHOOK_MAX_CONNECTIONS = 101
            with self.assertRaisesRegex(RuntimeError, "between 1 and 100"):
                main._validate_update_receiver_config()
        finally:
            for name, value in original.items():
                setattr(main, name, value)

    async def test_staging_retry_is_accelerated_but_production_override_fails_closed(self):
        names = (
            "BIBITASKS_ENVIRONMENT", "TELEGRAM_RETRY_BASE_SECONDS",
            "TELEGRAM_RETRY_MAX_SECONDS", "TELEGRAM_RETRY_MAX_ATTEMPTS",
        )
        original = {name: getattr(main, name) for name in names}
        try:
            main.BIBITASKS_ENVIRONMENT = "staging"
            main.TELEGRAM_RETRY_BASE_SECONDS = 1
            main.TELEGRAM_RETRY_MAX_SECONDS = 4
            main.TELEGRAM_RETRY_MAX_ATTEMPTS = 3
            self.assertEqual(
                [main._telegram_retry_delay(attempt) for attempt in (1, 2, 3, 4)],
                [1, 2, 4, 4],
            )
            main.BIBITASKS_ENVIRONMENT = "production"
            with self.assertRaisesRegex(RuntimeError, "forbidden in production"):
                main._validate_update_receiver_config()
        finally:
            for name, value in original.items():
                setattr(main, name, value)

    async def test_webhook_persists_deduplicates_and_processes_update(self):
        original_secret = main.WEBHOOK_SECRET
        main.WEBHOOK_SECRET = "secret_" + "z" * 40
        app = main.web.Application()
        app.router.add_post("/hook", main.telegram_webhook_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        payload = {
            "update_id": 7001,
            "message": {
                "message_id": 1,
                "date": int(time.time()),
                "chat": {"id": 123, "type": "private"},
                "from": {
                    "id": 123, "is_bot": False, "first_name": "Тест",
                },
                "text": "/start",
            },
        }
        headers = {"X-Telegram-Bot-Api-Secret-Token": main.WEBHOOK_SECRET}
        original_feed = main.dp.feed_raw_update
        calls = []

        async def fake_feed(_bot, update):
            calls.append(update["update_id"])
            return None

        try:
            denied = await client.post("/hook", json=payload)
            wrong = await client.post(
                "/hook", json=payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": "x" * 40},
            )
            get_response = await client.get("/hook")
            invalid = await client.post(
                "/hook", json={"update_id": True}, headers=headers,
            )
            too_deep = {"update_id": 7002}
            cursor = too_deep
            for _ in range(25):
                cursor["message"] = {}
                cursor = cursor["message"]
            deep_response = await client.post("/hook", json=too_deep, headers=headers)
            oversized = await client.post(
                "/hook", data=BytesIO(b"x" * (1024 * 1024 + 1)),
                headers={**headers, "Content-Type": "application/json"},
            )
            accepted = await client.post("/hook", json=payload, headers=headers)
            duplicate = await client.post("/hook", json=payload, headers=headers)
            with sqlite3.connect(main.DB_PATH) as db:
                encrypted = db.execute(
                    "SELECT payload_json FROM telegram_update_inbox WHERE update_id=7001"
                ).fetchone()[0]
            conflict = await client.post(
                "/hook", json={
                    **payload, "message": {**payload["message"], "message_id": 2},
                }, headers=headers,
            )
            self.assertEqual(
                (
                    denied.status, wrong.status, get_response.status,
                    invalid.status, deep_response.status, oversized.status,
                    accepted.status, duplicate.status, conflict.status,
                ),
                (401, 401, 405, 400, 400, 413, 200, 200, 409),
            )
            self.assertNotIn("/start", encrypted)
            self.assertTrue((await duplicate.json())["duplicate"])
            main.dp.feed_raw_update = fake_feed
            worker = asyncio.create_task(main.telegram_inbox_worker())
            status = "pending"
            for _ in range(200):
                with sqlite3.connect(main.DB_PATH) as db:
                    status = db.execute(
                        "SELECT status FROM telegram_update_inbox WHERE update_id=7001"
                    ).fetchone()[0]
                if status == "done":
                    break
                await asyncio.sleep(0.01)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        finally:
            main.dp.feed_raw_update = original_feed
            main.WEBHOOK_SECRET = original_secret
            await client.close()
        self.assertEqual(status, "done")
        self.assertEqual(calls, [7001])
        with sqlite3.connect(main.DB_PATH) as db:
            row = db.execute(
                "SELECT COUNT(*),payload_json FROM telegram_update_inbox "
                "WHERE update_id=7001"
            ).fetchone()
        self.assertEqual(row, (1, None))

    async def test_update_receiver_switches_without_dropping_updates(self):
        class Info:
            url = ""
            pending_update_count = 0
            last_error_date = None
            max_connections = 0
            allowed_updates = []

        class FakeBot:
            def __init__(self):
                self.info = Info()
                self.set_calls = []
                self.delete_calls = []

            async def set_webhook(self, url, **kwargs):
                self.set_calls.append((url, kwargs))
                self.info.url = url
                self.info.max_connections = kwargs["max_connections"]
                self.info.allowed_updates = kwargs["allowed_updates"]

            async def get_webhook_info(self):
                return self.info

            async def delete_webhook(self, **kwargs):
                self.delete_calls.append(kwargs)

        names = (
            "bot", "TELEGRAM_UPDATE_MODE", "PUBLIC_BASE_URL", "WEBHOOK_ROUTE_ID",
            "WEBHOOK_PATH", "WEBHOOK_SECRET",
        )
        original = {name: getattr(main, name) for name in names}
        fake = FakeBot()
        try:
            main.bot = fake
            main.TELEGRAM_UPDATE_MODE = "webhook"
            main.PUBLIC_BASE_URL = "https://tasks.example"
            main.WEBHOOK_ROUTE_ID = "route_" + "r" * 40
            main.WEBHOOK_PATH = "/telegram/webhook/" + main.WEBHOOK_ROUTE_ID
            main.WEBHOOK_SECRET = "secret_" + "s" * 40
            await main._configure_update_receiver()
            self.assertEqual(fake.set_calls[0][0], main._webhook_url())
            self.assertFalse(fake.set_calls[0][1]["drop_pending_updates"])
            self.assertIn("chat_member", fake.set_calls[0][1]["allowed_updates"])
            self.assertTrue(main._telegram_runtime["receiver_ready"])
            main.TELEGRAM_UPDATE_MODE = "polling"
            await main._configure_update_receiver()
            self.assertEqual(fake.delete_calls, [{"drop_pending_updates": False}])
        finally:
            for name, value in original.items():
                setattr(main, name, value)

    async def test_replayed_update_does_not_grant_chat_xp_twice(self):
        user = SimpleNamespace(
            id=900_000_099, full_name="Тестовый участник", username="tester",
        )
        token = main._current_update_id.set(88001)
        try:
            first = await main.add_chat_xp(user, 2, "msg")
            replay = await main.add_chat_xp(user, 2, "msg")
        finally:
            main._current_update_id.reset(token)
        with sqlite3.connect(main.DB_PATH) as db:
            xp = db.execute(
                "SELECT chat_xp FROM members WHERE user_id=?", (user.id,),
            ).fetchone()[0]
            effects = db.execute(
                "SELECT COUNT(*) FROM telegram_update_effects WHERE update_id=88001"
            ).fetchone()[0]
        self.assertEqual(first[0], 2)
        self.assertEqual(replay, (0, None))
        self.assertEqual((xp, effects), (2, 1))

    async def test_dead_telegram_update_redrive_is_audited_and_idempotent(self):
        original_admin = main._require_admin
        await self._seed_admin(42)

        async def allow_admin(_request):
            return 42, None

        main._require_admin = allow_admin
        payload = {"update_id": 99001}
        canonical = main.json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        operation_id = str(uuid.uuid4())
        stamp = main.now_iso()
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO telegram_update_inbox "
                "(update_id,payload_json,payload_sha256,status,attempts,available_at,"
                "received_at,dead_at) VALUES (?,?,?,'dead',10,?,?,?)",
                (
                    99001, main._encrypt_telegram_payload(canonical),
                    main._telegram_payload_fingerprint(canonical), stamp, stamp, stamp,
                ),
            )
            db.commit()
        body = {
            "update_id": 99001, "operation_id": operation_id,
            "reason": "Исправлен обработчик обновления",
        }
        try:
            first = response_json(await main.api_admin_telegram_inbox_redrive(
                DummyRequest(body),
            ))
            replay = response_json(await main.api_admin_telegram_inbox_redrive(
                DummyRequest(body),
            ))
            second_operation = str(uuid.uuid4())
            with sqlite3.connect(main.DB_PATH) as db:
                db.execute(
                    "UPDATE telegram_update_inbox SET status='dead',dead_at=? "
                    "WHERE update_id=99001", (main.now_iso(),),
                )
                db.commit()
            second = response_json(await main.api_admin_telegram_inbox_redrive(
                DummyRequest({**body, "operation_id": second_operation}),
            ))
            old_retry = response_json(await main.api_admin_telegram_inbox_redrive(
                DummyRequest(body),
            ))
        finally:
            main._require_admin = original_admin
        with sqlite3.connect(main.DB_PATH) as db:
            row = db.execute(
                "SELECT status,attempts,redrive_operation_id,redriven_by "
                "FROM telegram_update_inbox WHERE update_id=99001"
            ).fetchone()
            audit_count = db.execute(
                "SELECT COUNT(*) FROM telegram_update_redrive_commands "
                "WHERE update_id=99001"
            ).fetchone()[0]
        self.assertEqual(first["status"], "pending")
        self.assertTrue(replay["idempotent"])
        self.assertEqual(second["status"], "pending")
        self.assertTrue(old_retry["idempotent"])
        self.assertEqual(row, ("pending", 0, second_operation, 42))
        self.assertEqual(audit_count, 2)

    async def test_legacy_telegram_inbox_is_migrated_without_plaintext(self):
        payload = {"update_id": 99101, "message": {"text": "личный текст"}}
        canonical = main.json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO telegram_update_inbox "
                "(update_id,payload_json,payload_sha256,status,attempts,available_at,"
                "received_at) VALUES (?,?,?,'processing',0,?,?)",
                (
                    99101, canonical,
                    main.hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    main.now_iso(), main.now_iso(),
                ),
            )
            db.execute(
                "INSERT INTO telegram_update_effects "
                "(update_id,effect_key,created_at) VALUES (99101,'chat_xp:msg:12345',?)",
                (main.now_iso(),),
            )
            db.commit()
        await main.init_db()
        with sqlite3.connect(main.DB_PATH) as db:
            stored, fingerprint, status = db.execute(
                "SELECT payload_json,payload_sha256,status "
                "FROM telegram_update_inbox WHERE update_id=99101"
            ).fetchone()
            effect_key = db.execute(
                "SELECT effect_key FROM telegram_update_effects WHERE update_id=99101"
            ).fetchone()[0]
        self.assertNotIn("личный текст", stored)
        self.assertTrue(fingerprint.startswith("h1:"))
        self.assertEqual(status, "pending")
        self.assertEqual(main._decrypt_telegram_payload(stored), payload)
        self.assertEqual(effect_key, "chat_xp:msg")

    async def test_detailed_health_requires_token(self):
        unauthorized = SimpleNamespace(headers={}, remote="203.0.113.10")
        with self.assertRaises(main.web.HTTPUnauthorized):
            await main.api_health(unauthorized)
        authorized = SimpleNamespace(
            headers={"X-Health-Token": main.HEALTH_TOKEN},
            remote="203.0.113.10",
        )
        response = await main.api_health(authorized)
        self.assertIn(response.status, (200, 503))
        payload = json.loads(response.text)
        self.assertEqual(payload["report_version"], 1)
        self.assertEqual(payload["application_version"], main.APP_VERSION)
        self.assertEqual(payload["version"], main.BUILD_VERSION)
        self.assertIsNotNone(datetime.fromisoformat(payload["generated_at"]).tzinfo)

    async def test_forced_health_refresh_is_staging_loadtest_only(self):
        original_environment = main.BIBITASKS_ENVIRONMENT
        original_loadtest = main.PILOT_LOAD_TEST_ENABLED
        original_cache = dict(main._health_cache)
        request = SimpleNamespace(
            headers={"X-Health-Token": main.HEALTH_TOKEN},
            remote="172.20.0.10",
            rel_url=SimpleNamespace(query={"refresh": "1"}),
        )
        try:
            main._health_cache["checked_at"] = 9_000_000_000.0
            main.BIBITASKS_ENVIRONMENT = "test"
            main.PILOT_LOAD_TEST_ENABLED = False
            with self.assertRaises(main.web.HTTPForbidden):
                await main.api_health(request)

            before = main._health_cache["checked_at"]
            main.BIBITASKS_ENVIRONMENT = "staging"
            main.PILOT_LOAD_TEST_ENABLED = True
            response = await main.api_health(request)
            self.assertIn(response.status, (200, 503))
            self.assertNotEqual(main._health_cache["checked_at"], before)
        finally:
            main.BIBITASKS_ENVIRONMENT = original_environment
            main.PILOT_LOAD_TEST_ENABLED = original_loadtest
            main._health_cache.clear()
            main._health_cache.update(original_cache)

    async def test_loadtest_telegram_stub_is_staging_only_and_skips_bot_api(self):
        original_environment = main.BIBITASKS_ENVIRONMENT
        original_loadtest = main.PILOT_LOAD_TEST_ENABLED
        original_stub = main.PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED

        async def forbidden_send(*_args, **_kwargs):
            raise AssertionError("synthetic outbox touched Telegram")

        try:
            main.BIBITASKS_ENVIRONMENT = "staging"
            main.PILOT_LOAD_TEST_ENABLED = False
            main.PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED = True
            with self.assertRaisesRegex(RuntimeError, "requires staging load-test"):
                main._validate_update_receiver_config()

            main.PILOT_LOAD_TEST_ENABLED = True
            main._validate_update_receiver_config()
            item = {
                "payload_json": json.dumps({"text": "synthetic"}),
                "event_type": "direct",
                "recipient_id": 4_400_000_000_000_000,
            }
            with patch.object(main.bot, "send_message", new=forbidden_send):
                self.assertIsNone(await main._deliver_outbox_item(item))
        finally:
            main.BIBITASKS_ENVIRONMENT = original_environment
            main.PILOT_LOAD_TEST_ENABLED = original_loadtest
            main.PILOT_LOAD_TEST_TELEGRAM_STUB_ENABLED = original_stub

    async def test_database_lock_runtime_counter_is_monotonic_and_health_visible(self):
        before = main._runtime_errors["database_locked"]
        main._record_runtime_error(RuntimeError("unrelated"))
        self.assertEqual(main._runtime_errors["database_locked"], before)
        wrapped = RuntimeError("request failed")
        wrapped.__cause__ = sqlite3.OperationalError("database is locked")
        main._record_runtime_error(wrapped)
        self.assertEqual(main._runtime_errors["database_locked"], before + 1)
        request = SimpleNamespace(
            headers={"X-Health-Token": main.HEALTH_TOKEN},
            remote="127.0.0.1",
            rel_url=SimpleNamespace(query={}),
        )
        payload = json.loads((await main.api_health(request)).text)
        self.assertEqual(payload["database_locked_errors"], before + 1)
        main._runtime_errors["database_locked"] = before

    async def test_photo_declared_mime_must_match_content(self):
        image_bytes = BytesIO()
        Image.new("RGB", (2, 2), "green").save(image_bytes, format="PNG")
        encoded = base64.b64encode(image_bytes.getvalue()).decode()
        with self.assertRaisesRegex(ValueError, "не совпадает"):
            await main._save_image("data:image/jpeg;base64," + encoded)

    async def test_media_upload_is_idempotent_private_and_garbage_collected(self):
        image_bytes = BytesIO()
        Image.new("RGB", (3, 3), "blue").save(image_bytes, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(
            image_bytes.getvalue()
        ).decode()
        operation = f"test:{uuid.uuid4()}"
        first = await main._save_image(
            data_url, upload_operation_id=operation, request_hash="same-request",
        )
        replay = await main._save_image(
            data_url, upload_operation_id=operation, request_hash="same-request",
        )
        self.assertEqual(first["media_id"], replay["media_id"])
        with sqlite3.connect(main.DB_PATH) as db:
            count, state = db.execute(
                "SELECT COUNT(*),MAX(state) FROM media_objects "
                "WHERE upload_operation_id=?", (operation,),
            ).fetchone()
        self.assertEqual((count, state), (1, "ready"))

        app = main.web.Application()
        app.router.add_get("/media/{media_id}", main.serve_media)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            valid = await client.get(main._signed_media_url(first["media_id"]))
            tampered = await client.get(
                main._signed_media_url(first["media_id"]).replace("signature=", "signature=x")
            )
            self.assertEqual((valid.status, tampered.status), (200, 403))
            self.assertEqual(valid.headers["Cache-Control"], "private, no-store")
        finally:
            await client.close()

        old = (main.datetime.now(main.timezone.utc) - main.timedelta(days=2)).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "UPDATE media_objects SET created_at=? WHERE id=?",
                (old, first["media_id"]),
            )
            db.commit()
        await main._cleanup_media_objects()
        with sqlite3.connect(main.DB_PATH) as db:
            state = db.execute(
                "SELECT state FROM media_objects WHERE id=?", (first["media_id"],),
            ).fetchone()[0]
        self.assertEqual(state, "deleted")
        self.assertFalse((Path(main.TASK_PHOTO_DIR) / first["photo_file"]).exists())

    async def test_terminal_proof_photo_expires_but_audit_and_hash_remain(self):
        media_id = str(uuid.uuid4())
        filename = "retention-proof.jpg"
        content = b"retention-proof-bytes"
        digest = main.hashlib.sha256(content).hexdigest()
        (Path(main.TASK_PHOTO_DIR) / filename).write_bytes(content)
        old = (
            main.datetime.now(main.timezone.utc)
            - main.timedelta(
                days=main.EVIDENCE_RETENTION_DAYS + main.DISPUTE_OPEN_DAYS + 1
            )
        ).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO members (user_id,full_name,role,status,bonus,created_at) "
                "VALUES (701,'Исполнитель','helper','approved',50,?)", (old,),
            )
            task_id = db.execute(
                "INSERT INTO tasks (type,title,reward,status,created_at,done_at) "
                "VALUES ('fix_zone','Retention',50,'closed',?,?)", (old, old),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,done_at,terminal_at) "
                "VALUES (?,701,'done',?,?,?)", (task_id, old, old, old),
            ).lastrowid
            db.execute(
                "INSERT INTO media_objects "
                "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
                "upload_operation_id,request_hash,created_at,ready_at) "
                "VALUES (?,'local',?,'task_proof','ready','image/jpeg',?,?,?,?,?,?)",
                (media_id, filename, len(content), digest, "retention-proof", digest, old, old),
            )
            evidence_id = db.execute(
                "INSERT INTO task_evidence "
                "(assignment_id,task_id,user_id,kind,photo_file,media_id,sha256,"
                "attempt,is_current,created_at) VALUES (?,?,701,'after',?,?,?,1,1,?)",
                (assignment_id, task_id, filename, media_id, digest, old),
            ).lastrowid
            db.execute(
                "INSERT INTO bonus_ledger "
                "(user_id,amount,reason,task_id,assignment_id,created_at,operation_id) "
                "VALUES (701,50,'Retention audit',?,?,?,'retention-ledger')",
                (task_id, assignment_id, old),
            )
            db.commit()

        await main._cleanup_media_objects()
        with sqlite3.connect(main.DB_PATH) as db:
            evidence = db.execute(
                "SELECT media_id,photo_file,sha256 FROM task_evidence WHERE id=?",
                (evidence_id,),
            ).fetchone()
            media_state = db.execute(
                "SELECT state FROM media_objects WHERE id=?", (media_id,),
            ).fetchone()[0]
            ledger_count = db.execute(
                "SELECT COUNT(*) FROM bonus_ledger WHERE operation_id='retention-ledger'"
            ).fetchone()[0]
        self.assertEqual(evidence, (None, "", digest))
        self.assertEqual(media_state, "deleted")
        self.assertEqual(ledger_count, 1)
        self.assertFalse((Path(main.TASK_PHOTO_DIR) / filename).exists())

    async def test_pending_or_recent_dispute_holds_proof_photo(self):
        media_id = str(uuid.uuid4())
        filename = "retention-dispute.jpg"
        content = b"retention-dispute-bytes"
        digest = main.hashlib.sha256(content).hexdigest()
        (Path(main.TASK_PHOTO_DIR) / filename).write_bytes(content)
        old = (
            main.datetime.now(main.timezone.utc)
            - main.timedelta(
                days=main.EVIDENCE_RETENTION_DAYS + main.DISPUTE_OPEN_DAYS + 2
            )
        ).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks (type,title,reward,status,created_at,done_at) "
                "VALUES ('fix_zone','Dispute hold',50,'closed',?,?)", (old, old),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,terminal_at) "
                "VALUES (?,702,'done',?,?)", (task_id, old, old),
            ).lastrowid
            db.execute(
                "INSERT INTO media_objects "
                "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
                "upload_operation_id,request_hash,created_at,ready_at) "
                "VALUES (?,'local',?,'task_proof','ready','image/jpeg',?,?,?,?,?,?)",
                (media_id, filename, len(content), digest, "retention-dispute", digest, old, old),
            )
            db.execute(
                "INSERT INTO task_evidence "
                "(assignment_id,task_id,user_id,kind,photo_file,media_id,sha256,"
                "attempt,is_current,created_at) VALUES (?,?,702,'after',?,?,?,1,1,?)",
                (assignment_id, task_id, filename, media_id, digest, old),
            )
            dispute_id = db.execute(
                "INSERT INTO task_disputes "
                "(assignment_id,task_id,user_id,reward,reason,status,opened_by,opened_at,"
                "open_operation_id,open_request_hash) "
                "VALUES (?,?,702,50,'Проверка','pending',703,?,'retention-open','hash')",
                (assignment_id, task_id, old),
            ).lastrowid
            db.commit()

        await main._cleanup_media_objects()
        with sqlite3.connect(main.DB_PATH) as db:
            self.assertEqual(
                db.execute("SELECT media_id FROM task_evidence WHERE assignment_id=?", (assignment_id,)).fetchone()[0],
                media_id,
            )
            db.execute(
                "UPDATE task_disputes SET status='rejected',decided_at=? WHERE id=?",
                (main.now_iso(), dispute_id),
            )
            db.commit()
        await main._cleanup_media_objects()
        with sqlite3.connect(main.DB_PATH) as db:
            self.assertEqual(
                db.execute("SELECT media_id FROM task_evidence WHERE assignment_id=?", (assignment_id,)).fetchone()[0],
                media_id,
            )
            db.execute(
                "UPDATE task_disputes SET decided_at=? WHERE id=?", (old, dispute_id),
            )
            db.commit()
        await main._cleanup_media_objects()
        with sqlite3.connect(main.DB_PATH) as db:
            self.assertIsNone(
                db.execute("SELECT media_id FROM task_evidence WHERE assignment_id=?", (assignment_id,)).fetchone()[0]
            )
            self.assertEqual(
                db.execute("SELECT state FROM media_objects WHERE id=?", (media_id,)).fetchone()[0],
                "deleted",
            )

    async def test_closed_task_brief_photo_expires_after_all_work_and_disputes(self):
        media_id = str(uuid.uuid4())
        filename = "retention-brief.jpg"
        content = b"retention-brief-bytes"
        digest = main.hashlib.sha256(content).hexdigest()
        (Path(main.TASK_PHOTO_DIR) / filename).write_bytes(content)
        old = (
            main.datetime.now(main.timezone.utc)
            - main.timedelta(
                days=main.EVIDENCE_RETENTION_DAYS + main.DISPUTE_OPEN_DAYS + 1
            )
        ).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO media_objects "
                "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
                "upload_operation_id,request_hash,created_at,ready_at) "
                "VALUES (?,'local',?,'task_brief','ready','image/jpeg',?,?,?,?,?,?)",
                (media_id, filename, len(content), digest, "retention-brief", digest, old, old),
            )
            task_id = db.execute(
                "INSERT INTO tasks "
                "(type,title,details,address,lat,lng,reward,status,created_at,"
                "cancelled_at,photo_file,photo_media_id) "
                "VALUES ('fix_zone','Brief retention','Секрет','Точный адрес',"
                "45.0,39.0,0,'cancelled',?,?,?,?)",
                (old, old, filename, media_id),
            ).lastrowid
            db.execute(
                "INSERT INTO task_evidence "
                "(task_id,user_id,kind,photo_file,media_id,sha256,attempt,is_current,created_at) "
                "VALUES (?,704,'brief',?,?,?,1,1,?)",
                (task_id, filename, media_id, digest, old),
            )
            db.commit()
        await main._cleanup_media_objects()
        with sqlite3.connect(main.DB_PATH) as db:
            task = db.execute(
                "SELECT photo_media_id,photo_file,details,address,lat,lng "
                "FROM tasks WHERE id=?", (task_id,),
            ).fetchone()
            evidence = db.execute(
                "SELECT media_id,photo_file,sha256 FROM task_evidence WHERE task_id=?",
                (task_id,),
            ).fetchone()
            state = db.execute(
                "SELECT state FROM media_objects WHERE id=?", (media_id,),
            ).fetchone()[0]
        self.assertEqual(task, (None, None, None, None, None, None))
        self.assertEqual(evidence, (None, "", digest))
        self.assertEqual(state, "deleted")

    async def test_unresolved_dead_outbox_holds_media_then_sent_payload_is_redacted(self):
        media_id = str(uuid.uuid4())
        filename = "retention-outbox.jpg"
        content = b"retention-outbox-bytes"
        digest = main.hashlib.sha256(content).hexdigest()
        (Path(main.TASK_PHOTO_DIR) / filename).write_bytes(content)
        terminal_old = (
            main.datetime.now(main.timezone.utc)
            - main.timedelta(
                days=main.EVIDENCE_RETENTION_DAYS + main.DISPUTE_OPEN_DAYS + 2
            )
        ).isoformat()
        payload_old = (
            main.datetime.now(main.timezone.utc) - main.timedelta(days=31)
        ).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks (type,title,reward,status,created_at,done_at) "
                "VALUES ('fix_zone','Outbox hold',0,'closed',?,?)",
                (terminal_old, terminal_old),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,terminal_at) "
                "VALUES (?,705,'done',?,?)",
                (task_id, terminal_old, terminal_old),
            ).lastrowid
            db.execute(
                "INSERT INTO media_objects "
                "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
                "upload_operation_id,request_hash,created_at,ready_at) "
                "VALUES (?,'local',?,'task_proof','ready','image/jpeg',?,?,?,?,?,?)",
                (media_id, filename, len(content), digest, "retention-outbox", digest, terminal_old, terminal_old),
            )
            db.execute(
                "INSERT INTO task_evidence "
                "(assignment_id,task_id,user_id,kind,photo_file,media_id,sha256,"
                "attempt,is_current,created_at) VALUES (?,?,705,'after',?,?,?,1,1,?)",
                (assignment_id, task_id, filename, media_id, digest, terminal_old),
            )
            outbox_id = db.execute(
                "INSERT INTO task_outbox "
                "(event_key,event_type,media_id,payload_json,status,available_at,created_at) "
                "VALUES ('retention:dead','direct',?,?,'dead',?,?)",
                (
                    media_id, json.dumps({"address": "Точный адрес"}),
                    terminal_old, terminal_old,
                ),
            ).lastrowid
            db.commit()

        await main._cleanup_media_objects()
        with sqlite3.connect(main.DB_PATH) as db:
            self.assertEqual(
                db.execute("SELECT media_id FROM task_evidence WHERE assignment_id=?", (assignment_id,)).fetchone()[0],
                media_id,
            )
            db.execute(
                "UPDATE task_outbox SET status='sent',sent_at=? WHERE id=?",
                (payload_old, outbox_id),
            )
            db.commit()
        await main.cleanup_expired_analytics()
        with sqlite3.connect(main.DB_PATH) as db:
            outbox = db.execute(
                "SELECT payload_json,media_id FROM task_outbox WHERE id=?", (outbox_id,),
            ).fetchone()
            state = db.execute(
                "SELECT state FROM media_objects WHERE id=?", (media_id,),
            ).fetchone()[0]
        self.assertEqual(outbox, ('{"redacted":"retention"}', None))
        self.assertEqual(state, "deleted")

    async def test_malformed_terminal_timestamp_holds_evidence_fail_closed(self):
        media_id = str(uuid.uuid4())
        filename = "retention-malformed.jpg"
        content = b"retention-malformed-bytes"
        digest = main.hashlib.sha256(content).hexdigest()
        (Path(main.TASK_PHOTO_DIR) / filename).write_bytes(content)
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks (type,title,reward,status,created_at) "
                "VALUES ('fix_zone','Malformed hold',0,'closed','1')"
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,terminal_at) "
                "VALUES (?,706,'done','1','1')", (task_id,),
            ).lastrowid
            db.execute(
                "INSERT INTO media_objects "
                "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
                "upload_operation_id,request_hash,created_at,ready_at) "
                "VALUES (?,'local',?,'task_proof','ready','image/jpeg',?,?,?,?,?,?)",
                (media_id, filename, len(content), digest, "retention-malformed", digest, main.now_iso(), main.now_iso()),
            )
            db.execute(
                "INSERT INTO task_evidence "
                "(assignment_id,task_id,user_id,kind,photo_file,media_id,sha256,"
                "attempt,is_current,created_at) VALUES (?,?,706,'after',?,?,?,1,1,?)",
                (assignment_id, task_id, filename, media_id, digest, main.now_iso()),
            )
            db.commit()
        await main._cleanup_media_objects()
        with sqlite3.connect(main.DB_PATH) as db:
            self.assertEqual(
                db.execute("SELECT media_id FROM task_evidence WHERE assignment_id=?", (assignment_id,)).fetchone()[0],
                media_id,
            )
            self.assertEqual(
                db.execute("SELECT state FROM media_objects WHERE id=?", (media_id,)).fetchone()[0],
                "ready",
            )

    async def test_expired_task_with_review_assignment_holds_brief_and_address(self):
        old = (
            main.datetime.now(main.timezone.utc)
            - main.timedelta(
                days=main.EVIDENCE_RETENTION_DAYS + main.DISPUTE_OPEN_DAYS + 2
            )
        ).isoformat()
        media_id = str(uuid.uuid4())
        filename = "retention-active-review.jpg"
        content = b"retention-active-review-bytes"
        digest = main.hashlib.sha256(content).hexdigest()
        (Path(main.TASK_PHOTO_DIR) / filename).write_bytes(content)
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks "
                "(type,title,details,address,reward,status,created_at,expired_at,"
                "photo_file,photo_media_id) VALUES "
                "('fix_zone','Review hold','Sensitive brief','Exact address',0,"
                "'expired',?,?,?,?)",
                (old, old, filename, media_id),
            ).lastrowid
            db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,done_at,proof_note) "
                "VALUES (?,707,'review',?,?,'Still awaiting review')",
                (task_id, old, old),
            )
            db.execute(
                "INSERT INTO media_objects "
                "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
                "upload_operation_id,request_hash,created_at,ready_at) "
                "VALUES (?,'local',?,'task_brief','ready','image/jpeg',?,?,?,?,?,?)",
                (media_id, filename, len(content), digest, "review-hold", digest, old, old),
            )
            db.execute(
                "INSERT INTO task_evidence "
                "(task_id,user_id,kind,photo_file,media_id,sha256,attempt,is_current,created_at) "
                "VALUES (?,0,'brief',?,?,?,1,1,?)",
                (task_id, filename, media_id, digest, old),
            )
            db.commit()
        await main._cleanup_media_objects()
        with sqlite3.connect(main.DB_PATH) as db:
            task = db.execute(
                "SELECT details,address,photo_media_id FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            state = db.execute(
                "SELECT state FROM media_objects WHERE id=?", (media_id,),
            ).fetchone()[0]
        self.assertEqual(task, ("Sensitive brief", "Exact address", media_id))
        self.assertEqual(state, "ready")

    async def test_comment_only_evidence_and_resolved_dispute_text_expire(self):
        old = (
            main.datetime.now(main.timezone.utc)
            - main.timedelta(
                days=main.EVIDENCE_RETENTION_DAYS + main.DISPUTE_OPEN_DAYS + 2
            )
        ).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks (type,title,reward,status,created_at,done_at) "
                "VALUES ('fix_zone','Comment retention',0,'closed',?,?)",
                (old, old),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,done_at,terminal_at,proof_note,"
                "review_note,release_reason) VALUES "
                "(?,708,'done',?,?,?,'Sensitive proof','Sensitive review','Sensitive release')",
                (task_id, old, old, old),
            ).lastrowid
            db.execute(
                "INSERT INTO task_disputes "
                "(assignment_id,task_id,user_id,reward,reason,reconciliation_reason,"
                "reconciliation_reference,status,opened_by,opened_at,open_operation_id,"
                "open_request_hash,decided_by,decided_at,decision_note) VALUES "
                "(?,?,708,0,'Sensitive dispute','Sensitive reconciliation','Sensitive ref',"
                "'dismissed',1,?,'retention-dispute','hash',2,?,'Sensitive decision')",
                (assignment_id, task_id, old, old),
            )
            outbox_id = db.execute(
                "INSERT INTO task_outbox "
                "(event_key,event_type,payload_json,status,available_at,created_at,sent_at) "
                "VALUES (?, 'direct', ?, 'sent', ?, ?, ?)",
                (
                    f"assignment:{assignment_id}:proof:late-redrive",
                    json.dumps({"text": "Sensitive proof"}),
                    main.now_iso(), main.now_iso(), main.now_iso(),
                ),
            ).lastrowid
            db.commit()
        await main._cleanup_media_objects()
        with sqlite3.connect(main.DB_PATH) as db:
            assignment = db.execute(
                "SELECT proof_note,review_note,release_reason FROM task_assignments "
                "WHERE id=?", (assignment_id,),
            ).fetchone()
            dispute = db.execute(
                "SELECT reason,reconciliation_reason,reconciliation_reference,decision_note "
                "FROM task_disputes WHERE assignment_id=?", (assignment_id,),
            ).fetchone()
            outbox_payload = db.execute(
                "SELECT payload_json FROM task_outbox WHERE id=?", (outbox_id,),
            ).fetchone()[0]
        self.assertEqual(assignment, (None, None, None))
        self.assertEqual(dispute, ("", None, None, None))
        self.assertEqual(outbox_payload, '{"redacted":"retention"}')

    async def test_s3_storage_adapter_is_private_and_checksum_verified(self):
        class FakeS3:
            def __init__(self):
                self.objects = {}
                self.last_put = None

            def put_object(self, **kwargs):
                self.last_put = kwargs
                self.objects[(kwargs["Bucket"], kwargs["Key"])] = (
                    bytes(kwargs["Body"]), kwargs["Metadata"],
                )

            def head_object(self, **kwargs):
                try:
                    content, metadata = self.objects[(kwargs["Bucket"], kwargs["Key"])]
                except KeyError:
                    raise FileNotFoundError from None
                return {"ContentLength": len(content), "Metadata": metadata}

            def get_object(self, **kwargs):
                content, _ = self.objects[(kwargs["Bucket"], kwargs["Key"])]
                return {"Body": BytesIO(content)}

            def delete_object(self, **kwargs):
                self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)

            def list_object_versions(self, **_kwargs):
                return {"Versions": [], "DeleteMarkers": [], "IsTruncated": False}

        fake = FakeS3()
        original_storage = main.MEDIA_STORAGE
        original_client = main._s3_client
        main.MEDIA_STORAGE = "s3"
        main._s3_client = lambda **_kwargs: fake
        content = b"jpeg-bytes"
        digest = main.hashlib.sha256(content).hexdigest()
        try:
            await main._storage_put("safe.jpg", content, digest)
            size, stored_digest = await main._storage_head("safe.jpg")
            restored = await main._storage_read("safe.jpg")
            await main._storage_delete("safe.jpg")
        finally:
            main.MEDIA_STORAGE = original_storage
            main._s3_client = original_client
        self.assertEqual((size, stored_digest, restored), (len(content), digest, content))
        self.assertEqual(fake.last_put["ServerSideEncryption"], "AES256")
        self.assertNotIn("ACL", fake.last_put)
        self.assertFalse(fake.objects)

    async def test_versioned_s3_delete_removes_all_versions_and_delete_markers(self):
        class MissingVersion(Exception):
            response = {"Error": {"Code": "NoSuchVersion"}}

        class VersionedS3:
            def __init__(self):
                self.objects = {}
                self.delete_markers = {("bibitasks/versioned.jpg", "marker-v3")}
                self.deleted = []
                self.sequence = 0

            def put_object(self, **kwargs):
                self.sequence += 1
                version = f"v{self.sequence}"
                self.objects[(kwargs["Key"], version)] = bytes(kwargs["Body"])
                return {"VersionId": version}

            def head_object(self, **kwargs):
                version = kwargs.get("VersionId")
                if version is None:
                    candidates = [
                        item_version for item_key, item_version in self.objects
                        if item_key == kwargs["Key"]
                    ]
                    if not candidates:
                        raise MissingVersion()
                    version = sorted(candidates)[-1]
                key = (kwargs["Key"], version)
                if key not in self.objects:
                    raise MissingVersion()
                content = self.objects[key]
                return {
                    "ContentLength": len(content),
                    "Metadata": {"sha256": main.hashlib.sha256(content).hexdigest()},
                    "VersionId": key[1],
                }

            def delete_object(self, **kwargs):
                key = (kwargs["Key"], kwargs["VersionId"])
                self.deleted.append(key)
                self.objects.pop(key, None)
                self.delete_markers.discard(key)

            def list_object_versions(self, **kwargs):
                return {
                    "Versions": [
                        {"Key": key, "VersionId": version}
                        for key, version in self.objects
                        if key == kwargs["Prefix"]
                    ],
                    "DeleteMarkers": [
                        {"Key": key, "VersionId": version}
                        for key, version in self.delete_markers
                        if key == kwargs["Prefix"]
                    ],
                    "IsTruncated": False,
                }

        fake = VersionedS3()
        original_client = main._s3_client
        main._s3_client = lambda **_kwargs: fake
        try:
            digest = main.hashlib.sha256(b"photo").hexdigest()
            await main._storage_put(
                "versioned.jpg", b"older", main.hashlib.sha256(b"older").hexdigest(),
                backend="s3",
            )
            version_id = await main._storage_put(
                "versioned.jpg", b"photo", digest, backend="s3",
            )
            await main._storage_delete(
                "versioned.jpg", backend="s3", version_id=version_id,
            )
        finally:
            main._s3_client = original_client
        self.assertEqual(
            set(fake.deleted),
            {
                ("bibitasks/versioned.jpg", "v1"),
                ("bibitasks/versioned.jpg", "v2"),
                ("bibitasks/versioned.jpg", "marker-v3"),
            },
        )
        self.assertFalse(fake.objects)
        self.assertFalse(fake.delete_markers)

    async def test_s3_privacy_check_is_fail_closed_for_custom_endpoint(self):
        class FakeS3:
            def head_bucket(self, **_kwargs):
                return {}

            def get_public_access_block(self, **_kwargs):
                return {"PublicAccessBlockConfiguration": {}}

        original = (
            main.MEDIA_STORAGE, main.S3_ENDPOINT_URL, main.S3_PRIVACY_MODE,
            main._s3_client,
        )
        main.MEDIA_STORAGE = "s3"
        main.S3_ENDPOINT_URL = "https://s3.example.test"
        main.S3_PRIVACY_MODE = "public_access_block"
        main._s3_client = lambda **_kwargs: FakeS3()
        try:
            self.assertFalse(await main._storage_healthcheck())
        finally:
            (
                main.MEDIA_STORAGE, main.S3_ENDPOINT_URL, main.S3_PRIVACY_MODE,
                main._s3_client,
            ) = original

    async def test_local_backup_restore_round_trip(self):
        from scripts.backup import create_backup
        from scripts.restore import restore_backup

        image_bytes = BytesIO()
        Image.new("RGB", (4, 4), "green").save(image_bytes, format="PNG")
        media = await main._save_image(
            "data:image/png;base64," + base64.b64encode(image_bytes.getvalue()).decode()
        )
        data_dir = Path(main.DB_PATH).parent
        output_dir = TEST_ROOT / (self.id().rsplit(".", 1)[-1] + "_backups")
        restore_dir = TEST_ROOT / (self.id().rsplit(".", 1)[-1] + "_restored")
        backup_dir = create_backup(data_dir, output_dir)
        restored = restore_backup(backup_dir, restore_dir)
        with sqlite3.connect(restored / "bibitasks.db") as db:
            row = db.execute(
                "SELECT object_key,sha256,state FROM media_objects WHERE id=?",
                (media["media_id"],),
            ).fetchone()
        restored_photo = restored / "task_photos" / row[0]
        self.assertEqual(row[2], "ready")
        self.assertTrue(restored_photo.is_file())
        self.assertEqual(main.hashlib.sha256(restored_photo.read_bytes()).hexdigest(), row[1])
        self.assertTrue((restored / "restore-report.json").is_file())

    async def test_explicit_operational_env_file_beats_cwd_dotenv(self):
        work = TEST_ROOT / (self.id().rsplit(".", 1)[-1] + "_env")
        work.mkdir(parents=True, exist_ok=True)
        (work / ".env").write_text("S3_BUCKET=wrong-cwd-bucket\n", encoding="utf-8")
        explicit = work / "restore.env"
        explicit.write_text("S3_BUCKET=explicit-target-bucket\n", encoding="utf-8")
        repo_root = Path(__file__).resolve().parents[1]
        clean_env = dict(os.environ)
        clean_env.pop("S3_BUCKET", None)
        snippet = (
            "import os,sys; from pathlib import Path; "
            "sys.path.insert(0,sys.argv[1]); "
            "from scripts.restore import _load_environment; "
            "_load_environment(Path(sys.argv[2])); print(os.environ['S3_BUCKET'])"
        )
        result = subprocess.run(
            [sys.executable, "-c", snippet, str(repo_root), str(explicit)],
            cwd=work, env=clean_env, check=True, capture_output=True, text=True,
        )
        self.assertEqual(result.stdout.strip(), "explicit-target-bucket")

    async def test_s3_backup_restore_rewrites_version_id(self):
        from scripts.backup import create_backup
        from scripts.restore import restore_backup

        class Missing(Exception):
            response = {"Error": {"Code": "NoSuchKey"}}

        class FakeS3:
            def __init__(self):
                self.objects = {}
                self.latest = {}
                self.sequence = 1

            def head_bucket(self, **_kwargs):
                return {}

            def get_public_access_block(self, **_kwargs):
                names = (
                    "BlockPublicAcls", "IgnorePublicAcls",
                    "BlockPublicPolicy", "RestrictPublicBuckets",
                )
                return {"PublicAccessBlockConfiguration": {name: True for name in names}}

            def _record(self, kwargs):
                bucket, key = kwargs["Bucket"], kwargs["Key"]
                version = kwargs.get("VersionId") or self.latest.get((bucket, key))
                record = self.objects.get((bucket, key, version))
                if record is None:
                    raise Missing()
                return version, record

            def head_object(self, **kwargs):
                version, record = self._record(kwargs)
                return {
                    "ContentLength": len(record["content"]),
                    "Metadata": record["metadata"], "VersionId": version,
                }

            def get_object(self, **kwargs):
                version, record = self._record(kwargs)
                return {"Body": BytesIO(record["content"]), "VersionId": version}

            def put_object(self, **kwargs):
                bucket, key = kwargs["Bucket"], kwargs["Key"]
                version = f"new-v{self.sequence}"
                self.sequence += 1
                self.objects[(bucket, key, version)] = {
                    "content": bytes(kwargs["Body"]),
                    "metadata": dict(kwargs.get("Metadata") or {}),
                }
                self.latest[(bucket, key)] = version
                return {"VersionId": version}

            def delete_object(self, **kwargs):
                bucket, key = kwargs["Bucket"], kwargs["Key"]
                version = kwargs.get("VersionId") or self.latest.get((bucket, key))
                self.objects.pop((bucket, key, version), None)

        content = b"private-versioned-photo"
        digest = main.hashlib.sha256(content).hexdigest()
        media_id = str(uuid.uuid4())
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO media_objects "
                "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
                "upload_operation_id,request_hash,created_at,ready_at,version_id) "
                "VALUES (?,'s3','remote.jpg','task_photo','ready','image/jpeg',?,?,?,?,?,?,?)",
                (
                    media_id, len(content), digest, "remote-upload", digest,
                    main.now_iso(), main.now_iso(), "old-v1",
                ),
            )
            db.commit()

        fake = FakeS3()
        fake.objects[("source-bucket", "bibitasks/remote.jpg", "old-v1")] = {
            "content": content, "metadata": {"sha256": digest},
        }
        fake.latest[("source-bucket", "bibitasks/remote.jpg")] = "old-v1"
        fake_boto3 = SimpleNamespace(client=lambda *_args, **_kwargs: fake)
        fake_botocore = SimpleNamespace()
        fake_botocore_config = SimpleNamespace(Config=lambda **kwargs: kwargs)
        output_dir = TEST_ROOT / (self.id().rsplit(".", 1)[-1] + "_backups")
        restore_dir = TEST_ROOT / (self.id().rsplit(".", 1)[-1] + "_restored")
        env = {
            "S3_BUCKET": "source-bucket", "S3_PREFIX": "bibitasks",
            "S3_REGION": "us-east-1", "S3_SSE": "AES256",
            "S3_PRIVACY_MODE": "public_access_block",
        }
        with patch.dict(sys.modules, {
            "boto3": fake_boto3, "botocore": fake_botocore,
            "botocore.config": fake_botocore_config,
        }), patch.dict(
            os.environ, env, clear=False,
        ):
            backup_dir = create_backup(Path(main.DB_PATH).parent, output_dir)
            os.environ["S3_BUCKET"] = "target-bucket"
            restored = restore_backup(backup_dir, restore_dir)
        with sqlite3.connect(restored / "bibitasks.db") as db:
            version_id = db.execute(
                "SELECT version_id FROM media_objects WHERE id=?", (media_id,),
            ).fetchone()[0]
        self.assertEqual(version_id, "new-v1")
        self.assertEqual(
            fake.objects[("target-bucket", "bibitasks/remote.jpg", "new-v1")]["content"],
            content,
        )

    async def test_publication_reservation_prevents_two_pending_posts(self):
        original_admin = main.is_admin

        async def allow_admin(_uid):
            return True

        class FakeMessage:
            def __init__(self, message_id):
                self.message_id = message_id
                self.text = "/новости"
                self.chat = SimpleNamespace(id=700, type="private")
                self.from_user = SimpleNamespace(id=42)
                self.answers = []

            async def answer(self, text, **_kwargs):
                self.answers.append(text)

        main.is_admin = allow_admin
        first = FakeMessage(1)
        second = FakeMessage(2)
        try:
            token = main._current_update_id.set(101)
            await main._publish(first, ["часть 1", "часть 2"], 1, "news")
            main._current_update_id.reset(token)
            token = main._current_update_id.set(102)
            await main._publish(second, ["другая версия"], 1, "news")
            main._current_update_id.reset(token)
            with sqlite3.connect(main.DB_PATH) as db:
                operation_id = db.execute(
                    "SELECT operation_id FROM publication_jobs WHERE kind='news'"
                ).fetchone()[0]
                db.execute(
                    "UPDATE publication_jobs SET status='done' WHERE kind='news'"
                )
                db.execute(
                    "UPDATE task_outbox SET status='sent' WHERE event_key=?",
                    (operation_id,),
                )
                db.execute(
                    "INSERT INTO published_posts "
                    "(kind,chat_id,topic,message_ids,published_at,published_by,operation_id) "
                    "VALUES ('news',700,1,'[77]',?,42,?)",
                    (main.now_iso(), operation_id),
                )
                db.commit()
            replay = FakeMessage(1)
            replay.text = "/новости заново"
            token = main._current_update_id.set(101)
            await main._publish(replay, ["часть 1", "часть 2"], 1, "news")
            main._current_update_id.reset(token)
        finally:
            main.is_admin = original_admin
        with sqlite3.connect(main.DB_PATH) as db:
            jobs = db.execute(
                "SELECT COUNT(*) FROM publication_jobs WHERE kind='news'"
            ).fetchone()[0]
            queued = db.execute(
                "SELECT COUNT(*) FROM task_outbox WHERE event_type='group_publication'"
            ).fetchone()[0]
            job_status = db.execute(
                "SELECT status FROM publication_jobs WHERE kind='news'"
            ).fetchone()[0]
        self.assertEqual((jobs, queued), (1, 1))
        self.assertIn("уже стоит в очереди", second.answers[-1])
        self.assertEqual(job_status, "done")
        self.assertIn("уже опубликован", replay.answers[-1])

    async def test_publication_cleanup_is_durable_and_retryable(self):
        operation = "update:500:publication:news"
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO published_posts "
                "(kind,chat_id,topic,message_ids,published_at,published_by,operation_id) "
                "VALUES ('news',700,1,'[10,11]',?,42,'old-op')",
                (main.now_iso(),),
            )
            db.execute(
                "INSERT INTO publication_jobs "
                "(kind,operation_id,status,requested_by,created_at) "
                "VALUES ('news',?,'sending',42,?)",
                (operation, main.now_iso()),
            )
            db.commit()
        staged = await main._remember_published(
            "news", 700, 1, [20], 42, operation,
        )
        self.assertTrue(staged)

        class FakeBot:
            def __init__(self):
                self.fail = True
                self.deleted = []

            async def delete_message(self, chat_id, message_id):
                if self.fail:
                    self.fail = False
                    raise RuntimeError("temporary Telegram error")
                self.deleted.append((chat_id, message_id))

        original_bot = main.bot
        fake = FakeBot()
        main.bot = fake
        try:
            with self.assertRaises(RuntimeError):
                await main._run_publication_cleanup(operation)
            await main._run_publication_cleanup(operation)
        finally:
            main.bot = original_bot
        with sqlite3.connect(main.DB_PATH) as db:
            status = db.execute(
                "SELECT status FROM publication_jobs WHERE operation_id=?", (operation,),
            ).fetchone()[0]
            remaining = db.execute(
                "SELECT COUNT(*) FROM publication_cleanup_messages "
                "WHERE operation_id=? AND status!='deleted'", (operation,),
            ).fetchone()[0]
        self.assertEqual((status, remaining), ("done", 0))
        self.assertEqual(set(fake.deleted), {(700, 10), (700, 11)})

    async def test_permanent_publication_cleanup_does_not_delete_current_or_block_repost(self):
        operation = "update:501:publication:news"
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO published_posts "
                "(kind,chat_id,topic,message_ids,published_at,published_by,operation_id) "
                "VALUES ('news',700,1,'[10]',?,42,'old-op')",
                (main.now_iso(),),
            )
            db.execute(
                "INSERT INTO publication_jobs "
                "(kind,operation_id,status,requested_by,created_at) "
                "VALUES ('news',?,'sending',42,?)",
                (operation, main.now_iso()),
            )
            db.commit()
        await main._remember_published("news", 700, 1, [20], 42, operation)

        class FailingBot:
            async def delete_message(self, _chat_id, _message_id):
                raise RuntimeError("permanent Telegram error")

        original_bot = main.bot
        original_admin_ids = main.ADMIN_IDS
        main.bot = FailingBot()
        main.ADMIN_IDS = {42}
        try:
            await main.upsert_member(42, status="approved", role="admin")
            for _ in range(main.PUBLICATION_CLEANUP_MAX_ATTEMPTS - 1):
                with self.assertRaises(RuntimeError):
                    await main._run_publication_cleanup(operation)
            await main._run_publication_cleanup(operation)
        finally:
            main.bot = original_bot
            main.ADMIN_IDS = original_admin_ids

        with sqlite3.connect(main.DB_PATH) as db:
            current = db.execute(
                "SELECT message_ids,operation_id FROM published_posts WHERE kind='news'"
            ).fetchone()
            job_status = db.execute(
                "SELECT status FROM publication_jobs WHERE operation_id=?", (operation,),
            ).fetchone()[0]
            cleanup_status = db.execute(
                "SELECT status,attempts FROM publication_cleanup_messages "
                "WHERE operation_id=?", (operation,),
            ).fetchone()
            notification = db.execute(
                "SELECT COUNT(*) FROM task_outbox WHERE event_key LIKE ?",
                (f"publication-cleanup-failed:{operation}%",),
            ).fetchone()[0]
        self.assertEqual(current, ("[20]", operation))
        self.assertEqual(job_status, "cleanup_failed")
        self.assertEqual(cleanup_status, ("failed", 10))
        self.assertGreaterEqual(notification, 1)

        original_admin = main.is_admin

        async def allow_admin(_uid):
            return True

        class FakeMessage:
            message_id = 900
            text = "/новости заново"
            chat = SimpleNamespace(id=700, type="private")
            from_user = SimpleNamespace(id=42)

            def __init__(self):
                self.answers = []

            async def answer(self, text, **_kwargs):
                self.answers.append(text)

        main.is_admin = allow_admin
        message = FakeMessage()
        try:
            token = main._current_update_id.set(502)
            await main._publish(message, ["новая версия"], 1, "news")
            main._current_update_id.reset(token)
        finally:
            main.is_admin = original_admin
        with sqlite3.connect(main.DB_PATH) as db:
            next_job = db.execute(
                "SELECT operation_id,status FROM publication_jobs WHERE kind='news'"
            ).fetchone()
        self.assertEqual(next_job, ("update:502:publication:news", "pending"))

    async def test_current_publication_dead_delivery_keeps_cleanup_recoverable(self):
        operation = "update:900:publication:news"
        payload = {
            "kind": "news", "target": 700, "topic": 1,
            "parts": ["new"], "admin_id": 42,
        }
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO published_posts "
                "(kind,chat_id,topic,message_ids,published_at,published_by,operation_id) "
                "VALUES ('news',700,1,'[20]',?,42,?)", (main.now_iso(), operation),
            )
            db.execute(
                "INSERT INTO publication_jobs "
                "(kind,operation_id,status,requested_by,created_at) "
                "VALUES ('news',?,'cleanup_pending',42,?)",
                (operation, main.now_iso()),
            )
            db.execute(
                "INSERT INTO publication_cleanup_messages "
                "(operation_id,chat_id,message_id,final_job_status,status) "
                "VALUES (?, '700', 10, 'done', 'pending')", (operation,),
            )
            db.commit()
        item = {
            "event_type": "group_publication", "event_key": operation,
            "payload_json": main.json.dumps(payload),
        }
        await main._deliver_outbox_item(item)
        async with main.aiosqlite.connect(main.DB_PATH) as db:
            await main._handle_dead_publication_in_tx(db, item)
            await db.commit()
        with sqlite3.connect(main.DB_PATH) as db:
            job = db.execute(
                "SELECT status FROM publication_jobs WHERE operation_id=?", (operation,),
            ).fetchone()[0]
            cleanup = db.execute(
                "SELECT status,attempts FROM publication_cleanup_messages "
                "WHERE operation_id=?", (operation,),
            ).fetchone()
        self.assertEqual(job, "cleanup_pending")
        self.assertEqual(cleanup, ("pending", 0))

    async def test_stale_media_reconciliation_and_delete_errors(self):
        image_bytes = BytesIO()
        Image.new("RGB", (3, 3), "red").save(image_bytes, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(
            image_bytes.getvalue()
        ).decode()
        media = await main._save_image(data_url)
        old = (main.datetime.now(main.timezone.utc) - main.timedelta(hours=2)).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "UPDATE media_objects SET state='uploading',created_at=?,ready_at=NULL "
                "WHERE id=?", (old, media["media_id"]),
            )
            db.execute(
                "INSERT INTO media_objects "
                "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
                "upload_operation_id,request_hash,created_at,reconcile_attempts) "
                "VALUES (?,'local','missing.jpg','task_proof','uploading','image/jpeg',"
                "1,?,'missing-op','missing-request',?,4)",
                (str(uuid.uuid4()), "0" * 64, old),
            )
            missing_id = db.execute(
                "SELECT id FROM media_objects WHERE upload_operation_id='missing-op'"
            ).fetchone()[0]
            db.commit()
        await main._reconcile_stale_media_uploads()
        with sqlite3.connect(main.DB_PATH) as db:
            recovered = db.execute(
                "SELECT state FROM media_objects WHERE id=?", (media["media_id"],),
            ).fetchone()[0]
            missing = db.execute(
                "SELECT state FROM media_objects WHERE id=?", (missing_id,),
            ).fetchone()[0]
        self.assertEqual((recovered, missing), ("ready", "quarantined"))

        path = Path(main.TASK_PHOTO_DIR) / "permission.jpg"
        path.write_bytes(b"private")
        original_remove = main.os.remove
        main.os.remove = lambda _path: (_ for _ in ()).throw(PermissionError("denied"))
        try:
            with self.assertRaises(PermissionError):
                await main._storage_delete("permission.jpg", backend="local")
        finally:
            main.os.remove = original_remove

    async def test_media_gc_and_task_attach_never_leave_deleted_reference(self):
        await self._seed_admin(42)
        media_id = str(uuid.uuid4())
        filename = f"{media_id}.jpg"
        content = b"old-ready-photo"
        digest = main.hashlib.sha256(content).hexdigest()
        (Path(main.TASK_PHOTO_DIR) / filename).write_bytes(content)
        old = (main.datetime.now(main.timezone.utc) - main.timedelta(days=2)).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO media_objects "
                "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
                "upload_operation_id,request_hash,created_at,ready_at,delete_after) "
                "VALUES (?,'local',?,'task_brief','ready','image/jpeg',?,?,?,?,?,?,?)",
                (
                    media_id, filename, len(content), digest, "race-upload", digest,
                    old, old, main.now_iso(),
                ),
            )
            db.commit()

        original_admin = main._require_admin
        original_save = main._save_image

        async def allow_admin(_request):
            return 42, None

        async def reuse_media(*_args, **_kwargs):
            await asyncio.sleep(0)
            return {"media_id": media_id, "photo_file": filename, "sha256": digest}

        main._require_admin = allow_admin
        main._save_image = reuse_media
        body = {
            "operation_id": str(uuid.uuid4()), "type": "fix_zone",
            "title": "Гонка хранения", "details": "Проверка",
            "city": "Краснодар", "address": "ул. Красная, 1", "reward": 80,
            "evidence_policy": "after_required", "repeatable": False,
            "announce": False, "photo_data": "synthetic",
        }
        try:
            response, _ = await asyncio.gather(
                main.api_admin_task_create(DummyRequest(body)),
                main._cleanup_media_objects(),
            )
        finally:
            main._require_admin = original_admin
            main._save_image = original_save
        with sqlite3.connect(main.DB_PATH) as db:
            media_state = db.execute(
                "SELECT state FROM media_objects WHERE id=?", (media_id,),
            ).fetchone()[0]
            references = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE photo_media_id=?", (media_id,),
            ).fetchone()[0]
        if response.status == 200:
            self.assertEqual((references, media_state), (1, "ready"))
            self.assertTrue((Path(main.TASK_PHOTO_DIR) / filename).exists())
        else:
            self.assertEqual(response.status, 409)
            self.assertEqual(references, 0)
            self.assertEqual(media_state, "deleted")
            path.unlink(missing_ok=True)

    async def test_polling_validates_independent_media_secret(self):
        original_mode = main.TELEGRAM_UPDATE_MODE
        original_key = main.MEDIA_SIGNING_KEY
        try:
            main.TELEGRAM_UPDATE_MODE = "polling"
            main.MEDIA_SIGNING_KEY = ""
            with self.assertRaisesRegex(RuntimeError, "MEDIA_SIGNING_KEY"):
                main._validate_update_receiver_config()
        finally:
            main.TELEGRAM_UPDATE_MODE = original_mode
            main.MEDIA_SIGNING_KEY = original_key

    async def test_bonus_operation_is_idempotent_and_conflict_safe(self):
        uid = 900_000_002
        await main.upsert_member(uid, full_name="Тест")
        operation = str(uuid.uuid4())
        results = await asyncio.gather(*(
            main.add_bonus(
                uid, 77, "Проверка", by=42, operation_id=operation
            )
            for _ in range(20)
        ))
        with sqlite3.connect(main.DB_PATH) as db:
            balance = db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (uid,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT COUNT(*) FROM bonus_ledger WHERE operation_id=?",
                (operation,),
            ).fetchone()[0]
        self.assertEqual(balance, 77)
        self.assertEqual(rows, 1)
        self.assertEqual(sum(not item["replayed"] for item in results), 1)
        with self.assertRaisesRegex(ValueError, "другой операции"):
            await main.add_bonus(
                uid, 78, "Проверка", by=42, operation_id=operation
            )

    async def test_task_announcement_uses_private_ops_and_dead_retry(self):
        original_admin = main._require_admin
        await self._seed_admin(42)
        original_config = (
            main.OPS_GROUP_ID, main.OPS_GROUP_USERNAME,
            main.GROUP_ID, main.GROUP_USERNAME,
        )

        async def allow_admin(_request):
            return 42, None

        main._require_admin = allow_admin
        main.OPS_GROUP_ID = -1009000002222
        main.OPS_GROUP_USERNAME = ""
        main.GROUP_ID = -1009000001111
        main.GROUP_USERNAME = "bbbikefan"
        try:
            created = await main.api_admin_task_create(DummyRequest({
                "operation_id": str(uuid.uuid4()), "type": "fix_zone",
                "title": "Приватная парковка", "details": "Выровнять байки",
                "city": "Краснодар", "address": "Точный адрес", "reward": 80,
                "evidence_policy": "after_required", "repeatable": False,
                "announce": True,
            }))
            data = response_json(created)
            with sqlite3.connect(main.DB_PATH) as db:
                outbox = db.execute(
                    "SELECT id,chat_id,status FROM task_outbox WHERE event_key=?",
                    (f"task:{data['task_id']}:announcement",),
                ).fetchone()
                db.execute(
                    "UPDATE task_outbox SET status='sent',sent_at=?,"
                    "telegram_message_id=845,telegram_thread_id=17 WHERE id=?",
                    (main.now_iso(), outbox[0]),
                )
                db.commit()
            status_response = await main.api_admin_task_announcement_status(
                SimpleNamespace(rel_url=SimpleNamespace(query={
                    "ids": str(data["task_id"]),
                }))
            )
            invalid_status = await main.api_admin_task_announcement_status(
                SimpleNamespace(rel_url=SimpleNamespace(query={"ids": "1,bad"}))
            )
            oversized_status = await main.api_admin_task_announcement_status(
                SimpleNamespace(rel_url=SimpleNamespace(query={
                    "ids": ",".join(str(item) for item in range(1, 102)),
                }))
            )
            overlong_status = await main.api_admin_task_announcement_status(
                SimpleNamespace(rel_url=SimpleNamespace(query={"ids": "9" * 20}))
            )
            with sqlite3.connect(main.DB_PATH) as db:
                db.execute(
                    "UPDATE task_outbox SET status='dead',attempts=10 WHERE id=?",
                    (outbox[0],),
                )
                db.commit()
            retried = await main.api_admin_task_announcement_retry(DummyRequest({
                "task_id": data["task_id"], "operation_id": str(uuid.uuid4()),
            }))
        finally:
            main._require_admin = original_admin
            (
                main.OPS_GROUP_ID, main.OPS_GROUP_USERNAME,
                main.GROUP_ID, main.GROUP_USERNAME,
            ) = original_config
        self.assertEqual(data["announcement_status"], "queued")
        self.assertEqual(outbox[1], "-1009000002222")
        self.assertNotEqual(outbox[1], "-1009000001111")
        status_item = response_json(status_response)["items"][0]
        self.assertEqual(status_item["status"], "sent")
        self.assertEqual(status_item["url"], "https://t.me/c/9000002222/17/845")
        self.assertEqual(invalid_status.status, 400)
        self.assertEqual(oversized_status.status, 400)
        self.assertEqual(overlong_status.status, 400)
        self.assertEqual(response_json(retried)["status"], "pending")

    async def test_telegram_task_link_supports_private_topics_and_public_ops(self):
        self.assertEqual(
            main._telegram_message_url(-1009000002222, 845, 17),
            "https://t.me/c/9000002222/17/845",
        )
        self.assertEqual(
            main._telegram_message_url("@private_ops", 845, 17, "private_ops"),
            "https://t.me/private_ops/17/845",
        )
        self.assertEqual(main._telegram_message_url("bad", 845), "")

    async def test_withdrawal_processing_can_be_safely_handed_over(self):
        worker_id, first_admin, next_admin = 901_100_001, 901_100_002, 901_100_003
        for uid, role in ((worker_id, "helper"), (first_admin, "admin"), (next_admin, "admin")):
            await main.upsert_member(
                uid, full_name=str(uid), status="approved", role=role,
            )
        old = (main.datetime.now(main.timezone.utc) - main.timedelta(hours=2)).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            request_id = db.execute(
                "INSERT INTO withdrawal_requests "
                "(user_id,amount,status,created_at,processing_by,processing_at) "
                "VALUES (?,1000,'processing',?,?,?)",
                (worker_id, old, first_admin, old),
            ).lastrowid
            db.commit()
        original_admin = main._require_admin

        async def allow_next(_request):
            return next_admin, None

        main._require_admin = allow_next
        try:
            takeover = await main.api_admin_withdraw_handoff(DummyRequest({
                "request_id": request_id, "action": "takeover",
                "reason": "Предыдущая смена недоступна",
                "operation_id": str(uuid.uuid4()),
            }))
            overview = await main.api_admin_overview(DummyRequest({}))
            released = await main.api_admin_withdraw_handoff(DummyRequest({
                "request_id": request_id, "action": "release",
                "reason": "Передаю следующей смене",
                "operation_id": str(uuid.uuid4()),
            }))
        finally:
            main._require_admin = original_admin
        self.assertEqual(response_json(takeover)["processing_by"], next_admin)
        overview_item = next(
            item for item in response_json(overview)["withdrawals"]
            if item["id"] == request_id
        )
        self.assertEqual(overview_item["processing_name"], str(next_admin))
        self.assertEqual(overview_item["lease_state"], "held_by_me")
        self.assertGreater(overview_item["lease_remaining_seconds"], 0)
        self.assertIsNone(response_json(released)["processing_by"])
        with sqlite3.connect(main.DB_PATH) as db:
            owner = db.execute(
                "SELECT processing_by FROM withdrawal_requests WHERE id=?", (request_id,),
            ).fetchone()[0]
            events = db.execute(
                "SELECT COUNT(*) FROM withdrawal_events WHERE withdrawal_id=? "
                "AND event_type IN ('processing_taken_over','processing_released')",
                (request_id,),
            ).fetchone()[0]
        self.assertIsNone(owner)
        self.assertEqual(events, 2)

        released_public = main._withdrawal_public({
            "id": request_id,
            "status": "processing",
            "processing_by": None,
            "processing_at": None,
        }, viewer_id=next_admin)
        self.assertEqual(released_public["lease_state"], "unassigned")
        self.assertTrue(released_public["can_continue"])
        self.assertFalse(released_public["can_reject"])

    async def test_invalid_withdrawal_lease_timestamp_is_expired_and_audited(self):
        worker_id, owner_id, takeover_id = 901_110_001, 901_110_002, 901_110_003
        for uid, role in ((worker_id, "helper"), (owner_id, "admin"), (takeover_id, "admin")):
            await main.upsert_member(uid, full_name=str(uid), status="approved", role=role)
        with sqlite3.connect(main.DB_PATH) as db:
            request_id = db.execute(
                "INSERT INTO withdrawal_requests "
                "(user_id,amount,status,created_at,processing_by,processing_at) "
                "VALUES (?,1000,'processing',?,?,?)",
                (worker_id, main.now_iso(), owner_id, "not-an-iso-date"),
            ).lastrowid
            db.commit()
        original_admin = main._require_admin

        async def allow_takeover(_request):
            return takeover_id, None

        main._require_admin = allow_takeover
        try:
            response = await main.api_admin_withdraw_handoff(DummyRequest({
                "request_id": request_id, "action": "takeover",
                "reason": "Некорректный старый lease",
                "operation_id": str(uuid.uuid4()),
            }))
        finally:
            main._require_admin = original_admin
        self.assertEqual(response.status, 200)
        self.assertEqual(response_json(response)["lease_state"], "held_by_me")
        with sqlite3.connect(main.DB_PATH) as db:
            metadata = db.execute(
                "SELECT metadata_json FROM withdrawal_events WHERE withdrawal_id=? "
                "AND event_type='processing_taken_over'", (request_id,),
            ).fetchone()[0]
        self.assertTrue(json.loads(metadata)["invalid_processing_timestamp"])

    async def test_miniapp_referral_token_binds_once(self):
        referrer, newcomer, other = 901_200_001, 901_200_002, 901_200_003
        for uid in (referrer, newcomer, other):
            await main.upsert_member(uid, full_name=str(uid))
        token = "opaque-test-token"
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO referral_tokens(token,referrer_id,created_at,expires_at) "
                "VALUES (?,?,?,?)",
                (
                    token, referrer, main.now_iso(),
                    (main.datetime.now(main.timezone.utc) + main.timedelta(days=1)).isoformat(),
                ),
            )
            db.commit()
        first = await main._bind_referral_token(newcomer, "rf_" + token)
        second = await main._bind_referral_token(newcomer, "rf_" + token)
        with sqlite3.connect(main.DB_PATH) as db:
            bound = db.execute(
                "SELECT referred_by FROM members WHERE user_id=?", (newcomer,),
            ).fetchone()[0]
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(bound, referrer)

    async def test_application_resubmit_is_rejected_for_pending_and_approved(self):
        original_auth = main._auth_user
        current_uid = 901_250_001

        async def allow_user(_request):
            return {"id": current_uid, "username": "helper"}

        main._auth_user = allow_user
        try:
            for status, role in (("pending", "applicant"), ("approved", "helper")):
                current_uid += 1
                await main.upsert_member(
                    current_uid, full_name="Анна", city="Краснодар",
                    about="Помогаю с парковками", status=status, role=role,
                    applied_at=(
                        main.datetime.now(main.timezone.utc) - main.timedelta(days=2)
                    ).isoformat(),
                )
                response = await main.api_apply(DummyRequest({
                    "name": "Новое имя", "city": "Москва",
                    "about": "Хочу подать анкету ещё раз",
                }))
                self.assertEqual(response.status, 409)
        finally:
            main._auth_user = original_auth

    async def test_rejected_application_retry_has_daily_limit_and_audit_event(self):
        original_auth = main._auth_user
        current_uid = 901_260_001

        async def allow_user(_request):
            return {"id": current_uid, "username": "retry_user"}

        main._auth_user = allow_user
        try:
            await main.upsert_member(
                current_uid, full_name="Анна", city="Краснодар",
                about="Старая анкета", status="blocked", role="applicant",
                application_note="Нужно подробнее",
                applied_at=main.now_iso(),
            )
            limited = await main.api_apply(DummyRequest({
                "name": "Анна", "city": "Краснодар",
                "about": "Теперь подробно: могу поправлять парковки",
            }))
            self.assertEqual(limited.status, 429)
            self.assertGreater(response_json(limited)["retry_after"], 0)

            old = (
                main.datetime.now(main.timezone.utc) - main.timedelta(hours=25)
            ).isoformat()
            await main.upsert_member(current_uid, applied_at=old)
            retried = await main.api_apply(DummyRequest({
                "name": "Анна П.", "city": "Краснодар",
                "about": "Могу поправлять парковки и делать фотоотчёт",
            }))
        finally:
            main._auth_user = original_auth

        self.assertEqual(retried.status, 200)
        self.assertTrue(response_json(retried)["resubmitted"])
        with sqlite3.connect(main.DB_PATH) as db:
            member = db.execute(
                "SELECT status,full_name,about,application_note FROM members "
                "WHERE user_id=?", (current_uid,),
            ).fetchone()
            audit_count = db.execute(
                "SELECT COUNT(*) FROM product_events e "
                "JOIN analytics_subjects s ON s.subject_id=e.subject_id "
                "WHERE s.user_id=? AND e.event_name='application_resubmitted'",
                (current_uid,),
            ).fetchone()[0]
        self.assertEqual(
            member,
            ("pending", "Анна П.", "Могу поправлять парковки и делать фотоотчёт", ""),
        )
        self.assertEqual(audit_count, 1)

    async def test_application_limits_reject_instead_of_silently_truncating(self):
        original_auth = main._auth_user
        current_uid = 901_270_001

        async def allow_user(_request):
            return {"id": current_uid, "username": "limits_user"}

        main._auth_user = allow_user
        try:
            too_long_name = await main.api_apply(DummyRequest({
                "name": "А" * 81, "city": "Краснодар", "about": "Могу помогать",
            }))
            self.assertEqual(too_long_name.status, 400)
            self.assertEqual(response_json(too_long_name)["error"], "name_too_long")

            current_uid += 1
            too_long_about = await main.api_apply(DummyRequest({
                "name": "Анна", "city": "Краснодар", "about": "а" * 601,
            }))
            self.assertEqual(too_long_about.status, 400)
            self.assertEqual(response_json(too_long_about)["error"], "about_too_long")

            current_uid += 1
            exact_limits = await main.api_apply(DummyRequest({
                "name": "А" * 80, "city": "Краснодар", "about": "а" * 600,
            }))
        finally:
            main._auth_user = original_auth

        self.assertEqual(exact_limits.status, 200)
        with sqlite3.connect(main.DB_PATH) as db:
            stored = db.execute(
                "SELECT LENGTH(full_name),LENGTH(about) FROM members WHERE user_id=?",
                (current_uid,),
            ).fetchone()
        self.assertEqual(stored, (80, 600))

    async def test_admin_application_queue_is_paginated_and_searchable(self):
        original_admin = main._require_admin

        async def allow_admin(_request):
            return 42, None

        main._require_admin = allow_admin
        try:
            for index in range(120):
                await main.upsert_member(
                    902_000_000 + index, full_name=f"Кандидат {index}",
                    city="Краснодар" if index == 77 else "Москва",
                    about="Готов помогать", status="pending", role="applicant",
                    applied_at=main.now_iso(),
                )
            first = await main.api_admin_queue(SimpleNamespace(
                rel_url=SimpleNamespace(query={"kind": "applications", "limit": "50"})
            ))
            found = await main.api_admin_queue(SimpleNamespace(
                rel_url=SimpleNamespace(query={
                    "kind": "applications", "q": "Краснодар", "limit": "50",
                })
            ))
        finally:
            main._require_admin = original_admin
        first_data, found_data = response_json(first), response_json(found)
        self.assertEqual((len(first_data["items"]), first_data["total"]), (50, 120))
        self.assertEqual(first_data["next_cursor"], "50")
        self.assertEqual((len(found_data["items"]), found_data["total"]), (1, 1))

    async def test_admin_member_tag_filter_matches_whole_tag_only(self):
        original_admin = main._require_admin

        async def allow_admin(_request):
            return 42, None

        main._require_admin = allow_admin
        try:
            await main.upsert_member(
                902_100_001, full_name="Водитель", status="approved",
                role="helper", tags="авто, парковки",
            )
            await main.upsert_member(
                902_100_002, full_name="Фотограф", status="approved",
                role="helper", tags="то, фото",
            )
            response = await main.api_admin_members(SimpleNamespace(
                rel_url=SimpleNamespace(query={"tag": "то", "limit": "50"})
            ))
        finally:
            main._require_admin = original_admin
        data = response_json(response)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["name"], "Фотограф")

    async def test_pilot_templates_and_first_entry_links_are_unambiguous(self):
        parking = next(item for item in main.TASK_TEMPLATES if item["key"] == "parking")
        self.assertEqual(parking["evidence_policy"], "after_required")
        self.assertIn("фотоотчёт в задании", parking["details"])
        self.assertNotIn("в чат", parking["details"])
        keyboard = main._welcome_kb()
        urls = [button.url for row in keyboard.inline_keyboard for button in row]
        self.assertIn("https://t.me/BbGalterbot/bibibike", urls)
        self.assertIn("https://t.me/bbbikefan", urls)
        self.assertFalse(await main._bind_referral_token(902_100_003, "ref_902100001"))

    async def test_invalid_or_legacy_referral_link_is_explained(self):
        class FakeMessage:
            message_id = 701
            chat = SimpleNamespace(id=902_100_004, type="private")
            from_user = SimpleNamespace(
                id=902_100_004, full_name="Новый участник", username="new_helper",
            )

            def __init__(self):
                self.answers = []

            async def answer(self, text, **_kwargs):
                self.answers.append(text)

        message = FakeMessage()
        await main.start_ref(message, SimpleNamespace(args="ref_902100001"))
        self.assertTrue(any("устарела или недействительна" in text for text in message.answers))
        member = await main.get_member(message.from_user.id)
        self.assertIsNone(member["referred_by"])
        with sqlite3.connect(main.DB_PATH) as db:
            event = db.execute(
                "SELECT e.properties_json FROM product_events e "
                "JOIN analytics_subjects s ON s.subject_id=e.subject_id "
                "WHERE e.event_name='referral_link_invalid' AND s.user_id=?",
                (message.from_user.id,),
            ).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(json.loads(event[0])["referral_format"], "legacy")

    async def test_task_create_retry_returns_one_task(self):
        original = main._require_admin
        await self._seed_admin(42)

        async def allow_admin(_request):
            return 42, None

        main._require_admin = allow_admin
        operation = str(uuid.uuid4())
        body = {
            "operation_id": operation,
            "type": "fix_zone",
            "title": "Поправить парковку",
            "details": "Освободить проход",
            "city": "Краснодар",
            "address": "ул. Красная, 1",
            "reward": 80,
            "evidence_policy": "after_required",
            "max_participants": 1,
            "budget_cap": 80,
            "repeatable": False,
            "announce": False,
            "slot_start": (main.datetime.now(main.timezone.utc) + main.timedelta(hours=1)).isoformat(),
            "slot_end": (main.datetime.now(main.timezone.utc) + main.timedelta(days=1)).isoformat(),
        }
        try:
            first = await main.api_admin_task_create(DummyRequest(body))
            real_datetime = main.datetime

            class AfterDeadline(real_datetime):
                @classmethod
                def now(cls, tz=None):
                    return real_datetime.now(tz) + main.timedelta(days=2)

            with patch.object(main, "datetime", AfterDeadline):
                second = await main.api_admin_task_create(DummyRequest(body))
        finally:
            main._require_admin = original
        first_data, second_data = response_json(first), response_json(second)
        self.assertEqual(first.status, 200)
        self.assertEqual(first_data["task_id"], second_data["task_id"])
        self.assertTrue(second_data["idempotent"])
        with sqlite3.connect(main.DB_PATH) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE operation_id=?", (operation,)
            ).fetchone()[0]
        self.assertEqual(count, 1)
        conflict = dict(body, title="Другое задание")
        original = main._require_admin
        main._require_admin = allow_admin
        try:
            conflict_response = await main.api_admin_task_create(
                DummyRequest(conflict)
            )
        finally:
            main._require_admin = original
        self.assertEqual(conflict_response.status, 409)

    async def test_task_with_expired_deadline_is_rejected_before_publish(self):
        original = main._require_admin

        async def allow_admin(_request):
            return 42, None

        main._require_admin = allow_admin
        past_end = main.datetime.now(main.timezone.utc) - main.timedelta(minutes=1)
        body = {
            "operation_id": str(uuid.uuid4()),
            "type": "fix_zone",
            "title": "Просроченная парковка",
            "city": "Краснодар",
            "address": "ул. Красная, 1",
            "reward": 80,
            "evidence_policy": "after_required",
            "repeatable": False,
            "announce": True,
            "slot_start": (past_end - main.timedelta(hours=1)).isoformat(),
            "slot_end": past_end.isoformat(),
        }
        try:
            response = await main.api_admin_task_create(DummyRequest(body))
        finally:
            main._require_admin = original
        self.assertEqual(response.status, 400)
        self.assertEqual(response_json(response)["error"], "slot_expired")
        with sqlite3.connect(main.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    async def test_photo_report_is_linked_to_claimed_task(self):
        uid = 900_000_003
        await main.upsert_member(
            uid, full_name="Исполнитель", city="Краснодар",
            status="approved", role="helper",
        )
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks "
                "(type,title,city,address,reward,status,created_at,evidence_policy) "
                "VALUES ('fix_zone','Парковка','Краснодар','ул. Красная',80,'closed',?,?)",
                (main.now_iso(), "photo_required"),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,reward_snapshot) "
                "VALUES (?,?,'claimed',?,80)",
                (task_id, uid, main.now_iso()),
            ).lastrowid
            db.commit()

        original_worker, original_notify = main._require_worker, main._notify_admins

        async def allow_worker(_request):
            return uid, None

        main._require_worker = allow_worker
        main._notify_admins = lambda *_args, **_kwargs: None
        image_bytes = BytesIO()
        Image.new("RGB", (2, 2), "green").save(image_bytes, format="PNG")
        png = base64.b64encode(image_bytes.getvalue()).decode()
        completion_operation = str(uuid.uuid4())
        completion_body = {
            "task_id": task_id,
            "assignment_id": assignment_id,
            "note": "Готово",
            "proof_photos": ["data:image/png;base64," + png],
            "operation_id": completion_operation,
        }
        try:
            response = await main.api_task_complete(DummyRequest(completion_body))
            replay = await main.api_task_complete(DummyRequest(completion_body))
            conflict = await main.api_task_complete(DummyRequest({
                **completion_body, "note": "Другой отчёт",
            }))
        finally:
            main._require_worker = original_worker
            main._notify_admins = original_notify
        self.assertEqual(response.status, 200)
        self.assertEqual(replay.status, 200)
        self.assertTrue(response_json(replay)["idempotent"])
        self.assertEqual(conflict.status, 409)
        with sqlite3.connect(main.DB_PATH) as db:
            status = db.execute(
                "SELECT status FROM task_assignments WHERE id=?", (assignment_id,)
            ).fetchone()[0]
            evidence = db.execute(
                "SELECT kind,user_id,media_id FROM task_evidence WHERE task_id=?",
                (task_id,),
            ).fetchall()
        self.assertEqual(status, "review")
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0][:2], ("after", uid))
        self.assertTrue(evidence[0][2])
        with sqlite3.connect(main.DB_PATH) as db:
            stored_assignment = db.execute(
                "SELECT assignment_id FROM task_evidence WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
        self.assertEqual(stored_assignment, assignment_id)

    async def test_admin_cannot_approve_own_execution(self):
        admin_id = 900_000_004
        await main.upsert_member(
            admin_id, full_name="Администратор", city="Краснодар",
            status="approved", role="admin",
        )
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks "
                "(type,title,city,address,reward,status,created_by,created_at) "
                "VALUES ('fix_zone','Самопроверка','Краснодар','Адрес',80,'closed',?,?)",
                (42, main.now_iso()),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,reward_snapshot) "
                "VALUES (?,?,'review',?,80)",
                (task_id, admin_id, main.now_iso()),
            ).lastrowid
            db.commit()

        original_admin = main._require_admin

        async def allow_admin(_request):
            return admin_id, None

        main._require_admin = allow_admin
        try:
            response = await main.api_admin_task_approve(DummyRequest({
                "task_id": task_id, "assignment_id": assignment_id,
                "approve": True, "operation_id": str(uuid.uuid4()),
            }))
        finally:
            main._require_admin = original_admin
        self.assertEqual(response.status, 403)
        with sqlite3.connect(main.DB_PATH) as db:
            bonus, status = db.execute(
                "SELECT m.bonus,a.status FROM members m "
                "JOIN task_assignments a ON a.id=? WHERE m.user_id=?",
                (assignment_id, admin_id),
            ).fetchone()
        self.assertEqual((bonus, status), (0, "review"))

    async def test_admin_can_finally_reject_without_payout(self):
        worker_id, admin_id = 905_000_001, 905_000_002
        await self._seed_admin(admin_id)
        await main.upsert_member(
            worker_id, full_name="Исполнитель", city="Краснодар",
            status="approved", role="helper",
        )
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks "
                "(type,title,city,address,reward,status,created_by,created_at) "
                "VALUES ('fix_zone','Отклонение','Краснодар','Адрес',60,'closed',999,?)",
                (main.now_iso(),),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,reward_snapshot,completion_operation_id) "
                "VALUES (?,?,'review',?,60,?)",
                (task_id, worker_id, main.now_iso(), str(uuid.uuid4())),
            ).lastrowid
            db.commit()
        original_admin = main._require_admin

        async def allow_admin(_request):
            return admin_id, None

        main._require_admin = allow_admin
        try:
            response = await main.api_admin_task_approve(DummyRequest({
                "task_id": task_id, "assignment_id": assignment_id,
                "decision": "reject", "note": "Результат не соответствует заданию",
                "operation_id": str(uuid.uuid4()),
            }))
        finally:
            main._require_admin = original_admin
        self.assertEqual(response.status, 200)
        with sqlite3.connect(main.DB_PATH) as db:
            status = db.execute(
                "SELECT status FROM task_assignments WHERE id=?", (assignment_id,),
            ).fetchone()[0]
            bonus = db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (worker_id,),
            ).fetchone()[0]
            ledger = db.execute(
                "SELECT COUNT(*) FROM bonus_ledger WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()[0]
        self.assertEqual((status, bonus, ledger), ("rejected", 0, 0))

    async def test_review_decision_is_idempotent_under_concurrency(self):
        worker_id, admin_id = 906_000_001, 906_000_002
        await self._seed_admin(admin_id)
        await main.upsert_member(
            worker_id, full_name="Исполнитель", status="approved", role="helper",
        )
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks (type,title,city,address,reward,status,created_by,created_at) "
                "VALUES ('fix_zone','Проверка','Краснодар','Адрес',90,'closed',999,?)",
                (main.now_iso(),),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,reward_snapshot) "
                "VALUES (?,?,'review',?,90)",
                (task_id, worker_id, main.now_iso()),
            ).lastrowid
            db.commit()
        original_admin = main._require_admin

        async def allow_admin(_request):
            return admin_id, None

        main._require_admin = allow_admin
        operation_id = str(uuid.uuid4())
        body = {
            "task_id": task_id, "assignment_id": assignment_id,
            "decision": "approve", "operation_id": operation_id,
        }
        try:
            results = await asyncio.gather(*(
                main.api_admin_task_approve(DummyRequest(body)) for _ in range(20)
            ))
            conflict = await main.api_admin_task_approve(DummyRequest({
                **body, "decision": "reject", "note": "Иной результат",
            }))
        finally:
            main._require_admin = original_admin
        self.assertTrue(all(response.status == 200 for response in results))
        self.assertEqual(conflict.status, 409)
        self.assertEqual(sum(not response_json(r)["idempotent"] for r in results), 1)
        with sqlite3.connect(main.DB_PATH) as db:
            balance = db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (worker_id,),
            ).fetchone()[0]
            ledger = db.execute(
                "SELECT COUNT(*) FROM bonus_ledger WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()[0]
            commands = db.execute(
                "SELECT COUNT(*) FROM task_review_commands WHERE operation_id=?",
                (operation_id,),
            ).fetchone()[0]
        self.assertEqual((balance, ledger, commands), (90, 1, 1))

    async def test_completion_replay_survives_revision(self):
        worker_id, admin_id = 906_100_001, 906_100_002
        await self._seed_admin(admin_id)
        await main.upsert_member(worker_id, full_name="Исполнитель", status="approved")
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks "
                "(type,title,city,address,reward,status,created_by,created_at,evidence_policy) "
                "VALUES ('fix_zone','Повтор','Краснодар','Адрес',30,'closed',999,?,'comment_only')",
                (main.now_iso(),),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,reward_snapshot) "
                "VALUES (?,?,'claimed',?,30)",
                (task_id, worker_id, main.now_iso()),
            ).lastrowid
            db.commit()
        original_worker, original_admin = main._require_worker, main._require_admin

        async def allow_worker(_request):
            return worker_id, None

        async def allow_admin(_request):
            return admin_id, None

        main._require_worker, main._require_admin = allow_worker, allow_admin
        completion = {
            "task_id": task_id, "assignment_id": assignment_id,
            "note": "Работа выполнена", "proof_photos": [],
            "operation_id": str(uuid.uuid4()),
        }
        try:
            submitted = await main.api_task_complete(DummyRequest(completion))
            revised = await main.api_admin_task_approve(DummyRequest({
                "task_id": task_id, "assignment_id": assignment_id,
                "decision": "revise", "note": "Нужно поправить ещё раз",
                "operation_id": str(uuid.uuid4()),
            }))
            replay = await main.api_task_complete(DummyRequest(completion))
        finally:
            main._require_worker, main._require_admin = original_worker, original_admin
        self.assertEqual((submitted.status, revised.status, replay.status), (200, 200, 200))
        self.assertTrue(response_json(replay)["idempotent"])
        with sqlite3.connect(main.DB_PATH) as db:
            status = db.execute(
                "SELECT status FROM task_assignments WHERE id=?", (assignment_id,),
            ).fetchone()[0]
        self.assertEqual(status, "claimed")

    async def test_legacy_completion_command_is_backfilled_before_revision(self):
        worker_id, admin_id = 906_200_001, 906_200_002
        await self._seed_admin(admin_id)
        await main.upsert_member(worker_id, full_name="Исполнитель", status="approved")
        operation_id = str(uuid.uuid4())
        note = "Старый принятый отчёт"
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks "
                "(type,title,city,address,reward,status,created_by,created_at,evidence_policy) "
                "VALUES ('fix_zone','Миграция','Краснодар','Адрес',30,'closed',999,?,'comment_only')",
                (main.now_iso(),),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,done_at,reward_snapshot,"
                "completion_operation_id,completion_request_hash) "
                "VALUES (?,?,'review',?,?,30,?,NULL)",
                (task_id, worker_id, main.now_iso(), main.now_iso(), operation_id),
            ).lastrowid
            request_hash = main._request_fingerprint({
                "task_id": task_id, "assignment_id": assignment_id,
                "user_id": worker_id, "note": note, "proof_photos": [],
            })
            db.execute(
                "UPDATE task_assignments SET completion_request_hash=? WHERE id=?",
                (request_hash, assignment_id),
            )
            db.commit()
        await main.init_db()
        original_worker, original_admin = main._require_worker, main._require_admin

        async def allow_worker(_request):
            return worker_id, None

        async def allow_admin(_request):
            return admin_id, None

        main._require_worker, main._require_admin = allow_worker, allow_admin
        try:
            revised = await main.api_admin_task_approve(DummyRequest({
                "task_id": task_id, "assignment_id": assignment_id,
                "decision": "revise", "note": "Нужно поправить",
                "operation_id": str(uuid.uuid4()),
            }))
            replay = await main.api_task_complete(DummyRequest({
                "task_id": task_id, "assignment_id": assignment_id,
                "note": note, "proof_photos": [], "operation_id": operation_id,
            }))
        finally:
            main._require_worker, main._require_admin = original_worker, original_admin
        self.assertEqual((revised.status, replay.status), (200, 200))
        self.assertTrue(response_json(replay)["idempotent"])

    async def test_revision_deadline_expires_even_when_task_already_expired(self):
        worker_id, admin_id = 907_000_001, 907_000_002
        await self._seed_admin(admin_id)
        await main.upsert_member(worker_id, full_name="Исполнитель", status="approved")
        past = (main.datetime.now(main.timezone.utc) - main.timedelta(minutes=2)).isoformat()
        future = (main.datetime.now(main.timezone.utc) + main.timedelta(minutes=2)).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks (type,title,city,address,reward,status,created_by,created_at,slot_end) "
                "VALUES ('fix_zone','Доработка','Краснодар','Адрес',40,'expired',999,?,?)",
                (main.now_iso(), past),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,reward_snapshot,due_at) "
                "VALUES (?,?,'review',?,40,?)",
                (task_id, worker_id, main.now_iso(), past),
            ).lastrowid
            db.commit()
        original_admin = main._require_admin

        async def allow_admin(_request):
            return admin_id, None

        main._require_admin = allow_admin
        try:
            revised = await main.api_admin_task_approve(DummyRequest({
                "task_id": task_id, "assignment_id": assignment_id,
                "decision": "revise", "note": "Исправить расположение",
                "revision_due_at": future, "operation_id": str(uuid.uuid4()),
            }))
        finally:
            main._require_admin = original_admin
        self.assertEqual(revised.status, 200)
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "UPDATE task_assignments SET revision_due_at=? WHERE id=?",
                (past, assignment_id),
            )
            db.commit()
        await main._expire_due_tasks()
        with sqlite3.connect(main.DB_PATH) as db:
            status = db.execute(
                "SELECT status FROM task_assignments WHERE id=?", (assignment_id,),
            ).fetchone()[0]
        self.assertEqual(status, "expired")

    async def test_completed_one_off_task_cannot_be_cancelled(self):
        admin_id = 908_000_001
        await self._seed_admin(admin_id)
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks (type,title,city,address,reward,status,repeatable,created_at) "
                "VALUES ('fix_zone','Готово','Краснодар','Адрес',50,'closed',0,?)",
                (main.now_iso(),),
            ).lastrowid
            db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,reward_snapshot) "
                "VALUES (?,123,'done',?,50)",
                (task_id, main.now_iso()),
            )
            db.commit()
        original_admin = main._require_admin

        async def allow_admin(_request):
            return admin_id, None

        main._require_admin = allow_admin
        try:
            response = await main.api_admin_task_cancel(DummyRequest({
                "task_id": task_id, "reason": "Больше не нужно",
                "operation_id": str(uuid.uuid4()),
            }))
        finally:
            main._require_admin = original_admin
        self.assertEqual(response.status, 409)
        with sqlite3.connect(main.DB_PATH) as db:
            status = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        self.assertEqual(status, "closed")

    async def test_repeatable_capacity_release_and_reclaim(self):
        users = [910_000_001, 910_000_002, 910_000_003]
        for uid in users:
            await main.upsert_member(
                uid, full_name=f"Участник {uid}", city="Краснодар",
                status="approved", role="helper",
            )
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks "
                "(type,title,city,address,reward,status,created_at,repeatable,"
                "max_participants,budget_cap) "
                "VALUES ('fix_zone','Массовое','Краснодар','Адрес',80,'open',?,1,2,160)",
                (main.now_iso(),),
            ).lastrowid
            db.commit()
        original_worker = main._require_worker

        async def worker_from_request(request):
            return request.uid, None

        main._require_worker = worker_from_request
        try:
            results = await asyncio.gather(*(
                main.api_task_claim(DummyRequest({"task_id": task_id}, uid))
                for uid in users
            ))
            self.assertEqual(sorted(r.status for r in results), [200, 200, 409])
            winner = users[[r.status for r in results].index(200)]
            release_operation = str(uuid.uuid4())
            body = {
                "task_id": task_id, "reason": "Изменились планы",
                "operation_id": release_operation,
            }
            released = await main.api_task_release(DummyRequest(body, winner))
            replay = await main.api_task_release(DummyRequest(body, winner))
            conflict = await main.api_task_release(DummyRequest({
                **body, "reason": "Другая причина",
            }, winner))
            self.assertEqual((released.status, replay.status, conflict.status), (200, 200, 409))
            loser = users[[r.status for r in results].index(409)]
            reclaimed = await main.api_task_claim(
                DummyRequest({"task_id": task_id}, loser)
            )
            self.assertEqual(reclaimed.status, 200)
        finally:
            main._require_worker = original_worker
        with sqlite3.connect(main.DB_PATH) as db:
            committed = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(reward_snapshot),0) "
                "FROM task_assignments WHERE task_id=? "
                "AND status IN ('claimed','review','done')", (task_id,),
            ).fetchone()
            release_events = db.execute(
                "SELECT COUNT(*) FROM product_events WHERE event_name='task_released'"
            ).fetchone()[0]
        self.assertEqual(committed, (2, 160))
        self.assertEqual(release_events, 1)

    async def test_cancel_blocks_review_then_is_idempotent(self):
        worker_id, admin_id = 920_000_001, 920_000_002
        await main.upsert_member(
            worker_id, full_name="Исполнитель", city="Краснодар",
            status="approved", role="helper",
        )
        await main.upsert_member(
            admin_id, full_name="Ответственный", city="Краснодар",
            status="approved", role="admin",
        )
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks (type,title,city,address,reward,status,created_at) "
                "VALUES ('fix_zone','Отмена','Краснодар','Адрес',50,'closed',?)",
                (main.now_iso(),),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,reward_snapshot) "
                "VALUES (?,?,'review',?,50)",
                (task_id, worker_id, main.now_iso()),
            ).lastrowid
            db.commit()
        original_admin = main._require_admin

        async def allow_admin(_request):
            return admin_id, None

        main._require_admin = allow_admin
        operation = str(uuid.uuid4())
        body = {"task_id": task_id, "reason": "Больше не требуется", "operation_id": operation}
        try:
            blocked = await main.api_admin_task_cancel(DummyRequest(body))
            self.assertEqual(blocked.status, 409)
            with sqlite3.connect(main.DB_PATH) as db:
                db.execute(
                    "UPDATE task_assignments SET status='claimed' WHERE id=?",
                    (assignment_id,),
                )
                db.commit()
            cancelled = await main.api_admin_task_cancel(DummyRequest(body))
            replay = await main.api_admin_task_cancel(DummyRequest(body))
        finally:
            main._require_admin = original_admin
        self.assertEqual((cancelled.status, replay.status), (200, 200))
        self.assertTrue(response_json(replay)["idempotent"])
        with sqlite3.connect(main.DB_PATH) as db:
            task_status = db.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,),
            ).fetchone()[0]
            assignment_status = db.execute(
                "SELECT status FROM task_assignments WHERE id=?", (assignment_id,),
            ).fetchone()[0]
            ledger = db.execute(
                "SELECT COUNT(*) FROM bonus_ledger WHERE task_id=?", (task_id,),
            ).fetchone()[0]
        self.assertEqual((task_status, assignment_status, ledger), ("cancelled", "cancelled", 0))

    async def test_deadline_expires_claim_without_touching_review(self):
        past = "2020-01-01T00:00:00+00:00"
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks "
                "(type,title,city,address,reward,status,created_at,slot_start,slot_end,repeatable,"
                "max_participants,budget_cap) "
                "VALUES ('fix_zone','Просрочено','Краснодар','Адрес',40,'open',?,"
                "'2019-01-01T00:00:00+00:00',?,1,3,120)",
                (main.now_iso(), past),
            ).lastrowid
            claimed_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,reward_snapshot,due_at) "
                "VALUES (?,1,'claimed',?,40,?)",
                (task_id, main.now_iso(), past),
            ).lastrowid
            review_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,reward_snapshot,due_at) "
                "VALUES (?,2,'review',?,40,?)",
                (task_id, main.now_iso(), past),
            ).lastrowid
            db.commit()
        await main._expire_due_tasks()
        with sqlite3.connect(main.DB_PATH) as db:
            task_status = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
            statuses = dict(db.execute(
                "SELECT id,status FROM task_assignments WHERE id IN (?,?)",
                (claimed_id, review_id),
            ).fetchall())
        self.assertEqual(task_status, "expired")
        self.assertEqual(statuses, {claimed_id: "expired", review_id: "review"})

    async def test_encrypted_withdrawal_full_flow_is_idempotent(self):
        worker_id, admin_id = 930_000_001, 930_000_002
        await main.upsert_member(
            worker_id, full_name="Получатель", city="Краснодар",
            status="approved", role="helper", bonus=1500,
        )
        await main.upsert_member(
            admin_id, full_name="Кассир", city="Краснодар",
            status="approved", role="admin",
        )
        original_worker, original_admin = main._require_worker, main._require_admin

        async def allow_worker(_request):
            return worker_id, None

        async def allow_admin(_request):
            return admin_id, None

        main._require_worker, main._require_admin = allow_worker, allow_admin
        request_operation = str(uuid.uuid4())
        request_body = {
            "amount": 1000, "account_ref": "BB-ACCOUNT-7788",
            "operation_id": request_operation,
        }
        try:
            created = await main.api_withdraw_request(DummyRequest(request_body))
            replay = await main.api_withdraw_request(DummyRequest(request_body))
            changed = await main.api_withdraw_request(DummyRequest({
                **request_body, "account_ref": "BB-ACCOUNT-9999",
            }))
            request_id = response_json(created)["request_id"]
            reveal = await main.api_admin_withdraw_account(DummyRequest({
                "request_id": request_id,
            }))
            renewed = await main.api_admin_withdraw_account(DummyRequest({
                "request_id": request_id,
            }))
            decision_operation = str(uuid.uuid4())
            decision_body = {
                "request_id": request_id, "decision": "approve",
                "external_reference": "TX-2026-0001",
                "operation_id": decision_operation,
            }
            decided = await main.api_admin_withdraw_decide(DummyRequest(decision_body))
            decision_replay = await main.api_admin_withdraw_decide(DummyRequest(decision_body))
            decision_changed = await main.api_admin_withdraw_decide(DummyRequest({
                **decision_body, "external_reference": "TX-CHANGED",
            }))
            with sqlite3.connect(main.DB_PATH) as db:
                second_request_id = db.execute(
                    "INSERT INTO withdrawal_requests "
                    "(user_id,amount,status,created_at,account_ciphertext,account_masked,"
                    "processing_by,processing_at) "
                    "VALUES (?,?, 'processing', ?, ?, ?, ?, ?)",
                    (
                        930_000_003, 1000, main.now_iso(),
                        main._encrypt_account_ref("BB-SECOND-ACCOUNT"), "BB••••NT",
                        admin_id, main.now_iso(),
                    ),
                ).lastrowid
                db.commit()
            canonical_conflict = await main.api_admin_withdraw_decide(DummyRequest({
                "request_id": second_request_id, "decision": "approve",
                "external_reference": "tx-2026-0001",
                "operation_id": str(uuid.uuid4()),
            }))
        finally:
            main._require_worker, main._require_admin = original_worker, original_admin
        self.assertEqual((created.status, replay.status, changed.status), (200, 200, 409))
        self.assertEqual(response_json(reveal)["account_ref"], "BB-ACCOUNT-7788")
        self.assertEqual(response_json(renewed)["lease_state"], "held_by_me")
        self.assertGreater(response_json(renewed)["lease_remaining_seconds"], 0)
        self.assertEqual((decided.status, decision_replay.status, decision_changed.status), (200, 200, 409))
        self.assertEqual(canonical_conflict.status, 409)
        with sqlite3.connect(main.DB_PATH) as db:
            row = db.execute(
                "SELECT status,account_ciphertext,account_masked,external_reference "
                "FROM withdrawal_requests WHERE id=?", (request_id,),
            ).fetchone()
            balance = db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (worker_id,),
            ).fetchone()[0]
            reserve_rows = db.execute(
                "SELECT COUNT(*) FROM bonus_ledger WHERE withdrawal_id=?", (request_id,),
            ).fetchone()[0]
            renew_events = db.execute(
                "SELECT COUNT(*) FROM withdrawal_events WHERE withdrawal_id=? "
                "AND event_type='processing_renewed'", (request_id,),
            ).fetchone()[0]
        self.assertEqual(row[0], "completed")
        self.assertNotIn("BB-ACCOUNT-7788", row[1])
        self.assertNotEqual(row[2], "BB-ACCOUNT-7788")
        self.assertEqual(row[3], "TX-2026-0001")
        self.assertEqual((balance, reserve_rows), (500, 1))
        self.assertEqual(renew_events, 1)

    async def test_withdrawal_rejection_refunds_exactly_once(self):
        worker_id, admin_id = 940_000_001, 940_000_002
        await self._seed_admin(admin_id)
        await main.upsert_member(
            worker_id, full_name="Возврат", city="Краснодар",
            status="approved", role="helper", bonus=1200,
        )
        original_worker, original_admin = main._require_worker, main._require_admin

        async def allow_worker(_request):
            return worker_id, None

        async def allow_admin(_request):
            return admin_id, None

        main._require_worker, main._require_admin = allow_worker, allow_admin
        try:
            created = await main.api_withdraw_request(DummyRequest({
                "amount": 1000, "account_ref": "BB-REFUND-100",
                "operation_id": str(uuid.uuid4()),
            }))
            request_id = response_json(created)["request_id"]
            operation = str(uuid.uuid4())
            body = {
                "request_id": request_id, "decision": "reject",
                "note": "Аккаунт не найден", "operation_id": operation,
            }
            rejected = await main.api_admin_withdraw_decide(DummyRequest(body))
            replay = await main.api_admin_withdraw_decide(DummyRequest(body))
            conflict = await main.api_admin_withdraw_decide(DummyRequest({
                **body, "note": "Другая причина",
            }))
        finally:
            main._require_worker, main._require_admin = original_worker, original_admin
        self.assertEqual((rejected.status, replay.status, conflict.status), (200, 200, 409))
        with sqlite3.connect(main.DB_PATH) as db:
            balance = db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (worker_id,),
            ).fetchone()[0]
            ledger = db.execute(
                "SELECT amount FROM bonus_ledger WHERE withdrawal_id=? ORDER BY id",
                (request_id,),
            ).fetchall()
            status = db.execute(
                "SELECT status FROM withdrawal_requests WHERE id=?", (request_id,),
            ).fetchone()[0]
        self.assertEqual(balance, 1200)
        self.assertEqual(ledger, [(-1000,), (1000,)])
        self.assertEqual(status, "rejected_refunded")

    async def test_analytics_rejects_personal_free_text(self):
        async with main.aiosqlite.connect(main.DB_PATH, timeout=15) as db:
            await db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "Недопустимые свойства"):
                await main._track_event_in_tx(
                    db, "task_claimed", "backend", user_id=1,
                    properties={"name": "Нельзя хранить"},
                )
            await db.rollback()
        with sqlite3.connect(main.DB_PATH) as db:
            leaks = db.execute(
                "SELECT COUNT(*) FROM product_events "
                "WHERE properties_json LIKE '%Нельзя хранить%'"
            ).fetchone()[0]
        self.assertEqual(leaks, 0)

    async def test_analytics_dedupe_never_persists_raw_telegram_ids(self):
        raw_key = "group_join:-1001234567890:987654321"
        async with main.aiosqlite.connect(main.DB_PATH, timeout=15) as db:
            await main._track_event_in_tx(
                db, "group_member_joined", "group", user_id=987654321,
                dedupe_key=raw_key,
            )
            await db.commit()
        with sqlite3.connect(main.DB_PATH) as db:
            stored = db.execute(
                "SELECT dedupe_key FROM product_events WHERE event_name='group_member_joined'"
            ).fetchone()[0]
        self.assertTrue(stored.startswith("h1:"))
        self.assertNotIn("987654321", stored)
        self.assertNotIn("1001234567890", stored)

    async def test_legacy_raw_analytics_dedupe_is_removed_on_migration(self):
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO product_events "
                "(event_id,occurred_at,event_name,source,dedupe_key,expires_at) "
                "VALUES ('legacy-raw',?,'bot_started','bot','start:987654321',?)",
                (
                    main.now_iso(),
                    (main.datetime.now(main.timezone.utc) + main.timedelta(days=1)).isoformat(),
                ),
            )
            db.commit()
        await main.init_db()
        with sqlite3.connect(main.DB_PATH) as db:
            stored = db.execute(
                "SELECT dedupe_key FROM product_events WHERE event_id='legacy-raw'"
            ).fetchone()[0]
        self.assertIsNone(stored)

    async def test_completed_withdrawal_account_is_purged_after_retention(self):
        old = (
            main.datetime.now(main.timezone.utc)
            - main.timedelta(days=main.WITHDRAW_ACCOUNT_RETENTION_DAYS + 1)
        ).isoformat()
        ciphertext = main._encrypt_account_ref("BB-ACCOUNT-SECRET")
        with sqlite3.connect(main.DB_PATH) as db:
            request_id = db.execute(
                "INSERT INTO withdrawal_requests "
                "(user_id,amount,status,created_at,decided_at,account_ciphertext,"
                "account_masked,account_fingerprint) VALUES (1,1000,'completed',?,?,?,?,?)",
                (
                    old, old, ciphertext, "BB••••ET",
                    main._account_fingerprint("BB-ACCOUNT-SECRET"),
                ),
            ).lastrowid
            db.commit()
        await main.cleanup_expired_analytics()
        with sqlite3.connect(main.DB_PATH) as db:
            row = db.execute(
                "SELECT account_ciphertext,account_purged_at FROM withdrawal_requests WHERE id=?",
                (request_id,),
            ).fetchone()
            events = db.execute(
                "SELECT COUNT(*) FROM withdrawal_events "
                "WHERE withdrawal_id=? AND event_type='account_purged'",
                (request_id,),
            ).fetchone()[0]
        self.assertIsNone(row[0])
        self.assertTrue(row[1])
        self.assertEqual(events, 1)

    async def test_analytics_retention_removes_event_and_orphan_subject(self):
        past = (main.datetime.now(main.timezone.utc) - main.timedelta(days=1)).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "INSERT INTO analytics_subjects (subject_id,user_id,created_at) "
                "VALUES ('subject-old',123,?)", (past,),
            )
            db.execute(
                "INSERT INTO product_events "
                "(event_id,occurred_at,event_name,source,subject_id,expires_at) "
                "VALUES ('event-old',?,'bot_started','bot','subject-old',?)",
                (past, past),
            )
            db.commit()
        await main.cleanup_expired_analytics()
        with sqlite3.connect(main.DB_PATH) as db:
            events = db.execute(
                "SELECT COUNT(*) FROM product_events WHERE event_id='event-old'"
            ).fetchone()[0]
            subjects = db.execute(
                "SELECT COUNT(*) FROM analytics_subjects WHERE subject_id='subject-old'"
            ).fetchone()[0]
        self.assertEqual((events, subjects), (0, 0))

    async def test_best_effort_analytics_never_breaks_user_flow(self):
        original = main._track_event

        async def broken(*_args, **_kwargs):
            raise RuntimeError("analytics unavailable")

        main._track_event = broken
        try:
            await main._track_event_best_effort(
                "bot_started", "bot", user_id=123, dedupe_key="start:123"
            )
        finally:
            main._track_event = original

    async def test_outbox_stays_sent_when_publication_analytics_fails(self):
        with sqlite3.connect(main.DB_PATH) as db:
            outbox_id = db.execute(
                "INSERT INTO task_outbox "
                "(event_key,event_type,chat_id,payload_json,status,attempts,available_at,created_at) "
                "VALUES ('test-publish','group_task','@test',?,'pending',0,?,?)",
                (
                    main.json.dumps({
                        "text": "Задание", "task_id": 77,
                        "admin_id": 1, "operation_id": str(uuid.uuid4()),
                    }),
                    main.now_iso(), main.now_iso(),
                ),
            ).lastrowid
            db.commit()
        original_deliver, original_track = main._deliver_outbox_item, main._track_event

        async def delivered(_item):
            return SimpleNamespace(message_id=845, message_thread_id=17)

        async def broken(*_args, **_kwargs):
            raise RuntimeError("analytics unavailable")

        main._deliver_outbox_item, main._track_event = delivered, broken
        worker = asyncio.create_task(main.outbox_worker())
        try:
            status = "pending"
            receipt = (None, None)
            for _ in range(100):
                with sqlite3.connect(main.DB_PATH) as db:
                    status, *receipt = db.execute(
                        "SELECT status,telegram_message_id,telegram_thread_id "
                        "FROM task_outbox WHERE id=?", (outbox_id,),
                    ).fetchone()
                if status == "sent":
                    break
                await asyncio.sleep(0.01)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            main._deliver_outbox_item, main._track_event = original_deliver, original_track
        self.assertEqual(status, "sent")
        self.assertEqual(tuple(receipt), (845, 17))

    async def test_referral_confirmation_records_exactly_one_event(self):
        referrer_id, referred_id = 960_000_001, 960_000_002
        await main.upsert_member(
            referrer_id, full_name="Пригласивший", status="approved", role="helper",
        )
        await main.upsert_member(
            referred_id, full_name="Друг", status="approved", role="helper",
            referred_by=referrer_id, ref_confirmed=0,
        )
        original_notify = main._notify
        main._notify = lambda *_args, **_kwargs: None
        try:
            first = await main.confirm_referral(referred_id)
            replay = await main.confirm_referral(referred_id)
        finally:
            main._notify = original_notify
        self.assertEqual(first[0], referrer_id)
        self.assertEqual(replay, (referrer_id, 0, 0))
        with sqlite3.connect(main.DB_PATH) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM product_events "
                "WHERE event_name='referral_confirmed'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    async def test_legacy_assignment_unique_migrates_without_data_loss(self):
        legacy_root = TEST_ROOT / (self.id().rsplit(".", 1)[-1] + "_legacy")
        legacy_root.mkdir(parents=True, exist_ok=True)
        legacy_db = legacy_root / "legacy.db"
        with sqlite3.connect(legacy_db) as db:
            db.execute("""
                CREATE TABLE task_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'claimed',
                    claimed_at TEXT NOT NULL,
                    done_at TEXT,
                    proof_note TEXT,
                    review_note TEXT,
                    completion_operation_id TEXT,
                    completion_request_hash TEXT,
                    submission_attempt INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(task_id,user_id)
                )
            """)
            db.execute(
                "INSERT INTO task_assignments "
                "(id,task_id,user_id,status,claimed_at) VALUES (7,77,88,'claimed',?)",
                (main.now_iso(),),
            )
            db.commit()
        original_path = main.DB_PATH
        main.DB_PATH = str(legacy_db)
        try:
            await main.init_db()
        finally:
            main.DB_PATH = original_path
        with sqlite3.connect(legacy_db) as db:
            preserved = db.execute(
                "SELECT id,task_id,user_id,status FROM task_assignments WHERE id=7"
            ).fetchone()
            table_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_assignments'"
            ).fetchone()[0]
        self.assertEqual(preserved, (7, 77, 88, "claimed"))
        self.assertNotIn("UNIQUE(task_id,user_id)", table_sql.replace(" ", ""))

    async def test_legacy_task_reward_is_backfilled_to_canonical_assignment(self):
        legacy_root = TEST_ROOT / (self.id().rsplit(".", 1)[-1] + "_reward")
        legacy_root.mkdir(parents=True, exist_ok=True)
        legacy_db = legacy_root / "legacy_reward.db"
        original_path = main.DB_PATH
        main.DB_PATH = str(legacy_db)
        try:
            await main.init_db()
            with sqlite3.connect(legacy_db) as db:
                db.execute(
                    "INSERT INTO members (user_id,bonus,created_at) VALUES (?,?,?)",
                    (77, 80, main.now_iso()),
                )
                task_id = db.execute(
                    "INSERT INTO tasks "
                    "(type,title,city,address,reward,status,claimed_by,claimed_at,done_at,created_at) "
                    "VALUES ('fix_zone','Старое','Краснодар','Адрес',80,'done',77,?,?,?)",
                    (main.now_iso(), main.now_iso(), main.now_iso()),
                ).lastrowid
                db.execute(
                    "INSERT INTO bonus_ledger "
                    "(user_id,amount,reason,task_id,created_at,operation_id,balance_after) "
                    "VALUES (77,80,'Старое задание',?,?,?,80)",
                    (task_id, main.now_iso(), f"task_reward:task:{task_id}"),
                )
                db.commit()
            await main.init_db()
            with sqlite3.connect(legacy_db) as db:
                assignment = db.execute(
                    "SELECT id,status FROM task_assignments WHERE task_id=?", (task_id,),
                ).fetchone()
                ledger_assignment = db.execute(
                    "SELECT assignment_id FROM bonus_ledger WHERE task_id=?", (task_id,),
                ).fetchone()[0]
                balance = db.execute(
                    "SELECT bonus FROM members WHERE user_id=77"
                ).fetchone()[0]
        finally:
            main.DB_PATH = original_path
        self.assertEqual(assignment[1], "done")
        self.assertEqual(ledger_assignment, assignment[0])
        self.assertEqual(balance, 80)

    async def test_startup_schema_upgrade_rolls_back_as_one_transaction(self):
        atomic_root = TEST_ROOT / (self.id().rsplit(".", 1)[-1] + "_atomic")
        atomic_root.mkdir(parents=True, exist_ok=True)
        atomic_db = atomic_root / "atomic_upgrade.db"
        original_path = main.DB_PATH
        main.DB_PATH = str(atomic_db)
        try:
            await main.init_db()
            with sqlite3.connect(atomic_db) as db:
                task_id = db.execute(
                    "INSERT INTO tasks "
                    "(type,title,reward,status,repeatable,created_at) "
                    "VALUES ('fix_zone','Неоднозначное старое задание',80,'open',1,?)",
                    (main.now_iso(),),
                ).lastrowid
                for user_id in (701, 702):
                    db.execute(
                        "INSERT INTO task_assignments "
                        "(task_id,user_id,status,claimed_at) VALUES (?,?,'done',?)",
                        (task_id, user_id, main.now_iso()),
                    )
                db.execute(
                    "INSERT INTO bonus_ledger "
                    "(user_id,amount,reason,task_id,created_at,operation_id,balance_after) "
                    "VALUES (701,80,'Legacy',?,?,?,80)",
                    (task_id, main.now_iso(), f"task_reward:task:{task_id}"),
                )
                withdrawal_id = db.execute(
                    "INSERT INTO withdrawal_requests "
                    "(user_id,amount,status,created_at) VALUES (701,1000,'approved',?)",
                    (main.now_iso(),),
                ).lastrowid
                db.commit()

            with self.assertRaisesRegex(RuntimeError, "Ambiguous legacy task reward"):
                await main.init_db()

            with sqlite3.connect(atomic_db) as db:
                task_state = db.execute(
                    "SELECT max_participants,budget_cap FROM tasks WHERE id=?", (task_id,),
                ).fetchone()
                withdrawal_status = db.execute(
                    "SELECT status FROM withdrawal_requests WHERE id=?", (withdrawal_id,),
                ).fetchone()[0]
                ledger_assignment = db.execute(
                    "SELECT assignment_id FROM bonus_ledger WHERE task_id=?", (task_id,),
                ).fetchone()[0]
        finally:
            main.DB_PATH = original_path
        self.assertEqual(task_state, (None, None))
        self.assertEqual(withdrawal_status, "approved")
        self.assertIsNone(ledger_assignment)

    async def test_critical_worker_failure_terminates_service(self):
        async def failed_worker():
            await asyncio.sleep(0)
            raise RuntimeError("synthetic worker failure")

        async def long_running_service():
            await asyncio.Event().wait()

        previous = dict(main._background_tasks)
        main._background_tasks.clear()
        main._shutdown_event.clear()
        main._background_tasks["outbox"] = asyncio.create_task(failed_worker())
        try:
            with self.assertRaisesRegex(RuntimeError, "Critical worker failed: outbox"):
                await main._serve_with_critical_workers(long_running_service())
        finally:
            await asyncio.gather(*main._background_tasks.values(), return_exceptions=True)
            main._background_tasks.clear()
            main._background_tasks.update(previous)

    async def test_newer_sqlite_schema_refuses_application_downgrade(self):
        downgrade_root = TEST_ROOT / (self.id().rsplit(".", 1)[-1] + "_downgrade")
        downgrade_root.mkdir(parents=True, exist_ok=True)
        downgrade_db = downgrade_root / "newer.db"
        original_path = main.DB_PATH
        main.DB_PATH = str(downgrade_db)
        try:
            await main.init_db()
            with sqlite3.connect(downgrade_db) as db:
                db.execute(f"PRAGMA user_version={main.SQLITE_SCHEMA_VERSION + 1}")
            with self.assertRaisesRegex(RuntimeError, "refusing downgrade"):
                await main.init_db()
            with sqlite3.connect(downgrade_db) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
        finally:
            main.DB_PATH = original_path
        self.assertEqual(version, main.SQLITE_SCHEMA_VERSION + 1)

    async def test_city_normalization_allows_legacy_task_claim(self):
        worker_id = 910_000_001
        await main.upsert_member(
            worker_id, full_name="Участник", city="Краснодар",
            status="approved", role="helper",
        )
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks "
                "(type,title,city,address,reward,status,created_at,budget_cap) "
                "VALUES ('fix_zone','Парковка','г. Краснодар','Адрес',80,'open',?,80)",
                (main.now_iso(),),
            ).lastrowid
            db.commit()
        original_worker = main._require_worker

        async def allow_worker(_request):
            return worker_id, None

        main._require_worker = allow_worker
        try:
            response = await main.api_task_claim(DummyRequest({"task_id": task_id}))
        finally:
            main._require_worker = original_worker
        self.assertEqual(response.status, 200)
        self.assertEqual(main._city_key("город Орёл"), main._city_key("ОРЕЛ"))

    async def test_admin_tag_catalog_scans_beyond_first_page(self):
        for index in range(51):
            await main.upsert_member(
                911_000_000 + index, full_name=f"Участник {index}",
                status="approved", role="helper",
            )
        with sqlite3.connect(main.DB_PATH) as db:
            db.execute(
                "UPDATE members SET tags='далёкий-тег' WHERE user_id=?",
                (911_000_050,),
            )
            db.commit()
        original_admin = main._require_admin

        async def allow_admin(_request):
            return 911_999_999, None

        main._require_admin = allow_admin
        try:
            response = await main.api_admin_member_tags_catalog(DummyRequest({}))
        finally:
            main._require_admin = original_admin
        self.assertEqual(response.status, 200)
        items = response_json(response)["items"]
        self.assertIn({"tag": "далёкий-тег", "count": 1}, items)

    async def test_city_change_is_pending_until_admin_approves(self):
        worker_id, admin_id = 911_500_001, 911_500_002
        await self._seed_admin(admin_id)
        await main.upsert_member(
            worker_id, full_name="Участник", city="Краснодар",
            status="approved", role="helper",
        )
        original_auth, original_is_admin = main._auth_user, main.is_admin
        original_require_admin = main._require_admin

        async def auth_worker(_request):
            return {"id": worker_id}

        async def not_admin(_uid):
            return False

        async def allow_admin(_request):
            return admin_id, None

        main._auth_user = auth_worker
        main.is_admin = not_admin
        main._require_admin = allow_admin
        try:
            requested = await main.api_profile_city(DummyRequest({"city": "Орёл"}))
            requested_data = response_json(requested)
            with sqlite3.connect(main.DB_PATH) as db:
                before = db.execute(
                    "SELECT city,city_change_requested FROM members WHERE user_id=?",
                    (worker_id,),
                ).fetchone()
            stale = await main.api_admin_member_city_decide(DummyRequest({
                "user_id": worker_id, "decision": "approve",
                "requested_at": "2026-01-01T00:00:00+00:00",
            }))
            cancelled = await main.api_profile_city(DummyRequest({
                "action": "cancel", "requested_at": requested_data["requested_at"],
            }))
            requested_again = await main.api_profile_city(DummyRequest({"city": "Орёл"}))
            approved = await main.api_admin_member_city_decide(DummyRequest({
                "user_id": worker_id, "decision": "approve",
                "requested_at": response_json(requested_again)["requested_at"],
            }))
        finally:
            main._auth_user = original_auth
            main.is_admin = original_is_admin
            main._require_admin = original_require_admin
        self.assertEqual(
            (requested.status, stale.status, cancelled.status, requested_again.status, approved.status),
            (200, 409, 200, 200, 200),
        )
        self.assertTrue(response_json(requested)["pending"])
        self.assertEqual(before, ("Краснодар", "Орёл"))
        with sqlite3.connect(main.DB_PATH) as db:
            after = db.execute(
                "SELECT city,city_change_requested FROM members WHERE user_id=?",
                (worker_id,),
            ).fetchone()
        self.assertEqual(after, ("Орёл", None))

    async def test_task_dispute_cannot_open_after_evidence_window_started(self):
        worker_id, opener_id = 911_900_001, 911_900_002
        await main.upsert_member(
            worker_id, full_name="Исполнитель", status="approved", role="helper",
        )
        await self._seed_admin(opener_id)
        terminal_at = (
            main.datetime.now(main.timezone.utc)
            - main.timedelta(days=main.DISPUTE_OPEN_DAYS + 1)
        ).isoformat()
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks (type,title,reward,status,created_at) "
                "VALUES ('fix_zone','Старое решение',50,'closed',?)",
                (terminal_at,),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,done_at,reward_snapshot,"
                "terminal_at,terminal_by) VALUES (?,?,'done',?,?,50,?,?)",
                (
                    task_id, worker_id, terminal_at, terminal_at,
                    terminal_at, opener_id,
                ),
            ).lastrowid
            db.commit()
        original_admin = main._require_admin

        async def allow_admin(request):
            return request.uid, None

        main._require_admin = allow_admin
        try:
            response = await main.api_admin_task_dispute(DummyRequest({
                "action": "open", "assignment_id": assignment_id,
                "reason": "Слишком поздний спор",
                "operation_id": str(uuid.uuid4()),
            }, opener_id))
        finally:
            main._require_admin = original_admin
        self.assertEqual(response.status, 409)
        self.assertEqual(response_json(response)["error"], "dispute_window_closed")
        with sqlite3.connect(main.DB_PATH) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM task_disputes WHERE assignment_id=?",
                    (assignment_id,),
                ).fetchone()[0],
                0,
            )

    async def test_task_dispute_requires_second_admin_and_reverses_exactly_once(self):
        worker_id, opener_id, reviewer_id = (
            912_000_001, 912_000_002, 912_000_003,
        )
        await main.upsert_member(
            worker_id, full_name="Исполнитель", city="Краснодар",
            status="approved", role="helper", bonus=100, done_count=1,
        )
        for admin_id in (opener_id, reviewer_id):
            await main.upsert_member(
                admin_id, full_name="Ответственный", status="approved", role="admin",
            )
        open_flags = main._admin_decision_public({
            "id": 1, "type": "fix_zone", "title": "Проверка",
            "assignment_status": "done", "assignment_user_id": worker_id,
        }, opener_id, {opener_id, reviewer_id})
        decision_flags = main._admin_decision_public({
            "id": 1, "type": "fix_zone", "title": "Проверка",
            "assignment_status": "done", "assignment_user_id": worker_id,
            "dispute_status": "pending", "dispute_opened_by": opener_id,
            "assignment_terminal_by": reviewer_id,
        }, reviewer_id, {opener_id, reviewer_id})
        blocked_flags = main._admin_decision_public({
            "id": 1, "type": "fix_zone", "title": "Проверка",
            "assignment_status": "done", "assignment_user_id": worker_id,
        }, opener_id, {opener_id})
        self.assertTrue(open_flags["can_open_dispute"])
        self.assertEqual(open_flags["eligible_decider_count"], 1)
        self.assertTrue(decision_flags["can_decide_dispute"])
        self.assertFalse(blocked_flags["can_open_dispute"])
        self.assertIn("ещё один", blocked_flags["dispute_open_block_reason"])
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks "
                "(type,title,city,address,reward,status,repeatable,created_by,created_at) "
                "VALUES ('fix_zone','Ошибочное решение','Краснодар','Адрес',100,'closed',0,999,?)",
                (main.now_iso(),),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,done_at,reward_snapshot,"
                "terminal_by,terminal_at) VALUES (?,?,'done',?,?,100,?,?)",
                (
                    task_id, worker_id, main.now_iso(), main.now_iso(),
                    reviewer_id, main.now_iso(),
                ),
            ).lastrowid
            db.execute(
                "INSERT INTO bonus_ledger "
                "(user_id,amount,reason,task_id,assignment_id,created_by,created_at,"
                "operation_id,balance_after) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    worker_id, 100, "Задание", task_id, assignment_id, 999,
                    main.now_iso(), f"task_reward:assignment:{assignment_id}", 100,
                ),
            )
            db.commit()
        original_admin, original_worker = main._require_admin, main._require_worker

        async def allow_request_admin(request):
            return request.uid, None

        async def allow_worker(_request):
            return worker_id, None

        main._require_admin = allow_request_admin
        open_operation = str(uuid.uuid4())
        decide_operation = str(uuid.uuid4())
        try:
            opened = await main.api_admin_task_dispute(DummyRequest({
                "action": "open", "assignment_id": assignment_id,
                "reason": "Начисление подтверждено ошибочно",
                "operation_id": open_operation,
            }, opener_id))
            dispute_id = response_json(opened)["dispute_id"]
            main._require_worker = allow_worker
            blocked_withdrawal = await main.api_withdraw_request(DummyRequest({
                "amount": main.WITHDRAW_MIN, "account_ref": "bike-account-1",
                "operation_id": str(uuid.uuid4()),
            }))
            denied = await main.api_admin_task_dispute(DummyRequest({
                "action": "decide", "dispute_id": dispute_id,
                "decision": "approve", "note": "Проверено тем же ответственным",
                "operation_id": str(uuid.uuid4()),
            }, opener_id))
            decision_body = {
                "action": "decide", "dispute_id": dispute_id,
                "decision": "approve", "note": "Проверены фото и исходное решение",
                "operation_id": decide_operation,
            }
            decided = await main.api_admin_task_dispute(
                DummyRequest(decision_body, reviewer_id)
            )
            replay = await main.api_admin_task_dispute(
                DummyRequest(decision_body, reviewer_id)
            )
        finally:
            main._require_admin = original_admin
            main._require_worker = original_worker
        self.assertEqual(
            (
                opened.status, blocked_withdrawal.status, denied.status,
                decided.status, replay.status,
            ),
            (200, 409, 403, 200, 200),
        )
        self.assertTrue(response_json(replay)["idempotent"])
        with sqlite3.connect(main.DB_PATH) as db:
            assignment_status = db.execute(
                "SELECT status FROM task_assignments WHERE id=?", (assignment_id,),
            ).fetchone()[0]
            member_state = db.execute(
                "SELECT bonus,done_count FROM members WHERE user_id=?", (worker_id,),
            ).fetchone()
            task_status = db.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,),
            ).fetchone()[0]
            reversals = db.execute(
                "SELECT amount,balance_after FROM bonus_ledger "
                "WHERE operation_id=?",
                (f"task_reward_reversal:assignment:{assignment_id}",),
            ).fetchall()
        self.assertEqual(assignment_status, "reversed")
        self.assertEqual(member_state, (0, 0))
        self.assertEqual(task_status, "open")
        self.assertEqual(reversals, [(-100, 0)])

    async def test_dispute_with_spent_reward_requires_audited_manual_reconciliation(self):
        worker_id, opener_id, reviewer_id, decider_id = (
            912_100_001, 912_100_002, 912_100_003, 912_100_004,
        )
        await main.upsert_member(
            worker_id, full_name="Исполнитель", city="Краснодар",
            status="approved", role="helper", bonus=25, done_count=1,
        )
        for admin_id in (opener_id, reviewer_id, decider_id):
            await main.upsert_member(
                admin_id, full_name="Ответственный", status="approved", role="admin",
            )
        with sqlite3.connect(main.DB_PATH) as db:
            task_id = db.execute(
                "INSERT INTO tasks "
                "(type,title,city,address,reward,status,repeatable,created_by,created_at) "
                "VALUES ('fix_zone','Ручная сверка','Краснодар','Адрес',100,'closed',0,999,?)",
                (main.now_iso(),),
            ).lastrowid
            assignment_id = db.execute(
                "INSERT INTO task_assignments "
                "(task_id,user_id,status,claimed_at,done_at,reward_snapshot,"
                "terminal_by,terminal_at) VALUES (?,?,'done',?,?,100,?,?)",
                (
                    task_id, worker_id, main.now_iso(), main.now_iso(),
                    reviewer_id, main.now_iso(),
                ),
            ).lastrowid
            db.execute(
                "INSERT INTO bonus_ledger "
                "(user_id,amount,reason,task_id,assignment_id,created_by,created_at,"
                "operation_id,balance_after) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    worker_id, 100, "Задание", task_id, assignment_id, 999,
                    main.now_iso(), f"task_reward:assignment:{assignment_id}", 100,
                ),
            )
            db.commit()
        original_admin = main._require_admin

        async def allow_request_admin(request):
            return request.uid, None

        main._require_admin = allow_request_admin
        try:
            opened = await main.api_admin_task_dispute(DummyRequest({
                "action": "open", "assignment_id": assignment_id,
                "reason": "Нужна сверка потраченной выплаты",
                "operation_id": str(uuid.uuid4()),
            }, opener_id))
            dispute_id = response_json(opened)["dispute_id"]
            automatic = await main.api_admin_task_dispute(DummyRequest({
                "action": "decide", "dispute_id": dispute_id,
                "decision": "approve", "note": "Попытка автоматического сторно",
                "operation_id": str(uuid.uuid4()),
            }, decider_id))
            resolved = await main.api_admin_task_dispute(DummyRequest({
                "action": "decide", "dispute_id": dispute_id,
                "decision": "manual_reversed",
                "note": "Сверено с Бибибайком, обращение BB-142",
                "reconciliation_reference": "BB-142",
                "operation_id": str(uuid.uuid4()),
            }, decider_id))
        finally:
            main._require_admin = original_admin
        self.assertEqual((opened.status, automatic.status, resolved.status), (200, 409, 200))
        self.assertEqual(response_json(opened)["status"], "manual_required")
        with sqlite3.connect(main.DB_PATH) as db:
            dispute = db.execute(
                "SELECT status,reconciliation_reason,decision_note,reconciliation_reference "
                "FROM task_disputes WHERE id=?",
                (dispute_id,),
            ).fetchone()
            state = db.execute(
                "SELECT status FROM task_assignments WHERE id=?", (assignment_id,),
            ).fetchone()[0]
            balance = db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (worker_id,),
            ).fetchone()[0]
        self.assertEqual(dispute[0], "manual_reversed")
        self.assertIn("баланс", dispute[1].lower())
        self.assertIn("BB-142", dispute[2])
        self.assertEqual(dispute[3], "BB-142")
        self.assertEqual((state, balance), ("reversed", 0))

    async def test_award_revoke_is_all_or_nothing_when_bonus_was_spent(self):
        worker_id, admin_id = 912_200_001, 912_200_002
        await self._seed_admin(admin_id)
        await main.upsert_member(
            worker_id, full_name="Исполнитель", status="approved",
            role="helper", bonus=5,
        )
        with sqlite3.connect(main.DB_PATH) as db:
            award_id = db.execute(
                "INSERT INTO awards "
                "(code,emoji,title,description,bonus,repeatable,active,created_by,created_at) "
                "VALUES ('audit_award','🏅','Проверка','Тест',10,0,1,?,?)",
                (admin_id, main.now_iso()),
            ).lastrowid
            entry_id = db.execute(
                "INSERT INTO member_awards "
                "(user_id,award_id,slot,bonus,note,granted_by,granted_at) "
                "VALUES (?,?,0,10,'Тест',?,?)",
                (worker_id, award_id, admin_id, main.now_iso()),
            ).lastrowid
            db.execute(
                "INSERT INTO task_disputes "
                "(assignment_id,task_id,user_id,reward,reason,status,opened_by,opened_at,"
                "open_operation_id,open_request_hash) "
                "VALUES (999991,999991,?,-100,'Повреждённый snapshot','manual_required',?,?,?,?)",
                (worker_id, admin_id, main.now_iso(), str(uuid.uuid4()), "legacy-negative"),
            )
            db.commit()
        original_admin, original_notify = main._require_admin, main._notify

        async def allow_admin(_request):
            return admin_id, None

        main._require_admin = allow_admin
        main._notify = lambda *_args, **_kwargs: None
        operation_id = str(uuid.uuid4())
        body = {
            "entry_id": entry_id, "note": "Выдано ошибочно",
            "operation_id": operation_id,
        }
        try:
            response = await main.api_admin_award_revoke(DummyRequest(body))
            with sqlite3.connect(main.DB_PATH) as db:
                db.execute("UPDATE members SET bonus=10 WHERE user_id=?", (worker_id,))
                db.commit()
            success = await main.api_admin_award_revoke(DummyRequest(body))
            replay = await main.api_admin_award_revoke(DummyRequest(body))
        finally:
            main._require_admin = original_admin
            main._notify = original_notify
        self.assertEqual((response.status, success.status, replay.status), (409, 200, 200))
        self.assertTrue(response_json(replay)["idempotent"])
        with sqlite3.connect(main.DB_PATH) as db:
            revoked_at = db.execute(
                "SELECT revoked_at FROM member_awards WHERE id=?", (entry_id,),
            ).fetchone()[0]
            balance = db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (worker_id,),
            ).fetchone()[0]
        self.assertIsNotNone(revoked_at)
        self.assertEqual(balance, 0)

    async def test_rate_limit_uses_independent_valid_telegram_user_buckets(self):
        class RateRequest(dict):
            path = "/api/state"
            method = "GET"
            remote = "127.0.0.1"

        async def handler(_request):
            return main._json({"ok": True})

        original_limit = main.API_READS_PER_MIN
        original_buckets = main._api_rate_buckets
        original_requests = main._api_rate_requests
        main.API_READS_PER_MIN = 1
        main._api_rate_buckets = {}
        main._api_rate_requests = 0
        try:
            first = RateRequest(bibitasks_auth_context={"user": {"id": 1}})
            second = RateRequest(bibitasks_auth_context={"user": {"id": 2}})
            self.assertEqual((await main.rate_limit_middleware(first, handler)).status, 200)
            self.assertEqual((await main.rate_limit_middleware(first, handler)).status, 429)
            self.assertEqual((await main.rate_limit_middleware(second, handler)).status, 200)
        finally:
            main.API_READS_PER_MIN = original_limit
            main._api_rate_buckets = original_buckets
            main._api_rate_requests = original_requests

    async def test_historical_webhook_error_does_not_latch_readiness(self):
        original_mode = main.TELEGRAM_UPDATE_MODE
        original_base = main.PUBLIC_BASE_URL
        original_path = main.WEBHOOK_PATH
        original_connections = main.WEBHOOK_MAX_CONNECTIONS
        original_runtime = dict(main._telegram_runtime)
        original_method = main.bot.get_webhook_info
        main.TELEGRAM_UPDATE_MODE = "webhook"
        main.PUBLIC_BASE_URL = "https://tasks.example"
        main.WEBHOOK_PATH = "/telegram/webhook/test"
        main.WEBHOOK_MAX_CONNECTIONS = 8
        main._telegram_runtime["last_update_at"] = main.now_iso()

        webhook_state = {"last_error_date": 1_700_000_000}

        async def webhook_info():
            return SimpleNamespace(
                url=main._webhook_url(), max_connections=8,
                allowed_updates=list(main.dp.resolve_used_update_types()),
                pending_update_count=0,
                last_error_date=webhook_state["last_error_date"],
            )

        main.bot.get_webhook_info = webhook_info
        try:
            await main._refresh_telegram_runtime()
            self.assertTrue(main._telegram_runtime["receiver_ready"])
            self.assertTrue(main._telegram_runtime["last_error"])
            webhook_state["last_error_date"] = int(time.time()) + 60
            await main._refresh_telegram_runtime()
            self.assertFalse(main._telegram_runtime["receiver_ready"])
            t0 = main.datetime.now(main.timezone.utc) - main.timedelta(minutes=2)
            t1 = main.datetime.now(main.timezone.utc) - main.timedelta(minutes=1)
            t2 = main.datetime.now(main.timezone.utc)
            with sqlite3.connect(main.DB_PATH) as db:
                db.execute(
                    "INSERT INTO telegram_update_inbox "
                    "(update_id,payload_json,payload_sha256,status,attempts,available_at,received_at) "
                    "VALUES (99001,'encrypted','hash','processed',1,?,?)",
                    (t0.isoformat(), t0.isoformat()),
                )
                db.commit()
            webhook_state["last_error_date"] = int(t1.timestamp())
            main._telegram_runtime["last_update_at"] = t2.isoformat()
            await main._refresh_telegram_runtime()
            self.assertTrue(main._telegram_runtime["receiver_ready"])
        finally:
            main.bot.get_webhook_info = original_method
            main.TELEGRAM_UPDATE_MODE = original_mode
            main.PUBLIC_BASE_URL = original_base
            main.WEBHOOK_PATH = original_path
            main.WEBHOOK_MAX_CONNECTIONS = original_connections
            main._telegram_runtime.clear()
            main._telegram_runtime.update(original_runtime)

    async def test_manual_grant_is_positive_limited_idempotent_and_durable(self):
        maker_a, maker_b = 990_100_001, 990_100_002
        recipient_a, recipient_b = 990_100_003, 990_100_004
        for user_id, role in (
            (maker_a, "admin"), (maker_b, "admin"),
            (recipient_a, "helper"), (recipient_b, "helper"),
        ):
            await main.upsert_member(
                user_id, full_name=f"Участник {user_id}", status="approved",
                role=role, bonus=0,
            )
        original_admin = main._require_admin

        async def allow_admin(request):
            return request.uid, None

        main._require_admin = allow_admin
        first_operation = str(uuid.uuid4())
        try:
            negative = await main.api_admin_grant(DummyRequest({
                "user_id": recipient_a, "amount": -1, "reason": "Попытка списания",
                "operation_id": str(uuid.uuid4()),
            }, maker_a))
            first_body = {
                "user_id": recipient_a, "amount": 200,
                "reason": "Исправил парковку и прислал фото",
                "operation_id": first_operation,
            }
            first = await main.api_admin_grant(DummyRequest(first_body, maker_a))
            replay = await main.api_admin_grant(DummyRequest(first_body, maker_a))
            maker_split = await main.api_admin_grant(DummyRequest({
                "user_id": recipient_b, "amount": 101,
                "reason": "Помог на второй парковке",
                "operation_id": str(uuid.uuid4()),
            }, maker_a))
            recipient_split = await main.api_admin_grant(DummyRequest({
                "user_id": recipient_a, "amount": 101,
                "reason": "Ещё одна быстрая благодарность",
                "operation_id": str(uuid.uuid4()),
            }, maker_b))
            failed_operation = str(uuid.uuid4())
            original_outbox = main._enqueue_outbox_in_tx

            async def broken_outbox(*_args, **_kwargs):
                raise RuntimeError("outbox unavailable")

            main._enqueue_outbox_in_tx = broken_outbox
            try:
                with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
                    await main.api_admin_grant(DummyRequest({
                        "user_id": recipient_b, "amount": 50,
                        "reason": "Проверка атомарной доставки",
                        "operation_id": failed_operation,
                    }, maker_b))
            finally:
                main._enqueue_outbox_in_tx = original_outbox
        finally:
            main._require_admin = original_admin
        self.assertEqual(
            (negative.status, first.status, replay.status, maker_split.status, recipient_split.status),
            (400, 200, 200, 409, 409),
        )
        self.assertTrue(response_json(replay)["idempotent"])
        with sqlite3.connect(main.DB_PATH) as db:
            balance = db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (recipient_a,),
            ).fetchone()[0]
            ledger_count = db.execute(
                "SELECT COUNT(*) FROM bonus_ledger WHERE operation_id=?",
                (first_operation,),
            ).fetchone()[0]
            outbox = db.execute(
                "SELECT status FROM task_outbox WHERE event_key=?",
                (f"manual_grant:{first_operation}:recipient",),
            ).fetchone()
            command_count = db.execute(
                "SELECT COUNT(*) FROM manual_grant_commands"
            ).fetchone()[0]
            rolled_back = db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (recipient_b,),
            ).fetchone()[0]
            failed_ledger = db.execute(
                "SELECT COUNT(*) FROM bonus_ledger WHERE operation_id=?",
                (failed_operation,),
            ).fetchone()[0]
        self.assertEqual(
            (balance, ledger_count, command_count, rolled_back, failed_ledger),
            (200, 1, 1, 0, 0),
        )
        self.assertEqual(outbox, ("pending",))

    async def test_manual_grant_reversal_is_two_person_full_idempotent_and_linked(self):
        maker, checker, recipient = 990_140_001, 990_140_002, 990_140_003
        for user_id, role in (
            (maker, "admin"), (checker, "admin"), (recipient, "helper"),
        ):
            await main.upsert_member(
                user_id, full_name=f"Участник {user_id}", status="approved",
                role=role, bonus=0,
            )
        original_admin = main._require_admin
        original_environment = main.BIBITASKS_ENVIRONMENT

        async def allow_admin(request):
            return request.uid, None

        main._require_admin = allow_admin
        main.BIBITASKS_ENVIRONMENT = "staging"
        grant_operation = str(uuid.uuid4())
        request_operation = str(uuid.uuid4())
        decision_operation = str(uuid.uuid4())
        try:
            grant = await main.api_admin_grant(DummyRequest({
                "user_id": recipient, "amount": 100,
                "reason": "Поправил парковку и прислал фото",
                "operation_id": grant_operation,
            }, maker))
            request_body = {
                "action": "request", "grant_operation_id": grant_operation,
                "reason": "Начисление отправлено не за тот отчёт",
                "operation_id": request_operation,
            }
            requested = await main.api_admin_grant_reversal(
                DummyRequest(request_body, maker)
            )
            request_replay = await main.api_admin_grant_reversal(
                DummyRequest(request_body, maker)
            )
            duplicate = await main.api_admin_grant_reversal(DummyRequest({
                **request_body, "operation_id": str(uuid.uuid4()),
            }, maker))
            reversal_id = response_json(requested)["reversal_id"]
            self_decision = await main.api_admin_grant_reversal(DummyRequest({
                "action": "decide", "reversal_id": reversal_id,
                "decision": "approve", "note": "Проводка проверена",
                "operation_id": str(uuid.uuid4()),
            }, maker))
            decision_body = {
                "action": "decide", "reversal_id": reversal_id,
                "decision": "approve", "note": "Проводка и получатель проверены",
                "operation_id": decision_operation,
            }
            decided = await main.api_admin_grant_reversal(
                DummyRequest(decision_body, checker)
            )
            decision_replay = await main.api_admin_grant_reversal(
                DummyRequest(decision_body, checker)
            )
            decision_conflict = await main.api_admin_grant_reversal(DummyRequest({
                **decision_body, "note": "Другой результат проверки",
            }, checker))
        finally:
            main._require_admin = original_admin
            main.BIBITASKS_ENVIRONMENT = original_environment
        self.assertEqual(
            (
                grant.status, requested.status, request_replay.status,
                duplicate.status, self_decision.status, decided.status,
                decision_replay.status, decision_conflict.status,
            ),
            (200, 200, 200, 409, 403, 200, 200, 409),
        )
        self.assertTrue(response_json(request_replay)["idempotent"])
        self.assertTrue(response_json(decision_replay)["idempotent"])
        with sqlite3.connect(main.DB_PATH) as db:
            original_ledger = db.execute(
                "SELECT id FROM bonus_ledger WHERE operation_id=?",
                (grant_operation,),
            ).fetchone()[0]
            reversal = db.execute(
                "SELECT status,requested_by,decided_by,reversal_ledger_id,result_balance "
                "FROM manual_grant_reversals WHERE id=?", (reversal_id,),
            ).fetchone()
            debit = db.execute(
                "SELECT amount,reversal_of_ledger_id,balance_after FROM bonus_ledger "
                "WHERE id=?", (reversal[3],),
            ).fetchone()
            balance = db.execute(
                "SELECT bonus FROM members WHERE user_id=?", (recipient,),
            ).fetchone()[0]
            command_types = {
                row[0] for row in db.execute(
                    "SELECT command_type FROM operation_registry WHERE operation_id IN (?,?,?)",
                    (grant_operation, request_operation, decision_operation),
                )
            }
            events = db.execute(
                "SELECT event_name,COUNT(*) FROM product_events "
                "WHERE event_name LIKE 'manual_grant_reversal_%' GROUP BY event_name"
            ).fetchall()
            correction_outbox = db.execute(
                "SELECT COUNT(*) FROM task_outbox "
                "WHERE event_key LIKE 'manual_grant_reversal:%'",
            ).fetchone()[0]
        self.assertEqual(reversal[:3], ("applied", maker, checker))
        self.assertIsNotNone(reversal[3])
        self.assertEqual(reversal[4], 0)
        self.assertEqual(debit, (-100, original_ledger, 0))
        self.assertEqual(balance, 0)
        self.assertEqual(command_types, {
            "manual_grant", "manual_grant_reversal_request",
            "manual_grant_reversal_decision",
        })
        self.assertEqual(dict(events), {
            "manual_grant_reversal_requested": 1,
            "manual_grant_reversal_resolved": 1,
        })
        self.assertGreaterEqual(correction_outbox, 4)

    async def test_manual_grant_reversal_manual_required_never_partially_debits(self):
        maker, checker, recipient = 990_145_001, 990_145_002, 990_145_003
        for user_id, role in (
            (maker, "admin"), (checker, "admin"), (recipient, "helper"),
        ):
            await main.upsert_member(
                user_id, full_name=str(user_id), status="approved", role=role, bonus=0,
            )
        original_admin = main._require_admin
        original_environment = main.BIBITASKS_ENVIRONMENT

        async def allow_admin(request):
            return request.uid, None

        main._require_admin = allow_admin
        main.BIBITASKS_ENVIRONMENT = "staging"
        grant_operation = str(uuid.uuid4())
        decision_operation = str(uuid.uuid4())
        try:
            await main.api_admin_grant(DummyRequest({
                "user_id": recipient, "amount": 100, "reason": "Помощь на парковке",
                "operation_id": grant_operation,
            }, maker))
            with sqlite3.connect(main.DB_PATH) as db:
                db.execute("UPDATE members SET bonus=20 WHERE user_id=?", (recipient,))
                db.commit()
            requested = await main.api_admin_grant_reversal(DummyRequest({
                "action": "request", "grant_operation_id": grant_operation,
                "reason": "Начисление оказалось ошибочным",
                "operation_id": str(uuid.uuid4()),
            }, maker))
            reversal_id = response_json(requested)["reversal_id"]
            blocked = await main.api_admin_grant_reversal(DummyRequest({
                "action": "decide", "reversal_id": reversal_id,
                "decision": "approve", "note": "Проверил исходную проводку",
                "operation_id": decision_operation,
            }, checker))
            with self.assertRaisesRegex(ValueError, "зарезервирована"):
                await main.add_bonus(
                    recipient, -1, "Попытка списания", operation_id=str(uuid.uuid4()),
                )
            with sqlite3.connect(main.DB_PATH) as db:
                before_topup = db.execute(
                    "SELECT bonus FROM members WHERE user_id=?", (recipient,),
                ).fetchone()[0]
                debit_count = db.execute(
                    "SELECT COUNT(*) FROM bonus_ledger WHERE reversal_of_ledger_id IS NOT NULL"
                ).fetchone()[0]
                decision_registry = db.execute(
                    "SELECT COUNT(*) FROM operation_registry WHERE operation_id=?",
                    (decision_operation,),
                ).fetchone()[0]
                db.execute("UPDATE members SET bonus=100 WHERE user_id=?", (recipient,))
                db.commit()
            decided = await main.api_admin_grant_reversal(DummyRequest({
                "action": "decide", "reversal_id": reversal_id,
                "decision": "approve", "note": "Проверил исходную проводку",
                "operation_id": decision_operation,
            }, checker))
        finally:
            main._require_admin = original_admin
            main.BIBITASKS_ENVIRONMENT = original_environment
        self.assertEqual(response_json(requested)["status"], "manual_required")
        self.assertEqual((blocked.status, before_topup, debit_count, decision_registry), (409, 20, 0, 0))
        self.assertEqual((decided.status, response_json(decided)["status"]), (200, "applied"))
        with sqlite3.connect(main.DB_PATH) as db:
            self.assertEqual(
                db.execute("SELECT bonus FROM members WHERE user_id=?", (recipient,)).fetchone()[0],
                0,
            )

    async def test_manual_grant_reversal_authority_and_outbox_fail_closed(self):
        maker, checker, recipient, admin_recipient = (
            990_147_001, 990_147_002, 990_147_003, 990_147_004,
        )
        for user_id, role in (
            (maker, "admin"), (checker, "admin"), (recipient, "helper"),
            (admin_recipient, "admin"),
        ):
            await main.upsert_member(
                user_id, full_name=str(user_id), status="approved", role=role, bonus=0,
            )
        original_admin = main._require_admin
        original_environment = main.BIBITASKS_ENVIRONMENT
        original_outbox = main._enqueue_outbox_in_tx

        async def allow_admin(request):
            return request.uid, None

        main._require_admin = allow_admin
        main.BIBITASKS_ENVIRONMENT = "production"
        grant_operation = str(uuid.uuid4())
        decision_operation = str(uuid.uuid4())
        try:
            with sqlite3.connect(main.DB_PATH) as db:
                stamp = main.now_iso()
                db.executemany(
                    "INSERT OR IGNORE INTO admin_authorities "
                    "(user_id,origin,granted_operation_id,granted_at) VALUES (?,?,?,?)",
                    [
                        (maker, "manual", str(uuid.uuid4()), stamp),
                        (checker, "manual", str(uuid.uuid4()), stamp),
                    ],
                )
                db.execute("UPDATE members SET role='helper' WHERE user_id=?", (checker,))
                db.commit()
            admin_grant = await main.api_admin_grant(DummyRequest({
                "user_id": admin_recipient, "amount": 10, "reason": "Нельзя админу",
                "operation_id": str(uuid.uuid4()),
            }, maker))
            with sqlite3.connect(main.DB_PATH) as db:
                db.execute("UPDATE members SET role='admin' WHERE user_id=?", (checker,))
                db.commit()
            await main.api_admin_grant(DummyRequest({
                "user_id": recipient, "amount": 80, "reason": "Помощь на парковке",
                "operation_id": grant_operation,
            }, maker))
            requested = await main.api_admin_grant_reversal(DummyRequest({
                "action": "request", "grant_operation_id": grant_operation,
                "reason": "Выбран неверный фотоотчёт",
                "operation_id": str(uuid.uuid4()),
            }, maker))
            reversal_id = response_json(requested)["reversal_id"]
            with sqlite3.connect(main.DB_PATH) as db:
                db.execute("DELETE FROM admin_authorities WHERE user_id=?", (maker,))
                db.commit()
            maker_revoked = await main.api_admin_grant_reversal(DummyRequest({
                "action": "decide", "reversal_id": reversal_id,
                "decision": "approve", "note": "Проверка завершена",
                "operation_id": decision_operation,
            }, checker))
            with sqlite3.connect(main.DB_PATH) as db:
                db.execute(
                    "INSERT INTO admin_authorities "
                    "(user_id,origin,granted_operation_id,granted_at) VALUES (?,?,?,?)",
                    (maker, "manual", str(uuid.uuid4()), main.now_iso()),
                )
                db.commit()

            async def broken_outbox(*_args, **_kwargs):
                raise RuntimeError("outbox unavailable")

            main._enqueue_outbox_in_tx = broken_outbox
            with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
                await main.api_admin_grant_reversal(DummyRequest({
                    "action": "decide", "reversal_id": reversal_id,
                    "decision": "approve", "note": "Проверка завершена",
                    "operation_id": decision_operation,
                }, checker))
            main._enqueue_outbox_in_tx = original_outbox
            with sqlite3.connect(main.DB_PATH) as db:
                after_failure = (
                    db.execute("SELECT bonus FROM members WHERE user_id=?", (recipient,)).fetchone()[0],
                    db.execute("SELECT status FROM manual_grant_reversals WHERE id=?", (reversal_id,)).fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM operation_registry WHERE operation_id=?", (decision_operation,)).fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM bonus_ledger WHERE reversal_of_ledger_id IS NOT NULL").fetchone()[0],
                )
            retry = await main.api_admin_grant_reversal(DummyRequest({
                "action": "decide", "reversal_id": reversal_id,
                "decision": "approve", "note": "Проверка завершена",
                "operation_id": decision_operation,
            }, checker))
        finally:
            main._require_admin = original_admin
            main.BIBITASKS_ENVIRONMENT = original_environment
            main._enqueue_outbox_in_tx = original_outbox
        self.assertEqual(admin_grant.status, 409)
        self.assertEqual(maker_revoked.status, 409)
        self.assertEqual(after_failure, (80, "pending", 0, 0))
        self.assertEqual((retry.status, response_json(retry)["status"]), (200, "applied"))

    async def test_awards_share_manual_grant_limit_and_global_operation_namespace(self):
        maker_a, maker_b, recipient_a, recipient_b = 990_150_001, 990_150_002, 990_150_003, 990_150_004
        maker_c, recipient_c, recipient_d, maker_d = 990_150_005, 990_150_006, 990_150_007, 990_150_008
        for user_id, role in ((maker_a, "admin"), (maker_b, "admin"), (maker_c, "admin"), (maker_d, "admin"), (recipient_a, "helper"), (recipient_b, "helper"), (recipient_c, "helper"), (recipient_d, "helper")):
            await main.upsert_member(user_id, full_name=str(user_id), status="approved", role=role, bonus=0)
        with sqlite3.connect(main.DB_PATH) as db:
            award_101 = db.execute(
                "INSERT INTO awards (code,emoji,title,description,bonus,repeatable,active,created_by,created_at) "
                "VALUES ('limit101','🏅','Лимит 101','Тест',101,0,1,?,?)",
                (maker_a, main.now_iso()),
            ).lastrowid
            award_200 = db.execute(
                "INSERT INTO awards (code,emoji,title,description,bonus,repeatable,active,created_by,created_at) "
                "VALUES ('limit200','🏅','Лимит 200','Тест',200,0,1,?,?)",
                (maker_b, main.now_iso()),
            ).lastrowid
            db.commit()
        original_admin = main._require_admin
        async def allow_admin(request):
            return request.uid, None
        main._require_admin = allow_admin
        try:
            invalid_repeatable = await main.api_admin_award_save(DummyRequest({
                "title": "Денежная", "bonus": 10, "repeatable": True,
            }, maker_a))
            zero_repeatable = await main.api_admin_award_save(DummyRequest({
                "title": "Знак", "bonus": 0, "repeatable": True,
            }, maker_a))
            manual = await main.api_admin_grant(DummyRequest({
                "user_id": recipient_a, "amount": 200, "reason": "Первая выплата",
                "operation_id": str(uuid.uuid4()),
            }, maker_a))
            award_blocked = await main.api_admin_award_grant(DummyRequest({
                "user_id": recipient_b, "award_id": award_101, "note": "Вторая выплата",
                "operation_id": str(uuid.uuid4()),
            }, maker_a))
            award = await main.api_admin_award_grant(DummyRequest({
                "user_id": recipient_b, "award_id": award_200, "note": "Первая награда",
                "operation_id": str(uuid.uuid4()),
            }, maker_b))
            recipient_blocked = await main.api_admin_grant(DummyRequest({
                "user_id": recipient_b, "amount": 101, "reason": "Вторая выплата",
                "operation_id": str(uuid.uuid4()),
            }, maker_a))
            collision_id = str(uuid.uuid4())
            first_collision = await main.api_admin_grant(DummyRequest({
                "user_id": recipient_a, "amount": 1, "reason": "Коллизия UUID",
                "operation_id": collision_id,
            }, maker_b))
            second_collision = await main.api_admin_award_grant(DummyRequest({
                "user_id": recipient_a, "award_id": award_101, "note": "Коллизия UUID",
                "operation_id": collision_id,
            }, maker_b))
            concurrent = await asyncio.gather(
                main.api_admin_grant(DummyRequest({
                    "user_id": recipient_c, "amount": 101, "reason": "Параллельная выплата",
                    "operation_id": str(uuid.uuid4()),
                }, maker_c)),
                main.api_admin_award_grant(DummyRequest({
                    "user_id": recipient_c, "award_id": award_200, "note": "Параллельная награда",
                    "operation_id": str(uuid.uuid4()),
                }, maker_c)),
            )
            failed_operation = str(uuid.uuid4())
            original_outbox = main._enqueue_outbox_in_tx
            async def broken_outbox(*_args, **_kwargs):
                raise RuntimeError("outbox unavailable")
            main._enqueue_outbox_in_tx = broken_outbox
            try:
                with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
                    await main.api_admin_award_grant(DummyRequest({
                        "user_id": recipient_d, "award_id": award_101,
                        "note": "Проверка атомарности", "operation_id": failed_operation,
                    }, maker_d))
            finally:
                main._enqueue_outbox_in_tx = original_outbox
        finally:
            main._require_admin = original_admin
        self.assertEqual(
            (invalid_repeatable.status, zero_repeatable.status, manual.status,
             award_blocked.status, award.status, recipient_blocked.status,
             first_collision.status, second_collision.status),
            (400, 200, 200, 409, 200, 409, 200, 409),
        )
        self.assertEqual(sorted(response.status for response in concurrent), [200, 409])
        with sqlite3.connect(main.DB_PATH) as db:
            self.assertLessEqual(
                db.execute("SELECT bonus FROM members WHERE user_id=?", (recipient_c,)).fetchone()[0],
                main.MANUAL_GRANT_DAILY_LIMIT,
            )
            self.assertEqual(
                db.execute("SELECT bonus FROM members WHERE user_id=?", (recipient_d,)).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM operation_registry WHERE operation_id=?", (failed_operation,)).fetchone()[0],
                0,
            )

    async def test_admin_authority_rotation_keeps_manual_and_revokes_removed_env(self):
        original_ids, original_environment = main.ADMIN_IDS, main.BIBITASKS_ENVIRONMENT
        try:
            main.BIBITASKS_ENVIRONMENT = "production"
            main.ADMIN_IDS = {991_000_001, 991_000_002}
            await main.init_db()
            await main.upsert_member(991_000_003, status="approved", role="admin")
            with sqlite3.connect(main.DB_PATH) as db:
                db.execute(
                    "INSERT INTO admin_authorities (user_id,origin,granted_operation_id,granted_at) "
                    "VALUES (?,'manual',?,?)",
                    (991_000_003, str(uuid.uuid4()), main.now_iso()),
                )
                db.commit()
            main.ADMIN_IDS = {991_000_002, 991_000_004}
            await main.init_db()
            with sqlite3.connect(main.DB_PATH) as db:
                roles = dict(db.execute(
                    "SELECT user_id,role FROM members WHERE user_id IN (991000001,991000002,991000003,991000004)"
                ).fetchall())
                sources = set(db.execute("SELECT user_id,origin FROM admin_authorities").fetchall())
            self.assertEqual(roles[991_000_001], "helper")
            self.assertEqual(roles[991_000_002], "admin")
            self.assertEqual(roles[991_000_003], "admin")
            self.assertEqual(roles[991_000_004], "admin")
            self.assertIn((991_000_003, "manual"), sources)
            self.assertNotIn((991_000_001, "env"), sources)
        finally:
            main.ADMIN_IDS, main.BIBITASKS_ENVIRONMENT = original_ids, original_environment

    async def test_revoked_admin_is_rechecked_inside_financial_transaction(self):
        admin_id, recipient_id = 991_100_001, 991_100_002
        original_environment, original_admin = main.BIBITASKS_ENVIRONMENT, main._require_admin
        try:
            main.BIBITASKS_ENVIRONMENT = "production"
            await main.upsert_member(admin_id, status="approved", role="admin")
            await main.upsert_member(recipient_id, status="approved", role="helper", bonus=0)
            async def stale_outer_authorization(_request):
                return admin_id, None
            main._require_admin = stale_outer_authorization
            operation_id = str(uuid.uuid4())
            response = await main.api_admin_grant(DummyRequest({
                "user_id": recipient_id, "amount": 50, "reason": "Устаревшее право",
                "operation_id": operation_id,
            }, admin_id))
            with sqlite3.connect(main.DB_PATH) as db:
                effects = db.execute(
                    "SELECT COUNT(*) FROM operation_registry WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()[0]
                balance = db.execute(
                    "SELECT bonus FROM members WHERE user_id=?", (recipient_id,),
                ).fetchone()[0]
            self.assertEqual((response.status, effects, balance), (403, 0, 0))
        finally:
            main.BIBITASKS_ENVIRONMENT, main._require_admin = original_environment, original_admin

    async def test_revoked_admin_cannot_mutate_tasks_applications_city_tags_or_retry(self):
        admin_id, applicant_id, city_user_id, tagged_user_id = (
            991_200_001, 991_200_002, 991_200_003, 991_200_004,
        )
        original_environment, original_admin = main.BIBITASKS_ENVIRONMENT, main._require_admin
        try:
            main.BIBITASKS_ENVIRONMENT = "production"
            await main.upsert_member(admin_id, status="approved", role="admin")
            await main.upsert_member(applicant_id, status="pending", role="applicant")
            await main.upsert_member(
                city_user_id, status="approved", role="helper", city="Краснодар",
                city_change_requested="Сочи", city_change_requested_at="2026-07-28T10:00:00+00:00",
            )
            await main.upsert_member(
                tagged_user_id, status="approved", role="helper", tags="старый",
            )
            with sqlite3.connect(main.DB_PATH) as db:
                cancel_task_id = db.execute(
                    "INSERT INTO tasks (type,title,city,address,reward,status,repeatable,created_at) "
                    "VALUES ('fix_zone','Отмена','Краснодар','Адрес',50,'open',0,?)",
                    (main.now_iso(),),
                ).lastrowid
                retry_task_id = db.execute(
                    "INSERT INTO tasks (type,title,city,address,reward,status,repeatable,created_at) "
                    "VALUES ('fix_zone','Повтор','Краснодар','Адрес',50,'open',0,?)",
                    (main.now_iso(),),
                ).lastrowid
                db.execute(
                    "INSERT INTO task_outbox "
                    "(event_key,event_type,payload_json,status,attempts,available_at,created_at) "
                    "VALUES (?,'group_task','{}','dead',10,?,?)",
                    (f"task:{retry_task_id}:announcement", main.now_iso(), main.now_iso()),
                )
                db.execute(
                    "INSERT INTO telegram_update_inbox "
                    "(update_id,payload_json,payload_sha256,status,attempts,available_at,received_at,dead_at) "
                    "VALUES (991200005,'encrypted','hash','dead',10,?,?,?)",
                    (main.now_iso(), main.now_iso(), main.now_iso()),
                )
                before_tasks = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                db.commit()

            async def stale_outer_authorization(_request):
                return admin_id, None
            main._require_admin = stale_outer_authorization
            image_bytes = BytesIO()
            Image.new("RGB", (2, 2), "green").save(image_bytes, format="PNG")
            photo_data = "data:image/png;base64," + base64.b64encode(
                image_bytes.getvalue()
            ).decode()
            create = await main.api_admin_task_create(DummyRequest({
                "operation_id": str(uuid.uuid4()), "type": "fix_zone",
                "title": "Не должно создаться", "details": "Проверка",
                "city": "Краснодар", "address": "Адрес", "reward": 80,
                "evidence_policy": "after_required", "repeatable": False,
                "announce": False, "photo_data": photo_data,
            }, admin_id))
            cancel = await main.api_admin_task_cancel(DummyRequest({
                "task_id": cancel_task_id, "reason": "Устаревшее право",
                "operation_id": str(uuid.uuid4()),
            }, admin_id))
            application = await main.api_admin_decide(DummyRequest({
                "user_id": applicant_id, "decision": "approve",
            }, admin_id))
            city = await main.api_admin_member_city_decide(DummyRequest({
                "user_id": city_user_id, "decision": "approve",
                "requested_at": "2026-07-28T10:00:00+00:00",
            }, admin_id))
            tags = await main.api_admin_member_tags(DummyRequest({
                "user_id": tagged_user_id, "tags": ["новый"],
            }, admin_id))
            retry = await main.api_admin_task_announcement_retry(DummyRequest({
                "task_id": retry_task_id, "operation_id": str(uuid.uuid4()),
            }, admin_id))
            redrive = await main.api_admin_telegram_inbox_redrive(DummyRequest({
                "update_id": 991200005, "reason": "Устаревшее право",
                "operation_id": str(uuid.uuid4()),
            }, admin_id))
            with sqlite3.connect(main.DB_PATH) as db:
                state = (
                    db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                    db.execute("SELECT status FROM tasks WHERE id=?", (cancel_task_id,)).fetchone()[0],
                    db.execute("SELECT status FROM members WHERE user_id=?", (applicant_id,)).fetchone()[0],
                    db.execute("SELECT city,city_change_requested FROM members WHERE user_id=?", (city_user_id,)).fetchone(),
                    db.execute("SELECT tags FROM members WHERE user_id=?", (tagged_user_id,)).fetchone()[0],
                    db.execute("SELECT status FROM task_outbox WHERE event_key=?", (f"task:{retry_task_id}:announcement",)).fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM media_objects").fetchone()[0],
                    db.execute("SELECT status FROM telegram_update_inbox WHERE update_id=991200005").fetchone()[0],
                )
            self.assertEqual([r.status for r in (create, cancel, application, city, tags, retry, redrive)], [403] * 7)
            self.assertEqual(state, (before_tasks, "open", "pending", ("Краснодар", "Сочи"), "старый", "dead", 0, "dead"))
        finally:
            main.BIBITASKS_ENVIRONMENT, main._require_admin = original_environment, original_admin

    async def test_admin_role_change_requires_distinct_checker_and_is_atomic(self):
        maker, checker, target = 990_200_001, 990_200_002, 990_200_003
        for user_id, role in ((maker, "admin"), (checker, "admin"), (target, "helper")):
            await main.upsert_member(
                user_id, full_name=f"Участник {user_id}", status="approved", role=role,
            )
        original_admin = main._require_admin

        async def allow_admin(request):
            return request.uid, None

        main._require_admin = allow_admin
        request_operation = str(uuid.uuid4())
        try:
            requested = await main.api_admin_set_role(DummyRequest({
                "action": "request", "user_id": target, "role": "admin",
                "reason": "Будет проверять задания и выплаты",
                "operation_id": request_operation,
            }, maker))
            change_id = response_json(requested)["change_id"]
            maker_denied = await main.api_admin_set_role(DummyRequest({
                "action": "decide", "change_id": change_id, "decision": "approve",
                "note": "Проверил свой запрос", "operation_id": str(uuid.uuid4()),
            }, maker))
            decision_operation = str(uuid.uuid4())
            decision_body = {
                "action": "decide", "change_id": change_id, "decision": "approve",
                "note": "Личность и доступ проверены", "operation_id": decision_operation,
            }
            original_outbox = main._enqueue_outbox_in_tx

            async def broken_outbox(*_args, **_kwargs):
                raise RuntimeError("outbox unavailable")

            main._enqueue_outbox_in_tx = broken_outbox
            try:
                with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
                    await main.api_admin_set_role(DummyRequest({
                        **decision_body, "operation_id": str(uuid.uuid4()),
                    }, checker))
            finally:
                main._enqueue_outbox_in_tx = original_outbox
            with sqlite3.connect(main.DB_PATH) as db:
                role_after_failed_delivery = db.execute(
                    "SELECT role FROM members WHERE user_id=?", (target,),
                ).fetchone()[0]
                pending_after_failed_delivery = db.execute(
                    "SELECT status FROM admin_role_changes WHERE id=?", (change_id,),
                ).fetchone()[0]
            applied = await main.api_admin_set_role(DummyRequest(decision_body, checker))
            replay = await main.api_admin_set_role(DummyRequest(decision_body, checker))
            demotion_requested = await main.api_admin_set_role(DummyRequest({
                "action": "request", "user_id": target, "role": "helper",
                "reason": "Доступ больше не нужен после смены",
                "operation_id": str(uuid.uuid4()),
            }, maker))
            demotion_id = response_json(demotion_requested)["change_id"]
            demoted = await main.api_admin_set_role(DummyRequest({
                "action": "decide", "change_id": demotion_id, "decision": "approve",
                "note": "Активных обязательств нет", "operation_id": str(uuid.uuid4()),
            }, checker))
        finally:
            main._require_admin = original_admin
        self.assertEqual(
            (
                requested.status, maker_denied.status, applied.status, replay.status,
                demotion_requested.status, demoted.status,
            ),
            (200, 403, 200, 200, 200, 200),
        )
        self.assertTrue(response_json(requested)["queued"])
        self.assertTrue(response_json(replay)["idempotent"])
        self.assertEqual(
            (role_after_failed_delivery, pending_after_failed_delivery),
            ("helper", "pending"),
        )
        with sqlite3.connect(main.DB_PATH) as db:
            role = db.execute(
                "SELECT role FROM members WHERE user_id=?", (target,),
            ).fetchone()[0]
            changes = db.execute(
                "SELECT status,requested_by,decided_by FROM admin_role_changes ORDER BY id"
            ).fetchall()
            outbox_count = db.execute(
                "SELECT COUNT(*) FROM task_outbox "
                "WHERE event_key LIKE 'admin_role_change:%'"
            ).fetchone()[0]
        self.assertEqual(role, "helper")
        self.assertEqual(changes, [("applied", maker, checker), ("applied", maker, checker)])
        self.assertGreaterEqual(outbox_count, 6)


if __name__ == "__main__":
    unittest.main()
