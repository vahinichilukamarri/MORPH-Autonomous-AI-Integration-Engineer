"""System policy attributes: restrictive defaults, aggregation, audited writes."""

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db_models import AuditEvent, System
from app.policy import attributes
from app.policy.attributes import Attributes
from app.policy.audit import AuditLog
from app.policy.clock import ManualClock
from app.policy.models import DataClass, Environment


def system(session: Session, name: str) -> int:
    row = System(name=name)
    session.add(row)
    session.flush()
    return row.id


def test_a_system_with_no_record_gets_the_most_restrictive_values(session: Session) -> None:
    found = attributes.get_attributes(session, system(session, "fresh"))
    assert (found.environment, found.data_class, found.recorded) == (
        Environment.UNKNOWN, DataClass.UNCLASSIFIED, False,
    )  # fmt: skip


def test_attributes_are_recorded_and_audited(
    test_engine: Engine, session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    sid = system(session, "crm-attr")
    saved = attributes.set_attributes(
        session, audit, clock, sid, environment=Environment.MOCK, data_class=DataClass.SYNTHETIC,
        updated_by="human:approver",
    )  # fmt: skip
    assert saved.recorded and attributes.get_attributes(session, sid) == saved
    attributes.set_attributes(
        session, audit, clock, sid, environment=Environment.STAGING, data_class=DataClass.INTERNAL,
        updated_by="human:approver",
    )  # fmt: skip
    assert attributes.get_attributes(session, sid).environment is Environment.STAGING
    with Session(test_engine) as read:
        events = list(
            read.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.chain == audit.chain, AuditEvent.event_type == "ATTRIBUTES_CHANGED"
                )
                .order_by(AuditEvent.seq)
            )
        )
    assert len(events) == 2
    assert events[0].payload["before"] == {"environment": "unknown", "data_class": "unclassified"}
    assert events[1].payload["before"] == {"environment": "mock", "data_class": "synthetic"}
    assert events[1].principal == "human:approver"


def test_an_unknown_system_is_refused(
    session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    with pytest.raises(attributes.UnknownSystem):
        attributes.set_attributes(
            session, audit, clock, 999_999, environment=Environment.MOCK,
            data_class=DataClass.SYNTHETIC, updated_by="human:approver",
        )  # fmt: skip


def test_the_most_restrictive_value_wins_across_systems() -> None:
    mock = Attributes(Environment.MOCK, DataClass.SYNTHETIC, True)
    stage = Attributes(Environment.STAGING, DataClass.INTERNAL, True)
    unknown = attributes.DEFAULT
    assert attributes.aggregate([]) == (None, None)
    assert attributes.aggregate([mock]) == (Environment.MOCK, DataClass.SYNTHETIC)
    assert attributes.aggregate([mock, stage]) == (Environment.STAGING, DataClass.INTERNAL)
    assert attributes.aggregate([mock, stage, unknown]) == (
        Environment.UNKNOWN, DataClass.UNCLASSIFIED,
    )  # fmt: skip
    prod = Attributes(Environment.PRODUCTION, DataClass.RESTRICTED, True)
    assert attributes.aggregate([stage, prod]) == (Environment.PRODUCTION, DataClass.RESTRICTED)
