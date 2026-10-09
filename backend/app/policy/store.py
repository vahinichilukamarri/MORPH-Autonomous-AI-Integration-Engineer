"""Remember which policy versions the server has run with."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import PolicyVersionRow
from app.policy.loader import LoadedPolicy


def record_policy_version(session: Session, policy: LoadedPolicy) -> PolicyVersionRow:
    """Store the parsed policy under its hash; the same hash is stored once."""
    existing = session.scalar(
        select(PolicyVersionRow).where(PolicyVersionRow.policy_hash == policy.hash)
    )
    if existing is not None:
        return existing
    row = PolicyVersionRow(
        version=policy.version,
        policy_hash=policy.hash,
        content=policy.file.model_dump(mode="json"),
    )
    session.add(row)
    session.flush()
    return row
