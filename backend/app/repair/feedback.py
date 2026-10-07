"""Structured, deterministic feedback for one repair attempt. Pure: no I/O, no model.

Feedback is built only from structured fields (rule codes, file, line, a short template message),
never from raw tool output. It is sorted, deduplicated and capped, paths are POSIX and relative to
the bundle, and no environment value or opaque string ever appears in it: a literal that looks like
a secret is described by where it is and how long it is, never by what it says.
"""

import ast
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.codegen.gate import Finding, GateResult, Rule
from app.repair.guards import GuardFinding

MAX_ITEMS = 8
MAX_CHARS = 1200  # the item lines together
MAX_MESSAGE_CHARS = 200
MAX_HISTORY_CHARS = 240
MIN_SECRET_LENGTH = 6  # shorter configured values would redact ordinary words

CASES_PATH = "tests_generated/cases.py"

# Kept equal to the AST gate's own pattern (a test asserts it): a secret-like literal.
SECRET_PATTERN = re.compile(
    r"(?i)\bbearer\s+\S{8,}|\b(?:sk|gsk|ghp|xox[a-z])[-_][A-Za-z0-9]{10,}|[A-Za-z0-9+/=_\-]{40,}"
)
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9+/=_\-]{40,}")
_SYNTAX_LOCATION = re.compile(r" \([^()]*line \d+\)")
_WINDOWS_PATH = re.compile(r"\b(?:[A-Za-z]:)?\\?(?:[\w.\-]+\\)+[\w.\-]+")
_RUFF_CODE = re.compile(r"^([A-Z]+[0-9]+): (.*)$", re.DOTALL)


class Stage(StrEnum):
    """In the order feedback is listed."""

    PROPOSAL = "PROPOSAL"
    GUARD = "GUARD"
    AST = "AST"
    RUFF = "RUFF"
    MYPY = "MYPY"
    TESTS = "TESTS"
    SMOKE = "SMOKE"


_STAGE_ORDER = {stage: index for index, stage in enumerate(Stage)}
_GATE_STAGES = {"ast": Stage.AST, "ruff": Stage.RUFF, "mypy": Stage.MYPY}


@dataclass(frozen=True)
class FeedbackItem:
    stage: Stage
    code: str
    message: str
    file: str | None = None
    line: int | None = None

    def render(self) -> str:
        where = ""
        if self.file:
            where = f" {self.file}" + (f":{self.line}" if self.line else "")
        return f"[{self.stage}] {self.code}{where}: {self.message}"


@dataclass(frozen=True)
class Feedback:
    attempt: int
    items: tuple[FeedbackItem, ...]
    omitted: int
    history: str = ""

    def codes(self) -> tuple[str, ...]:
        return tuple(f"{item.stage}.{item.code}" for item in self.items)

    def render(self) -> str:
        head = (
            f"Feedback on attempt {self.attempt}: {len(self.items)} item(s), {self.omitted} omitted"
        )
        lines = [head, *(item.render() for item in self.items)]
        if self.history:
            lines.append(self.history)
        return "\n".join(lines)


# ---- cleaning ----------------------------------------------------------------------------------


def posix_path(path: str) -> str:
    cleaned = path.replace("\\", "/")
    cleaned = cleaned.split("/app/bundle/", 1)[-1]
    return cleaned[2:] if cleaned.startswith("./") else cleaned


def _clean(text: str, secrets: Sequence[str]) -> str:
    text = _SYNTAX_LOCATION.sub("", text)
    text = _WINDOWS_PATH.sub(lambda m: m.group(0).replace("\\", "/"), text)
    text = text.replace("/app/bundle/", "")
    for value in sorted({s for s in secrets if len(s) >= MIN_SECRET_LENGTH}, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    text = _OPAQUE_RUN.sub(lambda m: f"[opaque string of {len(m.group(0))} characters]", text)
    text = " ".join(text.split())
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 3].rstrip() + "..."
    return text


def _history_line(history: Sequence[tuple[int, Sequence[str]]]) -> str:
    if not history:
        return ""
    parts = [f"attempt {n}: {', '.join(codes) or 'none'}" for n, codes in history]
    line = "previous: " + "; ".join(parts)
    return line if len(line) <= MAX_HISTORY_CHARS else line[: MAX_HISTORY_CHARS - 3] + "..."


def build_feedback(
    attempt: int,
    items: Iterable[FeedbackItem],
    *,
    secrets: Sequence[str] = (),
    history: Sequence[tuple[int, Sequence[str]]] = (),
) -> Feedback:
    """Clean, deduplicate, sort and cap. The same inputs always give the same bytes."""
    cleaned = {
        FeedbackItem(
            i.stage,
            _clean(i.code, secrets),
            _clean(i.message, secrets),
            None if i.file is None else posix_path(i.file),
            i.line,
        )
        for i in items
    }
    ordered = sorted(
        cleaned,
        key=lambda i: (_STAGE_ORDER[i.stage], i.file or "", i.line or 0, i.code, i.message),
    )
    kept: list[FeedbackItem] = []
    used = 0
    for item in ordered:
        cost = len(item.render()) + 1
        if kept and (len(kept) >= MAX_ITEMS or used + cost > MAX_CHARS):
            break
        kept.append(item)
        used += cost
    return Feedback(attempt, tuple(kept), len(ordered) - len(kept), _history_line(history))


# ---- secret-like literals, mapped back to where they came from ---------------------------------


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _secret_literals_on(tree: ast.AST, line: int) -> list[ast.Constant]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.lineno <= line <= (node.end_lineno or node.lineno)
        and SECRET_PATTERN.search(node.value)
    ]


def _dict_key(parent: ast.AST, child: ast.AST) -> str | None:
    if isinstance(parent, ast.Dict):
        for key, value in zip(parent.keys, parent.values, strict=True):
            if value is child and isinstance(key, ast.Constant):
                return str(key.value)
    return None


def _case_origin(
    tree: ast.AST, literal: ast.Constant, base_cases: int
) -> tuple[str, str | None] | None:
    """Which case a literal in ``CASES`` belongs to, and its field; None outside the cases."""
    parents = _parents(tree)
    field: str | None = None
    side: str | None = None
    node: ast.AST = literal
    while node in parents:
        parent = parents[node]
        if field is None:
            field = _dict_key(parent, node)
        if isinstance(parent, ast.Dict) and isinstance(parents.get(parent), ast.List):
            side = _dict_key(parent, node)  # "record" or "expected"
        if isinstance(parent, ast.List) and isinstance(parents.get(parent), ast.AnnAssign):
            index = next((n for n, element in enumerate(parent.elts) if element is node), None)
            if index is None:
                return None
            who = f"edge record {index - base_cases}" if index >= base_cases else f"sample {index}"
            return (f"the computed value for {who}" if side == "expected" else who), field
        node = parent
    return None


def secret_literal_item(
    file: str, line: int, files: Mapping[str, str], base_cases: int
) -> FeedbackItem:
    """Describe a ``SECRET_LITERAL`` finding by origin and length, never by the literal."""
    rule = "has a run of 40 or more letters, digits or +/=_- characters, or a token-like prefix"
    message = f"a string literal {rule}; use a shorter or less token-like value"
    source = files.get(file)
    if source is not None:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None
        literals = _secret_literals_on(tree, line) if tree is not None else []
        if tree is not None and literals:
            length = len(str(literals[0].value))
            origin = _case_origin(tree, literals[0], base_cases) if file == CASES_PATH else None
            if origin is not None:
                where, field = origin
                named = f" (field '{field}')" if field else ""
                message = (
                    f"a string of {length} characters in {where}{named} {rule}; "
                    "use a shorter or less token-like value"
                )
            else:
                message = f"a string of {length} characters {rule}; use a shorter value"
    return FeedbackItem(Stage.AST, Rule.SECRET_LITERAL.value, message, file, line)


# ---- items from each source of failure ---------------------------------------------------------


def _gate_item(stage: Stage, finding: Finding) -> FeedbackItem:
    code, message = finding.rule.value, finding.message
    if finding.rule is Rule.LINT:
        match = _RUFF_CODE.match(message)
        if match:
            code, message = match.group(1), match.group(2)
    return FeedbackItem(stage, code, message, finding.file or None, finding.line or None)


def gate_items(result: GateResult, files: Mapping[str, str], base_cases: int) -> list[FeedbackItem]:
    stage = _GATE_STAGES[result.stage]
    return [
        secret_literal_item(f.file, f.line, files, base_cases)
        if f.rule is Rule.SECRET_LITERAL
        else _gate_item(stage, f)
        for f in result.findings
    ]


def guard_item(finding: GuardFinding) -> FeedbackItem:
    return FeedbackItem(Stage.GUARD, finding.guard, finding.message, finding.file, finding.line)


def validation_items(error: str) -> list[FeedbackItem]:
    """Items from a validator or schema error: ``a.b: message; c: message``."""
    items = []
    for piece in (p.strip() for p in error.split("; ")):
        if not piece:
            continue
        prefix, separator, rest = piece.partition(": ")
        if not separator:
            items.append(FeedbackItem(Stage.PROPOSAL, "INVALID", piece))
            continue
        code = "INVALID_JSON" if prefix == "<root>" else prefix.strip()
        items.append(FeedbackItem(Stage.PROPOSAL, code, rest))
    return items


def generated_tests_items(
    outcome: str, exit_code: int | None, result: Mapping[str, Any] | None
) -> list[FeedbackItem]:
    """Items from a run of the generated tests. Field names only: values are never echoed."""
    if outcome not in ("OK", "NONZERO"):
        return [
            FeedbackItem(Stage.TESTS, outcome, f"the generated tests did not complete: {outcome}")
        ]
    if result is None:
        message = f"the generated tests printed no result (exit code {exit_code})"
        return [FeedbackItem(Stage.TESTS, "NO_RESULT", message)]
    items = []
    for failure in result.get("failures", ()):
        case = failure.get("case")
        got, expected = failure.get("got", {}), failure.get("expected", {})
        if "__error__" in got:
            message = f"case {case}: the transform raised on field {got['__error__']}"
            items.append(FeedbackItem(Stage.TESTS, "TEST_ERROR", message))
            continue
        differing = sorted(k for k in set(got) | set(expected) if got.get(k) != expected.get(k))
        message = f"case {case}: output differs in fields {', '.join(differing[:6])}"
        items.append(FeedbackItem(Stage.TESTS, "TEST_MISMATCH", message))
    return items


def smoke_item(code: str, detail: str) -> FeedbackItem:
    return FeedbackItem(Stage.SMOKE, code, detail or "the smoke test failed")
