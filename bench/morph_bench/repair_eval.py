"""Results, budgets and fail-closed checks of the v0.5 repair evaluation.

Pure with respect to models and Docker. Nothing here reads an answer key; the oracle is called only
by the harness script, after a repair run has ended, and its result is stored next to the run's
outcome and never fed back. Honesty rules: units stopped before a call are results ("not run", kept
in every denominator), a unit that reached READY but fails the oracle is "gate-passing, incorrect",
and no number is computed here that is not a count from a saved run.
"""

import json
import math
from collections.abc import Sequence
from pathlib import Path

from app.db_models import LLMCall, RepairAttempt, RepairRun
from app.llm.base import BaseLLMProvider, CallMetadata, LLMRequest, RawCompletion
from app.repair.sizing import CHARS_PER_TOKEN, request_characters
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from morph_bench.codegen_eval import oracle_summary

SHORT = {
    "crm_customer_to_support_user": "S1",
    "support_user_to_crm_customer": "S3",
    "crm_v2_to_support_v2": "S4",
}
PHASES = ("fixed", "fresh")
PHASE_CALL_CAPS = {
    "fixed": 20,
    "fresh": 26,
}  # the plan's hard caps on real calls (worst case 18, 24)

# What the v0.4 failure of each unit was attributable to, fixed in the plan before any run.
EXPOSURE = {
    ("S1", "L1R"): "PROMPT_GAP",  # the frozen L1 prompt never states the UPSERT field-list rule
    ("S4", "L1R"): "PROMPT_GAP",
    ("S3", "L1R"): "SECRET_LITERAL_FALSE_POSITIVE",
    ("S1", "L2R"): "CAUSE_CONTRADICTION",  # the L2 prompt asks for __cause__, the gate bans it
    ("S3", "L2R"): "MODEL",  # a stray closing brace
    ("S4", "L2R"): "MODEL",
}


class MissingUsageError(Exception):
    """A real call came back without a usage block or total_tokens: stop, never guess."""


class BudgetExhausted(Exception):
    """A cap on real calls or tokens was reached. Not an error of the unit: the run can resume."""

    def __init__(self, which: str, detail: str) -> None:
        super().__init__(f"{which}: {detail}")
        self.which = which


def exposure_tag(scenario_id: str, condition: str) -> str:
    return EXPOSURE.get((SHORT.get(scenario_id, scenario_id), condition), "UNTAGGED")


# ---- fail closed on missing usage ----------------------------------------------------------------


def require_usage(meta: CallMetadata) -> None:
    if meta.usage is None or meta.total_tokens is None:
        raise MissingUsageError(
            f"a call to {meta.provider}/{meta.model} returned without usage or total_tokens"
        )


def check_recorded_calls(store_dir: Path) -> list[str]:
    """Every stored real call must carry usage and total_tokens; the problems found, if any."""
    path = store_dir / "calls" / "calls.jsonl"
    if not path.is_file():
        return []
    problems = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("usage") is None or record.get("total_tokens") is None:
            problems.append(f"{path.name} line {number}: no usage or total_tokens")
    return problems


# ---- caps on real calls and tokens ---------------------------------------------------------------


class BudgetState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: int = 0
    tokens: int = 0  # input + output + reasoning of every real call: an upper bound

    @staticmethod
    def load(path: Path) -> "BudgetState":
        if not path.is_file():
            return BudgetState()
        return BudgetState.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8", newline="\n")


class BudgetedProvider(BaseLLMProvider):
    """Counts real calls and tokens across resumes, and refuses a call that would pass a cap.

    It sits *inside* the caching provider, so it only ever sees calls that really go to the model.
    Tokens count reasoning as well, and the check adds an estimate of the request's own size before
    the call; the completion that follows can still overshoot the cap by at most one completion.
    """

    def __init__(
        self, inner: BaseLLMProvider, state_path: Path, *, max_calls: int, max_tokens: int | None
    ) -> None:
        self._inner = inner
        self._path = state_path
        self.state = BudgetState.load(state_path)
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.name = inner.name

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        if self.state.calls >= self.max_calls:
            raise BudgetExhausted("max-real-calls", f"{self.state.calls} of {self.max_calls} used")
        if self.max_tokens is not None:
            size = math.ceil(request_characters(request, response_model) / CHARS_PER_TOKEN)
            if self.state.tokens + size >= self.max_tokens:
                raise BudgetExhausted(
                    "max-real-tokens",
                    f"{self.state.tokens} used, this request alone is about {size}, "
                    f"cap {self.max_tokens}",
                )
        raw = self._inner.complete_raw(request, response_model)
        meta = raw.metadata
        self.state.calls += 1
        self.state.tokens += (
            (meta.input_tokens or 0) + (meta.output_tokens or 0) + (meta.reasoning_tokens or 0)
        )
        self.state.save(self._path)
        require_usage(meta)
        return raw


# ---- results -------------------------------------------------------------------------------------


class AttemptSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int
    source: str
    failed_stage: str | None
    feedback_codes: list[str] = Field(default_factory=list)
    guard_enforced: list[str] = Field(default_factory=list)
    guard_shadow: list[str] = Field(default_factory=list)
    output_hash: str
    prompt_hash: str | None = None  # links the attempt to its reply in the committed replays
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    latency_ms: int | None
    size_estimate_tokens: int | None


class RepairUnitResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    condition: str
    start_mode: str
    run_id: int
    status: str
    reason: str | None
    model_turns: int
    pauses: int
    real_calls: int
    exposure: str
    attempts: list[AttemptSummary] = Field(default_factory=list)
    oracle: dict[str, tuple[int, int]] | None = None
    oracle_failed_checks: list[str] = Field(default_factory=list)
    integration_correct: bool | None = None
    elapsed_s: float = 0.0
    test_only: bool = False

    @property
    def key(self) -> str:
        return f"{self.scenario_id}|{self.condition}|{self.start_mode}"

    @property
    def label(self) -> str:
        if self.status == "READY" and self.integration_correct is False:
            return "gate-passing, incorrect"
        if self.status == "INFRA_STOPPED":
            return "not run"
        return self.status


def load_results(path: Path) -> list[RepairUnitResult]:
    if not path.is_file():
        return []
    latest: dict[str, RepairUnitResult] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            result = RepairUnitResult.model_validate_json(line)
            latest[result.key] = result
    return list(latest.values())


def append_result(path: Path, result: RepairUnitResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(result.model_dump_json() + "\n")


def summarise_attempts(session: Session, run_id: int) -> list[AttemptSummary]:
    """What each attempt of a finished (or paused) run did, from the stored rows only."""
    rows = session.scalars(
        select(RepairAttempt)
        .where(RepairAttempt.repair_run_id == run_id)
        .order_by(RepairAttempt.attempt)
    ).all()
    out = []
    for row in rows:
        call = session.get(LLMCall, row.llm_call_id) if row.llm_call_id else None
        guards = row.guard_result or []
        out.append(
            AttemptSummary(
                attempt=row.attempt,
                source=row.source,
                failed_stage=row.failed_stage,
                feedback_codes=[
                    f"{i['stage']}.{i['code']}" for i in (row.feedback or {}).get("items", [])
                ],
                guard_enforced=sorted(g["guard"] for g in guards if g["mode"] == "ENFORCED"),
                guard_shadow=sorted(g["guard"] for g in guards if g["mode"] == "SHADOW"),
                output_hash=row.output_hash,
                prompt_hash=call.prompt_hash if call else None,
                finish_reason=row.finish_reason,
                input_tokens=call.input_tokens if call else None,
                output_tokens=call.output_tokens if call else None,
                reasoning_tokens=call.reasoning_tokens if call else None,
                total_tokens=row.total_tokens,
                latency_ms=call.latency_ms if call else None,
                size_estimate_tokens=(row.size_estimate or {}).get("total_tokens"),
            )
        )
    return out


def summarise_run(
    session: Session, run: RepairRun, *, scenario_id: str, start_mode: str, test_only: bool
) -> RepairUnitResult:
    attempts = summarise_attempts(session, run.id)
    turns = sum(1 for a in run.attempts if a.output_text)
    return RepairUnitResult(
        scenario_id=scenario_id,
        condition=run.condition,
        start_mode=start_mode,
        run_id=run.id,
        status=run.status,
        reason=run.terminal_reason,
        model_turns=turns,
        pauses=run.pauses,
        real_calls=sum(1 for a in attempts if a.source == "network"),
        exposure=exposure_tag(scenario_id, run.condition),
        attempts=attempts,
        test_only=test_only,
    )


def apply_grading(result: RepairUnitResult, checks: Sequence[dict[str, object]]) -> None:
    """Store the oracle's verdict on a unit that reached READY. Called once, after the loop."""
    summary, failed, correct = oracle_summary(checks)
    result.oracle = summary
    result.oracle_failed_checks = failed
    result.integration_correct = correct
