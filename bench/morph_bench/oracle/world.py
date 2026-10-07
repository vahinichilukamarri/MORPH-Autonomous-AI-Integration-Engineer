"""The oracle's view of a run: ephemeral mock systems, seeding, the sandbox and black-box reads.

The oracle judges from the target system's own state and from the request log the mock keeps,
never from what the integration says about itself.
"""

import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.codegen.sandbox import MockEnvironment, SandboxLimits, SandboxResult, SandboxRunner

from morph_bench.oracle.models import OracleFixture

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
AUTH_HEADER = {"crm": "x-api-key", "support": "authorization"}


@dataclass(frozen=True)
class RunResult:
    sandbox: SandboxResult
    report: dict[str, Any] | None
    elapsed_s: float

    @property
    def counts(self) -> dict[str, int]:
        return dict(self.report["counts"]) if self.report else {}

    @property
    def by_key(self) -> dict[str, dict[str, Any]]:
        return {str(r["key"]): r for r in (self.report or {}).get("records", [])}

    @property
    def status(self) -> str:
        return str(self.report["status"]) if self.report else "NO_REPORT"

    @property
    def fatal(self) -> dict[str, Any] | None:
        return self.report["fatal"] if self.report else None


class Harness:
    def __init__(
        self, fixture: OracleFixture, mocks: MockEnvironment, bundle: Path, timeout_s: float = 60.0
    ) -> None:
        self.fx = fixture
        self.mocks = mocks
        self.bundle = bundle
        self.runner = SandboxRunner(limits=SandboxLimits(timeout_s=timeout_s))
        self.runner.verify_limits()

    # ---- admin ---------------------------------------------------------------------------

    def system(self, role: str) -> str:
        return self.fx.source_system if role == "source" else self.fx.target_system

    def admin(self, role: str, method: str, path: str, body: Any = None) -> Any:
        system = self.system(role)
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(  # noqa: S310
            self.mocks.admin_url(system) + path,
            data=data,
            method=method,
            headers={
                "X-Admin-Token": self.mocks.admin_token(system),
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read()
        return json.loads(raw) if raw else None

    def seed(
        self,
        source: list[dict[str, Any]],
        target: list[dict[str, Any]],
        *,
        source_contract: str | None = None,
        target_contract: str | None = None,
    ) -> None:
        """Reset both systems to a known state and clear faults and request logs."""
        for role, records, contract in (
            ("source", source, source_contract or self.fx.contract),
            ("target", target, target_contract or self.fx.contract),
        ):
            self.admin(role, "POST", "/__admin/reset")
            self.admin(role, "PUT", "/__admin/faults", {"contract_version": contract})
            self.admin(role, "PUT", "/__admin/state", {"records": records})
            self.admin(role, "DELETE", "/__admin/requests")

    def faults(self, role: str, **profile: Any) -> None:
        current = self.admin(role, "GET", "/__admin/faults")
        self.admin(role, "PUT", "/__admin/faults", {**current, **profile})

    def clear_faults(self) -> None:
        for role in ("source", "target"):
            current = self.admin(role, "GET", "/__admin/faults")
            self.admin(
                role, "PUT", "/__admin/faults", {"contract_version": current["contract_version"]}
            )

    def state(self, role: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = self.admin(role, "GET", "/__admin/state")["records"]
        return records

    def requests(self, role: str) -> list[dict[str, Any]]:
        log: list[dict[str, Any]] = self.admin(role, "GET", "/__admin/requests")
        return log

    def clear_requests(self) -> None:
        for role in ("source", "target"):
            self.admin(role, "DELETE", "/__admin/requests")

    def writes(self, role: str) -> list[dict[str, Any]]:
        return [e for e in self.requests(role) if e["method"] in WRITE_METHODS]

    # ---- running -------------------------------------------------------------------------

    def run(
        self,
        *,
        source_credential: str | None = None,
        target_credential: str | None = None,
        keys: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> RunResult:
        env = self.mocks.role_env(
            self.fx.source_system,
            self.fx.target_system,
            source_credential=source_credential,
            target_credential=target_credential,
        )
        if keys is not None:
            env["MORPH_SOURCE_KEYS"] = ",".join(keys)
        env.update(extra_env or {})
        started = time.monotonic()
        result = self.runner.run(
            self.bundle,
            ["python", "-E", "-s", "-B", "-m", "integration"],
            env=env,
            network=self.mocks.network,
            run_id=self.mocks.run_id,
        )
        elapsed = time.monotonic() - started
        report: dict[str, Any] | None = None
        lines = result.stdout.strip().splitlines()
        if lines:
            try:
                parsed = json.loads(lines[-1])
                report = parsed if isinstance(parsed, dict) else None
            except ValueError:
                report = None
        return RunResult(result, report, elapsed)

    def keys_for(self, source_records: list[dict[str, Any]]) -> list[str] | None:
        """The operator-supplied ids when the source cannot be listed."""
        if not self.fx.keys_mode:
            return None
        return [str(r[self.fx.source_key]) for r in source_records]
