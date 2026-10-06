"""The mapping agent: one LLM call per target field, returning a typed proposal.

The LLM only proposes. It never executes anything, has no tools, and every reply is parsed and
validated; a reply that is still invalid after the single re-ask becomes an UNRESOLVED proposal
with reason ``invalid_llm_output``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.discovery.models import Field
from app.llm.base import Attempt, BaseLLMProvider, LLMRequest
from app.mapping.prompts import (
    Mode,
    RetrievedField,
    build_user_prompt,
    system_prompt,
)
from app.mapping.proposal import INVALID_OUTPUT_REASON, LLMProposal, Proposal
from app.mapping.transform import JsonScalar

SCHEMA_NAME = "mapping_proposal"


@dataclass(frozen=True)
class AgentOutcome:
    proposal: Proposal
    attempts: tuple[Attempt, ...]
    invalid_output: bool  # True when the proposal is the invalid_llm_output fallback
    prompt_hash: str


def build_request(
    mode: Mode,
    target_field: Field,
    source_fields: Sequence[Field],
    retrieved: Sequence[RetrievedField],
    samples: Sequence[Mapping[str, JsonScalar]],
    *,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> LLMRequest:
    return LLMRequest(
        system=system_prompt(),
        parts=(build_user_prompt(mode, target_field, source_fields, retrieved, samples),),
        schema_name=SCHEMA_NAME,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )


def propose_field(
    llm: BaseLLMProvider,
    mode: Mode,
    target_field: Field,
    source_fields: Sequence[Field],
    retrieved: Sequence[RetrievedField],
    samples: Sequence[Mapping[str, JsonScalar]],
    *,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> AgentOutcome:
    request = build_request(
        mode,
        target_field,
        source_fields,
        retrieved,
        samples,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    result = llm.complete_structured(
        request,
        LLMProposal,
        validate=lambda reply: reply.to_proposal(target_field.path),
    )
    first_hash = result.attempts[0].metadata.prompt_hash
    if result.value is None:
        error = result.final_error or "invalid output"
        fallback = Proposal.unresolved(
            target_field.path, INVALID_OUTPUT_REASON, rationale=error[:300]
        )
        return AgentOutcome(fallback, result.attempts, invalid_output=True, prompt_hash=first_hash)
    return AgentOutcome(
        result.value.to_proposal(target_field.path),
        result.attempts,
        invalid_output=False,
        prompt_hash=first_hash,
    )
