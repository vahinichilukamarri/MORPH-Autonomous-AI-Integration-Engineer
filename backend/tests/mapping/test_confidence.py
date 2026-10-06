import pytest

from app.mapping import confidence as conf
from app.mapping.confidence import ReviewStatus, confidence, retrieval_score, review_decision
from app.mapping.proposal import Certainty, MappingType
from app.mapping.validate import Code, Reason, Status, ValidationResult


def result(status: Status, *codes: Code) -> ValidationResult:
    return ValidationResult(status, tuple(Reason(c, "x") for c in codes))


def test_constants_are_the_documented_ones() -> None:
    assert conf.CONFIDENCE_VERSION == "confidence-v1"
    assert (conf.WEIGHT_VALIDATION, conf.WEIGHT_RETRIEVAL, conf.WEIGHT_CERTAINTY) == (
        0.45,
        0.30,
        0.25,
    )
    assert (conf.AMBIGUITY_FACTOR, conf.THRESHOLD) == (0.7, 0.70)
    assert conf.RANK_SCORE == {1: 1.0, 2: 0.7, 3: 0.5, 4: 0.3, 5: 0.3}


def test_retrieval_score_averages_over_sources() -> None:
    assert retrieval_score({"a": 1}, ["a"]) == 1.0
    assert retrieval_score({"a": 2}, ["a"]) == 0.7
    assert retrieval_score({"a": 3}, ["a"]) == 0.5
    assert retrieval_score({"a": 5}, ["a"]) == 0.3
    assert retrieval_score({"a": None}, ["a"]) == 0.0
    assert retrieval_score({}, ["a"]) == 0.0
    assert retrieval_score({"a": 1, "b": 3}, ["a", "b"]) == pytest.approx(0.75)
    assert retrieval_score({}, []) == 0.5, "no source field: neutral"


def test_confidence_is_the_documented_weighted_sum() -> None:
    best = confidence(result(Status.PASS), {"a": 1}, ["a"], Certainty.HIGH)
    assert best == 1.0
    mid = confidence(result(Status.WARN), {"a": 2}, ["a"], Certainty.MEDIUM)
    assert mid == pytest.approx(0.45 * 0.5 + 0.30 * 0.7 + 0.25 * 0.6, abs=1e-4)
    worst = confidence(result(Status.FAIL), {"a": None}, ["a"], Certainty.LOW)
    assert worst == pytest.approx(0.25 * 0.2, abs=1e-4)


def test_ambiguity_scales_the_score_down() -> None:
    plain = confidence(result(Status.PASS), {"a": 1}, ["a"], Certainty.HIGH)
    ambiguous = confidence(
        result(Status.PASS, Code.AMBIGUOUS_ALTERNATIVES), {"a": 1}, ["a"], Certainty.HIGH
    )
    assert ambiguous == pytest.approx(plain * 0.7)


def test_the_certainty_label_alone_cannot_rescue_a_failed_mapping() -> None:
    score = confidence(result(Status.FAIL), {"a": None}, ["a"], Certainty.HIGH)
    assert score < conf.THRESHOLD
    status, reasons = review_decision(MappingType.DIRECT, result(Status.FAIL), score)
    assert status is ReviewStatus.NEEDS_REVIEW and "validation_failed" in reasons


def test_clean_high_confidence_mapping_is_auto_accepted() -> None:
    status, reasons = review_decision(MappingType.DIRECT, result(Status.PASS), 0.95)
    assert (status, reasons) == (ReviewStatus.AUTO_ACCEPTED, ())


def test_threshold_boundary() -> None:
    assert review_decision(MappingType.DIRECT, result(Status.PASS), 0.70)[0] is (
        ReviewStatus.AUTO_ACCEPTED
    )
    status, reasons = review_decision(MappingType.DIRECT, result(Status.PASS), 0.6999)
    assert status is ReviewStatus.NEEDS_REVIEW and reasons == ("low_confidence<0.7",)


def test_unresolved_is_always_reviewed() -> None:
    status, reasons = review_decision(
        MappingType.UNRESOLVED, result(Status.WARN, Code.UNRESOLVED), None
    )
    assert status is ReviewStatus.NEEDS_REVIEW and "unresolved" in reasons


def test_ambiguous_mapping_is_reviewed_even_with_a_high_score() -> None:
    status, reasons = review_decision(
        MappingType.DIRECT, result(Status.PASS, Code.AMBIGUOUS_RETRIEVAL), 0.99
    )
    assert status is ReviewStatus.NEEDS_REVIEW and "ambiguous" in reasons


def test_nullable_to_required_forces_review_even_when_confident() -> None:
    """The S3 customer_id rule: the warning must reach a human."""
    validation = result(Status.WARN, Code.NULLABLE_TO_REQUIRED)
    score = confidence(validation, {"externalRef": 1}, ["externalRef"], Certainty.HIGH)
    assert score >= conf.THRESHOLD
    status, reasons = review_decision(MappingType.DIRECT, validation, score)
    assert status is ReviewStatus.NEEDS_REVIEW
    assert reasons == ("warning:NULLABLE_TO_REQUIRED",)


def test_other_warnings_lower_the_score_but_do_not_force_review_alone() -> None:
    validation = result(Status.WARN, Code.INFORMATION_LOSS_ENUM)
    score = confidence(validation, {"segment": 1}, ["segment"], Certainty.HIGH)
    assert score == pytest.approx(0.45 * 0.5 + 0.30 + 0.25)
    assert review_decision(MappingType.DERIVED, validation, score)[0] is ReviewStatus.AUTO_ACCEPTED
