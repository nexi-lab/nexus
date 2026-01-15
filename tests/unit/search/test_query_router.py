"""Tests for Query Router (Issue #1041).

Tests the intelligent query routing that automatically selects
optimal search strategies based on query complexity.
"""

from unittest.mock import MagicMock

import pytest

from nexus.search.query_router import (
    ROUTING_RULES,
    QueryRouter,
    RoutedQuery,
    RoutingConfig,
    create_query_router,
)


class TestRoutingConfig:
    """Test RoutingConfig validation."""

    def test_default_config(self):
        """Test default configuration values."""
        config = RoutingConfig()
        assert config.simple_max == 0.3
        assert config.moderate_max == 0.6
        assert config.complex_max == 0.8
        assert config.enabled is True

    def test_custom_config(self):
        """Test custom configuration values."""
        config = RoutingConfig(
            simple_max=0.25,
            moderate_max=0.5,
            complex_max=0.75,
            enabled=True,
        )
        assert config.simple_max == 0.25
        assert config.moderate_max == 0.5
        assert config.complex_max == 0.75

    def test_invalid_thresholds_order(self):
        """Test that invalid threshold order raises error."""
        with pytest.raises(ValueError, match="Invalid thresholds"):
            RoutingConfig(simple_max=0.5, moderate_max=0.3, complex_max=0.8)

    def test_invalid_thresholds_out_of_range(self):
        """Test that thresholds > 1.0 raise error."""
        with pytest.raises(ValueError, match="Invalid thresholds"):
            RoutingConfig(simple_max=0.3, moderate_max=0.6, complex_max=1.5)


class TestRoutedQuery:
    """Test RoutedQuery dataclass."""

    def test_to_dict(self):
        """Test RoutedQuery serialization."""
        routed = RoutedQuery(
            original_query="How does authentication work?",
            complexity_score=0.65,
            complexity_class="complex",
            search_mode="hybrid",
            graph_mode="dual",
            adjusted_limit=12,
            reasoning="Query classified as complex (score=0.65)",
            routing_latency_ms=1.5,
            include_community_summaries=False,
        )

        data = routed.to_dict()

        assert data["original_query"] == "How does authentication work?"
        assert data["complexity_score"] == 0.65
        assert data["complexity_class"] == "complex"
        assert data["search_mode"] == "hybrid"
        assert data["graph_mode"] == "dual"
        assert data["adjusted_limit"] == 12
        assert data["reasoning"] == "Query classified as complex (score=0.65)"
        assert data["routing_latency_ms"] == 1.5
        assert data["include_community_summaries"] is False


class TestQueryRouter:
    """Test QueryRouter functionality."""

    def test_simple_query_routing(self):
        """Test routing of simple queries."""
        router = QueryRouter()

        # Simple query: "what is X"
        routed = router.route("what is authentication")

        # Simple queries should use hybrid search with no graph
        assert routed.search_mode == "hybrid"
        assert routed.graph_mode == "none"
        assert routed.complexity_class == "simple"
        assert routed.complexity_score < 0.3

    def test_moderate_query_routing(self):
        """Test routing of moderate complexity queries."""
        router = QueryRouter()

        # Moderate query with comparison
        routed = router.route("compare OAuth vs JWT for API authentication")

        # Should have graph_mode="low" for entity expansion
        assert routed.search_mode == "hybrid"
        assert routed.complexity_class in ("moderate", "complex")
        assert routed.graph_mode in ("low", "dual")

    def test_complex_query_routing(self):
        """Test routing of complex multi-hop queries."""
        router = QueryRouter()

        # Complex query with multi-hop reasoning and temporal aspect
        routed = router.route(
            "How does the AuthService authenticate users and what happens "
            "when the JWTProvider expires the token before the refresh interval?"
        )

        # Should be complex or very_complex
        assert routed.complexity_class in ("complex", "very_complex")
        assert routed.graph_mode == "dual"

    def test_limit_adjustment_simple(self):
        """Test limit multiplier for simple queries."""
        router = QueryRouter()
        routed = router.route("what is X", base_limit=10)

        # Simple queries have 0.8 multiplier
        assert routed.adjusted_limit == 8

    def test_limit_adjustment_complex(self):
        """Test limit multiplier for complex queries."""
        router = QueryRouter()
        routed = router.route(
            "explain how authentication works and compare different methods "
            "including their security implications over time",
            base_limit=10,
        )

        # Complex queries have >= 1.0 multiplier
        assert routed.adjusted_limit >= 10

    def test_routing_latency_recorded(self):
        """Test that routing latency is recorded."""
        router = QueryRouter()
        routed = router.route("test query")

        assert routed.routing_latency_ms >= 0
        assert routed.routing_latency_ms < 100  # Should be very fast

    def test_routing_with_context_builder(self):
        """Test routing with a mock ContextBuilder."""
        mock_context_builder = MagicMock()
        mock_context_builder.estimate_query_complexity.return_value = 0.75

        router = QueryRouter(context_builder=mock_context_builder)
        routed = router.route("test query")

        # Should use the context builder's complexity
        assert routed.complexity_score == 0.75
        assert routed.complexity_class == "complex"
        mock_context_builder.estimate_query_complexity.assert_called_once_with("test query")

    def test_routing_without_context_builder(self):
        """Test fallback when no context builder is provided."""
        router = QueryRouter(context_builder=None)
        routed = router.route("How does authentication work?")

        # Should use fallback estimation
        assert 0.0 <= routed.complexity_score <= 1.0
        assert routed.complexity_class in ("simple", "moderate", "complex", "very_complex")

    def test_custom_thresholds(self):
        """Test routing with custom thresholds."""
        config = RoutingConfig(
            simple_max=0.2,
            moderate_max=0.4,
            complex_max=0.6,
        )

        # Mock a query with complexity 0.5 (would be "moderate" with defaults)
        mock_context_builder = MagicMock()
        mock_context_builder.estimate_query_complexity.return_value = 0.5

        router = QueryRouter(context_builder=mock_context_builder, config=config)
        routed = router.route("test")

        # With custom thresholds, 0.5 is "complex" (0.4 <= 0.5 < 0.6)
        assert routed.complexity_class == "complex"


class TestRoutingRules:
    """Test the routing rules configuration."""

    def test_simple_rules(self):
        """Test simple query routing rules."""
        rules = ROUTING_RULES["simple"]
        assert rules["search_mode"] == "hybrid"
        assert rules["graph_mode"] == "none"
        assert rules["limit_multiplier"] == 0.8

    def test_moderate_rules(self):
        """Test moderate query routing rules."""
        rules = ROUTING_RULES["moderate"]
        assert rules["search_mode"] == "hybrid"
        assert rules["graph_mode"] == "low"
        assert rules["limit_multiplier"] == 1.0

    def test_complex_rules(self):
        """Test complex query routing rules."""
        rules = ROUTING_RULES["complex"]
        assert rules["search_mode"] == "hybrid"
        assert rules["graph_mode"] == "dual"
        assert rules["limit_multiplier"] == 1.2

    def test_very_complex_rules(self):
        """Test very complex query routing rules."""
        rules = ROUTING_RULES["very_complex"]
        assert rules["search_mode"] == "hybrid"
        assert rules["graph_mode"] == "dual"
        assert rules["limit_multiplier"] == 1.5
        assert rules["include_community_summaries"] is True


class TestCreateQueryRouter:
    """Test the factory function."""

    def test_create_with_defaults(self):
        """Test creating router with defaults."""
        router = create_query_router()
        assert router.context_builder is None
        assert router.config.simple_max == 0.3

    def test_create_with_context_builder(self):
        """Test creating router with context builder."""
        mock_builder = MagicMock()
        router = create_query_router(context_builder=mock_builder)
        assert router.context_builder is mock_builder

    def test_create_with_custom_config(self):
        """Test creating router with custom config."""
        config = RoutingConfig(simple_max=0.25, moderate_max=0.5, complex_max=0.75)
        router = create_query_router(config=config)
        assert router.config.simple_max == 0.25
        assert router.config.moderate_max == 0.5
        assert router.config.complex_max == 0.75


class TestFallbackComplexityEstimation:
    """Test the fallback complexity estimation."""

    def test_word_count_factor(self):
        """Test word count contribution."""
        router = QueryRouter()

        short_query = router.route("test")
        long_query = router.route("this is a much longer query with many more words included")

        assert long_query.complexity_score > short_query.complexity_score

    def test_comparison_indicators(self):
        """Test comparison words boost complexity."""
        router = QueryRouter()

        normal = router.route("what is authentication")
        comparison = router.route("compare authentication methods")

        assert comparison.complexity_score > normal.complexity_score

    def test_temporal_indicators(self):
        """Test temporal words boost complexity."""
        router = QueryRouter()

        normal = router.route("what is the status")
        temporal = router.route("what was the status before the update")

        assert temporal.complexity_score > normal.complexity_score

    def test_multihop_patterns(self):
        """Test multi-hop patterns boost complexity."""
        router = QueryRouter()

        simple = router.route("what is X")
        multihop = router.route("how does X affect Y")

        assert multihop.complexity_score > simple.complexity_score

    def test_complex_question_patterns(self):
        """Test complex question patterns boost complexity."""
        router = QueryRouter()

        simple = router.route("what is authentication")
        complex_q = router.route("explain how authentication works")

        assert complex_q.complexity_score > simple.complexity_score


class TestPerformance:
    """Test performance requirements."""

    def test_routing_under_5ms(self):
        """Test that routing completes in under 5ms."""
        router = QueryRouter()

        # Run multiple iterations to ensure consistent performance
        for _ in range(10):
            routed = router.route(
                "How does the authentication system handle token refresh "
                "when multiple services are involved?"
            )
            # Routing should be fast (< 5ms as per requirement)
            assert routed.routing_latency_ms < 5.0

    def test_routing_with_mock_context_builder_under_5ms(self):
        """Test routing with mock context builder is fast."""
        mock_builder = MagicMock()
        mock_builder.estimate_query_complexity.return_value = 0.5

        router = QueryRouter(context_builder=mock_builder)

        for _ in range(10):
            routed = router.route("test query")
            assert routed.routing_latency_ms < 5.0
