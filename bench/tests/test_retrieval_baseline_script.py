"""The baseline script end to end with the fake provider: checks the report's shape, not numbers."""

import re
from pathlib import Path

import pytest
from app.embeddings.fake import FAKE_MODEL_NAME, FakeEmbeddingProvider
from sqlalchemy.orm import Session

from morph_bench.loader import SCENARIOS_ROOT, load_bundle
from morph_bench.retrieval_baseline import render
from scripts.retrieval_baseline import main, run_baseline

BUNDLE = load_bundle(SCENARIOS_ROOT / "crm_customer_to_support_user")


def test_run_through_the_database_produces_a_well_formed_report(session: Session) -> None:
    result = run_baseline(
        session,
        FakeEmbeddingProvider(),
        BUNDLE,
        provider_kind="fake",
        test_only=True,
        run_date="2026-01-02",
    )
    assert result.model_name == FAKE_MODEL_NAME
    assert result.pool_sizes == {"all": 8 + 3 + 4 + 1, "resource": 8}
    assert len(result.mappings) == 8

    text = render(result)
    assert text.startswith("> **TEST-ONLY RUN.**")
    for heading in (
        "# Retrieval baseline",
        "## Method",
        "## Summary",
        "## (a) All fields of the target system",
        "## (b) Target resource entity only",
        "## Misses: (a) All fields of the target system",
        "## Misses: (b) Target resource entity only",
    ):
        assert heading in text
    assert f"`{FAKE_MODEL_NAME}`" in text
    assert "2026-01-02" in text
    for mapping in BUNDLE.answer_key.mappings:
        assert f"| `{mapping.target_field}` |" in text
    overall = re.findall(r"\| \*\*Overall recall\*\* \|[^\n]*", text)
    assert len(overall) == 2
    assert all(re.search(r"\*\*\d+/8 \(\d+\.\d%\)\*\*", row) for row in overall)


def test_baseline_reruns_are_idempotent_on_the_same_database(session: Session) -> None:
    first = run_baseline(
        session, FakeEmbeddingProvider(), BUNDLE, provider_kind="fake", test_only=True, run_date="d"
    )
    second = run_baseline(
        session, FakeEmbeddingProvider(), BUNDLE, provider_kind="fake", test_only=True, run_date="d"
    )
    assert render(first) == render(second)


def test_the_fake_provider_is_refused_without_test_only(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--provider", "fake"]) == 2
    assert "pass --test-only" in capsys.readouterr().err


def test_test_only_cannot_overwrite_the_committed_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--provider", "fake", "--test-only", "--output", "docs/retrieval-baseline.md"])
    assert code == 2
    assert "must not write" in capsys.readouterr().err


def test_test_only_with_a_real_provider_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--provider", "fastembed", "--test-only"]) == 2
    assert "applies only to the fake" in capsys.readouterr().err


def test_main_writes_a_test_only_report(
    test_engine: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy import Engine

    assert isinstance(test_engine, Engine)
    output = tmp_path / "report.md"
    url = test_engine.url.render_as_string(hide_password=False)
    code = main(
        ["--provider", "fake", "--test-only", "--output", str(output), "--database-url", url]
    )
    assert code == 0
    assert output.read_text(encoding="utf-8").startswith("> **TEST-ONLY RUN.**")
    assert "wrote" in capsys.readouterr().out
