"""Static gate, AST stage: runs on the host, parses source and never executes it.

Defence in depth in front of the sandbox, not the security boundary. A bundle that fails any rule
is recorded as GATE_FAILED and is never run. The rules are deliberately conservative: a rejected
construct that is actually harmless costs a failed generation, an accepted one that is not costs
much more.
"""

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

GATE_VERSION = "1"
LOCAL_PACKAGE = "integration"

ALLOWED_MODULES = frozenset(
    {"typing", "dataclasses", "re", "datetime", "json", "enum", "collections.abc", "__future__"}
)
ALLOWED_PREFIXES = ("morph_runtime",)
BANNED_NAMES = frozenset(
    {
        "eval", "exec", "compile", "open", "__import__", "getattr", "setattr", "delattr",
        "globals", "locals", "vars", "breakpoint", "input", "exit", "quit", "help", "dir",
        "memoryview",
    }
)  # fmt: skip
BANNED_ATTRIBUTES = frozenset(
    {
        "f_globals",
        "f_locals",
        "f_back",
        "gi_frame",
        "gi_code",
        "cr_frame",
        "tb_frame",
        "func_globals",
    }
)
ALLOWED_DECORATORS = frozenset({"dataclass"})

MAX_FILES = 24
MAX_FILE_LINES = 1500
MAX_TOTAL_LINES = 4000
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://|\bwww\.")
_SECRET = re.compile(
    r"(?i)\bbearer\s+\S{8,}|\b(?:sk|gsk|ghp|xox[a-z])[-_][A-Za-z0-9]{10,}|[A-Za-z0-9+/=_\-]{40,}"
)


class Rule(StrEnum):
    SYNTAX = "SYNTAX"
    PATH = "PATH"
    LIMIT = "LIMIT"
    IMPORT_NOT_ALLOWED = "IMPORT_NOT_ALLOWED"
    IMPORT_STAR = "IMPORT_STAR"
    BANNED_NAME = "BANNED_NAME"
    DUNDER_ACCESS = "DUNDER_ACCESS"
    BANNED_ATTRIBUTE = "BANNED_ATTRIBUTE"
    BANNED_DECORATOR = "BANNED_DECORATOR"
    URL_LITERAL = "URL_LITERAL"
    SECRET_LITERAL = "SECRET_LITERAL"
    ASYNC = "ASYNC"
    GLOBAL_STATEMENT = "GLOBAL_STATEMENT"


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    rule: Rule
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "rule": self.rule.value,
            "message": self.message,
        }


@dataclass(frozen=True)
class GateResult:
    stage: str
    findings: tuple[Finding, ...]

    @property
    def passed(self) -> bool:
        return not self.findings


def _module_allowed(name: str, local: frozenset[str]) -> bool:
    if name in ALLOWED_MODULES or name in local:
        return True
    return any(name == p or name.startswith(p + ".") for p in ALLOWED_PREFIXES)


class _Checker(ast.NodeVisitor):
    def __init__(self, file: str, local: frozenset[str]) -> None:
        self.file = file
        self.local = local
        self.findings: list[Finding] = []

    def _add(self, node: ast.AST, rule: Rule, message: str) -> None:
        self.findings.append(Finding(self.file, getattr(node, "lineno", 0), rule, message))

    # imports
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not _module_allowed(alias.name, self.local):
                self._add(node, Rule.IMPORT_NOT_ALLOWED, f"import of {alias.name!r}")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if any(alias.name == "*" for alias in node.names):
            self._add(node, Rule.IMPORT_STAR, "star import")
        if node.level:
            stems = {m.rsplit(".", 1)[-1] for m in self.local}
            names = {alias.name for alias in node.names}
            ok = node.module in stems if node.module else names <= stems
            if not ok:
                self._add(node, Rule.IMPORT_NOT_ALLOWED, "relative import outside the bundle")
            return
        module = node.module or ""
        if not _module_allowed(module, self.local):
            self._add(node, Rule.IMPORT_NOT_ALLOWED, f"import from {module!r}")
            return
        if module == LOCAL_PACKAGE:
            for alias in node.names:
                if f"{LOCAL_PACKAGE}.{alias.name}" not in self.local:
                    self._add(node, Rule.IMPORT_NOT_ALLOWED, f"{alias.name!r} is not in the bundle")

    # names and attributes
    def visit_Name(self, node: ast.Name) -> None:
        if node.id in BANNED_NAMES:
            self._add(node, Rule.BANNED_NAME, f"use of {node.id!r}")
        elif node.id.startswith("__"):
            self._add(node, Rule.DUNDER_ACCESS, f"use of {node.id!r}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            self._add(node, Rule.DUNDER_ACCESS, f"attribute {node.attr!r}")
        elif node.attr in BANNED_ATTRIBUTES:
            self._add(node, Rule.BANNED_ATTRIBUTE, f"attribute {node.attr!r}")
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        if node.arg.startswith("__") or node.arg in BANNED_NAMES:
            self._add(node, Rule.BANNED_NAME, f"argument named {node.arg!r}")

    # literals
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            if _URL.search(node.value):
                self._add(node, Rule.URL_LITERAL, "URL literal (URLs come from the environment)")
            if _SECRET.search(node.value):
                self._add(node, Rule.SECRET_LITERAL, "string looks like a secret or token")

    # statements
    def _decorators(self, node: ast.FunctionDef | ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name not in ALLOWED_DECORATORS:
                self._add(decorator, Rule.BANNED_DECORATOR, f"decorator {name or '<expr>'!r}")
            else:
                self.visit(decorator)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._decorators(node)
        for child in (node.args, *node.body):
            self.visit(child)
        if node.returns:
            self.visit(node.returns)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._decorators(node)
        for child in (*node.bases, *node.keywords, *node.body):
            self.visit(child)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._add(node, Rule.ASYNC, "async functions are not allowed")

    def visit_Await(self, node: ast.Await) -> None:
        self._add(node, Rule.ASYNC, "await is not allowed")

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._add(node, Rule.ASYNC, "async for is not allowed")

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._add(node, Rule.ASYNC, "async with is not allowed")

    def visit_Global(self, node: ast.Global) -> None:
        self._add(node, Rule.GLOBAL_STATEMENT, "global statement")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self._add(node, Rule.GLOBAL_STATEMENT, "nonlocal statement")


def _bundle_modules(files: Mapping[str, str]) -> frozenset[str]:
    modules = {LOCAL_PACKAGE}
    for path in files:
        pure = PurePosixPath(path)
        if pure.suffix == ".py" and pure.parts[0] == LOCAL_PACKAGE:
            stem = ".".join((*pure.parts[:-1], pure.stem))
            modules.add(stem if pure.stem != "__init__" else ".".join(pure.parts[:-1]))
    return frozenset(modules)


def _path_findings(path: str) -> list[Finding]:
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "\\" in path or not pure.parts:
        return [Finding(path, 0, Rule.PATH, "path must be relative and stay inside the bundle")]
    if pure.suffix != ".py":
        return [Finding(path, 0, Rule.PATH, "only .py files may be generated")]
    return []


def check_ast(files: Mapping[str, str]) -> GateResult:
    """Run the AST rules over a bundle (``{relative path: source}``)."""
    findings: list[Finding] = []
    if len(files) > MAX_FILES:
        findings.append(Finding("", 0, Rule.LIMIT, f"more than {MAX_FILES} files"))
    if sum(s.count("\n") + 1 for s in files.values()) > MAX_TOTAL_LINES:
        findings.append(Finding("", 0, Rule.LIMIT, f"more than {MAX_TOTAL_LINES} lines in total"))
    local = _bundle_modules(files)
    for path, source in sorted(files.items()):
        path_findings = _path_findings(path)
        if path_findings:
            findings.extend(path_findings)
            continue
        if source.count("\n") + 1 > MAX_FILE_LINES:
            findings.append(Finding(path, 0, Rule.LIMIT, f"more than {MAX_FILE_LINES} lines"))
            continue
        try:
            tree = ast.parse(source, filename=path)
        except (SyntaxError, ValueError, RecursionError, MemoryError) as error:
            findings.append(
                Finding(path, getattr(error, "lineno", 0) or 0, Rule.SYNTAX, str(error))
            )
            continue
        checker = _Checker(path, local)
        checker.visit(tree)
        findings.extend(checker.findings)
    return GateResult("ast", tuple(findings))
