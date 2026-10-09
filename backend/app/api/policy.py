"""Policy, audit, approval and attribute endpoints.

Reads are open like the rest of the local REST API. The two writes that change what the policy
trusts (deciding an approval and recording a system's policy attributes) need the approver token,
which comes from the environment; no MCP tool can reach either of them."""

import hmac
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_engine, get_session
from app.db_models import AuditEvent
from app.policy import approvals, attributes
from app.policy.audit import AuditLog, ChainReport, verify_chain
from app.policy.catalogue import catalogue
from app.policy.clock import Clock, SystemClock
from app.policy.floor import FLOOR_RULES
from app.policy.loader import LoadedPolicy, PolicyError, load_active
from app.policy.models import DataClass, Environment
from app.policy.redact import Redactor
from app.policy.secrets import known_secrets
from app.settings import Settings, get_settings

router = APIRouter(tags=["policy"])
SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@lru_cache
def _policy(directory: Path) -> LoadedPolicy:
    return load_active(directory)


def get_policy(settings: SettingsDep) -> LoadedPolicy:
    try:
        return _policy(settings.policy_dir)
    except PolicyError as error:
        raise HTTPException(status_code=503, detail=f"policy unavailable: {error}") from error


def get_clock() -> Clock:
    return SystemClock()


def get_audit_log(clock: Annotated[Clock, Depends(get_clock)]) -> AuditLog:
    return AuditLog(get_engine(), clock=clock, redactor=Redactor(known_secrets(get_settings())))


PolicyDep = Annotated[LoadedPolicy, Depends(get_policy)]
AuditDep = Annotated[AuditLog, Depends(get_audit_log)]
ClockDep = Annotated[Clock, Depends(get_clock)]


def require_approver(
    settings: SettingsDep,
    token: Annotated[str | None, Header(alias="X-Approver-Token")] = None,
) -> str:
    """The caller must present the approver token. The token is never echoed or logged."""
    expected = settings.approver_token
    if expected is None:
        raise HTTPException(status_code=503, detail="no approver token is configured")
    given = (token or "").encode("utf-8")
    if not token or not hmac.compare_digest(given, expected.get_secret_value().encode("utf-8")):
        raise HTTPException(status_code=401, detail="approver token required")
    return approvals.APPROVER


ApproverDep = Annotated[str, Depends(require_approver)]


# ---- the active policy and the tool catalogue --------------------------------------------------


class PolicyOut(BaseModel):
    version: str
    hash: str
    description: str
    session_limits: dict[str, int]
    rules: list[dict[str, Any]]
    floor: list[dict[str, str]]


@router.get("/policy/active", response_model=PolicyOut)
def active_policy(policy: PolicyDep) -> PolicyOut:
    file = policy.file
    return PolicyOut(
        version=policy.version,
        hash=policy.hash,
        description=file.description,
        session_limits=file.session_limits.model_dump(),
        rules=[r.model_dump(mode="json", exclude_none=True) for r in file.rules],
        floor=[{"id": key, "description": text} for key, text in FLOOR_RULES.items()],
    )


@router.get("/mcp/tools")
def mcp_tools(policy: PolicyDep) -> list[dict[str, Any]]:
    return catalogue(policy)


# ---- the audit log -----------------------------------------------------------------------------


class AuditEventOut(BaseModel):
    seq: int
    created_at: datetime
    session_id: str
    event_type: str
    tool: str | None
    principal: str
    call_id: str | None
    policy_hash: str | None
    payload: dict[str, Any]
    row_hash: str


@router.get("/audit/events", response_model=list[AuditEventOut])
def audit_events(
    session: SessionDep,
    audit: AuditDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    session_id: str | None = None,
    tool: str | None = None,
    event_type: str | None = None,
    call_id: str | None = None,
    chain: str | None = None,
) -> list[AuditEventOut]:
    query = select(AuditEvent).where(
        AuditEvent.chain == (chain or audit.chain), AuditEvent.seq > after_seq
    )
    for column, value in (
        (AuditEvent.session_id, session_id),
        (AuditEvent.tool, tool),
        (AuditEvent.event_type, event_type),
        (AuditEvent.call_id, call_id),
    ):
        if value is not None:
            query = query.where(column == value)
    rows = session.scalars(query.order_by(AuditEvent.seq).limit(limit))
    return [
        AuditEventOut(
            seq=r.seq, created_at=r.created_at, session_id=r.session_id, event_type=r.event_type,
            tool=r.tool, principal=r.principal, call_id=r.call_id, policy_hash=r.policy_hash,
            payload=r.payload, row_hash=r.row_hash,
        )
        for r in rows
    ]  # fmt: skip


class ChainOut(BaseModel):
    chain: str
    ok: bool
    events: int
    first_bad_seq: int | None
    problem: str | None


@router.get("/audit/verify", response_model=ChainOut)
def audit_verify(audit: AuditDep, chain: str | None = None) -> ChainOut:
    report: ChainReport = verify_chain(audit.engine, chain or audit.chain)
    return ChainOut(
        chain=report.chain, ok=report.ok, events=report.events,
        first_bad_seq=report.first_bad_seq, problem=report.problem,
    )  # fmt: skip


# ---- approvals ---------------------------------------------------------------------------------


class ApprovalOut(BaseModel):
    id: str
    tool: str
    status: str
    summary: dict[str, Any]
    request_hash: str
    policy_hash: str
    requested_at: datetime
    expires_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    note: str | None
    session_id: str


def _approval_out(row: Any, clock: Clock) -> ApprovalOut:
    return ApprovalOut(
        id=row.id, tool=row.tool, status=approvals.effective_status(row, clock),
        summary=row.summary, request_hash=row.request_hash, policy_hash=row.policy_hash,
        requested_at=row.requested_at, expires_at=row.expires_at, decided_by=row.decided_by,
        decided_at=row.decided_at, note=row.note, session_id=row.session_id,
    )  # fmt: skip


@router.get("/approvals", response_model=list[ApprovalOut])
def list_approvals(
    session: SessionDep,
    clock: ClockDep,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ApprovalOut]:
    rows = approvals.listing(session, limit=limit)
    out = [_approval_out(r, clock) for r in rows]
    return [o for o in out if status is None or o.status == status]


@router.get("/approvals/{approval_id}", response_model=ApprovalOut)
def get_approval(approval_id: str, session: SessionDep, clock: ClockDep) -> ApprovalOut:
    row = approvals.get(session, approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return _approval_out(row, clock)


class DecideRequest(BaseModel):
    decision: Literal["approve", "deny"]
    note: str | None = Field(default=None, max_length=500)


@router.post("/approvals/{approval_id}/decide", response_model=ApprovalOut)
def decide_approval(
    approval_id: str,
    body: DecideRequest,
    session: SessionDep,
    clock: ClockDep,
    audit: AuditDep,
    who: ApproverDep,
) -> ApprovalOut:
    try:
        row = approvals.decide(
            session, audit, clock, approval_id, approve=body.decision == "approve",
            decided_by=who, note=body.note,
        )  # fmt: skip
    except approvals.ApprovalError as error:
        status = 404 if "no approval" in str(error) else 409
        raise HTTPException(status_code=status, detail=str(error)) from error
    return _approval_out(row, clock)


# ---- policy attributes of a system -------------------------------------------------------------


class AttributesBody(BaseModel):
    environment: Environment
    data_class: DataClass


class AttributesOut(BaseModel):
    system_id: int
    environment: Environment
    data_class: DataClass
    recorded: bool


@router.get("/systems/{system_id}/policy-attributes", response_model=AttributesOut)
def read_attributes(system_id: int, session: SessionDep) -> AttributesOut:
    found = attributes.get_attributes(session, system_id)
    return AttributesOut(
        system_id=system_id, environment=found.environment, data_class=found.data_class,
        recorded=found.recorded,
    )  # fmt: skip


@router.put("/systems/{system_id}/policy-attributes", response_model=AttributesOut)
def write_attributes(
    system_id: int,
    body: AttributesBody,
    session: SessionDep,
    clock: ClockDep,
    audit: AuditDep,
    who: ApproverDep,
) -> AttributesOut:
    try:
        saved = attributes.set_attributes(
            session, audit, clock, system_id, environment=body.environment,
            data_class=body.data_class, updated_by=who,
        )  # fmt: skip
    except attributes.UnknownSystem as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return AttributesOut(
        system_id=system_id, environment=saved.environment, data_class=saved.data_class,
        recorded=True,
    )  # fmt: skip
