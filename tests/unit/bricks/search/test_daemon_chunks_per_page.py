"""SearchDaemon must forward a per-document pooling cap to the plugin.

The deleted Python daemon pooled fused results per document
(``page_aggregation=True`` / ``chunks_per_page=2`` DaemonConfig defaults,
#4550); the P12 proxy forwarded nothing, so the plugin — which implements
pooling behind ``QueryRequest.chunks_per_page`` — always received 0 and one
long document's chunks could crowd the fused top-N (observed live: 2 docs
holding 6 of the hybrid top-12 on a 75-doc corpus, evicting a gold hit).
The proxy now resolves the same env contract once at construction and
stamps it on every single AND batch request, and exposes a ``config`` shim
so ``federated_search.daemon_pooling_cap`` agrees.
"""

from __future__ import annotations

import pytest

from nexus.bricks.search.daemon import SearchDaemon, daemon_pooling_cap
from nexus.contracts.search_types import SearchRequest
from nexus.grpc.search.v1 import search_pb2


class _CapturingStub:
    def __init__(self) -> None:
        self.query_requests: list[search_pb2.QueryRequest] = []
        self.batch_requests: list[search_pb2.BatchQueryRequest] = []

    async def Query(self, req: search_pb2.QueryRequest) -> search_pb2.QueryResponse:  # noqa: N802
        self.query_requests.append(req)
        return search_pb2.QueryResponse(results=[])

    async def BatchQuery(  # noqa: N802
        self, req: search_pb2.BatchQueryRequest
    ) -> search_pb2.BatchQueryResponse:
        self.batch_requests.append(req)
        return search_pb2.BatchQueryResponse(
            responses=[search_pb2.QueryResponse(results=[]) for _ in req.queries]
        )


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _CapturingStub:
    captured = _CapturingStub()
    monkeypatch.setattr(SearchDaemon, "_get_stub", lambda self: captured)
    return captured


@pytest.mark.asyncio
async def test_default_pooling_cap_reaches_single_query_proto(stub: _CapturingStub) -> None:
    daemon = SearchDaemon(target="127.0.0.1:1")

    await daemon.search(SearchRequest(query="needle"))

    assert len(stub.query_requests) == 1
    assert stub.query_requests[0].chunks_per_page == 2


@pytest.mark.asyncio
async def test_default_pooling_cap_reaches_every_batch_entry(stub: _CapturingStub) -> None:
    daemon = SearchDaemon(target="127.0.0.1:1")

    await daemon.batch_search([SearchRequest(query="a"), SearchRequest(query="b")])

    assert len(stub.batch_requests) == 1
    assert [q.chunks_per_page for q in stub.batch_requests[0].queries] == [2, 2]


@pytest.mark.asyncio
async def test_page_aggregation_env_kill_switch_sends_zero(
    stub: _CapturingStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXUS_SEARCH_PAGE_AGGREGATION", "false")
    daemon = SearchDaemon(target="127.0.0.1:1")

    await daemon.search(SearchRequest(query="needle"))

    assert stub.query_requests[0].chunks_per_page == 0


@pytest.mark.asyncio
async def test_chunks_per_page_env_overrides_cap(
    stub: _CapturingStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXUS_SEARCH_CHUNKS_PER_PAGE", "5")
    daemon = SearchDaemon(target="127.0.0.1:1")

    await daemon.search(SearchRequest(query="needle"))

    assert stub.query_requests[0].chunks_per_page == 5


def test_non_integer_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_SEARCH_CHUNKS_PER_PAGE", "banana")
    daemon = SearchDaemon(target="127.0.0.1:1")

    assert daemon._chunks_per_page == 2


def test_config_shim_feeds_federated_daemon_pooling_cap() -> None:
    daemon = SearchDaemon(target="127.0.0.1:1")

    assert daemon_pooling_cap(daemon) == 2


def test_config_shim_reports_pooling_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_SEARCH_PAGE_AGGREGATION", "off")
    daemon = SearchDaemon(target="127.0.0.1:1")

    assert daemon.config.page_aggregation is False
    assert daemon_pooling_cap(daemon) is None
