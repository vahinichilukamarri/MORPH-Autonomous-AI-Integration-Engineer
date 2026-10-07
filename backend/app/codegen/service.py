"""Generate, gate and persist an integration version (condition D: deterministic, no LLM)."""

import json
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.codegen.bundle import (
    build_manifest,
    bundle_hash,
    input_hash,
    sha256_text,
    write_bundle,
)
from app.codegen.gate import GateResult, check_ast
from app.codegen.gate_tools import run_tool_gate
from app.codegen.generated_tests import TESTS_PACKAGE, render_tests
from app.codegen.generator import GENERATOR_VERSION, RUNTIME_VERSION, generate_package
from app.codegen.inputs import CodegenInput, load_input
from app.codegen.operations import OperationPlan, PlanError, analyse
from app.codegen.review_gate import GateDecision, GateStatus, decide
from app.codegen.sandbox import Outcome, SandboxRunner
from app.db_models import (
    GateResultRow,
    Integration,
    IntegrationFile,
    IntegrationVersion,
    SandboxRunRow,
)

CONDITIONS = ("D",)

BLOCKED_PENDING_REVIEW = "BLOCKED_PENDING_REVIEW"
BLOCKED_UNSUPPORTED = "BLOCKED_UNSUPPORTED"
GATE_FAILED = "GATE_FAILED"
GENERATED = "GENERATED"
GENERATED_PARTIAL = "GENERATED_PARTIAL"
READY = "READY"
READY_PARTIAL = "READY_PARTIAL"
RUNNABLE = frozenset({READY, READY_PARTIAL})


class GenerationError(Exception):
    pass


def _gate_rows(results: list[GateResult]) -> list[GateResultRow]:
    return [
        GateResultRow(stage=r.stage, passed=r.passed, findings=[f.as_dict() for f in r.findings])
        for r in results
    ]


def _get_or_create_integration(session: Session, inp: CodegenInput, condition: str) -> Integration:
    assert inp.mapping_run_id is not None
    found = session.scalar(
        select(Integration).where(
            Integration.mapping_run_id == inp.mapping_run_id, Integration.condition == condition
        )
    )
    if found is not None:
        return found
    integration = Integration(
        mapping_run_id=inp.mapping_run_id,
        condition=condition,
        name=f"{inp.source.name}.{inp.source_entity} -> {inp.target.name}.{inp.target_entity}",
    )
    session.add(integration)
    session.flush()
    return integration


def _persist(
    session: Session,
    integration: Integration,
    inp: CodegenInput,
    *,
    status: str,
    in_hash: str,
    files: dict[str, str],
    manifest: dict[str, Any],
    gates: list[GateResult],
) -> IntegrationVersion:
    last = session.scalar(
        select(IntegrationVersion)
        .where(IntegrationVersion.integration_id == integration.id)
        .order_by(IntegrationVersion.version.desc())
        .limit(1)
    )
    version = IntegrationVersion(
        integration_id=integration.id,
        version=(last.version + 1) if last else 1,
        status=status,
        input_hash=in_hash,
        bundle_hash=bundle_hash(files) if files else None,
        generator_version=GENERATOR_VERSION,
        runtime_version=RUNTIME_VERSION,
        mapping_version_ids=[m.mapping_version_id for m in inp.fields],
        manifest={**manifest, "status": status},
    )
    session.add(version)
    session.flush()
    for path, text in files.items():
        session.add(
            IntegrationFile(
                integration_version_id=version.id,
                path=path,
                sha256=sha256_text(text),
                kind="test" if path.startswith(f"{TESTS_PACKAGE}/") else "generated",
                content=text,
            )
        )
    for row in _gate_rows(gates):
        row.integration_version_id = version.id
        session.add(row)
    session.flush()
    return version


def _status_after_gates(partial: bool, passed_ast: bool, tools: list[GateResult] | None) -> str:
    if not passed_ast or (tools is not None and not all(t.passed for t in tools)):
        return GATE_FAILED
    if tools is None:
        return GENERATED_PARTIAL if partial else GENERATED
    return READY_PARTIAL if partial else READY


def generate_from_input(
    session: Session,
    inp: CodegenInput,
    *,
    condition: str = "D",
    allow_partial: bool = False,
    runner: SandboxRunner | None = None,
) -> IntegrationVersion:
    """Generate an integration version for ``inp``. Reuses the latest version if nothing changed."""
    if condition not in CONDITIONS:
        raise GenerationError(f"condition {condition!r} is not available")
    integration = _get_or_create_integration(session, inp, condition)
    in_hash = input_hash(inp, condition, allow_partial=allow_partial)
    latest = session.scalar(
        select(IntegrationVersion)
        .where(IntegrationVersion.integration_id == integration.id)
        .order_by(IntegrationVersion.version.desc())
        .limit(1)
    )
    if (
        latest is not None
        and latest.input_hash == in_hash
        and (latest.status in RUNNABLE or latest.status.startswith("BLOCKED") or runner is None)
    ):
        return latest

    def blocked(
        status: str, decision: GateDecision | None, error: str | None
    ) -> IntegrationVersion:
        manifest = build_manifest(
            inp, condition=condition, status=status, decision=decision, strategy=None, files={},
            plan_error=error,
        )  # fmt: skip
        return _persist(
            session, integration, inp, status=status, in_hash=in_hash, files={}, manifest=manifest,
            gates=[],
        )  # fmt: skip

    try:
        plan: OperationPlan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    except PlanError as error:
        return blocked(BLOCKED_UNSUPPORTED, None, str(error))
    decision = decide(inp, plan, allow_partial=allow_partial)
    if decision.status is GateStatus.BLOCKED:
        return blocked(BLOCKED_PENDING_REVIEW, decision, None)
    try:
        package = generate_package(inp, plan, decision)
    except PlanError as error:
        return blocked(BLOCKED_UNSUPPORTED, decision, str(error))

    files = {**package.files, **render_tests(decision.included, inp.samples)}
    gates = [check_ast(files)]
    tools: list[GateResult] | None = None
    manifest = build_manifest(
        inp, condition=condition, status=GENERATED, decision=decision, strategy=package.strategy,
        files=files,
    )  # fmt: skip
    if gates[0].passed and runner is not None:
        with tempfile.TemporaryDirectory(prefix="morph-gate-") as tmp:
            write_bundle(Path(tmp), files, manifest)
            tools = run_tool_gate(runner, Path(tmp))
        gates.extend(tools)
    partial = decision.status is GateStatus.PARTIAL
    status = _status_after_gates(partial, gates[0].passed, tools)
    return _persist(
        session, integration, inp, status=status, in_hash=in_hash, files=files,
        manifest=manifest, gates=gates,
    )  # fmt: skip


def generate(
    session: Session,
    mapping_run_id: int,
    *,
    condition: str = "D",
    allow_partial: bool = False,
    runner: SandboxRunner | None = None,
    samples_dir: Path | None = None,
) -> IntegrationVersion:
    return generate_from_input(
        session,
        load_input(session, mapping_run_id, samples_dir),
        condition=condition,
        allow_partial=allow_partial,
        runner=runner,
    )


def materialize(version: IntegrationVersion, directory: Path) -> Path:
    """Write a stored version's files and manifest to ``directory`` for the sandbox."""
    files = {f.path: f.content for f in version.files}
    if not files:
        raise GenerationError(f"version {version.version} has no code ({version.status})")
    return write_bundle(directory, files, version.manifest)


def run_generated_tests(
    session: Session, version: IntegrationVersion, runner: SandboxRunner
) -> SandboxRunRow:
    """Run the generated tests in the sandbox (no network) and store the result."""
    if version.status not in RUNNABLE:
        raise GenerationError(
            f"version {version.version} has not passed the gate ({version.status})"
        )
    with tempfile.TemporaryDirectory(prefix="morph-tests-") as tmp:
        bundle = materialize(version, Path(tmp))
        result = runner.run(bundle, ["python", "-E", "-s", "-B", "-m", TESTS_PACKAGE], network=None)
    parsed: dict[str, Any] | None = None
    if result.outcome in (Outcome.OK, Outcome.NONZERO):
        try:
            loaded = json.loads(result.stdout.strip().splitlines()[-1])
            parsed = loaded if isinstance(loaded, dict) else None
        except (ValueError, IndexError):
            parsed = None
    row = SandboxRunRow(
        integration_version_id=version.id,
        purpose="generated_tests",
        limits=runner.limits.as_dict(),
        outcome=result.outcome.value,
        exit_code=result.exit_code,
        duration_s=result.duration_s,
        stdout_excerpt=result.stdout[:4000],
        stderr_excerpt=result.stderr[:4000],
        result=parsed,
    )
    session.add(row)
    session.flush()
    return row
