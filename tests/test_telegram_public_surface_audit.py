import unittest

from scripts import telegram_public_surface_audit as audit


BASE_ENV = {
    "BOT_USERNAME": "BbGalterbot",
    "GROUP_USERNAME": "bbbikefan",
    "WEBAPP_SHORTNAME": "bibibike",
    "PREFLIGHT_EXPECTED_BOT_NAME": "БибиЗадачи · Бибибайк",
    "PREFLIGHT_EXPECTED_GROUP_TITLE": "Бибибайк | Сообщество помощников",
    "PREFLIGHT_EXPECTED_GROUP_DESCRIPTION": (
        "Помогаем Бибибайку в своём городе и получаем бибибонусы на поездки. "
        "Задания: @BbGalterbot → «Открыть задания». 1 бонус = 1 ₽, минута — 8,5 ₽."
    ),
}


def page(title, description="", *, href="", action=""):
    link = (
        f'<a class="tgme_action_button_new shine" href="{href}">{action}</a>'
        if href else ""
    )
    return f"""
        <html><head>
          <meta property="og:title" content="{title}">
          <meta property="og:description" content="{description}">
          <meta property="og:image" content="https://cdn4.telesco.pe/avatar.jpg">
        </head><body>{link}</body></html>
    """


def valid_pages():
    return {
        "https://t.me/bbbikefan": page(
            BASE_ENV["PREFLIGHT_EXPECTED_GROUP_TITLE"],
            BASE_ENV["PREFLIGHT_EXPECTED_GROUP_DESCRIPTION"],
        ),
        "https://t.me/BbGalterbot": page(
            BASE_ENV["PREFLIGHT_EXPECTED_BOT_NAME"],
            href="tg://resolve?domain=BbGalterbot", action="Start Bot",
        ),
        "https://t.me/BbGalterbot/bibibike": page(
            BASE_ENV["PREFLIGHT_EXPECTED_BOT_NAME"],
            href="tg://resolve?domain=BbGalterbot&amp;appname=bibibike",
            action="Open App",
        ),
    }


class PublicTelegramSurfaceAuditTests(unittest.TestCase):
    def test_expected_public_surface_passes_without_secrets(self):
        pages = valid_pages()
        report = audit.run_public_surface_audit(
            BASE_ENV, fetch=lambda url: pages[url],
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"], {"pass": 13, "warn": 1, "fail": 0})
        self.assertEqual(report["scope"], "public_t_me_surface_no_secrets")

    def test_stale_live_branding_is_a_required_failure(self):
        pages = valid_pages()
        pages["https://t.me/bbbikefan"] = page(
            "Зеленый велосипед | Клуб любителей мягкой сидушки",
            "Старое описание",
        )
        pages["https://t.me/BbGalterbot"] = page(
            "BibiПомощник",
            href="tg://resolve?domain=BbGalterbot", action="Start Bot",
        )
        pages["https://t.me/BbGalterbot/bibibike"] = page(
            "BibiПомощник",
            href="tg://resolve?domain=BbGalterbot&amp;appname=bibibike",
            action="Open App",
        )
        report = audit.run_public_surface_audit(
            BASE_ENV, fetch=lambda url: pages[url],
        )
        failed = {
            item["name"] for item in report["checks"] if item["status"] == "fail"
        }
        self.assertFalse(report["ok"])
        self.assertEqual(
            failed,
            {
                "public group brand", "public group route copy",
                "public bot brand", "named Mini App brand",
            },
        )

    def test_named_app_public_preview_never_claims_registration(self):
        pages = valid_pages()
        pages["https://t.me/BbGalterbot/bibibike"] = page(
            BASE_ENV["PREFLIGHT_EXPECTED_BOT_NAME"],
            href="tg://resolve?domain=BbGalterbot&amp;appname=wrong",
            action="Open App",
        )
        report = audit.run_public_surface_audit(
            BASE_ENV, fetch=lambda url: pages[url],
        )
        route = next(
            item for item in report["checks"]
            if item["name"] == "named Mini App registration"
        )
        self.assertEqual(route["status"], "warn")
        self.assertTrue(report["ok"])
        self.assertIn("cannot prove", " ".join(report["limitations"]))

    def test_malformed_identifiers_fail_before_network_access(self):
        calls = []
        report = audit.run_public_surface_audit(
            {**BASE_ENV, "BOT_USERNAME": "bad/name"},
            fetch=lambda url: calls.append(url),
        )
        self.assertFalse(report["ok"])
        self.assertEqual(calls, [])

    def test_transport_failures_are_reported_without_claiming_success(self):
        def unavailable(_url):
            raise RuntimeError("network unavailable")

        report = audit.run_public_surface_audit(BASE_ENV, fetch=unavailable)
        self.assertFalse(report["ok"])
        failures = [
            item for item in report["checks"]
            if item["name"].endswith("public preview")
        ]
        self.assertEqual(len(failures), 3)
        self.assertTrue(all(item["status"] == "fail" for item in failures))

    def test_avatar_absence_is_visible_but_does_not_overclaim_artwork(self):
        pages = valid_pages()
        pages["https://t.me/BbGalterbot"] = pages[
            "https://t.me/BbGalterbot"
        ].replace(
            '<meta property="og:image" content="https://cdn4.telesco.pe/avatar.jpg">',
            '<meta property="og:image" content="">',
        )
        report = audit.run_public_surface_audit(
            BASE_ENV, fetch=lambda url: pages[url],
        )
        avatar = next(
            item for item in report["checks"] if item["name"] == "public bot avatar"
        )
        self.assertTrue(report["ok"])
        self.assertEqual(avatar["status"], "warn")
        self.assertIn("artwork", " ".join(report["limitations"]))


if __name__ == "__main__":
    unittest.main()
