import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
HTML = (ROOT / "index.html").read_text(encoding="utf-8")


class EntryModeContractTests(unittest.TestCase):
    def test_release_versions_match(self):
        app_version = re.search(r'^APP_VERSION = "([^"]+)"', MAIN, re.MULTILINE)
        self.assertIsNotNone(app_version)
        self.assertEqual(app_version.group(1), "v2.14.0")
        self.assertIn("v2.14.0", HTML)

    def test_role_picker_is_frontend_routing_only(self):
        self.assertIn('id="entryWorker"', HTML)
        self.assertIn('id="entryAdmin"', HTML)
        self.assertIn('id="entryBack"', HTML)
        self.assertIn('STATE.entry_modes&&STATE.entry_modes.admin&&hasStaffShell()', HTML)
        self.assertNotIn("/api/admin/login',{method:'POST'", HTML)

    def test_crm_has_adaptive_fullscreen_controls(self):
        self.assertIn('id="fullscreenToggle"', HTML)
        self.assertIn('requestCRMFullscreen', HTML)
        self.assertIn('tg.requestFullscreen()', HTML)
        self.assertIn('tg.exitFullscreen()', HTML)
        self.assertIn('@media (max-width:899px)', HTML)

    def test_state_exposes_server_derived_modes(self):
        self.assertIn('is_owner = "owner" in staff_access["presets"]', MAIN)
        self.assertIn('"admin": admin', MAIN)
        self.assertIn('"default_mode": "admin" if is_owner else "worker"', MAIN)

    def test_personal_owner_id_is_not_hardcoded(self):
        # The actual owner ID belongs in the hosting secret ADMIN_IDS.
        self.assertNotIn("7785586524", MAIN)
        self.assertNotIn("7785586524", HTML)
        self.assertIn('ADMIN_IDS = _parse_ids(os.getenv("ADMIN_IDS", ""))', MAIN)

    def test_financial_and_owner_safety_checks_remain(self):
        self.assertIn('"error": "maker_checker"', MAIN)
        self.assertIn('"error": "two_person_rule"', MAIN)
        self.assertIn('if len(ADMIN_IDS) < 2:', MAIN)

    def test_database_schema_is_unchanged_by_ui_release(self):
        schema = re.search(r'^SQLITE_SCHEMA_VERSION = (\d+)', MAIN, re.MULTILINE)
        self.assertIsNotNone(schema)
        self.assertEqual(schema.group(1), "300")


if __name__ == "__main__":
    unittest.main()
