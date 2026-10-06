"""Shared fixtures for mapping tests: real parsed fields and the committed sample records."""

import json
from pathlib import Path
from typing import Any

from app.discovery.models import Field
from app.discovery.parser import parse_spec
from app.discovery.source import load_spec
from app.mapping.proposal import Certainty, MappingType, Proposal
from app.mapping.transform import JsonScalar, Transformation

ROOT = Path(__file__).resolve().parents[3]
OPENAPI = ROOT / "mock_systems" / "openapi"
SAMPLES = ROOT / "mock_systems" / "samples"


def entity_fields(spec_file: str, entity: str) -> dict[str, Field]:
    model = parse_spec(load_spec(OPENAPI / spec_file), "t")
    found = model.entity(entity)
    assert found is not None
    return {f.path: f for f in found.fields}


def samples(name: str) -> list[dict[str, JsonScalar]]:
    document = json.loads((SAMPLES / name).read_text(encoding="utf-8"))
    records: list[dict[str, JsonScalar]] = document["records"]
    return records


def pipeline(*steps: dict[str, Any]) -> Transformation:
    return Transformation.model_validate({"steps": list(steps)})


def proposal(
    target: str,
    kind: MappingType,
    sources: tuple[str, ...],
    *steps: dict[str, Any],
    certainty: Certainty = Certainty.HIGH,
    alternatives: tuple[tuple[tuple[str, ...], str], ...] = (),
) -> Proposal:
    return Proposal(
        target_field=target,
        mapping_type=kind,
        source_fields=sources,
        transformation=pipeline(*steps),
        rationale="test",
        alternatives=alternatives,
        certainty=certainty,
    )


# The correct S1 pipelines, written by hand for tests (not read from any answer key).
USER_ID = (
    "userId",
    MappingType.TRANSFORMATION,
    ("customer_id",),
    {"op": "COPY", "field": "customer_id"},
    {"op": "REGEX_EXTRACT", "pattern": r"^C-(\d+)$", "group": 1},
    {"op": "CAST", "to": "int"},
)
FULL_NAME = (
    "fullName",
    MappingType.COMPOSITE,
    ("first_name", "last_name"),
    {"op": "JOIN_NONNULL", "fields": ["first_name", "last_name"], "separator": " ", "trim": True},
)
PHONE = (
    "phoneNumber",
    MappingType.TRANSFORMATION,
    ("phone",),
    {"op": "COPY", "field": "phone"},
    {"op": "STRIP_PREFIX", "prefix": "+"},
)
STATE = (
    "accountState",
    MappingType.TRANSFORMATION,
    ("status",),
    {"op": "COPY", "field": "status"},
    {
        "op": "MAP_ENUM",
        "mapping": {"ACTIVE": "ENABLED", "INACTIVE": "DISABLED", "SUSPENDED": "BLOCKED"},
    },
)
TIER = (
    "tier",
    MappingType.DERIVED,
    ("segment",),
    {"op": "COPY", "field": "segment"},
    {
        "op": "MAP_ENUM",
        "mapping": {"SMB": "STANDARD", "MIDMARKET": "STANDARD", "ENTERPRISE": "PRIORITY"},
    },
)
CREATED_AT = (
    "createdAt",
    MappingType.TRANSFORMATION,
    ("created_at",),
    {"op": "COPY", "field": "created_at"},
    {"op": "FORMAT_DATETIME", "from_format": "iso8601", "to_format": "epoch_s"},
)
