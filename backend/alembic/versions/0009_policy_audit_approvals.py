"""policy versions, the append-only audit log, approvals and system policy attributes

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

FUNCTION = """
CREATE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (% refused)', TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "policy_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column(
            "loaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "audit_events",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chain", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("tool", sa.String(64)),
        sa.Column("principal", sa.String(64), nullable=False),
        sa.Column("call_id", sa.String(32)),
        sa.Column("policy_hash", sa.String(64)),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("row_hash", sa.String(64), nullable=False, unique=True),
    )
    op.create_index("ix_audit_events_chain_seq", "audit_events", ["chain", "seq"])
    op.create_index("ix_audit_events_session_id", "audit_events", ["session_id"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index("ix_audit_events_call_id", "audit_events", ["call_id"])
    op.execute(FUNCTION)
    op.execute(
        "CREATE TRIGGER audit_events_no_change BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()"
    )
    op.execute(
        "CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only()"
    )
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(64)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text()),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_approval_requests_request_hash", "approval_requests", ["request_hash"])
    op.create_index("ix_approval_requests_status", "approval_requests", ["status"])
    op.create_table(
        "system_policy_attributes",
        sa.Column("system_id", sa.Integer(), sa.ForeignKey("systems.id"), primary_key=True),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("data_class", sa.String(16), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("system_policy_attributes")
    op.drop_table("approval_requests")
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_truncate ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_change ON audit_events")
    op.drop_table("audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_append_only()")
    op.drop_table("policy_versions")
