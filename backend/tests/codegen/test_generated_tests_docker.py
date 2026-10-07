"""Generated tests run in the real sandbox after the full gate (marker: docker)."""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.codegen.sandbox import Outcome, SandboxRunner
from app.codegen.service import READY, generate_from_input, materialize, run_generated_tests
from app.mapping.confidence import ReviewStatus
from tests.codegen.fixtures import (
    S3_PIPELINES,
    S3_SEGMENT_OVERRIDE,
    mapped,
    persist_run,
    s1_input,
    s3_input,
)

pytestmark = pytest.mark.docker


def test_full_gate_then_generated_tests_pass(session: Session) -> None:
    runner = SandboxRunner()
    inp = s1_input()
    run_id = persist_run(
        session, source=("crm.v1", "crm"), target=("support.v1", "support"),
        source_entity="Customer", target_entity="User", fields=inp.fields,
    )  # fmt: skip
    from dataclasses import replace

    stored = replace(inp, mapping_run_id=run_id)
    version = generate_from_input(session, stored, runner=runner)
    assert version.status == READY, [(g.stage, g.findings) for g in version.gate_results]
    assert [(g.stage, g.passed) for g in version.gate_results] == [
        ("ast", True), ("ruff", True), ("mypy", True),
    ]  # fmt: skip
    row = run_generated_tests(session, version, runner)
    assert row.outcome == Outcome.OK.value and row.exit_code == 0
    assert row.result is not None
    assert row.result["total"] == len(inp.samples) > 0
    assert row.result["passed"] == row.result["total"] and row.result["failures"] == []
    assert row.purpose == "generated_tests" and row.limits["memory_mb"] == 256


def test_a_tampered_transform_fails_the_generated_tests(session: Session, tmp_path: Path) -> None:
    runner = SandboxRunner()
    inp = s1_input()
    run_id = persist_run(
        session, source=("crm.v1", "crm"), target=("support.v1", "support"),
        source_entity="Customer", target_entity="User", fields=inp.fields,
    )  # fmt: skip
    from dataclasses import replace

    version = generate_from_input(session, replace(inp, mapping_run_id=run_id))
    bundle = materialize(version, tmp_path)
    transform = bundle / "integration" / "transform.py"
    transform.write_text(
        transform.read_text()
        .replace("'+'", "'#'")
        .replace("strip_prefix(value, '+')", "strip_prefix(value, '#')")
    )
    transform.write_text(
        transform.read_text().replace("ops.cast(value, 'int')", "ops.cast(value, 'str')")
    )
    result = runner.run(bundle, ["python", "-E", "-s", "-B", "-m", "tests_generated"])
    assert result.outcome is Outcome.NONZERO and result.exit_code == 1
    report = result.json_stdout() if result.stdout.strip() else {}
    assert report["failures"], "the tamper must be detected"


def test_s3_bundle_passes_the_full_gate(session: Session) -> None:
    runner = SandboxRunner()
    inp = s3_input(mapped(S3_PIPELINES, segment=(ReviewStatus.OVERRIDDEN, S3_SEGMENT_OVERRIDE)))
    run_id = persist_run(
        session, source=("support.v1", "support"), target=("crm.v1", "crm"),
        source_entity="User", target_entity="Customer", fields=inp.fields,
    )  # fmt: skip
    from dataclasses import replace

    version = generate_from_input(session, replace(inp, mapping_run_id=run_id), runner=runner)
    assert version.status == READY, [(g.stage, g.findings) for g in version.gate_results]
    row = run_generated_tests(session, version, runner)
    assert row.outcome == "OK" and row.result is not None and row.result["failures"] == []
