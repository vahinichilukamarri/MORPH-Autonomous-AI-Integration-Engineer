"""Records, aggregation and the markdown report of the mapping evaluation. Pure: no DB, no LLM.

Every number in the report is computed from the run records it is given; nothing is typed in.
Runs made with different prompt or confidence versions form separate labelled sets and are never
merged, so a later change cannot silently replace an earlier result.
"""

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from morph_bench.grader import FieldGrade

CONFIG_NAMES = {
    "A": "A: retrieval only (top-1 source, COPY, no LLM)",
    "B": "B: LLM, full schema",
    "C": "C: LLM + retrieval (RAG)",
}
DEFAULT_OUTPUT = Path("docs/mapping-eval.md")
BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.0001))
LLM_CONFIGS = ("B", "C")


@dataclass(frozen=True)
class FieldRecord:
    grade: FieldGrade
    proposed: str  # human readable: type, sources, pipeline ops
    expected: str
    review_status: str
    validation_status: str
    lossy_codes: tuple[str, ...] = ()  # INFORMATION_LOSS_* warnings the validator raised


@dataclass(frozen=True)
class Metrics:
    fields: int
    llm_calls: int = 0
    invalid_output_fields: int = 0
    reasked_fields: int = 0
    needs_review: int = 0
    unresolved: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: int = 0


@dataclass(frozen=True)
class RunRecord:
    label: str  # "<prompt version>/<confidence version>"
    scenario_id: str
    config: str  # A, B or C
    run_index: int
    provider: str
    model: str
    fields: tuple[FieldRecord, ...]
    metrics: Metrics

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @staticmethod
    def from_json(text: str) -> "RunRecord":
        raw = json.loads(text)
        fields = tuple(
            FieldRecord(
                grade=FieldGrade(**f["grade"]),
                proposed=f["proposed"],
                expected=f["expected"],
                review_status=f["review_status"],
                validation_status=f["validation_status"],
                lossy_codes=tuple(f.get("lossy_codes", ())),
            )
            for f in raw["fields"]
        )
        return RunRecord(**{**raw, "fields": fields, "metrics": Metrics(**raw["metrics"])})


@dataclass
class Limits:
    """Rate-limit observations from the real provider, saved next to the call store."""

    last_headers: dict[str, str] = field(default_factory=dict)
    rate_limit_waits: list[float] = field(default_factory=list)  # Retry-After seconds, each wait
    exhausted: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def load(path: Path) -> "Limits":
        if not path.is_file():
            return Limits()
        return Limits(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True)
class ReportMeta:
    run_date: str
    test_only: bool
    n_runs_requested: int
    limits: Limits = field(default_factory=Limits)


# ---- aggregation -------------------------------------------------------------------------------


def _fields(records: Iterable[RunRecord]) -> list[FieldGrade]:
    return [f.grade for r in records for f in r.fields]


def fraction(numerator: int, denominator: int) -> str:
    if not denominator:
        return "n/a"
    return f"{numerator}/{denominator} ({100 * numerator / denominator:.1f}%)"


def rate(records: Iterable[RunRecord], attribute: str) -> str:
    values = [getattr(g, attribute) for g in _fields(records) if getattr(g, attribute) is not None]
    return fraction(sum(1 for v in values if v), len(values))


def correct_counts(records: Sequence[RunRecord]) -> list[tuple[int, int]]:
    """(fully correct, fields) per run, in run order."""
    return [
        (sum(1 for f in r.fields if f.grade.fully_correct), len(r.fields))
        for r in sorted(records, key=lambda r: r.run_index)
    ]


def _select(
    records: Sequence[RunRecord], *, scenario: str | None = None, config: str | None = None
) -> list[RunRecord]:
    return [
        r
        for r in records
        if (scenario is None or r.scenario_id == scenario)
        and (config is None or r.config == config)
    ]


def _ordered(items: Iterable[str]) -> list[str]:
    return sorted(set(items))


def fields_per_run(records: Sequence[RunRecord], config: str) -> int:
    """Target fields graded in one run of a configuration (all scenarios together)."""
    subset = _select(records, config=config)
    if not subset:
        return 0
    first = min(r.run_index for r in subset)
    return sum(len(r.fields) for r in subset if r.run_index == first)


def total_metrics(records: Iterable[RunRecord]) -> Metrics:
    totals: dict[str, int] = defaultdict(int)
    for r in records:
        for key, value in asdict(r.metrics).items():
            totals[key] += value
    return Metrics(**totals)


def bucket_label(low: float, high: float) -> str:
    return f"{low:.2f} to {min(high, 1.0):.2f}" + (" (inclusive)" if high > 1 else "")


def calibration(records: Iterable[RunRecord]) -> list[tuple[str, int, int]]:
    """(bucket, fully correct, fields) for every confidence bucket plus 'no confidence'."""
    grades = _fields(records)
    rows: list[tuple[str, int, int]] = []
    for low, high in BUCKETS:
        inside = [g for g in grades if g.confidence is not None and low <= g.confidence < high]
        rows.append((bucket_label(low, high), sum(g.fully_correct for g in inside), len(inside)))
    none = [g for g in grades if g.confidence is None]
    rows.append(("no confidence (UNRESOLVED)", sum(g.fully_correct for g in none), len(none)))
    return rows


# ---- rendering ---------------------------------------------------------------------------------


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return [*lines, ""]


def _config_summary(records: Sequence[RunRecord], configs: Sequence[str]) -> list[str]:
    attributes = [
        ("Fully correct", "fully_correct"),
        ("Source match", "source_match"),
        ("Transformation correct", "transformation_correct"),
        ("Type label match", "type_match"),
        ("UNRESOLVED handled right", "unresolved_correct"),
        ("Review flag right", "flag_correct"),
        ("Guessed an UNRESOLVED field", "guessed"),
    ]
    rows = []
    for config in configs:
        subset = _select(records, config=config)
        rows.append([CONFIG_NAMES[config], *[rate(subset, attr) for _, attr in attributes]])
    return _table(["Configuration", *[name for name, _ in attributes]], rows)


def _scenario_table(records: Sequence[RunRecord], configs: Sequence[str], attr: str) -> list[str]:
    scenarios = _ordered(r.scenario_id for r in records)
    rows = []
    for scenario in scenarios:
        row = [f"`{scenario}`"]
        for config in configs:
            subset = _select(records, scenario=scenario, config=config)
            row.append(rate(subset, attr) if subset else "not run")
        rows.append(row)
    overall = ["**All scenarios**"] + [rate(_select(records, config=c), attr) for c in configs]
    return _table(["Scenario", *[CONFIG_NAMES[c] for c in configs]], [*rows, overall])


def _variance(records: Sequence[RunRecord], configs: Sequence[str]) -> list[str]:
    runs = _ordered(str(r.run_index) for r in records)
    if len(runs) < 2:
        return []
    out = ["#### Variation across repeated runs", ""]
    rows = []
    for config in configs:
        per_run: list[int] = []
        for index in sorted({r.run_index for r in records}):
            subset = [r for r in _select(records, config=config) if r.run_index == index]
            if subset:
                per_run.append(sum(f.grade.fully_correct for r in subset for f in r.fields))
        if not per_run:
            continue
        total = fields_per_run(records, config)
        outcomes: dict[tuple[str, str], set[bool]] = defaultdict(set)
        for r in _select(records, config=config):
            for f in r.fields:
                outcomes[(r.scenario_id, f.grade.target_field)].add(f.grade.fully_correct)
        unstable = sum(1 for seen in outcomes.values() if len(seen) > 1)
        rows.append(
            [
                CONFIG_NAMES[config],
                ", ".join(f"{n}/{total}" for n in per_run),
                f"{min(per_run)} to {max(per_run)}",
                f"{unstable} of {len(outcomes)}",
            ]
        )
    header = [
        "Configuration",
        "Fully correct per run",
        "Range",
        "Fields whose result changed between runs",
    ]
    return [*out, *_table(header, rows)]


def _operations(records: Sequence[RunRecord]) -> list[str]:
    rows = []
    for config in LLM_CONFIGS:
        subset = _select(records, config=config)
        if not subset:
            continue
        m = total_metrics(subset)
        rows.append(
            [
                CONFIG_NAMES[config],
                str(m.llm_calls),
                fraction(m.invalid_output_fields, m.fields),
                fraction(m.reasked_fields, m.fields),
                fraction(m.needs_review, m.fields),
                fraction(m.unresolved, m.fields),
                f"{m.input_tokens:,} / {m.output_tokens:,} / {m.reasoning_tokens:,}",
                f"{m.latency_ms / 1000:.1f} s",
            ]
        )
    header = [
        "Configuration",
        "LLM calls",
        "Fields left invalid after the re-ask",
        "Fields that needed a re-ask",
        "NEEDS_REVIEW",
        "Proposed UNRESOLVED",
        "Tokens in / out / reasoning (included in out)",
        "Total call latency",
    ]
    review_a = _select(records, config="A")
    lines = _table(header, rows)
    if review_a:
        m = total_metrics(review_a)
        lines += [
            "Configuration A makes no LLM calls; "
            f"NEEDS_REVIEW {fraction(m.needs_review, m.fields)}, "
            f"UNRESOLVED {fraction(m.unresolved, m.fields)}.",
            "",
        ]
    return lines


def _calibration(records: Sequence[RunRecord]) -> list[str]:
    lines: list[str] = []
    for config in ("A", *LLM_CONFIGS):
        subset = _select(records, config=config)
        if not subset:
            continue
        rows = [[b, fraction(c, n)] for b, c, n in calibration(subset)]
        flagged = [f.grade for r in subset for f in r.fields if f.grade.flagged]
        unflagged = [f.grade for r in subset for f in r.fields if not f.grade.flagged]
        rows.append(
            ["flagged for review", fraction(sum(g.fully_correct for g in flagged), len(flagged))]
        )
        rows.append(
            [
                "not flagged (auto-accepted)",
                fraction(sum(g.fully_correct for g in unflagged), len(unflagged)),
            ]
        )
        lines += [f"**{CONFIG_NAMES[config]}**", "", *_table(["Confidence", "Fully correct"], rows)]
    return lines


@dataclass(frozen=True)
class LossyBreakdown:
    """Informational: how lossy mappings were handled. Not part of the fully-correct score."""

    unrecoverable: int  # fields the key marks UNRESOLVED
    declined: int  # proposed UNRESOLVED
    guessed_flagged: int  # proposed a value but flagged it for review
    guessed_silent: int  # proposed a value and did not flag it
    lossy_proposals: int  # proposals the validator marked as losing information
    lossy_flagged: int
    lossy_silent: int


def lossy_breakdown(records: Iterable[RunRecord]) -> LossyBreakdown:
    unrecoverable = declined = flagged_guess = silent_guess = 0
    lossy = lossy_flagged = 0
    for record in records:
        for f in record.fields:
            g = f.grade
            if g.expected_type == "UNRESOLVED":
                unrecoverable += 1
                if g.proposed_type == "UNRESOLVED":
                    declined += 1
                elif g.flagged:
                    flagged_guess += 1
                else:
                    silent_guess += 1
            if f.lossy_codes and g.proposed_type != "UNRESOLVED":
                lossy += 1
                lossy_flagged += 1 if g.flagged else 0
    return LossyBreakdown(
        unrecoverable,
        declined,
        flagged_guess,
        silent_guess,
        lossy,
        lossy_flagged,
        lossy - lossy_flagged,
    )


def _lossy(records: Sequence[RunRecord]) -> list[str]:
    rows = []
    for config in CONFIG_NAMES:
        subset = _select(records, config=config)
        if not subset:
            continue
        b = lossy_breakdown(subset)
        rows.append(
            [
                CONFIG_NAMES[config],
                fraction(b.declined, b.unrecoverable),
                fraction(b.guessed_flagged, b.unrecoverable),
                fraction(b.guessed_silent, b.unrecoverable),
                fraction(b.lossy_flagged, b.lossy_proposals),
                fraction(b.lossy_silent, b.lossy_proposals),
            ]
        )
    header = [
        "Configuration",
        "Unrecoverable fields: declined (UNRESOLVED)",
        "Unrecoverable fields: proposed but flagged for review",
        "Unrecoverable fields: silent guess",
        "Validator-detected lossy proposals: flagged for review",
        "Validator-detected lossy proposals: not flagged",
    ]
    return [
        "Informational only; not part of the fully-correct score. *Unrecoverable* fields are the "
        "ones the answer key marks UNRESOLVED, where the source cannot supply the value. A "
        "*silent guess* is a value proposed for such a field without a review flag; a *proposed "
        "but flagged* one at least reaches a human. *Validator-detected lossy proposals* are "
        "proposals that raised an information-loss warning (for example a many-to-one enum map), "
        "whether or not the key allows them.",
        "",
        *_table(header, rows),
    ]


def _mistakes(records: Sequence[RunRecord]) -> list[str]:
    rows: list[list[str]] = []
    for r in sorted(records, key=lambda r: (r.scenario_id, r.config, r.run_index)):
        for f in r.fields:
            unresolved = f.grade.proposed_type == "UNRESOLVED"
            if f.grade.fully_correct and not unresolved:
                continue
            conf = "-" if f.grade.confidence is None else f"{f.grade.confidence:.2f}"
            rows.append(
                [
                    f"`{r.scenario_id}`",
                    r.config,
                    str(r.run_index),
                    f"`{f.grade.target_field}`",
                    f.expected,
                    f.proposed,
                    "correct (flagged)" if f.grade.fully_correct else f.grade.detail or "wrong",
                    f"{f.validation_status} / {f.review_status} / {conf}",
                ]
            )
    if not rows:
        return ["Every mapping was fully correct and none was UNRESOLVED.", ""]
    header = [
        "Scenario",
        "Config",
        "Run",
        "Target field",
        "Expected",
        "Proposed",
        "Verdict",
        "Validation / review / confidence",
    ]
    return _table(header, rows)


def _rag_vs_full(records: Sequence[RunRecord]) -> list[str]:
    scenarios = _ordered(r.scenario_id for r in records)
    higher = equal = lower = 0
    detail: list[str] = []
    for s in scenarios:
        b, c = _select(records, scenario=s, config="B"), _select(records, scenario=s, config="C")
        if not b or not c:
            continue
        nb = sum(f.grade.fully_correct for r in b for f in r.fields)
        nc = sum(f.grade.fully_correct for r in c for f in r.fields)
        higher, equal, lower = higher + (nc > nb), equal + (nc == nb), lower + (nc < nb)
        detail.append(f"`{s}`: C {nc} vs B {nb}")
    if not detail:
        return []
    return [
        f"RAG (C) was higher than full-schema (B) in {higher} scenario(s), equal in {equal} and "
        f"lower in {lower} ({'; '.join(detail)}). Scenario schemas here have at most 8 fields, so "
        "the full schema already fits in the prompt and retrieval has little room to help.",
        "",
    ]


def render_set(label: str, records: Sequence[RunRecord]) -> list[str]:
    configs = [c for c in CONFIG_NAMES if _select(records, config=c)]
    scenarios = _ordered(r.scenario_id for r in records)
    models = _ordered(f"`{r.model}` via `{r.provider}`" for r in records if r.config != "A")
    run_counts = ", ".join(
        f"{c}: {len({r.run_index for r in _select(records, config=c)})}" for c in configs
    )
    out = [
        f"## Run set `{label}`",
        "",
        f"- Model: {', '.join(models) if models else 'none (retrieval-only runs)'}",
        f"- Scenarios: {len(scenarios)}; target fields graded per configuration and run: "
        f"{fields_per_run(records, configs[0])}",
        f"- Repeated runs recorded per configuration: {run_counts}",
        "",
        "### Overall, by configuration",
        "",
        *_config_summary(records, configs),
        "### Fully correct, by scenario",
        "",
        *_scenario_table(records, configs, "fully_correct"),
        *_variance(records, configs),
        "### Where RAG does and does not help",
        "",
        *(_rag_vs_full(records) or ["Both LLM configurations are needed for this comparison.", ""]),
        "### Reliability and cost",
        "",
        *_operations(records),
        "### Lossy mappings: flagged versus silent",
        "",
        *_lossy(records),
        "### Confidence versus accuracy",
        "",
        "Fully correct fraction by confidence bucket (and for flagged vs auto-accepted mappings).",
        "",
        *_calibration(records),
        "### Every wrong or UNRESOLVED mapping",
        "",
        *_mistakes(records),
    ]
    return out


def _limits_section(limits: Limits) -> list[str]:
    out = ["## Free-tier limits observed", ""]
    headers = limits.last_headers
    if headers:
        out += ["Rate-limit headers on the most recent response:", ""]
        out += [f"- `{k}`: {v}" for k, v in sorted(headers.items())]
    else:
        out.append("No rate-limit headers were recorded (no network calls were made).")
    out.append("")
    out.append(
        f"429 waits honoured: {len(limits.rate_limit_waits)}"
        + (
            f", totalling {sum(limits.rate_limit_waits):.0f} s of Retry-After"
            if limits.rate_limit_waits
            else ""
        )
        + "."
    )
    if limits.exhausted:
        out += ["", "Runs stopped by a limit longer than the wall-clock budget (resumable):", ""]
        out += [
            f"- retry-after {e.get('retry_after_s')} s: {e.get('message', '')}"
            for e in limits.exhausted
        ]
    out.append("")
    return out


def render_report(records: Sequence[RunRecord], meta: ReportMeta) -> str:
    out: list[str] = []
    if meta.test_only:
        out += [
            "> **TEST-ONLY RUN.** Generated with a fake or replayed provider. These numbers mean",
            "> nothing and must never be committed as an evaluation.",
            "",
        ]
    labels = _ordered(r.label for r in records)
    scenarios = _ordered(r.scenario_id for r in records)
    out += [
        "# Mapping evaluation",
        "",
        "Generated by `bench/scripts/run_mapping_eval.py` from real runs. Do not edit by hand.",
        "",
        f"- Run date (UTC): {meta.run_date}",
        f"- Repeated runs requested per LLM configuration (N): {meta.n_runs_requested}",
        f"- Scenarios: {', '.join(f'`{s}`' for s in scenarios)}",
        "- Run sets (prompt version / confidence formula): "
        + ", ".join(f"`{label}`" for label in labels),
        "",
        "> **Sample size.** Each configuration is graded on about 30 target fields in total",
        "> (see the counts in the tables). Differences of one or two fields are within noise:",
        "> these results are not statistically strong, and with N=1 nothing here measures",
        "> run-to-run variance.",
        "",
    ]
    for label in labels:
        out += render_set(label, [r for r in records if r.label == label])
    out += _limits_section(meta.limits)
    return "\n".join(out).rstrip() + "\n"


def check_options(provider: str, test_only: bool, output: Path) -> str | None:
    """Why a run must be refused, or None. Fake and replayed output is never an evaluation."""
    real = provider in ("groq", "ollama")
    if not real and not test_only:
        return f"the {provider} provider is for tests only; pass --test-only to use it"
    if test_only and real:
        return "--test-only applies only to fake or replay providers; drop it for a real run"
    if test_only and output.as_posix().endswith(DEFAULT_OUTPUT.as_posix()):
        return f"a --test-only run must not write {DEFAULT_OUTPUT}; choose another --output"
    return None


def load_records(path: Path) -> list[RunRecord]:
    if not path.is_file():
        return []
    return [
        RunRecord.from_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_record(path: Path, record: RunRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(record.to_json() + "\n")
