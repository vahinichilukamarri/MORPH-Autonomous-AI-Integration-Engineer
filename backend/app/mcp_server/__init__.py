"""The MCP server: the only package that imports the MCP SDK.

Nothing outside this package may import ``mcp`` or ``mcp_types``, and this package imports only the
services, never the other way round (tests enforce both). Importing the package itself loads no SDK
code; only its submodules do.
"""
