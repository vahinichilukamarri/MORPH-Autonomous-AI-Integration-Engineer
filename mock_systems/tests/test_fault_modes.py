"""Deterministic fault modes, response rewriting and the fault counters."""

from typing import Any

from fastapi.testclient import TestClient

from crm.main import create_app

ADMIN = {"X-Admin-Token": "a"}
KEY = {"X-API-Key": "k"}


def client() -> TestClient:
    return TestClient(create_app(api_key="k", admin_token="a"))


def faults(c: TestClient, **profile: Any) -> None:
    assert c.put("/__admin/faults", json=profile, headers=ADMIN).status_code == 200


def test_the_first_n_requests_fail_and_the_rest_succeed() -> None:
    c = client()
    faults(c, http_500_first_n=2)
    statuses = [c.get("/customers", headers=KEY).status_code for _ in range(4)]
    assert statuses == [500, 500, 200, 200]
    counters = c.get("/__admin/counters", headers=ADMIN).json()
    assert counters["http_500"] == 2 and counters["requests"] == 4 and counters["in_flight"] == 0


def test_malformed_first_n_is_counted_even_though_the_status_is_200() -> None:
    c = client()
    faults(c, malformed_json_first_n=1)
    first = c.get("/customers", headers=KEY)
    assert first.status_code == 200
    try:
        first.json()
        broken = False
    except ValueError:
        broken = True
    assert broken and c.get("/customers", headers=KEY).json()["total"] == 50
    assert c.get("/__admin/counters", headers=ADMIN).json()["malformed_json"] == 1


def test_counters_reset_with_the_profile() -> None:
    c = client()
    faults(c, http_500_first_n=1)
    c.get("/customers", headers=KEY)
    faults(c, http_500_first_n=0)
    assert c.get("/__admin/counters", headers=ADMIN).json()["http_500"] == 0


def test_rewrite_records_changes_only_the_matching_object() -> None:
    c = client()
    items = c.get("/customers", headers=KEY, params={"page_size": 3}).json()["items"]
    target = items[0]["customer_id"]
    faults(
        c,
        rewrite_records=[
            {"match_key": "customer_id", "match_value": target, "set": {"status": "ARCHIVED"}}
        ],
    )
    changed = c.get("/customers", headers=KEY, params={"page_size": 3}).json()["items"]
    assert [i["status"] for i in changed][0] == "ARCHIVED"
    assert [i["status"] for i in changed][1:] == [i["status"] for i in items][1:]
    assert c.get(f"/customers/{target}", headers=KEY).json()["status"] == "ARCHIVED"


def test_duplicate_first_list_item_repeats_the_first_item() -> None:
    c = client()
    faults(c, duplicate_first_list_item=True)
    items = c.get("/customers", headers=KEY, params={"page_size": 3}).json()["items"]
    assert len(items) == 4 and items[0] == items[-1]
