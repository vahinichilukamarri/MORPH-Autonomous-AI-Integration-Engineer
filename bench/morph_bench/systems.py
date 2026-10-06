"""Where the mock systems' OpenAPI contracts live and how to read field schemas from them."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_FILES: dict[str, Path] = {
    "crm": REPO_ROOT / "mock_systems" / "openapi" / "crm.v1.json",
    "support": REPO_ROOT / "mock_systems" / "openapi" / "support.v1.json",
}


@lru_cache
def load_spec(system: str) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(SPEC_FILES[system].read_text(encoding="utf-8"))
    return spec


def entity_fields(system: str, entity: str) -> dict[str, Any] | None:
    """Property schemas of an entity, or None when the system has no such schema."""
    schema = load_spec(system).get("components", {}).get("schemas", {}).get(entity)
    if schema is None:
        return None
    properties: dict[str, Any] = schema.get("properties", {})
    return properties


def field_validator(system: str, entity: str, field: str) -> Draft202012Validator:
    """A validator for values of one field, resolving $refs against the system's spec."""
    schema = {
        "$ref": f"#/components/schemas/{entity}/properties/{field}",
        "components": load_spec(system)["components"],
    }
    return Draft202012Validator(schema)
