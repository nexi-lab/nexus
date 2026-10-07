"""Live CLI -> Rust HTTP -> signed Search plugin, without Python server RPCs.

Uses the isolated ReBAC/mTLS fixture configured by NEXUS_SEARCH_TEST_HTTP,
NEXUS_SEARCH_TEST_ADMIN_KEY and NEXUS_SEARCH_PLUGIN_TLS_*.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

import grpc
import httpx

from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc


async def main() -> None:
    admin = Path(os.environ["NEXUS_SEARCH_TEST_ADMIN_KEY"]).read_text().strip()
    target = os.environ["NEXUS_SEARCH_PLUGIN_TARGET"]
    base = os.environ.get("NEXUS_SEARCH_TEST_HTTP", "http://127.0.0.1:2327")
    tls = grpc.ssl_channel_credentials(
        root_certificates=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CA"]).read_bytes(),
        private_key=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_KEY"]).read_bytes(),
        certificate_chain=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CERT"]).read_bytes(),
    )
    channel = grpc.aio.secure_channel(target, tls)
    vfs = vfs_pb2_grpc.NexusVFSServiceStub(channel)
    suffix = uuid4().hex[:8]
    needle = f"heliotrope{suffix}"
    paths = {name: f"/docs/cli-{name}-{suffix}.txt" for name in ("alice", "bob")}
    keys = {}
    hashes = {}

    def safe(text):
        for secret in (admin, *keys.values()):
            text = text.replace(secret, "[redacted]")
        return text

    async def cli(args, key=admin, zone="sharedzone", expected=0):
        env = os.environ.copy()
        env.update(
            {
                "NEXUS_URL": base,
                "NEXUS_API_KEY": key,
                "NEXUS_ZONE_ID": zone,
                "NEXUS_GRPC_PORT": "1",
                "NEXUS_KERNEL_BINARY": "/search-cli-must-use-http",
                "NEXUS_NO_AUTO_JSON": "1",
                "NO_COLOR": "1",
                "COLUMNS": "240",
            }
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "nexus.cli.main",
            "search",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        output = stdout.decode() + stderr.decode()
        assert process.returncode == expected, (args, process.returncode, safe(output))
        return stdout.decode(), output

    async with httpx.AsyncClient(base_url=base, timeout=20, trust_env=False) as rust:

        async def admin_request(method, url, **kwargs):
            response = await rust.request(
                method, url, headers={"Authorization": f"Bearer {admin}"}, **kwargs
            )
            assert response.is_success, (response.status_code, safe(response.text))
            return response.json()

        async def grant(name, method):
            return await admin_request(
                method,
                "/v2/rebac/tuples",
                json={
                    "zone": "sharedzone",
                    "object_type": "file",
                    "object_id": paths[name],
                    "relation": "viewer",
                    "subject_type": "user",
                    "subject_id": f"cli-{name}-{suffix}",
                },
            )

        try:
            for name, path in paths.items():
                minted = await admin_request(
                    "POST",
                    "/v2/auth/keys",
                    json={
                        "subject_type": "user",
                        "subject_id": f"cli-{name}-{suffix}",
                        "zones": ["sharedzone:r"],
                    },
                )
                keys[name], hashes[name] = minted["key"], minted["key_hash"]
                written = await vfs.Write(
                    vfs_pb2.WriteRequest(
                        path=path,
                        content=f"{needle} constellation {name}".encode(),
                        auth_token=admin,
                    )
                )
                assert not written.is_error, safe(str(written))
                await grant(name, "POST")

            # Index through the actual CLI, then consume its committed result.
            _, indexed = await cli(["index", "/docs", "--no-recursive"])
            count = re.search(r"Files indexed:\s*(\d+)", indexed)
            assert count and int(count[1]) >= 2, safe(indexed)
            hits_json, _ = await cli(["query", needle, "--mode", "keyword", "--json"])
            assert {hit["path"] for hit in json.loads(hits_json)} == set(paths.values())
            _, human = await cli(["query", needle, "--mode", "keyword", "--limit", "1"])
            assert "Found 1 results" in human and any(path in human for path in paths.values()), (
                safe(human)
            )
            _, stats_output = await cli(["stats"])
            stats = await admin_request(
                "GET", "/v2/documents/stats", params={"zone_id": "sharedzone"}
            )
            for label, field in (
                ("Indexed files", "fts_path_count"),
                ("Keyword chunks", "fts_doc_count"),
                ("Vector chunks", "ann_chunk_count"),
                ("Pending documents", "pending"),
            ):
                assert f"{label}: {stats[field]}" in stats_output, safe(stats_output)

            # Two real CLI processes use separate user credentials against one node.
            for name, (output, _) in zip(
                paths,
                await asyncio.gather(
                    *(
                        cli(["query", needle, "--mode", "keyword", "--json"], key=keys[name])
                        for name in paths
                    )
                ),
                strict=True,
            ):
                assert [hit["path"] for hit in json.loads(output)] == [paths[name]]
            narrowed, _ = await cli(
                [
                    "query",
                    needle,
                    "--mode",
                    "keyword",
                    "--path",
                    paths["bob"],
                    "--json",
                ],
                key=keys["alice"],
            )
            assert json.loads(narrowed) == []
            await cli(["stats"], key=keys["alice"], expected=77)
            await cli(["index", "/docs"], key=keys["alice"], expected=77)
            await cli(
                ["query", needle, "--mode", "keyword"], key=keys["alice"], zone="root", expected=77
            )

            # The same query must respect live relationship and key revocation.
            await grant("alice", "DELETE")
            revoked_grant, _ = await cli(
                [
                    "query",
                    needle,
                    "--mode",
                    "keyword",
                    "--json",
                ],
                key=keys["alice"],
            )
            assert json.loads(revoked_grant) == []
            await admin_request("DELETE", f"/v2/auth/keys/{hashes['bob']}")
            await cli(["query", needle, "--mode", "keyword"], key=keys["bob"], expected=77)
            await cli(["query", needle, "--mode", "keyword"], key="sk-never-minted", expected=77)
            print(
                "Rust HTTP Search CLI passed: index/query/stats/isolation/path/zone/admin/revocation"
            )
        finally:
            await channel.close()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 300))
