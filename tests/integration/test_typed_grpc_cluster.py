"""Typed gRPC FS surface E2E via ``nexus-cluster`` (Issue #4133).

The standalone hub (``shared``/``demo``) intentionally does not bind
the typed gRPC service — see commit 607ae89b5 "delete legacy Python
gRPC bridge". The typed ``NexusVFSService``
(Ping/Read/Write/Delete/BatchRead) lives in ``rust/transport/src/grpc.rs``
and is bound only by ``nexus-cluster`` (Rust federation binary).

This module asserts that the typed contract — same names, same byte
semantics — actually works end-to-end over real gRPC against the
cluster binary. Skipped cleanly when:

  * ``NEXUS_E2E != "1"``                       (no E2E gate)
  * ``nexus-cluster`` is not on PATH           (binary not built)
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

requires_e2e = pytest.mark.skipif(
    os.environ.get("NEXUS_E2E") != "1",
    reason="typed gRPC E2E requires NEXUS_E2E=1",
)


def _resolve_worktree_cluster_binary() -> str | None:
    """Resolve the cluster daemon from the *worktree's* Rust build, not
    from ``$PATH``. The Cargo ``[[bin]]`` name is ``nexusd-cluster``;
    ``nexus-cluster`` is a common downstream symlink (e.g. ``cargo
    install`` renames). Probe both, prefer the canonical name.
    """
    repo_root = Path(__file__).resolve().parents[2]
    names = ("nexusd-cluster", "nexus-cluster")
    targets = (
        repo_root / "rust" / "target" / "release",
        repo_root / "rust" / "target" / "debug",
        repo_root / "target" / "release",
        repo_root / "target" / "debug",
    )
    for tdir in targets:
        for name in names:
            candidate = tdir / name
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
    # Last resort: a $PATH binary, only if NEXUS_E2E_ALLOW_SYSTEM_CLUSTER=1
    # opts in explicitly (e.g. CI installs the right version itself).
    if os.environ.get("NEXUS_E2E_ALLOW_SYSTEM_CLUSTER") == "1":
        for name in names:
            path = shutil.which(name)
            if path:
                return path
    return None


@pytest.fixture()
def cluster_grpc(tmp_path: Path) -> Iterator[str]:
    """Boot ``nexus-cluster --no-tls`` and yield the ``host:port`` address.

    Resolves the binary from the worktree's Rust build first (target/
    release|debug) so the test exercises the code under review, not a
    stale system install. Set ``NEXUS_E2E_ALLOW_SYSTEM_CLUSTER=1`` to
    fall back to ``$PATH`` (CI use).

    Teardown sends SIGTERM and waits up to 5s for graceful exit before
    SIGKILL. All resources (log file handle, process) are cleaned up
    even when readiness fails before yield.
    """
    nexus_cluster = _resolve_worktree_cluster_binary()
    if not nexus_cluster:
        pytest.skip(
            "nexus-cluster binary not in worktree rust/target (build with "
            "`cargo build -p nexus-profiles-cluster`) — or set "
            "NEXUS_E2E_ALLOW_SYSTEM_CLUSTER=1 to use the PATH binary"
        )

    data_dir = tmp_path / "data"
    log_path = tmp_path / "cluster.log"
    log_handle = log_path.open("wb")
    proc: subprocess.Popen | None = None
    addr: str | None = None

    def _cleanup() -> None:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
        log_handle.close()

    try:
        # Try a few ephemeral ports — there's an unavoidable TOCTOU
        # window between picking a free port and ``nexus-cluster``
        # actually binding it. Retrying makes the race tolerable.
        last_err: str = ""
        for _attempt in range(5):
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            finally:
                s.close()
            candidate_addr = f"127.0.0.1:{port}"
            proc = subprocess.Popen(
                [
                    nexus_cluster,
                    "--no-tls",
                    # Self-address must match the loopback bind, else the
                    # single-node raft leader resolves its self-address to the
                    # machine hostname (gethostname) — which doesn't map to
                    # 127.0.0.1 — and "Forward to leader failed (unreachable)"
                    # keeps the zone from going healthy, so the typed VFS gRPC
                    # service never becomes usable.
                    "--hostname",
                    "127.0.0.1",
                    "--bind-addr",
                    candidate_addr,
                    "--data-dir",
                    str(data_dir),
                    "--bootstrap-mode",
                    "static",
                ],
                # Capture the daemon's own logs into cluster.log so a boot
                # failure is diagnosable (the bin crate target is
                # `nexusd_cluster`).
                env={
                    **os.environ,
                    "RUST_LOG": os.environ.get("RUST_LOG") or "info,nexus_raft=info",
                },
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )

            # Wait for the port to accept connections (~1–2s boot).
            deadline = time.monotonic() + 20
            bound = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    last_err = (
                        f"nexus-cluster exited early (rc={proc.returncode}); "
                        f"log: {log_path.read_text()[-400:]}"
                    )
                    break
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        bound = True
                        break
                except OSError:
                    time.sleep(0.2)
            if bound:
                addr = candidate_addr
                break
            # This attempt failed — terminate before trying a new port.
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            proc = None

        if addr is None:
            raise AssertionError(
                f"nexus-cluster failed to bind any of 5 ephemeral ports; "
                f"last err: {last_err or 'timed out'}"
            )

        yield addr
    except BaseException:
        _cleanup()
        raise
    else:
        _cleanup()


@requires_e2e
def test_typed_grpc_ping_write_read_delete_batch(cluster_grpc):
    """Exercise the typed contract end-to-end against a real cluster.

    Asserts byte-identity on Read after Write, content_id stability,
    Delete success, and per-item BatchRead shape (Issue #4058).
    """
    import grpc

    from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

    ch = grpc.insecure_channel(cluster_grpc)
    stub = vfs_pb2_grpc.NexusVFSServiceStub(ch)

    # ---- Ping ---------------------------------------------------------------
    ping = stub.Ping(vfs_pb2.PingRequest(), timeout=10)
    assert ping.version, "Ping must return a non-empty version"
    assert ping.zone_id == "root"

    # ---- Write --------------------------------------------------------------
    body = b"typed-grpc-payload-xyz"
    w = stub.Write(vfs_pb2.WriteRequest(path="/g/a.txt", content=body), timeout=10)
    assert not w.is_error, w.error_payload
    assert w.size == len(body)
    assert w.content_id

    # Write a second file for BatchRead.
    body2 = b"second"
    w2 = stub.Write(vfs_pb2.WriteRequest(path="/g/b.txt", content=body2), timeout=10)
    assert not w2.is_error

    # ---- Read (byte-identical) ---------------------------------------------
    r = stub.Read(vfs_pb2.ReadRequest(path="/g/a.txt"), timeout=10)
    assert not r.is_error, r.error_payload
    assert r.content == body
    assert r.content_id == w.content_id

    # ---- BatchRead ----------------------------------------------------------
    br = stub.BatchRead(
        vfs_pb2.BatchReadRequest(
            items=[
                vfs_pb2.BatchReadItemRequest(path="/g/a.txt"),
                vfs_pb2.BatchReadItemRequest(path="/g/b.txt"),
            ],
        ),
        timeout=10,
    )
    assert len(br.results) == 2
    assert not br.results[0].is_error and br.results[0].content == body
    assert not br.results[1].is_error and br.results[1].content == body2

    # ---- Delete -------------------------------------------------------------
    d = stub.Delete(vfs_pb2.DeleteRequest(path="/g/a.txt"), timeout=10)
    assert d.success and not d.is_error

    # ---- Read after Delete returns is_error=True ---------------------------
    r2 = stub.Read(vfs_pb2.ReadRequest(path="/g/a.txt"), timeout=10)
    assert r2.is_error, "Read of deleted path must return is_error"


# ── bridge-2 (#4262): S3 / Cloudflare R2 DT_MOUNT over gRPC ──────────────────


def _r2_env() -> dict[str, str] | None:
    """Collect R2 / S3-compatible creds from the environment, or None.

    The Rust ``S3Backend`` is S3-compatible — Cloudflare R2 (and MinIO) are
    reached via a custom endpoint + region ``"auto"``. Set::

        NEXUS_R2_ENDPOINT           https://<acct>.r2.cloudflarestorage.com
        NEXUS_R2_ACCESS_KEY_ID
        NEXUS_R2_SECRET_ACCESS_KEY
        NEXUS_R2_BUCKET
        NEXUS_R2_REGION             (optional; defaults to "auto")
    """
    required = (
        "NEXUS_R2_ENDPOINT",
        "NEXUS_R2_ACCESS_KEY_ID",
        "NEXUS_R2_SECRET_ACCESS_KEY",
        "NEXUS_R2_BUCKET",
    )
    vals = {k: os.environ.get(k, "") for k in required}
    if not all(vals.values()):
        return None
    vals["NEXUS_R2_REGION"] = os.environ.get("NEXUS_R2_REGION", "auto")
    return vals


requires_r2 = pytest.mark.skipif(
    _r2_env() is None,
    reason=(
        "R2 E2E requires NEXUS_R2_ENDPOINT / NEXUS_R2_ACCESS_KEY_ID / "
        "NEXUS_R2_SECRET_ACCESS_KEY / NEXUS_R2_BUCKET"
    ),
)


@requires_e2e
@requires_r2
def test_s3_r2_dt_mount_builds_backend_and_round_trips(cluster_grpc):
    """bridge-2 (#4262) E2E — a Python S3 (Cloudflare R2) DT_MOUNT over gRPC
    reaches Rust, builds a live backend via ``ObjectStoreProvider``, and a
    subsequent write/read through Rust round-trips against real R2.

    Requires the cluster binary built with ``--features driver-s3`` (else the
    "s3" driver gate rejects the mount — surfaced here as a clear skip rather
    than a confusing failure).
    """
    import grpc

    from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

    env = _r2_env()
    assert env is not None  # guarded by @requires_r2

    ch = grpc.insecure_channel(cluster_grpc)
    stub = vfs_pb2_grpc.NexusVFSServiceStub(ch)

    mount = "/r2mnt"
    obj_path = f"{mount}/bridge2-e2e-{os.getpid()}.txt"
    body = b"cloudflare-r2-through-rust-" + str(os.getpid()).encode()

    # ---- DT_MOUNT (entry_type=2) S3/R2 → built via the provider ------------
    setattr_resp = stub.Setattr(
        vfs_pb2.SetattrRequest(
            path=mount,
            entry_type=2,
            backend_name="r2-e2e",
            backend_type="s3",
            s3_bucket=env["NEXUS_R2_BUCKET"],
            aws_region=env["NEXUS_R2_REGION"],
            aws_access_key=env["NEXUS_R2_ACCESS_KEY_ID"],
            aws_secret_key=env["NEXUS_R2_SECRET_ACCESS_KEY"],
            s3_endpoint=env["NEXUS_R2_ENDPOINT"],
        ),
        timeout=30,
    )
    if setattr_resp.is_error:
        payload = setattr_resp.error_payload.decode("utf-8", "replace")
        if "not enabled" in payload:
            pytest.skip(
                "cluster binary lacks the s3 driver — rebuild with "
                "`cargo build -p nexus-cluster --features driver-s3`"
            )
        pytest.fail(f"S3 DT_MOUNT failed: {payload}")
    assert setattr_resp.created, "S3 mount must build a live backend (created=true)"

    try:
        # ---- Write through Rust → R2 ---------------------------------------
        w = stub.Write(vfs_pb2.WriteRequest(path=obj_path, content=body), timeout=30)
        assert not w.is_error, w.error_payload
        assert w.size == len(body)

        # ---- Read back from R2 (byte-identical) ----------------------------
        r = stub.Read(vfs_pb2.ReadRequest(path=obj_path), timeout=30)
        assert not r.is_error, r.error_payload
        assert r.content == body, "read-back bytes differ — R2 round-trip broken"
    finally:
        # Best-effort cleanup of the test object.
        with contextlib.suppress(Exception):
            stub.Delete(vfs_pb2.DeleteRequest(path=obj_path), timeout=30)
