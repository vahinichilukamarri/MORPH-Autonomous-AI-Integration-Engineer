"""The audit log: append-only (a database trigger) and hash-chained (verified here).

Every event stores the hash of the previous event in its chain and its own hash over all its
fields, so an edited, inserted or removed row is found by ``verify_chain``. Appends take a
per-chain advisory lock inside their own short transaction, so concurrent writers (other processes
included) keep the chain linear, and an audit event survives the rollback of the business
transaction it describes.

Known limit, stated rather than hidden: deleting the *last* rows of a chain leaves a shorter, still
valid chain. Detecting that needs an external anchor (the head hash stored somewhere else), which
v0.6 does not have."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app.db_models import AuditEvent
from app.policy.canonical import canonical_json, sha256_hex
from app.policy.clock import Clock, SystemClock
from app.policy.redact import Redactor

GENESIS = "0" * 64
MAIN_CHAIN = "main"


class EventType(StrEnum):
    CALL_RECEIVED = "CALL_RECEIVED"
    POLICY_DECISION = "POLICY_DECISION"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_DECIDED = "APPROVAL_DECIDED"
    APPROVAL_CONSUMED = "APPROVAL_CONSUMED"
    CHARGE = "CHARGE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    OUTPUT_REDACTED = "OUTPUT_REDACTED"
    CALL_EXECUTED = "CALL_EXECUTED"
    ATTRIBUTES_CHANGED = "ATTRIBUTES_CHANGED"
    POLICY_LOADED = "POLICY_LOADED"


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def row_hash(
    *,
    chain: str,
    seq: int,
    prev_hash: str,
    created_at: datetime,
    session_id: str,
    event_type: str,
    tool: str | None,
    principal: str,
    call_id: str | None,
    policy_hash: str | None,
    payload: Mapping[str, Any],
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "chain": chain,
                "seq": seq,
                "prev": prev_hash,
                "at": stamp(created_at),
                "session": session_id,
                "type": event_type,
                "tool": tool,
                "principal": principal,
                "call": call_id,
                "policy": policy_hash,
                "payload": dict(payload),
            }
        )  # fmt: skip
    )


def event_hash(event: AuditEvent) -> str:
    return row_hash(
        chain=event.chain, seq=event.seq, prev_hash=event.prev_hash,
        created_at=event.created_at, session_id=event.session_id, event_type=event.event_type,
        tool=event.tool, principal=event.principal, call_id=event.call_id,
        policy_hash=event.policy_hash, payload=event.payload,
    )  # fmt: skip


def lock_key(chain: str) -> int:
    return int.from_bytes(hashlib.sha256(chain.encode("utf-8")).digest()[:8], "big", signed=True)


def _no_floats(value: Any) -> None:
    if isinstance(value, float):
        raise ValueError("audit payloads carry no floats (their text form is not stable)")
    if isinstance(value, Mapping):
        for item in value.values():
            _no_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _no_floats(item)


@dataclass(frozen=True)
class Appended:
    seq: int
    row_hash: str


class AuditLog:
    def __init__(
        self,
        engine: Engine,
        *,
        chain: str = MAIN_CHAIN,
        clock: Clock | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._engine = engine
        self.chain = chain
        self._clock = clock or SystemClock()
        self._redactor = redactor or Redactor()

    @property
    def engine(self) -> Engine:
        return self._engine

    def append(
        self,
        event_type: EventType,
        *,
        session_id: str,
        principal: str,
        tool: str | None = None,
        call_id: str | None = None,
        policy_hash: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Appended:
        """Redact the payload, then add one event in its own transaction."""
        clean, _ = self._redactor.value(dict(payload or {}))
        _no_floats(clean)
        with Session(self._engine) as session, session.begin():
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key(self.chain)}
            )
            previous = session.scalar(
                select(AuditEvent.row_hash)
                .where(AuditEvent.chain == self.chain)
                .order_by(AuditEvent.seq.desc())
                .limit(1)
            )
            prev_hash = previous or GENESIS
            seq = session.execute(
                text("SELECT nextval(pg_get_serial_sequence('audit_events', 'seq'))")
            ).scalar_one()
            created_at = self._clock.now()
            digest = row_hash(
                chain=self.chain, seq=seq, prev_hash=prev_hash, created_at=created_at,
                session_id=session_id, event_type=event_type.value, tool=tool,
                principal=principal, call_id=call_id, policy_hash=policy_hash, payload=clean,
            )  # fmt: skip
            session.add(
                AuditEvent(
                    seq=seq,
                    chain=self.chain,
                    created_at=created_at,
                    session_id=session_id,
                    event_type=event_type.value,
                    tool=tool,
                    principal=principal,
                    call_id=call_id,
                    policy_hash=policy_hash,
                    payload=clean,
                    prev_hash=prev_hash,
                    row_hash=digest,
                )  # fmt: skip
            )
        return Appended(seq, digest)

    def count(self, session_id: str, event_type: EventType, *, kind: str | None = None) -> int:
        """How many events of a type a session has (budget counters are CHARGE events by kind)."""
        query = (
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.chain == self.chain,
                AuditEvent.session_id == session_id,
                AuditEvent.event_type == event_type.value,
            )
        )
        if kind is not None:
            query = query.where(AuditEvent.payload["kind"].as_string() == kind)
        with Session(self._engine) as session:
            return int(session.scalar(query) or 0)


@dataclass(frozen=True)
class ChainReport:
    chain: str
    ok: bool
    events: int
    first_bad_seq: int | None
    problem: str | None


def verify_chain(engine: Engine, chain: str = MAIN_CHAIN) -> ChainReport:
    """Recompute every hash of a chain in order; report the first event that does not fit."""
    count = 0
    previous = GENESIS
    with Session(engine) as session:
        rows = session.scalars(
            select(AuditEvent).where(AuditEvent.chain == chain).order_by(AuditEvent.seq)
        ).yield_per(500)
        for event in rows:
            count += 1
            if event.prev_hash != previous:
                return ChainReport(
                    chain, False, count, event.seq,
                    "an earlier event was removed or the order was changed",
                )  # fmt: skip
            if event_hash(event) != event.row_hash:
                return ChainReport(chain, False, count, event.seq, "the event was altered")
            previous = event.row_hash
    return ChainReport(chain, True, count, None, None)
