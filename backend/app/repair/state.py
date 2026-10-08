"""The repair run's JSON-only state, its statuses and its limits.

The LangGraph checkpoint stores this state and nothing else: it is the position in the graph.
Content (model outputs, feedback, bundles, gate results) lives in our own tables. Everything here
must be JSON-serialisable.
"""

from enum import StrEnum
from typing import Any, TypedDict

from app.repair.feedback import FeedbackItem, Stage

MAX_REPAIR_ATTEMPTS = 3  # repairs after attempt 0, so at most 4 model turns
MAX_PAUSES = 5  # infrastructure pauses per unit; one more stops the unit

L1R = "L1R"
L2R = "L2R"
CONDITIONS = (L1R, L2R)


class StartMode(StrEnum):
    FIXED = "fixed"  # attempt 0 comes only from an injected replay
    FRESH = "fresh"  # attempt 0 is a new model call


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"  # not a result: the run can be resumed
    READY = "READY"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    BLOCKED_PENDING_REVIEW = "BLOCKED_PENDING_REVIEW"
    BLOCKED_UNSUPPORTED = "BLOCKED_UNSUPPORTED"
    INFRA_STOPPED = "INFRA_STOPPED"  # "not run": kept in every denominator, never a model result
    ABORTED = "ABORTED"  # a hard error, for example a missing fixed-start replay


TERMINAL = frozenset(RunStatus) - {RunStatus.RUNNING, RunStatus.PAUSED}


class Paused(Exception):
    """An infrastructure failure: the run stops here and can be resumed from its checkpoint."""


class HardError(Exception):
    """The run cannot continue and must not fall back to anything else (no live call, no retry)."""


class RepairState(TypedDict, total=False):
    run_id: int
    condition: str
    attempt: int  # 0 is the initial generation; 1 to 3 are repairs
    output_hashes: list[str]  # of every earlier attempt's output, for no-op detection
    history: list[list[Any]]  # [[attempt, ["STAGE.CODE", ...]], ...]
    pending: list[dict[str, Any]]  # the failing items of the current attempt
    failed_stage: str | None
    terminal: str | None
    terminal_reason: str | None


def item_to_dict(item: FeedbackItem) -> dict[str, Any]:
    return {
        "stage": item.stage.value,
        "code": item.code,
        "message": item.message,
        "file": item.file,
        "line": item.line,
    }


def item_from_dict(data: dict[str, Any]) -> FeedbackItem:
    return FeedbackItem(
        Stage(data["stage"]), data["code"], data["message"], data.get("file"), data.get("line")
    )
