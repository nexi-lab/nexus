"""Tool index for searching MCP tools.

Provides BM25S-based search for finding relevant tools from a large catalog.
This enables efficient tool discovery without loading all tools into context.

Uses the BM25S library (arXiv:2407.03618) for 500x faster search.
Issue: #484
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import bm25s


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
    """BM25S-based search index for MCP tools.

    Indexes tool names and descriptions for efficient keyword-based search.
    Uses BM25S library for 500x faster relevance scoring.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        """Initialize an empty tool index.

        Args:
            k1: Term frequency saturation parameter
            b: Length normalization parameter
        """
        self.k1 = k1
        self.b = b

        self._tools: dict[str, ToolInfo] = {}
        self._servers: set[str] = set()

        # BM25S index state
        self._tool_names: list[str] = []
        self._corpus_tokens: list[list[str]] = []
        self._retriever: bm25s.BM25 | None = None
        self._dirty: bool = False

    def add_tool(self, tool: ToolInfo) -> None:
        """Add a tool to the index."""
        if tool.name in self._tools:
            self.remove_tool(tool.name)

        self._tools[tool.name] = tool
        self._servers.add(tool.server)

        tokens = self._tokenize(f"{tool.name} {tool.description}")
        self._tool_names.append(tool.name)
        self._corpus_tokens.append(tokens)
        self._dirty = True

    def add_tools(self, tools: list[ToolInfo]) -> None:
        """Add multiple tools to the index."""
        for tool in tools:
            if tool.name in self._tools:
                self._remove_internal(tool.name)

            self._tools[tool.name] = tool
            self._servers.add(tool.server)

            tokens = self._tokenize(f"{tool.name} {tool.description}")
            self._tool_names.append(tool.name)
            self._corpus_tokens.append(tokens)

        self._dirty = True
        self._rebuild()

    def remove_tool(self, name: str) -> bool:
        """Remove a tool from the index."""
        if name not in self._tools:
            return False
        self._remove_internal(name)
        self._dirty = True
        return True

    def _remove_internal(self, name: str) -> None:
        """Remove tool without marking dirty."""
        self._tools.pop(name, None)
        if name in self._tool_names:
            idx = self._tool_names.index(name)
            del self._tool_names[idx]
            del self._corpus_tokens[idx]

    def _rebuild(self) -> None:
        """Rebuild BM25S index."""
        if not self._dirty or not self._corpus_tokens:
            self._retriever = None
            self._dirty = False
            return

        self._retriever = bm25s.BM25(k1=self.k1, b=self.b)
        self._retriever.index(self._corpus_tokens)
        self._dirty = False

    def search(self, query: str, top_k: int = 5) -> list[ToolMatch]:
        """Search for tools matching the query."""
        if not query or not self._tools:
            return []

        if self._dirty:
            self._rebuild()

        if self._retriever is None:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        k = min(top_k, len(self._tool_names))
        query_tokenized = bm25s.tokenize([" ".join(query_tokens)])
        results, scores = self._retriever.retrieve(query_tokenized, k=k)

        matches = []
        for idx, score in zip(results[0], scores[0], strict=True):
            if score <= 0:
                continue
            tool_name = self._tool_names[idx]
            tool = self._tools.get(tool_name)
            if tool:
                matches.append(ToolMatch(tool=tool, score=float(score)))

        return matches

    def get_tool(self, name: str) -> ToolInfo | None:
        """Get tool by exact name."""
        return self._tools.get(name)

    def list_tools(self, server: str | None = None) -> list[ToolInfo]:
        """List all tools, optionally filtered by server."""
        if server:
            return [t for t in self._tools.values() if t.server == server]
        return list(self._tools.values())

    def list_servers(self) -> list[str]:
        """List all known servers."""
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
        """Tokenize text for indexing/search."""
        return re.findall(r"\w+", text.lower())
