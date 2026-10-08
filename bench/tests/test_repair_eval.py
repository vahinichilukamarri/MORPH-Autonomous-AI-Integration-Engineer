"""The repair evaluation harness: refusals, caps, fail-closed usage, resume, grading, schema.

No real model is called and Docker is not needed: the model is scripted and the sandbox is a stub.
"""

import json
from pathlib import Path

import pytest
from app.llm.base import CallMetadata, LLMRequest, RawCompletion
from app.llm.store import ResponseStore
from app.repair.service import ensure_checkpoint_schema
from pydantic import BaseModel
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from morph_bench.mapping_eval import Limits
from morph_bench.repair_eval import (
    PHASE_CALL_CAPS,
    BudgetedProvider,
    BudgetExhausted,
    BudgetState,
    MissingUsageError,
    RepairUnitResult,
    check_recorded_calls,
    exposure_tag,
    load_results,
    require_usage,
)
from scripts import run_mapping_eval
from scripts.run_mapping_eval import ResumableProvider
from scripts.run_repair_eval import (
    EXIT_BUDGET,
    EXIT_HARD,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    check_options,
    main,
    verify_recording_path,
)
from tests.repair_support import GOOD, SUPPRESSED, Scripted, StubRunner, l2_reply

S1 = "crm_customer_to_support_user"


class Answer(BaseModel):
    value: int


REQUEST = LLMRequest("s", ("p",), "answer")


@pytest.fixture(scope="module", autouse=True)
def checkpoint_schema(test_engine: Engine) -> None:
    ensure_checkpoint_schema(conninfo_of(test_engine))


def url_of(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def conninfo_of(engine: Engine) -> str:
    return engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


class OracleSpy:
    def __init__(self, failing: bool) -> None:
        self.failing = failing
        self.calls: list[str] = []

    def __call__(self, bundle: Path, scenario_id: str) -> list[dict[str, object]]:
        self.calls.append(scenario_id)
        assert (bundle / "integration" / "sync.py").is_file(), "the READY bundle is what is graded"
        return [
            {"category": "O1", "name": "a", "passed": True},
            {"category": "O2", "name": "b", "passed": not self.failing},
        ]


def run_main(
    engine: Engine, store: Path, llm: Scripted | BudgetedProvider, *extra: str,
    phase: str = "fresh", oracle: OracleSpy | None = None, ensure: object = None,
) -> int:  # fmt: skip
    argv = [
        "--phase", phase, "--conditions", "L2R", "--scenarios", "S1", "--test-only",
        "--store-dir", str(store), "--database-url", url_of(engine), *extra,
    ]  # fmt: skip
    return main(
        argv,
        llm_override=llm,
        runner_override=StubRunner(),
        smoke_runner_override=StubRunner(),
        oracle_override=oracle or OracleSpy(False),
        ensure_schema=ensure or ensure_checkpoint_schema,  # type: ignore[arg-type]
    )


# ---- refusals -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        (dict(phase="fixed", provider=None), "--provider is required"),
        (dict(phase="fixed", provider="replay"), "only allowed with --test-only"),
        (dict(phase="fixed", provider="groq", test_only=True), "never calls a real provider"),
        (dict(phase="fixed", provider="groq"), "--confirm-real-run"),
        (dict(phase="fixed", provider="groq", confirm=True), "--max-real-tokens"),
        (
            dict(phase="fixed", provider="groq", confirm=True, max_tokens=1, max_calls=21),
            "cap of 20",
        ),
        (
            dict(phase="fresh", provider="groq", confirm=True, max_tokens=1, max_calls=27),
            "cap of 26",
        ),
        (dict(phase="fixed", provider="replay", test_only=True, conditions=["L3"]), "condition"),
        (dict(phase="other", provider="replay", test_only=True), "--phase"),
    ],
)
def test_the_run_is_refused_unless_every_precondition_holds(
    kwargs: dict[str, object], fragment: str
) -> None:
    base: dict[str, object] = dict(
        phase="fixed", provider="replay", test_only=False, confirm=False, max_calls=None,
        max_tokens=None, conditions=["L1R", "L2R"],
    )  # fmt: skip
    refusal = check_options(**{**base, **kwargs})  # type: ignore[arg-type]
    assert refusal is not None and fragment in refusal


def test_a_run_inside_every_precondition_is_allowed() -> None:
    assert PHASE_CALL_CAPS == {"fixed": 20, "fresh": 26}
    real = check_options(
        phase="fixed", provider="groq", test_only=False, confirm=True, max_calls=20,
        max_tokens=160_000, conditions=["L1R", "L2R"],
    )  # fmt: skip
    assert real is None
    offline = check_options(
        phase="fresh", provider="replay", test_only=True, confirm=False, max_calls=None,
        max_tokens=None, conditions=["L2R"],
    )  # fmt: skip
    assert offline is None


def test_main_refuses_before_touching_anything(tmp_path: Path) -> None:
    code = main(["--phase", "fixed", "--provider", "groq", "--store-dir", str(tmp_path / "s")])
    assert code == EXIT_REFUSED and not (tmp_path / "s").exists()


# ---- budget: calls and tokens, across resumes -----------------------------------------------


def meta(**changes: object) -> CallMetadata:
    base: dict[str, object] = dict(
        provider="p", model="m", prompt_hash="h", latency_ms=1, input_tokens=100,
        output_tokens=20, reasoning_tokens=30, total_tokens=150, usage={"total_tokens": 150},
    )  # fmt: skip
    return CallMetadata(**{**base, **changes})  # type: ignore[arg-type]


class Echo(Scripted):
    def __init__(self, *, usage: bool = True) -> None:
        super().__init__(["{}"] * 50)
        self.usage = usage

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        super().complete_raw(request, response_model)
        if self.usage:
            return RawCompletion('{"value": 1}', meta())
        return RawCompletion('{"value": 1}', meta(total_tokens=None, usage=None))


def test_the_call_cap_stops_the_next_call_and_counts_survive_a_restart(tmp_path: Path) -> None:
    state = tmp_path / "budget.json"
    first = BudgetedProvider(Echo(), state, max_calls=2, max_tokens=None)
    first.complete_raw(REQUEST, Answer)
    first.complete_raw(REQUEST, Answer)
    with pytest.raises(BudgetExhausted, match="max-real-calls"):
        first.complete_raw(REQUEST, Answer)
    restarted = BudgetedProvider(Echo(), state, max_calls=3, max_tokens=None)
    assert restarted.state.calls == 2, "cumulative across runs"
    restarted.complete_raw(REQUEST, Answer)  # the raised cap leaves room
    with pytest.raises(BudgetExhausted):
        restarted.complete_raw(REQUEST, Answer)


def test_the_token_cap_counts_input_output_and_reasoning(tmp_path: Path) -> None:
    provider = BudgetedProvider(Echo(), tmp_path / "b.json", max_calls=50, max_tokens=320)
    provider.complete_raw(REQUEST, Answer)  # 100 + 20 + 30 = 150
    assert provider.state.tokens == 150
    provider.complete_raw(REQUEST, Answer)
    assert provider.state.tokens == 300
    with pytest.raises(BudgetExhausted, match="max-real-tokens"):
        provider.complete_raw(REQUEST, Answer)  # 300 plus this request's own size passes 320
    assert BudgetState.load(tmp_path / "b.json").tokens == 300


def test_a_call_below_both_caps_goes_through(tmp_path: Path) -> None:
    provider = BudgetedProvider(Echo(), tmp_path / "b.json", max_calls=5, max_tokens=10_000)
    assert provider.complete_raw(REQUEST, Answer).text == '{"value": 1}'


def test_only_real_calls_count_because_the_cache_sits_outside(tmp_path: Path) -> None:
    budget = BudgetedProvider(Echo(), tmp_path / "b.json", max_calls=1, max_tokens=None)
    outer = ResumableProvider(budget, tmp_path, limits=Limits(), require_usage=True)
    outer.complete_raw(REQUEST, Answer)
    outer.complete_raw(REQUEST, Answer)  # served from the store: no second real call
    assert budget.state.calls == 1


# ---- fail closed on missing usage -----------------------------------------------------------


def test_a_call_without_usage_or_total_tokens_is_refused() -> None:
    require_usage(meta())
    with pytest.raises(MissingUsageError):
        require_usage(meta(usage=None))
    with pytest.raises(MissingUsageError):
        require_usage(meta(total_tokens=None))


def test_a_real_call_without_usage_stops_the_run_but_its_reply_is_kept(tmp_path: Path) -> None:
    provider = ResumableProvider(Echo(usage=False), tmp_path, limits=Limits(), require_usage=True)
    with pytest.raises(MissingUsageError):
        provider.complete_raw(REQUEST, Answer)
    assert len(ResponseStore(tmp_path / "calls")) == 1, "a paid reply is never thrown away"
    assert check_recorded_calls(tmp_path), "and the next start refuses because of it"


def test_the_resumable_provider_keeps_usage_total_tokens_and_finish_reason(tmp_path: Path) -> None:
    class Stops(Echo):
        def complete_raw(
            self, request: LLMRequest, response_model: type[BaseModel]
        ) -> RawCompletion:
            return RawCompletion('{"value": 1}', meta(finish_reason="length"))

    provider = ResumableProvider(Stops(), tmp_path, limits=Limits(), require_usage=True)
    first = provider.complete_raw(REQUEST, Answer).metadata
    again = provider.complete_raw(REQUEST, Answer).metadata  # from the store
    assert again.source == "cache"
    for got in (first, again):
        assert (got.total_tokens, got.usage, got.finish_reason) == (
            150, {"total_tokens": 150}, "length",
        )  # fmt: skip
    assert check_recorded_calls(tmp_path) == []


def test_the_recording_path_self_check_passes_and_catches_a_path_that_drops_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verify_recording_path()
    from app.llm.store import Recorded

    def lossy(key: str, raw: RawCompletion) -> Recorded:
        m = raw.metadata
        return Recorded(
            key, raw.text, m.provider, m.model, m.input_tokens, m.output_tokens,
            m.reasoning_tokens, m.latency_ms,
        )  # fmt: skip

    monkeypatch.setattr(run_mapping_eval, "_recorded", lossy)
    with pytest.raises(MissingUsageError):
        verify_recording_path()


def test_a_real_run_is_refused_when_a_stored_call_lacks_usage(tmp_path: Path) -> None:
    calls = tmp_path / "store" / "calls"
    calls.mkdir(parents=True)
    old = {
        "prompt_hash": "h:1", "text": "{}", "provider": "groq", "model": "m", "input_tokens": 1,
        "output_tokens": 1, "reasoning_tokens": 0, "latency_ms": 1,
    }  # fmt: skip
    (calls / "calls.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
    code = main(
        ["--phase", "fixed", "--provider", "groq", "--confirm-real-run", "--max-real-tokens", "9",
         "--store-dir", str(tmp_path / "store")]
    )  # fmt: skip
    assert code == EXIT_USAGE


# ---- a run: grading, resume, schema ---------------------------------------------------------


def results_of(store: Path) -> list[RepairUnitResult]:
    return load_results(store / "results.jsonl")


def test_a_ready_unit_is_graded_once_and_a_failing_oracle_makes_it_gate_passing_incorrect(
    test_engine: Engine, tmp_path: Path
) -> None:
    store, spy = tmp_path / "store", OracleSpy(failing=True)
    llm = Scripted([l2_reply(GOOD)])
    assert run_main(test_engine, store, llm, oracle=spy) == EXIT_OK
    (unit,) = results_of(store)
    assert (unit.status, unit.label) == ("READY", "gate-passing, incorrect")
    assert unit.integration_correct is False and unit.oracle_failed_checks == ["O2.b"]
    assert unit.oracle is not None and unit.oracle["O1"] == (1, 1)
    assert spy.calls == [S1]
    # a second start finds the unit finished: no model call, no second grading
    assert run_main(test_engine, store, llm, oracle=spy) == EXIT_OK
    assert spy.calls == [S1] and len(llm.calls) == 1 and len(results_of(store)) == 1


def test_a_ready_unit_that_passes_the_oracle_is_just_ready(
    test_engine: Engine, tmp_path: Path
) -> None:
    run_main(test_engine, tmp_path / "s", Scripted([l2_reply(GOOD)]), oracle=OracleSpy(False))
    (unit,) = results_of(tmp_path / "s")
    assert (unit.label, unit.integration_correct) == ("READY", True)


def test_a_unit_that_does_not_reach_ready_is_never_graded(
    test_engine: Engine, tmp_path: Path
) -> None:
    spy = OracleSpy(False)
    steps = [l2_reply(SUPPRESSED + f"\nX{n} = {n}\n") for n in range(4)]
    run_main(test_engine, tmp_path / "s", Scripted(steps), oracle=spy)
    (unit,) = results_of(tmp_path / "s")
    assert unit.status == "HUMAN_REVIEW_REQUIRED" and unit.reason == "EXHAUSTED:GUARD"
    assert unit.oracle is None and unit.integration_correct is None and spy.calls == []
    assert unit.model_turns == 4 and [a.attempt for a in unit.attempts] == [0, 1, 2, 3]
    assert unit.attempts[0].guard_enforced == ["G3"] and unit.attempts[0].feedback_codes == [
        "GUARD.G3"
    ]
    assert unit.exposure == "CAUSE_CONTRADICTION" and unit.real_calls == 4


def test_a_budget_stop_pauses_cleanly_and_the_next_start_continues_the_same_run(
    test_engine: Engine, tmp_path: Path
) -> None:
    store, state = tmp_path / "store", tmp_path / "budget.json"
    first = BudgetedProvider(
        Scripted(["not json", l2_reply(SUPPRESSED)]), state, max_calls=2, max_tokens=None
    )
    assert run_main(test_engine, store, first) == EXIT_BUDGET
    assert results_of(store) == [], "an unfinished unit is not a result"
    runs = json.loads((store / "runs.json").read_text("utf-8"))
    assert len(runs) == 1
    second_llm = Scripted([l2_reply(GOOD)])
    second = BudgetedProvider(second_llm, state, max_calls=10, max_tokens=None)
    spy = OracleSpy(False)
    assert run_main(test_engine, store, second, oracle=spy) == EXIT_OK
    (unit,) = results_of(store)
    assert (
        unit.status == "READY"
        and unit.model_turns == 3
        and unit.run_id == next(iter(runs.values()))
    )
    assert len(second_llm.calls) == 1, "the two finished attempts were not asked again"
    assert second.state.calls == 3 and spy.calls == [S1]
    # and the finished unit is skipped by a third start
    run_main(test_engine, store, Scripted([]), oracle=spy)
    assert len(results_of(store)) == 1 and spy.calls == [S1]


def test_the_checkpoint_schema_is_set_up_exactly_once_per_start(
    test_engine: Engine, tmp_path: Path
) -> None:
    seen: list[str] = []

    def counting(conninfo: str) -> None:
        seen.append(conninfo)
        ensure_checkpoint_schema(conninfo)

    argv = [
        "--phase", "fresh", "--conditions", "L2R", "--scenarios", "S1,S3", "--test-only",
        "--store-dir", str(tmp_path / "s"), "--database-url", url_of(test_engine),
    ]  # fmt: skip
    code = main(
        argv, llm_override=Scripted([l2_reply(GOOD), l2_reply(GOOD)]),
        runner_override=StubRunner(), smoke_runner_override=StubRunner(),
        oracle_override=OracleSpy(False), ensure_schema=counting,
    )  # fmt: skip
    assert code == EXIT_OK and len(results_of(tmp_path / "s")) == 2
    assert seen == [conninfo_of(test_engine)], "once, for two units"


def test_a_fixed_start_without_its_recorded_replay_aborts_and_makes_no_live_call(
    test_engine: Engine, tmp_path: Path
) -> None:
    live = Scripted([])
    empty_seed = tmp_path / "no-replays"
    code = run_main(test_engine, tmp_path / "s", live, "--seed-dir", str(empty_seed), phase="fixed")
    assert code == EXIT_HARD and live.calls == []
    assert results_of(tmp_path / "s") == []


# ---- tags -----------------------------------------------------------------------------------


def test_the_exposure_tags_are_the_plans() -> None:
    assert exposure_tag(S1, "L1R") == "PROMPT_GAP"
    assert exposure_tag("crm_v2_to_support_v2", "L1R") == "PROMPT_GAP"
    assert exposure_tag("support_user_to_crm_customer", "L1R") == "SECRET_LITERAL_FALSE_POSITIVE"
    assert exposure_tag(S1, "L2R") == "CAUSE_CONTRADICTION"
    assert exposure_tag("support_user_to_crm_customer", "L2R") == "MODEL"
    assert exposure_tag("something_else", "L2R") == "UNTAGGED"


def test_the_database_is_the_only_shared_state_between_starts(
    test_engine: Engine, tmp_path: Path
) -> None:
    with Session(test_engine) as session:
        from app.db_models import RepairRun

        before = session.scalar(select(func.count()).select_from(RepairRun)) or 0
    run_main(test_engine, tmp_path / "s", Scripted([l2_reply(GOOD)]))
    with Session(test_engine) as session:
        after = session.scalar(select(func.count()).select_from(RepairRun)) or 0
    assert after == before + 1
