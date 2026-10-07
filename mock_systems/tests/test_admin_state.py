from typing import Any

import pytest
from fastapi.testclient import TestClient

from crm.main import create_app as create_crm_app
from support.main import create_app as create_support_app

ADMIN = {"X-Admin-Token": "test-admin"}
CRM_KEY = "k"
TOKEN = "t"


def crm_client() -> TestClient:
    return TestClient(create_crm_app(api_key=CRM_KEY, admin_token="test-admin"))


def support_client() -> TestClient:
    return TestClient(create_support_app(token=TOKEN, admin_token="test-admin"))


CUSTOMER: dict[str, Any] = {
    "customer_id": "C-5000",
    "first_name": "Zed",
    "last_name": None,
    "email": "zed@example.com",
    "phone": None,
    "status": "ACTIVE",
    "segment": "SMB",
    "created_at": "2024-02-29T00:00:00Z",
}
USER: dict[str, Any] = {
    "userId": 7,
    "externalRef": None,
    "fullName": "Zed Z",
    "email_address": "zed@example.com",
    "phoneNumber": None,
    "accountState": "ENABLED",
    "tier": "STANDARD",
    "createdAt": 0,
}


def test_crm_state_roundtrip_and_next_id() -> None:
    client = crm_client()
    put = client.put("/__admin/state", json={"records": [CUSTOMER]}, headers=ADMIN)
    assert put.status_code == 200 and len(put.json()["records"]) == 1
    listing = client.get("/customers", headers={"X-API-Key": CRM_KEY}).json()
    assert listing["total"] == 1
    created = client.post(
        "/customers",
        headers={"X-API-Key": CRM_KEY},
        json={"first_name": "A", "email": "a@example.com", "segment": "SMB"},
    )
    assert created.json()["customer_id"] == "C-5001"
    assert len(client.get("/__admin/state", headers=ADMIN).json()["records"]) == 2


def test_support_state_roundtrip() -> None:
    client = support_client()
    assert client.put("/__admin/state", json={"records": [USER]}, headers=ADMIN).status_code == 200
    got = client.get("/users/7", headers={"Authorization": f"Bearer {TOKEN}"})
    assert got.status_code == 200 and got.json()["fullName"] == "Zed Z"
    assert client.get("/__admin/state", headers=ADMIN).json()["records"] == [USER]


@pytest.mark.parametrize("make", [crm_client, support_client])
def test_invalid_or_duplicate_state_rejected_and_unchanged(make: Any) -> None:
    client = make()
    before = client.get("/__admin/state", headers=ADMIN).json()
    bad = client.put("/__admin/state", json={"records": [{"nope": 1}]}, headers=ADMIN)
    assert bad.status_code == 422
    record = CUSTOMER if make is crm_client else USER
    dup = client.put("/__admin/state", json={"records": [record, record]}, headers=ADMIN)
    assert dup.status_code == 422
    assert client.get("/__admin/state", headers=ADMIN).json() == before


@pytest.mark.parametrize("make", [crm_client, support_client])
def test_state_and_requests_need_admin_token(make: Any) -> None:
    client = make()
    for method, path in [("get", "/__admin/state"), ("get", "/__admin/requests")]:
        assert getattr(client, method)(path).status_code == 401
    assert client.put("/__admin/state", json={"records": []}).status_code == 401


def test_admin_endpoints_are_not_in_public_openapi() -> None:
    for client in (crm_client(), support_client()):
        assert "__admin" not in str(client.get("/openapi.json").json()["paths"])


def test_request_log_records_auth_header_kind_status_and_never_values() -> None:
    client = crm_client()
    client.get("/customers", headers={"X-API-Key": CRM_KEY}, params={"page": 2})
    client.get("/customers", headers={"X-API-Key": "wrong-secret"})
    client.get("/customers")
    client.get("/customers", headers={"Authorization": "Bearer other"})
    log = client.get("/__admin/requests", headers=ADMIN).json()
    assert [(e["status"], e["auth_header"]) for e in log] == [
        (200, "x-api-key"),
        (401, "x-api-key"),
        (401, "none"),
        (401, "authorization"),
    ]
    assert log[0]["query"] == "page=2" and log[0]["method"] == "GET"
    assert "wrong-secret" not in str(log) and "other" not in str(log)
    assert all("__admin" not in e["path"] for e in log)


def test_request_log_sees_injected_faults_and_clears() -> None:
    client = support_client()
    client.put("/__admin/faults", json={"http_500_rate": 1.0}, headers=ADMIN)
    client.get("/users/1", headers={"Authorization": f"Bearer {TOKEN}"})
    assert [e["status"] for e in client.get("/__admin/requests", headers=ADMIN).json()] == [500]
    client.delete("/__admin/requests", headers=ADMIN)
    assert client.get("/__admin/requests", headers=ADMIN).json() == []
    client.get("/users/1", headers={"Authorization": f"Bearer {TOKEN}"})
    client.post("/__admin/reset", headers=ADMIN)
    assert client.get("/__admin/requests", headers=ADMIN).json() == []
