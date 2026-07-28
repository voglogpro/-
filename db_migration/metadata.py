"""Canonical PostgreSQL schema for the offline SQLite migration harness.

This module deliberately has no runtime database selection or application imports.
It mirrors the current, post-``init_db`` SQLite shape losslessly while giving
PostgreSQL strict scalar types and a small set of ownership-safe foreign keys.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .access_contract import ACCESS_PRESETS, CAPABILITIES_V1, sql_literals


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING_CONVENTION)


def fk(target: str) -> ForeignKey:
    return ForeignKey(
        target,
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )


members = Table(
    "members", metadata,
    Column("user_id", BigInteger, primary_key=True),
    Column("full_name", Text),
    Column("username", Text),
    Column("phone", Text),
    Column("city", Text),
    Column("help_type", Text),
    Column("transport", Text),
    Column("availability", Text),
    Column("about", Text),
    Column("tags", Text),
    Column("application_note", Text),
    Column("role", Text, nullable=False, server_default=text("'candidate'")),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("bonus", BigInteger, nullable=False, server_default=text("0")),
    Column("done_count", BigInteger, nullable=False, server_default=text("0")),
    Column("referred_by", BigInteger),
    Column("created_at", DateTime(timezone=True)),
    Column("approved_at", DateTime(timezone=True)),
    Column("approved_by", BigInteger),
    Column("applied_at", DateTime(timezone=True)),
    Column("chat_xp", BigInteger, nullable=False, server_default=text("0")),
    Column("ref_confirmed", Boolean, nullable=False, server_default=text("false")),
    Column("city_change_requested", Text),
    Column("city_change_requested_at", DateTime(timezone=True)),
    Column(
        "group_membership_status", Text, nullable=False,
        server_default=text("'unknown'"),
    ),
    Column("group_joined_at", DateTime(timezone=True)),
    Column("group_left_at", DateTime(timezone=True)),
)

tasks = Table(
    "tasks", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("type", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("details", Text),
    Column("lat", Double),
    Column("lng", Double),
    Column("address", Text),
    Column("city", Text),
    Column("reward", BigInteger, nullable=False, server_default=text("0")),
    Column("status", Text, nullable=False, server_default=text("'open'")),
    Column("created_by", BigInteger),
    Column("created_at", DateTime(timezone=True)),
    Column("claimed_by", BigInteger),
    Column("claimed_at", DateTime(timezone=True)),
    Column("done_at", DateTime(timezone=True)),
    Column("proof_note", Text),
    Column("review_note", Text),
    Column("assigned_to", BigInteger),
    Column("slot_start", DateTime(timezone=True)),
    Column("slot_end", DateTime(timezone=True)),
    Column("repeatable", Boolean, nullable=False, server_default=text("false")),
    Column("photo_file", Text),
    Column("photo_media_id", Text),
    Column("operation_id", Text),
    Column("request_hash", Text),
    Column("completion_operation_id", Text),
    Column("completion_request_hash", Text),
    Column("submission_attempt", Integer, nullable=False, server_default=text("0")),
    Column("evidence_policy", Text, nullable=False, server_default=text("'none'")),
    Column("max_participants", Integer),
    Column("budget_cap", BigInteger),
    Column("cancel_operation_id", Text),
    Column("cancel_request_hash", Text),
    Column("cancelled_at", DateTime(timezone=True)),
    Column("cancelled_by", BigInteger),
    Column("cancel_reason", Text),
    Column("expired_at", DateTime(timezone=True)),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("template_id", Text),
    Column("template_version_id", Text),
    ForeignKeyConstraint(
        ("template_id", "template_version_id"),
        ("task_template_versions.template_id", "task_template_versions.id"),
        name="fk_tasks_template_version", ondelete="RESTRICT",
        deferrable=True, initially="DEFERRED",
    ),
    CheckConstraint(
        "(template_id IS NULL)=(template_version_id IS NULL)",
        name="tasks_template_provenance_pair",
    ),
)

bonus_ledger = Table(
    "bonus_ledger", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("reason", Text, nullable=False),
    Column("task_id", BigInteger),
    Column("assignment_id", BigInteger),
    Column("withdrawal_id", BigInteger),
    Column("created_by", BigInteger),
    Column("created_at", DateTime(timezone=True)),
    Column("operation_id", Text),
    Column("balance_after", BigInteger),
    Column("reversal_of_ledger_id", BigInteger, fk("bonus_ledger.id")),
)

referral_rewards = Table(
    "referral_rewards", metadata,
    Column("referee_id", BigInteger, primary_key=True),
    Column("referrer_id", BigInteger, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

referral_tokens = Table(
    "referral_tokens", metadata,
    Column("token", Text, primary_key=True),
    Column("referrer_id", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

referral_milestone_rewards = Table(
    "referral_milestone_rewards", metadata,
    Column("user_id", BigInteger, nullable=False),
    Column("threshold", Integer, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("user_id", "threshold", name="pk_referral_milestone_rewards"),
)

task_assignments = Table(
    "task_assignments", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("task_id", BigInteger, fk("tasks.id"), nullable=False),
    Column("user_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("status", Text, nullable=False, server_default=text("'claimed'")),
    Column("claimed_at", DateTime(timezone=True), nullable=False),
    Column("done_at", DateTime(timezone=True)),
    Column("proof_note", Text),
    Column("review_note", Text),
    Column("completion_operation_id", Text),
    Column("completion_request_hash", Text),
    Column("submission_attempt", Integer, nullable=False, server_default=text("0")),
    Column("reward_snapshot", BigInteger),
    Column("due_at", DateTime(timezone=True)),
    Column("revision_due_at", DateTime(timezone=True)),
    Column("release_operation_id", Text),
    Column("release_request_hash", Text),
    Column("released_at", DateTime(timezone=True)),
    Column("release_reason", Text),
    Column("terminal_at", DateTime(timezone=True)),
    Column("terminal_by", BigInteger),
    Column("terminal_reason", Text),
    Column("decision_operation_id", Text),
    Column("decision_request_hash", Text),
    Column("version", Integer, nullable=False, server_default=text("1")),
)

task_evidence = Table(
    "task_evidence", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("assignment_id", BigInteger, fk("task_assignments.id")),
    Column("task_id", BigInteger, fk("tasks.id"), nullable=False),
    Column("user_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("photo_file", Text, nullable=False),
    Column("media_id", Text),
    Column("sha256", Text, nullable=False),
    Column("submission_operation_id", Text),
    Column("attempt", Integer, nullable=False, server_default=text("1")),
    Column("is_current", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

media_objects = Table(
    "media_objects", metadata,
    Column("id", Text, primary_key=True),
    Column("backend", Text, nullable=False),
    Column("object_key", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("content_type", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("upload_operation_id", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("ready_at", DateTime(timezone=True)),
    Column("delete_after", DateTime(timezone=True)),
    Column("deleted_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("reconcile_attempts", Integer, nullable=False, server_default=text("0")),
    Column("version_id", Text),
    Column("checked_at", DateTime(timezone=True)),
    UniqueConstraint("upload_operation_id", name="uq_media_objects_upload_operation_id"),
    UniqueConstraint("backend", "object_key", name="uq_media_objects_backend_object_key"),
)

task_templates = Table(
    "task_templates", metadata,
    Column("id", Text, primary_key=True),
    Column("key", Text, nullable=False),
    Column("origin", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("current_version_id", Text, nullable=False),
    Column("created_by", BigInteger, fk("members.user_id")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_by", BigInteger, fk("members.user_id")),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("archived_by", BigInteger, fk("members.user_id")),
    Column("archived_at", DateTime(timezone=True)),
    UniqueConstraint("key", name="uq_task_templates_key"),
    CheckConstraint("origin IN ('system','manual')", name="task_templates_origin"),
    CheckConstraint("status IN ('active','archived')", name="task_templates_status"),
    CheckConstraint("generation > 0", name="task_templates_generation"),
    CheckConstraint(
        "key ~ '^[a-z][a-z0-9_]{2,49}$'",
        name="task_templates_key",
    ),
    CheckConstraint(
        "(status='active' AND archived_by IS NULL AND archived_at IS NULL) OR "
        "(status='archived' AND archived_by IS NOT NULL AND archived_at IS NOT NULL)",
        name="task_templates_archive_state",
    ),
)

task_template_versions = Table(
    "task_template_versions", metadata,
    Column("id", Text, primary_key=True),
    Column("template_id", Text, fk("task_templates.id"), nullable=False),
    Column("version_number", Integer, nullable=False),
    Column("title", Text, nullable=False),
    Column("task_type", Text, nullable=False),
    Column("task_title", Text, nullable=False),
    Column("details", Text, nullable=False, server_default=text("''")),
    Column("reward", BigInteger, nullable=False),
    Column("mode", Text, nullable=False),
    Column("evidence_policy", Text, nullable=False),
    Column("max_participants", Integer, nullable=False),
    Column("budget_cap", BigInteger, nullable=False),
    Column("photo_media_id", Text, fk("media_objects.id")),
    Column("photo_sha256", Text),
    Column("content_hash", Text, nullable=False),
    Column("created_by", BigInteger, fk("members.user_id")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "template_id", "version_number",
        name="uq_task_template_versions_number",
    ),
    UniqueConstraint(
        "template_id", "id", name="uq_task_template_versions_template_id",
    ),
    CheckConstraint("version_number > 0", name="task_template_versions_number"),
    CheckConstraint(
        "reward BETWEEN 1 AND 300", name="task_template_versions_reward",
    ),
    CheckConstraint(
        "task_type IN ('relocate','fix_zone','charge','rescue','community',"
        "'referral','photo_check')",
        name="task_template_versions_type",
    ),
    CheckConstraint(
        "mode IN ('open','personal','all')", name="task_template_versions_mode",
    ),
    CheckConstraint(
        "evidence_policy IN "
        "('none','comment_only','photo_required','before_after')",
        name="task_template_versions_evidence",
    ),
    CheckConstraint(
        "(mode IN ('open','personal') AND max_participants=1 "
        "AND budget_cap=reward) OR (mode='all' AND max_participants BETWEEN 1 "
        "AND 500 AND budget_cap BETWEEN reward AND 150000 "
        "AND reward*max_participants<=budget_cap)",
        name="task_template_versions_participants",
    ),
    CheckConstraint(
        "(photo_media_id IS NULL)=(photo_sha256 IS NULL)",
        name="task_template_versions_photo_pair",
    ),
    CheckConstraint(
        "photo_sha256 IS NULL OR photo_sha256 ~ '^[0-9a-f]{64}$'",
        name="task_template_versions_photo_sha",
    ),
    CheckConstraint(
        "content_hash ~ '^[0-9a-f]{64}$'",
        name="task_template_versions_content_hash",
    ),
    CheckConstraint(
        "evidence_policy<>'before_after' OR photo_media_id IS NOT NULL",
        name="task_template_versions_before_after_photo",
    ),
)

task_templates.append_constraint(ForeignKeyConstraint(
    ("id", "current_version_id"),
    ("task_template_versions.template_id", "task_template_versions.id"),
    name="fk_task_templates_current_version", ondelete="RESTRICT",
    deferrable=True, initially="DEFERRED",
))

task_template_events = Table(
    "task_template_events", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("template_id", Text, fk("task_templates.id"), nullable=False),
    Column("template_version_id", Text),
    Column("event_type", Text, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("actor_id", BigInteger, fk("members.user_id")),
    Column("operation_id", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("note", Text, nullable=False, server_default=text("''")),
    Column("before_json", JSONB, nullable=False),
    Column("after_json", JSONB, nullable=False),
    Column("result_json", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ("template_id", "template_version_id"),
        ("task_template_versions.template_id", "task_template_versions.id"),
        name="fk_task_template_events_version", ondelete="RESTRICT",
        deferrable=True, initially="DEFERRED",
    ),
    UniqueConstraint("operation_id", name="uq_task_template_events_operation"),
    UniqueConstraint(
        "template_id", "generation", name="uq_task_template_events_generation",
    ),
    CheckConstraint(
        "event_type IN ('created','version_created','archived','activated')",
        name="task_template_events_type",
    ),
    CheckConstraint("generation > 0", name="task_template_events_generation"),
    CheckConstraint(
        "request_hash ~ '^[0-9a-f]{64}$'",
        name="task_template_events_request_hash",
    ),
)

withdrawal_requests = Table(
    "withdrawal_requests", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_by", BigInteger),
    Column("decided_at", DateTime(timezone=True)),
    Column("note", Text),
    Column("operation_id", Text),
    Column("request_hash", Text),
    Column("account_type", Text),
    Column("account_ciphertext", Text),
    Column("account_masked", Text),
    Column("account_fingerprint", Text),
    Column("key_version", Integer),
    Column("decision_operation_id", Text),
    Column("decision_request_hash", Text),
    Column("provider", Text),
    Column("external_reference", Text),
    Column("external_reference_canonical", Text),
    Column("reject_reason", Text),
    Column("account_purged_at", DateTime(timezone=True)),
    Column("processing_by", BigInteger),
    Column("processing_at", DateTime(timezone=True)),
)

withdrawal_events = Table(
    "withdrawal_events", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("withdrawal_id", BigInteger, fk("withdrawal_requests.id"), nullable=False),
    Column("event_type", Text, nullable=False),
    Column("from_status", Text),
    Column("to_status", Text, nullable=False),
    Column("actor_id", BigInteger),
    Column("operation_id", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
)

task_review_commands = Table(
    "task_review_commands", metadata,
    Column("operation_id", Text, primary_key=True),
    Column("assignment_id", BigInteger, fk("task_assignments.id"), nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("result_status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

manual_grant_commands = Table(
    "manual_grant_commands", metadata,
    Column("operation_id", Text, primary_key=True),
    Column("request_hash", Text, nullable=False),
    Column("user_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("reason", Text, nullable=False),
    Column("maker_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("ledger_id", BigInteger, fk("bonus_ledger.id"), nullable=False),
    Column("result_balance", BigInteger, nullable=False),
    CheckConstraint("amount BETWEEN 1 AND 200", name="manual_grant_positive"),
    CheckConstraint("maker_id <> user_id", name="manual_grant_distinct"),
)

manual_grant_reversals = Table(
    "manual_grant_reversals", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "grant_operation_id", Text, fk("manual_grant_commands.operation_id"),
        nullable=False,
    ),
    Column(
        "original_ledger_id", BigInteger, fk("bonus_ledger.id"), nullable=False,
    ),
    Column("user_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("reason", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("manual_reason", Text),
    Column("requested_by", BigInteger, fk("members.user_id"), nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("request_operation_id", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("decided_by", BigInteger, fk("members.user_id")),
    Column("decided_at", DateTime(timezone=True)),
    Column("decision_note", Text),
    Column("decision_operation_id", Text),
    Column("decision_hash", Text),
    Column("reversal_ledger_id", BigInteger, fk("bonus_ledger.id")),
    Column("result_balance", BigInteger),
    UniqueConstraint(
        "request_operation_id", name="uq_manual_grant_reversal_request_operation",
    ),
    UniqueConstraint(
        "decision_operation_id", name="uq_manual_grant_reversal_decision_operation",
    ),
    UniqueConstraint(
        "reversal_ledger_id", name="uq_manual_grant_reversal_ledger",
    ),
    CheckConstraint("amount BETWEEN 1 AND 200", name="manual_grant_reversal_amount"),
    CheckConstraint(
        "status IN ('pending','manual_required','applied','rejected')",
        name="manual_grant_reversal_status",
    ),
    CheckConstraint(
        "requested_by <> user_id", name="manual_grant_reversal_maker_target",
    ),
    CheckConstraint(
        "decided_by IS NULL OR (decided_by <> requested_by AND decided_by <> user_id)",
        name="manual_grant_reversal_checker_distinct",
    ),
)

admin_role_changes = Table(
    "admin_role_changes", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("from_role", Text, nullable=False),
    Column("to_role", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("requested_by", BigInteger, fk("members.user_id"), nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("request_operation_id", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("decided_by", BigInteger, fk("members.user_id")),
    Column("decided_at", DateTime(timezone=True)),
    Column("decision_note", Text),
    Column("decision_operation_id", Text),
    Column("decision_hash", Text),
    UniqueConstraint("request_operation_id", name="uq_admin_role_request_operation"),
    UniqueConstraint("decision_operation_id", name="uq_admin_role_decision_operation"),
    CheckConstraint("from_role <> to_role", name="admin_role_distinct"),
    CheckConstraint(
        "status IN ('pending','applied','rejected')", name="admin_role_status",
    ),
    CheckConstraint("requested_by <> user_id", name="admin_role_maker_target"),
    CheckConstraint(
        "decided_by IS NULL OR (decided_by <> requested_by AND decided_by <> user_id)",
        name="admin_role_checker_distinct",
    ),
)

operation_registry = Table(
    "operation_registry", metadata,
    Column("operation_id", Text, primary_key=True),
    Column("command_type", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("actor_id", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

admin_authorities = Table(
    "admin_authorities", metadata,
    Column("user_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("origin", Text, nullable=False),
    Column("granted_operation_id", Text),
    Column("granted_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("user_id", "origin", name="pk_admin_authorities"),
    CheckConstraint("origin IN ('env','manual')", name="admin_authority_origin"),
)

staff_access_grants = Table(
    "staff_access_grants", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("preset", Text, nullable=False),
    Column("origin", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("policy_version", Integer, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("granted_by", BigInteger),
    Column("approved_by", BigInteger),
    Column("grant_operation_id", Text, nullable=False),
    Column("granted_at", DateTime(timezone=True), nullable=False),
    Column("revoked_by", BigInteger),
    Column("revoke_operation_id", Text),
    Column("revoked_at", DateTime(timezone=True)),
    UniqueConstraint(
        "grant_operation_id", name="uq_staff_access_grants_grant_operation",
    ),
    UniqueConstraint(
        "revoke_operation_id", name="uq_staff_access_grants_revoke_operation",
    ),
    UniqueConstraint(
        "user_id", "preset", "origin", "generation",
        name="uq_staff_access_grants_generation",
    ),
    CheckConstraint(
        f"preset IN ({sql_literals(ACCESS_PRESETS)})",
        name="staff_access_grants_preset",
    ),
    CheckConstraint("origin IN ('env','manual')", name="staff_access_grants_origin"),
    CheckConstraint("status IN ('active','revoked')", name="staff_access_grants_status"),
    CheckConstraint("generation > 0", name="staff_access_grants_generation"),
    CheckConstraint(
        "approved_by IS NULL OR granted_by IS NULL OR approved_by<>granted_by",
        name="staff_access_grants_approver_distinct",
    ),
)

staff_grant_capabilities = Table(
    "staff_grant_capabilities", metadata,
    Column(
        "grant_id", BigInteger, fk("staff_access_grants.id"), nullable=False,
    ),
    Column("capability", Text, nullable=False),
    PrimaryKeyConstraint(
        "grant_id", "capability", name="pk_staff_grant_capabilities",
    ),
    CheckConstraint(
        f"capability IN ({sql_literals(CAPABILITIES_V1)})",
        name="staff_grant_capabilities_capability",
    ),
)

staff_access_changes = Table(
    "staff_access_changes", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("target_user_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("change_action", Text, nullable=False),
    Column("preset", Text, nullable=False),
    Column("expected_generation", Integer, nullable=False),
    Column("reason", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("requested_by", BigInteger, nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("request_operation_id", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("decided_by", BigInteger),
    Column("decided_at", DateTime(timezone=True)),
    Column("decision_note", Text),
    Column("decision_operation_id", Text),
    Column("decision_hash", Text),
    Column("result_json", JSONB),
    UniqueConstraint(
        "request_operation_id", name="uq_staff_access_changes_request_operation",
    ),
    UniqueConstraint(
        "decision_operation_id", name="uq_staff_access_changes_decision_operation",
    ),
    CheckConstraint(
        "change_action IN ('assign','revoke')", name="staff_access_changes_action",
    ),
    CheckConstraint(
        f"preset IN ({sql_literals(ACCESS_PRESETS)})",
        name="staff_access_changes_preset",
    ),
    CheckConstraint(
        "status IN ('pending','applied','rejected')",
        name="staff_access_changes_status",
    ),
    CheckConstraint(
        "expected_generation >= 0", name="staff_access_changes_expected_generation",
    ),
    CheckConstraint(
        "requested_by<>target_user_id",
        name="staff_access_changes_requester_target",
    ),
    CheckConstraint(
        "decided_by IS NULL OR (decided_by<>requested_by "
        "AND decided_by<>target_user_id)",
        name="staff_access_changes_checker_distinct",
    ),
)

staff_access_events = Table(
    "staff_access_events", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("target_user_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("preset", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("actor_id", BigInteger),
    Column("operation_id", Text, nullable=False),
    Column("policy_version", Integer, nullable=False),
    Column("before_json", JSONB, nullable=False),
    Column("after_json", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("operation_id", name="uq_staff_access_events_operation"),
)

task_disputes = Table(
    "task_disputes", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("assignment_id", BigInteger, fk("task_assignments.id"), nullable=False),
    Column("task_id", BigInteger, nullable=False),
    Column("user_id", BigInteger, nullable=False),
    Column("reward", BigInteger, nullable=False),
    Column("reason", Text, nullable=False),
    Column("reconciliation_reason", Text),
    Column("reconciliation_reference", Text),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("opened_by", BigInteger, nullable=False),
    Column("opened_at", DateTime(timezone=True), nullable=False),
    Column("open_operation_id", Text, nullable=False),
    Column("open_request_hash", Text, nullable=False),
    Column("decided_by", BigInteger),
    Column("decided_at", DateTime(timezone=True)),
    Column("decision_note", Text),
    Column("decision_operation_id", Text),
    Column("decision_request_hash", Text),
    UniqueConstraint("assignment_id", name="uq_task_disputes_assignment_id"),
    UniqueConstraint("open_operation_id", name="uq_task_disputes_open_operation_id"),
    UniqueConstraint(
        "decision_operation_id", name="uq_task_disputes_decision_operation_id",
    ),
)

task_completion_commands = Table(
    "task_completion_commands", metadata,
    Column("operation_id", Text, primary_key=True),
    Column("assignment_id", BigInteger, fk("task_assignments.id"), nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("result_status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

task_outbox = Table(
    "task_outbox", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("event_key", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("recipient_id", BigInteger),
    Column("chat_id", Text),
    Column("topic_id", BigInteger),
    Column("media_id", Text),
    Column("payload_json", JSONB, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("sent_at", DateTime(timezone=True)),
    Column("telegram_message_id", BigInteger),
    Column("telegram_thread_id", BigInteger),
    Column("last_error", Text),
    UniqueConstraint("event_key", name="uq_task_outbox_event_key"),
)

telegram_update_inbox = Table(
    "telegram_update_inbox", metadata,
    Column("update_id", BigInteger, primary_key=True),
    Column("payload_json", Text),
    Column("payload_sha256", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("processed_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("locked_by", Text),
    Column("locked_at", DateTime(timezone=True)),
    Column("dead_at", DateTime(timezone=True)),
    Column("redrive_operation_id", Text),
    Column("redrive_request_hash", Text),
    Column("redrive_reason", Text),
    Column("redriven_by", BigInteger),
    Column("redriven_at", DateTime(timezone=True)),
)

telegram_update_effects = Table(
    "telegram_update_effects", metadata,
    Column("update_id", BigInteger, primary_key=True),
    Column("effect_key", Text, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

telegram_update_redrive_commands = Table(
    "telegram_update_redrive_commands", metadata,
    Column("operation_id", Text, primary_key=True),
    Column("request_hash", Text, nullable=False),
    Column("update_id", BigInteger, nullable=False),
    Column("admin_id", BigInteger, nullable=False),
    Column("reason", Text, nullable=False),
    Column("result_status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

chat_activity = Table(
    "chat_activity", metadata,
    Column("user_id", BigInteger, primary_key=True),
    Column("last_msg_at", DateTime(timezone=True)),
    Column("day", Date),
    Column("msg_xp_today", BigInteger, nullable=False, server_default=text("0")),
    Column("thanks_xp_today", BigInteger, nullable=False, server_default=text("0")),
    Column("messages_total", BigInteger, nullable=False, server_default=text("0")),
    Column("thanks_total", BigInteger, nullable=False, server_default=text("0")),
)

analytics_subjects = Table(
    "analytics_subjects", metadata,
    Column("subject_id", Text, primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("user_id", name="uq_analytics_subjects_user_id"),
)

product_events = Table(
    "product_events", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("event_id", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("event_name", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("subject_id", Text),
    Column("session_id", Text),
    Column("task_id", BigInteger),
    Column("assignment_id", BigInteger),
    Column("outcome", Text),
    Column("reason_code", Text),
    Column("properties_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("dedupe_key", Text),
    Column("schema_version", Integer, nullable=False, server_default=text("1")),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("event_id", name="uq_product_events_event_id"),
)

published_posts = Table(
    "published_posts", metadata,
    Column("kind", Text, primary_key=True),
    Column("chat_id", BigInteger, nullable=False),
    Column("topic", BigInteger),
    Column("message_ids", JSONB, nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=False),
    Column("published_by", BigInteger),
    Column("operation_id", Text),
)

publication_jobs = Table(
    "publication_jobs", metadata,
    Column("kind", Text, primary_key=True),
    Column("operation_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("requested_by", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    UniqueConstraint("operation_id", name="uq_publication_jobs_operation_id"),
)

publication_delivery_parts = Table(
    "publication_delivery_parts", metadata,
    Column("operation_id", Text, primary_key=True),
    Column("part_index", Integer, primary_key=True),
    Column("message_id", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

publication_cleanup_messages = Table(
    "publication_cleanup_messages", metadata,
    Column("operation_id", Text, primary_key=True),
    Column("chat_id", Text, primary_key=True),
    Column("message_id", BigInteger, primary_key=True),
    Column("final_job_status", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("last_error", Text),
    Column("deleted_at", DateTime(timezone=True)),
)

thanks_pairs = Table(
    "thanks_pairs", metadata,
    Column("from_id", BigInteger, primary_key=True),
    Column("to_id", BigInteger, primary_key=True),
    Column("last_at", DateTime(timezone=True), nullable=False),
)

awards = Table(
    "awards", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("code", Text),
    Column("emoji", Text, nullable=False, server_default=text(r"U&'\+01F3C5'")),
    Column("title", Text, nullable=False),
    Column("description", Text),
    Column("bonus", BigInteger, nullable=False, server_default=text("0")),
    Column("repeatable", Boolean, nullable=False, server_default=text("true")),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("created_by", BigInteger),
    Column("created_at", DateTime(timezone=True)),
    UniqueConstraint("code", name="uq_awards_code"),
)

member_awards = Table(
    "member_awards", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("award_id", BigInteger, fk("awards.id"), nullable=False),
    Column("slot", Text, nullable=False, server_default=text("''")),
    Column("bonus", BigInteger, nullable=False, server_default=text("0")),
    Column("note", Text),
    Column("granted_by", BigInteger),
    Column("granted_at", DateTime(timezone=True), nullable=False),
    Column("operation_id", Text),
    Column("balance_after", BigInteger),
    Column("revoked_at", DateTime(timezone=True)),
    Column("revoked_by", BigInteger),
    Column("revoke_note", Text),
    Column("revoke_operation_id", Text),
    Column("revoke_request_hash", Text),
    UniqueConstraint("user_id", "award_id", "slot", name="uq_member_awards_user_award_slot"),
)


# The 32 semantic indexes created by the current SQLite initializer.
Index("idx_tasks_status", tasks.c.status)
Index("idx_tasks_assigned", tasks.c.assigned_to, tasks.c.status)
Index("idx_task_assignments_user", task_assignments.c.user_id, task_assignments.c.status)
Index("idx_task_assignments_review", task_assignments.c.status, task_assignments.c.task_id)
Index(
    "idx_assignment_one_active", task_assignments.c.task_id, task_assignments.c.user_id,
    unique=True,
    postgresql_where=task_assignments.c.status.in_(("claimed", "review")),
)
Index(
    "idx_assignment_one_done", task_assignments.c.task_id, task_assignments.c.user_id,
    unique=True, postgresql_where=task_assignments.c.status == "done",
)
Index(
    "idx_assignment_decision_operation", task_assignments.c.decision_operation_id,
    unique=True, postgresql_where=task_assignments.c.decision_operation_id.is_not(None),
)

telegram_join_requests = Table(
    "telegram_join_requests", metadata,
    Column("request_key", Text, primary_key=True),
    Column("update_id", BigInteger, unique=True),
    Column("chat_id", Text, nullable=False),
    Column("user_id", BigInteger, fk("members.user_id"), nullable=False),
    Column("invite_link_sha256", Text),
    Column("source", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("decision", Text),
    Column("decision_queued_at", DateTime(timezone=True)),
    Column("decided_at", DateTime(timezone=True)),
    Column("joined_at", DateTime(timezone=True)),
    Column("manual_retry_reason", Text),
    Column("manual_retry_by", BigInteger, fk("members.user_id")),
    Column("manual_retry_at", DateTime(timezone=True)),
    Column("last_error", Text),
    CheckConstraint(
        "source IN ('bot_invite','unverified')", name="join_request_source",
    ),
    CheckConstraint(
        "decision IS NULL OR decision IN ('approve','decline')",
        name="join_request_decision",
    ),
)
Index(
    "idx_task_disputes_status", task_disputes.c.status,
    task_disputes.c.opened_at, task_disputes.c.id,
)
Index(
    "idx_manual_grants_maker_time", manual_grant_commands.c.maker_id,
    manual_grant_commands.c.created_at,
)
Index(
    "idx_manual_grants_recipient_time", manual_grant_commands.c.user_id,
    manual_grant_commands.c.created_at,
)
Index(
    "idx_manual_grant_reversals_status", manual_grant_reversals.c.status,
    manual_grant_reversals.c.requested_at, manual_grant_reversals.c.id,
)
Index(
    "idx_manual_grant_one_pending_reversal",
    manual_grant_reversals.c.grant_operation_id, unique=True,
    postgresql_where=manual_grant_reversals.c.status.in_(("pending", "manual_required")),
)
Index(
    "idx_admin_role_changes_status", admin_role_changes.c.status,
    admin_role_changes.c.requested_at, admin_role_changes.c.id,
)
Index(
    "idx_admin_role_change_one_pending", admin_role_changes.c.user_id,
    unique=True, postgresql_where=admin_role_changes.c.status == "pending",
)
Index("idx_withdrawals_user", withdrawal_requests.c.user_id, withdrawal_requests.c.created_at)
Index(
    "idx_withdrawals_one_pending", withdrawal_requests.c.user_id, unique=True,
    postgresql_where=withdrawal_requests.c.status.in_(("pending", "processing")),
)
Index(
    "idx_withdrawals_operation", withdrawal_requests.c.operation_id, unique=True,
    postgresql_where=withdrawal_requests.c.operation_id.is_not(None),
)
Index(
    "idx_withdrawals_decision_operation", withdrawal_requests.c.decision_operation_id,
    unique=True,
    postgresql_where=withdrawal_requests.c.decision_operation_id.is_not(None),
)
Index(
    "idx_withdrawals_external_reference_canonical",
    withdrawal_requests.c.provider,
    withdrawal_requests.c.external_reference_canonical,
    unique=True,
    postgresql_where=(withdrawal_requests.c.status == "completed")
    & withdrawal_requests.c.external_reference_canonical.is_not(None),
)
Index("idx_withdrawal_events_request", withdrawal_events.c.withdrawal_id, withdrawal_events.c.id)
Index("idx_ledger_user", bonus_ledger.c.user_id)
Index(
    "idx_ledger_operation", bonus_ledger.c.operation_id, unique=True,
    postgresql_where=bonus_ledger.c.operation_id.is_not(None),
)
Index(
    "idx_ledger_reversal_origin", bonus_ledger.c.reversal_of_ledger_id,
    unique=True, postgresql_where=bonus_ledger.c.reversal_of_ledger_id.is_not(None),
)
Index(
    "idx_tasks_operation", tasks.c.operation_id, unique=True,
    postgresql_where=tasks.c.operation_id.is_not(None),
)
Index("idx_task_evidence_task", task_evidence.c.task_id, task_evidence.c.assignment_id, task_evidence.c.user_id)
Index(
    "idx_tasks_completion_operation", tasks.c.completion_operation_id, unique=True,
    postgresql_where=tasks.c.completion_operation_id.is_not(None),
)
Index(
    "idx_tasks_cancel_operation", tasks.c.cancel_operation_id, unique=True,
    postgresql_where=tasks.c.cancel_operation_id.is_not(None),
)
Index(
    "idx_assignments_completion_operation", task_assignments.c.completion_operation_id,
    unique=True, postgresql_where=task_assignments.c.completion_operation_id.is_not(None),
)
Index(
    "idx_assignments_release_operation", task_assignments.c.release_operation_id,
    unique=True, postgresql_where=task_assignments.c.release_operation_id.is_not(None),
)
Index("idx_member_awards_user", member_awards.c.user_id, member_awards.c.granted_at)
Index(
    "idx_member_awards_maker_time", member_awards.c.granted_by,
    member_awards.c.granted_at,
)
Index(
    "idx_member_awards_operation", member_awards.c.operation_id, unique=True,
    postgresql_where=member_awards.c.operation_id.is_not(None),
)
Index(
    "idx_member_awards_revoke_operation", member_awards.c.revoke_operation_id,
    unique=True, postgresql_where=member_awards.c.revoke_operation_id.is_not(None),
)
Index("idx_task_outbox_delivery", task_outbox.c.status, task_outbox.c.available_at, task_outbox.c.id)
Index(
    "idx_telegram_inbox_delivery", telegram_update_inbox.c.status,
    telegram_update_inbox.c.available_at, telegram_update_inbox.c.update_id,
)
Index(
    "idx_telegram_inbox_redrive_operation", telegram_update_inbox.c.redrive_operation_id,
    unique=True, postgresql_where=telegram_update_inbox.c.redrive_operation_id.is_not(None),
)
Index(
    "idx_join_requests_user_status", telegram_join_requests.c.user_id,
    telegram_join_requests.c.status, telegram_join_requests.c.requested_at,
)
Index("idx_media_gc", media_objects.c.state, media_objects.c.delete_after)
Index("idx_task_templates_status", task_templates.c.status, task_templates.c.id)
Index(
    "idx_task_template_versions_template",
    task_template_versions.c.template_id, task_template_versions.c.version_number,
)
Index(
    "idx_task_template_versions_media", task_template_versions.c.photo_media_id,
)
Index(
    "idx_task_template_events_template",
    task_template_events.c.template_id, task_template_events.c.generation,
    task_template_events.c.id,
)
Index(
    "idx_staff_access_one_active",
    staff_access_grants.c.user_id, staff_access_grants.c.preset,
    staff_access_grants.c.origin, unique=True,
    postgresql_where=staff_access_grants.c.status == "active",
)
Index(
    "idx_staff_grant_capability", staff_grant_capabilities.c.capability,
    staff_grant_capabilities.c.grant_id,
)
Index(
    "idx_staff_access_one_pending",
    staff_access_changes.c.target_user_id, staff_access_changes.c.preset,
    unique=True, postgresql_where=staff_access_changes.c.status == "pending",
)
Index(
    "idx_staff_access_changes_status", staff_access_changes.c.status,
    staff_access_changes.c.requested_at, staff_access_changes.c.id,
)
Index(
    "idx_product_events_dedupe", product_events.c.dedupe_key, unique=True,
    postgresql_where=product_events.c.dedupe_key.is_not(None),
)
Index("idx_product_events_funnel", product_events.c.event_name, product_events.c.occurred_at)
Index("idx_product_events_subject", product_events.c.subject_id, product_events.c.occurred_at)
Index(
    "idx_product_events_task", product_events.c.task_id, product_events.c.occurred_at,
    postgresql_where=product_events.c.task_id.is_not(None),
)
Index("idx_product_events_expiry", product_events.c.expires_at)


TABLE_ORDER = tuple(metadata.tables)
IDENTITY_TABLES = (
    "tasks", "bonus_ledger", "task_assignments", "task_evidence", "task_disputes",
    "admin_role_changes", "manual_grant_reversals",
    "withdrawal_requests", "withdrawal_events", "task_outbox",
    "product_events", "awards", "member_awards",
    "staff_access_grants", "staff_access_changes", "staff_access_events",
    "task_template_events",
)

if len(metadata.tables) != 41:
    raise RuntimeError(f"Expected 41 migration tables, found {len(metadata.tables)}")
if sum(len(table.indexes) for table in metadata.tables.values()) != 51:
    raise RuntimeError("Expected exactly 51 semantic indexes")
