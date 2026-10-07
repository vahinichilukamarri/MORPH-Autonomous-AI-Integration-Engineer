"""Validator v2: post-hoc checks added after the v1 evaluation results were seen.

Designed after seeing v1 failures, so it is not an unbiased improvement: the v1 evaluation
(``validate.py``, confidence formula v1) is frozen and reported as it was. This module is a
separate, versioned path. It runs every v1 check unchanged and then adds data-driven checks over
the sample records:

* ``MOSTLY_NULL_OUTPUT`` (forces review): for more than half of the records whose source values
  are non-null, the pipeline outputs null. A nullable target hides this from v1, because null is
  a legal value there.
* ``CONSTANT_OUTPUT`` (forces review): a mapping that is not declared CONSTANT outputs one single
  value for records whose source values differ.
* ``LOSSY_COLLAPSE`` (informational): several distinct values of a categorical (enum) source map
  to the same output.
* ``LOSSY_TRUNCATION`` (informational): a ``SPLIT_PART`` throws away parts of the input for some
  record, so different inputs can end in the same output.

The two lossy checks are recorded but do not force review (``LOSSY_FORCES_REVIEW`` is empty); see
docs/mapping-eval.md for the measured trade-off behind that choice.
"""

from collections.abc import Mapping, Sequence

from app.discovery.models import Field
from app.mapping.confidence import REVIEW_FORCING_CODES, THRESHOLD, ReviewStatus
from app.mapping.proposal import MappingType, Proposal
from app.mapping.transform import (
    JsonScalar,
    SplitPart,
    Transformation,
    TransformError,
    execute,
)
from app.mapping.validate import (
    Code,
    Reason,
    RetrievalSignal,
    Status,
    ValidationResult,
    validate_proposal,
)

VALIDATOR_V2 = "validator-v2"
MOSTLY_NULL_SHARE = 0.5
MIN_RECORDS = 2
SHOWN_EXAMPLES = 2

REVIEW_FORCING_CODES_V2 = REVIEW_FORCING_CODES | {Code.MOSTLY_NULL_OUTPUT, Code.CONSTANT_OUTPUT}
LOSSY_CODES = frozenset({Code.LOSSY_COLLAPSE, Code.LOSSY_TRUNCATION})
LOSSY_FORCES_REVIEW: frozenset[Code] = frozenset()


def _source_values(
    record: Mapping[str, JsonScalar], fields: Sequence[str]
) -> tuple[JsonScalar, ...]:
    return tuple(record.get(name) for name in fields)


def _has_source_value(values: tuple[JsonScalar, ...]) -> bool:
    return any(v is not None for v in values)


def _show(record: Mapping[str, JsonScalar], fields: Sequence[str], output: JsonScalar) -> str:
    inputs = ", ".join(repr(record.get(name)) for name in fields)
    return f"{inputs} -> {output!r}"


def _null_and_constant(
    proposal: Proposal,
    transformation: Transformation,
    rows: list[tuple[Mapping[str, JsonScalar], JsonScalar]],
) -> list[Reason]:
    fields = proposal.source_fields
    if len(rows) < MIN_RECORDS:
        return []
    reasons: list[Reason] = []
    nulls = [(r, o) for r, o in rows if o is None]
    if len(nulls) / len(rows) > MOSTLY_NULL_SHARE:
        examples = "; ".join(_show(r, fields, o) for r, o in nulls[:SHOWN_EXAMPLES])
        reasons.append(
            Reason(
                Code.MOSTLY_NULL_OUTPUT,
                f"{len(nulls)}/{len(rows)} sample records with a non-null source produce null "
                f"(e.g. {examples})",
            )
        )
    elif proposal.mapping_type is not MappingType.CONSTANT:
        outputs = {repr(o) for _, o in rows}
        distinct_inputs = {_source_values(r, fields) for r, _ in rows}
        if len(outputs) == 1 and len(distinct_inputs) >= MIN_RECORDS:
            first = rows[0]
            reasons.append(
                Reason(
                    Code.CONSTANT_OUTPUT,
                    f"every one of {len(rows)} sample records produces {first[1]!r} although "
                    f"{len(distinct_inputs)} distinct source values occur",
                )
            )
    return reasons


def _collapse(
    proposal: Proposal,
    source_fields: Mapping[str, Field],
    rows: list[tuple[Mapping[str, JsonScalar], JsonScalar]],
) -> list[Reason]:
    names = proposal.source_fields
    if len(names) != 1 or not source_fields[names[0]].enum_values:
        return []
    by_input: dict[str, str] = {}
    for record, output in rows:
        by_input.setdefault(repr(record.get(names[0])), repr(output))
    if len(by_input) < MIN_RECORDS:
        return []
    merged = len(by_input) - len(set(by_input.values()))
    if merged <= 0:
        return []
    return [
        Reason(
            Code.LOSSY_COLLAPSE,
            f"{len(by_input)} distinct values of {names[0]} give only "
            f"{len(set(by_input.values()))} distinct outputs",
        )
    ]


def _truncation(
    transformation: Transformation, samples: Sequence[Mapping[str, JsonScalar]]
) -> list[Reason]:
    reasons: list[Reason] = []
    for position, step in enumerate(transformation.steps):
        if not isinstance(step, SplitPart) or position == 0:
            continue
        prefix = Transformation(steps=transformation.steps[:position])
        for record in samples:
            try:
                value = execute(prefix, record)
            except TransformError:
                continue
            if not isinstance(value, str):
                continue
            parts = value.split(step.separator)
            used = step.index if step.index >= 0 else len(parts) + step.index
            discarded = [p for i, p in enumerate(parts) if i != used]
            if 0 <= used < len(parts) and any(p.strip() for p in discarded):
                reasons.append(
                    Reason(
                        Code.LOSSY_TRUNCATION,
                        f"SPLIT_PART keeps part {step.index} and discards {discarded} "
                        f"(e.g. {value!r} -> {parts[used]!r})",
                    )
                )
                break
    return reasons


def validate_proposal_v2(
    proposal: Proposal,
    *,
    source_fields: Mapping[str, Field],
    target_field: Field,
    samples: Sequence[Mapping[str, JsonScalar]],
    retrieval: RetrievalSignal | None = None,
) -> ValidationResult:
    """Every v1 check, unchanged, plus the v2 data checks. Only ever adds reasons."""
    base = validate_proposal(
        proposal,
        source_fields=source_fields,
        target_field=target_field,
        samples=samples,
        retrieval=retrieval,
    )
    transformation = proposal.transformation
    if (
        proposal.mapping_type is MappingType.UNRESOLVED
        or transformation is None
        or base.status is Status.FAIL
        or not proposal.source_fields
    ):
        return base

    rows: list[tuple[Mapping[str, JsonScalar], JsonScalar]] = []
    for record in samples:
        if not _has_source_value(_source_values(record, proposal.source_fields)):
            continue
        try:
            rows.append((record, execute(transformation, record)))
        except TransformError:
            return base  # v1 already reports execution errors

    extra = _null_and_constant(proposal, transformation, rows)
    if not any(r.code is Code.INFORMATION_LOSS_ENUM for r in base.reasons):
        extra += _collapse(proposal, source_fields, rows)
    extra += _truncation(transformation, samples)
    if not extra:
        return base
    return ValidationResult(
        status=Status.WARN,
        reasons=(*base.reasons, *extra),
        outputs_preview=base.outputs_preview,
        samples_checked=base.samples_checked,
    )


def review_decision_v2(
    mapping_type: MappingType,
    validation: ValidationResult,
    score: float | None,
    *,
    forcing: frozenset[Code] = REVIEW_FORCING_CODES_V2,
    threshold: float | None = None,
) -> tuple[ReviewStatus, tuple[str, ...]]:
    """Same rules as the v1 review decision, with the v2 set of review-forcing warnings."""
    limit = THRESHOLD if threshold is None else threshold
    reasons: list[str] = []
    if mapping_type is MappingType.UNRESOLVED:
        reasons.append("unresolved")
    if validation.status is Status.FAIL:
        reasons.append("validation_failed")
    if validation.ambiguous:
        reasons.append("ambiguous")
    reasons.extend(f"warning:{r.code.value}" for r in validation.reasons if r.code in forcing)
    if score is not None and score < limit:
        reasons.append(f"low_confidence<{limit}")
    status = ReviewStatus.NEEDS_REVIEW if reasons else ReviewStatus.AUTO_ACCEPTED
    return status, tuple(reasons)
