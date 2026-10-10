"""Live SDK/facade discovery through mTLS, a signed plugin and Raft grants.

Requires the isolated Search fixture configured by NEXUS_SEARCH_PLUGIN_TARGET,
NEXUS_SEARCH_TEST_HTTP, NEXUS_SEARCH_TEST_ADMIN_KEY and NEXUS_TLS_*.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import grpc
import httpx

from nexus.bricks.search.search_service import SearchService
from nexus.contracts.types import OperationContext
from nexus.lib.request_credentials import request_api_key
from nexus.remote.kernel_client import KernelClient
from nexus.remote.rpc_transport import RPCTransport
from nexus.remote.service_proxy import RemoteServiceProxy
from nexus.security.tls.config import ZoneTlsConfig


async def main() -> None:
    admin = Path(os.environ["NEXUS_SEARCH_TEST_ADMIN_KEY"]).read_text().strip()
    target = os.environ["NEXUS_SEARCH_PLUGIN_TARGET"]
    base = os.environ["NEXUS_SEARCH_TEST_HTTP"]
    tls = ZoneTlsConfig.from_env()
    transport = RPCTransport(target, auth_token=admin, tls_config=tls)
    search = RemoteServiceProxy(transport.call_rpc, "search")
    kernel = KernelClient(server_address=target)
    kernel._transport = transport

    class NoPythonPolicy:
        def __getattr__(self, name):
            raise AssertionError(f"Search attempted to use Python policy or SQL: {name}")

    facade = SearchService(
        metadata_store=kernel, permission_enforcer=NoPythonPolicy(), record_store=NoPythonPolicy()
    )
    suffix = uuid4().hex[:8]
    needle = f"discovery{suffix}"
    subjects = {
        name: {"subject_type": kind, "subject_id": f"discovery-{name}-{suffix}"}
        for name, kind in (("alice", "user"), ("bob", "user"))
    }
    paths = {name: f"/docs/{suffix}-{name}.md" for name in subjects}
    keys: dict[str, str] = {}
    hashes: dict[str, str] = {}
    created: list[str] = []

    def safe(value: str) -> str:
        for secret in (admin, *keys.values()):
            value = value.replace(secret, "[redacted]")
        return value

    async def rejects(call, *args, **kwargs):
        try:
            await asyncio.to_thread(call, *args, **kwargs)
        except grpc.RpcError as error:
            assert error.code() == grpc.StatusCode.UNAUTHENTICATED
        else:
            raise AssertionError("Invalid caller used the node certificate identity")

    async with httpx.AsyncClient(base_url=base, timeout=20, trust_env=False) as http:

        async def request(method: str, path: str, **kwargs):
            response = await http.request(
                method, path, headers={"Authorization": f"Bearer {admin}"}, **kwargs
            )
            assert response.is_success, (response.status_code, safe(response.text))
            return response.json()

        async def grant(name: str, method: str):
            return await request(
                method,
                "/v2/rebac/tuples",
                json={
                    "zone": "sharedzone",
                    "object_type": "file",
                    "object_id": paths[name],
                    "relation": "viewer",
                    **subjects[name],
                },
            )

        try:
            agent_key = await http.post(
                "/v2/auth/keys",
                headers={"Authorization": f"Bearer {admin}"},
                json={
                    "subject_type": "agent",
                    "subject_id": f"discovery-scode-{suffix}",
                    "zones": ["sharedzone:r"],
                },
            )
            if agent_key.is_success:
                await request("DELETE", f"/v2/auth/keys/{agent_key.json()['key_hash']}")
            assert agent_key.status_code == 400, "Agent identities must use the certificate plane"
            for name, path in paths.items():
                minted = await request(
                    "POST",
                    "/v2/auth/keys",
                    json={**subjects[name], "zones": ["sharedzone:r"]},
                )
                keys[name], hashes[name] = minted["key"], minted["key_hash"]
                text = f"# API\n{needle} prose\n```\n{needle} {name}\n```\n"
                await asyncio.to_thread(transport.write_file, path, text.encode())
                created.append(path)
                await grant(name, "POST")
                indexed = await request(
                    "POST",
                    "/v2/documents/batch",
                    json={"zone_id": "sharedzone", "documents": [{"path": path, "text": text}]},
                )
                assert indexed["indexed_count"] == 1, indexed

            indexed = await facade.semantic_search_index(
                path="/docs",
                recursive=False,
                max_docs=2,
                context=OperationContext(user_id="operator", groups=[], zone_id="sharedzone"),
            )
            assert indexed == {"indexed_count": 2, "skipped_count": 0}, indexed

            async def query(name):
                caller = request_api_key.set(keys[name])
                try:
                    hits = await asyncio.to_thread(
                        search.semantic_search, query=needle, path="/docs", search_mode="keyword"
                    )
                    assert [hit["path"] for hit in hits] == [paths[name]], hits
                    assert (
                        await facade.semantic_search(needle, path="/docs", search_mode="keyword")
                        == hits
                    )
                    claimed = OperationContext(
                        user_id="claimed-admin", groups=[], zone_id="other-zone", is_admin=True
                    )
                    try:
                        await facade.semantic_search(needle, search_mode="keyword", context=claimed)
                    except grpc.RpcError as error:
                        assert error.code() == grpc.StatusCode.PERMISSION_DENIED
                    else:
                        raise AssertionError("A Python context supplied cross-zone authority")
                    try:
                        await facade.semantic_search_stats()
                    except grpc.RpcError as error:
                        assert error.code() == grpc.StatusCode.PERMISSION_DENIED
                    else:
                        raise AssertionError("A user inherited node administrative authority")
                finally:
                    request_api_key.reset(caller)

            await asyncio.gather(*(query(name) for name in ("alice", "bob") * 3))
            stats = await facade.semantic_search_stats()
            assert stats["engine"] == stats["backend"], stats

            for name in paths:
                caller = request_api_key.set(keys[name])
                try:
                    files = await asyncio.to_thread(
                        search.glob, "*.md", path="/docs", files=list(paths.values())
                    )
                    assert files == [paths[name]], files
                    rows = await asyncio.to_thread(
                        search.grep,
                        needle,
                        path="/docs",
                        files=list(paths.values()),
                        block_type="code",
                        section="API",
                        before_context=1,
                        after_context=1,
                        max_results=1,
                    )
                    assert len(rows) == 1 and rows[0]["file"] == paths[name], rows
                    assert rows[0]["content"] == f"{needle} {name}", rows
                    assert rows[0]["line"] == 4 and rows[0]["section"]["heading"] == "API"
                    assert await facade.grep(needle, path="/docs", files=[]) == []
                finally:
                    request_api_key.reset(caller)

            changed = f"# API\n```\n{needle} revised\n```\n"
            await asyncio.to_thread(transport.write_file, paths["alice"], changed.encode())
            caller = request_api_key.set(keys["alice"])
            try:
                rows = await facade.grep(
                    needle, path="/docs", files=[paths["alice"]], block_type="code", section="API"
                )
                assert rows[0]["content"] == f"{needle} revised", rows
                assert rows[0]["line"] == 3 and rows[0]["section"]["line_end"] == 4
                await grant("alice", "DELETE")
                assert await facade.grep(needle, path="/docs", files=[paths["alice"]]) == []
                assert (
                    await facade.semantic_search(needle, path="/docs", search_mode="keyword") == []
                )
            finally:
                request_api_key.reset(caller)

            await asyncio.to_thread(transport.delete_file, paths["bob"])
            created.remove(paths["bob"])
            caller = request_api_key.set(keys["bob"])
            try:
                # The warm index still contains this document. Only the owning
                # kernel can decide that its namespace entry has been deleted.
                assert (
                    await facade.semantic_search(needle, path="/docs", search_mode="keyword") == []
                )
            finally:
                request_api_key.reset(caller)

            for invalid in ("sk-never-minted", ""):
                caller = request_api_key.set(invalid)
                try:
                    await rejects(search.grep, needle, path="/docs", files=[])
                    await rejects(search.semantic_search, query=needle, search_mode="keyword")
                finally:
                    request_api_key.reset(caller)
                await rejects(
                    transport.call_rpc,
                    "grep",
                    {"pattern": needle, "path": "/docs", "files": []},
                    auth_token=invalid,
                )
            explicit_empty = RPCTransport(target, auth_token="", tls_config=tls)
            try:
                await rejects(explicit_empty.call_rpc, "semantic_search_stats")
            finally:
                explicit_empty.close()
            print(
                "PASS: SDK/facade discovery and indexed queries, live namespace, grants and credentials"
            )
        finally:
            for name in keys:
                await grant(name, "DELETE")
            for path in created:
                await asyncio.to_thread(transport.delete_file, path)
            for key_hash in hashes.values():
                await request("DELETE", f"/v2/auth/keys/{key_hash}")
            facade.close()
            kernel.close()


if __name__ == "__main__":
    asyncio.run(main())
