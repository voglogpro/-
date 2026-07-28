import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.bootstrap_production_env import (
    DEFAULT_BOT_DESCRIPTION,
    DEFAULT_BOT_MENU_TEXT,
    DEFAULT_BOT_SHORT_DESCRIPTION,
)
from scripts.telegram_surface_setup import COMMANDS, reconcile_surface


def env():
    return {
        "BOT_TOKEN": "123456:" + "abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMN",
        "BOT_USERNAME": "BbGalterbot",
        "PREFLIGHT_EXPECTED_BOT_NAME": "Бибибайк",
        "BOT_PROFILE_DESCRIPTION": DEFAULT_BOT_DESCRIPTION,
        "BOT_PROFILE_SHORT_DESCRIPTION": DEFAULT_BOT_SHORT_DESCRIPTION,
        "BOT_MENU_TEXT": DEFAULT_BOT_MENU_TEXT,
        "MINI_APP_URL": "https://tasks.example.test/",
    }


class FakeTelegram:
    def __init__(self, *, current=False):
        self.writes = []
        self.avatar_sequence = []
        self.state = {
            "username": "BbGalterbot",
            "name": "Бибибайк" if current else "BibiПомощник",
            "description": DEFAULT_BOT_DESCRIPTION if current else "",
            "short_description": DEFAULT_BOT_SHORT_DESCRIPTION if current else "",
            "commands": list(COMMANDS) if current else [],
            "menu": {
                "type": "web_app", "text": DEFAULT_BOT_MENU_TEXT,
                "web_app": {"url": "https://tasks.example.test/"},
            } if current else {"type": "default"},
            "avatar": "old-avatar",
        }

    def __call__(self, method, params=None):
        if method == "getMe":
            return {"id": 777, "username": self.state["username"]}
        if method == "getMyName":
            return {"name": self.state["name"]}
        if method == "getMyDescription":
            return {"description": self.state["description"]}
        if method == "getMyShortDescription":
            return {"short_description": self.state["short_description"]}
        if method == "getMyCommands":
            return {"value": self.state["commands"]}
        if method == "getChatMenuButton":
            return self.state["menu"]
        if method == "getUserProfilePhotos":
            if self.avatar_sequence:
                self.state["avatar"] = self.avatar_sequence.pop(0)
            return {
                "total_count": 1,
                "photos": [[{"file_unique_id": self.state["avatar"]}]],
            }
        self.writes.append(method)
        if method == "setMyName":
            self.state["name"] = params["name"]
        elif method == "setMyDescription":
            self.state["description"] = params["description"]
        elif method == "setMyShortDescription":
            self.state["short_description"] = params["short_description"]
        elif method == "setMyCommands":
            self.state["commands"] = json.loads(params["commands"])
        elif method == "setChatMenuButton":
            self.state["menu"] = json.loads(params["menu_button"])
        else:
            raise AssertionError(method)
        return {"value": True}


class TelegramSurfaceSetupTests(unittest.TestCase):
    def test_current_surface_is_a_noop(self):
        api = FakeTelegram(current=True)
        report = reconcile_surface(env(), api)
        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "already_current")
        self.assertEqual(api.writes, [])

    def test_dry_run_never_writes_and_apply_requires_exact_confirmation(self):
        api = FakeTelegram()
        planned = reconcile_surface(env(), api)
        self.assertEqual(planned["status"], "changes_required")
        self.assertEqual(api.writes, [])
        denied = reconcile_surface(env(), api, apply=True, confirm_bot="OtherBot")
        self.assertEqual(denied["status"], "confirmation_required")
        self.assertEqual(api.writes, [])
        applied = reconcile_surface(
            env(), api, apply=True, confirm_bot="@BbGalterbot",
        )
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(len(api.writes), 5)

        with tempfile.TemporaryDirectory() as root:
            avatar = Path(root) / "logo.jpg"
            Image.new("RGB", (640, 640), "green").save(avatar, "JPEG")
            dry_run = reconcile_surface(env(), api, avatar_file=avatar)
            self.assertEqual(dry_run["status"], "changes_required")
            self.assertEqual(dry_run["changes"], ["avatar"])

            def upload(_token, uploaded):
                self.assertEqual(uploaded, avatar.resolve())
                api.state["avatar"] = "new-avatar"
                return {"value": True}

            avatar_result = reconcile_surface(
                env(), api, apply=True, confirm_bot="@BbGalterbot",
                avatar_file=avatar, avatar_upload=upload,
            )
            self.assertTrue(avatar_result["ok"])
            self.assertEqual(avatar_result["avatar_file_unique_id"], "new-avatar")
            self.assertEqual(avatar_result["avatar_verification"], "new_file_unique_id")

            same_result = reconcile_surface(
                env(), api, apply=True, confirm_bot="@BbGalterbot",
                avatar_file=avatar, avatar_upload=lambda _token, _path: {"value": True},
                avatar_sleep=lambda _seconds: None,
            )
            self.assertTrue(same_result["ok"])
            self.assertEqual(
                same_result["avatar_verification"],
                "api_confirmed_same_file_unique_id",
            )

            def delayed_upload(_token, _path):
                api.avatar_sequence = ["new-avatar", "eventually-visible-avatar"]
                return {"value": True}

            delayed_result = reconcile_surface(
                env(), api, apply=True, confirm_bot="@BbGalterbot",
                avatar_file=avatar, avatar_upload=delayed_upload,
                avatar_sleep=lambda _seconds: None,
            )
            self.assertTrue(delayed_result["ok"])
            self.assertEqual(delayed_result["avatar_file_unique_id"], "eventually-visible-avatar")

    def test_errors_never_include_token(self):
        values = env()

        def leaking(_method, _params=None):
            raise RuntimeError("transport " + values["BOT_TOKEN"])

        report = reconcile_surface(values, leaking)
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(values["BOT_TOKEN"], serialized)
        self.assertIn("[redacted]", serialized)


if __name__ == "__main__":
    unittest.main()
