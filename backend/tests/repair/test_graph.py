"""The repair graph with a scripted model and a stub sandbox (no Docker, no network, no prompts).

Each test states one rule of the plan: the attempt count, the shared budget, no-op stops, the guards
as seen through the graph, infrastructure pauses, the size policy, and resume.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.codegen.llm_codegen import SyncModuleProposal
from app.codegen.sandbox import LimitsNotEnforced
from app.db_models import GateResultRow, LLMCall, RepairAttempt, RepairRun
from app.llm.base import LLMError, RateLimitExhausted, ReplayMissError, RequestTooLarge
from app.llm.store import Recorded, ReplayLLMProvider, ResponseStore
from app.repair.service import TracingEnabled, resume_repair, run_repair
from app.repair.sizing import estimate_request
from app.repair.state import MAX_PAUSES, HardError, StartMode
from tests.repair.support import (
    DUNDER,
    GOOD,
    SUPPRESSED,
    FakeBuilder,
    Rig,
    failing_source,
    l2_reply,
    make_rig,
    needs_review_fields,
)


def attempts(session: Session, run_id: int) -> list[RepairAttempt]:
    session.expire_all()
    return list(
        session.scalars(
            select(RepairAttempt)
            .where(RepairAttempt.repair_run_id == run_id)
            .order_by(RepairAttempt.attempt)
        )
    )


def make(session: Session, engine: Engine, tmp: Path, steps: Sequence[object], **kw: Any) -> Rig:
    return make_rig(session, engine, tmp, steps, **kw)


# ---- reaching READY ------------------------------------------------------------------------------


def test_ready_at_attempt_two_after_a_non_json_reply_and_a_suppression(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    rig = make(
        session, test_engine, tmp_path, ["this is not json", l2_reply(SUPPRESSED), l2_reply(GOOD)]
    )
    result = run_repair(rig.env)
    assert (result.status, result.attempts, result.pauses) == ("READY", 3, 0)
    rows = attempts(session, result.run_id)
    assert [r.attempt for r in rows] == [0, 1, 2]
    assert [r.failed_stage for r in rows] == ["PROPOSAL", "GUARD", None]
    assert len(rig.live.calls) == 3, "one provider call per attempt: the inner re-ask is off"
    assert rows[0].feedback and rows[0].feedback["items"][0]["code"] == "INVALID_JSON"
    assert rows[1].feedback and rows[1].feedback["items"][0]["code"] == "G3"
    # the feedback of attempt 0 reached the model in the request for attempt 1
    assert "INVALID_JSON" in " ".join(rig.live.calls[1].parts)
    assert "G3" in " ".join(rig.live.calls[2].parts)
    assert rig.runner.calls[-3:] == ["ruff", "mypy", "tests"] or "smoke" in rig.runner.calls
    # every attempt keeps an integration version and its model call, with usage
    assert all(r.integration_version_id is not None for r in rows)
    calls = session.scalars(
        select(LLMCall).where(LLMCall.mapping_run_id == rig.env.inp.mapping_run_id)
    ).all()
    assert len(calls) == 3 and {c.total_tokens for c in calls} == {110}
    assert all(c.integration_version_id for c in calls)


def test_a_finished_run_is_never_reopened(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    rig = make(session, test_engine, tmp_path, [l2_reply(GOOD)])
    first = run_repair(rig.env)
    assert first.status == "READY" and len(rig.live.calls) == 1
    again = resume_repair(rig.env, first.run_id)
    assert (again.status, again.attempts) == ("READY", 1) and len(rig.live.calls) == 1


# ---- the shared budget ---------------------------------------------------------------------------


def test_exhaustion_ends_in_human_review_after_exactly_four_calls(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    steps = [l2_reply(failing_source(n)) for n in range(5)]  # a 5th reply must never be asked for
    rig = make(session, test_engine, tmp_path, steps)
    result = run_repair(rig.env)
    assert (result.status, result.reason) == ("HUMAN_REVIEW_REQUIRED", "EXHAUSTED:AST")
    assert len(rig.live.calls) == 4 and len(rig.live.steps) == 1
    assert [r.attempt for r in attempts(session, result.run_id)] == [0, 1, 2, 3]
    assert "ruff" not in rig.runner.calls, "a failing AST gate never reaches the tools"
    stages = session.scalars(
        select(GateResultRow.stage)
        .join(
            RepairAttempt,
            RepairAttempt.integration_version_id == GateResultRow.integration_version_id,
        )
        .where(RepairAttempt.repair_run_id == result.run_id)
    ).all()
    assert set(stages) == {"ast"}


def test_every_failure_kind_draws_on_the_same_budget(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    steps = [
        ('{"notes": "cut', "length"),  # truncated
        "  ",  # empty
        l2_reply(""),  # valid JSON, rejected by the module check
        l2_reply(SUPPRESSED),
        l2_reply(GOOD),  # never reached
    ]
    rig = make(session, test_engine, tmp_path, steps)
    result = run_repair(rig.env)
    assert (result.status, result.reason) == ("HUMAN_REVIEW_REQUIRED", "EXHAUSTED:GUARD")
    rows = attempts(session, result.run_id)
    codes = [(r.feedback or {}).get("items", [{}])[0].get("code") for r in rows]
    assert codes == ["TRUNCATED", "EMPTY_OUTPUT", "INVALID", "G3"]
    assert len(rig.live.calls) == 4


# ---- no-op detection -----------------------------------------------------------------------------


def test_a_repeat_of_the_previous_output_stops_at_attempt_two(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    again = SUPPRESSED.replace("\n", "   \n") + "\n\n"  # whitespace is not a new output
    rig = make(
        session,
        test_engine,
        tmp_path,
        [l2_reply(failing_source(1)), l2_reply(SUPPRESSED), l2_reply(again)],
    )
    result = run_repair(rig.env)
    assert (result.status, result.reason, result.attempts) == ("HUMAN_REVIEW_REQUIRED", "NO_OP", 3)
    assert len(rig.live.calls) == 3, "the no-op attempt was spent, and nothing more is asked"
    assert attempts(session, result.run_id)[2].failed_stage == "NOOP"


def test_a_repeat_of_attempt_zero_stops_at_attempt_three(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    steps = [
        l2_reply(SUPPRESSED),
        l2_reply(failing_source(1)),
        l2_reply(failing_source(2)),
        l2_reply(SUPPRESSED + "\n"),
    ]
    rig = make(session, test_engine, tmp_path, steps)
    result = run_repair(rig.env)
    assert (result.status, result.reason, result.attempts) == ("HUMAN_REVIEW_REQUIRED", "NO_OP", 4)
    assert len(rig.live.calls) == 4


# ---- the guards and the gate, seen through the graph ---------------------------------------------


def test_the_gate_cannot_be_skipped_whatever_the_guards_say(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    rig = make(session, test_engine, tmp_path, [l2_reply(DUNDER), l2_reply(GOOD)])
    result = run_repair(rig.env)
    assert result.status == "READY"
    first = attempts(session, result.run_id)[0]
    assert first.failed_stage == "AST" and first.feedback is not None
    assert first.feedback["items"][0]["code"] == "DUNDER_ACCESS"
    assert first.guard_result == [], "no guard objected: only the gate caught it"
    assert rig.runner.calls[:3] != ["ruff"], "the tools ran only after the AST gate passed"


def test_g4_trips_when_type_escapes_rise_between_attempts(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    one = DUNDER.replace("from typing import Any\n", "from typing import Any\n")
    more = one.replace(
        "-> RunReport:\n    report = RunReport()",
        "-> RunReport:\n    extra: Any = 1\n    other: Any = 2\n    report = RunReport()",
    )
    assert more != one
    rig = make(session, test_engine, tmp_path, [l2_reply(one), l2_reply(more), l2_reply(GOOD)])
    result = run_repair(rig.env)
    rows = attempts(session, result.run_id)
    assert [r.failed_stage for r in rows][:2] == ["AST", "GUARD"]
    assert rows[1].feedback is not None and rows[1].feedback["items"][0]["code"] == "G4"
    assert result.status == "READY"


def test_g1_and_g2_trip_when_the_bundle_or_the_base_tests_are_tampered(
    session: Session, test_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.codegen.generated_tests import render_tests as real_tests
    from app.codegen.generator import generate_package as real_package

    state = {"package": 0, "tests": 0}

    def tampered_package(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        package = real_package(*args, **kwargs)  # type: ignore[arg-type]
        state["package"] += 1
        if state["package"] == 1:  # the first build of attempt 0 only; the rebuild is honest
            package.files["integration/transform.py"] += "\n# tampered\n"
        return package

    def tampered_tests(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        tests = real_tests(*args, **kwargs)  # type: ignore[arg-type]
        state["tests"] += 1
        if state["tests"] == 5:  # the next build, after attempt 0's guard
            tests["tests_generated/__main__.py"] += "\n# weakened\n"
        return tests

    monkeypatch.setattr("app.repair.nodes.generate_package", tampered_package)
    monkeypatch.setattr("app.repair.nodes.render_tests", tampered_tests)
    rig = make(
        session,
        test_engine,
        tmp_path,
        [l2_reply(GOOD), l2_reply(GOOD + "\n# second\nX = 1\n"), l2_reply(GOOD)],
    )
    result = run_repair(rig.env)
    first = attempts(session, result.run_id)[0]
    assert first.failed_stage == "GUARD"
    assert first.feedback is not None
    guards = {i["code"] for i in first.feedback["items"]}
    assert "G1" in guards
    assert any(g["guard"] == "G1" for g in first.guard_result)


S1_FIELDS = [
    "userId", "externalRef", "fullName", "email_address", "phoneNumber", "accountState", "tier",
    "createdAt",
]  # fmt: skip


def l1_proposal(**changes: object) -> str:
    proposal = {
        "source_mode": "LIST", "source_key_field": "customer_id", "source_list_path": "/customers",
        "source_get_path": None, "source_page_param": "page", "source_size_param": "page_size",
        "source_items_key": "items", "source_total_key": "total", "target_mode": "UPSERT",
        "target_id_field": "userId", "target_get_path": "/users/{id}",
        "target_update_method": "PUT", "target_update_path": "/users/{id}",
        "target_create_path": None,
        "target_create_fields": S1_FIELDS,
        "target_update_fields": S1_FIELDS,
        "create_only_fields": [], "omit_if_null_fields": [], "natural_key_field": None,
        "id_assigned_by_target": False, "target_list_path": None, "target_page_param": None,
        "target_size_param": None, "target_items_key": None, "target_total_key": None,
        "edge_record_json": [], "rationale": "r",
    }  # fmt: skip
    return json.dumps({**proposal, **changes})


def test_l1r_reaches_ready_after_a_validator_error_and_a_dropped_required_field(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    fields = S1_FIELDS
    bad_validator = l1_proposal(omit_if_null_fields=["externalRef"])
    dropped = [f for f in fields if f != "fullName"]
    drops_required = l1_proposal(target_create_fields=dropped, target_update_fields=dropped)
    rig = make(
        session,
        test_engine,
        tmp_path,
        [bad_validator, drops_required, l1_proposal()],
        condition="L1R",
    )
    result = run_repair(rig.env)
    rows = attempts(session, result.run_id)
    assert result.status == "READY", (result, [r.feedback for r in rows])
    assert rows[0].failed_stage == "PROPOSAL"
    assert (
        rows[0].feedback is not None
        and "omit_if_null_fields" in rows[0].feedback["items"][0]["code"]
    )
    assert rows[1].failed_stage == "GUARD"
    assert rows[1].feedback is not None and rows[1].feedback["items"][0]["code"] == "G5"
    assert "fullName" in rows[1].feedback["items"][0]["message"]


# ---- blocked units never reach a model -----------------------------------------------------------


def test_a_unit_blocked_for_review_never_calls_a_model(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    rig = make(session, test_engine, tmp_path, [], fields=needs_review_fields())
    result = run_repair(rig.env)
    assert result.status == "BLOCKED_PENDING_REVIEW" and "accountState" in (result.reason or "")
    assert rig.live.calls == []
    assert attempts(session, result.run_id) == []
    mapping_run = rig.env.inp.mapping_run_id
    assert (
        session.scalar(
            select(func.count()).select_from(LLMCall).where(LLMCall.mapping_run_id == mapping_run)
        )
        == 0
    )
    assert rig.runner.calls == []


# ---- infrastructure ------------------------------------------------------------------------------


def test_infrastructure_failures_pause_without_using_an_attempt_then_stop_the_unit(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    failures = [RateLimitExhausted("429 persisted")] * (MAX_PAUSES + 1)
    rig = make(session, test_engine, tmp_path, failures)
    result = run_repair(rig.env)
    assert (result.status, result.pauses) == ("PAUSED", 1)
    for expected in range(2, MAX_PAUSES + 1):
        result = resume_repair(rig.env, result.run_id)
        assert (result.status, result.pauses) == ("PAUSED", expected)
    stopped = resume_repair(rig.env, result.run_id)
    assert stopped.status == "INFRA_STOPPED" and stopped.pauses == MAX_PAUSES + 1
    assert "stopped after 5 pauses" in (stopped.reason or "")
    assert stopped.attempts == 0, "no pause used an attempt"
    assert len(rig.live.calls) == MAX_PAUSES + 1


def test_a_pause_resumes_into_the_same_attempt(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    rig = make(session, test_engine, tmp_path, [LLMError("HTTP 500"), l2_reply(GOOD)])
    paused = run_repair(rig.env)
    assert paused.status == "PAUSED"
    done = resume_repair(rig.env, paused.run_id)
    assert (done.status, done.attempts, done.pauses) == ("READY", 1, 1)


def test_a_sandbox_that_never_started_pauses_and_a_limits_failure_pauses(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    rig = make(session, test_engine, tmp_path, [l2_reply(GOOD)])
    rig.runner.start_failed = "ruff"
    paused = run_repair(rig.env)
    assert paused.status == "PAUSED" and "sandbox did not start" in (paused.reason or "")
    rig.runner.start_failed = None
    done = resume_repair(rig.env, paused.run_id)
    assert done.status == "READY" and len(rig.live.calls) == 1, "the reply was reused, not re-asked"

    second = make(session, test_engine, tmp_path / "b", [l2_reply(GOOD)])

    def raise_limits(*args: object, **kwargs: object) -> None:
        raise LimitsNotEnforced("no cgroup")

    second.runner.run = raise_limits  # type: ignore[method-assign,assignment]
    assert run_repair(second.env).status == "PAUSED"


# ---- the size policy -----------------------------------------------------------------------------


def test_a_request_above_the_limit_by_more_than_ten_percent_is_not_sent(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    rig = make(
        session,
        test_engine,
        tmp_path,
        [l2_reply(failing_source(1))],
        builder=FakeBuilder(pad=45_000),
    )
    result = run_repair(rig.env)
    assert result.status == "INFRA_STOPPED" and (result.reason or "").startswith(
        "REQUEST_TOO_LARGE"
    )
    assert len(rig.live.calls) == 1, "attempt 0 was sent; the oversized repair request was not"
    last = attempts(session, result.run_id)[-1]
    assert last.attempt == 1 and last.failed_stage == "SIZE" and last.size_estimate is not None
    assert last.size_estimate["total_tokens"] > 8000 * 1.1


def test_a_request_within_ten_percent_over_the_limit_is_sent(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    steps = [l2_reply(DUNDER), l2_reply(GOOD)]
    # first learn how big the repair request is, then set the limit 5% below it
    probe = FakeBuilder(pad=36_000)
    first = make(session, test_engine, tmp_path / "probe", list(steps), builder=probe)
    run_repair(first.env)
    estimate = estimate_request(probe.requests[0], SyncModuleProposal, "L2R").total_tokens
    limit = int(estimate / 1.05)
    assert limit < estimate <= limit * 1.1
    rig = make(session, test_engine, tmp_path / "real", list(steps), builder=probe, tpm_limit=limit)
    result = run_repair(rig.env)
    assert result.status == "READY" and len(rig.live.calls) == 2, result


def test_a_provider_that_refuses_a_request_as_too_large_ends_the_unit_without_a_retry(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    rig = make(
        session, test_engine, tmp_path, [l2_reply(failing_source(1)), RequestTooLarge("413")]
    )
    result = run_repair(rig.env)
    assert result.status == "INFRA_STOPPED" and (result.reason or "").startswith("SIZE_REJECTED")
    assert "estimated" in (result.reason or "")
    assert len(rig.live.calls) == 2, "not retried"
    assert result.attempts == 1, "the refused request was not an attempt"
    assert result.pauses == 0


# ---- resume --------------------------------------------------------------------------------------


def test_a_crash_after_the_reply_is_stored_resumes_with_no_duplicate_call(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    crashed = {"done": False}

    def crash_once(attempt: int) -> None:
        if attempt == 1 and not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("the process died after the call, before the checkpoint")

    rig = make(
        session,
        test_engine,
        tmp_path,
        [l2_reply(SUPPRESSED), l2_reply(GOOD)],
        after_call=crash_once,
    )
    with pytest.raises(RuntimeError, match="process died"):
        run_repair(rig.env)
    run_id = session.scalars(select(RepairRun.id).order_by(RepairRun.id.desc())).first()
    assert run_id is not None and len(rig.live.calls) == 2
    done = resume_repair(rig.env, run_id)
    assert done.status == "READY"
    assert len(rig.live.calls) == 2, "the stored reply was reused: zero duplicate calls"
    assert [r.attempt for r in attempts(session, run_id)] == [0, 1]
    calls = session.scalars(
        select(LLMCall).where(LLMCall.mapping_run_id == rig.env.inp.mapping_run_id)
    ).all()
    assert len(calls) == 2, "and no duplicate call row either"


# ---- fixed start ---------------------------------------------------------------------------------


def seeded(tmp: Path, reply: str, builder: FakeBuilder) -> ReplayLLMProvider:
    from app.repair.nodes import RESPONSE_MODELS

    store = ResponseStore(tmp / "seed")
    request = builder.initial(None, None, "L2R")  # type: ignore[arg-type]
    store.put(
        Recorded(request.fingerprint(RESPONSE_MODELS["L2R"]), reply, "groq", "m", 10, 5, 0, 1),
        file=tmp / "seed" / "calls.jsonl",
    )
    return ReplayLLMProvider(ResponseStore(tmp / "seed"))


def test_fixed_start_takes_attempt_zero_only_from_the_injected_replay(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    builder = FakeBuilder()
    seed = seeded(tmp_path, l2_reply(SUPPRESSED), builder)
    rig = make(
        session,
        test_engine,
        tmp_path,
        [l2_reply(GOOD)],
        start_mode=StartMode.FIXED,
        seed=seed,
        builder=builder,
    )
    result = run_repair(rig.env)
    assert result.status == "READY" and len(rig.live.calls) == 1, "only the repair was a live call"
    rows = attempts(session, result.run_id)
    assert [r.source for r in rows] == ["replay", "scripted"]
    assert rows[0].size_estimate is None, "a replayed attempt is not sized: nothing was sent"


def test_fixed_start_with_a_missing_replay_aborts_and_never_calls_the_live_provider(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    empty = ReplayLLMProvider(ResponseStore(tmp_path / "nothing"))
    rig = make(session, test_engine, tmp_path, [], start_mode=StartMode.FIXED, seed=empty)
    with pytest.raises(HardError, match="missing fixed-start replay"):
        run_repair(rig.env)
    assert rig.live.calls == []
    run = session.scalars(select(RepairRun).order_by(RepairRun.id.desc())).first()
    assert run is not None and run.status == "ABORTED"
    with pytest.raises(ReplayMissError):
        empty.complete_structured(FakeBuilder().initial(None, None, "L2R"), SyncModuleProposal)  # type: ignore[arg-type]


def test_a_start_mode_and_its_seed_must_agree(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    fixed_without_seed = make(session, test_engine, tmp_path, [], start_mode=StartMode.FIXED)
    with pytest.raises(ValueError, match="injected replay"):
        run_repair(fixed_without_seed.env)
    seed = ReplayLLMProvider(ResponseStore(tmp_path / "s"))
    fresh_with_seed = make(session, test_engine, tmp_path / "x", [], seed=seed)
    with pytest.raises(ValueError, match="must not be given a replay"):
        run_repair(fresh_with_seed.env)


# ---- determinism and hygiene ---------------------------------------------------------------------


def test_the_same_script_gives_the_same_feedback_and_prompts_through_the_graph(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    def one(sub: str) -> tuple[list[object], list[str], list[str]]:
        rig = make(
            session, test_engine, tmp_path / sub, ["not json", l2_reply(SUPPRESSED), l2_reply(GOOD)]
        )
        result = run_repair(rig.env)
        rows = attempts(session, result.run_id)
        return (
            [r.feedback for r in rows],
            [r.prompt_hash for r in rows],
            [r.output_hash for r in rows],
        )

    assert one("a") == one("b")


def test_a_repair_run_refuses_to_start_when_tracing_is_on(
    session: Session, test_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make(session, test_engine, tmp_path, [])
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    with pytest.raises(TracingEnabled, match="LANGSMITH_TRACING"):
        run_repair(rig.env)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "1")
    with pytest.raises(TracingEnabled, match="LANGCHAIN_TRACING_V2"):
        run_repair(rig.env)
    assert rig.live.calls == []


def test_no_secret_value_reaches_the_stored_feedback(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    secret = "s3cr3tcredentialvalue"
    bad = GOOD.replace("from typing import Any\n", f"import {secret}\nfrom typing import Any\n")
    assert bad != GOOD
    rig = make(session, test_engine, tmp_path, [l2_reply(bad), l2_reply(GOOD)])
    rig.env.secrets = [secret]
    result = run_repair(rig.env)
    stored = json.dumps([r.feedback for r in attempts(session, result.run_id)])
    assert result.status == "READY" and secret not in stored and "[redacted]" in stored


def test_a_full_module_after_an_unparseable_one_is_not_rejected_by_g4(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    """Amendment A14, as seen through the graph: the stray-brace reply gives G4 no baseline."""
    stray_brace = GOOD + "}\n"  # JSON-valid reply whose source does not parse, as in v0.4 S3 and S4
    rig = make(session, test_engine, tmp_path, [l2_reply(stray_brace), l2_reply(GOOD)])
    result = run_repair(rig.env)
    rows = attempts(session, result.run_id)
    assert result.status == "READY", [r.feedback for r in rows]
    assert [r.failed_stage for r in rows] == ["AST", None]
    assert rows[0].feedback is not None and rows[0].feedback["items"][0]["code"] == "SYNTAX"
    assert rows[1].guard_result == []


def test_the_run_id_is_reported_as_soon_as_the_row_exists(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    told: list[int] = []
    rig = make(session, test_engine, tmp_path, [l2_reply(GOOD)])
    rig.env.on_created = told.append
    result = run_repair(rig.env)
    assert told == [result.run_id]
