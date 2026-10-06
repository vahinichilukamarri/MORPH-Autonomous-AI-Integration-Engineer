import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crm.main import create_app as create_crm_app
from support.main import create_app as create_support_app

ADMIN = {"X-Admin-Token": "test-admin"}
SUPPORT_USER: dict[str, Any] = {
    "userId": 1837,
    "fullName": "Asha Verma",
    "email_address": "asha.verma@example.com",
    "phoneNumber": "919876543210",
    "accountState": "ENABLED",
    "tier": "STANDARD",
    "createdAt": 1709633730,
}
CRM_CUSTOMER: dict[str, Any] = {
    "first_name": "Asha",
    "email": "asha@example.com",
    "phone": "+919876543210",
    "segment": "SMB",
}


@dataclass
class System:
    name: str
    client: TestClient
    read_path: str
    create_path: str
    create_body: dict[str, Any]
    droppable: str  # a response field present on success
    v1_field: str
    v2_field: str
    schema_name: str
    reset_check: Callable[[TestClient], bool]

    def read(self) -> Any:
        return self.client.get(self.read_path)

    def configure(self, **profile: Any) -> None:
        response = self.client.put("/__admin/faults", json=profile, headers=ADMIN)
        assert response.status_code == 200, response.text


def _crm() -> System:
    client = TestClient(
        create_crm_app(api_key="k", admin_token="test-admin"), headers={"X-API-Key": "k"}
    )
    return System(
        "crm",
        client,
        "/customers/C-1654",
        "/customers",
        CRM_CUSTOMER,
        "email",
        "phone",
        "phone_number",
        "Customer",
        lambda c: c.get("/customers").json()["total"] == 50,
    )


def _support() -> System:
    client = TestClient(
        create_support_app(token="t", admin_token="test-admin"),
        headers={"Authorization": "Bearer t"},
    )
    assert client.post("/users", json=SUPPORT_USER).status_code == 201
    return System(
        "support",
        client,
        "/users/1837",
        "/users",
        {**SUPPORT_USER, "userId": 2},
        "email_address",
        "tier",
        "serviceTier",
        "User",
        lambda c: c.get("/users/1837").status_code == 404,
    )


@pytest.fixture(params=["crm", "support"])
def system(request: pytest.FixtureRequest) -> System:
    return _crm() if request.param == "crm" else _support()


def statuses(system: System, count: int) -> list[int]:
    return [system.read().status_code for _ in range(count)]


def test_admin_requires_its_own_token(system: System) -> None:
    # the system's own credentials (sent by default) do not open the admin API
    assert system.client.get("/__admin/faults").status_code == 401
    wrong = {"X-Admin-Token": "nope"}
    assert system.client.get("/__admin/faults", headers=wrong).status_code == 401
    assert system.client.get("/__admin/faults", headers=ADMIN).status_code == 200


def test_admin_api_is_not_in_public_openapi(system: System) -> None:
    paths = system.client.get("/openapi.json").json()["paths"]
    assert not any(p.startswith("/__admin") for p in paths)


def test_default_profile_is_clean(system: System) -> None:
    body = system.client.get("/__admin/faults", headers=ADMIN).json()
    assert body["contract_version"] == "v1"
    assert statuses(system, 5) == [200] * 5


def test_http_500(system: System) -> None:
    system.configure(http_500_rate=1.0)
    response = system.read()
    assert response.status_code == 500
    assert "Injected" in response.text


def test_http_429_with_retry_after(system: System) -> None:
    system.configure(http_429_rate=1.0, retry_after_seconds=7)
    response = system.read()
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"


def test_fixed_latency(system: System) -> None:
    system.configure(latency_ms=250)
    start = time.perf_counter()
    assert system.read().status_code == 200
    assert time.perf_counter() - start >= 0.25


def test_timeout_holds_the_request(system: System) -> None:
    system.configure(timeout=True, timeout_seconds=0.3)
    start = time.perf_counter()
    assert system.read().status_code == 504
    assert time.perf_counter() - start >= 0.3


def test_malformed_json(system: System) -> None:
    system.configure(malformed_json_rate=1.0)
    response = system.read()
    assert response.status_code == 200
    with pytest.raises(ValueError):
        response.json()


def test_drop_field_from_responses(system: System) -> None:
    assert system.droppable in system.read().json()
    system.configure(drop_fields=[system.droppable])
    body = system.read().json()
    assert system.droppable not in body
    assert len(body) > 3


def test_drop_field_applies_inside_lists() -> None:
    crm = _crm()
    crm.configure(drop_fields=["email"])
    items = crm.client.get("/customers").json()["items"]
    assert items
    assert all("email" not in item for item in items)


def test_faults_are_deterministic_for_a_seed(system: System) -> None:
    system.configure(seed=7, http_500_rate=0.5)
    first = statuses(system, 40)
    system.configure(seed=7, http_500_rate=0.5)
    assert statuses(system, 40) == first
    assert 500 in first and 200 in first
    system.configure(seed=8, http_500_rate=0.5)
    assert statuses(system, 40) != first


def test_mixed_rates_share_one_draw(system: System) -> None:
    system.configure(seed=3, http_500_rate=0.3, http_429_rate=0.3)
    seen = set(statuses(system, 60))
    assert seen == {200, 429, 500}


def test_profile_validation(system: System) -> None:
    too_much = {"http_500_rate": 0.7, "http_429_rate": 0.7}
    assert system.client.put("/__admin/faults", json=too_much, headers=ADMIN).status_code == 422
    unknown = {"nonsense": 1}
    assert system.client.put("/__admin/faults", json=unknown, headers=ADMIN).status_code == 422


def test_openapi_and_health_are_exempt_from_faults(system: System) -> None:
    system.configure(http_500_rate=1.0)
    assert system.client.get("/openapi.json").status_code == 200


def test_contract_v2_renames_fields_in_responses(system: System) -> None:
    system.configure(contract_version="v2")
    body = system.read().json()
    assert system.v2_field in body
    assert system.v1_field not in body


def test_contract_v2_renames_fields_in_requests(system: System) -> None:
    system.configure(contract_version="v2")
    v1_name_body = dict(system.create_body)
    v2_name_body = {
        (system.v2_field if k == system.v1_field else k): v for k, v in v1_name_body.items()
    }
    assert system.client.post(system.create_path, json=v2_name_body).status_code == 201
    # old field name is no longer part of the contract
    old = system.client.post(system.create_path, json={**v1_name_body, "userId": 3})
    assert old.status_code == 422
    assert system.v2_field in old.text


def test_contract_v2_changes_openapi(system: System) -> None:
    v1 = system.client.get("/openapi.json").json()
    system.configure(contract_version="v2")
    v2 = system.client.get("/openapi.json").json()
    v1_props = v1["components"]["schemas"][system.schema_name]["properties"]
    v2_props = v2["components"]["schemas"][system.schema_name]["properties"]
    assert system.v1_field in v1_props and system.v2_field not in v1_props
    assert system.v2_field in v2_props and system.v1_field not in v2_props
    assert v2["info"]["version"] == "2.0.0"
    system.client.delete("/__admin/faults", headers=ADMIN)
    assert system.client.get("/openapi.json").json() == v1


def test_reset_restores_clean_state(system: System) -> None:
    system.configure(http_500_rate=1.0, contract_version="v2")
    system.client.post("/__admin/reset", headers=ADMIN)
    profile = system.client.get("/__admin/faults", headers=ADMIN).json()
    assert profile["http_500_rate"] == 0
    assert profile["contract_version"] == "v1"
    assert system.reset_check(system.client)
