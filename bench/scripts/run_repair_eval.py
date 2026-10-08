"""The v0.5 repair evaluation: L1R and L2R on the approved S1, S3 and S4, one phase per run.

    uv run python -m scripts.run_repair_eval --phase fixed --provider groq --confirm-real-run \
        --max-real-calls 20 --max-real-tokens 160000

``--phase fixed`` seeds attempt 0 from the recorded v0.4 replies (through an injected replay
provider, never a live call); ``--phase fresh`` makes attempt 0 a new model call. The phases are
separate runs with separate approvals. A real run needs ``--confirm-real-run``, a token cap, usage
recorded on every stored call (fail-closed), and the pre-registered cap on real calls (20 fixed,
26 fresh) as its ceiling. The harness stops cleanly when a cap is reached and a later run continues
the unfinished unit; units that finished are never run again. The oracle grades a READY unit once,
here, after the loop ended; nothing from the oracle ever reaches the repair code. Reports are
generated in M4, not here.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.codegen.inputs import load_input
from app.codegen.sandbox import SandboxRunner
from app.codegen.service import materialize
from app.db_models import IntegrationVersion, RepairAttempt, RepairRun
from app.llm.base import BaseLLMProvider, CallMetadata, LLMError, LLMRequest, RawCompletion
from app.llm.factory import create_llm_provider
from app.llm.store import ReplayLLMProvider, ResponseStore
from app.repair.graph import GRAPH_VERSION
from app.repair.prompts import REPAIR_PROMPT_VERSION, RepairPromptBuilder
from app.repair.service import (
    RepairEnv,
    TracingEnabled,
    ensure_checkpoint_schema,
    resume_repair,
    run_repair,
)
from app.repair.sizing import (
    CHARS_PER_TOKEN,
    DEFAULT_TPM_LIMIT,
    EXPECTED_OUTPUT_TOKENS,
    HARD_STOP_FACTOR,
)
from app.repair.smoke import make_smoke_runner
from app.repair.state import TERMINAL, HardError, RunStatus, StartMode
from app.settings import get_settings
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from morph_bench.mapping_eval import Limits
from morph_bench.oracle.build import SAMPLES_DIR, seed_mapping_run
from morph_bench.oracle.models import load_fixture
from morph_bench.repair_eval import (
    PHASE_CALL_CAPS,
    PHASES,
    SHORT,
    BudgetedProvider,
    BudgetExhausted,
    MissingUsageError,
    RepairUnitResult,
    append_result,
    apply_grading,
    check_recorded_calls,
    load_results,
    summarise_run,
)
from morph_bench.systems import REPO_ROOT
from scripts.run_codegen_eval import run_oracle
from scripts.run_mapping_eval import ResumableProvider

BENCH_DIR = Path(__file__).resolve().parents[1]
STORE_ROOT = BENCH_DIR / ".cache" / "repair-eval"
V04_REPLAYS = BENCH_DIR / "replays" / "codegen"
CONDITIONS = ("L1R", "L2R")
LONG_ID = {v: k for k, v in SHORT.items()}
OracleRunner = Callable[[Path, str], list[dict[str, object]] | None]

EXIT_OK, EXIT_REFUSED, EXIT_PAUSED, EXIT_BUDGET, EXIT_HARD, EXIT_USAGE = 0, 2, 3, 4, 5, 6


# ---- refusal rules -------------------------------------------------------------------------------


def check_options(
    *,
    phase: str,
    provider: str | None,
    test_only: bool,
    confirm: bool,
    max_calls: int | None,
    max_tokens: int | None,
    conditions: Sequence[str],
) -> str | None:
    """Why this run must not start, or None."""
    if phase not in PHASES:
        return f"--phase must be one of {', '.join(PHASES)}"
    if not set(conditions) <= set(CONDITIONS):
        return "unknown condition (use L1R and/or L2R)"
    real = provider in ("groq", "ollama")
    if provider is None:
        return "--provider is required (groq, ollama, or replay with --test-only)"
    if provider == "replay" and not test_only:
        return "the replay provider is only allowed with --test-only"
    if test_only and real:
        return "--test-only never calls a real provider"
    if real and not confirm:
        return "a real model call needs --confirm-real-run (an explicit go for this phase)"
    cap = PHASE_CALL_CAPS[phase]
    if max_calls is not None and max_calls > cap:
        return f"--max-real-calls {max_calls} is above the pre-registered cap of {cap} for {phase}"
    if real and max_tokens is None:
        return "a real run needs --max-real-tokens (a cap on cumulative tokens, reasoning included)"
    return None


class _Scripted(BaseLLMProvider):
    """Used only by the recording-path self-check: a reply that carries usage."""

    name = "selfcheck"

    def __init__(self, with_usage: bool) -> None:
        self.with_usage = with_usage

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        meta = CallMetadata(
            "selfcheck", "m", request.fingerprint(response_model), 1, 10, 5, 2,
            total_tokens=15 if self.with_usage else None,
            usage={"total_tokens": 15} if self.with_usage else None,
            finish_reason="stop", source="network",
        )  # fmt: skip
        return RawCompletion('{"value": 1}', meta)


def verify_recording_path() -> None:
    """Refuse a real run whose recording path would lose usage, total_tokens or finish_reason."""

    class Answer(BaseModel):
        value: int

    request = LLMRequest("s", ("p",), "answer")
    with tempfile.TemporaryDirectory(prefix="morph-selfcheck-") as tmp:
        provider = ResumableProvider(
            _Scripted(True), Path(tmp), limits=Limits(), require_usage=True
        )
        provider.complete_raw(request, Answer)
        problems = check_recorded_calls(Path(tmp))
        stored = ResponseStore(Path(tmp) / "calls").get(f"{request.fingerprint(Answer)}:1")
        if problems or stored is None or stored.finish_reason != "stop" or stored.usage is None:
            raise MissingUsageError(f"the recording path drops usage or finish_reason: {problems}")
        again = provider.complete_raw(request, Answer)
        if again.metadata.usage is None or again.metadata.finish_reason != "stop":
            raise MissingUsageError("a replayed call lost its usage or finish_reason")


# ---- one unit ------------------------------------------------------------------------------------


def git_sha() -> str:
    done = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )  # fmt: skip
    return done.stdout.strip() or "unknown"


def write_provenance(path: Path, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields, indent=2, sort_keys=True, default=str) + "\n", "utf-8")


def unit_key(scenario_id: str, condition: str, phase: str) -> str:
    return f"{scenario_id}|{condition}|{phase}"


def grade_if_ready(
    session: Session, result: RepairUnitResult, oracle: OracleRunner, scenario_id: str
) -> None:
    """Grade a READY unit exactly once, outside the repair package, after its loop has ended."""
    if result.status != RunStatus.READY.value or result.oracle is not None:
        return
    row = session.scalars(
        select(RepairAttempt)
        .where(RepairAttempt.repair_run_id == result.run_id)
        .order_by(RepairAttempt.attempt.desc())
    ).first()
    assert row is not None and row.integration_version_id is not None
    version = session.get(IntegrationVersion, row.integration_version_id)
    assert version is not None
    with tempfile.TemporaryDirectory(prefix="morph-repair-oracle-") as tmp:
        directory = materialize(version, Path(tmp) / "bundle")
        checks = oracle(directory, scenario_id)
    if checks is not None:
        apply_grading(result, checks)


def main(
    argv: Sequence[str] | None = None,
    *,
    llm_override: BaseLLMProvider | None = None,
    runner_override: SandboxRunner | None = None,
    smoke_runner_override: SandboxRunner | None = None,
    oracle_override: OracleRunner | None = None,
    ensure_schema: Callable[[str], None] = ensure_checkpoint_schema,
) -> int:
    args = _parse(argv)
    conditions = [c for c in args.conditions.split(",") if c]
    test_only = bool(args.test_only or llm_override is not None)
    provider = "replay" if llm_override is not None else args.provider
    refusal = check_options(
        phase=args.phase, provider=provider, test_only=test_only, confirm=args.confirm_real_run,
        max_calls=args.max_real_calls, max_tokens=args.max_real_tokens, conditions=conditions,
    )  # fmt: skip
    if refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    wanted = [s for s in args.scenarios.split(",") if s]
    if not set(wanted) <= set(LONG_ID):
        print("refused: unknown scenario (use S1, S3, S4)", file=sys.stderr)
        return EXIT_REFUSED
    scenarios = [LONG_ID[s] for s in ("S1", "S3", "S4") if s in wanted]
    real = provider in ("groq", "ollama")
    store_dir = args.store_dir or STORE_ROOT / ("test-only" if test_only else args.phase)
    max_calls = (
        args.max_real_calls if args.max_real_calls is not None else PHASE_CALL_CAPS[args.phase]
    )
    if real:
        problems = check_recorded_calls(store_dir)
        if problems:
            print("refused: a stored call lacks usage: " + "; ".join(problems[:3]), file=sys.stderr)
            return EXIT_USAGE
        try:
            verify_recording_path()
        except MissingUsageError as error:
            print(f"refused: {error}", file=sys.stderr)
            return EXIT_USAGE

    settings = get_settings()
    engine = create_engine(args.database_url or settings.database_url)
    conninfo = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    ensure_schema(conninfo)  # exactly once per start, outside any open transaction

    live: BaseLLMProvider | None = llm_override
    budget: BudgetedProvider | None = None
    if live is None and provider == "replay":
        live = ReplayLLMProvider(ResponseStore(args.replay_dir))
    if live is None:
        try:
            inner = create_llm_provider(settings)
        except LLMError as error:
            print(f"refused: {error}", file=sys.stderr)
            return EXIT_REFUSED
        budget = BudgetedProvider(
            inner, store_dir / "budget.json", max_calls=max_calls, max_tokens=args.max_real_tokens
        )
        live = ResumableProvider(budget, store_dir, limits=Limits(), require_usage=True)
    runner = runner_override or SandboxRunner()
    smoke_runner = smoke_runner_override or make_smoke_runner()
    oracle = oracle_override or run_oracle
    results_path = store_dir / "results.jsonl"
    runs_path = store_dir / "runs.json"
    runs: dict[str, int] = json.loads(runs_path.read_text("utf-8")) if runs_path.is_file() else {}
    done = {r.key for r in load_results(results_path)}
    secrets = _secrets(settings)
    builder = RepairPromptBuilder(
        temperature=settings.llm_temperature, max_output_tokens=settings.llm_max_output_tokens
    )
    start_mode = StartMode.FIXED if args.phase == "fixed" else StartMode.FRESH
    seed = (
        ReplayLLMProvider(ResponseStore(args.seed_dir or V04_REPLAYS))
        if start_mode is StartMode.FIXED
        else None
    )
    write_provenance(
        store_dir / "provenance.json", phase=args.phase, started=datetime.now(UTC).isoformat(),
        git_sha=git_sha(), provider=provider, model_configured=_model(settings, provider),
        tpm_limit_assumed=args.tpm_limit, tpm_limit_source="the v0.4 response headers (8000)",
        max_real_calls=max_calls, max_real_tokens=args.max_real_tokens, test_only=test_only,
        repair_prompt_version=REPAIR_PROMPT_VERSION, graph_version=GRAPH_VERSION,
        estimator={"chars_per_token": CHARS_PER_TOKEN, "hard_stop_factor": HARD_STOP_FACTOR,
                   "expected_output_tokens": EXPECTED_OUTPUT_TOKENS},
        conditions=conditions, scenarios=sorted(SHORT.values()),
    )  # fmt: skip

    status = EXIT_OK
    with Session(engine) as session:
        for scenario_id in scenarios:
            for condition in conditions:
                key = unit_key(scenario_id, condition, args.phase)
                if key in done:
                    print(f"skip (finished): {key}")
                    continue
                print(f"running {key}", flush=True)
                started = time.monotonic()
                try:
                    result = _run_unit(
                        session, key=key, scenario_id=scenario_id, condition=condition,
                        start_mode=start_mode, builder=builder, live=live, seed=seed,
                        runner=runner, smoke_runner=smoke_runner, store_dir=store_dir,
                        conninfo=conninfo, tpm_limit=args.tpm_limit, secrets=secrets, runs=runs,
                        runs_path=runs_path, test_only=test_only,
                    )  # fmt: skip
                except BudgetExhausted as stop:
                    print(f"paused by the budget ({stop}); run again to continue", file=sys.stderr)
                    status = EXIT_BUDGET
                    break
                except MissingUsageError as stop:
                    print(f"stopped, fail-closed: {stop}", file=sys.stderr)
                    status = EXIT_USAGE
                    break
                except HardError as stop:
                    print(f"aborted: {stop}", file=sys.stderr)
                    status = EXIT_HARD
                    break
                except TracingEnabled as stop:
                    print(f"refused: {stop}", file=sys.stderr)
                    status = EXIT_REFUSED
                    break
                if RunStatus(result.status) not in TERMINAL:
                    print(f"paused ({result.reason}); run again to continue", file=sys.stderr)
                    status = EXIT_PAUSED
                    break
                result.elapsed_s = round(time.monotonic() - started, 1)
                grade_if_ready(session, result, oracle, scenario_id)
                append_result(results_path, result)
            if status:
                break
    limits_file = store_dir / "limits.json"
    observed = json.loads(limits_file.read_text("utf-8")) if limits_file.is_file() else {}
    provenance = json.loads((store_dir / "provenance.json").read_text("utf-8"))
    provenance["finished"] = datetime.now(UTC).isoformat()
    provenance["limits_observed"] = observed.get("last_headers", {})
    provenance["budget_used"] = budget.state.model_dump() if budget is not None else None
    provenance["exit_status"] = status
    write_provenance(store_dir / "provenance.json", **provenance)
    return status


def _run_unit(
    session: Session,
    *,
    key: str,
    scenario_id: str,
    condition: str,
    start_mode: StartMode,
    builder: RepairPromptBuilder,
    live: BaseLLMProvider,
    seed: BaseLLMProvider | None,
    runner: SandboxRunner,
    smoke_runner: SandboxRunner,
    store_dir: Path,
    conninfo: str,
    tpm_limit: int,
    secrets: Sequence[str],
    runs: dict[str, int],
    runs_path: Path,
    test_only: bool,
) -> RepairUnitResult:
    def remember(run_id: int) -> None:
        runs[key] = run_id
        runs_path.parent.mkdir(parents=True, exist_ok=True)
        runs_path.write_text(json.dumps(runs, indent=2, sort_keys=True), "utf-8")

    existing = runs.get(key)
    if existing is not None:
        run = session.get(RepairRun, existing)
        assert run is not None
        mapping_run_id = run.mapping_run_id
    else:
        mapping_run_id = seed_mapping_run(session, load_fixture(scenario_id), with_override=True)
    inp = load_input(session, mapping_run_id, SAMPLES_DIR)
    env = RepairEnv(
        session=session, inp=inp, condition=condition, start_mode=start_mode, builder=builder,
        live=live, response_store=ResponseStore(store_dir / "responses"), runner=runner,
        smoke_runner=smoke_runner, conninfo=conninfo, seed=seed, tpm_limit=tpm_limit,
        secrets=secrets, on_created=remember,
    )  # fmt: skip
    outcome = run_repair(env) if existing is None else resume_repair(env, existing)
    run = session.get(RepairRun, outcome.run_id)
    assert run is not None
    return summarise_run(
        session, run, scenario_id=scenario_id, start_mode=start_mode.value, test_only=test_only
    )


def _secrets(settings: Any) -> list[str]:
    values = []
    key = getattr(settings, "groq_api_key", None)
    if key is not None and hasattr(key, "get_secret_value"):
        values.append(key.get_secret_value())
    return [v for v in values if v]


def _model(settings: Any, provider: str | None) -> str | None:
    return {"groq": settings.groq_model, "ollama": settings.ollama_model}.get(provider or "")


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--conditions", default=",".join(CONDITIONS))
    parser.add_argument(
        "--scenarios", default="S1,S3,S4", help="short ids; the default is all three"
    )
    parser.add_argument("--provider", choices=["groq", "ollama", "replay"])
    parser.add_argument("--replay-dir", type=Path, help="recorded repair replies (test-only)")
    parser.add_argument("--seed-dir", type=Path, help="recorded v0.4 replies for a fixed start")
    parser.add_argument("--test-only", action="store_true")
    parser.add_argument("--confirm-real-run", action="store_true")
    parser.add_argument("--max-real-calls", type=int)
    parser.add_argument("--max-real-tokens", type=int)
    parser.add_argument("--tpm-limit", type=int, default=DEFAULT_TPM_LIMIT)
    parser.add_argument("--store-dir", type=Path)
    parser.add_argument("--database-url")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
