"""Evaluate MORPH's code generation against the hidden oracle. Run from ``bench/``:

    uv run python -m scripts.run_codegen_eval --conditions D            # no model, no network
    uv run python -m scripts.run_codegen_eval --conditions L1,L2 --provider groq --confirm-real-run

A *unit* is one scenario x input set x condition. For each unit the script generates the
integration (static gate included), runs the generated tests in the sandbox, and grades the
bundle with the oracle (a pytest subprocess against fresh mock systems). Every finished unit is
appended to a results file, so an interrupted run resumes without repeating work; the report
``docs/codegen-eval.md`` is generated entirely from the saved results.

Model calls: a real provider needs ``--confirm-real-run`` (the explicit go for Checkpoint D);
replay or scripted providers need ``--test-only`` and can never write the committed report.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.codegen.sandbox import SandboxRunner
from app.codegen.service import RUNNABLE, generate, materialize, run_generated_tests
from app.db_models import IntegrationVersion, LLMCall
from app.embeddings.fake import FakeEmbeddingProvider
from app.llm.base import BaseLLMProvider, LLMError
from app.llm.factory import create_llm_provider
from app.llm.store import ReplayLLMProvider, ResponseStore
from app.mapping.runner import run_mapping
from app.mapping.store import save_run
from app.settings import get_settings
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from morph_bench.codegen_eval import (
    CONDITIONS,
    INPUT_SETS,
    GeneratedTests,
    LLMUse,
    UnitResult,
    append_result,
    load_results,
    oracle_summary,
    render_report,
)
from morph_bench.loader import SCENARIOS_ROOT, discover
from morph_bench.mapping_eval import Limits
from morph_bench.models import Bundle
from morph_bench.oracle.build import SAMPLES_DIR, seed_mapping_run
from morph_bench.oracle.models import OracleFixture, load_fixture
from morph_bench.systems import REPO_ROOT
from scripts.rescore_v2 import NoNewCalls
from scripts.run_mapping_eval import ResumableProvider, _ingest_scenario

BENCH_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "codegen-eval.md"
TEST_ONLY_OUTPUT = BENCH_DIR / ".cache" / "codegen-eval-test-only.md"
PARTIAL_OUTPUT = BENCH_DIR / ".cache" / "codegen-eval-partial.md"
STORE_ROOT = BENCH_DIR / ".cache" / "codegen-eval"
MAPPING_STORE = BENCH_DIR / ".cache" / "mapping-eval" / "groq-openai_gpt-oss-120b"
REPLAYS = BENCH_DIR / "replays"
SHORT_IDS = {
    "crm_customer_to_support_user": "S1",
    "support_user_to_crm_customer": "S3",
    "crm_v2_to_support_v2": "S4",
}

OracleRunner = Callable[[Path, str], list[dict[str, object]] | None]


# ---- the oracle as a subprocess --------------------------------------------------------------


def run_oracle(bundle: Path, scenario_id: str) -> list[dict[str, object]] | None:
    """Grade ``bundle`` with the oracle; None when the oracle could not run at all."""
    with tempfile.TemporaryDirectory(prefix="morph-oracle-") as tmp:
        results = Path(tmp) / "results.json"
        env = {
            **os.environ,
            "MORPH_ORACLE_BUNDLE": str(bundle),
            "MORPH_ORACLE_RESULTS": str(results),
        }
        subprocess.run(  # noqa: S603
            [
                sys.executable, "-m", "pytest", "oracle/test_oracle.py",
                "oracle/test_added_after_first_d_run.py", "-q", "-p", "no:cacheprovider",
                "-k", SHORT_IDS[scenario_id],
            ],
            cwd=BENCH_DIR, env=env, check=False, capture_output=True,
        )  # fmt: skip
        if not results.is_file():
            return None
        loaded = json.loads(results.read_text(encoding="utf-8"))
        return [c for c in loaded if isinstance(c, dict)]


# ---- mapping inputs ----------------------------------------------------------------------------


def proposed_run(session: Session, bundle: Bundle, replay_dir: Path | None) -> int:
    """The v0.3 mapping run for a scenario, rebuilt from saved responses (no model call)."""
    settings = get_settings()
    embedder = FakeEmbeddingProvider()
    source, target = _ingest_scenario(session, embedder, bundle)
    if (MAPPING_STORE / "calls").is_dir() and replay_dir is None:
        resumable = ResumableProvider(NoNewCalls(), MAPPING_STORE, limits=Limits())
        resumable.run_index = 1
        llm: BaseLLMProvider = resumable
    else:
        llm = ReplayLLMProvider(ResponseStore(replay_dir or REPLAYS))
    scenario = bundle.scenario
    result = run_mapping(
        session, llm, embedder,
        source_version_id=source.version_id, target_version_id=target.version_id,
        source_entity=scenario.source.entity, target_entity=scenario.target.entity,
        mode="full_schema", samples_dir=settings.samples_dir, requirement=scenario.requirement,
        validator="v1", temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
    )  # fmt: skip
    return save_run(session, result, settings.llm_temperature).id


# ---- one unit ----------------------------------------------------------------------------------


def _llm_use(session: Session, version: IntegrationVersion) -> LLMUse | None:
    rows = list(
        session.scalars(select(LLMCall).where(LLMCall.integration_version_id == version.id))
    )
    if not rows:
        return None
    info = version.manifest.get("llm") or {}
    return LLMUse(
        calls=len(rows),
        input_tokens=sum(r.input_tokens or 0 for r in rows),
        output_tokens=sum(r.output_tokens or 0 for r in rows),
        reasoning_tokens=sum(r.reasoning_tokens or 0 for r in rows),
        latency_ms=sum(r.latency_ms for r in rows),
        invalid_outputs=sum(1 for r in rows if r.outcome != "OK"),
        edge_records_used=info.get("edge_records_used"),
    )


def _blocked_reasons(version: IntegrationVersion) -> list[str]:
    manifest = version.manifest
    review = manifest.get("review") or {}
    reasons = [f"{b['target_field']}: {b['reason']}" for b in review.get("blocking", [])]
    if manifest.get("plan_error"):
        reasons.append(str(manifest["plan_error"]))
    llm = manifest.get("llm") or {}
    if llm.get("error"):
        reasons.append(f"model output invalid: {str(llm['error'])[:100]}")
    return reasons


def run_unit(
    session: Session,
    *,
    bundle: Bundle,
    fixture: OracleFixture,
    input_set: str,
    condition: str,
    llm: BaseLLMProvider | None,
    runner: SandboxRunner,
    oracle: OracleRunner,
    test_only: bool,
    replay_dir: Path | None,
) -> UnitResult:
    started = time.monotonic()
    settings = get_settings()
    if input_set == "approved":
        run_id = seed_mapping_run(session, fixture, with_override=True)
    else:
        run_id = proposed_run(session, bundle, replay_dir)
    version = generate(
        session, run_id, condition=condition, runner=runner, samples_dir=SAMPLES_DIR,
        llm=llm, temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
    )  # fmt: skip
    session.commit()
    files = [f for f in version.files]
    result = UnitResult(
        scenario_id=bundle.scenario.id,
        input_set=input_set,
        condition=condition,
        provider=llm.name if llm and condition != "D" else "none",
        model=str(getattr(llm, "model", llm.name if llm else "none"))
        if condition != "D"
        else "none",
        status=version.status,
        blocked_reasons=_blocked_reasons(version),
        gate={g.stage: g.passed for g in version.gate_results},
        gate_findings=[
            f"{g.stage}: {f['rule']} {f['message']}"[:200]
            for g in version.gate_results
            for f in g.findings
        ][:6],
        files=len(files),
        lines=sum(f.content.count("\n") + 1 for f in files if f.content),
        llm=_llm_use(session, version) if condition != "D" else None,
        test_only=test_only,
    )
    if version.status in RUNNABLE:
        row = run_generated_tests(session, version, runner)
        session.commit()
        parsed = row.result or {}
        result.generated_tests = GeneratedTests(
            outcome=row.outcome, total=parsed.get("total"), passed=parsed.get("passed")
        )
        with tempfile.TemporaryDirectory(prefix="morph-eval-") as tmp:
            directory = materialize(version, Path(tmp) / "bundle")
            checks = oracle(directory, bundle.scenario.id)
        if checks is not None:
            summary, failed, correct = oracle_summary(checks)
            result.oracle = summary
            result.oracle_failed_checks = failed
            result.integration_correct = correct
    result.elapsed_s = round(time.monotonic() - started, 1)
    return result


# ---- command line ------------------------------------------------------------------------------


def check_options(
    conditions: Sequence[str], provider: str | None, test_only: bool, confirm: bool, output: Path
) -> str | None:
    needs_model = any(c in ("L1", "L2") for c in conditions)
    if test_only and output.resolve() == DEFAULT_OUTPUT.resolve():
        return "test-only output can never overwrite the committed report"
    if not needs_model:
        return None
    if provider is None:
        return "L1 and L2 need --provider"
    if provider == "replay" and not test_only:
        return "the replay provider is only allowed with --test-only"
    if provider != "replay" and not test_only and not confirm:
        return "a real model call needs --confirm-real-run (Checkpoint D: an explicit go)"
    if test_only and provider != "replay":
        return "--test-only never calls a real provider"
    return None


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--scenarios", nargs="*", help="scenario ids (default: S1, S3, S4)")
    parser.add_argument("--inputs", default=",".join(INPUT_SETS))
    parser.add_argument("--conditions", default="D")
    parser.add_argument("--provider", choices=["groq", "ollama", "replay"])
    parser.add_argument("--replay-dir", type=Path, help="recorded codegen responses (test-only)")
    parser.add_argument("--mapping-replay-dir", type=Path, help="v0.3 replays for as_proposed")
    parser.add_argument("--test-only", action="store_true")
    parser.add_argument("--confirm-real-run", action="store_true")
    parser.add_argument("--rerun", action="store_true", help="redo units that already have results")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--store-dir", type=Path)
    parser.add_argument("--database-url")
    parser.add_argument("--report-only", action="store_true")
    return parser.parse_args(argv)


def _store_dir(test_only: bool, override: Path | None) -> Path:
    if override is not None:
        return override
    return STORE_ROOT / ("test-only" if test_only else "real")


def main(
    argv: Sequence[str] | None = None,
    *,
    llm_override: BaseLLMProvider | None = None,
    oracle_override: OracleRunner | None = None,
    runner_override: SandboxRunner | None = None,
) -> int:
    args = _parse(argv)
    conditions = [c for c in args.conditions.split(",") if c]
    inputs = [i for i in args.inputs.split(",") if i]
    if not set(conditions) <= set(CONDITIONS) or not set(inputs) <= set(INPUT_SETS):
        print("refused: unknown condition or input set", file=sys.stderr)
        return 2
    test_only = bool(args.test_only or llm_override is not None)
    output: Path = args.output or (TEST_ONLY_OUTPUT if test_only else DEFAULT_OUTPUT)
    provider = "replay" if llm_override is not None else args.provider
    refusal = check_options(conditions, provider, test_only, args.confirm_real_run, output)
    if refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    store_dir = _store_dir(test_only, args.store_dir)
    results_path = store_dir / "results.jsonl"
    wanted = set(args.scenarios or SHORT_IDS)
    bundles = [
        b
        for b in discover(SCENARIOS_ROOT)
        if b.scenario.id in wanted or SHORT_IDS.get(b.scenario.id) in wanted
    ]
    bundles = [b for b in bundles if b.scenario.id in SHORT_IDS]
    status = 0
    if not args.report_only:
        llm = llm_override
        if llm is None and any(c in ("L1", "L2") for c in conditions):
            if provider == "replay":
                llm = ReplayLLMProvider(ResponseStore(args.replay_dir or REPLAYS))
            else:
                try:
                    inner = create_llm_provider(get_settings())
                except LLMError as exc:
                    print(f"refused: {exc}", file=sys.stderr)
                    return 2
                llm = ResumableProvider(inner, store_dir, limits=Limits())
        runner = runner_override or SandboxRunner()
        oracle = oracle_override or run_oracle
        done = {r.key for r in load_results(results_path)}
        settings = get_settings()
        engine = create_engine(args.database_url or settings.database_url)
        with Session(engine) as session:
            for bundle in bundles:
                fixture = load_fixture(bundle.scenario.id)
                for input_set in inputs:
                    for condition in conditions:
                        unit_llm = llm if condition != "D" else None
                        probe = UnitResult(
                            scenario_id=bundle.scenario.id, input_set=input_set,
                            condition=condition, status="",
                            provider=unit_llm.name if unit_llm else "none",
                            model=str(getattr(unit_llm, "model", unit_llm.name)) if unit_llm else "none",
                        )  # fmt: skip
                        if probe.key in done and not args.rerun:
                            print(f"skip (saved): {probe.key}")
                            continue
                        print(f"running {probe.key}", flush=True)
                        try:
                            unit = run_unit(
                                session, bundle=bundle, fixture=fixture, input_set=input_set,
                                condition=condition, llm=unit_llm, runner=runner, oracle=oracle,
                                test_only=test_only, replay_dir=args.mapping_replay_dir,
                            )  # fmt: skip
                        except LLMError as exc:
                            print(f"stopped by the model provider: {exc}", file=sys.stderr)
                            status = 3
                            break
                        append_result(results_path, unit)
                    if status:
                        break
                if status:
                    break
    results = [
        r
        for r in load_results(results_path)
        if r.scenario_id in {b.scenario.id for b in bundles}
        and r.condition in conditions
        and r.input_set in inputs
    ]
    if not results:
        print("no completed units to report", file=sys.stderr)
        return status or 1
    destination = PARTIAL_OUTPUT if status else output
    destination = destination if destination.is_absolute() else REPO_ROOT / destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        render_report(results, datetime.now(UTC).date().isoformat(), test_only=test_only),
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {destination}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
