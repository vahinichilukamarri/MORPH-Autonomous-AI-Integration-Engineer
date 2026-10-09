"""A small interpreter for gateway corpus cases written as data.

A case is a list of steps (create a world, call a tool, decide an approval, move the clock, ...)
and an expectation about the **last call**. Keeping cases as data makes the corpora auditable and
their counts checkable; the interpreter is the only code that turns them into gateway calls.

Placeholders: a whole string starting with ``$`` names something earlier, for example
``$w.mapping_run_id`` (a world's field) or ``$first.approval_id`` (a field of a saved response).
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.mcp_server import adapters
from app.mcp_server.gateway import ToolResponse
from app.policy import approvals, attributes
from app.policy.loader import load_active
from app.policy.models import DataClass, Environment, Role
from tests.mcp_server.rig import Rig, World, make_rig, with_limits
from tests.repair.support import GOOD, l2_reply

REPLY_TOKENS = {"GOOD_L2": l2_reply(GOOD)}
SELF_DESCRIBING = ("approval_id", "how_to_retry", "retry_with", "expires_at", "reason_code",
                   "policy_version")  # fmt: skip


@dataclass(frozen=True)
class Case:
    corpus: str
    id: str
    group: str
    description: str
    steps: tuple[dict[str, Any], ...]
    expect: dict[str, Any]


@dataclass
class Outcome:
    case_id: str
    passed: bool
    observed: str
    problems: list[str] = field(default_factory=list)
    status: str = ""
    handler_ran: bool = False
    skipped: bool = False


class HandlerSpy:
    """Counts handler calls by wrapping every entry of the handler table."""

    def __init__(self) -> None:
        self.calls = 0
        self._originals: dict[str, adapters.Handler] = {}

    def __enter__(self) -> "HandlerSpy":
        for name, handler in adapters.HANDLERS.items():
            self._originals[name] = handler
            adapters.HANDLERS[name] = self._wrap(handler)
        return self

    def __exit__(self, *exc: object) -> None:
        adapters.HANDLERS.update(self._originals)

    def _wrap(self, handler: adapters.Handler) -> adapters.Handler:
        def wrapped(rt: adapters.Runtime, args: Any) -> adapters.Body:
            self.calls += 1
            return handler(rt, args)

        return wrapped


@dataclass
class State:
    engine: Engine
    tmp: Path
    rig: Rig
    spy: HandlerSpy
    worlds: dict[str, World] = field(default_factory=dict)
    saved: dict[str, dict[str, Any]] = field(default_factory=dict)
    last: ToolResponse | None = None
    handler_calls_in_last: int = 0


class Scope(Protocol):
    worlds: dict[str, Any]
    saved: dict[str, Any]


def resolve(value: Any, state: Scope) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        head, _, path = value[1:].partition(".")
        base: Any = state.worlds[head] if head in state.worlds else state.saved[head]
        for part in path.split(".") if path else []:
            base = base[part] if isinstance(base, dict) else getattr(base, part)
        return base
    if isinstance(value, dict):
        return {k: resolve(v, state) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, state) for v in value]
    return value


def make_link(link: Path, target: Path, directory: bool) -> bool:
    try:
        os.symlink(target, link, target_is_directory=directory)
        return True
    except OSError:
        if sys.platform != "win32" or not directory:
            return False
    done = subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
        capture_output=True, check=False,
    )  # fmt: skip
    return done.returncode == 0


class Skipped(Exception):
    """The host cannot set the case up (a link that cannot be created); reported, never a pass."""


def build_rig(engine: Engine, tmp: Path, spec: dict[str, Any]) -> Rig:
    limits = spec.get("limits")
    policy = with_limits(load_active(), **limits) if limits else None
    replies = [REPLY_TOKENS.get(r, r) for r in spec.get("replies", [])]
    return make_rig(
        engine, tmp, role=Role(spec.get("role", "operator")), policy=policy, replies=replies
    )


def step_world(state: State, spec: dict[str, Any]) -> None:
    env = spec.get("environment")
    data = spec.get("data_class")
    state.worlds[spec["name"]] = state.rig.world(
        Environment(env) if env else None, DataClass(data) if data else None,
        label=spec["name"],
    )  # fmt: skip


def step_call(state: State, spec: dict[str, Any]) -> None:
    args = resolve(spec.get("args", {}), state)
    before = state.spy.calls
    response = state.rig.call(spec["tool"], **args)
    state.handler_calls_in_last = state.spy.calls - before
    state.last = response
    if spec.get("save"):
        state.saved[spec["save"]] = response.body


def step_decide(state: State, spec: dict[str, Any]) -> None:
    approval_id = resolve(spec["approval"], state)
    with Session(state.engine) as db:
        approvals.decide(
            db, state.rig.audit, state.rig.clock, approval_id, approve=bool(spec["approve"])
        )


def step_link(state: State, spec: dict[str, Any]) -> None:
    """Make a link under the spec root that points outside it.

    A host that cannot create a symlink to a file (Windows without the privilege) gets a junction
    to the outside directory under the same name: it exercises the same resolution check and is
    reported the same way. A host that can create neither skips the case, and a skipped case is
    never a pass."""
    outside = state.tmp / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.json").write_text("{}", encoding="utf-8")
    directory = spec["kind"] == "dir"
    link = state.rig.spec_root / spec["name"]
    if directory:
        made = make_link(link, outside, True)
    else:
        made = make_link(link, outside / "secret.json", False) or make_link(link, outside, True)
    if not made:
        raise Skipped(f"this host cannot create a {spec['kind']} link")


def step_attributes(state: State, spec: dict[str, Any]) -> None:
    """Play the human approver: record a world's systems as a different environment or class."""
    world = state.worlds[spec["world"]]
    with Session(state.engine) as db:
        for system_id in (world.source_system_id, world.target_system_id):
            attributes.set_attributes(
                db, state.rig.audit, state.rig.clock, system_id,
                environment=Environment(spec["environment"]),
                data_class=DataClass(spec["data_class"]), updated_by="human:approver",
                session_id="corpus",
            )  # fmt: skip


def step_role(state: State, spec: dict[str, Any]) -> None:
    state.rig.deps.role = Role(spec["role"])


def step_replies(state: State, spec: dict[str, Any]) -> None:
    state.rig.model.replies = [REPLY_TOKENS.get(r, r) for r in spec["replies"]]


def step_switch_policy(state: State, spec: dict[str, Any]) -> None:
    from dataclasses import replace

    from app.mcp_server.gateway import Gateway

    policy = with_limits(load_active(), **spec["limits"])
    deps = replace(state.rig.deps, policy=policy)
    state.rig.deps = deps
    state.rig.gateway = Gateway(deps=deps, engine=state.engine, clock=state.rig.clock)


STEPS = {
    "attributes": step_attributes,
    "role": step_role,
    "replies": step_replies,
    "world": step_world,
    "call": step_call,
    "decide": step_decide,
    "link": step_link,
    "switch_policy": step_switch_policy,
}


def check(case: Case, state: State) -> Outcome:
    response = state.last
    if response is None:
        return Outcome(case.id, False, "no call was made", ["the case made no call"])
    body = response.body
    reason = body.get("reason_code", "-")
    observed = (
        f"{response.status}/{reason}/handler={'ran' if state.handler_calls_in_last else 'no'}"
    )
    problems: list[str] = []
    expect = case.expect
    if response.status != expect["status"]:
        problems.append(f"status {response.status!r}, expected {expect['status']!r}")
    if "reason" in expect and reason != expect["reason"]:
        problems.append(f"reason {reason!r}, expected {expect['reason']!r}")
    handler = expect.get("handler", "any")
    if handler == "not_run" and state.handler_calls_in_last:
        problems.append("the service handler ran")
    if handler == "ran" and not state.handler_calls_in_last:
        problems.append("the service handler did not run")
    for key, value in expect.get("body", {}).items():
        if body.get(key) != value:
            problems.append(f"body[{key!r}] is {body.get(key)!r}, expected {value!r}")
    if expect.get("self_describing"):
        missing = [k for k in SELF_DESCRIBING if k not in body]
        if missing:
            problems.append(f"the response lacks {missing}")
        elif body["approval_id"] not in body["how_to_retry"]:
            problems.append("how_to_retry does not name the approval id")
    return Outcome(
        case.id, not problems, observed, problems, response.status,
        handler_ran=bool(state.handler_calls_in_last),
    )  # fmt: skip


@contextmanager
def case_environment(engine: Engine, tmp: Path, case: Case) -> Iterator[State]:
    first = case.steps[0] if case.steps else {}
    spec = first.get("rig", {}) if "rig" in first else {}
    rig = build_rig(engine, tmp, spec)
    with HandlerSpy() as spy:
        yield State(engine=engine, tmp=tmp, rig=rig, spy=spy)


def run_case(engine: Engine, tmp: Path, case: Case) -> Outcome:
    try:
        with case_environment(engine, tmp, case) as state:
            for step in case.steps:
                ((kind, spec),) = step.items()
                if kind == "rig":
                    continue
                if kind == "clock":
                    state.rig.clock.advance(float(spec["seconds"]))
                    continue
                STEPS[kind](state, spec)
            return check(case, state)
    except Skipped as why:
        return Outcome(case.id, False, f"skipped: {why}", [str(why)], skipped=True)
