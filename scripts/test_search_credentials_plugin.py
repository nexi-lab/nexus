"""Live HTTP/MCP/Python RPC -> SearchDaemon -> signed plugin authorization.

Requires an isolated ReBAC-enabled cluster with /docs mounted in sharedzone,
node mTLS configured through NEXUS_SEARCH_PLUGIN_TLS_*, and an admin key file
in NEXUS_SEARCH_TEST_ADMIN_KEY. No test credentials are logged.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import grpc
import httpx
from fastapi import FastAPI

from nexus.bricks.auth.providers.static_key import StaticAPIKeyAuth
from nexus.bricks.search.daemon import SearchDaemon
from nexus.bricks.search.federated_search import FederatedSearchDispatcher
from nexus.bricks.search.search_service import SearchService
from nexus.contracts.cache_store import InMemoryCacheStore
from nexus.contracts.search_types import SearchRequest
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.lib.request_credentials import request_api_key
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.remote.rpc_transport import RPCTransport
from nexus.runtime.zone_runner import ZoneRegistry
from nexus.security.tls.config import ZoneTlsConfig
from nexus.server.api.v2.routers.search import router
from nexus.server.lifespan.vfs_grpc import VFSGrpcServicer
from nexus.server.middleware.request_credentials import RequestCredentialsMiddleware


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
    vfs = vfs_pb2_grpc.NexusVFSServiceStub(channel)
    suffix = uuid4().hex
    paths = {name: f"/docs/{name}-{suffix}.txt" for name in ("alice", "bob")}
    keys: dict[str, str] = {}
    key_hashes: dict[str, str] = {}
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
                await vfs.Write(
                    vfs_pb2.WriteRequest(path=path, content=text.encode(), auth_token=admin)
                )
                scope = request_api_key.set(admin)
                try:
                    indexed = await daemon.index_documents(
                        [{"path": path, "text": text}], zone_id="sharedzone"
                    )
                    assert indexed["indexed"] == 1, indexed
                finally:
                    request_api_key.reset(scope)
                await grant(name, "POST")

            scope = request_api_key.set(admin)
            try:
                indexed_hits = await daemon.search(
                    SearchRequest(query=needle, search_type="keyword", zone_id="sharedzone")
                )
                assert {hit.path for hit in indexed_hits} == set(paths.values()), indexed_hits
            finally:
                request_api_key.reset(scope)

            provider = StaticAPIKeyAuth(
                {
                    key: {"subject_id": f"{name}-{suffix}", "zone_id": "sharedzone"}
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
            app.state.record_store = object()
            app.state.async_read_session_factory = object()
            app.state.permission_enforcer = None
            app.state.zone_registry = zones
            app.state.subscription_manager = None
            app.state.exposed_methods = {}
            app.state.nexus_fs = Services()

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:

                async def query(name):
                    response = await client.get(
                        "/api/v2/search/query",
                        params={"q": needle, "type": "keyword"},
                        headers={"Authorization": f"Bearer {keys[name]}"},
                    )
                    assert response.is_success, response.text
                    assert [hit["path"] for hit in response.json()["results"]] == [paths[name]], (
                        response.text
                    )

                await asyncio.gather(*(query(name) for name in ("alice", "bob") * 3))
                batch = await client.post(
                    "/api/v2/search/query/batch",
                    headers={"Authorization": f"Bearer {keys['alice']}"},
                    json={"queries": [{"q": needle, "type": "keyword"}] * 2},
                )
                assert batch.is_success, batch.text
                assert all(
                    [hit["path"] for hit in result["results"]] == [paths["alice"]]
                    for result in batch.json()["queries"]
                ), batch.text

                class NoPythonPolicy:
                    def __getattr__(self, name):
                        raise AssertionError(f"HTTP discovery consulted Python policy: {name}")

                app.state.permission_enforcer = NoPythonPolicy()
                for operation, pattern in (("glob", "*.txt"), ("grep", needle)):
                    response = await client.post(
                        f"/api/v2/search/{operation}",
                        headers={"Authorization": f"Bearer {keys['bob']}"},
                        json={"pattern": pattern, "path": "/docs", "files": list(paths.values())},
                    )
                    assert response.is_success, response.text
                    items = response.json()["items"]
                    found = items if operation == "glob" else [hit["file"] for hit in items]
                    assert found == [paths["bob"]], response.text
                    empty = await client.post(
                        f"/api/v2/search/{operation}",
                        headers={"Authorization": f"Bearer {keys['bob']}"},
                        json={"pattern": pattern, "path": "/docs", "files": []},
                    )
                    assert empty.is_success and empty.json()["items"] == [], empty.text
                    denied = await client.post(
                        f"/api/v2/search/{operation}",
                        headers={"Authorization": f"Bearer {keys['bob']}"},
                        json={"pattern": pattern, "path": "/docs"},
                    )
                    assert denied.status_code == 403, denied.text

                app.state.permission_enforcer = None
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
                scope = set_request_api_key(keys["alice"])
                try:
                    async with Client(mcp) as mcp_client:
                        result = await mcp_client.call_tool(
                            "nexus_semantic_search", {"query": needle, "search_mode": "keyword"}
                        )
                        assert paths["alice"] in str(result), result
                        assert paths["bob"] not in str(result), result
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

                async with mcp_app.router.lifespan_context(mcp_app):
                    await asyncio.gather(mcp_http_search("alice"), mcp_http_search("bob"))

                class Zones:
                    async def list_accessible_zones(self, **_kwargs):
                        return ["sharedzone"]

                dispatcher = FederatedSearchDispatcher(daemon, Zones())
                scope = request_api_key.set(keys["alice"])
                try:
                    first = await dispatcher.search(
                        needle, ("user", f"alice-{suffix}"), search_type="keyword"
                    )
                    assert [hit["path"] for hit in first.results] == [paths["alice"]]
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
                    repeated = await dispatcher.search(
                        needle, ("user", f"alice-{suffix}"), search_type="keyword"
                    )
                    assert repeated.results == [], repeated
                finally:
                    request_api_key.reset(scope)
                # Python can cache identity, but kernel key revocation remains authoritative.
                await admin_request("DELETE", f"/v2/auth/keys/{key_hashes['bob']}")
                revoked = await client.get(
                    "/api/v2/search/query",
                    params={"q": needle, "type": "keyword"},
                    headers={"Authorization": f"Bearer {keys['bob']}"},
                )
                assert not revoked.is_success, revoked.text
                assert request_api_key.get() is None
                await daemon.get_health()  # Internal boot probe still uses node mTLS.
                for token in ("sk-never-minted", ""):
                    scope = request_api_key.set(token)
                    try:
                        try:
                            await daemon.search(
                                SearchRequest(
                                    query=needle, search_type="keyword", zone_id="sharedzone"
                                )
                            )
                        except grpc.aio.AioRpcError as exc:
                            assert exc.code() == grpc.StatusCode.UNAUTHENTICATED, exc.code()
                        else:
                            raise AssertionError("Invalid bearer inherited node authority")
                    finally:
                        request_api_key.reset(scope)
            print(
                "Search credentials live contract passed: HTTP/concurrency/batch/RPC/MCP/admin/revocation"
            )
        finally:
            await daemon.shutdown()
            await asyncio.to_thread(zones.stop_all)
            await channel.close()
            transport.close()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 120))
