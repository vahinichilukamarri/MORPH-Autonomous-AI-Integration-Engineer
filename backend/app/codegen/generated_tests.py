"""Deterministic generated tests, kept apart from the oracle suite.

For every sample record the DSL interpreter computes what the approved mappings must produce, and
the generated test asserts that the compiled ``to_target`` returns exactly that inside the sandbox.
These tests prove the compilation preserved the mapping; they say nothing about whether the
integration is correct against the live systems (that is the oracle's job, and it is reported
separately).
"""

import pprint
from collections.abc import Sequence
from typing import Any

from app.codegen.inputs import MappedField
from app.mapping.transform import JsonScalar, TransformError, execute

TESTS_PACKAGE = "tests_generated"

RUNNER = '''"""Generated tests: python -m tests_generated. Generated; do not edit."""

import json
from typing import Any

from morph_runtime.ops import FieldTransformError

from integration.transform import to_target
from tests_generated.cases import CASES


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def run() -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    for index, case in enumerate(CASES):
        try:
            got: dict[str, Any] = dict(to_target(case["record"]))
        except FieldTransformError as error:
            got = {"__error__": error.field}
        if canonical(got) != canonical(case["expected"]):
            failures.append({"case": index, "expected": case["expected"], "got": got})
    return {"total": len(CASES), "passed": len(CASES) - len(failures), "failures": failures}


RESULT = run()
print(json.dumps(RESULT), flush=True)
raise SystemExit(0 if not RESULT["failures"] else 1)
'''


def expected_for(included: Sequence[MappedField], record: dict[str, JsonScalar]) -> dict[str, Any]:
    """What ``to_target`` must return for ``record``, or the first field that must fail."""
    out: dict[str, Any] = {}
    for mapped in included:
        assert mapped.transformation is not None
        try:
            out[mapped.target_field] = execute(mapped.transformation, record)
        except TransformError:
            return {"__error__": mapped.target_field}
    return out


def render_tests(
    included: Sequence[MappedField], samples: Sequence[dict[str, JsonScalar]]
) -> dict[str, str]:
    cases = [{"record": dict(r), "expected": expected_for(included, r)} for r in samples]
    body = pprint.pformat(cases, width=96, sort_dicts=False)
    return {
        f"{TESTS_PACKAGE}/__init__.py": "",
        f"{TESTS_PACKAGE}/__main__.py": RUNNER,
        f"{TESTS_PACKAGE}/cases.py": (
            '"""Expected outputs of the approved mappings on the sample records. Generated."""\n\n'
            "from typing import Any\n\n"
            f"CASES: list[dict[str, Any]] = {body}\n"
        ),
    }
