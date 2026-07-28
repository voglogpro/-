import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts import telegram_staging_cleanup as cleanup


TOKEN = "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


class FakeTelegram:
    def __init__(self, *, identity_id=900001, username="BibiLoadTestBot", sticky=False):
        self.identity_id = identity_id
        self.username = username
        self.sticky = sticky
        self.calls = []

    def __call__(self, method, params=None):
        self.calls.append((method, params))
        if method == "getMe":
            return {"id": self.identity_id, "is_bot": True, "username": self.username}
        if method == "deleteWebhook":
            return {"value": True}
        if method == "getWebhookInfo":
            return {"url": "https://still.example/hook" if self.sticky else ""}
        raise AssertionError(method)


class TelegramStagingCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.token = Path(self.temp.name) / "bot-token"
        self.token.write_text(TOKEN + "\n", encoding="utf-8")
        if os.name != "nt":
            self.token.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_only_verifies_identity_and_never_leaks_token(self):
        api = FakeTelegram()
        report = cleanup.reconcile(
            token_file=self.token, expected_bot_id=900001,
            expected_username="BibiLoadTestBot", apply=False, api_call=api,
        )
        self.assertEqual([name for name, _ in api.calls], ["getMe"])
        self.assertNotIn(TOKEN, json.dumps(report))
        self.assertTrue(report["would_delete_webhook"])

    def test_apply_deletes_without_dropping_updates_and_confirms_empty_url(self):
        api = FakeTelegram()
        report = cleanup.reconcile(
            token_file=self.token, expected_bot_id=900001,
            expected_username="BibiLoadTestBot", apply=True,
            confirm_username="@BibiLoadTestBot", api_call=api,
        )
        self.assertTrue(report["webhook_removed"])
        self.assertEqual(api.calls[1], ("deleteWebhook", {"drop_pending_updates": "false"}))
        self.assertEqual(api.calls[-1][0], "getWebhookInfo")

    def test_identity_confirmation_and_postcondition_fail_closed(self):
        with self.assertRaisesRegex(cleanup.CleanupError, "identity"):
            cleanup.reconcile(
                token_file=self.token, expected_bot_id=900002,
                expected_username="BibiLoadTestBot", apply=False,
                api_call=FakeTelegram(),
            )
        with self.assertRaisesRegex(cleanup.CleanupError, "not confirmed"):
            cleanup.reconcile(
                token_file=self.token, expected_bot_id=900001,
                expected_username="BibiLoadTestBot", apply=True,
                confirm_username="BibiLoadTestBot", api_call=FakeTelegram(sticky=True),
            )
        with self.assertRaisesRegex(cleanup.CleanupError, "confirmation"):
            cleanup.reconcile(
                token_file=self.token, expected_bot_id=900001,
                expected_username="BibiLoadTestBot", apply=True,
                confirm_username="OtherLoadBot", api_call=FakeTelegram(),
            )


if __name__ == "__main__":
    unittest.main()
