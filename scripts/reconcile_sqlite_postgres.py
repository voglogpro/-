"""Independent, read-only post-import reconciliation report."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
from itertools import zip_longest
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from db_migration import ALEMBIC_HEAD
from db_migration.metadata import metadata
from scripts.migrate_sqlite_to_postgres import (
    _primary_key_order, validate_database_endpoint,
)
from scripts.pg_harness_common import (
    HarnessError, ReconcileError, SOURCE_COLUMNS, TABLE_ORDER,
    TargetError, file_sha256, open_source, validate_source_schema,
)


def _canonical(value, peer=None):
    if value is None:
        return None
    if isinstance(peer, bool):
        if type(value) is bool:
            return value
        if type(value) is int and value in (0, 1):
            return bool(value)
    if isinstance(peer, datetime):
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            raise ReconcileError("source contains a naive timestamp")
        return parsed.astimezone(timezone.utc).isoformat()
    if isinstance(peer, date) and not isinstance(peer, datetime):
        parsed = value if isinstance(value, date) else date.fromisoformat(str(value))
        return parsed.isoformat()
    if isinstance(peer, (dict, list)):
        parsed = json.loads(value) if isinstance(value, str) else value
        return parsed
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _digest_rows(source_rows, target_rows, columns):
    source_hash = hashlib.sha256()
    target_hash = hashlib.sha256()
    source_count = 0
    target_count = 0
    missing = object()
    for source_row, target_row in zip_longest(
        source_rows, target_rows, fillvalue=missing,
    ):
        if source_row is not missing:
            source_count += 1
            source_value = {
                name: _canonical(
                    source_row[name],
                    None if target_row is missing else target_row[name],
                )
                for name in columns
            }
            source_hash.update(json.dumps(
                source_value, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str,
            ).encode("utf-8"))
            source_hash.update(b"\n")
        if target_row is not missing:
            target_count += 1
            target_value = {name: _canonical(target_row[name]) for name in columns}
            target_hash.update(json.dumps(
                target_value, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str,
            ).encode("utf-8"))
            target_hash.update(b"\n")
    return (
        source_count, target_count,
        source_hash.hexdigest(), target_hash.hexdigest(),
    )


def reconcile(
    source_path: Path, database_url: str, expected_database: str,
    expected_schema: str, expected_server_address: str,
    expected_server_port: int, expected_user: str,
):
    if source_path.is_symlink():
        raise ReconcileError("source symbolic links are not accepted")
    source_path = source_path.resolve()
    source_hash = file_sha256(source_path)
    validate_database_endpoint(
        database_url, expected_server_address, expected_server_port,
    )
    engine = create_engine(
        database_url, future=True, pool_pre_ping=True, hide_parameters=True,
    )
    table_results = {}
    invariant_failures = []
    target_identity = {}
    try:
        try:
            with closing(open_source(source_path)) as source, engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as target, target.begin():
                target.execute(text("SET TRANSACTION READ ONLY"))
                target.execute(text("SELECT pg_advisory_xact_lock_shared(4242428675309)"))
                source_counts = validate_source_schema(source)
                identity = target.execute(text(
                    "SELECT current_database(),current_schema(),current_user,"
                    "COALESCE(inet_server_addr()::text,''),inet_server_port(),"
                    "current_setting('server_version')"
                )).one()
                if identity[0] != expected_database or identity[1] != expected_schema:
                    raise TargetError("target database/schema identity mismatch")
                if identity[2] != expected_user:
                    raise TargetError("target role identity mismatch")
                target_identity = {
                    "database": identity[0], "schema": identity[1], "user": identity[2],
                    "endpoint_address": expected_server_address,
                    "endpoint_port": int(expected_server_port),
                    "backend_address": identity[3], "backend_port": identity[4],
                    "server_version": identity[5],
                }
                if target.execute(text("SELECT version_num FROM alembic_version")).scalar_one() != ALEMBIC_HEAD:
                    raise TargetError("target is not at expected Alembic head")
                if set(inspect(target).get_table_names(schema=expected_schema)) != set(TABLE_ORDER) | {"alembic_version"}:
                    raise TargetError("target schema manifest mismatch")
                migration_context = MigrationContext.configure(target, opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_object": lambda obj, name, type_, reflected, compare_to: (
                        name != "alembic_version"
                    ),
                })
                if compare_metadata(migration_context, metadata):
                    raise TargetError("target schema objects differ from canonical metadata")

                for table_name in TABLE_ORDER:
                    columns = SOURCE_COLUMNS[table_name]
                    table = metadata.tables[table_name]
                    pk_columns = list(table.primary_key.columns)
                    source_order = _primary_key_order(pk_columns, source=True)
                    target_order = _primary_key_order(pk_columns, source=False)
                    source_rows = source.execute(
                        f'SELECT * FROM "{table_name}" ORDER BY {source_order}'
                    )
                    target_rows = target.execute(
                        text(
                            f'SELECT * FROM "{table_name}" ORDER BY {target_order}'
                        ).execution_options(
                            stream_results=True, max_row_buffer=500,
                        )
                    ).mappings()
                    (
                        source_count, target_count,
                        source_digest, target_digest,
                    ) = _digest_rows(
                        source_rows, target_rows, columns,
                    )
                    passed = (
                        source_count == target_count == source_counts[table_name]
                        and source_digest == target_digest
                    )
                    table_results[table_name] = {
                        "source_count": source_counts[table_name],
                        "target_count": target_count,
                        "source_digest": source_digest,
                        "target_digest": target_digest,
                        "passed": passed,
                    }
                    if not passed:
                        invariant_failures.append(f"row parity: {table_name}")

                checks = {
                "assignment_task_orphan": """
                    SELECT COUNT(*) FROM task_assignments a
                    LEFT JOIN tasks t ON t.id=a.task_id WHERE t.id IS NULL
                """,
                "assignment_member_orphan": """
                    SELECT COUNT(*) FROM task_assignments a
                    LEFT JOIN members m ON m.user_id=a.user_id WHERE m.user_id IS NULL
                """,
                "done_assignment_reward": """
                    SELECT COUNT(*) FROM task_assignments a
                    WHERE a.status='done' AND (
                      SELECT COUNT(*) FROM bonus_ledger l
                      WHERE l.assignment_id=a.id AND l.amount=a.reward_snapshot
                    ) <> 1
                """,
                "pending_withdrawal_unique": """
                    SELECT COUNT(*) FROM (
                      SELECT user_id FROM withdrawal_requests
                      WHERE status IN ('pending','processing') GROUP BY user_id HAVING COUNT(*)>1
                    ) q
                """,
                "withdrawal_ledger": """
                    SELECT COUNT(*) FROM withdrawal_requests w
                    LEFT JOIN LATERAL (
                      SELECT COUNT(*) FILTER (WHERE amount=-w.amount) debits,
                             COUNT(*) FILTER (WHERE amount=w.amount) refunds,
                             COALESCE(SUM(amount),0) net
                      FROM bonus_ledger l WHERE l.withdrawal_id=w.id
                    ) x ON true
                    WHERE (w.status IN ('pending','processing','completed')
                           AND (x.debits<>1 OR x.refunds<>0 OR x.net<>-w.amount))
                       OR (w.status='rejected_refunded'
                           AND (x.debits<>1 OR x.refunds<>1 OR x.net<>0))
                """,
                "withdrawal_latest_event": """
                    SELECT COUNT(*) FROM withdrawal_requests w
                    WHERE COALESCE((SELECT e.to_status FROM withdrawal_events e
                                    WHERE e.withdrawal_id=w.id ORDER BY e.id DESC LIMIT 1),'')
                          <> w.status
                """,
                "withdrawal_external_reference_unique": """
                    SELECT COUNT(*) FROM (
                      SELECT provider,external_reference_canonical
                      FROM withdrawal_requests WHERE status='completed'
                        AND external_reference_canonical IS NOT NULL
                      GROUP BY provider,external_reference_canonical HAVING COUNT(*)>1
                    ) q
                """,
                "media_reference_orphan": """
                    SELECT
                      (SELECT COUNT(*) FROM tasks t LEFT JOIN media_objects m
                       ON m.id=t.photo_media_id WHERE t.photo_media_id IS NOT NULL AND m.id IS NULL)
                    + (SELECT COUNT(*) FROM task_evidence e LEFT JOIN media_objects m
                       ON m.id=e.media_id WHERE e.media_id IS NOT NULL AND m.id IS NULL)
                    + (SELECT COUNT(*) FROM task_outbox o LEFT JOIN media_objects m
                       ON m.id=o.media_id WHERE o.media_id IS NOT NULL AND m.id IS NULL)
                """,
                "inbox_processing_lease": """
                    SELECT COUNT(*) FROM telegram_update_inbox
                    WHERE status='processing' AND (locked_by IS NULL OR locked_at IS NULL)
                """,
                }
                invariant_results = {}
                for name, sql in checks.items():
                    failures = int(target.execute(text(sql)).scalar_one())
                    invariant_results[name] = {"failures": failures, "passed": failures == 0}
                    if failures:
                        invariant_failures.append(name)

                opening_baselines = 0
                ledger_failures = 0

                def finish_ledger(final_bonus, cumulative, offset, mismatch):
                    nonlocal opening_baselines, ledger_failures
                    if mismatch:
                        ledger_failures += 1
                        return
                    opening = (
                        int(final_bonus) - cumulative if offset is None else offset
                    )
                    if opening + cumulative != int(final_bonus):
                        ledger_failures += 1
                    if opening:
                        opening_baselines += 1

                ledger_rows = target.execute(text(
                    "SELECT m.user_id,m.bonus,l.amount,l.balance_after "
                    "FROM members m LEFT JOIN bonus_ledger l ON l.user_id=m.user_id "
                    "ORDER BY m.user_id,l.created_at,l.id"
                ).execution_options(
                    stream_results=True, max_row_buffer=500,
                ))
                active_user = None
                active_bonus = 0
                cumulative = 0
                ledger_offset = None
                offset_mismatch = False
                for user_id, final_bonus, amount, balance_after in ledger_rows:
                    if active_user is not None and user_id != active_user:
                        finish_ledger(
                            active_bonus, cumulative, ledger_offset, offset_mismatch,
                        )
                        cumulative = 0
                        ledger_offset = None
                        offset_mismatch = False
                    active_user = user_id
                    active_bonus = final_bonus
                    if amount is None:
                        continue
                    cumulative += int(amount)
                    if balance_after is not None:
                        current_offset = int(balance_after) - cumulative
                        if ledger_offset is None:
                            ledger_offset = current_offset
                        elif current_offset != ledger_offset:
                            offset_mismatch = True
                if active_user is not None:
                    finish_ledger(
                        active_bonus, cumulative, ledger_offset, offset_mismatch,
                    )
                invariant_results["ledger_chain_with_opening_balance"] = {
                    "failures": ledger_failures, "opening_baselines": opening_baselines,
                    "passed": ledger_failures == 0,
                }
                if ledger_failures:
                    invariant_failures.append("ledger_chain_with_opening_balance")

                sequence_results = {}
                for table_name in (
                "awards", "tasks", "task_assignments", "task_evidence",
                "withdrawal_requests", "withdrawal_events", "bonus_ledger",
                "member_awards", "task_outbox", "product_events",
                "admin_role_changes",
                ):
                    sequence_name = target.execute(text(
                    "SELECT pg_get_serial_sequence(:table_name,'id')"
                ), {"table_name": table_name}).scalar_one()
                    quoted_sequence = ".".join(
                    '"' + part.replace('"', '""') + '"'
                    for part in str(sequence_name).split(".")
                )
                    state = target.execute(text(
                    f"SELECT last_value,is_called FROM {quoted_sequence}"
                )).one()
                    maximum = int(target.execute(text(
                    f'SELECT COALESCE(MAX(id),0) FROM "{table_name}"'
                )).scalar_one())
                    safe = int(state[0]) > maximum or (int(state[0]) == maximum and not state[1])
                    sequence_results[table_name] = {"safe": safe}
                    if not safe:
                        invariant_failures.append(f"identity sequence: {table_name}")
        except SQLAlchemyError as exc:
            raise TargetError(
                f"target reconciliation failed safely: {type(exc).__name__}"
            ) from None
    finally:
        engine.dispose()
    if file_sha256(source_path) != source_hash:
        raise ReconcileError("source changed during reconciliation")
    return {
        "passed": not invariant_failures,
        "source_sha256": source_hash,
        "target": target_identity,
        "alembic_head": ALEMBIC_HEAD,
        "tables": table_results,
        "invariants": invariant_results,
        "identity_sequences": sequence_results,
        "failures": invariant_failures,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--expected-schema", default="public")
    parser.add_argument("--expected-server-address", required=True)
    parser.add_argument("--expected-server-port", type=int, required=True)
    parser.add_argument("--expected-user", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = reconcile(
            args.source, args.database_url, args.expected_database,
            args.expected_schema, args.expected_server_address,
            args.expected_server_port, args.expected_user,
        )
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.report:
            args.report.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        if not report["passed"]:
            raise SystemExit(5)
    except HarnessError as exc:
        print(f"reconciliation refused: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code)
    except Exception as exc:
        print(
            f"reconciliation refused safely: {type(exc).__name__}",
            file=sys.stderr,
        )
        raise SystemExit(5) from None


if __name__ == "__main__":
    main()
