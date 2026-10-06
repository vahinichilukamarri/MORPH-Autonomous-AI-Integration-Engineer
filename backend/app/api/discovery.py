"""Discovery endpoints. No authentication yet (Phase 2)."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import schemas
from app.db import get_session
from app.db_models import (
    EntityRow,
    FieldRow,
    OperationRow,
    System,
    SystemVersion,
)
from app.discovery.errors import ParseError
from app.discovery.models import AuthScheme
from app.discovery.parser import parse_spec
from app.discovery.repository import get_version, ingest, latest_version
from app.discovery.source import load_spec
from app.embeddings.provider import (
    EmbeddingProvider,
    configured_model_name,
    create_embedding_provider,
)
from app.embeddings.retrieval import similar_fields
from app.embeddings.service import embed_version
from app.settings import Settings, get_settings

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@lru_cache
def _cached_provider() -> EmbeddingProvider:
    return create_embedding_provider(get_settings())


def get_provider() -> EmbeddingProvider:
    """FastAPI dependency: the configured embedding provider (the model loads once)."""
    return _cached_provider()


ProviderDep = Annotated[EmbeddingProvider, Depends(get_provider)]


def _resolve_file(raw: str, root: Path) -> Path:
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise HTTPException(status_code=400, detail=f"file must be under {root}")
    return resolved


@router.post(
    "/systems/ingest",
    response_model=schemas.IngestResponse,
    responses={422: {"model": schemas.InvalidSpecResponse}},
    summary="Ingest an OpenAPI spec as a new or updated system",
)
def ingest_system(
    body: schemas.IngestRequest,
    session: SessionDep,
    provider: ProviderDep,
    settings: SettingsDep,
) -> schemas.IngestResponse | JSONResponse:
    source = body.source
    location = (
        source.url if source.url else str(_resolve_file(source.file or "", settings.spec_root))
    )
    try:
        spec = load_spec(location)
        model = parse_spec(spec, body.name)
    except ParseError as exc:
        problems = [schemas.ProblemOut(pointer=p.pointer, message=p.message) for p in exc.problems]
        content = schemas.InvalidSpecResponse(problems=problems)
        return JSONResponse(status_code=422, content=content.model_dump())

    result = ingest(session, model, spec)
    stats = embed_version(session, result.version_id, provider)
    session.commit()
    return schemas.IngestResponse(
        system_id=result.system_id,
        name=body.name,
        version=result.version,
        version_id=result.version_id,
        created=result.created,
        spec_hash=model.spec_hash,
        entities=len(model.entities),
        fields=sum(len(e.fields) for e in model.entities),
        fields_embedded=stats.fields_embedded,
        entities_embedded=stats.entities_embedded,
        embedding_model=provider.model_name,
    )


@router.get("/systems", response_model=list[schemas.SystemSummary])
def list_systems(session: SessionDep) -> list[schemas.SystemSummary]:
    summaries: list[schemas.SystemSummary] = []
    for system in session.scalars(select(System).order_by(System.name)):
        latest = latest_version(session, system.id)
        summaries.append(
            schemas.SystemSummary(
                id=system.id,
                name=system.name,
                latest_version=latest.version if latest else None,
                api_title=latest.api_title if latest else None,
                api_version=latest.api_version if latest else None,
            )
        )
    return summaries


def _system_or_404(session: Session, system_id: int) -> System:
    system = session.get(System, system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="system not found")
    return system


@router.get("/systems/{system_id}", response_model=schemas.SystemDetail)
def get_system(system_id: int, session: SessionDep) -> schemas.SystemDetail:
    system = _system_or_404(session, system_id)
    latest = latest_version(session, system.id)
    if latest is None:
        raise HTTPException(status_code=404, detail="system has no versions")
    entities = session.scalar(
        select(func.count()).select_from(EntityRow).where(EntityRow.system_version_id == latest.id)
    )
    fields = session.scalar(
        select(func.count())
        .select_from(FieldRow)
        .join(EntityRow, EntityRow.id == FieldRow.entity_id)
        .where(EntityRow.system_version_id == latest.id)
    )
    operations = session.scalar(
        select(func.count())
        .select_from(OperationRow)
        .where(OperationRow.system_version_id == latest.id)
    )
    return schemas.SystemDetail(
        id=system.id,
        name=system.name,
        latest_version=latest.version,
        api_title=latest.api_title,
        api_version=latest.api_version,
        version_id=latest.id,
        spec_hash=latest.spec_hash,
        ingested_at=latest.ingested_at,
        entities=entities or 0,
        fields=fields or 0,
        operations=operations or 0,
        auth_schemes=[AuthScheme.model_validate(a) for a in latest.auth_schemes],
    )


@router.get("/systems/{system_id}/versions", response_model=list[schemas.VersionSummary])
def list_versions(system_id: int, session: SessionDep) -> list[schemas.VersionSummary]:
    system = _system_or_404(session, system_id)
    versions = session.scalars(
        select(SystemVersion)
        .where(SystemVersion.system_id == system.id)
        .order_by(SystemVersion.version)
    ).all()
    return [
        schemas.VersionSummary(
            version=v.version,
            version_id=v.id,
            api_title=v.api_title,
            api_version=v.api_version,
            spec_hash=v.spec_hash,
            ingested_at=v.ingested_at,
            entities=len(v.entities),
        )
        for v in versions
    ]


@router.get("/systems/{system_id}/entities", response_model=list[schemas.EntityOut])
def list_entities(
    system_id: int,
    session: SessionDep,
    version: Annotated[int | None, Query(ge=1, description="Default: the latest version.")] = None,
) -> list[schemas.EntityOut]:
    system = _system_or_404(session, system_id)
    chosen = (
        get_version(session, system.id, version) if version else latest_version(session, system.id)
    )
    if chosen is None:
        raise HTTPException(status_code=404, detail="version not found")
    return [
        schemas.EntityOut(
            id=e.id,
            name=e.name,
            role=e.role,
            description=e.description,
            wrapped_entity=e.wrapped_entity,
            fields=[
                schemas.FieldOut(
                    id=f.id,
                    path=f.path,
                    json_type=f.json_type,
                    format=f.format,
                    nullable=f.nullable,
                    required=f.required,
                    enum_values=f.enum_values,
                    description=f.description,
                    examples=f.examples,
                    constraints=f.constraints,
                    item_type=f.item_type,
                    entity_ref=f.entity_ref,
                )
                for f in e.fields
            ],
        )
        for e in chosen.entities
    ]


@router.get("/fields/{field_id}/similar", response_model=list[schemas.SimilarFieldOut])
def similar(
    field_id: int,
    session: SessionDep,
    settings: SettingsDep,
    k: Annotated[int, Query(ge=1, le=50)] = 5,
    target_system: Annotated[int | None, Query(description="Restrict to this system id.")] = None,
    target_entity: Annotated[str | None, Query(description="Restrict to this entity.")] = None,
) -> list[schemas.SimilarFieldOut]:
    if session.get(FieldRow, field_id) is None:
        raise HTTPException(status_code=404, detail="field not found")
    try:
        results = similar_fields(
            session,
            field_id,
            model_name=configured_model_name(settings),
            k=k,
            target_system_id=target_system,
            target_entity=target_entity,
        )
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return [
        schemas.SimilarFieldOut(
            field_id=r.field_id,
            system_id=r.system_id,
            system_name=r.system_name,
            version=r.version,
            entity=r.entity_name,
            entity_role=r.entity_role,
            path=r.field_path,
            json_type=r.json_type,
            distance=r.distance,
        )
        for r in results
    ]
