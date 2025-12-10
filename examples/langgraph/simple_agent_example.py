#!/usr/bin/env python3
"""Simple example of using official Nexus tools and system prompts.

This demonstrates the easiest way to create an agent with Nexus tools.
"""

import os

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

# Option 1: Import from nexus.tools (recommended)
from nexus.tools import CODING_AGENT_SYSTEM_PROMPT, get_nexus_tools

# Option 2: Import from nexus.tools.langgraph (more explicit)
# from nexus.tools.langgraph import CODING_AGENT_SYSTEM_PROMPT, get_nexus_tools

# Create LLM
llm = ChatAnthropic(
    model="claude-sonnet-4-5-20250929",
    max_tokens=10000,
)

# Get official Nexus tools
tools = get_nexus_tools()

# Create agent with official coding prompt
agent = create_react_agent(
    model=llm,
    tools=tools,
    prompt=CODING_AGENT_SYSTEM_PROMPT,
)

if __name__ == "__main__":
    # Example usage
    api_key = os.getenv("NEXUS_API_KEY")
    if not api_key:
        print("Error: NEXUS_API_KEY environment variable is required")
        print("Usage: NEXUS_API_KEY=your-key python simple_agent_example.py")
        exit(1)

    print("Simple Nexus Agent Example")
    print("=" * 50)

    # Test the agent
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "List Python files in the current directory"}]},
        config={"metadata": {"x_auth": f"Bearer {api_key}"}},
    )

    print("\nAgent Response:")
    print(result)
