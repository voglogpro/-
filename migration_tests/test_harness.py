import os
import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from db_migration.metadata import metadata
from db_migration import ALEMBIC_HEAD
from db_migration.access_contract import CAPABILITIES_V1
from db_migration.types import ConversionError, parse_bigint, parse_json
from scripts.migrate_sqlite_to_postgres import (
    iter_transformed_rows, validate_database_endpoint,
)
from scripts.pg_harness_common import (
    DataError, EXPECTED_SOURCE_SCHEMA_SHA256, EXPECTED_SOURCE_USER_VERSION,
    SOURCE_COLUMNS, SourceError,
    TargetError,
    open_source, validate_source_schema,
)


class MigrationHarnessTests(unittest.TestCase):
    def test_metadata_inventory_matches_source_contract(self):
        self.assertEqual(ALEMBIC_HEAD, "0007_capability_rbac")
        self.assertEqual(set(metadata.tables), set(SOURCE_COLUMNS))
        self.assertEqual(len(metadata.tables), 38)
        self.assertEqual(sum(len(table.indexes) for table in metadata.tables.values()), 47)
        self.assertEqual(len(EXPECTED_SOURCE_SCHEMA_SHA256), 64)
        self.assertEqual(EXPECTED_SOURCE_USER_VERSION, 298)

    def test_incremental_foreign_keys_match_canonical_deferrability(self):
        versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
        for filename in (
            "0003_admin_financial_controls.py",
            "0004_authority_operation_registry.py",
            "0005_manual_grant_reversals.py",
            "0006_join_request_admission.py",
            "0007_capability_rbac.py",
        ):
            ddl = (versions / filename).read_text(encoding="utf-8")
            self.assertGreater(ddl.count("FOREIGN KEY"), 0, filename)
            self.assertEqual(
                ddl.count("FOREIGN KEY"),
                ddl.count("DEFERRABLE INITIALLY DEFERRED"),
                f"{filename} must mirror db_migration.metadata fk() semantics",
            )

    def test_capability_v1_contract_is_complete_and_migration_is_forward_only(self):
        self.assertEqual(len(CAPABILITIES_V1), 38)
        self.assertEqual(len(set(CAPABILITIES_V1)), len(CAPABILITIES_V1))
        self.assertIn("task.template.manage", CAPABILITIES_V1)
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations" / "versions" / "0007_capability_rbac.py"
        ).read_text(encoding="utf-8")
        for capability in CAPABILITIES_V1:
            self.assertIn(capability, migration)
        self.assertIn("FROM admin_authorities", migration)
        self.assertIn("m.status='approved'", migration)
        self.assertNotIn("UPDATE members SET role", migration)
        self.assertIn("Destructive capability-RBAC downgrade is disabled", migration)

    def test_access_policy_json_must_be_canonical(self):
        table = metadata.tables["staff_access_events"]
        canonical = json.dumps(
            {"generation": 1, "preset": "owner"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        row = {
            "id": 1, "target_user_id": 10, "preset": "owner",
            "event_type": "assign", "actor_id": None,
            "operation_id": "access-event-test",
            "policy_version": 1, "before_json": "{}",
            "after_json": canonical, "created_at": "2026-07-28T10:00:00+00:00",
        }

        class SourceStub:
            def __init__(self, value):
                self.value = value

            def execute(self, _sql):
                return [{**row, "after_json": self.value}]

        converted = list(iter_transformed_rows(SourceStub(canonical), table.name))
        self.assertEqual(converted[0]["after_json"]["preset"], "owner")
        with self.assertRaisesRegex(DataError, "row 1"):
            list(iter_transformed_rows(SourceStub('{"preset": "owner", "generation": 1}'), table.name))

    def test_integer_and_json_conversion_are_lossless(self):
        self.assertEqual(parse_bigint(42), 42)
        for bad in (True, 1.0, 1.9, "1"):
            with self.assertRaises(ConversionError):
                parse_bigint(bad)
        for bad_json in ('{"value": NaN}', '{"value": Infinity}'):
            with self.assertRaises(ConversionError):
                parse_json(bad_json, shape="object")

    def test_dsn_query_cannot_override_guarded_endpoint(self):
        with self.assertRaisesRegex(TargetError, "must not override"):
            validate_database_endpoint(
                "postgresql+psycopg://u:p@expected:5432/db?host=evil&port=6543",
                "expected", 5432,
            )
        with self.assertRaisesRegex(TargetError, "exactly one"):
            validate_database_endpoint(
                "postgresql+psycopg://u:p@host1,host2:5432/db",
                "host1,host2", 5432,
            )

    def test_alembic_refuses_unguarded_dsn_before_connecting(self):
        environment = os.environ.copy()
        environment.update({
            "MIGRATION_DATABASE_URL": (
                "postgresql+psycopg://expected:secret@127.0.0.1:55432/"
                "bibitasks_migration?host=evil"
            ),
            "MIGRATION_EXPECTED_DATABASE": "bibitasks_migration",
            "MIGRATION_EXPECTED_SCHEMA": "public",
            "MIGRATION_EXPECTED_SERVER_ADDRESS": "127.0.0.1",
            "MIGRATION_EXPECTED_SERVER_PORT": "55432",
            "MIGRATION_EXPECTED_USER": "expected",
        })
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("identity/endpoint overrides", output)
        self.assertNotIn("secret@", output)

    def test_exact_source_fingerprint_rejects_index_drift(self):
        source = os.getenv("MIGRATION_SOURCE")
        if not source:
            self.skipTest("MIGRATION_SOURCE is provided by the PostgreSQL CI job")
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "candidate.db"
            shutil.copy2(source, candidate)
            with sqlite3.connect(candidate) as db:
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("DROP INDEX idx_tasks_status")
                db.execute("CREATE INDEX idx_tasks_status ON tasks(status, id)")
                db.commit()
            with closing(open_source(candidate)) as db:
                with self.assertRaises(SourceError):
                    validate_source_schema(db)

    def test_conversion_error_never_discloses_primary_key(self):
        source = os.getenv("MIGRATION_SOURCE")
        if not source:
            self.skipTest("MIGRATION_SOURCE is provided by the PostgreSQL CI job")
        secret_token = "sensitive-referral-token"
        with closing(open_source(Path(source))) as db:
            row = dict(db.execute("SELECT * FROM referral_tokens LIMIT 1").fetchone())
        row["token"] = secret_token + "\x00"

        class SourceStub:
            def execute(self, _sql):
                return [row]

        with self.assertRaises(DataError) as raised:
            list(iter_transformed_rows(SourceStub(), "referral_tokens"))
        self.assertNotIn(secret_token, str(raised.exception))
        self.assertIn("row 1", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
