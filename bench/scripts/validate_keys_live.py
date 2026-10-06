"""Check that the answer keys are consistent with the live mock APIs. Run from ``bench/``:

    uv run python -m scripts.validate_keys_live

The mock systems must be running (``./scripts/dev.ps1 up``). Nothing here edits a key; every
mismatch is reported.

* S1, S2, S4 (CRM -> Support): run each reference pipeline over every sample CRM record, POST the
  result to the live Support API (fresh state, no faults) and require HTTP 201, then read it back.
* S3 (Support -> CRM): the CRM API assigns customer_id and created_at itself, so those two fields
  cannot be POSTed. Each produced field is validated against the live CRM schema instead, and
  the exact phone and created_at formats are compared with what the live CRM returns for its own
  seeded customers. The creatable fields are also POSTed (with a test-supplied segment, because
  no source value exists for it).
"""

import os
import re
import sys
from typing import Any

import httpx
import yaml
from app.mapping.transform import JsonScalar, Transformation, TransformError, execute
from jsonschema import Draft202012Validator

from morph_bench.loader import SCENARIOS_ROOT, discover
from morph_bench.manifest import BENCH_DIR
from morph_bench.models import Bundle

SAMPLES = BENCH_DIR.parent / "mock_systems" / "samples"
CRM = os.environ.get("CRM_URL", "http://localhost:8101")
SUPPORT = os.environ.get("SUPPORT_URL", "http://localhost:8102")
CRM_HEADERS = {"X-API-Key": os.environ.get("CRM_API_KEY", "crm-dev-key")}
SUPPORT_HEADERS = {
    "Authorization": f"Bearer {os.environ.get('SUPPORT_TOKEN', 'support-dev-token')}"
}
ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_TOKEN", "admin-dev-token")}
TEST_SEGMENT = "SMB"  # test-only value for the one CRM field the key says cannot be derived

problems: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        problems.append(message)


def pipelines(scenario_id: str) -> dict[str, Transformation]:
    path = BENCH_DIR / "references" / f"{scenario_id}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["pipelines"]
    return {name: Transformation.model_validate(body) for name, body in raw.items()}


def records(system: str, entity: str, contract: str) -> list[dict[str, JsonScalar]]:
    import json

    name = f"{system}_{entity.lower()}.{contract}.json"
    data = json.loads((SAMPLES / name).read_text(encoding="utf-8"))
    out: list[dict[str, JsonScalar]] = data["records"]
    return out


def reset(client: httpx.Client, base: str, contract: str = "v1") -> None:
    client.post(f"{base}/__admin/reset", headers=ADMIN).raise_for_status()
    if contract != "v1":
        client.put(
            f"{base}/__admin/faults", headers=ADMIN, json={"contract_version": contract}
        ).raise_for_status()
    faults = client.get(f"{base}/__admin/faults", headers=ADMIN).json()
    clean = {k: v for k, v in faults.items() if k != "contract_version"}
    check(
        clean["http_500_rate"] == 0 and clean["http_429_rate"] == 0 and not clean["timeout"],
        f"{base}: faults are not clean after reset: {faults}",
    )


def to_support(bundle: Bundle, client: httpx.Client) -> None:
    sid = bundle.scenario.id
    contract = bundle.scenario.target.contract
    source = records("crm", "customer", bundle.scenario.source.contract)
    pipes = pipelines(sid)
    reset(client, SUPPORT, contract)
    created = 0
    for record in source:
        body: dict[str, JsonScalar] = {}
        try:
            for field, transformation in pipes.items():
                body[field] = execute(transformation, record)
        except TransformError as exc:
            problems.append(f"{sid}: {record['customer_id']}: pipeline error {exc}")
            continue
        response = client.post(f"{SUPPORT}/users", json=body, headers=SUPPORT_HEADERS)
        if response.status_code != 201:
            problems.append(
                f"{sid}: {record['customer_id']}: POST /users -> {response.status_code} "
                f"{response.text[:300]}"
            )
            continue
        created += 1
        again = client.get(f"{SUPPORT}/users/{body['userId']}", headers=SUPPORT_HEADERS)
        check(
            again.status_code == 200 and again.json() == body,
            f"{sid}: read-back differs for {body}",
        )
    print(f"{sid}: {created}/{len(source)} users accepted by live Support ({contract}), 201 each")
    reset(client, SUPPORT)


def customer_validators(client: httpx.Client) -> dict[str, Draft202012Validator]:
    spec = client.get(f"{CRM}/openapi.json").json()
    props = spec["components"]["schemas"]["Customer"]["properties"]
    return {
        name: Draft202012Validator({**schema, "components": spec["components"]})
        for name, schema in props.items()
    }


def to_crm(bundle: Bundle, client: httpx.Client) -> None:
    sid = bundle.scenario.id
    reset(client, CRM)
    validators = customer_validators(client)
    live = client.get(f"{CRM}/customers", params={"page_size": 100}, headers=CRM_HEADERS).json()
    live_items: list[dict[str, Any]] = live["items"]
    iso_z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    e164 = re.compile(r"^\+[1-9]\d{7,14}$")
    check(
        all(iso_z.fullmatch(c["created_at"]) for c in live_items),
        "live CRM created_at values do not all match the ISO-Z pattern used for comparison",
    )
    check(
        all(c["phone"] is None or e164.fullmatch(c["phone"]) for c in live_items),
        "live CRM phone values do not all match E.164",
    )
    print(
        f"{sid}: live CRM returns {len(live_items)} customers; created_at is ISO-8601 with Z, "
        "phone is E.164 (checked on all of them)"
    )

    pipes = pipelines(sid)
    users = records("support", "user", bundle.scenario.source.contract)
    not_syncable: list[Any] = []
    checked_fields = 0
    posted = 0
    for user in users:
        out: dict[str, JsonScalar] = {}
        try:
            for field, transformation in pipes.items():
                out[field] = execute(transformation, user)
        except TransformError as exc:
            problems.append(f"{sid}: user {user['userId']}: pipeline error {exc}")
            continue
        for field, value in out.items():
            if value is None and field == "customer_id":
                not_syncable.append(user["userId"])  # expected: flagged, never invented
                continue
            checked_fields += 1
            errors = list(validators[field].iter_errors(value))
            if errors:
                problems.append(
                    f"{sid}: user {user['userId']}: {field}={value!r} violates the live CRM "
                    f"schema: {errors[0].message}"
                )
        created_at = out["created_at"]
        check(
            isinstance(created_at, str) and iso_z.fullmatch(created_at) is not None,
            f"{sid}: user {user['userId']}: created_at {created_at!r} is not ISO-8601 with Z",
        )
        phone = out["phone"]
        check(
            phone is None or (isinstance(phone, str) and e164.fullmatch(phone) is not None),
            f"{sid}: user {user['userId']}: phone {phone!r} is not E.164",
        )
        creatable = {k: v for k, v in out.items() if k not in ("customer_id", "created_at")} | {
            "segment": TEST_SEGMENT
        }
        response = client.post(f"{CRM}/customers", json=creatable, headers=CRM_HEADERS)
        if response.status_code == 201:
            posted += 1
        else:
            problems.append(
                f"{sid}: user {user['userId']}: POST /customers -> {response.status_code} "
                f"{response.text[:300]}"
            )
    print(
        f"{sid}: {len(users)} support users; {checked_fields} produced fields validated against "
        f"the live Customer schema; {posted}/{len(users)} accepted by POST /customers "
        f"(creatable fields plus a test-supplied segment={TEST_SEGMENT!r})"
    )
    print(
        f"{sid}: customer_id is null (not syncable, to be flagged) for users {not_syncable}; "
        "customer_id and created_at are server-assigned by the CRM, so they cannot be POSTed"
    )
    extra = client.post(
        f"{CRM}/customers",
        json={
            **{k: v for k, v in creatable.items()},
            "customer_id": "C-1",
            "created_at": "2024-01-01T00:00:00Z",
        },
        headers=CRM_HEADERS,
    )
    print(
        f"{sid}: POST with customer_id and created_at -> {extra.status_code} (the API forbids them)"
    )
    reset(client, CRM)


def main() -> int:
    bundles = {b.scenario.id: b for b in discover(SCENARIOS_ROOT)}
    with httpx.Client(timeout=15) as client:
        for sid in (
            "crm_customer_to_support_user",
            "crm_customer_to_support_user_nodocs",
            "crm_v2_to_support_v2",
        ):
            to_support(bundles[sid], client)
        to_crm(bundles["support_user_to_crm_customer"], client)
    if problems:
        print(f"\n{len(problems)} MISMATCH(ES):")
        for line in problems:
            print(f"- {line}")
        return 1
    print("\nno mismatches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
