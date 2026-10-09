"""The redaction corpus: sentinel secrets through every channel, then secret-shaped strings.

Four secret values are planted in the gateway's configuration (model key, approver token, database
URL and its password). A case passes if none of them is found in the channel it is about: a tool
result, an audit row, a captured log, a REST response."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.policy import get_audit_log, get_clock
from app.db import get_session
from app.db_models import AuditEvent
from app.main import create_app
from app.mcp_server import adapters
from app.policy.redact import Redactor
from app.policy.toolspec import TOOL_SPECS
from app.settings import Settings, get_settings
from tests.mcp_server.rig import SECRETS, Rig, make_rig
from tests.policy.evalkit.redaction_cases import NEAR_MISSES, PATTERNS

ALL_SECRETS = tuple(SECRETS.values())
APPROVER = SECRETS["approver"]


@dataclass
class RedactionResult:
    id: str
    channel: str
    passed: bool
    detail: str = ""


def leaked(text: str) -> list[str]:
    return [name for name, value in SECRETS.items() if value in text]


def audit_dump(engine: Engine, rig: Rig) -> str:
    with Session(engine) as db:
        payloads = db.scalars(select(AuditEvent.payload).where(AuditEvent.chain == rig.audit.chain))
        return " ".join(json.dumps(p) for p in payloads)


def valid_calls(rig: Rig) -> dict[str, dict[str, Any]]:
    """For each tool, arguments the policy lets through, so the handler (the faulty part) is
    reached."""
    world = rig.world(label="red")
    generated = rig.call("generate_integration", mapping_run_id=world.mapping_run_id)
    result = generated.body["result"]
    repair = {"mapping_run_id": world.mapping_run_id, "condition": "L2R"}
    return {
        "list_systems": {},
        "get_system": {"system_id": world.source_system_id},
        "get_mapping_run": {"mapping_run_id": world.mapping_run_id},
        "get_integration": {"integration_id": result["integration_id"]},
        "get_integration_files": {
            "integration_id": result["integration_id"],
            "version": result["version"],
        },
        "get_repair_run": {"run_id": 1},
        "list_audit_events": {"limit": 5},
        "describe_policy": {},
        "ingest_contract": {"file": "crm.v1.json", "name": f"crm-red-{rig.tag}"},
        "propose_mapping": {
            "source_system_version": world.source_version_id,
            "target_system_version": world.target_version_id,
            "source_entity": "Customer",
            "target_entity": "User",
        },  # fmt: skip
        "generate_integration": {"mapping_run_id": world.mapping_run_id},
        "run_generated_tests": {
            "integration_id": result["integration_id"],
            "version": result["version"],
        },
        "repair_integration": repair,
    }


def tool_error_cases(engine: Engine, tmp: Path) -> list[RedactionResult]:
    rig = make_rig(engine, tmp / "tools")
    calls = valid_calls(rig)
    results: list[RedactionResult] = []
    originals = dict(adapters.HANDLERS)
    message = "failed: " + " | ".join(ALL_SECRETS)

    def explode(rt: Any, args: Any) -> dict[str, Any]:
        raise RuntimeError(message)

    try:
        for spec in TOOL_SPECS:
            adapters.HANDLERS[spec.name] = explode
            response = rig.call(spec.name, **calls[spec.name])
            text = json.dumps(response.body)
            found = leaked(text + audit_dump(engine, rig))
            reached = response.body.get("error_code") == "INTERNAL"
            results.append(RedactionResult(
                f"R-TOOL-{spec.name}", "tool result (error path)", reached and not found,
                "" if reached and not found else f"reached={reached} leaked={found}",
            ))  # fmt: skip
    finally:
        adapters.HANDLERS.update(originals)
    return results


def audit_cases(engine: Engine, tmp: Path) -> list[RedactionResult]:
    results = []
    for name, value in SECRETS.items():
        rig = make_rig(engine, tmp / f"audit-{name}")
        response = rig.call("get_system", system_id=value)  # refused by the schema, then recorded
        stored = audit_dump(engine, rig)
        found = [n for n, v in SECRETS.items() if v in stored + json.dumps(response.body)]
        detail = f"leaked={found}" if found else ""
        results.append(
            RedactionResult(f"R-AUDIT-{name}", "audit row of a call carrying it", not found, detail)
        )
    return results


def log_case(engine: Engine, tmp: Path) -> list[RedactionResult]:
    rig = make_rig(engine, tmp / "log")
    handler_records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            handler_records.append(record.getMessage())

    capture = Capture(level=logging.DEBUG)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        rig.call("get_system", system_id=" ".join(ALL_SECRETS))
        rig.call("ingest_contract", file="crm.v1.json", name="log-case", approval_id=APPROVER)
    finally:
        root.removeHandler(capture)
        root.setLevel(old_level)
    found = leaked(" ".join(handler_records))
    return [RedactionResult("R-LOG-1", "captured log output of calls carrying them", not found,
                            f"leaked={found}" if found else "")]  # fmt: skip


def rest_cases(engine: Engine, tmp: Path) -> list[RedactionResult]:
    rig = make_rig(engine, tmp / "rest")
    world = rig.world(data_class=None, environment=None, label="rest")
    secret_text = " ".join(ALL_SECRETS)
    asked = rig.call(
        "propose_mapping", source_system_version=world.source_version_id,
        target_system_version=world.target_version_id, source_entity=secret_text[:190],
        target_entity="User",
    )  # fmt: skip
    app = create_app()
    app.dependency_overrides[get_audit_log] = lambda: rig.audit
    app.dependency_overrides[get_clock] = lambda: rig.clock

    def one_session() -> Any:
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = one_session
    data = {"MORPH_APPROVER_TOKEN": APPROVER}
    app.dependency_overrides[get_settings] = lambda: Settings.model_validate(data)
    client = TestClient(app)
    routes = {
        "R-REST-policy": "/policy/active",
        "R-REST-audit": "/audit/events?limit=200",
        "R-REST-approvals": "/approvals",
        "R-REST-attributes": f"/systems/{world.source_system_id}/policy-attributes",
    }
    results = []
    for rid, url in routes.items():
        body = client.get(url).text
        found = leaked(body)
        results.append(RedactionResult(rid, "REST response", not found and bool(body),
                                       f"leaked={found}" if found else ""))  # fmt: skip
    assert asked.status in ("needs_approval", "denied", "error")
    return results


def pattern_cases() -> list[RedactionResult]:
    redactor = Redactor([])
    results = []
    for n, value in enumerate(PATTERNS, 1):
        done = redactor.text(f"x {value} y")
        core = value.splitlines()[1] if value.startswith("-----BEGIN") else value[8:]
        results.append(RedactionResult(f"R-PATTERN-{n:02d}", "secret-shaped string removed",
                                       done.patterns >= 1 and core not in done.text))  # fmt: skip
    return results


def near_miss_cases() -> list[RedactionResult]:
    redactor = Redactor([])
    return [
        RedactionResult(f"R-NEAR-{n}", "ordinary text unchanged",
                        redactor.text(text).text == text and not redactor.text(text).changed)
        for n, text in enumerate(NEAR_MISSES, 1)
    ]  # fmt: skip


def run_all(engine: Engine, tmp: Path) -> dict[str, list[RedactionResult]]:
    sentinel = [
        *tool_error_cases(engine, tmp), *audit_cases(engine, tmp), *log_case(engine, tmp),
        *rest_cases(engine, tmp),
    ]  # fmt: skip
    return {"sentinel": sentinel, "pattern": pattern_cases(), "near_miss": near_miss_cases()}
