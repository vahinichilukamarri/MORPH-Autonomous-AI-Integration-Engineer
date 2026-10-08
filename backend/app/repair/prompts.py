"""The real prompt builder: the v0.4 prompt for attempt 0, the repair-v1 prompt for repairs 1 to 3.

A repair prompt is the repair template (instructions only) plus three delimited data blocks: the
original rendered task, the previous reply and the check results with the history. Nothing from a
model reply, a gate or a spec is ever placed in the instruction text; the system text is the frozen
v0.4 one, which already tells the model that delimited blocks are data. The wording and the caps are
frozen by ``tests/repair/test_frozen_repair.py`` before any real run.

Pre-registered size mitigation (plan, section 7): feedback is capped at 1,200 characters by
``feedback.py``; the previous-reply echo is capped here at 7,500 characters.
"""

import json
import re
from pathlib import Path

from app.codegen.inputs import CodegenInput
from app.codegen.llm_codegen import build_l1_request, build_l2_request
from app.codegen.review_gate import GateDecision
from app.llm.base import LLMRequest
from app.mapping.prompts import BLOCK_CLOSE, BLOCK_OPEN
from app.repair.feedback import Feedback
from app.repair.state import MAX_REPAIR_ATTEMPTS

REPAIR_PROMPT_VERSION = "repair-v1"
PROMPT_DIR = Path(__file__).resolve().parent / "prompts" / REPAIR_PROMPT_VERSION
TEMPLATES = {"L1R": "l1_repair.md", "L2R": "l2_repair.md"}
ECHO_CAP_CHARS = 7500
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def neutralise(text: str) -> str:
    """Make it impossible for data to contain a block delimiter (the v0.4 rule)."""
    return text.replace("<<<", "‹‹‹").replace(">>>", "›››")


def text_block(name: str, text: str) -> str:
    return f"{BLOCK_OPEN.format(name=name)}\n{neutralise(text)}\n{BLOCK_CLOSE}"


def cap(text: str, limit: int = ECHO_CAP_CHARS) -> str:
    """The first ``limit`` characters, with a visible marker saying how much was left out."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[truncated: {len(text) - limit} more characters]"


def echo_previous(condition: str, previous_output: str) -> str:
    """What the model sees of its own previous reply.

    L2R: the module source alone, as plain text (not JSON-escaped). L1R: the strategy with its edge
    records replaced by a count. A reply that is not a JSON object is shown as it came. Always
    capped.
    """
    try:
        data = json.loads(previous_output)
    except ValueError:
        return cap(previous_output)
    if not isinstance(data, dict):
        return cap(previous_output)
    if condition == "L2R" and isinstance(data.get("source"), str):
        return cap(data["source"])
    if condition == "L1R":
        shown = dict(data)
        edges = shown.get("edge_record_json")
        if isinstance(edges, list):
            shown["edge_record_json"] = f"[{len(edges)} edge records left out]"
        return cap(json.dumps(shown, indent=2, ensure_ascii=False))
    return cap(previous_output)


class RepairPromptBuilder:
    """Implements the ``PromptBuilder`` protocol."""

    def __init__(self, *, temperature: float = 0.0, max_output_tokens: int | None = None) -> None:
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens

    def initial(self, inp: CodegenInput, decision: GateDecision, condition: str) -> LLMRequest:
        """Exactly the v0.4 request, so a fixed start finds its recorded reply by prompt hash."""
        build = build_l1_request if condition == "L1R" else build_l2_request
        return build(
            inp, decision, temperature=self.temperature, max_output_tokens=self.max_output_tokens
        )

    def repair(
        self,
        inp: CodegenInput,
        decision: GateDecision,
        condition: str,
        *,
        attempt: int,
        previous_output: str,
        feedback: Feedback,
    ) -> LLMRequest:
        original = self.initial(inp, decision, condition)
        values = {
            "ATTEMPT": str(attempt),
            "MAX_ATTEMPTS": str(MAX_REPAIR_ATTEMPTS),
            "ORIGINAL_TASK": text_block("ORIGINAL_TASK", "\n\n".join(original.parts)),
            "PREVIOUS_REPLY": text_block(
                "PREVIOUS_REPLY", echo_previous(condition, previous_output)
            ),
            "CHECK_RESULTS": text_block("CHECK_RESULTS", feedback.render()),
        }
        template = (PROMPT_DIR / TEMPLATES[condition]).read_text(encoding="utf-8")
        # one pass, so text inside a block can never be mistaken for a placeholder
        text = _PLACEHOLDER.sub(lambda m: values[m.group(1)], template).strip()
        return LLMRequest(
            system=original.system,
            parts=(text,),
            schema_name=original.schema_name,
            temperature=original.temperature,
            max_output_tokens=original.max_output_tokens,
        )
