"""The floor: rules in code that no policy file can weaken.

The policy decides what is allowed *above* the floor. The floor decides what is never allowed,
whatever the file, the role or an approval says. Each floor rule has a stable id, and a positive and
a negative control in the tests (a meta-test enforces both).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from app.policy.canonical import canonical_json
from app.policy.models import (
    ArgumentFacts,
    CallContext,
    DataClass,
    Effect,
    Environment,
    PolicyFile,
    Reason,
    RuleDecision,
)
from app.policy.toolspec import BY_NAME

FLOOR_RULES: dict[str, str] = {
    "F01-exec-target-mock": "A tool that runs code is denied when the target system is not a mock "
    "(unknown counts as production).",
    "F02-no-url": "No tool argument may be a URL; nothing fetches a remote address.",
    "F03-path-inside-root": "A file argument must stay inside the spec root: no absolute path, no "
    "'..', no symlink or junction that leaves it.",
    "F04-known-tools-only": "A tool that is not in the registry is denied.",
    "F05-no-forbidden-capability": "No tool may approve, override, change policy or attributes, "
    "grade, reach the bench, fetch, run a shell or read files or the environment.",
    "F06-result-size-cap": "A result larger than the session cap is not returned.",
    "F07-secret-redaction": "Known secret values and secret-shaped strings are removed from every "
    "result, audit payload and log line.",
    "F08-test-data-withheld": "Generated test data is returned only for synthetic data.",
    "F09-approval-never-overrides-deny": "An approval can turn needs-approval into allow, never a "
    "denial.",
    "F10-limit-ceilings": "A policy may not set a limit above the ceilings in code.",
}

MAX_MODEL_CALLS_CEILING = 50
MAX_SANDBOX_RUNS_CEILING = 200
MAX_RESULT_BYTES_CEILING = 1_000_000
TEST_DATA_PREFIXES = ("tests_generated/",)
EXECUTE_TOOLS = frozenset(
    name for name, spec in BY_NAME.items() if Effect.EXECUTE_SANDBOX in spec.effects
)

_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://|^//|\\\\")
_DRIVE = re.compile(r"^[A-Za-z]:")


# ---- arguments -----------------------------------------------------------------------------------


def strings_in(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from strings_in(item)
    elif isinstance(value, Sequence) and not isinstance(value, bytes):
        for item in value:
            yield from strings_in(item)


def looks_like_url(text: str) -> bool:
    return bool(_URL.search(text.strip()))


def path_escapes(root: Path, value: str) -> bool:
    """True if ``value`` could name anything outside ``root``: checked lexically, then resolved."""
    if "\x00" in value or not value.strip():
        return True
    if value.startswith(("/", "\\")) or _DRIVE.match(value):
        return True
    parts = PurePosixPath(value.replace("\\", "/")).parts + PureWindowsPath(value).parts
    if ".." in parts:
        return True
    resolved = (root / value).resolve()
    return not resolved.is_relative_to(root.resolve())


def inspect_arguments(arguments: Mapping[str, Any], *, spec_root: Path) -> ArgumentFacts:
    """Facts about the raw arguments. A file argument is the field named ``file``."""
    url = any(looks_like_url(s) for s in strings_in(arguments))
    file_value = arguments.get("file")
    escapes = isinstance(file_value, str) and path_escapes(spec_root, file_value)
    return ArgumentFacts(url_in_arguments=url, path_escapes_root=bool(escapes))


# ---- the context ---------------------------------------------------------------------------------


def floor_denials(ctx: CallContext) -> list[tuple[str, Reason]]:
    """Every floor rule that denies this context, in a fixed order."""
    hits: list[tuple[str, Reason]] = []
    if not ctx.tool_known:
        hits.append(("F04-known-tools-only", Reason.UNKNOWN_TOOL))
    if ctx.facts.url_in_arguments:
        hits.append(("F02-no-url", Reason.URL_DENIED))
    if ctx.facts.path_escapes_root:
        hits.append(("F03-path-inside-root", Reason.PATH_ESCAPE))
    runs_code = Effect.EXECUTE_SANDBOX in ctx.effects
    if runs_code and ctx.environment not in (None, Environment.MOCK):
        hits.append(("F01-exec-target-mock", Reason.EXEC_TARGET_NOT_MOCK))
    return hits


# ---- the policy file -----------------------------------------------------------------------------


def policy_floor_problems(policy: PolicyFile) -> list[str]:
    """Why this file would weaken the floor or name something that does not exist."""
    problems: list[str] = []
    limits = policy.session_limits
    ceilings = (
        ("max_model_calls", limits.max_model_calls, MAX_MODEL_CALLS_CEILING),
        ("max_sandbox_runs", limits.max_sandbox_runs, MAX_SANDBOX_RUNS_CEILING),
        ("max_result_bytes", limits.max_result_bytes, MAX_RESULT_BYTES_CEILING),
    )
    for name, value, ceiling in ceilings:
        if value > ceiling:
            problems.append(f"F10-limit-ceilings: {name} {value} is above the ceiling {ceiling}")
    for rule in policy.rules:
        for tool in rule.match.tools or ():
            if tool not in BY_NAME:
                problems.append(f"{rule.id}: names an unknown tool {tool!r}")
        if rule.decision is RuleDecision.DENY:
            continue
        non_mock = [e for e in rule.match.environments or () if e is not Environment.MOCK]
        executes = bool(
            EXECUTE_TOOLS & set(rule.match.tools or ())
            or Effect.EXECUTE_SANDBOX in (rule.match.effects or ())
        )
        if non_mock and executes:
            problems.append(
                f"F01-exec-target-mock: {rule.id} would allow code to run on "
                f"{[e.value for e in non_mock]}"
            )
    return problems


# ---- results -------------------------------------------------------------------------------------


def oversized(result: Any, max_bytes: int) -> bool:
    return len(canonical_json(result).encode("utf-8")) > max_bytes


def withhold_test_data(
    files: Sequence[Mapping[str, Any]], data_class: DataClass | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop generated test data unless the data is synthetic; returns (kept, withheld paths)."""
    if data_class is DataClass.SYNTHETIC:
        return [dict(f) for f in files], []
    kept, withheld = [], []
    for item in files:
        path = str(item.get("path", ""))
        if path.startswith(TEST_DATA_PREFIXES):
            withheld.append(path)
        else:
            kept.append(dict(item))
    return kept, withheld
