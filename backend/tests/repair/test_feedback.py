"""Feedback: sorted, capped, deterministic, POSIX paths, and no secret or literal ever echoed."""

import random

from app.codegen import gate
from app.codegen.gate import Finding, GateResult, Rule, check_ast
from app.repair import feedback as fb
from app.repair.feedback import (
    MAX_CHARS,
    MAX_ITEMS,
    FeedbackItem,
    Stage,
    build_feedback,
    gate_items,
    generated_tests_items,
    guard_item,
    posix_path,
    smoke_item,
    validation_items,
)
from app.repair.guards import GuardFinding
from tests.codegen.fixtures import s1_input
from tests.repair.helpers import build

LONG = "REF-" + "A" * 41  # 45 characters: a run of 40 or more token characters


def item(stage: Stage = Stage.AST, code: str = "C", message: str = "m", file: str | None = None,
         line: int | None = None) -> FeedbackItem:  # fmt: skip
    return FeedbackItem(stage, code, message, file, line)


def test_items_are_sorted_by_stage_file_line_and_code() -> None:
    items = [
        item(Stage.SMOKE, "S"),
        item(Stage.AST, "B", file="integration/sync.py", line=9),
        item(Stage.AST, "A", file="integration/sync.py", line=2),
        item(Stage.PROPOSAL, "P"),
        item(Stage.GUARD, "G1"),
    ]
    result = build_feedback(1, items)
    assert [i.code for i in result.items] == ["P", "G1", "A", "B", "S"]


def test_same_content_gives_the_same_bytes_whatever_the_order() -> None:
    items = [item(Stage.AST, f"R{n}", f"message {n}", "integration/sync.py", n) for n in range(6)]
    shuffled = items[:]
    random.Random(7).shuffle(shuffled)
    first = build_feedback(2, items, history=[(0, ["A.B"]), (1, ["C.D"])])
    second = build_feedback(2, [*shuffled, *shuffled], history=[(0, ["A.B"]), (1, ["C.D"])])
    assert first.render() == second.render()


def test_the_item_count_and_the_character_budget_are_capped() -> None:
    many = [item(Stage.AST, f"RULE{n:03d}", "x" * 150, "integration/sync.py", n) for n in range(40)]
    result = build_feedback(1, many)
    assert len(result.items) <= MAX_ITEMS
    assert result.omitted == 40 - len(result.items)
    assert sum(len(i.render()) + 1 for i in result.items) <= MAX_CHARS
    assert f"{result.omitted} omitted" in result.render()
    # a single oversized item is still shown (messages are capped at 200 characters)
    one = build_feedback(1, [item(message="y" * 5000)])
    assert len(one.items) == 1 and len(one.items[0].message) <= 200


def test_paths_are_posix_and_relative_to_the_bundle() -> None:
    assert posix_path("integration\\sync.py") == "integration/sync.py"
    assert posix_path("/app/bundle/integration/sync.py") == "integration/sync.py"
    assert posix_path("./integration/sync.py") == "integration/sync.py"
    result = build_feedback(
        1, [item(file="integration\\sync.py", message="see C:\\work\\bundle\\integration\\sync.py")]
    )
    text = result.render()
    assert "\\" not in text and "integration/sync.py" in text


def test_the_syntax_error_file_name_is_not_part_of_the_message() -> None:
    result = build_feedback(1, [item(message="unmatched '}' (integration/sync.py, line 177)")])
    assert result.items[0].message == "unmatched '}'"


def test_configured_secret_values_and_opaque_strings_never_appear() -> None:
    secret = "s3cr3t-credential-value"
    opaque = "A1b2" * 12
    result = build_feedback(
        1,
        [item(message=f"saw {secret} and {opaque} here"), item(code=secret, message="ok")],
        secrets=[secret, "ab"],  # a very short value is not redacted: it would eat ordinary words
    )
    text = result.render()
    assert secret not in text and opaque not in text
    assert "[redacted]" in text and "[opaque string of 48 characters]" in text


def test_the_secret_pattern_is_the_gates_own() -> None:
    assert fb.SECRET_PATTERN.pattern == gate._SECRET.pattern


def _long_ref_files(field: str) -> tuple[dict[str, str], int]:
    inp = s1_input()
    edge = dict(inp.samples[0])
    edge["customer_id"] = "C-9001"
    edge[field] = LONG
    built = build(inp, extra_samples=(edge,))
    return built.files, len(inp.samples)


def test_a_secret_literal_in_an_edge_record_is_described_by_origin_and_length() -> None:
    files, base = _long_ref_files("email")
    findings = [f for f in check_ast(files).findings if f.rule is Rule.SECRET_LITERAL]
    assert len(findings) == 2, "the input value and the computed value both carry the string"
    result = build_feedback(1, gate_items(GateResult("ast", tuple(findings)), files, base))
    text = result.render()
    assert LONG not in text and "AAAAAAAA" not in text, "the literal is never echoed"
    assert "45 characters in edge record 0 (field 'email')" in text
    assert "the computed value for edge record 0 (field 'email_address')" in text
    assert all(i.code == "SECRET_LITERAL" and i.stage is Stage.AST for i in result.items)
    assert all(i.file == "tests_generated/cases.py" and i.line for i in result.items)


def test_a_secret_literal_elsewhere_gets_a_length_only_message() -> None:
    files = {"integration/sync.py": f'KEY = "{LONG}"\n'}
    finding = Finding("integration/sync.py", 1, Rule.SECRET_LITERAL, "string looks like a secret")
    (only,) = gate_items(GateResult("ast", (finding,)), files, base_cases=0)
    assert "a string of 45 characters" in only.message and LONG not in only.message


def test_ruff_findings_carry_the_ruff_code() -> None:
    finding = Finding("integration/sync.py", 4, Rule.LINT, "F401: `os` imported but unused")
    (only,) = gate_items(GateResult("ruff", (finding,)), {}, 0)
    assert (only.stage, only.code, only.message) == (Stage.RUFF, "F401", "`os` imported but unused")
    typed = Finding(
        "integration/sync.py", 9, Rule.TYPE_ERROR, "Incompatible return value [return-value]"
    )
    (only,) = gate_items(GateResult("mypy", (typed,)), {}, 0)
    assert (only.stage, only.code) == (Stage.MYPY, "TYPE_ERROR")


def test_validator_errors_become_one_item_per_problem() -> None:
    error = (
        "UPSERT: target_update_fields must equal target_create_fields; "
        "omit_if_null_fields: 'externalRef' is not optional and non-nullable"
    )
    first, second = validation_items(error)
    assert (first.stage, first.code) == (Stage.PROPOSAL, "UPSERT")
    assert second.code == "omit_if_null_fields" and "externalRef" in second.message
    (invalid,) = validation_items("<root>: Invalid JSON: expected value at line 1 column 1")
    assert invalid.code == "INVALID_JSON"
    assert validation_items("no colon here")[0].code == "INVALID"


def test_generated_test_failures_name_fields_and_never_echo_values() -> None:
    result = {
        "failures": [
            {
                "case": 3,
                "expected": {"a": 1, "b": "secret-ish"},
                "got": {"a": 2, "b": "secret-ish"},
            },
            {"case": 4, "expected": {"a": 1}, "got": {"__error__": "userId"}},
        ]
    }
    items = generated_tests_items("NONZERO", 1, result)
    text = build_feedback(1, items).render()
    assert "case 3: output differs in fields a" in text and "secret-ish" not in text
    assert "case 4: the transform raised on field userId" in text
    assert generated_tests_items("TIMEOUT", None, None)[0].code == "TIMEOUT"
    assert generated_tests_items("OK", 0, None)[0].code == "NO_RESULT"


def test_guard_and_smoke_items() -> None:
    guard = guard_item(
        GuardFinding("G2", "a base test case was changed", "tests_generated/cases.py")
    )
    assert (guard.stage, guard.code) == (Stage.GUARD, "G2")
    assert smoke_item("TIMEOUT", "did not finish").stage is Stage.SMOKE


def test_the_history_line_lists_earlier_codes_and_is_bounded() -> None:
    short = build_feedback(2, [item()], history=[(0, ["PROPOSAL.UPSERT"]), (1, ["AST.SYNTAX"])])
    assert "previous: attempt 0: PROPOSAL.UPSERT; attempt 1: AST.SYNTAX" in short.render()
    long = build_feedback(9, [item()], history=[(n, ["X" * 60]) for n in range(10)])
    assert len(long.history) <= fb.MAX_HISTORY_CHARS


def test_feedback_survives_a_round_trip_through_json() -> None:
    import json

    original = build_feedback(
        2,
        [item(Stage.AST, "A", "m1", "integration/sync.py", 3), item(Stage.SMOKE, "TIMEOUT", "m2")],
        history=[(0, ["PROPOSAL.INVALID_JSON"])],
    )
    restored = fb.Feedback.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored == original and restored.render() == original.render()
