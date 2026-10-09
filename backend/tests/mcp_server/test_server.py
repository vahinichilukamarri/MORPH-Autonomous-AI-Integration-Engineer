"""The MCP server with the official SDK client: in memory, and as a real subprocess over stdio.

Tested with the official Python SDK client only; no third-party MCP client application was tried.
"""

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from sqlalchemy import Engine

from app.mcp_server.gateway import Gateway, ToolResponse
from app.mcp_server.server import build_server, tool_list
from app.policy.loader import POLICY_DIR
from app.policy.toolspec import TOOL_SPECS
from tests.mcp_server.rig import Rig

BACKEND = Path(__file__).resolve().parents[2]
TIMEOUT_S = 90.0


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT_S))


def test_the_listed_tools_are_the_thirteen_specifications(rig: Rig) -> None:
    async def listing() -> list[Any]:
        async with Client(build_server(rig.gateway)) as client:
            return list((await client.list_tools()).tools)

    tools = run(listing())
    assert [t.name for t in tools] == [s.name for s in TOOL_SPECS] and len(tools) == 13
    by_name = {t.name: t for t in tools}
    assert by_name["get_system"].annotations.read_only_hint is True
    assert by_name["ingest_contract"].annotations.read_only_hint is False
    assert all(t.annotations.destructive_hint is False for t in tools)
    assert "approval_id" in by_name["propose_mapping"].input_schema["properties"]
    assert [t.name for t in tool_list()] == [t.name for t in tools]


def test_a_tool_call_returns_the_gateways_answer_as_structured_json(rig: Rig) -> None:
    async def calls() -> list[Any]:
        async with Client(build_server(rig.gateway)) as client:
            return [
                await client.call_tool("describe_policy", {}),
                await client.call_tool("ingest_contract", {"file": "../x.json", "name": "n"}),
                await client.call_tool("run_shell", {"command": "id"}),
                await client.call_tool("propose_mapping", {
                    "source_system_version": 999_999_001, "target_system_version": 999_999_002,
                    "source_entity": "A", "target_entity": "B",
                }),
            ]  # fmt: skip

    described, escape, shell, proposed = run(calls())
    assert not described.is_error and described.structured_content["status"] == "ok"
    assert escape.is_error and escape.structured_content["reason_code"] == "PATH_ESCAPE"
    assert shell.is_error and shell.structured_content["reason_code"] == "UNKNOWN_TOOL"
    # system versions that do not exist involve no system, so no rule can match: default deny
    assert proposed.is_error and proposed.structured_content["reason_code"] == "DEFAULT_DENY"
    assert described.content[0].text.startswith("{")


def test_every_call_goes_through_the_gateway_and_only_the_gateway(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    original = Gateway.call

    def counting(self: Gateway, tool: str, arguments: Any) -> ToolResponse:
        seen.append(tool)
        return original(self, tool, arguments)

    monkeypatch.setattr(Gateway, "call", counting)

    async def everything() -> None:
        async with Client(build_server(rig.gateway)) as client:
            for spec in TOOL_SPECS:
                await client.call_tool(spec.name, {})

    run(everything())
    assert seen == [s.name for s in TOOL_SPECS]


def test_the_server_has_no_way_in_except_the_two_tool_methods(rig: Rig) -> None:
    server = build_server(rig.gateway)
    assert server.get_request_handler("tools/list") is not None
    assert server.get_request_handler("tools/call") is not None
    for method in (
        "resources/list", "resources/read", "resources/templates/list", "resources/subscribe",
        "prompts/list", "prompts/get", "completion/complete", "logging/setLevel",
    ):  # fmt: skip
        assert server.get_request_handler(method) is None, method


def stdio_params(
    engine: Engine, role: str = "reader", policy_dir: Path | None = None
) -> StdioServerParameters:
    env = {
        "DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "MORPH_MCP_ROLE": role,
        "EMBEDDING_PROVIDER": "fake",
        "POLICY_DIR": str(policy_dir or POLICY_DIR),
    }
    return StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp_server"], cwd=BACKEND, env=env
    )


def test_the_real_entry_point_serves_over_stdio_with_the_configured_role(
    test_engine: Engine,
) -> None:
    async def session() -> tuple[Any, Any, Any, Any]:
        async with Client(stdio_params(test_engine)) as client:
            tools = await client.list_tools()
            described = await client.call_tool("describe_policy", {})
            refused = await client.call_tool(
                "ingest_contract", {"file": "mock_systems/openapi/crm.v1.json", "name": "crm-stdio"}
            )
            unknown = await client.call_tool("read_env", {})
            return tools, described, refused, unknown

    tools, described, refused, unknown = run(session())
    assert len(tools.tools) == 13
    assert described.structured_content["result"]["your_role"] == "reader"
    assert refused.structured_content["reason_code"] == "ROLE_NOT_PERMITTED"
    assert unknown.structured_content["reason_code"] == "UNKNOWN_TOOL"


def test_the_server_refuses_to_start_when_the_policy_does_not_match_its_lock(
    test_engine: Engine, tmp_path: Path
) -> None:
    tampered = tmp_path / "policy"
    shutil.copytree(POLICY_DIR, tampered)
    path = tampered / "morph-policy-v1.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("max_model_calls: 40", "max_model_calls: 41"),
        encoding="utf-8",
    )
    params = stdio_params(test_engine, policy_dir=tampered)
    env = {**os.environ, **(params.env or {})}
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "app.mcp_server"], cwd=BACKEND, env=env, capture_output=True,
        text=True, timeout=120, check=False, input="",
    )  # fmt: skip
    assert done.returncode == 2
    assert "refusing to start" in done.stderr and "does not match policy.lock" in done.stderr
    assert done.stdout == ""
