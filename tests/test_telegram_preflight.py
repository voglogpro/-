import json
import unittest

from scripts.telegram_preflight import run_preflight


def base_env():
    return {
        "BOT_TOKEN": "123456:" + "abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMN",
        "BOT_USERNAME": "BbGalterbot",
        "GROUP_ID": "-1001111111111",
        "GROUP_USERNAME": "bbbikefan",
        "OPS_GROUP_ID": "-1002222222222",
        "PUBLIC_BASE_URL": "https://tasks.example.test",
        "TELEGRAM_UPDATE_MODE": "webhook",
        "WEBHOOK_ROUTE_ID": "route_" + "x" * 40,
        "PREFLIGHT_EXPECTED_BOT_NAME": "Бибибайк",
        "PREFLIGHT_EXPECTED_GROUP_TITLE": "Бибибайк",
    }


def good_api(method, params=None):
    if method == "getMe":
        return {"id": 777, "username": "BbGalterbot", "can_join_groups": True}
    if method == "getMyName":
        return {"name": "Бибибайк"}
    if method == "getChat":
        if params["chat_id"] == -1001111111111:
            return {"type": "supergroup", "is_forum": True, "username": "bbbikefan", "title": "Бибибайк · Команда"}
        return {"type": "supergroup", "is_forum": True, "title": "БибиЗадачи OPS"}
    if method == "getChatMember":
        if params["chat_id"] == -1001111111111:
            return {"status": "administrator", "can_delete_messages": True}
        return {"status": "member"}
    if method == "getWebhookInfo":
        return {
            "url": "https://tasks.example.test/telegram/webhook/route_" + "x" * 40,
            "pending_update_count": 0,
        }
    if method == "getChatMenuButton":
        return {"type": "web_app", "web_app": {"url": "https://tasks.example.test/"}}
    raise AssertionError(method)


class TelegramPreflightTests(unittest.TestCase):
    def test_happy_path_is_green_and_report_never_contains_token(self):
        env = base_env()
        report = run_preflight(env, good_api)
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertNotIn(env["BOT_TOKEN"], json.dumps(report, ensure_ascii=False))

        def leaking_error(_method, _params=None):
            raise RuntimeError("transport included " + env["BOT_TOKEN"])

        failed_report = run_preflight(env, leaking_error)
        serialized = json.dumps(failed_report, ensure_ascii=False)
        self.assertNotIn(env["BOT_TOKEN"], serialized)
        self.assertIn("[redacted]", serialized)

    def test_public_ops_or_brand_mismatch_fails(self):
        def mismatched(method, params=None):
            value = good_api(method, params)
            if method == "getMyName":
                return {"name": "BibiПомощник"}
            if method == "getChat" and params["chat_id"] == -1002222222222:
                return {**value, "username": "public_ops"}
            return value

        report = run_preflight(base_env(), mismatched)
        failed = {item["name"] for item in report["checks"] if item["status"] == "fail"}
        self.assertFalse(report["ok"])
        self.assertIn("bot brand name", failed)
        self.assertIn("OPS privacy", failed)

    def test_missing_configuration_stops_before_telegram(self):
        called = False

        def should_not_call(_method, _params=None):
            nonlocal called
            called = True
            return {}

        report = run_preflight({}, should_not_call)
        self.assertFalse(report["ok"])
        self.assertFalse(called)
        self.assertGreaterEqual(report["summary"]["fail"], 5)


if __name__ == "__main__":
    unittest.main()
