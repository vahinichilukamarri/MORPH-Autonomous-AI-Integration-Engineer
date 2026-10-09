"""The registry: 13 tools, each with a handler, none with a forbidden capability, one path in."""

import ast
import inspect
from collections.abc import Iterator
from pathlib import Path

from app.mcp_server import adapters
from app.mcp_server.gateway import Gateway
from app.policy.toolspec import BY_NAME, FORBIDDEN_NAME_PATTERN, TOOL_SPECS, forbidden_names

APP = Path(__file__).resolve().parents[2] / "app"
PACKAGE = APP / "mcp_server"
FORBIDDEN_ARGUMENT_FIELDS = (
    "policy", "environment", "data_class", "role", "token", "secret", "url", "command", "path",
    "shell", "query", "sql", "header", "key", "password",
)  # fmt: skip
ALLOWED_IMPORT_ROOTS = {
    "anyio", "collections", "dataclasses", "datetime", "json", "mcp", "mcp_types", "pathlib",
    "sqlalchemy", "threading", "typing", "uuid", "app", "sys", "pydantic",
}  # fmt: skip


def package_sources() -> Iterator[tuple[Path, ast.Module]]:
    for path in sorted(PACKAGE.glob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def test_every_tool_has_exactly_one_handler_and_every_handler_is_a_tool() -> None:
    assert set(adapters.HANDLERS) == {s.name for s in TOOL_SPECS} == set(BY_NAME)
    for name, handler in adapters.HANDLERS.items():
        parameters = list(inspect.signature(handler).parameters)
        assert parameters == ["rt", "args"], name


def test_no_tool_name_is_a_forbidden_capability() -> None:
    assert forbidden_names([s.name for s in TOOL_SPECS]) == []
    assert FORBIDDEN_NAME_PATTERN.search("approve_mapping") and FORBIDDEN_NAME_PATTERN.search(
        "set_policy_attributes"
    )


def test_no_tool_accepts_an_argument_that_could_carry_policy_or_secrets() -> None:
    offenders = {
        spec.name: field
        for spec in TOOL_SPECS
        for field in spec.args_model.model_fields
        for word in FORBIDDEN_ARGUMENT_FIELDS
        if word in field.lower() and field not in ("file",)
    }
    assert offenders == {}


def test_every_argument_model_refuses_extra_fields_and_coercion() -> None:
    for spec in TOOL_SPECS:
        config = spec.args_model.model_config
        assert config.get("extra") == "forbid" and config.get("strict") is True, spec.name


def test_the_package_cannot_write_attributes_or_decide_approvals() -> None:
    names = {"set_attributes", "decide", "SystemPolicyAttribute", "ApprovalRequest"}
    hits: dict[str, set[str]] = {}
    for path, tree in package_sources():
        found = {
            n.attr if isinstance(n, ast.Attribute) else n.id
            for n in ast.walk(tree)
            if (isinstance(n, ast.Attribute) and n.attr in names)
            or (isinstance(n, ast.Name) and n.id in names)
        }
        if found:
            hits[path.name] = found
    assert hits == {}


def test_the_package_reaches_for_nothing_outside_the_app_and_standard_libraries() -> None:
    roots: set[str] = set()
    for _, tree in package_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
    assert roots <= ALLOWED_IMPORT_ROOTS, sorted(roots - ALLOWED_IMPORT_ROOTS)


def module_file(dotted: str) -> Path | None:
    base = APP.parent.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def closure(start: str) -> set[str]:
    seen: set[str] = set()
    todo = [start]
    while todo:
        name = todo.pop()
        if name in seen or not name.startswith("app"):
            continue
        path = module_file(name)
        if path is None:
            continue
        seen.add(name)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                todo.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                todo.append(node.module)
                todo.extend(f"{node.module}.{a.name}" for a in node.names)
    return seen


def test_the_servers_import_graph_never_reaches_the_bench_the_oracle_or_the_tests() -> None:
    reached = closure("app.mcp_server.server") | closure("app.mcp_server.gateway")
    assert {"app.repair.service", "app.codegen.service", "app.policy.evaluate"} <= reached
    bad = [m for m in reached if any(t in m for t in ("bench", "oracle", "answer", "fixture"))]
    assert bad == []
    outside = [m for m in reached if not m.startswith("app")]
    assert outside == []


def test_the_gateway_source_runs_its_stages_in_the_documented_order() -> None:
    source = inspect.getsource(Gateway.call)
    markers = ["# 1. receive", "# 2. schema", "# 3. floor and policy", "# 4. approval",
               "# 5. service, then 6. redaction"]  # fmt: skip
    positions = [source.index(m) for m in markers]
    assert positions == sorted(positions)
    assert source.index("evaluate(policy, ctx)") < source.index("_execute(")
    assert source.index("approvals.consume") < source.index("_execute(")
