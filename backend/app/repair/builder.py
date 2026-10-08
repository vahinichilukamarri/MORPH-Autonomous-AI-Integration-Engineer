"""How prompts are made. The repair loop only knows this protocol.

M2 ships no prompt: tests use a fake builder. The real repair prompts are written, frozen and
hashed in M3.
"""

from typing import Protocol

from app.codegen.inputs import CodegenInput
from app.codegen.review_gate import GateDecision
from app.llm.base import LLMRequest
from app.repair.feedback import Feedback


class PromptBuilder(Protocol):
    def initial(self, inp: CodegenInput, decision: GateDecision, condition: str) -> LLMRequest:
        """The attempt-0 request (the v0.4 prompt for the condition)."""
        ...

    def repair(
        self,
        inp: CodegenInput,
        decision: GateDecision,
        condition: str,
        *,
        attempt: int,
        previous_output: str,
        feedback: Feedback,
    ) -> LLMRequest:
        """The request for repair ``attempt`` (1 to 3): the previous output and the feedback."""
        ...
