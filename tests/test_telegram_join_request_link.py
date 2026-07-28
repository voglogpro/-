import json
import unittest

from scripts.telegram_join_request_link import (
    JOIN_LINK_NAME,
    _invite_url,
    reconcile_join_request_link,
)


TOKEN = "123456:" + "abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMN"
CREATED_URL = "https://t.me/+abcdefghijklmnopqrstuv"


def env(*, invite_url=""):
    return {
        "BOT_TOKEN": TOKEN,
        "BOT_USERNAME": "BbGalterbot",
        "GROUP_ID": "-1001111111111",
        "JOIN_REQUEST_INVITE_URL": invite_url,
    }


class FakeTelegram:
    def __init__(self, *, username="BbGalterbot", result=None):
        self.username = username
        self.result = result
        self.calls = []

    def __call__(self, method, params=None):
        self.calls.append((method, params))
        if method == "getMe":
            return {"id": 777, "is_bot": True, "username": self.username}
        if method in {"createChatInviteLink", "editChatInviteLink"}:
            if self.result is not None:
                return self.result
            return {
                "invite_link": params.get("invite_link", CREATED_URL),
                "creator": {"id": 777, "is_bot": True},
                "creates_join_request": True,
                "is_revoked": False,
                "name": JOIN_LINK_NAME,
            }
        raise AssertionError(method)


class TelegramJoinRequestLinkTests(unittest.TestCase):
    def test_dry_run_verifies_bot_and_never_writes(self):
        api = FakeTelegram()
        report = reconcile_join_request_link(env(), api)
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "changes_required")
        self.assertEqual(report["plan"]["action"], "create")
        self.assertEqual(api.calls, [("getMe", None)])

        edit_api = FakeTelegram()
        edit = reconcile_join_request_link(env(invite_url=CREATED_URL), edit_api)
        self.assertEqual(edit["plan"]["action"], "edit")
        self.assertEqual(edit["plan"]["invite_url"], CREATED_URL)
        self.assertEqual(edit_api.calls, [("getMe", None)])

    def test_apply_requires_exact_confirmation_and_expected_bot(self):
        api = FakeTelegram()
        denied = reconcile_join_request_link(
            env(), api, apply=True, confirm_bot="OtherBot",
        )
        self.assertEqual(denied["status"], "confirmation_required")
        self.assertEqual(api.calls, [("getMe", None)])

        wrong = reconcile_join_request_link(
            env(), FakeTelegram(username="OtherBot"),
            apply=True, confirm_bot="@BbGalterbot",
        )
        self.assertEqual(wrong["status"], "wrong_bot")

    def test_create_uses_join_request_only_and_returns_verified_url(self):
        api = FakeTelegram()
        report = reconcile_join_request_link(
            env(), api, apply=True, confirm_bot="@BbGalterbot",
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["action"], "create")
        self.assertEqual(report["invite_url"], CREATED_URL)
        self.assertTrue(report["persist_invite_url"])
        method, params = api.calls[-1]
        self.assertEqual(method, "createChatInviteLink")
        self.assertEqual(params, {
            "chat_id": -1001111111111,
            "name": JOIN_LINK_NAME,
            "creates_join_request": True,
        })

    def test_existing_url_is_edited_without_creating_another_link(self):
        api = FakeTelegram()
        report = reconcile_join_request_link(
            env(invite_url=CREATED_URL), api,
            apply=True, confirm_bot="BbGalterbot",
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["action"], "edit")
        self.assertFalse(report["persist_invite_url"])
        method, params = api.calls[-1]
        self.assertEqual(method, "editChatInviteLink")
        self.assertEqual(params["invite_link"], CREATED_URL)
        self.assertTrue(params["creates_join_request"])

    def test_response_must_be_owned_active_and_join_request_enabled(self):
        invalid_results = (
            {
                "invite_link": CREATED_URL, "creator": {"id": 778},
                "creates_join_request": True, "is_revoked": False,
                "name": JOIN_LINK_NAME,
            },
            {
                "invite_link": CREATED_URL, "creator": {"id": 777},
                "creates_join_request": False, "is_revoked": False,
                "name": JOIN_LINK_NAME,
            },
            {
                "invite_link": CREATED_URL, "creator": {"id": 777},
                "creates_join_request": True, "is_revoked": True,
                "name": JOIN_LINK_NAME,
            },
            {
                "invite_link": CREATED_URL, "creator": {"id": 777},
                "creates_join_request": True, "name": JOIN_LINK_NAME,
            },
            {
                "invite_link": "https://example.com/invite", "creator": {"id": 777},
                "creates_join_request": True, "is_revoked": False,
                "name": JOIN_LINK_NAME,
            },
            {
                "invite_link": CREATED_URL, "creator": {"id": 777},
                "creates_join_request": True, "is_revoked": False,
                "name": "wrong",
            },
        )
        for result in invalid_results:
            with self.subTest(result=result):
                report = reconcile_join_request_link(
                    env(), FakeTelegram(result=result),
                    apply=True, confirm_bot="BbGalterbot",
                )
                self.assertFalse(report["ok"])
                self.assertEqual(report["status"], "telegram_error")

    def test_edit_must_return_the_configured_link(self):
        different = "https://t.me/+zyxwvutsrqponmlkjihgfe"
        api = FakeTelegram(result={
            "invite_link": different, "creator": {"id": 777},
            "creates_join_request": True, "is_revoked": False,
            "name": JOIN_LINK_NAME,
        })
        report = reconcile_join_request_link(
            env(invite_url=CREATED_URL), api,
            apply=True, confirm_bot="BbGalterbot",
        )
        self.assertEqual(report["status"], "telegram_error")
        self.assertIn("different", report["error"])

    def test_invite_url_validation_is_strict(self):
        valid = (
            CREATED_URL,
        )
        invalid = (
            "http://t.me/+abcdefghijklmnopqrstuv",
            "https://telegram.me/+abcdefghijklmnopqrstuv",
            "https://t.me/BbGalterbot",
            "https://t.me/+short",
            "https://t.me/+abcdefghijklmnopqrstuv?x=1",
            "https://user@t.me/+abcdefghijklmnopqrstuv",
            " https://t.me/+abcdefghijklmnopqrstuv",
            "https://t.me/%2Babcdefghijklmnopqrstuv",
            "https://t.me/joinchat/abcdefghijklmnopqrstuv",
        )
        for value in valid:
            with self.subTest(valid=value):
                self.assertEqual(_invite_url(value), value)
        for value in invalid:
            with self.subTest(invalid=value):
                self.assertEqual(_invite_url(value), "")

    def test_invalid_configuration_stops_before_telegram_and_token_is_redacted(self):
        calls = []
        report = reconcile_join_request_link(
            {**env(), "GROUP_ID": "@bbbikefan"},
            lambda method, params=None: calls.append((method, params)),
        )
        self.assertEqual(report["status"], "invalid_config")
        self.assertEqual(calls, [])

        def leaking(_method, _params=None):
            raise RuntimeError("transport " + TOKEN)

        leaked = reconcile_join_request_link(env(), leaking)
        serialized = json.dumps(leaked, ensure_ascii=False)
        self.assertNotIn(TOKEN, serialized)
        self.assertIn("[redacted]", serialized)


if __name__ == "__main__":
    unittest.main()
