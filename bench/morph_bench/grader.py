"""The grader: scores proposed mappings against a scenario's hand-written answer key.

Only this module (and the code that calls it) reads answer keys; the mapping pipeline never
does. A mapping is *fully correct* when the proposed source set equals the expected one and the
proposed pipeline, executed on every input -> output example of the key, reproduces every
output (and, where the key expects a review flag, the proposal is flagged), or when the key says
UNRESOLVED and the proposal is UNRESOLVED too. Producing a value for an UNRESOLVED field is a
guess and is scored wrong however plausible it looks.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.mapping.transform import JsonScalar, Transformation, TransformError, execute

from morph_bench.models import Bundle, MappingEntry, MappingType

UNRESOLVED = MappingType.UNRESOLVED.value


@dataclass(frozen=True)
class ProposedMapping:
    """What the grader needs from a proposal; independent of how it was produced."""

    target_field: str
    mapping_type: str
    source_fields: tuple[str, ...]
    transformation: Transformation | None
    flagged: bool  # reached a human: review status NEEDS_REVIEW
    confidence: float | None = None
    invalid_output: bool = False


@dataclass(frozen=True)
class FieldGrade:
    target_field: str
    expected_type: str
    proposed_type: str | None
    source_match: bool | None  # None when the key says UNRESOLVED
    type_match: bool
    transformation_correct: bool | None
    unresolved_correct: bool | None  # None unless the key says UNRESOLVED
    flag_correct: bool | None  # None unless the key expects a review flag
    guessed: bool  # the key says UNRESOLVED but a value was proposed
    fully_correct: bool
    flagged: bool
    confidence: float | None
    invalid_output: bool
    detail: str  # why it is wrong, empty when fully correct


@dataclass(frozen=True)
class ScenarioGrade:
    scenario_id: str
    fields: tuple[FieldGrade, ...]

    @property
    def correct(self) -> int:
        return sum(1 for f in self.fields if f.fully_correct)

    def rate(self, attribute: str) -> tuple[int, int]:
        """(count of True, count of graded) for a boolean per-field attribute that may be None."""
        values = [getattr(f, attribute) for f in self.fields if getattr(f, attribute) is not None]
        return sum(1 for v in values if v), len(values)


def _run_examples(entry: MappingEntry, transformation: Transformation) -> str | None:
    """None when the pipeline reproduces every example, else a description of the first miss."""
    for example in entry.examples:
        record: dict[str, JsonScalar] = dict(example.input)
        try:
            result = execute(transformation, record)
        except TransformError as exc:
            return f"on {example.input}: {type(exc).__name__}: {exc}"
        if result != example.output or type(result) is not type(example.output):
            return f"on {example.input}: produced {result!r}, expected {example.output!r}"
    return None


def _missing(entry: MappingEntry) -> FieldGrade:
    return FieldGrade(
        target_field=entry.target_field,
        expected_type=entry.mapping_type.value,
        proposed_type=None,
        source_match=None if entry.mapping_type is MappingType.UNRESOLVED else False,
        type_match=False,
        transformation_correct=None if entry.mapping_type is MappingType.UNRESOLVED else False,
        unresolved_correct=False if entry.mapping_type is MappingType.UNRESOLVED else None,
        flag_correct=False if entry.expects_review else None,
        guessed=False,
        fully_correct=False,
        flagged=False,
        confidence=None,
        invalid_output=False,
        detail="no proposal for this target field",
    )


def grade_field(entry: MappingEntry, proposal: ProposedMapping | None) -> FieldGrade:
    if proposal is None:
        return _missing(entry)
    expected_unresolved = entry.mapping_type is MappingType.UNRESOLVED
    proposed_unresolved = proposal.mapping_type == UNRESOLVED
    common: dict[str, Any] = {
        "target_field": entry.target_field,
        "expected_type": entry.mapping_type.value,
        "proposed_type": proposal.mapping_type,
        "type_match": proposal.mapping_type == entry.mapping_type.value,
        "flagged": proposal.flagged,
        "confidence": proposal.confidence,
        "invalid_output": proposal.invalid_output,
    }
    if expected_unresolved:
        return FieldGrade(
            **common,
            source_match=None,
            transformation_correct=None,
            unresolved_correct=proposed_unresolved,
            flag_correct=None,
            guessed=not proposed_unresolved,
            fully_correct=proposed_unresolved,
            detail=""
            if proposed_unresolved
            else "guessed a value for a field the key marks UNRESOLVED",
        )

    expected_sources = set(entry.source_fields)
    source_match = (not proposed_unresolved) and set(proposal.source_fields) == expected_sources
    miss: str | None
    if proposal.transformation is None or proposed_unresolved:
        miss = (
            "proposed UNRESOLVED for a resolvable field" if proposed_unresolved else "no pipeline"
        )
        transformation_correct = False
    else:
        miss = _run_examples(entry, proposal.transformation)
        transformation_correct = miss is None
    flag_correct = proposal.flagged if entry.expects_review else None
    fully = source_match and transformation_correct and flag_correct is not False
    problems: list[str] = []
    if not source_match:
        problems.append(
            f"sources {sorted(proposal.source_fields)} != expected {sorted(expected_sources)}"
        )
    if miss:
        problems.append(miss)
    if flag_correct is False:
        problems.append("a review flag was expected but the mapping was not flagged")
    return FieldGrade(
        **common,
        source_match=source_match,
        transformation_correct=transformation_correct,
        unresolved_correct=None,
        flag_correct=flag_correct,
        guessed=False,
        fully_correct=fully,
        detail="; ".join(problems),
    )


def grade_scenario(bundle: Bundle, proposals: Sequence[ProposedMapping]) -> ScenarioGrade:
    """Grade one scenario. Targets without a proposal are scored wrong; extras are ignored."""
    by_target = {p.target_field: p for p in proposals}
    grades = tuple(
        grade_field(entry, by_target.get(entry.target_field))
        for entry in bundle.answer_key.mappings
    )
    return ScenarioGrade(bundle.scenario.id, grades)
