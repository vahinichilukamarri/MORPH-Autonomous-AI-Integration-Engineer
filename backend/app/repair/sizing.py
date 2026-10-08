"""Request size: an offline-calibrated estimate and the pre-flight rule. Pure.

The estimate is deliberately simple and is an ESTIMATE: ``ceil(characters / CHARS_PER_TOKEN)`` plus
the largest completion the condition produced in v0.4. ``CHARS_PER_TOKEN`` was calibrated offline
against the provider's own ``prompt_tokens`` for every recorded v0.4 prompt (see
``tests/repair/test_sizing.py`` and ``docs/plans/v0.5-m2-report.md``). The characters counted are
what the provider is sent: the system text, the user message and the strict response schema.

The guard stops a unit before a call only if the estimate exceeds the per-minute token limit by more
than 10%. Anything below that is sent, and a provider "request too large" answer is handled as
``SIZE_REJECTED`` by the caller.
"""

import json
import math
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import BaseModel

from app.llm.base import LLMRequest, strictify_schema, user_message

CHARS_PER_TOKEN = 4.10
EXPECTED_OUTPUT_TOKENS = {"L1R": 1175, "L2R": 1859}  # the largest v0.4 completion_tokens
HARD_STOP_FACTOR = 1.10
DEFAULT_TPM_LIMIT = 8000  # observed in the v0.4 response headers; callers pass the live value
MIN_RUNNABLE_FRACTION = 2 / 3


@dataclass(frozen=True)
class SizeEstimate:
    characters: int
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def request_characters(request: LLMRequest, response_model: type[BaseModel]) -> int:
    schema = json.dumps(strictify_schema(response_model.model_json_schema()), sort_keys=True)
    return len(request.system) + len(user_message(request.parts)) + len(schema)


def estimate_request(
    request: LLMRequest, response_model: type[BaseModel], condition: str
) -> SizeEstimate:
    characters = request_characters(request, response_model)
    input_tokens = math.ceil(characters / CHARS_PER_TOKEN)
    output_tokens = EXPECTED_OUTPUT_TOKENS[condition]
    return SizeEstimate(characters, input_tokens, output_tokens, input_tokens + output_tokens)


def exceeds_hard_limit(estimate: SizeEstimate, tpm_limit: int) -> bool:
    """True only when the estimate is above the limit by more than 10%."""
    return estimate.total_tokens > tpm_limit * HARD_STOP_FACTOR


def condition_measurable(runnable: int, total: int) -> bool:
    """A condition with fewer than 2 of 3 runnable units is "not measurable at this tier"."""
    return total > 0 and runnable / total >= MIN_RUNNABLE_FRACTION
