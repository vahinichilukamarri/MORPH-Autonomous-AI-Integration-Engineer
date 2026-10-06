import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.discovery import get_provider
from app.api.mapping import get_llm
from app.db import get_session
from app.db_models import LLMCall, Mapping, MappingRun, MappingVersion
from app.discovery.parser import parse_spec
from app.discovery.repository import IngestResult, ingest
from app.discovery.source import load_spec
from app.embeddings.fake import FakeEmbeddingProvider
from app.llm.base import LLMRequest, RateLimitExhausted
from app.llm.fake import ScriptedFakeProvider
from app.main import create_app
from app.mapping.runner import MappingRunResult, run_mapping
from app.mapping.store import save_run
from app.settings import Settings, get_settings
from tests.mapping.helpers import (
    CREATED_AT,
    FULL_NAME,
    OPENAPI,
    PHONE,
    ROOT,
    SAMPLES,
    STATE,
    TIER,
    USER_ID,
)
from tests.mapping.test_proposal import BLANK

CORRECT: dict[str, tuple[Any, ...]] = {
    spec[0]: spec for spec in (USER_ID, FULL_NAME, PHONE, STATE, TIER, CREATED_AT)
}
CORRECT["externalRef"] = (
    "externalRef",
    "DIRECT",
    ("customer_id",),
    {"op": "COPY", "field": "customer_id"},
)
CORRECT["email_address"] = (
    "email_address",
    "DIRECT",
    ("email",),
    {"op": "COPY", "field": "email"},
)


def flat(step: Any) -> dict[str, Any]:
    out = {**BLANK, **step}
    if "mapping" in step:
        out["mapping"] = [{"source": k, "target": v} for k, v in step["mapping"].items()]
    return out


def reply_for(target: str) -> str:
    _, kind, sources, *steps = CORRECT[target]
    return json.dumps(
        {
            "target_field": target,
            "mapping_type": getattr(kind, "value", kind),
            "source_fields": list(sources),
            "steps": [flat(s) for s in steps],
            "unresolved_reason": None,
            "rationale": "scripted",
            "alternatives": [],
            "certainty": "HIGH",
        }
    )


def target_of(request: LLMRequest) -> str:
    block = request.parts[0].split('name="TARGET_FIELD">>>')[1]
    found = re.search(r'"name": "([^"]+)"', block)
    assert found
    return found.group(1)


def correct_llm() -> ScriptedFakeProvider:
    return ScriptedFakeProvider(responder=lambda request: reply_for(target_of(request)))


@pytest.fixture
def versions(session: Session) -> tuple[IngestResult, IngestResult]:
    out = []
    for system, file in (("crm", "crm.v1.json"), ("support", "support.v1.json")):
        raw = load_spec(OPENAPI / file)
        out.append(ingest(session, parse_spec(raw, system), raw))
    return out[0], out[1]


def run(
    session: Session,
    versions: tuple[IngestResult, IngestResult],
    llm: ScriptedFakeProvider,
    mode: str = "full_schema",
) -> MappingRunResult:
    crm, support = versions
    return run_mapping(
        session,
        llm,
        FakeEmbeddingProvider(),
        source_version_id=crm.version_id,
        target_version_id=support.version_id,
        source_entity="Customer",
        target_entity="User",
        mode=mode,  # type: ignore[arg-type]
        samples_dir=SAMPLES,
    )


# ---- the runner --------------------------------------------------------------------------------


def test_every_target_field_gets_exactly_one_validated_item(
    session: Session, versions: tuple[IngestResult, IngestResult]
) -> None:
    result = run(session, versions, correct_llm())
    assert [i.target_field for i in result.items] == [
        "userId",
        "externalRef",
        "fullName",
        "email_address",
        "phoneNumber",
        "accountState",
        "tier",
        "createdAt",
    ]
    assert result.run_reasons == ()
    by_field = {i.target_field: i for i in result.items}
    for name, item in by_field.items():
        # retrieval ambiguity depends on the (fake) embeddings, so judge the contract checks only
        contract = {
            r.code.value for r in item.validation.reasons if "AMBIGUOUS" not in r.code.value
        }
        assert contract == ({"INFORMATION_LOSS_ENUM"} if name == "tier" else set()), name
        assert len(item.attempts) == 1 and not item.invalid_output
    assert result.provider == "scripted" and result.model == "scripted-fake"
    assert result.summary["fields"] == 8 and result.summary["llm_calls"] == 8


def test_rag_mode_prompts_contain_only_retrieved_fields(
    session: Session, versions: tuple[IngestResult, IngestResult]
) -> None:
    llm = correct_llm()
    run(session, versions, llm, mode="rag")
    for request in llm.calls:
        prompt = request.parts[0]
        assert 'name="RETRIEVED_SOURCE_FIELDS"' in prompt
        assert prompt.count('"rank":') == 5, "top-5 retrieved source fields"


def test_confidence_and_review_are_deterministic_functions_of_signals(
    session: Session, versions: tuple[IngestResult, IngestResult]
) -> None:
    first = run(session, versions, correct_llm())
    second = run(session, versions, correct_llm())
    assert [(i.confidence, i.review_status, i.review_reasons) for i in first.items] == [
        (i.confidence, i.review_status, i.review_reasons) for i in second.items
    ]
    assert all(i.confidence is not None for i in first.items)


def test_invalid_output_becomes_unresolved_and_is_recorded(
    session: Session, versions: tuple[IngestResult, IngestResult]
) -> None:
    def responder(request: LLMRequest) -> str:
        target = target_of(request)
        return "garbage" if target == "tier" else reply_for(target)

    result = run(session, versions, ScriptedFakeProvider(responder=responder))
    tier = next(i for i in result.items if i.target_field == "tier")
    assert tier.invalid_output and len(tier.attempts) == 2
    assert tier.proposal.unresolved_reason == "invalid_llm_output"
    assert tier.confidence is None and tier.review_status.value == "NEEDS_REVIEW"
    assert result.summary["invalid_output_fields"] == 1 and result.summary["reasked_fields"] == 1
    assert result.summary["llm_calls"] == 9 and result.summary["unresolved"] == 1


# ---- persistence -------------------------------------------------------------------------------


def test_run_is_persisted_with_versions_and_call_metadata(
    session: Session, versions: tuple[IngestResult, IngestResult]
) -> None:
    result = run(session, versions, correct_llm())
    saved = save_run(session, result, 0.0)
    assert saved.prompt_version == "v1" and saved.confidence_version == "confidence-v1"
    mappings = session.scalars(select(Mapping).where(Mapping.mapping_run_id == saved.id)).all()
    assert len(mappings) == 8
    rows = session.scalars(select(MappingVersion)).all()
    assert (
        len(rows) == 8
        and {r.version for r in rows} == {1}
        and {r.author for r in rows} == {"system"}
    )
    calls = session.scalars(select(LLMCall).where(LLMCall.mapping_run_id == saved.id)).all()
    assert len(calls) == 8
    call = calls[0]
    assert (call.provider, call.model, call.outcome, call.source) == (
        "scripted",
        "scripted-fake",
        "OK",
        "scripted",
    )
    assert len(call.prompt_hash) == 64 and call.latency_ms >= 0
    columns = {c.name for c in LLMCall.__table__.columns}
    assert not {"prompt", "prompt_text", "api_key", "response"} & columns, "no prompts, no secrets"


# ---- API ---------------------------------------------------------------------------------------


@pytest.fixture
def client(session: Session) -> TestClient:
    app = create_app()
    settings = Settings(embedding_provider="fake", spec_root=ROOT, samples_dir=SAMPLES)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_provider] = lambda: FakeEmbeddingProvider()
    app.dependency_overrides[get_llm] = correct_llm
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def start_run(client: TestClient, versions: tuple[IngestResult, IngestResult]) -> dict[str, Any]:
    crm, support = versions
    response = client.post(
        "/mapping-runs",
        json={
            "source_system_version": crm.version_id,
            "target_system_version": support.version_id,
            "source_entity": "Customer",
            "target_entity": "User",
            "mode": "full_schema",
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_api_creates_and_reads_a_run(
    client: TestClient, versions: tuple[IngestResult, IngestResult]
) -> None:
    run_body = start_run(client, versions)
    assert run_body["mode"] == "full_schema" and run_body["summary"]["fields"] == 8
    assert run_body["model"] == "scripted-fake"
    again = client.get(f"/mapping-runs/{run_body['id']}").json()
    assert again["id"] == run_body["id"]
    mappings = client.get(f"/mapping-runs/{run_body['id']}/mappings").json()
    assert len(mappings) == 8
    tier = next(m for m in mappings if m["target_field"] == "tier")
    assert tier["versions"] == 1 and tier["current"]["version"] == 1
    assert tier["current"]["mapping_type"] == "DERIVED"
    assert tier["current"]["validation_status"] == "WARN"
    assert tier["current"]["validation_reasons"][0]["code"] == "INFORMATION_LOSS_ENUM"
    assert tier["current"]["transformation"]["steps"][0]["op"] == "COPY"
    assert client.get("/mapping-runs/9999").status_code == 404


def test_unknown_versions_or_entities_are_404(
    client: TestClient, versions: tuple[IngestResult, IngestResult]
) -> None:
    crm, support = versions
    body = {
        "source_system_version": crm.version_id,
        "target_system_version": support.version_id,
        "source_entity": "Nope",
        "target_entity": "User",
        "mode": "rag",
    }
    assert client.post("/mapping-runs", json=body).status_code == 404
    assert (
        client.post("/mapping-runs", json={**body, "source_system_version": 99999}).status_code
        == 404
    )


def test_review_approve_appends_a_version_and_keeps_the_old_one(
    client: TestClient, versions: tuple[IngestResult, IngestResult], session: Session
) -> None:
    run_body = start_run(client, versions)
    mapping = next(
        m
        for m in client.get(f"/mapping-runs/{run_body['id']}/mappings").json()
        if m["target_field"] == "phoneNumber"
    )
    response = client.post(f"/mappings/{mapping['id']}/review", json={"action": "approve"})
    assert response.status_code == 200
    current = response.json()["current"]
    assert (current["version"], current["author"], current["review_status"]) == (
        2,
        "human",
        "APPROVED",
    )
    versions_out = client.get(f"/mappings/{mapping['id']}/versions").json()
    assert [v["version"] for v in versions_out] == [1, 2]
    assert versions_out[0]["author"] == "system" and versions_out[0]["review_status"] != "APPROVED"
    assert versions_out[0]["transformation"] == versions_out[1]["transformation"]


def test_review_override_is_revalidated_and_versioned(
    client: TestClient, versions: tuple[IngestResult, IngestResult]
) -> None:
    run_body = start_run(client, versions)
    mapping = next(
        m
        for m in client.get(f"/mapping-runs/{run_body['id']}/mappings").json()
        if m["target_field"] == "tier"
    )
    good = {
        "action": "override",
        "mapping": {
            "mapping_type": "CONSTANT",
            "transformation": {"steps": [{"op": "CONSTANT", "value": "STANDARD"}]},
            "rationale": "everyone is standard for now",
        },
    }
    ok = client.post(f"/mappings/{mapping['id']}/review", json=good)
    assert ok.status_code == 200 and ok.json()["current"]["version"] == 2
    assert ok.json()["current"]["review_status"] == "OVERRIDDEN"

    bad = {
        "action": "override",
        "mapping": {
            "mapping_type": "CONSTANT",
            "transformation": {"steps": [{"op": "CONSTANT", "value": "GOLD"}]},
        },
    }
    rejected = client.post(f"/mappings/{mapping['id']}/review", json=bad)
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["reasons"][0]["code"] == "ENUM_VIOLATION"
    assert len(client.get(f"/mappings/{mapping['id']}/versions").json()) == 2, "nothing was added"

    missing_body = client.post(f"/mappings/{mapping['id']}/review", json={"action": "override"})
    assert missing_body.status_code == 422
    no_reason = {"action": "override", "mapping": {"mapping_type": "UNRESOLVED"}}
    assert client.post(f"/mappings/{mapping['id']}/review", json=no_reason).status_code == 422


def test_a_failing_mapping_cannot_be_approved(
    client: TestClient, versions: tuple[IngestResult, IngestResult]
) -> None:
    def wrong(request: LLMRequest) -> str:
        target = target_of(request)
        if target != "tier":
            return reply_for(target)
        body = json.loads(reply_for("tier"))
        body["steps"] = [flat({"op": "CONSTANT", "value": "GOLD"})]
        body["mapping_type"] = "CONSTANT"
        body["source_fields"] = []
        return json.dumps(body)

    client.app.dependency_overrides[get_llm] = lambda: ScriptedFakeProvider(responder=wrong)  # type: ignore[attr-defined]
    run_body = start_run(client, versions)
    mapping = next(
        m
        for m in client.get(f"/mapping-runs/{run_body['id']}/mappings").json()
        if m["target_field"] == "tier"
    )
    assert mapping["current"]["validation_status"] == "FAIL"
    assert mapping["current"]["review_status"] == "NEEDS_REVIEW"
    response = client.post(f"/mappings/{mapping['id']}/review", json={"action": "approve"})
    assert (
        response.status_code == 422 and "cannot be approved" in response.json()["detail"]["message"]
    )


def test_provider_rate_limit_becomes_429(
    client: TestClient, versions: tuple[IngestResult, IngestResult]
) -> None:
    def exhausted(request: LLMRequest) -> str:
        raise RateLimitExhausted("daily limit", 3600)

    client.app.dependency_overrides[get_llm] = lambda: ScriptedFakeProvider(responder=exhausted)  # type: ignore[attr-defined]
    crm, support = versions
    response = client.post(
        "/mapping-runs",
        json={
            "source_system_version": crm.version_id,
            "target_system_version": support.version_id,
            "source_entity": "Customer",
            "target_entity": "User",
        },
    )
    assert response.status_code == 429 and "daily limit" in response.json()["detail"]


def test_nothing_is_stored_when_a_run_fails(
    client: TestClient, versions: tuple[IngestResult, IngestResult], session: Session
) -> None:
    client.app.dependency_overrides[get_llm] = lambda: ScriptedFakeProvider(  # type: ignore[attr-defined]
        responder=lambda r: (_ for _ in ()).throw(RateLimitExhausted("x"))
    )
    crm, support = versions
    client.post(
        "/mapping-runs",
        json={
            "source_system_version": crm.version_id,
            "target_system_version": support.version_id,
            "source_entity": "Customer",
            "target_entity": "User",
        },
    )
    assert session.scalar(select(func.count()).select_from(MappingRun)) == 0


def test_samples_directory_exists_for_the_default_settings() -> None:
    assert Path(Settings().samples_dir).is_dir()
