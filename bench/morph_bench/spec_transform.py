"""Deterministic transforms applied to OpenAPI specs before a scenario uses them."""

import copy
from typing import Any, Literal

SpecTransform = Literal["none", "strip_docs"]
# Keys whose children are names (property names, schema names, ...), not schema keywords.
_NAME_MAPS = frozenset({"properties", "schemas", "securitySchemes", "paths"})
_DOC_KEYS = frozenset({"description", "examples", "example"})


def strip_docs(spec: dict[str, Any]) -> dict[str, Any]:
    """Remove every description and example from a spec; structure and constraints stay.

    OpenAPI requires a ``description`` on each response object, so those become empty strings.
    The result is a new document; the input is not modified.
    """
    stripped: dict[str, Any] = _strip(copy.deepcopy(spec), in_name_map=False)
    return stripped


def _strip(node: Any, *, in_name_map: bool) -> Any:
    if isinstance(node, list):
        return [_strip(item, in_name_map=False) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if not in_name_map and key in _DOC_KEYS:
            continue
        if key == "responses" and isinstance(value, dict) and not in_name_map:
            out[key] = {
                status: {**_strip(response, in_name_map=False), "description": ""}
                for status, response in value.items()
            }
            continue
        out[key] = _strip(value, in_name_map=key in _NAME_MAPS and not in_name_map)
    return out


def apply_transform(spec: dict[str, Any], transform: SpecTransform) -> dict[str, Any]:
    return strip_docs(spec) if transform == "strip_docs" else spec
