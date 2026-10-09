"""The policy file's model, the request context and the decision record. All strict."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

POLICY_VERSION_PATTERN = r"^v[0-9]+$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Effect(StrEnum):
    READ = "READ"
    WRITE_LOCAL = "WRITE_LOCAL"
    MODEL_CALL = "MODEL_CALL"
    EXECUTE_SANDBOX = "EXECUTE_SANDBOX"


SIDE_EFFECTS = frozenset({Effect.WRITE_LOCAL, Effect.MODEL_CALL, Effect.EXECUTE_SANDBOX})


class Role(StrEnum):
    READER = "reader"
    OPERATOR = "operator"


class Environment(StrEnum):
    MOCK = "mock"
    STAGING = "staging"
    PRODUCTION = "production"
    UNKNOWN = "unknown"  # no attributes recorded: treated as production


class DataClass(StrEnum):
    SYNTHETIC = "synthetic"
    INTERNAL = "internal"
    RESTRICTED = "restricted"
    UNCLASSIFIED = "unclassified"  # no attributes recorded: treated as restricted


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"


class RuleDecision(StrEnum):
    """What a policy rule may say; a rule never says 'allow with an approval'."""

    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


class Reason(StrEnum):
    READ_ALLOWED = "READ_ALLOWED"
    ALLOWED_BY_RULE = "ALLOWED_BY_RULE"
    ROLE_NOT_PERMITTED = "ROLE_NOT_PERMITTED"
    MODEL_DATA_NOT_SYNTHETIC = "MODEL_DATA_NOT_SYNTHETIC"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_VALID = "APPROVAL_VALID"
    APPROVAL_INVALID = "APPROVAL_INVALID"
    DEFAULT_DENY = "DEFAULT_DENY"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    EXEC_TARGET_NOT_MOCK = "EXEC_TARGET_NOT_MOCK"
    URL_DENIED = "URL_DENIED"
    PATH_ESCAPE = "PATH_ESCAPE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    FORBIDDEN_CAPABILITY = "FORBIDDEN_CAPABILITY"
    RESULT_TOO_LARGE = "RESULT_TOO_LARGE"


class Match(Strict):
    """Every field that is set must match; a field left out matches anything."""

    tools: tuple[str, ...] | None = None
    effects: tuple[Effect, ...] | None = None  # matches if the tool has any of them
    roles: tuple[Role, ...] | None = None
    environments: tuple[Environment, ...] | None = None
    data_classes: tuple[DataClass, ...] | None = None
    model_involved: bool | None = None


class Rule(Strict):
    id: str = Field(pattern=r"^[A-Z][0-9]{2}-[a-z0-9-]+$")
    decision: RuleDecision
    reason: Reason
    description: str = Field(min_length=1, max_length=300)
    match: Match


class SessionLimits(Strict):
    max_model_calls: int = Field(ge=0)
    max_sandbox_runs: int = Field(ge=0)
    max_result_bytes: int = Field(ge=1_000)


class PolicyFile(Strict):
    version: str = Field(pattern=POLICY_VERSION_PATTERN)
    description: str = Field(min_length=1, max_length=500)
    session_limits: SessionLimits
    rules: tuple[Rule, ...]
    # A version that loosens relative to the previous one must say why; empty means it does not.
    loosens: tuple[str, ...] = ()

    @field_validator("rules")
    @classmethod
    def _unique_ids(cls, rules: tuple[Rule, ...]) -> tuple[Rule, ...]:
        ids = [r.id for r in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        return rules


class ApprovalCheck(StrEnum):
    """What the approvals module found for the approval id a caller supplied."""

    NONE_SUPPLIED = "NONE_SUPPLIED"
    VALID = "VALID"
    UNKNOWN = "UNKNOWN"
    NOT_DECIDED = "NOT_DECIDED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"
    REQUEST_MISMATCH = "REQUEST_MISMATCH"
    POLICY_CHANGED = "POLICY_CHANGED"


class ArgumentFacts(Strict):
    """Facts the gateway computes from the arguments before evaluation. No free text."""

    url_in_arguments: bool = False
    path_escapes_root: bool = False


class CallContext(Strict):
    """Everything the policy may look at. Derived by the server; contains no secrets."""

    tool: str
    tool_known: bool = True
    effects: tuple[Effect, ...]
    role: Role
    environment: Environment | None = None  # None: the tool involves no target system
    data_class: DataClass | None = None
    model_involved: bool = False
    facts: ArgumentFacts = ArgumentFacts()
    approval: ApprovalCheck = ApprovalCheck.NONE_SUPPLIED
    model_calls_used: int = Field(default=0, ge=0)
    sandbox_runs_used: int = Field(default=0, ge=0)

    @property
    def has_side_effect(self) -> bool:
        return bool(SIDE_EFFECTS & set(self.effects))


class DecisionRecord(Strict):
    decision: Decision
    reason: Reason
    rule_ids: tuple[str, ...]  # every rule or floor id that matched, floor first
    policy_version: str
    policy_hash: str
    inputs: dict[str, Any]

    def replay_key(self) -> tuple[str, str, tuple[str, ...]]:
        return self.decision.value, self.reason.value, self.rule_ids
