"""Transformation operations used by compiled field functions.

Hand-written counterpart of the mapping DSL executor (``app.mapping.transform``). Compiled code
only calls these functions with literal arguments; a differential test in the backend checks that
every compiled pipeline returns exactly what the DSL executor returns, errors included.

Null policy: every value op passes null through unchanged; string ops on a non-string raise.
"""

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

JsonScalar = str | int | float | bool | None
Record = Mapping[str, JsonScalar]

MAX_INPUT_LENGTH = 10_000
_TRUE = {"true", "1"}
_FALSE = {"false", "0"}


class TransformError(Exception):
    """Base class of every error the operations can raise."""


class MissingFieldError(TransformError):
    pass


class TypeMismatchError(TransformError):
    pass


class UnmappedValueError(TransformError):
    pass


class DatetimeError(TransformError):
    pass


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


# ---- source ops --------------------------------------------------------------------------------


def copy_field(record: Record, name: str) -> JsonScalar:
    return _field(record, name)


def join_nonnull(record: Record, names: tuple[str, ...], separator: str, trim: bool) -> JsonScalar:
    parts: list[str] = []
    for name in names:
        raw = _field(record, name)
        if raw is None:
            continue
        text = _to_text(raw)
        text = text.strip() if trim else text
        if text:
            parts.append(text)
    return separator.join(parts) if parts else None


def coalesce(record: Record, names: tuple[str, ...]) -> JsonScalar:
    return next((v for v in (_field(record, n) for n in names) if v is not None), None)


# ---- value ops ---------------------------------------------------------------------------------


def cast(value: JsonScalar, to: str) -> JsonScalar:
    if value is None:
        return None
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
        number: str | int | float = value
        if to == "float":
            return float(number.strip() if isinstance(number, str) else number)
        if isinstance(number, float):
            if not number.is_integer():
                raise TypeMismatchError(f"cannot cast {value} to int without losing data")
            return int(number)
        return int(number.strip()) if isinstance(number, str) else number
    except (ValueError, OverflowError) as exc:
        raise TypeMismatchError(f"cannot cast {value!r} to {to}") from exc


def strip_prefix(value: JsonScalar, prefix: str) -> JsonScalar:
    if value is None:
        return None
    return _require_str(value, "STRIP_PREFIX").removeprefix(prefix)


def add_prefix(value: JsonScalar, prefix: str) -> JsonScalar:
    if value is None:
        return None
    return prefix + _require_str(value, "ADD_PREFIX")


def regex_extract(value: JsonScalar, pattern: str, group: int) -> JsonScalar:
    if value is None:
        return None
    found = re.search(pattern, _require_str(value, "REGEX_EXTRACT"))
    return found.group(group) if found else None


def regex_replace(value: JsonScalar, pattern: str, replacement: str) -> JsonScalar:
    if value is None:
        return None
    return re.sub(pattern, replacement, _require_str(value, "REGEX_REPLACE"))


def map_enum(
    value: JsonScalar,
    mapping: Mapping[str, JsonScalar],
    on_unmapped: str,
    default: JsonScalar,
) -> JsonScalar:
    if value is None:
        return None
    key = _require_str(value, "MAP_ENUM")
    if key in mapping:
        return mapping[key]
    if on_unmapped == "default":
        return default
    raise UnmappedValueError(f"no mapping for {key!r}")


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


def format_datetime(value: JsonScalar, from_format: str, to_format: str) -> JsonScalar:
    if value is None:
        return None
    return _format_datetime(_parse_datetime(value, from_format), to_format)


def split_part(value: JsonScalar, separator: str, index: int) -> JsonScalar:
    if value is None:
        return None
    parts = _require_str(value, "SPLIT_PART").split(separator)
    return parts[index] if -len(parts) <= index < len(parts) else None


# ---- record assembly ---------------------------------------------------------------------------


class FieldTransformError(TransformError):
    """A target field could not be computed; names the field so the record can be reported."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(f"{field}: {detail}")
        self.field = field
        self.detail = detail


def build_record(
    fields: tuple[tuple[str, Callable[[Record], JsonScalar]], ...], record: Record
) -> dict[str, JsonScalar]:
    """Run every compiled field function on one source record."""
    out: dict[str, JsonScalar] = {}
    for name, compute in fields:
        try:
            out[name] = compute(record)
        except TransformError as error:
            raise FieldTransformError(name, str(error)) from error
    return out
