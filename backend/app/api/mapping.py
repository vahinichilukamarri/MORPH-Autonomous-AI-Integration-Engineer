"""Mapping endpoints. No authentication yet (Phase 2)."""

from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.discovery import ProviderDep
from app.db import get_session
from app.db_models import Mapping, MappingRun, MappingVersion
from app.llm.base import BaseLLMProvider, LLMError, RateLimitExhausted
from app.llm.factory import create_llm_provider
from app.llm.store import CachingProvider, ResponseStore
from app.mapping.proposal import MappingType
from app.mapping.review import ReviewError, approve, override
from app.mapping.runner import MappingRunError, run_mapping
from app.mapping.store import latest_version, save_run
from app.mapping.transform import Transformation
from app.settings import Settings, get_settings

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@lru_cache
def _cached_llm() -> BaseLLMProvider:
    settings = get_settings()
    return CachingProvider(create_llm_provider(settings), ResponseStore(settings.llm_cache_dir))


def get_llm() -> BaseLLMProvider:
    """FastAPI dependency: the configured LLM provider behind the on-disk cache."""
    try:
        return _cached_llm()
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


LLMDep = Annotated[BaseLLMProvider, Depends(get_llm)]


class MappingRunRequest(BaseModel):
    source_system_version: int = Field(description="Id of the source system version.")
    target_system_version: int = Field(description="Id of the target system version.")
    source_entity: str
    target_entity: str
    mode: Literal["rag", "full_schema"] = "rag"
    requirement: str | None = Field(
        default=None,
        max_length=4000,
        description="The business requirement, written by the operator. It is shown to the model "
        "as a trusted section, separate from the untrusted spec data. Do not pass text taken "
        "from specs or from end users here.",
    )


class MappingRunOut(BaseModel):
    id: int
    source_version_id: int
    target_version_id: int
    source_entity: str
    target_entity: str
    mode: str
    provider: str
    model: str
    prompt_version: str
    confidence_version: str
    temperature: float
    requirement: str | None
    summary: dict[str, Any]
    run_reasons: list[Any]
    created_at: datetime


class MappingVersionOut(BaseModel):
    version: int
    author: str
    mapping_type: str
    source_fields: list[str]
    transformation: dict[str, Any] | None
    unresolved_reason: str | None
    rationale: str
    alternatives: list[Any]
    certainty: str
    validation_status: str
    validation_reasons: list[Any]
    outputs_preview: list[Any]
    confidence: float | None
    review_status: str
    review_reasons: list[str]
    created_at: datetime


class MappingOut(BaseModel):
    id: int
    run_id: int
    target_field: str
    versions: int
    current: MappingVersionOut


class OverrideBody(BaseModel):
    mapping_type: MappingType
    source_fields: list[str] = []
    transformation: Transformation | None = None
    unresolved_reason: str | None = None
    rationale: str = ""


class ReviewRequest(BaseModel):
    action: Literal["approve", "override"]
    mapping: OverrideBody | None = None


def _run_out(run: MappingRun) -> MappingRunOut:
    return MappingRunOut.model_validate(run, from_attributes=True)


def _version_out(v: MappingVersion) -> MappingVersionOut:
    return MappingVersionOut.model_validate(v, from_attributes=True)


def _mapping_out(session: Session, mapping: Mapping) -> MappingOut:
    return MappingOut(
        id=mapping.id,
        run_id=mapping.mapping_run_id,
        target_field=mapping.target_field,
        versions=len(mapping.versions),
        current=_version_out(latest_version(session, mapping.id)),
    )


@router.post("/mapping-runs", response_model=MappingRunOut, summary="Propose a mapping")
def create_run(
    body: MappingRunRequest,
    session: SessionDep,
    llm: LLMDep,
    embedder: ProviderDep,
    settings: SettingsDep,
) -> MappingRunOut:
    try:
        result = run_mapping(
            session,
            llm,
            embedder,
            source_version_id=body.source_system_version,
            target_version_id=body.target_system_version,
            source_entity=body.source_entity,
            target_entity=body.target_entity,
            mode=body.mode,
            requirement=body.requirement,
            samples_dir=settings.samples_dir,
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
        )
    except MappingRunError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RateLimitExhausted as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    run = save_run(session, result, settings.llm_temperature)
    session.commit()
    return _run_out(run)


def _run_or_404(session: Session, run_id: int) -> MappingRun:
    run = session.get(MappingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="mapping run not found")
    return run


@router.get("/mapping-runs/{run_id}", response_model=MappingRunOut)
def get_run(run_id: int, session: SessionDep) -> MappingRunOut:
    return _run_out(_run_or_404(session, run_id))


@router.get("/mapping-runs/{run_id}/mappings", response_model=list[MappingOut])
def list_mappings(run_id: int, session: SessionDep) -> list[MappingOut]:
    run = _run_or_404(session, run_id)
    return [_mapping_out(session, m) for m in run.mappings]


def _mapping_or_404(session: Session, mapping_id: int) -> Mapping:
    mapping = session.get(Mapping, mapping_id)
    if mapping is None:
        raise HTTPException(status_code=404, detail="mapping not found")
    return mapping


@router.get("/mappings/{mapping_id}/versions", response_model=list[MappingVersionOut])
def list_versions(mapping_id: int, session: SessionDep) -> list[MappingVersionOut]:
    return [_version_out(v) for v in _mapping_or_404(session, mapping_id).versions]


@router.post(
    "/mappings/{mapping_id}/review",
    response_model=MappingOut,
    summary="Approve a mapping or override it; either appends a new version",
)
def review_mapping(
    mapping_id: int, body: ReviewRequest, session: SessionDep, settings: SettingsDep
) -> MappingOut:
    mapping = _mapping_or_404(session, mapping_id)
    try:
        if body.action == "approve":
            approve(session, mapping)
        else:
            if body.mapping is None:
                raise HTTPException(status_code=422, detail="override needs a 'mapping' body")
            override(
                session,
                mapping,
                mapping_type=body.mapping.mapping_type,
                source_fields=body.mapping.source_fields,
                transformation=body.mapping.transformation,
                unresolved_reason=body.mapping.unresolved_reason,
                rationale=body.mapping.rationale,
                samples_dir=settings.samples_dir,
            )
    except ReviewError as exc:
        detail = {
            "message": str(exc),
            "reasons": [{"code": r.code.value, "detail": r.detail} for r in exc.reasons],
        }
        raise HTTPException(status_code=422, detail=detail) from exc
    session.commit()
    session.refresh(mapping)
    return _mapping_out(session, mapping)
