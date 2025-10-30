"""
Nexus Enhanced Tools for DeepAgents

Simplified tool creation for adding Nexus capabilities to agents.
Focus on memory API and grep/glob utilities.
"""

import contextlib
from typing import Any

try:
    from langchain_core.tools import tool
except ImportError:
    raise ImportError("Install langchain: pip install deepagents") from None


def create_nexus_tools(nx: Any, workspace: str = "/") -> list:
    """
    Create enhanced Nexus tools for DeepAgents.

    Args:
        nx: Nexus filesystem instance
        workspace: Base path for operations

    Returns:
        List of LangChain tools
    """
    workspace = workspace.rstrip("/")

    # Memory storage tool
    @tool
    def nexus_store_memory(
        content: str, memory_type: str | None = None, importance: float | None = None
    ) -> str:
        """Store an important insight in persistent memory.

        Use this to remember key learnings that should be recalled in future sessions.

        Args:
            content: The insight or fact to remember
            memory_type: Optional category (e.g., "transformers", "research")
            importance: Optional importance score 0.0-1.0 (default: 0.5)
        """
        try:
            nx.memory.store(content, scope="user", memory_type=memory_type, importance=importance)
            # IMPORTANT: Commit the session to persist the memory
            nx.memory.session.commit()
            return f"✓ Stored memory: {content[:80]}..."
        except Exception as e:
            # Roll back on error to keep session clean
            with contextlib.suppress(Exception):
                nx.memory.session.rollback()
            return f"Error storing memory: {str(e)}"

    # Memory search tool (semantic)
    @tool
    def nexus_query_memory(query: str, memory_type: str | None = None, limit: int = 5) -> str:
        """Search memories using natural language query.

        Find relevant past insights using semantic search.

        Args:
            query: What to remember (e.g., "What do I know about transformers?")
            memory_type: Optional filter by category
            limit: Maximum number of memories (default: 5)
        """
        try:
            memories = nx.memory.search(query, scope="user", memory_type=memory_type, limit=limit)

            if not memories:
                return f"No memories found for: {query}"

            output = [f"Found {len(memories)} relevant memories:\n"]
            for i, mem in enumerate(memories, 1):
                content = mem.get("content", "")
                mem_type = mem.get("memory_type", "")
                output.append(f"{i}. {content}")
                if mem_type:
                    output.append(f"   Type: {mem_type}")

            return "\n".join(output)
        except Exception as e:
            return f"Error: {str(e)}"

    # Grep tool
    @tool
    def nexus_grep(pattern: str, file_pattern: str | None = None) -> str:
        """Search file contents using regex.

        Args:
            pattern: Regex pattern to search for
            file_pattern: Glob to filter files (e.g., "*.md")
        """
        try:
            results = nx.grep(pattern, path=workspace, file_pattern=file_pattern)

            if not results:
                return f"No matches for: {pattern}"

            output = [f"Found {len(results)} matches:\n"]
            for i, match in enumerate(results[:20], 1):
                path = match.get("path", "")
                line = match.get("line", 0)
                text = match.get("text", "")
                output.append(f"{i}. {path}:{line}: {text}")

            if len(results) > 20:
                output.append(f"\n... ({len(results) - 20} more)")

            return "\n".join(output)
        except Exception as e:
            return f"Error: {str(e)}"

    # Glob tool
    @tool
    def nexus_glob(pattern: str) -> str:
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern (e.g., "*.md", "**/*.py")
        """
        try:
            full_pattern = f"{workspace}/{pattern}".replace("//", "/")
            matches = nx.glob(full_pattern)

            if not matches:
                return f"No files matching: {pattern}"

            output = [f"Found {len(matches)} files:\n"]
            for match in matches[:50]:
                output.append(f"  {match}")

            if len(matches) > 50:
                output.append(f"\n... ({len(matches) - 50} more)")

            return "\n".join(output)
        except Exception as e:
            return f"Error: {str(e)}"

    return [nexus_store_memory, nexus_query_memory, nexus_grep, nexus_glob]
