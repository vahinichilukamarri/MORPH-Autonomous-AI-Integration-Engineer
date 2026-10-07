import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from app.llm.base import (
    LLMError,
    LLMRequest,
    ModelUnavailableError,
    Outcome,
    RateLimitExhausted,
    ReplayMissError,
    strictify_schema,
)
from app.llm.fake import ScriptedFakeProvider
from app.llm.groq import GroqProvider, supports_strict_schema
from app.llm.ollama import OllamaProvider
from app.llm.store import CachingProvider, Recorded, ReplayLLMProvider, ResponseStore

SENTINEL = "gsk_SENTINEL_never_log_me_0123456789"


class Answer(BaseModel):
    value: int
    label: str


REQUEST = LLMRequest(system="sys", parts=("question",), schema_name="answer")
GOOD = '{"value": 7, "label": "seven"}'


# ---- request fingerprint -----------------------------------------------------------------------


def test_fingerprint_is_stable_and_sensitive() -> None:
    base = REQUEST.fingerprint(Answer)
    assert base == LLMRequest("sys", ("question",), "answer").fingerprint(Answer)
    assert base != LLMRequest("sys", ("question!",), "answer").fingerprint(Answer)
    assert base != LLMRequest("sys2", ("question",), "answer").fingerprint(Answer)
    assert base != LLMRequest("sys", ("question",), "answer", temperature=0.5).fingerprint(Answer)

    class Other(BaseModel):
        value: int

    assert base != REQUEST.fingerprint(Other)


def test_strictify_schema_requires_everything_and_closes_objects() -> None:
    class Inner(BaseModel):
        a: int | None = None

    class Outer(BaseModel):
        inner: Inner
        tags: list[str] = []

    strict = strictify_schema(Outer.model_json_schema())
    assert strict["additionalProperties"] is False
    assert strict["required"] == ["inner", "tags"]
    assert strict["$defs"]["Inner"]["additionalProperties"] is False
    assert strict["$defs"]["Inner"]["required"] == ["a"]


# ---- Groq via a mock transport -----------------------------------------------------------------


def models_response(*ids: str) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": i} for i in ids]})


def chat_response(text: str, model: str = "openai/gpt-oss-120b", **usage: Any) -> httpx.Response:
    usage = usage or {"prompt_tokens": 100, "completion_tokens": 20}
    return httpx.Response(
        200,
        json={"model": model, "choices": [{"message": {"content": text}}], "usage": usage},
        headers={"x-ratelimit-remaining-tokens": "5000", "content-type": "application/json"},
    )


class Recorder:
    def __init__(
        self, chat: Callable[[int, httpx.Request], httpx.Response], models: tuple[str, ...]
    ):
        self.chat = chat
        self.models = models
        self.chat_calls: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.now = 0.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return models_response(*self.models)
        self.chat_calls.append(request)
        return self.chat(len(self.chat_calls), request)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def provider(self, model: str = "openai/gpt-oss-120b", **kwargs: Any) -> GroqProvider:
        return GroqProvider(
            SecretStr(SENTINEL),
            model,
            transport=httpx.MockTransport(self.handler),
            sleep=self.sleep,
            clock=self.clock,
            **kwargs,
        )


def always_ok(text: str = GOOD) -> Callable[[int, httpx.Request], httpx.Response]:
    return lambda n, r: chat_response(text)


def test_groq_success_records_exact_model_tokens_and_limits() -> None:
    rec = Recorder(
        lambda n, r: chat_response(
            GOOD,
            model="openai/gpt-oss-120b-2026",
            prompt_tokens=120,
            completion_tokens=40,
            completion_tokens_details={"reasoning_tokens": 25},
        ),
        ("openai/gpt-oss-120b",),
    )
    result = rec.provider().complete_structured(REQUEST, Answer)
    assert result.value == Answer(value=7, label="seven")
    meta = result.attempts[0].metadata
    assert meta.model == "openai/gpt-oss-120b-2026", "the model the API reports, not the request"
    assert (meta.input_tokens, meta.output_tokens, meta.reasoning_tokens) == (120, 40, 25)
    assert meta.outcome is Outcome.OK and meta.source == "network"
    assert meta.rate_limits == {"x-ratelimit-remaining-tokens": "5000"}
    assert meta.prompt_hash == REQUEST.fingerprint(Answer)


def test_groq_uses_strict_schema_and_low_reasoning_for_gpt_oss() -> None:
    rec = Recorder(always_ok(), ("openai/gpt-oss-120b",))
    rec.provider().complete_structured(REQUEST, Answer)
    body = json.loads(rec.chat_calls[0].content)
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert body["reasoning_effort"] == "low"
    assert body["temperature"] == 0.0
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_groq_uses_json_object_mode_for_other_models() -> None:
    assert supports_strict_schema("openai/gpt-oss-20b")
    assert not supports_strict_schema("llama-3.3-70b-versatile")
    rec = Recorder(always_ok(), ("llama-3.3-70b-versatile",))
    rec.provider("llama-3.3-70b-versatile").complete_structured(REQUEST, Answer)
    body = json.loads(rec.chat_calls[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert "reasoning_effort" not in body
    assert '"value"' in body["messages"][0]["content"], "schema is spelled out for json_object"


def test_groq_checks_the_model_is_available_once() -> None:
    rec = Recorder(always_ok(), ("llama-3.1-8b-instant",))
    provider = rec.provider("openai/gpt-oss-120b")
    with pytest.raises(ModelUnavailableError, match="llama-3.1-8b-instant"):
        provider.complete_structured(REQUEST, Answer)
    assert rec.chat_calls == []


def test_groq_retries_429_honouring_retry_after() -> None:
    def chat(n: int, _: httpx.Request) -> httpx.Response:
        if n < 3:
            return httpx.Response(429, headers={"retry-after": "7"}, text="slow down")
        return chat_response(GOOD)

    rec = Recorder(chat, ("openai/gpt-oss-120b",))
    events: list[tuple[float, int]] = []
    result = rec.provider(
        on_rate_limit=lambda wait, n: events.append((wait, n))
    ).complete_structured(REQUEST, Answer)
    assert result.value is not None
    assert events == [(7.0, 1), (7.0, 2)], "each honoured wait is reported"
    assert rec.sleeps == [7.0, 7.0]
    assert len(rec.chat_calls) == 3
    assert result.attempts[0].metadata.http_attempts == 3


def test_groq_retry_count_is_bounded() -> None:
    rec = Recorder(
        lambda n, r: httpx.Response(429, headers={"retry-after": "1"}), ("openai/gpt-oss-120b",)
    )
    with pytest.raises(RateLimitExhausted):
        rec.provider(max_retries=3).complete_structured(REQUEST, Answer)
    assert len(rec.chat_calls) == 4, "first try plus three retries"
    assert rec.sleeps == [1.0, 1.0, 1.0]


def test_groq_wall_clock_budget_stops_long_waits_without_sleeping() -> None:
    rec = Recorder(
        lambda n, r: httpx.Response(429, headers={"retry-after": "7200"}), ("openai/gpt-oss-120b",)
    )
    with pytest.raises(RateLimitExhausted) as info:
        rec.provider(wall_clock_budget_s=600).complete_structured(REQUEST, Answer)
    assert info.value.retry_after_s == 7200
    assert rec.sleeps == []
    assert len(rec.chat_calls) == 1


def test_groq_does_not_retry_other_errors() -> None:
    rec = Recorder(lambda n, r: httpx.Response(500, text="boom"), ("openai/gpt-oss-120b",))
    with pytest.raises(LLMError, match="HTTP 500"):
        rec.provider().complete_structured(REQUEST, Answer)
    assert len(rec.chat_calls) == 1 and rec.sleeps == []


def test_groq_timeout_is_a_typed_error_not_a_retry() -> None:
    def chat(n: int, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    rec = Recorder(chat, ("openai/gpt-oss-120b",))
    with pytest.raises(LLMError, match="timed out"):
        rec.provider().complete_structured(REQUEST, Answer)
    assert len(rec.chat_calls) == 1


# ---- structured parsing and the single re-ask --------------------------------------------------


def test_invalid_output_is_reasked_exactly_once_with_the_error() -> None:
    provider = ScriptedFakeProvider(replies=["not json at all", GOOD])
    result = provider.complete_structured(REQUEST, Answer)
    assert result.value == Answer(value=7, label="seven")
    assert result.reasked and len(provider.calls) == 2
    assert result.attempts[0].metadata.outcome is Outcome.INVALID_OUTPUT
    assert result.attempts[1].metadata.outcome is Outcome.OK
    reask = provider.calls[1].parts[-1]
    assert "Validation error:" in reask and "not json at all" in reask


def test_schema_errors_name_the_field_in_the_reask() -> None:
    provider = ScriptedFakeProvider(replies=['{"value": "x", "label": "a"}', GOOD])
    provider.complete_structured(REQUEST, Answer)
    assert "value:" in provider.calls[1].parts[-1]


def test_two_invalid_outputs_give_a_recorded_failure_not_a_crash() -> None:
    provider = ScriptedFakeProvider(replies=["nope", '{"value": 1}'])
    result = provider.complete_structured(REQUEST, Answer)
    assert result.value is None
    assert result.outcome is Outcome.INVALID_OUTPUT
    assert len(result.attempts) == 2 and len(provider.calls) == 2, "never a third call"
    assert result.final_error is not None and "label" in result.final_error


def test_semantic_validator_errors_trigger_the_reask() -> None:
    def must_be_even(answer: Answer) -> None:
        if answer.value % 2:
            raise ValueError("value must be even")

    provider = ScriptedFakeProvider(replies=[GOOD, '{"value": 8, "label": "eight"}'])
    result = provider.complete_structured(REQUEST, Answer, validate=must_be_even)
    assert result.value == Answer(value=8, label="eight")
    assert "value must be even" in provider.calls[1].parts[-1]


# ---- record, cache and replay ------------------------------------------------------------------


def test_replay_serves_recorded_responses_by_prompt_hash(tmp_path: Path) -> None:
    store = ResponseStore(tmp_path)
    store.put(
        Recorded(REQUEST.fingerprint(Answer), GOOD, "groq", "m", 10, 5, 2, 123),
        file=tmp_path / "r.jsonl",
    )
    replay = ReplayLLMProvider(ResponseStore(tmp_path))
    result = replay.complete_structured(REQUEST, Answer)
    assert result.value == Answer(value=7, label="seven")
    meta = result.attempts[0].metadata
    assert (meta.source, meta.model, meta.input_tokens, meta.reasoning_tokens) == (
        "replay",
        "m",
        10,
        2,
    )


def test_replay_miss_fails_loudly_with_a_rerecord_message(tmp_path: Path) -> None:
    replay = ReplayLLMProvider(ResponseStore(tmp_path))
    with pytest.raises(ReplayMissError, match="re-record"):
        replay.complete_structured(REQUEST, Answer)


def test_caching_provider_hits_misses_and_bypasses(tmp_path: Path) -> None:
    inner = ScriptedFakeProvider(responder=lambda r: GOOD)
    cache = CachingProvider(inner, ResponseStore(tmp_path / "cache"))
    first = cache.complete_structured(REQUEST, Answer)
    second = cache.complete_structured(REQUEST, Answer)
    assert len(inner.calls) == 1
    assert first.attempts[0].metadata.source == "scripted"
    assert second.attempts[0].metadata.source == "cache"

    reload_cache = CachingProvider(inner, ResponseStore(tmp_path / "cache"))
    reload_cache.complete_structured(REQUEST, Answer)
    assert len(inner.calls) == 1, "the cache survives a restart"

    bypass = CachingProvider(inner, ResponseStore(tmp_path / "cache"), read=False)
    bypass.complete_structured(REQUEST, Answer)
    assert len(inner.calls) == 2, "bypass always calls the provider"


def test_record_file_receives_every_real_response(tmp_path: Path) -> None:
    inner = ScriptedFakeProvider(responder=lambda r: GOOD)
    record = tmp_path / "replays" / "s1.jsonl"
    CachingProvider(
        inner, ResponseStore(tmp_path / "cache"), record_file=record
    ).complete_structured(REQUEST, Answer)
    lines = record.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["prompt_hash"] == REQUEST.fingerprint(Answer)
    replay = ReplayLLMProvider(ResponseStore(record))
    assert replay.complete_structured(REQUEST, Answer).value is not None


# ---- Ollama (mock transport only; not verified against a real install) -------------------------


def test_ollama_provider_against_a_mock_transport() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "llama3.1:8b",
                "message": {"content": GOOD},
                "prompt_eval_count": 50,
                "eval_count": 9,
            },
        )

    provider = OllamaProvider(
        "http://ollama.test", "llama3.1:8b", transport=httpx.MockTransport(handler)
    )
    result = provider.complete_structured(REQUEST, Answer)
    assert result.value is not None
    assert seen[0]["format"]["title"] == "Answer"
    assert seen[0]["options"]["temperature"] == 0.0 and seen[0]["stream"] is False
    meta = result.attempts[0].metadata
    assert (meta.provider, meta.input_tokens, meta.output_tokens) == ("ollama", 50, 9)


def test_ollama_http_errors_are_typed() -> None:
    provider = OllamaProvider(
        "http://ollama.test", "m", transport=httpx.MockTransport(lambda r: httpx.Response(404))
    )
    with pytest.raises(LLMError, match="HTTP 404"):
        provider.complete_structured(REQUEST, Answer)


# ---- secrets -----------------------------------------------------------------------------------


def test_the_api_key_never_leaks(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    caplog.set_level(logging.DEBUG)
    statuses = iter([429, 429, 401, 200])

    def chat(n: int, request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        if status == 200:
            return chat_response("garbage", model="openai/gpt-oss-120b")
        # a hostile server that echoes the key back in its error body
        return httpx.Response(
            status, headers={"retry-after": "1"}, text=f"bad key {SENTINEL} rejected"
        )

    rec = Recorder(chat, ("openai/gpt-oss-120b",))
    provider = rec.provider()
    raised: list[str] = []
    try:
        provider.complete_structured(REQUEST, Answer)
    except LLMError as exc:
        raised.append(str(exc))
    assert raised and "[redacted]" in raised[0]

    rec2 = Recorder(always_ok(), ("openai/gpt-oss-120b",))
    store = ResponseStore(tmp_path)
    result = CachingProvider(
        rec2.provider(), store, record_file=tmp_path / "rec.jsonl"
    ).complete_structured(REQUEST, Answer)

    haystacks = [
        caplog.text,
        repr(provider),
        str(provider.__dict__.get("_key")),
        raised[0],
        (tmp_path / "rec.jsonl").read_text(encoding="utf-8"),
        repr(result),
    ]
    assert all(SENTINEL not in text for text in haystacks)
    # the key is only ever sent as the Authorization header
    assert rec2.chat_calls[0].headers["authorization"] == f"Bearer {SENTINEL}"
    body = rec2.chat_calls[0].content.decode()
    assert SENTINEL not in body


def _walk_any_of(node: Any) -> list[list[Any]]:
    found: list[list[Any]] = []
    if isinstance(node, dict):
        if isinstance(node.get("anyOf"), list):
            found.append(node["anyOf"])
        for value in node.values():
            found.extend(_walk_any_of(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_any_of(item))
    return found


def test_strictify_collapses_integer_and_number_to_number() -> None:
    schema = {
        "anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "null"}]
    }
    assert strictify_schema(schema) == {
        "anyOf": [{"type": "string"}, {"type": "number"}, {"type": "null"}]
    }
    only_integer = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert strictify_schema(only_integer) == only_integer, "integer alone is left alone"
    nested = {"properties": {"v": {"anyOf": [{"type": "integer"}, {"type": "number"}]}}}
    assert strictify_schema(nested)["properties"]["v"] == {"anyOf": [{"type": "number"}]}


def test_the_real_proposal_schema_never_mixes_integer_and_number() -> None:
    from app.mapping.proposal import LLMProposal

    original = LLMProposal.model_json_schema()
    assert any(
        {"integer", "number"} <= {b.get("type") for b in branches if isinstance(b, dict)}
        for branches in _walk_any_of(original)
    ), "the unmodified schema does have the ambiguous union, so this test is meaningful"
    strict = strictify_schema(original)
    for branches in _walk_any_of(strict):
        types = [b.get("type") for b in branches if isinstance(b, dict)]
        assert not ("integer" in types and "number" in types), branches


def test_local_validation_still_keeps_integers_as_integers() -> None:
    from app.mapping.proposal import EnumPair

    pair = EnumPair.model_validate({"source": "A", "target": 5})
    assert pair.target == 5 and isinstance(pair.target, int) and not isinstance(pair.target, bool)
