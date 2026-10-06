"""Groq (OpenAI-compatible chat completions API) via httpx.

* Strict ``json_schema`` outputs only exist on a few models; every other model gets
  ``json_object`` mode with the schema spelled out in the system text.
* Retries happen on HTTP 429 only, honour ``Retry-After`` and stop at a total wall-clock budget.
* The API key is a SecretStr: it appears only in the Authorization header.
"""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, SecretStr

from app.llm.base import (
    BaseLLMProvider,
    CallMetadata,
    LLMError,
    LLMRequest,
    ModelUnavailableError,
    RateLimitExhausted,
    RawCompletion,
    strictify_schema,
    user_message,
)

BASE_URL = "https://api.groq.com/openai/v1"
STRICT_SCHEMA_MODELS = frozenset({"openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"})
ERROR_BODY_LIMIT = 300


def supports_strict_schema(model: str) -> bool:
    return model in STRICT_SCHEMA_MODELS


class GroqProvider(BaseLLMProvider):
    name = "groq"

    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        *,
        base_url: str = BASE_URL,
        timeout_s: float = 60.0,
        max_retries: int = 5,
        wall_clock_budget_s: float = 600.0,
        reasoning_effort: str | None = "low",
        default_max_output_tokens: int = 4000,
        on_rate_limit: Callable[[float, int], None] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._key = api_key
        self.model = model
        self._max_retries = max_retries
        self._budget = wall_clock_budget_s
        self._reasoning_effort = reasoning_effort
        self._default_max_tokens = default_max_output_tokens
        self._sleep = sleep
        self._on_rate_limit = on_rate_limit
        self._clock = clock
        self._available_checked = False
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport)

    def __repr__(self) -> str:
        return f"GroqProvider(model={self.model!r})"

    def _redact(self, text: str) -> str:
        secret = self._key.get_secret_value()
        return text.replace(secret, "[redacted]") if secret else text

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key.get_secret_value()}"}

    def check_model_available(self) -> list[str]:
        """Ask the API which models are active; fail with the list if ours is missing."""
        try:
            response = self._client.get("/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise LLMError(f"could not list Groq models: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise LLMError(f"listing Groq models failed with HTTP {response.status_code}")
        available = sorted(item["id"] for item in response.json().get("data", []))
        if self.model not in available:
            raise ModelUnavailableError(
                f"model {self.model!r} is not available on Groq; available: {', '.join(available)}"
            )
        self._available_checked = True
        return available

    def _body(self, request: LLMRequest, response_model: type[BaseModel]) -> dict[str, Any]:
        schema = response_model.model_json_schema()
        system = request.system
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": request.temperature,
            "max_completion_tokens": request.max_output_tokens or self._default_max_tokens,
        }
        if supports_strict_schema(self.model):
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": strictify_schema(schema),
                },
            }
        else:
            body["response_format"] = {"type": "json_object"}
            system += (
                "\n\nReply with a single JSON object that matches this JSON Schema, "
                "and nothing else:\n" + json.dumps(schema, sort_keys=True)
            )
        if self.model.startswith("openai/gpt-oss") and self._reasoning_effort:
            body["reasoning_effort"] = self._reasoning_effort
        body["messages"] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message(request.parts)},
        ]
        return body

    def _post_with_retries(self, body: dict[str, Any]) -> tuple[httpx.Response, int]:
        started = self._clock()
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.post(
                    "/chat/completions", json=body, headers=self._headers()
                )
            except httpx.TimeoutException as exc:
                raise LLMError(f"Groq request timed out ({type(exc).__name__})") from exc
            except httpx.HTTPError as exc:
                raise LLMError(f"Groq request failed ({type(exc).__name__})") from exc
            if response.status_code != 429:
                return response, attempt
            retry_after = _retry_after(response)
            waited = self._clock() - started
            if attempt > self._max_retries:
                raise RateLimitExhausted(
                    f"Groq rate limit persisted after {attempt} attempts", retry_after
                )
            if waited + retry_after > self._budget:
                raise RateLimitExhausted(
                    f"Groq asked to wait {retry_after:.0f}s, beyond the {self._budget:.0f}s "
                    "wall-clock budget (daily or long-window limit)",
                    retry_after,
                )
            if self._on_rate_limit is not None:
                self._on_rate_limit(retry_after, attempt)
            self._sleep(retry_after)

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        if not self._available_checked:
            self.check_model_available()
        started = self._clock()
        response, attempts = self._post_with_retries(self._body(request, response_model))
        latency_ms = int((self._clock() - started) * 1000)
        if response.status_code != 200:
            snippet = self._redact(response.text[:ERROR_BODY_LIMIT])
            raise LLMError(f"Groq returned HTTP {response.status_code}: {snippet}")
        payload = response.json()
        usage = payload.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        text = payload["choices"][0]["message"].get("content") or ""
        metadata = CallMetadata(
            provider=self.name,
            model=str(payload.get("model", self.model)),
            prompt_hash=request.fingerprint(response_model),
            latency_ms=latency_ms,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            reasoning_tokens=details.get("reasoning_tokens"),
            http_attempts=attempts,
            rate_limits={
                k: v for k, v in response.headers.items() if k.lower().startswith("x-ratelimit")
            },
        )
        return RawCompletion(text=text, metadata=metadata)


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw is not None else 1.0
    except ValueError:
        return 1.0
