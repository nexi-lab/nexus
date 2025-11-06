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
- **Permission system**: ReBAC-based access control (owner, viewer, editor roles)
- **Code execution**: Run Python/bash code in E2B sandboxes with mounted filesystem access

## Available Tools

You have access to 6 Nexus tools:

### File Search & Discovery
- `grep_files(grep_cmd)`: Search file content using grep-style commands
- `glob_files(pattern, path)`: Find files by name pattern using glob syntax

### File Reading & Writing
- `read_file(read_cmd)`: Read file content using cat/less-style commands
  - For binary files (PDF, Excel, PowerPoint, Word), use the parsed markdown path:
    - Original: `document.pdf` → binary (unreadable)
    - Parsed: `document_parsed.pdf.md` → markdown text (readable)
  - Supported: .pdf, .xlsx, .xls, .pptx, .ppt, .docx, .doc, .odt, .ods, .odp, .rtf, .epub
  - Example: `read_file("cat /workspace/report_parsed.pdf.md")`
- `write_file(path, content)`: Write content to Nexus filesystem

### Code Execution (requires sandbox_id in metadata)
- `python(code)`: Execute Python code in sandbox

- `bash(command)`: Execute bash commands in sandbox

## Sandbox Integration

When sandbox_id is provided in metadata, python() and bash() tools execute code in isolated
sandboxes with the Nexus filesystem automatically mounted at `/mnt/nexus`.

This means you can:
- Access Nexus files directly: `/mnt/nexus/workspace/admin/data.csv`
- Use standard tools: `ls`, `cat`, `python`, `grep`, etc.
- Read/write files that persist in Nexus
- Run complex data processing pipelines

## Best Practices

1. **Use grep_files** for finding specific text, code patterns, or keywords in file contents
2. **Use glob_files** for finding files by name patterns (*.py, *.md, etc.)
3. **Use read_file with 'less'** for previewing large files before reading fully
4. **For binary files** (PDF, Excel, Word, PowerPoint), always use the `_parsed.{ext}.md` path to read as markdown, e.g. AR_Subledger_05.2025.xlsx -> AR_Subledger_05.2025_parsed.xlsx.md
5. **Use sandboxes** (python, bash, etc) for data analysis, testing, and complex file processing
6. **Respect permissions** - you inherit the authenticated user's permissions
7. **Write results** to /workspace/<user>/ or appropriate locations
8. **Use memories** to retrive preferences, secrets and other information that should be remembered

## File Paths

Common Nexus paths:
- `/workspace/<user>/` - User's personal workspace
- `/agent/<user>/<agent_name>/` - Agent-specific data and configs

Be helpful, efficient, and leverage these tools to assist the user!"""

# Create prebuilt ReAct agent with system prompt
agent = create_react_agent(
    model=llm,
    tools=tools,
    prompt=SYSTEM_PROMPT,
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
