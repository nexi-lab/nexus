"""Search management requests preserve the authorized indexing zone."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

# nexus.bricks.search.__init__ imports SearchService → nexus_runtime (Rust
# extension).  Stub it before any nexus.bricks.search import triggers it.
if "nexus_runtime" not in sys.modules:
    from unittest.mock import MagicMock as _MagicMock

    _nexus_runtime_stub = _MagicMock()
    _nexus_runtime_stub.__name__ = "nexus_runtime"
    _nexus_runtime_stub.__spec__ = types.ModuleType("nexus_runtime")
    sys.modules["nexus_runtime"] = _nexus_runtime_stub

from nexus.contracts.constants import ROOT_ZONE_ID
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

    @pytest.mark.asyncio
    async def test_evict_stamps_zone(self) -> None:
        daemon, stub = _daemon_with_stub(
            NotifyFileChange=search_pb2.NotifyFileChangeResponse(status="accepted", index_seq=2)
        )

        await daemon.notify_file_change("/a.py", "delete", zone_id="zone-a")
        await daemon.notify_file_change("/b.py", "delete", zone_id="zone-b")

        assert [(req.path, req.change_type, req.zone_id) for _, req in stub.requests] == [
            ("/a.py", "delete", "zone-a"),
            ("/b.py", "delete", "zone-b"),
        ]


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
