"""The MCP SDK works here: a client calls a tool on the probe server in memory and over stdio.

No MORPH logic and no model. The stdio test starts the probe as a real subprocess with the venv's
own interpreter, so it covers the transport that the real server will use.
"""

import asyncio
import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import TextContent

from app.mcp_server import probe

BACKEND = Path(__file__).resolve().parents[2]
TIMEOUT_S = 60.0


def text_of(result: object) -> str:
    content = getattr(result, "content")  # noqa: B009 - the SDK result type is checked below
    assert len(content) == 1 and isinstance(content[0], TextContent)
    return str(content[0].text)


async def in_memory() -> tuple[list[str], str, bool]:
    async with Client(probe.server) as client:
        tools = await client.list_tools()
        result = await client.call_tool("ping", {"text": "memory"})
        return [t.name for t in tools.tools], text_of(result), result.is_error


async def over_stdio() -> tuple[str, str | None]:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp_server.probe"], cwd=BACKEND
    )
    async with Client(params) as client:
        result = await client.call_tool("ping", {"text": "stdio"})
        info = client.server_info
        return text_of(result), info.name if info else None


def test_a_client_calls_the_probe_tool_in_memory() -> None:
    names, text, is_error = asyncio.run(asyncio.wait_for(in_memory(), TIMEOUT_S))
    assert names == ["ping"] and text == "pong:memory" and not is_error


def test_a_client_calls_the_probe_tool_over_stdio_in_a_subprocess() -> None:
    text, server_name = asyncio.run(asyncio.wait_for(over_stdio(), TIMEOUT_S))
    assert text == "pong:stdio" and server_name == "morph-probe"


def test_an_unknown_tool_is_an_error_not_a_crash() -> None:
    async def call() -> bool:
        async with Client(probe.server) as client:
            result = await client.call_tool("no_such_tool", {})
            return bool(result.is_error)

    assert asyncio.run(asyncio.wait_for(call(), TIMEOUT_S)) is True
