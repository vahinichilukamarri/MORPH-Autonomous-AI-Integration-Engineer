"""The nodes of the repair graph.

``propose`` is the only node that calls a model. Every other node is a deterministic function of the
stored attempt row and the unit's input, so a run that stops anywhere resumes at the interrupted
node, and the only thing that can differ on a resume is a sandbox run, never a model call: the
propose node records the reply (and the response store keyed by prompt hash is written by the
provider wrapper) before it returns.

The gates, the generated tests, the bundle and the persistence are the v0.4 functions, imported and
never copied.
"""

import hashlib
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.codegen.bundle import build_manifest, input_hash, write_bundle
from app.codegen.gate import GateResult, check_ast
from app.codegen.gate_tools import run_tool_gate
from app.codegen.generated_tests import render_tests
from app.codegen.generator import derive_strategy, generate_package
from app.codegen.inputs import CodegenInput
from app.codegen.llm_codegen import (
    MAX_MODULE_CHARS,
    StrategyProposal,
    SyncModuleProposal,
    parse_edge_records,
    validate_strategy,
)
from app.codegen.operations import OperationPlan, PlanError, analyse
from app.codegen.review_gate import GateDecision, GateStatus, decide
from app.codegen.sandbox import SandboxRunner
from app.codegen.service import (
    LLM_INVALID,
    _get_or_create_integration,
    _persist,
    _status_after_gates,
    run_generated_tests,
)
from app.db_models import IntegrationVersion, LLMCall, RepairAttempt, RepairRun
from app.llm.base import (
    Attempt,
    BaseLLMProvider,
    CallMetadata,
    ReplayMissError,
    RequestTooLarge,
)
from app.repair.builder import PromptBuilder
from app.repair.failures import (
    attempt_items,
    has_infrastructure_failure,
    is_infrastructure_error,
    is_infrastructure_outcome,
)
from app.repair.feedback import (
    Feedback,
    FeedbackItem,
    Stage,
    build_feedback,
    gate_items,
    generated_tests_items,
    guard_item,
    smoke_item,
)
from app.repair.guards import (
    GuardInputs,
    evaluate_guards,
    is_noop,
    output_hash_l1,
    output_hash_l2,
)
from app.repair.sizing import estimate_request, exceeds_hard_limit
from app.repair.smoke import run_smoke
from app.repair.state import (
    L1R,
    MAX_PAUSES,
    MAX_REPAIR_ATTEMPTS,
    HardError,
    Paused,
    RepairState,
    RunStatus,
    item_from_dict,
    item_to_dict,
)

SYNC_PATH = "integration/sync.py"
GUARD_REJECTED = "GUARD_REJECTED"
NOOP = "NOOP"
SIZE = "SIZE"

RESPONSE_MODELS: dict[str, type[BaseModel]] = {"L1R": StrategyProposal, "L2R": SyncModuleProposal}


@dataclass
class Work:
    """What one attempt's output turns into, rebuilt from the stored reply when needed."""

    value: BaseModel
    strategy: dict[str, Any] | None = None  # L1R
    extra_samples: tuple[dict[str, Any], ...] = ()  # L1R edge records
    source: str | None = None  # L2R
    files: dict[str, str] | None = None
    chosen_strategy: dict[str, Any] | None = None


@dataclass
class Deps:
    session: Session
    inp: CodegenInput  # with mapping_run_id set
    condition: str  # "L1R" or "L2R"
    builder: PromptBuilder
    live: BaseLLMProvider  # records every real reply before returning it
    seed: BaseLLMProvider | None  # fixed start: the only source of attempt 0
    runner: SandboxRunner
    smoke_runner: SandboxRunner
    tpm_limit: int
    secrets: Sequence[str] = ()
    after_call: Callable[[int], None] | None = None  # a test hook: runs after the reply is stored
    cache: dict[int, Work] = field(default_factory=dict)
    _plan: OperationPlan | None = None
    _decision: GateDecision | None = None


def _check_module(reply: SyncModuleProposal) -> None:
    if not reply.source.strip():
        raise ValueError("source is empty")
    if len(reply.source) > MAX_MODULE_CHARS:
        raise ValueError(f"source is longer than {MAX_MODULE_CHARS} characters")


def _raw_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


class Nodes:
    def __init__(self, deps: Deps) -> None:
        self.d = deps

    # ---- helpers -------------------------------------------------------------------------------

    def _run(self, state: RepairState) -> RepairRun:
        run = self.d.session.get(RepairRun, state["run_id"])
        assert run is not None
        return run

    def _row(self, run_id: int, attempt: int) -> RepairAttempt | None:
        return self.d.session.scalar(
            select(RepairAttempt).where(
                RepairAttempt.repair_run_id == run_id, RepairAttempt.attempt == attempt
            )
        )

    def _plan_decision(self) -> tuple[OperationPlan, GateDecision]:
        if self.d._plan is None or self.d._decision is None:
            inp = self.d.inp
            plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
            self.d._plan = plan
            self.d._decision = decide(inp, plan, allow_partial=False)
        return self.d._plan, self.d._decision

    def _terminal(self, state: RepairState, status: RunStatus, reason: str) -> RepairState:
        run = self._run(state)
        run.status = status.value
        run.terminal_reason = reason
        self.d.session.commit()
        return {"terminal": status.value, "terminal_reason": reason}

    def _pause(self, state: RepairState, reason: str) -> RepairState:
        """Count an infrastructure failure. Up to MAX_PAUSES the run stops and can be resumed."""
        run = self._run(state)
        run.pauses += 1
        if run.pauses > MAX_PAUSES:
            return self._terminal(
                state, RunStatus.INFRA_STOPPED, f"{reason}; stopped after {MAX_PAUSES} pauses"
            )
        run.status = RunStatus.PAUSED.value
        run.terminal_reason = reason
        self.d.session.commit()
        raise Paused(reason)

    def _fail(self, stage: Stage, items: Sequence[FeedbackItem]) -> RepairState:
        return {"pending": [item_to_dict(i) for i in items], "failed_stage": stage.value}

    def _model(self) -> type[BaseModel]:
        return RESPONSE_MODELS[self.d.condition]

    # ---- plan ----------------------------------------------------------------------------------

    def plan(self, state: RepairState) -> RepairState:
        try:
            _, decision = self._plan_decision()
        except PlanError as error:
            return self._terminal(state, RunStatus.BLOCKED_UNSUPPORTED, str(error))
        if decision.status is GateStatus.BLOCKED:
            reason = "; ".join(f"{b.target_field}: {b.reason.value}" for b in decision.blocking)
            return self._terminal(state, RunStatus.BLOCKED_PENDING_REVIEW, reason)
        return {}

    # ---- propose (the only model node) ----------------------------------------------------------

    def _validator(self, decision: GateDecision) -> Callable[[Any], object]:
        if self.d.condition == L1R:
            return lambda proposal: validate_strategy(proposal, self.d.inp, decision.included)
        return _check_module

    def propose(self, state: RepairState) -> RepairState:
        n = state["attempt"]
        run = self._run(state)
        _, decision = self._plan_decision()
        model = self._model()
        if n == 0:
            request = self.d.builder.initial(self.d.inp, decision, self.d.condition)
        else:
            previous = self._row(run.id, n - 1)
            assert previous is not None and previous.feedback is not None
            request = self.d.builder.repair(
                self.d.inp,
                decision,
                self.d.condition,
                attempt=n,
                previous_output=previous.output_text,
                feedback=Feedback.from_dict(previous.feedback),
            )
        seeded = n == 0 and self.d.seed is not None
        provider = self.d.seed if seeded else self.d.live
        assert provider is not None
        estimate = estimate_request(request, model, self.d.condition)
        row = self._row(run.id, n)
        if row is None:
            row = RepairAttempt(
                repair_run_id=run.id, attempt=n, prompt_hash=request.fingerprint(model),
                source="replay" if seeded else "network", output_text="", output_hash="",
                size_estimate=None if seeded else estimate.as_dict(), guard_result=[],
            )  # fmt: skip
            self.d.session.add(row)
            self.d.session.flush()
        if not seeded and exceeds_hard_limit(estimate, self.d.tpm_limit):
            row.failed_stage = SIZE
            return self._terminal(
                state,
                RunStatus.INFRA_STOPPED,
                f"REQUEST_TOO_LARGE: estimated {estimate.total_tokens} tokens against a limit of "
                f"{self.d.tpm_limit} (hard stop above {int(self.d.tpm_limit * 1.1)})",
            )
        try:
            result = provider.complete_structured(
                request, model, validate=self._validator(decision), reask=False
            )
        except ReplayMissError as error:
            run.status = RunStatus.ABORTED.value
            run.terminal_reason = f"missing fixed-start replay: {error}"
            self.d.session.commit()
            raise HardError(run.terminal_reason) from error
        except RequestTooLarge as error:
            row.failed_stage = SIZE
            return self._terminal(
                state,
                RunStatus.INFRA_STOPPED,
                f"SIZE_REJECTED: the provider refused the request; estimated "
                f"{estimate.total_tokens} tokens ({error})",
            )
        except Exception as error:
            if is_infrastructure_error(error):
                return self._pause(state, f"{type(error).__name__}: {error}")
            raise
        self._store_reply(row, result.attempts[0], result.value, result.final_error, run)
        if self.d.after_call is not None:
            self.d.after_call(n)
        return {}

    def _store_reply(
        self,
        row: RepairAttempt,
        attempt: Attempt,
        value: BaseModel | None,
        error: str | None,
        run: RepairRun,
    ) -> None:
        meta = attempt.metadata
        if value is None:
            output_hash = _raw_hash(attempt.raw_text)
        elif isinstance(value, StrategyProposal):
            output_hash = output_hash_l1(value.model_dump(mode="json"))
        else:
            assert isinstance(value, SyncModuleProposal)
            output_hash = output_hash_l2(value.source)
        row.output_text = attempt.raw_text
        row.output_hash = output_hash
        row.error = error
        row.finish_reason = meta.finish_reason
        row.total_tokens = meta.total_tokens
        row.usage = meta.usage
        row.source = meta.source
        if row.llm_call_id is None:
            call = LLMCall(
                mapping_run_id=run.mapping_run_id, integration_version_id=None, target_field=None,
                attempt=row.attempt, provider=meta.provider, model=meta.model,
                prompt_hash=meta.prompt_hash, input_tokens=meta.input_tokens,
                output_tokens=meta.output_tokens, reasoning_tokens=meta.reasoning_tokens,
                total_tokens=meta.total_tokens, usage=meta.usage, finish_reason=meta.finish_reason,
                latency_ms=meta.latency_ms, outcome="OK" if error is None else "INVALID_OUTPUT",
                source=meta.source, http_attempts=meta.http_attempts, error=error,
            )  # fmt: skip
            self.d.session.add(call)
            self.d.session.flush()
            row.llm_call_id = call.id
        self.d.session.commit()

    # ---- validate ------------------------------------------------------------------------------

    def _work(self, n: int, run_id: int) -> Work:
        if n in self.d.cache:
            return self.d.cache[n]
        row = self._row(run_id, n)
        assert row is not None and row.error is None
        _, decision = self._plan_decision()
        value = self._model().model_validate_json(row.output_text)
        if isinstance(value, StrategyProposal):
            strategy = validate_strategy(value, self.d.inp, decision.included)
            extras, _ = parse_edge_records(value.edge_record_json, self.d.inp)
            work = Work(value, strategy=strategy, extra_samples=tuple(extras))
        else:
            assert isinstance(value, SyncModuleProposal)
            work = Work(value, source=value.source)
        self.d.cache[n] = work
        return work

    def validate(self, state: RepairState) -> RepairState:
        n = state["attempt"]
        run = self._run(state)
        row = self._row(run.id, n)
        assert row is not None
        if is_noop(row.output_hash, state.get("output_hashes", [])):
            row.failed_stage = NOOP
            return self._terminal(state, RunStatus.HUMAN_REVIEW_REQUIRED, "NO_OP")
        update: RepairState = {"output_hashes": [*state.get("output_hashes", []), row.output_hash]}
        if row.error is not None:
            meta = CallMetadata(
                "", "", row.prompt_hash, 0, None, None, finish_reason=row.finish_reason
            )
            items = attempt_items(Attempt(meta, row.output_text, row.error))
            return {**update, **self._fail(Stage.PROPOSAL, items)}
        self._work(n, run.id)
        return update

    # ---- build ---------------------------------------------------------------------------------

    def _assemble(self, work: Work) -> tuple[dict[str, str], dict[str, Any]]:
        plan, decision = self._plan_decision()
        inp = self.d.inp
        if work.source is not None:
            package = generate_package(inp, plan, decision, sync_source=work.source)
            tests = render_tests(decision.included, inp.samples)
        else:
            package = generate_package(inp, plan, decision, strategy=work.strategy)
            tests = render_tests(decision.included, (*inp.samples, *work.extra_samples))
        return {**package.files, **tests}, package.strategy

    def build(self, state: RepairState) -> RepairState:
        work = self._work(state["attempt"], state["run_id"])
        try:
            work.files, work.chosen_strategy = self._assemble(work)
        except PlanError as error:
            return self._terminal(state, RunStatus.BLOCKED_UNSUPPORTED, str(error))
        return {}

    def _built(self, n: int, run_id: int) -> Work:
        work = self._work(n, run_id)
        if work.files is None:
            work.files, work.chosen_strategy = self._assemble(work)
        return work

    # ---- guard ---------------------------------------------------------------------------------

    def guard(self, state: RepairState) -> RepairState:
        n, run = state["attempt"], self._run(state)
        work = self._built(n, run.id)
        plan, decision = self._plan_decision()
        inp = self.d.inp
        assert work.files is not None
        rebuilt, _ = self._assemble(work)
        names = {m.target_field for m in decision.included}
        previous = self._row(run.id, n - 1) if n > 0 else None
        previous_sources: dict[str, str] | None = None
        if work.source is not None and previous is not None and previous.error is None:
            before = SyncModuleProposal.model_validate_json(previous.output_text)
            previous_sources = {SYNC_PATH: before.source}
        report = evaluate_guards(
            GuardInputs(
                files=work.files,
                rebuilt=rebuilt,
                owned=frozenset({SYNC_PATH}) if work.source is not None else frozenset(),
                base_tests=render_tests(decision.included, inp.samples),
                sources={SYNC_PATH: work.source} if work.source is not None else {},
                previous_sources=previous_sources,
                strategy={"target": work.chosen_strategy["target"]}
                if work.strategy is not None and work.chosen_strategy
                else None,
                required_on_create={f.name for f in plan.target.create_fields if f.required}
                & names,
                included=names & plan.target.writable,
            )
        )
        row = self._row(run.id, n)
        assert row is not None
        row.guard_result = [
            {"guard": f.guard, "mode": f.mode.value, "message": f.message, "file": f.file,
             "line": f.line}
            for f in report.findings
        ]  # fmt: skip
        self.d.session.flush()
        if report.rejected:
            return self._fail(Stage.GUARD, [guard_item(f) for f in report.enforced])
        return {}

    # ---- gates ---------------------------------------------------------------------------------

    def _record_version(
        self,
        state: RepairState,
        status: str,
        files: dict[str, str],
        gates: list[GateResult],
        work: Work | None,
    ) -> IntegrationVersion:
        """One integration version per attempt, through the v0.4 persistence function."""
        run, n = self._run(state), state["attempt"]
        row = self._row(run.id, n)
        assert row is not None
        if row.integration_version_id is not None:
            existing = self.d.session.get(IntegrationVersion, row.integration_version_id)
            assert existing is not None
            return existing
        inp, (_, decision) = self.d.inp, self._plan_decision()
        integration = _get_or_create_integration(self.d.session, inp, self.d.condition)
        manifest = build_manifest(
            inp, condition=self.d.condition, status=status, decision=decision,
            strategy=work.chosen_strategy if work is not None else None, files=files,
        )  # fmt: skip
        manifest["repair"] = {"run": run.id, "attempt": n}
        version = _persist(
            self.d.session, integration, inp, status=status,
            in_hash=input_hash(inp, self.d.condition, allow_partial=False,
                               llm_identity=f"repair:{n}:{row.output_hash[:16]}"),
            files=files, manifest=manifest, gates=gates,
        )  # fmt: skip
        row.integration_version_id = version.id
        if row.llm_call_id is not None:
            call = self.d.session.get(LLMCall, row.llm_call_id)
            if call is not None:
                call.integration_version_id = version.id
        self.d.session.commit()
        return version

    def gates(self, state: RepairState) -> RepairState:
        n, run = state["attempt"], self._run(state)
        work = self._built(n, run.id)
        assert work.files is not None
        ast_result = check_ast(work.files)
        results: list[GateResult] = [ast_result]
        tools: list[GateResult] | None = None
        if ast_result.passed:
            with tempfile.TemporaryDirectory(prefix="morph-gate-") as tmp:
                write_bundle(Path(tmp), work.files, {})
                try:
                    tools = run_tool_gate(self.d.runner, Path(tmp))
                except Exception as error:
                    if is_infrastructure_error(error):
                        return self._pause(state, f"{type(error).__name__}: {error}")
                    raise
            if any(has_infrastructure_failure(t) for t in tools):
                return self._pause(state, "the sandbox did not start for ruff or mypy")
            results += tools
        status = _status_after_gates(False, ast_result.passed, tools)
        self._record_version(state, status, work.files, results, work)
        failing = [g for g in results if not g.passed]
        if failing:
            base = len(self.d.inp.samples)
            items = [i for g in failing for i in gate_items(g, work.files, base)]
            stage = Stage(failing[0].stage.upper())
            return self._fail(stage, items)
        return {}

    # ---- tests ---------------------------------------------------------------------------------

    def tests(self, state: RepairState) -> RepairState:
        run = self._run(state)
        row = self._row(run.id, state["attempt"])
        assert row is not None and row.integration_version_id is not None
        version = self.d.session.get(IntegrationVersion, row.integration_version_id)
        assert version is not None
        try:
            outcome = run_generated_tests(self.d.session, version, self.d.runner)
        except Exception as error:
            if is_infrastructure_error(error):
                return self._pause(state, f"{type(error).__name__}: {error}")
            raise
        if is_infrastructure_outcome(outcome.outcome):
            return self._pause(state, "the sandbox did not start for the generated tests")
        result = outcome.result
        if (
            outcome.outcome == "OK"
            and outcome.exit_code == 0
            and result is not None
            and not result.get("failures")
        ):
            return {}
        items = generated_tests_items(outcome.outcome, outcome.exit_code, result)
        return self._fail(Stage.TESTS, items)

    # ---- smoke ---------------------------------------------------------------------------------

    def smoke(self, state: RepairState) -> RepairState:
        n, run = state["attempt"], self._run(state)
        work = self._built(n, run.id)
        plan, decision = self._plan_decision()
        assert work.files is not None
        with tempfile.TemporaryDirectory(prefix="morph-smoke-") as tmp:
            write_bundle(Path(tmp), work.files, {})
            try:
                result = run_smoke(
                    self.d.smoke_runner, Path(tmp), derive_strategy(plan, decision.included)
                )
            except Exception as error:
                if is_infrastructure_error(error):
                    return self._pause(state, f"{type(error).__name__}: {error}")
                raise
        if result.infra:
            return self._pause(state, "the sandbox did not start for the smoke test")
        if not result.ok:
            return self._fail(Stage.SMOKE, [smoke_item(result.code, result.detail)])
        row = self._row(run.id, n)
        assert row is not None
        row.failed_stage = None
        return self._terminal(state, RunStatus.READY, "")

    # ---- feedback and decide ------------------------------------------------------------------

    def feedback(self, state: RepairState) -> RepairState:
        n, run = state["attempt"], self._run(state)
        row = self._row(run.id, n)
        assert row is not None
        items = [item_from_dict(d) for d in state.get("pending", [])]
        history = [(int(a), list(codes)) for a, codes in state.get("history", [])]
        built = build_feedback(n, items, secrets=self.d.secrets, history=history)
        row.feedback = built.to_dict()
        row.failed_stage = state.get("failed_stage")
        self.d.session.flush()
        if row.integration_version_id is None:
            self._record_failed_attempt(state, row)
        self.d.session.commit()
        return {"pending": [], "history": [*state.get("history", []), [n, list(built.codes())]]}

    def _record_failed_attempt(self, state: RepairState, row: RepairAttempt) -> None:
        """The attempt failed before the gates: still keep a version for it."""
        stage = state.get("failed_stage")
        if stage == Stage.PROPOSAL.value:
            self._record_version(state, LLM_INVALID, {}, [], None)
        else:
            work = self.d.cache.get(row.attempt)
            files = work.files if work is not None and work.files is not None else {}
            self._record_version(state, GUARD_REJECTED, files, [], work)

    def decide(self, state: RepairState) -> RepairState:
        n = state["attempt"]
        if n >= MAX_REPAIR_ATTEMPTS:
            return self._terminal(
                state,
                RunStatus.HUMAN_REVIEW_REQUIRED,
                f"EXHAUSTED:{state.get('failed_stage') or 'UNKNOWN'}",
            )
        return {"attempt": n + 1, "failed_stage": None}
