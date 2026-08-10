"""Wire-contract tests for /search/index, /search/health, /search/stats.

#4617: these three response shapes drifted across the P12 pivot
(``count`` int → dict, ``zoneId`` → ``zone_id``, health/stats identity
fields dropped) and nothing at the HTTP layer pinned them.  These tests
run the routes against the REAL SearchDaemon gRPC proxy with only the
stub faked — the same pattern as ``TestBatchRealProxyRoundtrip`` — so
route↔proxy contract drift fails a test instead of breaking SDK
consumers in production.
"""

from __future__ import annotations

import sys
import types

# nexus.bricks.search.__init__ imports SearchService → nexus_runtime (Rust
# extension).  Stub it before any nexus.bricks.search import triggers it.
if "nexus_runtime" not in sys.modules:
    from unittest.mock import MagicMock as _MagicMock

    _nexus_runtime_stub = _MagicMock()
    _nexus_runtime_stub.__name__ = "nexus_runtime"
    _nexus_runtime_stub.__spec__ = types.ModuleType("nexus_runtime")
    sys.modules["nexus_runtime"] = _nexus_runtime_stub

from unittest.mock import MagicMock

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _HAS_FASTAPI_TESTCLIENT = True
except ImportError:
    _HAS_FASTAPI_TESTCLIENT = False

pytestmark = pytest.mark.skipif(
    not _HAS_FASTAPI_TESTCLIENT, reason="fastapi test client not available"
)


def _build_app(daemon):
    from nexus.server.api.v2.routers.search import router

    app = FastAPI()
    app.include_router(router)
    app.state.search_daemon = daemon
    app.state.search_daemon_enabled = daemon is not None
    app.state.record_store = MagicMock()
    app.state.async_session_factory = MagicMock()
    app.state.async_read_session_factory = MagicMock()

    from nexus.server.dependencies import require_auth

    app.dependency_overrides[require_auth] = lambda: {
        "authenticated": True,
        "user_id": "u",
        "zone_id": "eng",
        "zone_set": ["eng"],
        "zone_perms": [["eng", "r"], ["eng", "w"]],
    }
    return app


def _daemon_with_stub(**responses):
    """Real SearchDaemon proxy over a fake gRPC stub.

    ``responses`` maps RPC method name → canned pb response.
    """
    from nexus.bricks.search.daemon import SearchDaemon

    class _FakeStub:
        def __init__(self):
            self.requests = []

    def _make(method, resp):
        async def _call(req):
            stub.requests.append((method, req))
            return resp

        return _call

    daemon = SearchDaemon(target="127.0.0.1:1")  # never dialed
    stub = _FakeStub()
    for method, resp in responses.items():
        setattr(stub, method, _make(method, resp))
    daemon._stub = stub
    return daemon, stub


class TestIndexResponseContract:
    def test_count_is_int_and_zone_key_is_camel_case(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(indexed_count=2, skipped_count=0)
        )
        app = _build_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/index",
            json={
                "documents": [
                    {"path": "/ws/a.md", "text": "alpha"},
                    {"path": "/ws/b.md", "text": "beta"},
                ]
            },
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The pre-P12 public contract: count is a PLAIN INT (sum of
        # indexed) and the zone key is camelCase.  Post-pivot the
        # proxy's dict leaked whole into ``count`` and the key became
        # zone_id — strict SDK parsers threw on every successful index.
        # ``skippedCount`` is additive (review R2): content-level skips
        # are no longer invisible behind a bare 200.
        assert body == {"status": "indexed", "count": 2, "skippedCount": 0, "zoneId": "eng"}
        assert isinstance(body["count"], int)

    def test_content_skips_surface_in_response(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(indexed_count=1, skipped_count=1)
        )
        app = _build_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/index",
            json={
                "documents": [
                    {"path": "/ws/a.md", "text": "alpha"},
                    {"path": "/ws/b.md", "text": " "},
                ]
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["skippedCount"] == 1

    def test_plugin_error_fails_closed_500(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(
                indexed_count=1, error="fts commit failed"
            )
        )
        app = _build_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/index",
            json={"documents": [{"path": "/ws/a.md", "text": "alpha"}]},
        )

        # A populated plugin error must NEVER 200 (review R1) — clients
        # need to retry, not believe the index is complete.
        assert resp.status_code == 500, resp.text
        assert "fts commit failed" in resp.text

    def test_cross_zone_document_rejected(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, stub = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(indexed_count=1)
        )
        app = _build_app(daemon)

        # Auth zone is "eng"; smuggling a doc-level zone_id must 403
        # BEFORE anything reaches the daemon (review R2, critical —
        # per-doc zones are a plugin-side routing override, so honoring
        # them would let one tenant write into another's index).
        resp = TestClient(app).post(
            "/api/v2/search/index",
            json={
                "documents": [
                    {"path": "/ws/a.md", "text": "alpha", "zone_id": "victim-zone"},
                ]
            },
        )

        assert resp.status_code == 403, resp.text
        assert stub.requests == []

    def test_matching_doc_zone_is_allowed_and_forced(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, stub = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(indexed_count=2)
        )
        app = _build_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/index",
            json={
                "documents": [
                    {"path": "/ws/a.md", "text": "alpha", "zone_id": "eng"},
                    {"path": "/ws/b.md", "text": "beta"},
                ]
            },
        )

        assert resp.status_code == 200, resp.text
        (_, req) = stub.requests[0]
        # Defense in depth: EVERY wire document carries the authorized
        # zone regardless of what the caller supplied.
        assert [d.zone_id for d in req.documents] == ["eng", "eng"]


class TestHealthResponseContract:
    CONTRACT_KEYS = (
        "status",
        "initialized",
        "daemon_initialized",
        "backend",
        "bm25_index_loaded",
        "db_pool_ready",
        "zoekt_available",
    )

    def test_healthy_shape(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            Health=search_pb2.HealthResponse(status="healthy", detail="fts + ann online")
        )
        app = _build_app(daemon)

        body = TestClient(app).get("/api/v2/search/health").json()

        for key in self.CONTRACT_KEYS:
            assert key in body, f"missing contract key {key!r}: {body}"
        assert body["status"] == "healthy"
        assert body["initialized"] is True
        assert body["daemon_initialized"] is True
        assert body["backend"] == "rust-plugin"
        assert body["bm25_index_loaded"] is True
        assert body["db_pool_ready"] is True
        assert body["zoekt_available"] is False
        # Post-P12 additive detail stays.
        assert body["detail"] == "fts + ann online"

    def test_degraded_keeps_keyword_leg_loaded(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            Health=search_pb2.HealthResponse(status="degraded", detail="semantic leg missing")
        )
        app = _build_app(daemon)

        body = TestClient(app).get("/api/v2/search/health").json()
        assert body["status"] == "degraded"
        # Degraded = semantic missing; BM25 still answers.
        assert body["bm25_index_loaded"] is True

    def test_disabled_daemon_keeps_contract_keys(self):
        app = _build_app(None)

        body = TestClient(app).get("/api/v2/search/health").json()
        assert body["status"] == "disabled"
        for key in self.CONTRACT_KEYS:
            assert key in body, f"missing contract key {key!r}: {body}"
        assert body["initialized"] is False
        assert body["bm25_index_loaded"] is False

    def test_unreachable_plugin_is_unavailable_not_500(self):
        from nexus.bricks.search.daemon import SearchDaemon

        daemon = SearchDaemon(target="127.0.0.1:1")

        class _ExplodingStub:
            async def Health(self, req):  # noqa: N802 — gRPC stub surface
                raise RuntimeError("connection refused")

        daemon._stub = _ExplodingStub()
        app = _build_app(daemon)

        resp = TestClient(app).get("/api/v2/search/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "unavailable"
        assert body["bm25_index_loaded"] is False


class TestStatsResponseContract:
    def test_identity_and_visibility_fields(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            Stats=search_pb2.StatsResponse(
                fts_doc_count=22,
                fts_path_count=22,
                ann_chunk_count=37,
                parked_count=0,
                backend="rust-plugin",
                embedding_model="mE5-small-v1",
                indexing_in_progress=1,
            )
        )
        app = _build_app(daemon)

        body = TestClient(app).get("/api/v2/search/stats").json()

        assert body["fts_doc_count"] == 22
        assert body["initialized"] is True
        assert body["backend"] == "rust-plugin"
        assert body["embedding_model"] == "mE5-small-v1"
        # #4623: non-zero while an explicit index op is in flight.
        assert body["indexing_in_progress"] == 1

    def test_keyword_only_mode_reports_none_model(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            Stats=search_pb2.StatsResponse(
                fts_doc_count=5,
                fts_path_count=5,
                backend="rust-plugin",
                embedding_model="",
                indexing_in_progress=0,
            )
        )
        app = _build_app(daemon)

        body = TestClient(app).get("/api/v2/search/stats").json()
        # Empty on the wire ⇒ None on the JSON surface, matching the
        # pre-P12 "no embedding model configured" contract.
        assert body["embedding_model"] is None
        assert body["indexing_in_progress"] == 0
