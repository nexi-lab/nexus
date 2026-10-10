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

        async def Query(self, request, context):
            user = await self.authorize(context)
            calls.append(("Query", user, request))
            await asyncio.sleep(0)
            if request.q == "failed" or (request.q == "partial" and request.zone_id == "peerzone"):
                return search_pb2.QueryResponse(error="Backend failed")
            return search_pb2.QueryResponse(
                results=[
                    search_pb2.QueryResult(
                        path=f"/{user}.md", chunk_text="authorized", zone_id=request.zone_id
                    )
                ]
            )

        async def BatchQuery(self, request, context):
            user = await self.authorize(context)
            calls.append(("BatchQuery", user, request))
            return search_pb2.BatchQueryResponse(
                responses=[await self.Query(query, context) for query in request.queries]
            )

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
async def test_query_batch_and_locate_preserve_host_contracts(host_client):
    client, calls, _, _ = host_client
    responses = await asyncio.gather(
        *[
            client.get(
                "/api/v2/search/query",
                params={"q": "needle", "type": "keyword", "limit": 1},
                headers={"Authorization": f"Bearer sk-{name}"},
            )
            for name in ("alice", "bob") * 3
        ]
    )
    for name, response in zip(("alice", "bob") * 3, responses, strict=True):
        assert response.status_code == 200, response.text
        data = response.json()
        assert [hit["path"] for hit in data["results"]] == [f"/{name}.md"]
        assert "permission_denial_rate" not in data
        assert set(data["latency_breakdown"]) == {"total_ms"}
    assert all(request.limit == 1 for _, _, request in calls)

    batch = await client.post(
        "/api/v2/search/query/batch",
        json={
            "queries": [
                {"q": "needle", "limit": 2},
                {"limit": 0},
                {"q": "failed"},
                {"q": "needle", "graph_mode": "auto"},
            ]
        },
        headers={"Authorization": "sk-bob"},
    )
    assert batch.status_code == 200, batch.text
    entries = batch.json()["queries"]
    assert entries[0]["results"][0]["path"] == "/bob.md"
    assert entries[1]["error"]
    assert entries[2]["error"] == "Backend failed"
    assert entries[3]["error"] == "Graph search is not available"
    assert "permission_filter_ms" not in batch.json()
    _, user, request = next(call for call in calls if call[0] == "BatchQuery")
    assert user == "bob"
    assert [query.limit for query in request.queries] == [2, 10]

    single = await client.get(
        "/api/v2/search/query?q=failed", headers={"Authorization": "Bearer sk-alice"}
    )
    assert single.json()["error"] == "Backend failed"
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
async def test_federated_leg_uses_the_host_and_rejects_backend_failure(host_client):
    client, calls, _, _ = host_client
    for query in ("needle", "partial", "failed"):
        response = await client.get(
            "/api/v2/search/query",
            params={"q": query, "federated": True},
            headers={"Authorization": "Bearer sk-alice"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        if query == "failed":
            assert data["zones_searched"] == []
            assert data["zones_failed"][0]["zone_id"] == "sharedzone"
        elif query == "partial":
            assert data["zones_searched"] == ["sharedzone"]
            assert [failure["zone_id"] for failure in data["zones_failed"]] == ["peerzone"]
            assert [hit["path"] for hit in data["results"]] == ["/alice.md"]
        else:
            assert set(data["zones_searched"]) == {"sharedzone", "peerzone"}
            assert {hit["zone_qualified_path"] for hit in data["results"]} == {
                "sharedzone:/alice.md",
                "peerzone:/alice.md",
            }
    assert all(user == "alice" for _, user, _ in calls)


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
        await client.get("/api/v2/search/query?q=needle", headers={"Authorization": "sk-alice"}),
        await client.post(
            "/api/v2/search/query/batch",
            json={"queries": [{"q": "needle"}]},
            headers={"Authorization": "sk-alice"},
        ),
        await client.post(
            "/api/v2/search/locate",
            json={"path": "/alice.md"},
            headers={"Authorization": "sk-alice"},
        ),
    ]
    assert [response.status_code for response in responses] == [status] * 3


@pytest.mark.asyncio
async def test_revoked_host_key_rejects_cached_python_identity(host_client):
    client, _, state, _ = host_client
    headers = {"Authorization": "Bearer sk-alice"}
    assert (await client.get("/api/v2/search/query?q=needle", headers=headers)).status_code == 200
    state.revoked = True
    assert (await client.get("/api/v2/search/query?q=needle", headers=headers)).status_code == 401
    batch = await client.post(
        "/api/v2/search/query/batch", json={"queries": [{"q": "needle"}]}, headers=headers
    )
    assert batch.status_code == 401


@pytest.mark.asyncio
async def test_unsupported_graph_and_invalid_locate_make_no_rpc(host_client):
    client, calls, _, _ = host_client
    for mode in ("low", "high", "dual", "auto"):
        response = await client.get(
            "/api/v2/search/query",
            params={"q": "q", "graph_mode": mode},
            headers={"Authorization": "sk-alice"},
        )
        assert response.status_code == 501
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
