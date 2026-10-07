"""Running the mapping agent over every target field of an entity, with validation and scoring.

Per target field: retrieve similar source fields (target -> source), build the prompt, make one
LLM call (plus at most one re-ask), validate deterministically, compute confidence, decide
review status. Nothing here trusts the model: a proposal only matters after it passes code.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import EntityRow, SystemVersion
from app.discovery.models import Field
from app.discovery.repository import field_from_row
from app.embeddings.provider import EmbeddingProvider
from app.embeddings.retrieval import similar_fields
from app.embeddings.service import embed_version
from app.llm.base import Attempt, BaseLLMProvider
from app.mapping.agent import propose_field
from app.mapping.confidence import (
    ReviewStatus,
    confidence,
    review_decision,
)
from app.mapping.prompts import Mode, RetrievedField
from app.mapping.proposal import MappingType, Proposal
from app.mapping.samples import load_samples
from app.mapping.transform import JsonScalar
from app.mapping.validate import (
    Reason,
    RetrievalSignal,
    Status,
    ValidationResult,
    validate_proposal,
    validate_run,
)
from app.mapping.validate_v2 import (
    review_decision_v2,
    review_decision_v2_1,
    validate_proposal_v2,
)

_COMPOUND = ("object", "array")
# Validator versions. v1 is the frozen original; v2 and v2.1 are post-hoc (see docs/mapping.md).
VALIDATORS = {
    "v1": (validate_proposal, review_decision),
    "v2": (validate_proposal_v2, review_decision_v2),
    "v2.1": (validate_proposal_v2, review_decision_v2_1),
}
DEFAULT_TOP_K = 5
POOL_LIMIT = 10_000


class MappingRunError(Exception):
    """The run cannot start: unknown version or entity."""


@dataclass(frozen=True)
class MappingItem:
    target_field: str
    proposal: Proposal
    validation: ValidationResult
    confidence: float | None
    review_status: ReviewStatus
    review_reasons: tuple[str, ...]
    attempts: tuple[Attempt, ...]
    invalid_output: bool
    retrieval_ranks: Mapping[str, int | None]


@dataclass(frozen=True)
class MappingRunResult:
    mode: Mode
    provider: str
    model: str
    items: tuple[MappingItem, ...]
    run_reasons: tuple[Reason, ...]
    summary: dict[str, Any] = field(default_factory=dict)
    run_id: int | None = None
    source_version_id: int = 0
    target_version_id: int = 0
    source_entity: str = ""
    target_entity: str = ""
    requirement: str | None = None


def _entity_rows(session: Session, version_id: int, name: str) -> EntityRow:
    row = session.scalar(
        select(EntityRow).where(EntityRow.system_version_id == version_id, EntityRow.name == name)
    )
    if row is None:
        raise MappingRunError(f"entity {name!r} not found in system version {version_id}")
    return row


def load_entity_fields(session: Session, version_id: int, name: str) -> list[tuple[int, Field]]:
    """(field row id, scalar field) pairs of one entity, in declaration order."""
    row = _entity_rows(session, version_id, name)
    pairs = [(f.id, field_from_row(f)) for f in row.fields]
    return [(i, f) for i, f in pairs if f.entity_ref is None and f.json_type not in _COMPOUND]


def summarise(items: tuple[MappingItem, ...]) -> dict[str, Any]:
    calls = [a.metadata for item in items for a in item.attempts]
    return {
        "fields": len(items),
        "review": {s.value: sum(1 for i in items if i.review_status is s) for s in ReviewStatus},
        "validation": {s.value: sum(1 for i in items if i.validation.status is s) for s in Status},
        "unresolved": sum(1 for i in items if i.proposal.mapping_type is MappingType.UNRESOLVED),
        "invalid_output_fields": sum(1 for i in items if i.invalid_output),
        "reasked_fields": sum(1 for i in items if len(i.attempts) > 1),
        "llm_calls": len(calls),
        "input_tokens": sum(c.input_tokens or 0 for c in calls),
        "output_tokens": sum(c.output_tokens or 0 for c in calls),
        "reasoning_tokens": sum(c.reasoning_tokens or 0 for c in calls),
        "latency_ms": sum(c.latency_ms for c in calls),
    }


def run_mapping(
    session: Session,
    llm: BaseLLMProvider,
    embedder: EmbeddingProvider,
    *,
    source_version_id: int,
    target_version_id: int,
    source_entity: str,
    target_entity: str,
    mode: Mode,
    samples_dir: Path,
    top_k: int = DEFAULT_TOP_K,
    requirement: str | None = None,
    validator: str = "v1",
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> MappingRunResult:
    source_version = session.get(SystemVersion, source_version_id)
    target_version = session.get(SystemVersion, target_version_id)
    if source_version is None or target_version is None:
        raise MappingRunError("unknown source or target system version")
    if validator not in VALIDATORS:
        raise MappingRunError(
            f"unknown validator {validator!r}; choose one of {sorted(VALIDATORS)}"
        )
    validate_with, decide = VALIDATORS[validator]
    source_pairs = load_entity_fields(session, source_version_id, source_entity)
    target_pairs = load_entity_fields(session, target_version_id, target_entity)
    source_fields = {f.path: f for _, f in source_pairs}
    samples: list[dict[str, JsonScalar]] = load_samples(
        source_entity, source_version.api_version, samples_dir
    )

    embed_version(session, source_version_id, embedder)
    embed_version(session, target_version_id, embedder)

    items: list[MappingItem] = []
    for target_id, target_field in target_pairs:
        candidates = similar_fields(
            session,
            target_id,
            model_name=embedder.model_name,
            k=POOL_LIMIT,
            target_version_id=source_version_id,
            target_entity=source_entity,
        )
        candidates = [c for c in candidates if c.field_path in source_fields]
        ranks: dict[str, int | None] = {
            c.field_path: position for position, c in enumerate(candidates, start=1)
        }
        retrieved = [
            RetrievedField(position, source_fields[c.field_path], c.distance)
            for position, c in enumerate(candidates[:top_k], start=1)
        ]
        gap = candidates[1].distance - candidates[0].distance if len(candidates) > 1 else None
        signal = RetrievalSignal(
            ranks=ranks, top_gap=gap, top_two=tuple(c.field_path for c in candidates[:2])
        )
        outcome = propose_field(
            llm,
            mode,
            target_field,
            list(source_fields.values()),
            retrieved,
            samples,
            requirement=requirement,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        validation = validate_with(
            outcome.proposal,
            source_fields=source_fields,
            target_field=target_field,
            samples=samples,
            retrieval=signal,
        )
        unresolved = outcome.proposal.mapping_type is MappingType.UNRESOLVED
        score = (
            None
            if unresolved
            else confidence(
                validation, ranks, outcome.proposal.source_fields, outcome.proposal.certainty
            )
        )
        status, reasons = decide(outcome.proposal.mapping_type, validation, score)
        items.append(
            MappingItem(
                target_field=target_field.path,
                proposal=outcome.proposal,
                validation=validation,
                confidence=score,
                review_status=status,
                review_reasons=reasons,
                attempts=outcome.attempts,
                invalid_output=outcome.invalid_output,
                retrieval_ranks=ranks,
            )
        )

    results = tuple(items)
    first_call = next((a.metadata for i in results for a in i.attempts), None)
    run_reasons = tuple(validate_run([f for _, f in target_pairs], [i.proposal for i in results]))
    return MappingRunResult(
        mode=mode,
        provider=first_call.provider if first_call else llm.name,
        model=first_call.model if first_call else "unknown",
        items=results,
        run_reasons=run_reasons,
        summary=summarise(results),
        source_version_id=source_version_id,
        target_version_id=target_version_id,
        source_entity=source_entity,
        target_entity=target_entity,
        requirement=requirement,
    )
