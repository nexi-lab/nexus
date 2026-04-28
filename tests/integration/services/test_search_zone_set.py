"""Search router auto-fans-out across token zone_set (#3785, AC #2/#3)."""

from __future__ import annotations

import sys
import types

# nexus.bricks.search.__init__ imports SearchService → nexus_runtime (Rust
# extension).  The Rust binary is not available in the test venv, so we
# stub the module before any nexus.bricks.search import can trigger it.
# Using a MagicMock stub so that any attribute access (import name from ...)
# succeeds without enumerating every symbol the Rust extension exposes.
if "nexus_runtime" not in sys.modules:
    from unittest.mock import MagicMock as _MagicMock

    _nexus_runtime_stub = _MagicMock()
    _nexus_runtime_stub.__name__ = "nexus_runtime"
    _nexus_runtime_stub.__spec__ = types.ModuleType("nexus_runtime")
    sys.modules["nexus_runtime"] = _nexus_runtime_stub

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

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
class TestSearchZoneSet:
    def _build_app(self, zone_set):
        from nexus.server.api.v2.routers.search import router

        app = FastAPI()
        app.include_router(router)

        mock_daemon = MagicMock()
        mock_daemon.is_initialized = True
        mock_daemon.get_health.return_value = {"status": "ok"}

        async def mock_search(**kwargs):
            return [_MockResult(path="result.txt", chunk_text="found", score=0.9)]

        mock_daemon.search = mock_search
        app.state.search_daemon = mock_daemon
        app.state.search_daemon_enabled = True
        app.state.record_store = MagicMock()
        app.state.async_session_factory = MagicMock()
        app.state.async_read_session_factory = MagicMock()

        from nexus.server.dependencies import require_auth

        app.dependency_overrides[require_auth] = lambda: {
            "authenticated": True,
            "user_id": "test_user",
            "zone_id": "eng",
            "zone_set": list(zone_set),
        }
        return app

    def test_single_zone_token_uses_single_zone_path(self, monkeypatch):
        """Single-zone token → unchanged single-zone code path (no federated)."""
        app = self._build_app(["eng"])
        client = TestClient(app)

        # Sentinel: federated path would call _handle_federated_search; intercept it.
        from nexus.server.api.v2.routers import search as search_mod

        called = {"federated": False}

        async def fake_federated(**kwargs):
            called["federated"] = True
            return {"results": [], "federated": True}

        monkeypatch.setattr(search_mod, "_handle_federated_search", fake_federated)
        resp = client.get("/api/v2/search/query?q=alpha")
        assert resp.status_code == 200, resp.text
        assert called["federated"] is False

    def test_multi_zone_token_auto_promotes_to_federated(self, monkeypatch):
        """Multi-zone token → auto-promotes to federated even with federated=false."""
        app = self._build_app(["eng", "legal"])
        client = TestClient(app)

        from nexus.server.api.v2.routers import search as search_mod

        captured = {}

        async def fake_federated(*, zone_filter=None, **kwargs):
            captured["zone_filter"] = zone_filter
            return {"results": [], "federated": True}

        monkeypatch.setattr(search_mod, "_handle_federated_search", fake_federated)
        resp = client.get("/api/v2/search/query?q=alpha")
        assert resp.status_code == 200, resp.text
        assert captured["zone_filter"] is not None
        assert sorted(captured["zone_filter"]) == ["eng", "legal"]


@pytest.mark.skipif(not _HAS_FASTAPI_TESTCLIENT, reason="fastapi test client not available")
class TestFederatedDispatcherZoneFilter:
    """Direct unit test on FederatedSearchDispatcher.search(zone_filter=...) — verifies
    that the dispatcher intersects accessible_zones with the provided filter (#3785)."""

    @pytest.mark.asyncio
    async def test_zone_filter_intersects_accessible_zones(self):
        from nexus.bricks.search.federated_search import FederatedSearchDispatcher

        rebac = MagicMock()
        daemon = MagicMock()
        daemon.search = AsyncMock(return_value=[])
        registry = MagicMock()

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac, registry=registry)

        # Stub zone discovery to return three zones.
        async def fake_accessible(subject):
            return ["eng", "legal", "ops"]

        dispatcher._get_accessible_zones = fake_accessible
        # Skip the per-zone search type filter
        dispatcher._should_skip_zone = lambda z, search_type: False

        searched_zones = []

        async def fake_zone_search(zone_id, **kwargs):
            searched_zones.append(zone_id)
            return []

        # Patch the actual zone-search method (find it via inspection).
        # The dispatcher iterates searchable_zones and calls a per-zone method.
        # If the method name differs, adapt; the assertion below is what matters.
        # We rely on the public search() method intersecting zone_filter properly.

        resp = await dispatcher.search(
            query="alpha",
            subject=("user", "alice"),
            search_type="hybrid",
            limit=10,
            path_filter=None,
            alpha=0.5,
            fusion_method="rrf",
            zone_filter=frozenset({"eng"}),  # Token only grants eng
        )

        # The dispatcher should have only searched eng (intersection of
        # accessible {eng,legal,ops} and zone_filter {eng}).
        assert resp.zones_searched == ["eng"], f"expected only eng, got {resp.zones_searched}"
