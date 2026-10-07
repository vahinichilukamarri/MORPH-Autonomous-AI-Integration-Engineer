"""The oracle: black-box tests of a generated integration against live mock systems.

Hand-written and hidden from the generator. Run with ``pytest oracle`` from ``bench/`` (needs
Docker, the sandbox and mock images, and the dev Postgres). Every check is recorded with its
category (O1 to O7 here, O8 in test_review_gate.py) and scenario in ``.cache/oracle-results.json``.

The oracle judges from the target system's own state and the mocks' request logs, not from the
integration's report; the report is only used where the behaviour under test is the report
itself (outcome labels, failure categories, exit codes).
"""

import math
from typing import Any

import pytest

from morph_bench.oracle import bulk
from morph_bench.oracle.recorder import Checks
from morph_bench.oracle.world import AUTH_HEADER, Harness, RunResult

pytestmark = pytest.mark.docker

SYNCED = ("CREATED", "UPDATED", "UNCHANGED")


# ---- helpers -----------------------------------------------------------------------------------


def by_key(records: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(r[key]): r for r in records}


def source_records(h: Harness) -> list[dict[str, Any]]:
    return [dict(r.source) for r in h.fx.records]


def first_run(h: Harness) -> RunResult:
    source = source_records(h)
    h.seed(source, [dict(r) for r in h.fx.initial_target])
    return h.run(keys=h.keys_for(source))


def clean_exit(run: RunResult) -> bool:
    return run.sandbox.exit_code == 0 and run.report is not None and run.fatal is None


def state_matches(h: Harness, expected: list[dict[str, Any]]) -> tuple[bool, str]:
    actual = by_key(h.state("target"), h.fx.target_key)
    wanted = by_key(expected, h.fx.target_key)
    if set(actual) != set(wanted):
        return (
            False,
            f"records differ: extra {sorted(set(actual) - set(wanted))}, missing {sorted(set(wanted) - set(actual))}",
        )
    wrong = [k for k in wanted if actual[k] != wanted[k]]
    return not wrong, f"records with wrong values: {wrong}" if wrong else ""


def terminated_by_itself(run: RunResult) -> bool:
    """The integration ended on its own (not killed by the sandbox) with a report."""
    return run.sandbox.outcome.value in ("OK", "NONZERO") and run.report is not None


# ---- O1: field-level correctness ---------------------------------------------------------------


def test_o1_field_correctness(h: Harness) -> None:
    fx = h.fx
    run = first_run(h)
    with Checks(fx.scenario, "O1") as c:
        c.ok(
            "run_completed_cleanly",
            clean_exit(run),
            f"exit {run.sandbox.exit_code}, fatal {run.fatal}, stderr {run.sandbox.stderr[:200]}",
        )
        for record in fx.records:
            key = str(record.source[fx.source_key])
            got = run.by_key.get(key)
            c.ok(
                f"{record.name}:outcome",
                got is not None and got["outcome"] == record.outcome,
                f"expected {record.outcome}, got {got}",
            )
        actual = by_key(h.state("target"), fx.target_key)
        for record in fx.records:
            if record.expect is not None:
                key = str(record.expect[fx.target_key])
                c.ok(
                    f"{record.name}:values",
                    actual.get(key) == record.expect,
                    f"expected {record.expect}, got {actual.get(key)}",
                )
        initial = by_key(list(fx.initial_target), fx.target_key)
        for key in fx.untouched_after:
            c.ok(
                f"untouched:{key}",
                actual.get(key) == initial[key],
                f"expected {initial[key]}, got {actual.get(key)}",
            )
        ok, detail = state_matches(h, fx.expected_target())
        c.ok("no_missing_extra_or_wrong_target_records", ok, detail)


# ---- O2: idempotency ---------------------------------------------------------------------------


def _mutation(h: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """(source key, changed source field, expected change in the target record)."""
    first = h.fx.records[0]
    key = str(first.source[h.fx.source_key])
    if h.fx.source_system == "crm":
        return (
            key,
            {"email": "asha.renamed@example.com"},
            {"email_address": "asha.renamed@example.com"},
        )
    return key, {"fullName": "Asha Verma-Shah"}, {"first_name": "Asha", "last_name": "Verma-Shah"}


def test_o2_idempotency(h: Harness) -> None:
    fx = h.fx
    run1 = first_run(h)
    state1 = by_key(h.state("target"), fx.target_key)
    synced = [r for r in fx.records if r.outcome in SYNCED]
    syncable = [r for r in fx.records if r.outcome != "NOT_SYNCABLE"]
    h.clear_requests()
    run2 = h.run(keys=h.keys_for(source_records(h)))
    with Checks(fx.scenario, "O2") as c:
        c.ok("first_run_ok", clean_exit(run1), f"fatal {run1.fatal}")
        c.ok("second_run_ok", clean_exit(run2), f"fatal {run2.fatal}")
        c.ok(
            "second_run_creates_and_updates_nothing",
            run2.counts.get("CREATED", 1) == 0 and run2.counts.get("UPDATED", 1) == 0,
            f"counts {run2.counts}",
        )
        c.ok(
            "second_run_reports_every_syncable_record_unchanged",
            run2.counts.get("UNCHANGED") == len(syncable),
            f"counts {run2.counts}, syncable {len(syncable)}",
        )
        c.ok(
            "second_run_issues_no_write_requests_to_the_target",
            h.writes("target") == [],
            f"{h.writes('target')[:3]}",
        )
        c.ok("second_run_issues_no_write_requests_to_the_source", h.writes("source") == [], "")
        c.ok(
            "target_state_identical_after_second_run",
            by_key(h.state("target"), fx.target_key) == state1,
            "",
        )
        c.ok(
            "not_syncable_stay_not_syncable",
            all(
                run2.by_key[str(r.source[fx.source_key])]["outcome"] == "NOT_SYNCABLE"
                for r in fx.records
                if r.outcome == "NOT_SYNCABLE"
            ),
            "",
        )
        # a changed source record updates in place, never duplicates
        key, source_change, target_change = _mutation(h)
        changed_source = [
            {**r, **source_change} if str(r[fx.source_key]) == key else r for r in source_records(h)
        ]
        h.admin("source", "PUT", "/__admin/state", {"records": changed_source})
        run3 = h.run(keys=h.keys_for(changed_source))
        after = by_key(h.state("target"), fx.target_key)
        target_key = str(
            next(
                r.expect[fx.target_key]
                for r in fx.records
                if str(r.source[fx.source_key]) == key and r.expect
            )
        )
        c.ok(
            "changed_record_is_updated_not_created",
            run3.counts.get("UPDATED") == 1 and run3.counts.get("CREATED", 1) == 0,
            f"counts {run3.counts}",
        )
        c.ok(
            "changed_value_reaches_the_target",
            all(after[target_key].get(k) == v for k, v in target_change.items()),
            f"{after[target_key]}",
        )
        c.ok(
            "no_duplicate_records_after_third_run",
            len(after) == len(state1),
            f"{len(state1)} -> {len(after)}",
        )
        c.ok("identity_is_stable_across_runs", set(after) == set(state1), "")
        c.ok(
            "other_records_untouched_by_third_run",
            all(after[k] == state1[k] for k in state1 if k != target_key),
            "",
        )
        _ = synced


# ---- O3: authentication ------------------------------------------------------------------------


def test_o3_authentication(h: Harness) -> None:
    fx = h.fx
    source = source_records(h)
    initial = [dict(r) for r in fx.initial_target]
    keys = h.keys_for(source)
    with Checks(fx.scenario, "O3") as c:
        # a normal run first: the right header kind is used for each system
        h.seed(source, initial)
        good = h.run(keys=keys)
        c.ok("good_credentials_work", clean_exit(good), f"fatal {good.fatal}")
        for role in ("source", "target"):
            log = h.requests(role)
            kinds = {e["auth_header"] for e in log}
            c.ok(
                f"{role}_uses_{AUTH_HEADER[h.system(role)]}_header",
                kinds == {AUTH_HEADER[h.system(role)]},
                f"saw {kinds}",
            )
        # wrong source credential
        h.seed(source, initial)
        bad_source = h.run(keys=keys, source_credential="wrong-source-credential")
        c.ok(
            "wrong_source_credential_is_a_fatal_auth_failure",
            bad_source.fatal is not None
            and bad_source.fatal["category"] == "AUTH"
            and bad_source.sandbox.exit_code == 2,
            f"{bad_source.fatal}, exit {bad_source.sandbox.exit_code}",
        )
        c.ok(
            "wrong_source_credential_one_attempt",
            [e["status"] for e in h.requests("source")] == [401],
            f"{[e['status'] for e in h.requests('source')]}",
        )
        c.ok("wrong_source_credential_nothing_sent_to_target", h.requests("target") == [], "")
        # wrong target credential
        h.seed(source, initial)
        bad_target = h.run(keys=keys, target_credential="wrong-target-credential")
        c.ok(
            "wrong_target_credential_is_a_fatal_auth_failure",
            bad_target.fatal is not None
            and bad_target.fatal["category"] == "AUTH"
            and bad_target.sandbox.exit_code == 2,
            f"{bad_target.fatal}",
        )
        target_statuses = [e["status"] for e in h.requests("target")]
        c.ok("wrong_target_credential_one_attempt", target_statuses == [401], f"{target_statuses}")
        c.ok(
            "wrong_target_credential_nothing_written",
            h.writes("target") == []
            and by_key(h.state("target"), fx.target_key) == by_key(initial, fx.target_key),
            "",
        )
        # missing credentials
        h.seed(source, initial)
        missing = h.run(keys=keys, source_credential="")
        c.ok(
            "missing_credential_is_a_configuration_failure_without_any_request",
            missing.fatal is not None
            and missing.fatal["category"] == "CONFIGURATION"
            and h.requests("source") == []
            and h.requests("target") == [],
            f"{missing.fatal}",
        )
        h.seed(source, initial)
        missing_target = h.run(keys=keys, target_credential="")
        c.ok(
            "missing_target_credential_is_a_configuration_failure",
            missing_target.fatal is not None
            and missing_target.fatal["category"] == "CONFIGURATION"
            and h.writes("target") == [],
            f"{missing_target.fatal}",
        )


# ---- O4: pagination and volume -----------------------------------------------------------------


def _bulk(
    h: Harness, n: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(source records, initial target, expected final target)."""
    if h.fx.source_system == "crm":
        customers = [bulk.customer(i) for i in range(n)]
        return customers, [], [bulk.support_user_for(c) for c in customers]
    return (
        [bulk.support_user(i) for i in range(n)],
        [bulk.crm_customer_for(i) for i in range(n)],
        [bulk.crm_customer_after(i) for i in range(n)],
    )


@pytest.mark.parametrize("n", [0, 1, 100, 101, 250])
def test_o4_pagination(h: Harness, n: int) -> None:
    fx = h.fx
    if fx.keys_mode and n in (100, 250):
        pytest.skip("keys mode: sizes 0, 1 and 101 are used")
    source, initial, expected = _bulk(h, n)
    h.seed(source, initial)
    run = h.run(keys=h.keys_for(source))
    with Checks(fx.scenario, "O4") as c:
        c.ok(
            f"n={n}:run_completed_cleanly",
            clean_exit(run),
            f"fatal {run.fatal}, exit {run.sandbox.exit_code}, {run.sandbox.stderr[:200]}",
        )
        wanted = "UPDATED" if fx.keys_mode else "CREATED"
        c.ok(
            f"n={n}:every_record_synced_exactly_once",
            run.counts.get(wanted) == n and sum(run.counts.values()) == n,
            f"counts {run.counts}",
        )
        ok, detail = state_matches(h, expected)
        c.ok(f"n={n}:target_holds_exactly_the_expected_records", ok, detail)
        if not fx.keys_mode:
            pages = [
                e
                for e in h.requests("source")
                if e["method"] == "GET" and e["path"] == "/customers"
            ]
            c.ok(
                f"n={n}:no_extra_page_requests",
                len(pages) == max(1, math.ceil(n / 100)),
                f"{len(pages)} list requests",
            )


# ---- O5: fault injection -----------------------------------------------------------------------

TIMEOUT_ENV = {"MORPH_RUN_BUDGET_S": "12", "MORPH_HTTP_TIMEOUT_S": "2"}


def _converges(h: Harness, c: Checks, label: str, run: RunResult) -> None:
    c.ok(
        f"{label}:run_ended_by_itself_with_a_report",
        terminated_by_itself(run),
        f"outcome {run.sandbox.outcome}, {run.sandbox.stderr[:200]}",
    )
    c.ok(f"{label}:within_the_time_limit", run.elapsed_s < 58, f"{run.elapsed_s:.1f}s")
    h.clear_faults()
    again = h.run(keys=h.keys_for(source_records(h)))
    ok, detail = state_matches(h, h.fx.expected_target())
    c.ok(
        f"{label}:converges_to_the_expected_state_without_duplicates",
        ok and again.fatal is None,
        detail or f"fatal {again.fatal}",
    )


@pytest.mark.parametrize(
    "name",
    [
        "http_500_on_target",
        "http_500_on_source",
        "http_429_on_target",
        "malformed_json_on_target",
        "latency_on_both",
    ],
)
def test_o5_transient_faults(h: Harness, name: str) -> None:
    fx = h.fx
    source = source_records(h)
    h.seed(source, [dict(r) for r in fx.initial_target])
    with Checks(fx.scenario, "O5") as c:
        if name == "http_500_on_target":
            h.faults("target", http_500_rate=0.3, seed=7)
        elif name == "http_500_on_source":
            # oracle revision r1 (post-hoc): was http_500_rate=0.3, seed=11, which never fired
            # on the one list request S1 and S4 make; now the first request is failed for sure
            h.faults("source", http_500_first_n=1)
        elif name == "http_429_on_target":
            h.faults("target", http_429_rate=0.5, retry_after_seconds=1, seed=3)
        elif name == "malformed_json_on_target":
            h.faults("target", malformed_json_rate=0.3, seed=5)
        else:
            h.faults("source", latency_ms=150)
            h.faults("target", latency_ms=150)
        run = h.run(keys=h.keys_for(source))
        if name == "http_429_on_target":
            log = h.requests("target")
            gaps = [
                log[i + 1]["at"] - e["at"] for i, e in enumerate(log[:-1]) if e["status"] == 429
            ]
            c.ok(
                "http_429:faults_were_actually_injected",
                len(gaps) > 0,
                "no 429 was seen; the check proves nothing",
            )
            c.ok(
                "http_429:retry_after_is_honoured",
                all(g >= 0.8 for g in gaps),
                f"smallest gap {min(gaps, default=0):.2f}s",
            )
        if name == "latency_on_both":
            c.ok("latency:completes_cleanly", clean_exit(run), f"fatal {run.fatal}")
            ok, detail = state_matches(h, fx.expected_target())
            c.ok("latency:state_is_correct", ok, detail)
            c.ok("latency:within_the_time_limit", run.elapsed_s < 58, f"{run.elapsed_s:.1f}s")
        else:
            # oracle revision r1 (post-hoc): injection is proven by the mock's fault counter
            role, counter = {
                "http_500_on_target": ("target", "http_500"),
                "http_500_on_source": ("source", "http_500"),
                "http_429_on_target": ("target", "http_429"),
                "malformed_json_on_target": ("target", "malformed_json"),
            }[name]
            injected = h.counters(role)[counter]
            c.ok(
                f"{name}:faults_were_actually_injected",
                injected > 0,
                f"the {role} mock injected {injected} {counter} faults; the check proves nothing",
                revision="r1-fix",
            )
            _converges(h, c, name, run)


def test_o5_a_dropped_source_field_is_not_silently_treated_as_null(h: Harness) -> None:
    fx = h.fx
    source = source_records(h)
    initial = [dict(r) for r in fx.initial_target]
    h.seed(source, initial)
    dropped = "last_name" if fx.source_system == "crm" else "fullName"
    h.faults("source", drop_fields=[dropped])
    run = h.run(keys=h.keys_for(source))
    with Checks(fx.scenario, "O5") as c:
        c.ok(
            "drop_fields:stops_with_contract_drift",
            run.fatal is not None
            and run.fatal["category"] == "CONTRACT_DRIFT"
            and run.sandbox.exit_code == 2,
            f"{run.fatal}, exit {run.sandbox.exit_code}",
        )
        c.ok(
            "drop_fields:nothing_written",
            h.writes("target") == []
            and by_key(h.state("target"), fx.target_key) == by_key(initial, fx.target_key),
            "",
        )


@pytest.mark.parametrize("role", ["target", "source"])
def test_o5_timeouts_end_cleanly(h: Harness, role: str) -> None:
    fx = h.fx
    source = source_records(h)
    initial = [dict(r) for r in fx.initial_target]
    h.seed(source, initial)
    h.faults(role, timeout=True, timeout_seconds=30)
    run = h.run(keys=h.keys_for(source), extra_env=TIMEOUT_ENV)
    with Checks(fx.scenario, "O5") as c:
        c.ok(
            f"timeout_on_{role}:ends_by_itself_with_a_fatal_timeout",
            terminated_by_itself(run)
            and run.fatal is not None
            and run.fatal["category"] == "TIMEOUT"
            and run.sandbox.exit_code == 2,
            f"outcome {run.sandbox.outcome}, fatal {run.fatal}",
        )
        c.ok(
            f"timeout_on_{role}:well_inside_the_sandbox_limit",
            run.elapsed_s < 45,
            f"{run.elapsed_s:.1f}s",
        )
        c.ok(
            f"timeout_on_{role}:nothing_written",
            by_key(h.state("target"), fx.target_key) == by_key(initial, fx.target_key),
            "",
        )
    h.clear_faults()


# ---- O6: failure mapping -----------------------------------------------------------------------


def test_o6_failure_mapping(h: Harness) -> None:
    fx = h.fx
    plain = fx.records[0]
    extra: list[dict[str, Any]] = [
        dict(r.source) for r in fx.records if r.outcome == "NOT_SYNCABLE"
    ]
    source = [dict(plain.source), *extra, *[dict(r.source) for r in fx.rejects]]
    target = [dict(r) for r in fx.initial_target] + [dict(r) for r in fx.reject_target_extra]
    keys = h.keys_for(source)
    if keys is not None and fx.missing_source_key is not None:
        keys.append(str(fx.missing_source_key))
    h.seed(source, target)
    run = h.run(keys=keys)
    actual = by_key(h.state("target"), fx.target_key)
    with Checks(fx.scenario, "O6") as c:
        c.ok(
            "run_finishes_with_a_partial_report",
            run.status == "PARTIAL" and run.sandbox.exit_code == 3,
            f"status {run.status}, exit {run.sandbox.exit_code}, fatal {run.fatal}",
        )
        plain_key = str(plain.source[fx.source_key])
        c.ok(
            "the_valid_record_still_syncs",
            run.by_key.get(plain_key, {}).get("outcome") == plain.outcome,
            f"{run.by_key.get(plain_key)}",
        )
        for reject in fx.rejects:
            got = run.by_key.get(str(reject.source[fx.source_key]), {})
            c.ok(
                f"{reject.name}:failed_as_validation",
                got.get("outcome") == "FAILED" and got.get("category") == "VALIDATION",
                f"{got}",
            )
            c.ok(
                f"{reject.name}:names_the_field_{reject.field}",
                reject.field in got.get("fields", []),
                f"{got}",
            )
        for record in fx.records:
            if record.outcome == "NOT_SYNCABLE":
                got = run.by_key.get(str(record.source[fx.source_key]), {})
                c.ok(
                    f"{record.name}:reported_not_syncable",
                    got.get("outcome") == "NOT_SYNCABLE",
                    f"{got}",
                )
        if fx.missing_source_key is not None:
            got = run.by_key.get(str(fx.missing_source_key), {})
            c.ok(
                "missing_source_record:failed_as_not_found",
                got.get("outcome") == "FAILED" and got.get("category") == "NOT_FOUND",
                f"{got}",
            )
        if fx.keys_mode:
            initial = by_key(target, fx.target_key)
            c.ok(
                "rejected_target_record_unchanged",
                all(
                    actual[str(e[fx.target_key])] == initial[str(e[fx.target_key])]
                    for e in fx.reject_target_extra
                ),
                "",
            )
            c.ok(
                "nothing_was_created",
                set(actual) == set(initial),
                f"extra {set(actual) - set(initial)}",
            )
        else:
            c.ok(
                "only_valid_records_were_created",
                set(actual) == {str(plain.expect[fx.target_key])} if plain.expect else False,
                f"{sorted(actual)}",
            )


# ---- O7: contract drift ------------------------------------------------------------------------


def test_o7_contract_drift(h: Harness) -> None:
    fx = h.fx
    source = source_records(h)
    initial = [dict(r) for r in fx.initial_target]
    other = "v1" if fx.contract == "v2" else "v2"
    with Checks(fx.scenario, "O7") as c:
        if fx.source_system == "crm":
            # the source drifted: a field the mapping reads has been renamed
            h.seed(source, initial, source_contract=other)
            run = h.run(keys=h.keys_for(source))
            c.ok(
                "drifted_source:contract_drift_and_exit_2",
                run.fatal is not None
                and run.fatal["category"] == "CONTRACT_DRIFT"
                and run.sandbox.exit_code == 2,
                f"{run.fatal}, exit {run.sandbox.exit_code}",
            )
            c.ok(
                "drifted_source:no_request_reaches_the_target",
                h.requests("target") == [],
                f"{len(h.requests('target'))} requests",
            )
            # the target drifted: it now rejects the old field name
            h.seed(source, initial, target_contract=other)
            run = h.run(keys=h.keys_for(source))
            failed = (
                [r for r in run.report["records"] if r["outcome"] == "FAILED"] if run.report else []
            )
            c.ok(
                "drifted_target:every_record_fails_as_validation",
                len(failed) == len(fx.records)
                and all(r["category"] == "VALIDATION" for r in failed),
                f"counts {run.counts}",
            )
            c.ok(
                "drifted_target:nothing_is_written",
                h.state("target") == [],
                f"{h.state('target')[:2]}",
            )
            c.ok(
                "drifted_target:run_does_not_crash",
                terminated_by_itself(run) and run.sandbox.exit_code == 3,
                f"exit {run.sandbox.exit_code}",
            )
        else:
            # Support v2 only renames `tier`, which this mapping never reads: no false alarm
            h.seed(source, initial, source_contract="v2")
            run = h.run(keys=h.keys_for(source))
            ok, detail = state_matches(h, fx.expected_target())
            c.ok(
                "unrelated_source_drift:no_false_alarm",
                clean_exit(run) and ok,
                detail or f"fatal {run.fatal}",
            )
            # CRM v2 renames phone: updates that carry a phone must fail cleanly, never half-apply
            h.seed(source, initial, target_contract="v2")
            run = h.run(keys=h.keys_for(source))
            actual = by_key(h.state("target"), fx.target_key)
            before = by_key(initial, fx.target_key)
            after = by_key(fx.expected_target(), fx.target_key)
            c.ok(
                "drifted_target:every_record_is_either_old_or_fully_new",
                all(actual[k] in (before[k], after[k]) for k in before),
                "",
            )
            c.ok(
                "drifted_target:failures_are_validation_errors",
                all(
                    r["category"] == "VALIDATION"
                    for r in (run.report or {}).get("records", [])
                    if r["outcome"] == "FAILED"
                ),
                f"{run.counts}",
            )
            # oracle revision r1 (post-hoc): the two checks that were here demanded per-record
            # FAILED(VALIDATION) and a non-fatal run. The plan's O7 is "ends CONTRACT_DRIFT with
            # zero writes", so they are replaced by safety assertions.
            c.ok(
                "drifted_target:ends_with_contract_drift_and_exit_2",
                run.fatal is not None
                and run.fatal["category"] == "CONTRACT_DRIFT"
                and run.sandbox.exit_code == 2,
                f"{run.fatal}, exit {run.sandbox.exit_code}",
                revision="r1-fix",
            )
            written = len(h.writes("target"))
            reported = run.counts.get("UPDATED", 0) + run.counts.get("CREATED", 0)
            c.ok(
                "drifted_target:no_write_beyond_the_records_reported_before_the_drift",
                written == reported,
                f"{written} write requests, {reported} records reported as written",
                revision="r1-fix",
            )
