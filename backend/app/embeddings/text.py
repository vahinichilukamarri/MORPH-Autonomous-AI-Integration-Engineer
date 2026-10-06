"""The exact text that gets embedded for a field and for an entity.

Templates (documented in docs/discovery.md). Segments with no content are left out; the
remaining segments are joined with " | ":

  field:   <Entity>.<path> | type: <json_type> [<format>] | <description>
             | allowed values: <v1, v2> | examples: <e1, e2>
  entity:  <Entity> | <description> | fields: <path1, path2>
"""

import json

from pydantic import JsonValue

from app.discovery.models import Entity, Field

SEPARATOR = " | "


def _scalar(value: JsonValue) -> str:
    return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))


def field_text(entity_name: str, field: Field) -> str:
    type_segment = " ".join(p for p in (field.json_type, field.format) if p)
    segments = [f"{entity_name}.{field.path}", f"type: {type_segment}"]
    if field.description:
        segments.append(field.description)
    if field.enum_values:
        segments.append("allowed values: " + ", ".join(_scalar(v) for v in field.enum_values))
    if field.examples:
        segments.append("examples: " + ", ".join(_scalar(v) for v in field.examples))
    return SEPARATOR.join(segments)


def entity_text(entity: Entity) -> str:
    segments = [entity.name]
    if entity.description:
        segments.append(entity.description)
    if entity.fields:
        segments.append("fields: " + ", ".join(f.path for f in entity.fields))
    return SEPARATOR.join(segments)
