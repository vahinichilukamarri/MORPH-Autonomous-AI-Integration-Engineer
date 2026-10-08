"""Test doubles for the repair harness: a scripted model and a stub sandbox. No prompts here."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.codegen.sandbox import Outcome, SandboxResult, SandboxRunner
from app.llm.base import BaseLLMProvider, CallMetadata, LLMRequest, RawCompletion
from pydantic import BaseModel

CONTROL = (
    Path(__file__).resolve().parents[2]
    / "backend" / "tests" / "repair" / "controls" / "positive_sync.py.txt"
)  # fmt: skip
GOOD = CONTROL.read_text(encoding="utf-8")
SUPPRESSED = GOOD.replace("PAGE_SIZE = 100\n", "PAGE_SIZE = 100  # noqa: E501\n")


def l2_reply(source: str) -> str:
    return json.dumps({"notes": "n", "source": source})


class Scripted(BaseLLMProvider):
    """Replies in order; a call past the end fails the test."""

    name = "scripted"

    def __init__(self, steps: Sequence[str]) -> None:
        self.steps = list(steps)
        self.calls: list[LLMRequest] = []

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        self.calls.append(request)
        if not self.steps:
            raise AssertionError("an unexpected extra model call")
        meta = CallMetadata(
            "scripted", "scripted-model", request.fingerprint(response_model), 1, 100, 10, 0,
            total_tokens=110, usage={"total_tokens": 110}, finish_reason="stop", source="network",
        )  # fmt: skip
        return RawCompletion(self.steps.pop(0), meta)


class StubRunner(SandboxRunner):
    """Answers the gate tools, the generated tests and the smoke test without Docker."""

    def run(
        self,
        bundle_dir: Path,
        argv: Sequence[str],
        *,
        env: Any = None,
        network: Any = None,
        run_id: Any = None,
    ) -> SandboxResult:
        if "ruff" in argv:
            stdout = "[]"
        elif "mypy" in argv:
            stdout = ""
        elif "-c" in argv:
            stdout = '{"smoke": "ok", "code": "OK", "detail": ""}'
        else:
            stdout = '{"total": 1, "passed": 1, "failures": []}'
        return SandboxResult(Outcome.OK, 0, stdout, "", 0.0, "stub")
