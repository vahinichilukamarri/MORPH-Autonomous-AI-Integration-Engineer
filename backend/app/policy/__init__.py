"""The policy layer: plain code, no model, no MCP SDK.

A declarative, versioned and hashed policy file is evaluated against a request context that the
server derives from its own state (never from caller-supplied text). A floor written in code sits
under the policy and cannot be weakened by it; the default is deny.
"""
