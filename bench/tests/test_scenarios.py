import json
from typing import Any

import pytest
from pydantic import ValidationError

from morph_bench.loader import SCENARIOS_ROOT, load_bundle
from morph_bench.models import MappingEntry, MappingType
from morph_bench.spec_transform import strip_docs
from morph_bench.systems import load_spec, spec_path


def bundle(name: str):  # type: ignore[no-untyped-def]
    return load_bundle(SCENARIOS_ROOT / name)


def walk(node: Any, keys: set[str], in_properties: bool = False) -> list[str]:
    """Paths of every schema keyword in ``keys`` (property names are not keywords)."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if not in_properties and key in keys:
                found.append(key)
            found.extend(walk(value, keys, in_properties=key == "properties" and not in_properties))
    elif isinstance(node, list):
        for item in node:
            found.extend(walk(item, keys))
    return found


# ---- spec transform ----------------------------------------------------------------------------


def test_strip_docs_removes_every_description_and_example() -> None:
    for system in ("crm", "support"):
        original = json.loads(spec_path(system).read_text(encoding="utf-8"))
        stripped = strip_docs(original)
        assert walk(original, {"description", "example", "examples"}), (
            "the originals are documented"
        )
        leftovers = [k for k in walk(stripped, {"description", "example", "examples"})]
        # only the (required, now empty) response descriptions remain
        assert all(k == "description" for k in leftovers)
        responses = [
            r
            for path in stripped["paths"].values()
            for op in path.values()
            for r in op["responses"].values()
        ]
        assert responses and all(r["description"] == "" for r in responses)
        assert "description" not in stripped["info"]


def test_strip_docs_keeps_structure_and_constraints_and_is_pure() -> None:
    original = json.loads(spec_path("support").read_text(encoding="utf-8"))
    before = json.dumps(original, sort_keys=True)
    stripped = strip_docs(original)
    assert json.dumps(original, sort_keys=True) == before, "the input is not modified"
    assert strip_docs(original) == stripped, "deterministic"
    user = stripped["components"]["schemas"]["User"]
    assert list(user["properties"]) == list(original["components"]["schemas"]["User"]["properties"])
    assert user["required"] == original["components"]["schemas"]["User"]["required"]
    assert "pattern" in json.dumps(user["properties"]["phoneNumber"])
    assert stripped["paths"].keys() == original["paths"].keys()


def test_property_named_description_is_not_mistaken_for_documentation() -> None:
    spec = {
        "info": {"title": "t", "description": "doc"},
        "components": {
            "schemas": {
                "X": {
                    "type": "object",
                    "description": "doc",
                    "properties": {
                        "description": {"type": "string", "description": "doc", "example": "e"},
                        "example": {"type": "string"},
                    },
                }
            }
        },
    }
    stripped = strip_docs(spec)
    props = stripped["components"]["schemas"]["X"]["properties"]
    assert props == {"description": {"type": "string"}, "example": {"type": "string"}}
    assert "description" not in stripped["components"]["schemas"]["X"]


def test_stripped_specs_still_parse_with_the_backend_parser() -> None:
    from app.discovery.parser import parse_spec

    for system in ("crm", "support"):
        model = parse_spec(load_spec(system, "v1", "strip_docs"), system)
        assert all(
            f.description is None and not f.examples for e in model.entities for f in e.fields
        )
        assert sum(len(e.fields) for e in model.entities) > 10


# ---- the four scenarios ------------------------------------------------------------------------


def test_nodocs_scenario_has_the_same_key_content_as_the_documented_one() -> None:
    plain, nodocs = (
        bundle("crm_customer_to_support_user"),
        bundle("crm_customer_to_support_user_nodocs"),
    )
    assert (
        nodocs.scenario.spec_transform == "strip_docs" and plain.scenario.spec_transform == "none"
    )
    assert nodocs.answer_key.mappings == plain.answer_key.mappings


def test_v2_scenario_uses_the_renamed_fields() -> None:
    v2 = bundle("crm_v2_to_support_v2")
    assert (v2.scenario.source.contract, v2.scenario.target.contract) == ("v2", "v2")
    assert v2.scenario.fault_profile.contract_version == "v2"
    targets = {m.target_field: m for m in v2.answer_key.mappings}
    assert "serviceTier" in targets and "tier" not in targets
    assert targets["phoneNumber"].source_fields == ("phone_number",)


def test_reverse_scenario_encodes_the_decisions_about_hard_cases() -> None:
    s3 = bundle("support_user_to_crm_customer")
    assert (s3.scenario.source.system, s3.scenario.target.system) == ("support", "crm")
    keyed = {m.target_field: m for m in s3.answer_key.mappings}
    assert set(keyed) == {
        "customer_id",
        "first_name",
        "last_name",
        "email",
        "phone",
        "status",
        "segment",
        "created_at",
    }
    cid = keyed["customer_id"]
    assert cid.mapping_type is MappingType.DIRECT and cid.source_fields == ("externalRef",)
    assert cid.expects_review and cid.review_note and "never be fabricated" in cid.review_note
    assert [e.output for e in cid.examples] == [e.input["externalRef"] for e in cid.examples]
    assert keyed["segment"].mapping_type is MappingType.UNRESOLVED
    assert keyed["segment"].reason and not keyed["segment"].examples
    assert {m.target_field for m in s3.answer_key.mappings if m.expects_review} == {"customer_id"}
    last = keyed["last_name"]
    assert any(e.input == {"fullName": "Kiran"} and e.output is None for e in last.examples)


# ---- key format additions ----------------------------------------------------------------------


def entry(**kwargs: Any) -> MappingEntry:
    return MappingEntry.model_validate({"target_field": "t", **kwargs})


def test_expects_review_needs_a_note_and_vice_versa() -> None:
    base: dict[str, Any] = {
        "mapping_type": "DIRECT",
        "source_fields": ["a"],
        "examples": [{"input": {"a": 1}, "output": 1}],
    }
    assert entry(**base, expects_review=True, review_note="why").expects_review
    with pytest.raises(ValidationError, match="needs a review_note"):
        entry(**base, expects_review=True)
    with pytest.raises(ValidationError, match="only for entries with expects_review"):
        entry(**base, review_note="why")
