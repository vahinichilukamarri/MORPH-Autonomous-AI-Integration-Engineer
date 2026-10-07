"""The review gate: which mapped fields may be compiled into an integration.

Only fields a person or the confidence policy has accepted are compiled. A field that is not
accepted is excluded and recorded with the reason. If an excluded field is required and writable
on the target, the whole integration is blocked: nothing partial is shipped silently.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.codegen.inputs import CodegenInput, MappedField
from app.codegen.operations import OperationPlan
from app.mapping.confidence import ReviewStatus
from app.mapping.proposal import MappingType


class GateStatus(StrEnum):
    OPEN = "OPEN"  # every writable target field is compiled
    PARTIAL = "PARTIAL"  # optional fields were excluded and the operator accepted that
    BLOCKED = "BLOCKED_PENDING_REVIEW"


class Reason(StrEnum):
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNRESOLVED = "UNRESOLVED"
    NO_MAPPING = "NO_MAPPING"
    NOT_WRITABLE = "NOT_WRITABLE"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    PARTIAL_NOT_ALLOWED = "PARTIAL_NOT_ALLOWED"


@dataclass(frozen=True)
class Exclusion:
    target_field: str
    reason: Reason
    required: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "target_field": self.target_field,
            "reason": self.reason.value,
            "required": self.required,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GateDecision:
    status: GateStatus
    included: tuple[MappedField, ...]
    excluded: tuple[Exclusion, ...]
    not_writable: tuple[str, ...]
    blocking: tuple[Exclusion, ...]


def _why_not(mapped: MappedField) -> tuple[Reason, str]:
    if mapped.mapping_type is MappingType.UNRESOLVED or mapped.transformation is None:
        return Reason.UNRESOLVED, "the mapping is UNRESOLVED; a person must supply one"
    if mapped.review_status is ReviewStatus.NEEDS_REVIEW:
        return Reason.NEEDS_REVIEW, "the mapping is flagged for review and not yet approved"
    return Reason.NEEDS_REVIEW, f"review status {mapped.review_status.value} is not accepted"


def decide(inp: CodegenInput, plan: OperationPlan, *, allow_partial: bool) -> GateDecision:
    target = plan.target
    entity = inp.target.entity(inp.target_entity)
    assert entity is not None
    by_name = {m.target_field: m for m in inp.fields}
    required_on_create = {f.name for f in target.create_fields if f.required}
    writable = target.writable
    included: list[MappedField] = []
    excluded: list[Exclusion] = []
    not_writable: list[str] = []

    for field in entity.fields:
        mapped = by_name.get(field.name)
        is_id = field.name == target.id_field
        if field.name not in writable:
            not_writable.append(field.name)
            # a target-assigned identity is still compiled: it is used to find existing records
            if is_id and mapped is not None and mapped.approved:
                included.append(mapped)
            continue
        required = field.name in required_on_create
        if mapped is None:
            excluded.append(
                Exclusion(field.name, Reason.NO_MAPPING, required, "no mapping was proposed")
            )
        elif mapped.approved:
            included.append(mapped)
        else:
            reason, detail = _why_not(mapped)
            excluded.append(Exclusion(field.name, reason, required, detail))
    unknown = tuple(
        Exclusion(name, Reason.UNKNOWN_FIELD, False, "the target entity has no such field")
        for name in by_name
        if entity.fields and name not in {f.name for f in entity.fields}
    )
    blocking = tuple(e for e in excluded if e.required)
    optional = tuple(e for e in excluded if not e.required)
    if blocking:
        status = GateStatus.BLOCKED
    elif optional and not allow_partial:
        status = GateStatus.BLOCKED
        blocking = tuple(
            Exclusion(e.target_field, Reason.PARTIAL_NOT_ALLOWED, False, e.detail) for e in optional
        )
    else:
        status = GateStatus.PARTIAL if optional else GateStatus.OPEN
    return GateDecision(
        status=status,
        included=tuple(included),
        excluded=(*excluded, *unknown),
        not_writable=tuple(not_writable),
        blocking=blocking,
    )
