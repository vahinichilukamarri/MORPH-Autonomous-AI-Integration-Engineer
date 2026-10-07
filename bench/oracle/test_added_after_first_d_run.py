"""Oracle cases added after the first D run (oracle revision r1, post-hoc).

The first version of the oracle did not cover these three situations. Their expected values are
hand-written in the fixtures (``added_after_first_d_run``) from the rules in the answer keys and
the fixture comments, not taken from what D did. Every check here is labelled ``r1-new``.
"""

from typing import Any

import pytest

from morph_bench.oracle.recorder import Checks
from morph_bench.oracle.world import Harness, RunResult

pytestmark = pytest.mark.docker
REVISION = "r1-new"


def by_key(records: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(r[key]): r for r in records}


def source_records(h: Harness) -> list[dict[str, Any]]:
    return [dict(r.source) for r in h.fx.records]


def added(h: Harness) -> dict[str, Any]:
    assert h.fx.added_after_first_d_run is not None
    return h.fx.added_after_first_d_run


def key_of(h: Harness, value: Any) -> str:
    return str(value)


def rewrite(h: Harness, key: Any, fields: dict[str, Any]) -> dict[str, Any]:
    return {"match_key": h.fx.source_key, "match_value": key, "set": fields}


def expected_after(h: Harness, failed_keys: set[str]) -> dict[str, dict[str, Any]]:
    """The hand-written final target state, except that records whose source failed are not
    written: they stay as they were (S3) or do not exist (S1, S4)."""
    fx = h.fx
    expected = by_key(fx.expected_target(), fx.target_key)
    initial = by_key(list(fx.initial_target), fx.target_key)
    for record in fx.records:
        if str(record.source[fx.source_key]) in failed_keys and record.expect is not None:
            target_key = str(record.expect[fx.target_key])
            if target_key in initial:
                expected[target_key] = initial[target_key]
            else:
                expected.pop(target_key)
    return expected


def run_with_rewrites(h: Harness, rewrites: list[dict[str, Any]]) -> RunResult:
    source = source_records(h)
    h.seed(source, [dict(r) for r in h.fx.initial_target])
    h.faults("source", rewrite_records=rewrites)
    return h.run(keys=h.keys_for(source))


def check_case(
    h: Harness, c: Checks, run: RunResult, cases: list[dict[str, Any]], label: str
) -> None:
    fx = h.fx
    failed: set[str] = set()
    by_report = run.by_key
    for case in cases:
        key = str(case["record"])
        got = by_report.get(key, {})
        c.ok(
            f"{label}:{key}:outcome_is_{case['outcome']}",
            got.get("outcome") == case["outcome"],
            f"{got}",
        )
        if case["outcome"] == "FAILED":
            failed.add(key)
            c.ok(
                f"{label}:{key}:category_is_{case['category']}",
                got.get("category") == case["category"],
                f"{got}",
            )
            c.ok(
                f"{label}:{key}:names_{case['field']}",
                case["field"] in got.get("fields", []),
                f"{got}",
            )
    for record in fx.records:
        key = str(record.source[fx.source_key])
        if key not in {str(case["record"]) for case in cases}:
            c.ok(
                f"{label}:{key}:other_records_unaffected",
                by_report.get(key, {}).get("outcome") == record.outcome,
                f"expected {record.outcome}, got {by_report.get(key)}",
            )
    c.ok("the_run_does_not_crash", run.report is not None and run.fatal is None, f"{run.fatal}")
    wanted = expected_after(h, failed)
    actual = by_key(h.state("target"), fx.target_key)
    c.ok(
        f"{label}:target_state_is_exactly_the_expected_one",
        actual == wanted,
        f"differs for {[k for k in set(actual) | set(wanted) if actual.get(k) != wanted.get(k)]}",
    )


def test_a_unknown_enum_value_from_the_source_fails_only_that_record(h: Harness) -> None:
    case = added(h)["unknown_enum"]
    run = run_with_rewrites(h, [rewrite(h, case["record"], case["rewrite"])])
    with Checks(h.fx.scenario, "O6", REVISION) as c:
        check_case(h, c, run, [case], "unknown_enum")
        c.ok("exit_code_is_partial", run.sandbox.exit_code == 3, f"exit {run.sandbox.exit_code}")


def test_c_a_created_at_that_is_not_an_iso_datetime_fails_that_record(h: Harness) -> None:
    cases = added(h)["bad_created_at"]
    run = run_with_rewrites(h, [rewrite(h, c["record"], c["rewrite"]) for c in cases])
    with Checks(h.fx.scenario, "O6", REVISION) as c:
        check_case(h, c, run, cases, "bad_created_at")
        failing = any(case["outcome"] == "FAILED" for case in cases)
        c.ok(
            "exit_code_matches_whether_any_record_failed",
            run.sandbox.exit_code == (3 if failing else 0),
            f"exit {run.sandbox.exit_code}",
        )


def test_b_the_same_source_key_twice_in_one_run_writes_once(h: Harness) -> None:
    fx = h.fx
    case = added(h)["duplicate_key"]
    source = source_records(h)
    h.seed(source, [dict(r) for r in fx.initial_target])
    keys = h.keys_for(source)
    if keys is not None:  # the operator lists the same id twice
        keys = [str(case["key"]), *keys]
    else:  # the CRM list repeats its first item
        h.faults("source", duplicate_first_list_item=True)
    run = h.run(keys=keys)
    records = (run.report or {}).get("records", [])
    with Checks(fx.scenario, "O2", REVISION) as c:
        c.ok("run_completed", run.report is not None and run.fatal is None, f"{run.fatal}")
        expected = dict(case["expected_counts"])
        counts = {k: v for k, v in run.counts.items() if v}
        c.ok("counts_are_exactly_the_expected_ones", counts == expected, f"{counts} != {expected}")
        c.ok(
            "the_key_is_reported_twice",
            sum(1 for r in records if str(r["key"]) == str(case["key"])) == 2,
            f"{[r['key'] for r in records]}",
        )
        writes = len(h.writes("target"))
        c.ok(
            "no_duplicate_create_or_update_request",
            writes == case["expected_writes"],
            f"{writes} write requests, expected {case['expected_writes']}",
        )
        wanted = by_key(fx.expected_target(), fx.target_key)
        actual = by_key(h.state("target"), fx.target_key)
        c.ok("target_state_is_exactly_the_expected_one", actual == wanted, "")
        c.ok("no_duplicate_target_records", len(h.state("target")) == len(wanted), "")
