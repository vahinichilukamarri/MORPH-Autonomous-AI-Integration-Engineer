"""bench/ mirrors the fault profile model; fail loudly if the two drift apart."""

import json
from pathlib import Path

from common.faults import FaultProfile

BENCH_SCHEMA = Path(__file__).resolve().parents[2] / "bench" / "schemas" / "scenario.schema.json"


def test_bench_fault_profile_matches_the_mock_systems_profile() -> None:
    bench = json.loads(BENCH_SCHEMA.read_text(encoding="utf-8"))["$defs"]["FaultProfile"]
    bench_defaults = {name: prop.get("default") for name, prop in bench["properties"].items()}
    assert bench_defaults == FaultProfile().model_dump()
