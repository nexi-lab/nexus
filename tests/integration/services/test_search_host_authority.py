"""Search HTTP routes use one shared gRPC channel and the host's responses."""

import asyncio
from types import SimpleNamespace

import grpc
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from nexus.bricks.auth.providers.static_key import StaticAPIKeyAuth
from nexus.bricks.search.daemon import SearchDaemon
from nexus.contracts.cache_store import InMemoryCacheStore
from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc
from nexus.lib.request_credentials import request_api_key
from nexus.runtime.zone_runner import ZoneRegistry
from nexus.server.api.v2.routers.search import router
from nexus.server.middleware.request_credentials import RequestCredentialsMiddleware

_MUTATIONS = [
    ("POST", "/api/v2/search/index", {"json": {"documents": [{"path": "/alice.md", "text": "a"}]}}),
    ("POST", "/api/v2/search/refresh", {"params": {"path": "/probe.md", "change_type": "delete"}}),
    ("POST", "/api/v2/search/index-directory", {"json": {"path": "/docs"}}),
    ("DELETE", "/api/v2/search/index-directory", {"json": {"path": "/docs"}}),
    ("GET", "/api/v2/search/indexed-dirs", {}),
    ("POST", "/api/v2/search/indexing-mode", {"json": {"mode": "on"}}),
]


async def mutation_responses(client, headers):
    return [
        await client.request(method, route, headers=headers, **kwargs)
        for method, route, kwargs in _MUTATIONS
    ]


@pytest_asyncio.fixture
async def host_client(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    calls = []
    policy_calls = []
    state = SimpleNamespace(error=None, revoked=False)

    class Policy:
        async def list_accessible_zones(self, **kwargs):
            return ["sharedzone", "peerzone"]

        def __getattr__(self, name):
            policy_calls.append(name)
            raise AssertionError(f"Python Search policy was consulted: {name}")

    class Host(search_pb2_grpc.SearchServiceServicer):
        async def authorize(self, context):
            token = dict(context.invocation_metadata()).get("authorization")
            if state.revoked:
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Key revoked")
            if state.error is not None:
                await context.abort(state.error, "Host rejected request")
            assert token in ("Bearer sk-alice", "Bearer sk-bob")
            return token.removeprefix("Bearer sk-")

        async def Health(self, request, context):
            return search_pb2.HealthResponse()

        async def Stats(self, request, context):
            user = await self.authorize(context)
            if user == "bob":
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "Diagnostic access denied")
            calls.append(("Stats", user, request))
            return search_pb2.StatsResponse(fts_doc_count=7, fts_path_count=3)

        async def manage(self, method, request, context):
            user = await self.authorize(context)
            if user == "bob":
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "Management access denied")
            calls.append((method, user, request))

        async def IndexDocuments(self, request, context):
            await self.manage("IndexDocuments", request, context)
            return search_pb2.IndexDocumentsResponse(
                indexed_count=len(request.documents), index_seq=7
            )

        async def NotifyFileChange(self, request, context):
            await self.manage("NotifyFileChange", request, context)
            return search_pb2.NotifyFileChangeResponse(status="accepted", index_seq=8)

        async def AddIndexedDirectory(self, request, context):
            await self.manage("AddIndexedDirectory", request, context)
            return search_pb2.AddIndexedDirectoryResponse(added=True)

        async def RemoveIndexedDirectory(self, request, context):
            await self.manage("RemoveIndexedDirectory", request, context)
            return search_pb2.RemoveIndexedDirectoryResponse(removed=True)

        async def ListIndexedDirectories(self, request, context):
            await self.manage("ListIndexedDirectories", request, context)
            return search_pb2.ListIndexedDirectoriesResponse()

        async def ListZoneIndexingModes(self, request, context):
            await self.manage("ListZoneIndexingModes", request, context)
            return search_pb2.ListZoneIndexingModesResponse()

        async def SetZoneIndexingMode(self, request, context):
            await self.manage("SetZoneIndexingMode", request, context)
            return search_pb2.SetZoneIndexingModeResponse()

        async def Locate(self, request, context):
            user = await self.authorize(context)
            calls.append(("Locate", user, request))
            return search_pb2.LocateResponse(
                indexed=True, chunk_count=2, mtime_ms=123, zone_id=request.zone_id
            )

    server = grpc.aio.server()
    search_pb2_grpc.add_SearchServiceServicer_to_server(Host(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    daemon = SearchDaemon(target=f"127.0.0.1:{port}")
    zones = ZoneRegistry()
    await daemon.get_health()
    app = FastAPI()
    app.add_middleware(RequestCredentialsMiddleware)
    app.include_router(router)
    app.state.api_key = "sk-internal"
    app.state.auth_provider = StaticAPIKeyAuth(
        {f"sk-{name}": {"subject_id": name, "zone_id": "sharedzone"} for name in ("alice", "bob")}
    )
    app.state.auth_cache_store = InMemoryCacheStore()
    app.state.search_daemon = daemon
    app.state.zone_registry = zones
    app.state.permission_enforcer = Policy()
    app.state.rebac_service = Policy()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            yield client, calls, state, app
        assert policy_calls == []
        assert request_api_key.get() is None
    finally:
        await daemon.shutdown()
        await asyncio.to_thread(zones.stop_all)
        await server.stop(None)


@pytest.mark.asyncio
async def test_locate_preserves_host_contracts(host_client):
    client, calls, _, _ = host_client
    located = await client.post(
        "/api/v2/search/locate",
        json={"path": "/alice.md"},
        headers={"Authorization": "Bearer sk-alice"},
    )
    assert located.status_code == 200, located.text
    assert located.json() | {"elapsed_ms": 0} == {
        "indexed": True,
        "chunk_count": 2,
        "mtime_ms": 123,
        "zone_id": "sharedzone",
        "elapsed_ms": 0,
    }
    _, user, request = calls[-1]
    assert (user, request.path, request.zone_id) == ("alice", "/alice.md", "sharedzone")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [
        (grpc.StatusCode.UNAUTHENTICATED, 401),
        (grpc.StatusCode.PERMISSION_DENIED, 403),
        (grpc.StatusCode.UNIMPLEMENTED, 501),
        (grpc.StatusCode.UNAVAILABLE, 503),
    ],
)
async def test_host_rpc_failures_keep_their_http_category(host_client, code, status):
    client, _, state, _ = host_client
    state.error = code
    responses = [
        await client.get("/api/v2/search/stats", headers={"Authorization": "sk-alice"}),
        await client.post(
            "/api/v2/search/locate",
            json={"path": "/alice.md"},
            headers={"Authorization": "sk-alice"},
        ),
    ]
    responses += await mutation_responses(client, {"Authorization": "Bearer sk-alice"})
    assert [response.status_code for response in responses] == [status] * len(responses)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,status", [("sk-alice", 200), ("sk-bob", 403), (None, 401), ("sk-invalid", 401)]
)
async def test_host_authorizes_management_without_python_policy(host_client, key, status):
    client, calls, _, _ = host_client
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    responses = await mutation_responses(client, headers)
    assert [response.status_code for response in responses] == [status] * len(responses)
    if status == 200:
        assert responses[0].json()["count"] == 1
        assert responses[0].json()["indexSeq"] == 7
        assert all(user == "alice" for _, user, _ in calls)
        assert all(
            request.zone_id == "sharedzone"
            for _, _, request in calls
            if hasattr(request, "zone_id")
        )
    else:
        assert calls == []


@pytest.mark.asyncio
async def test_revoked_host_key_rejects_management_with_cached_identity(host_client):
    client, _, state, app = host_client
    headers = {"Authorization": "Bearer sk-alice"}
    assert all(
        response.status_code == 200 for response in await mutation_responses(client, headers)
    )
    state.revoked = True
    responses = await mutation_responses(client, headers)
    assert [response.status_code for response in responses] == [401] * len(responses)
    assert app.state.search_index_inflight == 0


@pytest.mark.asyncio
async def test_revoked_host_key_rejects_cached_python_identity(host_client):
    client, _, state, _ = host_client
    headers = {"Authorization": "Bearer sk-alice"}
    assert (await client.get("/api/v2/search/stats", headers=headers)).status_code == 200
    state.revoked = True
    assert (await client.get("/api/v2/search/stats", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_stats_keeps_host_authority_and_concurrent_caller_credentials(host_client):
    client, calls, _, _ = host_client
    responses = await asyncio.gather(
        *[
            client.get("/api/v2/search/stats", headers={"Authorization": f"Bearer sk-{name}"})
            for name in ("alice", "bob") * 3
        ]
    )
    for name, response in zip(("alice", "bob") * 3, responses, strict=True):
        if name == "bob":
            assert response.status_code == 403, response.text
        else:
            assert response.status_code == 200, response.text
            assert response.json()["fts_doc_count"] == 7
            assert response.json()["zone_id"] == "sharedzone"
    assert [(method, user, request.zone_id) for method, user, request in calls] == [
        ("Stats", "alice", "sharedzone")
    ] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer invalid-key"])
async def test_stats_rejects_invalid_auth_before_using_the_node_connection(
    host_client, authorization
):
    client, calls, _, _ = host_client
    headers = {} if authorization is None else {"Authorization": authorization}
    response = await client.get("/api/v2/search/stats", headers=headers)
    assert response.status_code == 401
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_locate_makes_no_rpc(host_client):
    client, calls, _, _ = host_client
    for payload in (
        {"q": "filename"},
        {"path": "relative"},
        {"path": ""},
        {"path": "/alice.md", "limit": 10},
    ):
        response = await client.post(
            "/api/v2/search/locate", json=payload, headers={"Authorization": "sk-alice"}
        )
        assert response.status_code == 422
    assert calls == []
