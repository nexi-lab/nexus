"""Pytest configuration and fixtures for e2e tests.

Provides fixtures for:
- isolated_db: Isolated SQLite database for each test
- nexus_server: Actual nexus serve process running on a free port
- test_client: httpx client for making real HTTP requests

These are TRUE e2e tests that start the actual server process.
"""

from __future__ import annotations

import gc
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import closing, suppress
from pathlib import Path

import httpx
import pytest

from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore

# Add src directory to Python path for local development
_src_path = Path(__file__).parent.parent.parent / "src"
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))


def find_free_port() -> int:
    """Find a free port on localhost."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    """Wait for server to be ready by polling /health endpoint."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            # trust_env=False prevents proxy interference with localhost
            response = httpx.get(f"{url}/health", timeout=1.0, trust_env=False)
            if response.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.1)
    return False


# Aggressive cleanup to prevent SQLite "database is locked" errors
@pytest.fixture(autouse=True)
def cleanup_gc():
    """Force garbage collection after each test."""
    yield
    gc.collect()


@pytest.fixture(scope="function")
def isolated_db(tmp_path, monkeypatch):
    """Create an isolated database path for tests that need guaranteed fresh state.

    This fixture ensures each test gets a completely unique database path
    to prevent any cross-test pollution. It also clears environment variables
    that could override the database path.

    Returns:
        Path: Unique database file path in temporary directory
    """
    # Clear environment variables that would override db_path
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    unique_id = str(uuid.uuid4())[:8]
    db_path = tmp_path / f"test_db_{unique_id}.db"

    yield db_path

    # Clean up database file after test
    if db_path.exists():
        from contextlib import suppress

        with suppress(Exception):  # Best effort cleanup
            db_path.unlink()


@pytest.fixture(scope="function")
def nexus_server(isolated_db, tmp_path):
    """Start actual nexus serve process for true e2e testing.

    This fixture:
    1. Creates storage directory and database
    2. Finds a free port
    3. Starts `nexus serve` as subprocess
    4. Waits for server to be ready
    5. Yields server info (port, base_url)
    6. Kills server process on cleanup

    Returns:
        dict with 'port', 'base_url', 'process'
    """
    # Set up environment
    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Environment for the server process
    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{isolated_db}"
    env["PYTHONPATH"] = str(_src_path)

    # Issue #1186: Enable lock manager if Dragonfly/Redis is available
    dragonfly_url = env.get("NEXUS_DRAGONFLY_URL") or env.get("REDIS_URL")
    if dragonfly_url:
        env["NEXUS_DRAGONFLY_COORDINATION_URL"] = dragonfly_url
        env["NEXUS_ALLOW_SINGLE_DRAGONFLY"] = "true"

    # Start nexus serve process
    # Using python -c to invoke the CLI entry point from source
    # --data-dir sets both storage path and database location
    # Uses FastAPI async server (default) for full API support including Graph API
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"from nexus.cli import main; main(['serve', '--host', '127.0.0.1', '--port', '{port}', '--data-dir', '{tmp_path}'])",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Use process group so we can kill all child processes
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    # Wait for server to be ready
    if not wait_for_server(base_url, timeout=30.0):
        # Server failed to start, get output for debugging
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"Server failed to start on port {port}.\n"
            f"stdout: {stdout.decode()}\n"
            f"stderr: {stderr.decode()}"
        )

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "db_path": isolated_db,
        "storage_path": storage_path,
    }

    # Cleanup: kill server process and all children
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


@pytest.fixture(scope="function")
def test_app(nexus_server):
    """Create httpx client for making real HTTP requests to the server.

    This is the main fixture tests should use. It provides an httpx client
    configured to talk to the running nexus server.

    Returns:
        httpx.Client configured with base_url
    """
    # trust_env=False prevents httpx from using system proxy settings
    # which can interfere with localhost connections
    with httpx.Client(base_url=nexus_server["base_url"], timeout=30.0, trust_env=False) as client:
        yield client


# Keep the old fixture for backward compatibility during transition
@pytest.fixture(scope="function")
def nexus_fs(isolated_db, tmp_path):
    """Create a NexusFS instance for testing (direct, no server).

    This is useful for tests that need direct access to NexusFS
    without going through HTTP.
    """
    os.environ["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"

    from nexus import NexusFS
    from nexus.backends.local import LocalBackend

    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)
    backend = LocalBackend(root_path=str(storage_path))

    metadata_store = RaftMetadataStore.local(str(isolated_db).replace(".db", ""))
    record_store = SQLAlchemyRecordStore()  # in-memory SQLite for tests
    nx = NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        enforce_permissions=False,
    )

    yield nx

    nx.close()
