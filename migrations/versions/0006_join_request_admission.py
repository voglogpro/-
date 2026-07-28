"""Add managed Telegram join-request admission state.

Revision ID: 0006_join_request_admission
Revises: 0005_manual_grant_reversals
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0006_join_request_admission"
down_revision: str | None = "0005_manual_grant_reversals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE members ADD COLUMN "
        "group_membership_status TEXT NOT NULL DEFAULT 'unknown'"
    )
    op.execute("ALTER TABLE members ADD COLUMN group_joined_at TIMESTAMPTZ")
    op.execute("ALTER TABLE members ADD COLUMN group_left_at TIMESTAMPTZ")
    op.execute("""
        CREATE TABLE telegram_join_requests (
            request_key TEXT NOT NULL,
            update_id BIGINT,
            chat_id TEXT NOT NULL,
            user_id BIGINT NOT NULL,
            invite_link_sha256 TEXT,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_at TIMESTAMPTZ NOT NULL,
            decision TEXT,
            decision_queued_at TIMESTAMPTZ,
            decided_at TIMESTAMPTZ,
            joined_at TIMESTAMPTZ,
            manual_retry_reason TEXT,
            manual_retry_by BIGINT,
            manual_retry_at TIMESTAMPTZ,
            last_error TEXT,
            CONSTRAINT pk_telegram_join_requests PRIMARY KEY (request_key),
            CONSTRAINT uq_telegram_join_requests_update UNIQUE (update_id),
            CONSTRAINT fk_telegram_join_requests_user
                FOREIGN KEY (user_id) REFERENCES members (user_id)
                ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_telegram_join_requests_retry_admin
                FOREIGN KEY (manual_retry_by) REFERENCES members (user_id)
                ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_telegram_join_requests_source
                CHECK (source IN ('bot_invite','unverified')),
            CONSTRAINT ck_telegram_join_requests_decision
                CHECK (decision IS NULL OR decision IN ('approve','decline'))
        )
    """)
    op.execute(
        "CREATE INDEX idx_join_requests_user_status ON "
        "telegram_join_requests (user_id, status, requested_at)"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Destructive join-request downgrade is disabled. Restore a verified "
        "backup or deploy a forward corrective migration."
    )
