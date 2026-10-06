from typing import Any

import pytest

from app.mapping.proposal import LLMProposal, MappingType
from app.mapping.transform import Coalesce, Copy, FormatDatetime, MapEnum, execute

BLANK: dict[str, Any] = {
    "field": None,
    "fields": None,
    "separator": None,
    "trim": None,
    "prefix": None,
    "pattern": None,
    "group": None,
    "replacement": None,
    "mapping": None,
    "on_unmapped": None,
    "default": None,
    "to": None,
    "from_format": None,
    "to_format": None,
    "value": None,
    "index": None,
}


def step(op: str, **params: Any) -> dict[str, Any]:
    return {**BLANK, "op": op, **params}


def reply(
    kind: str = "TRANSFORMATION",
    steps: list[dict[str, Any]] | None = None,
    sources: list[str] | None = None,
    **extra: Any,
) -> LLMProposal:
    body: dict[str, Any] = {
        "target_field": "t",
        "mapping_type": kind,
        "source_fields": sources if sources is not None else ["a"],
        "steps": steps if steps is not None else [step("COPY", field="a")],
        "unresolved_reason": None,
        "rationale": "because",
        "alternatives": [],
        "certainty": "HIGH",
    }
    body.update(extra)
    return LLMProposal.model_validate(body)


def test_a_valid_reply_becomes_a_typed_proposal() -> None:
    proposal = reply(
        steps=[
            step("COPY", field="status"),
            step(
                "MAP_ENUM",
                mapping=[{"source": "A", "target": "X"}, {"source": "B", "target": None}],
            ),
        ],
        sources=["status"],
    ).to_proposal("t")
    assert proposal.transformation is not None
    first, second = proposal.transformation.steps
    assert isinstance(first, Copy) and isinstance(second, MapEnum)
    assert second.mapping == {"A": "X", "B": None} and second.on_unmapped == "error"
    assert execute(proposal.transformation, {"status": "A"}) == "X"


def test_unset_optional_parameters_use_dsl_defaults() -> None:
    proposal = reply(
        steps=[step("JOIN_NONNULL", fields=["a", "b"])], kind="COMPOSITE", sources=["a", "b"]
    ).to_proposal("t")
    assert proposal.transformation is not None
    assert execute(proposal.transformation, {"a": " x ", "b": "y"}) == "x y"


def test_default_is_only_used_with_on_unmapped_default() -> None:
    lenient = reply(
        steps=[
            step("COPY", field="a"),
            step(
                "MAP_ENUM", mapping=[{"source": "A", "target": 1}], on_unmapped="default", default=0
            ),
        ]
    ).to_proposal("t")
    assert lenient.transformation is not None
    assert execute(lenient.transformation, {"a": "Z"}) == 0
    step_obj = lenient.transformation.steps[1]
    assert isinstance(step_obj, MapEnum) and step_obj.default == 0


def test_every_op_converts() -> None:
    cases = [
        step("COALESCE", fields=["a", "b"]),
        step("CONSTANT", value="x"),
    ]
    for case in cases:
        assert reply(kind="DERIVED", steps=[case]).to_proposal("t").transformation is not None
    chain = reply(
        steps=[
            step("COPY", field="a"),
            step("CAST", to="int"),
            step("STRIP_PREFIX", prefix="0"),
            step("ADD_PREFIX", prefix="x"),
            step("REGEX_EXTRACT", pattern=r"(\d)", group=1),
            step("REGEX_REPLACE", pattern="a", replacement=""),
            step("FORMAT_DATETIME", from_format="iso8601", to_format="epoch_s"),
            step("SPLIT_PART", separator=" ", index=0),
        ]
    ).to_proposal("t")
    assert chain.transformation is not None and len(chain.transformation.steps) == 8
    assert isinstance(chain.transformation.steps[6], FormatDatetime)


def test_coalesce_and_constant_shapes() -> None:
    assert isinstance(
        reply(steps=[step("COALESCE", fields=["a", "b"])]).to_proposal("t").transformation.steps[0],  # type: ignore[union-attr]
        Coalesce,
    )


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([step("COPY")], "field"),
        ([step("COPY", field="a"), step("CAST")], "to"),
        ([step("COPY", field="a"), step("REGEX_REPLACE", pattern="x")], "replacement"),
        ([step("COPY", field="a"), step("SPLIT_PART", separator=" ")], "index"),
        ([step("COPY", field="a"), step("MAP_ENUM")], "mapping"),
        ([step("COPY", field="a"), step("REGEX_EXTRACT", pattern="(")], "regular expression"),
        ([step("COPY", field="a"), step("COPY", field="b")], "first step"),
        ([step("CAST", to="int")], "first step"),
    ],
)
def test_malformed_steps_are_rejected_with_a_useful_message(
    steps: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        reply(steps=steps).to_proposal("t")


def test_target_field_must_match_the_question() -> None:
    with pytest.raises(ValueError, match="target_field must be 'other'"):
        reply().to_proposal("other")


def test_non_unresolved_needs_steps() -> None:
    with pytest.raises(ValueError, match="at least one step"):
        reply(steps=[]).to_proposal("t")


def test_unresolved_shape() -> None:
    ok = reply(kind="UNRESOLVED", steps=[], sources=[], unresolved_reason="lossy").to_proposal("t")
    assert ok.mapping_type is MappingType.UNRESOLVED and ok.transformation is None
    with pytest.raises(ValueError, match="needs an unresolved_reason"):
        reply(kind="UNRESOLVED", steps=[], sources=[]).to_proposal("t")
    with pytest.raises(ValueError, match="no steps"):
        reply(kind="UNRESOLVED", unresolved_reason="x").to_proposal("t")


def test_schema_is_strict_mode_friendly() -> None:
    from app.llm.base import strictify_schema

    schema = strictify_schema(LLMProposal.model_json_schema())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    flat = schema["$defs"]["FlatStep"]
    assert set(flat["required"]) == set(flat["properties"]), "every step parameter is required"
