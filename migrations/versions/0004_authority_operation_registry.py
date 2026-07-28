"""Add dual-source admin authority and global operation registry.

Revision ID: 0004_authority_registry
Revises: 0003_admin_financial_controls
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0004_authority_registry"
down_revision: str | None = "0003_admin_financial_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE operation_registry (
            operation_id TEXT NOT NULL, command_type TEXT NOT NULL,
            request_hash TEXT NOT NULL, actor_id BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT pk_operation_registry PRIMARY KEY (operation_id)
        )
    """)
    op.execute("""
        CREATE TABLE admin_authorities (
            user_id BIGINT NOT NULL, origin TEXT NOT NULL,
            granted_operation_id TEXT, granted_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT pk_admin_authorities PRIMARY KEY (user_id, origin),
            CONSTRAINT fk_admin_authority_user FOREIGN KEY (user_id)
                REFERENCES members (user_id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_admin_authority_origin CHECK (origin IN ('env','manual'))
        )
    """)
    op.execute(
        "CREATE INDEX idx_member_awards_maker_time "
        "ON member_awards (granted_by, granted_at)"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Destructive authority/operation-registry downgrade is disabled. "
        "Restore a verified backup or deploy a forward corrective migration."
    )
