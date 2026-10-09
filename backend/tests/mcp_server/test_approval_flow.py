"""Needs-approval through the gateway: self-describing, bound to the call, single use, expiring."""

import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.policy import approvals
from app.policy.approvals import DEFAULT_TTL
from app.policy.loader import load_active
from app.policy.models import DataClass, Environment
from tests.mcp_server.rig import Rig, make_rig, with_limits


def propose_args(rig: Rig, data_class: DataClass = DataClass.INTERNAL) -> dict[str, Any]:
    world = rig.world(Environment.MOCK, data_class)
    return {
        "source_system_version": world.source_version_id,
        "target_system_version": world.target_version_id,
        "source_entity": "Customer",
        "target_entity": "User",
    }


def decide(rig: Rig, approval_id: str, approve: bool = True) -> None:
    with Session(rig.engine) as db:
        approvals.decide(db, rig.audit, rig.clock, approval_id, approve=approve)


def test_the_response_says_what_happened_who_decides_and_how_to_retry(rig: Rig) -> None:
    args = propose_args(rig)
    response = rig.call("propose_mapping", **args)
    body = response.body
    assert response.status == "needs_approval" and not response.is_error
    assert rig.model.calls == 0, "nothing ran"
    assert (
        body["decision"] == "NEEDS_APPROVAL" and body["reason_code"] == "MODEL_DATA_NOT_SYNTHETIC"
    )
    assert body["policy_version"] == "v1" and body["approval_id"].startswith("apr_")
    assert body["retry_with"] == {**args, "approval_id": body["approval_id"]}
    text = body["how_to_retry"]
    assert body["approval_id"] in text and "exactly the same arguments" in text
    assert "you cannot" in text and "single-use" in text and "expires" in text
    assert body["expires_at"] == (rig.clock.now() + DEFAULT_TTL).isoformat()


def test_asking_again_reuses_the_open_request_but_other_arguments_get_their_own(rig: Rig) -> None:
    args = propose_args(rig)
    first = rig.call("propose_mapping", **args).body["approval_id"]
    assert rig.call("propose_mapping", **args).body["approval_id"] == first
    other = {**args, "target_entity": "Other"}
    assert rig.call("propose_mapping", **other).body["approval_id"] != first


def test_an_undecided_approval_does_not_unlock_the_call(rig: Rig) -> None:
    args = propose_args(rig)
    approval_id = rig.call("propose_mapping", **args).body["approval_id"]
    response = rig.call("propose_mapping", **args, approval_id=approval_id)
    assert response.status == "denied" and response.body["reason_code"] == "APPROVAL_INVALID"
    assert response.body["approval_check"] == "NOT_DECIDED" and rig.model.calls == 0


def test_an_approved_call_runs_exactly_once(rig: Rig) -> None:
    args = propose_args(rig)
    approval_id = rig.call("propose_mapping", **args).body["approval_id"]
    decide(rig, approval_id)
    first = rig.call("propose_mapping", **args, approval_id=approval_id)
    assert first.status == "ok", first.body
    assert first.body["decision"] == "ALLOW" and rig.model.calls > 0
    calls = rig.model.calls
    replay = rig.call("propose_mapping", **args, approval_id=approval_id)
    assert replay.status == "denied" and replay.body["approval_check"] == "CONSUMED"
    assert rig.model.calls == calls, "the replay ran nothing"


def test_an_approval_covers_only_the_call_it_was_given_for(rig: Rig) -> None:
    args = propose_args(rig)
    approval_id = rig.call("propose_mapping", **args).body["approval_id"]
    decide(rig, approval_id)
    changed = rig.call("propose_mapping", **{**args, "target_entity": "Other"},
                       approval_id=approval_id)  # fmt: skip
    assert changed.body["approval_check"] == "REQUEST_MISMATCH"
    world = rig.world(Environment.MOCK, DataClass.INTERNAL)
    other_tool = rig.call("generate_integration", mapping_run_id=world.mapping_run_id,
                          condition="L1", approval_id=approval_id)  # fmt: skip
    assert other_tool.status == "denied" and other_tool.body["approval_check"] == "REQUEST_MISMATCH"


def test_an_approval_expires_by_the_clock(rig: Rig) -> None:
    args = propose_args(rig)
    approval_id = rig.call("propose_mapping", **args).body["approval_id"]
    decide(rig, approval_id)
    rig.clock.advance(DEFAULT_TTL.total_seconds() + 1)
    response = rig.call("propose_mapping", **args, approval_id=approval_id)
    assert response.body["approval_check"] == "EXPIRED" and rig.model.calls == 0
    again = rig.call("propose_mapping", **args)
    assert again.status == "needs_approval" and again.body["approval_id"] != approval_id


def test_a_denied_or_unknown_approval_is_refused(rig: Rig) -> None:
    args = propose_args(rig)
    approval_id = rig.call("propose_mapping", **args).body["approval_id"]
    decide(rig, approval_id, approve=False)
    assert (
        rig.call("propose_mapping", **args, approval_id=approval_id).body["approval_check"]
        == "DENIED"
    )
    ghost = rig.call("propose_mapping", **args, approval_id="apr_0123456789abcdef")
    assert ghost.body["approval_check"] == "UNKNOWN"
    malformed = rig.call("propose_mapping", **args, approval_id="apr_nonsense")
    assert malformed.body["reason_code"] == "SCHEMA_INVALID"


def test_a_change_of_policy_voids_an_approval(
    rig: Rig, test_engine: Engine, tmp_path: Path
) -> None:
    args = propose_args(rig)
    approval_id = rig.call("propose_mapping", **args).body["approval_id"]
    decide(rig, approval_id)
    stricter = make_rig(
        test_engine, tmp_path / "stricter", policy=with_limits(load_active(), max_model_calls=30),
        session_id=rig.deps.session_id,
    )  # fmt: skip
    response = stricter.call("propose_mapping", **args, approval_id=approval_id)
    assert response.status == "denied" and response.body["approval_check"] == "POLICY_CHANGED"


def test_an_approval_never_unlocks_something_the_floor_denies(rig: Rig) -> None:
    world = rig.world(Environment.PRODUCTION, DataClass.INTERNAL)
    args = {"mapping_run_id": world.mapping_run_id, "condition": "L1"}
    denied = rig.call("generate_integration", **args)
    assert denied.body["reason_code"] == "EXEC_TARGET_NOT_MOCK" and "approval_id" not in denied.body
    # a human approves a request for the same call on a mock twin, then the target is production
    mock = rig.world(Environment.MOCK, DataClass.INTERNAL)
    twin = {"mapping_run_id": mock.mapping_run_id, "condition": "L1"}
    approval_id = rig.call("generate_integration", **twin).body["approval_id"]
    decide(rig, approval_id)
    forged = rig.call("generate_integration", **args, approval_id=approval_id)
    assert forged.status == "denied" and forged.body["reason_code"] == "EXEC_TARGET_NOT_MOCK"


def test_two_callers_racing_for_one_approval_run_the_call_once(rig: Rig) -> None:
    args = propose_args(rig)
    approval_id = rig.call("propose_mapping", **args).body["approval_id"]
    decide(rig, approval_id)
    statuses: list[str] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        barrier.wait()
        statuses.append(rig.call("propose_mapping", **args, approval_id=approval_id).status)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert sorted(statuses) == ["denied", "ok"]


@pytest.mark.parametrize("tool", ["generate_integration", "repair_integration"])
def test_the_other_model_tools_ask_in_the_same_way(rig: Rig, tool: str) -> None:
    world = rig.world(Environment.MOCK, DataClass.RESTRICTED)
    args = (
        {"mapping_run_id": world.mapping_run_id, "condition": "L2"}
        if tool == "generate_integration"
        else {"mapping_run_id": world.mapping_run_id, "condition": "L1R"}
    )
    response = rig.call(tool, **args)
    assert response.status == "needs_approval" and response.body["retry_with"]["condition"]
    assert rig.model.calls == 0
