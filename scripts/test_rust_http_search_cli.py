"""Live CLI -> native HTTP/gRPC -> registry and signed Search plugin.

Uses the isolated ReBAC/mTLS fixture configured by NEXUS_SEARCH_TEST_HTTP,
NEXUS_SEARCH_TEST_ADMIN_KEY and NEXUS_SEARCH_PLUGIN_TLS_*.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import grpc
import httpx

from nexus.cli.exit_codes import ExitCode
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
        return re.sub(r"sk-[A-Za-z0-9_.-]+", "[redacted]", text)

    async def run(
        args, key=admin, zone="sharedzone", expected=0, extra_env=None, bytes_output=False
    ):
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
        env.update(extra_env or {})
        process = await asyncio.create_subprocess_exec(
            sys.executable,
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
        output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
        assert process.returncode == expected, (args, process.returncode, safe(output))
        return (stdout if bytes_output else stdout.decode()), output

    async def cli(args, key=admin, zone="sharedzone", expected=0):
        return await run(["-m", "nexus.cli.main", "search", *args], key, zone, expected)

    async def native_cli(args, key=admin, expected=0, extra_env=None):
        stdout, _ = await run(
            ["-m", "nexus.cli.main", *args, "--json"],
            key=key,
            expected=expected,
            extra_env=extra_env,
        )
        return json.loads(stdout)

    agent_env = {
        "NEXUS_GRPC_PORT": target.rsplit(":", 1)[1],
        "NEXUS_GRPC_TLS": "true",
        "NEXUS_TLS_CA": os.environ["NEXUS_SEARCH_PLUGIN_TLS_CA"],
        "NEXUS_TLS_CERT": os.environ["NEXUS_SEARCH_PLUGIN_TLS_CERT"],
        "NEXUS_TLS_KEY": os.environ["NEXUS_SEARCH_PLUGIN_TLS_KEY"],
    }

    async def agent_cli(args, expected=0, key=admin):
        return await native_cli(["agent", *args], key=key, expected=expected, extra_env=agent_env)

    async def verify_agent_registry():
        owner = f"cli-runtime-{suffix}"
        register = [
            "register",
            "CLI native acceptance",
            "--owner-id",
            owner,
            "--connection-id",
            f"cli-{suffix}",
            "--host-pid",
            str(os.getpid()),
            "--label",
            f"acceptance={suffix}",
        ]
        parent = str(os.getpid())
        child = f"{parent}.7"
        created = []
        try:
            record = (await agent_cli(register))["data"]
            created.append(parent)
            assert record["pid"] == parent and record["state"] == "REGISTERED", record
            assert record["owner_id"] == owner and record["zone_id"] == "sharedzone", record
            assert record["labels"]["acceptance"] == suffix, record
            assert record["external_info"]["connection_id"] == f"cli-{suffix}", record
            assert "host_pid" not in record["external_info"] and "api_key" not in record, record
            duplicate = await agent_cli(register, expected=1)
            assert duplicate["error"] and duplicate["data"] is None, duplicate

            micro = (await agent_cli([*register, "--local-id", "7", "--parent-pid", parent]))[
                "data"
            ]
            created.append(child)
            assert micro["pid"] == child and micro["parent_pid"] == parent, micro
            assert set((await agent_cli(["info", parent]))["data"]["children"]) == {child}
            records = (await agent_cli(["list", "--owner-id", owner]))["data"]
            assert {r["pid"] for r in records} == {parent, child}, records
            assert not (await agent_cli(["list", "--owner-id", owner, "--zone-id", "otherzone"]))[
                "data"
            ]

            rejected = await agent_cli(["transition", child, "ready"], expected=1)
            assert rejected["error"] and rejected["data"] is None, rejected
            assert (await agent_cli(["info", child]))["data"]["state"] == "REGISTERED"
            for state in ("warming_up", "ready", "busy", "awaiting_input", "busy", "ready"):
                record = (await agent_cli(["transition", child, state]))["data"]
                assert record["state"] == state.upper(), record
            ready = (await agent_cli(["list", "--owner-id", owner, "--state", "ready"]))["data"]
            assert [r["pid"] for r in ready] == [child], ready
            heartbeat = (await agent_cli(["heartbeat", child]))["data"]
            assert heartbeat["external_info"]["last_heartbeat_ms"] > 0, heartbeat
            steered = (
                await agent_cli(["signal", child, "SIGUSR1", "--label", "steering=accepted"])
            )["data"]
            assert steered["labels"]["steering"] == "accepted" and steered["state"] == "READY", (
                steered
            )
            terminated = (await agent_cli(["signal", child, "SIGTERM"]))["data"]
            assert terminated["state"] == "TERMINATED", terminated
            rejected = await agent_cli(["transition", child, "ready"], expected=1)
            assert rejected["error"] and rejected["data"] is None, rejected
            await agent_cli(["list"], key="sk-never-minted", expected=1)
            assert (await agent_cli(["delete", child, "--yes"]))["data"] == {"unregistered": True}
            created.remove(child)
            missing = await agent_cli(["info", child], expected=1)
            assert missing["error"]["code"] == "NOT_FOUND", missing
            assert not (await agent_cli(["info", parent]))["data"]["children"]
            print(
                "Native agent CLI passed: canonical PID, parent/child, filters, state machine, heartbeat, signals, auth, unregister"
            )
        finally:
            for pid in reversed(created):
                await agent_cli(["delete", pid, "--yes"])

    async def verify_file_cli(client_data_dir):
        prefix = f"/docs/cli-files-{suffix}"
        path = f"{prefix}/字节 文件#原始.bin"
        moved = f"{prefix}/renamed.bin"
        content = bytes(range(256)) + "Native 文件\r\n".encode()
        file_env = {
            **agent_env,
            "NEXUS_KERNEL_BINARY": "/file-cli-must-use-native-grpc",
            "NEXUS_PROFILE": "",
            "NEXUS_DATA_DIR": str(client_data_dir),
        }

        async def file_cli(args, **options):
            return await run(["-m", "nexus.cli.main", *args], extra_env=file_env, **options)

        created = False
        try:
            await file_cli(["mkdir", prefix])
            created = True
            with tempfile.TemporaryDirectory(prefix="native-cli-bytes-") as temp:
                source = Path(temp) / "原始字节.bin"
                source.write_bytes(content)
                await file_cli(["write", path, "--input", str(source)])
            read = await vfs.Read(vfs_pb2.ReadRequest(path=path, auth_token=admin))
            assert not read.is_error and bytes(read.content) == content, safe(str(read))
            lock_lease_ms = 300_000
            locked = await vfs.Lock(
                vfs_pb2.LockRequest(path=path, auth_token=admin, timeout_ms=lock_lease_ms),
                timeout=15,
            )
            assert not locked.is_error and locked.acquired and locked.lock_id, safe(str(locked))
            release = ["lock", "release", path, "--lock-id", locked.lock_id, "--json"]
            try:
                denied, _ = await file_cli(release, key=keys["alice"], expected=1)
                assert json.loads(denied)["error"]["code"] == "PERMISSION_DENIED", safe(denied)
                wrong, _ = await file_cli(
                    ["lock", "release", path, "--lock-id", "wrong-lock-id", "--json"], expected=1
                )
                assert json.loads(wrong)["data"] == {"path": path, "released": False}, safe(wrong)
                contended = await vfs.Lock(
                    vfs_pb2.LockRequest(path=path, auth_token=admin, timeout_ms=lock_lease_ms),
                    timeout=15,
                )
                assert not contended.is_error and not contended.acquired, safe(str(contended))
                released, _ = await file_cli(release)
                assert json.loads(released)["data"] == {"path": path, "released": True}, safe(
                    released
                )
                absent, _ = await file_cli(release, expected=1)
                assert json.loads(absent)["data"]["released"] is False, safe(absent)
                replacement = await vfs.Lock(
                    vfs_pb2.LockRequest(path=path, auth_token=admin, timeout_ms=lock_lease_ms),
                    timeout=15,
                )
                assert not replacement.is_error and replacement.acquired, safe(str(replacement))
                forced, _ = await file_cli(["lock", "release", path, "--force", "--json"])
                assert json.loads(forced)["data"]["released"] is True, safe(forced)
            finally:
                cleanup = await vfs.Unlock(
                    vfs_pb2.UnlockRequest(path=path, auth_token=admin, force=True), timeout=15
                )
                assert not cleanup.is_error, safe(str(cleanup))
            stdout, _ = await file_cli(["cat", path, "--json"])
            preview = json.loads(stdout)["data"]
            assert preview["binary"] is True and preview["content"] is None, preview
            assert preview["size"] == len(content), preview
            raw, _ = await file_cli(
                ["cat", path, "--offset", "12", "--length", "96"], bytes_output=True
            )
            assert raw == content[12:108], raw
            raw, _ = await file_cli(["cat", path, "--length", "17"], bytes_output=True)
            assert raw == content[:17], raw
            raw, _ = await file_cli(["cat", path, "--length", "0"], bytes_output=True)
            assert raw == b"", raw
            raw, _ = await file_cli(
                ["cat", path, "--offset", str(len(content) + 1), "--stream"], bytes_output=True
            )
            assert raw == b"", raw
            raw, _ = await file_cli(
                ["cat", path, "--offset", "12", "--length", "96", "--stream", "--chunk-size", "7"],
                bytes_output=True,
            )
            assert raw == content[12:108], raw
            raw, _ = await file_cli(
                ["cat", path, "--stream", "--chunk-size", "31"], bytes_output=True
            )
            assert raw == content, (len(raw), len(content))
            stdout, _ = await file_cli(["stat", path, "--json"])
            metadata = json.loads(stdout)["data"]
            assert metadata["size"] == len(content) and metadata["content_id"], metadata
            stdout, _ = await file_cli(["info", path, "--json"])
            info = json.loads(stdout)["data"]
            assert info["size"] == len(content) and info["content_id"] == metadata["content_id"]
            for target in (path, prefix):
                stdout, _ = await file_cli(["size", target, "--details"])
                assert "File count: 1" in stdout and f"{len(content):,} bytes" in stdout, stdout
            stdout, _ = await file_cli(["ls", prefix, "--json"])
            assert path in {row["path"] for row in json.loads(stdout)["data"]}, stdout
            copied = f"{prefix}/copied.bin"
            await file_cli(["cp", path, copied])
            raw, _ = await file_cli(["cat", copied, "--stream"], bytes_output=True)
            assert raw == content, (len(raw), len(content))
            with tempfile.TemporaryDirectory(prefix="native-cli-batch-") as temp:
                source = Path(temp)
                (source / "first.txt").write_bytes(b"first native batch")
                (source / "second.txt").write_bytes(b"second native batch")
                await file_cli(["write-batch", str(source), "--dest-prefix", prefix])
            first, second = f"{prefix}/first.txt", f"{prefix}/second.txt"
            missing = f"{prefix}/missing.txt"
            stdout, _ = await file_cli(["read-bulk", first, second, missing, "--json"])
            assert json.loads(stdout)["data"] == {
                first: "first native batch",
                second: "second native batch",
                missing: None,
            }
            stdout, _ = await file_cli(
                ["rename-batch", f"{first}:{first}.moved", f"{missing}:{missing}.moved", "--json"]
            )
            renamed = json.loads(stdout)["data"]
            assert renamed[first]["success"] is True and renamed[missing]["success"] is False, (
                renamed
            )
            stdout, _ = await file_cli(["rm-batch", first + ".moved", second, missing, "--json"])
            deleted = json.loads(stdout)["data"]
            assert (
                deleted[first + ".moved"]["success"] is True and deleted[second]["success"] is True
            ), deleted
            assert deleted[missing]["success"] is False, deleted
            denied, _ = await file_cli(
                ["cat", path, "--json", "--subject", "user:forged-admin", "--is-admin"],
                key=keys["alice"],
                expected=1,
            )
            assert json.loads(denied)["error"]["code"] == "PERMISSION_DENIED", safe(denied)
            denied, _ = await file_cli(["info", path, "--json"], key=keys["alice"], expected=1)
            assert json.loads(denied)["error"]["code"] == "PERMISSION_DENIED", safe(denied)
            await native_cli(
                [
                    "rebac",
                    "create",
                    "user",
                    f"cli-alice-{suffix}",
                    "viewer",
                    "file",
                    path,
                    "--zone-id",
                    "sharedzone",
                ]
            )
            raw, _ = await file_cli(["cat", path, "--stream"], key=keys["alice"], bytes_output=True)
            assert raw == content, (len(raw), len(content))
            await native_cli(
                [
                    "rebac",
                    "delete",
                    "user",
                    f"cli-alice-{suffix}",
                    "viewer",
                    "file",
                    path,
                    "--zone-id",
                    "sharedzone",
                ]
            )
            await file_cli(["cat", path, "--json"], key=keys["alice"], expected=1)
            await file_cli(["move", path, moved, "--force"])
            read = await vfs.Read(vfs_pb2.ReadRequest(path=moved, auth_token=admin))
            assert not read.is_error and bytes(read.content) == content, safe(str(read))
            await file_cli(["rm", moved, "--force"])
            print(
                "Native file CLI passed: Unicode paths, binary exact bytes, range/stream, metadata/list, forged identity denial, immediate grant/revoke, rename/delete"
            )
        finally:
            if created:
                await file_cli(["rmdir", prefix, "--recursive", "--force"])

    async with httpx.AsyncClient(base_url=base, timeout=20, trust_env=False) as rust:

        async def admin_request(method, url, **kwargs):
            response = await rust.request(
                method, url, headers={"Authorization": f"Bearer {admin}"}, **kwargs
            )
            assert response.is_success, (response.status_code, safe(response.text))
            return response.json()

        async def grant(name, method):
            return (
                await native_cli(
                    [
                        "rebac",
                        "create" if method == "POST" else "delete",
                        "user",
                        f"cli-{name}-{suffix}",
                        "viewer",
                        "file",
                        paths[name],
                        "--zone-id",
                        "sharedzone",
                    ]
                )
            )["data"]

        try:
            await verify_agent_registry()
            for name, path in paths.items():
                minted = (
                    await native_cli(
                        [
                            "admin",
                            "create-key",
                            f"cli-{name}-{suffix}",
                            "--subject-type",
                            "user",
                            "--zone",
                            "sharedzone:r",
                        ]
                    )
                )["data"]
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

            identity = (
                await native_cli(
                    ["identity", "whoami"],
                    key=keys["alice"],
                    extra_env={
                        "NEXUS_SUBJECT": "user:forged-admin",
                        "NEXUS_AGENT_ID": "forged-admin",
                    },
                )
            )["data"]
            assert identity["subject_id"] == f"cli-alice-{suffix}" and identity["is_admin"] is False
            with tempfile.TemporaryDirectory(prefix="native-cli-no-local-state-") as temp:
                client_data_dir = Path(temp)
                await verify_file_cli(client_data_dir)
                assert not list(client_data_dir.iterdir()), "Remote CLI created local kernel state"
            status = (await native_cli(["status"], key=keys["alice"]))["data"]
            assert status["server"]["status"] == "ok" and status["server"]["version"], status
            assert status["identity"] == identity, status
            assert "deployment_profile" not in status and "enabled_bricks" not in status, status
            ready = (await native_cli(["ready", "--timeout", "10"], key=keys["alice"]))["data"]
            assert ready["ready"] is True and ready["identity"] == identity, ready
            for args in (["status"], ["ready", "--timeout", "10"]):
                denied = await native_cli(
                    args, key="sk-never-minted", expected=ExitCode.PERMISSION_DENIED
                )
                assert denied["error"]["code"] == "PERMISSION_DENIED", denied
            stdout, _ = await run(
                ["-m", "nexus.cli.main", "profile", "show", "--test"], key=keys["alice"]
            )
            assert "Native HTTP connection OK" in stdout and identity["subject_id"] in stdout, safe(
                stdout
            )
            await run(
                ["-m", "nexus.cli.main", "profile", "show", "--test"],
                key="sk-never-minted",
                expected=1,
            )
            doctor = await native_cli(["doctor", "remote"], key=keys["alice"], extra_env=agent_env)
            assert {check["name"] for check in doctor["data"]} == {"remote-http", "remote-grpc"}, (
                doctor
            )
            assert all(check["status"] == "ok" for check in doctor["data"]), doctor

            routes = []

            async def unrelated_server(reader, writer):
                try:
                    request = await reader.readuntil(b"\r\n\r\n")
                    route = request.split(b" ")[1].decode()
                    routes.append(route)
                    body = json.dumps(
                        {"status": "ok", "version": "unrelated"} if route == "/v2/status" else {}
                    ).encode()
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\n\r\n"
                        + body
                    )
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            fake = await asyncio.start_server(unrelated_server, "127.0.0.1", 0)
            async with fake:
                port = fake.sockets[0].getsockname()[1]
                wrong_host = {"NEXUS_URL": f"http://127.0.0.1:{port}"}
                rejected = await native_cli(
                    ["status"],
                    key="sk-disposable-fixture",
                    expected=ExitCode.INTERNAL_ERROR,
                    extra_env=wrong_host,
                )
                assert rejected["data"] is None and rejected["error"], rejected
                timed_out = await native_cli(
                    ["ready", "--timeout", "1"],
                    key="sk-disposable-fixture",
                    expected=ExitCode.TEMPFAIL,
                    extra_env=wrong_host,
                )
                assert timed_out["data"]["ready"] is False, timed_out
            assert set(routes) == {"/v2/status", "/v2/auth/whoami"}, routes
            listed = (
                await native_cli(["admin", "list-keys", "--subject-id", f"cli-alice-{suffix}"])
            )["data"]["keys"]
            assert len(listed) == 1 and listed[0]["key_hash"] == hashes["alice"]
            assert all("key" not in row for row in listed)
            tuples = (await native_cli(["rebac", "list", "--zone-id", "sharedzone"]))["data"][
                "tuples"
            ]
            assert all(tuple_["zone"] == "sharedzone" for tuple_ in tuples)
            assert any(tuple_["subject_id"] == f"cli-alice-{suffix}" for tuple_ in tuples)
            for args in (["admin", "list-keys"], ["rebac", "list", "--zone-id", "sharedzone"]):
                denied = await native_cli(
                    args, key=keys["alice"], expected=ExitCode.PERMISSION_DENIED
                )
                assert denied["error"]["code"] == "PERMISSION_DENIED", denied
            await native_cli(
                ["admin", "revoke-key", hashes["bob"]],
                key=keys["alice"],
                expected=ExitCode.PERMISSION_DENIED,
            )
            await native_cli(
                ["identity", "whoami"], key="sk-never-minted", expected=ExitCode.PERMISSION_DENIED
            )
            userset = [
                "group",
                f"cli-team-{suffix}",
                "viewer",
                "file",
                paths["alice"],
                "--zone-id",
                "sharedzone",
            ]
            await native_cli(["rebac", "create", *userset])
            await native_cli(["rebac", "create", *userset, "--subject-relation", "member"])
            removed = await native_cli(
                ["rebac", "delete", *userset, "--subject-relation", "member"]
            )
            assert removed["data"]["existed"] is True
            remaining = (await native_cli(["rebac", "list", "--zone-id", "sharedzone"]))["data"][
                "tuples"
            ]
            remaining = [item for item in remaining if item["subject_id"] == f"cli-team-{suffix}"]
            assert len(remaining) == 1 and remaining[0].get("subject_relation") is None
            await native_cli(["rebac", "delete", *userset])

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
            context_command = ["-m", "nexus.cli.main", "path-context"]
            prefix = paths["alice"]
            await run(
                [
                    *context_command,
                    "set",
                    prefix,
                    "preferred document",
                    "--zone-id",
                    "sharedzone",
                    "--weight",
                    "4",
                ]
            )
            context_json, _ = await run(
                [*context_command, "list", "--zone-id", "sharedzone", "--json"], key=keys["alice"]
            )
            configured = next(
                record
                for record in json.loads(context_json)
                if record["path_prefix"] == prefix.strip("/")
            )
            assert configured["weight"] == 4.0, configured
            weighted_json, _ = await cli(
                ["query", needle, "--mode", "keyword", "--json"], key=keys["alice"]
            )
            assert json.loads(weighted_json)[0]["tier_boost"] == 4.0
            await run(
                [*context_command, "set", prefix, "forbidden", "--zone-id", "sharedzone"],
                key=keys["alice"],
                expected=1,
            )
            await run(
                [*context_command, "list", "--zone-id", "root", "--json"],
                key=keys["alice"],
                expected=1,
            )
            await run([*context_command, "list", "--json"], key="sk-never-minted", expected=1)
            await run(
                [*context_command, "set", prefix, "plain document", "--zone-id", "sharedzone"]
            )
            replaced_json, _ = await run(
                [*context_command, "list", "--zone-id", "sharedzone", "--json"]
            )
            replaced = next(
                record
                for record in json.loads(replaced_json)
                if record["path_prefix"] == prefix.strip("/")
            )
            assert replaced["weight"] is None and replaced["created_at"] == configured["created_at"]
            plain_json, _ = await cli(
                ["query", needle, "--mode", "keyword", "--json"], key=keys["alice"]
            )
            assert json.loads(plain_json)[0].get("tier_boost") in (None, 1.0)
            await run([*context_command, "delete", prefix, "--zone-id", "sharedzone"])
            await run([*context_command, "delete", prefix, "--zone-id", "sharedzone"], expected=1)
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
            revoked = await native_cli(["admin", "revoke-key", hashes["bob"]])
            assert revoked["data"]["existed"] is True
            repeated = await native_cli(["admin", "revoke-key", hashes["bob"]])
            assert repeated["data"]["existed"] is False
            await cli(["query", needle, "--mode", "keyword"], key=keys["bob"], expected=77)
            await cli(["query", needle, "--mode", "keyword"], key="sk-never-minted", expected=77)

            with tempfile.TemporaryDirectory(prefix="search-benchmark-") as temp_dir:
                dataset = Path(temp_dir)
                corpus = dataset / "corpus"
                corpus.mkdir()
                bench_prefix = f"/docs/benchmark-{suffix}"
                pages = [
                    {
                        "slug": "alpha",
                        "title": f"amaryllis{suffix}",
                        "compiled_truth": "first page",
                    },
                    {
                        "slug": "nested/beta",
                        "title": f"buttercup{suffix}",
                        "compiled_truth": "second page",
                        "timeline": ["one", "two"],
                    },
                ]
                queries = [
                    {"id": str(i), "text": page["title"], "gold": {"relevant": [page["slug"]]}}
                    for i, page in enumerate(pages)
                ]
                for i, page in enumerate(pages):
                    (corpus / f"{i}.json").write_text(json.dumps(page), encoding="utf-8")
                query_file = dataset / "queries.json"
                query_file.write_text(json.dumps(queries), encoding="utf-8")
                result_file = dataset / "results.json"
                bench_env = {
                    "BENCH_CORPUS_DIR": str(corpus),
                    "BENCH_QUERIES_FILE": str(query_file),
                    "BENCH_ZONE_PREFIX": bench_prefix,
                    "NEXUS_GRPC_PORT": target.rsplit(":", 1)[1],
                    "NEXUS_GRPC_TLS": "true",
                    "NEXUS_TLS_CA": os.environ["NEXUS_SEARCH_PLUGIN_TLS_CA"],
                    "NEXUS_TLS_CERT": os.environ["NEXUS_SEARCH_PLUGIN_TLS_CERT"],
                    "NEXUS_TLS_KEY": os.environ["NEXUS_SEARCH_PLUGIN_TLS_KEY"],
                }
                benchmark = [
                    str(Path(__file__).with_name("bench_gbrain_evals.py")),
                    "--mode",
                    "keyword",
                    "--save-results",
                    str(result_file),
                ]
                await run(benchmark, extra_env=bench_env)
                for page in pages:
                    read = await vfs.Read(
                        vfs_pb2.ReadRequest(
                            path=f"{bench_prefix}/{page['slug']}.md", auth_token=admin
                        )
                    )
                    assert not read.is_error and page["title"].encode() in read.content, safe(
                        str(read)
                    )
                metrics = json.loads(result_file.read_text())
                assert metrics["query_type"] == "keyword", metrics
                assert metrics["queries"] == 2 and metrics["hits_any"] == "2/2", metrics
                assert metrics["p_at_5"] == 0.2 and metrics["r_at_5"] == 1.0, metrics
                assert metrics["mrr"] == 1.0, metrics

                # Query-only runs never resolve an upload channel or require admin statistics.
                query_env = {**bench_env, "NEXUS_GRPC_PORT": "invalid"}
                await run([*benchmark, "--skip-index"], extra_env=query_env)
                reused = json.loads(result_file.read_text())
                assert reused["per_query"] == metrics["per_query"], reused
                await run([*benchmark, "--skip-index"], key=keys["alice"], extra_env=query_env)
                assert json.loads(result_file.read_text())["hits_any"] == "0/2"
                await run(
                    [*benchmark, "--skip-index"],
                    key="sk-never-minted",
                    expected=1,
                    extra_env=query_env,
                )
                await run(benchmark, key=keys["alice"], expected=1, extra_env=bench_env)

                # Corpus paths and duplicate slugs fail before any connection or write.
                invalid_page = corpus / "0.json"
                for slug, message in (
                    ("../escape", "Invalid corpus slug"),
                    ("nested/beta", "unique"),
                ):
                    invalid_page.write_text(
                        json.dumps({**pages[0], "slug": slug}), encoding="utf-8"
                    )
                    _, error = await run(benchmark, expected=1, extra_env=query_env)
                    assert message in error, safe(error)
                invalid_page.write_text(json.dumps(pages[0]), encoding="utf-8")
                _, error = await run(
                    benchmark,
                    expected=1,
                    extra_env={**query_env, "BENCH_ZONE_PREFIX": "/docs/../escape"},
                )
                assert "absolute VFS directory" in error, safe(error)
            print(
                "Rust HTTP CLI passed: native-identity/keys/full-tuples/index/query/stats/path-contexts/isolation/path/zone/admin/revocation/benchmark"
            )
        finally:
            await channel.close()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 300))
