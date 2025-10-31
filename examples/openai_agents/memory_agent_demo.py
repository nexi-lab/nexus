#!/usr/bin/env python3
"""OpenAI Agents SDK with Nexus Persistent Memory.

This example demonstrates how to use Nexus Memory API with OpenAI Agents SDK
to create agents with persistent memory across sessions.

Features:
- Store facts, preferences, and experiences in Nexus Memory API
- Query stored memories using semantic search
- Memory persists across Python sessions
- Scoped memory per agent for isolation

Requirements:
    pip install -r requirements.txt

Usage:
    # Set your OpenAI API key:
    export OPENAI_API_KEY="your-key"

    # Optional: Use remote Nexus server
    export NEXUS_SERVER_URL="http://your-server:8080"
    export NEXUS_API_KEY="your-nexus-key"

    # Run the demo
    python memory_agent_demo.py

Example interactions:
    1. "Remember that I prefer Python over JavaScript"
    2. "What programming language do I prefer?"
    3. "Remember that our API uses JWT authentication"
    4. "How does our API handle authentication?"
"""

import os
import sys
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from agents import Agent, Runner, function_tool

from nexus.remote import RemoteNexusFS


def connect_to_nexus(tenant_id: str = "openai-memory-demo", agent_id: str = "memory-agent"):
    """
    Connect to Nexus filesystem with memory support.

    Args:
        tenant_id: Tenant identifier for data isolation
        agent_id: Agent identifier for memory scoping

    Returns:
        NexusFilesystem instance
    """
    server_url = os.getenv("NEXUS_SERVER_URL", "http://136.117.224.98")
    api_key = os.getenv("NEXUS_API_KEY")

    # Allow overriding via environment variables
    tenant_id = os.getenv("NEXUS_TENANT_ID", tenant_id)
    agent_id = os.getenv("NEXUS_AGENT_ID", agent_id)

    print(f"Connecting to Nexus server at {server_url}...")
    print(f"  Tenant: {tenant_id}")
    print(f"  Agent: {agent_id}")

    try:
        nx = RemoteNexusFS(
            server_url=server_url,
            api_key=api_key,
        )

        nx.tenant_id = tenant_id
        nx.agent_id = agent_id

        print("✓ Connected to Nexus server with memory support")
        return nx

    except Exception as e:
        print(f"⚠ Could not connect to remote server: {e}")
        print("Falling back to local filesystem...")

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


def create_memory_tools(nx):
    """
    Create memory-enabled tools using Nexus Memory API.

    Args:
        nx: NexusFilesystem instance

    Returns:
        List of function tools for memory operations
    """

    @function_tool
    async def store_memory(content: str, memory_type: str = "fact") -> str:
        """Store information in persistent memory.

        Use this tool to remember important facts, preferences, or experiences
        that should be recalled in future conversations.

        Args:
            content: The information to remember
            memory_type: Type of memory - "fact", "preference", or "experience" (default: "fact")

        Returns:
            Success message confirming storage

        Examples:
            - store_memory("User prefers Python over JavaScript", "preference")
            - store_memory("Our API uses JWT authentication", "fact")
            - store_memory("Successfully deployed feature X on 2025-01-15", "experience")
        """
        try:
            # Store in Nexus Memory API with agent scope
            nx.memory.store(content=content, scope=f"agent-{nx.agent_id}", memory_type=memory_type)
            return f"✓ Stored {memory_type}: {content}"
        except Exception as e:
            return f"Error storing memory: {str(e)}"

    @function_tool
    async def recall_memory(query: str, limit: int = 5) -> str:
        """Query stored memories using semantic search.

        Use this tool to recall previously stored information.
        Searches all stored memories and returns the most relevant ones.

        Args:
            query: What to search for in stored memories
            limit: Maximum number of memories to return (default: 5)

        Returns:
            String containing relevant memories, or "No memories found"

        Examples:
            - recall_memory("programming language preferences")
            - recall_memory("authentication")
            - recall_memory("recent deployments")
        """
        try:
            # Query Nexus Memory API with agent scope
            results = nx.memory.query(query, scope=f"agent-{nx.agent_id}", limit=limit)

            if not results:
                return "No relevant memories found."

            # Format results
            output = [f"Found {len(results)} relevant memories:\n"]
            for i, memory in enumerate(results, 1):
                mem_type = getattr(memory, "memory_type", "unknown")
                output.append(f"{i}. [{mem_type}] {memory.content}")

            return "\n".join(output)

        except Exception as e:
            return f"Error querying memory: {str(e)}"

    @function_tool
    async def list_all_memories() -> str:
        """List all stored memories.

        Use this tool to see everything that has been stored in memory.
        Useful for reviewing what the agent remembers.

        Returns:
            String listing all stored memories, organized by type
        """
        try:
            # Get all memories for this agent
            memories = nx.memory.list(scope=f"agent-{nx.agent_id}")

            if not memories:
                return "No memories stored yet."

            # Group by type
            by_type = {}
            for mem in memories:
                mem_type = getattr(mem, "memory_type", "unknown")
                if mem_type not in by_type:
                    by_type[mem_type] = []
                by_type[mem_type].append(mem.content)

            # Format output
            output = [f"Total memories: {len(memories)}\n"]
            for mem_type, items in by_type.items():
                output.append(f"\n{mem_type.upper()} ({len(items)}):")
                for item in items:
                    output.append(f"  - {item}")

            return "\n".join(output)

        except Exception as e:
            return f"Error listing memories: {str(e)}"

    return [store_memory, recall_memory, list_all_memories]


def run_demo():
    """Run the memory agent demo."""
    print("=" * 70)
    print("OpenAI Agents SDK with Nexus Persistent Memory")
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
        return

    # Create memory tools
    print("\nCreating memory tools...")
    tools = create_memory_tools(nx)
    print(f"✓ Created {len(tools)} memory tools")

    # Create agent with memory
    print("\nBuilding OpenAI Agent with memory...")
    agent = Agent(
        name="MemoryAgent",
        instructions="""You are a helpful assistant with persistent memory.

You have three memory tools:
- store_memory: Save important information for future recall
- recall_memory: Search for previously stored information
- list_all_memories: See all stored memories

When interacting with users:
1. Store important facts, preferences, and experiences they share
2. Recall relevant information from past conversations
3. Use memory to provide personalized, context-aware responses

Always proactively store useful information and recall relevant memories.""",
        tools=tools,
        model="gpt-4o",
    )
    print("✓ Agent ready with memory capabilities")

    # Demo conversations
    conversations = [
        "Remember that I prefer Python over JavaScript for backend development.",
        "Also remember that our API uses JWT tokens for authentication.",
        "What programming language do I prefer?",
        "How does our API handle authentication?",
        "Show me everything you remember about me.",
    ]

    print("\n" + "=" * 70)
    print("Demo Conversation")
    print("=" * 70)

    for i, message in enumerate(conversations, 1):
        print(f"\n[{i}] User: {message}")
        print("-" * 70)

        try:
            # Use Runner.run_sync() for synchronous execution
            result = Runner.run_sync(agent, message)

            if result and hasattr(result, "final_output"):
                print(f"Agent: {result.final_output}")
            else:
                print("Agent: [Task completed]")

        except Exception as e:
            print(f"Error: {e}")
            import traceback

            traceback.print_exc()

    print("\n" + "=" * 70)
    print("Demo Complete!")
    print("=" * 70)
    print("\nThe agent's memories persist across Python sessions.")
    print("Run this script again to see how it remembers your preferences!")


if __name__ == "__main__":
    run_demo()
