"""Sync engine tests against small in-memory stand-ins for a CRM-like and a Support-like API."""

import json
import random
import re
from collections.abc import Callable, Mapping
from typing import Any

from morph_runtime.auth import ApiKeyAuth, BearerAuth
from morph_runtime.errors import Category
from morph_runtime.http import HttpClient, Response
from morph_runtime.ops import FieldTransformError, JsonScalar, MissingFieldError, Record
from morph_runtime.report import Outcome
from morph_runtime.retry import RetryPolicy
from morph_runtime.sync import (
    SourceMode,
    SourceSpec,
    Strategy,
    SyncEngine,
    TargetMode,
    TargetSpec,
)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def ok(payload: Any, status: int = 200) -> Response:
    return Response(status, {}, json.dumps(payload).encode())


class FakeCrm:
    """GET /customers (paged), GET/PATCH /customers/{id}, POST /customers (server ids)."""

    def __init__(self, rows: list[dict[str, Any]], key: str = "k") -> None:
        self.rows = {r["customer_id"]: r for r in rows}
        self.next_id = 5000
        self.log: list[tuple[str, str]] = []
        self.key = key
        self.fail_posts: list[int] = []  # statuses to answer the next POSTs with (after storing)

    def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response:
        path = re.sub(r"^https?://[^/]+", "", url)
        self.log.append((method, path))
        if headers.get("X-API-Key") != self.key:
            return ok({"detail": "bad key"}, 401)
        data: dict[str, Any] = json.loads(body) if body else {}
        if method == "GET" and path.startswith("/customers?"):
            query = dict(p.split("=") for p in path.split("?")[1].split("&"))
            page, size = int(query["page"]), int(query["page_size"])
            items = list(self.rows.values())
            return ok({"items": items[(page - 1) * size : page * size], "total": len(items)})
        match = re.fullmatch(r"/customers/([^/]+)", path)
        if method == "GET" and match:
            row = self.rows.get(match[1])
            return ok(row) if row else ok({"detail": "nf"}, 404)
        if method == "PATCH" and match:
            row = self.rows.get(match[1])
            if row is None:
                return ok({"detail": "nf"}, 404)
            row.update(data)
            return ok(row)
        if method == "POST" and path == "/customers":
            if "segment" not in data:
                return ok({"detail": [{"loc": ["body", "segment"], "msg": "required"}]}, 422)
            row = {"customer_id": f"C-{self.next_id}", "created_at": "2024-01-01T00:00:00Z", **data}
            self.next_id += 1
            self.rows[row["customer_id"]] = row
            if self.fail_posts:
                return Response(self.fail_posts.pop(0), {}, b"")
            return ok(row, 201)
        return ok({}, 404)


class FakeSupport:
    """GET/PUT /users/{id} with upsert; no list operation."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, token: str = "t") -> None:
        self.rows = {r["userId"]: r for r in rows or []}
        self.log: list[tuple[str, str]] = []
        self.token = token

    def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response:
        path = re.sub(r"^https?://[^/]+", "", url)
        self.log.append((method, path))
        if headers.get("Authorization") != f"Bearer {self.token}":
            return ok({"error": {"code": "UNAUTHORIZED"}}, 401)
        match = re.fullmatch(r"/users/(\d+)", path)
        assert match
        user_id = int(match[1])
        if method == "GET":
            return ok(self.rows[user_id]) if user_id in self.rows else ok({}, 404)
        data = json.loads(body or b"{}")
        if data.get("tier") not in (None, "STANDARD", "PRIORITY"):
            return ok({"error": {"details": [{"field": "tier"}]}}, 422)
        created = user_id not in self.rows
        self.rows[user_id] = data
        return ok(data, 201 if created else 200)


def client(base: str, auth: Any, transport: Any, clock: Clock) -> HttpClient:
    return HttpClient(
        base, auth, transport=transport, clock=clock, policy=RetryPolicy(), rng=random.Random(1)
    )


CRM_SOURCE = SourceSpec(
    mode=SourceMode.LIST, key_field="customer_id", list_path="/customers", total_key="total",
    page_size=2,
)  # fmt: skip
SUPPORT_TARGET = TargetSpec(
    mode=TargetMode.UPSERT, id_field="userId", get_path="/users/{id}", update_method="PUT",
    update_path="/users/{id}", update_fields=("userId", "fullName", "tier"),
    create_fields=("userId", "fullName", "tier"),
)  # fmt: skip


def to_support(record: Record) -> dict[str, JsonScalar]:
    if "customer_id" not in record:
        raise FieldTransformError("userId", "x") from MissingFieldError("customer_id")
    cid = record["customer_id"]
    if not isinstance(cid, str):
        raise FieldTransformError("userId", "not a string")
    number = cid.removeprefix("C-")
    return {
        "userId": int(number) if number.isdigit() else None,
        "fullName": f"{record.get('first_name')}",
        "tier": "PRIORITY" if record.get("segment") == "ENTERPRISE" else "STANDARD",
    }


def crm_rows(n: int) -> list[dict[str, Any]]:
    return [
        {
            "customer_id": f"C-{1000 + i}",
            "first_name": f"N{i}",
            "segment": "ENTERPRISE" if i % 2 else "SMB",
        }
        for i in range(n)
    ]


def run_crm_to_support(
    crm: FakeCrm, support: FakeSupport, strategy: Strategy | None = None,
    to_target: Callable[[Record], dict[str, JsonScalar]] = to_support,
) -> Any:  # fmt: skip
    clock = Clock()
    engine = SyncEngine(
        strategy or Strategy(CRM_SOURCE, SUPPORT_TARGET),
        client("http://crm", ApiKeyAuth("X-API-Key", "k"), crm, clock),
        client("http://support", BearerAuth("t"), support, clock),
        to_target,
        clock=clock,
    )
    return engine.run()


def test_upsert_creates_then_second_run_issues_no_writes() -> None:
    crm, support = FakeCrm(crm_rows(5)), FakeSupport()
    first = run_crm_to_support(crm, support)
    assert first.counts()["CREATED"] == 5 and first.exit_code == 0
    assert len(support.rows) == 5
    writes_before = [m for m, _ in support.log if m == "PUT"]
    second = run_crm_to_support(crm, support)
    assert second.counts()["UNCHANGED"] == 5
    assert [m for m, _ in support.log if m == "PUT"] == writes_before  # no new writes


def test_changed_source_record_updates_in_place() -> None:
    crm, support = FakeCrm(crm_rows(3)), FakeSupport()
    run_crm_to_support(crm, support)
    crm.rows["C-1001"]["first_name"] = "Renamed"
    report = run_crm_to_support(crm, support)
    assert report.counts() == {
        "CREATED": 0, "UPDATED": 1, "UNCHANGED": 2, "NOT_SYNCABLE": 0, "FAILED": 0,
    }  # fmt: skip
    assert support.rows[1001]["fullName"] == "Renamed" and len(support.rows) == 3


def test_pagination_boundaries_sync_every_record_once() -> None:
    for n in (0, 1, 2, 3, 4, 7):
        crm, support = FakeCrm(crm_rows(n)), FakeSupport()
        report = run_crm_to_support(crm, support)
        assert report.counts()["CREATED"] == n and len(support.rows) == n


def test_null_identity_is_not_syncable_and_does_not_stop_the_run() -> None:
    rows = crm_rows(2) + [{"customer_id": "X-bad", "first_name": "N", "segment": "SMB"}]
    report = run_crm_to_support(FakeCrm(rows), FakeSupport())
    assert report.counts()["CREATED"] == 2 and report.counts()["NOT_SYNCABLE"] == 1
    assert report.exit_code == 0


def test_target_validation_error_fails_one_record_and_names_the_field() -> None:
    def bad_tier(record: Record) -> dict[str, JsonScalar]:
        mapped = to_support(record)
        if record["customer_id"] == "C-1001":
            mapped["tier"] = "GOLD"
        return mapped

    report = run_crm_to_support(FakeCrm(crm_rows(3)), FakeSupport(), to_target=bad_tier)
    failed = [r for r in report.records if r.outcome is Outcome.FAILED]
    assert len(failed) == 1 and failed[0].category is Category.VALIDATION
    assert failed[0].fields == ("tier",) and report.counts()["CREATED"] == 2
    assert report.status.value == "PARTIAL" and report.exit_code == 3


def test_target_auth_failure_is_fatal_after_one_attempt() -> None:
    crm, support = FakeCrm(crm_rows(5)), FakeSupport(token="other")
    report = run_crm_to_support(crm, support)
    assert report.status.value == "FATAL" and report.fatal_category is Category.AUTH
    assert report.requests["target"] == 1 and support.rows == {}


def test_source_auth_failure_is_fatal_and_nothing_is_written() -> None:
    crm, support = FakeCrm(crm_rows(5), key="other"), FakeSupport()
    report = run_crm_to_support(crm, support)
    assert report.fatal_category is Category.AUTH and report.requests["source"] == 1
    assert support.log == []


def test_missing_source_field_is_contract_drift_with_no_writes() -> None:
    rows = [{"cust": "C-1", "first_name": "A"}]
    report = run_crm_to_support(FakeCrm([{**r, "customer_id": "C-1"} for r in rows]), FakeSupport())
    assert report.counts()["CREATED"] == 1  # control: field present

    class Drifted(FakeCrm):
        def send(self, *args: Any) -> Response:
            response = super().send(*args)
            return Response(
                response.status, response.headers, response.body.replace(b"customer_id", b"cid")
            )

    support = FakeSupport()
    report = run_crm_to_support(Drifted(crm_rows(3)), support)
    assert report.fatal_category is Category.CONTRACT_DRIFT
    assert [m for m, _ in support.log if m == "PUT"] == []


def test_keys_mode_updates_by_mapped_id_and_never_invents_one() -> None:
    support_rows: list[dict[str, Any]] = [
        {"userId": 1, "externalRef": None, "fullName": "A"},
        {"userId": 2, "externalRef": "C-1", "fullName": "B"},
        {"userId": 3, "externalRef": "C-404", "fullName": "C"},
    ]
    source = SourceSpec(mode=SourceMode.KEYS, key_field="userId", get_path="/users/{id}")
    target = TargetSpec(
        mode=TargetMode.CREATE_UPDATE, id_field="customer_id", get_path="/customers/{id}",
        update_method="PATCH", update_path="/customers/{id}", create_path="/customers",
        create_fields=("first_name", "segment"), update_fields=("first_name",),
        create_only=("segment",), natural_key="first_name", id_assigned_by_target=True,
        list_path="/customers", total_key="total",
    )  # fmt: skip

    def to_crm(record: Record) -> dict[str, JsonScalar]:
        return {
            "customer_id": record["externalRef"],
            "first_name": record["fullName"],
            "segment": "SMB",
        }

    crm = FakeCrm([{"customer_id": "C-1", "first_name": "Old", "segment": "ENTERPRISE"}])
    support = FakeSupport(support_rows)
    clock = Clock()
    engine = SyncEngine(
        Strategy(source, target),
        client("http://support", BearerAuth("t"), support, clock),
        client("http://crm", ApiKeyAuth("X-API-Key", "k"), crm, clock),
        to_crm,
        keys=("1", "2", "3", "99"),
        clock=clock,
    )
    report = engine.run()
    by_key = {r.key: r for r in report.records}
    assert by_key["1"].outcome is Outcome.NOT_SYNCABLE  # null id: never fabricated
    assert by_key["2"].outcome is Outcome.UPDATED  # found by its externalRef
    assert by_key["3"].outcome is Outcome.NOT_SYNCABLE  # the target cannot create with that id
    assert by_key["99"].outcome is Outcome.FAILED and by_key["99"].category is Category.NOT_FOUND
    assert crm.rows["C-1"]["segment"] == "ENTERPRISE"  # create-only constants never overwrite
    assert crm.rows["C-1"]["first_name"] == "B" and len(crm.rows) == 1
    assert [m for m, _ in crm.log if m == "POST"] == []


def test_without_a_mapped_identity_the_natural_key_creates_once() -> None:
    source = SourceSpec(mode=SourceMode.KEYS, key_field="userId", get_path="/users/{id}")
    target = TargetSpec(
        mode=TargetMode.CREATE_UPDATE, id_field="customer_id", get_path="/customers/{id}",
        update_method="PATCH", update_path="/customers/{id}", create_path="/customers",
        create_fields=("first_name", "segment"), update_fields=("first_name",),
        create_only=("segment",), natural_key="first_name", id_assigned_by_target=True,
        list_path="/customers", total_key="total",
    )  # fmt: skip

    def to_crm(record: Record) -> dict[str, JsonScalar]:
        return {"first_name": record["fullName"], "segment": "SMB"}

    crm = FakeCrm([])
    support = FakeSupport([{"userId": 1, "externalRef": None, "fullName": "Zed"}])
    clock = Clock()

    def run_once() -> Any:
        return SyncEngine(
            Strategy(source, target),
            client("http://support", BearerAuth("t"), support, clock),
            client("http://crm", ApiKeyAuth("X-API-Key", "k"), crm, clock),
            to_crm,
            keys=("1",),
            clock=clock,
        ).run()

    assert run_once().counts()["CREATED"] == 1
    assert run_once().counts()["UNCHANGED"] == 1 and len(crm.rows) == 1


def test_ambiguous_create_failure_does_not_duplicate() -> None:
    source = SourceSpec(mode=SourceMode.KEYS, key_field="userId", get_path="/users/{id}")
    target = TargetSpec(
        mode=TargetMode.CREATE_UPDATE, id_field="customer_id", get_path="/customers/{id}",
        update_method="PATCH", update_path="/customers/{id}", create_path="/customers",
        create_fields=("first_name", "segment"), update_fields=("first_name",),
        create_only=("segment",), natural_key="first_name", id_assigned_by_target=True,
        list_path="/customers", total_key="total",
    )  # fmt: skip

    def to_crm(record: Record) -> dict[str, JsonScalar]:
        return {"first_name": record["fullName"], "segment": "SMB"}

    crm = FakeCrm([])
    crm.fail_posts = [500]  # stored, then answered with a 500
    support = FakeSupport([{"userId": 1, "externalRef": None, "fullName": "Zed"}])
    clock = Clock()
    report = SyncEngine(
        Strategy(source, target),
        client("http://support", BearerAuth("t"), support, clock),
        client("http://crm", ApiKeyAuth("X-API-Key", "k"), crm, clock),
        to_crm,
        keys=("1",),
        clock=clock,
    ).run()
    assert report.counts()["CREATED"] == 1 and len(crm.rows) == 1
    assert [m for m, p in crm.log if m == "POST"] == ["POST"]


def test_consecutive_server_failures_end_the_run_cleanly() -> None:
    class Down(FakeSupport):
        def send(self, method: str, url: str, *rest: Any) -> Response:
            return Response(503, {}, b"")

    report = run_crm_to_support(FakeCrm(crm_rows(30)), Down())
    assert report.status.value == "FATAL" and report.fatal_category is Category.SERVER
    assert len(report.records) < 30
