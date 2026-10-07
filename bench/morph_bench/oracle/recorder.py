"""Records every oracle check as a pass or fail with its category, for scoring and reports.

Scoring: each check is one test id (scenario, category, name) that is passed or failed. There is
no weighting and no composite score. ``integration_correct`` holds when no check in categories
O1 to O7 failed.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType

RESULTS_PATH = Path(__file__).resolve().parents[2] / ".cache" / "oracle-results.json"
CORRECTNESS_CATEGORIES = ("O1", "O2", "O3", "O4", "O5", "O6", "O7")
ALL_CATEGORIES = (*CORRECTNESS_CATEGORIES, "O8")


@dataclass(frozen=True)
class CheckResult:
    scenario: str
    category: str
    name: str
    passed: bool
    detail: str


RESULTS: list[CheckResult] = []


class Checks:
    """Collect the checks of one test; fail the test at the end listing every failed check."""

    def __init__(self, scenario: str, category: str) -> None:
        self.scenario = scenario
        self.category = category
        self.failed: list[CheckResult] = []

    def ok(self, name: str, condition: bool, detail: str = "") -> bool:
        result = CheckResult(self.scenario, self.category, name, bool(condition), detail)
        RESULTS.append(result)
        if not result.passed:
            self.failed.append(result)
        return result.passed

    def __enter__(self) -> "Checks":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None and self.failed:
            lines = [f"{c.category}.{c.name} [{c.scenario}]: {c.detail}" for c in self.failed]
            raise AssertionError("oracle checks failed:\n" + "\n".join(lines))


def summarise(results: list[CheckResult]) -> dict[str, dict[str, dict[str, int]]]:
    """scenario -> category -> {passed, total}."""
    table: dict[str, dict[str, dict[str, int]]] = {}
    for r in results:
        cell = table.setdefault(r.scenario, {}).setdefault(r.category, {"passed": 0, "total": 0})
        cell["total"] += 1
        cell["passed"] += int(r.passed)
    return table


def integration_correct(results: list[CheckResult], scenario: str) -> bool:
    return not any(
        not r.passed
        for r in results
        if r.scenario == scenario and r.category in CORRECTNESS_CATEGORIES
    )


def write_results(path: Path = RESULTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(r) for r in RESULTS], indent=2), encoding="utf-8")
