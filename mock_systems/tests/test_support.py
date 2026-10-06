from typing import Any

from fastapi.testclient import TestClient

from support.main import create_app

USER: dict[str, Any] = {
    "userId": 1837,
    "externalRef": "C-1837",
    "fullName": "Asha Verma",
    "email_address": "asha.verma@example.com",
    "phoneNumber": "919876543210",
    "accountState": "ENABLED",
    "tier": "STANDARD",
    "createdAt": 1709633730,
}


def detail_for(response_json: dict[str, Any], field: str) -> dict[str, Any]:
    matches = [d for d in response_json["error"]["details"] if d["field"] == field]
    assert matches, f"no error detail for {field}: {response_json}"
    detail: dict[str, Any] = matches[0]
    return detail


def test_requires_bearer_token() -> None:
    client = TestClient(create_app(token="t"))
    assert client.get("/users/1").status_code == 401
    assert client.get("/users/1", headers={"Authorization": "Bearer nope"}).status_code == 401
    body = client.get("/users/1").json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert client.get("/users/1", headers={"Authorization": "Bearer t"}).status_code == 404


def test_create_and_get(support: TestClient) -> None:
    created = support.post("/users", json=USER)
    assert created.status_code == 201
    assert created.json() == USER
    assert support.get("/users/1837").json() == USER


def test_optional_fields_default_to_null(support: TestClient) -> None:
    minimal = {k: v for k, v in USER.items() if k not in {"externalRef", "phoneNumber"}}
    body = support.post("/users", json=minimal).json()
    assert body["externalRef"] is None
    assert body["phoneNumber"] is None


def test_get_unknown_user(support: TestClient) -> None:
    response = support.get("/users/999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_duplicate_user_id_returns_409(support: TestClient) -> None:
    assert support.post("/users", json=USER).status_code == 201
    response = support.post("/users", json=USER)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DUPLICATE_USER"


def test_put_creates_then_replaces(support: TestClient) -> None:
    assert support.put("/users/1837", json=USER).status_code == 201
    changed = {**USER, "accountState": "BLOCKED"}
    response = support.put("/users/1837", json=changed)
    assert response.status_code == 200
    assert support.get("/users/1837").json()["accountState"] == "BLOCKED"


def test_put_rejects_path_body_mismatch(support: TestClient) -> None:
    response = support.put("/users/5", json=USER)
    assert response.status_code == 422
    detail = detail_for(response.json(), "userId")
    assert detail["received"] == 1837
    assert "5" in detail["expected"]


def test_422_names_field_expected_and_received(support: TestClient) -> None:
    response = support.post("/users", json={**USER, "phoneNumber": "+919876543210"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    detail = detail_for(body, "phoneNumber")
    assert detail["location"] == "body"
    assert detail["received"] == "+919876543210"
    assert "pattern" in detail["expected"]


def test_422_enum_lists_allowed_values(support: TestClient) -> None:
    detail = detail_for(support.post("/users", json={**USER, "tier": "SMB"}).json(), "tier")
    assert detail["received"] == "SMB"
    assert "STANDARD" in detail["expected"] and "PRIORITY" in detail["expected"]


def test_422_missing_required_field(support: TestClient) -> None:
    body = {k: v for k, v in USER.items() if k != "fullName"}
    detail = detail_for(support.post("/users", json=body).json(), "fullName")
    assert detail["received"] is None


def test_strict_types_reject_coercion(support: TestClient) -> None:
    assert support.post("/users", json={**USER, "userId": "1837"}).status_code == 422
    assert (
        support.post("/users", json={**USER, "createdAt": "2024-03-05T10:15:30Z"}).status_code
        == 422
    )
    assert support.post("/users", json={**USER, "createdAt": 1.5}).status_code == 422


def test_full_name_must_be_normalised(support: TestClient) -> None:
    for bad in (" Asha Verma", "Asha  Verma", "Asha Verma "):
        response = support.post("/users", json={**USER, "fullName": bad})
        assert response.status_code == 422, bad
        assert "single spaces" in detail_for(response.json(), "fullName")["expected"]


def test_unknown_fields_are_rejected(support: TestClient) -> None:
    response = support.post("/users", json={**USER, "segment": "SMB"})
    assert response.status_code == 422
    assert detail_for(response.json(), "segment")["received"] == "SMB"


def test_path_validation_error_has_same_shape(support: TestClient) -> None:
    response = support.get("/users/abc")
    assert response.status_code == 422
    detail = detail_for(response.json(), "userId")
    assert detail["location"] == "path"
    assert detail["received"] == "abc"
