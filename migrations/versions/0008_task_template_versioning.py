"""Add immutable, versioned task templates and task provenance.

Revision ID: 0008_task_template_versioning
Revises: 0007_capability_rbac
"""

from collections.abc import Sequence
import json

from alembic import op
import sqlalchemy as sa

from db_migration.template_contract import (
    SYSTEM_TEMPLATE_SEED_AT,
    SYSTEM_TEMPLATE_SEEDS,
)


revision: str = "0008_task_template_versioning"
down_revision: str | None = "0007_capability_rbac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _canonical_json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _seed_existing_installation() -> None:
    """Backfill defaults only on an existing installation.

    A pristine Alembic target must remain row-empty for the strict offline
    SQLite importer. Fresh application databases are seeded by runtime
    bootstrap; databases with members are upgraded here.
    """

    bind = op.get_bind()
    if not bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM members)" )).scalar():
        return

    insert_template = sa.text("""
        INSERT INTO task_templates (
            id,key,origin,status,generation,current_version_id,
            created_by,created_at,updated_by,updated_at,archived_by,archived_at
        ) VALUES (
            :id,:key,'system','active',1,:version_id,
            NULL,:created_at,NULL,:created_at,NULL,NULL
        )
    """)
    insert_version = sa.text("""
        INSERT INTO task_template_versions (
            id,template_id,version_number,title,task_type,task_title,details,
            reward,mode,evidence_policy,max_participants,budget_cap,
            photo_media_id,photo_sha256,content_hash,created_by,created_at
        ) VALUES (
            :version_id,:id,1,:title,:task_type,:task_title,:details,
            :reward,:mode,:evidence_policy,:max_participants,:budget_cap,
            NULL,NULL,:content_hash,NULL,:created_at
        )
    """)
    insert_event = sa.text("""
        INSERT INTO task_template_events (
            template_id,template_version_id,event_type,generation,actor_id,
            operation_id,request_hash,note,before_json,after_json,result_json,
            created_at
        ) VALUES (
            :id,:version_id,'created',1,NULL,:operation_id,:content_hash,'',
            CAST(:before_json AS JSONB),CAST(:after_json AS JSONB),
            CAST(:result_json AS JSONB),:created_at
        )
    """)
    for seed in SYSTEM_TEMPLATE_SEEDS:
        values = dict(seed)
        values["created_at"] = SYSTEM_TEMPLATE_SEED_AT
        values["operation_id"] = f"task-template-seed:{seed['key']}:v1"
        version = {
            field: seed[field]
            for field in (
                "title", "task_type", "task_title", "details", "reward",
                "mode", "evidence_policy", "max_participants", "budget_cap",
            )
        }
        version.update({
            "photo_media_id": None,
            "photo_sha256": None,
            "id": seed["version_id"],
            "version_number": 1,
            "content_hash": seed["content_hash"],
        })
        values["before_json"] = "{}"
        values["after_json"] = _canonical_json({
            "id": seed["id"],
            "key": seed["key"],
            "origin": "system",
            "status": "active",
            "generation": 1,
            "current_version_id": seed["version_id"],
            "version": version,
        })
        values["result_json"] = _canonical_json({
            "generation": 1,
            "idempotent": False,
            "ok": True,
            "status": "active",
            "template_id": seed["id"],
            "version_id": seed["version_id"],
            "version_number": 1,
        })
        bind.execute(insert_template, values)
        bind.execute(insert_version, values)
        bind.execute(insert_event, values)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE task_templates (
            id TEXT NOT NULL,
            key TEXT NOT NULL,
            origin TEXT NOT NULL,
            status TEXT NOT NULL,
            generation INTEGER NOT NULL,
            current_version_id TEXT NOT NULL,
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL,
            updated_by BIGINT,
            updated_at TIMESTAMPTZ NOT NULL,
            archived_by BIGINT,
            archived_at TIMESTAMPTZ,
            CONSTRAINT pk_task_templates PRIMARY KEY (id),
            CONSTRAINT uq_task_templates_key UNIQUE (key),
            CONSTRAINT fk_task_templates_created_by FOREIGN KEY (created_by)
                REFERENCES members (user_id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_task_templates_updated_by FOREIGN KEY (updated_by)
                REFERENCES members (user_id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_task_templates_archived_by FOREIGN KEY (archived_by)
                REFERENCES members (user_id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_task_templates_origin
                CHECK (origin IN ('system','manual')),
            CONSTRAINT ck_task_templates_status
                CHECK (status IN ('active','archived')),
            CONSTRAINT ck_task_templates_generation CHECK (generation > 0),
            CONSTRAINT ck_task_templates_key CHECK (
                key ~ '^[a-z][a-z0-9_]{2,49}$'
            ),
            CONSTRAINT ck_task_templates_archive_state CHECK (
                (status='active' AND archived_by IS NULL AND archived_at IS NULL)
                OR (status='archived' AND archived_by IS NOT NULL AND archived_at IS NOT NULL)
            )
        )
    """)
    op.execute("""
        CREATE TABLE task_template_versions (
            id TEXT NOT NULL,
            template_id TEXT NOT NULL,
            version_number INTEGER NOT NULL,
            title TEXT NOT NULL,
            task_type TEXT NOT NULL,
            task_title TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            reward BIGINT NOT NULL,
            mode TEXT NOT NULL,
            evidence_policy TEXT NOT NULL,
            max_participants INTEGER NOT NULL,
            budget_cap BIGINT NOT NULL,
            photo_media_id TEXT,
            photo_sha256 TEXT,
            content_hash TEXT NOT NULL,
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT pk_task_template_versions PRIMARY KEY (id),
            CONSTRAINT uq_task_template_versions_number
                UNIQUE (template_id,version_number),
            CONSTRAINT uq_task_template_versions_template_id
                UNIQUE (template_id,id),
            CONSTRAINT fk_task_template_versions_template FOREIGN KEY (template_id)
                REFERENCES task_templates (id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_task_template_versions_media FOREIGN KEY (photo_media_id)
                REFERENCES media_objects (id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_task_template_versions_created_by FOREIGN KEY (created_by)
                REFERENCES members (user_id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_task_template_versions_number CHECK (version_number > 0),
            CONSTRAINT ck_task_template_versions_reward CHECK (reward BETWEEN 1 AND 300),
            CONSTRAINT ck_task_template_versions_type CHECK (task_type IN (
                'relocate','fix_zone','charge','rescue','community','referral','photo_check'
            )),
            CONSTRAINT ck_task_template_versions_mode
                CHECK (mode IN ('open','personal','all')),
            CONSTRAINT ck_task_template_versions_evidence CHECK (evidence_policy IN (
                'none','comment_only','photo_required','before_after'
            )),
            CONSTRAINT ck_task_template_versions_participants CHECK (
                (mode IN ('open','personal') AND max_participants=1 AND budget_cap=reward)
                OR
                (mode='all' AND max_participants BETWEEN 1 AND 500
                 AND budget_cap BETWEEN reward AND 150000
                 AND reward*max_participants<=budget_cap)
            ),
            CONSTRAINT ck_task_template_versions_photo_pair CHECK (
                (photo_media_id IS NULL)=(photo_sha256 IS NULL)
            ),
            CONSTRAINT ck_task_template_versions_photo_sha CHECK (
                photo_sha256 IS NULL OR photo_sha256 ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_task_template_versions_content_hash CHECK (
                content_hash ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_task_template_versions_before_after_photo CHECK (
                evidence_policy<>'before_after' OR photo_media_id IS NOT NULL
            )
        )
    """)
    op.execute("""
        ALTER TABLE task_templates ADD CONSTRAINT fk_task_templates_current_version
        FOREIGN KEY (id,current_version_id)
        REFERENCES task_template_versions (template_id,id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
    """)
    op.execute("""
        CREATE TABLE task_template_events (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY NOT NULL,
            template_id TEXT NOT NULL,
            template_version_id TEXT,
            event_type TEXT NOT NULL,
            generation INTEGER NOT NULL,
            actor_id BIGINT,
            operation_id TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            before_json JSONB NOT NULL,
            after_json JSONB NOT NULL,
            result_json JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT pk_task_template_events PRIMARY KEY (id),
            CONSTRAINT uq_task_template_events_operation UNIQUE (operation_id),
            CONSTRAINT uq_task_template_events_generation UNIQUE (template_id,generation),
            CONSTRAINT fk_task_template_events_template FOREIGN KEY (template_id)
                REFERENCES task_templates (id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_task_template_events_version
                FOREIGN KEY (template_id,template_version_id)
                REFERENCES task_template_versions (template_id,id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_task_template_events_actor FOREIGN KEY (actor_id)
                REFERENCES members (user_id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_task_template_events_type CHECK (event_type IN (
                'created','version_created','archived','activated'
            )),
            CONSTRAINT ck_task_template_events_generation CHECK (generation > 0),
            CONSTRAINT ck_task_template_events_request_hash CHECK (
                request_hash ~ '^[0-9a-f]{64}$'
            )
        )
    """)

    op.execute("ALTER TABLE tasks ADD COLUMN template_id TEXT")
    op.execute("ALTER TABLE tasks ADD COLUMN template_version_id TEXT")
    op.execute("""
        ALTER TABLE tasks ADD CONSTRAINT ck_tasks_template_provenance_pair CHECK (
            (template_id IS NULL)=(template_version_id IS NULL)
        )
    """)
    op.execute("""
        ALTER TABLE tasks ADD CONSTRAINT fk_tasks_template_version
        FOREIGN KEY (template_id,template_version_id)
        REFERENCES task_template_versions (template_id,id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
    """)

    op.execute(
        "CREATE INDEX idx_task_templates_status ON task_templates (status,id)"
    )
    op.execute(
        "CREATE INDEX idx_task_template_versions_template "
        "ON task_template_versions (template_id,version_number)"
    )
    op.execute(
        "CREATE INDEX idx_task_template_versions_media "
        "ON task_template_versions (photo_media_id)"
    )
    op.execute(
        "CREATE INDEX idx_task_template_events_template "
        "ON task_template_events (template_id,generation,id)"
    )

    op.execute("""
        CREATE FUNCTION reject_task_template_version_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'task_template_versions are immutable';
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_task_template_versions_immutable
        BEFORE UPDATE OR DELETE ON task_template_versions
        FOR EACH ROW EXECUTE FUNCTION reject_task_template_version_mutation()
    """)
    op.execute("""
        CREATE FUNCTION reject_task_template_event_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'task_template_events are immutable';
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_task_template_events_immutable
        BEFORE UPDATE OR DELETE ON task_template_events
        FOR EACH ROW EXECUTE FUNCTION reject_task_template_event_mutation()
    """)
    op.execute("""
        CREATE FUNCTION reject_task_template_key_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.key<>OLD.key THEN
                RAISE EXCEPTION 'task template key is immutable';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_task_templates_key_immutable
        BEFORE UPDATE OF key ON task_templates
        FOR EACH ROW EXECUTE FUNCTION reject_task_template_key_mutation()
    """)

    _seed_existing_installation()


def downgrade() -> None:
    raise RuntimeError(
        "Destructive task-template downgrade is disabled. Restore a verified "
        "schema-298 backup with its media manifest or deploy a forward corrective "
        "migration."
    )
