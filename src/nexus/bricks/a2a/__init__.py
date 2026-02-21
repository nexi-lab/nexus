"""Google A2A (Agent-to-Agent) protocol brick for Nexus.

This module implements the A2A protocol specification, enabling Nexus to
participate in the agent interoperability ecosystem as one of three protocol
surfaces (alongside VFS and MCP).

The FastAPI router lives in ``nexus.server.api.v2.routers.a2a`` (server
layer), while business logic (handlers, streaming, task management) lives
here in the brick.

See: https://a2a-protocol.org/latest/specification/
"""
