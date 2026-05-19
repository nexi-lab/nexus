"""Subprocess smoke test: `nexusd --profile sandbox` boot story (Issue #4126).

Distinct from tests/integration/test_sandbox_boot.py (Issue #3778), which
boots in-process via `nexus.connect()`. This test boots the *real daemon
process* and exercises the readiness file, real HTTP socket, gRPC Ping,
and process RSS — the surfaces a `nexus up --profile sandbox` operator
actually touches. No PostgreSQL, Dragonfly/Redis, or Zoekt is started by
this harness; the daemon must boot without them.

Marked slow + integration. Serial via xdist_group (shared free-port range
and the per-test HOME-scoped readiness file).
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import grpc
import httpx
import pytest

from nexus.cli.exit_codes import ExitCode
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

pytestmark = [
    pytest.mark.slow,
    pytest.mark.integration,
    pytest.mark.xdist_group(name="sandbox_boot_smoke"),
]

BOOT_TIMEOUT_S = 90.0  # cold interpreter + Rust kernel init; generous for CI


def _free_port() -> int:
    """Return an OS-assigned free TCP port (best-effort; race-tolerant)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_sandbox_daemon(tmp_path: Path, port: int) -> tuple[subprocess.Popen[bytes], Path, Path]:
    """Spawn `nexusd --profile sandbox` with an isolated HOME + data dir.

    Returns (process, ready_file_path, log_file_path). The HOME override
    scopes `~/.nexus/nexusd.ready` per-test (parallel-safe).
    """
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    data_dir = tmp_path / "data"
    for d in (home, workspace, data_dir, home / ".nexus"):
        d.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("NEXUS_PROFILE", None)
    env.pop("NEXUS_HOSTNAME", None)  # ensure no federation/Raft trigger
    env.pop("NEXUS_HUB_URL", None)
    env.pop("NEXUS_HUB_TOKEN", None)

    log_path = tmp_path / "nexusd.log"
    log_fh = log_path.open("wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "nexus.daemon.main",
            "--profile",
            "sandbox",
            "--workspace",
            str(workspace),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--data-dir",
            str(data_dir),
        ],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    ready_file = home / ".nexus" / "nexusd.ready"
    return proc, ready_file, log_path


def _wait_ready(proc: subprocess.Popen[bytes], ready_file: Path, log_path: Path) -> tuple[str, int]:
    """Poll the readiness file until it appears; return (host, port)."""
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log = log_path.read_text(errors="replace")
            # #4146: a fresh env without the built Rust extension errors
            # with ModuleNotFoundError. Skip cleanly instead of ERRORing.
            if "No module named 'nexus_runtime'" in log:
                pytest.skip(
                    "nexus_runtime extension not built — see #4146; build via maturin then re-run"
                )
            raise AssertionError(f"nexusd exited early (code {proc.returncode}). Log:\n{log}")
        if ready_file.exists():
            content = ready_file.read_text().strip()
            host, _, port_s = content.partition(":")
            return host, int(port_s)
        time.sleep(0.25)
    log = log_path.read_text(errors="replace")
    raise AssertionError(f"nexusd not ready within {BOOT_TIMEOUT_S}s. Log:\n{log}")


@pytest.fixture()
def sandbox_daemon(tmp_path: Path):
    """Boot a sandbox daemon for the test module; tear it down after."""
    port = _free_port()
    proc, ready_file, log_path = _spawn_sandbox_daemon(tmp_path, port)
    try:
        host, ready_port = _wait_ready(proc, ready_file, log_path)
        yield {
            "proc": proc,
            "host": host,
            "http_port": ready_port,
            "grpc_port": ready_port + 2,
            "log_path": log_path,
            # Additive (#4126 Task 8b): the daemon's isolated readiness file
            # so subprocess CLI tests can point `--readiness-file` at it
            # (they inherit the test runner's HOME, not the daemon's).
            "ready_file": ready_file,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_sandbox_daemon_boots_and_writes_readiness(sandbox_daemon) -> None:
    """The daemon process boots with no external services and is ready."""
    proc = sandbox_daemon["proc"]
    assert proc.poll() is None, "daemon should still be running after readiness"
    assert sandbox_daemon["host"] == "127.0.0.1"
    assert sandbox_daemon["http_port"] > 0

    log = Path(sandbox_daemon["log_path"]).read_text(errors="replace").lower()
    # The harness starts NO Postgres/Dragonfly/Redis/Zoekt. The sandbox
    # profile must not even attempt them: a forbidden service name must
    # not co-occur with any connection-failure marker in the boot log.
    failure_markers = (
        "connection refused",
        "could not connect",
        "connectionrefusederror",
        "connection error",
        "timed out",
    )
    for forbidden in ("postgres", "dragonfly", "zoekt", "redis"):
        if forbidden in log:
            offending = [m for m in failure_markers if m in log]
            assert not offending, (
                f"sandbox appears to have attempted '{forbidden}' "
                f"(failure markers {offending} present); log:\n{log}"
            )


def test_sandbox_uses_no_external_service_drivers(sandbox_daemon) -> None:
    """Positive contract: sandbox runs with zero external-service drivers.

    Strengthens the log-heuristic in
    ``test_sandbox_daemon_boots_and_writes_readiness`` (kept as
    defense-in-depth) into two positive assertions:

    1. ``/api/v2/features`` reports ``profile == "sandbox"`` and the enabled
       brick set excludes every external-service-implying brick
       (``pay``/``llm``/``observability``/``federation``), proving no
       postgres/dragonfly/zoekt-backed brick is active.
    2. Process-level: the daemon process (and any children) holds zero
       ESTABLISHED/SYN_SENT TCP connections to the default external-service
       remote ports — PostgreSQL 5432, Redis/Dragonfly 6379, Zoekt 6070.

    The connection check only flags ESTABLISHED/SYN_SENT to those *remote*
    ports, so localhost HTTP / the daemon's own listen sockets never
    trip it (no flakiness). ``psutil`` is optional: if absent only the
    process-level sub-check is skipped, not the whole test.
    """
    base = f"http://{sandbox_daemon['host']}:{sandbox_daemon['http_port']}"
    with httpx.Client(base_url=base, timeout=10.0) as client:
        r = _http_get_with_retry(client, "/api/v2/features")
        assert r.status_code == 200, r.text
        body = r.json()

    assert body["profile"] == "sandbox", body
    enabled = set(body["enabled_bricks"])
    # No external-service-implying brick is enabled in sandbox: pay/llm/
    # observability/federation each pull a postgres/dragonfly/zoekt-class
    # dependency in non-sandbox profiles.
    for forbidden in ("pay", "llm", "observability", "federation"):
        assert forbidden not in enabled, (
            f"sandbox must not enable external-service brick '{forbidden}'; "
            f"enabled={sorted(enabled)}"
        )

    # Process-level positive proof: no live connection to default external
    # service ports. Optional dependency — skip only this sub-check.
    psutil = pytest.importorskip("psutil")

    external_remote_ports = {5432, 6379, 6070}
    live_states = {psutil.CONN_ESTABLISHED, psutil.CONN_SYN_SENT}

    proc = psutil.Process(sandbox_daemon["proc"].pid)
    procs = [proc]
    with contextlib.suppress(psutil.Error):
        procs.extend(proc.children(recursive=True))

    offending = []
    for p in procs:
        try:
            conns = p.net_connections(kind="tcp")
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        for c in conns:
            if c.status in live_states and c.raddr and c.raddr.port in external_remote_ports:
                offending.append((p.pid, c.status, c.raddr.ip, c.raddr.port))

    assert not offending, (
        f"sandbox daemon holds live TCP connections to external-service "
        f"ports {sorted(external_remote_ports)}: {offending}"
    )


def _http_get_with_retry(client, path, *, attempts=40, delay=0.5):
    """GET `path`, retrying transient connection errors.

    The daemon writes its readiness file before the HTTP socket is
    actually listening (src/nexus/daemon/main.py:514 precedes :520), so
    the first requests after readiness may be refused. Retry for a
    bounded window before giving up.
    """
    import time as _t

    last_exc = None
    for _ in range(attempts):
        try:
            return client.get(path)
        except httpx.TransportError as exc:  # connect refused / reset
            last_exc = exc
            _t.sleep(delay)
    raise AssertionError(f"GET {path} never succeeded after {attempts} attempts: {last_exc!r}")


def test_sandbox_http_surface_over_real_socket(sandbox_daemon) -> None:
    """`/health` 200 and `/api/v2/features` reports profile=sandbox.

    Real TCP socket (not ASGI in-process) — this is the value-add over
    tests/integration/test_sandbox_boot.py.
    """
    base = f"http://{sandbox_daemon['host']}:{sandbox_daemon['http_port']}"
    with httpx.Client(base_url=base, timeout=10.0) as client:
        r = _http_get_with_retry(client, "/health")
        assert r.status_code == 200, r.text

        r = _http_get_with_retry(client, "/api/v2/features")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["profile"] == "sandbox", body

        enabled = set(body["enabled_bricks"])
        expected_subset = {
            "search",
            "mcp",
            "parsers",
            "eventlog",
            "namespace",
            "permissions",
        }
        assert expected_subset.issubset(enabled), (
            f"sandbox missing bricks {expected_subset - enabled}; enabled={sorted(enabled)}"
        )
        for forbidden in ("llm", "pay", "observability", "federation"):
            assert forbidden not in enabled, (
                f"sandbox must not enable '{forbidden}'; enabled={sorted(enabled)}"
            )


def test_sandbox_does_not_bind_typed_vfs_grpc(sandbox_daemon) -> None:
    """Sandbox does NOT bind the typed VFS gRPC server (root-caused #4126).

    ROOT CAUSE (verified, definitive — encoded here as a contract):
    The typed VFS gRPC service ``NexusVfsService`` (serving Ping/Read/Write,
    defined in ``rust/transport/src/grpc.rs``) has exactly ONE server spawn
    call site in the whole repo — ``rust/profiles/cluster/src/main.rs:422``,
    the *cluster* profile binary. No call site exists in the sandbox path
    (``rust/``, ``src/nexus/``). The only Python gRPC server is the
    env-gated approvals brick (``src/nexus/server/lifespan/approvals.py``),
    which is not in the sandbox profile and has no ``Ping``. What the
    sandbox profile *does* start is the Raft/federation gRPC on the fixed
    port :2126 (``rust/raft/src/transport/server.rs``) — a different
    surface, not VFS ``Ping``, and not at ``http_port + 2``.

    Therefore the typed VFS gRPC ``Ping`` is **unavailable in sandbox by
    architecture (cluster-profile-only)** — empirically reproduced as
    connection-refused on ``http_port + 2`` for the daemon's lifetime while
    the daemon is otherwise healthy. #4148's "no-auth VFS Ping returns
    UNAUTHENTICATED" does NOT reproduce in sandbox because no VFS gRPC
    server exists there to return anything.

    This test PASSES while the contract holds and would FAIL if a future
    change made sandbox bind the typed VFS gRPC server — exactly the
    regression signal we want.
    """
    target = f"{sandbox_daemon['host']}:{sandbox_daemon['grpc_port']}"
    channel = grpc.insecure_channel(target)
    try:
        # The VFS gRPC server is cluster-profile-only, so the channel must
        # never become ready within a generous-but-bounded window.
        with pytest.raises(grpc.FutureTimeoutError):
            grpc.channel_ready_future(channel).result(timeout=8)

        # And an actual Ping attempt must fail with an unavailable/
        # connection error (no server bound to answer it in sandbox).
        stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.Ping(vfs_pb2.PingRequest(auth_token=""), timeout=5)
        assert excinfo.value.code() in (
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.DEADLINE_EXCEEDED,
        ), (
            f"expected UNAVAILABLE/DEADLINE_EXCEEDED (no VFS gRPC server in "
            f"sandbox), got {excinfo.value.code()}: {excinfo.value}"
        )
    finally:
        channel.close()


def test_sandbox_does_not_bootstrap_federation_or_raft(sandbox_daemon) -> None:
    """Sandbox must NOT bootstrap Raft federation (Issue #4126 — the fix).

    ROOT CAUSE: ``rust/raft/src/distributed_coordinator.rs::install()`` —
    the single per-process chokepoint, called from the cdylib boot path —
    unconditionally built the real ``RaftDistributedCoordinator``, set it on
    the kernel, and ran ``init_from_env``. ``init_from_env`` derives a
    hostname from ``NEXUS_HOSTNAME`` *or the system ``hostname`` fallback*,
    then binds a Raft gRPC server on ``0.0.0.0:2126`` and logs "federation
    bootstrap complete" — even with ``NEXUS_HOSTNAME`` unset (the
    ``_spawn_sandbox_daemon`` fixture explicitly unsets it). That violates
    the sandbox profile contract (lightweight / no federation / no external
    services).

    THE FIX: the sandbox daemon boot path sets ``NEXUS_FEDERATION_DISABLED=1``
    before ``nexus.connect(...)``, and ``install()`` early-returns on that
    var, keeping the kernel's default ``NoopDistributedCoordinator``. So no
    ZoneManager, no Raft gRPC :2126, no federation bootstrap — while the
    daemon is otherwise fully healthy.

    PRE-FIX this test FAILS (the boot log contained these markers).
    POST-FIX it PASSES. Deterministic: pure log-substring assertions, plus
    a positive health check that sandbox still works without federation.
    """
    log = Path(sandbox_daemon["log_path"]).read_text(errors="replace")

    forbidden_markers = (
        "federation bootstrap complete",
        "Starting Raft gRPC server",
        "ZoneManager node",
        ":2126",
    )
    present = [m for m in forbidden_markers if m in log]
    assert not present, (
        f"sandbox bootstrapped Raft/federation (markers {present} present in "
        f"boot log) — the #4126 kill-switch did not take effect. Log:\n{log}"
    )

    # Positive: sandbox boots fully WITHOUT federation. Process alive and
    # /health 200 (readiness already gated by the fixture).
    assert sandbox_daemon["proc"].poll() is None, (
        "daemon must remain healthy after booting without federation"
    )
    base = f"http://{sandbox_daemon['host']}:{sandbox_daemon['http_port']}"
    with httpx.Client(base_url=base, timeout=10.0) as client:
        r = _http_get_with_retry(client, "/health")
        assert r.status_code == 200, r.text


RSS_CEILING_MB = 800  # loose gross-regression guard, not a tuned baseline
# Boot-to-readiness varies widely across cold Rust-kernel init and CI
# load (empirically ~7-105s in this suite). This ceiling only guards
# against gross regressions of a setup path; it is intentionally loose.
WARM_BOOT_CEILING_S = 150.0


def _spawn_and_time(tmp_path: Path) -> tuple[float, float | None, object]:
    """Spawn a sandbox daemon, time boot-to-readiness, sample RSS.

    Returns (boot_seconds, rss_mb_or_None, proc). Caller must terminate
    the returned process.
    """
    psutil = pytest.importorskip("psutil")
    port = _free_port()
    t0 = time.monotonic()
    proc, ready_file, log_path = _spawn_sandbox_daemon(tmp_path, port)
    _wait_ready(proc, ready_file, log_path)
    boot_s = time.monotonic() - t0
    try:
        rss_mb = psutil.Process(proc.pid).memory_info().rss / (1024 * 1024)
    except Exception:
        rss_mb = None
    return boot_s, rss_mb, proc


def _terminate(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


@contextlib.contextmanager
def _timed_daemon(path: Path):
    """Spawn+time a sandbox daemon, guaranteeing teardown.

    Yields (boot_seconds, rss_mb_or_None). The daemon process is always
    terminated on exit, including if the body or spawn raises.
    """
    boot_s, rss_mb, proc = _spawn_and_time(path)
    try:
        yield boot_s, rss_mb
    finally:
        _terminate(proc)


def test_sandbox_boot_time_and_rss_within_loose_bounds(tmp_path: Path, record_property) -> None:
    """Measure cold + warm boot time and RSS; assert loose ceilings only.

    Boot is a setup path and RSS a resource budget — neither is a hot
    path. These bounds guard against gross regressions; the observed
    numbers are surfaced via record_property + stdout for the user guide.

    Here "boot" means time-to-readiness-file, which is written before the
    HTTP socket binds, so it deliberately undercounts full request-ready
    time — acceptable for a loose setup-path gross-regression guard.
    """
    pytest.importorskip("psutil")

    with _timed_daemon(tmp_path / "cold") as (cold_boot_s, rss_mb):
        pass

    with _timed_daemon(tmp_path / "warm") as (warm_boot_s, _):
        pass

    record_property("sandbox_cold_boot_s", round(cold_boot_s, 2))
    record_property("sandbox_warm_boot_s", round(warm_boot_s, 2))
    if rss_mb is not None:
        record_property("sandbox_rss_mb", round(rss_mb, 1))
    print(
        f"\n[#4126] cold_boot={cold_boot_s:.2f}s "
        f"warm_boot={warm_boot_s:.2f}s "
        f"rss={'n/a' if rss_mb is None else f'{rss_mb:.1f}MB'}"
    )

    assert warm_boot_s < WARM_BOOT_CEILING_S, (
        f"warm boot {warm_boot_s:.2f}s exceeds loose {WARM_BOOT_CEILING_S}s "
        f"gross-regression ceiling"
    )
    if rss_mb is not None:
        assert rss_mb < RSS_CEILING_MB, (
            f"RSS {rss_mb:.1f}MB exceeds loose {RSS_CEILING_MB}MB ceiling"
        )


def test_sandbox_flag_without_profile_is_rejected_by_daemon() -> None:
    """`--workspace` without `--profile sandbox` is a usage error.

    Parity with tests/unit/cli/test_stack_sandbox.py, asserted against
    the real daemon process (end-to-end gating, not just Click). This also
    pins the `__main__` guard in `src/nexus/daemon/main.py` — without it
    `python -m nexus.daemon.main` exits 0 and this test fails, surfacing
    the regression.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "nexus.daemon.main",
            "--workspace",
            "/tmp/should-not-be-allowed",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == ExitCode.USAGE_ERROR, (
        f"daemon must reject --workspace without --profile sandbox with "
        f"USAGE_ERROR; got returncode={proc.returncode} "
        f"stdout={proc.stdout} stderr={proc.stderr}"
    )
    combined = (proc.stdout + proc.stderr).lower()
    assert "sandbox" in combined, (
        f"error should mention sandbox profile requirement; "
        f"stdout={proc.stdout} stderr={proc.stderr}"
    )


def test_nexus_ready_reports_sandbox_daemon_ready(sandbox_daemon) -> None:
    """`nexus ready` reports the booted sandbox daemon as ready (real e2e).

    Invoked via `python -m nexus.cli` (the package exposes
    `src/nexus/cli/__main__.py`), mirroring how this module already runs
    the daemon as `python -m nexus.daemon.main`. This is robust under
    `uv run pytest` without relying on a console-script being on PATH.

    `--readiness-file` is pointed at the fixture's isolated readiness file:
    this subprocess inherits the test runner's HOME, NOT the daemon's
    per-test isolated HOME, so the default `~/.nexus/nexusd.ready` would be
    wrong here.
    """
    import json as _json

    ready_file = sandbox_daemon["ready_file"]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "nexus.cli",
            "ready",
            "--readiness-file",
            str(ready_file),
            "--json",
            "--timeout",
            "30",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, (
        f"nexus ready should exit 0; rc={result.returncode} "
        f"stdout={result.stdout} stderr={result.stderr}"
    )
    payload = _json.loads(result.stdout)
    data = payload.get("data", payload)
    assert data["ready"] is True, data
    assert data["profile"] == "sandbox", data
    assert data["endpoint"] == (f"{sandbox_daemon['host']}:{sandbox_daemon['http_port']}"), data


def test_sandbox_up_state_is_consumed_by_status(sandbox_daemon, tmp_path: Path) -> None:
    """#4144: the state `nexus up --profile sandbox` persists is consumable.

    Genuine `nexus up --profile sandbox` blocks on the foreground daemon,
    so we assert the *state-consumption path* at functional level instead:
    write the exact `.state.json` shape the up-path produces (see
    src/nexus/cli/commands/stack.py sandbox branch), then prove `nexus
    status --json` pointed at the persisted HTTP URL reports the real
    booted sandbox daemon reachable. This closes the readiness/discovery
    gap end-to-end without a blocking foreground process.
    """
    import json as _json

    host = sandbox_daemon["host"]
    http_port = sandbox_daemon["http_port"]
    grpc_port = sandbox_daemon["grpc_port"]
    data_dir = tmp_path / "consume-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    workspace = str(tmp_path / "consume-ws")

    # Mirror exactly what the sandbox `up` path writes (#4144). This test
    # validates the *consumer* contract only (state → env/status); the
    # *producer* side — that stack.py emits exactly these keys
    # (profile/workspace/ports.{http,grpc}/grpc_host, hub_token absent) —
    # is locked by TestSandboxStateDictShape in
    # tests/unit/cli/test_stack_sandbox.py. Keep the two in sync.
    (data_dir / ".state.json").write_text(
        _json.dumps(
            {
                "version": 1,
                "profile": "sandbox",
                "workspace": workspace,
                "ports": {"http": http_port, "grpc": grpc_port},
                "grpc_host": host,
            }
        )
    )

    # `nexus env` (consumes state via load_runtime_state) emits the conn
    # vars derived purely from the persisted state.
    from nexus.cli.state import load_runtime_state, resolve_connection_env

    env_vars = resolve_connection_env({}, load_runtime_state(data_dir))
    assert env_vars["NEXUS_PROFILE"] == "sandbox"
    assert env_vars["NEXUS_WORKSPACE"] == workspace
    assert env_vars["NEXUS_GRPC_PORT"] == str(grpc_port)
    assert f":{http_port}" in env_vars["NEXUS_URL"]

    # `nexus status --json` pointed at the persisted HTTP URL reports the
    # real booted sandbox daemon reachable.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "nexus.cli",
            "status",
            "--url",
            f"http://{host}:{http_port}",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"nexus status should exit 0; rc={result.returncode} "
        f"stdout={result.stdout} stderr={result.stderr}"
    )
    status_payload = _json.loads(result.stdout)
    status_data = status_payload.get("data", status_payload)
    assert status_data["server_reachable"] is True, status_data
    assert status_data["server_url"] == f"http://{host}:{http_port}", status_data
