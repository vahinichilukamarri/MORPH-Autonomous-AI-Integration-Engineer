"""Model failures use an attempt; infrastructure failures pause. Truncation is the model's."""

from app.codegen.gate import Finding, GateResult, Rule
from app.codegen.sandbox import LimitsNotEnforced, SandboxError
from app.llm.base import (
    Attempt,
    CallMetadata,
    LLMError,
    ModelUnavailableError,
    RateLimitExhausted,
    ReplayMissError,
)
from app.repair.failures import (
    EMPTY_OUTPUT,
    TRUNCATED,
    attempt_items,
    has_infrastructure_failure,
    is_infrastructure_error,
    is_infrastructure_outcome,
)
from app.repair.feedback import Stage


def attempt(text: str, error: str | None, finish: str | None = "stop") -> Attempt:
    meta = CallMetadata("groq", "m", "h", 1, 1, 1, finish_reason=finish)
    return Attempt(meta, text, error)


def test_a_valid_reply_has_no_feedback() -> None:
    assert attempt_items(attempt('{"a": 1}', None)) == []


def test_a_reply_cut_off_at_the_output_limit_is_a_distinct_model_failure() -> None:
    (only,) = attempt_items(attempt('{"a": 1, "b', "<root>: Invalid JSON", finish="length"))
    assert (only.stage, only.code) == (Stage.PROPOSAL, TRUNCATED)
    assert "shorter" in only.message


def test_an_empty_reply_is_a_model_failure() -> None:
    (only,) = attempt_items(attempt("  \n", "<root>: Invalid JSON: EOF"))
    assert only.code == EMPTY_OUTPUT


def test_non_json_and_invalid_replies_are_model_failures_with_the_validator_text() -> None:
    (only,) = attempt_items(attempt("not json", "<root>: Invalid JSON: expected value"))
    assert only.code == "INVALID_JSON"
    first, second = attempt_items(attempt("{}", "a: bad; b: worse"))
    assert (first.code, second.code) == ("a", "b")


def test_provider_and_sandbox_failures_are_infrastructure() -> None:
    assert is_infrastructure_error(RateLimitExhausted("429 persisted"))
    assert is_infrastructure_error(ModelUnavailableError("gone"))
    assert is_infrastructure_error(LLMError("HTTP 500"))
    assert is_infrastructure_error(LimitsNotEnforced("no cgroup"))
    assert is_infrastructure_error(SandboxError("docker failed"))
    assert not is_infrastructure_error(ValueError("a bug"))


def test_a_missing_replay_is_a_hard_error_never_a_pause() -> None:
    assert isinstance(ReplayMissError("x"), LLMError)  # why it needs an explicit exception
    assert not is_infrastructure_error(ReplayMissError("no recorded response"))


def test_only_a_container_that_never_started_is_infrastructure() -> None:
    assert is_infrastructure_outcome("START_FAILED")
    assert not any(is_infrastructure_outcome(o) for o in ("OK", "NONZERO", "TIMEOUT", "OOM"))
    started = Finding("", 0, Rule.TOOL_FAILURE, "ruff did not complete: START_FAILED, exit None")
    timed_out = Finding("", 0, Rule.TOOL_FAILURE, "mypy did not complete: TIMEOUT, exit None")
    assert has_infrastructure_failure(GateResult("ruff", (started,)))
    assert not has_infrastructure_failure(GateResult("mypy", (timed_out,)))
    assert not has_infrastructure_failure(GateResult("ast", ()))
