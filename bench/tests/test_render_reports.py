"""The committed reports regenerate from committed files, and edits that are not declared fail.

A scripted fixed-start run is recorded, exported and published through the same code the real run
will use; then the report is rendered from the published files and the replays only. No model, no
Docker.
"""

import json
from pathlib import Path

import pytest
from app.repair.service import ensure_checkpoint_schema
from sqlalchemy import Engine

from morph_bench.repair_eval import load_results
from morph_bench.repair_report import cross_check, load_calls, verify_provenance
from scripts.export_repair_replays import export
from scripts.publish_repair_results import build_provenance
from scripts.render_reports import DOCS, render_all
from scripts.run_repair_eval import EXIT_OK, main
from tests.repair_support import GOOD, Scripted, StubRunner, l2_reply

V04 = Path(__file__).resolve().parents[1] / "replays" / "codegen"


@pytest.fixture(scope="module", autouse=True)
def checkpoint_schema(test_engine: Engine) -> None:
    ensure_checkpoint_schema(
        test_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    )


def published(engine: Engine, tmp_path: Path) -> tuple[Path, Path, Path]:
    """A scripted fixed-start run, exported and published into tmp_path."""
    store = tmp_path / "store"
    argv = [
        "--phase", "fixed", "--conditions", "L2R", "--scenarios", "S1", "--test-only",
        "--store-dir", str(store), "--database-url",
        engine.url.render_as_string(hide_password=False),
    ]  # fmt: skip
    code = main(
        argv,
        llm_override=Scripted([l2_reply(GOOD)]),
        runner_override=StubRunner(),
        smoke_runner_override=StubRunner(),
        oracle_override=lambda bundle, scenario: [{"category": "O1", "name": "a", "passed": True}],
    )
    assert code == EXIT_OK
    replays = tmp_path / "replays"
    export(store, replays / "repair" / "fixed", allow_test_only=True)
    (replays / "codegen").mkdir(parents=True)
    (replays / "codegen" / "calls.jsonl").write_bytes((V04 / "calls.jsonl").read_bytes())
    results_dir = tmp_path / "results" / "fixed"
    results_dir.mkdir(parents=True)
    (results_dir / "results.jsonl").write_bytes((store / "results.jsonl").read_bytes())
    provenance = build_provenance(
        store, replays / "repair" / "fixed", results_dir / "results.jsonl"
    )
    (results_dir / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    return tmp_path / "results", replays, results_dir


def render(root: Path, replays: Path, tmp_path: Path) -> tuple[Path, list[str]]:
    doc = tmp_path / "repair-eval.md"
    written, problems = render_all(
        codegen_dir=tmp_path / "none", repair_root=root, replays=replays, docs={"fixed": doc}
    )
    assert written == [doc] or problems
    return doc, problems


def test_the_repair_report_regenerates_from_the_published_files(
    test_engine: Engine, tmp_path: Path
) -> None:
    root, replays, _ = published(test_engine, tmp_path)
    doc, problems = render(root, replays, tmp_path)
    assert problems == []
    text = doc.read_text(encoding="utf-8")
    assert "# Repair evaluation (v0.5, fixed-start)" in text
    assert "L2R" in text and "seeded (v0.4 reply)" in text and "READY, oracle correct" in text
    assert "Seeded attempt 0" in text
    first = text
    again, _ = render(root, replays, tmp_path)
    assert again.read_text(encoding="utf-8") == first, "rendering is deterministic"


def test_a_model_dependent_edit_to_the_results_fails_the_cross_check(
    test_engine: Engine, tmp_path: Path
) -> None:
    root, replays, results_dir = published(test_engine, tmp_path)
    results = load_results(results_dir / "results.jsonl")
    expected = json.loads((replays / "repair" / "fixed" / "expected.json").read_text("utf-8"))
    calls = load_calls(replays / "repair" / "fixed" / "calls.jsonl")
    seed = load_calls(replays / "codegen" / "calls.jsonl")
    assert cross_check(results, expected, calls, seed) == [], "the control passes"
    results[0].attempts[1].output_tokens = 1
    results[0].status = "HUMAN_REVIEW_REQUIRED"
    problems = cross_check(results, expected, calls, seed)
    assert any("status" in p for p in problems) and any("usage differs" in p for p in problems)


def test_an_undeclared_edit_to_the_results_fails_the_provenance_check(
    test_engine: Engine, tmp_path: Path
) -> None:
    root, replays, results_dir = published(test_engine, tmp_path)
    assert verify_provenance(results_dir, repair=True) == [], "the control passes"
    target = results_dir / "results.jsonl"
    target.write_text(target.read_text("utf-8").replace('"READY"', '"READY "'), encoding="utf-8")
    problems = verify_provenance(results_dir, repair=True)
    assert problems and "undeclared edit" in problems[0]
    _, from_render = render(root, replays, tmp_path)
    assert any("undeclared edit" in p for p in from_render)


def test_the_provenance_header_has_the_pre_registered_fields(
    test_engine: Engine, tmp_path: Path
) -> None:
    _, _, results_dir = published(test_engine, tmp_path)
    header = json.loads((results_dir / "provenance.json").read_text("utf-8"))
    assert header["post_run_edits"] == []
    assert set(header["versions"]) == {
        "generator", "runtime", "compiler", "gate", "codegen_prompt", "repair_prompt",
        "repair_graph",
    }  # fmt: skip
    assert header["model_reported_by_replies"] == "scripted-model" or header["units"] == 1


def test_the_committed_codegen_report_regenerates_byte_for_byte(tmp_path: Path) -> None:
    target = tmp_path / "codegen-eval.md"
    written, problems = render_all(
        repair_root=tmp_path / "none", replays=tmp_path / "none", codegen_doc=target
    )
    assert problems == [] and written == [target]
    assert target.read_text(encoding="utf-8") == (DOCS / "codegen-eval.md").read_text(
        encoding="utf-8"
    )


def test_a_test_only_run_cannot_be_published_as_results(
    test_engine: Engine, tmp_path: Path
) -> None:
    from scripts.publish_repair_results import publish

    published(test_engine, tmp_path)
    with pytest.raises(ValueError, match="test-only"):
        publish(tmp_path / "store", tmp_path / "out", tmp_path / "replays" / "repair" / "fixed")
    assert not (tmp_path / "out" / "results.jsonl").exists()
