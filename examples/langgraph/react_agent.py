#!/usr/bin/env python3
"""Simple ReAct Agent using LangGraph's Prebuilt create_react_agent.

This example demonstrates how to use LangGraph's prebuilt create_react_agent
function to quickly build a ReAct agent with Nexus filesystem integration.

Authentication:
    API keys are REQUIRED via metadata.x_auth: "Bearer <token>"
    Frontend automatically passes the authenticated user's API key in request metadata.
    Each tool extracts and uses the token to create an authenticated RemoteNexusFS instance.

Requirements:
    pip install langgraph langchain-anthropic

Usage from Frontend (HTTP):
    POST http://localhost:2024/runs/stream
    {
        "assistant_id": "agent",
        "input": {
            "messages": [{"role": "user", "content": "Find all Python files"}]
        },
        "metadata": {
            "x_auth": "Bearer sk-your-api-key-here",
            "user_id": "user-123",
            "tenant_id": "tenant-123"
        }
    }

    Note: The frontend automatically includes x_auth in metadata when user is logged in.
"""

import os

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent
from nexus_tools import get_nexus_tools

# Get configuration from environment variables
E2B_TEMPLATE_ID = os.getenv("E2B_TEMPLATE_ID")

print("API key will be provided per-request via config.configurable.nexus_api_key")

# Check E2B configuration
if E2B_TEMPLATE_ID:
    print(f"E2B sandbox enabled with template: {E2B_TEMPLATE_ID}")
else:
    print("E2B sandbox disabled (E2B_TEMPLATE_ID not set)")

# Create tools (no API key needed - will be passed per-request)
tools = get_nexus_tools()

# Create LLM
llm = ChatAnthropic(
    model="claude-sonnet-4-5-20250929",
)

# System prompt for Nexus filesystem awareness
SYSTEM_PROMPT = """You are an AI assistant with access to the Nexus distributed filesystem.

## Nexus Filesystem Overview

Nexus is an AI-native distributed filesystem that provides:
- **Unified namespace**: All files accessible via `/workspace/`, `/agent/`, or mount points
- **Rich metadata**: Every file has searchable metadata (tags, descriptions, permissions)
- **Version control**: Automatic versioning of all file changes
- **Semantic search**: Find files by content meaning, not just keywords
- **Permission system**: ReBAC-based access control (owner, viewer, editor roles)
- **Code execution**: Run Python/bash code in E2B sandboxes with mounted filesystem access

## Available Tools

You have access to the following Nexus tools:

### File Operations
- `list_files(path)`: List directory contents
- `read_file(path)`: Read file contents
- `write_file(path, content)`: Write/update files
- `delete_file(path)`: Delete files
- `grep_files(pattern, path)`: Search file contents with grep syntax

### File Discovery
- `find_files(pattern, path)`: Find files by name pattern (glob)
- `search_files_semantic(query)`: Find files by semantic meaning
- `search_files_by_tag(tag)`: Find files with specific tags

### Metadata
- `get_metadata(path)`: Get file metadata (tags, description, permissions)
- `set_metadata(path, tags, description)`: Update file metadata

### Code Execution (if sandbox_id provided)
- `python(code)`: Execute Python code in sandbox with Nexus mounted at /home/user/nexus
- `bash(command)`: Execute bash commands in sandbox with Nexus mounted at /home/user/nexus

Note: When sandbox_id is provided in metadata, the Nexus filesystem is automatically mounted
at `/home/user/nexus` inside the sandbox, allowing direct file access via standard tools.

## Best Practices

1. **Use semantic search** when users ask conceptual questions ("find ML code", "show auth logic")
2. **Tag files** with meaningful tags to improve future discoverability
3. **Check metadata** before operations to understand file context
4. **Use grep** for precise text/code pattern matching
5. **Leverage sandboxes** for data analysis, testing, and code execution
6. **Respect permissions** - you inherit the user's permissions

## File Paths

Common Nexus paths:
- `/workspace/<user>/` - User's personal workspace
- `/agent/<user>/<agent_name>/` - Agent-specific data and configs
- `/.raw/` - Raw file storage (no metadata)

When a sandbox is active, the Nexus filesystem is mounted at `/home/user/nexus` inside the sandbox,
so you can access Nexus files directly: `/home/user/nexus/workspace/admin/file.txt`

Be helpful, efficient, and leverage Nexus's rich features to assist the user!"""

# Create prebuilt ReAct agent with system prompt
agent = create_react_agent(
    model=llm,
    tools=tools,
    state_modifier=SYSTEM_PROMPT,
)


if __name__ == "__main__":
    # Example usage - Note: requires NEXUS_API_KEY to be set for testing
    import sys

    api_key = os.getenv("NEXUS_API_KEY")
    if not api_key:
        print("Error: NEXUS_API_KEY environment variable is required for testing")
        print("Usage: NEXUS_API_KEY=your-key python react_agent.py")
        sys.exit(1)

    print("Testing ReAct agent...")
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "Find all Python files and count them"}]},
        config={"metadata": {"x_auth": f"Bearer {api_key}"}},
    )
    print(result)
