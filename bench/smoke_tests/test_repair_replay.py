"""The committed repair replays reproduce their recorded outcomes offline, with the real sandbox.

Empty until the first real repair run is exported (``scripts/export_repair_replays.py``). Once a
phase is committed, every unit of it is replayed through the harness with the real gate, tests and
smoke test, and the attempts must match the recorded ones; a missing reply fails loudly.
"""

import json
from pathlib import Path

import pytest
from app.repair.service import ensure_checkpoint_schema
from sqlalchemy import Engine

from morph_bench.repair_eval import PHASES, load_results
from scripts.export_repair_replays import expected_unit
from scripts.run_repair_eval import EXIT_OK, main

pytestmark = pytest.mark.docker

REPLAYS = Path(__file__).resolve().parents[1] / "replays" / "repair"
COMMITTED = [p for p in PHASES if (REPLAYS / p / "expected.json").is_file()]


@pytest.fixture(scope="module", autouse=True)
def checkpoint_schema(test_engine: Engine) -> None:
    ensure_checkpoint_schema(
        test_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    )


def test_the_replay_directory_is_complete_or_explicitly_empty() -> None:
    if not COMMITTED:
        assert (REPLAYS / "README.md").is_file(), "an empty directory must say why"
        return
    for phase in COMMITTED:
        expected = json.loads((REPLAYS / phase / "expected.json").read_text("utf-8"))
        calls = (REPLAYS / phase / "calls.jsonl").read_text("utf-8").splitlines()
        hashes = [json.loads(line)["prompt_hash"] for line in calls]
        assert len(hashes) == len(set(hashes)) and expected, phase
        assert all("usage" in json.loads(line) for line in calls), "usage is stored with each reply"


@pytest.mark.skipif(not COMMITTED, reason="no real repair run has been recorded yet")
@pytest.mark.parametrize("phase", COMMITTED or ["none"])
def test_a_committed_phase_replays_to_the_recorded_attempts(
    test_engine: Engine, tmp_path: Path, phase: str
) -> None:
    expected = json.loads((REPLAYS / phase / "expected.json").read_text("utf-8"))
    argv = [
        "--phase", phase, "--provider", "replay", "--test-only",
        "--replay-dir", str(REPLAYS / phase), "--store-dir", str(tmp_path / "store"),
        "--database-url", test_engine.url.render_as_string(hide_password=False),
    ]  # fmt: skip
    assert main(argv, oracle_override=lambda bundle, scenario: None) == EXIT_OK
    replayed = load_results(tmp_path / "store" / "results.jsonl")
    got = sorted(
        (expected_unit(u) for u in replayed), key=lambda u: (u["scenario_id"], u["condition"])
    )
    assert got == expected
