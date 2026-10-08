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
from nexus.lib.request_credentials import request_api_key
from nexus.remote.rpc_transport import RPCTransport
from nexus.remote.service_proxy import RemoteServiceProxy
from nexus.security.tls.config import ZoneTlsConfig


async def main() -> None:
    admin = Path(os.environ["NEXUS_SEARCH_TEST_ADMIN_KEY"]).read_text().strip()
    target = os.environ["NEXUS_SEARCH_PLUGIN_TARGET"]
    base = os.environ["NEXUS_SEARCH_TEST_HTTP"]
    transport = RPCTransport(target, auth_token=admin, tls_config=ZoneTlsConfig.from_env())
    search = RemoteServiceProxy(transport.call_rpc, "search")
    facade = SearchService(metadata_store=transport, enforce_permissions=False)
    suffix = uuid4().hex[:8]
    needle = f"discovery{suffix}"
    paths = {name: f"/docs/{suffix}-{name}.md" for name in ("alice", "bob")}
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
                    "subject_type": "user",
                    "subject_id": f"discovery-{name}-{suffix}",
                },
            )

        try:
            for name, path in paths.items():
                minted = await request(
                    "POST",
                    "/v2/auth/keys",
                    json={
                        "subject_type": "user",
                        "subject_id": f"discovery-{name}-{suffix}",
                        "zones": ["sharedzone:r"],
                    },
                )
                keys[name], hashes[name] = minted["key"], minted["key_hash"]
                text = f"# API\n{needle} prose\n```\n{needle} {name}\n```\n"
                await asyncio.to_thread(transport.write_file, path, text.encode())
                created.append(path)
                await grant(name, "POST")

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
            finally:
                request_api_key.reset(caller)

            for invalid in ("sk-never-minted", ""):
                caller = request_api_key.set(invalid)
                try:
                    await rejects(search.grep, needle, path="/docs", files=[])
                finally:
                    request_api_key.reset(caller)
                await rejects(
                    transport.call_rpc,
                    "grep",
                    {"pattern": needle, "path": "/docs", "files": []},
                    auth_token=invalid,
                )
            print(
                "PASS: SDK/facade discovery, working sets, Markdown, live edits, grants and credentials"
            )
        finally:
            for name in keys:
                await grant(name, "DELETE")
            for path in created:
                await asyncio.to_thread(transport.delete_file, path)
            for key_hash in hashes.values():
                await request("DELETE", f"/v2/auth/keys/{key_hash}")
            facade.close()
            transport.close()


if __name__ == "__main__":
    asyncio.run(main())
