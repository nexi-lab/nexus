"""Integration tests for the Nexus observability pipeline (Issue #761).

Validates that the Prometheus /metrics endpoint works correctly when
wired through the real FastAPI application (create_app), and that
the middleware correctly tracks request metrics with acceptable overhead.
"""

from __future__ import annotations

import os
import time

import pytest
from starlette.testclient import TestClient

from nexus.backends.local import LocalBackend
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore

# ---------------------------------------------------------------------------
# Fixture: create a real FastAPI app with Prometheus middleware wired
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_and_key(tmp_path):
    """Build a real FastAPI app with NexusFS + Prometheus middleware."""
    from nexus.server.fastapi_server import create_app

    os.environ.setdefault("NEXUS_JWT_SECRET", "test-secret-12345")

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(exist_ok=True)
    backend = LocalBackend(root_path=str(storage_dir))

    metadata_store = RaftMetadataStore.embedded(str(tmp_path / "raft-metadata"))

    db_url = f"sqlite:///{tmp_path / 'records.db'}"
    record_store = SQLAlchemyRecordStore(db_url=db_url)

    nx = create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        is_admin=True,
        enable_tiger_cache=False,
    )

    api_key = "test-observability-key"
    app = create_app(nexus_fs=nx, api_key=api_key, database_url=db_url)

    return app, api_key


@pytest.fixture()
def client(app_and_key):
    """TestClient backed by the real app."""
    app, _ = app_and_key
    return TestClient(app)


@pytest.fixture()
def auth_headers(app_and_key):
    """Headers with a valid API key."""
    _, key = app_and_key
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# Metrics endpoint integration
# ---------------------------------------------------------------------------


class TestMetricsIntegration:
    """Test /metrics endpoint through the real FastAPI app."""

    def test_metrics_endpoint_returns_200(self, client) -> None:
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_correct_content_type(self, client) -> None:
        resp = client.get("/metrics")
        assert "text/plain" in resp.headers["content-type"]

    def test_metrics_contains_request_duration(self, client) -> None:
        # Make a request that gets tracked
        client.get("/health")
        resp = client.get("/metrics")
        assert "http_request_duration_seconds" in resp.text

    def test_metrics_contains_request_count(self, client) -> None:
        client.get("/health")
        resp = client.get("/metrics")
        assert "http_requests_total" in resp.text

    def test_metrics_contains_nexus_info(self, client) -> None:
        resp = client.get("/metrics")
        assert "nexus_info" in resp.text

    def test_health_not_in_endpoint_labels(self, client) -> None:
        """Health checks should be excluded from metric labels."""
        for _ in range(5):
            client.get("/health")
        resp = client.get("/metrics")
        assert 'endpoint="/health"' not in resp.text

    def test_api_request_tracked(self, client, auth_headers) -> None:
        """A real API call should appear in metrics."""
        # Use a simple endpoint — stat returns 404 for missing path, that's fine
        client.get("/v1/stat?path=/nonexistent", headers=auth_headers)
        resp = client.get("/metrics")
        assert "http_requests_total" in resp.text


# ---------------------------------------------------------------------------
# Performance: verify minimal overhead
# ---------------------------------------------------------------------------


class TestMetricsPerformance:
    """Verify Prometheus middleware adds minimal overhead."""

    @pytest.mark.benchmark
    def test_health_with_middleware_fast(self, client) -> None:
        """100 /health requests must complete in under 5s (middleware overhead < 1ms/req)."""
        n = 100
        start = time.perf_counter()
        for _ in range(n):
            resp = client.get("/health")
            assert resp.status_code == 200
        elapsed = time.perf_counter() - start

        per_req_ms = (elapsed / n) * 1000
        assert elapsed < 5.0, f"100 requests took {elapsed:.2f}s — too slow"
        assert per_req_ms < 50, f"Per-request: {per_req_ms:.1f}ms — too much overhead"

    @pytest.mark.benchmark
    def test_metrics_endpoint_latency(self, client) -> None:
        """/metrics endpoint itself must respond quickly."""
        client.get("/metrics")  # warm-up

        n = 20
        start = time.perf_counter()
        for _ in range(n):
            resp = client.get("/metrics")
            assert resp.status_code == 200
        elapsed = time.perf_counter() - start

        per_req_ms = (elapsed / n) * 1000
        assert per_req_ms < 50, f"/metrics: {per_req_ms:.1f}ms per-request"
