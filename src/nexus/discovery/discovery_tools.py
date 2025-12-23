"""Discovery tools for dynamic tool loading.

These 4 MCP tools enable agents to discover and load relevant tools on-demand,
rather than loading all tools into context upfront. This dramatically reduces
token usage while maintaining accuracy.

Tools:
- search_tools: Search for tools by query (BM25 ranking)
- list_servers: List all available MCP servers
- get_tool_details: Get detailed information about a specific tool
- load_tools: Load tools into the active context
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.discovery.tool_index import ToolIndex


# Discovery tool definitions (for MCP registration)
DISCOVERY_TOOLS = {
    "nexus_discovery:search_tools": {
        "name": "nexus_discovery:search_tools",
        "description": (
            "Search for MCP tools by query. Returns relevant tools ranked by BM25 score. "
            "Use this to find tools that can help accomplish a task."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query describing the desired tool functionality",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    "nexus_discovery:list_servers": {
        "name": "nexus_discovery:list_servers",
        "description": (
            "List all available MCP servers. Use this to see what tool providers are available."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "nexus_discovery:get_tool_details": {
        "name": "nexus_discovery:get_tool_details",
        "description": (
            "Get detailed information about a specific tool, including its full input schema. "
            "Use this after search_tools to get complete parameter information."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Full tool name (e.g., 'server:tool_name')",
                },
            },
            "required": ["tool_name"],
        },
    },
    "nexus_discovery:load_tools": {
        "name": "nexus_discovery:load_tools",
        "description": (
            "Load specified tools into the active context. After loading, these tools "
            "become available for direct use. Use this after finding relevant tools with search_tools."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of tool names to load",
                },
            },
            "required": ["tool_names"],
        },
    },
}


def search_tools(
    index: ToolIndex,
    query: str,
    top_k: int = 5,
) -> dict[str, Any]:
    """Search for tools by query.

    Args:
        index: Tool index to search
        query: Search query
        top_k: Maximum number of results

    Returns:
        Dict with 'tools' list and 'count'
    """
    matches = index.search(query, top_k=top_k)
    return {
        "tools": [m.to_dict() for m in matches],
        "count": len(matches),
        "query": query,
    }


def list_servers(index: ToolIndex) -> dict[str, Any]:
    """List all available servers.

    Args:
        index: Tool index

    Returns:
        Dict with 'servers' list and counts
    """
    servers = index.list_servers()
    server_tool_counts = {server: len(index.list_tools(server=server)) for server in servers}
    return {
        "servers": servers,
        "server_tool_counts": server_tool_counts,
        "total_servers": len(servers),
        "total_tools": index.tool_count,
    }


def get_tool_details(
    index: ToolIndex,
    tool_name: str,
) -> dict[str, Any]:
    """Get detailed information about a tool.

    Args:
        index: Tool index
        tool_name: Full tool name

    Returns:
        Dict with tool details or error
    """
    tool = index.get_tool(tool_name)
    if tool is None:
        return {
            "error": f"Tool '{tool_name}' not found",
            "found": False,
        }
    return {
        "found": True,
        **tool.to_dict(),
    }


def load_tools(
    index: ToolIndex,
    tool_names: list[str],
    active_tools: dict[str, Any],
) -> dict[str, Any]:
    """Load tools into the active context.

    Args:
        index: Tool index
        tool_names: List of tool names to load
        active_tools: Dict to add loaded tools to (modified in place)

    Returns:
        Dict with loaded tools and status
    """
    loaded = []
    not_found = []
    already_loaded = []

    for name in tool_names:
        if name in active_tools:
            already_loaded.append(name)
            continue

        tool = index.get_tool(name)
        if tool is None:
            not_found.append(name)
            continue

        # Add to active tools
        active_tools[name] = tool.to_dict()
        loaded.append(name)

    return {
        "loaded": loaded,
        "already_loaded": already_loaded,
        "not_found": not_found,
        "active_tool_count": len(active_tools),
    }
