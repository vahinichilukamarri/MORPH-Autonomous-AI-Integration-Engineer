"""The repair-mode smoke test: does the bundle's entry point run to a well-formed report?

It exists because the generated tests only exercise ``integration.transform.to_target``: they never
import the sync module or the strategy, so without this check the model's contribution has no
runtime signal at all. The script runs *outside the bundle*: it is passed to the sandbox as a
``python -c`` argument, so the bundle (and its hash) is untouched and the sandbox needs no new mount
or privilege. It checks execution only: the entry point imports, runs to completion against
in-memory fake clients, and prints a report of the expected shape. It asserts nothing about what the
records contain, which outcome each got, or how many requests were made.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.codegen.sandbox import Outcome, SandboxLimits, SandboxRunner

SMOKE_TIMEOUT_S = 20.0

_SCRIPT = r"""
import contextlib
import io
import json
import os
import runpy

SHAPE = json.loads(__SHAPE__)
SRC, TGT = SHAPE["source"], SHAPE["target"]


def finish(code, detail=""):
    result = {"smoke": "ok" if code == "OK" else "fail", "code": code, "detail": str(detail)[:200]}
    print(json.dumps(result), flush=True)
    raise SystemExit(0)


try:
    from morph_runtime.errors import Category, RuntimeFailure
    import integration.clients as clients
    from tests_generated.cases import CASES

    RECORDS = [dict(case["record"]) for case in CASES]
except BaseException as error:
    finish("SETUP", f"{type(error).__name__}: {error}")


class Fake:
    def __init__(self, role):
        self.role = role
        self.requests_made = 0
        self.retries_made = 0
        self.writes = 0

    def _page(self, spec, items, query):
        page = int(query.get(spec["page_param"], 1))
        size = int(query.get(spec["size_param"], max(len(items), 1)))
        body = {spec["items_key"]: items[(page - 1) * size : page * size]}
        if spec.get("total_key"):
            body[spec["total_key"]] = len(items)
        return body

    def request(self, method, path, *, query=None, body=None):
        self.requests_made += 1
        if self.role == "source":
            if query:
                return self._page(SRC, RECORDS, query)
            key = path.rstrip("/").rsplit("/", 1)[-1]
            for record in RECORDS:
                if str(record.get(SRC["key_field"])) == key:
                    return dict(record)
            raise RuntimeFailure(Category.NOT_FOUND, "no such record", status=404)
        if method == "GET":
            if query:
                return self._page(TGT, [], query)
            raise RuntimeFailure(Category.NOT_FOUND, "no such record", status=404)
        self.writes += 1
        return {TGT["id_field"]: f"t{self.writes}", **(body or {})}


if SRC.get("mode") == "KEYS":
    os.environ["MORPH_SOURCE_KEYS"] = ",".join(str(r.get(SRC["key_field"])) for r in RECORDS)
source, target = Fake("source"), Fake("target")
clients.source_client = lambda: source
clients.target_client = lambda: target

buffer = io.StringIO()
exit_code = None
try:
    with contextlib.redirect_stdout(buffer):
        runpy.run_module("integration", run_name="__main__")
except SystemExit as stop:
    exit_code = stop.code
except (ImportError, SyntaxError) as error:
    finish("IMPORT_ERROR", f"{type(error).__name__}: {error}")
except BaseException as error:
    finish("RAISED", f"{type(error).__name__}: {error}")

lines = buffer.getvalue().strip().splitlines()
if not lines:
    finish("NO_REPORT", "the entry point printed nothing")
try:
    report = json.loads(lines[-1])
except ValueError:
    finish("NO_REPORT", "the last output line is not JSON")
if not (
    isinstance(report, dict)
    and isinstance(report.get("status"), str)
    and isinstance(report.get("records"), list)
    and isinstance(report.get("requests"), dict)
):
    finish("BAD_SHAPE", "the report lacks status, records or requests")
if report["status"] == "FATAL" or exit_code not in (0, 3):
    fatal = report.get("fatal") or {}
    finish("RUN_FATAL", f"status {report['status']}, exit {exit_code}: {fatal.get('detail', '')}")
finish("OK")
"""


@dataclass(frozen=True)
class SmokeResult:
    ok: bool
    code: str
    detail: str = ""
    infra: bool = False  # the sandbox never started: a pause, not a verdict on the code


def build_script(shape: Mapping[str, Any]) -> str:
    """The script with the contract's page-key names filled in (the bundle may not carry them)."""
    return _SCRIPT.replace("__SHAPE__", repr(json.dumps(shape, sort_keys=True)))


def make_smoke_runner() -> SandboxRunner:
    """The ordinary sandbox with a shorter time limit, so a non-terminating module is cut off."""
    return SandboxRunner(limits=SandboxLimits(timeout_s=SMOKE_TIMEOUT_S))


def parse_smoke_output(
    outcome: Outcome, exit_code: int | None, stdout: str, stderr: str
) -> SmokeResult:
    if outcome is Outcome.START_FAILED:
        return SmokeResult(False, "START_FAILED", "the sandbox did not start", infra=True)
    if outcome is Outcome.TIMEOUT:
        detail = f"the entry point did not finish within {SMOKE_TIMEOUT_S:.0f} seconds"
        return SmokeResult(False, "TIMEOUT", detail)
    if outcome not in (Outcome.OK, Outcome.NONZERO):
        return SmokeResult(False, outcome.value, f"the smoke test ended with {outcome.value}")
    lines = stdout.strip().splitlines()
    try:
        parsed = json.loads(lines[-1]) if lines else None
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict) or "code" not in parsed:
        last = stderr.strip().splitlines()[-1] if stderr.strip() else ""
        return SmokeResult(False, "CRASH", f"no smoke result (exit {exit_code}) {last}".strip())
    return SmokeResult(parsed["smoke"] == "ok", str(parsed["code"]), str(parsed.get("detail", "")))


def run_smoke(runner: SandboxRunner, bundle_dir: Path, shape: Mapping[str, Any]) -> SmokeResult:
    """Run the entry point of the bundle in ``bundle_dir`` against in-memory fake clients."""
    result = runner.run(
        bundle_dir, ["python", "-E", "-s", "-B", "-c", build_script(shape)], network=None
    )
    return parse_smoke_output(result.outcome, result.exit_code, result.stdout, result.stderr)
