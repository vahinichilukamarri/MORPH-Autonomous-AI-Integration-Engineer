"""M0 round-trip probe: a one-tool MCP server that proves the SDK works in this environment.

It has no MORPH logic and touches no data. The tests start it in memory and over stdio. The real
server, with the policy gateway in front of every tool, replaces it in M3.
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer("morph-probe")


@server.tool()
def ping(text: str) -> str:
    """Return the text with a prefix, so a test can see the call went through the server."""
    return f"pong:{text}"


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
