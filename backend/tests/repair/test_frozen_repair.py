"""Golden values for everything the v0.5 evaluation freezes before the first real call.

A change to any of these is a change to the experiment, not a tuning step: it needs a new,
separately labelled run, a new golden value here and a note in the plan. The v0.4 side (generator,
gate, prompts, validator, D bundles) is pinned by ``tests/codegen/test_frozen_codegen.py``.
"""

import hashlib
import json

from app.codegen.llm_codegen import StrategyProposal, SyncModuleProposal
from app.llm.base import LLMRequest
from app.repair import feedback as fb
from app.repair import guards, sizing, smoke
from app.repair import state as st
from app.repair.feedback import FeedbackItem, Stage, build_feedback
from app.repair.graph import GRAPH_VERSION, NODES, build_graph
from app.repair.nodes import Nodes
from app.repair.prompts import (
    ECHO_CAP_CHARS,
    PROMPT_DIR,
    REPAIR_PROMPT_VERSION,
    RepairPromptBuilder,
)
from tests.codegen.fixtures import s1_input

# A15 (a pre-first-call change; no real call had been made): the L1 repair echo carries the previous
# edge records, numbered as the feedback numbers them. The L1 template hash and the rendered L1R
# hash were re-frozen; the L2 values are unchanged.
PROMPT_FILE_HASHES = {
    "l1_repair.md": "bc8cd2f611372be316b55d2525858636ac23caca21ffdf6a65620eaf7b590492",
    "l2_repair.md": "2de93ae7a81c342384591419e0a08d465332192fb042b7ca085c83ca0ebb5a43",
}
SMOKE_SCRIPT_HASH = "31310f5418d319dba93cec1e0a55a4dd38a60e038da74014657da45990c2f2b4"
RENDERED_REPAIR_HASH = {
    "L2R": "1d2f3e7c54e766f6517824a8307e46afb67652d85544f921170e004ed63fd11b",
    "L1R": "203534a7ef60de2e5e8400e348c5d96883a074c99e4950c818e0144c16e3610e",
}
GRAPH_TOPOLOGY_HASH = "2771b0c400aa20d1e7b866e42d15831bd426c8f1cbabd1c7a44dff5c8357a5ee"
SAMPLE_FEEDBACK = (
    "Feedback on attempt 1: 2 item(s), 0 omitted\n"
    "[GUARD] G3 integration/sync.py:4: lint and type suppression comments\n"
    "[AST] DUNDER_ACCESS integration/sync.py:12: attribute '__cause__'\n"
    "previous: attempt 0: PROPOSAL.INVALID_JSON"
)
NOOP_L2_HASH = "6f0677e251edb5a2cce880270271d43d602d3efffd33c6f0ba73c838ada41fd0"
NOOP_L1_HASH = "f7fa7e6b9d8a01da92112ca24cccaf399cd9cbf6167302608d3102124e8e07ed"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sample_feedback() -> fb.Feedback:
    sync = "integration/sync.py"
    items = [
        FeedbackItem(Stage.AST, "DUNDER_ACCESS", "attribute '__cause__'", sync, 12),
        FeedbackItem(Stage.GUARD, "G3", "lint and type suppression comments", sync, 4),
    ]
    return build_feedback(1, items, history=[(0, ["PROPOSAL.INVALID_JSON"])])


class FixedOriginal(RepairPromptBuilder):
    """The real repair templates around a fixed stand-in for the original task."""

    def initial(self, inp: object, decision: object, condition: str) -> LLMRequest:
        return LLMRequest("SYSTEM", ("ORIGINAL TASK TEXT",), "schema", 0.0, 4000)


# ---- the prompts ---------------------------------------------------------------------------------


def test_the_repair_prompt_files_are_frozen() -> None:
    assert REPAIR_PROMPT_VERSION == "repair-v1"
    found = {
        p.name: sha(p.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
        for p in sorted(PROMPT_DIR.glob("*.md"))
    }
    assert found == PROMPT_FILE_HASHES


def test_the_rendered_repair_prompts_are_frozen() -> None:
    builder = FixedOriginal(temperature=0.0, max_output_tokens=4000)
    l2 = builder.repair(
        None, None, "L2R",  # type: ignore[arg-type]
        attempt=2, previous_output=json.dumps({"notes": "n", "source": "x = 1\n"}),
        feedback=sample_feedback(),
    )  # fmt: skip
    inp = s1_input()
    edges = [json.dumps({**inp.samples[0], "customer_id": "C-9001"}), '{"not": "a source record"}']
    l1 = builder.repair(
        inp, None, "L1R",  # type: ignore[arg-type]
        attempt=1, previous_output=json.dumps({"rationale": "r", "edge_record_json": edges}),
        feedback=sample_feedback(),
    )  # fmt: skip
    assert l2.fingerprint(SyncModuleProposal) == RENDERED_REPAIR_HASH["L2R"]
    assert l1.fingerprint(StrategyProposal) == RENDERED_REPAIR_HASH["L1R"]


def test_the_pre_registered_caps_are_frozen() -> None:
    assert ECHO_CAP_CHARS == 7500
    assert (fb.MAX_ITEMS, fb.MAX_CHARS, fb.MAX_MESSAGE_CHARS, fb.MAX_HISTORY_CHARS) == (
        8, 1200, 200, 240,
    )  # fmt: skip
    assert fb.MIN_SECRET_LENGTH == 6


# ---- feedback, guards, limits --------------------------------------------------------------------


def test_the_feedback_format_is_frozen() -> None:
    assert sample_feedback().render() == SAMPLE_FEEDBACK
    assert [s.value for s in Stage] == [
        "PROPOSAL", "GUARD", "AST", "RUFF", "MYPY", "TESTS", "SMOKE",
    ]  # fmt: skip


def test_the_guard_set_modes_and_hash_rules_are_frozen() -> None:
    assert {g: m.value for g, m in guards.GUARD_MODES.items()} == {
        "G1": "ENFORCED", "G2": "ENFORCED", "G3": "ENFORCED", "G4": "ENFORCED", "G5": "ENFORCED",
        "G6": "SHADOW", "G7": "SHADOW",
    }  # fmt: skip
    assert guards.output_hash_l2("def run(keys):\n    return 1\n") == NOOP_L2_HASH
    assert guards.output_hash_l1({"a": 1, "edge_record_json": ['{"y":2,"x":1}']}) == NOOP_L1_HASH
    # G4 (amendment A14): a count only exists for source that parses
    assert guards.type_escape_count("x: object = 1\n") == 1
    assert guards.type_escape_count("x = (\n") is None


def test_the_limits_and_the_size_policy_are_frozen() -> None:
    assert (st.MAX_REPAIR_ATTEMPTS, st.MAX_PAUSES) == (3, 5)
    assert (sizing.CHARS_PER_TOKEN, sizing.HARD_STOP_FACTOR, sizing.DEFAULT_TPM_LIMIT) == (
        4.10, 1.10, 8000,
    )  # fmt: skip
    assert sizing.EXPECTED_OUTPUT_TOKENS == {"L1R": 1175, "L2R": 1859}
    assert smoke.SMOKE_TIMEOUT_S == 20.0
    assert sha(smoke._SCRIPT.encode("utf-8")) == SMOKE_SCRIPT_HASH


# ---- the graph -----------------------------------------------------------------------------------


def test_the_graph_version_and_topology_are_frozen() -> None:
    assert GRAPH_VERSION == "repair-graph-1"
    assert NODES == (
        "plan", "propose", "validate", "build", "guard", "gates", "tests", "smoke", "feedback",
        "decide",
    )  # fmt: skip
    graph = build_graph(Nodes(None), None).get_graph()  # type: ignore[arg-type]
    edges = sorted((e.source, e.target, bool(e.conditional)) for e in graph.edges)
    assert sha(json.dumps(edges).encode("utf-8")) == GRAPH_TOPOLOGY_HASH
