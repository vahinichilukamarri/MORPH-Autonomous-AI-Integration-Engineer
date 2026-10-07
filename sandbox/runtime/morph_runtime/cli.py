"""Entry point used by every generated integration: ``python -m integration``."""

from collections.abc import Callable

from morph_runtime.config import env_optional
from morph_runtime.errors import RuntimeFailure
from morph_runtime.http import HttpClient
from morph_runtime.ops import JsonScalar, Record
from morph_runtime.report import RunReport
from morph_runtime.sync import DEFAULT_RUN_BUDGET_S, Strategy, SyncEngine


def main(
    strategy: Strategy,
    source_factory: Callable[[], HttpClient],
    target_factory: Callable[[], HttpClient],
    to_target: Callable[[Record], dict[str, JsonScalar]],
) -> int:
    """Run one sync and print the report as a single JSON line. Returns the exit code."""
    try:
        source = source_factory()
        target = target_factory()
        keys = tuple(k for k in env_optional("MORPH_SOURCE_KEYS", "").split(",") if k)
        budget = float(env_optional("MORPH_RUN_BUDGET_S", str(DEFAULT_RUN_BUDGET_S)))
    except RuntimeFailure as failure:
        report = RunReport()
        report.fail_run(failure.category, failure.detail)
    else:
        report = SyncEngine(strategy, source, target, to_target, keys=keys, budget_s=budget).run()
    print(report.to_json(), flush=True)
    return report.exit_code
