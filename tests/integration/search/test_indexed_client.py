"""SDK and facade queries over a real TCP SearchService without generic VFS Call."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest

from nexus.bricks.search.search_service import SearchService
from nexus.contracts.types import OperationContext
from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc
from nexus.lib.request_credentials import request_api_key
from nexus.remote.kernel_client import KernelClient
from nexus.remote.rpc_transport import RPCTransport
from nexus.remote.service_proxy import RemoteServiceProxy


class Host(search_pb2_grpc.SearchServiceServicer):
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, str]]] = []
        self.status: grpc.StatusCode | None = None
        self.failures: list[grpc.StatusCode] = []
        self.query = search_pb2.QueryResponse(
            results=[
                search_pb2.QueryResult(
                    path="/docs/design.md",
                    chunk_text="marigold design",
                    score=0.987654,
                    chunk_index=2,
                    zone_id="sharedzone",
                    expanded_context="marigold document context",
                    title_score=0,
                    keyword_score=0,
                    vector_score=0.25,
                    tier_boost=1.5,
                    recency_boost=0.75,
                    expansion_variant_index=0,
                ),
                search_pb2.QueryResult(path="/docs/notes.md", score=0.5),
            ]
        )
        self.stats = search_pb2.StatsResponse(
            fts_doc_count=2,
            fts_path_count=2,
            backend="tantivy",
            last_index_seq=12,
            last_successful_index_at_ms=1000,
        )
        self.index = search_pb2.IndexResponse(indexed_count=2, skipped_count=1)

    def record(self, request: Any, context: Any) -> None:
        self.calls.append((request, dict(context.invocation_metadata())))
        if self.failures:
            context.abort(self.failures.pop(0), "host temporarily unavailable")
        if self.status is not None:
            context.abort(self.status, "caller rejected by Search host")

    def Query(self, request: Any, context: Any) -> Any:
        self.record(request, context)
        return self.query

    def Stats(self, request: Any, context: Any) -> Any:
        self.record(request, context)
        return self.stats

    def Index(self, request: Any, context: Any) -> Any:
        self.record(request, context)
        return self.index


@pytest.fixture
def indexed():
    host = Host()
    with ThreadPoolExecutor(max_workers=4) as pool:
        server = grpc.server(pool)
        search_pb2_grpc.add_SearchServiceServicer_to_server(host, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        transport = RPCTransport(f"127.0.0.1:{port}", auth_token="operator-key")
        kernel = KernelClient(server_address=f"127.0.0.1:{port}")
        kernel._transport = transport
        enforcer = MagicMock()
        records = MagicMock()
        service = SearchService(
            metadata_store=kernel,
            permission_enforcer=enforcer,
            record_store=records,
        )
        try:
            yield host, transport, RemoteServiceProxy(transport.call_rpc, "search"), service
            enforcer.assert_not_called()
            assert not enforcer.method_calls
            records.session_factory.assert_not_called()
        finally:
            service.close()
            kernel.close()
            server.stop(0).wait()


def test_sdk_queries_the_typed_host_with_exact_scope_limit_and_result_presence(indexed):
    host, _transport, search, _service = indexed
    hits = search.semantic_search(query="marigold", path="/docs", limit=2, search_mode="keyword")
    assert [hit["path"] for hit in hits] == ["/docs/design.md", "/docs/notes.md"]
    assert hits[0]["score"] == 0.9877 and hits[0]["title_score"] == 0
    assert hits[0]["chunk_index"] == 2 and "title_score" not in hits[1]
    assert hits[0]["zone_id"] == "sharedzone"
    assert hits[0]["macro_text"] == "marigold document context"
    assert hits[0]["keyword_score"] == 0 and "keyword_score" not in hits[1]
    assert hits[0]["vector_score"] == 0.25
    assert hits[0]["tier_boost"] == 1.5 and hits[0]["recency_boost"] == 0.75
    assert hits[0]["expansion_variant_index"] == 0
    assert "expansion_variant_index" not in hits[1]
    request, metadata = host.calls[-1]
    assert request.q == "marigold" and request.path_filter == "/docs" and request.limit == 2
    assert request.query_type == search_pb2.QUERY_TYPE_KEYWORD
    assert metadata["authorization"] == "Bearer operator-key" and not request.auth_token


@pytest.mark.asyncio
async def test_kernel_facade_forwards_caller_credentials_without_python_policy(indexed):
    host, _transport, _search, service = indexed
    # The context supplies a target zone. Only the transmitted credential can
    # grant authority; Python enforcers and SQL projections are not consulted.
    context = OperationContext(user_id="claimed-user", groups=[], zone_id="legal")
    for credential in ("alice-key", "bob-key", ""):
        scope = request_api_key.set(credential)
        try:
            hits = await service.semantic_search("marigold", limit=2, context=context)
        finally:
            request_api_key.reset(scope)
        assert len(hits) == 2
        request, metadata = host.calls[-1]
        assert request.zone_id == "legal" and request.limit == 2
        assert metadata["authorization"] == f"Bearer {credential}"
        assert not request.auth_token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.UNIMPLEMENTED,
    ],
)
async def test_search_host_denial_cannot_be_replaced_by_a_python_admin_context(indexed, status):
    host, _transport, _search, service = indexed
    host.status = status
    context = OperationContext(user_id="claimed-admin", groups=[], is_admin=True)
    with pytest.raises(grpc.RpcError) as error:
        await service.semantic_search("marigold", context=context)
    assert error.value.code() == status
    assert len(host.calls) == 1


def test_request_credentials_take_precedence_over_per_call_and_default_keys(indexed):
    host, transport, _search, _service = indexed
    scope = request_api_key.set("caller-key")
    try:
        transport.call_rpc("semantic_search", {"query": "marigold"}, auth_token="override-key")
    finally:
        request_api_key.reset(scope)
    assert host.calls[-1][1]["authorization"] == "Bearer caller-key"
    transport.call_rpc("semantic_search", {"query": "marigold"}, auth_token="")
    assert host.calls[-1][1]["authorization"] == "Bearer "


@pytest.mark.parametrize("credential", [None, "", "caller-key"])
def test_configured_credential_presence_is_preserved_on_the_wire(indexed, credential):
    host, transport, _search, _service = indexed
    configured = RPCTransport(transport.server_address, auth_token=credential)
    try:
        configured.call_rpc("semantic_search", {"query": "marigold"})
    finally:
        configured.close()
    metadata = host.calls[-1][1]
    if credential is None:
        assert "authorization" not in metadata
    else:
        assert metadata["authorization"] == f"Bearer {credential}"


def test_indexed_errors_and_empty_results_are_distinct(indexed):
    host, _transport, search, _service = indexed
    host.query = search_pb2.QueryResponse(error="embedding provider unavailable")
    with pytest.raises(RuntimeError, match="embedding provider unavailable"):
        search.semantic_search(query="marigold")
    host.query = search_pb2.QueryResponse()
    assert search.semantic_search(query="marigold") == []


@pytest.mark.asyncio
async def test_indexing_reads_vfs_on_the_host_and_returns_its_counts(indexed):
    host, _transport, search, service = indexed
    expected = {"indexed_count": 2, "skipped_count": 1}
    assert search.semantic_search_index("/docs", recursive=False, max_docs=7) == expected
    scope = request_api_key.set("caller-key")
    try:
        assert (
            await service.semantic_search_index(
                path="/docs",
                max_docs=7,
                context=OperationContext(user_id="caller", groups=[], zone_id="legal"),
            )
            == expected
        )
    finally:
        request_api_key.reset(scope)
    request, metadata = host.calls[-1]
    assert request.root_path == "/docs" and request.zone_id == "legal"
    assert request.max_docs == 7 and request.recursive and not request.auth_token
    assert metadata["authorization"] == "Bearer caller-key"
    host.index.error = "VFS root missing"
    with pytest.raises(RuntimeError, match="VFS root missing"):
        search.semantic_search_index(path="/absent")
    host.status = grpc.StatusCode.PERMISSION_DENIED
    before = len(host.calls)
    with pytest.raises(grpc.RpcError) as error:
        search.semantic_search_index(path="/docs")
    assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
    assert len(host.calls) == before + 1


def test_transient_search_host_failures_still_retry(indexed):
    host, _transport, search, _service = indexed
    host.failures = [grpc.StatusCode.UNAVAILABLE]
    assert len(search.semantic_search(query="marigold")) == 2
    assert len(host.calls) == 2


@pytest.mark.asyncio
async def test_statistics_use_the_same_channel_and_preserve_operational_facts(indexed):
    host, _transport, search, service = indexed
    expected = search.semantic_search_stats()
    assert await service.semantic_search_stats() == expected
    assert expected["engine"] == "tantivy"
    assert expected["fts_doc_count"] == 2 and expected["last_index_seq"] == 12
    assert expected["last_successful_index_at"] == "1970-01-01T00:00:01+00:00"
    assert expected["last_index_refresh"] == 1.0 and expected["embedding_model"] is None
    scope = request_api_key.set("alice-key")
    try:
        await service.semantic_search_stats()
    finally:
        request_api_key.reset(scope)
    assert host.calls[-1][1]["authorization"] == "Bearer alice-key"
    host.stats.error = "statistics unavailable"
    with pytest.raises(RuntimeError, match="statistics unavailable"):
        search.semantic_search_stats()


@pytest.mark.parametrize(
    "arguments",
    [
        {"search_mode": "automatic"},
        {"filters": {"owner": "alice"}},
        {"unsupported": True},
    ],
)
def test_unsupported_queries_fail_before_transport(indexed, arguments):
    host, _transport, search, _service = indexed
    with pytest.raises((ValueError, TypeError)):
        search.semantic_search(query="marigold", **arguments)
    assert not host.calls
