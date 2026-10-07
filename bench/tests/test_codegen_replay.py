"""L1 and L2 replayed from the real Groq run: CI reproduces every recorded outcome without a model.

The replies in replays/codegen/ are the real replies of the evaluation run. If a prompt, the
response schema, the generator or the gate changes, a replay misses (or the outcome differs) and
this test fails loudly: re-record with a real run and report it as a separate, labelled run.
"""

import json
import re
from pathlib import Path

import pytest
from app.codegen.service import generate
from app.llm.store import ReplayLLMProvider, ResponseStore
from app.settings import get_settings
from sqlalchemy.orm import Session

from morph_bench.oracle.build import SAMPLES_DIR, seed_mapping_run
from morph_bench.oracle.models import load_fixture
from scripts.run_codegen_eval import response_model

REPLAYS = Path(__file__).resolve().parents[1] / "replays" / "codegen"
EXPECTED = json.loads((REPLAYS / "expected.json").read_text(encoding="utf-8"))


def plain(text: str) -> str:
    return re.sub(r" \([^()]*line \d+\)", "", text)


APPROVED = [e for e in EXPECTED if e["input_set"] == "approved"]


def test_the_replay_fixtures_hold_every_real_reply_and_no_secret() -> None:
    text = (REPLAYS / "calls.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert len(records) == sum(e["calls"] for e in EXPECTED) == 9
    assert {r["provider"] for r in records} == {"groq"}
    assert {r["model"] for r in records} == {"openai/gpt-oss-120b"}
    lowered = text.lower()
    assert "gsk_" not in lowered and "authorization" not in lowered and "api_key" not in lowered


def test_units_that_block_before_the_model_cost_zero_calls() -> None:
    blocked = [e for e in EXPECTED if e["input_set"] == "as_proposed"]
    assert len(blocked) == 6
    assert all(e["status"] == "BLOCKED_PENDING_REVIEW" and e["calls"] == 0 for e in blocked)
    assert all(e["blocked_reasons"] for e in blocked)


@pytest.mark.parametrize(
    "unit", APPROVED, ids=[f"{e['scenario_id'][:6]}-{e['condition']}" for e in APPROVED]
)
def test_replayed_unit_reproduces_the_recorded_outcome(
    session: Session, unit: dict[str, object]
) -> None:
    settings = get_settings()
    fixture = load_fixture(str(unit["scenario_id"]))
    run_id = seed_mapping_run(session, fixture, with_override=True)
    llm = ReplayLLMProvider(ResponseStore(REPLAYS))
    version = generate(
        session, run_id, condition=str(unit["condition"]), samples_dir=SAMPLES_DIR, llm=llm,
        temperature=settings.llm_temperature, max_output_tokens=settings.llm_max_output_tokens,
    )  # fmt: skip
    assert version.status == unit["status"]
    gate = {g.stage: g.passed for g in version.gate_results}
    assert gate == unit["gate"]
    findings = [
        f"{g.stage}: {f['rule']} {f['message']}"[:200]
        for g in version.gate_results
        for f in g.findings
    ][:6]
    # Python writes the file name into a SyntaxError message differently on Windows and Linux
    assert [plain(f) for f in findings] == [plain(str(f)) for f in unit["gate_findings"]]  # type: ignore[attr-defined]
    assert response_model(session, version) == "openai/gpt-oss-120b"  # from the recorded reply
    info = version.manifest["llm"]
    assert info["calls"] == unit["calls"]
    assert info["input_tokens"] == unit["input_tokens"]
    assert info["output_tokens"] == unit["output_tokens"]
    assert info["reasoning_tokens"] == unit["reasoning_tokens"]
    assert not version.status.startswith("READY")  # no replayed unit ever reached the sandbox
