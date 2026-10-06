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
