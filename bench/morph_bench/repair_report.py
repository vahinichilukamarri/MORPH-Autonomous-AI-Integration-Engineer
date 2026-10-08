# ruff: noqa: E501  (the report prose is written as long literal lines)
"""The v0.5 repair report, regenerated from committed files. Pure: no DB, no Docker, no model.

Model-dependent fields (status, reason, attempts, stages, feedback codes, guards, tokens, latency,
finish reasons) are taken from the committed replays: ``expected.json`` for the outcome and
``calls.jsonl`` for each reply's usage, found by the attempt's prompt hash (a fixed start's attempt 0
is found in the v0.4 replays and reported apart from the repair cost). Only the oracle numbers, the
exposure tags and the start mode come from the committed ``results.jsonl``. ``cross_check`` fails if
the two sources disagree, so a hand-edited results file cannot change a model-dependent number.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.repair.feedback import Stage

from morph_bench.codegen_eval import CATEGORIES, CORRECTNESS
from morph_bench.repair_eval import SHORT, RepairUnitResult

PROVENANCE_KEYS = (
    "harness_sha", "versions", "run_date", "model_reported_by_replies", "groq_limits",
    "post_run_edits", "files",
)  # fmt: skip
STAGE_ORDER = {stage.value: index for index, stage in enumerate(Stage)}
CONDITION_ORDER = ("L1R", "L2R")


# ---- loading and hashing -------------------------------------------------------------------------


def load_calls(path: Path) -> dict[str, dict[str, Any]]:
    """Recorded replies by prompt hash; an absent file is an empty store."""
    if not path.is_file():
        return {}
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return {r["prompt_hash"]: r for r in records}


def sha256_lf(path: Path) -> str:
    """SHA-256 of the file with LF line endings, as stored in git."""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def verify_provenance(directory: Path, *, repair: bool) -> list[str]:
    """Problems with a results directory's provenance; an undeclared edit is one of them."""
    path = directory / "provenance.json"
    if not path.is_file():
        return [f"{directory.name}: provenance.json is missing"]
    provenance = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if repair:
        problems += [
            f"{directory}: provenance lacks '{key}'"
            for key in PROVENANCE_KEYS
            if key not in provenance
        ]
    files = provenance.get("run", {}).get("files") or provenance.get("files") or {}
    files = files if isinstance(files, dict) else {}
    for name, entry in files.items():
        if not isinstance(entry, dict) or "sha256" not in entry:
            continue
        target = directory / name
        if not target.is_file():
            problems.append(f"{directory}: {name} is listed in the provenance but missing")
        elif sha256_lf(target) != entry["sha256"]:
            problems.append(f"{directory}: {name} differs from its recorded hash (undeclared edit)")
        elif repair and entry.get("sha256_as_exported", entry["sha256"]) != entry["sha256"]:
            if not provenance.get("post_run_edits"):
                problems.append(f"{directory}: {name} was edited after export but none is declared")
    return problems


# ---- cross-check ---------------------------------------------------------------------------------


def _unit_key(scenario_id: str, condition: str) -> tuple[str, str]:
    return scenario_id, condition


def cross_check(
    results: Sequence[RepairUnitResult],
    expected: Sequence[Mapping[str, Any]],
    calls: Mapping[str, Mapping[str, Any]],
    seed_calls: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Every disagreement between the committed results and the replays, as text."""
    problems: list[str] = []
    by_result = {_unit_key(r.scenario_id, r.condition): r for r in results}
    by_expected = {_unit_key(e["scenario_id"], e["condition"]): e for e in expected}
    if set(by_result) != set(by_expected):
        return [f"units differ: results {sorted(by_result)} against replays {sorted(by_expected)}"]
    for key, unit in sorted(by_result.items()):
        want = by_expected[key]
        tag = f"{SHORT.get(key[0], key[0])} {key[1]}"
        for field in ("status", "reason", "model_turns", "start_mode"):
            if getattr(unit, field) != want[field]:
                problems.append(
                    f"{tag}: {field} is {getattr(unit, field)!r}, replays say {want[field]!r}"
                )
        if len(unit.attempts) != len(want["attempts"]):
            problems.append(
                f"{tag}: {len(unit.attempts)} attempts, replays say {len(want['attempts'])}"
            )
            continue
        for got, exp in zip(unit.attempts, want["attempts"], strict=True):
            shape = {
                "attempt": got.attempt, "failed_stage": got.failed_stage,
                "output_hash": got.output_hash, "feedback_codes": got.feedback_codes,
                "guard_enforced": got.guard_enforced, "guard_shadow": got.guard_shadow,
            }  # fmt: skip
            if shape != exp:
                problems.append(f"{tag}: attempt {got.attempt} differs from the replays")
            record = (calls if got.source == "network" else seed_calls).get(got.prompt_hash or "")
            if record is None:
                problems.append(f"{tag}: attempt {got.attempt} has no recorded reply")
                continue
            if got.source != "network":
                continue  # a seeded reply's usage is the v0.4 one: shown, not compared
            usage = (
                got.input_tokens, got.output_tokens, got.reasoning_tokens, got.total_tokens,
                got.latency_ms, got.finish_reason,
            )  # fmt: skip
            replayed = tuple(
                record.get(k) for k in (
                    "input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
                    "latency_ms", "finish_reason",
                )
            )  # fmt: skip
            if usage != replayed:
                problems.append(
                    f"{tag}: attempt {got.attempt} usage differs from its recorded reply"
                )
    return problems


# ---- the report ----------------------------------------------------------------------------------


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return [*lines, ""]


def _tag(unit: RepairUnitResult) -> str:
    return f"{SHORT.get(unit.scenario_id, unit.scenario_id)} {unit.condition}"


def _order(unit: RepairUnitResult) -> tuple[int, str]:
    cond = CONDITION_ORDER.index(unit.condition) if unit.condition in CONDITION_ORDER else 9
    return cond, unit.scenario_id


def _num(value: Any) -> str:
    return "not recorded" if value is None else str(value)


def _repairs_to_ready(unit: RepairUnitResult) -> str:
    if unit.status != "READY":
        return "-"
    return str(max(a.attempt for a in unit.attempts))


def _outcome(unit: RepairUnitResult) -> str:
    if unit.status == "INFRA_STOPPED":
        return f"not run ({unit.reason})"
    if unit.status == "READY" and unit.integration_correct is False:
        return "READY, **gate-passing, incorrect**"
    if unit.status == "READY" and unit.integration_correct is None:
        return "READY (not graded)"
    if unit.status == "READY":
        return "READY, oracle correct"
    return f"`{unit.status}` ({unit.reason})"


def _transitions(unit: RepairUnitResult) -> list[str]:
    """For each failed attempt, what the next attempt did with the feedback it was given."""
    lines = []
    for before, after in zip(unit.attempts, unit.attempts[1:], strict=False):
        if before.failed_stage is None:
            continue
        cleared = sorted(set(before.feedback_codes) - set(after.feedback_codes))
        kept = sorted(set(before.feedback_codes) & set(after.feedback_codes))
        new = sorted(set(after.feedback_codes) - set(before.feedback_codes))
        if after.failed_stage is None:
            verdict = f"READY; the {before.failed_stage} feedback resolved it"
        elif STAGE_ORDER.get(after.failed_stage, -1) > STAGE_ORDER.get(before.failed_stage, 99):
            verdict = f"passed {before.failed_stage}, failed later at {after.failed_stage}"
        elif after.failed_stage == before.failed_stage:
            verdict = f"still failing at {before.failed_stage}"
        else:
            verdict = f"went back from {before.failed_stage} to {after.failed_stage}"
        lines.append(
            f"| {_tag(unit)} | {before.attempt} to {after.attempt} | {verdict} | "
            f"{', '.join(cleared) or '-'} | {', '.join(kept) or '-'} | {', '.join(new) or '-'} |"
        )
    return lines


def render_repair_report(
    results: Sequence[RepairUnitResult],
    expected: Sequence[Mapping[str, Any]],
    calls: Mapping[str, Mapping[str, Any]],
    seed_calls: Mapping[str, Mapping[str, Any]],
    provenance: Mapping[str, Any],
    *,
    phase: str,
) -> str:
    units = sorted(results, key=_order)
    by_expected = {_unit_key(e["scenario_id"], e["condition"]): e for e in expected}

    def record(unit: RepairUnitResult, attempt_no: int) -> tuple[str, Mapping[str, Any] | None]:
        attempt = next(a for a in unit.attempts if a.attempt == attempt_no)
        store = calls if attempt.source == "network" else seed_calls
        return attempt.source, store.get(attempt.prompt_hash or "")

    run = provenance.get("run_date", "unknown")
    model = provenance.get("model_reported_by_replies", "unknown")
    out = [
        f"# Repair evaluation (v0.5, {phase}-start)",
        "",
        f"Run date: {run}. Model reported by the replies: `{model}`. Units: **N = {len(units)}** "
        "(scenario x condition, approved inputs), one run each.",
        "",
        "N is 1 per unit and 3 units per condition: nothing here is statistically significant and a",
        "difference of one unit is noise. The experiment was frozen and pre-registered before the run",
        "(`docs/plans/v0.5-fixed-start-preregistration.md`); failures are results and nothing was tuned.",
        "Model-dependent values below are derived from the committed replays; only the oracle results,",
        "start mode and exposure tags come from the committed results file. `render_reports --check`",
        "fails if the two disagree.",
        "",
        "Conditions: **L1R** the model proposes the sync strategy and edge records, repaired; **L2R**",
        "the model writes the sync module, repaired. Fixed start: attempt 0 is the recorded v0.4 reply",
        "(no call), so the repair effect is measured from the same starting failure. Reference: condition",
        "**D** (no model) in `codegen-eval.md`.",
        "",
        "## Outcome of each unit",
        "",
    ]
    out += _table(
        ["Unit", "Exposure tag", "Outcome", "Repairs used", "Model turns", "Real calls"],
        [
            [_tag(u), f"`{u.exposure}`", _outcome(u), _repairs_to_ready(u), str(u.model_turns),
             str(u.real_calls)]
            for u in units
        ],
    )  # fmt: skip
    out += ["## By condition", ""]
    rows = []
    for cond in CONDITION_ORDER:
        group = [u for u in units if u.condition == cond]
        if not group:
            continue
        ready = [u for u in group if u.status == "READY"]
        graded = [u for u in ready if u.integration_correct is not None]
        correct = [u for u in graded if u.integration_correct]
        hrr = [u for u in group if u.status == "HUMAN_REVIEW_REQUIRED"]
        stopped = [u for u in group if u.status == "INFRA_STOPPED"]
        runnable = len(group) - len(stopped)
        measurable = (
            "yes" if group and runnable / len(group) >= 2 / 3 else "**not measurable at this tier**"
        )
        rows.append([
            cond, str(len(group)), f"{len(ready)}/{len(group)}",
            f"{len(correct)}/{len(graded)}" if graded else "-",
            f"{len(ready) - len(correct)}" if graded else "-",
            f"{len(hrr)}/{len(group)}", f"{len(stopped)}/{len(group)}", measurable,
        ])  # fmt: skip
    out += _table(
        ["Condition", "Units", "Reached READY", "Oracle correct (of graded)",
         "READY but incorrect", "Human review required", "Not run", "Measurable"],
        rows,
    )  # fmt: skip
    out += [
        "READY means the proposal passed the static gate, the generated tests and the smoke test. It is",
        "not correctness; the oracle decides that. A READY unit that fails the oracle is reported as",
        "READY-but-incorrect and is not a success. Units not run stay in every denominator.",
        "",
        "## Oracle results on READY units (passed/total checks)",
        "",
    ]
    graded_units = [u for u in units if u.oracle is not None]
    if graded_units:
        out += _table(
            ["Unit", *CATEGORIES],
            [
                [_tag(u), *[
                    "-" if c not in (u.oracle or {}) else f"{u.oracle[c][0]}/{u.oracle[c][1]}"
                    for c in CATEGORIES
                ]]
                for u in graded_units if u.oracle is not None
            ],
        )  # fmt: skip
        failed = [(u, f) for u in graded_units for f in u.oracle_failed_checks]
        if failed:
            out += ["Failed checks (O1 to O7 decide correctness; O8 is shown, not counted):", ""]
            out += _table(
                ["Unit", "Check", "Counts against correctness"],
                [[_tag(u), f"`{f}`", "yes" if f.split(".", 1)[0] in CORRECTNESS else "no"]
                 for u, f in failed],
            )  # fmt: skip
    else:
        out += ["No unit reached READY, so the oracle graded nothing.", ""]
    out += ["## Attempts", ""]
    attempt_rows = []
    for u in units:
        want = by_expected[_unit_key(u.scenario_id, u.condition)]
        for exp, got in zip(want["attempts"], u.attempts, strict=True):
            source, rec = record(u, got.attempt)
            attempt_rows.append([
                _tag(u), str(exp["attempt"]), "seeded (v0.4 reply)" if source != "network" else "model",
                exp["failed_stage"] or "READY", ", ".join(exp["feedback_codes"]) or "-",
                ", ".join(exp["guard_enforced"]) or "-", ", ".join(exp["guard_shadow"]) or "-",
                _num(rec.get("finish_reason") if rec else None),
            ])  # fmt: skip
    out += _table(
        ["Unit", "Attempt", "Source", "Failed stage", "Feedback codes", "Guards tripped (enforced)",
         "Shadow guards", "Finish reason"],
        attempt_rows,
    )  # fmt: skip
    out += ["## Which feedback resolved what", ""]
    transitions = [line for u in units for line in _transitions(u)]
    if transitions:
        out += [
            "| Unit | Attempts | Result | Codes cleared | Codes still present | New codes |",
            "|---|---|---|---|---|---|",
            *transitions,
            "",
        ]
    else:
        out += ["No unit had a failed attempt followed by another attempt.", ""]
    out += _usage_sections(units, record, by_expected)
    out += [
        "## How to read this",
        "",
        "* The feedback stage order is PROPOSAL, GUARD, AST, RUFF, MYPY, TESTS, SMOKE. The gate stops at",
        "  the first failing stage, so later stages are not seen until earlier ones pass.",
        "* L1R S3 was pre-registered as likely READY-but-incorrect (its segment-overwrite flaw has no",
        "  runtime signal). L2R S1 measures compliance with the repair prompt's instruction to follow a",
        "  check over a conflicting hint, not discovery of the conflict.",
        "* `SIZE_REJECTED` and size-stopped units are infrastructure results, not model results.",
        "* The reading of these numbers is in `repair-eval-findings.md`.",
        "",
    ]
    return "\n".join(out)


def _usage_sections(
    units: Sequence[RepairUnitResult],
    record: Any,
    by_expected: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[str]:
    repair_rows: list[list[str]] = []
    seeded_rows: list[list[str]] = []
    totals: dict[str, int] = defaultdict(int)
    for u in units:
        calls_in_unit = 0
        sums = {"in": 0, "out": 0, "reason": 0, "total": 0, "latency": 0}
        for attempt in u.attempts:
            source, rec = record(u, attempt.attempt)
            if rec is None:
                continue
            if source != "network":
                seeded_rows.append([
                    _tag(u), str(attempt.attempt), _num(rec.get("input_tokens")),
                    _num(rec.get("output_tokens")), _num(rec.get("reasoning_tokens")),
                    _num(rec.get("total_tokens")),
                ])  # fmt: skip
                continue
            calls_in_unit += 1
            for key, field in (("in", "input_tokens"), ("out", "output_tokens"),
                               ("reason", "reasoning_tokens"), ("total", "total_tokens"),
                               ("latency", "latency_ms")):  # fmt: skip
                sums[key] += int(rec.get(field) or 0)
            total = rec.get("total_tokens")
            base = int(rec.get("input_tokens") or 0) + int(rec.get("output_tokens") or 0)
            totals["calls"] += 1
            if total == base:
                totals["total_eq_in_out"] += 1
            if total == base + int(rec.get("reasoning_tokens") or 0):
                totals["total_eq_in_out_reason"] += 1
        if calls_in_unit:
            repair_rows.append([
                _tag(u), str(calls_in_unit), str(sums["in"]), str(sums["out"]), str(sums["reason"]),
                str(sums["total"]), str(sums["latency"]),
            ])  # fmt: skip
    out = ["## Tokens and latency of the repair calls (real counts)", ""]
    if repair_rows:
        out += _table(
            [
                "Unit",
                "Calls",
                "Input tok.",
                "Output tok.",
                "Reasoning tok.",
                "Total tok. (provider)",
                "Latency ms",
            ],
            repair_rows,
        )
        out += [
            "Provider totals: `total_tokens` equals input + output on "
            f"{totals['total_eq_in_out']} of {totals['calls']} calls and input + output + reasoning on "
            f"{totals['total_eq_in_out_reason']} of {totals['calls']}.",
            "",
        ]
    else:
        out += ["No repair call was made.", ""]
    out += ["## Seeded attempt 0 (the recorded v0.4 replies, not part of the repair cost)", ""]
    out += (
        _table(
            ["Unit", "Attempt", "Input tok.", "Output tok.", "Reasoning tok.", "Total tok."],
            seeded_rows,
        )
        if seeded_rows
        else ["None.", ""]
    )
    return out
