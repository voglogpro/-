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
import hashlib
import uuid

from db_migration.metadata import metadata
from db_migration import ALEMBIC_HEAD
from db_migration.access_contract import CAPABILITIES_V1
from db_migration.template_contract import SYSTEM_TEMPLATE_SEEDS
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
        self.assertEqual(ALEMBIC_HEAD, "0008_task_template_versioning")
        self.assertEqual(set(metadata.tables), set(SOURCE_COLUMNS))
        self.assertEqual(len(metadata.tables), 41)
        self.assertEqual(sum(len(table.indexes) for table in metadata.tables.values()), 51)
        self.assertEqual(len(EXPECTED_SOURCE_SCHEMA_SHA256), 64)
        self.assertEqual(EXPECTED_SOURCE_USER_VERSION, 299)

    def test_incremental_foreign_keys_match_canonical_deferrability(self):
        versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
        for filename in (
            "0003_admin_financial_controls.py",
            "0004_authority_operation_registry.py",
            "0005_manual_grant_reversals.py",
            "0006_join_request_admission.py",
            "0007_capability_rbac.py",
            "0008_task_template_versioning.py",
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

    def test_task_template_seed_hashes_and_ids_are_deterministic(self):
        self.assertEqual(len(SYSTEM_TEMPLATE_SEEDS), 4)
        self.assertEqual(
            {seed["key"] for seed in SYSTEM_TEMPLATE_SEEDS},
            {"parking", "parking_photo", "relocate", "charge"},
        )
        for seed in SYSTEM_TEMPLATE_SEEDS:
            self.assertEqual(str(uuid.UUID(seed["id"])), seed["id"])
            self.assertEqual(str(uuid.UUID(seed["version_id"])), seed["version_id"])
            content = {
                name: seed[name]
                for name in (
                    "title", "task_type", "task_title", "details", "reward",
                    "mode", "evidence_policy", "max_participants", "budget_cap",
                )
            }
            content.update({"photo_media_id": None, "photo_sha256": None})
            digest = hashlib.sha256(json.dumps(
                content, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            self.assertEqual(digest, seed["content_hash"])

    def test_template_migration_is_immutable_and_forward_only(self):
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations" / "versions" / "0008_task_template_versioning.py"
        ).read_text(encoding="utf-8")
        self.assertIn("reject_task_template_version_mutation", migration)
        self.assertIn("reject_task_template_event_mutation", migration)
        self.assertIn("reject_task_template_key_mutation", migration)
        self.assertIn("fk_tasks_template_version", migration)
        self.assertIn("Destructive task-template downgrade is disabled", migration)

    def test_template_key_contract_is_identical_across_runtime_and_postgres(self):
        root = Path(__file__).resolve().parents[1]
        runtime = (root / "main.py").read_text(encoding="utf-8")
        importer = (root / "scripts/migrate_sqlite_to_postgres.py").read_text(
            encoding="utf-8",
        )
        migration = (
            root / "migrations/versions/0008_task_template_versioning.py"
        ).read_text(encoding="utf-8")
        metadata_source = (root / "db_migration/metadata.py").read_text(
            encoding="utf-8",
        )
        canonical = "[a-z][a-z0-9_]{2,49}"
        self.assertIn(canonical, runtime)
        self.assertIn(canonical, importer)
        self.assertIn(canonical, migration)
        self.assertIn(canonical, metadata_source)

    def test_template_event_json_must_be_canonical(self):
        table = metadata.tables["task_template_events"]
        canonical = json.dumps(
            {"generation": 1, "ok": True}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        row = {
            "id": 1,
            "template_id": "80c3f0b4-44fd-4df6-866e-d9872e7aa874",
            "template_version_id": "d05974dc-2379-4ca5-b049-e862f5938f40",
            "event_type": "created", "generation": 1, "actor_id": None,
            "operation_id": "template-event-test", "request_hash": "a" * 64,
            "note": "", "before_json": "{}", "after_json": canonical,
            "result_json": canonical, "created_at": "2026-07-28T10:00:00+00:00",
        }

        class SourceStub:
            def __init__(self, value):
                self.value = value

            def execute(self, _sql):
                return [{**row, "after_json": self.value}]

        converted = list(iter_transformed_rows(SourceStub(canonical), table.name))
        self.assertTrue(converted[0]["after_json"]["ok"])
        with self.assertRaisesRegex(DataError, "row 1"):
            list(iter_transformed_rows(
                SourceStub('{"ok": true, "generation": 1}'), table.name,
            ))

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
