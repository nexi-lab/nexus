"""Context building for LLM document reading.

Builds optimal context from search results for LLM prompts.

Includes adaptive retrieval depth based on query complexity (Issue #1021),
inspired by SimpleMem (arXiv:2601.02553).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.search.semantic import SemanticSearchResult


@dataclass
class AdaptiveRetrievalConfig:
    """Configuration for adaptive retrieval depth.

    Based on SimpleMem formula: k_dyn = ⌊k_base · (1 + δ · C_q)⌋

    Attributes:
        k_base: Default retrieval count (default: 10)
        k_min: Minimum results to retrieve (default: 3)
        k_max: Maximum results to retrieve (default: 20)
        delta: Complexity scaling factor (default: 0.5)
        enabled: Whether adaptive retrieval is enabled (default: True)
    """

    k_base: int = 10
    k_min: int = 3
    k_max: int = 20
    delta: float = 0.5
    enabled: bool = True


class ContextBuilder:
    """Builds context from search results for LLM prompts.

    Supports adaptive retrieval depth that dynamically adjusts the number
    of results (k) based on query complexity, reducing token waste on
    simple queries while providing comprehensive context for complex ones.
    """

    # Keywords for query complexity estimation
    _COMPARISON_WORDS = frozenset(
        {
            "vs",
            "versus",
            "compare",
            "comparison",
            "difference",
            "differences",
            "between",
            "better",
            "worse",
            "advantages",
            "disadvantages",
            "pros",
            "cons",
        }
    )
    _TEMPORAL_WORDS = frozenset(
        {
            "when",
            "before",
            "after",
            "since",
            "during",
            "history",
            "timeline",
            "recent",
            "recently",
            "latest",
            "oldest",
            "first",
            "last",
            "evolution",
        }
    )
    _AGGREGATION_WORDS = frozenset(
        {
            "all",
            "every",
            "total",
            "summary",
            "overview",
            "list",
            "complete",
            "comprehensive",
            "entire",
            "full",
            "each",
            "various",
            "multiple",
        }
    )
    _MULTIHOP_PATTERNS = (
        "how does",
        "why does",
        "what causes",
        "relationship between",
        "impact of",
        "leads to",
        "results in",
        "connected to",
        "depends on",
        "affects",
        "influences",
        "related to",
        "interaction between",
    )
    _SIMPLE_QUESTION_PATTERNS = ("what is", "who is", "where is", "define")
    _COMPLEX_QUESTION_PATTERNS = (
        "how does",
        "why does",
        "explain",
        "analyze",
        "describe how",
        "what are the reasons",
        "elaborate",
        "discuss",
    )

    def __init__(
        self,
        max_context_tokens: int = 3000,
        adaptive_config: AdaptiveRetrievalConfig | None = None,
    ):
        """Initialize context builder.

        Args:
            max_context_tokens: Maximum number of tokens for context
            adaptive_config: Configuration for adaptive retrieval depth
        """
        self.max_context_tokens = max_context_tokens
        self.adaptive_config = adaptive_config or AdaptiveRetrievalConfig()

    def estimate_query_complexity(self, query: str) -> float:
        """Estimate query complexity score (0.0-1.0).

        Uses multiple heuristics to estimate how complex a query is:
        - Word count (longer queries tend to be more complex)
        - Comparison indicators (vs, compare, difference)
        - Temporal indicators (when, before, after, history)
        - Aggregation indicators (all, every, summary)
        - Multi-hop reasoning patterns (how does X affect Y)
        - Question type (simple "what is" vs complex "explain how")

        Args:
            query: The search query to analyze

        Returns:
            Complexity score between 0.0 (simple) and 1.0 (complex)
        """
        score = 0.0
        query_lower = query.lower()
        words = query_lower.split()
        word_set = set(words)

        # 1. Word count factor (normalized, max 0.25 contribution)
        word_count = len(words)
        score += min(word_count / 20.0, 0.25)

        # 2. Comparison indicators (+0.2)
        if word_set & self._COMPARISON_WORDS:
            score += 0.2

        # 3. Temporal indicators (+0.15)
        if word_set & self._TEMPORAL_WORDS:
            score += 0.15

        # 4. Aggregation indicators (+0.15)
        if word_set & self._AGGREGATION_WORDS:
            score += 0.15

        # 5. Multi-hop reasoning indicators (+0.2)
        if any(pattern in query_lower for pattern in self._MULTIHOP_PATTERNS):
            score += 0.2

        # 6. Question complexity adjustment
        if any(pattern in query_lower for pattern in self._COMPLEX_QUESTION_PATTERNS):
            score += 0.15
        elif any(pattern in query_lower for pattern in self._SIMPLE_QUESTION_PATTERNS):
            score -= 0.1

        # 7. Entity count heuristic (multiple quoted terms or capitalized words)
        # Count words that look like proper nouns (capitalized, not at start)
        proper_nouns = sum(
            1 for i, word in enumerate(query.split()) if i > 0 and word and word[0].isupper()
        )
        if proper_nouns >= 2:
            score += 0.1

        return max(0.0, min(1.0, score))  # Clamp to [0, 1]

    def calculate_k_dynamic(
        self,
        query: str,
        k_base: int | None = None,
        delta: float | None = None,
        k_min: int | None = None,
        k_max: int | None = None,
    ) -> int:
        """Calculate adaptive retrieval depth based on query complexity.

        Uses the SimpleMem formula: k_dyn = ⌊k_base · (1 + δ · C_q)⌋

        Args:
            query: The search query
            k_base: Base retrieval count (default: from config)
            delta: Complexity scaling factor (default: from config)
            k_min: Minimum results (default: from config)
            k_max: Maximum results (default: from config)

        Returns:
            Dynamic k value adjusted for query complexity
        """
        config = self.adaptive_config
        k_base = k_base if k_base is not None else config.k_base
        delta = delta if delta is not None else config.delta
        k_min = k_min if k_min is not None else config.k_min
        k_max = k_max if k_max is not None else config.k_max

        if not config.enabled:
            logger.debug(
                "[ADAPTIVE-K] Disabled, returning k_base=%d for query: %s",
                k_base,
                query[:50],
            )
            return k_base

        c_q = self.estimate_query_complexity(query)
        k_dyn = int(k_base * (1 + delta * c_q))
        k_final = max(k_min, min(k_max, k_dyn))

        logger.info(
            "[ADAPTIVE-K] query='%s' complexity=%.3f k_base=%d delta=%.2f "
            "k_raw=%d k_final=%d (min=%d, max=%d)",
            query[:80] if len(query) > 80 else query,
            c_q,
            k_base,
            delta,
            k_dyn,
            k_final,
            k_min,
            k_max,
        )

        return k_final

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

    def get_retrieval_params(
        self,
        query: str,
        k_base: int | None = None,
    ) -> dict[str, int | float]:
        """Get recommended retrieval parameters for a query.

        Returns a dictionary with the recommended k value and complexity score,
        useful for logging/debugging adaptive retrieval behavior.

        Args:
            query: The search query
            k_base: Base retrieval count (default: from config)

        Returns:
            Dictionary with 'k', 'complexity_score', and 'k_base' values
        """
        k_base = k_base if k_base is not None else self.adaptive_config.k_base
        complexity = self.estimate_query_complexity(query)
        k_dynamic = self.calculate_k_dynamic(query, k_base=k_base)

        return {
            "k": k_dynamic,
            "k_base": k_base,
            "complexity_score": complexity,
        }

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
