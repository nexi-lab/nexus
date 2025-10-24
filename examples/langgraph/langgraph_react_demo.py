#!/usr/bin/env python3
"""LangGraph ReAct Agent with Nexus Filesystem Integration.

This example demonstrates a ReAct (Reasoning + Acting) agent using LangGraph
that interacts with a remote Nexus filesystem. The agent can:

1. Search for files and patterns using grep and glob
2. Read file contents using cat/less commands
3. Analyze and process information
4. Write results back to the filesystem

The agent uses the ReAct pattern:
- Think: LLM reasons about the task
- Act: Calls tools (grep, glob, read, write)
- Observe: Receives tool results
- Repeat: Until task is complete

Requirements:
    pip install -r requirements.txt

Usage:
    # Set your API key (choose one):
    export ANTHROPIC_API_KEY="your-key"
    # or
    export OPENAI_API_KEY="your-key"
    # or use OpenRouter for multiple models:
    export OPENROUTER_API_KEY="sk-or-v1-..."

    # Optional: Set Nexus API key for remote server
    export NEXUS_API_KEY="your-nexus-key"

    # Run the demo
    python langgraph_react_demo.py

Example tasks:
    1. Find all Python files with async patterns and create a summary
    2. Search for TODO comments and generate a task list
    3. Analyze code structure and write documentation
"""

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, TypedDict

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph import END, StateGraph, add_messages
from langgraph.prebuilt import ToolNode
from nexus_tools import get_nexus_tools

from nexus.remote import RemoteNexusFS


# Define agent state
class AgentState(TypedDict):
    """State of the ReAct agent."""

    messages: Annotated[Sequence[BaseMessage], add_messages]


def create_agent(tools, llm):
    """
    Create a ReAct agent with the given tools and LLM.

    Args:
        tools: List of tools the agent can use
        llm: Language model for reasoning

    Returns:
        Compiled LangGraph agent
    """

    # Bind tools to LLM
    llm_with_tools = llm.bind_tools(tools)

    # Define the function that calls the model
    def call_model(state: AgentState):
        messages = state["messages"]
        response = llm_with_tools.invoke(messages)
        return {"messages": [response]}

    # Define the function that determines whether to continue or end
    def should_continue(state: AgentState) -> Literal["tools", "end"]:
        messages = state["messages"]
        last_message = messages[-1]

        # If there are no tool calls, then we finish
        if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
            return "end"
        # Otherwise if there are, we continue
        else:
            return "tools"

    # Create the graph
    workflow = StateGraph(AgentState)

    # Add nodes
    workflow.add_node("agent", call_model)
    workflow.add_node("tools", ToolNode(tools))

    # Set the entry point
    workflow.set_entry_point("agent")

    # Add conditional edges
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools": "tools",
            "end": END,
        },
    )

    # Add edge from tools back to agent
    workflow.add_edge("tools", "agent")

    # Compile the graph
    return workflow.compile()


def get_llm():
    """
    Get LLM instance using Nexus LLM abstraction layer.

    This function tries multiple API keys in order:
    1. OpenRouter (recommended - access to all models with one key)
    2. Anthropic (Claude)
    3. OpenAI (GPT-4)

    Returns:
        LangChain LLM instance
    """

    # Try OpenRouter first (recommended)
    if os.getenv("OPENROUTER_API_KEY"):
        print("Using OpenRouter API")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model="anthropic/claude-3-5-sonnet",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            base_url="https://openrouter.ai/api/v1",
            temperature=0.7,
        )

    # Fall back to Anthropic
    elif os.getenv("ANTHROPIC_API_KEY"):
        print("Using Anthropic API (Claude)")
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model="claude-3-5-sonnet-20241022",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            temperature=0.7,
        )

    # Fall back to OpenAI
    elif os.getenv("OPENAI_API_KEY"):
        print("Using OpenAI API (GPT-4)")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model="gpt-4-turbo-preview",
            api_key=os.getenv("OPENAI_API_KEY"),
            temperature=0.7,
        )

    else:
        raise ValueError(
            "No API key found. Please set one of:\n"
            "  - OPENROUTER_API_KEY (recommended - access to all models)\n"
            "  - ANTHROPIC_API_KEY (for Claude)\n"
            "  - OPENAI_API_KEY (for GPT-4)"
        )


def connect_to_nexus(tenant_id: str = "langgraph-demo", agent_id: str = "react-agent"):
    """
    Connect to remote Nexus server with multi-tenancy support.

    Uses the server at http://136.117.224.98 (or localhost for testing).
    You can override with NEXUS_SERVER_URL environment variable.

    Args:
        tenant_id: Tenant identifier for data isolation (default: "langgraph-demo")
        agent_id: Agent identifier for tracking (default: "react-agent")

    Returns:
        NexusFilesystem instance configured for the specified tenant

    Multi-tenancy:
        Nexus supports multi-tenancy, allowing multiple agents or users to share
        the same server while keeping their data isolated. Each tenant has its own
        namespace, and operations are scoped to the tenant_id.

        Example tenant IDs:
        - "langgraph-demo" - Demo/testing tenant
        - "user-123" - Per-user tenant for SaaS apps
        - "team-acme" - Team-based tenant for collaboration
        - "prod-workflow" - Production workflow tenant
    """
    server_url = os.getenv("NEXUS_SERVER_URL", "http://136.117.224.98")
    api_key = os.getenv("NEXUS_API_KEY")

    # Allow overriding via environment variables
    tenant_id = os.getenv("NEXUS_TENANT_ID", tenant_id)
    agent_id = os.getenv("NEXUS_AGENT_ID", agent_id)

    print(f"Connecting to Nexus server at {server_url}...")
    print(f"  Tenant: {tenant_id}")
    print(f"  Agent: {agent_id}")

    # Connect to remote Nexus server using RemoteNexusFS
    nx = RemoteNexusFS(
        server_url=server_url,
        api_key=api_key,
    )

    # Set tenant and agent identifiers for multi-tenancy
    nx.tenant_id = tenant_id
    nx.agent_id = agent_id

    print("✓ Connected to Nexus server")

    return nx


def run_demo():
    """Run the ReAct agent demo."""
    print("=" * 70)
    print("LangGraph ReAct Agent with Nexus Filesystem")
    print("=" * 70)
    print()

    # Connect to Nexus
    try:
        nx = connect_to_nexus()
    except Exception as e:
        print(f"Error connecting to Nexus: {e}")
        print("\nTo run this demo, you need access to a Nexus server.")
        print("You can start a local server with:")
        print("  python examples/py_demo/remote_server_demo.py server")
        print("\nOr set NEXUS_SERVER_URL to point to a remote server.")
        return

    # Create tools
    print("\nCreating Nexus file operation tools...")
    tools = get_nexus_tools(nx)
    print(f"✓ Created {len(tools)} tools: {[t.name for t in tools]}")

    # Get LLM
    print("\nInitializing LLM...")
    try:
        llm = get_llm()
        print("✓ LLM initialized")
    except ValueError as e:
        print(f"\nError: {e}")
        return

    # Create agent
    print("\nBuilding ReAct agent...")
    agent = create_agent(tools, llm)
    print("✓ Agent ready")

    # Example tasks
    tasks = [
        {
            "name": "Search and Analyze Python Files",
            "prompt": (
                "Find all Python files in /workspace that contain 'async def' or 'await'. "
                "Read a couple of them to understand the async patterns being used. "
                "Then write a summary report to /reports/async-patterns.md that includes:\n"
                "1. Number of files using async/await\n"
                "2. Common async patterns you observed\n"
                "3. List of files reviewed\n\n"
                "Keep the report concise but informative."
            ),
        },
        {
            "name": "TODO Task Analysis",
            "prompt": (
                "Search for all TODO and FIXME comments in the codebase. "
                "Categorize them by priority or type if possible. "
                "Write a task list to /reports/todo-list.md."
            ),
        },
        {
            "name": "Documentation Generator",
            "prompt": (
                "Find all Python files in /workspace. "
                "Generate a brief documentation overview in /reports/code-structure.md "
                "that lists the main modules and their apparent purposes based on filenames."
            ),
        },
    ]

    # Run first task by default (you can add menu selection here)
    print("\n" + "=" * 70)
    print("Available Tasks:")
    print("=" * 70)
    for i, task in enumerate(tasks, 1):
        print(f"{i}. {task['name']}")

    # For demo, run the first task
    # You can modify this to let users choose
    selected_task = tasks[0]

    print(f"\nRunning: {selected_task['name']}")
    print("=" * 70)
    print(f"Task: {selected_task['prompt']}")
    print("=" * 70)
    print()

    # Run the agent
    print("Agent starting...\n")

    try:
        result = agent.invoke({"messages": [HumanMessage(content=selected_task["prompt"])]})

        # Display the conversation
        print("\n" + "=" * 70)
        print("Agent Execution Trace")
        print("=" * 70)

        for message in result["messages"]:
            if isinstance(message, HumanMessage):
                print("\n[USER]")
                print(message.content)
            elif isinstance(message, AIMessage):
                if message.content:
                    print("\n[AGENT - Reasoning]")
                    print(message.content)
                if hasattr(message, "tool_calls") and message.tool_calls:
                    print("\n[AGENT - Tool Calls]")
                    for tool_call in message.tool_calls:
                        print(f"  → {tool_call['name']}({tool_call['args']})")
            elif isinstance(message, ToolMessage):
                print(f"\n[TOOL - {message.name}]")
                # Truncate long outputs for readability
                content = str(message.content)
                if len(content) > 500:
                    print(content[:500] + "\n... (truncated)")
                else:
                    print(content)

        print("\n" + "=" * 70)
        print("Task Complete!")
        print("=" * 70)

        # Show final result
        final_message = result["messages"][-1]
        if isinstance(final_message, AIMessage) and final_message.content:
            print("\nFinal Response:")
            print(final_message.content)

    except Exception as e:
        print(f"\nError during agent execution: {e}")
        import traceback

        traceback.print_exc()

    print()


if __name__ == "__main__":
    run_demo()
