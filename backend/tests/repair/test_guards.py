"""Guards G1 to G7 and no-op detection, including false-positive checks against D and a control."""

from collections.abc import Mapping
from pathlib import Path

import pytest

from app.repair.guards import (
    CASES_PATH,
    GUARD_MODES,
    GuardFinding,
    GuardInputs,
    GuardReport,
    Mode,
    check_any_loosening,
    check_record_mutation,
    check_required_fields,
    check_suppression,
    check_surface,
    check_swallowed_failures,
    check_tests_frozen,
    evaluate_guards,
    is_noop,
    output_hash_l1,
    output_hash_l2,
    type_escape_count,
)
from tests.codegen.fixtures import s1_input
from tests.repair.helpers import Built, build, control, s1_built, s3_approved

RUNTIME_SYNC = Path(__file__).resolve().parents[3] / "sandbox/runtime/morph_runtime/sync.py"
SYNC = "integration/sync.py"


def inputs_for(
    built: Built, *, owned: frozenset[str] = frozenset(), previous: Mapping[str, str] | None = None
) -> GuardInputs:
    names = {m.target_field for m in built.decision.included}
    required = {f.name for f in built.plan.target.create_fields if f.required} & names
    # a mapped identity the target assigns itself is never written, so it is not a "mapped field"
    writable = names & built.plan.target.writable
    return GuardInputs(
        files=built.files,
        rebuilt=dict(built.files),
        owned=owned,
        base_tests=built.base_tests,
        sources={p: t for p, t in built.files.items() if p.endswith(".py") and "integration/" in p},
        previous_sources=previous,
        strategy=built.strategy,
        required_on_create=required,
        included=writable,
    )


def codes(findings: list[GuardFinding]) -> list[str]:
    return [f.guard for f in findings]


# ---- modes ---------------------------------------------------------------------------------------


def test_g1_to_g5_are_enforced_and_g6_and_g7_are_shadow() -> None:
    assert {g: m.value for g, m in GUARD_MODES.items()} == {
        "G1": "ENFORCED", "G2": "ENFORCED", "G3": "ENFORCED", "G4": "ENFORCED", "G5": "ENFORCED",
        "G6": "SHADOW", "G7": "SHADOW",
    }  # fmt: skip


def test_shadow_findings_are_reported_and_never_reject() -> None:
    shadow = GuardReport((GuardFinding("G6", "pop() on 'x'"), GuardFinding("G7", "swallowed")))
    assert not shadow.rejected and len(shadow.shadow) == 2 and not shadow.enforced
    mixed = GuardReport((GuardFinding("G7", "swallowed"), GuardFinding("G2", "case dropped")))
    assert mixed.rejected and [f.guard for f in mixed.enforced] == ["G2"]
    assert all(f.mode is Mode.SHADOW for f in mixed.shadow)


# ---- false positives: D, the runtime's own engine, and the known-good control ----------------


@pytest.mark.parametrize("make", [s1_built, lambda: build(s3_approved())], ids=["S1", "S3"])
def test_no_guard_trips_on_ds_artifacts(make) -> None:  # type: ignore[no-untyped-def]
    built = make()
    report = evaluate_guards(inputs_for(built, previous=inputs_for(built).sources))
    assert report.findings == (), report.findings


def test_no_enforced_guard_trips_on_the_runtimes_own_sync_engine() -> None:
    source = RUNTIME_SYNC.read_text(encoding="utf-8")
    assert check_record_mutation(source, "morph_runtime/sync.py") == []
    assert check_swallowed_failures(source, "morph_runtime/sync.py") == []
    assert check_any_loosening({"x": source}, {"x": source}) == []
    assert check_suppression({"morph_runtime/sync.py": source}) == []


def test_no_guard_trips_on_the_known_good_l2_control() -> None:
    source = control("positive_sync")
    built = s1_built(source)
    report = evaluate_guards(inputs_for(built, owned=frozenset({SYNC}), previous={SYNC: source}))
    assert report.findings == (), report.findings


# ---- G1 ------------------------------------------------------------------------------------------


def test_g1_flags_a_changed_missing_or_extra_file_but_not_the_owned_one() -> None:
    rebuilt = {"integration/transform.py": "a", "integration/clients.py": "b", SYNC: "x"}
    files = {"integration/transform.py": "TAMPERED", SYNC: "y", "integration/extra.py": "z"}
    findings = check_surface(files, rebuilt, owned={SYNC})
    assert sorted((f.file, f.message) for f in findings) == [
        ("integration/clients.py", "a generated file is missing"),
        ("integration/extra.py", "unexpected file in the bundle"),
        ("integration/transform.py", "differs from its deterministic rebuild"),
    ]
    assert check_surface(rebuilt, rebuilt, owned=set()) == []


# ---- G2 ------------------------------------------------------------------------------------------


def test_g2_allows_appended_cases_and_nothing_else() -> None:
    inp = s1_input()
    base = build(inp)
    edge = dict(inp.samples[0])
    edge["first_name"] = "Zed"
    more = build(inp, extra_samples=(edge,))
    assert check_tests_frozen(more.files, base.base_tests) == []

    fewer = build(inp)
    cases = fewer.files[CASES_PATH]
    dropped = {**fewer.files, CASES_PATH: cases.replace("'record'", "'recorD'", 1)}
    assert [f.message for f in check_tests_frozen(dropped, base.base_tests)] == [
        "a base test case was changed"
    ]

    short = build(s1_input().__class__(**{**inp.__dict__, "samples": inp.samples[:-1]}))
    assert "test cases dropped" in check_tests_frozen(short.files, base.base_tests)[0].message

    changed = {**base.files, "tests_generated/__main__.py": "print('ok')\n"}
    assert [f.message for f in check_tests_frozen(changed, base.base_tests)] == [
        "a base test file was changed"
    ]
    added = {**base.files, "tests_generated/extra.py": ""}
    assert [f.message for f in check_tests_frozen(added, base.base_tests)] == [
        "a file was added to the generated tests"
    ]
    removed = {p: t for p, t in base.files.items() if p != "tests_generated/__main__.py"}
    assert [f.message for f in check_tests_frozen(removed, base.base_tests)] == [
        "a base test file is missing"
    ]


# ---- G3 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "comment", ["# noqa: F401", "# type: ignore", "# pragma: no cover", "# mypy: ignore-errors"]
)
def test_g3_is_the_gates_suppression_rule(comment: str) -> None:
    bad = {SYNC: f"x = 1  {comment}\n"}
    (finding,) = check_suppression(bad)
    assert (finding.guard, finding.file, finding.line) == ("G3", SYNC, 1)
    assert check_suppression({SYNC: "x = 1\n"}) == []


# ---- G4 ------------------------------------------------------------------------------------------

ONE_ANY = "from typing import Any\n\n\ndef f(a: Any) -> int:\n    return 1\n"
THREE_ESCAPES = (
    "from typing import Any, cast\n\n\ndef f(a: Any, b: object) -> Any:\n    return cast(Any, a)\n"
)


def test_g4_counts_any_cast_and_object_annotations() -> None:
    assert type_escape_count("def f(a: int) -> int:\n    return a\n") == 0
    assert type_escape_count(ONE_ANY) == 1
    assert type_escape_count(THREE_ESCAPES) == 5  # Any x3, object, cast
    assert type_escape_count("def broken(:\n") == 0


def test_g4_trips_only_when_escapes_rise_against_the_previous_attempt() -> None:
    assert check_any_loosening({SYNC: THREE_ESCAPES}, None) == []
    (finding,) = check_any_loosening({SYNC: THREE_ESCAPES}, {SYNC: ONE_ANY})
    assert (finding.guard, finding.message) == ("G4", "type escapes rose from 1 to 5")
    assert check_any_loosening({SYNC: ONE_ANY}, {SYNC: ONE_ANY}) == []
    assert check_any_loosening({SYNC: ONE_ANY}, {SYNC: THREE_ESCAPES}) == []
    assert check_any_loosening({SYNC: THREE_ESCAPES}, {"other.py": ONE_ANY}) == []


# ---- G5 ------------------------------------------------------------------------------------------

STRATEGY = {
    "target": {
        "mode": "CREATE_UPDATE",
        "create_fields": ("a", "b", "c"),
        "update_fields": ("b", "c"),
        "create_only": ("a",),
    }
}


def test_g5_passes_when_required_and_mapped_fields_are_written() -> None:
    assert check_required_fields(STRATEGY, {"a", "b"}, {"a", "b", "c"}) == []


def test_g5_flags_a_dropped_required_field_and_a_field_written_nowhere() -> None:
    dropped = {"target": {**STRATEGY["target"], "create_fields": ("b", "c")}}
    (finding,) = check_required_fields(dropped, {"a", "b"}, {"b", "c"})
    assert finding.message == "required fields not written on create: a"
    (nowhere,) = check_required_fields(STRATEGY, set(), {"a", "b", "c", "d"})
    assert nowhere.message == "mapped fields written nowhere: d"


def test_g5_requires_an_upsert_to_write_required_fields_on_replace() -> None:
    upsert = {"target": {"mode": "UPSERT", "create_fields": ("a", "b"), "update_fields": ("b",)}}
    (finding,) = check_required_fields(upsert, {"a"}, {"a", "b"})
    assert finding.message == "required fields not written on replace: a"


# ---- G6 and G7 (shadow) ---------------------------------------------------------------------

MUTATING = (
    "def run(record):\n"
    "    body = to_target(record)\n"
    "    del body['x']\n"
    "    body.pop('y')\n"
    "    body.clear()\n"
    "    other = {}\n"
    "    other.pop('z', None)\n"
)
SWALLOWING = (
    "def run():\n"
    "    try:\n        x = 1\n    except Exception:\n        pass\n"
    "    try:\n        y = 1\n    except:\n        ...\n"
    "    try:\n        z = 1\n    except ValueError:\n        pass\n"
    "    try:\n        w = 1\n    except Exception as error:\n"
    "        raise RuntimeError() from error\n"
)


def test_g6_flags_mutation_of_the_transformed_record_only() -> None:
    findings = check_record_mutation(MUTATING, SYNC)
    assert [(f.guard, f.line) for f in findings] == [("G6", 3), ("G6", 4), ("G6", 5)]
    assert check_record_mutation("def f(:\n", SYNC) == []


def test_g7_flags_broad_silent_handlers_only() -> None:
    findings = check_swallowed_failures(SWALLOWING, SYNC)
    assert [(f.guard, f.line) for f in findings] == [("G7", 4), ("G7", 8)]


def test_a_unit_that_trips_g6_and_g7_is_still_not_rejected() -> None:
    built = s1_built(control("positive_sync"))
    sources = {SYNC: MUTATING + SWALLOWING}
    report = evaluate_guards(
        GuardInputs(
            files=built.files,
            rebuilt=dict(built.files),
            owned=frozenset({SYNC}),
            base_tests=built.base_tests,
            sources=sources,
        )  # fmt: skip
    )
    assert {f.guard for f in report.shadow} == {"G6", "G7"}
    assert not report.rejected and report.enforced == ()


# ---- no-op detection -----------------------------------------------------------------------------


def test_only_blank_lines_and_trailing_whitespace_leave_the_l2_hash_unchanged() -> None:
    plain = "def run(keys):\n    return 1\n"
    padded = "def run(keys):   \n\n    return 1\n\n\n"
    other = "def run(keys):\n    return 2\n"
    assert output_hash_l2(plain) == output_hash_l2(padded) != output_hash_l2(other)
    assert output_hash_l2("def broken(:") == output_hash_l2("def broken(:\n\n")


def test_deleting_a_suppression_comment_is_a_new_output_not_a_repeat() -> None:
    suppressed = "def run(keys):\n    return 1  # noqa: E501\n"
    repaired = "def run(keys):\n    return 1\n"
    assert output_hash_l2(suppressed) != output_hash_l2(repaired)
    assert not is_noop(output_hash_l2(repaired), [output_hash_l2(suppressed)])


def test_the_l1_hash_ignores_key_order_and_edge_record_formatting() -> None:
    first = {"a": 1, "edge_record_json": ['{"x": 1, "y": 2}', "not json"]}
    second = {"edge_record_json": ['{"y":2,"x":1}', "not json"], "a": 1}
    assert output_hash_l1(first) == output_hash_l1(second)
    assert output_hash_l1(first) != output_hash_l1({**first, "a": 2})


def test_a_repeat_of_any_earlier_attempt_is_a_no_op_not_only_the_last() -> None:
    h0, h1, h2 = (output_hash_l2(f"def run(k):\n    return {n}\n") for n in range(3))
    assert is_noop(h0, [h0, h1])
    assert is_noop(h0, [h0, h1, h2]), "attempt 3 repeating attempt 0 stops"
    assert not is_noop(h2, [h0, h1])
    assert not is_noop(h0, [])


# ---- A13: what G5 must and must not reject -------------------------------------------------------


def g5_inputs(built: Built) -> tuple[dict[str, object], set[str], set[str]]:
    names = {m.target_field for m in built.decision.included}
    required = {f.name for f in built.plan.target.create_fields if f.required} & names
    return built.strategy, required, names & built.plan.target.writable


def test_g5_trips_on_a_strategy_that_drops_a_required_field_the_request_can_carry() -> None:
    strategy, required, included = g5_inputs(s1_built())
    target = dict(strategy["target"])  # type: ignore[call-overload]
    victim = sorted(required)[0]
    for key in ("create_fields", "update_fields"):
        target[key] = tuple(f for f in target[key] if f != victim)
    findings = check_required_fields({"target": target}, required, included)
    assert any(victim in f.message and f.guard == "G5" for f in findings), findings


def test_g5_does_not_trip_on_a_target_assigned_id_that_no_request_carries() -> None:
    built = build(s3_approved())
    strategy, required, included = g5_inputs(built)
    names = {m.target_field for m in built.decision.included}
    target = strategy["target"]
    assert "customer_id" in names, "the id is mapped (it identifies the record)"
    assert target["id_assigned_by_target"] is True  # type: ignore[index]
    assert "customer_id" not in target["create_fields"]  # type: ignore[index]
    assert "customer_id" not in target["update_fields"]  # type: ignore[index]
    assert "customer_id" not in included and "customer_id" not in required
    assert check_required_fields(strategy, required, included) == []
