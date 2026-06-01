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


# ── bridge-3 (#4263): S3 mount declared via NEXUS_S3_* at startup ─────────────


def _s3_env_from_r2(env_r2: dict[str, str]) -> dict[str, str]:
    """Map the test's ``NEXUS_R2_*`` creds onto the daemon's ``NEXUS_S3_*``
    startup-mount surface (mount point ``/s3``)."""
    return {
        "NEXUS_S3_BUCKET": env_r2["NEXUS_R2_BUCKET"],
        "NEXUS_S3_REGION": env_r2["NEXUS_R2_REGION"],
        "NEXUS_S3_ACCESS_KEY_ID": env_r2["NEXUS_R2_ACCESS_KEY_ID"],
        "NEXUS_S3_SECRET_ACCESS_KEY": env_r2["NEXUS_R2_SECRET_ACCESS_KEY"],
        "NEXUS_S3_ENDPOINT": env_r2["NEXUS_R2_ENDPOINT"],
        "NEXUS_S3_MOUNT": "/s3",
    }


def _spawn_cluster_s3(
    binary: str,
    data_dir: Path,
    s3_env: dict[str, str],
    log_handle,
    log_path: Path,
) -> tuple[subprocess.Popen, str]:
    """Boot ``nexus-cluster`` with an S3 startup mount on ``data_dir`` and
    return ``(proc, "host:port")`` once the gRPC port accepts connections.

    Retries a few ephemeral ports (TOCTOU between pick and bind). Raises
    ``AssertionError`` if it never binds; calls ``pytest.skip`` when the
    binary lacks the ``driver-s3`` arm (so the gate is a clean skip, not a
    confusing failure).
    """
    last_err = ""
    for _attempt in range(5):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        finally:
            s.close()
        addr = f"127.0.0.1:{port}"
        proc = subprocess.Popen(
            [
                binary,
                "--no-tls",
                "--hostname",
                "127.0.0.1",
                "--bind-addr",
                addr,
                "--data-dir",
                str(data_dir),
                "--bootstrap-mode",
                "static",
            ],
            env={
                **os.environ,
                **s3_env,
                "RUST_LOG": os.environ.get("RUST_LOG") or "info,nexus_raft=info",
            },
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log_text = log_path.read_text()
                last_err = (
                    f"nexus-cluster exited early (rc={proc.returncode}); log: {log_text[-600:]}"
                )
                # A driver-not-compiled exit is an explicit skip signal.
                if "not enabled" in log_text:
                    pytest.skip(
                        "cluster binary lacks the s3 driver — rebuild with "
                        "`cargo build -p nexus-cluster --features driver-s3`"
                    )
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return proc, addr
            except OSError:
                time.sleep(0.2)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
    raise AssertionError(
        f"nexus-cluster (S3 startup) failed to bind; last err: {last_err or 'timed out'}"
    )


def _terminate(proc: subprocess.Popen | None) -> None:
    """SIGTERM then SIGKILL-after-grace teardown for a spawned daemon."""
    if proc is None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


@pytest.fixture()
def cluster_grpc_s3(tmp_path: Path) -> Iterator[str]:
    """Boot ``nexus-cluster`` with ``NEXUS_S3_*`` set so the daemon mounts
    an S3-compatible backend at ``/s3`` at startup, and yield ``host:port``.

    Skips cleanly when the binary or R2 creds are absent. Maps the test's
    ``NEXUS_R2_*`` creds onto the daemon's ``NEXUS_S3_*`` surface.
    """
    nexus_cluster = _resolve_worktree_cluster_binary()
    if not nexus_cluster:
        pytest.skip(
            "nexus-cluster binary not in worktree rust/target (build with "
            "`cargo build -p nexus-cluster --features driver-s3`)"
        )
    env_r2 = _r2_env()
    if env_r2 is None:
        pytest.skip("startup S3 mount E2E requires NEXUS_R2_* creds")

    data_dir = tmp_path / "data"
    log_path = tmp_path / "cluster.log"
    log_handle = log_path.open("wb")
    proc: subprocess.Popen | None = None
    s3_env = _s3_env_from_r2(env_r2)

    try:
        proc, addr = _spawn_cluster_s3(nexus_cluster, data_dir, s3_env, log_handle, log_path)
        yield addr
    finally:
        _terminate(proc)
        log_handle.close()


@requires_e2e
@requires_r2
def test_startup_s3_mount_round_trips(cluster_grpc_s3):
    """bridge-3 (#4263) E2E — the daemon mounts S3/R2 at ``/s3`` from
    ``NEXUS_S3_*`` config at startup (no gRPC Setattr), and a write/read
    through that mount round-trips against real R2.
    """
    import grpc

    from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

    ch = grpc.insecure_channel(cluster_grpc_s3)
    stub = vfs_pb2_grpc.NexusVFSServiceStub(ch)

    obj_path = f"/s3/bridge3-startup-{os.getpid()}.txt"
    body = b"startup-s3-mount-through-rust-" + str(os.getpid()).encode()

    try:
        w = stub.Write(vfs_pb2.WriteRequest(path=obj_path, content=body), timeout=30)
        assert not w.is_error, w.error_payload
        assert w.size == len(body)

        r = stub.Read(vfs_pb2.ReadRequest(path=obj_path), timeout=30)
        assert not r.is_error, r.error_payload
        assert r.content == body, "read-back bytes differ — R2 round-trip broken"
    finally:
        with contextlib.suppress(Exception):
            stub.Delete(vfs_pb2.DeleteRequest(path=obj_path), timeout=30)


@requires_e2e
@requires_r2
def test_startup_s3_mount_survives_restart(tmp_path: Path):
    """bridge-3 (#4263) — metadata for objects written through the startup
    S3 mount survives a daemon restart on the same data dir.

    Guards against the startup mount's path→content_id metadata landing in a
    non-durable boot metastore: write via ``/s3`` in one process, stop the
    daemon, reboot it on the *same* data dir + bucket config, and read the
    same path back. A regression that routed startup-mount metadata to a
    throwaway store would write+read fine in one process (the round-trip
    test) but fail this read-after-restart.
    """
    import grpc

    from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

    binary = _resolve_worktree_cluster_binary()
    if not binary:
        pytest.skip(
            "nexus-cluster binary not built (`cargo build -p nexus-cluster --features driver-s3`)"
        )
    env_r2 = _r2_env()
    assert env_r2 is not None  # guarded by @requires_r2
    s3_env = _s3_env_from_r2(env_r2)

    data_dir = tmp_path / "data"
    obj_path = f"/s3/bridge3-restart-{os.getpid()}.txt"
    body = b"startup-s3-survives-restart-" + str(os.getpid()).encode()

    # ---- Boot 1: write through /s3, confirm the round-trip ----
    log1 = tmp_path / "boot1.log"
    h1 = log1.open("wb")
    proc1: subprocess.Popen | None = None
    try:
        proc1, addr1 = _spawn_cluster_s3(binary, data_dir, s3_env, h1, log1)
        stub1 = vfs_pb2_grpc.NexusVFSServiceStub(grpc.insecure_channel(addr1))
        w = stub1.Write(vfs_pb2.WriteRequest(path=obj_path, content=body), timeout=30)
        assert not w.is_error, w.error_payload
        assert w.size == len(body)
    finally:
        _terminate(proc1)
        h1.close()

    # ---- Boot 2: SAME data dir, fresh process — metadata must persist ----
    log2 = tmp_path / "boot2.log"
    h2 = log2.open("wb")
    proc2: subprocess.Popen | None = None
    addr2: str | None = None
    try:
        proc2, addr2 = _spawn_cluster_s3(binary, data_dir, s3_env, h2, log2)
        stub2 = vfs_pb2_grpc.NexusVFSServiceStub(grpc.insecure_channel(addr2))
        r = stub2.Read(vfs_pb2.ReadRequest(path=obj_path), timeout=30)
        assert not r.is_error, (
            "read after restart failed — startup S3 mount metadata is not "
            f"durable across restart: {r.error_payload!r}"
        )
        assert r.content == body, "read-back bytes differ after restart"
    finally:
        if addr2 is not None:
            with contextlib.suppress(Exception):
                stub_cleanup = vfs_pb2_grpc.NexusVFSServiceStub(grpc.insecure_channel(addr2))
                stub_cleanup.Delete(vfs_pb2.DeleteRequest(path=obj_path), timeout=30)
        _terminate(proc2)
        h2.close()
