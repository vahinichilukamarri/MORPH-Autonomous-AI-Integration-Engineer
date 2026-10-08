"""Record a scripted repair run, export it, replay it offline: the same attempts come out.

This tests the plumbing that the committed replays of the real run will use (the export script, the
replay key and the loud miss). The committed replays themselves are checked by
``smoke_tests/test_repair_replay.py`` in the sandbox job. No model, no Docker.
"""

import json
from pathlib import Path

import pytest
from app.repair.service import ensure_checkpoint_schema
from sqlalchemy import Engine

from morph_bench.repair_eval import RepairUnitResult, load_results
from scripts.export_repair_replays import expected_unit, export
from scripts.run_repair_eval import EXIT_HARD, EXIT_OK, main
from tests.repair_support import GOOD, SUPPRESSED, Scripted, StubRunner, l2_reply


@pytest.fixture(scope="module", autouse=True)
def checkpoint_schema(test_engine: Engine) -> None:
    ensure_checkpoint_schema(
        test_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    )


def run(engine: Engine, store: Path, phase: str, *extra: str, llm: Scripted | None = None) -> int:
    argv = [
        "--phase", phase, "--conditions", "L2R", "--scenarios", "S1", "--test-only",
        "--store-dir", str(store), "--database-url",
        engine.url.render_as_string(hide_password=False), *extra,
    ]  # fmt: skip
    return main(
        argv,
        llm_override=llm,
        runner_override=StubRunner(),
        smoke_runner_override=StubRunner(),
        oracle_override=lambda bundle, scenario: [{"category": "O1", "name": "a", "passed": True}],
    )


def shape(units: list[RepairUnitResult]) -> list[dict[str, object]]:
    return [expected_unit(u) for u in units]


@pytest.mark.parametrize("phase", ["fresh", "fixed"])
def test_a_recorded_run_replays_to_the_same_attempts(
    test_engine: Engine, tmp_path: Path, phase: str
) -> None:
    replies = ["not json", l2_reply(SUPPRESSED), l2_reply(GOOD)]
    if phase == "fixed":
        replies = [l2_reply(GOOD)]  # attempt 0 is the recorded v0.4 reply, not this script
    recorded = tmp_path / "recorded"
    live = Scripted(replies)
    assert run(test_engine, recorded, phase, llm=live) == EXIT_OK
    units = load_results(recorded / "results.jsonl")
    assert len(units) == 1 and units[0].status == "READY"

    out = tmp_path / "replays"
    assert export(recorded, out, allow_test_only=True) == (len(replies), 1)
    calls = [json.loads(x) for x in (out / "calls.jsonl").read_text("utf-8").splitlines()]
    assert all(c["usage"] is not None and c["total_tokens"] is not None for c in calls)
    if phase == "fixed":
        attempt0 = units[0].attempts[0]
        assert attempt0.source == "replay", "a fixed start's attempt 0 comes from the v0.4 replays"
        assert len(calls) == len(units[0].attempts) - 1, "and it is not exported again"

    replayed = tmp_path / "replayed"
    code = run(test_engine, replayed, phase, "--provider", "replay", "--replay-dir", str(out))
    assert code == EXIT_OK
    again = load_results(replayed / "results.jsonl")
    assert shape(again) == shape(units) == json.loads((out / "expected.json").read_text("utf-8"))


def test_a_replay_that_is_missing_a_reply_fails_loudly(test_engine: Engine, tmp_path: Path) -> None:
    recorded = tmp_path / "recorded"
    run(test_engine, recorded, "fresh", llm=Scripted(["not json", l2_reply(GOOD)]))
    out = tmp_path / "replays"
    export(recorded, out, allow_test_only=True)
    lines = (out / "calls.jsonl").read_text("utf-8").splitlines()
    (out / "calls.jsonl").write_text(lines[0] + "\n", encoding="utf-8")  # drop one reply
    code = run(test_engine, tmp_path / "again", "fresh", "--provider", "replay",
               "--replay-dir", str(out))  # fmt: skip
    assert code == EXIT_HARD
    assert load_results(tmp_path / "again" / "results.jsonl") == []


def test_results_of_a_test_only_run_are_not_exported_as_a_real_run(
    test_engine: Engine, tmp_path: Path
) -> None:
    recorded = tmp_path / "recorded"
    run(test_engine, recorded, "fresh", llm=Scripted([l2_reply(GOOD)]))
    with pytest.raises(ValueError, match="test-only"):
        export(recorded, tmp_path / "out")
    assert not (tmp_path / "out" / "expected.json").exists()
