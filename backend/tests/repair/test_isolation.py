"""The repair package cannot reach grading data, cannot import code dynamically, and cannot make
a live model call of its own.

The oracle, the answer keys and the bench fixtures are eval-only: feeding any of them back into a
repair would be leakage. These checks read the source, so they hold without running anything:
the import graph reachable from ``app.repair``, the dynamic-import and execution routes, the strings
that could name a grading path, and the file and environment access of the pure modules.

The pure modules (feedback, failures, guards, smoke) read no file and no environment variable. When
the graph and persistence modules arrive, they get their own explicit allow-list here (their prompt
files) and nothing else.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.llm.base import LLMError, ReplayMissError
from app.llm.store import ReplayLLMProvider, ResponseStore

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
REPAIR = APP / "repair"
PURE = ("feedback", "failures", "guards", "smoke")

FORBIDDEN_IMPORT_ROOTS = {"morph_bench", "bench", "oracle", "tests"}
LIVE_PROVIDERS = {"app.llm.groq", "app.llm.ollama", "app.llm.factory"}
DYNAMIC_IMPORT_MODULES = {"importlib", "runpy", "imp", "pkgutil", "zipimport"}
DYNAMIC_NAMES = {"__import__", "exec", "eval", "compile"}
DYNAMIC_ATTRIBUTES = {"import_module", "run_path", "run_module", "exec_module", "__import__"}
GRADING_TOKENS = ("bench", "oracle", "answer_key", "fixtures", "controls", "morph_bench", "tests/")
FILE_AND_ENV_ATTRIBUTES = {
    "read_text", "read_bytes", "write_text", "write_bytes", "open", "glob", "rglob", "iterdir",
    "listdir", "scandir", "environ", "getenv",
}  # fmt: skip


def repair_files() -> list[Path]:
    return sorted(p for p in REPAIR.rglob("*.py"))


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def imported_names(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def module_file(dotted: str) -> Path | None:
    if not dotted.startswith("app"):
        return None
    base = BACKEND.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def closure() -> tuple[set[str], set[str]]:
    """(app modules reachable from app.repair, every imported name seen along the way)."""
    seen_files: set[Path] = set()
    names: set[str] = set()
    pending = list(repair_files())
    while pending:
        path = pending.pop()
        if path in seen_files:
            continue
        seen_files.add(path)
        for name in imported_names(parse(path)):
            names.add(name)
            target = module_file(name)
            if target is not None:
                pending.append(target)
    modules = {
        ".".join(p.relative_to(BACKEND).with_suffix("").parts).removesuffix(".__init__")
        for p in seen_files
    }
    return modules, names


def docstring_nodes(tree: ast.Module) -> set[ast.AST]:
    docs: set[ast.AST] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(body[0].value)
    return docs


# ---- the import graph -----------------------------------------------------------------------


def test_the_closure_is_non_trivial() -> None:
    modules, _ = closure()
    assert {"app.repair.feedback", "app.repair.guards", "app.codegen.gate"} <= modules


def test_nothing_reachable_from_app_repair_imports_grading_code() -> None:
    modules, names = closure()
    roots = {n.split(".")[0] for n in names}
    assert not roots & FORBIDDEN_IMPORT_ROOTS, roots & FORBIDDEN_IMPORT_ROOTS
    for path in (module_file(m) for m in modules):
        assert path is not None
        text = path.read_text(encoding="utf-8")
        assert "answer_key" not in text and "morph_bench" not in text, path


def test_no_live_provider_is_reachable_from_app_repair() -> None:
    """A fixed-start unit can only be seeded by an injected replay: repair builds no provider."""
    modules, names = closure()
    assert not (modules | names) & LIVE_PROVIDERS
    assert "httpx" not in {n.split(".")[0] for n in names}


@pytest.mark.parametrize("name", PURE)
def test_the_pure_modules_import_no_langgraph(name: str) -> None:
    roots = {n.split(".")[0] for n in imported_names(parse(REPAIR / f"{name}.py"))}
    assert not roots & {"langgraph", "langchain_core", "langchain", "langsmith"}


# ---- dynamic import and execution -----------------------------------------------------------


def dynamic_violations(tree: ast.Module) -> list[str]:
    found = [
        f"imports {n}" for n in imported_names(tree) if n.split(".")[0] in DYNAMIC_IMPORT_MODULES
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in DYNAMIC_NAMES:
            found.append(f"uses {node.id} at line {node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr in DYNAMIC_ATTRIBUTES:
            found.append(f"uses .{node.attr} at line {node.lineno}")
    return found


def path_violations(tree: ast.Module) -> list[str]:
    docs = docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docs:
            found += [
                f"line {node.lineno} names {token!r}"
                for token in GRADING_TOKENS
                if token in node.value.lower()
            ]
    return found


@pytest.mark.parametrize("path", repair_files(), ids=lambda p: p.name)
def test_no_dynamic_import_or_execution_route(path: Path) -> None:
    assert dynamic_violations(parse(path)) == []


@pytest.mark.parametrize("path", repair_files(), ids=lambda p: p.name)
def test_no_string_names_a_grading_or_fixture_path(path: Path) -> None:
    assert path_violations(parse(path)) == []


def test_the_scanners_catch_what_they_are_for() -> None:
    bad = ast.parse(
        "import importlib\nfrom runpy import run_path\n"
        "exec('x')\n__import__('os')\neval('1')\nmod.import_module('y')\n"
    )
    found = " ".join(dynamic_violations(bad))
    for expected in ("importlib", "runpy", "exec", "__import__", "eval", ".import_module"):
        assert expected in found, expected
    paths = ast.parse(
        "A = 'bench/oracle/x.py'\nB = 'answer_key.yaml'\nC = 'tests/repair/controls'\n"
        "D = 'fixtures'\nE = 'a harmless string'\n"
    )
    flagged = " ".join(path_violations(paths))
    for token in ("bench", "oracle", "answer_key", "controls", "tests/", "fixtures"):
        assert token in flagged, token
    assert "line 5" not in flagged
    assert path_violations(ast.parse('"""A docstring about the oracle."""\nX = 1\n')) == []


@pytest.mark.parametrize("name", PURE)
def test_the_pure_modules_read_no_file_and_no_environment_variable(name: str) -> None:
    tree = parse(REPAIR / f"{name}.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "open", (name, node.lineno)
        elif isinstance(node, ast.Attribute):
            assert node.attr not in FILE_AND_ENV_ATTRIBUTES, (name, node.lineno, node.attr)
    roots = {n.split(".")[0] for n in imported_names(tree)}
    assert not roots & {"os", "shutil", "glob", "tempfile"}
    # smoke only names a Path to pass the bundle directory on to the sandbox runner
    assert ("pathlib" in roots) == (name == "smoke")


# ---- a missing replay is a hard error -------------------------------------------------------


def test_a_missing_replay_raises_and_no_fallback_exists(tmp_path: Path) -> None:
    from pydantic import BaseModel

    from app.llm.base import LLMRequest

    class Reply(BaseModel):
        value: int

    request = LLMRequest(system="s", parts=("p",), schema_name="reply")
    replay = ReplayLLMProvider(ResponseStore(tmp_path))
    with pytest.raises(ReplayMissError) as raised:
        replay.complete_structured(request, Reply, reask=False)
    assert isinstance(raised.value, LLMError)


# ---- M2: the graph modules --------------------------------------------------------------------


def test_the_closure_now_includes_the_graph_modules() -> None:
    modules, _ = closure()
    assert {"app.repair.nodes", "app.repair.graph", "app.repair.service"} <= modules


def test_langgraph_is_imported_only_by_the_graph_and_the_service() -> None:
    users = {
        p.stem
        for p in repair_files()
        if {n.split(".")[0] for n in imported_names(parse(p))} & {"langgraph"}
    }
    assert users == {"graph", "service"}


def test_only_the_service_reads_the_environment_and_only_for_tracing() -> None:
    readers = {
        p.stem
        for p in repair_files()
        if any(
            isinstance(n, ast.Attribute) and n.attr in {"environ", "getenv"}
            for n in ast.walk(parse(p))
        )
    }
    assert readers == {"service"}
    constants = {
        n.value
        for n in ast.walk(parse(REPAIR / "service.py"))
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isupper()
    }
    assert {c for c in constants if "TRACING" not in c and len(c) > 3} == set()


def test_the_nodes_read_no_file_of_their_own() -> None:
    for name in ("nodes", "graph", "state", "service", "builder", "sizing"):
        for node in ast.walk(parse(REPAIR / f"{name}.py")):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"read_text", "read_bytes", "glob", "rglob", "iterdir"}
            if isinstance(node, ast.Name):
                assert node.id != "open", name


def test_only_the_prompt_builder_reads_a_file_and_only_from_its_own_prompt_directory() -> None:
    readers: dict[str, list[ast.Attribute]] = {}
    for path in repair_files():
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Attribute) and node.attr in {"read_text", "read_bytes"}:
                readers.setdefault(path.stem, []).append(node)
    assert set(readers) == {"prompts"}
    for node in readers["prompts"]:
        source = node.value
        assert isinstance(source, ast.BinOp) and isinstance(source.left, ast.Name)
        assert source.left.id == "PROMPT_DIR"


def test_a_file_read_outside_the_prompt_directory_would_be_caught() -> None:
    bad = ast.parse("(BASE / 'x').read_text()")
    node = next(n for n in ast.walk(bad) if isinstance(n, ast.Attribute) and n.attr == "read_text")
    assert isinstance(node.value, ast.BinOp) and node.value.left.id != "PROMPT_DIR"  # type: ignore[attr-defined]
