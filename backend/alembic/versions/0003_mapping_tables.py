"""mapping runs, mappings, mapping versions and LLM call metadata

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07
"""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _created_at() -> Any:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "mapping_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_version_id", sa.Integer(), sa.ForeignKey("system_versions.id"), nullable=False
        ),
        sa.Column(
            "target_version_id", sa.Integer(), sa.ForeignKey("system_versions.id"), nullable=False
        ),
        sa.Column("source_entity", sa.Text(), nullable=False),
        sa.Column("target_entity", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.String(16), nullable=False),
        sa.Column("confidence_version", sa.String(32), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("run_reasons", postgresql.JSONB(), nullable=False),
        _created_at(),
    )
    op.create_index("ix_mapping_runs_source_version_id", "mapping_runs", ["source_version_id"])
    op.create_index("ix_mapping_runs_target_version_id", "mapping_runs", ["target_version_id"])
    op.create_table(
        "mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mapping_run_id", sa.Integer(), sa.ForeignKey("mapping_runs.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("target_field", sa.Text(), nullable=False),
        sa.UniqueConstraint("mapping_run_id", "target_field"),
    )
    op.create_index("ix_mappings_mapping_run_id", "mappings", ["mapping_run_id"])
    op.create_table(
        "mapping_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mapping_id", sa.Integer(), sa.ForeignKey("mappings.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("author", sa.String(16), nullable=False),
        sa.Column("mapping_type", sa.String(16), nullable=False),
        sa.Column("source_fields", postgresql.JSONB(), nullable=False),
        sa.Column("transformation", postgresql.JSONB()),
        sa.Column("unresolved_reason", sa.Text()),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("alternatives", postgresql.JSONB(), nullable=False),
        sa.Column("certainty", sa.String(8), nullable=False),
        sa.Column("validation_status", sa.String(8), nullable=False),
        sa.Column("validation_reasons", postgresql.JSONB(), nullable=False),
        sa.Column("outputs_preview", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.Column("review_status", sa.String(16), nullable=False),
        sa.Column("review_reasons", postgresql.JSONB(), nullable=False),
        _created_at(),
        sa.UniqueConstraint("mapping_id", "version"),
    )
    op.create_index("ix_mapping_versions_mapping_id", "mapping_versions", ["mapping_id"])
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mapping_run_id", sa.Integer(), sa.ForeignKey("mapping_runs.id")),
        sa.Column("target_field", sa.Text()),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("reasoning_tokens", sa.Integer()),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("http_attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        _created_at(),
    )
    op.create_index("ix_llm_calls_mapping_run_id", "llm_calls", ["mapping_run_id"])


def downgrade() -> None:
    for table in ("llm_calls", "mapping_versions", "mappings", "mapping_runs"):
        op.drop_table(table)
