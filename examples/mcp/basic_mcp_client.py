"""Basic MCP Client Example for Nexus.

This example demonstrates how to programmatically interact with a Nexus MCP server
using the MCP Python SDK.

Requirements:
    pip install mcp

Usage:
    # Start Nexus MCP server in another terminal:
    nexus mcp serve --transport http --port 8081

    # Run this example:
    python basic_mcp_client.py
"""

import asyncio

# Note: This is a demonstration of how a client would work
# The actual MCP client SDK may have different APIs
# This follows the general pattern from MCP documentation


async def main() -> None:
    """Demonstrate basic MCP client operations."""

    print("=== Nexus MCP Client Example ===\n")

    # Note: The actual client implementation depends on fastmcp's client API
    # This is a conceptual example showing the operations you could perform

    print("1. File Operations")
    print("   - Create a file using nexus_write_file")
    print("   - Read it back using nexus_read_file")
    print("   - List files using nexus_list_files")
    print()

    print("2. Search Operations")
    print("   - Find Python files using nexus_glob")
    print("   - Search for TODO comments using nexus_grep")
    print("   - Semantic search using nexus_semantic_search")
    print()

    print("3. Memory Operations")
    print("   - Store a memory using nexus_store_memory")
    print("   - Query memories using nexus_query_memory")
    print()

    print("4. Workflow Operations")
    print("   - List workflows using nexus_list_workflows")
    print("   - Execute a workflow using nexus_execute_workflow")
    print()

    print("To use MCP programmatically, you would:")
    print("1. Install the MCP SDK: pip install mcp")
    print("2. Connect to the MCP server (stdio, http, or sse)")
    print("3. Call tools using session.call_tool()")
    print("4. Read resources using session.read_resource()")
    print("5. Use prompts using session.get_prompt()")
    print()

    print("For Claude Desktop integration, use the stdio transport")
    print("and configure claude_desktop_config.json as shown in README.md")


if __name__ == "__main__":
    asyncio.run(main())
