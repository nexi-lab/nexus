"""Nexus MCP Server - Model Context Protocol integration.

This module provides an MCP server implementation that exposes Nexus
functionality to AI agents and tools through the Model Context Protocol.

Key Features:
- File operations (read, write, delete, list)
- Search capabilities (grep, glob, semantic search)
- Memory management (store, query)
- Workflow execution
- Resource browsing
- Infrastructure-level API key management

Usage:
    # Start MCP server
    nexus mcp serve --transport stdio

    # Or use programmatically
    from nexus.mcp import create_mcp_server

    nx = connect()
    server = create_mcp_server(nx)
    server.run()

    # Infrastructure API key management
    from nexus.mcp import set_request_api_key

    # In middleware/proxy code:
    token = set_request_api_key("sk-user-api-key")
    try:
        # Tool calls here will use this API key
        pass
    finally:
        token.reset()
"""

from nexus.mcp.server import (
    _request_api_key,
    create_mcp_server,
    get_request_api_key,
    set_request_api_key,
)

__all__ = ["create_mcp_server", "set_request_api_key", "get_request_api_key", "_request_api_key"]
