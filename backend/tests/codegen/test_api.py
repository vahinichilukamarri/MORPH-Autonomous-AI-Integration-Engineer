from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db import get_session
from app.main import create_app
from app.mapping.confidence import ReviewStatus
from tests.codegen.fixtures import S1_PIPELINES, S3_PIPELINES, mapped, persist_run


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)


def s1(session: Session) -> int:
    return persist_run(
        session, source=("crm.v1", "crm"), target=("support.v1", "support"),
        source_entity="Customer", target_entity="User", fields=mapped(S1_PIPELINES),
    )  # fmt: skip


def test_generate_and_inspect(client: TestClient, session: Session) -> None:
    created = client.post("/integrations", json={"mapping_run_id": s1(session)})
    assert created.status_code == 201
    version = created.json()
    assert version["status"] == "GENERATED" and version["review"]["gate_status"] == "OPEN"
    iid = version["integration_id"]
    assert client.get(f"/integrations/{iid}").json()["versions"] == [1]
    assert (
        client.get(f"/integrations/{iid}/versions/1").json()["bundle_hash"]
        == version["bundle_hash"]
    )
    files = {f["path"]: f for f in client.get(f"/integrations/{iid}/versions/1/files").json()}
    assert "integration/transform.py" in files and files["integration/__init__.py"]["content"] == ""
    gate = client.get(f"/integrations/{iid}/versions/1/gate").json()
    assert gate == [{"stage": "ast", "passed": True, "findings": []}]
    assert client.get(f"/integrations/{iid}/versions/1/sandbox-runs").json() == []


def test_blocked_integration_explains_why(client: TestClient, session: Session) -> None:
    run = persist_run(
        session, source=("support.v1", "support"), target=("crm.v1", "crm"),
        source_entity="User", target_entity="Customer",
        fields=mapped(S3_PIPELINES, segment=(ReviewStatus.NEEDS_REVIEW, None)),
    )  # fmt: skip
    version = client.post("/integrations", json={"mapping_run_id": run}).json()
    assert version["status"] == "BLOCKED_PENDING_REVIEW"
    assert [b["target_field"] for b in version["review"]["blocking"]] == ["segment"]
    files = client.get(f"/integrations/{version['integration_id']}/versions/1/files").json()
    assert files == []


def test_unknown_ids_are_404(client: TestClient) -> None:
    assert client.post("/integrations", json={"mapping_run_id": 999999}).status_code == 404
    assert client.get("/integrations/999999").status_code == 404
    assert (
        client.post("/integrations", json={"mapping_run_id": 1, "condition": "X"}).status_code
        == 422
    )
