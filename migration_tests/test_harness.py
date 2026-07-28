import os
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
        self.assertEqual(ALEMBIC_HEAD, "0002_pilot_reliability")
        self.assertEqual(set(metadata.tables), set(SOURCE_COLUMNS))
        self.assertEqual(len(metadata.tables), 28)
        self.assertEqual(sum(len(table.indexes) for table in metadata.tables.values()), 34)
        self.assertEqual(len(EXPECTED_SOURCE_SCHEMA_SHA256), 64)
        self.assertEqual(EXPECTED_SOURCE_USER_VERSION, 293)

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
