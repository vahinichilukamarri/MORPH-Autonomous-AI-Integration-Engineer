"""Results and report of the v0.4 code-generation evaluation. Pure: no DB, no Docker, no LLM.

Every number in the report is computed from saved unit results. Honesty rules (see
docs/codegen-eval.md): N is stated on every table, prompts and generator v1 are frozen before the
first real run, a changed version is reported as a separate labelled run, and blocked or failed
units are results, not omissions.
"""

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

CORRECTNESS = ("O1", "O2", "O3", "O4", "O5", "O6", "O7")
CATEGORIES = (*CORRECTNESS, "O8")
SHORT = {
    "crm_customer_to_support_user": "S1",
    "support_user_to_crm_customer": "S3",
    "crm_v2_to_support_v2": "S4",
}
CONDITIONS = ("D", "L1", "L2")
INPUT_SETS = ("approved", "as_proposed")
RUNNABLE = ("READY", "READY_PARTIAL")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMUse(_Model):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: int = 0
    invalid_outputs: int = 0
    edge_records_used: int | None = None


class GeneratedTests(_Model):
    outcome: str
    total: int | None = None
    passed: int | None = None


class UnitResult(_Model):
    scenario_id: str
    input_set: str
    condition: str
    provider: str = "none"
    model: str = "none"
    status: str
    blocked_reasons: list[str] = Field(default_factory=list)
    gate: dict[str, bool] = Field(default_factory=dict)
    gate_findings: list[str] = Field(default_factory=list)
    files: int = 0
    lines: int = 0
    generated_tests: GeneratedTests | None = None
    oracle: dict[str, tuple[int, int]] | None = None  # category -> (passed, total)
    oracle_failed_checks: list[str] = Field(default_factory=list)
    integration_correct: bool | None = None
    llm: LLMUse | None = None
    elapsed_s: float = 0.0
    test_only: bool = False

    @property
    def key(self) -> str:
        return f"{self.scenario_id}|{self.input_set}|{self.condition}|{self.provider}|{self.model}"


def load_results(path: Path) -> list[UnitResult]:
    if not path.is_file():
        return []
    latest: dict[str, UnitResult] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            result = UnitResult.model_validate_json(line)
            latest[result.key] = result
    return list(latest.values())


def append_result(path: Path, result: UnitResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(result.model_dump_json() + "\n")


def oracle_summary(
    checks: Iterable[dict[str, object]],
) -> tuple[dict[str, tuple[int, int]], list[str], bool]:
    """Per-category (passed, total), the failed check ids, and ``integration_correct``."""
    table: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    failed: list[str] = []
    for check in checks:
        cell = table[str(check["category"])]
        cell[1] += 1
        cell[0] += int(bool(check["passed"]))
        if not check["passed"]:
            failed.append(f"{check['category']}.{check['name']}")
    summary = {c: (v[0], v[1]) for c, v in table.items()}
    correct = not any(f.split(".", 1)[0] in CORRECTNESS for f in failed)
    return summary, failed, correct


# ---- report ------------------------------------------------------------------------------------


def _cell(value: tuple[int, int] | None) -> str:
    return "-" if value is None else f"{value[0]}/{value[1]}"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return [*lines, ""]


def _label(r: UnitResult) -> str:
    return f"{SHORT.get(r.scenario_id, r.scenario_id)} {r.input_set}"


def _order(r: UnitResult) -> tuple[int, str, int, str]:
    return (
        INPUT_SETS.index(r.input_set) if r.input_set in INPUT_SETS else 9,
        r.scenario_id,
        CONDITIONS.index(r.condition) if r.condition in CONDITIONS else 9,
        r.model,
    )


def render_report(results: Sequence[UnitResult], run_date: str, *, test_only: bool) -> str:
    rows = sorted(results, key=_order)
    out = ["# Code generation evaluation (v0.4)", ""]
    if test_only:
        out += [
            "> **TEST-ONLY OUTPUT. These results come from scripted or replayed providers and are",
            "> not a measurement of any model. Never commit this file as the report.**",
            "",
        ]
    models = sorted({f"{r.provider}/{r.model}" for r in rows if r.condition != "D"})
    out += [
        f"Run date: {run_date}. Units: **N = {len(rows)}** (scenario x input set x condition); "
        f"models: {', '.join(models) or 'none (condition D uses no model)'}.",
        "",
        "N is small. Differences of one unit are within noise; nothing here is statistically",
        "significant. Generator v1 and codegen prompts v1 were frozen (golden hashes) before the",
        "first real run; any later change is a separate, labelled run.",
        "",
        "Conditions: **D** deterministic compile and heuristic strategy, no model. **L1** D with a",
        "model-proposed, contract-validated sync strategy and edge-case inputs. **L2** D with a",
        "free-form model-written sync module behind the static gate. Input sets: **approved** =",
        "reference pipelines stored as human-approved mappings (isolates codegen from mapping",
        "quality); **as_proposed** = the real v0.3 proposals with the review gate and no human edit.",
        "",
        "## Outcome of each unit",
        "",
    ]
    out += _table(
        [
            "Unit",
            "Cond.",
            "Status",
            "Gate (ast/ruff/mypy)",
            "Gen. tests",
            "Oracle correct",
            "Notes",
        ],
        [
            [
                _label(r),
                r.condition,
                f"`{r.status}`",
                "/".join("pass" if r.gate.get(s) else "FAIL" for s in ("ast", "ruff", "mypy"))
                if r.gate
                else "-",
                _tests(r.generated_tests),
                {True: "yes", False: "**no**", None: "-"}[r.integration_correct],
                "; ".join(r.blocked_reasons)[:120] or "; ".join(r.gate_findings[:1])[:120],
            ]
            for r in rows
        ],
    )
    graded = [r for r in rows if r.oracle is not None]
    out += ["## Oracle results by category (passed/total checks)", ""]
    out += _table(
        ["Unit", "Cond.", *CATEGORIES],
        [
            [_label(r), r.condition, *[_cell((r.oracle or {}).get(c)) for c in CATEGORIES]]
            for r in graded
        ],
    )
    out += ["## By condition", ""]
    out += _table(
        [
            "Condition",
            "Units",
            "Reached READY",
            "Gate passed",
            "Oracle-graded",
            "integration_correct",
            "Blocked / LLM invalid",
        ],
        [
            _condition_row(c, [r for r in rows if r.condition == c])
            for c in CONDITIONS
            if any(r.condition == c for r in rows)
        ],
    )
    llm_rows = [r for r in rows if r.llm is not None and r.llm.calls]
    if llm_rows:
        out += ["## Model use (real counts)", ""]
        out += _table(
            [
                "Unit",
                "Cond.",
                "Calls",
                "Input tok.",
                "Output tok.",
                "Reasoning tok.",
                "Latency ms",
                "Invalid",
            ],
            [
                [
                    _label(r),
                    r.condition,
                    str(r.llm.calls),
                    str(r.llm.input_tokens),
                    str(r.llm.output_tokens),
                    str(r.llm.reasoning_tokens),
                    str(r.llm.latency_ms),
                    str(r.llm.invalid_outputs),
                ]
                for r in llm_rows
                if r.llm
            ],
        )
    failed = [(r, f) for r in graded for f in r.oracle_failed_checks]
    if failed:
        out += ["## Failed oracle checks", ""]
        out += _table(
            ["Unit", "Cond.", "Check"], [[_label(r), r.condition, f"`{f}`"] for r, f in failed]
        )
    out += [
        "## How to read this",
        "",
        "* `Gen. tests` are the deterministic generated tests (compiled code against the DSL on",
        "  sample records). They are reported apart from the oracle and never mixed into it.",
        "* `Oracle correct` means no failed check in O1 to O7; O8 (the review gate) is shown but is",
        "  not part of correctness.",
        "* A `BLOCKED_*` or `LLM_INVALID` status is a result: no code was produced and none was run.",
        "* `as_proposed` mappings are rebuilt, with no model call, from the responses saved by the",
        "  real v0.3 evaluation (the committed replay for S1, the local response store for the",
        "  others), validated with validator v1 as in that run. A unit whose required fields were",
        "  left in review is blocked exactly as a person would see it.",
        "* Every oracle check passed or failed on its own; results are never weighted or combined.",
        "",
    ]
    return "\n".join(out)


def _tests(t: GeneratedTests | None) -> str:
    if t is None:
        return "-"
    return t.outcome if t.total is None else f"{t.passed}/{t.total}"


def _condition_row(condition: str, rows: Sequence[UnitResult]) -> list[str]:
    ready = [r for r in rows if r.status in RUNNABLE]
    gated = [r for r in rows if r.gate and all(r.gate.values())]
    graded = [r for r in rows if r.integration_correct is not None]
    correct = [r for r in graded if r.integration_correct]
    blocked = [r for r in rows if r.status.startswith("BLOCKED") or r.status == "LLM_INVALID"]
    return [
        condition, str(len(rows)), f"{len(ready)}/{len(rows)}", f"{len(gated)}/{len(rows)}",
        str(len(graded)), f"{len(correct)}/{len(graded)}" if graded else "-", str(len(blocked)),
    ]  # fmt: skip


def results_json(results: Sequence[UnitResult]) -> str:
    return json.dumps([r.model_dump(mode="json") for r in results], indent=2)
