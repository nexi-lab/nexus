"""Wire-contract tests for /search/index, /search/health, /search/stats, /search/refresh.

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


# Admin principal: /search/index enforces admin-or-path-WRITE (review
# R3); the read-only case is pinned by test_read_only_principal_cannot_index
# below.
_ADMIN_AUTH = {
    "authenticated": True,
    "user_id": "u",
    "zone_id": "eng",
    "zone_set": ["eng"],
    "zone_perms": [["eng", "r"], ["eng", "w"]],
    "is_admin": True,
}


def _build_app(daemon, auth=None):
    from nexus.server.api.v2.routers.search import router

    app = FastAPI()
    app.include_router(router)
    app.state.search_daemon = daemon
    app.state.search_daemon_enabled = daemon is not None
    app.state.record_store = MagicMock()
    app.state.async_session_factory = MagicMock()
    app.state.async_read_session_factory = MagicMock()

    from nexus.server.dependencies import get_auth_result, require_auth

    principal = dict(auth or _ADMIN_AUTH)
    app.dependency_overrides[require_auth] = lambda: principal
    # /search/stats takes OPTIONAL auth via get_auth_result directly
    # (#4736 zone scoping), so override that dependency too.
    app.dependency_overrides[get_auth_result] = lambda: principal
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
        # #4736 adds ``skippedPaths`` + ``indexSeq`` (0 here: the stub
        # reports no sequence) — additive next to the #4617 keys.
        assert body == {
            "status": "indexed",
            "count": 2,
            "skippedCount": 0,
            "skippedPaths": [],
            "indexSeq": 0,
            "zoneId": "eng",
        }
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

    def test_read_only_principal_cannot_index(self):
        from nexus.grpc.search.v1 import search_pb2
        from nexus.server.dependencies import require_auth

        daemon, stub = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(indexed_count=1)
        )
        app = _build_app(daemon)
        # Non-admin principal + no permission enforcer wired ⇒ the
        # WRITE gate fails CLOSED (review R3): explicit indexing
        # replaces content other readers see, so a read-only token
        # must not reach the daemon.
        app.dependency_overrides[require_auth] = lambda: {
            "authenticated": True,
            "user_id": "reader",
            "zone_id": "eng",
            "zone_set": ["eng"],
            "zone_perms": [["eng", "r"]],
            "is_admin": False,
        }

        resp = TestClient(app).post(
            "/api/v2/search/index",
            json={"documents": [{"path": "/ws/a.md", "text": "poison"}]},
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

    def test_writer_liveness_fields_flow_through(self):
        # #4725: the plugin's structured writer-liveness counters ride
        # the same dict as ``status`` / ``detail`` so pollers can gate
        # on numbers instead of parsing prose.
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            Health=search_pb2.HealthResponse(
                status="degraded",
                detail="fts writer fault, unverified since — zone 'root' 3s ago",
                fts_writer_faults=1,
                fts_writer_unavailable=0,
                last_verified_commit_age_ms=4200,
                dispatch_panics=2,
            )
        )
        app = _build_app(daemon)

        body = TestClient(app).get("/api/v2/search/health").json()
        assert body["status"] == "degraded"
        assert body["fts_writer_faults"] == 1
        assert body["fts_writer_unavailable"] == 0
        assert body["last_verified_commit_age_ms"] == 4200
        assert body["dispatch_panics"] == 2
        # Rebuilt-but-unverified writer: keyword leg still answers.
        assert body["bm25_index_loaded"] is True

    def test_unset_commit_age_is_null(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            Health=search_pb2.HealthResponse(status="healthy", detail="fts + ann online")
        )

        body = TestClient(_build_app(daemon)).get("/api/v2/search/health").json()
        assert body["last_verified_commit_age_ms"] is None
        assert body["fts_writer_faults"] == 0
        assert body["fts_writer_unavailable"] == 0
        assert body["dispatch_panics"] == 0

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
        # #4643: monitors key on a non-empty vector_backend as the
        # "vector lane is configured" signal.
        assert body["vector_backend"] == "hnsw-in-process"
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
        # #4643: keyword-only mode has no vector lane — the key is
        # present (absent keys break consumers) but honestly None.
        assert "vector_backend" in body
        assert body["vector_backend"] is None
        assert body["indexing_in_progress"] == 0


# =============================================================================
# #4736 — write-to-searchable contract
# =============================================================================


class _FakeFS:
    """Just enough VFS for /search/refresh: ``read`` + ``sys_stat``."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def read(self, path, *, context=None, **_):
        from nexus.contracts.exceptions import NexusFileNotFoundError

        if path not in self.files:
            raise NexusFileNotFoundError(path)
        return self.files[path]

    def sys_stat(self, path, *, context=None, **_):
        return {"modified_at_ms": 1_700_000_000_000}


class TestRefreshResponseContract:
    """/search/refresh says what it DID — never a bare "accepted" ack."""

    def test_update_reads_the_file_and_indexes_it_synchronously(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, stub = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(indexed_count=1, index_seq=9)
        )
        app = _build_app(daemon)
        app.state.nexus_fs = _FakeFS({"/docs/a.md": b"needle in the doc"})

        resp = TestClient(app).post(
            "/api/v2/search/refresh", params={"path": "/docs/a.md", "change_type": "update"}
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "indexed",
            "path": "/docs/a.md",
            "change_type": "update",
            "index_seq": 9,
        }
        ((method, req),) = stub.requests
        assert method == "IndexDocuments"
        # The TOKEN zone is stamped on the call — the zone /search/query
        # reads and /search/index writes — even for this principal whose
        # two zone_perms rows make get_operation_context collapse the
        # OperationContext zone to ROOT (#4736).  The doc carries the
        # stat mtime so a later Refresh walk verdicts it Unchanged.
        assert req.zone_id == "eng"
        assert [(d.path, d.text, d.mtime_ms, d.zone_id) for d in req.documents] == [
            ("/docs/a.md", "needle in the doc", 1_700_000_000_000, "eng")
        ]

    def test_update_on_a_missing_path_is_404_not_accepted(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, stub = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(indexed_count=1, index_seq=1),
            NotifyFileChange=search_pb2.NotifyFileChangeResponse(status="skipped"),
        )
        app = _build_app(daemon)
        app.state.nexus_fs = _FakeFS({})

        resp = TestClient(app).post(
            "/api/v2/search/refresh", params={"path": "/docs/nope.md", "change_type": "update"}
        )

        assert resp.status_code == 404
        assert "accepted" not in resp.text
        assert stub.requests == [], "nothing to index ⇒ the plugin is not called"

    def test_update_with_empty_content_is_409_skipped(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(
                indexed_count=0, skipped_count=1, skipped_paths=["/docs/empty.md"], index_seq=4
            )
        )
        app = _build_app(daemon)
        app.state.nexus_fs = _FakeFS({"/docs/empty.md": b"   \n"})

        resp = TestClient(app).post(
            "/api/v2/search/refresh", params={"path": "/docs/empty.md", "change_type": "update"}
        )

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["status"] == "skipped"
        assert detail["path"] == "/docs/empty.md"
        assert "empty" in detail["reason"]

    def test_update_with_binary_content_is_409_without_reaching_the_plugin(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, stub = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(indexed_count=1, index_seq=1)
        )
        app = _build_app(daemon)
        app.state.nexus_fs = _FakeFS({"/bin/blob": b"\xff\xfe\x00"})

        resp = TestClient(app).post(
            "/api/v2/search/refresh", params={"path": "/bin/blob", "change_type": "create"}
        )

        assert resp.status_code == 409
        assert resp.json()["detail"]["status"] == "skipped"
        assert "UTF-8" in resp.json()["detail"]["reason"]
        assert stub.requests == []

    def test_delete_evicts_and_reports_deleted_with_seq(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, stub = _daemon_with_stub(
            NotifyFileChange=search_pb2.NotifyFileChangeResponse(status="accepted", index_seq=4)
        )
        app = _build_app(daemon)
        app.state.nexus_fs = _FakeFS({})  # delete never reads the file

        resp = TestClient(app).post(
            "/api/v2/search/refresh", params={"path": "/docs/gone.md", "change_type": "delete"}
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "deleted",
            "path": "/docs/gone.md",
            "change_type": "delete",
            "index_seq": 4,
        }
        ((method, req),) = stub.requests
        assert method == "NotifyFileChange"
        assert (req.path, req.change_type, req.zone_id) == ("/docs/gone.md", "delete", "eng")

    def test_delete_plugin_error_fails_closed_500(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            NotifyFileChange=search_pb2.NotifyFileChangeResponse(
                status="", error="delete tombstone persist failed"
            )
        )
        app = _build_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/refresh", params={"path": "/docs/gone.md", "change_type": "delete"}
        )

        assert resp.status_code == 500
        assert "tombstone" in resp.json()["detail"]

    def test_invalid_change_type_is_400(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            NotifyFileChange=search_pb2.NotifyFileChangeResponse(status="skipped")
        )
        resp = TestClient(_build_app(daemon)).post(
            "/api/v2/search/refresh", params={"path": "/x", "change_type": "touch"}
        )
        assert resp.status_code == 400


class TestIndexSeqContract:
    def test_index_seq_and_skipped_paths_surface_on_search_index(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(
                indexed_count=2, skipped_count=1, skipped_paths=["/e.md"], index_seq=7
            )
        )
        app = _build_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/index",
            json={
                "documents": [
                    {"path": "/a.md", "text": "alpha"},
                    {"path": "/b.md", "text": "bravo"},
                    {"path": "/e.md", "text": ""},
                ]
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["skippedCount"] == 1
        assert body["skippedPaths"] == ["/e.md"]
        assert body["indexSeq"] == 7


class TestStatsStallDetectionContract:
    def test_seq_pending_and_clock_surface(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, stub = _daemon_with_stub(
            Stats=search_pb2.StatsResponse(
                fts_doc_count=1,
                backend="rust-plugin",
                embedding_model="",
                indexing_in_progress=1,
                last_index_seq=42,
                pending=3,
                last_successful_index_at_ms=1_700_000_000_000,
            )
        )
        body = TestClient(_build_app(daemon)).get("/api/v2/search/stats").json()

        assert body["last_index_seq"] == 42
        assert body["pending"] == 3
        assert body["last_successful_index_at"] == "2023-11-14T22:13:20+00:00"
        # Pre-P12 pollers keyed on float epoch seconds — same instant.
        assert body["last_index_refresh"] == 1_700_000_000.0
        # #4736: scoped to (and echoing) the token zone — the same zone
        # /search/query reads and every indexing call writes.
        ((method, req),) = stub.requests
        assert method == "Stats"
        assert req.zone_id == "eng"
        assert body["zone_id"] == "eng"

    def test_token_less_poller_keeps_root_zone_view(self):
        from nexus.contracts.constants import ROOT_ZONE_ID
        from nexus.grpc.search.v1 import search_pb2

        daemon, stub = _daemon_with_stub(
            Stats=search_pb2.StatsResponse(backend="rust-plugin", embedding_model="")
        )
        app = _build_app(daemon, auth={"authenticated": False})

        resp = TestClient(app).get("/api/v2/search/stats")

        assert resp.status_code == 200, "stats auth stays optional"
        ((_, req),) = stub.requests
        assert req.zone_id == "", "empty on the wire ⇒ plugin ROOT zone"
        assert resp.json()["zone_id"] == ROOT_ZONE_ID

    def test_never_indexed_reports_zero_and_none_not_epoch(self):
        from nexus.grpc.search.v1 import search_pb2

        daemon, _ = _daemon_with_stub(
            Stats=search_pb2.StatsResponse(backend="rust-plugin", embedding_model="")
        )
        body = TestClient(_build_app(daemon)).get("/api/v2/search/stats").json()

        assert body["last_index_seq"] == 0
        assert body["pending"] == 0
        assert body["last_successful_index_at"] is None
        assert body["last_index_refresh"] is None
