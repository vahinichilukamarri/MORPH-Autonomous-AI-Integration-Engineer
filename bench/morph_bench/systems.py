"""Where the mock systems' OpenAPI contracts live and how to read field schemas from them."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from morph_bench.spec_transform import SpecTransform, apply_transform

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_DIR = REPO_ROOT / "mock_systems" / "openapi"
SPEC_FILES: dict[str, Path] = {
    "crm": OPENAPI_DIR / "crm.v1.json",
    "support": OPENAPI_DIR / "support.v1.json",
}


def spec_path(system: str, contract: str = "v1") -> Path:
    return OPENAPI_DIR / f"{system}.{contract}.json"


@lru_cache
def _read(system: str, contract: str) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(spec_path(system, contract).read_text(encoding="utf-8"))
    return spec


@lru_cache
def load_spec(
    system: str, contract: str = "v1", transform: SpecTransform = "none"
) -> dict[str, Any]:
    """A system's OpenAPI document at a contract version, with the scenario's transform applied.

    Cached: callers must not modify the result.
    """
    return apply_transform(_read(system, contract), transform)


def entity_fields(
    system: str, entity: str, contract: str = "v1", transform: SpecTransform = "none"
) -> dict[str, Any] | None:
    """Property schemas of an entity, or None when the system has no such schema."""
    schema = (
        load_spec(system, contract, transform).get("components", {}).get("schemas", {}).get(entity)
    )
    if schema is None:
        return None
    properties: dict[str, Any] = schema.get("properties", {})
    return properties


def field_validator(
    system: str,
    entity: str,
    field: str,
    contract: str = "v1",
    transform: SpecTransform = "none",
) -> Draft202012Validator:
    """A validator for values of one field, resolving $refs against the system's spec."""
    schema = {
        "$ref": f"#/components/schemas/{entity}/properties/{field}",
        "components": load_spec(system, contract, transform)["components"],
    }
    return Draft202012Validator(schema)
