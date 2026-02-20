"""Tests for health probe endpoints (#2168)."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.health.probes import router
from nexus.server.health.startup_tracker import _REQUIRED_FOR_READY, StartupPhase, StartupTracker


def _make_app(tracker: StartupTracker | None = None) -> FastAPI:
    """Create a minimal FastAPI app with probes router."""
    app = FastAPI()
    app.include_router(router)
    app.state.startup_tracker = tracker
    app.state.nexus_fs = None
    return app


class TestLivenessProbe:
    """GET /healthz/live"""

    def test_always_200(self) -> None:
        client = TestClient(_make_app())
        resp = client.get("/healthz/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"

    def test_200_even_during_startup(self) -> None:
        client = TestClient(_make_app(StartupTracker()))
        resp = client.get("/healthz/live")
        assert resp.status_code == 200


class TestStartupProbe:
    """GET /healthz/startup"""

    def test_503_when_phases_pending(self) -> None:
        tracker = StartupTracker()
        client = TestClient(_make_app(tracker))
        resp = client.get("/healthz/startup")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "starting"
        assert "pending_phases" in body
        assert "completed_phases" in body

    def test_200_when_all_complete(self) -> None:
        tracker = StartupTracker()
        for phase in StartupPhase:
            tracker.complete(phase)
        client = TestClient(_make_app(tracker))
        resp = client.get("/healthz/startup")
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

    def test_200_when_no_tracker(self) -> None:
        """If no tracker is set (e.g., tests), assume started."""
        client = TestClient(_make_app(tracker=None))
        resp = client.get("/healthz/startup")
        assert resp.status_code == 200


class TestReadinessProbe:
    """GET /healthz/ready"""

    def test_503_before_required_phases(self) -> None:
        tracker = StartupTracker()
        client = TestClient(_make_app(tracker))
        resp = client.get("/healthz/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready"
        assert body["reason"] == "startup_incomplete"

    def test_200_after_required_phases(self) -> None:
        tracker = StartupTracker()
        for phase in _REQUIRED_FOR_READY:
            tracker.complete(phase)
        client = TestClient(_make_app(tracker))
        resp = client.get("/healthz/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert "uptime_seconds" in body

    def test_503_on_raft_not_ready(self) -> None:
        tracker = StartupTracker()
        for phase in _REQUIRED_FOR_READY:
            tracker.complete(phase)

        # Mock NexusFS with zone_mgr that returns False
        mock_fs = MagicMock()
        mock_fs._zone_mgr.ensure_topology.return_value = False

        app = _make_app(tracker)
        app.state.nexus_fs = mock_fs
        client = TestClient(app)
        resp = client.get("/healthz/ready")
        assert resp.status_code == 503
        assert resp.json()["reason"] == "Raft topology not ready"

    def test_200_when_raft_ok(self) -> None:
        tracker = StartupTracker()
        for phase in _REQUIRED_FOR_READY:
            tracker.complete(phase)

        mock_fs = MagicMock()
        mock_fs._zone_mgr.ensure_topology.return_value = True

        app = _make_app(tracker)
        app.state.nexus_fs = mock_fs
        client = TestClient(app)
        resp = client.get("/healthz/ready")
        assert resp.status_code == 200

    def test_503_on_db_pool_exhausted(self) -> None:
        tracker = StartupTracker()
        for phase in _REQUIRED_FOR_READY:
            tracker.complete(phase)

        mock_fs = MagicMock()
        mock_fs._zone_mgr = None  # no Raft
        mock_fs.metadata.get_pool_stats.return_value = {"idle": 0}

        app = _make_app(tracker)
        app.state.nexus_fs = mock_fs
        client = TestClient(app)
        resp = client.get("/healthz/ready")
        assert resp.status_code == 503
        assert "DB pool exhausted" in resp.json()["reason"]

    def test_fail_open_on_exception(self) -> None:
        """Any unexpected error should still return 200 (fail-open)."""
        tracker = StartupTracker()
        for phase in _REQUIRED_FOR_READY:
            tracker.complete(phase)

        mock_fs = MagicMock()
        mock_fs._zone_mgr = None
        mock_fs.metadata.get_pool_stats.side_effect = RuntimeError("boom")

        app = _make_app(tracker)
        app.state.nexus_fs = mock_fs
        client = TestClient(app)
        resp = client.get("/healthz/ready")
        # The db check fails open, so we still get 200
        assert resp.status_code == 200
