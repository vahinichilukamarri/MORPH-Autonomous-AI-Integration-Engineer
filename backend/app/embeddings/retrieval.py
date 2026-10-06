"""Nearest-field retrieval across systems with pgvector (cosine distance)."""

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.db_models import EntityRow, FieldEmbedding, FieldRow, System, SystemVersion
from app.discovery.models import EntityRole


@dataclass(frozen=True)
class SimilarField:
    field_id: int
    system_id: int
    system_name: str
    version: int
    entity_name: str
    entity_role: EntityRole
    field_path: str
    json_type: str
    distance: float  # cosine distance: 0 identical, 1 unrelated, 2 opposite


def similar_fields(
    session: Session,
    field_id: int,
    *,
    model_name: str,
    k: int = 5,
    target_system_id: int | None = None,
    target_version_id: int | None = None,
    target_entity: str | None = None,
    roles: Collection[EntityRole] | None = None,
) -> list[SimilarField]:
    """The k nearest fields of *other* systems, compared only within one embedding model.

    By default candidates come from the latest version of each other system;
    ``target_version_id`` pins an exact version.
    """
    source = session.execute(
        select(FieldEmbedding.embedding, System.id)
        .join(FieldRow, FieldRow.id == FieldEmbedding.field_id)
        .join(EntityRow, EntityRow.id == FieldRow.entity_id)
        .join(SystemVersion, SystemVersion.id == EntityRow.system_version_id)
        .join(System, System.id == SystemVersion.system_id)
        .where(FieldEmbedding.field_id == field_id, FieldEmbedding.model_name == model_name)
    ).one_or_none()
    if source is None:
        raise LookupError(f"field {field_id} has no embedding for model {model_name!r}")
    vector, source_system_id = source

    newer = aliased(SystemVersion)
    distance = FieldEmbedding.embedding.cosine_distance(vector).label("distance")
    query = (
        select(FieldRow, EntityRow, SystemVersion, System, distance)
        .join(FieldEmbedding, FieldEmbedding.field_id == FieldRow.id)
        .join(EntityRow, EntityRow.id == FieldRow.entity_id)
        .join(SystemVersion, SystemVersion.id == EntityRow.system_version_id)
        .join(System, System.id == SystemVersion.system_id)
        .where(FieldEmbedding.model_name == model_name, System.id != source_system_id)
        .order_by(distance, FieldRow.id)
        .limit(k)
    )
    if target_version_id is not None:
        query = query.where(SystemVersion.id == target_version_id)
    else:
        query = query.where(
            ~exists().where(
                newer.system_id == SystemVersion.system_id, newer.version > SystemVersion.version
            )
        )
    if target_system_id is not None:
        query = query.where(System.id == target_system_id)
    if target_entity is not None:
        query = query.where(EntityRow.name == target_entity)
    if roles is not None:
        query = query.where(EntityRow.role.in_([r.value for r in roles]))

    return [
        SimilarField(
            field_id=field.id,
            system_id=system.id,
            system_name=system.name,
            version=version.version,
            entity_name=entity.name,
            entity_role=EntityRole(entity.role),
            field_path=field.path,
            json_type=field.json_type,
            distance=float(dist),
        )
        for field, entity, version, system, dist in session.execute(query).all()
    ]
