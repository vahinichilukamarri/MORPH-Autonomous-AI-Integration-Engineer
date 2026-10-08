"""Test doubles for the repair graph: a fake prompt builder, a scripted provider, a stub sandbox.

Nothing here is a prompt. The builder produces placeholder text so the tests can see feedback and
previous output flow into the next request; the real repair prompts are written in M3.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.codegen.inputs import CodegenInput, MappedField
from app.codegen.review_gate import GateDecision
from app.codegen.sandbox import Outcome, SandboxResult, SandboxRunner
from app.llm.base import BaseLLMProvider, CallMetadata, LLMRequest, RawCompletion
from app.llm.store import ResponseStore
from app.mapping.confidence import ReviewStatus
from app.repair.feedback import Feedback
from app.repair.service import RepairEnv
from app.repair.state import StartMode
from tests.codegen.fixtures import S1_PIPELINES, mapped, persist_run, s1_input
from tests.repair.helpers import control

OK_SMOKE = '{"smoke": "ok", "code": "OK", "detail": ""}'
TESTS_PASS = '{"total": 1, "passed": 1, "failures": []}'


def l2_reply(source: str, notes: str = "n") -> str:
    return json.dumps({"notes": notes, "source": source})


GOOD = control("positive_sync")
SUPPRESSED = GOOD.replace("PAGE_SIZE = 100\n", "PAGE_SIZE = 100  # noqa: E501\n")
DUNDER = GOOD.replace("return report\n", "_ = report.__class__\n    return report\n")


def failing_source(n: int) -> str:
    """Valid Python that passes every guard and fails the AST gate; distinct for each ``n``."""
    return f"def run(keys: tuple[str, ...]) -> int:\n    return keys.__len__() + {n}\n"


@dataclass
class FakeBuilder:
    """Placeholder prompts. ``pad`` inflates repair requests to exercise the size policy."""

    pad: int = 0
    requests: list[LLMRequest] = field(default_factory=list)

    def initial(self, inp: CodegenInput, decision: GateDecision, condition: str) -> LLMRequest:
        return LLMRequest("test system", ("initial", condition), condition)

    def repair(
        self,
        inp: CodegenInput,
        decision: GateDecision,
        condition: str,
        *,
        attempt: int,
        previous_output: str,
        feedback: Feedback,
    ) -> LLMRequest:
        parts = ("repair", str(attempt), previous_output, feedback.render(), "x" * self.pad)
        request = LLMRequest("test system", parts, condition)
        self.requests.append(request)
        return request


class Scripted(BaseLLMProvider):
    """Replies in order; an exception in the list is raised; a call past the end fails the test."""

    name = "scripted"

    def __init__(self, steps: Sequence[Any] = ()) -> None:
        self.steps = list(steps)
        self.calls: list[LLMRequest] = []

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        self.calls.append(request)
        if not self.steps:
            raise AssertionError("an unexpected extra model call")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        text, finish = step if isinstance(step, tuple) else (step, "stop")
        meta = CallMetadata(
            "scripted", "scripted-model", request.fingerprint(response_model), 1, 100, 10,
            total_tokens=110, usage={"total_tokens": 110}, finish_reason=finish, source="scripted",
        )  # fmt: skip
        return RawCompletion(text, meta)


class StubRunner(SandboxRunner):
    """Answers the gate, the generated tests and the smoke test without Docker."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.ruff_findings: list[dict[str, Any]] = []
        self.start_failed: str | None = None  # "ruff", "mypy", "tests" or "smoke"
        self.tests_stdout = TESTS_PASS
        self.smoke_stdout = OK_SMOKE

    def run(
        self,
        bundle_dir: Path,
        argv: Sequence[str],
        *,
        env: Any = None,
        network: Any = None,
        run_id: Any = None,
    ) -> SandboxResult:
        text = " ".join(argv)
        kind = (
            "ruff" if "ruff" in argv else "mypy" if "mypy" in argv
            else "smoke" if "-c" in argv else "tests"
        )  # fmt: skip
        self.calls.append(kind)
        if self.start_failed == kind:
            return SandboxResult(Outcome.START_FAILED, None, "", "", 0.0, "stub")
        stdout = {
            "ruff": json.dumps(self.ruff_findings),
            "mypy": "",
            "tests": self.tests_stdout,
            "smoke": self.smoke_stdout,
        }[kind]
        code = 1 if kind == "ruff" and self.ruff_findings else 0
        assert text
        return SandboxResult(
            Outcome.OK if code == 0 else Outcome.NONZERO, code, stdout, "", 0.0, "stub"
        )


@dataclass
class Rig:
    env: RepairEnv
    live: Scripted
    runner: StubRunner
    session: Session
    run_ids: list[int] = field(default_factory=list)


def conninfo(engine: Engine) -> str:
    return engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


def make_rig(
    session: Session,
    engine: Engine,
    tmp_path: Path,
    steps: Sequence[Any],
    *,
    condition: str = "L2R",
    start_mode: StartMode = StartMode.FRESH,
    seed: BaseLLMProvider | None = None,
    builder: FakeBuilder | None = None,
    fields: tuple[MappedField, ...] | None = None,
    tpm_limit: int = 8000,
    runner: SandboxRunner | None = None,
    smoke_runner: SandboxRunner | None = None,
    after_call: Any = None,
) -> Rig:
    base = s1_input(fields if fields is not None else mapped(S1_PIPELINES))
    run_id = persist_run(
        session, source=("crm.v1", "crm"), target=("support.v1", "support"),
        source_entity="Customer", target_entity="User", fields=base.fields,
    )  # fmt: skip
    live = Scripted(steps)
    stub = StubRunner()
    use = runner if runner is not None else stub
    env = RepairEnv(
        session=session, inp=replace(base, mapping_run_id=run_id), condition=condition,
        start_mode=start_mode, builder=builder or FakeBuilder(), live=live,
        response_store=ResponseStore(tmp_path / "responses"), runner=use,
        smoke_runner=smoke_runner or use,
        conninfo=conninfo(engine), seed=seed, tpm_limit=tpm_limit, after_call=after_call,
    )  # fmt: skip
    return Rig(env, live, stub, session)


def needs_review_fields() -> tuple[MappedField, ...]:
    return mapped(
        S1_PIPELINES, accountState=(ReviewStatus.NEEDS_REVIEW, S1_PIPELINES["accountState"])
    )
