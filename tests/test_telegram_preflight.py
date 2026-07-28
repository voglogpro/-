import json
import unittest
from datetime import datetime

from scripts.telegram_preflight import BOT_COMMANDS, run_preflight


def base_env():
    return {
        "BOT_TOKEN": "123456:" + "abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMN",
        "BOT_USERNAME": "BbGalterbot",
        "GROUP_ID": "-1001111111111",
        "GROUP_USERNAME": "bbbikefan",
        "OPS_GROUP_ID": "-1002222222222",
        "PUBLIC_BASE_URL": "https://tasks.example.test",
        "MINI_APP_URL": "https://tasks.example.test/",
        "PRIVACY_URL": "https://tasks.example.test/privacy",
        "PRIVACY_CONTROLLER_NAME": "ООО Бибибайк",
        "PRIVACY_CONTACT": "@privacy_bibibike",
        "WEBAPP_SHORTNAME": "bibibike",
        "PREFLIGHT_REQUIRE_MAIN_MINI_APP": "true",
        "JOIN_REQUEST_ADMISSION_ENABLED": "true",
        "JOIN_REQUEST_INVITE_URL": "https://t.me/+abcdefghijklmnopQRSTUV",
        "TELEGRAM_UPDATE_MODE": "webhook",
        "WEBHOOK_ROUTE_ID": "route_" + "x" * 40,
        "PREFLIGHT_EXPECTED_BOT_NAME": "БибиЗадачи · Бибибайк",
        "PREFLIGHT_EXPECTED_GROUP_TITLE": "Бибибайк | Сообщество помощников",
        "PREFLIGHT_EXPECTED_GROUP_DESCRIPTION": "Помогаем Бибибайку в своём городе и получаем бибибонусы на поездки. Задания: @BbGalterbot → «Открыть задания». 1 бонус = 1 ₽, минута — 8,5 ₽.",
        "BOT_PROFILE_DESCRIPTION": "Полное описание",
        "BOT_PROFILE_SHORT_DESCRIPTION": "Короткое описание",
        "BOT_MENU_TEXT": "Открыть задания",
        "TOPIC_NEWS": "11",
        "TOPIC_CHAT": "12",
        "TOPIC_WORK": "13",
        "TOPIC_FRANCHISE": "14",
        "OPS_TOPIC_TASKS": "21",
    }


def good_api(method, params=None):
    if method == "getMe":
        return {
            "id": 777, "username": "BbGalterbot", "can_join_groups": True,
            "has_main_web_app": True,
        }
    if method == "getMyName":
        return {"name": "БибиЗадачи · Бибибайк"}
    if method == "getMyDescription":
        return {"description": "Полное описание"}
    if method == "getMyShortDescription":
        return {"short_description": "Короткое описание"}
    if method == "getMyCommands":
        return {"value": list(BOT_COMMANDS)}
    if method == "getChat":
        if params["chat_id"] == -1001111111111:
            return {
                "type": "supergroup", "is_forum": True,
                "join_by_request": True,
                "username": "bbbikefan",
                "title": "Бибибайк | Сообщество помощников",
                "description": base_env()["PREFLIGHT_EXPECTED_GROUP_DESCRIPTION"],
            }
        return {"type": "supergroup", "is_forum": True, "title": "БибиЗадачи OPS"}
    if method == "getChatMember":
        if params["chat_id"] == -1001111111111:
            return {
                "status": "administrator", "can_delete_messages": True,
                "can_invite_users": True,
            }
        return {"status": "member"}
    if method == "getWebhookInfo":
        return {
            "url": "https://tasks.example.test/telegram/webhook/route_" + "x" * 40,
            "pending_update_count": 0,
            "allowed_updates": [
                "message", "callback_query", "chat_member", "chat_join_request",
            ],
        }
    if method == "getChatMenuButton":
        return {
            "type": "web_app", "text": "Открыть задания",
            "web_app": {"url": "https://tasks.example.test/"},
        }
    raise AssertionError(method)


def good_privacy(url):
    return {
        "status": 200,
        "final_url": url,
        "content_type": "text/html; charset=utf-8",
        "body": (
            '<html data-bibitasks-privacy-version="1">'
            "ООО Бибибайк @privacy_bibibike</html>"
        ),
    }


class TelegramPreflightTests(unittest.TestCase):
    def test_happy_path_is_green_and_report_never_contains_token(self):
        env = base_env()
        report = run_preflight(env, good_api, good_privacy)
        self.assertTrue(report["ok"])
        self.assertEqual(report["report_version"], 1)
        self.assertIsNotNone(datetime.fromisoformat(report["generated_at"]).tzinfo)
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertNotIn(env["BOT_TOKEN"], json.dumps(report, ensure_ascii=False))

        def leaking_error(_method, _params=None):
            raise RuntimeError("transport included " + env["BOT_TOKEN"])

        failed_report = run_preflight(env, leaking_error)
        serialized = json.dumps(failed_report, ensure_ascii=False)
        self.assertNotIn(env["BOT_TOKEN"], serialized)
        self.assertIn("[redacted]", serialized)

    def test_privacy_page_must_be_reachable_versioned_and_same_origin(self):
        for probe in (
            lambda url: {**good_privacy(url), "status": 404},
            lambda url: {**good_privacy(url), "final_url": "https://other.example.test/privacy"},
            lambda url: {**good_privacy(url), "body": "<html>old policy</html>"},
        ):
            with self.subTest(probe=probe):
                report = run_preflight(base_env(), good_api, probe)
                self.assertFalse(report["ok"])
                self.assertIn(
                    "privacy policy HTTP",
                    {
                        item["name"] for item in report["checks"]
                        if item["status"] == "fail"
                    },
                )

        external = {
            **base_env(), "PRIVACY_URL": "https://policy.example.test/privacy",
        }
        report = run_preflight(external, good_api, good_privacy)
        self.assertFalse(report["ok"])
        self.assertIn(
            "same-origin privacy policy",
            {item["name"] for item in report["checks"] if item["status"] == "fail"},
        )

    def test_join_request_gate_permissions_and_webhook_update_are_required(self):
        variants = (
            ("getChat", "join_by_request", False, "public join-by-request gate"),
            ("getChatMember", "can_invite_users", False, "public invite permission"),
            ("getWebhookInfo", "allowed_updates", ["message"], "webhook join-request updates"),
        )
        for method_name, field, value, expected_check in variants:
            with self.subTest(check=expected_check):
                def broken(method, params=None):
                    result = good_api(method, params)
                    if method != method_name:
                        return result
                    if method == "getChat" and params["chat_id"] != -1001111111111:
                        return result
                    if method == "getChatMember" and params["chat_id"] != -1001111111111:
                        return result
                    return {**result, field: value}

                report = run_preflight(base_env(), broken, good_privacy)
                self.assertFalse(report["ok"])
                self.assertIn(
                    expected_check,
                    {
                        item["name"] for item in report["checks"]
                        if item["status"] == "fail"
                    },
                )

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

    def test_production_requires_exact_menu_and_supported_https_port(self):
        def missing_menu(method, params=None):
            if method == "getChatMenuButton":
                return {"type": "default"}
            return good_api(method, params)

        report = run_preflight(base_env(), missing_menu)
        self.assertFalse(report["ok"])
        self.assertIn(
            "menu Mini App",
            {item["name"] for item in report["checks"] if item["status"] == "fail"},
        )

        env = {**base_env(), "PUBLIC_BASE_URL": "https://tasks.example.test:9443"}
        report = run_preflight(env, good_api)
        self.assertFalse(report["ok"])

    def test_production_requires_safe_public_privacy_url_before_telegram(self):
        for value in (
            "",
            "http://tasks.example.test/privacy",
            "https://user:pass@tasks.example.test/privacy",
            "https://tasks.example.test:9443/privacy",
            "https://localhost/privacy",
            "https://127.0.0.1/privacy",
            "https://tasks.example.test/privacy\nX-Injected: yes",
        ):
            called = False

            def should_not_call(_method, _params=None):
                nonlocal called
                called = True
                return {}

            with self.subTest(value=value):
                report = run_preflight({**base_env(), "PRIVACY_URL": value}, should_not_call)
                self.assertFalse(report["ok"])
                self.assertFalse(called)
                self.assertIn(
                    "PRIVACY_URL",
                    {item["name"] for item in report["checks"] if item["status"] == "fail"},
                )

    def test_production_cannot_disable_main_app_or_accept_stale_profile(self):
        disabled = {**base_env(), "PREFLIGHT_REQUIRE_MAIN_MINI_APP": "false"}
        report = run_preflight(disabled, good_api)
        self.assertFalse(report["ok"])
        self.assertIn(
            "main Mini App policy",
            {item["name"] for item in report["checks"] if item["status"] == "fail"},
        )

        def stale_surface(method, params=None):
            value = good_api(method, params)
            if method == "getMyName":
                return {"name": "X Бибибайк X"}
            if method == "getMyCommands":
                return {"value": list(BOT_COMMANDS) + [
                    {"command": "legacy", "description": "Старая команда"},
                ]}
            return value

        report = run_preflight(base_env(), stale_surface)
        failed = {item["name"] for item in report["checks"] if item["status"] == "fail"}
        self.assertIn("bot brand name", failed)
        self.assertIn("bot commands", failed)


if __name__ == "__main__":
    unittest.main()
