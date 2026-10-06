"""Persisting discovered systems with versioning.

Rules: ingesting a spec whose hash is already stored for the system returns the existing
version and changes nothing; a changed spec becomes version N+1 and older versions stay intact.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db_models import EntityRow, FieldRow, OperationRow, System, SystemVersion
from app.discovery.models import (
    AuthScheme,
    Constraints,
    Entity,
    EntityRole,
    Operation,
    SystemModel,
)
from app.discovery.models import Field as ModelField


@dataclass(frozen=True)
class IngestResult:
    system_id: int
    version_id: int
    version: int
    created: bool  # False when the spec was already stored (no-op)


def ingest(session: Session, model: SystemModel, spec: dict[str, Any]) -> IngestResult:
    system = session.scalar(select(System).where(System.name == model.name).with_for_update())
    if system is None:
        system = System(name=model.name)
        session.add(system)
        session.flush()

    existing = session.scalar(
        select(SystemVersion).where(
            SystemVersion.system_id == system.id, SystemVersion.spec_hash == model.spec_hash
        )
    )
    if existing is not None:
        return IngestResult(system.id, existing.id, existing.version, created=False)

    latest = session.scalar(
        select(func.max(SystemVersion.version)).where(SystemVersion.system_id == system.id)
    )
    version = SystemVersion(
        system_id=system.id,
        version=(latest or 0) + 1,
        spec_hash=model.spec_hash,
        spec_json=spec,
        api_title=model.api_title,
        api_version=model.api_version,
        auth_schemes=[a.model_dump(mode="json") for a in model.auth_schemes],
    )
    session.add(version)
    session.flush()

    for position, entity in enumerate(model.entities):
        row = EntityRow(
            system_version_id=version.id,
            position=position,
            name=entity.name,
            schema_name=entity.schema_name,
            description=entity.description,
            role=entity.role.value,
            wrapped_entity=entity.wrapped_entity,
        )
        session.add(row)
        session.flush()
        session.add_all(
            FieldRow(
                entity_id=row.id,
                position=index,
                name=f.name,
                path=f.path,
                json_type=f.json_type,
                format=f.format,
                nullable=f.nullable,
                required=f.required,
                enum_values=list(f.enum_values),
                description=f.description,
                examples=list(f.examples),
                constraints=f.constraints.model_dump(mode="json"),
                item_type=f.item_type,
                entity_ref=f.entity_ref,
            )
            for index, f in enumerate(entity.fields)
        )
    session.add_all(
        OperationRow(
            system_version_id=version.id,
            position=position,
            method=op.method,
            path=op.path,
            operation_id=op.operation_id,
            summary=op.summary,
            parameters=[p.model_dump(mode="json") for p in op.parameters],
            request_entity=op.request_entity,
            responses=[r.model_dump(mode="json") for r in op.responses],
            auth_scheme=op.auth_scheme,
            status_codes=list(op.status_codes),
        )
        for position, op in enumerate(model.operations)
    )
    session.flush()
    return IngestResult(system.id, version.id, version.version, created=True)


def latest_version(session: Session, system_id: int) -> SystemVersion | None:
    return session.scalar(
        select(SystemVersion)
        .where(SystemVersion.system_id == system_id)
        .order_by(SystemVersion.version.desc())
        .limit(1)
    )


def get_version(session: Session, system_id: int, version: int) -> SystemVersion | None:
    return session.scalar(
        select(SystemVersion).where(
            SystemVersion.system_id == system_id, SystemVersion.version == version
        )
    )


def load_model(session: Session, version_id: int) -> SystemModel:
    """Rebuild the SystemModel of a stored version, exactly as it was ingested."""
    version = session.get(SystemVersion, version_id)
    if version is None:
        raise LookupError(f"no system version {version_id}")
    entities = tuple(
        Entity(
            name=e.name,
            schema_name=e.schema_name,
            description=e.description,
            role=EntityRole(e.role),
            wrapped_entity=e.wrapped_entity,
            fields=tuple(
                ModelField(
                    name=f.name,
                    path=f.path,
                    json_type=f.json_type,
                    format=f.format,
                    nullable=f.nullable,
                    required=f.required,
                    enum_values=tuple(f.enum_values),
                    description=f.description,
                    examples=tuple(f.examples),
                    constraints=Constraints.model_validate(f.constraints),
                    item_type=f.item_type,
                    entity_ref=f.entity_ref,
                )
                for f in e.fields
            ),
        )
        for e in version.entities
    )
    operations = tuple(
        Operation.model_validate(
            {
                "method": o.method,
                "path": o.path,
                "operation_id": o.operation_id,
                "summary": o.summary,
                "parameters": o.parameters,
                "request_entity": o.request_entity,
                "responses": o.responses,
                "auth_scheme": o.auth_scheme,
                "status_codes": o.status_codes,
            }
        )
        for o in version.operations
    )
    return SystemModel(
        name=version.system.name,
        api_title=version.api_title,
        api_version=version.api_version,
        spec_hash=version.spec_hash,
        auth_schemes=tuple(AuthScheme.model_validate(a) for a in version.auth_schemes),
        entities=entities,
        operations=operations,
    )
