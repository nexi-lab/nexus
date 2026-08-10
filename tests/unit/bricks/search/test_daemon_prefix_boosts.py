"""SearchDaemon must map SearchRequest.path_prefix_boosts onto the proto (#4620).

The gRPC proxy is the last hop before the plugin — if it drops the
field, the router wiring upstream is dead config all over again.
"""

from __future__ import annotations

import pytest

from nexus.bricks.search.daemon import SearchDaemon
from nexus.contracts.search_types import SearchRequest
from nexus.grpc.search.v1 import search_pb2


class _CapturingStub:
    def __init__(self) -> None:
        self.query_requests: list[search_pb2.QueryRequest] = []

    async def Query(self, req: search_pb2.QueryRequest) -> search_pb2.QueryResponse:  # noqa: N802
        self.query_requests.append(req)
        return search_pb2.QueryResponse(results=[])


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _CapturingStub:
    captured = _CapturingStub()
    monkeypatch.setattr(SearchDaemon, "_get_stub", lambda self: captured)
    return captured


@pytest.mark.asyncio
async def test_search_forwards_path_prefix_boosts_to_proto(stub: _CapturingStub) -> None:
    daemon = SearchDaemon(target="127.0.0.1:1")

    await daemon.search(SearchRequest(query="needle", path_prefix_boosts={"/docs/": 5.0, "": 1.5}))

    assert len(stub.query_requests) == 1
    assert dict(stub.query_requests[0].path_prefix_boosts) == {"/docs/": 5.0, "": 1.5}


@pytest.mark.asyncio
async def test_search_without_boosts_sends_empty_map(stub: _CapturingStub) -> None:
    daemon = SearchDaemon(target="127.0.0.1:1")

    await daemon.search(SearchRequest(query="needle"))

    assert len(stub.query_requests) == 1
    assert dict(stub.query_requests[0].path_prefix_boosts) == {}
