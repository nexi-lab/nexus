"""E2E tests for RLM inference endpoint (Issue #1306).

Tests the full HTTP request/response cycle through FastAPI's TestClient,
validating:
- POST /api/v2/rlm/infer returns 503 when RLM service is unavailable
- POST /api/v2/rlm/infer returns SSE stream when service is available
- POST /api/v2/rlm/infer returns JSON when stream=false
- Request validation (Pydantic models)
- X-API-Version header is present on v2 responses
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.rlm.types import RLMInferenceResult, RLMStatus, SSEEvent, SSEEventType
from nexus.server.api.v2.routers.rlm import router
from nexus.server.api.v2.versioning import VersionHeaderMiddleware

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_no_rlm() -> FastAPI:
    """FastAPI app with RLM router but no RLM service (should 503)."""
    app = FastAPI()
    app.state.rlm_service = None
    app.include_router(router)
    app.add_middleware(VersionHeaderMiddleware)
    return app


@pytest.fixture
def app_with_rlm() -> FastAPI:
    """FastAPI app with mocked RLM service."""
    app = FastAPI()

    # Create mock RLM service
    mock_service = MagicMock()

    # Non-streaming: return completed result
    mock_service.infer = AsyncMock(
        return_value=RLMInferenceResult(
            status=RLMStatus.COMPLETED,
            answer="The answer is 42",
            iterations=(),
            total_tokens=500,
            total_duration_seconds=2.5,
        )
    )

    # Streaming: return async iterator of SSE events
    async def mock_stream(*args, **kwargs):
        yield SSEEvent(
            event=SSEEventType.STARTED,
            data={"query": "test", "max_iterations": 15, "model": "test-model"},
        )
        yield SSEEvent(
            event=SSEEventType.FINAL_ANSWER,
            data={
                "answer": "42",
                "total_tokens": 500,
                "total_duration_seconds": 2.5,
                "iterations": 1,
            },
        )

    mock_service.infer_stream = mock_stream

    app.state.rlm_service = mock_service
    app.include_router(router)
    app.add_middleware(VersionHeaderMiddleware)
    return app


# ---------------------------------------------------------------------------
# Tests: Service unavailable
# ---------------------------------------------------------------------------


class TestRLMServiceUnavailable:
    """When rlm_service is None, the endpoint returns 503."""

    def test_infer_returns_503(self, app_no_rlm: FastAPI) -> None:
        """POST /api/v2/rlm/infer returns 503 when service is None."""
        client = TestClient(app_no_rlm)
        response = client.post(
            "/api/v2/rlm/infer",
            json={"query": "What is the meaning of life?"},
        )
        assert response.status_code == 503
        assert "not available" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Tests: Non-streaming
# ---------------------------------------------------------------------------


class TestRLMNonStreaming:
    """Non-streaming inference returns JSON response."""

    def test_non_streaming_response(self, app_with_rlm: FastAPI) -> None:
        """POST with stream=false returns JSON result."""
        client = TestClient(app_with_rlm)
        response = client.post(
            "/api/v2/rlm/infer",
            json={"query": "What is 6 times 7?", "stream": False},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["answer"] == "The answer is 42"
        assert data["total_tokens"] == 500

    def test_version_header_present(self, app_with_rlm: FastAPI) -> None:
        """X-API-Version header is present in response."""
        client = TestClient(app_with_rlm)
        response = client.post(
            "/api/v2/rlm/infer",
            json={"query": "test", "stream": False},
        )
        assert response.headers.get("x-api-version") == "2.0"


# ---------------------------------------------------------------------------
# Tests: Streaming
# ---------------------------------------------------------------------------


class TestRLMStreaming:
    """Streaming inference returns SSE events."""

    def test_streaming_response_content_type(self, app_with_rlm: FastAPI) -> None:
        """POST with stream=true returns text/event-stream."""
        client = TestClient(app_with_rlm)
        response = client.post(
            "/api/v2/rlm/infer",
            json={"query": "What is 6 times 7?", "stream": True},
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")

    def test_streaming_contains_events(self, app_with_rlm: FastAPI) -> None:
        """SSE stream contains started and final_answer events."""
        client = TestClient(app_with_rlm)
        response = client.post(
            "/api/v2/rlm/infer",
            json={"query": "test", "stream": True},
        )
        body = response.text
        assert "event: rlm.started" in body
        assert "event: rlm.final_answer" in body


# ---------------------------------------------------------------------------
# Tests: Request validation
# ---------------------------------------------------------------------------


class TestRLMRequestValidation:
    """Pydantic validation for inference requests."""

    def test_missing_query_returns_422(self, app_with_rlm: FastAPI) -> None:
        """Request without query field returns 422."""
        client = TestClient(app_with_rlm)
        response = client.post("/api/v2/rlm/infer", json={})
        assert response.status_code == 422

    def test_invalid_max_iterations_returns_422(self, app_with_rlm: FastAPI) -> None:
        """max_iterations=0 fails validation."""
        client = TestClient(app_with_rlm)
        response = client.post(
            "/api/v2/rlm/infer",
            json={"query": "test", "max_iterations": 0},
        )
        assert response.status_code == 422

    def test_invalid_max_iterations_too_high_returns_422(self, app_with_rlm: FastAPI) -> None:
        """max_iterations=100 fails validation (max 50)."""
        client = TestClient(app_with_rlm)
        response = client.post(
            "/api/v2/rlm/infer",
            json={"query": "test", "max_iterations": 100},
        )
        assert response.status_code == 422

    def test_valid_request_with_all_fields(self, app_with_rlm: FastAPI) -> None:
        """Full request with all optional fields succeeds."""
        client = TestClient(app_with_rlm)
        response = client.post(
            "/api/v2/rlm/infer",
            json={
                "query": "Analyze the data",
                "context_paths": ["/zone/default/data.csv"],
                "zone_id": "analytics",
                "model": "claude-sonnet-4-20250514",
                "max_iterations": 10,
                "max_duration_seconds": 60,
                "max_total_tokens": 50000,
                "stream": False,
            },
        )
        assert response.status_code == 200
