"""Approvals: bound to the call and the policy, single use, expiring (with an injected clock)."""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db_models import ApprovalRequest, AuditEvent
from app.policy import approvals
from app.policy.audit import AuditLog, EventType, verify_chain
from app.policy.clock import ManualClock
from app.policy.models import ApprovalCheck

ARGS = {"source_system_version": 1, "target_system_version": 2, "source_entity": "A",
        "target_entity": "B"}  # fmt: skip
HASH = "p" * 64


def make(session: Session, audit: AuditLog, clock: ManualClock) -> ApprovalRequest:
    return approvals.create(
        session, audit, clock, session_id="s1", tool="propose_mapping", arguments=ARGS,
        policy_hash=HASH, summary={"tool": "propose_mapping", "ids": [1, 2]},
    )  # fmt: skip


def check(session: Session, clock: ManualClock, approval_id: str | None, **over: object):  # type: ignore[no-untyped-def]
    kwargs: dict[str, object] = {"tool": "propose_mapping", "arguments": ARGS, "policy_hash": HASH}
    kwargs.update(over)
    return approvals.check(session, clock, approval_id, **kwargs)  # type: ignore[arg-type]


def test_the_request_hash_ignores_the_approval_id_but_not_anything_else() -> None:
    base = approvals.request_hash("propose_mapping", ARGS)
    assert base == approvals.request_hash(
        "propose_mapping", {**ARGS, "approval_id": "apr_0123456789abcdef"}
    )
    assert base != approvals.request_hash("propose_mapping", {**ARGS, "target_entity": "C"})
    assert base != approvals.request_hash("generate_integration", ARGS)


def test_a_new_request_is_pending_and_not_usable(
    session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    row = make(session, audit, clock)
    assert row.id.startswith("apr_") and len(row.id) == 20 and row.status == "PENDING"
    assert row.expires_at - row.requested_at == approvals.DEFAULT_TTL
    assert check(session, clock, row.id) is ApprovalCheck.NOT_DECIDED
    assert not approvals.consume(session, audit, clock, row.id)


def test_an_approved_call_is_valid_once(
    session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    row = make(session, audit, clock)
    approvals.decide(session, audit, clock, row.id, approve=True, note="reviewed")
    assert check(session, clock, row.id) is ApprovalCheck.VALID
    assert approvals.consume(session, audit, clock, row.id) is True
    assert check(session, clock, row.id) is ApprovalCheck.CONSUMED
    assert approvals.consume(session, audit, clock, row.id) is False, "single use"


def test_a_denied_request_is_never_valid(
    session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    row = make(session, audit, clock)
    approvals.decide(session, audit, clock, row.id, approve=False)
    assert check(session, clock, row.id) is ApprovalCheck.DENIED
    assert not approvals.consume(session, audit, clock, row.id)


def test_an_approval_expires_by_the_injected_clock(
    session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    row = make(session, audit, clock)
    approvals.decide(session, audit, clock, row.id, approve=True)
    clock.advance(approvals.DEFAULT_TTL.total_seconds() - 1)
    assert check(session, clock, row.id) is ApprovalCheck.VALID
    clock.advance(1)
    assert check(session, clock, row.id) is ApprovalCheck.EXPIRED
    assert not approvals.consume(session, audit, clock, row.id)


def test_a_pending_request_that_lapses_cannot_be_decided(
    session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    row = make(session, audit, clock)
    clock.advance(approvals.DEFAULT_TTL.total_seconds() + 1)
    with pytest.raises(approvals.ApprovalError, match="EXPIRED"):
        approvals.decide(session, audit, clock, row.id, approve=True)


def test_an_approval_is_tied_to_the_exact_call(
    session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    row = make(session, audit, clock)
    approvals.decide(session, audit, clock, row.id, approve=True)
    other_args = {**ARGS, "target_entity": "C"}
    assert check(session, clock, row.id, arguments=other_args) is ApprovalCheck.REQUEST_MISMATCH
    assert (
        check(session, clock, row.id, tool="generate_integration") is ApprovalCheck.REQUEST_MISMATCH
    )
    with_id = {**ARGS, "approval_id": row.id}
    assert check(session, clock, row.id, arguments=with_id) is ApprovalCheck.VALID


def test_a_change_of_policy_voids_the_approval(
    session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    row = make(session, audit, clock)
    approvals.decide(session, audit, clock, row.id, approve=True)
    assert check(session, clock, row.id, policy_hash="q" * 64) is ApprovalCheck.POLICY_CHANGED


def test_unknown_and_missing_ids(session: Session, clock: ManualClock) -> None:
    assert check(session, clock, None) is ApprovalCheck.NONE_SUPPLIED
    assert check(session, clock, "apr_0000000000000000") is ApprovalCheck.UNKNOWN


def test_a_decision_cannot_be_changed(
    session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    row = make(session, audit, clock)
    approvals.decide(session, audit, clock, row.id, approve=False)
    with pytest.raises(approvals.ApprovalError, match="DENIED"):
        approvals.decide(session, audit, clock, row.id, approve=True)
    with pytest.raises(approvals.ApprovalError, match="no approval"):
        approvals.decide(session, audit, clock, "apr_ffffffffffffffff", approve=True)


def test_two_callers_racing_for_one_approval_get_exactly_one_win(
    test_engine: Engine, audit: AuditLog, clock: ManualClock
) -> None:
    with Session(test_engine) as setup:
        row = make(setup, audit, clock)
        approvals.decide(setup, audit, clock, row.id, approve=True)
        approval_id = row.id
    wins: list[bool] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        with Session(test_engine) as own:
            barrier.wait()
            wins.append(approvals.consume(own, audit, clock, approval_id))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(wins) == [False, True]


def test_every_step_is_mirrored_into_the_audit_log(
    test_engine: Engine, session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    row = make(session, audit, clock)
    approvals.decide(session, audit, clock, row.id, approve=True, note="ok")
    approvals.consume(session, audit, clock, row.id)
    with Session(test_engine) as read:
        types = list(
            read.scalars(
                select(AuditEvent.event_type)
                .where(AuditEvent.chain == audit.chain)
                .order_by(AuditEvent.seq)
            )
        )
        count = read.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.chain == audit.chain)
        )
    assert (
        types
        == [
            EventType.APPROVAL_REQUESTED.value,
            EventType.APPROVAL_DECIDED.value,
            EventType.APPROVAL_CONSUMED.value,
        ]
        and count == 3
    )
    assert verify_chain(test_engine, audit.chain).ok


def test_the_time_to_live_is_configurable(
    session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    row = approvals.create(
        session, audit, clock, session_id="s", tool="propose_mapping", arguments=ARGS,
        policy_hash=HASH, summary={}, ttl=timedelta(seconds=5),
    )  # fmt: skip
    approvals.decide(session, audit, clock, row.id, approve=True)
    clock.advance(6)
    assert check(session, clock, row.id) is ApprovalCheck.EXPIRED
