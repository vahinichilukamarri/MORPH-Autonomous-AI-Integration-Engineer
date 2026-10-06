"""Prompts v1 and the confidence formula v1 are frozen before the first real evaluation.

Amended once, deliberately, before any real run existed: the operator requirement section and
stratified sample selection were added to v1 (see "Prompt v1 amendment" in docs/mapping.md).
That was completion of the first version, not retuning on results.

Changing either after real runs exist means introducing prompt v2 / confidence-v2 and reporting
it as a separate, labelled run in docs/mapping-eval.md. Never retune on the same fields and
present only the better result. If this test fails, add a new version instead of editing v1.
"""

import hashlib

from app.mapping import confidence
from app.mapping.prompts import PROMPT_DIR, PROMPT_VERSION

FROZEN_PROMPT_HASHES = {
    "system.md": "b5eefee4f1b1afe2f57f1d7056f1d27a9b45dc33892973b51cfbdacb7364e96e",
    "user_full_schema.md": "fc71b6d8b9cd8a30dbc5354205df281a0541f20cf243bc0b394ec9c123d938e8",
    "user_rag.md": "8e3076a626651593a6703a8917ecf044080bf63b644cfb082a04ea24d4db9ea7",
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


def test_sample_selection_and_the_rendered_s1_prompt_are_frozen() -> None:
    """The code that fills the templates is part of the frozen prompt, not just the files."""
    from app.mapping.prompts import build_user_prompt, pick_samples
    from tests.mapping.helpers import entity_fields, samples

    source = entity_fields("crm.v1.json", "Customer")
    target = entity_fields("support.v1.json", "User")
    records = samples("crm_customer.v1.json")
    picked = pick_samples(records, list(source.values()))
    assert [r["customer_id"] for r in picked] == ["C-1654", "C-1759", "C-1837", "C-1001", "C-0042"]
    requirement = (
        "Whenever a customer exists in the CRM, the support desk must have a matching user so the "
        "support team can handle their tickets. Customers on the enterprise segment are entitled "
        "to priority support; all other customers receive standard support."
    )
    prompt = build_user_prompt(
        "full_schema", target["tier"], list(source.values()), [], records, requirement
    )
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == (
        "b8926b130eb75dbf448686a7205528d8dc5b83b4d526778d8bf969fda88d6edb"
    ), "the rendered prompt changed: introduce prompt v2 and report it as a separate run"
