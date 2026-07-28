"""Shared guards and source manifest for offline SQLite→PostgreSQL rehearsal."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path


SOURCE_COLUMNS = {
    "members": "user_id full_name username phone city help_type transport availability about tags application_note role status bonus done_count referred_by created_at approved_at approved_by applied_at city_change_requested city_change_requested_at chat_xp ref_confirmed group_membership_status group_joined_at group_left_at".split(),
    "awards": "id code emoji title description bonus repeatable active created_by created_at".split(),
    "media_objects": "id backend object_key purpose state content_type size_bytes sha256 upload_operation_id request_hash created_at ready_at delete_after deleted_at last_error reconcile_attempts version_id checked_at".split(),
    "task_templates": "id key origin status generation current_version_id created_by created_at updated_by updated_at archived_by archived_at".split(),
    "task_template_versions": "id template_id version_number title task_type task_title details reward mode evidence_policy max_participants budget_cap photo_media_id photo_sha256 content_hash created_by created_at".split(),
    "task_template_events": "id template_id template_version_id event_type generation actor_id operation_id request_hash note before_json after_json result_json created_at".split(),
    "analytics_subjects": "subject_id user_id created_at".split(),
    "tasks": "id type title details lat lng address city reward status created_by created_at claimed_by claimed_at done_at proof_note review_note assigned_to slot_start slot_end repeatable photo_file photo_media_id operation_id request_hash completion_operation_id completion_request_hash submission_attempt evidence_policy max_participants budget_cap cancel_operation_id cancel_request_hash cancelled_at cancelled_by cancel_reason expired_at version template_id template_version_id".split(),
    "referral_rewards": "referee_id referrer_id amount created_at".split(),
    "referral_tokens": "token referrer_id created_at expires_at".split(),
    "referral_milestone_rewards": "user_id threshold amount created_at".split(),
    "chat_activity": "user_id last_msg_at day msg_xp_today thanks_xp_today messages_total thanks_total".split(),
    "thanks_pairs": "from_id to_id last_at".split(),
    "task_assignments": "id task_id user_id status claimed_at done_at proof_note review_note completion_operation_id completion_request_hash submission_attempt reward_snapshot due_at revision_due_at release_operation_id release_request_hash released_at release_reason terminal_at terminal_by terminal_reason decision_operation_id decision_request_hash version".split(),
    "task_evidence": "id assignment_id task_id user_id kind photo_file media_id sha256 submission_operation_id attempt is_current created_at".split(),
    "withdrawal_requests": "id user_id amount status created_at decided_by decided_at note operation_id request_hash account_type account_ciphertext account_masked account_fingerprint key_version decision_operation_id decision_request_hash provider external_reference external_reference_canonical reject_reason account_purged_at processing_by processing_at".split(),
    "withdrawal_events": "id withdrawal_id event_type from_status to_status actor_id operation_id created_at metadata_json".split(),
    "bonus_ledger": "id user_id amount reason task_id assignment_id withdrawal_id created_by created_at operation_id balance_after reversal_of_ledger_id".split(),
    "manual_grant_commands": "operation_id request_hash user_id amount reason maker_id created_at ledger_id result_balance".split(),
    "manual_grant_reversals": "id grant_operation_id original_ledger_id user_id amount reason status manual_reason requested_by requested_at request_operation_id request_hash decided_by decided_at decision_note decision_operation_id decision_hash reversal_ledger_id result_balance".split(),
    "admin_role_changes": "id user_id from_role to_role reason status requested_by requested_at request_operation_id request_hash decided_by decided_at decision_note decision_operation_id decision_hash".split(),
    "operation_registry": "operation_id command_type request_hash actor_id created_at".split(),
    "admin_authorities": "user_id origin granted_operation_id granted_at".split(),
    "staff_access_grants": "id user_id preset origin status policy_version generation granted_by approved_by grant_operation_id granted_at revoked_by revoke_operation_id revoked_at".split(),
    "staff_grant_capabilities": "grant_id capability".split(),
    "staff_access_changes": "id target_user_id change_action preset expected_generation reason status requested_by requested_at request_operation_id request_hash decided_by decided_at decision_note decision_operation_id decision_hash result_json".split(),
    "staff_access_events": "id target_user_id preset event_type actor_id operation_id policy_version before_json after_json created_at".split(),
    "member_awards": "id user_id award_id slot bonus note granted_by granted_at operation_id balance_after revoked_at revoked_by revoke_note revoke_operation_id revoke_request_hash".split(),
    "task_review_commands": "operation_id assignment_id request_hash result_status created_at".split(),
    "task_disputes": "id assignment_id task_id user_id reward reason reconciliation_reason reconciliation_reference status opened_by opened_at open_operation_id open_request_hash decided_by decided_at decision_note decision_operation_id decision_request_hash".split(),
    "task_completion_commands": "operation_id assignment_id request_hash result_status created_at".split(),
    "telegram_update_inbox": "update_id payload_json payload_sha256 status attempts available_at received_at processed_at last_error locked_by locked_at dead_at redrive_operation_id redrive_request_hash redrive_reason redriven_by redriven_at".split(),
    "telegram_update_effects": "update_id effect_key created_at".split(),
    "telegram_update_redrive_commands": "operation_id request_hash update_id admin_id reason result_status created_at".split(),
    "telegram_join_requests": "request_key update_id chat_id user_id invite_link_sha256 source status requested_at decision decision_queued_at decided_at joined_at manual_retry_reason manual_retry_by manual_retry_at last_error".split(),
    "published_posts": "kind chat_id topic message_ids published_at published_by operation_id".split(),
    "publication_jobs": "kind operation_id status requested_by created_at completed_at".split(),
    "publication_delivery_parts": "operation_id part_index message_id created_at".split(),
    "publication_cleanup_messages": "operation_id chat_id message_id final_job_status status attempts last_error deleted_at".split(),
    "task_outbox": "id event_key event_type recipient_id chat_id topic_id media_id payload_json status attempts available_at created_at sent_at telegram_message_id telegram_thread_id last_error".split(),
    "product_events": "id event_id occurred_at event_name source subject_id session_id task_id assignment_id outcome reason_code properties_json dedupe_key schema_version expires_at".split(),
}

TABLE_ORDER = list(SOURCE_COLUMNS)
IDENTITY_TABLES = {
    "awards", "tasks", "task_assignments", "task_evidence",
    "withdrawal_requests", "withdrawal_events", "bonus_ledger",
    "member_awards", "task_outbox", "product_events", "task_disputes",
    "admin_role_changes", "manual_grant_reversals",
    "staff_access_grants", "staff_access_changes", "staff_access_events",
    "task_template_events",
}

EXPECTED_SOURCE_INDEXES = {
    "idx_tasks_status", "idx_tasks_assigned", "idx_task_assignments_user",
    "idx_task_assignments_review", "idx_assignment_one_active",
    "idx_assignment_one_done", "idx_assignment_decision_operation",
    "idx_task_disputes_status", "idx_manual_grants_maker_time",
    "idx_manual_grants_recipient_time", "idx_manual_grant_reversals_status",
    "idx_manual_grant_one_pending_reversal", "idx_admin_role_changes_status",
    "idx_admin_role_change_one_pending",
    "idx_withdrawals_user", "idx_withdrawals_one_pending",
    "idx_withdrawals_operation", "idx_withdrawals_decision_operation",
    "idx_withdrawals_external_reference_canonical", "idx_withdrawal_events_request",
    "idx_ledger_user", "idx_ledger_operation", "idx_ledger_reversal_origin",
    "idx_tasks_operation",
    "idx_task_evidence_task", "idx_tasks_completion_operation",
    "idx_tasks_cancel_operation", "idx_assignments_completion_operation",
    "idx_assignments_release_operation", "idx_member_awards_user",
    "idx_member_awards_operation", "idx_member_awards_revoke_operation",
    "idx_member_awards_maker_time",
    "idx_task_outbox_delivery",
    "idx_join_requests_user_status",
    "idx_staff_access_one_active", "idx_staff_grant_capability",
    "idx_staff_access_one_pending", "idx_staff_access_changes_status",
    "idx_telegram_inbox_delivery", "idx_telegram_inbox_redrive_operation",
    "idx_media_gc", "idx_product_events_dedupe", "idx_product_events_funnel",
    "idx_product_events_subject", "idx_product_events_task",
    "idx_product_events_expiry",
    "idx_task_templates_status", "idx_task_template_versions_template",
    "idx_task_template_versions_media", "idx_task_template_events_template",
}
EXPECTED_SOURCE_TRIGGERS = {
    "task_template_versions_immutable_update",
    "task_template_versions_immutable_delete",
    "task_templates_key_immutable",
    "task_template_events_immutable_update",
    "task_template_events_immutable_delete",
    "tasks_template_provenance_insert",
    "tasks_template_provenance_update",
    "task_templates_current_version_update",
    "task_template_events_provenance_insert",
}

# Filled from a canonical post-init_db schema-299 fixture,
# including table SQL, ordered PRAGMA column metadata, explicit index SQL, and
# integrity/immutable trigger SQL. Any semantic schema drift is rejected before reading
# business rows.
EXPECTED_SOURCE_SCHEMA_SHA256 = "15b6940d3b4239b9daf4d40ba0f28b9431552da2bcaf5b78961023c60fe3900e"
EXPECTED_SOURCE_USER_VERSION = 299


class HarnessError(RuntimeError):
    exit_code = 2


class SourceError(HarnessError):
    exit_code = 2


class TargetError(HarnessError):
    exit_code = 3


class DataError(HarnessError):
    exit_code = 4


class ReconcileError(HarnessError):
    exit_code = 5


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_source(path: Path):
    if path.is_symlink():
        raise SourceError("source symbolic links are not accepted")
    path = path.resolve()
    if not path.is_file():
        raise SourceError("source must be a regular immutable SQLite backup")
    for suffix in ("-wal", "-shm"):
        companion = Path(str(path) + suffix)
        if companion.exists() and companion.stat().st_size:
            raise SourceError("source has WAL/SHM state; create a consistent backup first")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def source_schema_fingerprint(connection) -> str:
    manifest = []
    for table in sorted(SOURCE_COLUMNS):
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        columns = [
            tuple(row) for row in connection.execute(f'PRAGMA table_info("{table}")')
        ]
        manifest.append({
            "kind": "table", "name": table,
            "sql": re.sub(r"\s+", " ", str(table_sql or "")).strip(),
            "columns": columns,
        })
    for name in sorted(EXPECTED_SOURCE_INDEXES):
        row = connection.execute(
            "SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        manifest.append({
            "kind": "index", "name": name, "table": row[0] if row else None,
            "sql": re.sub(r"\s+", " ", str(row[1] if row else "")).strip(),
        })
    for name in sorted(EXPECTED_SOURCE_TRIGGERS):
        row = connection.execute(
            "SELECT tbl_name,sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()
        manifest.append({
            "kind": "trigger", "name": name, "table": row[0] if row else None,
            "sql": re.sub(r"\s+", " ", str(row[1] if row else "")).strip(),
        })
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_source_schema(connection) -> dict[str, int]:
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SourceError("source SQLite quick_check failed")
    if str(connection.execute("PRAGMA encoding").fetchone()[0]).upper() != "UTF-8":
        raise SourceError("source SQLite encoding must be UTF-8")
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != EXPECTED_SOURCE_USER_VERSION:
        raise SourceError("source SQLite user_version does not match the cutover build")
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables != set(SOURCE_COLUMNS):
        raise SourceError("source table manifest does not match the cutover build")
    for table, expected in SOURCE_COLUMNS.items():
        actual = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        if actual != expected:
            raise SourceError(f"source column manifest mismatch: {table}")
    indexes = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'"
        )
    }
    if indexes != EXPECTED_SOURCE_INDEXES:
        raise SourceError("source index manifest does not match the cutover build")
    triggers = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }
    if triggers != EXPECTED_SOURCE_TRIGGERS:
        raise SourceError("source trigger manifest does not match the cutover build")
    fingerprint = source_schema_fingerprint(connection)
    if fingerprint != EXPECTED_SOURCE_SCHEMA_SHA256:
        raise SourceError("source schema fingerprint does not match the cutover build")
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in TABLE_ORDER
    }
