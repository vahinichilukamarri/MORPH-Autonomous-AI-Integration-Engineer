"""The single LLM seam: request/response types, structured parsing and the one re-ask.

Every LLM call in MORPH goes through a provider built on BaseLLMProvider. Providers only
return raw text plus call metadata; parsing into a Pydantic model, the single re-ask and the
"invalid output is a recorded failure, never a crash" rule live here, once.
"""

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

logger = logging.getLogger("morph.llm")

Source = Literal["network", "cache", "replay", "scripted"]
REASK_ERROR_LIMIT = 1500
REASK_PREVIOUS_LIMIT = 2000


class Outcome(StrEnum):
    OK = "OK"
    INVALID_OUTPUT = "INVALID_OUTPUT"


class LLMError(Exception):
    """A provider could not produce a completion (transport, HTTP or availability problem)."""


class RateLimitExhausted(LLMError):
    """HTTP 429 persisted past the retry bound or the wall-clock budget (e.g. a daily limit)."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ModelUnavailableError(LLMError):
    pass


class ReplayMissError(LLMError):
    pass


@dataclass(frozen=True)
class LLMRequest:
    """A full prompt. Spec text and data belong in parts, never in the system text."""

    system: str
    parts: tuple[str, ...]
    schema_name: str
    temperature: float = 0.0
    max_output_tokens: int | None = None

    def with_part(self, part: str) -> "LLMRequest":
        return LLMRequest(
            self.system, (*self.parts, part), self.schema_name, self.temperature,
            self.max_output_tokens,
        )  # fmt: skip

    def fingerprint(self, response_model: type[BaseModel]) -> str:
        """SHA-256 of the full prompt, response schema and temperature."""
        canonical = json.dumps(
            {
                "system": self.system,
                "parts": list(self.parts),
                "schema_name": self.schema_name,
                "schema": response_model.model_json_schema(),
                "temperature": self.temperature,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CallMetadata:
    provider: str
    model: str
    prompt_hash: str
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    usage: dict[str, Any] | None = None  # the provider's own usage block, unmodified
    finish_reason: str | None = None  # the provider's own value, e.g. "stop" or "length"
    outcome: Outcome = Outcome.OK
    source: Source = "network"
    http_attempts: int = 1
    rate_limits: dict[str, str] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class RawCompletion:
    text: str
    metadata: CallMetadata


@dataclass(frozen=True)
class Attempt:
    """One provider call within a structured call, with how its output fared."""

    metadata: CallMetadata
    raw_text: str
    error: str | None  # None when the output parsed and validated


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    value: T | None  # None when both attempts were invalid
    attempts: tuple[Attempt, ...]

    @property
    def reasked(self) -> bool:
        return len(self.attempts) > 1

    @property
    def outcome(self) -> Outcome:
        return Outcome.OK if self.value is not None else Outcome.INVALID_OUTPUT

    @property
    def final_error(self) -> str | None:
        return None if self.value is not None else self.attempts[-1].error


def _format_validation_error(exc: ValidationError | ValueError) -> str:
    if isinstance(exc, ValidationError):
        lines = [
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        ]
        text = "; ".join(lines)
    else:
        text = str(exc)
    return text[:REASK_ERROR_LIMIT]


class BaseLLMProvider(ABC):
    """Subclasses return raw text; this class parses, validates and re-asks once."""

    name: str

    @abstractmethod
    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        """One completion call. Raises LLMError subclasses on failure; never parses."""

    def complete_structured[T: BaseModel](
        self,
        request: LLMRequest,
        response_model: type[T],
        *,
        validate: Callable[[T], object] | None = None,
        reask: bool = True,
    ) -> StructuredResult[T]:
        """Parse the reply into ``response_model``; on invalid output re-ask exactly once.

        ``reask=False`` makes the call a single provider call: an invalid reply is returned as a
        failed result and the caller decides what happens next (the repair loop does).

        ``validate`` may raise ValueError for semantic problems the schema cannot express; its
        message is fed back in the re-ask like a schema error.
        """
        attempts: list[Attempt] = []
        current = request
        for attempt_number in (1, 2) if reask else (1,):
            raw = self.complete_raw(current, response_model)
            error: str | None = None
            value: T | None = None
            try:
                value = response_model.model_validate_json(raw.text)
                if validate is not None:
                    validate(value)
            except (ValidationError, ValueError) as exc:
                error = _format_validation_error(exc)
                value = None
            metadata = raw.metadata
            if error is not None:
                metadata = CallMetadata(
                    **{**metadata.__dict__, "outcome": Outcome.INVALID_OUTPUT, "error": error}
                )
            attempts.append(Attempt(metadata=metadata, raw_text=raw.text, error=error))
            if value is not None:
                return StructuredResult(value=value, attempts=tuple(attempts))
            logger.info("invalid LLM output on attempt %d: %s", attempt_number, error)
            current = request.with_part(
                "Your previous reply was rejected.\n"
                f"Previous reply (truncated):\n{raw.text[:REASK_PREVIOUS_LIMIT]}\n"
                f"Validation error: {error}\n"
                "Reply again with one corrected JSON object only."
            )
        return StructuredResult(value=None, attempts=tuple(attempts))


def user_message(parts: Sequence[str]) -> str:
    return "\n\n".join(parts)


def strictify_schema(schema: Any) -> Any:
    """Make a Pydantic JSON schema acceptable to strict structured-output mode.

    Every object gets ``additionalProperties: false`` and lists all its properties as required.
    An ``anyOf`` with both ``integer`` and ``number`` keeps only ``number`` (JSON numbers cover
    integers, and the provider rejects the pair as ambiguous). Only the schema sent to the
    provider changes; replies are still validated against the original model, where integers
    stay integers.
    """
    if isinstance(schema, dict):
        out = {key: strictify_schema(value) for key, value in schema.items()}
        branches = out.get("anyOf")
        if isinstance(branches, list):
            plain = [b.get("type") for b in branches if isinstance(b, dict) and len(b) == 1]
            if "integer" in plain and "number" in plain:
                out["anyOf"] = [b for b in branches if b != {"type": "integer"}]
        if out.get("type") == "object" or "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out.get("properties", {}))
        return out
    if isinstance(schema, list):
        return [strictify_schema(item) for item in schema]
    return schema
