"""Deterministic hash-based embeddings. For tests only: they carry no real semantics."""

import hashlib
import math
import re
from collections.abc import Sequence

from app.db_models import EMBEDDING_DIMENSIONS

FAKE_MODEL_NAME = "fake-hash-384"
_TOKEN = re.compile(r"[a-z0-9]+")


def _token_vector(token: str) -> list[float]:
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    stream = b""
    counter = 0
    while len(stream) < EMBEDDING_DIMENSIONS:
        stream += hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
        counter += 1
    return [1.0 if byte & 1 else -1.0 for byte in stream[:EMBEDDING_DIMENSIONS]]


class FakeEmbeddingProvider:
    """A bag-of-tokens hash embedding: texts sharing tokens get a higher cosine similarity."""

    model_name = FAKE_MODEL_NAME
    dimensions = EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    @staticmethod
    def _embed_one(text: str) -> list[float]:
        total = [0.0] * EMBEDDING_DIMENSIONS
        for token in _TOKEN.findall(text.lower()):
            for index, value in enumerate(_token_vector(token)):
                total[index] += value
        norm = math.sqrt(sum(v * v for v in total)) or 1.0
        return [v / norm for v in total]
