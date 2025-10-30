#!/usr/bin/env python3
"""
Demo 2: Workflows - Auto-Process Agent Output

This demo shows how Nexus workflows automatically process files written by
the DeepAgents research agent - NO need to tell the agent to store memories!

Scenario:
1. Register workflow that listens for file writes in agent workspace
2. Agent researches and writes files (same as demo_1)
3. Workflow automatically triggers when agent writes files
4. Workflow extracts insights and stores them in memory
5. Shows event-driven, automatic agent output processing

Key Value: Zero prompt engineering - automatic memory storage!
"""

import asyncio
import contextlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nexus_backend import NexusBackend

import nexus
from nexus.workflows import WorkflowAPI

try:
    from deepagents import create_deep_agent
    from langchain_core.tools import tool

    try:
        from langchain_tavily import TavilySearch as TavilySearchResults
    except ImportError:
        from langchain_community.tools.tavily_search import TavilySearchResults
except ImportError as e:
    print("Error: Missing dependencies")
    print(f"Details: {e}")
    sys.exit(1)


def create_research_agent(nx, workspace):
    """Create research agent with internet search."""

    @tool
    def internet_search(query: str, max_results: int = 5):
        """Search the internet for information."""
        if not os.getenv("TAVILY_API_KEY"):
            return "Error: TAVILY_API_KEY not set"
        search = TavilySearchResults(max_results=max_results)
        return search.invoke({"query": query})

    agent = create_deep_agent(
        model="anthropic:claude-sonnet-4-20250514",
        backend=NexusBackend(nx, base_path=workspace),
        tools=[internet_search],
    )

    return agent


async def main_async():
    """Run the demo (async version for workflows)."""

    print("=" * 70)
    print("Demo 2: Workflows - Auto-Process Agent Output")
    print("=" * 70)
    print()

    # Check API keys
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("❌ Error: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    has_tavily = bool(os.getenv("TAVILY_API_KEY"))
    if not has_tavily:
        print("⚠️  Warning: TAVILY_API_KEY not set")
        print("   Agent can write files but won't search internet")
        print()

    # Connect to Nexus
    print("📁 Connecting to Nexus...")
    nx = nexus.connect(config={"enable_workflows": True})
    print("✓ Connected (workflows enabled)")
    print()

    workspace = "/workflow-demo"

    # Clean workspace
    with contextlib.suppress(Exception):
        nx.rmdir(workspace, recursive=True)
    nx.mkdir(workspace, parents=True)

    # ===== Register Workflow =====
    print("=" * 70)
    print("🔧 Registering Workflow")
    print("=" * 70)
    print()

    # Define workflow using Python dict (could also use YAML)
    workflow_def = {
        "name": "agent-output-processor",
        "version": "1.0",
        "description": "Auto-process DeepAgents research output",
        "triggers": [
            {
                "type": "file_write",
                "pattern": f"{workspace}/*.md",  # Match markdown files in workspace
            }
        ],
        "actions": [
            {
                "name": "store-insight",
                "type": "python",
                "code": """
import nexus

# Connect to Nexus to access memory API
nx = nexus.connect()

# Get file info
file_path = context.file_path
filename = file_path.split('/')[-1]

# Read content to determine memory type
try:
    content_bytes = nx.read(file_path)
    content = content_bytes.decode('utf-8')

    # Extract memory type from filename
    if 'transformer' in filename.lower():
        memory_type = 'transformers'
    elif 'vision' in filename.lower():
        memory_type = 'vision_transformers'
    else:
        memory_type = 'research'

    # Store insight about this file
    insight = f"Agent created {filename} ({len(content)} bytes) about {memory_type}"

    nx.memory.store(
        content=insight,
        scope="user",
        memory_type=memory_type,
        importance=0.8
    )
    nx.memory.session.commit()

    result = f"✓ Stored: {insight}"
except Exception as e:
    result = f"Error: {str(e)}"
                    """,
            }
        ],
    }

    # Load workflow
    print("Workflow configuration:")
    print(f"  Name: {workflow_def['name']}")
    print(f"  Trigger: file_write on {workspace}/*.md")
    print("  Action: Store insight in memory")
    print()

    workflows = WorkflowAPI()
    success = workflows.load(workflow_def, enabled=True)

    if success:
        print("✓ Workflow registered and enabled!")
        print()
    else:
        print("❌ Failed to register workflow")
        return

    # ===== Run Agent =====
    print("=" * 70)
    print("🤖 Agent Task: Research Transformers")
    print("=" * 70)
    print()

    agent = create_research_agent(nx, workspace)

    task = """Research transformer architecture basics.

Write a brief summary to 'transformers.md' covering:
- What transformers are
- Key innovation (attention mechanism)
- Why they're important

Keep it concise (2-3 paragraphs)."""

    print("Task:", task)
    print()
    print("Agent working...")
    print("-" * 70)

    try:
        agent.invoke({"messages": [{"role": "user", "content": task}]})
        print()
        print("-" * 70)
        print("✓ Agent completed!")
    except Exception as e:
        print(f"❌ Error: {e}")
        return

    # Give workflow time to process
    print()
    print("⏳ Waiting for workflow to process...")
    await asyncio.sleep(10)  # Increased wait time for background thread

    # ===== Show Results =====
    print()
    print("=" * 70)
    print("📊 Results")
    print("=" * 70)
    print()

    # Show files
    print("Files created by agent:")
    files = nx.list(workspace)
    for f in files:
        size = len(nx.read(f))
        print(f"  📄 {f.split('/')[-1]} ({size} bytes)")
    print()

    # Show memories stored by workflow
    print("Memories stored by workflow:")
    memories = nx.memory.search("", scope="user", limit=10)

    if memories:
        for i, mem in enumerate(memories, 1):
            content = mem.get("content", "")
            mem_type = mem.get("memory_type", "")
            print(f"  {i}. {content}")
            if mem_type:
                print(f"     Type: {mem_type}")
    else:
        print("  (None - workflow may still be processing)")
    print()

    # Workflow executed automatically - check logs for details
    print("✅ Workflow executed automatically on file write")

    # Summary
    print("=" * 70)
    print("✅ Demo Complete!")
    print("=" * 70)
    print()
    print("What happened:")
    print()
    print("  1️⃣  Agent wrote files → Nexus storage (Tier 1)")
    print("  2️⃣  Workflow detected file write events")
    print("  3️⃣  Workflow auto-processed and stored insights")
    print("  4️⃣  No agent prompt engineering required!")
    print()
    print("This is Tier 2 done right:")
    print("  ✅ Event-driven automation")
    print("  ✅ Separation of concerns (agent writes, workflow processes)")
    print("  ✅ Reliable (always triggers, no LLM behavior issues)")
    print("  ✅ Extensible (add more workflow actions as needed)")
    print()
    print("Next demo: See demo_1_drop_in.py for full research workflow")
    print()


def main():
    """Run the demo."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
