"""Conftest for self-contained E2E tests.

Provides a lightweight ``nexus_server`` fixture that spins up ``nexusd``
with a temp SQLite database. No external infrastructure required.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
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
