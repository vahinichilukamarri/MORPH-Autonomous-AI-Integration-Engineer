"""Approvals: one human decision about one exact call. Single use, expiring, bound to the policy.

An approval turns a policy's needs-approval into allow for the call it was requested for and for no
other: it is tied to the tool, to the hash of the arguments (the approval id itself excluded) and
to the policy hash. It can be used once, it expires, and a change of policy voids it. It never
overrides a denial (the floor and the evaluator see to that), and no MCP tool can decide one."""

import secrets
from datetime import timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db_models import ApprovalRequest
from app.policy.audit import AuditLog, EventType
from app.policy.canonical import digest
from app.policy.clock import Clock
from app.policy.models import ApprovalCheck

DEFAULT_TTL = timedelta(minutes=30)
APPROVER = "human:approver"


class Status(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    CONSUMED = "CONSUMED"


class ApprovalError(Exception):
    """The approval cannot be decided (unknown, already decided, or expired)."""


def request_hash(tool: str, arguments: dict[str, Any]) -> str:
    """Hash of the call without its approval id, so a retry with the id hashes the same."""
    bare = {k: v for k, v in arguments.items() if k != "approval_id"}
    return digest({"tool": tool, "arguments": bare})


def new_id() -> str:
    return "apr_" + secrets.token_hex(8)


def effective_status(row: ApprovalRequest, clock: Clock) -> str:
    """The stored status, with a lapsed PENDING or APPROVED reported as EXPIRED."""
    lapsed = row.expires_at <= clock.now()
    if lapsed and row.status in (Status.PENDING.value, Status.APPROVED.value):
        return "EXPIRED"
    return row.status


def create(
    session: Session,
    audit: AuditLog,
    clock: Clock,
    *,
    session_id: str,
    tool: str,
    arguments: dict[str, Any],
    policy_hash: str,
    summary: dict[str, Any],
    ttl: timedelta = DEFAULT_TTL,
) -> ApprovalRequest:
    now = clock.now()
    row = ApprovalRequest(
        id=new_id(), session_id=session_id, tool=tool,
        request_hash=request_hash(tool, arguments), policy_hash=policy_hash,
        status=Status.PENDING.value, summary=summary, requested_at=now, expires_at=now + ttl,
    )  # fmt: skip
    session.add(row)
    session.flush()
    session.commit()
    audit.append(
        EventType.APPROVAL_REQUESTED, session_id=session_id, principal="system", tool=tool,
        policy_hash=policy_hash,
        payload={"approval_id": row.id, "request_hash": row.request_hash, "summary": summary},
    )  # fmt: skip
    return row


def get(session: Session, approval_id: str) -> ApprovalRequest | None:
    return session.get(ApprovalRequest, approval_id)


def decide(
    session: Session,
    audit: AuditLog,
    clock: Clock,
    approval_id: str,
    *,
    approve: bool,
    decided_by: str = APPROVER,
    note: str | None = None,
) -> ApprovalRequest:
    row = get(session, approval_id)
    if row is None:
        raise ApprovalError(f"no approval {approval_id}")
    status = effective_status(row, clock)
    if status != Status.PENDING.value:
        raise ApprovalError(f"approval {approval_id} is {status}, not pending")
    row.status = (Status.APPROVED if approve else Status.DENIED).value
    row.decided_by, row.decided_at, row.note = decided_by, clock.now(), note
    session.commit()
    audit.append(
        EventType.APPROVAL_DECIDED, session_id=row.session_id, principal=decided_by, tool=row.tool,
        policy_hash=row.policy_hash,
        payload={"approval_id": row.id, "decision": row.status, "note": note or ""},
    )  # fmt: skip
    return row


def check(
    session: Session,
    clock: Clock,
    approval_id: str | None,
    *,
    tool: str,
    arguments: dict[str, Any],
    policy_hash: str,
) -> ApprovalCheck:
    """What the approval is worth for this call. Read-only; ``consume`` is what uses it up."""
    if approval_id is None:
        return ApprovalCheck.NONE_SUPPLIED
    row = get(session, approval_id)
    if row is None:
        return ApprovalCheck.UNKNOWN
    status = effective_status(row, clock)
    if status == Status.CONSUMED.value:
        return ApprovalCheck.CONSUMED
    if status == Status.DENIED.value:
        return ApprovalCheck.DENIED
    if status == "EXPIRED":
        return ApprovalCheck.EXPIRED
    if status == Status.PENDING.value:
        return ApprovalCheck.NOT_DECIDED
    if row.tool != tool or row.request_hash != request_hash(tool, arguments):
        return ApprovalCheck.REQUEST_MISMATCH
    if row.policy_hash != policy_hash:
        return ApprovalCheck.POLICY_CHANGED
    return ApprovalCheck.VALID


def consume(session: Session, audit: AuditLog, clock: Clock, approval_id: str) -> bool:
    """Use the approval up, atomically: of two callers racing for it, exactly one gets True."""
    now = clock.now()
    result = session.execute(
        update(ApprovalRequest)
        .where(
            ApprovalRequest.id == approval_id,
            ApprovalRequest.status == Status.APPROVED.value,
            ApprovalRequest.expires_at > now,
        )
        .values(status=Status.CONSUMED.value, consumed_at=now)
    )
    session.commit()
    won = bool(result.rowcount)  # type: ignore[attr-defined]
    if won:
        row = get(session, approval_id)
        assert row is not None
        audit.append(
            EventType.APPROVAL_CONSUMED, session_id=row.session_id, principal="system",
            tool=row.tool, policy_hash=row.policy_hash,
            payload={"approval_id": approval_id, "request_hash": row.request_hash},
        )  # fmt: skip
    return won


def listing(
    session: Session, *, status: str | None = None, limit: int = 100
) -> list[ApprovalRequest]:
    query = select(ApprovalRequest).order_by(ApprovalRequest.requested_at.desc()).limit(limit)
    if status is not None:
        query = query.where(ApprovalRequest.status == status)
    return list(session.scalars(query))
