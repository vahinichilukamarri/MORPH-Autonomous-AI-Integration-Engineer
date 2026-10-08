"""Anti-gaming guards for one repair attempt. Pure: no I/O, no model, no sandbox.

The model's writable surface is small (L1: a strategy and edge records; L2:
``integration/sync.py``).
These checks turn that into a verified invariant. They are static: they cannot prove that the code
behaves, only that the model did not weaken the checks around it. G1 to G5 reject an attempt. G6 and
G7 are shadow guards: they are reported and never reject, until a later, labelled decision.
"""

import ast
import hashlib
import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.codegen.gate import Rule, check_ast
from app.codegen.generated_tests import TESTS_PACKAGE

CASES_PATH = f"{TESTS_PACKAGE}/cases.py"


class Mode(StrEnum):
    ENFORCED = "ENFORCED"
    SHADOW = "SHADOW"


GUARD_MODES: Mapping[str, Mode] = {
    "G1": Mode.ENFORCED,  # only the model-owned file differs from a deterministic rebuild
    "G2": Mode.ENFORCED,  # the base generated tests are unchanged
    "G3": Mode.ENFORCED,  # no lint or type suppression comments (the gate's own rule)
    "G4": Mode.ENFORCED,  # type escapes do not increase between attempts
    "G5": Mode.ENFORCED,  # required and mapped fields are still written
    "G6": Mode.SHADOW,  # the record returned by to_target is not mutated
    "G7": Mode.SHADOW,  # no swallowed exceptions
}


@dataclass(frozen=True)
class GuardFinding:
    guard: str
    message: str
    file: str | None = None
    line: int | None = None

    @property
    def mode(self) -> Mode:
        return GUARD_MODES[self.guard]


@dataclass(frozen=True)
class GuardReport:
    findings: tuple[GuardFinding, ...] = field(default_factory=tuple)

    @property
    def enforced(self) -> tuple[GuardFinding, ...]:
        return tuple(f for f in self.findings if f.mode is Mode.ENFORCED)

    @property
    def shadow(self) -> tuple[GuardFinding, ...]:
        return tuple(f for f in self.findings if f.mode is Mode.SHADOW)

    @property
    def rejected(self) -> bool:
        """Only enforced findings reject an attempt; shadow findings never do."""
        return bool(self.enforced)


# ---- G1 ----------------------------------------------------------------------------------------


def check_surface(
    files: Mapping[str, str], rebuilt: Mapping[str, str], owned: Collection[str]
) -> list[GuardFinding]:
    """G1: every file except the model-owned ones equals an independent deterministic rebuild."""
    findings: list[GuardFinding] = []
    for path in sorted(set(files) | set(rebuilt)):
        if path in owned:
            continue
        if path not in rebuilt:
            findings.append(GuardFinding("G1", "unexpected file in the bundle", path))
        elif path not in files:
            findings.append(GuardFinding("G1", "a generated file is missing", path))
        elif files[path] != rebuilt[path]:
            findings.append(GuardFinding("G1", "differs from its deterministic rebuild", path))
    return findings


# ---- G2 ----------------------------------------------------------------------------------------


def _cases(source: str) -> list[Any] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "CASES"
            and node.value is not None
        ):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return None
            return value if isinstance(value, list) else None
    return None


def check_tests_frozen(
    files: Mapping[str, str], base_tests: Mapping[str, str]
) -> list[GuardFinding]:
    """G2: the base generated tests are unchanged; extra cases may only be appended."""
    findings: list[GuardFinding] = []
    prefix = f"{TESTS_PACKAGE}/"
    for path in sorted(p for p in files if p.startswith(prefix) and p not in base_tests):
        findings.append(GuardFinding("G2", "a file was added to the generated tests", path))
    for path, text in sorted(base_tests.items()):
        if path == CASES_PATH:
            continue
        if path not in files:
            findings.append(GuardFinding("G2", "a base test file is missing", path))
        elif files[path] != text:
            findings.append(GuardFinding("G2", "a base test file was changed", path))
    base = _cases(base_tests.get(CASES_PATH, ""))
    if base is None:
        return findings
    final = _cases(files.get(CASES_PATH, ""))
    if final is None:
        findings.append(GuardFinding("G2", "the test cases cannot be read", CASES_PATH))
    elif len(final) < len(base):
        message = f"test cases dropped: {len(base)} base cases, {len(final)} left"
        findings.append(GuardFinding("G2", message, CASES_PATH))
    elif final[: len(base)] != base:
        findings.append(GuardFinding("G2", "a base test case was changed", CASES_PATH))
    return findings


# ---- G3 ----------------------------------------------------------------------------------------


def check_suppression(files: Mapping[str, str]) -> list[GuardFinding]:
    """G3: the AST gate's own suppression rule, so there is one definition of it."""
    return [
        GuardFinding("G3", f.message, f.file, f.line)
        for f in check_ast(dict(files)).findings
        if f.rule is Rule.SUPPRESSION
    ]


# ---- G4 ----------------------------------------------------------------------------------------


def type_escape_count(source: str) -> int:
    """How many ways the code opts out of precise types: ``Any``, ``cast(...)``, ``object``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    count = 0
    annotations: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "Any":
            count += 1
        elif isinstance(node, ast.Attribute) and node.attr == "Any":
            count += 1
        elif isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
            count += int(name == "cast")
        elif isinstance(node, ast.arg) and node.annotation is not None:
            annotations.append(node.annotation)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns is not None:
            annotations.append(node.returns)
    for annotation in annotations:
        count += sum(isinstance(n, ast.Name) and n.id == "object" for n in ast.walk(annotation))
    return count


def check_any_loosening(
    sources: Mapping[str, str], previous: Mapping[str, str] | None
) -> list[GuardFinding]:
    """G4: type escapes may not increase against the previous attempt (nothing to compare at 0)."""
    if previous is None:
        return []
    findings = []
    for path, source in sorted(sources.items()):
        if path not in previous:
            continue
        before, after = type_escape_count(previous[path]), type_escape_count(source)
        if after > before:
            findings.append(GuardFinding("G4", f"type escapes rose from {before} to {after}", path))
    return findings


# ---- G5 ----------------------------------------------------------------------------------------


def check_required_fields(
    strategy: Mapping[str, Any], required_on_create: Collection[str], included: Collection[str]
) -> list[GuardFinding]:
    """G5: required target fields are still written, and every mapped field is written somewhere.

    ``required_on_create`` and ``included`` come from the contract and the review decision, never
    from grading data: the required target fields that have an approved mapping, and the approved
    mappings the contract lets a create or update request carry. A mapped identity that the target
    assigns itself (S3's ``customer_id``) is in neither, because it is used to find a record and is
    never written; counting it would reject D's own bundle.
    """
    target = strategy.get("target", {})
    create = set(target.get("create_fields", ()))
    update = set(target.get("update_fields", ()))
    create_only = set(target.get("create_only", ()))
    findings: list[GuardFinding] = []
    missing = sorted(set(required_on_create) - create)
    if missing:
        message = f"required fields not written on create: {', '.join(missing)}"
        findings.append(GuardFinding("G5", message))
    if target.get("mode") == "UPSERT":
        missing = sorted(set(required_on_create) - update)
        if missing:
            message = f"required fields not written on replace: {', '.join(missing)}"
            findings.append(GuardFinding("G5", message))
    nowhere = sorted(set(included) - create - update - create_only)
    if nowhere:
        findings.append(GuardFinding("G5", f"mapped fields written nowhere: {', '.join(nowhere)}"))
    return findings


# ---- G6 and G7 (shadow) -----------------------------------------------------------------------


def _is_call_to(node: ast.AST, name: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    callee = node.func
    if isinstance(callee, ast.Name):
        return callee.id == name
    return isinstance(callee, ast.Attribute) and callee.attr == name


def check_record_mutation(source: str, path: str) -> list[GuardFinding]:
    """G6 (shadow): del, pop, popitem or clear on a record returned by ``to_target``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_call_to(node.value, "to_target"):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_call_to(node.value, "to_target")
            and isinstance(node.target, ast.Name)
        ):
            names.add(node.target.id)
    findings: list[GuardFinding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Delete):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in names
                ):
                    message = f"del on {target.value.id!r}"
                    findings.append(GuardFinding("G6", message, path, node.lineno))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("pop", "popitem", "clear")
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in names
        ):
            message = f"{node.func.attr}() on {node.func.value.id!r}"
            findings.append(GuardFinding("G6", message, path, node.lineno))
    return findings


def check_swallowed_failures(source: str, path: str) -> list[GuardFinding]:
    """G7 (shadow): a bare or catch-all ``except`` whose body does nothing."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    findings: list[GuardFinding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        broad = node.type is None or (
            isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException")
        )
        silent = all(
            isinstance(stmt, ast.Pass)
            or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
            for stmt in node.body
        )
        if broad and silent:
            message = "a broad except that does nothing"
            findings.append(GuardFinding("G7", message, path, node.lineno))
    return findings


# ---- no-op detection ---------------------------------------------------------------------------


def output_hash_l2(source: str) -> str:
    """Identical when only blank lines or trailing whitespace differ.

    Comments count: deleting a suppression comment is a real repair, not a repeat.
    """
    lines = (line.rstrip() for line in source.splitlines())
    normalised = "\n".join(line for line in lines if line)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _canonical_json(item: object) -> object:
    if not isinstance(item, str):
        return item
    try:
        return json.dumps(json.loads(item), sort_keys=True, separators=(",", ":"))
    except ValueError:
        return item


def output_hash_l1(proposal: Mapping[str, Any]) -> str:
    """Canonical JSON of the proposal; edge-record strings are compared as JSON when they parse."""
    canonical: dict[str, Any] = {}
    for key, value in proposal.items():
        if key == "edge_record_json" and isinstance(value, list):
            canonical[key] = [_canonical_json(item) for item in value]
        else:
            canonical[key] = value
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_noop(output_hash: str, earlier: Iterable[str]) -> bool:
    """True when this output equals ANY earlier attempt's output, not only the previous one."""
    return output_hash in set(earlier)


# ---- all guards --------------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardInputs:
    files: Mapping[str, str]
    rebuilt: Mapping[str, str]
    owned: frozenset[str]
    base_tests: Mapping[str, str]
    sources: Mapping[str, str]  # the model-owned Python files, by bundle path
    previous_sources: Mapping[str, str] | None = None
    strategy: Mapping[str, Any] | None = None
    required_on_create: Collection[str] = ()
    included: Collection[str] = ()


def evaluate_guards(inputs: GuardInputs) -> GuardReport:
    """Run every guard whose inputs are present. Enforced and shadow findings are kept apart."""
    findings = [
        *check_surface(inputs.files, inputs.rebuilt, inputs.owned),
        *check_tests_frozen(inputs.files, inputs.base_tests),
        *check_suppression(inputs.files),
        *check_any_loosening(inputs.sources, inputs.previous_sources),
    ]
    if inputs.strategy is not None:
        findings += check_required_fields(
            inputs.strategy, inputs.required_on_create, inputs.included
        )
    for path, source in sorted(inputs.sources.items()):
        findings += check_record_mutation(source, path)
        findings += check_swallowed_failures(source, path)
    return GuardReport(tuple(findings))
