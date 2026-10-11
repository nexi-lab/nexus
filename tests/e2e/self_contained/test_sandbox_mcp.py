"""MCP preserves typed Search results and failures over a real TCP channel."""

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast
from unittest.mock import MagicMock

import grpc
import pytest

from nexus.bricks.mcp.server import create_mcp_server
from nexus.bricks.search.search_service import SearchService
from nexus.core.nexus_fs import NexusFS
from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.remote.kernel_client import KernelClient


class SearchNexus:
    def __init__(self, search: SearchService) -> None:
        self.search = search

    def service(self, name: str) -> Any:
        return self.search if name == "search" else None


class SearchHost(search_pb2_grpc.SearchServiceServicer, vfs_pb2_grpc.NexusVFSServiceServicer):
    def __init__(self) -> None:
        self.response = search_pb2.QueryResponse(
            results=[search_pb2.QueryResult(path="/notes.md", chunk_text="orchid", score=0.8)]
        )
        self.status: grpc.StatusCode | None = None
        self.calls: list[tuple[Any, dict[str, str]]] = []

    def Ping(self, request: Any, context: Any) -> Any:
        return vfs_pb2.PingResponse(version="fixture")

    def Query(self, request: Any, context: Any) -> Any:
        self.calls.append((request, dict(context.invocation_metadata())))
        if self.status is not None:
            context.abort(self.status, "internal fixture diagnostic")
        return self.response


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["sandbox", "full"])
async def test_mcp_search_preserves_plugin_results_and_errors(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setenv("NEXUS_PROFILE", profile)
    host = SearchHost()
    with ThreadPoolExecutor(max_workers=2) as pool:
        server = grpc.server(pool)
        search_pb2_grpc.add_SearchServiceServicer_to_server(host, server)
        vfs_pb2_grpc.add_NexusVFSServiceServicer_to_server(host, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        kernel = KernelClient(server_address=f"127.0.0.1:{port}", auth_token="mcp-key")
        records = MagicMock()
        service = SearchService(
            metadata_store=kernel, record_store=records, enforce_permissions=False
        )
        try:
            kernel.open()
            mcp = await create_mcp_server(nx=cast(NexusFS, SearchNexus(service)))
            tool = await mcp.get_tool("nexus_semantic_search")
            assert tool is not None
            response = json.loads(await tool.fn(query="orchid", limit=5, search_mode="hybrid"))
            assert [item["path"] for item in response["items"]] == ["/notes.md"]
            assert "semantic_degraded" not in response
            request, metadata = host.calls[-1]
            assert request.query_type == search_pb2.QUERY_TYPE_HYBRID
            assert metadata["authorization"] == "Bearer mcp-key"

            host.response = search_pb2.QueryResponse()
            empty = json.loads(await tool.fn(query="absent"))
            assert empty["items"] == []
            host.response.error = "embedding provider unavailable"
            failure = await tool.fn(query="orchid")
            assert failure.startswith("Error:"), failure
            assert "embedding provider unavailable" in failure

            for status, message in (
                (grpc.StatusCode.UNIMPLEMENTED, "not available"),
                (grpc.StatusCode.DEADLINE_EXCEEDED, "timed out"),
                (grpc.StatusCode.UNAUTHENTICATED, "Authentication required"),
                (grpc.StatusCode.PERMISSION_DENIED, "Permission denied"),
            ):
                host.status = status
                before = len(host.calls)
                failure = await tool.fn(query="orchid")
                assert failure.startswith("Error:") and message in failure
                assert "internal fixture diagnostic" not in failure
                assert "_InactiveRpcError" not in failure
                assert len(host.calls) == before + 1
            records.session_factory.assert_not_called()
        finally:
            service.close()
            kernel.close()
            server.stop(0).wait()
