"""Discovery module for Nexus.

Provides tool discovery capabilities for dynamic tool loading:
- Search for tools by query (BM25 ranking)
- List available MCP servers
- Get detailed tool information
- Load tools into active context

This module implements the Dynamic Discovery approach described in
benchmarks/METHODOLOGY.md, enabling agents to discover and load
relevant tools on-demand rather than loading all tools upfront.
"""

from nexus.discovery.discovery_tools import (
    DISCOVERY_TOOLS,
    get_tool_details,
    list_servers,
    load_tools,
    search_tools,
)
from nexus.discovery.tool_index import ToolIndex, ToolInfo, ToolMatch

__all__ = [
    # Tool Index
    "ToolIndex",
    "ToolInfo",
    "ToolMatch",
    # Discovery Tools
    "DISCOVERY_TOOLS",
    "search_tools",
    "list_servers",
    "get_tool_details",
    "load_tools",
]
