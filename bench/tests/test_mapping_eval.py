"""The evaluation script end to end with a scripted provider: shape, resume and refusals."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from app.embeddings.fake import FakeEmbeddingProvider
from app.llm.base import LLMRequest, RateLimitExhausted
from app.llm.fake import ScriptedFakeProvider
from app.llm.store import ReplayLLMProvider, ResponseStore
from app.mapping.transform import Transformation
from pydantic import BaseModel
from sqlalchemy.orm import Session

from morph_bench.loader import SCENARIOS_ROOT, discover
from morph_bench.manifest import BENCH_DIR
from morph_bench.mapping_eval import (
    Limits,
    ReportMeta,
    RunRecord,
    check_options,
    load_records,
    render_report,
)
from scripts.run_mapping_eval import (
    LABEL,
    ResumableProvider,
    main,
    parse_duration,
    run_evaluation,
)

SAMPLES_DIR = BENCH_DIR.parent / "mock_systems" / "samples"
BUNDLES = discover(SCENARIOS_ROOT)
BLANK: dict[str, Any] = {
    k: None
    for k in (
        "field", "fields", "separator", "trim", "prefix", "pattern", "group", "replacement",
        "mapping", "on_unmapped", "default", "to", "from_format", "to_format", "value", "index",
    )
}  # fmt: skip


def _references() -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    for bundle in BUNDLES:
        path = BENCH_DIR / "references" / f"{bundle.scenario.id}.yaml"
        pipelines = yaml.safe_load(path.read_text(encoding="utf-8"))["pipelines"]
        for entry in bundle.answer_key.mappings:
            if entry.target_field in pipelines:
                out.append(
                    (entry.target_field, entry.mapping_type.value, pipelines[entry.target_field])
                )
    return out


REFERENCES = _references()


def _flat(step: dict[str, Any]) -> dict[str, Any]:
    flat = {**BLANK, **step}
    if "mapping" in step:
        flat["mapping"] = [{"source": k, "target": v} for k, v in step["mapping"].items()]
    return flat


def _names(block: str) -> set[str]:
    return set(re.findall(r'"name": "([^"]+)"', block))


def perfect_reply(request: LLMRequest) -> str:
    """What a perfect model answers, using the hand-written reference pipelines."""
    prompt = request.parts[0]
    target = _names(prompt.split('name="TARGET_FIELD">>>')[1].split("<<<END")[0]).pop()
    shown = _names(prompt.split("<<<END_UNTRUSTED_DATA>>>")[1])
    for name, kind, pipeline in REFERENCES:
        sources = set(Transformation.model_validate(pipeline).source_fields)
        if name == target and sources <= shown:
            return json.dumps(
                {
                    "target_field": target,
                    "mapping_type": kind,
                    "source_fields": sorted(sources),
                    "steps": [_flat(s) for s in pipeline["steps"]],
                    "unresolved_reason": None,
                    "rationale": "reference",
                    "alternatives": [],
                    "certainty": "HIGH",
                }
            )
    return json.dumps(
        {
            "target_field": target,
            "mapping_type": "UNRESOLVED",
            "source_fields": [],
            "steps": [],
            "unresolved_reason": "the source does not contain this information",
            "rationale": "no suitable source field",
            "alternatives": [],
            "certainty": "HIGH",
        }
    )


def evaluate(session: Session, tmp_path: Path, **kwargs: Any) -> tuple[int, Path]:
    results = tmp_path / "results.jsonl"
    llm = kwargs.pop("llm", ScriptedFakeProvider(responder=perfect_reply))
    done, _ = run_evaluation(
        session,
        bundles=kwargs.pop("bundles", BUNDLES),
        configs=kwargs.pop("configs", ("A", "B", "C")),
        n_runs=kwargs.pop("n_runs", 1),
        llm=llm,
        embedder=FakeEmbeddingProvider(),
        results_path=results,
        samples_dir=SAMPLES_DIR,
        log=lambda message: None,
        **kwargs,
    )
    return done, results


# ---- running -----------------------------------------------------------------------------------


def test_every_scenario_config_and_run_is_graded_and_recorded(
    session: Session, tmp_path: Path
) -> None:
    done, results = evaluate(session, tmp_path, n_runs=2)
    records = load_records(results)
    assert done == len(records) == 4 * (1 + 2 + 2), "A once, B and C per requested run"
    assert {r.label for r in records} == {LABEL}
    assert {(r.config, r.run_index) for r in records} == {
        ("A", 1), ("B", 1), ("B", 2), ("C", 1), ("C", 2)
    }  # fmt: skip
    assert all(len(r.fields) == 8 for r in records)


def test_a_perfect_model_scores_every_field_correct_in_full_schema_mode(
    session: Session, tmp_path: Path
) -> None:
    _, results = evaluate(session, tmp_path, configs=("B",))
    records = load_records(results)
    wrong = [
        (r.scenario_id, f.grade.target_field, f.grade.detail)
        for r in records
        for f in r.fields
        if not f.grade.fully_correct
    ]
    assert wrong == []
    s3 = next(r for r in records if r.scenario_id == "support_user_to_crm_customer")
    by_field = {f.grade.target_field: f for f in s3.fields}
    assert by_field["customer_id"].review_status == "NEEDS_REVIEW", (
        "nullable -> required forces review"
    )
    assert by_field["segment"].grade.unresolved_correct is True
    assert s3.metrics.unresolved == 1


def test_retrieval_only_baseline_runs_without_an_llm(session: Session, tmp_path: Path) -> None:
    done, results = evaluate(session, tmp_path, configs=("A",), llm=None)
    records = load_records(results)
    assert done == 4 and all(r.config == "A" and r.provider == "retrieval" for r in records)
    assert all(r.metrics.llm_calls == 0 for r in records)
    # A can only copy one field: it can never get fullName (composite) or userId (extract) right
    s1 = next(r for r in records if r.scenario_id == "crm_customer_to_support_user")
    assert not {f.grade.target_field: f for f in s1.fields}["fullName"].grade.fully_correct


def test_finished_units_are_skipped_on_resume(session: Session, tmp_path: Path) -> None:
    done, results = evaluate(session, tmp_path, configs=("A", "B"))
    assert done == 8
    again, _ = evaluate(session, tmp_path, configs=("A", "B"))
    assert again == 0, "nothing is repeated"
    lines = results.read_text(encoding="utf-8").splitlines()
    results.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    resumed, _ = evaluate(session, tmp_path, configs=("A", "B"))
    assert resumed == 1 and len(load_records(results)) == 8


def test_a_rate_limit_stops_the_run_and_keeps_finished_work(
    session: Session, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def flaky(request: LLMRequest) -> str:
        calls["n"] += 1
        if calls["n"] > 12:
            raise RateLimitExhausted("daily token limit", 7200)
        return perfect_reply(request)

    results = tmp_path / "results.jsonl"
    with pytest.raises(RateLimitExhausted):
        run_evaluation(
            session,
            bundles=BUNDLES,
            configs=("B",),
            n_runs=1,
            llm=ScriptedFakeProvider(responder=flaky),
            embedder=FakeEmbeddingProvider(),
            results_path=results,
            samples_dir=SAMPLES_DIR,
            log=lambda message: None,
        )
    finished = load_records(results)
    assert [r.scenario_id for r in finished] == ["crm_customer_to_support_user"]
    assert all(len(r.fields) == 8 for r in finished), "a half-finished unit is never recorded"
    done, _ = evaluate(session, tmp_path, configs=("B",))
    assert done == 3, "the next invocation resumes with the units that are still missing"
    assert len(load_records(results)) == 4


# ---- the resumable provider --------------------------------------------------------------------


class Answer(BaseModel):
    value: int


REQUEST = LLMRequest("sys", ("q",), "answer")


def test_calls_are_saved_by_prompt_hash_and_run_index(tmp_path: Path) -> None:
    inner = ScriptedFakeProvider(responder=lambda r: '{"value": 1}')
    limits = Limits()
    provider = ResumableProvider(inner, tmp_path, limits=limits)
    provider.complete_structured(REQUEST, Answer)
    provider.complete_structured(REQUEST, Answer)
    assert provider.network_calls == 1, "same prompt, same run: served from the store"
    provider.run_index = 2
    provider.complete_structured(REQUEST, Answer)
    assert provider.network_calls == 2, "another run repeats the call, so variance is real"

    after_restart = ResumableProvider(inner, tmp_path, limits=Limits())
    result = after_restart.complete_structured(REQUEST, Answer)
    assert after_restart.network_calls == 0
    assert result.attempts[0].metadata.source == "cache"
    assert result.attempts[0].metadata.input_tokens is not None, "tokens are kept for the totals"


def test_pacing_waits_for_the_window_when_the_next_call_will_not_fit(tmp_path: Path) -> None:
    from app.llm.base import CallMetadata, RawCompletion

    class WithHeaders(ScriptedFakeProvider):
        def complete_raw(
            self, request: LLMRequest, response_model: type[BaseModel]
        ) -> RawCompletion:
            raw = super().complete_raw(request, response_model)
            meta = CallMetadata(
                **{
                    **raw.metadata.__dict__,
                    "input_tokens": 3000,
                    "output_tokens": 500,
                    "rate_limits": {
                        "x-ratelimit-remaining-tokens": "1000",
                        "x-ratelimit-reset-tokens": "1m30.5s",
                    },
                }
            )
            return RawCompletion(raw.text, meta)

    sleeps: list[float] = []
    limits = Limits()
    provider = ResumableProvider(
        WithHeaders(responder=lambda r: '{"value": 1}'),
        tmp_path,
        limits=limits,
        sleep=sleeps.append,
    )
    provider.complete_structured(REQUEST, Answer)
    assert sleeps == [90.0], "waits for the reset, capped"
    assert limits.last_headers["x-ratelimit-remaining-tokens"] == "1000"
    assert Limits.load(tmp_path / "limits.json").last_headers == limits.last_headers


def test_parse_duration() -> None:
    assert parse_duration("2.5s") == 2.5
    assert parse_duration("1m30.5s") == 90.5
    assert parse_duration("120ms") == pytest.approx(0.12)
    assert parse_duration("1h2m") == 3720
    assert parse_duration("garbage") == 0.0


def test_recorded_replays_reproduce_the_whole_pipeline(session: Session, tmp_path: Path) -> None:
    store = tmp_path / "store"
    live = ResumableProvider(ScriptedFakeProvider(responder=perfect_reply), store, limits=Limits())
    s1 = [b for b in BUNDLES if b.scenario.id == "crm_customer_to_support_user"]
    _, first = evaluate(
        session,
        tmp_path / "a",
        bundles=s1,
        configs=("B",),
        llm=live,
        record_scenarios=["crm_customer_to_support_user"],
        replay_dir=tmp_path / "replays",
    )
    replay = ReplayLLMProvider(ResponseStore(tmp_path / "replays"))
    _, second = evaluate(session, tmp_path / "b", bundles=s1, configs=("B",), llm=replay)

    def outcome(path: Path) -> list[tuple[str, bool, str]]:
        return [
            (f.grade.target_field, f.grade.fully_correct, f.proposed)
            for f in load_records(path)[0].fields
        ]

    assert outcome(first) == outcome(second)


# ---- report ------------------------------------------------------------------------------------


def _records(session: Session, tmp_path: Path) -> list[RunRecord]:
    _, results = evaluate(session, tmp_path, n_runs=2)
    return load_records(results)


def test_report_has_every_required_section_and_computed_numbers(
    session: Session, tmp_path: Path
) -> None:
    records = _records(session, tmp_path)
    limits = Limits(last_headers={"x-ratelimit-limit-tokens": "8000"}, rate_limit_waits=[7.0, 3.0])
    text = render_report(records, ReportMeta("2026-01-02", False, 2, limits))
    for heading in (
        "# Mapping evaluation",
        f"## Run set `{LABEL}`",
        "### Overall, by configuration",
        "### Fully correct, by scenario",
        "#### Variation across repeated runs",
        "### Where RAG does and does not help",
        "### Reliability and cost",
        "### Confidence versus accuracy",
        "### Every wrong or UNRESOLVED mapping",
        "## Free-tier limits observed",
    ):
        assert heading in text, heading
    assert "not statistically strong" in text and "about 30 target fields" in text
    assert "Run date (UTC): 2026-01-02" in text
    assert "scripted-fake" in text, "the exact model id comes from the records"
    assert "`x-ratelimit-limit-tokens`: 8000" in text
    assert "429 waits honoured: 2, totalling 10 s" in text
    assert "TEST-ONLY" not in text
    for scenario in ("support_user_to_crm_customer", "crm_v2_to_support_v2"):
        assert f"`{scenario}`" in text
    assert re.search(r"\| B: LLM, full schema \| 64/64 \(100\.0%\)", text), "computed, not typed"


def test_every_wrong_or_unresolved_mapping_is_listed_with_expected_and_proposed(
    session: Session, tmp_path: Path
) -> None:
    records = _records(session, tmp_path)
    text = render_report(records, ReportMeta("d", False, 2))
    section = text.split("### Every wrong or UNRESOLVED mapping")[1].split("## Free-tier")[0]
    assert "| `segment` | UNRESOLVED | UNRESOLVED (the source does not contain" in section
    assert "correct (flagged)" in section, "a correct UNRESOLVED is still listed"
    assert "| `fullName` | COMPOSITE [first_name, last_name] | DIRECT [" in section, (
        "config A mistakes"
    )
    assert "!= expected" in section


def test_test_only_runs_are_marked_and_guarded() -> None:
    text = render_report([], ReportMeta("d", True, 1))
    assert text.startswith("> **TEST-ONLY RUN.**")
    docs = Path("docs/mapping-eval.md")
    assert check_options("groq", False, docs) is None
    assert "tests only" in (check_options("replay", False, Path("x.md")) or "")
    assert check_options("replay", True, Path("x.md")) is None
    assert "applies only to fake" in (check_options("groq", True, docs) or "")
    assert "must not write" in (check_options("replay", True, docs) or "")
    assert "must not write" in (check_options("replay", True, Path("/r") / docs) or "")


def test_the_command_line_refuses_unsafe_combinations(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--provider", "replay"]) == 2
    assert "pass --test-only" in capsys.readouterr().err
    assert main(["--provider", "groq", "--test-only"]) == 2
    assert main(["--provider", "replay", "--test-only", "--output", "docs/mapping-eval.md"]) == 2
    assert main(["--provider", "replay", "--test-only", "--record-replays", "x"]) == 2
    assert main(["--provider", "replay", "--test-only", "--configs", "Z"]) == 2


def test_a_real_run_without_a_key_is_refused_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from app.settings import get_settings

    get_settings.cache_clear()
    code = main(
        ["--provider", "groq", "--configs", "B", "--store-dir", str(tmp_path), "--scenarios", "x"]
    )
    get_settings.cache_clear()
    assert code == 2
    assert "GROQ_API_KEY" in capsys.readouterr().err


def test_main_writes_a_test_only_report_end_to_end(
    test_engine: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "eval.md"
    code = main(
        [
            "--provider", "replay", "--test-only", "--configs", "A,B",
            "--scenarios", "crm_customer_to_support_user",
            "--store-dir", str(tmp_path / "store"), "--output", str(output),
            "--database-url", test_engine.url.render_as_string(hide_password=False),
        ],
        llm_override=ScriptedFakeProvider(responder=perfect_reply),
    )  # fmt: skip
    assert code == 0
    text = output.read_text(encoding="utf-8")
    assert text.startswith("> **TEST-ONLY RUN.**") and "# Mapping evaluation" in text
    assert "wrote" in capsys.readouterr().out
