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
from app.codegen.llm_codegen import PROMPT_VERSION, propose_strategy, propose_sync_module
from app.codegen.operations import OperationPlan, PlanError, analyse
from app.codegen.review_gate import GateDecision, GateStatus, decide
from app.codegen.sandbox import Outcome, SandboxRunner
from app.db_models import (
    GateResultRow,
    Integration,
    IntegrationFile,
    IntegrationVersion,
    LLMCall,
    SandboxRunRow,
)
from app.llm.base import Attempt, BaseLLMProvider
from app.mapping.transform import JsonScalar

CONDITIONS = ("D", "L1", "L2")

BLOCKED_PENDING_REVIEW = "BLOCKED_PENDING_REVIEW"
BLOCKED_UNSUPPORTED = "BLOCKED_UNSUPPORTED"
LLM_INVALID = "LLM_INVALID"
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
    attempts: tuple[Attempt, ...] = (),
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
    for number, attempt in enumerate(attempts, start=1):
        meta = attempt.metadata
        session.add(
            LLMCall(
                mapping_run_id=inp.mapping_run_id,
                integration_version_id=version.id,
                target_field=None,
                attempt=number,
                provider=meta.provider,
                model=meta.model,
                prompt_hash=meta.prompt_hash,
                input_tokens=meta.input_tokens,
                output_tokens=meta.output_tokens,
                reasoning_tokens=meta.reasoning_tokens,
                total_tokens=meta.total_tokens,
                usage=meta.usage,
                finish_reason=meta.finish_reason,
                latency_ms=meta.latency_ms,
                outcome=meta.outcome.value,
                source=meta.source,
                http_attempts=meta.http_attempts,
                error=meta.error,
            )
        )
    session.flush()
    return version


def _status_after_gates(partial: bool, passed_ast: bool, tools: list[GateResult] | None) -> str:
    if not passed_ast or (tools is not None and not all(t.passed for t in tools)):
        return GATE_FAILED
    if tools is None:
        return GENERATED_PARTIAL if partial else GENERATED
    return READY_PARTIAL if partial else READY


def _llm_info(
    llm: BaseLLMProvider, prompt_hash: str, attempts: tuple[Attempt, ...], error: str | None
) -> dict[str, Any]:
    return {
        "prompt_version": PROMPT_VERSION,
        "provider": llm.name,
        "model": getattr(llm, "model", ""),
        "prompt_hash": prompt_hash,
        "calls": len(attempts),
        "input_tokens": sum(a.metadata.input_tokens or 0 for a in attempts),
        "output_tokens": sum(a.metadata.output_tokens or 0 for a in attempts),
        "reasoning_tokens": sum(a.metadata.reasoning_tokens or 0 for a in attempts),
        "error": error,
    }


def _llm_failed(
    session: Session,
    integration: Integration,
    inp: CodegenInput,
    condition: str,
    decision: GateDecision,
    in_hash: str,
    llm_info: dict[str, Any],
    attempts: tuple[Attempt, ...],
) -> IntegrationVersion:
    """The model's output was invalid after the single re-ask: record it, never fall back to D."""
    manifest = build_manifest(
        inp, condition=condition, status=LLM_INVALID, decision=decision, strategy=None, files={}
    )
    manifest["llm"] = llm_info
    return _persist(
        session, integration, inp, status=LLM_INVALID, in_hash=in_hash, files={},
        manifest=manifest, gates=[], attempts=attempts,
    )  # fmt: skip


def generate_from_input(
    session: Session,
    inp: CodegenInput,
    *,
    condition: str = "D",
    allow_partial: bool = False,
    runner: SandboxRunner | None = None,
    llm: BaseLLMProvider | None = None,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> IntegrationVersion:
    """Generate an integration version for ``inp``. Reuses the latest version if nothing changed."""
    if condition not in CONDITIONS:
        raise GenerationError(f"condition {condition!r} is not available")
    if condition != "D" and llm is None:
        raise GenerationError(f"condition {condition} needs an LLM provider")
    integration = _get_or_create_integration(session, inp, condition)
    identity = (
        ""
        if llm is None or condition == "D"
        else f"{PROMPT_VERSION}:{llm.name}:{getattr(llm, 'model', '')}"
    )
    in_hash = input_hash(inp, condition, allow_partial=allow_partial, llm_identity=identity)
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
    llm_info: dict[str, Any] | None = None
    attempts: tuple[Attempt, ...] = ()
    strategy_override: dict[str, Any] | None = None
    sync_source: str | None = None
    extra_samples: tuple[dict[str, JsonScalar], ...] = ()
    if condition == "L1":
        assert llm is not None
        proposed = propose_strategy(
            llm, inp, decision, temperature=temperature, max_output_tokens=max_output_tokens
        )
        attempts = proposed.attempts
        llm_info = _llm_info(llm, proposed.prompt_hash, attempts, proposed.error)
        llm_info["dropped_edge_records"] = list(proposed.dropped_edge_records)
        llm_info["edge_records_used"] = len(proposed.edge_records)
        if proposed.strategy is None:
            return _llm_failed(
                session, integration, inp, condition, decision, in_hash, llm_info, attempts
            )
        strategy_override = proposed.strategy
        extra_samples = proposed.edge_records
    elif condition == "L2":
        assert llm is not None
        module = propose_sync_module(
            llm, inp, decision, temperature=temperature, max_output_tokens=max_output_tokens
        )
        attempts = module.attempts
        llm_info = _llm_info(llm, module.prompt_hash, attempts, module.error)
        if module.source is None:
            return _llm_failed(
                session, integration, inp, condition, decision, in_hash, llm_info, attempts
            )
        sync_source = module.source
    try:
        package = generate_package(
            inp, plan, decision, strategy=strategy_override, sync_source=sync_source
        )
    except PlanError as error:
        return blocked(BLOCKED_UNSUPPORTED, decision, str(error))

    files = {**package.files, **render_tests(decision.included, (*inp.samples, *extra_samples))}
    gates = [check_ast(files)]
    tools: list[GateResult] | None = None
    manifest = build_manifest(
        inp, condition=condition, status=GENERATED, decision=decision, strategy=package.strategy,
        files=files,
    )  # fmt: skip
    if llm_info is not None:
        manifest["llm"] = llm_info
    if gates[0].passed and runner is not None:
        with tempfile.TemporaryDirectory(prefix="morph-gate-") as tmp:
            write_bundle(Path(tmp), files, manifest)
            tools = run_tool_gate(runner, Path(tmp))
        gates.extend(tools)
    partial = decision.status is GateStatus.PARTIAL
    status = _status_after_gates(partial, gates[0].passed, tools)
    return _persist(
        session, integration, inp, status=status, in_hash=in_hash, files=files,
        manifest=manifest, gates=gates, attempts=attempts,
    )  # fmt: skip


def generate(
    session: Session,
    mapping_run_id: int,
    *,
    condition: str = "D",
    allow_partial: bool = False,
    runner: SandboxRunner | None = None,
    samples_dir: Path | None = None,
    llm: BaseLLMProvider | None = None,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> IntegrationVersion:
    return generate_from_input(
        session,
        load_input(session, mapping_run_id, samples_dir),
        condition=condition,
        allow_partial=allow_partial,
        runner=runner,
        llm=llm,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
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
