"""Certificate-agent Search against an isolated signed-plugin/ReBAC cluster.

Uses the existing node TLS/admin fixture and two CLI-minted certificate bundles
in NEXUS_SEARCH_TEST_AGENT_A/B. Agent calls carry their certificate alone.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import grpc
import httpx

from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.remote.rpc_transport import RPCTransport
from nexus.security.tls.config import ZoneTlsConfig


async def denied(call, code):
    try:
        await call
    except grpc.aio.AioRpcError as error:
        assert error.code() == code, error.code()
    else:
        raise AssertionError(f"Expected {code.name}")


async def main() -> None:
    target = os.environ["NEXUS_SEARCH_PLUGIN_TARGET"]
    admin = Path(os.environ["NEXUS_SEARCH_TEST_ADMIN_KEY"]).read_text().strip()
    base = os.environ.get("NEXUS_SEARCH_TEST_HTTP", "http://127.0.0.1:2327")
    suffix = uuid4().hex
    needle = f"gentian{suffix}"
    paths = {name: f"/docs/cert-{name}-{suffix}.txt" for name in ("a", "b")}
    channels = []
    agents = {}
    transports = {}

    def dial(ca, cert, key, server_name=None):
        credentials = grpc.ssl_channel_credentials(
            ca.read_bytes(), key.read_bytes(), cert.read_bytes()
        )
        options = (("grpc.ssl_target_name_override", server_name),) if server_name else ()
        channel = grpc.aio.secure_channel(target, credentials, options=options)
        channels.append(channel)
        return channel

    node = dial(
        Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CA"]),
        Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CERT"]),
        Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_KEY"]),
    )
    vfs = vfs_pb2_grpc.NexusVFSServiceStub(node)
    search = search_pb2_grpc.SearchServiceStub(node)
    try:
        for name in paths:
            bundle = Path(os.environ[f"NEXUS_SEARCH_TEST_AGENT_{name.upper()}"])
            manifest = json.loads((bundle / "credential.json").read_text())
            channel = dial(
                bundle / manifest["ca"],
                bundle / manifest["cert"],
                bundle / manifest["key"],
                manifest["server_name"],
            )
            agents[name] = (
                manifest["agent"],
                vfs_pb2_grpc.NexusVFSServiceStub(channel),
                search_pb2_grpc.SearchServiceStub(channel),
            )
            transports[name] = RPCTransport(
                target,
                tls_config=ZoneTlsConfig(
                    ca_cert_path=bundle / manifest["ca"],
                    node_cert_path=bundle / manifest["cert"],
                    node_key_path=bundle / manifest["key"],
                    known_zones_path=Path(f"/tmp/search-cert-zones-{name}-{suffix}"),
                ),
                timeout=15,
            )
            written = await vfs.Write(
                vfs_pb2.WriteRequest(
                    path=paths[name], content=f"{needle} {name}".encode(), auth_token=admin
                ),
                timeout=15,
            )
            assert not written.is_error, written
        # Admission must refuse before indexing any prefix of a mixed-zone batch.
        before = await search.Stats(
            search_pb2.StatsRequest(zone_id="sharedzone", auth_token=admin), timeout=15
        )
        for zone in ("../../agent-query-probe", "a/b", "a\\b", ".", "..", "C:escape"):
            bad = search_pb2.QueryRequest(
                q=needle, zone_id=zone, query_type=search_pb2.QUERY_TYPE_KEYWORD
            )
            await denied(agents["a"][2].Query(bad, timeout=15), grpc.StatusCode.INVALID_ARGUMENT)
            await denied(
                agents["a"][2].BatchQuery(search_pb2.BatchQueryRequest(queries=[bad]), timeout=15),
                grpc.StatusCode.INVALID_ARGUMENT,
            )
            bad.auth_token = "sk-never-minted"
            await denied(agents["a"][2].Query(bad, timeout=15), grpc.StatusCode.UNAUTHENTICATED)
        await denied(
            search.IndexDocuments(
                search_pb2.IndexDocumentsRequest(
                    zone_id="sharedzone",
                    auth_token=admin,
                    documents=[
                        search_pb2.DocumentInput(path=paths["a"], text=needle),
                        search_pb2.DocumentInput(
                            path=paths["b"], text=needle, zone_id="../../agent-query-probe"
                        ),
                    ],
                ),
                timeout=15,
            ),
            grpc.StatusCode.INVALID_ARGUMENT,
        )
        after = await search.Stats(
            search_pb2.StatsRequest(zone_id="sharedzone", auth_token=admin), timeout=15
        )
        assert after.last_index_seq == before.last_index_seq, (before, after)
        assert after.fts_doc_count == before.fts_doc_count, (before, after)
        assert after.pending == after.indexing_in_progress == 0, after
        unknown = await agents["a"][2].Query(
            search_pb2.QueryRequest(
                q=needle, zone_id=f"unindexed-{suffix}", query_type=search_pb2.QUERY_TYPE_KEYWORD
            ),
            timeout=15,
        )
        assert not unknown.error and not unknown.results, unknown
        indexed = await search.IndexDocuments(
            search_pb2.IndexDocumentsRequest(
                documents=[
                    search_pb2.DocumentInput(path=path, text=f"{needle} {name}")
                    for name, path in paths.items()
                ],
                zone_id="sharedzone",
                auth_token=admin,
            ),
            timeout=15,
        )
        assert indexed.indexed_count == 2, indexed

        def request():
            return search_pb2.QueryRequest(
                q=needle,
                query_type=search_pb2.QUERY_TYPE_KEYWORD,
                zone_id="sharedzone",
                path_filter="/docs",
                limit=10,
            )

        async def query(name, expected):
            response = await agents[name][2].Query(request(), timeout=15)
            assert not response.error, response
            assert [hit.path for hit in response.results] == expected, response

        async def sdk(name, expected):
            transport = transports[name]
            response = await asyncio.to_thread(
                transport.call_rpc,
                "semantic_search",
                {"query": needle, "search_mode": "keyword", "zone_id": "sharedzone"},
            )
            assert [hit["path"] for hit in response["results"]] == expected, response
            for method, pattern, result_key, path_key in (
                ("glob", "*.txt", "matches", None),
                ("grep", needle, "results", "file"),
            ):
                response = await asyncio.to_thread(
                    transport.call_rpc,
                    method,
                    {"path": "/docs", "pattern": pattern, "files": list(paths.values())},
                )
                found = response[result_key]
                assert (found if path_key is None else [hit[path_key] for hit in found]) == expected

        async def discovery(name, expected):
            client = agents[name][2]
            files = search_pb2.DiscoveryFiles(paths=list(paths.values()))
            glob = await client.Glob(
                search_pb2.GlobRequest(
                    root_path="/docs", pattern="*.txt", max_results=10, files=files
                ),
                timeout=15,
            )
            assert not glob.error, glob
            assert list(glob.paths) == expected, glob
            assert glob.applied_filters & search_pb2.DISCOVERY_FILTER_FILES, glob
            grep = await client.Grep(
                search_pb2.GrepRequest(
                    root_path="/docs", pattern=needle, max_results=10, files=files
                ),
                timeout=15,
            )
            assert not grep.error, grep
            assert [hit.path for hit in grep.matches] == expected, grep
            assert grep.applied_filters & search_pb2.DISCOVERY_FILTER_FILES, grep

        async with httpx.AsyncClient(base_url=base, timeout=15, trust_env=False) as http:

            async def grant(name, method):
                response = await http.request(
                    method,
                    "/v2/rebac/tuples",
                    headers={"Authorization": f"Bearer {admin}"},
                    json={
                        "zone": "sharedzone",
                        "object_type": "file",
                        "object_id": paths[name],
                        "relation": "viewer",
                        "subject_type": "agent",
                        "subject_id": agents[name][0],
                    },
                )
                assert response.is_success, response.status_code

            # A valid certificate authenticates; file access still needs the kernel's policy.
            for name in paths:
                await query(name, [])
                await discovery(name, [])
                await sdk(name, [])
                await grant(name, "POST")
            await asyncio.gather(*(query(name, [paths[name]]) for name in paths for _ in range(3)))
            for name, (_, agent_vfs, client) in agents.items():
                read = await agent_vfs.Read(vfs_pb2.ReadRequest(path=paths[name]), timeout=15)
                assert not read.is_error and needle.encode() in read.content, read
                await discovery(name, [paths[name]])
                await sdk(name, [paths[name]])
                batch = await client.BatchQuery(
                    search_pb2.BatchQueryRequest(queries=[request(), request()]), timeout=15
                )
                assert len(batch.responses) == 2, batch
                assert all(
                    not item.error and [hit.path for hit in item.results] == [paths[name]]
                    for item in batch.responses
                ), batch
                located = await client.Locate(
                    search_pb2.LocateRequest(path=paths[name], zone_id="sharedzone"), timeout=15
                )
                assert located.indexed and located.chunk_count > 0, located
                assert located.zone_id == "sharedzone", located
                # A file grant does not authorize a directory walk.
                await denied(
                    client.Glob(
                        search_pb2.GlobRequest(root_path="/docs", pattern="*.txt"), timeout=15
                    ),
                    grpc.StatusCode.PERMISSION_DENIED,
                )
                await denied(
                    client.Grep(
                        search_pb2.GrepRequest(root_path="/docs", pattern=needle), timeout=15
                    ),
                    grpc.StatusCode.PERMISSION_DENIED,
                )
                # Naming a different zone cannot make this mount readable there.
                await denied(
                    client.Locate(
                        search_pb2.LocateRequest(path=paths[name], zone_id="root"), timeout=15
                    ),
                    grpc.StatusCode.PERMISSION_DENIED,
                )
                other = "b" if name == "a" else "a"
                await denied(
                    client.Locate(
                        search_pb2.LocateRequest(path=paths[other], zone_id="sharedzone"),
                        timeout=15,
                    ),
                    grpc.StatusCode.PERMISSION_DENIED,
                )
                await denied(
                    client.Stats(search_pb2.StatsRequest(), timeout=15),
                    grpc.StatusCode.PERMISSION_DENIED,
                )
                await denied(
                    client.IndexDocuments(
                        search_pb2.IndexDocumentsRequest(zone_id="sharedzone"), timeout=15
                    ),
                    grpc.StatusCode.PERMISSION_DENIED,
                )
                await denied(
                    client.Query(
                        request(),
                        metadata=(("authorization", "Bearer sk-never-minted"),),
                        timeout=15,
                    ),
                    grpc.StatusCode.UNAUTHENTICATED,
                )

            await grant("a", "DELETE")
            await query("a", [])
            await discovery("a", [])
            await sdk("a", [])
            await denied(
                agents["a"][2].Locate(
                    search_pb2.LocateRequest(path=paths["a"], zone_id="sharedzone"), timeout=15
                ),
                grpc.StatusCode.PERMISSION_DENIED,
            )
            await query("b", [paths["b"]])
            deleted = await vfs.Delete(
                vfs_pb2.DeleteRequest(path=paths["b"], auth_token=admin), timeout=15
            )
            assert not deleted.is_error, deleted
            await query("b", [])
            await discovery("b", [])
            await sdk("b", [])
        print(
            "Agent certificate Search passed: Query/Batch/Glob/Grep/Locate/SDK/isolation/ReBAC/revocation/VFS/admin"
        )
    finally:
        for transport in transports.values():
            transport.close()
        for channel in channels:
            await channel.close()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 180))
