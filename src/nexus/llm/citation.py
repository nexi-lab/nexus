"""Citation extraction and management for LLM document reading."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Citation:
    """A citation reference in an LLM response."""

    path: str
    """Path to the source document."""

    chunk_index: int | None = None
    """Index of the chunk within the document."""

    score: float | None = None
    """Relevance score from semantic search."""

    start_offset: int | None = None
    """Start character offset in the document."""

    end_offset: int | None = None
    """End character offset in the document."""


@dataclass
class DocumentReadResult:
    """Result from LLM document reading."""

    answer: str
    """The LLM's answer to the query."""

    citations: list[Citation]
    """Citations/sources used in the answer."""

    sources: list[str]
    """List of source document paths."""

    tokens_used: int | None = None
    """Total tokens used in the request."""

    cost: float | None = None
    """Cost of the request in USD."""

    cached: bool = False
    """Whether this response came from cache."""

    cache_savings: float | None = None
    """Cost savings from cache hit."""

    @classmethod
    def from_cached(
        cls, cached_response: str, chunks: list[dict] | None = None
    ) -> DocumentReadResult:
        """Create result from cached response.

        Args:
            cached_response: Cached LLM response
            chunks: Optional search chunks for citations

        Returns:
            DocumentReadResult with cached flag set
        """
        citations = []
        sources = []

        if chunks:
            for chunk in chunks:
                path = chunk.get("path", "")
                if path and path not in sources:
                    sources.append(path)
                citations.append(
                    Citation(
                        path=path,
                        chunk_index=chunk.get("chunk_index"),
                        score=chunk.get("score"),
                        start_offset=chunk.get("start_offset"),
                        end_offset=chunk.get("end_offset"),
                    )
                )

        return cls(
            answer=cached_response,
            citations=citations,
            sources=sources,
            tokens_used=0,
            cost=0.0,
            cached=True,
        )


class CitationExtractor:
    """Extracts citations from LLM responses."""

    # Common citation patterns
    CITATION_PATTERNS = [
        r"\[Source:\s*([^\]]+)\]",  # [Source: path]
        r"\(Source:\s*([^\)]+)\)",  # (Source: path)
        r"\[(\d+)\]",  # [1], [2], etc.
        r"\[\^(\d+)\]",  # [^1], [^2], etc.
    ]

    @staticmethod
    def extract_citations(
        answer: str, chunks: list[dict], include_all_sources: bool = True
    ) -> list[Citation]:
        """Extract citations from LLM answer.

        Args:
            answer: The LLM's answer text
            chunks: List of search result chunks that were used as context
            include_all_sources: If True, include all chunks as potential citations
                               even if not explicitly referenced (default: True)

        Returns:
            List of citations found in the answer
        """
        citations = []
        referenced_paths = set()

        # Look for explicit citation patterns
        for pattern in CitationExtractor.CITATION_PATTERNS:
            matches = re.findall(pattern, answer)
            for match in matches:
                # Try to match against chunk paths
                for chunk in chunks:
                    chunk_path = chunk.get("path", "")
                    # Check if path matches (substring match or exact)
                    if (
                        match in chunk_path or chunk_path in match
                    ) and chunk_path not in referenced_paths:
                        referenced_paths.add(chunk_path)
                        citations.append(
                            Citation(
                                path=chunk_path,
                                chunk_index=chunk.get("chunk_index"),
                                score=chunk.get("score"),
                                start_offset=chunk.get("start_offset"),
                                end_offset=chunk.get("end_offset"),
                            )
                        )

        # If include_all_sources, add all chunks as potential citations
        if include_all_sources:
            for chunk in chunks:
                chunk_path = chunk.get("path", "")
                if chunk_path and chunk_path not in referenced_paths:
                    citations.append(
                        Citation(
                            path=chunk_path,
                            chunk_index=chunk.get("chunk_index"),
                            score=chunk.get("score"),
                            start_offset=chunk.get("start_offset"),
                            end_offset=chunk.get("end_offset"),
                        )
                    )

        return citations
