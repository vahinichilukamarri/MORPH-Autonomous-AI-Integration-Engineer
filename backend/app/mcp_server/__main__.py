"""Run the MORPH MCP server on stdio: python -m app.mcp_server."""

from app.mcp_server.server import main

raise SystemExit(main())
