"""Pydantic models for MORPH-Bench scenarios and answer keys.

A scenario directory holds two hand-written, read-only fixtures:

* ``scenario.yaml``   - what is to be integrated and under which fault profile.
* ``answer_key.yaml`` - the correct field-level mapping, expressed as examples, never code.

Agents never write or modify either file. Models are frozen so loaded data cannot be mutated
by accident.
"""

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SystemName = Literal["crm", "support"]


class MappingType(StrEnum):
    DIRECT = "DIRECT"  # one source field copied unchanged (the target name may differ)
    COMPOSITE = "COMPOSITE"  # several source fields combined into one target value
    TRANSFORMATION = "TRANSFORMATION"  # one source field re-encoded: format, type or vocabulary
    CONSTANT = "CONSTANT"  # fixed value, no source field
    DERIVED = "DERIVED"  # value computed from a different concept, e.g. segment -> tier
    UNRESOLVED = "UNRESOLVED"  # no correct mapping exists; MORPH must say so, not guess


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RecordRewrite(_Frozen):
    """Overwrite fields of the one JSON object whose ``match_key`` equals ``match_value``."""

    match_key: str
    match_value: Any
    set: dict[str, Any]


class FaultProfile(_Frozen):
    """Mirror of the mock systems' ``/__admin/faults`` body (a test keeps the two in sync)."""

    seed: int = 0
    http_500_rate: float = Field(default=0.0, ge=0, le=1)
    http_429_rate: float = Field(default=0.0, ge=0, le=1)
    retry_after_seconds: int = Field(default=1, ge=0)
    malformed_json_rate: float = Field(default=0.0, ge=0, le=1)
    http_500_first_n: int = Field(default=0, ge=0)
    malformed_json_first_n: int = Field(default=0, ge=0)
    rewrite_records: tuple[RecordRewrite, ...] = ()
    duplicate_first_list_item: bool = False
    latency_ms: int = Field(default=0, ge=0, le=60_000)
    timeout: bool = False
    timeout_seconds: float = Field(default=30.0, ge=0, le=300)
    drop_fields: tuple[str, ...] = ()
    contract_version: Literal["v1", "v2"] = "v1"


class EntityRef(_Frozen):
    system: SystemName = Field(description="Which mock system the entity lives in.")
    entity: str = Field(description="Schema name of the entity in that system OpenAPI spec.")
    contract: Literal["v1", "v2"] = Field(
        default="v1", description="Which published contract version of the system is used."
    )


class Scenario(_Frozen):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="Unique id; equals the folder name.")
    description: str = Field(min_length=1)
    source: EntityRef
    target: EntityRef
    requirement: str = Field(
        min_length=1, description="The integration request in natural language, as a user would."
    )
    fault_profile: FaultProfile = Field(
        default_factory=FaultProfile, description="Faults applied to the systems for this scenario."
    )
    spec_transform: Literal["none", "strip_docs"] = Field(
        default="none",
        description="Deterministic transform applied to both specs before use (strip_docs removes "
        "every description and example).",
    )

    @model_validator(mode="after")
    def _systems_differ(self) -> Self:
        if self.source.system == self.target.system:
            raise ValueError("source and target must be different systems")
        return self


class ExamplePair(_Frozen):
    """One input -> output example of the expected transformation. Never code."""

    input: dict[str, Any] = Field(
        description="Source field name -> value. Must contain exactly the entry's source_fields."
    )
    output: Any = Field(description="The expected value of the target field for that input.")


class MappingEntry(_Frozen):
    target_field: str
    mapping_type: MappingType
    source_fields: tuple[str, ...] = ()
    examples: tuple[ExamplePair, ...] = ()
    notes: str | None = Field(default=None, description="Why this is the correct mapping.")
    reason: str | None = Field(default=None, description="Required for UNRESOLVED entries only.")
    expects_review: bool = Field(
        default=False,
        description="A correct proposal must also be flagged for review (validation WARN or "
        "NEEDS_REVIEW), e.g. a nullable source feeding a required target.",
    )
    review_note: str | None = Field(
        default=None, description="Why a review flag is expected; required with expects_review."
    )

    @model_validator(mode="after")
    def _shape_matches_mapping_type(self) -> Self:
        problems = _shape_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self


def _shape_problems(entry: MappingEntry) -> list[str]:
    kind = entry.mapping_type
    sources = entry.source_fields
    problems: list[str] = []
    if len(set(sources)) != len(sources):
        problems.append("source_fields contains duplicates")
    if kind is MappingType.DIRECT and len(sources) != 1:
        problems.append("DIRECT needs exactly one source field")
    if kind is MappingType.COMPOSITE and len(sources) < 2:
        problems.append("COMPOSITE needs at least two source fields")
    if kind in (MappingType.TRANSFORMATION, MappingType.DERIVED) and not sources:
        problems.append(f"{kind} needs at least one source field")
    if kind in (MappingType.CONSTANT, MappingType.UNRESOLVED) and sources:
        problems.append(f"{kind} must not list source fields")
    if kind is MappingType.UNRESOLVED:
        if entry.examples:
            problems.append("UNRESOLVED must not have examples")
        if not entry.reason:
            problems.append("UNRESOLVED needs a reason")
    else:
        if not entry.examples:
            problems.append(f"{kind} needs at least one example")
        if entry.reason:
            problems.append("reason is only for UNRESOLVED entries")
    for example in entry.examples:
        if set(example.input) != set(sources):
            problems.append(
                f"example input keys {sorted(example.input)} must equal source_fields "
                f"{sorted(sources)}"
            )
        if kind is MappingType.DIRECT and len(sources) == 1:
            if example.output != example.input.get(sources[0]):
                problems.append("DIRECT example output must equal its input value")
    if entry.expects_review and not entry.review_note:
        problems.append("expects_review needs a review_note")
    if entry.review_note and not entry.expects_review:
        problems.append("review_note is only for entries with expects_review")
    if kind is MappingType.CONSTANT and len({repr(e.output) for e in entry.examples}) > 1:
        problems.append("CONSTANT examples must all have the same output")
    return problems


class AnswerKey(_Frozen):
    scenario_id: str
    mappings: tuple[MappingEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_entry_per_target_field(self) -> Self:
        names = [m.target_field for m in self.mappings]
        duplicated = sorted({n for n in names if names.count(n) > 1})
        if duplicated:
            raise ValueError(f"target fields mapped more than once: {duplicated}")
        return self

    def entry_for(self, target_field: str) -> MappingEntry | None:
        return next((m for m in self.mappings if m.target_field == target_field), None)


class Bundle(_Frozen):
    """A scenario together with its answer key, both validated against the real contracts."""

    scenario: Scenario
    answer_key: AnswerKey
    answer_key_sha256: str = Field(description="Fingerprint of the answer-key file as read.")
