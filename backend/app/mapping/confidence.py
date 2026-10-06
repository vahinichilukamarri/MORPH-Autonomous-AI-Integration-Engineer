"""Confidence and review decisions: a documented, deterministic function (formula v1).

    confidence = (0.45 * V + 0.30 * R + 0.25 * C) * (0.7 if ambiguous else 1.0)

* V, validation:  PASS 1.0, WARN 0.5, FAIL 0.0
* R, retrieval:   mean over the proposal's source fields of rank 1 -> 1.0, 2 -> 0.7, 3 -> 0.5,
                  4 or 5 -> 0.3, not retrieved -> 0.0; 0.5 when there is no source field
* C, certainty:   the model's label, HIGH 1.0, MEDIUM 0.6, LOW 0.2 (a label, not a probability)

It is *not* the model's own confidence: V and R come from code, and the label is one input of
three. A mapping is NEEDS_REVIEW when it is UNRESOLVED, FAILs validation, is ambiguous, carries
a review-forcing warning, or scores below THRESHOLD.

These constants are frozen for the first real evaluation. Changing any of them means introducing
formula v2 and reporting it as a separate, labelled run.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum

from app.mapping.proposal import Certainty, MappingType
from app.mapping.validate import Code, Status, ValidationResult

CONFIDENCE_VERSION = "confidence-v1"
WEIGHT_VALIDATION = 0.45
WEIGHT_RETRIEVAL = 0.30
WEIGHT_CERTAINTY = 0.25
AMBIGUITY_FACTOR = 0.7
THRESHOLD = 0.70
NO_SOURCE_RETRIEVAL_SCORE = 0.5

VALIDATION_SCORE = {Status.PASS: 1.0, Status.WARN: 0.5, Status.FAIL: 0.0}
CERTAINTY_SCORE = {Certainty.HIGH: 1.0, Certainty.MEDIUM: 0.6, Certainty.LOW: 0.2}
RANK_SCORE = {1: 1.0, 2: 0.7, 3: 0.5, 4: 0.3, 5: 0.3}
REVIEW_FORCING_CODES = frozenset({Code.NULLABLE_TO_REQUIRED})


class ReviewStatus(StrEnum):
    AUTO_ACCEPTED = "AUTO_ACCEPTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    OVERRIDDEN = "OVERRIDDEN"


def retrieval_score(ranks: Mapping[str, int | None], source_fields: Sequence[str]) -> float:
    if not source_fields:
        return NO_SOURCE_RETRIEVAL_SCORE
    scores = [RANK_SCORE.get(ranks.get(name) or 0, 0.0) for name in source_fields]
    return sum(scores) / len(scores)


def confidence(
    validation: ValidationResult,
    ranks: Mapping[str, int | None],
    source_fields: Sequence[str],
    certainty: Certainty,
) -> float:
    raw = (
        WEIGHT_VALIDATION * VALIDATION_SCORE[validation.status]
        + WEIGHT_RETRIEVAL * retrieval_score(ranks, source_fields)
        + WEIGHT_CERTAINTY * CERTAINTY_SCORE[certainty]
    )
    return round(raw * (AMBIGUITY_FACTOR if validation.ambiguous else 1.0), 4)


def review_decision(
    mapping_type: MappingType, validation: ValidationResult, score: float | None
) -> tuple[ReviewStatus, tuple[str, ...]]:
    """AUTO_ACCEPTED or NEEDS_REVIEW, with every reason that forced a review."""
    reasons: list[str] = []
    if mapping_type is MappingType.UNRESOLVED:
        reasons.append("unresolved")
    if validation.status is Status.FAIL:
        reasons.append("validation_failed")
    if validation.ambiguous:
        reasons.append("ambiguous")
    reasons.extend(
        f"warning:{r.code.value}" for r in validation.reasons if r.code in REVIEW_FORCING_CODES
    )
    if score is not None and score < THRESHOLD:
        reasons.append(f"low_confidence<{THRESHOLD}")
    status = ReviewStatus.NEEDS_REVIEW if reasons else ReviewStatus.AUTO_ACCEPTED
    return status, tuple(reasons)
