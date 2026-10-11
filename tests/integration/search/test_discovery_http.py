"""HTTP discovery preserves the owning Search host's decisions over TCP."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock

import grpc
import httpx
import pytest
from fastapi import FastAPI

from nexus.bricks.auth.providers.static_key import StaticAPIKeyAuth
from nexus.bricks.search.search_service import SearchService
from nexus.contracts.cache_store import InMemoryCacheStore
from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.remote.kernel_client import KernelClient
from nexus.server.api.v2.routers.search import router
from nexus.server.middleware.request_credentials import RequestCredentialsMiddleware


class Host(search_pb2_grpc.SearchServiceServicer, vfs_pb2_grpc.NexusVFSServiceServicer):
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, str]]] = []
        self.status: grpc.StatusCode | None = None
        self.denied: set[str] = set()

    def Ping(self, request: Any, context: Any) -> Any:
        return vfs_pb2.PingResponse(version="fixture")

    def paths(self, method: str, request: Any, context: Any) -> list[str]:
        metadata = dict(context.invocation_metadata())
        self.calls.append((method, request, metadata))
        if self.status is not None:
            context.abort(self.status, "Search host rejected the request")
        credential = metadata.get("authorization", "")
        if credential not in ("Bearer sk-alice", "Bearer sk-bob"):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "caller credential required")
        if credential in self.denied:
            return []
        name = credential.removeprefix("Bearer sk-")
        paths = [f"/docs/{name}-{index}.py" for index in range(5)]
        if request.HasField("files"):
            paths = [path for path in paths if path in request.files.paths]
        return paths[: request.max_results or 100]

    def Glob(self, request: Any, context: Any) -> Any:
        return search_pb2.GlobResponse(
            paths=self.paths("Glob", request, context),
            applied_filters=search_pb2.DISCOVERY_FILTER_FILES if request.HasField("files") else 0,
        )

    def Grep(self, request: Any, context: Any) -> Any:
        return search_pb2.GrepResponse(
            matches=[
                search_pb2.GrepMatch(path=path, line_number=1, line="orchid")
                for path in self.paths("Grep", request, context)
            ],
            applied_filters=search_pb2.DISCOVERY_FILTER_FILES if request.HasField("files") else 0,
        )


@pytest.fixture
def discovery_http():
    host = Host()
    with ThreadPoolExecutor(max_workers=4) as pool:
        server = grpc.server(pool)
        search_pb2_grpc.add_SearchServiceServicer_to_server(host, server)
        vfs_pb2_grpc.add_NexusVFSServiceServicer_to_server(host, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        kernel = KernelClient(server_address=f"127.0.0.1:{port}", auth_token="gateway-key")
        policy = MagicMock()
        policy.check.side_effect = AssertionError("Python must not decide Search authorization")
        policy.filter_list.side_effect = AssertionError("Python must not filter Search results")
        records = MagicMock()
        service = SearchService(
            metadata_store=kernel, permission_enforcer=policy, record_store=records
        )
        fs = MagicMock()
        fs.service.side_effect = lambda name: service if name == "search" else None
        app = FastAPI()
        app.add_middleware(RequestCredentialsMiddleware)
        app.include_router(router)
        app.state.nexus_fs = fs
        app.state.permission_enforcer = policy
        app.state.api_key = None
        app.state.auth_provider = StaticAPIKeyAuth(
            {
                f"sk-{name}": {"subject_id": name, "zone_id": "root", "is_admin": True}
                for name in ("alice", "bob")
            }
        )
        app.state.auth_cache_store = InMemoryCacheStore()
        try:
            kernel.open()
            yield app, host
            assert not policy.method_calls
            records.session_factory.assert_not_called()
        finally:
            service.close()
            kernel.close()
            server.stop(0).wait()


def found(operation: str, body: dict[str, Any]) -> list[str]:
    return body["items"] if operation == "glob" else [item["file"] for item in body["items"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grep", "glob"])
async def test_concurrent_callers_keep_credentials_and_current_host_results(
    discovery_http, operation
):
    app, host = discovery_http
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:

        async def search(name: str) -> None:
            response = await client.post(
                f"/api/v2/search/{operation}",
                headers={"Authorization": f"Bearer sk-{name}"},
                json={"pattern": "orchid", "path": "/docs", "limit": 2, "offset": 1},
            )
            assert response.status_code == 200, response.text
            assert found(operation, response.json()) == [
                f"/docs/{name}-{index}.py" for index in (1, 2)
            ]
            assert response.json()["has_more"] is True
            assert "permission_denial_rate" not in response.json()

        await asyncio.gather(*(search(name) for name in ("alice", "bob") * 3))
        assert len(host.calls) == 6
        assert {call[2]["authorization"] for call in host.calls} == {
            "Bearer sk-alice",
            "Bearer sk-bob",
        }
        assert all(not call[1].auth_token for call in host.calls)
        if operation == "grep":
            assert all(call[1].max_results == 4 for call in host.calls)
        host.denied.add("Bearer sk-alice")
        response = await client.get(
            f"/api/v2/search/{operation}?pattern=orchid",
            headers={"Authorization": "Bearer sk-alice"},
        )
        assert response.status_code == 200 and found(operation, response.json()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grep", "glob"])
async def test_empty_working_set_reaches_the_host_and_searches_nothing(discovery_http, operation):
    app, host = discovery_http
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v2/search/{operation}",
            headers={"Authorization": "Bearer sk-alice"},
            json={"pattern": "orchid", "path": "/docs", "files": []},
        )
        assert response.status_code == 200 and found(operation, response.json()) == []
        assert len(host.calls) == 1
        assert host.calls[0][1].HasField("files") and not host.calls[0][1].files.paths


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grep", "glob"])
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize(
    "status, expected",
    [(grpc.StatusCode.UNAUTHENTICATED, 401), (grpc.StatusCode.PERMISSION_DENIED, 403)],
)
async def test_http_admin_context_cannot_override_host_denial(
    discovery_http, operation, method, status, expected
):
    app, host = discovery_http
    host.status = status
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        kwargs = {"params" if method == "get" else "json": {"pattern": "orchid"}}
        response = await client.request(
            method,
            f"/api/v2/search/{operation}",
            headers={"Authorization": "Bearer sk-alice"},
            **kwargs,
        )
        assert response.status_code == expected, response.text
        assert len(host.calls) == 1
