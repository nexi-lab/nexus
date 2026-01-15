"""Query Router for automatic search strategy selection (Issue #1041).

This module provides intelligent query routing that automatically selects
the optimal search strategy (vector-only, hybrid, graph-enhanced) based on
query complexity analysis.

References:
    - Issue #1041: Query router for automatic search strategy selection
    - Issue #1022: Query Complexity Estimator (dependency)
    - Issue #1040: Graph-Enhanced Retrieval (dependency)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.llm.context_builder import ContextBuilder

logger = logging.getLogger(__name__)

# Routing rules based on query complexity
ROUTING_RULES: dict[str, dict[str, Any]] = {
    # Simple queries (C_q < 0.3): Vector + BM25 hybrid
    "simple": {
        "search_mode": "hybrid",
        "graph_mode": "none",
        "limit_multiplier": 0.8,  # Fewer results needed
    },
    # Moderate queries (0.3 <= C_q < 0.6): Add entity graph
    "moderate": {
        "search_mode": "hybrid",
        "graph_mode": "low",  # Entity expansion only
        "limit_multiplier": 1.0,
    },
    # Complex queries (0.6 <= C_q < 0.8): Full dual-level
    "complex": {
        "search_mode": "hybrid",
        "graph_mode": "dual",  # Low + high level
        "limit_multiplier": 1.2,
    },
    # Very complex queries (C_q >= 0.8): Maximum retrieval
    "very_complex": {
        "search_mode": "hybrid",
        "graph_mode": "dual",
        "limit_multiplier": 1.5,
        "include_community_summaries": True,
    },
}

# Complexity thresholds (configurable)
DEFAULT_THRESHOLDS = {
    "simple_max": 0.3,
    "moderate_max": 0.6,
    "complex_max": 0.8,
}


@dataclass
class RoutingConfig:
    """Configuration for query routing thresholds and behavior."""

    simple_max: float = 0.3
    moderate_max: float = 0.6
    complex_max: float = 0.8
    enabled: bool = True

    def __post_init__(self) -> None:
        """Validate thresholds."""
        if not (0 < self.simple_max < self.moderate_max < self.complex_max <= 1.0):
            raise ValueError(
                f"Invalid thresholds: simple_max={self.simple_max}, "
                f"moderate_max={self.moderate_max}, complex_max={self.complex_max}. "
                "Must satisfy: 0 < simple_max < moderate_max < complex_max <= 1.0"
            )


@dataclass
class RoutedQuery:
    """Result of query routing with strategy decisions."""

    original_query: str
    complexity_score: float
    complexity_class: str  # simple, moderate, complex, very_complex
    search_mode: str
    graph_mode: str
    adjusted_limit: int
    reasoning: str  # Explanation for debugging
    routing_latency_ms: float = 0.0
    include_community_summaries: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "original_query": self.original_query,
            "complexity_score": self.complexity_score,
            "complexity_class": self.complexity_class,
            "search_mode": self.search_mode,
            "graph_mode": self.graph_mode,
            "adjusted_limit": self.adjusted_limit,
            "reasoning": self.reasoning,
            "routing_latency_ms": self.routing_latency_ms,
            "include_community_summaries": self.include_community_summaries,
        }


@dataclass
class QueryRouter:
    """Routes queries to optimal search strategies based on complexity.

    Uses query complexity estimation from ContextBuilder to automatically
    select the best search mode and graph mode for each query.

    Example:
        >>> from nexus.llm import ContextBuilder
        >>> from nexus.search import QueryRouter
        >>> context_builder = ContextBuilder()
        >>> router = QueryRouter(context_builder)
        >>> routed = router.route("How does authentication work?")
        >>> print(routed.search_mode, routed.graph_mode)
        hybrid low
    """

    context_builder: ContextBuilder | None = None
    config: RoutingConfig = field(default_factory=RoutingConfig)

    def route(self, query: str, base_limit: int = 10) -> RoutedQuery:
        """Analyze query and determine optimal search strategy.

        Args:
            query: The search query to route
            base_limit: Base number of results to retrieve

        Returns:
            RoutedQuery with optimal strategy parameters
        """
        start_time = time.perf_counter()

        # Estimate complexity
        if self.context_builder is not None:
            complexity = self.context_builder.estimate_query_complexity(query)
        else:
            # Fallback to simple heuristic if no context builder
            complexity = self._estimate_complexity_fallback(query)

        # Classify and get strategy
        complexity_class = self._classify_complexity(complexity)
        strategy = ROUTING_RULES[complexity_class]

        # Calculate adjusted limit
        adjusted_limit = max(1, int(base_limit * strategy["limit_multiplier"]))

        # Calculate routing latency
        routing_latency_ms = (time.perf_counter() - start_time) * 1000

        routed = RoutedQuery(
            original_query=query,
            complexity_score=complexity,
            complexity_class=complexity_class,
            search_mode=strategy["search_mode"],
            graph_mode=strategy["graph_mode"],
            adjusted_limit=adjusted_limit,
            reasoning=f"Query classified as {complexity_class} (score={complexity:.2f})",
            routing_latency_ms=routing_latency_ms,
            include_community_summaries=strategy.get("include_community_summaries", False),
        )

        logger.debug(
            f"[QUERY-ROUTER] {routed.reasoning}, "
            f"search_mode={routed.search_mode}, graph_mode={routed.graph_mode}, "
            f"limit={routed.adjusted_limit} (latency={routing_latency_ms:.2f}ms)"
        )

        return routed

    def _classify_complexity(self, complexity: float) -> str:
        """Classify complexity score into a category."""
        if complexity < self.config.simple_max:
            return "simple"
        elif complexity < self.config.moderate_max:
            return "moderate"
        elif complexity < self.config.complex_max:
            return "complex"
        else:
            return "very_complex"

    def _estimate_complexity_fallback(self, query: str) -> float:
        """Simple fallback complexity estimation when no context builder available.

        Uses basic heuristics similar to ContextBuilder but simplified.
        """
        score = 0.0
        query_lower = query.lower()
        words = query_lower.split()
        word_set = set(words)

        # Word count factor (normalized, max 0.25)
        score += min(len(words) / 20.0, 0.25)

        # Comparison indicators (+0.2)
        comparison_words = {"vs", "versus", "compare", "comparison", "difference", "between"}
        if word_set & comparison_words:
            score += 0.2

        # Temporal indicators (+0.15)
        temporal_words = {"when", "before", "after", "history", "timeline", "since", "until"}
        if word_set & temporal_words:
            score += 0.15

        # Aggregation indicators (+0.15)
        aggregation_words = {"all", "every", "summary", "overview", "list", "total"}
        if word_set & aggregation_words:
            score += 0.15

        # Multi-hop patterns (+0.2)
        multihop_patterns = [
            "how does",
            "how do",
            "why does",
            "why do",
            "what happens when",
            "relationship between",
            "impact of",
            "effect of",
        ]
        if any(pattern in query_lower for pattern in multihop_patterns):
            score += 0.2

        # Complex question patterns (+0.15)
        complex_patterns = ["explain", "analyze", "evaluate", "describe how"]
        if any(pattern in query_lower for pattern in complex_patterns):
            score += 0.15

        return min(score, 1.0)


def create_query_router(
    context_builder: ContextBuilder | None = None,
    config: RoutingConfig | None = None,
) -> QueryRouter:
    """Factory function to create a QueryRouter.

    Args:
        context_builder: Optional ContextBuilder for complexity estimation
        config: Optional routing configuration

    Returns:
        Configured QueryRouter instance
    """
    return QueryRouter(
        context_builder=context_builder,
        config=config or RoutingConfig(),
    )
