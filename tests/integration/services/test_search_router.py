"""Regression tests for the search API router (Issue #3147).

Tests parameter validation, response structure, and error handling
for the search endpoint, including the federated=true parameter.
"""

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

# =============================================================================
# search_query endpoint validation (via FastAPI TestClient)
# =============================================================================

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _HAS_FASTAPI_TESTCLIENT = True
except ImportError:
    _HAS_FASTAPI_TESTCLIENT = False


@dataclass
class _MockResult:
    path: str = "test.txt"
    chunk_text: str = "hello"
    score: float = 0.95
    chunk_index: int = 0
    line_start: int | None = None
    line_end: int | None = None
    keyword_score: float | None = None
    vector_score: float | None = None
    splade_score: float | None = None
    reranker_score: float | None = None


@pytest.mark.skipif(not _HAS_FASTAPI_TESTCLIENT, reason="fastapi test client not available")
class TestSearchQueryEndpoint:
    @pytest.fixture
    def app(self) -> "FastAPI":
        from nexus.server.api.v2.routers.search import router

        app = FastAPI()
        app.include_router(router)

        # Mock dependencies
        mock_daemon = MagicMock()
        mock_daemon.is_initialized = True
        mock_daemon.get_health.return_value = {"status": "ok"}
        mock_daemon.get_stats.return_value = {"queries": 0}
        mock_daemon.last_search_timing = {
            "backend_ms": 12.34,
            "rerank_ms": 0.0,
        }

        async def mock_search(**kwargs: Any) -> list[_MockResult]:
            return [
                _MockResult(path="result.txt", chunk_text="found", score=0.9),
            ]

        mock_daemon.search = mock_search
        app.state.search_daemon = mock_daemon
        app.state.search_daemon_enabled = True
        app.state.record_store = MagicMock()
        app.state.async_session_factory = MagicMock()
        app.state.async_read_session_factory = MagicMock()

        # Override auth dependency
        from nexus.server.dependencies import require_auth

        app.dependency_overrides[require_auth] = lambda: {
            "authenticated": True,
            "user_id": "test_user",
            "zone_id": "root",
        }

        return app

    @pytest.fixture
    def client(self, app: "FastAPI") -> "TestClient":
        return TestClient(app)

    def test_valid_query(self, client: "TestClient") -> None:
        resp = client.get("/api/v2/search/query?q=hello")
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "hello"
        assert data["search_type"] == "hybrid"
        assert "results" in data
        assert "total" in data

    def test_invalid_search_type(self, client: "TestClient") -> None:
        resp = client.get("/api/v2/search/query?q=hello&type=invalid")
        assert resp.status_code == 400

    def test_invalid_fusion_method(self, client: "TestClient") -> None:
        resp = client.get("/api/v2/search/query?q=hello&fusion=invalid")
        assert resp.status_code == 400

    def test_invalid_graph_mode(self, client: "TestClient") -> None:
        resp = client.get("/api/v2/search/query?q=hello&graph_mode=invalid")
        assert resp.status_code == 400

    def test_empty_query_rejected(self, client: "TestClient") -> None:
        resp = client.get("/api/v2/search/query?q=")
        assert resp.status_code == 422

    def test_limit_bounds(self, client: "TestClient") -> None:
        resp = client.get("/api/v2/search/query?q=hello&limit=0")
        assert resp.status_code == 422
        resp = client.get("/api/v2/search/query?q=hello&limit=101")
        assert resp.status_code == 422

    def test_health_endpoint(self, client: "TestClient") -> None:
        resp = client.get("/api/v2/search/health")
        assert resp.status_code == 200

    def test_result_structure(self, client: "TestClient") -> None:
        resp = client.get("/api/v2/search/query?q=hello")
        data = resp.json()
        result = data["results"][0]
        assert "path" in result
        assert "chunk_text" in result
        assert "score" in result

    def test_latency_breakdown_includes_backend_leg_timings(self, client: "TestClient") -> None:
        app: Any = client.app
        app.state.search_daemon.last_search_timing = {
            "backend_ms": 42.567,
            "embed_ms": 3.214,
            "keyword_ms": 11.111,
            "page_keyword_ms": 7.777,
            "vector_ms": 19.999,
            "fusion_ms": 1.005,
            "rerank_ms": 0.0,
        }

        resp = client.get("/api/v2/search/query?q=hello")

        assert resp.status_code == 200
        breakdown = resp.json()["latency_breakdown"]
        assert breakdown["backend_ms"] == 42.57
        assert breakdown["embed_ms"] == 3.21
        assert breakdown["keyword_ms"] == 11.11
        assert breakdown["page_keyword_ms"] == 7.78
        assert breakdown["vector_ms"] == 20.0
        assert breakdown["fusion_ms"] == 1.0
        assert breakdown["rerank_ms"] == 0.0

    def test_latency_breakdown_prefers_result_timing_snapshot(self, client: "TestClient") -> None:
        class _TimedResults(list[_MockResult]):
            search_timing: dict[str, float]

        timed_results = _TimedResults(
            [_MockResult(path="snapshot.txt", chunk_text="snapshot", score=0.8)]
        )
        timed_results.search_timing = {
            "backend_ms": 12.345,
            "keyword_ms": 6.789,
            "rerank_ms": 0.0,
        }

        app: Any = client.app
        app.state.search_daemon.last_search_timing = {
            "backend_ms": 999.0,
            "keyword_ms": 999.0,
            "rerank_ms": 999.0,
        }

        async def mock_search(**kwargs: Any) -> _TimedResults:
            return timed_results

        app.state.search_daemon.search = mock_search

        resp = client.get("/api/v2/search/query?q=hello")

        assert resp.status_code == 200
        breakdown = resp.json()["latency_breakdown"]
        assert breakdown["backend_ms"] == 12.35
        assert breakdown["keyword_ms"] == 6.79
        assert breakdown["rerank_ms"] == 0.0
