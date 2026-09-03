"""Zone isolation through the post-P12 ``SearchDaemon`` gRPC proxy.

Every search / index / evict call the server makes must carry the caller's
zone to the Rust ``nexus-search-plugin`` — the plugin keeps one tantivy +
HNSW index per zone, so a dropped or wrong ``zone_id`` either leaks another
tenant's documents into a query or files a tenant's document where its own
queries never look.  These tests run the REAL proxy over a fake gRPC stub
and pin the zone on every outgoing request (same pattern as
``tests/integration/services/test_search_response_contracts.py``).

Rewritten for P12 (#4598 / #4736): the previous version drove the deleted
in-process daemon (``DaemonConfig`` / ``create_backend`` / txtai SQL WHERE
clauses) and had failed at collection since the pivot.
"""

from __future__ import annotations

import asyncio
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

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.search_types import SearchRequest
from nexus.grpc.search.v1 import search_pb2


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


def _hit(path: str, zone_id: str) -> search_pb2.QueryResult:
    return search_pb2.QueryResult(
        path=path, chunk_index=0, chunk_text="x", score=0.9, zone_id=zone_id
    )


class TestQueryZoneStamping:
    def test_search_forwards_the_request_zone(self) -> None:
        daemon, stub = _daemon_with_stub(Query=search_pb2.QueryResponse(results=[]))

        asyncio.run(daemon.search(SearchRequest(query="test query", zone_id="corp")))

        ((method, req),) = stub.requests
        assert method == "Query"
        assert req.zone_id == "corp"

    def test_search_zone_none_is_empty_on_the_wire(self) -> None:
        """Empty ``zone_id`` is the plugin's ROOT-zone selector — the proxy
        must not invent a zone the caller did not ask for."""
        daemon, stub = _daemon_with_stub(Query=search_pb2.QueryResponse(results=[]))

        asyncio.run(daemon.search(SearchRequest(query="test")))

        ((_, req),) = stub.requests
        assert req.zone_id == ""

    def test_different_zones_are_different_requests(self) -> None:
        daemon, stub = _daemon_with_stub(Query=search_pb2.QueryResponse(results=[]))

        asyncio.run(daemon.search(SearchRequest(query="test", zone_id="zone-a")))
        asyncio.run(daemon.search(SearchRequest(query="test", zone_id="zone-b")))

        assert [req.zone_id for _, req in stub.requests] == ["zone-a", "zone-b"]

    def test_results_keep_the_plugin_zone(self) -> None:
        """A hit's zone rides back on the result so the router can scope
        ReBAC / path unscoping per zone."""
        daemon, _ = _daemon_with_stub(
            Query=search_pb2.QueryResponse(results=[_hit("/a.py", "corp"), _hit("/b.py", "corp")])
        )

        results = asyncio.run(daemon.search(SearchRequest(query="test", zone_id="corp")))

        assert [r.path for r in results] == ["/a.py", "/b.py"]
        assert all(getattr(r, "zone_id", "corp") == "corp" for r in results)


class TestIndexZoneStamping:
    def test_index_documents_stamps_request_and_every_document(self) -> None:
        daemon, stub = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(indexed_count=2, index_seq=1)
        )

        asyncio.run(
            daemon.index_documents(
                [{"path": "/a.py", "text": "hello"}, {"path": "/b.py", "text": "world"}],
                zone_id="corp",
            )
        )

        ((method, req),) = stub.requests
        assert method == "IndexDocuments"
        assert req.zone_id == "corp"
        assert [d.zone_id for d in req.documents] == ["corp", "corp"]

    def test_authorized_zone_overrides_caller_supplied_document_zones(self) -> None:
        """Tenant boundary (review R2): a per-document ``zone_id`` is a
        plugin routing override, so honouring a caller-controlled value
        would let zone A write into zone B's index."""
        daemon, stub = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(indexed_count=1, index_seq=1)
        )

        asyncio.run(
            daemon.index_documents(
                [{"path": "/a.py", "text": "hello", "zone_id": "zone-b"}], zone_id="zone-a"
            )
        )

        ((_, req),) = stub.requests
        assert req.zone_id == "zone-a"
        assert [d.zone_id for d in req.documents] == ["zone-a"]

    def test_without_an_authorized_zone_document_zones_pass_through(self) -> None:
        daemon, stub = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(indexed_count=1, index_seq=1)
        )

        asyncio.run(daemon.index_documents([{"path": "/a.py", "text": "hello", "zone_id": "corp"}]))

        ((_, req),) = stub.requests
        assert req.zone_id == ""
        assert [d.zone_id for d in req.documents] == ["corp"]

    def test_evict_stamps_zone(self) -> None:
        daemon, stub = _daemon_with_stub(
            NotifyFileChange=search_pb2.NotifyFileChangeResponse(status="accepted", index_seq=2)
        )

        asyncio.run(daemon.notify_file_change("/a.py", "delete", zone_id="zone-a"))
        asyncio.run(daemon.notify_file_change("/b.py", "delete", zone_id="zone-b"))

        assert [(req.path, req.change_type, req.zone_id) for _, req in stub.requests] == [
            ("/a.py", "delete", "zone-a"),
            ("/b.py", "delete", "zone-b"),
        ]

    def test_locate_stamps_zone(self) -> None:
        daemon, stub = _daemon_with_stub(
            Locate=search_pb2.LocateResponse(indexed=True, chunk_count=1, zone_id="corp")
        )

        asyncio.run(daemon.locate("/a.py", zone_id="corp"))

        ((method, req),) = stub.requests
        assert method == "Locate"
        assert req.zone_id == "corp"


class TestRootZoneFallback:
    def test_root_zone_constant_is_what_the_http_layer_defaults_to(self) -> None:
        """The HTTP routes default a token without ``zone_id`` to
        ``ROOT_ZONE_ID`` (see ``index_zone_for``); the proxy sends that
        name verbatim, so the plugin's ``resolve_zone`` receives the
        same zone the server believes it indexed into."""
        from nexus.server.api.v2.routers._index_on_write import index_zone_for

        assert index_zone_for(None) == ROOT_ZONE_ID
        assert index_zone_for({"zone_id": ""}) == ROOT_ZONE_ID
        assert index_zone_for({"zone_id": "corp"}) == "corp"
        assert index_zone_for({"zone_id": "corp"}, override="other") == "other"

        daemon, stub = _daemon_with_stub(
            IndexDocuments=search_pb2.IndexDocumentsResponse(indexed_count=1, index_seq=1)
        )
        asyncio.run(
            daemon.index_documents(
                [{"path": "/a.py", "text": "hello"}], zone_id=index_zone_for({"zone_id": None})
            )
        )
        ((_, req),) = stub.requests
        assert req.zone_id == ROOT_ZONE_ID
