"""Live native HTTP, SDK, RPC, MCP and Search management authorization.

Requires an isolated ReBAC-enabled cluster with /docs mounted in sharedzone,
node mTLS configured through NEXUS_SEARCH_PLUGIN_TLS_*, and an admin key file
in NEXUS_SEARCH_TEST_ADMIN_KEY. No test credentials are logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import grpc
import httpx
import uvicorn
from fastapi import FastAPI

from nexus.bricks.auth.providers.static_key import StaticAPIKeyAuth
from nexus.bricks.search.daemon import SearchDaemon
from nexus.bricks.search.search_service import SearchService
from nexus.contracts.cache_store import InMemoryCacheStore
from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.lib.request_credentials import request_api_key
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.remote.rpc_transport import RPCTransport
from nexus.runtime.zone_runner import ZoneRegistry
from nexus.security.tls.config import ZoneTlsConfig
from nexus.server.api.v2.routers.search import router
from nexus.server.lifespan.vfs_grpc import VFSGrpcServicer
from nexus.server.middleware.request_credentials import RequestCredentialsMiddleware


@asynccontextmanager
async def live_http_client(app):
    """Exercise the HTTP middleware and zone runner through a real TCP listener."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                        raise AssertionError("HTTP listener exited before startup")
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                timeout=20,
                trust_env=False,
            ) as client:
                yield client
        finally:
            server.should_exit = True
            await task


async def main() -> None:
    admin = Path(os.environ["NEXUS_SEARCH_TEST_ADMIN_KEY"]).read_text().strip()
    target = os.environ["NEXUS_SEARCH_PLUGIN_TARGET"]
    base = os.environ.get("NEXUS_SEARCH_TEST_HTTP", "http://127.0.0.1:2327")
    daemon = SearchDaemon(target=target)
    zones = ZoneRegistry()
    credentials = grpc.ssl_channel_credentials(
        root_certificates=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CA"]).read_bytes(),
        private_key=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_KEY"]).read_bytes(),
        certificate_chain=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CERT"]).read_bytes(),
    )
    channel = grpc.aio.secure_channel(target, credentials)
    transport = RPCTransport(
        target,
        tls_config=ZoneTlsConfig(
            ca_cert_path=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CA"]),
            node_cert_path=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CERT"]),
            node_key_path=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_KEY"]),
            known_zones_path=Path("/tmp/search-known-zones"),
        ),
    )
    search = search_pb2_grpc.SearchServiceStub(channel)
    vfs = vfs_pb2_grpc.NexusVFSServiceStub(channel)
    suffix = uuid4().hex
    paths = {name: f"/docs/{name}-{suffix}.txt" for name in ("alice", "bob")}
    keys: dict[str, str] = {}
    key_hashes: dict[str, str] = {}
    created = set()
    needle = f"marigold{suffix[:8]}"
    text = f"{needle} constellation credential isolation"

    async with httpx.AsyncClient(base_url=base, timeout=20, trust_env=False) as rust:

        async def admin_request(method, url, **kwargs):
            response = await rust.request(
                method, url, headers={"Authorization": f"Bearer {admin}"}, **kwargs
            )
            assert response.is_success, (response.status_code, response.text)
            return response.json()

        async def grant(name, method):
            await admin_request(
                method,
                "/v2/rebac/tuples",
                json={
                    "zone": "sharedzone",
                    "object_type": "file",
                    "object_id": paths[name],
                    "relation": "viewer",
                    "subject_type": "user",
                    "subject_id": f"{name}-{suffix}",
                },
            )

        try:
            baseline = await admin_request(
                "GET", "/v2/documents/stats", params={"zone_id": "sharedzone"}
            )
            for name, path in paths.items():
                minted = await admin_request(
                    "POST",
                    "/v2/auth/keys",
                    json={
                        "subject_type": "user",
                        "subject_id": f"{name}-{suffix}",
                        "zones": ["sharedzone:r"],
                    },
                )
                keys[name] = minted["key"]
                key_hashes[name] = minted["key_hash"]
                written = await vfs.Write(
                    vfs_pb2.WriteRequest(path=path, content=text.encode(), auth_token=admin)
                )
                assert not written.is_error, written
                created.add(path)
                scope = request_api_key.set(admin)
                try:
                    indexed = await daemon.index_documents(
                        [{"path": path, "text": text}], zone_id="sharedzone"
                    )
                    assert indexed["indexed"] == 1, indexed
                finally:
                    request_api_key.reset(scope)
                await grant(name, "POST")

            indexed = await search.Query(
                search_pb2.QueryRequest(
                    q=needle,
                    query_type=search_pb2.QUERY_TYPE_KEYWORD,
                    zone_id="sharedzone",
                    limit=10,
                ),
                metadata=(("authorization", f"Bearer {admin}"),),
                timeout=20,
            )
            assert not indexed.HasField("error"), indexed.error
            assert {hit.path for hit in indexed.results} == set(paths.values()), indexed

            provider = StaticAPIKeyAuth(
                {
                    key: {
                        "subject_type": "user",
                        "subject_id": f"{name}-{suffix}",
                        "zone_id": "sharedzone",
                    }
                    for name, key in keys.items()
                }
            )
            service = SearchService(metadata_store=transport)

            class Services:
                def service(self, name):
                    return service if name == "search" else None

            app = FastAPI()
            app.add_middleware(RequestCredentialsMiddleware)
            app.include_router(router)
            app.state.api_key = admin
            app.state.auth_provider = provider
            app.state.auth_cache_store = InMemoryCacheStore()
            app.state.search_daemon = daemon
            policy_calls = []

            class NoPythonPolicy:
                def __getattr__(self, name):
                    policy_calls.append(name)
                    raise AssertionError(f"HTTP Search consulted Python policy: {name}")

            app.state.permission_enforcer = NoPythonPolicy()
            app.state.zone_registry = zones
            app.state.subscription_manager = None
            app.state.exposed_methods = {}
            app.state.nexus_fs = Services()

            async with live_http_client(app) as client:
                health = await client.get("/api/v2/search/health")
                assert health.is_success, health.text
                assert health.json()["status"] in ("healthy", "degraded"), health.text
                assert health.json()["initialized"] is True, health.text
                assert health.json()["fts_writer_faults"] == 0, health.text
                assert health.json()["fts_writer_unavailable"] == 0, health.text

                async def stats(credential, expected_status):
                    headers = {"X-Nexus-Zone-ID": "sharedzone"}
                    if credential is not None:
                        headers["Authorization"] = f"Bearer {credential}"
                    response = await client.get("/api/v2/search/stats", headers=headers)
                    assert response.status_code == expected_status, response.text
                    if expected_status == 200:
                        assert response.json()["zone_id"] == "sharedzone", response.text
                        assert response.json()["fts_path_count"] == baseline[
                            "fts_path_count"
                        ] + len(paths), response.text

                await asyncio.gather(
                    *[
                        stats(credential, status)
                        for credential, status in (
                            (admin, 200),
                            (None, 401),
                            (keys["alice"], 403),
                            (keys["bob"], 403),
                            ("sk-never-minted", 401),
                        )
                        * 3
                    ]
                )

                scope_path = f"/docs/probe-{suffix}"
                management = (
                    (
                        "POST",
                        "/api/v2/search/index",
                        {"json": {"documents": [{"path": paths["alice"], "text": text}]}},
                    ),
                    (
                        "POST",
                        "/api/v2/search/refresh",
                        {"params": {"path": f"{scope_path}.txt", "change_type": "delete"}},
                    ),
                    ("POST", "/api/v2/search/index-directory", {"json": {"path": scope_path}}),
                    ("GET", "/api/v2/search/indexed-dirs", {}),
                    ("POST", "/api/v2/search/indexing-mode", {"json": {"mode": "on"}}),
                    ("DELETE", "/api/v2/search/index-directory", {"json": {"path": scope_path}}),
                )

                async def manage(method, route, kwargs, credential, status):
                    headers = {"X-Nexus-Zone-ID": "sharedzone"}
                    if credential is not None:
                        headers["Authorization"] = f"Bearer {credential}"
                    response = await client.request(method, route, headers=headers, **kwargs)
                    assert response.status_code == status, (method, route, response.text)
                    if status == 200 and route.endswith("/index"):
                        assert response.json()["count"] == 1, response.text
                        assert response.json()["indexSeq"] > 0, response.text
                    if status == 200 and route.endswith("/indexed-dirs"):
                        assert scope_path in response.json()["directories"], response.text
                        assert response.json()["zone_id"] == "sharedzone", response.text

                for method, route, kwargs in management:
                    await asyncio.gather(
                        *[
                            manage(method, route, kwargs, credential, status)
                            for credential, status in (
                                (admin, 200),
                                (keys["alice"], 403),
                                (keys["bob"], 403),
                                (None, 401),
                                ("sk-invalid", 401),
                            )
                        ]
                    )

                async def query(name):
                    response = await rust.post(
                        "/v2/search/query",
                        json={"q": needle, "query_type": "keyword"},
                        headers={"Authorization": f"Bearer {keys[name]}"},
                    )
                    assert response.is_success, response.text
                    assert [hit["path"] for hit in response.json()["results"]] == [paths[name]], (
                        response.text
                    )

                await asyncio.gather(*(query(name) for name in ("alice", "bob") * 3))
                batch = await rust.post(
                    "/v2/search/query/batch",
                    headers={"Authorization": f"Bearer {keys['alice']}"},
                    json={"queries": [{"q": needle, "query_type": "keyword"}] * 2},
                )
                assert batch.is_success, batch.text
                assert all(
                    [hit["path"] for hit in result["results"]] == [paths["alice"]]
                    for result in batch.json()["responses"]
                ), batch.text

                async def discover(name, operation, pattern, *, allowed=True):
                    response = await rust.post(
                        f"/v2/search/{operation}",
                        headers={"Authorization": f"Bearer {keys[name]}"},
                        json={
                            "pattern": pattern,
                            "root_path": "/docs",
                            "files": list(paths.values()),
                        },
                    )
                    assert response.is_success, response.text
                    body = response.json()
                    assert not body.get("error"), response.text
                    found = (
                        body["paths"]
                        if operation == "glob"
                        else [hit["path"] for hit in body["matches"]]
                    )
                    assert found == ([paths[name]] if allowed else []), response.text

                await asyncio.gather(
                    *[
                        discover(name, operation, pattern)
                        for name in ("alice", "bob") * 3
                        for operation, pattern in (("glob", "*.txt"), ("grep", needle))
                    ]
                )
                for operation, pattern in (("glob", "*.txt"), ("grep", needle)):
                    result_field = "paths" if operation == "glob" else "matches"
                    empty = await rust.post(
                        f"/v2/search/{operation}",
                        headers={"Authorization": f"Bearer {keys['bob']}"},
                        json={"pattern": pattern, "root_path": "/docs", "files": []},
                    )
                    assert empty.is_success and empty.json()[result_field] == [], empty.text
                    for method in ("GET", "POST"):
                        kwargs = {
                            "params" if method == "GET" else "json": {
                                "pattern": pattern,
                                "root_path": "/docs",
                            }
                        }
                        denied = await rust.request(
                            method,
                            f"/v2/search/{operation}",
                            headers={"Authorization": f"Bearer {keys['bob']}"},
                            **kwargs,
                        )
                        assert denied.status_code == 403, denied.text
                        walked = await rust.request(
                            method,
                            f"/v2/search/{operation}",
                            headers={"Authorization": f"Bearer {admin}"},
                            **kwargs,
                        )
                        assert walked.is_success, walked.text
                        assert not walked.json().get("error"), walked.text
                        visible = (
                            walked.json()["paths"]
                            if operation == "glob"
                            else [hit["path"] for hit in walked.json()["matches"]]
                        )
                        assert set(paths.values()) <= set(visible), walked.text
                        for invalid in (None, "sk-never-minted"):
                            headers = {"Authorization": f"Bearer {invalid}"} if invalid else {}
                            rejected = await rust.request(
                                method, f"/v2/search/{operation}", headers=headers, **kwargs
                            )
                            assert rejected.status_code == 401, rejected.text

                from nexus.server.dependencies import _get_cached_auth

                cached_identity = await _get_cached_auth(app.state.auth_cache_store, keys["alice"])
                assert cached_identity and cached_identity["subject_id"] == f"alice-{suffix}"
                assert keys["alice"] not in repr(cached_identity)

                # A real Python gRPC service dispatch shares the same daemon.
                servicer = VFSGrpcServicer(app)
                indexed_rpc = await servicer.Call(
                    vfs_pb2.CallRequest(
                        method="semantic_search_index",
                        auth_token=admin,
                        payload=encode_rpc_message(
                            {
                                "path": "/zone/sharedzone/docs",
                                "recursive": False,
                                "max_docs": 2,
                            }
                        ),
                    ),
                    SimpleNamespace(peer=lambda: "ipv4:127.0.0.1:1"),
                )
                assert not indexed_rpc.is_error, decode_rpc_message(indexed_rpc.payload)
                assert decode_rpc_message(indexed_rpc.payload)["result"] == {
                    "indexed_count": 2,
                    "skipped_count": 0,
                }
                denied_index = await servicer.Call(
                    vfs_pb2.CallRequest(
                        method="semantic_search_index",
                        auth_token=keys["bob"],
                        payload=encode_rpc_message({"path": "/docs"}),
                    ),
                    SimpleNamespace(peer=lambda: "ipv4:127.0.0.1:1"),
                )
                from nexus.contracts.rpc_types import RPCErrorCode

                assert denied_index.is_error
                assert (
                    decode_rpc_message(denied_index.payload)["code"]
                    == RPCErrorCode.PERMISSION_ERROR.value
                )
                rpc = await servicer.Call(
                    vfs_pb2.CallRequest(
                        method="semantic_search",
                        auth_token=keys["bob"],
                        payload=encode_rpc_message({"query": needle, "search_mode": "keyword"}),
                    ),
                    SimpleNamespace(peer=lambda: "ipv4:127.0.0.1:1"),
                )
                assert not rpc.is_error, decode_rpc_message(rpc.payload)
                assert [
                    hit["path"] for hit in decode_rpc_message(rpc.payload)["result"]["results"]
                ] == [paths["bob"]]
                for operation, pattern, result_key in (
                    ("glob", "*.txt", "matches"),
                    ("grep", needle, "results"),
                ):
                    rpc = await servicer.Call(
                        vfs_pb2.CallRequest(
                            method=operation,
                            auth_token=keys["bob"],
                            payload=encode_rpc_message(
                                {
                                    "pattern": pattern,
                                    "path": "/docs",
                                    "files": list(paths.values()),
                                }
                            ),
                        ),
                        SimpleNamespace(peer=lambda: "ipv4:127.0.0.1:1"),
                    )
                    assert not rpc.is_error, decode_rpc_message(rpc.payload)
                    items = decode_rpc_message(rpc.payload)["result"][result_key]
                    found = items if operation == "glob" else [hit["file"] for hit in items]
                    assert found == [paths["bob"]], items

                from fastmcp import Client

                from nexus.bricks.mcp.server import (
                    create_mcp_server,
                    reset_request_api_key,
                    set_request_api_key,
                )

                mcp = await create_mcp_server(nx=app.state.nexus_fs, auth_provider=provider)

                async def mcp_discovery(mcp_client, name, *, allowed=True):
                    for operation, pattern in (("glob", "*.txt"), ("grep", needle)):
                        result = await mcp_client.call_tool(
                            f"nexus_{operation}",
                            {"pattern": pattern, "path": "/docs", "files": list(paths.values())},
                        )
                        body = json.loads(result.content[0].text)
                        found = (
                            body["items"]
                            if operation == "glob"
                            else [hit["file"] for hit in body["items"]]
                        )
                        assert found == ([paths[name]] if allowed else []), body
                        assert {"permission_denial_rate", "truncated_by_permissions"}.isdisjoint(
                            body
                        )

                scope = set_request_api_key(keys["alice"])
                try:
                    async with Client(mcp) as mcp_client:
                        result = await mcp_client.call_tool(
                            "nexus_semantic_search", {"query": needle, "search_mode": "keyword"}
                        )
                        assert paths["alice"] in str(result), result
                        assert paths["bob"] not in str(result), result
                        await mcp_discovery(mcp_client, "alice")
                finally:
                    reset_request_api_key(scope)

                from fastmcp.client.transports import StreamableHttpTransport

                mcp_app = mcp.http_app(stateless_http=True, json_response=True)

                def mcp_http_client(**kwargs):
                    return httpx.AsyncClient(transport=httpx.ASGITransport(mcp_app), **kwargs)

                async def mcp_http_search(name):
                    transport = StreamableHttpTransport(
                        "http://localhost/mcp",
                        headers={"Authorization": f"Bearer {keys[name]}"},
                        httpx_client_factory=mcp_http_client,
                    )
                    async with Client(transport) as mcp_client:
                        result = await mcp_client.call_tool(
                            "nexus_semantic_search", {"query": needle, "search_mode": "keyword"}
                        )
                        assert paths[name] in str(result), result
                        other = "bob" if name == "alice" else "alice"
                        assert paths[other] not in str(result), result
                        await mcp_discovery(mcp_client, name)

                async with mcp_app.router.lifespan_context(mcp_app):
                    await asyncio.gather(mcp_http_search("alice"), mcp_http_search("bob"))

                federated = await rust.post(
                    "/v2/search/query",
                    json={"q": needle, "query_type": "keyword"},
                    headers={"Authorization": f"Bearer {keys['alice']}"},
                )
                assert federated.is_success, federated.text
                assert [hit["path"] for hit in federated.json()["results"]] == [paths["alice"]]

                for name in keys:
                    located = await client.post(
                        "/api/v2/search/locate",
                        json={"path": paths[name]},
                        headers={"Authorization": f"Bearer {keys[name]}"},
                    )
                    assert located.is_success and located.json()["indexed"], located.text
                    assert located.json()["chunk_count"] > 0, located.text
                    assert 0 <= located.json()["elapsed_ms"] < 2000, located.text
                    other = "bob" if name == "alice" else "alice"
                    denied = await client.post(
                        "/api/v2/search/locate",
                        json={"path": paths[other]},
                        headers={"Authorization": f"Bearer {keys[name]}"},
                    )
                    assert denied.status_code == 403, denied.text

                scope = request_api_key.set(keys["alice"])
                try:
                    first = await rust.post(
                        "/v2/search/query",
                        json={"q": needle, "query_type": "keyword"},
                        headers={"Authorization": f"Bearer {keys['alice']}"},
                    )
                    assert first.is_success, first.text
                    assert [hit["path"] for hit in first.json()["results"]] == [paths["alice"]]
                    for call in (
                        daemon.get_stats,
                        lambda: daemon.index_documents([], zone_id="sharedzone"),
                    ):
                        try:
                            await call()
                        except grpc.aio.AioRpcError as exc:
                            assert exc.code() == grpc.StatusCode.PERMISSION_DENIED, exc.code()
                        else:
                            raise AssertionError("User inherited node administrative authority")
                    await grant("alice", "DELETE")
                    await discover("alice", "glob", "*.txt", allowed=False)
                    await discover("alice", "grep", needle, allowed=False)
                    async with Client(mcp) as mcp_client:
                        await mcp_discovery(mcp_client, "alice", allowed=False)
                    repeated = await rust.post(
                        "/v2/search/query",
                        json={"q": needle, "query_type": "keyword"},
                        headers={"Authorization": f"Bearer {keys['alice']}"},
                    )
                    assert repeated.is_success, repeated.text
                    assert repeated.json()["results"] == [], repeated
                    for route, body in (
                        (
                            "/v2/search/query/batch",
                            {"queries": [{"q": needle, "query_type": "keyword"}]},
                        ),
                        ("/api/v2/search/locate", {"path": paths["alice"]}),
                    ):
                        response = await (rust if route.endswith("batch") else client).post(
                            route,
                            json=body,
                            headers={"Authorization": f"Bearer {keys['alice']}"},
                        )
                        if route.endswith("batch"):
                            assert (
                                response.is_success
                                and response.json()["responses"][0]["results"] == []
                            ), response.text
                        else:
                            assert response.status_code == 403, response.text
                    response = await rust.post(
                        "/v2/search/query",
                        json={"q": needle, "query_type": "keyword"},
                        headers={"Authorization": f"Bearer {keys['alice']}"},
                    )
                    assert response.is_success and response.json()["results"] == [], response.text
                finally:
                    request_api_key.reset(scope)
                # Python can cache identity, but kernel key revocation remains authoritative.
                await admin_request("DELETE", f"/v2/auth/keys/{key_hashes['bob']}")
                for operation, pattern in (("glob", "*.txt"), ("grep", needle)):
                    rejected = await rust.post(
                        f"/v2/search/{operation}",
                        headers={"Authorization": f"Bearer {keys['bob']}"},
                        json={"pattern": pattern, "root_path": "/docs", "files": []},
                    )
                    assert rejected.status_code == 401, rejected.text
                key_hashes.pop("bob")
                revoked = await rust.post(
                    "/v2/search/query",
                    json={"q": needle, "query_type": "keyword"},
                    headers={"Authorization": f"Bearer {keys['bob']}"},
                )
                assert revoked.status_code == 401, revoked.text
                revoked_batch = await rust.post(
                    "/v2/search/query/batch",
                    json={"queries": [{"q": needle, "query_type": "keyword"}]},
                    headers={"Authorization": f"Bearer {keys['bob']}"},
                )
                assert revoked_batch.status_code == 401, revoked_batch.text
                await stats(keys["bob"], 401)
                for method, route, kwargs in management:
                    await manage(method, route, kwargs, keys["bob"], 401)
                scope = set_request_api_key(keys["bob"])
                try:
                    async with Client(mcp) as mcp_client:
                        for operation, pattern in (("glob", "*.txt"), ("grep", needle)):
                            rejected = await mcp_client.call_tool(
                                f"nexus_{operation}",
                                {
                                    "pattern": pattern,
                                    "path": "/docs",
                                    "files": list(paths.values()),
                                },
                            )
                            assert "Authentication required" in rejected.content[0].text, rejected
                finally:
                    reset_request_api_key(scope)
                assert policy_calls == [], policy_calls
                assert request_api_key.get() is None
                await daemon.get_health()  # Internal boot probe still uses node mTLS.
                for token in ("sk-never-minted", ""):
                    try:
                        await search.Query(
                            search_pb2.QueryRequest(
                                q=needle,
                                query_type=search_pb2.QUERY_TYPE_KEYWORD,
                                zone_id="sharedzone",
                                limit=10,
                            ),
                            metadata=(("authorization", f"Bearer {token}"),),
                            timeout=20,
                        )
                    except grpc.aio.AioRpcError as exc:
                        assert exc.code() == grpc.StatusCode.UNAUTHENTICATED, exc.code()
                    else:
                        raise AssertionError("Invalid bearer inherited node authority")
            print(
                "Search credentials live contract passed: TCP HTTP/user/concurrency/batch/locate/RPC/MCP/admin/management/revocation"
            )
        finally:
            try:
                for name in keys:
                    await grant(name, "DELETE")
                scope = request_api_key.set(admin)
                try:
                    for path in created:
                        removed = await vfs.Delete(
                            vfs_pb2.DeleteRequest(path=path, auth_token=admin), timeout=15
                        )
                        assert not removed.is_error, removed
                        await daemon.notify_file_change(path, "delete", zone_id="sharedzone")
                finally:
                    request_api_key.reset(scope)
                for key_hash in key_hashes.values():
                    await admin_request("DELETE", f"/v2/auth/keys/{key_hash}")
            finally:
                await daemon.shutdown()
                await asyncio.to_thread(zones.stop_all)
                await channel.close()
                transport.close()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 120))
