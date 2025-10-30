#!/usr/bin/env python3
"""
Demo 1: Drop-In Replacement with Nexus Backend

This demo shows the basic integration of Nexus as a filesystem backend for
DeepAgents research agent. The agent works exactly as before, but files are
stored in Nexus instead of local disk.

What You Get (Tier 1 - Production Readiness):
- ✅ Automatic versioning of all file writes
- ✅ Persistent workspace (survives restarts)
- ✅ Audit trail of agent operations
- ✅ Time-travel debugging capabilities

The agent's behavior is identical - this is purely infrastructure improvement.
"""

import contextlib
import os
import sys
from pathlib import Path

# Add parent directory to path to import nexus_backend
sys.path.insert(0, str(Path(__file__).parent.parent))

from nexus_backend import NexusBackend

import nexus

try:
    from deepagents import create_deep_agent
    from langchain_core.tools import tool

    try:
        from langchain_tavily import TavilySearch as TavilySearchResults
    except ImportError:
        # Fallback to deprecated version
        from langchain_community.tools.tavily_search import TavilySearchResults
except ImportError as e:
    print("Error: Missing dependencies. Install with:")
    print("  pip install -r requirements.txt")
    print(f"\nDetails: {e}")
    sys.exit(1)


def create_research_agent_with_nexus(nx, workspace="/research"):
    """
    Create a research agent that uses Nexus as its filesystem backend.

    Args:
        nx: Nexus connection
        workspace: Base path in Nexus for agent operations

    Returns:
        DeepAgents agent configured with Nexus backend
    """

    # Create internet search tool
    @tool
    def internet_search(
        query: str,
        max_results: int = 5,
        topic: str = "general",
        include_raw_content: bool = False,
    ):
        """Search the internet for information.

        Args:
            query: The search query
            max_results: Maximum number of results (default: 5)
            topic: Topic for search - general, news, or finance (default: general)
            include_raw_content: Include raw content in results (default: False)
        """
        # Check for Tavily API key
        if not os.getenv("TAVILY_API_KEY"):
            return "Error: TAVILY_API_KEY environment variable not set"

        search = TavilySearchResults(
            max_results=max_results,
            topic=topic,
            include_raw_content=include_raw_content,
        )
        return search.invoke({"query": query})

    # Create agent with Nexus backend
    # Note: create_deep_agent automatically adds FilesystemMiddleware with this backend
    agent = create_deep_agent(
        model="anthropic:claude-sonnet-4-20250514",
        backend=NexusBackend(nx, base_path=workspace),  # Pass backend directly!
        tools=[internet_search],
    )

    return agent


def main():
    """Run the research agent demo."""

    print("=" * 70)
    print("DeepAgents + Nexus: Drop-In Replacement Demo")
    print("=" * 70)
    print()

    # Check for required API keys
    if not os.getenv("TAVILY_API_KEY"):
        print("⚠️  Warning: TAVILY_API_KEY not set")
        print("   The agent won't be able to search the internet.")
        print("   Get a key at: https://tavily.com")
        print()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("❌ Error: ANTHROPIC_API_KEY not set")
        print("   Get a key at: https://console.anthropic.com")
        sys.exit(1)

    # Connect to Nexus (embedded mode)
    print("📁 Connecting to Nexus...")
    nx = nexus.connect()
    print("✓ Connected to Nexus (embedded mode)")
    print()

    # Create workspace
    workspace = "/research-demo"
    print(f"📂 Creating workspace: {workspace}")
    with contextlib.suppress(Exception):
        nx.mkdir(workspace, parents=True)  # Directory may already exist
    print()

    # Create agent
    print("🤖 Creating research agent with Nexus backend...")
    agent = create_research_agent_with_nexus(nx, workspace=workspace)
    print("✓ Agent created")
    print()

    # Run research
    print("=" * 70)
    print("🔍 Starting Research")
    print("=" * 70)
    print()

    # Research question with explicit file writing instruction
    question = """Research the key components of transformer architecture in AI.

Please:
1. Write the original question to a file called 'question.txt'
2. Conduct your research
3. Write a comprehensive report to 'final_report.md' in markdown format
4. Include sources and citations in your report

Focus on: architecture components, attention mechanisms, and real-world applications."""

    print(f"Question: {question}")
    print()
    print("Agent is working... (this may take 30-60 seconds)")
    print("-" * 70)

    try:
        # Run the agent
        agent.invoke({"messages": [{"role": "user", "content": question}]})

        print()
        print("-" * 70)
        print("✓ Research complete!")
        print()

    except Exception as e:
        print()
        print(f"❌ Error running agent: {e}")
        print()
        return

    # Show what was written to Nexus
    print("=" * 70)
    print("📊 Files in Nexus Workspace")
    print("=" * 70)
    print()

    files = nx.list(workspace)
    for file_path in files:
        # Extract filename from full path
        file_name = file_path.split("/")[-1] if "/" in file_path else file_path

        # Get file size
        try:
            content = nx.read(file_path)
            size = len(content)
        except Exception:
            size = 0

        print(f"📄 {file_name}")
        print(f"   Path: {file_path}")
        print(f"   Size: {size} bytes")

        # Check versions
        try:
            versions = nx.list_versions(file_path)
            if len(versions) > 1:
                print(f"   Versions: {len(versions)} (edited {len(versions) - 1} times)")
        except Exception:
            pass

        print()

    # Show the final report
    print("=" * 70)
    print("📝 Final Report")
    print("=" * 70)
    print()

    try:
        report = nx.read(f"{workspace}/final_report.md").decode("utf-8")
        print(report)
        print()
    except FileNotFoundError:
        print("(No final_report.md found)")
        print()

    # Show versioning capabilities
    print("=" * 70)
    print("⏱️  Time-Travel Debugging (Nexus Feature)")
    print("=" * 70)
    print()

    try:
        versions = nx.list_versions(f"{workspace}/final_report.md")
        print(f"The report has {len(versions)} versions:")
        print()

        for i, version in enumerate(versions, 1):
            version_id = version.get("version", i)
            created_at = version.get("created_at", "unknown")
            print(f"  v{i}: Version {version_id}")
            print(f"      Created: {created_at}")

        print()
        print("💡 You can read any version with:")
        print(f"   nx.get_version('{workspace}/final_report.md', 1)")
        print()

    except FileNotFoundError:
        print("(No versions found)")
        print()

    # Show audit trail
    print("=" * 70)
    print("🔍 Audit Trail (Nexus Feature)")
    print("=" * 70)
    print()

    try:
        audit_log = nx.audit.query(object=f"{workspace}/final_report.md", limit=10)
        print("Recent operations on final_report.md:")
        print()

        for entry in audit_log[:5]:  # Show last 5
            action = entry.get("action", "unknown")
            timestamp = entry.get("timestamp", "unknown")
            print(f"  {timestamp}: {action}")

        print()

    except Exception as e:
        print(f"(Audit trail not available: {e})")
        print()

    # Summary
    print("=" * 70)
    print("✅ Demo Complete!")
    print("=" * 70)
    print()
    print("What you got with Nexus (no code changes to agent logic):")
    print()
    print("  ✅ Automatic versioning - Track report evolution")
    print("  ✅ Persistent storage - Files survive restarts")
    print("  ✅ Audit trail - Know what agent did and when")
    print("  ✅ Time-travel debugging - Reproduce any state")
    print()
    print("Next: Try Tier 2 demos for enhanced agent capabilities!")
    print()


if __name__ == "__main__":
    main()
