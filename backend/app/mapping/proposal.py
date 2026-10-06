"""What the LLM returns (a structured proposal) and its typed, validated internal form.

The LLM-facing model is deliberately flat so it works with strict structured-output modes:
every step carries the same nullable parameters. ``LLMProposal.to_proposal`` turns it into the
typed DSL; anything that does not convert is invalid output and triggers the single re-ask.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.mapping.transform import JsonScalar, Transformation

OPS = Literal[
    "COPY",
    "JOIN_NONNULL",
    "COALESCE",
    "CONSTANT",
    "CAST",
    "STRIP_PREFIX",
    "ADD_PREFIX",
    "REGEX_EXTRACT",
    "REGEX_REPLACE",
    "MAP_ENUM",
    "FORMAT_DATETIME",
    "SPLIT_PART",
]
DatetimeFormat = Literal["iso8601", "epoch_s", "epoch_ms"]
INVALID_OUTPUT_REASON = "invalid_llm_output"


class MappingType(StrEnum):
    DIRECT = "DIRECT"
    COMPOSITE = "COMPOSITE"
    TRANSFORMATION = "TRANSFORMATION"
    CONSTANT = "CONSTANT"
    DERIVED = "DERIVED"
    UNRESOLVED = "UNRESOLVED"


class Certainty(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class EnumPair(BaseModel):
    source: str
    target: JsonScalar


class FlatStep(BaseModel):
    """One DSL step with every parameter present and unused ones null."""

    op: OPS
    field: str | None
    fields: list[str] | None
    separator: str | None
    trim: bool | None
    prefix: str | None
    pattern: str | None
    group: int | None
    replacement: str | None
    mapping: list[EnumPair] | None
    on_unmapped: Literal["error", "default"] | None
    default: JsonScalar
    to: Literal["int", "str", "float", "bool"] | None
    from_format: DatetimeFormat | None
    to_format: DatetimeFormat | None
    value: JsonScalar
    index: int | None

    def to_dsl(self) -> dict[str, object]:
        """The step as a typed-DSL dict (validated later with the whole pipeline)."""
        raw: dict[str, object]
        match self.op:
            case "COPY":
                raw = {"field": self.field}
            case "JOIN_NONNULL":
                raw = {"fields": self.fields, "separator": self.separator, "trim": self.trim}
            case "COALESCE":
                raw = {"fields": self.fields}
            case "CONSTANT":
                raw = {"value": self.value}
            case "CAST":
                raw = {"to": self.to}
            case "STRIP_PREFIX" | "ADD_PREFIX":
                raw = {"prefix": self.prefix}
            case "REGEX_EXTRACT":
                raw = {"pattern": self.pattern, "group": self.group}
            case "REGEX_REPLACE":
                raw = {"pattern": self.pattern, "replacement": self.replacement}
            case "MAP_ENUM":
                raw = {
                    "mapping": {p.source: p.target for p in self.mapping or []},
                    "on_unmapped": self.on_unmapped,
                    "default": self.default,
                }
            case "FORMAT_DATETIME":
                raw = {"from_format": self.from_format, "to_format": self.to_format}
            case "SPLIT_PART":
                raw = {"separator": self.separator, "index": self.index}
        # Unset optional parameters are omitted so the DSL defaults apply; required ones stay
        # (as null) and the DSL validation reports them.
        optional = {"separator", "trim", "group", "on_unmapped"}
        step: dict[str, object] = {"op": self.op}
        for key, value in raw.items():
            if value is None and key in optional:
                continue
            if key == "default" and self.on_unmapped != "default":
                continue
            step[key] = value
        return step


class Alternative(BaseModel):
    source_fields: list[str]
    reason: str = Field(max_length=300)


class LLMProposal(BaseModel):
    """The structured reply requested from the LLM for one target field."""

    model_config = ConfigDict(extra="forbid")

    target_field: str
    mapping_type: MappingType
    source_fields: list[str]
    steps: list[FlatStep]
    unresolved_reason: str | None
    rationale: str = Field(max_length=500)
    alternatives: list[Alternative]
    certainty: Certainty

        """Convert to the typed form; any problem raises ValueError (which triggers the re-ask)."""
        """Convert to the typed form. Raises ValueError (and so triggers the re-ask) on any problem."""
        if self.target_field != expected_target:
            raise ValueError(
                f"target_field must be {expected_target!r} but the reply says {self.target_field!r}"
            )
        transformation: Transformation | None = None
        if self.mapping_type is MappingType.UNRESOLVED:
            if self.steps:
                raise ValueError("an UNRESOLVED proposal must have no steps")
            if not self.unresolved_reason:
                raise ValueError("an UNRESOLVED proposal needs an unresolved_reason")
        else:
            if not self.steps:
                raise ValueError(f"a {self.mapping_type} proposal needs at least one step")
            try:
                transformation = Transformation.model_validate(
                    {"steps": [step.to_dsl() for step in self.steps]}
                )
            except ValidationError as exc:
                problems = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
                )
                raise ValueError(f"invalid transformation: {problems}") from exc
        return Proposal(
            target_field=self.target_field,
            mapping_type=self.mapping_type,
            source_fields=tuple(self.source_fields),
            transformation=transformation,
            unresolved_reason=self.unresolved_reason,
            rationale=self.rationale,
            alternatives=tuple((tuple(a.source_fields), a.reason) for a in self.alternatives),
            certainty=self.certainty,
        )


class Proposal(BaseModel):
    """A proposal in typed form: the DSL pipeline is already validated and executable."""

    model_config = ConfigDict(frozen=True)

    target_field: str
    mapping_type: MappingType
    source_fields: tuple[str, ...]
    transformation: Transformation | None
    unresolved_reason: str | None = None
    rationale: str = ""
    alternatives: tuple[tuple[tuple[str, ...], str], ...] = ()
    certainty: Certainty = Certainty.LOW

    @classmethod
    def unresolved(cls, target_field: str, reason: str, *, rationale: str = "") -> "Proposal":
        return cls(
            target_field=target_field,
            mapping_type=MappingType.UNRESOLVED,
            source_fields=(),
            transformation=None,
            unresolved_reason=reason,
            rationale=rationale,
        )
