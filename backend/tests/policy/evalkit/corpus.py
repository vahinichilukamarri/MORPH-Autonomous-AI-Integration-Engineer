"""Loading the gateway corpora and the counts fixed in the pre-registration."""

from pathlib import Path
from typing import Any

import yaml

from tests.policy.evalkit.interpreter import Case

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"

# docs/plans/v0.6-m4-preregistration.md: the counts, fixed before any corpus was run
EXPECTED_COUNTS = {"must_deny": 65, "must_allow": 31, "must_need_approval": 15}
EXPECTED_GROUPS = {
    "must_deny": {
        "role scope": 5,
        "tool that must not exist": 12,
        "schema": 9,
        "ingest path": 9,
        "ingest URL": 8,
        "code on a non-mock target": 9,
        "budget": 4,
        "approval abuse": 9,
    },
    "must_allow": {
        "read tools": 16,
        "ingest": 2,
        "deterministic generation": 2,
        "tests in the sandbox": 2,
        "model on synthetic data": 6,
        "approved calls": 3,
    },
    "must_need_approval": {"model call on data that is not synthetic": 15},
}
INJECTION_CLASSES = 8
INJECTION_CARRIERS = 5
INJECTION_ATTEMPTS_PER_CARRIER = 18
TAMPER_DETECTABLE = 11
TAMPER_LIMITS = 1
REDACTION_SENTINEL = 22
REDACTION_PATTERNS = 12
REDACTION_NEAR_MISSES = 8
CONTROLS = 36


def load(name: str) -> list[Case]:
    raw: list[dict[str, Any]] = yaml.safe_load(
        (CORPUS_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    )
    return [
        Case(name, c["id"], c["group"], c["description"], tuple(c["steps"]), c["expect"])
        for c in raw
    ]
