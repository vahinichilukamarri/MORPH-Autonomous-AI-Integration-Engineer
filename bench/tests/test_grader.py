"""The grader scores right mappings right and, just as important, wrong mappings wrong."""

from pathlib import Path
from typing import Any

import yaml
from app.mapping.transform import Transformation

from morph_bench.grader import FieldGrade, ProposedMapping, ScenarioGrade, grade_scenario
from morph_bench.loader import SCENARIOS_ROOT, load_bundle
from morph_bench.manifest import BENCH_DIR

S1 = load_bundle(SCENARIOS_ROOT / "crm_customer_to_support_user")
S3 = load_bundle(SCENARIOS_ROOT / "support_user_to_crm_customer")


def reference(scenario_id: str) -> dict[str, Any]:
    path = Path(BENCH_DIR) / "references" / f"{scenario_id}.yaml"
    pipelines: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))["pipelines"]
    return pipelines


def correct_proposals(bundle: Any, *, flag: set[str] = frozenset()) -> dict[str, ProposedMapping]:  # type: ignore[assignment]
    """What a perfect system would propose, built from the reference pipelines."""
    pipelines = reference(bundle.scenario.id)
    out: dict[str, ProposedMapping] = {}
    for entry in bundle.answer_key.mappings:
        if entry.target_field in pipelines:
            transformation = Transformation.model_validate(pipelines[entry.target_field])
            out[entry.target_field] = ProposedMapping(
                entry.target_field,
                entry.mapping_type.value,
                entry.source_fields,
                transformation,
                flagged=entry.target_field in flag or entry.expects_review,
            )
        else:
            out[entry.target_field] = ProposedMapping(
                entry.target_field, "UNRESOLVED", (), None, flagged=True
            )
    return out


def pipeline(*steps: dict[str, Any]) -> Transformation:
    return Transformation.model_validate({"steps": list(steps)})


def with_(
    proposals: dict[str, ProposedMapping], **changes: ProposedMapping
) -> list[ProposedMapping]:
    return list({**proposals, **changes}.values())


def grades(result: ScenarioGrade) -> dict[str, FieldGrade]:
    return {g.target_field: g for g in result.fields}


# ---- right answers are scored right ------------------------------------------------------------


def test_a_perfect_system_scores_every_field_correct() -> None:
    for bundle in (S1, S3):
        result = grade_scenario(bundle, list(correct_proposals(bundle).values()))
        assert result.correct == len(bundle.answer_key.mappings), [
            (g.target_field, g.detail) for g in result.fields if not g.fully_correct
        ]


def test_a_different_pipeline_that_agrees_on_every_example_is_also_correct() -> None:
    other = ProposedMapping(
        "userId",
        "TRANSFORMATION",
        ("customer_id",),
        pipeline(
            {"op": "COPY", "field": "customer_id"},
            {"op": "SPLIT_PART", "separator": "-", "index": 1},
            {"op": "CAST", "to": "int"},
        ),
        flagged=False,
    )
    result = grade_scenario(S1, with_(correct_proposals(S1), userId=other))
    assert grades(result)["userId"].fully_correct


def test_correctly_flagged_unresolved_is_correct() -> None:
    result = grade_scenario(S3, list(correct_proposals(S3).values()))
    segment = grades(result)["segment"]
    assert segment.unresolved_correct and segment.fully_correct and not segment.guessed


# ---- wrong answers are scored wrong ------------------------------------------------------------


def test_wrong_sources_are_wrong_even_when_the_values_look_right() -> None:
    wrong = ProposedMapping(
        "email_address",
        "DIRECT",
        ("last_name",),
        pipeline({"op": "COPY", "field": "last_name"}),
        flagged=False,
    )
    grade = grades(grade_scenario(S1, with_(correct_proposals(S1), email_address=wrong)))[
        "email_address"
    ]
    assert grade.source_match is False and not grade.fully_correct
    assert "!= expected" in grade.detail


def test_right_sources_wrong_transformation_is_wrong() -> None:
    reversed_map = ProposedMapping(
        "tier",
        "DERIVED",
        ("segment",),
        pipeline(
            {"op": "COPY", "field": "segment"},
            {
                "op": "MAP_ENUM",
                "mapping": {"SMB": "PRIORITY", "MIDMARKET": "STANDARD", "ENTERPRISE": "STANDARD"},
            },
        ),
        flagged=False,
    )
    grade = grades(grade_scenario(S1, with_(correct_proposals(S1), tier=reversed_map)))["tier"]
    assert grade.source_match is True
    assert grade.transformation_correct is False and not grade.fully_correct
    assert "produced 'PRIORITY', expected 'STANDARD'" in grade.detail


def test_a_composite_that_does_not_trim_is_wrong() -> None:
    untrimmed = ProposedMapping(
        "fullName",
        "COMPOSITE",
        ("first_name", "last_name"),
        pipeline(
            {
                "op": "JOIN_NONNULL",
                "fields": ["first_name", "last_name"],
                "separator": " ",
                "trim": False,
            }
        ),
        flagged=False,
    )
    grade = grades(grade_scenario(S1, with_(correct_proposals(S1), fullName=untrimmed)))["fullName"]
    assert grade.transformation_correct is False
    assert "' Ravi '" in grade.detail


def test_split_on_the_last_space_fails_the_agreed_convention() -> None:
    last_space = ProposedMapping(
        "first_name",
        "TRANSFORMATION",
        ("fullName",),
        pipeline(
            {"op": "COPY", "field": "fullName"},
            {"op": "REGEX_EXTRACT", "pattern": "^(.+) \\S+$", "group": 1},
        ),
        flagged=False,
    )
    grade = grades(grade_scenario(S3, with_(correct_proposals(S3), first_name=last_space)))[
        "first_name"
    ]
    assert not grade.fully_correct


def test_a_pipeline_that_errors_on_an_example_is_wrong_not_a_crash() -> None:
    erroring = ProposedMapping(
        "status",
        "TRANSFORMATION",
        ("accountState",),
        pipeline(
            {"op": "COPY", "field": "accountState"},
            {"op": "MAP_ENUM", "mapping": {"ENABLED": "ACTIVE"}},
        ),
        flagged=False,
    )
    grade = grades(grade_scenario(S3, with_(correct_proposals(S3), status=erroring)))["status"]
    assert grade.transformation_correct is False and "UnmappedValueError" in grade.detail


def test_guessing_an_unresolvable_field_is_wrong() -> None:
    guess = ProposedMapping(
        "segment",
        "DERIVED",
        ("tier",),
        pipeline(
            {"op": "COPY", "field": "tier"},
            {"op": "MAP_ENUM", "mapping": {"STANDARD": "SMB", "PRIORITY": "ENTERPRISE"}},
        ),
        flagged=True,
        confidence=0.4,
    )
    grade = grades(grade_scenario(S3, with_(correct_proposals(S3), segment=guess)))["segment"]
    assert grade.guessed and grade.unresolved_correct is False and not grade.fully_correct
    assert grade.flagged, "even a flagged guess is still a guess"


def test_unresolved_for_a_resolvable_field_is_wrong() -> None:
    gave_up = ProposedMapping("email", "UNRESOLVED", (), None, flagged=True)
    grade = grades(grade_scenario(S3, with_(correct_proposals(S3), email=gave_up)))["email"]
    assert not grade.fully_correct and "UNRESOLVED for a resolvable" in grade.detail
    assert grade.source_match is False and grade.transformation_correct is False


def test_a_missing_proposal_is_wrong() -> None:
    proposals = [p for p in correct_proposals(S1).values() if p.target_field != "createdAt"]
    grade = grades(grade_scenario(S1, proposals))["createdAt"]
    assert not grade.fully_correct and grade.detail == "no proposal for this target field"


# ---- the review flag ---------------------------------------------------------------------------


def test_customer_id_needs_the_review_flag_to_count_as_correct() -> None:
    unflagged = ProposedMapping(
        "customer_id",
        "DIRECT",
        ("externalRef",),
        pipeline({"op": "COPY", "field": "externalRef"}),
        flagged=False,
    )
    grade = grades(grade_scenario(S3, with_(correct_proposals(S3), customer_id=unflagged)))[
        "customer_id"
    ]
    assert grade.source_match and grade.transformation_correct
    assert grade.flag_correct is False and not grade.fully_correct
    assert "review flag was expected" in grade.detail


def test_a_flag_on_the_wrong_mapping_does_not_rescue_it() -> None:
    fabricated = ProposedMapping(
        "customer_id",
        "TRANSFORMATION",
        ("userId",),
        pipeline(
            {"op": "COPY", "field": "userId"},
            {"op": "CAST", "to": "str"},
            {"op": "ADD_PREFIX", "prefix": "C-"},
        ),
        flagged=True,
    )
    grade = grades(grade_scenario(S3, with_(correct_proposals(S3), customer_id=fabricated)))[
        "customer_id"
    ]
    assert grade.flag_correct is True and grade.source_match is False
    assert not grade.fully_correct


def test_a_flag_is_not_required_where_the_key_does_not_expect_one() -> None:
    flagged_email = correct_proposals(S3, flag={"email"})
    unflagged_email = correct_proposals(S3)
    for proposals in (flagged_email, unflagged_email):
        assert grades(grade_scenario(S3, list(proposals.values())))["email"].fully_correct


# ---- aggregation -------------------------------------------------------------------------------


def test_rates_count_only_applicable_fields() -> None:
    result = grade_scenario(S3, list(correct_proposals(S3).values()))
    assert result.rate("source_match") == (7, 7), "segment has no source to match"
    assert result.rate("unresolved_correct") == (1, 1)
    assert result.rate("flag_correct") == (1, 1)
    assert result.correct == 8
