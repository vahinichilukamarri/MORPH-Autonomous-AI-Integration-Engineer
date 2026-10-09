"""The REST shapes for the policy layer, and the approver token on the two routes that write."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.api.policy import get_audit_log, get_clock
from app.db import get_session
from app.db_models import System
from app.main import create_app
from app.policy import approvals
from app.policy.audit import AuditLog, EventType
from app.policy.clock import ManualClock
from app.settings import Settings, get_settings

TOKEN = "approver-token-for-tests-7a1c"
ARGS = {"source_system_version": 1, "target_system_version": 2, "source_entity": "A",
        "target_entity": "B"}  # fmt: skip


def build(session: Session, audit: AuditLog, clock: ManualClock, token: str | None) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_audit_log] = lambda: audit
    app.dependency_overrides[get_clock] = lambda: clock
    data = {"MORPH_APPROVER_TOKEN": token} if token else {}
    app.dependency_overrides[get_settings] = lambda: Settings.model_validate(data)
    return TestClient(app)


@pytest.fixture
def client(session: Session, audit: AuditLog, clock: ManualClock) -> Iterator[TestClient]:
    yield build(session, audit, clock, TOKEN)


AUTH = {"X-Approver-Token": TOKEN}


def pending(session: Session, audit: AuditLog, clock: ManualClock) -> str:
    row = approvals.create(
        session, audit, clock, session_id="s1", tool="propose_mapping", arguments=ARGS,
        policy_hash="p" * 64, summary={"ids": [1, 2]},
    )  # fmt: skip
    return row.id


def test_the_active_policy_is_described(client: TestClient) -> None:
    body = client.get("/policy/active").json()
    assert body["version"] == "v1" and len(body["hash"]) == 64
    assert [r["id"] for r in body["rules"]][:2] == ["P01-read-any-role", "P02-operator-ingest"]
    assert len(body["rules"]) == 7 and len(body["floor"]) == 10
    assert body["session_limits"]["max_model_calls"] == 12


def test_the_tool_catalogue_lists_all_tools_with_their_outcomes(client: TestClient) -> None:
    tools = client.get("/mcp/tools").json()
    assert len(tools) == 13
    by_name = {t["name"]: t for t in tools}
    assert by_name["get_system"]["side_effect"] is False
    outcomes = by_name["propose_mapping"]["outcomes_on_a_mock_target"]
    assert outcomes["operator / synthetic"] == "ALLOW"
    assert outcomes["operator / unclassified"] == "NEEDS_APPROVAL"
    assert outcomes["reader / synthetic"] == "DENY"
    generate = by_name["generate_integration"]["outcomes_on_a_mock_target"]
    assert generate["operator / internal / condition D"] == "ALLOW"
    assert generate["operator / internal / condition L1 or L2"] == "NEEDS_APPROVAL"
    assert "approval_id" in by_name["propose_mapping"]["input_schema"]["properties"]
    assert "approval_id" not in by_name["get_system"]["input_schema"]["properties"]


def test_audit_events_page_and_filter(client: TestClient, audit: AuditLog) -> None:
    for i in range(5):
        audit.append(EventType.CALL_RECEIVED, session_id="sa", principal="agent:operator",
                     tool="get_system", call_id=f"c{i}", payload={"i": i})  # fmt: skip
    audit.append(
        EventType.POLICY_DECISION, session_id="sb", principal="agent:reader", tool="ingest_contract"
    )
    first = client.get("/audit/events", params={"limit": 3}).json()
    assert len(first) == 3 and first[0]["event_type"] == "CALL_RECEIVED"
    rest = client.get("/audit/events", params={"after_seq": first[-1]["seq"]}).json()
    assert [e["seq"] for e in first + rest] == sorted(e["seq"] for e in first + rest)
    assert len(first + rest) == 6
    only_b = client.get("/audit/events", params={"session_id": "sb"}).json()
    assert [e["event_type"] for e in only_b] == ["POLICY_DECISION"]
    assert len(client.get("/audit/events", params={"tool": "get_system"}).json()) == 5
    assert len(client.get("/audit/events", params={"call_id": "c2"}).json()) == 1
    assert client.get("/audit/events", params={"limit": 0}).status_code == 422


def test_the_chain_can_be_verified_over_rest(client: TestClient, audit: AuditLog) -> None:
    audit.append(EventType.CALL_RECEIVED, session_id="s", principal="agent:reader")
    body = client.get("/audit/verify").json()
    assert body["ok"] is True and body["events"] == 1 and body["chain"] == audit.chain


def test_approvals_are_listed_and_read(
    client: TestClient, session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    approval_id = pending(session, audit, clock)
    listed = client.get("/approvals", params={"status": "PENDING"}).json()
    assert [a["id"] for a in listed] == [approval_id]
    one = client.get(f"/approvals/{approval_id}").json()
    assert one["status"] == "PENDING" and one["summary"] == {"ids": [1, 2]}
    assert client.get("/approvals/apr_0000000000000000").status_code == 404
    clock.advance(approvals.DEFAULT_TTL.total_seconds() + 1)
    assert client.get(f"/approvals/{approval_id}").json()["status"] == "EXPIRED"
    assert client.get("/approvals", params={"status": "EXPIRED"}).json()[0]["id"] == approval_id


def test_deciding_an_approval_needs_the_token(
    client: TestClient, session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    approval_id = pending(session, audit, clock)
    url, body = f"/approvals/{approval_id}/decide", {"decision": "approve", "note": "ok"}
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers={"X-Approver-Token": "wrong"}).status_code == 401
    assert client.post(url, json=body, headers={"X-Approver-Token": ""}).status_code == 401
    assert client.get(f"/approvals/{approval_id}").json()["status"] == "PENDING"
    done = client.post(url, json=body, headers=AUTH)
    assert done.status_code == 200 and done.json()["status"] == "APPROVED"
    assert done.json()["decided_by"] == "human:approver"
    assert client.post(url, json=body, headers=AUTH).status_code == 409
    assert (
        client.post("/approvals/apr_0000000000000000/decide", json=body, headers=AUTH).status_code
        == 404
    )
    assert client.post(url, json={"decision": "maybe"}, headers=AUTH).status_code == 422


def test_with_no_token_configured_the_write_routes_are_closed(
    session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    closed = build(session, audit, clock, None)
    approval_id = pending(session, audit, clock)
    res = closed.post(
        f"/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=AUTH
    )
    assert res.status_code == 503
    sid = System(name="closed-sys")
    session.add(sid)
    session.flush()
    body = {"environment": "mock", "data_class": "synthetic"}
    res = closed.put(f"/systems/{sid.id}/policy-attributes", json=body, headers=AUTH)
    assert res.status_code == 503


def test_policy_attributes_are_read_open_and_written_with_the_token(
    client: TestClient, session: Session, test_engine: Engine
) -> None:
    row = System(name="attr-sys")
    session.add(row)
    session.flush()
    url = f"/systems/{row.id}/policy-attributes"
    default = client.get(url).json()
    assert default == {"system_id": row.id, "environment": "unknown",
                       "data_class": "unclassified", "recorded": False}  # fmt: skip
    body = {"environment": "mock", "data_class": "synthetic"}
    assert client.put(url, json=body).status_code == 401
    assert client.put(url, json=body, headers={"X-Approver-Token": "nope"}).status_code == 401
    assert client.get(url).json()["recorded"] is False
    saved = client.put(url, json=body, headers=AUTH)
    assert saved.status_code == 200 and saved.json()["environment"] == "mock"
    assert client.get(url).json()["recorded"] is True
    assert client.put(url, json={"environment": "elsewhere", "data_class": "synthetic"},
                      headers=AUTH).status_code == 422  # fmt: skip
    assert (
        client.put("/systems/999999/policy-attributes", json=body, headers=AUTH).status_code == 404
    )


def test_no_response_ever_contains_the_token(
    client: TestClient, session: Session, audit: AuditLog, clock: ManualClock
) -> None:
    approval_id = pending(session, audit, clock)
    texts = [
        client.get("/policy/active").text, client.get("/mcp/tools").text,
        client.get("/audit/events").text, client.get("/approvals").text,
        client.post(f"/approvals/{approval_id}/decide", json={"decision": "approve"}).text,
        client.post(f"/approvals/{approval_id}/decide", json={"decision": "approve"},
                    headers=AUTH).text,
    ]  # fmt: skip
    assert all(TOKEN not in text for text in texts)
