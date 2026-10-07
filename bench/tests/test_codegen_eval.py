"""The code-generation evaluation: report, saved results and the refusal rules (no Docker)."""

from pathlib import Path

import pytest

from morph_bench.codegen_eval import (
    GeneratedTests,
    LLMUse,
    UnitResult,
    append_result,
    load_results,
    oracle_summary,
    render_report,
    response_model_id,
)
from scripts.run_codegen_eval import DEFAULT_OUTPUT, TEST_ONLY_OUTPUT, check_options

S1 = "crm_customer_to_support_user"
S3 = "support_user_to_crm_customer"


def unit(**changes: object) -> UnitResult:
    base: dict[str, object] = {
        "scenario_id": S1,
        "input_set": "approved",
        "condition": "D",
        "status": "READY",
        "gate": {"ast": True, "ruff": True, "mypy": True},
        "generated_tests": GeneratedTests(outcome="OK", total=5, passed=5),
        "oracle": {"O1": (18, 18), "O2": (13, 13), "O8": (4, 4)},
        "integration_correct": True,
    }
    return UnitResult.model_validate({**base, **changes})


def test_oracle_summary_counts_and_decides_correctness_from_o1_to_o7_only() -> None:
    checks: list[dict[str, object]] = [
        {"category": "O1", "name": "a", "passed": True},
        {"category": "O1", "name": "b", "passed": True},
        {"category": "O5", "name": "c", "passed": False},
        {"category": "O8", "name": "d", "passed": False},
    ]
    summary, failed, correct = oracle_summary(checks)
    assert summary == {"O1": (2, 2), "O5": (0, 1), "O8": (0, 1)}
    assert failed == ["O5.c", "O8.d"] and correct is False
    assert oracle_summary([{"category": "O8", "name": "x", "passed": False}])[2] is True


def test_report_states_n_conditions_and_every_unit_including_blocked_ones() -> None:
    results = [
        unit(),
        unit(scenario_id=S3, input_set="as_proposed", status="BLOCKED_PENDING_REVIEW",
             gate={}, generated_tests=None, oracle=None, integration_correct=None,
             blocked_reasons=["segment: UNRESOLVED"]),
        unit(condition="L1", provider="groq", model="m", llm=LLMUse(calls=2, input_tokens=100,
             output_tokens=50, reasoning_tokens=10, latency_ms=900, invalid_outputs=1),
             integration_correct=False, oracle_failed_checks=["O2.second_run_ok"]),
    ]  # fmt: skip
    text = render_report(results, "2026-10-07", test_only=False)
    assert "**N = 3**" in text and "groq/m" in text
    assert "BLOCKED_PENDING_REVIEW" in text and "segment: UNRESOLVED" in text
    assert "`O2.second_run_ok`" in text
    assert "| S1 approved | L1 | 2 | 100 | 50 | 10 | 900 | 1 |" in text
    assert "TEST-ONLY" not in text
    assert "TEST-ONLY" in render_report(results, "2026-10-07", test_only=True)
    assert "18/18" in text and "-" in text  # a blocked unit has no oracle cells


def test_no_number_is_invented_a_report_without_model_units_says_so() -> None:
    text = render_report([unit()], "2026-10-07", test_only=False)
    assert "none (condition D uses no model)" in text and "Model use" not in text


def test_results_are_saved_and_the_latest_record_of_a_unit_wins(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    append_result(path, unit(status="GATE_FAILED"))
    append_result(path, unit(status="READY"))
    append_result(path, unit(scenario_id=S3))
    loaded = load_results(path)
    assert len(loaded) == 2
    assert {r.scenario_id: r.status for r in loaded} == {S1: "READY", S3: "READY"}
    assert load_results(tmp_path / "missing.jsonl") == []


@pytest.mark.parametrize(
    ("conditions", "provider", "test_only", "confirm", "output", "expected"),
    [
        (["D"], None, False, False, DEFAULT_OUTPUT, None),
        (["D"], None, True, False, DEFAULT_OUTPUT, "never overwrite"),
        (["L1"], None, False, False, DEFAULT_OUTPUT, "--provider"),
        (["L1"], "groq", False, False, DEFAULT_OUTPUT, "--confirm-real-run"),
        (["L2"], "groq", False, True, DEFAULT_OUTPUT, None),
        (["L1"], "replay", False, False, DEFAULT_OUTPUT, "--test-only"),
        (["L1"], "replay", True, False, TEST_ONLY_OUTPUT, None),
        (["L1"], "groq", True, True, TEST_ONLY_OUTPUT, "never calls a real provider"),
    ],
)
def test_refusal_rules(
    conditions: list[str],
    provider: str | None,
    test_only: bool,
    confirm: bool,
    output: Path,
    expected: str | None,
) -> None:
    refusal = check_options(conditions, provider, test_only, confirm, output)
    assert (refusal is None) if expected is None else (refusal is not None and expected in refusal)


def test_the_model_label_is_the_id_the_replies_reported_not_the_requested_one() -> None:
    assert response_model_id(["openai/gpt-oss-120b"] * 3) == "openai/gpt-oss-120b"
    assert response_model_id(["b", "a", "a"]) == "a+b"
    assert response_model_id([]) is None
    result = unit(
        condition="L2", provider="groq", model="groq", response_model="openai/gpt-oss-120b",
        llm=LLMUse(calls=1),
    )  # fmt: skip
    assert "groq/openai/gpt-oss-120b" in render_report([result], "2026-10-07", test_only=False)
