"""The repair state graph.

Failures route to ``feedback`` and then ``decide``; a terminal state ends the graph. A pause is an
exception raised inside a node, so the checkpoint still points at that node and a resume runs it
again.
"""

from collections.abc import Callable, Hashable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.repair.nodes import Nodes
from app.repair.state import RepairState

NODES = (
    "plan",
    "propose",
    "validate",
    "build",
    "guard",
    "gates",
    "tests",
    "smoke",
    "feedback",
    "decide",
)
GRAPH_VERSION = "repair-graph-1"


def _route(next_node: str) -> Callable[[RepairState], str]:
    def route(state: RepairState) -> str:
        if state.get("terminal"):
            return END
        if state.get("pending"):
            return "feedback"
        return next_node

    return route


def build_graph(
    nodes: Nodes, checkpointer: BaseCheckpointSaver[Any] | None
) -> CompiledStateGraph[RepairState, Any, RepairState, RepairState]:
    graph = StateGraph(RepairState)
    for name in NODES:
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "plan")
    chain = {
        "plan": "propose",
        "propose": "validate",
        "validate": "build",
        "build": "guard",
        "guard": "gates",
        "gates": "tests",
        "tests": "smoke",
        "smoke": END,
    }
    for source, target in chain.items():
        destinations: dict[Hashable, str] = {target: target, "feedback": "feedback", END: END}
        graph.add_conditional_edges(source, _route(target), destinations)
    graph.add_edge("feedback", "decide")
    graph.add_conditional_edges("decide", _route("propose"), {"propose": "propose", END: END})
    return graph.compile(checkpointer=checkpointer)
