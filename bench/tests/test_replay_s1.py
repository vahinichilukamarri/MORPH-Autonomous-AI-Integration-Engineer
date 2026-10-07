"""The whole mapping pipeline for scenario 1, replayed from real recorded model responses.

The recorded replies in bench/replays/ come from a real run; CI replays them with no network.
Full-schema mode only: its prompts do not contain retrieval results, so they do not depend on
which embedding model is used (CI uses fake embeddings). If a prompt, the schema or the sample
selection changes, the replay misses loudly and the fixtures must be re-recorded (see
docs/milestones.md, "Re-record replays").
"""

import json
from pathlib import Path

from app.embeddings.fake import FakeEmbeddingProvider
from app.llm.store import ReplayLLMProvider, ResponseStore
from sqlalchemy.orm import Session

from morph_bench.loader import SCENARIOS_ROOT, load_bundle
from morph_bench.mapping_eval import RunRecord, load_records
from scripts.run_mapping_eval import run_evaluation

REPLAYS = Path(__file__).resolve().parents[1] / "replays"
SCENARIO = "crm_customer_to_support_user"
SAMPLES_DIR = Path(__file__).resolve().parents[2] / "mock_systems" / "samples"


def replay_once(session: Session, results: Path) -> RunRecord:
    run_evaluation(
        session,
        bundles=[load_bundle(SCENARIOS_ROOT / SCENARIO)],
        configs=("B",),
        n_runs=1,
        llm=ReplayLLMProvider(ResponseStore(REPLAYS)),
        embedder=FakeEmbeddingProvider(),
        results_path=results,
        samples_dir=SAMPLES_DIR,
        log=lambda message: None,
    )
    return load_records(results)[0]


def outcome(record: RunRecord) -> list[dict[str, object]]:
    return [
        {
            "target_field": f.grade.target_field,
            "fully_correct": f.grade.fully_correct,
            "proposed": f.proposed,
        }
        for f in record.fields
    ]


def test_the_recorded_run_replays_to_exactly_the_committed_outcome(
    session: Session, tmp_path: Path
) -> None:
    record = replay_once(session, tmp_path / "a.jsonl")
    expected = json.loads((REPLAYS / f"{SCENARIO}.expected.json").read_text(encoding="utf-8"))
    assert outcome(record) == expected["full_schema"]
    assert record.provider == "groq" and record.model == expected["model"]
    assert record.metrics.llm_calls == len(record.fields), "one recorded reply per target field"


def test_replaying_twice_gives_identical_results(session: Session, tmp_path: Path) -> None:
    first = replay_once(session, tmp_path / "a.jsonl")
    second = replay_once(session, tmp_path / "b.jsonl")
    assert outcome(first) == outcome(second)
    assert [f.grade for f in first.fields] == [f.grade for f in second.fields]
