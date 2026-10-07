import json

import pytest

from morph_runtime.cli import run_module
from morph_runtime.errors import Category, RuntimeFailure
from morph_runtime.report import Outcome, RecordResult, RunReport


def lines(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    out: dict[str, object] = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return out


def test_a_module_that_fails_still_yields_one_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    def run(keys: tuple[str, ...]) -> RunReport:
        report = RunReport()
        report.add(RecordResult(keys[0], Outcome.CREATED))
        return report

    assert run_module(run) == 2  # no keys in the environment: the module fails and is reported
    out = lines(capsys)
    assert out["status"] == "FATAL"  # the module indexed an empty tuple, which is reported


def test_keys_come_from_the_environment(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MORPH_SOURCE_KEYS", "a,b,,c")
    seen: list[tuple[str, ...]] = []

    def run(keys: tuple[str, ...]) -> RunReport:
        seen.append(keys)
        return RunReport()

    assert run_module(run) == 0
    assert seen == [("a", "b", "c")] and lines(capsys)["status"] == "OK"


def test_a_runtime_failure_becomes_a_fatal_report(capsys: pytest.CaptureFixture[str]) -> None:
    def run(keys: tuple[str, ...]) -> RunReport:
        raise RuntimeFailure(Category.AUTH, "401 from target")

    assert run_module(run) == 2
    assert lines(capsys)["fatal"] == {"category": "AUTH", "detail": "401 from target"}


def test_any_other_exception_is_reported_without_a_traceback_or_its_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def run(keys: tuple[str, ...]) -> RunReport:
        raise ValueError("secret-looking detail")

    assert run_module(run) == 2
    out = capsys.readouterr().out
    assert "ValueError" in out and "secret-looking" not in out
