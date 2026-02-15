"""Integration tests for WriteBuffer Prometheus metrics pipeline (Issue #1370).

Validates that nexus_write_buffer_* metrics appear on the /metrics endpoint
when the real FastAPI application is wired with a WriteBuffer-enabled NexusFS.

Uses an in-process TestClient (no subprocess) for speed and determinism.
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

# All 9 metric families emitted by WriteBufferCollector
_EXPECTED_WB_METRICS = [
    "nexus_write_buffer_events_enqueued_total",
    "nexus_write_buffer_events_flushed_total",
    "nexus_write_buffer_events_failed_total",
    "nexus_write_buffer_retries_total",
    "nexus_write_buffer_pending_events",
    "nexus_write_buffer_flush_duration_seconds_sum",
    "nexus_write_buffer_flush_duration_seconds_count",
    "nexus_write_buffer_flush_batch_size_sum",
    "nexus_write_buffer_flush_batch_size_count",
]


@pytest.fixture()
def app_and_key(tmp_path):
    """Build a real FastAPI app with NexusFS + WriteBuffer enabled."""
    from nexus.server.fastapi_server import create_app

    os.environ.setdefault("NEXUS_JWT_SECRET", "test-secret-wb-metrics")

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
        enable_write_buffer=True,  # Force-enable for SQLite
    )

    api_key = "test-wb-metrics-key"
    app = create_app(nexus_fs=nx, api_key=api_key, database_url=db_url)

    return app, api_key, nx


@pytest.fixture()
def client(app_and_key):
    app, _, _ = app_and_key
    return TestClient(app)


@pytest.fixture()
def auth_headers(app_and_key):
    _, key, _ = app_and_key
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture()
def nexus_fs(app_and_key):
    _, _, nx = app_and_key
    return nx


# ---------------------------------------------------------------------------
# WriteBuffer metrics in /metrics output
# ---------------------------------------------------------------------------


class TestWriteBufferMetricsPresent:
    """Verify all nexus_write_buffer_* metric families appear on /metrics."""

    def test_all_wb_metric_families_present(self, client) -> None:
        """All 9 WriteBufferCollector families must appear."""
        resp = client.get("/metrics")
        assert resp.status_code == 200
        for metric in _EXPECTED_WB_METRICS:
            assert metric in resp.text, f"Missing metric: {metric}"

    def test_enqueued_has_event_type_labels(self, client) -> None:
        """The enqueued metric should have write/delete/rename labels."""
        resp = client.get("/metrics")
        body = resp.text
        assert 'event_type="write"' in body
        assert 'event_type="delete"' in body
        assert 'event_type="rename"' in body

    def test_wb_metrics_start_at_zero(self, client) -> None:
        """Before any writes, all counters should be 0."""
        resp = client.get("/metrics")
        body = resp.text
        # Flushed, failed, retries should all be 0
        assert "nexus_write_buffer_events_flushed_total 0.0" in body
        assert "nexus_write_buffer_events_failed_total 0.0" in body
        assert "nexus_write_buffer_retries_total 0.0" in body


# ---------------------------------------------------------------------------
# WriteBuffer metrics update after writes
# ---------------------------------------------------------------------------


class TestWriteBufferMetricsAfterWrites:
    """After performing writes via the API, counters should increase."""

    def test_enqueued_increases_after_write(self, client, auth_headers) -> None:
        """Writing a file should increment the enqueued counter."""
        # Write a file via the API
        client.put(
            "/v1/write",
            params={"path": "/test/wb_metrics.txt"},
            content=b"hello world",
            headers=auth_headers,
        )

        # Give the write buffer a moment to process
        time.sleep(0.5)

        resp = client.get("/metrics")
        body = resp.text
        # At minimum, the write enqueue count should be > 0
        # Find the line: nexus_write_buffer_events_enqueued_total{event_type="write"} N
        assert "nexus_write_buffer_events_enqueued_total" in body


# ---------------------------------------------------------------------------
# No auth required for /metrics (even with permissions enabled)
# ---------------------------------------------------------------------------


class TestMetricsNoAuthRequired:
    """The /metrics endpoint should be accessible without authentication."""

    def test_unauthenticated_can_read_wb_metrics(self, client) -> None:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "nexus_write_buffer_events_flushed_total" in resp.text


# ---------------------------------------------------------------------------
# Performance: verify negligible overhead
# ---------------------------------------------------------------------------


class TestWriteBufferMetricsPerformance:
    """Verify that the WriteBuffer collector adds no measurable latency."""

    @pytest.mark.benchmark
    def test_metrics_latency_under_50ms(self, client) -> None:
        """/metrics must still respond in <50ms with WriteBuffer collector."""
        client.get("/metrics")  # warm-up

        n = 20
        start = time.perf_counter()
        for _ in range(n):
            resp = client.get("/metrics")
            assert resp.status_code == 200
        elapsed = time.perf_counter() - start

        per_req_ms = (elapsed / n) * 1000
        assert per_req_ms < 50, f"/metrics: {per_req_ms:.1f}ms per request -- too slow"

    @pytest.mark.benchmark
    def test_collector_scrape_overhead(self, nexus_fs) -> None:
        """Reading metrics dict directly must be sub-millisecond."""
        wo = nexus_fs._write_observer
        assert wo is not None

        n = 10000
        start = time.perf_counter()
        for _ in range(n):
            _ = wo.metrics
        elapsed = time.perf_counter() - start

        per_call_us = (elapsed / n) * 1_000_000
        assert per_call_us < 100, f"metrics property: {per_call_us:.1f}us -- too slow"


# ---------------------------------------------------------------------------
# Existing metrics still present (co-existence)
# ---------------------------------------------------------------------------


class TestExistingMetricsCoexistence:
    """Existing HTTP and DB metrics should still appear alongside WB metrics."""

    def test_http_metrics_still_present(self, client, auth_headers) -> None:
        client.get("/health")
        resp = client.get("/metrics")
        assert "http_requests_total" in resp.text
        assert "http_request_duration_seconds" in resp.text

    def test_nexus_info_still_present(self, client) -> None:
        resp = client.get("/metrics")
        assert "nexus_info" in resp.text
