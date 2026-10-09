"""The evaluator: precedence, default deny, approvals, budgets, determinism and replay."""

import pytest

from app.policy.evaluate import evaluate
from app.policy.models import (
    ApprovalCheck,
    CallContext,
    DataClass,
    Decision,
    Effect,
    Environment,
    Reason,
    Role,
)
from tests.policy.helpers import active, ctx

MOCK = Environment.MOCK
SYNTH = DataClass.SYNTHETIC


def test_nothing_matching_is_default_deny() -> None:
    record = evaluate(
        active(), ctx("get_system", effects=(Effect.MODEL_CALL,), model_involved=False)
    )
    assert (record.decision, record.reason) == (Decision.DENY, Reason.DEFAULT_DENY)
    assert record.rule_ids == ()


def test_deny_beats_needs_approval_beats_allow() -> None:
    # reader + model call on internal data: P06 does not apply (operator only), P07 denies
    reader = evaluate(
        active(), ctx("propose_mapping", role=Role.READER, environment=MOCK,
                      data_class=DataClass.INTERNAL),
    )  # fmt: skip
    assert reader.decision is Decision.DENY and "P07-reader-no-side-effects" in reader.rule_ids
    operator = evaluate(
        active(), ctx("propose_mapping", environment=MOCK, data_class=DataClass.INTERNAL)
    )
    assert operator.decision is Decision.NEEDS_APPROVAL
    allowed = evaluate(active(), ctx("propose_mapping", environment=MOCK, data_class=SYNTH))
    assert allowed.decision is Decision.ALLOW and allowed.rule_ids == (
        "P05-operator-model-synthetic",
    )


def test_the_floor_is_reported_before_the_policy() -> None:
    record = evaluate(
        active(), ctx("run_generated_tests", environment=Environment.PRODUCTION, url=True)
    )
    assert record.decision is Decision.DENY
    assert record.rule_ids[:2] == ("F02-no-url", "F01-exec-target-mock")
    assert record.reason is Reason.URL_DENIED  # the first floor hit decides the reason


@pytest.mark.parametrize(
    "environment", [Environment.STAGING, Environment.PRODUCTION, Environment.UNKNOWN]
)
def test_code_never_runs_on_a_target_that_is_not_a_mock(environment: Environment) -> None:
    for tool in ("run_generated_tests", "generate_integration", "repair_integration"):
        record = evaluate(active(), ctx(tool, environment=environment, data_class=SYNTH))
        assert (record.decision, record.reason) == (Decision.DENY, Reason.EXEC_TARGET_NOT_MOCK)


def test_a_read_on_a_production_system_is_still_allowed() -> None:
    record = evaluate(active(), ctx("get_integration", environment=Environment.PRODUCTION))
    assert record.decision is Decision.ALLOW


@pytest.mark.parametrize(
    "check",
    [
        ApprovalCheck.UNKNOWN, ApprovalCheck.NOT_DECIDED, ApprovalCheck.DENIED,
        ApprovalCheck.EXPIRED, ApprovalCheck.CONSUMED, ApprovalCheck.REQUEST_MISMATCH,
        ApprovalCheck.POLICY_CHANGED,
    ],
)  # fmt: skip
def test_every_way_an_approval_can_be_invalid_denies(check: ApprovalCheck) -> None:
    record = evaluate(
        active(),
        ctx("propose_mapping", environment=MOCK, data_class=DataClass.INTERNAL, approval=check),
    )
    assert (record.decision, record.reason) == (Decision.DENY, Reason.APPROVAL_INVALID)


def test_a_valid_approval_turns_needs_approval_into_allow_only() -> None:
    record = evaluate(
        active(), ctx("propose_mapping", environment=MOCK, data_class=DataClass.RESTRICTED,
                      approval=ApprovalCheck.VALID),
    )  # fmt: skip
    assert (record.decision, record.reason) == (Decision.ALLOW, Reason.APPROVAL_VALID)
    # a valid approval does not rescue a role that is not permitted
    reader = evaluate(
        active(), ctx("propose_mapping", role=Role.READER, environment=MOCK,
                      data_class=DataClass.RESTRICTED, approval=ApprovalCheck.VALID),
    )  # fmt: skip
    assert reader.decision is Decision.DENY


def test_the_budget_counts_model_calls_and_sandbox_runs_separately() -> None:
    limits = active().file.session_limits
    over_model = evaluate(
        active(),
        ctx(
            "repair_integration",
            environment=MOCK,
            data_class=SYNTH,
            model_calls_used=limits.max_model_calls,
        ),
    )
    assert over_model.reason is Reason.BUDGET_EXCEEDED
    over_runs = evaluate(
        active(),
        ctx("run_generated_tests", environment=MOCK, sandbox_runs_used=limits.max_sandbox_runs),
    )
    assert over_runs.reason is Reason.BUDGET_EXCEEDED
    # sandbox runs do not count against a tool that runs no code, and vice versa
    read = evaluate(active(), ctx("get_system", model_calls_used=999, sandbox_runs_used=999))
    assert read.decision is Decision.ALLOW
    last_ok = evaluate(
        active(),
        ctx("run_generated_tests", environment=MOCK, sandbox_runs_used=limits.max_sandbox_runs - 1),
    )
    assert last_ok.decision is Decision.ALLOW


def test_evaluation_is_deterministic_and_replays_from_the_recorded_inputs() -> None:
    original = ctx("propose_mapping", environment=MOCK, data_class=DataClass.INTERNAL)
    first, second = evaluate(active(), original), evaluate(active(), original)
    assert first == second
    replayed = evaluate(active(), CallContext.model_validate(first.inputs))
    assert replayed.replay_key() == first.replay_key()
    assert first.policy_version == "v1" and first.policy_hash == active().hash


def test_the_decision_does_not_depend_on_anything_outside_the_context() -> None:
    # the context has no free-text field: two calls with equal contexts get equal decisions
    a = evaluate(active(), ctx("ingest_contract"))
    b = evaluate(active(), ctx("ingest_contract"))
    assert (
        a.replay_key() == b.replay_key() == ("ALLOW", "ALLOWED_BY_RULE", ("P02-operator-ingest",))
    )
    assert set(CallContext.model_fields) == {
        "tool", "tool_known", "effects", "role", "environment", "data_class", "model_involved",
        "facts", "approval", "model_calls_used", "sandbox_runs_used",
    }  # fmt: skip
