"""Recorded LLM responses on disk (JSON lines) and the providers that read and write them.

* ``ReplayLLMProvider``: serves recorded responses by prompt hash and nothing else. A prompt that
  was not recorded fails loudly. This is what CI uses.
* ``CachingProvider``: wraps a real provider with an on-disk cache keyed by the same hash, and
  can append every real response to a record file (committed replay fixtures).

Records hold the raw reply and token counts, never prompts, headers or keys.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.llm.base import (
    BaseLLMProvider,
    CallMetadata,
    LLMRequest,
    RawCompletion,
    ReplayMissError,
)


@dataclass(frozen=True)
class Recorded:
    prompt_hash: str
    text: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    latency_ms: int
    total_tokens: int | None = None
    usage: dict[str, Any] | None = None


class ResponseStore:
    """Append-only JSONL files in a directory (or one file). The first record per hash wins."""

    def __init__(self, location: Path) -> None:
        self.location = location
        self._by_hash: dict[str, Recorded] = {}
        for path in self._files():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = Recorded(**json.loads(line))
                    self._by_hash.setdefault(record.prompt_hash, record)

    def _files(self) -> list[Path]:
        if self.location.is_file():
            return [self.location]
        return sorted(self.location.glob("*.jsonl")) if self.location.is_dir() else []

    def get(self, prompt_hash: str) -> Recorded | None:
        return self._by_hash.get(prompt_hash)

    def __len__(self) -> int:
        return len(self._by_hash)

    def put(self, record: Recorded, *, file: Path | None = None) -> None:
        target = file or (self.location if self.location.suffix == ".jsonl" else None)
        if target is None:
            target = self.location / "responses.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n")
        self._by_hash.setdefault(record.prompt_hash, record)


def _completion(record: Recorded, source: str) -> RawCompletion:
    metadata = CallMetadata(
        provider=record.provider,
        model=record.model,
        prompt_hash=record.prompt_hash,
        latency_ms=record.latency_ms,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        reasoning_tokens=record.reasoning_tokens,
        total_tokens=record.total_tokens,
        usage=record.usage,
        source=source,  # type: ignore[arg-type]
    )
    return RawCompletion(text=record.text, metadata=metadata)


class ReplayLLMProvider(BaseLLMProvider):
    name = "replay"

    def __init__(self, store: ResponseStore) -> None:
        self._store = store

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        key = request.fingerprint(response_model)
        record = self._store.get(key)
        if record is None:
            raise ReplayMissError(
                f"no recorded response for prompt {key[:12]}... in {self._store.location}. "
                "The prompt, schema or retrieval context changed: re-record the replay fixtures "
                "with a real provider (see docs/milestones.md, 'Re-record replays')."
            )
        return _completion(record, "replay")


class CachingProvider(BaseLLMProvider):
    """On-disk cache in front of a provider; ``read=False`` skips reads but still writes."""

    def __init__(
        self,
        inner: BaseLLMProvider,
        store: ResponseStore,
        *,
        read: bool = True,
        record_file: Path | None = None,
    ) -> None:
        self._inner = inner
        self._store = store
        self._read = read
        self._record_file = record_file
        self.name = inner.name

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        key = request.fingerprint(response_model)
        if self._read:
            hit = self._store.get(key)
            if hit is not None:
                return _completion(hit, "cache")
        raw = self._inner.complete_raw(request, response_model)
        meta = raw.metadata
        self._store.put(
            Recorded(
                prompt_hash=key,
                text=raw.text,
                provider=meta.provider,
                model=meta.model,
                input_tokens=meta.input_tokens,
                output_tokens=meta.output_tokens,
                reasoning_tokens=meta.reasoning_tokens,
                latency_ms=meta.latency_ms,
                total_tokens=meta.total_tokens,
                usage=meta.usage,
            ),
            file=self._record_file,
        )
        return raw
