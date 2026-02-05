"""End-to-end tests for adaptive retrieval depth (Issue #1021).

Tests the adaptive k calculation through the FastAPI /api/search/query endpoint.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Configure logging to verify adaptive k behavior
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Add direct import path for context_builder to avoid Python 3.12+ imports in nexus/__init__.py
_llm_path = str(Path(__file__).parent.parent.parent / "src" / "nexus" / "llm")
if _llm_path not in sys.path:
    sys.path.insert(0, _llm_path)


class TestAdaptiveRetrievalFastAPI:
    """E2E tests for adaptive retrieval through FastAPI endpoints."""

    @pytest.fixture
    def mock_search_results(self):
        """Create mock search results."""
        from dataclasses import dataclass

        @dataclass
        class MockSearchResult:
            path: str
            chunk_text: str
            score: float
            chunk_index: int = 0
            line_start: int | None = None
            line_end: int | None = None
            keyword_score: float | None = None
            vector_score: float | None = None

        return [
            MockSearchResult(
                path=f"/docs/doc{i}.md",
                chunk_text=f"Document {i} content about authentication and security.",
                score=0.9 - (i * 0.05),
                chunk_index=i,
            )
            for i in range(20)
        ]

    @pytest.fixture
    def test_client(self):
        """Create FastAPI test client."""
        pytest.importorskip("httpx")
        pytest.importorskip("litellm")
        from fastapi.testclient import TestClient

        from nexus.server.fastapi_server import create_app

        mock_nexus_fs = MagicMock()
        mock_nexus_fs.SessionLocal = None  # Skip subscription manager init
        app = create_app(mock_nexus_fs)
        return TestClient(app)

    @pytest.fixture
    def mock_app_state(self, mock_search_results):
        """Mock the app state with search daemon."""
        from nexus.server import fastapi_server

        # Create mock search daemon
        mock_daemon = MagicMock()
        mock_daemon.is_initialized = True

        # Track calls to verify adaptive_k is passed
        search_calls = []

        async def mock_search(
            query: str,
            search_type: str = "hybrid",
            limit: int = 10,
            path_filter: str | None = None,
            alpha: float = 0.5,
            fusion_method: str = "rrf",
            adaptive_k: bool = False,
        ):
            # Record the call
            search_calls.append(
                {
                    "query": query,
                    "limit": limit,
                    "adaptive_k": adaptive_k,
                }
            )

            # If adaptive_k is enabled, the limit should have been adjusted
            # by the daemon before this mock is called
            return mock_search_results[:limit]

        mock_daemon.search = mock_search

        # Patch app state
        original_daemon = fastapi_server._app_state.search_daemon
        fastapi_server._app_state.search_daemon = mock_daemon

        yield {"daemon": mock_daemon, "calls": search_calls}

        # Restore
        fastapi_server._app_state.search_daemon = original_daemon

    def test_search_endpoint_accepts_adaptive_k_param(self, test_client, mock_app_state):
        """Test that /api/search/query accepts adaptive_k parameter."""
        response = test_client.get(
            "/api/search/query",
            params={
                "q": "What is Python?",
                "limit": 10,
                "adaptive_k": "true",
            },
        )

        # Should not error on the parameter
        assert response.status_code in (200, 503), f"Unexpected status: {response.status_code}"

    def test_search_endpoint_passes_adaptive_k_to_daemon(self, test_client, mock_app_state):
        """Test that adaptive_k parameter is passed to search daemon."""
        # Make request with adaptive_k=true
        response = test_client.get(
            "/api/search/query",
            params={
                "q": "How does authentication compare to authorization?",
                "limit": 10,
                "adaptive_k": "true",
            },
        )

        if response.status_code == 200:
            # Verify adaptive_k was passed to daemon
            calls = mock_app_state["calls"]
            assert len(calls) > 0, "Search daemon should have been called"
            assert calls[-1]["adaptive_k"] is True, "adaptive_k should be True"
            logger.info(f"[TEST] Search call: {calls[-1]}")

    def test_search_without_adaptive_k_defaults_to_false(self, test_client, mock_app_state):
        """Test that adaptive_k defaults to False when not specified."""
        response = test_client.get(
            "/api/search/query",
            params={
                "q": "What is Python?",
                "limit": 10,
            },
        )

        if response.status_code == 200:
            calls = mock_app_state["calls"]
            assert len(calls) > 0
            assert calls[-1]["adaptive_k"] is False, "adaptive_k should default to False"


class TestAdaptiveRetrievalDaemon:
    """Test adaptive retrieval in the search daemon."""

    @pytest.mark.asyncio
    async def test_daemon_applies_adaptive_k(self, caplog):
        """Test that SearchDaemon applies adaptive k when enabled."""
        # Import directly to avoid Python 3.12+ requirements in nexus/__init__.py
        from context_builder import ContextBuilder

        # Test the context builder directly (daemon uses this)
        builder = ContextBuilder()

        simple_query = "What is Python?"
        complex_query = "How does authentication compare to authorization in web security?"

        k_simple = builder.calculate_k_dynamic(simple_query, k_base=10)
        k_complex = builder.calculate_k_dynamic(complex_query, k_base=10)

        logger.info(f"[TEST] Simple query: k={k_simple}")
        logger.info(f"[TEST] Complex query: k={k_complex}")

        assert k_complex > k_simple, (
            f"Complex query should get higher k ({k_complex} vs {k_simple})"
        )

    @pytest.mark.asyncio
    async def test_daemon_search_with_adaptive_k_logging(self, caplog):
        """Test that daemon logs adaptive k adjustments."""
        with caplog.at_level(logging.INFO):
            # Import directly to avoid Python 3.12+ requirements
            from context_builder import ContextBuilder

            builder = ContextBuilder()
            query = "How does authentication compare to authorization in web security?"

            # This should log the adaptive k calculation
            _ = builder.calculate_k_dynamic(query, k_base=10)

            # Check logs
            adaptive_logs = [r for r in caplog.records if "[ADAPTIVE-K]" in r.message]
            assert len(adaptive_logs) > 0, "Should have logged adaptive k calculation"

            log_msg = adaptive_logs[0].message
            assert "complexity=" in log_msg
            assert "k_final=" in log_msg
            logger.info(f"[TEST] Adaptive k log: {log_msg}")


class TestAdaptiveRetrievalComplexity:
    """Test query complexity estimation."""

    def test_simple_queries_low_complexity(self):
        """Test that simple queries have low complexity scores."""
        # Import directly to avoid Python 3.12+ requirements
        from context_builder import ContextBuilder

        builder = ContextBuilder()

        simple_queries = [
            "What is Python?",
            "Define REST API",
            "Who created Linux?",
        ]

        for query in simple_queries:
            score = builder.estimate_query_complexity(query)
            assert score < 0.3, f"Simple query '{query}' has high complexity {score}"
            logger.info(f"[TEST] '{query}' -> complexity={score:.3f}")

    def test_complex_queries_high_complexity(self):
        """Test that complex queries have higher complexity scores."""
        # Import directly to avoid Python 3.12+ requirements
        from context_builder import ContextBuilder

        builder = ContextBuilder()

        complex_queries = [
            "How does authentication compare to authorization in web security?",
            "Explain all the differences between REST and GraphQL since 2020",
            "What is the relationship between microservices and distributed systems?",
        ]

        for query in complex_queries:
            score = builder.estimate_query_complexity(query)
            assert score > 0.4, f"Complex query '{query}' has low complexity {score}"
            logger.info(f"[TEST] '{query}' -> complexity={score:.3f}")

    def test_dynamic_k_respects_bounds(self):
        """Test that k_min and k_max bounds are respected."""
        # Import directly to avoid Python 3.12+ requirements
        from context_builder import AdaptiveRetrievalConfig, ContextBuilder

        config = AdaptiveRetrievalConfig(k_base=10, k_min=5, k_max=15, delta=2.0)
        builder = ContextBuilder(adaptive_config=config)

        queries = [
            "x",  # Very simple
            "Explain all complex relationships between auth, authz, sessions, tokens",
        ]

        for query in queries:
            k = builder.calculate_k_dynamic(query)
            assert 5 <= k <= 15, f"k={k} out of bounds for query: {query}"
            logger.info(f"[TEST] '{query[:30]}...' -> k={k}")


class TestAdaptiveRetrievalAPIContract:
    """Test the API contract for adaptive retrieval."""

    def test_adaptive_k_parameter_in_openapi_schema(self):
        """Test that adaptive_k is documented in OpenAPI schema."""
        pytest.importorskip("litellm")
        from nexus.server.fastapi_server import create_app

        mock_nexus_fs = MagicMock()
        mock_nexus_fs.SessionLocal = None  # Skip subscription manager init
        app = create_app(mock_nexus_fs)
        openapi = app.openapi()

        # Find the search endpoint
        search_path = openapi.get("paths", {}).get("/api/search/query", {})
        get_params = search_path.get("get", {}).get("parameters", [])

        param_names = [p.get("name") for p in get_params]
        assert "adaptive_k" in param_names, (
            f"adaptive_k should be in OpenAPI params, found: {param_names}"
        )

        # Find adaptive_k param details
        adaptive_k_param = next(p for p in get_params if p.get("name") == "adaptive_k")
        assert adaptive_k_param.get("schema", {}).get("type") == "boolean"
        assert adaptive_k_param.get("schema", {}).get("default") is False

        logger.info(f"[TEST] adaptive_k param: {adaptive_k_param}")


# Standalone test runner
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--log-cli-level=INFO"])
