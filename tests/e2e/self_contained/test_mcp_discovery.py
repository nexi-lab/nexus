"""MCP discovery uses host authorization over the filesystem's TCP channel."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, cast

import grpc
import pytest
from fastmcp import Client

from nexus.bricks.mcp.server import (
    create_mcp_server,
    reset_request_api_key,
    set_request_api_key,
)
from nexus.bricks.search.search_service import SearchService
from nexus.core.nexus_fs import NexusFS
from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.remote.kernel_client import KernelClient


class DiscoveryHost(search_pb2_grpc.SearchServiceServicer, vfs_pb2_grpc.NexusVFSServiceServicer):
    def __init__(self) -> None:
        self.keys = {"alice-key", "bob-key"}
        self.grants = set(self.keys)
        self.calls: list[tuple[str, Any, dict[str, str]]] = []
        self.status: grpc.StatusCode | None = None
        self.backend_error: str | None = None
        self.acknowledge_filters = True

    def Ping(self, request: Any, context: Any) -> Any:
        return vfs_pb2.PingResponse(version="fixture")

    def _paths(self, method: str, request: Any, context: Any) -> list[str]:
        metadata = dict(context.invocation_metadata())
        self.calls.append((method, request, metadata))
        if self.status is not None:
            context.abort(self.status, "internal fixture diagnostic")
        token = metadata.get("authorization", "").removeprefix("Bearer ")
        if token not in self.keys:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "host key revoked")
        if token not in self.grants:
            return []
        owner = token.removesuffix("-key")
        paths = [f"/docs/{owner}-{i}.md" for i in range(5)]
        if request.HasField("files"):
            paths = [path for path in paths if path in request.files.paths]
        return paths

    def _filters(self, request: Any) -> int:
        if not self.acknowledge_filters:
            return 0
        flags = search_pb2.DISCOVERY_FILTER_FILES if request.HasField("files") else 0
        if hasattr(request, "block_type") and request.block_type:
            flags |= search_pb2.DISCOVERY_FILTER_BLOCK_TYPE
        if hasattr(request, "section") and request.section:
            flags |= search_pb2.DISCOVERY_FILTER_SECTION
        return flags

    def Glob(self, request: Any, context: Any) -> Any:
        response = search_pb2.GlobResponse(
            paths=self._paths("glob", request, context), applied_filters=self._filters(request)
        )
        if self.backend_error is not None:
            response.error = self.backend_error
        return response

    def Grep(self, request: Any, context: Any) -> Any:
        paths = self._paths("grep", request, context)
        response = search_pb2.GrepResponse(
            matches=[
                search_pb2.GrepMatch(path=path, line_number=3, line="orchid", before=["before"])
                for path in paths[: request.max_results]
            ],
            truncated=len(paths) > request.max_results,
            applied_filters=self._filters(request),
        )
        if self.backend_error is not None:
            response.error = self.backend_error
        return response


class CachedIdentityProvider:
    def authenticate(self, token: str) -> Any:
        if token not in ("alice-key", "bob-key"):
            return None
        return SimpleNamespace(
            authenticated=True,
            subject_type="user",
            subject_id=token,
            zone_id="team",
            zone_set=("team",),
            zone_perms=(("team", "r"),),
            is_admin=False,
        )


class SearchNexus:
    def __init__(self, search: SearchService) -> None:
        self.search = search
        self.policy_attempts: list[str] = []

    def service(self, name: str) -> Any:
        if name in ("permission_enforcer", "rebac_manager", "rebac_service"):
            self.policy_attempts.append(name)
            raise AssertionError("Python policy lookup")
        return self.search if name == "search" else None


@pytest.fixture
def discovery():
    host = DiscoveryHost()
    with ThreadPoolExecutor(max_workers=4) as pool:
        server = grpc.server(pool)
        search_pb2_grpc.add_SearchServiceServicer_to_server(host, server)
        vfs_pb2_grpc.add_NexusVFSServiceServicer_to_server(host, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        kernel = KernelClient(server_address=f"127.0.0.1:{port}", auth_token="ambient-admin")
        service = SearchService(metadata_store=kernel, enforce_permissions=False)
        nx = SearchNexus(service)
        try:
            kernel.open()
            yield host, nx
        finally:
            service.close()
            kernel.close()
            server.stop(0).wait()


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["glob", "grep"])
async def test_discovery_credentials_pagination_and_revocation(discovery, method):
    host, nx = discovery
    mcp = await create_mcp_server(nx=cast(NexusFS, nx), auth_provider=CachedIdentityProvider())

    async def invoke(token, **params):
        return await _invoke(mcp, method, token, **params)

    def paths(body):
        return body["items"] if method == "glob" else [hit["file"] for hit in body["items"]]

    alice, bob = await asyncio.gather(
        invoke("alice-key", limit=2, offset=1), invoke("bob-key", limit=2, offset=1)
    )
    for owner, result in (("alice", alice), ("bob", bob)):
        body = json.loads(result)
        assert paths(body) == [f"/docs/{owner}-1.md", f"/docs/{owner}-2.md"]
        assert body["count"] == 2 and body["has_more"] and body["next_offset"] == 3
        assert {"permission_denial_rate", "truncated_by_permissions"}.isdisjoint(body)
    assert {call[2]["authorization"] for call in host.calls} == {
        "Bearer alice-key",
        "Bearer bob-key",
    }
    if method == "grep":
        assert all(call[1].max_results == 4 for call in host.calls)

    last = json.loads(await invoke("alice-key", offset=3, limit=2))
    assert paths(last) == ["/docs/alice-3.md", "/docs/alice-4.md"]
    assert last["has_more"] is False and last["next_offset"] is None
    assert json.loads(await invoke("alice-key", files=[]))["items"] == []
    narrowed = json.loads(await invoke("alice-key", files=["/docs/alice-0.md", "/docs/bob-0.md"]))
    assert paths(narrowed) == ["/docs/alice-0.md"]
    if method == "grep":
        await invoke("alice-key", block_type="code", section="API", before_context=1)
        request = host.calls[-1][1]
        assert request.block_type == "code" and request.section == "API"
        assert request.before_context == 1

    host.grants.remove("alice-key")
    assert json.loads(await invoke("alice-key"))["items"] == []
    assert len(json.loads(await invoke("bob-key"))["items"]) == 5
    host.keys.remove("alice-key")
    rejected = await invoke("alice-key")
    assert rejected.startswith("Error:") and "Authentication required" in rejected
    assert host.calls[-1][2]["authorization"] == "Bearer alice-key"
    before = len(host.calls)
    assert (await invoke("")).startswith("Error:")
    assert len(host.calls) == before
    assert nx.policy_attempts == []


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["glob", "grep"])
async def test_discovery_failures_and_filter_acknowledgment(discovery, method):
    host, nx = discovery
    mcp = await create_mcp_server(nx=cast(NexusFS, nx), auth_provider=CachedIdentityProvider())
    scope = set_request_api_key("bob-key")
    try:

        async def invoke():
            return await _invoke(mcp, method, "bob-key", files=["/docs/bob-0.md"])

        host.acknowledge_filters = False
        failure = await invoke()
        assert failure.startswith("Error:") and "every requested discovery filter" in failure
        host.acknowledge_filters = True
        host.backend_error = "index unavailable"
        assert "index unavailable" in await invoke()
        host.backend_error = None
        for status, message in (
            (grpc.StatusCode.UNIMPLEMENTED, "not available"),
            (grpc.StatusCode.DEADLINE_EXCEEDED, "timed out"),
            (grpc.StatusCode.UNAUTHENTICATED, "Authentication required"),
            (grpc.StatusCode.PERMISSION_DENIED, "Permission denied"),
        ):
            host.status = status
            before = len(host.calls)
            failure = await invoke()
            assert failure.startswith("Error:") and message in failure
            assert "internal fixture diagnostic" not in failure
            assert len(host.calls) == before + 1
        assert nx.policy_attempts == []
    finally:
        reset_request_api_key(scope)


async def _invoke(mcp, method, token, **params):
    scope = set_request_api_key(token)
    try:
        async with Client(mcp) as client:
            result = await client.call_tool(
                f"nexus_{method}",
                {"pattern": "*" if method == "glob" else "orchid", "path": "/docs", **params},
            )
            return result.content[0].text
    finally:
        reset_request_api_key(scope)
