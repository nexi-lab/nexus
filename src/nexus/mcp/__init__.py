"""Nexus MCP Server - Model Context Protocol integration.

This module provides an MCP server implementation that exposes Nexus
functionality to AI agents and tools through the Model Context Protocol.

Key Features:
- File operations (read, write, delete, list)
- Search capabilities (grep, glob, semantic search)
- Memory management (store, query)
- Workflow execution
- Resource browsing

Usage:
    # Start MCP server
    nexus mcp serve --transport stdio

    # Or use programmatically
    from nexus.mcp import create_mcp_server

    nx = connect()
    server = create_mcp_server(nx)
    server.run()
"""

from nexus.mcp.server import create_mcp_server

__all__ = ["create_mcp_server"]
