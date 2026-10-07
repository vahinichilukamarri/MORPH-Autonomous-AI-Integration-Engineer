"""The repair-mode smoke test passes D and a known-good L2 module, and fails every known-bad one.

The positive and negative controls are plain text under ``tests/repair/controls``. They are never
imported, never shown to a prompt and never reachable from ``app.repair`` (the isolation test scans
for that). The sandbox tests need Docker (marker: docker).
"""

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.codegen.gate import check_ast
from app.codegen.gate_tools import run_tool_gate
from app.codegen.sandbox import Outcome, SandboxRunner
from app.repair.smoke import (
    SMOKE_TIMEOUT_S,
    SmokeResult,
    build_script,
    make_smoke_runner,
    parse_smoke_output,
    run_smoke,
)
from tests.repair.helpers import Built, build, control, s1_built, s3_approved

SHAPE = {
    "source": {"mode": "LIST", "key_field": "id", "page_param": "page", "size_param": "size"},
    "target": {"id_field": "id", "items_key": "items", "total_key": None},
}


# ---- no sandbox -----------------------------------------------------------------------------


def test_the_script_embeds_the_shape_and_is_valid_python() -> None:
    script = build_script(SHAPE)
    compile(script, "<smoke>", "exec")
    assert '"key_field": "id"' in script and "__SHAPE__" not in script
    assert len(script) < 8000, "it is passed as one command-line argument"


def test_the_runner_has_a_shorter_time_limit_and_nothing_else_changed() -> None:
    limits = make_smoke_runner().limits
    assert limits.timeout_s == SMOKE_TIMEOUT_S < 60.0
    assert (limits.memory_mb, limits.cpus, limits.pids) == (256, 0.5, 64)


OK_LINE = '{"smoke": "ok", "code": "OK", "detail": ""}'
BAD_LINE = '{"smoke": "fail", "code": "BAD_SHAPE", "detail": "d"}'


@pytest.mark.parametrize(
    ("outcome", "exit_code", "stdout", "stderr", "ok", "code", "infra"),
    [
        (Outcome.OK, 0, OK_LINE, "", True, "OK", False),
        (Outcome.OK, 0, "noise\n" + OK_LINE, "", True, "OK", False),
        (Outcome.OK, 0, BAD_LINE, "", False, "BAD_SHAPE", False),
        (Outcome.OK, 0, "", "", False, "CRASH", False),
        (Outcome.NONZERO, 1, "not json", "Traceback...\nKeyError: 'x'", False, "CRASH", False),
        (Outcome.TIMEOUT, None, "", "", False, "TIMEOUT", False),
        (Outcome.OOM, 137, "", "", False, "OOM", False),
        (Outcome.START_FAILED, None, "", "", False, "START_FAILED", True),
    ],
)  # fmt: skip
def test_parse_smoke_output(
    outcome: Outcome, exit_code: int | None, stdout: str, stderr: str, ok: bool, code: str,
    infra: bool,
) -> None:  # fmt: skip
    result = parse_smoke_output(outcome, exit_code, stdout, stderr)
    assert (result.ok, result.code, result.infra) == (ok, code, infra)


# ---- in the sandbox -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runner() -> SandboxRunner:
    return make_smoke_runner()


@pytest.fixture
def workdir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="morph-smoke-") as tmp:
        yield Path(tmp)


def smoke(runner: SandboxRunner, built: Built, workdir: Path) -> SmokeResult:
    return run_smoke(runner, built.write(workdir), built.strategy)


@pytest.mark.docker
@pytest.mark.parametrize("make", [s1_built, lambda: build(s3_approved())], ids=["S1", "S3"])
def test_the_smoke_test_passes_d(runner: SandboxRunner, workdir: Path, make) -> None:  # type: ignore[no-untyped-def]
    result = smoke(runner, make(), workdir)
    assert (result.ok, result.code) == (True, "OK"), result


@pytest.mark.docker
def test_the_positive_control_is_known_good_and_passes_the_smoke_test(
    runner: SandboxRunner, workdir: Path
) -> None:
    built = s1_built(control("positive_sync"))
    # known good means it clears the full v0.4 gate too, not only the smoke test
    assert check_ast(built.files).passed
    bundle = built.write(workdir)
    assert [(g.stage, g.passed) for g in run_tool_gate(runner, bundle)] == [
        ("ruff", True),
        ("mypy", True),
    ]
    result = run_smoke(runner, bundle, built.strategy)
    assert (result.ok, result.code) == (True, "OK"), result


NEGATIVES = [
    ("negative_raises", "RUN_FATAL"),
    ("negative_imports", "IMPORT_ERROR"),
    ("negative_wrong_report", "BAD_SHAPE"),
    ("negative_never_ends", "TIMEOUT"),
]


@pytest.mark.docker
@pytest.mark.parametrize(("name", "code"), NEGATIVES, ids=[n for n, _ in NEGATIVES])
def test_every_negative_control_fails_the_smoke_test(
    runner: SandboxRunner, workdir: Path, name: str, code: str
) -> None:
    result = smoke(runner, s1_built(control(name)), workdir)
    assert (result.ok, result.code, result.infra) == (False, code, False), result
