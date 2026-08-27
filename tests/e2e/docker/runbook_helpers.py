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

# DirEntryType codes (mirror kernel `entry_type`): DT_STREAM is the mailbox.
DT_STREAM = 4

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

    # Auth-on: when `NEXUS_CA_PEM` points at the cluster CA, dial over TLS and
    # pin it. The cluster server cert's SAN is the fixed name `nexus-node` (not
    # the dialed host/IP), so override the target name to match; caller identity
    # still rides the per-request `auth_token` (`sk-`), not a client cert. Absent
    # the env, stay on the plaintext channel the docker/CI suites use.
    ca_pem = os.environ.get("NEXUS_CA_PEM")
    if ca_pem:
        with open(ca_pem, "rb") as fh:
            root_certificates = fh.read()
        # The cluster serves MUTUAL TLS, so a client cert is mandatory even for an
        # sk- caller: the cert authenticates the transport (a trusted cluster node),
        # the per-request `auth_token` (`sk-`) authenticates the caller. A local
        # admin tool presents the node cert — the same pair `auth mint` dials with.
        client_cert = os.environ.get("NEXUS_CLIENT_CERT")
        client_key = os.environ.get("NEXUS_CLIENT_KEY")
        if client_cert and client_key:
            with open(client_cert, "rb") as fh:
                certificate_chain = fh.read()
            with open(client_key, "rb") as fh:
                private_key = fh.read()
            creds = grpc.ssl_channel_credentials(
                root_certificates=root_certificates,
                private_key=private_key,
                certificate_chain=certificate_chain,
            )
        else:
            creds = grpc.ssl_channel_credentials(root_certificates=root_certificates)
        server_name = os.environ.get("NEXUS_TLS_SERVER_NAME", "nexus-node")
        options = GRPC_CHANNEL_OPTIONS + [("grpc.ssl_target_name_override", server_name)]
        channel = grpc.secure_channel(target, creds, options=options)
    else:
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


def vfs_create_stream(
    target: str,
    path: str,
    *,
    io_profile: str = "wal",
    capacity: int = 0,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 30,
) -> dict:
    """Create a DT_STREAM (durable append-only log) via Setattr.

    ``io_profile="wal"`` + ``capacity=0`` is the keep-forever unbounded log
    (audit / A2A-mailbox / transcript shape); a ``capacity>0`` trims the cold
    tier to that retention budget. Returns ``{"result": {"created": bool}}`` on
    success or ``{"error": {...}}`` on server-reported failure.
    """
    from nexus.grpc.vfs import vfs_pb2

    channel, stub = _open_stub(target)
    try:
        req = vfs_pb2.SetattrRequest(
            path=path,
            auth_token=api_key,
            entry_type=DT_STREAM,
            io_profile=io_profile,
            capacity=capacity,
        )
        resp = stub.Setattr(req, timeout=timeout)
        if resp.is_error:
            return {"error": _maybe_error_from_payload(resp.error_payload)}
        return {"result": {"created": resp.created}}
    finally:
        channel.close()


def vfs_stream_write(
    target: str,
    path: str,
    data: bytes,
    *,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 30,
) -> dict:
    """Append one frame to a DT_STREAM (StreamWriteNowait).

    Returns ``{"result": {"offset": int}}`` (the offset the frame landed at) on
    success or ``{"error": {...}}`` on server-reported failure.
    """
    from nexus.grpc.vfs import vfs_pb2

    channel, stub = _open_stub(target)
    try:
        req = vfs_pb2.StreamWriteRequest(path=path, data=data, auth_token=api_key)
        resp = stub.StreamWriteNowait(req, timeout=timeout)
        if resp.is_error:
            return {"error": _maybe_error_from_payload(resp.error_payload)}
        return {"result": {"offset": resp.offset}}
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


def vfs_delete(
    target: str,
    path: str,
    *,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 30,
) -> dict:
    """Typed Delete RPC — remove a file (or empty dir) from the VFS.

    Returns ``{"result": {"deleted": bool}}`` on success or
    ``{"error": str}``.  Idempotent-ish: deleting a missing path
    surfaces the kernel's NotFound as an error field.
    """
    from nexus.grpc.vfs import vfs_pb2

    channel, stub = _open_stub(target)
    try:
        req = vfs_pb2.DeleteRequest(path=path, auth_token=api_key)
        resp = stub.Delete(req, timeout=timeout)
        if resp.is_error:
            return {"error": _maybe_error_from_payload(resp.error_payload)}
        return {"result": {"deleted": True}}
    finally:
        channel.close()


def vfs_setattr(
    target: str,
    path: str,
    *,
    entry_type: int,
    io_profile: str = "",
    capacity: int = 0,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 30,
) -> dict:
    """Typed SetAttr RPC — create / idempotent-open a DT_* entry.

    For a replicated mailbox stream pass ``entry_type=DT_STREAM,
    io_profile="wal", capacity=<n>``: the kernel's stream waterfall builds a
    `WalStreamCore` (strong-consistency), and the state machine assigns each
    append's offset at the raft apply — so concurrent writers never collide.

    Returns ``{"result": {"created": bool, "entryType": int}}`` on success.
    """
    from nexus.grpc.vfs import vfs_pb2

    channel, stub = _open_stub(target)
    try:
        req = vfs_pb2.SetattrRequest(
            path=path,
            auth_token=api_key,
            entry_type=entry_type,
            io_profile=io_profile,
            capacity=capacity,
        )
        resp = stub.Setattr(req, timeout=timeout)
        if resp.is_error:
            return {"error": _maybe_error_from_payload(resp.error_payload)}
        return {"result": {"created": resp.created, "entryType": resp.entry_type}}
    finally:
        channel.close()


def stream_write_nowait(
    target: str,
    path: str,
    data: bytes,
    *,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 30,
) -> dict:
    """Append one message to a DT_STREAM mailbox (non-blocking). Returns
    ``{"result": {"offset": int}}`` — the offset the entry landed at."""
    from nexus.grpc.vfs import vfs_pb2

    channel, stub = _open_stub(target)
    try:
        req = vfs_pb2.StreamWriteRequest(path=path, data=data, auth_token=api_key)
        resp = stub.StreamWriteNowait(req, timeout=timeout)
        if resp.is_error:
            return {"error": _maybe_error_from_payload(resp.error_payload)}
        return {"result": {"offset": resp.offset}}
    finally:
        channel.close()


def stream_collect_all(
    target: str,
    path: str,
    *,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 30,
) -> dict:
    """Collect every message currently in a DT_STREAM mailbox. Returns
    ``{"result": {"data": bytes}}`` — the concatenated entry payloads."""
    from nexus.grpc.vfs import vfs_pb2

    channel, stub = _open_stub(target)
    try:
        req = vfs_pb2.IpcPathRequest(path=path, auth_token=api_key)
        resp = stub.StreamCollectAll(req, timeout=timeout)
        if resp.is_error:
            return {"error": _maybe_error_from_payload(resp.error_payload)}
        return {"result": {"data": resp.data}}
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
    boot regression.  On timeout, dumps container logs so CI
    transcripts carry the daemon's own boot log without needing a
    re-run with manual diagnostics.
    """
    deadline = time.time() + timeout
    for addr in grpc_addrs:
        while time.time() < deadline:
            h = health(addr)
            if h and h.get("status") == "healthy":
                break
            time.sleep(2)
        else:
            host = addr.split(":", 1)[0]
            candidates = [host, f"nexus-cc-tasks-{host}", f"nexus-runbook-{host}"]
            log_dump = ""
            for container in candidates:
                try:
                    logs = docker_logs(container, tail=200)
                except subprocess.SubprocessError:
                    continue
                if logs:
                    log_dump = f"\n--- {container} (tail 200) ---\n{logs}"
                    break
            pytest.fail(
                f"Timed out waiting for {addr} to become healthy{log_dump or ' (no container logs)'}"
            )


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
    probe_path: str | None = None,
) -> None:
    """Wait until every node in ``nodes`` has caught up on every zone in
    ``zone_ids``.

    Signal: a typed ``Stat`` for ``probe_path`` returns ``found=True``
    on every node.  When ``probe_path`` is supplied, this directly
    pins "the leader's last write is visible on every node's local
    state machine", which is what raft catch-up means at the
    application layer.

    Why typed Stat vs the Python-server ``federation_cluster_info``
    snapshot: the Rust ``nexusd-cluster`` binary's ``Call`` dispatcher
    exposes typed agent/mount-points surfaces only — federation
    cluster-info was a Python-server method and was never ported.
    Stat is the production read path; if it returns ``found=True``
    everywhere, the apply pointer is observably caught up — strictly
    stronger than the snapshot-equality check the legacy helper used.

    Backwards-compat: if ``probe_path`` is None, fall back to a brief
    settle window — the original gate was protecting against
    follower-stale reads which the runbook §3b 1-voter sharedzone with
    a single learner does not re-elect through, and the *next* op
    (``vfs_read`` / ``vfs_stat``) carries its own typed retry.
    """
    if isinstance(zone_ids, str):
        zone_ids = [zone_ids]
    if probe_path is None:
        time.sleep(0.5)
        return
    deadline = time.time() + timeout
    last_results: dict[str, dict] = {}
    while time.time() < deadline:
        last_results = {}
        all_ready = True
        for n in nodes:
            for zone_id in zone_ids:
                s = vfs_stat(n, probe_path, api_key=api_key, zone_id=zone_id, timeout=5)
                last_results[f"{n}|{zone_id}"] = s
                if "error" in s or not s.get("result", {}).get("found"):
                    all_ready = False
        if all_ready:
            return
        time.sleep(0.5)
    pytest.fail(
        f"Raft catch-up stalled: probe_path={probe_path} zones={zone_ids} "
        f"results={last_results} within {timeout}s.  Check transport reconnect / peer health."
    )


def wait_zone_ready(
    target: str,
    zone_id: str,
    *,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 30,
    mount_path: str = "/shared",
) -> None:
    """Poll until the daemon at ``target`` responds to a typed Stat on
    ``mount_path``, which proves that:

      1. The gRPC server is past the health-check window and routing
         typed VFS RPCs (``RaftService`` is live).
      2. The kernel has loaded ``zone_id`` from disk and wired its
         mount into the parent's DT_MOUNT table (otherwise the Stat
         path resolution short-circuits at the boundary check, surfacing
         as a gRPC ``error`` rather than a ``found=False`` result).

    ``found=False`` for a freshly-created mount with no content is a
    success signal: the request resolved through the mount, queried
    sharedzone's local state machine, and returned "no such path".  An
    *error* response (the only thing wedged paths produce) is the
    failure signal — that's what the legacy ``federation_cluster_info``
    poll was trying to detect by proxy, and what nexusd-cluster's
    typed-RPC surface lets us detect directly.

    Why two observations: the `nexusd-cluster join` CLI's wait-gate
    (nexus-vfs #31) guarantees the joiner's local state is current
    when it exits, but the post-restart daemon registers zones during
    boot, after which raft replays log entries into the state machine.
    Two consecutive successful responses (500 ms apart) pins that the
    second response wasn't the leading edge of an in-flight apply.
    """
    deadline = time.time() + timeout
    stable_window = 2  # consecutive successful observations
    successes = 0
    last_stat: dict = {}
    while True:
        last_stat = vfs_stat(target, mount_path, api_key=api_key, zone_id=zone_id, timeout=5)
        if "error" not in last_stat:
            successes += 1
            if successes >= stable_window:
                return
        else:
            successes = 0
        if time.time() >= deadline:
            pytest.fail(
                f"Zone '{zone_id}' not ready on {target} within {timeout}s "
                f"(last stat result: {last_stat})"
            )
        time.sleep(0.5)


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


_NODE_ID_LOG_RE = re.compile(r"Zone 'root' registered \(local_node_id=(\d+)")


def fetch_node_id(container: str, *, timeout: float = 30) -> int:
    """Extract the daemon's opaque node_id from its boot log.

    Mirrors the runbook §3b operator step verbatim: "Wait for:
    `Zone 'root' registered (local_node_id=<A_node_id>, peers=1)`".
    The `local_node_id` prefix (renamed from `node_id` in the
    peer-identity-surface refactor) disambiguates this node's own id
    from a `peer_node_id` field that names a remote peer — the two
    concepts previously shared the same field name and caused
    operator/AI misdiagnosis under n>2 topologies.  The on-disk
    `.node_id` file is a Rust-internal `u64::to_le_bytes()` binary
    detail the runbook itself never inspects.  Reading the log keeps
    the test aligned with the operator's vantage point and insulated
    from persistence-format changes.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        logs = docker_logs(container, tail=5000)
        m = _NODE_ID_LOG_RE.search(logs)
        if m:
            return int(m.group(1))
        time.sleep(1)
    pytest.fail(
        f"Could not find `Zone 'root' registered (local_node_id=...)` "
        f"in {container} logs within {timeout}s; daemon may not have "
        f"completed root-zone bootstrap."
    )


def run_nexusd_cluster_join(
    *,
    target_container: str,
    target_volume: str,
    founder_node_id: int,
    founder_addr: str,
    zone_id: str,
    local_path: str,
    hostname: str,
    cluster_image: str | None = None,
    network: str | None = None,
    data_dir: str = "/app/data",
    identity_volume: str | None = None,
    identity_dir: str = "/app/identity",
    timeout: float = 120,
    as_role: str = "voter",
) -> subprocess.CompletedProcess:
    """Run runbook §3b's offline `nexusd-cluster join` in a transient sidecar.

    The runbook says explicitly: the daemon must be stopped while
    `nexusd-cluster join` runs because redb holds an exclusive file
    lock.  In Docker, killing PID 1 (the daemon) exits the container,
    which means we cannot `docker exec` the join into the same
    container.  Instead, spin up a transient sidecar container from
    the same `nexusd-cluster:latest` image that the joiner uses, mount
    the joiner's persisted-data volume into it, and run `join` there.

    Lifecycle the caller must arrange around this:
      1. `docker_stop(target_container)` — releases the redb lock.
      2. `run_nexusd_cluster_join(...)` — this function, runs join.
      3. `docker_start(target_container)` — daemon comes back up,
         Phase G row 0 auto-resumes from on-disk state and replays
         DT_MOUNT via apply-cb (runbook §3c).

    ``as_role`` is the operator-chosen membership role passed to
    ``nexusd-cluster join --as <role>``.  Default is ``"voter"`` to
    match the daemon CLI default (nexus-vfs PR #66 flipped the CLI
    default from ``learner`` to ``voter`` so the operator-facing
    default aligns with the wire-level protocol default — proto3
    ``JoinZoneRequest.as_learner`` defaults to ``false`` = voter).
    Pass ``"learner"`` to exercise the owner-pattern share where the
    joiner gets full replication but doesn't count toward quorum
    (wipe-rejoin safe).

    Returns the CompletedProcess so callers can assert on rc /
    stdout / stderr (see test_joiner_zero_mount_not_leader_in_join_cli_log).
    """
    image = cluster_image or os.environ.get("NEXUS_CLUSTER_IMAGE", "nexusd-cluster:latest")
    net = network or os.environ.get("NEXUS_RUNBOOK_NETWORK", "nexus-runbook-net")
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        net,
        "--hostname",
        hostname,
        "-v",
        f"{target_volume}:{data_dir}",
    ]
    # `identity_volume` mounts the same host volume the target daemon
    # uses for its `NEXUS_IDENTITY_DIR` so the sidecar's post-JoinZone
    # `identity::persist_peers` write persists across the sidecar's
    # exit and the target daemon reads it on its next boot.  Without
    # this mount the sidecar writes to its own transient fs and every
    # restart of the target daemon loses the leader peer address.
    if identity_volume is not None:
        cmd.extend(["-v", f"{identity_volume}:{identity_dir}"])
        cmd.extend(["-e", f"NEXUS_IDENTITY_DIR={identity_dir}"])
    cmd.extend(
        [
            # Override the image's `ENTRYPOINT ["nexusd-cluster"]` so we
            # can pass `join` as the subcommand instead of an arg.
            "--entrypoint",
            "nexusd-cluster",
            image,
            "join",
            # Bare `host:port` is the ONLY accepted operator-facing
            # form post nexus-vfs #109.  `founder_node_id` is unused
            # by the CLI (kept as a keyword arg for source-diff-
            # compat; the daemon learns founder's real id from the
            # first inbound raft message via `learn_peer_address`).
            founder_addr,
            zone_id,
            local_path,
            "--data-dir",
            data_dir,
            "--no-tls",
            "--hostname",
            hostname,
            "--as",
            as_role,
        ]
    )
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


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
