"""Strict offline SQLite→PostgreSQL rehearsal importer.

This script is intentionally not wired into main.py. It accepts only an exact
current-schema backup and a fresh Alembic-head PostgreSQL database.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Integer, JSON, String, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import create_engine

from db_migration import ALEMBIC_HEAD
from db_migration.access_contract import CAPABILITIES_V1
from db_migration.metadata import metadata
from db_migration.types import (
    ConversionError, parse_bigint, parse_bool01, parse_date, parse_json, parse_utc,
)
from scripts.pg_harness_common import (
    DataError, HarnessError, IDENTITY_TABLES, SOURCE_COLUMNS, SourceError,
    TABLE_ORDER, TargetError, file_sha256, open_source, validate_source_schema,
)


JSON_SHAPES = {
    ("withdrawal_events", "metadata_json"): "object",
    ("task_outbox", "payload_json"): "object",
    ("product_events", "properties_json"): "object",
    ("published_posts", "message_ids"): "array",
    ("staff_access_changes", "result_json"): "object",
    ("staff_access_events", "before_json"): "object",
    ("staff_access_events", "after_json"): "object",
}
CANONICAL_JSON_COLUMNS = {
    ("staff_access_changes", "result_json"),
    ("staff_access_events", "before_json"),
    ("staff_access_events", "after_json"),
}
ALLOWED = {
    ("members", "role"): {"candidate", "applicant", "helper", "employee", "admin"},
    ("members", "status"): {"pending", "approved", "blocked"},
    ("tasks", "status"): {"open", "closed", "cancelled", "expired"},
    ("task_assignments", "status"): {
        "claimed", "review", "done", "rejected", "released", "expired", "cancelled",
    },
    ("withdrawal_requests", "status"): {
        "pending", "processing", "completed", "rejected_refunded",
    },
    ("task_outbox", "status"): {"pending", "sending", "sent", "dead"},
    ("telegram_update_inbox", "status"): {"pending", "processing", "done", "dead"},
    ("media_objects", "state"): {
        "uploading", "ready", "delete_pending", "deleted", "quarantined",
    },
    ("publication_jobs", "status"): {
        "pending", "sending", "done", "cleanup_pending", "cleanup_failed",
        "failed", "failed_cleanup_pending",
    },
    ("publication_cleanup_messages", "status"): {"pending", "deleted", "failed"},
    ("staff_access_grants", "preset"): {"scout", "reviewer", "cashier", "owner"},
    ("staff_access_grants", "origin"): {"env", "manual"},
    ("staff_access_grants", "status"): {"active", "revoked"},
    ("staff_grant_capabilities", "capability"): set(CAPABILITIES_V1),
    ("staff_access_changes", "change_action"): {"assign", "revoke"},
    ("staff_access_changes", "preset"): {"scout", "reviewer", "cashier", "owner"},
    ("staff_access_changes", "status"): {"pending", "applied", "rejected"},
    ("staff_access_events", "preset"): {"scout", "reviewer", "cashier", "owner"},
    ("staff_access_events", "event_type"): {"assign", "revoke", "env_sync"},
}
FORBIDDEN_DSN_QUERY_KEYS = {"host", "hostaddr", "port", "service", "dbname", "user"}


def validate_database_endpoint(
    database_url, expected_server_address, expected_server_port,
):
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql") or not url.database:
        raise TargetError("database URL must name an explicit PostgreSQL database")
    overrides = FORBIDDEN_DSN_QUERY_KEYS.intersection(url.query)
    if overrides:
        raise TargetError("database URL must not override endpoint or identity in query")
    if not url.host or "," in url.host:
        raise TargetError("database URL must name exactly one endpoint host")
    endpoint_port = int(url.port or 5432)
    if url.host != expected_server_address:
        raise TargetError("database URL host does not match --expected-server-address")
    if endpoint_port != int(expected_server_port):
        raise TargetError("database URL port does not match --expected-server-port")
    return url


def _convert(table_name, column, value):
    nullable = bool(column.nullable)
    if isinstance(column.type, Boolean):
        return parse_bool01(value, nullable=nullable)
    if isinstance(column.type, DateTime):
        return parse_utc(value, nullable=nullable)
    if isinstance(column.type, Date):
        return parse_date(value, nullable=nullable)
    if isinstance(column.type, JSON):
        return parse_json(
            value, shape=JSON_SHAPES.get((table_name, column.name)), nullable=nullable,
        )
    if isinstance(column.type, (BigInteger, Integer)):
        return parse_bigint(value, nullable=nullable)
    if isinstance(column.type, Float):
        if value is None and nullable:
            return None
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ConversionError("expected finite floating point value") from exc
        if not math.isfinite(result):
            raise ConversionError("floating point value must be finite")
        return result
    if value is None:
        if nullable:
            return None
        raise ConversionError("required value is null")
    if not isinstance(value, str):
        raise ConversionError("expected text")
    if "\x00" in value:
        raise ConversionError("text contains PostgreSQL NUL byte")
    return value


def iter_transformed_rows(source, table_name):
    table = metadata.tables[table_name]
    for row_number, source_row in enumerate(
        source.execute(f'SELECT * FROM "{table_name}"'), start=1,
    ):
        try:
            converted = {
                column.name: _convert(table_name, column, source_row[column.name])
                for column in table.columns
            }
            for column in table.columns:
                key = (table_name, column.name)
                if key not in CANONICAL_JSON_COLUMNS or source_row[column.name] is None:
                    continue
                canonical = json.dumps(
                    converted[column.name], ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )
                if source_row[column.name] != canonical:
                    raise ConversionError("access policy JSON is not canonical")
            for column in table.columns:
                allowed = ALLOWED.get((table_name, column.name))
                if allowed and converted[column.name] not in allowed:
                    raise ConversionError("unknown controlled vocabulary value")
            if table_name == "telegram_update_inbox" and converted["payload_json"]:
                if not str(converted["payload_sha256"]).startswith("h1:"):
                    raise ConversionError("encrypted inbox fingerprint must use h1")
            if table_name == "media_objects":
                digest = str(converted["sha256"] or "")
                if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                    raise ConversionError("invalid media SHA-256")
            if table_name == "tasks":
                if converted["lat"] is not None and not -90 <= converted["lat"] <= 90:
                    raise ConversionError("latitude outside range")
                if converted["lng"] is not None and not -180 <= converted["lng"] <= 180:
                    raise ConversionError("longitude outside range")
        except ConversionError as exc:
            raise DataError(
                f"invalid source value in {table_name} at row {row_number}: {exc}"
            ) from None
        yield converted


def transform_rows(source, table_name):
    return list(iter_transformed_rows(source, table_name))


def _primary_key_digest(rows, columns, *, source_values=False):
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        values = []
        for index, column in enumerate(columns):
            value = row[column.name] if hasattr(row, "keys") else row[index]
            if source_values:
                value = _convert(column.table.name, column, value)
            if isinstance(value, (datetime,)):
                value = value.astimezone(timezone.utc).isoformat()
            values.append(value)
        digest.update(json.dumps(
            values, ensure_ascii=False, separators=(",", ":"), default=str,
        ).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _primary_key_order(columns, *, source):
    expressions = []
    for column in columns:
        quoted = f'"{column.name}"'
        if isinstance(column.type, String):
            expressions.append(
                f"CAST({quoted} AS BLOB)" if source
                else f"convert_to({quoted},'UTF8')"
            )
        else:
            expressions.append(quoted)
    return ",".join(expressions)


def _target_guard(
    connection, expected_database, expected_schema,
    expected_server_address, expected_server_port, expected_user,
):
    if connection.dialect.name != "postgresql":
        raise TargetError("target must be PostgreSQL")
    identity = connection.execute(text(
        "SELECT current_database(),current_schema(),current_user,"
        "COALESCE(inet_server_addr()::text,''),inet_server_port(),"
        "current_setting('server_version')"
    )).one()
    actual_database, actual_schema, actual_user, server_address, server_port, server_version = identity
    if actual_database != expected_database or actual_database in ("postgres", "template0", "template1"):
        raise TargetError("target database identity does not match --expected-database")
    if actual_schema != expected_schema:
        raise TargetError("target schema does not match --expected-schema")
    if actual_user != expected_user:
        raise TargetError("target role does not match --expected-user")
    connection.execute(text("SELECT pg_advisory_xact_lock(4242428675309)"))
    db_inspector = inspect(connection)
    tables = set(db_inspector.get_table_names(schema=expected_schema))
    expected_tables = set(metadata.tables) | {"alembic_version"}
    if tables != expected_tables:
        raise TargetError("target schema is not the exact Alembic baseline")
    revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    if revision != ALEMBIC_HEAD:
        raise TargetError("target Alembic revision is not the expected head")
    migration_context = MigrationContext.configure(connection, opts={
        "compare_type": True,
        "compare_server_default": True,
        "include_object": lambda obj, name, type_, reflected, compare_to: (
            name != "alembic_version"
        ),
    })
    if compare_metadata(migration_context, metadata):
        raise TargetError("target schema objects differ from canonical metadata")
    for table_name in TABLE_ORDER:
        count = connection.execute(
            text(f'SELECT COUNT(*) FROM "{table_name}"')
        ).scalar_one()
        if count:
            raise TargetError("target application schema must be empty")
    return {
        "database": actual_database, "schema": actual_schema,
        "user": actual_user,
        "endpoint_address": expected_server_address,
        "endpoint_port": int(expected_server_port),
        "backend_address": server_address, "backend_port": server_port,
        "server_version": server_version,
    }


def run(
    source_path: Path, *, database_url=None, expected_database=None,
    expected_schema="public", expected_server_address=None,
    expected_server_port=None, expected_user=None, apply=False,
):
    if source_path.is_symlink():
        raise SourceError("source symbolic links are not accepted")
    source_path = source_path.resolve()
    before_hash = file_sha256(source_path)
    with closing(open_source(source_path)) as source:
        counts = validate_source_schema(source)
        for table_name in TABLE_ORDER:
            for _row in iter_transformed_rows(source, table_name):
                pass
    if file_sha256(source_path) != before_hash:
        raise SourceError("source changed during preflight")
    report = {
        "mode": "apply" if apply else "dry-run",
        "source_sha256": before_hash,
        "source_counts": counts,
        "alembic_head": ALEMBIC_HEAD,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if not apply:
        return report
    if not all((
        database_url, expected_database, expected_schema,
        expected_server_address, expected_server_port, expected_user,
    )):
        raise TargetError("all explicit target identity arguments are required for apply")
    validate_database_endpoint(
        database_url, expected_server_address, expected_server_port,
    )
    engine = create_engine(
        database_url, future=True, pool_pre_ping=True, hide_parameters=True,
    )
    try:
        try:
            with closing(open_source(source_path)) as source, engine.begin() as target:
                identity = _target_guard(
                    target, expected_database, expected_schema,
                    expected_server_address, expected_server_port, expected_user,
                )
                for table_name in TABLE_ORDER:
                    table = metadata.tables[table_name]
                    batch = []
                    for row in iter_transformed_rows(source, table_name):
                        batch.append(row)
                        if len(batch) == 500:
                            target.execute(table.insert(), batch)
                            batch.clear()
                    if batch:
                        target.execute(table.insert(), batch)
                    target_count = int(target.execute(text(
                        f'SELECT COUNT(*) FROM "{table_name}"'
                    )).scalar_one())
                    pk_names = [column.name for column in table.primary_key]
                    pk_columns = list(table.primary_key.columns)
                    selected_pk = ",".join(f'"{name}"' for name in pk_names)
                    source_order = _primary_key_order(pk_columns, source=True)
                    target_order = _primary_key_order(pk_columns, source=False)
                    source_pk_count, source_pk_digest = _primary_key_digest(
                        source.execute(
                            f'SELECT {selected_pk} FROM "{table_name}" '
                            f'ORDER BY {source_order}'
                        ),
                        pk_columns, source_values=True,
                    )
                    target_pk_count, target_pk_digest = _primary_key_digest(
                        target.execute(
                            text(
                                f'SELECT {selected_pk} FROM "{table_name}" '
                                f'ORDER BY {target_order}'
                            )
                            .execution_options(stream_results=True, max_row_buffer=500)
                        ),
                        pk_columns,
                    )
                    if (
                        target_count != counts[table_name]
                        or source_pk_count != target_pk_count
                        or source_pk_digest != target_pk_digest
                    ):
                        raise DataError(f"in-transaction row/PK parity failed: {table_name}")
                for table_name in sorted(IDENTITY_TABLES):
                    target.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{table_name}','id'), "
                        f"COALESCE((SELECT MAX(id) FROM \"{table_name}\"),0)+1, false)"
                    ))
                if file_sha256(source_path) != before_hash:
                    raise SourceError("source changed during import")
        except SQLAlchemyError as exc:
            raise TargetError(
                f"target operation failed safely: {type(exc).__name__}"
            ) from None
    finally:
        engine.dispose()
    report["target"] = identity
    report["transaction"] = "committed"
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--database-url")
    parser.add_argument("--expected-database")
    parser.add_argument("--expected-schema", default="public")
    parser.add_argument("--expected-server-address")
    parser.add_argument("--expected-server-port", type=int)
    parser.add_argument("--expected-user")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.apply and not args.writers_stopped:
            raise SourceError("--writers-stopped acknowledgement is required for apply")
        report = run(
            args.source, database_url=args.database_url,
            expected_database=args.expected_database,
            expected_schema=args.expected_schema,
            expected_server_address=args.expected_server_address,
            expected_server_port=args.expected_server_port,
            expected_user=args.expected_user, apply=args.apply,
        )
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.report:
            args.report.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
    except HarnessError as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code)
    except Exception as exc:
        print(f"migration refused safely: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(4) from None


if __name__ == "__main__":
    main()
