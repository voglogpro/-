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
from db_migration.access_contract import CAPABILITIES_V1
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
                    + (SELECT COUNT(*) FROM task_template_versions v LEFT JOIN media_objects m
                       ON m.id=v.photo_media_id
                       WHERE v.photo_media_id IS NOT NULL AND m.id IS NULL)
                """,
                "task_template_current_version": """
                    SELECT COUNT(*) FROM task_templates t
                    LEFT JOIN task_template_versions v
                      ON v.template_id=t.id AND v.id=t.current_version_id
                    WHERE v.id IS NULL
                """,
                "task_template_version_sequence": """
                    SELECT COUNT(*) FROM (
                      SELECT template_id FROM task_template_versions
                      GROUP BY template_id
                      HAVING MIN(version_number)<>1
                         OR COUNT(*)<>MAX(version_number)
                         OR COUNT(DISTINCT version_number)<>COUNT(*)
                    ) q
                """,
                "task_template_generation": """
                    SELECT COUNT(*) FROM task_templates t
                    WHERE t.generation<>COALESCE((SELECT MAX(e.generation)
                            FROM task_template_events e WHERE e.template_id=t.id),0)
                       OR t.generation<>(SELECT COUNT(*) FROM task_template_events e
                            WHERE e.template_id=t.id)
                """,
                "task_template_media_integrity": """
                    SELECT COUNT(*) FROM task_template_versions v
                    JOIN media_objects m ON m.id=v.photo_media_id
                    WHERE v.photo_media_id IS NOT NULL AND (
                      m.state<>'ready' OR m.purpose<>'task_template_brief'
                      OR m.sha256<>v.photo_sha256
                    )
                """,
                "task_template_event_version": """
                    SELECT COUNT(*) FROM task_template_events e
                    LEFT JOIN task_template_versions v
                      ON v.template_id=e.template_id AND v.id=e.template_version_id
                    WHERE e.template_version_id IS NOT NULL AND v.id IS NULL
                """,
                "task_template_task_provenance": """
                    SELECT COUNT(*) FROM tasks t
                    LEFT JOIN task_template_versions v
                      ON v.template_id=t.template_id AND v.id=t.template_version_id
                    WHERE (t.template_id IS NULL)<>(t.template_version_id IS NULL)
                       OR (t.template_id IS NOT NULL AND v.id IS NULL)
                """,
                "task_template_system_seed": """
                    SELECT (SELECT COUNT(*) FROM (
                      VALUES
                        ('f679a68c-ef2a-561f-b191-96fc89b306e4','parking',
                         '455586eb-d473-5c43-b522-a3b093bfd5af',
                         '3a57569012e4a11f73067b4b524311c8517c4f78ce78e8852eacbcdbf929acc2',1,80),
                        ('2f6cee00-51f1-5cf2-9dda-487711836f2f','parking_photo',
                         '81cfc2f7-6106-5707-add0-4234710e85a0',
                         'cd85abc8d5a1dee6e80bac320abe939da8bdecb2e7627fd5972a2502279b870a',10,500),
                        ('e482a568-3a00-5e77-b668-ca19ba42fbaa','relocate',
                         '10ba0a01-7474-50d4-9cd8-06d027ca85af',
                         '56fc16f014d13187aab36bfd78662ab05f8820bce47e6ef02c57abc6547749cd',1,100),
                        ('cf1573ed-f6ce-58f5-a18f-d3bf239c4b9b','charge',
                         'cc48e39d-6578-5793-a383-879bc7913193',
                         'ada4d56af8db5c3ee83f8f49aeaefdae9736fd09dfc8eb01e3be7503b6897c8e',1,120)
                    ) AS expected(id,key,version_id,content_hash,max_participants,budget_cap)
                    LEFT JOIN task_templates t ON t.id=expected.id
                    LEFT JOIN task_template_versions v
                      ON v.template_id=t.id AND v.id=expected.version_id
                    WHERE t.id IS NULL OR t.key<>expected.key OR t.origin<>'system'
                       OR v.version_number<>1 OR v.content_hash<>expected.content_hash
                       OR v.max_participants<>expected.max_participants
                       OR v.budget_cap<>expected.budget_cap
                       OR v.evidence_policy<>'photo_required'
                    ) + CASE WHEN (
                      SELECT COUNT(*) FROM task_templates WHERE origin='system'
                    )<>4 THEN 1 ELSE 0 END
                """,
                "award_reversal_domain": """
                    SELECT COUNT(*) FROM award_reversals r
                    LEFT JOIN member_awards ma ON ma.id=r.member_award_id
                    LEFT JOIN members m ON m.user_id=r.user_id
                    LEFT JOIN awards a ON a.id=r.award_id
                    WHERE ma.id IS NULL OR m.user_id IS NULL OR a.id IS NULL
                       OR ma.user_id<>r.user_id OR ma.award_id<>r.award_id
                       OR ma.bonus<>r.amount
                """,
                "award_reversal_snapshot_provenance": """
                    SELECT COUNT(*) FROM award_reversals r
                    JOIN member_awards ma ON ma.id=r.member_award_id
                    JOIN awards a ON a.id=r.award_id
                    WHERE r.award_title<>a.title
                       OR r.original_grant_operation_id IS DISTINCT FROM ma.operation_id
                       OR (r.origin='maker_checker'
                           AND r.original_granted_by IS DISTINCT FROM ma.granted_by)
                       OR (r.origin<>'maker_checker'
                           AND r.original_granted_by IS DISTINCT FROM ma.granted_by
                           AND NOT (
                             r.original_granted_by IS NULL
                             AND ma.granted_by IS NOT NULL
                             AND NOT EXISTS (
                               SELECT 1 FROM members historical_granter
                               WHERE historical_granter.user_id=ma.granted_by
                             )
                           ))
                """,
                "award_reversal_grant_lineage": """
                    SELECT COUNT(*) FROM award_reversals r
                    LEFT JOIN bonus_ledger original ON original.id=r.original_ledger_id
                    WHERE r.origin='maker_checker' AND (
                      (r.amount=0 AND r.original_ledger_id IS NOT NULL)
                      OR (r.amount>0 AND (
                        r.original_grant_operation_id IS NULL
                        OR original.id IS NULL OR original.user_id<>r.user_id
                        OR original.amount<>r.amount
                        OR original.operation_id<>'award:' || r.original_grant_operation_id
                      ))
                    )
                """,
                "award_reversal_applied_ledger": """
                    SELECT COUNT(*) FROM award_reversals r
                    LEFT JOIN bonus_ledger reversal ON reversal.id=r.reversal_ledger_id
                    WHERE r.origin='maker_checker' AND r.status='applied' AND (
                      (r.amount=0 AND r.reversal_ledger_id IS NOT NULL)
                      OR (r.amount>0 AND (
                        reversal.id IS NULL OR reversal.user_id<>r.user_id
                        OR reversal.amount<>-r.amount
                        OR reversal.reversal_of_ledger_id IS DISTINCT FROM r.original_ledger_id
                        OR reversal.balance_after IS DISTINCT FROM r.result_balance
                      ))
                    )
                """,
                "award_reversal_legacy_lineage": """
                    SELECT COUNT(*) FROM award_reversals r
                    LEFT JOIN bonus_ledger original ON original.id=r.original_ledger_id
                    LEFT JOIN bonus_ledger reversal ON reversal.id=r.reversal_ledger_id
                    WHERE (r.origin='legacy_single_actor' AND r.amount>0 AND (
                             original.id IS NULL OR reversal.id IS NULL
                             OR original.user_id<>r.user_id OR original.amount<>r.amount
                             OR reversal.user_id<>r.user_id OR reversal.amount<>-r.amount
                             OR reversal.reversal_of_ledger_id IS DISTINCT FROM original.id
                           ))
                       OR (r.origin='legacy_unlinked' AND r.amount>0
                           AND original.id IS NOT NULL AND reversal.id IS NOT NULL
                           AND original.user_id=r.user_id AND original.amount=r.amount
                           AND reversal.user_id=r.user_id AND reversal.amount=-r.amount
                           AND reversal.reversal_of_ledger_id=original.id)
                """,
                "award_reversal_terminal_projection": """
                    SELECT COUNT(*) FROM award_reversals r
                    JOIN member_awards ma ON ma.id=r.member_award_id
                    WHERE (r.status='applied' AND (
                             ma.revoked_at IS NULL
                             OR (r.origin='maker_checker' AND (
                               ma.revoked_by IS DISTINCT FROM r.decided_by
                               OR ma.revoke_operation_id IS DISTINCT FROM r.decision_operation_id
                               OR ma.revoke_request_hash IS DISTINCT FROM r.decision_hash
                             ))
                           ))
                       OR (r.status<>'applied' AND ma.revoked_at IS NOT NULL
                           AND NOT EXISTS (
                             SELECT 1 FROM award_reversals applied
                             WHERE applied.member_award_id=r.member_award_id
                               AND applied.status='applied'
                           ))
                """,
                "award_reversal_event_projection": """
                    SELECT COUNT(*) FROM award_reversals r
                    WHERE NOT EXISTS (
                      SELECT 1 FROM award_reversal_events e WHERE e.reversal_id=r.id
                    ) OR COALESCE((
                      SELECT e.to_status FROM award_reversal_events e
                      WHERE e.reversal_id=r.id ORDER BY e.id DESC LIMIT 1
                    ),'')<>r.status
                """,
                "award_reversal_event_chain": """
                    WITH ordered AS (
                      SELECT e.*,
                             ROW_NUMBER() OVER (
                               PARTITION BY reversal_id ORDER BY id
                             ) AS ordinal,
                             LAG(to_status) OVER (
                               PARTITION BY reversal_id ORDER BY id
                             ) AS prior_status
                      FROM award_reversal_events e
                    )
                    SELECT COUNT(*) FROM ordered
                    WHERE (ordinal=1 AND from_status IS NOT NULL)
                       OR (ordinal>1 AND from_status IS DISTINCT FROM prior_status)
                """,
                "award_reversal_event_cardinality": """
                    SELECT COUNT(*) FROM award_reversals r
                    LEFT JOIN (
                      SELECT reversal_id,
                             COUNT(*) FILTER (WHERE event_type='requested') requested,
                             COUNT(*) FILTER (
                               WHERE event_type IN ('applied','rejected')
                             ) terminal
                      FROM award_reversal_events GROUP BY reversal_id
                    ) counts ON counts.reversal_id=r.id
                    WHERE r.origin='maker_checker' AND (
                      COALESCE(counts.requested,0)<>1
                      OR (r.status IN ('pending','manual_required')
                          AND COALESCE(counts.terminal,0)<>0)
                      OR (r.status IN ('applied','rejected')
                          AND COALESCE(counts.terminal,0)<>1)
                    )
                """,
                "award_reversal_event_actors": """
                    WITH ordered AS (
                      SELECT e.*,
                             LAG(event_type) OVER (
                               PARTITION BY reversal_id ORDER BY id
                             ) AS prior_event_type
                      FROM award_reversal_events e
                    )
                    SELECT COUNT(*) FROM ordered e
                    JOIN award_reversals r ON r.id=e.reversal_id
                    WHERE r.origin='maker_checker' AND (
                      (e.event_type='requested'
                       AND e.actor_id IS DISTINCT FROM r.requested_by)
                      OR (e.event_type='manual_required' AND (
                        e.operation_id IS NOT NULL
                        OR (e.actor_id IS NOT DISTINCT FROM r.requested_by
                            AND e.prior_event_type<>'requested')
                        OR (e.actor_id IS DISTINCT FROM r.requested_by AND (
                          e.actor_id IS NULL OR e.actor_id=r.user_id
                          OR (r.original_granted_by IS NOT NULL
                              AND e.actor_id=r.original_granted_by)
                        ))
                      ))
                      OR (e.event_type IN ('applied','rejected')
                          AND e.actor_id IS DISTINCT FROM r.decided_by)
                    )
                """,
                "award_reversal_event_operations": """
                    SELECT COUNT(*) FROM award_reversals r
                    WHERE (r.origin='maker_checker' AND NOT EXISTS (
                             SELECT 1 FROM award_reversal_events e
                             WHERE e.reversal_id=r.id AND e.event_type='requested'
                               AND e.operation_id=r.request_operation_id
                           ))
                       OR (r.origin='maker_checker' AND r.status IN ('applied','rejected')
                           AND NOT EXISTS (
                             SELECT 1 FROM award_reversal_events e
                             WHERE e.reversal_id=r.id AND e.event_type=r.status
                               AND e.operation_id=r.decision_operation_id
                           ))
                       OR (r.origin<>'maker_checker' AND NOT EXISTS (
                             SELECT 1 FROM award_reversal_events e
                             WHERE e.reversal_id=r.id AND e.event_type='legacy_imported'
                           ))
                """,
                "award_reversal_request_registry": """
                    SELECT COUNT(*) FROM award_reversals r
                    LEFT JOIN operation_registry o ON o.operation_id=r.request_operation_id
                    WHERE r.origin='maker_checker' AND (
                      o.operation_id IS NULL OR o.command_type<>'award_reversal_request'
                      OR o.request_hash<>r.request_hash OR o.actor_id<>r.requested_by
                    )
                """,
                "award_reversal_decision_registry": """
                    SELECT COUNT(*) FROM award_reversals r
                    LEFT JOIN operation_registry o ON o.operation_id=r.decision_operation_id
                    WHERE r.origin='maker_checker' AND r.status IN ('applied','rejected') AND (
                      o.operation_id IS NULL OR o.command_type<>'award_reversal_decision'
                      OR o.request_hash<>r.decision_hash OR o.actor_id<>r.decided_by
                    )
                """,
                "inbox_processing_lease": """
                    SELECT COUNT(*) FROM telegram_update_inbox
                    WHERE status='processing' AND (locked_by IS NULL OR locked_at IS NULL)
                """,
                "staff_capability_orphan": """
                    SELECT COUNT(*) FROM staff_grant_capabilities c
                    LEFT JOIN staff_access_grants g ON g.id=c.grant_id
                    WHERE g.id IS NULL
                """,
                "staff_owner_v1_snapshot": f"""
                    SELECT COUNT(*) FROM staff_access_grants g
                    WHERE g.preset='owner' AND g.policy_version=1
                      AND (SELECT COUNT(*) FROM staff_grant_capabilities c
                           WHERE c.grant_id=g.id) <> {len(CAPABILITIES_V1)}
                """,
                "admin_authority_access_projection": """
                    SELECT COUNT(*) FROM admin_authorities aa
                    JOIN members m ON m.user_id=aa.user_id AND m.status='approved'
                    WHERE NOT EXISTS (
                      SELECT 1 FROM staff_access_grants g
                      WHERE g.user_id=aa.user_id AND g.preset='owner'
                        AND g.origin=aa.origin AND g.status='active'
                        AND g.policy_version=1
                    )
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
                "award_reversals", "award_reversal_events",
                "staff_access_grants", "staff_access_changes",
                "staff_access_events",
                "task_template_events",
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
