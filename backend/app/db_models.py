"""SQLAlchemy tables for discovered systems, their versions and field embeddings.

Every ingested spec version owns its own entities, fields and operations, so older versions
are never modified when a newer spec arrives.
"""

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIMENSIONS = 384


class Base(DeclarativeBase):
    pass


class System(Base):
    __tablename__ = "systems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    versions: Mapped[list["SystemVersion"]] = relationship(
        back_populates="system", order_by="SystemVersion.version"
    )


class SystemVersion(Base):
    __tablename__ = "system_versions"
    __table_args__ = (
        UniqueConstraint("system_id", "version"),
        UniqueConstraint("system_id", "spec_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    spec_hash: Mapped[str] = mapped_column(String(64))
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    api_title: Mapped[str] = mapped_column(Text)
    api_version: Mapped[str] = mapped_column(Text)
    auth_schemes: Mapped[list[Any]] = mapped_column(JSONB)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    system: Mapped[System] = relationship(back_populates="versions")
    entities: Mapped[list["EntityRow"]] = relationship(
        back_populates="version", order_by="EntityRow.position"
    )
    operations: Mapped[list["OperationRow"]] = relationship(
        back_populates="version", order_by="OperationRow.position"
    )


class EntityRow(Base):
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_version_id: Mapped[int] = mapped_column(ForeignKey("system_versions.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(Text)
    schema_name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16))
    wrapped_entity: Mapped[str | None] = mapped_column(Text)

    version: Mapped[SystemVersion] = relationship(back_populates="entities")
    fields: Mapped[list["FieldRow"]] = relationship(
        back_populates="entity", order_by="FieldRow.position"
    )


class FieldRow(Base):
    __tablename__ = "fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    json_type: Mapped[str] = mapped_column(String(64))
    format: Mapped[str | None] = mapped_column(Text)
    nullable: Mapped[bool] = mapped_column(Boolean)
    required: Mapped[bool] = mapped_column(Boolean)
    enum_values: Mapped[list[Any]] = mapped_column(JSONB)
    description: Mapped[str | None] = mapped_column(Text)
    examples: Mapped[list[Any]] = mapped_column(JSONB)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSONB)
    item_type: Mapped[str | None] = mapped_column(String(64))
    entity_ref: Mapped[str | None] = mapped_column(Text)

    entity: Mapped[EntityRow] = relationship(back_populates="fields")


class OperationRow(Base):
    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_version_id: Mapped[int] = mapped_column(ForeignKey("system_versions.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(Text)
    operation_id: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    parameters: Mapped[list[Any]] = mapped_column(JSONB)
    request_entity: Mapped[str | None] = mapped_column(Text)
    responses: Mapped[list[Any]] = mapped_column(JSONB)
    auth_scheme: Mapped[str | None] = mapped_column(Text)
    status_codes: Mapped[list[Any]] = mapped_column(JSONB)

    version: Mapped[SystemVersion] = relationship(back_populates="operations")


class FieldEmbedding(Base):
    __tablename__ = "field_embeddings"
    __table_args__ = (
        UniqueConstraint("field_id", "model_name"),
        Index(
            "ix_field_embeddings_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))


class EntityEmbedding(Base):
    __tablename__ = "entity_embeddings"
    __table_args__ = (UniqueConstraint("entity_id", "model_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))


class MappingRun(Base):
    __tablename__ = "mapping_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_version_id: Mapped[int] = mapped_column(ForeignKey("system_versions.id"), index=True)
    target_version_id: Mapped[int] = mapped_column(ForeignKey("system_versions.id"), index=True)
    source_entity: Mapped[str] = mapped_column(Text)
    target_entity: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(String(16))
    confidence_version: Mapped[str] = mapped_column(String(32))
    temperature: Mapped[float] = mapped_column(Float)
    requirement: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    run_reasons: Mapped[list[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    mappings: Mapped[list["Mapping"]] = relationship(
        back_populates="run", order_by="Mapping.position"
    )


class Mapping(Base):
    __tablename__ = "mappings"
    __table_args__ = (UniqueConstraint("mapping_run_id", "target_field"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mapping_run_id: Mapped[int] = mapped_column(ForeignKey("mapping_runs.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    target_field: Mapped[str] = mapped_column(Text)

    run: Mapped[MappingRun] = relationship(back_populates="mappings")
    versions: Mapped[list["MappingVersion"]] = relationship(
        back_populates="mapping", order_by="MappingVersion.version"
    )


class MappingVersion(Base):
    """Version 1 is the proposal made by the system; approvals and overrides append versions."""

    __tablename__ = "mapping_versions"
    __table_args__ = (UniqueConstraint("mapping_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("mappings.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    author: Mapped[str] = mapped_column(String(16))
    mapping_type: Mapped[str] = mapped_column(String(16))
    source_fields: Mapped[list[Any]] = mapped_column(JSONB)
    transformation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    unresolved_reason: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    alternatives: Mapped[list[Any]] = mapped_column(JSONB)
    certainty: Mapped[str] = mapped_column(String(8))
    validation_status: Mapped[str] = mapped_column(String(8))
    validation_reasons: Mapped[list[Any]] = mapped_column(JSONB)
    outputs_preview: Mapped[list[Any]] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    review_status: Mapped[str] = mapped_column(String(16))
    review_reasons: Mapped[list[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    mapping: Mapped[Mapping] = relationship(back_populates="versions")


class LLMCall(Base):
    """Metadata of one provider call. No prompt text and no secrets."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mapping_run_id: Mapped[int | None] = mapped_column(ForeignKey("mapping_runs.id"), index=True)
    integration_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("integration_versions.id"), index=True
    )
    target_field: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(Text)
    prompt_hash: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16))
    http_attempts: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Integration(Base):
    """One integration per mapping run and condition; input changes append versions."""

    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("mapping_run_id", "condition"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mapping_run_id: Mapped[int] = mapped_column(ForeignKey("mapping_runs.id"), index=True)
    condition: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    versions: Mapped[list["IntegrationVersion"]] = relationship(
        back_populates="integration", order_by="IntegrationVersion.version"
    )


class IntegrationVersion(Base):
    __tablename__ = "integration_versions"
    __table_args__ = (UniqueConstraint("integration_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    integration_id: Mapped[int] = mapped_column(ForeignKey("integrations.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    input_hash: Mapped[str] = mapped_column(String(64))
    bundle_hash: Mapped[str | None] = mapped_column(String(64))
    generator_version: Mapped[str] = mapped_column(String(16))
    runtime_version: Mapped[str] = mapped_column(String(16))
    mapping_version_ids: Mapped[list[Any]] = mapped_column(JSONB)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    integration: Mapped[Integration] = relationship(back_populates="versions")
    files: Mapped[list["IntegrationFile"]] = relationship(
        back_populates="version", order_by="IntegrationFile.path"
    )
    gate_results: Mapped[list["GateResultRow"]] = relationship(
        back_populates="version", order_by="GateResultRow.id"
    )
    sandbox_runs: Mapped[list["SandboxRunRow"]] = relationship(
        back_populates="version", order_by="SandboxRunRow.id"
    )


class IntegrationFile(Base):
    __tablename__ = "integration_files"
    __table_args__ = (UniqueConstraint("integration_version_id", "path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    integration_version_id: Mapped[int] = mapped_column(
        ForeignKey("integration_versions.id"), index=True
    )
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)

    version: Mapped[IntegrationVersion] = relationship(back_populates="files")


class GateResultRow(Base):
    __tablename__ = "gate_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    integration_version_id: Mapped[int] = mapped_column(
        ForeignKey("integration_versions.id"), index=True
    )
    stage: Mapped[str] = mapped_column(String(16))
    passed: Mapped[bool] = mapped_column(Boolean)
    findings: Mapped[list[Any]] = mapped_column(JSONB)

    version: Mapped[IntegrationVersion] = relationship(back_populates="gate_results")


class SandboxRunRow(Base):
    __tablename__ = "sandbox_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    integration_version_id: Mapped[int] = mapped_column(
        ForeignKey("integration_versions.id"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(24))
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB)
    outcome: Mapped[str] = mapped_column(String(16))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[float] = mapped_column(Float)
    stdout_excerpt: Mapped[str] = mapped_column(Text)
    stderr_excerpt: Mapped[str] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    version: Mapped[IntegrationVersion] = relationship(back_populates="sandbox_runs")
