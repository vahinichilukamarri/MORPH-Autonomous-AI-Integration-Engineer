"""Validator v2: post-hoc checks designed after seeing the v1 evaluation results."""

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from app.discovery.models import Field
from app.mapping.confidence import ReviewStatus, confidence, review_decision
from app.mapping.proposal import Certainty, MappingType
from app.mapping.transform import JsonScalar
from app.mapping.validate import Code, Status, ValidationResult, validate_proposal
from app.mapping.validate_v2 import (
    LOSSY_CODES,
    LOSSY_FORCES_REVIEW,
    REVIEW_FORCING_CODES_V2,
    review_decision_v2,
    validate_proposal_v2,
)
from tests.mapping.helpers import STATE, TIER, entity_fields, proposal, samples

SUPPORT = entity_fields("support.v1.json", "User")
CRM = entity_fields("crm.v1.json", "Customer")
USERS = samples("support_user.v1.json")
CUSTOMERS = samples("crm_customer.v1.json")


def v2(
    spec_proposal: Any,
    target: str,
    *,
    source: Any = SUPPORT,
    fields: Any = CRM,
    records: Any = USERS,
) -> ValidationResult:
    return validate_proposal_v2(
        spec_proposal, source_fields=source, target_field=fields[target], samples=records
    )


def only(*names: str) -> list[Any]:
    """The sample users with exactly these full names, in sample order."""
    return [u for u in USERS if u["fullName"] in names]


def codes(result: ValidationResult) -> set[Code]:
    return {r.code for r in result.reasons}


# The two v1 failures that motivated v2 (S3 `last_name`), as explicit regression cases.
NEVER_MATCHES = proposal(
    "last_name",
    MappingType.TRANSFORMATION,
    ("fullName",),
    {"op": "COPY", "field": "fullName"},
    {"op": "REGEX_EXTRACT", "pattern": r"^\S+  (.+)$", "group": 1},
)
SECOND_TOKEN = proposal(
    "last_name",
    MappingType.TRANSFORMATION,
    ("fullName",),
    {"op": "COPY", "field": "fullName"},
    {"op": "SPLIT_PART", "separator": " ", "index": 1},
)
REMAINDER = proposal(
    "last_name",
    MappingType.TRANSFORMATION,
    ("fullName",),
    {"op": "COPY", "field": "fullName"},
    {"op": "REGEX_EXTRACT", "pattern": r"^\S+ (.+)$", "group": 1},
)
FIRST_TOKEN = proposal(
    "first_name",
    MappingType.TRANSFORMATION,
    ("fullName",),
    {"op": "COPY", "field": "fullName"},
    {"op": "SPLIT_PART", "separator": " ", "index": 0},
)


# ---- regression: "Asha Verma -> null" ----------------------------------------------------------


def test_asha_verma_to_null_is_flagged_and_forces_review() -> None:
    v1 = validate_proposal(
        NEVER_MATCHES, source_fields=SUPPORT, target_field=CRM["last_name"], samples=USERS
    )
    assert v1.status is Status.PASS, "v1 cannot see it: null is legal for a nullable target"
    score = confidence(v1, {"fullName": 1}, ["fullName"], Certainty.HIGH)
    assert score == 1.0
    assert review_decision(MappingType.TRANSFORMATION, v1, score)[0] is ReviewStatus.AUTO_ACCEPTED

    result = v2(NEVER_MATCHES, "last_name", records=only("Asha Verma", "Ravi Iyer"))
    assert Code.MOSTLY_NULL_OUTPUT in codes(result)
    detail = next(r.detail for r in result.reasons if r.code is Code.MOSTLY_NULL_OUTPUT)
    assert "'Asha Verma' -> None" in detail
    status, reasons = review_decision_v2(MappingType.TRANSFORMATION, result, score)
    assert status is ReviewStatus.NEEDS_REVIEW
    assert "warning:MOSTLY_NULL_OUTPUT" in reasons


# ---- regression: "Mary Ann Smith -> Ann" -------------------------------------------------------


def test_mary_ann_smith_to_ann_is_detected_as_lossy_truncation() -> None:
    v1 = validate_proposal(
        SECOND_TOKEN, source_fields=SUPPORT, target_field=CRM["last_name"], samples=USERS
    )
    assert v1.status is Status.PASS and not v1.reasons

    result = v2(SECOND_TOKEN, "last_name", records=only("Mary Ann Smith"))
    assert Code.LOSSY_TRUNCATION in codes(result)
    detail = next(r.detail for r in result.reasons if r.code is Code.LOSSY_TRUNCATION)
    assert "'Mary Ann Smith' -> 'Ann'" in detail and "discards ['Mary', 'Smith']" in detail
    assert Code.MOSTLY_NULL_OUTPUT not in codes(result), "it is wrong, not null"


def test_the_lossy_checks_do_not_force_review_until_a_policy_says_so() -> None:
    result = v2(SECOND_TOKEN, "last_name")
    assert LOSSY_FORCES_REVIEW == frozenset() and not (LOSSY_CODES & REVIEW_FORCING_CODES_V2)
    status, _ = review_decision_v2(MappingType.TRANSFORMATION, result, 1.0)
    assert status is ReviewStatus.AUTO_ACCEPTED
    forced, reasons = review_decision_v2(
        MappingType.TRANSFORMATION, result, 1.0, forcing=REVIEW_FORCING_CODES_V2 | LOSSY_CODES
    )
    assert forced is ReviewStatus.NEEDS_REVIEW and "warning:LOSSY_TRUNCATION" in reasons


def test_the_correct_remainder_pipeline_is_not_flagged_but_first_token_is_a_known_lossy_case() -> (
    None
):
    assert codes(v2(REMAINDER, "last_name")) == set(), (
        "Mary Ann Smith -> Ann Smith keeps everything"
    )
    first = v2(FIRST_TOKEN, "first_name")
    assert codes(first) == {Code.LOSSY_TRUNCATION}, (
        "correct per the key, still lossy: informational"
    )
    assert first.status is Status.WARN


# ---- the null and constant checks --------------------------------------------------------------


def test_records_with_a_null_source_are_not_counted() -> None:
    phone = proposal(
        "phone",
        MappingType.TRANSFORMATION,
        ("phoneNumber",),
        {"op": "COPY", "field": "phoneNumber"},
        {"op": "ADD_PREFIX", "prefix": "+"},
    )
    assert any(u["phoneNumber"] is None for u in USERS)
    assert codes(v2(phone, "phone")) == set()


def test_a_single_constant_output_for_varying_sources_is_flagged() -> None:
    always_standard = proposal(
        "tier",
        MappingType.DERIVED,
        ("segment",),
        {"op": "COPY", "field": "segment"},
        {
            "op": "MAP_ENUM",
            "mapping": {"SMB": "STANDARD", "MIDMARKET": "STANDARD", "ENTERPRISE": "STANDARD"},
        },
    )
    result = validate_proposal_v2(
        always_standard,
        source_fields=entity_fields("crm.v1.json", "Customer"),
        target_field=entity_fields("support.v1.json", "User")["tier"],
        samples=CUSTOMERS,
    )
    assert Code.CONSTANT_OUTPUT in codes(result)
    assert Code.CONSTANT_OUTPUT in REVIEW_FORCING_CODES_V2


def test_a_declared_constant_mapping_is_not_flagged() -> None:
    declared = proposal("tier", MappingType.CONSTANT, (), {"op": "CONSTANT", "value": "STANDARD"})
    result = validate_proposal_v2(
        declared,
        source_fields=entity_fields("crm.v1.json", "Customer"),
        target_field=entity_fields("support.v1.json", "User")["tier"],
        samples=CUSTOMERS,
    )
    assert codes(result) == set()


def test_the_correct_enum_maps_are_not_flagged_as_constant_or_null() -> None:
    support_fields = entity_fields("support.v1.json", "User")
    crm_fields = entity_fields("crm.v1.json", "Customer")
    for spec, target in ((TIER, "tier"), (STATE, "accountState")):
        result = validate_proposal_v2(
            proposal(*spec),
            source_fields=crm_fields,
            target_field=support_fields[target],
            samples=CUSTOMERS,
        )
        assert not codes(result) & REVIEW_FORCING_CODES_V2, spec[0]


def test_lossy_collapse_is_found_for_pipelines_other_than_map_enum() -> None:
    target = Field(name="t", path="t", json_type="string", required=True)
    collapse = proposal(
        "t",
        MappingType.TRANSFORMATION,
        ("segment",),
        {"op": "COPY", "field": "segment"},
        {"op": "REGEX_REPLACE", "pattern": r"^(SMB|MIDMARKET)$", "replacement": "SMALL"},
    )
    result = validate_proposal_v2(
        collapse, source_fields=CRM, target_field=target, samples=CUSTOMERS
    )
    assert Code.LOSSY_COLLAPSE in codes(result)
    keeps = proposal(
        "t",
        MappingType.TRANSFORMATION,
        ("segment",),
        {"op": "COPY", "field": "segment"},
        {"op": "REGEX_REPLACE", "pattern": r"^SMB$", "replacement": "SMALL"},
    )
    assert Code.LOSSY_COLLAPSE not in codes(
        validate_proposal_v2(keeps, source_fields=CRM, target_field=target, samples=CUSTOMERS)
    )


def test_map_enum_collapse_is_not_reported_twice() -> None:
    result = validate_proposal_v2(
        proposal(*TIER),
        source_fields=CRM,
        target_field=entity_fields("support.v1.json", "User")["tier"],
        samples=CUSTOMERS,
    )
    assert Code.INFORMATION_LOSS_ENUM in codes(result) and Code.LOSSY_COLLAPSE not in codes(result)


def test_too_few_records_means_no_data_checks() -> None:
    assert codes(v2(NEVER_MATCHES, "last_name", records=USERS[:1])) == set()
    assert codes(v2(NEVER_MATCHES, "last_name", records=[])) == {Code.NO_SAMPLES}


def test_v2_leaves_unresolved_and_failed_results_exactly_as_v1_has_them() -> None:
    from app.mapping.proposal import Proposal

    unresolved = Proposal.unresolved("segment", "no source")
    kwargs: dict[str, Any] = {
        "source_fields": SUPPORT,
        "target_field": CRM["segment"],
        "samples": USERS,
    }
    assert validate_proposal_v2(unresolved, **kwargs) == validate_proposal(unresolved, **kwargs)
    broken = proposal(
        "status",
        MappingType.TRANSFORMATION,
        ("accountState",),
        {"op": "COPY", "field": "accountState"},
    )
    kwargs = {"source_fields": SUPPORT, "target_field": CRM["status"], "samples": USERS}
    assert validate_proposal(broken, **kwargs).status is Status.FAIL
    assert validate_proposal_v2(broken, **kwargs) == validate_proposal(broken, **kwargs)


def test_v2_never_changes_what_v1_reports() -> None:
    for candidate, target in (
        (NEVER_MATCHES, "last_name"),
        (SECOND_TOKEN, "last_name"),
        (REMAINDER, "last_name"),
    ):
        base = validate_proposal(
            candidate, source_fields=SUPPORT, target_field=CRM[target], samples=USERS
        )
        extended = v2(candidate, target)
        assert extended.reasons[: len(base.reasons)] == base.reasons


# ---- properties --------------------------------------------------------------------------------

values = st.one_of(st.none(), st.text(alphabet="abc XYZ0", max_size=8))
records = st.lists(st.fixed_dictionaries({"a": values, "b": values}), max_size=8)
PIPELINES: list[list[dict[str, Any]]] = [
    [{"op": "COPY", "field": "a"}],
    [{"op": "COPY", "field": "a"}, {"op": "SPLIT_PART", "separator": " ", "index": 1}],
    [{"op": "COPY", "field": "a"}, {"op": "SPLIT_PART", "separator": " ", "index": -1}],
    [{"op": "COPY", "field": "a"}, {"op": "REGEX_EXTRACT", "pattern": r"^(\S+) ", "group": 1}],
    [{"op": "COPY", "field": "a"}, {"op": "REGEX_EXTRACT", "pattern": r"^(?!)(x)", "group": 1}],
    [{"op": "JOIN_NONNULL", "fields": ["a", "b"], "separator": "-", "trim": True}],
]
SOURCE = {
    "a": Field(name="a", path="a", json_type="string", nullable=True, required=False),
    "b": Field(name="b", path="b", json_type="string", nullable=True, required=False),
}
TARGET = Field(name="t", path="t", json_type="string", nullable=True, required=False)
SEVERITY = {Status.PASS: 0, Status.WARN: 1, Status.FAIL: 2}


def candidate(index: int) -> Any:
    steps = PIPELINES[index]
    sources = tuple(steps[0].get("fields", [steps[0].get("field")]))
    kind = MappingType.COMPOSITE if len(sources) > 1 else MappingType.TRANSFORMATION
    return proposal("t", kind, sources, *steps)


@settings(max_examples=200, deadline=None)
@given(st.integers(min_value=0, max_value=len(PIPELINES) - 1), records)
def test_property_v2_only_adds_to_v1_and_never_raises(
    index: int, rows: list[dict[str, JsonScalar]]
) -> None:
    p = candidate(index)
    base = validate_proposal(p, source_fields=SOURCE, target_field=TARGET, samples=rows)
    extended = validate_proposal_v2(p, source_fields=SOURCE, target_field=TARGET, samples=rows)
    assert extended.reasons[: len(base.reasons)] == base.reasons
    assert SEVERITY[extended.status] >= SEVERITY[base.status]
    if base.status is Status.FAIL:
        assert extended == base


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.text(alphabet="abc XYZ0", min_size=1, max_size=8).filter(lambda t: t.strip() != ""),
        min_size=2,
        max_size=8,
    )
)
def test_property_a_pipeline_that_always_yields_null_is_always_flagged_and_forces_review(
    names: list[str],
) -> None:
    rows: list[dict[str, JsonScalar]] = [{"a": n, "b": None} for n in names]
    result = validate_proposal_v2(
        candidate(4), source_fields=SOURCE, target_field=TARGET, samples=rows
    )
    assert Code.MOSTLY_NULL_OUTPUT in {r.code for r in result.reasons}
    status, _ = review_decision_v2(MappingType.TRANSFORMATION, result, 1.0)
    assert status is ReviewStatus.NEEDS_REVIEW


@settings(max_examples=200, deadline=None)
@given(st.lists(st.text(alphabet="abc XYZ0", min_size=1, max_size=8), min_size=2, max_size=8))
def test_property_copying_non_null_values_is_never_flagged_null_or_constant(
    names: list[str],
) -> None:
    rows: list[dict[str, JsonScalar]] = [{"a": n, "b": None} for n in names]
    result = validate_proposal_v2(
        candidate(0), source_fields=SOURCE, target_field=TARGET, samples=rows
    )
    assert not {r.code for r in result.reasons} & {Code.MOSTLY_NULL_OUTPUT, Code.CONSTANT_OUTPUT}
