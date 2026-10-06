"""discovery tables: systems, versions, entities, fields, operations, embeddings

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DIMENSIONS = 384


def upgrade() -> None:
    op.create_table(
        "systems",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "system_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("system_id", sa.Integer(), sa.ForeignKey("systems.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec_hash", sa.String(64), nullable=False),
        sa.Column("spec_json", postgresql.JSONB(), nullable=False),
        sa.Column("api_title", sa.Text(), nullable=False),
        sa.Column("api_version", sa.Text(), nullable=False),
        sa.Column("auth_schemes", postgresql.JSONB(), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("system_id", "version"),
        sa.UniqueConstraint("system_id", "spec_hash"),
    )
    op.create_index("ix_system_versions_system_id", "system_versions", ["system_id"])
    op.create_table(
        "entities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "system_version_id", sa.Integer(), sa.ForeignKey("system_versions.id"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("schema_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("wrapped_entity", sa.Text()),
    )
    op.create_index("ix_entities_system_version_id", "entities", ["system_version_id"])
    op.create_table(
        "fields",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_id", sa.Integer(), sa.ForeignKey("entities.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("json_type", sa.String(64), nullable=False),
        sa.Column("format", sa.Text()),
        sa.Column("nullable", sa.Boolean(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("enum_values", postgresql.JSONB(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("examples", postgresql.JSONB(), nullable=False),
        sa.Column("constraints", postgresql.JSONB(), nullable=False),
        sa.Column("item_type", sa.String(64)),
        sa.Column("entity_ref", sa.Text()),
    )
    op.create_index("ix_fields_entity_id", "fields", ["entity_id"])
    op.create_table(
        "operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "system_version_id", sa.Integer(), sa.ForeignKey("system_versions.id"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("request_entity", sa.Text()),
        sa.Column("responses", postgresql.JSONB(), nullable=False),
        sa.Column("auth_scheme", sa.Text()),
        sa.Column("status_codes", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_operations_system_version_id", "operations", ["system_version_id"])
    op.create_table(
        "field_embeddings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("field_id", sa.Integer(), sa.ForeignKey("fields.id"), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(DIMENSIONS), nullable=False),
        sa.UniqueConstraint("field_id", "model_name"),
    )
    op.create_index("ix_field_embeddings_field_id", "field_embeddings", ["field_id"])
    op.create_index(
        "ix_field_embeddings_embedding",
        "field_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_table(
        "entity_embeddings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_id", sa.Integer(), sa.ForeignKey("entities.id"), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(DIMENSIONS), nullable=False),
        sa.UniqueConstraint("entity_id", "model_name"),
    )
    op.create_index("ix_entity_embeddings_entity_id", "entity_embeddings", ["entity_id"])


def downgrade() -> None:
    for table in (
        "entity_embeddings",
        "field_embeddings",
        "operations",
        "fields",
        "entities",
        "system_versions",
        "systems",
    ):
        op.drop_table(table)
