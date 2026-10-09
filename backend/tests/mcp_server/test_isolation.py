"""The MCP SDK is imported only under ``app/mcp_server``, and the rest of the app never loads it.

Two checks, each with a control so a pass means something: an AST scan of every module under
``app/``, and a subprocess that imports the application's main paths (the D path, discovery,
mapping, repair, the API) and lists what ended up in ``sys.modules``.
"""

import ast
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
BACKEND = APP.parent
SDK_ROOTS = {"mcp", "mcp_types"}
PACKAGE = "mcp_server"
MAIN_PATHS = (
    "app.main", "app.codegen.service", "app.codegen.generator", "app.codegen.gate",
    "app.repair.service", "app.mapping.runner", "app.discovery.parser", "app.api.integrations",
)  # fmt: skip


def imported_modules(source: str) -> set[str]:
    """Every dotted module name the source imports (``from a import b`` gives ``a`` and ``a.b``)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def imported_roots(source: str) -> set[str]:
    return {name.split(".")[0] for name in imported_modules(source)}


def modules_outside_package() -> list[Path]:
    return [p for p in sorted(APP.rglob("*.py")) if p.relative_to(APP).parts[0] != PACKAGE]


def run_python(code: str) -> str:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def test_the_scan_finds_an_sdk_import_when_there_is_one() -> None:
    assert SDK_ROOTS & imported_roots("import mcp.server\n")
    assert SDK_ROOTS & imported_roots("from mcp_types import TextContent\n")
    assert not SDK_ROOTS & imported_roots("import json\nfrom app.llm import base\n")


def test_the_sdk_is_imported_only_under_app_mcp_server() -> None:
    offenders = [
        str(p.relative_to(APP))
        for p in modules_outside_package()
        if SDK_ROOTS & imported_roots(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
    inside = [
        p for p in (APP / PACKAGE).rglob("*.py") if "mcp" in imported_roots(p.read_text("utf-8"))
    ]
    assert inside, "control: the package itself does import the SDK"


def test_no_other_package_imports_the_mcp_server() -> None:
    ours = f"app.{PACKAGE}"
    assert any(
        m == ours or m.startswith(f"{ours}.") for m in imported_modules("import app.mcp_server.x")
    )
    offenders = [
        str(p.relative_to(APP))
        for p in modules_outside_package()
        if any(
            m == ours or m.startswith(f"{ours}.")
            for m in imported_modules(p.read_text(encoding="utf-8"))
        )
    ]
    assert offenders == []


def test_the_main_paths_do_not_load_the_sdk() -> None:
    code = (
        f"import sys, importlib; [importlib.import_module(m) for m in {list(MAIN_PATHS)!r}];"
        f"print(sorted(m for m in sys.modules if m.split('.')[0] in {sorted(SDK_ROOTS)!r}))"
    )
    assert run_python(code) == "[]"


def test_importing_the_package_alone_loads_nothing_but_the_server_module_does() -> None:
    bare = (
        "import sys, app.mcp_server;"
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'mcp'))"
    )
    assert run_python(bare) == "[]"
    server = "import sys, app.mcp_server.server; print('mcp' in sys.modules)"
    assert run_python(server) == "True", "control: importing the server module does load the SDK"
