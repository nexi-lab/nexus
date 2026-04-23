"""Pytest configuration and fixtures for e2e tests.

Provides fixtures for:
- isolated_db: Isolated SQLite database for each test
- metadata_store: Raft metadata store (from integration tests)
- record_store: In-memory SQLAlchemy record store (from integration tests)
- nexus_server: Actual nexusd process running on a free port
- test_app: httpx client for making real HTTP requests
- nexus_fs: Direct NexusFS instance (no server)

Merged from tests/integration/conftest.py and tests/e2e/conftest.py.
"""

import gc
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing, suppress
from pathlib import Path

import httpx
import pytest

from nexus.core.config import PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore

# Conditionally ignore MCP tests if fastmcp is not installed
# This must be done at collection time, before any imports from test files
try:
    import fastmcp  # noqa: F401
except ImportError:
    collect_ignore_glob = ["self_contained/mcp/*"]

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


def _drain_pipe(pipe, lines: list[str], ready: "threading.Event | None" = None):
    """Read lines from a subprocess pipe (runs in daemon thread).

    If *ready* is provided and a line contains "Application startup complete",
    the event is set — giving the caller an event-driven readiness signal
    instead of polling /health.
    """
    try:
        for raw in iter(pipe.readline, b""):
            decoded = raw.decode(errors="replace")
            lines.append(decoded)
            if ready and "Application startup complete" in decoded:
                ready.set()
    except ValueError:
        pass  # pipe closed
    finally:
        pipe.close()


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
    """Start actual nexusd process for true e2e testing.

    This fixture:
    1. Creates storage directory and database
    2. Finds a free port
    3. Starts ``nexusd`` as subprocess
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
    # Allow PostgreSQL via NEXUS_E2E_DATABASE_URL env var; default to SQLite
    env["NEXUS_DATABASE_URL"] = os.environ.get("NEXUS_E2E_DATABASE_URL", f"sqlite:///{isolated_db}")
    env["PYTHONPATH"] = str(_src_path)

    # Set API key for authenticated tests
    env["NEXUS_API_KEY"] = "test-e2e-api-key-12345"

    # Issue #788: Lower min chunk size for e2e tests (default 5MB too large for test payloads)
    env["NEXUS_UPLOAD_MIN_CHUNK_SIZE"] = "1"

    # Issue #2035: Enable RecordStore + ReBAC so skills subscribe/share/unshare
    # and share-link operations have a working EnhancedReBACManager.
    # Without this, the server starts in "bare kernel" mode (no ReBAC).
    env["NEXUS_RECORD_STORE_PATH"] = str(tmp_path / "record_store.db")

    # Issue #1186: Enable lock manager if Dragonfly/Redis is available
    dragonfly_url = env.get("NEXUS_DRAGONFLY_URL") or env.get("REDIS_URL")
    if dragonfly_url:
        env["NEXUS_DRAGONFLY_COORDINATION_URL"] = dragonfly_url
        env["NEXUS_ALLOW_SINGLE_DRAGONFLY"] = "true"

    # Start nexusd process
    # Using python -c to invoke the daemon entry point from source
    # --data-dir sets both storage path and database location
    # Uses FastAPI async server (default) for full API support including Graph API
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"from nexus.daemon.main import main; "
                f"main(['--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Use process group so we can kill all child processes
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    # Event-driven readiness: drain stdout/stderr in background threads and
    # wait for uvicorn's "Application startup complete" log line.  This is
    # deterministic (no polling race) and prevents pipe-buffer deadlocks.
    stderr_lines: list[str] = []
    stdout_lines: list[str] = []
    ready = threading.Event()

    t_err = threading.Thread(
        target=_drain_pipe, args=(process.stderr, stderr_lines, ready), daemon=True
    )
    t_out = threading.Thread(target=_drain_pipe, args=(process.stdout, stdout_lines), daemon=True)
    t_err.start()
    t_out.start()

    # 120s safety ceiling — NOT a polling interval.  The event fires the
    # instant the server emits the log line, so this only triggers on a
    # genuine hang.
    if not ready.wait(timeout=120.0):
        process.terminate()
        t_err.join(timeout=2)
        t_out.join(timeout=2)
        pytest.fail(
            f"Server failed to start on port {port} "
            f"(never saw 'Application startup complete').\n"
            f"stdout: {''.join(stdout_lines)}\n"
            f"stderr: {''.join(stderr_lines)}"
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
async def nexus_fs(isolated_db, tmp_path):
    """Create a NexusFS instance for testing (direct, no server).

    This is useful for tests that need direct access to NexusFS
    without going through HTTP.
    """
    os.environ["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"

    from nexus.backends.storage.cas_local import CASLocalBackend

    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)
    backend = CASLocalBackend(root_path=str(storage_path))

    metadata_store = RaftMetadataStore.embedded(str(isolated_db).replace(".db", ""))
    record_store = SQLAlchemyRecordStore()  # in-memory SQLite for tests
    nx = create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        permissions=PermissionConfig(enforce=False),
    )

    yield nx

    nx.close()


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    """Wait for server to be ready by polling /health endpoint.

    Shared helper — previously duplicated across multiple test files.
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(f"{url}/health", timeout=1.0, trust_env=False)
            if response.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.1)
    return False


@pytest.fixture
def metadata_store(tmp_path):
    """Create Raft metadata store for tests (primary production path).

    Uses RaftMetadataStore (Strong Consistency, primary production default).

    Returns:
        RaftMetadataStore: Raft-backed metadata store (SC mode)
    """
    store = RaftMetadataStore.embedded(str(tmp_path / "raft-metadata"))
    yield store
    # Cleanup handled by tmp_path


@pytest.fixture
def record_store():
    """Create in-memory RecordStore for tests.

    Uses in-memory SQLite for test isolation.

    Returns:
        SQLAlchemyRecordStore: In-memory SQLite record store
    """
    store = SQLAlchemyRecordStore()  # defaults to sqlite:///:memory:
    yield store
    store.close()
