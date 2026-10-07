"""ruff and mypy stages of the gate, run in the sandbox image (marker: docker)."""

from pathlib import Path

import pytest

from app.codegen.compiler import FieldSpec, compile_transform_module
from app.codegen.gate import Rule, check_ast
from app.codegen.gate_tools import run_mypy, run_ruff
from app.codegen.sandbox import SandboxRunner
from app.mapping.transform import Cast, Copy, Transformation

pytestmark = pytest.mark.docker


def write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


GOOD = {
    "integration/__init__.py": "",
    "integration/transform.py": compile_transform_module(
        [FieldSpec("customerId", Transformation(steps=(Copy(field="a"), Cast(to="str"))))]
    ),
}


def test_compiled_bundle_passes_ruff_and_mypy(tmp_path: Path) -> None:
    runner = SandboxRunner()
    bundle = write(tmp_path, GOOD)
    assert run_ruff(runner, bundle).passed
    assert run_mypy(runner, bundle).passed


def test_lint_and_type_errors_are_reported(tmp_path: Path) -> None:
    runner = SandboxRunner()
    bundle = write(
        tmp_path,
        {
            "integration/__init__.py": "",
            "integration/bad.py": (
                "import re\n"
                "def f(x: int) -> str:\n"
                "    return x\n"
                "PASSWORD = 'hunter2'  # noqa: S105\n"
            ),
        },
    )
    ruff = run_ruff(runner, bundle)
    codes = " ".join(f.message for f in ruff.findings)
    assert not ruff.passed and "F401" in codes and "S105" in codes  # noqa is ignored
    mypy = run_mypy(runner, bundle)
    assert not mypy.passed and any(f.rule is Rule.TYPE_ERROR and f.line == 3 for f in mypy.findings)


def test_the_bundle_cannot_weaken_the_gate_configuration(tmp_path: Path) -> None:
    runner = SandboxRunner()
    bundle = write(
        tmp_path,
        {
            "integration/__init__.py": "",
            "integration/bad.py": "def f(x):\n    return x\n",
            "pyproject.toml": "[tool.ruff]\nselect=[]\n[tool.mypy]\nstrict=false\n",
            "mypy.ini": "[mypy]\nstrict=False\n",
        },
    )
    assert not run_mypy(runner, bundle).passed


def test_suppression_comments_fail_the_ast_stage() -> None:
    for line in [
        "x = 1  # noqa",
        "y: int = 'a'  # type: ignore",
        "# mypy: ignore-errors",
        "z = 1  # ruff: noqa",
    ]:
        assert Rule.SUPPRESSION in {f.rule for f in check_ast({"integration/x.py": line}).findings}
