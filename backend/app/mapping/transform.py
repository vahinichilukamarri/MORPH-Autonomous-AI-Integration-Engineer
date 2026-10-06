"""The transformation DSL: a closed, typed, deterministic set of operations.

A transformation is an ordered pipeline over the named fields of one source record. The first
step produces a value from the record (a *source op*); every later step rewrites the running
value (a *value op*). The executor is plain Python: no eval, no exec, no I/O, typed errors only.

Null policy, per op (documented in docs/mapping.md):

* COPY: a null field gives null. A field missing from the record is an error.
* JOIN_NONNULL: null and empty parts are skipped; no parts left gives null.
* COALESCE: the first non-null field; all null gives null.
* CONSTANT: the value, which may itself be null.
* Every value op passes null through unchanged. Applying a string op to a non-string is an error
  (CAST first).
* MAP_ENUM: an unmapped key is an error unless ``on_unmapped`` is ``default``.
* REGEX_EXTRACT: no match gives null.
* SPLIT_PART: an index past the end gives null.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

JsonScalar = str | int | float | bool | None
Record = Mapping[str, JsonScalar]

MAX_PATTERN_LENGTH = 200
MAX_INPUT_LENGTH = 10_000
_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*][^)]*\)[+*{]")
_TRUE = {"true", "1"}
_FALSE = {"false", "0"}


class TransformError(Exception):
    """Base class of every error the executor can raise."""


class MissingFieldError(TransformError):
    pass


class TypeMismatchError(TransformError):
    pass


class UnmappedValueError(TransformError):
    pass


class DatetimeError(TransformError):
    pass


class _Step(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---- source ops --------------------------------------------------------------------------------


class Copy(_Step):
    op: Literal["COPY"] = "COPY"
    field: str


class JoinNonNull(_Step):
    op: Literal["JOIN_NONNULL"] = "JOIN_NONNULL"
    fields: tuple[str, ...] = Field(min_length=1)
    separator: str = " "
    trim: bool = True


class Coalesce(_Step):
    op: Literal["COALESCE"] = "COALESCE"
    fields: tuple[str, ...] = Field(min_length=1)


class Constant(_Step):
    op: Literal["CONSTANT"] = "CONSTANT"
    value: JsonScalar = None


# ---- value ops ---------------------------------------------------------------------------------


class Cast(_Step):
    op: Literal["CAST"] = "CAST"
    to: Literal["int", "str", "float", "bool"]


class StripPrefix(_Step):
    op: Literal["STRIP_PREFIX"] = "STRIP_PREFIX"
    prefix: str = Field(min_length=1)


class AddPrefix(_Step):
    op: Literal["ADD_PREFIX"] = "ADD_PREFIX"
    prefix: str = Field(min_length=1)


def _check_pattern(pattern: str) -> str:
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f"pattern longer than {MAX_PATTERN_LENGTH} characters")
    if _NESTED_QUANTIFIER.search(pattern):
        raise ValueError("pattern has a nested quantifier (risk of catastrophic backtracking)")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc
    return pattern


class RegexExtract(_Step):
    op: Literal["REGEX_EXTRACT"] = "REGEX_EXTRACT"
    pattern: str
    group: int = Field(default=1, ge=0)

    @field_validator("pattern")
    @classmethod
    def _safe(cls, value: str) -> str:
        return _check_pattern(value)

    @model_validator(mode="after")
    def _group_exists(self) -> "RegexExtract":
        if self.group > re.compile(self.pattern).groups:
            raise ValueError(f"group {self.group} does not exist in the pattern")
        return self


class RegexReplace(_Step):
    op: Literal["REGEX_REPLACE"] = "REGEX_REPLACE"
    pattern: str
    replacement: str

    @field_validator("pattern")
    @classmethod
    def _safe(cls, value: str) -> str:
        return _check_pattern(value)


class MapEnum(_Step):
    op: Literal["MAP_ENUM"] = "MAP_ENUM"
    mapping: dict[str, JsonScalar] = Field(min_length=1)
    on_unmapped: Literal["error", "default"] = "error"
    default: JsonScalar = None


DatetimeFormat = Literal["iso8601", "epoch_s", "epoch_ms"]


class FormatDatetime(_Step):
    op: Literal["FORMAT_DATETIME"] = "FORMAT_DATETIME"
    from_format: DatetimeFormat
    to_format: DatetimeFormat


class SplitPart(_Step):
    op: Literal["SPLIT_PART"] = "SPLIT_PART"
    separator: str = Field(min_length=1)
    index: int


SourceStep = Copy | JoinNonNull | Coalesce | Constant
ValueStep = (
    Cast
    | StripPrefix
    | AddPrefix
    | RegexExtract
    | RegexReplace
    | MapEnum
    | FormatDatetime
    | SplitPart
)
Step = Annotated[SourceStep | ValueStep, Field(discriminator="op")]
SOURCE_OPS = ("COPY", "JOIN_NONNULL", "COALESCE", "CONSTANT")


class Transformation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: tuple[Step, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> "Transformation":
        if self.steps[0].op not in SOURCE_OPS:
            raise ValueError(f"the first step must be one of {SOURCE_OPS}, not {self.steps[0].op}")
        later = [s.op for s in self.steps[1:] if s.op in SOURCE_OPS]
        if later:
            raise ValueError(f"source ops are only allowed as the first step, found {later}")
        return self

    @property
    def source_fields(self) -> tuple[str, ...]:
        """The record fields the pipeline reads, in order of first use."""
        first = self.steps[0]
        if isinstance(first, Copy):
            return (first.field,)
        if isinstance(first, JoinNonNull | Coalesce):
            return first.fields
        return ()


# ---- execution ---------------------------------------------------------------------------------


def _require_str(value: JsonScalar, op: str) -> str:
    if not isinstance(value, str):
        raise TypeMismatchError(f"{op} needs a string but got {type(value).__name__}")
    if len(value) > MAX_INPUT_LENGTH:
        raise TransformError(f"{op} input longer than {MAX_INPUT_LENGTH} characters")
    return value


def _field(record: Record, name: str) -> JsonScalar:
    if name not in record:
        raise MissingFieldError(f"record has no field {name!r}")
    return record[name]


def _to_text(value: JsonScalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _cast(value: JsonScalar, to: str) -> JsonScalar:
    try:
        if to == "str":
            return _to_text(value)
        if to == "bool":
            text = _to_text(value).strip().lower()
            if text in _TRUE:
                return True
            if text in _FALSE:
                return False
            raise TypeMismatchError(f"cannot cast {value!r} to bool")
        if isinstance(value, bool):
            raise TypeMismatchError(f"cannot cast a boolean to {to}")
        number: str | int | float = value  # type: ignore[assignment]
        if to == "float":
            return float(number.strip() if isinstance(number, str) else number)
        if isinstance(number, float):
            if not number.is_integer():
                raise TypeMismatchError(f"cannot cast {value} to int without losing data")
            return int(number)
        return int(number.strip()) if isinstance(number, str) else number
    except (ValueError, OverflowError) as exc:
        raise TypeMismatchError(f"cannot cast {value!r} to {to}") from exc


def _parse_datetime(value: JsonScalar, fmt: str) -> datetime:
    try:
        if fmt == "iso8601":
            text = _require_str(value, "FORMAT_DATETIME")
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise DatetimeError(f"epoch value must be a number, got {type(value).__name__}")
        seconds = value / 1000 if fmt == "epoch_ms" else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise DatetimeError(f"cannot read {value!r} as {fmt}") from exc


def _format_datetime(moment: datetime, fmt: str) -> JsonScalar:
    moment = moment.astimezone(UTC)
    if fmt == "epoch_s":
        return int(moment.timestamp() // 1)
    if fmt == "epoch_ms":
        return round(moment.timestamp() * 1000)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond // 1000:03d}"
    return text + "Z"


def _apply(step: Step, value: JsonScalar) -> JsonScalar:
    if value is None:
        return None
    if isinstance(step, Cast):
        return _cast(value, step.to)
    if isinstance(step, StripPrefix):
        text = _require_str(value, step.op)
        return text.removeprefix(step.prefix)
    if isinstance(step, AddPrefix):
        return step.prefix + _require_str(value, step.op)
    if isinstance(step, RegexExtract):
        found = re.search(step.pattern, _require_str(value, step.op))
        return found.group(step.group) if found else None
    if isinstance(step, RegexReplace):
        return re.sub(step.pattern, step.replacement, _require_str(value, step.op))
    if isinstance(step, MapEnum):
        key = _require_str(value, step.op)
        if key in step.mapping:
            return step.mapping[key]
        if step.on_unmapped == "default":
            return step.default
        raise UnmappedValueError(f"no mapping for {key!r}")
    if isinstance(step, FormatDatetime):
        return _format_datetime(_parse_datetime(value, step.from_format), step.to_format)
    if isinstance(step, SplitPart):
        parts = _require_str(value, step.op).split(step.separator)
        return parts[step.index] if -len(parts) <= step.index < len(parts) else None
    raise TransformError(f"{step.op} cannot be used after the first step")


def execute(transformation: Transformation, record: Record) -> JsonScalar:
    """Run a pipeline on one record. Raises only TransformError subclasses."""
    first = transformation.steps[0]
    value: JsonScalar
    if isinstance(first, Copy):
        value = _field(record, first.field)
    elif isinstance(first, JoinNonNull):
        parts: list[str] = []
        for name in first.fields:
            raw = _field(record, name)
            if raw is None:
                continue
            text = _to_text(raw)
            text = text.strip() if first.trim else text
            if text:
                parts.append(text)
        value = first.separator.join(parts) if parts else None
    elif isinstance(first, Coalesce):
        value = next((v for v in (_field(record, n) for n in first.fields) if v is not None), None)
    elif isinstance(first, Constant):
        value = first.value
    else:
        raise TransformError(f"{first.op} cannot be the first step")
    for step in transformation.steps[1:]:
        value = _apply(step, value)
    return value
