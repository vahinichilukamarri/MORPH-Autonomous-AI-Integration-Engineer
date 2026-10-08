"""The request-size estimator, calibrated offline against every recorded v0.4 prompt.

CALIBRATION is data, not a model call: for each recorded v0.4 request, the number of characters the
provider was sent (system text, user message and strict response schema, measured by rebuilding the
request offline) and the provider's own ``prompt_tokens`` (the v0.4 replay fixtures).
The three re-ask prompts resemble repair prompts: a previous reply plus an error.
"""

import math

import pytest

from app.codegen.llm_codegen import (
    StrategyProposal,
    SyncModuleProposal,
    build_l1_request,
    build_l2_request,
)
from app.codegen.operations import analyse
from app.codegen.review_gate import decide
from app.llm.base import LLMRequest
from app.repair.sizing import (
    CHARS_PER_TOKEN,
    EXPECTED_OUTPUT_TOKENS,
    HARD_STOP_FACTOR,
    SizeEstimate,
    condition_measurable,
    estimate_request,
    exceeds_hard_limit,
    request_characters,
)
from tests.codegen.fixtures import s1_input

# (scenario, prompt, characters sent, provider prompt_tokens)
CALIBRATION = [
    ("S1", "L1 first turn", 17129, 4080),
    ("S1", "L1 re-ask", 19459, 4701),
    ("S1", "L2 first turn", 16442, 4002),
    ("S4", "L1 first turn", 17171, 4086),
    ("S4", "L1 re-ask", 19501, 4706),
    ("S4", "L2 first turn", 16484, 4008),
    ("S3", "L1 first turn", 16926, 4029),
    ("S3", "L1 re-ask", 19186, 4661),
    ("S3", "L2 first turn", 16239, 3951),
]


def error_percent(characters: int, actual: int) -> float:
    return 100 * (math.ceil(characters / CHARS_PER_TOKEN) - actual) / actual


def test_the_estimator_never_under_counts_a_recorded_prompt_and_stays_within_three_percent() -> (
    None
):
    errors = [error_percent(chars, actual) for _, _, chars, actual in CALIBRATION]
    assert min(errors) >= 0.0, "an estimate below the provider's count would let a request through"
    assert max(errors) <= 3.0, errors


def test_the_calibration_covers_re_ask_prompts_not_only_first_turns() -> None:
    assert {p for _, p, _, _ in CALIBRATION} == {"L1 first turn", "L1 re-ask", "L2 first turn"}
    assert len(CALIBRATION) == 9


def test_first_turn_prompts_fit_the_limit_with_room() -> None:
    inp = s1_input()
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    decision = decide(inp, plan, allow_partial=False)
    for condition, build, model in (
        ("L1R", build_l1_request, StrategyProposal),
        ("L2R", build_l2_request, SyncModuleProposal),
    ):
        request = build(inp, decision, temperature=0.0, max_output_tokens=4000)
        estimate = estimate_request(request, model, condition)
        assert 3_500 <= estimate.input_tokens <= 4_600, (condition, estimate)
        assert not exceeds_hard_limit(estimate, 8000)


def test_the_estimate_adds_the_largest_recorded_completion() -> None:
    request = LLMRequest("s" * 410, ("u" * 410,), "x")
    from pydantic import BaseModel

    class Empty(BaseModel):
        pass

    estimate = estimate_request(request, Empty, "L2R")
    assert estimate.characters == request_characters(request, Empty)
    assert estimate.input_tokens == math.ceil(estimate.characters / CHARS_PER_TOKEN)
    assert estimate.output_tokens == EXPECTED_OUTPUT_TOKENS["L2R"] == 1859
    assert estimate.total_tokens == estimate.input_tokens + 1859
    assert EXPECTED_OUTPUT_TOKENS["L1R"] == 1175


@pytest.mark.parametrize(
    ("total", "stops"),
    [(8000, False), (8800, False), (8801, True), (12000, True), (100, False)],
)
def test_the_hard_stop_is_more_than_ten_percent_over_the_limit(total: int, stops: bool) -> None:
    estimate = SizeEstimate(0, total, 0, total)
    assert exceeds_hard_limit(estimate, 8000) is stops
    assert HARD_STOP_FACTOR == 1.10


@pytest.mark.parametrize(
    ("runnable", "total", "measurable"),
    [(3, 3, True), (2, 3, True), (1, 3, False), (0, 3, False), (0, 0, False)],
)
def test_a_condition_needs_two_of_three_runnable_units_to_be_measurable(
    runnable: int, total: int, measurable: bool
) -> None:
    assert condition_measurable(runnable, total) is measurable
