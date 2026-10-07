"""Deterministic validation of mapping proposals. Plain code: no LLM, no I/O.

A proposal is checked statically (do the fields exist, is the pipeline consistent with the
claimed mapping type) and dynamically (run it on every sample record and check each output
against the target field's type, format, enum, pattern, range, length and nullability).
"""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.discovery.models import Field
from app.mapping.proposal import MappingType, Proposal
from app.mapping.transform import (
    Coalesce,
    Constant,
    JoinNonNull,
    JsonScalar,
    MapEnum,
    TransformError,
    execute,
)

MAX_REASON_SAMPLES = 3


class Status(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class Code(StrEnum):
    # FAIL: the proposal is wrong or unusable
    SOURCE_FIELD_MISSING = "SOURCE_FIELD_MISSING"
    SOURCES_MISMATCH = "SOURCES_MISMATCH"
    TYPE_INCONSISTENT = "TYPE_INCONSISTENT"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    FORMAT_MISMATCH = "FORMAT_MISMATCH"
    ENUM_VIOLATION = "ENUM_VIOLATION"
    PATTERN_VIOLATION = "PATTERN_VIOLATION"
    RANGE_VIOLATION = "RANGE_VIOLATION"
    LENGTH_VIOLATION = "LENGTH_VIOLATION"
    NULL_FOR_REQUIRED_TARGET = "NULL_FOR_REQUIRED_TARGET"
    # WARN: valid, but a human should look
    NULLABLE_TO_REQUIRED = "NULLABLE_TO_REQUIRED"
    INFORMATION_LOSS_ENUM = "INFORMATION_LOSS_ENUM"
    INFORMATION_LOSS_COALESCE = "INFORMATION_LOSS_COALESCE"
    AMBIGUOUS_ALTERNATIVES = "AMBIGUOUS_ALTERNATIVES"
    AMBIGUOUS_RETRIEVAL = "AMBIGUOUS_RETRIEVAL"
    NO_SAMPLES = "NO_SAMPLES"
    UNRESOLVED = "UNRESOLVED"
    # raised only by validator v2 (app/mapping/validate_v2.py); added after the v1 results
    MOSTLY_NULL_OUTPUT = "MOSTLY_NULL_OUTPUT"
    CONSTANT_OUTPUT = "CONSTANT_OUTPUT"
    LOSSY_COLLAPSE = "LOSSY_COLLAPSE"
    LOSSY_TRUNCATION = "LOSSY_TRUNCATION"
    # run level
    TARGET_NOT_COVERED = "TARGET_NOT_COVERED"
    DUPLICATE_TARGET_COVERAGE = "DUPLICATE_TARGET_COVERAGE"
    REQUIRED_TARGET_UNRESOLVED = "REQUIRED_TARGET_UNRESOLVED"


FAIL_CODES = frozenset(
    {
        Code.SOURCE_FIELD_MISSING,
        Code.SOURCES_MISMATCH,
        Code.TYPE_INCONSISTENT,
        Code.EXECUTION_ERROR,
        Code.TYPE_MISMATCH,
        Code.FORMAT_MISMATCH,
        Code.ENUM_VIOLATION,
        Code.PATTERN_VIOLATION,
        Code.RANGE_VIOLATION,
        Code.LENGTH_VIOLATION,
        Code.NULL_FOR_REQUIRED_TARGET,
        Code.TARGET_NOT_COVERED,
        Code.DUPLICATE_TARGET_COVERAGE,
    }
)
AMBIGUITY_CODES = frozenset({Code.AMBIGUOUS_ALTERNATIVES, Code.AMBIGUOUS_RETRIEVAL})


@dataclass(frozen=True)
class Reason:
    code: Code
    detail: str

    @property
    def severity(self) -> Status:
        return Status.FAIL if self.code in FAIL_CODES else Status.WARN


@dataclass(frozen=True)
class ValidationResult:
    status: Status
    reasons: tuple[Reason, ...]
    outputs_preview: tuple[JsonScalar, ...] = ()
    samples_checked: int = 0

    def has(self, code: Code) -> bool:
        return any(r.code is code for r in self.reasons)

    @property
    def ambiguous(self) -> bool:
        return any(r.code in AMBIGUITY_CODES for r in self.reasons)


@dataclass(frozen=True)
class RetrievalSignal:
    """How retrieval ranked the source fields for this target field."""

    ranks: Mapping[str, int | None]  # source field -> 1-based rank among source fields
    top_gap: float | None  # distance gap between the two nearest candidates
    top_two: tuple[str, ...] = ()


AMBIGUITY_EPSILON = 0.02


def _status(reasons: Sequence[Reason]) -> Status:
    if any(r.severity is Status.FAIL for r in reasons):
        return Status.FAIL
    return Status.WARN if reasons else Status.PASS


def _type_ok(field: Field, value: JsonScalar) -> bool:
    kinds = field.json_type.split("|")
    for kind in kinds:
        if kind == "string" and isinstance(value, str):
            return True
        if kind == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if kind == "number" and isinstance(value, int | float) and not isinstance(value, bool):
            return True
        if kind == "boolean" and isinstance(value, bool):
            return True
        if kind in ("any", "object", "array"):
            return True
    return False


def _output_problem(field: Field, value: JsonScalar) -> Reason | None:
    """The first way ``value`` breaks the target field's contract, or None."""
    if value is None:
        if field.nullable:
            return None
        return Reason(Code.NULL_FOR_REQUIRED_TARGET, "null for a non-nullable target field")
    if not _type_ok(field, value):
        return Reason(
            Code.TYPE_MISMATCH,
            f"{value!r} is {type(value).__name__}, target type is {field.json_type}",
        )
    if field.enum_values and value not in field.enum_values:
        return Reason(Code.ENUM_VIOLATION, f"{value!r} is not one of {list(field.enum_values)}")
    if isinstance(value, str):
        if field.format == "date-time":
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return Reason(Code.FORMAT_MISMATCH, f"{value!r} is not an ISO 8601 date-time")
        c = field.constraints
        if c.pattern is not None and re.search(c.pattern, value) is None:
            return Reason(Code.PATTERN_VIOLATION, f"{value!r} does not match {c.pattern}")
        if c.min_length is not None and len(value) < c.min_length:
            return Reason(Code.LENGTH_VIOLATION, f"{value!r} is shorter than {c.min_length}")
        if c.max_length is not None and len(value) > c.max_length:
            return Reason(Code.LENGTH_VIOLATION, f"{value!r} is longer than {c.max_length}")
    if isinstance(value, int | float) and not isinstance(value, bool):
        c = field.constraints
        if c.minimum is not None and value < c.minimum:
            return Reason(Code.RANGE_VIOLATION, f"{value} is below the minimum {c.minimum}")
        if c.maximum is not None and value > c.maximum:
            return Reason(Code.RANGE_VIOLATION, f"{value} is above the maximum {c.maximum}")
    return None


def _can_be_null(proposal: Proposal, source_fields: Mapping[str, Field]) -> bool:
    """Whether the pipeline can produce null given which source fields are nullable."""
    transformation = proposal.transformation
    if transformation is None:
        return False
    first = transformation.steps[0]
    if isinstance(first, Constant):
        return first.value is None
    names = transformation.source_fields
    nullable = [source_fields[n].nullable for n in names if n in source_fields]
    if isinstance(first, JoinNonNull | Coalesce):
        return bool(nullable) and all(nullable)
    return any(nullable)


def _consistency(proposal: Proposal) -> list[Reason]:
    kind = proposal.mapping_type
    transformation = proposal.transformation
    if kind is MappingType.UNRESOLVED:
        return []
    if transformation is None:
        return [Reason(Code.TYPE_INCONSISTENT, f"{kind} proposal has no transformation")]
    first = transformation.steps[0]
    n_sources = len(proposal.source_fields)
    problems: list[str] = []
    if kind is MappingType.DIRECT and (len(transformation.steps) != 1 or n_sources != 1):
        problems.append("DIRECT must be a single COPY of one source field")
    if kind is MappingType.COMPOSITE and n_sources < 2:
        problems.append("COMPOSITE needs at least two source fields")
    if kind in (MappingType.TRANSFORMATION, MappingType.DERIVED) and n_sources < 1:
        problems.append(f"{kind} needs at least one source field")
    if kind is MappingType.CONSTANT and not (
        isinstance(first, Constant) and len(transformation.steps) == 1 and n_sources == 0
    ):
        problems.append("CONSTANT must be a single CONSTANT step with no source fields")
    if kind is not MappingType.CONSTANT and isinstance(first, Constant):
        problems.append("a CONSTANT step only fits a CONSTANT mapping")
    reasons = [Reason(Code.TYPE_INCONSISTENT, p) for p in problems]
    if set(transformation.source_fields) != set(proposal.source_fields):
        reasons.append(
            Reason(
                Code.SOURCES_MISMATCH,
                f"source_fields {list(proposal.source_fields)} differ from the fields the "
                f"pipeline reads {list(transformation.source_fields)}",
            )
        )
    return reasons


def _information_loss(proposal: Proposal) -> list[Reason]:
    transformation = proposal.transformation
    if transformation is None:
        return []
    reasons: list[Reason] = []
    first = transformation.steps[0]
    if isinstance(first, Coalesce) and len(first.fields) > 1:
        reasons.append(
            Reason(
                Code.INFORMATION_LOSS_COALESCE,
                f"COALESCE keeps one of {list(first.fields)}; the others are ignored",
            )
        )
    for step in transformation.steps:
        if isinstance(step, MapEnum):
            values = [repr(v) for v in step.mapping.values()]
            repeated = sorted(v for v, n in Counter(values).items() if n > 1)
            if repeated:
                reasons.append(
                    Reason(
                        Code.INFORMATION_LOSS_ENUM,
                        f"several source values map to the same target value: {repeated}",
                    )
                )
    return reasons


def _ambiguity(proposal: Proposal, retrieval: RetrievalSignal | None) -> list[Reason]:
    reasons: list[Reason] = []
    chosen = set(proposal.source_fields)
    other = [alt for alt, _ in proposal.alternatives if set(alt) != chosen and alt]
    if other:
        reasons.append(
            Reason(
                Code.AMBIGUOUS_ALTERNATIVES,
                "the model listed alternative sources: " + "; ".join(str(list(a)) for a in other),
            )
        )
    if (
        retrieval is not None
        and retrieval.top_gap is not None
        and retrieval.top_gap < AMBIGUITY_EPSILON
        and len(chosen & set(retrieval.top_two)) == 1
    ):
        reasons.append(
            Reason(
                Code.AMBIGUOUS_RETRIEVAL,
                f"one of the two nearest source fields {list(retrieval.top_two)} are within "
                f"{AMBIGUITY_EPSILON} of each other",
            )
        )
    return reasons


def validate_proposal(
    proposal: Proposal,
    *,
    source_fields: Mapping[str, Field],
    target_field: Field,
    samples: Sequence[Mapping[str, JsonScalar]],
    retrieval: RetrievalSignal | None = None,
) -> ValidationResult:
    """Check one proposal. ``source_fields`` and ``samples`` describe the source entity."""
    reasons: list[Reason] = []
    if proposal.mapping_type is MappingType.UNRESOLVED:
        detail = proposal.unresolved_reason or "no reason given"
        return ValidationResult(Status.WARN, (Reason(Code.UNRESOLVED, detail),))

    for name in proposal.source_fields:
        if name not in source_fields:
            reasons.append(
                Reason(Code.SOURCE_FIELD_MISSING, f"source field {name!r} does not exist")
            )
    reasons.extend(_consistency(proposal))
    if any(r.code is Code.SOURCE_FIELD_MISSING for r in reasons):
        return ValidationResult(_status(reasons), tuple(reasons))
    transformation = proposal.transformation
    if transformation is None:  # already reported as TYPE_INCONSISTENT
        return ValidationResult(_status(reasons), tuple(reasons))

    reasons.extend(_information_loss(proposal))
    reasons.extend(_ambiguity(proposal, retrieval))
    if not target_field.nullable and _can_be_null(proposal, source_fields):
        reasons.append(
            Reason(
                Code.NULLABLE_TO_REQUIRED,
                "a nullable source field feeds a non-nullable target field",
            )
        )

    outputs: list[JsonScalar] = []
    failures: dict[Code, list[str]] = {}
    null_outputs = 0
    if not samples:
        reasons.append(Reason(Code.NO_SAMPLES, "no sample records, so nothing was executed"))
    for index, record in enumerate(samples):
        try:
            value = execute(transformation, record)
        except TransformError as exc:
            failures.setdefault(Code.EXECUTION_ERROR, []).append(
                f"sample {index}: {type(exc).__name__}: {exc}"
            )
            continue
        outputs.append(value)
        if value is None and not target_field.nullable:
            null_outputs += 1
            continue
        problem = _output_problem(target_field, value)
        if problem is not None:
            failures.setdefault(problem.code, []).append(f"sample {index}: {problem.detail}")
    for code, details in failures.items():
        shown = "; ".join(details[:MAX_REASON_SAMPLES])
        extra = (
            f" (+{len(details) - MAX_REASON_SAMPLES} more)"
            if len(details) > MAX_REASON_SAMPLES
            else ""
        )
        reasons.append(Reason(code, f"{len(details)}/{len(samples)} samples: {shown}{extra}"))
    if null_outputs and not any(r.code is Code.NULLABLE_TO_REQUIRED for r in reasons):
        reasons.append(
            Reason(
                Code.NULL_FOR_REQUIRED_TARGET,
                f"{null_outputs}/{len(samples)} samples produce null for a non-nullable target",
            )
        )
    return ValidationResult(
        _status(reasons), tuple(reasons), tuple(outputs[:5]), samples_checked=len(samples)
    )


def validate_run(target_fields: Sequence[Field], proposals: Sequence[Proposal]) -> list[Reason]:
    """Run-level checks: every target covered exactly once; required targets not left unresolved."""
    reasons: list[Reason] = []
    counts = Counter(p.target_field for p in proposals)
    by_name = {p.target_field: p for p in proposals}
    for field in target_fields:
        n = counts.get(field.path, 0)
        if n == 0:
            reasons.append(Reason(Code.TARGET_NOT_COVERED, f"{field.path} has no proposal"))
        elif n > 1:
            reasons.append(
                Reason(Code.DUPLICATE_TARGET_COVERAGE, f"{field.path} has {n} proposals")
            )
        elif field.required and by_name[field.path].mapping_type is MappingType.UNRESOLVED:
            reasons.append(
                Reason(
                    Code.REQUIRED_TARGET_UNRESOLVED, f"required field {field.path} is unresolved"
                )
            )
    return reasons
