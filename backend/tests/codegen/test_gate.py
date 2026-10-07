"""The AST gate must reject every hostile or sloppy construct and accept benign generated code."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.codegen.compiler import FieldSpec, compile_transform_module
from app.codegen.gate import Rule, check_ast
from app.mapping.transform import Cast, Copy, MapEnum, Transformation

HOSTILE: list[tuple[str, str, Rule]] = [
    ("eval", "x = eval('1+1')", Rule.BANNED_NAME),
    ("exec", "exec('import os')", Rule.BANNED_NAME),
    ("compile", "c = compile('1', 'f', 'eval')", Rule.BANNED_NAME),
    ("open", "f = open('/etc/passwd')", Rule.BANNED_NAME),
    ("dunder import call", "m = __import__('os')", Rule.BANNED_NAME),
    ("getattr builtins trick", "f = getattr(__builtins__, 'ev' + 'al')", Rule.BANNED_NAME),
    ("setattr", "setattr(object, 'a', 1)", Rule.BANNED_NAME),
    ("globals", "g = globals()", Rule.BANNED_NAME),
    ("vars", "v = vars()", Rule.BANNED_NAME),
    ("input", "x = input()", Rule.BANNED_NAME),
    ("breakpoint", "breakpoint()", Rule.BANNED_NAME),
    ("os import", "import os", Rule.IMPORT_NOT_ALLOWED),
    ("subprocess import", "import subprocess", Rule.IMPORT_NOT_ALLOWED),
    ("from os import", "from os import system", Rule.IMPORT_NOT_ALLOWED),
    ("importlib", "import importlib", Rule.IMPORT_NOT_ALLOWED),
    ("socket", "import socket", Rule.IMPORT_NOT_ALLOWED),
    ("urllib direct", "import urllib.request", Rule.IMPORT_NOT_ALLOWED),
    ("sys", "from sys import modules", Rule.IMPORT_NOT_ALLOWED),
    ("builtins module", "import builtins", Rule.IMPORT_NOT_ALLOWED),
    ("ctypes", "import ctypes", Rule.IMPORT_NOT_ALLOWED),
    ("lookalike prefix", "import morph_runtime_evil", Rule.IMPORT_NOT_ALLOWED),
    ("star import", "from morph_runtime.ops import *", Rule.IMPORT_STAR),
    ("class escape", "x = ().__class__.__bases__[0].__subclasses__()", Rule.DUNDER_ACCESS),
    ("dunder dict", "x = int.__dict__", Rule.DUNDER_ACCESS),
    ("dunder globals", "def f(): pass\nx = f.__globals__", Rule.DUNDER_ACCESS),
    ("dunder name use", "x = __builtins__", Rule.DUNDER_ACCESS),
    ("frame walk", "def g():\n    yield 1\nx = g().gi_frame.f_back", Rule.BANNED_ATTRIBUTE),
    ("lambda calling eval", "f = lambda s: eval(s)", Rule.BANNED_NAME),
    ("comprehension calling exec", "[exec(s) for s in ['1']]", Rule.BANNED_NAME),
    (
        "nested function calling open",
        "def a():\n    def b():\n        return open('x')\n    return b",
        Rule.BANNED_NAME,
    ),
    ("fstring with eval", "x = f'{eval(\"1\")}'", Rule.BANNED_NAME),
    ("decorator eval", "@eval\ndef f(): pass", Rule.BANNED_DECORATOR),
    ("custom decorator", "def d(f): return f\n@d\ndef g(): pass", Rule.BANNED_DECORATOR),
    ("class decorator", "def d(c): return c\n@d\nclass A: pass", Rule.BANNED_DECORATOR),
    ("argument named like a builtin", "def f(eval): return eval", Rule.BANNED_NAME),
    ("url literal", "U = 'http://example.org/x'", Rule.URL_LITERAL),
    ("url in fstring", "def f(x): return f'https://evil.test/{x}'", Rule.URL_LITERAL),
    ("scheme-less www", "U = 'www.evil.test'", Rule.URL_LITERAL),
    ("token literal", "T = 'gsk_abcdefghijklmnopqrstuv'", Rule.SECRET_LITERAL),
    ("bearer literal", "H = 'Bearer abcdefgh12345678'", Rule.SECRET_LITERAL),
    (
        "long opaque string",
        "K = 'A' * 1 + 'x' * 0 + 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP1234'",
        Rule.SECRET_LITERAL,
    ),
    ("async def", "async def f(): pass", Rule.ASYNC),
    ("await", "def f(x):\n    return x\nasync def g(): await f(1)", Rule.ASYNC),
    ("global stmt", "x = 1\ndef f():\n    global x\n    x = 2", Rule.GLOBAL_STATEMENT),
    ("syntax error", "def (:", Rule.SYNTAX),
    ("null byte", "x = 1\x00", Rule.SYNTAX),
]

BENIGN = {
    "integration/transform.py": compile_transform_module(
        [
            FieldSpec("customerId", Transformation(steps=(Copy(field="a"), Cast(to="str")))),
            FieldSpec("s", Transformation(steps=(Copy(field="b"), MapEnum(mapping={"x": "y"})))),
        ]
    ),
    "integration/sync.py": (
        "from __future__ import annotations\n"
        "import json\nimport re\nfrom dataclasses import dataclass\nfrom enum import StrEnum\n"
        "from typing import Any\n"
        "from morph_runtime.http import HttpClient\n"
        "from morph_runtime.paging import paginate\n"
        "from integration.transform import to_target\n"
        "from . import transform\n"
        "from .transform import TARGET_FIELDS\n\n"
        "@dataclass(frozen=True)\nclass Plan:\n    key: str\n\n"
        "def run(client: HttpClient, rows: list[dict[str, Any]]) -> list[str]:\n"
        "    return [json.dumps(to_target(r)) for r in rows if re.match(r'^C-', str(r))]\n"
    ),
    "integration/__init__.py": "",
    "integration/__main__.py": "from integration import sync\n",
}


@pytest.mark.parametrize(("name", "source", "rule"), HOSTILE, ids=[h[0] for h in HOSTILE])
def test_hostile_construct_is_rejected(name: str, source: str, rule: Rule) -> None:
    result = check_ast({"integration/x.py": source})
    assert not result.passed
    assert rule in {f.rule for f in result.findings}, [f.as_dict() for f in result.findings]


def test_benign_bundle_passes() -> None:
    result = check_ast(BENIGN)
    assert result.passed, [f.as_dict() for f in result.findings]


def test_import_of_a_missing_bundle_module_is_rejected() -> None:
    result = check_ast(
        {"integration/a.py": "from integration import ghost\nfrom .ghost import x\n"}
    )
    assert [f.rule for f in result.findings] == [Rule.IMPORT_NOT_ALLOWED] * 2


@pytest.mark.parametrize(
    "path",
    ["/abs/x.py", "../x.py", "integration/../../x.py", "a\\b.py", "integration/x.sh", "data.json"],
)
def test_bad_paths_are_rejected(path: str) -> None:
    result = check_ast({path: "x = 1"})
    assert [f.rule for f in result.findings] == [Rule.PATH]


def test_limits() -> None:
    assert Rule.LIMIT in {
        f.rule for f in check_ast({f"integration/f{i}.py": "" for i in range(30)}).findings
    }
    assert Rule.LIMIT in {
        f.rule for f in check_ast({"integration/big.py": "x = 1\n" * 2000}).findings
    }


def test_every_finding_names_file_and_line() -> None:
    result = check_ast({"integration/x.py": "a = 1\nb = eval('1')\n"})
    finding = result.findings[0]
    assert (finding.file, finding.line, finding.rule) == ("integration/x.py", 2, Rule.BANNED_NAME)


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=200))
def test_gate_never_crashes_on_arbitrary_text(source: str) -> None:
    result = check_ast({"integration/x.py": source})
    assert isinstance(result.passed, bool)
