"""The evaluator: floor first, then the policy. Pure. deny > needs-approval > allow > default deny.

The inputs are a ``CallContext`` the server derived from its own state, so the decision can be
replayed from the recorded context and the recorded policy version.
"""

from app.policy.floor import floor_denials
from app.policy.loader import LoadedPolicy
from app.policy.models import (
    ApprovalCheck,
    CallContext,
    Decision,
    DecisionRecord,
    Effect,
    Match,
    Reason,
    Rule,
    RuleDecision,
)

BUDGET_RULE = "L01-session-limits"
APPROVAL_FLOOR_RULE = "F09-approval-never-overrides-deny"


def matches(match: Match, ctx: CallContext) -> bool:
    if match.tools is not None and ctx.tool not in match.tools:
        return False
    if match.effects is not None and not set(match.effects) & set(ctx.effects):
        return False
    if match.roles is not None and ctx.role not in match.roles:
        return False
    if match.environments is not None and ctx.environment not in match.environments:
        return False
    if match.data_classes is not None and ctx.data_class not in match.data_classes:
        return False
    return match.model_involved is None or match.model_involved == ctx.model_involved


def budget_exceeded(policy: LoadedPolicy, ctx: CallContext) -> bool:
    limits = policy.file.session_limits
    if ctx.model_involved and ctx.model_calls_used >= limits.max_model_calls:
        return True
    return (
        Effect.EXECUTE_SANDBOX in ctx.effects and ctx.sandbox_runs_used >= limits.max_sandbox_runs
    )


def _record(
    policy: LoadedPolicy, ctx: CallContext, decision: Decision, reason: Reason, ids: list[str]
) -> DecisionRecord:
    return DecisionRecord(
        decision=decision,
        reason=reason,
        rule_ids=tuple(ids),
        policy_version=policy.version,
        policy_hash=policy.hash,
        inputs=ctx.model_dump(mode="json"),
    )


def evaluate(policy: LoadedPolicy, ctx: CallContext) -> DecisionRecord:
    floor = floor_denials(ctx)
    if floor:
        floor_ids = [rule_id for rule_id, _ in floor]
        if ctx.approval is ApprovalCheck.VALID:
            floor_ids.append(APPROVAL_FLOOR_RULE)  # the approval was valid and still did not help
        return _record(policy, ctx, Decision.DENY, floor[0][1], floor_ids)

    ids: list[str] = []
    if budget_exceeded(policy, ctx):
        ids.append(BUDGET_RULE)
    matched: list[Rule] = [r for r in policy.file.rules if matches(r.match, ctx)]
    ids.extend(r.id for r in matched)

    if BUDGET_RULE in ids:
        return _record(policy, ctx, Decision.DENY, Reason.BUDGET_EXCEEDED, ids)
    denies = [r for r in matched if r.decision is RuleDecision.DENY]
    if denies:
        return _record(policy, ctx, Decision.DENY, denies[0].reason, ids)
    asks = [r for r in matched if r.decision is RuleDecision.NEEDS_APPROVAL]
    if asks:
        if ctx.approval is ApprovalCheck.VALID:
            return _record(policy, ctx, Decision.ALLOW, Reason.APPROVAL_VALID, ids)
        if ctx.approval is ApprovalCheck.NONE_SUPPLIED:
            return _record(policy, ctx, Decision.NEEDS_APPROVAL, asks[0].reason, ids)
        return _record(policy, ctx, Decision.DENY, Reason.APPROVAL_INVALID, ids)
    allows = [r for r in matched if r.decision is RuleDecision.ALLOW]
    if allows:
        return _record(policy, ctx, Decision.ALLOW, allows[0].reason, ids)
    return _record(policy, ctx, Decision.DENY, Reason.DEFAULT_DENY, ids)
