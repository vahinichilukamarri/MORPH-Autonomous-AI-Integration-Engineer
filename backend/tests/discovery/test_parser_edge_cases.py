"""Parser behaviour on small synthetic specs: composition, nesting, nullability and errors."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.discovery.errors import ParseError
from app.discovery.models import EntityRole, Field, SystemModel
from app.discovery.parser import parse_spec
from app.discovery.source import load_spec

Schemas = dict[str, Any]


def make_spec(
    schemas: Schemas,
    paths: dict[str, Any] | None = None,
    version: str = "3.1.0",
) -> dict[str, Any]:
    return {
        "openapi": version,
        "info": {"title": "Synthetic", "version": "1"},
        "paths": paths or {},
        "components": {"schemas": schemas},
    }


def get_path(ok: str | None = None, error: str | None = None) -> dict[str, Any]:
    def content(name: str) -> dict[str, Any]:
        return {
            "description": "r",
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}},
        }

    responses: dict[str, Any] = {}
    if ok:
        responses["200"] = content(ok)
    if error:
        responses["404"] = content(error)
    if not responses:
        responses["204"] = {"description": "none"}
    return {"/things": {"get": {"responses": responses}}}


def parse(
    schemas: Schemas, paths: dict[str, Any] | None = None, version: str = "3.1.0"
) -> SystemModel:
    return parse_spec(make_spec(schemas, paths, version), "syn")


def fields(model: SystemModel, entity: str) -> dict[str, Field]:
    found = model.entity(entity)
    assert found is not None, entity
    return {f.path: f for f in found.fields}


def test_ref_chain_resolves_to_the_final_object() -> None:
    model = parse(
        {
            "A": {"$ref": "#/components/schemas/B"},
            "B": {"$ref": "#/components/schemas/C"},
            "C": {"type": "object", "properties": {"x": {"type": "string"}}},
            "Holder": {"type": "object", "properties": {"a": {"$ref": "#/components/schemas/A"}}},
        }
    )
    assert list(fields(model, "A")) == ["x"]
    assert list(fields(model, "C")) == ["x"]
    holder = fields(model, "Holder")["a"]
    assert (holder.json_type, holder.entity_ref) == ("object", "A")


def test_all_of_merges_properties_and_required() -> None:
    model = parse(
        {
            "Base": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "integer"}},
            },
            "Child": {
                "allOf": [
                    {"$ref": "#/components/schemas/Base"},
                    {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}, "id": {"type": "integer"}},
                    },
                ]
            },
        }
    )
    child = fields(model, "Child")
    assert list(child) == ["id", "name"]
    assert child["id"].required and child["name"].required


def test_all_of_with_one_ref_keeps_the_field_description() -> None:
    model = parse(
        {
            "Color": {"type": "string", "enum": ["RED", "BLUE"]},
            "Thing": {
                "type": "object",
                "properties": {
                    "color": {"allOf": [{"$ref": "#/components/schemas/Color"}], "description": "d"}
                },
            },
        }
    )
    color = fields(model, "Thing")["color"]
    assert color.enum_values == ("RED", "BLUE")
    assert color.description == "d"
    assert color.entity_ref is None
    assert model.entity("Color") is None, "enums are field types, not entities"


def test_nested_objects_are_flattened_with_dotted_paths() -> None:
    model = parse(
        {
            "Order": {
                "type": "object",
                "required": ["address"],
                "properties": {
                    "address": {
                        "type": "object",
                        "required": ["street"],
                        "properties": {
                            "street": {"type": "string"},
                            "geo": {"type": "object", "properties": {"lat": {"type": "number"}}},
                        },
                    }
                },
            }
        }
    )
    order = fields(model, "Order")
    assert list(order) == ["address", "address.street", "address.geo", "address.geo.lat"]
    assert order["address"].json_type == "object"
    assert order["address.street"].required and not order["address.geo"].required
    assert order["address.geo.lat"].json_type == "number"
    assert order["address.geo.lat"].name == "lat"


def test_arrays_of_objects_scalars_and_refs() -> None:
    model = parse(
        {
            "Tag": {"type": "object", "properties": {"label": {"type": "string"}}},
            "Order": {
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"sku": {"type": "string"}}},
                    },
                    "tags": {"type": "array", "items": {"$ref": "#/components/schemas/Tag"}},
                    "notes": {"type": "array", "items": {"type": "string"}},
                    "kinds": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
                },
            },
        }
    )
    order = fields(model, "Order")
    assert list(order) == ["lines", "lines[].sku", "tags", "notes", "kinds"]
    assert order["lines"].item_type == "object"
    assert order["tags"].entity_ref == "Tag"
    assert order["notes"].item_type == "string"
    assert order["kinds"].enum_values == ("a", "b")


def test_nullability_in_every_dialect() -> None:
    model = parse(
        {
            "T": {
                "type": "object",
                "properties": {
                    "any_of": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "one_of": {"oneOf": [{"type": "integer"}, {"type": "null"}]},
                    "type_list": {"type": ["string", "null"]},
                    "plain": {"type": "string"},
                },
            }
        }
    )
    t = fields(model, "T")
    assert [(p, f.json_type, f.nullable) for p, f in t.items()] == [
        ("any_of", "string", True),
        ("one_of", "integer", True),
        ("type_list", "string", True),
        ("plain", "string", False),
    ]


def test_openapi_30_nullable_keyword() -> None:
    model = parse(
        {"T": {"type": "object", "properties": {"x": {"type": "string", "nullable": True}}}},
        version="3.0.3",
    )
    assert fields(model, "T")["x"].nullable is True


def test_example_keyword_from_openapi_30() -> None:
    model = parse(
        {"T": {"type": "object", "properties": {"x": {"type": "string", "example": "hi"}}}},
        version="3.0.3",
    )
    assert fields(model, "T")["x"].examples == ("hi",)


def test_wrapper_needs_one_collection_and_paging_fields() -> None:
    model = parse(
        {
            "Item": {"type": "object", "properties": {"id": {"type": "integer"}}},
            "Page": {
                "type": "object",
                "properties": {
                    "data": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}},
                    "total": {"type": "integer"},
                },
            },
            "Bag": {
                "type": "object",
                "properties": {
                    "data": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}},
                    "label": {"type": "string"},
                },
            },
        }
    )
    assert model.entity("Page") is not None and model.entity("Page").role is EntityRole.WRAPPER  # type: ignore[union-attr]
    assert model.entity("Bag") is not None and model.entity("Bag").role is EntityRole.RESOURCE  # type: ignore[union-attr]


def test_error_role_only_when_used_only_in_error_responses() -> None:
    schemas = {
        "Thing": {"type": "object", "properties": {"id": {"type": "integer"}}},
        "Problem": {"type": "object", "properties": {"detail": {"type": "string"}}},
        "Shared": {"type": "object", "properties": {"id": {"type": "integer"}}},
    }
    paths = {
        "/a": get_path(ok="Thing", error="Problem")["/things"],
        "/b": get_path(ok="Shared", error="Shared")["/things"],
    }
    roles = {e.name: e.role for e in parse(schemas, paths).entities}
    assert roles == {
        "Thing": EntityRole.RESOURCE,
        "Problem": EntityRole.ERROR,
        "Shared": EntityRole.RESOURCE,
    }


def test_inline_response_schema_becomes_an_entity() -> None:
    paths = {
        "/x": {
            "get": {
                "operationId": "getX",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"n": {"type": "integer"}},
                                }
                            }
                        },
                    }
                },
            }
        }
    }
    model = parse({}, paths)
    assert model.operations[0].responses[0].entity == "getX_response_200"
    assert list(fields(model, "getX_response_200")) == ["n"]


def test_global_security_applies_when_operation_has_none() -> None:
    spec = make_spec({}, get_path())
    spec["components"]["securitySchemes"] = {"Key": {"type": "apiKey", "in": "header", "name": "K"}}
    spec["security"] = [{"Key": []}]
    assert parse_spec(spec, "s").operations[0].auth_scheme == "Key"


# ---- failures: structured ParseError, never a crash, never silent ------------------------------


def problems(spec: dict[str, Any]) -> list[tuple[str, str]]:
    with pytest.raises(ParseError) as info:
        parse_spec(spec, "bad")
    return [(p.pointer, p.message) for p in info.value.problems]


def test_invalid_spec_reports_pointer() -> None:
    spec = make_spec({}, {"/x": {"get": {"responses": {"200": {}}}}})
    found = problems(spec)
    assert any(pointer == "/paths/~1x/get/responses/200" for pointer, _ in found), found


def test_unsupported_openapi_version() -> None:
    assert problems({"swagger": "2.0", "info": {}, "paths": {}})[0][0] == "/openapi"


def test_unresolvable_ref() -> None:
    found = problems(
        make_spec(
            {"T": {"type": "object", "properties": {"x": {"$ref": "#/components/schemas/Nope"}}}}
        )
    )
    assert any("unresolvable" in message for _, message in found)


def test_remote_ref_is_unsupported() -> None:
    found = problems(
        make_spec({"T": {"type": "object", "properties": {"x": {"$ref": "other.json#/A"}}}})
    )
    assert any("only local" in message for _, message in found)


def test_circular_ref_chain() -> None:
    found = problems(
        make_spec(
            {"A": {"$ref": "#/components/schemas/B"}, "B": {"$ref": "#/components/schemas/A"}}
        )
    )
    assert any("circular" in message for _, message in found)


def test_union_of_objects_is_unsupported_and_pointed_at() -> None:
    schema = {
        "T": {
            "type": "object",
            "properties": {
                "x": {
                    "anyOf": [
                        {"type": "object", "properties": {"a": {"type": "string"}}},
                        {"type": "array", "items": {"type": "string"}},
                    ]
                }
            },
        }
    }
    found = problems(make_spec(schema))
    assert (
        "/components/schemas/T/properties/x/anyOf",
        "unsupported anyOf with several non-primitive types",
    ) in found


def test_not_keyword_and_typed_additional_properties_are_unsupported() -> None:
    schema = {
        "T": {
            "type": "object",
            "properties": {
                "a": {"not": {"type": "string"}},
                "b": {"type": "object", "additionalProperties": {"type": "string"}},
            },
        }
    }
    messages = [m for _, m in problems(make_spec(schema))]
    assert any("'not'" in m for m in messages)
    assert any("additionalProperties" in m for m in messages)


def test_all_problems_are_reported_together() -> None:
    schema = {
        "T": {
            "type": "object",
            "properties": {
                "a": {"not": {"type": "string"}},
                "b": {"$ref": "#/components/schemas/Nope"},
            },
        }
    }
    assert len(problems(make_spec(schema))) >= 2


def test_load_spec_errors_are_structured(tmp_path: Path) -> None:
    with pytest.raises(ParseError) as missing:
        load_spec(tmp_path / "nope.json")
    assert "cannot load" in missing.value.problems[0].message
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ParseError):
        load_spec(broken)
    array = tmp_path / "array.json"
    array.write_text(json.dumps([1]), encoding="utf-8")
    with pytest.raises(ParseError, match="not a JSON object"):
        load_spec(array)
    with pytest.raises(ParseError, match="cannot load"):
        load_spec("http://127.0.0.1:9/openapi.json", timeout=0.5)
