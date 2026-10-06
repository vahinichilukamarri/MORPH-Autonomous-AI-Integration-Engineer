"""Scoring and rendering of the retrieval baseline, with scripted rankings (no DB, no model)."""

from collections.abc import Sequence
from pathlib import Path

import pytest

from morph_bench.loader import SCENARIOS_ROOT, load_bundle
from morph_bench.retrieval_baseline import (
    DEFAULT_OUTPUT,
    BaselineResult,
    Candidate,
    Scope,
    check_options,
    evaluate,
    render,
)

BUNDLE = load_bundle(SCENARIOS_ROOT / "crm_customer_to_support_user")
USER = "User"
FILLER = [Candidate("ErrorDetail", f"noise{i}", 0.5 + i / 100) for i in range(10)]


def ranking(target: str | None, rank: int, entity: str = USER) -> list[Candidate]:
    """FILLER with the true target inserted at a 1-based rank (or absent)."""
    items = list(FILLER)
    if target is not None:
        items.insert(rank - 1, Candidate(entity, target, 0.1 * rank))
    return items


def scripted(table: dict[tuple[str, Scope], Sequence[Candidate]]):  # type: ignore[no-untyped-def]
    def retrieve(source: str, scope: Scope) -> Sequence[Candidate]:
        return table.get((source, scope), FILLER)

    return retrieve


def run(
    table: dict[tuple[str, Scope], Sequence[Candidate]], *, test_only: bool = False
) -> BaselineResult:
    return evaluate(
        BUNDLE,
        scripted(table),
        model_name="test-model",
        provider="fake" if test_only else "fastembed",
        run_date="2026-01-02",
        test_only=test_only,
        pool_sizes={"all": 20, "resource": 8},
    )


TABLE: dict[tuple[str, Scope], Sequence[Candidate]] = {
    # email -> email_address: first in both scopes
    ("email", "all"): ranking("email_address", 1),
    ("email", "resource"): ranking("email_address", 1),
    # status -> accountState: third overall, second inside User
    ("status", "all"): ranking("accountState", 3),
    ("status", "resource"): ranking("accountState", 2),
    # segment -> tier: only reachable at rank 7 overall, absent from the User pool
    ("segment", "all"): ranking("tier", 7),
    # fullName is COMPOSITE: first_name misses, last_name finds it at rank 4
    ("first_name", "all"): ranking("fullName", 9),
    ("last_name", "all"): ranking("fullName", 4),
    ("last_name", "resource"): ranking("fullName", 1),
    # a same-named field in the wrong entity must not count
    ("customer_id", "all"): ranking("userId", 1, entity="ErrorDetail"),
}


def by_name(result: BaselineResult) -> dict[str, object]:
    return {m.target_field: m for m in result.mappings}


def test_every_retrievable_mapping_is_evaluated() -> None:
    result = run(TABLE)
    assert [m.target_field for m in result.mappings] == [
        "userId",
        "externalRef",
        "fullName",
        "email_address",
        "phoneNumber",
        "accountState",
        "tier",
        "createdAt",
    ]
    assert result.skipped == ()


def test_hits_and_ranks_are_computed_from_the_rankings() -> None:
    result = run(TABLE)
    scopes = {m.target_field: m.scopes for m in result.mappings}
    assert scopes["email_address"]["all"].best_rank == 1
    assert scopes["accountState"]["all"].best_rank == 3
    assert scopes["accountState"]["resource"].best_rank == 2
    assert scopes["tier"]["all"].best_rank == 7
    assert scopes["tier"]["resource"].best_rank is None
    assert scopes["createdAt"]["all"].best_rank is None
    assert scopes["userId"]["all"].best_rank is None, "same field name in another entity"


def test_composite_hits_when_any_source_field_finds_the_target() -> None:
    full_name = {m.target_field: m for m in run(TABLE).mappings}["fullName"]
    all_scope = full_name.scopes["all"]
    assert all_scope.per_source_rank == {"first_name": 9, "last_name": 4}
    assert all_scope.best_rank == 4
    assert (all_scope.hit(1), all_scope.hit(3), all_scope.hit(5)) == (False, False, True)
    assert full_name.scopes["resource"].best_rank == 1


def test_recall_is_hits_over_evaluated_mappings() -> None:
    result = run(TABLE)
    assert [result.hits("all", k) for k in (1, 3, 5)] == [1, 2, 3]
    assert [result.hits("resource", k) for k in (1, 3, 5)] == [2, 3, 3]
    assert result.recall("all", 5) == pytest.approx(3 / 8)


def test_chance_level_is_computed_from_the_pool_sizes() -> None:
    result = run(TABLE)  # pools: 20 fields overall, 8 in User; fullName has two source fields
    expected_at_1 = 7 * (1 / 20) + (1 - (19 / 20) ** 2)
    assert result.chance_hits("all", 1) == pytest.approx(expected_at_1)
    assert result.chance_hits("resource", 8) == pytest.approx(8.0)
    assert result.chance_hits("all", 5) == pytest.approx(7 * (5 / 20) + (1 - (15 / 20) ** 2))
    text = render(result)
    assert "chance level" in text
    assert f"{expected_at_1:.1f}/8" in text


def test_report_contains_both_tables_and_the_computed_numbers() -> None:
    text = render(run(TABLE))
    assert text.startswith("# Retrieval baseline")
    assert "Embedding model: `test-model` via `fastembed`" in text
    assert "Run date (UTC): 2026-01-02" in text
    assert "## (a) All fields of the target system" in text
    assert "## (b) Target resource entity only" in text
    assert "| Scope | Recall@1 | Recall@3 | Recall@5 |" in text
    assert (
        "| (a) All fields of the target system | 1/8 (12.5%) | 2/8 (25.0%) | 3/8 (37.5%) |" in text
    )
    assert "| (b) Target resource entity only | 2/8 (25.0%) | 3/8 (37.5%) | 3/8 (37.5%) |" in text
    assert "| `accountState` | TRANSFORMATION | status | 3 | no | yes | yes |" in text
    assert "| `tier` | DERIVED | segment | not retrieved | no | no | no |" in text
    assert "TEST-ONLY" not in text


def test_every_miss_lists_its_top_five() -> None:
    text = render(run(TABLE))
    misses_a = text.split("## Misses: (a) All fields of the target system")[1].split(
        "## Misses: (b)"
    )[0]
    assert "**`User.accountState`** (TRANSFORMATION): true target at rank 3." in misses_a
    assert "- query `status` (true target: rank 3). Top 5:" in misses_a
    assert "3. `User.accountState` (distance 0.3000) <- true target" in misses_a
    assert "`User.email_address`" not in misses_a, "a first-place hit is not a miss"
    for query in ("first_name", "last_name", "segment", "customer_id", "created_at"):
        assert f"- query `{query}`" in misses_a
    assert misses_a.count("Top 5:") >= 6
    top_lines = [line for line in misses_a.splitlines() if line.startswith("  ")]
    assert all(int(line.split(".")[0]) <= 5 for line in top_lines)


def test_test_only_runs_are_marked() -> None:
    assert render(run(TABLE, test_only=True)).startswith("> **TEST-ONLY RUN.**")


def test_options_guard() -> None:
    docs = DEFAULT_OUTPUT
    assert check_options("fastembed", False, docs) is None
    assert check_options("fake", True, Path(".run/x.md")) is None
    assert "tests only" in (check_options("fake", False, Path(".run/x.md")) or "")
    assert "applies only to the fake" in (check_options("fastembed", True, docs) or "")
    assert "must not write" in (check_options("fake", True, docs) or "")
    assert "must not write" in (check_options("fake", True, Path("/repo") / docs) or "")
