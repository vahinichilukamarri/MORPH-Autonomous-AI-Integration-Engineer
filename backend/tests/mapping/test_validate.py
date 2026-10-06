from typing import Any

import pytest

from app.discovery.models import Constraints, Field
from app.mapping.proposal import Certainty, MappingType, Proposal
from app.mapping.validate import (
    Code,
    RetrievalSignal,
    Status,
    ValidationResult,
    validate_proposal,
    validate_run,
)
from tests.mapping.helpers import (
    CREATED_AT,
    FULL_NAME,
    PHONE,
    STATE,
    TIER,
    USER_ID,
    entity_fields,
    proposal,
    samples,
)

SOURCE = entity_fields("crm.v1.json", "Customer")
TARGET = entity_fields("support.v1.json", "User")
CRM_SAMPLES = samples("crm_customer.v1.json")
SUPPORT_SAMPLES = samples("support_user.v1.json")


def check(
    p: Proposal,
    target: str,
    *,
    source: dict[str, Field] | None = None,
    target_fields: dict[str, Field] | None = None,
    records: Any = None,
    retrieval: RetrievalSignal | None = None,
) -> ValidationResult:
    return validate_proposal(
        p,
        source_fields=source or SOURCE,
        target_field=(target_fields or TARGET)[target],
        samples=CRM_SAMPLES if records is None else records,
        retrieval=retrieval,
    )


def codes(result: Any) -> set[Code]:
    return {r.code for r in result.reasons}


def synthetic(**kwargs: Any) -> Field:
    base: dict[str, Any] = {"name": "t", "path": "t", "json_type": "string", "required": True}
    return Field(**{**base, **kwargs})


# ---- the correct S1 pipelines pass on all samples ----------------------------------------------


@pytest.mark.parametrize("spec", [USER_ID, FULL_NAME, PHONE, STATE, CREATED_AT])
def test_correct_pipelines_pass(spec: Any) -> None:
    result = check(proposal(*spec), spec[0])
    assert result.status is Status.PASS, result.reasons
    assert result.samples_checked == len(CRM_SAMPLES)
    assert result.outputs_preview


def test_many_to_one_enum_is_flagged_as_information_loss() -> None:
    result = check(proposal(*TIER), "tier")
    assert result.status is Status.WARN
    assert codes(result) == {Code.INFORMATION_LOSS_ENUM}


def test_coalesce_of_several_sources_is_flagged() -> None:
    p = proposal(
        "fullName",
        MappingType.COMPOSITE,
        ("first_name", "last_name"),
        {"op": "COALESCE", "fields": ["first_name", "last_name"]},
    )
    assert Code.INFORMATION_LOSS_COALESCE in codes(check(p, "fullName"))


# ---- static checks -----------------------------------------------------------------------------


def test_missing_source_field_fails_before_execution() -> None:
    p = proposal(
        "userId", MappingType.TRANSFORMATION, ("cust_id",), {"op": "COPY", "field": "cust_id"}
    )
    result = check(p, "userId")
    assert result.status is Status.FAIL and codes(result) == {Code.SOURCE_FIELD_MISSING}
    assert result.samples_checked == 0


def test_declared_sources_must_match_what_the_pipeline_reads() -> None:
    p = proposal(
        "userId", MappingType.TRANSFORMATION, ("email",), {"op": "COPY", "field": "customer_id"}
    )
    assert Code.SOURCES_MISMATCH in codes(check(p, "userId"))


@pytest.mark.parametrize(
    ("kind", "sources", "steps"),
    [
        (
            MappingType.DIRECT,
            ("customer_id",),
            [{"op": "COPY", "field": "customer_id"}, {"op": "CAST", "to": "str"}],
        ),
        (
            MappingType.DIRECT,
            ("first_name", "last_name"),
            [{"op": "COALESCE", "fields": ["first_name", "last_name"]}],
        ),
        (MappingType.COMPOSITE, ("first_name",), [{"op": "COPY", "field": "first_name"}]),
        (MappingType.CONSTANT, ("status",), [{"op": "COPY", "field": "status"}]),
        (MappingType.TRANSFORMATION, (), [{"op": "CONSTANT", "value": "x"}]),
    ],
)
def test_mapping_type_must_agree_with_the_pipeline(
    kind: MappingType, sources: tuple[str, ...], steps: list[dict[str, Any]]
) -> None:
    p = proposal("fullName", kind, sources, *steps)
    assert Code.TYPE_INCONSISTENT in codes(check(p, "fullName"))


def test_constant_mapping_is_valid() -> None:
    p = proposal("tier", MappingType.CONSTANT, (), {"op": "CONSTANT", "value": "STANDARD"})
    assert check(p, "tier").status is Status.PASS


def test_unresolved_is_a_flagged_warning_with_the_reason() -> None:
    result = check(Proposal.unresolved("tier", "no source for tier"), "tier")
    assert result.status is Status.WARN
    assert result.reasons[0].code is Code.UNRESOLVED
    assert "no source" in result.reasons[0].detail


def test_no_samples_is_a_warning_not_a_pass() -> None:
    result = check(proposal(*USER_ID), "userId", records=[])
    assert result.status is Status.WARN and codes(result) == {Code.NO_SAMPLES}


# ---- dynamic checks: every sample is executed --------------------------------------------------


def test_execution_errors_name_the_sample() -> None:
    p = proposal(
        "accountState",
        MappingType.TRANSFORMATION,
        ("status",),
        {"op": "COPY", "field": "status"},
        {"op": "MAP_ENUM", "mapping": {"ACTIVE": "ENABLED"}},
    )
    result = check(p, "accountState")
    assert result.status is Status.FAIL
    reason = next(r for r in result.reasons if r.code is Code.EXECUTION_ERROR)
    assert "UnmappedValueError" in reason.detail and "samples" in reason.detail


def test_wrong_output_type_fails() -> None:
    p = proposal(
        "userId",
        MappingType.TRANSFORMATION,
        ("customer_id",),
        {"op": "COPY", "field": "customer_id"},
        {"op": "REGEX_EXTRACT", "pattern": r"^C-(\d+)$", "group": 1},
    )
    assert Code.TYPE_MISMATCH in codes(check(p, "userId"))


def test_enum_violation() -> None:
    p = proposal(
        "tier",
        MappingType.DERIVED,
        ("segment",),
        {"op": "COPY", "field": "segment"},
        {"op": "MAP_ENUM", "mapping": {"SMB": "GOLD", "MIDMARKET": "GOLD", "ENTERPRISE": "GOLD"}},
    )
    assert Code.ENUM_VIOLATION in codes(check(p, "tier"))


def test_pattern_violation_when_the_plus_is_not_stripped() -> None:
    p = proposal("phoneNumber", MappingType.DIRECT, ("phone",), {"op": "COPY", "field": "phone"})
    assert Code.PATTERN_VIOLATION in codes(check(p, "phoneNumber"))


def test_range_violation() -> None:
    zero = [{"customer_id": "C-0"}]
    result = check(proposal(*USER_ID), "userId", records=zero)
    assert Code.RANGE_VIOLATION in codes(result)


def test_length_violation() -> None:
    target = {"t": synthetic(constraints=Constraints(max_length=3))}
    p = proposal("t", MappingType.DIRECT, ("first_name",), {"op": "COPY", "field": "first_name"})
    result = check(p, "t", target_fields=target)
    assert Code.LENGTH_VIOLATION in codes(result)


def test_format_violation_for_date_time_targets() -> None:
    target = {"t": synthetic(format="date-time")}
    p = proposal("t", MappingType.DIRECT, ("email",), {"op": "COPY", "field": "email"})
    assert Code.FORMAT_MISMATCH in codes(check(p, "t", target_fields=target))
    ok = proposal("t", MappingType.DIRECT, ("created_at",), {"op": "COPY", "field": "created_at"})
    assert check(ok, "t", target_fields=target).status is Status.PASS


def test_null_from_non_nullable_sources_fails() -> None:
    p = proposal(
        "userId",
        MappingType.TRANSFORMATION,
        ("customer_id",),
        {"op": "COPY", "field": "customer_id"},
        {"op": "REGEX_EXTRACT", "pattern": r"^X-(\d+)$", "group": 1},
        {"op": "CAST", "to": "int"},
    )
    result = check(p, "userId")
    assert result.status is Status.FAIL
    assert Code.NULL_FOR_REQUIRED_TARGET in codes(result)


def test_nullable_source_into_required_target_is_a_warning_not_a_failure() -> None:
    """S3's customer_id: Support externalRef is nullable, CRM customer_id is required."""
    support = entity_fields("support.v1.json", "User")
    crm = entity_fields("crm.v1.json", "Customer")
    p = proposal(
        "customer_id",
        MappingType.DIRECT,
        ("externalRef",),
        {"op": "COPY", "field": "externalRef"},
    )
    result = check(p, "customer_id", source=support, target_fields=crm, records=SUPPORT_SAMPLES)
    assert result.status is Status.WARN
    assert codes(result) == {Code.NULLABLE_TO_REQUIRED}
    assert result.outputs_preview


def test_null_into_a_nullable_target_is_fine() -> None:
    p = proposal("phoneNumber", MappingType.TRANSFORMATION, *PHONE[2:])
    assert check(p, "phoneNumber").status is Status.PASS


# ---- ambiguity ---------------------------------------------------------------------------------


def test_alternatives_with_other_sources_make_a_mapping_ambiguous() -> None:
    p = proposal(
        *USER_ID,
        alternatives=((("email",), "also looks like an id"),),
    )
    result = check(p, "userId")
    assert result.ambiguous and Code.AMBIGUOUS_ALTERNATIVES in codes(result)
    same = proposal(*USER_ID, alternatives=((("customer_id",), "same source"),))
    assert not check(same, "userId").ambiguous


def test_close_retrieval_candidates_make_a_mapping_ambiguous() -> None:
    close = RetrievalSignal(
        ranks={"customer_id": 1}, top_gap=0.005, top_two=("customer_id", "email")
    )
    assert check(proposal(*USER_ID), "userId", retrieval=close).ambiguous
    clear = RetrievalSignal(ranks={"customer_id": 1}, top_gap=0.2, top_two=("customer_id", "email"))
    assert not check(proposal(*USER_ID), "userId", retrieval=clear).ambiguous
    elsewhere = RetrievalSignal(ranks={"customer_id": 4}, top_gap=0.001, top_two=("email", "phone"))
    assert not check(proposal(*USER_ID), "userId", retrieval=elsewhere).ambiguous


# ---- run level ---------------------------------------------------------------------------------


def test_run_level_coverage_checks() -> None:
    fields = list(TARGET.values())
    full = [proposal(*s) for s in (USER_ID, FULL_NAME, PHONE, STATE, TIER, CREATED_AT)]
    full += [
        proposal(
            "externalRef",
            MappingType.DIRECT,
            ("customer_id",),
            {"op": "COPY", "field": "customer_id"},
        ),
        proposal("email_address", MappingType.DIRECT, ("email",), {"op": "COPY", "field": "email"}),
    ]
    assert validate_run(fields, full) == []
    missing = validate_run(fields, full[:-1])
    assert [r.code for r in missing] == [Code.TARGET_NOT_COVERED]
    duplicate = validate_run(fields, [*full, full[0]])
    assert [r.code for r in duplicate] == [Code.DUPLICATE_TARGET_COVERAGE]
    unresolved = [p for p in full if p.target_field != "userId"] + [
        Proposal.unresolved("userId", "cannot tell")
    ]
    assert [r.code for r in validate_run(fields, unresolved)] == [Code.REQUIRED_TARGET_UNRESOLVED]
    optional_unresolved = [p for p in full if p.target_field != "externalRef"] + [
        Proposal.unresolved("externalRef", "no source")
    ]
    assert validate_run(fields, optional_unresolved) == []


def test_certainty_label_does_not_change_validation() -> None:
    low = proposal(*PHONE, certainty=Certainty.LOW)
    assert check(low, "phoneNumber").status is Status.PASS
