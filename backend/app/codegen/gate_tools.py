"""Static gate, tool stages: ruff and mypy --strict, run inside the sandbox image.

The configuration lives in the image (``/opt/gate``), not in the bundle, so generated code cannot
weaken it. The container has no network and the bundle is mounted read-only, like any other run.
A bundle is only ever executed after the AST stage and both tool stages have passed.
"""

import json
import re
from pathlib import Path

from app.codegen.gate import Finding, GateResult, Rule
from app.codegen.sandbox import Outcome, SandboxResult, SandboxRunner

_MYPY_LINE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):(?:\d+:)? (?P<sev>error|note): (?P<msg>.*)$"
)


def _tool_failure(stage: str, result: SandboxResult) -> GateResult:
    detail = f"{stage} did not complete: {result.outcome.value}, exit {result.exit_code}"
    return GateResult(
        stage, (Finding("", 0, Rule.TOOL_FAILURE, detail + " " + result.stderr[:300]),)
    )


def run_ruff(runner: SandboxRunner, bundle_dir: Path) -> GateResult:
    result = runner.run(
        bundle_dir,
        [
            "python", "-m", "ruff", "check", "--config", "/opt/gate/ruff.toml",
            "--no-cache", "--ignore-noqa", "--output-format", "json", "integration",
        ],
        network=None,
    )  # fmt: skip
    if result.outcome not in (Outcome.OK, Outcome.NONZERO) or result.exit_code not in (0, 1):
        return _tool_failure("ruff", result)
    findings = tuple(
        Finding(
            str(item["filename"]).replace("\\", "/").split("/app/bundle/", 1)[-1],
            int(item["location"]["row"]),
            Rule.SYNTAX if item["code"] is None else Rule.LINT,
            f"{item['code']}: {item['message']}",
        )
        for item in json.loads(result.stdout or "[]")
    )
    return GateResult("ruff", findings)


def run_mypy(runner: SandboxRunner, bundle_dir: Path) -> GateResult:
    result = runner.run(
        bundle_dir,
        [
            "python", "-m", "mypy", "--config-file", "/opt/gate/mypy.ini",
            "--cache-dir", "/scratch/mypy-cache", "--no-error-summary", "integration",
        ],
        network=None,
    )  # fmt: skip
    if result.outcome not in (Outcome.OK, Outcome.NONZERO) or result.exit_code not in (0, 1):
        return _tool_failure("mypy", result)
    findings = []
    for line in result.stdout.splitlines():
        match = _MYPY_LINE.match(line.strip())
        if match and match["sev"] == "error":
            findings.append(
                Finding(
                    match["file"].replace("\\", "/"),
                    int(match["line"]),
                    Rule.TYPE_ERROR,
                    match["msg"],
                )
            )
    return GateResult("mypy", tuple(findings))


def run_tool_gate(runner: SandboxRunner, bundle_dir: Path) -> list[GateResult]:
    return [run_ruff(runner, bundle_dir), run_mypy(runner, bundle_dir)]
