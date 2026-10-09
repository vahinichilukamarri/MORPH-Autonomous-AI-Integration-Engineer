"""The tamper corpus: 11 edits that chain verification must detect, and 1 known limit it cannot.

Each case writes a private chain of events, then does what someone with ownership of the database
could do (switch the append-only trigger off, change the table, switch it back on), and asks
``verify_chain``."""

import hashlib
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.db_models import AuditEvent
from app.policy.audit import AuditLog, EventType, verify_chain
from app.policy.clock import ManualClock


@dataclass(frozen=True)
class TamperCase:
    id: str
    description: str
    detectable: bool
    edit: Callable[[Engine, str, list[int]], int | None]  # returns the seq that should fail


@contextmanager
def triggers_off(engine: Engine) -> Iterator[None]:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER USER"))


def update(
    column: str, value: str, index: int = 2
) -> Callable[[Engine, str, list[int]], int | None]:
    def edit(engine: Engine, chain: str, seqs: list[int]) -> int | None:
        with triggers_off(engine), engine.begin() as conn:
            conn.execute(
                text(f"UPDATE audit_events SET {column} = {value} WHERE seq = :s"),
                {"s": seqs[index]},
            )
        return seqs[index]

    return edit


def remove_middle(engine: Engine, chain: str, seqs: list[int]) -> int | None:
    with triggers_off(engine), engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_events WHERE seq = :s"), {"s": seqs[2]})
    return seqs[3]


def forge(engine: Engine, chain: str, seqs: list[int]) -> int | None:
    forged_seq = seqs[-1] + 10_000_000
    with Session(engine) as db:
        last = db.scalars(select(AuditEvent).where(AuditEvent.seq == seqs[-1])).one()
        event = AuditEvent(
            seq=forged_seq, chain=chain, created_at=last.created_at, session_id="x",
            event_type="CALL_EXECUTED", tool=None, principal="agent:operator", call_id=None,
            policy_hash=None, payload={}, prev_hash="f" * 64,
            row_hash=hashlib.sha256(chain.encode()).hexdigest(),  # wrong, and unique per chain
        )  # fmt: skip
    with triggers_off(engine), Session(engine) as db, db.begin():
        db.add(event)
    return forged_seq


def swap(engine: Engine, chain: str, seqs: list[int]) -> int | None:
    a, b = seqs[1], seqs[2]
    with triggers_off(engine), engine.begin() as conn:
        conn.execute(text("UPDATE audit_events SET seq = -1 WHERE seq = :s"), {"s": a})
        conn.execute(text("UPDATE audit_events SET seq = :a WHERE seq = :b"), {"a": a, "b": b})
        conn.execute(text("UPDATE audit_events SET seq = :b WHERE seq = -1"), {"b": b})
    return None  # any failing position will do


def remove_tail(engine: Engine, chain: str, seqs: list[int]) -> int | None:
    with triggers_off(engine), engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_events WHERE seq = :s"), {"s": seqs[-1]})
    return None


CASES: tuple[TamperCase, ...] = (
    TamperCase("T-01", "an edited payload", True, update("payload", "'{\"i\": 99}'")),
    TamperCase("T-02", "an edited event type", True, update("event_type", "'CALL_EXECUTED'", 1)),
    TamperCase("T-03", "an edited principal", True, update("principal", "'human:approver'", 1)),
    TamperCase("T-04", "an edited tool", True, update("tool", "'ingest_contract'", 1)),
    TamperCase(
        "T-05", "an edited policy hash", True, update("policy_hash", "'" + "a" * 64 + "'", 1)
    ),
    TamperCase("T-06", "an edited session", True, update("session_id", "'other'", 1)),
    TamperCase("T-07", "an edited call id", True, update("call_id", "'forged'", 1)),
    TamperCase(
        "T-08",
        "an edited timestamp",
        True,
        update("created_at", "created_at + interval '1 second'", 1),
    ),  # fmt: skip
    TamperCase("T-09", "a removed middle event", True, remove_middle),
    TamperCase("T-10", "a forged event with a wrong previous hash", True, forge),
    TamperCase("T-11", "two events swapped", True, swap),
    TamperCase("T-12", "the last event removed (known limit)", False, remove_tail),
)


@dataclass
class TamperResult:
    id: str
    description: str
    detectable: bool
    detected: bool
    at_expected_event: bool

    @property
    def as_documented(self) -> bool:
        if self.detectable:
            return self.detected and self.at_expected_event
        return not self.detected  # the known limit: reported as undetected, not hidden


def run_case(engine: Engine, case: TamperCase) -> TamperResult:
    chain = f"tamper-{uuid.uuid4().hex[:10]}"
    log = AuditLog(engine, chain=chain, clock=ManualClock())
    for i in range(5):
        log.append(
            EventType.CALL_RECEIVED, session_id="s", principal="agent:operator", tool="get_system",
            call_id=f"c{i}", payload={"i": i},
        )  # fmt: skip
    with Session(engine) as db:
        seqs = list(
            db.scalars(
                select(AuditEvent.seq).where(AuditEvent.chain == chain).order_by(AuditEvent.seq)
            )
        )
    assert verify_chain(engine, chain).ok, "the chain is intact before the edit"
    expected = case.edit(engine, chain, seqs)
    report = verify_chain(engine, chain)
    return TamperResult(
        case.id, case.description, case.detectable, detected=not report.ok,
        at_expected_event=expected is None or report.first_bad_seq == expected,
    )  # fmt: skip
