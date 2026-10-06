"""Real embeddings via fastembed (ONNX runtime, no torch). The model downloads on first use."""

from collections.abc import Sequence
from typing import Any

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class FastEmbedProvider:
    def __init__(self, model_name: str = DEFAULT_MODEL, cache_dir: str | None = None) -> None:
        # Imported here so installs without the optional `embeddings` group still work.
        from fastembed import TextEmbedding

        self.model_name = model_name
        self._model: Any = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        self.dimensions = int(self._model.embedding_size)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(x) for x in vector] for vector in self._model.embed(list(texts))]
