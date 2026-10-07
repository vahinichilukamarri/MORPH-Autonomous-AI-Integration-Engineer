"""Rescore the saved v1 responses with validator v2 (post-hoc). Run from ``bench/``:

    uv run --group embeddings python -m scripts.rescore_v2

Makes **zero** LLM calls: the provider underneath the resumable store raises if it is ever asked
for a completion, so every response must come from the saved v1 run. The v1 outcome is first
reproduced from those responses and compared with the saved results; only then is validator v2
applied, and the comparison is spliced into docs/mapping-eval.md below the untouched v1 report.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.embeddings.provider import EmbeddingProvider
from app.llm.base import BaseLLMProvider, LLMRequest, RawCompletion
from app.mapping.confidence import ReviewStatus
from app.mapping.runner import MappingItem, MappingRunResult, run_mapping
from app.mapping.validate import Code
from app.mapping.validate_v2 import (
    LOSSY_CODES,
    REVIEW_FORCING_CODES_V2,
    review_decision_v2,
)
from app.settings import get_settings
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from morph_bench.grader import ProposedMapping, ScenarioGrade, grade_scenario
from morph_bench.loader import SCENARIOS_ROOT, discover
from morph_bench.mapping_eval import Limits, load_records
from morph_bench.models import Bundle
from morph_bench.rescore_v2 import (
    LLM_CONFIGS,
    FieldPair,
    V2Meta,
    render_v2_section,
    splice_section,
)
from morph_bench.systems import REPO_ROOT
from scripts.run_mapping_eval import (
    ResumableProvider,
    _describe_expected,
    _describe_proposal,
    _ingest_scenario,
    _store_dir,
)

DOC = REPO_ROOT / "docs" / "mapping-eval.md"


class NoNewCalls(BaseLLMProvider):
    """The provider under the store: any attempt to reach a model is a bug in the rescoring."""

    name = "none"

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        raise AssertionError("validator v2 rescoring must not make new LLM calls")


def _proposed(item: MappingItem) -> ProposedMapping:
    return ProposedMapping(
        item.target_field,
        item.proposal.mapping_type.value,
        item.proposal.source_fields,
        item.proposal.transformation,
        flagged=item.review_status is ReviewStatus.NEEDS_REVIEW,
        confidence=item.confidence,
        invalid_output=item.invalid_output,
    )


def _run(
    session: Session,
    llm: BaseLLMProvider,
    embedder: EmbeddingProvider,
    bundle: Bundle,
    config: str,
    versions: tuple[int, int],
    validator: str,
    samples_dir: Path,
    temperature: float,
    max_output_tokens: int | None,
) -> tuple[MappingRunResult, ScenarioGrade]:
    scenario = bundle.scenario
    result = run_mapping(
        session,
        llm,
        embedder,
        source_version_id=versions[0],
        target_version_id=versions[1],
        source_entity=scenario.source.entity,
        target_entity=scenario.target.entity,
        mode="full_schema" if config == "B" else "rag",
        samples_dir=samples_dir,
        requirement=scenario.requirement,
        validator=validator,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    return result, grade_scenario(bundle, [_proposed(i) for i in result.items])


def _policy_reviews(item: MappingItem) -> dict[str, str]:
    """The v2 review decision under each policy for the lossy checks."""
    kind = item.proposal.mapping_type
    policies = {
        "v2": REVIEW_FORCING_CODES_V2,
        "v2_truncation": REVIEW_FORCING_CODES_V2 | {Code.LOSSY_TRUNCATION},
        "v2_collapse": REVIEW_FORCING_CODES_V2 | {Code.INFORMATION_LOSS_ENUM, Code.LOSSY_COLLAPSE},
        "v2_all_lossy": REVIEW_FORCING_CODES_V2
        | LOSSY_CODES
        | {Code.INFORMATION_LOSS_ENUM, Code.INFORMATION_LOSS_COALESCE},
    }
    return {
        key: review_decision_v2(kind, item.validation, item.confidence, forcing=forcing)[0].value
        for key, forcing in policies.items()
    }


def rescore(
    session: Session,
    *,
    bundles: Sequence[Bundle],
    configs: Sequence[str],
    store_dir: Path,
    embedder: EmbeddingProvider,
    samples_dir: Path,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> tuple[list[FieldPair], V2Meta]:
    saved = {
        (r.scenario_id, r.config, r.run_index): r for r in load_records(store_dir / "results.jsonl")
    }
    llm = ResumableProvider(NoNewCalls(), store_dir, limits=Limits())
    llm.run_index = 1
    pairs: list[FieldPair] = []
    reproduced = checked = reused = 0
    model = "unknown"
    for bundle in bundles:
        source, target = _ingest_scenario(session, embedder, bundle)
        versions = (source.version_id, target.version_id)
        for config in configs:
            tail = (samples_dir, temperature, max_output_tokens)
            v1, v1_grade = _run(session, llm, embedder, bundle, config, versions, "v1", *tail)
            v2, v2_grade = _run(session, llm, embedder, bundle, config, versions, "v2", *tail)
            record = saved[(bundle.scenario.id, config, 1)]
            model = record.model
            reused += sum(len(i.attempts) for i in v1.items)
            entries = {m.target_field: m for m in bundle.answer_key.mappings}
            was = {f.grade.target_field: f for f in record.fields}
            for item1, item2, g1, g2 in zip(
                v1.items, v2.items, v1_grade.fields, v2_grade.fields, strict=True
            ):
                saved_field = was[item1.target_field]
                checked += 1
                if (
                    g1.fully_correct == saved_field.grade.fully_correct
                    and item1.review_status.value == saved_field.review_status
                    and item1.confidence == saved_field.grade.confidence
                ):
                    reproduced += 1
                assert g1.fully_correct == g2.fully_correct, "the mappings must be identical"
                reviews = {"v1": item1.review_status.value, **_policy_reviews(item2)}
                pairs.append(
                    FieldPair(
                        scenario_id=bundle.scenario.id,
                        config=config,
                        target_field=item1.target_field,
                        fully_correct=g1.fully_correct,
                        unresolved=item1.proposal.mapping_type.value == "UNRESOLVED",
                        proposed=_describe_proposal(item1.proposal),
                        expected=_describe_expected(entries[item1.target_field]),
                        v1_confidence=item1.confidence,
                        v2_confidence=item2.confidence,
                        v1_codes=tuple(r.code.value for r in item1.validation.reasons),
                        v2_codes=tuple(r.code.value for r in item2.validation.reasons),
                        reviews=reviews,
                        confidence={
                            "v1": item1.confidence,
                            "v2": item2.confidence,
                            "v2_truncation": item2.confidence,
                            "v2_collapse": item2.confidence,
                            "v2_all_lossy": item2.confidence,
                        },
                        detail=g1.detail,
                    )
                )
    meta = V2Meta(
        run_date=datetime.now(UTC).date().isoformat(),
        model=model,
        responses_reused=reused,
        llm_calls_made=llm.network_calls,
        v1_reproduced=reproduced,
        fields_checked=checked,
    )
    return pairs, meta


def main(
    argv: Sequence[str] | None = None, *, embedder_override: EmbeddingProvider | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--store-dir", type=Path)
    parser.add_argument("--database-url")
    parser.add_argument("--doc", type=Path, default=DOC, help="report to splice the section into")
    parser.add_argument("--scenarios", nargs="*")
    args = parser.parse_args(argv)
    settings = get_settings()
    store_dir = _store_dir("groq", settings.groq_model, args.store_dir)
    bundles = [
        b for b in discover(SCENARIOS_ROOT) if not args.scenarios or b.scenario.id in args.scenarios
    ]
    embedder = embedder_override
    if embedder is None:
        from app.embeddings.fastembed_provider import FastEmbedProvider

        embedder = FastEmbedProvider(
            settings.embedding_model, cache_dir=settings.embedding_cache_dir
        )
    engine = create_engine(args.database_url or settings.database_url)
    with Session(engine) as session:
        pairs, meta = rescore(
            session,
            bundles=bundles,
            configs=LLM_CONFIGS,
            store_dir=store_dir,
            embedder=embedder,
            samples_dir=settings.samples_dir,
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
        )
    if meta.llm_calls_made != 0 or meta.v1_reproduced != meta.fields_checked:
        print(
            f"refused: {meta.llm_calls_made} LLM calls, v1 reproduced "
            f"{meta.v1_reproduced}/{meta.fields_checked}; not writing the report",
            file=sys.stderr,
        )
        return 1
    section = render_v2_section(pairs, meta)
    existing = args.doc.read_text(encoding="utf-8")
    args.doc.write_text(splice_section(existing, section), encoding="utf-8", newline="\n")
    print(f"wrote the v2 section into {args.doc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
