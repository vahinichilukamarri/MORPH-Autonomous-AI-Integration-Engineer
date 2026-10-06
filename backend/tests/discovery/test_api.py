import json
import threading
from collections.abc import Iterator
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.discovery import get_provider
from app.db import get_session
from app.embeddings.fake import FAKE_MODEL_NAME, FakeEmbeddingProvider
from app.main import create_app
from app.settings import Settings, get_settings

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENAPI = "mock_systems/openapi"


def make_client(session: Session, spec_root: Path = REPO_ROOT) -> TestClient:
    app = create_app()
    settings = Settings(embedding_provider="fake", spec_root=spec_root)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_provider] = lambda: FakeEmbeddingProvider()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


@pytest.fixture
def client(session: Session) -> TestClient:
    return make_client(session)


def ingest(client: TestClient, name: str, file: str) -> dict[str, Any]:
    response = client.post(
        "/systems/ingest", json={"name": name, "source": {"file": f"{OPENAPI}/{file}.json"}}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_ingest_returns_a_summary(client: TestClient) -> None:
    body = ingest(client, "crm", "crm.v1")
    assert (body["version"], body["created"]) == (1, True)
    assert body["entities"] == 6
    assert body["fields"] == 30
    assert body["fields_embedded"] == 30 and body["entities_embedded"] == 6
    assert body["embedding_model"] == FAKE_MODEL_NAME
    assert len(body["spec_hash"]) == 64


def test_reingest_is_a_noop_and_changed_spec_is_version_two(client: TestClient) -> None:
    first = ingest(client, "crm", "crm.v1")
    again = ingest(client, "crm", "crm.v1")
    assert again["created"] is False
    assert (again["version"], again["fields_embedded"]) == (1, 0)
    second = ingest(client, "crm", "crm.v2")
    assert (second["version"], second["created"]) == (2, True)
    assert second["system_id"] == first["system_id"]

    versions = client.get(f"/systems/{first['system_id']}/versions").json()
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["spec_hash"] == first["spec_hash"]
    assert versions[1]["spec_hash"] == second["spec_hash"]


def test_list_and_detail(client: TestClient) -> None:
    crm = ingest(client, "crm", "crm.v1")
    ingest(client, "support", "support.v1")
    listing = client.get("/systems").json()
    assert [(s["name"], s["latest_version"]) for s in listing] == [("crm", 1), ("support", 1)]
    detail = client.get(f"/systems/{crm['system_id']}").json()
    assert detail["name"] == "crm" and detail["api_title"] == "Mock CRM"
    assert (detail["entities"], detail["fields"], detail["operations"]) == (6, 30, 4)
    assert detail["auth_schemes"][0]["name"] == "APIKeyHeader"
    assert client.get("/systems/9999").status_code == 404


def test_entities_default_to_latest_and_accept_a_version(client: TestClient) -> None:
    crm = ingest(client, "crm", "crm.v1")
    ingest(client, "crm", "crm.v2")
    sid = crm["system_id"]

    def customer_fields(**params: Any) -> list[str]:
        entities = client.get(f"/systems/{sid}/entities", params=params).json()
        customer = next(e for e in entities if e["name"] == "Customer")
        return [f["path"] for f in customer["fields"]]

    assert "phone_number" in customer_fields() and "phone" not in customer_fields()
    assert "phone" in customer_fields(version=1) and "phone_number" not in customer_fields(
        version=1
    )
    assert client.get(f"/systems/{sid}/entities", params={"version": 7}).status_code == 404


def test_entity_payload_has_field_ids_and_roles(client: TestClient) -> None:
    crm = ingest(client, "crm", "crm.v1")
    entities = {e["name"]: e for e in client.get(f"/systems/{crm['system_id']}/entities").json()}
    assert entities["CustomerPage"]["role"] == "WRAPPER"
    assert entities["CustomerPage"]["wrapped_entity"] == "Customer"
    assert entities["HTTPValidationError"]["role"] == "ERROR"
    status = next(f for f in entities["Customer"]["fields"] if f["path"] == "status")
    assert status["enum_values"] == ["ACTIVE", "INACTIVE", "SUSPENDED"]
    assert isinstance(status["id"], int)


def test_similar_fields_endpoint(client: TestClient) -> None:
    crm = ingest(client, "crm", "crm.v1")
    support = ingest(client, "support", "support.v1")
    entities = client.get(f"/systems/{crm['system_id']}/entities").json()
    email = next(
        f["id"]
        for e in entities
        if e["name"] == "Customer"
        for f in e["fields"]
        if f["path"] == "email"
    )
    results = client.get(f"/fields/{email}/similar", params={"k": 3}).json()
    assert len(results) == 3
    assert all(r["system_name"] == "support" for r in results)
    assert [r["distance"] for r in results] == sorted(r["distance"] for r in results)
    assert {"field_id", "entity", "path", "json_type", "distance", "version"} <= set(results[0])

    only_user = client.get(
        f"/fields/{email}/similar",
        params={"k": 50, "target_system": support["system_id"], "target_entity": "User"},
    ).json()
    assert {r["entity"] for r in only_user} == {"User"}
    assert len(only_user) == 8
    assert (
        client.get(f"/fields/{email}/similar", params={"target_system": crm["system_id"]}).json()
        == []
    )
    assert client.get("/fields/999999/similar").status_code == 404
    assert client.get(f"/fields/{email}/similar", params={"k": 0}).status_code == 422


def test_ingest_validates_the_request(client: TestClient) -> None:
    both = {"name": "x", "source": {"file": "a", "url": "http://b"}}
    assert client.post("/systems/ingest", json=both).status_code == 422
    neither = {"name": "x", "source": {}}
    assert client.post("/systems/ingest", json=neither).status_code == 422
    assert (
        client.post("/systems/ingest", json={"name": "", "source": {"file": "a"}}).status_code
        == 422
    )


def test_files_outside_the_spec_root_are_refused(client: TestClient) -> None:
    body = {"name": "x", "source": {"file": "../../etc/hosts"}}
    assert client.post("/systems/ingest", json=body).status_code == 400


def test_invalid_spec_returns_structured_problems(session: Session, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "T", "version": "1"},
                "paths": {},
                "components": {
                    "schemas": {
                        "T": {"type": "object", "properties": {"a": {"not": {"type": "string"}}}}
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    client = make_client(session, spec_root=tmp_path)
    response = client.post("/systems/ingest", json={"name": "bad", "source": {"file": "bad.json"}})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid_spec"
    assert body["problems"] == [
        {
            "pointer": "/components/schemas/T/properties/a/not",
            "message": "unsupported keyword 'not'",
        }
    ]
    assert client.get("/systems").json() == [], "nothing was stored"


def test_missing_file_is_a_structured_error(client: TestClient) -> None:
    body = {"name": "x", "source": {"file": f"{OPENAPI}/nope.json"}}
    response = client.post("/systems/ingest", json=body)
    assert response.status_code == 422
    assert "cannot load" in response.json()["problems"][0]["message"]


@pytest.fixture
def spec_server() -> Iterator[str]:
    payload = (REPO_ROOT / OPENAPI / "support.v1.json").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), partial(Handler))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/openapi.json"
    server.shutdown()
    thread.join()


def test_ingest_from_a_live_url(client: TestClient, spec_server: str) -> None:
    response = client.post("/systems/ingest", json={"name": "live", "source": {"url": spec_server}})
    assert response.status_code == 200, response.text
    assert response.json()["entities"] == 4
    same_as_file = ingest(client, "from-file", "support.v1")
    assert same_as_file["spec_hash"] == response.json()["spec_hash"]
