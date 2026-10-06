"""The single seam for turning text into vectors. Providers are chosen from settings."""

from collections.abc import Sequence
from typing import Protocol

from app.settings import Settings


class EmbeddingProvider(Protocol):
    """Maps texts to unit-length vectors. ``model_name`` is stored with every embedding."""

    model_name: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def create_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "fake":
        from app.embeddings.fake import FakeEmbeddingProvider

        return FakeEmbeddingProvider()
    from app.embeddings.fastembed_provider import FastEmbedProvider

    return FastEmbedProvider(settings.embedding_model, cache_dir=settings.embedding_cache_dir)


def configured_model_name(settings: Settings) -> str:
    """The model name the configured provider will use, without loading the model."""
    if settings.embedding_provider == "fake":
        from app.embeddings.fake import FAKE_MODEL_NAME

        return FAKE_MODEL_NAME
    return settings.embedding_model
