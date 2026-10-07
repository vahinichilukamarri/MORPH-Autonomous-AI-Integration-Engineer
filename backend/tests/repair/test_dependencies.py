"""M0 dependency spike: LangGraph and its Postgres checkpointer work here, and stay contained.

The spike graph is a stand-in with no MORPH logic. It proves what the repair loop will rely on:
a state graph runs, its position is stored in the project's own Postgres, and a run that dies in
a node resumes at that node without redoing the finished ones. LangGraph is imported only in
``app/repair`` (not yet written) and in this test; nothing on the D path may load it.
"""

import ast
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import Engine

APP = Path(__file__).resolve().parents[2] / "app"
BACKEND = APP.parent


class State(TypedDict):
    trail: list[str]


def _conninfo(engine: Engine) -> str:
    return engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


def test_a_run_that_dies_in_a_node_resumes_there_from_postgres(test_engine: Engine) -> None:
    calls: list[str] = []
    fail = {"second": True}

    def first(state: State) -> State:
        calls.append("first")
        return {"trail": [*state["trail"], "first"]}

    def second(state: State) -> State:
        calls.append("second")
        if fail["second"]:
            raise RuntimeError("process died")
        return {"trail": [*state["trail"], "second"]}

    builder = StateGraph(State)
    builder.add_node("first", first)
    builder.add_node("second", second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)

    config: RunnableConfig = {"configurable": {"thread_id": "spike-resume"}}
    with PostgresSaver.from_conn_string(_conninfo(test_engine)) as saver:
        saver.setup()
        graph = builder.compile(checkpointer=saver)
        with pytest.raises(RuntimeError, match="process died"):
            graph.invoke({"trail": []}, config)
        fail["second"] = False
        # a new graph object over the same database: nothing is carried in memory
        resumed = builder.compile(checkpointer=saver).invoke(None, config)
    assert resumed["trail"] == ["first", "second"]
    assert calls == ["first", "second", "second"]  # "first" ran once; "second" was retried


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_langgraph_is_imported_only_under_app_repair() -> None:
    offenders = [
        str(path.relative_to(APP))
        for path in APP.rglob("*.py")
        if "repair" not in path.relative_to(APP).parts[:1] and "langgraph" in _imports(path)
    ]
    assert offenders == []


def test_the_d_path_does_not_load_langgraph() -> None:
    code = (
        "import sys, app.codegen.service, app.codegen.generator, app.codegen.gate;"
        "banned = ('langgraph', 'langchain_core');"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in banned);"
        "print(bad)"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "[]"
