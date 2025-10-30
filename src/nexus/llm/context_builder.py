"""Context building for LLM document reading.

Builds optimal context from search results for LLM prompts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.search.semantic import SemanticSearchResult


class ContextBuilder:
    """Builds context from search results for LLM prompts."""

    def __init__(self, max_context_tokens: int = 3000):
        """Initialize context builder.

        Args:
            max_context_tokens: Maximum number of tokens for context
        """
        self.max_context_tokens = max_context_tokens

    def build_context(
        self,
        chunks: list[SemanticSearchResult],
        include_metadata: bool = True,
        include_scores: bool = True,
    ) -> str:
        """Build context from search result chunks.

        Args:
            chunks: List of search results
            include_metadata: Whether to include metadata like source path
            include_scores: Whether to include relevance scores

        Returns:
            Formatted context string for LLM prompt
        """
        if not chunks:
            return ""

        context_parts = []
        total_tokens = 0

        for i, chunk in enumerate(chunks):
            # Estimate tokens (rough approximation: 1 token ≈ 4 chars)
            chunk_tokens = len(chunk.chunk_text) // 4

            if total_tokens + chunk_tokens > self.max_context_tokens:
                break

            # Build chunk header with metadata
            chunk_header_parts = []
            if include_metadata:
                chunk_header_parts.append(f"Source: {chunk.path}")
                if chunk.chunk_index is not None:
                    chunk_header_parts.append(f"Chunk: {chunk.chunk_index}")
            if include_scores and chunk.score is not None:
                chunk_header_parts.append(f"Relevance: {chunk.score:.2f}")

            chunk_header = ", ".join(chunk_header_parts) if chunk_header_parts else f"[{i + 1}]"

            # Format chunk
            context_parts.append(f"[{chunk_header}]\n{chunk.chunk_text}\n")
            total_tokens += chunk_tokens

        return "\n".join(context_parts)

    def build_simple_context(self, chunks: list[SemanticSearchResult]) -> str:
        """Build simple context without metadata.

        Args:
            chunks: List of search results

        Returns:
            Simple concatenated context
        """
        return self.build_context(chunks, include_metadata=False, include_scores=False)

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for text.

        Args:
            text: Text to estimate tokens for

        Returns:
            Estimated token count
        """
        # Rough approximation: 1 token ≈ 4 characters
        return len(text) // 4

    def build_context_with_budget(
        self,
        chunks: list[SemanticSearchResult],
        system_prompt_tokens: int = 100,
        query_tokens: int = 50,
        max_output_tokens: int = 1000,
        model_context_window: int = 8000,
    ) -> str:
        """Build context that fits within token budget.

        Args:
            chunks: List of search results
            system_prompt_tokens: Tokens used by system prompt
            query_tokens: Tokens used by user query
            max_output_tokens: Maximum tokens for LLM output
            model_context_window: Total context window for the model

        Returns:
            Context string that fits within budget
        """
        # Calculate available tokens for context
        reserved_tokens = system_prompt_tokens + query_tokens + max_output_tokens
        available_tokens = model_context_window - reserved_tokens

        # Use available tokens, with safety margin
        safe_token_budget = int(available_tokens * 0.9)

        # Temporarily set max tokens
        original_max = self.max_context_tokens
        self.max_context_tokens = safe_token_budget

        try:
            context = self.build_context(chunks)
            return context
        finally:
            # Restore original max
            self.max_context_tokens = original_max

    @staticmethod
    def format_sources(chunks: list[SemanticSearchResult]) -> str:
        """Format source list from chunks.

        Args:
            chunks: List of search results

        Returns:
            Formatted source list
        """
        if not chunks:
            return "No sources"

        # Get unique paths
        unique_sources = {}
        for chunk in chunks:
            path = chunk.path
            if path not in unique_sources:
                unique_sources[path] = {
                    "score": chunk.score,
                    "chunks": 0,
                }
            unique_sources[path]["chunks"] += 1

        # Format sources
        sources = []
        for i, (path, info) in enumerate(unique_sources.items(), start=1):
            score_str = f" (relevance: {info['score']:.2f})" if info["score"] is not None else ""
            chunk_str = f" [{info['chunks']} chunks]" if info["chunks"] > 1 else ""
            sources.append(f"{i}. {path}{score_str}{chunk_str}")

        return "\n".join(sources)
