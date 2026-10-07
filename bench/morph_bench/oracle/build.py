"""Build the integration under test: the benchmark acts as the human reviewer.

The mapping run is seeded from the hand-written reference pipelines (bench-side only) and stored
as human-approved versions; the generator then sees only the database, exactly like any other
input. The review fixture is how the test-only human decisions are recorded.
"""

import json
from pathlib import Path
from typing import Any

import yaml
from app.codegen.sandbox import SandboxRunner
from app.codegen.service import generate, materialize
from app.db_models import IntegrationVersion, Mapping, MappingRun, MappingVersion
from app.discovery.parser import parse_spec
from app.discovery.repository import ingest
from app.mapping.confidence import ReviewStatus
from app.mapping.proposal import MappingType
from app.mapping.transform import Transformation
from sqlalchemy.orm import Session

from morph_bench.loader import SCENARIOS_ROOT, load_scenario
from morph_bench.oracle.models import OracleFixture
from morph_bench.systems import REPO_ROOT, load_spec

REFERENCES = Path(__file__).resolve().parents[2] / "references"
SAMPLES_DIR = REPO_ROOT / "mock_systems" / "samples"


def reference_pipelines(scenario_id: str) -> dict[str, Transformation]:
    raw: dict[str, Any] = yaml.safe_load(
        (REFERENCES / f"{scenario_id}.yaml").read_text(encoding="utf-8")
    )
    return {k: Transformation.model_validate(v) for k, v in raw["pipelines"].items()}


def seed_mapping_run(
    session: Session,
    fixture: OracleFixture,
    *,
    statuses: dict[str, ReviewStatus] | None = None,
    with_override: bool = True,
) -> int:
    """Store a mapping run for the scenario; returns its id.

    Every reference pipeline is stored as APPROVED (a human accepted it). Fields in ``statuses``
    get that status instead. The scenario's review override, if any, is stored as an OVERRIDDEN
    human version of its field; without it that field stays UNRESOLVED.
    """
    scenario = load_scenario(SCENARIOS_ROOT / fixture.scenario.removeprefix(""))
    ids: list[int] = []
    for ref in (scenario.source, scenario.target):
        spec = load_spec(ref.system, ref.contract)
        ids.append(ingest(session, parse_spec(spec, ref.system), spec).version_id)
    run = MappingRun(
        source_version_id=ids[0], target_version_id=ids[1], source_entity=scenario.source.entity,
        target_entity=scenario.target.entity, mode="rag", provider="oracle-fixture",
        model="human-review", prompt_version="v1", confidence_version="v1", temperature=0.0,
        requirement=None, summary={}, run_reasons=[],
    )  # fmt: skip
    session.add(run)
    session.flush()
    statuses = statuses or {}
    pipelines = reference_pipelines(fixture.scenario)
    names = list(pipelines)
    if fixture.review_override is not None:
        names.append(fixture.review_override.target_field)
    for position, name in enumerate(names):
        mapping = Mapping(mapping_run_id=run.id, position=position, target_field=name)
        session.add(mapping)
        session.flush()
        override = fixture.review_override
        is_override = override is not None and name == override.target_field
        transformation: Transformation | None
        if is_override:
            assert override is not None
            transformation = Transformation.model_validate(
                {"steps": [{"op": "CONSTANT", "value": override.value}]}
            )
        else:
            transformation = pipelines[name]
        _add_version(
            session, mapping.id, 1, "system",
            MappingType.UNRESOLVED if is_override else MappingType.DIRECT,
            None if is_override else transformation,
            statuses.get(name, ReviewStatus.NEEDS_REVIEW if is_override else ReviewStatus.APPROVED),
        )  # fmt: skip
        if is_override and with_override:
            _add_version(
                session, mapping.id, 2, "human", MappingType.CONSTANT, transformation,
                ReviewStatus.OVERRIDDEN,
            )  # fmt: skip
    session.flush()
    return run.id


def _add_version(
    session: Session,
    mapping_id: int,
    version: int,
    author: str,
    mapping_type: MappingType,
    transformation: Transformation | None,
    status: ReviewStatus,
) -> None:
    session.add(
        MappingVersion(
            mapping_id=mapping_id, version=version, author=author,
            mapping_type=mapping_type.value,
            source_fields=list(transformation.source_fields) if transformation else [],
            transformation=json.loads(transformation.model_dump_json()) if transformation else None,
            unresolved_reason=None if transformation else "no source", rationale="oracle review",
            alternatives=[], certainty="HIGH", validation_status="PASS", validation_reasons=[],
            outputs_preview=[], confidence=None, review_status=status.value, review_reasons=[],
        )
    )  # fmt: skip


def build_integration(
    session: Session,
    fixture: OracleFixture,
    runner: SandboxRunner,
    *,
    statuses: dict[str, ReviewStatus] | None = None,
    with_override: bool = True,
) -> IntegrationVersion:
    run_id = seed_mapping_run(session, fixture, statuses=statuses, with_override=with_override)
    return generate(session, run_id, runner=runner, samples_dir=SAMPLES_DIR)


def build_bundle(
    session: Session, fixture: OracleFixture, runner: SandboxRunner, directory: Path
) -> tuple[IntegrationVersion, Path]:
    version = build_integration(session, fixture, runner)
    return version, materialize(version, directory)
