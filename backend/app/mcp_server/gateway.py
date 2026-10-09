"""The gateway: the one path every tool call takes, in a fixed and explicit order.

    1. receive      record the call (arguments redacted)
    2. schema       the tool must exist and its arguments must fit the strict model
    3. floor+policy derive the context from our own state, ask the evaluator
    4. approval     needs-approval becomes a request for a human, or a valid approval is used up
    5. service      run the handler (metered: model calls and sandbox runs are charged one by one)
    6. redaction    secrets out, size capped, test data already withheld by the handler
    7. audit        what happened, with a digest of what was returned

The MCP server registers one handler that calls ``Gateway.call`` and nothing else. Middleware may
add to this; it is never the place where anything is enforced."""

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.mcp_server.adapters import HANDLERS, Deps, Runtime, ToolError
from app.mcp_server.context import involved_systems
from app.mcp_server.metering import BudgetExceeded, Meter
from app.policy import approvals, attributes
from app.policy.approvals import request_hash
from app.policy.audit import EventType
from app.policy.canonical import digest
from app.policy.clock import Clock, SystemClock
from app.policy.evaluate import evaluate
from app.policy.floor import inspect_arguments, oversized
from app.policy.models import (
    ApprovalCheck,
    ArgumentFacts,
    CallContext,
    Decision,
    Reason,
)
from app.policy.redact import Redactor
from app.policy.toolspec import BY_NAME, TOOL_SPECS, forbidden_names, model_involved

Status = Literal["ok", "denied", "needs_approval", "budget_exceeded", "error"]


@dataclass(frozen=True)
class ToolResponse:
    status: Status
    body: dict[str, Any]

    @property
    def is_error(self) -> bool:
        return self.status in ("denied", "budget_exceeded", "error")


@dataclass
class Gateway:
    deps: Deps
    engine: Engine
    clock: Clock = field(default_factory=SystemClock)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        names = {spec.name for spec in TOOL_SPECS}
        if set(HANDLERS) != names:
            raise RuntimeError(f"tools and handlers differ: {sorted(set(HANDLERS) ^ names)}")
        bad = forbidden_names(sorted(names))
        if bad:
            raise RuntimeError(f"forbidden capabilities cannot be tools: {bad}")
        self._redactor = Redactor(self.deps.secret_values)

    # ---- the public entry --------------------------------------------------------------------

    def call(self, tool: str, arguments: Mapping[str, Any] | None) -> ToolResponse:
        deps = self.deps
        call_id = uuid.uuid4().hex[:16]
        raw = dict(arguments or {})
        principal = f"agent:{deps.role.value}"
        policy = deps.policy

        def note(event: EventType, payload: Mapping[str, Any]) -> None:
            deps.audit.append(
                event, session_id=deps.session_id, principal=principal, tool=tool[:64],
                call_id=call_id, policy_hash=policy.hash,
                payload=self._redactor.value(dict(payload))[0],
            )  # fmt: skip

        # 1. receive
        note(EventType.CALL_RECEIVED, {"arguments": raw, "request_hash": request_hash(tool, raw)})

        # 2. schema
        spec = BY_NAME.get(tool)
        if spec is None:
            return self._decide_unknown(tool, call_id, note)
        try:
            args = spec.args_model.model_validate(raw)
        except ValidationError as error:
            errors = [{"where": ".".join(str(p) for p in e["loc"]), "problem": e["type"]}
                      for e in error.errors()]  # fmt: skip
            return self._deny(note, tool, call_id, Reason.SCHEMA_INVALID, [], {"errors": errors})

        # 3. floor and policy, on a context derived from our own state
        with Session(self.engine) as db:
            ids = involved_systems(db, args)
            environment, data_class = attributes.aggregate(
                attributes.get_attributes(db, sid) for sid in ids
            )
            facts = inspect_arguments(raw, spec_root=deps.spec_root)
            approval_id = getattr(args, "approval_id", None)
            approval = approvals.check(
                db, self.clock, approval_id, tool=tool, arguments=raw, policy_hash=policy.hash
            )
        ctx = CallContext(
            tool=tool,
            effects=tuple(sorted(spec.effects)),
            role=deps.role,
            environment=environment,
            data_class=data_class,
            model_involved=model_involved(tool, args),
            facts=facts,
            approval=approval,
            model_calls_used=deps.audit.count(deps.session_id, EventType.CHARGE, kind="model_call"),
            sandbox_runs_used=deps.audit.count(
                deps.session_id, EventType.CHARGE, kind="sandbox_run"
            ),
        )
        record = evaluate(policy, ctx)
        note(EventType.POLICY_DECISION, record.model_dump(mode="json"))

        # 4. approval
        if record.decision is Decision.DENY:
            detail: dict[str, Any] = {}
            if record.reason is Reason.APPROVAL_INVALID:
                detail["approval_check"] = approval.value
            return self._refuse(tool, call_id, record.reason, list(record.rule_ids), detail)
        if record.decision is Decision.NEEDS_APPROVAL:
            return self._ask_for_approval(tool, call_id, raw, record.reason.value, note)
        if record.reason is Reason.APPROVAL_VALID:
            assert approval_id is not None
            with Session(self.engine) as db:
                used = approvals.consume(db, deps.audit, self.clock, approval_id)
            if not used:
                return self._refuse(
                    tool, call_id, Reason.APPROVAL_INVALID, list(record.rule_ids),
                    {"approval_check": ApprovalCheck.CONSUMED.value},
                )  # fmt: skip

        # 5. service, then 6. redaction
        return self._execute(tool, spec.name, args, ctx, call_id, note)

    # ---- the steps ---------------------------------------------------------------------------

    def _execute(
        self, tool: str, name: str, args: Any, ctx: CallContext, call_id: str, note: Any
    ) -> ToolResponse:
        deps = self.deps
        meter = Meter(deps.audit, deps.policy.file.session_limits, session_id=deps.session_id,
                      call_id=call_id, tool=tool)  # fmt: skip
        status: Status = "ok"
        error: dict[str, Any] | None = None
        result: dict[str, Any] = {}
        notes: dict[str, Any] = {}
        with self._lock, Session(self.engine) as db:
            runtime = Runtime(db=db, deps=deps, ctx=ctx, meter=meter, call_id=call_id)
            try:
                result = HANDLERS[name](runtime, args)
            except BudgetExceeded as stop:
                db.rollback()
                status, error = "budget_exceeded", {
                    "reason_code": Reason.BUDGET_EXCEEDED.value, "kind": stop.kind,
                    "used": stop.used, "limit": stop.limit,
                }  # fmt: skip
            except ToolError as failure:
                db.rollback()
                status, error = "error", {"error_code": failure.code, "message": failure.message}
            except Exception as failure:
                db.rollback()
                status, error = "error", {
                    "error_code": "INTERNAL",
                    "message": f"{type(failure).__name__}: {str(failure)[:200]}",
                }  # fmt: skip
            notes = dict(runtime.notes)

        body: dict[str, Any] = {
            "status": status,
            "tool": tool,
            "call_id": call_id,
            "decision": "ALLOW",
            "policy_version": deps.policy.version,
        }
        if status == "ok":
            body["result"] = result
        else:
            body.update(error or {})
        if notes:
            body["partial"] = notes
        body["charged"] = dict(meter.charged)

        clean, hits = self._redactor.value(body)
        too_big = oversized(clean, deps.policy.file.session_limits.max_result_bytes)
        if too_big:
            clean = {
                "status": "error", "tool": tool, "call_id": call_id,
                "reason_code": Reason.RESULT_TOO_LARGE.value,
                "limit_bytes": deps.policy.file.session_limits.max_result_bytes,
            }  # fmt: skip
            status = "error"
        if hits:
            note(EventType.OUTPUT_REDACTED, {"hits": hits})
        note(
            EventType.CALL_EXECUTED,
            {
                "status": status,
                "result_digest": digest(clean),
                "charged": dict(meter.charged),
                "redactions": hits,
                "too_large": too_big,
            },
        )
        return ToolResponse(status, clean)

    def _decide_unknown(self, tool: str, call_id: str, note: Any) -> ToolResponse:
        ctx = CallContext(tool=tool[:64], tool_known=False, effects=(), role=self.deps.role,
                          facts=ArgumentFacts())  # fmt: skip
        record = evaluate(self.deps.policy, ctx)
        note(EventType.POLICY_DECISION, record.model_dump(mode="json"))
        return self._refuse(tool, call_id, record.reason, list(record.rule_ids), {})

    def _deny(
        self, note: Any, tool: str, call_id: str, reason: Reason, rule_ids: list[str],
        detail: dict[str, Any],
    ) -> ToolResponse:  # fmt: skip
        note(
            EventType.POLICY_DECISION,
            {"decision": "DENY", "reason": reason.value, "rule_ids": rule_ids,
             "policy_version": self.deps.policy.version, "policy_hash": self.deps.policy.hash,
             "inputs": {"tool": tool[:64]}},
        )  # fmt: skip
        return self._refuse(tool, call_id, reason, rule_ids, detail)

    def _refuse(
        self, tool: str, call_id: str, reason: Reason, rule_ids: list[str], detail: dict[str, Any]
    ) -> ToolResponse:
        body = {
            "status": "denied",
            "tool": tool[:64],
            "call_id": call_id,
            "decision": "DENY",
            "reason_code": reason.value,
            "rule_ids": rule_ids,
            "policy_version": self.deps.policy.version,
            **detail,
        }
        clean, _ = self._redactor.value(body)
        return ToolResponse("denied", clean)

    def _ask_for_approval(
        self, tool: str, call_id: str, raw: dict[str, Any], reason: str, note: Any
    ) -> ToolResponse:
        deps = self.deps
        with Session(self.engine) as db:
            row = approvals.find_pending(
                db, self.clock, tool=tool, arguments=raw, policy_hash=deps.policy.hash
            )
            if row is None:
                row = approvals.create(
                    db, deps.audit, self.clock, session_id=deps.session_id, tool=tool,
                    arguments=raw, policy_hash=deps.policy.hash,
                    summary={"tool": tool, "arguments": self._redactor.value(
                        {k: v for k, v in raw.items() if k != "approval_id"})[0]},
                )  # fmt: skip
            approval_id, expires = row.id, row.expires_at
        retry = dict(raw)
        retry["approval_id"] = approval_id
        body = {
            "status": "needs_approval",
            "tool": tool,
            "call_id": call_id,
            "decision": "NEEDS_APPROVAL",
            "reason_code": reason,
            "policy_version": deps.policy.version,
            "approval_id": approval_id,
            "expires_at": expires.isoformat(),
            "how_to_retry": (
                f"A human must approve this exact call (the approver decides it at "
                f"POST /approvals/{approval_id}/decide with the approver token; you cannot). "
                f"After they approve, call {tool} again with exactly the same arguments plus "
                f'"approval_id": "{approval_id}". The approval is single-use, covers only this '
                f"call, expires at {expires.isoformat()} and is void if the policy changes."
            ),
            "retry_with": retry,
        }
        clean, _ = self._redactor.value(body)
        return ToolResponse("needs_approval", clean)
