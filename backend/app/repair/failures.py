"""Which failures are the model's and which are infrastructure's. Pure.

A model failure uses an attempt and produces feedback: a reply cut off by the output limit, an empty
reply, a reply that is not valid JSON, and every gate, guard, test or smoke failure. An
infrastructure failure never uses an attempt and produces no feedback: it pauses the run (the
caller counts the pauses and stops a unit after five). Truncation is a model failure on purpose: a
model that writes more than it is allowed to must be told so.
"""

from app.codegen.gate import GateResult, Rule
from app.codegen.sandbox import Outcome as SandboxOutcome
from app.codegen.sandbox import SandboxError
from app.llm.base import Attempt, LLMError, ReplayMissError
from app.repair.feedback import FeedbackItem, Stage, validation_items

TRUNCATED = "TRUNCATED"
EMPTY_OUTPUT = "EMPTY_OUTPUT"
LENGTH = "length"  # the provider's finish reason for a reply cut off at the output limit


def attempt_items(attempt: Attempt) -> list[FeedbackItem]:
    """Feedback for a model reply that did not parse or validate; empty when it did."""
    if attempt.error is None:
        return []
    if attempt.metadata.finish_reason == LENGTH:
        message = "the reply was cut off at the output limit; write a shorter reply"
        return [FeedbackItem(Stage.PROPOSAL, TRUNCATED, message)]
    if not attempt.raw_text.strip():
        return [FeedbackItem(Stage.PROPOSAL, EMPTY_OUTPUT, "the reply was empty")]
    return validation_items(attempt.error)


def is_infrastructure_error(error: BaseException) -> bool:
    """A provider or sandbox failure that is not the model's fault.

    A missing replay is a hard error and never an infrastructure pause: a fixed-start unit must not
    quietly fall back to anything else.
    """
    if isinstance(error, ReplayMissError):
        return False
    return isinstance(error, LLMError | SandboxError)


def is_infrastructure_outcome(outcome: str) -> bool:
    """A sandbox run that never started."""
    return outcome == SandboxOutcome.START_FAILED.value


def has_infrastructure_failure(result: GateResult) -> bool:
    """A tool stage (ruff, mypy) whose container did not start, not one that the code broke."""
    return any(
        finding.rule is Rule.TOOL_FAILURE and SandboxOutcome.START_FAILED.value in finding.message
        for finding in result.findings
    )
