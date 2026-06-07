"""Shared helpers for the runbook-binding federation E2E suite.

The functions in this module fall into two groups:

1. **gRPC + raft catch-up gates lifted from the legacy suite.**
   The body of `grpc_call`, `wait_healthy`, `wait_replicated`,
   `wait_leader_elected`, `wait_nodes_caught_up`, `wait_zone_ready` is
   copy-faithful with the legacy `test_federation_e2e.py` (lines 92-451)
   so the convergence semantics are preserved.  The renames drop the
   leading underscore and *deliberately* omit the legacy
   `_grpc_call_or_skip` / `_docker_client_or_skip` /
   `_cli_exec_or_skip` shapes — the runbook suite treats "method not
   available" / "docker socket missing" / "subcommand not found" as
   hard test failures, not silent skips.  See the plan for why.

2. **New runbook-specific helpers**
   (`run_nexusd_cluster_join`, `restart_daemon`, `assert_log_contains`,
   `assert_log_does_not_contain`).  These wrap `docker exec` /
   `docker restart` so test bodies stay focused on assertions.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import struct
import subprocess
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

import grpc
import pytest

HEALTH_TIMEOUT = 120
ADMIN_API_KEY = os.environ.get("NEXUS_API_KEY", "sk-test-runbook-key")

_LEADER_HINT_RE = re.compile(r"leader hint: Some\((\d+)\)")


# ---------------------------------------------------------------------------
# Topology resolution — env-driven so the same helpers work for the
# 3-node runbook compose and any future witness-only variant.
# ---------------------------------------------------------------------------
def hostname_to_node_id(hostname: str) -> int:
    """SHA-256 hostname -> u64 (matches Rust PeerAddress hashing)."""
    digest = hashlib.sha256(hostname.encode()).digest()
    return struct.unpack("<Q", digest[:8])[0] or 1


@dataclass(frozen=True)
class RunbookTopology:
    founder_grpc: str
    joiner_grpc: str
    witness_grpc: str
    founder_container: str
    joiner_container: str
    witness_container: str

    @property
    def all_voters_grpc(self) -> list[str]:
        return [self.founder_grpc, self.joiner_grpc, self.witness_grpc]


def topology_from_env() -> RunbookTopology:
    return RunbookTopology(
        founder_grpc=os.environ.get("NEXUS_FOUNDER_GRPC", "founder:2126"),
        joiner_grpc=os.environ.get("NEXUS_JOINER_GRPC", "joiner:2126"),
        witness_grpc=os.environ.get("NEXUS_WITNESS_GRPC", "witness:2126"),
        founder_container=os.environ.get("NEXUS_FOUNDER_CONTAINER", "nexus-runbook-founder"),
        joiner_container=os.environ.get("NEXUS_JOINER_CONTAINER", "nexus-runbook-joiner"),
        witness_container=os.environ.get("NEXUS_WITNESS_CONTAINER", "nexus-runbook-witness"),
    )


# ---------------------------------------------------------------------------
# gRPC wrappers — typed RPCs for VFS ops, generic Call for federation_*
#
# nexusd-cluster's gRPC server (the Rust production binary) exposes both
# typed RPCs (Write/Read/Stat/Mkdir/...) AND a generic Call RPC.  The
# typed RPCs are what the runbook itself uses (see runbook §4 grpcurl
# invocations against `.../Write`, `.../Stat`, `.../Read`); the generic
# Call dispatcher in the Rust binary registers federation_* methods and
# other non-VFS surfaces but does NOT register "write" / "read" / "stat"
# (those are typed RPCs).  Tests that go through the typed path mirror
# the operator contract exactly and avoid the legacy Python-fullnode-
# only `Call("write", ...)` dispatch pattern.
# ---------------------------------------------------------------------------
GRPC_CHANNEL_OPTIONS = [
    ("grpc.max_send_message_length", 64 * 1024 * 1024),
    ("grpc.max_receive_message_length", 64 * 1024 * 1024),
]


def _open_stub(target: str):
    """Open a NexusVFSServiceStub against `target`, returning (channel, stub).

    Caller owns the channel lifecycle.  Lazy imports keep collection-time
    cost zero so pytest can collect the suite without the stub-package
    being importable (relevant for static analysis / IDE tooling).
    """
    from nexus.grpc.vfs import vfs_pb2_grpc

    channel = grpc.insecure_channel(target, options=GRPC_CHANNEL_OPTIONS)
    return channel, vfs_pb2_grpc.NexusVFSServiceStub(channel)


def _maybe_error_from_payload(error_payload: bytes) -> dict:
    """Decode a typed RPC's is_error=true payload via the wire-format SSOT."""
    from nexus.lib.rpc_codec import decode_rpc_message

    if not error_payload:
        return {"message": "unknown error (empty error_payload)"}
    try:
        decoded = decode_rpc_message(error_payload)
        return decoded if isinstance(decoded, dict) else {"message": str(decoded)}
    except Exception as exc:
        return {"message": f"undecodable error_payload: {exc}"}


def vfs_write(
    target: str,
    path: str,
    content: bytes,
    *,
    api_key: str = ADMIN_API_KEY,
    content_id: str = "",
    timeout: float = 30,
) -> dict:
    """Typed Write RPC.  Mirrors runbook §4 `.../Write` grpcurl call.

    Returns ``{"result": {"contentId": str, "size": int, "gen": int}}``
    on success or ``{"error": {...}}`` on server-reported failure.
    """
    from nexus.grpc.vfs import vfs_pb2

    channel, stub = _open_stub(target)
    try:
        req = vfs_pb2.WriteRequest(
            path=path,
            content=content,
            auth_token=api_key,
            content_id=content_id,
        )
        resp = stub.Write(req, timeout=timeout)
        if resp.is_error:
            return {"error": _maybe_error_from_payload(resp.error_payload)}
        return {
            "result": {
                "contentId": resp.content_id,
                "size": resp.size,
                "gen": resp.gen,
            }
        }
    finally:
        channel.close()


def vfs_read(
    target: str,
    path: str,
    *,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 30,
) -> dict:
    """Typed Read RPC.  Mirrors runbook §4 `.../Read` grpcurl call.

    Returns ``{"result": {"content": bytes, "contentId": str, "size": int}}``
    on success.  `content` is raw bytes (typed protobuf field, no
    base64 round-trip) — callers compare with bytes literals.
    """
    from nexus.grpc.vfs import vfs_pb2

    channel, stub = _open_stub(target)
    try:
        req = vfs_pb2.ReadRequest(path=path, auth_token=api_key)
        resp = stub.Read(req, timeout=timeout)
        if resp.is_error:
            return {"error": _maybe_error_from_payload(resp.error_payload)}
        return {
            "result": {
                "content": resp.content,
                "contentId": resp.content_id,
                "size": resp.size,
                "gen": resp.gen,
            }
        }
    finally:
        channel.close()


def vfs_stat(
    target: str,
    path: str,
    *,
    api_key: str = ADMIN_API_KEY,
    zone_id: str = "",
    timeout: float = 15,
) -> dict:
    """Typed Stat RPC.  Mirrors runbook §4 `.../Stat` grpcurl call.

    Returns ``{"result": {"found", "zoneId", "contentId",
    "lastWriterAddress", "size", "gen", "version", ...}}`` on success.
    Keys are camelCase to match the legacy Python-side helper shape so
    existing test assertions don't need rewriting at every call site.
    """
    from nexus.grpc.vfs import vfs_pb2

    channel, stub = _open_stub(target)
    try:
        req = vfs_pb2.StatRequest(path=path, auth_token=api_key, zone_id=zone_id)
        resp = stub.Stat(req, timeout=timeout)
        if resp.is_error:
            return {"error": _maybe_error_from_payload(resp.error_payload)}
        return {
            "result": {
                "found": resp.found,
                "path": resp.path,
                "size": resp.size,
                "contentId": resp.content_id,
                "zoneId": resp.zone_id,
                "lastWriterAddress": resp.last_writer_address,
                "gen": resp.gen,
                "version": resp.version,
                "entryType": resp.entry_type,
                "isDirectory": resp.is_directory,
                "mimeType": resp.mime_type,
            }
        }
    finally:
        channel.close()


def vfs_mkdir(
    target: str,
    path: str,
    *,
    parents: bool = False,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 30,
) -> dict:
    """Typed Mkdir RPC."""
    from nexus.grpc.vfs import vfs_pb2

    channel, stub = _open_stub(target)
    try:
        req = vfs_pb2.MkdirRequest(
            path=path,
            auth_token=api_key,
            parents=parents,
            exist_ok=True,
        )
        resp = stub.Mkdir(req, timeout=timeout)
        if resp.is_error:
            return {"error": _maybe_error_from_payload(resp.error_payload)}
        return {"result": {"hit": resp.hit}}
    finally:
        channel.close()


def grpc_call(
    target: str,
    method: str,
    params: dict,
    *,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 10,
    node_id_to_grpc: dict[int, str] | None = None,
) -> dict:
    """Generic Call RPC — used for federation_* methods that route through
    the dynamic dispatcher rather than typed RPCs.

    Use the typed wrappers above (vfs_write / vfs_read / vfs_stat /
    vfs_mkdir) for VFS ops.  The Rust nexusd-cluster's Call dispatcher
    does NOT register "write" / "read" / "stat" / "mkdir" — those are
    typed RPCs at the gRPC service level.

    Follows Raft leader hints up to 2 redirects when ``node_id_to_grpc``
    is supplied.  Returns ``{"result": ...}`` on success or
    ``{"error": ...}`` on server-reported failure.
    """
    from nexus.grpc.vfs import vfs_pb2
    from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

    current = target
    result: dict = {}
    for _ in range(3):
        channel, stub = _open_stub(current)
        try:
            req = vfs_pb2.CallRequest(
                method=method,
                payload=encode_rpc_message(params),
                auth_token=api_key,
            )
            resp = stub.Call(req, timeout=timeout)
            result = decode_rpc_message(resp.payload)
            if resp.is_error and "not leader" in str(result) and node_id_to_grpc:
                match = _LEADER_HINT_RE.search(str(result.get("message", result)))
                if match:
                    leader_id = int(match.group(1))
                    leader_target = node_id_to_grpc.get(leader_id)
                    if leader_target and leader_target != current:
                        current = leader_target
                        continue
            if resp.is_error:
                return {"error": result}
            already_wrapped = (
                isinstance(result, dict) and "result" in result and "jsonrpc" not in result
            )
            if not already_wrapped:
                return {"result": result}
            return result
        finally:
            channel.close()
    return {"error": result}


def decode_content(result: dict) -> bytes:
    """Decode a `read` response payload to raw bytes (no str coercion).

    Runbook tests assert byte-exact equality; coercing to str the way
    the legacy helper did silently masked encoding bugs that flipped a
    `\\x00`-containing payload into a UTF-8 replacement char.  Always
    return `bytes`, callers decode if they want text.
    """
    data = result["result"]
    if isinstance(data, dict):
        if data.get("__type__") == "bytes":
            return base64.b64decode(data["data"])
        if "content" in data:
            content = data["content"]
            if isinstance(content, dict) and content.get("__type__") == "bytes":
                return base64.b64decode(content["data"])
            if isinstance(content, bytes):
                return content
            if isinstance(content, str):
                # Federation Read returns base64-encoded content per the
                # gRPC contract; the legacy str-coercion path was wrong.
                try:
                    return base64.b64decode(content)
                except Exception:
                    return content.encode()
        if "data" in data:
            try:
                return base64.b64decode(data["data"])
            except Exception:
                return str(data["data"]).encode()
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        try:
            return base64.b64decode(data)
        except Exception:
            return data.encode()
    return str(data).encode()


# ---------------------------------------------------------------------------
# Health + readiness gates
# ---------------------------------------------------------------------------
def health(grpc_addr: str) -> dict | None:
    """TCP-reachability probe.  Returns None if unreachable."""
    try:
        host, port_str = grpc_addr.rsplit(":", 1)
        with socket.create_connection((host, int(port_str)), timeout=5):
            return {"status": "healthy"}
    except (OSError, ValueError):
        return None


def wait_healthy(grpc_addrs: Iterable[str], timeout: float = HEALTH_TIMEOUT) -> None:
    """Wait until all gRPC addresses are TCP-reachable.

    Hard-fails on timeout — runbook tests cannot proceed past boot if
    a voter isn't reachable; silent skip would mask a real cluster
    boot regression.
    """
    deadline = time.time() + timeout
    for addr in grpc_addrs:
        while time.time() < deadline:
            h = health(addr)
            if h and h.get("status") == "healthy":
                break
            time.sleep(2)
        else:
            pytest.fail(f"Timed out waiting for {addr} to become healthy")


def uid() -> str:
    """Short unique ID for test isolation."""
    return uuid.uuid4().hex[:8]


def list_paths(result: dict) -> list[str]:
    files = result["result"]
    if isinstance(files, dict):
        files = files.get("files", [])
    return [f["path"] if isinstance(f, dict) else f for f in files]


def wait_replicated(
    target: str,
    parent: str,
    expected_path: str,
    *,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 15,
) -> None:
    """Poll `list` via gRPC until `expected_path` appears under `parent`."""
    deadline = time.time() + timeout
    while True:
        ls = grpc_call(target, "list", {"path": parent}, api_key=api_key, timeout=5)
        if "error" not in ls and expected_path in list_paths(ls):
            return
        if time.time() >= deadline:
            pytest.fail(f"File not replicated: {expected_path} not in {parent} on {target}")
        time.sleep(0.5)


def wait_leader_elected(
    target: str,
    zone_id: str,
    *,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 15,
) -> None:
    """Poll `federation_cluster_info` until this node leads `zone_id`."""
    deadline = time.time() + timeout
    last: dict = {}
    while True:
        try:
            r = grpc_call(
                target,
                "federation_cluster_info",
                {"zone_id": zone_id},
                api_key=api_key,
                timeout=5,
            )
            last = r
            if "error" not in r and r.get("result", {}).get("is_leader"):
                return
        except Exception:
            pass
        if time.time() >= deadline:
            pytest.fail(
                f"Zone '{zone_id}' has no leader on {target} within {timeout}s (last: {last})"
            )
        time.sleep(0.2)


def wait_nodes_caught_up(
    nodes: list[str],
    zone_ids: str | list[str],
    *,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 60,
) -> None:
    """Wait until every node in `nodes` has caught up to the actual raft
    leader on every zone in `zone_ids`.

    The critical causal gate — without this, follower-side reads can
    race the apply pipeline and observe metadata=None even when the
    write committed on the leader.  Body is preserved verbatim from
    the legacy helper so the convergence semantics carry over.
    """
    if isinstance(zone_ids, str):
        zone_ids = [zone_ids]
    for zone_id in zone_ids:
        _wait_one_zone_caught_up(nodes, zone_id, api_key, timeout=timeout)


def _wait_one_zone_caught_up(
    nodes: list[str],
    zone_id: str,
    api_key: str,
    *,
    timeout: float,
) -> None:
    deadline = time.time() + timeout
    last_snapshots: dict[str, dict] = {}
    while time.time() < deadline:
        snapshots: dict[str, dict] = {}
        for n in nodes:
            snapshots[n] = (
                grpc_call(
                    n,
                    "federation_cluster_info",
                    {"zone_id": zone_id},
                    api_key=api_key,
                ).get("result")
                or {}
            )
        last_snapshots = snapshots

        candidate = None
        for n, info in snapshots.items():
            if not info.get("is_leader"):
                continue
            node_id = info.get("node_id", 0)
            leader_id = info.get("leader_id", 0)
            term = info.get("term", 0)
            if node_id == 0 or leader_id != node_id or term == 0:
                continue
            consensus = True
            for other, other_info in snapshots.items():
                if other == n or not other_info.get("has_store"):
                    continue
                if other_info.get("leader_id", 0) != node_id:
                    consensus = False
                    break
                if other_info.get("term", 0) != term:
                    consensus = False
                    break
            if consensus:
                candidate = info
                break

        if candidate is not None:
            leader_ai = candidate.get("applied_index", 0)
            if leader_ai > 0 and all(
                snapshots[n].get("has_store") and snapshots[n].get("applied_index", 0) >= leader_ai
                for n in nodes
            ):
                return
        time.sleep(0.5)
    pytest.fail(
        f"Raft catch-up stalled: zone={zone_id} snapshots={last_snapshots} "
        f"within {timeout}s.  Check transport reconnect / peer health."
    )


def wait_zone_ready(
    target: str,
    zone_id: str,
    *,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 30,
) -> None:
    """Poll until `zone_id` exists on `target` AND its raft group has a
    *stable* leader (same leader_id for >= 2 s).

    Stability requirement protects subsequent writes from racing the
    next election; see the legacy comment block for the failure mode
    this guards against.
    """
    deadline = time.time() + timeout
    stable_window = 2.0
    last_leader = 0
    leader_first_seen: float | None = None
    while True:
        r = grpc_call(target, "federation_list_zones", {}, api_key=api_key, timeout=5)
        if "error" not in r:
            zones = r.get("result", {}).get("zones", [])
            zone_ids = [z["zone_id"] for z in zones]
            if zone_id in zone_ids:
                ci = grpc_call(
                    target,
                    "federation_cluster_info",
                    {"zone_id": zone_id},
                    api_key=api_key,
                    timeout=5,
                )
                if "error" not in ci:
                    leader = ci.get("result", {}).get("leader_id", 0)
                    if leader:
                        if leader != last_leader:
                            last_leader = leader
                            leader_first_seen = time.time()
                        elif (
                            leader_first_seen is not None
                            and time.time() - leader_first_seen >= stable_window
                        ):
                            return
                    else:
                        last_leader = 0
                        leader_first_seen = None
        if time.time() >= deadline:
            pytest.fail(f"Zone '{zone_id}' not ready on {target} within {timeout}s")
        time.sleep(1)


# ---------------------------------------------------------------------------
# Runbook-specific CLI + log helpers
# ---------------------------------------------------------------------------
@dataclass
class DockerExecResult:
    rc: int
    stdout: str
    stderr: str


def docker_exec(
    container: str,
    argv: list[str],
    *,
    timeout: float = 60,
    check: bool = False,
) -> DockerExecResult:
    """Run `docker exec <container> argv...` and capture rc/stdout/stderr.

    Hard-required: there is no `or_skip` fallback.  If the docker
    socket is unavailable inside the test container, that is itself a
    test failure — the runbook suite is docker-bound by definition.
    """
    cmd = ["docker", "exec", container, *argv]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        pytest.fail(
            f"docker exec {container} {' '.join(argv)} failed "
            f"(rc={proc.returncode}):\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return DockerExecResult(
        rc=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def docker_restart(container: str, *, timeout: float = 30) -> None:
    subprocess.run(
        ["docker", "restart", container],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def docker_stop(container: str, *, timeout: float = 30) -> None:
    subprocess.run(
        ["docker", "stop", container],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def docker_start(container: str, *, timeout: float = 30) -> None:
    subprocess.run(
        ["docker", "start", container],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def docker_logs(container: str, *, tail: int = 1000, since: str | None = None) -> str:
    cmd = ["docker", "logs", container, "--tail", str(tail)]
    if since:
        cmd.extend(["--since", since])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    # docker logs prints to both stdout (stdout from container) and
    # stderr (stderr from container); merge for substring assertions.
    return (proc.stdout or "") + (proc.stderr or "")


def fetch_node_id(container: str) -> int:
    """Read `<data-dir>/.node_id` from inside `container`.

    Used to spell out the runbook's `<node_id>@<host>:<port>` peer
    address when building `nexusd-cluster join` invocations.
    """
    paths = ["/app/data/.node_id", "/home/nexus/data/.node_id"]
    for path in paths:
        result = docker_exec(container, ["cat", path], timeout=10)
        if result.rc == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    pytest.fail(
        f"Could not read .node_id from {container} at {paths}; "
        f"daemon may not have completed first-boot persistence."
    )


def run_nexusd_cluster_join(
    joiner_container: str,
    founder_node_id: int,
    founder_addr: str,
    zone_id: str,
    local_path: str,
    *,
    hostname: str | None = None,
    data_dir: str = "/app/data",
    timeout: float = 60,
) -> DockerExecResult:
    """Run the runbook's exact `nexusd-cluster join` invocation.

    Daemon must be stopped on the joiner side (redb holds an exclusive
    file lock); callers are responsible for `pkill nexusd-cluster` +
    grace wait before invoking.  Returns the rc/stdout/stderr for
    log assertions.
    """
    argv = [
        "nexusd-cluster",
        "join",
        f"{founder_node_id}@{founder_addr}",
        zone_id,
        local_path,
        "--data-dir",
        data_dir,
        "--no-tls",
    ]
    if hostname:
        argv.extend(["--hostname", hostname])
    return docker_exec(joiner_container, argv, timeout=timeout)


def stop_daemon_in_container(container: str, *, grace_seconds: float = 3) -> None:
    """SIGTERM the `nexusd-cluster` process inside `container` and wait.

    Used between `up -d` boot and the offline `join` subcommand the
    runbook calls for.  `docker stop` would also stop the container
    itself; we want the process down with the container still up so
    the same /app/data filesystem is reachable for the `join` run.
    """
    docker_exec(container, ["pkill", "-TERM", "-f", "nexusd-cluster"], timeout=10)
    time.sleep(grace_seconds)


def assert_log_contains(
    container: str,
    pattern: str,
    *,
    since: str | None = None,
    tail: int = 2000,
    msg: str | None = None,
) -> None:
    logs = docker_logs(container, tail=tail, since=since)
    if pattern not in logs:
        snippet = logs[-2000:] if len(logs) > 2000 else logs
        pytest.fail(
            (msg or f"expected log line containing {pattern!r} not found in {container}")
            + f"\n--- tail of {container} logs ---\n{snippet}"
        )


def assert_log_does_not_contain(
    container: str,
    pattern: str,
    *,
    since: str | None = None,
    tail: int = 2000,
    msg: str | None = None,
) -> None:
    logs = docker_logs(container, tail=tail, since=since)
    if pattern in logs:
        snippet = logs[-2000:] if len(logs) > 2000 else logs
        pytest.fail(
            (msg or f"forbidden log line containing {pattern!r} found in {container}")
            + f"\n--- tail of {container} logs ---\n{snippet}"
        )


def count_log_occurrences(
    container: str,
    pattern: str,
    *,
    since: str | None = None,
    tail: int = 2000,
) -> int:
    logs = docker_logs(container, tail=tail, since=since)
    return logs.count(pattern)
