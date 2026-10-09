"""Budgets count what a call really does, including the calls inside a repair run."""

from pathlib import Path

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db_models import AuditEvent, IntegrationVersion, RepairRun
from app.policy.audit import EventType
from app.policy.loader import load_active
from app.policy.models import DataClass, Environment
from tests.mcp_server.rig import Rig, make_rig, with_limits
from tests.repair.support import GOOD, l2_reply


def tight(engine: Engine, tmp_path: Path, **limits: int) -> Rig:
    return make_rig(engine, tmp_path, policy=with_limits(load_active(), **limits))


def count(rig: Rig, event: EventType, kind: str | None = None) -> int:
    return rig.audit.count(rig.deps.session_id, event, kind=kind)


def test_a_mapping_run_stops_when_the_model_budget_runs_out(
    test_engine: Engine, tmp_path: Path
) -> None:
    rig = tight(test_engine, tmp_path, max_model_calls=3)
    world = rig.world()
    args = {
        "source_system_version": world.source_version_id,
        "target_system_version": world.target_version_id,
        "source_entity": "Customer", "target_entity": "User",
    }  # fmt: skip
    stopped = rig.call("propose_mapping", **args)
    assert stopped.status == "budget_exceeded" and stopped.is_error
    assert stopped.body["kind"] == "model_call" and stopped.body["used"] == 3
    assert stopped.body["limit"] == 3 and rig.model.calls == 3
    assert count(rig, EventType.CHARGE, "model_call") == 3
    assert count(rig, EventType.BUDGET_EXCEEDED) == 1
    again = rig.call("propose_mapping", **args)
    assert again.status == "denied" and again.body["reason_code"] == "BUDGET_EXCEEDED"
    assert rig.model.calls == 3, "the second call was refused before anything ran"


def test_a_sandbox_budget_stops_a_generation_midway_and_leaves_no_half_result(
    test_engine: Engine, tmp_path: Path
) -> None:
    rig = tight(test_engine, tmp_path, max_sandbox_runs=1)
    world = rig.world()
    with Session(test_engine) as db:
        before = db.scalar(select(func.count()).select_from(IntegrationVersion)) or 0
    stopped = rig.call("generate_integration", mapping_run_id=world.mapping_run_id)
    assert stopped.status == "budget_exceeded" and stopped.body["kind"] == "sandbox_run"
    assert stopped.body["charged"]["sandbox_run"] == 1
    with Session(test_engine) as db:
        after = db.scalar(select(func.count()).select_from(IntegrationVersion)) or 0
    assert after == before


def test_the_model_budget_counts_calls_inside_a_repair_run(
    test_engine: Engine, tmp_path: Path
) -> None:
    rig = tight(test_engine, tmp_path, max_model_calls=2)
    rig.model.replies = ["not json", "still not json", "no", "no"]
    world = rig.world()
    stopped = rig.call("repair_integration", mapping_run_id=world.mapping_run_id, condition="L2R")
    assert stopped.status == "budget_exceeded", stopped.body
    assert stopped.body["kind"] == "model_call" and rig.model.calls == 2
    run_id = stopped.body["partial"]["repair_run_id"]
    with Session(test_engine) as db:
        run = db.get(RepairRun, run_id)
        assert run is not None and len(run.attempts) == 2 and run.status == "RUNNING"
    shown = rig.call("get_repair_run", run_id=run_id)
    assert shown.status == "ok" and len(shown.body["result"]["attempts"]) == 2


def test_the_sandbox_budget_counts_runs_inside_a_repair_run(
    test_engine: Engine, tmp_path: Path
) -> None:
    rig = tight(test_engine, tmp_path, max_sandbox_runs=3)
    rig.model.replies = [l2_reply(GOOD)]
    world = rig.world()
    stopped = rig.call("repair_integration", mapping_run_id=world.mapping_run_id, condition="L2R")
    assert stopped.status == "budget_exceeded" and stopped.body["kind"] == "sandbox_run"
    assert stopped.body["charged"]["sandbox_run"] == 3  # ruff, mypy, then the generated tests
    assert rig.model.calls == 1


def test_a_budget_belongs_to_a_session_not_to_the_server(
    test_engine: Engine, tmp_path: Path
) -> None:
    first = tight(test_engine, tmp_path / "a", max_sandbox_runs=2)
    world = first.world()
    assert first.call("generate_integration", mapping_run_id=world.mapping_run_id).status == "ok"
    refused = first.call("run_generated_tests", integration_id=1, version=1)
    assert refused.status == "denied" and refused.body["reason_code"] == "BUDGET_EXCEEDED"
    second = tight(test_engine, tmp_path / "b", max_sandbox_runs=2)
    second_world = second.world()
    assert (
        second.call("generate_integration", mapping_run_id=second_world.mapping_run_id).status
        == "ok"
    )


def test_charges_are_audit_events_and_the_chain_stays_valid(
    test_engine: Engine, tmp_path: Path
) -> None:
    from app.policy.audit import verify_chain

    rig = tight(test_engine, tmp_path, max_model_calls=3)
    world = rig.world()
    rig.call("propose_mapping", source_system_version=world.source_version_id,
             target_system_version=world.target_version_id, source_entity="Customer",
             target_entity="User")  # fmt: skip
    with Session(test_engine) as db:
        kinds = [
            e.payload["kind"]
            for e in db.scalars(
                select(AuditEvent).where(
                    AuditEvent.chain == rig.audit.chain, AuditEvent.event_type == "CHARGE"
                )
            )
        ]
    assert kinds == ["model_call"] * 3
    assert verify_chain(test_engine, rig.audit.chain).ok
    assert DataClass.SYNTHETIC and Environment.MOCK
