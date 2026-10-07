"""integrations, versions, files, gate results and sandbox runs

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _created_at() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _version_fk() -> sa.Column[int]:
    return sa.Column(
        "integration_version_id",
        sa.Integer(),
        sa.ForeignKey("integration_versions.id"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "integrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mapping_run_id", sa.Integer(), sa.ForeignKey("mapping_runs.id"), nullable=False),
        sa.Column("condition", sa.String(8), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        _created_at(),
        sa.UniqueConstraint("mapping_run_id", "condition"),
    )
    op.create_index("ix_integrations_mapping_run_id", "integrations", ["mapping_run_id"])
    op.create_table(
        "integration_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("integration_id", sa.Integer(), sa.ForeignKey("integrations.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("bundle_hash", sa.String(64)),
        sa.Column("generator_version", sa.String(16), nullable=False),
        sa.Column("runtime_version", sa.String(16), nullable=False),
        sa.Column("mapping_version_ids", postgresql.JSONB(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        _created_at(),
        sa.UniqueConstraint("integration_id", "version"),
    )
    op.create_index(
        "ix_integration_versions_integration_id", "integration_versions", ["integration_id"]
    )
    op.create_table(
        "integration_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        _version_fk(),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.UniqueConstraint("integration_version_id", "path"),
    )
    op.create_index(
        "ix_integration_files_integration_version_id",
        "integration_files",
        ["integration_version_id"],
    )
    op.create_table(
        "gate_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        _version_fk(),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("findings", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_gate_results_integration_version_id", "gate_results", ["integration_version_id"]
    )
    op.create_table(
        "sandbox_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        _version_fk(),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("limits", postgresql.JSONB(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("duration_s", sa.Float(), nullable=False),
        sa.Column("stdout_excerpt", sa.Text(), nullable=False),
        sa.Column("stderr_excerpt", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        _created_at(),
    )
    op.create_index(
        "ix_sandbox_runs_integration_version_id", "sandbox_runs", ["integration_version_id"]
    )
    op.add_column(
        "llm_calls",
        sa.Column("integration_version_id", sa.Integer(), sa.ForeignKey("integration_versions.id")),
    )
    op.create_index("ix_llm_calls_integration_version_id", "llm_calls", ["integration_version_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_calls_integration_version_id", table_name="llm_calls")
    op.drop_column("llm_calls", "integration_version_id")
    for table in (
        "sandbox_runs",
        "gate_results",
        "integration_files",
        "integration_versions",
        "integrations",
    ):
        op.drop_table(table)
