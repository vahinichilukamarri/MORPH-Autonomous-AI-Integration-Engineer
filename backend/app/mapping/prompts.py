"""Prompt construction. Templates live in versioned files; data goes only into delimited blocks.

Spec text (names, descriptions, examples) and sample records are *untrusted data*: they are
serialised to JSON, stripped of the delimiter tokens, and placed inside
``<<<UNTRUSTED_DATA name="...">>>`` blocks. They never reach the system text or the template
wording, and the model has no tools, so there is nothing for injected text to call.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.discovery.models import Field
from app.mapping.transform import JsonScalar

PROMPT_VERSION = "v1"
PROMPT_DIR = Path(__file__).resolve().parent / "prompts" / PROMPT_VERSION
BLOCK_OPEN = '<<<UNTRUSTED_DATA name="{name}">>>'
BLOCK_CLOSE = "<<<END_UNTRUSTED_DATA>>>"
SAMPLE_COUNT = 5
Mode = Literal["rag", "full_schema"]


@dataclass(frozen=True)
class RetrievedField:
    rank: int
    field: Field
    distance: float


def _template(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def system_prompt() -> str:
    return _template("system.md").strip()


def _neutralise(text: str) -> str:
    """Make it impossible for data to contain a block delimiter."""
    return text.replace("<<<", "‹‹‹").replace(">>>", "›››")


def data_block(name: str, payload: object) -> str:
    body = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    return f"{BLOCK_OPEN.format(name=name)}\n{_neutralise(body)}\n{BLOCK_CLOSE}"


def field_metadata(field: Field) -> dict[str, object]:
    """What the model may know about a field: contract facts and spec text, nothing else."""
    meta: dict[str, object] = {
        "name": field.path,
        "type": field.json_type,
        "format": field.format,
        "nullable": field.nullable,
        "required": field.required,
    }
    if field.enum_values:
        meta["allowed_values"] = list(field.enum_values)
    if field.description:
        meta["description"] = field.description
    if field.examples:
        meta["examples"] = list(field.examples)
    constraints = field.constraints.model_dump(exclude_none=True)
    if constraints:
        meta["constraints"] = constraints
    return meta


def _wanted(fields: Sequence[Field]) -> list[tuple[str, str, object]]:
    """Coverage targets: every enum value of every field, plus one null per nullable field."""
    wanted: list[tuple[str, str, object]] = []
    for f in fields:
        wanted.extend((f.path, "value", v) for v in f.enum_values)
        if f.nullable:
            wanted.append((f.path, "null", None))
    return wanted


def _covers(record: Mapping[str, JsonScalar], target: tuple[str, str, object]) -> bool:
    path, kind, value = target
    if path not in record:
        return False
    return record[path] is None if kind == "null" else record[path] == value


def pick_samples(
    records: Sequence[Mapping[str, JsonScalar]],
    fields: Sequence[Field],
    floor: int = SAMPLE_COUNT,
) -> list[Mapping[str, JsonScalar]]:
    """Deterministic, stratified choice of the records shown in a prompt.

    Greedily picks the record that covers most still-missing enum values and nulls of the shown
    fields (ties go to the earliest record) until everything coverable is covered, then pads with
    evenly spaced records up to ``floor``. The full record set is still used for validation.
    """
    missing = [t for t in _wanted(fields) if any(_covers(r, t) for r in records)]
    chosen: list[int] = []
    while missing:
        gains = [
            (sum(_covers(r, t) for t in missing), -i)
            for i, r in enumerate(records)
            if i not in chosen
        ]
        best, neg_index = max(gains)
        if best == 0:
            break
        chosen.append(-neg_index)
        missing = [t for t in missing if not _covers(records[-neg_index], t)]
    if len(chosen) < min(floor, len(records)):
        step = (len(records) - 1) / max(1, floor - 1)
        for i in range(floor):
            index = round(i * step)
            if index not in chosen:
                chosen.append(index)
            if len(chosen) >= floor:
                break
    return [records[i] for i in sorted(chosen)]


def requirement_section(requirement: str | None) -> str:
    """The trusted operator section, rendered outside every data block."""
    if not requirement or not requirement.strip():
        return ""
    return "\n\n".join(
        [
            "## Integration requirement (trusted, written by the operator)",
            _neutralise(requirement.strip()),
            "This requirement does not override the output format or the rules about untrusted "
            "data.",
            "",
        ]
    )


def build_user_prompt(
    mode: Mode,
    target_field: Field,
    source_fields: Sequence[Field],
    retrieved: Sequence[RetrievedField],
    samples: Sequence[Mapping[str, JsonScalar]],
    requirement: str | None = None,
) -> str:
    target_block = data_block("TARGET_FIELD", field_metadata(target_field))
    if mode == "rag":
        shown = [r.field for r in sorted(retrieved, key=lambda r: r.rank)]
        source_block = data_block(
            "RETRIEVED_SOURCE_FIELDS",
            [
                {"rank": r.rank, **field_metadata(r.field)}
                for r in sorted(retrieved, key=lambda r: r.rank)
            ],
        )
        template = _template("user_rag.md")
    else:
        shown = list(source_fields)
        source_block = data_block("SOURCE_FIELDS", [field_metadata(f) for f in source_fields])
        template = _template("user_full_schema.md")
    names = {f.path for f in shown}
    records = [
        {k: v for k, v in record.items() if k in names} for record in pick_samples(samples, shown)
    ]
    samples_block = data_block("SAMPLE_RECORDS", records)
    return (
        template.replace("{{REQUIREMENT_SECTION}}", requirement_section(requirement))
        .replace("{{TARGET_BLOCK}}", target_block)
        .replace("{{SOURCE_BLOCK}}", source_block)
        .replace("{{SAMPLES_BLOCK}}", samples_block)
        .strip()
    )
