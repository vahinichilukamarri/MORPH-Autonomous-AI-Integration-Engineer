"""Operation analysis, review gate and package generation (condition D), without a database."""

import pytest

from app.codegen.gate import check_ast
from app.codegen.generator import generate_package
from app.codegen.operations import PlanCode, PlanError, analyse
from app.codegen.review_gate import GateStatus, Reason, decide
from app.mapping.confidence import ReviewStatus
from tests.codegen.fixtures import (
    S1_PIPELINES,
    S3_PIPELINES,
    S3_SEGMENT_OVERRIDE,
    mapped,
    model,
    s1_input,
    s3_input,
)


def test_crm_to_support_plan() -> None:
    plan = analyse(model("crm.v1", "crm"), "Customer", model("support.v1", "support"), "User")
    assert (plan.source.mode, plan.source.key_field, plan.source.list_path) == (
        "LIST", "customer_id", "/customers",
    )  # fmt: skip
    assert (plan.source.page_param, plan.source.size_param) == ("page", "page_size")
    assert (plan.source.items_key, plan.source.total_key) == ("items", "total")
    assert plan.source.auth.kind == "apiKey" and plan.source.auth.header == "X-API-Key"
    t = plan.target
    assert (t.mode, t.id_field, t.update_method) == ("UPSERT", "userId", "PUT")
    assert t.get_path == "/users/{id}" and t.auth.kind == "bearer" and not t.id_assigned_by_target


def test_support_to_crm_plan_has_no_list_and_no_target_ids() -> None:
    plan = analyse(model("support.v1", "support"), "User", model("crm.v1", "crm"), "Customer")
    assert (plan.source.mode, plan.source.key_field, plan.source.get_path) == (
        "KEYS", "userId", "/users/{id}",
    )  # fmt: skip
    t = plan.target
    assert (t.mode, t.id_field, t.update_method, t.create_path) == (
        "CREATE_UPDATE", "customer_id", "PATCH", "/customers",
    )  # fmt: skip
    assert t.id_assigned_by_target and t.natural_key == "email" and t.list_path == "/customers"
    assert "customer_id" not in {f.name for f in t.create_fields}


def test_unsupported_shapes_are_reported_not_guessed() -> None:
    with pytest.raises(PlanError) as caught:
        analyse(model("crm.v1", "crm"), "Customer", model("crm.v1", "crm"), "Nope")
    assert caught.value.code is PlanCode.UNKNOWN_ENTITY
    with pytest.raises(PlanError) as caught:  # CRM as a target for Support's list-less read side
        analyse(model("support.v1", "support"), "User", model("support.v1", "support"), "Nope")
    assert caught.value.code is PlanCode.UNKNOWN_ENTITY


def test_gate_open_when_everything_is_accepted() -> None:
    inp = s1_input()
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    decision = decide(inp, plan, allow_partial=False)
    assert decision.status is GateStatus.OPEN and not decision.excluded
    assert {m.target_field for m in decision.included} == set(S1_PIPELINES)


def test_gate_blocks_on_a_required_field_that_needs_review() -> None:
    fields = mapped(S1_PIPELINES, tier=(ReviewStatus.NEEDS_REVIEW, S1_PIPELINES["tier"]))
    inp = s1_input(fields)
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    decision = decide(inp, plan, allow_partial=True)
    assert decision.status is GateStatus.BLOCKED
    assert [(e.target_field, e.reason) for e in decision.blocking] == [
        ("tier", Reason.NEEDS_REVIEW)
    ]


def test_human_approval_unblocks() -> None:
    for status in (ReviewStatus.APPROVED, ReviewStatus.OVERRIDDEN):
        inp = s1_input(mapped(S1_PIPELINES, tier=(status, S1_PIPELINES["tier"])))
        plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
        assert decide(inp, plan, allow_partial=False).status is GateStatus.OPEN


def test_optional_exclusions_need_allow_partial() -> None:
    fields = mapped(
        S1_PIPELINES, externalRef=(ReviewStatus.NEEDS_REVIEW, S1_PIPELINES["externalRef"])
    )
    inp = s1_input(fields)
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    strict = decide(inp, plan, allow_partial=False)
    assert strict.status is GateStatus.BLOCKED
    assert [e.reason for e in strict.blocking] == [Reason.PARTIAL_NOT_ALLOWED]
    partial = decide(inp, plan, allow_partial=True)
    assert partial.status is GateStatus.PARTIAL
    assert "externalRef" not in {m.target_field for m in partial.included}
    assert [e.target_field for e in partial.excluded] == ["externalRef"]


def test_s3_is_blocked_without_the_segment_decision() -> None:
    inp = s3_input()
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    decision = decide(inp, plan, allow_partial=True)
    assert decision.status is GateStatus.BLOCKED
    assert [(e.target_field, e.reason) for e in decision.blocking] == [
        ("segment", Reason.UNRESOLVED)
    ]
    assert set(decision.not_writable) == {"customer_id", "created_at"}


def test_s3_with_a_human_constant_for_segment() -> None:
    fields = mapped(S3_PIPELINES, segment=(ReviewStatus.OVERRIDDEN, S3_SEGMENT_OVERRIDE))
    inp = s3_input(fields)
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    decision = decide(inp, plan, allow_partial=False)
    assert decision.status is GateStatus.OPEN
    package = generate_package(inp, plan, decision)
    target = package.strategy["target"]
    assert target["create_only"] == ("segment",)  # constants are never sent on update
    assert "segment" not in target["update_fields"] and "segment" in target["create_fields"]
    assert "created_at" not in target["create_fields"]  # not writable, never sent
    assert target["omit_if_null"] == ("status",)  # optional and not nullable
    assert target["natural_key"] == "email" and target["id_assigned_by_target"] is True


@pytest.mark.parametrize("which", ["s1", "s3"])
def test_generated_package_passes_the_ast_gate_and_runs(which: str) -> None:
    if which == "s1":
        inp = s1_input()
    else:
        inp = s3_input(mapped(S3_PIPELINES, segment=(ReviewStatus.OVERRIDDEN, S3_SEGMENT_OVERRIDE)))
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    package = generate_package(inp, plan, decide(inp, plan, allow_partial=False))
    result = check_ast(package.files)
    assert result.passed, [f.as_dict() for f in result.findings]
    assert set(package.files) == {
        "integration/__init__.py",
        "integration/__main__.py",
        "integration/clients.py",
        "integration/strategy.py",
        "integration/transform.py",
    }
    again = generate_package(inp, plan, decide(inp, plan, allow_partial=False))
    assert again.files == package.files  # deterministic


def test_generated_code_never_contains_secrets_or_urls() -> None:
    inp = s1_input()
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    files = generate_package(inp, plan, decide(inp, plan, allow_partial=False)).files
    text = "\n".join(files.values())
    assert "http://" not in text and "https://" not in text
    assert 'env("MORPH_SOURCE_CREDENTIAL")' in text and "ApiKeyAuth('X-API-Key'" in text
