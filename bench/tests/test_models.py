from typing import Any

import pytest
from pydantic import ValidationError

from morph_bench.models import AnswerKey, MappingEntry, MappingType, Scenario


def entry(**kwargs: Any) -> MappingEntry:
    return MappingEntry.model_validate({"target_field": "t", **kwargs})


def pair(**inputs: Any) -> dict[str, Any]:
    return {"input": inputs, "output": next(iter(inputs.values()), None)}


def test_direct_requires_one_source_and_identity_examples() -> None:
    ok = entry(mapping_type="DIRECT", source_fields=["a"], examples=[pair(a="x")])
    assert ok.mapping_type is MappingType.DIRECT
    with pytest.raises(ValidationError, match="exactly one source"):
        entry(mapping_type="DIRECT", source_fields=["a", "b"], examples=[pair(a=1, b=2)])
    with pytest.raises(ValidationError, match="must equal its input"):
        entry(
            mapping_type="DIRECT",
            source_fields=["a"],
            examples=[{"input": {"a": "x"}, "output": "y"}],
        )


def test_composite_needs_two_sources() -> None:
    with pytest.raises(ValidationError, match="at least two"):
        entry(mapping_type="COMPOSITE", source_fields=["a"], examples=[pair(a=1)])


def test_transformation_and_derived_need_sources_and_examples() -> None:
    for kind in ("TRANSFORMATION", "DERIVED"):
        with pytest.raises(ValidationError, match="at least one source"):
            entry(mapping_type=kind, examples=[{"input": {}, "output": 1}])
        with pytest.raises(ValidationError, match="at least one example"):
            entry(mapping_type=kind, source_fields=["a"])


def test_constant_has_no_sources_and_one_value() -> None:
    ok = entry(mapping_type="CONSTANT", examples=[{"input": {}, "output": "X"}])
    assert ok.source_fields == ()
    with pytest.raises(ValidationError, match="must not list source"):
        entry(mapping_type="CONSTANT", source_fields=["a"], examples=[pair(a=1)])
    with pytest.raises(ValidationError, match="same output"):
        entry(
            mapping_type="CONSTANT",
            examples=[{"input": {}, "output": "X"}, {"input": {}, "output": "Y"}],
        )


def test_unresolved_needs_reason_and_no_examples() -> None:
    assert entry(mapping_type="UNRESOLVED", reason="no source data").reason
    with pytest.raises(ValidationError, match="needs a reason"):
        entry(mapping_type="UNRESOLVED")
    with pytest.raises(ValidationError, match="must not have examples"):
        entry(mapping_type="UNRESOLVED", reason="r", examples=[{"input": {}, "output": 1}])


def test_example_inputs_must_match_source_fields() -> None:
    with pytest.raises(ValidationError, match="must equal source_fields"):
        entry(
            mapping_type="TRANSFORMATION",
            source_fields=["a"],
            examples=[{"input": {"b": 1}, "output": 1}],
        )


def test_reason_only_for_unresolved() -> None:
    with pytest.raises(ValidationError, match="only for UNRESOLVED"):
        entry(
            mapping_type="TRANSFORMATION",
            source_fields=["a"],
            reason="x",
            examples=[{"input": {"a": 1}, "output": 1}],
        )


def test_unknown_mapping_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        entry(mapping_type="MAGIC")


def test_answer_key_rejects_duplicate_targets() -> None:
    unresolved = {"target_field": "t", "mapping_type": "UNRESOLVED", "reason": "r"}
    with pytest.raises(ValidationError, match="more than once"):
        AnswerKey.model_validate({"scenario_id": "s", "mappings": [unresolved, unresolved]})


def test_scenario_systems_must_differ() -> None:
    body: dict[str, Any] = {
        "id": "s",
        "description": "d",
        "source": {"system": "crm", "entity": "Customer"},
        "target": {"system": "crm", "entity": "Customer"},
        "requirement": "r",
    }
    with pytest.raises(ValidationError, match="different systems"):
        Scenario.model_validate(body)


def test_models_reject_unknown_keys_and_are_frozen() -> None:
    with pytest.raises(ValidationError):
        entry(mapping_type="UNRESOLVED", reason="r", surprise=1)
    unresolved = entry(mapping_type="UNRESOLVED", reason="r")
    with pytest.raises(ValidationError):
        unresolved.reason = "changed"  # type: ignore[misc]
