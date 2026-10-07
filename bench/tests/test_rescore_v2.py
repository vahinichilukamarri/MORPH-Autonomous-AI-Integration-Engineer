"""Validator v2 rescoring: report rendering, splicing and the zero-new-calls harness."""

from pathlib import Path
from typing import Any

import pytest
from app.embeddings.fake import FakeEmbeddingProvider
from app.llm.base import LLMRequest
from app.llm.fake import ScriptedFakeProvider
from pydantic import BaseModel
from sqlalchemy.orm import Session

from morph_bench.mapping_eval import Limits
from morph_bench.rescore_v2 import (
    END,
    START,
    FieldPair,
    V2Meta,
    render_v2_section,
    splice_block,
    splice_section,
)
from morph_bench.rescore_v21 import END as V21_END
from morph_bench.rescore_v21 import START as V21_START
from morph_bench.rescore_v21 import render_v21_section
from scripts.rescore_v2 import NoNewCalls, main, rescore
from scripts.run_mapping_eval import ResumableProvider
from tests.test_mapping_eval import BUNDLES, SAMPLES_DIR, evaluate, perfect_reply

META = V2Meta("2026-01-02", "test-model", 65, 0, 64, 64)


def pair(
    field: str,
    correct: bool,
    v1_review: str,
    v2_review: str,
    *,
    config: str = "B",
    v1_codes: tuple[str, ...] = (),
    v2_codes: tuple[str, ...] = (),
    trunc_review: str | None = None,
    collapse_review: str | None = None,
    v1_conf: float = 1.0,
    v2_conf: float = 1.0,
) -> FieldPair:
    reviews = {
        "v1": v1_review,
        "v2": v2_review,
        "v2_truncation": trunc_review or v2_review,
        "v2_collapse": collapse_review or v2_review,
        "v2_all_lossy": trunc_review or collapse_review or v2_review,
        "v2_1": trunc_review or v2_review,
    }
    return FieldPair(
        scenario_id="s",
        config=config,
        target_field=field,
        fully_correct=correct,
        unresolved=False,
        proposed="TRANSFORMATION [x] COPY",
        expected="TRANSFORMATION [x]",
        v1_confidence=v1_conf,
        v2_confidence=v2_conf,
        v1_codes=v1_codes,
        v2_codes=v2_codes,
        reviews=reviews,
        confidence={k: (v1_conf if k == "v1" else v2_conf) for k in reviews},
        detail="" if correct else "produced 'Ann', expected 'Ann Smith'",
    )


AUTO, REVIEW = "AUTO_ACCEPTED", "NEEDS_REVIEW"


def pairs() -> list[FieldPair]:
    return [
        pair("ok", True, AUTO, AUTO),
        pair("caught", False, AUTO, REVIEW, v2_codes=("MOSTLY_NULL_OUTPUT",), v2_conf=0.78),
        pair("silent", False, AUTO, AUTO, v2_codes=("LOSSY_TRUNCATION",), trunc_review=REVIEW),
        pair("tier", True, AUTO, AUTO, v1_codes=("INFORMATION_LOSS_ENUM",),
             v2_codes=("INFORMATION_LOSS_ENUM",), collapse_review=REVIEW),
        pair("cfg_c_ok", True, REVIEW, REVIEW, config="C"),
    ]  # fmt: skip


def test_section_is_labelled_post_hoc_and_says_it_is_not_unbiased() -> None:
    text = render_v2_section(pairs(), META)
    assert text.startswith(START) and text.rstrip().endswith(END)
    assert "v2, post-hoc, designed after seeing v1 results" in text
    assert "not an unbiased result" in text
    assert "**0 new LLM calls**" in text and "65 stored responses reused" in text
    assert "reproduced 64/64 mapping outcomes" in text
    assert "above is unchanged" in text


def test_the_comparison_numbers_are_computed_from_the_pairs() -> None:
    text = render_v2_section(pairs(), META)
    b = text.split("### B: LLM, full schema: v1 versus v2")[1].split("### C:")[0]
    assert "| Flagged for review | 0/4 (0.0%) | 1/4 (25.0%) |" in b
    assert "| Accuracy of unflagged (auto-accepted) mappings | 2/4 (50.0%) | 2/3 (66.7%) |" in b
    assert (
        "Newly flagged by v2: 1 mapping(s), of which 1 were wrong (caught) and 0 were correct" in b
    )
    assert "MOSTLY_NULL_OUTPUT" in b and "1.00 to 0.78" in b
    assert "Wrong mappings still auto-accepted under v2: 1." in b
    assert "produced 'Ann', expected 'Ann Smith'" in b


def test_policy_table_counts_caught_and_false_positives_per_policy() -> None:
    text = render_v2_section(pairs(), META)
    table = text.split("### Should the lossy checks force review?")[1].split("New v2 codes")[0]
    rows = {
        line.split("|")[1].strip(): [c.strip() for c in line.split("|")[2:-1]]
        for line in table.splitlines()
        if line.startswith("| ") and "---" not in line
    }
    assert rows["v1 (as reported above)"][2:] == ["0", "0", "2"]
    truncation = next(v for k, v in rows.items() if k == "v2 + LOSSY_TRUNCATION forces review")
    assert truncation[2:] == ["2", "0", "0"], "caught both wrong mappings, no new false positive"
    collapse = next(v for k, v in rows.items() if k.startswith("v2 + many-to-one"))
    assert collapse[3] == "1", "the correct tier mapping would become a false positive"


def test_the_collapse_and_truncation_recommendation_is_marked_not_applied() -> None:
    text = render_v2_section(pairs(), META)
    assert "**not applied**" in text and "LOSSY_FORCES_REVIEW" in text


def test_splice_keeps_the_text_above_byte_identical_and_is_idempotent() -> None:
    v1 = "# Mapping evaluation\n\nthe v1 text, exactly\n"
    section = render_v2_section(pairs(), META)
    once = splice_section(v1, section)
    assert once.startswith(v1.rstrip("\n"))
    assert once.split(START)[0].rstrip("\n") == v1.rstrip("\n")
    assert splice_section(once, section) == once
    changed = render_v2_section(pairs(), V2Meta("2027-01-01", "m", 1, 0, 1, 1))
    twice = splice_section(once, changed)
    assert twice.count(START) == 1 and "2027-01-01" in twice and "2026-01-02" not in twice
    assert twice.split(START)[0].rstrip("\n") == v1.rstrip("\n")


def test_the_provider_under_the_store_refuses_to_make_calls() -> None:
    with pytest.raises(AssertionError, match="must not make new LLM calls"):
        NoNewCalls().complete_raw(LLMRequest("s", ("p",), "x"), BaseModel)


def _saved_run(session: Session, store: Path) -> None:
    llm = ResumableProvider(ScriptedFakeProvider(responder=perfect_reply), store, limits=Limits())
    evaluate(session, store, configs=("B", "C"), llm=llm)


def test_rescoring_reuses_every_saved_response_and_makes_no_calls(
    session: Session, tmp_path: Path
) -> None:
    _saved_run(session, tmp_path)
    found, meta = rescore(
        session,
        bundles=BUNDLES,
        configs=("B", "C"),
        store_dir=tmp_path,
        embedder=FakeEmbeddingProvider(),
        samples_dir=SAMPLES_DIR,
    )
    assert meta.llm_calls_made == 0
    assert meta.v1_reproduced == meta.fields_checked == len(found) == 64
    assert meta.responses_reused >= 64
    assert all(p.fully_correct for p in found if p.config == "B"), "a perfect scripted model"
    assert {p.config for p in found} == {"B", "C"}
    for p in found:
        assert set(p.reviews) == {
            "v1",
            "v2",
            "v2_1",
            "v2_truncation",
            "v2_collapse",
            "v2_all_lossy",
        }


def test_rescoring_an_empty_store_fails_instead_of_calling_a_model(
    session: Session, tmp_path: Path
) -> None:
    _saved_run(session, tmp_path)
    empty = tmp_path / "empty"
    (empty).mkdir()
    (empty / "results.jsonl").write_text(
        (tmp_path / "results.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="must not make new LLM calls"):
        rescore(
            session,
            bundles=BUNDLES[:1],
            configs=("B",),
            store_dir=empty,
            embedder=FakeEmbeddingProvider(),
            samples_dir=SAMPLES_DIR,
        )


class _Borrowed:
    """Hands the test's own session to main(), so it never opens a competing transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def __call__(self, engine: Any) -> "_Borrowed":
        return self

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *exc: object) -> None:
        return None


def test_the_command_splices_the_section_under_an_untouched_v1_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: Session
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    _saved_run(session, store)
    monkeypatch.setattr("scripts.rescore_v2.create_engine", lambda url: object())
    monkeypatch.setattr("scripts.rescore_v2.Session", _Borrowed(session))
    doc = tmp_path / "report.md"
    v1_text = "# Mapping evaluation\n\nv1 text that must not change.\n"
    doc.write_text(v1_text, encoding="utf-8")
    args = ["--store-dir", str(store), "--doc", str(doc)]
    assert main(args, embedder_override=FakeEmbeddingProvider()) == 0
    first = doc.read_text(encoding="utf-8")
    assert first.startswith(v1_text.rstrip("\n")) and "post-hoc" in first
    assert main(args, embedder_override=FakeEmbeddingProvider()) == 0
    again = doc.read_text(encoding="utf-8")
    assert again.count(START) == 1 and again.split(START)[0] == first.split(START)[0]
    assert again == first, "re-running changes nothing: the blocks are replaced in place"
    v2_block = first.split(START)[1].split(END)[0]
    assert V21_START in first and first.index(START) < first.index(V21_START)
    assert again.split(START)[1].split(END)[0] == v2_block, "the v2 block is left as it was"
    assert main([*args, "--refresh-v2"], embedder_override=FakeEmbeddingProvider()) == 0
    refreshed = doc.read_text(encoding="utf-8")
    assert refreshed.count(START) == 1 and refreshed.count(V21_START) == 1


# ---- policy v2.1 -------------------------------------------------------------------------------


def test_v21_section_is_labelled_post_hoc_and_states_the_policy() -> None:
    text = render_v21_section(pairs(), META)
    assert text.startswith(V21_START) and text.rstrip().endswith(V21_END)
    assert "v2.1, post-hoc, chosen after seeing v1 failures" in text
    assert "not an unbiased result" in text
    assert "`LOSSY_TRUNCATION` now forces `NEEDS_REVIEW`" in text
    assert "`LOSSY_COLLAPSE`" in text and "do **not** force review" in text
    assert "**0 new LLM calls**" in text


def test_v21_comparison_has_the_four_requested_measures_for_v1_v2_and_v21() -> None:
    text = render_v21_section(pairs(), META)
    b = text.split("### B: LLM, full schema: v1 versus v2 versus v2.1")[1].split("### C:")[0]
    assert "| Measure | v1 | v2 | v2.1 |" in b
    assert "| Flagged for review | 0/4 (0.0%) | 1/4 (25.0%) | 2/4 (50.0%) |" in b
    unflagged_row = "| Accuracy of unflagged (auto-accepted) mappings |"
    assert f"{unflagged_row} 2/4 (50.0%) | 2/3 (66.7%) | 2/2 (100.0%) |" in b
    assert (
        "| Wrong mappings that were auto-accepted | 2/2 (100.0%) | 1/2 (50.0%) | 0/2 (0.0%) |" in b
    )
    assert "| New false positives (correct mappings flagged now, not in v1) | 0 | 0 | 0 |" in b
    assert "| Wrong mappings newly caught (flagged now, not in v1) | 0 | 1 | 2 |" in b
    assert "### B and C together" in text


def test_v21_reports_a_new_false_positive_when_a_correct_mapping_is_newly_flagged() -> None:
    extra = pairs() + [
        pair("splitter", True, AUTO, AUTO, v2_codes=("LOSSY_TRUNCATION",), trunc_review=REVIEW)
    ]
    text = render_v21_section(extra, META)
    b = text.split("### B: LLM, full schema: v1 versus v2 versus v2.1")[1].split("### C:")[0]
    assert "| New false positives (correct mappings flagged now, not in v1) | 0 | 0 | 1 |" in b
    assert "yes (false positive)" in text


def test_v21_lists_the_customer_id_limitation_as_not_fixed_with_its_evidence() -> None:
    fabricated = pair(
        "customer_id", False, REVIEW, REVIEW, v1_codes=("AMBIGUOUS_ALTERNATIVES",),
        v2_codes=("AMBIGUOUS_ALTERNATIVES",), v1_conf=0.54, v2_conf=0.54,
    )  # fmt: skip
    text = render_v21_section([*pairs(), fabricated], META)
    limits = text.split("### Known limitations (not fixed)")[1]
    assert "B's fabricated `customer_id` is not detectable by any deterministic check" in limits
    assert "plausible, varied, non-null output" in limits
    assert "listed `externalRef` among its rejected alternatives" in limits
    assert "known limitation, not fixed" in limits
    assert "| `s` `customer_id` | B | AMBIGUOUS_ALTERNATIVES | 0.54 | yes | yes |" in limits


def test_splice_block_replaces_only_its_own_block() -> None:
    v1 = "# Report\n\nv1 text\n"
    v2 = render_v2_section(pairs(), META)
    v21 = render_v21_section(pairs(), META)
    doc = splice_block(splice_section(v1, v2), v21, V21_START, V21_END)
    assert doc.index(START) < doc.index(V21_START)
    changed_v21 = render_v21_section(pairs(), V2Meta("2030-01-01", "m", 1, 0, 1, 1))
    again = splice_block(doc, changed_v21, V21_START, V21_END)
    assert "2030-01-01" in again and again.count(V21_START) == 1
    assert again.split(V21_START)[0] == doc.split(V21_START)[0], "v1 and v2 text untouched"
    refreshed = splice_section(
        doc, render_v2_section(pairs(), V2Meta("2031-01-01", "m", 1, 0, 1, 1))
    )
    assert "2031-01-01" in refreshed and V21_START in refreshed and refreshed.count(START) == 1
    assert refreshed.startswith(v1.rstrip("\n")), "the v1 text is still first"
    assert refreshed.split(V21_START)[1] == doc.split(V21_START)[1], "the v2.1 block is kept"
