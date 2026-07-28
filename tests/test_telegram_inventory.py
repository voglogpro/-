import unittest

from scripts.telegram_inventory import collect_inventory, inventory_from_updates


class TelegramInventoryTests(unittest.TestCase):
    def test_extracts_unique_locations_without_message_or_user_data(self):
        updates = [
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "from": {"id": 999, "first_name": "Secret"},
                    "chat": {
                        "id": -100123,
                        "type": "supergroup",
                        "title": "OPS",
                    },
                    "message_thread_id": 42,
                    "is_topic_message": True,
                    "text": "private marker text",
                },
            },
            {
                "update_id": 2,
                "message": {
                    "chat": {
                        "id": -100123,
                        "type": "supergroup",
                        "title": "OPS",
                    },
                    "message_thread_id": 42,
                    "text": "duplicate",
                },
            },
        ]
        updates[0]["message"]["text"] = "/inventory_ops_tasks@BbGalterbot"
        updates[1]["message"]["text"] = "/inventory_ops_tasks@BbGalterbot"
        result = inventory_from_updates(updates)
        self.assertEqual(len(result["locations"]), 1)
        self.assertEqual(result["locations"][0]["chat_id"], -100123)
        self.assertEqual(result["locations"][0]["message_thread_id"], 42)
        self.assertEqual(result["locations"][0]["variable"], "OPS_TOPIC_TASKS")
        rendered = repr(result)
        self.assertNotIn("private marker", rendered)
        self.assertNotIn("999", rendered)

    def test_collect_refuses_wrong_bot_and_active_webhook(self):
        wrong = collect_inventory(
            {"BOT_TOKEN": "secret", "BOT_USERNAME": "BbGalterbot"},
            lambda method, params=None: {"username": "OtherBot"},
        )
        self.assertEqual(wrong["status"], "wrong_bot")

        def active(method, params=None):
            if method == "getMe":
                return {"username": "BbGalterbot"}
            if method == "getWebhookInfo":
                return {"url": "https://example.test/hook"}
            raise AssertionError(method)

        webhook = collect_inventory(
            {"BOT_TOKEN": "secret", "BOT_USERNAME": "BbGalterbot"}, active,
        )
        self.assertEqual(webhook["status"], "webhook_active")

    def test_collect_refuses_unpageable_backlog(self):
        def api(method, params=None):
            if method == "getMe":
                return {"username": "BbGalterbot"}
            if method == "getWebhookInfo":
                return {"url": "", "pending_update_count": 100}
            raise AssertionError(method)

        report = collect_inventory(
            {"BOT_TOKEN": "secret", "BOT_USERNAME": "BbGalterbot"}, api,
        )
        self.assertEqual(report["status"], "backlog_requires_operator")

    def test_collect_returns_redacted_inventory(self):
        def api(method, params=None):
            if method == "getMe":
                return {"username": "BbGalterbot"}
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getUpdates":
                self.assertEqual(params, {"timeout": 0, "limit": 100})
                markers = [
                    ("/inventory_news@BbGalterbot", 1, -1007, "Public", "bbbikefan"),
                    ("/inventory_chat@BbGalterbot", 2, -1007, "Public", "bbbikefan"),
                    ("/inventory_work@BbGalterbot", 3, -1007, "Public", "bbbikefan"),
                    ("/inventory_franchise@BbGalterbot", 4, -1007, "Public", "bbbikefan"),
                    ("/inventory_ops_tasks@BbGalterbot", 5, -1008, "OPS", None),
                ]
                return [{"message": {
                    "chat": {
                        "id": chat_id,
                        "type": "supergroup",
                        "title": title,
                        **({"username": username} if username else {}),
                    },
                    "message_thread_id": thread_id,
                    "is_topic_message": True,
                    "text": marker,
                }} for marker, thread_id, chat_id, title, username in markers]
            raise AssertionError(method)

        report = collect_inventory(
            {"BOT_TOKEN": "secret", "BOT_USERNAME": "BbGalterbot"}, api,
        )
        self.assertTrue(report["ok"])
        public = next(
            item for item in report["locations"]
            if item["variable"] == "TOPIC_WORK"
        )
        self.assertEqual(public["chat_username"], "@bbbikefan")

    def test_admin_ids_require_private_exact_marker_and_explicit_opt_in(self):
        updates = [
            {"message": {
                "chat": {"id": 101, "type": "private"},
                "from": {"id": 101},
                "text": "/inventory_admin@BbGalterbot",
            }},
            {"message": {
                "chat": {"id": 202, "type": "private"},
                "from": {"id": 202},
                "text": "/inventory_admin@BbGalterbot",
            }},
            {"message": {
                "chat": {"id": 303, "type": "private"},
                "from": {"id": 303},
                "text": "please show my id",
            }},
            {"message": {
                "chat": {"id": 404, "type": "private"},
                "from": {"id": 404},
                "text": "/inventory_admin@OtherBot",
            }},
            {"message": {
                "chat": {"id": 505, "type": "private"},
                "from": {"id": 505},
                "text": "/inventory_admin@BbGalterbot extra",
            }},
        ]
        hidden = inventory_from_updates(updates)
        self.assertEqual(hidden["admin_ids"], [])
        shown = inventory_from_updates(updates, include_admin_ids=True)
        self.assertEqual(shown["admin_ids"], [101, 202])


if __name__ == "__main__":
    unittest.main()
