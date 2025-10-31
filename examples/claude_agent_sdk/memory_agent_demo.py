#!/usr/bin/env python3
"""Claude Agent with Persistent Memory using Nexus Memory API.

This example demonstrates how to use Nexus Memory API to give Claude agents
persistent, queryable memory across conversations.

Features:
1. Store facts, preferences, and experiences in Nexus Memory
2. Query memory using semantic search
3. Agent remembers context across sessions
4. Scoped memory (agent, user, tenant, global)

Requirements:
    pip install claude-sdk nexus-ai-fs

Usage:
    export ANTHROPIC_API_KEY="your-key"
    python memory_agent_demo.py

Try saying:
    - "My favorite programming language is Python"
    - "I prefer async/await over callbacks"
    - "What do you know about my preferences?"
    - "Remember that I'm working on a Nexus plugin"
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import nexus
from claude_agent_sdk import query


def setup_nexus(agent_id: str = "memory-agent"):
    """Setup Nexus with agent identifier"""
    nx = nexus.connect()
    print("✓ Connected to Nexus")
    print(f"  Agent ID: {agent_id}")
    return nx


def create_memory_tools(nx, agent_id: str):
    """
    Create memory tools that let Claude store and recall information.

    Args:
        nx: NexusFilesystem instance
        agent_id: Agent identifier for scoping memory

    Returns:
        List of memory-related tools
    """

    async def store_fact(fact: str, importance: float = 0.7) -> str:
        """Store a factual piece of information in agent memory.

        Use this to remember important facts, data points, or statements.

        Args:
            fact: The factual information to remember
            importance: How important this fact is (0.0-1.0, default: 0.7)

        Returns:
            Confirmation message

        Examples:
            - store_fact("User prefers Python over JavaScript", 0.8)
            - store_fact("Database password is stored in .env file", 0.9)
        """
        try:
            nx.memory.store(
                content=fact,
                scope="agent",
                memory_type="fact",
                importance=importance,
                metadata={"agent_id": agent_id},
            )
            return f"✓ Stored fact (importance: {importance})"
        except Exception as e:
            return f"Error storing fact: {e}"

    async def store_preference(preference: str, importance: float = 0.6) -> str:
        """Store user preferences or settings.

        Use this to remember how the user likes things done.

        Args:
            preference: The preference to remember
            importance: How important this preference is (0.0-1.0, default: 0.6)

        Returns:
            Confirmation message

        Examples:
            - store_preference("User prefers concise responses")
            - store_preference("Always use async/await for I/O operations")
        """
        try:
            nx.memory.store(
                content=preference,
                scope="agent",
                memory_type="preference",
                importance=importance,
                metadata={"agent_id": agent_id},
            )
            return f"✓ Stored preference (importance: {importance})"
        except Exception as e:
            return f"Error storing preference: {e}"

    async def store_experience(experience: str, importance: float = 0.5) -> str:
        """Store an experience or event that happened.

        Use this to remember conversations, actions taken, or outcomes.

        Args:
            experience: Description of what happened
            importance: How important this experience is (0.0-1.0, default: 0.5)

        Returns:
            Confirmation message

        Examples:
            - store_experience("Helped user debug async code in project.py")
            - store_experience("User reported bug in version 2.1.0")
        """
        try:
            nx.memory.store(
                content=experience,
                scope="agent",
                memory_type="experience",
                importance=importance,
                metadata={"agent_id": agent_id},
            )
            return f"✓ Stored experience (importance: {importance})"
        except Exception as e:
            return f"Error storing experience: {e}"

    async def recall_memories(query_text: str, limit: int = 5) -> str:
        """Search agent memory for relevant information.

        Use this to recall facts, preferences, or experiences related to a topic.

        Args:
            query_text: What to search for (semantic search)
            limit: Maximum number of memories to return (default: 5)

        Returns:
            Formatted list of relevant memories

        Examples:
            - recall_memories("user preferences")
            - recall_memories("what programming language does user like?")
            - recall_memories("previous conversations about databases")
        """
        try:
            results = nx.memory.query(query=query_text, scope="agent", limit=limit)

            if not results:
                return f"No memories found for: {query_text}"

            # Format memories
            lines = [f"Found {len(results)} relevant memories:\n"]
            for i, memory in enumerate(results, 1):
                memory_type = memory.memory_type or "unknown"
                importance = memory.importance or 0.0
                content = memory.content

                lines.append(f"{i}. [{memory_type.upper()}] (importance: {importance:.1f})")
                lines.append(f"   {content}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"Error recalling memories: {e}"

    async def list_recent_memories(limit: int = 10) -> str:
        """List recent memories chronologically.

        Args:
            limit: Number of recent memories to show (default: 10)

        Returns:
            List of recent memories
        """
        try:
            # Query with empty string returns all, sorted by recency
            results = nx.memory.query(query="", scope="agent", limit=limit)

            if not results:
                return "No memories stored yet"

            lines = [f"Most recent {len(results)} memories:\n"]
            for i, memory in enumerate(results, 1):
                memory_type = memory.memory_type or "unknown"
                content = memory.content

                # Truncate long content
                if len(content) > 100:
                    content = content[:97] + "..."

                lines.append(f"{i}. [{memory_type.upper()}] {content}")

            return "\n".join(lines)

        except Exception as e:
            return f"Error listing memories: {e}"

    async def forget_about(topic: str) -> str:
        """Remove memories related to a specific topic.

        WARNING: This permanently deletes memories!

        Args:
            topic: Topic to forget about

        Returns:
            Confirmation of deletion
        """
        try:
            # First query to find relevant memories
            results = nx.memory.query(query=topic, scope="agent", limit=10)

            if not results:
                return f"No memories found about: {topic}"

            # Delete them
            count = 0
            for memory in results:
                try:
                    nx.memory.delete(memory.id)
                    count += 1
                except Exception:
                    pass

            return f"✓ Forgot {count} memories about: {topic}"

        except Exception as e:
            return f"Error forgetting: {e}"

    # Return all memory tools
    return [
        store_fact,
        store_preference,
        store_experience,
        recall_memories,
        list_recent_memories,
        forget_about,
    ]


async def run_conversation(_nx, tools, prompt: str):
    """Run a single conversation turn with memory"""
    print("\n" + "─" * 70)
    print(f"You: {prompt}")
    print("─" * 70)

    # Add system prompt that encourages using memory
    full_prompt = f"""You are a helpful assistant with persistent memory.

IMPORTANT: Use the memory tools to remember important information from our conversation:
- Use store_fact() for factual information
- Use store_preference() for user preferences
- Use store_experience() for experiences or events
- Use recall_memories() to remember relevant past information

Always check your memory first before responding, especially for questions about preferences or past conversations.

User message: {prompt}"""

    print("\nClaude:", end=" ", flush=True)

    assistant_response = ""
    async for message in query(full_prompt, tools=tools):
        # Extract text from assistant messages
        if hasattr(message, "role") and message.role == "assistant" and hasattr(message, "content"):
            for block in message.content:
                if hasattr(block, "text") and block.text:
                    assistant_response = block.text
                    print(block.text, end="", flush=True)

    print("\n")
    return assistant_response


async def interactive_demo():
    """Run an interactive demo with memory"""
    print("=" * 70)
    print("Claude Agent with Persistent Memory (Nexus Memory API)")
    print("=" * 70)

    # Check API key
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("\nError: ANTHROPIC_API_KEY not set")
        print("Please set your Anthropic API key:")
        print("  export ANTHROPIC_API_KEY='your-key'")
        return

    # Setup
    print("\nInitializing...")
    agent_id = "memory-demo-agent"
    nx = setup_nexus(agent_id)

    print("\nCreating memory tools...")
    tools = create_memory_tools(nx, agent_id)
    print(f"✓ Created {len(tools)} memory tools")

    print("\n" + "=" * 70)
    print("Demo Conversation")
    print("=" * 70)
    print("\nThis demo shows how Claude can remember information across messages.")
    print("Try telling Claude facts, preferences, or asking what it remembers!\n")

    # Example conversation
    conversation = [
        "My name is Alex and I'm a software engineer.",
        "I prefer Python over JavaScript, especially for backend work.",
        "I'm currently working on integrating Nexus with Claude Agent SDK.",
        "What do you know about me?",
        "What are my programming preferences?",
        "What am I working on?",
    ]

    for prompt in conversation:
        await run_conversation(nx, tools, prompt)
        await asyncio.sleep(1)  # Small delay between messages

    print("\n" + "=" * 70)
    print("Demo Complete!")
    print("=" * 70)
    print("\nNotice how Claude:")
    print("1. Stored facts and preferences as you told them")
    print("2. Recalled them when asked questions")
    print("3. Memory persists across multiple conversation turns")
    print("\nThis memory is stored in Nexus and persists across Python sessions!")


async def scripted_demo():
    """Run a scripted demo showing memory capabilities"""
    print("=" * 70)
    print("Claude Agent with Persistent Memory - Scripted Demo")
    print("=" * 70)

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("\nError: ANTHROPIC_API_KEY not set")
        return

    agent_id = "scripted-demo-agent"
    nx = setup_nexus(agent_id)
    tools = create_memory_tools(nx, agent_id)

    # Demo scenarios
    print("\n📝 Scenario 1: Learning About User")
    await run_conversation(nx, tools, "I love building AI agents!")

    print("\n📝 Scenario 2: Remembering Preferences")
    await run_conversation(nx, tools, "I prefer async/await over callbacks")

    print("\n📝 Scenario 3: Storing Project Context")
    await run_conversation(nx, tools, "I'm building a Nexus plugin for MCP integration")

    print("\n📝 Scenario 4: Recalling Information")
    await run_conversation(nx, tools, "What do you remember about my preferences?")

    print("\n📝 Scenario 5: Specific Recall")
    await run_conversation(nx, tools, "What project am I working on?")


if __name__ == "__main__":
    # Run interactive demo by default
    # Change to scripted_demo() for a pre-scripted conversation
    asyncio.run(interactive_demo())
