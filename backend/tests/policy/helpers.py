"""Builders for policy tests: a context with sensible defaults and the active policy."""

from functools import lru_cache
from typing import Any

from app.policy.loader import LoadedPolicy, load_active
from app.policy.models import (
    ApprovalCheck,
    ArgumentFacts,
    CallContext,
    DataClass,
    Effect,
    Environment,
    Role,
)
from app.policy.toolspec import BY_NAME


@lru_cache
def active() -> LoadedPolicy:
    return load_active()


def ctx(
    tool: str = "get_system",
    *,
    role: Role = Role.OPERATOR,
    environment: Environment | None = None,
    data_class: DataClass | None = None,
    model_involved: bool | None = None,
    approval: ApprovalCheck = ApprovalCheck.NONE_SUPPLIED,
    url: bool = False,
    escapes: bool = False,
    known: bool = True,
    effects: tuple[Effect, ...] | None = None,
    model_calls_used: int = 0,
    sandbox_runs_used: int = 0,
) -> CallContext:
    spec = BY_NAME.get(tool)
    used_effects = effects if effects is not None else tuple(sorted(spec.effects) if spec else ())
    involved = model_involved
    if involved is None:
        involved = tool in ("propose_mapping", "repair_integration")
    kwargs: dict[str, Any] = {
        "tool": tool,
        "tool_known": known,
        "effects": used_effects,
        "role": role,
        "environment": environment,
        "data_class": data_class,
        "model_involved": involved,
        "facts": ArgumentFacts(url_in_arguments=url, path_escapes_root=escapes),
        "approval": approval,
        "model_calls_used": model_calls_used,
        "sandbox_runs_used": sandbox_runs_used,
    }
    return CallContext(**kwargs)
