"""A scripted provider for tests: replies come from a queue or a function, no network."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.llm.base import BaseLLMProvider, CallMetadata, LLMRequest, RawCompletion

Responder = Callable[[LLMRequest], str]


@dataclass
class ScriptedFakeProvider(BaseLLMProvider):
    """Returns queued replies in order, or ``responder(request)`` when no queue is given."""

    replies: Sequence[str] = ()
    responder: Responder | None = None
    model: str = "scripted-fake"
    calls: list[LLMRequest] = field(default_factory=list)
    name: str = "scripted"
    _next: int = 0

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        self.calls.append(request)
        if self.responder is not None:
            text = self.responder(request)
        else:
            if self._next >= len(self.replies):
                raise AssertionError("ScriptedFakeProvider ran out of scripted replies")
            text = self.replies[self._next]
            self._next += 1
        metadata = CallMetadata(
            provider=self.name,
            model=self.model,
            prompt_hash=request.fingerprint(response_model),
            latency_ms=1,
            input_tokens=len(request.system) // 4 + sum(len(p) for p in request.parts) // 4,
            output_tokens=len(text) // 4,
            source="scripted",
        )
        return RawCompletion(text=text, metadata=metadata)
