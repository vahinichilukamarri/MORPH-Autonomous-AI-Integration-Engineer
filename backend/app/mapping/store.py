"""Persisting mapping runs, versioned mappings and LLM call metadata."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import LLMCall, Mapping, MappingRun, MappingVersion
from app.mapping.confidence import CONFIDENCE_VERSION, ReviewStatus
from app.mapping.prompts import PROMPT_VERSION
from app.mapping.proposal import Certainty, MappingType, Proposal
from app.mapping.runner import MappingItem, MappingRunResult
from app.mapping.transform import Transformation
from app.mapping.validate import Code, Reason, Status, ValidationResult


def reasons_json(result: ValidationResult) -> list[dict[str, str]]:
    return [
        {"code": r.code.value, "severity": r.severity.value, "detail": r.detail}
        for r in result.reasons
    ]


def _version_row(
    mapping_id: int,
    version: int,
    author: str,
    proposal: Proposal,
    validation: ValidationResult,
    score: float | None,
    review_status: ReviewStatus,
    review_reasons: tuple[str, ...],
) -> MappingVersion:
    return MappingVersion(
        mapping_id=mapping_id,
        version=version,
        author=author,
        mapping_type=proposal.mapping_type.value,
        source_fields=list(proposal.source_fields),
        transformation=(
            proposal.transformation.model_dump(mode="json") if proposal.transformation else None
        ),
        unresolved_reason=proposal.unresolved_reason,
        rationale=proposal.rationale,
        alternatives=[
            {"source_fields": list(fields), "reason": reason}
            for fields, reason in proposal.alternatives
        ],
        certainty=proposal.certainty.value,
        validation_status=validation.status.value,
        validation_reasons=reasons_json(validation),
        outputs_preview=list(validation.outputs_preview),
        confidence=score,
        review_status=review_status.value,
        review_reasons=list(review_reasons),
    )


def save_run(session: Session, result: MappingRunResult, temperature: float) -> MappingRun:
    run = MappingRun(
        source_version_id=result.source_version_id,
        target_version_id=result.target_version_id,
        source_entity=result.source_entity,
        target_entity=result.target_entity,
        mode=result.mode,
        provider=result.provider,
        model=result.model,
        prompt_version=PROMPT_VERSION,
        confidence_version=CONFIDENCE_VERSION,
        temperature=temperature,
        requirement=result.requirement,
        summary=result.summary,
        run_reasons=[{"code": r.code.value, "detail": r.detail} for r in result.run_reasons],
    )
    session.add(run)
    session.flush()
    for position, item in enumerate(result.items):
        _save_item(session, run, position, item)
    session.flush()
    return run


def _save_item(session: Session, run: MappingRun, position: int, item: MappingItem) -> None:
    mapping = Mapping(mapping_run_id=run.id, position=position, target_field=item.target_field)
    session.add(mapping)
    session.flush()
    session.add(
        _version_row(
            mapping.id,
            1,
            "system",
            item.proposal,
            item.validation,
            item.confidence,
            item.review_status,
            item.review_reasons,
        )
    )
    for attempt, call in enumerate(item.attempts, start=1):
        meta = call.metadata
        session.add(
            LLMCall(
                mapping_run_id=run.id,
                target_field=item.target_field,
                attempt=attempt,
                provider=meta.provider,
                model=meta.model,
                prompt_hash=meta.prompt_hash,
                input_tokens=meta.input_tokens,
                output_tokens=meta.output_tokens,
                reasoning_tokens=meta.reasoning_tokens,
                latency_ms=meta.latency_ms,
                outcome=meta.outcome.value,
                source=meta.source,
                http_attempts=meta.http_attempts,
                error=meta.error,
            )
        )


def latest_version(session: Session, mapping_id: int) -> MappingVersion:
    version = session.scalar(
        select(MappingVersion)
        .where(MappingVersion.mapping_id == mapping_id)
        .order_by(MappingVersion.version.desc())
        .limit(1)
    )
    if version is None:
        raise LookupError(f"mapping {mapping_id} has no versions")
    return version


def proposal_from_version(version: MappingVersion) -> Proposal:
    raw: dict[str, Any] | None = version.transformation
    return Proposal(
        target_field=version.mapping.target_field,
        mapping_type=MappingType(version.mapping_type),
        source_fields=tuple(version.source_fields),
        transformation=Transformation.model_validate(raw) if raw else None,
        unresolved_reason=version.unresolved_reason,
        rationale=version.rationale,
        alternatives=tuple((tuple(a["source_fields"]), a["reason"]) for a in version.alternatives),
        certainty=Certainty(version.certainty),
    )


def validation_from_version(version: MappingVersion) -> ValidationResult:
    return ValidationResult(
        status=Status(version.validation_status),
        reasons=tuple(Reason(Code(r["code"]), r["detail"]) for r in version.validation_reasons),
        outputs_preview=tuple(version.outputs_preview),
    )


def append_version(
    session: Session,
    mapping: Mapping,
    author: str,
    proposal: Proposal,
    validation: ValidationResult,
    score: float | None,
    review_status: ReviewStatus,
    review_reasons: tuple[str, ...],
) -> MappingVersion:
    """Add the next version of a mapping; earlier versions are never touched."""
    current = latest_version(session, mapping.id)
    row = _version_row(
        mapping.id,
        current.version + 1,
        author,
        proposal,
        validation,
        score,
        review_status,
        review_reasons,
    )
    session.add(row)
    session.flush()
    return row
