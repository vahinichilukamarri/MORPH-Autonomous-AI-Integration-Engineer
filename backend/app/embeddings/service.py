"""Embedding every field and entity of a stored system version."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import EntityEmbedding, EntityRow, FieldEmbedding, FieldRow
from app.discovery.repository import entity_from_row, field_from_row
from app.embeddings.provider import EmbeddingProvider
from app.embeddings.text import entity_text, field_text

BATCH_SIZE = 64


@dataclass(frozen=True)
class EmbedStats:
    fields_embedded: int
    entities_embedded: int


def _embed_checked(provider: EmbeddingProvider, texts: Sequence[str]) -> list[list[float]]:
    vectors = provider.embed(texts)
    if len(vectors) != len(texts) or any(len(v) != provider.dimensions for v in vectors):
        raise ValueError(f"{provider.model_name} returned vectors of the wrong shape")
    return vectors


def embed_version(session: Session, version_id: int, provider: EmbeddingProvider) -> EmbedStats:
    """Embed what is not yet embedded with this model. Safe to call repeatedly."""
    model_name = provider.model_name
    entities = session.scalars(
        select(EntityRow).where(EntityRow.system_version_id == version_id).order_by(EntityRow.id)
    ).all()

    done_fields = set(
        session.scalars(
            select(FieldEmbedding.field_id)
            .join(FieldRow, FieldRow.id == FieldEmbedding.field_id)
            .join(EntityRow, EntityRow.id == FieldRow.entity_id)
            .where(
                EntityRow.system_version_id == version_id, FieldEmbedding.model_name == model_name
            )
        )
    )
    pending_fields = [
        (row, field_text(entity.name, field_from_row(row)))
        for entity in entities
        for row in entity.fields
        if row.id not in done_fields
    ]
    for start in range(0, len(pending_fields), BATCH_SIZE):
        batch = pending_fields[start : start + BATCH_SIZE]
        vectors = _embed_checked(provider, [text for _, text in batch])
        session.add_all(
            FieldEmbedding(field_id=row.id, model_name=model_name, text=text, embedding=vector)
            for (row, text), vector in zip(batch, vectors, strict=True)
        )

    done_entities = set(
        session.scalars(
            select(EntityEmbedding.entity_id)
            .join(EntityRow, EntityRow.id == EntityEmbedding.entity_id)
            .where(
                EntityRow.system_version_id == version_id, EntityEmbedding.model_name == model_name
            )
        )
    )
    pending_entities = [
        (entity, entity_text(entity_from_row(entity)))
        for entity in entities
        if entity.id not in done_entities
    ]
    if pending_entities:
        vectors = _embed_checked(provider, [text for _, text in pending_entities])
        session.add_all(
            EntityEmbedding(entity_id=entity.id, model_name=model_name, text=text, embedding=vector)
            for (entity, text), vector in zip(pending_entities, vectors, strict=True)
        )
    session.flush()
    return EmbedStats(len(pending_fields), len(pending_entities))
