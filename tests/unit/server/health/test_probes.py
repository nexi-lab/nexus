"""Tests for health probe endpoints (#2168)."""

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

    def test_200_when_federation_disabled_even_if_kernel_reports_not_ready(
        self, monkeypatch
    ) -> None:
        tracker = StartupTracker()
        for phase in _REQUIRED_FOR_READY:
            tracker.complete(phase)

        import sys
        from types import SimpleNamespace

        monkeypatch.delenv("NEXUS_PEERS", raising=False)
        fake = SimpleNamespace(federation_is_initialized=lambda _k: False)
        monkeypatch.setitem(sys.modules, "nexus_runtime", fake)

        mock_fs = MagicMock()
        mock_fs._kernel = MagicMock()

        app = _make_app(tracker)
        app.state.nexus_fs = mock_fs
        client = TestClient(app)
        resp = client.get("/healthz/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_503_on_raft_not_ready(self, monkeypatch) -> None:
        tracker = StartupTracker()
        for phase in _REQUIRED_FOR_READY:
            tracker.complete(phase)

        # Phase H: federation readiness moved from the deleted
        # ``Kernel.mount_reconciliation_done`` PyO3 method to a
        # kernel-internal HAL probe exposed as
        # ``nexus_runtime.federation_is_initialized(kernel)``.  Stub
        # it out with the readiness flag flipped to False so the probe
        # fires the "Raft topology not ready" branch.
        import sys
        from types import SimpleNamespace

        monkeypatch.setenv("NEXUS_PEERS", "node-a,node-b")
        fake = SimpleNamespace(federation_is_initialized=lambda _k: False)
        monkeypatch.setitem(sys.modules, "nexus_runtime", fake)

        mock_fs = MagicMock()
        mock_fs._kernel = MagicMock()

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

        mock_root_store = MagicMock()
        mock_root_store.is_leader.return_value = True
        mock_zmgr = MagicMock()
        mock_zmgr.root_zone_id = "root"
        mock_zmgr.get_store.return_value = mock_root_store
        mock_fed = MagicMock()
        mock_fed.ensure_topology.return_value = True
        mock_fed.zone_manager = mock_zmgr
        mock_fs = MagicMock()
        mock_fs.service.return_value = mock_fed

        app = _make_app(tracker)
        app.state.nexus_fs = mock_fs
        client = TestClient(app)
        resp = client.get("/healthz/ready")
        assert resp.status_code == 200

    def test_200_when_follower(self) -> None:
        """Follower nodes are ready — they serve reads and forward writes."""
        tracker = StartupTracker()
        for phase in _REQUIRED_FOR_READY:
            tracker.complete(phase)

        mock_root_store = MagicMock()
        mock_root_store.is_leader.return_value = False
        mock_zmgr = MagicMock()
        mock_zmgr.root_zone_id = "root"
        mock_zmgr.get_store.return_value = mock_root_store
        mock_fed = MagicMock()
        mock_fed.ensure_topology.return_value = True
        mock_fed.zone_manager = mock_zmgr
        mock_fs = MagicMock()
        mock_fs.service.return_value = mock_fed

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

    def test_fail_open_on_db_check_exception(self) -> None:
        """DB pool check fails open (individual helper, not the probe itself)."""
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
        # The db check helper fails open, so readiness still succeeds
        assert resp.status_code == 200

    def test_503_on_unexpected_exception(self) -> None:
        """Issue #3063 §7: readiness probe fails closed on unexpected errors."""
        app = _make_app()
        # Force startup_tracker to raise when accessed
        app.state.startup_tracker = MagicMock()
        app.state.startup_tracker.is_ready = property(
            lambda s: (_ for _ in ()).throw(RuntimeError("probe bug"))
        )
        type(app.state.startup_tracker).is_ready = property(
            lambda s: (_ for _ in ()).throw(RuntimeError("probe bug"))
        )

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/healthz/ready")
        assert resp.status_code == 503
        assert resp.json()["status"] == "error"


class TestStartupProbeFailClosed:
    """Issue #3063 §7: startup probe exception behavior."""

    def test_503_on_unexpected_exception(self) -> None:
        """Startup probe should return 503 on unexpected error."""
        app = _make_app()
        # Force tracker to raise on attribute access
        bad_tracker = MagicMock()
        type(bad_tracker).is_complete = property(
            lambda s: (_ for _ in ()).throw(RuntimeError("probe bug"))
        )
        app.state.startup_tracker = bad_tracker

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/healthz/startup")
        assert resp.status_code == 503
        assert resp.json()["status"] == "error"


class TestLivenessProbeFailOpen:
    """Verify liveness probe stays fail-open (no change from Issue #3063)."""

    def test_always_200_regardless(self) -> None:
        """Liveness should never return non-200 to avoid restart loops."""
        client = TestClient(_make_app())
        resp = client.get("/healthz/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"
