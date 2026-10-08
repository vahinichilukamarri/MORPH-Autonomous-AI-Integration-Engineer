"""G4 with the recorded v0.4 L2 replies as the previous attempt (amendment A14).

Two of the three recorded L2 replies (S4 and S3) end with a stray closing brace, so their source
does not parse: they have no type-escape count and cannot be a baseline. The first recorded reply
(S1) is a full module with many ``Any``: under the old rule it would have been rejected after either.
"""

import ast
import json
from pathlib import Path

import pytest
from app.repair.guards import check_any_loosening, type_escape_count

CALLS = Path(__file__).resolve().parents[1] / "replays" / "codegen" / "calls.jsonl"
SYNC = "integration/sync.py"


def l2_sources() -> dict[str, str]:
    records = [json.loads(line) for line in CALLS.read_text(encoding="utf-8").splitlines()]
    # the L2 replies are the third of each unit's calls: S1, S4, S3 in the order they were run
    return {
        "S1": json.loads(records[2]["text"])["source"],
        "S4": json.loads(records[5]["text"])["source"],
        "S3": json.loads(records[8]["text"])["source"],
    }


@pytest.mark.parametrize("unit", ["S4", "S3"])
def test_the_recorded_stray_brace_replies_have_no_baseline(unit: str) -> None:
    source = l2_sources()[unit]
    with pytest.raises(SyntaxError):
        ast.parse(source)
    assert type_escape_count(source) is None


def test_a_full_module_after_a_recorded_unparseable_one_does_not_trip_g4() -> None:
    sources = l2_sources()
    full = type_escape_count(sources["S1"])
    assert full is not None and full > 0, "the negative control means something: it has escapes"
    for unit in ("S4", "S3"):
        assert check_any_loosening({SYNC: sources["S1"]}, {SYNC: sources[unit]}) == []


def test_the_positive_control_a_real_increase_between_parseable_modules_still_trips() -> None:
    full = l2_sources()["S1"]
    more = full + "\n\ndef extra(a: object) -> object:\n    return a\n"
    (finding,) = check_any_loosening({SYNC: more}, {SYNC: full})
    assert finding.guard == "G4" and "rose from" in finding.message
