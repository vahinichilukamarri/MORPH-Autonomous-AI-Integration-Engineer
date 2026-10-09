"""A positive and a negative control for every policy rule and every floor rule.

A *positive* control is a case where the rule fires; a *negative* control is a near miss where it
must not. ``test_controls.py`` fails if a rule id has no entry here, or if either control does not
behave.
Each control returns True when it behaved as intended. All are pure (no database, no model).
"""

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.policy.evaluate import BUDGET_RULE, evaluate
from app.policy.floor import (
    looks_like_url,
    oversized,
    path_escapes,
    policy_floor_problems,
    withhold_test_data,
)
from app.policy.models import (
    ApprovalCheck,
    DataClass,
    Decision,
    Environment,
    PolicyFile,
    Reason,
    Role,
)
from app.policy.redact import Redactor
from app.policy.toolspec import TOOL_SPECS, forbidden_names
from tests.policy.helpers import active, ctx


@dataclass(frozen=True)
class Control:
    positive: Callable[[], bool]
    negative: Callable[[], bool]


def fires(rule: str, **kwargs: Any) -> bool:
    return rule in evaluate(active(), ctx(**kwargs)).rule_ids


def silent(rule: str, **kwargs: Any) -> bool:
    return rule not in evaluate(active(), ctx(**kwargs)).rule_ids


def verdict(decision: Decision, reason: Reason, **kwargs: Any) -> bool:
    record = evaluate(active(), ctx(**kwargs))
    return record.decision is decision and record.reason is reason


def _escape_control() -> tuple[bool, bool]:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        (root / "crm.json").write_text("{}", encoding="utf-8")
        return path_escapes(root, "../x.json"), path_escapes(root, "crm.json")


def _redaction_control() -> tuple[bool, bool]:
    secret = "supersecretvalue-0123456789"
    hit = Redactor([secret]).text(f"key={secret}")
    miss = Redactor([secret]).text("a perfectly ordinary sentence about field names")
    return hit.changed and secret not in hit.text, not miss.changed


def _withhold_control() -> tuple[bool, bool]:
    files = [{"path": "integration/sync.py"}, {"path": "tests_generated/cases.py"}]
    _, withheld = withhold_test_data(files, DataClass.INTERNAL)
    _, kept_all = withhold_test_data(files, DataClass.SYNTHETIC)
    return withheld == ["tests_generated/cases.py"], kept_all == []


def _ceiling_control() -> tuple[bool, bool]:
    text = active().path.read_text(encoding="utf-8")
    too_big = parse_policy_unchecked(text.replace("max_model_calls: 12", "max_model_calls: 5000"))
    return bool(policy_floor_problems(too_big)), policy_floor_problems(active().file) == []


def parse_policy_unchecked(text: str) -> PolicyFile:
    """Parse without the floor check, to build a policy that the floor would refuse."""
    return PolicyFile.model_validate(yaml.safe_load(text))


CONTROLS: dict[str, Control] = {
    # ---- policy rules
    "P01-read-any-role": Control(
        lambda: fires("P01-read-any-role", tool="get_system", role=Role.READER),
        lambda: silent("P01-read-any-role", tool="ingest_contract"),
    ),
    "P02-operator-ingest": Control(
        lambda: fires("P02-operator-ingest", tool="ingest_contract"),
        lambda: silent("P02-operator-ingest", tool="ingest_contract", role=Role.READER),
    ),
    "P03-operator-generate-without-model": Control(
        lambda: fires(
            "P03-operator-generate-without-model", tool="generate_integration",
            model_involved=False, environment=Environment.MOCK,
        ),
        lambda: silent(
            "P03-operator-generate-without-model", tool="generate_integration",
            model_involved=True, environment=Environment.MOCK, data_class=DataClass.SYNTHETIC,
        ),
    ),
    "P04-operator-run-tests": Control(
        lambda: fires("P04-operator-run-tests", tool="run_generated_tests",
                      environment=Environment.MOCK),
        lambda: silent("P04-operator-run-tests", tool="run_generated_tests",
                       role=Role.READER, environment=Environment.MOCK),
    ),
    "P05-operator-model-synthetic": Control(
        lambda: fires("P05-operator-model-synthetic", tool="propose_mapping",
                      environment=Environment.MOCK, data_class=DataClass.SYNTHETIC),
        lambda: silent("P05-operator-model-synthetic", tool="propose_mapping",
                       environment=Environment.MOCK, data_class=DataClass.INTERNAL),
    ),
    "P06-model-other-data-needs-approval": Control(
        lambda: verdict(
            Decision.NEEDS_APPROVAL, Reason.MODEL_DATA_NOT_SYNTHETIC, tool="propose_mapping",
            environment=Environment.MOCK, data_class=DataClass.UNCLASSIFIED,
        ),
        lambda: silent("P06-model-other-data-needs-approval", tool="propose_mapping",
                       environment=Environment.MOCK, data_class=DataClass.SYNTHETIC),
    ),
    "P07-reader-no-side-effects": Control(
        lambda: verdict(Decision.DENY, Reason.ROLE_NOT_PERMITTED, tool="ingest_contract",
                        role=Role.READER),
        lambda: silent("P07-reader-no-side-effects", tool="ingest_contract"),
    ),
    # ---- the budget
    BUDGET_RULE: Control(
        lambda: verdict(
            Decision.DENY, Reason.BUDGET_EXCEEDED, tool="repair_integration",
            environment=Environment.MOCK, data_class=DataClass.SYNTHETIC, model_calls_used=12,
        ),
        lambda: verdict(
            Decision.ALLOW, Reason.ALLOWED_BY_RULE, tool="repair_integration",
            environment=Environment.MOCK, data_class=DataClass.SYNTHETIC, model_calls_used=11,
        ),
    ),
    # ---- the floor
    "F01-exec-target-mock": Control(
        lambda: fires("F01-exec-target-mock", tool="run_generated_tests",
                      environment=Environment.PRODUCTION),
        lambda: silent("F01-exec-target-mock", tool="run_generated_tests",
                       environment=Environment.MOCK),
    ),
    "F02-no-url": Control(
        lambda: looks_like_url("http://169.254.169.254/spec.json")
        and verdict(Decision.DENY, Reason.URL_DENIED, tool="ingest_contract", url=True),
        lambda: not looks_like_url("specs/crm.v1.json")
        and silent("F02-no-url", tool="ingest_contract"),
    ),
    "F03-path-inside-root": Control(
        lambda: _escape_control()[0]
        and verdict(Decision.DENY, Reason.PATH_ESCAPE, tool="ingest_contract", escapes=True),
        lambda: not _escape_control()[1] and silent("F03-path-inside-root", tool="ingest_contract"),
    ),
    "F04-known-tools-only": Control(
        lambda: verdict(Decision.DENY, Reason.UNKNOWN_TOOL, tool="run_shell", known=False),
        lambda: silent("F04-known-tools-only", tool="get_system"),
    ),
    "F05-no-forbidden-capability": Control(
        lambda: forbidden_names(["approve_mapping", "set_policy_attributes"])
        == ["approve_mapping", "set_policy_attributes"],
        lambda: forbidden_names([t.name for t in TOOL_SPECS]) == [],
    ),
    "F06-result-size-cap": Control(
        lambda: oversized({"x": "a" * 5_000}, 1_000),
        lambda: not oversized({"x": "a" * 50}, 1_000),
    ),
    "F07-secret-redaction": Control(
        lambda: _redaction_control()[0], lambda: _redaction_control()[1]
    ),
    "F08-test-data-withheld": Control(
        lambda: _withhold_control()[0], lambda: _withhold_control()[1]
    ),
    "F09-approval-never-overrides-deny": Control(
        lambda: verdict(
            Decision.DENY, Reason.EXEC_TARGET_NOT_MOCK, tool="run_generated_tests",
            environment=Environment.PRODUCTION, approval=ApprovalCheck.VALID,
        )
        and fires("F09-approval-never-overrides-deny", tool="run_generated_tests",
                  environment=Environment.PRODUCTION, approval=ApprovalCheck.VALID),
        lambda: verdict(
            Decision.ALLOW, Reason.APPROVAL_VALID, tool="propose_mapping",
            environment=Environment.MOCK, data_class=DataClass.INTERNAL,
            approval=ApprovalCheck.VALID,
        )
        and silent("F09-approval-never-overrides-deny", tool="propose_mapping",
                   environment=Environment.MOCK, data_class=DataClass.INTERNAL,
                   approval=ApprovalCheck.VALID),
    ),
    "F10-limit-ceilings": Control(lambda: _ceiling_control()[0], lambda: _ceiling_control()[1]),
}  # fmt: skip
