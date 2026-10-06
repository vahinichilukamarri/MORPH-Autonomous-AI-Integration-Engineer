"""Scoring and reporting for the embedding-retrieval baseline. Pure: no database, no model.

For every mapping in an answer key we ask: when each of its source fields is used as a query,
does the true target field appear among the nearest target fields? A COMPOSITE mapping counts
as a hit if the true target is retrieved for *any* of its source fields. Every number in the
report is computed from the rankings it is given.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from morph_bench.models import Bundle, MappingEntry, MappingType

KS = (1, 3, 5)
TOP_SHOWN = 5
Scope = Literal["all", "resource"]
SCOPES: tuple[Scope, ...] = ("all", "resource")
SCOPE_TITLES: dict[Scope, str] = {
    "all": "(a) All fields of the target system",
    "resource": "(b) Target resource entity only",
}
DEFAULT_OUTPUT = Path("docs/retrieval-baseline.md")
NOT_RETRIEVABLE = (MappingType.CONSTANT, MappingType.UNRESOLVED)


@dataclass(frozen=True)
class Candidate:
    entity: str
    path: str
    distance: float

    @property
    def label(self) -> str:
        return f"{self.entity}.{self.path}"


Retriever = Callable[[str, Scope], Sequence[Candidate]]
"""(source field name, scope) -> every candidate of that scope, nearest first."""


@dataclass(frozen=True)
class ScopeResult:
    best_rank: int | None  # best 1-based rank of the true target over the source fields
    per_source_rank: dict[str, int | None]
    top: dict[str, tuple[Candidate, ...]]  # per source field, the TOP_SHOWN nearest

    def hit(self, k: int) -> bool:
        return self.best_rank is not None and self.best_rank <= k


@dataclass(frozen=True)
class MappingResult:
    target_field: str
    mapping_type: MappingType
    source_fields: tuple[str, ...]
    scopes: dict[Scope, ScopeResult]


@dataclass(frozen=True)
class Skipped:
    target_field: str
    mapping_type: MappingType
    reason: str


@dataclass(frozen=True)
class BaselineResult:
    scenario_id: str
    target_entity: str
    answer_key_sha256: str
    model_name: str
    provider: str
    run_date: str
    test_only: bool
    mappings: tuple[MappingResult, ...]
    skipped: tuple[Skipped, ...]
    pool_sizes: dict[Scope, int]

    def hits(self, scope: Scope, k: int) -> int:
        return sum(1 for m in self.mappings if m.scopes[scope].hit(k))

    def recall(self, scope: Scope, k: int) -> float:
        return self.hits(scope, k) / len(self.mappings) if self.mappings else 0.0


def _scope_result(
    entry: MappingEntry, target_entity: str, retrieve: Retriever, scope: Scope
) -> ScopeResult:
    ranks: dict[str, int | None] = {}
    top: dict[str, tuple[Candidate, ...]] = {}
    for source in entry.source_fields:
        ranking = list(retrieve(source, scope))
        top[source] = tuple(ranking[:TOP_SHOWN])
        ranks[source] = next(
            (
                position
                for position, c in enumerate(ranking, start=1)
                if (c.entity, c.path) == (target_entity, entry.target_field)
            ),
            None,
        )
    found = [r for r in ranks.values() if r is not None]
    return ScopeResult(best_rank=min(found) if found else None, per_source_rank=ranks, top=top)


def evaluate(
    bundle: Bundle,
    retrieve: Retriever,
    *,
    model_name: str,
    provider: str,
    run_date: str,
    test_only: bool,
    pool_sizes: dict[Scope, int],
) -> BaselineResult:
    target_entity = bundle.scenario.target.entity
    mappings: list[MappingResult] = []
    skipped: list[Skipped] = []
    for entry in bundle.answer_key.mappings:
        if entry.mapping_type in NOT_RETRIEVABLE:
            skipped.append(
                Skipped(entry.target_field, entry.mapping_type, "no source field to query with")
            )
            continue
        scopes = {scope: _scope_result(entry, target_entity, retrieve, scope) for scope in SCOPES}
        mappings.append(
            MappingResult(entry.target_field, entry.mapping_type, entry.source_fields, scopes)
        )
    return BaselineResult(
        scenario_id=bundle.scenario.id,
        target_entity=target_entity,
        answer_key_sha256=bundle.answer_key_sha256,
        model_name=model_name,
        provider=provider,
        run_date=run_date,
        test_only=test_only,
        mappings=tuple(mappings),
        skipped=tuple(skipped),
        pool_sizes=pool_sizes,
    )


def check_options(provider: str, test_only: bool, output: Path) -> str | None:
    """Returns why a run must be refused, or None. Fake-provider output is never a baseline."""
    if provider == "fake" and not test_only:
        return "the fake embedding provider is for tests only; pass --test-only to use it"
    if test_only and provider != "fake":
        return "--test-only applies only to the fake provider; drop it for a real run"
    if test_only and output.as_posix().endswith(DEFAULT_OUTPUT.as_posix()):
        return f"a --test-only run must not write {DEFAULT_OUTPUT}; choose another --output"
    return None


# ---- markdown ----------------------------------------------------------------------------------


def _mark(value: bool) -> str:
    return "yes" if value else "no"


def _fraction(hits: int, total: int) -> str:
    return f"{hits}/{total} ({100 * hits / total:.1f}%)" if total else "0/0"


def _table(result: BaselineResult, scope: Scope) -> list[str]:
    header = "| Target field | Mapping type | Source field(s) | True target rank |"
    header += "".join(f" @{k} |" for k in KS)
    lines = [header, "|" + "---|" * (4 + len(KS))]
    for m in result.mappings:
        scoped = m.scopes[scope]
        rank = str(scoped.best_rank) if scoped.best_rank else "not retrieved"
        cells = "".join(f" {_mark(scoped.hit(k))} |" for k in KS)
        sources = ", ".join(m.source_fields)
        lines.append(f"| `{m.target_field}` | {m.mapping_type.value} | {sources} | {rank} |{cells}")
    total = len(result.mappings)
    overall = "".join(f" **{_fraction(result.hits(scope, k), total)}** |" for k in KS)
    lines.append(f"| **Overall recall** | | | |{overall}")
    return lines


def _misses(result: BaselineResult, scope: Scope) -> list[str]:
    lines: list[str] = []
    for m in result.mappings:
        scoped = m.scopes[scope]
        if scoped.hit(1):
            continue
        rank = f"rank {scoped.best_rank}" if scoped.best_rank else "not retrieved at all"
        lines.append(
            f"**`{result.target_entity}.{m.target_field}`** ({m.mapping_type.value}): "
            f"true target at {rank}."
        )
        lines.append("")
        for source in m.source_fields:
            source_rank = scoped.per_source_rank[source]
            where = f"rank {source_rank}" if source_rank else "not retrieved"
            lines.append(f"- query `{source}` (true target: {where}). Top {TOP_SHOWN}:")
            for position, c in enumerate(scoped.top[source], start=1):
                star = (
                    " <- true target"
                    if (c.entity, c.path)
                    == (
                        result.target_entity,
                        m.target_field,
                    )
                    else ""
                )
                lines.append(f"  {position}. `{c.label}` (distance {c.distance:.4f}){star}")
        lines.append("")
    return lines or ["No misses: every true target was retrieved first.", ""]


def render(result: BaselineResult) -> str:
    out: list[str] = []
    if result.test_only:
        out += [
            "> **TEST-ONLY RUN.** Generated with the fake embedding provider. These numbers",
            "> mean nothing and must never be committed as a baseline.",
            "",
        ]
    out += [
        "# Retrieval baseline",
        "",
        "Generated by `bench/scripts/retrieval_baseline.py` from a real run. Do not edit by hand.",
        "",
        f"- Scenario: `{result.scenario_id}` (answer key sha256 `{result.answer_key_sha256[:16]}`)",
        f"- Embedding model: `{result.model_name}` via `{result.provider}`",
        f"- Run date (UTC): {result.run_date}",
        f"- Candidate pool: {result.pool_sizes['all']} fields in the target system, "
        f"{result.pool_sizes['resource']} in `{result.target_entity}`",
        "",
        "## Method",
        "",
        "Each field of the source and the target system is embedded from the text template in",
        "`docs/discovery.md`. For every mapping in the answer key, each of its source fields is",
        "used as a query and the target fields are ranked by cosine distance (pgvector). A",
        "mapping is a hit at *k* when its true target is among the top *k* for any of its source",
        "fields (so COMPOSITE mappings count once). Recall@k is hits divided by the number of",
        "evaluated mappings.",
        "",
        "## Summary",
        "",
        "| Scope | " + " | ".join(f"Recall@{k}" for k in KS) + " |",
        "|---|" + "---|" * len(KS),
    ]
    for scope in SCOPES:
        total = len(result.mappings)
        cells = " | ".join(_fraction(result.hits(scope, k), total) for k in KS)
        out.append(f"| {SCOPE_TITLES[scope]} | {cells} |")
    out.append("")
    for scope in SCOPES:
        out += [f"## {SCOPE_TITLES[scope]}", "", *_table(result, scope), ""]
    for scope in SCOPES:
        out += [
            f"## Misses: {SCOPE_TITLES[scope]}",
            "",
            "Every mapping whose true target was not the nearest neighbour.",
            "",
            *_misses(result, scope),
        ]
    if result.skipped:
        out += ["## Not evaluated", ""]
        out += [
            f"- `{s.target_field}` ({s.mapping_type.value}): {s.reason}" for s in result.skipped
        ]
        out.append("")
    return "\n".join(out).rstrip() + "\n"
