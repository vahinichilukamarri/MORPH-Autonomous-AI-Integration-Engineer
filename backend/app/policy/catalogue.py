"""The tool catalogue for the UI and the docs: effects, argument schema and what the policy says.

The outcome matrix is computed by running the evaluator on typical contexts (a mock target, each
role, each data class, no approval). It shows the *class* of outcome a caller can expect; the real
decision also depends on budgets, approvals and the arguments."""

from typing import Any

from app.policy.evaluate import evaluate
from app.policy.loader import LoadedPolicy
from app.policy.models import (
    ArgumentFacts,
    CallContext,
    DataClass,
    Environment,
    Role,
)
from app.policy.toolspec import TOOL_SPECS, ToolSpec


def _variants(spec: ToolSpec) -> list[tuple[str, bool]]:
    if spec.name == "generate_integration":
        return [("condition D", False), ("condition L1 or L2", True)]
    return [("", spec.name in ("propose_mapping", "repair_integration"))]


def outcome_matrix(policy: LoadedPolicy, spec: ToolSpec) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    for label, model in _variants(spec):
        for role in Role:
            for data_class in DataClass:
                ctx = CallContext(
                    tool=spec.name,
                    effects=tuple(sorted(spec.effects)),
                    role=role,
                    environment=Environment.MOCK,
                    data_class=data_class,
                    model_involved=model,
                    facts=ArgumentFacts(),
                )
                key = f"{role.value} / {data_class.value}" + (f" / {label}" if label else "")
                outcomes[key] = evaluate(policy, ctx).decision.value
    return outcomes


def catalogue(policy: LoadedPolicy) -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "effects": sorted(e.value for e in spec.effects),
            "side_effect": spec.side_effect,
            "input_schema": spec.args_model.model_json_schema(),
            "outcomes_on_a_mock_target": outcome_matrix(policy, spec),
        }
        for spec in TOOL_SPECS
    ]
