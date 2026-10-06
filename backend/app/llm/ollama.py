"""Ollama local fallback.

Implemented and unit-tested against a mock transport only; not verified against a real Ollama
install (none is available in the development environment).
"""

import time

import httpx
from pydantic import BaseModel

from app.llm.base import (
    BaseLLMProvider,
    CallMetadata,
    LLMError,
    LLMRequest,
    RawCompletion,
    user_message,
)


class OllamaProvider(BaseLLMProvider):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_s: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport)

    def __repr__(self) -> str:
        return f"OllamaProvider(model={self.model!r})"

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        body = {
            "model": self.model,
            "stream": False,
            "format": response_model.model_json_schema(),
            "options": {"temperature": request.temperature},
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": user_message(request.parts)},
            ],
        }
        started = time.monotonic()
        try:
            response = self._client.post("/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request failed ({type(exc).__name__})") from exc
        if response.status_code != 200:
            raise LLMError(f"Ollama returned HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        metadata = CallMetadata(
            provider=self.name,
            model=str(payload.get("model", self.model)),
            prompt_hash=request.fingerprint(response_model),
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=payload.get("prompt_eval_count"),
            output_tokens=payload.get("eval_count"),
        )
        return RawCompletion(text=payload.get("message", {}).get("content", ""), metadata=metadata)
