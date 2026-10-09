"""Budgets that count what a tool call really does, including inside a repair run.

Every model call and every sandbox run is charged before it happens, as an audit event, against the
limits of the active policy. The counters are the audit log itself (events of type CHARGE for the
session), so they cannot drift from the record. A repair run makes up to four model calls and a
dozen sandbox runs inside one tool call; each of them passes through here.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.codegen.sandbox import SandboxResult, SandboxRunner
from app.llm.base import BaseLLMProvider, LLMRequest, RawCompletion
from app.policy.audit import AuditLog, EventType
from app.policy.models import SessionLimits

Kind = Literal["model_call", "sandbox_run"]


class BudgetExceeded(Exception):
    def __init__(self, kind: str, used: int, limit: int) -> None:
        super().__init__(f"{kind} budget exhausted ({used} of {limit} used)")
        self.kind = kind
        self.used = used
        self.limit = limit


class Meter:
    def __init__(
        self, audit: AuditLog, limits: SessionLimits, *, session_id: str, call_id: str, tool: str
    ) -> None:
        self._audit = audit
        self._limits = limits
        self._session_id = session_id
        self._call_id = call_id
        self._tool = tool
        self.charged: dict[str, int] = {"model_call": 0, "sandbox_run": 0}

    def charge(self, kind: Kind) -> None:
        limit = (
            self._limits.max_model_calls if kind == "model_call" else self._limits.max_sandbox_runs
        )
        used = self._audit.count(self._session_id, EventType.CHARGE, kind=kind)
        if used >= limit:
            self._audit.append(
                EventType.BUDGET_EXCEEDED, session_id=self._session_id, principal="system",
                tool=self._tool, call_id=self._call_id,
                payload={"kind": kind, "used": used, "limit": limit},
            )  # fmt: skip
            raise BudgetExceeded(kind, used, limit)
        self._audit.append(
            EventType.CHARGE, session_id=self._session_id, principal="system", tool=self._tool,
            call_id=self._call_id, payload={"kind": kind, "n": used + 1},
        )  # fmt: skip
        self.charged[kind] += 1


class MeteredProvider(BaseLLMProvider):
    """Charges one model call per request, then delegates (cache hits count too)."""

    def __init__(self, inner: BaseLLMProvider, meter: Meter) -> None:
        self._inner = inner
        self._meter = meter
        self.name = inner.name

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        self._meter.charge("model_call")
        return self._inner.complete_raw(request, response_model)


class MeteredRunner(SandboxRunner):
    """Charges one sandbox run per ``run``, then delegates to the wrapped runner."""

    def __init__(self, inner: SandboxRunner, meter: Meter) -> None:
        super().__init__(inner.image, inner.limits)
        self._inner = inner
        self._meter = meter

    def run(
        self,
        bundle_dir: Path,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        network: str | None = None,
        run_id: str | None = None,
    ) -> SandboxResult:
        self._meter.charge("sandbox_run")
        return self._inner.run(bundle_dir, argv, env=env, network=network, run_id=run_id)
