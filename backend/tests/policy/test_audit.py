"""The audit log: hash chain, append-only trigger, tamper detection, two writers at once."""

import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db_models import AuditEvent
from app.policy.audit import GENESIS, AuditLog, EventType, event_hash, verify_chain
from app.policy.clock import ManualClock
from app.policy.redact import Redactor

BACKEND = Path(__file__).resolve().parents[2]


def fill(audit: AuditLog, n: int = 5, session_id: str = "s1") -> None:
    for i in range(n):
        audit.append(
            EventType.CALL_RECEIVED,
            session_id=session_id,
            principal="agent:operator",
            tool="get_system",
            call_id=f"c{i}",
            payload={"i": i},
        )


@contextmanager
def triggers_off(engine: Engine) -> Iterator[None]:
    """What someone with database ownership could do: switch the guard off, then back on."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER USER"))


def rows(engine: Engine, chain: str) -> list[AuditEvent]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.chain == chain).order_by(AuditEvent.seq)
            )
        )


def test_events_are_chained_from_a_genesis_hash(test_engine: Engine, audit: AuditLog) -> None:
    fill(audit, 4)
    events = rows(test_engine, audit.chain)
    assert events[0].prev_hash == GENESIS
    for before, after in zip(events, events[1:], strict=False):
        assert after.prev_hash == before.row_hash and after.seq > before.seq
    assert all(event_hash(e) == e.row_hash for e in events)
    report = verify_chain(test_engine, audit.chain)
    assert (report.ok, report.events, report.first_bad_seq) == (True, 4, None)


def test_an_empty_chain_verifies(test_engine: Engine, audit: AuditLog) -> None:
    assert verify_chain(test_engine, audit.chain).ok


def test_chains_are_independent(test_engine: Engine, clock: ManualClock) -> None:
    a, b = (
        AuditLog(test_engine, chain="iso-a", clock=clock),
        AuditLog(test_engine, chain="iso-b", clock=clock),
    )
    fill(a, 2)
    fill(b, 3)
    assert verify_chain(test_engine, "iso-a").events >= 2 and verify_chain(test_engine, "iso-b").ok
    assert rows(test_engine, "iso-b")[0].prev_hash == GENESIS


@pytest.mark.parametrize("statement", [
    "UPDATE audit_events SET principal = 'x' WHERE chain = :c",
    "DELETE FROM audit_events WHERE chain = :c",
])  # fmt: skip
def test_update_and_delete_are_refused_by_the_database(
    test_engine: Engine, audit: AuditLog, statement: str
) -> None:
    fill(audit, 2)
    with pytest.raises(DBAPIError, match="append-only"), test_engine.begin() as conn:
        conn.execute(text(statement), {"c": audit.chain})
    assert verify_chain(test_engine, audit.chain).ok


def test_truncate_is_refused_by_the_database(test_engine: Engine, audit: AuditLog) -> None:
    fill(audit, 1)
    with pytest.raises(DBAPIError, match="append-only"), test_engine.begin() as conn:
        conn.execute(text("TRUNCATE audit_events"))


def tamper(engine: Engine, chain: str, sql: str, **params: object) -> None:
    with triggers_off(engine), engine.begin() as conn:
        conn.execute(text(sql), {"c": chain, **params})


def test_an_edited_payload_fails_verification_at_that_event(
    test_engine: Engine, audit: AuditLog
) -> None:
    fill(audit, 5)
    target = rows(test_engine, audit.chain)[2]
    tamper(
        test_engine, audit.chain,
        "UPDATE audit_events SET payload = '{\"i\": 99}' WHERE seq = :s", s=target.seq,
    )  # fmt: skip
    report = verify_chain(test_engine, audit.chain)
    assert (report.ok, report.first_bad_seq, report.problem) == (
        False,
        target.seq,
        "the event was altered",
    )


@pytest.mark.parametrize(
    "column, value",
    [
        ("event_type", "'CALL_EXECUTED'"),
        ("principal", "'human:approver'"),
        ("tool", "'ingest_contract'"),
        ("policy_hash", "'" + "a" * 64 + "'"),
        ("session_id", "'other'"),
        ("call_id", "'forged'"),
    ],
)
def test_any_edited_column_fails_verification(
    test_engine: Engine, audit: AuditLog, column: str, value: str
) -> None:
    fill(audit, 3)
    target = rows(test_engine, audit.chain)[1]
    tamper(
        test_engine,
        audit.chain,
        f"UPDATE audit_events SET {column} = {value} WHERE seq = :s",
        s=target.seq,
    )
    report = verify_chain(test_engine, audit.chain)
    assert not report.ok and report.first_bad_seq == target.seq


def test_an_edited_timestamp_fails_verification(test_engine: Engine, audit: AuditLog) -> None:
    fill(audit, 3)
    target = rows(test_engine, audit.chain)[1]
    tamper(
        test_engine, audit.chain,
        "UPDATE audit_events SET created_at = created_at + interval '1 second' WHERE seq = :s",
        s=target.seq,
    )  # fmt: skip
    assert verify_chain(test_engine, audit.chain).first_bad_seq == target.seq


def test_a_removed_middle_event_fails_verification_at_its_successor(
    test_engine: Engine, audit: AuditLog
) -> None:
    fill(audit, 5)
    events = rows(test_engine, audit.chain)
    tamper(test_engine, audit.chain, "DELETE FROM audit_events WHERE seq = :s", s=events[2].seq)
    report = verify_chain(test_engine, audit.chain)
    assert (report.ok, report.first_bad_seq) == (False, events[3].seq)
    assert report.problem == "an earlier event was removed or the order was changed"


def test_a_forged_event_with_a_wrong_prev_hash_fails_verification(
    test_engine: Engine, audit: AuditLog
) -> None:
    fill(audit, 3)
    last = rows(test_engine, audit.chain)[-1]
    forged_seq = last.seq + 10_000_000
    forged = AuditEvent(
        seq=forged_seq, chain=audit.chain, created_at=last.created_at, session_id="x",
        event_type="CALL_EXECUTED", tool=None, principal="agent:operator", call_id=None,
        policy_hash=None, payload={}, prev_hash="f" * 64, row_hash="e" * 64,
    )  # fmt: skip
    with triggers_off(test_engine), Session(test_engine) as session, session.begin():
        session.add(forged)
    report = verify_chain(test_engine, audit.chain)
    assert (report.ok, report.first_bad_seq) == (False, forged_seq)


def test_swapping_two_events_fails_verification(test_engine: Engine, audit: AuditLog) -> None:
    fill(audit, 4)
    events = rows(test_engine, audit.chain)
    a, b = events[1], events[2]
    with triggers_off(test_engine), test_engine.begin() as conn:
        conn.execute(text("UPDATE audit_events SET seq = -1 WHERE seq = :s"), {"s": a.seq})
        conn.execute(
            text("UPDATE audit_events SET seq = :a WHERE seq = :b"), {"a": a.seq, "b": b.seq}
        )
        conn.execute(text("UPDATE audit_events SET seq = :b WHERE seq = -1"), {"b": b.seq})
    assert not verify_chain(test_engine, audit.chain).ok


def test_removing_the_last_events_is_not_detected_and_that_is_stated(
    test_engine: Engine, audit: AuditLog
) -> None:
    """A documented limit: with no anchor outside the database, a shortened tail verifies."""
    fill(audit, 4)
    last = rows(test_engine, audit.chain)[-1]
    tamper(test_engine, audit.chain, "DELETE FROM audit_events WHERE seq = :s", s=last.seq)
    assert verify_chain(test_engine, audit.chain).ok  # known limit, see app/policy/audit.py


def test_payloads_are_redacted_before_they_are_stored(
    test_engine: Engine, clock: ManualClock
) -> None:
    secret = "sentinel-secret-value-12345"
    log = AuditLog(test_engine, chain="redact-1", clock=clock, redactor=Redactor([secret]))
    log.append(
        EventType.CALL_EXECUTED, session_id="s", principal="agent:operator",
        payload={"detail": f"failed with {secret}", "token": "gsk_" + "Ab1" * 8},
    )  # fmt: skip
    stored = rows(test_engine, "redact-1")[0].payload
    assert secret not in str(stored) and "gsk_" not in str(stored)
    assert verify_chain(test_engine, "redact-1").ok


def test_floats_are_refused_because_their_text_form_is_not_stable(audit: AuditLog) -> None:
    with pytest.raises(ValueError, match="floats"):
        audit.append(EventType.CHARGE, session_id="s", principal="p", payload={"x": 1.5})


def test_counters_come_from_the_log(audit: AuditLog) -> None:
    for kind in ("model_call", "model_call", "sandbox_run"):
        audit.append(EventType.CHARGE, session_id="s9", principal="system", payload={"kind": kind})
    audit.append(
        EventType.CHARGE, session_id="other", principal="system", payload={"kind": "model_call"}
    )
    assert audit.count("s9", EventType.CHARGE, kind="model_call") == 2
    assert audit.count("s9", EventType.CHARGE, kind="sandbox_run") == 1
    assert audit.count("s9", EventType.CHARGE) == 3
    assert audit.count("s9", EventType.BUDGET_EXCEEDED) == 0


def test_two_processes_appending_at_once_keep_one_linear_valid_chain(
    test_engine: Engine, chain_name: str
) -> None:
    url = test_engine.url.render_as_string(hide_password=False)
    per_worker = 30
    start = time.time() + 4.0
    workers = [
        subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "tests.policy.audit_worker",
                url,
                chain_name,
                str(per_worker),
                str(start),
                label,
            ],
            cwd=BACKEND,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )  # fmt: skip
        for label in ("a", "b")
    ]
    outputs = [w.communicate(timeout=120) for w in workers]
    assert [w.returncode for w in workers] == [0, 0], outputs
    assert url not in "".join(out + err for out, err in outputs), "the worker never prints the url"

    events = rows(test_engine, chain_name)
    assert len(events) == 2 * per_worker
    report = verify_chain(test_engine, chain_name)
    assert (report.ok, report.events) == (True, 2 * per_worker)
    labels = [e.payload["label"] for e in events]
    assert labels != sorted(labels), "the two writers really interleaved"
    for label in ("a", "b"):
        own = [e.payload["n"] for e in events if e.payload["label"] == label]
        assert own == list(range(per_worker)), "each writer's events stay in order"
