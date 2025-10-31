#!/usr/bin/env python3
"""Test that the tools structure is correct."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool


# Test tool definition
@tool("test_tool", "A test tool", {"message": str})
async def test_tool(args):
    return {"content": [{"type": "text", "text": f"Hello {args.get('message', 'world')}"}]}


# Test server creation
try:
    server = create_sdk_mcp_server(name="test-server", version="1.0.0", tools=[test_tool])
    print("✓ MCP server created successfully")
    print(f"  Server: {server}")
except Exception as e:
    print(f"✗ Failed to create MCP server: {e}")
    sys.exit(1)

# Test options creation
try:
    options = ClaudeAgentOptions(
        mcp_servers={"test": server}, allowed_tools=["mcp__test__test_tool"]
    )
    print("✓ ClaudeAgentOptions created successfully")
    print(f"  MCP servers: {list(options.mcp_servers.keys())}")
    print(f"  Allowed tools: {options.allowed_tools}")
except Exception as e:
    print(f"✗ Failed to create options: {e}")
    sys.exit(1)

print("\n✓ All tool structures are valid!")
print("\nThe demo should work now (with ANTHROPIC_API_KEY set)")
