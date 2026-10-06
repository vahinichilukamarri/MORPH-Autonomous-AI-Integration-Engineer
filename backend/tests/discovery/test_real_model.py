"""Needs the real embedding model (downloaded on first run): `./scripts/dev.ps1 test-slow`."""

import math
import os
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import EMBEDDING_DIMENSIONS, EntityRow, FieldRow
from app.discovery.parser import parse_spec
from app.discovery.repository import ingest
from app.discovery.source import load_spec
from app.embeddings.fastembed_provider import DEFAULT_MODEL, FastEmbedProvider
from app.embeddings.retrieval import similar_fields
from app.embeddings.service import embed_version

pytestmark = pytest.mark.slow

OPENAPI_DIR = Path(__file__).resolve().parents[3] / "mock_systems" / "openapi"


@pytest.fixture(scope="module")
def provider() -> FastEmbedProvider:
    return FastEmbedProvider(cache_dir=os.environ.get("EMBEDDING_CACHE_DIR"))


def test_real_provider_shape_and_determinism(provider: FastEmbedProvider) -> None:
    assert provider.model_name == DEFAULT_MODEL
    assert provider.dimensions == EMBEDDING_DIMENSIONS == 384
    first, second = provider.embed(["Customer.email"]), provider.embed(["Customer.email"])
    assert first == second
    assert math.isclose(math.sqrt(sum(v * v for v in first[0])), 1.0, rel_tol=1e-3)


def test_email_finds_email_address_in_the_other_system(
    session: Session, provider: FastEmbedProvider
) -> None:
    versions = {}
    for system, file in (("crm", "crm.v1"), ("support", "support.v1")):
        raw = load_spec(OPENAPI_DIR / f"{file}.json")
        versions[system] = ingest(session, parse_spec(raw, system), raw)
        embed_version(session, versions[system].version_id, provider)
    email = session.scalar(
        select(FieldRow.id)
        .join(EntityRow, EntityRow.id == FieldRow.entity_id)
        .where(
            EntityRow.system_version_id == versions["crm"].version_id,
            EntityRow.name == "Customer",
            FieldRow.path == "email",
        )
    )
    assert email is not None
    top = similar_fields(
        session,
        email,
        model_name=provider.model_name,
        k=1,
        target_system_id=versions["support"].system_id,
        target_entity="User",
    )
    assert [r.field_path for r in top] == ["email_address"]
