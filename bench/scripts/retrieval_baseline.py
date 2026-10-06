"""Measure how well embedding retrieval finds the correct target field. Run from ``bench/``:

    uv run python -m scripts.retrieval_baseline

Ingests the scenario's two OpenAPI contracts into the dev database with the real embedding
provider, ranks target fields for every source field in the answer key, and writes
``docs/retrieval-baseline.md``. The fake provider is refused unless ``--test-only`` is given,
and a test-only run can never overwrite the committed report.
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.db_models import EntityRow, FieldRow
from app.discovery.parser import parse_spec
from app.discovery.repository import IngestResult, ingest
from app.discovery.source import load_spec
from app.embeddings.provider import EmbeddingProvider
from app.embeddings.retrieval import similar_fields
from app.embeddings.service import embed_version
from app.settings import get_settings
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from morph_bench.loader import SCENARIOS_ROOT, load_bundle
from morph_bench.models import Bundle
from morph_bench.retrieval_baseline import (
    DEFAULT_OUTPUT,
    BaselineResult,
    Candidate,
    Scope,
    check_options,
    evaluate,
    render,
)
from morph_bench.systems import REPO_ROOT, spec_path

DEFAULT_SCENARIO = "crm_customer_to_support_user"
TEST_ONLY_OUTPUT = Path(".run/retrieval-baseline.test-only.md")
POOL_LIMIT = 100_000  # larger than any pool: rank every candidate


def _ingest_and_embed(session: Session, provider: EmbeddingProvider, system: str) -> IngestResult:
    spec = load_spec(spec_path(system, "v1"))
    result = ingest(session, parse_spec(spec, system), spec)
    embed_version(session, result.version_id, provider)
    return result


def run_baseline(
    session: Session,
    provider: EmbeddingProvider,
    bundle: Bundle,
    *,
    provider_kind: str,
    test_only: bool,
    run_date: str | None = None,
) -> BaselineResult:
    scenario = bundle.scenario
    source = _ingest_and_embed(session, provider, scenario.source.system)
    target = _ingest_and_embed(session, provider, scenario.target.system)

    rows = session.execute(
        select(FieldRow.path, FieldRow.id)
        .join(EntityRow, EntityRow.id == FieldRow.entity_id)
        .where(
            EntityRow.system_version_id == source.version_id,
            EntityRow.name == scenario.source.entity,
        )
    ).all()
    source_fields: dict[str, int] = {path: field_id for path, field_id in rows}
    pool_all = session.scalar(
        select(func.count())
        .select_from(FieldRow)
        .join(EntityRow, EntityRow.id == FieldRow.entity_id)
        .where(EntityRow.system_version_id == target.version_id)
    )
    pool_resource = session.scalar(
        select(func.count())
        .select_from(FieldRow)
        .join(EntityRow, EntityRow.id == FieldRow.entity_id)
        .where(
            EntityRow.system_version_id == target.version_id,
            EntityRow.name == scenario.target.entity,
        )
    )

    cache: dict[tuple[str, Scope], list[Candidate]] = {}

    def retrieve(source_field: str, scope: Scope) -> list[Candidate]:
        key = (source_field, scope)
        if key not in cache:
            found = similar_fields(
                session,
                source_fields[source_field],
                model_name=provider.model_name,
                k=POOL_LIMIT,
                target_version_id=target.version_id,
                target_entity=scenario.target.entity if scope == "resource" else None,
            )
            cache[key] = [Candidate(r.entity_name, r.field_path, r.distance) for r in found]
        return cache[key]

    return evaluate(
        bundle,
        retrieve,
        model_name=provider.model_name,
        provider=provider_kind,
        run_date=run_date or datetime.now(UTC).date().isoformat(),
        test_only=test_only,
        pool_sizes={"all": pool_all or 0, "resource": pool_resource or 0},
    )


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    parser.add_argument("--provider", choices=["fastembed", "fake"], help="default: from settings")
    parser.add_argument("--test-only", action="store_true", help="allow the fake provider (tests)")
    parser.add_argument("--output", type=Path, help=f"default: {DEFAULT_OUTPUT}")
    parser.add_argument("--database-url", help="default: DATABASE_URL (the dev database)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    settings = get_settings()
    provider_kind: str = args.provider or settings.embedding_provider
    output: Path = args.output or (TEST_ONLY_OUTPUT if args.test_only else DEFAULT_OUTPUT)
    refusal = check_options(provider_kind, args.test_only, output)
    if refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2

    provider: EmbeddingProvider
    if provider_kind == "fake":
        from app.embeddings.fake import FakeEmbeddingProvider

        provider = FakeEmbeddingProvider()
    else:
        from app.embeddings.fastembed_provider import FastEmbedProvider

        provider = FastEmbedProvider(
            settings.embedding_model, cache_dir=settings.embedding_cache_dir
        )

    bundle = load_bundle(SCENARIOS_ROOT / args.scenario)
    engine = create_engine(args.database_url or settings.database_url)
    with Session(engine) as session:
        result = run_baseline(
            session, provider, bundle, provider_kind=provider_kind, test_only=args.test_only
        )
        session.commit()

    destination = output if output.is_absolute() else REPO_ROOT / output
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(result), encoding="utf-8", newline="\n")
    print(f"wrote {destination}")
    for scope in ("all", "resource"):
        cells = ", ".join(f"@{k}={result.hits(scope, k)}/{len(result.mappings)}" for k in (1, 3, 5))
        print(f"{scope}: {cells}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
