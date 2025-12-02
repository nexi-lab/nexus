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
- Unified MCP connection management (Klavis + local providers)

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

    # Unified MCP connection (Klavis or local)
    from nexus.mcp import KlavisClient, MCPProviderRegistry

    registry = MCPProviderRegistry.load_default()
    klavis = KlavisClient(api_key="...")
"""

from nexus.mcp.connection_manager import MCPConnection, MCPConnectionError, MCPConnectionManager
from nexus.mcp.klavis_client import KlavisClient, KlavisError, KlavisMCPInstance, KlavisOAuthResult
from nexus.mcp.provider_registry import (
    BackendConfig,
    MCPConfig,
    MCPProviderRegistry,
    OAuthConfig,
    ProviderConfig,
    ProviderType,
)
from nexus.mcp.server import (
    _request_api_key,
    create_mcp_server,
    get_request_api_key,
    set_request_api_key,
)

__all__ = [
    # Server
    "create_mcp_server",
    "set_request_api_key",
    "get_request_api_key",
    "_request_api_key",
    # Connection manager
    "MCPConnectionManager",
    "MCPConnection",
    "MCPConnectionError",
    # Klavis client
    "KlavisClient",
    "KlavisError",
    "KlavisOAuthResult",
    "KlavisMCPInstance",
    # Provider registry
    "MCPProviderRegistry",
    "ProviderConfig",
    "ProviderType",
    "OAuthConfig",
    "MCPConfig",
    "BackendConfig",
]
