"""Comprehensive unit tests for QueryRouter (Issue #1041, #1499).

Tests cover:
- RoutingConfig validation
- _classify_complexity boundary values
- _estimate_complexity_fallback heuristics
- route() end-to-end
- RoutedQuery serialization
"""

import pytest

from nexus.bricks.search.query_router import QueryRouter, RoutedQuery, RoutingConfig

# =============================================================================
# RoutingConfig validation
# =============================================================================


class TestRoutingConfig:
    """Test RoutingConfig threshold validation."""

    def test_default_config_valid(self) -> None:
        config = RoutingConfig()
        assert config.simple_max == 0.3
        assert config.moderate_max == 0.6
        assert config.complex_max == 0.8
        assert config.enabled is True

    def test_custom_valid_thresholds(self) -> None:
        config = RoutingConfig(simple_max=0.2, moderate_max=0.5, complex_max=0.9)
        assert config.simple_max == 0.2

    def test_invalid_simple_max_zero(self) -> None:
        with pytest.raises(ValueError, match="Invalid thresholds"):
            RoutingConfig(simple_max=0.0, moderate_max=0.6, complex_max=0.8)

    def test_invalid_simple_gte_moderate(self) -> None:
        with pytest.raises(ValueError, match="Invalid thresholds"):
            RoutingConfig(simple_max=0.6, moderate_max=0.6, complex_max=0.8)

    def test_invalid_moderate_gte_complex(self) -> None:
        with pytest.raises(ValueError, match="Invalid thresholds"):
            RoutingConfig(simple_max=0.3, moderate_max=0.8, complex_max=0.8)

    def test_invalid_complex_exceeds_one(self) -> None:
        with pytest.raises(ValueError, match="Invalid thresholds"):
            RoutingConfig(simple_max=0.3, moderate_max=0.6, complex_max=1.1)

    def test_invalid_reversed_order(self) -> None:
        with pytest.raises(ValueError, match="Invalid thresholds"):
            RoutingConfig(simple_max=0.8, moderate_max=0.6, complex_max=0.3)

    def test_negative_threshold(self) -> None:
        with pytest.raises(ValueError, match="Invalid thresholds"):
            RoutingConfig(simple_max=-0.1, moderate_max=0.6, complex_max=0.8)


# =============================================================================
# _classify_complexity boundary values
# =============================================================================


class TestClassifyComplexity:
    """Test _classify_complexity at boundary values."""

    def setup_method(self) -> None:
        self.router = QueryRouter()

    def test_below_simple_max(self) -> None:
        assert self.router._classify_complexity(0.29) == "simple"

    def test_at_simple_max_boundary(self) -> None:
        """At exactly 0.3, should be 'moderate' (not < 0.3)."""
        assert self.router._classify_complexity(0.30) == "moderate"

    def test_below_moderate_max(self) -> None:
        assert self.router._classify_complexity(0.59) == "moderate"

    def test_at_moderate_max_boundary(self) -> None:
        """At exactly 0.6, should be 'complex' (not < 0.6)."""
        assert self.router._classify_complexity(0.60) == "complex"

    def test_below_complex_max(self) -> None:
        assert self.router._classify_complexity(0.79) == "complex"

    def test_at_complex_max_boundary(self) -> None:
        """At exactly 0.8, should be 'very_complex' (not < 0.8)."""
        assert self.router._classify_complexity(0.80) == "very_complex"

    def test_above_complex_max(self) -> None:
        assert self.router._classify_complexity(0.95) == "very_complex"

    def test_zero_is_simple(self) -> None:
        assert self.router._classify_complexity(0.0) == "simple"

    def test_one_is_very_complex(self) -> None:
        assert self.router._classify_complexity(1.0) == "very_complex"

    def test_custom_thresholds(self) -> None:
        config = RoutingConfig(simple_max=0.2, moderate_max=0.5, complex_max=0.9)
        router = QueryRouter(config=config)
        assert router._classify_complexity(0.19) == "simple"
        assert router._classify_complexity(0.20) == "moderate"
        assert router._classify_complexity(0.49) == "moderate"
        assert router._classify_complexity(0.50) == "complex"
        assert router._classify_complexity(0.89) == "complex"
        assert router._classify_complexity(0.90) == "very_complex"


# =============================================================================
# _estimate_complexity_fallback
# =============================================================================


class TestEstimateComplexityFallback:
    """Test heuristic complexity estimation."""

    def setup_method(self) -> None:
        self.router = QueryRouter()

    def test_simple_one_word_query(self) -> None:
        score = self.router._estimate_complexity_fallback("hello")
        # 1 word / 20 = 0.05
        assert 0.0 < score < 0.3

    def test_comparison_query(self) -> None:
        score = self.router._estimate_complexity_fallback("compare python vs java")
        # word_count(4/20=0.2) + comparison(0.2) = 0.4 (moderate)
        assert score >= 0.3

    def test_temporal_query(self) -> None:
        score = self.router._estimate_complexity_fallback("when was the function added")
        # word_count(6/20=0.3→capped 0.25) + temporal(0.15) = 0.4
        assert score >= 0.3

    def test_aggregation_query(self) -> None:
        score = self.router._estimate_complexity_fallback("list all exported functions")
        # word_count(4/20=0.2) + aggregation(0.15) = 0.35
        assert score >= 0.3

    def test_multihop_query(self) -> None:
        score = self.router._estimate_complexity_fallback("how does authentication work")
        # word_count(4/20=0.2) + multihop(0.2) = 0.4
        assert score >= 0.3

    def test_complex_pattern_query(self) -> None:
        score = self.router._estimate_complexity_fallback("explain the caching mechanism")
        # word_count(4/20=0.2) + complex_pattern(0.15) = 0.35
        assert score >= 0.3

    def test_very_complex_query(self) -> None:
        """Multiple indicators should stack up to high complexity."""
        score = self.router._estimate_complexity_fallback(
            "explain how does the compare function work before and after the refactoring overview"
        )
        # word_count(13/20=0.65→capped 0.25) + multihop(0.2) + comparison("compare": 0.2)
        # + temporal("before","after": 0.15) + aggregation("overview": 0.15) + complex("explain": 0.15)
        # = 0.25+0.2+0.2+0.15+0.15+0.15 = 1.1 → capped at 1.0
        assert score >= 0.8

    def test_score_capped_at_one(self) -> None:
        """Score should never exceed 1.0."""
        score = self.router._estimate_complexity_fallback(
            "explain how does compare analyze evaluate between all every summary before after overview"
        )
        assert score <= 1.0

    def test_empty_query(self) -> None:
        score = self.router._estimate_complexity_fallback("")
        assert score == 0.0

    def test_case_insensitive(self) -> None:
        """Patterns should match regardless of case."""
        score_lower = self.router._estimate_complexity_fallback("how does it work")
        score_upper = self.router._estimate_complexity_fallback("HOW DOES IT WORK")
        assert score_lower == score_upper


# =============================================================================
# route() end-to-end
# =============================================================================


class TestRoute:
    """Test route() end-to-end behavior."""

    def setup_method(self) -> None:
        self.router = QueryRouter()

    def test_simple_query_routing(self) -> None:
        routed = self.router.route("hello")
        assert routed.complexity_class == "simple"
        assert routed.search_mode == "hybrid"
        assert routed.graph_mode == "none"
        assert routed.include_community_summaries is False

    def test_moderate_query_routing(self) -> None:
        routed = self.router.route("compare python vs java")
        assert routed.complexity_class in ("moderate", "complex")
        assert routed.graph_mode in ("low", "dual")

    def test_adjusted_limit_simple(self) -> None:
        routed = self.router.route("hello", base_limit=10)
        # simple: 0.8x multiplier → 8
        assert routed.adjusted_limit == 8

    def test_adjusted_limit_minimum_one(self) -> None:
        routed = self.router.route("hello", base_limit=1)
        # 1 * 0.8 = 0.8 → max(1, int(0.8)) = max(1, 0) = 1
        assert routed.adjusted_limit >= 1

    def test_routing_latency_measured(self) -> None:
        routed = self.router.route("test query")
        assert routed.routing_latency_ms >= 0.0

    def test_reasoning_present(self) -> None:
        routed = self.router.route("test query")
        assert "classified as" in routed.reasoning
        assert routed.complexity_class in routed.reasoning

    def test_original_query_preserved(self) -> None:
        routed = self.router.route("exact query text")
        assert routed.original_query == "exact query text"

    def test_disabled_router_still_works(self) -> None:
        """Router with enabled=False still routes (enabled flag is for callers)."""
        config = RoutingConfig(enabled=False)
        router = QueryRouter(config=config)
        routed = router.route("test")
        assert isinstance(routed, RoutedQuery)

    def test_default_router(self) -> None:
        """Default router without args uses heuristic estimation."""
        router = QueryRouter()
        routed = router.route("hello")
        assert routed.complexity_class == "simple"


# =============================================================================
# RoutedQuery.to_dict()
# =============================================================================


class TestRoutedQueryToDict:
    """Test RoutedQuery serialization."""

    def test_to_dict_contains_all_fields(self) -> None:
        routed = RoutedQuery(
            original_query="test",
            complexity_score=0.5,
            complexity_class="moderate",
            search_mode="hybrid",
            graph_mode="low",
            adjusted_limit=10,
            reasoning="test reasoning",
            routing_latency_ms=1.23,
            include_community_summaries=False,
        )
        d = routed.to_dict()
        assert d["original_query"] == "test"
        assert d["complexity_score"] == 0.5
        assert d["complexity_class"] == "moderate"
        assert d["search_mode"] == "hybrid"
        assert d["graph_mode"] == "low"
        assert d["adjusted_limit"] == 10
        assert d["reasoning"] == "test reasoning"
        assert d["routing_latency_ms"] == 1.23
        assert d["include_community_summaries"] is False

    def test_to_dict_with_community_summaries(self) -> None:
        routed = RoutedQuery(
            original_query="complex query",
            complexity_score=0.9,
            complexity_class="very_complex",
            search_mode="hybrid",
            graph_mode="dual",
            adjusted_limit=15,
            reasoning="very complex",
            include_community_summaries=True,
        )
        d = routed.to_dict()
        assert d["include_community_summaries"] is True
