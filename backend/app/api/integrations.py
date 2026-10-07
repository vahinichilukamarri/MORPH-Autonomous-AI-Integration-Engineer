"""Integration endpoints: generate, inspect and (later) test integrations. No authentication yet."""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.codegen.inputs import InputError
from app.codegen.sandbox import SandboxError, SandboxRunner
from app.codegen.service import (
    CONDITIONS,
    GenerationError,
    generate,
    run_generated_tests,
)
from app.db import get_session
from app.db_models import Integration, IntegrationVersion
from app.settings import Settings, get_settings

router = APIRouter(prefix="/integrations", tags=["integrations"])
SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_runner() -> SandboxRunner:
    """FastAPI dependency: the sandbox runner (needs Docker and the sandbox image)."""
    return SandboxRunner()


RunnerDep = Annotated[SandboxRunner, Depends(get_runner)]


class GenerateRequest(BaseModel):
    mapping_run_id: int
    condition: Literal["D"] = Field(default="D", description=f"One of {CONDITIONS}.")
    allow_partial: bool = Field(
        default=False,
        description="Compile only the accepted fields when optional fields are not accepted yet.",
    )


class VersionOut(BaseModel):
    id: int
    integration_id: int
    version: int
    status: str
    bundle_hash: str | None
    input_hash: str
    generator_version: str
    runtime_version: str
    mapping_version_ids: list[Any]
    review: dict[str, Any] | None
    plan_error: str | None
    created_at: datetime


class IntegrationOut(BaseModel):
    id: int
    mapping_run_id: int
    condition: str
    name: str
    versions: list[int]


class FileOut(BaseModel):
    path: str
    sha256: str
    kind: str
    content: str


class GateOut(BaseModel):
    stage: str
    passed: bool
    findings: list[Any]


class SandboxRunOut(BaseModel):
    id: int
    purpose: str
    outcome: str
    exit_code: int | None
    duration_s: float
    result: dict[str, Any] | None


def version_out(version: IntegrationVersion) -> VersionOut:
    manifest = version.manifest
    return VersionOut(
        id=version.id,
        integration_id=version.integration_id,
        version=version.version,
        status=version.status,
        bundle_hash=version.bundle_hash,
        input_hash=version.input_hash,
        generator_version=version.generator_version,
        runtime_version=version.runtime_version,
        mapping_version_ids=version.mapping_version_ids,
        review=manifest.get("review"),
        plan_error=manifest.get("plan_error"),
        created_at=version.created_at,
    )


def _integration(session: Session, integration_id: int) -> Integration:
    integration = session.get(Integration, integration_id)
    if integration is None:
        raise HTTPException(404, f"integration {integration_id} not found")
    return integration


def _version(session: Session, integration_id: int, number: int) -> IntegrationVersion:
    found = session.scalar(
        select(IntegrationVersion).where(
            IntegrationVersion.integration_id == integration_id,
            IntegrationVersion.version == number,
        )
    )
    if found is None:
        raise HTTPException(404, f"integration {integration_id} has no version {number}")
    return found


@router.post("", response_model=VersionOut, status_code=201)
def create_integration(
    body: GenerateRequest, session: SessionDep, settings: SettingsDep
) -> VersionOut:
    """Generate (or reuse) an integration version from a mapping run, then gate it."""
    try:
        version = generate(
            session,
            body.mapping_run_id,
            condition=body.condition,
            allow_partial=body.allow_partial,
            samples_dir=settings.samples_dir,
        )
    except InputError as error:
        raise HTTPException(404, str(error)) from error
    except GenerationError as error:
        raise HTTPException(400, str(error)) from error
    session.commit()
    return version_out(version)


@router.get("/{integration_id}", response_model=IntegrationOut)
def get_integration(integration_id: int, session: SessionDep) -> IntegrationOut:
    integration = _integration(session, integration_id)
    return IntegrationOut(
        id=integration.id,
        mapping_run_id=integration.mapping_run_id,
        condition=integration.condition,
        name=integration.name,
        versions=[v.version for v in integration.versions],
    )


@router.get("/{integration_id}/versions", response_model=list[VersionOut])
def list_versions(integration_id: int, session: SessionDep) -> list[VersionOut]:
    return [version_out(v) for v in _integration(session, integration_id).versions]


@router.get("/{integration_id}/versions/{number}", response_model=VersionOut)
def get_version(integration_id: int, number: int, session: SessionDep) -> VersionOut:
    return version_out(_version(session, integration_id, number))


@router.get("/{integration_id}/versions/{number}/files", response_model=list[FileOut])
def get_files(integration_id: int, number: int, session: SessionDep) -> list[FileOut]:
    version = _version(session, integration_id, number)
    return [
        FileOut(path=f.path, sha256=f.sha256, kind=f.kind, content=f.content) for f in version.files
    ]


@router.get("/{integration_id}/versions/{number}/gate", response_model=list[GateOut])
def get_gate(integration_id: int, number: int, session: SessionDep) -> list[GateOut]:
    version = _version(session, integration_id, number)
    return [
        GateOut(stage=g.stage, passed=g.passed, findings=g.findings) for g in version.gate_results
    ]


@router.get("/{integration_id}/versions/{number}/sandbox-runs", response_model=list[SandboxRunOut])
def get_sandbox_runs(integration_id: int, number: int, session: SessionDep) -> list[SandboxRunOut]:
    version = _version(session, integration_id, number)
    return [
        SandboxRunOut(
            id=r.id, purpose=r.purpose, outcome=r.outcome, exit_code=r.exit_code,
            duration_s=r.duration_s, result=r.result,
        )
        for r in version.sandbox_runs
    ]  # fmt: skip


@router.post("/{integration_id}/versions/{number}/run-tests", response_model=SandboxRunOut)
def run_tests(
    integration_id: int, number: int, session: SessionDep, runner: RunnerDep
) -> SandboxRunOut:
    """Run the generated tests in the sandbox. Reported separately from the oracle."""
    version = _version(session, integration_id, number)
    try:
        row = run_generated_tests(session, version, runner)
    except GenerationError as error:
        raise HTTPException(409, str(error)) from error
    except SandboxError as error:
        raise HTTPException(503, f"sandbox unavailable: {error}") from error
    session.commit()
    return SandboxRunOut(
        id=row.id, purpose=row.purpose, outcome=row.outcome, exit_code=row.exit_code,
        duration_s=row.duration_s, result=row.result,
    )  # fmt: skip
