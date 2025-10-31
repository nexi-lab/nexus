#!/usr/bin/env python3
"""OpenAI Agents SDK ReAct Agent with Nexus Filesystem Integration.

This example demonstrates a ReAct (Reasoning + Acting) agent using OpenAI Agents SDK
that interacts with a Nexus filesystem. The agent can:

1. Search for files and patterns using grep and glob
2. Read file contents
3. Analyze and process information
4. Write results back to the filesystem

The agent uses the ReAct pattern (built into OpenAI Agents SDK):
- Think: LLM reasons about the task
- Act: Calls tools (grep, glob, read, write)
- Observe: Receives tool results
- Repeat: Until task is complete

Unlike LangGraph, OpenAI Agents SDK handles the ReAct loop automatically,
requiring significantly less boilerplate code.

Requirements:
    pip install -r requirements.txt

Usage:
    # Set your OpenAI API key:
    export OPENAI_API_KEY="your-key"

    # Optional: Set Nexus server URL for remote server
    export NEXUS_SERVER_URL="http://your-server:8080"
    export NEXUS_API_KEY="your-nexus-key"

    # Run the demo
    python openai_agent_react_demo.py

Example tasks:
    1. Find all Python files with async patterns and create a summary
    2. Search for TODO comments and generate a task list
    3. Analyze code structure and write documentation
"""

import os
import sys
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from agents import Agent, Runner
from nexus_tools import get_nexus_tools

from nexus.remote import RemoteNexusFS


def connect_to_nexus(tenant_id: str = "openai-agents-demo", agent_id: str = "react-agent"):
    """
    Connect to Nexus filesystem (local or remote) with multi-tenancy support.

    By default, uses RemoteNexusFS which connects to a remote server.
    Falls back to local filesystem if no server is configured.

    Args:
        tenant_id: Tenant identifier for data isolation (default: "openai-agents-demo")
        agent_id: Agent identifier for tracking (default: "react-agent")

    Returns:
        NexusFilesystem instance configured for the specified tenant

    Multi-tenancy:
        Nexus supports multi-tenancy, allowing multiple agents or users to share
        the same server while keeping their data isolated. Each tenant has its own
        namespace, and operations are scoped to the tenant_id.

        Example tenant IDs:
        - "openai-agents-demo" - Demo/testing tenant
        - "user-123" - Per-user tenant for SaaS apps
        - "team-acme" - Team-based tenant for collaboration
        - "prod-workflow" - Production workflow tenant
    """
    server_url = os.getenv("NEXUS_SERVER_URL") or os.getenv("NEXUS_URL", "http://localhost:8080")
    api_key = os.getenv("NEXUS_API_KEY")

    # Allow overriding via environment variables
    tenant_id = os.getenv("NEXUS_TENANT_ID", tenant_id)
    agent_id = os.getenv("NEXUS_AGENT_ID", agent_id)

    print(f"Connecting to Nexus server at {server_url}...")
    print(f"  Tenant: {tenant_id}")
    print(f"  Agent: {agent_id}")

    # Use remote server if NEXUS_URL is set, otherwise use local
    use_local = os.getenv("NEXUS_URL") is None and os.getenv("NEXUS_SERVER_URL") is None

    if not use_local:
        try:
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

        except Exception as e:
            print(f"⚠ Could not connect to remote server: {e}")
            print("Falling back to local filesystem...")
    else:
        print("Using local filesystem for demo")

        # Fall back to local filesystem
        from pathlib import Path

        from nexus import NexusFS
        from nexus.backends.local import LocalBackend

        data_dir = Path(f"/tmp/nexus-{tenant_id}")
        backend = LocalBackend(root_path=data_dir)
        nx = NexusFS(
            backend=backend,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        print(f"✓ Using local filesystem at {data_dir}")
        return nx


def run_demo():
    """Run the OpenAI Agent SDK demo."""
    print("=" * 70)
    print("OpenAI Agents SDK ReAct Agent with Nexus Filesystem")
    print("=" * 70)
    print()

    # Check for API key
    if not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY environment variable not set")
        print("\nPlease set your OpenAI API key:")
        print("  export OPENAI_API_KEY='your-key'")
        return

    # Connect to Nexus
    try:
        nx = connect_to_nexus()
    except Exception as e:
        print(f"Error connecting to Nexus: {e}")
        print("\nTo run this demo, you can:")
        print("1. Use local filesystem (default)")
        print("2. Start a remote server: nexus serve")
        print("3. Set NEXUS_SERVER_URL to point to a remote server")
        return

    # Create tools
    print("\nCreating Nexus file operation tools...")
    tools = get_nexus_tools(nx)
    print(f"✓ Created {len(tools)} tools: {[t.name for t in tools]}")

    # Create agent with tools
    print("\nBuilding OpenAI Agent...")
    agent = Agent(
        name="NexusFileAgent",
        instructions="""You are a helpful file analysis assistant with access to a Nexus filesystem.

You have four tools available:
- grep_files: Search file content using patterns
- glob_files: Find files by name pattern
- read_file: Read file content (use preview=True for large files)
- write_file: Write content to files

When given a task:
1. Break it down into steps
2. Use grep/glob to find relevant files
3. Read files to understand content
4. Analyze and synthesize information
5. Write results to the specified output file

Always provide clear, concise summaries and well-structured reports.""",
        tools=tools,
        model="gpt-4o",
    )
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
    print(f"\nTask: {selected_task['prompt']}")
    print("=" * 70)
    print()

    # Run the agent using Runner
    print("Agent starting...\n")

    try:
        # Use Runner.run_sync() for synchronous execution
        result = Runner.run_sync(agent, selected_task["prompt"])

        # Display the execution trace
        print("\n" + "=" * 70)
        print("Agent Execution Complete")
        print("=" * 70)

        # Show final result
        if result and hasattr(result, "final_output"):
            print("\nFinal Response:")
            print(result.final_output)
        elif result:
            print(f"\nResult: {result}")
        else:
            print("\nAgent completed the task.")

        print("\n" + "=" * 70)
        print("Task Complete!")
        print("=" * 70)

    except Exception as e:
        print(f"\nError during agent execution: {e}")
        import traceback

        traceback.print_exc()

    print()


if __name__ == "__main__":
    run_demo()
