#!/usr/bin/env python3
"""Simple ReAct Agent using LangGraph's Prebuilt create_react_agent.

This example demonstrates how to use LangGraph's prebuilt create_react_agent
function to quickly build a ReAct agent with Nexus filesystem integration.

Requirements:
    pip install langgraph langchain-anthropic

Usage:
    from nexus.remote import RemoteNexusFS
    from nexus_tools import get_nexus_tools
    from react_agent import agent

    # Use the agent
    result = agent.invoke({"messages": [{"role": "user", "content": "Find all Python files"}]})
"""

import os

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

from nexus.remote import RemoteNexusFS
from nexus_tools import get_nexus_tools

print(os.getenv("NEXUS_API_KEY"))
print(os.getenv("NEXUS_SERVER_URL"))
print(os.getenv("NEXUS_TENANT_ID"))
print(os.getenv("NEXUS_AGENT_ID"))

# Connect to Nexus server
nx = RemoteNexusFS(
    server_url=os.getenv("NEXUS_SERVER_URL", "http://localhost:8080"),
    # api_key=os.getenv("NEXUS_API_KEY", "sk-default_joe_9846f79b_6dd5743425680ea7221da8007423c4d9"),
    # api_key="sk-alice,alice__ef5849ee_3d96d4b4ded670b3f50537fa1a4ce24e",
    api_key="sk-default_alice_4e0e11f4_73fefbce9cb49fddbe5bfc6d98b5593c",
)


# Create tools
tools = get_nexus_tools(nx)

# Create LLM
llm = ChatAnthropic(
    model="claude-sonnet-4-5-20250929",
)

# Create prebuilt ReAct agent
agent = create_react_agent(
    model=llm,
    tools=tools,
)


if __name__ == "__main__":
    # Example usage
    print("Testing ReAct agent...")
    result = agent.invoke({
        "messages": [{"role": "user", "content": "Find all Python files and count them"}]
    })
    print(result)
