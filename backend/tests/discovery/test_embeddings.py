import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db_models import EMBEDDING_DIMENSIONS, EntityEmbedding, EntityRow, FieldEmbedding, FieldRow
from app.discovery.models import EntityRole
from app.discovery.parser import parse_spec
from app.discovery.repository import IngestResult, ingest
from app.discovery.source import load_spec
from app.embeddings.fake import FAKE_MODEL_NAME, FakeEmbeddingProvider
from app.embeddings.retrieval import SimilarField, similar_fields
from app.embeddings.service import embed_version
from app.embeddings.text import entity_text, field_text

OPENAPI_DIR = Path(__file__).resolve().parents[3] / "mock_systems" / "openapi"


def ingest_file(session: Session, system: str, file: str) -> IngestResult:
    raw = load_spec(OPENAPI_DIR / f"{file}.json")
    return ingest(session, parse_spec(raw, system), raw)


def field_id(session: Session, version_id: int, entity: str, path: str) -> int:
    found = session.scalar(
        select(FieldRow.id)
        .join(EntityRow, EntityRow.id == FieldRow.entity_id)
        .where(
            EntityRow.system_version_id == version_id,
            EntityRow.name == entity,
            FieldRow.path == path,
        )
    )
    assert found is not None, (entity, path)
    return found


class ScriptedProvider:
    """Returns hand-placed unit vectors so retrieval ordering can be asserted exactly."""

    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self, model_name: str, placement: dict[str, float]) -> None:
        self.model_name = model_name
        self.placement = placement  # text prefix -> angle in radians

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            angle = next(
                (a for prefix, a in self.placement.items() if text.startswith(prefix)), 3.0
            )
            vector = [0.0] * EMBEDDING_DIMENSIONS
            vector[0], vector[1] = math.cos(angle), math.sin(angle)
            out.append(vector)
        return out


def small_spec(
    title: str, entity: str, props: dict[str, Any], role_error: bool = False
) -> dict[str, Any]:
    paths: dict[str, Any] = {
        f"/{entity.lower()}": {
            "get": {
                "responses": {
                    "404" if role_error else "200": {
                        "description": "r",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": f"#/components/schemas/{entity}"}
                            }
                        },
                    }
                }
            }
        }
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": "1"},
        "paths": paths,
        "components": {
            "schemas": {entity: {"type": "object", "properties": props}},
        },
    }


# ---- fake provider -----------------------------------------------------------------------------


def test_fake_provider_is_deterministic_normalised_and_sized() -> None:
    provider = FakeEmbeddingProvider()
    first = provider.embed(["Customer.email | type: string"])[0]
    second = FakeEmbeddingProvider().embed(["Customer.email | type: string"])[0]
    assert first == second
    assert len(first) == EMBEDDING_DIMENSIONS == provider.dimensions
    assert math.isclose(math.sqrt(sum(v * v for v in first)), 1.0, rel_tol=1e-9)
    assert provider.model_name == FAKE_MODEL_NAME


def test_fake_provider_gives_shared_tokens_higher_similarity() -> None:
    a, b, c = FakeEmbeddingProvider().embed(
        ["customer email address", "support email address", "unix epoch seconds"]
    )

    def cosine(x: list[float], y: list[float]) -> float:
        return sum(p * q for p, q in zip(x, y, strict=True))

    assert cosine(a, b) > cosine(a, c)


# ---- text templates ----------------------------------------------------------------------------


def test_field_text_snapshots(session: Session) -> None:
    crm = parse_spec(load_spec(OPENAPI_DIR / "crm.v1.json"), "crm")
    customer = crm.entity("Customer")
    assert customer is not None
    by_name = {f.name: f for f in customer.fields}
    assert field_text("Customer", by_name["status"]) == (
        "Customer.status | type: string | Lifecycle status of the customer account."
        " | allowed values: ACTIVE, INACTIVE, SUSPENDED | examples: ACTIVE"
    )
    assert field_text("Customer", by_name["customer_id"]) == (
        "Customer.customer_id | type: string | Unique customer identifier: the letter 'C',"
        " a hyphen and decimal digits. | examples: C-1837"
    )
    support = parse_spec(load_spec(OPENAPI_DIR / "support.v1.json"), "support")
    user = support.entity("User")
    assert user is not None
    created = {f.name: f for f in user.fields}["createdAt"]
    assert field_text("User", created) == (
        "User.createdAt | type: integer | Creation time as an integer count of seconds since"
        " the Unix epoch (UTC). | examples: 1709633730"
    )


def test_entity_text_snapshot() -> None:
    support = parse_spec(load_spec(OPENAPI_DIR / "support.v1.json"), "support")
    user = support.entity("User")
    assert user is not None
    assert entity_text(user) == (
        "User | fields: userId, externalRef, fullName, email_address, phoneNumber,"
        " accountState, tier, createdAt"
    )


def test_field_text_includes_format_and_skips_missing_segments() -> None:
    from app.discovery.models import Field

    plain = Field(name="id", path="id", json_type="integer")
    assert field_text("T", plain) == "T.id | type: integer"
    dated = Field(name="at", path="a.at", json_type="string", format="date-time")
    assert field_text("T", dated) == "T.a.at | type: string date-time"


# ---- embedding a stored version ----------------------------------------------------------------


def test_embed_version_stores_model_name_and_is_idempotent(session: Session) -> None:
    result = ingest_file(session, "crm", "crm.v1")
    provider = FakeEmbeddingProvider()
    stats = embed_version(session, result.version_id, provider)
    assert stats.fields_embedded == 8 + 6 + 4 + 6 + 1 + 5
    assert stats.entities_embedded == 6
    again = embed_version(session, result.version_id, provider)
    assert (again.fields_embedded, again.entities_embedded) == (0, 0)
    models = set(session.scalars(select(FieldEmbedding.model_name)))
    assert models == {FAKE_MODEL_NAME}
    assert set(session.scalars(select(EntityEmbedding.model_name))) == {FAKE_MODEL_NAME}
    stored = session.scalar(select(FieldEmbedding.text).limit(1))
    assert stored is not None and stored.startswith("Customer.")


def test_a_second_model_adds_its_own_embeddings(session: Session) -> None:
    result = ingest_file(session, "support", "support.v1")
    embed_version(session, result.version_id, FakeEmbeddingProvider())
    other = ScriptedProvider("other-model", {})
    stats = embed_version(session, result.version_id, other)
    assert stats.fields_embedded > 0
    total = session.scalar(select(func.count()).select_from(FieldEmbedding))
    assert total == 2 * stats.fields_embedded


# ---- retrieval ---------------------------------------------------------------------------------


@pytest.fixture
def placed(session: Session) -> tuple[ScriptedProvider, dict[str, int], dict[str, IngestResult]]:
    """System A with a source field and a same-system decoy; system B with three candidates."""
    a_spec = small_spec("A", "Alpha", {"src": {"type": "string"}, "decoy": {"type": "string"}})
    b_spec = small_spec(
        "B",
        "Beta",
        {"near": {"type": "string"}, "mid": {"type": "string"}, "far": {"type": "string"}},
    )
    ingested = {
        "A": ingest(session, parse_spec(a_spec, "A"), a_spec),
        "B": ingest(session, parse_spec(b_spec, "B"), b_spec),
    }
    provider = ScriptedProvider(
        "scripted",
        {
            "Alpha.src": 0.0,
            "Alpha.decoy": 0.01,  # closest of all, but in the same system
            "Beta.near": 0.1,
            "Beta.mid": 0.5,
            "Beta.far": 1.5,
        },
    )
    for result in ingested.values():
        embed_version(session, result.version_id, provider)
    ids = {
        "src": field_id(session, ingested["A"].version_id, "Alpha", "src"),
        "near": field_id(session, ingested["B"].version_id, "Beta", "near"),
        "mid": field_id(session, ingested["B"].version_id, "Beta", "mid"),
        "far": field_id(session, ingested["B"].version_id, "Beta", "far"),
    }
    return provider, ids, ingested


def paths(results: list[SimilarField]) -> list[str]:
    return [r.field_path for r in results]


def test_retrieval_orders_by_cosine_distance_with_scores(
    session: Session, placed: tuple[ScriptedProvider, dict[str, int], dict[str, IngestResult]]
) -> None:
    provider, ids, _ = placed
    results = similar_fields(session, ids["src"], model_name=provider.model_name, k=5)
    assert paths(results) == ["near", "mid", "far"]
    assert results[0].distance == pytest.approx(1 - math.cos(0.1), abs=1e-6)
    assert results[1].distance == pytest.approx(1 - math.cos(0.5), abs=1e-6)
    assert results[2].distance == pytest.approx(1 - math.cos(1.5), abs=1e-6)
    assert [r.distance for r in results] == sorted(r.distance for r in results)
    assert (results[0].system_name, results[0].entity_name, results[0].version) == ("B", "Beta", 1)


def test_retrieval_respects_k(
    session: Session, placed: tuple[ScriptedProvider, dict[str, int], dict[str, IngestResult]]
) -> None:
    provider, ids, _ = placed
    assert paths(similar_fields(session, ids["src"], model_name=provider.model_name, k=2)) == [
        "near",
        "mid",
    ]


def test_retrieval_never_returns_fields_of_the_same_system(
    session: Session, placed: tuple[ScriptedProvider, dict[str, int], dict[str, IngestResult]]
) -> None:
    provider, ids, ingested = placed
    results = similar_fields(session, ids["src"], model_name=provider.model_name, k=10)
    assert all(r.system_id != ingested["A"].system_id for r in results)
    assert "decoy" not in paths(results)
    # and in reverse: from B, only A's fields come back
    reverse = similar_fields(session, ids["near"], model_name=provider.model_name, k=10)
    assert {r.system_name for r in reverse} == {"A"}


def test_retrieval_can_be_restricted_to_a_system_and_an_entity(
    session: Session, placed: tuple[ScriptedProvider, dict[str, int], dict[str, IngestResult]]
) -> None:
    provider, ids, ingested = placed
    only_b = similar_fields(
        session,
        ids["src"],
        model_name=provider.model_name,
        target_system_id=ingested["B"].system_id,
    )
    assert paths(only_b) == ["near", "mid", "far"]
    nothing = similar_fields(
        session,
        ids["src"],
        model_name=provider.model_name,
        target_system_id=ingested["A"].system_id,
    )
    assert nothing == []
    wrong_entity = similar_fields(
        session, ids["src"], model_name=provider.model_name, target_entity="Nope"
    )
    assert wrong_entity == []
    right_entity = similar_fields(
        session, ids["src"], model_name=provider.model_name, target_entity="Beta", k=1
    )
    assert paths(right_entity) == ["near"]


def test_retrieval_can_filter_by_entity_role(session: Session) -> None:
    a_spec = small_spec("A", "Alpha", {"src": {"type": "string"}})
    b_spec = small_spec("B", "Problem", {"near": {"type": "string"}}, role_error=True)
    a = ingest(session, parse_spec(a_spec, "A"), a_spec)
    b = ingest(session, parse_spec(b_spec, "B"), b_spec)
    provider = ScriptedProvider("scripted", {"Alpha.src": 0.0, "Problem.near": 0.1})
    for result in (a, b):
        embed_version(session, result.version_id, provider)
    source = field_id(session, a.version_id, "Alpha", "src")
    both = similar_fields(session, source, model_name="scripted")
    assert [r.entity_role for r in both] == [EntityRole.ERROR]
    assert similar_fields(session, source, model_name="scripted", roles={EntityRole.RESOURCE}) == []


def test_models_are_never_mixed(session: Session) -> None:
    a_spec = small_spec("A", "Alpha", {"src": {"type": "string"}})
    b_spec = small_spec("B", "Beta", {"near": {"type": "string"}})
    a = ingest(session, parse_spec(a_spec, "A"), a_spec)
    b = ingest(session, parse_spec(b_spec, "B"), b_spec)
    embed_version(session, a.version_id, ScriptedProvider("model-one", {"Alpha.src": 0.0}))
    embed_version(session, b.version_id, ScriptedProvider("model-two", {"Beta.near": 0.0}))
    source = field_id(session, a.version_id, "Alpha", "src")
    # B was embedded with another model, so it is invisible to model-one queries
    assert similar_fields(session, source, model_name="model-one") == []
    # a model that never embedded the source field is an error, not a silent empty list
    with pytest.raises(LookupError, match="model-two"):
        similar_fields(session, source, model_name="model-two")


def test_retrieval_uses_latest_version_unless_pinned(session: Session) -> None:
    support = ingest_file(session, "support", "support.v1")
    v1 = ingest_file(session, "crm", "crm.v1")
    v2 = ingest_file(session, "crm", "crm.v2")
    provider = FakeEmbeddingProvider()
    for result in (support, v1, v2):
        embed_version(session, result.version_id, provider)
    source = field_id(session, support.version_id, "User", "phoneNumber")

    latest = similar_fields(session, source, model_name=provider.model_name, k=50)
    assert {r.version for r in latest} == {2}
    assert "phone_number" in {r.field_path for r in latest}
    assert "phone" not in {r.field_path for r in latest}

    pinned = similar_fields(
        session, source, model_name=provider.model_name, k=50, target_version_id=v1.version_id
    )
    assert {r.version for r in pinned} == {1}
    assert "phone" in {r.field_path for r in pinned}
