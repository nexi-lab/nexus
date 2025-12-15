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

# Import official system prompt from Nexus tools
from nexus.tools import get_prompt

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


def build_prompt(state: dict, config: RunnableConfig) -> list:
    """Build prompt with optional opened file context from metadata.

    This function is called before each LLM invocation and can access
    the config which includes metadata from the frontend.
    """
    # Get complete prompt with skills and opened file context using the convenience function
    # get_prompt automatically extracts opened_file_path from config.metadata
    system_content = get_prompt(config, role="general", state=state)

    # Add additional context specific to this agent (sandbox integration, binary files, etc.)
    system_content += """

## Additional Context

### Sandbox Integration

When sandbox_id is provided in metadata, python() and bash() tools execute code in isolated
sandboxes with the Nexus filesystem automatically mounted at `/mnt/nexus`.

This means you can:
- Access Nexus files directly: `/mnt/nexus/workspace/<user>/data.csv`
- Use standard tools: `ls`, `cat`, `python`, `grep`, etc.
- Read/write files that persist in Nexus
- Run complex data processing pipelines

### Binary Files

For binary files (PDF, Excel, PowerPoint, Word), use the parsed markdown path:
- Original: `document.pdf` → binary (unreadable)
- Parsed: `document_parsed.pdf.md` → markdown text (readable)
- Supported: .pdf, .xlsx, .xls, .pptx, .ppt, .docx, .doc, .odt, .ods, .odp, .rtf, .epub
- Example: `read_file("cat /workspace/report_parsed.pdf.md")`

### Common File Paths

- `/workspace/<user>/` - User's personal workspace
- `/agent/<user>/<agent_name>/` - Agent-specific data and configs
"""

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
