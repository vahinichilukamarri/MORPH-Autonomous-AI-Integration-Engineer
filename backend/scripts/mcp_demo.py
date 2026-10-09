"""A walk-through of the policy layer over MCP. Run from ``backend/`` (Docker must be running):

    uv run python -m scripts.mcp_demo

It starts the real server twice over stdio (a reader and an operator) with the official SDK client,
and plays the human approver by calling the same functions the approver-token REST routes call.
Nothing here calls a model: the steps use the deterministic generator. It writes to the configured
database (systems named ``demo-...``) and to the audit log, so point ``DATABASE_URL`` at a
development database."""

import asyncio
import sys
import uuid
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from sqlalchemy.orm import Session

from app.db import get_engine
from app.policy import attributes
from app.policy.audit import AuditLog, verify_chain
from app.policy.clock import SystemClock
from app.policy.models import DataClass, Environment
from app.settings import get_settings

SETTINGS = get_settings()
CRM = "mock_systems/openapi/crm.v1.json"


def params(role: str) -> StdioServerParameters:
    env = {
        "MORPH_MCP_ROLE": role,
        "DATABASE_URL": SETTINGS.database_url,
        "EMBEDDING_PROVIDER": "fake",
    }
    return StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"], env=env)


def show(step: str, result: Any) -> dict[str, Any]:
    body: dict[str, Any] = result.structured_content
    why = body.get("reason_code") or body.get("error_code") or ""
    print(f"  {step:<58} -> {body['status']:<15} {why}")
    return body


async def main() -> int:
    tag = uuid.uuid4().hex[:6]
    print("1. A reader can look, but not change anything")
    async with Client(params("reader")) as reader:
        show("describe_policy", await reader.call_tool("describe_policy", {}))
        show("ingest_contract (reader)", await reader.call_tool(
            "ingest_contract", {"file": CRM, "name": f"demo-crm-{tag}"}
        ))  # fmt: skip
        show("run_shell (a tool that does not exist)", await reader.call_tool("run_shell", {}))

    print("2. An operator ingests a contract; the new system is unclassified")
    async with Client(params("operator")) as operator:
        ingested = show("ingest_contract", await operator.call_tool(
            "ingest_contract", {"file": CRM, "name": f"demo-crm-{tag}"}
        ))  # fmt: skip
        show("ingest_contract with a path that leaves the spec root", await operator.call_tool(
            "ingest_contract", {"file": "../.env", "name": f"demo-env-{tag}"}
        ))  # fmt: skip
        show("ingest_contract with a URL", await operator.call_tool(
            "ingest_contract", {"file": "http://169.254.169.254/latest/meta-data", "name": "x"}
        ))  # fmt: skip
        system_id = ingested["result"]["system_id"]
        print("  (a human records the system as a mock with synthetic data; no MCP tool can)")
        engine = get_engine()
        with Session(engine) as db:
            attributes.set_attributes(
                db, AuditLog(engine), SystemClock(), system_id, environment=Environment.MOCK,
                data_class=DataClass.SYNTHETIC, updated_by="human:approver", session_id="demo",
            )  # fmt: skip
        shown = show("get_system", await operator.call_tool("get_system", {"system_id": system_id}))
        result = shown["result"]
        print(f"  environment={result['environment']} data_class={result['data_class']}")

    print("3. The audit log records every call and can be verified")
    report = verify_chain(get_engine())
    print(f"  chain 'main': ok={report.ok}, events={report.events}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
