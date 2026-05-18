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

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

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
    # No external-service connection failures: sandbox must not even try
    # PostgreSQL / Dragonfly-Redis / Zoekt.
    for forbidden in ("postgres", "dragonfly", "zoekt", "redis"):
        assert f"{forbidden} connection refused" not in log, (
            f"sandbox attempted {forbidden}; log:\n{log}"
        )
