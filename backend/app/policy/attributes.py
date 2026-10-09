"""Policy attributes of a system: where it runs and how sensitive its data is.

These are the only inputs about a *system* the policy sees, and they are written only through the
approver-token REST route, with an audit event; no MCP tool can write them (a test enumerates the
tools). A system with no record is treated as the most restrictive case: ``unknown`` environment
(as production) and ``unclassified`` data (as restricted)."""

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db_models import System, SystemPolicyAttribute
from app.policy.audit import AuditLog, EventType
from app.policy.clock import Clock
from app.policy.models import DataClass, Environment

ENVIRONMENT_RANK = {
    Environment.MOCK: 0, Environment.STAGING: 1, Environment.PRODUCTION: 2, Environment.UNKNOWN: 3,
}  # fmt: skip
DATA_CLASS_RANK = {
    DataClass.SYNTHETIC: 0,
    DataClass.INTERNAL: 1,
    DataClass.RESTRICTED: 2,
    DataClass.UNCLASSIFIED: 3,
}  # fmt: skip


@dataclass(frozen=True)
class Attributes:
    environment: Environment
    data_class: DataClass
    recorded: bool  # False: nothing was ever set, so these are the restrictive defaults


DEFAULT = Attributes(Environment.UNKNOWN, DataClass.UNCLASSIFIED, recorded=False)


class UnknownSystem(Exception):
    pass


def get_attributes(session: Session, system_id: int) -> Attributes:
    row = session.get(SystemPolicyAttribute, system_id)
    if row is None:
        return DEFAULT
    return Attributes(Environment(row.environment), DataClass(row.data_class), recorded=True)


def set_attributes(
    session: Session,
    audit: AuditLog,
    clock: Clock,
    system_id: int,
    *,
    environment: Environment,
    data_class: DataClass,
    updated_by: str,
    session_id: str = "rest",
) -> Attributes:
    """Record the attributes of a system. Callable only from the approver-authenticated route."""
    if session.get(System, system_id) is None:
        raise UnknownSystem(f"no system {system_id}")
    before = get_attributes(session, system_id)
    row = session.get(SystemPolicyAttribute, system_id)
    if row is None:
        row = SystemPolicyAttribute(system_id=system_id)
        session.add(row)
    row.environment, row.data_class = environment.value, data_class.value
    row.updated_by, row.updated_at = updated_by, clock.now()
    session.commit()
    audit.append(
        EventType.ATTRIBUTES_CHANGED, session_id=session_id, principal=updated_by,
        payload={
            "system_id": system_id,
            "before": {
                "environment": before.environment.value,
                "data_class": before.data_class.value,
            },
            "after": {"environment": environment.value, "data_class": data_class.value},
        },
    )  # fmt: skip
    return Attributes(environment, data_class, recorded=True)


def aggregate(items: Iterable[Attributes]) -> tuple[Environment | None, DataClass | None]:
    """The most restrictive value across the systems a call involves; None if it involves none."""
    listed = list(items)
    if not listed:
        return None, None
    environment = max((a.environment for a in listed), key=lambda e: ENVIRONMENT_RANK[e])
    data_class = max((a.data_class for a in listed), key=lambda d: DATA_CLASS_RANK[d])
    return environment, data_class
