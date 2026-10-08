"""repair runs and attempts

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _now(name: str) -> sa.Column[sa.DateTime]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "repair_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mapping_run_id", sa.Integer(), sa.ForeignKey("mapping_runs.id"), nullable=False),
        sa.Column("condition", sa.String(8), nullable=False),
        sa.Column("start_mode", sa.String(8), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("terminal_reason", sa.Text()),
        sa.Column("pauses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("size_policy", postgresql.JSONB(), nullable=False),
        _now("created_at"),
        _now("updated_at"),
    )
    op.create_index("ix_repair_runs_mapping_run_id", "repair_runs", ["mapping_run_id"])
    op.create_table(
        "repair_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("repair_run_id", sa.Integer(), sa.ForeignKey("repair_runs.id"), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("output_text", sa.Text(), nullable=False),
        sa.Column("output_hash", sa.String(64), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("finish_reason", sa.String(32)),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("usage", postgresql.JSONB()),
        sa.Column("size_estimate", postgresql.JSONB()),
        sa.Column("failed_stage", sa.String(16)),
        sa.Column("guard_result", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("feedback", postgresql.JSONB()),
        sa.Column("integration_version_id", sa.Integer(), sa.ForeignKey("integration_versions.id")),
        sa.Column("llm_call_id", sa.Integer(), sa.ForeignKey("llm_calls.id")),
        _now("created_at"),
        sa.UniqueConstraint("repair_run_id", "attempt"),
    )
    op.create_index("ix_repair_attempts_repair_run_id", "repair_attempts", ["repair_run_id"])
    op.create_index(
        "ix_repair_attempts_integration_version_id", "repair_attempts", ["integration_version_id"]
    )


def downgrade() -> None:
    op.drop_table("repair_attempts")
    op.drop_table("repair_runs")
