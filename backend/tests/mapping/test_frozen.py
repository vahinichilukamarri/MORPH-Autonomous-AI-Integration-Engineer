"""Prompts v1 and the confidence formula v1 are frozen before the first real evaluation.

Changing either after real runs exist means introducing prompt v2 / confidence-v2 and reporting
it as a separate, labelled run in docs/mapping-eval.md. Never retune on the same fields and
present only the better result. If this test fails, add a new version instead of editing v1.
"""

import hashlib

from app.mapping import confidence
from app.mapping.prompts import PROMPT_DIR, PROMPT_VERSION

FROZEN_PROMPT_HASHES = {
    "system.md": "2e222fee1d4aa8a5cc339ca2620d75ea4915d63210a19dd5dff3d852edcc53f3",
    "user_full_schema.md": "9c80688fbc17070ee30947f7eeadf6d994cbd06084c1fe3b75b205312203e275",
    "user_rag.md": "f9ae30b4fbb6a9f67752b7150236a91be34e95b9271343e1af159b18bbb3d318",
}


def test_prompt_templates_v1_are_unchanged() -> None:
    assert PROMPT_VERSION == "v1"
    for name, expected in FROZEN_PROMPT_HASHES.items():
        data = (PROMPT_DIR / name).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == expected, (
            f"{name} changed: introduce prompts/v2 and report it as a separate labelled run"
        )
    assert {p.name for p in PROMPT_DIR.iterdir()} == set(FROZEN_PROMPT_HASHES)


def test_confidence_formula_v1_constants_are_unchanged() -> None:
    assert confidence.CONFIDENCE_VERSION == "confidence-v1"
    assert (
        confidence.WEIGHT_VALIDATION,
        confidence.WEIGHT_RETRIEVAL,
        confidence.WEIGHT_CERTAINTY,
    ) == (0.45, 0.30, 0.25)
    assert (confidence.AMBIGUITY_FACTOR, confidence.THRESHOLD) == (0.7, 0.70)
    assert confidence.NO_SOURCE_RETRIEVAL_SCORE == 0.5
    assert confidence.RANK_SCORE == {1: 1.0, 2: 0.7, 3: 0.5, 4: 0.3, 5: 0.3}
    assert {k.value: v for k, v in confidence.CERTAINTY_SCORE.items()} == {
        "HIGH": 1.0,
        "MEDIUM": 0.6,
        "LOW": 0.2,
    }
    assert {k.value: v for k, v in confidence.VALIDATION_SCORE.items()} == {
        "PASS": 1.0,
        "WARN": 0.5,
        "FAIL": 0.0,
    }
