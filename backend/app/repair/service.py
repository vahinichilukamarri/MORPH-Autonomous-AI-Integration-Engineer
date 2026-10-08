"""Start and resume repair runs. Everything a run needs from the outside is injected.

A fixed-start run takes attempt 0 only from the injected replay provider: a missing replay aborts
the run and nothing else is called. This module builds no provider of its own.
"""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy.orm import Session

from app.codegen.inputs import CodegenInput
from app.codegen.sandbox import SandboxRunner
from app.db_models import RepairRun
from app.llm.base import BaseLLMProvider
from app.llm.store import CachingProvider, ResponseStore
from app.repair.builder import PromptBuilder
from app.repair.graph import GRAPH_VERSION, build_graph
from app.repair.nodes import Deps, Nodes
from app.repair.sizing import (
    CHARS_PER_TOKEN,
    DEFAULT_TPM_LIMIT,
    EXPECTED_OUTPUT_TOKENS,
    HARD_STOP_FACTOR,
)
from app.repair.state import (
    CONDITIONS,
    TERMINAL,
    Paused,
    RepairState,
    RunStatus,
    StartMode,
)

TRACING_VARIABLES = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
)
_OFF = {"", "0", "false", "no", "off"}


class TracingEnabled(Exception):
    """A tracing variable is set: a repair run would send prompts and outputs to a third party."""


def refuse_if_tracing() -> None:
    enabled = [v for v in TRACING_VARIABLES if os.environ.get(v, "").strip().lower() not in _OFF]
    if enabled:
        raise TracingEnabled(f"unset {', '.join(enabled)} before a repair run")


def ensure_checkpoint_schema(conninfo: str) -> None:
    """Create the checkpointer's tables. Call once at startup, outside any open transaction.

    The checkpointer builds an index with ``CREATE INDEX CONCURRENTLY``, which waits for every
    transaction that is open in the database. Running it from inside a run would hang behind the
    run's own session, so a run never does it and fails fast if the tables are missing.
    """
    with PostgresSaver.from_conn_string(conninfo) as saver:
        saver.setup()


@dataclass
class RepairEnv:
    session: Session
    inp: CodegenInput  # with mapping_run_id set
    condition: str
    start_mode: StartMode
    builder: PromptBuilder
    live: BaseLLMProvider  # the real provider (or a scripted one in tests)
    response_store: ResponseStore  # every reply from `live` is stored here before it is used
    runner: SandboxRunner
    smoke_runner: SandboxRunner
    conninfo: str  # the LangGraph checkpointer's Postgres connection
    seed: BaseLLMProvider | None = None  # fixed start: a replay provider, nothing else
    tpm_limit: int = DEFAULT_TPM_LIMIT
    secrets: Sequence[str] = ()
    after_call: Any = None  # a test hook, called with the attempt number after a reply is stored
    on_created: Callable[[int], None] | None = None  # told the run id as soon as the row exists


@dataclass(frozen=True)
class RepairResult:
    run_id: int
    status: str
    reason: str | None
    attempts: int  # model turns used (rows with a reply)
    pauses: int


def _result(env: RepairEnv, run: RepairRun) -> RepairResult:
    env.session.refresh(run)
    turns = sum(1 for a in run.attempts if a.output_text)
    return RepairResult(run.id, run.status, run.terminal_reason, turns, run.pauses)


def _check(env: RepairEnv) -> None:
    if env.condition not in CONDITIONS:
        raise ValueError(f"condition {env.condition!r} is not a repair condition")
    if env.inp.mapping_run_id is None:
        raise ValueError("the input needs a mapping_run_id")
    if env.start_mode is StartMode.FIXED and env.seed is None:
        raise ValueError("a fixed-start run needs an injected replay provider for attempt 0")
    if env.start_mode is StartMode.FRESH and env.seed is not None:
        raise ValueError("a fresh-start run must not be given a replay provider")


def _initial_state(env: RepairEnv, run: RepairRun) -> RepairState:
    return {
        "run_id": run.id,
        "condition": env.condition,
        "attempt": 0,
        "output_hashes": [],
        "history": [],
        "pending": [],
        "failed_stage": None,
        "terminal": None,
        "terminal_reason": None,
    }


def _drive(env: RepairEnv, run: RepairRun) -> RepairResult:
    deps = Deps(
        session=env.session,
        inp=env.inp,
        condition=env.condition,
        builder=env.builder,
        live=CachingProvider(env.live, env.response_store),
        seed=env.seed,
        runner=env.runner,
        smoke_runner=env.smoke_runner,
        tpm_limit=env.tpm_limit,
        secrets=env.secrets,
        after_call=env.after_call,
    )
    config: RunnableConfig = {
        "configurable": {"thread_id": str(run.id)},
        "recursion_limit": 100,
    }
    with PostgresSaver.from_conn_string(env.conninfo) as saver:
        graph = build_graph(Nodes(deps), saver)
        started = bool(graph.get_state(config).values)
        try:
            graph.invoke(None if started else _initial_state(env, run), config)
        except Paused:
            pass  # recorded on the run; resume_repair continues from the checkpoint
    return _result(env, run)


def run_repair(env: RepairEnv) -> RepairResult:
    """Create the run and drive it until it ends or pauses."""
    refuse_if_tracing()
    _check(env)
    assert env.inp.mapping_run_id is not None
    run = RepairRun(
        mapping_run_id=env.inp.mapping_run_id,
        condition=env.condition,
        start_mode=env.start_mode.value,
        status=RunStatus.RUNNING.value,
        pauses=0,
        size_policy={
            "tpm_limit": env.tpm_limit,
            "hard_stop_factor": HARD_STOP_FACTOR,
            "chars_per_token": CHARS_PER_TOKEN,
            "expected_output_tokens": EXPECTED_OUTPUT_TOKENS[env.condition],
            "graph_version": GRAPH_VERSION,
        },
    )
    env.session.add(run)
    env.session.commit()
    if env.on_created is not None:
        env.on_created(run.id)
    return _drive(env, run)


def resume_repair(env: RepairEnv, run_id: int) -> RepairResult:
    """Continue a paused or interrupted run from its checkpoint; a finished run is not reopened."""
    refuse_if_tracing()
    _check(env)
    run = env.session.get(RepairRun, run_id)
    if run is None:
        raise ValueError(f"no repair run {run_id}")
    if RunStatus(run.status) in TERMINAL:
        return _result(env, run)
    run.status = RunStatus.RUNNING.value
    env.session.commit()
    return _drive(env, run)
