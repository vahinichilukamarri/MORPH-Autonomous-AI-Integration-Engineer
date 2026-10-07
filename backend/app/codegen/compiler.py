"""Deterministic compiler: mapping DSL pipelines -> a Python module of field functions.

No LLM is involved. Every DSL op becomes one call into ``morph_runtime.ops`` with literal
arguments, so the generated surface is a flat list of calls that the static gate can check. The
differential test suite proves the compiled functions return exactly what the DSL executor
returns, errors included.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.mapping.transform import (
    AddPrefix,
    Cast,
    Coalesce,
    Constant,
    Copy,
    FormatDatetime,
    JoinNonNull,
    JsonScalar,
    MapEnum,
    RegexExtract,
    RegexReplace,
    SplitPart,
    Step,
    StripPrefix,
    Transformation,
)

COMPILER_VERSION = "1"


class CompileError(Exception):
    """A pipeline cannot be compiled (for example a non-finite float constant)."""


@dataclass(frozen=True)
class FieldSpec:
    target_field: str
    transformation: Transformation


def _scalar(value: JsonScalar) -> str:
    if isinstance(value, float) and not math.isfinite(value):
        raise CompileError("non-finite float constants are not supported")
    return repr(value)


def _tuple(values: Sequence[str]) -> str:
    items = ", ".join(repr(v) for v in values)
    return f"({items},)" if len(values) == 1 else f"({items})"


def _mapping(mapping: dict[str, JsonScalar]) -> str:
    inner = "".join(f"            {k!r}: {_scalar(v)},\n" for k, v in mapping.items())
    return "{\n" + inner + "        }"


def _step_line(step: Step) -> str:
    """The statement that computes ``value`` for one step."""
    if isinstance(step, Copy):
        return f"value = ops.copy_field(record, {step.field!r})"
    if isinstance(step, JoinNonNull):
        return (
            f"value = ops.join_nonnull(record, {_tuple(step.fields)}, "
            f"{step.separator!r}, {step.trim!r})"
        )
    if isinstance(step, Coalesce):
        return f"value = ops.coalesce(record, {_tuple(step.fields)})"
    if isinstance(step, Constant):
        return f"value = {_scalar(step.value)}"
    if isinstance(step, Cast):
        return f"value = ops.cast(value, {step.to!r})"
    if isinstance(step, StripPrefix):
        return f"value = ops.strip_prefix(value, {step.prefix!r})"
    if isinstance(step, AddPrefix):
        return f"value = ops.add_prefix(value, {step.prefix!r})"
    if isinstance(step, RegexExtract):
        return f"value = ops.regex_extract(value, {step.pattern!r}, {step.group!r})"
    if isinstance(step, RegexReplace):
        return f"value = ops.regex_replace(value, {step.pattern!r}, {step.replacement!r})"
    if isinstance(step, MapEnum):
        return (
            f"value = ops.map_enum(\n        value,\n        {_mapping(step.mapping)},\n"
            f"        {step.on_unmapped!r},\n        {_scalar(step.default)},\n    )"
        )
    if isinstance(step, FormatDatetime):
        return f"value = ops.format_datetime(value, {step.from_format!r}, {step.to_format!r})"
    if isinstance(step, SplitPart):
        return f"value = ops.split_part(value, {step.separator!r}, {step.index!r})"
    raise CompileError(f"no compilation rule for {step.op}")


def function_name(index: int, target_field: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z]+", "_", target_field).strip("_").lower() or "field"
    return f"field_{index}_{safe}"


def compile_field(name: str, transformation: Transformation) -> str:
    body = "".join(f"    {_step_line(step)}\n" for step in transformation.steps)
    return f"def {name}(record: Record) -> JsonScalar:\n{body}    return value\n"


def compile_transform_module(fields: Sequence[FieldSpec]) -> str:
    """The source of ``transform.py``: one function per target field and ``to_target``."""
    if len({f.target_field for f in fields}) != len(fields):
        raise CompileError("duplicate target field")
    names = [function_name(i, f.target_field) for i, f in enumerate(fields)]
    out = [
        '"""Compiled from approved mappings. Generated; do not edit."""\n',
        "from morph_runtime import ops\nfrom morph_runtime.ops import JsonScalar, Record\n",
    ]
    out += [compile_field(n, f.transformation) for n, f in zip(names, fields, strict=True)]
    table = "".join(f"    ({f.target_field!r}, {n}),\n" for f, n in zip(fields, names, strict=True))
    out.append(f"TARGET_FIELDS = (\n{table})\n")
    out.append(
        "def to_target(record: Record) -> dict[str, JsonScalar]:\n"
        "    return ops.build_record(TARGET_FIELDS, record)\n"
    )
    return "\n\n".join(out)
