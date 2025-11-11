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
            "tenant_id": "tenant-123",
            "opened_file_path": "/workspace/admin/script.py"  // Optional: currently opened file
        }
    }

    Note: The frontend automatically includes x_auth and opened_file_path in metadata when user is logged in.
"""

import os

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
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
    max_tokens=10000,
)

# System prompt for Nexus filesystem awareness
SYSTEM_PROMPT = """You are a general-purpose assistant with access to the Nexus filesystem.

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
- Access Nexus files directly: `/mnt/nexus/workspace/<user>/data.csv`
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
"""


def build_prompt(state: dict, config: RunnableConfig) -> list:
    """Build prompt with optional opened file context from metadata.

    This function is called before each LLM invocation and can access
    the config which includes metadata from the frontend.
    """
    # Extract opened_file_path from metadata
    metadata = config.get("metadata", {})
    opened_file_path = metadata.get("opened_file_path")

    # Build system prompt with optional context
    system_content = SYSTEM_PROMPT
    if opened_file_path:
        system_content += f"""

## Current Context

The user currently has the following file open in their editor:
**{opened_file_path}**

When the user asks questions or requests changes without specifying a file, they are likely referring to this currently opened file. Use this context to provide more relevant and targeted assistance."""

    # Return system message + user messages
    return [SystemMessage(content=system_content)] + state["messages"]


# Create a runnable that wraps the prompt builder
prompt_runnable = RunnableLambda(build_prompt)

# Create prebuilt ReAct agent with dynamic prompt
agent = create_react_agent(
    model=llm,
    tools=tools,
    prompt=prompt_runnable,
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

    # Test with opened file context
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "What does this file do?"}]},
        config={
            "metadata": {
                "x_auth": f"Bearer {api_key}",
                "opened_file_path": "/workspace/admin/test.py",  # Optional: simulates opened file
            }
        },
    )
    print(result)
