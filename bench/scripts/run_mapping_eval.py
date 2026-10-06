"""Evaluate MORPH's mapping step against the hand-written answer keys. Run from ``bench/``:

    uv run --group embeddings python -m scripts.run_mapping_eval --n-runs 1

Three configurations are compared on every scenario: A retrieval only (no LLM), B LLM with the
full schema, C LLM with retrieved fields (RAG). Every completed LLM call is saved by prompt hash
and run index, so a run that hits a rate limit stops cleanly and a later invocation resumes
without repeating finished calls. The report, docs/mapping-eval.md, is generated entirely from
the saved results. Fake or replayed providers are refused unless --test-only is given, and
test-only output can never overwrite the committed report.
"""

import argparse
import re
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.db_models import SystemVersion
from app.discovery.parser import parse_spec
from app.discovery.repository import IngestResult, ingest
from app.embeddings.provider import EmbeddingProvider
from app.embeddings.retrieval import similar_fields
from app.embeddings.service import embed_version
from app.llm.base import (
    BaseLLMProvider,
    LLMError,
    LLMRequest,
    RateLimitExhausted,
    RawCompletion,
)
from app.llm.factory import create_llm_provider
from app.llm.store import Recorded, ReplayLLMProvider, ResponseStore
from app.mapping.confidence import (
    CONFIDENCE_VERSION,
    ReviewStatus,
    confidence,
    review_decision,
)
from app.mapping.prompts import PROMPT_VERSION, Mode
from app.mapping.proposal import Certainty, MappingType, Proposal
from app.mapping.runner import (
    MappingItem,
    MappingRunResult,
    load_entity_fields,
    run_mapping,
    summarise,
)
from app.mapping.samples import load_samples
from app.mapping.store import save_run
from app.mapping.transform import Copy, Transformation
from app.mapping.validate import RetrievalSignal, validate_proposal
from app.settings import get_settings
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from morph_bench.grader import ProposedMapping, ScenarioGrade, grade_scenario
from morph_bench.loader import SCENARIOS_ROOT, discover
from morph_bench.mapping_eval import (
    DEFAULT_OUTPUT,
    FieldRecord,
    Limits,
    Metrics,
    ReportMeta,
    RunRecord,
    append_record,
    check_options,
    load_records,
    render_report,
)
from morph_bench.models import Bundle, MappingEntry
from morph_bench.models import MappingType as ExpectedType
from morph_bench.systems import REPO_ROOT, load_spec

TEST_ONLY_OUTPUT = Path(".run/mapping-eval.test-only.md")
PARTIAL_OUTPUT = Path(".run/mapping-eval.partial.md")
STORE_ROOT = REPO_ROOT / ".cache" / "mapping-eval"
REPLAY_DIR = Path(__file__).resolve().parents[1] / "replays"
LABEL = f"{PROMPT_VERSION}/{CONFIDENCE_VERSION}"
CONFIGS = ("A", "B", "C")
POOL_LIMIT = 10_000
PACING_CEILING_S = 90.0


# ---- resumable provider ------------------------------------------------------------------------


def parse_duration(text: str) -> float:
    """Seconds from a rate-limit reset header such as '2.5s', '1m30.5s' or '120ms'."""
    total = 0.0
    for number, unit in re.findall(r"([\d.]+)(ms|s|m|h)", text):
        total += float(number) * {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
    return total


class ResumableProvider(BaseLLMProvider):
    """Saves every completed call by (prompt hash, run index) and serves it again on resume."""

    def __init__(
        self,
        inner: BaseLLMProvider,
        store_dir: Path,
        *,
        limits: Limits,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._store = ResponseStore(store_dir / "calls")
        self._file = store_dir / "calls" / "calls.jsonl"
        self._limits = limits
        self._limits_path = store_dir / "limits.json"
        self._sleep = sleep
        self.name = inner.name
        self.run_index = 1
        self.record_file: Path | None = None
        self.network_calls = 0

    def _key(self, request: LLMRequest, model: type[BaseModel]) -> str:
        return f"{request.fingerprint(model)}:{self.run_index}"

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        key = self._key(request, response_model)
        hit = self._store.get(key)
        if hit is not None:
            return _to_completion(hit, request.fingerprint(response_model))
        raw = self._inner.complete_raw(request, response_model)
        self.network_calls += 1
        meta = raw.metadata
        record = Recorded(
            key, raw.text, meta.provider, meta.model, meta.input_tokens, meta.output_tokens,
            meta.reasoning_tokens, meta.latency_ms,
        )  # fmt: skip
        self._store.put(record, file=self._file)
        if self.record_file is not None and self.run_index == 1:
            plain = Recorded(
                request.fingerprint(response_model), raw.text, meta.provider, meta.model,
                meta.input_tokens, meta.output_tokens, meta.reasoning_tokens, meta.latency_ms,
            )  # fmt: skip
            ResponseStore(self.record_file).put(plain, file=self.record_file)
        if meta.rate_limits:
            self._limits.last_headers = dict(meta.rate_limits)
            self._limits.save(self._limits_path)
            self._pace(meta.rate_limits, (meta.input_tokens or 0) + (meta.output_tokens or 0))
        return raw

    def _pace(self, headers: dict[str, str], last_call_tokens: int) -> None:
        """Wait for the token window to reset when the next call would probably not fit."""
        remaining = headers.get("x-ratelimit-remaining-tokens")
        reset = headers.get("x-ratelimit-reset-tokens")
        if remaining is None or reset is None:
            return
        try:
            if float(remaining) < last_call_tokens * 1.2:
                self._sleep(min(parse_duration(reset), PACING_CEILING_S))
        except ValueError:
            return


def _to_completion(record: Recorded, prompt_hash: str) -> RawCompletion:
    from app.llm.base import CallMetadata

    return RawCompletion(
        record.text,
        CallMetadata(
            provider=record.provider,
            model=record.model,
            prompt_hash=prompt_hash,
            latency_ms=record.latency_ms,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            reasoning_tokens=record.reasoning_tokens,
            source="cache",
        ),
    )


# ---- one scenario, one configuration -----------------------------------------------------------


def _ingest_scenario(
    session: Session, embedder: EmbeddingProvider, bundle: Bundle
) -> tuple[IngestResult, IngestResult]:
    scenario = bundle.scenario
    out: list[IngestResult] = []
    for role, ref in (("source", scenario.source), ("target", scenario.target)):
        spec = load_spec(ref.system, ref.contract, scenario.spec_transform)
        name = f"eval-{scenario.id}-{role}"
        result = ingest(session, parse_spec(dict(spec), name), dict(spec))
        embed_version(session, result.version_id, embedder)
        out.append(result)
    return out[0], out[1]


def _describe_expected(entry: MappingEntry) -> str:
    if entry.mapping_type is ExpectedType.UNRESOLVED:
        return "UNRESOLVED"
    return f"{entry.mapping_type.value} [{', '.join(entry.source_fields)}]"


def _describe_proposal(proposal: Proposal) -> str:
    if proposal.mapping_type is MappingType.UNRESOLVED:
        return f"UNRESOLVED ({(proposal.unresolved_reason or '').strip()[:80]})"
    ops = ">".join(s.op for s in proposal.transformation.steps) if proposal.transformation else "-"
    return f"{proposal.mapping_type.value} [{', '.join(proposal.source_fields)}] {ops}"


def _proposed(item: MappingItem) -> ProposedMapping:
    return ProposedMapping(
        target_field=item.target_field,
        mapping_type=item.proposal.mapping_type.value,
        source_fields=item.proposal.source_fields,
        transformation=item.proposal.transformation,
        flagged=item.review_status is ReviewStatus.NEEDS_REVIEW,
        confidence=item.confidence,
        invalid_output=item.invalid_output,
    )


def run_retrieval_only(
    session: Session,
    embedder: EmbeddingProvider,
    *,
    source_version_id: int,
    target_version_id: int,
    source_entity: str,
    target_entity: str,
    samples_dir: Path,
) -> MappingRunResult:
    """Configuration A: copy the top-1 retrieved source field. No LLM; validated like B and C."""
    source_pairs = load_entity_fields(session, source_version_id, source_entity)
    target_pairs = load_entity_fields(session, target_version_id, target_entity)
    source_fields = {f.path: f for _, f in source_pairs}
    source_version = session.get(SystemVersion, source_version_id)
    assert source_version is not None
    samples = load_samples(source_entity, source_version.api_version, samples_dir)
    items: list[MappingItem] = []
    for target_id, target_field in target_pairs:
        found = similar_fields(
            session,
            target_id,
            model_name=embedder.model_name,
            k=POOL_LIMIT,
            target_version_id=source_version_id,
            target_entity=source_entity,
        )
        found = [c for c in found if c.field_path in source_fields]
        ranks = {c.field_path: i for i, c in enumerate(found, start=1)}
        gap = found[1].distance - found[0].distance if len(found) > 1 else None
        signal = RetrievalSignal(ranks, gap, tuple(c.field_path for c in found[:2]))
        if found:
            top = found[0].field_path
            proposal = Proposal(
                target_field=target_field.path,
                mapping_type=MappingType.DIRECT,
                source_fields=(top,),
                transformation=Transformation(steps=(Copy(field=top),)),
                rationale="top-1 retrieved source field",
                certainty=Certainty.LOW,
            )
        else:
            proposal = Proposal.unresolved(target_field.path, "nothing retrieved")
        validation = validate_proposal(
            proposal,
            source_fields=source_fields,
            target_field=target_field,
            samples=samples,
            retrieval=signal,
        )
        unresolved = proposal.mapping_type is MappingType.UNRESOLVED
        score = (
            None
            if unresolved
            else confidence(validation, ranks, proposal.source_fields, proposal.certainty)
        )
        status, reasons = review_decision(proposal.mapping_type, validation, score)
        items.append(
            MappingItem(
                target_field.path, proposal, validation, score, status, reasons, (), False, ranks
            )
        )
    results = tuple(items)
    return MappingRunResult(
        mode="rag",
        provider="retrieval",
        model="none",
        items=results,
        run_reasons=(),
        summary=summarise(results),
        source_version_id=source_version_id,
        target_version_id=target_version_id,
        source_entity=source_entity,
        target_entity=target_entity,
    )


def to_record(
    bundle: Bundle, result: MappingRunResult, grade: ScenarioGrade, config: str, run_index: int
) -> RunRecord:
    by_target = {i.target_field: i for i in result.items}
    entries = {m.target_field: m for m in bundle.answer_key.mappings}
    fields = tuple(
        FieldRecord(
            grade=g,
            proposed=_describe_proposal(by_target[g.target_field].proposal)
            if g.target_field in by_target
            else "(none)",
            expected=_describe_expected(entries[g.target_field]),
            review_status=by_target[g.target_field].review_status.value
            if g.target_field in by_target
            else "-",
            validation_status=by_target[g.target_field].validation.status.value
            if g.target_field in by_target
            else "-",
        )
        for g in grade.fields
    )
    s = result.summary
    metrics = Metrics(
        fields=len(result.items),
        llm_calls=s["llm_calls"],
        invalid_output_fields=s["invalid_output_fields"],
        reasked_fields=s["reasked_fields"],
        needs_review=s["review"]["NEEDS_REVIEW"],
        unresolved=s["unresolved"],
        input_tokens=s["input_tokens"],
        output_tokens=s["output_tokens"],
        reasoning_tokens=s["reasoning_tokens"],
        latency_ms=s["latency_ms"],
    )
    return RunRecord(
        label=LABEL,
        scenario_id=bundle.scenario.id,
        config=config,
        run_index=run_index,
        provider=result.provider,
        model=result.model,
        fields=fields,
        metrics=metrics,
    )


def evaluate_unit(
    session: Session,
    bundle: Bundle,
    config: str,
    run_index: int,
    llm: BaseLLMProvider | None,
    embedder: EmbeddingProvider,
    versions: tuple[IngestResult, IngestResult],
    samples_dir: Path,
    temperature: float,
    max_output_tokens: int | None,
) -> RunRecord:
    source, target = versions
    scenario = bundle.scenario
    if config == "A":
        result = run_retrieval_only(
            session,
            embedder,
            source_version_id=source.version_id,
            target_version_id=target.version_id,
            source_entity=scenario.source.entity,
            target_entity=scenario.target.entity,
            samples_dir=samples_dir,
        )
    else:
        assert llm is not None
        mode: Mode = "full_schema" if config == "B" else "rag"
        result = run_mapping(
            session,
            llm,
            embedder,
            source_version_id=source.version_id,
            target_version_id=target.version_id,
            source_entity=scenario.source.entity,
            target_entity=scenario.target.entity,
            mode=mode,
            samples_dir=samples_dir,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        save_run(session, result, temperature)
        session.commit()
    grade = grade_scenario(bundle, [_proposed(i) for i in result.items])
    return to_record(bundle, result, grade, config, run_index)


# ---- command line ------------------------------------------------------------------------------


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--scenarios", nargs="*", help="scenario ids (default: all)")
    parser.add_argument("--configs", default="A,B,C", help="comma separated subset of A,B,C")
    parser.add_argument("--n-runs", type=int, default=1, help="repeated runs per LLM configuration")
    parser.add_argument(
        "--provider", choices=["groq", "ollama", "replay"], help="default: settings"
    )
    parser.add_argument("--replay-dir", type=Path, default=REPLAY_DIR)
    parser.add_argument("--test-only", action="store_true", help="allow replay/fake providers")
    parser.add_argument("--output", type=Path, help=f"default: {DEFAULT_OUTPUT}")
    parser.add_argument("--database-url", help="default: DATABASE_URL (the dev database)")
    parser.add_argument("--store-dir", type=Path, help="resumable call and result store")
    parser.add_argument(
        "--record-replays",
        nargs="*",
        metavar="SCENARIO",
        help="also record run-1 responses of these scenarios into bench/replays/ (real runs only)",
    )
    parser.add_argument("--report-only", action="store_true", help="render from saved results")
    return parser.parse_args(argv)


def _store_dir(provider: str, model: str, override: Path | None) -> Path:
    if override is not None:
        return override
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{provider}-{model}")
    return STORE_ROOT / safe


def run_evaluation(
    session: Session,
    *,
    bundles: Sequence[Bundle],
    configs: Sequence[str],
    n_runs: int,
    llm: ResumableProvider | BaseLLMProvider | None,
    embedder: EmbeddingProvider,
    results_path: Path,
    samples_dir: Path,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
    record_scenarios: Sequence[str] = (),
    replay_dir: Path = REPLAY_DIR,
    log: Callable[[str], None] = print,
) -> tuple[int, int]:
    """Run every missing unit. Returns (units completed this call, units still missing)."""
    existing = {(r.label, r.scenario_id, r.config, r.run_index) for r in load_records(results_path)}
    todo: list[tuple[Bundle, str, int]] = []
    for bundle in bundles:
        for config in configs:
            for run_index in range(1, (1 if config == "A" else n_runs) + 1):
                if (LABEL, bundle.scenario.id, config, run_index) not in existing:
                    todo.append((bundle, config, run_index))
    done = 0
    prepared: dict[str, tuple[IngestResult, IngestResult]] = {}
    for bundle, config, run_index in todo:
        scenario_id = bundle.scenario.id
        if scenario_id not in prepared:
            prepared[scenario_id] = _ingest_scenario(session, embedder, bundle)
            session.commit()
        if isinstance(llm, ResumableProvider):
            llm.run_index = run_index
            if record_scenarios:
                llm.record_file = (
                    replay_dir / f"{scenario_id}.jsonl" if scenario_id in record_scenarios else None
                )
        log(f"[{done + 1}/{len(todo)}] {scenario_id} config {config} run {run_index}")
        record = evaluate_unit(
            session,
            bundle,
            config,
            run_index,
            llm,
            embedder,
            prepared[scenario_id],
            samples_dir,
            temperature,
            max_output_tokens,
        )
        append_record(results_path, record)
        done += 1
    return done, 0


def main(
    argv: Sequence[str] | None = None,
    *,
    llm_override: BaseLLMProvider | None = None,
    embedder_override: EmbeddingProvider | None = None,
) -> int:
    args = _parse(argv)
    settings = get_settings()
    provider_kind: str = args.provider or settings.llm_provider
    if llm_override is not None:
        provider_kind = "replay"
    output: Path = args.output or (TEST_ONLY_OUTPUT if args.test_only else DEFAULT_OUTPUT)
    refusal = check_options(provider_kind, args.test_only, output)
    if refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    if args.record_replays is not None and args.test_only:
        print("refused: --record-replays needs a real provider", file=sys.stderr)
        return 2

    configs = [c for c in args.configs.split(",") if c]
    if not set(configs) <= set(CONFIGS):
        print("refused: --configs must be a subset of A,B,C", file=sys.stderr)
        return 2
    bundles = [
        b for b in discover(SCENARIOS_ROOT) if not args.scenarios or b.scenario.id in args.scenarios
    ]

    model_label = (
        (settings.groq_model if provider_kind == "groq" else settings.ollama_model)
        if not args.test_only
        else "test"
    )
    store_dir = _store_dir(provider_kind, model_label, args.store_dir)
    limits = Limits.load(store_dir / "limits.json")
    results_path = store_dir / "results.jsonl"

    llm: BaseLLMProvider | None = None
    if any(c in configs for c in ("B", "C")) and not args.report_only:
        if llm_override is not None:
            llm = llm_override
        elif provider_kind == "replay":
            llm = ReplayLLMProvider(ResponseStore(args.replay_dir))
        else:

            def on_wait(retry_after: float, attempt: int) -> None:
                limits.rate_limit_waits.append(retry_after)
                limits.save(store_dir / "limits.json")

            try:
                inner = create_llm_provider(settings, on_rate_limit=on_wait)
            except LLMError as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 2
            llm = ResumableProvider(inner, store_dir, limits=limits)

    embedder: EmbeddingProvider
    if embedder_override is not None:
        embedder = embedder_override
    elif args.report_only:
        from app.embeddings.fake import FakeEmbeddingProvider  # never used when only reporting

        embedder = FakeEmbeddingProvider()
    elif args.test_only:
        from app.embeddings.fake import FakeEmbeddingProvider

        embedder = FakeEmbeddingProvider()
    else:
        from app.embeddings.fastembed_provider import FastEmbedProvider

        embedder = FastEmbedProvider(
            settings.embedding_model, cache_dir=settings.embedding_cache_dir
        )

    status = 0
    if not args.report_only:
        engine = create_engine(args.database_url or settings.database_url)
        with Session(engine) as session:
            try:
                run_evaluation(
                    session,
                    bundles=bundles,
                    configs=configs,
                    n_runs=args.n_runs,
                    llm=llm,
                    embedder=embedder,
                    results_path=results_path,
                    samples_dir=settings.samples_dir,
                    temperature=settings.llm_temperature,
                    max_output_tokens=settings.llm_max_output_tokens,
                    record_scenarios=args.record_replays or (),
                )
            except RateLimitExhausted as exc:
                limits.exhausted.append(
                    {"retry_after_s": exc.retry_after_s, "message": str(exc)[:200]}
                )
                limits.save(store_dir / "limits.json")
                print(
                    f"stopped by a rate limit: {exc}\nFinished calls are saved in {store_dir}; "
                    "run the same command again later to resume.",
                    file=sys.stderr,
                )
                status = 3

    records = load_records(results_path)
    wanted = {b.scenario.id for b in bundles}
    records = [r for r in records if r.scenario_id in wanted and r.config in configs]
    if not records:
        print("no completed runs to report", file=sys.stderr)
        return status or 1
    meta = ReportMeta(
        run_date=datetime.now(UTC).date().isoformat(),
        test_only=args.test_only,
        n_runs_requested=args.n_runs,
        limits=limits,
    )
    if status != 0:
        # an interrupted run must never overwrite the committed report with partial results
        output = PARTIAL_OUTPUT
    destination = output if output.is_absolute() else REPO_ROOT / output
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_report(records, meta), encoding="utf-8", newline="\n")
    print(f"wrote {destination}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
