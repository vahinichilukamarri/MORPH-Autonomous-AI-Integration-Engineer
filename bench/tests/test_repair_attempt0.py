"""Fixed start depends on this: attempt 0 of the repair builder IS the recorded v0.4 prompt.

For each of the six v0.4 units the prompt hash the repair builder produces for attempt 0 must equal
the prompt hash stored with the recorded reply, or the injected replay could not find it.
"""

import json
from pathlib import Path

import pytest
from app.codegen.inputs import load_input
from app.codegen.llm_codegen import StrategyProposal, SyncModuleProposal
from app.codegen.operations import analyse
from app.codegen.review_gate import decide
from app.llm.store import ReplayLLMProvider, ResponseStore
from app.repair.prompts import RepairPromptBuilder
from app.settings import get_settings
from sqlalchemy.orm import Session

from morph_bench.oracle.build import SAMPLES_DIR, seed_mapping_run
from morph_bench.oracle.models import load_fixture

REPLAYS = Path(__file__).resolve().parents[1] / "replays" / "codegen"
CALLS = [json.loads(line) for line in (REPLAYS / "calls.jsonl").read_text("utf-8").splitlines()]
# (scenario, condition, index of the unit's first call in calls.jsonl)
UNITS = [
    ("crm_customer_to_support_user", "L1R", 0),
    ("crm_customer_to_support_user", "L2R", 2),
    ("crm_v2_to_support_v2", "L1R", 3),
    ("crm_v2_to_support_v2", "L2R", 5),
    ("support_user_to_crm_customer", "L1R", 6),
    ("support_user_to_crm_customer", "L2R", 8),
]


@pytest.mark.parametrize(("scenario", "condition", "index"), UNITS)
def test_attempt_zero_is_the_recorded_v0_4_prompt(
    session: Session, scenario: str, condition: str, index: int
) -> None:
    settings = get_settings()
    run_id = seed_mapping_run(session, load_fixture(scenario), with_override=True)
    inp = load_input(session, run_id, SAMPLES_DIR)
    decision = decide(inp, analyse(inp.source, inp.source_entity, inp.target, inp.target_entity),
                      allow_partial=False)  # fmt: skip
    builder = RepairPromptBuilder(
        temperature=settings.llm_temperature, max_output_tokens=settings.llm_max_output_tokens
    )
    request = builder.initial(inp, decision, condition)
    model = StrategyProposal if condition == "L1R" else SyncModuleProposal
    assert request.fingerprint(model) == CALLS[index]["prompt_hash"]
    # and the injected replay really serves it
    replay = ReplayLLMProvider(ResponseStore(REPLAYS))
    served = replay.complete_raw(request, model)
    assert served.text == CALLS[index]["text"] and served.metadata.source == "replay"
