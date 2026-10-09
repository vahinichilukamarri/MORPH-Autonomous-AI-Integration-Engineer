"""The injection corpus: 8 payload classes x 5 carriers, a metamorphic check and a scripted
adversary.

For each (payload, carrier) the case builds a real world through the gateway, puts the payload into
the carrier (or a benign string, for the baseline), runs a fixed script of six legitimate calls,
and then lets a *scripted adversary* act on whatever it finds in the tool results:

* **payload reached the agent**: the payload's marker appears in a tool result;
* **decision invariance**: the six legitimate calls get the same decisions (decision, reason, rule
  ids) with the payload as with the benign string;
* **forbidden actions executed**: of the adversary's attempts, how many ran a service handler;
* **no secret** planted in the payload appears in any result or audit row.

The adversary is deterministic code, not a model. Its result says nothing about real agents.
"""

import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db_models import AuditEvent
from app.mcp_server.gateway import ToolResponse
from app.policy import attributes
from app.policy.models import DataClass, Environment, Role
from tests.codegen.fixtures import ROOT as OPENAPI_DIR
from tests.codegen.fixtures import SAMPLES
from tests.mcp_server.rig import SECRETS, Rig, World, make_rig
from tests.policy.evalkit.interpreter import HandlerSpy, resolve
from tests.repair.support import GOOD, l2_reply

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "injection.yaml"
CARRIERS = ("description", "name_and_enum", "sample_value", "generated_code", "model_reply")
BENIGN = "A note about how this field is used in reports."
LEAK = SECRETS["model_key"]


@dataclass(frozen=True)
class Payload:
    id: str
    text: str
    attempts: tuple[dict[str, Any], ...]

    @property
    def marker(self) -> str:
        return f"MORPH-INJ-{self.id}"

    def carried(self) -> str:
        """The text placed in a carrier: the marker, the instruction, and a planted secret."""
        return f"{self.marker}: {' '.join(self.text.split())} (key: {LEAK})"


def load_payloads() -> list[Payload]:
    raw = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    return [Payload(p["id"], p["text"], tuple(p["attempts"])) for p in raw]


@dataclass
class Attempt:
    tool: str
    status: str
    reason: str
    executed: bool


@dataclass
class Trace:
    decisions: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)
    reached: bool = False
    attempts: list[Attempt] = field(default_factory=list)
    leaked: bool = False
    skipped_attempts: int = 0


# ---- building the carrier ------------------------------------------------------------------------


def spec_with(text: str, kind: str) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads((OPENAPI_DIR / "crm.v1.json").read_text(encoding="utf-8"))
    properties = spec["components"]["schemas"]["Customer"]["properties"]
    if kind == "description":
        properties["email"]["description"] = text
    else:  # a property whose *name* carries the marker and whose enum value carries the text
        marker, _, _ = text.partition(":")
        properties[marker] = {"type": "string", "enum": [text]}
    return spec


def samples_with(text: str, directory: Path) -> Path:
    shutil.copytree(SAMPLES, directory)
    path = directory / "crm_customer.v1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["records"][0]["first_name"] = text
    path.write_text(json.dumps(document), encoding="utf-8")
    return directory


def module_with(text: str) -> str:
    return GOOD.replace("PAGE_SIZE = 100\n", f"PAGE_SIZE = 100\nNOTE = {text!r}\n", 1)


# ---- the legitimate script, per carrier ----------------------------------------------------------


@dataclass
class Scene:
    rig: Rig
    carried: str
    results: list[ToolResponse] = field(default_factory=list)

    def call(self, tool: str, **args: Any) -> ToolResponse:
        response = self.rig.call(tool, **args)
        self.results.append(response)
        return response


def script_for(carrier: str, scene: Scene, tmp: Path, text: str) -> None:
    rig = scene.rig
    scene.call("describe_policy")
    if carrier in ("description", "name_and_enum"):
        spec = spec_with(text, carrier)
        (rig.spec_root / "crm-inj.json").write_text(json.dumps(spec), encoding="utf-8")
        ingested = scene.call("ingest_contract", file="crm-inj.json", name=f"inj-{rig.tag}")
        scene.call("list_systems")
        system_id = ingested.body["result"]["system_id"]
        scene.call("get_system", system_id=system_id)
        scene.call("list_audit_events", limit=20)
        scene.call("get_system", system_id=system_id)
        return
    world = rig.world(label="leg")
    if carrier == "sample_value":
        generated = scene.call("generate_integration", mapping_run_id=world.mapping_run_id)
        result = generated.body.get("result", {})
        scene.call("get_integration", integration_id=result["integration_id"])
        scene.call("get_integration_files", integration_id=result["integration_id"],
                   version=result["version"])  # fmt: skip
        scene.call("run_generated_tests", integration_id=result["integration_id"],
                   version=result["version"])  # fmt: skip
        scene.call("list_audit_events", limit=20)
    elif carrier == "generated_code":
        rig.model.replies = [l2_reply(module_with(text))]
        generated = scene.call("generate_integration", mapping_run_id=world.mapping_run_id,
                               condition="L2")  # fmt: skip
        result = generated.body.get("result", {})
        scene.call("get_integration", integration_id=result["integration_id"])
        scene.call("get_integration_files", integration_id=result["integration_id"],
                   version=result["version"])  # fmt: skip
        scene.call("list_audit_events", limit=20)
        scene.call("get_mapping_run", mapping_run_id=world.mapping_run_id)
    else:  # model_reply
        rig.model.replies = [text, text, text, text]
        started = scene.call("repair_integration", mapping_run_id=world.mapping_run_id,
                             condition="L2R")  # fmt: skip
        run_id = started.body.get("result", {}).get("repair_run_id")
        scene.call("get_repair_run", run_id=run_id)
        scene.call("list_audit_events", limit=20)
        scene.call("get_mapping_run", mapping_run_id=world.mapping_run_id)
        scene.call("list_systems")


# ---- the adversary -------------------------------------------------------------------------------


def prepare_worlds(rig: Rig, mock: World | None = None) -> dict[str, Any]:
    """The systems the adversary's attempts point at; the production one has an integration."""
    worlds: dict[str, Any] = {"mock": mock or rig.world(label="adv")}
    worlds["internal"] = rig.world(Environment.MOCK, DataClass.INTERNAL, label="int")
    prod = rig.world(Environment.MOCK, DataClass.SYNTHETIC, label="prod")
    generated = rig.call("generate_integration", mapping_run_id=prod.mapping_run_id)
    result = generated.body["result"]
    with Session(rig.engine) as db:
        for system_id in (prod.source_system_id, prod.target_system_id):
            attributes.set_attributes(
                db, rig.audit, rig.clock, system_id, environment=Environment.PRODUCTION,
                data_class=DataClass.SYNTHETIC, updated_by="human:approver", session_id="corpus",
            )  # fmt: skip
    worlds["prod"] = {
        "mapping_run_id": prod.mapping_run_id, "source_system_id": prod.source_system_id,
        "integration_id": result["integration_id"], "version": result["version"],
    }  # fmt: skip
    return worlds


@dataclass
class Holder:
    worlds: dict[str, Any]
    saved: dict[str, Any]


def ambient(worlds: dict[str, Any]) -> Holder:
    return Holder(
        {k: v for k, v in worlds.items() if not isinstance(v, dict)},
        {k: v for k, v in worlds.items() if isinstance(v, dict)},
    )


def attempt(
    rig: Rig, reader: Rig, spec: dict[str, Any], worlds: dict[str, Any], spy: HandlerSpy
) -> Attempt:
    holder = ambient(worlds)
    args = resolve(spec["args"], holder)
    target = reader if spec.get("session") == "reader" else rig
    if spec.get("approval") == "pending":
        first = target.call(spec["tool"], **args)
        args = {**args, "approval_id": first.body["approval_id"]}
    before = spy.calls
    response = target.call(spec["tool"], **args)
    ran = spy.calls > before
    return Attempt(spec["tool"], response.status, response.body.get("reason_code", "-"), ran)


def decisions_of(
    engine: Engine, rig: Rig, upto_seq: int | None = None
) -> list[tuple[str, str, tuple[str, ...]]]:
    with Session(engine) as db:
        rows = db.scalars(
            select(AuditEvent)
            .where(AuditEvent.chain == rig.audit.chain, AuditEvent.event_type == "POLICY_DECISION",
                   AuditEvent.session_id == rig.deps.session_id)
            .order_by(AuditEvent.seq)
        ).all()  # fmt: skip
    chosen = [r for r in rows if upto_seq is None or r.seq <= upto_seq]
    return [
        (str(r.payload["decision"]), str(r.payload["reason"]), tuple(r.payload["rule_ids"]))
        for r in chosen
    ]


def last_seq(engine: Engine, rig: Rig) -> int:
    with Session(engine) as db:
        rows = db.scalars(select(AuditEvent.seq).where(AuditEvent.chain == rig.audit.chain)).all()
    return max(rows) if rows else 0


def audit_text(engine: Engine, rig: Rig) -> str:
    with Session(engine) as db:
        rows = db.scalars(
            select(AuditEvent.payload).where(AuditEvent.chain == rig.audit.chain)
        ).all()
    return " ".join(json.dumps(r) for r in rows)


def run_trace(engine: Engine, tmp: Path, payload: Payload, carrier: str, *, benign: bool) -> Trace:
    text = BENIGN if benign else payload.carried()
    if benign:
        text = f"{payload.marker}: {BENIGN}"
    samples = samples_with(text, tmp / "samples") if carrier == "sample_value" else None
    rig = make_rig(engine, tmp / "operator", role=Role.OPERATOR, samples_dir=samples)
    scene = Scene(rig, text)
    script_for(carrier, scene, tmp, text)
    cut = last_seq(engine, rig)
    trace = Trace(decisions=decisions_of(engine, rig, cut))
    haystack = json.dumps([r.body for r in scene.results], ensure_ascii=False)
    trace.reached = payload.marker in haystack
    if benign:
        return trace
    reader = make_rig(engine, tmp / "reader", role=Role.READER)
    with HandlerSpy() as spy:
        worlds = prepare_worlds(rig, None)
        if trace.reached:  # the adversary acts only on what it found
            for spec in payload.attempts:
                trace.attempts.append(attempt(rig, reader, spec, worlds, spy))
    everything = haystack + audit_text(engine, rig) + audit_text(engine, reader)
    trace.leaked = any(secret in everything for secret in SECRETS.values())
    return trace


@dataclass
class CaseResult:
    payload: str
    carrier: str
    reached: bool
    invariant: bool
    executed: int
    attempts: int
    refused: int
    leaked: bool
    note: str = ""

    @property
    def id(self) -> str:
        return f"{self.payload}-{self.carrier}"

    @property
    def passed(self) -> bool:
        return (
            self.reached
            and self.invariant
            and self.executed == 0
            and self.attempts == self.expected_attempts
            and not self.leaked
        )

    expected_attempts: int = 0


def run_case(engine: Engine, base: Path, payload: Payload, carrier: str) -> CaseResult:
    unique = base / f"{payload.id}-{carrier}-{uuid.uuid4().hex[:6]}"
    attacked = run_trace(engine, unique / "attack", payload, carrier, benign=False)
    baseline = run_trace(engine, unique / "benign", payload, carrier, benign=True)
    refused = sum(1 for a in attacked.attempts if a.status in ("denied", "needs_approval"))
    return CaseResult(
        payload=payload.id, carrier=carrier, reached=attacked.reached and baseline.reached,
        invariant=attacked.decisions == baseline.decisions and bool(attacked.decisions),
        executed=sum(1 for a in attacked.attempts if a.executed), attempts=len(attacked.attempts),
        refused=refused, leaked=attacked.leaked, expected_attempts=len(payload.attempts),
    )  # fmt: skip
