import re

from fastapi.testclient import TestClient

from crm.main import create_app
from crm.store import SEED_COUNT

NEW_CUSTOMER = {
    "first_name": "Test",
    "last_name": "User",
    "email": "test.user@example.com",
    "phone": "+919876543210",
    "segment": "SMB",
}


def test_requires_api_key() -> None:
    client = TestClient(create_app(api_key="k"))
    assert client.get("/customers").status_code == 401
    assert client.get("/customers", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/customers", headers={"X-API-Key": "k"}).status_code == 200


def test_list_is_paginated(crm: TestClient) -> None:
    first = crm.get("/customers", params={"page": 1, "page_size": 20}).json()
    assert first["total"] == SEED_COUNT
    assert first["page"] == 1
    assert len(first["items"]) == 20
    last = crm.get("/customers", params={"page": 3, "page_size": 20}).json()
    assert len(last["items"]) == SEED_COUNT - 40
    ids = [
        c["customer_id"]
        for p in (1, 2, 3)
        for c in crm.get("/customers", params={"page": p, "page_size": 20}).json()["items"]
    ]
    assert len(set(ids)) == SEED_COUNT


def test_list_rejects_bad_pagination(crm: TestClient) -> None:
    assert crm.get("/customers", params={"page": 0}).status_code == 422
    assert crm.get("/customers", params={"page_size": 101}).status_code == 422


def test_seed_is_deterministic_and_well_formed(crm: TestClient) -> None:
    other = TestClient(create_app(api_key="x"), headers={"X-API-Key": "x"})
    params = {"page_size": 100}
    assert (
        crm.get("/customers", params=params).json() == other.get("/customers", params=params).json()
    )
    for c in crm.get("/customers", params=params).json()["items"]:
        assert re.fullmatch(r"C-\d+", c["customer_id"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", c["created_at"])
        assert c["phone"] is None or re.fullmatch(r"\+[1-9]\d{7,14}", c["phone"])


def test_seed_contains_null_last_name_and_phone(crm: TestClient) -> None:
    items = crm.get("/customers", params={"page_size": 100}).json()["items"]
    assert any(c["last_name"] is None for c in items)
    assert any(c["phone"] is None for c in items)


def test_get_customer_and_404(crm: TestClient) -> None:
    first = crm.get("/customers").json()["items"][0]
    assert crm.get(f"/customers/{first['customer_id']}").json() == first
    assert crm.get("/customers/C-1").status_code == 404


def test_create_customer(crm: TestClient) -> None:
    response = crm.post("/customers", json=NEW_CUSTOMER)
    assert response.status_code == 201
    body = response.json()
    assert re.fullmatch(r"C-\d+", body["customer_id"])
    assert body["status"] == "ACTIVE"
    assert crm.get(f"/customers/{body['customer_id']}").json() == body


def test_create_validation_errors(crm: TestClient) -> None:
    assert crm.post("/customers", json={**NEW_CUSTOMER, "email": "nope"}).status_code == 422
    assert crm.post("/customers", json={**NEW_CUSTOMER, "phone": "9876543210"}).status_code == 422
    assert crm.post("/customers", json={**NEW_CUSTOMER, "segment": "HUGE"}).status_code == 422
    missing = {k: v for k, v in NEW_CUSTOMER.items() if k != "first_name"}
    assert crm.post("/customers", json=missing).status_code == 422
    assert crm.post("/customers", json={**NEW_CUSTOMER, "extra": 1}).status_code == 422


def test_patch_updates_only_given_fields(crm: TestClient) -> None:
    created = crm.post("/customers", json=NEW_CUSTOMER).json()
    patched = crm.patch(f"/customers/{created['customer_id']}", json={"status": "SUSPENDED"})
    assert patched.status_code == 200
    assert patched.json() == {**created, "status": "SUSPENDED"}


def test_patch_can_null_optional_fields_but_not_required_ones(crm: TestClient) -> None:
    created = crm.post("/customers", json=NEW_CUSTOMER).json()
    url = f"/customers/{created['customer_id']}"
    cleared = crm.patch(url, json={"last_name": None, "phone": None}).json()
    assert cleared["last_name"] is None
    assert cleared["phone"] is None
    assert crm.patch(url, json={"first_name": None}).status_code == 422


def test_patch_missing_customer(crm: TestClient) -> None:
    assert crm.patch("/customers/C-1", json={"status": "ACTIVE"}).status_code == 404
