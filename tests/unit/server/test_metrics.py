"""Tests for nexus.server.metrics — Prometheus middleware and endpoint (Issue #761)."""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nexus.server.metrics import (
    REQUEST_COUNT,
    REQUEST_DURATION,
    REQUESTS_IN_PROGRESS,
    PrometheusMiddleware,
    _status_group,
    metrics_endpoint,
    setup_prometheus,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Clear metric samples between tests to avoid cross-contamination."""
    # Reset counters/histograms by clearing their child metrics
    for collector in [REQUEST_COUNT, REQUEST_DURATION, REQUESTS_IN_PROGRESS]:
        collector._metrics.clear()
    yield


def _make_app() -> Starlette:
    """Build a minimal Starlette app with the Prometheus middleware."""

    async def _index(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def _items(request: Request) -> PlainTextResponse:
        return PlainTextResponse("items")

    async def _health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("healthy")

    async def _error(request: Request) -> PlainTextResponse:
        return PlainTextResponse("not found", status_code=404)

    app = Starlette(
        routes=[
            Route("/", _index),
            Route("/items/{item_id}", _items),
            Route("/health", _health),
            Route("/metrics", metrics_endpoint),
            Route("/error", _error),
        ],
    )
    app.add_middleware(PrometheusMiddleware)
    return app


# ---------------------------------------------------------------------------
# TestMetricsEndpoint
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    """Tests for the /metrics handler."""

    def test_returns_200(self) -> None:
        client = TestClient(_make_app())
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_correct_content_type(self) -> None:
        client = TestClient(_make_app())
        resp = client.get("/metrics")
        assert "text/plain" in resp.headers["content-type"]
        assert "version=0.0.4" in resp.headers["content-type"]

    def test_contains_expected_metric_families(self) -> None:
        client = TestClient(_make_app())
        # Make a request first so metrics are populated
        client.get("/")
        resp = client.get("/metrics")
        body = resp.text
        assert "http_request_duration_seconds" in body
        assert "http_requests_total" in body
        assert "http_requests_in_progress" in body

    def test_health_excluded_from_metrics(self) -> None:
        client = TestClient(_make_app())
        client.get("/health")
        resp = client.get("/metrics")
        body = resp.text
        # /health requests should not appear as endpoint labels
        assert 'endpoint="/health"' not in body

    def test_metrics_excluded_from_metrics(self) -> None:
        client = TestClient(_make_app())
        resp = client.get("/metrics")
        body = resp.text
        # /metrics itself should not appear as a tracked endpoint
        assert 'endpoint="/metrics"' not in body


# ---------------------------------------------------------------------------
# TestPrometheusMiddleware
# ---------------------------------------------------------------------------


class TestPrometheusMiddleware:
    """Tests for the PrometheusMiddleware ASGI middleware."""

    def test_increments_request_count(self) -> None:
        client = TestClient(_make_app())
        client.get("/")
        client.get("/")
        # Check the counter was incremented
        sample = REQUEST_COUNT.labels(method="GET", status="2xx", endpoint="/")
        assert sample._value.get() == 2  # type: ignore[union-attr]

    def test_records_duration(self) -> None:
        client = TestClient(_make_app())
        client.get("/")
        # Histogram should have at least one observation
        sample = REQUEST_DURATION.labels(method="GET", status="2xx", endpoint="/")
        # _sum is the sum of observed values
        assert sample._sum.get() > 0  # type: ignore[union-attr]

    def test_status_group_2xx(self) -> None:
        assert _status_group(200) == "2xx"
        assert _status_group(201) == "2xx"
        assert _status_group(204) == "2xx"

    def test_status_group_4xx(self) -> None:
        assert _status_group(400) == "4xx"
        assert _status_group(404) == "4xx"
        assert _status_group(422) == "4xx"

    def test_status_group_5xx(self) -> None:
        assert _status_group(500) == "5xx"
        assert _status_group(502) == "5xx"

    def test_404_recorded_as_4xx(self) -> None:
        client = TestClient(_make_app())
        client.get("/error")
        sample = REQUEST_COUNT.labels(method="GET", status="4xx", endpoint="/error")
        assert sample._value.get() == 1  # type: ignore[union-attr]

    def test_uses_route_template_not_actual_path(self) -> None:
        client = TestClient(_make_app())
        client.get("/items/42")
        client.get("/items/99")
        # Both should map to the route template, not the actual path
        sample = REQUEST_COUNT.labels(method="GET", status="2xx", endpoint="/items/{item_id}")
        assert sample._value.get() == 2  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TestSetupPrometheus
# ---------------------------------------------------------------------------


class TestSetupPrometheus:
    """Tests for setup_prometheus()."""

    def test_sets_version_info(self) -> None:
        setup_prometheus()
        # Verify the info metric family was registered
        metric_names = [m.name for m in REGISTRY.collect()]
        assert "nexus" in metric_names
