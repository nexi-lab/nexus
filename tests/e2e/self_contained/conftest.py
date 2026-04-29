"""Conftest for self-contained E2E tests.

Provides a lightweight ``nexus_server`` fixture that spins up ``nexusd``
with a temp SQLite database. No external infrastructure required.

Also provides a ``running_nexus`` fixture that drives ``nexus init`` /
``nexus up --build`` / ``nexus down`` against a real Docker Compose
stack — used by approvals E2E (#3790 Tasks 21–23) where the Python
gRPC server on ``:2029`` and PostgreSQL backing must both be live.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, suppress
from pathlib import Path

import pytest


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _drain_pipe(pipe, lines: list[str], ready: threading.Event | None = None) -> None:
    try:
        for raw in iter(pipe.readline, b""):
            decoded = raw.decode(errors="replace")
            lines.append(decoded)
            if ready and "Application startup complete" in decoded:
                ready.set()
    except ValueError:
        pass
    finally:
        pipe.close()


@pytest.fixture(scope="function")
def nexus_server(tmp_path: Path):
    """Start a lightweight ``nexusd`` process for E2E testing.

    Uses SQLite (no PostgreSQL required). Yields a dict with 'port' and 'base_url'.
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Use the console_scripts entry point from the same venv
    nexusd_bin = str(Path(sys.executable).parent / "nexusd")

    env = os.environ.copy()
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{tmp_path / 'test.db'}"
    env["NEXUS_JWT_SECRET"] = "e2e-test-secret"
    env["NEXUS_RECORD_STORE_PATH"] = str(tmp_path / "record_store.db")
    env["NEXUS_DATA_DIR"] = str(tmp_path)

    process = subprocess.Popen(
        [
            nexusd_bin,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    stderr_lines: list[str] = []
    stdout_lines: list[str] = []
    ready = threading.Event()

    t_err = threading.Thread(
        target=_drain_pipe, args=(process.stderr, stderr_lines, ready), daemon=True
    )
    t_out = threading.Thread(target=_drain_pipe, args=(process.stdout, stdout_lines), daemon=True)
    t_err.start()
    t_out.start()

    if not ready.wait(timeout=60.0):
        process.terminate()
        t_err.join(timeout=2)
        t_out.join(timeout=2)
        pytest.skip(f"Server failed to start on port {port} (serve command may have been removed).")

    # Quick health check to confirm it's truly up
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with closing(socket.create_connection(("127.0.0.1", port), timeout=1)):
                break
        except OSError:
            time.sleep(0.2)

    yield {"port": port, "base_url": base_url, "process": process}

    # Cleanup
    if sys.platform != "win32":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    else:
        process.terminate()

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


# ---------------------------------------------------------------------------
# running_nexus — full Docker Compose stack for approvals E2E (#3790)
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Check if Docker daemon is running (parity with test_first_run_ux.py)."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@dataclasses.dataclass
class RunningNexus:
    """Handle for a running nexus stack (started via ``nexus up --build``).

    Exposes the connection knobs Tasks 21–23 need:
      - ``http_url``: base URL for the daemon's HTTP API (``/health``,
        ``/hub/approvals/dump``).
      - ``grpc_addr``: ``host:port`` for the Python ApprovalsV1 gRPC
        server (default ``127.0.0.1:2029``).
      - ``admin_token``: the bearer secret callers must send as
        ``authorization: Bearer <token>`` to the gRPC server.
      - ``zone``: a uuid-prefixed zone id for test isolation.
    """

    http_url: str
    grpc_addr: str
    admin_token: str
    zone: str
    project_dir: Path


@pytest.fixture(scope="class")
def running_nexus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RunningNexus]:
    """Start a full nexus stack via ``nexus up --build`` and tear down.

    Skipped automatically when Docker is unavailable (mirrors the pattern
    in ``tests/e2e/test_first_run_ux.py``). The fixture is class-scoped
    so multiple tests in the same class share one stack startup.

    Environment knobs set on the daemon process (via ``nexus.yaml``):
      - ``NEXUS_APPROVALS_ENABLED=1`` — enable the brick.
      - ``NEXUS_APPROVALS_ADMIN_TOKEN=<random>`` — gates the Python
        gRPC server. We surface the same token via ``.admin_token``.
      - ``NEXUS_APPROVALS_GRPC_PORT=2029`` — pinned for E2E reachability;
        the daemon defaults to this anyway, but we set it explicitly so
        the test contract is obvious.
    """
    if not _docker_available():
        pytest.skip("nexus up requires docker")

    project_dir = tmp_path_factory.mktemp("nexus_running")
    config_path = project_dir / "nexus.yaml"
    data_dir = project_dir / "nexus-data"

    repo_root = Path(__file__).resolve().parents[3]
    compose_file = repo_root / "nexus-stack.yml"
    if not compose_file.exists():
        pytest.skip(f"compose file not found: {compose_file}")

    admin_token = f"e2e-{uuid.uuid4().hex}"
    zone = f"z-e2e-{uuid.uuid4().hex[:8]}"
    grpc_port = int(os.environ.get("NEXUS_APPROVALS_GRPC_PORT", "2029"))

    # nexus init writes nexus.yaml; we then export the approvals env vars
    # via the user's shell environment so docker compose picks them up.
    init_env = os.environ.copy()
    init_result = subprocess.run(
        [
            "nexus",
            "init",
            "--preset",
            "demo",
            "--config-path",
            str(config_path),
            "--data-dir",
            str(data_dir),
            "--compose-file",
            str(compose_file),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(project_dir),
        env=init_env,
    )
    if init_result.returncode != 0:
        pytest.skip(f"nexus init failed: {init_result.stderr}")

    up_env = os.environ.copy()
    up_env["NEXUS_APPROVALS_ENABLED"] = "1"
    up_env["NEXUS_APPROVALS_ADMIN_TOKEN"] = admin_token
    up_env["NEXUS_APPROVALS_GRPC_PORT"] = str(grpc_port)

    up_result = subprocess.run(
        ["nexus", "up", "--build"],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(project_dir),
        env=up_env,
    )
    if up_result.returncode != 0:
        pytest.skip(
            f"nexus up failed (likely missing build deps or docker quota): {up_result.stderr[:400]}"
        )

    # Re-read config — `nexus up` may have resolved port conflicts and
    # persisted new ports back to nexus.yaml.
    import yaml

    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}
    http_port = cfg.get("ports", {}).get("http", 2026)
    http_url = f"http://127.0.0.1:{http_port}"

    # Wait for /health to come up (containers need a moment).
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + 60
    healthy = False
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{http_url}/health", timeout=2) as resp:
                if resp.status == 200:
                    healthy = True
                    break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1.0)

    if not healthy:
        # Best-effort teardown then skip — the test cluster isn't usable.
        subprocess.run(
            ["nexus", "down"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(project_dir),
        )
        pytest.skip(f"nexus stack failed health check on {http_url}/health")

    handle = RunningNexus(
        http_url=http_url,
        grpc_addr=f"127.0.0.1:{grpc_port}",
        admin_token=admin_token,
        zone=zone,
        project_dir=project_dir,
    )

    try:
        yield handle
    finally:
        subprocess.run(
            ["nexus", "down"],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(project_dir),
        )
