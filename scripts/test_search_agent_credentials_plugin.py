"""Certificate-agent Search against an isolated signed-plugin/ReBAC cluster.

Uses the existing node TLS/admin fixture and two CLI-minted certificate bundles
in NEXUS_SEARCH_TEST_AGENT_A/B. Agent calls carry their certificate alone.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import grpc
import httpx

import nexus
from nexus.contracts.exceptions import NexusPermissionError
from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.lib.request_credentials import request_api_key
from nexus.remote.rpc_transport import RPCTransport


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
    filesystems = {}
    mcp_servers = {}

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
            client_env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("NEXUS_") or key == "NEXUS_KERNEL_BINARY"
            }
            client_env.update(
                NEXUS_GRPC_PORT=target.rsplit(":", 1)[1],
                NEXUS_GRPC_TLS="true",
                NEXUS_TLS_CA=str(bundle / manifest["ca"]),
                NEXUS_TLS_CERT=str(bundle / manifest["cert"]),
                NEXUS_TLS_KEY=str(bundle / manifest["key"]),
            )
            with patch.dict(os.environ, client_env, clear=True):
                filesystem = await asyncio.to_thread(
                    nexus.connect,
                    {
                        "profile": "remote",
                        "url": f"https://{target}",
                        "timeout": 15,
                        "zone_id": "sharedzone",
                    },
                )
            filesystems[name] = filesystem
            from nexus.bricks.mcp.server import create_mcp_server

            mcp_servers[name] = await create_mcp_server(
                nx=filesystem, remote_url=f"grpcs://{target}"
            )
            transports[name] = filesystem._nexus_remote_call_rpc.__self__
            assert isinstance(transports[name], RPCTransport)
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
        for zone in (
            "../../agent-query-probe",
            "a/b",
            "a\\b",
            ".",
            "..",
            "C:escape",
            ".. ",
            "zone.",
            "zone ",
        ):
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

        async def mcp_call(name, method, params, credential=None):
            from fastmcp import Client

            scope = request_api_key.set(credential)
            try:
                with patch("nexus.connect", side_effect=AssertionError("unexpected connection")):
                    async with Client(mcp_servers[name]) as client:
                        result = await client.call_tool(f"nexus_{method}", params)
                        return result.content[0].text
            finally:
                request_api_key.reset(scope)

        async def mcp_read(name, path, credential=None):
            return await mcp_call(name, "read_file", {"path": path}, credential)

        async def sdk(name, expected):
            filesystem = filesystems[name]
            response = await asyncio.to_thread(
                filesystem.semantic_search,
                query=needle,
                search_mode="keyword",
            )
            assert [hit["path"] for hit in response] == expected, response
            for method, pattern, path_key in (
                ("glob", "*.txt", None),
                ("grep", needle, "file"),
            ):
                response = await asyncio.to_thread(
                    getattr(filesystem, method),
                    pattern=pattern,
                    path="/docs",
                    files=list(paths.values()),
                )
                found = response
                assert (found if path_key is None else [hit[path_key] for hit in found]) == expected

            # Use the public MCP transport atop the remote SDK, including its
            # configured zone, certificate and sync Search proxy.
            from fastmcp import Client

            async with Client(mcp_servers[name]) as mcp_client:
                result = await mcp_client.call_tool(
                    "nexus_semantic_search",
                    {"query": needle, "path": "/docs", "search_mode": "keyword"},
                )
                body = json.loads(result.content[0].text)
                assert [hit["path"] for hit in body["items"]] == expected, body
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
                    assert found == expected, body
            content = await mcp_read(name, paths[name])
            if expected:
                assert content == f"{needle} {name}", "MCP certificate file read failed"
            else:
                assert content.startswith("Error:"), "MCP file access ignored revocation"
                assert "rebac:" not in content, "MCP exposed a policy diagnostic"
            metadata = await mcp_call(name, "file_info", {"path": paths[name]})
            if expected:
                info = json.loads(metadata)
                assert info["size"] == len(f"{needle} {name}".encode())
                assert info["exists"] and not info["is_directory"]
            else:
                assert metadata.startswith("Error:"), "MCP stat ignored revocation"
            assert not transports[name]._closed, "MCP closed its borrowed transport"

        async def sdk_denied(name, method, *args, **kwargs):
            try:
                result = await asyncio.to_thread(
                    getattr(filesystems[name], method), *args, **kwargs
                )
            except NexusPermissionError:
                pass
            except grpc.RpcError as error:
                assert error.code() == grpc.StatusCode.PERMISSION_DENIED, error
            else:
                raise AssertionError(f"SDK {method} unexpectedly passed authorization: {result!r}")

        async def vfs_denied(call):
            response = await call
            assert response.is_error, response
            assert json.loads(response.error_payload)["code"] in (-32003, -32018), response

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

            async def grant(name, method, path=None, relation="viewer"):
                response = await http.request(
                    method,
                    "/v2/rebac/tuples",
                    headers={"Authorization": f"Bearer {admin}"},
                    json={
                        "zone": "sharedzone",
                        "object_type": "file",
                        "object_id": path or paths[name],
                        "relation": relation,
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
                await sdk_denied(name, "sys_read", paths[name])
                await sdk_denied(name, "sys_stat", paths[name])
                await grant(name, "POST")
            await asyncio.gather(*(query(name, [paths[name]]) for name in paths for _ in range(3)))
            for name, (_, agent_vfs, client) in agents.items():
                read = await agent_vfs.Read(vfs_pb2.ReadRequest(path=paths[name]), timeout=15)
                assert not read.is_error and needle.encode() in read.content, read
                sdk_bytes = await asyncio.to_thread(filesystems[name].sys_read, paths[name])
                assert sdk_bytes == read.content
                sdk_range = await asyncio.to_thread(
                    filesystems[name].sys_read, paths[name], offset=1, count=5
                )
                assert sdk_range == read.content[1:6]
                sdk_stat = await asyncio.to_thread(filesystems[name].sys_stat, paths[name])
                assert sdk_stat["path"] == paths[name] and sdk_stat["size"] == len(read.content)
                for credential in ("sk-invalid-fixture-key", ""):
                    content = await mcp_read(name, paths[name], credential)
                    assert content.startswith("Error:"), "Invalid bearer fell back to certificate"
                other = "b" if name == "a" else "a"
                content = await mcp_read(name, paths[other], admin)
                assert content == f"{needle} {other}", "MCP did not forward the request bearer"
                content = await mcp_read(name, paths[other])
                assert content.startswith("Error:"), "Request bearer survived its scope"
                assert request_api_key.get() is None
                tagged = await vfs.SetXattr(
                    vfs_pb2.SetXattrRequest(
                        path=paths[name], key="r10_label", value=name, auth_token=admin
                    ),
                    timeout=15,
                )
                label = await agent_vfs.GetXattr(
                    vfs_pb2.GetXattrRequest(path=paths[name], key="r10_label"), timeout=15
                )
                if tagged.is_error:
                    # This cluster metastore has no xattr implementation. Check
                    # admission separately from backend support.
                    assert json.loads(tagged.error_payload)["code"] == -32603, tagged
                    assert "not implemented for this metastore" in str(tagged), tagged
                    reference = await vfs.GetXattr(
                        vfs_pb2.GetXattrRequest(
                            path=paths[name], key="r10_label", auth_token=admin
                        ),
                        timeout=15,
                    )
                    assert label == reference, (label, reference)
                else:
                    assert label.found and label.value == name, label
                await vfs_denied(
                    agent_vfs.SetXattr(
                        vfs_pb2.SetXattrRequest(path=paths[name], key="r10_label", value="bad"),
                        timeout=15,
                    )
                )
                await vfs_denied(
                    agent_vfs.Lock(
                        vfs_pb2.LockRequest(path=paths[name], timeout_ms=1000), timeout=15
                    )
                )
                await vfs_denied(
                    agent_vfs.Unlock(
                        vfs_pb2.UnlockRequest(path=paths[name], force=True), timeout=15
                    )
                )
                await vfs_denied(agent_vfs.CloseAllPipes(vfs_pb2.IpcEmpty(), timeout=15))
                await vfs_denied(
                    agent_vfs.Watch(vfs_pb2.WatchRequest(path="/docs/*", timeout_ms=0), timeout=15)
                )
                await sdk_denied(name, "sys_write", paths[name], b"bad", context={"is_admin": True})
                await sdk_denied(name, "sys_readdir", "/docs")
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
                await sdk_denied(name, "sys_stat", paths[other])
                await vfs_denied(
                    agent_vfs.GetXattr(
                        vfs_pb2.GetXattrRequest(path=paths[other], key="r10_label"), timeout=15
                    )
                )
                await vfs_denied(
                    agent_vfs.GetXattrBulk(
                        vfs_pb2.GetXattrBulkRequest(
                            paths=[paths[name], paths[other]], key="r10_label"
                        ),
                        timeout=15,
                    )
                )
                await denied(
                    agent_vfs.BatchStat(
                        vfs_pb2.BatchStatRequest(paths=[paths[name], paths[other]]), timeout=15
                    ),
                    grpc.StatusCode.PERMISSION_DENIED,
                )
                # Directory permission allows enumeration, while each returned
                # child still needs its own current file grant.
                await grant(name, "POST", "/docs")
                listed = await asyncio.to_thread(filesystems[name].sys_readdir, "/docs")
                assert listed == [paths[name]], listed
                page = await asyncio.to_thread(
                    filesystems[name].sys_readdir, "/docs", details=True, limit=1
                )
                assert [item["path"] for item in page.items] == [paths[name]]
                assert not page.has_more
                await grant(name, "DELETE", "/docs")
                await sdk_denied(name, "sys_readdir", "/docs")
                # Writer grants permit metadata and coordination operations.
                await grant(name, "POST", relation="writer")
                changed = await agent_vfs.SetXattr(
                    vfs_pb2.SetXattrRequest(
                        path=paths[name], key="r10_label", value=f"{name}-writer"
                    ),
                    timeout=15,
                )
                if tagged.is_error:
                    assert changed.error_payload == tagged.error_payload, (changed, tagged)
                else:
                    assert not changed.is_error, changed
                label = await agent_vfs.GetXattr(
                    vfs_pb2.GetXattrRequest(path=paths[name], key="r10_label"), timeout=15
                )
                if tagged.is_error:
                    assert label == reference, (label, reference)
                else:
                    assert label.value == f"{name}-writer", label
                locked = await agent_vfs.Lock(
                    vfs_pb2.LockRequest(path=paths[name], timeout_ms=1000), timeout=15
                )
                assert locked.acquired and not locked.is_error, locked
                await vfs_denied(
                    agent_vfs.Unlock(
                        vfs_pb2.UnlockRequest(path=paths[name], force=True), timeout=15
                    )
                )
                unlocked = await agent_vfs.Unlock(
                    vfs_pb2.UnlockRequest(path=paths[name], lock_id=locked.lock_id), timeout=15
                )
                assert unlocked.released and not unlocked.is_error, unlocked
                await grant(name, "DELETE", relation="writer")
                await vfs_denied(
                    agent_vfs.SetXattr(
                        vfs_pb2.SetXattrRequest(path=paths[name], key="r10_label", value="bad"),
                        timeout=15,
                    )
                )
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
            await sdk_denied("a", "sys_read", paths["a"])
            await sdk_denied("a", "sys_stat", paths["a"])
            await denied(
                agents["a"][1].BatchStat(vfs_pb2.BatchStatRequest(paths=[paths["a"]]), timeout=15),
                grpc.StatusCode.PERMISSION_DENIED,
            )
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
        for filesystem in filesystems.values():
            await asyncio.to_thread(filesystem.close)
        for channel in channels:
            await channel.close()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 180))
