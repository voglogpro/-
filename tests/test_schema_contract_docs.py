import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DOC = ROOT / "docs" / "TECHNICAL-README.md"


def assigned_constant(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a literal module constant in {path}")


class SchemaContractDocumentationTests(unittest.TestCase):
    def test_documented_sqlite_and_alembic_versions_match_code(self):
        sqlite_version = assigned_constant(ROOT / "main.py", "SQLITE_SCHEMA_VERSION")
        alembic_head = assigned_constant(
            ROOT / "db_migration" / "__init__.py", "ALEMBIC_HEAD",
        )
        documentation = CONTRACT_DOC.read_text(encoding="utf-8")
        match = re.search(
            r"<!-- bibitasks-schema-contract: sqlite=(\d+) "
            r"alembic=([a-z0-9_]+) -->",
            documentation,
        )
        self.assertIsNotNone(match, "machine-readable schema contract is missing")
        self.assertEqual(int(match.group(1)), sqlite_version)
        self.assertEqual(match.group(2), alembic_head)
        self.assertTrue(
            (ROOT / "migrations" / "versions" / f"{alembic_head}.py").is_file(),
            "ALEMBIC_HEAD does not name a checked-in migration",
        )

    def test_release_instructions_do_not_reference_retired_schema(self):
        retired_schema = "29" + "6"
        paths = (
            ROOT / "docs" / "RELEASE-AND-RECOVERY.md",
            ROOT / "docs" / "SECRET-RECOVERY-EVIDENCE.md",
            ROOT / "scripts" / "recovery_key_canary.py",
        )
        for path in paths:
            with self.subTest(path=path.name):
                self.assertNotRegex(
                    path.read_text(encoding="utf-8"),
                    rf"(?<!\d){retired_schema}(?!\d)",
                )


if __name__ == "__main__":
    unittest.main()
