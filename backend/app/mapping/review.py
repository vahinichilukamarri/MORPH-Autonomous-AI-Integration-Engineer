"""Human review: approving or overriding a mapping appends a new version.

An override is validated deterministically like any proposal, so a person cannot introduce a
mapping that fails the checks; approving a failing mapping is refused for the same reason.
"""

from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.orm import Session

from app.db_models import Mapping, MappingRun, MappingVersion, SystemVersion
from app.discovery.models import Field
from app.mapping.confidence import ReviewStatus
from app.mapping.proposal import Certainty, MappingType, Proposal
from app.mapping.runner import load_entity_fields
from app.mapping.samples import load_samples
from app.mapping.store import (
    append_version,
    latest_version,
    proposal_from_version,
    validation_from_version,
)
from app.mapping.transform import JsonScalar, Transformation
from app.mapping.validate import Reason, Status, validate_proposal


class ReviewError(Exception):
    def __init__(self, message: str, reasons: Sequence[Reason] = ()) -> None:
        super().__init__(message)
        self.reasons = tuple(reasons)


def _context(
    session: Session, run: MappingRun, samples_dir: Path
) -> tuple[dict[str, Field], dict[str, Field], list[dict[str, JsonScalar]]]:
    source = {
        f.path: f for _, f in load_entity_fields(session, run.source_version_id, run.source_entity)
    }
    target = {
        f.path: f for _, f in load_entity_fields(session, run.target_version_id, run.target_entity)
    }
    source_version = session.get(SystemVersion, run.source_version_id)
    assert source_version is not None
    samples = load_samples(run.source_entity, source_version.api_version, samples_dir)
    return source, target, samples


def approve(session: Session, mapping: Mapping) -> MappingVersion:
    current = latest_version(session, mapping.id)
    if current.validation_status == Status.FAIL.value:
        raise ReviewError("a mapping that fails validation cannot be approved; override it instead")
    return append_version(
        session,
        mapping,
        "human",
        proposal_from_version(current),
        validation_from_version(current),
        current.confidence,
        ReviewStatus.APPROVED,
        ("approved",),
    )


def override(
    session: Session,
    mapping: Mapping,
    *,
    mapping_type: MappingType,
    source_fields: Sequence[str],
    transformation: Transformation | None,
    unresolved_reason: str | None,
    rationale: str,
    samples_dir: Path,
) -> MappingVersion:
    proposal = Proposal(
        target_field=mapping.target_field,
        mapping_type=mapping_type,
        source_fields=tuple(source_fields),
        transformation=transformation,
        unresolved_reason=unresolved_reason,
        rationale=rationale,
        certainty=Certainty.HIGH,
    )
    if mapping_type is MappingType.UNRESOLVED and not unresolved_reason:
        raise ReviewError("an UNRESOLVED override needs an unresolved_reason")
    source, target, samples = _context(session, mapping.run, samples_dir)
    if mapping.target_field not in target:
        raise ReviewError(f"target field {mapping.target_field!r} does not exist")
    validation = validate_proposal(
        proposal, source_fields=source, target_field=target[mapping.target_field], samples=samples
    )
    if validation.status is Status.FAIL:
        raise ReviewError("the override fails validation", validation.reasons)
    return append_version(
        session,
        mapping,
        "human",
        proposal,
        validation,
        None,
        ReviewStatus.OVERRIDDEN,
        ("human_override",),
    )
