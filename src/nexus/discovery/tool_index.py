"""Tool index for searching MCP tools.

Provides BM25-based search for finding relevant tools from a large catalog.
This enables efficient tool discovery without loading all tools into context.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolInfo:
    """Information about an MCP tool."""

    name: str
    description: str
    server: str
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "description": self.description,
            "server": self.server,
            "input_schema": self.input_schema,
        }


@dataclass
class ToolMatch:
    """A tool search result with relevance score."""

    tool: ToolInfo
    score: float

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            **self.tool.to_dict(),
            "score": round(self.score, 4),
        }


class ToolIndex:
    """BM25-based search index for MCP tools.

    Indexes tool names and descriptions for efficient keyword-based search.
    Uses BM25 ranking algorithm for relevance scoring.
    """

    # BM25 parameters
    k1: float = 1.5  # Term frequency saturation
    b: float = 0.75  # Length normalization

    def __init__(self) -> None:
        """Initialize an empty tool index."""
        self._tools: dict[str, ToolInfo] = {}
        self._servers: set[str] = set()

        # Inverted index: token -> set of tool names
        self._inverted_index: dict[str, set[str]] = {}

        # Document lengths for BM25
        self._doc_lengths: dict[str, int] = {}
        self._avg_doc_length: float = 0.0

        # IDF cache
        self._idf_cache: dict[str, float] = {}

    def add_tool(self, tool: ToolInfo) -> None:
        """Add a tool to the index.

        Args:
            tool: Tool information to index
        """
        self._tools[tool.name] = tool
        self._servers.add(tool.server)

        # Tokenize name and description
        tokens = self._tokenize(f"{tool.name} {tool.description}")
        self._doc_lengths[tool.name] = len(tokens)

        # Update inverted index
        for token in set(tokens):  # Use set to count each token once per doc
            if token not in self._inverted_index:
                self._inverted_index[token] = set()
            self._inverted_index[token].add(tool.name)

        # Update average document length
        self._update_avg_doc_length()

        # Clear IDF cache (document frequencies changed)
        self._idf_cache.clear()

    def add_tools(self, tools: list[ToolInfo]) -> None:
        """Add multiple tools to the index.

        Args:
            tools: List of tools to index
        """
        for tool in tools:
            self.add_tool(tool)

    def remove_tool(self, name: str) -> bool:
        """Remove a tool from the index.

        Args:
            name: Tool name to remove

        Returns:
            True if tool was found and removed, False otherwise
        """
        if name not in self._tools:
            return False

        tool = self._tools.pop(name)

        # Update inverted index
        tokens = self._tokenize(f"{tool.name} {tool.description}")
        for token in set(tokens):
            if token in self._inverted_index:
                self._inverted_index[token].discard(name)
                if not self._inverted_index[token]:
                    del self._inverted_index[token]

        # Remove doc length
        del self._doc_lengths[name]

        # Update average and clear cache
        self._update_avg_doc_length()
        self._idf_cache.clear()

        return True

    def search(self, query: str, top_k: int = 5) -> list[ToolMatch]:
        """Search for tools matching the query.

        Args:
            query: Search query
            top_k: Maximum number of results to return

        Returns:
            List of matching tools sorted by relevance score (descending)
        """
        if not query or not self._tools:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        # Calculate BM25 scores for each document
        scores: dict[str, float] = {}

        for token in query_tokens:
            if token not in self._inverted_index:
                continue

            idf = self._get_idf(token)

            for tool_name in self._inverted_index[token]:
                # Calculate term frequency in this document
                tool = self._tools[tool_name]
                doc_tokens = self._tokenize(f"{tool.name} {tool.description}")
                tf = doc_tokens.count(token)

                # BM25 scoring
                doc_len = self._doc_lengths[tool_name]
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (
                    1 - self.b + self.b * (doc_len / self._avg_doc_length)
                )
                score = idf * (numerator / denominator)

                if tool_name not in scores:
                    scores[tool_name] = 0.0
                scores[tool_name] += score

        # Sort by score and return top_k
        sorted_tools = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

        return [ToolMatch(tool=self._tools[name], score=score) for name, score in sorted_tools]

    def get_tool(self, name: str) -> ToolInfo | None:
        """Get tool by exact name.

        Args:
            name: Exact tool name

        Returns:
            Tool info if found, None otherwise
        """
        return self._tools.get(name)

    def list_tools(self, server: str | None = None) -> list[ToolInfo]:
        """List all tools, optionally filtered by server.

        Args:
            server: Optional server name to filter by

        Returns:
            List of tools
        """
        if server:
            return [t for t in self._tools.values() if t.server == server]
        return list(self._tools.values())

    def list_servers(self) -> list[str]:
        """List all known servers.

        Returns:
            List of server names
        """
        return sorted(self._servers)

    @property
    def tool_count(self) -> int:
        """Number of tools in the index."""
        return len(self._tools)

    @property
    def server_count(self) -> int:
        """Number of servers in the index."""
        return len(self._servers)

    def _tokenize(self, text: str) -> list[str]:
        """Tokenize text for indexing/search.

        Args:
            text: Text to tokenize

        Returns:
            List of lowercase tokens
        """
        # Simple tokenization: split on non-alphanumeric, lowercase
        tokens = re.findall(r"\w+", text.lower())
        return tokens

    def _update_avg_doc_length(self) -> None:
        """Update average document length."""
        if self._doc_lengths:
            self._avg_doc_length = sum(self._doc_lengths.values()) / len(self._doc_lengths)
        else:
            self._avg_doc_length = 0.0

    def _get_idf(self, token: str) -> float:
        """Get inverse document frequency for a token.

        Args:
            token: Token to get IDF for

        Returns:
            IDF score
        """
        if token in self._idf_cache:
            return self._idf_cache[token]

        n = len(self._tools)
        df = len(self._inverted_index.get(token, set()))

        # BM25 IDF formula
        idf = math.log((n - df + 0.5) / (df + 0.5) + 1)
        self._idf_cache[token] = idf
        return idf
