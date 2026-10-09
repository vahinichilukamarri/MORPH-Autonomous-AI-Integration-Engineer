"""The pre-registered corpora, run case by case. The counts are those of the pre-registration."""

from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import Engine

from tests.policy.controls import CONTROLS
from tests.policy.evalkit import corpus, injection, redaction, tamper
from tests.policy.evalkit.interpreter import Case, run_case

GATEWAY = {name: corpus.load(name) for name in corpus.EXPECTED_COUNTS}
ALL_CASES = [case for cases in GATEWAY.values() for case in cases]


@pytest.mark.parametrize("name", sorted(GATEWAY))
def test_the_counts_and_groups_are_the_pre_registered_ones(name: str) -> None:
    cases = GATEWAY[name]
    assert len(cases) == corpus.EXPECTED_COUNTS[name]
    assert dict(Counter(c.group for c in cases)) == corpus.EXPECTED_GROUPS[name]
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids)), "case ids are unique"


def test_the_total_of_gateway_cases() -> None:
    assert len(ALL_CASES) == 65 + 31 + 15


@pytest.mark.parametrize("case", ALL_CASES, ids=[c.id for c in ALL_CASES])
def test_a_gateway_case(case: Case, test_engine: Engine, tmp_path: Path) -> None:
    outcome = run_case(test_engine, tmp_path, case)
    if outcome.skipped:
        pytest.skip(outcome.observed)
    assert outcome.passed, f"{case.description}: {outcome.problems} (observed {outcome.observed})"


PAYLOADS = injection.load_payloads()
INJECTION = [(p, c) for p in PAYLOADS for c in injection.CARRIERS]


def test_the_injection_corpus_is_8_by_5_with_18_attempts_per_carrier() -> None:
    assert len(PAYLOADS) == corpus.INJECTION_CLASSES
    assert len(injection.CARRIERS) == corpus.INJECTION_CARRIERS
    assert len(INJECTION) == 40
    assert sum(len(p.attempts) for p in PAYLOADS) == corpus.INJECTION_ATTEMPTS_PER_CARRIER
    assert sum(len(p.attempts) for p in PAYLOADS) * corpus.INJECTION_CARRIERS == 90


@pytest.mark.parametrize(
    ("payload", "carrier"), INJECTION, ids=[f"{p.id}-{c}" for p, c in INJECTION]
)
def test_an_injection_case(
    payload: injection.Payload, carrier: str, test_engine: Engine, tmp_path: Path
) -> None:
    result = injection.run_case(test_engine, tmp_path, payload, carrier)
    assert result.reached, "the payload never reached the agent: the case would be vacuous"
    assert result.invariant, "a legitimate call was decided differently with the payload present"
    assert result.attempts == len(payload.attempts)
    assert result.executed == 0, "a forbidden action ran"
    assert not result.leaked, "a planted secret reached a result or an audit row"


@pytest.mark.parametrize("case", tamper.CASES, ids=[c.id for c in tamper.CASES])
def test_a_tamper_case(case: tamper.TamperCase, test_engine: Engine) -> None:
    result = tamper.run_case(test_engine, case)
    assert result.as_documented, result
    assert len([c for c in tamper.CASES if c.detectable]) == corpus.TAMPER_DETECTABLE
    assert len([c for c in tamper.CASES if not c.detectable]) == corpus.TAMPER_LIMITS


def test_the_redaction_corpus(test_engine: Engine, tmp_path: Path) -> None:
    results = redaction.run_all(test_engine, tmp_path)
    assert len(results["sentinel"]) == corpus.REDACTION_SENTINEL
    assert len(results["pattern"]) == corpus.REDACTION_PATTERNS
    assert len(results["near_miss"]) == corpus.REDACTION_NEAR_MISSES
    failed = [r for group in results.values() for r in group if not r.passed]
    assert failed == [], [(r.id, r.detail) for r in failed]


def test_the_rule_controls_are_36() -> None:
    assert len(CONTROLS) * 2 == corpus.CONTROLS
