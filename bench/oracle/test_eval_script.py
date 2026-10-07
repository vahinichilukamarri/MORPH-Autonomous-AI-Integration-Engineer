"""The evaluation script end to end with a stand-in oracle (real database and sandbox, no model).

The oracle itself is exercised by test_oracle.py; here only the script's own behaviour is under
test: generation, gate, generated tests, resumability, blocked units and the L1 path with a
scripted provider.
"""

import json
from pathlib import Path

import pytest
from app.llm.fake import ScriptedFakeProvider
from sqlalchemy import Engine

from morph_bench.codegen_eval import load_results
from scripts.run_codegen_eval import main

pytestmark = pytest.mark.docker
S1 = "crm_customer_to_support_user"
S3 = "support_user_to_crm_customer"

CHECKS: list[dict[str, object]] = [
    {
        "scenario": "x",
        "category": "O1",
        "name": "a",
        "passed": True,
        "detail": "",
        "revision": "r0",
    },
    {
        "scenario": "x",
        "category": "O2",
        "name": "b",
        "passed": True,
        "detail": "",
        "revision": "r0",
    },
]

L1_REPLY = {
    "source_mode": "LIST", "source_key_field": "customer_id", "source_list_path": "/customers",
    "source_get_path": None, "source_page_param": "page", "source_size_param": "page_size",
    "source_items_key": "items", "source_total_key": "total", "target_mode": "UPSERT",
    "target_id_field": "userId", "target_get_path": "/users/{id}", "target_update_method": "PUT",
    "target_update_path": "/users/{id}", "target_create_path": None,
    "target_create_fields": ["userId", "externalRef", "fullName", "email_address", "phoneNumber", "accountState", "tier", "createdAt"],
    "target_update_fields": ["userId", "externalRef", "fullName", "email_address", "phoneNumber", "accountState", "tier", "createdAt"],
    "create_only_fields": [], "omit_if_null_fields": [], "natural_key_field": None,
    "id_assigned_by_target": False, "target_list_path": None, "target_page_param": None,
    "target_size_param": None, "target_items_key": None, "target_total_key": None,
    "edge_record_json": [], "rationale": "scripted",
}  # fmt: skip


def fake_oracle(calls: list[tuple[Path, str]]):  # type: ignore[no-untyped-def]
    def run(bundle: Path, scenario_id: str) -> list[dict[str, object]]:
        assert (bundle / "integration" / "__main__.py").is_file()
        calls.append((bundle, scenario_id))
        return CHECKS

    return run


def args(tmp_path: Path, engine: Engine, *extra: str) -> list[str]:
    return [
        "--test-only", "--store-dir", str(tmp_path / "store"), "--output", str(tmp_path / "r.md"),
        "--database-url", engine.url.render_as_string(hide_password=False), *extra,
    ]  # fmt: skip


def test_condition_d_unit_is_generated_gated_tested_graded_and_resumable(
    tmp_path: Path,
    test_engine: Engine,
) -> None:
    calls: list[tuple[Path, str]] = []
    argv = args(
        tmp_path, test_engine, "--scenarios", "S1", "--inputs", "approved", "--conditions", "D"
    )
    assert main(argv, oracle_override=fake_oracle(calls)) == 0
    [unit] = load_results(tmp_path / "store" / "results.jsonl")
    assert unit.status == "READY" and unit.gate == {"ast": True, "ruff": True, "mypy": True}
    assert unit.generated_tests is not None and unit.generated_tests.outcome == "OK"
    assert unit.generated_tests.total and unit.generated_tests.passed == unit.generated_tests.total
    assert unit.oracle == {"O1": (1, 1), "O2": (1, 1)} and unit.integration_correct is True
    assert unit.llm is None and unit.provider == "none"
    report = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "TEST-ONLY" in report and "S1 approved" in report
    assert len(calls) == 1
    assert main(argv, oracle_override=fake_oracle(calls)) == 0  # nothing is redone
    assert len(calls) == 1 and len(load_results(tmp_path / "store" / "results.jsonl")) == 1


def test_s3_approved_uses_the_recorded_human_decision(tmp_path: Path, test_engine: Engine) -> None:
    calls: list[tuple[Path, str]] = []
    argv = args(
        tmp_path, test_engine, "--scenarios", "S3", "--inputs", "approved", "--conditions", "D"
    )
    assert main(argv, oracle_override=fake_oracle(calls)) == 0
    [unit] = load_results(tmp_path / "store" / "results.jsonl")
    assert unit.status == "READY" and unit.integration_correct is True
    assert calls and calls[0][1] == S3


def test_l1_with_a_scripted_provider_records_the_model_use(
    tmp_path: Path,
    test_engine: Engine,
) -> None:
    calls: list[tuple[Path, str]] = []
    llm = ScriptedFakeProvider(replies=[json.dumps(L1_REPLY)])
    argv = args(
        tmp_path, test_engine, "--scenarios", "S1", "--inputs", "approved", "--conditions", "L1"
    )
    assert main(argv, llm_override=llm, oracle_override=fake_oracle(calls)) == 0
    [unit] = load_results(tmp_path / "store" / "results.jsonl")
    assert unit.condition == "L1" and unit.status == "READY"
    assert unit.llm is not None and unit.llm.calls == 1 and unit.llm.invalid_outputs == 0
    assert unit.provider == "scripted" and len(llm.calls) == 1


def test_a_real_provider_is_refused_without_the_explicit_go(
    tmp_path: Path,
    test_engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "--conditions",
            "L1",
            "--provider",
            "groq",
            "--store-dir",
            str(tmp_path / "s"),
            "--output",
            str(tmp_path / "r.md"),
        ]
    )
    assert code == 2 and "--confirm-real-run" in capsys.readouterr().err
