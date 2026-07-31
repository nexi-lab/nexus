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

    def test_singleton_token_explicit_federated_stays_scoped(self, monkeypatch):
        """#4541 review round 3: a single-zone (non-root) token requesting
        federated=true must still be scoped to its allow-list — previously it
        reached the dispatcher with zone_filter=None and searched every
        subject-accessible zone."""
        app = self._build_app(["eng"])
        client = TestClient(app)

        from nexus.server.api.v2.routers import search as search_mod

        captured = {}

        async def fake_federated(*, zone_filter=None, **kwargs):
            captured["zone_filter"] = zone_filter
            return {"results": [], "federated": True}

        monkeypatch.setattr(search_mod, "_handle_federated_search", fake_federated)
        resp = client.get("/api/v2/search/query?q=alpha&federated=true")
        assert resp.status_code == 200, resp.text
        assert captured["zone_filter"] == frozenset({"eng"})

    def test_unconstrained_admin_federated_stays_unrestricted(self, monkeypatch):
        """#4541 review round 5: an EMPTY zone_set is the auth contract for
        unconstrained credentials — the synthesized active-zone fallback must
        not become a hard allow-list under federated=true."""
        from nexus.server.api.v2.routers import search as search_mod
        from nexus.server.dependencies import require_auth

        app = self._build_app(["eng"])
        app.dependency_overrides[require_auth] = lambda: {
            "authenticated": True,
            "user_id": "admin_user",
            "zone_id": "eng",  # active routing zone, NOT a token allow-list
            "zone_set": [],
        }
        client = TestClient(app)

        captured = {}

        async def fake_federated(*, zone_filter=None, **kwargs):
            captured["zone_filter"] = zone_filter
            return {"results": [], "federated": True}

        monkeypatch.setattr(search_mod, "_handle_federated_search", fake_federated)
        resp = client.get("/api/v2/search/query?q=alpha&federated=true")
        assert resp.status_code == 200, resp.text
        assert captured["zone_filter"] is None

    def test_root_token_federated_stays_unrestricted(self, monkeypatch):
        """Root-scoped tokens keep wildcard federation: the root zone grants
        cross-zone access and its id never matches concrete zone names."""
        from nexus.server.api.v2.routers import search as search_mod
        from nexus.server.dependencies import require_auth

        app = self._build_app(["root"])
        app.dependency_overrides[require_auth] = lambda: {
            "authenticated": True,
            "user_id": "test_user",
            "zone_id": "root",
            "zone_set": ["root"],
        }
        client = TestClient(app)

        captured = {}

        async def fake_federated(*, zone_filter=None, **kwargs):
            captured["zone_filter"] = zone_filter
            return {"results": [], "federated": True}

        monkeypatch.setattr(search_mod, "_handle_federated_search", fake_federated)
        resp = client.get("/api/v2/search/query?q=alpha&federated=true")
        assert resp.status_code == 200, resp.text
        assert captured["zone_filter"] is None


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


@pytest.mark.skipif(not _HAS_FASTAPI_TESTCLIENT, reason="fastapi test client not available")
class TestSingleZoneTokenFederatedEscape:
    """Issue #4542 round-6 review: a single-zone token requesting
    federated=true must still be confined to its zone — the router used to
    forward zone_filter=None for one-element zone_sets, letting the query
    fan out to every zone the SUBJECT can reach (credential-scope escape)."""

    def test_single_zone_token_federated_true_forwards_zone_filter(self, monkeypatch):
        builder = TestSearchZoneSet()
        app = builder._build_app(["eng"])
        client = TestClient(app)

        from nexus.server.api.v2.routers import search as search_mod

        captured = {}

        async def fake_federated(*, zone_filter=None, **kwargs):
            captured["zone_filter"] = zone_filter
            return {"results": [], "federated": True}

        monkeypatch.setattr(search_mod, "_handle_federated_search", fake_federated)
        resp = client.get("/api/v2/search/query?q=alpha&federated=true")
        assert resp.status_code == 200, resp.text
        assert captured["zone_filter"] == frozenset({"eng"})

    def test_no_explicit_zone_set_keeps_unbounded_federation(self, monkeypatch):
        """Credentials WITHOUT an explicit zone allow-list keep the legacy
        subject-wide federation (zone_filter=None)."""
        from nexus.server.api.v2.routers.search import router

        app = FastAPI()
        app.include_router(router)
        mock_daemon = MagicMock()
        mock_daemon.is_initialized = True

        async def mock_search(**kwargs):
            return []

        mock_daemon.search = mock_search
        app.state.search_daemon = mock_daemon
        app.state.search_daemon_enabled = True
        app.state.record_store = MagicMock()
        app.state.async_read_session_factory = MagicMock()

        from nexus.server.dependencies import require_auth

        app.dependency_overrides[require_auth] = lambda: {
            "authenticated": True,
            "user_id": "test_user",
            "zone_id": "eng",
            # no zone_set key: synthesized fallback, not a token grant
        }
        client = TestClient(app)

        from nexus.server.api.v2.routers import search as search_mod

        captured = {}

        async def fake_federated(*, zone_filter=None, **kwargs):
            captured["zone_filter"] = zone_filter
            return {"results": [], "federated": True}

        monkeypatch.setattr(search_mod, "_handle_federated_search", fake_federated)
        resp = client.get("/api/v2/search/query?q=alpha&federated=true")
        assert resp.status_code == 200, resp.text
        assert captured["zone_filter"] is None


@pytest.mark.skipif(not _HAS_FASTAPI_TESTCLIENT, reason="fastapi test client not available")
class TestSandboxFallbackPooling:
    """Issue #4542 round-6 review: the SANDBOX all-peers-failed fallback
    replaces the dispatcher's capped results wholesale, so it must apply the
    per-document cap itself (fetch wider, cap, trim)."""

    def test_all_peers_failed_fallback_caps(self, monkeypatch):
        from nexus.bricks.search.daemon import DaemonConfig
        from nexus.server.api.v2.routers.search import router

        app = FastAPI()
        app.include_router(router)

        mock_daemon = MagicMock()
        mock_daemon.is_initialized = True
        mock_daemon.config = DaemonConfig(page_aggregation=True, chunks_per_page=1)
        app.state.search_daemon = mock_daemon
        app.state.search_daemon_enabled = True
        app.state.record_store = MagicMock()
        app.state.async_read_session_factory = MagicMock()
        app.state.deployment_profile = "sandbox"

        rebac = MagicMock()
        rebac.list_accessible_zones = AsyncMock(return_value=[])
        app.state.rebac_service = rebac

        captured = {}

        async def fake_semantic_search(**kwargs):
            captured["limit"] = kwargs.get("limit")
            rows = [
                {"path": "/long.md", "chunk_index": i, "score": 0.9 - i * 0.1,
                 "chunk_text": f"c{i}", "semantic_degraded": True}
                for i in range(3)
            ]
            rows.append(
                {"path": "/other.md", "chunk_index": 0, "score": 0.5,
                 "chunk_text": "o", "semantic_degraded": True}
            )
            return rows[: kwargs.get("limit", 10)]

        search_service = MagicMock()
        search_service.semantic_search = fake_semantic_search
        nexus_fs = MagicMock()
        nexus_fs.service.return_value = search_service
        app.state.nexus_fs = nexus_fs

        from nexus.server.dependencies import require_auth

        app.dependency_overrides[require_auth] = lambda: {
            "authenticated": True,
            "user_id": "test_user",
            "subject_type": "user",
            "subject_id": "test_user",
            "zone_id": "eng",
            "zone_set": ["eng"],
        }
        client = TestClient(app)

        resp = client.get("/api/v2/search/query?q=alpha&federated=true&limit=3")
        assert resp.status_code == 200, resp.text

        paths = [r["path"] for r in resp.json()["results"]]
        assert paths == ["/long.md", "/other.md"]
        # Fetched wider than the requested limit so the cap could backfill.
        assert captured["limit"] > 3
